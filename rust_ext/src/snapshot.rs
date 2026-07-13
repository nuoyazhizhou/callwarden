//! Phase 4: GraphSnapshot — 只读内存快照 + ArcSwap 原子发布
//!
//! 设计要点：
//! - GraphSnapshot 包装 GraphStore 数据 + 元数据（generation/workspace_id/health）
//! - SnapshotManager 使用 ArcSwap 无锁读，发布时原子替换 Arc<GraphSnapshot>
//! - 多 workspace 通过 SnapshotCache 统一管理，按 workspace_instance_id 检索
//! - 旧 generation 被 in-flight 查询持有，查询结束后自然释放
//!
//! 参考：enterprise-daemon-shared-snapshot-plan.md §8

use std::collections::HashMap;
use std::sync::Arc;
use arc_swap::{ArcSwap, Guard};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use crate::graph::GraphStore;
use crate::diff;

// ============================================
// 类型别名
// ============================================

/// workspace 实例 ID（16 位 hex 字符串，由 register_workspace 生成）
pub type WorkspaceInstanceId = String;

/// 共享 snapshot ID（clean workspace 可共享）
pub type SnapshotId = String;

/// 内容/上下文 hash（sha256 hex）
pub type Hash = String;

/// generation 单调递增版本号
pub type Generation = u64;

// ============================================
// GraphSnapshot
// ============================================

/// 只读内存快照。
///
/// 由 Coordinator 在后台构建，构建完成后通过 SnapshotManager::publish 原子发布。
/// 在线查询读 Arc<GraphSnapshot>，发布时不阻塞正在运行的查询。
///
/// 注：GraphStore 本身持有 symbols/calls 内存索引，GraphSnapshot 在其上
/// 增加 generation/workspace_id/health 等元数据。
pub struct GraphSnapshot {
    pub workspace_instance_id: WorkspaceInstanceId,
    pub snapshot_id: Option<SnapshotId>,
    pub generation: Generation,
    pub build_context_hash: Hash,
    pub source_db_path: String,
    /// 内部 GraphStore（symbols + calls + CSR 索引）
    pub store: Arc<GraphStore>,
    pub health: SnapshotHealth,
}

/// 快照健康状态
#[derive(Clone, Debug, Default)]
pub struct SnapshotHealth {
    pub symbol_count: usize,
    pub call_count: usize,
    pub file_count: usize,
    pub build_duration_ms: u64,
    pub last_error: Option<String>,
}

impl GraphSnapshot {
    /// 创建新快照（load_from_sqlite 已完成后调用）
    pub fn new(
        workspace_instance_id: WorkspaceInstanceId,
        snapshot_id: Option<SnapshotId>,
        generation: Generation,
        build_context_hash: Hash,
        source_db_path: String,
        store: Arc<GraphStore>,
        health: SnapshotHealth,
    ) -> Self {
        Self {
            workspace_instance_id,
            snapshot_id,
            generation,
            build_context_hash,
            source_db_path,
            store,
            health,
        }
    }
}

// ============================================
// SnapshotManager — 单 workspace 的 ArcSwap 原子发布
// ============================================

/// 单个 workspace 的 snapshot manager。
///
/// 读路径无锁：load() 返回 Guard<Arc<GraphSnapshot>>，发布时不阻塞。
/// 写路径：publish() 原子替换内部 ArcSwap 的指针。
///
/// Rust 侧用法：
/// ```ignore
/// let mgr = SnapshotManager::new("ws_abc".into());
/// let snap = mgr.build_and_publish(db_path, ctx_hash).await?;
/// let reader = mgr.load(); // 无锁读
/// println!("generation: {}", reader.generation);
/// ```
pub struct SnapshotManager {
    workspace_instance_id: WorkspaceInstanceId,
    current: ArcSwap<Option<Arc<GraphSnapshot>>>,
    next_generation: std::sync::atomic::AtomicU64,
}

impl SnapshotManager {
    pub fn new(workspace_instance_id: WorkspaceInstanceId) -> Self {
        Self {
            workspace_instance_id,
            current: ArcSwap::from_pointee(None),
            next_generation: std::sync::atomic::AtomicU64::new(1),
        }
    }

    /// 读取当前 generation（若尚未发布返回 0）
    pub fn current_generation(&self) -> Generation {
        let guard = self.current.load();
        match guard.as_ref() {
            Some(snap) => snap.generation,
            None => 0,
        }
    }

    /// 加载当前 snapshot 的 Guard（无锁读，发布时不阻塞）
    pub fn load(&self) -> Guard<Arc<Option<Arc<GraphSnapshot>>>> {
        self.current.load()
    }

    /// 原子发布新 snapshot，返回被替换的旧 snapshot（若有）
    pub fn publish(&self, snapshot: Arc<GraphSnapshot>) -> Option<Arc<GraphSnapshot>> {
        // ArcSwap::swap 接收 T = Arc<Option<Arc<GraphSnapshot>>>
        // 返回旧 T，需要先取 Arc 内部值再 try_unwrap
        let old: Arc<Option<Arc<GraphSnapshot>>> = self.current.swap(Some(snapshot).into());
        match Arc::try_unwrap(old) {
            Ok(opt) => opt,
            Err(_arc) => None, // 还有读者在持有，不强制拿回
        }
    }

    /// 分配下一个 generation 号
    pub fn alloc_generation(&self) -> Generation {
        self.next_generation.fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    }

    /// 获取当前 snapshot 的 GraphStore 引用（供 diff 模块只读访问）
    pub fn current_store(&self) -> Option<Arc<GraphStore>> {
        let guard = self.current.load();
        // guard derefs to Arc<Option<Arc<GraphSnapshot>>>
        // 第一个 as_ref(): Arc::as_ref() → &Option<Arc<GraphSnapshot>>
        // 第二个 as_ref(): Option::as_ref() → Option<&Arc<GraphSnapshot>>
        guard.as_ref().as_ref().map(|snap| snap.store.clone())
    }
}

// ============================================
// SnapshotCache — 多 workspace 统一管理
// ============================================

/// 多 workspace 的 snapshot 缓存。
///
/// 按 workspace_instance_id 索引，每个 workspace 独立维护 generation。
/// LRU 淘汰策略：超过 max_workspaces 时淘汰最久未访问的 workspace。
pub struct SnapshotCache {
    managers: parking_lot::RwLock<HashMap<WorkspaceInstanceId, Arc<SnapshotManager>>>,
    max_workspaces: usize,
}

impl SnapshotCache {
    pub fn new(max_workspaces: usize) -> Self {
        Self {
            managers: parking_lot::RwLock::new(HashMap::new()),
            max_workspaces,
        }
    }

    /// 获取或创建指定 workspace 的 manager
    pub fn get_or_create(&self, workspace_id: &str) -> Arc<SnapshotManager> {
        let mut mgrs = self.managers.write();
        if let Some(mgr) = mgrs.get(workspace_id) {
            return mgr.clone();
        }
        // LRU 淘汰
        if mgrs.len() >= self.max_workspaces {
            // 简单策略：淘汰第一个找到的（生产应换成 LRU 时间戳）
            if let Some(first_key) = mgrs.keys().next().cloned() {
                mgrs.remove(&first_key);
            }
        }
        let mgr = Arc::new(SnapshotManager::new(workspace_id.to_string()));
        mgrs.insert(workspace_id.to_string(), mgr.clone());
        mgr
    }

    /// 获取已存在的 manager（不创建）
    pub fn get(&self, workspace_id: &str) -> Option<Arc<SnapshotManager>> {
        self.managers.read().get(workspace_id).cloned()
    }

    /// 列出所有 workspace_id
    pub fn list_workspaces(&self) -> Vec<WorkspaceInstanceId> {
        self.managers.read().keys().cloned().collect()
    }

    /// 移除指定 workspace（用于 workspace 注销）
    pub fn evict(&self, workspace_id: &str) -> Option<Arc<SnapshotManager>> {
        self.managers.write().remove(workspace_id)
    }

    /// 当前缓存数量
    pub fn len(&self) -> usize {
        self.managers.read().len()
    }
}

impl Default for SnapshotCache {
    fn default() -> Self {
        Self::new(32) // 默认最多 32 个 workspace 同时在线
    }
}

// ============================================
// PyO3 暴露
// ============================================

/// Python 侧 SnapshotManager 包装。
///
/// Python 用法：
///   from callwarden_core import PySnapshotManager
///   mgr = PySnapshotManager("ws_abc")
///   mgr.build_and_publish(db_path="/path/to/callwarden.db", ctx_hash="abc")
///   gen = mgr.current_generation()
///   stats = mgr.snapshot_stats()
#[pyclass(name = "PySnapshotManager")]
pub struct PySnapshotManager {
    inner: Arc<SnapshotManager>,
}

#[pymethods]
impl PySnapshotManager {
    #[new]
    fn new(workspace_instance_id: String) -> Self {
        Self {
            inner: Arc::new(SnapshotManager::new(workspace_instance_id)),
        }
    }

    /// 当前 generation 号（0 表示尚未发布）
    fn current_generation(&self) -> Generation {
        self.inner.current_generation()
    }

    /// 构建 snapshot 并原子发布。
    ///
    /// 内部：创建 GraphStore → load_from_sqlite → 包装为 GraphSnapshot → publish
    /// 返回 (generation, symbol_count, call_count)
    fn build_and_publish(
        &self,
        db_path: &str,
        build_context_hash: &str,
        snapshot_id: Option<String>,
    ) -> PyResult<(Generation, usize, usize)> {
        // 1. 创建 GraphStore 并加载 SQLite
        let mut store = GraphStore::new();
        let (symbol_count, call_count) = store.load_from_sqlite_blocking(db_path)?;

        // 2. 分配 generation
        let generation = self.inner.alloc_generation();

        // 3. 构建 GraphSnapshot
        let health = SnapshotHealth {
            symbol_count,
            call_count,
            file_count: 0,
            build_duration_ms: 0,
            last_error: None,
        };
        let snap = Arc::new(GraphSnapshot::new(
            self.inner.workspace_instance_id.clone(),
            snapshot_id,
            generation,
            build_context_hash.to_string(),
            db_path.to_string(),
            Arc::new(store),
            health,
        ));

        // 4. 原子发布
        self.inner.publish(snap);

        Ok((generation, symbol_count, call_count))
    }

    /// 当前 snapshot 健康统计（若尚未发布返回 None）
    fn snapshot_stats<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyDict>>> {
        let guard = self.inner.load();
        match guard.as_ref() {
            Some(snap) => {
                let d = PyDict::new(py);
                d.set_item("workspace_instance_id", snap.workspace_instance_id.clone())?;
                d.set_item("generation", snap.generation)?;
                d.set_item("symbol_count", snap.health.symbol_count)?;
                d.set_item("call_count", snap.health.call_count)?;
                d.set_item("build_context_hash", snap.build_context_hash.clone())?;
                if let Some(sid) = &snap.snapshot_id {
                    d.set_item("snapshot_id", sid.clone())?;
                }
                Ok(Some(d))
            }
            None => Ok(None),
        }
    }

    /// 返回当前已发布 snapshot 的共享查询视图，不复制 symbols/calls。
    fn current_store(&self) -> Option<GraphStore> {
        self.inner.current_store().map(|store| store.fork_shared())
    }
}

/// Python 侧多 workspace snapshot 缓存包装。
///
/// Python 用法：
///   from callwarden_core import PySnapshotCache
///   cache = PySnapshotCache(max_workspaces=32)
///   mgr = cache.get_or_create("ws_abc")
///   mgr.build_and_publish(db_path, ctx_hash)
#[pyclass(name = "PySnapshotCache")]
pub struct PySnapshotCache {
    inner: Arc<SnapshotCache>,
}

#[pymethods]
impl PySnapshotCache {
    #[new]
    #[pyo3(signature = (max_workspaces=32))]
    fn new(max_workspaces: usize) -> Self {
        Self {
            inner: Arc::new(SnapshotCache::new(max_workspaces)),
        }
    }

    /// 获取或创建指定 workspace 的 manager
    fn get_or_create(&self, workspace_id: &str) -> PySnapshotManager {
        let mgr = self.inner.get_or_create(workspace_id);
        PySnapshotManager { inner: mgr }
    }

    /// 获取已存在的 manager（不存在返回 None）
    fn get(&self, workspace_id: &str) -> Option<PySnapshotManager> {
        self.inner.get(workspace_id).map(|mgr| PySnapshotManager { inner: mgr })
    }

    /// 列出所有 workspace_id
    fn list_workspaces<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let list = self.inner.list_workspaces();
        Ok(PyList::new(py, list.iter().map(String::as_str))?)
    }

    /// 当前缓存数量
    fn len(&self) -> usize {
        self.inner.len()
    }

    /// 移除指定 workspace 的 snapshot
    fn evict(&self, workspace_id: &str) -> bool {
        self.inner.evict(workspace_id).is_some()
    }

    /// Phase 4.7: 对比两个 workspace 中同一 qualified_name 的符号完整差异
    ///
    /// 返回包含 change_kind / signature_change / caller_delta / callee_delta 的 dict
    fn diff_symbol<'py>(
        &self,
        py: Python<'py>,
        left_workspace_id: &str,
        right_workspace_id: &str,
        qualified_name: &str,
    ) -> PyResult<Bound<'py, PyDict>> {
        let left_store = self.get_store(left_workspace_id)?;
        let right_store = self.get_store(right_workspace_id)?;
        let record = diff::diff_symbol(&left_store, &right_store, qualified_name);
        diff::symbol_diff_to_pydict(py, &record)
    }

    /// Phase 4.7: 仅对比两个 workspace 中同一符号的签名差异
    ///
    /// 返回 file/line_range/kind 层面的变化
    fn diff_signature<'py>(
        &self,
        py: Python<'py>,
        left_workspace_id: &str,
        right_workspace_id: &str,
        qualified_name: &str,
    ) -> PyResult<Option<Bound<'py, PyDict>>> {
        let left_store = self.get_store(left_workspace_id)?;
        let right_store = self.get_store(right_workspace_id)?;
        match diff::diff_signature(&left_store, &right_store, qualified_name) {
            Some(sig) => {
                let d = diff::signature_diff_to_pydict(py, &sig)?;
                Ok(Some(d))
            }
            None => Ok(None),
        }
    }

    /// Phase 4.8: 仅对比两个 workspace 中同一符号的 caller 边集合
    ///
    /// 基于 resolved edge delta：只返回 caller 的增删（谁开始/不再调用这个函数）。
    /// 返回的 dict 中 added_callers / removed_callers 有值，
    /// added_callees / removed_callees 为空列表。
    /// 符号在任一侧不存在时返回 None。
    /// 设计参考：enterprise-daemon-shared-snapshot-plan.md §12.3 Query API
    fn diff_callers<'py>(
        &self,
        py: Python<'py>,
        left_workspace_id: &str,
        right_workspace_id: &str,
        qualified_name: &str,
    ) -> PyResult<Option<Bound<'py, PyDict>>> {
        let left_store = self.get_store(left_workspace_id)?;
        let right_store = self.get_store(right_workspace_id)?;
        match diff::diff_callers(&left_store, &right_store, qualified_name) {
            Some(delta) => {
                let d = diff::edge_delta_to_pydict(py, &delta)?;
                Ok(Some(d))
            }
            None => Ok(None),
        }
    }

    /// Phase 4.8: 仅对比两个 workspace 中同一符号的 callee 边集合
    ///
    /// 基于 resolved edge delta：只返回 callee 的增删（这个函数开始/不再调用谁）。
    /// 返回的 dict 中 added_callees / removed_callees 有值，
    /// added_callers / removed_callers 为空列表。
    /// 符号在任一侧不存在时返回 None。
    /// 设计参考：enterprise-daemon-shared-snapshot-plan.md §12.3 Query API
    fn diff_callees<'py>(
        &self,
        py: Python<'py>,
        left_workspace_id: &str,
        right_workspace_id: &str,
        qualified_name: &str,
    ) -> PyResult<Option<Bound<'py, PyDict>>> {
        let left_store = self.get_store(left_workspace_id)?;
        let right_store = self.get_store(right_workspace_id)?;
        match diff::diff_callees(&left_store, &right_store, qualified_name) {
            Some(delta) => {
                let d = diff::edge_delta_to_pydict(py, &delta)?;
                Ok(Some(d))
            }
            None => Ok(None),
        }
    }

    /// Phase 4.8: 统计两个 workspace 中匹配 scope 的符号数量（并集）
    ///
    /// 用于判断是否应走同步路径还是转后台 job。
    /// scope_type: "repo" / "file" / "module"
    /// scope_value: 文件路径或模块路径（repo 时忽略）
    fn count_symbols_in_scope(
        &self,
        left_workspace_id: &str,
        right_workspace_id: &str,
        scope_type: &str,
        scope_value: &str,
    ) -> PyResult<usize> {
        let left_store = self.get_store(left_workspace_id)?;
        let right_store = self.get_store(right_workspace_id)?;
        let scope = build_scope_filter(scope_type, scope_value);
        Ok(diff::count_symbols_in_scope(&left_store, &right_store, &scope))
    }

    /// Phase 4.8: 对比两个 workspace 中指定 scope 内的所有符号差异
    ///
    /// 同步查询：遍历 scope 内的符号并集，对每个符号调用 diff_symbol，
    /// 返回所有有变化的 SymbolDiffRecord 列表（unchanged 的不包含）。
    ///
    /// scope_type: "repo" / "file" / "module"
    /// scope_value: 文件路径或模块路径（repo 时忽略）
    ///
    /// 注意：repo 级 scope 可能很慢，调用方应先用 count_symbols_in_scope
    /// 检查大小，超阈值时改用 start_snapshot_diff 后台 job。
    ///
    /// 设计参考：enterprise-daemon-shared-snapshot-plan.md §12.3 compare_snapshots
    fn compare_snapshots<'py>(
        &self,
        py: Python<'py>,
        left_workspace_id: &str,
        right_workspace_id: &str,
        scope_type: &str,
        scope_value: &str,
    ) -> PyResult<Bound<'py, PyList>> {
        let left_store = self.get_store(left_workspace_id)?;
        let right_store = self.get_store(right_workspace_id)?;
        let scope = build_scope_filter(scope_type, scope_value);
        let records = diff::compare_snapshots(&left_store, &right_store, &scope);
        diff::symbol_diff_list_to_pylist(py, &records)
    }
}

/// 根据 Python 传入的 scope 参数构造 ScopeFilter
fn build_scope_filter(scope_type: &str, scope_value: &str) -> diff::ScopeFilter {
    match scope_type {
        "file" => diff::ScopeFilter::File(scope_value.to_string()),
        "module" => diff::ScopeFilter::Module(scope_value.to_string()),
        _ => diff::ScopeFilter::Repo,
    }
}

// 非 PyO3 的内部方法
impl PySnapshotCache {
    /// 获取指定 workspace 的当前 GraphStore（内部 Rust 接口）
    pub fn get_store(&self, workspace_id: &str) -> PyResult<Arc<GraphStore>> {
        let mgr = self.inner.get(workspace_id)
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err(format!(
                "workspace '{}' not found in cache", workspace_id
            )))?;
        mgr.current_store()
            .ok_or_else(|| pyo3::exceptions::PyRuntimeError::new_err(format!(
                "workspace '{}' has no published snapshot yet", workspace_id
            )))
    }
}
