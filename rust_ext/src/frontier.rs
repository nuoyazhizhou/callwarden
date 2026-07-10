//! Phase 5.4: Affected Frontier 计算
//!
//! 设计参考：enterprise-daemon-shared-snapshot-plan.md §9.1
//!
//! 文件保存流程：
//!   ... → resolve delta → compute affected graph frontier → build snapshot generation+1
//!
//! 本模块负责：
//! - 基于 ParseDelta / ResolveDelta 计算受影响的图区域
//! - 上游 frontier：直接或间接调用变更符号的符号
//! - 下游 frontier：被变更符号直接或间接调用的符号
//! - 输出 AffectedFrontier，供局部 depth/cycle/impact 更新使用

use std::collections::{HashMap, HashSet, VecDeque};

use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList, PySet};

use crate::delta::ParseDelta;
use crate::graph::GraphStore;

// ============================================
// AffectedFrontier —— 受影响的图区域
// ============================================

/// 受影响的前沿区域
#[derive(Clone, Debug, Default)]
pub struct AffectedFrontier {
    /// 直接变更的符号（parse delta 中的 added/removed/changed）
    pub directly_affected: HashSet<String>,
    /// 上游：直接调用变更符号的符号（1-hop callers）
    pub upstream_direct: HashSet<String>,
    /// 下游：被变更符号直接调用的符号（1-hop callees）
    pub downstream_direct: HashSet<String>,
    /// 多跳上游（max_depth > 1 时填充）
    pub upstream_transitive: HashSet<String>,
    /// 多跳下游（max_depth > 1 时填充）
    pub downstream_transitive: HashSet<String>,
}

impl AffectedFrontier {
    /// 所有受影响的符号（直接 + 上游 + 下游）
    pub fn all_affected(&self) -> HashSet<String> {
        let mut all = HashSet::new();
        all.extend(self.directly_affected.iter().cloned());
        all.extend(self.upstream_direct.iter().cloned());
        all.extend(self.downstream_direct.iter().cloned());
        all.extend(self.upstream_transitive.iter().cloned());
        all.extend(self.downstream_transitive.iter().cloned());
        all
    }

    /// 受影响符号总数
    pub fn total_count(&self) -> usize {
        self.all_affected().len()
    }

    /// 直接受影响符号数
    pub fn direct_count(&self) -> usize {
        self.directly_affected.len()
    }

    /// 是否为空
    pub fn is_empty(&self) -> bool {
        self.directly_affected.is_empty()
    }

    /// 摘要
    pub fn summary(&self) -> String {
        format!(
            "frontier: {} direct, {} upstream ({}+{}), {} downstream ({}+{})",
            self.directly_affected.len(),
            self.upstream_direct.len() + self.upstream_transitive.len(),
            self.upstream_direct.len(),
            self.upstream_transitive.len(),
            self.downstream_direct.len() + self.downstream_transitive.len(),
            self.downstream_direct.len(),
            self.downstream_transitive.len(),
        )
    }
}

// ============================================
// FrontierComputer —— frontier 计算核心
// ============================================

/// Frontier 计算器
pub struct FrontierComputer;

impl FrontierComputer {
    /// 计算 affected frontier
    ///
    /// 参数：
    /// - parse_delta: 文件 parse delta（包含 symbol delta）
    /// - store: 当前 GraphStore
    /// - max_depth: 传递闭包的最大深度（1=仅直接，2+=多跳）
    pub fn compute_frontier(
        parse_delta: &ParseDelta,
        store: Option<&GraphStore>,
        max_depth: u32,
    ) -> AffectedFrontier {
        let mut frontier = AffectedFrontier::default();

        // 1. 直接受影响的符号
        frontier.directly_affected = parse_delta
            .symbol_delta
            .affected_qnames()
            .into_iter()
            .collect();

        if store.is_none() {
            return frontier;
        }
        let store = store.unwrap();

        // 2. 上游 1-hop：谁调用了变更符号
        for qname in &frontier.directly_affected {
            if let Some(sym) = store.get_symbol_ref(qname) {
                let caller_ids = store.get_caller_ids(sym.id);
                for caller_id in caller_ids {
                    if let Some(caller) = store.get_symbol_by_id(caller_id) {
                        frontier.upstream_direct.insert(caller.qualified_name.clone());
                    }
                }
            }
        }

        // 3. 下游 1-hop：变更符号调用了谁
        for qname in &frontier.directly_affected {
            if let Some(sym) = store.get_symbol_ref(qname) {
                let callee_ids = store.get_callee_ids(sym.id);
                for callee_id in callee_ids {
                    if let Some(callee) = store.get_symbol_by_id(callee_id) {
                        frontier.downstream_direct.insert(callee.qualified_name.clone());
                    }
                }
            }
        }

        // 4. 多跳传递闭包（如果 max_depth > 1）
        if max_depth > 1 {
            // 上游传递闭包
            frontier.upstream_transitive = Self::bfs_upstream(
                store,
                &frontier.upstream_direct,
                &frontier.directly_affected,
                max_depth - 1,
            );

            // 下游传递闭包
            frontier.downstream_transitive = Self::bfs_downstream(
                store,
                &frontier.downstream_direct,
                &frontier.directly_affected,
                max_depth - 1,
            );
        }

        frontier
    }

    /// BFS 上游遍历：从 initial 出发，向上找 callers
    fn bfs_upstream(
        store: &GraphStore,
        initial: &HashSet<String>,
        exclude: &HashSet<String>,
        max_hops: u32,
    ) -> HashSet<String> {
        let mut result = HashSet::new();
        let mut visited: HashSet<String> = exclude.clone();
        let mut queue: VecDeque<(String, u32)> = VecDeque::new();

        // 初始化队列
        for qname in initial {
            if !visited.contains(qname) {
                visited.insert(qname.clone());
                queue.push_back((qname.clone(), 0));
            }
        }

        while let Some((qname, depth)) = queue.pop_front() {
            if depth >= max_hops {
                continue;
            }

            if let Some(sym) = store.get_symbol_ref(&qname) {
                let caller_ids = store.get_caller_ids(sym.id);
                for caller_id in caller_ids {
                    if let Some(caller) = store.get_symbol_by_id(caller_id) {
                        let caller_qname = &caller.qualified_name;
                        if !visited.contains(caller_qname) {
                            visited.insert(caller_qname.clone());
                            result.insert(caller_qname.clone());
                            queue.push_back((caller_qname.clone(), depth + 1));
                        }
                    }
                }
            }
        }

        result
    }

    /// BFS 下游遍历：从 initial 出发，向下找 callees
    fn bfs_downstream(
        store: &GraphStore,
        initial: &HashSet<String>,
        exclude: &HashSet<String>,
        max_hops: u32,
    ) -> HashSet<String> {
        let mut result = HashSet::new();
        let mut visited: HashSet<String> = exclude.clone();
        let mut queue: VecDeque<(String, u32)> = VecDeque::new();

        // 初始化队列
        for qname in initial {
            if !visited.contains(qname) {
                visited.insert(qname.clone());
                queue.push_back((qname.clone(), 0));
            }
        }

        while let Some((qname, depth)) = queue.pop_front() {
            if depth >= max_hops {
                continue;
            }

            if let Some(sym) = store.get_symbol_ref(&qname) {
                let callee_ids = store.get_callee_ids(sym.id);
                for callee_id in callee_ids {
                    if let Some(callee) = store.get_symbol_by_id(callee_id) {
                        let callee_qname = &callee.qualified_name;
                        if !visited.contains(callee_qname) {
                            visited.insert(callee_qname.clone());
                            result.insert(callee_qname.clone());
                            queue.push_back((callee_qname.clone(), depth + 1));
                        }
                    }
                }
            }
        }

        result
    }
}

// ============================================
// PyO3 暴露
// ============================================

/// Python 侧 AffectedFrontier 包装
#[pyclass(name = "PyAffectedFrontier")]
pub struct PyAffectedFrontier {
    pub inner: AffectedFrontier,
}

#[pymethods]
impl PyAffectedFrontier {
    /// 直接受影响符号数
    #[getter]
    fn direct_count(&self) -> usize {
        self.inner.direct_count()
    }

    /// 所有受影响符号数
    #[getter]
    fn total_count(&self) -> usize {
        self.inner.total_count()
    }

    /// 直接受影响的符号列表
    #[getter]
    fn directly_affected<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let mut items: Vec<String> = self.inner.directly_affected.iter().cloned().collect();
        items.sort();
        Ok(PyList::new(py, items.iter().map(String::as_str))?)
    }

    /// 上游 1-hop 符号列表
    #[getter]
    fn upstream_direct<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let mut items: Vec<String> = self.inner.upstream_direct.iter().cloned().collect();
        items.sort();
        Ok(PyList::new(py, items.iter().map(String::as_str))?)
    }

    /// 下游 1-hop 符号列表
    #[getter]
    fn downstream_direct<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let mut items: Vec<String> = self.inner.downstream_direct.iter().cloned().collect();
        items.sort();
        Ok(PyList::new(py, items.iter().map(String::as_str))?)
    }

    /// 多跳上游符号列表
    #[getter]
    fn upstream_transitive<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let mut items: Vec<String> = self.inner.upstream_transitive.iter().cloned().collect();
        items.sort();
        Ok(PyList::new(py, items.iter().map(String::as_str))?)
    }

    /// 多跳下游符号列表
    #[getter]
    fn downstream_transitive<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let mut items: Vec<String> = self.inner.downstream_transitive.iter().cloned().collect();
        items.sort();
        Ok(PyList::new(py, items.iter().map(String::as_str))?)
    }

    /// 所有受影响符号的集合
    fn all_affected<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let mut items: Vec<String> = self.inner.all_affected().into_iter().collect();
        items.sort();
        Ok(PyList::new(py, items.iter().map(String::as_str))?)
    }

    /// 是否为空
    fn is_empty(&self) -> bool {
        self.inner.is_empty()
    }

    /// 摘要
    fn summary(&self) -> String {
        self.inner.summary()
    }

    fn __repr__(&self) -> String {
        format!("PyAffectedFrontier({})", self.inner.summary())
    }
}

/// 扩展 PyDeltaComputer，添加 compute_frontier 方法
#[pyfunction]
#[pyo3(signature = (parse_delta, cache=None, workspace_id=None, max_depth=1))]
pub fn compute_frontier<'py>(
    py: Python<'py>,
    parse_delta: &crate::delta::PyParseDelta,
    cache: Option<&crate::snapshot::PySnapshotCache>,
    workspace_id: Option<&str>,
    max_depth: u32,
) -> PyResult<Py<PyAffectedFrontier>> {
    let store_ref: Option<std::sync::Arc<GraphStore>> = match (cache, workspace_id) {
        (Some(c), Some(wid)) => Some(c.get_store(wid)?),
        _ => None,
    };

    let store = store_ref.as_ref().map(|s| s.as_ref());
    let frontier = FrontierComputer::compute_frontier(&parse_delta.inner, store, max_depth);

    Py::new(py, PyAffectedFrontier { inner: frontier })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::delta::{ParseDelta, SymbolDelta, SymbolDeltaEntry, SymbolDeltaKind, RawCallDelta};
    use std::path::PathBuf;

    #[test]
    fn test_frontier_empty_delta() {
        let delta = ParseDelta {
            file_path: PathBuf::from("/test.py"),
            content_hash: "abc".to_string(),
            language: "python".to_string(),
            symbol_delta: SymbolDelta::default(),
            raw_call_delta: RawCallDelta::default(),
            total_lines: 0,
        };

        let frontier = FrontierComputer::compute_frontier(&delta, None, 1);
        assert!(frontier.is_empty());
        assert_eq!(frontier.direct_count(), 0);
        assert_eq!(frontier.total_count(), 0);
    }

    #[test]
    fn test_frontier_no_store() {
        let delta = ParseDelta {
            file_path: PathBuf::from("/test.py"),
            content_hash: "abc".to_string(),
            language: "python".to_string(),
            symbol_delta: SymbolDelta {
                added: vec![SymbolDeltaEntry {
                    kind: SymbolDeltaKind::Added,
                    qualified_name: "test.func".to_string(),
                    name: "func".to_string(),
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

        let frontier = FrontierComputer::compute_frontier(&delta, None, 1);
        assert!(!frontier.is_empty());
        assert_eq!(frontier.direct_count(), 1);
        // 无 store → upstream/downstream 为空
        assert!(frontier.upstream_direct.is_empty());
        assert!(frontier.downstream_direct.is_empty());
    }

    #[test]
    fn test_frontier_summary() {
        let mut frontier = AffectedFrontier::default();
        frontier.directly_affected.insert("a".to_string());
        frontier.upstream_direct.insert("b".to_string());
        frontier.downstream_direct.insert("c".to_string());

        let s = frontier.summary();
        assert!(s.contains("1 direct"));
        assert!(s.contains("1 upstream"));
        assert!(s.contains("1 downstream"));
    }

    #[test]
    fn test_all_affected() {
        let mut frontier = AffectedFrontier::default();
        frontier.directly_affected.insert("a".to_string());
        frontier.upstream_direct.insert("b".to_string());
        frontier.downstream_direct.insert("c".to_string());
        frontier.upstream_transitive.insert("d".to_string());

        let all = frontier.all_affected();
        assert_eq!(all.len(), 4);
        assert!(all.contains("a"));
        assert!(all.contains("b"));
        assert!(all.contains("c"));
        assert!(all.contains("d"));
    }

    #[test]
    fn test_frontier_default_is_empty() {
        let frontier = AffectedFrontier::default();
        assert!(frontier.is_empty());
        assert_eq!(frontier.total_count(), 0);
    }
}
