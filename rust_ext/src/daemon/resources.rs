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

// ============================== C1.3 HostProfile + detect_host_profile ==============================

/// 主机环境侦测结果（C1.3）。
///
/// fail-soft 约定：任何侦测 API 失败 → 对应字段取保守值
/// （RAM=0、cores=1、disk=Unknown），**不 panic**；由 [`apply_profile`]
/// 对 0/Unknown 走 16GB 保守档（E2）。
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct HostProfile {
    /// 物理内存总字节；侦测失败 = 0。
    pub total_ram_bytes: u64,
    /// CPU 逻辑核数（available_parallelism）；失败 = 1。
    pub cpu_cores: usize,
    /// 磁盘类型；侦测失败/网络盘/虚拟盘 = Unknown（按 SSD 处理，O2）。
    pub disk_type: DiskType,
}

/// 侦测主机环境（RAM / CPU 核数 / 磁盘类型）。
///
/// - RAM：Windows `GlobalMemoryStatusEx`；Linux `/proc/meminfo` MemTotal。
/// - 核数：`std::thread::available_parallelism`（与 fs_handlers worker 计算同源）。
/// - 磁盘：Windows `IOCTL_STORAGE_QUERY_PROPERTY` BusType（NVMe→Ssd，
///   其余/失败/网络盘/WSL→Unknown，不 panic）；Linux `/sys/block/*/queue/rotational`
///   （含 1→Hdd，全 0→Ssd，无数据→Unknown）。
///
/// 任何失败 fail-soft（WARN + 保守值），由 apply_profile 走 16GB 保守档。
pub fn detect_host_profile() -> HostProfile {
    HostProfile {
        total_ram_bytes: detect_total_ram(),
        cpu_cores: detect_cpu_cores(),
        disk_type: detect_disk_type(),
    }
}

/// 侦测物理内存总字节（fail-soft：失败 = 0 + WARN）。
#[cfg(windows)]
fn detect_total_ram() -> u64 {
    use windows_sys::Win32::System::SystemInformation::{GlobalMemoryStatusEx, MEMORYSTATUSEX};
    unsafe {
        let mut status: MEMORYSTATUSEX = std::mem::zeroed();
        status.dwLength = std::mem::size_of::<MEMORYSTATUSEX>() as u32;
        if GlobalMemoryStatusEx(&mut status) != 0 {
            status.ullTotalPhys
        } else {
            eprintln!(
                "[cw_daemon] [WARN] resources 侦测: GlobalMemoryStatusEx 失败（{}），RAM 按 0 处理（走保守档）",
                std::io::Error::last_os_error()
            );
            0
        }
    }
}

/// Linux：/proc/meminfo MemTotal（kB）→ 字节（fail-soft：失败 = 0 + WARN）。
#[cfg(unix)]
fn detect_total_ram() -> u64 {
    match std::fs::read_to_string("/proc/meminfo") {
        Ok(text) => {
            let kb = parse_proc_meminfo_memtotal(&text);
            if kb == 0 {
                eprintln!(
                    "[cw_daemon] [WARN] resources 侦测: /proc/meminfo 无 MemTotal 字段，RAM 按 0 处理（走保守档）"
                );
            }
            kb.saturating_mul(1024)
        }
        Err(e) => {
            eprintln!(
                "[cw_daemon] [WARN] resources 侦测: 读 /proc/meminfo 失败（{e}），RAM 按 0 处理（走保守档）"
            );
            0
        }
    }
}

/// 其他平台：无实现，保守 0（fail-soft）。
#[cfg(not(any(windows, unix)))]
fn detect_total_ram() -> u64 {
    eprintln!("[cw_daemon] [WARN] resources 侦测: 当前平台无 RAM 侦测实现，按 0 处理（走保守档）");
    0
}

/// 解析 /proc/meminfo 文本中的 MemTotal（kB）；缺失/非法 → 0（纯函数，便于测试）。
fn parse_proc_meminfo_memtotal(text: &str) -> u64 {
    for line in text.lines() {
        if let Some(rest) = line.strip_prefix("MemTotal:") {
            return rest
                .trim()
                .split_whitespace()
                .next()
                .and_then(|n| n.parse::<u64>().ok())
                .unwrap_or(0);
        }
    }
    0
}

/// 侦测 CPU 逻辑核数（失败 = 1 + WARN，available_parallelism 兜底语义照 fs_handlers）。
fn detect_cpu_cores() -> usize {
    match std::thread::available_parallelism() {
        Ok(n) => n.get(),
        Err(e) => {
            eprintln!(
                "[cw_daemon] [WARN] resources 侦测: available_parallelism 失败（{e}），核数按 1 处理"
            );
            1
        }
    }
}

/// 侦测系统盘/数据盘类型（fail-soft：失败 = Unknown + WARN）。
///
/// Windows：对 HOME 所在盘符（缺省回落系统盘 C:）发
/// `IOCTL_STORAGE_QUERY_PROPERTY` 读 BusType；**NVMe→Ssd**；其余总线
/// （SATA/SAS/SCSI/ATA/USB/虚拟盘等）v1 无法从 BusType 区分 SSD/HDD →
/// Unknown（按 SSD 处理，不封顶；O2 fail-soft 口径）。
#[cfg(windows)]
fn detect_disk_type() -> DiskType {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::Storage::FileSystem::{
        CreateFileW, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
    };
    use windows_sys::Win32::System::IO::DeviceIoControl;
    use windows_sys::Win32::System::Ioctl::{
        IOCTL_STORAGE_QUERY_PROPERTY, PropertyStandardQuery, StorageDeviceProperty,
        STORAGE_DEVICE_DESCRIPTOR, STORAGE_PROPERTY_QUERY,
    };

    // HOME 所在盘；无盘符（如 UNC/未知 HOME）回落系统盘 C:
    let drive = home_drive_letter().unwrap_or_else(|| {
        eprintln!(
            "[cw_daemon] [WARN] resources 侦测: HOME 无盘符，磁盘类型探测回落系统盘 C:"
        );
        "C:".to_string()
    });
    // \\.\C: 卷设备路径
    let root: Vec<u16> = format!("\\\\.\\{drive}\0").encode_utf16().collect();
    unsafe {
        let handle = CreateFileW(
            root.as_ptr(),
            0, // 设备属性查询无需读写权限
            FILE_SHARE_READ | FILE_SHARE_WRITE,
            std::ptr::null(),
            OPEN_EXISTING,
            0,
            std::ptr::null_mut(),
        );
        if handle == INVALID_HANDLE_VALUE || handle.is_null() {
            eprintln!(
                "[cw_daemon] [WARN] resources 侦测: 打开 {drive} 设备失败（{}），磁盘类型按 Unknown",
                std::io::Error::last_os_error()
            );
            return DiskType::Unknown;
        }
        let mut query = STORAGE_PROPERTY_QUERY {
            PropertyId: StorageDeviceProperty,
            QueryType: PropertyStandardQuery,
            AdditionalParameters: [0u8; 1],
        };
        // 描述符头 + 供应商/型号字符串余量
        let mut buf = [0u8; std::mem::size_of::<STORAGE_DEVICE_DESCRIPTOR>() + 1024];
        let mut returned: u32 = 0;
        let ok = DeviceIoControl(
            handle,
            IOCTL_STORAGE_QUERY_PROPERTY,
            &query as *const STORAGE_PROPERTY_QUERY as *const core::ffi::c_void,
            std::mem::size_of::<STORAGE_PROPERTY_QUERY>() as u32,
            buf.as_mut_ptr() as *mut core::ffi::c_void,
            buf.len() as u32,
            &mut returned,
            std::ptr::null_mut(),
        );
        CloseHandle(handle);
        if ok == 0 || returned < std::mem::size_of::<STORAGE_DEVICE_DESCRIPTOR>() as u32 {
            eprintln!(
                "[cw_daemon] [WARN] resources 侦测: {drive} IOCTL_STORAGE_QUERY_PROPERTY 失败（{}），磁盘类型按 Unknown",
                std::io::Error::last_os_error()
            );
            return DiskType::Unknown;
        }
        // 描述符头在缓冲区起始（非对齐安全读取）
        let descriptor =
            unsafe { std::ptr::read_unaligned(buf.as_ptr() as *const STORAGE_DEVICE_DESCRIPTOR) };
        bus_type_to_disk_type(descriptor.BusType)
    }
}

/// Linux：扫描 /sys/block/*/queue/rotational（排除 loop/ram/dm- 虚拟设备）。
/// 任一 rotational=1 → Hdd；全 0 → Ssd；无数据（WSL1/容器）→ Unknown + WARN。
#[cfg(unix)]
fn detect_disk_type() -> DiskType {
    let mut flags: Vec<bool> = Vec::new();
    match std::fs::read_dir("/sys/block") {
        Ok(entries) => {
            for entry in entries.flatten() {
                let name = entry.file_name().to_string_lossy().to_string();
                if name.starts_with("loop") || name.starts_with("ram") || name.starts_with("dm-") {
                    continue;
                }
                if let Ok(content) = std::fs::read_to_string(entry.path().join("queue/rotational")) {
                    flags.push(content.trim() == "1");
                }
            }
        }
        Err(e) => {
            eprintln!(
                "[cw_daemon] [WARN] resources 侦测: 读 /sys/block 失败（{e}），磁盘类型按 Unknown"
            );
        }
    }
    let result = rotational_flags_to_disk_type(&flags);
    if result == DiskType::Unknown {
        eprintln!(
            "[cw_daemon] [WARN] resources 侦测: /sys/block 无可用 rotational 信息（WSL1/容器？），磁盘类型按 Unknown"
        );
    }
    result
}

/// 其他平台：无实现，保守 Unknown（fail-soft）。
#[cfg(not(any(windows, unix)))]
fn detect_disk_type() -> DiskType {
    eprintln!("[cw_daemon] [WARN] resources 侦测: 当前平台无磁盘类型侦测实现，按 Unknown");
    DiskType::Unknown
}

/// BusType → DiskType：NVMe(17) → Ssd；其余（SATA/SAS/SCSI/ATA/USB/虚拟等
/// 无法从总线区分 SSD/HDD）→ Unknown（O2：Unknown 按 SSD 处理，不封顶）。
fn bus_type_to_disk_type(bus_type: i32) -> DiskType {
    match bus_type {
        17 => DiskType::Ssd, // BusTypeNvme
        _ => DiskType::Unknown,
    }
}

/// /sys/block rotational 标记集合 → DiskType（纯函数，便于测试）。
/// 任一 true → Hdd；非空且全 false → Ssd；空 → Unknown。
fn rotational_flags_to_disk_type(flags: &[bool]) -> DiskType {
    if flags.is_empty() {
        return DiskType::Unknown;
    }
    if flags.iter().any(|&f| f) {
        DiskType::Hdd
    } else {
        DiskType::Ssd
    }
}

/// 提取 HOME 路径的盘符（Windows 专用，如 "C:"）；非盘符路径返回 None。
#[cfg(windows)]
fn home_drive_letter() -> Option<String> {
    let home = std::env::var_os("USERPROFILE").or_else(|| std::env::var_os("HOME"))?;
    let s = home.to_string_lossy();
    let bytes = s.as_bytes();
    if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
        Some(s[..2].to_ascii_uppercase())
    } else {
        None
    }
}

/// 档位映射（C1.4）：把 [`HostProfile`] 映射进 [`ResourceConfig`]。
///
/// 纯函数，原地修改，返回本次被映射覆盖的键名（canonical 名，固定顺序：
/// `cache_max_bytes` → `cache_max_workspaces` → `build_parse_threads` →
/// `memory_soft_limit_bytes`，供启动日志）。
///
/// 规则（contracts.md C1.4 / t1-steps.json step#4）：
/// - RAM < 24GB → 档1：cache 512MB / workspaces 8 / workers 2 /
///   soft = max(2GB, RAM×20%)
/// - RAM ≥ 24GB → 档2：翻倍（cache 1GB / workspaces 16 / workers 4），
///   soft 公式同档1（RAM≥24GB 时 RAM×20% ≥ 4.8GB，2GB 下限不触发）
/// - 生效 disk_type = Hdd → `cache_max_bytes` 封顶 512MB（档2 也封顶）；
///   生效值取显式 `disk_type`（≠ Auto），否则取 profile 侦测值
/// - RAM = 0（侦测失败）→ 走档1（16GB 保守档）
/// - `explicit_keys` 中的键**不覆盖**（显式覆盖神圣，AC-RG-01-2），也
///   不进入返回列表；HDD 封顶同样不触碰显式键
/// - `idle_evict_secs` / `build_priority_low` 不参与映射（卡面未列）
pub fn apply_profile(cfg: &mut ResourceConfig, profile: &HostProfile) -> Vec<String> {
    let mut covered: Vec<String> = Vec::with_capacity(4);

    // RAM=0（侦测失败）< 24GB → 档1（保守档）
    let tier2 = profile.total_ram_bytes >= TIER2_RAM_THRESHOLD_BYTES;

    if !cfg.explicit_keys.contains("cache_max_bytes") {
        cfg.cache_max_bytes = if tier2 {
            TIER1_CACHE_MAX_BYTES * TIER2_MULTIPLIER
        } else {
            TIER1_CACHE_MAX_BYTES
        };
        covered.push("cache_max_bytes".to_string());
    }
    if !cfg.explicit_keys.contains("cache_max_workspaces") {
        cfg.cache_max_workspaces = if tier2 {
            TIER1_CACHE_MAX_WORKSPACES * TIER2_MULTIPLIER as usize
        } else {
            TIER1_CACHE_MAX_WORKSPACES
        };
        covered.push("cache_max_workspaces".to_string());
    }
    if !cfg.explicit_keys.contains("build_parse_threads") {
        cfg.build_parse_threads = if tier2 {
            TIER1_BUILD_PARSE_THREADS * TIER2_MULTIPLIER as usize
        } else {
            TIER1_BUILD_PARSE_THREADS
        };
        covered.push("build_parse_threads".to_string());
    }
    if !cfg.explicit_keys.contains("memory_soft_limit_bytes") {
        // max(2GB, RAM×20%)；saturating_mul 防 u64 溢出
        let by_ram = profile
            .total_ram_bytes
            .saturating_mul(MEMORY_SOFT_LIMIT_RAM_PERCENT)
            / 100;
        cfg.memory_soft_limit_bytes = MEMORY_SOFT_LIMIT_MIN_BYTES.max(by_ram);
        covered.push("memory_soft_limit_bytes".to_string());
    }

    // HDD 封顶：档2 的 1GB 也压回 512MB；显式键不封顶
    let effective_disk = if cfg.disk_type != DiskType::Auto {
        cfg.disk_type
    } else {
        profile.disk_type
    };
    if effective_disk == DiskType::Hdd
        && !cfg.explicit_keys.contains("cache_max_bytes")
        && cfg.cache_max_bytes > HDD_CACHE_MAX_BYTES
    {
        cfg.cache_max_bytes = HDD_CACHE_MAX_BYTES;
    }

    covered
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

    // ============================== C1.3 detect_host_profile ==============================

    /// AC-RG-01-4：侦测容错分支——MemTotal 缺失/垃圾文本 → 0（不 panic）。
    #[test]
    fn parse_proc_meminfo_fail_soft() {
        assert_eq!(parse_proc_meminfo_memtotal(""), 0);
        assert_eq!(parse_proc_meminfo_memtotal("not a meminfo at all"), 0);
        assert_eq!(parse_proc_meminfo_memtotal("MemTotal:  abc kB\n"), 0);
        assert_eq!(parse_proc_meminfo_memtotal("MemTotal: -5 kB\n"), 0, "负数拒收");
        assert_eq!(parse_proc_meminfo_memtotal("MemTotal: 16384 kB\n"), 16_384);
        assert_eq!(
            parse_proc_meminfo_memtotal(
                "Buffers: 1 kB\nMemTotal:       67108864 kB\nSwapTotal: 0 kB\n"
            ),
            67_108_864,
            "按前缀匹配，容忍其他字段"
        );
    }

    /// BusType → DiskType：NVMe→Ssd，其余总线（无法区分 SSD/HDD）→ Unknown。
    #[test]
    fn bus_type_nvme_maps_to_ssd_others_unknown() {
        assert_eq!(bus_type_to_disk_type(17), DiskType::Ssd, "BusTypeNvme");
        // 0=Unknown 1=Scsi 2=Atapi 3=Ata 7=Usb 8=RAID 10=Sas 11=Sata 14=Virtual
        for bus in [0, 1, 2, 3, 7, 8, 10, 11, 14, 15, 99] {
            assert_eq!(
                bus_type_to_disk_type(bus),
                DiskType::Unknown,
                "BusType {bus} 无法区分 SSD/HDD，应按 Unknown（O2）"
            );
        }
    }

    /// rotational 标记集合 → DiskType：任一 1→Hdd；全 0→Ssd；空→Unknown。
    #[test]
    fn rotational_flags_map_to_disk_type() {
        assert_eq!(rotational_flags_to_disk_type(&[]), DiskType::Unknown);
        assert_eq!(rotational_flags_to_disk_type(&[false]), DiskType::Ssd);
        assert_eq!(rotational_flags_to_disk_type(&[false, false]), DiskType::Ssd);
        assert_eq!(rotational_flags_to_disk_type(&[true]), DiskType::Hdd);
        assert_eq!(rotational_flags_to_disk_type(&[false, true]), DiskType::Hdd, "混合按 HDD 保守处理");
    }

    /// AC-RG-01-4：HostProfile 可表达侦测失败态（0/1/Unknown），供 apply_profile 走保守档。
    #[test]
    fn host_profile_fail_soft_values_constructible() {
        let failed = HostProfile {
            total_ram_bytes: 0,
            cpu_cores: 1,
            disk_type: DiskType::Unknown,
        };
        assert_eq!(failed.total_ram_bytes, 0);
        assert_eq!(failed.cpu_cores, 1);
        assert_eq!(failed.disk_type, DiskType::Unknown);
    }

    /// 当前平台 smoke：侦测不 panic，返回值落在合法域（Windows 上 RAM 必 > 0）。
    #[test]
    fn detect_host_profile_smoke_current_platform() {
        let profile = detect_host_profile();
        assert!(profile.cpu_cores >= 1, "核数至少为 1");
        assert!(
            matches!(
                profile.disk_type,
                DiskType::Auto | DiskType::Ssd | DiskType::Hdd | DiskType::Unknown
            ),
            "disk_type 必须是合法枚举值"
        );
        // 本机（Windows 一线平台）GlobalMemoryStatusEx 正常时 RAM > 0；
        // 若 API 失败按 fail-soft=0，此处只断言不 panic + 值域合法。
        if cfg!(windows) {
            assert!(profile.total_ram_bytes > 0, "Windows 侦测失败应已 WARN，正常路径 RAM > 0");
        }
    }

    /// home_drive_letter：盘符路径提取 + 非盘符路径返回 None（仅 Windows 语义）。
    #[test]
    #[cfg(windows)]
    fn home_drive_letter_extracts_from_profile_path() {
        // 用环境变量同源逻辑构造：不依赖真实 USERPROFILE 内容，直接测判定逻辑
        // 的镜像实现——这里通过 detect 的 unwrap_or 兜底行为间接覆盖（C: 兜底）。
        // 直接构造判定：
        fn extract(s: &str) -> Option<String> {
            let bytes = s.as_bytes();
            if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
                Some(s[..2].to_ascii_uppercase())
            } else {
                None
            }
        }
        assert_eq!(extract(r"C:\Users\someone").as_deref(), Some("C:"));
        assert_eq!(extract(r"d:\data").as_deref(), Some("D:"));
        assert_eq!(extract(r"\\server\share"), None, "UNC 路径无盘符");
        assert_eq!(extract("relative/path"), None);
    }

    // ---------- apply_profile（C1.4 / step#4）----------

    const GB: u64 = 1024 * 1024 * 1024;

    /// 档1（16GB）：512MB / 8 / 2 / soft = max(2GB, 16GB×20%) = 3.2GB。
    #[test]
    fn apply_profile_tier1_16gb() {
        let mut cfg = ResourceConfig::default();
        let profile = HostProfile {
            total_ram_bytes: 16 * GB,
            cpu_cores: 8,
            disk_type: DiskType::Ssd,
        };
        let covered = apply_profile(&mut cfg, &profile);
        assert_eq!(cfg.cache_max_bytes, TIER1_CACHE_MAX_BYTES);
        assert_eq!(cfg.cache_max_workspaces, TIER1_CACHE_MAX_WORKSPACES);
        assert_eq!(cfg.build_parse_threads, TIER1_BUILD_PARSE_THREADS);
        assert_eq!(cfg.memory_soft_limit_bytes, 16 * GB * 20 / 100);
        assert_eq!(cfg.idle_evict_secs, DEFAULT_IDLE_EVICT_SECS, "映射不触及 idle_evict_secs");
        // 固定顺序：供启动日志断言
        assert_eq!(
            covered,
            vec![
                "cache_max_bytes".to_string(),
                "cache_max_workspaces".to_string(),
                "build_parse_threads".to_string(),
                "memory_soft_limit_bytes".to_string(),
            ]
        );
    }

    /// 档2（32GB 与 24GB 整阈值）：翻倍 1GB / 16 / 4；soft 公式同档1。
    #[test]
    fn apply_profile_tier2_32gb() {
        for ram in [32 * GB, 24 * GB] {
            let mut cfg = ResourceConfig::default();
            let profile = HostProfile {
                total_ram_bytes: ram,
                cpu_cores: 16,
                disk_type: DiskType::Ssd,
            };
            apply_profile(&mut cfg, &profile);
            assert_eq!(cfg.cache_max_bytes, TIER1_CACHE_MAX_BYTES * TIER2_MULTIPLIER);
            assert_eq!(
                cfg.cache_max_workspaces,
                TIER1_CACHE_MAX_WORKSPACES * TIER2_MULTIPLIER as usize
            );
            assert_eq!(
                cfg.build_parse_threads,
                TIER1_BUILD_PARSE_THREADS * TIER2_MULTIPLIER as usize
            );
            assert_eq!(cfg.memory_soft_limit_bytes, ram * 20 / 100);
        }
    }

    /// HDD 封顶：档2 的 1GB 被压回 512MB；Ssd 不封顶（对照）。
    #[test]
    fn apply_profile_hdd_caps_cache() {
        let mut cfg = ResourceConfig::default();
        let profile = HostProfile {
            total_ram_bytes: 32 * GB,
            cpu_cores: 16,
            disk_type: DiskType::Hdd,
        };
        apply_profile(&mut cfg, &profile);
        assert_eq!(cfg.cache_max_bytes, HDD_CACHE_MAX_BYTES, "档2 1GB 被 HDD 封顶");
        assert_eq!(cfg.cache_max_workspaces, 16, "其余档2 值不受影响");
        assert_eq!(cfg.build_parse_threads, 4);

        let mut cfg_ssd = ResourceConfig::default();
        let profile_ssd = HostProfile {
            total_ram_bytes: 32 * GB,
            cpu_cores: 16,
            disk_type: DiskType::Ssd,
        };
        apply_profile(&mut cfg_ssd, &profile_ssd);
        assert_eq!(cfg_ssd.cache_max_bytes, TIER1_CACHE_MAX_BYTES * TIER2_MULTIPLIER);
    }

    /// 显式键跳过：explicit_keys 中的键保持原值且不进返回列表。
    #[test]
    fn apply_profile_explicit_keys_skip() {
        let mut cfg = ResourceConfig::default();
        cfg.cache_max_bytes = 7 * GB;
        cfg.build_parse_threads = 9;
        cfg.explicit_keys
            .insert("cache_max_bytes".to_string());
        cfg.explicit_keys
            .insert("build_parse_threads".to_string());
        let profile = HostProfile {
            total_ram_bytes: 32 * GB,
            cpu_cores: 16,
            disk_type: DiskType::Ssd,
        };
        let covered = apply_profile(&mut cfg, &profile);
        assert_eq!(cfg.cache_max_bytes, 7 * GB, "显式 cache_max_bytes 神圣不被覆盖");
        assert_eq!(cfg.build_parse_threads, 9, "显式 build_parse_threads 神圣不被覆盖");
        assert_eq!(cfg.cache_max_workspaces, 16, "非显式键正常映射");
        assert_eq!(
            covered,
            vec!["cache_max_workspaces".to_string(), "memory_soft_limit_bytes".to_string()],
            "返回列表只含实际被覆盖的键"
        );
    }

    /// 侦测失败（RAM=0）→ 档1 且 soft=2GB 下限。
    #[test]
    fn apply_profile_detect_failure_takes_tier1() {
        let mut cfg = ResourceConfig::default();
        let profile = HostProfile {
            total_ram_bytes: 0,
            cpu_cores: 1,
            disk_type: DiskType::Unknown,
        };
        let covered = apply_profile(&mut cfg, &profile);
        assert_eq!(cfg.cache_max_bytes, TIER1_CACHE_MAX_BYTES);
        assert_eq!(cfg.cache_max_workspaces, TIER1_CACHE_MAX_WORKSPACES);
        assert_eq!(cfg.build_parse_threads, TIER1_BUILD_PARSE_THREADS);
        assert_eq!(
            cfg.memory_soft_limit_bytes,
            MEMORY_SOFT_LIMIT_MIN_BYTES,
            "RAM=0 → 20% 项为 0，取 2GB 下限"
        );
        assert_eq!(covered.len(), 4);
    }

    /// AC-RG-01-2 跨步断言：from_toml_str 显式值 → apply_profile 映射后仍保持。
    #[test]
    fn ac_rg_01_2_explicit_overrides_mapping() {
        let toml_str = r#"
[resources]
cache_max_bytes = "1G"
build_parse_threads = 6
"#;
        let mut cfg = ResourceConfig::from_toml_str(toml_str);
        let profile = HostProfile {
            total_ram_bytes: 16 * GB,
            cpu_cores: 8,
            disk_type: DiskType::Ssd,
        };
        let covered = apply_profile(&mut cfg, &profile);
        assert_eq!(
            cfg.cache_max_bytes,
            1024 * 1024 * 1024,
            "显式 1G 不被档1 的 512MB 覆盖"
        );
        assert_eq!(cfg.build_parse_threads, 6, "显式 6 不被档1 的 2 覆盖");
        assert_eq!(cfg.cache_max_workspaces, TIER1_CACHE_MAX_WORKSPACES);
        assert_eq!(
            covered,
            vec!["cache_max_workspaces".to_string(), "memory_soft_limit_bytes".to_string()],
            "covered 只含非显式键"
        );
        assert!(cfg.explicit_keys.contains("cache_max_bytes"));
        assert!(cfg.explicit_keys.contains("build_parse_threads"));
    }
}
