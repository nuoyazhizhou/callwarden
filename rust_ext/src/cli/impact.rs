//! `cw impact` 的 GraphStore CSR 查询与兼容输出。

use std::collections::HashMap;

use rusqlite::types::Value as SqlValue;
use rusqlite::{params, params_from_iter, Connection};
use serde_json::{json, Map, Value};

use crate::graph::GraphStore;
use crate::impact::{cross_layer_impact_core, CodeLayerEntry};

/// 防止异常深度参数放大图遍历。
pub const MAX_IMPACT_DEPTH: usize = 100;

#[derive(Clone, Debug)]
struct SymbolMetadata {
    symbol_hash: String,
    qualified_name: String,
    name: String,
    module_path: String,
    file_path: String,
    visibility: String,
    kind: String,
}

impl SymbolMetadata {
    fn to_json(&self) -> Value {
        json!({
            "symbol_hash": self.symbol_hash,
            "qualified_name": self.qualified_name,
            "name": self.name,
            "module_path": self.module_path,
            "file_path": self.file_path,
            "visibility": self.visibility,
            "kind": self.kind,
        })
    }

    fn to_code_layer(&self) -> CodeLayerEntry {
        CodeLayerEntry {
            qualified_name: self.qualified_name.clone(),
            name: self.name.clone(),
            module_path: self.module_path.clone(),
            visibility: self.visibility.clone(),
            kind: self.kind.clone(),
            file_path: self.file_path.clone(),
        }
    }
}

/// local 模式从当前只读连接加载 GraphStore，确保能读取尚在 WAL 中的数据。
pub fn query_local_impact(
    conn: &Connection,
    workspace_id: i64,
    symbol_hash: &str,
    requested_depth: i64,
) -> Result<Value, String> {
    let db_path = conn
        .query_row("PRAGMA database_list", [], |row| row.get::<_, String>(2))
        .map_err(|error| format!("cannot resolve local graph database path: {error}"))?;
    let mut store = GraphStore::new();
    store
        .load_from_sqlite_readonly_blocking(&db_path, workspace_id)
        .map_err(|error| format!("cannot load impact graph: {error}"))?;
    query_impact_with_store(conn, &store, workspace_id, symbol_hash, requested_depth)
}

/// 复用已发布 GraphStore 查询影响半径，并从同代 snapshot SQLite 补全元数据。
pub fn query_impact_with_store(
    conn: &Connection,
    store: &GraphStore,
    workspace_id: i64,
    symbol_hash: &str,
    requested_depth: i64,
) -> Result<Value, String> {
    let source = query_source(conn, workspace_id, symbol_hash)?;
    let Some((source_id, source_metadata, source_content)) = source else {
        return Ok(empty_result(symbol_hash, requested_depth));
    };

    let bounded_depth = requested_depth.max(0).min(MAX_IMPACT_DEPTH as i64) as usize;
    let layer_ids = store
        .blast_radius_ids_rust(source_id, bounded_depth)
        .map_err(|error| format!("cannot calculate impact radius: {error}"))?;
    let mut direct_caller_ids = store.get_caller_ids(source_id);
    direct_caller_ids.sort_unstable();
    direct_caller_ids.dedup();
    let mut all_ids = layer_ids
        .iter()
        .flat_map(|layer| layer.iter().copied())
        .collect::<Vec<_>>();
    all_ids.extend_from_slice(&direct_caller_ids);
    all_ids.sort_unstable();
    all_ids.dedup();
    let mut metadata = query_metadata(conn, workspace_id, &all_ids)?;
    metadata.insert(source_id, source_metadata.clone());

    let mut layers = Vec::with_capacity(layer_ids.len());
    for (depth, ids) in layer_ids.iter().enumerate() {
        let symbols = ids
            .iter()
            .filter_map(|id| metadata.get(id))
            .map(SymbolMetadata::to_json)
            .collect::<Vec<_>>();
        if !symbols.is_empty() {
            layers.push(json!({"depth": depth, "symbols": symbols}));
        }
    }

    let code_layer = direct_caller_ids
        .iter()
        .filter_map(|id| metadata.get(id))
        .map(SymbolMetadata::to_code_layer)
        .collect::<Vec<_>>();
    let cross = cross_layer_impact_core(
        &source_metadata.qualified_name,
        &source_metadata.name,
        &source_content,
        code_layer,
    );
    let total_impacted = layers
        .iter()
        .filter_map(|layer| layer.get("symbols").and_then(Value::as_array))
        .map(Vec::len)
        .sum::<usize>();

    Ok(json!({
        "source_symbol": source_metadata.qualified_name,
        "source_hash": symbol_hash,
        "depth": requested_depth,
        "layers": layers,
        "total_impacted": total_impacted,
        "by_layer": {
            "code": cross.code.len(),
            "db": cross.db.len(),
            "api": cross.api.len(),
            "config": cross.config.len(),
        },
    }))
}

/// 按 Python `cw impact` 默认中文文本格式输出。
pub fn format_impact_output(value: &Value) -> Result<String, String> {
    let object = value
        .as_object()
        .ok_or_else(|| "impact result must be a JSON object".to_string())?;
    let source_symbol = string_field(object, "source_symbol");
    let source_hash = string_field(object, "source_hash");
    let mut lines = vec!["=== 变更影响半径分析 ===".to_string()];
    if source_symbol.is_empty() {
        lines.push(format!("  ✗ 未找到符号: {source_hash}"));
        return Ok(lines.join("\n"));
    }

    let depth = integer_field(object, "depth");
    let total = integer_field(object, "total_impacted");
    let short_hash = source_hash.chars().take(12).collect::<String>();
    lines.push(format!("  源符号: {source_symbol}"));
    lines.push(format!("  源 hash: {short_hash}..."));
    lines.push(format!("  遍历深度: {depth}"));
    lines.push(format!("  影响符号总数: {total}"));
    lines.push(String::new());

    let by_layer = object
        .get("by_layer")
        .and_then(Value::as_object)
        .ok_or_else(|| "impact by_layer must be a JSON object".to_string())?;
    lines.push("  跨层影响分布:".to_string());
    lines.push(format!(
        "    代码层: {} 个",
        integer_field(by_layer, "code")
    ));
    lines.push(format!("    DB 层:  {} 个", integer_field(by_layer, "db")));
    lines.push(format!("    API 层: {} 个", integer_field(by_layer, "api")));
    lines.push(format!(
        "    配置层: {} 个",
        integer_field(by_layer, "config")
    ));
    lines.push(String::new());

    let layers = object
        .get("layers")
        .and_then(Value::as_array)
        .ok_or_else(|| "impact layers must be a JSON array".to_string())?;
    for layer in layers {
        let layer_object = layer
            .as_object()
            .ok_or_else(|| "impact layer must be a JSON object".to_string())?;
        let layer_depth = integer_field(layer_object, "depth");
        let symbols = layer_object
            .get("symbols")
            .and_then(Value::as_array)
            .ok_or_else(|| "impact layer symbols must be a JSON array".to_string())?;
        let label = if layer_depth == 0 {
            "源符号".to_string()
        } else {
            format!("第 {layer_depth} 层")
        };
        lines.push(format!("  【{label}】（{} 个符号）:", symbols.len()));
        for symbol in symbols.iter().take(15) {
            let symbol = symbol
                .as_object()
                .ok_or_else(|| "impact symbol must be a JSON object".to_string())?;
            lines.push(format!(
                "    {} {}",
                string_field(symbol, "kind"),
                string_field(symbol, "qualified_name")
            ));
            let file_path = string_field(symbol, "file_path");
            if !file_path.is_empty() {
                lines.push(format!("             {file_path}"));
            }
        }
        if symbols.len() > 15 {
            lines.push(format!("    ... 还有 {} 个", symbols.len() - 15));
        }
        lines.push(String::new());
    }
    Ok(lines.join("\n"))
}

fn query_source(
    conn: &Connection,
    workspace_id: i64,
    symbol_hash: &str,
) -> Result<Option<(u32, SymbolMetadata, String)>, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT s.id, s.symbol_hash, COALESCE(s.qualified_name, ''),
                   COALESCE(s.name, ''), COALESCE(s.module_path, ''),
                   COALESCE(fi.rel_path, ''), COALESCE(s.visibility, ''),
                   COALESCE(s.kind, ''), COALESCE(sc.content, '')
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ?1
              AND fi.status != 'archived'
              AND s.symbol_hash = ?2
            LIMIT 1
            ",
        )
        .map_err(|error| format!("cannot prepare impact source query: {error}"))?;
    let mut rows = stmt
        .query(params![workspace_id, symbol_hash])
        .map_err(|error| format!("cannot query impact source: {error}"))?;
    let Some(row) = rows
        .next()
        .map_err(|error| format!("cannot read impact source: {error}"))?
    else {
        return Ok(None);
    };
    let id = row
        .get::<_, i64>(0)
        .map_err(|error| format!("cannot read impact source id: {error}"))? as u32;
    let metadata = SymbolMetadata {
        symbol_hash: row
            .get(1)
            .map_err(|error| format!("cannot read impact source hash: {error}"))?,
        qualified_name: row
            .get(2)
            .map_err(|error| format!("cannot read impact source qualified name: {error}"))?,
        name: row
            .get(3)
            .map_err(|error| format!("cannot read impact source name: {error}"))?,
        module_path: row
            .get(4)
            .map_err(|error| format!("cannot read impact source module: {error}"))?,
        file_path: row
            .get(5)
            .map_err(|error| format!("cannot read impact source file: {error}"))?,
        visibility: row
            .get(6)
            .map_err(|error| format!("cannot read impact source visibility: {error}"))?,
        kind: row
            .get(7)
            .map_err(|error| format!("cannot read impact source kind: {error}"))?,
    };
    let content = row
        .get(8)
        .map_err(|error| format!("cannot read impact source content: {error}"))?;
    Ok(Some((id, metadata, content)))
}

fn query_metadata(
    conn: &Connection,
    workspace_id: i64,
    symbol_ids: &[u32],
) -> Result<HashMap<u32, SymbolMetadata>, String> {
    let mut output = HashMap::with_capacity(symbol_ids.len());
    for chunk in symbol_ids.chunks(500) {
        if chunk.is_empty() {
            continue;
        }
        let placeholders = std::iter::repeat("?")
            .take(chunk.len())
            .collect::<Vec<_>>()
            .join(",");
        let sql = format!(
            "
            SELECT s.id, s.symbol_hash, COALESCE(s.qualified_name, ''),
                   COALESCE(s.name, ''), COALESCE(s.module_path, ''),
                   COALESCE(fi.rel_path, ''), COALESCE(s.visibility, ''),
                   COALESCE(s.kind, '')
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND fi.status != 'archived'
              AND s.id IN ({placeholders})
            "
        );
        let mut bindings = Vec::with_capacity(chunk.len() + 1);
        bindings.push(SqlValue::Integer(workspace_id));
        bindings.extend(chunk.iter().map(|id| SqlValue::Integer(i64::from(*id))));
        let mut stmt = conn
            .prepare(&sql)
            .map_err(|error| format!("cannot prepare impact metadata query: {error}"))?;
        let rows = stmt
            .query_map(params_from_iter(bindings.iter()), |row| {
                Ok((
                    row.get::<_, i64>(0)? as u32,
                    SymbolMetadata {
                        symbol_hash: row.get(1)?,
                        qualified_name: row.get(2)?,
                        name: row.get(3)?,
                        module_path: row.get(4)?,
                        file_path: row.get(5)?,
                        visibility: row.get(6)?,
                        kind: row.get(7)?,
                    },
                ))
            })
            .map_err(|error| format!("cannot query impact metadata: {error}"))?;
        for row in rows {
            let (id, metadata) =
                row.map_err(|error| format!("cannot read impact metadata: {error}"))?;
            output.insert(id, metadata);
        }
    }
    Ok(output)
}

fn empty_result(symbol_hash: &str, requested_depth: i64) -> Value {
    json!({
        "source_symbol": "",
        "source_hash": symbol_hash,
        "depth": requested_depth,
        "layers": [],
        "total_impacted": 0,
        "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
    })
}

fn string_field<'a>(object: &'a Map<String, Value>, key: &str) -> &'a str {
    object.get(key).and_then(Value::as_str).unwrap_or_default()
}

fn integer_field(object: &Map<String, Value>, key: &str) -> i64 {
    object.get(key).and_then(Value::as_i64).unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn create_impact_fixture() -> (tempfile::TempDir, Connection) {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("impact.db");
        let conn = Connection::open(path).unwrap();
        conn.execute_batch(
            "
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE symbol_contents (
                content_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL
            );
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                visibility TEXT,
                module_path TEXT,
                qualified_name TEXT,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                depth INTEGER NOT NULL
            );
            CREATE TABLE calls (
                caller_id INTEGER NOT NULL,
                callee_id INTEGER NOT NULL,
                callee_name TEXT NOT NULL,
                call_line INTEGER NOT NULL,
                is_cross_file INTEGER NOT NULL
            );
            INSERT INTO file_instances VALUES
                (1, 1, 'a.py', 'active'),
                (2, 2, 'foreign.py', 'active'),
                (3, 1, 'archived.py', 'archived');
            INSERT INTO symbol_contents VALUES
                ('hash-caller', 'caller', 'fn', 'caller()'),
                ('hash-target', 'route_handler', 'fn',
                 'SELECT * FROM orders; config.get(\"DB_URL\")'),
                ('hash-foreign', 'foreign', 'fn', 'foreign()'),
                ('hash-archived', 'archived', 'fn', 'archived()');
            INSERT INTO symbols VALUES
                (1, 1, 'hash-caller', 'caller', 'fn', 'public', 'a', 'a.caller', 1, 2, 0),
                (2, 1, 'hash-target', 'route_handler', 'fn', 'private', 'a', 'a.target', 4, 8, 0),
                (3, 2, 'hash-foreign', 'foreign', 'fn', 'public', 'foreign', 'foreign.caller', 1, 2, 0),
                (4, 3, 'hash-archived', 'archived', 'fn', 'public', 'old', 'old.caller', 1, 2, 0);
            INSERT INTO calls VALUES
                (1, 2, 'route_handler', 2, 0),
                (3, 2, 'route_handler', 2, 1),
                (4, 2, 'route_handler', 2, 0);
            ",
        )
        .unwrap();
        (temp, conn)
    }

    #[test]
    fn local_query_reuses_csr_and_isolates_workspace() {
        let (_temp, conn) = create_impact_fixture();
        let result = query_local_impact(&conn, 1, "hash-target", 3).unwrap();
        assert_eq!(result["source_symbol"], "a.target");
        assert_eq!(result["total_impacted"], 2);
        assert_eq!(
            result["layers"][1]["symbols"][0]["qualified_name"],
            "a.caller"
        );
        assert_eq!(result["by_layer"]["code"], 1);
        assert_eq!(result["by_layer"]["db"], 1);
        assert_eq!(result["by_layer"]["api"], 1);
        assert_eq!(result["by_layer"]["config"], 1);

        let zero_depth = query_local_impact(&conn, 1, "hash-target", 0).unwrap();
        assert_eq!(zero_depth["total_impacted"], 1);
        assert_eq!(zero_depth["by_layer"]["code"], 1);
    }

    #[test]
    fn format_missing_symbol_matches_python() {
        let value = empty_result("missing-hash", 3);
        assert_eq!(
            format_impact_output(&value).unwrap(),
            "=== 变更影响半径分析 ===\n  ✗ 未找到符号: missing-hash"
        );
    }

    #[test]
    fn format_found_symbol_limits_details_to_fifteen() {
        let symbols = (0..16)
            .map(|index| {
                json!({
                    "symbol_hash": format!("hash-{index}"),
                    "qualified_name": format!("a.fn_{index}"),
                    "name": format!("fn_{index}"),
                    "module_path": "a",
                    "file_path": "a.py",
                    "visibility": "public",
                    "kind": "fn",
                })
            })
            .collect::<Vec<_>>();
        let value = json!({
            "source_symbol": "a.fn_0",
            "source_hash": "1234567890abcdef",
            "depth": 3,
            "total_impacted": 16,
            "by_layer": {"code": 2, "db": 1, "api": 1, "config": 1},
            "layers": [{"depth": 0, "symbols": symbols}],
        });
        let output = format_impact_output(&value).unwrap();
        assert!(output.contains("  源 hash: 1234567890ab..."));
        assert!(output.contains("    ... 还有 1 个"));
        assert!(!output.contains("a.fn_15\n"));
    }
}
