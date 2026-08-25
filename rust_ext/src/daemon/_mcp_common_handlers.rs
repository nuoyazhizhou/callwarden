//! MCP common 面 handler（SRV-001：server mcp common Python authority → Rust daemon）。
//!
//! 对应 `tool_migration_matrix.json` 中 `server/_mcp_common.py::_get_db_path_for_daemon`
//! 的 Rust 下沉：返回 daemon 权威任务库路径（对齐 Python `config.py:DB_PATH` /
//! `get_project_db_path`，即 `~/.callwarden/callwarden.db`）。
//!
//! 不变量：
//! - 数据源：daemon 配置（`config::default_authority_task_db_path()`），不打开 SQLite；
//! - 身份控制：传输层已保证 peer 身份（SO_PEERCRED / 命名管道 SID）；可选
//!   `workspace_instance_id` 仅用于响应归因，不改变返回路径（权威路径为全局）；
//! - fail-closed：主目录（HOME/USERPROFILE）缺失时返回
//!   `authority_task_db_unconfigured` 错误，绝不回退本地 SQLite。

use serde_json::{json, Map, Value};

use super::config::default_authority_task_db_path;
use super::dispatch::{get_str_param_or, DaemonRpcError};

/// `mcp.common.get_db_path_for_daemon` —— 返回 daemon 权威任务库路径。
///
/// Python `server/_mcp_common.py::_get_db_path_for_daemon` 经本 RPC 取路径（fail-closed），
/// 不再调用 `get_db()` / `get_project_db_path` 计算本地 SQLite 路径。
pub fn handle_get_db_path_for_daemon(params: &Value) -> Result<Value, DaemonRpcError> {
    // 可选归因：workspace_instance_id 仅回显，不改写权威路径（路径为全局）。
    let workspace_instance_id = get_str_param_or(params, "workspace_instance_id", "");

    let path = default_authority_task_db_path();
    let db_path = path.to_str().unwrap_or("").to_string();
    if db_path.is_empty() {
        // fail-closed：HOME/USERPROFILE 缺失时返回稳定错误码，绝不回退本地 SQLite。
        return Err(DaemonRpcError::new(
            "authority_task_db_unconfigured",
            "权威任务库路径未配置（HOME/USERPROFILE 缺失，fail-closed）",
        ));
    }

    let mut m = Map::new();
    m.insert("db_path".into(), Value::String(db_path));
    if !workspace_instance_id.is_empty() {
        m.insert(
            "workspace_instance_id".into(),
            Value::String(workspace_instance_id),
        );
    }
    Ok(Value::Object(m))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_handler_contract() {
        // 有 HOME/USERPROFILE 时返回非空 db_path；缺失时返回 Err（fail-closed）。
        let r = handle_get_db_path_for_daemon(&json!({}));
        match r {
            Ok(v) => assert!(!v["db_path"].as_str().unwrap_or("").is_empty()),
            Err(e) => assert_eq!(e.code, "authority_task_db_unconfigured"),
        }
    }

    #[test]
    fn test_echoes_workspace_instance_id() {
        let r = handle_get_db_path_for_daemon(&json!({"workspace_instance_id": "ws-123"}))
            .unwrap();
        assert_eq!(r["workspace_instance_id"].as_str(), Some("ws-123"));
    }
}
