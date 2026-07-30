//! Phase 2-3: 调用边解析、resolve 与批量写入 PyO3 暴露层
//!
//! 对应 Python `db_build.py::_build_call_graph_multi_lang`：
//! - `batch_resolve_and_save_calls` —— 5 策略 resolve + 批量写入 calls + call_versions
//!
//! 设计原则（见 docs/design/phase2-3-batch-save-calls-contract.md §5）：
//! - codegraph_db_path 用读写连接（默认 OpenFlags）
//! - busy_timeout=5000（写锁冲突最多等 5 秒）
//! - BEGIN IMMEDIATE → 全部 SQL → COMMIT/ROLLBACK
//! - 失败不抛异常，返回 dict {"success": False, "error": str(e)}
//!
//! 行为契约（C1-C14，见契约文档 §3）：
//! - 5 策略 resolve 与 Python 一致
//! - caller_id 多级 fallback（qname_id_map → name → simple_name with :: . # 分隔符）
//! - DELETE calls 分批 500（SQLite 999 参数限制）
//! - INSERT calls/call_versions 顺序与 file_results.raw_calls 遍历顺序一致

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use pyo3::Bound;
use rusqlite::params;
use std::collections::{HashMap, HashSet};

// ===========================================================================
// 数据结构
// ===========================================================================

/// 从 Python dict 提取的 call 信息（对应 Python raw_call 字段）
struct CallInfo {
    caller_name: String,
    caller_qualified: String,
    caller_module: String,
    callee_name: String,
    callee_module: String,
    call_line: i64,
}

/// 从 Python dict 提取的项目符号信息
struct SymbolInfo {
    id: i64,
    name: String,
    qualified_name: String,
    #[allow(dead_code)]
    kind: String,
    file_instance_id: i64,
    rel_path: String,
}

/// 从 Python dict 提取的外部符号信息
struct ExtSymbolInfo {
    id: i64,
    symbol_name: String,
    qualified_name: String,
    package_name: String,
}

/// 单个文件的信息（从 file_results 提取）
struct FileInfo {
    rel_path: String,
    file_instance_id: i64,
    file_version_id: i64,
    module_path: String,
    raw_calls: Vec<CallInfo>,
    /// alias/module_name → full_module_path
    imports: HashMap<String, String>,
    /// qualified_name → content_hash（用于 caller_hash）
    fn_hash_map: HashMap<String, String>,
}

/// 单条 call 的 resolve 结果
struct ResolveResult {
    callee_qname: String,
    callee_file: String,
    /// 与 Python 一致：resolve 阶段计算但写入 calls 表时用 qname_id_map 重新查询，此字段冗余
    #[allow(dead_code)]
    callee_id: i64,
    is_cross_file: i64,
}

/// 符号索引（对应 Python all_symbols_map 等 6 个 dict）
struct SymbolIndexes {
    /// qname -> SymbolInfo
    all_symbols_map: HashMap<String, SymbolInfo>,
    /// simple_name -> Vec<qname>
    name_index: HashMap<String, Vec<String>>,
    /// symbol.name -> Vec<qname>（含 "."，用于策略 4.5）
    name_to_qname: HashMap<String, Vec<String>>,
    /// rel_path -> Set<simple_name>
    file_symbols: HashMap<String, HashSet<String>>,
    /// rel_path -> (simple_name -> qname)
    file_local_qname: HashMap<String, HashMap<String, String>>,
    /// suffix（含前导点）-> Vec<qname>
    suffix_index: HashMap<String, Vec<String>>,
}

/// 外部符号索引
struct ExtIndexes {
    /// qname -> ExtSymbolInfo
    ext_by_qname: HashMap<String, ExtSymbolInfo>,
    /// symbol_name -> Vec<ExtSymbolInfo>
    ext_by_name: HashMap<String, Vec<ExtSymbolInfo>>,
}

/// ID 映射（从 DB 构建，对应 Python qname_id_map + file_sym_id_map）
struct IdMaps {
    /// qname -> symbol_id（项目符号正 id，外部符号负 id）
    qname_id_map: HashMap<String, i64>,
    /// file_instance_id -> (name -> symbol_id)
    file_sym_id_map: HashMap<i64, HashMap<String, i64>>,
}

/// 批量写入结果
#[derive(Default)]
struct BatchSaveCallsResult {
    total_calls: usize,
    resolved_count: usize,
    calls_inserted: usize,
    call_versions_inserted: usize,
    old_calls_deleted: usize,
    files_processed: usize,
}

// ===========================================================================
// 工具函数
// ===========================================================================

/// 打开读写连接（与 batch_build_query.rs 一致）
/// PRAGMA 与 Python db_base.py 完全对齐，避免两个 SQLite 实例
/// 对 WAL 文件操作不一致导致 database disk image is malformed
fn open_readwrite(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open(db_path)
        .map_err(|e| PyIOError::new_err(format!("打开 CodeGraph 数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    conn.pragma_update(None, "journal_mode", "WAL")
        .map_err(|e| PyIOError::new_err(format!("设置 WAL 模式失败: {}", e)))?;
    conn.pragma_update(None, "synchronous", "NORMAL")
        .map_err(|e| PyIOError::new_err(format!("设置 synchronous 失败: {}", e)))?;
    conn.pragma_update(None, "foreign_keys", "OFF")
        .map_err(|e| PyIOError::new_err(format!("设置 foreign_keys 失败: {}", e)))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    Ok(conn)
}

/// 从 Python dict 提取 str 字段（必填）
fn get_str(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    let val = dict
        .get_item(key)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))?;
    val.extract::<String>()
}

/// 从 Python dict 提取 str 字段（可选，缺失返回默认值）
fn get_str_or(dict: &Bound<'_, PyDict>, key: &str, default: &str) -> PyResult<String> {
    match dict.get_item(key)? {
        Some(val) => val.extract::<String>(),
        None => Ok(default.to_string()),
    }
}

/// 从 Python dict 提取 i64 字段（必填）
fn get_i64(dict: &Bound<'_, PyDict>, key: &str) -> PyResult<i64> {
    let val = dict
        .get_item(key)?
        .ok_or_else(|| pyo3::exceptions::PyKeyError::new_err(key.to_string()))?;
    val.extract::<i64>()
}

/// 从 Python dict 提取 i64 字段（可选，缺失返回默认值）
fn get_i64_or(dict: &Bound<'_, PyDict>, key: &str, default: i64) -> PyResult<i64> {
    match dict.get_item(key)? {
        Some(val) => val.extract::<i64>(),
        None => Ok(default),
    }
}

/// 从 Python dict 提取 SymbolInfo（项目符号）
fn extract_symbol_info(dict: &Bound<'_, PyDict>) -> PyResult<SymbolInfo> {
    Ok(SymbolInfo {
        id: get_i64(dict, "id")?,
        name: get_str(dict, "name")?,
        qualified_name: get_str(dict, "qualified_name")?,
        kind: get_str_or(dict, "kind", "")?,
        file_instance_id: get_i64(dict, "file_instance_id")?,
        rel_path: get_str(dict, "rel_path")?,
    })
}

/// 从 Python dict 提取 ExtSymbolInfo（外部符号）
fn extract_ext_symbol_info(dict: &Bound<'_, PyDict>) -> PyResult<ExtSymbolInfo> {
    Ok(ExtSymbolInfo {
        id: get_i64(dict, "id")?,
        symbol_name: get_str(dict, "symbol_name")?,
        qualified_name: get_str(dict, "qualified_name")?,
        package_name: get_str_or(dict, "package_name", "")?,
    })
}

/// 从 Python dict 提取 CallInfo
fn extract_call_info(dict: &Bound<'_, PyDict>) -> PyResult<CallInfo> {
    Ok(CallInfo {
        caller_name: get_str_or(dict, "caller_name", "")?,
        caller_qualified: get_str_or(dict, "caller_qualified", "")?,
        caller_module: get_str_or(dict, "caller_module", "")?,
        callee_name: get_str_or(dict, "callee_name", "")?,
        callee_module: get_str_or(dict, "callee_module", "")?,
        call_line: get_i64_or(dict, "call_line", 0)?,
    })
}

/// 从 Python dict 提取 FileInfo（file_results 中单个文件）
fn extract_file_info(dict: &Bound<'_, PyDict>) -> PyResult<FileInfo> {
    let rel_path = get_str(dict, "rel_path")?;
    let file_instance_id = get_i64(dict, "file_instance_id")?;
    let file_version_id = get_i64(dict, "file_version_id")?;
    let module_path = get_str_or(dict, "module_path", "")?;

    // raw_calls
    let raw_calls = match dict.get_item("raw_calls")? {
        Some(val) => {
            let list = val.extract::<Bound<'_, PyList>>()?;
            let mut calls = Vec::with_capacity(list.len());
            for item in list.iter() {
                let d = item.extract::<Bound<'_, PyDict>>()?;
                calls.push(extract_call_info(&d)?);
            }
            calls
        }
        None => Vec::new(),
    };

    // imports（可能是 str 或 dict）
    let imports = match dict.get_item("imports")? {
        Some(val) => {
            let list = val.extract::<Bound<'_, PyList>>()?;
            let mut map = HashMap::new();
            for item in list.iter() {
                let module = if let Ok(s) = item.extract::<String>() {
                    s
                } else if let Ok(d) = item.extract::<Bound<'_, PyDict>>() {
                    get_str_or(&d, "module", "")?
                } else {
                    continue;
                };
                if module.is_empty() {
                    continue;
                }
                // 计算 alias（与 Python 一致）
                let alias = if module.contains('/') {
                    let parts: Vec<&str> = module.trim_end_matches('/').split('/').collect();
                    parts.last().copied().unwrap_or("").to_string()
                } else if module.contains('.') {
                    let parts: Vec<&str> = module.split('.').collect();
                    parts.last().copied().unwrap_or("").to_string()
                } else {
                    module.replace(".h", "").replace(".hpp", "")
                };
                map.insert(alias, module);
            }
            map
        }
        None => HashMap::new(),
    };

    // fn_hash_map：优先用预提取的，否则从 symbols + inline_modules 构建
    let fn_hash_map = match dict.get_item("fn_hash_map")? {
        Some(val) if !val.is_none() => {
            // 预提取的 dict
            let d = val.extract::<Bound<'_, PyDict>>()?;
            let mut map = HashMap::new();
            for (k, v) in d.iter() {
                let key: String = k.extract::<String>()?;
                let value: String = v.extract::<String>()?;
                map.insert(key, value);
            }
            map
        }
        _ => {
            // 从 symbols + inline_modules 构建
            let mut map = HashMap::new();
            if let Some(val) = dict.get_item("symbols")? {
                let list = val.extract::<Bound<'_, PyList>>()?;
                for item in list.iter() {
                    if let Ok(d) = item.extract::<Bound<'_, PyDict>>() {
                        let kind = get_str_or(&d, "kind", "")?;
                        if kind == "fn" || kind == "test_fn" {
                            if let Ok(qname) = get_str(&d, "qualified_name") {
                                if let Ok(hash) = get_str(&d, "content_hash") {
                                    map.insert(qname, hash);
                                }
                            }
                        }
                    }
                }
            }
            if let Some(val) = dict.get_item("inline_modules")? {
                if let Ok(list) = val.extract::<Bound<'_, PyList>>() {
                    for inline_mod in list.iter() {
                        if let Ok(d) = inline_mod.extract::<Bound<'_, PyDict>>() {
                            if let Some(syms_val) = d.get_item("symbols")? {
                                if let Ok(syms) = syms_val.extract::<Bound<'_, PyList>>() {
                                    for sym in syms.iter() {
                                        if let Ok(sd) = sym.extract::<Bound<'_, PyDict>>() {
                                            let kind = get_str_or(&sd, "kind", "")?;
                                            if kind == "fn" || kind == "test_fn" {
                                                if let Ok(qname) = get_str(&sd, "qualified_name") {
                                                    if let Ok(hash) = get_str(&sd, "content_hash") {
                                                        map.insert(qname, hash);
                                                    }
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
            map
        }
    };

    Ok(FileInfo {
        rel_path,
        file_instance_id,
        file_version_id,
        module_path,
        raw_calls,
        imports,
        fn_hash_map,
    })
}

// ===========================================================================
// 索引构建
// ===========================================================================

/// 构建符号索引（all_symbols_map + name_index + suffix_index 等 6 个 dict）
fn build_symbol_indexes(symbols: &[SymbolInfo]) -> SymbolIndexes {
    let mut all_symbols_map: HashMap<String, SymbolInfo> = HashMap::new();
    let mut name_index: HashMap<String, Vec<String>> = HashMap::new();
    let mut name_to_qname: HashMap<String, Vec<String>> = HashMap::new();
    let mut file_symbols: HashMap<String, HashSet<String>> = HashMap::new();
    let mut file_local_qname: HashMap<String, HashMap<String, String>> = HashMap::new();
    let mut suffix_index: HashMap<String, Vec<String>> = HashMap::new();

    for s in symbols {
        let qname = &s.qualified_name;
        if qname.is_empty() {
            continue;
        }

        all_symbols_map.insert(qname.clone(), SymbolInfo {
            id: s.id,
            name: s.name.clone(),
            qualified_name: s.qualified_name.clone(),
            kind: s.kind.clone(),
            file_instance_id: s.file_instance_id,
            rel_path: s.rel_path.clone(),
        });

        // simple_name = qualified_name 最后一段（:: → . 后 rsplit）
        let norm_qname = qname.replace("::", ".");
        let simple_name = match norm_qname.rfind('.') {
            Some(idx) => norm_qname[idx + 1..].to_string(),
            None => norm_qname.clone(),
        };

        name_index.entry(simple_name.clone()).or_default().push(qname.clone());
        name_to_qname.entry(s.name.clone()).or_default().push(qname.clone());

        let file_set = file_symbols.entry(s.rel_path.clone()).or_default();
        file_set.insert(simple_name.clone());

        let local_map = file_local_qname.entry(s.rel_path.clone()).or_default();
        // 后写覆盖前写（与 Python 一致）
        local_map.insert(simple_name, qname.clone());

        // 后缀索引（含前导点）
        let norm_parts: Vec<&str> = norm_qname.split('.').collect();
        for i in 0..norm_parts.len() {
            let suffix = format!(".{}", norm_parts[i..].join("."));
            suffix_index.entry(suffix).or_default().push(qname.clone());
        }
    }

    SymbolIndexes {
        all_symbols_map,
        name_index,
        name_to_qname,
        file_symbols,
        file_local_qname,
        suffix_index,
    }
}

/// 构建外部符号索引（ext_by_qname + ext_by_name）
fn build_ext_indexes(symbols: &[ExtSymbolInfo]) -> ExtIndexes {
    let mut ext_by_qname: HashMap<String, ExtSymbolInfo> = HashMap::new();
    let mut ext_by_name: HashMap<String, Vec<ExtSymbolInfo>> = HashMap::new();

    for s in symbols {
        if !s.qualified_name.is_empty() {
            ext_by_qname.insert(s.qualified_name.clone(), ExtSymbolInfo {
                id: s.id,
                symbol_name: s.symbol_name.clone(),
                qualified_name: s.qualified_name.clone(),
                package_name: s.package_name.clone(),
            });
        }
        ext_by_name.entry(s.symbol_name.clone()).or_default().push(ExtSymbolInfo {
            id: s.id,
            symbol_name: s.symbol_name.clone(),
            qualified_name: s.qualified_name.clone(),
            package_name: s.package_name.clone(),
        });
    }

    ExtIndexes {
        ext_by_qname,
        ext_by_name,
    }
}

/// 从 DB 构建 IdMaps（qname_id_map + file_sym_id_map）
///
/// 与 Python 一致：
/// - 外部符号先加载（负 id）
/// - 项目符号后加载（正 id，覆盖外部同名）
/// - 项目符号还构建 file_sym_id_map[file_instance_id][name] = id
fn build_id_maps_from_db(conn: &rusqlite::Connection) -> Result<IdMaps, rusqlite::Error> {
    let mut qname_id_map: HashMap<String, i64> = HashMap::new();
    let mut file_sym_id_map: HashMap<i64, HashMap<String, i64>> = HashMap::new();

    // 1. 外部符号（负 id）
    //    注意：外部符号信息由调用方传入，这里只查 DB 构建 qname_id_map
    //    但 Python 是从 DB 一次性读 external_symbols 表 → 这里也走 DB
    let ext_result = conn.prepare(
        "SELECT id, qualified_name FROM external_symbols WHERE qualified_name != ''"
    );
    if let Ok(mut stmt) = ext_result {
        let rows = stmt.query_map([], |row| {
            Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?))
        })?;
        for row in rows {
            let (id, qname) = row?;
            qname_id_map.insert(qname, -id);
        }
    }
    // 表不存在时静默跳过（与 Python try/except 一致）

    // 2. 项目符号（正 id，覆盖外部同名）
    let mut stmt = conn.prepare(
        "SELECT id, name, qualified_name, file_instance_id FROM symbols"
    )?;
    let rows = stmt.query_map([], |row| {
        Ok((
            row.get::<_, i64>(0)?,
            row.get::<_, String>(1)?,
            row.get::<_, String>(2)?,
            row.get::<_, i64>(3)?,
        ))
    })?;
    for row in rows {
        let (id, name, qname, fi_id) = row?;
        if !qname.is_empty() {
            qname_id_map.insert(qname, id);
        }
        file_sym_id_map.entry(fi_id).or_default().insert(name, id);
    }

    Ok(IdMaps {
        qname_id_map,
        file_sym_id_map,
    })
}

// ===========================================================================
// 5 策略 resolve
// ===========================================================================

/// 对单条 call 执行 5 策略 resolve（与 Python 一致）
///
/// 参数：
/// - callee_name, callee_module: 来自 raw_call
/// - rel_path: 当前文件相对路径
/// - sym_idx: 符号索引
/// - ext_idx: 外部符号索引
/// - imports: 当前文件的 import 映射（alias → full_module）
fn resolve_call(
    callee_name: &str,
    callee_module: &str,
    rel_path: &str,
    sym_idx: &SymbolIndexes,
    ext_idx: &ExtIndexes,
    imports: &HashMap<String, String>,
) -> ResolveResult {
    let mut callee_qname = String::new();
    let mut callee_file = String::new();
    let mut callee_id: i64 = 0;
    let mut is_cross: i64 = 0;

    // 策略 1: 精确匹配 module.name
    if !callee_module.is_empty() {
        let test_qname = format!("{}.{}", callee_module, callee_name);
        if let Some(sym) = sym_idx.all_symbols_map.get(&test_qname) {
            callee_qname = test_qname;
            callee_file = sym.rel_path.clone();
            callee_id = sym.id;
            if callee_file != rel_path {
                is_cross = 1;
            }
        }
    }

    // 策略 2: import 映射
    if callee_qname.is_empty() && !callee_module.is_empty() {
        if let Some(full_mod) = imports.get(callee_module) {
            // 注意：必须把 replace 的结果绑定到变量，避免临时值在循环中被释放
            let normalized = full_mod.replace('/', ".");
            let mod_parts: Vec<&str> = normalized.split('.').collect();
            for i in 0..mod_parts.len() {
                let test_mod = mod_parts[i..].join(".");
                let test_qname = format!("{}.{}", test_mod, callee_name);
                if let Some(sym) = sym_idx.all_symbols_map.get(&test_qname) {
                    callee_qname = test_qname;
                    callee_file = sym.rel_path.clone();
                    callee_id = sym.id;
                    if callee_file != rel_path {
                        is_cross = 1;
                    }
                    break;
                }
            }
        }

        // 后缀索引匹配
        if callee_qname.is_empty() {
            let suffix = format!(".{}.{}", callee_module, callee_name);
            if let Some(qnames) = sym_idx.suffix_index.get(&suffix) {
                if let Some(qname) = qnames.first() {
                    let sym = &sym_idx.all_symbols_map[qname];
                    callee_qname = qname.clone();
                    callee_file = sym.rel_path.clone();
                    callee_id = sym.id;
                    if callee_file != rel_path {
                        is_cross = 1;
                    }
                }
            }
        }
    }

    // 策略 3: 简名唯一匹配
    if callee_qname.is_empty() {
        if let Some(candidates) = sym_idx.name_index.get(callee_name) {
            if candidates.len() == 1 {
                let qname = &candidates[0];
                let sym = &sym_idx.all_symbols_map[qname];
                callee_qname = qname.clone();
                callee_file = sym.rel_path.clone();
                callee_id = sym.id;
                if callee_file != rel_path {
                    is_cross = 1;
                }
            } else if candidates.len() > 1 {
                // 多候选：优先同文件
                if let Some(local_map) = sym_idx.file_local_qname.get(rel_path) {
                    if let Some(local_qname) = local_map.get(callee_name) {
                        let sym = &sym_idx.all_symbols_map[local_qname];
                        callee_qname = local_qname.clone();
                        callee_file = rel_path.to_string();
                        callee_id = sym.id;
                    }
                }
                // 如果同文件没有，且 callee_module 匹配某个候选的父级
                if callee_qname.is_empty() && !callee_module.is_empty() {
                    for qname in candidates {
                        let norm_qname = qname.replace("::", ".");
                        if let Some(idx) = norm_qname.rfind('.') {
                            let parent = &norm_qname[..idx];
                            if parent.ends_with(callee_module) {
                                let sym = &sym_idx.all_symbols_map[qname];
                                callee_qname = qname.clone();
                                callee_file = sym.rel_path.clone();
                                callee_id = sym.id;
                                if callee_file != rel_path {
                                    is_cross = 1;
                                }
                                break;
                            }
                        }
                    }
                }
            }
        }
    }

    // 策略 4: 同文件简名匹配
    if callee_qname.is_empty() {
        if let Some(file_set) = sym_idx.file_symbols.get(rel_path) {
            if file_set.contains(callee_name) {
                if let Some(local_map) = sym_idx.file_local_qname.get(rel_path) {
                    if let Some(local_qname) = local_map.get(callee_name) {
                        let sym = &sym_idx.all_symbols_map[local_qname];
                        callee_qname = local_qname.clone();
                        callee_file = rel_path.to_string();
                        callee_id = sym.id;
                    }
                }
            }
        }
    }

    // 策略 4.5: HCL 多段 name（symbol.name 直接匹配）
    if callee_qname.is_empty() && callee_name.contains('.') {
        if let Some(candidates) = sym_idx.name_to_qname.get(callee_name) {
            if candidates.len() == 1 {
                let qname = &candidates[0];
                let sym = &sym_idx.all_symbols_map[qname];
                callee_qname = qname.clone();
                callee_file = sym.rel_path.clone();
                callee_id = sym.id;
                if callee_file != rel_path {
                    is_cross = 1;
                }
            } else if candidates.len() > 1 {
                // 多候选：优先同文件
                let same_file_qnames: Vec<&String> = candidates.iter()
                    .filter(|q| sym_idx.all_symbols_map.get(*q)
                        .map(|s| s.rel_path == rel_path)
                        .unwrap_or(false))
                    .collect();
                if !same_file_qnames.is_empty() {
                    let qname = same_file_qnames[0];
                    let sym = &sym_idx.all_symbols_map[qname];
                    callee_qname = qname.clone();
                    callee_file = rel_path.to_string();
                    callee_id = sym.id;
                }
            }
        }
    }

    // 策略 5: external_symbols
    if callee_qname.is_empty() {
        if !callee_module.is_empty() {
            let test_qname = format!("{}.{}", callee_module, callee_name);
            if let Some(ext_sym) = ext_idx.ext_by_qname.get(&test_qname) {
                callee_qname = test_qname;
                callee_file = format!("external://{}", ext_sym.package_name);
                callee_id = -ext_sym.id;
                is_cross = 1;
            }
        } else {
            if let Some(ext_syms) = ext_idx.ext_by_name.get(callee_name) {
                if ext_syms.len() == 1 {
                    let ext_sym = &ext_syms[0];
                    callee_qname = ext_sym.qualified_name.clone();
                    callee_file = format!("external://{}", ext_sym.package_name);
                    callee_id = -ext_sym.id;
                    is_cross = 1;
                }
            }
        }
    }

    ResolveResult {
        callee_qname,
        callee_file,
        callee_id,
        is_cross_file: is_cross,
    }
}

/// 从 caller_name 提取 simple_name（去 :: . # 分隔符后）
///
/// 与 Python 一致：
/// ```python
/// for sep in ("::", ".", "#"):
///     if sep in simple_name:
///         simple_name = simple_name.rsplit(sep, 1)[-1]
/// ```
fn extract_simple_name(caller_name: &str) -> &str {
    let mut s = caller_name;
    for sep in &["::", ".", "#"] {
        if let Some(idx) = s.rfind(sep) {
            s = &s[idx + sep.len()..];
        }
    }
    s
}

// ===========================================================================
// SQL 写入
// ===========================================================================

/// 批量 resolve + 写入 calls + call_versions
///
/// 步骤（与 Python 一致）：
/// 1. 构建 IdMaps（从 DB 读取 symbols + external_symbols 的 id）
/// 2. 对每个 file_result 中的 raw_call 执行 5 策略 resolve
/// 3. 收集 calls_to_insert + call_versions_to_insert
/// 4. DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id IN (?))
///    分批 500（避免 SQLite 999 参数限制）
/// 5. INSERT calls（loop execute，顺序与 Python executemany 一致）
/// 6. INSERT call_versions（loop execute）
fn batch_resolve_and_save_calls_inner(
    conn: &rusqlite::Connection,
    file_results: &[FileInfo],
    sym_idx: &SymbolIndexes,
    ext_idx: &ExtIndexes,
    changed_file_instance_ids: &[i64],
) -> Result<BatchSaveCallsResult, rusqlite::Error> {
    let mut result = BatchSaveCallsResult::default();

    if file_results.is_empty() {
        // C14: 空文件列表，无副作用
        return Ok(result);
    }

    // 1. 构建 IdMaps（从 DB）
    let id_maps = build_id_maps_from_db(conn)?;

    // 2. 收集 calls_to_insert + call_versions_to_insert
    let mut calls_to_insert: Vec<(
        i64,           // caller_id
        String,        // caller_name
        String,        // caller_module
        String,        // callee_name
        String,        // callee_module
        String,        // callee_qualified
        String,        // callee_file
        i64,           // callee_id (resolved)
        i64,           // call_line
        i64,           // is_cross_file
    )> = Vec::new();
    let mut call_versions_to_insert: Vec<(
        i64,           // file_version_id
        String,        // caller_qualified
        String,        // caller_hash
        String,        // callee_name
        String,        // callee_module
        String,        // callee_qualified
        String,        // callee_file
        i64,           // call_line
        i64,           // is_cross_file
    )> = Vec::new();

    for file_info in file_results {
        result.files_processed += 1;
        let fi_id = file_info.file_instance_id;
        let fv_id = file_info.file_version_id;
        let mod_path = &file_info.module_path;
        let rel_path = &file_info.rel_path;
        let imports = &file_info.imports;
        let fn_hash_map = &file_info.fn_hash_map;

        let sym_id_map_fi = id_maps.file_sym_id_map.get(&fi_id);

        for raw in &file_info.raw_calls {
            result.total_calls += 1;

            let callee_name = &raw.callee_name;
            let callee_module = &raw.callee_module;
            let caller_name_raw = &raw.caller_name;
            let caller_qualified_orig = &raw.caller_qualified;

            // 5 策略 resolve
            let resolved = if callee_name.is_empty() {
                // C6: 空 callee_name，调用 _make_call_entry(raw, "", "", 0, 0)
                ResolveResult {
                    callee_qname: String::new(),
                    callee_file: String::new(),
                    callee_id: 0,
                    is_cross_file: 0,
                }
            } else {
                resolve_call(callee_name, callee_module, rel_path, sym_idx, ext_idx, imports)
            };

            if !resolved.callee_qname.is_empty() {
                result.resolved_count += 1;
            }

            // caller_id 多级 fallback
            let mut caller_id: i64 = 0;
            if !caller_qualified_orig.is_empty() {
                if let Some(&id) = id_maps.qname_id_map.get(caller_qualified_orig) {
                    caller_id = id;
                }
            }
            if caller_id == 0 && !caller_name_raw.is_empty() {
                if let Some(&id) = id_maps.qname_id_map.get(caller_name_raw) {
                    caller_id = id;
                }
            }
            if caller_id == 0 && !caller_name_raw.is_empty() {
                let simple_name = extract_simple_name(caller_name_raw);
                if let Some(fi_map) = sym_id_map_fi {
                    if let Some(&id) = fi_map.get(simple_name) {
                        caller_id = id;
                    }
                }
            }

            // call_versions：始终写入（不管 caller_id 是否为 0）
            // 推导 caller_qualified
            let caller_qualified_final = if !caller_qualified_orig.is_empty() {
                caller_qualified_orig.clone()
            } else if !caller_name_raw.is_empty() {
                format!("{}::{}", mod_path, caller_name_raw)
            } else {
                String::new()
            };
            let caller_hash = fn_hash_map.get(&caller_qualified_final).cloned().unwrap_or_default();

            // caller_id == 0 → 跳过 calls 表插入（与 Python 一致）
            if caller_id != 0 {
                let callee_q = &resolved.callee_qname;
                let callee_id_resolved = if callee_q.is_empty() {
                    0
                } else {
                    *id_maps.qname_id_map.get(callee_q).unwrap_or(&0)
                };
                calls_to_insert.push((
                    caller_id,
                    caller_name_raw.clone(),
                    raw.caller_module.clone(),
                    callee_name.clone(),
                    callee_module.clone(),
                    resolved.callee_qname.clone(),
                    resolved.callee_file.clone(),
                    callee_id_resolved,
                    raw.call_line,
                    resolved.is_cross_file,
                ));
            }

            call_versions_to_insert.push((
                fv_id,
                caller_qualified_final,
                caller_hash,
                callee_name.clone(),
                callee_module.clone(),
                resolved.callee_qname.clone(),
                resolved.callee_file.clone(),
                raw.call_line,
                resolved.is_cross_file,
            ));
        }
    }

    // 3. DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id IN (?))
    //    分批 500（与 Python 一致）
    if !changed_file_instance_ids.is_empty() {
        const BATCH: usize = 500;
        let mut deleted_total: usize = 0;
        for chunk in changed_file_instance_ids.chunks(BATCH) {
            let placeholders = std::iter::repeat("?")
                .take(chunk.len())
                .collect::<Vec<_>>()
                .join(",");
            let sql = format!(
                "DELETE FROM calls WHERE caller_id IN \
                 (SELECT id FROM symbols WHERE file_instance_id IN ({}))",
                placeholders
            );
            let params_iter: Vec<&dyn rusqlite::ToSql> = chunk
                .iter()
                .map(|x| x as &dyn rusqlite::ToSql)
                .collect();
            let deleted = conn.execute(&sql, params_iter.as_slice())?;
            deleted_total += deleted;
        }
        result.old_calls_deleted = deleted_total;
    }

    // 4. INSERT calls（loop execute，顺序与 Python executemany 一致）
    if !calls_to_insert.is_empty() {
        let sql = "INSERT INTO calls \
                   (caller_id, caller_name, caller_module, callee_name, \
                    callee_module, callee_qualified, callee_file, callee_id, \
                    call_line, is_cross_file) \
                   VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10)";
        for call in &calls_to_insert {
            conn.execute(sql, params![
                call.0, &call.1, &call.2, &call.3, &call.4,
                &call.5, &call.6, call.7, call.8, call.9,
            ])?;
        }
        result.calls_inserted = calls_to_insert.len();
    }

    // 5. INSERT call_versions
    if !call_versions_to_insert.is_empty() {
        let sql = "INSERT INTO call_versions \
                   (file_version_id, caller_qualified, caller_hash, callee_name, \
                    callee_module, callee_qualified, callee_file, call_line, is_cross_file) \
                   VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9)";
        for cv in &call_versions_to_insert {
            conn.execute(sql, params![
                cv.0, &cv.1, &cv.2, &cv.3, &cv.4,
                &cv.5, &cv.6, cv.7, cv.8,
            ])?;
        }
        result.call_versions_inserted = call_versions_to_insert.len();
    }

    Ok(result)
}

// ===========================================================================
// PyO3 暴露
// ===========================================================================

/// 批量 resolve + 写入 calls + call_versions
///
/// 与 Python `db_build.py:_build_call_graph_multi_lang` 行为一致：
/// - 5 策略 resolve（精确/import/简名/同文件/HCL/external）
/// - caller_id 多级 fallback
/// - DELETE calls 分批 500
/// - INSERT calls + call_versions 顺序与 Python 一致
///
/// 事务边界：BEGIN IMMEDIATE → 全部 SQL → COMMIT
/// 失败：返回 dict {"success": False, "error": str(e)}，不抛异常
#[pyfunction]
#[pyo3(signature = (
    codegraph_db_path,
    workspace_id,
    file_results,
    all_symbols,
    external_symbols,
    changed_file_instance_ids,
))]
#[allow(clippy::too_many_arguments)]
pub fn batch_resolve_and_save_calls<'py>(
    py: Python<'py>,
    codegraph_db_path: &str,
    workspace_id: i64,
    file_results: Vec<Bound<'py, PyDict>>,
    all_symbols: Vec<Bound<'py, PyDict>>,
    external_symbols: Vec<Bound<'py, PyDict>>,
    changed_file_instance_ids: Vec<i64>,
) -> PyResult<Bound<'py, PyDict>> {
    // workspace_id 当前仅作为 metadata（不写入，不校验）
    let _ = workspace_id;

    let dict = PyDict::new(py);
    dict.set_item("success", false)?;
    dict.set_item("total_calls", 0usize)?;
    dict.set_item("resolved_count", 0usize)?;
    dict.set_item("calls_inserted", 0usize)?;
    dict.set_item("call_versions_inserted", 0usize)?;
    dict.set_item("old_calls_deleted", 0usize)?;
    dict.set_item("files_processed", 0usize)?;
    dict.set_item("error", ())?;

    // 1. 提取 FileInfo + SymbolInfo + ExtSymbolInfo（在 GIL 内）
    let file_infos: Vec<FileInfo> = match file_results
        .iter()
        .map(|d| extract_file_info(d))
        .collect::<PyResult<Vec<_>>>()
    {
        Ok(v) => v,
        Err(e) => {
            dict.set_item("success", false)?;
            dict.set_item("error", format!("提取 file_results 字段失败: {}", e))?;
            return Ok(dict);
        }
    };

    let all_symbols_info: Vec<SymbolInfo> = match all_symbols
        .iter()
        .map(|d| extract_symbol_info(d))
        .collect::<PyResult<Vec<_>>>()
    {
        Ok(v) => v,
        Err(e) => {
            dict.set_item("success", false)?;
            dict.set_item("error", format!("提取 all_symbols 字段失败: {}", e))?;
            return Ok(dict);
        }
    };

    let ext_symbols_info: Vec<ExtSymbolInfo> = match external_symbols
        .iter()
        .map(|d| extract_ext_symbol_info(d))
        .collect::<PyResult<Vec<_>>>()
    {
        Ok(v) => v,
        Err(e) => {
            dict.set_item("success", false)?;
            dict.set_item("error", format!("提取 external_symbols 字段失败: {}", e))?;
            return Ok(dict);
        }
    };

    // 2. 释放 GIL 做索引构建 + SQL 操作
    let result = py.detach(|| -> Result<BatchSaveCallsResult, String> {
        // 构建索引
        let sym_idx = build_symbol_indexes(&all_symbols_info);
        let ext_idx = build_ext_indexes(&ext_symbols_info);

        // 打开 DB + BEGIN IMMEDIATE
        let conn = open_readwrite(codegraph_db_path)
            .map_err(|e| format!("打开 CodeGraph 数据库失败: {}", e))?;

        conn.execute_batch("BEGIN IMMEDIATE;")
            .map_err(|e| format!("BEGIN IMMEDIATE 失败: {}", e))?;

        let inner_result = batch_resolve_and_save_calls_inner(
            &conn,
            &file_infos,
            &sym_idx,
            &ext_idx,
            &changed_file_instance_ids,
        );

        match inner_result {
            Ok(r) => {
                conn.execute_batch("COMMIT;")
                    .map_err(|e| format!("COMMIT 失败: {}", e))?;
                Ok(r)
            }
            Err(e) => {
                let _ = conn.execute_batch("ROLLBACK;");
                Err(format!("batch_resolve_and_save_calls 失败: {}", e))
            }
        }
    });

    match result {
        Ok(r) => {
            dict.set_item("success", true)?;
            dict.set_item("total_calls", r.total_calls)?;
            dict.set_item("resolved_count", r.resolved_count)?;
            dict.set_item("calls_inserted", r.calls_inserted)?;
            dict.set_item("call_versions_inserted", r.call_versions_inserted)?;
            dict.set_item("old_calls_deleted", r.old_calls_deleted)?;
            dict.set_item("files_processed", r.files_processed)?;
            dict.set_item("error", ())?;
        }
        Err(e) => {
            dict.set_item("success", false)?;
            dict.set_item("error", e)?;
        }
    }

    Ok(dict)
}

/// 模块注册入口（供 lib.rs 调用）
pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(batch_resolve_and_save_calls, m)?)?;
    Ok(())
}
