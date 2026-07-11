//! Phase 5.3: Parse Delta / Resolve Delta
//!
//! 设计参考：enterprise-daemon-shared-snapshot-plan.md §9.1
//!
//! 文件保存流程：
//!   ... → content hash diff → CAS lookup / parse worker → resolve affected raw calls
//!
//! 本模块负责：
//! - 解析变更文件，对比当前 GraphStore 产生 symbol/raw_call delta
//! - 对 raw_calls 做 resolve（callee_name → qualified_name），产生 resolved edge delta
//! - 输出结构化的 ParseDelta / ResolveDelta，供后续 affected frontier 和 snapshot generation 使用

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};

use crate::graph::GraphStore;
use crate::multi_lang::{GenericParser, LangConfig};

// ============================================
// 语言检测
// ============================================

/// 从文件扩展名检测语言 ID
pub fn lang_from_extension(path: &Path) -> Option<&'static str> {
    let ext = path.extension()?.to_str()?.to_lowercase();
    Some(match ext.as_str() {
        "py" => "python",
        "rs" => "rust",
        "go" => "go",
        "java" => "java",
        "ts" => "typescript",
        "js" => "javascript",
        "rb" => "ruby",
        "php" => "php",
        "scala" => "scala",
        "cs" => "csharp",
        "cpp" | "cc" | "cxx" => "cpp",
        "c" | "h" => "cpp", // C 头文件也走 cpp parser
        "hpp" => "cpp",
        _ => return None,
    })
}

// ============================================
// SymbolDelta —— 符号变更
// ============================================

/// 符号变更类型
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SymbolDeltaKind {
    Added,
    Removed,
    Changed,
}

impl SymbolDeltaKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            SymbolDeltaKind::Added => "added",
            SymbolDeltaKind::Removed => "removed",
            SymbolDeltaKind::Changed => "changed",
        }
    }
}

/// 单个符号的变更记录
#[derive(Clone, Debug)]
pub struct SymbolDeltaEntry {
    pub kind: SymbolDeltaKind,
    pub qualified_name: String,
    pub name: String,
    pub symbol_kind: String,
    pub start_line: u32,
    pub end_line: u32,
    /// 变更前 start_line（Removed/Changed 时有值）
    pub prev_start_line: Option<u32>,
    /// 变更前 end_line（Removed/Changed 时有值）
    pub prev_end_line: Option<u32>,
}

/// 文件级符号 delta
#[derive(Clone, Debug, Default)]
pub struct SymbolDelta {
    pub added: Vec<SymbolDeltaEntry>,
    pub removed: Vec<SymbolDeltaEntry>,
    pub changed: Vec<SymbolDeltaEntry>,
}

impl SymbolDelta {
    pub fn total(&self) -> usize {
        self.added.len() + self.removed.len() + self.changed.len()
    }

    pub fn is_empty(&self) -> bool {
        self.added.is_empty() && self.removed.is_empty() && self.changed.is_empty()
    }

    /// 所有受影响的 qualified_name（added + removed + changed）
    pub fn affected_qnames(&self) -> Vec<String> {
        let mut qnames = Vec::new();
        for e in &self.added { qnames.push(e.qualified_name.clone()); }
        for e in &self.removed { qnames.push(e.qualified_name.clone()); }
        for e in &self.changed { qnames.push(e.qualified_name.clone()); }
        qnames
    }
}

// ============================================
// RawCallDelta —— 原始调用变更
// ============================================

/// 原始调用记录（简化版，用于 delta）
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct RawCallKey {
    pub caller_qualified: String,
    pub callee_name: String,
    pub call_line: u32,
}

/// 文件级 raw call delta
#[derive(Clone, Debug, Default)]
pub struct RawCallDelta {
    pub added: Vec<RawCallKey>,
    pub removed: Vec<RawCallKey>,
}

impl RawCallDelta {
    pub fn total(&self) -> usize {
        self.added.len() + self.removed.len()
    }

    pub fn is_empty(&self) -> bool {
        self.added.is_empty() && self.removed.is_empty()
    }
}

// ============================================
// ParseDelta —— 文件级 parse delta
// ============================================

/// 一个变更文件的完整 parse delta
#[derive(Clone, Debug)]
pub struct ParseDelta {
    pub file_path: PathBuf,
    pub content_hash: String,
    pub language: String,
    pub symbol_delta: SymbolDelta,
    pub raw_call_delta: RawCallDelta,
    pub total_lines: u32,
}

impl ParseDelta {
    pub fn is_empty(&self) -> bool {
        self.symbol_delta.is_empty() && self.raw_call_delta.is_empty()
    }

    /// 变更摘要（用于日志和报告）
    pub fn summary(&self) -> String {
        format!(
            "{}: {} symbols ({}+{}-{}), {} calls ({}+{})",
            self.file_path.display(),
            self.symbol_delta.total(),
            self.symbol_delta.added.len(),
            self.symbol_delta.removed.len(),
            self.symbol_delta.changed.len(),
            self.raw_call_delta.total(),
            self.raw_call_delta.added.len(),
            self.raw_call_delta.removed.len(),
        )
    }
}

// ============================================
// ResolveDelta —— resolved edge delta
// ============================================

/// 已解析的调用边
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
pub struct ResolvedEdge {
    pub caller_qname: String,
    pub callee_qname: String,
}

/// 文件级 resolved edge delta
#[derive(Clone, Debug, Default)]
pub struct ResolveDelta {
    pub added: Vec<ResolvedEdge>,
    pub removed: Vec<ResolvedEdge>,
    /// 未能解析的 callee_name 列表（目标不在当前符号索引中）
    pub unresolved: Vec<String>,
}

impl ResolveDelta {
    pub fn total_edges(&self) -> usize {
        self.added.len() + self.removed.len()
    }

    pub fn is_empty(&self) -> bool {
        self.added.is_empty() && self.removed.is_empty()
    }
}

// ============================================
// DeltaComputer —— delta 计算核心
// ============================================

/// Delta 计算器：解析变更文件并与 GraphStore 对比
pub struct DeltaComputer;

impl DeltaComputer {
    /// 解析文件并对比 GraphStore，产生 ParseDelta
    ///
    /// 参数：
    /// - file_path: 变更文件路径
    /// - store: 当前 GraphStore（用于对比旧符号）
    ///
    /// 返回 ParseDelta，包含 symbol delta 和 raw call delta
    pub fn compute_parse_delta(
        file_path: &Path,
        store: Option<&GraphStore>,
    ) -> Result<ParseDelta, String> {
        // 1. 检测语言
        let language = lang_from_extension(file_path)
            .ok_or_else(|| format!("unsupported file extension: {:?}", file_path))?;

        // 2. 获取 parser config
        let config = LangConfig::get(language)
            .ok_or_else(|| format!("unsupported language: {}", language))?;

        // 3. 解析文件
        // 修复 T-1783751519227-18d8: 先 canonicalize 再 parse，
        // 避免 parse_file 直接读文件绕过 BOM 剥离 / 编码检测 / CRLF→LF 归一化
        let parser = GenericParser::new(Arc::new(config));
        let abs_path = file_path.to_string_lossy();
        let module_path = file_path
            .file_stem()
            .map(|s| s.to_string_lossy().to_string())
            .unwrap_or_default();
        let canonical = crate::canonicalize::canonicalize_source(&abs_path)
            .map_err(|e| format!("canonicalize failed: {}", e))?;
        let parse_result = parser.parse_canonical_bytes(
            &canonical.canonical_bytes,
            &abs_path,
            &module_path,
            &canonical.content_hash,
        );

        if let Some(err) = &parse_result.error {
            return Err(format!("parse error: {}", err));
        }

        // 4. 计算符号 delta
        let symbol_delta = Self::compute_symbol_delta(&parse_result, store);

        // 5. 计算 raw call delta
        let raw_call_delta = Self::compute_raw_call_delta(&parse_result, store);

        Ok(ParseDelta {
            file_path: file_path.to_path_buf(),
            content_hash: parse_result.content_hash.clone(),
            language: language.to_string(),
            symbol_delta,
            raw_call_delta,
            total_lines: parse_result.total_lines,
        })
    }

    /// 对比 parse result 和 GraphStore，计算符号 delta
    fn compute_symbol_delta(
        parse_result: &crate::ParseResult,
        store: Option<&GraphStore>,
    ) -> SymbolDelta {
        let mut delta = SymbolDelta::default();

        // 构建 parse result 的符号映射：qualified_name → SymbolInfo
        let new_symbols: HashMap<&str, &crate::SymbolInfo> = parse_result
            .symbols
            .iter()
            .map(|s| (s.qualified_name.as_str(), s))
            .collect();

        // 构建 store 中同文件的旧符号映射
        let old_symbols: HashMap<String, (u32, u32, String)> = if let Some(store) = store {
            store
                .get_symbols_by_file(&parse_result.rel_path)
                .into_iter()
                .map(|s| {
                    (
                        s.qualified_name.clone(),
                        (s.start_line, s.end_line, s.kind.clone()),
                    )
                })
                .collect()
        } else {
            HashMap::new()
        };

        // Added: 在新解析中但不在旧 store 中
        for (qname, new_sym) in &new_symbols {
            if !old_symbols.contains_key(*qname) {
                delta.added.push(SymbolDeltaEntry {
                    kind: SymbolDeltaKind::Added,
                    qualified_name: qname.to_string(),
                    name: new_sym.name.clone(),
                    symbol_kind: new_sym.kind.clone(),
                    start_line: new_sym.start_line,
                    end_line: new_sym.end_line,
                    prev_start_line: None,
                    prev_end_line: None,
                });
            }
        }

        // Removed: 在旧 store 中但不在新解析中
        for (qname, (old_start, old_end, old_kind)) in &old_symbols {
            if !new_symbols.contains_key(qname.as_str()) {
                delta.removed.push(SymbolDeltaEntry {
                    kind: SymbolDeltaKind::Removed,
                    qualified_name: qname.clone(),
                    name: String::new(),
                    symbol_kind: old_kind.clone(),
                    start_line: 0,
                    end_line: 0,
                    prev_start_line: Some(*old_start),
                    prev_end_line: Some(*old_end),
                });
            }
        }

        // Changed: 在两者中但 line range 不同
        for (qname, new_sym) in &new_symbols {
            if let Some((old_start, old_end, old_kind)) = old_symbols.get(*qname) {
                if new_sym.start_line != *old_start
                    || new_sym.end_line != *old_end
                    || new_sym.kind != *old_kind
                {
                    delta.changed.push(SymbolDeltaEntry {
                        kind: SymbolDeltaKind::Changed,
                        qualified_name: qname.to_string(),
                        name: new_sym.name.clone(),
                        symbol_kind: new_sym.kind.clone(),
                        start_line: new_sym.start_line,
                        end_line: new_sym.end_line,
                        prev_start_line: Some(*old_start),
                        prev_end_line: Some(*old_end),
                    });
                }
            }
        }

        delta
    }

    /// 对比 parse result 和 GraphStore，计算 raw call delta
    fn compute_raw_call_delta(
        parse_result: &crate::ParseResult,
        _store: Option<&GraphStore>,
    ) -> RawCallDelta {
        let mut delta = RawCallDelta::default();

        // 构建新 raw calls 的 key 集合
        let new_calls: HashSet<RawCallKey> = parse_result
            .calls
            .iter()
            .map(|c| RawCallKey {
                caller_qualified: c.caller_qualified.clone(),
                callee_name: c.callee_name.clone(),
                call_line: c.call_line,
            })
            .collect();

        // 构建 store 中同文件的旧 raw calls
        // 注意：GraphStore 中的 calls 是 resolved edges（有 caller_id/callee_id），
        // 而 parse_result 中的 calls 是 raw calls（有 callee_name 但未 resolved）。
        // 这里我们只对比 raw calls：新的 - 旧的 = added, 旧的 - 新的 = removed
        // 但 GraphStore 不直接存 raw calls（只存 resolved edges），
        // 所以这里只返回 added（新 parse 产生的 raw calls），
        // removed 需要通过 resolved edge delta 在 resolve 阶段处理。
        delta.added = new_calls.into_iter().collect();

        delta
    }

    /// 对 raw calls 做 resolve，产生 ResolveDelta
    ///
    /// 参数：
    /// - parse_delta: 文件 parse delta
    /// - store: 当前 GraphStore（用于 resolve callee_name → qualified_name）
    pub fn compute_resolve_delta(
        parse_delta: &ParseDelta,
        store: Option<&GraphStore>,
    ) -> ResolveDelta {
        let mut delta = ResolveDelta::default();

        let store = match store {
            Some(s) => s,
            None => return delta,
        };

        // 使用 GraphStore 的 name → qnames 映射做 resolve
        let name_to_qnames = store.get_name_to_qnames();

        // 对每个 raw call 做 resolve
        for raw_call in &parse_delta.raw_call_delta.added {
            let callee_qnames = name_to_qnames.get(&raw_call.callee_name);

            match callee_qnames {
                Some(qnames) if !qnames.is_empty() => {
                    // 找到匹配的 callee
                    for callee_qname in qnames {
                        delta.added.push(ResolvedEdge {
                            caller_qname: raw_call.caller_qualified.clone(),
                            callee_qname: callee_qname.clone(),
                        });
                    }
                }
                Some(_) => {
                    // 空列表，视为未解析
                    delta.unresolved.push(raw_call.callee_name.clone());
                }
                None => {
                    // 未解析
                    delta.unresolved.push(raw_call.callee_name.clone());
                }
            }
        }

        // Removed edges: 从 parse delta 的 removed symbols 推导
        // 如果一个 symbol 被移除，它的所有 caller edges 也要移除
        for sym_delta in &parse_delta.symbol_delta.removed {
            let qname = &sym_delta.qualified_name;
            // 通过 get_symbol_ref 查找 symbol_id
            if let Some(sym) = store.get_symbol_ref(qname) {
                let caller_ids = store.get_caller_ids(sym.id);
                for caller_id in caller_ids {
                    if let Some(caller) = store.get_symbol_by_id(caller_id) {
                        delta.removed.push(ResolvedEdge {
                            caller_qname: caller.qualified_name.clone(),
                            callee_qname: qname.clone(),
                        });
                    }
                }
            }
        }

        delta
    }
}

// ============================================
// PyO3 暴露
// ============================================

/// Python 侧 delta 计算结果包装
#[pyclass(name = "PyParseDelta")]
pub struct PyParseDelta {
    pub inner: ParseDelta,
}

#[pymethods]
impl PyParseDelta {
    #[getter]
    fn file_path(&self) -> String {
        self.inner.file_path.to_string_lossy().to_string()
    }

    #[getter]
    fn content_hash(&self) -> String {
        self.inner.content_hash.clone()
    }

    #[getter]
    fn language(&self) -> String {
        self.inner.language.clone()
    }

    #[getter]
    fn total_lines(&self) -> u32 {
        self.inner.total_lines
    }

    /// 符号 delta 统计
    #[getter]
    fn symbol_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        d.set_item("added", self.inner.symbol_delta.added.len())?;
        d.set_item("removed", self.inner.symbol_delta.removed.len())?;
        d.set_item("changed", self.inner.symbol_delta.changed.len())?;
        d.set_item("total", self.inner.symbol_delta.total())?;
        Ok(d)
    }

    /// raw call delta 统计
    #[getter]
    fn call_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let d = PyDict::new(py);
        d.set_item("added", self.inner.raw_call_delta.added.len())?;
        d.set_item("removed", self.inner.raw_call_delta.removed.len())?;
        d.set_item("total", self.inner.raw_call_delta.total())?;
        Ok(d)
    }

    /// 变更摘要
    fn summary(&self) -> String {
        self.inner.summary()
    }

    /// 是否无变更
    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    /// 获取受影响的 qualified_name 列表
    fn affected_qnames<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let qnames = self.inner.symbol_delta.affected_qnames();
        Ok(PyList::new(py, qnames.iter().map(String::as_str))?)
    }

    fn __repr__(&self) -> String {
        format!(
            "PyParseDelta({}: {})",
            self.inner.file_path.display(),
            self.inner.summary()
        )
    }
}

/// Python 侧 resolve delta 结果包装
#[pyclass(name = "PyResolveDelta")]
pub struct PyResolveDelta {
    pub inner: ResolveDelta,
}

#[pymethods]
impl PyResolveDelta {
    #[getter]
    fn added_edges<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let result = PyList::empty(py);
        for edge in &self.inner.added {
            let t = (edge.caller_qname.clone(), edge.callee_qname.clone());
            result.append(t)?;
        }
        Ok(result)
    }

    #[getter]
    fn removed_edges<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let result = PyList::empty(py);
        for edge in &self.inner.removed {
            let t = (edge.caller_qname.clone(), edge.callee_qname.clone());
            result.append(t)?;
        }
        Ok(result)
    }

    #[getter]
    fn unresolved<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        Ok(PyList::new(py, self.inner.unresolved.iter().map(String::as_str))?)
    }

    #[getter]
    fn total_edges(&self) -> usize {
        self.inner.total_edges()
    }

    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    fn __repr__(&self) -> String {
        format!(
            "PyResolveDelta(added={}, removed={}, unresolved={})",
            self.inner.added.len(),
            self.inner.removed.len(),
            self.inner.unresolved.len()
        )
    }
}

/// Python 侧 delta 计算器
///
/// Python 用法：
///   from callwarden_core import PyDeltaComputer, PySnapshotCache
///   cache = PySnapshotCache()
///   cache.load_from_db("workspace_id", "/path/to/db.sqlite")
///   delta = PyDeltaComputer.compute_parse_delta("/path/to/changed.py", cache, "workspace_id")
///   resolve = PyDeltaComputer.compute_resolve_delta(delta, cache, "workspace_id")
#[pyclass(name = "PyDeltaComputer")]
pub struct PyDeltaComputer;

#[pymethods]
impl PyDeltaComputer {
    /// 计算文件的 parse delta
    ///
    /// 参数：
    /// - file_path: 变更文件路径
    /// - cache: PySnapshotCache（可选，用于对比旧符号）
    /// - workspace_id: workspace 实例 ID（cache 中查找 store 的 key）
    #[staticmethod]
    #[pyo3(signature = (file_path, cache=None, workspace_id=None))]
    fn compute_parse_delta(
        py: Python<'_>,
        file_path: &str,
        cache: Option<&crate::snapshot::PySnapshotCache>,
        workspace_id: Option<&str>,
    ) -> PyResult<Py<PyParseDelta>> {
        let path = Path::new(file_path);

        // 获取 GraphStore（如果提供了 cache 和 workspace_id）
        let store_ref: Option<std::sync::Arc<GraphStore>> = match (cache, workspace_id) {
            (Some(c), Some(wid)) => {
                let store = c.get_store(wid)?;
                Some(store)
            }
            _ => None,
        };

        let store = store_ref.as_ref().map(|s| s.as_ref());

        let delta = DeltaComputer::compute_parse_delta(path, store)
            .map_err(|e| pyo3::exceptions::PyRuntimeError::new_err(e))?;

        Py::new(py, PyParseDelta { inner: delta })
    }

    /// 计算 resolve delta
    ///
    /// 参数：
    /// - parse_delta: PyParseDelta（compute_parse_delta 的返回值）
    /// - cache: PySnapshotCache
    /// - workspace_id: workspace 实例 ID
    #[staticmethod]
    #[pyo3(signature = (parse_delta, cache=None, workspace_id=None))]
    fn compute_resolve_delta(
        py: Python<'_>,
        parse_delta: &PyParseDelta,
        cache: Option<&crate::snapshot::PySnapshotCache>,
        workspace_id: Option<&str>,
    ) -> PyResult<Py<PyResolveDelta>> {
        let store_ref: Option<std::sync::Arc<GraphStore>> = match (cache, workspace_id) {
            (Some(c), Some(wid)) => {
                let store = c.get_store(wid)?;
                Some(store)
            }
            _ => None,
        };

        let store = store_ref.as_ref().map(|s| s.as_ref());

        let resolve_delta = DeltaComputer::compute_resolve_delta(&parse_delta.inner, store);

        Py::new(py, PyResolveDelta { inner: resolve_delta })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;

    #[test]
    fn test_lang_from_extension() {
        assert_eq!(lang_from_extension(Path::new("test.py")), Some("python"));
        assert_eq!(lang_from_extension(Path::new("test.rs")), Some("rust"));
        assert_eq!(lang_from_extension(Path::new("test.go")), Some("go"));
        assert_eq!(lang_from_extension(Path::new("test.ts")), Some("typescript"));
        assert_eq!(lang_from_extension(Path::new("test.unknown")), None);
        assert_eq!(lang_from_extension(Path::new("noext")), None);
    }

    #[test]
    fn test_symbol_delta_empty() {
        let delta = SymbolDelta::default();
        assert!(delta.is_empty());
        assert_eq!(delta.total(), 0);
        assert!(delta.affected_qnames().is_empty());
    }

    #[test]
    fn test_raw_call_delta_empty() {
        let delta = RawCallDelta::default();
        assert!(delta.is_empty());
        assert_eq!(delta.total(), 0);
    }

    #[test]
    fn test_parse_delta_summary() {
        let delta = ParseDelta {
            file_path: PathBuf::from("/test.py"),
            content_hash: "abc123".to_string(),
            language: "python".to_string(),
            symbol_delta: SymbolDelta {
                added: vec![SymbolDeltaEntry {
                    kind: SymbolDeltaKind::Added,
                    qualified_name: "test.func".to_string(),
                    name: "func".to_string(),
                    symbol_kind: "function".to_string(),
                    start_line: 1,
                    end_line: 10,
                    prev_start_line: None,
                    prev_end_line: None,
                }],
                removed: vec![],
                changed: vec![],
            },
            raw_call_delta: RawCallDelta::default(),
            total_lines: 10,
        };
        let summary = delta.summary();
        assert!(summary.contains("test.py"));
        assert!(summary.contains("1 symbols"));
    }

    #[test]
    fn test_compute_parse_delta_no_store() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.py");
        fs::write(&path, "def hello():\n    pass\n").unwrap();

        let delta = DeltaComputer::compute_parse_delta(&path, None).unwrap();
        assert_eq!(delta.language, "python");
        assert!(!delta.content_hash.is_empty());

        // 无 store 对比时，所有符号都是 Added
        assert!(!delta.symbol_delta.added.is_empty());
        assert!(delta.symbol_delta.removed.is_empty());
        assert!(delta.symbol_delta.changed.is_empty());

        // 应该有 raw calls（如果有函数调用）
        // hello() 函数没有调用，所以 raw_calls 可能为空
    }

    #[test]
    fn test_compute_parse_delta_unsupported() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("test.unknown");
        fs::write(&path, "content\n").unwrap();

        let result = DeltaComputer::compute_parse_delta(&path, None);
        assert!(result.is_err());
    }

    #[test]
    fn test_resolve_delta_empty_store() {
        let parse_delta = ParseDelta {
            file_path: PathBuf::from("/test.py"),
            content_hash: "abc".to_string(),
            language: "python".to_string(),
            symbol_delta: SymbolDelta::default(),
            raw_call_delta: RawCallDelta {
                added: vec![RawCallKey {
                    caller_qualified: "test.caller".to_string(),
                    callee_name: "callee".to_string(),
                    call_line: 5,
                }],
                removed: vec![],
            },
            total_lines: 10,
        };

        // 无 store → 无法 resolve
        let resolve_delta = DeltaComputer::compute_resolve_delta(&parse_delta, None);
        assert!(resolve_delta.added.is_empty());
        assert!(resolve_delta.unresolved.is_empty()); // 无 store 时直接返回空
    }

    #[test]
    fn test_symbol_delta_affected_qnames() {
        let delta = SymbolDelta {
            added: vec![SymbolDeltaEntry {
                kind: SymbolDeltaKind::Added,
                qualified_name: "added.fn".to_string(),
                name: "fn".to_string(),
                symbol_kind: "function".to_string(),
                start_line: 1, end_line: 5,
                prev_start_line: None, prev_end_line: None,
            }],
            removed: vec![SymbolDeltaEntry {
                kind: SymbolDeltaKind::Removed,
                qualified_name: "removed.fn".to_string(),
                name: "fn".to_string(),
                symbol_kind: "function".to_string(),
                start_line: 0, end_line: 0,
                prev_start_line: Some(1), prev_end_line: Some(5),
            }],
            changed: vec![],
        };

        let qnames = delta.affected_qnames();
        assert_eq!(qnames.len(), 2);
        assert!(qnames.contains(&"added.fn".to_string()));
        assert!(qnames.contains(&"removed.fn".to_string()));
    }

    #[test]
    fn test_parse_delta_is_empty() {
        let empty = ParseDelta {
            file_path: PathBuf::from("/test.py"),
            content_hash: "abc".to_string(),
            language: "python".to_string(),
            symbol_delta: SymbolDelta::default(),
            raw_call_delta: RawCallDelta::default(),
            total_lines: 0,
        };
        assert!(empty.is_empty());

        let non_empty = ParseDelta {
            file_path: PathBuf::from("/test.py"),
            content_hash: "abc".to_string(),
            language: "python".to_string(),
            symbol_delta: SymbolDelta {
                added: vec![SymbolDeltaEntry {
                    kind: SymbolDeltaKind::Added,
                    qualified_name: "test.fn".to_string(),
                    name: "fn".to_string(),
                    symbol_kind: "function".to_string(),
                    start_line: 1, end_line: 5,
                    prev_start_line: None, prev_end_line: None,
                }],
                removed: vec![],
                changed: vec![],
            },
            raw_call_delta: RawCallDelta::default(),
            total_lines: 5,
        };
        assert!(!non_empty.is_empty());
    }
}
