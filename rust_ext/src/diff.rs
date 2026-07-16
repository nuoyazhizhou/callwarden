//! Phase 4.7: Version Diff Index —— 函数级 diff_symbol / diff_signature
//!
//! 设计参考：enterprise-daemon-shared-snapshot-plan.md §8.4
//!
//! 核心能力：
//! - diff_symbol: 对比两个 snapshot 中同一 qualified_name 的符号，
//!   返回 change_kind（added/removed/moved/signature_changed/callers_changed/callees_changed/unchanged）
//! - diff_signature: 仅对比符号的签名层面（文件位置、行号范围、kind）
//! - EdgeDeltaSummary: 对比 caller/callee 集合的增删
//!
//! 匹配规则（按置信度分层）：
//! 1. qualified_name 完全一致（当前实现，tier 2）
//! 2. signature 相似（未来扩展，tier 3）
//! 3. 低置信度返回 ambiguous（未来扩展，tier 4）

use crate::graph::GraphStore;
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::HashSet;

// ============================================
// 变更类型枚举
// ============================================

/// 符号变更类型（对齐设计文档 §8.4 SymbolChangeKind）
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum SymbolChangeKind {
    /// 仅右侧存在（新增）
    Added,
    /// 仅左侧存在（删除）
    Removed,
    /// 文件路径变化（移动）
    Moved,
    /// 行号范围或 kind 变化（签名变化）
    SignatureChanged,
    /// caller 集合变化
    CallersChanged,
    /// callee 集合变化
    CalleesChanged,
    /// 无变化
    Unchanged,
    /// 匹配歧义（低置信度，不自动断言同一函数）
    Ambiguous,
}

impl SymbolChangeKind {
    /// 转为字符串表示（用于 Python 侧序列化）
    pub fn as_str(&self) -> &'static str {
        match self {
            SymbolChangeKind::Added => "added",
            SymbolChangeKind::Removed => "removed",
            SymbolChangeKind::Moved => "moved",
            SymbolChangeKind::SignatureChanged => "signature_changed",
            SymbolChangeKind::CallersChanged => "callers_changed",
            SymbolChangeKind::CalleesChanged => "callees_changed",
            SymbolChangeKind::Unchanged => "unchanged",
            SymbolChangeKind::Ambiguous => "ambiguous",
        }
    }
}

// ============================================
// Diff 结果结构
// ============================================

/// 签名差异（行号、文件、kind 层面的变化）
#[derive(Clone, Debug, Default)]
pub struct SignatureDiff {
    pub left_file: String,
    pub right_file: String,
    pub file_changed: bool,
    pub left_start_line: u32,
    pub left_end_line: u32,
    pub right_start_line: u32,
    pub right_end_line: u32,
    pub line_range_changed: bool,
    pub left_kind: String,
    pub right_kind: String,
    pub kind_changed: bool,
}

/// 边差异摘要（caller/callee 集合的增删）
#[derive(Clone, Debug, Default)]
pub struct EdgeDeltaSummary {
    /// 新增的 caller qualified_name 列表
    pub added_callers: Vec<String>,
    /// 移除的 caller qualified_name 列表
    pub removed_callers: Vec<String>,
    /// 新增的 callee qualified_name 列表
    pub added_callees: Vec<String>,
    /// 移除的 callee qualified_name 列表
    pub removed_callees: Vec<String>,
}

impl EdgeDeltaSummary {
    pub fn has_changes(&self) -> bool {
        !self.added_callers.is_empty()
            || !self.removed_callers.is_empty()
            || !self.added_callees.is_empty()
            || !self.removed_callees.is_empty()
    }

    pub fn total_delta(&self) -> usize {
        self.added_callers.len()
            + self.removed_callers.len()
            + self.added_callees.len()
            + self.removed_callees.len()
    }
}

/// 符号差异记录（对齐设计文档 §8.4 SymbolDiffRecord）
#[derive(Clone, Debug)]
pub struct SymbolDiffRecord {
    pub qualified_name: String,
    pub change_kind: SymbolChangeKind,
    pub signature_change: SignatureDiff,
    pub caller_delta: EdgeDeltaSummary,
    pub callee_delta: EdgeDeltaSummary,
}

// ============================================
// 核心对比函数
// ============================================

/// 对比两个 snapshot 中同一 qualified_name 的符号签名
///
/// 返回 SignatureDiff，包含文件/行号/kind 层面的变化
pub fn diff_signature(
    left: &GraphStore,
    right: &GraphStore,
    qualified_name: &str,
) -> Option<SignatureDiff> {
    let left_sym = left.get_symbol_ref(qualified_name)?;
    let right_sym = right.get_symbol_ref(qualified_name)?;

    Some(SignatureDiff {
        left_file: left
            .get_file_rel_path(left_sym.file_instance_id)
            .to_string(),
        right_file: right
            .get_file_rel_path(right_sym.file_instance_id)
            .to_string(),
        file_changed: left.get_file_rel_path(left_sym.file_instance_id)
            != right.get_file_rel_path(right_sym.file_instance_id),
        left_start_line: left_sym.start_line,
        left_end_line: left_sym.end_line,
        right_start_line: right_sym.start_line,
        right_end_line: right_sym.end_line,
        line_range_changed: left_sym.start_line != right_sym.start_line
            || left_sym.end_line != right_sym.end_line,
        left_kind: left_sym.kind.as_str().to_string(),
        right_kind: right_sym.kind.as_str().to_string(),
        kind_changed: left_sym.kind != right_sym.kind,
    })
}

/// 对比两个 snapshot 中同一 qualified_name 的 caller/callee 边集合
///
/// 返回 EdgeDeltaSummary，包含新增/移除的 caller 和 callee
pub fn diff_edges(
    left: &GraphStore,
    right: &GraphStore,
    qualified_name: &str,
) -> Option<(EdgeDeltaSummary, EdgeDeltaSummary)> {
    let left_sym = left.get_symbol_ref(qualified_name)?;
    let right_sym = right.get_symbol_ref(qualified_name)?;

    // 1. caller diff: 谁调用了这个函数
    let left_callers = left.get_caller_ids(left_sym.id);
    let right_callers = right.get_caller_ids(right_sym.id);
    let caller_delta = compute_edge_delta(left, &left_callers, right, &right_callers);

    // 2. callee diff: 这个函数调用了谁
    let left_callees = left.get_callee_ids(left_sym.id);
    let right_callees = right.get_callee_ids(right_sym.id);
    let callee_delta = compute_edge_delta(left, &left_callees, right, &right_callees);

    Some((caller_delta, callee_delta))
}

/// 对比两个 snapshot 中同一 qualified_name 的 caller 边集合
///
/// 基于 resolved edge delta：只返回 caller 的增删（谁开始/不再调用这个函数）。
/// 返回的 EdgeDeltaSummary 中只有 added_callers / removed_callers 有值，
/// added_callees / removed_callees 为空。
///
/// 符号在任一侧不存在时返回 None（与 diff_signature 一致）。
/// 设计参考：enterprise-daemon-shared-snapshot-plan.md §12.3 Query API
pub fn diff_callers(
    left: &GraphStore,
    right: &GraphStore,
    qualified_name: &str,
) -> Option<EdgeDeltaSummary> {
    let (caller_delta_raw, _) = diff_edges(left, right, qualified_name)?;
    Some(EdgeDeltaSummary {
        added_callers: caller_delta_raw.added_callers,
        removed_callers: caller_delta_raw.removed_callers,
        added_callees: vec![],
        removed_callees: vec![],
    })
}

/// 对比两个 snapshot 中同一 qualified_name 的 callee 边集合
///
/// 基于 resolved edge delta：只返回 callee 的增删（这个函数开始/不再调用谁）。
/// 返回的 EdgeDeltaSummary 中只有 added_callees / removed_callees 有值，
/// added_callers / removed_callers 为空。
///
/// 符号在任一侧不存在时返回 None（与 diff_signature 一致）。
/// 设计参考：enterprise-daemon-shared-snapshot-plan.md §12.3 Query API
pub fn diff_callees(
    left: &GraphStore,
    right: &GraphStore,
    qualified_name: &str,
) -> Option<EdgeDeltaSummary> {
    let (_, callee_delta_raw) = diff_edges(left, right, qualified_name)?;
    Some(EdgeDeltaSummary {
        added_callers: vec![],
        removed_callers: vec![],
        added_callees: callee_delta_raw.added_callers,
        removed_callees: callee_delta_raw.removed_callers,
    })
}

/// 计算两组 symbol_id 的增删差异，返回 qualified_name 列表
fn compute_edge_delta(
    left: &GraphStore,
    left_ids: &[u32],
    right: &GraphStore,
    right_ids: &[u32],
) -> EdgeDeltaSummary {
    // 用 qualified_name 集合做对比（而非 symbol_id，因为两个 snapshot 的 id 可能不同）
    let left_names: HashSet<String> = left_ids
        .iter()
        .filter_map(|&id| {
            left.get_symbol_by_id(id)
                .map(|s| left.symbol_qname(s).to_string())
        })
        .collect();
    let right_names: HashSet<String> = right_ids
        .iter()
        .filter_map(|&id| {
            right
                .get_symbol_by_id(id)
                .map(|s| right.symbol_qname(s).to_string())
        })
        .collect();

    let added: Vec<String> = right_names.difference(&left_names).cloned().collect();
    let removed: Vec<String> = left_names.difference(&right_names).cloned().collect();

    let mut delta = EdgeDeltaSummary::default();
    // added_callers / removed_callers 由调用方区分，这里统一返回 added/removed
    delta.added_callers = added;
    delta.removed_callers = removed;
    delta
}

/// 对比两个 snapshot 中同一 qualified_name 的符号完整差异
///
/// 匹配规则（当前实现 tier 2）：
/// 1. 左右都存在且 qualified_name 一致 → 进一步对比签名和边
/// 2. 仅右侧存在 → Added
/// 3. 仅左侧存在 → Removed
///
/// change_kind 判定优先级：
/// - 文件路径变化 → Moved
/// - 行号/kind 变化 → SignatureChanged
/// - caller 集合变化 → CallersChanged
/// - callee 集合变化 → CalleesChanged
/// - 都没变 → Unchanged
pub fn diff_symbol(
    left: &GraphStore,
    right: &GraphStore,
    qualified_name: &str,
) -> SymbolDiffRecord {
    let left_sym = left.get_symbol_ref(qualified_name);
    let right_sym = right.get_symbol_ref(qualified_name);

    match (left_sym, right_sym) {
        (None, None) => SymbolDiffRecord {
            qualified_name: qualified_name.to_string(),
            change_kind: SymbolChangeKind::Ambiguous,
            signature_change: SignatureDiff::default(),
            caller_delta: EdgeDeltaSummary::default(),
            callee_delta: EdgeDeltaSummary::default(),
        },
        (Some(_), None) => SymbolDiffRecord {
            qualified_name: qualified_name.to_string(),
            change_kind: SymbolChangeKind::Removed,
            signature_change: SignatureDiff::default(),
            caller_delta: EdgeDeltaSummary::default(),
            callee_delta: EdgeDeltaSummary::default(),
        },
        (None, Some(_)) => SymbolDiffRecord {
            qualified_name: qualified_name.to_string(),
            change_kind: SymbolChangeKind::Added,
            signature_change: SignatureDiff::default(),
            caller_delta: EdgeDeltaSummary::default(),
            callee_delta: EdgeDeltaSummary::default(),
        },
        (Some(_left_s), Some(_right_s)) => {
            // 两侧都存在，对比签名和边
            let sig = diff_signature(left, right, qualified_name).unwrap_or_default();

            // 计算 edge delta
            let (caller_delta_raw, callee_delta_raw) =
                diff_edges(left, right, qualified_name).unwrap_or_default();

            // 重新分配 delta 到 caller/callee 字段
            // compute_edge_delta 统一返回 added_callers/removed_callers，
            // 需要区分 caller delta 和 callee delta
            let caller_delta = EdgeDeltaSummary {
                added_callers: caller_delta_raw.added_callers,
                removed_callers: caller_delta_raw.removed_callers,
                added_callees: vec![],
                removed_callees: vec![],
            };
            let callee_delta = EdgeDeltaSummary {
                added_callers: vec![],
                removed_callers: vec![],
                added_callees: callee_delta_raw.added_callers,
                removed_callees: callee_delta_raw.removed_callers,
            };

            // 判定 change_kind（优先级从高到低）
            let change_kind = if sig.file_changed {
                SymbolChangeKind::Moved
            } else if sig.line_range_changed || sig.kind_changed {
                SymbolChangeKind::SignatureChanged
            } else if caller_delta.has_changes() {
                SymbolChangeKind::CallersChanged
            } else if callee_delta.has_changes() {
                SymbolChangeKind::CalleesChanged
            } else {
                SymbolChangeKind::Unchanged
            };

            SymbolDiffRecord {
                qualified_name: qualified_name.to_string(),
                change_kind,
                signature_change: sig,
                caller_delta,
                callee_delta,
            }
        }
    }
}

// ============================================
// compare_snapshots: scope 级批量对比
// ============================================

/// Scope 过滤器
#[derive(Clone, Debug)]
pub enum ScopeFilter {
    /// 仓库级（所有符号）
    Repo,
    /// 文件级（按 file_rel_path 过滤）
    File(String),
    /// 模块级（按 module_path 过滤）
    Module(String),
}

impl ScopeFilter {
    /// 将 ScopeFilter 转换为 (file_filter, module_filter) 元组
    ///
    /// 供 GraphStore.get_all_qualified_names 使用
    fn to_filters(&self) -> (Option<&str>, Option<&str>) {
        match self {
            ScopeFilter::Repo => (None, None),
            ScopeFilter::File(path) => (Some(path.as_str()), None),
            ScopeFilter::Module(module_path) => (None, Some(module_path.as_str())),
        }
    }
}

/// 统计两个 snapshot 中匹配 scope 的符号数量（并集）
///
/// 用于判断是否应走同步路径还是转后台 job。
/// 设计参考：enterprise-daemon-shared-snapshot-plan.md §12.3 / §12.4
pub fn count_symbols_in_scope(left: &GraphStore, right: &GraphStore, scope: &ScopeFilter) -> usize {
    let (file_filter, module_filter) = scope.to_filters();
    let mut qnames = HashSet::new();
    for qname in left.get_all_qualified_names(file_filter, module_filter) {
        qnames.insert(qname);
    }
    for qname in right.get_all_qualified_names(file_filter, module_filter) {
        qnames.insert(qname);
    }
    qnames.len()
}

/// 对比两个 snapshot 中指定 scope 内的所有符号差异
///
/// 遍历 scope 内的符号并集，对每个符号调用 diff_symbol，
/// 返回所有有变化的 SymbolDiffRecord（unchanged 的不包含）。
///
/// scope:
/// - ScopeFilter::Repo: 仓库级（调用方应先用 count_symbols_in_scope 检查大小）
/// - ScopeFilter::File(path): 文件级
/// - ScopeFilter::Module(path): 模块级
///
/// 设计参考：enterprise-daemon-shared-snapshot-plan.md §12.3 compare_snapshots
pub fn compare_snapshots(
    left: &GraphStore,
    right: &GraphStore,
    scope: &ScopeFilter,
) -> Vec<SymbolDiffRecord> {
    // 1. 收集 scope 内的 qualified_names 并集
    let (file_filter, module_filter) = scope.to_filters();
    let mut qnames = HashSet::new();
    for qname in left.get_all_qualified_names(file_filter, module_filter) {
        qnames.insert(qname);
    }
    for qname in right.get_all_qualified_names(file_filter, module_filter) {
        qnames.insert(qname);
    }

    // 2. 对每个 qualified_name 调用 diff_symbol，过滤 unchanged
    qnames
        .into_iter()
        .filter_map(|qname| {
            let record = diff_symbol(left, right, &qname);
            if record.change_kind != SymbolChangeKind::Unchanged {
                Some(record)
            } else {
                None
            }
        })
        .collect()
}

// ============================================
// PyO3 序列化辅助
// ============================================

/// 将 SignatureDiff 转为 Python dict
pub fn signature_diff_to_pydict<'py>(
    py: Python<'py>,
    sig: &SignatureDiff,
) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("left_file", &sig.left_file)?;
    d.set_item("right_file", &sig.right_file)?;
    d.set_item("file_changed", sig.file_changed)?;
    d.set_item("left_start_line", sig.left_start_line)?;
    d.set_item("left_end_line", sig.left_end_line)?;
    d.set_item("right_start_line", sig.right_start_line)?;
    d.set_item("right_end_line", sig.right_end_line)?;
    d.set_item("line_range_changed", sig.line_range_changed)?;
    d.set_item("left_kind", &sig.left_kind)?;
    d.set_item("right_kind", &sig.right_kind)?;
    d.set_item("kind_changed", sig.kind_changed)?;
    Ok(d)
}

/// 将 EdgeDeltaSummary 转为 Python dict
pub fn edge_delta_to_pydict<'py>(
    py: Python<'py>,
    delta: &EdgeDeltaSummary,
) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    let added_callers = PyList::new(py, delta.added_callers.iter().map(String::as_str))?;
    let removed_callers = PyList::new(py, delta.removed_callers.iter().map(String::as_str))?;
    let added_callees = PyList::new(py, delta.added_callees.iter().map(String::as_str))?;
    let removed_callees = PyList::new(py, delta.removed_callees.iter().map(String::as_str))?;
    d.set_item("added_callers", added_callers)?;
    d.set_item("removed_callers", removed_callers)?;
    d.set_item("added_callees", added_callees)?;
    d.set_item("removed_callees", removed_callees)?;
    Ok(d)
}

/// 将 SymbolDiffRecord 转为 Python dict
pub fn symbol_diff_to_pydict<'py>(
    py: Python<'py>,
    record: &SymbolDiffRecord,
) -> PyResult<Bound<'py, PyDict>> {
    let d = PyDict::new(py);
    d.set_item("qualified_name", &record.qualified_name)?;
    d.set_item("change_kind", record.change_kind.as_str())?;
    let sig = signature_diff_to_pydict(py, &record.signature_change)?;
    d.set_item("signature_change", sig)?;
    let caller = edge_delta_to_pydict(py, &record.caller_delta)?;
    d.set_item("caller_delta", caller)?;
    let callee = edge_delta_to_pydict(py, &record.callee_delta)?;
    d.set_item("callee_delta", callee)?;
    Ok(d)
}

/// 将 Vec<SymbolDiffRecord> 转为 Python list of dict
pub fn symbol_diff_list_to_pylist<'py>(
    py: Python<'py>,
    records: &[SymbolDiffRecord],
) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty(py);
    for record in records {
        let dict = symbol_diff_to_pydict(py, record)?;
        list.append(dict)?;
    }
    Ok(list)
}
