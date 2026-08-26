//! CLI admin 面 handler（SRV-004：server cli admin Python authority → Rust daemon）。
//!
//! 对应 `server/cli_admin.py` 中仍直接 open SQLite 的五个 Python authority 函数：
//! - `connection_test`：连续打开只读连接执行 `SELECT 1` 的连通性测试；
//! - `open_readonly_conn`：只读连接可用性探测（RPC 无法传递连接对象，
//!   下沉为「打开只读连接 + SELECT 1 + 立即关闭」的探测语义）；
//! - `read_pragmas`：静态 PRAGMA 白名单读取（journal_mode/synchronous/...）；
//! - `read_task_dependencies`：任务/契约依赖、产物、接口只读查询；
//! - `scan_hash_databases`：扫描旧版 16 位 hex hash 数据库目录的 workspaces 表。
//!
//! 下沉后 Python `server/cli_admin.py` 仅作 daemon RPC 薄客户端，不再 `import sqlite3`、
//! 不再 open 本地 DB、不再执行业务 SQL。全部 handler 只读（mode=ro），
//! 不持有写锁、不触发 WAL 写入；错误语义与 Python 对齐：
//! 库不可打开/查询失败返回稳定空值（空串/空列表/error 字段），参数缺失返回
//! `invalid_params`（stable errors）。

#[allow(unused_imports)] // Path 由单测（tmp_db）使用
use std::path::Path;
use std::path::PathBuf;

use rusqlite::Connection;
use serde_json::{json, Value};

use super::dispatch::{get_int_param, get_str_param_or, DaemonRpcError};

/// 只读连接打开（mode=ro，busy 3s，对齐 Python `timeout=3`）。
fn open_readonly(db_path: &str) -> Result<Connection, rusqlite::Error> {
    let uri = format!("file:{}?mode=ro", db_path);
    let conn = Connection::open_with_flags(
        &uri,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
    )?;
    conn.busy_timeout(std::time::Duration::from_secs(3))?;
    Ok(conn)
}

/// 默认 `~/.callwarden` 目录（对齐 Python `config.CALLWARDEN_DIR`）。
fn default_callwarden_dir() -> PathBuf {
    let home = std::env::var("CALLWARDEN_HOME")
        .ok()
        .filter(|v| !v.is_empty())
        .or_else(|| std::env::var("USERPROFILE").ok())
        .or_else(|| std::env::var("HOME").ok())
        .unwrap_or_default();
    PathBuf::from(home).join(".callwarden")
}

/// 默认用户级单库路径（对齐 Python `get_default_db_path`：
/// `CALLWARDEN_DB` 环境变量 → `~/.callwarden/callwarden.db`）。
fn default_db_path() -> PathBuf {
    if let Ok(v) = std::env::var("CALLWARDEN_DB") {
        if !v.is_empty() {
            return PathBuf::from(v);
        }
    }
    default_callwarden_dir().join("callwarden.db")
}

/// 任意列 → JSON Value（rusqlite 未启用 serde_json feature，手写 ValueRef 转换；
/// 读取失败/越界统一 Null，对齐 Python 宽松读取语义）。
fn cell(row: &rusqlite::Row<'_>, idx: usize) -> Value {
    match row.get_ref(idx) {
        Ok(rusqlite::types::ValueRef::Null) => Value::Null,
        Ok(rusqlite::types::ValueRef::Integer(i)) => Value::Number(i.into()),
        Ok(rusqlite::types::ValueRef::Real(f)) => serde_json::Number::from_f64(f)
            .map(Value::Number)
            .unwrap_or(Value::Null),
        Ok(rusqlite::types::ValueRef::Text(t)) => {
            Value::String(String::from_utf8_lossy(t).into_owned())
        }
        Ok(rusqlite::types::ValueRef::Blob(b)) => Value::String(hex::encode(b)),
        Err(_) => Value::Null,
    }
}

/// `mcp.cli_admin.connection_test` —— 连续 rounds 次只读连接 + `SELECT 1`
/// （对齐 Python `connection_test`）。
///
/// 返回 `{"success": n, "fail": n}`；db_path 缺失 → `invalid_params`。
pub fn handle_connection_test(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = get_str_param_or(params, "db_path", "");
    if db_path.is_empty() {
        return Err(DaemonRpcError::invalid_params(
            "connection_test 需要 db_path",
        ));
    }
    let rounds = get_int_param(params, "rounds").unwrap_or(5).max(0) as usize;
    let mut success: i64 = 0;
    let mut fail: i64 = 0;
    for _ in 0..rounds {
        let ok = open_readonly(&db_path)
            .and_then(|conn| conn.query_row("SELECT 1", [], |_r| Ok(())))
            .is_ok();
        if ok {
            success += 1;
        } else {
            fail += 1;
        }
    }
    Ok(json!({"success": success, "fail": fail}))
}

/// `mcp.cli_admin.open_readonly_conn` —— 只读连接可用性探测
/// （对齐 Python `open_readonly_conn` 的 mode=ro 语义；RPC 不传递连接对象，
/// 下沉为「打开 + SELECT 1 + 关闭」探测）。
///
/// db_path 为空 → 默认用户级单库路径。
/// 返回 `{"db_path": s, "readonly": true, "openable": bool, "error": s|null}`。
pub fn handle_open_readonly_conn(params: &Value) -> Result<Value, DaemonRpcError> {
    let raw = get_str_param_or(params, "db_path", "");
    let db_path = if raw.is_empty() {
        default_db_path().to_string_lossy().to_string()
    } else {
        raw.to_string()
    };
    match open_readonly(&db_path).and_then(|conn| conn.query_row("SELECT 1", [], |_r| Ok(()))) {
        Ok(()) => Ok(json!({
            "db_path": db_path,
            "readonly": true,
            "openable": true,
            "error": Value::Null,
        })),
        Err(e) => Ok(json!({
            "db_path": db_path,
            "readonly": true,
            "openable": false,
            "error": e.to_string(),
        })),
    }
}

/// PRAGMA 静态白名单（对齐 Python `_PRAGMA_QUERIES`；SQLite 不支持绑定参数，
/// 静态分派避免字符串拼接）。
const PRAGMA_QUERIES: &[(&str, &str)] = &[
    ("journal_mode", "PRAGMA journal_mode"),
    ("synchronous", "PRAGMA synchronous"),
    ("busy_timeout", "PRAGMA busy_timeout"),
    ("cache_size", "PRAGMA cache_size"),
    ("mmap_size", "PRAGMA mmap_size"),
];

fn pragma_query(key: &str) -> Option<&'static str> {
    PRAGMA_QUERIES
        .iter()
        .find(|(k, _)| *k == key)
        .map(|(_, q)| *q)
}

/// `mcp.cli_admin.read_pragmas` —— 只读读取一组 PRAGMA 实际值
/// （对齐 Python `read_pragmas`：失败键/未知键返回空串，库不可打开全部空串）。
///
/// 返回 `{"pragmas": {key: value_str}}`；db_path/keys 缺失 → `invalid_params`。
pub fn handle_read_pragmas(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = get_str_param_or(params, "db_path", "");
    if db_path.is_empty() {
        return Err(DaemonRpcError::invalid_params(
            "read_pragmas 需要 db_path",
        ));
    }
    let keys: Vec<String> = params
        .get("keys")
        .and_then(|v| v.as_array())
        .ok_or_else(|| DaemonRpcError::invalid_params("read_pragmas 需要 keys 数组"))?
        .iter()
        .filter_map(|v| v.as_str().map(str::to_string))
        .collect();
    let mut pragmas = serde_json::Map::new();
    let conn = open_readonly(&db_path).ok();
    for key in &keys {
        let value = conn
            .as_ref()
            .and_then(|c| pragma_query(key).map(|q| (c, q)))
            .and_then(|(c, q)| c.query_row(q, [], |r| Ok(cell(r, 0))).ok())
            .map(|v| match v {
                Value::String(s) => s,
                other => other.to_string(),
            })
            .unwrap_or_default();
        pragmas.insert(key.clone(), Value::String(value));
    }
    Ok(json!({"pragmas": pragmas}))
}

/// contract 分支行映射（统一 7 列：revision>0 时 SELECT 无 contract_revision 列，补 null）。
/// 提取为模块函数以保证 revision 两分支 query_map 闭包同型。
fn dep_row_mapper(revision: i64) -> impl FnMut(&rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    move |r| {
        let has_revision_col = revision <= 0;
        Ok(json!({
            "dependency_type": cell(r, 0),
            "target_ref": cell(r, 1),
            "target_task_id": cell(r, 2),
            "is_informational": cell(r, 3),
            "task_id": cell(r, 4),
            "contract_revision": if has_revision_col { cell(r, 5) } else { Value::Null },
            "declared_at": cell(r, if has_revision_col { 6 } else { 5 }),
        }))
    }
}

/// `mcp.cli_admin.read_task_dependencies` —— 任务/契约依赖、产物、接口只读查询
/// （对齐 Python `read_task_dependencies`；任一查询失败该列表为空，
/// db 不可打开全部为空列表）。
///
/// task_id 与 contract_id 二选一，均缺失 → `invalid_params`。
pub fn handle_read_task_dependencies(params: &Value) -> Result<Value, DaemonRpcError> {
    let workspace_id = get_int_param(params, "workspace_id").ok_or_else(|| {
        DaemonRpcError::invalid_params("read_task_dependencies 需要 workspace_id")
    })?;
    let task_id = get_str_param_or(params, "task_id", "");
    let contract_id = get_str_param_or(params, "contract_id", "");
    let revision = get_int_param(params, "revision").unwrap_or(0);
    let raw = get_str_param_or(params, "db_path", "");
    let db_path = if raw.is_empty() {
        default_db_path().to_string_lossy().to_string()
    } else {
        raw.to_string()
    };
    if task_id.is_empty() && contract_id.is_empty() {
        return Err(DaemonRpcError::invalid_params(
            "read_task_dependencies 需要 task_id 或 contract_id",
        ));
    }

    let mut dependencies: Vec<Value> = Vec::new();
    let mut artifacts: Vec<Value> = Vec::new();
    let mut interfaces: Vec<Value> = Vec::new();

    if let Ok(conn) = open_readonly(&db_path) {
        if !task_id.is_empty() {
            if let Ok(mut stmt) = conn.prepare(
                "SELECT dependency_type, target_ref, target_task_id, is_informational, \
                 contract_id, contract_revision, declared_at \
                 FROM task_dependencies WHERE workspace_id = ?1 AND task_id = ?2",
            ) {
                if let Ok(rows) = stmt.query_map(rusqlite::params![workspace_id, task_id], |r| {
                    Ok(json!({
                        "dependency_type": cell(r, 0),
                        "target_ref": cell(r, 1),
                        "target_task_id": cell(r, 2),
                        "is_informational": cell(r, 3),
                        "contract_id": cell(r, 4),
                        "contract_revision": cell(r, 5),
                        "declared_at": cell(r, 6),
                    }))
                }) {
                    dependencies.extend(rows.filter_map(Result::ok));
                }
            }
            if let Ok(mut stmt) = conn.prepare(
                "SELECT artifact_id, artifact_type, artifact_ref, artifact_hash, \
                 freshness_status, produced_at \
                 FROM artifact_identities WHERE workspace_id = ?1 AND task_id = ?2",
            ) {
                if let Ok(rows) = stmt.query_map(rusqlite::params![workspace_id, task_id], |r| {
                    Ok(json!({
                        "artifact_id": cell(r, 0),
                        "artifact_type": cell(r, 1),
                        "artifact_ref": cell(r, 2),
                        "artifact_hash": cell(r, 3),
                        "freshness_status": cell(r, 4),
                        "produced_at": cell(r, 5),
                    }))
                }) {
                    artifacts.extend(rows.filter_map(Result::ok));
                }
            }
            if let Ok(mut stmt) = conn.prepare(
                "SELECT interface_id, interface_name, version, interface_hash \
                 FROM interface_identities WHERE workspace_id = ?1 AND provider_task_id = ?2",
            ) {
                if let Ok(rows) = stmt.query_map(rusqlite::params![workspace_id, task_id], |r| {
                    Ok(json!({
                        "interface_id": cell(r, 0),
                        "interface_name": cell(r, 1),
                        "version": cell(r, 2),
                        "interface_hash": cell(r, 3),
                    }))
                }) {
                    interfaces.extend(rows.filter_map(Result::ok));
                }
            }
        } else {
            let sql = if revision > 0 {
                "SELECT dependency_type, target_ref, target_task_id, is_informational, \
                 task_id, declared_at FROM task_dependencies \
                 WHERE workspace_id = ?1 AND contract_id = ?2 AND contract_revision = ?3"
            } else {
                "SELECT dependency_type, target_ref, target_task_id, is_informational, \
                 task_id, contract_revision, declared_at FROM task_dependencies \
                 WHERE workspace_id = ?1 AND contract_id = ?2"
            };
            if let Ok(mut stmt) = conn.prepare(sql) {
                // 先绑定 params（临时数组生命周期延长至语句块尾，避免 E0716）
                let mapped = if revision > 0 {
                    let binds = rusqlite::params![workspace_id, contract_id, revision];
                    stmt.query_map(binds, dep_row_mapper(revision))
                } else {
                    let binds = rusqlite::params![workspace_id, contract_id];
                    stmt.query_map(binds, dep_row_mapper(revision))
                };
                if let Ok(rows) = mapped {
                    dependencies.extend(rows.filter_map(Result::ok));
                }
            }
        }
    }

    Ok(json!({
        "dependencies": dependencies,
        "artifacts": artifacts,
        "interfaces": interfaces,
    }))
}

/// `mcp.cli_admin.scan_hash_databases` —— 扫描旧版 16 位 hex hash 数据库目录
/// （对齐 Python `scan_hash_databases`，供 `cw gc db-cleanup` 使用）。
///
/// callwarden_dir 为空 → 默认 `~/.callwarden`。目录不存在 → 空列表。
/// 每个 hash 目录一条记录；workspaces 读取失败时 `error` 非空（稳定错误语义）。
pub fn handle_scan_hash_databases(params: &Value) -> Result<Value, DaemonRpcError> {
    let raw = get_str_param_or(params, "callwarden_dir", "");
    let dir = if raw.is_empty() {
        default_callwarden_dir()
    } else {
        PathBuf::from(raw)
    };
    let mut results: Vec<Value> = Vec::new();
    let entries = match std::fs::read_dir(&dir) {
        Ok(entries) => entries,
        Err(_) => return Ok(json!({"databases": []})),
    };
    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .map(|e| e.file_name().to_string_lossy().to_string())
        .filter(|name| {
            name.len() == 16 && name.chars().all(|c| c.is_ascii_hexdigit() && !c.is_uppercase())
        })
        .collect();
    names.sort();
    for name in names {
        let dir_path = dir.join(&name);
        let db_file = dir_path.join("callwarden.db");
        if !db_file.is_file() {
            continue;
        }
        let mut entry = json!({
            "hash": name,
            "dir": dir_path.to_string_lossy(),
            "db_file": db_file.to_string_lossy(),
            "workspaces": [],
            "error": Value::Null,
        });
        match open_readonly(&db_file.to_string_lossy()) {
            Ok(conn) => {
                let mut workspaces: Vec<Value> = Vec::new();
                match conn.prepare("SELECT id, name, root_path FROM workspaces ORDER BY id") {
                    Ok(mut stmt) => {
                        if let Ok(rows) = stmt.query_map([], |r| {
                            Ok(json!({
                                "id": cell(r, 0),
                                "name": cell(r, 1)
                                    .as_str()
                                    .map(str::to_string)
                                    .unwrap_or_default(),
                                "root_path": cell(r, 2)
                                    .as_str()
                                    .map(str::to_string)
                                    .unwrap_or_default(),
                            }))
                        }) {
                            workspaces.extend(rows.filter_map(Result::ok));
                        }
                    }
                    Err(e) => {
                        entry["error"] = Value::String(format!("read_error: {e}"));
                    }
                }
                entry["workspaces"] = Value::Array(workspaces);
            }
            Err(e) => {
                entry["error"] = Value::String(format!("read_error: {e}"));
            }
        }
        results.push(entry);
    }
    Ok(json!({"databases": results}))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_db(dir: &Path) -> PathBuf {
        let db = dir.join("callwarden.db");
        let conn = Connection::open(&db).expect("创建测试库");
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT);
             INSERT INTO workspaces (id, name, root_path) VALUES (1, 'ws-a', '/repo/a');",
        )
        .expect("建表");
        conn.close();
        db
    }

    #[test]
    fn test_connection_test_success_and_invalid_params() {
        let dir = tempfile::tempdir().expect("tmpdir");
        let db = tmp_db(dir.path());
        let resp = handle_connection_test(&json!({
            "db_path": db.to_string_lossy(),
            "rounds": 3,
        }))
        .expect("ok");
        assert_eq!(resp["success"], 3);
        assert_eq!(resp["fail"], 0);

        let err = handle_connection_test(&json!({})).unwrap_err();
        assert_eq!(err.code, "invalid_params");
    }

    #[test]
    fn test_connection_test_fail_on_missing_db() {
        let resp = handle_connection_test(&json!({
            "db_path": "/nonexistent/dir/x.db",
            "rounds": 2,
        }))
        .expect("ok");
        assert_eq!(resp["success"], 0);
        assert_eq!(resp["fail"], 2);
    }

    #[test]
    fn test_open_readonly_conn_probe() {
        let dir = tempfile::tempdir().expect("tmpdir");
        let db = tmp_db(dir.path());
        let resp = handle_open_readonly_conn(&json!({
            "db_path": db.to_string_lossy(),
        }))
        .expect("ok");
        assert_eq!(resp["openable"], true);
        assert_eq!(resp["readonly"], true);

        let resp2 = handle_open_readonly_conn(&json!({
            "db_path": "/nonexistent/dir/x.db",
        }))
        .expect("ok");
        assert_eq!(resp2["openable"], false);
        assert!(resp2["error"].is_string());
    }

    #[test]
    fn test_read_pragmas_whitelist_and_errors() {
        let dir = tempfile::tempdir().expect("tmpdir");
        let db = tmp_db(dir.path());
        let resp = handle_read_pragmas(&json!({
            "db_path": db.to_string_lossy(),
            "keys": ["journal_mode", "unknown_key"],
        }))
        .expect("ok");
        assert!(resp["pragmas"]["journal_mode"].as_str().unwrap().len() > 0);
        assert_eq!(resp["pragmas"]["unknown_key"], "");

        let err = handle_read_pragmas(&json!({"keys": ["journal_mode"]})).unwrap_err();
        assert_eq!(err.code, "invalid_params");
        let err2 = handle_read_pragmas(&json!({"db_path": "/x.db"})).unwrap_err();
        assert_eq!(err2.code, "invalid_params");
    }

    #[test]
    fn test_read_task_dependencies_requires_task_or_contract() {
        let err = handle_read_task_dependencies(&json!({"workspace_id": 1})).unwrap_err();
        assert_eq!(err.code, "invalid_params");
        let err2 = handle_read_task_dependencies(&json!({"task_id": "T-1"})).unwrap_err();
        assert_eq!(err2.code, "invalid_params");
    }

    #[test]
    fn test_read_task_dependencies_missing_db_returns_empty() {
        let resp = handle_read_task_dependencies(&json!({
            "workspace_id": 1,
            "task_id": "T-1",
            "db_path": "/nonexistent/dir/x.db",
        }))
        .expect("ok");
        assert_eq!(resp["dependencies"], json!([]));
        assert_eq!(resp["artifacts"], json!([]));
        assert_eq!(resp["interfaces"], json!([]));
    }

    #[test]
    fn test_scan_hash_databases_layout() {
        let dir = tempfile::tempdir().expect("tmpdir");
        // 合法 16 位 hex 目录 + callwarden.db
        let hash_dir = dir.path().join("0123456789abcdef");
        std::fs::create_dir_all(&hash_dir).expect("mkdir");
        let conn = Connection::open(hash_dir.join("callwarden.db")).expect("open");
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT);
             INSERT INTO workspaces VALUES (1, 'ws-a', '/repo/a');",
        )
        .expect("seed");
        conn.close();
        // 非法目录名（长度不对 / 大写 hex）应被跳过
        std::fs::create_dir_all(dir.path().join("short")).expect("mkdir");
        std::fs::create_dir_all(dir.path().join("0123456789ABCDEF")).expect("mkdir");

        let resp = handle_scan_hash_databases(&json!({
            "callwarden_dir": dir.path().to_string_lossy(),
        }))
        .expect("ok");
        let dbs = resp["databases"].as_array().expect("array");
        assert_eq!(dbs.len(), 1);
        assert_eq!(dbs[0]["hash"], "0123456789abcdef");
        assert_eq!(dbs[0]["workspaces"][0]["name"], "ws-a");
        assert!(dbs[0]["error"].is_null());
    }

    #[test]
    fn test_scan_hash_databases_missing_dir_returns_empty() {
        let resp = handle_scan_hash_databases(&json!({
            "callwarden_dir": "/nonexistent/callwarden/dir",
        }))
        .expect("ok");
        assert_eq!(resp["databases"], json!([]));
    }
}
