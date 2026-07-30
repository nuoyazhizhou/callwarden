//! `cw search` 的本地查询与兼容输出。

use rusqlite::{params, Connection, Row};
use serde_json::{json, Map, Value};

/// 从当前快照搜索符号。
///
/// 查询语义与 Python `db_query.search_symbols` 的最终 LIKE 路径一致：
/// workspace 隔离、归档文件过滤、kind 可选过滤，以及稳定排序。
pub fn query_local_search(
    conn: &Connection,
    workspace_id: i64,
    query: &str,
    kind: Option<&str>,
    limit: usize,
) -> Result<Value, String> {
    if limit == 0 {
        return Ok(Value::Array(Vec::new()));
    }

    if let Ok(fts_query) = build_fts_query(query) {
        if let Ok(value) = query_local_search_fts(conn, workspace_id, &fts_query, kind, limit) {
            return Ok(value);
        }
    }
    query_local_search_like(conn, workspace_id, query, kind, limit)
}

fn query_local_search_fts(
    conn: &Connection,
    workspace_id: i64,
    query: &str,
    kind: Option<&str>,
    limit: usize,
) -> Result<Value, String> {
    let base_sql = "
        SELECT DISTINCT
            s.qualified_name,
            s.module_path,
            s.start_line,
            s.end_line,
            s.depth,
            s.name,
            s.kind,
            COALESCE(s.signature, ''),
            COALESCE(s.has_comment, 0),
            fi.rel_path
        FROM symbols_fts
        JOIN symbols s ON s.id = symbols_fts.rowid
        JOIN file_instances fi ON s.file_instance_id = fi.id
        WHERE fi.workspace_id = ?1
          AND fi.status != 'archived'
          AND symbols_fts MATCH ?2
    ";
    query_search_rows(conn, base_sql, workspace_id, query, kind, limit)
}

fn query_local_search_like(
    conn: &Connection,
    workspace_id: i64,
    query: &str,
    kind: Option<&str>,
    limit: usize,
) -> Result<Value, String> {
    let pattern = format!("%{query}%");
    let base_sql = "
        SELECT
            s.qualified_name,
            s.module_path,
            s.start_line,
            s.end_line,
            s.depth,
            s.name,
            s.kind,
            COALESCE(s.signature, ''),
            COALESCE(s.has_comment, 0),
            fi.rel_path
        FROM symbols s
        JOIN file_instances fi ON s.file_instance_id = fi.id
        WHERE fi.workspace_id = ?1
          AND fi.status != 'archived'
          AND (s.qualified_name LIKE ?2 OR s.name LIKE ?2)
    ";
    query_search_rows(conn, base_sql, workspace_id, &pattern, kind, limit)
}

fn query_search_rows(
    conn: &Connection,
    base_sql: &str,
    workspace_id: i64,
    query: &str,
    kind: Option<&str>,
    limit: usize,
) -> Result<Value, String> {
    let mut rows = Vec::new();
    if let Some(kind) = kind {
        let sql = format!(
            "{base_sql}
             AND s.kind = ?3
             ORDER BY s.kind, s.depth DESC, fi.rel_path, s.start_line
             LIMIT ?4"
        );
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|error| format!("cannot prepare local symbol search: {error}"))?;
        let mapped = stmt
            .query_map(
                params![workspace_id, query, kind, limit as i64],
                search_row_to_json,
            )
            .map_err(|error| format!("cannot query local symbols: {error}"))?;
        for row in mapped {
            rows.push(
                row.map_err(|error| format!("cannot read local symbol search row: {error}"))?,
            );
        }
    } else {
        let sql = format!(
            "{base_sql}
             ORDER BY s.kind, s.depth DESC, fi.rel_path, s.start_line
             LIMIT ?3"
        );
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|error| format!("cannot prepare local symbol search: {error}"))?;
        let mapped = stmt
            .query_map(
                params![workspace_id, query, limit as i64],
                search_row_to_json,
            )
            .map_err(|error| format!("cannot query local symbols: {error}"))?;
        for row in mapped {
            rows.push(
                row.map_err(|error| format!("cannot read local symbol search row: {error}"))?,
            );
        }
    }

    Ok(Value::Array(rows))
}

fn build_fts_query(query: &str) -> Result<String, String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    for character in query.chars() {
        if character.is_ascii_alphanumeric() || character == '_' {
            current.push(character);
        } else if !current.is_empty() {
            tokens.push(std::mem::take(&mut current));
        }
    }
    if !current.is_empty() {
        tokens.push(current);
    }
    let valid = tokens
        .into_iter()
        .filter(|token| token.chars().count() >= 3)
        .map(|token| format!("\"{token}\""))
        .collect::<Vec<_>>();
    if valid.is_empty() {
        return Err("all tokens shorter than 3 chars".to_string());
    }
    Ok(valid.join(" "))
}

fn search_row_to_json(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "qualified_name": row.get::<_, String>(0)?,
        "module_path": row.get::<_, String>(1)?,
        "start_line": row.get::<_, i64>(2)?,
        "end_line": row.get::<_, i64>(3)?,
        "depth": row.get::<_, i64>(4)?,
        "name": row.get::<_, String>(5)?,
        "kind": row.get::<_, String>(6)?,
        "signature": row.get::<_, String>(7)?,
        "has_comment": row.get::<_, i64>(8)? != 0,
        "file_path": row.get::<_, String>(9)?,
    }))
}

/// 统一 daemon GraphStore 与本地 SQLite 的字段名和缺省字段。
pub fn normalize_search_results(value: Value) -> Result<Value, String> {
    let values = value
        .as_array()
        .ok_or_else(|| "search result must be a JSON array".to_string())?;
    let mut normalized = Vec::with_capacity(values.len());

    for item in values {
        let mut object = item
            .as_object()
            .cloned()
            .ok_or_else(|| "search result item must be a JSON object".to_string())?;
        if !object.contains_key("file_path") {
            let file_path = object
                .get("file_rel_path")
                .cloned()
                .unwrap_or_else(|| Value::String(String::new()));
            object.insert("file_path".to_string(), file_path);
        }
        object
            .entry("signature".to_string())
            .or_insert_with(|| Value::String(String::new()));
        object
            .entry("has_comment".to_string())
            .or_insert(Value::Bool(false));
        normalized.push(Value::Object(object));
    }
    Ok(Value::Array(normalized))
}

/// 按 Python `cw search` 的默认中文输出格式渲染结果。
pub fn format_search_output(
    value: &Value,
    query: &str,
    kind: Option<&str>,
    limit: usize,
) -> Result<String, String> {
    let symbols = value
        .as_array()
        .ok_or_else(|| "search result must be a JSON array".to_string())?;
    let kind_info = kind
        .map(|value| format!("（类型: {value}）"))
        .unwrap_or_default();
    let mut lines = vec![
        format!(
            "搜索结果: '{query}' {kind_info}（共 {} 个，显示前 {} 个）:",
            symbols.len(),
            limit.min(symbols.len())
        ),
        String::new(),
    ];

    for (index, symbol) in symbols.iter().take(limit).enumerate() {
        let object = symbol
            .as_object()
            .ok_or_else(|| "search result item must be a JSON object".to_string())?;
        let depth = json_i64(object, "depth")?;
        let depth = if depth >= 0 {
            depth.to_string()
        } else {
            "?".to_string()
        };
        let has_comment = json_boolish(object, "has_comment");
        let comment_mark = if has_comment { "✓" } else { " " };
        let symbol_kind = json_str(object, "kind")?;
        let qualified_name = json_str(object, "qualified_name")?;
        let file_path = json_str(object, "file_path")?;
        let start_line = json_i64(object, "start_line")?;
        lines.push(format!(
            "  [{:3}] depth={:>3} [{comment_mark}] {:8} {qualified_name}",
            index + 1,
            depth,
            symbol_kind
        ));
        lines.push(format!("         {file_path}:{start_line}"));

        let signature = object
            .get("signature")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .chars()
            .take(50)
            .collect::<String>();
        if !signature.is_empty() {
            lines.push(format!("         {signature}"));
        }
    }

    if symbols.len() >= limit {
        lines.push(String::new());
        lines.push("... 用 --search-limit N 调整显示数量".to_string());
    }
    Ok(lines.join("\n"))
}

fn json_str<'a>(object: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("search result field {key:?} must be a string"))
}

fn json_i64(object: &Map<String, Value>, key: &str) -> Result<i64, String> {
    object
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("search result field {key:?} must be an integer"))
}

fn json_boolish(object: &Map<String, Value>, key: &str) -> bool {
    match object.get(key) {
        Some(Value::Bool(value)) => *value,
        Some(Value::Number(value)) => value.as_i64().unwrap_or_default() != 0,
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> (Connection, i64) {
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
                qualified_name TEXT NOT NULL,
                module_path TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                depth INTEGER NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                signature TEXT,
                has_comment INTEGER
            );
            INSERT INTO file_instances VALUES
                (1, 7, 'a.py', 'active'),
                (2, 8, 'other.py', 'active'),
                (3, 7, 'archived.py', 'archived');
            INSERT INTO symbols VALUES
                (1, 1, 'a.alpha', 'a', 1, 3, 0, 'alpha', 'fn', 'alpha()', 1),
                (2, 1, 'a.AlphaType', 'a', 5, 8, -1, 'AlphaType', 'class', '', 0),
                (3, 2, 'other.alpha', 'other', 1, 2, 0, 'alpha', 'fn', '', 0),
                (4, 3, 'archived.alpha', 'archived', 1, 2, 0, 'alpha', 'fn', '', 0);
            ",
        )
        .unwrap();
        (conn, 7)
    }

    #[test]
    fn local_search_is_workspace_scoped_and_stably_sorted() {
        let (conn, workspace_id) = fixture();
        let result = query_local_search(&conn, workspace_id, "alpha", None, 50).unwrap();
        let rows = result.as_array().unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!(rows[0]["qualified_name"], "a.AlphaType");
        assert_eq!(rows[1]["qualified_name"], "a.alpha");
        assert_eq!(rows[1]["file_path"], "a.py");
    }

    #[test]
    fn local_search_applies_kind_and_limit() {
        let (conn, workspace_id) = fixture();
        let result = query_local_search(&conn, workspace_id, "alpha", Some("fn"), 1).unwrap();
        let rows = result.as_array().unwrap();
        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0]["qualified_name"], "a.alpha");
    }

    #[test]
    fn daemon_results_receive_compatibility_fields() {
        let value = json!([{
            "qualified_name": "a.alpha",
            "kind": "fn",
            "depth": 0,
            "start_line": 1,
            "file_rel_path": "a.py"
        }]);
        let normalized = normalize_search_results(value).unwrap();
        assert_eq!(normalized[0]["file_path"], "a.py");
        assert_eq!(normalized[0]["signature"], "");
        assert_eq!(normalized[0]["has_comment"], false);
    }

    #[test]
    fn output_matches_python_layout() {
        let value = json!([{
            "qualified_name": "a.alpha",
            "kind": "fn",
            "depth": 0,
            "start_line": 1,
            "file_path": "a.py",
            "signature": "alpha()",
            "has_comment": true
        }]);
        let output = format_search_output(&value, "alpha", Some("fn"), 50).unwrap();
        assert!(output.starts_with("搜索结果: 'alpha' （类型: fn）（共 1 个，显示前 1 个）:\n\n"));
        assert!(output.contains("[✓] fn       a.alpha"));
        assert!(output.contains("a.py:1"));
        assert!(output.ends_with("alpha()"));
    }

    #[test]
    fn fts_query_matches_python_token_filtering() {
        assert_eq!(
            build_fts_query("alpha::beta-xy").unwrap(),
            "\"alpha\" \"beta\""
        );
        assert!(build_fts_query("a::xy").is_err());
        assert!(build_fts_query("中文").is_err());
    }
}
