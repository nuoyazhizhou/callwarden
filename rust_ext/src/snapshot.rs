//! Phase 4: GraphSnapshot — 只读内存快照 + ArcSwap 原子发布
//!
//! 设计要点：
//! - GraphSnapshot 包装 GraphStore 数据 + 元数据（generation/workspace_id/health）
//! - SnapshotManager 使用 ArcSwap 无锁读，发布时原子替换 Arc<GraphSnapshot>
//! - 多 workspace 通过 SnapshotCache 统一管理，按 workspace_instance_id 检索
//! - 旧 generation 被 in-flight 查询持有，查询结束后自然释放
//!
//! 参考：enterprise-daemon-shared-snapshot-plan.md §8

use crate::diff;
use crate::graph::GraphStore;
use arc_swap::{ArcSwap, Guard};
use pyo3::prelude::*;
use pyo3::types::{PyDict, PyList};
use std::collections::{HashMap, VecDeque};
use std::sync::Arc;
use std::sync::Mutex;
use std::time::Instant;

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
/// **多 generation 历史**：publish 时被替换的旧 snapshot 会被推入 `history`
/// 队列（VecDeque），保留最近若干个 generation 供 diff 查询或回滚。
/// `gc_generations(keep_last)` 删除超出 keep_last 的历史 generation。
/// `current` 始终指向最新 generation，旧 generation 被 in-flight 读者持有时
/// 不会真正释放（Arc 引用计数机制）。
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
    /// 历史 generation 队列（旧的已发布 snapshot，最新在前）
    /// publish 时 push_front 旧 snapshot；gc_generations 时 truncate
    /// `current` 不在 history 中（它是最新的）
    history: Mutex<VecDeque<Arc<GraphSnapshot>>>,
}

impl SnapshotManager {
    pub fn new(workspace_instance_id: WorkspaceInstanceId) -> Self {
        Self {
            workspace_instance_id,
            current: ArcSwap::from_pointee(None),
            next_generation: std::sync::atomic::AtomicU64::new(1),
            history: Mutex::new(VecDeque::new()),
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

    /// 原子发布新 snapshot，将被替换的旧 snapshot 推入 history 队列。
    ///
    /// 旧 snapshot 在以下条件之一会被丢弃（不入 history）：
    /// 1. 当前为首次发布（current 为 None）
    /// 2. 旧 snapshot 仍被 in-flight 读者持有（Arc::try_unwrap 失败）
    ///
    /// history 队列不会自动 truncate，需显式调用 `gc_generations(keep_last)`
    /// 清理。`current` 始终是最新 generation，不在 history 中。
    pub fn publish(&self, snapshot: Arc<GraphSnapshot>) -> Option<Arc<GraphSnapshot>> {
        let old: Arc<Option<Arc<GraphSnapshot>>> = self.current.swap(Some(snapshot).into());
        match Arc::try_unwrap(old) {
            Ok(Some(old_snap)) => {
                // 无其他读者，旧 snapshot 完整归还，推入 history
                let mut history = self.history.lock().unwrap();
                history.push_front(old_snap);
                None
            }
            Ok(None) => None, // 首次发布，无旧 snapshot
            Err(_arc) => {
                // 还有 in-flight 读者持有 Arc，无法拿回所有权
                // 旧 snapshot 仍会被 ArcSwap 替换，但 history 无法记录它
                // （这是设计取舍：为避免拷贝，牺牲了 history 完整性）
                None
            }
        }
    }

    /// 查询历史 generation 数量（不含 current）
    pub fn history_len(&self) -> usize {
        self.history.lock().unwrap().len()
    }

    /// 获取指定 generation 的 snapshot 引用（current 或 history）
    ///
    /// 先检查 current 是否匹配，再在 history 中查找。
    /// 用于 diff 跨 generation 对比。
    pub fn get_generation(&self, generation: Generation) -> Option<Arc<GraphSnapshot>> {
        // 先查 current
        let current_guard = self.current.load();
        if let Some(snap) = current_guard.as_ref() {
            if snap.generation == generation {
                return Some(snap.clone());
            }
        }
        // drop guard 避免与 history 锁交叉
        drop(current_guard);
        // 再查 history
        let history = self.history.lock().unwrap();
        history
            .iter()
            .find(|s| s.generation == generation)
            .cloned()
    }

    /// 列出所有保留的 generation 号（current + history，最新在前）
    pub fn list_generations(&self) -> Vec<Generation> {
        let mut gens = Vec::new();
        let current_guard = self.current.load();
        if let Some(snap) = current_guard.as_ref() {
            gens.push(snap.generation);
        }
        drop(current_guard);
        let history = self.history.lock().unwrap();
        for snap in history.iter() {
            gens.push(snap.generation);
        }
        gens
    }

    /// 分配下一个 generation 号
    pub fn alloc_generation(&self) -> Generation {
        self.next_generation
            .fetch_add(1, std::sync::atomic::Ordering::Relaxed)
    }

    /// 获取当前 snapshot 的 GraphStore 引用（供 diff 模块只读访问）
    pub fn current_store(&self) -> Option<Arc<GraphStore>> {
        let guard = self.current.load();
        // guard derefs to Arc<Option<Arc<GraphSnapshot>>>
        // 第一个 as_ref(): Arc::as_ref() → &Option<Arc<GraphSnapshot>>
        // 第二个 as_ref(): Option::as_ref() → Option<&Arc<GraphSnapshot>>
        guard.as_ref().as_ref().map(|snap| snap.store.clone())
    }

    // ============================================
    // R6: Rust 原生方法（不依赖 PyO3 GIL），供 daemon 模块直接调用
    // ============================================

    /// 构建 snapshot 并原子发布（Rust 原生入口，不需要 Python token）。
    ///
    /// 内部流程与 `PySnapshotManager::build_and_publish` 一致：
    /// 1. wal_checkpoint(PASSIVE) 刷 WAL 到主 DB（GraphStore 用 immutable=1 打开）
    /// 2. 创建 GraphStore + load_from_sqlite_blocking
    /// 3. 分配 generation + 计时
    /// 4. 包装为 GraphSnapshot + 原子 publish
    ///
    /// 返回 (generation, symbol_count, call_count)
    pub fn build_and_publish_blocking(
        &self,
        db_path: &str,
        build_context_hash: &str,
        snapshot_id: Option<String>,
    ) -> PyResult<(Generation, usize, usize)> {
        let start = Instant::now();

        // G7-T6: wal_checkpoint 防止 GraphStore 用 immutable=1 读到旧 WAL 数据
        // 对应 AGENTS.md 第 7 条：SQLite WAL 模式与只读连接
        let _ = wal_checkpoint_passive(db_path);

        let mut store = GraphStore::new();
        let (symbol_count, call_count) = store.load_from_sqlite_blocking(db_path)?;

        // G7-T3: 补全 SnapshotHealth 字段
        // file_count 从 GraphStore 的 file_paths_offsets 推算（最后一个为 sentinel）
        let file_count = store.file_count();
        let build_duration_ms = start.elapsed().as_millis() as u64;

        let generation = self.alloc_generation();
        let health = SnapshotHealth {
            symbol_count,
            call_count,
            file_count,
            build_duration_ms,
            last_error: None,
        };
        let snap = Arc::new(GraphSnapshot::new(
            self.workspace_instance_id.clone(),
            snapshot_id,
            generation,
            build_context_hash.to_string(),
            db_path.to_string(),
            Arc::new(store),
            health,
        ));
        self.publish(snap);
        Ok((generation, symbol_count, call_count))
    }

    /// GC 历史 generations，保留最近 `keep_last` 个（不含 current）。
    ///
    /// **实现**：history 是 VecDeque，最新在前（push_front），所以 truncate
    /// 超出 keep_last 的尾部（最旧的 generation）。被删除的 snapshot 若无
    /// 其他 Arc 引用会真正 drop；若仍有 diff 查询持有，会延迟到查询结束 drop。
    ///
    /// 返回被删除的 generation 数量。
    ///
    /// **特殊值**：
    /// - keep_last = 0：清空所有 history（仅保留 current）
    /// - keep_last >= history.len()：无操作，返回 0
    pub fn gc_generations(&self, keep_last: usize) -> usize {
        let mut history = self.history.lock().unwrap();
        let current_len = history.len();
        if keep_last >= current_len {
            return 0;
        }
        let removed = current_len - keep_last;
        history.truncate(keep_last);
        removed
    }

    /// 获取 workspace_instance_id（供 daemon 跨模块访问）
    pub fn workspace_instance_id(&self) -> &str {
        &self.workspace_instance_id
    }

    /// 当前 snapshot 的 health 信息（供 daemon health RPC 返回）
    pub fn current_health(&self) -> Option<SnapshotHealth> {
        let guard = self.current.load();
        guard.as_ref().as_ref().map(|snap| snap.health.clone())
    }

    /// 当前 snapshot 的 generation + source_db_path（供 daemon query.stats 附加元信息）
    pub fn current_meta(&self) -> Option<(Generation, String)> {
        let guard = self.current.load();
        guard.as_ref().as_ref().map(|snap| (snap.generation, snap.source_db_path.clone()))
    }
}

// ============================================
// SnapshotCache — 多 workspace 统一管理
// ============================================

/// 多 workspace 的 snapshot 缓存。
///
/// 按 workspace_instance_id 索引，每个 workspace 独立维护 generation。
/// LRU 淘汰策略：超过 max_workspaces 时淘汰最久未访问的 workspace。
///
/// 内部使用 HashMap 做 O(1) 查找 + Vec 维护插入顺序（供 list_workspaces
/// 返回稳定顺序 + LRU eviction 取最旧条目）。
pub struct SnapshotCache {
    managers: parking_lot::RwLock<HashMap<WorkspaceInstanceId, Arc<SnapshotManager>>>,
    /// 插入顺序队列：与 managers 同步（insert push_back，evict 按值移除）
    /// 用于 list_workspaces 返回稳定顺序 + LRU eviction 取队首（最旧）
    order: parking_lot::RwLock<Vec<WorkspaceInstanceId>>,
    max_workspaces: usize,
}

impl SnapshotCache {
    pub fn new(max_workspaces: usize) -> Self {
        Self {
            managers: parking_lot::RwLock::new(HashMap::new()),
            order: parking_lot::RwLock::new(Vec::new()),
            max_workspaces,
        }
    }

    /// 获取或创建指定 workspace 的 manager
    pub fn get_or_create(&self, workspace_id: &str) -> Arc<SnapshotManager> {
        let mut mgrs = self.managers.write();
        if let Some(mgr) = mgrs.get(workspace_id) {
            return mgr.clone();
        }
        // LRU 淘汰：超过 max_workspaces 时取 order 队首（最旧的 workspace）
        if mgrs.len() >= self.max_workspaces {
            let mut order = self.order.write();
            // 找到第一个仍然存在于 managers 中的 order 条目（兜底处理
            // 之前 evict 调用残留的 stale 条目）
            while let Some(first_key) = order.first().cloned() {
                if mgrs.contains_key(&first_key) {
                    mgrs.remove(&first_key);
                    order.remove(0);
                    break;
                }
                // stale 条目（之前 evict 已从 managers 移除但未从 order 清理）
                order.remove(0);
            }
        }
        let mgr = Arc::new(SnapshotManager::new(workspace_id.to_string()));
        mgrs.insert(workspace_id.to_string(), mgr.clone());
        self.order.write().push(workspace_id.to_string());
        mgr
    }

    /// 获取已存在的 manager（不创建）
    pub fn get(&self, workspace_id: &str) -> Option<Arc<SnapshotManager>> {
        self.managers.read().get(workspace_id).cloned()
    }

    /// 列出所有 workspace_id（按插入顺序，最旧在前）
    pub fn list_workspaces(&self) -> Vec<WorkspaceInstanceId> {
        let mgrs = self.managers.read();
        let order = self.order.read();
        // 过滤掉 stale 条目（理论上 order 与 managers 应同步，但兜底处理）
        order
            .iter()
            .filter(|k| mgrs.contains_key(*k))
            .cloned()
            .collect()
    }

    /// 移除指定 workspace（用于 workspace 注销）
    pub fn evict(&self, workspace_id: &str) -> Option<Arc<SnapshotManager>> {
        let removed = self.managers.write().remove(workspace_id);
        if removed.is_some() {
            // 从 order 队列中移除（保留其余条目顺序）
            self.order.write().retain(|k| k != workspace_id);
        }
        removed
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
    /// 内部：wal_checkpoint → 创建 GraphStore → load_from_sqlite → 包装为 GraphSnapshot → publish
    /// 返回 (generation, symbol_count, call_count)
    ///
    /// G7-T2/T6：直接委托给 `SnapshotManager::build_and_publish_blocking`，
    /// 保证 Python 和 Rust daemon 路径走同一份逻辑（含 wal_checkpoint + 计时 + file_count）。
    fn build_and_publish(
        &self,
        db_path: &str,
        build_context_hash: &str,
        snapshot_id: Option<String>,
    ) -> PyResult<(Generation, usize, usize)> {
        self.inner.build_and_publish_blocking(db_path, build_context_hash, snapshot_id)
    }

    /// GC 历史 generations，保留最近 `keep_last` 个（不含 current）。
    ///
    /// 返回被删除的 generation 数量。
    /// - keep_last = 0：清空所有 history
    /// - keep_last >= history.len()：无操作
    ///
    /// G7-T1/T2：对应 Python `SnapshotManagerService.gc_snapshots(keep_last=3)`
    /// 通过 getattr 反射调用此方法。
    fn gc_generations(&self, keep_last: usize) -> usize {
        self.inner.gc_generations(keep_last)
    }

    /// 查询历史 generation 数量（不含 current）。
    /// 用于监控/调试，确认 GC 是否生效。
    fn history_len(&self) -> usize {
        self.inner.history_len()
    }

    /// 列出所有保留的 generation 号（current + history，最新在前）。
    /// 用于 diff 跨 generation 对比时定位目标 generation。
    fn list_generations(&self) -> Vec<Generation> {
        self.inner.list_generations()
    }

    /// 当前 snapshot 健康统计（若尚未发布返回 None）
    ///
    /// G7-T3：补全 file_count 和 build_duration_ms 字段
    fn snapshot_stats<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyDict>>> {
        let guard = self.inner.load();
        match guard.as_ref() {
            Some(snap) => {
                let d = PyDict::new(py);
                d.set_item("workspace_instance_id", snap.workspace_instance_id.clone())?;
                d.set_item("generation", snap.generation)?;
                d.set_item("symbol_count", snap.health.symbol_count)?;
                d.set_item("call_count", snap.health.call_count)?;
                d.set_item("file_count", snap.health.file_count)?;
                d.set_item("build_duration_ms", snap.health.build_duration_ms)?;
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
        self.inner
            .get(workspace_id)
            .map(|mgr| PySnapshotManager { inner: mgr })
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
        Ok(diff::count_symbols_in_scope(
            &left_store,
            &right_store,
            &scope,
        ))
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
        let mgr = self.inner.get(workspace_id).ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "workspace '{}' not found in cache",
                workspace_id
            ))
        })?;
        mgr.current_store().ok_or_else(|| {
            pyo3::exceptions::PyRuntimeError::new_err(format!(
                "workspace '{}' has no published snapshot yet",
                workspace_id
            ))
        })
    }
}

// ============================================
// 辅助函数
// ============================================

/// 对 SQLite 数据库执行 PRAGMA wal_checkpoint(PASSIVE)。
///
/// 用于 GraphStore 用 immutable=1 URI 打开 SQLite 前调用，
/// 防止读到旧 WAL 数据（AGENTS.md 第 7 条）。
///
/// - 失败不抛错（只是 warning 级别），因为：
///   1. db_path 可能不存在（首次创建场景）
///   2. 数据库可能未启用 WAL（checkpoint 是 no-op）
///   3. 调用方（build_and_publish_blocking）会继续尝试 load_from_sqlite_blocking
///
/// 内部使用独立连接，不影响 GraphStore 后续 immutable=1 打开。
fn wal_checkpoint_passive(db_path: &str) -> std::result::Result<(), String> {
    use rusqlite::Connection;
    let conn = match Connection::open(db_path) {
        Ok(c) => c,
        Err(e) => return Err(format!("open {}: {}", db_path, e)),
    };
    // busy_timeout 防止与其他写连接撞锁
    if let Err(e) = conn.execute_batch("PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(PASSIVE);") {
        return Err(format!("wal_checkpoint {}: {}", db_path, e));
    }
    Ok(())
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    fn make_snapshot(generation: Generation, ws_id: &str) -> Arc<GraphSnapshot> {
        let store = Arc::new(GraphStore::new());
        Arc::new(GraphSnapshot::new(
            ws_id.to_string(),
            None,
            generation,
            "ctx_hash".to_string(),
            "/tmp/test.db".to_string(),
            store,
            SnapshotHealth {
                symbol_count: 0,
                call_count: 0,
                file_count: 0,
                build_duration_ms: 0,
                last_error: None,
            },
        ))
    }

    #[test]
    fn test_gc_generations_empty_history() {
        // 全新 manager，未 publish，history 为空
        let mgr = SnapshotManager::new("ws_test".to_string());
        assert_eq!(mgr.history_len(), 0);
        assert_eq!(mgr.gc_generations(3), 0);
        assert_eq!(mgr.history_len(), 0);
    }

    #[test]
    fn test_publish_pushes_old_to_history() {
        let mgr = SnapshotManager::new("ws_test".to_string());
        let snap1 = make_snapshot(1, "ws_test");
        let snap2 = make_snapshot(2, "ws_test");
        let snap3 = make_snapshot(3, "ws_test");

        // 首次 publish：current 为 None，不入 history
        mgr.publish(snap1);
        assert_eq!(mgr.history_len(), 0);
        assert_eq!(mgr.current_generation(), 1);

        // 第二次 publish：旧 snap1 入 history
        mgr.publish(snap2);
        assert_eq!(mgr.history_len(), 1);
        assert_eq!(mgr.current_generation(), 2);

        // 第三次 publish：旧 snap2 入 history
        mgr.publish(snap3);
        assert_eq!(mgr.history_len(), 2);
        assert_eq!(mgr.current_generation(), 3);
    }

    #[test]
    fn test_gc_generations_truncate_old() {
        let mgr = SnapshotManager::new("ws_test".to_string());
        // publish 5 次，history 累积 4 个
        for gen in 1..=5 {
            mgr.publish(make_snapshot(gen, "ws_test"));
        }
        assert_eq!(mgr.current_generation(), 5);
        assert_eq!(mgr.history_len(), 4);

        // keep_last=2：应删除 2 个最旧的（gen=1,2）
        let removed = mgr.gc_generations(2);
        assert_eq!(removed, 2);
        assert_eq!(mgr.history_len(), 2);

        // 剩余的 generation 应为 4, 3（最新在前）
        let gens = mgr.list_generations();
        assert_eq!(gens, vec![5, 4, 3]);
    }

    #[test]
    fn test_gc_generations_keep_all() {
        let mgr = SnapshotManager::new("ws_test".to_string());
        for gen in 1..=3 {
            mgr.publish(make_snapshot(gen, "ws_test"));
        }
        // history 有 2 个，keep_last=5 大于 history.len()
        let removed = mgr.gc_generations(5);
        assert_eq!(removed, 0);
        assert_eq!(mgr.history_len(), 2);
    }

    #[test]
    fn test_gc_generations_clear_all() {
        let mgr = SnapshotManager::new("ws_test".to_string());
        for gen in 1..=3 {
            mgr.publish(make_snapshot(gen, "ws_test"));
        }
        // keep_last=0：清空所有 history
        let removed = mgr.gc_generations(0);
        assert_eq!(removed, 2);
        assert_eq!(mgr.history_len(), 0);
        // current 仍在
        assert_eq!(mgr.current_generation(), 3);
    }

    #[test]
    fn test_get_generation_from_current() {
        let mgr = SnapshotManager::new("ws_test".to_string());
        mgr.publish(make_snapshot(5, "ws_test"));
        let snap = mgr.get_generation(5).expect("current 必须找到");
        assert_eq!(snap.generation, 5);
    }

    #[test]
    fn test_get_generation_from_history() {
        let mgr = SnapshotManager::new("ws_test".to_string());
        mgr.publish(make_snapshot(1, "ws_test"));
        mgr.publish(make_snapshot(2, "ws_test"));
        // gen=1 应在 history 中
        let snap = mgr.get_generation(1).expect("history 中应找到 gen=1");
        assert_eq!(snap.generation, 1);
        // gen=2 是 current
        let snap = mgr.get_generation(2).expect("current 应找到 gen=2");
        assert_eq!(snap.generation, 2);
        // gen=99 不存在
        assert!(mgr.get_generation(99).is_none());
    }

    #[test]
    fn test_list_generations_order() {
        let mgr = SnapshotManager::new("ws_test".to_string());
        for gen in 1..=4 {
            mgr.publish(make_snapshot(gen, "ws_test"));
        }
        // 期望：current (4) + history (3, 2, 1)
        let gens = mgr.list_generations();
        assert_eq!(gens, vec![4, 3, 2, 1]);
    }

    #[test]
    fn test_wal_checkpoint_passive_nonexistent_db() {
        // 不存在的路径不应 panic（仅返回 Err）
        let result = wal_checkpoint_passive("/nonexistent/path/test.db");
        assert!(result.is_err());
    }

    #[test]
    fn test_wal_checkpoint_passive_valid_db() {
        // 创建临时 SQLite DB 并测试 checkpoint
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("test.db");
        // 先创建一个有效的 SQLite DB
        {
            use rusqlite::Connection;
            let conn = Connection::open(&db_path).unwrap();
            conn.execute_batch("CREATE TABLE t (id INTEGER); INSERT INTO t VALUES (1);")
                .unwrap();
        }
        // wal_checkpoint 应成功（即便未启用 WAL 也是 no-op）
        let result = wal_checkpoint_passive(db_path.to_str().unwrap());
        assert!(result.is_ok(), "checkpoint 应成功: {:?}", result);
    }
}
