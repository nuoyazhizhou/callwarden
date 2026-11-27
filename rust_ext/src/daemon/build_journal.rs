//! 建图 jsonl 中间日志（两阶段流水线 REQ-01）。
//!
//! 全量建图（`run_build_graph`）两阶段改造的中间产物层：
//! 阶段一 worker 并行解析、零 DB 写，把每个文件的解析事实顺序 append 到
//! 本 worker 独立的 jsonl 分片（`.callwarden/build-tmp/build-<runid>-w<idx>.jsonl`）；
//! 阶段二单写者按批合并事务导入，成功后删除分片。
//!
//! 分片是**临时中间产物**，不是持久审计日志：导入成功即删；崩溃残留由
//! [`cleanup_expired_shards`] 按 run_id（启动时间戳）过期（默认 24h）清理。
//!
//! 事件 schema（每条事件都带 `rel`/`hash`，供阶段二按文件分组与一致性校验）：
//! - `file`：文件元事件，每个变更文件最先写一条（含解析失败/空符号场景）；
//! - `sym`：符号事件，字段与 `symbols` / `symbol_contents` 落库列一一对应；
//! - `call`：原始调用边事件，字段与 `cas_raw_calls` 落库列一一对应。
//!
//! 崩溃语义：分片按行落盘（每文件事件组 flush 一次），尾部无换行的半行
//! 视为截断丢弃（[`ShardReader::truncated`]);中间行损坏 fail-closed 报错，
//! 不静默跳过。

use serde::{Deserialize, Serialize};
use std::fs::{self, File};
use std::io::{self, BufRead, BufReader, BufWriter, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// 分片默认过期时长：run_id 距今超过该值的残留分片在下次建图启动时清理。
pub const DEFAULT_SHARD_MAX_AGE: Duration = Duration::from_secs(24 * 60 * 60);

/// 分片目录：`<workspace 根>/.callwarden/build-tmp`。
///
/// `.callwarden` 是项目既有约定目录（ignore 契约所在地）；`build-tmp` 下
/// 只放 jsonl 分片。`scan_files` 剪枝隐藏目录且扩展名白名单不含 jsonl，
/// 不会把分片当源文件扫进索引。
pub fn shard_dir(workspace_root: &Path) -> PathBuf {
    workspace_root.join(".callwarden").join("build-tmp")
}

/// 分片文件名：`build-<runid>-w<idx>.jsonl`（runid=建图启动的 Unix 秒）。
pub fn shard_file_name(run_id: u64, worker_idx: usize) -> String {
    format!("build-{run_id}-w{worker_idx:03}.jsonl")
}

/// 反解分片文件名 → (run_id, worker_idx)；不匹配返回 None。
pub fn parse_shard_file_name(file_name: &str) -> Option<(u64, usize)> {
    let rest = file_name.strip_prefix("build-")?.strip_suffix(".jsonl")?;
    let (run_str, idx_str) = rest.rsplit_once("-w")?;
    if run_str.is_empty() || idx_str.is_empty() {
        return None;
    }
    if !run_str.bytes().all(|b| b.is_ascii_digit()) || !idx_str.bytes().all(|b| b.is_ascii_digit())
    {
        return None;
    }
    match (run_str.parse::<u64>(), idx_str.parse::<usize>()) {
        (Ok(run_id), Ok(idx)) => Some((run_id, idx)),
        _ => None,
    }
}

/// 建图中间事件（jsonl 每行一条，`t` 字段区分类型）。
///
/// 字段即阶段二 apply 落库所需的最小集（从 `parse_and_store_symbols`
/// 的 INSERT 列反推）：不在落库链路上的 ParseFact 诊断字段
/// （byte_start/ordinal/lexical_parent_local_id 等）不进事件，保持分片紧凑。
#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
#[serde(tag = "t", rename_all = "lowercase")]
pub enum BuildEvent {
    /// 文件元事件：每个变更文件最先写一条。
    File {
        /// 相对 workspace 根的路径（`/` 分隔，与 file_instances.rel_path 一致）
        rel: String,
        /// 文件内容 sha256（十六进制小写）
        hash: String,
        /// 文件 mtime（Unix 秒，浮点）
        mtime: f64,
        /// 文件总行数
        total_lines: i64,
        /// 文件级 module_path（raw call 的 caller_module 落库值）
        module_path: String,
        /// 是否可解析语言（false = 仅登记文件，无符号/调用事件）
        parsed: bool,
        /// 库内既有内容 hash（None = 新文件）。旧 run_build_graph 主循环的
        /// per-file stored 查询在阶段 0 预取后由 worker 写入事件，阶段二据此
        /// classify_build_graph_file 判定 Inserted/Unchanged（不再回查 DB）。
        #[serde(default, skip_serializing_if = "Option::is_none")]
        prev_hash: Option<String>,
        /// 库内既有 status（'parsed'/'pending'/'archived'...），与 prev_hash 配对
        #[serde(default, skip_serializing_if = "Option::is_none")]
        prev_status: Option<String>,
        /// 解析失败信息（Some 时阶段二做 status='pending' 补偿并整体失败）。
        /// 读文件失败不产生事件（worker 直接跳过，保持旧行为）。
        #[serde(default, skip_serializing_if = "Option::is_none")]
        error: Option<String>,
    },
    /// 符号事件（字段对应 `parse_and_store_symbols` 的 symbols/symbol_contents 写入）。
    Sym {
        rel: String,
        hash: String,
        /// 符号内容哈希（symbol_contents.content_hash / symbols.symbol_hash）
        symbol_hash: String,
        name: String,
        kind: String,
        /// 符号源码内容（symbol_contents.content）
        content: String,
        /// 声明头签名（symbol_contents.signature）
        signature: String,
        /// 是否有注释（symbol_contents.has_comment）
        has_comment: bool,
        /// 限定名（symbols.qualified_name）
        qualified_name: String,
        /// 可见性（symbols.visibility）
        visibility: String,
        /// 起始行（1-based）
        start_line: u32,
        /// 结束行（1-based）
        end_line: u32,
        /// 符号级 module_path（空时阶段二回退文件级）
        module_path: String,
        /// 文件内稳定符号 ID（1-based；阶段二据此建 local_id_map）
        local_id: u32,
        /// 调用深度（file_symbol_versions.depth）
        depth: i32,
    },
    /// 原始调用边事件（字段对应 cas_raw_calls 写入；callee_id 阶段二恒置 0，
    /// 由既有 resolve_raw_calls 重解析，与 CR12 降级语义一致）。
    Call {
        rel: String,
        hash: String,
        /// 调用者符号 local_id（None = 顶层裸调用）
        caller_local_id: Option<u32>,
        caller_name: String,
        callee_name: String,
        callee_module: String,
        /// 调用行号（1-based）
        call_line: u32,
        is_cross_file: bool,
    },
}

/// 单 worker 分片顺序写器（BufWriter + 每行一条 JSON）。
pub struct ShardWriter {
    writer: BufWriter<File>,
    path: PathBuf,
    event_count: usize,
}

impl ShardWriter {
    /// 创建分片（父目录不存在则递归创建）。同 run 内 worker_idx 不重复。
    pub fn create(dir: &Path, run_id: u64, worker_idx: usize) -> io::Result<Self> {
        fs::create_dir_all(dir)?;
        let path = dir.join(shard_file_name(run_id, worker_idx));
        let file = File::create(&path)?;
        Ok(Self {
            writer: BufWriter::new(file),
            path,
            event_count: 0,
        })
    }

    /// 分片路径。
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// 已写入事件数（统计用）。
    pub fn event_count(&self) -> usize {
        self.event_count
    }

    /// 追加一条事件（序列化 + 换行；不立即 flush，由调用方按文件组 flush）。
    pub fn append(&mut self, event: &BuildEvent) -> io::Result<()> {
        let mut line = serde_json::to_vec(event)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        line.push(b'\n');
        self.writer.write_all(&line)?;
        self.event_count += 1;
        Ok(())
    }

    /// 刷盘（每个文件的事件组写完后调用一次，确立崩溃边界）。
    pub fn flush(&mut self) -> io::Result<()> {
        self.writer.flush()
    }
}

/// 分片流式读取器：逐行反序列化。
///
/// - 文件尾无换行符的半行 = 崩溃截断，丢弃并置 [`ShardReader::truncated`]；
/// - 中间行损坏（非法 JSON / 未知事件类型）= Err（fail-closed）。
pub struct ShardReader {
    reader: BufReader<File>,
    path: PathBuf,
    /// 尾部是否发现截断半行（该行已丢弃）。
    pub truncated: bool,
}

impl ShardReader {
    /// 打开分片（不存在时由调用方先 [`shard_exists`] 判定）。
    pub fn open(path: &Path) -> io::Result<Self> {
        let file = File::open(path)?;
        Ok(Self {
            reader: BufReader::new(file),
            path: path.to_path_buf(),
            truncated: false,
        })
    }

    /// 分片路径。
    pub fn path(&self) -> &Path {
        &self.path
    }

    /// 下一条完整事件。返回 None 表示 EOF（或截断尾已丢弃）。
    pub fn next_event(&mut self) -> io::Result<Option<BuildEvent>> {
        let mut line = String::new();
        let n = self.reader.read_line(&mut line)?;
        if n == 0 {
            return Ok(None);
        }
        // 崩溃截断：最后一行没有换行符 → 丢弃
        if !line.ends_with('\n') {
            self.truncated = true;
            return Ok(None);
        }
        let event = serde_json::from_str::<BuildEvent>(&line)
            .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))?;
        Ok(Some(event))
    }
}

/// 分片是否存在（读侧入口判定）。
pub fn shard_exists(path: &Path) -> bool {
    path.is_file()
}

/// 列出目录下的建图分片（按文件名排序，保证阶段二导入顺序确定）。
/// 目录不存在返回空。
pub fn list_shards(dir: &Path) -> io::Result<Vec<PathBuf>> {
    let entries = match fs::read_dir(dir) {
        Ok(entries) => entries,
        Err(e) if e.kind() == io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(e) => return Err(e),
    };
    let mut shards: Vec<PathBuf> = Vec::new();
    for entry in entries {
        let entry = entry?;
        let file_name = entry.file_name().to_string_lossy().to_string();
        if parse_shard_file_name(&file_name).is_some() {
            shards.push(entry.path());
        }
    }
    shards.sort();
    Ok(shards)
}

/// 删除单个分片（不存在视为已删，幂等）。
pub fn remove_shard(path: &Path) -> io::Result<()> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(e) if e.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(e) => Err(e),
    }
}

/// 清理过期残留分片：run_id 距 `now_unix_secs` 超过 `max_age` 的分片删除。
///
/// 在建图启动时调用（阶段一之前），回收上次崩溃留下的分片；clock skew
/// （run_id 在未来）保守保留。返回删除数。
pub fn cleanup_expired_shards(
    dir: &Path,
    now_unix_secs: u64,
    max_age: Duration,
) -> io::Result<usize> {
    let mut removed = 0;
    for shard in list_shards(dir)? {
        let file_name = shard.file_name().unwrap_or_default().to_string_lossy();
        let Some((run_id, _)) = parse_shard_file_name(&file_name) else {
            continue;
        };
        let age = now_unix_secs.saturating_sub(run_id);
        if age > max_age.as_secs() {
            remove_shard(&shard)?;
            removed += 1;
        }
    }
    Ok(removed)
}

// ============================== 配置面（REQ-05） ==============================

/// 两阶段流水线可调参数（daemon.toml 四项，缺省内置）。
///
/// 读 `~/.callwarden/daemon.toml`（Windows 用 `USERPROFILE`，其余用 `HOME`）；
/// 支持顶层键与 `[build]` 表两种写法。文件缺失/损坏/单键非法 → 该项回退默认，
/// 不影响建图主流程（配置是调优项，不是正确性依赖）。
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BuildPipelineConfig {
    /// 阶段一解析 worker 数上限
    pub parse_threads: usize,
    /// 阶段一有界 channel 深度（背压队列）
    pub channel_depth: usize,
    /// 阶段二批导入：批文件数阈值
    pub import_batch_files: usize,
    /// 阶段二批导入：批事件数阈值
    pub import_batch_events: usize,
}

impl Default for BuildPipelineConfig {
    fn default() -> Self {
        Self {
            parse_threads: 4,
            channel_depth: 256,
            import_batch_files: 200,
            import_batch_events: 5000,
        }
    }
}

impl BuildPipelineConfig {
    /// daemon.toml 相对主目录的路径（`~/.callwarden/daemon.toml`）。
    pub fn config_path() -> Option<PathBuf> {
        let home = std::env::var_os("USERPROFILE")
            .or_else(|| std::env::var_os("HOME"))?;
        if home.is_empty() {
            return None;
        }
        Some(PathBuf::from(home).join(".callwarden").join("daemon.toml"))
    }

    /// 读取配置：缺文件/损坏/非法值一律回退默认，不报错。
    pub fn load() -> Self {
        let Some(path) = Self::config_path() else {
            return Self::default();
        };
        let Ok(content) = fs::read_to_string(&path) else {
            return Self::default();
        };
        Self::from_toml_str(&content)
    }

    /// 从 toml 文本解析（纯函数，便于测试）；顶层键优先，其次 `[build]` 表。
    /// 解析失败/非法值回退默认，不报错。
    pub fn from_toml_str(content: &str) -> Self {
        let mut cfg = Self::default();
        let Ok(doc) = toml::from_str::<toml::Value>(content) else {
            return cfg;
        };
        let lookup = |key: &str| -> Option<i64> {
            if let Some(v) = doc.get(key).and_then(|v| v.as_integer()) {
                return Some(v);
            }
            doc.get("build")
                .and_then(|t| t.get(key))
                .and_then(|v| v.as_integer())
        };
        if let Some(v) = lookup("build_parse_threads") {
            if v > 0 {
                cfg.parse_threads = v as usize;
            }
        }
        if let Some(v) = lookup("build_channel_depth") {
            if v > 0 {
                cfg.channel_depth = v as usize;
            }
        }
        if let Some(v) = lookup("build_import_batch_files") {
            if v > 0 {
                cfg.import_batch_files = v as usize;
            }
        }
        if let Some(v) = lookup("build_import_batch_events") {
            if v > 0 {
                cfg.import_batch_events = v as usize;
            }
        }
        cfg
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample_events() -> Vec<BuildEvent> {
        vec![
            BuildEvent::File {
                rel: "src/main.rs".to_string(),
                hash: "abc123".to_string(),
                mtime: 1700000000.5,
                total_lines: 42,
                module_path: "main".to_string(),
                parsed: true,
                prev_hash: None,
                prev_status: None,
                error: None,
            },
            BuildEvent::Sym {
                rel: "src/main.rs".to_string(),
                hash: "abc123".to_string(),
                symbol_hash: "h1".to_string(),
                name: "main".to_string(),
                kind: "function".to_string(),
                content: "fn main() {}".to_string(),
                signature: "fn main()".to_string(),
                has_comment: false,
                qualified_name: "main".to_string(),
                visibility: "public".to_string(),
                start_line: 1,
                end_line: 3,
                module_path: "main".to_string(),
                local_id: 1,
                depth: -1,
            },
            BuildEvent::Call {
                rel: "src/main.rs".to_string(),
                hash: "abc123".to_string(),
                caller_local_id: Some(1),
                caller_name: "main".to_string(),
                callee_name: "println".to_string(),
                callee_module: String::new(),
                call_line: 2,
                is_cross_file: false,
            },
        ]
    }

    #[test]
    fn roundtrip_events() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        let events = sample_events();
        {
            let mut w = ShardWriter::create(dir, 1700000000, 0).unwrap();
            for e in &events {
                w.append(e).unwrap();
            }
            w.flush().unwrap();
            assert_eq!(w.event_count(), 3);
        }
        let shards = list_shards(dir).unwrap();
        assert_eq!(shards.len(), 1);
        let mut r = ShardReader::open(&shards[0]).unwrap();
        let mut back = Vec::new();
        while let Some(e) = r.next_event().unwrap() {
            back.push(e);
        }
        assert_eq!(back, events);
        assert!(!r.truncated);
    }

    #[test]
    fn truncated_tail_is_dropped() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("build-1700000000-w000.jsonl");
        let events = sample_events();
        let full = serde_json::to_string(&events[0]).unwrap();
        let partial = serde_json::to_string(&events[1]).unwrap();
        // 第二行只写一半且无换行符（崩溃截断）
        let mut bytes = full.into_bytes();
        bytes.push(b'\n');
        let p = partial.into_bytes();
        bytes.extend_from_slice(&p[..p.len() / 2]);
        fs::write(&path, &bytes).unwrap();

        let mut r = ShardReader::open(&path).unwrap();
        let first = r.next_event().unwrap();
        assert_eq!(first, Some(events[0].clone()));
        assert_eq!(r.next_event().unwrap(), None);
        assert!(r.truncated);
    }

    #[test]
    fn corrupted_middle_line_fails() {
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("build-1700000000-w000.jsonl");
        let events = sample_events();
        let line0 = serde_json::to_string(&events[0]).unwrap();
        fs::write(&path, format!("{line0}\n{{not json}}\n")).unwrap();

        let mut r = ShardReader::open(&path).unwrap();
        assert_eq!(r.next_event().unwrap(), Some(events[0].clone()));
        assert!(r.next_event().is_err(), "中间损坏行必须 fail-closed");
    }

    #[test]
    fn cleanup_removes_only_expired() {
        let tmp = tempfile::tempdir().unwrap();
        let dir = tmp.path();
        let old = 1700000000u64;
        let fresh = old + 23 * 3600;
        for (run_id, idx) in [(old, 0), (old, 1), (fresh, 0)] {
            let mut w = ShardWriter::create(dir, run_id, idx).unwrap();
            w.append(&sample_events()[0]).unwrap();
            w.flush().unwrap();
        }
        let removed =
            cleanup_expired_shards(dir, old + 25 * 3600, DEFAULT_SHARD_MAX_AGE).unwrap();
        assert_eq!(removed, 2, "只清理超过 24h 的旧 run 分片");
        let left = list_shards(dir).unwrap();
        assert_eq!(left.len(), 1);
        assert_eq!(
            parse_shard_file_name(left[0].file_name().unwrap().to_str().unwrap()),
            Some((fresh, 0))
        );
    }

    #[test]
    fn filename_parse_roundtrip() {
        assert_eq!(
            parse_shard_file_name("build-1700000000-w007.jsonl"),
            Some((1700000000, 7))
        );
        // 非零填充的 idx 同样可解析（宽容读取，便于手工排查）
        assert_eq!(
            parse_shard_file_name("build-1700000000-w0.jsonl"),
            Some((1700000000, 0))
        );
        assert_eq!(parse_shard_file_name("other-1700000000-w0.jsonl"), None);
        assert_eq!(parse_shard_file_name("build-abc-w0.jsonl"), None);
        assert_eq!(parse_shard_file_name("build-1700000000-wx.jsonl"), None);
        assert_eq!(shard_file_name(1700000000, 7), "build-1700000000-w007.jsonl");
    }

    #[test]
    fn pipeline_config_defaults() {
        assert_eq!(
            BuildPipelineConfig::default(),
            BuildPipelineConfig {
                parse_threads: 4,
                channel_depth: 256,
                import_batch_files: 200,
                import_batch_events: 5000,
            }
        );
    }

    #[test]
    fn pipeline_config_from_toml() {
        let cfg = BuildPipelineConfig::from_toml_str(
            "build_parse_threads = 8\nbuild_channel_depth = 512\n\
             build_import_batch_files = 100\nbuild_import_batch_events = 9999\n",
        );
        assert_eq!(cfg.parse_threads, 8);
        assert_eq!(cfg.channel_depth, 512);
        assert_eq!(cfg.import_batch_files, 100);
        assert_eq!(cfg.import_batch_events, 9999);
    }

    #[test]
    fn pipeline_config_build_table_and_partial() {
        // [build] 表写法 + 只覆盖部分键 → 其余保持默认
        let cfg = BuildPipelineConfig::from_toml_str(
            "[build]\nbuild_parse_threads = 2\nbuild_import_batch_events = 42\n",
        );
        assert_eq!(cfg.parse_threads, 2);
        assert_eq!(cfg.import_batch_events, 42);
        assert_eq!(cfg.channel_depth, 256);
        assert_eq!(cfg.import_batch_files, 200);
    }

    #[test]
    fn pipeline_config_invalid_falls_back() {
        // 损坏 toml / 非正数 / 类型错误 → 回退默认，不 panic
        assert_eq!(
            BuildPipelineConfig::from_toml_str("not valid toml {{{"),
            BuildPipelineConfig::default()
        );
        let cfg = BuildPipelineConfig::from_toml_str(
            "build_parse_threads = -1\nbuild_channel_depth = \"x\"\nbuild_import_batch_files = 7\n",
        );
        assert_eq!(cfg.parse_threads, 4, "非正数回退默认");
        assert_eq!(cfg.channel_depth, 256, "类型错误回退默认");
        assert_eq!(cfg.import_batch_files, 7, "合法项照常生效");
    }
}
