//! GC/审计/运维 handler（T02-admin 批次，22 个工具）。
//!
//! 对应 `tool_migration_matrix.json` 中 target_backend=rust_native、
//! batch=T02-admin 的 22 个拒止写面/运维工具：
//! gc_archive_import/inspect/list、gc_audit_get/list、gc_policy_get/set、
//! gc_retention、audit_rotate_key、cleanup_rule_sync_log、clear_clones、
//! snapshot_compare、metrics_get、branch_register/switch、
//! assignment_create/revoke、record_action_identity、
//! register_attestation_revocation、record_artifact_identity、
//! publish_interface、select_interface_provider。
//!
//! 所有写操作经 daemon 权威路径（调用方负责 SerializationPoint 串行化），
//! 本模块只操作 workspace codegraph DB（rusqlite 写连接，由调用方传入）。

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::dispatch::{get_int_param_or, get_str_param, get_str_param_or, require_str_param, DaemonRpcError};

fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// `admin.gc_archive_import` —— 导入归档记录（archived_files 写入）。
pub fn handle_gc_archive_import(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let archive_path = require_str_param(params, "archive_path")?;
    let reason = get_str_param_or(params, "archive_reason", "manual_import");
    let now = now_ts();
    let file_instance_id = get_int_param_or(params, "file_instance_id", 0);
    let rel_path = get_str_param_or(params, "rel_path", "");
    let content_hash = get_str_param_or(params, "content_hash", "");
    let inserted = conn
        .execute(
            "INSERT INTO archived_files
               (file_instance_id, workspace_id, rel_path, abs_path, content_hash, symbol_count, call_count, archive_reason, archived_at)
             VALUES (?1, ?2, ?3, ?4, ?5, 0, 0, ?6, ?7)",
            rusqlite::params![file_instance_id, workspace_id, rel_path, archive_path, content_hash, reason, now],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_archive_import: {e}")))?;
    Ok(json!({ "ok": true, "inserted": inserted, "archive_path": archive_path }))
}

/// `admin.gc_archive_inspect` —— 检查归档记录。
pub fn handle_gc_archive_inspect(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let archive_path = require_str_param(params, "archive_path")?;
    let rows = conn
        .prepare(
            "SELECT id, file_instance_id, rel_path, abs_path, content_hash, symbol_count, call_count, archive_reason, archived_at
             FROM archived_files WHERE workspace_id = ?1 AND (abs_path = ?2 OR rel_path = ?2) LIMIT 1",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_archive_inspect prepare: {e}")))?
        .query_map(rusqlite::params![workspace_id, archive_path], |row| {
            Ok(json!({
                "id": row.get::<_, i64>(0)?,
                "file_instance_id": row.get::<_, i64>(1)?,
                "rel_path": row.get::<_, String>(2)?,
                "abs_path": row.get::<_, String>(3)?,
                "content_hash": row.get::<_, String>(4)?,
                "symbol_count": row.get::<_, i64>(5)?,
                "call_count": row.get::<_, i64>(6)?,
                "archive_reason": row.get::<_, String>(7)?,
                "archived_at": row.get::<_, f64>(8)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_archive_inspect query: {e}")))?
        .next();
    match rows {
        Some(Ok(v)) => Ok(v),
        Some(Err(e)) => Err(DaemonRpcError::internal_error(format!("gc_archive_inspect row: {e}"))),
        None => Ok(Value::Null),
    }
}

/// `admin.gc_archive_list` —— 列出归档记录。
pub fn handle_gc_archive_list(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 20).clamp(1, 500);
    let rows = conn
        .prepare(
            "SELECT id, file_instance_id, rel_path, abs_path, archive_reason, archived_at
             FROM archived_files WHERE workspace_id = ?1 ORDER BY archived_at DESC LIMIT ?2",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_archive_list prepare: {e}")))?
        .query_map(rusqlite::params![workspace_id, limit], |row| {
            Ok(json!({
                "id": row.get::<_, i64>(0)?,
                "file_instance_id": row.get::<_, i64>(1)?,
                "rel_path": row.get::<_, String>(2)?,
                "abs_path": row.get::<_, String>(3)?,
                "archive_reason": row.get::<_, String>(4)?,
                "archived_at": row.get::<_, f64>(5)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_archive_list query: {e}")))?
        .collect::<Result<Vec<Value>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_archive_list collect: {e}")))?;
    Ok(Value::Array(rows))
}

/// `admin.gc_audit_get` —— 按 audit id 查询变更审计。
pub fn handle_gc_audit_get(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let audit_id = require_str_param(params, "audit_id")?;
    let rows = conn
        .prepare(
            "SELECT id, task_id, step_id, file_path, hash_before, hash_after, diff, author, timestamp
             FROM change_audit WHERE id = ?1 AND task_id IN (SELECT id FROM tasks WHERE workspace_id = ?2)
             LIMIT 1",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_audit_get prepare: {e}")))?
        .query_map(rusqlite::params![audit_id, workspace_id], |row| {
            Ok(json!({
                "id": row.get::<_, String>(0)?,
                "task_id": row.get::<_, String>(1)?,
                "step_id": row.get::<_, Option<String>>(2)?,
                "file_path": row.get::<_, String>(3)?,
                "hash_before": row.get::<_, String>(4)?,
                "hash_after": row.get::<_, String>(5)?,
                "diff": row.get::<_, String>(6)?,
                "author": row.get::<_, String>(7)?,
                "timestamp": row.get::<_, f64>(8)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_audit_get query: {e}")))?
        .next();
    match rows {
        Some(Ok(v)) => Ok(v),
        Some(Err(e)) => Err(DaemonRpcError::internal_error(format!("gc_audit_get row: {e}"))),
        None => Ok(Value::Null),
    }
}

/// `admin.gc_audit_list` —— 列出变更审计。
pub fn handle_gc_audit_list(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 20).clamp(1, 500);
    let rows = conn
        .prepare(
            "SELECT ca.id, ca.task_id, ca.file_path, ca.author, ca.timestamp
             FROM change_audit ca
             JOIN tasks t ON t.id = ca.task_id
             WHERE t.workspace_id = ?1
             ORDER BY ca.timestamp DESC LIMIT ?2",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_audit_list prepare: {e}")))?
        .query_map(rusqlite::params![workspace_id, limit], |row| {
            Ok(json!({
                "id": row.get::<_, String>(0)?,
                "task_id": row.get::<_, String>(1)?,
                "file_path": row.get::<_, String>(2)?,
                "author": row.get::<_, String>(3)?,
                "timestamp": row.get::<_, f64>(4)?,
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_audit_list query: {e}")))?
        .collect::<Result<Vec<Value>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_audit_list collect: {e}")))?;
    Ok(Value::Array(rows))
}

/// `admin.gc_policy_get` —— 读取 GC 策略（无记录时返回默认值）。
pub fn handle_gc_policy_get(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let _ = (workspace_id, params);
    let rows = conn
        .query_row(
            "SELECT older_than_days, keep_versions, include_external, external_stale_days, backup_enabled, vacuum_enabled, updated_at
             FROM gc_policies LIMIT 1",
            [],
            |row| {
                Ok(json!({
                    "older_than_days": row.get::<_, i64>(0)?,
                    "keep_versions": row.get::<_, i64>(1)?,
                    "include_external": row.get::<_, i64>(2)? != 0,
                    "external_stale_days": row.get::<_, i64>(3)?,
                    "backup_enabled": row.get::<_, i64>(4)? != 0,
                    "vacuum_enabled": row.get::<_, i64>(5)? != 0,
                    "updated_at": row.get::<_, f64>(6)?,
                }))
            },
        );
    match rows {
        Ok(v) => Ok(v),
        Err(_) => Ok(json!({
            "older_than_days": 30,
            "keep_versions": 3,
            "include_external": false,
            "external_stale_days": 90,
            "backup_enabled": true,
            "vacuum_enabled": false,
            "updated_at": 0.0,
        })),
    }
}

/// `admin.gc_policy_set` —— 写入 GC 策略（UPSERT 单行）。
pub fn handle_gc_policy_set(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let older_than_days = get_int_param_or(params, "older_than_days", 30);
    let keep_versions = get_int_param_or(params, "keep_versions", 3);
    let include_external = params.get("include_external").and_then(Value::as_bool).unwrap_or(false);
    let external_stale_days = get_int_param_or(params, "external_stale_days", 90);
    let backup_enabled = params.get("backup_enabled").and_then(Value::as_bool).unwrap_or(true);
    let vacuum_enabled = params.get("vacuum_enabled").and_then(Value::as_bool).unwrap_or(false);
    let now = now_ts();
    conn.execute(
        "CREATE TABLE IF NOT EXISTS gc_policies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            older_than_days INTEGER NOT NULL DEFAULT 30,
            keep_versions INTEGER NOT NULL DEFAULT 3,
            include_external INTEGER NOT NULL DEFAULT 0,
            external_stale_days INTEGER NOT NULL DEFAULT 90,
            backup_enabled INTEGER NOT NULL DEFAULT 1,
            vacuum_enabled INTEGER NOT NULL DEFAULT 0,
            updated_at REAL NOT NULL
         )",
        [],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("gc_policies ddl: {e}")))?;
    conn.execute(
        "INSERT INTO gc_policies (older_than_days, keep_versions, include_external, external_stale_days, backup_enabled, vacuum_enabled, updated_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        rusqlite::params![
            older_than_days,
            keep_versions,
            include_external as i64,
            external_stale_days,
            backup_enabled as i64,
            vacuum_enabled as i64,
            now
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("gc_policies insert: {e}")))?;
    let _ = workspace_id;
    Ok(json!({
        "ok": true,
        "older_than_days": older_than_days,
        "keep_versions": keep_versions,
        "include_external": include_external,
        "external_stale_days": external_stale_days,
        "backup_enabled": backup_enabled,
        "vacuum_enabled": vacuum_enabled,
    }))
}

/// `admin.gc_retention` —— 保留策略统计（归档/过期文件计数）。
pub fn handle_gc_retention(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let retention_days = get_int_param_or(params, "retention_days", 30);
    let cutoff = now_ts() - retention_days as f64 * 86400.0;
    let archived = conn
        .query_row(
            "SELECT COUNT(*) FROM archived_files WHERE workspace_id = ?1 AND archived_at < ?2",
            rusqlite::params![workspace_id, cutoff],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_retention archived: {e}")))?;
    let active = conn
        .query_row(
            "SELECT COUNT(*) FROM file_instances WHERE workspace_id = ?1 AND status != 'archived'",
            rusqlite::params![workspace_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("gc_retention active: {e}")))?;
    Ok(json!({
        "retention_days": retention_days,
        "archived_eligible": archived,
        "active_files": active,
        "total": archived + active,
    }))
}

/// `admin.audit_rotate_key` —— 轮换审计签名密钥。
pub fn handle_audit_rotate_key(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let reason = get_str_param_or(params, "reason", "scheduled_rotation");
    let now = now_ts();
    conn.execute(
        "UPDATE audit_key_rotations SET is_active = 0 WHERE is_active = 1",
        [],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("audit key deactivate: {e}")))?;
    let key_id = format!("key-{:016x}", (now * 1000.0) as u64);
    let key_secret = crate::daemon::fs_handlers::sha256_hex(format!("{key_id}:{now}").as_bytes());
    conn.execute(
        "INSERT INTO audit_key_rotations (key_id, key_secret, rotated_at, is_active)
         VALUES (?1, ?2, ?3, 1)",
        rusqlite::params![key_id, key_secret, now],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("audit key rotate: {e}")))?;
    let _ = (workspace_id, reason);
    Ok(json!({ "ok": true, "key_id": key_id, "rotated_at": now }))
}

/// `admin.cleanup_rule_sync_log` —— 清理 AGENTS.md 规则同步日志。
pub fn handle_cleanup_rule_sync_log(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let before_ts = get_int_param_or(params, "before_ts", 0);
    let deleted = conn
        .execute(
            "DELETE FROM agent_rule_sync_log WHERE created_at < ?1",
            rusqlite::params![if before_ts > 0 { before_ts as f64 } else { now_ts() }],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("cleanup_rule_sync_log: {e}")))?;
    let _ = workspace_id;
    Ok(json!({ "ok": true, "deleted": deleted }))
}

/// `admin.clear_clones` —— 清空克隆检测结果。
pub fn handle_clear_clones(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let _ = params;
    let deleted = conn
        .execute(
            "DELETE FROM clone_pairs WHERE workspace_id = ?1",
            rusqlite::params![workspace_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("clear_clones: {e}")))?;
    Ok(json!({ "ok": true, "deleted_pairs": deleted }))
}

/// `admin.snapshot_compare` —— 快照对比（归档 vs 当前文件清单）。
pub fn handle_snapshot_compare(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let _ = params;
    let archived_count = conn
        .query_row(
            "SELECT COUNT(*) FROM archived_files WHERE workspace_id = ?1",
            rusqlite::params![workspace_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("snapshot_compare archived: {e}")))?;
    let active_count = conn
        .query_row(
            "SELECT COUNT(*) FROM file_instances WHERE workspace_id = ?1 AND status != 'archived'",
            rusqlite::params![workspace_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("snapshot_compare active: {e}")))?;
    let symbol_count = conn
        .query_row(
            "SELECT COUNT(*) FROM symbols s JOIN file_instances fi ON fi.id = s.file_instance_id WHERE fi.workspace_id = ?1",
            rusqlite::params![workspace_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("snapshot_compare symbols: {e}")))?;
    Ok(json!({
        "archived_files": archived_count,
        "active_files": active_count,
        "symbols": symbol_count,
        "delta_files": active_count - archived_count,
    }))
}

/// `admin.metrics_get` —— daemon 进程度量（无需 DB）。
pub fn handle_metrics_get(params: &Value) -> Result<Value, DaemonRpcError> {
    let _ = params;
    let uptime = std::time::Instant::now()
        .elapsed()
        .as_secs_f64();
    let job_count = super::job_runner::rpc_job_list(&json!({}))
        .map(|v| v.as_array().map(|a| a.len()).unwrap_or(0))
        .unwrap_or(0);
    Ok(json!({
        "pid": std::process::id(),
        "uptime_seconds": uptime,
        "schema_version": super::SCHEMA_VERSION,
        "active_jobs": job_count,
        "transport": "http",
    }))
}

/// `metrics.snapshot` —— daemon 运行时度量快照（Rust daemon 为唯一 authority）。
///
/// CLI-004 整改：原先 `cw daemon metrics` 在 RPC 失败时静默降级到
/// 进程内 Python `MetricsCollector`（in-process SQLite），违反「Rust daemon
/// 为唯一 authority」。本 handler 提供 daemon 自身运行时视图，结构对齐 CLI
/// 的 counters/gauges/histograms 过滤逻辑；用户自定义计数器/直方图当前由
/// daemon 进程内维护，本快照仅暴露运行时指标（gauges）。
pub fn handle_metrics_snapshot(_params: &Value) -> Result<Value, DaemonRpcError> {
    let uptime = std::time::Instant::now().elapsed().as_secs_f64();
    let job_count = super::job_runner::rpc_job_list(&json!({}))
        .map(|v| v.as_array().map(|a| a.len()).unwrap_or(0))
        .unwrap_or(0);
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis() as i64)
        .unwrap_or(0);
    let gauges = json!({
        "daemon.pid": std::process::id(),
        "daemon.uptime_seconds": uptime,
        "daemon.active_jobs": job_count,
        "daemon.schema_version": super::SCHEMA_VERSION,
    });
    Ok(json!({
        "timestamp": ts,
        "uptime": uptime,
        "uptime_seconds": uptime,
        "pid": std::process::id(),
        "schema_version": super::SCHEMA_VERSION,
        "active_jobs": job_count,
        "transport": "http",
        "source": "daemon_runtime",
        "counters": {},
        "gauges": gauges,
        "histograms": {},
    }))
}

/// `metrics.prometheus` —— 同 `metrics.snapshot`，但导出 Prometheus 文本格式。
pub fn handle_metrics_prometheus(_params: &Value) -> Result<Value, DaemonRpcError> {
    let snap = handle_metrics_snapshot(_params)?;
    let uptime = snap.get("uptime_seconds").and_then(Value::as_f64).unwrap_or(0.0);
    let jobs = snap.get("active_jobs").and_then(Value::as_i64).unwrap_or(0);
    let pid = snap.get("pid").and_then(Value::as_i64).unwrap_or(0);
    let mut out = String::new();
    out.push_str("# HELP callwarden_daemon_uptime_seconds Daemon uptime in seconds\n");
    out.push_str("# TYPE callwarden_daemon_uptime_seconds gauge\n");
    out.push_str(&format!("callwarden_daemon_uptime_seconds {}\n", uptime));
    out.push_str("# HELP callwarden_daemon_active_jobs Number of active jobs\n");
    out.push_str("# TYPE callwarden_daemon_active_jobs gauge\n");
    out.push_str(&format!("callwarden_daemon_active_jobs {}\n", jobs));
    out.push_str("# HELP callwarden_daemon_pid Daemon process id\n");
    out.push_str("# TYPE callwarden_daemon_pid gauge\n");
    out.push_str(&format!("callwarden_daemon_pid {}\n", pid));
    Ok(Value::String(out))
}

/// `admin.branch_register` —— 注册分支（daemon 自有的 daemon_branches 表）。
pub fn handle_branch_register(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let name = require_str_param(params, "name")?;
    if name.is_empty() || name.contains('/') && !name.starts_with("refs/") {
        return Err(DaemonRpcError::invalid_params("非法分支名"));
    }
    let ref_sha = get_str_param_or(params, "ref_sha", "");
    let now = now_ts();
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daemon_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            ref_sha TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            UNIQUE(workspace_id, name)
         )",
        [],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("daemon_branches ddl: {e}")))?;
    conn.execute(
        "INSERT INTO daemon_branches (workspace_id, name, ref_sha, is_active, created_at)
         VALUES (?1, ?2, ?3, 0, ?4)
         ON CONFLICT(workspace_id, name) DO UPDATE SET ref_sha = excluded.ref_sha",
        rusqlite::params![workspace_id, name, ref_sha, now],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("branch register: {e}")))?;
    Ok(json!({ "ok": true, "name": name, "ref_sha": ref_sha }))
}

/// `admin.branch_switch` —— 切换活动分支。
pub fn handle_branch_switch(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let name = require_str_param(params, "name")?;
    conn.execute(
        "CREATE TABLE IF NOT EXISTS daemon_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            ref_sha TEXT DEFAULT '',
            is_active INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            UNIQUE(workspace_id, name)
         )",
        [],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("daemon_branches ddl: {e}")))?;
    conn.execute(
        "UPDATE daemon_branches SET is_active = 0 WHERE workspace_id = ?1",
        rusqlite::params![workspace_id],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("branch switch deactivate: {e}")))?;
    let changed = conn
        .execute(
            "UPDATE daemon_branches SET is_active = 1 WHERE workspace_id = ?1 AND name = ?2",
            rusqlite::params![workspace_id, name],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("branch switch activate: {e}")))?;
    if changed == 0 {
        return Err(DaemonRpcError::new(
            "branch_not_found",
            format!("分支 {name} 未注册，请先 register_branch"),
        ));
    }
    Ok(json!({ "ok": true, "name": name, "is_active": true }))
}

/// `admin.assignment_create` —— 创建 Assignment（P4 Req 11.1）。
pub fn handle_assignment_create(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let task_id = require_str_param(params, "task_id")?;
    let role = get_str_param_or(params, "role", "implementer");
    let agent_id = require_str_param(params, "agent_id")?;
    let session_id = get_str_param_or(params, "session_id", "");
    let model_id = get_str_param_or(params, "model_id", "");
    let now = now_ts();
    conn.execute(
        "INSERT INTO task_assignments (workspace_id, task_id, role, agent_id, session_id, model_id, status, created_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active', ?7)",
        rusqlite::params![workspace_id, task_id, role, agent_id, session_id, model_id, now],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("assignment_create: {e}")))?;
    let assignment_id = conn.last_insert_rowid();
    Ok(json!({
        "ok": true,
        "assignment_id": assignment_id,
        "task_id": task_id,
        "role": role,
        "agent_id": agent_id,
        "session_id": session_id,
        "model_id": model_id,
        "created_at": now,
    }))
}

/// `admin.assignment_revoke` —— 撤销 Assignment。
pub fn handle_assignment_revoke(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let task_id = require_str_param(params, "task_id")?;
    let role = get_str_param_or(params, "role", "implementer");
    let reason = get_str_param_or(params, "reason", "");
    let now = now_ts();
    let changed = conn
        .execute(
            "UPDATE task_assignments SET status = 'revoked', revoked_at = ?1
             WHERE workspace_id = ?2 AND task_id = ?3 AND role = ?4 AND status = 'active'",
            rusqlite::params![now, workspace_id, task_id, role],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("assignment_revoke: {e}")))?;
    if changed == 0 {
        return Err(DaemonRpcError::new(
            "assignment_not_found",
            format!("task {task_id} 角色 {role} 无 active assignment"),
        ));
    }
    Ok(json!({ "ok": true, "task_id": task_id, "role": role, "reason": reason }))
}

/// `admin.record_action_identity` —— 记录动作身份（Governance 审计）。
pub fn handle_record_action_identity(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let action_type = get_str_param_or(params, "action_type", "");
    let task_id = get_str_param_or(params, "task_id", "");
    let contract_id = get_str_param_or(params, "contract_id", "");
    let contract_revision = get_int_param_or(params, "contract_revision", 0);
    let agent_id = get_str_param_or(params, "agent_id", "");
    let session_id = get_str_param_or(params, "session_id", "");
    let model_id = get_str_param_or(params, "model_id", "");
    let role = get_str_param_or(params, "role", "");
    let now = now_ts();
    conn.execute(
        "INSERT INTO action_identities (workspace_id, action_type, task_id, contract_id, contract_revision, agent_id, session_id, model_id, role, recorded_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        rusqlite::params![
            workspace_id,
            action_type,
            task_id,
            contract_id,
            contract_revision,
            agent_id,
            session_id,
            model_id,
            role,
            now
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("record_action_identity: {e}")))?;
    Ok(json!({ "ok": true, "recorded_at": now }))
}

/// `admin.register_attestation_revocation` —— 登记 Attestation 撤销。
pub fn handle_register_attestation_revocation(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let issuer = get_str_param_or(params, "issuer", "");
    let signing_key_id = get_str_param_or(params, "signing_key_id", "");
    let revocation_mode = get_str_param_or(params, "revocation_mode", "");
    if revocation_mode != "compromised" && revocation_mode != "rotated" {
        return Err(DaemonRpcError::new(
            "E_REVOCATION_MODE_MISSING",
            "撤销请求必须显式指定 revocation_mode: compromised 或 rotated",
        ));
    }
    let revocation_reason = get_str_param_or(params, "revocation_reason", "");
    let initiating_actor = get_str_param_or(params, "initiating_actor", "");
    let now = now_ts();
    conn.execute(
        "INSERT INTO attestation_revocation_records (workspace_id, issuer, signing_key_id, revocation_mode, revocation_reason, initiating_actor, revoked_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)",
        rusqlite::params![
            workspace_id,
            issuer,
            signing_key_id,
            revocation_mode,
            revocation_reason,
            initiating_actor,
            now
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("register_attestation_revocation: {e}")))?;
    Ok(json!({ "ok": true, "revocation_mode": revocation_mode, "revoked_at": now }))
}

/// `admin.record_artifact_identity` —— 记录 artifact 身份。
pub fn handle_record_artifact_identity(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let artifact_id = require_str_param(params, "artifact_id")?;
    let task_id = get_str_param_or(params, "task_id", "");
    let contract_id = get_str_param_or(params, "contract_id", "");
    let contract_revision = get_int_param_or(params, "contract_revision", 0);
    let artifact_type = get_str_param_or(params, "artifact_type", "file");
    let artifact_ref = get_str_param_or(params, "artifact_ref", "");
    let artifact_hash = get_str_param_or(params, "artifact_hash", "");
    let now = now_ts();
    conn.execute(
        "INSERT INTO artifact_identities (workspace_id, artifact_id, task_id, contract_id, contract_revision, artifact_type, artifact_ref, artifact_hash, freshness_status, produced_at, workspace_snapshot_id)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, 'fresh', ?9, '')",
        rusqlite::params![
            workspace_id,
            artifact_id,
            task_id,
            contract_id,
            contract_revision,
            artifact_type,
            artifact_ref,
            artifact_hash,
            now
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("record_artifact_identity: {e}")))?;
    Ok(json!({ "ok": true, "artifact_id": artifact_id, "produced_at": now }))
}

/// `admin.publish_interface` —— 发布 interface identity。
pub fn handle_publish_interface(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let interface_name = require_str_param(params, "interface_name")?;
    let provider_task_id = get_str_param_or(params, "provider_task_id", "");
    let version = get_str_param_or(params, "version", "1.0.0");
    let contract_id = get_str_param_or(params, "contract_id", "");
    let contract_revision = get_int_param_or(params, "contract_revision", 0);
    let now = now_ts();
    let interface_id = format!(
        "IF-{:016x}",
        crate::daemon::fs_handlers::sha256_hex(format!("{interface_name}:{version}").as_bytes())
            .chars()
            .take(16)
            .collect::<String>()
            .parse::<u64>()
            .unwrap_or(0)
    );
    let interface_hash = crate::daemon::fs_handlers::sha256_hex(interface_name.as_bytes());
    conn.execute(
        "INSERT INTO interface_identities (workspace_id, interface_id, interface_name, version, interface_hash, provider_task_id, contract_id, contract_revision, published_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)
         ON CONFLICT(workspace_id, interface_name, version) DO UPDATE SET
           interface_hash = excluded.interface_hash,
           provider_task_id = excluded.provider_task_id,
           published_at = excluded.published_at",
        rusqlite::params![
            workspace_id,
            interface_id,
            interface_name,
            version,
            interface_hash,
            provider_task_id,
            contract_id,
            contract_revision,
            now
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("publish_interface: {e}")))?;
    Ok(json!({ "ok": true, "interface_id": interface_id, "interface_name": interface_name, "version": version }))
}

/// `admin.select_interface_provider` —— 显式选择 interface provider。
pub fn handle_select_interface_provider(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let interface_name = require_str_param(params, "interface_name")?;
    let provider_task_id = require_str_param(params, "provider_task_id")?;
    let consumer_task_id = get_str_param_or(params, "consumer_task_id", "");
    let contract_id = get_str_param_or(params, "contract_id", "");
    let contract_revision = get_int_param_or(params, "contract_revision", 0);
    let now = now_ts();
    conn.execute(
        "INSERT INTO interface_provider_selections (workspace_id, consumer_task_id, contract_id, contract_revision, interface_name, selected_provider_task_id, selected_at)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
         ON CONFLICT(workspace_id, consumer_task_id, contract_id, contract_revision, interface_name) DO UPDATE SET
           selected_provider_task_id = excluded.selected_provider_task_id,
           selected_at = excluded.selected_at",
        rusqlite::params![
            workspace_id,
            consumer_task_id,
            contract_id,
            contract_revision,
            interface_name,
            provider_task_id,
            now
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("select_interface_provider: {e}")))?;
    Ok(json!({
        "ok": true,
        "interface_name": interface_name,
        "selected_provider_task_id": provider_task_id,
        "selected_at": now,
    }))
}

// ---------------------------------------------------------------------------
// 参数辅助（模块内再导出，供 snapshot_state.rs 复用）
// ---------------------------------------------------------------------------

pub fn _param_str<'a>(params: &'a Value, key: &str) -> Option<&'a str> {
    get_str_param(params, key)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_now_ts_positive() {
        assert!(now_ts() > 1_700_000_000.0);
    }

    #[test]
    fn test_revocation_mode_validation() {
        // 非法 mode 必须返回结构化 E_REVOCATION_MODE_MISSING
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE IF NOT EXISTS attestation_revocation_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                issuer TEXT DEFAULT '',
                signing_key_id TEXT DEFAULT '',
                revocation_mode TEXT NOT NULL,
                revocation_reason TEXT DEFAULT '',
                initiating_actor TEXT DEFAULT '',
                revoked_at REAL NOT NULL
             );",
        )
        .unwrap();
        let params = json!({ "revocation_mode": "invalid" });
        let err = handle_register_attestation_revocation(&conn, 1, &params).unwrap_err();
        assert_eq!(err.code, "E_REVOCATION_MODE_MISSING");
    }
}
