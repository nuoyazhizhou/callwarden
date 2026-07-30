//! `cw file` 与 `cw query` 的本地只读查询和兼容输出。

use std::path::Path;

use rusqlite::{params, Connection, OptionalExtension, Row};
use serde_json::{json, Value};

/// 查询文件当前投影中的全部符号。
pub fn query_local_file_symbols(
    conn: &Connection,
    workspace_id: i64,
    file_path: &str,
) -> Result<Value, String> {
    let rel_path = normalize_workspace_path(conn, workspace_id, file_path)?;
    let mut stmt = conn
        .prepare(
            "
            SELECT
                s.id, s.file_instance_id, s.symbol_hash, s.name, s.kind,
                s.visibility, s.start_line, s.end_line, s.start_col, s.end_col,
                s.signature, s.has_comment, s.comment_status, s.module_path,
                s.qualified_name, s.depth
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fi.rel_path = ?2
              AND fi.status != 'archived'
            ORDER BY s.start_line
            ",
        )
        .map_err(|error| format!("cannot prepare file symbol query: {error}"))?;
    let rows = stmt
        .query_map(params![workspace_id, rel_path], symbol_row)
        .map_err(|error| format!("cannot query file symbols: {error}"))?;
    let mut symbols = Vec::new();
    for row in rows {
        symbols.push(row.map_err(|error| format!("cannot read file symbol row: {error}"))?);
    }
    Ok(Value::Array(symbols))
}

/// 在指定文件中按短名定位符号。
pub fn query_local_symbol_location(
    conn: &Connection,
    workspace_id: i64,
    name: &str,
    file_path: &str,
) -> Result<Value, String> {
    let rel_path = normalize_workspace_path(conn, workspace_id, file_path)?;
    conn.query_row(
        "
        SELECT
            s.id, s.file_instance_id, s.symbol_hash, s.name, s.kind,
            s.visibility, s.start_line, s.end_line, s.start_col, s.end_col,
            s.signature, s.has_comment, s.comment_status, s.module_path,
            s.qualified_name, s.depth, fi.rel_path, fi.abs_path
        FROM symbols s
        JOIN file_instances fi ON s.file_instance_id = fi.id
        WHERE fi.workspace_id = ?1
          AND fi.status != 'archived'
          AND s.name = ?2
          AND fi.rel_path = ?3
        LIMIT 1
        ",
        params![workspace_id, name, rel_path],
        symbol_location_row,
    )
    .optional()
    .map(|value| value.unwrap_or(Value::Null))
    .map_err(|error| format!("cannot query symbol location: {error}"))
}

/// 按 Python `cw file` 的默认中文格式输出符号列表。
pub fn format_file_symbols_output(value: &Value, requested_path: &str) -> Result<String, String> {
    let symbols = value
        .as_array()
        .ok_or_else(|| "file symbol result must be a JSON array".to_string())?;
    let mut lines = vec![format!(
        "{requested_path} 内的符号（{} 个）:",
        symbols.len()
    )];
    for symbol in symbols {
        let symbol = symbol
            .as_object()
            .ok_or_else(|| "file symbol item must be a JSON object".to_string())?;
        let start_line = symbol
            .get("start_line")
            .and_then(Value::as_i64)
            .ok_or_else(|| "file symbol start_line must be an integer".to_string())?;
        let end_line = symbol
            .get("end_line")
            .and_then(Value::as_i64)
            .ok_or_else(|| "file symbol end_line must be an integer".to_string())?;
        let kind = required_str(symbol, "kind")?;
        let name = required_str(symbol, "name")?;
        let visibility = optional_python_display(symbol, "visibility");
        lines.push(format!(
            "  {start_line}-{end_line}: {kind} {name} ({visibility})"
        ));
    }
    Ok(lines.join("\n"))
}

/// 按 Python `cw query` 输出 JSON 或未找到提示。
pub fn format_symbol_location_output(
    value: &Value,
    requested_name: &str,
) -> Result<String, String> {
    if value.is_null() {
        return Ok(format!("未找到符号: {requested_name}"));
    }
    serde_json::to_string_pretty(value)
        .map_err(|error| format!("cannot encode symbol location: {error}"))
}

fn normalize_workspace_path(
    conn: &Connection,
    workspace_id: i64,
    file_path: &str,
) -> Result<String, String> {
    let root_path: String = conn
        .query_row(
            "SELECT root_path FROM workspaces WHERE id = ?1",
            params![workspace_id],
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot query workspace root: {error}"))?;
    let path = Path::new(file_path);
    let relative = if path.is_absolute() {
        path.strip_prefix(Path::new(&root_path)).unwrap_or(path)
    } else {
        path
    };
    Ok(relative.to_string_lossy().replace('\\', "/"))
}

fn symbol_row(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "id": row.get::<_, i64>(0)?,
        "file_instance_id": row.get::<_, i64>(1)?,
        "symbol_hash": row.get::<_, String>(2)?,
        "name": row.get::<_, String>(3)?,
        "kind": row.get::<_, String>(4)?,
        "visibility": row.get::<_, Option<String>>(5)?,
        "start_line": row.get::<_, i64>(6)?,
        "end_line": row.get::<_, i64>(7)?,
        "start_col": row.get::<_, Option<i64>>(8)?,
        "end_col": row.get::<_, Option<i64>>(9)?,
        "signature": row.get::<_, Option<String>>(10)?,
        "has_comment": row.get::<_, Option<i64>>(11)?,
        "comment_status": row.get::<_, Option<String>>(12)?,
        "module_path": row.get::<_, Option<String>>(13)?,
        "qualified_name": row.get::<_, Option<String>>(14)?,
        "depth": row.get::<_, Option<i64>>(15)?,
    }))
}

fn symbol_location_row(row: &Row<'_>) -> rusqlite::Result<Value> {
    let mut value = symbol_row(row)?;
    let object = value.as_object_mut().expect("symbol_row returns an object");
    object.insert("rel_path".to_string(), Value::String(row.get(16)?));
    object.insert("abs_path".to_string(), Value::String(row.get(17)?));
    Ok(value)
}

fn required_str<'a>(
    object: &'a serde_json::Map<String, Value>,
    key: &str,
) -> Result<&'a str, String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("file symbol field {key:?} must be a string"))
}

fn optional_python_display<'a>(object: &'a serde_json::Map<String, Value>, key: &str) -> &'a str {
    object.get(key).and_then(Value::as_str).unwrap_or("None")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "
            CREATE TABLE workspaces (
                id INTEGER PRIMARY KEY,
                root_path TEXT NOT NULL
            );
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                abs_path TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                visibility TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                start_col INTEGER NOT NULL,
                end_col INTEGER NOT NULL,
                signature TEXT NOT NULL,
                has_comment INTEGER NOT NULL,
                comment_status TEXT NOT NULL,
                module_path TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                depth INTEGER NOT NULL
            );
            INSERT INTO workspaces VALUES (7, '/repo'), (8, '/other');
            INSERT INTO file_instances VALUES
                (1, 7, 'src/a.py', '/repo/src/a.py', 'active'),
                (2, 7, 'src/old.py', '/repo/src/old.py', 'archived'),
                (3, 8, 'src/a.py', '/other/src/a.py', 'active');
            INSERT INTO symbols VALUES
                (1, 1, 'hash-outer', 'outer', 'fn', 'public', 1, 8, 0, 0,
                 'outer()', 1, 'done', 'src.a', 'src.a.outer', 0),
                (2, 1, 'hash-inner', 'inner', 'fn', 'private', 3, 5, 4, 8,
                 'inner()', 0, 'pending', 'src.a', 'src.a.inner', 1),
                (3, 2, 'hash-old', 'old', 'fn', 'public', 1, 2, 0, 0,
                 'old()', 0, 'pending', 'src.old', 'src.old.old', 0),
                (4, 3, 'hash-other', 'other', 'fn', 'public', 1, 2, 0, 0,
                 'other()', 0, 'pending', 'src.a', 'src.a.other', 0);
            ",
        )
        .unwrap();
        conn
    }

    #[test]
    fn file_symbols_are_ordered_and_workspace_scoped() {
        let conn = fixture();
        let value = query_local_file_symbols(&conn, 7, "src/a.py").unwrap();
        assert_eq!(value.as_array().unwrap().len(), 2);
        assert_eq!(value[0]["name"], "outer");
        assert_eq!(value[1]["name"], "inner");
        assert_eq!(
            format_file_symbols_output(&value, "src/a.py").unwrap(),
            "src/a.py 内的符号（2 个）:\n  1-8: fn outer (public)\n  3-5: fn inner (private)"
        );
    }

    #[test]
    fn archived_and_foreign_workspace_symbols_are_hidden() {
        let conn = fixture();
        assert_eq!(
            query_local_file_symbols(&conn, 7, "src/old.py")
                .unwrap()
                .as_array()
                .unwrap()
                .len(),
            0
        );
        assert!(query_local_symbol_location(&conn, 7, "other", "src/a.py")
            .unwrap()
            .is_null());
    }

    #[test]
    fn symbol_location_preserves_python_json_contract() {
        let conn = fixture();
        let value = query_local_symbol_location(&conn, 7, "inner", "src/a.py").unwrap();
        assert_eq!(value["qualified_name"], "src.a.inner");
        assert_eq!(value["rel_path"], "src/a.py");
        assert_eq!(value["abs_path"], "/repo/src/a.py");
        let output = format_symbol_location_output(&value, "inner").unwrap();
        assert!(output.contains("\"name\": \"inner\""));
        assert_eq!(
            format_symbol_location_output(&Value::Null, "missing").unwrap(),
            "未找到符号: missing"
        );
    }
}
