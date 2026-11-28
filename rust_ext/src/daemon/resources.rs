//! daemon 资源治理面 —— 配置解析 + 主机环境侦测 + 档位映射（REQ-RG-01）。
//!
//! 本模块是 daemon 资源治理的唯一配置权威：
//!
//! 1. [`ResourceConfig`]：`~/.callwarden/daemon.toml` `[resources]`（或顶层同名键）
//!    的内存表示，内置默认值对齐现状（`DEFAULT_SNAPSHOT_CACHE_CAPACITY` = 32、
//!    `[build]` parse_threads 默认 4、idle 驱逐 30 分钟）。
//! 2. [`load_resource_config`]：读文件解析；文件缺失/损坏/单键非法/类型错 →
//!    仅该键回退默认 + WARN，不 panic 不阻断启动（容错语义照
//!    `BuildPipelineConfig::from_toml_str` 先例，build_journal.rs）。
//! 3. [`detect_host_profile`]：侦测 RAM / CPU 核数 / 磁盘类型，任何 API 失败
//!    一律 fail-soft（0 / Unknown + WARN），由 [`apply_profile`] 对 0/Unknown
//!    走 16GB 保守档。
//! 4. [`apply_profile`]：纯函数档位映射（RAM<24GB→档1；≥24GB→档2 翻倍；
//!    HDD→cache 封顶 512MB），**只覆盖 `explicit_keys` 之外的键**
//!    （daemon.toml 显式覆盖神圣，AC-RG-01-2）。
//!
//! 设计文档：`.design/daemon-resource-governance/contracts.md` C1.1-C1.4；
//! 锚定：symbol-anchor.md A6/A7/A13。
//!
//! 注意：本模块只做「配置解析 + 侦测 + 映射」。启动接线（侦测→写回
//! `[resources]`）、缓存驱逐、内存软限状态机分别在 T2/T3/T8 落地。

use std::collections::HashSet;
use std::path::PathBuf;

use super::config::DEFAULT_SNAPSHOT_CACHE_CAPACITY;

// ============================== 档位常量（O1：数值裁决可调） ==============================

/// 内置默认 cache 字节预算（2GB）——`Default` 档，映射前的保守值。
pub const DEFAULT_CACHE_MAX_BYTES: u64 = 2 * 1024 * 1024 * 1024;

/// 内置默认空闲驱逐时长（30 分钟，秒）。
pub const DEFAULT_IDLE_EVICT_SECS: u64 = 1800;

/// 档1（RAM < 24GB）cache 字节预算：512MB。
pub const TIER1_CACHE_MAX_BYTES: u64 = 512 * 1024 * 1024;

/// 档1（RAM < 24GB）cache 最大 workspace 数。
pub const TIER1_CACHE_MAX_WORKSPACES: usize = 8;

/// 档1（RAM < 24GB）build 解析 worker 数。
pub const TIER1_BUILD_PARSE_THREADS: usize = 2;

/// 档2（RAM ≥ 24GB）相对档1的翻倍系数（cache 1GB / workspaces 16 / workers 4）。
pub const TIER2_MULTIPLIER: u64 = 2;

/// 档位分界：RAM ≥ 24GB 走档2，否则（含侦测失败 RAM=0）走档1。
pub const TIER2_RAM_THRESHOLD_BYTES: u64 = 24 * 1024 * 1024 * 1024;

/// 内存软限下限：max(2GB, RAM×20%) 中的 2GB 常量。
pub const MEMORY_SOFT_LIMIT_MIN_BYTES: u64 = 2 * 1024 * 1024 * 1024;

/// 内存软限随 RAM 的比例：RAM×20%。
pub const MEMORY_SOFT_LIMIT_RAM_PERCENT: u64 = 20;

/// HDD 磁盘上 cache 字节预算封顶值（512MB）：机械盘重建慢，避免缓存抖动。
pub const HDD_CACHE_MAX_BYTES: u64 = 512 * 1024 * 1024;

// ============================== 磁盘类型 ==============================

/// 磁盘类型（影响 cache 预算封顶：Hdd → 512MB）。
///
/// - `Auto`：未判定（配置缺省/用户显式 auto）。`apply_profile` 遇到 Auto 时
///   采用侦测结果 `HostProfile.disk_type`。
/// - `Ssd`：固态（含 NVMe）。
/// - `Hdd`：机械盘（Windows 侧 v1 仅在用户显式配置或 Linux rotational=1 时产生）。
/// - `Unknown`：侦测失败/网络盘/虚拟盘/USB 等——按 SSD 处理（不封顶，O2）。
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum DiskType {
    Auto,
    Ssd,
    Hdd,
    Unknown,
}

impl DiskType {
    /// daemon.toml 写回/日志用的规范小写串。
    pub fn as_str(self) -> &'static str {
        match self {
            DiskType::Auto => "auto",
            DiskType::Ssd => "ssd",
            DiskType::Hdd => "hdd",
            DiskType::Unknown => "unknown",
        }
    }
}

// ============================== C1.1 ResourceConfig ==============================

/// 资源治理配置（daemon.toml `[resources]` / 顶层同名键）。
///
/// 默认值对齐现状：`cache_max_workspaces=32` 对齐
/// `DEFAULT_SNAPSHOT_CACHE_CAPACITY`；`build_parse_threads=4` 对齐
/// `[build]` 默认；`idle_evict_secs=1800`（30 分钟）。
///
/// `explicit_keys` 记录 daemon.toml 中**显式出现过的键**（canonical 字段名），
/// [`apply_profile`] 对这些键不覆盖（显式覆盖神圣，AC-RG-01-2）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct ResourceConfig {
    /// snapshot cache 字节预算（各 workspace 驻留 snapshot 之和的上限）。
    pub cache_max_bytes: u64,
    /// snapshot cache 最大 workspace 数（对齐 DEFAULT_SNAPSHOT_CACHE_CAPACITY）。
    pub cache_max_workspaces: usize,
    /// 空闲驱逐时长（秒）：last_access 超过该值且非 pressure 时驱逐。
    pub idle_evict_secs: u64,
    /// 进程内存软限（字节）；0 = 禁用（仅档位映射后非 0）。
    pub memory_soft_limit_bytes: u64,
    /// build 解析 worker 数（对齐 `[build]` 默认 4；档位映射可覆盖）。
    pub build_parse_threads: usize,
    /// build 线程是否以低优先级运行（默认 true：不抢前台 agent CPU）。
    pub build_priority_low: bool,
    /// 磁盘类型（Auto=随侦测；显式配置优先于侦测参与 HDD 封顶判定）。
    pub disk_type: DiskType,
    /// daemon.toml 中显式出现过的键（canonical 名）；映射时跳过。
    pub explicit_keys: HashSet<String>,
}

impl Default for ResourceConfig {
    fn default() -> Self {
        Self {
            cache_max_bytes: DEFAULT_CACHE_MAX_BYTES,
            cache_max_workspaces: DEFAULT_SNAPSHOT_CACHE_CAPACITY,
            idle_evict_secs: DEFAULT_IDLE_EVICT_SECS,
            // 0 = 禁用软限；仅档位映射（apply_profile）后非 0
            memory_soft_limit_bytes: 0,
            build_parse_threads: 4,
            build_priority_low: true,
            disk_type: DiskType::Auto,
            explicit_keys: HashSet::new(),
        }
    }
}

impl ResourceConfig {
    /// 构造（等同 `Default`；显式给出便于调用方一眼识别）。
    pub fn new() -> Self {
        Self::default()
    }

    /// daemon.toml 绝对路径（`~/.callwarden/daemon.toml`）。
    ///
    /// 路径逻辑照 `BuildPipelineConfig::config_path`（build_journal.rs）：
    /// Windows 用 `USERPROFILE`，其余用 `HOME`；都缺失时返回 None（调用方
    /// 走内置默认，fail-soft）。
    pub fn config_path() -> Option<PathBuf> {
        let home = std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME"))?;
        if home.is_empty() {
            return None;
        }
        Some(PathBuf::from(home).join(".callwarden").join("daemon.toml"))
    }

    /// 读取资源配置（C1.2）。
    ///
    /// 容错语义照 `BuildPipelineConfig` 先例，不 panic 不阻断启动：
    /// - 路径未知 / 文件不存在 → 内置默认（首次启动常态，T2 写回会补
    ///   `[resources]` 段，此处不 WARN 刷屏）；
    /// - 文件存在但不可读 → WARN + 内置默认；
    /// - 文件损坏 / 单键非法 / 类型错 → 仅该键回退默认 + WARN（见
    ///   [`Self::from_toml_str`]）。
    pub fn load_resource_config() -> Self {
        let Some(path) = Self::config_path() else {
            return Self::default();
        };
        match std::fs::read_to_string(&path) {
            Ok(content) => Self::from_toml_str(&content),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Self::default(),
            Err(e) => {
                eprintln!(
                    "[cw_daemon] [WARN] resources 配置: 读取 {} 失败（{e}），全部回退默认值",
                    path.display()
                );
                Self::default()
            }
        }
    }

    /// 从指定文件读取（测试注入点；生产走 [`Self::load_resource_config`]）。
    fn load_from_path(path: &PathBuf) -> Self {
        match std::fs::read_to_string(path) {
            Ok(content) => Self::from_toml_str(&content),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Self::default(),
            Err(e) => {
                eprintln!(
                    "[cw_daemon] [WARN] resources 配置: 读取 {} 失败（{e}），全部回退默认值",
                    path.display()
                );
                Self::default()
            }
        }
    }

    /// 从 toml 文本解析（纯函数，便于测试）。
    ///
    /// 支持**顶层键**与 **`[resources]` 表**两种写法；同名键顶层优先（照
    /// `BuildPipelineConfig::from_toml_str` 的 lookup 先例）。
    ///
    /// 容错：整文件损坏 → 全默认（WARN）；单键非法/类型错 → 仅该键回退默认
    /// （WARN），其余键正常解析。
    ///
    /// `explicit_keys` 按**出现即记录**（不要求值合法）：daemon.toml 中显式
    /// 出现过的键（canonical 字段名）都会进入集合，`apply_profile` 据此跳过
    /// 映射覆盖（AC-RG-01-2 显式覆盖神圣）。
    pub fn from_toml_str(content: &str) -> Self {
        let mut cfg = Self::default();
        let Ok(doc) = toml::from_str::<toml::Value>(content) else {
            eprintln!(
                "[cw_daemon] [WARN] resources 配置: daemon.toml 解析失败，[resources] 全部回退默认值"
            );
            return cfg;
        };
        let resources_tbl = doc.get("resources");

        // lookup：顶层键优先，其次 [resources] 表；返回 (值, 是否显式出现)
        let lookup = |key: &str| -> (Option<&toml::Value>, bool) {
            let top = doc.get(key);
            let in_tbl = resources_tbl.and_then(|t| t.get(key));
            let explicit = top.is_some() || in_tbl.is_some();
            // 顶层优先：顶层存在（即使类型错）也以顶层为准
            let value = if top.is_some() { top } else { in_tbl };
            (value, explicit)
        };
        let mut explicit_keys = HashSet::new();

        // ---- cache_max_bytes：整数或 "512M"/"1G"/纯字节数字符串 ----
        let (value, explicit) = lookup("cache_max_bytes");
        if explicit {
            explicit_keys.insert("cache_max_bytes".to_string());
        }
        if let Some(v) = value {
            match parse_cache_max_bytes(v) {
                Ok(bytes) => cfg.cache_max_bytes = bytes,
                Err(reason) => warn_invalid_key(
                    "cache_max_bytes",
                    v,
                    reason,
                    &cfg.cache_max_bytes.to_string(),
                ),
            }
        }

        // ---- cache_max_workspaces：正整数 ----
        let (value, explicit) = lookup("cache_max_workspaces");
        if explicit {
            explicit_keys.insert("cache_max_workspaces".to_string());
        }
        if let Some(v) = value {
            match v.as_integer().and_then(|n| usize::try_from(n).ok()) {
                Some(n) if n > 0 => {
                    cfg.cache_max_workspaces = n;
                }
                _ => warn_invalid_key(
                    "cache_max_workspaces",
                    v,
                    "必须是正整数",
                    &cfg.cache_max_workspaces.to_string(),
                ),
            }
        }

        // ---- idle_evict_secs：正整数（0 会让空闲驱逐退化为全清，拒收） ----
        let (value, explicit) = lookup("idle_evict_secs");
        if explicit {
            explicit_keys.insert("idle_evict_secs".to_string());
        }
        if let Some(v) = value {
            match v.as_integer() {
                Some(n) if n > 0 => cfg.idle_evict_secs = n as u64,
                _ => warn_invalid_key(
                    "idle_evict_secs",
                    v,
                    "必须是正整数（秒）",
                    &cfg.idle_evict_secs.to_string(),
                ),
            }
        }

        // ---- memory_soft_limit_bytes：非负整数或尺寸串（0=禁用软限，合法） ----
        let (value, explicit) = lookup("memory_soft_limit_bytes");
        if explicit {
            explicit_keys.insert("memory_soft_limit_bytes".to_string());
        }
        if let Some(v) = value {
            match parse_size_value(v) {
                Ok(bytes) => cfg.memory_soft_limit_bytes = bytes,
                Err(reason) => warn_invalid_key(
                    "memory_soft_limit_bytes",
                    v,
                    reason,
                    &cfg.memory_soft_limit_bytes.to_string(),
                ),
            }
        }

        // ---- build_parse_threads：正整数 ----
        let (value, explicit) = lookup("build_parse_threads");
        if explicit {
            explicit_keys.insert("build_parse_threads".to_string());
        }
        if let Some(v) = value {
            match v.as_integer().and_then(|n| usize::try_from(n).ok()) {
                Some(n) if n > 0 => {
                    cfg.build_parse_threads = n;
                }
                _ => warn_invalid_key(
                    "build_parse_threads",
                    v,
                    "必须是正整数",
                    &cfg.build_parse_threads.to_string(),
                ),
            }
        }

        // ---- build_priority_low：布尔 ----
        let (value, explicit) = lookup("build_priority_low");
        if explicit {
            explicit_keys.insert("build_priority_low".to_string());
        }
        if let Some(v) = value {
            match v.as_bool() {
                Some(b) => cfg.build_priority_low = b,
                None => warn_invalid_key(
                    "build_priority_low",
                    v,
                    "必须是布尔值",
                    &cfg.build_priority_low.to_string(),
                ),
            }
        }

        // ---- disk_type：auto/ssd/hdd/unknown（大小写不敏感） ----
        let (value, explicit) = lookup("disk_type");
        if explicit {
            explicit_keys.insert("disk_type".to_string());
        }
        if let Some(v) = value {
            match v.as_str().and_then(parse_disk_type) {
                Some(dt) => cfg.disk_type = dt,
                None => warn_invalid_key(
                    "disk_type",
                    v,
                    "必须是 auto/ssd/hdd/unknown（大小写不敏感）",
                    cfg.disk_type.as_str(),
                ),
            }
        }

        cfg.explicit_keys = explicit_keys;
        cfg
    }
}

/// 解析 disk_type 字符串（大小写不敏感）；非法返回 None。
fn parse_disk_type(s: &str) -> Option<DiskType> {
    match s.trim().to_ascii_lowercase().as_str() {
        "auto" => Some(DiskType::Auto),
        "ssd" => Some(DiskType::Ssd),
        "hdd" => Some(DiskType::Hdd),
        "unknown" => Some(DiskType::Unknown),
        _ => None,
    }
}

/// 解析 cache_max_bytes：正整数或尺寸串（"512M"/"1G"/"2048"）。
/// 0 会使字节预算驱逐退化为常驱逐，拒收；负数/溢出/非法单位 → Err。
fn parse_cache_max_bytes(v: &toml::Value) -> Result<u64, &'static str> {
    let bytes = parse_size_value(v)?;
    if bytes == 0 {
        return Err("必须为正字节数（0 会退化为常驱逐）");
    }
    Ok(bytes)
}

/// 解析尺寸值：整数（≥0，字节数）或 "512M"/"1G"/"2048" 字符串。
/// 单位 K/M/G/T 大小写不敏感；与 Python `_is_valid_size_string`（daemon_config.py）
/// 口径对齐：数字+可选单位，不接受小数/负数。
fn parse_size_value(v: &toml::Value) -> Result<u64, &'static str> {
    match v {
        toml::Value::Integer(n) => {
            if *n < 0 {
                Err("不能为负数")
            } else {
                Ok(*n as u64)
            }
        }
        toml::Value::String(s) => parse_size_string(s),
        _ => Err("必须是整数或尺寸字符串（如 \"512M\"/\"1G\"/\"2048\"）"),
    }
}

/// 解析尺寸字符串：纯数字（字节）或 数字+K/M/G/T 单位（大小写不敏感）。
/// 溢出/空串/非法格式 → None。
fn parse_size_string(s: &str) -> Result<u64, &'static str> {
    let t = s.trim();
    if t.is_empty() {
        return Err("尺寸字符串为空");
    }
    // 纯数字 = 字节数
    if let Ok(v) = t.parse::<u64>() {
        return Ok(v);
    }
    // 数字 + 单位
    if t.len() < 2 {
        return Err("尺寸字符串格式非法");
    }
    let (num_part, unit) = t.split_at(t.len() - 1);
    let mult: u64 = match unit.to_ascii_uppercase().as_str() {
        "K" => 1024,
        "M" => 1024 * 1024,
        "G" => 1024 * 1024 * 1024,
        "T" => 1024 * 1024 * 1024 * 1024,
        _ => return Err("尺寸单位必须是 K/M/G/T"),
    };
    let v: u64 = num_part
        .trim()
        .parse()
        .map_err(|_| "尺寸数值部分必须是整数")?;
    v.checked_mul(mult).ok_or_else(|| "尺寸数值溢出")
}

/// 单键非法的统一 WARN 输出（不 panic 不阻断）。
fn warn_invalid_key(key: &str, value: &toml::Value, reason: &str, fallback: &str) {
    eprintln!(
        "[cw_daemon] [WARN] resources 配置: 键 `{key}` 非法（{value}；{reason}），回退默认 {fallback}"
    );
}

#[cfg(test)]
mod tests {
    use super::*;
    use super::super::fs_handlers::build_journal::BuildPipelineConfig;

    /// Default 与现状值对齐（C1.1 测试要点：32/4/1800）。
    #[test]
    fn default_aligns_with_current_values() {
        let cfg = ResourceConfig::default();
        assert_eq!(
            cfg.cache_max_workspaces,
            DEFAULT_SNAPSHOT_CACHE_CAPACITY,
            "cache_max_workspaces 必须对齐 DEFAULT_SNAPSHOT_CACHE_CAPACITY"
        );
        assert_eq!(
            cfg.build_parse_threads,
            BuildPipelineConfig::default().parse_threads,
            "build_parse_threads 必须对齐 [build] 默认"
        );
        assert_eq!(cfg.idle_evict_secs, 1800, "空闲驱逐默认 30 分钟");
        assert_eq!(cfg.cache_max_bytes, 2 * 1024 * 1024 * 1024);
        assert_eq!(cfg.memory_soft_limit_bytes, 0, "默认禁用软限（0）");
        assert!(cfg.build_priority_low, "build 默认低优先级");
        assert_eq!(cfg.disk_type, DiskType::Auto);
        assert!(cfg.explicit_keys.is_empty());
        // new() 与 default() 等价
        assert_eq!(ResourceConfig::new(), ResourceConfig::default());
    }

    #[test]
    fn disk_type_as_str_roundtrip_labels() {
        assert_eq!(DiskType::Auto.as_str(), "auto");
        assert_eq!(DiskType::Ssd.as_str(), "ssd");
        assert_eq!(DiskType::Hdd.as_str(), "hdd");
        assert_eq!(DiskType::Unknown.as_str(), "unknown");
    }

    // ============================== C1.2 load_resource_config ==============================

    /// AC-RG-01-3：损坏 toml → 全默认 + explicit_keys 为空，不 panic。
    #[test]
    fn ac_rg_01_3_corrupt_toml_falls_back_to_default() {
        for bad in [
            "cache_max_bytes = [",        // 未闭合
            "not a toml at all {{{",      // 语法错
            "",                            // 空文件（等价无键）
        ] {
            let cfg = ResourceConfig::from_toml_str(bad);
            assert_eq!(cfg, ResourceConfig::default(), "损坏配置 {bad:?} 应回退全默认");
        }
    }

    /// 单键非法 → 仅该键回退默认，其余键正常解析（E1 容错矩阵）。
    #[test]
    fn single_invalid_key_falls_back_per_key() {
        let content = r#"
[resources]
cache_max_workspaces = 0
build_parse_threads = 2
idle_evict_secs = 60
"#;
        let cfg = ResourceConfig::from_toml_str(content);
        assert_eq!(cfg.cache_max_workspaces, 32, "非法键回退默认 32");
        assert_eq!(cfg.build_parse_threads, 2, "合法键正常解析");
        assert_eq!(cfg.idle_evict_secs, 60, "合法键正常解析");
        // 出现即记录：非法键也在 explicit_keys（用户表达过偏好）
        assert!(cfg.explicit_keys.contains("cache_max_workspaces"));
        assert!(cfg.explicit_keys.contains("build_parse_threads"));
        assert!(cfg.explicit_keys.contains("idle_evict_secs"));
        assert_eq!(cfg.explicit_keys.len(), 3);
    }

    /// cache_max_bytes 三种合法形态 + 非法形态（照 _is_valid_size_string 口径）。
    #[test]
    fn cache_max_bytes_accepts_size_strings_and_bytes() {
        let cfg = ResourceConfig::from_toml_str("[resources]\ncache_max_bytes = \"512M\"\n");
        assert_eq!(cfg.cache_max_bytes, 536_870_912, "512M = 536870912（AC-RG-01-1 断言值）");
        let cfg = ResourceConfig::from_toml_str("[resources]\ncache_max_bytes = \"1G\"\n");
        assert_eq!(cfg.cache_max_bytes, 1_073_741_824, "1G = 1073741824");
        let cfg = ResourceConfig::from_toml_str("[resources]\ncache_max_bytes = 2048\n");
        assert_eq!(cfg.cache_max_bytes, 2048, "纯整数 = 字节数");
        let cfg = ResourceConfig::from_toml_str("[resources]\ncache_max_bytes = \"2048\"\n");
        assert_eq!(cfg.cache_max_bytes, 2048, "纯数字字符串 = 字节数");
        // 非法形态 → 回退默认 2GB
        for bad in ["\"abc\"", "\"1.5G\"", "\"12X\"", "-1024", "0", "\"\""] {
            let content = format!("[resources]\ncache_max_bytes = {bad}\n");
            let cfg = ResourceConfig::from_toml_str(&content);
            assert_eq!(
                cfg.cache_max_bytes,
                2 * 1024 * 1024 * 1024,
                "非法值 {bad} 应回退默认 2GB"
            );
        }
    }

    /// memory_soft_limit_bytes：0=禁用合法；负数非法；尺寸串合法。
    #[test]
    fn memory_soft_limit_accepts_zero_and_size_strings() {
        let cfg = ResourceConfig::from_toml_str("[resources]\nmemory_soft_limit_bytes = 0\n");
        assert_eq!(cfg.memory_soft_limit_bytes, 0, "0=禁用软限，合法");
        let cfg = ResourceConfig::from_toml_str("[resources]\nmemory_soft_limit_bytes = \"2G\"\n");
        assert_eq!(cfg.memory_soft_limit_bytes, 2 * 1024 * 1024 * 1024);
        let cfg = ResourceConfig::from_toml_str("[resources]\nmemory_soft_limit_bytes = -1\n");
        assert_eq!(cfg.memory_soft_limit_bytes, 0, "负数回退默认 0");
    }

    /// 顶层键与 [resources] 表两种写法都支持；同名键顶层优先。
    #[test]
    fn top_level_keys_and_resources_table_both_supported() {
        let content = r#"
idle_evict_secs = 120
[resources]
cache_max_bytes = "512M"
idle_evict_secs = 60
"#;
        let cfg = ResourceConfig::from_toml_str(content);
        assert_eq!(cfg.cache_max_bytes, 536_870_912, "[resources] 表键生效");
        assert_eq!(cfg.idle_evict_secs, 120, "顶层键优先于表内同名键");
        assert!(cfg.explicit_keys.contains("idle_evict_secs"));
        assert_eq!(cfg.explicit_keys.len(), 2);
    }

    /// 类型错 → 该键回退默认 + 其余键不受影响（E1）。
    #[test]
    fn type_errors_fall_back_per_key() {
        let content = r#"
[resources]
cache_max_bytes = true
build_priority_low = 1
disk_type = "TURBO"
build_parse_threads = 3
"#;
        let cfg = ResourceConfig::from_toml_str(content);
        assert_eq!(cfg.cache_max_bytes, 2 * 1024 * 1024 * 1024, "布尔回退默认");
        assert!(cfg.build_priority_low, "整数当布尔回退默认 true");
        assert_eq!(cfg.disk_type, DiskType::Auto, "非法 disk_type 回退 Auto");
        assert_eq!(cfg.build_parse_threads, 3, "合法键不受影响");
        assert_eq!(cfg.explicit_keys.len(), 4, "出现即记录（含类型错键）");
    }

    /// disk_type 大小写不敏感解析。
    #[test]
    fn disk_type_parsing_case_insensitive() {
        for (s, expected) in [
            ("\"auto\"", DiskType::Auto),
            ("\"SSD\"", DiskType::Ssd),
            ("\"Hdd\"", DiskType::Hdd),
            ("\"UNKNOWN\"", DiskType::Unknown),
            ("\" Ssd \"", DiskType::Ssd),
        ] {
            let content = format!("[resources]\ndisk_type = {s}\n");
            let cfg = ResourceConfig::from_toml_str(&content);
            assert_eq!(cfg.disk_type, expected, "disk_type {s} 应解析为 {expected:?}");
        }
    }

    /// load_from_path：文件缺失 → 默认（静默）；损坏 → 默认；合法 → 解析。
    #[test]
    fn load_from_path_handles_missing_and_corrupt_files() {
        let dir = tempfile::tempdir().expect("tempdir");
        // 文件缺失
        let missing = dir.path().join("missing.toml");
        assert_eq!(ResourceConfig::load_from_path(&missing), ResourceConfig::default());
        // 损坏文件
        let corrupt = dir.path().join("corrupt.toml");
        std::fs::write(&corrupt, "resources = [[[").expect("write");
        assert_eq!(ResourceConfig::load_from_path(&corrupt), ResourceConfig::default());
        // 合法文件
        let valid = dir.path().join("valid.toml");
        std::fs::write(&valid, "[resources]\nbuild_parse_threads = 6\n").expect("write");
        let cfg = ResourceConfig::load_from_path(&valid);
        assert_eq!(cfg.build_parse_threads, 6);
        assert!(cfg.explicit_keys.contains("build_parse_threads"));
    }

    /// config_path 与 BuildPipelineConfig::config_path 同源（USERPROFILE/HOME）。
    #[test]
    fn config_path_matches_build_pipeline_precedent() {
        let ours = ResourceConfig::config_path();
        let precedent =
            super::super::fs_handlers::build_journal::BuildPipelineConfig::config_path();
        assert_eq!(ours, precedent, "路径逻辑必须与 [build] 先例一致");
    }

    /// parse_size_string 边界（单位大小写/空串/溢出）。
    #[test]
    fn parse_size_string_edges() {
        assert_eq!(parse_size_string("512m").ok(), Some(536_870_912));
        assert_eq!(parse_size_string(" 2G ").ok(), Some(2 * 1024 * 1024 * 1024));
        assert_eq!(parse_size_string("1T").ok(), Some(1024u64 * 1024 * 1024 * 1024));
        assert!(parse_size_string("").is_err());
        assert!(parse_size_string("K").is_err());
        assert!(parse_size_string("1.5G").is_err());
        assert!(parse_size_string("-1G").is_err());
        assert!(parse_size_string("99999999999999999999T").is_err(), "溢出拒收");
    }
}
