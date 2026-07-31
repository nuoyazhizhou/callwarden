//! `cw callers` 与 `cw callees` 的本地查询和兼容输出。

use rusqlite::{params, Connection, Row};
use serde_json::{json, Map, Value};

/// 查询调用指定函数的调用边。
pub fn query_local_callers(
    conn: &Connection,
    workspace_id: i64,
    requested_name: &str,
    qualified_name: Option<&str>,
) -> Result<Value, String> {
    let (callee_name, effective_qname, auto_qname) =
        resolve_name_filter(requested_name, qualified_name);

    if let Some(qname) = effective_qname {
        let rows = query_callers_by_qname(conn, workspace_id, qname)?;
        if !rows.is_empty() || !auto_qname {
            return Ok(Value::Array(rows));
        }
    }

    query_callers_by_name(conn, workspace_id, callee_name).map(Value::Array)
}

/// 查询指定函数调用的调用边。
pub fn query_local_callees(
    conn: &Connection,
    workspace_id: i64,
    requested_name: &str,
    qualified_name: Option<&str>,
) -> Result<Value, String> {
    let (caller_name, effective_qname, auto_qname) =
        resolve_name_filter(requested_name, qualified_name);

    if let Some(qname) = effective_qname {
        let rows = query_callees_by_qname(conn, workspace_id, qname)?;
        if !rows.is_empty() || !auto_qname {
            return Ok(Value::Array(rows));
        }
    }

    query_callees_by_name(conn, workspace_id, caller_name).map(Value::Array)
}

pub fn resolve_name_filter<'a>(
    requested_name: &'a str,
    qualified_name: Option<&'a str>,
) -> (&'a str, Option<&'a str>, bool) {
    if let Some(qname) = qualified_name {
        return (requested_name, Some(qname), false);
    }
    if requested_name.contains('.') || requested_name.contains("::") {
        let short_name = requested_name
            .rsplit('.')
            .next()
            .unwrap_or(requested_name)
            .rsplit("::")
            .next()
            .unwrap_or(requested_name);
        return (short_name, Some(requested_name), true);
    }
    (requested_name, None, false)
}

fn query_callers_by_qname(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Result<Vec<Value>, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT s.name, fi.rel_path, c.call_line, c.is_cross_file,
                   c.caller_id, c.callee_id
            FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fi.status != 'archived'
              AND c.callee_id > 0
              AND c.callee_id = (
                  SELECT target.id
                  FROM symbols target
                  JOIN file_instances target_fi
                    ON target.file_instance_id = target_fi.id
                  WHERE target_fi.workspace_id = ?1
                    AND target_fi.status != 'archived'
                    AND target.qualified_name = ?2
                  LIMIT 1
              )
            ORDER BY fi.rel_path, c.call_line, c.id
            ",
        )
        .map_err(|error| format!("cannot prepare qualified callers query: {error}"))?;
    let mapped = stmt
        .query_map(params![workspace_id, qualified_name], caller_row_to_json)
        .map_err(|error| format!("cannot query qualified callers: {error}"))?;
    let result = collect_rows(mapped, "qualified callers");
    result
}

fn query_callers_by_name(
    conn: &Connection,
    workspace_id: i64,
    callee_name: &str,
) -> Result<Vec<Value>, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT s.name, fi.rel_path, c.call_line, c.is_cross_file,
                   c.caller_id, c.callee_id
            FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fi.status != 'archived'
              AND c.callee_name = ?2
            ORDER BY fi.rel_path, c.call_line, c.id
            ",
        )
        .map_err(|error| format!("cannot prepare callers query: {error}"))?;
    let mapped = stmt
        .query_map(params![workspace_id, callee_name], caller_row_to_json)
        .map_err(|error| format!("cannot query callers: {error}"))?;
    let result = collect_rows(mapped, "callers");
    result
}

fn query_callees_by_qname(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Result<Vec<Value>, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT c.callee_name, COALESCE(c.callee_file, ''),
                   COALESCE(c.callee_qualified, ''), c.call_line,
                   c.is_cross_file, c.callee_id,
                   COALESCE(c.callee_module, '')
            FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fi.status != 'archived'
              AND s.qualified_name = ?2
            ORDER BY c.call_line, c.id
            ",
        )
        .map_err(|error| format!("cannot prepare qualified callees query: {error}"))?;
    let mapped = stmt
        .query_map(params![workspace_id, qualified_name], callee_row_to_json)
        .map_err(|error| format!("cannot query qualified callees: {error}"))?;
    let result = collect_rows(mapped, "qualified callees");
    result
}

fn query_callees_by_name(
    conn: &Connection,
    workspace_id: i64,
    caller_name: &str,
) -> Result<Vec<Value>, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT c.callee_name, COALESCE(c.callee_file, ''),
                   COALESCE(c.callee_qualified, ''), c.call_line,
                   c.is_cross_file, c.callee_id,
                   COALESCE(c.callee_module, '')
            FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fi.status != 'archived'
              AND s.name = ?2
            ORDER BY c.call_line, c.id
            ",
        )
        .map_err(|error| format!("cannot prepare callees query: {error}"))?;
    let mapped = stmt
        .query_map(params![workspace_id, caller_name], callee_row_to_json)
        .map_err(|error| format!("cannot query callees: {error}"))?;
    let result = collect_rows(mapped, "callees");
    result
}

fn collect_rows<I>(rows: I, label: &str) -> Result<Vec<Value>, String>
where
    I: Iterator<Item = rusqlite::Result<Value>>,
{
    rows.map(|row| row.map_err(|error| format!("cannot read {label} row: {error}")))
        .collect()
}

fn caller_row_to_json(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "caller_name": row.get::<_, String>(0)?,
        "caller_file": row.get::<_, String>(1)?,
        "call_line": row.get::<_, i64>(2)?,
        "is_cross_file": row.get::<_, i64>(3)? != 0,
        "caller_id": row.get::<_, i64>(4)?,
        "callee_id": row.get::<_, i64>(5)?,
    }))
}

fn callee_row_to_json(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "callee_name": row.get::<_, String>(0)?,
        "callee_file": row.get::<_, String>(1)?,
        "callee_qualified": row.get::<_, String>(2)?,
        "call_line": row.get::<_, i64>(3)?,
        "is_cross_file": row.get::<_, i64>(4)? != 0,
        "callee_id": row.get::<_, i64>(5)?,
        "callee_module": row.get::<_, String>(6)?,
    }))
}

/// 统一 daemon snapshot 与本地 SQLite 的 callers 字段。
pub fn normalize_callers(value: Value) -> Result<Value, String> {
    normalize_graph_rows(value, GraphDirection::Callers)
}

/// 统一 daemon snapshot 与本地 SQLite 的 callees 字段。
pub fn normalize_callees(value: Value) -> Result<Value, String> {
    normalize_graph_rows(value, GraphDirection::Callees)
}

#[derive(Clone, Copy)]
enum GraphDirection {
    Callers,
    Callees,
}

fn normalize_graph_rows(value: Value, direction: GraphDirection) -> Result<Value, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "graph query result must be a JSON array".to_string())?;
    let mut normalized = Vec::with_capacity(rows.len());
    for row in rows {
        let mut object = row
            .as_object()
            .cloned()
            .ok_or_else(|| "graph query item must be a JSON object".to_string())?;
        object
            .entry("call_line".to_string())
            .or_insert(Value::Number(0.into()));
        object
            .entry("is_cross_file".to_string())
            .or_insert(Value::Bool(false));
        match direction {
            GraphDirection::Callers => {
                require_string(&object, "caller_name")?;
                object
                    .entry("caller_file".to_string())
                    .or_insert_with(|| Value::String(String::new()));
            }
            GraphDirection::Callees => {
                require_string(&object, "callee_name")?;
                if !object.contains_key("callee_qualified") {
                    let qualified = object
                        .get("callee_qualified_name")
                        .cloned()
                        .unwrap_or_else(|| Value::String(String::new()));
                    object.insert("callee_qualified".to_string(), qualified);
                }
                object
                    .entry("callee_file".to_string())
                    .or_insert_with(|| Value::String(String::new()));
                object
                    .entry("callee_module".to_string())
                    .or_insert_with(|| Value::String(String::new()));
            }
        }
        normalized.push(Value::Object(object));
    }
    Ok(Value::Array(normalized))
}

/// 按 Python `cw callers` 的默认中文格式渲染。
pub fn format_callers_output(value: &Value, requested_name: &str) -> Result<String, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "callers result must be a JSON array".to_string())?;
    let mut lines = vec![format!(
        "调用 {requested_name} 的函数（{} 个）:",
        rows.len()
    )];
    for row in rows {
        let object = row
            .as_object()
            .ok_or_else(|| "caller item must be a JSON object".to_string())?;
        let file = require_string(object, "caller_file")?;
        let name = require_string(object, "caller_name")?;
        let line = require_i64(object, "call_line")?;
        let cross = if boolish(object.get("is_cross_file")) {
            " [跨文件]"
        } else {
            ""
        };
        lines.push(format!("  {file}:{line} -> {name}{cross}"));
    }
    Ok(lines.join("\n"))
}

/// 按 Python `cw callees` 的默认中文格式渲染。
pub fn format_callees_output(value: &Value, requested_name: &str) -> Result<String, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "callees result must be a JSON array".to_string())?;
    let mut lines = vec![format!("{requested_name} 调用的函数（{} 个）:", rows.len())];
    for row in rows {
        let object = row
            .as_object()
            .ok_or_else(|| "callee item must be a JSON object".to_string())?;
        let name = require_string(object, "callee_name")?;
        let file = require_string(object, "callee_file")?;
        let line = require_i64(object, "call_line")?;
        let cross = if boolish(object.get("is_cross_file")) {
            " [跨文件]"
        } else {
            ""
        };
        let file_info = if file.is_empty() {
            " [未解析]".to_string()
        } else {
            format!(" ({file})")
        };
        lines.push(format!("  line {line}: {name}{cross}{file_info}"));
    }
    Ok(lines.join("\n"))
}

fn require_string<'a>(object: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("graph query field {key:?} must be a string"))
}

fn require_i64(object: &Map<String, Value>, key: &str) -> Result<i64, String> {
    object
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("graph query field {key:?} must be an integer"))
}

fn boolish(value: Option<&Value>) -> bool {
    match value {
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_i64().unwrap_or_default() != 0,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn automatic_qname_uses_last_separator() {
        assert_eq!(
            resolve_name_filter("crate::module::run", None),
            ("run", Some("crate::module::run"), true)
        );
        assert_eq!(
            resolve_name_filter("module.run", None),
            ("run", Some("module.run"), true)
        );
    }

    #[test]
    fn formats_python_compatible_graph_rows() {
        let callers = json!([{
            "caller_name": "alpha",
            "caller_file": "a.py",
            "call_line": 2,
            "is_cross_file": true
        }]);
        assert_eq!(
            format_callers_output(&callers, "Thing").unwrap(),
            "调用 Thing 的函数（1 个）:\n  a.py:2 -> alpha [跨文件]"
        );

        let callees = json!([{
            "callee_name": "external",
            "callee_file": "",
            "call_line": 3,
            "is_cross_file": false
        }]);
        assert_eq!(
            format_callees_output(&callees, "alpha").unwrap(),
            "alpha 调用的函数（1 个）:\n  line 3: external [未解析]"
        );
    }

    #[test]
    fn normalizes_legacy_daemon_fields() {
        let normalized = normalize_callees(json!([{
            "callee_name": "beta",
            "callee_qualified_name": "a.beta"
        }]))
        .unwrap();
        let row = normalized.as_array().unwrap()[0].as_object().unwrap();
        assert_eq!(row["callee_qualified"], "a.beta");
        assert_eq!(row["call_line"], 0);
        assert_eq!(row["is_cross_file"], false);
    }

    #[test]
    fn local_queries_isolate_same_names_by_workspace() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL
            );
            CREATE TABLE calls (
                id INTEGER PRIMARY KEY,
                caller_id INTEGER NOT NULL,
                callee_name TEXT NOT NULL,
                callee_module TEXT,
                callee_qualified TEXT,
                callee_file TEXT,
                callee_id INTEGER,
                call_line INTEGER,
                is_cross_file INTEGER
            );
            INSERT INTO file_instances VALUES
                (10, 1, 'one.py', 'active'),
                (20, 2, 'two.py', 'active');
            INSERT INTO symbols VALUES
                (100, 10, 'run', 'one.run'),
                (101, 10, 'target', 'one.target'),
                (200, 20, 'run', 'two.run'),
                (201, 20, 'target', 'two.target');
            INSERT INTO calls VALUES
                (1, 100, 'target', 'one', 'one.target', 'one.py', 101, 3, 0),
                (2, 200, 'target', 'two', 'two.target', 'two.py', 201, 7, 1);
            ",
        )
        .unwrap();

        let callers = query_local_callers(&conn, 1, "target", None).unwrap();
        let caller_rows = callers.as_array().unwrap();
        assert_eq!(caller_rows.len(), 1);
        assert_eq!(caller_rows[0]["caller_file"], "one.py");

        let callees = query_local_callees(&conn, 1, "run", None).unwrap();
        let callee_rows = callees.as_array().unwrap();
        assert_eq!(callee_rows.len(), 1);
        assert_eq!(callee_rows[0]["callee_qualified"], "one.target");

        let exact = query_local_callers(&conn, 1, "target", Some("two.target")).unwrap();
        assert!(exact.as_array().unwrap().is_empty());
    }
}
