//! storage.rs
//! ==========
//!
//! Rust StorageService 核心库（Task C2）
//!
//! 职责：
//! 1. 成为 SQLite registry 数据库连接、WAL 模式设置、外键与超时预置的唯一真相源。
//! 2. 全量内嵌 SCHEMA_VERSION = 52 的 Schema SQL 与版本升级 Migration 路径。
//! 3. 提供完整性检查 (integrity_check)、迁移灾备备份 (backup_before_migration)、
//!    WAL checkpoint 和事务 (BEGIN IMMEDIATE / COMMIT / ROLLBACK) 句柄管理。
//! 4. 暴露 PyO3 接口供 Python db_base.py 的 Facade 安全调用。

use std::fs;
use std::path::Path;
use std::time::Duration;

use pyo3::exceptions::{PyIOError, PyOSError, PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rusqlite::{Connection, ErrorCode, OpenFlags, Transaction, TransactionBehavior};
use sha2::{Digest, Sha256};

use crate::sqlite_query::{
    migrate_role_contract_legacy, seed_operation_params_rule, seed_role_contract_rule,
    seed_verdict_normalization_rule,
};

/// Schema 版本号（真相源对齐 db/schema.py）
pub const SCHEMA_VERSION: u32 = 52;

// db/schema.py is the repository schema authority.  Embedding its SQL at
// compile time keeps frozen Rust artifacts independent of a Python checkout
// while preventing this service from drifting into a second partial schema.
const EMBEDDED_SCHEMA_SOURCE: &str =
    include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../db/schema.py"));

fn canonical_schema_sql() -> Result<String, String> {
    let marker = "SCHEMA_SQL = \"\"\"";
    let start = EMBEDDED_SCHEMA_SOURCE
        .find(marker)
        .ok_or_else(|| "MIGRATION_FAILED: SCHEMA_SQL marker missing".to_string())?
        + marker.len();
    let end = EMBEDDED_SCHEMA_SOURCE[start..]
        .find("\n\"\"\"")
        .ok_or_else(|| "MIGRATION_FAILED: SCHEMA_SQL block unterminated".to_string())?
        + start;
    let sql = &EMBEDDED_SCHEMA_SOURCE[start..end];
    Ok(sql.replace("\r\n", "\n"))
}

pub(crate) fn canonical_schema_checksum() -> Result<String, String> {
    let mut digest = Sha256::new();
    digest.update(canonical_schema_sql()?.as_bytes());
    Ok(format!("{:x}", digest.finalize()))
}

const STORAGE_METADATA_SQL: &str = r#"
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL,
    description TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    checksum TEXT NOT NULL,
    description TEXT NOT NULL,
    applied_at REAL NOT NULL
);
"#;

/// 建表 SQL (v42 全量表结构)
pub const SCHEMA_TABLES_SQL: &str = r#"
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
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
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

CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_hash TEXT NOT NULL,
    comment_type TEXT DEFAULT 'doc',
    content TEXT DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);

CREATE TABLE IF NOT EXISTS file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    version_num INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    parsed_at REAL NOT NULL,
    is_current INTEGER DEFAULT 1,
    is_deleted INTEGER DEFAULT 0,
    commit_hash TEXT DEFAULT '',
    ast_cache BLOB DEFAULT NULL,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    FOREIGN KEY (content_hash) REFERENCES file_contents(content_hash)
);

CREATE TABLE IF NOT EXISTS file_symbol_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    module_path TEXT DEFAULT '',
    depth INTEGER DEFAULT -1,
    is_deleted INTEGER DEFAULT 0,
    FOREIGN KEY (file_version_id) REFERENCES file_versions(id),
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);

CREATE TABLE IF NOT EXISTS call_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    caller_qualified TEXT NOT NULL,
    caller_hash TEXT DEFAULT '',
    callee_name TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT '',
    callee_file TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0,
    FOREIGN KEY (file_version_id) REFERENCES file_versions(id)
);

CREATE TABLE IF NOT EXISTS semgrep_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    content_hash TEXT DEFAULT '',
    rule_id TEXT NOT NULL,
    rule_name TEXT DEFAULT '',
    message TEXT DEFAULT '',
    severity TEXT DEFAULT 'INFO',
    confidence TEXT DEFAULT 'UNKNOWN',
    language TEXT DEFAULT '',
    start_line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    snippet TEXT DEFAULT '',
    fix TEXT DEFAULT '',
    symbol_id INTEGER DEFAULT 0,
    symbol_qualified TEXT DEFAULT '',
    scanned_at REAL DEFAULT 0,
    scan_id INTEGER DEFAULT 0,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    UNIQUE(content_hash, rule_id, start_line)
);

CREATE TABLE IF NOT EXISTS semgrep_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_type TEXT DEFAULT 'full',
    config TEXT DEFAULT '',
    workspace_id INTEGER DEFAULT 0,
    started_at REAL NOT NULL,
    completed_at REAL DEFAULT 0,
    total_findings INTEGER DEFAULT 0,
    files_scanned INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);

CREATE TABLE IF NOT EXISTS git_commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    commit_hash TEXT UNIQUE NOT NULL,
    author_name TEXT DEFAULT '',
    author_email TEXT DEFAULT '',
    committed_at REAL NOT NULL,
    message TEXT DEFAULT '',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS git_file_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_id INTEGER NOT NULL,
    file_instance_id INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    lines_added INTEGER DEFAULT 0,
    lines_deleted INTEGER DEFAULT 0,
    FOREIGN KEY (commit_id) REFERENCES git_commits(id),
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
);

CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    workspace_id INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    closed_at REAL DEFAULT 0,
    applied_at REAL DEFAULT 0,
    parent_id TEXT DEFAULT '',
    depth INTEGER DEFAULT 0,
    sort_order INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS task_steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_file TEXT DEFAULT '',
    target_symbol TEXT DEFAULT '',
    check_items TEXT DEFAULT '',
    status TEXT DEFAULT 'pending',
    result TEXT DEFAULT '',
    created_at REAL NOT NULL,
    completed_at REAL DEFAULT 0,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS task_quality_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_id INTEGER DEFAULT 0,
    finding_type TEXT NOT NULL,
    severity TEXT DEFAULT 'WARN',
    message TEXT NOT NULL,
    details TEXT DEFAULT '',
    status TEXT DEFAULT 'open',
    created_at REAL NOT NULL,
    resolved_at REAL DEFAULT 0,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE TABLE IF NOT EXISTS test_case_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    test_fn_id INTEGER NOT NULL,
    tested_fn_id INTEGER NOT NULL,
    source TEXT DEFAULT 'import',
    created_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (test_fn_id) REFERENCES symbols(id),
    FOREIGN KEY (tested_fn_id) REFERENCES symbols(id)
);

CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    test_fn_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    duration_ms REAL DEFAULT 0,
    error_message TEXT DEFAULT '',
    ci_run_id TEXT DEFAULT '',
    ci_url TEXT DEFAULT '',
    run_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (test_fn_id) REFERENCES symbols(id)
);

CREATE TABLE IF NOT EXISTS rollback_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,
    task_id TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    phase INTEGER NOT NULL,
    production_entry TEXT NOT NULL,
    rollback_entry TEXT NOT NULL,
    rollback_flag INTEGER NOT NULL DEFAULT 0,
    rollback_window_until TEXT DEFAULT '',
    config_blob TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
"#;

/// 索引与触发器 SQL
pub const SCHEMA_INDEXES_SQL: &str = r#"
CREATE INDEX IF NOT EXISTS idx_workspaces_active ON workspaces(is_active);
CREATE INDEX IF NOT EXISTS idx_workspaces_active_task ON workspaces(active_task_id);
CREATE INDEX IF NOT EXISTS idx_file_contents_lang ON file_contents(language);
CREATE INDEX IF NOT EXISTS idx_file_instances_workspace ON file_instances(workspace_id);
CREATE INDEX IF NOT EXISTS idx_file_instances_hash ON file_instances(current_content_hash);
CREATE INDEX IF NOT EXISTS idx_file_instances_relpath ON file_instances(rel_path);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_symbols_hash ON symbols(symbol_hash);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_kind_file ON symbols(kind, file_instance_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_symbols_unique ON symbols(file_instance_id, name, start_line);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_calls_caller_name ON calls(caller_name);
CREATE INDEX IF NOT EXISTS idx_calls_callee_name ON calls(callee_name);
CREATE INDEX IF NOT EXISTS idx_calls_callee_id ON calls(callee_id);
CREATE INDEX IF NOT EXISTS idx_comments_hash ON comments(symbol_hash);
CREATE INDEX IF NOT EXISTS idx_file_versions_instance ON file_versions(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_file_versions_hash ON file_versions(content_hash);
CREATE INDEX IF NOT EXISTS idx_file_versions_current ON file_versions(file_instance_id, is_current);
CREATE INDEX IF NOT EXISTS idx_file_symbol_versions_version ON file_symbol_versions(file_version_id);
CREATE INDEX IF NOT EXISTS idx_file_symbol_versions_hash ON file_symbol_versions(symbol_hash);
CREATE INDEX IF NOT EXISTS idx_call_versions_version ON call_versions(file_version_id);
CREATE INDEX IF NOT EXISTS idx_call_versions_caller_hash ON call_versions(caller_hash);
CREATE INDEX IF NOT EXISTS idx_semgrep_file ON semgrep_findings(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_semgrep_symbol ON semgrep_findings(symbol_qualified);
CREATE INDEX IF NOT EXISTS idx_semgrep_severity ON semgrep_findings(severity);
CREATE INDEX IF NOT EXISTS idx_semgrep_rule ON semgrep_findings(rule_id);
CREATE INDEX IF NOT EXISTS idx_semgrep_scan ON semgrep_findings(scan_id);
CREATE INDEX IF NOT EXISTS idx_git_commits_workspace ON git_commits(workspace_id);
CREATE INDEX IF NOT EXISTS idx_git_file_changes_commit ON git_file_changes(commit_id);
CREATE INDEX IF NOT EXISTS idx_git_file_changes_file ON git_file_changes(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_workspace ON tasks(workspace_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id);
CREATE INDEX IF NOT EXISTS idx_task_steps_status ON task_steps(status);
CREATE INDEX IF NOT EXISTS idx_task_quality_findings_task ON task_quality_findings(task_id);
CREATE INDEX IF NOT EXISTS idx_test_case_relations_workspace ON test_case_relations(workspace_id);
CREATE INDEX IF NOT EXISTS idx_test_runs_workspace ON test_runs(workspace_id, run_at);
CREATE INDEX IF NOT EXISTS idx_rollback_config_task ON rollback_config(task_id);
CREATE INDEX IF NOT EXISTS idx_rollback_config_feature ON rollback_config(feature_name);
CREATE INDEX IF NOT EXISTS idx_rollback_config_flag ON rollback_config(rollback_flag);
"#;

/// 建立基础 SQLite 连接，配置标准的 WAL, timeout(5000ms), foreign_keys
fn open_connection<P: AsRef<Path>>(path: P) -> Result<Connection, rusqlite::Error> {
    let conn = Connection::open_with_flags(
        path.as_ref(),
        OpenFlags::SQLITE_OPEN_READ_WRITE
            | OpenFlags::SQLITE_OPEN_CREATE
            | OpenFlags::SQLITE_OPEN_NO_MUTEX
            | OpenFlags::SQLITE_OPEN_URI,
    )?;

    conn.busy_timeout(Duration::from_millis(5000))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "foreign_keys", "ON")?;

    Ok(conn)
}

fn table_exists(conn: &Connection, name: &str) -> Result<bool, rusqlite::Error> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
        [name],
        |row| row.get(0),
    )
}

/// canonical db/schema.py SCHEMA_SQL 中定义的预期表名集合。
///
/// 与 Rust `canonical_schema_sql()` 提取的 SCHEMA_SQL 块一致（同一来源），
/// 用于策略 A 的"表结构完整性校验"：当 `current_version == expected_version`
/// 但 `schema_migrations` 的 stored checksum 与当前二进制不一致时，用该集合
/// 判断 DB 实际表结构是否与 canonical schema 一致，而非仅凭 checksum 判定。
fn expected_canonical_table_names() -> Vec<String> {
    let sql = canonical_schema_sql().unwrap_or_default();
    let mut names = Vec::new();
    for line in sql.lines() {
        let trimmed = line.trim_start();
        if let Some(rest) = trimmed.strip_prefix("CREATE TABLE ") {
            let rest = rest.strip_prefix("IF NOT EXISTS ").unwrap_or(rest);
            if let Some(name) = rest.split([' ', '(', '\t', '\n']).next() {
                if !name.is_empty() {
                    names.push(name.to_string());
                }
            }
        }
    }
    names
}

/// 校验 DB 实际表结构是否与 canonical schema 一致（策略 A）。
///
/// 返回 Ok(true) 表示所有 canonical 表均存在（DB schema 完整）；
/// Ok(false) 表示存在缺失表（真正的 schema 漂移，应 fail-closed）；
/// Err(e) 表示校验本身失败。
fn schema_shape_matches_canonical(conn: &Connection) -> Result<bool, rusqlite::Error> {
    let expected = expected_canonical_table_names();
    for table in expected {
        if !table_exists(conn, &table)? {
            return Ok(false);
        }
    }
    Ok(true)
}

/// 将 schema_migrations 表中指定版本的 checksum 更新为当前 canonical。
///
/// 仅在表结构完整性校验通过（DB 实际 schema 与 canonical 一致）时调用，
/// 用于修正 8/1 旧 pyd 写入的过期 checksum，使新编译扩展能正常打开 DB。
fn update_stored_checksum_to_canonical(
    conn: &mut Connection,
    version: u32,
) -> Result<(), String> {
    let checksum = canonical_schema_checksum()?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_secs_f64())
        .unwrap_or(0.0);
    conn.execute(
        "INSERT OR REPLACE INTO schema_migrations (version, checksum, description, applied_at)
         VALUES (?1, ?2, ?3, ?4)",
        rusqlite::params![version, checksum, "canonical db/schema.py (checksum reconciled)", now],
    )
    .map_err(|e| format!("{}", e))?;
    Ok(())
}

fn schema_version_from_connection(conn: &Connection) -> Result<u32, rusqlite::Error> {
    if table_exists(conn, "schema_version")? {
        conn.query_row(
            "SELECT COALESCE(MAX(version), 0) FROM schema_version",
            [],
            |row| row.get(0),
        )
    } else {
        // Compatibility for databases created by the first incomplete C2
        // implementation.  A successful migration always creates the table.
        conn.query_row("PRAGMA user_version", [], |row| row.get(0))
    }
}

fn has_column(conn: &Connection, table: &str, column: &str) -> Result<bool, rusqlite::Error> {
    let mut statement = conn.prepare(&format!("PRAGMA table_info({table})"))?;
    let columns = statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(columns.iter().any(|value| value == column))
}

fn legacy_shape_migration_required(conn: &Connection) -> Result<Option<String>, rusqlite::Error> {
    if table_exists(conn, "files")? {
        return Ok(Some(
            "legacy files table requires the v2->v3 data-shape migration".to_string(),
        ));
    }
    for (table, column) in [
        ("file_instances", "workspace_id"),
        ("symbols", "file_instance_id"),
        ("symbols", "symbol_hash"),
        ("calls", "caller_id"),
        ("file_versions", "file_instance_id"),
    ] {
        if table_exists(conn, table)? && !has_column(conn, table, column)? {
            return Ok(Some(format!(
                "legacy {table} table is missing {column}; data-shape migration is required"
            )));
        }
    }
    Ok(None)
}

/// 检测 canonical schema 中"后补列"是否已存在（P0-1 复审：短路径也校验列）。
///
/// 背景：v48 曾被陈旧 .pyd 抢先打标（内嵌 schema 无 workspace_id 列），
/// initialize_or_migrate 在 current_version == expected_version 时只校验表名
/// 不校验列，导致主库永久停留"版本已达成但缺列"状态。v50 引入
/// agent_registrations Agent Identity 最小字段（agent-task-contract-design.md §4.1），
/// 同样需要列级校验。此函数枚举已知的兼容列（与 sqlite_query.rs
/// execute_existing_schema 的列级清单一致），缺列时返回需要补的列列表，
/// 调用方应执行 compat 修复（而非静默放行）。
fn missing_compat_columns(conn: &Connection) -> Result<Vec<(String, String, &'static str)>, rusqlite::Error> {
    const COMPAT_COLUMNS: &[(&str, &str, &str)] = &[
        ("guardrail_findings", "workspace_id", "INTEGER NOT NULL DEFAULT 0"),
        ("agent_registrations", "agent_instance_id", "TEXT DEFAULT ''"),
        ("agent_registrations", "client_id", "TEXT DEFAULT ''"),
        ("agent_registrations", "provider", "TEXT DEFAULT ''"),
        ("agent_registrations", "model_id", "TEXT DEFAULT ''"),
        ("agent_registrations", "model_mode", "TEXT DEFAULT ''"),
        ("agent_registrations", "system_fingerprint", "TEXT DEFAULT ''"),
        ("agent_registrations", "runtime_hash", "TEXT DEFAULT ''"),
        ("agent_registrations", "session_id", "TEXT DEFAULT ''"),
        ("agent_registrations", "role", "TEXT DEFAULT ''"),
        // v57（1E，T-1787203926824-9f873bfc）：commit 030fd8c 在 v57 已发布后
        // 才把 verdict/gate provenance 列补入 canonical SCHEMA_SQL，但未 bump
        // 版本号，导致既有 v57 库缺列。这些列定义与 sqlite_query.rs
        // execute_existing_schema 的列级清单保持同一真相源；缺列时强制走
        // 迁移事务，由 apply_task_contract_v57_compat 幂等补列。
        ("task_contract_revisions", "normalization_version", "TEXT DEFAULT ''"),
        ("task_contract_revisions", "normalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_verdict_events", "step_id", "TEXT DEFAULT ''"),
        ("task_verdict_events", "role_contract_lineage_id", "TEXT DEFAULT ''"),
        ("task_verdict_events", "role_contract_revision_id", "TEXT DEFAULT ''"),
        ("task_verdict_events", "role_contract_revision", "INTEGER DEFAULT 0"),
        ("task_verdict_events", "role_contract_hash", "TEXT DEFAULT ''"),
        ("task_verdict_events", "canonicalization_version", "TEXT DEFAULT ''"),
        ("task_verdict_events", "canonicalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_verdict_events", "normalization_version", "TEXT DEFAULT ''"),
        ("task_verdict_events", "normalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "step_id", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "role_contract_lineage_id", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "role_contract_revision_id", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "role_contract_revision", "INTEGER DEFAULT 0"),
        ("task_gate_decisions", "role_contract_hash", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "canonicalization_version", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "canonicalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "normalization_version", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "normalization_rules_hash", "TEXT DEFAULT ''"),
    ];
    let mut missing = Vec::new();
    for (table, column, definition) in COMPAT_COLUMNS {
        if table_exists(conn, table)? && !has_column(conn, table, column)? {
            missing.push(((*table).to_string(), (*column).to_string(), *definition));
        }
    }
    Ok(missing)
}

/// 查询当前 Schema 版本
pub fn get_schema_version<P: AsRef<Path>>(path: P) -> Result<u32, rusqlite::Error> {
    if !path.as_ref().exists() {
        return Ok(0);
    }
    let conn = open_connection(path)?;
    schema_version_from_connection(&conn)
}

/// 尝试迁移旧版 v1/v2 的 `files` 表结构到 v3 的 `file_instances` 与 `workspaces`
fn table_exists_tx(tx: &Transaction<'_>, name: &str) -> Result<bool, rusqlite::Error> {
    tx.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type='table' AND name=?1)",
        [name],
        |row| row.get(0),
    )
}

fn has_column_tx(tx: &Transaction<'_>, table: &str, column: &str) -> Result<bool, rusqlite::Error> {
    let mut statement = tx.prepare(&format!("PRAGMA table_info({table})"))?;
    let columns = statement
        .query_map([], |row| row.get::<_, String>(1))?
        .collect::<Result<Vec<_>, _>>()?;
    Ok(columns.iter().any(|value| value == column))
}

/// v48（W2.3 P1-1）：为既有库补齐 guardrail_findings.workspace_id 归属列并
/// 将无归属 open 旧行置为 orphaned（fail-closed）。
///
/// 设计原则：旧数据无法安全归属时**禁止静默归入当前 workspace**。workspace_id=0
/// 且 status='open' 的旧行被标记为 'orphaned'，查询层不返回 orphan 行，直到用户
/// 显式迁移或清理。CREATE TABLE IF NOT EXISTS 不会给已存在表加列，此函数在
/// canonical SCHEMA_SQL 执行后调用，保证既有库与新库行为一致。
fn apply_guardrail_findings_v48_compat(tx: &Transaction<'_>) -> Result<(), rusqlite::Error> {
    if !table_exists_tx(tx, "guardrail_findings")? {
        return Ok(()); // 极简库没有此表，跳过
    }
    if !has_column_tx(tx, "guardrail_findings", "workspace_id")? {
        tx.execute(
            "ALTER TABLE guardrail_findings ADD COLUMN workspace_id INTEGER NOT NULL DEFAULT 0",
            [],
        )?;
    }
    tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_guardrail_findings_workspace
         ON guardrail_findings(workspace_id)",
        [],
    )?;
    tx.execute(
        "CREATE INDEX IF NOT EXISTS idx_guardrail_findings_ws_file
         ON guardrail_findings(workspace_id, file_path, symbol_hash)",
        [],
    )?;
    tx.execute(
        "UPDATE guardrail_findings SET status='orphaned'
         WHERE workspace_id = 0 AND status = 'open'",
        [],
    )?;
    Ok(())
}

/// v50（Agent-task-contract M1）：为既有库补齐 agent_registrations 的
/// Agent Identity 最小字段（agent-task-contract-design.md §4.1）。
///
/// 背景：v50 在 agent_registrations 上扩展 9 个身份列（agent_instance_id /
/// client_id / provider / model_id / model_mode / system_fingerprint /
/// runtime_hash / session_id / role）。CREATE TABLE IF NOT EXISTS 不会给已存在
/// 表加列，陈旧二进制可能把"缺列库"打标为 v50。此函数在 canonical SCHEMA_SQL
/// 执行前调用，幂等补齐身份列，随后 SCHEMA_SQL 里的 CREATE INDEX
/// idx_agent_registrations_instance 才能成功建立。全新库时表由 SCHEMA_SQL 建齐，
/// 本函数探测到列已存在会跳过，无副作用。
fn apply_agent_registrations_v50_compat(tx: &Transaction<'_>) -> Result<(), rusqlite::Error> {
    if !table_exists_tx(tx, "agent_registrations")? {
        return Ok(()); // 极简库没有此表，跳过
    }
    const IDENTITY_COLUMNS: &[&str] = &[
        "agent_instance_id",
        "client_id",
        "provider",
        "model_id",
        "model_mode",
        "system_fingerprint",
        "runtime_hash",
        "session_id",
        "role",
    ];
    for column in IDENTITY_COLUMNS {
        if !has_column_tx(tx, "agent_registrations", column)? {
            tx.execute(
                &format!("ALTER TABLE agent_registrations ADD COLUMN {column} TEXT DEFAULT ''"),
                [],
            )?;
        }
    }
    Ok(())
}

/// v57（1E，T-1787203926824-9f873bfc）：为既有库补齐 Task Contract / Verdict /
/// Gate 的 provenance 与 normalization 绑定列。
///
/// 背景：commit 030fd8c 在 v57 已发布后向 canonical db/schema.py SCHEMA_SQL 补入
/// 这些列（task_contract_revisions ×2、task_verdict_events ×9、
/// task_gate_decisions ×9），但未递增版本号，导致既有 v57 库（1）stored checksum
/// 与当前二进制失配，（2）缺 11 张 v53-v57 表，（3）缺上述列。CREATE TABLE IF
/// NOT EXISTS 不会给已存在表加列，必须显式 ALTER TABLE 补齐；本函数在 canonical
/// SCHEMA_SQL 执行前调用（与 apply_guardrail_findings_v48_compat /
/// apply_agent_registrations_v50_compat 同一模式），幂等，绝不动已有数据行。
/// 全新库时表由 SCHEMA_SQL 建齐，本函数探测到列已存在会跳过，无副作用。
fn apply_task_contract_v57_compat(tx: &Transaction<'_>) -> Result<(), rusqlite::Error> {
    const COMPAT_COLUMNS: &[(&str, &str, &str)] = &[
        ("task_contract_revisions", "normalization_version", "TEXT DEFAULT ''"),
        ("task_contract_revisions", "normalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_verdict_events", "step_id", "TEXT DEFAULT ''"),
        ("task_verdict_events", "role_contract_lineage_id", "TEXT DEFAULT ''"),
        ("task_verdict_events", "role_contract_revision_id", "TEXT DEFAULT ''"),
        ("task_verdict_events", "role_contract_revision", "INTEGER DEFAULT 0"),
        ("task_verdict_events", "role_contract_hash", "TEXT DEFAULT ''"),
        ("task_verdict_events", "canonicalization_version", "TEXT DEFAULT ''"),
        ("task_verdict_events", "canonicalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_verdict_events", "normalization_version", "TEXT DEFAULT ''"),
        ("task_verdict_events", "normalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "step_id", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "role_contract_lineage_id", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "role_contract_revision_id", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "role_contract_revision", "INTEGER DEFAULT 0"),
        ("task_gate_decisions", "role_contract_hash", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "canonicalization_version", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "canonicalization_rules_hash", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "normalization_version", "TEXT DEFAULT ''"),
        ("task_gate_decisions", "normalization_rules_hash", "TEXT DEFAULT ''"),
    ];
    for (table, column, definition) in COMPAT_COLUMNS {
        if table_exists_tx(tx, table)? && !has_column_tx(tx, table, column)? {
            tx.execute(
                &format!("ALTER TABLE {table} ADD COLUMN {column} {definition}"),
                [],
            )?;
        }
    }
    Ok(())
}

fn migrate_legacy_v1_v3_tx(tx: &Transaction<'_>) -> Result<(), rusqlite::Error> {
    // 旧版表通过 *_old_v2 保留到所有映射完成后再删除。整个过程在同一个
    // BEGIN IMMEDIATE 中执行，任意一步失败都会回滚，不能留下半套 schema。
    tx.execute_batch(
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
        INSERT OR IGNORE INTO workspaces
            (id, name, root_path, created_at, is_active, description)
        VALUES (1, 'default', '/', 0.0, 1, 'v2 to v3 migrated');
        CREATE TABLE IF NOT EXISTS file_contents (
            content_hash TEXT PRIMARY KEY,
            language TEXT DEFAULT '',
            total_lines INTEGER DEFAULT 0,
            first_seen_at REAL NOT NULL
        );
        INSERT OR IGNORE INTO file_contents(content_hash, language, total_lines, first_seen_at)
        VALUES ('', '', 0, 0.0);
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
        CREATE TEMP TABLE IF NOT EXISTS legacy_file_map(
            old_id INTEGER PRIMARY KEY,
            new_id INTEGER NOT NULL
        );
        CREATE TEMP TABLE IF NOT EXISTS legacy_symbol_map(
            old_id INTEGER PRIMARY KEY,
            new_id INTEGER NOT NULL
        );
        CREATE TEMP TABLE IF NOT EXISTS legacy_file_version_map(
            old_id INTEGER PRIMARY KEY,
            new_id INTEGER NOT NULL
        );
        "#,
    )?;

    if table_exists_tx(tx, "file_instances")?
        && !has_column_tx(tx, "file_instances", "workspace_id")?
    {
        tx.execute(
            "ALTER TABLE file_instances RENAME TO file_instances_old_v2",
            [],
        )?;
    }
    tx.execute_batch(
        r#"
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
        "#,
    )?;

    if table_exists_tx(tx, "files")? {
        let has_old_versions = table_exists_tx(tx, "file_versions")?
            && has_column_tx(tx, "file_versions", "file_id")?
            && has_column_tx(tx, "file_versions", "content_hash")?;
        if has_old_versions {
            tx.execute(
                "INSERT OR IGNORE INTO file_contents(content_hash, language, total_lines, first_seen_at)
                 SELECT DISTINCT content_hash, '', COALESCE(total_lines, 0), COALESCE(parsed_at, 0)
                 FROM file_versions WHERE content_hash IS NOT NULL",
                [],
            )?;
            tx.execute(
                "INSERT OR IGNORE INTO file_instances
                 (id, workspace_id, rel_path, abs_path, current_content_hash, mtime,
                  total_lines, last_parsed, status, module_path)
                 SELECT f.id, 1, f.path, f.abs_path,
                    COALESCE((SELECT fv.content_hash FROM file_versions fv
                              WHERE fv.file_id=f.id AND fv.is_current=1 LIMIT 1), ''),
                    f.mtime,
                    COALESCE((SELECT fv.total_lines FROM file_versions fv
                              WHERE fv.file_id=f.id AND fv.is_current=1 LIMIT 1), 0),
                    COALESCE((SELECT fv.parsed_at FROM file_versions fv
                              WHERE fv.file_id=f.id AND fv.is_current=1 LIMIT 1), 0),
                    COALESCE(f.status, 'pending'), COALESCE(f.module_path, '')
                 FROM files f",
                [],
            )?;
        } else {
            tx.execute(
                "INSERT OR IGNORE INTO file_instances
                 (id, workspace_id, rel_path, abs_path, current_content_hash, mtime,
                  total_lines, last_parsed, status, module_path)
                 SELECT id, 1, path, abs_path, '', COALESCE(mtime, 0), 0, 0,
                        COALESCE(status, 'pending'), COALESCE(module_path, '')
                 FROM files",
                [],
            )?;
        }
        tx.execute(
            "INSERT OR REPLACE INTO legacy_file_map(old_id, new_id)
             SELECT id, id FROM files",
            [],
        )?;
    } else if table_exists_tx(tx, "file_instances_old_v2")? {
        tx.execute(
            "INSERT OR IGNORE INTO file_instances
             (id, workspace_id, rel_path, abs_path, current_content_hash, mtime,
              total_lines, last_parsed, status, module_path)
             SELECT id, 1, path, abs_path, '', COALESCE(mtime, 0), 0, 0,
                    COALESCE(status, 'pending'), COALESCE(module_path, '')
             FROM file_instances_old_v2",
            [],
        )?;
        tx.execute(
            "INSERT OR REPLACE INTO legacy_file_map(old_id, new_id)
             SELECT id, id FROM file_instances_old_v2",
            [],
        )?;
    }

    let mut legacy_symbols = false;
    if table_exists_tx(tx, "symbols")? && !has_column_tx(tx, "symbols", "file_instance_id")? {
        tx.execute("ALTER TABLE symbols RENAME TO symbols_old_v2", [])?;
        legacy_symbols = true;
    }
    tx.execute_batch(
        r#"
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
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        );
        "#,
    )?;
    if legacy_symbols {
        tx.execute(
            "INSERT OR IGNORE INTO symbol_contents
             (content_hash, name, kind, content, signature, has_comment, qualified_name)
             SELECT COALESCE(NULLIF(sc.content_hash, ''), 'legacy-symbol:' || s.id),
                    s.name, s.kind, '', COALESCE(s.signature, ''),
                    COALESCE(s.has_comment, 0), COALESCE(s.qualified_name, '')
             FROM symbols_old_v2 s
             LEFT JOIN symbol_contents sc ON sc.qualified_name=s.qualified_name",
            [],
        )?;
        tx.execute(
            "INSERT OR IGNORE INTO symbols
             (id, file_instance_id, symbol_hash, name, kind, visibility, start_line,
              end_line, start_col, end_col, signature, has_comment, comment_status,
              module_path, qualified_name, depth)
             SELECT s.id, fm.new_id,
                    COALESCE(NULLIF(sc.content_hash, ''), 'legacy-symbol:' || s.id),
                    s.name, s.kind, COALESCE(s.visibility, 'private'), s.start_line,
                    s.end_line, COALESCE(s.start_col, 0), COALESCE(s.end_col, 0),
                    COALESCE(s.signature, ''), COALESCE(s.has_comment, 0),
                    COALESCE(s.comment_status, 'pending'), COALESCE(s.module_path, ''),
                    COALESCE(s.qualified_name, ''), COALESCE(s.depth, -1)
             FROM symbols_old_v2 s
             JOIN legacy_file_map fm ON fm.old_id=s.file_id
             LEFT JOIN symbol_contents sc ON sc.qualified_name=s.qualified_name",
            [],
        )?;
        tx.execute(
            "INSERT OR REPLACE INTO legacy_symbol_map(old_id, new_id)
             SELECT id, id FROM symbols_old_v2",
            [],
        )?;
    }

    if table_exists_tx(tx, "comments")? && has_column_tx(tx, "comments", "symbol_id")? {
        tx.execute("ALTER TABLE comments RENAME TO comments_old_v2", [])?;
    }
    tx.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol_hash TEXT NOT NULL,
            comment_type TEXT DEFAULT 'doc',
            content TEXT DEFAULT '',
            created_at REAL NOT NULL,
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        );
        "#,
    )?;
    if table_exists_tx(tx, "comments_old_v2")? {
        tx.execute(
            "INSERT OR IGNORE INTO comments(id, symbol_hash, comment_type, content, created_at)
             SELECT c.id, s.symbol_hash, COALESCE(c.comment_type, 'doc'),
                    COALESCE(c.content, ''), COALESCE(c.created_at, 0)
             FROM comments_old_v2 c
             JOIN legacy_symbol_map sm ON sm.old_id=c.symbol_id
             JOIN symbols s ON s.id=sm.new_id",
            [],
        )?;
    }

    let mut legacy_calls = false;
    if table_exists_tx(tx, "calls")?
        && (!has_column_tx(tx, "calls", "callee_id")? || legacy_symbols)
    {
        tx.execute("ALTER TABLE calls RENAME TO calls_old_v2", [])?;
        legacy_calls = true;
    }
    tx.execute_batch(
        r#"
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
        "#,
    )?;
    if legacy_calls {
        tx.execute(
            "INSERT OR IGNORE INTO calls
             (id, caller_id, caller_name, caller_module, callee_name, callee_module,
              callee_qualified, callee_file, callee_id, call_line, is_cross_file)
             SELECT c.id, sm.new_id, COALESCE(c.caller_name, ''),
                    COALESCE(c.caller_module, ''), COALESCE(c.callee_name, ''),
                    COALESCE(c.callee_module, ''), COALESCE(c.callee_qualified, ''),
                    COALESCE(c.callee_file, ''), COALESCE(sc.id, 0),
                    COALESCE(c.call_line, 0), COALESCE(c.is_cross_file, 0)
             FROM calls_old_v2 c
             JOIN legacy_symbol_map sm ON sm.old_id=c.caller_id
             LEFT JOIN symbols sc ON sc.qualified_name=c.callee_qualified",
            [],
        )?;
    }

    let mut legacy_versions = false;
    if table_exists_tx(tx, "file_versions")?
        && !has_column_tx(tx, "file_versions", "file_instance_id")?
    {
        tx.execute(
            "ALTER TABLE file_versions RENAME TO file_versions_old_v2",
            [],
        )?;
        legacy_versions = true;
    }
    tx.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS file_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            version_num INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            mtime REAL NOT NULL,
            total_lines INTEGER DEFAULT 0,
            parsed_at REAL NOT NULL,
            is_current INTEGER DEFAULT 1,
            is_deleted INTEGER DEFAULT 0,
            commit_hash TEXT DEFAULT '',
            ast_cache BLOB DEFAULT NULL,
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
            FOREIGN KEY (content_hash) REFERENCES file_contents(content_hash)
        );
        "#,
    )?;
    if legacy_versions {
        tx.execute(
            "INSERT OR IGNORE INTO file_contents(content_hash, language, total_lines, first_seen_at)
             SELECT DISTINCT content_hash, '', COALESCE(total_lines, 0), COALESCE(parsed_at, 0)
             FROM file_versions_old_v2 WHERE content_hash IS NOT NULL",
            [],
        )?;
        tx.execute(
            "INSERT OR IGNORE INTO file_versions
             (id, file_instance_id, version_num, content_hash, mtime, total_lines,
              parsed_at, is_current, is_deleted)
             SELECT fv.id, fm.new_id, fv.version_num, fv.content_hash,
                    COALESCE(fv.mtime, 0), COALESCE(fv.total_lines, 0),
                    COALESCE(fv.parsed_at, 0), COALESCE(fv.is_current, 1), 0
             FROM file_versions_old_v2 fv
             JOIN legacy_file_map fm ON fm.old_id=fv.file_id",
            [],
        )?;
        tx.execute(
            "INSERT OR REPLACE INTO legacy_file_version_map(old_id, new_id)
             SELECT id, id FROM file_versions_old_v2",
            [],
        )?;
        tx.execute(
            "UPDATE file_instances
             SET current_content_hash=COALESCE((SELECT fv.content_hash FROM file_versions fv
                 WHERE fv.file_instance_id=file_instances.id AND fv.is_current=1 LIMIT 1),
                 current_content_hash),
                 total_lines=COALESCE((SELECT fv.total_lines FROM file_versions fv
                 WHERE fv.file_instance_id=file_instances.id AND fv.is_current=1 LIMIT 1),
                 total_lines),
                 last_parsed=COALESCE((SELECT fv.parsed_at FROM file_versions fv
                 WHERE fv.file_instance_id=file_instances.id AND fv.is_current=1 LIMIT 1),
                 last_parsed)",
            [],
        )?;
    }

    let mut legacy_fsv = false;
    if table_exists_tx(tx, "file_symbol_versions")?
        && !has_column_tx(tx, "file_symbol_versions", "is_deleted")?
    {
        tx.execute(
            "ALTER TABLE file_symbol_versions RENAME TO file_symbol_versions_old_v2",
            [],
        )?;
        legacy_fsv = true;
    }
    tx.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS file_symbol_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_version_id INTEGER NOT NULL,
            symbol_hash TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL,
            module_path TEXT DEFAULT '',
            depth INTEGER DEFAULT -1,
            is_deleted INTEGER DEFAULT 0,
            FOREIGN KEY (file_version_id) REFERENCES file_versions(id),
            FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
        );
        "#,
    )?;
    if legacy_fsv {
        tx.execute(
            "INSERT OR IGNORE INTO symbol_contents
             (content_hash, name, kind, content, qualified_name)
             SELECT COALESCE(NULLIF(symbol_hash, ''), 'legacy-fsv:' || id),
                    qualified_name, 'unknown', '', qualified_name
             FROM file_symbol_versions_old_v2",
            [],
        )?;
        tx.execute(
            "INSERT OR IGNORE INTO file_symbol_versions
             (id, file_version_id, symbol_hash, qualified_name, start_line, end_line,
              module_path, depth, is_deleted)
             SELECT fsv.id, fm.new_id,
                    COALESCE(NULLIF(fsv.symbol_hash, ''), 'legacy-fsv:' || fsv.id),
                    fsv.qualified_name, fsv.start_line, fsv.end_line,
                    COALESCE(fsv.module_path, ''), COALESCE(fsv.depth, -1), 0
             FROM file_symbol_versions_old_v2 fsv
             JOIN legacy_file_version_map fm ON fm.old_id=fsv.file_version_id",
            [],
        )?;
    }

    if table_exists_tx(tx, "semgrep_findings")? && has_column_tx(tx, "semgrep_findings", "file_id")?
    {
        tx.execute(
            "ALTER TABLE semgrep_findings RENAME TO semgrep_findings_old_v2",
            [],
        )?;
    }
    tx.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS semgrep_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            content_hash TEXT DEFAULT '',
            rule_id TEXT NOT NULL,
            rule_name TEXT DEFAULT '',
            message TEXT DEFAULT '',
            severity TEXT DEFAULT 'INFO',
            confidence TEXT DEFAULT 'UNKNOWN',
            language TEXT DEFAULT '',
            start_line INTEGER DEFAULT 0,
            end_line INTEGER DEFAULT 0,
            snippet TEXT DEFAULT '',
            fix TEXT DEFAULT '',
            symbol_id INTEGER DEFAULT 0,
            symbol_qualified TEXT DEFAULT '',
            scanned_at REAL DEFAULT 0,
            scan_id INTEGER DEFAULT 0,
            FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
            UNIQUE(content_hash, rule_id, start_line)
        );
        "#,
    )?;
    if table_exists_tx(tx, "semgrep_findings_old_v2")? {
        tx.execute(
            "INSERT OR IGNORE INTO semgrep_findings
             (id, file_instance_id, content_hash, rule_id, rule_name, message, severity,
              confidence, language, start_line, end_line, snippet, fix, symbol_id,
              symbol_qualified, scanned_at)
             SELECT sf.id, fm.new_id, '', sf.rule_id, COALESCE(sf.rule_name, ''),
                    COALESCE(sf.message, ''), COALESCE(sf.severity, 'INFO'),
                    COALESCE(sf.confidence, 'UNKNOWN'), COALESCE(sf.language, ''),
                    COALESCE(sf.start_line, 0), COALESCE(sf.end_line, 0),
                    COALESCE(sf.snippet, ''), COALESCE(sf.fix, ''),
                    COALESCE(sf.symbol_id, 0), COALESCE(sf.symbol_qualified, ''),
                    COALESCE(sf.scanned_at, 0)
             FROM semgrep_findings_old_v2 sf
             JOIN legacy_file_map fm ON fm.old_id=sf.file_id",
            [],
        )?;
    }

    if table_exists_tx(tx, "semgrep_scans")? && !has_column_tx(tx, "semgrep_scans", "workspace_id")?
    {
        tx.execute(
            "ALTER TABLE semgrep_scans RENAME TO semgrep_scans_old_v2",
            [],
        )?;
    }
    tx.execute_batch(
        r#"
        CREATE TABLE IF NOT EXISTS semgrep_scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_type TEXT DEFAULT 'full',
            config TEXT DEFAULT '',
            workspace_id INTEGER DEFAULT 0,
            started_at REAL NOT NULL,
            completed_at REAL DEFAULT 0,
            total_findings INTEGER DEFAULT 0,
            files_scanned INTEGER DEFAULT 0,
            status TEXT DEFAULT 'running'
        );
        "#,
    )?;
    if table_exists_tx(tx, "semgrep_scans_old_v2")? {
        tx.execute(
            "INSERT OR IGNORE INTO semgrep_scans
             (id, scan_type, config, workspace_id, started_at, completed_at,
              total_findings, files_scanned, status)
             SELECT id, COALESCE(scan_type, 'full'), COALESCE(config, ''), 1,
                    started_at, COALESCE(completed_at, 0), COALESCE(total_findings, 0),
                    0, COALESCE(status, 'running')
             FROM semgrep_scans_old_v2",
            [],
        )?;
    }

    for old_table in [
        "comments_old_v2",
        "calls_old_v2",
        "file_symbol_versions_old_v2",
        "file_versions_old_v2",
        "semgrep_findings_old_v2",
        "semgrep_scans_old_v2",
        "symbols_old_v2",
        "file_instances_old_v2",
        "files",
    ] {
        if table_exists_tx(tx, old_table)? {
            tx.execute(&format!("DROP TABLE {old_table}"), [])?;
        }
    }
    tx.execute_batch(
        "DROP TABLE IF EXISTS temp.legacy_file_map;
         DROP TABLE IF EXISTS temp.legacy_symbol_map;
         DROP TABLE IF EXISTS temp.legacy_file_version_map;",
    )?;
    Ok(())
}

/// 将 v1/v2 的 path/file_id 模型完整转换为 v3 的 workspace/file_instance 模型。
/// 迁移前先判断形状，迁移过程使用独占事务，避免只完成半套转换。
fn migrate_legacy_v1_v3_if_needed(conn: &mut Connection) -> Result<(), rusqlite::Error> {
    let needs_migration = table_exists(conn, "files")?
        || (table_exists(conn, "file_instances")?
            && !has_column(conn, "file_instances", "workspace_id")?)
        || (table_exists(conn, "symbols")? && !has_column(conn, "symbols", "file_instance_id")?)
        || (table_exists(conn, "file_versions")?
            && !has_column(conn, "file_versions", "file_instance_id")?);
    if !needs_migration {
        return Ok(());
    }
    let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
    migrate_legacy_v1_v3_tx(&tx)?;
    tx.commit()
}

/// 执行从 0 到 SCHEMA_VERSION 的初始化或迁移
pub fn initialize_or_migrate<P: AsRef<Path>>(
    path: P,
    expected_version: u32,
) -> Result<u32, String> {
    let db_path = path.as_ref();

    if let Some(parent) = db_path.parent() {
        if !parent.exists() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("DB_OPEN_FAILED: Failed to create directory: {}", e))?;
        }
    }

    let mut conn = open_connection(db_path).map_err(|e| format!("{}", sqlite_error_message(&e)))?;

    migrate_legacy_v1_v3_if_needed(&mut conn).map_err(|e| {
        format!(
            "MIGRATION_FAILED: Failed to migrate legacy v1-v3 tables: {}",
            e
        )
    })?;

    let current_version = schema_version_from_connection(&conn)
        .map_err(|e| format!("DB_OPEN_FAILED: Failed to query schema version: {}", e))?;

    if current_version > expected_version {
        return Err(format!(
            "SCHEMA_TOO_NEW: Current schema version {} is greater than expected version {}",
            current_version, expected_version
        ));
    }

    let checksum = canonical_schema_checksum()?;
    let metadata_current = if table_exists(&conn, "schema_migrations")
        .map_err(|e| format!("DB_OPEN_FAILED: Failed to inspect schema metadata: {}", e))?
    {
        conn.query_row(
            "SELECT checksum FROM schema_migrations WHERE version=?1",
            [expected_version],
            |row| row.get::<_, String>(0),
        )
        .ok()
    } else {
        None
    };

    if current_version > 0 && (current_version < expected_version || metadata_current.is_none()) {
        if let Some(reason) = legacy_shape_migration_required(&conn)
            .map_err(|e| format!("DB_OPEN_FAILED: Failed to inspect legacy schema: {}", e))?
        {
            return Err(format!(
                "MIGRATION_FAILED: {}. Rust StorageService refuses to stamp v{} without a lossless migration",
                reason, expected_version
            ));
        }
    }

    if current_version == expected_version {
        // 复审 P0-1：即使版本号已达成，也必须校验列级 compat。
        // 陈旧二进制可能把缺列库打标为当前版本，只校验表名会静默放行。
        let missing_columns = missing_compat_columns(&conn)
            .map_err(|e| format!("MIGRATION_FAILED: Failed to inspect compat columns: {e}"))?;
        if !missing_columns.is_empty() {
            // 缺列 → 不能静默接受，落入下方迁移路径（apply_*_compat 补齐列）。
            // 此时忽略 checksum 状态，强制走迁移事务。
            // 注意：项目未引入 tracing/log 依赖，此处用 eprintln! 保持可见性
            //（与 daemon 二进制日志风格一致），仅在"版本已达成但缺列"的
            // 陈旧打标异常场景触发，属低频防御性路径。
            eprintln!(
                "callwarden-core: schema v{expected_version} stamp predates compat columns \
                 {missing_columns:?}; forcing repair migration"
            );
        } else {
            match metadata_current.as_deref() {
                Some(value) if value == checksum => return Ok(current_version),
                Some(_value) => {
                    // 策略 A（T-1785831377544-d99b57de）：stored checksum 过期
                    // （例如 8/1 旧 pyd 在 a8580e9 前写入的 v46 记录），但 DB 实际
                    // 表结构可能已与 canonical schema 一致（Python 侧 v43-v46 迁移
                    // 已应用）。此时不再仅凭 checksum fail-closed，而是校验表结构
                    // 完整性：全部 canonical 表存在即视为 schema 一致，接受并重写
                    // stored checksum 为当前 canonical；存在缺失表（真正的 schema
                    // 漂移）才 fail-closed 阻断。
                    let shape_ok = schema_shape_matches_canonical(&conn).map_err(|e| {
                        format!(
                            "MIGRATION_FAILED: Failed to inspect schema shape for v{}: {}",
                            expected_version, e
                        )
                    })?;
                    if shape_ok {
                        update_stored_checksum_to_canonical(&mut conn, expected_version).map_err(
                            |e| {
                                format!(
                                    "MIGRATION_FAILED: Failed to reconcile schema checksum for v{}: {}",
                                    expected_version, e
                                )
                            },
                        )?;
                        return Ok(current_version);
                    }
                    return Err(format!(
                        "MIGRATION_FAILED: schema checksum mismatch for v{}: stored={}, binary={}, \
                         and DB schema shape differs from canonical (missing tables)",
                        expected_version,
                        metadata_current.unwrap_or_default(),
                        checksum
                    ));
                }
                None => {}
            }
        }
    }

    // Existing databases are checked and copied before any schema write.  A
    // failed checkpoint/backup is fatal: proceeding would make recovery
    // depend on a possibly stale WAL sidecar.
    if current_version > 0 {
        let check = integrity_check(db_path)?;
        if check.iter().any(|item| item != "ok") {
            return Err(format!("INTEGRITY_FAILED: {}", check.join("; ")));
        }
        let backup_path = db_path.with_extension("pre-migration.bak");
        // Backup 只复制主库文件；TRUNCATE 成功后才允许复制，避免遗漏 WAL。
        storage_checkpoint(db_path, "TRUNCATE")?;
        backup_before_migration(db_path, &backup_path)?;
    }

    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|e| format!("MIGRATION_FAILED: Failed to begin transaction: {}", e))?;

    tx.execute_batch(STORAGE_METADATA_SQL)
        .map_err(|e| format!("MIGRATION_FAILED: Failed to create storage metadata: {}", e))?;
    // v48/v49（W2.3 P1-1）：既有库的 guardrail_findings 可能缺少 workspace_id 归属列
    //（陈旧二进制打标）。**必须先补列，再执行 canonical SCHEMA_SQL**——否则 SCHEMA_SQL
    // 里的 CREATE INDEX idx_guardrail_findings_workspace ON guardrail_findings(workspace_id)
    // 会因列不存在而报 no such column，整个迁移事务失败。CREATE TABLE IF NOT EXISTS
    // 对已存在的表不加列，必须显式 ALTER TABLE 补齐；随后把 workspace_id=0 的 open
    // 旧行置为 orphaned（fail-closed，不静默归入当前 workspace）。
    // 全新库时 guardrail_findings 表尚不存在，compat 函数检测到表不存在会跳过，无副作用。
    apply_guardrail_findings_v48_compat(&tx).map_err(|e| {
        format!("MIGRATION_FAILED: Failed to apply guardrail_findings v48 compat: {}", e)
    })?;
    // v50（Agent-task-contract M1）：既有库的 agent_registrations 可能缺少
    // Agent Identity 身份列（陈旧二进制打标）。**必须先补列，再执行 canonical
    // SCHEMA_SQL**——否则 SCHEMA_SQL 里的 CREATE INDEX idx_agent_registrations_instance
    // ON agent_registrations(agent_instance_id, role, status) 会因列不存在而报
    // no such column。全新库时表由 SCHEMA_SQL 建齐，compat 函数探测到列已存在
    // 会跳过，无副作用。
    apply_agent_registrations_v50_compat(&tx).map_err(|e| {
        format!("MIGRATION_FAILED: Failed to apply agent_registrations v50 compat: {}", e)
    })?;
    // v57/v58（1E，T-1787203926824-9f873bfc）：既有库的 task_contract_revisions /
    // task_verdict_events / task_gate_decisions 可能缺少 provenance 与 normalization
    // 绑定列（v57 发布后 schema.py 补列但未 bump 版本号导致）。**必须先补列，再执行
    // canonical SCHEMA_SQL**——与 v48/v50 compat 同一模式；ALTER TABLE 幂等，
    // 只加列不动数据，全新库时表尚不存在会跳过，无副作用。
    apply_task_contract_v57_compat(&tx).map_err(|e| {
        format!("MIGRATION_FAILED: Failed to apply task contract v57 compat: {}", e)
    })?;
    tx.execute_batch(&canonical_schema_sql()?)
        .map_err(|e| format!("SCHEMA_CREATE_FAILED: Failed to execute SCHEMA_SQL: {}", e))?;
    // v52（1D1）：operation_params 初始 rule row 幂等播种（与 sqlite_query 同一真相源）。
    // 幂等：重复迁移不重复播种；已存在但 payload/hash 失配则迁移失败回滚（fail closed）。
    seed_operation_params_rule(&tx)
        .map_err(|e| format!("MIGRATION_FAILED: Failed to seed operation_params rule row: {}", e))?;
    // v55（1B）：role_contract 初始 rule row 幂等播种（与 sqlite_query 同一真相源）。
    // 幂等：重复迁移不重复播种；已存在但 payload/hash 失配则迁移失败回滚（fail closed）。
    seed_role_contract_rule(&tx)
        .map_err(|e| format!("MIGRATION_FAILED: Failed to seed role_contract rule row: {}", e))?;
    // v55（1B）：历史 role_contracts → lineage/revision 回填（歧义组跳过，保留旧行）。
    migrate_role_contract_legacy(&tx)
        .map_err(|e| format!("MIGRATION_FAILED: Failed to backfill role_contract legacy: {}", e))?;
    // v57（1E）：verdict-normalization/v1 初始 rule row 幂等播种（与 sqlite_query 同一真相源）。
    // 幂等：重复迁移不重复播种；已存在但 payload/hash 失配则迁移失败回滚（fail closed）。
    seed_verdict_normalization_rule(&tx)
        .map_err(|e| format!("MIGRATION_FAILED: Failed to seed verdict-normalization rule row: {}", e))?;
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|value| value.as_secs_f64())
        .unwrap_or(0.0);
    tx.execute(
        "INSERT OR REPLACE INTO schema_version (version, applied_at, description)
         VALUES (?1, ?2, ?3)",
        rusqlite::params![expected_version, now, "Rust StorageService schema"],
    )
    .map_err(|e| format!("MIGRATION_FAILED: Failed to publish schema version: {}", e))?;
    tx.execute(
        "INSERT OR REPLACE INTO schema_migrations (version, checksum, description, applied_at)
         VALUES (?1, ?2, ?3, ?4)",
        rusqlite::params![expected_version, checksum, "canonical db/schema.py", now],
    )
    .map_err(|e| format!("MIGRATION_FAILED: Failed to publish schema checksum: {}", e))?;
    tx.pragma_update(None, "user_version", expected_version)
        .map_err(|e| format!("MIGRATION_FAILED: Failed to update user_version: {}", e))?;

    tx.commit().map_err(|e| {
        format!(
            "MIGRATION_FAILED: Failed to commit migration transaction: {}",
            e
        )
    })?;

    Ok(expected_version)
}

/// 数据库完整性检查 (PRAGMA integrity_check)
pub fn integrity_check<P: AsRef<Path>>(path: P) -> Result<Vec<String>, String> {
    if !path.as_ref().exists() {
        return Ok(vec!["ok".to_string()]);
    }

    let conn = open_connection(path).map_err(|e| format!("INTEGRITY_FAILED: {}", e))?;
    let mut stmt = conn
        .prepare("PRAGMA integrity_check")
        .map_err(|e| format!("INTEGRITY_FAILED: {}", e))?;

    let rows = stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(|e| format!("INTEGRITY_FAILED: {}", e))?;

    let mut results = Vec::new();
    for res in rows {
        results.push(res.map_err(|e| format!("INTEGRITY_FAILED: {}", e))?);
    }

    Ok(results)
}

/// 迁移前灾备备份
pub fn backup_before_migration<P: AsRef<Path>, Q: AsRef<Path>>(
    src: P,
    dst: Q,
) -> Result<u64, String> {
    if !src.as_ref().exists() {
        return Ok(0);
    }

    if let Some(parent) = dst.as_ref().parent() {
        if !parent.exists() {
            fs::create_dir_all(parent)
                .map_err(|e| format!("BACKUP_FAILED: Failed to create backup directory: {}", e))?;
        }
    }

    let bytes_copied = fs::copy(src.as_ref(), dst.as_ref())
        .map_err(|e| format!("DB_OPEN_FAILED: Failed to copy database file: {}", e))?;

    Ok(bytes_copied)
}

/// WAL Checkpoint 操作
pub fn storage_checkpoint<P: AsRef<Path>>(path: P, mode: &str) -> Result<String, String> {
    if !path.as_ref().exists() {
        return Ok("ok".to_string());
    }

    let conn = open_connection(path).map_err(|e| format!("CHECKPOINT_FAILED: {}", e))?;
    let sql_mode = match mode.to_uppercase().as_str() {
        "PASSIVE" => "PASSIVE",
        "FULL" => "FULL",
        "RESTART" => "RESTART",
        "TRUNCATE" => "TRUNCATE",
        _ => return Err(format!("CHECKPOINT_FAILED: unsupported mode {}", mode)),
    };

    conn.execute_batch(&format!("PRAGMA wal_checkpoint({});", sql_mode))
        .map_err(|e| format!("CHECKPOINT_FAILED: {}", e))?;

    Ok("ok".to_string())
}

// ===========================================================================
// PyO3 暴露层
// ===========================================================================

fn storage_py_error(message: String) -> PyErr {
    if message.starts_with("SCHEMA_TOO_NEW") {
        PyValueError::new_err(message)
    } else if message.starts_with("DB_LOCKED") || message.contains("database is locked") {
        PyOSError::new_err(format!("DB_LOCKED: {}", message))
    } else if message.starts_with("DB_OPEN_FAILED") {
        PyIOError::new_err(message)
    } else {
        PyRuntimeError::new_err(message)
    }
}

fn sqlite_error_message(error: &rusqlite::Error) -> String {
    match error {
        rusqlite::Error::SqliteFailure(code, _) => match code.code {
            ErrorCode::DatabaseBusy | ErrorCode::DatabaseLocked => {
                format!("DB_LOCKED: {}", error)
            }
            _ => error.to_string(),
        },
        _ => error.to_string(),
    }
}

#[pyclass(unsendable)]
pub struct StorageHandle {
    path: String,
    conn: Option<Connection>,
}

#[pymethods]
impl StorageHandle {
    #[getter]
    fn path(&self) -> &str {
        &self.path
    }

    fn close(&mut self) {
        self.conn.take();
    }

    fn pragma(&self, name: &str) -> PyResult<String> {
        let conn = self
            .conn
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("DB_OPEN_FAILED: handle is closed"))?;
        let sql = format!("PRAGMA {}", name);
        conn.query_row(&sql, [], |row| {
            row.get::<_, String>(0)
                .or_else(|_| row.get::<_, i64>(0).map(|value| value.to_string()))
        })
        .map_err(|error| storage_py_error(format!("{}", sqlite_error_message(&error))))
    }
}

#[pyclass(unsendable)]
pub struct StorageTransaction {
    conn: Option<Connection>,
    kind: String,
}

#[pymethods]
impl StorageTransaction {
    #[getter]
    fn kind(&self) -> &str {
        &self.kind
    }

    fn execute(&self, sql: &str) -> PyResult<usize> {
        let conn = self
            .conn
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("TX_ABORTED: transaction is closed"))?;
        conn.execute(sql, [])
            .map_err(|error| storage_py_error(format!("TX_ABORTED: {}", error)))
    }

    fn commit(&mut self) -> PyResult<()> {
        let conn = self
            .conn
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("TX_ABORTED: transaction is closed"))?;
        conn.execute_batch("COMMIT")
            .map_err(|error| storage_py_error(format!("TX_ABORTED: commit failed: {}", error)))
    }

    fn rollback(&mut self) -> PyResult<()> {
        let conn = self
            .conn
            .take()
            .ok_or_else(|| PyRuntimeError::new_err("TX_ABORTED: transaction is closed"))?;
        conn.execute_batch("ROLLBACK")
            .map_err(|error| storage_py_error(format!("TX_ABORTED: rollback failed: {}", error)))
    }
}

#[pyfunction]
pub fn storage_open(py: Python<'_>, path: &str, mode: &str) -> PyResult<Py<StorageHandle>> {
    if path.is_empty() {
        return Err(PyValueError::new_err("DB_OPEN_FAILED: db_path 不能为空"));
    }
    let flags = match mode.to_ascii_lowercase().as_str() {
        "readonly" | "read_only" => OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
        "readwrite" | "read_write" => {
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_URI
        }
        "create" | "readwrite_create" => {
            OpenFlags::SQLITE_OPEN_READ_WRITE
                | OpenFlags::SQLITE_OPEN_CREATE
                | OpenFlags::SQLITE_OPEN_URI
        }
        _ => {
            return Err(PyValueError::new_err(
                "DB_OPEN_FAILED: mode must be readonly, readwrite, or create",
            ))
        }
    };
    let conn = Connection::open_with_flags(path, flags)
        .map_err(|error| storage_py_error(format!("{}", sqlite_error_message(&error))))?;
    conn.busy_timeout(Duration::from_millis(5000))
        .map_err(|error| storage_py_error(format!("{}", sqlite_error_message(&error))))?;
    if mode.to_ascii_lowercase() != "readonly" && mode.to_ascii_lowercase() != "read_only" {
        conn.pragma_update(None, "journal_mode", "WAL")
            .map_err(|error| storage_py_error(format!("{}", sqlite_error_message(&error))))?;
        conn.pragma_update(None, "synchronous", "NORMAL")
            .map_err(|error| storage_py_error(format!("{}", sqlite_error_message(&error))))?;
        conn.pragma_update(None, "foreign_keys", "ON")
            .map_err(|error| storage_py_error(format!("{}", sqlite_error_message(&error))))?;
    }
    Py::new(
        py,
        StorageHandle {
            path: path.to_string(),
            conn: Some(conn),
        },
    )
}

#[pyfunction]
pub fn storage_begin(py: Python<'_>, path: &str, kind: &str) -> PyResult<Py<StorageTransaction>> {
    let conn = open_connection(path)
        .map_err(|error| storage_py_error(format!("{}", sqlite_error_message(&error))))?;
    let behavior = match kind.to_ascii_lowercase().as_str() {
        "immediate" | "write" => "BEGIN IMMEDIATE",
        "deferred" | "read" => "BEGIN",
        "exclusive" => "BEGIN EXCLUSIVE",
        _ => {
            return Err(PyValueError::new_err(
                "TX_ABORTED: kind must be immediate, deferred, or exclusive",
            ))
        }
    };
    conn.execute_batch(behavior)
        .map_err(|error| storage_py_error(format!("TX_ABORTED: {}", error)))?;
    Py::new(
        py,
        StorageTransaction {
            conn: Some(conn),
            kind: kind.to_string(),
        },
    )
}

#[pyfunction]
pub fn storage_commit(mut tx: PyRefMut<'_, StorageTransaction>) -> PyResult<()> {
    tx.commit()
}

#[pyfunction]
pub fn storage_rollback(mut tx: PyRefMut<'_, StorageTransaction>) -> PyResult<()> {
    tx.rollback()
}

#[pyfunction]
pub fn storage_schema_version(path: &str) -> PyResult<u32> {
    get_schema_version(path).map_err(|e| storage_py_error(format!("DB_OPEN_FAILED: {}", e)))
}

#[pyfunction]
pub fn storage_initialize_or_migrate<'py>(
    py: Python<'py>,
    path: &str,
    expected_version: u32,
) -> PyResult<Bound<'py, PyDict>> {
    match initialize_or_migrate(path, expected_version) {
        Ok(v) => {
            let dict = PyDict::new(py);
            dict.set_item("success", true)?;
            dict.set_item("version", v)?;
            dict.set_item("error", "")?;
            Ok(dict)
        }
        Err(err_msg) => {
            if err_msg.starts_with("SCHEMA_TOO_NEW") {
                Err(storage_py_error(err_msg))
            } else {
                Err(storage_py_error(err_msg))
            }
        }
    }
}

#[pyfunction]
pub fn storage_integrity_check(path: &str) -> PyResult<Vec<String>> {
    integrity_check(path).map_err(storage_py_error)
}

#[pyfunction]
pub fn storage_backup_before_migration(src: &str, dst: &str) -> PyResult<u64> {
    backup_before_migration(src, dst).map_err(storage_py_error)
}

#[pyfunction]
pub fn storage_checkpoint_py(path: &str, mode: &str) -> PyResult<String> {
    storage_checkpoint(path, mode).map_err(storage_py_error)
}

// ===========================================================================
// Rust 单元测试
// ===========================================================================

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::tempdir;

    #[test]
    fn test_storage_init_new_db() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("test_new.db");

        assert_eq!(get_schema_version(&db_path).unwrap(), 0);

        let ver = initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap();
        assert_eq!(ver, SCHEMA_VERSION);
        assert_eq!(get_schema_version(&db_path).unwrap(), SCHEMA_VERSION);

        let status = integrity_check(&db_path).unwrap();
        assert_eq!(status, vec!["ok".to_string()]);

        let conn = open_connection(&db_path).unwrap();
        let metadata: String = conn
            .query_row(
                "SELECT checksum FROM schema_migrations WHERE version=?1",
                [SCHEMA_VERSION],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(metadata, canonical_schema_checksum().unwrap());
        let table_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(
            table_count >= 40,
            "canonical schema was truncated: {table_count}"
        );
    }

    #[test]
    fn test_storage_schema_too_new() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("test_future.db");

        let conn = open_connection(&db_path).unwrap();
        conn.pragma_update(None, "user_version", 999).unwrap();
        drop(conn);

        let err = initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap_err();
        assert!(err.contains("SCHEMA_TOO_NEW"));
    }

    #[test]
    fn test_storage_backup_and_checkpoint() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("test_src.db");
        let backup_path = dir.path().join("test_dst.db");

        initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap();

        let bytes = backup_before_migration(&db_path, &backup_path).unwrap();
        assert!(bytes > 0);
        assert!(backup_path.exists());

        let res = storage_checkpoint(&db_path, "PASSIVE").unwrap();
        assert_eq!(res, "ok");
    }

    #[test]
    fn test_legacy_user_version_is_repaired_into_metadata() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("legacy.db");
        let conn = open_connection(&db_path).unwrap();
        conn.pragma_update(None, "user_version", SCHEMA_VERSION)
            .unwrap();
        drop(conn);

        initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap();
        let conn = open_connection(&db_path).unwrap();
        let version: u32 = conn
            .query_row("SELECT MAX(version) FROM schema_version", [], |row| {
                row.get(0)
            })
            .unwrap();
        assert_eq!(version, SCHEMA_VERSION);
        let workspaces: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE name='workspaces'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(workspaces, 1);
    }

    #[test]
    fn test_schema_checksum_mismatch_accepts_when_shape_complete() {
        // 策略 A（T-1785831377544-d99b57de）：stored checksum 过期但 DB 表结构
        // 完整（所有 canonical 表存在）时，应接受并重写 checksum，不再 fail-closed。
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("checksum.db");
        initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap();
        let conn = open_connection(&db_path).unwrap();
        conn.execute(
            "UPDATE schema_migrations SET checksum='tampered' WHERE version=?1",
            [SCHEMA_VERSION],
        )
        .unwrap();
        drop(conn);

        // 表结构完整 + checksum 过期 → 应成功，且 checksum 被重写为当前 canonical
        let ver = initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap();
        assert_eq!(ver, SCHEMA_VERSION);
        let conn = open_connection(&db_path).unwrap();
        let stored: String = conn
            .query_row(
                "SELECT checksum FROM schema_migrations WHERE version=?1",
                [SCHEMA_VERSION],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(stored, canonical_schema_checksum().unwrap());
    }

    #[test]
    fn test_schema_checksum_mismatch_fails_when_table_missing() {
        // 表结构不完整（删掉 canonical 表）→ 即使版本号一致也应 fail-closed，
        // 保留对真正 schema 漂移的阻断。
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("checksum_missing.db");
        initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap();
        let conn = open_connection(&db_path).unwrap();
        // 删掉一个 canonical 表模拟真正的 schema 漂移
        conn.execute_batch("DROP TABLE IF EXISTS task_dependencies;").unwrap();
        conn.execute(
            "UPDATE schema_migrations SET checksum='tampered' WHERE version=?1",
            [SCHEMA_VERSION],
        )
        .unwrap();
        drop(conn);

        let err = initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap_err();
        assert!(
            err.contains("schema checksum mismatch") || err.contains("missing tables"),
            "expected fail-closed on shape mismatch, got: {}",
            err
        );
    }

    #[test]
    fn test_version_matches_but_compat_column_missing_repairs() {
        // 复审 P0-1：陈旧二进制可能把"缺 workspace_id 列"的库打标为当前版本，
        // 且 checksum 与 canonical 完全一致（陈旧二进制内嵌 schema 恰好是它自己
        // 写入的）。旧代码在 checksum 匹配分支直接 return Ok，永不补列。本测试
        // 验证新代码在 current_version==expected_version 且 checksum 匹配时仍
        // 校验 compat 列，缺列则强制走迁移事务补齐并 orphan 旧 open 行。
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("stale_stamp.db");
        let conn = open_connection(&db_path).unwrap();
        // 手工构造"陈旧二进制打标"状态：schema_version=SCHEMA_VERSION、
        // schema_migrations checksum=当前 canonical、guardrail_findings 无 workspace_id
        conn.execute_batch(
            &format!(
                "
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, description TEXT DEFAULT '');
                INSERT INTO schema_version VALUES ({SCHEMA_VERSION}, 1, 'stale stamp');
                CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, description TEXT NOT NULL, applied_at REAL NOT NULL);
                INSERT INTO schema_migrations VALUES ({SCHEMA_VERSION}, '{}', 'stale binary', 1);
                CREATE TABLE workspaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE NOT NULL,
                    root_path TEXT UNIQUE NOT NULL,
                    created_at REAL NOT NULL,
                    is_active INTEGER DEFAULT 0,
                    description TEXT DEFAULT '',
                    active_task_id TEXT DEFAULT ''
                );
                INSERT INTO workspaces (id, name, root_path, created_at, is_active) VALUES (1, 'ws', '/ws', 1, 1);
                CREATE TABLE guardrail_rules (
                    rule_id TEXT PRIMARY KEY,
                    category TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'warn',
                    pattern TEXT NOT NULL,
                    action TEXT NOT NULL DEFAULT 'warn',
                    description TEXT DEFAULT '',
                    is_builtin INTEGER DEFAULT 0,
                    created_at REAL NOT NULL
                );
                INSERT INTO guardrail_rules VALUES ('r1','db_safety','warn','x','warn','',1,1);
                CREATE TABLE guardrail_findings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rule_id TEXT NOT NULL,
                    file_path TEXT NOT NULL,
                    symbol_hash TEXT DEFAULT '',
                    severity TEXT NOT NULL DEFAULT 'warn',
                    status TEXT NOT NULL DEFAULT 'open',
                    message TEXT DEFAULT '',
                    detected_at REAL NOT NULL,
                    resolved_at REAL
                );
                INSERT INTO guardrail_findings (rule_id, file_path, symbol_hash, severity, status, message, detected_at)
                    VALUES ('r1', 'a.py', 'h1', 'warn', 'open', 'legacy-open', 1.0);
                ",
                canonical_schema_checksum().unwrap()
            ),
        )
        .unwrap();
        drop(conn);

        // 旧代码在此直接 return Ok(SCHEMA_VERSION)；新代码必须补列
        let ver = initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap();
        assert_eq!(ver, SCHEMA_VERSION);

        let conn = open_connection(&db_path).unwrap();
        let has_column: bool = {
            let mut stmt = conn
                .prepare("PRAGMA table_info(guardrail_findings)")
                .unwrap();
            stmt.query_map([], |row| row.get::<_, String>(1))
                .unwrap()
                .collect::<Result<Vec<_>, _>>()
                .unwrap()
                .iter()
                .any(|name| name == "workspace_id")
        };
        assert!(has_column, "workspace_id 列应被补齐");
        let index_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name IN
                 ('idx_guardrail_findings_workspace','idx_guardrail_findings_ws_file')",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(index_count, 2, "两个新索引应补齐");
        let status: String = conn
            .query_row(
                "SELECT status FROM guardrail_findings WHERE message='legacy-open'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(status, "orphaned", "无归属 open 旧行应置 orphaned");
    }

    #[test]
    fn test_v50_stale_stamp_missing_identity_columns_repaired() {
        // v50 对照场景：陈旧二进制把"缺 Agent Identity 身份列"的库打标为
        // SCHEMA_VERSION。missing_compat_columns 必须报告缺列并强制走迁移事务，
        // apply_agent_registrations_v50_compat 补齐 9 个身份列后 SCHEMA_SQL 的
        // idx_agent_registrations_instance 才能建立。
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("stale_stamp_v50.db");
        let conn = open_connection(&db_path).unwrap();
        conn.execute_batch(
            &format!(
                "
                CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL NOT NULL, description TEXT DEFAULT '');
                INSERT INTO schema_version VALUES ({SCHEMA_VERSION}, 1, 'stale stamp');
                CREATE TABLE schema_migrations (version INTEGER PRIMARY KEY, checksum TEXT NOT NULL, description TEXT NOT NULL, applied_at REAL NOT NULL);
                INSERT INTO schema_migrations VALUES ({SCHEMA_VERSION}, '{}', 'stale binary', 1);
                CREATE TABLE agent_registrations (
                    agent_id TEXT PRIMARY KEY,
                    agent_name TEXT NOT NULL,
                    owner_key TEXT NOT NULL,
                    capabilities TEXT DEFAULT '[]',
                    registered_at REAL NOT NULL,
                    last_heartbeat REAL NOT NULL,
                    status TEXT DEFAULT 'active'
                );
                INSERT INTO agent_registrations (agent_id, agent_name, owner_key, registered_at, last_heartbeat)
                    VALUES ('a1', 'legacy-agent', 'owner', 1.0, 1.0);
                ",
                canonical_schema_checksum().unwrap()
            ),
        )
        .unwrap();
        drop(conn);

        let ver = initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap();
        assert_eq!(ver, SCHEMA_VERSION);

        let conn = open_connection(&db_path).unwrap();
        let mut columns = conn
            .prepare("PRAGMA table_info(agent_registrations)")
            .unwrap()
            .query_map([], |row| row.get::<_, String>(1))
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        columns.sort();
        for column in [
            "agent_instance_id", "client_id", "provider", "model_id", "model_mode",
            "system_fingerprint", "runtime_hash", "session_id", "role",
        ] {
            assert!(
                columns.contains(&column.to_string()),
                "Agent Identity 列 {column} 应被补齐"
            );
        }
        let index_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name='idx_agent_registrations_instance'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(index_count, 1, "idx_agent_registrations_instance 索引应补齐");
        let agent_name: String = conn
            .query_row(
                "SELECT agent_name FROM agent_registrations WHERE agent_id='a1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(agent_name, "legacy-agent", "旧数据行应保留");
    }

    #[test]
    fn test_legacy_v1_v3_files_migration() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("legacy_v2.db");
        let conn = open_connection(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE files (id INTEGER PRIMARY KEY, path TEXT, abs_path TEXT, mtime REAL, status TEXT, module_path TEXT);\n\
             INSERT INTO files VALUES (1, 'src/main.rs', '/app/src/main.rs', 100.0, 'parsed', 'main');\n\
             PRAGMA user_version=2;",
        )
        .unwrap();
        drop(conn);

        let ver = initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap();
        assert_eq!(ver, SCHEMA_VERSION);

        let conn = open_connection(&db_path).unwrap();
        let count: i64 = conn
            .query_row("SELECT count(*) FROM file_instances", [], |r| r.get(0))
            .unwrap();
        assert_eq!(count, 1);
        let path: String = conn
            .query_row("SELECT rel_path FROM file_instances WHERE id=1", [], |r| {
                r.get(0)
            })
            .unwrap();
        assert_eq!(path, "src/main.rs");
    }

    #[test]
    fn test_legacy_v2_relation_migration_preserves_graph_and_history() {
        let dir = tempdir().unwrap();
        let db_path = dir.path().join("legacy_v2_relations.db");
        let conn = open_connection(&db_path).unwrap();
        conn.execute_batch(
            r#"
            CREATE TABLE files (
                id INTEGER PRIMARY KEY,
                path TEXT NOT NULL,
                abs_path TEXT NOT NULL,
                mtime REAL DEFAULT 0,
                status TEXT DEFAULT 'parsed',
                module_path TEXT DEFAULT ''
            );
            CREATE TABLE file_versions (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                version_num INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                mtime REAL DEFAULT 0,
                total_lines INTEGER DEFAULT 0,
                parsed_at REAL DEFAULT 0,
                is_current INTEGER DEFAULT 1
            );
            CREATE TABLE symbol_contents (
                content_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                signature TEXT DEFAULT '',
                has_comment INTEGER DEFAULT 0,
                comment_content TEXT DEFAULT '',
                qualified_name TEXT DEFAULT ''
            );
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                visibility TEXT DEFAULT 'private',
                start_line INTEGER DEFAULT 1,
                end_line INTEGER DEFAULT 1,
                start_col INTEGER DEFAULT 0,
                end_col INTEGER DEFAULT 0,
                signature TEXT DEFAULT '',
                has_comment INTEGER DEFAULT 0,
                comment_status TEXT DEFAULT 'pending',
                module_path TEXT DEFAULT '',
                qualified_name TEXT DEFAULT '',
                depth INTEGER DEFAULT -1
            );
            CREATE TABLE file_symbol_versions (
                id INTEGER PRIMARY KEY,
                file_version_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                module_path TEXT DEFAULT '',
                depth INTEGER DEFAULT -1
            );
            CREATE TABLE calls (
                id INTEGER PRIMARY KEY,
                caller_id INTEGER NOT NULL,
                caller_name TEXT NOT NULL,
                caller_module TEXT NOT NULL,
                callee_name TEXT NOT NULL,
                callee_module TEXT DEFAULT '',
                callee_qualified TEXT DEFAULT '',
                callee_file TEXT DEFAULT '',
                call_line INTEGER DEFAULT 0,
                is_cross_file INTEGER DEFAULT 0
            );
            CREATE TABLE comments (
                id INTEGER PRIMARY KEY,
                symbol_id INTEGER,
                comment_type TEXT DEFAULT 'doc',
                content TEXT DEFAULT '',
                created_at REAL DEFAULT 0
            );
            CREATE TABLE semgrep_findings (
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL,
                rule_id TEXT NOT NULL,
                rule_name TEXT DEFAULT '',
                message TEXT DEFAULT '',
                severity TEXT DEFAULT 'INFO',
                confidence TEXT DEFAULT 'UNKNOWN',
                language TEXT DEFAULT '',
                start_line INTEGER DEFAULT 0,
                end_line INTEGER DEFAULT 0,
                snippet TEXT DEFAULT '',
                fix TEXT DEFAULT '',
                symbol_id INTEGER DEFAULT 0,
                symbol_qualified TEXT DEFAULT '',
                scanned_at REAL DEFAULT 0
            );
            CREATE TABLE semgrep_scans (
                id INTEGER PRIMARY KEY,
                scan_type TEXT DEFAULT 'full',
                config TEXT DEFAULT '',
                started_at REAL NOT NULL,
                completed_at REAL DEFAULT 0,
                total_findings INTEGER DEFAULT 0,
                status TEXT DEFAULT 'running'
            );
            INSERT INTO files VALUES (1, 'src/main.rs', '/repo/src/main.rs', 100, 'parsed', 'main');
            INSERT INTO file_versions VALUES (7, 1, 1, 'file-hash-1', 100, 20, 101, 1);
            INSERT INTO symbol_contents VALUES ('sym-hash-1', 'caller', 'function', 'fn caller() {}', 'fn caller()', 0, '', 'crate::caller');
            INSERT INTO symbol_contents VALUES ('sym-hash-2', 'callee', 'function', 'fn callee() {}', 'fn callee()', 0, '', 'crate::callee');
            INSERT INTO symbols VALUES (3, 1, 'caller', 'function', 'public', 1, 3, 0, 10, 'fn caller()', 0, 'pending', 'main', 'crate::caller', 0);
            INSERT INTO symbols VALUES (4, 1, 'callee', 'function', 'private', 5, 7, 0, 10, 'fn callee()', 0, 'pending', 'main', 'crate::callee', 0);
            INSERT INTO file_symbol_versions VALUES (8, 7, 'sym-hash-1', 'crate::caller', 1, 3, 'main', 0);
            INSERT INTO file_symbol_versions VALUES (9, 7, 'sym-hash-2', 'crate::callee', 5, 7, 'main', 0);
            INSERT INTO calls VALUES (10, 3, 'caller', 'main', 'callee', 'main', 'crate::callee', 'src/main.rs', 2, 0);
            INSERT INTO comments VALUES (11, 3, 'doc', 'caller docs', 102);
            INSERT INTO semgrep_findings VALUES (12, 1, 'rust.demo', 'demo', 'demo finding', 'WARNING', 'HIGH', 'rust', 2, 2, 'callee()', '', 4, 'crate::callee', 103);
            INSERT INTO semgrep_scans VALUES (13, 'full', '{}', 100, 104, 1, 'completed');
            PRAGMA user_version = 2;
            "#,
        )
        .unwrap();
        drop(conn);

        assert_eq!(
            initialize_or_migrate(&db_path, SCHEMA_VERSION).unwrap(),
            SCHEMA_VERSION
        );

        let conn = open_connection(&db_path).unwrap();
        let file_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM file_instances", [], |row| row.get(0))
            .unwrap();
        let symbol_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM symbols", [], |row| row.get(0))
            .unwrap();
        let version_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM file_versions", [], |row| row.get(0))
            .unwrap();
        let fsv_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM file_symbol_versions", [], |row| {
                row.get(0)
            })
            .unwrap();
        let call: (i64, i64) = conn
            .query_row(
                "SELECT caller_id, callee_id FROM calls WHERE id=10",
                [],
                |row| Ok((row.get(0)?, row.get(1)?)),
            )
            .unwrap();
        let comment_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM comments WHERE symbol_hash='sym-hash-1'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        let finding_count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM semgrep_findings WHERE file_instance_id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();

        assert_eq!(file_count, 1);
        assert_eq!(symbol_count, 2);
        assert_eq!(version_count, 1);
        assert_eq!(fsv_count, 2);
        assert_eq!(call, (3, 4));
        assert_eq!(comment_count, 1);
        assert_eq!(finding_count, 1);
        assert_eq!(integrity_check(&db_path).unwrap(), vec!["ok".to_string()]);
    }
}
