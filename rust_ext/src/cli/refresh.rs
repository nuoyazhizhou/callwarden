//! Rust `cw refresh <path...>` 增量刷新写链。

use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Instant, SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

use crate::canonicalize::{canonicalize_source, sha256_hex};
use crate::daemon::cas::CasStore;
use crate::daemon::cas_merge::{merge_cas_to_codegraph_with_history, MergeHistoryMetadata};
use crate::daemon::replicator::{_daemon_parse_and_publish, detect_language_from_path};

#[derive(Debug, Clone)]
pub struct RefreshFileResult {
    pub input_path: String,
    pub success: bool,
    pub status: String,
    pub error: Option<String>,
}

#[derive(Debug, Clone)]
pub struct RefreshBatchResult {
    pub files: Vec<RefreshFileResult>,
    pub elapsed_seconds: f64,
}

impl RefreshBatchResult {
    pub fn success_count(&self) -> usize {
        self.files.iter().filter(|item| item.success).count()
    }

    pub fn failure_count(&self) -> usize {
        self.files.len().saturating_sub(self.success_count())
    }
}

/// 本地多文件增量刷新。每个文件独立提交，单文件内部保持原子性。
pub fn refresh_local_paths(
    conn: &Connection,
    db_path: &Path,
    workspace_id: i64,
    paths: &[PathBuf],
) -> Result<RefreshBatchResult, String> {
    let workspace_root: String = conn
        .query_row(
            "SELECT root_path FROM workspaces WHERE id = ?1",
            params![workspace_id],
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot query workspace root: {error}"))?;
    let workspace_root = std::fs::canonicalize(&workspace_root)
        .map_err(|error| format!("cannot resolve workspace root {}: {error}", workspace_root))?;
    let cas_path = db_path
        .parent()
        .unwrap_or_else(|| Path::new("."))
        .join("cas.db");
    let cas_store = CasStore::open(&cas_path.to_string_lossy())
        .map_err(|error| format!("cannot open local CAS {}: {error}", cas_path.display()))?;
    let commit_hash = current_git_commit(&workspace_root);
    let started = Instant::now();
    let mut files = Vec::with_capacity(paths.len());

    for path in paths {
        let input_path = path.to_string_lossy().to_string();
        let result = refresh_one_local(
            conn,
            &cas_store,
            workspace_id,
            &workspace_root,
            path,
            &commit_hash,
        );
        files.push(match result {
            Ok(status) => RefreshFileResult {
                input_path,
                success: true,
                status,
                error: None,
            },
            Err(error) => RefreshFileResult {
                input_path,
                success: false,
                status: "failed".to_string(),
                error: Some(error),
            },
        });
    }

    conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE)")
        .map_err(|error| format!("WAL checkpoint failed: {error}"))?;

    Ok(RefreshBatchResult {
        files,
        elapsed_seconds: started.elapsed().as_secs_f64(),
    })
}

fn refresh_one_local(
    conn: &Connection,
    cas_store: &CasStore,
    workspace_id: i64,
    workspace_root: &Path,
    input_path: &Path,
    commit_hash: &str,
) -> Result<String, String> {
    let candidate = if input_path.is_absolute() {
        input_path.to_path_buf()
    } else {
        std::env::current_dir()
            .map_err(|error| format!("cannot read current directory: {error}"))?
            .join(input_path)
    };
    if !candidate.exists() {
        return Ok("skipped_missing".to_string());
    }
    let abs_path = std::fs::canonicalize(&candidate)
        .map_err(|error| format!("cannot resolve {}: {error}", candidate.display()))?;
    if !abs_path.starts_with(workspace_root) {
        return Err(format!(
            "path escapes workspace root: {}",
            abs_path.display()
        ));
    }
    if !abs_path.is_file() {
        return Err(format!(
            "refresh path is not a file: {}",
            abs_path.display()
        ));
    }

    let rel_path = normalize_rel_path(
        abs_path
            .strip_prefix(workspace_root)
            .map_err(|_| "cannot derive workspace-relative path".to_string())?,
    );
    let language = detect_language_from_path(&rel_path);
    if language.is_empty() {
        return Ok("skipped_unsupported".to_string());
    }

    let parse_result = _daemon_parse_and_publish(
        &rel_path,
        None,
        &abs_path.to_string_lossy(),
        Some(cas_store),
        workspace_id,
    );
    let cas_state = require_json_str(&parse_result, "cas_state")?;
    if cas_state != "ready_published" && cas_state != "ready_cache_hit" {
        let detail = parse_result
            .get("error")
            .or_else(|| parse_result.get("parse_error"))
            .and_then(Value::as_str)
            .unwrap_or(cas_state);
        return Err(format!("parse/CAS publish failed ({cas_state}): {detail}"));
    }
    let cas_key = require_json_str(&parse_result, "cas_key")?;
    let content_hash = require_json_str(&parse_result, "content_hash")?;
    let metadata = std::fs::metadata(&abs_path)
        .map_err(|error| format!("cannot stat {}: {error}", abs_path.display()))?;
    let mtime = metadata
        .modified()
        .ok()
        .and_then(|value| value.duration_since(UNIX_EPOCH).ok())
        .map(|value| value.as_secs_f64())
        .unwrap_or(0.0);
    let history = MergeHistoryMetadata {
        mtime,
        parsed_at: now_seconds(),
        commit_hash: commit_hash.to_string(),
    };
    let cas_conn = cas_store
        .conn()
        .lock()
        .map_err(|_| "local CAS mutex is poisoned".to_string())?;
    let merged = merge_cas_to_codegraph_with_history(
        &cas_conn,
        conn,
        cas_key,
        workspace_id,
        &rel_path,
        &abs_path.to_string_lossy(),
        content_hash,
        &language,
        &workspace_root.to_string_lossy(),
        &history,
    )?;
    if merged.merge_status == "cas_miss" {
        return Err(format!("CAS entry disappeared before merge: {cas_key}"));
    }
    Ok(merged.merge_status)
}

/// 构造 enterprise `workspace.file.refresh` 参数。
pub fn build_enterprise_refresh_params(
    workspace_instance_id: &str,
    rel_path: &str,
    agent_session_id: &str,
    session_epoch: u64,
    monotonic_seq: u64,
    canonical_bytes: &[u8],
) -> Value {
    json!({
        "workspace_instance_id": workspace_instance_id,
        "rel_path": normalize_rel_path(Path::new(rel_path)),
        "agent_session_id": agent_session_id,
        "session_epoch": session_epoch,
        "monotonic_seq": monotonic_seq,
        "canonical_bytes_hex": hex::encode(canonical_bytes),
        "content_hash": sha256_hex(canonical_bytes),
    })
}

/// 读取并规范化 enterprise 文件，同时生成相对当前目录的路径。
pub fn prepare_enterprise_file(path: &Path) -> Result<(String, Vec<u8>), String> {
    let current_dir = std::fs::canonicalize(
        std::env::current_dir()
            .map_err(|error| format!("cannot read current directory: {error}"))?,
    )
    .map_err(|error| format!("cannot resolve current directory: {error}"))?;
    let candidate = if path.is_absolute() {
        path.to_path_buf()
    } else {
        current_dir.join(path)
    };
    let abs_path = std::fs::canonicalize(&candidate)
        .map_err(|error| format!("cannot resolve {}: {error}", candidate.display()))?;
    if !abs_path.starts_with(&current_dir) {
        return Err(format!(
            "enterprise refresh path must be inside the current workspace: {}",
            abs_path.display()
        ));
    }
    let rel_path = normalize_rel_path(
        abs_path
            .strip_prefix(&current_dir)
            .map_err(|_| "cannot derive enterprise relative path".to_string())?,
    );
    let canonical = canonicalize_source(&abs_path.to_string_lossy())
        .map_err(|error| format!("cannot canonicalize {}: {error}", abs_path.display()))?;
    Ok((rel_path, canonical.canonical_bytes))
}

pub fn new_cli_session_id() -> String {
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_nanos())
        .unwrap_or(0);
    format!("cw-cli-{}-{nanos:x}", std::process::id())
}

/// 对齐 Python 多路径 refresh 的可见输出。
pub fn format_refresh_output(result: &RefreshBatchResult) -> String {
    let mut lines = Vec::new();
    for item in &result.files {
        if item.success {
            lines.push(format!("Refreshed: {}", item.input_path));
        } else {
            lines.push(format!(
                "[Failed] Error refreshing {}: {}",
                item.input_path,
                item.error.as_deref().unwrap_or("unknown error")
            ));
        }
    }
    if result.files.len() > 1 {
        lines.push(format!(
            "Refresh summary: success {} / failure {} / total {}, elapsed {:.2}s",
            result.success_count(),
            result.failure_count(),
            result.files.len(),
            result.elapsed_seconds
        ));
        if result.failure_count() > 0 {
            lines.push("Failed files:".to_string());
            for item in result.files.iter().filter(|item| !item.success) {
                lines.push(format!(
                    "  - {}: {}",
                    item.input_path,
                    item.error.as_deref().unwrap_or("unknown error")
                ));
            }
        }
    }
    lines.join("\n")
}

fn require_json_str<'a>(value: &'a Value, key: &str) -> Result<&'a str, String> {
    value
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("parse/CAS response is missing string field {key}"))
}

fn normalize_rel_path(path: &Path) -> String {
    path.components()
        .filter_map(|component| match component {
            std::path::Component::Normal(value) => Some(value.to_string_lossy().to_string()),
            std::path::Component::ParentDir => Some("..".to_string()),
            _ => None,
        })
        .collect::<Vec<_>>()
        .join("/")
}

fn current_git_commit(workspace_root: &Path) -> String {
    Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(workspace_root)
        .output()
        .ok()
        .filter(|output| output.status.success())
        .map(|output| String::from_utf8_lossy(&output.stdout).trim().to_string())
        .unwrap_or_default()
}

fn now_seconds() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

pub fn enterprise_batch_result(
    inputs: &[PathBuf],
    responses: Vec<Result<Value, String>>,
    elapsed_seconds: f64,
) -> RefreshBatchResult {
    let files = inputs
        .iter()
        .zip(responses)
        .map(|(path, response)| match response {
            Ok(value) => RefreshFileResult {
                input_path: path.to_string_lossy().to_string(),
                success: true,
                status: value
                    .get("status")
                    .and_then(Value::as_str)
                    .unwrap_or("committed")
                    .to_string(),
                error: None,
            },
            Err(error) => RefreshFileResult {
                input_path: path.to_string_lossy().to_string(),
                success: false,
                status: "failed".to_string(),
                error: Some(error),
            },
        })
        .collect();
    RefreshBatchResult {
        files,
        elapsed_seconds,
    }
}

pub fn connect_params(workspace_instance_id: &str, agent_session_id: &str) -> Value {
    let mut params = Map::new();
    params.insert(
        "workspace_instance_id".to_string(),
        Value::String(workspace_instance_id.to_string()),
    );
    params.insert(
        "agent_session_id".to_string(),
        Value::String(agent_session_id.to_string()),
    );
    Value::Object(params)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::cas_merge::init_codegraph_schema;

    fn setup_db(root: &Path, db_path: &Path) -> Connection {
        let conn = Connection::open(db_path).unwrap();
        init_codegraph_schema(&conn).unwrap();
        conn.execute_batch(
            "CREATE TABLE file_versions (
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
                ast_cache BLOB DEFAULT NULL
            );
            CREATE TABLE file_symbol_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_version_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                module_path TEXT DEFAULT '',
                depth INTEGER DEFAULT -1,
                is_deleted INTEGER DEFAULT 0
            );
            CREATE TABLE call_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_version_id INTEGER NOT NULL,
                caller_qualified TEXT NOT NULL,
                caller_hash TEXT DEFAULT '',
                callee_name TEXT NOT NULL,
                callee_module TEXT DEFAULT '',
                callee_qualified TEXT DEFAULT '',
                callee_file TEXT DEFAULT '',
                call_line INTEGER DEFAULT 0,
                is_cross_file INTEGER DEFAULT 0
            );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (1, 'ws', ?1, 0)",
            params![root.to_string_lossy()],
        )
        .unwrap();
        conn
    }

    #[test]
    fn local_refresh_updates_graph_and_version_history() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("workspace");
        std::fs::create_dir_all(root.join("src")).unwrap();
        let source = root.join("src/lib.rs");
        std::fs::write(&source, "pub fn alpha() { beta(); }\nfn beta() {}\n").unwrap();
        let db_path = temp.path().join("callwarden.db");
        let conn = setup_db(&root, &db_path);

        let first = refresh_local_paths(&conn, &db_path, 1, std::slice::from_ref(&source)).unwrap();
        assert_eq!(first.success_count(), 1);
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM symbols", [], |row| row
                .get::<_, i64>(0))
                .unwrap(),
            2
        );
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM calls", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            1
        );
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM file_versions", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap(),
            1
        );

        std::fs::write(
            &source,
            "pub fn alpha() { gamma(); }\nfn gamma() {}\nfn delta() {}\n",
        )
        .unwrap();
        let second =
            refresh_local_paths(&conn, &db_path, 1, std::slice::from_ref(&source)).unwrap();
        assert_eq!(second.success_count(), 1);
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM file_versions", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap(),
            2
        );
        assert_eq!(
            conn.query_row(
                "SELECT COUNT(*) FROM file_versions WHERE is_current = 1",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            1
        );
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM file_symbol_versions", [], |row| row
                .get::<_, i64>(
                0
            ),)
                .unwrap(),
            6
        );
        assert_eq!(
            conn.query_row(
                "SELECT COUNT(*) FROM file_symbol_versions versions \
                 JOIN file_versions files ON versions.file_version_id = files.id \
                 WHERE files.is_current = 1 AND versions.qualified_name = 'lib.beta' \
                   AND versions.is_deleted = 1",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            1
        );
    }

    #[test]
    fn history_failure_rolls_back_current_graph_update() {
        let temp = tempfile::tempdir().unwrap();
        let root = temp.path().join("workspace");
        std::fs::create_dir_all(&root).unwrap();
        let source = root.join("lib.rs");
        std::fs::write(&source, "fn first() {}\n").unwrap();
        let db_path = temp.path().join("callwarden.db");
        let conn = setup_db(&root, &db_path);
        refresh_local_paths(&conn, &db_path, 1, std::slice::from_ref(&source)).unwrap();
        let original_hash: String = conn
            .query_row(
                "SELECT current_content_hash FROM file_instances WHERE workspace_id = 1",
                [],
                |row| row.get(0),
            )
            .unwrap();

        conn.execute_batch("DROP TABLE call_versions").unwrap();
        std::fs::write(&source, "fn second() {}\n").unwrap();
        let failed =
            refresh_local_paths(&conn, &db_path, 1, std::slice::from_ref(&source)).unwrap();
        assert_eq!(failed.failure_count(), 1);
        let current_hash: String = conn
            .query_row(
                "SELECT current_content_hash FROM file_instances WHERE workspace_id = 1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(current_hash, original_hash);
        assert_eq!(
            conn.query_row(
                "SELECT COUNT(*) FROM symbols WHERE name = 'first'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap(),
            1
        );
    }

    #[test]
    fn enterprise_params_include_generation_and_trusted_hash_input() {
        let params =
            build_enterprise_refresh_params("ws-1", "src\\lib.rs", "session-1", 4, 9, b"abc");
        assert_eq!(params["workspace_instance_id"], "ws-1");
        assert_eq!(params["rel_path"], "src/lib.rs");
        assert_eq!(params["agent_session_id"], "session-1");
        assert_eq!(params["session_epoch"], 4);
        assert_eq!(params["monotonic_seq"], 9);
        assert_eq!(params["canonical_bytes_hex"], "616263");
        assert_eq!(params["content_hash"], sha256_hex(b"abc"));
    }

    #[test]
    fn formatter_matches_python_multi_path_shape() {
        let result = RefreshBatchResult {
            files: vec![
                RefreshFileResult {
                    input_path: "a.rs".to_string(),
                    success: true,
                    status: "merged".to_string(),
                    error: None,
                },
                RefreshFileResult {
                    input_path: "b.rs".to_string(),
                    success: false,
                    status: "failed".to_string(),
                    error: Some("broken".to_string()),
                },
            ],
            elapsed_seconds: 1.25,
        };
        assert_eq!(
            format_refresh_output(&result),
            "Refreshed: a.rs\n\
             [Failed] Error refreshing b.rs: broken\n\
             Refresh summary: success 1 / failure 1 / total 2, elapsed 1.25s\n\
             Failed files:\n  - b.rs: broken"
        );
    }
}
