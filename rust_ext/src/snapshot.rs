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
use std::fs::File;
use std::sync::Arc;
use std::sync::Mutex;
use std::time::{Duration, Instant};

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
    /// 保留源 DB 的只读句柄。Linux FD 发布时，原 SCM_RIGHTS FD 在请求结束后
    /// 会关闭；此副本保证后续完整详情查询仍能通过 `/proc/self/fd/N` 访问同一快照。
    source_db_file: Option<File>,
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
        let source_db_file = File::open(&source_db_path).ok();
        Self {
            workspace_instance_id,
            snapshot_id,
            generation,
            build_context_hash,
            source_db_path,
            source_db_file,
            store,
            health,
        }
    }

    /// 返回可用于只读 SQLite 查询的稳定路径。
    pub fn query_db_path(&self) -> String {
        #[cfg(target_os = "linux")]
        {
            use std::os::fd::AsRawFd;
            if let Some(file) = &self.source_db_file {
                return format!("/proc/self/fd/{}", file.as_raw_fd());
            }
        }
        self.source_db_path.clone()
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
        history.iter().find(|s| s.generation == generation).cloned()
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
    /// P0-2 整改（2026-07-22 复审整改-v2）：`workspace_id` 必传，确保 GraphStore
    /// 在 SQL 层过滤本 workspace 的 file_instances/symbols/calls，避免用户级
    /// 单库多 workspace 场景下 snapshot 混入其他 workspace 数据。
    ///
    /// 返回 (generation, symbol_count, call_count)
    ///
    /// T-1786574299601：错误以 String 传播（daemon 无解释器时 PyErr 无法格式化），
    /// pyo3 边界（build_and_publish）再转回 PyErr。
    pub fn build_and_publish_blocking(
        &self,
        db_path: &str,
        workspace_id: i64,
        build_context_hash: &str,
        snapshot_id: Option<String>,
    ) -> Result<(Generation, usize, usize), String> {
        let start = Instant::now();

        // G7-T6: wal_checkpoint 防止 GraphStore 用 immutable=1 读到旧 WAL 数据
        // 对应 AGENTS.md 第 7 条：SQLite WAL 模式与只读连接
        let _ = wal_checkpoint_passive(db_path);

        let mut store = GraphStore::new();
        let (symbol_count, call_count) = store.load_from_sqlite_blocking(db_path, workspace_id)?;

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
        guard
            .as_ref()
            .as_ref()
            .map(|snap| (snap.generation, snap.source_db_path.clone()))
    }

    /// 当前 snapshot 的稳定只读 DB 路径。
    pub fn current_query_db_path(&self) -> Option<String> {
        let guard = self.current.load();
        guard.as_ref().as_ref().map(|snap| snap.query_db_path())
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
/// 内部使用 HashMap 做 O(1) 查找 + Vec 维护**访问顺序**（供 list_workspaces
/// 返回稳定顺序 + LRU eviction 取最旧条目）。
///
/// T3（REQ-RG-02）修正：此前命中路径不更新顺序，注释称 LRU 实为 FIFO；
/// 现命中路径调用 [`Self::touch`] 更新 last_access 并把条目移到 order 尾部，
/// 驱逐按**真实访问序**（AC-RG-02-3）。
///
/// 锁序约定（防交叉死锁，所有路径必须一致）：
/// `managers` → `order` → `last_access`。
pub struct SnapshotCache {
    managers: parking_lot::RwLock<HashMap<WorkspaceInstanceId, Arc<SnapshotManager>>>,
    /// 访问顺序队列：最旧在前（touch 把条目移到尾部）。
    /// 用于 list_workspaces 返回稳定顺序 + LRU eviction 取队首（最旧）
    order: parking_lot::RwLock<Vec<WorkspaceInstanceId>>,
    /// 各 workspace 最近访问时刻（T3：touch/get_or_create 维护，
    /// evict_idle 依此驱逐；与 managers 同步增删）
    last_access: parking_lot::RwLock<HashMap<WorkspaceInstanceId, Instant>>,
    max_workspaces: usize,
}

impl SnapshotCache {
    pub fn new(max_workspaces: usize) -> Self {
        Self {
            managers: parking_lot::RwLock::new(HashMap::new()),
            order: parking_lot::RwLock::new(Vec::new()),
            last_access: parking_lot::RwLock::new(HashMap::new()),
            max_workspaces,
        }
    }

    /// 获取或创建指定 workspace 的 manager
    pub fn get_or_create(&self, workspace_id: &str) -> Arc<SnapshotManager> {
        let mut mgrs = self.managers.write();
        if let Some(mgr) = mgrs.get(workspace_id) {
            let mgr = mgr.clone();
            drop(mgrs);
            // 命中路径：touch（FIFO→真 LRU，AC-RG-02-3）。
            // 锁序 managers(已释放) → order → last_access，与创建路径一致。
            self.touch(workspace_id);
            return mgr;
        }
        // LRU 淘汰：超过 max_workspaces 时取 order 队首（最久未访问的 workspace）
        if mgrs.len() >= self.max_workspaces {
            let mut order = self.order.write();
            // 找到第一个仍然存在于 managers 中的 order 条目（兜底处理
            // 之前 evict 调用残留的 stale 条目）
            while let Some(first_key) = order.first().cloned() {
                if mgrs.contains_key(&first_key) {
                    mgrs.remove(&first_key);
                    self.last_access.write().remove(&first_key);
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
        // 插入即访问：新条目 last_access=now（否则会被 evict_idle 立即驱逐）
        self.last_access
            .write()
            .insert(workspace_id.to_string(), Instant::now());
        mgr
    }

    /// 更新指定 workspace 的最近访问时刻（T3，C2）。
    ///
    /// 不存在则无操作（不创建）；存在则 last_access=now 并把条目移到
    /// order 尾部（访问序）。锁序：order → last_access。
    pub fn touch(&self, workspace_id: &str) {
        let mut order = self.order.write();
        let mut last = self.last_access.write();
        if !last.contains_key(workspace_id) {
            return; // 不存在则无操作（不创建）
        }
        last.insert(workspace_id.to_string(), Instant::now());
        if let Some(pos) = order.iter().position(|k| k == workspace_id) {
            order.remove(pos);
            order.push(workspace_id.to_string());
        }
    }

    /// 获取已存在的 manager（不创建）
    pub fn get(&self, workspace_id: &str) -> Option<Arc<SnapshotManager>> {
        self.managers.read().get(workspace_id).cloned()
    }

    /// 列出所有 workspace_id（按访问顺序，最旧在前）
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
            // 同步清理 last_access（防泄漏 + touch 语义一致）
            self.last_access.write().remove(workspace_id);
        }
        removed
    }

    /// 当前缓存数量
    pub fn len(&self) -> usize {
        self.managers.read().len()
    }

    /// 驱逐空闲超过 `idle` 的 workspace（T3，C2；AC-RG-02-1）。
    ///
    /// 判定：`now - last_access > idle` 的条目逐出 cache；返回被驱逐列表
    /// （供日志/指标）。`Duration::ZERO` = 全量清冷（C6 内存压力路径用）。
    ///
    /// Arc 语义（AC-RG-02-5）：驱逐只把 manager 从 cache 移除；in-flight
    /// 查询持有的 `load()` Guard 引用计数仍存活，数据可读到查询结束。
    pub fn evict_idle(&self, idle: Duration) -> Vec<String> {
        let now = Instant::now();
        // 1) last_access 读锁内判定候选（与 managers 交集，防 stale）
        let candidates: Vec<WorkspaceInstanceId> = {
            let last = self.last_access.read();
            let mgrs = self.managers.read();
            last.iter()
                .filter(|(k, v)| mgrs.contains_key(*k) && now.duration_since(**v) > idle)
                .map(|(k, _)| k.clone())
                .collect()
        };
        if candidates.is_empty() {
            return Vec::new();
        }
        // 2) 写锁内统一移除（锁序 managers → order → last_access）
        let mut evicted: Vec<String> = Vec::with_capacity(candidates.len());
        let mut mgrs = self.managers.write();
        let mut order = self.order.write();
        let mut last = self.last_access.write();
        for k in candidates {
            if mgrs.remove(&k).is_some() {
                order.retain(|x| x != &k);
                last.remove(&k);
                evicted.push(k);
            }
        }
        if !evicted.is_empty() {
            eprintln!(
                "[snapshot_cache] evict_idle: evicted {} idle workspace(s): {}",
                evicted.len(),
                evicted.join(",")
            );
        }
        evicted
    }

    /// 当前驻留 snapshot 的近似字节总和（T3，C2；AC-RG-02-4 前置）。
    ///
    /// 各 ws 当前已发布 snapshot 的 `GraphStore::approximate_bytes()` 求和；
    /// 未发布的 ws 计 0（跳过）。历史 generation 不计（只算 current）。
    pub fn approx_bytes(&self) -> u64 {
        let mgrs = self.managers.read();
        mgrs
            .values()
            .filter_map(|mgr| mgr.current_store())
            .map(|store| store.approximate_bytes() as u64)
            .sum()
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
    /// P0-2 整改（2026-07-22 复审整改-v2）：新增 `workspace_id` 参数（末尾，
    /// 默认 0），用于 GraphStore SQL 层过滤本 workspace 数据。`workspace_id=0`
    /// 表示不过滤（兼容旧测试和单 workspace DB）。生产路径应传 >0 的 workspace_id。
    ///
    /// G7-T2/T6：保证 Python 和 Rust daemon 路径走同一份逻辑（含 wal_checkpoint + 计时 + file_count）。
    #[pyo3(signature = (db_path, build_context_hash="", snapshot_id=None, workspace_id=0))]
    fn build_and_publish(
        &self,
        db_path: &str,
        build_context_hash: &str,
        snapshot_id: Option<String>,
        workspace_id: i64,
    ) -> PyResult<(Generation, usize, usize)> {
        // T-1786574299601：内部以 String 传播错误，pyo3 边界转回 PyErr 保持 Python API 不变。
        self.inner
            .build_and_publish_blocking(db_path, workspace_id, build_context_hash, snapshot_id)
            .map_err(pyo3::exceptions::PyRuntimeError::new_err)
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

    // ============================================================
    // Phase 1-4: 便捷查询方法（封装 GraphStore 的 4 个查询方法）
    //
    // 与 Python `SnapshotManagerService.query_*` 行为一致：
    // - 调用前需通过 build_and_publish 加载 snapshot 到内存
    // - snapshot 未加载时返回空列表 / 空 dict，不抛错
    // - 所有查询走内存 ArcSwap 保护的 GraphStore，无 SQLite 锁
    // ============================================================

    /// 向下调用链查询（BFS）。
    ///
    /// 与 Python `SnapshotManagerService.query_call_chain_down(workspace_instance_id, root)`
    /// 行为一致：
    /// - root 符号不存在 → 返回 []
    /// - root 存在 → 返回 [{depth, caller_name, callee_name, callee_id, call_line, is_cross_file}, ...]
    /// - snapshot 未加载 → 返回 []
    /// - max_depth 限制递归深度（默认 10，对齐 Python `QueryBudget.max_depth`）
    #[pyo3(signature = (root, max_depth=10))]
    fn query_call_chain_down<'py>(
        &self,
        py: Python<'py>,
        root: &str,
        max_depth: usize,
    ) -> PyResult<Bound<'py, PyList>> {
        let store = match self.inner.current_store() {
            Some(s) => s,
            None => return Ok(PyList::empty(py)),
        };
        // 调用 native 实现，避免 GIL 边界
        let edges = store.call_chain_down_rust(root, max_depth);
        let list = PyList::empty(py);
        for edge in edges {
            let dict = PyDict::new(py);
            dict.set_item("depth", edge.depth)?;
            dict.set_item("caller_name", edge.caller_name.clone())?;
            dict.set_item("callee_name", edge.callee_name.clone())?;
            dict.set_item("callee_id", edge.callee_id)?;
            dict.set_item("call_line", edge.call_line)?;
            dict.set_item("is_cross_file", edge.is_cross_file)?;
            list.append(dict)?;
        }
        Ok(list)
    }

    /// 拓扑排序（Kahn 算法，按调用深度升序返回 qualified_name 列表）。
    ///
    /// 与 Python `SnapshotManagerService.query_topological_order(workspace_instance_id)`
    /// 行为一致：
    /// - 空 GraphStore → 返回 []
    /// - DAG 无循环 → 返回拓扑序列表
    /// - 含循环 → 返回部分排序（不抛错）
    /// - snapshot 未加载 → 返回 []
    fn query_topological_order(&self) -> Vec<String> {
        match self.inner.current_store() {
            Some(store) => store.topological_order_rust(),
            None => Vec::new(),
        }
    }

    /// 循环检测（DFS 三色标记）。
    ///
    /// 与 Python `SnapshotManagerService.query_detect_cycles(workspace_instance_id)`
    /// 行为一致：
    /// - 无循环 → 返回 []
    /// - 有循环 → 返回 [[node1, node2, ...], ...] 每个内层列表是一个循环
    /// - snapshot 未加载 → 返回 []
    fn query_detect_cycles(&self) -> Vec<Vec<String>> {
        match self.inner.current_store() {
            Some(store) => store.detect_cycles_rust(),
            None => Vec::new(),
        }
    }

    /// 统计信息（符号数 / 边数 / 已解析边数 / 索引大小）。
    ///
    /// 与 Python `SnapshotManagerService.query_stats(workspace_instance_id)` 行为一致：
    /// - 返回 dict，含 symbol_count / edge_count / resolved_edge_count 等字段
    /// - snapshot 未加载 → 返回 None
    fn query_stats<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyDict>>> {
        let store = match self.inner.current_store() {
            Some(s) => s,
            None => return Ok(None),
        };
        // 复用 GraphStore::stats() PyO3 方法的字段集（与 graph.rs:1052 一致）
        // 通过 pub(crate) 访问器 symbols_table() / call_graph() 读取内部状态
        let dict = PyDict::new(py);
        if let Some(symbols) = store.symbols_table() {
            dict.set_item("symbol_count", symbols.by_id.len())?;
            dict.set_item("qname_index_size", symbols.by_qname_sorted_ids.len())?;
            dict.set_item(
                "simple_name_index_size",
                symbols.by_simple_name_sorted_ids.len(),
            )?;
            dict.set_item(
                "file_index_size",
                symbols.file_paths_offsets.len().saturating_sub(1),
            )?;
            dict.set_item("name_pool_size", symbols.name_pool.len())?;
            dict.set_item("qname_pool_size", symbols.qname_pool.len())?;
            dict.set_item("module_pool_size", symbols.module_pool.len())?;
            dict.set_item("search_pool_size", symbols.search_pool_lower.len())?;
            dict.set_item("search_entry_count", symbols.search_entry_offsets.len())?;
        } else {
            dict.set_item("symbol_count", 0)?;
            dict.set_item("qname_index_size", 0)?;
            dict.set_item("simple_name_index_size", 0)?;
            dict.set_item("file_index_size", 0)?;
            dict.set_item("name_pool_size", 0)?;
            dict.set_item("qname_pool_size", 0)?;
            dict.set_item("module_pool_size", 0)?;
            dict.set_item("search_pool_size", 0)?;
            dict.set_item("search_entry_count", 0)?;
        }
        if let Some(calls) = store.call_graph() {
            let resolved = calls
                .forward_edges
                .iter()
                .filter(|e| e.callee_id != 0)
                .count();
            dict.set_item("edge_count", calls.forward_edges.len())?;
            dict.set_item("resolved_edge_count", resolved)?;
            dict.set_item("forward_offsets_size", calls.forward_offsets.len())?;
            dict.set_item("backward_offsets_size", calls.backward_offsets.len())?;
            dict.set_item("callee_name_pool_size", calls.callee_names_pool.len())?;
            dict.set_item(
                "callee_name_count",
                calls.callee_names_offsets.len().saturating_sub(1),
            )?;
            dict.set_item(
                "callee_name_to_idx_size",
                calls.callee_name_sorted_idxs.len(),
            )?;
            dict.set_item("root_count", calls.roots.len())?;
        } else {
            dict.set_item("edge_count", 0)?;
        }
        Ok(Some(dict))
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
    if let Err(e) = conn.execute_batch("PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(PASSIVE);")
    {
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
    fn current_query_db_path_tracks_published_snapshot() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("snapshot.db");
        std::fs::write(&db_path, b"sqlite fixture").unwrap();
        let mgr = SnapshotManager::new("ws_test".to_string());
        let snapshot = Arc::new(GraphSnapshot::new(
            "ws_test".to_string(),
            None,
            1,
            "ctx_hash".to_string(),
            db_path.to_string_lossy().into_owned(),
            Arc::new(GraphStore::new()),
            SnapshotHealth {
                symbol_count: 0,
                call_count: 0,
                file_count: 0,
                build_duration_ms: 0,
                last_error: None,
            },
        ));
        mgr.publish(snapshot);

        let query_path = mgr.current_query_db_path().unwrap();
        assert!(std::fs::metadata(query_path).is_ok());
    }

    #[cfg(target_os = "linux")]
    #[test]
    fn retained_fd_survives_original_fd_close() {
        use std::os::fd::AsRawFd;

        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("snapshot.db");
        let conn = rusqlite::Connection::open(&db_path).unwrap();
        conn.execute_batch("CREATE TABLE marker(value TEXT); INSERT INTO marker VALUES ('ok');")
            .unwrap();
        drop(conn);

        let original = File::open(&db_path).unwrap();
        let proc_path = format!("/proc/self/fd/{}", original.as_raw_fd());
        let snapshot = GraphSnapshot::new(
            "ws_test".to_string(),
            None,
            1,
            "ctx_hash".to_string(),
            proc_path,
            Arc::new(GraphStore::new()),
            SnapshotHealth {
                symbol_count: 0,
                call_count: 0,
                file_count: 0,
                build_duration_ms: 0,
                last_error: None,
            },
        );
        drop(original);

        let retained_path = snapshot.query_db_path();
        let retained = rusqlite::Connection::open_with_flags(
            retained_path,
            rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY,
        )
        .unwrap();
        let value: String = retained
            .query_row("SELECT value FROM marker", [], |row| row.get(0))
            .unwrap();
        assert_eq!(value, "ok");
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

    // ---------- T3: SnapshotCache 真 LRU（C2 / AC-RG-02-3）----------

    /// touch 更新访问序：list_workspaces 由插入序变为访问序（最旧在前）。
    #[test]
    fn touch_updates_access_order() {
        let cache = SnapshotCache::new(8);
        cache.get_or_create("A");
        cache.get_or_create("B");
        cache.get_or_create("C");
        // 插入序 = [A, B, C]
        assert_eq!(cache.list_workspaces(), vec!["A", "B", "C"]);
        // touch A → 访问序 [B, C, A]
        cache.touch("A");
        assert_eq!(cache.list_workspaces(), vec!["B", "C", "A"]);
        // len 不变（touch 不创建不删除）
        assert_eq!(cache.len(), 3);
    }

    /// AC-RG-02-3：驱逐按**访问序**（修正 FIFO）——
    /// A 插入后持续 touch，B/C 插入后不访问 → B（最久未访问）先被驱逐。
    #[test]
    fn ac_rg_02_3_eviction_by_access_order_not_insertion() {
        let cache = SnapshotCache::new(3);
        cache.get_or_create("A");
        cache.get_or_create("B");
        cache.get_or_create("C");
        // A 持续访问（B/C 插入后不访问）——FIFO 下 A 最旧会被驱逐，LRU 下 B 应被驱逐
        cache.touch("A");
        cache.touch("A");
        assert_eq!(cache.list_workspaces(), vec!["B", "C", "A"]);
        // 插入第 4 个 → 驱逐最久未访问的 B（FIFO 会错杀 A）
        cache.get_or_create("D");
        assert_eq!(cache.len(), 3);
        let names = cache.list_workspaces();
        assert!(!names.contains(&"B".to_string()), "B 应被驱逐（最久未访问）: {:?}", names);
        assert!(names.contains(&"A".to_string()), "A 持续访问不应被驱逐: {:?}", names);
        assert!(names.contains(&"C".to_string()) && names.contains(&"D".to_string()));
    }

    /// touch 不存在的 workspace 无操作（不创建、不 panic）。
    #[test]
    fn touch_unknown_workspace_is_noop() {
        let cache = SnapshotCache::new(8);
        cache.get_or_create("A");
        cache.touch("ghost");
        assert_eq!(cache.len(), 1, "touch 不得创建条目");
        assert_eq!(cache.list_workspaces(), vec!["A"]);
        // 空缓存上 touch 也不 panic
        let empty = SnapshotCache::new(8);
        empty.touch("nobody");
        assert_eq!(empty.len(), 0);
    }

    /// get_or_create 命中路径自身也维护访问序（不显式 touch 的调用方也拿到 LRU）。
    #[test]
    fn get_or_create_hit_path_updates_access_order() {
        let cache = SnapshotCache::new(3);
        cache.get_or_create("A");
        cache.get_or_create("B");
        cache.get_or_create("C");
        // 命中 A（内部 touch）→ [B, C, A]
        let _ = cache.get_or_create("A");
        assert_eq!(cache.list_workspaces(), vec!["B", "C", "A"]);
        // 插入 D → 驱逐 B
        cache.get_or_create("D");
        let names = cache.list_workspaces();
        assert!(!names.contains(&"B".to_string()), "命中路径未更新访问序: {:?}", names);
    }

    /// evict 同步清理 last_access（touch 后 evict，再 touch 原 ws 无操作）。
    #[test]
    fn evict_cleans_last_access() {
        let cache = SnapshotCache::new(8);
        cache.get_or_create("A");
        cache.touch("A");
        assert!(cache.evict("A").is_some());
        cache.touch("A"); // last_access 已清理 → 无操作
        assert_eq!(cache.len(), 0);
        // 重新 get_or_create 后 touch 恢复正常
        cache.get_or_create("A");
        cache.touch("A");
        assert_eq!(cache.list_workspaces(), vec!["A"]);
    }

    // ---------- T3-S3: evict_idle + approx_bytes + Arc 语义 ----------

    use crate::graph::{CallEdge, CallGraph, GraphSymbol, SymbolKind, SymbolTable};

    /// 构造带已知数据的 snapshot（approx_bytes > 0）。
    fn make_loaded_snapshot(generation: Generation, ws_id: &str) -> Arc<GraphSnapshot> {
        let n = 100usize;
        let name = "sym_000000";
        let mut by_id = Vec::with_capacity(n);
        let mut name_pool = String::with_capacity(name.len() * n);
        let mut qname_pool = String::with_capacity(name.len() * n);
        let mut module_pool = String::with_capacity(3 * n);
        let mut by_qname_sorted_ids = Vec::with_capacity(n);
        let mut by_simple_name_sorted_ids = Vec::with_capacity(n);
        let mut search_pool_lower = String::with_capacity((name.len() + 1) * n);
        let mut search_entry_offsets = Vec::with_capacity(n);
        let mut search_entry_sym_ids = Vec::with_capacity(n);
        for i in 0..n {
            let off = name_pool.len() as u32;
            name_pool.push_str(name);
            let qoff = qname_pool.len() as u32;
            qname_pool.push_str(name);
            let moff = module_pool.len() as u32;
            module_pool.push_str("mod");
            by_id.push(GraphSymbol {
                id: i as u32,
                file_instance_id: 0,
                kind: SymbolKind::Fn,
                name_offset: off,
                name_len: name.len() as u32,
                qname_offset: qoff,
                qname_len: name.len() as u32,
                module_offset: moff,
                module_len: 3,
                start_line: 1,
                end_line: 2,
                depth: 0,
            });
            by_qname_sorted_ids.push(i as u32);
            by_simple_name_sorted_ids.push(i as u32);
            search_entry_offsets.push(search_pool_lower.len() as u32);
            search_entry_sym_ids.push(i as u32);
            search_pool_lower.push_str(name);
            search_pool_lower.push('\0');
        }
        let symbols = SymbolTable {
            by_id,
            by_qname_sorted_ids,
            by_simple_name_sorted_ids,
            file_paths_pool: "src/lib.rs".to_string(),
            file_paths_offsets: vec![0, 10],
            name_pool,
            qname_pool,
            module_pool,
            search_pool_lower,
            search_entry_offsets,
            search_entry_sym_ids,
        };
        let calls = CallGraph {
            forward_edges: (0..50)
                .map(|i| CallEdge {
                    caller_id: i as u32,
                    callee_id: (i + 1) as u32,
                    call_line_packed: CallEdge::pack_call_line(10, false),
                    callee_name_idx: 0,
                })
                .collect(),
            forward_offsets: vec![0, 50],
            backward_positions: (0..50).map(|i| i as u32).collect(),
            backward_offsets: vec![0, 50],
            callee_position_offsets: vec![0, 1],
            callee_positions: vec![0],
            callee_names_pool: "sym_000000".to_string(),
            callee_names_offsets: vec![0, 10],
            callee_name_sorted_idxs: vec![0],
            roots: vec![0],
        };
        let store = GraphStore::with_tables(symbols, calls);
        Arc::new(GraphSnapshot::new(
            ws_id.to_string(),
            None,
            generation,
            "ctx_hash".to_string(),
            "/tmp/test.db".to_string(),
            Arc::new(store),
            SnapshotHealth {
                symbol_count: 0,
                call_count: 0,
                file_count: 0,
                build_duration_ms: 0,
                last_error: None,
            },
        ))
    }

    /// AC-RG-02-1：空闲超过短超时后被驱逐，cache.len() 下降，返回列表含该 ws。
    #[test]
    fn ac_rg_02_1_evict_idle_short_timeout() {
        let cache = SnapshotCache::new(8);
        cache.get_or_create("A");
        std::thread::sleep(Duration::from_millis(30));
        let evicted = cache.evict_idle(Duration::from_millis(10));
        assert_eq!(evicted, vec!["A".to_string()], "空闲 30ms > 10ms 应被驱逐");
        assert_eq!(cache.len(), 0, "驱逐后 cache.len() 下降");
    }

    /// evict_idle 保留最近访问的条目（只逐空闲超限的）。
    #[test]
    fn evict_idle_preserves_recently_touched() {
        let cache = SnapshotCache::new(8);
        cache.get_or_create("A");
        std::thread::sleep(Duration::from_millis(30));
        cache.get_or_create("B"); // B 插入即访问（新）
        cache.touch("B");
        let evicted = cache.evict_idle(Duration::from_millis(10));
        assert_eq!(evicted, vec!["A".to_string()], "只驱逐空闲超限的 A");
        assert_eq!(cache.len(), 1);
        assert_eq!(cache.list_workspaces(), vec!["B"]);
    }

    /// Duration::ZERO 全量清冷（C6 内存压力路径语义）。
    #[test]
    fn evict_idle_zero_clears_all() {
        let cache = SnapshotCache::new(8);
        for k in ["A", "B", "C"] {
            cache.get_or_create(k);
        }
        std::thread::sleep(Duration::from_millis(2));
        let evicted = cache.evict_idle(Duration::ZERO);
        assert_eq!(evicted.len(), 3, "ZERO 全量清冷: {:?}", evicted);
        assert_eq!(cache.len(), 0);
    }

    /// approx_bytes：未发布的 ws 计 0（跳过）；空 cache = 0。
    #[test]
    fn approx_bytes_unpublished_is_zero() {
        let cache = SnapshotCache::new(8);
        assert_eq!(cache.approx_bytes(), 0, "空 cache");
        cache.get_or_create("A"); // 创建但未发布
        assert_eq!(cache.approx_bytes(), 0, "未发布 = 0");
    }

    /// approx_bytes 聚合：各 ws 已发布 snapshot 的 estimate 求和（AC-RG-02-4 前置）。
    #[test]
    fn approx_bytes_sums_published_stores() {
        let cache = SnapshotCache::new(8);
        let mgr_a = cache.get_or_create("A");
        mgr_a.publish(make_loaded_snapshot(1, "A"));
        let single = cache.approx_bytes();
        assert!(single > 0, "已发布带数据 snapshot 应 >0");
        let mgr_b = cache.get_or_create("B");
        mgr_b.publish(make_loaded_snapshot(2, "B"));
        let total = cache.approx_bytes();
        assert_eq!(total, single * 2, "聚合 = 各 ws 之和（同规模 store）");
        // 驱逐 A 后只剩 B 的份额
        cache.evict("A");
        assert_eq!(cache.approx_bytes(), single);
    }

    /// AC-RG-02-5：驱逐不破坏 in-flight 查询——Guard 存活期间数据可读，
    /// 查询结束（drop Guard）后才释放。
    #[test]
    fn ac_rg_02_5_guard_survives_evict() {
        let cache = SnapshotCache::new(8);
        let mgr = cache.get_or_create("A");
        mgr.publish(make_loaded_snapshot(1, "A"));
        let guard = mgr.load(); // in-flight 查询持有 Guard
        // 驱逐（cache 移除 manager；测试侧 mgr Arc 仍存活）
        let removed = cache.evict("A");
        assert!(removed.is_some());
        assert_eq!(cache.len(), 0);
        // Guard 存活：查询正常返回（驱逐未破坏 in-flight 数据）
        assert_eq!(
            guard.as_ref().as_ref().unwrap().generation,
            1,
            "驱逐后 in-flight Guard 仍可读"
        );
        drop(guard); // 查询结束才释放
        drop(mgr);
    }
}
