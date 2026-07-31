//! R6: SnapshotDaemonState —— 集成 SnapshotCache 的 daemon state 实现
//!
//! 在 `WorkspaceDaemonState` 之上扩展，新增以下 RPC handler：
//!
//! - `snapshot.publish`：接收 db_path 或 FD（Linux `/proc/self/fd`）+ WAL checkpoint
//!   + 调用 `SnapshotManager::build_and_publish_blocking`
//! - `gc.snapshots`：遍历所有 workspace，调用 `SnapshotManager::gc_generations(keep_last)`
//! - `query.stats`：通过 `SnapshotCache` 获取 store + `stats_rust()`
//! - `query.symbol`：从当前 snapshot SQLite 查询完整符号详情
//! - `query.search`：调用 `GraphStore::search_symbols_rust`
//! - `query.callers`：从 GraphStore 反向索引逐边组装 JSON
//! - `query.callees`：从 GraphStore 正向索引逐边组装 JSON
//!
//! workspace.* 方法（register/list/status/connect/file.refresh/recover）委托给
//! `WorkspaceDaemonState`（在 base 中实现）。
//! 运维方法（backup/restore/gc.cas）也委托给 base。
//!
//! 至此所有 dispatch 路由均有实现（除 `cw_daemon.rs` 中 `sd_notify READY=1` TODO 外）。
//!
//! 参考：Python `server/daemon_server.py:EnterpriseDaemonService.dispatch` L441-490

use std::sync::Arc;

use rusqlite::{Connection, OpenFlags};
use serde_json::{json, Map, Value};

use super::dispatch::{
    current_daemon_uid, get_int_param_or, get_str_param, get_str_param_or, require_str_param,
    DaemonRpcError, DaemonState, DaemonStateExt, PeerCredential,
};
use super::workspace::{
    owned_workspace, owned_workspace_by_id, validate_owned_path, WorkspaceDaemonState,
    WorkspaceRegistry,
};
use crate::cli::impact::query_impact_with_store;
use crate::graph::GraphStore;
use crate::snapshot::SnapshotCache;
use crate::symbol_query::query_symbol_detail;

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
    /// G1 Layer 2: Toolchain DB（独立 toolchain.db，跨 workspace 共享）
    toolchain_store: Option<Arc<super::toolchain::ToolchainStore>>,
}

impl SnapshotDaemonState {
    /// 从已有 `WorkspaceDaemonState` + `SnapshotCache` 构造
    pub fn new(base: WorkspaceDaemonState, snapshot_cache: Arc<SnapshotCache>) -> Self {
        Self {
            base,
            snapshot_cache,
            toolchain_store: None,
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

    /// G1 Layer 2：注入 ToolchainStore
    pub fn with_toolchain_store(
        mut self,
        toolchain_store: Arc<super::toolchain::ToolchainStore>,
    ) -> Self {
        self.toolchain_store = Some(toolchain_store);
        self
    }

    /// G11：注入 SnapshotCachePublisher，透传到 base `WorkspaceDaemonState`
    ///
    /// 启用后 `handle_workspace_file_refresh` 中的 Replicator 会调用
    /// `publish_snapshot`，从 CodeGraph DB 加载符号 + 调用图 → 发布到
    /// 共享 `SnapshotCache`（per-workspace ArcSwap）。
    ///
    /// 必须配合 `with_codegraph_db_path_template` 一起使用：publisher 提供发布
    /// 能力，db_path 模板提供源数据库路径。二者任一缺失，replicate 跳过发布。
    pub fn with_snapshot_publisher(
        mut self,
        publisher: Arc<super::replicator::SnapshotCachePublisher>,
    ) -> Self {
        self.base = self.base.with_snapshot_publisher(publisher);
        self
    }

    /// G11：设置 CodeGraph DB 路径模板，透传到 base
    ///
    /// 模板含 `{workspace_instance_id}` 占位符，运行时替换为实际 workspace ID。
    /// 空字符串表示不启用 snapshot publish（保持 R5 行为）。
    pub fn with_codegraph_db_path_template(mut self, template: String) -> Self {
        self.base = self.base.with_codegraph_db_path_template(template);
        self
    }

    /// G1 Layer 2：获取 ToolchainStore（若未注入返回 None）
    pub fn toolchain_store(&self) -> Option<&Arc<super::toolchain::ToolchainStore>> {
        self.toolchain_store.as_ref()
    }

    /// G1 Layer 2：要求 ToolchainStore（若未注入返回 internal_error）
    fn require_toolchain_store(
        &self,
    ) -> Result<&Arc<super::toolchain::ToolchainStore>, DaemonRpcError> {
        self.toolchain_store.as_ref().ok_or_else(|| {
            DaemonRpcError::internal_error(
                "ToolchainStore 未注入（daemon 启动时未加载 toolchain.db）",
            )
        })
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
        m.insert("kind".into(), Value::String(sym.kind.as_str().to_string()));
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
        m.insert(
            "workspace_count".into(),
            Value::Number(workspace_count.into()),
        );
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
        self.base
            .handle_workspace_file_refresh(peer, params, received_fds)
    }

    fn handle_workspace_file_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_file_delete(peer, params)
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
        let workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;

        // P0-2 整改：提取数值 workspace_id，传入 build_and_publish_blocking
        // 用于 GraphStore SQL 层过滤，避免 snapshot 混入其他 workspace 数据
        let workspace_id_num: i64 = workspace
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| {
                DaemonRpcError::internal_error("workspace_id 字段缺失或非数值".to_string())
            })?;

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

        // 构建 + 发布（传入 workspace_id_num 用于 SQL 过滤）
        let (generation, symbol_count, call_count) = mgr
            .build_and_publish_blocking(
                &db_path,
                workspace_id_num,
                &build_context_hash,
                snapshot_id,
            )
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
        // G12 批次8：修复 keep_last 字段错配（同 query.search limit）
        let keep_last = get_int_param_or(params, "keep_last", 3) as usize;

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

        let store = self.get_store(workspace_instance_id).ok_or_else(|| {
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
        let workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let workspace_id = workspace
            .get("workspace_id")
            .and_then(Value::as_i64)
            .ok_or_else(|| {
                DaemonRpcError::internal_error("workspace_id 字段缺失或非数值".to_string())
            })?;
        let manager = self
            .get_snapshot_manager(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;
        let db_path = manager.current_query_db_path().ok_or_else(|| {
            DaemonRpcError::new(
                "snapshot_not_ready",
                format!("workspace {} 未发布 snapshot", workspace_instance_id),
            )
        })?;
        let conn = Connection::open_with_flags(
            &db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|error| {
            DaemonRpcError::internal_error(format!(
                "无法打开 snapshot SQLite {}: {}",
                db_path, error
            ))
        })?;
        query_symbol_detail(&conn, workspace_id, qualified_name)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_search(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let query = require_str_param(params, "query")?;
        let kind = get_str_param(params, "kind");
        // G12 批次8：修复 limit 字段错配——
        // Python daemon_client.py 传 int（如 `"limit": 50`），
        // 原 get_str_param 只接受字符串，数字被忽略导致始终用默认 20。
        // 改用 get_int_param_or 支持 JSON 数字 + 字符串两种形式。
        let limit = get_int_param_or(params, "limit", 20) as usize;

        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self.get_store(workspace_instance_id).ok_or_else(|| {
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
        let store = self.get_store(workspace_instance_id).ok_or_else(|| {
            DaemonRpcError::new(
                "snapshot_not_ready",
                format!("workspace {} 未发布 snapshot", workspace_instance_id),
            )
        })?;

        let symbols = store
            .symbols_table()
            .ok_or_else(|| DaemonRpcError::internal_error("symbols table not loaded"))?;
        let calls = store
            .call_graph()
            .ok_or_else(|| DaemonRpcError::internal_error("calls not loaded"))?;

        // 精确 QN 按已解析 callee_id 查边；短名路径同时保留 unresolved 边。
        let edge_positions: Vec<u32> = if let Some(qname) = qualified_name {
            match store.get_symbol_ref(qname).map(|symbol| symbol.id) {
                Some(callee_id) => calls.positions_for_callee_id(callee_id).to_vec(),
                None => Vec::new(),
            }
        } else {
            calls
                .callee_name_idx(callee_name)
                .map(|name_idx| calls.positions_for_callee_name(name_idx).to_vec())
                .unwrap_or_default()
        };

        let mut callers = Vec::new();
        for position in edge_positions {
            let Some(edge) = calls.forward_edges.get(position as usize) else {
                continue;
            };
            if let Some(caller_sym) = store.get_symbol_by_id(edge.caller_id) {
                let mut m = Map::new();
                m.insert(
                    "caller_name".into(),
                    Value::String(symbols.sym_name(caller_sym).to_string()),
                );
                m.insert(
                    "caller_qualified_name".into(),
                    Value::String(symbols.sym_qname(caller_sym).to_string()),
                );
                m.insert("caller_id".into(), Value::Number(edge.caller_id.into()));
                m.insert("callee_id".into(), Value::Number(edge.callee_id.into()));
                m.insert(
                    "caller_file".into(),
                    Value::String(
                        symbols
                            .file_rel_path(caller_sym.file_instance_id)
                            .to_string(),
                    ),
                );
                m.insert(
                    "caller_module".into(),
                    Value::String(symbols.sym_module(caller_sym).to_string()),
                );
                m.insert("call_line".into(), Value::Number(edge.call_line().into()));
                m.insert("is_cross_file".into(), Value::Bool(edge.is_cross_file()));
                callers.push(Value::Object(m));
            }
        }
        callers.sort_by(|left, right| {
            left["caller_file"]
                .as_str()
                .unwrap_or_default()
                .cmp(right["caller_file"].as_str().unwrap_or_default())
                .then_with(|| {
                    left["call_line"]
                        .as_u64()
                        .unwrap_or_default()
                        .cmp(&right["call_line"].as_u64().unwrap_or_default())
                })
        });
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
        let store = self.get_store(workspace_instance_id).ok_or_else(|| {
            DaemonRpcError::new(
                "snapshot_not_ready",
                format!("workspace {} 未发布 snapshot", workspace_instance_id),
            )
        })?;

        let symbols = store
            .symbols_table()
            .ok_or_else(|| DaemonRpcError::internal_error("symbols table not loaded"))?;
        let calls = store
            .call_graph()
            .ok_or_else(|| DaemonRpcError::internal_error("calls not loaded"))?;

        let caller_ids: Vec<u32> = if let Some(qname) = qualified_name {
            store
                .get_symbol_ref(qname)
                .map(|s| vec![s.id])
                .unwrap_or_default()
        } else {
            symbols.simple_name_ids(caller_name).to_vec()
        };

        let mut callees = Vec::new();
        for caller_id in caller_ids {
            let start = calls
                .forward_offsets
                .get(caller_id as usize)
                .copied()
                .unwrap_or(0);
            let end = calls
                .forward_offsets
                .get(caller_id as usize + 1)
                .copied()
                .unwrap_or(0);
            for edge in &calls.forward_edges[start..end] {
                let (qualified, file, module) = store
                    .get_symbol_by_id(edge.callee_id)
                    .map(|symbol| {
                        (
                            symbols.sym_qname(symbol),
                            symbols.file_rel_path(symbol.file_instance_id),
                            symbols.sym_module(symbol),
                        )
                    })
                    .unwrap_or(("", "", ""));
                let mut m = Map::new();
                m.insert(
                    "callee_name".into(),
                    Value::String(calls.callee_name(edge.callee_name_idx).to_string()),
                );
                m.insert(
                    "callee_qualified_name".into(),
                    Value::String(qualified.to_string()),
                );
                m.insert(
                    "callee_qualified".into(),
                    Value::String(qualified.to_string()),
                );
                m.insert("callee_id".into(), Value::Number(edge.callee_id.into()));
                m.insert("callee_file".into(), Value::String(file.to_string()));
                m.insert("callee_module".into(), Value::String(module.to_string()));
                m.insert("call_line".into(), Value::Number(edge.call_line().into()));
                m.insert("is_cross_file".into(), Value::Bool(edge.is_cross_file()));
                callees.push(Value::Object(m));
            }
        }
        callees.sort_by_key(|item| item["call_line"].as_u64().unwrap_or_default());
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
        // G12 批次8：修复 max_depth 字段错配（同 query.search limit）
        let max_depth = get_int_param_or(params, "max_depth", 5).max(0) as usize;

        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self.get_store(workspace_instance_id).ok_or_else(|| {
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
                m.insert("caller_qualified".into(), Value::String(e.caller_qualified));
                m.insert("callee_name".into(), Value::String(e.callee_name));
                m.insert("callee_qualified".into(), Value::String(e.callee_qualified));
                m.insert("callee_id".into(), Value::Number(e.callee_id.into()));
                m.insert("call_line".into(), Value::Number(e.call_line.into()));
                m.insert("is_cross_file".into(), Value::Bool(e.is_cross_file));
                Value::Object(m)
            })
            .collect();
        Ok(Value::Array(results))
    }

    fn handle_query_impact(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let symbol_hash = require_str_param(params, "symbol_hash")?;
        let requested_depth = get_int_param_or(params, "depth", 3);
        let workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let workspace_id = workspace
            .get("workspace_id")
            .and_then(Value::as_i64)
            .ok_or_else(|| {
                DaemonRpcError::internal_error("workspace_id 字段缺失或非数值".to_string())
            })?;
        let manager = self
            .get_snapshot_manager(workspace_instance_id)
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {} 未发布 snapshot", workspace_instance_id),
                )
            })?;
        let guard = manager.load();
        let snapshot = guard.as_ref().as_ref().ok_or_else(|| {
            DaemonRpcError::new(
                "snapshot_not_ready",
                format!("workspace {} 未发布 snapshot", workspace_instance_id),
            )
        })?;
        let db_path = snapshot.query_db_path();
        let conn = Connection::open_with_flags(
            &db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|error| {
            DaemonRpcError::internal_error(format!(
                "无法打开 snapshot SQLite {}: {}",
                db_path, error
            ))
        })?;
        query_impact_with_store(
            &conn,
            snapshot.store.as_ref(),
            workspace_id,
            symbol_hash,
            requested_depth,
        )
        .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_topological_order(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self.get_store(workspace_instance_id).ok_or_else(|| {
            DaemonRpcError::new(
                "snapshot_not_ready",
                format!("workspace {} 未发布 snapshot", workspace_instance_id),
            )
        })?;

        let limit = get_int_param_or(params, "limit", 20).max(0) as usize;
        let detail = params
            .get("detail")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let results = if detail {
            store
                .topological_details_rust(limit)
                .into_iter()
                .map(|symbol| {
                    json!({
                        "qualified_name": symbol.qualified_name,
                        "name": symbol.name,
                        "path": symbol.path,
                        "start_line": symbol.start_line,
                        "depth": symbol.depth,
                    })
                })
                .collect()
        } else {
            store
                .topological_order_rust()
                .into_iter()
                .take(limit)
                .map(Value::String)
                .collect()
        };
        Ok(Value::Array(results))
    }

    fn handle_query_detect_cycles(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let _workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let store = self.get_store(workspace_instance_id).ok_or_else(|| {
            DaemonRpcError::new(
                "snapshot_not_ready",
                format!("workspace {} 未发布 snapshot", workspace_instance_id),
            )
        })?;

        let cycles = store.detect_cycles_rust();
        let results: Vec<Value> = cycles
            .into_iter()
            .map(|c| Value::Array(c.into_iter().map(Value::String).collect()))
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

        let mgr = self
            .get_snapshot_manager(workspace_instance_id)
            .ok_or_else(|| {
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
        let (generation, source_db_path) = mgr.current_meta().unwrap_or((0, String::new()));

        let mut m = Map::new();
        m.insert(
            "workspace_instance_id".into(),
            Value::String(workspace_instance_id.to_string()),
        );
        m.insert("generation".into(), Value::Number(generation.into()));
        m.insert(
            "symbol_count".into(),
            Value::Number(health.symbol_count.into()),
        );
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
        m.insert(
            "history_len".into(),
            Value::Number(mgr.history_len().into()),
        );
        Ok(Value::Object(m))
    }

    fn handle_snapshot_list_workspaces(
        &mut self,
        peer: PeerCredential,
        _params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // P0-2 整改（2026-07-21）：按 peer_uid 过滤 workspace
        // 原实现忽略 _peer，返回 snapshot_cache 中全部 workspace，导致跨 UID 泄露。
        // 修复：先从 registry 拿到当前 peer UID 的 workspace_instance_id 集合，
        // 再对 snapshot_cache 返回的 ws_ids 做交集过滤。
        //
        // admin（peer.uid == 0 或 daemon uid）可以查看所有 workspace（运维监控场景）；
        // 非 admin 只能看自己的 workspace。
        let admin_view = peer.uid == 0 || peer.uid == current_daemon_uid();

        let ws_ids = self.snapshot_cache.list_workspaces();
        let mut entries = Vec::with_capacity(ws_ids.len());
        for ws_id in ws_ids {
            // 非 admin 校验 workspace 所有权
            if !admin_view {
                let workspace = self
                    .base
                    .registry
                    .get_workspace_status(&ws_id)
                    .map_err(|e| {
                        DaemonRpcError::internal_error(format!("registry 查询失败: {}", e))
                    })?;
                let owner_uid = workspace
                    .as_ref()
                    .and_then(|w| w.get("owner_uid"))
                    .and_then(|v| v.as_i64())
                    .unwrap_or(-1);
                if owner_uid != peer.uid as i64 {
                    // 不属于当前 UID 的 workspace 跳过
                    continue;
                }
            }
            if let Some(mgr) = self.snapshot_cache.get(&ws_id) {
                let mut m = Map::new();
                m.insert("workspace_instance_id".into(), Value::String(ws_id));
                m.insert(
                    "generation".into(),
                    Value::Number(mgr.current_generation().into()),
                );
                m.insert(
                    "history_len".into(),
                    Value::Number(mgr.history_len().into()),
                );
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

    // ============================================
    // G1 Layer 2: Toolchain / BuildContext / ResolvedEdges handlers
    // ============================================
    //
    // 实现原则：
    // - toolchain_store 未注入时返回 internal_error
    // - 参数校验与 Python daemon_server.py 对齐
    // - workspace_id 由调用方传入（来自 workspace.register 返回值）

    fn handle_toolchain_register(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let store = self.require_toolchain_store()?;
        let name = require_str_param(params, "name")?;
        let compiler_path = require_str_param(params, "compiler_path")?;
        let compiler_type = get_str_param_or(params, "compiler_type", "");
        let version = get_str_param_or(params, "version", "");
        let target_triple = get_str_param_or(params, "target_triple", "");
        let sysroot = get_str_param_or(params, "sysroot", "");
        let description = get_str_param_or(params, "description", "");

        // include_dirs: JSON array of strings
        let include_dirs: Vec<String> = params
            .get("include_dirs")
            .and_then(|v| serde_json::from_value(v.clone()).unwrap_or(None))
            .unwrap_or_default();
        // predefined_macros: JSON object → Vec<(K, V)>（保持顺序）
        let predefined_macros: Vec<(String, String)> = params
            .get("predefined_macros")
            .and_then(|v| v.as_object())
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_string()))
                    .collect()
            })
            .unwrap_or_default();
        // fingerprint：必填（调用方负责计算，与 Python compute_toolchain_fingerprint 一致）
        let fingerprint = require_str_param(params, "fingerprint")?;

        store
            .register_toolchain(
                name,
                compiler_path,
                &compiler_type,
                &version,
                &target_triple,
                &sysroot,
                &include_dirs,
                &predefined_macros,
                fingerprint,
                &description,
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("register_toolchain: {}", e)))
    }

    fn handle_toolchain_list(
        &mut self,
        _peer: PeerCredential,
        _params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let store = self.require_toolchain_store()?;
        store
            .list_toolchains()
            .map(Value::Array)
            .map_err(|e| DaemonRpcError::internal_error(format!("list_toolchains: {}", e)))
    }

    fn handle_toolchain_get(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let store = self.require_toolchain_store()?;
        let name_or_id = require_str_param(params, "name_or_id")?;
        match store
            .get_toolchain(name_or_id)
            .map_err(|e| DaemonRpcError::internal_error(format!("get_toolchain: {}", e)))?
        {
            Some(tc) => Ok(tc),
            None => Err(DaemonRpcError::method_not_found("toolchain not found")),
        }
    }

    fn handle_toolchain_delete(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let store = self.require_toolchain_store()?;
        let name_or_id = require_str_param(params, "name_or_id")?;
        let deleted = store
            .delete_toolchain(name_or_id)
            .map_err(|e| DaemonRpcError::internal_error(format!("delete_toolchain: {}", e)))?;
        let mut m = Map::new();
        m.insert("deleted".to_string(), Value::Number(deleted.into()));
        Ok(Value::Object(m))
    }

    fn handle_toolchain_bind(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let store = self.require_toolchain_store()?;
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        let toolchain_id = require_str_param(params, "toolchain_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("toolchain_id 必须是整数".to_string()))?;
        let build_context_hash = get_str_param_or(params, "build_context_hash", "");
        store
            .bind_toolchain_to_workspace(workspace_id, toolchain_id, &build_context_hash)
            .map(|_| {
                let mut m = Map::new();
                m.insert("status".to_string(), Value::String("ok".to_string()));
                Value::Object(m)
            })
            .map_err(|e| DaemonRpcError::internal_error(format!("bind_toolchain: {}", e)))
    }

    fn handle_toolchain_resolve(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        // P0-1 整改（2026-07-22）：先鉴权再访问资源，防止跨 UID 读取 toolchain 解析结果
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let build_context_hash = get_str_param(params, "build_context_hash");
        let result = store
            .resolve_toolchain(workspace_id, build_context_hash)
            .map_err(|e| DaemonRpcError::internal_error(format!("resolve_toolchain: {}", e)))?;
        Ok(match result {
            Some(tc) => tc,
            None => Value::Null,
        })
    }

    fn handle_toolchain_list_bound(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let build_context_hash = get_str_param(params, "build_context_hash");
        store
            .get_workspace_toolchains(workspace_id, build_context_hash)
            .map(Value::Array)
            .map_err(|e| DaemonRpcError::internal_error(format!("list_bound_toolchains: {}", e)))
    }

    fn handle_build_context_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let name = require_str_param(params, "name")?;
        let compile_flags: Vec<String> = params
            .get("compile_flags")
            .and_then(|v| serde_json::from_value(v.clone()).unwrap_or(None))
            .unwrap_or_default();
        let defines: Vec<(String, String)> = params
            .get("defines")
            .and_then(|v| v.as_object())
            .map(|m| {
                m.iter()
                    .map(|(k, v)| (k.clone(), v.as_str().unwrap_or("").to_string()))
                    .collect()
            })
            .unwrap_or_default();
        let include_paths: Vec<String> = params
            .get("include_paths")
            .and_then(|v| serde_json::from_value(v.clone()).unwrap_or(None))
            .unwrap_or_default();
        let set_active = params
            .get("set_active")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);

        store
            .register_build_context(
                workspace_id,
                name,
                &compile_flags,
                &defines,
                &include_paths,
                set_active,
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("register_build_context: {}", e)))
    }

    fn handle_build_context_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        // P0-1 整改（2026-07-22）：先鉴权再访问资源
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        store
            .list_build_contexts(workspace_id)
            .map(Value::Array)
            .map_err(|e| DaemonRpcError::internal_error(format!("list_build_contexts: {}", e)))
    }

    fn handle_build_context_get(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let build_context_hash = require_str_param(params, "build_context_hash")?;
        store
            .get_build_context(workspace_id, build_context_hash)
            .map(|value| value.unwrap_or(Value::Null))
            .map_err(|e| DaemonRpcError::internal_error(format!("get_build_context: {}", e)))
    }

    fn handle_build_context_set_active(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let build_context_hash = require_str_param(params, "build_context_hash")?;
        let ok = store
            .set_active_build_context(workspace_id, build_context_hash)
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("set_active_build_context: {}", e))
            })?;
        let mut m = Map::new();
        m.insert("ok".to_string(), Value::Bool(ok));
        Ok(Value::Object(m))
    }

    fn handle_build_context_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let build_context_hash = require_str_param(params, "build_context_hash")?;
        let deleted = store
            .delete_build_context(workspace_id, build_context_hash)
            .map_err(|e| DaemonRpcError::internal_error(format!("delete_build_context: {}", e)))?;
        let mut m = Map::new();
        m.insert("deleted".to_string(), Value::Number(deleted.into()));
        Ok(Value::Object(m))
    }

    fn handle_resolved_edges_store(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        // P0-1 整改（2026-07-22）：先鉴权再访问资源
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let build_context_hash = require_str_param(params, "build_context_hash")?;
        // edges: JSON array of edge objects
        let edges_arr = params
            .get("edges")
            .and_then(|v| v.as_array())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 edges 数组".to_string()))?;
        let edges = parse_resolved_edge_inputs(edges_arr)?;
        let inserted = store
            .store_resolved_edges(workspace_id, build_context_hash, &edges)
            .map_err(|e| DaemonRpcError::internal_error(format!("store_resolved_edges: {}", e)))?;
        let mut m = Map::new();
        m.insert("inserted".to_string(), Value::Number(inserted.into()));
        Ok(Value::Object(m))
    }

    fn handle_resolved_edges_get(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        // P0-1 整改（2026-07-22）：先鉴权再访问资源
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let build_context_hash = require_str_param(params, "build_context_hash")?;
        let caller_symbol_id = params.get("caller_symbol_id").and_then(|v| v.as_i64());
        let limit = params
            .get("limit")
            .and_then(|v| v.as_u64())
            .map(|n| n as usize);
        store
            .get_resolved_edges(workspace_id, build_context_hash, caller_symbol_id, limit)
            .map(Value::Array)
            .map_err(|e| DaemonRpcError::internal_error(format!("get_resolved_edges: {}", e)))
    }

    fn handle_resolved_edges_count(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        // P0-1 整改（2026-07-22）：先鉴权再访问资源
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let build_context_hash = require_str_param(params, "build_context_hash")?;
        let count = store
            .count_resolved_edges(workspace_id, build_context_hash)
            .map_err(|e| DaemonRpcError::internal_error(format!("count_resolved_edges: {}", e)))?;
        let mut m = Map::new();
        m.insert("count".to_string(), Value::Number(count.into()));
        Ok(Value::Object(m))
    }

    fn handle_resolved_edges_replace(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_id = require_str_param(params, "workspace_id")?
            .parse::<i64>()
            .map_err(|_| DaemonRpcError::invalid_params("workspace_id 必须是整数".to_string()))?;
        let _workspace = owned_workspace_by_id(&self.base.registry, peer.uid, workspace_id)?;
        let store = self.require_toolchain_store()?;
        let build_context_hash = require_str_param(params, "build_context_hash")?;
        let edges_arr = params
            .get("edges")
            .and_then(Value::as_array)
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少 edges 数组".to_string()))?;
        let edges = parse_resolved_edge_inputs(edges_arr)?;
        let (deleted, inserted) = store
            .replace_resolved_edges(workspace_id, build_context_hash, &edges)
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("replace_resolved_edges: {}", e))
            })?;
        let mut result = Map::new();
        result.insert("deleted".to_string(), Value::Number(deleted.into()));
        result.insert("inserted".to_string(), Value::Number(inserted.into()));
        Ok(Value::Object(result))
    }
}

// ============================================
// 辅助函数
// ============================================

fn parse_resolved_edge_inputs(
    edges: &[Value],
) -> Result<Vec<super::toolchain::ResolvedEdgeInput>, DaemonRpcError> {
    edges
        .iter()
        .enumerate()
        .map(|(index, edge)| {
            let caller_symbol_id = edge
                .get("caller_symbol_id")
                .and_then(Value::as_i64)
                .ok_or_else(|| {
                    DaemonRpcError::invalid_params(format!(
                        "edges[{index}].caller_symbol_id 缺失或不是整数"
                    ))
                })?;
            let callee_symbol_id = edge
                .get("callee_symbol_id")
                .and_then(Value::as_i64)
                .ok_or_else(|| {
                    DaemonRpcError::invalid_params(format!(
                        "edges[{index}].callee_symbol_id 缺失或不是整数"
                    ))
                })?;
            let callee_name = edge
                .get("callee_name")
                .and_then(Value::as_str)
                .ok_or_else(|| {
                    DaemonRpcError::invalid_params(format!(
                        "edges[{index}].callee_name 缺失或不是字符串"
                    ))
                })?;
            Ok(super::toolchain::ResolvedEdgeInput {
                caller_symbol_id,
                callee_symbol_id,
                callee_name: callee_name.to_string(),
                callee_file: edge
                    .get("callee_file")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
                call_line: edge.get("call_line").and_then(Value::as_i64).unwrap_or(0),
                resolution_method: edge
                    .get("resolution_method")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string(),
            })
        })
        .collect()
}

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
        let response = dispatch(state, make_peer(uid), "workspace.register", &params, &[]);
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
            response["result"]["snapshot_workspace_count"]
                .as_u64()
                .unwrap_or(0)
                == 0,
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
    fn test_query_symbol_returns_complete_snapshot_detail() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);
        let workspace = owned_workspace(&state.base.registry, 0, &ws_id).unwrap();
        let workspace_id = workspace["workspace_id"].as_i64().unwrap();
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("snapshot.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(&format!(
            "
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                abs_path TEXT NOT NULL,
                status TEXT NOT NULL
            );
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                module_path TEXT NOT NULL,
                visibility TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                depth INTEGER NOT NULL
            );
            CREATE TABLE calls (
                caller_id INTEGER NOT NULL,
                callee_id INTEGER NOT NULL,
                callee_name TEXT NOT NULL,
                call_line INTEGER NOT NULL,
                is_cross_file INTEGER NOT NULL
            );
            CREATE TABLE file_versions (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                is_current INTEGER NOT NULL
            );
            CREATE TABLE symbol_contents (
                content_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                signature TEXT,
                has_comment INTEGER,
                comment_content TEXT
            );
            CREATE TABLE file_symbol_versions (
                file_version_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                module_path TEXT,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                depth INTEGER NOT NULL,
                is_deleted INTEGER NOT NULL
            );
            CREATE TABLE call_versions (
                file_version_id INTEGER NOT NULL,
                caller_qualified TEXT NOT NULL,
                caller_hash TEXT,
                callee_name TEXT NOT NULL,
                callee_module TEXT,
                callee_qualified TEXT,
                callee_file TEXT,
                call_line INTEGER
            );
            INSERT INTO file_instances VALUES
                (1, {workspace_id}, 'a.py', '/repo/a.py', 'active');
            INSERT INTO symbols VALUES
                (1, 1, 'hash-alpha', 'fn', 'alpha', 'a.alpha', 'a', 'public', 1, 4, 0),
                (2, 1, 'hash-beta', 'fn', 'beta', 'a.beta', 'a', 'private', 6, 8, 0);
            INSERT INTO calls VALUES
                (1, 2, 'beta', 3, 0),
                (1, 0, 'external_api', 4, 1);
            INSERT INTO file_versions VALUES (10, 1, 1);
            INSERT INTO symbol_contents VALUES
                ('hash-alpha', 'alpha', 'fn', 'def alpha(): beta()', 'alpha()', 1, 'alpha docs'),
                ('hash-beta', 'beta', 'fn', 'SELECT * FROM orders; config.get(\"DB_URL\")', 'beta()', 0, '');
            INSERT INTO file_symbol_versions VALUES
                (10, 'hash-alpha', 'a.alpha', 'a', 1, 4, 0, 0),
                (10, 'hash-beta', 'a.beta', 'a', 6, 8, 0, 0);
            INSERT INTO call_versions VALUES
                (10, 'a.alpha', 'hash-alpha', 'beta', 'a', 'a.beta', 'a.py', 3);
            "
        ))
        .unwrap();
        drop(conn);

        state
            .snapshot_cache
            .get_or_create(&ws_id)
            .build_and_publish_blocking(db_path.to_str().unwrap(), workspace_id, "ctx", None)
            .unwrap();
        let response = dispatch(
            &mut state,
            peer,
            "query.symbol",
            &json!({
                "workspace_instance_id": ws_id,
                "qualified_name": "a.alpha"
            }),
            &[],
        );

        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["signature"], "alpha()");
        assert_eq!(response["result"]["comment_content"], "alpha docs");
        assert_eq!(response["result"]["calls_out"][0]["target_name"], "a.beta");
        assert_eq!(response["result"]["issues_total"], 0);

        let callers = dispatch(
            &mut state,
            peer,
            "query.callers",
            &json!({
                "workspace_instance_id": ws_id,
                "callee_name": "beta",
                "qualified_name": "a.beta"
            }),
            &[],
        );
        assert_eq!(callers["ok"], true);
        assert_eq!(callers["result"][0]["caller_name"], "alpha");
        assert_eq!(callers["result"][0]["caller_file"], "a.py");
        assert_eq!(callers["result"][0]["call_line"], 3);
        assert_eq!(callers["result"][0]["is_cross_file"], false);

        let callees = dispatch(
            &mut state,
            peer,
            "query.callees",
            &json!({
                "workspace_instance_id": ws_id,
                "caller_name": "alpha",
                "qualified_name": "a.alpha"
            }),
            &[],
        );
        assert_eq!(callees["ok"], true);
        assert_eq!(callees["result"].as_array().unwrap().len(), 2);
        assert_eq!(callees["result"][0]["callee_name"], "beta");
        assert_eq!(callees["result"][0]["callee_file"], "a.py");
        assert_eq!(callees["result"][0]["call_line"], 3);
        assert_eq!(callees["result"][1]["callee_name"], "external_api");
        assert_eq!(callees["result"][1]["callee_file"], "");
        assert_eq!(callees["result"][1]["call_line"], 4);
        assert_eq!(callees["result"][1]["is_cross_file"], true);

        let chain = dispatch(
            &mut state,
            peer,
            "query.call_chain_down",
            &json!({
                "workspace_instance_id": ws_id,
                "qualified_name": "a.alpha",
                "max_depth": 10
            }),
            &[],
        );
        assert_eq!(chain["ok"], true);
        assert_eq!(chain["result"][0]["caller_qualified"], "a.alpha");
        assert_eq!(chain["result"][0]["callee_qualified"], "a.beta");
        assert_eq!(chain["result"][1]["callee_qualified"], "");

        let topo = dispatch(
            &mut state,
            peer,
            "query.topological_order",
            &json!({
                "workspace_instance_id": ws_id,
                "limit": 1,
                "detail": true
            }),
            &[],
        );
        assert_eq!(topo["ok"], true);
        assert_eq!(topo["result"].as_array().unwrap().len(), 1);
        assert_eq!(topo["result"][0]["qualified_name"], "a.alpha");
        assert_eq!(topo["result"][0]["path"], "a.py");
        assert_eq!(topo["result"][0]["start_line"], 1);

        let impact = dispatch(
            &mut state,
            peer,
            "query.impact",
            &json!({
                "workspace_instance_id": ws_id,
                "symbol_hash": "hash-beta",
                "depth": 3
            }),
            &[],
        );
        assert_eq!(impact["ok"], true);
        assert_eq!(impact["result"]["source_symbol"], "a.beta");
        assert_eq!(impact["result"]["total_impacted"], 2);
        assert_eq!(
            impact["result"]["layers"][1]["symbols"][0]["qualified_name"],
            "a.alpha"
        );
        assert_eq!(impact["result"]["by_layer"]["code"], 1);
        assert_eq!(impact["result"]["by_layer"]["db"], 1);
        assert_eq!(impact["result"]["by_layer"]["config"], 1);
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

    // ---- P0-1 整改（2026-07-22）：toolchain/build_context/resolved_edges 跨 UID ACL ----

    /// 注册 workspace 并返回 (workspace_instance_id, numeric_workspace_id)
    fn register_workspace_with_numeric_id(
        state: &mut SnapshotDaemonState,
        uid: u32,
    ) -> (String, i64) {
        let tmp = tempfile::tempdir().unwrap();
        let dir_path = tmp.path().to_str().unwrap().to_string();
        let params = json!({
            "client_view_root": dir_path,
            "git_remote_url": "https://example.com/test.git",
            "git_head_commit_sha": "abc123",
            "toolchain_fingerprint": "test"
        });
        let response = dispatch(state, make_peer(uid), "workspace.register", &params, &[]);
        assert_eq!(response["ok"], true);
        let ws_instance_id = response["result"]["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string();
        let ws_num_id = response["result"]["workspace_id"]
            .as_i64()
            .expect("register 响应应包含数字 workspace_id");
        (ws_instance_id, ws_num_id)
    }

    /// P0-1：toolchain.resolve 跨 UID 调用应返回 workspace_forbidden
    /// （ACL 校验在 require_toolchain_store 之前，所以无需注入 ToolchainStore）
    #[test]
    fn test_toolchain_resolve_rejects_non_owner() {
        let mut state = make_state();
        let owner_uid = 0; // root 注册，绕过 Unix 路径 ACL
        let (_ws_instance, ws_num_id) = register_workspace_with_numeric_id(&mut state, owner_uid);

        // 非 owner 调用 toolchain.resolve → workspace_forbidden
        let other_peer = make_peer(9999);
        let response = dispatch(
            &mut state,
            other_peer,
            "toolchain.resolve",
            &json!({"workspace_id": ws_num_id.to_string()}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    /// P0-1：toolchain.resolve 不存在的 workspace_id → workspace_not_found
    #[test]
    fn test_toolchain_resolve_rejects_nonexistent_workspace() {
        let mut state = make_state();
        let peer = make_peer(0);

        let response = dispatch(
            &mut state,
            peer,
            "toolchain.resolve",
            &json!({"workspace_id": "99999"}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_not_found");
    }

    /// P0-1：toolchain.resolve owner 调用通过 ACL 后，因无 ToolchainStore 返回 internal_error
    /// （证明 owner 不被 ACL 拦截，仅受资源可用性约束）
    #[test]
    fn test_toolchain_resolve_owner_passes_acl() {
        let mut state = make_state();
        let owner_uid = 0;
        let (_ws_instance, ws_num_id) = register_workspace_with_numeric_id(&mut state, owner_uid);

        let response = dispatch(
            &mut state,
            make_peer(owner_uid),
            "toolchain.resolve",
            &json!({"workspace_id": ws_num_id.to_string()}),
            &[],
        );
        assert_eq!(response["ok"], false);
        // owner 通过 ACL，但无 ToolchainStore → internal_error（非 workspace_forbidden）
        assert_eq!(response["error"]["code"], "internal_error");
    }

    /// P0-1：build_context.list 跨 UID 调用应返回 workspace_forbidden
    #[test]
    fn test_build_context_list_rejects_non_owner() {
        let mut state = make_state();
        let owner_uid = 0;
        let (_ws_instance, ws_num_id) = register_workspace_with_numeric_id(&mut state, owner_uid);

        let other_peer = make_peer(9999);
        let response = dispatch(
            &mut state,
            other_peer,
            "build_context.list",
            &json!({"workspace_id": ws_num_id.to_string()}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_build_context_writes_and_new_reads_use_workspace_owner_acl() {
        let mut state = make_state();
        let owner_uid = 0;
        let (_ws_instance, ws_num_id) = register_workspace_with_numeric_id(&mut state, owner_uid);
        let other_peer = make_peer(9999);
        let workspace_id = ws_num_id.to_string();
        let requests = [
            (
                "toolchain.list_bound",
                json!({"workspace_id": workspace_id}),
            ),
            (
                "build_context.get",
                json!({"workspace_id": workspace_id, "build_context_hash": "ctx"}),
            ),
            (
                "build_context.register",
                json!({"workspace_id": workspace_id, "name": "debug"}),
            ),
            (
                "build_context.set_active",
                json!({"workspace_id": workspace_id, "build_context_hash": "ctx"}),
            ),
            (
                "build_context.delete",
                json!({"workspace_id": workspace_id, "build_context_hash": "ctx"}),
            ),
            (
                "resolved_edges.replace",
                json!({
                    "workspace_id": workspace_id,
                    "build_context_hash": "ctx",
                    "edges": []
                }),
            ),
        ];
        for (method, params) in requests {
            let response = dispatch(&mut state, other_peer, method, &params, &[]);
            assert_eq!(
                response["error"]["code"], "workspace_forbidden",
                "{method} 必须在访问 toolchain store 之前拒绝非 owner"
            );
        }
    }

    /// P0-1：resolved_edges.store 跨 UID 调用应返回 workspace_forbidden
    #[test]
    fn test_resolved_edges_store_rejects_non_owner() {
        let mut state = make_state();
        let owner_uid = 0;
        let (_ws_instance, ws_num_id) = register_workspace_with_numeric_id(&mut state, owner_uid);

        let other_peer = make_peer(9999);
        let response = dispatch(
            &mut state,
            other_peer,
            "resolved_edges.store",
            &json!({
                "workspace_id": ws_num_id.to_string(),
                "build_context_hash": "test",
                "edges": []
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    /// P0-1：resolved_edges.get 跨 UID 调用应返回 workspace_forbidden
    #[test]
    fn test_resolved_edges_get_rejects_non_owner() {
        let mut state = make_state();
        let owner_uid = 0;
        let (_ws_instance, ws_num_id) = register_workspace_with_numeric_id(&mut state, owner_uid);

        let other_peer = make_peer(9999);
        let response = dispatch(
            &mut state,
            other_peer,
            "resolved_edges.get",
            &json!({
                "workspace_id": ws_num_id.to_string(),
                "build_context_hash": "test"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    /// P0-1：resolved_edges.count 跨 UID 调用应返回 workspace_forbidden
    #[test]
    fn test_resolved_edges_count_rejects_non_owner() {
        let mut state = make_state();
        let owner_uid = 0;
        let (_ws_instance, ws_num_id) = register_workspace_with_numeric_id(&mut state, owner_uid);

        let other_peer = make_peer(9999);
        let response = dispatch(
            &mut state,
            other_peer,
            "resolved_edges.count",
            &json!({
                "workspace_id": ws_num_id.to_string(),
                "build_context_hash": "test"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
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

        let response = dispatch(&mut state, peer, "gc.cas", &json!({"grace_days": 7}), &[]);
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

    #[test]
    fn test_workspace_file_delete_delegated_to_base() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.delete",
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
    fn test_query_impact_returns_snapshot_not_ready_when_no_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);
        let response = dispatch(
            &mut state,
            peer,
            "query.impact",
            &json!({
                "workspace_instance_id": ws_id,
                "symbol_hash": "hash-a"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "snapshot_not_ready");
    }

    #[test]
    fn test_query_impact_rejects_non_owner() {
        let mut state = make_state();
        let ws_id = register_workspace_for_test(&mut state, 0);
        let response = dispatch(
            &mut state,
            make_peer(9999),
            "query.impact",
            &json!({
                "workspace_instance_id": ws_id,
                "symbol_hash": "hash-a"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
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
        // snapshot.evict 在 ADMIN_ONLY_METHODS 中，非 admin 在 dispatch 层
        // 就被拒绝（permission_denied），不会进入 handler 的 workspace ACL 检查
        assert_eq!(response["error"]["code"], "permission_denied");
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
