//! CLI-054（T-1787322797796-6da4f0b4）：`cw rule cleanup-sync-log` 专用 handler。
//!
//! 复刻 Python `db/db_agent_rules.py::cleanup_sync_log` 的完整语义：
//! 清理 `agent_rule_sync_log` 旧记录，同时满足两个条件才删除：
//!   1. created_at 早于 older_than_days 天前（时间过滤）；
//!   2. 不在最近 keep_latest 条记录内（按 created_at 倒序的保留阈值）。
//! 支持 dry_run（只预估不删除），dry_run=False 才真正执行 DELETE。
//!
//! 旧 RPC `admin.cleanup_rule_sync_log` 只支持 `before_ts` 且忽略 dry_run，
//! 会导致 CLI 传 dry_run=True 时实际删除全部旧记录——本 handler 修复该语义，
//! 保留 `before_ts` 兼容参数（显式传入 before_ts 且未传 older_than_days 时按旧行为）。

use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Value};

use super::dispatch::{get_int_param_or, DaemonRpcError};

fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 解析 bool 参数：JSON bool 直取；字符串 "true"/"1" 视为 true。
fn get_bool_param_or(params: &Value, key: &str, default: bool) -> bool {
    match params.get(key) {
        Some(v) if v.is_boolean() => v.as_bool().unwrap_or(default),
        Some(v) if v.is_string() => match v.as_str() {
            Some("true") | Some("1") => true,
            _ => default,
        },
        _ => default,
    }
}

/// 解析 f64 参数（before_ts 时间戳可能为浮点）：JSON 数字直取；字符串解析。
fn get_f64_param_or(params: &Value, key: &str, default: f64) -> f64 {
    match params.get(key) {
        Some(v) if v.is_number() => v.as_f64().unwrap_or(default),
        Some(v) if v.is_string() => v
            .as_str()
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(default),
        _ => default,
    }
}

/// `admin.cleanup_rule_sync_log`（CLI-054 完整语义版）——
/// 清理 `agent_rule_sync_log` 旧记录（C6 GC，dry-run 默认开启）。
pub fn handle_cleanup_sync_log(
    conn: &Connection,
    _workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let older_than_days = get_int_param_or(params, "older_than_days", 90);
    let keep_latest = get_int_param_or(params, "keep_latest", 100);
    let dry_run = get_bool_param_or(params, "dry_run", true);
    let before_ts = get_f64_param_or(params, "before_ts", 0.0);

    let now = now_ts();
    let cutoff_ts = now - older_than_days as f64 * 86400.0;

    let err = |e: rusqlite::Error| DaemonRpcError::internal_error(format!("cleanup_sync_log: {e}"));

    // 清理前总记录数
    let cur_total: i64 = conn
        .query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |r| r.get(0))
        .map_err(err)?;

    // 构造删除条件：双重过滤（时间 + 保留阈值）；显式 before_ts 且未传
    // older_than_days 时按旧行为（仅时间过滤，且默认 now）。
    let (sql_where, params_bind): (String, Vec<f64>) = if before_ts > 0.0
        && !params.get("older_than_days").is_some()
    {
        (String::from("WHERE created_at < ?1"), vec![before_ts])
    } else if cur_total <= keep_latest {
        // 总记录数 <= keep_latest，仅按时间过滤
        (String::from("WHERE created_at < ?1"), vec![cutoff_ts])
    } else {
        // 找到第 keep_latest 条（按 created_at DESC）的 created_at 作为下限
        let keep_threshold: Option<f64> = conn
            .query_row(
                "SELECT created_at FROM agent_rule_sync_log ORDER BY created_at DESC LIMIT 1 OFFSET ?1",
                rusqlite::params![keep_latest - 1],
                |r| r.get(0),
            )
            .optional()
            .map_err(err)?;
        let keep_threshold = keep_threshold.unwrap_or(now);
        (String::from("WHERE created_at < ?1 AND created_at < ?2"), vec![cutoff_ts, keep_threshold])
    };

    let deleted: i64 = if dry_run {
        // 预演：用 SELECT COUNT 估算将删除的记录数
        let sql = format!("SELECT COUNT(*) FROM agent_rule_sync_log {sql_where}");
        conn.query_row(&sql, rusqlite::params_from_iter(params_bind.iter().copied()), |r| r.get(0))
            .map_err(err)?
    } else {
        // 执行删除
        let sql = format!("DELETE FROM agent_rule_sync_log {sql_where}");
        let n = conn
            .execute(&sql, rusqlite::params_from_iter(params_bind.iter().copied()))
            .map_err(err)?;
        n as i64
    };

    let remaining: i64 = conn
        .query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |r| r.get(0))
        .map_err(err)?;

    Ok(json!({
        "success": true,
        "dry_run": dry_run,
        "deleted_count": deleted,
        "remaining_count": remaining,
        "total_before": cur_total,
        "older_than_days": older_than_days,
        "keep_latest": keep_latest,
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn temp_db() -> (tempfile::TempDir, Connection) {
        let dir = tempfile::tempdir().unwrap();
        let conn = Connection::open(dir.path().join("t.db")).unwrap();
        conn.execute_batch(
            "CREATE TABLE agent_rule_sync_log (
                id INTEGER PRIMARY KEY,
                target_path TEXT,
                rule_ids_json TEXT,
                before_hash TEXT,
                after_hash TEXT,
                dry_run INTEGER,
                created_at REAL,
                actor TEXT
            );",
        )
        .unwrap();
        (dir, conn)
    }

    fn seed(conn: &Connection, ts_count: &[(f64, i64)]) {
        for (ts, count) in ts_count {
            for _ in 0..*count {
                conn.execute(
                    "INSERT INTO agent_rule_sync_log
                     (target_path, rule_ids_json, before_hash, after_hash, dry_run, created_at, actor)
                     VALUES ('p', '[]', 'b', 'a', 0, ?1, 't')",
                    rusqlite::params![ts],
                )
                .unwrap();
            }
        }
    }

    #[test]
    fn dry_run_never_deletes_and_reports_estimate() {
        let (_dir, conn) = temp_db();
        let now = now_ts();
        // 100 条旧记录（30 天前）+ 10 条新记录（1 天前）
        seed(&conn, &[(now - 30.0 * 86400.0, 100), (now - 1.0 * 86400.0, 10)]);
        let total: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(total, 110);

        let params = json!({"older_than_days": 7, "keep_latest": 200, "dry_run": true});
        let res = handle_cleanup_sync_log(&conn, 1, &params).unwrap();
        assert_eq!(res["success"], true);
        assert_eq!(res["dry_run"], true);
        // dry_run 不删除任何行
        let after: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(after, 110);
        // 预估值 = 30 天前的 100 条（总 110 <= keep_latest 200，仅时间过滤）
        assert_eq!(res["deleted_count"].as_i64().unwrap(), 100);
        assert_eq!(res["remaining_count"].as_i64().unwrap(), 110);
        assert_eq!(res["total_before"].as_i64().unwrap(), 110);
    }

    #[test]
    fn apply_deletes_only_old_beyond_keep_latest() {
        let (_dir, conn) = temp_db();
        let now = now_ts();
        // 总 120 条 > keep_latest 20：10 条 1 天前 + 10 条 10 天前 + 100 条 40 天前。
        // keep_latest=20 → 保留最近 20 条（1 天前 10 条 + 10 天前 10 条），
        // 40 天前的 100 条同时满足时间与保留阈值 → 删除。
        seed(
            &conn,
            &[
                (now - 1.0 * 86400.0, 10),
                (now - 10.0 * 86400.0, 10),
                (now - 40.0 * 86400.0, 100),
            ],
        );
        let params = json!({"older_than_days": 7, "keep_latest": 20, "dry_run": false});
        let res = handle_cleanup_sync_log(&conn, 1, &params).unwrap();
        assert_eq!(res["success"], true);
        assert_eq!(res["dry_run"], false);
        assert_eq!(res["deleted_count"].as_i64().unwrap(), 100);
        let after: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(after, 20);
        assert_eq!(res["remaining_count"].as_i64().unwrap(), 20);
        assert_eq!(res["total_before"].as_i64().unwrap(), 120);
    }

    #[test]
    fn total_below_keep_latest_only_time_filter() {
        let (_dir, conn) = temp_db();
        let now = now_ts();
        // 总记录 20 条（<= keep_latest 30），其中 8 条旧（30 天前）、12 条新（1 天前）
        seed(&conn, &[(now - 30.0 * 86400.0, 8), (now - 1.0 * 86400.0, 12)]);
        let params = json!({"older_than_days": 7, "keep_latest": 30, "dry_run": false});
        let res = handle_cleanup_sync_log(&conn, 1, &params).unwrap();
        assert_eq!(res["success"], true);
        assert_eq!(res["deleted_count"].as_i64().unwrap(), 8);
        let after: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(after, 12);
    }

    #[test]
    fn before_ts_compat_legacy_semantics() {
        let (_dir, conn) = temp_db();
        let now = now_ts();
        seed(&conn, &[(now - 30.0 * 86400.0, 5), (now - 1.0 * 86400.0, 5)]);
        // 显式 before_ts 且未传 older_than_days → 旧行为（仅时间过滤）
        let params = json!({"before_ts": now - 5.0 * 86400.0, "dry_run": false});
        let res = handle_cleanup_sync_log(&conn, 1, &params).unwrap();
        assert_eq!(res["success"], true);
        // 30 天前的 5 条旧记录（< 5 天前）删除，1 天前的 5 条新记录保留
        assert_eq!(res["deleted_count"].as_i64().unwrap(), 5);
        let after: i64 = conn
            .query_row("SELECT COUNT(*) FROM agent_rule_sync_log", [], |r| r.get(0))
            .unwrap();
        assert_eq!(after, 5);
    }
}
