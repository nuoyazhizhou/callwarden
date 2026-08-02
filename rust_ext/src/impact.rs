//! Phase 6-1 P2/P3: cross_layer_impact + defect_correlation Rust 核心
//!
//! 对齐 Python 实现：
//! - `db/db_impact.py::cross_layer_impact` (L553-L697)
//! - `db/db_evolution.py::defect_correlation` (L311-L447)
//!
//! ## cross_layer_impact
//! 分析源符号对四个层面的潜在影响：
//! - 代码层（code）：调用方（由 Python 通过 SQL 预查询后传入）
//! - DB 层（db）：从 content 用正则提取 SQL 表名
//!   （FROM / UPDATE / INSERT INTO / DELETE FROM）
//! - API 层（api）：函数名关键词检测 + HTTP 注解正则 + 路由装饰器正则
//! - 配置层（config）：从 content 用正则提取配置项引用
//!   （env::var / std::env::var / config.get）
//!
//! ## defect_correlation
//! 统计符号变更后 window_commits 次提交内引入的缺陷数量：
//! - 对每个文件的每个变更点，找到后续 window_commits 个版本
//! - 查询这些版本的 content_hash 对应的 semgrep_findings
//! - 用 seen_finding_ids set 去重
//! - 补充通过 symbol_qualified 直接关联的缺陷
//!
//! 性能优化：
//! - once_cell::sync::Lazy 缓存编译后的正则（避免每次调用重新编译）
//! - rustc_hash::FxHashMap 替代 std HashMap（非加密哈希，比 SipHash 快 5-10x）
//! - BTreeSet 自动排序去重（对齐 Python sorted(set(...)) 语义）

use std::collections::BTreeSet;

use once_cell::sync::Lazy;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use regex::Regex;
use rustc_hash::FxHashMap;

// ============================================
// 正则模式缓存（与 Python 完全对齐）
// ============================================

/// DB 层 SQL 表名提取正则（IGNORECASE，对齐 Python sql_patterns）
///
/// Python:
/// ```python
/// sql_patterns = [
///     re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE),
///     re.compile(r"\bUPDATE\s+(\w+)", re.IGNORECASE),
///     re.compile(r"\bINSERT\s+INTO\s+(\w+)", re.IGNORECASE),
///     re.compile(r"\bDELETE\s+FROM\s+(\w+)", re.IGNORECASE),
/// ]
/// ```
static SQL_PATTERNS: Lazy<Vec<Regex>> = Lazy::new(|| {
    vec![
        Regex::new(r"(?i)\bFROM\s+(\w+)").unwrap(),
        Regex::new(r"(?i)\bUPDATE\s+(\w+)").unwrap(),
        Regex::new(r"(?i)\bINSERT\s+INTO\s+(\w+)").unwrap(),
        Regex::new(r"(?i)\bDELETE\s+FROM\s+(\w+)").unwrap(),
    ]
});

/// API 层 HTTP 方法注解正则（IGNORECASE，对齐 Python http_annotation）
///
/// Python:
/// ```python
/// http_annotation = re.search(
///     r"#\[(?:get|post|put|delete|patch|head|options)\s*\(",
///     content,
///     re.IGNORECASE,
/// )
/// ```
static HTTP_ANNOTATION: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)#\[(?:get|post|put|delete|patch|head|options)\s*\(").unwrap());

/// API 层路由装饰器正则（IGNORECASE，对齐 Python route_decorator）
///
/// Python:
/// ```python
/// route_decorator = re.search(
///     r"@\w+\.(?:route|get|post|put|delete|patch)\s*\(",
///     content,
///     re.IGNORECASE,
/// )
/// ```
static ROUTE_DECORATOR: Lazy<Regex> =
    Lazy::new(|| Regex::new(r"(?i)@\w+\.(?:route|get|post|put|delete|patch)\s*\(").unwrap());

/// 配置层配置项提取正则（区分大小写，对齐 Python config_patterns）
///
/// 注意：Python 此组正则未使用 re.IGNORECASE，Rust 侧也不加 (?i)
///
/// Python:
/// ```python
/// config_patterns = [
///     re.compile(r"env::var\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
///     re.compile(r"std::env::var\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
///     re.compile(r"config\.get\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
/// ]
/// ```
static CONFIG_PATTERNS: Lazy<Vec<Regex>> = Lazy::new(|| {
    vec![
        Regex::new(r#"env::var\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"#).unwrap(),
        Regex::new(r#"std::env::var\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"#).unwrap(),
        Regex::new(r#"config\.get\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"#).unwrap(),
    ]
});

// ============================================
// 数据结构定义
// ============================================

/// 代码层条目（对应 Python code_layer 的 dict 元素）
///
/// Python:
/// ```python
/// code_layer.append({
///     "qualified_name": r["qualified_name"],
///     "name": r["name"],
///     "module_path": r["module_path"],
///     "visibility": r["visibility"],
///     "kind": r["kind"],
///     "file_path": r["rel_path"],
/// })
/// ```
#[derive(Clone, Debug)]
pub struct CodeLayerEntry {
    pub qualified_name: String,
    pub name: String,
    pub module_path: String,
    pub visibility: String,
    pub kind: String,
    pub file_path: String,
}

/// DB 层条目（对应 Python db_layer 的 dict 元素）
#[derive(Clone, Debug)]
pub struct DbLayerEntry {
    pub table: String,
    pub source: String,
}

/// API 层条目（对应 Python api_layer 的 dict 元素）
#[derive(Clone, Debug)]
pub struct ApiLayerEntry {
    pub symbol: String,
    pub name: String,
    pub reason: String,
}

/// 配置层条目（对应 Python config_layer 的 dict 元素）
#[derive(Clone, Debug)]
pub struct ConfigLayerEntry {
    pub config_key: String,
    pub source: String,
}

/// cross_layer_impact 结果（对应 Python 返回的四个层 dict）
#[derive(Clone, Debug, Default)]
pub struct CrossLayerImpactResult {
    pub code: Vec<CodeLayerEntry>,
    pub db: Vec<DbLayerEntry>,
    pub api: Vec<ApiLayerEntry>,
    pub config: Vec<ConfigLayerEntry>,
}

/// 文件版本信息（用于 defect_correlation 窗口切片）
///
/// 对齐 Python all_versions 的每行：
/// ```python
/// {"id": ..., "version_num": ..., "content_hash": ..., "parsed_at": ...}
/// ```
/// Rust 侧不需要 parsed_at（after_change_at 由调用方按需补充）
#[derive(Clone, Debug)]
pub struct VersionInfo {
    pub version_num: i64,
    pub content_hash: String,
}

/// 缺陷发现信息（对应 Python semgrep_findings 行）
///
/// 对齐 Python defect_findings 的 dict 元素：
/// ```python
/// {
///     "rule_id": ..., "rule_name": ..., "severity": ...,
///     "start_line": ..., "end_line": ..., "scanned_at": ...,
///     "message": ..., "after_change_at": ...
/// }
/// ```
#[derive(Clone, Debug)]
pub struct FindingInfo {
    pub id: i64,
    pub rule_id: String,
    pub rule_name: String,
    pub severity: String,
    pub start_line: i64,
    pub end_line: i64,
    pub scanned_at: f64,
    pub message: String,
    /// 变更后时间戳：窗口发现时 Python 设为 change["parsed_at"]；
    /// Rust 侧无 parsed_at 字段，保持 0.0。
    /// 直接关联发现 Python 设为 0.0。
    pub after_change_at: f64,
}

/// defect_correlation 结果
#[derive(Clone, Debug, Default)]
pub struct DefectCorrelationResult {
    pub total_changes: i64,
    pub defects_after_change: i64,
    pub defect_types: Vec<(String, i64)>,
    pub findings: Vec<FindingInfo>,
}

// ============================================
// cross_layer_impact 核心实现
// ============================================

/// 跨层影响分析核心（对齐 Python db_impact.py::cross_layer_impact L553-L697）
///
/// 分析源符号对四个层面的潜在影响：
/// - 代码层：直接返回 Python 预查询的调用方列表（depth=1，不递归）
/// - DB 层：从 content 用 4 种正则提取 SQL 表名，排序去重后输出
/// - API 层：函数名关键词检测 + HTTP 注解正则 + 路由装饰器正则
/// - 配置层：从 content 用 3 种正则提取配置项引用，排序去重后输出
///
/// # 参数
/// - `source_qn`: 源符号限定名
/// - `source_name`: 源符号名（用于 API 层关键词检测）
/// - `content`: 符号源码内容（用于 DB/API/config 层正则提取）
/// - `code_layer`: Python 预查询的调用方列表（SQL 反向查找 depth=1）
///
/// # 返回
/// `CrossLayerImpactResult` 包含 code / db / api / config 四个 Vec
pub fn cross_layer_impact_core(
    source_qn: &str,
    source_name: &str,
    content: &str,
    code_layer: Vec<CodeLayerEntry>,
) -> CrossLayerImpactResult {
    // ---- DB 层：从 content 中正则提取 SQL 表名 ----
    // 对齐 Python: table_names = set()
    //              for pat in sql_patterns:
    //                  for m in pat.finditer(content):
    //                      table_names.add(m.group(1))
    //              for tbl in sorted(table_names):
    //                  db_layer.append({"table": tbl, "source": source_qn})
    // BTreeSet 自动排序去重，等价于 Python 的 sorted(set(...))
    let mut table_names = BTreeSet::new();
    for pat in SQL_PATTERNS.iter() {
        for cap in pat.captures_iter(content) {
            if let Some(m) = cap.get(1) {
                table_names.insert(m.as_str().to_string());
            }
        }
    }
    let db_layer: Vec<DbLayerEntry> = table_names
        .into_iter()
        .map(|tbl| DbLayerEntry {
            table: tbl,
            source: source_qn.to_string(),
        })
        .collect();

    // ---- API 层：函数名关键词 + HTTP 注解 + 路由装饰器 ----
    // 对齐 Python: name_lower = source_name.lower()
    //              is_api_name = "route" in name_lower or "handler" in name_lower or "endpoint" in name_lower
    let name_lower = source_name.to_lowercase();
    let is_api_name = name_lower.contains("route")
        || name_lower.contains("handler")
        || name_lower.contains("endpoint");
    // 对齐 Python: http_annotation = re.search(r"#\[(?:get|post|...)\s*\(", content, re.IGNORECASE)
    let has_http_annotation = HTTP_ANNOTATION.is_match(content);
    // 对齐 Python: route_decorator = re.search(r"@\w+\.(?:route|get|...)\s*\(", content, re.IGNORECASE)
    let has_route_decorator = ROUTE_DECORATOR.is_match(content);

    let mut api_layer: Vec<ApiLayerEntry> = Vec::new();
    // 对齐 Python: if is_api_name or http_annotation or route_decorator:
    //                  reasons = []
    //                  if is_api_name: reasons.append("function_name_keyword")
    //                  if http_annotation: reasons.append("http_method_annotation")
    //                  if route_decorator: reasons.append("route_decorator")
    //                  api_layer.append({"symbol": source_qn, "name": source_name, "reason": ",".join(reasons)})
    if is_api_name || has_http_annotation || has_route_decorator {
        let mut reasons: Vec<&str> = Vec::new();
        if is_api_name {
            reasons.push("function_name_keyword");
        }
        if has_http_annotation {
            reasons.push("http_method_annotation");
        }
        if has_route_decorator {
            reasons.push("route_decorator");
        }
        api_layer.push(ApiLayerEntry {
            symbol: source_qn.to_string(),
            name: source_name.to_string(),
            reason: reasons.join(","),
        });
    }

    // ---- 配置层：从 content 中正则提取配置项引用 ----
    // 对齐 Python: config_keys = set()
    //              for pat in config_patterns:
    //                  for m in pat.finditer(content):
    //                      config_keys.add(m.group(1))
    //              for key in sorted(config_keys):
    //                  config_layer.append({"config_key": key, "source": source_qn})
    // 注意：Python 此组正则未使用 re.IGNORECASE，Rust 侧也不加 (?i)
    let mut config_keys = BTreeSet::new();
    for pat in CONFIG_PATTERNS.iter() {
        for cap in pat.captures_iter(content) {
            if let Some(m) = cap.get(1) {
                config_keys.insert(m.as_str().to_string());
            }
        }
    }
    let config_layer: Vec<ConfigLayerEntry> = config_keys
        .into_iter()
        .map(|key| ConfigLayerEntry {
            config_key: key,
            source: source_qn.to_string(),
        })
        .collect();

    CrossLayerImpactResult {
        code: code_layer,
        db: db_layer,
        api: api_layer,
        config: config_layer,
    }
}

// ============================================
// defect_correlation 核心实现
// ============================================

/// 变更-缺陷关联分析核心（对齐 Python db_evolution.py::defect_correlation L311-L447）
///
/// 统计符号变更后 window_commits 次提交内引入的缺陷数量：
/// 1. 对每个文件的每个变更点，找到后续 window_commits 个版本
/// 2. 查询这些版本的 content_hash 对应的 semgrep_findings
/// 3. 用 seen_finding_ids set 去重
/// 4. 补充通过 symbol_qualified 直接关联的缺陷
///
/// # 参数
/// - `changes_by_file`: (file_instance_id, Vec<version_num>) — 符号出现过的变更版本
/// - `all_versions_by_file`: (file_instance_id, Vec<VersionInfo>) — 每个文件的所有版本（按 version_num 排序）
/// - `findings_by_file_hash`: (file_instance_id, content_hash, Vec<FindingInfo>) — 预查询的 semgrep_findings
/// - `direct_findings`: 通过 symbol_qualified 直接关联的缺陷
/// - `window_commits`: 变更后观察的提交窗口数
///
/// # 返回
/// `DefectCorrelationResult` 包含 total_changes / defects_after_change / defect_types / findings
pub fn defect_correlation_core(
    changes_by_file: &[(i64, Vec<i64>)],
    all_versions_by_file: &[(i64, Vec<VersionInfo>)],
    findings_by_file_hash: &[(i64, String, Vec<FindingInfo>)],
    direct_findings: Vec<FindingInfo>,
    window_commits: usize,
) -> DefectCorrelationResult {
    // 对齐 Python: total_changes = sum(len(v) for v in changes_by_file.values())
    let total_changes: i64 = changes_by_file
        .iter()
        .map(|(_, versions)| versions.len() as i64)
        .sum();

    // 构建 file_instance_id -> Vec<VersionInfo> 查找函数
    // 对齐 Python: all_versions = cur.fetchall()（按 version_num ASC 排序）
    let find_versions = |fid: i64| -> Option<&Vec<VersionInfo>> {
        all_versions_by_file
            .iter()
            .find(|(id, _)| *id == fid)
            .map(|(_, v)| v)
    };

    // 构建 file_instance_id -> (version_num -> index) 映射
    // 对齐 Python: version_num_to_index = {v["version_num"]: idx for idx, v in enumerate(all_versions)}
    let mut version_index_by_file: FxHashMap<i64, FxHashMap<i64, usize>> = FxHashMap::default();
    for (fid, versions) in all_versions_by_file {
        let mut idx_map: FxHashMap<i64, usize> = FxHashMap::default();
        for (idx, v) in versions.iter().enumerate() {
            idx_map.insert(v.version_num, idx);
        }
        version_index_by_file.insert(*fid, idx_map);
    }

    // 构建 (file_instance_id, content_hash) -> Vec<&FindingInfo> 映射
    // Python 侧通过 SQL IN 查询，Rust 侧由调用方预查询后传入
    let mut findings_map: FxHashMap<(i64, String), Vec<&FindingInfo>> = FxHashMap::default();
    for (fid, content_hash, findings) in findings_by_file_hash {
        let key = (*fid, content_hash.clone());
        findings_map.entry(key).or_default().extend(findings.iter());
    }

    let mut defect_findings: Vec<FindingInfo> = Vec::new();
    let mut defect_types: FxHashMap<String, i64> = FxHashMap::default();
    let mut seen_finding_ids: FxHashMap<i64, ()> = FxHashMap::default();

    // 对齐 Python: for file_instance_id, change_versions in changes_by_file.items():
    for (file_instance_id, change_versions) in changes_by_file {
        let all_versions = match find_versions(*file_instance_id) {
            Some(v) => v,
            None => continue,
        };
        let version_num_to_index = match version_index_by_file.get(file_instance_id) {
            Some(m) => m,
            None => continue,
        };

        // 对齐 Python: for change in change_versions:
        for change_version_num in change_versions {
            // 对齐 Python: idx = version_num_to_index.get(change["version_num"])
            //              if idx is None: continue
            let idx = match version_num_to_index.get(change_version_num) {
                Some(&i) => i,
                None => continue,
            };
            // 对齐 Python: window_versions = all_versions[idx + 1: idx + 1 + window_commits]
            let window_start = idx + 1;
            let window_end = (idx + 1 + window_commits).min(all_versions.len());
            // 对齐 Python: window_hashes = [v["content_hash"] for v in window_versions if v["content_hash"]]
            let window_hashes: Vec<&String> = all_versions[window_start..window_end]
                .iter()
                .filter(|v| !v.content_hash.is_empty())
                .map(|v| &v.content_hash)
                .collect();

            for content_hash in window_hashes {
                let key = (*file_instance_id, content_hash.clone());
                if let Some(findings) = findings_map.get(&key) {
                    for f in findings {
                        // 对齐 Python: if fid in seen_finding_ids: continue
                        if seen_finding_ids.contains_key(&f.id) {
                            continue;
                        }
                        seen_finding_ids.insert(f.id, ());
                        // 对齐 Python: defect_types[frow["rule_id"]] += 1
                        *defect_types.entry(f.rule_id.clone()).or_insert(0) += 1;
                        // after_change_at: Python 设为 change["parsed_at"]
                        // Rust 侧无 parsed_at 字段，保持输入值（应为 0.0）
                        // f 是 &FindingInfo（findings_map 值为 Vec<&FindingInfo>），
                        // (*f).clone() 调用 FindingInfo::clone 返回 owned FindingInfo
                        defect_findings.push((*f).clone());
                    }
                }
            }
        }
    }

    // 补充：通过 symbol_qualified 直接关联的缺陷（不局限于窗口）
    // 对齐 Python: if qualified_name:
    //                  cur = self.conn.execute("SELECT ... WHERE symbol_qualified = ?", ...)
    for f in &direct_findings {
        if seen_finding_ids.contains_key(&f.id) {
            continue;
        }
        seen_finding_ids.insert(f.id, ());
        *defect_types.entry(f.rule_id.clone()).or_insert(0) += 1;
        // 对齐 Python: "after_change_at": 0.0
        let mut finding = f.clone();
        finding.after_change_at = 0.0;
        defect_findings.push(finding);
    }

    // defect_types 转为 Vec<(String, i64)>，按 key 排序保证输出稳定
    // （Python dict 是插入序，Rust 用 sorted 序，语义等价且确定性更好）
    let mut defect_types_vec: Vec<(String, i64)> = defect_types.into_iter().collect();
    defect_types_vec.sort_by(|a, b| a.0.cmp(&b.0));

    DefectCorrelationResult {
        total_changes,
        defects_after_change: defect_findings.len() as i64,
        defect_types: defect_types_vec,
        findings: defect_findings,
    }
}

// ============================================
// PyO3 暴露层
// ============================================

/// PyO3 包装：cross_layer_impact
///
/// Python 调用：
///   from callwarden_core import py_cross_layer_impact
///   result = py_cross_layer_impact(source_qn, source_name, content, code_layer)
///   # result = {"code": [...], "db": [...], "api": [...], "config": [...]}
///
/// # 参数
/// - `source_qn`: 源符号限定名
/// - `source_name`: 源符号名
/// - `content`: 符号源码内容
/// - `code_layer`: 调用方列表，每个元素为元组
///   (qualified_name, name, module_path, visibility, kind, file_path)
#[pyfunction]
pub fn py_cross_layer_impact<'py>(
    py: Python<'py>,
    source_qn: &str,
    source_name: &str,
    content: &str,
    code_layer: Vec<(String, String, String, String, String, String)>,
) -> PyResult<Bound<'py, PyDict>> {
    // 转换 code_layer 元组为 CodeLayerEntry
    let code_entries: Vec<CodeLayerEntry> = code_layer
        .into_iter()
        .map(
            |(qualified_name, name, module_path, visibility, kind, file_path)| CodeLayerEntry {
                qualified_name,
                name,
                module_path,
                visibility,
                kind,
                file_path,
            },
        )
        .collect();

    let result = cross_layer_impact_core(source_qn, source_name, content, code_entries);
    cross_layer_impact_to_pydict(py, &result)
}

/// 将 CrossLayerImpactResult 转为 Python dict
fn cross_layer_impact_to_pydict<'py>(
    py: Python<'py>,
    result: &CrossLayerImpactResult,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);

    // code 层：List[Dict]
    let code_list = PyList::new(
        py,
        result
            .code
            .iter()
            .map(|e| -> PyResult<Bound<'py, PyDict>> {
                let d = PyDict::new(py);
                d.set_item("qualified_name", e.qualified_name.clone())?;
                d.set_item("name", e.name.clone())?;
                d.set_item("module_path", e.module_path.clone())?;
                d.set_item("visibility", e.visibility.clone())?;
                d.set_item("kind", e.kind.clone())?;
                d.set_item("file_path", e.file_path.clone())?;
                Ok(d)
            })
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    dict.set_item("code", code_list)?;

    // db 层：List[Dict]
    let db_list = PyList::new(
        py,
        result
            .db
            .iter()
            .map(|e| -> PyResult<Bound<'py, PyDict>> {
                let d = PyDict::new(py);
                d.set_item("table", e.table.clone())?;
                d.set_item("source", e.source.clone())?;
                Ok(d)
            })
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    dict.set_item("db", db_list)?;

    // api 层：List[Dict]
    let api_list = PyList::new(
        py,
        result
            .api
            .iter()
            .map(|e| -> PyResult<Bound<'py, PyDict>> {
                let d = PyDict::new(py);
                d.set_item("symbol", e.symbol.clone())?;
                d.set_item("name", e.name.clone())?;
                d.set_item("reason", e.reason.clone())?;
                Ok(d)
            })
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    dict.set_item("api", api_list)?;

    // config 层：List[Dict]
    let config_list = PyList::new(
        py,
        result
            .config
            .iter()
            .map(|e| -> PyResult<Bound<'py, PyDict>> {
                let d = PyDict::new(py);
                d.set_item("config_key", e.config_key.clone())?;
                d.set_item("source", e.source.clone())?;
                Ok(d)
            })
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    dict.set_item("config", config_list)?;

    Ok(dict)
}

/// PyO3 包装：defect_correlation
///
/// Python 调用：
///   from callwarden_core import py_defect_correlation
///   result = py_defect_correlation(changes_by_file, all_versions_by_file,
///                                  findings_by_file_hash, direct_findings, window_commits)
///   # result = {"total_changes": int, "defects_after_change": int,
///   #           "defect_types": Dict[str, int], "findings": List[Dict]}
///
/// # 参数
/// - `changes_by_file`: List[Tuple[int, List[int]]] — (file_instance_id, Vec<version_num>)
/// - `all_versions_by_file`: List[Tuple[int, List[Tuple[int, str]]]] — (file_instance_id, Vec<(version_num, content_hash)>)
/// - `findings_by_file_hash`: List[Tuple[int, str, List[Tuple]]] — (file_instance_id, content_hash, Vec<FindingInfo_tuple>)
///   其中 FindingInfo_tuple = (id, rule_id, rule_name, severity, start_line, end_line, scanned_at, message)
/// - `direct_findings`: List[FindingInfo_tuple]
/// - `window_commits`: 变更后观察的提交窗口数
#[pyfunction]
#[allow(clippy::type_complexity)]
pub fn py_defect_correlation<'py>(
    py: Python<'py>,
    changes_by_file: Vec<(i64, Vec<i64>)>,
    all_versions_by_file: Vec<(i64, Vec<(i64, String)>)>,
    findings_by_file_hash: Vec<(
        i64,
        String,
        Vec<(i64, String, String, String, i64, i64, f64, String)>,
    )>,
    direct_findings: Vec<(i64, String, String, String, i64, i64, f64, String)>,
    window_commits: usize,
) -> PyResult<Bound<'py, PyDict>> {
    // 转换 all_versions_by_file 元组为 VersionInfo
    let all_versions: Vec<(i64, Vec<VersionInfo>)> = all_versions_by_file
        .into_iter()
        .map(|(fid, versions)| {
            let vis: Vec<VersionInfo> = versions
                .into_iter()
                .map(|(vn, ch)| VersionInfo {
                    version_num: vn,
                    content_hash: ch,
                })
                .collect();
            (fid, vis)
        })
        .collect();

    // 转换 findings_by_file_hash 元组为 FindingInfo
    let findings_by_hash: Vec<(i64, String, Vec<FindingInfo>)> = findings_by_file_hash
        .into_iter()
        .map(|(fid, ch, findings)| {
            let fis: Vec<FindingInfo> = findings
                .into_iter()
                .map(
                    |(
                        id,
                        rule_id,
                        rule_name,
                        severity,
                        start_line,
                        end_line,
                        scanned_at,
                        message,
                    )| {
                        FindingInfo {
                            id,
                            rule_id,
                            rule_name,
                            severity,
                            start_line,
                            end_line,
                            scanned_at,
                            message,
                            after_change_at: 0.0,
                        }
                    },
                )
                .collect();
            (fid, ch, fis)
        })
        .collect();

    // 转换 direct_findings 元组为 FindingInfo
    let direct: Vec<FindingInfo> = direct_findings
        .into_iter()
        .map(
            |(id, rule_id, rule_name, severity, start_line, end_line, scanned_at, message)| {
                FindingInfo {
                    id,
                    rule_id,
                    rule_name,
                    severity,
                    start_line,
                    end_line,
                    scanned_at,
                    message,
                    after_change_at: 0.0,
                }
            },
        )
        .collect();

    let result = defect_correlation_core(
        &changes_by_file,
        &all_versions,
        &findings_by_hash,
        direct,
        window_commits,
    );
    defect_correlation_to_pydict(py, &result)
}

/// 将 DefectCorrelationResult 转为 Python dict
fn defect_correlation_to_pydict<'py>(
    py: Python<'py>,
    result: &DefectCorrelationResult,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);

    dict.set_item("total_changes", result.total_changes)?;
    dict.set_item("defects_after_change", result.defects_after_change)?;

    // defect_types 转为 Dict[str, int]（对齐 Python dict(defect_types)）
    let defect_types_dict = PyDict::new(py);
    for (rule_id, count) in &result.defect_types {
        defect_types_dict.set_item(rule_id, count)?;
    }
    dict.set_item("defect_types", defect_types_dict)?;

    // findings 转为 List[Dict]（对齐 Python defect_findings）
    let findings_list = PyList::new(
        py,
        result
            .findings
            .iter()
            .map(|f| -> PyResult<Bound<'py, PyDict>> {
                let d = PyDict::new(py);
                d.set_item("rule_id", f.rule_id.clone())?;
                d.set_item("rule_name", f.rule_name.clone())?;
                d.set_item("severity", f.severity.clone())?;
                d.set_item("start_line", f.start_line)?;
                d.set_item("end_line", f.end_line)?;
                d.set_item("scanned_at", f.scanned_at)?;
                d.set_item("message", f.message.clone())?;
                d.set_item("after_change_at", f.after_change_at)?;
                Ok(d)
            })
            .collect::<PyResult<Vec<_>>>()?,
    )?;
    dict.set_item("findings", findings_list)?;

    Ok(dict)
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    // ---- cross_layer_impact_core 测试 ----

    #[test]
    fn test_cross_layer_impact_db_extraction() {
        let content = r#"
            SELECT * FROM users WHERE id = 1;
            UPDATE orders SET status = 'paid';
            INSERT INTO logs (msg) VALUES ('test');
            DELETE FROM temp_data;
        "#;
        let result = cross_layer_impact_core("mod.func", "func", content, vec![]);

        // 验证 4 个表名都被提取且排序（BTreeSet 自动排序）
        assert_eq!(result.db.len(), 4);
        let tables: Vec<&str> = result.db.iter().map(|e| e.table.as_str()).collect();
        assert_eq!(tables, vec!["logs", "orders", "temp_data", "users"]);
        // 验证 source 字段
        assert_eq!(result.db[0].source, "mod.func");
    }

    #[test]
    fn test_cross_layer_impact_db_case_insensitive() {
        // 验证 IGNORECASE：select from / UPDATE 等
        let content = "select * from Users; update ORDERS set x=1;";
        let result = cross_layer_impact_core("mod.func", "func", content, vec![]);
        assert_eq!(result.db.len(), 2);
        let tables: Vec<&str> = result.db.iter().map(|e| e.table.as_str()).collect();
        assert!(tables.contains(&"Users"));
        assert!(tables.contains(&"ORDERS"));
    }

    #[test]
    fn test_cross_layer_impact_db_dedup() {
        // 验证重复表名去重（对齐 Python set 语义）
        let content = "SELECT * FROM users; SELECT * FROM users;";
        let result = cross_layer_impact_core("mod.func", "func", content, vec![]);
        assert_eq!(result.db.len(), 1);
        assert_eq!(result.db[0].table, "users");
    }

    #[test]
    fn test_cross_layer_impact_api_name_keyword() {
        // 函数名含 route/handler/endpoint 关键词（对齐 Python 子串匹配）
        // 注意：Python 检查 "handler" 子串，"handle_request" 不含 "handler"
        let result =
            cross_layer_impact_core("mod.handler_func", "handler_func", "some content", vec![]);
        assert_eq!(result.api.len(), 1);
        assert!(result.api[0].reason.contains("function_name_keyword"));
    }

    #[test]
    fn test_cross_layer_impact_api_http_annotation() {
        let content = r#"
            #[get("/api/users")]
            fn get_users() {}
        "#;
        let result = cross_layer_impact_core("mod.func", "func", content, vec![]);
        assert_eq!(result.api.len(), 1);
        assert!(result.api[0].reason.contains("http_method_annotation"));
    }

    #[test]
    fn test_cross_layer_impact_api_route_decorator() {
        let content = r#"
            @app.route("/api/users")
            def list_users(): pass
        "#;
        let result = cross_layer_impact_core("mod.func", "func", content, vec![]);
        assert_eq!(result.api.len(), 1);
        assert!(result.api[0].reason.contains("route_decorator"));
    }

    #[test]
    fn test_cross_layer_impact_api_no_match() {
        let content = "fn normal_function() {}";
        let result = cross_layer_impact_core("mod.func", "func", content, vec![]);
        assert_eq!(result.api.len(), 0);
    }

    #[test]
    fn test_cross_layer_impact_config_extraction() {
        let content = r#"
            let db_url = env::var("DATABASE_URL");
            let host = std::env::var("HOST");
            let timeout = config.get("timeout");
        "#;
        let result = cross_layer_impact_core("mod.func", "func", content, vec![]);

        assert_eq!(result.config.len(), 3);
        let keys: Vec<&str> = result
            .config
            .iter()
            .map(|e| e.config_key.as_str())
            .collect();
        assert_eq!(keys, vec!["DATABASE_URL", "HOST", "timeout"]);
    }

    #[test]
    fn test_cross_layer_impact_config_case_sensitive() {
        // 验证 config 模式区分大小写（无 IGNORECASE）
        // ENV::var 不会匹配 env::var（因为 ENV != env）
        let content = r#"ENV::var("SHOULD_NOT_MATCH"); env::var("SHOULD_MATCH");"#;
        let result = cross_layer_impact_core("mod.func", "func", content, vec![]);
        assert_eq!(result.config.len(), 1);
        assert_eq!(result.config[0].config_key, "SHOULD_MATCH");
    }

    #[test]
    fn test_cross_layer_impact_code_passthrough() {
        let code_layer = vec![CodeLayerEntry {
            qualified_name: "mod.caller".to_string(),
            name: "caller".to_string(),
            module_path: "mod".to_string(),
            visibility: "public".to_string(),
            kind: "fn".to_string(),
            file_path: "src/caller.rs".to_string(),
        }];
        let result = cross_layer_impact_core("mod.func", "func", "", code_layer);
        assert_eq!(result.code.len(), 1);
        assert_eq!(result.code[0].qualified_name, "mod.caller");
    }

    #[test]
    fn test_cross_layer_impact_empty_content() {
        let result = cross_layer_impact_core("mod.func", "func", "", vec![]);
        assert!(result.db.is_empty());
        assert!(result.api.is_empty());
        assert!(result.config.is_empty());
    }

    // ---- defect_correlation_core 测试 ----

    #[test]
    fn test_defect_correlation_basic() {
        // 文件 1 有 2 个变更点（版本 1 和 3），后续窗口内有缺陷
        let changes_by_file = vec![(1, vec![1, 3])];
        let all_versions = vec![(
            1,
            vec![
                VersionInfo {
                    version_num: 1,
                    content_hash: "hash_v1".to_string(),
                },
                VersionInfo {
                    version_num: 2,
                    content_hash: "hash_v2".to_string(),
                },
                VersionInfo {
                    version_num: 3,
                    content_hash: "hash_v3".to_string(),
                },
                VersionInfo {
                    version_num: 4,
                    content_hash: "hash_v4".to_string(),
                },
                VersionInfo {
                    version_num: 5,
                    content_hash: "hash_v5".to_string(),
                },
            ],
        )];
        // hash_v2 和 hash_v4 有缺陷（在变更版本 1 和 3 的窗口内）
        let findings = vec![
            (
                1,
                "hash_v2".to_string(),
                vec![FindingInfo {
                    id: 100,
                    rule_id: "rule_a".to_string(),
                    rule_name: "Rule A".to_string(),
                    severity: "high".to_string(),
                    start_line: 10,
                    end_line: 20,
                    scanned_at: 1000.0,
                    message: "test".to_string(),
                    after_change_at: 0.0,
                }],
            ),
            (
                1,
                "hash_v4".to_string(),
                vec![FindingInfo {
                    id: 101,
                    rule_id: "rule_b".to_string(),
                    rule_name: "Rule B".to_string(),
                    severity: "low".to_string(),
                    start_line: 30,
                    end_line: 40,
                    scanned_at: 2000.0,
                    message: "test2".to_string(),
                    after_change_at: 0.0,
                }],
            ),
        ];

        let result = defect_correlation_core(&changes_by_file, &all_versions, &findings, vec![], 2);

        assert_eq!(result.total_changes, 2);
        assert_eq!(result.defects_after_change, 2);
        assert_eq!(result.findings.len(), 2);
        // defect_types 按 rule_id 排序
        assert_eq!(result.defect_types.len(), 2);
        assert_eq!(result.defect_types[0], ("rule_a".to_string(), 1));
        assert_eq!(result.defect_types[1], ("rule_b".to_string(), 1));
    }

    #[test]
    fn test_defect_correlation_dedup() {
        // 同一个 finding id 出现在多个窗口中，应去重
        let changes_by_file = vec![(1, vec![1, 2])];
        let all_versions = vec![(
            1,
            vec![
                VersionInfo {
                    version_num: 1,
                    content_hash: "hash_v1".to_string(),
                },
                VersionInfo {
                    version_num: 2,
                    content_hash: "hash_v2".to_string(),
                },
                VersionInfo {
                    version_num: 3,
                    content_hash: "hash_v3".to_string(),
                },
            ],
        )];
        // hash_v2 和 hash_v3 都有同一个 finding id（应去重）
        let findings = vec![
            (
                1,
                "hash_v2".to_string(),
                vec![FindingInfo {
                    id: 200,
                    rule_id: "rule_x".to_string(),
                    rule_name: "X".to_string(),
                    severity: "med".to_string(),
                    start_line: 1,
                    end_line: 5,
                    scanned_at: 100.0,
                    message: "dup".to_string(),
                    after_change_at: 0.0,
                }],
            ),
            (
                1,
                "hash_v3".to_string(),
                vec![FindingInfo {
                    id: 200,
                    rule_id: "rule_x".to_string(),
                    rule_name: "X".to_string(),
                    severity: "med".to_string(),
                    start_line: 1,
                    end_line: 5,
                    scanned_at: 100.0,
                    message: "dup".to_string(),
                    after_change_at: 0.0,
                }],
            ),
        ];

        let result = defect_correlation_core(&changes_by_file, &all_versions, &findings, vec![], 3);

        assert_eq!(result.defects_after_change, 1); // 去重后只剩 1 个
        assert_eq!(result.findings.len(), 1);
        assert_eq!(result.defect_types, vec![("rule_x".to_string(), 1)]);
    }

    #[test]
    fn test_defect_correlation_direct_findings() {
        // 直接关联的缺陷（不局限于窗口）
        let changes_by_file = vec![(1, vec![1])];
        let all_versions = vec![(
            1,
            vec![VersionInfo {
                version_num: 1,
                content_hash: "hash_v1".to_string(),
            }],
        )];
        let findings: Vec<(i64, String, Vec<FindingInfo>)> = vec![];
        let direct = vec![FindingInfo {
            id: 300,
            rule_id: "direct_rule".to_string(),
            rule_name: "Direct".to_string(),
            severity: "high".to_string(),
            start_line: 1,
            end_line: 10,
            scanned_at: 500.0,
            message: "direct".to_string(),
            after_change_at: 0.0,
        }];

        let result = defect_correlation_core(&changes_by_file, &all_versions, &findings, direct, 5);

        assert_eq!(result.defects_after_change, 1);
        assert_eq!(result.findings[0].rule_id, "direct_rule");
        assert_eq!(result.findings[0].after_change_at, 0.0); // 直接关联设为 0.0
    }

    #[test]
    fn test_defect_correlation_empty() {
        let result = defect_correlation_core(&[], &[], &[], vec![], 5);
        assert_eq!(result.total_changes, 0);
        assert_eq!(result.defects_after_change, 0);
        assert!(result.findings.is_empty());
        assert!(result.defect_types.is_empty());
    }

    #[test]
    fn test_defect_correlation_window_boundary() {
        // 验证窗口边界：变更在版本 1，window_commits=2 → 取版本 2 和 3
        let changes_by_file = vec![(1, vec![1])];
        let all_versions = vec![(
            1,
            vec![
                VersionInfo {
                    version_num: 1,
                    content_hash: "h1".to_string(),
                },
                VersionInfo {
                    version_num: 2,
                    content_hash: "h2".to_string(),
                },
                VersionInfo {
                    version_num: 3,
                    content_hash: "h3".to_string(),
                },
                VersionInfo {
                    version_num: 4,
                    content_hash: "h4".to_string(),
                }, // 不在窗口内
            ],
        )];
        // h4 不在窗口内，对应的 finding 不应被收集
        let findings = vec![
            (
                1,
                "h2".to_string(),
                vec![FindingInfo {
                    id: 1,
                    rule_id: "r1".to_string(),
                    rule_name: "R1".to_string(),
                    severity: "low".to_string(),
                    start_line: 1,
                    end_line: 2,
                    scanned_at: 1.0,
                    message: "in".to_string(),
                    after_change_at: 0.0,
                }],
            ),
            (
                1,
                "h3".to_string(),
                vec![FindingInfo {
                    id: 2,
                    rule_id: "r2".to_string(),
                    rule_name: "R2".to_string(),
                    severity: "low".to_string(),
                    start_line: 1,
                    end_line: 2,
                    scanned_at: 2.0,
                    message: "in".to_string(),
                    after_change_at: 0.0,
                }],
            ),
            (
                1,
                "h4".to_string(),
                vec![FindingInfo {
                    id: 3,
                    rule_id: "r3".to_string(),
                    rule_name: "R3".to_string(),
                    severity: "low".to_string(),
                    start_line: 1,
                    end_line: 2,
                    scanned_at: 3.0,
                    message: "out".to_string(),
                    after_change_at: 0.0,
                }],
            ),
        ];

        let result = defect_correlation_core(&changes_by_file, &all_versions, &findings, vec![], 2);

        assert_eq!(result.defects_after_change, 2); // h2 和 h3 的 finding，h4 不在窗口内
    }

    #[test]
    fn test_defect_correlation_empty_hash_skip() {
        // 对齐 Python: window_hashes = [v["content_hash"] for v in window_versions if v["content_hash"]]
        // 空 content_hash 应被跳过
        let changes_by_file = vec![(1, vec![1])];
        let all_versions = vec![(
            1,
            vec![
                VersionInfo {
                    version_num: 1,
                    content_hash: "h1".to_string(),
                },
                VersionInfo {
                    version_num: 2,
                    content_hash: "".to_string(),
                }, // 空 hash
            ],
        )];
        let findings = vec![(
            1,
            "".to_string(),
            vec![FindingInfo {
                id: 1,
                rule_id: "r1".to_string(),
                rule_name: "R1".to_string(),
                severity: "low".to_string(),
                start_line: 1,
                end_line: 2,
                scanned_at: 1.0,
                message: "should_skip".to_string(),
                after_change_at: 0.0,
            }],
        )];

        let result = defect_correlation_core(&changes_by_file, &all_versions, &findings, vec![], 5);
        assert_eq!(result.defects_after_change, 0); // 空 hash 的 finding 被跳过
    }
}
