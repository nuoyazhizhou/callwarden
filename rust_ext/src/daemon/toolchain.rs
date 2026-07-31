//! ToolchainStore —— 工具链专属库（Layer 2 of 三层存储）。
//!
//! 对应 Python：
//! - `db/db_toolchain.py`（TOOLCHAIN_SCHEMA_DDL + 4 张表 + 32 个函数）
//!
//! 设计参考：`docs/design/enterprise-architecture-evolution.md` §"三层存储设计"：
//! - Layer 1 Global CAS（`cas.rs`）—— 单文件粒度符号内容
//! - Layer 2 Toolchain DB（本模块）—— 工具链指纹 + build_context + resolved_edges
//! - Layer 3 Thin Workspace（`workspace.rs`）—— workspace 注册表 + manifest 引用
//!
//! ## 独立 DB 设计
//! 与 Layer 1 CasStore / Layer 3 WorkspaceRegistry 对称，ToolchainStore 管理
//! 独立的 `toolchain.db` 文件。daemon 启动时由 `cw_daemon` 显式 open，并通过
//! `ATTACH DATABASE` 挂载到 workspace 连接（如有需要），实现跨 workspace 共享。
//!
//! ## 跨平台
//! rusqlite + sha2 + serde_json，Windows 可完整验收。
//!
//! ## 不变量
//! - T1: 同一 fingerprint 的 toolchain 只能存在一条（UNIQUE 约束）
//! - T2: workspace 内 (workspace_id, build_context_hash, caller, callee, call_line) 唯一
//! - T3: workspace 内最多一个 active build_context（由 set_active_build_context 维护）
//! - T4: resolved_edges 按 (workspace_id, build_context_hash) 隔离

use std::sync::Mutex;

use rusqlite::{params, Connection, OpenFlags, OptionalExtension, Row, TransactionBehavior};
use serde_json::{Map, Value};

// ============================================
// Schema DDL（与 Python db_toolchain.py:TOOLCHAIN_SCHEMA_DDL 一致）
// ============================================

pub const TOOLCHAIN_SCHEMA_DDL: &str = r#"
CREATE TABLE IF NOT EXISTS toolchains (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    compiler_path TEXT NOT NULL,
    compiler_type TEXT NOT NULL,
    version TEXT DEFAULT '',
    target_triple TEXT DEFAULT '',
    sysroot TEXT DEFAULT '',
    include_dirs TEXT DEFAULT '[]',
    predefined_macros TEXT DEFAULT '{}',
    fingerprint TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    description TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS workspace_toolchains (
    workspace_id INTEGER NOT NULL,
    toolchain_id INTEGER NOT NULL,
    build_context_hash TEXT DEFAULT '',
    PRIMARY KEY (workspace_id, toolchain_id, build_context_hash)
);

CREATE TABLE IF NOT EXISTS workspace_build_contexts (
    workspace_id INTEGER NOT NULL,
    build_context_hash TEXT NOT NULL,
    name TEXT DEFAULT '',
    compile_flags TEXT DEFAULT '[]',
    defines TEXT DEFAULT '{}',
    include_paths TEXT DEFAULT '[]',
    is_active INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, build_context_hash)
);

CREATE TABLE IF NOT EXISTS resolved_edges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    build_context_hash TEXT NOT NULL,
    caller_symbol_id INTEGER NOT NULL,
    callee_symbol_id INTEGER NOT NULL,
    callee_name TEXT NOT NULL,
    callee_file TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    resolution_method TEXT DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE(workspace_id, build_context_hash, caller_symbol_id, callee_symbol_id, call_line)
);

CREATE INDEX IF NOT EXISTS idx_toolchain_fingerprint ON toolchains(fingerprint);
CREATE INDEX IF NOT EXISTS idx_workspace_toolchains_ws ON workspace_toolchains(workspace_id);
CREATE INDEX IF NOT EXISTS idx_workspace_toolchains_ctx ON workspace_toolchains(build_context_hash);
CREATE INDEX IF NOT EXISTS idx_build_contexts_ws ON workspace_build_contexts(workspace_id);
CREATE INDEX IF NOT EXISTS idx_build_contexts_active ON workspace_build_contexts(workspace_id, is_active);
CREATE INDEX IF NOT EXISTS idx_resolved_edges_ws_ctx ON resolved_edges(workspace_id, build_context_hash);
CREATE INDEX IF NOT EXISTS idx_resolved_edges_caller ON resolved_edges(caller_symbol_id);
CREATE INDEX IF NOT EXISTS idx_resolved_edges_callee ON resolved_edges(callee_symbol_id);
"#;

fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// hex 编码（避免引入 hex crate）
fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

// ============================================
// ToolchainStore：封装 SQLite 连接 + 提供 toolchain 层操作
// ============================================

/// ToolchainStore 管理独立 `toolchain.db`，与 CasStore / WorkspaceRegistry 对称。
///
/// 设计：
/// - `open(db_path)`：打开/创建 toolchain.db，初始化 4 张表 + 7 个索引
/// - `open_in_memory()`：测试用
/// - schema 与 Python `db_toolchain.py:TOOLCHAIN_SCHEMA_DDL` 完全一致
pub struct ToolchainStore {
    conn: Mutex<Connection>,
    /// toolchain.db 文件路径（用于 ATTACH DATABASE 或备份）
    pub db_path: String,
}

impl ToolchainStore {
    /// 打开指定路径的 toolchain DB（不存在则创建并初始化 schema）
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
            db_path: db_path.to_string(),
        })
    }

    /// 以只读方式打开已存在的数据库。
    ///
    /// CLI 的 list/show/edges 路径使用此入口，避免只读命令因初始化 schema
    /// 获得写锁。数据库尚未初始化时由调用方把 `no such table` 转为明确提示。
    pub fn open_read_only(db_path: &str) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_with_flags(
            db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )?;
        conn.busy_timeout(std::time::Duration::from_secs(5))?;
        Ok(Self {
            conn: Mutex::new(conn),
            db_path: db_path.to_string(),
        })
    }

    /// 内存数据库（测试用）
    pub fn open_in_memory() -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        Self::init_conn(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
            db_path: ":memory:".to_string(),
        })
    }

    fn init_conn(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch("PRAGMA busy_timeout=5000;")?;
        conn.execute_batch("PRAGMA journal_mode=WAL;")?;
        conn.execute_batch(TOOLCHAIN_SCHEMA_DDL)?;
        Ok(())
    }

    // ============================================
    // Toolchain CRUD
    // ============================================

    /// 注册工具链（INSERT OR REPLACE on name，相同 fingerprint 视为同一工具链）
    ///
    /// 对应 Python `db_toolchain.py:register_toolchain`：
    /// - name 必须唯一（UNIQUE 约束）
    /// - fingerprint 必须唯一（UNIQUE 约束）—— 相同 fingerprint 的 toolchain 视为同一
    /// - 如果 fingerprint 已存在，直接返回已注册的 toolchain（不重复插入）
    /// - 如果 name 已存在但 fingerprint 不同，INSERT OR REPLACE 会覆盖（注意：此处与
    ///   Python 行为略有差异，Python 是 SELECT 后决定 insert 还是 return existing；
    ///   Rust 版本更严格：fingerprint 已存在则返回现有，否则插入新行）
    pub fn register_toolchain(
        &self,
        name: &str,
        compiler_path: &str,
        compiler_type: &str,
        version: &str,
        target_triple: &str,
        sysroot: &str,
        include_dirs: &[String],
        predefined_macros: &[(String, String)],
        fingerprint: &str,
        description: &str,
    ) -> Result<Value, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();

        // 先查 fingerprint 是否已存在
        let existing: Option<(i64,)> = conn
            .query_row(
                "SELECT id FROM toolchains WHERE fingerprint = ?1",
                params![fingerprint],
                |row| Ok((row.get(0)?,)),
            )
            .optional()?;
        if let Some((id,)) = existing {
            // fingerprint 已存在，返回现有记录
            return self.get_toolchain_by_id(&conn, id);
        }

        // 插入新记录
        let now = now_ts();
        let include_dirs_json = serde_json::to_string(include_dirs).unwrap_or_else(|_| "[]".into());
        let macros_json = serde_json::to_string(
            &predefined_macros
                .iter()
                .map(|(k, v)| (k.clone(), v.clone()))
                .collect::<std::collections::HashMap<_, _>>(),
        )
        .unwrap_or_else(|_| "{}".into());

        conn.execute(
            "INSERT INTO toolchains
             (name, compiler_path, compiler_type, version, target_triple, sysroot,
              include_dirs, predefined_macros, fingerprint, created_at, updated_at, description)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
            params![
                name,
                compiler_path,
                compiler_type,
                version,
                target_triple,
                sysroot,
                include_dirs_json,
                macros_json,
                fingerprint,
                now,
                now,
                description,
            ],
        )?;
        let id = conn.last_insert_rowid();
        self.get_toolchain_by_id(&conn, id)
    }

    /// 查询 toolchain by id
    fn get_toolchain_by_id(&self, conn: &Connection, id: i64) -> Result<Value, rusqlite::Error> {
        let mut stmt = conn.prepare(
            "SELECT id, name, compiler_path, compiler_type, version, target_triple,
                    sysroot, include_dirs, predefined_macros, fingerprint,
                    created_at, updated_at, description
             FROM toolchains WHERE id = ?1",
        )?;
        let mut rows = stmt.query(params![id])?;
        if let Some(row) = rows.next()? {
            toolchain_row_to_json(row)
        } else {
            Err(rusqlite::Error::QueryReturnedNoRows)
        }
    }

    /// 查询 toolchain by name 或 id（name 优先）
    pub fn get_toolchain(&self, name_or_id: &str) -> Result<Option<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        // 先尝试按 name 查
        let mut stmt = conn.prepare(
            "SELECT id, name, compiler_path, compiler_type, version, target_triple,
                    sysroot, include_dirs, predefined_macros, fingerprint,
                    created_at, updated_at, description
             FROM toolchains WHERE name = ?1",
        )?;
        let mut rows = stmt.query(params![name_or_id])?;
        if let Some(row) = rows.next()? {
            return Ok(Some(toolchain_row_to_json(row)?));
        }
        drop(rows);
        drop(stmt);
        // 再尝试按 id 查
        if let Ok(id) = name_or_id.parse::<i64>() {
            let mut stmt = conn.prepare(
                "SELECT id, name, compiler_path, compiler_type, version, target_triple,
                        sysroot, include_dirs, predefined_macros, fingerprint,
                        created_at, updated_at, description
                 FROM toolchains WHERE id = ?1",
            )?;
            let mut rows = stmt.query(params![id])?;
            if let Some(row) = rows.next()? {
                return Ok(Some(toolchain_row_to_json(row)?));
            }
        }
        Ok(None)
    }

    /// 列出所有 toolchain
    pub fn list_toolchains(&self) -> Result<Vec<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT id, name, compiler_path, compiler_type, version, target_triple,
                    sysroot, include_dirs, predefined_macros, fingerprint,
                    created_at, updated_at, description
             FROM toolchains ORDER BY id ASC",
        )?;
        let mut result = Vec::new();
        for row in stmt.query_map([], toolchain_row_to_json)? {
            result.push(row?);
        }
        Ok(result)
    }

    /// 删除 toolchain by name 或 id
    ///
    /// 注意：由于 workspace_toolchains 没有 FK 约束（Rust schema 简化），
    /// 调用方应先确认无 workspace 绑定，或显式删除绑定。
    pub fn delete_toolchain(&self, name_or_id: &str) -> Result<u64, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        // 先找 id
        let id: Option<i64> = {
            let mut stmt = conn.prepare("SELECT id FROM toolchains WHERE name = ?1")?;
            let mut rows = stmt.query(params![name_or_id])?;
            if let Some(row) = rows.next()? {
                Some(row.get(0)?)
            } else if let Ok(parsed) = name_or_id.parse::<i64>() {
                let mut stmt2 = conn.prepare("SELECT id FROM toolchains WHERE id = ?1")?;
                let mut rows2 = stmt2.query(params![parsed])?;
                if let Some(row) = rows2.next()? {
                    Some(row.get(0)?)
                } else {
                    None
                }
            } else {
                None
            }
        };
        if let Some(id) = id {
            // 先删除绑定
            conn.execute(
                "DELETE FROM workspace_toolchains WHERE toolchain_id = ?1",
                params![id],
            )?;
            let affected = conn.execute("DELETE FROM toolchains WHERE id = ?1", params![id])?;
            Ok(affected as u64)
        } else {
            Ok(0)
        }
    }

    // ============================================
    // Workspace ↔ Toolchain 绑定
    // ============================================

    /// 绑定 toolchain 到 workspace（对应 Python `bind_toolchain_to_workspace`）
    pub fn bind_toolchain_to_workspace(
        &self,
        workspace_id: i64,
        toolchain_id: i64,
        build_context_hash: &str,
    ) -> Result<(), rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO workspace_toolchains
             (workspace_id, toolchain_id, build_context_hash)
             VALUES (?1, ?2, ?3)",
            params![workspace_id, toolchain_id, build_context_hash],
        )?;
        Ok(())
    }

    /// 查询 workspace 绑定的 toolchain 列表
    pub fn get_workspace_toolchains(
        &self,
        workspace_id: i64,
        build_context_hash: Option<&str>,
    ) -> Result<Vec<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = if build_context_hash.is_some() {
            conn.prepare(
                "SELECT t.id, t.name, t.compiler_path, t.compiler_type, t.version,
                        t.target_triple, t.sysroot, t.include_dirs, t.predefined_macros,
                        t.fingerprint, t.created_at, t.updated_at, t.description
                 FROM toolchains t
                 INNER JOIN workspace_toolchains wt
                   ON t.id = wt.toolchain_id
                 WHERE wt.workspace_id = ?1 AND wt.build_context_hash = ?2
                 ORDER BY t.id ASC",
            )?
        } else {
            conn.prepare(
                "SELECT t.id, t.name, t.compiler_path, t.compiler_type, t.version,
                        t.target_triple, t.sysroot, t.include_dirs, t.predefined_macros,
                        t.fingerprint, t.created_at, t.updated_at, t.description
                 FROM toolchains t
                 INNER JOIN workspace_toolchains wt
                   ON t.id = wt.toolchain_id
                 WHERE wt.workspace_id = ?1
                 ORDER BY t.id ASC",
            )?
        };
        let mut result = Vec::new();
        if let Some(bch) = build_context_hash {
            for row in stmt.query_map(params![workspace_id, bch], toolchain_row_to_json)? {
                result.push(row?);
            }
        } else {
            for row in stmt.query_map(params![workspace_id], toolchain_row_to_json)? {
                result.push(row?);
            }
        }
        Ok(result)
    }

    // ============================================
    // Build Context CRUD
    // ============================================

    /// 注册 build context（对应 Python `register_build_context`）
    ///
    /// 若 `set_active=true`，先清除同 workspace 其他 active context，再插入并设为 active。
    pub fn register_build_context(
        &self,
        workspace_id: i64,
        name: &str,
        compile_flags: &[String],
        defines: &[(String, String)],
        include_paths: &[String],
        set_active: bool,
    ) -> Result<Value, rusqlite::Error> {
        let bch = compute_build_context_hash(compile_flags, defines, include_paths);
        let mut conn = self.conn.lock().unwrap();
        let now = now_ts();
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;

        if set_active {
            tx.execute(
                "UPDATE workspace_build_contexts SET is_active = 0 WHERE workspace_id = ?1",
                params![workspace_id],
            )?;
        }

        let flags_json = serde_json::to_string(compile_flags).unwrap_or_else(|_| "[]".into());
        let defines_json = serde_json::to_string(
            &defines
                .iter()
                .map(|(key, value)| (key.clone(), Value::String(value.clone())))
                .collect::<Map<_, _>>(),
        )
        .unwrap_or_else(|_| "{}".into());
        let includes_json = serde_json::to_string(include_paths).unwrap_or_else(|_| "[]".into());

        tx.execute(
            "INSERT OR REPLACE INTO workspace_build_contexts
             (workspace_id, build_context_hash, name, compile_flags, defines,
              include_paths, is_active, created_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8)",
            params![
                workspace_id,
                bch,
                name,
                flags_json,
                defines_json,
                includes_json,
                if set_active { 1 } else { 0 },
                now,
            ],
        )?;
        tx.commit()?;

        Ok(build_context_to_json(
            workspace_id,
            &bch,
            name,
            compile_flags,
            defines,
            include_paths,
            set_active,
            now,
        ))
    }

    /// 查询 build context（支持短 hash 前缀匹配）
    pub fn get_build_context(
        &self,
        workspace_id: i64,
        build_context_hash: &str,
    ) -> Result<Option<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        // 1. 精确匹配
        let mut stmt = conn.prepare(
            "SELECT workspace_id, build_context_hash, name, compile_flags, defines,
                    include_paths, is_active, created_at
             FROM workspace_build_contexts
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
        )?;
        let mut rows = stmt.query(params![workspace_id, build_context_hash])?;
        if let Some(row) = rows.next()? {
            return Ok(Some(build_context_row_to_json(row)?));
        }
        drop(rows);
        drop(stmt);
        // 2. 前缀匹配（短 hash → 完整 hash）
        let pattern = format!("{}%", build_context_hash);
        let mut stmt = conn.prepare(
            "SELECT workspace_id, build_context_hash, name, compile_flags, defines,
                    include_paths, is_active, created_at
             FROM workspace_build_contexts
             WHERE workspace_id = ?1 AND build_context_hash LIKE ?2",
        )?;
        let rows: Vec<Value> = stmt
            .query_map(params![workspace_id, pattern], build_context_row_to_json)?
            .filter_map(|r| r.ok())
            .collect();
        if rows.len() == 1 {
            Ok(Some(rows[0].clone()))
        } else {
            Ok(None)
        }
    }

    /// 列出 workspace 的所有 build context
    pub fn list_build_contexts(&self, workspace_id: i64) -> Result<Vec<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT workspace_id, build_context_hash, name, compile_flags, defines,
                    include_paths, is_active, created_at
             FROM workspace_build_contexts
             WHERE workspace_id = ?1
             ORDER BY created_at ASC",
        )?;
        let mut result = Vec::new();
        for row in stmt.query_map(params![workspace_id], build_context_row_to_json)? {
            result.push(row?);
        }
        Ok(result)
    }

    /// 设置 active build context
    pub fn set_active_build_context(
        &self,
        workspace_id: i64,
        build_context_hash: &str,
    ) -> Result<bool, rusqlite::Error> {
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        // 校验存在
        let exists: bool = tx
            .query_row(
                "SELECT 1 FROM workspace_build_contexts
                 WHERE workspace_id = ?1 AND build_context_hash = ?2",
                params![workspace_id, build_context_hash],
                |_| Ok(true),
            )
            .optional()?
            .is_some();
        if !exists {
            tx.rollback()?;
            return Ok(false);
        }
        // 清除其他 active
        tx.execute(
            "UPDATE workspace_build_contexts SET is_active = 0 WHERE workspace_id = ?1",
            params![workspace_id],
        )?;
        // 设置目标为 active
        tx.execute(
            "UPDATE workspace_build_contexts SET is_active = 1
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
            params![workspace_id, build_context_hash],
        )?;
        tx.commit()?;
        Ok(true)
    }

    /// 获取当前 active build context
    pub fn get_active_build_context(
        &self,
        workspace_id: i64,
    ) -> Result<Option<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT workspace_id, build_context_hash, name, compile_flags, defines,
                    include_paths, is_active, created_at
             FROM workspace_build_contexts
             WHERE workspace_id = ?1 AND is_active = 1",
        )?;
        let mut rows = stmt.query(params![workspace_id])?;
        if let Some(row) = rows.next()? {
            Ok(Some(build_context_row_to_json(row)?))
        } else {
            Ok(None)
        }
    }

    /// 删除 build context（同时删除其 resolved_edges）
    pub fn delete_build_context(
        &self,
        workspace_id: i64,
        build_context_hash: &str,
    ) -> Result<u64, rusqlite::Error> {
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        // 先删 resolved_edges
        tx.execute(
            "DELETE FROM resolved_edges
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
            params![workspace_id, build_context_hash],
        )?;
        // 删 workspace_toolchains 绑定
        tx.execute(
            "DELETE FROM workspace_toolchains
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
            params![workspace_id, build_context_hash],
        )?;
        // 删 context 自身
        let affected = tx.execute(
            "DELETE FROM workspace_build_contexts
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
            params![workspace_id, build_context_hash],
        )?;
        tx.commit()?;
        Ok(affected as u64)
    }

    // ============================================
    // Resolved Edges CRUD
    // ============================================

    /// 批量存储 resolved edges（INSERT OR IGNORE，UNIQUE 约束去重）
    pub fn store_resolved_edges(
        &self,
        workspace_id: i64,
        build_context_hash: &str,
        edges: &[ResolvedEdgeInput],
    ) -> Result<usize, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let now = now_ts();
        let mut count = 0usize;
        for edge in edges {
            let affected = conn.execute(
                "INSERT OR IGNORE INTO resolved_edges
                 (workspace_id, build_context_hash, caller_symbol_id, callee_symbol_id,
                  callee_name, callee_file, call_line, resolution_method, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                params![
                    workspace_id,
                    build_context_hash,
                    edge.caller_symbol_id,
                    edge.callee_symbol_id,
                    edge.callee_name,
                    edge.callee_file,
                    edge.call_line,
                    edge.resolution_method,
                    now,
                ],
            )?;
            count += affected;
        }
        Ok(count)
    }

    /// 在单个 IMMEDIATE 事务内替换一个 build context 的 resolved edge 缓存。
    ///
    /// 这用于 `cw build-context resolve`，保证进程崩溃或任一 INSERT 失败时
    /// 旧缓存仍完整可用，不会暴露半构建状态。
    pub fn replace_resolved_edges(
        &self,
        workspace_id: i64,
        build_context_hash: &str,
        edges: &[ResolvedEdgeInput],
    ) -> Result<(u64, usize), rusqlite::Error> {
        let mut conn = self.conn.lock().unwrap();
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let deleted = tx.execute(
            "DELETE FROM resolved_edges
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
            params![workspace_id, build_context_hash],
        )? as u64;
        let now = now_ts();
        let mut inserted = 0usize;
        for edge in edges {
            inserted += tx.execute(
                "INSERT OR IGNORE INTO resolved_edges
                 (workspace_id, build_context_hash, caller_symbol_id, callee_symbol_id,
                  callee_name, callee_file, call_line, resolution_method, created_at)
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)",
                params![
                    workspace_id,
                    build_context_hash,
                    edge.caller_symbol_id,
                    edge.callee_symbol_id,
                    edge.callee_name,
                    edge.callee_file,
                    edge.call_line,
                    edge.resolution_method,
                    now,
                ],
            )?;
        }
        tx.commit()?;
        Ok((deleted, inserted))
    }

    /// 查询 resolved edges（可选按 caller_symbol_id 过滤）
    pub fn get_resolved_edges(
        &self,
        workspace_id: i64,
        build_context_hash: &str,
        caller_symbol_id: Option<i64>,
        limit: Option<usize>,
    ) -> Result<Vec<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let sql = if caller_symbol_id.is_some() {
            "SELECT id, workspace_id, build_context_hash, caller_symbol_id, callee_symbol_id,
                    callee_name, callee_file, call_line, resolution_method, created_at
             FROM resolved_edges
             WHERE workspace_id = ?1 AND build_context_hash = ?2 AND caller_symbol_id = ?3
             ORDER BY call_line"
        } else {
            "SELECT id, workspace_id, build_context_hash, caller_symbol_id, callee_symbol_id,
                    callee_name, callee_file, call_line, resolution_method, created_at
             FROM resolved_edges
             WHERE workspace_id = ?1 AND build_context_hash = ?2
             ORDER BY caller_symbol_id, call_line"
        };
        let mut stmt = conn.prepare(sql)?;
        let iter: Box<dyn Iterator<Item = rusqlite::Result<Value>>> =
            if let Some(csid) = caller_symbol_id {
                let rows = stmt.query_map(
                    params![workspace_id, build_context_hash, csid],
                    resolved_edge_row_to_json,
                )?;
                Box::new(rows)
            } else {
                let rows = stmt.query_map(
                    params![workspace_id, build_context_hash],
                    resolved_edge_row_to_json,
                )?;
                Box::new(rows)
            };
        let mut result = Vec::new();
        for (i, row) in iter.enumerate() {
            if let Some(limit) = limit {
                if i >= limit {
                    break;
                }
            }
            result.push(row?);
        }
        Ok(result)
    }

    /// 删除 resolved edges
    pub fn delete_resolved_edges(
        &self,
        workspace_id: i64,
        build_context_hash: Option<&str>,
    ) -> Result<u64, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let affected = if let Some(bch) = build_context_hash {
            conn.execute(
                "DELETE FROM resolved_edges
                 WHERE workspace_id = ?1 AND build_context_hash = ?2",
                params![workspace_id, bch],
            )?
        } else {
            conn.execute(
                "DELETE FROM resolved_edges WHERE workspace_id = ?1",
                params![workspace_id],
            )?
        };
        Ok(affected as u64)
    }

    /// 统计 resolved edges 数量
    pub fn count_resolved_edges(
        &self,
        workspace_id: i64,
        build_context_hash: &str,
    ) -> Result<i64, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let count: i64 = conn.query_row(
            "SELECT COUNT(*) FROM resolved_edges
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
            params![workspace_id, build_context_hash],
            |row| row.get(0),
        )?;
        Ok(count)
    }

    /// 列出 workspace 下各 build context 的 edge 统计
    pub fn list_build_context_edges(
        &self,
        workspace_id: i64,
    ) -> Result<Vec<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT build_context_hash, COUNT(*) as edge_count
             FROM resolved_edges
             WHERE workspace_id = ?1
             GROUP BY build_context_hash
             ORDER BY build_context_hash",
        )?;
        let mut result = Vec::new();
        for row in stmt.query_map(params![workspace_id], |row| {
            let bch: String = row.get(0)?;
            let count: i64 = row.get(1)?;
            let mut m = Map::new();
            m.insert("build_context_hash".to_string(), Value::String(bch));
            m.insert("edge_count".to_string(), Value::Number(count.into()));
            Ok(Value::Object(m))
        })? {
            result.push(row?);
        }
        Ok(result)
    }

    // ============================================
    // 高级查询：resolve_toolchain（降级策略）
    // ============================================

    /// 解析 workspace + build context 对应的 toolchain
    ///
    /// 降级策略（与 Python `resolve_toolchain` 一致）：
    /// 1. 精确匹配 build_context_hash
    /// 2. active build context
    /// 3. 默认 context（空 hash）
    /// 4. None
    pub fn resolve_toolchain(
        &self,
        workspace_id: i64,
        build_context_hash: Option<&str>,
    ) -> Result<Option<Value>, rusqlite::Error> {
        // 1. 精确匹配
        if let Some(bch) = build_context_hash {
            let tcs = self.get_workspace_toolchains(workspace_id, Some(bch))?;
            if let Some(tc) = tcs.into_iter().next() {
                return Ok(Some(tc));
            }
        }
        // 2. active build context
        if let Some(active) = self.get_active_build_context(workspace_id)? {
            let active_bch = active["build_context_hash"]
                .as_str()
                .unwrap_or("")
                .to_string();
            let tcs = self.get_workspace_toolchains(workspace_id, Some(&active_bch))?;
            if let Some(tc) = tcs.into_iter().next() {
                return Ok(Some(tc));
            }
        }
        // 3. 默认 context（空 hash）
        let tcs = self.get_workspace_toolchains(workspace_id, Some(""))?;
        if let Some(tc) = tcs.into_iter().next() {
            return Ok(Some(tc));
        }
        // 4. 无绑定
        Ok(None)
    }

    // ============================================
    // ATTACH DATABASE 支持
    // ============================================

    /// 将本 toolchain.db ATTACH 到目标连接（用于跨 workspace 共享查询）
    ///
    /// 对应 Python `db_toolchain.py:attach_toolchain_db`（待实现）：
    /// 在目标连接上执行 `ATTACH DATABASE '<toolchain.db>' AS toolchain`，
    /// 调用方可通过 `toolchain.toolchains` 等全限定表名访问。
    ///
    /// 注意：ATTACH 后，本 ToolchainStore 的 conn 仍持有写锁，调用方应只用 ATTACH 做只读查询。
    pub fn attach_to(
        &self,
        target_conn: &Connection,
        schema_name: &str,
    ) -> Result<(), rusqlite::Error> {
        target_conn.execute_batch(&format!(
            "ATTACH DATABASE '{}' AS {};",
            self.db_path.replace('\'', "''"),
            schema_name.replace('"', "\"\"")
        ))?;
        Ok(())
    }

    /// 获取内部连接（仅用于 daemon 内部直接访问，不推荐外部使用）
    pub fn conn(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().unwrap()
    }
}

// ============================================
// 输入/输出结构
// ============================================

/// ResolvedEdge 输入（对应 Python dict）
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedEdgeInput {
    pub caller_symbol_id: i64,
    pub callee_symbol_id: i64,
    pub callee_name: String,
    pub callee_file: String,
    pub call_line: i64,
    pub resolution_method: String,
}

// ============================================
// Hash 计算
// ============================================

/// 计算 build context 哈希（与 Python `compute_build_context_hash` 一致）
///
/// 格式：`buildctx_v1|<flags_str>|<defines_str>|<includes_str>`
/// 其中 flags_str/defines_str/includes_str 均为 `item;` 串联（排序后）
pub fn compute_build_context_hash(
    compile_flags: &[String],
    defines: &[(String, String)],
    include_paths: &[String],
) -> String {
    use sha2::{Digest, Sha256};

    let mut sorted_flags: Vec<&String> = compile_flags.iter().collect();
    sorted_flags.sort();
    let mut sorted_defines = defines.to_vec();
    sorted_defines.sort();
    let mut sorted_includes: Vec<&String> = include_paths.iter().collect();
    sorted_includes.sort();

    let norm_includes: Vec<String> = sorted_includes
        .iter()
        .map(|p| p.replace('\\', "/"))
        .collect();

    let flags_str: String = sorted_flags.iter().map(|f| format!("{};", f)).collect();
    let defines_str: String = sorted_defines
        .iter()
        .map(|(k, v)| format!("{}={};", k, v))
        .collect();
    let includes_str: String = norm_includes.iter().map(|p| format!("{};", p)).collect();

    let raw = format!("buildctx_v1|{}|{}|{}", flags_str, defines_str, includes_str);
    let mut hasher = Sha256::new();
    hasher.update(raw.as_bytes());
    hex_encode(&hasher.finalize())
}

// ============================================
// Row → JSON 转换
// ============================================

fn toolchain_row_to_json(row: &Row<'_>) -> rusqlite::Result<Value> {
    let id: i64 = row.get(0)?;
    let name: String = row.get(1)?;
    let compiler_path: String = row.get(2)?;
    let compiler_type: String = row.get(3)?;
    let version: String = row.get(4)?;
    let target_triple: String = row.get(5)?;
    let sysroot: String = row.get(6)?;
    let include_dirs_str: String = row.get(7)?;
    let predefined_macros_str: String = row.get(8)?;
    let fingerprint: String = row.get(9)?;
    let created_at: f64 = row.get(10)?;
    let updated_at: f64 = row.get(11)?;
    let description: String = row.get(12)?;

    let include_dirs: Value =
        serde_json::from_str(&include_dirs_str).unwrap_or(Value::Array(vec![]));
    let predefined_macros: Value =
        serde_json::from_str(&predefined_macros_str).unwrap_or(Value::Object(Map::new()));

    let mut m = Map::new();
    m.insert("id".to_string(), Value::Number(id.into()));
    m.insert("name".to_string(), Value::String(name));
    m.insert("compiler_path".to_string(), Value::String(compiler_path));
    m.insert("compiler_type".to_string(), Value::String(compiler_type));
    m.insert("version".to_string(), Value::String(version));
    m.insert("target_triple".to_string(), Value::String(target_triple));
    m.insert("sysroot".to_string(), Value::String(sysroot));
    m.insert("include_dirs".to_string(), include_dirs);
    m.insert("predefined_macros".to_string(), predefined_macros);
    m.insert("fingerprint".to_string(), Value::String(fingerprint));
    m.insert(
        "created_at".to_string(),
        Value::Number(serde_json::Number::from_f64(created_at).unwrap_or_else(|| 0.into())),
    );
    m.insert(
        "updated_at".to_string(),
        Value::Number(serde_json::Number::from_f64(updated_at).unwrap_or_else(|| 0.into())),
    );
    m.insert("description".to_string(), Value::String(description));
    Ok(Value::Object(m))
}

fn build_context_row_to_json(row: &Row<'_>) -> rusqlite::Result<Value> {
    let workspace_id: i64 = row.get(0)?;
    let build_context_hash: String = row.get(1)?;
    let name: String = row.get(2)?;
    let compile_flags_str: String = row.get(3)?;
    let defines_str: String = row.get(4)?;
    let include_paths_str: String = row.get(5)?;
    let is_active: i64 = row.get(6)?;
    let created_at: f64 = row.get(7)?;

    let compile_flags: Value =
        serde_json::from_str(&compile_flags_str).unwrap_or(Value::Array(vec![]));
    let defines: Value = serde_json::from_str(&defines_str).unwrap_or(Value::Object(Map::new()));
    let include_paths: Value =
        serde_json::from_str(&include_paths_str).unwrap_or(Value::Array(vec![]));

    let mut m = Map::new();
    m.insert(
        "workspace_id".to_string(),
        Value::Number(workspace_id.into()),
    );
    m.insert(
        "build_context_hash".to_string(),
        Value::String(build_context_hash),
    );
    m.insert("name".to_string(), Value::String(name));
    m.insert("compile_flags".to_string(), compile_flags);
    m.insert("defines".to_string(), defines);
    m.insert("include_paths".to_string(), include_paths);
    m.insert("is_active".to_string(), Value::Bool(is_active != 0));
    m.insert(
        "created_at".to_string(),
        Value::Number(serde_json::Number::from_f64(created_at).unwrap_or_else(|| 0.into())),
    );
    Ok(Value::Object(m))
}

fn build_context_to_json(
    workspace_id: i64,
    build_context_hash: &str,
    name: &str,
    compile_flags: &[String],
    defines: &[(String, String)],
    include_paths: &[String],
    is_active: bool,
    created_at: f64,
) -> Value {
    let compile_flags: Value = serde_json::to_string(compile_flags)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(Value::Array(vec![]));
    let defines = Value::Object(
        defines
            .iter()
            .map(|(key, value)| (key.clone(), Value::String(value.clone())))
            .collect(),
    );
    let include_paths: Value = serde_json::to_string(include_paths)
        .ok()
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or(Value::Array(vec![]));

    let mut m = Map::new();
    m.insert(
        "workspace_id".to_string(),
        Value::Number(workspace_id.into()),
    );
    m.insert(
        "build_context_hash".to_string(),
        Value::String(build_context_hash.to_string()),
    );
    m.insert("name".to_string(), Value::String(name.to_string()));
    m.insert("compile_flags".to_string(), compile_flags);
    m.insert("defines".to_string(), defines);
    m.insert("include_paths".to_string(), include_paths);
    m.insert("is_active".to_string(), Value::Bool(is_active));
    m.insert(
        "created_at".to_string(),
        Value::Number(serde_json::Number::from_f64(created_at).unwrap_or_else(|| 0.into())),
    );
    Value::Object(m)
}

fn resolved_edge_row_to_json(row: &Row<'_>) -> rusqlite::Result<Value> {
    let id: i64 = row.get(0)?;
    let workspace_id: i64 = row.get(1)?;
    let build_context_hash: String = row.get(2)?;
    let caller_symbol_id: i64 = row.get(3)?;
    let callee_symbol_id: i64 = row.get(4)?;
    let callee_name: String = row.get(5)?;
    let callee_file: String = row.get(6)?;
    let call_line: i64 = row.get(7)?;
    let resolution_method: String = row.get(8)?;
    let created_at: f64 = row.get(9)?;

    let mut m = Map::new();
    m.insert("id".to_string(), Value::Number(id.into()));
    m.insert(
        "workspace_id".to_string(),
        Value::Number(workspace_id.into()),
    );
    m.insert(
        "build_context_hash".to_string(),
        Value::String(build_context_hash),
    );
    m.insert(
        "caller_symbol_id".to_string(),
        Value::Number(caller_symbol_id.into()),
    );
    m.insert(
        "callee_symbol_id".to_string(),
        Value::Number(callee_symbol_id.into()),
    );
    m.insert("callee_name".to_string(), Value::String(callee_name));
    m.insert("callee_file".to_string(), Value::String(callee_file));
    m.insert("call_line".to_string(), Value::Number(call_line.into()));
    m.insert(
        "resolution_method".to_string(),
        Value::String(resolution_method),
    );
    m.insert(
        "created_at".to_string(),
        Value::Number(serde_json::Number::from_f64(created_at).unwrap_or_else(|| 0.into())),
    );
    Ok(Value::Object(m))
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_open_in_memory_initializes_schema() {
        // open_in_memory 应初始化 4 张表 + 7 个索引
        let store = ToolchainStore::open_in_memory().unwrap();
        let conn = store.conn.lock().unwrap();
        // 验证 4 张表存在
        for table in [
            "toolchains",
            "workspace_toolchains",
            "workspace_build_contexts",
            "resolved_edges",
        ] {
            let count: i64 = conn
                .query_row(
                    &format!(
                        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='{}'",
                        table
                    ),
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(count, 1, "表 {} 应存在", table);
        }
    }

    #[test]
    fn test_register_toolchain_inserts_row() {
        // 注册一条 toolchain，验证字段正确写入
        let store = ToolchainStore::open_in_memory().unwrap();
        let result = store
            .register_toolchain(
                "gcc-13",
                "/usr/bin/gcc",
                "gcc",
                "13.0",
                "x86_64-linux-gnu",
                "/usr",
                &["/usr/include".into()],
                &[("DEBUG".into(), "1".into())],
                "fp_test_001",
                "test toolchain",
            )
            .unwrap();
        assert_eq!(result["name"], "gcc-13");
        assert_eq!(result["compiler_path"], "/usr/bin/gcc");
        assert_eq!(result["compiler_type"], "gcc");
        assert_eq!(result["version"], "13.0");
        assert_eq!(result["target_triple"], "x86_64-linux-gnu");
        assert_eq!(result["fingerprint"], "fp_test_001");
        assert_eq!(result["description"], "test toolchain");
        assert!(result["id"].as_i64().unwrap_or(0) > 0);
    }

    #[test]
    fn test_register_toolchain_dedup_by_fingerprint() {
        // 相同 fingerprint 视为同一 toolchain，返回现有记录
        let store = ToolchainStore::open_in_memory().unwrap();
        let _ = store
            .register_toolchain(
                "gcc-13",
                "/usr/bin/gcc",
                "gcc",
                "13.0",
                "",
                "",
                &[],
                &[],
                "fp_dedup",
                "",
            )
            .unwrap();
        // 用不同 name 但相同 fingerprint 注册
        let result = store
            .register_toolchain(
                "gcc-13-alias",
                "/usr/bin/gcc",
                "gcc",
                "13.0",
                "",
                "",
                &[],
                &[],
                "fp_dedup",
                "",
            )
            .unwrap();
        // 应返回原 name（fingerprint 已存在，不插入）
        assert_eq!(result["name"], "gcc-13", "相同 fingerprint 应返回现有记录");
        // 列表应只有 1 条
        let all = store.list_toolchains().unwrap();
        assert_eq!(all.len(), 1);
    }

    #[test]
    fn test_get_toolchain_by_name_and_id() {
        // 先按 name 查，再按 id 查
        let store = ToolchainStore::open_in_memory().unwrap();
        let registered = store
            .register_toolchain(
                "clang-18",
                "/usr/bin/clang",
                "clang",
                "18.0",
                "",
                "",
                &[],
                &[],
                "fp_clang",
                "",
            )
            .unwrap();
        let id = registered["id"].as_i64().unwrap();

        let by_name = store.get_toolchain("clang-18").unwrap();
        assert!(by_name.is_some());
        assert_eq!(by_name.as_ref().unwrap()["id"], id);

        let by_id = store.get_toolchain(&id.to_string()).unwrap();
        assert!(by_id.is_some());
        assert_eq!(by_id.as_ref().unwrap()["name"], "clang-18");
    }

    #[test]
    fn test_get_toolchain_returns_none_for_missing() {
        let store = ToolchainStore::open_in_memory().unwrap();
        assert!(store.get_toolchain("nonexistent").unwrap().is_none());
        assert!(store.get_toolchain("99999").unwrap().is_none());
    }

    #[test]
    fn test_list_toolchains_returns_all_ordered_by_id() {
        let store = ToolchainStore::open_in_memory().unwrap();
        store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        store
            .register_toolchain("tc2", "/p2", "clang", "", "", "", &[], &[], "fp2", "")
            .unwrap();
        store
            .register_toolchain("tc3", "/p3", "arm-gcc", "", "", "", &[], &[], "fp3", "")
            .unwrap();
        let all = store.list_toolchains().unwrap();
        assert_eq!(all.len(), 3);
        // 验证按 id 升序
        assert_eq!(all[0]["name"], "tc1");
        assert_eq!(all[1]["name"], "tc2");
        assert_eq!(all[2]["name"], "tc3");
    }

    #[test]
    fn test_delete_toolchain_by_name() {
        let store = ToolchainStore::open_in_memory().unwrap();
        store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        let deleted = store.delete_toolchain("tc1").unwrap();
        assert_eq!(deleted, 1);
        assert!(store.list_toolchains().unwrap().is_empty());
    }

    #[test]
    fn test_delete_toolchain_by_id() {
        let store = ToolchainStore::open_in_memory().unwrap();
        let registered = store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        let id = registered["id"].as_i64().unwrap();
        let deleted = store.delete_toolchain(&id.to_string()).unwrap();
        assert_eq!(deleted, 1);
    }

    #[test]
    fn test_delete_toolchain_returns_zero_for_missing() {
        let store = ToolchainStore::open_in_memory().unwrap();
        let deleted = store.delete_toolchain("nonexistent").unwrap();
        assert_eq!(deleted, 0);
    }

    #[test]
    fn test_delete_toolchain_cascades_bindings() {
        // 删除 toolchain 时应同时删除 workspace_toolchains 绑定
        let store = ToolchainStore::open_in_memory().unwrap();
        let tc = store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        let tc_id = tc["id"].as_i64().unwrap();
        store.bind_toolchain_to_workspace(1, tc_id, "").unwrap();
        // 删除 toolchain
        store.delete_toolchain("tc1").unwrap();
        // 绑定应也被删除
        let bindings = store.get_workspace_toolchains(1, None).unwrap();
        assert!(bindings.is_empty());
    }

    #[test]
    fn test_bind_toolchain_to_workspace() {
        let store = ToolchainStore::open_in_memory().unwrap();
        let tc = store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        let tc_id = tc["id"].as_i64().unwrap();
        store.bind_toolchain_to_workspace(1, tc_id, "bch1").unwrap();
        let bindings = store.get_workspace_toolchains(1, Some("bch1")).unwrap();
        assert_eq!(bindings.len(), 1);
        assert_eq!(bindings[0]["name"], "tc1");
    }

    #[test]
    fn test_get_workspace_toolchains_filters_by_build_context() {
        // 不同 build_context_hash 应返回不同绑定
        let store = ToolchainStore::open_in_memory().unwrap();
        let tc = store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        let tc_id = tc["id"].as_i64().unwrap();
        store
            .bind_toolchain_to_workspace(1, tc_id, "bch_debug")
            .unwrap();
        store
            .bind_toolchain_to_workspace(1, tc_id, "bch_release")
            .unwrap();
        let debug = store
            .get_workspace_toolchains(1, Some("bch_debug"))
            .unwrap();
        assert_eq!(debug.len(), 1);
        let release = store
            .get_workspace_toolchains(1, Some("bch_release"))
            .unwrap();
        assert_eq!(release.len(), 1);
        let all = store.get_workspace_toolchains(1, None).unwrap();
        assert_eq!(all.len(), 2);
    }

    #[test]
    fn test_register_build_context() {
        let store = ToolchainStore::open_in_memory().unwrap();
        let result = store
            .register_build_context(
                1,
                "debug",
                &["-g".into(), "-O0".into()],
                &[("DEBUG".into(), "1".into())],
                &["/usr/include".into()],
                true,
            )
            .unwrap();
        assert_eq!(result["workspace_id"], 1);
        assert_eq!(result["name"], "debug");
        assert_eq!(result["is_active"], true);
        assert!(!result["build_context_hash"].as_str().unwrap().is_empty());
    }

    #[test]
    fn test_register_build_context_set_active_clears_others() {
        // set_active=true 时清除其他 active
        // 注意：两次注册内容必须不同，否则 bch 相同会被 INSERT OR REPLACE 覆盖
        let store = ToolchainStore::open_in_memory().unwrap();
        store
            .register_build_context(1, "debug", &["-g".into()], &[], &[], true)
            .unwrap();
        store
            .register_build_context(1, "release", &["-O2".into()], &[], &[], true)
            .unwrap();
        // 现在 release 应为 active，debug 应为非 active
        let contexts = store.list_build_contexts(1).unwrap();
        assert_eq!(contexts.len(), 2);
        let active_count = contexts
            .iter()
            .filter(|c| c["is_active"].as_bool().unwrap_or(false))
            .count();
        assert_eq!(active_count, 1, "只能有一个 active context");
        let active = store.get_active_build_context(1).unwrap().unwrap();
        assert_eq!(active["name"], "release");
    }

    #[test]
    fn test_get_build_context_exact_match() {
        let store = ToolchainStore::open_in_memory().unwrap();
        let registered = store
            .register_build_context(1, "debug", &["-g".into()], &[], &[], false)
            .unwrap();
        let bch = registered["build_context_hash"].as_str().unwrap();
        let fetched = store.get_build_context(1, bch).unwrap().unwrap();
        assert_eq!(fetched["name"], "debug");
    }

    #[test]
    fn test_get_build_context_prefix_match() {
        // 短 hash 前缀匹配
        let store = ToolchainStore::open_in_memory().unwrap();
        let registered = store
            .register_build_context(1, "debug", &["-g".into()], &[], &[], false)
            .unwrap();
        let bch = registered["build_context_hash"].as_str().unwrap();
        let short = &bch[..8];
        let fetched = store.get_build_context(1, short).unwrap().unwrap();
        assert_eq!(fetched["name"], "debug");
    }

    #[test]
    fn test_get_build_context_returns_none_for_missing() {
        let store = ToolchainStore::open_in_memory().unwrap();
        assert!(store.get_build_context(1, "nonexistent").unwrap().is_none());
    }

    #[test]
    fn test_list_build_contexts() {
        let store = ToolchainStore::open_in_memory().unwrap();
        // 使用不同的 compile_flags 产生不同的 build_context_hash（避免 INSERT OR REPLACE 覆盖）
        store
            .register_build_context(1, "debug", &["-g".into()], &[], &[], false)
            .unwrap();
        store
            .register_build_context(1, "release", &["-O2".into()], &[], &[], true)
            .unwrap();
        let contexts = store.list_build_contexts(1).unwrap();
        assert_eq!(contexts.len(), 2);
        // 与 Python 真相源一致：按 created_at 升序，不按 active 重排
        assert_eq!(contexts[0]["name"], "debug");
        assert_eq!(contexts[0]["is_active"], false);
        assert_eq!(contexts[1]["name"], "release");
        assert_eq!(contexts[1]["is_active"], true);
    }

    #[test]
    fn test_set_active_build_context() {
        let store = ToolchainStore::open_in_memory().unwrap();
        // 使用不同的 compile_flags 产生不同的 build_context_hash（避免 INSERT OR REPLACE 覆盖）
        let c1 = store
            .register_build_context(1, "debug", &["-g".into()], &[], &[], false)
            .unwrap();
        let c2 = store
            .register_build_context(1, "release", &["-O2".into()], &[], &[], false)
            .unwrap();
        // 初始都非 active
        // 设置 c1 为 active
        let bch1 = c1["build_context_hash"].as_str().unwrap();
        assert!(store.set_active_build_context(1, bch1).unwrap());
        let active = store.get_active_build_context(1).unwrap().unwrap();
        assert_eq!(active["name"], "debug");
        // 切换到 c2
        let bch2 = c2["build_context_hash"].as_str().unwrap();
        assert!(store.set_active_build_context(1, bch2).unwrap());
        let active = store.get_active_build_context(1).unwrap().unwrap();
        assert_eq!(active["name"], "release");
    }

    #[test]
    fn test_set_active_build_context_returns_false_for_missing() {
        let store = ToolchainStore::open_in_memory().unwrap();
        assert!(!store.set_active_build_context(1, "nonexistent").unwrap());
    }

    #[test]
    fn test_delete_build_context_cascades_edges() {
        // 删除 build context 应同时删除其 resolved_edges
        let store = ToolchainStore::open_in_memory().unwrap();
        let ctx = store
            .register_build_context(1, "debug", &[], &[], &[], false)
            .unwrap();
        let bch = ctx["build_context_hash"].as_str().unwrap();
        store
            .store_resolved_edges(
                1,
                bch,
                &[ResolvedEdgeInput {
                    caller_symbol_id: 1,
                    callee_symbol_id: 2,
                    callee_name: "foo".into(),
                    callee_file: "bar.rs".into(),
                    call_line: 10,
                    resolution_method: "exact_match".into(),
                }],
            )
            .unwrap();
        // 删除 context
        let deleted = store.delete_build_context(1, bch).unwrap();
        assert_eq!(deleted, 1);
        // edges 也应被删除
        let edges = store.get_resolved_edges(1, bch, None, None).unwrap();
        assert!(edges.is_empty());
    }

    #[test]
    fn test_store_resolved_edges_inserts_rows() {
        let store = ToolchainStore::open_in_memory().unwrap();
        let inserted = store
            .store_resolved_edges(
                1,
                "bch1",
                &[
                    ResolvedEdgeInput {
                        caller_symbol_id: 1,
                        callee_symbol_id: 2,
                        callee_name: "foo".into(),
                        callee_file: "bar.rs".into(),
                        call_line: 10,
                        resolution_method: "exact_match".into(),
                    },
                    ResolvedEdgeInput {
                        caller_symbol_id: 1,
                        callee_symbol_id: 3,
                        callee_name: "baz".into(),
                        callee_file: "qux.rs".into(),
                        call_line: 20,
                        resolution_method: "include_path".into(),
                    },
                ],
            )
            .unwrap();
        assert_eq!(inserted, 2);
    }

    #[test]
    fn test_store_resolved_edges_dedup_by_unique_constraint() {
        // 同一 (workspace, ctx, caller, callee, call_line) 应被 IGNORE
        let store = ToolchainStore::open_in_memory().unwrap();
        let edge = ResolvedEdgeInput {
            caller_symbol_id: 1,
            callee_symbol_id: 2,
            callee_name: "foo".into(),
            callee_file: "bar.rs".into(),
            call_line: 10,
            resolution_method: "exact_match".into(),
        };
        store
            .store_resolved_edges(1, "bch1", &[edge.clone()])
            .unwrap();
        let inserted = store.store_resolved_edges(1, "bch1", &[edge]).unwrap();
        assert_eq!(inserted, 0, "重复 edge 应被 IGNORE");
    }

    #[test]
    fn test_replace_resolved_edges_rolls_back_to_old_cache_on_insert_failure() {
        let store = ToolchainStore::open_in_memory().unwrap();
        let old_edge = ResolvedEdgeInput {
            caller_symbol_id: 1,
            callee_symbol_id: 2,
            callee_name: "old".into(),
            callee_file: "old.rs".into(),
            call_line: 10,
            resolution_method: "exact_match".into(),
        };
        store.store_resolved_edges(1, "bch1", &[old_edge]).unwrap();
        {
            let conn = store.conn();
            conn.execute_batch(
                "CREATE TRIGGER reject_boom BEFORE INSERT ON resolved_edges
                 WHEN NEW.callee_name = 'boom'
                 BEGIN SELECT RAISE(ABORT, 'boom rejected'); END;",
            )
            .unwrap();
        }
        let replacement = [
            ResolvedEdgeInput {
                caller_symbol_id: 3,
                callee_symbol_id: 4,
                callee_name: "new".into(),
                callee_file: "new.rs".into(),
                call_line: 20,
                resolution_method: "exact_match".into(),
            },
            ResolvedEdgeInput {
                caller_symbol_id: 5,
                callee_symbol_id: 6,
                callee_name: "boom".into(),
                callee_file: "boom.rs".into(),
                call_line: 30,
                resolution_method: "exact_match".into(),
            },
        ];
        assert!(store
            .replace_resolved_edges(1, "bch1", &replacement)
            .is_err());
        let rows = store.get_resolved_edges(1, "bch1", None, None).unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["callee_name"], "old");
    }

    #[test]
    fn test_get_resolved_edges_filters_by_caller() {
        let store = ToolchainStore::open_in_memory().unwrap();
        store
            .store_resolved_edges(
                1,
                "bch1",
                &[
                    ResolvedEdgeInput {
                        caller_symbol_id: 1,
                        callee_symbol_id: 2,
                        callee_name: "foo".into(),
                        callee_file: "a.rs".into(),
                        call_line: 10,
                        resolution_method: "exact_match".into(),
                    },
                    ResolvedEdgeInput {
                        caller_symbol_id: 5,
                        callee_symbol_id: 2,
                        callee_name: "foo".into(),
                        callee_file: "a.rs".into(),
                        call_line: 20,
                        resolution_method: "exact_match".into(),
                    },
                ],
            )
            .unwrap();
        let all = store.get_resolved_edges(1, "bch1", None, None).unwrap();
        assert_eq!(all.len(), 2);
        let only_caller_1 = store.get_resolved_edges(1, "bch1", Some(1), None).unwrap();
        assert_eq!(only_caller_1.len(), 1);
        assert_eq!(only_caller_1[0]["caller_symbol_id"], 1);
    }

    #[test]
    fn test_get_resolved_edges_applies_limit() {
        let store = ToolchainStore::open_in_memory().unwrap();
        let edges: Vec<ResolvedEdgeInput> = (0..10)
            .map(|i| ResolvedEdgeInput {
                caller_symbol_id: 1,
                callee_symbol_id: 100 + i,
                callee_name: format!("callee_{}", i),
                callee_file: "x.rs".into(),
                call_line: i * 10,
                resolution_method: "exact_match".into(),
            })
            .collect();
        store.store_resolved_edges(1, "bch1", &edges).unwrap();
        let limited = store.get_resolved_edges(1, "bch1", None, Some(5)).unwrap();
        assert_eq!(limited.len(), 5);
    }

    #[test]
    fn test_delete_resolved_edges_by_context() {
        let store = ToolchainStore::open_in_memory().unwrap();
        store
            .store_resolved_edges(
                1,
                "bch1",
                &[ResolvedEdgeInput {
                    caller_symbol_id: 1,
                    callee_symbol_id: 2,
                    callee_name: "foo".into(),
                    callee_file: "a.rs".into(),
                    call_line: 10,
                    resolution_method: "exact_match".into(),
                }],
            )
            .unwrap();
        store
            .store_resolved_edges(
                1,
                "bch2",
                &[ResolvedEdgeInput {
                    caller_symbol_id: 1,
                    callee_symbol_id: 3,
                    callee_name: "bar".into(),
                    callee_file: "b.rs".into(),
                    call_line: 20,
                    resolution_method: "exact_match".into(),
                }],
            )
            .unwrap();
        // 只删 bch1
        let deleted = store.delete_resolved_edges(1, Some("bch1")).unwrap();
        assert_eq!(deleted, 1);
        // bch2 应保留
        let remaining_bch2 = store.get_resolved_edges(1, "bch2", None, None).unwrap();
        assert_eq!(remaining_bch2.len(), 1);
    }

    #[test]
    fn test_delete_resolved_edges_all_for_workspace() {
        let store = ToolchainStore::open_in_memory().unwrap();
        store
            .store_resolved_edges(
                1,
                "bch1",
                &[ResolvedEdgeInput {
                    caller_symbol_id: 1,
                    callee_symbol_id: 2,
                    callee_name: "foo".into(),
                    callee_file: "a.rs".into(),
                    call_line: 10,
                    resolution_method: "exact_match".into(),
                }],
            )
            .unwrap();
        store
            .store_resolved_edges(
                1,
                "bch2",
                &[ResolvedEdgeInput {
                    caller_symbol_id: 1,
                    callee_symbol_id: 3,
                    callee_name: "bar".into(),
                    callee_file: "b.rs".into(),
                    call_line: 20,
                    resolution_method: "exact_match".into(),
                }],
            )
            .unwrap();
        let deleted = store.delete_resolved_edges(1, None).unwrap();
        assert_eq!(deleted, 2);
    }

    #[test]
    fn test_count_resolved_edges() {
        let store = ToolchainStore::open_in_memory().unwrap();
        store
            .store_resolved_edges(
                1,
                "bch1",
                &[
                    ResolvedEdgeInput {
                        caller_symbol_id: 1,
                        callee_symbol_id: 2,
                        callee_name: "foo".into(),
                        callee_file: "a.rs".into(),
                        call_line: 10,
                        resolution_method: "exact_match".into(),
                    },
                    ResolvedEdgeInput {
                        caller_symbol_id: 1,
                        callee_symbol_id: 3,
                        callee_name: "bar".into(),
                        callee_file: "b.rs".into(),
                        call_line: 20,
                        resolution_method: "exact_match".into(),
                    },
                ],
            )
            .unwrap();
        assert_eq!(store.count_resolved_edges(1, "bch1").unwrap(), 2);
        assert_eq!(store.count_resolved_edges(1, "bch2").unwrap(), 0);
    }

    #[test]
    fn test_list_build_context_edges() {
        let store = ToolchainStore::open_in_memory().unwrap();
        store
            .store_resolved_edges(
                1,
                "bch1",
                &[ResolvedEdgeInput {
                    caller_symbol_id: 1,
                    callee_symbol_id: 2,
                    callee_name: "foo".into(),
                    callee_file: "a.rs".into(),
                    call_line: 10,
                    resolution_method: "exact_match".into(),
                }],
            )
            .unwrap();
        store
            .store_resolved_edges(
                1,
                "bch2",
                &[
                    ResolvedEdgeInput {
                        caller_symbol_id: 1,
                        callee_symbol_id: 3,
                        callee_name: "bar".into(),
                        callee_file: "b.rs".into(),
                        call_line: 20,
                        resolution_method: "exact_match".into(),
                    },
                    ResolvedEdgeInput {
                        caller_symbol_id: 1,
                        callee_symbol_id: 4,
                        callee_name: "baz".into(),
                        callee_file: "c.rs".into(),
                        call_line: 30,
                        resolution_method: "exact_match".into(),
                    },
                ],
            )
            .unwrap();
        let summary = store.list_build_context_edges(1).unwrap();
        assert_eq!(summary.len(), 2);
        // 应包含 bch1 和 bch2
        let hashes: Vec<&str> = summary
            .iter()
            .map(|s| s["build_context_hash"].as_str().unwrap())
            .collect();
        assert!(hashes.contains(&"bch1"));
        assert!(hashes.contains(&"bch2"));
    }

    #[test]
    fn test_resolve_toolchain_exact_match() {
        // 1. 精确匹配 build_context_hash
        let store = ToolchainStore::open_in_memory().unwrap();
        let tc = store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        let tc_id = tc["id"].as_i64().unwrap();
        store.bind_toolchain_to_workspace(1, tc_id, "bch1").unwrap();
        let resolved = store.resolve_toolchain(1, Some("bch1")).unwrap().unwrap();
        assert_eq!(resolved["name"], "tc1");
    }

    #[test]
    fn test_resolve_toolchain_fallback_to_active() {
        // 2. 精确匹配失败 → active context
        let store = ToolchainStore::open_in_memory().unwrap();
        let tc = store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        let tc_id = tc["id"].as_i64().unwrap();
        let ctx = store
            .register_build_context(1, "debug", &[], &[], &[], true)
            .unwrap();
        let active_bch = ctx["build_context_hash"].as_str().unwrap();
        store
            .bind_toolchain_to_workspace(1, tc_id, active_bch)
            .unwrap();
        // 用一个不存在的 bch 查询，应降级到 active
        let resolved = store
            .resolve_toolchain(1, Some("nonexistent_bch"))
            .unwrap()
            .unwrap();
        assert_eq!(resolved["name"], "tc1");
    }

    #[test]
    fn test_resolve_toolchain_fallback_to_default() {
        // 3. 精确失败 + active 失败 → 默认 context（空 hash）
        let store = ToolchainStore::open_in_memory().unwrap();
        let tc = store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        let tc_id = tc["id"].as_i64().unwrap();
        store.bind_toolchain_to_workspace(1, tc_id, "").unwrap();
        let resolved = store
            .resolve_toolchain(1, Some("nonexistent_bch"))
            .unwrap()
            .unwrap();
        assert_eq!(resolved["name"], "tc1");
    }

    #[test]
    fn test_resolve_toolchain_returns_none_when_no_binding() {
        // 4. 无任何绑定
        let store = ToolchainStore::open_in_memory().unwrap();
        store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        let resolved = store.resolve_toolchain(1, Some("bch1")).unwrap();
        assert!(resolved.is_none());
    }

    #[test]
    fn test_compute_build_context_hash_deterministic() {
        // 相同输入应产生相同 hash
        let h1 = compute_build_context_hash(
            &["-g".into(), "-O0".into()],
            &[("DEBUG".into(), "1".into())],
            &["/usr/include".into()],
        );
        let h2 = compute_build_context_hash(
            &["-O0".into(), "-g".into()],
            &[("DEBUG".into(), "1".into())],
            &["/usr/include".into()],
        );
        assert_eq!(h1, h2, "顺序无关");
    }

    #[test]
    fn test_compute_build_context_hash_different_inputs() {
        let h1 = compute_build_context_hash(&["-g".into()], &[], &[]);
        let h2 = compute_build_context_hash(&["-O2".into()], &[], &[]);
        assert_ne!(h1, h2);
    }

    #[test]
    fn test_persistence_across_reopen() {
        // 验证 toolchain.db 持久化
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("toolchain.db");
        let db_path_str = db_path.to_string_lossy().to_string();
        {
            let store = ToolchainStore::open(&db_path_str).unwrap();
            store
                .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
                .unwrap();
        }
        {
            let store = ToolchainStore::open(&db_path_str).unwrap();
            let all = store.list_toolchains().unwrap();
            assert_eq!(all.len(), 1);
            assert_eq!(all[0]["name"], "tc1");
        }
    }

    #[test]
    fn test_attach_to_target_connection() {
        // ATTACH 后，目标连接应能查询 toolchain 表
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("toolchain.db");
        let db_path_str = db_path.to_string_lossy().to_string();
        let store = ToolchainStore::open(&db_path_str).unwrap();
        store
            .register_toolchain("tc1", "/p1", "gcc", "", "", "", &[], &[], "fp1", "")
            .unwrap();
        // 创建目标连接并 ATTACH
        let target = Connection::open_in_memory().unwrap();
        store.attach_to(&target, "toolchain").unwrap();
        // 查询
        let count: i64 = target
            .query_row("SELECT COUNT(*) FROM toolchain.toolchains", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(count, 1);
    }
}
