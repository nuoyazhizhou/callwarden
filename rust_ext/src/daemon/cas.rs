//! CAS（Content-Addressable Storage）内容寻址存储 + file_generations 两阶段 CAS。
//!
//! 对应 Python：
//! - `db/db_cas.py`（CAS_SCHEMA_DDL + compute_cas_key_v1 + cas_lookup/publish/pin/gc +
//!   file_generation_seen/committed）
//!
//! 跨平台：rusqlite + sha2，Windows 可完整验收。
//! `cas_gc` 的跨平台 flock 用 fs2 crate（如不可用则降级为 BEGIN IMMEDIATE 保护），
//! 本 R5 阶段仅实现 cas_gc 的 mark-sweep 逻辑（不加 flock，留给后续集成）。
//!
//! 不变量（与 Python cas-gc-protocol.md 一致）：
//! - C3: building 状态残留由 GC 清理
//! - C9: pending_refs 未过期的 cas_key 视为 live
//! - C10: stale manifest commit 被 latest_seen_generation 条件 UPDATE 阻止

use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection};
use sha2::{Digest, Sha256};

// ============================================
// Schema DDL（与 Python db_cas.py:CAS_SCHEMA_DDL 一致）
// ============================================

/// CAS schema DDL（cas_file_cache + cas_symbol_contents + cas_symbols +
/// cas_raw_calls + cas_imports + cas_pending_refs + file_generations）
pub const CAS_SCHEMA_DDL: &str = r#"
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

CREATE TABLE IF NOT EXISTS cas_symbol_contents (
    content_hash TEXT PRIMARY KEY,
    content TEXT NOT NULL
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
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
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
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    UNIQUE(cas_key, caller_local_id, call_line, callee_name, call_ordinal)
);

CREATE TABLE IF NOT EXISTS cas_imports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cas_key TEXT NOT NULL,
    import_path TEXT NOT NULL,
    import_kind TEXT DEFAULT 'import',
    FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
    UNIQUE(cas_key, import_path, import_kind)
);

CREATE TABLE IF NOT EXISTS cas_pending_refs (
    cas_key TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (cas_key, workspace_id)
);

CREATE TABLE IF NOT EXISTS file_generations (
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    latest_session_id TEXT DEFAULT '',
    latest_session_epoch INTEGER DEFAULT 0,
    latest_seq INTEGER DEFAULT 0,
    latest_seen_generation TEXT DEFAULT '',
    latest_committed_generation TEXT DEFAULT '',
    PRIMARY KEY (workspace_id, rel_path)
);
"#;

pub const CAS_INDEX_SQL: &str = r#"
CREATE INDEX IF NOT EXISTS idx_cas_symbols_cas_key ON cas_symbols(cas_key);
CREATE INDEX IF NOT EXISTS idx_cas_symbols_content_hash ON cas_symbols(symbol_content_hash);
CREATE INDEX IF NOT EXISTS idx_cas_raw_calls_cas_key ON cas_raw_calls(cas_key);
CREATE INDEX IF NOT EXISTS idx_cas_file_cache_content_hash ON cas_file_cache(content_hash);
CREATE INDEX IF NOT EXISTS idx_cas_file_cache_state ON cas_file_cache(state);
"#;

// ============================================
// CAS key 计算（与 Python compute_cas_key_v1 一致）
// ============================================

/// 计算 CAS key——全文档唯一的 CAS key 计算函数。
///
/// 输入：content_hash | language | parser_version | callwarden_version |
///       extraction_config_version | abi_version | input_abi_version
/// 输出：sha256(raw).hexdigest()（64 字符 hex）
pub fn compute_cas_key_v1(
    content_hash: &str,
    language: &str,
    parser_version: &str,
    callwarden_version: &str,
    extraction_config_version: &str,
    abi_version: &str,
    input_abi_version: &str,
) -> String {
    let raw = format!(
        "{}|{}|{}|{}|{}|{}|{}",
        content_hash,
        language,
        parser_version,
        callwarden_version,
        extraction_config_version,
        abi_version,
        input_abi_version
    );
    let mut hasher = Sha256::new();
    hasher.update(raw.as_bytes());
    let full = hasher.finalize();
    hex_encode(&full)
}

/// 符号内容 hash（与 Python db_cas.py 阶段 2 一致：sha256(content).hexdigest()）
pub fn compute_symbol_content_hash(content: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(content.as_bytes());
    let full = hasher.finalize();
    hex_encode(&full)
}

fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ============================================
// CasStore: 封装 SQLite 连接 + 提供 CAS 操作
// ============================================

/// CAS 输入符号（对应 Python parse_result["symbols"][i]）
#[derive(Debug, Clone)]
pub struct CasSymbolInput {
    pub name: String,
    pub qualified_name: String,
    pub parent_id: Option<i64>,
    pub kind: String,
    pub start_line: i64,
    pub end_line: i64,
    pub start_col: i64,
    pub end_col: i64,
    pub start_byte: i64,
    pub end_byte: i64,
    pub visibility: String,
    pub signature: String,
    pub has_comment: bool,
    pub depth: i64,
    pub content: String,
}

/// CAS 输入 raw call（对应 Python parse_result["raw_calls"][i]）
#[derive(Debug, Clone)]
pub struct CasRawCallInput {
    pub caller_id: Option<i64>,
    pub caller_name: String,
    pub callee_name: String,
    pub line: i64,
    pub ordinal: i64,
}

/// CAS 输入 import（对应 Python parse_result["imports"][i]）
#[derive(Debug, Clone)]
pub struct CasImportInput {
    pub path: String,
    pub kind: String,
}

/// CAS 发布输入（对应 Python parse_result dict）
#[derive(Debug, Clone, Default)]
pub struct CasPublishInput {
    pub file_size: i64,
    pub total_lines: i64,
    pub symbols: Vec<CasSymbolInput>,
    pub raw_calls: Vec<CasRawCallInput>,
    pub imports: Vec<CasImportInput>,
}

/// CAS store：封装 rusqlite Connection，提供 CAS 原子发布 / lookup / pin / GC
pub struct CasStore {
    conn: Mutex<Connection>,
}

impl CasStore {
    /// 打开指定路径的 CAS DB（不存在则创建并初始化 schema）
    pub fn open(db_path: &str) -> Result<Self, rusqlite::Error> {
        if let Some(parent) = std::path::Path::new(db_path).parent() {
            if !parent.as_os_str().is_empty() {
                let _ = std::fs::create_dir_all(parent);
            }
        }
        let conn = Connection::open(db_path)?;
        Self::init_conn(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    /// 内存数据库（测试用）
    pub fn open_in_memory() -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        Self::init_conn(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
    }

    fn init_conn(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch("PRAGMA busy_timeout=5000;")?;
        conn.execute_batch("PRAGMA journal_mode=WAL;")?;
        conn.execute_batch(CAS_SCHEMA_DDL)?;
        conn.execute_batch(CAS_INDEX_SQL)?;
        Ok(())
    }

    /// 查询 CAS 是否命中（state='ready'）。返回 Some(json) 表示命中。
    pub fn lookup(&self, cas_key: &str) -> Result<Option<CasFileCacheRow>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT cas_key, content_hash, language, file_size, total_lines,
                    parser_version, callwarden_version, extraction_config_version,
                    abi_version, input_abi_version, state, parsed_at
             FROM cas_file_cache WHERE cas_key = ?1 AND state = 'ready'",
        )?;
        let mut rows = stmt.query(params![cas_key])?;
        if let Some(row) = rows.next()? {
            Ok(Some(CasFileCacheRow {
                cas_key: row.get(0)?,
                content_hash: row.get(1)?,
                language: row.get(2)?,
                file_size: row.get(3)?,
                total_lines: row.get(4)?,
                parser_version: row.get(5)?,
                callwarden_version: row.get(6)?,
                extraction_config_version: row.get(7)?,
                abi_version: row.get(8)?,
                input_abi_version: row.get(9)?,
                state: row.get(10)?,
                parsed_at: row.get(11)?,
            }))
        } else {
            Ok(None)
        }
    }

    /// CAS 原子发布——四阶段：building → payload → raw calls → ready。
    ///
    /// 对应 Python db_cas.py:cas_publish。BEGIN IMMEDIATE 保证原子性，
    /// 崩溃后 building 残留由 GC 清理（不变量 C3）。
    pub fn publish(
        &self,
        cas_key: &str,
        content_hash: &str,
        language: &str,
        parse_result: &CasPublishInput,
        parser_version: &str,
        callwarden_version: &str,
        extraction_config_version: &str,
        abi_version: &str,
        input_abi_version: &str,
    ) -> Result<(), CasPublishError> {
        let now = now_ts();
        let conn = self.conn.lock().unwrap();

        // BEGIN IMMEDIATE 获取写锁
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = self.publish_inner(
            &conn,
            cas_key,
            content_hash,
            language,
            parse_result,
            parser_version,
            callwarden_version,
            extraction_config_version,
            abi_version,
            input_abi_version,
            now,
        );
        match result {
            Ok(()) => {
                conn.execute_batch("COMMIT")?;
                Ok(())
            }
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    fn publish_inner(
        &self,
        conn: &Connection,
        cas_key: &str,
        content_hash: &str,
        language: &str,
        parse_result: &CasPublishInput,
        parser_version: &str,
        callwarden_version: &str,
        extraction_config_version: &str,
        abi_version: &str,
        input_abi_version: &str,
        now: f64,
    ) -> Result<(), CasPublishError> {
        // 阶段 1: 插入 building 状态（INSERT OR IGNORE 保证幂等）
        conn.execute(
            "INSERT OR IGNORE INTO cas_file_cache
             (cas_key, content_hash, language, file_size, total_lines,
              parser_version, callwarden_version, extraction_config_version,
              abi_version, input_abi_version, state, parsed_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 'building', ?11)",
            params![
                cas_key,
                content_hash,
                language,
                parse_result.file_size,
                parse_result.total_lines,
                parser_version,
                callwarden_version,
                extraction_config_version,
                abi_version,
                input_abi_version,
                now,
            ],
        )
        .map_err(|e| CasPublishError::Sqlite(e))?;

        // 阶段 2: 写入符号正文（批量 + 预计算 hash 供阶段 3 复用）
        let mut sym_hash_map: Vec<String> = Vec::with_capacity(parse_result.symbols.len());
        let mut sym_content_rows: Vec<(String, String)> =
            Vec::with_capacity(parse_result.symbols.len());
        for sym in &parse_result.symbols {
            let sym_content_hash = compute_symbol_content_hash(&sym.content);
            sym_hash_map.push(sym_content_hash.clone());
            sym_content_rows.push((sym_content_hash, sym.content.clone()));
        }
        if !sym_content_rows.is_empty() {
            // executemany 等价：循环 execute 或 prepared statement
            let mut stmt = conn.prepare(
                "INSERT OR IGNORE INTO cas_symbol_contents (content_hash, content) VALUES (?1, ?2)",
            )?;
            for (hash, content) in &sym_content_rows {
                stmt.execute(params![hash, content])?;
            }
        }

        // 阶段 3: 写入符号（INSERT OR REPLACE，复用预计算的 hash）
        if !parse_result.symbols.is_empty() {
            let mut stmt = conn.prepare(
                "INSERT OR REPLACE INTO cas_symbols
                 (cas_key, local_symbol_id, symbol_content_hash, name,
                  local_qualified_name, lexical_parent_local_id, kind,
                  start_line, end_line, start_col, end_col, start_byte, end_byte,
                  visibility, signature, has_comment, depth)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14, ?15, ?16, ?17)",
            )?;
            for (i, sym) in parse_result.symbols.iter().enumerate() {
                stmt.execute(params![
                    cas_key,
                    i as i64,
                    &sym_hash_map[i],
                    &sym.name,
                    &sym.qualified_name,
                    sym.parent_id,
                    &sym.kind,
                    sym.start_line,
                    sym.end_line,
                    sym.start_col,
                    sym.end_col,
                    sym.start_byte,
                    sym.end_byte,
                    &sym.visibility,
                    &sym.signature,
                    sym.has_comment as i64,
                    sym.depth,
                ])?;
            }
        }

        // 阶段 3b: 写入 raw calls（INSERT OR IGNORE）
        if !parse_result.raw_calls.is_empty() {
            let mut stmt = conn.prepare(
                "INSERT OR IGNORE INTO cas_raw_calls
                 (cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            )?;
            for call in &parse_result.raw_calls {
                stmt.execute(params![
                    cas_key,
                    call.caller_id,
                    &call.caller_name,
                    &call.callee_name,
                    call.line,
                    call.ordinal,
                ])?;
            }
        }

        // 阶段 3c: 写入 imports（INSERT OR IGNORE）
        if !parse_result.imports.is_empty() {
            let mut stmt = conn.prepare(
                "INSERT OR IGNORE INTO cas_imports
                 (cas_key, import_path, import_kind) VALUES (?1, ?2, ?3)",
            )?;
            for imp in &parse_result.imports {
                stmt.execute(params![cas_key, &imp.path, &imp.kind])?;
            }
        }

        // 阶段 4: 原子切换 ready
        conn.execute(
            "UPDATE cas_file_cache SET state = 'ready' WHERE cas_key = ?1",
            params![cas_key],
        )?;
        Ok(())
    }

    /// 添加 CAS pending ref（GC 保护窗口）。
    /// 对应 Python db_cas.py:cas_pin
    pub fn pin(
        &self,
        cas_key: &str,
        workspace_id: i64,
        ttl_seconds: f64,
    ) -> Result<(), rusqlite::Error> {
        let now = now_ts();
        let expires_at = now + ttl_seconds;
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO cas_pending_refs
             (cas_key, workspace_id, expires_at, created_at) VALUES (?1, ?2, ?3, ?4)",
            params![cas_key, workspace_id, expires_at, now],
        )?;
        Ok(())
    }

    /// CAS GC——mark-sweep 实现（对应 Python db_cas.py:cas_gc）
    ///
    /// 注意：本 R5 阶段不加 flock（跨平台 flock 留给后续），仅靠 BEGIN IMMEDIATE 保护。
    /// live_keys: 仍然存活的 cas_key 集合（manifest 引用）
    /// grace_period_days: 未使用（保留接口与 Python 一致）
    pub fn gc(
        &self,
        live_keys: &std::collections::HashSet<String>,
        _grace_period_days: f64,
    ) -> Result<CasGcStats, CasPublishError> {
        let now = now_ts();
        let conn = self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = self.gc_inner(&conn, live_keys, now);
        match result {
            Ok(stats) => {
                conn.execute_batch("COMMIT")?;
                Ok(stats)
            }
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    fn gc_inner(
        &self,
        conn: &Connection,
        live_keys: &std::collections::HashSet<String>,
        now: f64,
    ) -> Result<CasGcStats, CasPublishError> {
        // 阶段 2: pending_refs 未过期的 cas_key 并入 live set（不变量 C9）
        let mut all_live: std::collections::HashSet<String> = live_keys.clone();
        let mut stmt = conn.prepare(
            "SELECT DISTINCT cas_key FROM cas_pending_refs WHERE expires_at > ?1",
        )?;
        let pending_keys: Vec<String> = stmt
            .query_map(params![now], |row| row.get(0))?
            .filter_map(|r| r.ok())
            .collect();
        for k in pending_keys {
            all_live.insert(k);
        }

        // 创建临时 live 表
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _gc_live (cas_key TEXT PRIMARY KEY)", [])?;
        conn.execute("DELETE FROM _gc_live", [])?;
        // 批量插入 live keys
        {
            let mut stmt = conn.prepare("INSERT OR IGNORE INTO _gc_live VALUES (?1)")?;
            for k in &all_live {
                stmt.execute(params![k])?;
            }
        }

        let mut stats = CasGcStats::default();

        // 3a. 先删子表
        let deleted_symbols = conn.execute(
            "DELETE FROM cas_symbols WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)",
            [],
        )?;
        let deleted_raw_calls = conn.execute(
            "DELETE FROM cas_raw_calls WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)",
            [],
        )?;
        let deleted_imports = conn.execute(
            "DELETE FROM cas_imports WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)",
            [],
        )?;

        // 3b. 再删正文表（content_hash 不再被任何 symbol 引用）
        let deleted_symbol_contents = conn.execute(
            "DELETE FROM cas_symbol_contents WHERE content_hash NOT IN
             (SELECT DISTINCT symbol_content_hash FROM cas_symbols)",
            [],
        )?;

        // 3c. 最后删父表（只删 ready）
        let deleted_files = conn.execute(
            "DELETE FROM cas_file_cache WHERE cas_key NOT IN (SELECT cas_key FROM _gc_live)
             AND state = 'ready'",
            [],
        )?;

        // 3d. 清理孤儿 building 条目（不变量 C3）
        let orphan_symbols = conn.execute(
            "DELETE FROM cas_symbols WHERE cas_key IN
             (SELECT cas_key FROM cas_file_cache WHERE state = 'building')",
            [],
        )?;
        let orphan_raw_calls = conn.execute(
            "DELETE FROM cas_raw_calls WHERE cas_key IN
             (SELECT cas_key FROM cas_file_cache WHERE state = 'building')",
            [],
        )?;
        let orphan_imports = conn.execute(
            "DELETE FROM cas_imports WHERE cas_key IN
             (SELECT cas_key FROM cas_file_cache WHERE state = 'building')",
            [],
        )?;
        let orphan_symbol_contents = conn.execute(
            "DELETE FROM cas_symbol_contents WHERE content_hash NOT IN
             (SELECT DISTINCT symbol_content_hash FROM cas_symbols)",
            [],
        )?;
        let orphan_files = conn.execute("DELETE FROM cas_file_cache WHERE state = 'building'", [])?;

        // 3e. 清理过期 pending_refs
        let expired_refs = conn.execute("DELETE FROM cas_pending_refs WHERE expires_at <= ?1", params![now])?;

        conn.execute("DROP TABLE _gc_live", [])?;

        stats.deleted_symbols = deleted_symbols + orphan_symbols;
        stats.deleted_raw_calls = deleted_raw_calls + orphan_raw_calls;
        stats.deleted_imports = deleted_imports + orphan_imports;
        stats.deleted_symbol_contents = deleted_symbol_contents + orphan_symbol_contents;
        stats.deleted_files = deleted_files + orphan_files;
        stats.expired_refs = expired_refs;
        Ok(stats)
    }

    // ---- file_generations 两阶段 CAS（对应 Python db_cas.py:file_generation_seen/committed）----

    /// 第一阶段 seen：记录已看到的 generation。
    ///
    /// 协议：BEGIN IMMEDIATE → 条件 UPDATE（generation < incoming 才更新）→ COMMIT
    /// stale seq 直接丢弃，不报错。
    ///
    /// 返回：true=seen 更新成功，false=stale seq
    pub fn file_generation_seen(
        &self,
        workspace_id: i64,
        rel_path: &str,
        session_id: &str,
        epoch: i64,
        seq: i64,
    ) -> Result<bool, CasPublishError> {
        let incoming_gen = format!("{}:{}", epoch, seq);
        let conn = self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = Self::file_generation_seen_inner(
            &conn,
            workspace_id,
            rel_path,
            session_id,
            epoch,
            seq,
            &incoming_gen,
        );
        match result {
            Ok(seen) => {
                conn.execute_batch("COMMIT")?;
                Ok(seen)
            }
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    fn file_generation_seen_inner(
        conn: &Connection,
        workspace_id: i64,
        rel_path: &str,
        session_id: &str,
        epoch: i64,
        seq: i64,
        incoming_gen: &str,
    ) -> Result<bool, CasPublishError> {
        // 确保 file_generations 行存在
        conn.execute(
            "INSERT OR IGNORE INTO file_generations
             (workspace_id, rel_path, latest_session_id, latest_session_epoch,
              latest_seq, latest_seen_generation, latest_committed_generation)
             VALUES (?1, ?2, '', 0, 0, '', '')",
            params![workspace_id, rel_path],
        )?;

        // 检查是否 stale
        let mut stmt = conn.prepare(
            "SELECT latest_seen_generation FROM file_generations
             WHERE workspace_id = ?1 AND rel_path = ?2",
        )?;
        let mut rows = stmt.query(params![workspace_id, rel_path])?;
        let existing: Option<String> = if let Some(row) = rows.next()? {
            row.get(0)?
        } else {
            None
        };

        if let Some(existing_gen) = existing {
            if !existing_gen.is_empty() {
                // 比较 generation：格式 "epoch:seq"
                if let Some((existing_epoch, existing_seq)) = parse_generation(&existing_gen) {
                    if epoch < existing_epoch || (epoch == existing_epoch && seq <= existing_seq) {
                        // stale：incoming_gen <= latest_seen
                        return Ok(false);
                    }
                }
                // 格式异常则允许更新
            }
        }

        // 更新 seen generation
        conn.execute(
            "UPDATE file_generations SET
             latest_session_id = ?1, latest_session_epoch = ?2,
             latest_seq = ?3, latest_seen_generation = ?4
             WHERE workspace_id = ?5 AND rel_path = ?6",
            params![session_id, epoch, seq, incoming_gen, workspace_id, rel_path],
        )?;
        Ok(true)
    }

    /// 第二阶段 committed：条件 UPDATE 确认 manifest 已提交。
    ///
    /// 返回：true=committed 更新成功，false=stale（其他 handler 已覆盖 seen）
    pub fn file_generation_committed(
        &self,
        workspace_id: i64,
        rel_path: &str,
        epoch: i64,
        seq: i64,
    ) -> Result<bool, CasPublishError> {
        let incoming_gen = format!("{}:{}", epoch, seq);
        let conn = self.conn.lock().unwrap();
        conn.execute_batch("BEGIN IMMEDIATE")?;
        let result = Self::file_generation_committed_inner(
            &conn,
            workspace_id,
            rel_path,
            epoch,
            seq,
            &incoming_gen,
        );
        match result {
            Ok(committed) => {
                conn.execute_batch("COMMIT")?;
                Ok(committed)
            }
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK");
                Err(e)
            }
        }
    }

    fn file_generation_committed_inner(
        conn: &Connection,
        workspace_id: i64,
        rel_path: &str,
        _epoch: i64,
        _seq: i64,
        incoming_gen: &str,
    ) -> Result<bool, CasPublishError> {
        // 条件 UPDATE：只有 latest_seen_generation = incoming_gen 时才更新
        let affected = conn.execute(
            "UPDATE file_generations SET latest_committed_generation = ?1
             WHERE workspace_id = ?2 AND rel_path = ?3
             AND latest_seen_generation = ?4",
            params![incoming_gen, workspace_id, rel_path, incoming_gen],
        )?;
        Ok(affected == 1)
    }

    /// 查询 file_generation 状态（用于测试和调试）
    pub fn get_file_generation(
        &self,
        workspace_id: i64,
        rel_path: &str,
    ) -> Result<Option<FileGenerationRow>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT workspace_id, rel_path, latest_session_id, latest_session_epoch,
                    latest_seq, latest_seen_generation, latest_committed_generation
             FROM file_generations WHERE workspace_id = ?1 AND rel_path = ?2",
        )?;
        let mut rows = stmt.query(params![workspace_id, rel_path])?;
        if let Some(row) = rows.next()? {
            Ok(Some(FileGenerationRow {
                workspace_id: row.get(0)?,
                rel_path: row.get(1)?,
                latest_session_id: row.get(2)?,
                latest_session_epoch: row.get(3)?,
                latest_seq: row.get(4)?,
                latest_seen_generation: row.get(5)?,
                latest_committed_generation: row.get(6)?,
            }))
        } else {
            Ok(None)
        }
    }

    /// 统计 cas_file_cache 行数（用于测试）
    pub fn count_cas_files(&self) -> Result<i64, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        conn.query_row("SELECT COUNT(*) FROM cas_file_cache", [], |row| row.get(0))
    }

    /// 查询 cas_file_cache state（用于测试）
    pub fn get_cas_state(&self, cas_key: &str) -> Result<Option<String>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare("SELECT state FROM cas_file_cache WHERE cas_key = ?1")?;
        let mut rows = stmt.query(params![cas_key])?;
        if let Some(row) = rows.next()? {
            Ok(Some(row.get(0)?))
        } else {
            Ok(None)
        }
    }
}

/// 解析 "epoch:seq" 字符串为 (epoch, seq)
fn parse_generation(gen: &str) -> Option<(i64, i64)> {
    let mut parts = gen.splitn(2, ':');
    let epoch_str = parts.next()?;
    let seq_str = parts.next()?;
    Some((epoch_str.parse().ok()?, seq_str.parse().ok()?))
}

// ============================================
// 数据结构
// ============================================

/// cas_file_cache 行（对应 Python cas_lookup 返回的 dict）
#[derive(Debug, Clone)]
pub struct CasFileCacheRow {
    pub cas_key: String,
    pub content_hash: String,
    pub language: String,
    pub file_size: i64,
    pub total_lines: i64,
    pub parser_version: String,
    pub callwarden_version: String,
    pub extraction_config_version: String,
    pub abi_version: String,
    pub input_abi_version: String,
    pub state: String,
    pub parsed_at: f64,
}

/// file_generations 行
#[derive(Debug, Clone)]
pub struct FileGenerationRow {
    pub workspace_id: i64,
    pub rel_path: String,
    pub latest_session_id: String,
    pub latest_session_epoch: i64,
    pub latest_seq: i64,
    pub latest_seen_generation: String,
    pub latest_committed_generation: String,
}

/// CAS 发布错误
#[derive(Debug)]
pub enum CasPublishError {
    Sqlite(rusqlite::Error),
}

impl std::fmt::Display for CasPublishError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            CasPublishError::Sqlite(e) => write!(f, "SQLite 错误: {}", e),
        }
    }
}

impl std::error::Error for CasPublishError {}

impl From<rusqlite::Error> for CasPublishError {
    fn from(e: rusqlite::Error) -> Self {
        CasPublishError::Sqlite(e)
    }
}

/// CAS GC 统计
#[derive(Debug, Default, Clone)]
pub struct CasGcStats {
    pub deleted_symbols: usize,
    pub deleted_raw_calls: usize,
    pub deleted_imports: usize,
    pub deleted_symbol_contents: usize,
    pub deleted_files: usize,
    pub expired_refs: usize,
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::HashSet;

    fn make_store() -> CasStore {
        CasStore::open_in_memory().unwrap()
    }

    fn make_input() -> CasPublishInput {
        CasPublishInput {
            file_size: 100,
            total_lines: 10,
            symbols: vec![
                CasSymbolInput {
                    name: "foo".to_string(),
                    qualified_name: "module.foo".to_string(),
                    parent_id: None,
                    kind: "fn".to_string(),
                    start_line: 1,
                    end_line: 5,
                    start_col: 0,
                    end_col: 0,
                    start_byte: 0,
                    end_byte: 50,
                    visibility: "public".to_string(),
                    signature: "fn foo()".to_string(),
                    has_comment: false,
                    depth: -1,
                    content: "fn foo() {}".to_string(),
                },
                CasSymbolInput {
                    name: "bar".to_string(),
                    qualified_name: "module.bar".to_string(),
                    parent_id: None,
                    kind: "fn".to_string(),
                    start_line: 7,
                    end_line: 9,
                    start_col: 0,
                    end_col: 0,
                    start_byte: 60,
                    end_byte: 100,
                    visibility: "private".to_string(),
                    signature: "fn bar()".to_string(),
                    has_comment: true,
                    depth: 0,
                    content: "fn bar() {}".to_string(),
                },
            ],
            raw_calls: vec![CasRawCallInput {
                caller_id: Some(0),
                caller_name: "foo".to_string(),
                callee_name: "bar".to_string(),
                line: 3,
                ordinal: 0,
            }],
            imports: vec![CasImportInput {
                path: "std::collections".to_string(),
                kind: "use".to_string(),
            }],
        }
    }

    // ---- compute_cas_key_v1 测试 ----

    #[test]
    fn test_compute_cas_key_v1_deterministic() {
        let k1 = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let k2 = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        assert_eq!(k1, k2);
        assert_eq!(k1.len(), 64); // sha256 hex = 64 字符
    }

    #[test]
    fn test_compute_cas_key_v1_differs_on_inputs() {
        let base = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        // 任一字段不同，cas_key 应不同
        assert_ne!(
            base,
            compute_cas_key_v1("hash2", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1")
        );
        assert_ne!(
            base,
            compute_cas_key_v1("hash1", "python", "0.1.0", "0.2.0", "v1", "v1", "v1")
        );
        assert_ne!(
            base,
            compute_cas_key_v1("hash1", "rust", "0.2.0", "0.2.0", "v1", "v1", "v1")
        );
    }

    #[test]
    fn test_compute_symbol_content_hash_deterministic() {
        let h1 = compute_symbol_content_hash("fn foo() {}");
        let h2 = compute_symbol_content_hash("fn foo() {}");
        assert_eq!(h1, h2);
        assert_eq!(h1.len(), 64);
    }

    // ---- CasStore schema 初始化测试 ----

    #[test]
    fn test_open_in_memory_initializes_schema() {
        let store = make_store();
        // 表应该存在（count 不报错即表示表存在）
        assert_eq!(store.count_cas_files().unwrap(), 0);
    }

    // ---- cas_publish + cas_lookup 测试 ----

    #[test]
    fn test_publish_then_lookup_returns_ready() {
        let store = make_store();
        let cas_key = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let input = make_input();

        store
            .publish(
                &cas_key,
                "hash1",
                "rust",
                &input,
                "0.1.0",
                "0.2.0",
                "v1",
                "v1",
                "v1",
            )
            .unwrap();

        // 查询应命中，state='ready'
        let row = store.lookup(&cas_key).unwrap();
        assert!(row.is_some());
        let row = row.unwrap();
        assert_eq!(row.cas_key, cas_key);
        assert_eq!(row.content_hash, "hash1");
        assert_eq!(row.language, "rust");
        assert_eq!(row.state, "ready");
        assert_eq!(row.file_size, 100);
        assert_eq!(row.total_lines, 10);
    }

    #[test]
    fn test_lookup_returns_none_for_missing() {
        let store = make_store();
        let row = store.lookup("nonexistent").unwrap();
        assert!(row.is_none());
    }

    #[test]
    fn test_lookup_returns_none_for_building_state() {
        let store = make_store();
        let cas_key = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let input = make_input();

        // 直接 INSERT building 状态（模拟崩溃残留）
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO cas_file_cache
                 (cas_key, content_hash, language, file_size, total_lines,
                  parser_version, callwarden_version, extraction_config_version,
                  abi_version, input_abi_version, state, parsed_at)
                 VALUES (?1, ?2, ?3, 0, 0, 'pv', 'cv', 'ec', 'av', 'iav', 'building', 0.0)",
                params![cas_key, "hash1", "rust"],
            )
            .unwrap();
        }

        // lookup 应返回 None（state != 'ready'）
        let row = store.lookup(&cas_key).unwrap();
        assert!(row.is_none());
    }

    // ---- cas_publish 幂等性测试 ----

    #[test]
    fn test_publish_idempotent_same_cas_key() {
        let store = make_store();
        let cas_key = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let input = make_input();

        // 相同 cas_key publish 两次应该成功（INSERT OR IGNORE + UPDATE ready）
        store
            .publish(&cas_key, "hash1", "rust", &input, "0.1.0", "0.2.0", "v1", "v1", "v1")
            .unwrap();
        store
            .publish(&cas_key, "hash1", "rust", &input, "0.1.0", "0.2.0", "v1", "v1", "v1")
            .unwrap();

        // 仍然只有一条记录，state='ready'
        assert_eq!(store.count_cas_files().unwrap(), 1);
        assert_eq!(store.get_cas_state(&cas_key).unwrap(), Some("ready".to_string()));
    }

    #[test]
    fn test_publish_with_empty_symbols() {
        let store = make_store();
        let cas_key = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let input = CasPublishInput {
            file_size: 0,
            total_lines: 0,
            symbols: vec![],
            raw_calls: vec![],
            imports: vec![],
        };

        store
            .publish(&cas_key, "hash1", "rust", &input, "0.1.0", "0.2.0", "v1", "v1", "v1")
            .unwrap();
        assert_eq!(store.get_cas_state(&cas_key).unwrap(), Some("ready".to_string()));
    }

    // ---- cas_pin 测试 ----

    #[test]
    fn test_pin_inserts_pending_ref() {
        let store = make_store();
        let cas_key = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let input = make_input();
        store
            .publish(&cas_key, "hash1", "rust", &input, "0.1.0", "0.2.0", "v1", "v1", "v1")
            .unwrap();

        // pin
        store.pin(&cas_key, 1, 3600.0).unwrap();

        // 验证 pin 已写入
        let conn = store.conn.lock().unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cas_pending_refs WHERE cas_key = ?1 AND workspace_id = 1",
                params![cas_key],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn test_pin_idempotent_same_workspace() {
        let store = make_store();
        let cas_key = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let input = make_input();
        store
            .publish(&cas_key, "hash1", "rust", &input, "0.1.0", "0.2.0", "v1", "v1", "v1")
            .unwrap();

        // 同一 workspace pin 两次（INSERT OR REPLACE 幂等）
        store.pin(&cas_key, 1, 3600.0).unwrap();
        store.pin(&cas_key, 1, 3600.0).unwrap();

        let conn = store.conn.lock().unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cas_pending_refs WHERE cas_key = ?1 AND workspace_id = 1",
                params![cas_key],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }

    // ---- cas_gc 测试 ----

    #[test]
    fn test_gc_deletes_unreferenced_files() {
        let store = make_store();
        let cas_key1 = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let cas_key2 = compute_cas_key_v1("hash2", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let input = make_input();
        store
            .publish(&cas_key1, "hash1", "rust", &input, "0.1.0", "0.2.0", "v1", "v1", "v1")
            .unwrap();
        store
            .publish(&cas_key2, "hash2", "rust", &input, "0.1.0", "0.2.0", "v1", "v1", "v1")
            .unwrap();
        assert_eq!(store.count_cas_files().unwrap(), 2);

        // live set 只保留 cas_key1
        let mut live = HashSet::new();
        live.insert(cas_key1.clone());
        let stats = store.gc(&live, 7.0).unwrap();

        // cas_key2 应该被删除
        assert_eq!(store.count_cas_files().unwrap(), 1);
        assert!(store.get_cas_state(&cas_key2).is_ok());
        assert_eq!(store.get_cas_state(&cas_key2).unwrap(), None);
        // cas_key1 应该保留
        assert_eq!(
            store.get_cas_state(&cas_key1).unwrap(),
            Some("ready".to_string())
        );
        // 统计应该删除了 1 个 file + 2 个 symbols + 1 raw_call + 1 import
        assert_eq!(stats.deleted_files, 1);
        assert_eq!(stats.deleted_symbols, 2);
    }

    #[test]
    fn test_gc_preserves_pinned_keys() {
        let store = make_store();
        let cas_key1 = compute_cas_key_v1("hash1", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let cas_key2 = compute_cas_key_v1("hash2", "rust", "0.1.0", "0.2.0", "v1", "v1", "v1");
        let input = make_input();
        store
            .publish(&cas_key1, "hash1", "rust", &input, "0.1.0", "0.2.0", "v1", "v1", "v1")
            .unwrap();
        store
            .publish(&cas_key2, "hash2", "rust", &input, "0.1.0", "0.2.0", "v1", "v1", "v1")
            .unwrap();

        // pin cas_key2（GC 保护窗口）
        store.pin(&cas_key2, 1, 3600.0).unwrap();

        // live set 为空（即没有任何 manifest 引用）
        let live = HashSet::new();
        let stats = store.gc(&live, 7.0).unwrap();

        // cas_key1 应该被删除，cas_key2 因为 pin 保留
        assert_eq!(store.count_cas_files().unwrap(), 1);
        assert_eq!(
            store.get_cas_state(&cas_key1).unwrap(),
            None,
            "cas_key1 应该被删除"
        );
        assert_eq!(
            store.get_cas_state(&cas_key2).unwrap(),
            Some("ready".to_string()),
            "cas_key2 应该因 pin 保留"
        );
    }

    #[test]
    fn test_gc_cleans_orphan_building_state() {
        let store = make_store();
        // 直接插入 building 状态（模拟崩溃残留，不变量 C3）
        let cas_key = "orphan_building_key";
        {
            let conn = store.conn.lock().unwrap();
            conn.execute(
                "INSERT INTO cas_file_cache
                 (cas_key, content_hash, language, file_size, total_lines,
                  parser_version, callwarden_version, extraction_config_version,
                  abi_version, input_abi_version, state, parsed_at)
                 VALUES (?1, 'hash', 'rust', 0, 0, 'pv', 'cv', 'ec', 'av', 'iav', 'building', 0.0)",
                params![cas_key],
            )
            .unwrap();
        }

        // GC 应该清理 building 状态
        let live = HashSet::new();
        let stats = store.gc(&live, 7.0).unwrap();

        assert_eq!(store.count_cas_files().unwrap(), 0);
        assert_eq!(stats.deleted_files, 1); // orphan_files
    }

    // ---- file_generation_seen 测试 ----

    #[test]
    fn test_file_generation_seen_first_time() {
        let store = make_store();
        // 首次 seen 应该成功
        let seen = store
            .file_generation_seen(1, "src/main.rs", "session-1", 1, 1)
            .unwrap();
        assert!(seen);

        let row = store.get_file_generation(1, "src/main.rs").unwrap().unwrap();
        assert_eq!(row.latest_session_id, "session-1");
        assert_eq!(row.latest_session_epoch, 1);
        assert_eq!(row.latest_seq, 1);
        assert_eq!(row.latest_seen_generation, "1:1");
        assert_eq!(row.latest_committed_generation, ""); // 还没 committed
    }

    #[test]
    fn test_file_generation_seen_rejects_stale_seq() {
        let store = make_store();
        // epoch=1, seq=5
        store.file_generation_seen(1, "src/main.rs", "session-1", 1, 5).unwrap();

        // stale: epoch=1, seq=3（小于 5）
        let seen = store
            .file_generation_seen(1, "src/main.rs", "session-1", 1, 3)
            .unwrap();
        assert!(!seen, "stale seq 应该被拒绝");

        // row 不应该被更新
        let row = store.get_file_generation(1, "src/main.rs").unwrap().unwrap();
        assert_eq!(row.latest_seq, 5);
    }

    #[test]
    fn test_file_generation_seen_rejects_stale_epoch() {
        let store = make_store();
        // epoch=2, seq=1
        store.file_generation_seen(1, "src/main.rs", "session-2", 2, 1).unwrap();

        // stale: epoch=1（小于 2）
        let seen = store
            .file_generation_seen(1, "src/main.rs", "session-1", 1, 100)
            .unwrap();
        assert!(!seen, "stale epoch 应该被拒绝");
    }

    #[test]
    fn test_file_generation_seen_accepts_newer_seq() {
        let store = make_store();
        store.file_generation_seen(1, "src/main.rs", "session-1", 1, 5).unwrap();

        // newer: epoch=1, seq=10
        let seen = store
            .file_generation_seen(1, "src/main.rs", "session-1", 1, 10)
            .unwrap();
        assert!(seen);

        let row = store.get_file_generation(1, "src/main.rs").unwrap().unwrap();
        assert_eq!(row.latest_seq, 10);
        assert_eq!(row.latest_seen_generation, "1:10");
    }

    // ---- file_generation_committed 测试 ----

    #[test]
    fn test_file_generation_committed_updates_on_matching_seen() {
        let store = make_store();
        // seen: epoch=1, seq=5
        store.file_generation_seen(1, "src/main.rs", "session-1", 1, 5).unwrap();

        // committed: epoch=1, seq=5（与 seen 一致）
        let committed = store.file_generation_committed(1, "src/main.rs", 1, 5).unwrap();
        assert!(committed);

        let row = store.get_file_generation(1, "src/main.rs").unwrap().unwrap();
        assert_eq!(row.latest_committed_generation, "1:5");
    }

    #[test]
    fn test_file_generation_committed_rejects_stale() {
        let store = make_store();
        // seen: epoch=1, seq=5
        store.file_generation_seen(1, "src/main.rs", "session-1", 1, 5).unwrap();

        // 其他 handler 已覆盖 seen（变为 epoch=1, seq=10）
        store.file_generation_seen(1, "src/main.rs", "session-1", 1, 10).unwrap();

        // 现在 committed: epoch=1, seq=5 应该被拒绝（latest_seen_generation 已变为 "1:10"）
        let committed = store.file_generation_committed(1, "src/main.rs", 1, 5).unwrap();
        assert!(!committed, "stale manifest commit 应该被条件 UPDATE 阻止");

        let row = store.get_file_generation(1, "src/main.rs").unwrap().unwrap();
        assert_eq!(row.latest_committed_generation, ""); // 仍然为空
    }

    // ---- parse_generation 辅助函数测试 ----

    #[test]
    fn test_parse_generation_valid() {
        assert_eq!(parse_generation("1:5"), Some((1, 5)));
        assert_eq!(parse_generation("100:200"), Some((100, 200)));
    }

    #[test]
    fn test_parse_generation_invalid() {
        assert_eq!(parse_generation("invalid"), None);
        assert_eq!(parse_generation("1:not_a_number"), None);
        assert_eq!(parse_generation(""), None);
    }
}
