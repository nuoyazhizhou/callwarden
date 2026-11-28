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
}
