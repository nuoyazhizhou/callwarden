//! Audit log 面 handler（SRV-002：server audit log Python authority → Rust daemon）。
//!
//! 对应 `server/audit_log.py::AuditLogger` 的 SQLite 权威下沉：daemon 成为 `audit_log`
//! 表（`audit.db`，路径 = `config::DaemonConfig.audit_db_path`，默认
//! `/var/log/callwarden/audit.log`）的唯一写者。Python `AuditLogger` 退化为纯 RPC 客户端
//! （见 `server/audit_log.py` 的 `mcp.audit_log.*` 调用），不再 import `sqlite3`。
//!
//! 不变量：
//! - 数据源：daemon 配置 `audit_db_path`；所有写操作在本模块内打开 SQLite 连接；
//! - fail-closed：`audit_db_path` 为空时 `handle_get_conn`/`handle_init_db` 返回稳定错误码，
//!   绝不回退本地 SQLite 或内存缓冲；
//! - 身份控制：传输层已保证 peer 身份（SO_PEERCRED / 命名管道 SID）；`workspace_instance_id`
//!   可选归因，不改变权威路径（路径为全局）。

use std::path::{Path, PathBuf};

use rusqlite::Connection;
use serde_json::{json, Map, Value};

use super::dispatch::{get_int_param_or, get_str_param_or, DaemonRpcError};

/// audit_log 表 + 索引 DDL（与 Python `server/schema_migrator.py::_audit_v1/_audit_v2` 一致）。
const AUDIT_LOG_DDL: &str = "
CREATE TABLE IF NOT EXISTS audit_log (
    event_id TEXT PRIMARY KEY,
    timestamp REAL NOT NULL,
    event_type TEXT NOT NULL,
    actor_uid INTEGER NOT NULL,
    actor_role TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT DEFAULT '',
    result TEXT NOT NULL,
    details TEXT DEFAULT '{}',
    client_ip TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_timestamp ON audit_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_audit_type ON audit_log(event_type);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor_uid);
CREATE INDEX IF NOT EXISTS idx_audit_result ON audit_log(result);
";

/// `mcp.audit_log.get_conn` —— 返回 daemon 权威审计日志 DB 路径（authority）。
///
/// Python `AuditLogger` 经本 RPC 取路径用于归属/校验；daemon 不泄露连接对象。
pub fn handle_get_conn(db_path: &Path) -> Result<Value, DaemonRpcError> {
    let p = db_path.to_str().unwrap_or("").to_string();
    if p.is_empty() {
        // fail-closed：audit_db_path 未配置时返回稳定错误码，绝不回退本地 SQLite。
        return Err(DaemonRpcError::new(
            "audit_db_unconfigured",
            "审计日志 DB 路径未配置（daemon 未配置 audit_db_path，fail-closed）",
        ));
    }
    let mut m = Map::new();
    m.insert("db_path".into(), Value::String(p));
    Ok(Value::Object(m))
}

/// `mcp.audit_log.init_db` —— 在 daemon 权威 audit.db 上建 `audit_log` 表 + 索引。
pub fn handle_init_db(conn: &Connection) -> Result<Value, DaemonRpcError> {
    conn.execute_batch(AUDIT_LOG_DDL)
        .map_err(|e| DaemonRpcError::internal_error(format!("audit init_db DDL 失败: {e}")))?;
    Ok(json!({"ok": true}))
}

/// 从 params 取出 audit event 对象（调用方已确保为 object）。
fn extract_event(params: &Value) -> Result<Map<String, Value>, DaemonRpcError> {
    params
        .get("event")
        .and_then(|v| v.as_object())
        .cloned()
        .ok_or_else(|| DaemonRpcError::invalid_params("缺少 audit event 对象（params.event）"))
}

/// `mcp.audit_log.append` —— 写入单条审计事件。
pub fn handle_append(conn: &Connection, params: &Value) -> Result<Value, DaemonRpcError> {
    let ev = extract_event(params);
    let ev = match ev {
        Ok(e) => e,
        Err(e) => return Err(e),
    };
    let event_id = get_str_param_or_val(&ev, "event_id", "");
    let timestamp = ev
        .get("timestamp")
        .and_then(|v| v.as_f64())
        .unwrap_or(0.0);
    let event_type = get_str_param_or_val(&ev, "event_type", "");
    let actor_uid = ev.get("actor_uid").and_then(|v| v.as_i64()).unwrap_or(0);
    let actor_role = get_str_param_or_val(&ev, "actor_role", "");
    let action = get_str_param_or_val(&ev, "action", "");
    let target = get_str_param_or_val(&ev, "target", "");
    let result = get_str_param_or_val(&ev, "result", "");
    let client_ip = get_str_param_or_val(&ev, "client_ip", "");
    let details = ev
        .get("details")
        .map(|d| serde_json::to_string(d).unwrap_or_else(|_| "{}".to_string()))
        .unwrap_or_else(|| "{}".to_string());

    if event_id.is_empty() || event_type.is_empty() || action.is_empty() {
        return Err(DaemonRpcError::invalid_params(
            "audit event 缺必填字段（event_id/event_type/action）",
        ));
    }

    conn.execute(
        "INSERT OR REPLACE INTO audit_log \
         (event_id, timestamp, event_type, actor_uid, actor_role, action, target, result, details, client_ip) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)",
        rusqlite::params![
            event_id,
            timestamp,
            event_type,
            actor_uid,
            actor_role,
            action,
            target,
            result,
            details,
            client_ip
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("audit append 失败: {e}")))?;

    Ok(json!({"ok": true, "event_id": event_id}))
}

/// `mcp.audit_log.query` —— 按时间/类型/UID/结果过滤审计日志（倒序）。
pub fn handle_query(conn: &Connection, params: &Value) -> Result<Value, DaemonRpcError> {
    let start_time = params.get("start_time").and_then(|v| v.as_f64());
    let end_time = params.get("end_time").and_then(|v| v.as_f64());
    let event_type = get_str_param_or(params, "event_type", "");
    let actor_uid = params.get("actor_uid").and_then(|v| v.as_i64());
    let result = get_str_param_or(params, "result", "");
    let limit = get_int_param_or(params, "limit", 100);
    let offset = get_int_param_or(params, "offset", 0);

    let mut conditions: Vec<String> = Vec::new();
    let mut bind: Vec<rusqlite::types::Value> = Vec::new();
    if let Some(t) = start_time {
        conditions.push("timestamp >= ?".to_string());
        bind.push(rusqlite::types::Value::Real(t));
    }
    if let Some(t) = end_time {
        conditions.push("timestamp < ?".to_string());
        bind.push(rusqlite::types::Value::Real(t));
    }
    if !event_type.is_empty() {
        conditions.push("event_type = ?".to_string());
        bind.push(rusqlite::types::Value::Text(event_type));
    }
    if let Some(u) = actor_uid {
        conditions.push("actor_uid = ?".to_string());
        bind.push(rusqlite::types::Value::Integer(u));
    }
    if !result.is_empty() {
        conditions.push("result = ?".to_string());
        bind.push(rusqlite::types::Value::Text(result));
    }
    let where_clause = if conditions.is_empty() {
        "1=1".to_string()
    } else {
        conditions.join(" AND ")
    };
    let sql = format!(
        "SELECT * FROM audit_log WHERE {where_clause} ORDER BY timestamp DESC LIMIT ? OFFSET ?"
    );
    bind.push(rusqlite::types::Value::Integer(limit));
    bind.push(rusqlite::types::Value::Integer(offset));

    let params_refs: Vec<&rusqlite::types::Value> = bind.iter().collect();

    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("audit query prepare 失败: {e}")))?;
    let rows = stmt
        .query_map(rusqlite::params_from_iter(params_refs), |row| {
            let details_raw: String = row.get("details").unwrap_or_else(|_| "{}".to_string());
            let details: Value = serde_json::from_str(&details_raw).unwrap_or(Value::Object(Map::new()));
            Ok(json!({
                "event_id": row.get::<_, String>("event_id")?,
                "timestamp": row.get::<_, f64>("timestamp")?,
                "event_type": row.get::<_, String>("event_type")?,
                "actor_uid": row.get::<_, i64>("actor_uid")?,
                "actor_role": row.get::<_, String>("actor_role")?,
                "action": row.get::<_, String>("action")?,
                "target": row.get::<_, String>("target").unwrap_or_default(),
                "result": row.get::<_, String>("result")?,
                "details": details,
                "client_ip": row.get::<_, String>("client_ip").unwrap_or_default(),
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("audit query 失败: {e}")))?;

    let mut out: Vec<Value> = Vec::new();
    for r in rows {
        match r {
            Ok(v) => out.push(v),
            Err(e) => return Err(DaemonRpcError::internal_error(format!("audit query row 失败: {e}"))),
        }
    }
    Ok(Value::Array(out))
}

/// `mcp.audit_log.count` —— 统计审计日志条数（同过滤条件）。
pub fn handle_count(conn: &Connection, params: &Value) -> Result<Value, DaemonRpcError> {
    let start_time = params.get("start_time").and_then(|v| v.as_f64());
    let end_time = params.get("end_time").and_then(|v| v.as_f64());
    let event_type = get_str_param_or(params, "event_type", "");
    let actor_uid = params.get("actor_uid").and_then(|v| v.as_i64());
    let result = get_str_param_or(params, "result", "");

    let mut conditions: Vec<String> = Vec::new();
    let mut bind: Vec<rusqlite::types::Value> = Vec::new();
    if let Some(t) = start_time {
        conditions.push("timestamp >= ?".to_string());
        bind.push(rusqlite::types::Value::Real(t));
    }
    if let Some(t) = end_time {
        conditions.push("timestamp < ?".to_string());
        bind.push(rusqlite::types::Value::Real(t));
    }
    if !event_type.is_empty() {
        conditions.push("event_type = ?".to_string());
        bind.push(rusqlite::types::Value::Text(event_type));
    }
    if let Some(u) = actor_uid {
        conditions.push("actor_uid = ?".to_string());
        bind.push(rusqlite::types::Value::Integer(u));
    }
    if !result.is_empty() {
        conditions.push("result = ?".to_string());
        bind.push(rusqlite::types::Value::Text(result));
    }
    let where_clause = if conditions.is_empty() {
        "1=1".to_string()
    } else {
        conditions.join(" AND ")
    };
    let sql = format!("SELECT COUNT(*) as count FROM audit_log WHERE {where_clause}");
    let params_refs: Vec<&rusqlite::types::Value> = bind.iter().collect();

    let count: i64 = conn
        .query_row(&sql, rusqlite::params_from_iter(params_refs), |row| row.get("count"))
        .map_err(|e| DaemonRpcError::internal_error(format!("audit count 失败: {e}")))?;
    Ok(json!({"count": count}))
}

/// `mcp.audit_log.clear` —— 清空所有审计日志（仅测试/运维用）。
pub fn handle_clear(conn: &Connection) -> Result<Value, DaemonRpcError> {
    conn.execute("DELETE FROM audit_log", [])
        .map_err(|e| DaemonRpcError::internal_error(format!("audit clear 失败: {e}")))?;
    Ok(json!({"ok": true}))
}

/// `mcp.audit_log.get_stats` —— 审计日志统计（total / by_type / by_result）。
pub fn handle_get_stats(conn: &Connection) -> Result<Value, DaemonRpcError> {
    let total: i64 = conn
        .query_row("SELECT COUNT(*) as count FROM audit_log", [], |row| row.get("count"))
        .map_err(|e| DaemonRpcError::internal_error(format!("audit get_stats total 失败: {e}")))?;

    let mut by_type: Map<String, Value> = Map::new();
    let mut stmt = conn
        .prepare("SELECT event_type, COUNT(*) as count FROM audit_log GROUP BY event_type")
        .map_err(|e| DaemonRpcError::internal_error(format!("audit get_stats by_type 失败: {e}")))?;
    let rows = stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>("event_type")?, row.get::<_, i64>("count")?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("audit get_stats by_type 失败: {e}")))?;
    for r in rows {
        if let Ok((k, v)) = r {
            by_type.insert(k, json!(v));
        }
    }

    let mut by_result: Map<String, Value> = Map::new();
    let mut stmt = conn
        .prepare("SELECT result, COUNT(*) as count FROM audit_log GROUP BY result")
        .map_err(|e| DaemonRpcError::internal_error(format!("audit get_stats by_result 失败: {e}")))?;
    let rows = stmt
        .query_map([], |row| {
            Ok((row.get::<_, String>("result")?, row.get::<_, i64>("count")?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("audit get_stats by_result 失败: {e}")))?;
    for r in rows {
        if let Ok((k, v)) = r {
            by_result.insert(k, json!(v));
        }
    }

    let mut m = Map::new();
    m.insert("total".into(), json!(total));
    m.insert("by_type".into(), Value::Object(by_type));
    m.insert("by_result".into(), Value::Object(by_result));
    m.insert("buffer_size".into(), json!(0));
    Ok(Value::Object(m))
}

/// 从 event object 取字符串字段（缺失返回默认）。
fn get_str_param_or_val(ev: &Map<String, Value>, key: &str, default: &str) -> String {
    ev.get(key)
        .and_then(|v| v.as_str())
        .unwrap_or(default)
        .to_string()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn with_conn() -> Connection {
        let conn = Connection::open(":memory:").unwrap();
        handle_init_db(&conn).unwrap();
        conn
    }

    #[test]
    fn test_get_conn_contract() {
        // 默认路径非空
        let r = handle_get_conn(Path::new("/var/log/callwarden/audit.log")).unwrap();
        assert!(!r["db_path"].as_str().unwrap_or("").is_empty());
        // 空路径 fail-closed
        let e = handle_get_conn(Path::new("")).unwrap_err();
        assert_eq!(e.code, "audit_db_unconfigured");
    }

    #[test]
    fn test_init_append_query_count_clear_stats() {
        let conn = with_conn();
        let ev = json!({
            "event_id": "A-1",
            "timestamp": 100.0,
            "event_type": "admin_operation",
            "actor_uid": 1000,
            "actor_role": "admin",
            "action": "register",
            "target": "workspace:ws-1",
            "result": "success",
            "details": {"k": "v"},
            "client_ip": "127.0.0.1"
        });
        handle_append(&conn, &json!({"event": ev})).unwrap();
        let q = handle_query(&conn, &json!({})).unwrap();
        assert_eq!(q.as_array().unwrap().len(), 1);
        assert_eq!(q[0]["details"]["k"], json!("v"));
        let c = handle_count(&conn, &json!({})).unwrap();
        assert_eq!(c["count"], json!(1));
        let s = handle_get_stats(&conn).unwrap();
        assert_eq!(s["total"], json!(1));
        handle_clear(&conn).unwrap();
        let c2 = handle_count(&conn, &json!({})).unwrap();
        assert_eq!(c2["count"], json!(0));
    }

    #[test]
    fn test_append_invalid_params() {
        let conn = with_conn();
        let e = handle_append(&conn, &json!({"event": {"event_id": "X"}})).unwrap_err();
        assert_eq!(e.code, "invalid_params");
    }
}
