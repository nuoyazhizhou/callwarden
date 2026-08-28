//! daemon client 面 handler（SRV-006：server daemon_client Python authority → Rust daemon）。
//!
//! 对应 `server/daemon_client.py` 中 12 个仍直接 open SQLite / 持有 DB authority 的
//! Python 权威符号：
//! - `_get_db`：权威库路径解析（RPC 无法传递连接对象，下沉为路径元信息探测语义）；
//! - `_inject_workspace_id`：为 RPC params 注入显式 workspace_id（fail-closed）；
//! - 8 个 `_sql_fallback_*`：local 模式兼容回退的只读权威查询
//!   （callers/callees/search/symbol/stats/topological_order/call_chain_down/detect_cycles）；
//! - `call_with_fd`：SCM_RIGHTS FD 传递平台能力探测（RPC 无法传 FD，下沉为能力探测）；
//! - `publish_snapshot`：checkpoint PASSIVE + 发布 payload 归一化。
//!
//! workspace/identity 控制（对齐 db_base._get_active_workspace_id 与 abi-error-code-contract）：
//! 所有查询 handler 只接受显式 `workspace_id > 0`，缺失时从权威库
//! `workspaces WHERE is_active = 1` fail-closed 解析；无 active workspace 一律报错，
//! 绝不静默回退 workspace 1。
//!
//! 错误语义与 Python 对齐（stable errors）：必填参数缺失 → invalid_params；
//! 库不可打开 / 无 active workspace / SQL 失败 → internal_error（稳定消息前缀）。

#[allow(unused_imports)] // BTreeMap/BTreeSet 用于有序 JSON 输出与确定性 DFS
use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::path::PathBuf;

use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Map, Value};

use super::dispatch::{
    get_int_param, get_int_param_or, get_str_param, get_str_param_or, require_str_param,
    DaemonRpcError,
};

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

/// 从 params 解析目标库路径（`db_path` 缺省 → 用户级单库）。
fn resolve_db_path(params: &Value) -> PathBuf {
    match get_str_param(params, "db_path") {
        Some(p) if !p.is_empty() => PathBuf::from(p),
        _ => default_db_path(),
    }
}

/// 只读连接打开（mode=ro，busy 3s，对齐 SRV-004 cli_admin 只读探测语义）。
fn open_readonly(db_path: &str) -> Result<Connection, DaemonRpcError> {
    let uri = format!("file:{}?mode=ro", db_path);
    let conn = Connection::open_with_flags(
        &uri,
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(|e| {
        DaemonRpcError::internal_error(format!("无法打开权威库 {}: {}", db_path, e))
    })?;
    conn.busy_timeout(std::time::Duration::from_secs(3))
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("busy_timeout 设置失败 {}: {}", db_path, e))
        })?;
    Ok(conn)
}

/// 任意列 → JSON Value（rusqlite 未启用 serde_json feature，手写 ValueRef 转换）。
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

/// workspace_id 解析（fail-closed）：显式 `workspace_id > 0` 优先；
/// 缺失时从 workspaces 表 is_active=1 解析；无 active workspace → internal_error。
fn resolve_workspace_id(conn: &Connection, params: &Value) -> Result<i64, DaemonRpcError> {
    if let Some(n) = get_int_param(params, "workspace_id") {
        if n > 0 {
            return Ok(n);
        }
        return Err(DaemonRpcError::invalid_params(
            "workspace_id 必须 > 0，禁止回退 workspace 1".to_string(),
        ));
    }
    let ws: Option<i64> = conn
        .query_row(
            "SELECT id FROM workspaces WHERE is_active = 1 ORDER BY id LIMIT 1",
            [],
            |r| r.get::<_, i64>(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("workspaces 查询失败: {}", e)))?;
    ws.ok_or_else(|| {
        DaemonRpcError::internal_error(
            "没有 active workspace（is_active=1），拒绝推导 workspace_id（fail-closed）"
                .to_string(),
        )
    })
}

// ======================================================================
// 1. get_db / inject_workspace_id
// ======================================================================

/// `_get_db` 下沉：RPC 无法传递 CodeGraphDB 实例，返回权威库路径元信息
///（探测语义：路径 + 是否存在）。
pub fn handle_get_db(params: &Value) -> Result<Value, DaemonRpcError> {
    let path = resolve_db_path(params);
    Ok(json!({
        "db_path": path.to_string_lossy(),
        "exists": path.exists(),
    }))
}

/// `_inject_workspace_id` 下沉：params 已有 truthy workspace_id 时原样短路；
/// 否则从权威库 fail-closed 解析 active workspace 并注入。
pub fn handle_inject_workspace_id(params: &Value) -> Result<Value, DaemonRpcError> {
    let inner = params
        .get("params")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    // 对齐 Python `if params.get("workspace_id"):` 的 truthy 短路
    if let Some(v) = inner.get("workspace_id") {
        let truthy = match v {
            Value::Number(n) => n.as_i64().map(|x| x > 0).unwrap_or(true),
            Value::String(s) => !s.is_empty(),
            Value::Bool(b) => *b,
            Value::Null => false,
            _ => true,
        };
        if truthy {
            return Ok(json!({"params": inner, "injected": false}));
        }
    }
    let db_path = resolve_db_path(params);
    let conn = open_readonly(&db_path.to_string_lossy())?;
    let ws_id = resolve_workspace_id(&conn, params)?;
    let mut out = match inner {
        Value::Object(m) => m,
        other => {
            let mut m = Map::new();
            m.insert("_raw".to_string(), other);
            m
        }
    };
    out.insert("workspace_id".to_string(), json!(ws_id));
    Ok(json!({"params": Value::Object(out), "injected": true}))
}

// ======================================================================
// 2. callers / callees（对齐 db_query.get_callers / get_callees SQL 降级路径）
// ======================================================================

/// QN 自动识别拆分：含 `.`/`::` 的名称视为 QN，提取短名（对齐 Python rsplit 语义）。
fn split_qn_name(name: &str) -> (String, Option<String>) {
    if name.contains('.') || name.contains("::") {
        let short = name
            .rsplit('.')
            .next()
            .unwrap_or(name)
            .rsplit("::")
            .next()
            .unwrap_or(name);
        (short.to_string(), Some(name.to_string()))
    } else {
        (name.to_string(), None)
    }
}

fn row_to_caller(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": cell(row, 0),
        "caller_id": cell(row, 1),
        "caller_name": cell(row, 2),
        "caller_module": cell(row, 3),
        "callee_name": cell(row, 4),
        "callee_module": cell(row, 5),
        "callee_qualified": cell(row, 6),
        "callee_file": cell(row, 7),
        "callee_id": cell(row, 8),
        "call_line": cell(row, 9),
        "is_cross_file": cell(row, 10),
        "caller_file": cell(row, 11),
    }))
}

/// 显式 QN 分支：先解析 callee_id 再过滤边（P7/P28 语义）。
fn query_callers_by_qn(conn: &Connection, ws_id: i64, qn: &str) -> Result<Vec<Value>, DaemonRpcError> {
    let sql = "SELECT c.id, c.caller_id, s.name, c.caller_module, c.callee_name, \
               c.callee_module, c.callee_qualified, c.callee_file, c.callee_id, \
               c.call_line, c.is_cross_file, fi.rel_path \
               FROM calls c \
               JOIN symbols s ON c.caller_id = s.id \
               JOIN file_instances fi ON s.file_instance_id = fi.id \
               WHERE fi.workspace_id = ?1 \
                 AND c.callee_id > 0 \
                 AND c.callee_id = ( \
                     SELECT target.id FROM symbols target \
                     JOIN file_instances target_fi ON target.file_instance_id = target_fi.id \
                     WHERE target_fi.workspace_id = ?1 AND target.qualified_name = ?2 \
                     LIMIT 1) \
               ORDER BY fi.rel_path, c.call_line";
    let qn = qn.to_string();
    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("callers QN 查询准备失败: {}", e)))?;
    let rows = stmt
        .query_map(rusqlite::params![ws_id, qn], row_to_caller)
        .map_err(|e| DaemonRpcError::internal_error(format!("callers QN 查询失败: {}", e)))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("callers QN 行读取失败: {}", e)))
}

/// 短名分支（对齐 Python：`WHERE c.callee_name = ?`，不带 workspace 过滤）。
fn query_callers_by_short(conn: &Connection, name: &str) -> Result<Vec<Value>, DaemonRpcError> {
    let sql = "SELECT c.id, c.caller_id, s.name, c.caller_module, c.callee_name, \
               c.callee_module, c.callee_qualified, c.callee_file, c.callee_id, \
               c.call_line, c.is_cross_file, fi.rel_path \
               FROM calls c \
               JOIN symbols s ON c.caller_id = s.id \
               JOIN file_instances fi ON s.file_instance_id = fi.id \
               WHERE c.callee_name = ?1 \
               ORDER BY fi.rel_path, c.call_line";
    let name = name.to_string();
    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("callers 短名查询准备失败: {}", e)))?;
    let rows = stmt
        .query_map(rusqlite::params![name], row_to_caller)
        .map_err(|e| DaemonRpcError::internal_error(format!("callers 短名查询失败: {}", e)))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("callers 短名行读取失败: {}", e)))
}

pub fn handle_sql_fallback_get_callers(params: &Value) -> Result<Value, DaemonRpcError> {
    let callee_name = require_str_param(params, "callee_name")?;
    let explicit_qn = get_str_param(params, "qualified_name")
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string());
    // QN 自动识别：显式 qualified_name 缺失且名称含分隔符
    let (short_name, auto_qn) = match &explicit_qn {
        Some(qn) => (callee_name.to_string(), Some(qn.clone())),
        None => split_qn_name(callee_name),
    };
    let auto_qn_fallback = explicit_qn.is_none() && auto_qn.is_some();
    let db_path = resolve_db_path(params);
    let conn = open_readonly(&db_path.to_string_lossy())?;
    let ws_id = resolve_workspace_id(&conn, params)?;
    let mut result: Vec<Value> = Vec::new();
    if let Some(qn) = &auto_qn {
        result = query_callers_by_qn(&conn, ws_id, qn)?;
        if result.is_empty() && !auto_qn_fallback {
            return Ok(json!({"callers": []}));
        }
    }
    if result.is_empty() {
        result = query_callers_by_short(&conn, &short_name)?;
    }
    Ok(json!({"callers": result}))
}

fn row_to_callee(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "callee_name": cell(row, 0),
        "callee_file": cell(row, 1),
        "callee_qualified": cell(row, 2),
        "call_line": cell(row, 3),
        "is_cross_file": cell(row, 4),
    }))
}

pub fn handle_sql_fallback_get_callees(params: &Value) -> Result<Value, DaemonRpcError> {
    let caller_name = require_str_param(params, "caller_name")?;
    let explicit_qn = get_str_param(params, "qualified_name")
        .filter(|s| !s.is_empty())
        .map(|s| s.to_string());
    let (short_name, auto_qn) = match &explicit_qn {
        Some(qn) => (caller_name.to_string(), Some(qn.clone())),
        None => split_qn_name(caller_name),
    };
    let auto_qn_fallback = explicit_qn.is_none() && auto_qn.is_some();
    let db_path = resolve_db_path(params);
    let conn = open_readonly(&db_path.to_string_lossy())?;
    // workspace 身份控制：解析 fail-closed（QN 分支 SQL 虽全局匹配，身份解析仍必须成功）
    let _ws_id = resolve_workspace_id(&conn, params)?;
    let mut result: Vec<Value> = Vec::new();
    if let Some(qn) = &auto_qn {
        let sql = "SELECT c.callee_name, c.callee_file, c.callee_qualified, c.call_line, \
                   c.is_cross_file FROM calls c \
                   JOIN symbols s ON c.caller_id = s.id \
                   WHERE s.qualified_name = ?1 ORDER BY c.call_line";
        let qn = qn.clone();
        let mut stmt = conn.prepare(sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("callees QN 查询准备失败: {}", e))
        })?;
        let rows = stmt
            .query_map(rusqlite::params![qn], row_to_callee)
            .map_err(|e| DaemonRpcError::internal_error(format!("callees QN 查询失败: {}", e)))?;
        result = rows
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("callees QN 行读取失败: {}", e)))?;
        if result.is_empty() && !auto_qn_fallback {
            return Ok(json!({"callees": []}));
        }
    }
    if result.is_empty() {
        let sql = "SELECT c.callee_name, c.callee_file, c.callee_qualified, c.call_line, \
                   c.is_cross_file FROM calls c \
                   JOIN symbols s ON c.caller_id = s.id \
                   WHERE s.name = ?1 ORDER BY c.call_line";
        let name = short_name.clone();
        let mut stmt = conn.prepare(sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("callees 短名查询准备失败: {}", e))
        })?;
        let rows = stmt
            .query_map(rusqlite::params![name], row_to_callee)
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("callees 短名查询失败: {}", e))
            })?;
        result = rows
            .collect::<Result<Vec<_>, _>>()
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("callees 短名行读取失败: {}", e))
            })?;
    }
    Ok(json!({"callees": result}))
}

// ======================================================================
// 3. search_symbols（FTS5 trigram 主路径 + LIKE 兜底，对齐 db_query.search_symbols）
// ======================================================================

/// FTS5 trigram 查询构建（对齐 _build_fts_query：token 须 >= 3 字符，双引号包裹）。
fn build_fts_query(query: &str) -> Option<String> {
    let mut tokens: Vec<String> = Vec::new();
    let mut cur = String::new();
    for ch in query.chars() {
        if ch.is_ascii_alphanumeric() || ch == '_' {
            cur.push(ch);
        } else if !cur.is_empty() {
            tokens.push(std::mem::take(&mut cur));
        }
    }
    if !cur.is_empty() {
        tokens.push(cur);
    }
    let valid: Vec<String> = tokens.into_iter().filter(|t| t.len() >= 3).collect();
    if valid.is_empty() {
        return None;
    }
    Some(
        valid
            .iter()
            .map(|t| format!("\"{}\"", t))
            .collect::<Vec<_>>()
            .join(" "),
    )
}

fn search_like(conn: &Connection, ws_id: i64, query: &str, kind: Option<&str>, limit: i64) -> Result<Vec<Value>, DaemonRpcError> {
    let mut sql = String::from(
        "SELECT DISTINCT fsv.qualified_name, fsv.module_path, fsv.start_line, fsv.end_line, \
         fsv.depth, sc.name, sc.kind, sc.signature, sc.has_comment, fi.rel_path \
         FROM file_symbol_versions fsv \
         JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
         JOIN file_versions fv ON fsv.file_version_id = fv.id \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         WHERE fi.workspace_id = ? AND fi.status != 'archived' AND fv.is_current = 1 \
           AND fsv.is_deleted = 0 AND (fsv.qualified_name LIKE ? OR sc.name LIKE ?)",
    );
    let pat = format!("%{}%", query);
    let mut binds: Vec<Value> = vec![json!(ws_id), json!(pat), json!(pat)];
    if let Some(k) = kind {
        sql.push_str(" AND sc.kind = ?");
        binds.push(json!(k));
    }
    sql.push_str(" ORDER BY sc.kind, fsv.depth DESC, fi.rel_path, fsv.start_line LIMIT ?");
    binds.push(json!(limit));
    run_symbol_search(conn, &sql, &binds)
}

fn run_symbol_search(conn: &Connection, sql: &str, binds: &[Value]) -> Result<Vec<Value>, DaemonRpcError> {
    let mut stmt = conn
        .prepare(sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("search 查询准备失败: {}", e)))?;
    let params: Vec<Box<dyn rusqlite::ToSql>> = binds
        .iter()
        .map(|v| -> Box<dyn rusqlite::ToSql> {
            match v {
                Value::Number(n) => Box::new(n.as_i64().unwrap_or(0)),
                Value::String(s) => Box::new(s.clone()),
                _ => Box::new(v.to_string()),
            }
        })
        .collect();
    let refs: Vec<&dyn rusqlite::ToSql> = params.iter().map(|b| b.as_ref()).collect();
    let rows = stmt
        .query_map(refs.as_slice(), |row| {
            Ok(json!({
                "qualified_name": cell(row, 0),
                "module_path": cell(row, 1),
                "start_line": cell(row, 2),
                "end_line": cell(row, 3),
                "depth": cell(row, 4),
                "name": cell(row, 5),
                "kind": cell(row, 6),
                "signature": cell(row, 7),
                "has_comment": cell(row, 8),
                "file_path": cell(row, 9),
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("search 查询失败: {}", e)))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("search 行读取失败: {}", e)))
}

pub fn handle_sql_fallback_search_symbols(params: &Value) -> Result<Value, DaemonRpcError> {
    let query = require_str_param(params, "query")?;
    let kind = get_str_param(params, "kind").filter(|s| !s.is_empty());
    let limit = get_int_param_or(params, "limit", 20);
    let db_path = resolve_db_path(params);
    let conn = open_readonly(&db_path.to_string_lossy())?;
    let ws_id = resolve_workspace_id(&conn, params)?;
    // 1. FTS5 trigram 主路径（query >= 3 字符且 symbols_fts 可用）
    if let Some(fts_query) = build_fts_query(query) {
        let mut sql = String::from(
            "SELECT DISTINCT s.qualified_name, s.module_path, s.start_line, s.end_line, \
             s.depth, s.name, s.kind, s.signature, s.has_comment, fi.rel_path \
             FROM symbols_fts JOIN symbols s ON s.id = symbols_fts.rowid \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ? AND fi.status != 'archived' AND symbols_fts MATCH ?",
        );
        let mut binds: Vec<Value> = vec![json!(ws_id), json!(fts_query)];
        if let Some(k) = kind {
            sql.push_str(" AND s.kind = ?");
            binds.push(json!(k));
        }
        sql.push_str(" ORDER BY s.kind, s.depth DESC, fi.rel_path, s.start_line LIMIT ?");
        binds.push(json!(limit));
        if let Ok(rows) = run_symbol_search(&conn, &sql, &binds) {
            return Ok(json!({"symbols": rows}));
        }
        // FTS5 不可用 / MATCH 语法错误 → 兜底 LIKE
    }
    // 2. LIKE %query% 兜底（对齐 _search_symbols_like）
    let rows = search_like(&conn, ws_id, query, kind, limit)?;
    Ok(json!({"symbols": rows}))
}

// ======================================================================
// 4. get_symbol（对齐 db_query.get_symbol 核心三段 SQL；富注入降级为空默认值）
// ======================================================================

pub fn handle_sql_fallback_get_symbol(params: &Value) -> Result<Value, DaemonRpcError> {
    let qn = require_str_param(params, "qualified_name")?;
    let db_path = resolve_db_path(params);
    let conn = open_readonly(&db_path.to_string_lossy())?;
    let ws_id = resolve_workspace_id(&conn, params)?;
    let main_sql = "SELECT DISTINCT fsv.qualified_name, fsv.module_path, fsv.start_line, \
        fsv.end_line, fsv.depth, sc.name, sc.kind, sc.signature, sc.has_comment, \
        sc.comment_content, sc.content_hash, fi.rel_path, fi.abs_path \
        FROM file_symbol_versions fsv \
        JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash \
        JOIN file_versions fv ON fsv.file_version_id = fv.id \
        JOIN file_instances fi ON fv.file_instance_id = fi.id \
        WHERE fi.workspace_id = ?1 AND fi.status != 'archived' AND fv.is_current = 1 \
          AND fsv.is_deleted = 0 AND fsv.qualified_name = ?2 LIMIT 1";
    let qn_owned = qn.to_string();
    let main: Option<Value> = conn
        .prepare(main_sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol 主查询准备失败: {}", e)))?
        .query_row(rusqlite::params![ws_id, qn_owned], |row| {
            Ok(json!({
                "qualified_name": cell(row, 0),
                "module_path": cell(row, 1),
                "start_line": cell(row, 2),
                "end_line": cell(row, 3),
                "depth": cell(row, 4),
                "name": cell(row, 5),
                "kind": cell(row, 6),
                "signature": cell(row, 7),
                "has_comment": cell(row, 8),
                "comment_content": cell(row, 9),
                "content_hash": cell(row, 10),
                "file_path": cell(row, 11),
                "abs_path": cell(row, 12),
            }))
        })
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol 主查询失败: {}", e)))?;
    let mut result = match main {
        Some(v) => v,
        None => return Ok(json!({"symbol": Value::Null})),
    };
    let out_sql = "SELECT DISTINCT COALESCE(NULLIF(cv.callee_qualified, ''), cv.callee_name), \
        cv.callee_module, cv.callee_file, cv.call_line \
        FROM call_versions cv \
        JOIN file_versions fv ON cv.file_version_id = fv.id \
        JOIN file_instances fi ON fv.file_instance_id = fi.id \
        WHERE fi.workspace_id = ?1 AND fv.is_current = 1 AND cv.caller_qualified = ?2 \
        ORDER BY cv.callee_qualified";
    let mut stmt = conn
        .prepare(out_sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("calls_out 查询准备失败: {}", e)))?;
    let calls_out = stmt
        .query_map(rusqlite::params![ws_id, qn_owned], |row| {
            Ok(json!({
                "target_name": cell(row, 0),
                "target_module": cell(row, 1),
                "target_file": cell(row, 2),
                "call_line": cell(row, 3),
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("calls_out 查询失败: {}", e)))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("calls_out 行读取失败: {}", e)))?;
    result["calls_out"] = json!(calls_out);
    let in_sql = "SELECT DISTINCT cv.caller_qualified, cv.caller_hash, cv.call_line, fi.rel_path \
        FROM call_versions cv \
        JOIN file_versions fv ON cv.file_version_id = fv.id \
        JOIN file_instances fi ON fv.file_instance_id = fi.id \
        WHERE fi.workspace_id = ?1 AND fv.is_current = 1 AND cv.callee_qualified = ?2 \
          AND cv.caller_qualified != '' \
        ORDER BY cv.caller_qualified";
    let mut stmt = conn
        .prepare(in_sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("called_by 查询准备失败: {}", e)))?;
    let called_by = stmt
        .query_map(rusqlite::params![ws_id, qn_owned], |row| {
            Ok(json!({
                "caller_name": cell(row, 0),
                "caller_hash": cell(row, 1),
                "call_line": cell(row, 2),
                "caller_file": cell(row, 3),
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("called_by 查询失败: {}", e)))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("called_by 行读取失败: {}", e)))?;
    result["called_by"] = json!(called_by);
    // 富注入（rules/issues/tests/evolution）在 Python 侧为 fail-soft try-except，
    // 下沉后统一以空默认值对齐异常降级语义（见 SRV-006 finding）。
    result["applicable_rules"] = json!([]);
    result["issues"] = json!([]);
    result["issues_total"] = json!(0);
    result["has_tests"] = json!(false);
    result["test_count"] = json!(0);
    result["test_cases"] = json!([]);
    result["evolution_summary"] = json!({
        "qualified_name": qn,
        "change_count": 0,
        "defect_count": 0,
        "defect_rate": 0.0,
        "recent_defects": [],
    });
    Ok(json!({"symbol": result}))
}

// ======================================================================
// 5. get_stats（对齐 db_query.get_stats 的 7 段聚合 SQL）
// ======================================================================

pub fn handle_sql_fallback_get_stats(params: &Value) -> Result<Value, DaemonRpcError> {
    let db_path = resolve_db_path(params);
    let conn = open_readonly(&db_path.to_string_lossy())?;
    let ws_id = resolve_workspace_id(&conn, params)?;
    let mut stats = Map::new();
    // SQL 1：file_instances + symbol_contents
    let (total_files, unique_sc): (i64, i64) = conn
        .query_row(
            "SELECT (SELECT COUNT(*) FROM file_instances \
                     WHERE workspace_id = ?1 AND status != 'archived'), \
                    (SELECT COUNT(*) FROM symbol_contents)",
            rusqlite::params![ws_id],
            |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?)),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL1 失败: {}", e)))?;
    stats.insert("total_files".into(), json!(total_files));
    stats.insert("unique_symbol_contents".into(), json!(unique_sc));
    // SQL 2：symbols 聚合
    let (total_symbols, commented): (i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), SUM(CASE WHEN s.comment_status = 'done' THEN 1 ELSE 0 END) \
             FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived'",
            rusqlite::params![ws_id],
            |r| Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1).unwrap_or(0))),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL2 失败: {}", e)))?;
    stats.insert("total_symbols".into(), json!(total_symbols));
    stats.insert("commented".into(), json!(commented));
    // SQL 3：calls 聚合
    let (total_calls, cross_file, resolved): (i64, i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), \
                    SUM(CASE WHEN c.is_cross_file = 1 THEN 1 ELSE 0 END), \
                    SUM(CASE WHEN c.callee_id IS NOT NULL THEN 1 ELSE 0 END) \
             FROM calls c JOIN symbols s ON c.caller_id = s.id \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived'",
            rusqlite::params![ws_id],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1).unwrap_or(0),
                    r.get::<_, i64>(2).unwrap_or(0),
                ))
            },
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL3 失败: {}", e)))?;
    stats.insert("total_calls".into(), json!(total_calls));
    stats.insert("cross_file_calls".into(), json!(cross_file));
    stats.insert("resolved_calls".into(), json!(resolved));
    // SQL 4：file_versions 聚合
    let (total_fv, current_files, multi_version): (i64, i64, i64) = conn
        .query_row(
            "SELECT COUNT(*), \
                    SUM(CASE WHEN fv.is_current = 1 THEN 1 ELSE 0 END), \
                    (SELECT COUNT(*) FROM ( \
                        SELECT fv2.file_instance_id FROM file_versions fv2 \
                        JOIN file_instances fi2 ON fv2.file_instance_id = fi2.id \
                        WHERE fi2.workspace_id = ?1 AND fi2.status != 'archived' \
                        GROUP BY fv2.file_instance_id HAVING COUNT(*) > 1)) \
             FROM file_versions fv JOIN file_instances fi ON fv.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived'",
            rusqlite::params![ws_id],
            |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, i64>(1).unwrap_or(0),
                    r.get::<_, i64>(2)?,
                ))
            },
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL4 失败: {}", e)))?;
    stats.insert("total_file_versions".into(), json!(total_fv));
    stats.insert("current_files".into(), json!(current_files));
    stats.insert("multi_version_files".into(), json!(multi_version));
    // SQL 5：fsv + cv UNION ALL
    let mut stmt = conn
        .prepare(
            "SELECT 'fsv', COUNT(*) FROM file_symbol_versions fsv \
             JOIN file_versions fv ON fsv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived' \
             UNION ALL \
             SELECT 'cv', COUNT(*) FROM call_versions cv \
             JOIN file_versions fv ON cv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived'",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL5 准备失败: {}", e)))?;
    let rows = stmt
        .query_map(rusqlite::params![ws_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL5 失败: {}", e)))?;
    for r in rows {
        let (kind, cnt) = r
            .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL5 行读取失败: {}", e)))?;
        if kind == "fsv" {
            stats.insert("total_file_symbol_links".into(), json!(cnt));
        } else if kind == "cv" {
            stats.insert("total_call_versions".into(), json!(cnt));
        }
    }
    // SQL 6：by_kind
    let mut by_kind: BTreeMap<String, i64> = BTreeMap::new();
    let mut stmt = conn
        .prepare(
            "SELECT s.kind, COUNT(*) FROM symbols s \
             WHERE s.file_instance_id IN ( \
                 SELECT id FROM file_instances WHERE workspace_id = ?1 AND status != 'archived') \
             GROUP BY s.kind ORDER BY COUNT(*) DESC",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL6 准备失败: {}", e)))?;
    let rows = stmt
        .query_map(rusqlite::params![ws_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, i64>(1)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL6 失败: {}", e)))?;
    for r in rows {
        let (kind, cnt) = r
            .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL6 行读取失败: {}", e)))?;
        by_kind.insert(kind, cnt);
    }
    stats.insert("by_kind".into(), json!(by_kind));
    // SQL 7：depth_distribution
    let mut depth_dist: BTreeMap<i64, i64> = BTreeMap::new();
    let mut stmt = conn
        .prepare(
            "SELECT s.depth, COUNT(*) FROM symbols s \
             WHERE s.file_instance_id IN ( \
                 SELECT id FROM file_instances WHERE workspace_id = ?1 AND status != 'archived') \
               AND s.kind IN ('fn', 'test_fn') AND s.depth >= 0 \
             GROUP BY s.depth ORDER BY s.depth",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL7 准备失败: {}", e)))?;
    let rows = stmt
        .query_map(rusqlite::params![ws_id], |r| {
            Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL7 失败: {}", e)))?;
    for r in rows {
        let (depth, cnt) = r
            .map_err(|e| DaemonRpcError::internal_error(format!("stats SQL7 行读取失败: {}", e)))?;
        depth_dist.insert(depth, cnt);
    }
    stats.insert("depth_distribution".into(), json!(depth_dist));
    Ok(json!({"stats": Value::Object(stats)}))
}

// ======================================================================
// 6. get_topological_order（对齐 db_query SQL 降级路径：depth ASC）
// ======================================================================

pub fn handle_sql_fallback_get_topological_order(params: &Value) -> Result<Value, DaemonRpcError> {
    let limit = get_int_param_or(params, "limit", 50);
    let db_path = resolve_db_path(params);
    let conn = open_readonly(&db_path.to_string_lossy())?;
    let ws_id = resolve_workspace_id(&conn, params)?;
    let mut stmt = conn
        .prepare(
            "SELECT s.id, s.name, s.qualified_name, s.module_path, s.kind, s.signature, \
                    s.start_line, s.end_line, s.depth, s.has_comment, s.comment_status, \
                    fi.rel_path, fi.abs_path \
             FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.kind = 'fn' \
             ORDER BY s.depth ASC, s.start_line ASC LIMIT ?2",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("topo 查询准备失败: {}", e)))?;
    let rows = stmt
        .query_map(rusqlite::params![ws_id, limit], |row| {
            Ok(json!({
                "id": cell(row, 0),
                "name": cell(row, 1),
                "qualified_name": cell(row, 2),
                "module_path": cell(row, 3),
                "kind": cell(row, 4),
                "signature": cell(row, 5),
                "start_line": cell(row, 6),
                "end_line": cell(row, 7),
                "depth": cell(row, 8),
                "has_comment": cell(row, 9),
                "comment_status": cell(row, 10),
                "rel_path": cell(row, 11),
                "abs_path": cell(row, 12),
            }))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("topo 查询失败: {}", e)))?;
    let order = rows
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("topo 行读取失败: {}", e)))?;
    Ok(json!({"order": order}))
}

// ======================================================================
// 7. get_call_chain_down（对齐 analyzers/call_chain.py BFS，按层展开）
// ======================================================================

/// 单层批量查询：当前层 caller 集合 → call_versions 下游边（500/批，对齐 Python）。
fn chunk_down_edges(
    conn: &Connection,
    ws_id: i64,
    chunk: &[String],
) -> Result<Vec<(String, String)>, DaemonRpcError> {
    let placeholders = vec!["?"; chunk.len()].join(",");
    let sql = format!(
        "SELECT DISTINCT cv.callee_qualified, cv.caller_qualified \
         FROM call_versions cv \
         JOIN file_versions fv ON cv.file_version_id = fv.id \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         WHERE fi.workspace_id = ? AND fv.is_current = 1 \
           AND cv.caller_qualified IN ({}) AND cv.callee_qualified != ''",
        placeholders
    );
    let mut binds: Vec<Box<dyn rusqlite::ToSql>> = vec![Box::new(ws_id)];
    for c in chunk {
        binds.push(Box::new(c.clone()));
    }
    let refs: Vec<&dyn rusqlite::ToSql> = binds.iter().map(|b| b.as_ref()).collect();
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|e| DaemonRpcError::internal_error(format!("chain_down 查询准备失败: {}", e)))?;
    let rows = stmt
        .query_map(refs.as_slice(), |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("chain_down 查询失败: {}", e)))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("chain_down 行读取失败: {}", e)))
}

pub fn handle_sql_fallback_get_call_chain_down(params: &Value) -> Result<Value, DaemonRpcError> {
    let qn = require_str_param(params, "qualified_name")?;
    let max_depth = get_int_param_or(params, "max_depth", 10);
    let db_path = resolve_db_path(params);
    let conn = open_readonly(&db_path.to_string_lossy())?;
    let ws_id = resolve_workspace_id(&conn, params)?;
    let mut visited: HashSet<String> = HashSet::new();
    visited.insert(qn.to_string());
    let mut current: Vec<String> = vec![qn.to_string()];
    let mut levels: Vec<Value> = Vec::new();
    let mut edges: Vec<Value> = Vec::new();
    let mut depth: i64 = 0;
    while depth < max_depth {
        let mut level_callees: Vec<Value> = Vec::new();
        let mut next: BTreeSet<String> = BTreeSet::new();
        for chunk in current.chunks(500) {
            for (callee, caller) in chunk_down_edges(&conn, ws_id, chunk)? {
                if !callee.is_empty() && !visited.contains(&callee) {
                    visited.insert(callee.clone());
                    next.insert(callee.clone());
                    let edge = json!({
                        "callee": callee,
                        "caller": caller,
                        "depth": depth + 1,
                    });
                    level_callees.push(edge);
                }
            }
        }
        if level_callees.is_empty() {
            break;
        }
        levels.push(json!({
            "depth": depth + 1,
            "count": level_callees.len(),
            "callees": level_callees.clone(),
        }));
        edges.extend(level_callees);
        depth += 1;
        current = next.into_iter().collect();
        if current.is_empty() {
            break;
        }
    }
    Ok(json!({
        "start": qn,
        "edges": edges,
        "levels": levels,
        "max_depth_reached": levels.len(),
        "total_downstream": visited.len().saturating_sub(1),
    }))
}

// ======================================================================
// 8. detect_cycles（对齐 analyzers/call_chain.py Python DFS：路径栈 + 环归一化）
// ======================================================================

/// 环归一化：旋转到最小元素开头，末尾补回起点（对齐 Python normalized_cycle）。
fn normalize_cycle(cycle: &[String]) -> Vec<String> {
    let ring = &cycle[..cycle.len() - 1];
    if ring.is_empty() {
        return cycle.to_vec();
    }
    let min_idx = ring
        .iter()
        .enumerate()
        .min_by(|a, b| a.1.cmp(b.1))
        .map(|(i, _)| i)
        .unwrap_or(0);
    let mut out: Vec<String> = Vec::with_capacity(cycle.len());
    out.extend_from_slice(&ring[min_idx..]);
    out.extend_from_slice(&ring[..min_idx]);
    out.push(ring[min_idx].clone());
    out
}

/// DFS 环检测（迭代式，等价 Python 递归 dfs：path_set 记录在途环，
/// 每根重置 visited，最多 50 环）。
fn detect_cycles_impl(edges: &[(String, String)], max_depth: i64) -> Vec<Vec<String>> {
    let mut adj: HashMap<String, Vec<String>> = HashMap::new();
    let mut nodes: BTreeSet<String> = BTreeSet::new();
    for (a, b) in edges {
        adj.entry(a.clone()).or_default().push(b.clone());
        nodes.insert(a.clone());
        nodes.insert(b.clone());
    }
    for v in adj.values_mut() {
        v.sort();
        v.dedup();
    }
    let mut cycles: Vec<Vec<String>> = Vec::new();
    let mut seen: HashSet<Vec<String>> = HashSet::new();
    let mut visited: HashSet<String> = HashSet::new();
    for root in nodes.iter() {
        if visited.contains(root) {
            continue;
        }
        let mut path_stack: Vec<String> = Vec::new();
        let mut path_set: HashSet<String> = HashSet::new();
        let mut frames: Vec<(String, i64, usize)> = Vec::new();
        // 进入根节点（depth=0，必满足 <= max_depth）
        visited.insert(root.clone());
        path_stack.push(root.clone());
        path_set.insert(root.clone());
        frames.push((root.clone(), 0, 0));
        while let Some((node, depth, idx)) = frames.last_mut() {
            let nb_count = adj.get(node).map(|v| v.len()).unwrap_or(0);
            if *depth + 1 > max_depth || *idx >= nb_count {
                let done = node.clone();
                frames.pop();
                path_stack.pop();
                path_set.remove(&done);
                continue;
            }
            let nb = adj[node][*idx].clone();
            *idx += 1;
            let child_depth = *depth + 1;
            if child_depth > max_depth {
                continue;
            }
            if path_set.contains(&nb) {
                if let Some(start) = path_stack.iter().position(|x| x == &nb) {
                    let mut cyc: Vec<String> = path_stack[start..].to_vec();
                    cyc.push(nb.clone());
                    let norm = normalize_cycle(&cyc);
                    if seen.insert(norm.clone()) {
                        cycles.push(norm);
                    }
                }
                continue;
            }
            if visited.contains(&nb) {
                continue;
            }
            visited.insert(nb.clone());
            path_stack.push(nb.clone());
            path_set.insert(nb.clone());
            frames.push((nb, child_depth, 0));
        }
        visited.clear(); // Python：每根 DFS 结束后重置 visited
        if cycles.len() >= 50 {
            break;
        }
    }
    cycles
}

pub fn handle_sql_fallback_detect_cycles(params: &Value) -> Result<Value, DaemonRpcError> {
    let max_depth = get_int_param_or(params, "max_depth", 10);
    let db_path = resolve_db_path(params);
    let conn = open_readonly(&db_path.to_string_lossy())?;
    let ws_id = resolve_workspace_id(&conn, params)?;
    let mut stmt = conn
        .prepare(
            "SELECT DISTINCT cv.caller_qualified, cv.callee_qualified \
             FROM call_versions cv \
             JOIN file_versions fv ON cv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fv.is_current = 1 \
               AND cv.caller_qualified != '' AND cv.callee_qualified != ''",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("cycles 边加载准备失败: {}", e)))?;
    let rows = stmt
        .query_map(rusqlite::params![ws_id], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("cycles 边加载失败: {}", e)))?;
    let edges = rows
        .collect::<Result<Vec<_>, _>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("cycles 边读取失败: {}", e)))?;
    let cycles = detect_cycles_impl(&edges, max_depth);
    Ok(json!({"cycles": cycles}))
}

// ======================================================================
// 9. call_with_fd / publish_snapshot
// ======================================================================

/// `call_with_fd` 下沉：SCM_RIGHTS FD 传递平台能力探测（RPC 无法传 FD，
/// 对齐 Python win32/无 AF_UNIX 时抛 DaemonUnavailableError 的语义 → supported=false）。
pub fn handle_call_with_fd(params: &Value) -> Result<Value, DaemonRpcError> {
    let _method = get_str_param(params, "method").unwrap_or("");
    #[cfg(unix)]
    {
        Ok(json!({"supported": true, "transport": "scm_rights"}))
    }
    #[cfg(not(unix))]
    {
        Ok(json!({
            "supported": false,
            "transport": "",
            "error": "当前平台不支持 SCM_RIGHTS FD 传递",
        }))
    }
}

/// `publish_snapshot` 下沉：checkpoint PASSIVE 双保险（对齐 C4/S8 策略：
/// busy_timeout=5000 等待 + PASSIVE checkpoint，busy 不 fail-fast），
/// 返回归一化发布 payload；实际 snapshot.publish 由薄客户端经既有 RPC 发起。
pub fn handle_publish_snapshot(params: &Value) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let db_path = require_str_param(params, "db_path")?;
    let build_context_hash = get_str_param_or(params, "build_context_hash", "");
    let conn = Connection::open(db_path).map_err(|e| {
        DaemonRpcError::internal_error(format!("无法打开快照库 {}: {}", db_path, e))
    })?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("busy_timeout 设置失败 {}: {}", db_path, e))
        })?;
    // PASSIVE checkpoint：返回行 (busy, log, checkpointed)，busy 时不抛错
    let mut stmt = conn
        .prepare("PRAGMA wal_checkpoint(PASSIVE)")
        .map_err(|e| DaemonRpcError::internal_error(format!("checkpoint 准备失败: {}", e)))?;
    let checkpointed = {
        let mut rows = stmt
            .query([])
            .map_err(|e| DaemonRpcError::internal_error(format!("checkpoint 执行失败: {}", e)))?;
        rows.next()
            .map_err(|e| DaemonRpcError::internal_error(format!("checkpoint 读取失败: {}", e)))?
            .is_some()
    };
    // 归一化绝对路径（canonicalize 失败时保留原路径，对齐 os.path.abspath 宽松语义）
    let abs_path = std::fs::canonicalize(db_path)
        .map(|p| p.to_string_lossy().into_owned())
        .unwrap_or_else(|_| db_path.to_string());
    Ok(json!({
        "checkpointed": checkpointed,
        "db_path": abs_path,
        "workspace_instance_id": workspace_instance_id,
        "build_context_hash": build_context_hash,
        "transport": "db_path",
    }))
}

// ======================================================================
// 单元测试
// ======================================================================

#[cfg(test)]
mod tests {
    use super::*;

    /// 建最小 schema 临时库并返回路径（SRV-006 查询所需全部表）。
    fn tmp_db(tag: &str) -> (Connection, String) {
        let path = std::env::temp_dir().join(format!(
            "srv006_{}_{}_{}.db",
            tag,
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ));
        let p = path.to_string_lossy().into_owned();
        let conn = Connection::open(&p).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT, \
             created_at REAL, is_active INTEGER DEFAULT 0); \
             CREATE TABLE file_instances (id INTEGER PRIMARY KEY, workspace_id INTEGER, \
             rel_path TEXT, abs_path TEXT, status TEXT DEFAULT 'active'); \
             CREATE TABLE symbols (id INTEGER PRIMARY KEY, file_instance_id INTEGER, \
             symbol_hash TEXT, name TEXT, kind TEXT, visibility TEXT DEFAULT 'private', \
             start_line INTEGER, end_line INTEGER, start_col INTEGER DEFAULT 0, \
             end_col INTEGER DEFAULT 0, signature TEXT DEFAULT '', has_comment INTEGER DEFAULT 0, \
             comment_status TEXT DEFAULT 'pending', module_path TEXT DEFAULT '', \
             qualified_name TEXT DEFAULT '', depth INTEGER DEFAULT -1); \
             CREATE TABLE calls (id INTEGER PRIMARY KEY, caller_id INTEGER, caller_name TEXT, \
             caller_module TEXT, callee_name TEXT, callee_module TEXT DEFAULT '', \
             callee_qualified TEXT DEFAULT '', callee_file TEXT DEFAULT '', \
             callee_id INTEGER DEFAULT 0, call_line INTEGER DEFAULT 0, \
             is_cross_file INTEGER DEFAULT 0); \
             CREATE TABLE file_versions (id INTEGER PRIMARY KEY, file_instance_id INTEGER, \
             is_current INTEGER DEFAULT 1); \
             CREATE TABLE symbol_contents (content_hash TEXT PRIMARY KEY, name TEXT, \
             kind TEXT, signature TEXT DEFAULT '', has_comment INTEGER DEFAULT 0, \
             comment_content TEXT DEFAULT ''); \
             CREATE TABLE file_symbol_versions (file_version_id INTEGER, symbol_hash TEXT, \
             qualified_name TEXT, module_path TEXT DEFAULT '', start_line INTEGER DEFAULT 0, \
             end_line INTEGER DEFAULT 0, depth INTEGER DEFAULT 0, is_deleted INTEGER DEFAULT 0); \
             CREATE TABLE call_versions (file_version_id INTEGER, caller_qualified TEXT, \
             caller_hash TEXT DEFAULT '', callee_name TEXT DEFAULT '', callee_qualified TEXT, \
             callee_module TEXT DEFAULT '', \
             callee_file TEXT DEFAULT '', call_line INTEGER DEFAULT 0);",
        )
        .unwrap();
        (conn, p)
    }

    fn seed_basic(conn: &Connection) {
        conn.execute_batch(
            "INSERT INTO workspaces (id, name, root_path, created_at, is_active) \
             VALUES (7, 'ws7', '/tmp/ws7', 1.0, 1); \
             INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, status) \
             VALUES (1, 7, 'a.py', '/tmp/ws7/a.py', 'active'); \
             INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, start_line, end_line, \
             signature, comment_status, qualified_name, depth) VALUES \
             (1, 1, 'h_main', 'main', 'fn', 1, 10, 'def main()', 'done', 'mod.main', 0), \
             (2, 1, 'h_helper', 'helper', 'fn', 12, 20, 'def helper()', 'pending', 'mod.helper', 1); \
             INSERT INTO calls (caller_id, caller_name, caller_module, callee_name, callee_qualified, \
             callee_id, call_line, is_cross_file) VALUES \
             (1, 'main', 'mod', 'helper', 'mod.helper', 2, 3, 0); \
             INSERT INTO file_versions (id, file_instance_id, is_current) VALUES (1, 1, 1); \
             INSERT INTO symbol_contents (content_hash, name, kind, signature, has_comment, comment_content) \
             VALUES ('h_main', 'main', 'fn', 'def main()', 1, 'entry'); \
             INSERT INTO file_symbol_versions (file_version_id, symbol_hash, qualified_name, start_line, end_line, depth) \
             VALUES (1, 'h_main', 'mod.main', 1, 10, 0); \
             INSERT INTO call_versions (file_version_id, caller_qualified, callee_qualified, call_line) VALUES \
             (1, 'mod.main', 'mod.helper', 3), \
             (1, 'mod.helper', 'mod.main', 9);",
        )
        .unwrap();
    }

    fn params_with_db(path: &str, extra: Value) -> Value {
        let mut m = match extra {
            Value::Object(m) => m,
            _ => Map::new(),
        };
        m.insert("db_path".to_string(), json!(path));
        Value::Object(m)
    }

    #[test]
    fn test_get_db_returns_path_meta() {
        let res = handle_get_db(&json!({"db_path": "/nonexistent/srv006_x.db"})).unwrap();
        assert_eq!(res["exists"], json!(false));
        assert_eq!(res["db_path"], json!("/nonexistent/srv006_x.db"));
    }

    #[test]
    fn test_inject_short_circuit_existing_workspace_id() {
        let res = handle_inject_workspace_id(&json!({"params": {"workspace_id": 42}})).unwrap();
        assert_eq!(res["injected"], json!(false));
        assert_eq!(res["params"]["workspace_id"], json!(42));
    }

    #[test]
    fn test_inject_fail_closed_no_active_workspace() {
        let (_conn, path) = tmp_db("inject_empty");
        let res = handle_inject_workspace_id(&params_with_db(&path, json!({"params": {}})));
        assert!(res.is_err());
        assert_eq!(res.unwrap_err().code, "internal_error");
    }

    #[test]
    fn test_inject_resolves_active_workspace() {
        let (conn, path) = tmp_db("inject_ok");
        seed_basic(&conn);
        let res =
            handle_inject_workspace_id(&params_with_db(&path, json!({"params": {"q": 1}}))).unwrap();
        assert_eq!(res["injected"], json!(true));
        assert_eq!(res["params"]["workspace_id"], json!(7));
    }

    #[test]
    fn test_callers_short_and_auto_qn() {
        let (conn, path) = tmp_db("callers");
        seed_basic(&conn);
        let res = handle_sql_fallback_get_callers(&params_with_db(
            &path,
            json!({"callee_name": "helper"}),
        ))
        .unwrap();
        assert_eq!(res["callers"].as_array().unwrap().len(), 1);
        assert_eq!(res["callers"][0]["caller_name"], json!("main"));
        // QN 自动识别（mod.helper → 短名 helper 命中）
        let res = handle_sql_fallback_get_callers(&params_with_db(
            &path,
            json!({"callee_name": "mod.helper"}),
        ))
        .unwrap();
        assert_eq!(res["callers"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn test_callers_explicit_qn_not_found_no_fallback() {
        let (conn, path) = tmp_db("callers_qn");
        seed_basic(&conn);
        let res = handle_sql_fallback_get_callers(&params_with_db(
            &path,
            json!({"callee_name": "helper", "qualified_name": "no.such.qn"}),
        ))
        .unwrap();
        assert_eq!(res["callers"], json!([]));
    }

    #[test]
    fn test_callers_missing_param_invalid_params() {
        let res = handle_sql_fallback_get_callers(&json!({}));
        assert!(res.is_err());
        assert_eq!(res.unwrap_err().code, "invalid_params");
    }

    #[test]
    fn test_callees_by_name() {
        let (conn, path) = tmp_db("callees");
        seed_basic(&conn);
        let res = handle_sql_fallback_get_callees(&params_with_db(
            &path,
            json!({"caller_name": "main"}),
        ))
        .unwrap();
        assert_eq!(res["callees"].as_array().unwrap().len(), 1);
        assert_eq!(res["callees"][0]["callee_name"], json!("helper"));
    }

    #[test]
    fn test_search_like_fallback_short_query() {
        let (conn, path) = tmp_db("search");
        seed_basic(&conn);
        // query < 3 字符 → 走 LIKE 路径（fsv/sc 视图）
        let res = handle_sql_fallback_search_symbols(&params_with_db(
            &path,
            json!({"query": "ma", "limit": 10}),
        ))
        .unwrap();
        let syms = res["symbols"].as_array().unwrap();
        assert_eq!(syms.len(), 1);
        assert_eq!(syms[0]["qualified_name"], json!("mod.main"));
    }

    #[test]
    fn test_get_symbol_full_and_missing() {
        let (conn, path) = tmp_db("symbol");
        seed_basic(&conn);
        let res = handle_sql_fallback_get_symbol(&params_with_db(
            &path,
            json!({"qualified_name": "mod.main"}),
        ))
        .unwrap();
        let sym = &res["symbol"];
        assert_eq!(sym["name"], json!("main"));
        assert_eq!(sym["calls_out"][0]["target_name"], json!("mod.helper"));
        assert_eq!(sym["called_by"][0]["caller_name"], json!("mod.helper"));
        assert_eq!(sym["applicable_rules"], json!([]));
        assert_eq!(sym["issues_total"], json!(0));
        // 未命中 → symbol 为 null
        let res = handle_sql_fallback_get_symbol(&params_with_db(
            &path,
            json!({"qualified_name": "no.such"}),
        ))
        .unwrap();
        assert_eq!(res["symbol"], Value::Null);
    }

    #[test]
    fn test_stats_basic() {
        let (conn, path) = tmp_db("stats");
        seed_basic(&conn);
        let res = handle_sql_fallback_get_stats(&params_with_db(&path, json!({}))).unwrap();
        let s = &res["stats"];
        assert_eq!(s["total_files"], json!(1));
        assert_eq!(s["total_symbols"], json!(2));
        assert_eq!(s["total_calls"], json!(1));
        assert_eq!(s["commented"], json!(1));
        assert_eq!(s["by_kind"]["fn"], json!(2));
        assert_eq!(s["depth_distribution"]["0"], json!(1));
    }

    #[test]
    fn test_topological_order_depth_asc() {
        let (conn, path) = tmp_db("topo");
        seed_basic(&conn);
        let res = handle_sql_fallback_get_topological_order(
            &params_with_db(&path, json!({"limit": 10})),
        )
        .unwrap();
        let order = res["order"].as_array().unwrap();
        assert_eq!(order.len(), 2);
        assert_eq!(order[0]["qualified_name"], json!("mod.main")); // depth 0 在前
        assert_eq!(order[1]["qualified_name"], json!("mod.helper"));
    }

    #[test]
    fn test_call_chain_down_bfs() {
        let (conn, path) = tmp_db("chain");
        seed_basic(&conn);
        let res = handle_sql_fallback_get_call_chain_down(&params_with_db(
            &path,
            json!({"qualified_name": "mod.main", "max_depth": 3}),
        ))
        .unwrap();
        // main → helper → main（已访问不重复入队）
        assert_eq!(res["total_downstream"], json!(1));
        assert_eq!(res["edges"].as_array().unwrap().len(), 1);
        assert_eq!(res["edges"][0]["callee"], json!("mod.helper"));
    }

    #[test]
    fn test_detect_cycles_pair() {
        let (conn, path) = tmp_db("cycles");
        seed_basic(&conn);
        let res =
            handle_sql_fallback_detect_cycles(&params_with_db(&path, json!({"max_depth": 10})))
                .unwrap();
        let cycles = res["cycles"].as_array().unwrap();
        assert_eq!(cycles.len(), 1);
        // 归一化：最小元素开头，末尾闭合
        let arr = cycles[0].as_array().unwrap();
        assert_eq!(arr[0], arr[arr.len() - 1]);
        assert!(arr.contains(&json!("mod.helper")));
        assert!(arr.contains(&json!("mod.main")));
    }

    #[test]
    fn test_normalize_cycle_rotation() {
        let cyc = vec![
            "c".to_string(),
            "a".to_string(),
            "b".to_string(),
            "c".to_string(),
        ];
        let norm = normalize_cycle(&cyc);
        assert_eq!(
            norm,
            vec![
                "a".to_string(),
                "b".to_string(),
                "c".to_string(),
                "a".to_string()
            ]
        );
    }

    #[test]
    fn test_build_fts_query_filters_short_tokens() {
        assert_eq!(build_fts_query("ab"), None);
        assert_eq!(
            build_fts_query("login_handler"),
            Some("\"login_handler\"".to_string())
        );
        assert_eq!(build_fts_query("ab login"), Some("\"login\"".to_string()));
    }

    #[test]
    fn test_call_with_fd_platform_probe() {
        let res = handle_call_with_fd(&json!({"method": "snapshot.publish"})).unwrap();
        #[cfg(unix)]
        assert_eq!(res["supported"], json!(true));
        #[cfg(not(unix))]
        assert_eq!(res["supported"], json!(false));
    }

    #[test]
    fn test_publish_snapshot_checkpoint_and_payload() {
        let (conn, path) = tmp_db("publish");
        conn.execute_batch("PRAGMA journal_mode=WAL;").unwrap();
        conn.execute_batch("CREATE TABLE t (x INTEGER); INSERT INTO t VALUES (1);")
            .unwrap();
        drop(conn);
        let res = handle_publish_snapshot(&json!({
            "workspace_instance_id": "ws-inst-1",
            "db_path": path,
            "build_context_hash": "abc",
        }))
        .unwrap();
        assert_eq!(res["checkpointed"], json!(true));
        assert_eq!(res["workspace_instance_id"], json!("ws-inst-1"));
        assert_eq!(res["build_context_hash"], json!("abc"));
        assert_eq!(res["transport"], json!("db_path"));
    }

    #[test]
    fn test_publish_snapshot_missing_param() {
        let res = handle_publish_snapshot(&json!({"db_path": "/tmp/x.db"}));
        assert!(res.is_err());
        assert_eq!(res.unwrap_err().code, "invalid_params");
    }

    #[test]
    fn test_workspace_id_zero_rejected() {
        let (conn, path) = tmp_db("ws_zero");
        seed_basic(&conn);
        let res =
            handle_sql_fallback_get_stats(&params_with_db(&path, json!({"workspace_id": 0})));
        assert!(res.is_err());
        assert_eq!(res.unwrap_err().code, "invalid_params");
    }
}
