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

/// 本地增量刷新写入文件版本历史所需的元数据。
#[derive(Debug, Clone)]
pub struct MergeHistoryMetadata {
    pub mtime: f64,
    pub parsed_at: f64,
    pub commit_hash: String,
}

/// workspace 文件 tombstone 的事务结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct DeleteFileResult {
    pub workspace_id: i64,
    pub rel_path: String,
    pub file_instance_id: i64,
    pub symbols_removed: usize,
    pub outgoing_calls_removed: usize,
    pub incoming_edges_cleared: usize,
    pub manifest_removed: usize,
    pub history_versions_marked: usize,
    /// `deleted` / `already_deleted` / `not_found`
    pub delete_status: String,
}

/// 当前时间戳（Unix epoch 秒，f64）
fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// P0-2 v2 修复：初始化 fresh CodeGraph DB 的主 schema。
///
/// 复审报告指出：默认 `codegraph_db_path_template` 非空后，`rusqlite::Connection::open(&db_path)`
/// 只创建空 SQLite 文件，merge 时查询 `workspaces` 等表会报 `no such table`。
///
/// 本函数用 `CREATE TABLE IF NOT EXISTS` 创建主 schema（与 schema.py 对齐），
/// 已存在时无操作，幂等可重复调用。
///
/// 包括：workspaces / file_contents / file_instances / symbols / calls / symbol_contents
/// + 索引 + FTS5 虚拟表与触发器。
///
/// 调用时机：每次打开 CodeGraph DB 后立即调用（CREATE IF NOT EXISTS 保证幂等）。
pub fn init_codegraph_schema(conn: &Connection) -> Result<(), rusqlite::Error> {
    // 先启用外键约束（与 Python 一致）
    conn.execute_batch("PRAGMA foreign_keys = ON;")?;

    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS workspaces (\
            id INTEGER PRIMARY KEY AUTOINCREMENT,\
            name TEXT UNIQUE NOT NULL,\
            root_path TEXT UNIQUE NOT NULL,\
            created_at REAL NOT NULL,\
            is_active INTEGER DEFAULT 0,\
            description TEXT DEFAULT '',\
            active_task_id TEXT DEFAULT ''\
         );\
         CREATE TABLE IF NOT EXISTS file_contents (\
            content_hash TEXT PRIMARY KEY,\
            language TEXT DEFAULT '',\
            total_lines INTEGER DEFAULT 0,\
            first_seen_at REAL NOT NULL\
         );\
         CREATE TABLE IF NOT EXISTS file_instances (\
            id INTEGER PRIMARY KEY AUTOINCREMENT,\
            workspace_id INTEGER NOT NULL,\
            rel_path TEXT NOT NULL,\
            abs_path TEXT NOT NULL,\
            current_content_hash TEXT DEFAULT '',\
            mtime REAL NOT NULL,\
            total_lines INTEGER DEFAULT 0,\
            last_parsed REAL DEFAULT 0,\
            status TEXT DEFAULT 'pending',\
            module_path TEXT DEFAULT '',\
            FOREIGN KEY (workspace_id) REFERENCES workspaces(id),\
            FOREIGN KEY (current_content_hash) REFERENCES file_contents(content_hash),\
            UNIQUE(workspace_id, rel_path)\
         );\
         CREATE TABLE IF NOT EXISTS symbols (\
            id INTEGER PRIMARY KEY AUTOINCREMENT,\
            file_instance_id INTEGER NOT NULL,\
            symbol_hash TEXT NOT NULL,\
            name TEXT NOT NULL,\
            kind TEXT NOT NULL,\
            visibility TEXT DEFAULT 'private',\
            start_line INTEGER NOT NULL,\
            end_line INTEGER NOT NULL,\
            start_col INTEGER DEFAULT 0,\
            end_col INTEGER DEFAULT 0,\
            signature TEXT DEFAULT '',\
            has_comment INTEGER DEFAULT 0,\
            comment_status TEXT DEFAULT 'pending',\
            module_path TEXT DEFAULT '',\
            qualified_name TEXT DEFAULT '',\
            depth INTEGER DEFAULT -1,\
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),\
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)\
         );\
         CREATE TABLE IF NOT EXISTS calls (\
            id INTEGER PRIMARY KEY AUTOINCREMENT,\
            caller_id INTEGER NOT NULL,\
            caller_name TEXT NOT NULL,\
            caller_module TEXT NOT NULL,\
            callee_name TEXT NOT NULL,\
            callee_module TEXT DEFAULT '',\
            callee_qualified TEXT DEFAULT '',\
            callee_file TEXT DEFAULT '',\
            callee_id INTEGER DEFAULT 0,\
            call_line INTEGER DEFAULT 0,\
            is_cross_file INTEGER DEFAULT 0,\
            FOREIGN KEY (caller_id) REFERENCES symbols(id)\
         );\
         CREATE TABLE IF NOT EXISTS symbol_contents (\
            content_hash TEXT PRIMARY KEY,\
            name TEXT NOT NULL,\
            kind TEXT NOT NULL,\
            content TEXT NOT NULL,\
            signature TEXT DEFAULT '',\
            has_comment INTEGER DEFAULT 0,\
            comment_content TEXT DEFAULT '',\
            qualified_name TEXT DEFAULT ''\
         );",
    )?;

    // 索引（IF NOT EXISTS 幂等）
    conn.execute_batch(
        "CREATE INDEX IF NOT EXISTS idx_workspaces_active ON workspaces(is_active);\
         CREATE INDEX IF NOT EXISTS idx_workspaces_active_task ON workspaces(active_task_id);\
         CREATE INDEX IF NOT EXISTS idx_file_contents_lang ON file_contents(language);\
         CREATE INDEX IF NOT EXISTS idx_file_instances_workspace ON file_instances(workspace_id);\
         CREATE INDEX IF NOT EXISTS idx_file_instances_hash ON file_instances(current_content_hash);\
         CREATE INDEX IF NOT EXISTS idx_file_instances_relpath ON file_instances(rel_path);\
         CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_instance_id);\
         CREATE INDEX IF NOT EXISTS idx_symbols_hash ON symbols(symbol_hash);\
         CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);\
         CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);\
         CREATE INDEX IF NOT EXISTS idx_symbols_kind_file ON symbols(kind, file_instance_id);\
         CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified_name);\
         CREATE INDEX IF NOT EXISTS idx_symbols_module ON symbols(module_path);\
         CREATE UNIQUE INDEX IF NOT EXISTS idx_symbols_unique ON symbols(file_instance_id, name, start_line);\
         CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);\
         CREATE INDEX IF NOT EXISTS idx_calls_callee_id_resolved ON calls(callee_id) WHERE callee_id > 0;",
    )?;

    // FTS5 虚拟表与触发器（与 schema.py 对齐，trigram tokenizer）
    // 注意：FTS5 需要 SQLite 编译时启用 SQLITE_ENABLE_FTS5，rusqlite bundled 版本已启用
    // 注意：Rust 字符串字面量用 \ 续行不会自动加空格，BEGIN 后必须显式空格
    conn.execute_batch(
        "CREATE VIRTUAL TABLE IF NOT EXISTS symbols_fts USING fts5(\
            name, qualified_name,\
            content='symbols', content_rowid='id',\
            tokenize='trigram'\
         );\
         CREATE TRIGGER IF NOT EXISTS symbols_fts_ai AFTER INSERT ON symbols BEGIN \
            INSERT INTO symbols_fts(rowid, name, qualified_name) \
            VALUES (new.id, new.name, new.qualified_name); \
         END;\
         CREATE TRIGGER IF NOT EXISTS symbols_fts_ad AFTER DELETE ON symbols BEGIN \
            INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name) \
            VALUES ('delete', old.id, old.name, old.qualified_name); \
         END;\
         CREATE TRIGGER IF NOT EXISTS symbols_fts_au AFTER UPDATE ON symbols BEGIN \
            INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name) \
            VALUES ('delete', old.id, old.name, old.qualified_name); \
            INSERT INTO symbols_fts(rowid, name, qualified_name) \
            VALUES (new.id, new.name, new.qualified_name); \
         END;",
    )?;

    Ok(())
}

/// 从 rel_path 推导 module_path，与 Python 增量刷新契约保持一致。
pub(crate) fn module_path_from_rel(rel_path: &str, language: &str) -> String {
    let mut path = rel_path.replace('\\', "/");

    if language == "rust" {
        if let Some(stripped) = path.strip_prefix("src/") {
            path = stripped.to_string();
        }
        if let Some(stripped) = path.strip_suffix(".rs") {
            path = stripped.to_string();
        }
        if let Some(stripped) = path.strip_suffix("/mod") {
            path = stripped.to_string();
        }
        if path == "lib" || path == "main" {
            return path;
        }
        return format!("lib::{}", path.replace('/', "::"));
    }

    if let Some(dot_idx) = path.rfind('.') {
        let last_sep = path.rfind('/').map(|index| index + 1).unwrap_or(0);
        if dot_idx > last_sep {
            path.truncate(dot_idx);
        }
    }
    for prefix in ["src/", "lib/", "app/", "main/"] {
        if let Some(stripped) = path.strip_prefix(prefix) {
            path = stripped.to_string();
            break;
        }
    }

    let basename = path.rsplit('/').next().unwrap_or_default();
    if matches!(basename, "index" | "__init__" | "mod") {
        path = path
            .rsplit_once('/')
            .map(|(parent, _)| parent.to_string())
            .unwrap_or_else(|| "(root)".to_string());
    }
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
    let module_path = module_path_from_rel(rel_path, language);

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
            params![
                abs_path,
                content_hash,
                now,
                total_lines,
                now,
                module_path,
                id
            ],
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

/// CAS symbols 行（从 cas_symbols 读出，P1-2 修复：含符号正文 content）
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
    /// P1-2 修复：从 cas_symbol_contents JOIN 读出的实际符号源码内容
    content: String,
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
    workspace_id: i64,
    cas_symbols: &[CasSymbolRow],
    cas_raw_calls: &[CasRawCallRow],
    rel_path: &str,
    language: &str,
) -> Result<(usize, usize), rusqlite::Error> {
    let module_path = module_path_from_rel(rel_path, language);

    // 1. 查询旧 symbol_ids
    let mut old_sym_ids: Vec<i64> = Vec::new();
    {
        let mut stmt =
            codegraph_conn.prepare("SELECT id FROM symbols WHERE file_instance_id = ?1")?;
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
    let sym_count = cas_symbols.len(); // 提前保存，避免 for 消费后无法访问
    for sym in cas_symbols {
        // UPSERT symbol_contents（INSERT OR IGNORE + UPDATE 空正文）
        // P1-2 v2 修复：原实现只用 INSERT OR IGNORE，若旧版本已写入空 content 行
        // （content=''),新版本 merge 后 IGNORE 不覆盖，仍读到空正文。
        //
        // 不能用 INSERT OR REPLACE：symbol_contents.content_hash 被其他文件的
        // symbols.symbol_hash 引用（全局共享），REPLACE 会先 DELETE 旧行，
        // 生产环境 PRAGMA foreign_keys=ON 时触发 FK 约束失败。
        //
        // 正确策略：
        // 1. INSERT OR IGNORE：行不存在时创建（带实际 content）
        // 2. UPDATE ... WHERE content=''：行已存在但 content 为空时，填入实际 content
        //    （修复旧版本遗留的空正文行，不影响已有有效 content 的行）
        if !sym.symbol_content_hash.is_empty() {
            codegraph_conn.execute(
                "INSERT OR IGNORE INTO symbol_contents \
                 (content_hash, name, kind, content, signature, has_comment, \
                 comment_content, qualified_name) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, '', ?7)",
                params![
                    &sym.symbol_content_hash,
                    &sym.name,
                    &sym.kind,
                    &sym.content,
                    &sym.signature,
                    sym.has_comment,
                    &sym.local_qualified_name,
                ],
            )?;
            // P1-2 v2 修复：UPDATE 空正文行（旧版本遗留）
            // 仅当 content 为空或 NULL 时更新，避免覆盖已有的有效 content
            codegraph_conn.execute(
                "UPDATE symbol_contents SET \
                 content = ?1, name = ?2, kind = ?3, signature = ?4, \
                 has_comment = ?5, qualified_name = ?6 \
                 WHERE content_hash = ?7 AND (content = '' OR content IS NULL)",
                params![
                    &sym.content,
                    &sym.name,
                    &sym.kind,
                    &sym.signature,
                    sym.has_comment,
                    &sym.local_qualified_name,
                    &sym.symbol_content_hash,
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

    // 5. INSERT 新 calls（P1-2 修复：跨文件 callee_id resolve）
    //
    // resolve 策略（与 db_build.py 对齐）：
    // a) 先在本文件内按 qualified_name 精确匹配（is_cross_file=0）
    // b) 再在本文件内按 name 短名匹配（is_cross_file=0）
    // c) 再跨文件按 qualified_name 匹配（is_cross_file=1）
    // d) 最后跨文件按 name 短名匹配（is_cross_file=1）
    // e) 全部未命中 → callee_id=0, callee_module='', is_cross_file=0（保留调用关系）
    //
    // 注意：本文件 symbols 已在 step 4 全部 INSERT，可以立即查询。
    // 跨文件查询在 CodeGraph DB 全量 symbols 表上进行（可能命中其他文件已 merge 的符号）。
    let mut inserted_calls: usize = 0;
    for call in cas_raw_calls {
        let caller_global_id = call
            .caller_local_id
            .and_then(|lid| local_to_global.get(&lid).copied())
            .unwrap_or(0);

        // P1-2 修复：resolve callee_id
        let (callee_id, callee_module, is_cross_file) = resolve_callee(
            codegraph_conn,
            &call.callee_name,
            file_instance_id,
            workspace_id,
        )?;

        codegraph_conn.execute(
            "INSERT INTO calls \
             (caller_id, caller_name, caller_module, callee_name, callee_module, \
             callee_file, callee_id, call_line, is_cross_file) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
            params![
                caller_global_id,
                &call.caller_name,
                module_path,
                &call.callee_name,
                &callee_module,
                rel_path,
                callee_id,
                call.call_line,
                is_cross_file,
            ],
        )?;
        inserted_calls += 1;
    }

    Ok((sym_count, inserted_calls))
}

/// 在当前图更新事务中同步维护本地文件与调用版本历史。
fn save_file_version_history(
    conn: &Connection,
    file_instance_id: i64,
    content_hash: &str,
    total_lines: i64,
    rel_path: &str,
    language: &str,
    cas_symbols: &[CasSymbolRow],
    metadata: &MergeHistoryMetadata,
) -> Result<i64, rusqlite::Error> {
    let latest: Option<(i64, i64, String)> = conn
        .query_row(
            "SELECT id, version_num, content_hash FROM file_versions \
             WHERE file_instance_id = ?1 AND is_current = 1 \
             ORDER BY version_num DESC LIMIT 1",
            params![file_instance_id],
            |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
        )
        .optional()?;

    if let Some((latest_id, _, latest_hash)) = latest.as_ref() {
        if latest_hash == content_hash {
            conn.execute(
                "UPDATE file_versions SET mtime = ?1, parsed_at = ?2, commit_hash = ?3 \
                 WHERE id = ?4",
                params![
                    metadata.mtime,
                    metadata.parsed_at,
                    &metadata.commit_hash,
                    latest_id
                ],
            )?;
            return Ok(*latest_id);
        }
    }

    let previous_version_id = latest.as_ref().map(|(version_id, _, _)| *version_id);
    let version_num = match latest {
        Some((latest_id, latest_version_num, _)) => {
            conn.execute(
                "UPDATE file_versions SET is_current = 0 WHERE id = ?1",
                params![latest_id],
            )?;
            latest_version_num + 1
        }
        None => 1,
    };

    conn.execute(
        "INSERT INTO file_versions \
         (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, \
          is_current, is_deleted, commit_hash) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 1, 0, ?7)",
        params![
            file_instance_id,
            version_num,
            content_hash,
            metadata.mtime,
            total_lines,
            metadata.parsed_at,
            &metadata.commit_hash,
        ],
    )?;
    let file_version_id = conn.last_insert_rowid();
    let module_path = module_path_from_rel(rel_path, language);

    for symbol in cas_symbols {
        if symbol.symbol_content_hash.is_empty() {
            continue;
        }
        conn.execute(
            "INSERT INTO file_symbol_versions \
             (file_version_id, symbol_hash, qualified_name, start_line, end_line, \
              module_path, depth, is_deleted) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 0)",
            params![
                file_version_id,
                &symbol.symbol_content_hash,
                &symbol.local_qualified_name,
                symbol.start_line,
                symbol.end_line,
                &module_path,
                symbol.depth,
            ],
        )?;
    }

    if let Some(previous_version_id) = previous_version_id {
        conn.execute(
            "INSERT INTO file_symbol_versions \
             (file_version_id, symbol_hash, qualified_name, start_line, end_line, \
              module_path, depth, is_deleted) \
             SELECT ?1, previous.symbol_hash, previous.qualified_name, \
                    previous.start_line, previous.end_line, previous.module_path, \
                    previous.depth, 1 \
             FROM file_symbol_versions previous \
             WHERE previous.file_version_id = ?2 \
               AND previous.is_deleted = 0 \
               AND NOT EXISTS ( \
                   SELECT 1 FROM file_symbol_versions current \
                   WHERE current.file_version_id = ?1 \
                     AND current.qualified_name = previous.qualified_name \
               )",
            params![file_version_id, previous_version_id],
        )?;
    }

    conn.execute(
        "INSERT INTO call_versions \
         (file_version_id, caller_qualified, caller_hash, callee_name, callee_module, \
          callee_qualified, callee_file, call_line, is_cross_file) \
         SELECT ?1, caller.qualified_name, caller.symbol_hash, calls.callee_name, \
                calls.callee_module, COALESCE(callee.qualified_name, ''), \
                calls.callee_file, calls.call_line, calls.is_cross_file \
         FROM calls \
         JOIN symbols caller ON calls.caller_id = caller.id \
         LEFT JOIN symbols callee ON calls.callee_id = callee.id \
         WHERE caller.file_instance_id = ?2",
        params![file_version_id, file_instance_id],
    )?;

    Ok(file_version_id)
}

/// P1-2 修复：resolve callee_id —— 按 qualified_name / name 在本文件和跨文件查找符号。
///
/// 返回 (callee_id, callee_module, is_cross_file)。未命中返回 (0, "", 0)。
///
/// 查找顺序（与 db_build.py resolve_calls 对齐）：
/// 1. 本文件按 qualified_name 精确匹配
/// 2. 本文件按 name 短名匹配
/// 3. 跨文件按 qualified_name 精确匹配
/// 4. 跨文件按 name 短名匹配
///
/// P1-2 v2 修复：所有 4 个查询添加 `ORDER BY s.id ASC` 消除同名符号歧义。
/// 复审报告指出旧实现 `LIMIT 1` 无 `ORDER BY`，同名符号任取一条，行为不确定。
/// 按 `s.id ASC` 排序保证返回最早插入的符号，行为稳定可预测。
fn resolve_callee(
    codegraph_conn: &Connection,
    callee_name: &str,
    file_instance_id: i64,
    workspace_id: i64,
) -> Result<(i64, String, i64), rusqlite::Error> {
    if callee_name.is_empty() {
        return Ok((0, String::new(), 0));
    }

    // 1. 本文件按 qualified_name 精确匹配
    let mut stmt = codegraph_conn.prepare(
        "SELECT s.id, s.module_path FROM symbols s \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         WHERE fi.id = ?1 AND s.qualified_name = ?2 \
         ORDER BY s.id ASC LIMIT 1",
    )?;
    if let Some(row) = stmt
        .query_map(params![file_instance_id, callee_name], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })?
        .next()
    {
        let (id, module) = row?;
        return Ok((id, module, 0));
    }

    // 2. 本文件按 name 短名匹配
    let mut stmt = codegraph_conn.prepare(
        "SELECT s.id, s.module_path FROM symbols s \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         WHERE fi.id = ?1 AND s.name = ?2 \
         ORDER BY s.id ASC LIMIT 1",
    )?;
    if let Some(row) = stmt
        .query_map(params![file_instance_id, callee_name], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })?
        .next()
    {
        let (id, module) = row?;
        return Ok((id, module, 0));
    }

    // 3. 跨文件按 qualified_name 精确匹配（同 workspace 内其他文件）
    let mut stmt = codegraph_conn.prepare(
        "SELECT s.id, s.module_path FROM symbols s \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1 AND fi.id != ?2 AND s.qualified_name = ?3 \
         ORDER BY s.id ASC LIMIT 1",
    )?;
    if let Some(row) = stmt
        .query_map(
            params![workspace_id, file_instance_id, callee_name],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
        )?
        .next()
    {
        let (id, module) = row?;
        return Ok((id, module, 1));
    }

    // 4. 跨文件按 name 短名匹配
    let mut stmt = codegraph_conn.prepare(
        "SELECT s.id, s.module_path FROM symbols s \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1 AND fi.id != ?2 AND s.name = ?3 \
         ORDER BY s.id ASC LIMIT 1",
    )?;
    if let Some(row) = stmt
        .query_map(
            params![workspace_id, file_instance_id, callee_name],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
        )?
        .next()
    {
        let (id, module) = row?;
        return Ok((id, module, 1));
    }

    // 5. 未命中：保留调用关系，callee_id=0
    Ok((0, String::new(), 0))
}

/// P1-2 v2 修复：workspace 级回扫 pass。
///
/// 复审报告指出：A→B 在 A 先 merge 时 callee_id=0（B 尚未 merge），
/// B 后到不会回扫 A 的入边，导致跨文件调用永久 unresolved。
///
/// 本函数在 merge 单个文件完成后调用，扫描当前 workspace 内所有
/// `callee_id=0` 的 calls，对每个 call 用 `resolve_callee` 跨文件 resolve。
/// 命中则 UPDATE calls SET callee_id=?, callee_module=?, is_cross_file=?。
///
/// 注意：仅扫描 callee_id=0 的行（增量），避免全表扫描。
/// 对于本文件内 callee_id=0 的 calls，resolve_callee 会重新尝试本文件查找。
///
/// 返回成功 resolve 的 calls 数量。
fn resolve_unresolved_calls_in_workspace(
    codegraph_conn: &Connection,
    workspace_id: i64,
) -> Result<usize, rusqlite::Error> {
    // 查询所有 callee_id=0 的 calls（同 workspace）
    // JOIN file_instances 获取 file_instance_id，用于 resolve_callee 的本文件查找
    let mut stmt = codegraph_conn.prepare(
        "SELECT c.id, c.callee_name, s.file_instance_id \
         FROM calls c \
         JOIN symbols s ON c.caller_id = s.id \
         JOIN file_instances fi ON s.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1 AND c.callee_id = 0 AND c.callee_name != ''",
    )?;
    let unresolved_rows: Vec<(i64, String, i64)> = stmt
        .query_map(params![workspace_id], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
            ))
        })?
        .filter_map(|r| r.ok())
        .collect();

    let mut resolved_count: usize = 0;
    for (call_id, callee_name, caller_file_instance_id) in unresolved_rows {
        // 用 caller 所在文件作为"本文件"上下文尝试 resolve
        let (callee_id, callee_module, is_cross_file) = resolve_callee(
            codegraph_conn,
            &callee_name,
            caller_file_instance_id,
            workspace_id,
        )?;
        if callee_id > 0 {
            // UPDATE calls SET callee_id=?, callee_module=?, is_cross_file=? WHERE id=?
            let affected = codegraph_conn.execute(
                "UPDATE calls SET callee_id = ?1, callee_module = ?2, is_cross_file = ?3 \
                 WHERE id = ?4 AND callee_id = 0",
                params![callee_id, callee_module, is_cross_file, call_id],
            )?;
            if affected > 0 {
                resolved_count += 1;
            }
        }
    }
    Ok(resolved_count)
}

/// DELETE calls WHERE caller_id IN (ids)
fn delete_calls_by_caller_ids(conn: &Connection, ids: &[i64]) -> Result<(), rusqlite::Error> {
    // 分批处理，避免 SQL 参数数量上限（SQLITE_MAX_VARIABLE_NUMBER，默认 999）
    for chunk in ids.chunks(500) {
        if chunk.is_empty() {
            continue;
        }
        let placeholders: Vec<String> = (0..chunk.len()).map(|i| format!("?{}", i + 1)).collect();
        let sql = format!(
            "DELETE FROM calls WHERE caller_id IN ({})",
            placeholders.join(",")
        );
        let params_iter: Vec<&dyn rusqlite::ToSql> =
            chunk.iter().map(|id| id as &dyn rusqlite::ToSql).collect();
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
        let placeholders: Vec<String> = (0..chunk.len()).map(|i| format!("?{}", i + 1)).collect();
        let sql = format!(
            "UPDATE calls SET callee_id = 0 WHERE callee_id IN ({})",
            placeholders.join(",")
        );
        let params_iter: Vec<&dyn rusqlite::ToSql> =
            chunk.iter().map(|id| id as &dyn rusqlite::ToSql).collect();
        conn.execute(&sql, params_iter.as_slice())?;
    }
    Ok(())
}

/// P1-2 修复：确保 workspace_manifests 表存在。
///
/// schema 与 db_workspace_manifest.py:MANIFEST_SCHEMA_DDL 对齐。
/// 使用 CREATE TABLE IF NOT EXISTS，已存在时无操作。
fn ensure_manifest_schema(conn: &Connection) -> Result<(), rusqlite::Error> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS workspace_manifests (\
            workspace_id INTEGER NOT NULL,\
            rel_path TEXT NOT NULL,\
            content_hash TEXT NOT NULL,\
            cas_key TEXT,\
            raw_hash TEXT,\
            source_encoding TEXT DEFAULT 'utf-8',\
            bom_kind TEXT DEFAULT 'none',\
            newline_style TEXT DEFAULT 'lf',\
            file_size INTEGER DEFAULT 0,\
            mtime_ns INTEGER DEFAULT 0,\
            is_dirty INTEGER DEFAULT 0,\
            updated_at REAL NOT NULL,\
            PRIMARY KEY (workspace_id, rel_path)\
         );\
         CREATE INDEX IF NOT EXISTS idx_manifests_hash ON workspace_manifests(content_hash);\
         CREATE INDEX IF NOT EXISTS idx_manifests_cas ON workspace_manifests(cas_key);\
         CREATE INDEX IF NOT EXISTS idx_manifests_dirty ON workspace_manifests(workspace_id, is_dirty);",
    )?;
    Ok(())
}

/// P1-2 v2 修复：UPSERT workspace_manifests 行。
///
/// 记录文件的 content_hash / cas_key / file_size，标记为 dirty（daemon merge 写入）。
/// 对应 db_workspace_manifest.py:upsert_manifest。
///
/// P1-2 v2 修复（复审报告 §3 P1-2 第 3、4 点）：
/// 1. file_size 与 total_lines 分离：原实现把 total_lines 当 file_size 传入，
///    manifest 中 file_size 字段实际是文件总行数，造成下游 snapshot overlay 错误。
///    现在接受 file_size 参数（从 cas_file_cache.file_size 读取，单位字节）。
/// 2. 硬编码字段说明：raw_hash/source_encoding/bom_kind/newline_style/mtime_ns
///    这些字段属于 file scan 阶段从文件系统读取的元数据，CAS DB 不存储。
///    本函数用合理默认值（utf-8 / none / lf / 0），snapshot publish 时若需真实值，
///    应从 file scan 阶段补全或 link_to_snapshot 引用 clean snapshot 的 manifest。
/// 3. is_dirty=1：标记为 daemon merge 写入（与 snapshot-level clean manifest 区分）。
fn upsert_manifest(
    conn: &Connection,
    workspace_id: i64,
    rel_path: &str,
    content_hash: &str,
    cas_key: &str,
    file_size: i64,
) -> Result<(), rusqlite::Error> {
    let now = now_ts();
    // is_dirty=1：标记为 daemon merge 写入（与 snapshot-level clean manifest 区分）
    //
    // P1-2 v2 注：以下字段使用默认值，因 CAS DB 不存储这些 file scan 阶段的元数据：
    // - raw_hash=''：文件原始内容 hash（去除 BOM/normalize 后），daemon 无此信息
    // - source_encoding='utf-8'：默认 UTF-8，Python 侧 file scan 会覆盖为真实值
    // - bom_kind='none'：默认无 BOM
    // - newline_style='lf'：默认 LF
    // - mtime_ns=0：文件修改时间（纳秒），daemon 无此信息；snapshot publish 不依赖此字段
    conn.execute(
        "INSERT OR REPLACE INTO workspace_manifests \
         (workspace_id, rel_path, content_hash, cas_key, raw_hash, \
          source_encoding, bom_kind, newline_style, file_size, mtime_ns, \
          is_dirty, updated_at) \
         VALUES (?1, ?2, ?3, ?4, '', 'utf-8', 'none', 'lf', ?5, 0, 1, ?6)",
        params![
            workspace_id,
            rel_path,
            content_hash,
            cas_key,
            file_size,
            now
        ],
    )?;
    Ok(())
}

/// 在单个事务中删除 workspace 文件的当前图快照并保留历史/CAS 内容。
///
/// 语义对齐 Python `db_build.py:remove_file`：
/// - `file_instances` 只标记为 deleted，不物理删除；
/// - `file_versions` 若存在，只标记当前版本为 deleted；
/// - `file_contents` / `symbol_contents` / CAS 条目均保留；
/// - 其他文件指向被删符号的调用边保留调用文本，只清空 `callee_id`。
pub fn delete_workspace_file_from_codegraph(
    codegraph_conn: &Connection,
    workspace_id: i64,
    rel_path: &str,
) -> Result<DeleteFileResult, String> {
    ensure_manifest_schema(codegraph_conn)
        .map_err(|e| format!("ensure_manifest_schema 失败: {}", e))?;

    if let Err(e) = codegraph_conn.execute_batch("BEGIN IMMEDIATE") {
        return Err(format!("BEGIN IMMEDIATE 失败: {}", e));
    }

    let inner = || -> Result<DeleteFileResult, String> {
        let file_row: Option<(i64, String)> = codegraph_conn
            .query_row(
                "SELECT id, status FROM file_instances \
                 WHERE workspace_id = ?1 AND rel_path = ?2",
                params![workspace_id, rel_path],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .optional()
            .map_err(|e| format!("查询 file_instances 失败: {}", e))?;

        let mut result = DeleteFileResult {
            workspace_id,
            rel_path: rel_path.to_string(),
            file_instance_id: file_row.as_ref().map(|row| row.0).unwrap_or(0),
            symbols_removed: 0,
            outgoing_calls_removed: 0,
            incoming_edges_cleared: 0,
            manifest_removed: 0,
            history_versions_marked: 0,
            delete_status: "not_found".to_string(),
        };

        if let Some((file_instance_id, previous_status)) = file_row {
            result.outgoing_calls_removed = codegraph_conn
                .execute(
                    "DELETE FROM calls WHERE caller_id IN \
                     (SELECT id FROM symbols WHERE file_instance_id = ?1)",
                    params![file_instance_id],
                )
                .map_err(|e| format!("删除文件出边失败: {}", e))?;
            result.incoming_edges_cleared = codegraph_conn
                .execute(
                    "UPDATE calls SET callee_id = 0 WHERE callee_id IN \
                     (SELECT id FROM symbols WHERE file_instance_id = ?1)",
                    params![file_instance_id],
                )
                .map_err(|e| format!("清理文件入边失败: {}", e))?;
            result.symbols_removed = codegraph_conn
                .execute(
                    "DELETE FROM symbols WHERE file_instance_id = ?1",
                    params![file_instance_id],
                )
                .map_err(|e| format!("删除文件符号失败: {}", e))?;

            codegraph_conn
                .execute(
                    "UPDATE file_instances SET status = 'deleted', mtime = ?1 WHERE id = ?2",
                    params![now_ts(), file_instance_id],
                )
                .map_err(|e| format!("写入文件 tombstone 失败: {}", e))?;

            let has_file_versions: bool = codegraph_conn
                .query_row(
                    "SELECT EXISTS(SELECT 1 FROM sqlite_master \
                     WHERE type = 'table' AND name = 'file_versions')",
                    [],
                    |row| row.get(0),
                )
                .map_err(|e| format!("检查 file_versions schema 失败: {}", e))?;
            if has_file_versions {
                result.history_versions_marked = codegraph_conn
                    .execute(
                        "UPDATE file_versions SET is_deleted = 1 \
                         WHERE file_instance_id = ?1 AND is_current = 1",
                        params![file_instance_id],
                    )
                    .map_err(|e| format!("标记当前文件版本失败: {}", e))?;
            }

            result.delete_status = if previous_status == "deleted" {
                "already_deleted".to_string()
            } else {
                "deleted".to_string()
            };
        }

        result.manifest_removed = codegraph_conn
            .execute(
                "DELETE FROM workspace_manifests WHERE workspace_id = ?1 AND rel_path = ?2",
                params![workspace_id, rel_path],
            )
            .map_err(|e| format!("删除 workspace manifest 失败: {}", e))?;

        if result.file_instance_id == 0 && result.manifest_removed > 0 {
            result.delete_status = "deleted".to_string();
        }
        Ok(result)
    };

    match inner() {
        Ok(result) => {
            if let Err(e) = codegraph_conn.execute_batch("COMMIT") {
                let _ = codegraph_conn.execute_batch("ROLLBACK");
                return Err(format!("COMMIT 失败: {}", e));
            }
            Ok(result)
        }
        Err(e) => {
            let _ = codegraph_conn.execute_batch("ROLLBACK");
            Err(e)
        }
    }
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
    merge_cas_to_codegraph_impl(
        cas_conn,
        codegraph_conn,
        cas_key,
        workspace_id,
        rel_path,
        abs_path,
        content_hash,
        language,
        workspace_root_path,
        None,
    )
}

/// 把 CAS 事实与文件版本历史原子合并到本地 CodeGraph DB。
///
/// daemon overlay 路径继续使用 [`merge_cas_to_codegraph`]；本地 `cw refresh`
/// 使用本入口，在同一个 `BEGIN IMMEDIATE` 中同时更新当前图和历史表。
pub fn merge_cas_to_codegraph_with_history(
    cas_conn: &Connection,
    codegraph_conn: &Connection,
    cas_key: &str,
    workspace_id: i64,
    rel_path: &str,
    abs_path: &str,
    content_hash: &str,
    language: &str,
    workspace_root_path: &str,
    history: &MergeHistoryMetadata,
) -> Result<MergeResult, String> {
    merge_cas_to_codegraph_impl(
        cas_conn,
        codegraph_conn,
        cas_key,
        workspace_id,
        rel_path,
        abs_path,
        content_hash,
        language,
        workspace_root_path,
        Some(history),
    )
}

fn merge_cas_to_codegraph_impl(
    cas_conn: &Connection,
    codegraph_conn: &Connection,
    cas_key: &str,
    workspace_id: i64,
    rel_path: &str,
    abs_path: &str,
    content_hash: &str,
    language: &str,
    workspace_root_path: &str,
    history: Option<&MergeHistoryMetadata>,
) -> Result<MergeResult, String> {
    // 1. 查 CAS file cache（P1-2 v2 修复：同时取 file_size 和 total_lines）
    //    复审报告指出旧实现只取 total_lines，把 total_lines 当 file_size 传给 upsert_manifest，
    //    manifest 中 file_size 字段实际是文件总行数。现分开读取，file_size 用于 manifest，
    //    total_lines 用于 file_contents/file_instances。
    let (file_size, total_lines): (i64, i64) = match cas_conn
        .query_row(
            "SELECT file_size, total_lines FROM cas_file_cache WHERE cas_key = ?1",
            params![cas_key],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)),
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

    // 2. 查 CAS symbols（P1-2 修复：LEFT JOIN cas_symbol_contents 读取实际符号正文）
    let mut cas_symbols: Vec<CasSymbolRow> = Vec::new();
    {
        let mut stmt = cas_conn
            .prepare(
                "SELECT s.local_symbol_id, s.symbol_content_hash, s.name, s.local_qualified_name, \
                 s.kind, s.start_line, s.end_line, s.start_col, s.end_col, s.visibility, \
                 s.signature, s.has_comment, s.depth, \
                 COALESCE(sc.content, '') AS content \
                 FROM cas_symbols s \
                 LEFT JOIN cas_symbol_contents sc ON s.symbol_content_hash = sc.content_hash \
                 WHERE s.cas_key = ?1 ORDER BY s.local_symbol_id",
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
                    depth: row.get::<_, Option<i64>>(12)?.unwrap_or(-1),
                    content: row.get::<_, Option<String>>(13)?.unwrap_or_default(),
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

    // 4-7. 在 CodeGraph DB 上执行事务
    //      BEGIN IMMEDIATE → UPSERT workspaces / file_records → 替换 symbols/calls →
    //      UPSERT workspace_manifests → COMMIT
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
            workspace_id,
            &cas_symbols,
            &cas_raw_calls,
            rel_path,
            language,
        )
        .map_err(|e| format!("replace_symbols_and_calls 失败: {}", e))?;

        // 6b. P1-2 v2 修复：workspace 级回扫 pass
        //
        // 复审报告指出：A→B 在 A 先 merge 时 callee_id=0（B 尚未 merge），
        // B 后到不会回扫 A 的入边。本 pass 在每次 merge 完成后扫描当前 workspace 内
        // 所有 callee_id=0 的 calls，尝试用新 merge 的 symbols resolve。
        //
        // 这是 workspace 级 pass，开销与 unresolved calls 数量成正比（增量扫描）。
        // 多文件 merge 时，每次 merge 后都会触发回扫，但已 resolve 的 calls 不会被重复处理。
        if !cas_symbols.is_empty() {
            resolve_unresolved_calls_in_workspace(codegraph_conn, workspace_id)
                .map_err(|e| format!("resolve_unresolved_calls_in_workspace 失败: {}", e))?;
        }

        if let Some(history) = history {
            save_file_version_history(
                codegraph_conn,
                file_instance_id,
                content_hash,
                total_lines,
                rel_path,
                language,
                &cas_symbols,
                history,
            )
            .map_err(|e| format!("save_file_version_history 失败: {}", e))?;
        }

        // 7. P1-2 修复：UPSERT workspace_manifests（记录文件 CAS key + content_hash）
        //
        // workspace_manifests 表由 db_workspace_manifest.py 管理，记录每个 workspace 中
        // 每个文件的 content_hash / cas_key / file_size 等元数据。merge 完成后写入 manifest
        // 使后续 snapshot publish / dirty overlay 能正确引用。
        //
        // P1-2 v2 修复：传入 file_size（字节）而非 total_lines（行数）。
        ensure_manifest_schema(codegraph_conn)
            .map_err(|e| format!("ensure_manifest_schema 失败: {}", e))?;
        upsert_manifest(
            codegraph_conn,
            workspace_id,
            rel_path,
            content_hash,
            cas_key,
            file_size,
        )
        .map_err(|e| format!("upsert_manifest 失败: {}", e))?;

        let merge_status = if cas_symbols.is_empty() {
            "no_symbols"
        } else {
            "merged"
        };

        Ok((
            file_instance_id,
            sym_count,
            call_count,
            merge_status.to_string(),
        ))
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
            -- P1-2 修复：cas_symbol_contents 表（存储符号实际源码内容）
            CREATE TABLE IF NOT EXISTS cas_symbol_contents (
                content_hash TEXT PRIMARY KEY,
                content TEXT NOT NULL
            );
            "#,
        )
        .unwrap();
    }

    /// 往 CAS DB 插入一个 cas_key + 2 个 symbols + 1 个 raw_call + 2 个 symbol_contents
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

        // P1-2 修复：写入符号实际源码内容到 cas_symbol_contents
        conn.execute(
            "INSERT OR IGNORE INTO cas_symbol_contents (content_hash, content) \
             VALUES (?1, ?2)",
            params!["sym_hash_main", "fn main() {\n    helper();\n}"],
        )
        .unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO cas_symbol_contents (content_hash, content) \
             VALUES (?1, ?2)",
            params!["sym_hash_helper", "fn helper() {\n    println!(\"hi\");\n}"],
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
        assert_eq!(
            module_path_from_rel("src/server/main.py", "python"),
            "server.main"
        );
        assert_eq!(
            module_path_from_rel("src/server/main.rs", "rust"),
            "lib::server::main"
        );
        assert_eq!(module_path_from_rel("src/lib.rs", "rust"), "lib");
        // 无扩展名
        assert_eq!(
            module_path_from_rel("src/server/main", "python"),
            "server.main"
        );
        // Windows 路径
        assert_eq!(
            module_path_from_rel("src\\server\\main.py", "python"),
            "server.main"
        );
        assert_eq!(module_path_from_rel("main.py", "python"), "main");
        assert_eq!(module_path_from_rel("src/main", "python"), "main");
        assert_eq!(module_path_from_rel("src/pkg/__init__.py", "python"), "pkg");
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
    fn test_init_codegraph_schema_on_fresh_db() {
        // P0-2 v2 修复：fresh CodeGraph DB 首次打开时初始化主 schema
        //
        // 复审报告指出：Connection::open 只创建空 SQLite 文件，
        // merge 时查询 workspaces 等表会报 "no such table"。
        // init_codegraph_schema 应幂等建表。
        let cg_conn = Connection::open_in_memory().unwrap();

        // fresh DB：不应有任何业务表
        let table_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN \
                 ('workspaces','file_contents','file_instances','symbols','calls','symbol_contents')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(table_count, 0, "fresh DB 不应有业务表");

        // 初始化 schema
        init_codegraph_schema(&cg_conn).expect("init_codegraph_schema 应成功");

        // 验证所有业务表都已创建
        let table_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN \
                 ('workspaces','file_contents','file_instances','symbols','calls','symbol_contents')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(table_count, 6, "应有 6 张业务表");

        // 验证 FTS5 虚拟表
        let fts_exists: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='symbols_fts'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(fts_exists, 1, "应有 symbols_fts 虚拟表");

        // 验证触发器
        let trigger_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='trigger' AND name LIKE 'symbols_fts_%'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(trigger_count, 3, "应有 3 个 FTS5 同步触发器");
    }

    #[test]
    fn test_init_codegraph_schema_idempotent() {
        // P0-2 v2 修复：init_codegraph_schema 应幂等可重复调用
        let cg_conn = Connection::open_in_memory().unwrap();
        init_codegraph_schema(&cg_conn).expect("第一次调用应成功");
        // 第二次调用应无操作（CREATE IF NOT EXISTS）
        init_codegraph_schema(&cg_conn).expect("第二次调用应成功（幂等）");

        // 验证仍只有 6 张业务表（没有重复创建）
        let table_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name IN \
                 ('workspaces','file_contents','file_instances','symbols','calls','symbol_contents')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(table_count, 6);
    }

    #[test]
    fn test_merge_succeeds_on_fresh_db_with_init_schema() {
        // P0-2 v2 修复：fresh DB 调用 init_codegraph_schema 后 merge 应成功
        //
        // 复现复审报告场景：默认 codegraph_db_path_template 非空后，
        // fresh DB 首次 merge 不会报 "no such table"
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        seed_cas(&cas_conn, "ck1", "ch1", "rust", 10);

        // fresh CodeGraph DB（不调用 make_codegraph_schema，只调用 init_codegraph_schema）
        let cg_conn = Connection::open_in_memory().unwrap();
        init_codegraph_schema(&cg_conn).expect("init_codegraph_schema 应成功");

        // merge 应成功（不会报 no such table）
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
        .expect("merge 在 fresh DB 上应成功");

        assert_eq!(result.merge_status, "merged");
        assert_eq!(result.symbols_inserted, 2);
        assert_eq!(result.calls_inserted, 1);
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
            .query_row("SELECT COUNT(*) FROM workspaces WHERE id = 42", [], |row| {
                row.get(0)
            })
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

        // ---- P1-2 修复验证 ----

        // 1. symbol_contents.content 不再为空（从 cas_symbol_contents JOIN 读取）
        let main_content: String = cg_conn
            .query_row(
                "SELECT content FROM symbol_contents WHERE content_hash = 'sym_hash_main'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            !main_content.is_empty(),
            "P1-2: symbol_contents.content 不应为空"
        );
        assert!(
            main_content.contains("fn main"),
            "P1-2: content 应包含 main 函数源码"
        );

        let helper_content: String = cg_conn
            .query_row(
                "SELECT content FROM symbol_contents WHERE content_hash = 'sym_hash_helper'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            !helper_content.is_empty(),
            "P1-2: symbol_contents.content 不应为空"
        );
        assert!(
            helper_content.contains("fn helper"),
            "P1-2: content 应包含 helper 函数源码"
        );

        // 2. callee_id 已 resolve（不再是 0，指向 helper symbol 的 id）
        let (callee_id, is_cross_file): (i64, i64) = cg_conn
            .query_row(
                "SELECT callee_id, is_cross_file FROM calls \
                 WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?1) \
                 AND callee_name = 'helper'",
                params![fi_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert!(
            callee_id > 0,
            "P1-2: callee_id 应被 resolve 到有效 symbol id（>0），实际={}",
            callee_id
        );
        assert_eq!(
            is_cross_file, 0,
            "P1-2: main -> helper 在同文件内，is_cross_file 应为 0"
        );

        // 校验 callee_id 指向的 symbol 确实是 helper
        let callee_name_from_id: String = cg_conn
            .query_row(
                "SELECT name FROM symbols WHERE id = ?1",
                params![callee_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(callee_name_from_id, "helper");

        // 3. workspace_manifests 表有对应行
        let manifest_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM workspace_manifests \
                 WHERE workspace_id = 42 AND rel_path = 'src/main.rs'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(manifest_count, 1, "P1-2: workspace_manifests 应有 1 行记录");

        // 校验 manifest 内容
        let (m_cas_key, m_content_hash, m_is_dirty): (String, String, i64) = cg_conn
            .query_row(
                "SELECT cas_key, content_hash, is_dirty FROM workspace_manifests \
                 WHERE workspace_id = 42 AND rel_path = 'src/main.rs'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?)),
            )
            .unwrap();
        assert_eq!(m_cas_key, "ck1");
        assert_eq!(m_content_hash, "ch1");
        assert_eq!(m_is_dirty, 1, "daemon merge 写入的 manifest 应标记为 dirty");
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

        // 验证：旧的 main_sym_id 已不存在
        let old_main_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM symbols WHERE id = ?1",
                params![main_sym_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(old_main_count, 0);

        // P0-2 + P1-2 v2 联合行为验证：
        // 1. P0-2 入边清理：旧 main_sym_id 被删除后，指向它的 callee_id 应被置 0（避免悬空引用）
        // 2. P1-2 v2 回扫 pass：merge 完成后，回扫 pass 会扫描 callee_id=0 的 calls，
        //    尝试用新插入的 symbols resolve。因此 callee_id 应被 resolve 到新的 main symbol。
        //
        // 旧断言 callee_id == 0 已不适用——回扫 pass 会主动修复悬空引用。
        // 新断言：callee_id > 0 且 != main_sym_id（旧 id），指向新的 main symbol。
        let (callee_id_after, callee_name_after): (i64, String) = cg_conn
            .query_row(
                "SELECT callee_id, callee_name FROM calls WHERE caller_id = ?1",
                params![caller_id],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert!(
            callee_id_after > 0,
            "P1-2 v2: 回扫 pass 应 resolve callee_id > 0，实际={}",
            callee_id_after
        );
        assert_ne!(
            callee_id_after, main_sym_id,
            "P0-2: 不应仍指向旧 main_sym_id（已被删除）"
        );
        assert_eq!(callee_name_after, "main", "callee_name 应保持为 'main'");

        // 验证：callee_id 指向新的 main symbol
        let new_main_id: i64 = cg_conn
            .query_row(
                "SELECT id FROM symbols WHERE file_instance_id = ?1 AND name = 'main'",
                params![r2.file_instance_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            callee_id_after, new_main_id,
            "P1-2 v2: 回扫 pass 应 resolve 到新的 main symbol id"
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

    /// P1-2 v2 修复：workspace 级回扫 pass 测试
    ///
    /// 场景：A.rs 有 call to helper()，但 helper 定义在 B.rs。
    /// 先 merge A.rs（callee_id=0，因 B.rs 尚未 merge），
    /// 再 merge B.rs——回扫 pass 应将 A.rs 中 callee_id=0 的 call resolve 到 B.rs 的 helper。
    #[test]
    fn test_workspace_resolve_backfill_after_late_merge() {
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);

        // CAS for A.rs：caller "fn_a" 调用 "helper"（helper 不在 A.rs 中）
        // file_size=200, total_lines=20（不同值用于校验 manifest file_size 字段）
        cas_conn
            .execute(
                "INSERT INTO cas_file_cache \
             (cas_key, content_hash, language, file_size, total_lines, \
             parser_version, callwarden_version, extraction_config_version, \
             abi_version, input_abi_version, state, parsed_at) \
             VALUES ('ck_a', 'ch_a', 'rust', 200, 20, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
                [],
            )
            .unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbol_contents (content_hash, content) VALUES ('sh_fn_a', 'fn fn_a() { helper(); }')",
            [],
        ).unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbols \
             (cas_key, local_symbol_id, symbol_content_hash, name, local_qualified_name, \
             kind, start_line, end_line, start_col, end_col, visibility, signature, \
             has_comment, depth) \
             VALUES ('ck_a', 1, 'sh_fn_a', 'fn_a', 'fn_a', 'function', 1, 3, 0, 0, 'public', 'fn fn_a()', 0, 0)",
            [],
        ).unwrap();
        // fn_a 调用 helper（callee 在 A.rs 中不存在）
        cas_conn
            .execute(
                "INSERT INTO cas_raw_calls \
             (cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal) \
             VALUES ('ck_a', 1, 'fn_a', 'helper', 2, 0)",
                [],
            )
            .unwrap();

        // CAS for B.rs：定义 helper 函数
        cas_conn
            .execute(
                "INSERT INTO cas_file_cache \
             (cas_key, content_hash, language, file_size, total_lines, \
             parser_version, callwarden_version, extraction_config_version, \
             abi_version, input_abi_version, state, parsed_at) \
             VALUES ('ck_b', 'ch_b', 'rust', 80, 8, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
                [],
            )
            .unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbol_contents (content_hash, content) VALUES ('sh_helper', 'fn helper() { }')",
            [],
        ).unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbols \
             (cas_key, local_symbol_id, symbol_content_hash, name, local_qualified_name, \
             kind, start_line, end_line, start_col, end_col, visibility, signature, \
             has_comment, depth) \
             VALUES ('ck_b', 1, 'sh_helper', 'helper', 'helper', 'function', 1, 3, 0, 0, 'public', 'fn helper()', 0, 0)",
            [],
        ).unwrap();

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        // 先 merge A.rs：fn_a 调用 helper，但 helper 尚未 merge
        // callee_id 应为 0（A.rs 内查找 helper 失败，B.rs 还没 merge）
        let r_a = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_a",
            7,
            "src/a.rs",
            "/app/src/a.rs",
            "ch_a",
            "rust",
            "/app",
        )
        .unwrap();
        assert_eq!(r_a.merge_status, "merged");
        assert_eq!(r_a.symbols_inserted, 1);

        // 校验 A.rs merge 后 callee_id=0（B.rs 尚未 merge）
        let callee_id_after_a: i64 = cg_conn
            .query_row(
                "SELECT callee_id FROM calls WHERE callee_name = 'helper'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            callee_id_after_a, 0,
            "A.rs merge 后 callee_id 应为 0（B.rs 尚未 merge）"
        );

        // 再 merge B.rs：helper 函数被插入
        // 回扫 pass 应将 A.rs 中 callee_id=0 的 call resolve 到 B.rs 的 helper
        let r_b = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_b",
            7,
            "src/b.rs",
            "/app/src/b.rs",
            "ch_b",
            "rust",
            "/app",
        )
        .unwrap();
        assert_eq!(r_b.merge_status, "merged");
        assert_eq!(r_b.symbols_inserted, 1);

        // 校验回扫 pass 已 resolve callee_id
        let (callee_id, is_cross_file): (i64, i64) = cg_conn
            .query_row(
                "SELECT callee_id, is_cross_file FROM calls WHERE callee_name = 'helper'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert!(
            callee_id > 0,
            "P1-2 v2: 回扫 pass 应 resolve callee_id > 0，实际={}",
            callee_id
        );
        assert_eq!(is_cross_file, 1, "P1-2 v2: 跨文件调用 is_cross_file 应为 1");

        // 校验 callee_id 指向 B.rs 的 helper symbol
        let callee_name: String = cg_conn
            .query_row(
                "SELECT name FROM symbols WHERE id = ?1",
                params![callee_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(callee_name, "helper");
    }

    /// P1-2 v2 修复：manifest file_size 正确性测试
    ///
    /// 复审报告指出：旧实现把 total_lines 当 file_size 传给 upsert_manifest，
    /// manifest 中 file_size 字段实际是文件总行数。
    /// 本测试校验 manifest.file_size == cas_file_cache.file_size（字节），而非 total_lines。
    #[test]
    fn test_manifest_file_size_correctness() {
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        // file_size=256 字节，total_lines=10 行（不同值）
        seed_cas_with_file_size(&cas_conn, "ck_fs", "ch_fs", "rust", 256, 10);

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        let _r = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_fs",
            1,
            "src/main.rs",
            "/app/src/main.rs",
            "ch_fs",
            "rust",
            "/app",
        )
        .unwrap();

        // 校验 manifest.file_size == 256（字节，来自 cas_file_cache.file_size）
        let (m_file_size, m_total_lines_unused): (i64, i64) = cg_conn
            .query_row(
                "SELECT file_size, mtime_ns FROM workspace_manifests \
                 WHERE workspace_id = 1 AND rel_path = 'src/main.rs'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(
            m_file_size, 256,
            "P1-2 v2: manifest.file_size 应为字节大小（256），实际={}",
            m_file_size
        );
        // m_total_lines_unused（实际查的是 mtime_ns）应为 0（daemon 无此信息）
        assert_eq!(m_total_lines_unused, 0, "mtime_ns 应为默认值 0");

        // 同时校验 file_instances.total_lines == 10（行数）
        let fi_total_lines: i64 = cg_conn
            .query_row(
                "SELECT total_lines FROM file_instances \
                 WHERE workspace_id = 1 AND rel_path = 'src/main.rs'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(fi_total_lines, 10, "file_instances.total_lines 应为 10");
    }

    /// P1-2 v2 修复：symbol_contents 空正文修复测试
    ///
    /// 复审报告指出：旧版本可能写入 content='' 的 symbol_contents 行，
    /// 新版本 merge 后 INSERT OR IGNORE 不覆盖，仍读到空正文。
    /// 本测试模拟：预插入空正文行 → merge → 验证 content 被更新为实际值。
    #[test]
    fn test_symbol_contents_empty_content_repair() {
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        seed_cas(&cas_conn, "ck_ec", "ch_ec", "rust", 10);

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        // 预插入空正文行（模拟旧版本遗留）
        cg_conn.execute(
            "INSERT INTO symbol_contents (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name) \
             VALUES ('sym_hash_main', 'main', 'function', '', '', 0, '', 'main')",
            [],
        ).unwrap();
        cg_conn.execute(
            "INSERT INTO symbol_contents (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name) \
             VALUES ('sym_hash_helper', 'helper', 'function', '', '', 0, '', 'main.helper')",
            [],
        ).unwrap();

        // 校验预插入的空正文
        let empty_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM symbol_contents WHERE content = ''",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(empty_count, 2, "预插入 2 个空正文行");

        // merge：应修复空正文
        let _r = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_ec",
            1,
            "src/main.rs",
            "/app/src/main.rs",
            "ch_ec",
            "rust",
            "/app",
        )
        .unwrap();

        // 校验空正文已被修复
        let main_content: String = cg_conn
            .query_row(
                "SELECT content FROM symbol_contents WHERE content_hash = 'sym_hash_main'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            !main_content.is_empty(),
            "P1-2 v2: 空正文应被修复为实际 content"
        );
        assert!(
            main_content.contains("fn main"),
            "P1-2 v2: content 应包含 main 函数源码"
        );

        let helper_content: String = cg_conn
            .query_row(
                "SELECT content FROM symbol_contents WHERE content_hash = 'sym_hash_helper'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(!helper_content.is_empty(), "P1-2 v2: helper 空正文应被修复");

        // 不应再有空正文行
        let empty_after: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM symbol_contents WHERE content = ''",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(empty_after, 0, "merge 后不应有空正文行");
    }

    /// P1-2 v2 修复：resolve_callee ORDER BY 稳定性测试
    ///
    /// 复审报告指出：旧实现 LIMIT 1 无 ORDER BY，同名符号任取一条。
    /// 本测试插入两个同名 symbol，验证 resolve 总是返回 id 较小的（ORDER BY s.id ASC）。
    #[test]
    fn test_resolve_callee_order_by_stability() {
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);

        // CAS：A.rs 中 caller 调用 "shared"（同名符号在两个文件中都有）
        cas_conn.execute(
            "INSERT INTO cas_file_cache \
             (cas_key, content_hash, language, file_size, total_lines, \
             parser_version, callwarden_version, extraction_config_version, \
             abi_version, input_abi_version, state, parsed_at) \
             VALUES ('ck_o1', 'ch_o1', 'rust', 100, 10, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
            [],
        ).unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbol_contents (content_hash, content) VALUES ('sh_caller', 'fn caller() { shared(); }')",
            [],
        ).unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbols \
             (cas_key, local_symbol_id, symbol_content_hash, name, local_qualified_name, \
             kind, start_line, end_line, start_col, end_col, visibility, signature, has_comment, depth) \
             VALUES ('ck_o1', 1, 'sh_caller', 'caller', 'caller', 'function', 1, 3, 0, 0, 'public', 'fn caller()', 0, 0)",
            [],
        ).unwrap();
        cas_conn
            .execute(
                "INSERT INTO cas_raw_calls \
             (cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal) \
             VALUES ('ck_o1', 1, 'caller', 'shared', 2, 0)",
                [],
            )
            .unwrap();

        // CAS：B.rs 中定义 "shared"
        cas_conn.execute(
            "INSERT INTO cas_file_cache \
             (cas_key, content_hash, language, file_size, total_lines, \
             parser_version, callwarden_version, extraction_config_version, \
             abi_version, input_abi_version, state, parsed_at) \
             VALUES ('ck_o2', 'ch_o2', 'rust', 100, 10, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
            [],
        ).unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbol_contents (content_hash, content) VALUES ('sh_shared_b', 'fn shared() {}')",
            [],
        ).unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbols \
             (cas_key, local_symbol_id, symbol_content_hash, name, local_qualified_name, \
             kind, start_line, end_line, start_col, end_col, visibility, signature, has_comment, depth) \
             VALUES ('ck_o2', 1, 'sh_shared_b', 'shared', 'shared', 'function', 1, 3, 0, 0, 'public', 'fn shared()', 0, 0)",
            [],
        ).unwrap();

        // CAS：C.rs 中也定义 "shared"（同名，模拟跨文件同名符号）
        cas_conn.execute(
            "INSERT INTO cas_file_cache \
             (cas_key, content_hash, language, file_size, total_lines, \
             parser_version, callwarden_version, extraction_config_version, \
             abi_version, input_abi_version, state, parsed_at) \
             VALUES ('ck_o3', 'ch_o3', 'rust', 100, 10, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
            [],
        ).unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbol_contents (content_hash, content) VALUES ('sh_shared_c', 'fn shared() { /* c */ }')",
            [],
        ).unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbols \
             (cas_key, local_symbol_id, symbol_content_hash, name, local_qualified_name, \
             kind, start_line, end_line, start_col, end_col, visibility, signature, has_comment, depth) \
             VALUES ('ck_o3', 1, 'sh_shared_c', 'shared', 'shared', 'function', 1, 3, 0, 0, 'public', 'fn shared()', 0, 0)",
            [],
        ).unwrap();

        let cg_conn = Connection::open_in_memory().unwrap();
        make_codegraph_schema(&cg_conn);

        // merge 顺序：B → C → A（B 先 merge，shared id 较小）
        // merge A 时 resolve_callee 跨文件查找 shared，应返回 B 的 shared（id 较小）
        merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_o2",
            9,
            "src/b.rs",
            "/app/src/b.rs",
            "ch_o2",
            "rust",
            "/app",
        )
        .unwrap();
        merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_o3",
            9,
            "src/c.rs",
            "/app/src/c.rs",
            "ch_o3",
            "rust",
            "/app",
        )
        .unwrap();
        merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_o1",
            9,
            "src/a.rs",
            "/app/src/a.rs",
            "ch_o1",
            "rust",
            "/app",
        )
        .unwrap();

        // 获取 B 和 C 的 shared symbol id
        let (b_shared_id, c_shared_id): (i64, i64) = cg_conn
            .query_row(
                "SELECT \
                 (SELECT s.id FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id \
                  WHERE fi.rel_path = 'src/b.rs' AND s.name = 'shared'), \
                 (SELECT s.id FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id \
                  WHERE fi.rel_path = 'src/c.rs' AND s.name = 'shared')",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert!(b_shared_id < c_shared_id, "B 先 merge，id 应较小");

        // 校验 callee_id 解析到 B 的 shared（ORDER BY s.id ASC）
        let callee_id: i64 = cg_conn
            .query_row(
                "SELECT callee_id FROM calls WHERE callee_name = 'shared'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            callee_id, b_shared_id,
            "P1-2 v2: resolve 应返回 id 较小的 B 的 shared（ORDER BY s.id ASC），实际={}",
            callee_id
        );
    }

    /// 辅助：seed CAS with 自定义 file_size 和 total_lines
    fn seed_cas_with_file_size(
        conn: &Connection,
        cas_key: &str,
        content_hash: &str,
        language: &str,
        file_size: i64,
        total_lines: i64,
    ) {
        conn.execute(
            "INSERT INTO cas_file_cache \
             (cas_key, content_hash, language, file_size, total_lines, \
             parser_version, callwarden_version, extraction_config_version, \
             abi_version, input_abi_version, state, parsed_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
            params![cas_key, content_hash, language, file_size, total_lines],
        )
        .unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO cas_symbol_contents (content_hash, content) \
             VALUES (?1, ?2)",
            params!["sym_hash_main", "fn main() {\n    helper();\n}"],
        )
        .unwrap();
        conn.execute(
            "INSERT OR IGNORE INTO cas_symbol_contents (content_hash, content) \
             VALUES (?1, ?2)",
            params!["sym_hash_helper", "fn helper() {\n    println!(\"hi\");\n}"],
        )
        .unwrap();
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
        conn.execute(
            "INSERT INTO cas_raw_calls \
             (cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal) \
             VALUES (?1, 1, 'main', 'helper', 3, 0)",
            params![cas_key],
        )
        .unwrap();
    }

    // ---- 门槛 2：空文件 DB → schema → merge → query E2E 集成测试 ----
    //
    // 复审报告 §6 门槛 2 要求：
    //   "用唯一 schema/migration 入口初始化每 workspace CodeGraph DB，补空文件 DB 的真实
    //    refresh 到 query E2E。"
    //
    // 本组测试模拟完整生产链路：
    // 1. fresh CodeGraph DB（Connection::open 只创建空 SQLite 文件，无任何表）
    // 2. 调用 init_codegraph_schema() 初始化主 schema
    // 3. seed CAS（模拟 daemon_handle_refresh 解析结果）
    // 4. 调用 merge_cas_to_codegraph()
    // 5. 查询 symbols / calls / file_instances / workspace_manifests 验证完整链路

    /// 门槛 2 E2E：fresh DB → schema init → merge → query 完整链路
    ///
    /// 关键验证点：
    /// - init_codegraph_schema 能在完全空的 SQLite 文件上成功执行
    /// - merge_cas_to_codegraph 能在 fresh schema 上成功 merge
    /// - merge 后 symbols/calls/file_instances/workspace_manifests 行数正确
    /// - 跨文件 resolve（回扫 pass）能正确 resolve callee_id
    /// - symbol_contents 有实际 content（非空字符串）
    #[test]
    fn test_e2e_fresh_db_schema_init_merge_then_query() {
        // 1. fresh CodeGraph DB（空 SQLite 文件）
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("fresh_codegraph.db");
        let cg_conn = Connection::open(&db_path).unwrap();

        // 验证 fresh DB 无任何表
        let table_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(table_count, 0, "fresh DB 不应有任何表");

        // 2. 初始化 schema
        init_codegraph_schema(&cg_conn).unwrap();

        // 验证关键表存在（workspace_manifests 由 ensure_manifest_schema 在 merge 时创建）
        let key_tables = [
            "workspaces",
            "file_contents",
            "file_instances",
            "symbols",
            "calls",
            "symbol_contents",
            "symbols_fts",
        ];
        for tbl in &key_tables {
            let exists: i64 = cg_conn
                .query_row(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = ?1",
                    params![tbl],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(exists, 1, "门槛 2: 表 {} 应存在", tbl);
        }

        // workspace_manifests 应在 merge 时由 ensure_manifest_schema 创建（init_codegraph_schema 不创建）
        let manifest_exists_before: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'workspace_manifests'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            manifest_exists_before, 0,
            "门槛 2: workspace_manifests 应在 merge 时创建（init 不创建）"
        );

        // 3. seed CAS（两个文件，A 调用 B 的函数——测试跨文件 resolve 回扫）
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);

        // CAS A：定义 caller "fn_a"，调用 "helper"（在 B 中定义）
        cas_conn
            .execute(
                "INSERT INTO cas_file_cache \
             (cas_key, content_hash, language, file_size, total_lines, \
             parser_version, callwarden_version, extraction_config_version, \
             abi_version, input_abi_version, state, parsed_at) \
             VALUES ('ck_a', 'ch_a', 'rust', 150, 15, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
                [],
            )
            .unwrap();
        cas_conn
            .execute(
                "INSERT INTO cas_symbol_contents (content_hash, content) \
             VALUES ('sh_fn_a', 'fn fn_a() { helper(); }')",
                [],
            )
            .unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbols \
             (cas_key, local_symbol_id, symbol_content_hash, name, local_qualified_name, \
             kind, start_line, end_line, start_col, end_col, visibility, signature, \
             has_comment, depth) \
             VALUES ('ck_a', 1, 'sh_fn_a', 'fn_a', 'fn_a', 'function', 1, 3, 0, 0, 'public', 'fn fn_a()', 0, 0)",
            [],
        ).unwrap();
        cas_conn
            .execute(
                "INSERT INTO cas_raw_calls \
             (cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal) \
             VALUES ('ck_a', 1, 'fn_a', 'helper', 2, 0)",
                [],
            )
            .unwrap();

        // CAS B：定义 "helper"
        cas_conn
            .execute(
                "INSERT INTO cas_file_cache \
             (cas_key, content_hash, language, file_size, total_lines, \
             parser_version, callwarden_version, extraction_config_version, \
             abi_version, input_abi_version, state, parsed_at) \
             VALUES ('ck_b', 'ch_b', 'rust', 60, 6, 'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0.0)",
                [],
            )
            .unwrap();
        cas_conn
            .execute(
                "INSERT INTO cas_symbol_contents (content_hash, content) \
             VALUES ('sh_helper', 'fn helper() { /* B */ }')",
                [],
            )
            .unwrap();
        cas_conn.execute(
            "INSERT INTO cas_symbols \
             (cas_key, local_symbol_id, symbol_content_hash, name, local_qualified_name, \
             kind, start_line, end_line, start_col, end_col, visibility, signature, \
             has_comment, depth) \
             VALUES ('ck_b', 1, 'sh_helper', 'helper', 'helper', 'function', 1, 3, 0, 0, 'public', 'fn helper()', 0, 0)",
            [],
        ).unwrap();

        // 4. merge A → B（A 先 merge，B 后到——测试回扫 pass）
        let r_a = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_a",
            42,
            "src/a.rs",
            "/app/src/a.rs",
            "ch_a",
            "rust",
            "/app",
        )
        .unwrap();
        assert_eq!(r_a.merge_status, "merged");
        assert_eq!(r_a.symbols_inserted, 1);

        // A merge 后 callee_id 应为 0（B 尚未 merge）
        let callee_id_after_a: i64 = cg_conn
            .query_row(
                "SELECT callee_id FROM calls WHERE callee_name = 'helper'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(callee_id_after_a, 0, "A merge 后 callee_id 应为 0");

        let r_b = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_b",
            42,
            "src/b.rs",
            "/app/src/b.rs",
            "ch_b",
            "rust",
            "/app",
        )
        .unwrap();
        assert_eq!(r_b.merge_status, "merged");
        assert_eq!(r_b.symbols_inserted, 1);

        // 5. 查询验证完整链路

        // 5a. symbols 表应有 2 行（fn_a + helper）
        let sym_count: i64 = cg_conn
            .query_row("SELECT COUNT(*) FROM symbols", [], |row| row.get(0))
            .unwrap();
        assert_eq!(sym_count, 2, "门槛 2: symbols 应有 2 行");

        // 5b. calls 表应有 1 行，callee_id 已 resolve（回扫 pass）
        let (call_count, resolved_calls): (i64, i64) = cg_conn
            .query_row(
                "SELECT COUNT(*), COUNT(CASE WHEN callee_id > 0 THEN 1 END) FROM calls",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(call_count, 1, "门槛 2: calls 应有 1 行");
        assert_eq!(
            resolved_calls, 1,
            "门槛 2: 回扫 pass 应 resolve 1 个 callee_id"
        );

        // 5c. file_instances 表应有 2 行（src/a.rs + src/b.rs）
        let fi_count: i64 = cg_conn
            .query_row("SELECT COUNT(*) FROM file_instances", [], |row| row.get(0))
            .unwrap();
        assert_eq!(fi_count, 2, "门槛 2: file_instances 应有 2 行");

        // 5d. workspace_manifests 表应有 2 行，file_size 正确
        //     merge 时由 ensure_manifest_schema 创建
        let manifest_exists_after: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table' AND name = 'workspace_manifests'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            manifest_exists_after, 1,
            "门槛 2: workspace_manifests 应在 merge 时创建"
        );

        let manifest_count: i64 = cg_conn
            .query_row("SELECT COUNT(*) FROM workspace_manifests", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(manifest_count, 2, "门槛 2: workspace_manifests 应有 2 行");

        let (a_file_size, b_file_size): (i64, i64) = cg_conn
            .query_row(
                "SELECT \
                 (SELECT file_size FROM workspace_manifests WHERE rel_path = 'src/a.rs'), \
                 (SELECT file_size FROM workspace_manifests WHERE rel_path = 'src/b.rs')",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(
            a_file_size, 150,
            "门槛 2: manifest A.file_size 应为 150 字节"
        );
        assert_eq!(b_file_size, 60, "门槛 2: manifest B.file_size 应为 60 字节");

        // 5e. symbol_contents 应有实际 content（非空）
        let empty_content_count: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM symbol_contents WHERE content = '' OR content IS NULL",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(empty_content_count, 0, "门槛 2: 不应有空 content 行");

        let fn_a_content: String = cg_conn
            .query_row(
                "SELECT content FROM symbol_contents WHERE content_hash = 'sh_fn_a'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            fn_a_content.contains("fn fn_a"),
            "content 应含 fn fn_a 源码"
        );

        // 5f. 跨文件 resolve 校验：A 中的 call 指向 B 的 helper symbol
        let (callee_id, is_cross_file): (i64, i64) = cg_conn
            .query_row(
                "SELECT callee_id, is_cross_file FROM calls WHERE callee_name = 'helper'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert!(callee_id > 0, "门槛 2: callee_id 应已 resolve");
        assert_eq!(is_cross_file, 1, "门槛 2: is_cross_file 应为 1");

        let callee_file: String = cg_conn
            .query_row(
                "SELECT fi.rel_path FROM symbols s \
                 JOIN file_instances fi ON s.file_instance_id = fi.id \
                 WHERE s.id = ?1",
                params![callee_id],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            callee_file, "src/b.rs",
            "门槛 2: callee 应指向 src/b.rs 的 helper"
        );

        // 5g. workspace 行存在
        let ws_count: i64 = cg_conn
            .query_row("SELECT COUNT(*) FROM workspaces WHERE id = 42", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(ws_count, 1, "门槛 2: workspace 42 应存在");
    }

    /// 门槛 2 E2E：fresh DB schema 初始化幂等性（多次调用不报错）
    #[test]
    fn test_e2e_fresh_db_schema_init_idempotent_e2e() {
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("idempotent.db");
        let cg_conn = Connection::open(&db_path).unwrap();

        // 第一次初始化
        init_codegraph_schema(&cg_conn).unwrap();
        let count_1: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'",
                [],
                |row| row.get(0),
            )
            .unwrap();

        // 第二次初始化（应幂等，不报错，表数量不变）
        init_codegraph_schema(&cg_conn).unwrap();
        let count_2: i64 = cg_conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type = 'table'",
                [],
                |row| row.get(0),
            )
            .unwrap();

        assert_eq!(count_1, count_2, "门槛 2: 幂等初始化不应增加表数量");

        // merge 仍能成功执行
        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        seed_cas(&cas_conn, "ck_idem", "ch_idem", "rust", 5);

        let r = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_idem",
            7,
            "src/x.rs",
            "/app/src/x.rs",
            "ch_idem",
            "rust",
            "/app",
        )
        .unwrap();
        assert_eq!(r.merge_status, "merged");
        assert_eq!(r.symbols_inserted, 2);
    }

    /// 门槛 2 E2E：fresh DB merge 后 file_instances.total_lines 与 cas_file_cache 一致
    #[test]
    fn test_e2e_fresh_db_total_lines_correctness() {
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("total_lines.db");
        let cg_conn = Connection::open(&db_path).unwrap();
        init_codegraph_schema(&cg_conn).unwrap();

        let cas_conn = Connection::open_in_memory().unwrap();
        make_cas_schema(&cas_conn);
        // file_size=300 字节, total_lines=30 行
        seed_cas_with_file_size(&cas_conn, "ck_tl", "ch_tl", "rust", 300, 30);

        let r = merge_cas_to_codegraph(
            &cas_conn,
            &cg_conn,
            "ck_tl",
            1,
            "src/main.rs",
            "/app/src/main.rs",
            "ch_tl",
            "rust",
            "/app",
        )
        .unwrap();
        assert_eq!(r.merge_status, "merged");

        // file_instances.total_lines 应为 30（行数）
        let fi_total_lines: i64 = cg_conn
            .query_row(
                "SELECT total_lines FROM file_instances \
                 WHERE workspace_id = 1 AND rel_path = 'src/main.rs'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            fi_total_lines, 30,
            "门槛 2: file_instances.total_lines 应为 30"
        );

        // file_contents.total_lines 也应为 30
        let fc_total_lines: i64 = cg_conn
            .query_row(
                "SELECT total_lines FROM file_contents WHERE content_hash = 'ch_tl'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(
            fc_total_lines, 30,
            "门槛 2: file_contents.total_lines 应为 30"
        );

        // manifest.file_size 应为 300（字节）
        let m_file_size: i64 = cg_conn
            .query_row(
                "SELECT file_size FROM workspace_manifests \
                 WHERE workspace_id = 1 AND rel_path = 'src/main.rs'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(m_file_size, 300, "门槛 2: manifest.file_size 应为 300 字节");
    }

    #[test]
    fn test_delete_workspace_file_is_idempotent_and_workspace_scoped() {
        let conn = Connection::open_in_memory().unwrap();
        init_codegraph_schema(&conn).unwrap();
        ensure_manifest_schema(&conn).unwrap();
        conn.execute_batch(
            "CREATE TABLE file_versions (\
                id INTEGER PRIMARY KEY,\
                file_instance_id INTEGER NOT NULL,\
                is_current INTEGER DEFAULT 1,\
                is_deleted INTEGER DEFAULT 0\
             );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at) \
             VALUES (1, 'ws1', '/ws1', 0), (2, 'ws2', '/ws2', 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_contents (content_hash, language, total_lines, first_seen_at) \
             VALUES ('shared-content', 'rust', 1, 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbol_contents \
             (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name) \
             VALUES ('sym-a', 'a', 'function', 'fn a() {}', '', 0, '', 'a'), \
                    ('sym-b', 'b', 'function', 'fn b() {}', '', 0, '', 'b')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_instances \
             (id, workspace_id, rel_path, abs_path, current_content_hash, mtime, status) \
             VALUES (10, 1, 'src/shared.rs', '/ws1/src/shared.rs', 'shared-content', 0, 'ok'), \
                    (20, 2, 'src/shared.rs', '/ws2/src/shared.rs', 'shared-content', 0, 'ok')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols \
             (id, file_instance_id, symbol_hash, name, kind, start_line, end_line, qualified_name) \
             VALUES (100, 10, 'sym-a', 'a', 'function', 1, 1, 'a'), \
                    (200, 20, 'sym-b', 'b', 'function', 1, 1, 'b')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO calls \
             (caller_id, caller_name, caller_module, callee_name, callee_id) \
             VALUES (100, 'a', '', 'b', 200), (200, 'b', '', 'a', 100)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_versions (id, file_instance_id, is_current, is_deleted) \
             VALUES (1, 10, 1, 0), (2, 20, 1, 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspace_manifests \
             (workspace_id, rel_path, content_hash, updated_at) \
             VALUES (1, 'src/shared.rs', 'shared-content', 0), \
                    (2, 'src/shared.rs', 'shared-content', 0)",
            [],
        )
        .unwrap();

        let result = delete_workspace_file_from_codegraph(&conn, 1, "src/shared.rs").unwrap();
        assert_eq!(result.delete_status, "deleted");
        assert_eq!(result.symbols_removed, 1);
        assert_eq!(result.outgoing_calls_removed, 1);
        assert_eq!(result.incoming_edges_cleared, 1);
        assert_eq!(result.manifest_removed, 1);
        assert_eq!(result.history_versions_marked, 1);

        let status: String = conn
            .query_row(
                "SELECT status FROM file_instances WHERE id = 10",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(status, "deleted");
        let incoming: (i64, i64) = conn
            .query_row(
                "SELECT COUNT(*), COALESCE(MAX(callee_id), -1) FROM calls WHERE caller_id = 200",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(incoming, (1, 0));
        let other_workspace_symbols: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM symbols WHERE file_instance_id = 20",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(other_workspace_symbols, 1);
        let other_manifest: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM workspace_manifests \
                 WHERE workspace_id = 2 AND rel_path = 'src/shared.rs'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(other_manifest, 1);
        let shared_contents: (i64, i64) = conn
            .query_row(
                "SELECT (SELECT COUNT(*) FROM file_contents), \
                        (SELECT COUNT(*) FROM symbol_contents)",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        assert_eq!(shared_contents, (1, 2));

        let repeated = delete_workspace_file_from_codegraph(&conn, 1, "src/shared.rs").unwrap();
        assert_eq!(repeated.delete_status, "already_deleted");
        assert_eq!(repeated.symbols_removed, 0);
        assert_eq!(repeated.outgoing_calls_removed, 0);
        assert_eq!(repeated.manifest_removed, 0);
    }

    #[test]
    fn test_delete_workspace_file_rolls_back_on_manifest_failure() {
        let conn = Connection::open_in_memory().unwrap();
        init_codegraph_schema(&conn).unwrap();
        ensure_manifest_schema(&conn).unwrap();
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at) \
             VALUES (1, 'ws1', '/ws1', 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_contents (content_hash, language, total_lines, first_seen_at) \
             VALUES ('content-a', 'rust', 1, 0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbol_contents \
             (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name) \
             VALUES ('sym-a', 'a', 'function', 'fn a() {}', '', 0, '', 'a')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_instances \
             (id, workspace_id, rel_path, abs_path, current_content_hash, mtime, status) \
             VALUES (10, 1, 'src/a.rs', '/ws1/src/a.rs', 'content-a', 0, 'ok')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols \
             (id, file_instance_id, symbol_hash, name, kind, start_line, end_line, qualified_name) \
             VALUES (100, 10, 'sym-a', 'a', 'function', 1, 1, 'a')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO calls \
             (caller_id, caller_name, caller_module, callee_name) \
             VALUES (100, 'a', '', 'external')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspace_manifests \
             (workspace_id, rel_path, content_hash, updated_at) \
             VALUES (1, 'src/a.rs', 'content-a', 0)",
            [],
        )
        .unwrap();
        conn.execute_batch(
            "CREATE TRIGGER fail_manifest_delete \
             BEFORE DELETE ON workspace_manifests \
             BEGIN SELECT RAISE(ABORT, 'forced manifest failure'); END;",
        )
        .unwrap();

        let error = delete_workspace_file_from_codegraph(&conn, 1, "src/a.rs").unwrap_err();
        assert!(error.contains("forced manifest failure"));
        let preserved: (String, i64, i64, i64) = conn
            .query_row(
                "SELECT \
                    (SELECT status FROM file_instances WHERE id = 10), \
                    (SELECT COUNT(*) FROM symbols WHERE file_instance_id = 10), \
                    (SELECT COUNT(*) FROM calls WHERE caller_id = 100), \
                    (SELECT COUNT(*) FROM workspace_manifests \
                     WHERE workspace_id = 1 AND rel_path = 'src/a.rs')",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();
        assert_eq!(preserved, ("ok".to_string(), 1, 1, 1));
    }
}
