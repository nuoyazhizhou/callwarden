//! CLI-067（T-1787322798722-a4d2c340）：`cw tests` 专用 handler。
//!
//! 复刻 Python `db/db_tests.py` 的 build_test_relations / import_test_results
//! 语义，使 CLI 成为 HTTP thin client、Rust daemon 成为唯一 authority：
//! - build_test_relations：全量扫描 test 函数符号，按 direct_call /
//!   name_convention / indirect 三种方法推断 test_case_relations 并批量入库；
//! - import_test_results：解析 JUnit XML（quick-xml）写入 test_runs 表，
//!   通过 test_name 匹配 symbols 表 test 函数（CLI 路径不传 binding_context）。

use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Value};

use super::dispatch::{get_int_param_or, get_str_param_or, DaemonRpcError};

fn now_ts() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

fn workspace_root(conn: &Connection, workspace_id: i64) -> Result<std::path::PathBuf, DaemonRpcError> {
    conn.query_row(
        "SELECT root_path FROM workspaces WHERE id = ?1",
        rusqlite::params![workspace_id],
        |row| row.get::<_, String>(0),
    )
    .map(std::path::PathBuf::from)
    .map_err(|e| DaemonRpcError::internal_error(format!("查询 workspace root 失败: {e}")))
}

/// `build_test_relations` —— 全量扫描 test 函数符号，推断 test_case_relations。
pub fn handle_build_test_relations(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let force = params.get("force").and_then(Value::as_bool).unwrap_or(false);
    let now = now_ts();

    if force {
        conn.execute(
            "DELETE FROM test_case_relations WHERE workspace_id = ?1",
            rusqlite::params![workspace_id],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations delete: {e}")))?;
    }

    // 1. 取所有可能是 test 的函数（路径在 tests/ 或文件名 test_*.py 或函数名 test_*）
    let mut stmt = conn
        .prepare(
            "SELECT s.id, s.name, s.qualified_name, s.file_instance_id, fi.rel_path \
             FROM symbols s JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived' \
               AND s.kind IN ('fn','test_fn','method','function') \
               AND (fi.rel_path LIKE 'tests/%' OR fi.rel_path LIKE '%/test_%' \
                    OR s.name LIKE 'test_%')",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations prepare1: {e}")))?;
    let test_fns: Vec<(i64, String, String, i64, String)> = stmt
        .query_map(rusqlite::params![workspace_id], |r| {
            Ok((
                r.get::<_, i64>(0)?,
                r.get::<_, String>(1)?,
                r.get::<_, String>(2)?,
                r.get::<_, i64>(3)?,
                r.get::<_, String>(4)?,
            ))
        })
        .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations query1: {e}")))?
        .collect::<Result<Vec<_>, rusqlite::Error>>()
        .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations collect1: {e}")))?;

    // 2. 所有 fn 符号 name → ids 映射（name_convention 用）
    let mut fn_by_name: std::collections::HashMap<String, Vec<i64>> =
        std::collections::HashMap::new();
    {
        let mut stmt2 = conn
            .prepare(
                "SELECT s.id, s.name FROM symbols s \
                 JOIN file_instances fi ON s.file_instance_id = fi.id \
                 WHERE fi.workspace_id = ?1 AND fi.status != 'archived' \
                   AND s.kind IN ('fn','method','function')",
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations prepare2: {e}")))?;
        let rows: Vec<(i64, String)> = stmt2
            .query_map(rusqlite::params![workspace_id], |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations query2: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations collect2: {e}")))?;
        for (id, name) in rows {
            fn_by_name.entry(name).or_default().push(id);
        }
    }

    // 3a. 批量查 direct_call callees
    let test_fn_ids: Vec<i64> = test_fns.iter().map(|t| t.0).collect();
    let mut direct_calls_map: std::collections::HashMap<i64, Vec<i64>> =
        std::collections::HashMap::new();
    if !test_fn_ids.is_empty() {
        let placeholders = test_fn_ids.iter().map(|_| "?").collect::<Vec<_>>().join(",");
        let sql = format!(
            "SELECT DISTINCT c.caller_id, c.callee_id FROM calls c \
             WHERE c.caller_id IN ({placeholders}) AND c.callee_id > 0"
        );
        let binds: Vec<rusqlite::types::Value> =
            test_fn_ids.iter().map(|x| rusqlite::types::Value::Integer(*x)).collect();
        let mut stmt3 = conn
            .prepare(&sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations prepare3: {e}")))?;
        let rows: Vec<(i64, i64)> = stmt3
            .query_map(rusqlite::params_from_iter(binds.iter()), |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations query3: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations collect3: {e}")))?;
        for (caller, callee) in rows {
            direct_calls_map.entry(caller).or_default().push(callee);
        }
    }

    // 3c. 批量查 indirect callee 的 callers
    let mut seen_callees = std::collections::HashSet::new();
    let mut indirect_callees: Vec<i64> = Vec::new();
    for tid in &test_fn_ids {
        if let Some(callees) = direct_calls_map.get(tid) {
            for c in callees {
                if seen_callees.insert(*c) {
                    indirect_callees.push(*c);
                }
            }
        }
    }
    let mut indirect_map: std::collections::HashMap<i64, Vec<i64>> =
        std::collections::HashMap::new();
    if !indirect_callees.is_empty() {
        let placeholders = indirect_callees.iter().map(|_| "?").collect::<Vec<_>>().join(",");
        let sql = format!(
            "SELECT DISTINCT c.callee_id, c.caller_id FROM calls c \
             JOIN symbols s ON c.caller_id = s.id \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND c.callee_id IN ({placeholders}) \
               AND s.kind IN ('fn','method','function')"
        );
        let mut binds: Vec<rusqlite::types::Value> =
            indirect_callees.iter().map(|x| rusqlite::types::Value::Integer(*x)).collect();
        binds.insert(0, rusqlite::types::Value::Integer(workspace_id));
        let mut stmt4 = conn
            .prepare(&sql)
            .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations prepare4: {e}")))?;
        let rows: Vec<(i64, i64)> = stmt4
            .query_map(rusqlite::params_from_iter(binds.iter()), |r| {
                Ok((r.get::<_, i64>(0)?, r.get::<_, i64>(1)?))
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations query4: {e}")))?
            .collect::<Result<Vec<_>, rusqlite::Error>>()
            .map_err(|e| DaemonRpcError::internal_error(format!("build_test_relations collect4: {e}")))?;
        for (callee, caller) in rows {
            indirect_map.entry(callee).or_default().push(caller);
        }
    }

    // 3d. 组装关联（三种方法）
    // 简化 name_convention：test_foo → foo
    let mut direct_count = 0i64;
    let mut name_count = 0i64;
    let mut indirect_count = 0i64;
    let mut all_relations: Vec<(i64, i64, &str, &str)> = Vec::new();
    for test_fn in &test_fns {
        let test_fn_id = test_fn.0;
        let test_name = &test_fn.1;
        let mut tested: Vec<(i64, &str, &str)> = Vec::new();
        // 方法 A：direct_call
        if let Some(callees) = direct_calls_map.get(&test_fn_id) {
            for c in callees {
                tested.push((*c, "direct_call", "high"));
            }
        }
        // 方法 B：name_convention - test_foo → foo
        if let Some(stripped) = test_name.strip_prefix("test_") {
            if let Some(ids) = fn_by_name.get(stripped) {
                for fid in ids {
                    if !tested.iter().any(|(id, _, _)| *id == *fid) {
                        tested.push((*fid, "name_convention", "mid"));
                    }
                }
            }
        }
        // 方法 C：indirect - 无 direct_call 时回查
        let has_direct = tested.iter().any(|(_, m, _)| *m == "direct_call");
        if !has_direct {
            if let Some(callees) = direct_calls_map.get(&test_fn_id) {
                for c in callees {
                    if let Some(callers) = indirect_map.get(c) {
                        for caller in callers {
                            if !tested.iter().any(|(id, _, _)| *id == *caller) {
                                tested.push((*caller, "indirect", "low"));
                            }
                        }
                    }
                }
            }
        }
        for (tested_id, method, confidence) in tested {
            match method {
                "direct_call" => direct_count += 1,
                "name_convention" => name_count += 1,
                _ => indirect_count += 1,
            }
            all_relations.push((test_fn_id, tested_id, method, confidence));
        }
    }

    // 3e. 批量入库
    let mut inserted = 0i64;
    if !all_relations.is_empty() {
        for (test_fn_id, tested_fn_id, method, confidence) in &all_relations {
            let res = conn.execute(
                "INSERT OR IGNORE INTO test_case_relations \
                 (workspace_id, test_fn_id, tested_fn_id, match_method, confidence, detected_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                rusqlite::params![workspace_id, test_fn_id, tested_fn_id, method, confidence, now],
            );
            if let Ok(n) = res {
                if n > 0 {
                    inserted += n as i64;
                }
            }
        }
    }
    let _ = workspace_root;
    Ok(json!({
        "total_test_fns": test_fns.len(),
        "direct_call": direct_count,
        "name_convention": name_count,
        "indirect": indirect_count,
        "inserted": inserted,
    }))
}

/// `import_test_results` —— 解析 JUnit XML 写入 test_runs（CLI 路径无 binding_context）。
pub fn handle_import_test_results(
    conn: &Connection,
    workspace_id: i64,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let junit_xml = get_str_param_or(params, "junit_xml", "");
    let ci_run_id = get_str_param_or(params, "ci_run_id", "");
    let ci_url = get_str_param_or(params, "ci_url", "");
    let now = now_ts();

    // 如果传入的是文件路径，读取内容
    let xml_content = if std::path::Path::new(&junit_xml).is_file() {
        std::fs::read_to_string(&junit_xml)
            .map_err(|e| DaemonRpcError::internal_error(format!("读取 JUnit XML 失败: {e}")))?
    } else {
        junit_xml.clone()
    };

    let mut total = 0i64;
    let mut passed = 0i64;
    let mut failed = 0i64;
    let mut skipped = 0i64;
    let mut errored = 0i64;
    let mut matched = 0i64;

    // quick-xml 解析
    let mut reader = quick_xml::Reader::from_str(&xml_content);
    reader.config_mut().trim_text(true);
    let mut buf = Vec::new();
    let mut current_tc: Option<(String, String, String, String, String, String, String)> = None;

    loop {
        match reader.read_event_into(&mut buf) {
            Ok(quick_xml::events::Event::Start(e)) if e.name().as_ref() == b"testcase" => {
                let mut name = String::new();
                let mut classname = String::new();
                let mut file = String::new();
                let mut time_str = String::from("0");
                for attr in e.attributes().flatten() {
                    match attr.key.as_ref() {
                        b"name" => name = attr.unescape_value().unwrap_or_default().into_owned(),
                        b"classname" => classname = attr.unescape_value().unwrap_or_default().into_owned(),
                        b"file" => file = attr.unescape_value().unwrap_or_default().into_owned(),
                        b"time" => time_str = attr.unescape_value().unwrap_or_default().into_owned(),
                        _ => {}
                    }
                }
                current_tc = Some((name, classname, file, time_str, String::new(), String::new(), String::from("passed")));
            }
            Ok(quick_xml::events::Event::Empty(e)) if e.name().as_ref() == b"testcase" => {
                let mut name = String::new();
                let mut classname = String::new();
                let mut file = String::new();
                let mut time_str = String::from("0");
                for attr in e.attributes().flatten() {
                    match attr.key.as_ref() {
                        b"name" => name = attr.unescape_value().unwrap_or_default().into_owned(),
                        b"classname" => classname = attr.unescape_value().unwrap_or_default().into_owned(),
                        b"file" => file = attr.unescape_value().unwrap_or_default().into_owned(),
                        b"time" => time_str = attr.unescape_value().unwrap_or_default().into_owned(),
                        _ => {}
                    }
                }
                // 空 testcase（无子元素）= passed
                let matched_here = process_testcase(conn, workspace_id, name, classname, file, time_str, "", "", "passed", ci_run_id.as_str(), ci_url.as_str(), now)?;
                total += 1;
                passed += 1;
                matched += matched_here;
                current_tc = None;
            }
            Ok(quick_xml::events::Event::Start(e)) if e.name().as_ref() == b"failure" => {
                if let Some(tc) = current_tc.as_mut() {
                    tc.5 = String::from("failed");
                    if let Some(msg) = e.attributes().flatten().find(|a| a.key.as_ref() == b"type") {
                        tc.6 = msg.unescape_value().unwrap_or_default().into_owned();
                    }
                }
            }
            Ok(quick_xml::events::Event::Start(e)) if e.name().as_ref() == b"error" => {
                if let Some(tc) = current_tc.as_mut() {
                    tc.5 = String::from("error");
                    if let Some(msg) = e.attributes().flatten().find(|a| a.key.as_ref() == b"type") {
                        tc.6 = msg.unescape_value().unwrap_or_default().into_owned();
                    }
                }
            }
            Ok(quick_xml::events::Event::Start(e)) if e.name().as_ref() == b"skipped" => {
                if let Some(tc) = current_tc.as_mut() {
                    tc.5 = String::from("skipped");
                }
            }
            Ok(quick_xml::events::Event::Empty(e)) if e.name().as_ref() == b"skipped" => {
                if let Some(tc) = current_tc.as_mut() {
                    tc.5 = String::from("skipped");
                }
            }
            Ok(quick_xml::events::Event::Text(t)) if current_tc.is_some() => {
                // 收集 failure/error 文本（错误信息）
                if let Some(tc) = current_tc.as_mut() {
                    if tc.5 == "failed" || tc.5 == "error" {
                        let text = t.unescape().unwrap_or_default().trim().to_string();
                        if !text.is_empty() {
                            if tc.4.len() < 500 {
                                tc.4.push_str(&text);
                            }
                        }
                    }
                }
            }
            Ok(quick_xml::events::Event::End(e)) if e.name().as_ref() == b"testcase" => {
                if let Some((name, classname, file, time_str, err_msg, status, err_type)) = current_tc.take() {
                    let matched_here = process_testcase(conn, workspace_id, name, classname, file, time_str, err_msg.as_str(), err_type.as_str(), status.as_str(), ci_run_id.as_str(), ci_url.as_str(), now)?;
                    total += 1;
                    match status.as_str() {
                        "passed" => passed += 1,
                        "failed" => failed += 1,
                        "skipped" => skipped += 1,
                        _ => errored += 1,
                    }
                    matched += matched_here;
                }
            }
            Ok(quick_xml::events::Event::Eof) => break,
            Err(e) => {
                return Ok(json!({"parse_error": format!("XML parse error: {e}")}));
            }
            _ => {}
        }
        buf.clear();
    }

    Ok(json!({
        "total": total, "passed": passed, "failed": failed,
        "skipped": skipped, "error": errored, "matched": matched,
    }))
}

/// 处理单个 testcase：匹配 symbol + 落库，返回是否 matched。
#[allow(clippy::too_many_arguments)]
fn process_testcase(
    conn: &Connection,
    workspace_id: i64,
    name: String,
    classname: String,
    file: String,
    time_str: String,
    err_msg: &str,
    err_type: &str,
    status: &str,
    ci_run_id: &str,
    ci_url: &str,
    now: f64,
) -> Result<i64, DaemonRpcError> {
    let duration_ms = time_str.parse::<f64>().unwrap_or(0.0) * 1000.0;

    // 匹配 test_fn_id：test_class.test_name → test_name → 短名
    let mut test_fn_id: i64 = 0;
    let mut matched = 0i64;
    let full_name = if !classname.is_empty() { format!("{classname}.{name}") } else { name.clone() };
    // 查询 name → id 映射
    let find_id = |n: &str| -> Option<i64> {
        conn.query_row(
            "SELECT s.id FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.status != 'archived' AND s.name = ?2 \
             LIMIT 1",
            rusqlite::params![workspace_id, n],
            |r| r.get(0),
        )
        .optional()
        .ok()
        .flatten()
    };
    if let Some(id) = find_id(&full_name) {
        test_fn_id = id;
        matched = 1;
    } else if let Some(id) = find_id(&name) {
        test_fn_id = id;
        matched = 1;
    } else {
        let short = name.rsplit('.').next().unwrap_or(&name).to_string();
        if let Some(id) = find_id(&short) {
            test_fn_id = id;
            matched = 1;
        }
    }

    conn.execute(
        "INSERT INTO test_runs \
         (workspace_id, test_fn_id, test_name, test_class, test_file, status, duration_ms, \
          error_message, error_type, ci_run_id, ci_url, run_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12)",
        rusqlite::params![
            workspace_id,
            test_fn_id,
            name,
            classname,
            file,
            status,
            duration_ms,
            err_msg.chars().take(500).collect::<String>(),
            err_type,
            ci_run_id,
            ci_url,
            now
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("test_runs insert: {e}")))?;
    Ok(matched)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn temp_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE symbols (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                qualified_name TEXT,
                kind TEXT DEFAULT 'fn',
                file_instance_id INTEGER DEFAULT 0,
                has_comment INTEGER DEFAULT 0,
                start_line INTEGER DEFAULT 0,
                end_line INTEGER DEFAULT 0,
                signature TEXT DEFAULT '',
                symbol_hash TEXT DEFAULT ''
             );
             CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER,
                rel_path TEXT,
                status TEXT DEFAULT 'active',
                mtime REAL DEFAULT 0,
                last_parsed REAL DEFAULT 0
             );
             CREATE TABLE calls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                caller_id INTEGER,
                callee_id INTEGER
             );
             CREATE TABLE test_case_relations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                test_fn_id INTEGER NOT NULL,
                tested_fn_id INTEGER NOT NULL,
                match_method TEXT NOT NULL,
                confidence TEXT NOT NULL DEFAULT 'mid',
                detected_at REAL NOT NULL
             );
             CREATE TABLE test_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workspace_id INTEGER NOT NULL,
                test_fn_id INTEGER NOT NULL,
                test_name TEXT NOT NULL,
                test_class TEXT DEFAULT '',
                test_file TEXT DEFAULT '',
                status TEXT NOT NULL,
                duration_ms REAL DEFAULT 0,
                error_message TEXT DEFAULT '',
                error_type TEXT DEFAULT '',
                ci_run_id TEXT DEFAULT '',
                ci_url TEXT DEFAULT '',
                run_at REAL NOT NULL
             );
             CREATE TABLE workspaces (
                id INTEGER PRIMARY KEY,
                root_path TEXT,
                name TEXT,
                created_at REAL,
                is_active INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                active_task_id TEXT DEFAULT '',
                runtime_policy TEXT DEFAULT ''
             );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspaces (id, root_path, name) VALUES (1, 'C:/tmp', 'ws1')",
            [],
        )
        .unwrap();
        conn
    }

    #[test]
    fn import_test_results_parses_junit() {
        let conn = temp_db();
        // test 函数符号
        conn.execute(
            "INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'tests/test_a.py')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols (id, name, qualified_name, file_instance_id) VALUES (1, 'test_foo', 'tests.test_foo', 1)",
            [],
        )
        .unwrap();
        let xml = "<?xml version='1.0'?>
        <testsuite>
          <testcase name='test_foo' classname='tests' file='tests/test_a.py' time='0.1'/>
          <testcase name='test_bar' classname='tests' file='tests/test_b.py' time='0.2'>
            <failure type='AssertionError'>assert x</failure>
          </testcase>
          <testcase name='test_skip' classname='tests' file='tests/test_c.py'>
            <skipped/>
          </testcase>
        </testsuite>";
        let params = json!({"junit_xml": xml, "ci_run_id": "run-1", "ci_url": "http://ci/1"});
        let res = handle_import_test_results(&conn, 1, &params).unwrap();
        assert_eq!(res["total"], 3);
        assert_eq!(res["passed"], 1);
        assert_eq!(res["failed"], 1);
        assert_eq!(res["skipped"], 1);
        assert_eq!(res["matched"], 1);
        // test_runs 落库
        let cnt: i64 = conn
            .query_row("SELECT COUNT(*) FROM test_runs", [], |r| r.get(0))
            .unwrap();
        assert_eq!(cnt, 3);
        // failed 记录含 error_type
        let err_type: String = conn
            .query_row(
                "SELECT error_type FROM test_runs WHERE test_name='test_bar'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(err_type, "AssertionError");
    }

    #[test]
    fn import_test_results_parse_error() {
        let conn = temp_db();
        // 非法 XML：标签未闭合 → quick-xml 报 UnexpectedEof
        let params = json!({"junit_xml": "<testsuite><testcase name='x'"});
        let res = handle_import_test_results(&conn, 1, &params).unwrap();
        assert!(res.get("parse_error").is_some());
    }

    #[test]
    fn build_test_relations_detects_direct_call() {
        let conn = temp_db();
        conn.execute(
            "INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'tests/test_a.py')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (2, 1, 'src/a.py')",
            [],
        )
        .unwrap();
        // test_foo 直接调用 foo
        conn.execute(
            "INSERT INTO symbols (id, name, qualified_name, file_instance_id) VALUES (1, 'test_foo', 'tests.test_foo', 1)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO symbols (id, name, qualified_name, file_instance_id) VALUES (2, 'foo', 'pkg.foo', 2)",
            [],
        )
        .unwrap();
        conn.execute("INSERT INTO calls (caller_id, callee_id) VALUES (1, 2)", []).unwrap();
        let params = json!({"force": false});
        let res = handle_build_test_relations(&conn, 1, &params).unwrap();
        assert_eq!(res["total_test_fns"], 1);
        assert_eq!(res["direct_call"], 1);
        assert_eq!(res["inserted"], 1);
        // 关系落库
        let cnt: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM test_case_relations WHERE match_method='direct_call'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(cnt, 1);
    }
}

