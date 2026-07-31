//! `cw toolchain` 与 `cw build-context` 的 Rust 业务实现。
//!
//! 该模块负责客户端可安全执行的纯逻辑：
//! - 编译器探测与稳定 fingerprint；
//! - `compile_commands.json` 解析和 workspace 级聚合；
//! - build-context-aware resolved edge 计算。
//!
//! SQLite CRUD 复用 daemon 的 `ToolchainStore`，确保 local 与 enterprise
//! 使用同一套 schema、hash 和原子缓存发布语义。

use std::collections::{BTreeMap, HashMap, HashSet};
use std::io::Write;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use rusqlite::{params, Connection, OptionalExtension};
use serde::Deserialize;
use serde_json::Value;

use crate::daemon::toolchain::ResolvedEdgeInput;
use crate::toolchain::{compute_toolchain_fingerprint, detect_compiler_type};

const COMPILER_PROBE_TIMEOUT: Duration = Duration::from_secs(10);

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolchainRegistration {
    pub name: String,
    pub compiler_path: String,
    pub compiler_type: String,
    pub version: String,
    pub target_triple: String,
    pub sysroot: String,
    pub include_dirs: Vec<String>,
    pub predefined_macros: HashMap<String, String>,
    pub fingerprint: String,
    pub description: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct AggregatedBuildContext {
    pub defines: BTreeMap<String, String>,
    pub include_paths: Vec<String>,
    pub compile_flags: Vec<String>,
    pub compiler_path: String,
    pub file_count: usize,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ResolvedEdgesResult {
    pub edges: Vec<ResolvedEdgeInput>,
    pub source: String,
    pub skipped: usize,
}

#[derive(Debug, Deserialize)]
struct CompileCommandItem {
    #[serde(default)]
    file: String,
    #[serde(default)]
    directory: String,
    #[serde(default)]
    command: String,
    #[serde(default)]
    arguments: Vec<Value>,
}

#[derive(Debug, Default)]
struct ParsedCompileEntry {
    file: String,
    directory: String,
    compiler_path: String,
    defines: BTreeMap<String, String>,
    include_paths: Vec<String>,
    compile_flags: Vec<String>,
}

#[derive(Debug, Default)]
struct SymbolIndex {
    qname_map: HashMap<String, i64>,
    name_index: HashMap<String, Vec<i64>>,
    file_symbols: HashMap<String, HashMap<String, i64>>,
    file_for_symbol: HashMap<i64, String>,
}

/// 收集注册工具链所需的可信输入，并计算稳定 fingerprint。
///
/// `no_probe=true` 时只根据文件名推断 compiler type；这与 Python
/// `register_toolchain(..., probe=False)` 的行为一致。
pub fn prepare_toolchain_registration(
    name: &str,
    compiler_path: &Path,
    sysroot: &Path,
    description: &str,
    no_probe: bool,
) -> Result<ToolchainRegistration, String> {
    if name.trim().is_empty() {
        return Err("toolchain name must not be empty".to_string());
    }
    let compiler_path = normalize_path_string(compiler_path);
    let sysroot = if sysroot.as_os_str().is_empty() {
        String::new()
    } else {
        normalize_path_string(sysroot)
    };
    let compiler_type = detect_compiler_type(&compiler_path);
    let mut version = String::new();
    let mut target_triple = String::new();
    let mut include_dirs = Vec::new();
    let mut predefined_macros = HashMap::new();

    if !no_probe && Path::new(&compiler_path).is_file() {
        version = run_compiler(&compiler_path, &["--version"], None)
            .ok()
            .and_then(|(stdout, _)| stdout.lines().next().map(str::trim).map(str::to_string))
            .unwrap_or_default();
        target_triple = run_compiler(&compiler_path, &["-dumpmachine"], None)
            .ok()
            .map(|(stdout, _)| stdout.trim().to_string())
            .unwrap_or_default();
        include_dirs = run_compiler(&compiler_path, &["-E", "-x", "c", "-v", "-"], Some(""))
            .ok()
            .map(|(_, stderr)| parse_include_dirs(&stderr))
            .unwrap_or_default();
        predefined_macros = run_compiler(&compiler_path, &["-E", "-dM", "-x", "c", "-"], Some(""))
            .ok()
            .map(|(stdout, _)| parse_predefined_macros(&stdout))
            .unwrap_or_default();
    }

    let fingerprint = compute_toolchain_fingerprint(
        &compiler_path,
        &compiler_type,
        &version,
        &target_triple,
        &sysroot,
        &include_dirs,
        &predefined_macros,
    );
    Ok(ToolchainRegistration {
        name: name.to_string(),
        compiler_path,
        compiler_type,
        version,
        target_triple,
        sysroot,
        include_dirs,
        predefined_macros,
        fingerprint,
        description: description.to_string(),
    })
}

fn run_compiler(
    compiler_path: &str,
    args: &[&str],
    stdin_text: Option<&str>,
) -> Result<(String, String), String> {
    let mut child = Command::new(compiler_path)
        .args(args)
        .stdin(if stdin_text.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("cannot start compiler {compiler_path}: {error}"))?;
    if let (Some(input), Some(mut stdin)) = (stdin_text, child.stdin.take()) {
        stdin
            .write_all(input.as_bytes())
            .map_err(|error| format!("cannot write compiler stdin: {error}"))?;
    }

    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(status)) => {
                let output = child
                    .wait_with_output()
                    .map_err(|error| format!("cannot collect compiler output: {error}"))?;
                if !status.success() {
                    return Err(format!("compiler exited with {status}"));
                }
                return Ok((
                    String::from_utf8_lossy(&output.stdout).to_string(),
                    String::from_utf8_lossy(&output.stderr).to_string(),
                ));
            }
            Ok(None) if started.elapsed() < COMPILER_PROBE_TIMEOUT => {
                thread::sleep(Duration::from_millis(20));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(format!(
                    "compiler probe timed out after {} seconds",
                    COMPILER_PROBE_TIMEOUT.as_secs()
                ));
            }
            Err(error) => return Err(format!("cannot wait for compiler: {error}")),
        }
    }
}

fn parse_include_dirs(stderr: &str) -> Vec<String> {
    let mut result = Vec::new();
    let mut in_section = false;
    for line in stderr.lines() {
        if line.contains("search starts here:") {
            in_section = true;
            continue;
        }
        if line.contains("End of search list.") {
            break;
        }
        let path = line.trim();
        if in_section && !path.is_empty() && !result.iter().any(|item| item == path) {
            result.push(path.to_string());
        }
    }
    result
}

fn parse_predefined_macros(stdout: &str) -> HashMap<String, String> {
    let mut result = HashMap::new();
    for line in stdout.lines() {
        let Some(body) = line.trim().strip_prefix("#define ") else {
            continue;
        };
        let mut parts = body.splitn(2, char::is_whitespace);
        let name = parts.next().unwrap_or("").trim();
        if !name.is_empty() {
            result.insert(
                name.to_string(),
                parts.next().unwrap_or("").trim().to_string(),
            );
        }
    }
    result
}

/// 解析并聚合 `compile_commands.json`。
pub fn import_compile_commands(
    json_path: &Path,
    workspace_root: &Path,
) -> Result<AggregatedBuildContext, String> {
    let bytes = std::fs::read(json_path)
        .map_err(|error| format!("cannot read {}: {error}", json_path.display()))?;
    let items: Vec<CompileCommandItem> = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid compile_commands.json: {error}"))?;
    let mut entries = Vec::new();
    for item in items {
        if item.file.is_empty() {
            continue;
        }
        let tokens = if !item.arguments.is_empty() {
            item.arguments
                .iter()
                .map(|value| match value {
                    Value::String(text) => text.clone(),
                    other => other.to_string(),
                })
                .collect()
        } else if !item.command.is_empty() {
            shell_split(&item.command).unwrap_or_else(|_| {
                item.command
                    .split_whitespace()
                    .map(str::to_string)
                    .collect()
            })
        } else {
            Vec::new()
        };
        let mut entry = ParsedCompileEntry {
            file: item.file,
            directory: item.directory,
            ..ParsedCompileEntry::default()
        };
        parse_compile_tokens(&tokens, &mut entry);
        normalize_compile_entry_paths(&mut entry, workspace_root);
        entries.push(entry);
    }
    Ok(aggregate_compile_entries(entries))
}

fn shell_split(input: &str) -> Result<Vec<String>, String> {
    let mut tokens = Vec::new();
    let mut current = String::new();
    let mut quote = None;
    let mut escaped = false;
    for ch in input.chars() {
        if escaped {
            current.push(ch);
            escaped = false;
            continue;
        }
        if ch == '\\' && quote != Some('\'') {
            escaped = true;
            continue;
        }
        if let Some(active) = quote {
            if ch == active {
                quote = None;
            } else {
                current.push(ch);
            }
            continue;
        }
        if ch == '\'' || ch == '"' {
            quote = Some(ch);
        } else if ch.is_whitespace() {
            if !current.is_empty() {
                tokens.push(std::mem::take(&mut current));
            }
        } else {
            current.push(ch);
        }
    }
    if quote.is_some() || escaped {
        return Err("unterminated quote or escape".to_string());
    }
    if !current.is_empty() {
        tokens.push(current);
    }
    Ok(tokens)
}

fn parse_compile_tokens(tokens: &[String], entry: &mut ParsedCompileEntry) {
    let mut index = 0usize;
    while index < tokens.len() {
        let token = &tokens[index];
        if index == 0 && !token.starts_with('-') {
            entry.compiler_path = token.clone();
            index += 1;
            continue;
        }
        if token == "-D" {
            if let Some(value) = tokens.get(index + 1) {
                parse_define(value, &mut entry.defines);
                index += 2;
            } else {
                index += 1;
            }
        } else if let Some(value) = token.strip_prefix("-D") {
            parse_define(value, &mut entry.defines);
            index += 1;
        } else if token == "-I" || token == "-isystem" {
            if let Some(value) = tokens.get(index + 1) {
                entry.include_paths.push(value.clone());
                index += 2;
            } else {
                index += 1;
            }
        } else if let Some(value) = token.strip_prefix("-I") {
            entry.include_paths.push(value.to_string());
            index += 1;
        } else if token == "-include" {
            if let Some(value) = tokens.get(index + 1) {
                entry.compile_flags.push(format!("-include {value}"));
                index += 2;
            } else {
                index += 1;
            }
        } else if token == "-U" {
            if let Some(value) = tokens.get(index + 1) {
                entry.compile_flags.push(format!("-U {value}"));
                index += 2;
            } else {
                index += 1;
            }
        } else if token.starts_with('-') {
            entry.compile_flags.push(token.clone());
            index += 1;
        } else {
            index += 1;
        }
    }
}

fn parse_define(value: &str, defines: &mut BTreeMap<String, String>) {
    if let Some((name, value)) = value.split_once('=') {
        defines.insert(name.to_string(), value.to_string());
    } else {
        defines.insert(value.to_string(), String::new());
    }
}

fn normalize_compile_entry_paths(entry: &mut ParsedCompileEntry, workspace_root: &Path) {
    let base = if entry.directory.is_empty() {
        workspace_root.to_path_buf()
    } else {
        PathBuf::from(&entry.directory)
    };
    entry.include_paths = entry
        .include_paths
        .iter()
        .map(|path| normalize_against(Path::new(path), &base))
        .collect();
    if !entry.file.is_empty() && !Path::new(&entry.file).is_absolute() {
        entry.file = normalize_against(Path::new(&entry.file), &base);
    }
}

fn aggregate_compile_entries(entries: Vec<ParsedCompileEntry>) -> AggregatedBuildContext {
    let mut defines = BTreeMap::new();
    let mut include_paths = Vec::new();
    let mut compile_flags = Vec::new();
    let mut seen_includes = HashSet::new();
    let mut seen_flags = HashSet::new();
    let mut compiler_path = String::new();
    for entry in &entries {
        if compiler_path.is_empty() && !entry.compiler_path.is_empty() {
            compiler_path = entry.compiler_path.clone();
        }
        defines.extend(entry.defines.clone());
        for include in &entry.include_paths {
            if seen_includes.insert(include.clone()) {
                include_paths.push(include.clone());
            }
        }
        for flag in &entry.compile_flags {
            if seen_flags.insert(flag.clone()) {
                compile_flags.push(flag.clone());
            }
        }
    }
    AggregatedBuildContext {
        defines,
        include_paths,
        compile_flags,
        compiler_path,
        file_count: entries.len(),
    }
}

/// 计算 build-context-aware resolved edges。
///
/// CAS 表可用时按 raw call 重新解析；否则与 Python 一致，从 calls 表复制。
pub fn compute_resolved_edges(
    conn: &Connection,
    workspace_id: i64,
    build_context_hash: &str,
) -> Result<ResolvedEdgesResult, String> {
    let exists = conn
        .query_row(
            "SELECT 1 FROM workspace_build_contexts
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
            params![workspace_id, build_context_hash],
            |_| Ok(true),
        )
        .optional()
        .map_err(sql_error)?
        .unwrap_or(false);
    if !exists {
        return Err("build_context not found".to_string());
    }
    let search_paths = load_search_paths(conn, workspace_id, build_context_hash)?;
    compute_resolved_edges_with_search_paths(conn, workspace_id, &search_paths)
}

/// 使用 daemon 返回的 build context/toolchain 计算本地符号快照。
///
/// enterprise 模式下 context 真相位于 daemon DB，客户端 SQLite 只提供
/// workspace 符号事实，因此不能再次要求本地库存在同一 context 记录。
pub fn compute_resolved_edges_for_external_context(
    conn: &Connection,
    workspace_id: i64,
    context: &Value,
    toolchain: Option<&Value>,
) -> Result<ResolvedEdgesResult, String> {
    let context_workspace_id = context
        .get("workspace_id")
        .and_then(|value| {
            value
                .as_i64()
                .or_else(|| value.as_str().and_then(|text| text.parse().ok()))
        })
        .ok_or_else(|| "daemon build context lacks workspace_id".to_string())?;
    if context_workspace_id != workspace_id {
        return Err(format!(
            "daemon build context workspace mismatch: expected {workspace_id}, got {context_workspace_id}"
        ));
    }
    let mut search_paths = json_string_array(context, "include_paths")
        .into_iter()
        .filter(|path| !path.is_empty())
        .map(|path| (normalize_search_path(&path), "include_path"))
        .collect::<Vec<_>>();
    if let Some(toolchain) = toolchain {
        if let Some(sysroot) = toolchain.get("sysroot").and_then(Value::as_str) {
            if !sysroot.is_empty() {
                search_paths.push((normalize_search_path(sysroot), "sysroot"));
            }
        }
        search_paths.extend(
            json_string_array(toolchain, "include_dirs")
                .into_iter()
                .filter(|path| !path.is_empty())
                .map(|path| (normalize_search_path(&path), "sysroot")),
        );
    }
    compute_resolved_edges_with_search_paths(conn, workspace_id, &search_paths)
}

fn compute_resolved_edges_with_search_paths(
    conn: &Connection,
    workspace_id: i64,
    search_paths: &[(String, &'static str)],
) -> Result<ResolvedEdgesResult, String> {
    if table_exists(conn, "workspace_manifests")?
        && table_exists(conn, "cas_raw_calls")?
        && table_exists(conn, "cas_symbols")?
    {
        if let Some(result) = compute_from_cas(conn, workspace_id, search_paths)? {
            return Ok(result);
        }
    }
    compute_from_calls(conn, workspace_id)
}

fn compute_from_cas(
    conn: &Connection,
    workspace_id: i64,
    search_paths: &[(String, &'static str)],
) -> Result<Option<ResolvedEdgesResult>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT rc.cas_key, rc.caller_local_id, rc.callee_name, rc.call_line, wm.rel_path
             FROM workspace_manifests wm
             JOIN cas_raw_calls rc ON rc.cas_key = wm.cas_key
             WHERE wm.workspace_id = ?1
               AND wm.cas_key IS NOT NULL AND wm.cas_key != ''",
        )
        .map_err(sql_error)?;
    let raw_calls = stmt
        .query_map(params![workspace_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, Option<i64>>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, String>(4)?,
            ))
        })
        .map_err(sql_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql_error)?;
    if raw_calls.is_empty() {
        return Ok(None);
    }

    let caller_map = build_cas_caller_map(conn, workspace_id)?;
    let symbol_index = build_symbol_index(conn, workspace_id)?;
    let mut edges = Vec::with_capacity(raw_calls.len());
    let mut skipped = 0usize;
    for (cas_key, caller_local_id, callee_name, call_line, caller_relpath) in raw_calls {
        let Some(local_id) = caller_local_id else {
            skipped += 1;
            continue;
        };
        let Some(caller_symbol_id) = caller_map.get(&(cas_key, local_id)).copied() else {
            skipped += 1;
            continue;
        };
        let (callee_symbol_id, callee_file, resolution_method) =
            resolve_callee(&callee_name, &caller_relpath, &symbol_index, search_paths);
        edges.push(ResolvedEdgeInput {
            caller_symbol_id,
            callee_symbol_id,
            callee_name,
            callee_file,
            call_line,
            resolution_method,
        });
    }
    Ok(Some(ResolvedEdgesResult {
        edges,
        source: "cas".to_string(),
        skipped,
    }))
}

fn json_string_array(value: &Value, key: &str) -> Vec<String> {
    value
        .get(key)
        .and_then(Value::as_array)
        .map(|items| {
            items
                .iter()
                .filter_map(Value::as_str)
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default()
}

fn build_cas_caller_map(
    conn: &Connection,
    workspace_id: i64,
) -> Result<HashMap<(String, i64), i64>, String> {
    let mut file_stmt = conn
        .prepare(
            "SELECT id, rel_path FROM file_instances
             WHERE workspace_id = ?1 AND COALESCE(status, '') != 'archived'",
        )
        .map_err(sql_error)?;
    let file_ids = file_stmt
        .query_map(params![workspace_id], |row| {
            Ok((row.get::<_, String>(1)?, row.get::<_, i64>(0)?))
        })
        .map_err(sql_error)?
        .collect::<Result<HashMap<_, _>, _>>()
        .map_err(sql_error)?;

    let mut qname_index = HashMap::new();
    let mut name_line_index = HashMap::new();
    let mut symbol_stmt = conn
        .prepare(
            "SELECT s.id, s.qualified_name, s.name, s.start_line, s.file_instance_id
             FROM symbols s
             JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.workspace_id = ?1",
        )
        .map_err(sql_error)?;
    for row in symbol_stmt
        .query_map(params![workspace_id], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, Option<String>>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, i64>(3)?,
                row.get::<_, i64>(4)?,
            ))
        })
        .map_err(sql_error)?
    {
        let (id, qname, name, line, file_id) = row.map_err(sql_error)?;
        if let Some(qname) = qname.filter(|value| !value.is_empty()) {
            qname_index.insert((qname, file_id), id);
        }
        name_line_index.insert((name, line, file_id), id);
    }

    let mut stmt = conn
        .prepare(
            "SELECT cs.cas_key, cs.local_symbol_id, cs.local_qualified_name,
                    cs.name, cs.start_line, wm.rel_path
             FROM workspace_manifests wm
             JOIN cas_symbols cs ON cs.cas_key = wm.cas_key
             WHERE wm.workspace_id = ?1",
        )
        .map_err(sql_error)?;
    let mut result = HashMap::new();
    for row in stmt
        .query_map(params![workspace_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
                row.get::<_, Option<String>>(2)?,
                row.get::<_, String>(3)?,
                row.get::<_, i64>(4)?,
                row.get::<_, String>(5)?,
            ))
        })
        .map_err(sql_error)?
    {
        let (cas_key, local_id, local_qname, name, line, rel_path) = row.map_err(sql_error)?;
        let Some(file_id) = file_ids.get(&rel_path).copied() else {
            continue;
        };
        let symbol_id = local_qname
            .filter(|value| !value.is_empty())
            .and_then(|qname| qname_index.get(&(qname, file_id)).copied())
            .or_else(|| name_line_index.get(&(name, line, file_id)).copied());
        if let Some(symbol_id) = symbol_id {
            result.insert((cas_key, local_id), symbol_id);
        }
    }
    Ok(result)
}

fn compute_from_calls(conn: &Connection, workspace_id: i64) -> Result<ResolvedEdgesResult, String> {
    let mut stmt = conn
        .prepare(
            "SELECT c.caller_id, c.callee_id, c.callee_name, c.callee_file, c.call_line
             FROM calls c
             JOIN symbols s ON c.caller_id = s.id
             JOIN file_instances fi ON s.file_instance_id = fi.id
             WHERE fi.workspace_id = ?1",
        )
        .map_err(sql_error)?;
    let edges = stmt
        .query_map(params![workspace_id], |row| {
            Ok(ResolvedEdgeInput {
                caller_symbol_id: row.get(0)?,
                callee_symbol_id: row.get::<_, Option<i64>>(1)?.unwrap_or(0),
                callee_name: row.get(2)?,
                callee_file: row.get::<_, Option<String>>(3)?.unwrap_or_default(),
                call_line: row.get::<_, Option<i64>>(4)?.unwrap_or(0),
                resolution_method: "from_calls".to_string(),
            })
        })
        .map_err(sql_error)?
        .collect::<Result<Vec<_>, _>>()
        .map_err(sql_error)?;
    Ok(ResolvedEdgesResult {
        edges,
        source: "calls_table".to_string(),
        skipped: 0,
    })
}

fn build_symbol_index(conn: &Connection, workspace_id: i64) -> Result<SymbolIndex, String> {
    let mut stmt = conn
        .prepare(
            "SELECT s.id, s.name, s.qualified_name, fi.rel_path
             FROM symbols s
             JOIN file_instances fi ON s.file_instance_id = fi.id
             WHERE fi.workspace_id = ?1",
        )
        .map_err(sql_error)?;
    let mut index = SymbolIndex::default();
    for row in stmt
        .query_map(params![workspace_id], |row| {
            Ok((
                row.get::<_, i64>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
                row.get::<_, String>(3)?,
            ))
        })
        .map_err(sql_error)?
    {
        let (id, name, qname, rel_path) = row.map_err(sql_error)?;
        if let Some(qname) = qname.filter(|value| !value.is_empty()) {
            index.qname_map.insert(qname, id);
        }
        index.name_index.entry(name.clone()).or_default().push(id);
        index
            .file_symbols
            .entry(rel_path.clone())
            .or_default()
            .insert(name, id);
        index.file_for_symbol.insert(id, rel_path);
    }
    Ok(index)
}

fn load_search_paths(
    conn: &Connection,
    workspace_id: i64,
    build_context_hash: &str,
) -> Result<Vec<(String, &'static str)>, String> {
    let include_json = conn
        .query_row(
            "SELECT include_paths FROM workspace_build_contexts
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
            params![workspace_id, build_context_hash],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(sql_error)?
        .unwrap_or_else(|| "[]".to_string());
    let includes: Vec<String> = serde_json::from_str(&include_json).unwrap_or_default();
    let toolchain = conn
        .query_row(
            "SELECT t.sysroot, t.include_dirs
             FROM toolchains t
             JOIN workspace_toolchains wt ON wt.toolchain_id = t.id
             WHERE wt.workspace_id = ?1 AND wt.build_context_hash = ?2
             ORDER BY t.id LIMIT 1",
            params![workspace_id, build_context_hash],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(sql_error)?;
    let mut result = includes
        .into_iter()
        .filter(|path| !path.is_empty())
        .map(|path| (normalize_search_path(&path), "include_path"))
        .collect::<Vec<_>>();
    if let Some((sysroot, include_json)) = toolchain {
        if !sysroot.is_empty() {
            result.push((normalize_search_path(&sysroot), "sysroot"));
        }
        let toolchain_includes: Vec<String> =
            serde_json::from_str(&include_json).unwrap_or_default();
        result.extend(
            toolchain_includes
                .into_iter()
                .filter(|path| !path.is_empty())
                .map(|path| (normalize_search_path(&path), "sysroot")),
        );
    }
    Ok(result)
}

fn resolve_callee(
    callee_name: &str,
    caller_relpath: &str,
    index: &SymbolIndex,
    search_paths: &[(String, &'static str)],
) -> (i64, String, String) {
    if let Some(symbol_id) = index.qname_map.get(callee_name).copied() {
        return (
            symbol_id,
            index
                .file_for_symbol
                .get(&symbol_id)
                .cloned()
                .unwrap_or_default(),
            "exact_match".to_string(),
        );
    }
    let candidates = index
        .name_index
        .get(callee_name)
        .map(Vec::as_slice)
        .unwrap_or(&[]);
    if candidates.len() == 1 {
        let symbol_id = candidates[0];
        return (
            symbol_id,
            index
                .file_for_symbol
                .get(&symbol_id)
                .cloned()
                .unwrap_or_default(),
            "simple_name_unique".to_string(),
        );
    }
    if let Some(symbol_id) = index
        .file_symbols
        .get(caller_relpath)
        .and_then(|symbols| symbols.get(callee_name))
        .copied()
    {
        return (
            symbol_id,
            caller_relpath.to_string(),
            "same_file".to_string(),
        );
    }

    for method in ["include_path", "sysroot"] {
        let hits = candidates
            .iter()
            .filter_map(|symbol_id| {
                let rel_path = index.file_for_symbol.get(symbol_id)?;
                let normalized_rel = normalize_search_path(rel_path);
                let matched = search_paths.iter().any(|(path, path_method)| {
                    if *path_method != method {
                        return false;
                    }
                    if method == "include_path" {
                        normalized_rel.starts_with(path)
                    } else {
                        let basename = path.rsplit('/').next().unwrap_or(path);
                        !basename.is_empty() && normalized_rel.starts_with(basename)
                    }
                });
                matched.then_some((*symbol_id, rel_path.clone()))
            })
            .collect::<Vec<_>>();
        if hits.len() == 1 {
            return (hits[0].0, hits[0].1.clone(), method.to_string());
        }
    }
    (0, String::new(), "unresolved".to_string())
}

fn table_exists(conn: &Connection, table: &str) -> Result<bool, String> {
    conn.query_row(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1",
        params![table],
        |_| Ok(true),
    )
    .optional()
    .map(|value| value.unwrap_or(false))
    .map_err(sql_error)
}

fn normalize_search_path(path: &str) -> String {
    path.replace('\\', "/").trim_end_matches('/').to_string()
}

fn normalize_path_string(path: &Path) -> String {
    normalize_components(path).to_string_lossy().to_string()
}

fn normalize_against(path: &Path, base: &Path) -> String {
    let combined = if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    };
    normalize_components(&combined)
        .to_string_lossy()
        .to_string()
}

fn normalize_components(path: &Path) -> PathBuf {
    let mut result = PathBuf::new();
    for component in path.components() {
        match component {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                result.pop();
            }
            other => result.push(other.as_os_str()),
        }
    }
    result
}

fn sql_error(error: rusqlite::Error) -> String {
    format!("build-context SQLite query failed: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::toolchain::{compute_build_context_hash, TOOLCHAIN_SCHEMA_DDL};

    #[test]
    fn compile_commands_arguments_are_aggregated_like_python() {
        let temp = tempfile::tempdir().unwrap();
        let path = temp.path().join("compile_commands.json");
        std::fs::write(
            &path,
            r#"[
                {"directory":"/project","file":"a.c",
                 "arguments":["gcc","-DDEBUG=1","-I","include","-O2","a.c"]},
                {"directory":"/project","file":"b.c",
                 "command":"gcc -DBOARD=A98 -I include -g b.c"}
            ]"#,
        )
        .unwrap();
        let result = import_compile_commands(&path, Path::new("/project")).unwrap();
        assert_eq!(result.file_count, 2);
        assert_eq!(result.compiler_path, "gcc");
        assert_eq!(result.defines["DEBUG"], "1");
        assert_eq!(result.defines["BOARD"], "A98");
        assert_eq!(
            result.include_paths,
            vec![normalize_against(
                Path::new("include"),
                Path::new("/project")
            )]
        );
        assert_eq!(result.compile_flags, vec!["-O2", "-g"]);
    }

    #[test]
    fn registration_without_probe_has_stable_fingerprint() {
        let first = prepare_toolchain_registration(
            "gcc",
            Path::new("/opt/gcc/bin/gcc"),
            Path::new(""),
            "",
            true,
        )
        .unwrap();
        let second = prepare_toolchain_registration(
            "gcc",
            Path::new("/opt/gcc/bin/gcc"),
            Path::new(""),
            "",
            true,
        )
        .unwrap();
        assert_eq!(first.compiler_type, "gcc");
        assert_eq!(first.fingerprint, second.fingerprint);
        assert_eq!(first.fingerprint.len(), 64);
    }

    #[test]
    fn calls_fallback_is_workspace_scoped() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY, workspace_id INTEGER, rel_path TEXT, status TEXT
             );
             CREATE TABLE symbols (
                id INTEGER PRIMARY KEY, file_instance_id INTEGER, name TEXT,
                qualified_name TEXT, start_line INTEGER
             );
             CREATE TABLE calls (
                caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
                callee_file TEXT, call_line INTEGER
             );",
        )
        .unwrap();
        conn.execute_batch(TOOLCHAIN_SCHEMA_DDL).unwrap();
        let hash = compute_build_context_hash(&[], &[], &[]);
        conn.execute(
            "INSERT INTO workspace_build_contexts
             (workspace_id, build_context_hash, name, created_at)
             VALUES (1, ?1, 'debug', 1.0)",
            params![hash],
        )
        .unwrap();
        conn.execute_batch(
            "INSERT INTO file_instances VALUES (1, 1, 'a.c', 'clean');
             INSERT INTO file_instances VALUES (2, 2, 'b.c', 'clean');
             INSERT INTO symbols VALUES (10, 1, 'a', 'a', 1);
             INSERT INTO symbols VALUES (20, 2, 'b', 'b', 1);
             INSERT INTO calls VALUES (10, 0, 'callee_a', '', 3);
             INSERT INTO calls VALUES (20, 0, 'callee_b', '', 4);",
        )
        .unwrap();
        let result = compute_resolved_edges(&conn, 1, &hash).unwrap();
        assert_eq!(result.source, "calls_table");
        assert_eq!(result.edges.len(), 1);
        assert_eq!(result.edges[0].callee_name, "callee_a");
    }

    #[test]
    fn external_context_does_not_require_local_context_row() {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY, workspace_id INTEGER, rel_path TEXT, status TEXT
             );
             CREATE TABLE symbols (
                id INTEGER PRIMARY KEY, file_instance_id INTEGER, name TEXT,
                qualified_name TEXT, start_line INTEGER
             );
             CREATE TABLE calls (
                caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
                callee_file TEXT, call_line INTEGER
             );
             INSERT INTO file_instances VALUES (1, 7, 'a.c', 'clean');
             INSERT INTO symbols VALUES (10, 1, 'a', 'a', 1);
             INSERT INTO calls VALUES (10, 0, 'callee_a', '', 3);",
        )
        .unwrap();
        let context = serde_json::json!({
            "workspace_id": 7,
            "build_context_hash": "remote-only",
            "include_paths": ["include"],
        });
        let toolchain = serde_json::json!({
            "sysroot": "/opt/sdk",
            "include_dirs": ["/opt/sdk/include"],
        });
        let result =
            compute_resolved_edges_for_external_context(&conn, 7, &context, Some(&toolchain))
                .unwrap();
        assert_eq!(result.source, "calls_table");
        assert_eq!(result.edges.len(), 1);
        assert_eq!(result.edges[0].caller_symbol_id, 10);
    }

    #[test]
    fn shell_split_handles_quoted_arguments() {
        assert_eq!(
            shell_split(r#"gcc -DNAME="hello world" -I 'some dir' a.c"#).unwrap(),
            vec!["gcc", "-DNAME=hello world", "-I", "some dir", "a.c"]
        );
    }
}
