//! `cw call-chain` 与 `cw topo` 的本地查询、daemon 归一化和兼容输出。

use std::collections::{BTreeMap, HashSet};

use rusqlite::types::Value as SqlValue;
use rusqlite::{params, params_from_iter, Connection, Row};
use serde_json::{json, Map, Value};

/// CLI 调用链允许的最大深度，避免异常参数放大图遍历。
pub const MAX_CALL_CHAIN_DEPTH: usize = 100;

/// 按 Python `CallChainAnalyzerMixin.get_call_chain_down` 语义查询下游调用链。
pub fn query_local_call_chain(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
    requested_depth: i64,
) -> Result<Value, String> {
    let max_depth = bounded_depth(requested_depth);
    let mut visited = HashSet::from([qualified_name.to_string()]);
    let mut visited_order = Vec::new();
    let mut current_level = vec![qualified_name.to_string()];
    let mut levels = Vec::new();

    for depth in 0..max_depth {
        let mut next_level = Vec::new();
        let mut level_callees = Vec::new();

        for chunk in current_level.chunks(500) {
            let placeholders = std::iter::repeat("?")
                .take(chunk.len())
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!(
                "
                SELECT DISTINCT cv.callee_qualified, cv.caller_qualified
                FROM call_versions cv
                JOIN file_versions fv ON cv.file_version_id = fv.id
                JOIN file_instances fi ON fv.file_instance_id = fi.id
                WHERE fi.workspace_id = ?
                  AND fi.status != 'archived'
                  AND fv.is_current = 1
                  AND cv.caller_qualified IN ({placeholders})
                  AND cv.callee_qualified != ''
                "
            );
            let mut bindings = Vec::with_capacity(chunk.len() + 1);
            bindings.push(SqlValue::Integer(workspace_id));
            bindings.extend(chunk.iter().cloned().map(SqlValue::Text));
            let mut stmt = conn
                .prepare(&sql)
                .map_err(|error| format!("cannot prepare call-chain query: {error}"))?;
            let mapped = stmt
                .query_map(params_from_iter(bindings.iter()), |row| {
                    Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
                })
                .map_err(|error| format!("cannot query call-chain level: {error}"))?;
            let mut edges = mapped
                .collect::<Result<Vec<_>, _>>()
                .map_err(|error| format!("cannot read call-chain edge: {error}"))?;
            edges.sort();
            for (callee, caller) in edges {
                if visited.insert(callee.clone()) {
                    visited_order.push(callee.clone());
                    next_level.push(callee.clone());
                    level_callees.push(json!({
                        "callee": callee,
                        "caller": caller,
                        "depth": depth + 1,
                    }));
                }
            }
        }

        if level_callees.is_empty() {
            break;
        }
        levels.push(json!({
            "depth": depth + 1,
            "count": level_callees.len(),
            "callees": level_callees,
        }));
        current_level = next_level;
        if current_level.is_empty() {
            break;
        }
    }

    Ok(json!({
        "start": qualified_name,
        "max_depth_reached": levels.len(),
        "total_downstream": visited_order.len(),
        "levels": levels,
        "all_downstream": visited_order,
    }))
}

/// 将 daemon 的平铺调用边恢复为 Python CLI 的分层结构。
pub fn normalize_enterprise_call_chain(
    value: Value,
    qualified_name: &str,
    requested_depth: i64,
) -> Result<Value, String> {
    let edges = value
        .as_array()
        .ok_or_else(|| "call-chain daemon result must be a JSON array".to_string())?;
    let max_depth = bounded_depth(requested_depth);
    let mut visited = HashSet::from([qualified_name.to_string()]);
    let mut visited_order = Vec::new();
    let mut levels: BTreeMap<usize, Vec<Value>> = BTreeMap::new();

    for edge in edges {
        let object = edge
            .as_object()
            .ok_or_else(|| "call-chain edge must be a JSON object".to_string())?;
        let edge_depth = optional_i64(object, "depth").max(0) as usize + 1;
        if edge_depth > max_depth {
            continue;
        }
        let callee_id = optional_i64(object, "callee_id");
        let callee = optional_string(object, "callee_qualified")
            .or_else(|| {
                (callee_id > 0)
                    .then(|| optional_string(object, "callee_name"))
                    .flatten()
            })
            .unwrap_or_default();
        if callee.is_empty() || !visited.insert(callee.to_string()) {
            continue;
        }
        let caller = optional_string(object, "caller_qualified")
            .or_else(|| optional_string(object, "caller_name"))
            .unwrap_or_default();
        visited_order.push(callee.to_string());
        levels.entry(edge_depth).or_default().push(json!({
            "callee": callee,
            "caller": caller,
            "depth": edge_depth,
        }));
    }

    let levels = levels
        .into_iter()
        .map(|(depth, callees)| {
            json!({
                "depth": depth,
                "count": callees.len(),
                "callees": callees,
            })
        })
        .collect::<Vec<_>>();
    Ok(json!({
        "start": qualified_name,
        "max_depth_reached": levels.len(),
        "total_downstream": visited_order.len(),
        "levels": levels,
        "all_downstream": visited_order,
    }))
}

/// 按 Python `get_topological_order` 的持久化 depth 语义查询函数。
pub fn query_local_topological_order(
    conn: &Connection,
    workspace_id: i64,
    limit: i64,
) -> Result<Value, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT s.depth, fi.rel_path, s.start_line, s.name, s.qualified_name
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fi.status != 'archived'
              AND s.kind = 'fn'
            ORDER BY s.depth ASC, s.start_line ASC, s.id ASC
            LIMIT ?2
            ",
        )
        .map_err(|error| format!("cannot prepare topological query: {error}"))?;
    let mapped = stmt
        .query_map(params![workspace_id, limit], topo_row_to_json)
        .map_err(|error| format!("cannot query topological order: {error}"))?;
    let rows = mapped
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read topological row: {error}"))?;
    Ok(Value::Array(rows))
}

/// 校验 daemon `detail=true` 的拓扑详情响应。
pub fn normalize_enterprise_topological_order(value: Value) -> Result<Value, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "topological daemon result must be a JSON array".to_string())?;
    let mut normalized = Vec::with_capacity(rows.len());
    for row in rows {
        let object = row
            .as_object()
            .cloned()
            .ok_or_else(|| "topological detail item must be a JSON object".to_string())?;
        require_i64(&object, "depth")?;
        require_i64(&object, "start_line")?;
        require_string(&object, "path")?;
        require_string(&object, "name")?;
        normalized.push(Value::Object(object));
    }
    Ok(Value::Array(normalized))
}

/// 按 Python `cw call-chain` 默认中文格式渲染。
pub fn format_call_chain_output(value: &Value) -> Result<String, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "call-chain result must be a JSON object".to_string())?;
    let start = require_string(object, "start")?;
    let total = require_i64(object, "total_downstream")?;
    let max_depth = require_i64(object, "max_depth_reached")?;
    let levels = object
        .get("levels")
        .and_then(Value::as_array)
        .ok_or_else(|| "call-chain levels must be an array".to_string())?;

    let mut lines = vec![
        format!("调用链向下: {start}"),
        format!("  总下游函数数: {total}"),
        format!("  最大深度: {max_depth}"),
        String::new(),
    ];
    for level in levels {
        let level = level
            .as_object()
            .ok_or_else(|| "call-chain level must be a JSON object".to_string())?;
        let depth = require_i64(level, "depth")?;
        let count = require_i64(level, "count")?;
        let callees = level
            .get("callees")
            .and_then(Value::as_array)
            .ok_or_else(|| "call-chain callees must be an array".to_string())?;
        lines.push(format!("第 {depth} 层（{count} 个被调用）:"));
        for item in callees.iter().take(15) {
            let item = item
                .as_object()
                .ok_or_else(|| "call-chain callee must be a JSON object".to_string())?;
            lines.push(format!("  → {}", require_string(item, "callee")?));
        }
        if count > 15 {
            lines.push(format!("  ... 还有 {} 个", count - 15));
        }
        lines.push(String::new());
    }
    Ok(lines.join("\n"))
}

/// 按 Python `cw topo` 默认中文格式渲染。
pub fn format_topological_output(value: &Value) -> Result<String, String> {
    let rows = value
        .as_array()
        .ok_or_else(|| "topological result must be a JSON array".to_string())?;
    let mut lines = vec![format!(
        "拓扑排序（前 {} 个，按 depth 升序 = 底层在前）:",
        rows.len()
    )];
    for (index, row) in rows.iter().enumerate() {
        let object = row
            .as_object()
            .ok_or_else(|| "topological item must be a JSON object".to_string())?;
        let depth = require_i64(object, "depth")?;
        let path = require_string(object, "path")?;
        let line = require_i64(object, "start_line")?;
        let name = require_string(object, "name")?;
        lines.push(format!(
            "  {}. depth={depth:2}  {path}:{line}  {name}",
            index + 1
        ));
    }
    Ok(lines.join("\n"))
}

fn bounded_depth(requested_depth: i64) -> usize {
    usize::try_from(requested_depth.max(0))
        .unwrap_or(MAX_CALL_CHAIN_DEPTH)
        .min(MAX_CALL_CHAIN_DEPTH)
}

fn topo_row_to_json(row: &Row<'_>) -> rusqlite::Result<Value> {
    Ok(json!({
        "depth": row.get::<_, i64>(0)?,
        "path": row.get::<_, String>(1)?,
        "start_line": row.get::<_, i64>(2)?,
        "name": row.get::<_, String>(3)?,
        "qualified_name": row.get::<_, String>(4)?,
    }))
}

fn require_string<'a>(object: &'a Map<String, Value>, key: &str) -> Result<&'a str, String> {
    object
        .get(key)
        .and_then(Value::as_str)
        .ok_or_else(|| format!("traversal field {key:?} must be a string"))
}

fn require_i64(object: &Map<String, Value>, key: &str) -> Result<i64, String> {
    object
        .get(key)
        .and_then(Value::as_i64)
        .ok_or_else(|| format!("traversal field {key:?} must be an integer"))
}

fn optional_string<'a>(object: &'a Map<String, Value>, key: &str) -> Option<&'a str> {
    object.get(key).and_then(Value::as_str)
}

fn optional_i64(object: &Map<String, Value>, key: &str) -> i64 {
    object.get(key).and_then(Value::as_i64).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn make_test_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE file_versions (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                is_current INTEGER NOT NULL
            );
            CREATE TABLE call_versions (
                file_version_id INTEGER NOT NULL,
                caller_qualified TEXT NOT NULL,
                callee_qualified TEXT NOT NULL
            );
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                depth INTEGER NOT NULL
            );

            INSERT INTO file_instances VALUES
                (1, 1, 'one.py', 'active'),
                (2, 2, 'two.py', 'active'),
                (3, 1, 'old.py', 'archived');
            INSERT INTO file_versions VALUES
                (1, 1, 1),
                (2, 2, 1),
                (3, 3, 1);
            INSERT INTO call_versions VALUES
                (1, 'one.a', 'one.b'),
                (1, 'one.b', 'one.c'),
                (1, 'one.c', 'one.a'),
                (2, 'one.a', 'other.secret'),
                (3, 'one.a', 'old.hidden');
            INSERT INTO symbols VALUES
                (1, 1, 'fn', 'a', 'one.a', 30, 2),
                (2, 1, 'fn', 'b', 'one.b', 10, 0),
                (3, 1, 'struct', 'Thing', 'one.Thing', 1, -1),
                (4, 2, 'fn', 'secret', 'other.secret', 1, -1),
                (5, 3, 'fn', 'hidden', 'old.hidden', 1, -1);
            ",
        )
        .unwrap();
        conn
    }

    #[test]
    fn local_queries_enforce_workspace_and_depth_contracts() {
        let conn = make_test_db();
        let chain = query_local_call_chain(&conn, 1, "one.a", 1).unwrap();
        assert_eq!(chain["total_downstream"], 1);
        assert_eq!(chain["levels"][0]["callees"][0]["callee"], "one.b");
        assert_eq!(chain["max_depth_reached"], 1);

        let topo = query_local_topological_order(&conn, 1, 10).unwrap();
        assert_eq!(topo.as_array().unwrap().len(), 2);
        assert_eq!(topo[0]["qualified_name"], "one.b");
        assert_eq!(topo[1]["qualified_name"], "one.a");
    }

    #[test]
    fn enterprise_edges_become_unique_python_levels() {
        let value = normalize_enterprise_call_chain(
            json!([
                {
                    "depth": 0,
                    "caller_qualified": "a",
                    "callee_qualified": "b",
                    "callee_id": 2
                },
                {
                    "depth": 1,
                    "caller_qualified": "b",
                    "callee_qualified": "c",
                    "callee_id": 3
                },
                {
                    "depth": 1,
                    "caller_qualified": "b",
                    "callee_qualified": "a",
                    "callee_id": 1
                }
            ]),
            "a",
            10,
        )
        .unwrap();
        assert_eq!(value["total_downstream"], 2);
        assert_eq!(value["max_depth_reached"], 2);
        assert_eq!(value["levels"][0]["callees"][0]["callee"], "b");
        assert_eq!(value["levels"][1]["callees"][0]["callee"], "c");
    }

    #[test]
    fn output_matches_python_layout() {
        let chain = json!({
            "start": "a",
            "total_downstream": 1,
            "max_depth_reached": 1,
            "levels": [{
                "depth": 1,
                "count": 1,
                "callees": [{"callee": "b"}]
            }]
        });
        assert_eq!(
            format_call_chain_output(&chain).unwrap(),
            "调用链向下: a\n  总下游函数数: 1\n  最大深度: 1\n\n第 1 层（1 个被调用）:\n  → b\n"
        );
        let topo = json!([{
            "depth": -1,
            "path": "a.py",
            "start_line": 2,
            "name": "alpha"
        }]);
        assert_eq!(
            format_topological_output(&topo).unwrap(),
            "拓扑排序（前 1 个，按 depth 升序 = 底层在前）:\n  1. depth=-1  a.py:2  alpha"
        );
    }

    #[test]
    fn depth_is_bounded() {
        assert_eq!(bounded_depth(-1), 0);
        assert_eq!(bounded_depth(10), 10);
        assert_eq!(bounded_depth(10_000), MAX_CALL_CHAIN_DEPTH);
    }
}
