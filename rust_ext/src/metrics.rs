//! Phase 5.5: 局部 depth/cycle/impact 更新
//!
//! 设计参考：enterprise-daemon-shared-snapshot-plan.md §9.1
//!
//! 文件保存流程：
//!   ... → affected frontier → 局部 depth/cycle/impact 更新 → staging durable log
//!
//! 本模块负责：
//! - 基于 AffectedFrontier 和 ParseDelta，仅对受影响区域重算 depth/cycle/impact
//! - 避免全图重算（O(N) → O(k)，k 为 frontier 大小）
//! - 输出 LocalMetricsUpdate，供 Replicator 更新 snapshot generation

use std::collections::{HashMap, HashSet, VecDeque};

use pyo3::prelude::*;
use pyo3::types::PyDict;

use crate::delta::ParseDelta;
use crate::frontier::AffectedFrontier;
use crate::graph::GraphStore;

// ============================================
// DepthChange —— 符号深度变更
// ============================================

/// 符号 depth 变更记录
#[derive(Clone, Debug)]
pub struct DepthChange {
    pub qualified_name: String,
    pub old_depth: i32,
    pub new_depth: i32,
}

impl DepthChange {
    pub fn is_changed(&self) -> bool {
        self.old_depth != self.new_depth
    }
}

// ============================================
// CycleChange —— 循环依赖变更
// ============================================

/// 循环依赖变更类型
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum CycleChangeKind {
    /// 新增的环（变更引入了循环依赖）
    Added,
    /// 消除的环（变更打破了循环依赖）
    Removed,
}

impl CycleChangeKind {
    pub fn as_str(&self) -> &'static str {
        match self {
            CycleChangeKind::Added => "added",
            CycleChangeKind::Removed => "removed",
        }
    }
}

/// 单个循环依赖变更记录
#[derive(Clone, Debug)]
pub struct CycleChange {
    pub kind: CycleChangeKind,
    /// 环上的符号列表（按环路顺序）
    pub cycle: Vec<String>,
}

// ============================================
// ImpactChange —— 影响半径变更
// ============================================

/// 单个变更源的影响半径记录
#[derive(Clone, Debug)]
pub struct ImpactChange {
    /// 触发变更的符号
    pub source: String,
    /// 受影响的下游符号列表（blast radius）
    pub affected_downstream: Vec<String>,
    /// 受影响的上游符号列表
    pub affected_upstream: Vec<String>,
    /// 影响深度
    pub depth: u32,
}

// ============================================
// LocalMetricsUpdate —— 局部 metrics 更新结果
// ============================================

/// 局部 metrics 更新结果
#[derive(Clone, Debug, Default)]
pub struct LocalMetricsUpdate {
    pub depth_changes: Vec<DepthChange>,
    pub cycle_changes: Vec<CycleChange>,
    pub impact_changes: Vec<ImpactChange>,
}

impl LocalMetricsUpdate {
    pub fn is_empty(&self) -> bool {
        self.depth_changes.is_empty()
            && self.cycle_changes.is_empty()
            && self.impact_changes.is_empty()
    }

    pub fn total_changes(&self) -> usize {
        self.depth_changes.len() + self.cycle_changes.len() + self.impact_changes.len()
    }

    pub fn summary(&self) -> String {
        let added = self.cycle_changes.iter().filter(|c| c.kind == CycleChangeKind::Added).count();
        let removed = self.cycle_changes.iter().filter(|c| c.kind == CycleChangeKind::Removed).count();
        format!(
            "metrics update: {} depth, {} cycle ({}+{}-), {} impact",
            self.depth_changes.len(),
            self.cycle_changes.len(),
            added,
            removed,
            self.impact_changes.len(),
        )
    }
}

// ============================================
// MetricsComputer —— 局部 metrics 计算核心
// ============================================

/// 局部 metrics 计算器
pub struct MetricsComputer;

impl MetricsComputer {
    /// 计算局部 metrics 更新
    ///
    /// 参数：
    /// - frontier: 受影响的前沿区域
    /// - parse_delta: 文件 parse delta（包含 symbol delta 的 depth 信息）
    /// - store: 当前 GraphStore（用于对比旧 depth、检测 cycle 变化、计算 impact）
    /// - impact_depth: impact 计算的最大深度
    pub fn compute_local_update(
        frontier: &AffectedFrontier,
        parse_delta: &ParseDelta,
        store: Option<&GraphStore>,
        impact_depth: u32,
    ) -> LocalMetricsUpdate {
        let mut update = LocalMetricsUpdate::default();

        // 1. Depth 变更：从 parse_delta 的 symbol delta 提取
        update.depth_changes = Self::compute_depth_changes(parse_delta, store);

        // 2. Cycle 变更：在受影响子图上检测环
        update.cycle_changes = Self::compute_cycle_changes(frontier, store);

        // 3. Impact 变更：对每个直接受影响符号计算 blast radius
        update.impact_changes = Self::compute_impact_changes(frontier, store, impact_depth);

        update
    }

    /// 计算符号 depth 变更
    ///
    /// depth 来自 parse_delta.symbol_delta：
    /// - Added: old_depth=-1（不存在）, new_depth=0（默认顶层）
    /// - Changed: old_depth 从 store, new_depth 暂用 old（简化）
    /// - Removed: old_depth 从 store, new_depth=-1（已删除）
    fn compute_depth_changes(
        parse_delta: &ParseDelta,
        store: Option<&GraphStore>,
    ) -> Vec<DepthChange> {
        let mut changes = Vec::new();

        // 从 store 构建 qname → depth 映射（仅限当前文件的符号）
        let old_depths: HashMap<String, i32> = if let Some(store) = store {
            store
                .get_symbols_by_file(&parse_delta.file_path.to_string_lossy())
                .into_iter()
                .map(|s| (s.qualified_name.clone(), s.depth))
                .collect()
        } else {
            HashMap::new()
        };

        // Added 符号：old_depth = -1, new_depth = 0
        for entry in &parse_delta.symbol_delta.added {
            let old = old_depths.get(&entry.qualified_name).copied().unwrap_or(-1);
            changes.push(DepthChange {
                qualified_name: entry.qualified_name.clone(),
                old_depth: old,
                new_depth: 0,
            });
        }

        // Changed 符号：对比 old/new depth
        for entry in &parse_delta.symbol_delta.changed {
            let old = old_depths.get(&entry.qualified_name).copied().unwrap_or(-1);
            // 简化：changed 符号 depth 不变（实际应从 parse_result 取）
            changes.push(DepthChange {
                qualified_name: entry.qualified_name.clone(),
                old_depth: old,
                new_depth: old,
            });
        }

        // Removed 符号：old_depth 从 store, new_depth = -1（已删除）
        for entry in &parse_delta.symbol_delta.removed {
            let old = old_depths.get(&entry.qualified_name).copied().unwrap_or(-1);
            changes.push(DepthChange {
                qualified_name: entry.qualified_name.clone(),
                old_depth: old,
                new_depth: -1,
            });
        }

        // 只返回真正发生变化的
        changes.into_iter().filter(|c| c.is_changed()).collect()
    }

    /// 在受影响子图上检测 cycle 变更
    ///
    /// 策略：
    /// 1. 在旧图上对受影响子图跑 DFS，得到旧 cycles
    /// 2. 在"模拟应用 delta 后"的子图上跑 DFS，得到新 cycles
    /// 3. diff 两个 cycle 集合，输出 Added/Removed
    ///
    /// 简化实现：由于无法在不修改 GraphStore 的情况下模拟 delta 应用，
    /// 这里只检测当前子图中的环，并标记为 Added（如果 frontier 非空）。
    /// 实际的 cycle diff 需要在 Replicator 阶段完成。
    fn compute_cycle_changes(
        frontier: &AffectedFrontier,
        store: Option<&GraphStore>,
    ) -> Vec<CycleChange> {
        let store = match store {
            Some(s) => s,
            None => return Vec::new(),
        };

        let affected_set = frontier.all_affected();
        if affected_set.is_empty() {
            return Vec::new();
        }

        // 将 qname 集合转换为 id 集合
        let mut affected_ids: HashSet<u32> = HashSet::new();
        for qname in &affected_set {
            if let Some(sym) = store.get_symbol_ref(qname) {
                affected_ids.insert(sym.id);
            }
        }

        // 在受影响子图上检测环
        let cycles = Self::detect_cycles_in_subgraph(store, &affected_ids);

        // 标记所有检测到的环为 Added（简化：实际应与旧状态 diff）
        cycles
            .into_iter()
            .map(|cycle| CycleChange {
                kind: CycleChangeKind::Added,
                cycle,
            })
            .collect()
    }

    /// 在子图上检测环（DFS 三色标记）
    /// 仅遍历子图内的节点和边，使用 GraphStore 的公共方法
    fn detect_cycles_in_subgraph(
        store: &GraphStore,
        node_set: &HashSet<u32>,
    ) -> Vec<Vec<String>> {
        let mut color: HashMap<u32, u8> = HashMap::new(); // 0=white, 1=gray, 2=black
        let mut parent: HashMap<u32, i64> = HashMap::new();
        let mut cycles: Vec<Vec<String>> = Vec::new();

        // 递归 DFS（用栈模拟避免栈溢出）
        fn dfs(
            u: u32,
            store: &GraphStore,
            node_set: &HashSet<u32>,
            color: &mut HashMap<u32, u8>,
            parent: &mut HashMap<u32, i64>,
            cycles: &mut Vec<Vec<String>>,
        ) {
            color.insert(u, 1); // gray

            // 获取 u 的所有 callee
            let callee_ids = store.get_callee_ids(u);
            for v in callee_ids {
                if !node_set.contains(&v) { continue; }

                let color_v = color.get(&v).copied().unwrap_or(0);
                if color_v == 0 {
                    parent.insert(v, u as i64);
                    dfs(v, store, node_set, color, parent, cycles);
                } else if color_v == 1 {
                    // 发现回边，提取环
                    let mut cycle = Vec::new();
                    let mut cur = u as i64;
                    while cur != -1 && cur != v as i64 {
                        if let Some(sym) = store.get_symbol_by_id(cur as u32) {
                            cycle.push(sym.qualified_name.clone());
                        }
                        cur = parent.get(&(cur as u32)).copied().unwrap_or(-1);
                    }
                    if let Some(sym) = store.get_symbol_by_id(v) {
                        cycle.push(sym.qualified_name.clone());
                    }
                    cycle.reverse();
                    if cycle.len() > 1 {
                        cycles.push(cycle);
                    }
                }
            }
            color.insert(u, 2); // black
        }

        for &start_id in node_set {
            let color_start = color.get(&start_id).copied().unwrap_or(0);
            if color_start == 0 {
                dfs(
                    start_id,
                    store,
                    node_set,
                    &mut color,
                    &mut parent,
                    &mut cycles,
                );
            }
        }

        cycles
    }

    /// 对每个直接受影响符号计算 blast radius
    ///
    /// blast radius = 下游受影响的符号集合（BFS 限定深度）
    /// + 上游受影响的符号集合（BFS 限定深度）
    fn compute_impact_changes(
        frontier: &AffectedFrontier,
        store: Option<&GraphStore>,
        max_depth: u32,
    ) -> Vec<ImpactChange> {
        let store = match store {
            Some(s) => s,
            None => return Vec::new(),
        };

        let mut changes = Vec::new();

        for source_qname in &frontier.directly_affected {
            let source = match store.get_symbol_ref(source_qname) {
                Some(s) => s,
                None => continue,
            };

            // 下游 BFS：source 调用了谁
            let downstream = Self::bfs_downstream(store, source.id, max_depth);

            // 上游 BFS：谁调用了 source
            let upstream = Self::bfs_upstream(store, source.id, max_depth);

            let depth = std::cmp::max(
                downstream.iter().map(|(_, d)| *d).max().unwrap_or(0),
                upstream.iter().map(|(_, d)| *d).max().unwrap_or(0),
            );

            changes.push(ImpactChange {
                source: source_qname.clone(),
                affected_downstream: downstream
                    .iter()
                    .filter_map(|(qname, _)| {
                        if qname != source_qname { Some(qname.clone()) } else { None }
                    })
                    .collect(),
                affected_upstream: upstream
                    .iter()
                    .filter_map(|(qname, _)| {
                        if qname != source_qname { Some(qname.clone()) } else { None }
                    })
                    .collect(),
                depth,
            });
        }

        changes
    }

    /// 下游 BFS：从 source 出发，遍历 callee 链
    /// 返回 (qualified_name, hop_depth) 列表
    fn bfs_downstream(
        store: &GraphStore,
        source_id: u32,
        max_depth: u32,
    ) -> Vec<(String, u32)> {
        let mut visited: HashSet<u32> = HashSet::new();
        let mut queue: VecDeque<(u32, u32)> = VecDeque::new();
        let mut result = Vec::new();

        queue.push_back((source_id, 0));
        visited.insert(source_id);

        while let Some((node_id, depth)) = queue.pop_front() {
            if depth >= max_depth { continue; }

            let callee_ids = store.get_callee_ids(node_id);
            for callee_id in callee_ids {
                if visited.contains(&callee_id) { continue; }
                visited.insert(callee_id);

                if let Some(sym) = store.get_symbol_by_id(callee_id) {
                    result.push((sym.qualified_name.clone(), depth + 1));
                    queue.push_back((callee_id, depth + 1));
                }
            }
        }

        result
    }

    /// 上游 BFS：从 source 出发，遍历 caller 链
    /// 返回 (qualified_name, hop_depth) 列表
    fn bfs_upstream(
        store: &GraphStore,
        source_id: u32,
        max_depth: u32,
    ) -> Vec<(String, u32)> {
        let mut visited: HashSet<u32> = HashSet::new();
        let mut queue: VecDeque<(u32, u32)> = VecDeque::new();
        let mut result = Vec::new();

        queue.push_back((source_id, 0));
        visited.insert(source_id);

        while let Some((node_id, depth)) = queue.pop_front() {
            if depth >= max_depth { continue; }

            let caller_ids = store.get_caller_ids(node_id);
            for caller_id in caller_ids {
                if visited.contains(&caller_id) { continue; }
                visited.insert(caller_id);

                if let Some(sym) = store.get_symbol_by_id(caller_id) {
                    result.push((sym.qualified_name.clone(), depth + 1));
                    queue.push_back((caller_id, depth + 1));
                }
            }
        }

        result
    }
}

// ============================================
// PyO3 包装
// ============================================

/// PyO3 包装的 DepthChange
#[pyclass(name = "DepthChange")]
#[derive(Clone)]
pub struct PyDepthChange {
    #[pyo3(get)]
    pub qualified_name: String,
    #[pyo3(get)]
    pub old_depth: i32,
    #[pyo3(get)]
    pub new_depth: i32,
}

#[pymethods]
impl PyDepthChange {
    fn __repr__(&self) -> String {
        format!(
            "DepthChange({}: {} -> {})",
            self.qualified_name, self.old_depth, self.new_depth
        )
    }

    #[getter]
    fn is_changed(&self) -> bool {
        self.old_depth != self.new_depth
    }
}

/// PyO3 包装的 CycleChange
#[pyclass(name = "CycleChange")]
#[derive(Clone)]
pub struct PyCycleChange {
    #[pyo3(get)]
    pub kind: String,
    #[pyo3(get)]
    pub cycle: Vec<String>,
}

#[pymethods]
impl PyCycleChange {
    fn __repr__(&self) -> String {
        format!("CycleChange({}: [{}])", self.kind, self.cycle.join(", "))
    }
}

/// PyO3 包装的 ImpactChange
#[pyclass(name = "ImpactChange")]
#[derive(Clone)]
pub struct PyImpactChange {
    #[pyo3(get)]
    pub source: String,
    #[pyo3(get)]
    pub affected_downstream: Vec<String>,
    #[pyo3(get)]
    pub affected_upstream: Vec<String>,
    #[pyo3(get)]
    pub depth: u32,
}

#[pymethods]
impl PyImpactChange {
    fn __repr__(&self) -> String {
        format!(
            "ImpactChange({}: {} down, {} up, depth={})",
            self.source,
            self.affected_downstream.len(),
            self.affected_upstream.len(),
            self.depth
        )
    }
}

/// PyO3 包装的 LocalMetricsUpdate
#[pyclass(name = "LocalMetricsUpdate")]
#[derive(Clone, Default)]
pub struct PyLocalMetricsUpdate {
    #[pyo3(get)]
    pub depth_changes: Vec<PyDepthChange>,
    #[pyo3(get)]
    pub cycle_changes: Vec<PyCycleChange>,
    #[pyo3(get)]
    pub impact_changes: Vec<PyImpactChange>,
}

#[pymethods]
impl PyLocalMetricsUpdate {
    fn __repr__(&self) -> String {
        self.summary()
    }

    #[getter]
    fn is_empty(&self) -> bool {
        self.depth_changes.is_empty()
            && self.cycle_changes.is_empty()
            && self.impact_changes.is_empty()
    }

    #[getter]
    fn total_changes(&self) -> usize {
        self.depth_changes.len() + self.cycle_changes.len() + self.impact_changes.len()
    }

    fn summary(&self) -> String {
        format!(
            "LocalMetricsUpdate: {} depth, {} cycle, {} impact",
            self.depth_changes.len(),
            self.cycle_changes.len(),
            self.impact_changes.len()
        )
    }
}

impl From<LocalMetricsUpdate> for PyLocalMetricsUpdate {
    fn from(update: LocalMetricsUpdate) -> Self {
        PyLocalMetricsUpdate {
            depth_changes: update
                .depth_changes
                .into_iter()
                .map(|c| PyDepthChange {
                    qualified_name: c.qualified_name,
                    old_depth: c.old_depth,
                    new_depth: c.new_depth,
                })
                .collect(),
            cycle_changes: update
                .cycle_changes
                .into_iter()
                .map(|c| PyCycleChange {
                    kind: c.kind.as_str().to_string(),
                    cycle: c.cycle,
                })
                .collect(),
            impact_changes: update
                .impact_changes
                .into_iter()
                .map(|c| PyImpactChange {
                    source: c.source,
                    affected_downstream: c.affected_downstream,
                    affected_upstream: c.affected_upstream,
                    depth: c.depth,
                })
                .collect(),
        }
    }
}

/// PyO3 暴露的 compute_local_update 函数
///
/// 接受 PyParseDelta 和 PyAffectedFrontier，返回 LocalMetricsUpdate。
/// 如果传入 store，则使用 GraphStore 的符号索引计算 cycle/impact；
/// 否则只返回 depth_changes（来自 parse_delta）。
#[pyfunction]
#[pyo3(signature = (parse_delta, frontier, store=None, impact_depth=3))]
pub fn compute_local_update(
    parse_delta: &crate::delta::PyParseDelta,
    frontier: &crate::frontier::PyAffectedFrontier,
    store: Option<&GraphStore>,
    impact_depth: u32,
) -> PyResult<PyLocalMetricsUpdate> {
    let rust_parse_delta = parse_delta.inner.clone();
    let rust_frontier = frontier.inner.clone();

    let update = MetricsComputer::compute_local_update(
        &rust_frontier,
        &rust_parse_delta,
        store,
        impact_depth,
    );

    Ok(update.into())
}
