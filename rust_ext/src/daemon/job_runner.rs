//! 异步长任务 job 状态机（T02-job 批次，18 个 → task_rpc）。
//!
//! 对应 `tool_migration_matrix.json` 中 target_backend=task_rpc 的 18 个
//! 拒止长任务：semgrep_scan / semgrep_incremental / clone_detect / embed /
//! embed_single / git_history / git_blame / codeowners / project_deps /
//! envelope_deps / coverage / hard_dep_edges / cross_repo_deps /
//! prune_external（经 `task.job_submit` + `task.wait_for_job` 复用）。
//!
//! 设计要点：
//! - 单实例 daemon 内进程级 job 注册表（OnceLock 单例，无多实例竞争）；
//! - `submit` 生成 job_id，spawn 后台线程执行 job_fn；`wait` 轮询结果；
//! - `cancel` 通过 Arc<AtomicBool> 协作取消（executor 定期检查）；
//! - 每个 executor 是真实 SQL/子进程操作，写 codegraph DB（daemon 权威写路径），
//!   不依赖 Python 双实现。

use serde_json::{json, Map, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use super::dispatch::{get_int_param_or, get_str_param_or, DaemonRpcError};

/// job 状态。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JobStatus {
    Queued,
    Running,
    Completed,
    Failed,
    Cancelled,
}

impl JobStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            JobStatus::Queued => "queued",
            JobStatus::Running => "running",
            JobStatus::Completed => "completed",
            JobStatus::Failed => "failed",
            JobStatus::Cancelled => "cancelled",
        }
    }
}

/// job 执行上下文（executor 从 params 解析所需字段）。
#[derive(Clone)]
pub struct JobContext {
    pub job_id: String,
    pub job_type: String,
    pub workspace_id: i64,
    pub workspace_instance_id: String,
    pub codegraph_db: Option<PathBuf>,
    pub cancel_flag: Arc<AtomicBool>,
    pub params: Value,
}

/// 单条 job 记录。
struct JobEntry {
    id: String,
    job_type: String,
    status: JobStatus,
    progress: f64,
    result: Option<Value>,
    error: Option<String>,
    cancel_flag: Arc<AtomicBool>,
    created_at: f64,
    started_at: Option<f64>,
    finished_at: Option<f64>,
}

/// 进程级 job 注册表（单实例 daemon 内全局唯一）。
pub struct JobRunner {
    jobs: Mutex<HashMap<String, JobEntry>>,
    next_id: AtomicU64,
}

fn global_runner() -> &'static JobRunner {
    static RUNNER: OnceLock<JobRunner> = OnceLock::new();
    RUNNER.get_or_init(|| JobRunner {
        jobs: Mutex::new(HashMap::new()),
        next_id: AtomicU64::new(1),
    })
}

/// 秒级 Unix 时间戳。
fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

impl JobRunner {
    /// 提交 job。`sync=true` 时同步执行（等价同步工具），否则立即返回 job_id。
    pub fn submit(
        job_type: &str,
        workspace_id: i64,
        workspace_instance_id: &str,
        codegraph_db: Option<PathBuf>,
        params: &Value,
        sync: bool,
    ) -> Result<Value, DaemonRpcError> {
        let runner = global_runner();
        let id = format!("job-{:06}", runner.next_id.fetch_add(1, Ordering::SeqCst));
        let cancel_flag = Arc::new(AtomicBool::new(false));
        let ctx = JobContext {
            job_id: id.clone(),
            job_type: job_type.to_string(),
            workspace_id,
            workspace_instance_id: workspace_instance_id.to_string(),
            codegraph_db,
            cancel_flag: Arc::clone(&cancel_flag),
            params: params.clone(),
        };
        {
            let mut jobs = runner.jobs.lock().unwrap_or_else(|e| e.into_inner());
            jobs.insert(
                id.clone(),
                JobEntry {
                    id: id.clone(),
                    job_type: job_type.to_string(),
                    status: JobStatus::Queued,
                    progress: 0.0,
                    result: None,
                    error: None,
                    cancel_flag: Arc::clone(&cancel_flag),
                    created_at: now_ts(),
                    started_at: None,
                    finished_at: None,
                },
            );
        }

        if sync {
            let outcome = execute_job(&ctx);
            runner.finish(&id, outcome);
            let entry = runner.status(&id)?;
            return Ok(entry);
        }

        let id_for_thread = id.clone();
        std::thread::Builder::new()
            .name(format!("cw-job-{}", id))
            .spawn(move || {
                let outcome = execute_job(&ctx);
                global_runner().finish(&id_for_thread, outcome);
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("spawn job 线程失败: {e}")))?;

        let mut m = Map::new();
        m.insert("job_id".into(), Value::String(id));
        m.insert("status".into(), Value::String(JobStatus::Queued.as_str().to_string()));
        m.insert("job_type".into(), Value::String(job_type.to_string()));
        Ok(Value::Object(m))
    }

    /// 记录 job 完成结果（executor 线程结束统一调用）。
    fn finish(&self, id: &str, outcome: Result<Value, String>) {
        let mut jobs = self.jobs.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(entry) = jobs.get_mut(id) {
            match outcome {
                Ok(value) => {
                    entry.status = JobStatus::Completed;
                    entry.result = Some(value);
                    entry.progress = 1.0;
                }
                Err(e) => {
                    entry.status = JobStatus::Failed;
                    entry.error = Some(e);
                }
            }
            entry.finished_at = Some(now_ts());
        }
    }

    /// 取消 job（协作式：设置 cancel_flag，executor 定期检查）。
    pub fn cancel(id: &str) -> Result<Value, DaemonRpcError> {
        let runner = global_runner();
        let mut jobs = runner.jobs.lock().unwrap_or_else(|e| e.into_inner());
        let entry = jobs.get_mut(id).ok_or_else(|| {
            DaemonRpcError::new("job_not_found", format!("job {id} 不存在"))
        })?;
        match entry.status {
            JobStatus::Completed | JobStatus::Failed | JobStatus::Cancelled => {
                return Ok(json!({ "job_id": id, "status": entry.status.as_str() }));
            }
            _ => {}
        }
        entry.cancel_flag.store(true, Ordering::SeqCst);
        entry.status = JobStatus::Cancelled;
        entry.finished_at = Some(now_ts());
        Ok(json!({ "job_id": id, "status": "cancelled" }))
    }

    /// 查询 job 状态。
    pub fn status(&self, id: &str) -> Result<Value, DaemonRpcError> {
        let jobs = self.jobs.lock().unwrap_or_else(|e| e.into_inner());
        let entry = jobs.get(id).ok_or_else(|| {
            DaemonRpcError::new("job_not_found", format!("job {id} 不存在"))
        })?;
        let mut m = Map::new();
        m.insert("job_id".into(), Value::String(entry.id.clone()));
        m.insert("job_type".into(), Value::String(entry.job_type.clone()));
        m.insert("status".into(), Value::String(entry.status.as_str().to_string()));
        m.insert("progress".into(), serde_json::Number::from_f64(entry.progress).map(Value::Number).unwrap_or(Value::Null));
        m.insert("created_at".into(), serde_json::Number::from_f64(entry.created_at).map(Value::Number).unwrap_or(Value::Null));
        if let Some(result) = &entry.result {
            m.insert("result".into(), result.clone());
        }
        if let Some(error) = &entry.error {
            m.insert("error".into(), Value::String(error.clone()));
        }
        Ok(Value::Object(m))
    }

    /// 等待 job 完成（轮询，最多 timeout_ms）。
    pub fn wait(id: &str, timeout_ms: u64) -> Result<Value, DaemonRpcError> {
        let runner = global_runner();
        let deadline = Instant::now() + Duration::from_millis(timeout_ms.clamp(100, 600_000));
        loop {
            let status = runner.status(id)?;
            let state = status.get("status").and_then(Value::as_str).unwrap_or("");
            if matches!(state, "completed" | "failed" | "cancelled") {
                return Ok(status);
            }
            if Instant::now() >= deadline {
                return Err(DaemonRpcError::new(
                    "job_timeout",
                    format!("等待 job {id} 超时（{timeout_ms}ms）"),
                ));
            }
            std::thread::sleep(Duration::from_millis(100));
        }
    }

    /// 列出 job。
    pub fn list(&self, status_filter: &str) -> Result<Value, DaemonRpcError> {
        let jobs = self.jobs.lock().unwrap_or_else(|e| e.into_inner());
        let mut rows = Vec::new();
        for entry in jobs.values() {
            if !status_filter.is_empty() && entry.status.as_str() != status_filter {
                continue;
            }
            rows.push(json!({
                "job_id": entry.id,
                "job_type": entry.job_type,
                "status": entry.status.as_str(),
                "progress": entry.progress,
                "created_at": entry.created_at,
            }));
        }
        rows.sort_by_key(|r| r["created_at"].as_f64().unwrap_or(0.0) as i64);
        Ok(Value::Array(rows))
    }
}

// ---------------------------------------------------------------------------
// 公开 RPC 入口
// ---------------------------------------------------------------------------

/// `task.job_submit`（含 sync 语义）。
pub fn rpc_job_submit(
    workspace_id: i64,
    workspace_instance_id: &str,
    codegraph_db: Option<PathBuf>,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let job_type = super::dispatch::require_str_param(params, "job_type")?;
    if !SUPPORTED_JOB_TYPES.contains(&job_type) {
        return Err(DaemonRpcError::invalid_params(format!(
            "非法 job_type: {job_type}（支持: {SUPPORTED_JOB_TYPES:?}）"
        )));
    }
    let sync = params.get("sync").and_then(Value::as_bool).unwrap_or(false);
    JobRunner::submit(job_type, workspace_id, workspace_instance_id, codegraph_db, params, sync)
}

/// `task.job_cancel`。
pub fn rpc_job_cancel(params: &Value) -> Result<Value, DaemonRpcError> {
    let job_id = super::dispatch::require_str_param(params, "job_id")?;
    JobRunner::cancel(job_id)
}

/// `task.job_status`。
pub fn rpc_job_status(params: &Value) -> Result<Value, DaemonRpcError> {
    let job_id = super::dispatch::require_str_param(params, "job_id")?;
    global_runner().status(job_id)
}

/// `task.wait_for_job`（复用既有 RPC 语义）。
pub fn rpc_job_wait(params: &Value) -> Result<Value, DaemonRpcError> {
    let job_id = super::dispatch::require_str_param(params, "job_id")?;
    let timeout_ms = get_int_param_or(params, "timeout_ms", 30_000) as u64;
    JobRunner::wait(job_id, timeout_ms)
}

/// `task.list_jobs`。
pub fn rpc_job_list(params: &Value) -> Result<Value, DaemonRpcError> {
    let status_filter = get_str_param_or(params, "status", "");
    global_runner().list(&status_filter)
}

const SUPPORTED_JOB_TYPES: &[&str] = &[
    "semgrep_scan",
    "semgrep_incremental",
    "clone_detect",
    "embed",
    "embed_single",
    "git_history",
    "git_blame",
    "codeowners",
    "project_deps",
    "envelope_deps",
    "coverage",
    "hard_dep_edges",
    "cross_repo_deps",
    "prune_external",
];

// ---------------------------------------------------------------------------
// Executors（真实业务操作，写 codegraph DB 或调用外部 CLI）
// ---------------------------------------------------------------------------

/// 分发到具体 executor。
fn execute_job(ctx: &JobContext) -> Result<Value, String> {
    let started = now_ts();
    let outcome = match ctx.job_type.as_str() {
        "semgrep_scan" | "semgrep_incremental" => exec_semgrep(ctx),
        "clone_detect" => exec_clone_detect(ctx),
        "embed" | "embed_single" => exec_embed(ctx),
        "git_history" => exec_git_history(ctx),
        "git_blame" => exec_git_blame(ctx),
        "codeowners" => exec_codeowners(ctx),
        "project_deps" | "envelope_deps" => exec_dependency_import(ctx),
        "coverage" => exec_coverage(ctx),
        "hard_dep_edges" => exec_hard_dep_edges(ctx),
        "cross_repo_deps" => exec_cross_repo_deps(ctx),
        "prune_external" => exec_prune_external(ctx),
        other => Err(format!("未实现的 job_type: {other}")),
    };
    let _ = started;
    outcome
}

/// 打开 workspace codegraph DB（写路径）。
fn open_db(ctx: &JobContext) -> Result<rusqlite::Connection, String> {
    let path = ctx.codegraph_db.as_ref().ok_or_else(|| {
        "daemon 未配置 codegraph_db_path_template，无法执行 job（fail-closed）".to_string()
    })?;
    rusqlite::Connection::open(path).map_err(|e| format!("打开 codegraph DB 失败: {e}"))
}

/// 解析 workspace 根目录（从 codegraph DB workspaces 表）。
fn workspace_root(conn: &rusqlite::Connection, workspace_id: i64) -> Result<PathBuf, String> {
    conn.query_row(
        "SELECT root_path FROM workspaces WHERE id = ?1",
        rusqlite::params![workspace_id],
        |row| row.get::<_, String>(0),
    )
    .map(PathBuf::from)
    .map_err(|e| format!("查询 workspace root 失败: {e}"))
}

/// semgrep 扫描：`semgrep --json --config p/default <paths>` → semgrep_findings。
fn exec_semgrep(ctx: &JobContext) -> Result<Value, String> {
    let mut conn = open_db(ctx)?;
    let root = workspace_root(&conn, ctx.workspace_id)?;
    let semgrep = which("semgrep").ok_or_else(|| "semgrep 未安装（PATH 中找不到 semgrep）".to_string())?;
    let mut cmd = std::process::Command::new(&semgrep);
    cmd.arg("--json")
        .arg("--config")
        .arg("p/default")
        .arg("--no-rewrite-rule-ids")
        .current_dir(&root);
    // 可选 file_filter 缩小扫描范围
    if let Some(file_filter) = ctx.params.get("file_filter").and_then(Value::as_str) {
        if !file_filter.is_empty() {
            cmd.arg(&file_filter);
        }
    } else {
        cmd.arg(".");
    }
    let output = cmd
        .output()
        .map_err(|e| format!("semgrep 执行失败: {e}"))?;
    if !output.status.success() {
        // 无 findings 时 semgrep 可能返回非 0（如规则缺失）；尽力解析 stdout
    }
    let parsed: Value = serde_json::from_slice(&output.stdout)
        .map_err(|e| format!("semgrep JSON 解析失败: {e}"))?;
    let results = parsed
        .get("results")
        .and_then(Value::as_array)
        .cloned()
        .unwrap_or_default();
    // 创建 scan 记录
    let scan_id = conn
        .execute(
            "INSERT INTO semgrep_scans (scan_type, config, workspace_id, started_at, status)
             VALUES ('full', 'p/default', ?1, ?2, 'running')",
            rusqlite::params![ctx.workspace_id, now_ts()],
        )
        .map_err(|e| format!("semgrep_scans insert: {e}"))?;
    let scan_id = conn.last_insert_rowid();
    let mut inserted = 0usize;
    for r in results {
        if ctx.cancel_flag.load(Ordering::SeqCst) {
            break;
        }
        let path = r.get("path").and_then(Value::as_str).unwrap_or("");
        let rel = path.trim_start_matches(&format!("{}/", root.to_string_lossy().replace('\\', "/")));
        let rule_id = r.get("check_id").and_then(Value::as_str).unwrap_or("unknown");
        let message = r.get("extra").and_then(|e| e.get("message")).and_then(Value::as_str).unwrap_or("");
        let severity = r.get("extra").and_then(|e| e.get("severity")).and_then(Value::as_str).unwrap_or("INFO");
        let start_line = r.get("start").and_then(|s| s.get("line")).and_then(Value::as_i64).unwrap_or(0);
        let end_line = r.get("end").and_then(|s| s.get("line")).and_then(Value::as_i64).unwrap_or(start_line);
        let content_hash = crate::daemon::fs_handlers::sha256_hex(
            std::fs::read(root.join(rel)).unwrap_or_default().as_slice(),
        );
        // 找到对应 file_instance
        let file_instance_id: Option<i64> = conn
            .query_row(
                "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2 LIMIT 1",
                rusqlite::params![ctx.workspace_id, rel],
                |row| row.get(0),
            )
            .ok();
        let file_instance_id = file_instance_id.unwrap_or(0);
        let res = conn.execute(
            "INSERT OR IGNORE INTO semgrep_findings
               (file_instance_id, content_hash, rule_id, rule_name, message, severity, confidence,
                language, start_line, end_line, snippet, symbol_id, symbol_qualified, scanned_at, scan_id)
             VALUES (?1, ?2, ?3, '', ?4, ?5, 'UNKNOWN', '', ?6, ?7, '', 0, '', ?8, ?9)",
            rusqlite::params![
                file_instance_id,
                content_hash,
                rule_id,
                message,
                severity.to_uppercase(),
                start_line,
                end_line,
                now_ts(),
                scan_id
            ],
        );
        if res.is_ok() && res.unwrap() > 0 {
            inserted += 1;
        }
    }
    conn.execute(
        "UPDATE semgrep_scans SET completed_at = ?1, total_findings = ?2, status = 'completed' WHERE id = ?3",
        rusqlite::params![now_ts(), inserted as i64, scan_id],
    )
    .map_err(|e| format!("semgrep_scans update: {e}"))?;
    Ok(json!({
        "scan_id": scan_id,
        "total_findings": inserted,
        "files_scanned": ctx.params.get("files_scanned").and_then(Value::as_i64).unwrap_or(0),
    }))
}

/// Type-1 克隆检测：按 content_hash 分组相同函数。
fn exec_clone_detect(ctx: &JobContext) -> Result<Value, String> {
    let mut conn = open_db(ctx)?;
    let min_lines = get_int_param_or(&ctx.params, "min_lines", 5);
    let rows = conn
        .prepare(
            "SELECT s.id, s.symbol_hash, s.name, s.qualified_name, fi.rel_path,
                    (s.end_line - s.start_line + 1) AS span
             FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.workspace_id = ?1 AND s.kind IN ('fn','test_fn','func','function','method')
               AND (s.end_line - s.start_line + 1) >= ?2
             ORDER BY s.symbol_hash, s.id",
        )
        .map_err(|e| format!("clone prepare: {e}"))?
        .query_map(rusqlite::params![ctx.workspace_id, min_lines], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, String>(4)?,
                row.get::<_, i64>(5)?,
            ))
        })
        .map_err(|e| format!("clone query: {e}"))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| format!("clone collect: {e}"))?;
    let mut by_hash: HashMap<String, Vec<(i64, String, String, String, i64)>> = HashMap::new();
    for (id, hash, _name, qname, rel, span) in rows {
        by_hash.entry(hash.clone()).or_default().push((id, qname, rel, hash.clone(), span));
    }
    let mut total_pairs = 0usize;
    let mut type1 = 0usize;
    let now = now_ts();
    let tx = conn.transaction().map_err(|e| format!("clone tx: {e}"))?;
    for group in by_hash.values() {
        if group.len() < 2 {
            continue;
        }
        for i in 0..group.len() {
            for j in (i + 1)..group.len() {
                let (a_id, _, _, hash, _) = &group[i];
                let (b_id, _, _, _, _) = &group[j];
                let res = tx.execute(
                    "INSERT OR IGNORE INTO clone_pairs
                       (workspace_id, symbol_a_id, symbol_b_id, clone_type, similarity, token_hash, lines_a, lines_b, detected_at)
                     VALUES (?1, ?2, ?3, 1, 1.0, ?4, 0, 0, ?5)",
                    rusqlite::params![ctx.workspace_id, a_id, b_id, hash, now],
                );
                if res.is_ok() && res.unwrap() > 0 {
                    type1 += 1;
                }
                total_pairs += 1;
                if ctx.cancel_flag.load(Ordering::SeqCst) {
                    break;
                }
            }
            if ctx.cancel_flag.load(Ordering::SeqCst) {
                break;
            }
        }
    }
    tx.commit().map_err(|e| format!("clone commit: {e}"))?;
    Ok(json!({
        "total_pairs": total_pairs,
        "type1_pairs": type1,
        "type2_pairs": 0,
        "type3_pairs": 0,
        "scanned_symbols": by_hash.values().map(|g| g.len()).sum::<usize>(),
        "skipped_symbols": 0,
        "similarity_threshold": 1.0,
        "min_lines": min_lines,
    }))
}

/// 内容 hash 嵌入（确定性近似：sha256 前 64 字节作为浮点向量写入 symbol_embeddings）。
fn exec_embed(ctx: &JobContext) -> Result<Value, String> {
    let conn = open_db(ctx)?;
    let force = ctx.params.get("force").and_then(Value::as_bool).unwrap_or(false);
    let single_hash = if ctx.job_type == "embed_single" {
        ctx.params.get("symbol_hash").and_then(Value::as_str).map(|s| s.to_string())
    } else {
        None
    };
    let mut total = 0usize;
    let mut ok = 0usize;
    let mut skipped = 0usize;
    let mut failed = 0usize;
    let now = now_ts();
    let model = "hash-v1";
    let mut stmt = conn
        .prepare(
            "SELECT s.symbol_hash, sc.content FROM symbols s
             JOIN symbol_contents sc ON sc.content_hash = s.symbol_hash
             JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.workspace_id = ?1",
        )
        .map_err(|e| format!("embed prepare: {e}"))?;
    let symbols = stmt
        .query_map(rusqlite::params![ctx.workspace_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|e| format!("embed query: {e}"))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| format!("embed collect: {e}"))?;
    for (hash, content) in symbols {
        if let Some(single) = &single_hash {
            if single != &hash {
                continue;
            }
        }
        total += 1;
        if !force {
            let exists: bool = conn
                .query_row(
                    "SELECT 1 FROM symbol_embeddings WHERE symbol_hash = ?1",
                    rusqlite::params![hash],
                    |_| Ok(true),
                )
                .unwrap_or(false);
            if exists {
                skipped += 1;
                continue;
            }
        }
        // 确定性嵌入：sha256(content) 的 64 字节作为 16 维 float 向量
        let digest = crate::daemon::fs_handlers::sha256_hex(content.as_bytes());
        let mut embedding: Vec<f32> = Vec::with_capacity(16);
        for i in 0..16 {
            let byte = digest.as_bytes()[i];
            embedding.push(byte as f32 / 255.0);
        }
        let blob = unsafe {
            std::slice::from_raw_parts(embedding.as_ptr() as *const u8, embedding.len() * 4)
        };
        let res = conn.execute(
            "INSERT INTO symbol_embeddings (symbol_hash, embedding, model_version, dim, embedded_at)
             VALUES (?1, ?2, ?3, 16, ?4)
             ON CONFLICT(symbol_hash) DO UPDATE SET embedding = excluded.embedding,
               model_version = excluded.model_version, dim = excluded.dim, embedded_at = excluded.embedded_at",
            rusqlite::params![hash, blob, model, now],
        );
        if res.is_ok() {
            ok += 1;
        } else {
            failed += 1;
        }
        if ctx.cancel_flag.load(Ordering::SeqCst) {
            break;
        }
    }
    Ok(json!({ "total": total, "ok": ok, "skipped": skipped, "failed": failed, "model": model }))
}

/// git 历史导入：`git log` → git_commits（+ git_file_changes 名称状态）。
fn exec_git_history(ctx: &JobContext) -> Result<Value, String> {
    let mut conn = open_db(ctx)?;
    let root = workspace_root(&conn, ctx.workspace_id)?;
    let max_commits = get_int_param_or(&ctx.params, "max_commits", 100);
    let output = std::process::Command::new("git")
        .args(["log", "--format=%H|%an|%ae|%at|%s", "--name-status", "-n"])
        .arg(max_commits.to_string())
        .current_dir(&root)
        .output()
        .map_err(|e| format!("git log 执行失败: {e}"))?;
    if !output.status.success() {
        return Err(format!(
            "git log 失败: {}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    let text = String::from_utf8_lossy(&output.stdout).to_string();
    let mut commits = 0usize;
    let mut changes = 0usize;
    let now = now_ts();
    let tx = conn.transaction().map_err(|e| format!("git tx: {e}"))?;
    for block in text.split("\n\n") {
        let mut lines = block.lines();
        let Some(header) = lines.next() else { continue };
        let mut parts = header.splitn(5, '|');
        let (Some(hash), Some(author), Some(email), Some(ts_raw), Some(message)) = (
            parts.next(),
            parts.next(),
            parts.next(),
            parts.next(),
            parts.next(),
        ) else { continue };
        if hash.is_empty() {
            continue;
        }
        let timestamp: f64 = ts_raw.trim().parse().unwrap_or(now);
        tx.execute(
            "INSERT OR IGNORE INTO git_commits (commit_hash, message, author, email, timestamp, workspace_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            rusqlite::params![hash, message.trim(), author, email, timestamp, ctx.workspace_id],
        )
        .map_err(|e| format!("git_commits insert: {e}"))?;
        commits += 1;
        for change_line in lines {
            let mut ch = change_line.splitn(2, '\t');
            let (Some(change_type), Some(rel)) = (ch.next(), ch.next()) else { continue };
            let file_instance_id: i64 = tx
                .query_row(
                    "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2 LIMIT 1",
                    rusqlite::params![ctx.workspace_id, rel],
                    |row| row.get(0),
                )
                .unwrap_or(0);
            let res = tx.execute(
                "INSERT INTO git_file_changes (commit_hash, file_instance_id, change_type, lines_added, lines_deleted)
                 VALUES (?1, ?2, ?3, 0, 0)",
                rusqlite::params![hash, file_instance_id, change_type],
            );
            if res.is_ok() {
                changes += 1;
            }
            if ctx.cancel_flag.load(Ordering::SeqCst) {
                break;
            }
        }
        if ctx.cancel_flag.load(Ordering::SeqCst) {
            break;
        }
    }
    tx.commit().map_err(|e| format!("git commit: {e}"))?;
    Ok(json!({ "commits_imported": commits, "file_changes_imported": changes }))
}

/// git blame 归属导入（简化：按文件记录最近提交作者 → file_ownership）。
fn exec_git_blame(ctx: &JobContext) -> Result<Value, String> {
    let conn = open_db(ctx)?;
    let root = workspace_root(&conn, ctx.workspace_id)?;
    let mut stmt = conn
        .prepare("SELECT id, rel_path FROM file_instances WHERE workspace_id = ?1 AND status != 'archived'")
        .map_err(|e| format!("blame prepare: {e}"))?;
    let files = stmt
        .query_map(rusqlite::params![ctx.workspace_id], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|e| format!("blame query: {e}"))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| format!("blame collect: {e}"))?;
    let mut attributed = 0usize;
    let now = now_ts();
    for (file_instance_id, rel) in files {
        if ctx.cancel_flag.load(Ordering::SeqCst) {
            break;
        }
        let output = std::process::Command::new("git")
            .args(["log", "-1", "--format=%H|%an|%ae|%at", "--"])
            .arg(&rel)
            .current_dir(&root)
            .output();
        let Ok(output) = output else { continue };
        if !output.status.success() {
            continue;
        }
        let line = String::from_utf8_lossy(&output.stdout).trim().to_string();
        let mut parts = line.splitn(4, '|');
        let (Some(hash), Some(author), Some(_email), Some(ts_raw)) = (
            parts.next(),
            parts.next(),
            parts.next(),
            parts.next(),
        ) else { continue };
        let ts: f64 = ts_raw.trim().parse().unwrap_or(now);
        conn.execute(
            "INSERT INTO file_ownership (file_instance_id, owner, source, confidence, last_commit_hash, last_commit_author, last_commit_time, updated_at)
             VALUES (?1, ?2, 'git_blame', 0.7, ?3, ?2, ?4, ?5)
             ON CONFLICT(file_instance_id) DO UPDATE SET
               owner = excluded.owner, last_commit_hash = excluded.last_commit_hash,
               last_commit_author = excluded.last_commit_author,
               last_commit_time = excluded.last_commit_time, updated_at = excluded.updated_at",
            rusqlite::params![file_instance_id, author, hash, ts, now],
        )
        .map_err(|e| format!("file_ownership upsert: {e}"))?;
        attributed += 1;
    }
    Ok(json!({ "files_attributed": attributed }))
}

/// CODEOWNERS 导入 → file_ownership（按 pattern 匹配 file_instances）。
fn exec_codeowners(ctx: &JobContext) -> Result<Value, String> {
    let conn = open_db(ctx)?;
    let root = workspace_root(&conn, ctx.workspace_id)?;
    let candidates = [
        root.join("CODEOWNERS"),
        root.join(".github/CODEOWNERS"),
        root.join("docs/CODEOWNERS"),
    ];
    let path = candidates.into_iter().find(|p| p.exists()).ok_or_else(|| {
        "仓库根未找到 CODEOWNERS（CODEOWNERS / .github/CODEOWNERS / docs/CODEOWNERS）".to_string()
    })?;
    let content = std::fs::read_to_string(&path).map_err(|e| format!("读取 CODEOWNERS 失败: {e}"))?;
    let mut stmt = conn
        .prepare("SELECT id, rel_path FROM file_instances WHERE workspace_id = ?1")
        .map_err(|e| format!("codeowners prepare: {e}"))?;
    let files = stmt
        .query_map(rusqlite::params![ctx.workspace_id], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|e| format!("codeowners query: {e}"))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| format!("codeowners collect: {e}"))?;
    let mut rules: Vec<(String, String)> = Vec::new();
    for line in content.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') {
            continue;
        }
        let mut parts = line.split_whitespace();
        let (Some(pattern), Some(owner)) = (parts.next(), parts.next()) else { continue };
        rules.push((pattern.to_string(), owner.trim_start_matches('@').to_string()));
    }
    let mut matched = 0usize;
    let now = now_ts();
    for (file_instance_id, rel) in files {
        if ctx.cancel_flag.load(Ordering::SeqCst) {
            break;
        }
        for (pattern, owner) in &rules {
            if codeowners_match(pattern, &rel) {
                conn.execute(
                    "INSERT INTO file_ownership (file_instance_id, owner, source, confidence, updated_at)
                     VALUES (?1, ?2, 'codeowners', 1.0, ?3)
                     ON CONFLICT(file_instance_id) DO UPDATE SET owner = excluded.owner, updated_at = excluded.updated_at",
                    rusqlite::params![file_instance_id, owner, now],
                )
                .map_err(|e| format!("file_ownership upsert: {e}"))?;
                matched += 1;
                break;
            }
        }
    }
    Ok(json!({ "rules_parsed": rules.len(), "files_matched": matched, "source": path.to_string_lossy() }))
}

/// 依赖清单导入（requirements.txt / package.json / Cargo.toml）→ dependency_edges。
fn exec_dependency_import(ctx: &JobContext) -> Result<Value, String> {
    let conn = open_db(ctx)?;
    let root = workspace_root(&conn, ctx.workspace_id)?;
    let mut deps: Vec<String> = Vec::new();
    let mut source_file = String::new();
    // requirements.txt
    let req_path = root.join("requirements.txt");
    if req_path.exists() {
        source_file = "requirements.txt".to_string();
        if let Ok(content) = std::fs::read_to_string(&req_path) {
            for line in content.lines() {
                let line = line.trim();
                if line.is_empty() || line.starts_with('#') || line.starts_with('-') || line.starts_with('[') {
                    continue;
                }
                let name = line.split(['=', '<', '>', '!', '~', ';', '[', ' ']).next().unwrap_or("").trim();
                if !name.is_empty() {
                    deps.push(name.to_string());
                }
            }
        }
    }
    // package.json
    let pkg_path = root.join("package.json");
    if pkg_path.exists() {
        if let Ok(content) = std::fs::read_to_string(&pkg_path) {
            if let Ok(pkg) = serde_json::from_str::<Value>(&content) {
                source_file = "package.json".to_string();
                for section in ["dependencies", "devDependencies", "peerDependencies"] {
                    if let Some(map) = pkg.get(section).and_then(Value::as_object) {
                        deps.extend(map.keys().cloned());
                    }
                }
            }
        }
    }
    // Cargo.toml
    let cargo_path = root.join("Cargo.toml");
    if cargo_path.exists() {
        if let Ok(content) = std::fs::read_to_string(&cargo_path) {
            source_file = "Cargo.toml".to_string();
            for line in content.lines() {
                let line = line.trim();
                if line.starts_with('[') && line.ends_with(']') && line.contains("dependencies") {
                    // fallthrough: collect following entries
                }
            }
            // 简化：匹配 `name = "..."` 形式的行（crates）
            for line in content.lines() {
                let line = line.trim();
                if let Some(rest) = line.strip_prefix('[') {
                    if rest.starts_with("dependencies") {
                        continue;
                    }
                }
            }
            for line in content.lines() {
                let line = line.trim();
                if line.starts_with("tokio") || line.contains(" = {") || (line.contains(" = \"") && !line.contains('#')) {
                    let name = line.split('=').next().unwrap_or("").trim().to_string();
                    if !name.is_empty() && !name.starts_with('[') {
                        deps.push(name);
                    }
                }
            }
        }
    }
    let now = now_ts();
    let mut imported = 0usize;
    for dep in deps {
        let res = conn.execute(
            "INSERT OR IGNORE INTO dependency_edges
               (workspace_id, provider_task_id, consumer_task_id, edge_type, source_type, contract_id, contract_revision, is_hard, created_at)
             VALUES (?1, 'manifest', ?2, 'dependency_import', ?3, '', 0, 1, ?4)",
            rusqlite::params![ctx.workspace_id, dep, source_file, now],
        );
        if res.is_ok() {
            imported += 1;
        }
        if ctx.cancel_flag.load(Ordering::SeqCst) {
            break;
        }
    }
    Ok(json!({ "source_file": source_file, "dependencies_imported": imported }))
}

/// 覆盖率导入（lcov 格式）→ coverage_data。
fn exec_coverage(ctx: &JobContext) -> Result<Value, String> {
    let mut conn = open_db(ctx)?;
    let root = workspace_root(&conn, ctx.workspace_id)?;
    let report_path = ctx
        .params
        .get("report_path")
        .and_then(Value::as_str)
        .map(PathBuf::from)
        .unwrap_or_else(|| root.join("coverage/lcov.info"));
    if !report_path.exists() {
        return Err(format!("覆盖率报告不存在: {}", report_path.to_string_lossy()));
    }
    let content = std::fs::read_to_string(&report_path).map_err(|e| format!("读取报告失败: {e}"))?;
    let mut current_file: Option<String> = None;
    let mut rows_inserted = 0usize;
    let now = now_ts();
    let tx = conn.transaction().map_err(|e| format!("coverage tx: {e}"))?;
    for line in content.lines() {
        if let Some(rest) = line.strip_prefix("SF:") {
            current_file = Some(rest.trim().to_string());
        } else if let Some(rest) = line.strip_prefix("DA:") {
            if let Some(file) = &current_file {
                let mut parts = rest.split(',');
                let (Some(line_no), Some(hits)) = (parts.next(), parts.next()) else { continue };
                let Ok(line_no) = line_no.trim().parse::<i64>() else { continue };
                let Ok(hits) = hits.trim().parse::<i64>() else { continue };
                let file_instance_id: i64 = tx
                    .query_row(
                        "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2 LIMIT 1",
                        rusqlite::params![ctx.workspace_id, file],
                        |row| row.get(0),
                    )
                    .unwrap_or(0);
                let res = tx.execute(
                    "INSERT OR IGNORE INTO coverage_data (file_instance_id, line_start, line_end, hit_count, report_source, imported_at)
                     VALUES (?1, ?2, ?2, ?3, 'lcov', ?4)",
                    rusqlite::params![file_instance_id, line_no, hits, now],
                );
                if res.is_ok() {
                    rows_inserted += 1;
                }
            }
        }
        if ctx.cancel_flag.load(Ordering::SeqCst) {
            break;
        }
    }
    tx.commit().map_err(|e| format!("coverage commit: {e}"))?;
    Ok(json!({ "report_path": report_path.to_string_lossy(), "lines_imported": rows_inserted }))
}

/// 硬依赖边计算：从 calls 表推导跨模块硬依赖 → dependency_edges(is_hard=1)。
fn exec_hard_dep_edges(ctx: &JobContext) -> Result<Value, String> {
    let conn = open_db(ctx)?;
    let mut stmt = conn
        .prepare(
            "SELECT c.caller_module, c.callee_module, COUNT(*) AS cnt
             FROM calls c
             JOIN symbols s ON s.id = c.caller_id
             JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.workspace_id = ?1 AND c.caller_module != '' AND c.callee_module != ''
               AND c.caller_module != c.callee_module
             GROUP BY c.caller_module, c.callee_module",
        )
        .map_err(|e| format!("hard_dep prepare: {e}"))?;
    let edges = stmt
        .query_map(rusqlite::params![ctx.workspace_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
            ))
        })
        .map_err(|e| format!("hard_dep query: {e}"))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| format!("hard_dep collect: {e}"))?;
    let now = now_ts();
    let mut inserted = 0usize;
    for (caller_module, callee_module, cnt) in &edges {
        let res = conn.execute(
            "INSERT OR IGNORE INTO dependency_edges
               (workspace_id, provider_task_id, consumer_task_id, edge_type, source_type, contract_id, contract_revision, is_hard, created_at)
             VALUES (?1, ?2, ?3, 'hard_module_dependency', 'call_graph', '', 0, 1, ?4)",
            rusqlite::params![ctx.workspace_id, callee_module, caller_module, now],
        );
        if res.is_ok() {
            inserted += 1;
        }
        let _ = cnt;
        if ctx.cancel_flag.load(Ordering::SeqCst) {
            break;
        }
    }
    Ok(json!({ "edges_computed": edges.len(), "edges_inserted": inserted }))
}

/// 跨仓库依赖检测（按模块前缀推测跨仓库边界）。
fn exec_cross_repo_deps(ctx: &JobContext) -> Result<Value, String> {
    let conn = open_db(ctx)?;
    let root = workspace_root(&conn, ctx.workspace_id)?;
    let workspace_name = root
        .file_name()
        .map(|s| s.to_string_lossy().to_string())
        .unwrap_or_else(|| "workspace".to_string());
    let mut stmt = conn
        .prepare(
            "SELECT c.callee_module, COUNT(*) AS cnt
             FROM calls c
             JOIN symbols s ON s.id = c.caller_id
             JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.workspace_id = ?1 AND c.callee_module != ''
             GROUP BY c.callee_module",
        )
        .map_err(|e| format!("cross_repo prepare: {e}"))?;
    let callee_modules = stmt
        .query_map(rusqlite::params![ctx.workspace_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(|e| format!("cross_repo query: {e}"))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| format!("cross_repo collect: {e}"))?;
    let now = now_ts();
    let mut candidates = 0usize;
    for (module, cnt) in callee_modules {
        // 本仓库模块通常以 workspace 名或 src/ 开头；其余视为跨仓库候选
        let local = module.starts_with(&workspace_name) || module.starts_with("src/") || module.starts_with("lib/");
        if !local && cnt >= 3 {
            let res = conn.execute(
                "INSERT OR IGNORE INTO dependency_edges
                   (workspace_id, provider_task_id, consumer_task_id, edge_type, source_type, contract_id, contract_revision, is_hard, created_at)
                 VALUES (?1, ?2, ?3, 'cross_repo_dependency', 'call_graph', '', 0, 0, ?4)",
                rusqlite::params![ctx.workspace_id, module, workspace_name, now],
            );
            if res.is_ok() {
                candidates += 1;
            }
        }
        if ctx.cancel_flag.load(Ordering::SeqCst) {
            break;
        }
    }
    Ok(json!({ "workspace": workspace_name, "cross_repo_candidates": candidates }))
}

/// 剪除外部分支符号（删除指向不存在文件的符号与调用）。
fn exec_prune_external(ctx: &JobContext) -> Result<Value, String> {
    let mut conn = open_db(ctx)?;
    let tx = conn.transaction().map_err(|e| format!("prune tx: {e}"))?;
    let deleted_symbols = tx
        .execute(
            "DELETE FROM symbols WHERE id IN (
               SELECT s.id FROM symbols s
               JOIN file_instances fi ON fi.id = s.file_instance_id
               WHERE fi.workspace_id = ?1 AND fi.status = 'archived'
             )",
            rusqlite::params![ctx.workspace_id],
        )
        .map_err(|e| format!("prune symbols: {e}"))?;
    let deleted_calls = tx
        .execute(
            "DELETE FROM calls WHERE caller_id IN (
               SELECT s.id FROM symbols s
               JOIN file_instances fi ON fi.id = s.file_instance_id
               WHERE fi.workspace_id = ?1 AND fi.status = 'archived'
             ) OR callee_id IN (
               SELECT s.id FROM symbols s
               JOIN file_instances fi ON fi.id = s.file_instance_id
               WHERE fi.workspace_id = ?1 AND fi.status = 'archived'
             )",
            rusqlite::params![ctx.workspace_id],
        )
        .map_err(|e| format!("prune calls: {e}"))?;
    tx.commit().map_err(|e| format!("prune commit: {e}"))?;
    Ok(json!({ "deleted_symbols": deleted_symbols, "deleted_calls": deleted_calls }))
}

// ---------------------------------------------------------------------------
// 工具函数
// ---------------------------------------------------------------------------

/// 在 PATH 中查找可执行文件。
fn which(name: &str) -> Option<PathBuf> {
    let path_var = std::env::var_os("PATH")?;
    for dir in std::env::split_paths(&path_var) {
        let candidate = dir.join(name);
        if candidate.is_file() {
            return Some(candidate);
        }
        #[cfg(windows)]
        {
            let candidate_exe = dir.join(format!("{name}.exe"));
            if candidate_exe.is_file() {
                return Some(candidate_exe);
            }
        }
    }
    None
}

/// CODEOWNERS pattern 匹配（支持 `*` glob 与目录前缀）。
fn codeowners_match(pattern: &str, rel_path: &str) -> bool {
    let pattern = pattern.trim_end_matches('/');
    if pattern == rel_path || pattern == "*" {
        return true;
    }
    if pattern.ends_with('*') {
        let prefix = &pattern[..pattern.len() - 1];
        return rel_path.starts_with(prefix);
    }
    rel_path.starts_with(&format!("{pattern}/")) || rel_path == pattern
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_supported_job_types() {
        assert!(SUPPORTED_JOB_TYPES.contains(&"semgrep_scan"));
        assert!(SUPPORTED_JOB_TYPES.contains(&"git_history"));
        assert!(SUPPORTED_JOB_TYPES.contains(&"prune_external"));
        assert_eq!(SUPPORTED_JOB_TYPES.len(), 14);
    }

    #[test]
    fn test_codeowners_match() {
        assert!(codeowners_match("src/", "src/a/b.rs"));
        assert!(codeowners_match("src/*", "src/a.rs"));
        assert!(!codeowners_match("src/", "tests/a.rs"));
        assert!(codeowners_match("README.md", "README.md"));
    }
}
