//! R6: SnapshotDaemonState —— 集成 SnapshotCache 的 daemon state 实现
//!
//! 在 `WorkspaceDaemonState` 之上扩展，新增以下 RPC handler：
//!
//! - `snapshot.publish`：接收 db_path 或 FD（Linux `/proc/self/fd`）+ WAL checkpoint
//!   + 调用 `SnapshotManager::build_and_publish_blocking`
//! - `gc.snapshots`：遍历所有 workspace，调用 `SnapshotManager::gc_generations(keep_last)`
//! - `query.stats`：通过 `SnapshotCache` 获取 store + `stats_rust()`
//! - `query.symbol`：调用 `GraphStore::get_symbol_ref`
//! - `query.search`：调用 `GraphStore::search_symbols_rust`
//! - `query.callers`：调用 `GraphStore::get_caller_ids` + 组装 JSON
//! - `query.callees`：调用 `GraphStore::get_callee_ids` + 组装 JSON
//!
//! workspace.* 方法（register/list/status/connect/file.refresh/recover）委托给
//! `WorkspaceDaemonState`（在 base 中实现）。
//! 运维方法（backup/restore/gc.cas）也委托给 base。
//!
//! 至此所有 dispatch 路由均有实现（除 `cw_daemon.rs` 中 `sd_notify READY=1` TODO 外）。
//!
//! 参考：Python `server/daemon_server.py:EnterpriseDaemonService.dispatch` L441-490

use std::collections::HashSet;
use std::sync::Arc;

use serde_json::{Map, Value};

use super::dispatch::{
    DaemonState, DaemonStateExt, DaemonRpcError, PeerCredential, get_str_param,
    require_str_param, get_str_param_or,
};
use super::workspace::{WorkspaceDaemonState, WorkspaceRegistry, owned_workspace, validate_owned_path};
use crate::graph::{CallChainEdgeInfo, GraphStore};
use crate::snapshot::SnapshotCache;

// ============================================
// SnapshotDaemonState
// ============================================

/// 集成 `SnapshotCache` 的 daemon state。
///
/// 组合 `WorkspaceDaemonState`（处理 workspace.*）+ `SnapshotCache`
/// （处理 snapshot.publish / gc.snapshots / query.*）。
///
/// 用法：
/// ```ignore
/// let registry = WorkspaceRegistry::open_in_memory()?;
/// let snapshot_cache = Arc::new(SnapshotCache::new(32));
/// let state = SnapshotDaemonState::with_registry(registry, snapshot_cache);
/// ```
pub struct SnapshotDaemonState {
    /// 委托 workspace.* 处理（含 base DaemonState）
    base: WorkspaceDaemonState,
    /// 多 workspace snapshot 缓存
    snapshot_cache: Arc<SnapshotCache>,
}

impl SnapshotDaemonState {
    /// 从已有 `WorkspaceDaemonState` + `SnapshotCache` 构造
    pub fn new(base: WorkspaceDaemonState, snapshot_cache: Arc<SnapshotCache>) -> Self {
        Self {
            base,
            snapshot_cache,
        }
    }

    /// 便捷构造器：从 `WorkspaceRegistry` + `SnapshotCache` 直接构造
    pub fn with_registry(registry: WorkspaceRegistry, snapshot_cache: Arc<SnapshotCache>) -> Self {
        Self::new(WorkspaceDaemonState::new(registry), snapshot_cache)
    }

    /// 便捷构造器：指定 data_root（用于 workspace.recover 找到 staging.log）
    pub fn with_registry_and_data_root(
        registry: WorkspaceRegistry,
        snapshot_cache: Arc<SnapshotCache>,
        data_root: std::path::PathBuf,
    ) -> Self {
        Self::new(
            WorkspaceDaemonState::with_data_root(registry, data_root),
            snapshot_cache,
        )
    }

    /// 访问内部 registry（供测试或外部查询）
    pub fn registry(&self) -> &WorkspaceRegistry {
        &self.base.registry
    }

    /// 访问内部 snapshot_cache
    pub fn snapshot_cache(&self) -> &Arc<SnapshotCache> {
        &self.snapshot_cache
    }

    /// 获取 workspace 的 SnapshotManager（不存在返回 None）
    fn get_snapshot_manager(
        &self,
        workspace_id: &str,
    ) -> Option<Arc<crate::snapshot::SnapshotManager>> {
        self.snapshot_cache.get(workspace_id)
    }

    /// 获取 workspace 的当前 GraphStore Arc（不存在或未发布返回 None）
    fn get_store(&self, workspace_id: &str) -> Option<Arc<GraphStore>> {
        self.snapshot_cache
            .get(workspace_id)
            .and_then(|mgr| mgr.current_store())
    }

    /// 构造单个符号的 JSON 对象（对应 Python `get_symbol` 返回字段）
    ///
    /// 字段：id / name / kind / qualified_name / module_path / start_line / end_line /
    /// depth / file_rel_path
    fn symbol_to_json(&self, store: &GraphStore, sym: &crate::graph::GraphSymbol) -> Value {
        let symbols = match store.symbols_table() {
            Some(s) => s,
            None => return Value::Null,
        };
        let mut m = Map::new();
        m.insert("id".into(), Value::Number(sym.id.into()));
        m.insert(
            "name".into(),
            Value::String(symbols.sym_name(sym).to_string()),
        );
        m.insert(
            "kind".into(),
            Value::String(sym.kind.as_str().to_string()),
        );
        m.insert(
            "qualified_name".into(),
            Value::String(symbols.sym_qname(sym).to_string()),
        );
        m.insert(
            "module_path".into(),
            Value::String(symbols.sym_module(sym).to_string()),
        );
        m.insert("start_line".into(), Value::Number(sym.start_line.into()));
        m.insert("end_line".into(), Value::Number(sym.end_line.into()));
        m.insert("depth".into(), Value::Number(sym.depth.into()));
        m.insert(
            "file_rel_path".into(),
            Value::String(symbols.file_rel_path(sym.file_instance_id).to_string()),
        );
        Value::Object(m)
    }
}

impl DaemonStateExt for SnapshotDaemonState {
    fn daemon_state(&self) -> &DaemonState {
        self.base.daemon_state()
    }

    // ---- 基础方法（重写 health 附加 snapshot 统计）----

    fn handle_health(&mut self, _peer: PeerCredential) -> Result<Value, DaemonRpcError> {
        let state = self.daemon_state();
        let uptime = state.start_time.elapsed().as_secs();
        let workspace_count = self
            .base
            .registry
            .count_workspaces()
            .map_err(|e| DaemonRpcError::internal_error(format!("count_workspaces: {}", e)))?;
        let snapshot_workspace_count = self.snapshot_cache.len();

        let mut m = Map::new();
        m.insert("status".into(), Value::String("ok".to_string()));
        m.insert("pid".into(), Value::Number(state.pid.into()));
        m.insert("uptime_seconds".into(), Value::Number(uptime.into()));
        m.insert(
            "schema_version".into(),
            Value::Number(state.schema_version.into()),
        );
        m.insert("workspace_count".into(), Value::Number(workspace_count.into()));
        m.insert(
            "snapshot_workspace_count".into(),
            Value::Number(snapshot_workspace_count.into()),
        );
        Ok(Value::Object(m))
    }

    // ---- workspace.* 委托 base ----

    fn handle_workspace_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_register(peer, params)
    }

    fn handle_workspace_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_list(peer, params)
    }

    fn handle_workspace_status(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_status(peer, params)
    }

    fn handle_workspace_recover(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_recover(peer, params)
    }

    fn handle_workspace_connect(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_connect(peer, params)
    }

    fn handle_workspace_file_refresh(
        &mut self,
        peer: PeerCredential,
        params: &Value,
        received_fds: &[i32],
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_file_refresh(peer, params, received_fds)
    }

    // ---- 运维方法（backup / restore / gc.cas 委托 base）----

    fn handle_backup(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_backup(peer, params)
    }

    fn handle_restore(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_restore(peer, params)
    }

    fn handle_gc_cas(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_gc_cas(peer, params)
    }

    // ---- snapshot 管理（R6 实现）----

    fn handle_snapshot_publish(
        &mut self,
        peer: PeerCredential,
        params: &Value,
        received_fds: &[i32],
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let build_context_hash = get_str_param_or(params, "build_context_hash", "");
        let snapshot_id = get_str_param(params, "snapshot_id").map(|s| s.to_string());

        // ACL 检查（workspace 必须属于 peer）
        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;

        // 确定 db_path：优先用 FD（Linux），其次用 db_path 参数
        let db_path = if !received_fds.is_empty() {
            #[cfg(unix)]
            {
                if received_fds.len() > 1 {
                    return Err(DaemonRpcError::invalid_params(
                        "snapshot.publish 最多接收 1 个 FD",
                    ));
                }
                validate_snapshot_fd(received_fds[0], peer.uid)?
            }
            #[cfg(not(unix))]
            {
                let _ = peer;
                return Err(DaemonRpcError::invalid_params(
                    "FD 模式仅 Unix 支持（Windows 跳过 #[cfg(unix)]）",
                ));
            }
        } else {
            let path = require_str_param(params, "db_path")?;
            // 路径校验 + owner_uid ACL
            let real_path = validate_owned_path(path, peer.uid, true)?;
            // WAL checkpoint（GraphStore 用 immutable=1 打开，必须先 checkpoint）
            wal_checkpoint(&real_path)?;
            real_path
        };

        // 获取或创建 SnapshotManager
        let mgr = self.snapshot_cache.get_or_create(workspace_instance_id);

        // 构建 + 发布
        let (generation, symbol_count, call_count) = mgr
            .build_and_publish_blocking(&db_path, &build_context_hash, snapshot_id)
            .map_err(|e| DaemonRpcError::internal_error(format!("build_and_publish: {}", e)))?;

        let mut m = Map::new();
        m.insert("generation".into(), Value::Number(generation.into()));
        m.insert("symbol_count".into(), Value::Number(symbol_count.into()));
        m.insert("call_count".into(), Value::Number(call_count.into()));
        m.insert(
            "workspace_instance_id".into(),
            Value::String(workspace_instance_id.to_string()),
        );
        Ok(Value::Object(m))
    }

    fn handle_gc_snapshots(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // gc.snapshots 不需要 workspace_id（全局 GC）
        // 对应 Python daemon_server.py L345-349
        let keep_last = get_str_param(params, "keep_last")
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(3);

        let mut total_deleted = 0usize;
        for ws_id in self.snapshot_cache.list_workspaces() {
            if let Some(mgr) = self.snapshot_cache.get(&ws_id) {
                total_deleted += mgr.gc_generations(keep_last);
            }
        }

        let mut m = Map::new();
        m.insert("deleted_count".into(), Value::Number(total_deleted.into()));
        m.insert("keep_last".into(), Value::Number(keep_last.into()));
        Ok(Value::Object(m))
    }

    // ---- query.* 方法（R6 实现）----

    fn handle_query_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;

        let store = self
            .get_store(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;

        let mut stats = store.stats_rust();
        // 附加 snapshot 元信息
        if let Some(mgr) = self.get_snapshot_manager(workspace_instance_id) {
            if let Some((gen, db_path)) = mgr.current_meta() {
                if let Value::Object(ref mut m) = stats {
                    m.insert("generation".into(), Value::Number(gen.into()));
                    m.insert("source_db_path".into(), Value::String(db_path));
                }
            }
        }
        Ok(stats)
    }

    fn handle_query_symbol(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let qualified_name = require_str_param(params, "qualified_name")?;
        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;

        let store = self
            .get_store(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;

        match store.get_symbol_ref(qualified_name) {
            Some(sym) => Ok(self.symbol_to_json(&store, sym)),
            None => Ok(Value::Null),
        }
    }

    fn handle_query_search(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let query = require_str_param(params, "query")?;
        let kind = get_str_param(params, "kind");
        let limit = get_str_param(params, "limit")
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(20);

        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self
            .get_store(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;

        let sym_ids = store.search_symbols_rust(query, kind, limit);
        let mut results = Vec::with_capacity(sym_ids.len());
        for sym_id in sym_ids {
            if let Some(sym) = store.get_symbol_by_id(sym_id) {
                results.push(self.symbol_to_json(&store, sym));
            }
        }
        Ok(Value::Array(results))
    }

    fn handle_query_callers(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let callee_name = require_str_param(params, "callee_name")?;
        let qualified_name = get_str_param(params, "qualified_name");

        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self
            .get_store(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;

        let symbols = store
            .symbols_table()
            .ok_or_else(|| DaemonRpcError::internal_error("symbols table not loaded"))?;

        // 解析 callee_ids：若传了 qname 则精确匹配单个，否则用 simple_name 找所有同名
        let callee_ids: Vec<u32> = if let Some(qname) = qualified_name {
            store.get_symbol_ref(qname).map(|s| vec![s.id]).unwrap_or_default()
        } else {
            symbols.simple_name_ids(callee_name).to_vec()
        };

        let mut callers = Vec::new();
        let mut seen = HashSet::new();
        for cid in callee_ids {
            for caller_id in store.get_caller_ids(cid) {
                if !seen.insert(caller_id) {
                    continue;
                }
                if let Some(caller_sym) = store.get_symbol_by_id(caller_id) {
                    let mut m = Map::new();
                    m.insert(
                        "caller_name".into(),
                        Value::String(symbols.sym_name(caller_sym).to_string()),
                    );
                    m.insert(
                        "caller_qualified_name".into(),
                        Value::String(symbols.sym_qname(caller_sym).to_string()),
                    );
                    m.insert("caller_id".into(), Value::Number(caller_id.into()));
                    m.insert(
                        "caller_file".into(),
                        Value::String(
                            symbols.file_rel_path(caller_sym.file_instance_id).to_string(),
                        ),
                    );
                    m.insert(
                        "caller_module".into(),
                        Value::String(symbols.sym_module(caller_sym).to_string()),
                    );
                    callers.push(Value::Object(m));
                }
            }
        }
        Ok(Value::Array(callers))
    }

    fn handle_query_callees(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let caller_name = require_str_param(params, "caller_name")?;
        let qualified_name = get_str_param(params, "qualified_name");

        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self
            .get_store(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;

        let symbols = store
            .symbols_table()
            .ok_or_else(|| DaemonRpcError::internal_error("symbols table not loaded"))?;

        let caller_ids: Vec<u32> = if let Some(qname) = qualified_name {
            store.get_symbol_ref(qname).map(|s| vec![s.id]).unwrap_or_default()
        } else {
            symbols.simple_name_ids(caller_name).to_vec()
        };

        let mut callees = Vec::new();
        let mut seen = HashSet::new();
        for caller_id in caller_ids {
            for callee_id in store.get_callee_ids(caller_id) {
                if !seen.insert(callee_id) {
                    continue;
                }
                if let Some(callee_sym) = store.get_symbol_by_id(callee_id) {
                    let mut m = Map::new();
                    m.insert(
                        "callee_name".into(),
                        Value::String(symbols.sym_name(callee_sym).to_string()),
                    );
                    m.insert(
                        "callee_qualified_name".into(),
                        Value::String(symbols.sym_qname(callee_sym).to_string()),
                    );
                    m.insert("callee_id".into(), Value::Number(callee_id.into()));
                    m.insert(
                        "callee_file".into(),
                        Value::String(
                            symbols.file_rel_path(callee_sym.file_instance_id).to_string(),
                        ),
                    );
                    m.insert(
                        "callee_module".into(),
                        Value::String(symbols.sym_module(callee_sym).to_string()),
                    );
                    callees.push(Value::Object(m));
                }
            }
        }
        Ok(Value::Array(callees))
    }

    // ---- G7-T4: 高级查询方法（call_chain_down / topological_order / detect_cycles）----
    // 对齐 Python snapshot_manager.py:305-373 的 query_call_chain_down 等
    //
    // 与 R6 的 query.* 一致：均需 workspace ACL 校验 + snapshot 已发布检查。

    fn handle_query_call_chain_down(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let qualified_name = require_str_param(params, "qualified_name")?;
        let max_depth = get_str_param(params, "max_depth")
            .and_then(|s| s.parse::<usize>().ok())
            .unwrap_or(5);

        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self
            .get_store(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;

        let edges = store.call_chain_down_rust(qualified_name, max_depth);
        let results: Vec<Value> = edges
            .into_iter()
            .map(|e| {
                let mut m = Map::new();
                m.insert("depth".into(), Value::Number(e.depth.into()));
                m.insert("caller_name".into(), Value::String(e.caller_name));
                m.insert("callee_name".into(), Value::String(e.callee_name));
                m.insert("callee_id".into(), Value::Number(e.callee_id.into()));
                m.insert("call_line".into(), Value::Number(e.call_line.into()));
                m.insert("is_cross_file".into(), Value::Bool(e.is_cross_file));
                Value::Object(m)
            })
            .collect();
        Ok(Value::Array(results))
    }

    fn handle_query_topological_order(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self
            .get_store(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;

        let order = store.topological_order_rust();
        let results: Vec<Value> = order.into_iter().map(Value::String).collect();
        Ok(Value::Array(results))
    }

    fn handle_query_detect_cycles(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self
            .get_store(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;

        let cycles = store.detect_cycles_rust();
        let results: Vec<Value> = cycles
            .into_iter()
            .map(|c| {
                Value::Array(c.into_iter().map(Value::String).collect())
            })
            .collect();
        Ok(Value::Array(results))
    }

    // ---- G7-T5: Snapshot 管理方法（stats / list_workspaces / evict）----
    // 对齐 Python snapshot_manager.py:162-192 的 get_snapshot_stats 等
    //
    // 与 snapshot.publish 一样使用 owned_workspace ACL 校验（evict/stats 需 workspace_id），
    // list_workspaces 不带 workspace_id 参数，返回当前 cache 中所有 workspace 的统计信息。

    fn handle_snapshot_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;

        let mgr = self.get_snapshot_manager(workspace_instance_id).ok_or_else(|| {
            DaemonRpcError::new(
                "snapshot_not_ready",
                format!("workspace {} 未发布 snapshot", workspace_instance_id),
            )
        })?;

        let health = mgr.current_health().ok_or_else(|| {
            DaemonRpcError::new(
                "snapshot_not_ready",
                format!("workspace {} 未发布 snapshot", workspace_instance_id),
            )
        })?;
        let (generation, source_db_path) = mgr
            .current_meta()
            .unwrap_or((0, String::new()));

        let mut m = Map::new();
        m.insert(
            "workspace_instance_id".into(),
            Value::String(workspace_instance_id.to_string()),
        );
        m.insert("generation".into(), Value::Number(generation.into()));
        m.insert("symbol_count".into(), Value::Number(health.symbol_count.into()));
        m.insert("call_count".into(), Value::Number(health.call_count.into()));
        m.insert("file_count".into(), Value::Number(health.file_count.into()));
        m.insert(
            "build_duration_ms".into(),
            Value::Number(health.build_duration_ms.into()),
        );
        m.insert(
            "last_error".into(),
            match health.last_error {
                Some(e) => Value::String(e),
                None => Value::Null,
            },
        );
        m.insert("source_db_path".into(), Value::String(source_db_path));
        // history_len 便于运维判断 GC 时机
        m.insert("history_len".into(), Value::Number(mgr.history_len().into()));
        Ok(Value::Object(m))
    }

    fn handle_snapshot_list_workspaces(
        &mut self,
        _peer: PeerCredential,
        _params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // 列出所有已发布 snapshot 的 workspace（对齐 Python list_workspaces）
        // 返回每个 workspace 的 generation + health 摘要，便于运维监控
        let ws_ids = self.snapshot_cache.list_workspaces();
        let mut entries = Vec::with_capacity(ws_ids.len());
        for ws_id in ws_ids {
            if let Some(mgr) = self.snapshot_cache.get(&ws_id) {
                let mut m = Map::new();
                m.insert("workspace_instance_id".into(), Value::String(ws_id));
                m.insert(
                    "generation".into(),
                    Value::Number(mgr.current_generation().into()),
                );
                m.insert("history_len".into(), Value::Number(mgr.history_len().into()));
                if let Some(h) = mgr.current_health() {
                    m.insert("symbol_count".into(), Value::Number(h.symbol_count.into()));
                    m.insert("call_count".into(), Value::Number(h.call_count.into()));
                    m.insert("file_count".into(), Value::Number(h.file_count.into()));
                    m.insert(
                        "build_duration_ms".into(),
                        Value::Number(h.build_duration_ms.into()),
                    );
                }
                entries.push(Value::Object(m));
            }
        }
        Ok(Value::Array(entries))
    }

    fn handle_snapshot_evict(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // evict 修改 cache，必须校验 workspace 所有权（与 snapshot.publish 一致）
        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;

        let removed = self.snapshot_cache.evict(workspace_instance_id);
        let mut m = Map::new();
        m.insert("evicted".into(), Value::Bool(removed.is_some()));
        m.insert(
            "workspace_instance_id".into(),
            Value::String(workspace_instance_id.to_string()),
        );
        Ok(Value::Object(m))
    }

    // ---- 以下方法保持默认 method_not_found（R6 范围外）----
    // handle_workspace_connect / handle_workspace_file_refresh / handle_workspace_recover
    // handle_gc_cas / handle_backup / handle_restore
    // 这些方法使用 DaemonStateExt 的默认实现（返回 method_not_found）
}

// ============================================
// 辅助函数
// ============================================

/// WAL checkpoint（在 GraphStore 用 immutable=1 打开前必须调用）
///
/// 对应 AGENTS.md 第 7 条规则：SQLite WAL 模式下 `immutable=1` 只读连接会读到旧数据，
/// 必须先 `PRAGMA wal_checkpoint(PASSIVE)` 确保 WAL 数据已 checkpoint 到主库。
///
/// 对应 Python daemon_server.py L446-452 的 WAL checkpoint 逻辑。
fn wal_checkpoint(db_path: &str) -> Result<(), DaemonRpcError> {
    use rusqlite::Connection;
    let conn = Connection::open(db_path)
        .map_err(|e| DaemonRpcError::internal_error(format!("open db for checkpoint: {}", e)))?;
    conn.execute_batch("PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(PASSIVE);")
        .map_err(|e| DaemonRpcError::internal_error(format!("wal_checkpoint: {}", e)))?;
    Ok(())
}

/// 校验 FD 是只读常规文件 + owner_uid 匹配（Linux）
///
/// 对应 Python daemon_server.py:EnterpriseDaemonService._validate_snapshot_fd L493-514
///
/// 校验项：
/// 1. `fstat(fd)` 必须是常规文件（`S_IFREG`）
/// 2. `st_uid` 必须匹配 peer_uid（root 跳过）
/// 3. 大小检查：`0 < st_size <= 64 GiB`
/// 4. `fcntl(F_GETFL) & O_ACCMODE == O_RDONLY`（必须只读）
///
/// 返回 `/proc/self/fd/{fd}` 路径供 GraphStore `immutable=1` URI 打开。
#[cfg(unix)]
fn validate_snapshot_fd(fd: i32, peer_uid: u32) -> Result<String, DaemonRpcError> {
    // fstat 校验
    let mut stat_buf: libc::stat = unsafe { std::mem::zeroed() };
    let ret = unsafe { libc::fstat(fd, &mut stat_buf) };
    if ret < 0 {
        return Err(DaemonRpcError::invalid_params(format!(
            "fstat fd {} 失败: {}",
            fd,
            std::io::Error::last_os_error()
        )));
    }
    // 必须是常规文件
    if (stat_buf.st_mode & libc::S_IFMT) != libc::S_IFREG {
        return Err(DaemonRpcError::invalid_params(format!(
            "fd {} 不是常规文件",
            fd
        )));
    }
    // owner_uid 校验（root 跳过）
    if peer_uid != 0 && stat_buf.st_uid != peer_uid as u32 {
        return Err(DaemonRpcError::permission_denied(format!(
            "fd owner_uid={} != peer_uid={}",
            stat_buf.st_uid, peer_uid
        )));
    }
    // 大小检查（默认 64 GiB，与 Python CW_MAX_SNAPSHOT_DB_BYTES 一致）
    const MAX_SNAPSHOT_DB_BYTES: u64 = 64 * 1024 * 1024 * 1024;
    let st_size = stat_buf.st_size as u64;
    if st_size == 0 || st_size > MAX_SNAPSHOT_DB_BYTES {
        return Err(DaemonRpcError::invalid_params(format!(
            "fd size {} 超出范围 (0, {}]",
            st_size, MAX_SNAPSHOT_DB_BYTES
        )));
    }
    // O_RDONLY 校验
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFL) };
    if flags < 0 {
        return Err(DaemonRpcError::invalid_params(format!(
            "fcntl F_GETFL fd {} 失败: {}",
            fd,
            std::io::Error::last_os_error()
        )));
    }
    let acc_mode = (flags & libc::O_ACCMODE) as i32;
    if acc_mode != libc::O_RDONLY {
        return Err(DaemonRpcError::permission_denied(format!(
            "fd {} 不是只读（O_ACCMODE={}）",
            fd, acc_mode
        )));
    }
    // 返回 /proc/self/fd/{fd} 路径
    Ok(format!("/proc/self/fd/{}", fd))
}

// ============================================
// 测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::dispatch::dispatch;
    use crate::snapshot::SnapshotCache;
    use serde_json::json;

    /// 创建测试用 state（内存 registry + 默认 snapshot cache）
    fn make_state() -> SnapshotDaemonState {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let snapshot_cache = Arc::new(SnapshotCache::new(32));
        SnapshotDaemonState::with_registry(registry, snapshot_cache)
    }

    fn make_peer(uid: u32) -> PeerCredential {
        PeerCredential {
            uid,
            gid: 1000,
            pid: 12345,
        }
    }

    /// 注册一个 workspace（用当前进程 uid 作为 owner，避免 Unix ACL 失败）
    fn register_workspace_for_test(state: &mut SnapshotDaemonState, uid: u32) -> String {
        let tmp = tempfile::tempdir().unwrap();
        let dir_path = tmp.path().to_str().unwrap().to_string();
        // 注意：tmp 在函数结束时会清理，所以仅用于注册校验
        let params = json!({
            "client_view_root": dir_path,
            "git_remote_url": "https://example.com/test.git",
            "git_head_commit_sha": "abc123",
            "toolchain_fingerprint": "test"
        });
        let response = dispatch(
            state,
            make_peer(uid),
            "workspace.register",
            &params,
            &[],
        );
        assert_eq!(response["ok"], true);
        response["result"]["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string()
    }

    // ---- health ----

    #[test]
    fn test_handle_health_returns_snapshot_workspace_count() {
        let mut state = make_state();
        let peer = make_peer(0); // root 避免 Unix ACL 问题

        let response = dispatch(&mut state, peer, "health", &json!({}), &[]);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
        assert!(
            response["result"]["snapshot_workspace_count"].as_u64().unwrap_or(0) == 0,
            "初始 snapshot_workspace_count 应为 0"
        );
    }

    // ---- gc.snapshots ----

    #[test]
    fn test_gc_snapshots_returns_zero_for_empty_cache() {
        let mut state = make_state();
        let peer = make_peer(0);

        let response = dispatch(
            &mut state,
            peer,
            "gc.snapshots",
            &json!({"keep_last": 3}),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["deleted_count"], 0);
        assert_eq!(response["result"]["keep_last"], 3);
    }

    #[test]
    fn test_gc_snapshots_uses_default_keep_last_when_missing() {
        let mut state = make_state();
        let peer = make_peer(0);

        let response = dispatch(&mut state, peer, "gc.snapshots", &json!({}), &[]);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["keep_last"], 3);
    }

    // ---- query.* 无 snapshot 时返回 snapshot_not_ready ----

    #[test]
    fn test_query_stats_returns_snapshot_not_ready_when_no_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "query.stats",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    #[test]
    fn test_query_symbol_returns_snapshot_not_ready_when_no_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "query.symbol",
            &json!({
                "workspace_instance_id": ws_id,
                "qualified_name": "test_fn"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    #[test]
    fn test_query_search_returns_snapshot_not_ready_when_no_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "query.search",
            &json!({
                "workspace_instance_id": ws_id,
                "query": "test"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    #[test]
    fn test_query_callers_returns_snapshot_not_ready_when_no_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "query.callers",
            &json!({
                "workspace_instance_id": ws_id,
                "callee_name": "some_fn"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    #[test]
    fn test_query_callees_returns_snapshot_not_ready_when_no_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "query.callees",
            &json!({
                "workspace_instance_id": ws_id,
                "caller_name": "some_fn"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    // ---- snapshot.publish 缺少 db_path/FD 时报错 ----

    #[test]
    fn test_snapshot_publish_requires_db_path_or_fd() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        // 既无 db_path 也无 FD
        let response = dispatch(
            &mut state,
            peer,
            "snapshot.publish",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_snapshot_publish_rejects_nonexistent_workspace() {
        let mut state = make_state();
        let peer = make_peer(0);

        let response = dispatch(
            &mut state,
            peer,
            "snapshot.publish",
            &json!({
                "workspace_instance_id": "nonexistent_ws",
                "db_path": "/tmp/whatever.db"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_not_found");
    }

    // ---- query ACL 隔离 ----

    #[test]
    fn test_query_stats_rejects_non_owner() {
        let mut state = make_state();
        let owner_uid = 0; // root 注册
        let ws_id = register_workspace_for_test(&mut state, owner_uid);

        // 用另一个 uid 查询
        let other_peer = make_peer(9999);
        let response = dispatch(
            &mut state,
            other_peer,
            "query.stats",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        // workspace_forbidden 优先于 snapshot_not_ready
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    // ---- method_not_found for R6-out-of-scope methods ----

    #[test]
    fn test_gc_cas_delegated_to_base() {
        // gc.cas 现已委托给 WorkspaceDaemonState 实现，
        // dispatch 应进入 handler 而非返回 method_not_found。
        // 传入缺少 workspace_instance_id 的参数，应返回 invalid_params（而非 method_not_found）。
        let mut state = make_state();
        let peer = make_peer(0);

        let response = dispatch(
            &mut state,
            peer,
            "gc.cas",
            &json!({"grace_days": 7}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_backup_delegated_to_base() {
        // backup 现已委托给 WorkspaceDaemonState 实现，
        // dispatch 应进入 handler 而非返回 method_not_found。
        // 传入缺少 output_path 的参数，应返回 invalid_params（而非 method_not_found）。
        let mut state = make_state();
        let peer = make_peer(0);

        let response = dispatch(&mut state, peer, "backup", &json!({}), &[]);
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_workspace_connect_delegated_to_base() {
        // workspace.connect 现已委托给 WorkspaceDaemonState 实现，
        // dispatch 应进入 handler 而非返回 method_not_found。
        // 传入缺少 agent_session_id 的参数，应返回 invalid_params（而非 method_not_found）。
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.connect",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_workspace_file_refresh_delegated_to_base() {
        // workspace.file.refresh 现已委托给 WorkspaceDaemonState 实现，
        // dispatch 应进入 handler 而非返回 method_not_found。
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        // 缺 rel_path 等参数：应返回 invalid_params（而非 method_not_found）
        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    // ---- 辅助函数测试 ----

    #[test]
    fn test_wal_checkpoint_on_nonexistent_db_returns_error() {
        let result = wal_checkpoint("/nonexistent/path/xyz.db");
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "internal_error");
    }

    #[test]
    #[cfg(unix)]
    fn test_validate_snapshot_fd_rejects_invalid_fd() {
        // fd = 99999 几乎肯定无效
        let result = validate_snapshot_fd(99999, 0);
        assert!(result.is_err());
    }

    #[test]
    #[cfg(unix)]
    fn test_validate_snapshot_fd_accepts_readonly_file() {
        use std::os::unix::io::AsRawFd;
        // 创建临时只读文件
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().join("test_readonly.db");
        std::fs::write(&path, b"hello").unwrap();
        let file = std::fs::File::open(&path).unwrap();
        let fd = file.as_raw_fd();
        // peer_uid=0 (root) 跳过 owner 检查
        let result = validate_snapshot_fd(fd, 0);
        assert!(result.is_ok());
        let returned_path = result.unwrap();
        assert_eq!(returned_path, format!("/proc/self/fd/{}", fd));
    }

    // ---- G7-T4/T5: 新增 6 个 RPC handler 的测试 ----

    #[test]
    fn test_query_call_chain_down_returns_snapshot_not_ready_when_no_snapshot() {
        // 未发布 snapshot 时，query.call_chain_down 返回 snapshot_not_ready
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "query.call_chain_down",
            &json!({
                "workspace_instance_id": ws_id,
                "qualified_name": "some_fn"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    #[test]
    fn test_query_call_chain_down_rejects_non_owner() {
        // 非 workspace 所有者调用，返回 workspace_forbidden（优先于 snapshot_not_ready）
        let mut state = make_state();
        let ws_id = register_workspace_for_test(&mut state, 0);

        let other_peer = make_peer(9999);
        let response = dispatch(
            &mut state,
            other_peer,
            "query.call_chain_down",
            &json!({
                "workspace_instance_id": ws_id,
                "qualified_name": "some_fn"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_query_call_chain_down_requires_qualified_name() {
        // 缺 qualified_name 参数时返回 invalid_params
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "query.call_chain_down",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_query_topological_order_returns_snapshot_not_ready_when_no_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "query.topological_order",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    #[test]
    fn test_query_detect_cycles_returns_snapshot_not_ready_when_no_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "query.detect_cycles",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    #[test]
    fn test_snapshot_stats_returns_snapshot_not_ready_when_no_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "snapshot.stats",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    #[test]
    fn test_snapshot_stats_rejects_non_owner() {
        let mut state = make_state();
        let ws_id = register_workspace_for_test(&mut state, 0);

        let other_peer = make_peer(9999);
        let response = dispatch(
            &mut state,
            other_peer,
            "snapshot.stats",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_snapshot_list_workspaces_returns_empty_array_initially() {
        // 初始空 cache，snapshot.list_workspaces 返回空数组
        let mut state = make_state();
        let peer = make_peer(0);

        let response = dispatch(
            &mut state,
            peer,
            "snapshot.list_workspaces",
            &json!({}),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert!(response["result"].is_array());
        assert_eq!(response["result"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn test_snapshot_evict_rejects_nonexistent_workspace() {
        // evict 未注册的 workspace 返回 workspace_not_found
        let mut state = make_state();
        let peer = make_peer(0);

        let response = dispatch(
            &mut state,
            peer,
            "snapshot.evict",
            &json!({"workspace_instance_id": "nonexistent_ws"}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_not_found");
    }

    #[test]
    fn test_snapshot_evict_rejects_non_owner() {
        let mut state = make_state();
        let ws_id = register_workspace_for_test(&mut state, 0);

        let other_peer = make_peer(9999);
        let response = dispatch(
            &mut state,
            other_peer,
            "snapshot.evict",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_snapshot_evict_returns_false_for_registered_but_uncached_workspace() {
        // workspace 已注册但从未发布 snapshot（cache 中无 manager）
        // evict 应返回 evicted: false（cache.evict 返回 None）
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "snapshot.evict",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["evicted"], false);
        assert_eq!(response["result"]["workspace_instance_id"], ws_id);
    }

    #[test]
    fn test_snapshot_evict_returns_true_after_get_or_create() {
        // 通过 get_or_create 创建 manager 后 evict 应返回 evicted: true
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        // 触发 cache 创建 manager（不需要真发布 snapshot）
        let _ = state.snapshot_cache().get_or_create(&ws_id);

        let response = dispatch(
            &mut state,
            peer,
            "snapshot.evict",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["evicted"], true);
    }
}
