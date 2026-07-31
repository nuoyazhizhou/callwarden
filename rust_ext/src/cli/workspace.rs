//! `cw workspace` 的本地生命周期实现。
//!
//! 本地模式保持 Python `CodeGraphDB` 的兼容语义：
//! - register 按 name 或规范化 root_path 幂等；
//! - activate 保证最多一个 active workspace；
//! - remove 在单事务内清理 workspace 关联数据后物理删除；
//! - status/list 只读，不触发 workspace 激活写入。

use std::collections::HashSet;
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct WorkspaceRecord {
    pub id: i64,
    pub name: String,
    pub root_path: String,
    pub is_active: bool,
    pub description: String,
}

/// 对齐 Python `norm_path(os.path.abspath(path))`，但不要求路径已经存在。
pub fn normalize_workspace_root(path: &Path) -> Result<String, String> {
    let absolute = if path.is_absolute() {
        path.to_path_buf()
    } else {
        std::env::current_dir()
            .map_err(|error| format!("cannot resolve current directory: {error}"))?
            .join(path)
    };
    let mut clean = PathBuf::new();
    for component in absolute.components() {
        match component {
            Component::CurDir => {}
            Component::ParentDir => {
                clean.pop();
            }
            other => clean.push(other.as_os_str()),
        }
    }
    let mut normalized = clean.to_string_lossy().replace('\\', "/");
    while normalized.len() > 1 && normalized.ends_with('/') {
        normalized.pop();
    }
    let bytes = normalized.as_bytes();
    if bytes.len() >= 2 && bytes[1] == b':' && bytes[0].is_ascii_alphabetic() {
        normalized.replace_range(0..1, &normalized[0..1].to_ascii_lowercase());
    }
    Ok(normalized)
}

pub fn list_local_workspaces(conn: &Connection) -> Result<Vec<WorkspaceRecord>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT id, name, root_path, is_active, COALESCE(description, '')
             FROM workspaces ORDER BY is_active DESC, id ASC",
        )
        .map_err(sql_error)?;
    let rows = stmt
        .query_map([], row_to_workspace)
        .map_err(sql_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql_error)?;
    Ok(rows)
}

pub fn get_local_workspace(
    conn: &Connection,
    id_or_name: &str,
) -> Result<Option<WorkspaceRecord>, String> {
    query_workspace(conn, id_or_name).map_err(sql_error)
}

pub fn register_local_workspace(
    conn: &mut Connection,
    name: &str,
    root: &Path,
    description: &str,
) -> Result<WorkspaceRecord, String> {
    if name.trim().is_empty() {
        return Err("workspace name must not be empty".to_string());
    }
    let root_path = normalize_workspace_root(root)?;
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(sql_error)?;
    if let Some(existing) = tx
        .query_row(
            "SELECT id, name, root_path, is_active, COALESCE(description, '')
             FROM workspaces WHERE name = ?1 OR root_path = ?2
             ORDER BY CASE WHEN name = ?1 THEN 0 ELSE 1 END LIMIT 1",
            params![name, root_path],
            row_to_workspace,
        )
        .optional()
        .map_err(sql_error)?
    {
        tx.commit().map_err(sql_error)?;
        return Ok(existing);
    }

    tx.execute(
        "INSERT INTO workspaces
         (name, root_path, created_at, is_active, description)
         VALUES (?1, ?2, ?3, 0, ?4)",
        params![name, root_path, now_ts(), description],
    )
    .map_err(sql_error)?;
    let id = tx.last_insert_rowid();
    let workspace = tx
        .query_row(
            "SELECT id, name, root_path, is_active, COALESCE(description, '')
             FROM workspaces WHERE id = ?1",
            params![id],
            row_to_workspace,
        )
        .map_err(sql_error)?;
    tx.commit().map_err(sql_error)?;
    Ok(workspace)
}

pub fn activate_local_workspace(
    conn: &mut Connection,
    id_or_name: &str,
) -> Result<Option<WorkspaceRecord>, String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(sql_error)?;
    let Some(mut workspace) = query_workspace(&tx, id_or_name).map_err(sql_error)? else {
        tx.rollback().map_err(sql_error)?;
        return Ok(None);
    };
    if !workspace.is_active {
        tx.execute(
            "UPDATE workspaces SET is_active = 0 WHERE is_active != 0",
            [],
        )
        .map_err(sql_error)?;
        tx.execute(
            "UPDATE workspaces SET is_active = 1 WHERE id = ?1",
            params![workspace.id],
        )
        .map_err(sql_error)?;
        workspace.is_active = true;
    }
    tx.commit().map_err(sql_error)?;
    Ok(Some(workspace))
}

pub fn remove_local_workspace(
    conn: &mut Connection,
    id_or_name: &str,
) -> Result<Option<WorkspaceRecord>, String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(sql_error)?;
    let Some(workspace) = query_workspace(&tx, id_or_name).map_err(sql_error)? else {
        tx.rollback().map_err(sql_error)?;
        return Ok(None);
    };

    delete_workspace_dependents(&tx, workspace.id)?;
    tx.execute(
        "DELETE FROM symbols WHERE file_instance_id IN
         (SELECT id FROM file_instances WHERE workspace_id = ?1)",
        params![workspace.id],
    )
    .map_err(sql_error)?;
    tx.execute(
        "DELETE FROM file_versions WHERE file_instance_id IN
         (SELECT id FROM file_instances WHERE workspace_id = ?1)",
        params![workspace.id],
    )
    .map_err(sql_error)?;
    tx.execute(
        "DELETE FROM file_instances WHERE workspace_id = ?1",
        params![workspace.id],
    )
    .map_err(sql_error)?;
    tx.execute(
        "DELETE FROM workspaces WHERE id = ?1",
        params![workspace.id],
    )
    .map_err(sql_error)?;
    tx.commit().map_err(sql_error)?;
    Ok(Some(workspace))
}

/// 清理所有具有已知 workspace/file/symbol/version 关联列的从表。
///
/// 这里读取 SQLite schema，而不是维护易过期的固定表名单；表名和列名均来自
/// `sqlite_master` / `PRAGMA table_info`，并在拼接前进行标识符引用。
fn delete_workspace_dependents(tx: &Transaction<'_>, workspace_id: i64) -> Result<(), String> {
    let protected = HashSet::from([
        "workspaces",
        "file_instances",
        "file_versions",
        "symbols",
        "sqlite_sequence",
    ]);
    let mut table_stmt = tx
        .prepare(
            "SELECT name FROM sqlite_master
             WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
             ORDER BY name",
        )
        .map_err(sql_error)?;
    let tables = table_stmt
        .query_map([], |row| row.get::<_, String>(0))
        .map_err(sql_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql_error)?;
    drop(table_stmt);

    for table in tables {
        if protected.contains(table.as_str()) {
            continue;
        }
        let columns = table_columns(tx, &table)?;
        let mut predicates = Vec::new();
        for column in ["workspace_id", "source_workspace_id", "target_workspace_id"] {
            if columns.contains(column) {
                predicates.push(format!("{} = ?1", quote_identifier(column)));
            }
        }
        if columns.contains("file_instance_id") {
            predicates.push(format!(
                "{} IN (SELECT id FROM file_instances WHERE workspace_id = ?1)",
                quote_identifier("file_instance_id")
            ));
        }
        if columns.contains("file_version_id") {
            predicates.push(format!(
                "{} IN (
                    SELECT fv.id FROM file_versions fv
                    JOIN file_instances fi ON fi.id = fv.file_instance_id
                    WHERE fi.workspace_id = ?1
                )",
                quote_identifier("file_version_id")
            ));
        }
        for column in [
            "symbol_id",
            "caller_id",
            "callee_id",
            "symbol_a_id",
            "symbol_b_id",
            "test_fn_id",
            "tested_fn_id",
        ] {
            if columns.contains(column) {
                predicates.push(format!(
                    "{} IN (
                        SELECT s.id FROM symbols s
                        JOIN file_instances fi ON fi.id = s.file_instance_id
                        WHERE fi.workspace_id = ?1
                    )",
                    quote_identifier(column)
                ));
            }
        }
        if predicates.is_empty() {
            continue;
        }
        let sql = format!(
            "DELETE FROM {} WHERE {}",
            quote_identifier(&table),
            predicates.join(" OR ")
        );
        tx.execute(&sql, params![workspace_id]).map_err(sql_error)?;
    }
    Ok(())
}

fn table_columns(tx: &Transaction<'_>, table: &str) -> Result<HashSet<String>, String> {
    let sql = format!("PRAGMA table_info({})", quote_identifier(table));
    let mut stmt = tx.prepare(&sql).map_err(sql_error)?;
    let columns = stmt
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(sql_error)?
        .collect::<Result<HashSet<_>, _>>()
        .map_err(sql_error)?;
    Ok(columns)
}

fn quote_identifier(value: &str) -> String {
    format!("\"{}\"", value.replace('"', "\"\""))
}

fn query_workspace(
    conn: &Connection,
    id_or_name: &str,
) -> rusqlite::Result<Option<WorkspaceRecord>> {
    if let Ok(id) = id_or_name.parse::<i64>() {
        conn.query_row(
            "SELECT id, name, root_path, is_active, COALESCE(description, '')
             FROM workspaces WHERE id = ?1",
            params![id],
            row_to_workspace,
        )
        .optional()
    } else {
        conn.query_row(
            "SELECT id, name, root_path, is_active, COALESCE(description, '')
             FROM workspaces WHERE name = ?1",
            params![id_or_name],
            row_to_workspace,
        )
        .optional()
    }
}

fn row_to_workspace(row: &rusqlite::Row<'_>) -> rusqlite::Result<WorkspaceRecord> {
    Ok(WorkspaceRecord {
        id: row.get(0)?,
        name: row.get(1)?,
        root_path: row.get(2)?,
        is_active: row.get::<_, i64>(3)? != 0,
        description: row.get(4)?,
    })
}

pub fn workspace_record_json(workspace: &WorkspaceRecord) -> Value {
    json!({
        "id": workspace.id,
        "name": workspace.name,
        "root_path": workspace.root_path,
        "is_active": workspace.is_active,
        "description": workspace.description,
    })
}

pub fn format_workspace_list(workspaces: &[WorkspaceRecord], zh_cn: bool) -> String {
    let mut lines = vec![if zh_cn {
        format!("工作区列表（共 {} 个）:", workspaces.len())
    } else {
        format!("Workspaces ({} total):", workspaces.len())
    }];
    for workspace in workspaces {
        let active = if workspace.is_active {
            if zh_cn {
                " [活动]"
            } else {
                " [active]"
            }
        } else {
            ""
        };
        lines.push(format!("[{}] {}{}", workspace.id, workspace.name, active));
        lines.push(if zh_cn {
            format!("路径: {}", workspace.root_path)
        } else {
            format!("Path: {}", workspace.root_path)
        });
        if !workspace.description.is_empty() {
            lines.push(if zh_cn {
                format!("描述: {}", workspace.description)
            } else {
                format!("Description: {}", workspace.description)
            });
        }
    }
    lines.join("\n")
}

pub fn format_register_success(workspace: &WorkspaceRecord, zh_cn: bool) -> String {
    if zh_cn {
        format!(
            "工作区已注册: ID={}, name={}, root={}",
            workspace.id, workspace.name, workspace.root_path
        )
    } else {
        format!(
            "Workspace registered: ID={}, name={}, root={}",
            workspace.id, workspace.name, workspace.root_path
        )
    }
}

pub fn format_activate_result(
    workspace: Option<&WorkspaceRecord>,
    id_or_name: &str,
    zh_cn: bool,
) -> String {
    match workspace {
        Some(workspace) if zh_cn => format!(
            "已切换到活动工作区: {} ({})",
            workspace.name, workspace.root_path
        ),
        Some(workspace) => format!(
            "Switched to active workspace: {} ({})",
            workspace.name, workspace.root_path
        ),
        None if zh_cn => format!("切换失败: 未找到工作区 '{}'", id_or_name),
        None => format!("Switch failed: workspace '{}' not found", id_or_name),
    }
}

pub fn format_remove_result(
    workspace: Option<&WorkspaceRecord>,
    id_or_name: &str,
    zh_cn: bool,
) -> String {
    match workspace {
        Some(_) if zh_cn => format!("工作区 '{}' 已删除", id_or_name),
        Some(_) => format!("Workspace '{}' deleted", id_or_name),
        None if zh_cn => format!("删除失败: 未找到工作区 '{}'", id_or_name),
        None => format!("Delete failed: workspace '{}' not found", id_or_name),
    }
}

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs_f64()
}

fn sql_error(error: rusqlite::Error) -> String {
    format!("workspace SQLite query failed: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn setup_conn() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "PRAGMA foreign_keys=ON;
             CREATE TABLE workspaces (
                 id INTEGER PRIMARY KEY AUTOINCREMENT,
                 name TEXT UNIQUE NOT NULL,
                 root_path TEXT UNIQUE NOT NULL,
                 created_at REAL NOT NULL,
                 is_active INTEGER DEFAULT 0,
                 description TEXT DEFAULT ''
             );
             CREATE TABLE file_instances (
                 id INTEGER PRIMARY KEY,
                 workspace_id INTEGER NOT NULL,
                 FOREIGN KEY(workspace_id) REFERENCES workspaces(id)
             );
             CREATE TABLE symbols (
                 id INTEGER PRIMARY KEY,
                 file_instance_id INTEGER NOT NULL,
                 FOREIGN KEY(file_instance_id) REFERENCES file_instances(id)
             );
             CREATE TABLE file_versions (
                 id INTEGER PRIMARY KEY,
                 file_instance_id INTEGER NOT NULL,
                 FOREIGN KEY(file_instance_id) REFERENCES file_instances(id)
             );
             CREATE TABLE calls (
                 id INTEGER PRIMARY KEY,
                 caller_id INTEGER NOT NULL,
                 callee_id INTEGER DEFAULT 0,
                 FOREIGN KEY(caller_id) REFERENCES symbols(id)
             );",
        )
        .unwrap();
        conn
    }

    #[test]
    fn register_is_idempotent_by_name_or_root() {
        let mut conn = setup_conn();
        let root = std::env::current_dir().unwrap().join("one");
        let first = register_local_workspace(&mut conn, "one", &root, "first").unwrap();
        let by_name =
            register_local_workspace(&mut conn, "one", Path::new("other"), "second").unwrap();
        let by_root = register_local_workspace(&mut conn, "other", &root, "second").unwrap();
        assert_eq!(first.id, by_name.id);
        assert_eq!(first.id, by_root.id);
        assert_eq!(list_local_workspaces(&conn).unwrap().len(), 1);
    }

    #[test]
    fn activate_is_atomic_and_unique() {
        let mut conn = setup_conn();
        let one = register_local_workspace(&mut conn, "one", Path::new("one"), "").unwrap();
        let two = register_local_workspace(&mut conn, "two", Path::new("two"), "").unwrap();
        activate_local_workspace(&mut conn, &one.id.to_string()).unwrap();
        activate_local_workspace(&mut conn, "two").unwrap();
        let rows = list_local_workspaces(&conn).unwrap();
        assert_eq!(rows.iter().filter(|row| row.is_active).count(), 1);
        assert_eq!(rows[0].id, two.id);
    }

    #[test]
    fn remove_cleans_symbol_edges_before_parent_rows() {
        let mut conn = setup_conn();
        let workspace = register_local_workspace(&mut conn, "one", Path::new("one"), "").unwrap();
        conn.execute(
            "INSERT INTO file_instances(id, workspace_id) VALUES (10, ?1)",
            params![workspace.id],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols(id, file_instance_id) VALUES (20, 10)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO calls(id, caller_id, callee_id) VALUES (30, 20, 0)",
            [],
        )
        .unwrap();

        let removed = remove_local_workspace(&mut conn, "one").unwrap().unwrap();
        assert_eq!(removed.id, workspace.id);
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM calls", [], |row| row.get::<_, i64>(0))
                .unwrap(),
            0
        );
        assert!(list_local_workspaces(&conn).unwrap().is_empty());
    }
}
