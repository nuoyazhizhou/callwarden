//! `Experiment_Batch_Config` 到 daemon Stage_Toggle 存储的迁移 authority。
//!
//! Python 端只负责读取旧配置文件和序列化请求；schema、幂等写入和审计
//! 记录全部在 daemon 内完成。迁移使用 P0 的原始 `scope_key`，不会把值
//! 重置为默认关闭。

use rusqlite::{params, Connection, OptionalExtension};
use serde_json::{json, Value};

use super::{get_str_param, DaemonRpcError};

const STAGE_TOGGLE_SCHEMA: &str = r#"
CREATE TABLE IF NOT EXISTS stage_toggles (
    stage       TEXT NOT NULL,
    scope_key   TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 0,
    actor       TEXT NOT NULL DEFAULT '',
    changed_at  INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (stage, scope_key)
);
CREATE TABLE IF NOT EXISTS toggle_audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stage       TEXT NOT NULL,
    scope_key   TEXT NOT NULL,
    old_value   INTEGER,
    new_value   INTEGER NOT NULL,
    actor       TEXT NOT NULL,
    changed_at  INTEGER NOT NULL
);
"#;

fn required_db_path(params: &Value) -> Result<&str, DaemonRpcError> {
    let path = get_str_param(params, "db_path")
        .ok_or_else(|| DaemonRpcError::invalid_params("缺少字段: db_path"))?;
    if path.trim().is_empty() {
        return Err(DaemonRpcError::invalid_params("db_path 不能为空"));
    }
    Ok(path)
}

fn open_store(params: &Value) -> Result<Connection, DaemonRpcError> {
    let path = required_db_path(params)?;
    Connection::open(path)
        .map_err(|e| DaemonRpcError::internal_error(format!("打开 Stage_Toggle 存储失败: {}", e)))
}

fn migration_actor(params: &Value) -> Result<String, DaemonRpcError> {
    let actor = get_str_param(params, "migration_actor")
        .unwrap_or("stage_toggle_migration")
        .trim();
    if actor.is_empty() {
        return Err(DaemonRpcError::invalid_params("migration_actor 不能为空"));
    }
    Ok(actor.to_string())
}

fn migration_toggles(params: &Value) -> Result<&[Value], DaemonRpcError> {
    params
        .get("toggles")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .ok_or_else(|| DaemonRpcError::invalid_params("toggles 必须是数组"))
}

fn toggle_fields(toggle: &Value) -> Result<(&str, bool), DaemonRpcError> {
    let scope_key = toggle
        .get("scope_key")
        .and_then(Value::as_str)
        .ok_or_else(|| DaemonRpcError::invalid_params("toggle 缺少字符串 scope_key"))?;
    if scope_key.trim().is_empty() {
        return Err(DaemonRpcError::invalid_params("scope_key 不能为空"));
    }
    let enabled = toggle
        .get("enabled")
        .and_then(Value::as_bool)
        .ok_or_else(|| DaemonRpcError::invalid_params("toggle 缺少布尔 enabled"))?;
    Ok((scope_key, enabled))
}

/// 将旧 P0 配置按原作用域幂等迁入 daemon 配置存储。
pub fn handle_migrate_p0_toggles(params: &Value) -> Result<Value, DaemonRpcError> {
    let toggles = migration_toggles(params)?;
    let actor = migration_actor(params)?;
    let dry_run = params
        .get("dry_run")
        .and_then(Value::as_bool)
        .unwrap_or(false);

    // dry-run 也在 daemon 侧校验完整载荷，但不打开或创建 authority DB。
    if dry_run {
        for toggle in toggles {
            let _ = toggle_fields(toggle)?;
        }
        return Ok(json!({
            "migrated_count": toggles.len(),
            "dry_run": true,
            "source": "rust",
        }));
    }

    let conn = open_store(params)?;
    conn.execute_batch(STAGE_TOGGLE_SCHEMA)
        .map_err(|e| DaemonRpcError::internal_error(format!("初始化 Stage_Toggle schema 失败: {}", e)))?;

    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_err(|e| DaemonRpcError::internal_error(format!("读取 daemon 时钟失败: {}", e)))?
        .as_millis() as i64;
    let tx = conn
        .unchecked_transaction()
        .map_err(|e| DaemonRpcError::internal_error(format!("开启迁移事务失败: {}", e)))?;
    let mut count = 0usize;

    for toggle in toggles {
        let (scope_key, enabled) = toggle_fields(toggle)?;
        let existing: Option<i32> = tx
            .query_row(
                "SELECT enabled FROM stage_toggles WHERE stage = 'P0' AND scope_key = ?1",
                params![scope_key],
                |row: &rusqlite::Row<'_>| row.get(0),
            )
            .optional()
            .map_err(|e| DaemonRpcError::internal_error(format!("查询既有 Stage_Toggle 失败: {}", e)))?;
        if existing.is_some() {
            continue;
        }

        tx.execute(
            "INSERT INTO stage_toggles (stage, scope_key, enabled, actor, changed_at)
             VALUES ('P0', ?1, ?2, ?3, ?4)",
            params![scope_key, enabled as i32, actor, now_ms],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("写入 P0 Stage_Toggle 失败: {}", e)))?;
        tx.execute(
            "INSERT INTO toggle_audit_log (stage, scope_key, old_value, new_value, actor, changed_at)
             VALUES ('P0', ?1, NULL, ?2, ?3, ?4)",
            params![scope_key, enabled as i32, actor, now_ms],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("写入 Stage_Toggle 审计失败: {}", e)))?;
        count += 1;
    }

    tx.commit()
        .map_err(|e| DaemonRpcError::internal_error(format!("提交 Stage_Toggle 迁移失败: {}", e)))?;
    Ok(json!({
        "migrated_count": count,
        "dry_run": false,
        "source": "rust",
    }))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    use std::path::PathBuf;

    fn temp_db(name: &str) -> String {
        let mut path = std::env::temp_dir();
        path.push(format!("callwarden-srv017-{}-{}.db", name, std::process::id()));
        let _ = std::fs::remove_file(&path);
        path.to_string_lossy().to_string()
    }

    #[test]
    fn migration_preserves_scopes_and_is_idempotent() {
        let path = temp_db("idempotent");
        let params = json!({
            "db_path": path,
            "toggles": [
                {"scope_key": "global", "enabled": true},
                {"scope_key": "workspace:ws-1", "enabled": false},
                {"scope_key": "task:t-1", "enabled": true}
            ],
            "migration_actor": "legacy-migration"
        });
        let first = handle_migrate_p0_toggles(&params).unwrap();
        assert_eq!(first["migrated_count"], 3);
        let second = handle_migrate_p0_toggles(&params).unwrap();
        assert_eq!(second["migrated_count"], 0);

        let conn = Connection::open(&params["db_path"].as_str().unwrap()).unwrap();
        let rows: Vec<(String, i32)> = conn
            .prepare("SELECT scope_key, enabled FROM stage_toggles ORDER BY scope_key")
            .unwrap()
            .query_map([], |row| Ok((row.get(0)?, row.get(1)?)))
            .unwrap()
            .map(|row| row.unwrap())
            .collect();
        assert_eq!(rows, vec![("global".into(), 1), ("task:t-1".into(), 1), ("workspace:ws-1".into(), 0)]);
        let audit_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM toggle_audit_log", [], |row| row.get(0))
            .unwrap();
        assert_eq!(audit_count, 3);
        let _ = std::fs::remove_file(PathBuf::from(params["db_path"].as_str().unwrap()));
    }

    #[test]
    fn dry_run_validates_without_creating_db() {
        let path = temp_db("dry-run");
        let result = handle_migrate_p0_toggles(&json!({
            "db_path": path,
            "dry_run": true,
            "toggles": [{"scope_key": "global", "enabled": true}]
        }))
        .unwrap();
        assert_eq!(result["migrated_count"], 1);
        assert!(!PathBuf::from(&path).exists());
    }

    #[test]
    fn invalid_payload_has_stable_error() {
        let error = handle_migrate_p0_toggles(&json!({"db_path": "/tmp/srv017.db", "toggles": [{}]}))
            .unwrap_err();
        assert_eq!(error.code, "invalid_params");
    }
}
