//! CAS → CodeGraph DB merge 层（P0-2 子问题1 修复，2026-07-22）。
//!
//! 对应 Python `db/db_cas_merge.py:merge_cas_to_codegraph`。
//!
//! 复审报告 §3 P0-2 子问题1：daemon_handle_refresh CAS committed 后，
//! CAS 中的解析结果（`cas_symbols` / `cas_raw_calls` / `cas_file_cache`）
//! 从未 merge 到主 CodeGraph DB（`~/.callwarden/callwarden.db`）的
//! `file_instances` / `symbols` / `calls` 表，导致 `publish_snapshot` 加载到
//! GraphSnapshot 的是 STALE 数据。
//!
//! 本模块实现最小侵入的 merge：
//! 1. UPSERT `workspaces`（按 workspace_id 数字主键，name 用 `daemon_ws_{id}`）
//! 2. UPSERT `file_contents`（content_hash 主键）
//! 3. UPSERT `file_instances`（workspace_id + rel_path 唯一）
//! 4. DELETE 旧 `symbols` WHERE file_instance_id = ?
//! 5. INSERT 新 `symbols`（从 cas_symbols 读）
//! 6. DELETE 旧 `calls` WHERE caller_id IN (旧 symbol_ids)
//! 7. INSERT 新 `calls`（从 cas_raw_calls 读，caller_id 关联到新 symbols.id）
//!
//! **范围说明**（与 Python 一致）：
//! - 跨文件调用解析（resolve）不在本步骤范围；cas_raw_calls 只含单文件内调用
//! - file_versions 历史版本不在本步骤写入
//! - lexical_parent_local_id 不转换（CodeGraph DB symbols 表无对应字段）

use std::collections::HashMap;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension};

/// merge 结果（对应 Python `merge_cas_to_codegraph` 返回 dict）
#[derive(Debug, Clone)]
pub struct MergeResult {
    pub cas_key: String,
    pub workspace_id: i64,
    pub file_instance_id: i64,
    pub symbols_inserted: usize,
    pub calls_inserted: usize,
    /// "merged" / "cas_miss" / "no_symbols"
    pub merge_status: String,
}

/// 当前时间戳（Unix epoch 秒，f64）
fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 从 rel_path 推导 module_path（简化版，与 Python `_module_path_from_rel` 一致）。
///
/// 完整实现见 `db_build.py:_infer_module_path_generic`（含 src/lib/app/main 前缀
/// 去除、index/__init__ 处理）。P0-2 整改使用简化版，避免引入 db_build.py 依赖。
fn module_path_from_rel(rel_path: &str) -> String {
    // 统一斜杠方向（Windows 路径兼容）
    let path = rel_path.replace('\\', "/");
    // 去掉扩展名（仅最后一个 .）
    let path = match path.rfind('.') {
        // 仅当 . 出现在 basename 内才去除（避免误伤目录名中的 .）
        Some(dot_idx) => {
            let last_sep = path.rfind('/').map(|s| s + 1).unwrap_or(0);
            if dot_idx > last_sep {
                &path[..dot_idx]
            } else {
                &path[..]
            }
        }
        None => &path[..],
    };
    path.replace('/', ".")
}

/// 确保 CodeGraph DB 中有对应 workspace_id 的 workspaces 行。
///
/// INSERT OR IGNORE：若 workspace_id 已存在则跳过（不覆盖 name/root_path，
/// 避免与 CLI `cw --workspace` 注册的 workspace 冲突）。
fn ensure_workspace_row(
    codegraph_conn: &Connection,
    workspace_id: i64,
    root_path: &str,
) -> Result<(), rusqlite::Error> {
    let now = now_ts();
    let name = format!("daemon_ws_{}", workspace_id);
    codegraph_conn.execute(
        "INSERT OR IGNORE INTO workspaces \
         (id, name, root_path, created_at, is_active, description) \
         VALUES (?1, ?2, ?3, ?4, 0, 'daemon-managed workspace')",
        params![workspace_id, name, root_path, now],
    )?;
    Ok(())
}

/// UPSERT file_contents + file_instances，返回 file_instance_id。
///
/// - file_contents：INSERT OR REPLACE（content_hash 主键）
/// - file_instances：先 SELECT 现有 id，存在则 UPDATE，否则 INSERT
fn upsert_file_records(
    codegraph_conn: &Connection,
    workspace_id: i64,
    rel_path: &str,
    abs_path: &str,
    content_hash: &str,
    language: &str,
    total_lines: i64,
) -> Result<i64, rusqlite::Error> {
    let now = now_ts();
    let module_path = module_path_from_rel(rel_path);

    // file_contents：INSERT OR REPLACE
    codegraph_conn.execute(
        "INSERT OR REPLACE INTO file_contents \
         (content_hash, language, total_lines, first_seen_at) \
         VALUES (?1, ?2, ?3, ?4)",
        params![content_hash, language, total_lines, now],
    )?;

    // file_instances：先查现有 id
    let existing_id: Option<i64> = codegraph_conn
        .query_row(
            "SELECT id FROM file_instances WHERE workspace_id = ?1 AND rel_path = ?2",
            params![workspace_id, rel_path],
            |row| row.get(0),
        )
        .optional()?;

    if let Some(id) = existing_id {
        // UPDATE 现有行
        codegraph_conn.execute(
            "UPDATE file_instances SET \
             abs_path = ?1, current_content_hash = ?2, mtime = ?3, \
             total_lines = ?4, last_parsed = ?5, status = 'parsed', module_path = ?6 \
             WHERE id = ?7",
            params![abs_path, content_hash, now, total_lines, now, module_path, id],
        )?;
        Ok(id)
    } else {
        // INSERT 新行
        codegraph_conn.execute(
            "INSERT INTO file_instances \
             (workspace_id, rel_path, abs_path, current_content_hash, mtime, \
             total_lines, last_parsed, status, module_path) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'parsed', ?8)",
            params![
                workspace_id,
                rel_path,
                abs_path,
                content_hash,
                now,
                total_lines,
                now,
                module_path,
            ],
        )?;
        Ok(codegraph_conn.last_insert_rowid())
    }
}

/// CAS symbols 行（从 cas_symbols 读出）
struct CasSymbolRow {
    local_symbol_id: i64,
    symbol_content_hash: String,
    name: String,
    local_qualified_name: String,
    kind: String,
    start_line: i64,
    end_line: i64,
    start_col: i64,
    end_col: i64,
    visibility: String,
    signature: String,
    has_comment: i64,
    depth: i64,
}

/// CAS raw calls 行（从 cas_raw_calls 读出）
struct CasRawCallRow {
    caller_local_id: Option<i64>,
    caller_name: String,
    callee_name: String,
    call_line: i64,
}

/// 替换 file_instance 对应的 symbols + calls。
///
/// 流程：
/// 1. 查询旧 symbol_ids
/// 2. DELETE calls WHERE caller_id IN (旧 symbol_ids)
/// 2b. UPDATE calls SET callee_id=0 WHERE callee_id IN (旧 symbol_ids)（入边清理）
/// 3. DELETE symbols WHERE file_instance_id = ?
/// 4. INSERT 新 symbols（构建 local_symbol_id → 全局 id 映射）
/// 5. INSERT 新 calls（caller_id 关联到新 symbols.id）
///
/// 返回 (inserted_symbols, inserted_calls)。
fn replace_symbols_and_calls(
    codegraph_conn: &Connection,
    file_instance_id: i64,
    cas_symbols: &[CasSymbolRow],
    cas_raw_calls: &[CasRawCallRow],
    rel_path: &str,
) -> Result<(usize, usize), rusqlite::Error> {
    let module_path = module_path_from_rel(rel_path);

    // 1. 查询旧 symbol_ids
    let mut old_sym_ids: Vec<i64> = Vec::new();
    {
        let mut stmt = codegraph_conn.prepare(
            "SELECT id FROM symbols WHERE file_instance_id = ?1",
        )?;
        let rows = stmt.query_map(params![file_instance_id], |row| row.get::<_, i64>(0))?;
        for r in rows {
            old_sym_ids.push(r?);
        }
    }

    // 2. 删除旧 calls（出边 + 入边清理）
    if !old_sym_ids.is_empty() {
        // DELETE calls WHERE caller_id IN (...)（出边）
        delete_calls_by_caller_ids(codegraph_conn, &old_sym_ids)?;
        // UPDATE calls SET callee_id=0 WHERE callee_id IN (...)（入边置 0）
        clear_callee_ids(codegraph_conn, &old_sym_ids)?;
    }

    // 3. 删除旧 symbols
    codegraph_conn.execute(
        "DELETE FROM symbols WHERE file_instance_id = ?1",
        params![file_instance_id],
    )?;

    // 4. INSERT 新 symbols，构建 local_symbol_id → 全局 id 映射
    let mut local_to_global: HashMap<i64, i64> = HashMap::new();
    for sym in cas_symbols {
        // UPSERT symbol_contents（INSERT OR IGNORE）
        // symbols.symbol_hash 指向 symbol_contents.content_hash，
        // 未写入 symbol_contents 会导致 JOIN 查询断链
        if !sym.symbol_content_hash.is_empty() {
            codegraph_conn.execute(
                "INSERT OR IGNORE INTO symbol_contents \
                 (content_hash, name, kind, content, signature, has_comment, \
                 comment_content, qualified_name) \
                 VALUES (?1, ?2, ?3, '', ?4, ?5, '', ?6)",
                params![
                    &sym.symbol_content_hash,
                    &sym.name,
                    &sym.kind,
                    &sym.signature,
                    sym.has_comment,
                    &sym.local_qualified_name,
                ],
            )?;
        }

        codegraph_conn.execute(
            "INSERT INTO symbols \
             (file_instance_id, symbol_hash, name, kind, visibility, \
             start_line, end_line, start_col, end_col, signature, has_comment, \
             comment_status, module_path, qualified_name, depth) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, 'pending', ?12, ?13, ?14)",
            params![
                file_instance_id,
                &sym.symbol_content_hash,
                &sym.name,
                &sym.kind,
                &sym.visibility,
                sym.start_line,
                sym.end_line,
                sym.start_col,
                sym.end_col,
                &sym.signature,
                sym.has_comment,
                module_path,
                &sym.local_qualified_name,
                sym.depth,
            ],
        )?;
        let new_id = codegraph_conn.last_insert_rowid();
        local_to_global.insert(sym.local_symbol_id, new_id);
    }

    // 5. INSERT 新 calls
    let mut inserted_calls: usize = 0;
    for call in cas_raw_calls {
        let caller_global_id = call
            .caller_local_id
            .and_then(|lid| local_to_global.get(&lid).copied())
            .unwrap_or(0);
        codegraph_conn.execute(
            "INSERT INTO calls \
             (caller_id, caller_name, caller_module, callee_name, callee_module, \
             callee_file, callee_id, call_line, is_cross_file) \
             VALUES (?1, ?2, ?3, ?4, '', ?5, 0, ?6, 0)",
            params![
                caller_global_id,
                &call.caller_name,
                module_path,
                &call.callee_name,
                rel_path,
                call.call_line,
            ],
        )?;
        inserted_calls += 1;
    }

    Ok((cas_symbols.len(), inserted_calls))
}

/// DELETE calls WHERE caller_id IN (ids)
fn delete_calls_by_caller_ids(conn: &Connection, ids: &[i64]) -> Result<(), rusqlite::Error> {
    // 分批处理，避免 SQL 参数数量上限（SQLITE_MAX_VARIABLE_NUMBER，默认 999）
    for chunk in ids.chunks(500) {
        if chunk.is_empty() {
            continue;
        }
        let placeholders: Vec<String> = (0..chunk.len())
            .map(|i| format!("?{}", i + 1))
            .collect();
        let sql = format!(
            "DELETE FROM calls WHERE caller_id IN ({})",
            placeholders.join(",")
        );
        let params_iter: Vec<&dyn rusqlite::ToSql> = chunk
            .iter()
            .map(|id| id as &dyn rusqlite::ToSql)
            .collect();
        conn.execute(&sql, params_iter.as_slice())?;
    }
    Ok(())
}

/// UPDATE calls SET callee_id=0 WHERE callee_id IN (ids)
fn clear_callee_ids(conn: &Connection, ids: &[i64]) -> Result<(), rusqlite::Error> {
    for chunk in ids.chunks(500) {
        if chunk.is_empty() {
            continue;
        }
        let placeholders: Vec<String> = (0..chunk.len())
            .map(|i| format!("?{}", i + 1))
            .collect();
        let sql = format!(
            "UPDATE calls SET callee_id = 0 WHERE callee_id IN ({})",
            placeholders.join(",")
        );
        let params_iter: Vec<&dyn rusqlite::ToSql> = chunk
            .iter()
            .map(|id| id as &dyn rusqlite::ToSql)
            .collect();
        conn.execute(&sql, params_iter.as_slice())?;
    }
    Ok(())
}

/// 把 CAS 中的解析结果 merge 到 CodeGraph DB 主表。
///
/// 对应 Python `db/db_cas_merge.py:merge_cas_to_codegraph`。
/// 事务在 CodeGraph DB 上执行（`BEGIN IMMEDIATE` + `COMMIT`），CAS DB 只读。
///
/// 流程：
/// 1. 查 CAS file cache（取 total_lines）—— miss 则返回 `cas_miss`
/// 2. 查 CAS symbols / cas_raw_calls
/// 3. UPSERT workspaces（INSERT OR IGNORE）
/// 4. UPSERT file_contents + file_instances
/// 5. 替换 symbols + calls（DELETE 旧 + INSERT 新）
/// 6. COMMIT
///
/// 参数：
/// - `cas_conn`: CAS 数据库连接（只读）
/// - `codegraph_conn`: 主 CodeGraph DB 连接（写）
/// - `cas_key`: CAS key
/// - `workspace_id`: 数字 workspace_id
/// - `rel_path` / `abs_path`: 文件相对路径 / 绝对路径
/// - `content_hash`: 文件内容 SHA-256
/// - `language`: 语言 ID
/// - `workspace_root_path`: workspace 根路径（用于 workspaces.root_path）
///
/// 返回 `MergeResult`，失败返回 `Err(String)`。
pub fn merge_cas_to_codegraph(
    cas_conn: &Connection,
    codegraph_conn: &Connection,
    cas_key: &str,
    workspace_id: i64,
    rel_path: &str,
    abs_path: &str,
    content_hash: &str,
    language: &str,
    workspace_root_path: &str,
) -> Result<MergeResult, String> {
    // 1. 查 CAS file cache（取 total_lines）
    let total_lines: i64 = match cas_conn
        .query_row(
            "SELECT file_size, total_lines FROM cas_file_cache WHERE cas_key = ?1",
            params![cas_key],
            |row| row.get::<_, i64>(1),
        )
        .optional()
    {
        Ok(Some(v)) => v,
        Ok(None) => {
            return Ok(MergeResult {
                cas_key: cas_key.to_string(),
                workspace_id,
                file_instance_id: 0,
                symbols_inserted: 0,
                calls_inserted: 0,
                merge_status: "cas_miss".to_string(),
            });
        }
        Err(e) => {
            return Err(format!("查询 cas_file_cache 失败: {}", e));
        }
    };

    // 2. 查 CAS symbols
    let mut cas_symbols: Vec<CasSymbolRow> = Vec::new();
    {
        let mut stmt = cas_conn
            .prepare(
                "SELECT local_symbol_id, symbol_content_hash, name, local_qualified_name, \
                 kind, start_line, end_line, start_col, end_col, visibility, signature, \
                 has_comment, depth \
                 FROM cas_symbols WHERE cas_key = ?1 ORDER BY local_symbol_id",
            )
            .map_err(|e| format!("prepare cas_symbols 查询失败: {}", e))?;
        let rows = stmt
            .query_map(params![cas_key], |row| {
                Ok(CasSymbolRow {
                    local_symbol_id: row.get(0)?,
                    symbol_content_hash: row.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    name: row.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    local_qualified_name: row.get::<_, Option<String>>(3)?.unwrap_or_default(),
                    kind: row
                        .get::<_, Option<String>>(4)?
                        .unwrap_or_else(|| "function".to_string()),
                    start_line: row.get(5)?,
                    end_line: row.get(6)?,
                    start_col: row.get(7)?,
                    end_col: row.get(8)?,
                    visibility: row
                        .get::<_, Option<String>>(9)?
                        .unwrap_or_else(|| "private".to_string()),
                    signature: row.get::<_, Option<String>>(10)?.unwrap_or_default(),
                    has_comment: row.get(11)?,
                    depth: row
                        .get::<_, Option<i64>>(12)?
                        .unwrap_or(-1),
                })
            })
            .map_err(|e| format!("query cas_symbols 失败: {}", e))?;
        for r in rows {
            cas_symbols.push(r.map_err(|e| format!("row cas_symbols 失败: {}", e))?);
        }
    }

    // 3. 查 CAS raw calls
    let mut cas_raw_calls: Vec<CasRawCallRow> = Vec::new();
    {
        let mut stmt = cas_conn
            .prepare(
                "SELECT caller_local_id, caller_name, callee_name, call_line \
                 FROM cas_raw_calls WHERE cas_key = ?1",
            )
            .map_err(|e| format!("prepare cas_raw_calls 查询失败: {}", e))?;
        let rows = stmt
            .query_map(params![cas_key], |row| {
                Ok(CasRawCallRow {
                    caller_local_id: row.get(0)?,
                    caller_name: row.get::<_, Option<String>>(1)?.unwrap_or_default(),
                    callee_name: row.get::<_, Option<String>>(2)?.unwrap_or_default(),
                    call_line: row.get(3)?,
                })
            })
            .map_err(|e| format!("query cas_raw_calls 失败: {}", e))?;
        for r in rows {
            cas_raw_calls.push(r.map_err(|e| format!("row cas_raw_calls 失败: {}", e))?);
        }
    }

    // 4-6. 在 CodeGraph DB 上执行事务
    //     BEGIN IMMEDIATE → UPSERT workspaces / file_records → 替换 symbols/calls → COMMIT
    if let Err(e) = codegraph_conn.execute_batch("BEGIN IMMEDIATE") {
        return Err(format!("BEGIN IMMEDIATE 失败: {}", e));
    }

    let inner = || -> Result<(i64, usize, usize, String), String> {
        // 4. 确保 workspace 行
        ensure_workspace_row(codegraph_conn, workspace_id, workspace_root_path)
            .map_err(|e| format!("ensure_workspace_row 失败: {}", e))?;

        // 5. UPSERT file_contents + file_instances
        let file_instance_id = upsert_file_records(
            codegraph_conn,
            workspace_id,
            rel_path,
            abs_path,
            content_hash,
            language,
            total_lines,
        )
        .map_err(|e| format!("upsert_file_records 失败: {}", e))?;

        // 6. 替换 symbols + calls
        let (sym_count, call_count) = replace_symbols_and_calls(
            codegraph_conn,
            file_instance_id,
            &cas_symbols,
            &cas_raw_calls,
            rel_path,
        )
        .map_err(|e| format!("replace_symbols_and_calls 失败: {}", e))?;

        let merge_status = if cas_symbols.is_empty() {
            "no_symbols"
        } else {
            "merged"
        };

        Ok((file_instance_id, sym_count, call_count, merge_status.to_string()))
    };

    match inner() {
        Ok((file_instance_id, sym_count, call_count, merge_status)) => {
            if let Err(e) = codegraph_conn.execute_batch("COMMIT") {
                let _ = codegraph_conn.execute_batch("ROLLBACK");
                return Err(format!("COMMIT 失败: {}", e));
            }
            Ok(MergeResult {
                cas_key: cas_key.to_string(),
                workspace_id,
                file_instance_id,
                symbols_inserted: sym_count,
                calls_inserted: call_count,
                merge_status,
            })
        }
        Err(e) => {
            let _ = codegraph_conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
}

// ============================================
// 测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    /// 创建 CodeGraph DB schema（与 db/schema.py 对齐）
    fn make_codegraph_schema(conn: &Connection) {
        // 生产环境 Python sqlite3 默认 PRAGMA foreign_keys=OFF，
        // FK 约束仅为 schema 信息标注，不强制执行。测试中也关闭以匹配生产行为。
        conn.execute_batch("PRAGMA foreign_keys=OFF;").unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                root_path TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL,
                is_active INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                active_task_id TEXT DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS file_contents (
                content_hash TEXT PRIMARY KEY,
                language TEXT DEFAULT '',
                total_lines INTEGER DEFAULT 0,
                first_seen_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS file_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                abs_path TEXT NOT NULL,
                current_content_hash TEXT DEFAULT '',
                mtime REAL NOT NULL,
                total_lines INTEGER DEFAULT 0,
                last_parsed REAL DEFAULT 0,
                status TEXT DEFAULT 'pending',
                module_path TEXT DEFAULT '',
                FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
                FOREIGN KEY (current_content_hash) REFERENCES file_contents(content_hash),
                UNIQUE(workspace_id, rel_path)
            );
            CREATE TABLE IF NOT EXISTS symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_instance_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                visibility TEXT DEFAULT 'private',
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                start_col INTEGER DEFAULT 0,
                end_col INTEGER DEFAULT 0,
                signature TEXT DEFAULT '',
                has_comment INTEGER DEFAULT 0,
                comment_status TEXT DEFAULT 'pending',
                module_path TEXT DEFAULT '',
                qualified_name TEXT DEFAULT '',
                depth INTEGER DEFAULT -1,
                FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
            );
            CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_id INTEGER NOT NULL,
                caller_name TEXT NOT NULL,
                caller_module TEXT NOT NULL,
                callee_name TEXT NOT NULL,
                callee_module TEXT DEFAULT '',
                callee_qualified TEXT DEFAULT '',
                callee_file TEXT DEFAULT '',
                callee_id INTEGER DEFAULT 0,
                call_line INTEGER DEFAULT 0,
                is_cross_file INTEGER DEFAULT 0,
                FOREIGN KEY (caller_id) REFERENCES symbols(id)
            );
            CREATE TABLE IF NOT EXISTS symbol_contents (
                content_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                signature TEXT DEFAULT '',
                has_comment INTEGER DEFAULT 0,
                comment_content TEXT DEFAULT '',
                qualified_name TEXT DEFAULT ''
            );
            "#,
        )
        .unwrap();
    }

    /// 创建 CAS DB schema（与 cas.rs:CAS_SCHEMA_DDL 对齐）
    fn make_cas_schema(conn: &Connection) {
        conn.execute_batch(
            r#"
            CREATE TABLE IF NOT EXISTS cas_file_cache (
                cas_key TEXT PRIMARY KEY,
                content_hash TEXT NOT NULL,
                language TEXT NOT NULL,
                file_size INTEGER DEFAULT 0,
                total_lines INTEGER DEFAULT 0,
                parser_version TEXT NOT NULL,
                callwarden_version TEXT NOT NULL,
                extraction_config_version TEXT NOT NULL,
                abi_version TEXT NOT NULL,
                input_abi_version TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'ready',
                parsed_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS cas_symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cas_key TEXT NOT NULL,
                local_symbol_id INTEGER NOT NULL,
                symbol_content_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                local_qualified_name TEXT NOT NULL,
                lexical_parent_local_id INTEGER DEFAULT NULL,
                kind TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                start_col INTEGER DEFAULT 0,
                end_col INTEGER DEFAULT 0,
                start_byte INTEGER DEFAULT 0,
                end_byte INTEGER DEFAULT 0,
                visibility TEXT DEFAULT 'private',
                signature TEXT DEFAULT '',
                has_comment INTEGER DEFAULT 0,
                depth INTEGER DEFAULT -1,
                UNIQUE(cas_key, local_symbol_id)
            );
            CREATE TABLE IF NOT EXISTS cas_raw_calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                cas_key TEXT NOT NULL,
                caller_local_id INTEGER DEFAULT NULL,
                caller_name TEXT NOT NULL,
                callee_name TEXT NOT NULL,
                call_line INTEGER NOT NULL,
                call_ordinal INTEGER DEFAULT 0,
                UNIQUE(cas_key, caller_local_id, call_line, callee_name, call_ordinal)
            );
            "#,
        )
        .unwrap();
    }

    /// 往 CAS DB 插入一个 cas_key + 2 个 symbols + 1 个 raw_call
    fn seed_cas(
        conn: &Connection,
        cas_key: &str,
        content_hash: &str,
        language: &str,
        total_lines: i64,
    ) {
        conn.execute(
            "INSERT INTO cas_file_cache \
             (cas_key, content_hash, language, file_size, total_lines, \
             parser_version, callwarden_version, extraction_config_version, \
             abi_version, input_abi_version, state, parsed_at) \
             VALUES (?1, ?2, ?3, 100, ?4, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
            params![cas_key, content_hash, language, total_lines],
        )
        .unwrap();

        // symbol 1: main function
        conn.execute(
            "INSERT INTO cas_symbols \
             (cas_key, local_symbol_id, symbol_content_hash, name, local_qualified_name, \
             kind, start_line, end_line, start_col, end_col, visibility, signature, \
             has_comment, depth) \
             VALUES (?1, 1, ?2, 'main', 'main', 'function', 1, 5, 0, 0, 'public', \
             'fn main()', 1, 0)",
            params![cas_key, "sym_hash_main"],
        )
        .unwrap();

        // symbol 2: helper function
        conn.execute(
            "INSERT INTO cas_symbols \
             (cas_key, local_symbol_id, symbol_content_hash, name, local_qualified_name, \
             kind, start_line, end_line, start_col, end_col, visibility, signature, \
             has_comment, depth) \
             VALUES (?1, 2, ?2, 'helper', 'main.helper', 'function', 6, 8, 4, 4, 'private', \
             'fn helper()', 0, 1)",
            params![cas_key, "sym_hash_helper"],
        )
        .unwrap();

        // raw call: main calls helper at line 3
        conn.execute(
            "INSERT INTO cas_raw_calls \
             (cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal) \
             VALUES (?1, 1, 'main', 'helper', 3, 0)",
            params![cas_key],
        )
        .unwrap();
    }

    #[test]
    fn test_module_path_from_rel_basic() {
        assert_eq!(module_path_from_rel("src/server/main.py"), "src.server.main");
        assert_eq!(module_path_from_rel("lib/util.rs"), "lib.util");
        // 无扩展名
        assert_eq!(module_path_from_rel("src/server/main"), "src.server.main");
        // Windows 路径
        assert_eq!(module_path_from_rel("src\\server\\main.py"), "src.server.main");
        // 仅 basename
        assert_eq!(module_path_from_rel("main.py"), "main");
        // basename 中无 .
        assert_eq!(module_path_from_rel("src/main"), "src.main");
    }

    #[test]
    fn test_merge_cas_miss() {
        // cas_key 在 CAS DB 中不存在 → cas_miss
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        let result = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "non_existent_key",
            1,
            "src/main.rs",
            "/app/src/main.rs",
            "hash123",
            "rust",
            "/app",
        )
        .expect("merge should not error");

        assert_eq!(result.merge_status, "cas_miss");
        assert_eq!(result.file_instance_id, 0);
        assert_eq!(result.symbols_inserted, 0);
        assert_eq!(result.calls_inserted, 0);
    }

    #[test]
    fn test_merge_full_pipeline() {
        // 完整 merge：2 symbols + 1 call
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        seed_cas(&cas_conn, "ck1", "ch1", "rust", 10);

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        let result = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck1",
            42,
            "src/main.rs",
            "/app/src/main.rs",
            "ch1",
            "rust",
            "/app",
        )
        .expect("merge should succeed");

        assert_eq!(result.merge_status, "merged");
        assert_eq!(result.workspace_id, 42);
        assert!(result.file_instance_id > 0);
        assert_eq!(result.symbols_inserted, 2);
        assert_eq!(result.calls_inserted, 1);

        // 校验 workspaces 行
        let ws_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM workspaces WHERE id = 42",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(ws_count, 1);

        // 校验 file_contents 行
        let fc_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM file_contents WHERE content_hash = 'ch1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(fc_count, 1);

        // 校验 file_instances 行
        let (fi_id, status, total_lines): (i64, String, i64) = cg_conn
            .query_row(
                "SELECT id, status, total_lines FROM file_instances \
                 WHERE workspace_id = 42 AND rel_path = 'src/main.rs'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(fi_id, result.file_instance_id);
        assert_eq!(status, "parsed");
        assert_eq!(total_lines, 10);

        // 校验 symbols 行
        let sym_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM symbols WHERE file_instance_id = ?1",
                params![fi_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(sym_count, 2);

        // 校验 symbol_contents（UPSERT）
        let sc_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM symbol_contents WHERE content_hash IN ('sym_hash_main', 'sym_hash_helper')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(sc_count, 2);

        // 校验 calls 行
        let call_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM calls WHERE caller_id IN \
                 (SELECT id FROM symbols WHERE file_instance_id = ?1)",
                params![fi_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(call_count, 1);

        // 校验 call 的 caller_name 和 callee_name
        let (caller_name, callee_name, call_line): (String, String, i64) = cg_conn
            .query_row(
                "SELECT caller_name, callee_name, call_line FROM calls \
                 WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?1)",
                params![fi_id],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(caller_name, "main");
        assert_eq!(callee_name, "helper");
        assert_eq!(call_line, 3);
    }

    #[test]
    fn test_merge_idempotent_replace() {
        // 二次 merge 同一文件：旧 symbols/calls 应被替换，不累积
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        seed_cas(&cas_conn, "ck1", "ch1", "rust", 10);

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        let r1 = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck1",
            1,
            "src/main.rs",
            "/app/src/main.rs",
            "ch1",
            "rust",
            "/app",
        )
        .unwrap();
        assert_eq!(r1.symbols_inserted, 2);
        assert_eq!(r1.calls_inserted, 1);

        // 再次 merge（同 cas_key 同 file_instance）
        let r2 = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck1",
            1,
            "src/main.rs",
            "/app/src/main.rs",
            "ch1",
            "rust",
            "/app",
        )
        .unwrap();
        assert_eq!(r2.merge_status, "merged");
        assert_eq!(r2.symbols_inserted, 2);
        assert_eq!(r2.calls_inserted, 1);
        // file_instance_id 应保持不变（UPDATE 路径）
        assert_eq!(r1.file_instance_id, r2.file_instance_id);

        // 总 symbols 仍是 2（不累积）
        let sym_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM symbols WHERE file_instance_id = ?1",
                params![r2.file_instance_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(sym_count, 2);

        // 总 calls 仍是 1
        let call_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM calls WHERE caller_id IN \
                 (SELECT id FROM symbols WHERE file_instance_id = ?1)",
                params![r2.file_instance_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(call_count, 1);
    }

    #[test]
    fn test_merge_no_symbols() {
        // CAS 命中但无 symbols：merge_status = no_symbols
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        // 只插入 cas_file_cache，不插入 symbols/calls
        cas_conn
            .execute(
                "INSERT INTO cas_file_cache \
                 (cas_key, content_hash, language, file_size, total_lines, \
                 parser_version, callwarden_version, extraction_config_version, \
                 abi_version, input_abi_version, state, parsed_at) \
                 VALUES ('ck2', 'ch2', 'rust', 0, 0, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
                [],
            )
            .unwrap();

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        let result = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck2",
            1,
            "empty.rs",
            "/app/empty.rs",
            "ch2",
            "rust",
            "/app",
        )
        .unwrap();

        assert_eq!(result.merge_status, "no_symbols");
        assert_eq!(result.symbols_inserted, 0);
        assert_eq!(result.calls_inserted, 0);
        // file_instance 仍应被创建
        assert!(result.file_instance_id > 0);
    }

    #[test]
    fn test_merge_inbound_edge_cleanup() {
        // 验证 P0-2 入边清理：跨 file_instance 的 callee_id 应被置 0
        // 场景：file_instance A 的 symbol X 被 file_instance B 的 call 引用（callee_id=X.id）
        //       重新 merge A 后，指向旧 X.id 的 callee_id 应被置 0
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        seed_cas(&cas_conn, "ck1", "ch1", "rust", 10);

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        // 第一次 merge 创建 file_instance + symbols（包含 main 的 id）
        let r1 = merge_cas_to_codegraph(
            &cas_conn, &cg_conn, "ck1", 1, "src/main.rs", "/app/src/main.rs", "ch1", "rust",
            "/app",
        ).unwrap();

        // 获取 main symbol 的 id
        let main_sym_id: i64 = cg_conn
            .query_row(
                "SELECT id FROM symbols WHERE file_instance_id = ?1 AND name = 'main'",
                params![r1.file_instance_id],
                |row| row.get(0),
            )
            .unwrap();

        // 模拟另一个 file_instance 的 call 引用 main_sym_id 作为 callee_id
        // 先创建第二个 file_instance
        cg_conn
            .execute(
                "INSERT INTO file_instances \
                 (workspace_id, rel_path, abs_path, current_content_hash, mtime, \
                 total_lines, last_parsed, status, module_path) \
                 VALUES (1, 'src/other.rs', '/app/src/other.rs', 'ch_other', 0.0, 5, 0.0, 'parsed', 'src.other')",
                [],
            )
            .unwrap();
        let other_fi_id = cg_conn.last_insert_rowid();
        // 创建一个 symbol 作为 caller
        cg_conn
            .execute(
                "INSERT INTO symbols \
                 (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line, \
                 start_col, end_col, signature, has_comment, comment_status, module_path, \
                 qualified_name, depth) \
                 VALUES (?1, 'caller_hash', 'caller_fn', 'function', 'private', 1, 3, 0, 0, '', 0, 'pending', 'src.other', 'caller_fn', 0)",
                params![other_fi_id],
            )
            .unwrap();
        let caller_id = cg_conn.last_insert_rowid();
        // 创建跨文件 call，callee_id 指向 main_sym_id
        cg_conn
            .execute(
                "INSERT INTO calls \
                 (caller_id, caller_name, caller_module, callee_name, callee_module, \
                 callee_file, callee_id, call_line, is_cross_file) \
                 VALUES (?1, 'caller_fn', 'src.other', 'main', '', 'src/main.rs', ?2, 2, 1)",
                params![caller_id, main_sym_id],
            )
            .unwrap();

        // 再次 merge 同一文件——旧 main_sym_id 会被删除，入边 callee_id 应置 0
        let r2 = merge_cas_to_codegraph(
            &cas_conn, &cg_conn, "ck1", 1, "src/main.rs", "/app/src/main.rs", "ch1", "rust",
            "/app",
        ).unwrap();

        // 验证：旧的 main_sym_id 已不存在
        let old_main_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM symbols WHERE id = ?1",
                params![main_sym_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(old_main_count, 0);

        // 验证：跨文件 call 的 callee_id 应被置 0（入边清理）
        let callee_id_after: i64 = cg_conn
            .query_row(
                "SELECT callee_id FROM calls WHERE caller_id = ?1",
                params![caller_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            callee_id_after, 0,
            "入边 callee_id 应被置 0，避免悬空引用"
        );

        // r2 仍应正常 merge
        assert_eq!(r2.merge_status, "merged");
        assert_eq!(r2.symbols_inserted, 2);
    }

    #[test]
    fn test_merge_workspaces_insert_or_ignore() {
        // 验证 workspaces INSERT OR IGNORE：已存在的 workspace 不被覆盖
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        seed_cas(&cas_conn, "ck1", "ch1", "rust", 10);

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        // 预先插入 workspace_id=1 的行（CLI 注册路径）
        cg_conn
            .execute(
                "INSERT INTO workspaces (id, name, root_path, created_at, is_active, description) \
                 VALUES (1, 'cli_ws_name', '/custom/root', 1000.0, 1, 'CLI registered')",
                [],
            )
            .unwrap();

        // merge 时尝试 INSERT OR IGNORE workspace_id=1
        let _r = merge_cas_to_codegraph(
            &cas_conn, &cg_conn, "ck1", 1, "src/main.rs", "/app/src/main.rs", "ch1", "rust",
            "/app",
        ).unwrap();

        // 验证：workspace 行未被覆盖
        let (name, root_path, description): (String, String, String) = cg_conn
            .query_row(
                "SELECT name, root_path, description FROM workspaces WHERE id = 1",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(name, "cli_ws_name");
        assert_eq!(root_path, "/custom/root");
        assert_eq!(description, "CLI registered");
    }
}
