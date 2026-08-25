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

use std::collections::{BTreeMap, HashMap, HashSet};
use std::sync::Arc;

use regex::Regex;
use rusqlite::{params, params_from_iter, Connection, OpenFlags, OptionalExtension};
use serde_json::{json, Map, Value};

use super::dispatch::{
    current_daemon_uid, get_int_param, get_int_param_or, get_str_param, get_str_param_or,
    require_str_param, DaemonRpcError, DaemonState, DaemonStateExt, PeerCredential,
};
use super::workspace::{
    normalize_path_key, owned_workspace, owned_workspace_by_id, validate_owned_path,
    WorkspaceDaemonState, WorkspaceRegistry,
};
use crate::cli::file_query::{query_local_file_symbols, query_local_symbol_location};
use crate::cli::grep::{query_local_grep, GrepOptions};
use crate::cli::impact::query_impact_with_store;
use crate::cli::issues_tests::{
    query_local_issues, query_local_test_cases, query_local_test_stability,
    query_local_tested_functions,
};
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

    /// SRV-002：设置审计日志 DB 路径，透传到 base `WorkspaceDaemonState`。
    ///
    /// 空表示未配置（fail-closed 由 `audit_log_handlers` 兜底）。
    pub fn with_audit_db_path(mut self, path: std::path::PathBuf) -> Self {
        self.base = self.base.with_audit_db_path(path);
        self
    }

    /// 注入 TaskCollabStore 协同存储（用于 task.create / task.claim 等协同 RPC）
    pub fn with_task_collab_store(
        mut self,
        store: Arc<super::task_collab::TaskCollabStore>,
    ) -> Self {
        self.base = self.base.with_task_collab_store(store);
        self
    }

    /// 注入 task_loop control-plane（1D3B），透传到 base `WorkspaceDaemonState`。
    /// 未注入时 `task_loop.public_promote` 与公共路由 fail-closed。
    pub fn with_task_loop_control(
        mut self,
        gate: Arc<super::task_loop::capability_control::CapabilityMutationGate>,
        daemon_generation: u64,
    ) -> Self {
        self.base = self.base.with_task_loop_control(gate, daemon_generation);
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

    fn open_query_connection(
        &self,
        peer: PeerCredential,
        workspace_instance_id: &str,
    ) -> Result<(i64, Connection), DaemonRpcError> {
        let workspace = owned_workspace(&self.base.registry, peer.uid, workspace_instance_id)?;
        let workspace_id = workspace
            .get("workspace_id")
            .and_then(Value::as_i64)
            .ok_or_else(|| DaemonRpcError::internal_error("workspace_id 字段缺失或非数值"))?;
        let db_path = self
            .get_snapshot_manager(workspace_instance_id)
            .and_then(|manager| manager.current_query_db_path())
            .ok_or_else(|| {
                DaemonRpcError::new(
                    "snapshot_not_ready",
                    format!("workspace {workspace_instance_id} 未发布 snapshot"),
                )
            })?;
        let conn = Connection::open_with_flags(
            db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|error| {
            DaemonRpcError::internal_error(format!("打开 snapshot SQLite: {error}"))
        })?;
        Ok((workspace_id, conn))
    }

    /// W3-1（T-1786861820150-bfe5e805）：校验 params 中的 `workspace_id` 与
    /// workspace_instance_id 解析出的权威 workspace_id 一致，返回权威值。
    ///
    /// build 读组 5 工具的 Python 签名显式携带 `workspace_id`（MCP 参数），
    /// 而 Rust handler 经 open_query_connection（owned_workspace ACL）解析出的
    /// workspace_id 才是权威值。二者不一致时 fail-closed 拒绝（invalid_params），
    /// 防止跨 workspace 越权读取。
    fn require_bound_workspace_id(
        &self,
        params: &Value,
        workspace_id: i64,
    ) -> Result<i64, DaemonRpcError> {
        let param_workspace_id = get_int_param(params, "workspace_id")
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少字段: workspace_id".to_string()))?;
        if param_workspace_id != workspace_id {
            return Err(DaemonRpcError::invalid_params(format!(
                "workspace_id {} 与 workspace_instance_id 绑定的 {} 不一致",
                param_workspace_id, workspace_id
            )));
        }
        Ok(param_workspace_id)
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

    fn handle_workspace_refresh_plan(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_refresh_plan(peer, params)
    }

    fn handle_workspace_file_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_file_delete(peer, params)
    }

    fn handle_workspace_activate(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_activate(peer, params)
    }

    fn handle_workspace_remove(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        self.base.handle_workspace_remove(peer, params)
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

        // W1-4-FIX：registry ROWID 仅作 fallback 主键（真实过滤 id 见下方
        // 真相源解析）。P0-2 整改要求提取数值 workspace_id 传入
        // build_and_publish_blocking 用于 GraphStore SQL 层过滤，避免 snapshot
        // 混入其他 workspace 数据。
        let registry_rowid: i64 = workspace
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| {
                DaemonRpcError::internal_error("workspace_id 字段缺失或非数值".to_string())
            })?;
        // W1-4-FIX：client_view_root 用于与真相源 workspaces.root_path 匹配
        let client_view_root = workspace
            .get("client_view_root")
            .and_then(|v| v.as_str())
            .unwrap_or("");

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

        // W1-4-FIX：从真相源（db_path 的 workspaces 表）解析真实 workspace id。
        //
        // daemon_workspaces.workspace_id 是 INTEGER PRIMARY KEY AUTOINCREMENT，
        // register_workspace 用 INSERT OR REPLACE（SQLite delete+insert）导致每次
        // 重复 register ROWID 轮转递增，与 Python 侧 file_instances.workspace_id
        // （= workspaces.id，root_path 唯一匹配）不一致。若直接用 registry ROWID
        // 作为 GraphStore SQL 过滤值，真实用户库 publish 后快照为空（syms=0）。
        //
        // 此处只读打开 db_path 查 workspaces 表，按规范化后的 client_view_root
        // 匹配 root_path 取真实 id；查不到（表不存在/无匹配/打开失败）时 fallback
        // registry ROWID，保持 P0-2 隔离语义现状不更糟，不影响既有测试。
        // FD 分支（Linux /proc/self/fd/N）与 db_path 分支统一走同一条解析路径，
        // 打开失败同样 fallback，不 panic。
        let workspace_id_num =
            resolve_true_workspace_id(&db_path, client_view_root, registry_rowid);

        // 获取或创建 SnapshotManager
        let mgr = self.snapshot_cache.get_or_create(workspace_instance_id);

        // 构建 + 发布（传入 workspace_id_num 用于 SQL 过滤）
        //
        // T-1785854423993：cw-daemon 未嵌入 Python 解释器，build_and_publish_blocking
        // 的错误路径若以 PyErr 构造会 panic。T-1786574299601 已把内部加载错误改为
        // String 传播（graph.rs/snapshot.rs），此处 Err(String) 携带真实失败原因，
        // 直接输出不再被掩盖。
        let publish_outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            mgr.build_and_publish_blocking(
                &db_path,
                workspace_id_num,
                &build_context_hash,
                snapshot_id,
            )
        }));
        let (generation, symbol_count, call_count) = match publish_outcome {
            Ok(Ok(v)) => v,
            Ok(Err(msg)) => {
                // 真实错误消息以 String 传播（不再构造 PyErr），直接输出定位真实原因。
                return Err(DaemonRpcError::internal_error(format!(
                    "build_and_publish 失败: {}",
                    msg
                )));
            }
            Err(_) => {
                return Err(DaemonRpcError::internal_error(
                    "snapshot.publish panic: daemon 未嵌入 Python 解释器，Python 依赖的 \
                     publish 路径不可用（请求已隔离）"
                        .to_string(),
                ));
            }
        };

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

    // ---- W2-1（T-1786840097330-dec66710）：query 面 stats HTTP native 迁移 ----
    // 三个工具（get_uncommented_symbols / get_module_call_stats / get_semgrep_stats）
    // 从 python_compat（compat worker）迁移为 rust_native。数据源经 snapshot
    // query_db_path（主库只读连接，与 handle_query_symbol / handle_query_issues
    // 同构）访问主库全表（file_symbol_versions / call_versions / semgrep_findings），
    // snapshot_not_ready 保护 + owned_workspace ACL 与既有 query.* 完全一致。
    // 语义逐条复刻 Python db 层（analyzers/coverage.py / call_chain.py / issues.py）。

    fn handle_query_uncommented_symbols(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let kind = get_str_param_or(params, "kind", "fn");
        let module_filter = get_str_param_or(params, "module_filter", "");
        // limit 语义对齐 Python 工具层 `[:limit]`：limit=0 返回空数组；
        // 负数视为越界参数 fail-closed（invalid_params）。
        let limit = get_int_param_or(params, "limit", 100);
        if limit < 0 {
            return Err(DaemonRpcError::invalid_params("limit 不能为负数"));
        }
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_uncommented_symbols(&conn, workspace_id, &kind, &module_filter, limit as usize)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_module_call_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // limit 语义对齐 Python 工具层 `results[:limit]`：limit=0 返回空数组；
        // 负数视为越界参数 fail-closed（invalid_params）。
        let limit = get_int_param_or(params, "limit", 30);
        if limit < 0 {
            return Err(DaemonRpcError::invalid_params("limit 不能为负数"));
        }
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_module_call_stats(&conn, workspace_id, limit as usize)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_semgrep_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_semgrep_stats(&conn, workspace_id).map_err(DaemonRpcError::internal_error)
    }

    // W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 从 python_compat
    // （compat worker）迁移为 rust_native。数据源（semgrep_findings JOIN
    // file_instances）在主库，经 snapshot query_db_path（主库只读连接，
    // open_query_connection 同构 W2-1 query.semgrep_stats）访问；无需扩展
    // snapshot schema。snapshot_not_ready 保护 + owned_workspace ACL 与既有
    // query.* 完全一致。隔离机制：semgrep_findings 表**无 workspace_id 列**，
    // 与 query_local_semgrep_stats 同构，经 `JOIN file_instances fi ON
    // sf.file_instance_id = fi.id` + `WHERE fi.workspace_id = ?` 实现跨
    // workspace 隔离（不调用 require_bound_workspace_id——工具签名无
    // workspace_id 参数，同 W2-1 get_semgrep_stats）。语义逐条复刻 Python
    // db 层 `get_semgrep_findings`（analyzers/issues.py）：
    // - severity 过滤（upper()）、language 精确过滤、rule_id 模糊 LIKE 匹配；
    // - 排序：`sf.severity = 'ERROR' DESC, sf.severity = 'WARNING' DESC,
    //   sf.id DESC`，LIMIT 截断（limit=0 → 空数组，limit<0 → invalid_params）。

    fn handle_query_semgrep_findings(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // 越界参数 fail-closed：limit<0 → invalid_params（对齐 W3-2 list_jobs）；
        // limit=0 → 空数组（SQL LIMIT 0 语义，与 Python 工具层一致）。
        let limit = get_int_param_or(params, "limit", 50);
        if limit < 0 {
            return Err(DaemonRpcError::invalid_params(
                "limit 不能为负数".to_string(),
            ));
        }
        // 空字符串 → 不过滤（复刻 Python `if severity: / if language: / if rule_id:`）
        let severity = get_str_param_or(params, "severity", "");
        let language = get_str_param_or(params, "language", "");
        let rule_id = get_str_param_or(params, "rule_id", "");
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_semgrep_findings(
            &conn,
            workspace_id,
            &severity,
            &language,
            &rule_id,
            limit as usize,
        )
        .map_err(DaemonRpcError::internal_error)
    }

    // W4-1（T-1786886251769-22b94ee8-sub-1）：git 读组 5 工具从 python_compat
    // （compat worker）迁移为 rust_native（query.file_history /
    // query.git_commits / query.git_commit_changes / query.git_stats /
    // query.commit_tasks）。数据源（file_versions + file_instances / git_commits /
    // git_file_changes / task_symbol_changes + tasks）均在主库，经 snapshot
    // query_db_path（主库只读连接，open_query_connection 同构 W3-3
    // query.semgrep_findings）访问；无需扩展 snapshot schema。
    // snapshot_not_ready 保护 + owned_workspace ACL 与既有 query.* 完全一致。
    // 语义逐条复刻 Python db 层（db_query.py get_file_history / db_git.py
    // get_git_commits/get_commit_changes/get_git_stats / db_task_attribution.py
    // get_commit_tasks）。workspace 隔离语义（schema 核实结论）：
    // - git_commits 表有 workspace_id 列 → 直接 WHERE 过滤；
    // - git_file_changes 表无 workspace_id 列，但其 commit_hash 为全局唯一
    //   （TEXT UNIQUE），第一段先按 git_commits.workspace_id 确认 commit 归属
    //   后，第二段按 commit_hash 查询不跨 workspace（与 Python 两段式同构）；
    // - file_history 经 JOIN file_instances WHERE fi.workspace_id 隔离；
    // - commit_tasks 复刻 Python 全局查询（task_symbol_changes 无 workspace
    //   维度，task_id 全局唯一）。
    // get_file_history 的路径归一化（绝对路径 → norm_path(relpath(file_path,
    // workspace_root))）保留在 Python 工具层（与 db 层/compat worker 同源，
    // workspaces.root_path 为真相源；daemon 侧 client_view_root 与之不同源），
    // Rust 侧只按最终 rel_path 精确匹配（rel_path 仅用于 SQL 等值匹配，
    // 不触文件系统，无路径穿越风险）。

    fn handle_query_file_history(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let file_path = require_str_param(params, "file_path")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_file_history(&conn, workspace_id, &file_path)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_git_commits(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // 越界参数 fail-closed：limit/offset<0 → invalid_params（对齐 W3-2
        // list_jobs 的 limit 校验；Python db 层无校验，负值被 SQLite 静默
        // 吞掉，native 侧显式拒绝）。limit=0 → 空数组（SQL LIMIT 0 语义）。
        let limit = get_int_param_or(params, "limit", 20);
        if limit < 0 {
            return Err(DaemonRpcError::invalid_params(
                "limit 不能为负数".to_string(),
            ));
        }
        let offset = get_int_param_or(params, "offset", 0);
        if offset < 0 {
            return Err(DaemonRpcError::invalid_params(
                "offset 不能为负数".to_string(),
            ));
        }
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_git_commits(&conn, workspace_id, limit as usize, offset as usize)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_git_commit_changes(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let commit_hash = require_str_param(params, "commit_hash")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_git_commit_changes(&conn, workspace_id, &commit_hash)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_git_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_git_stats(&conn, workspace_id).map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_commit_tasks(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // 复刻 Python：空 commit_hash → 空数组（`if not commit_hash: return []`）
        let commit_hash = get_str_param_or(params, "commit_hash", "");
        if commit_hash.is_empty() {
            return Ok(Value::Array(vec![]));
        }
        // include_task_details 缺省 True（复刻 Python 工具签名默认值）
        let include_task_details = params
            .get("include_task_details")
            .and_then(Value::as_bool)
            .unwrap_or(true);
        let (_workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_commit_tasks(&conn, &commit_hash, include_task_details)
            .map_err(DaemonRpcError::internal_error)
    }

    // W4-2（T-1786886251769-22b94ee8-sub-2）：coverage/review 读组迁移。
    // get_coverage_for_symbol / diff_to_symbol 从 python_compat（compat worker）
    // 迁移为 rust_native（query.coverage_for_symbol / query.diff_to_symbol）。
    // 数据源（symbols + file_instances / coverage_data）均在主库，经 snapshot
    // query_db_path（主库只读连接，open_query_connection 同构 W4-1）访问；
    // 无需扩展 snapshot schema。snapshot_not_ready 保护 + owned_workspace ACL
    // 与既有 query.* 完全一致。语义逐条复刻 Python db 层（db_coverage.py
    // get_coverage_for_symbol / db_impact.py diff_to_symbol）。
    // workspace 隔离语义（schema 核实结论）：symbols / coverage_data 均无
    // workspace_id 列，两段式查询第一段均经 JOIN file_instances
    // WHERE fi.workspace_id 限定（与 W3-3 semgrep_findings 同构）；
    // coverage_data 第二段按 symbol_id 查询，symbol_id 为全局主键，不跨
    // workspace。review_readiness 依赖 blast_radius 与 cross_layer_impact
    // （均未迁移），保持 python_compat（决策见 ledger §9.23）。
    // diff_to_symbol 参数为原始 diff 文本（MCP 参数直传，无路径归一化），
    // Rust 侧按 Python 逐行状态机复刻（4 个 regex + 显式 DiffParseState）。

    fn handle_query_coverage_for_symbol(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let qualified_name = require_str_param(params, "qualified_name")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_coverage_for_symbol(&conn, workspace_id, qualified_name)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_diff_to_symbol(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let diff_text = require_str_param(params, "diff_text")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_diff_to_symbol(&conn, workspace_id, diff_text)
            .map_err(DaemonRpcError::internal_error)
    }

    // W4-3（T-1786886251769-22b94ee8-sub-3）：defect 读组迁移。
    // defect_correlation / churn_analysis / defect_search / defect_suggest_fix
    // / get_defect_correlation 从 python_compat（compat worker）迁移为
    // rust_native（query.defect_correlation / query.churn_analysis /
    // query.defect_search / query.defect_suggest_fix /
    // query.get_defect_correlation）。数据源（symbol_contents /
    // file_symbol_versions / file_versions / file_instances / semgrep_findings
    // / git_file_changes / git_commits / defect_patterns / defect_fixes）均在
    // 主库，经 snapshot query_db_path（主库只读连接，open_query_connection
    // 同构 W4-1/W4-2）访问；无需扩展 snapshot schema。snapshot_not_ready
    // 保护 + owned_workspace ACL 与既有 query.* 完全一致。语义逐条复刻
    // Python db 层（db_evolution.py defect_correlation /
    // get_defect_correlation_by_qn / churn_analysis + db_defect_kb.py
    // defect_pattern_search / suggest_fix）。defect_learn 为写操作
    // （INSERT/UPDATE defect_patterns/defect_fixes），不迁移 rust_native
    // （决策见 ledger §9.24），保持 python_compat。

    fn handle_query_defect_correlation(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let symbol_hash = require_str_param(params, "symbol_hash")?;
        // window_commits 负数 Python 语义为空窗口（切片 idx+1:idx+1+n 为空，
        // 不报错），Rust 复刻该语义不拒绝（见 query_local_defect_correlation）
        let window_commits = get_int_param_or(params, "window_commits", 5);
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_defect_correlation(&conn, workspace_id, symbol_hash, window_commits)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_churn_analysis(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let module_filter = get_str_param_or(params, "module_filter", "");
        let time_window = get_str_param_or(params, "time_window", "90d");
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_churn_analysis(&conn, workspace_id, &module_filter, &time_window)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_defect_search(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let category = get_str_param_or(params, "category", "");
        let severity_filter = get_str_param_or(params, "severity_filter", "");
        // defect_patterns 无 workspace_id 列（全局知识库），workspace 隔离
        // 由连接级 ACL（owned_workspace + snapshot query_db_path）保证
        let (_workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_defect_search(&conn, &category, &severity_filter)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_defect_suggest_fix(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let symbol_hash = require_str_param(params, "symbol_hash")?;
        let finding_id = get_int_param_or(params, "finding_id", 0);
        let (_workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_defect_suggest_fix(&conn, symbol_hash, finding_id)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_get_defect_correlation(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let qualified_name = require_str_param(params, "qualified_name")?;
        let window_commits = get_int_param_or(params, "window_commits", 5);
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_get_defect_correlation(&conn, workspace_id, qualified_name, window_commits)
            .map_err(DaemonRpcError::internal_error)
    }

    // W4-4（T-1786886251769-22b94ee8-sub-4）：分支差异 diff_branches 迁移。
    // diff_branches 从 python_compat（compat worker）迁移为 rust_native
    // （query.diff_branches）。语义真相源 db/db_branch.py L179-252（纯 SELECT）。
    // 关键语义：按分支名（workspace name）精确匹配（`WHERE name = ?`，无大小写
    // 折叠）查 source/target 两个 workspace（复刻 _find_workspace_by_name，
    // 取首行），任一不存在 → 返回 {"error": "源分支不存在: ..."} / {"error":
    // "目标分支不存在: ..."}（正常响应体，非 RPC 错误）；分别加载两 workspace
    // 的 symbols（复刻 _load_workspace_symbols：SELECT s.symbol_hash,
    // s.qualified_name, s.name, s.kind, s.module_path, fi.rel_path FROM
    // symbols s JOIN file_instances fi ... WHERE fi.workspace_id = ?，跳过空
    // qualified_name，按 qn 索引），按 qualified_name 对比 symbol_hash：
    // added（target 有 source 无，含 target 侧 symbol_hash/name/kind）/
    // removed（source 有 target 无，含 source 侧字段）/ modified（两边都有
    // hash 不同，含 source_hash/target_hash/name/kind）/ unchanged_count
    // （hash 相同计数）。列表顺序 = 各 workspace 的 SELECT 行序（Python dict
    // 插入序 = SQLite 行序，无 ORDER BY；Rust 用 Vec 保序 + HashMap 索引，
    // 重复 qn 覆盖值不改变位置，复刻 Python dict 语义）。
    // 跨 workspace 读取：source/target 是两个不同 workspace，但均位于 peer
    // 合法可访问的 snapshot 库（snapshot.publish 发布 client 整个主库副本，
    // 含全部 workspace 数据），workspace_instance_id 仅用于连接级 ACL
    // （owned_workspace + snapshot query_db_path，同 W4-3 defect_search
    // 全局视图模式）。空 branch 名 Python 语义为查不到 → error dict（不抛
    // 异常），Rust 复刻不拒绝。fail-closed：HTTP 失败原样传播不回退本地 SQL。

    fn handle_query_diff_branches(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let source_branch = require_str_param(params, "source_branch")?;
        let target_branch = require_str_param(params, "target_branch")?;
        let (_workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_diff_branches(&conn, source_branch, target_branch)
            .map_err(DaemonRpcError::internal_error)
    }

    // ---- W2-2（T-1786840097330-a9e0ec69）：task 面 stats HTTP native 迁移 ----
    // 三个工具（get_clone_stats / get_job_stats / get_clone_group_stats）
    // 从 python_compat（compat worker）迁移为 rust_native。数据源
    // （clone_pairs / clone_groups / clone_group_members / jobs / symbols /
    // file_instances）均在主库，经 snapshot query_db_path（主库只读连接，
    // open_query_connection 同构 W2-1）访问；无需扩展 snapshot schema。
    // snapshot_not_ready 保护 + owned_workspace ACL 与既有 query.* 完全一致。
    // 语义逐条复刻 Python db 层（db_clone_detection.py / db_jobs.py /
    // db_clone_groups.py），含 SUM 可能为 NULL 的 `or 0` 语义。

    fn handle_task_clone_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_clone_stats(&conn, workspace_id).map_err(DaemonRpcError::internal_error)
    }

    fn handle_task_job_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_job_stats(&conn, workspace_id).map_err(DaemonRpcError::internal_error)
    }

    // W3-2（T-1786861820151-f3cecf40）：job 读组 3 工具 HTTP native 迁移。
    // get_job_status / list_jobs / wait_for_job 从 python_compat（compat worker）
    // 迁移为 rust_native。数据源（jobs 表）在主库，经 snapshot query_db_path
    // （主库只读连接，open_query_connection 同构 W2-2 task.job_stats）访问；
    // 无需扩展 snapshot schema。snapshot_not_ready 保护 + owned_workspace ACL
    // 与既有 query.* 完全一致。语义逐条复刻 Python db 层（db_jobs.py）：
    // - job_status：按 job_id + workspace_id 查询单行，返回 Job.to_dict()
    //   （asdict 全字段），找不到 → {"error": "job not found: <job_id>"}；
    // - list_jobs：WHERE workspace_id [+ job_type] [+ status] ORDER BY
    //   created_at DESC LIMIT ?，limit<0 fail-closed invalid_params；
    // - wait_for_job：复刻 Python 轮询循环（deadline 内查询 jobs 表，终态即
    //   返回 {job_id,status,progress,result_summary,error,elapsed}，否则 sleep
    //   poll_interval；超时返回 status="timeout"）。
    // 三个查询均限定 workspace_id 实现跨 workspace 隔离（job 属于其他
    // workspace → not found，fail-closed；与 list_jobs 的隔离语义一致）。

    fn handle_task_job_status(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let job_id = require_str_param(params, "job_id")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        match query_local_get_job(&conn, workspace_id, job_id)
            .map_err(DaemonRpcError::internal_error)?
        {
            Some(job) => Ok(job),
            None => Ok(json!({"error": format!("job not found: {job_id}")})),
        }
    }

    fn handle_task_list_jobs(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // 越界参数 fail-closed：limit<0 → invalid_params（对齐 W3-1 build
        // resolved_edges 的 limit 校验）；limit=0 返回空数组（SQL LIMIT 0 语义）
        let limit = get_int_param_or(params, "limit", 100);
        if limit < 0 {
            return Err(DaemonRpcError::invalid_params(
                "limit 不能为负数".to_string(),
            ));
        }
        // Python 工具层 `job_type or None` / `status or None`：空字符串 → 不过滤
        let job_type = get_str_param(params, "job_type").filter(|s| !s.is_empty());
        let status = get_str_param(params, "status").filter(|s| !s.is_empty());
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_list_jobs(&conn, workspace_id, job_type, status, limit)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_task_wait_for_job(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let job_id = require_str_param(params, "job_id")?;
        let timeout = params
            .get("timeout")
            .and_then(Value::as_f64)
            .unwrap_or(30.0);
        let poll_interval = params
            .get("poll_interval")
            .and_then(Value::as_f64)
            .unwrap_or(0.5);
        // 越界参数 fail-closed：负数 → invalid_params（poll_interval 负数的
        // sleep 会 panic；timeout 负数无业务意义）
        if timeout < 0.0 {
            return Err(DaemonRpcError::invalid_params(
                "timeout 不能为负数".to_string(),
            ));
        }
        if poll_interval < 0.0 {
            return Err(DaemonRpcError::invalid_params(
                "poll_interval 不能为负数".to_string(),
            ));
        }
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        // 复刻 Python wait_for_job 轮询循环：deadline 内循环查询 jobs 表，
        // 终态即返回（含 elapsed），否则 sleep poll_interval；超时分支再次
        // 查询并返回 status="timeout"。
        let start = std::time::Instant::now();
        let deadline = start + std::time::Duration::from_secs_f64(timeout);
        loop {
            if std::time::Instant::now() >= deadline {
                break;
            }
            let job = query_local_get_job(&conn, workspace_id, job_id)
                .map_err(DaemonRpcError::internal_error)?;
            match job {
                None => {
                    return Ok(json!({"error": format!("job not found: {job_id}")}));
                }
                Some(j) if is_job_terminal(&j) => {
                    let elapsed = start.elapsed().as_secs_f64();
                    return Ok(Value::Object(job_wait_result_map(job_id, &j, elapsed)));
                }
                Some(_) => {
                    std::thread::sleep(std::time::Duration::from_secs_f64(poll_interval));
                }
            }
        }
        // 超时：非终态 → status="timeout" + error="timeout after Xs"
        let job = query_local_get_job(&conn, workspace_id, job_id)
            .map_err(DaemonRpcError::internal_error)?;
        match job {
            None => Ok(json!({"error": format!("job not found: {job_id}")})),
            Some(j) => {
                let elapsed = start.elapsed().as_secs_f64();
                let mut m = job_wait_result_map(job_id, &j, elapsed);
                if !is_job_terminal(&j) {
                    m.insert("status".into(), Value::String("timeout".to_string()));
                }
                m.insert(
                    "error".into(),
                    Value::String(format!("timeout after {timeout}s")),
                );
                Ok(Value::Object(m))
            }
        }
    }

    fn handle_task_clone_group_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_clone_group_stats(&conn, workspace_id).map_err(DaemonRpcError::internal_error)
    }

    // W2-3（T-1786840097331-fd01a3f8）：defect/edit stats HTTP native 迁移。
    // 两个工具（defect_stats / get_edit_stats）从 python_compat（compat worker）
    // 迁移为 rust_native。数据源（defect_patterns / defect_fixes /
    // file_edit_audit）均在主库，经 snapshot query_db_path（主库只读连接，
    // open_query_connection 同构 W2-1/W2-2）访问；无需扩展 snapshot schema。
    // snapshot_not_ready 保护 + owned_workspace ACL 与既有 query.* 完全一致。
    // 语义逐条复刻 Python db 层（db_defect_kb.py / db_edit.py），注意两个统计
    // 均为**全局视图**（defect 两表无 workspace_id 列；file_edit_audit 虽有列但
    // Python SQL 无过滤），workspace_instance_id 仅用于 ACL，查询不带
    // workspace_id WHERE。

    fn handle_defect_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let (_workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_defect_stats(&conn).map_err(DaemonRpcError::internal_error)
    }

    fn handle_edit_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let time_window = get_str_param_or(params, "time_window", "30d");
        let (_workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_edit_stats(&conn, &time_window).map_err(DaemonRpcError::internal_error)
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

    fn handle_query_file(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let file_path = require_str_param(params, "file_path")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_file_symbols(&conn, workspace_id, file_path)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_symbol_location(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let name = require_str_param(params, "name")?;
        let file_path = require_str_param(params, "file_path")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_symbol_location(&conn, workspace_id, name, file_path)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_grep(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let patterns = params
            .get("patterns")
            .and_then(Value::as_array)
            .ok_or_else(|| DaemonRpcError::invalid_params("patterns 必须是字符串数组"))?
            .iter()
            .map(|value| {
                value
                    .as_str()
                    .map(ToOwned::to_owned)
                    .ok_or_else(|| DaemonRpcError::invalid_params("patterns 必须是字符串数组"))
            })
            .collect::<Result<Vec<_>, _>>()?;
        if patterns.is_empty() {
            return Err(DaemonRpcError::invalid_params("grep 至少需要一个 pattern"));
        }
        let options = GrepOptions {
            patterns,
            fixed: params
                .get("fixed")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            limit: get_int_param_or(params, "limit", 200).max(0) as usize,
            path: get_str_param(params, "path").map(std::path::PathBuf::from),
            include_all: params
                .get("include_all")
                .and_then(Value::as_bool)
                .unwrap_or(false),
            kind: get_str_param(params, "kind").map(ToOwned::to_owned),
        };
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_grep(&conn, workspace_id, &options).map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_issues(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let qualified_name = require_str_param(params, "qualified_name")?;
        let include_info = params
            .get("include_info")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        query_local_issues(&conn, workspace_id, qualified_name, include_info)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_query_tests(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let qualified_name = require_str_param(params, "qualified_name")?;
        let reverse = params
            .get("reverse")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let history = params
            .get("history")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let limit = get_int_param_or(params, "limit", 50).max(0) as usize;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        if history {
            query_local_test_stability(&conn, workspace_id, qualified_name, limit)
        } else if reverse {
            query_local_tested_functions(&conn, workspace_id, qualified_name)
        } else {
            query_local_test_cases(&conn, workspace_id, qualified_name)
        }
        .map_err(DaemonRpcError::internal_error)
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
        // W3-1（T-1786861820150-bfe5e805）：双模式。HTTP rust_native 便捷方法
        // 携带 workspace_instance_id → 经 open_query_connection（主库只读，
        // owned_workspace ACL + snapshot_not_ready 保护）查询主库
        // workspace_build_contexts，并用 require_bound_workspace_id 校验
        // params.workspace_id 与权威 workspace_id 一致（不一致 → invalid_params
        // fail-closed，防跨 workspace 越权）；G1 Layer 2 CLI/legacy 调用仅携带
        // workspace_id → 走 ToolchainStore（独立 toolchain.db），保持 G1 语义
        // 与 P0-1 ACL 不变。
        if params.get("workspace_instance_id").is_some() {
            let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
            let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
            let _ = self.require_bound_workspace_id(params, workspace_id)?;
            return query_local_build_context_list(&conn, workspace_id)
                .map_err(DaemonRpcError::internal_error);
        }
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
        // W3-1：同 handle_build_context_list 双模式。workspace_instance_id 模式
        // 支持短 hash 前缀匹配（唯一前缀才返回，0/多返回 Null，复刻 Python
        // db_toolchain.get_build_context）。
        if params.get("workspace_instance_id").is_some() {
            let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
            let build_context_hash = require_str_param(params, "build_context_hash")?;
            let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
            let _ = self.require_bound_workspace_id(params, workspace_id)?;
            return query_local_build_context_get(&conn, workspace_id, build_context_hash)
                .map_err(DaemonRpcError::internal_error);
        }
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

    // W3-1（T-1786861820150-bfe5e805）：build 读组新增 3 个 HTTP native handler
    // （build_context.active / build_context.resolved_edges /
    // build_context.count_resolved_edges）。无 G1 同名路由，仅支持
    // workspace_instance_id 模式（主库只读 + workspace_id 绑定校验）。
    fn handle_build_context_active(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        let _ = self.require_bound_workspace_id(params, workspace_id)?;
        query_local_build_context_active(&conn, workspace_id)
            .map_err(DaemonRpcError::internal_error)
    }

    fn handle_build_context_resolved_edges(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let build_context_hash = require_str_param(params, "build_context_hash")?;
        let caller_symbol_id = get_int_param(params, "caller_symbol_id");
        let limit = get_int_param_or(params, "limit", 50);
        if limit < 0 {
            return Err(DaemonRpcError::invalid_params(
                "limit 不能为负数".to_string(),
            ));
        }
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        let _ = self.require_bound_workspace_id(params, workspace_id)?;
        query_local_resolved_edges(
            &conn,
            workspace_id,
            build_context_hash,
            caller_symbol_id,
            limit,
        )
        .map_err(DaemonRpcError::internal_error)
    }

    fn handle_build_context_count_resolved_edges(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        let build_context_hash = require_str_param(params, "build_context_hash")?;
        let (workspace_id, conn) = self.open_query_connection(peer, workspace_instance_id)?;
        let _ = self.require_bound_workspace_id(params, workspace_id)?;
        query_local_count_resolved_edges(&conn, workspace_id, build_context_hash)
            .map_err(DaemonRpcError::internal_error)
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

    // ---- 收敛架构 RPC（T02 下沉：fs / metrics / job / admin / edit）----
    // 所有 CONVERGENCE_RPC_METHODS 经 dispatch 进入本方法。ACL（owned_workspace）
    // 由 open_query_connection / resolve_convergence_workspace 统一强制。

    fn handle_convergence_rpc(
        &mut self,
        peer: PeerCredential,
        method: &str,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        use super::admin_handlers as admin;
        use super::edit_handlers as edit;
        use super::fs_handlers as fs;
        use super::job_runner as job;
        use super::metrics_handlers as metrics;
        use super::_mcp_common_handlers as mcp;
        use super::audit_log_handlers as audit;

        // 解析 workspace codegraph DB 路径（daemon 权威写库，来自模板）。
        let codegraph_db = |state: &Self, ws: &str| -> Result<Option<std::path::PathBuf>, DaemonRpcError> {
            if state.base.codegraph_db_path_template.is_empty() {
                return Ok(None);
            }
            Ok(Some(std::path::PathBuf::from(
                state
                    .base
                    .codegraph_db_path_template
                    .replace("{workspace_instance_id}", ws),
            )))
        };

        // 打开 workspace codegraph DB 写连接（admin/edit/job 用）。
        let open_write = |state: &Self, ws: &str| -> Result<Connection, DaemonRpcError> {
            let db = codegraph_db(state, ws)?.ok_or_else(|| {
                DaemonRpcError::new(
                    "codegraph_db_unconfigured",
                    "daemon 未配置 codegraph_db_path_template（fail-closed）",
                )
            })?;
            Connection::open(db).map_err(|e| {
                DaemonRpcError::internal_error(format!("打开 codegraph DB 失败: {e}"))
            })
        };

        // 打开 daemon 权威 audit.db 写连接（audit_handlers 用）。
        let open_audit = |state: &Self| -> Result<Connection, DaemonRpcError> {
            if state.base.audit_db_path.as_os_str().is_empty() {
                return Err(DaemonRpcError::new(
                    "audit_db_unconfigured",
                    "daemon 未配置 audit_db_path（fail-closed）",
                ));
            }
            // 确保父目录存在（对齐 Python AuditLogger._init_db 的 os.makedirs）。
            if let Some(parent) = state.base.audit_db_path.parent() {
                if !parent.as_os_str().is_empty() {
                    let _ = std::fs::create_dir_all(parent);
                }
            }
            Connection::open(&state.base.audit_db_path).map_err(|e| {
                DaemonRpcError::internal_error(format!("打开 audit DB 失败: {e}"))
            })
        };

        match method {
            // ---- 文件/构建面（fs_handlers）----
            "workspace.build_graph" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let db = codegraph_db(self, ws)?;
                fs::handle_build_graph(&self.base.registry, &peer, params, db.as_deref())
            }
            "workspace.build_directory" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let db = codegraph_db(self, ws)?;
                fs::handle_build_directory(&self.base.registry, &peer, params, db.as_deref())
            }
            "workspace.file.read" => fs::handle_file_read(&self.base.registry, &peer, params),
            "workspace.file.grep" => fs::handle_file_grep(&self.base.registry, &peer, params),
            "workspace.file.list" => fs::handle_file_list(&self.base.registry, &peer, params),
            "workspace.file.symbol_content" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let db = codegraph_db(self, ws)?;
                fs::handle_file_symbol_content(&self.base.registry, &peer, params, db.as_deref())
            }
            "workspace.file.remove" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let db = codegraph_db(self, ws)?;
                fs::handle_file_remove(&self.base.registry, &peer, params, db.as_deref())
            }
            "workspace.file.refresh_file" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let db = codegraph_db(self, ws)?;
                fs::handle_refresh_file(&self.base.registry, &peer, params, db.as_deref())
            }
            "workspace.file.health" => fs::handle_file_health(&self.base.registry, &peer, params),

            // ---- MCP common 面（SRV-001：Python authority → Rust daemon）----
            "mcp.common.get_db_path_for_daemon" => {
                mcp::handle_get_db_path_for_daemon(params)
            }

            // ---- audit log 面（SRV-002：server audit log Python authority → Rust daemon）----
            "mcp.audit_log.get_conn" => {
                audit::handle_get_conn(&self.base.audit_db_path)
            }
            "mcp.audit_log.init_db" => {
                let conn = open_audit(self)?;
                audit::handle_init_db(&conn)
            }
            "mcp.audit_log.append" => {
                let conn = open_audit(self)?;
                audit::handle_append(&conn, params)
            }
            "mcp.audit_log.query" => {
                let conn = open_audit(self)?;
                audit::handle_query(&conn, params)
            }
            "mcp.audit_log.count" => {
                let conn = open_audit(self)?;
                audit::handle_count(&conn, params)
            }
            "mcp.audit_log.clear" => {
                let conn = open_audit(self)?;
                audit::handle_clear(&conn)
            }
            "mcp.audit_log.get_stats" => {
                let conn = open_audit(self)?;
                audit::handle_get_stats(&conn)
            }

            // ---- 度量/状态面（metrics_handlers，经 open_query_connection 只读）----
            "query.status" | "query.metrics_summary" | "query.complexity_hotspots"
            | "query.coupling_analysis" | "query.function_metrics"
            | "query.largest_functions" | "query.most_coupled_functions"
            | "query.code_health" | "query.symbol_content_by_hash" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let (workspace_id, conn) = self.open_query_connection(peer, ws)?;
                match method {
                    "query.status" => metrics::handle_status(&conn, workspace_id, ws, params),
                    "query.metrics_summary" => metrics::handle_metrics_summary(&conn, workspace_id, params),
                    "query.complexity_hotspots" => metrics::handle_complexity_hotspots(&conn, workspace_id, params),
                    "query.coupling_analysis" => metrics::handle_coupling_analysis(&conn, workspace_id, params),
                    "query.function_metrics" => metrics::handle_function_metrics(&conn, workspace_id, params),
                    "query.largest_functions" => metrics::handle_largest_functions(&conn, workspace_id, params),
                    "query.most_coupled_functions" => metrics::handle_most_coupled_functions(&conn, workspace_id, params),
                    "query.code_health" => metrics::handle_code_health(&conn, workspace_id, params),
                    _ => metrics::handle_symbol_content_by_hash(&conn, workspace_id, params),
                }
            }

            // ---- diff 读面（edit_handlers，只读）----
            "query.diff_callees" | "query.diff_callers" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let (workspace_id, conn) = self.open_query_connection(peer, ws)?;
                match method {
                    "query.diff_callees" => edit::handle_diff_callees(&conn, workspace_id, params),
                    _ => edit::handle_diff_callers(&conn, workspace_id, params),
                }
            }

            // ---- 异步长任务（job_runner）----
            "task.job_submit" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let workspace = super::workspace::owned_workspace(
                    &self.base.registry,
                    peer.uid,
                    ws,
                )?;
                let workspace_id = workspace
                    .get("workspace_id")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| DaemonRpcError::internal_error("workspace_id 缺失".to_string()))?;
                let db = codegraph_db(self, ws)?;
                job::rpc_job_submit(workspace_id, ws, db, params)
            }
            "task.job_cancel" => job::rpc_job_cancel(params),

            // ---- GC/审计/运维（admin_handlers）----
            "admin.metrics_get" => admin::handle_metrics_get(params),
            "admin.gc_archive_import" | "admin.gc_archive_inspect" | "admin.gc_archive_list"
            | "admin.gc_audit_get" | "admin.gc_audit_list" | "admin.gc_policy_get"
            | "admin.gc_policy_set" | "admin.gc_retention" | "admin.audit_rotate_key"
            | "admin.cleanup_rule_sync_log" | "admin.clear_clones"
            | "admin.snapshot_compare" | "admin.branch_register" | "admin.branch_switch"
            | "admin.assignment_create" | "admin.assignment_revoke"
            | "admin.record_action_identity" | "admin.register_attestation_revocation"
            | "admin.record_artifact_identity" | "admin.publish_interface"
            | "admin.select_interface_provider" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let workspace = super::workspace::owned_workspace(
                    &self.base.registry,
                    peer.uid,
                    ws,
                )?;
                let workspace_id = workspace
                    .get("workspace_id")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| DaemonRpcError::internal_error("workspace_id 缺失".to_string()))?;
                let conn = open_write(self, ws)?;
                match method {
                    "admin.gc_archive_import" => admin::handle_gc_archive_import(&conn, workspace_id, params),
                    "admin.gc_archive_inspect" => admin::handle_gc_archive_inspect(&conn, workspace_id, params),
                    "admin.gc_archive_list" => admin::handle_gc_archive_list(&conn, workspace_id, params),
                    "admin.gc_audit_get" => admin::handle_gc_audit_get(&conn, workspace_id, params),
                    "admin.gc_audit_list" => admin::handle_gc_audit_list(&conn, workspace_id, params),
                    "admin.gc_policy_get" => admin::handle_gc_policy_get(&conn, workspace_id, params),
                    "admin.gc_policy_set" => admin::handle_gc_policy_set(&conn, workspace_id, params),
                    "admin.gc_retention" => admin::handle_gc_retention(&conn, workspace_id, params),
                    "admin.audit_rotate_key" => admin::handle_audit_rotate_key(&conn, workspace_id, params),
                    "admin.cleanup_rule_sync_log" => admin::handle_cleanup_rule_sync_log(&conn, workspace_id, params),
                    "admin.clear_clones" => admin::handle_clear_clones(&conn, workspace_id, params),
                    "admin.snapshot_compare" => admin::handle_snapshot_compare(&conn, workspace_id, params),
                    "admin.branch_register" => admin::handle_branch_register(&conn, workspace_id, params),
                    "admin.branch_switch" => admin::handle_branch_switch(&conn, workspace_id, params),
                    "admin.assignment_create" => admin::handle_assignment_create(&conn, workspace_id, params),
                    "admin.assignment_revoke" => admin::handle_assignment_revoke(&conn, workspace_id, params),
                    "admin.record_action_identity" => admin::handle_record_action_identity(&conn, workspace_id, params),
                    "admin.register_attestation_revocation" => admin::handle_register_attestation_revocation(&conn, workspace_id, params),
                    "admin.record_artifact_identity" => admin::handle_record_artifact_identity(&conn, workspace_id, params),
                    "admin.publish_interface" => admin::handle_publish_interface(&conn, workspace_id, params),
                    _ => admin::handle_select_interface_provider(&conn, workspace_id, params),
                }
            }

            // ---- 编辑/提案/规则写面（edit_handlers）----
            "edit.propose" | "edit.propose_range_patch" | "edit.propose_symbol_id_patch"
            | "edit.propose_symbol_patch" | "edit.revert" | "edit.restore_all_comments"
            | "edit.restore_comment" | "edit.record_token_savings" | "gate.resolve_findings"
            | "gate.run_check" | "rule.seed_bootstrap" | "rule.extract_candidates"
            | "rule.candidate_accept" | "rule.candidate_create" | "rule.candidate_reject"
            | "rule.insert_agents_md_block" | "rule.sync_agents_md" | "guardrail.add_rule"
            | "summary.generate" => {
                let ws = require_str_param(params, "workspace_instance_id")?;
                let workspace = super::workspace::owned_workspace(
                    &self.base.registry,
                    peer.uid,
                    ws,
                )?;
                let workspace_id = workspace
                    .get("workspace_id")
                    .and_then(Value::as_i64)
                    .ok_or_else(|| DaemonRpcError::internal_error("workspace_id 缺失".to_string()))?;
                let conn = open_write(self, ws)?;
                match method {
                    "edit.propose" => edit::handle_propose_edit(&conn, workspace_id, params),
                    "edit.propose_range_patch" => edit::handle_propose_range_patch(&conn, workspace_id, params),
                    "edit.propose_symbol_id_patch" => edit::handle_propose_symbol_id_patch(&conn, workspace_id, params),
                    "edit.propose_symbol_patch" => edit::handle_propose_symbol_patch(&conn, workspace_id, params),
                    "edit.revert" => edit::handle_revert_edit(&conn, workspace_id, params),
                    "edit.restore_all_comments" => edit::handle_restore_all_comments(&conn, workspace_id, params),
                    "edit.restore_comment" => edit::handle_restore_comment(&conn, workspace_id, params),
                    "edit.record_token_savings" => edit::handle_record_token_savings(&conn, workspace_id, params),
                    "gate.resolve_findings" => edit::handle_resolve_gate_findings(&conn, workspace_id, params),
                    "gate.run_check" => edit::handle_run_check_gate(&conn, workspace_id, params),
                    "rule.seed_bootstrap" => edit::handle_rule_seed_bootstrap(&conn, workspace_id, params),
                    "rule.extract_candidates" => edit::handle_extract_rule_candidates(&conn, workspace_id, params),
                    "rule.candidate_accept" => edit::handle_rule_candidate_accept(&conn, workspace_id, params),
                    "rule.candidate_create" => edit::handle_rule_candidate_create(&conn, workspace_id, params),
                    "rule.candidate_reject" => edit::handle_rule_candidate_reject(&conn, workspace_id, params),
                    "rule.insert_agents_md_block" => edit::handle_rule_insert_agents_md_block(&conn, workspace_id, params),
                    "rule.sync_agents_md" => edit::handle_rule_sync_agents_md(&conn, workspace_id, params),
                    "guardrail.add_rule" => edit::handle_guardrail_add_rule(&conn, workspace_id, params),
                    _ => edit::handle_summary_generate(&conn, workspace_id, params),
                }
            }

            _ => Err(DaemonRpcError::method_not_found(method)),
        }
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

/// 从真相源（Python 用户级单库）解析真实 workspace id（W1-4-FIX）
///
/// 背景：`daemon_workspaces.workspace_id` 是 AUTOINCREMENT ROWID，重复
/// register（INSERT OR REPLACE → delete+insert）会轮转递增，与 Python 侧
/// `file_instances.workspace_id`（= `workspaces.id`，`root_path` UNIQUE）不一致。
/// publish 时必须从 db_path 的 `workspaces` 表按 `root_path` 匹配取真实 id 作为
/// GraphStore 过滤值，否则真实用户库 publish 后快照为空（syms=0）。
///
/// 匹配规则（与 Python `config.norm_path` 对齐）：
/// - `client_view_root` 与 `root_path` 各自经 `normalize_path_key` 规范化后比较
///   （反斜杠→正斜杠、去尾斜杠、盘符小写）
/// - 大小写不敏感（Windows 文件系统不区分大小写）
/// - 分支工作区 `root_path` 可能带 `#分支名` 后缀（db_branch.py），前缀匹配兜底
///
/// fallback：任何失败（打开失败 / 无 `workspaces` 表 / 无匹配行）返回
/// `fallback_rowid`（registry ROWID），保持 P0-2 隔离语义现状不更糟，禁止 panic。
fn resolve_true_workspace_id(db_path: &str, client_view_root: &str, fallback_rowid: i64) -> i64 {
    // 只读连接，避免对生产库产生写锁；打开失败直接 fallback
    let conn = match Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
    ) {
        Ok(c) => c,
        Err(_) => return fallback_rowid,
    };
    // workspaces 表可能不存在（测试 minimal_db 或非用户级库）→ fallback
    let mut stmt = match conn.prepare("SELECT id, root_path FROM workspaces") {
        Ok(s) => s,
        Err(_) => return fallback_rowid,
    };
    let key = normalize_path_key(client_view_root);
    if key.is_empty() {
        // client_view_root 缺失时无法匹配，保持 fallback（不返回 0，维持隔离语义）
        return fallback_rowid;
    }
    let key_lower = key.to_lowercase();
    let key_hash_prefix = format!("{}#", key_lower);
    let rows = match stmt.query_map([], |row| {
        let id: i64 = row.get(0)?;
        let root_path: String = row.get(1)?;
        Ok((id, root_path))
    }) {
        Ok(r) => r,
        Err(_) => return fallback_rowid,
    };
    for row in rows.flatten() {
        let (id, root_path) = row;
        let norm = normalize_path_key(&root_path).to_lowercase();
        if norm == key_lower || norm.starts_with(&key_hash_prefix) {
            return id;
        }
    }
    fallback_rowid
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
// W2-1（T-1786840097330-dec66710）：query 面 stats 只读查询函数
// ============================================
// 三个查询复刻 Python db 层语义（analyzers/coverage.py / call_chain.py / issues.py），
// 在 snapshot query_db_path（主库只读连接）上执行，按 workspace_id 过滤。

/// 查询当前 workspace 未注释符号（复刻 Python `get_uncommented_symbols`）。
///
/// 返回扁平 list：{qualified_name, module_path, start_line, end_line, depth,
/// name, kind, signature, file_path}，按 depth DESC / rel_path / start_line
/// 排序后无条件截断 limit（limit=0 返回空数组，对齐 Python 工具层 `[:limit]`）。
fn query_local_uncommented_symbols(
    conn: &Connection,
    workspace_id: i64,
    kind: &str,
    module_filter: &str,
    limit: usize,
) -> Result<Value, String> {
    let mut sql = String::from(
        "SELECT fsv.qualified_name, fsv.module_path, fsv.start_line, fsv.end_line,
                fsv.depth, sc.name, sc.kind, COALESCE(sc.signature, ''), fi.rel_path
         FROM (
            SELECT fsv_inner.*,
                   ROW_NUMBER() OVER (
                       PARTITION BY fsv_inner.qualified_name, fi.rel_path
                       ORDER BY fsv_inner.id DESC
                   ) as rn
            FROM file_symbol_versions fsv_inner
            JOIN file_versions fv_inner ON fsv_inner.file_version_id = fv_inner.id
            JOIN file_instances fi ON fv_inner.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1 AND fv_inner.is_current = 1
              AND (fsv_inner.is_deleted = 0 OR fsv_inner.is_deleted IS NULL)
         ) fsv
         JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
         JOIN file_versions fv ON fsv.file_version_id = fv.id
         JOIN file_instances fi ON fv.file_instance_id = fi.id
         WHERE fi.workspace_id = ?1 AND fsv.rn = 1
           AND sc.has_comment = 0
           AND sc.kind = ?2",
    );
    let mut params: Vec<rusqlite::types::Value> = vec![
        rusqlite::types::Value::Integer(workspace_id),
        rusqlite::types::Value::Text(kind.to_string()),
    ];
    if !module_filter.is_empty() {
        sql.push_str(" AND fsv.module_path LIKE ?3");
        params.push(rusqlite::types::Value::Text(format!("{module_filter}%")));
    }
    sql.push_str(" ORDER BY fsv.depth DESC, fi.rel_path, fsv.start_line");
    // 无条件 LIMIT：对齐 Python 工具层 `[:limit]`（limit=0 → 空数组）
    sql.push_str(" LIMIT ?");
    params.push(rusqlite::types::Value::Integer(limit as i64));
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare uncommented symbol query: {error}"))?;
    let rows = stmt
        .query_map(params_from_iter(params.iter()), |row| {
            Ok(json!({
                "qualified_name": row.get::<_, String>(0)?,
                "module_path": row.get::<_, String>(1)?,
                "start_line": row.get::<_, i64>(2)?,
                "end_line": row.get::<_, i64>(3)?,
                "depth": row.get::<_, i64>(4)?,
                "name": row.get::<_, String>(5)?,
                "kind": row.get::<_, String>(6)?,
                "signature": row.get::<_, String>(7)?,
                "file_path": row.get::<_, String>(8)?,
            }))
        })
        .map_err(|error| format!("cannot query uncommented symbols: {error}"))?;
    let mut symbols = Vec::new();
    for row in rows {
        symbols.push(row.map_err(|error| format!("cannot read uncommented symbol row: {error}"))?);
    }
    Ok(Value::Array(symbols))
}

/// 查询当前 workspace 模块间调用统计（复刻 Python `get_module_call_stats`）。
///
/// 从 call_versions 按 (caller_qualified, callee_qualified) 分组，提取顶级模块
/// （前 2-3 级路径），caller_mod != callee_mod 才计入；返回
/// [{caller_module, callee_module, call_count, unique_caller_count,
///   unique_callee_count}]，按 call_count DESC 排序后截断 limit。
fn query_local_module_call_stats(
    conn: &Connection,
    workspace_id: i64,
    limit: usize,
) -> Result<Value, String> {
    let mut stmt = conn
        .prepare(
            "
            SELECT cv.caller_qualified, cv.callee_qualified, COUNT(*) as call_count
            FROM call_versions cv
            JOIN file_versions fv ON cv.file_version_id = fv.id
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fi.workspace_id = ?1
              AND fv.is_current = 1
              AND cv.caller_qualified != ''
              AND cv.callee_qualified != ''
              AND cv.caller_qualified LIKE '%::%'
              AND cv.callee_qualified LIKE '%::%'
            GROUP BY cv.caller_qualified, cv.callee_qualified
            ",
        )
        .map_err(|error| format!("cannot prepare module call stats query: {error}"))?;
    let rows = stmt
        .query_map([workspace_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, i64>(2)?,
            ))
        })
        .map_err(|error| format!("cannot query module call stats rows: {error}"))?;

    // Python 聚合语义：模块级归并（caller_mod != callee_mod），
    // unique_* 为去重后的 caller/callee 限定名数量
    let mut module_stats: HashMap<(String, String), (i64, HashSet<String>, HashSet<String>)> =
        HashMap::new();
    for row in rows {
        let (caller, callee, count) =
            row.map_err(|error| format!("cannot read module call stats row: {error}"))?;
        let caller_mod = top_module(&caller);
        let callee_mod = top_module(&callee);
        if caller_mod == callee_mod {
            continue;
        }
        let entry = module_stats
            .entry((caller_mod.clone(), callee_mod.clone()))
            .or_insert_with(|| (0, HashSet::new(), HashSet::new()));
        entry.0 += count;
        entry.1.insert(caller);
        entry.2.insert(callee);
    }

    let mut results: Vec<Value> = module_stats
        .into_iter()
        .map(
            |((caller_module, callee_module), (call_count, callers, callees))| {
                json!({
                    "caller_module": caller_module,
                    "callee_module": callee_module,
                    "call_count": call_count,
                    "unique_caller_count": callers.len(),
                    "unique_callee_count": callees.len(),
                })
            },
        )
        .collect();
    // 无条件截断：对齐 Python 工具层 `results[:limit]`（limit=0 → 空数组）。
    // 稳定排序：call_count DESC；同 count 时按 caller_module/callee_module 字典序
    // （Python `results.sort(key=lambda x: x["call_count"], reverse=True)` 不稳定，
    // Rust 显式稳定排序避免顺序抖动）
    results.sort_by(|a, b| {
        let ac = a["call_count"].as_i64().unwrap_or(0);
        let bc = b["call_count"].as_i64().unwrap_or(0);
        bc.cmp(&ac)
            .then_with(|| {
                a["caller_module"]
                    .as_str()
                    .unwrap_or("")
                    .cmp(b["caller_module"].as_str().unwrap_or(""))
            })
            .then_with(|| {
                a["callee_module"]
                    .as_str()
                    .unwrap_or("")
                    .cmp(b["callee_module"].as_str().unwrap_or(""))
            })
    });
    results.truncate(limit);
    Ok(Value::Array(results))
}

/// 从 qualified_name 提取顶级模块（复刻 Python `get_top_module`）：
/// >=3 段取前 3 段（lib::core::xxx），2 段取前 2 段（lib::core），否则原样。
fn top_module(name: &str) -> String {
    let parts: Vec<&str> = name.split("::").collect();
    match parts.len() {
        0 | 1 => name.to_string(),
        2 => format!("{}::{}", parts[0], parts[1]),
        _ => format!("{}::{}::{}", parts[0], parts[1], parts[2]),
    }
}

/// 查询当前 workspace Semgrep 缺陷统计（复刻 Python `get_semgrep_stats`）。
///
/// 返回 {by_severity, by_language, by_rule(20), by_symbol(20), total_findings}，
/// by_severity/by_language 为 {key: count} 映射（Python dict 有序性由调用方承担，
/// 此处保证键集合一致），by_rule/by_symbol 为扁平 list。
fn query_local_semgrep_stats(conn: &Connection, workspace_id: i64) -> Result<Value, String> {
    // total_findings
    let total_findings: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM semgrep_findings sf
             JOIN file_instances fi ON sf.file_instance_id = fi.id
             WHERE fi.workspace_id = ?1",
            [workspace_id],
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot query semgrep total: {error}"))?;

    // by_severity / by_language：{key: count}
    let by_severity = group_count_pairs(conn, workspace_id, "sf.severity")?;
    let by_language = group_count_pairs(conn, workspace_id, "sf.language")?;

    // by_rule（TOP 20）
    let mut by_rule = Vec::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT sf.rule_id, sf.rule_name, sf.severity, COUNT(*) as cnt
                 FROM semgrep_findings sf
                 JOIN file_instances fi ON sf.file_instance_id = fi.id
                 WHERE fi.workspace_id = ?1
                 GROUP BY sf.rule_id ORDER BY cnt DESC LIMIT 20",
            )
            .map_err(|error| format!("cannot prepare semgrep by_rule query: {error}"))?;
        let rows = stmt
            .query_map([workspace_id], |row| {
                Ok(json!({
                    "rule_id": row.get::<_, String>(0)?,
                    "rule_name": row.get::<_, String>(1)?,
                    "severity": row.get::<_, String>(2)?,
                    "cnt": row.get::<_, i64>(3)?,
                }))
            })
            .map_err(|error| format!("cannot query semgrep by_rule: {error}"))?;
        for row in rows {
            by_rule.push(row.map_err(|error| format!("cannot read semgrep by_rule row: {error}"))?);
        }
    }

    // by_symbol（TOP 20，symbol_qualified 非空）
    let mut by_symbol = Vec::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT sf.symbol_qualified, COUNT(*) as cnt
                 FROM semgrep_findings sf
                 JOIN file_instances fi ON sf.file_instance_id = fi.id
                 WHERE fi.workspace_id = ?1 AND sf.symbol_qualified != ''
                 GROUP BY sf.symbol_qualified ORDER BY cnt DESC LIMIT 20",
            )
            .map_err(|error| format!("cannot prepare semgrep by_symbol query: {error}"))?;
        let rows = stmt
            .query_map([workspace_id], |row| {
                Ok(json!({
                    "symbol_qualified": row.get::<_, String>(0)?,
                    "cnt": row.get::<_, i64>(1)?,
                }))
            })
            .map_err(|error| format!("cannot query semgrep by_symbol: {error}"))?;
        for row in rows {
            by_symbol
                .push(row.map_err(|error| format!("cannot read semgrep by_symbol row: {error}"))?);
        }
    }

    Ok(json!({
        "by_severity": by_severity,
        "by_language": by_language,
        "by_rule": by_rule,
        "by_symbol": by_symbol,
        "total_findings": total_findings,
    }))
}

/// W3-3（T-1786861820151-deb64c48）：查询当前 workspace Semgrep 缺陷列表
/// （复刻 Python `get_semgrep_findings`，analyzers/issues.py L776-819）。
///
/// semgrep_findings 表无 workspace_id 列，隔离经 `JOIN file_instances fi ON
/// sf.file_instance_id = fi.id` + `WHERE fi.workspace_id = ?1` 实现（与
/// query_local_semgrep_stats 同构）。可选过滤（复刻 Python 条件拼接语义）：
/// - severity 非空 → `sf.severity = ?`（Python `severity.upper()`）；
/// - language 非空 → `sf.language = ?`（精确匹配）；
/// - rule_id 非空 → `sf.rule_id LIKE ?`（模糊匹配 `%rule_id%`）。
/// 排序：`sf.severity = 'ERROR' DESC, sf.severity = 'WARNING' DESC,
/// sf.id DESC`（ERROR 优先、其次 WARNING、同权重按 id 降序）；LIMIT 截断
/// （limit=0 → 空数组）。返回 `sf.*` 全列 + `fi.rel_path as file_path`
/// （Python `SELECT sf.*, fi.rel_path as file_path` + `dict(row)` 的
/// snake_case 列名集合）。
fn query_local_semgrep_findings(
    conn: &Connection,
    workspace_id: i64,
    severity: &str,
    language: &str,
    rule_id: &str,
    limit: usize,
) -> Result<Value, String> {
    let mut sql = String::from(
        "SELECT sf.id, sf.file_instance_id, sf.content_hash, sf.rule_id, \
         sf.rule_name, sf.message, sf.severity, sf.confidence, sf.language, \
         sf.start_line, sf.end_line, sf.snippet, sf.fix, sf.symbol_id, \
         sf.symbol_qualified, sf.scanned_at, sf.scan_id, \
         fi.rel_path as file_path \
         FROM semgrep_findings sf \
         JOIN file_instances fi ON sf.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?1",
    );
    let mut params: Vec<rusqlite::types::Value> =
        vec![rusqlite::types::Value::Integer(workspace_id)];
    if !severity.is_empty() {
        sql.push_str(" AND sf.severity = ?");
        params.push(rusqlite::types::Value::Text(severity.to_uppercase()));
    }
    if !language.is_empty() {
        sql.push_str(" AND sf.language = ?");
        params.push(rusqlite::types::Value::Text(language.to_string()));
    }
    if !rule_id.is_empty() {
        sql.push_str(" AND sf.rule_id LIKE ?");
        params.push(rusqlite::types::Value::Text(format!("%{rule_id}%")));
    }
    sql.push_str(
        " ORDER BY sf.severity = 'ERROR' DESC, sf.severity = 'WARNING' DESC, \
         sf.id DESC LIMIT ?",
    );
    params.push(rusqlite::types::Value::Integer(limit as i64));

    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare semgrep findings query: {error}"))?;
    // W3-3（T-1786861820151-deb64c48）语义对齐修复：Python `dict(row)`
    // 对 NULL 列返回 None，绝不报错；schema 中 semgrep_findings 除
    // file_instance_id/rule_id 外均无 NOT NULL 约束，历史数据可含 NULL
    // （如 fix/symbol_id/symbol_qualified/scan_id）。此处全部 Option 化
    // （JSON null ≡ Python None），避免 `Invalid column type Null` 崩溃。
    let rows = stmt
        .query_map(params_from_iter(params.iter()), |row| {
            Ok(json!({
                "id": row.get::<_, i64>(0)?,
                "file_instance_id": row.get::<_, Option<i64>>(1)?,
                "content_hash": row.get::<_, Option<String>>(2)?,
                "rule_id": row.get::<_, Option<String>>(3)?,
                "rule_name": row.get::<_, Option<String>>(4)?,
                "message": row.get::<_, Option<String>>(5)?,
                "severity": row.get::<_, Option<String>>(6)?,
                "confidence": row.get::<_, Option<String>>(7)?,
                "language": row.get::<_, Option<String>>(8)?,
                "start_line": row.get::<_, Option<i64>>(9)?,
                "end_line": row.get::<_, Option<i64>>(10)?,
                "snippet": row.get::<_, Option<String>>(11)?,
                "fix": row.get::<_, Option<String>>(12)?,
                "symbol_id": row.get::<_, Option<i64>>(13)?,
                "symbol_qualified": row.get::<_, Option<String>>(14)?,
                "scanned_at": row.get::<_, Option<f64>>(15)?,
                "scan_id": row.get::<_, Option<i64>>(16)?,
                "file_path": row.get::<_, Option<String>>(17)?,
            }))
        })
        .map_err(|error| format!("cannot query semgrep findings: {error}"))?;
    let mut results = Vec::new();
    for row in rows {
        results.push(row.map_err(|error| format!("cannot read semgrep findings row: {error}"))?);
    }
    Ok(Value::Array(results))
}

// ============================================
// W4-1（T-1786886251769-22b94ee8-sub-1）：git 读组只读查询函数
// ============================================
// 五个查询复刻 Python db 层语义（db_query.py `get_file_history` /
// db_git.py `get_git_commits` / `get_commit_changes` / `get_git_stats` /
// db_task_attribution.py `get_commit_tasks`），在 snapshot query_db_path
// （主库只读连接）上执行。workspace 隔离策略见文件头 W4-1 注释块。

/// 查询文件版本历史（复刻 Python `get_file_history`，db_query.py）。
///
/// `fv.* + fi.rel_path` 按 version_num 倒序；`fi.status != 'archived'`
/// 过滤归档实例。Python `dict(row)` 对 NULL 列返回 None，此处除
/// id/file_instance_id/version_num/content_hash/mtime/parsed_at（NOT NULL）
/// 外全部 Option 化（JSON null ≡ Python None）。ast_cache 为 BLOB 列，
/// Python compat worker 的 `json.dumps` 遇非 NULL bytes 会直接崩溃，
/// 常态数据为 NULL；此处恒输出 null（JSON 兼容，语义覆盖常态场景）。
fn query_local_file_history(
    conn: &Connection,
    workspace_id: i64,
    file_path: &str,
) -> Result<Value, String> {
    let mut stmt = conn
        .prepare(
            "SELECT fv.id, fv.file_instance_id, fv.version_num, fv.content_hash, \
             fv.mtime, fv.total_lines, fv.parsed_at, fv.is_current, fv.is_deleted, \
             fv.commit_hash, fi.rel_path \
             FROM file_versions fv \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.rel_path = ?2 \
               AND fi.status != 'archived' \
             ORDER BY fv.version_num DESC",
        )
        .map_err(|error| format!("cannot prepare file history query: {error}"))?;
    let rows = stmt
        .query_map(params![workspace_id, file_path], |row| {
            Ok(json!({
                "id": row.get::<_, i64>(0)?,
                "file_instance_id": row.get::<_, i64>(1)?,
                "version_num": row.get::<_, i64>(2)?,
                "content_hash": row.get::<_, String>(3)?,
                "mtime": row.get::<_, f64>(4)?,
                "total_lines": row.get::<_, Option<i64>>(5)?,
                "parsed_at": row.get::<_, f64>(6)?,
                "is_current": row.get::<_, Option<i64>>(7)?,
                "is_deleted": row.get::<_, Option<i64>>(8)?,
                "commit_hash": row.get::<_, Option<String>>(9)?,
                "ast_cache": Value::Null,
                "rel_path": row.get::<_, String>(10)?,
            }))
        })
        .map_err(|error| format!("cannot query file history: {error}"))?;
    let mut versions = Vec::new();
    for row in rows {
        versions.push(row.map_err(|error| format!("cannot read file history row: {error}"))?);
    }
    Ok(Value::Array(versions))
}

/// 查询当前 workspace Git commit 列表（复刻 Python `get_git_commits`）。
///
/// git_commits 全列，按 timestamp 倒序分页。message/author/email 虽有
/// DEFAULT ''，但无 NOT NULL 约束，历史数据可含 NULL → Option 化。
fn query_local_git_commits(
    conn: &Connection,
    workspace_id: i64,
    limit: usize,
    offset: usize,
) -> Result<Value, String> {
    let mut stmt = conn
        .prepare(
            "SELECT id, commit_hash, message, author, email, timestamp, workspace_id \
             FROM git_commits \
             WHERE workspace_id = ?1 \
             ORDER BY timestamp DESC LIMIT ?2 OFFSET ?3",
        )
        .map_err(|error| format!("cannot prepare git commits query: {error}"))?;
    let rows = stmt
        .query_map(
            params_from_iter([workspace_id, limit as i64, offset as i64].iter()),
            |row| {
                Ok(json!({
                    "id": row.get::<_, i64>(0)?,
                    "commit_hash": row.get::<_, String>(1)?,
                    "message": row.get::<_, Option<String>>(2)?,
                    "author": row.get::<_, Option<String>>(3)?,
                    "email": row.get::<_, Option<String>>(4)?,
                    "timestamp": row.get::<_, f64>(5)?,
                    "workspace_id": row.get::<_, i64>(6)?,
                }))
            },
        )
        .map_err(|error| format!("cannot query git commits: {error}"))?;
    let mut commits = Vec::new();
    for row in rows {
        commits.push(row.map_err(|error| format!("cannot read git commits row: {error}"))?);
    }
    Ok(Value::Array(commits))
}

/// 查询指定 commit 变更详情（复刻 Python `get_commit_changes` 两段式）。
///
/// 第一段按 workspace_id + commit_hash 确认归属（跨 workspace commit
/// 视为不存在 → {"commit": null, "file_changes": []} fail-closed）；
/// 第二段按 commit_hash 查 git_file_changes LEFT JOIN file_instances
/// 补 rel_path/abs_path（archived 行 file_instances 可能缺失 → NULL）。
fn query_local_git_commit_changes(
    conn: &Connection,
    workspace_id: i64,
    commit_hash: &str,
) -> Result<Value, String> {
    let commit = conn
        .query_row(
            "SELECT id, commit_hash, message, author, email, timestamp, workspace_id \
             FROM git_commits \
             WHERE workspace_id = ?1 AND commit_hash = ?2",
            params![workspace_id, commit_hash],
            |row| {
                Ok(json!({
                    "id": row.get::<_, i64>(0)?,
                    "commit_hash": row.get::<_, String>(1)?,
                    "message": row.get::<_, Option<String>>(2)?,
                    "author": row.get::<_, Option<String>>(3)?,
                    "email": row.get::<_, Option<String>>(4)?,
                    "timestamp": row.get::<_, f64>(5)?,
                    "workspace_id": row.get::<_, i64>(6)?,
                }))
            },
        )
        .optional()
        .map_err(|error| format!("cannot query git commit row: {error}"))?;
    let commit = match commit {
        Some(commit) => commit,
        None => return Ok(json!({"commit": null, "file_changes": []})),
    };

    let mut stmt = conn
        .prepare(
            "SELECT gfc.id, gfc.commit_hash, gfc.file_instance_id, gfc.change_type, \
             gfc.old_content_hash, gfc.new_content_hash, gfc.lines_added, \
             gfc.lines_deleted, fi.rel_path, fi.abs_path \
             FROM git_file_changes gfc \
             LEFT JOIN file_instances fi ON gfc.file_instance_id = fi.id \
             WHERE gfc.commit_hash = ?1 \
             ORDER BY fi.rel_path",
        )
        .map_err(|error| format!("cannot prepare git commit changes query: {error}"))?;
    let rows = stmt
        .query_map(params_from_iter([commit_hash].iter()), |row| {
            Ok(json!({
                "id": row.get::<_, i64>(0)?,
                "commit_hash": row.get::<_, String>(1)?,
                "file_instance_id": row.get::<_, i64>(2)?,
                "change_type": row.get::<_, String>(3)?,
                "old_content_hash": row.get::<_, Option<String>>(4)?,
                "new_content_hash": row.get::<_, Option<String>>(5)?,
                "lines_added": row.get::<_, Option<i64>>(6)?,
                "lines_deleted": row.get::<_, Option<i64>>(7)?,
                "rel_path": row.get::<_, Option<String>>(8)?,
                "abs_path": row.get::<_, Option<String>>(9)?,
            }))
        })
        .map_err(|error| format!("cannot query git commit changes: {error}"))?;
    let mut file_changes = Vec::new();
    for row in rows {
        file_changes.push(row.map_err(|error| {
            format!("cannot read git commit changes row: {error}")
        })?);
    }
    Ok(json!({"commit": commit, "file_changes": file_changes}))
}

/// 查询当前 workspace Git 集成统计（复刻 Python `get_git_stats`）。
///
/// git_file_changes 无 workspace_id，经 JOIN git_commits（含 workspace_id）
/// 限定统计范围。返回 {commit_count, file_change_count, change_types}。
fn query_local_git_stats(conn: &Connection, workspace_id: i64) -> Result<Value, String> {
    let commit_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM git_commits WHERE workspace_id = ?1",
            params_from_iter([workspace_id].iter()),
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot count git commits: {error}"))?;
    let file_change_count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM git_file_changes gfc \
             JOIN git_commits gc ON gfc.commit_hash = gc.commit_hash \
             WHERE gc.workspace_id = ?1",
            params_from_iter([workspace_id].iter()),
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot count git file changes: {error}"))?;

    let mut stmt = conn
        .prepare(
            "SELECT gfc.change_type, COUNT(*) \
             FROM git_file_changes gfc \
             JOIN git_commits gc ON gfc.commit_hash = gc.commit_hash \
             WHERE gc.workspace_id = ?1 GROUP BY gfc.change_type",
        )
        .map_err(|error| format!("cannot prepare git change types query: {error}"))?;
    let rows = stmt
        .query_map(params_from_iter([workspace_id].iter()), |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, i64>(1)?,
            ))
        })
        .map_err(|error| format!("cannot query git change types: {error}"))?;
    let mut change_types = serde_json::Map::new();
    for row in rows {
        let (change_type, count) = row
            .map_err(|error| format!("cannot read git change type row: {error}"))?;
        change_types.insert(change_type, json!(count));
    }
    Ok(json!({
        "commit_count": commit_count,
        "file_change_count": file_change_count,
        "change_types": Value::Object(change_types),
    }))
}

// ============================================================
// W4-4（T-1786886251769-22b94ee8-sub-4）：分支差异 diff_branches
// ============================================================

/// 工作区符号集合（复刻 Python `_load_workspace_symbols` 的 dict 语义）：
/// `syms` 保序（SQL 行序，重复 qn 覆盖值不改位置），`index` 提供 qn → 位置映射。
struct WorkspaceSymbols {
    syms: Vec<(String, BranchSymbol)>,
    index: HashMap<String, usize>,
}

/// 单个符号行（symbol_hash / name / kind 均可空——schema 无 NOT NULL 强约束，
/// 与 Python 直接透传 dict 的行为一致，null 原样输出；rel_path/module_path
/// 本工具不输出，无需读取）。
struct BranchSymbol {
    symbol_hash: Option<String>,
    name: Option<String>,
    kind: Option<String>,
}

/// 加载工作区全部符号（复刻 Python `_load_workspace_symbols`，db/db_branch.py
/// L81-110）：SELECT s.symbol_hash, s.qualified_name, s.name, s.kind,
/// s.module_path, fi.rel_path FROM symbols s JOIN file_instances fi ON
/// s.file_instance_id = fi.id WHERE fi.workspace_id = ?；跳过空 qualified_name；
/// 按 qualified_name 索引（重复 qn 后值覆盖但保持首次位置，Python dict 语义）。
fn load_workspace_symbols(
    conn: &Connection,
    workspace_id: i64,
) -> Result<WorkspaceSymbols, String> {
    let mut stmt = conn
        .prepare(
            "SELECT s.symbol_hash, s.qualified_name, s.name, s.kind, s.module_path, fi.rel_path \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1",
        )
        .map_err(|error| format!("cannot prepare workspace symbols query: {error}"))?;
    let rows = stmt
        .query_map(params_from_iter([workspace_id].iter()), |row| {
            Ok((
                row.get::<_, Option<String>>(1)?, // qualified_name
                BranchSymbol {
                    symbol_hash: row.get::<_, Option<String>>(0)?,
                    name: row.get::<_, Option<String>>(2)?,
                    kind: row.get::<_, Option<String>>(3)?,
                },
            ))
        })
        .map_err(|error| format!("cannot query workspace symbols: {error}"))?;

    let mut syms: Vec<(String, BranchSymbol)> = Vec::new();
    let mut index: HashMap<String, usize> = HashMap::new();
    for row in rows {
        let (qn, sym) =
            row.map_err(|error| format!("cannot read workspace symbols row: {error}"))?;
        let qn = match qn {
            Some(q) if !q.is_empty() => q,
            _ => continue, // 跳过空 qualified_name（Python: `if not qn: continue`）
        };
        if let Some(&idx) = index.get(&qn) {
            // 重复 qn：覆盖值但不改变位置（Python dict `result[qn] = {...}` 语义）
            syms[idx].1 = sym;
        } else {
            index.insert(qn.clone(), syms.len());
            syms.push((qn, sym));
        }
    }
    Ok(WorkspaceSymbols { syms, index })
}

/// 比较两个分支的符号差异（复刻 Python `diff_branches`，db/db_branch.py L179-252）。
///
/// 按分支名（workspace name）精确匹配（`WHERE name = ?`，无大小写折叠，取首行）
/// 查 source/target 两个 workspace；任一不存在 → {"error": "源分支不存在: <名>"}
/// / {"error": "目标分支不存在: <名>"}（正常响应体，非 RPC 错误）。两 workspace
/// 符号按 qualified_name 对比 symbol_hash，三集合输出 added / removed /
/// modified / unchanged_count。列表顺序 = 各 workspace SELECT 行序（Python
/// dict 插入序 = SQLite 行序，无 ORDER BY）。
fn query_local_diff_branches(
    conn: &Connection,
    source_branch: &str,
    target_branch: &str,
) -> Result<Value, String> {
    let src_ws_id: Option<i64> = conn
        .query_row(
            "SELECT id FROM workspaces WHERE name = ?1",
            [source_branch],
            |row| row.get(0),
        )
        .optional()
        .map_err(|error| format!("cannot query source workspace: {error}"))?;
    let Some(src_ws_id) = src_ws_id else {
        return Ok(json!({"error": format!("源分支不存在: {source_branch}")}));
    };
    let tgt_ws_id: Option<i64> = conn
        .query_row(
            "SELECT id FROM workspaces WHERE name = ?1",
            [target_branch],
            |row| row.get(0),
        )
        .optional()
        .map_err(|error| format!("cannot query target workspace: {error}"))?;
    let Some(tgt_ws_id) = tgt_ws_id else {
        return Ok(json!({"error": format!("目标分支不存在: {target_branch}")}));
    };

    let src_syms = load_workspace_symbols(conn, src_ws_id)?;
    let tgt_syms = load_workspace_symbols(conn, tgt_ws_id)?;

    // 三集合对比（顺序：target 遍历 → added/modified/unchanged；source 遍历 → removed）
    let mut added: Vec<Value> = Vec::new();
    let mut removed: Vec<Value> = Vec::new();
    let mut modified: Vec<Value> = Vec::new();
    let mut unchanged_count: i64 = 0;

    for (qn, tgt_sym) in &tgt_syms.syms {
        match src_syms.index.get(qn) {
            None => {
                added.push(json!({
                    "qualified_name": qn,
                    "symbol_hash": tgt_sym.symbol_hash,
                    "name": tgt_sym.name,
                    "kind": tgt_sym.kind,
                }));
            }
            Some(&src_idx) => {
                let src_sym = &src_syms.syms[src_idx].1;
                if src_sym.symbol_hash != tgt_sym.symbol_hash {
                    modified.push(json!({
                        "qualified_name": qn,
                        "source_hash": src_sym.symbol_hash,
                        "target_hash": tgt_sym.symbol_hash,
                        "name": tgt_sym.name,
                        "kind": tgt_sym.kind,
                    }));
                } else {
                    unchanged_count += 1;
                }
            }
        }
    }

    for (qn, src_sym) in &src_syms.syms {
        if !tgt_syms.index.contains_key(qn) {
            removed.push(json!({
                "qualified_name": qn,
                "symbol_hash": src_sym.symbol_hash,
                "name": src_sym.name,
                "kind": src_sym.kind,
            }));
        }
    }

    Ok(json!({
        "added": added,
        "removed": removed,
        "modified": modified,
        "unchanged_count": unchanged_count,
    }))
}

/// 查询 commit 关联任务（复刻 Python `get_commit_tasks`，全局查询）。
///
/// task_symbol_changes.source_commit_hash JOIN tasks（include_task_details
/// =true 时补 title/status/parent_id），按 task_id 分组、last_change_at
/// 倒序。task_id 全局唯一（无 workspace 维度），与 Python 全局语义一致；
/// LEFT JOIN 缺 task 行 → 详情字段 null（Python None）。
fn query_local_commit_tasks(
    conn: &Connection,
    commit_hash: &str,
    include_task_details: bool,
) -> Result<Value, String> {
    let sql = if include_task_details {
        "SELECT tsc.task_id, COUNT(*) AS change_count, \
         MIN(tsc.created_at) AS first_change_at, \
         MAX(tsc.created_at) AS last_change_at, \
         t.title AS task_title, t.status AS task_status, t.parent_id AS task_parent_id \
         FROM task_symbol_changes tsc \
         LEFT JOIN tasks t ON tsc.task_id = t.id \
         WHERE tsc.source_commit_hash = ?1 \
         GROUP BY tsc.task_id \
         ORDER BY last_change_at DESC"
    } else {
        "SELECT task_id, COUNT(*) AS change_count, \
         MIN(created_at) AS first_change_at, \
         MAX(created_at) AS last_change_at \
         FROM task_symbol_changes \
         WHERE source_commit_hash = ?1 \
         GROUP BY task_id \
         ORDER BY last_change_at DESC"
    };
    let mut stmt = conn
        .prepare(sql)
        .map_err(|error| format!("cannot prepare commit tasks query: {error}"))?;
    let rows = stmt
        .query_map(params_from_iter([commit_hash].iter()), |row| {
            if include_task_details {
                Ok(json!({
                    "task_id": row.get::<_, String>(0)?,
                    "change_count": row.get::<_, i64>(1)?,
                    "first_change_at": row.get::<_, f64>(2)?,
                    "last_change_at": row.get::<_, f64>(3)?,
                    "task_title": row.get::<_, Option<String>>(4)?,
                    "task_status": row.get::<_, Option<String>>(5)?,
                    "task_parent_id": row.get::<_, Option<String>>(6)?,
                }))
            } else {
                Ok(json!({
                    "task_id": row.get::<_, String>(0)?,
                    "change_count": row.get::<_, i64>(1)?,
                    "first_change_at": row.get::<_, f64>(2)?,
                    "last_change_at": row.get::<_, f64>(3)?,
                }))
            }
        })
        .map_err(|error| format!("cannot query commit tasks: {error}"))?;
    let mut tasks = Vec::new();
    for row in rows {
        tasks.push(row.map_err(|error| format!("cannot read commit tasks row: {error}"))?);
    }
    Ok(Value::Array(tasks))
}

// ============================================
// W4-2（T-1786886251769-22b94ee8-sub-2）：coverage/review 读组只读查询函数
// ============================================
// 两个查询复刻 Python db 层语义（db_coverage.py `get_coverage_for_symbol` /
// db_impact.py `diff_to_symbol`），在 snapshot query_db_path（主库只读连接）
// 上执行。workspace 隔离策略见文件头 W4-2 注释块。

/// 复刻 Python `round(covered / tracked * 100, 1)`（十进制 round-half-even）。
///
/// Python 的 round(float, 1) 基于二进制浮点值，绝大多数场景与十进制
/// round-half-even 一致；此处用整数精确计算（covered*1000/tracked 商余
/// 判定）替代浮点，覆盖 tracked=0 → 0.0 语义。
fn coverage_pct_round(covered: i64, tracked: i64) -> f64 {
    if tracked <= 0 {
        return 0.0;
    }
    let n = (covered as i128) * 1000;
    let d = tracked as i128;
    let q = n / d;
    let r = n % d;
    // 商余判定：2r > d 进位；2r == d 取偶数（round-half-even）；否则不进位
    let rounded = if 2 * r > d || (2 * r == d && q % 2 == 1) {
        q + 1
    } else {
        q
    };
    rounded as f64 / 10.0
}

/// 查询符号覆盖率（复刻 Python `get_coverage_for_symbol`，db_coverage.py）。
///
/// 两段式：① symbols JOIN file_instances WHERE fi.workspace_id +
/// qualified_name LIMIT 1（未找到 → JSON null，即 Python None）；
/// ② coverage_data WHERE symbol_id + 行范围 ORDER BY line_start。
/// symbols/coverage_data 均无 workspace_id 列，第一段经 JOIN file_instances
/// 限定（与 W3-3 semgrep_findings 同构）。rel_path 为 NOT NULL 列。
fn query_local_coverage_for_symbol(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
) -> Result<Value, String> {
    let row = conn
        .query_row(
            "SELECT s.id, s.start_line, s.end_line, fi.rel_path \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND s.qualified_name = ?2 \
             LIMIT 1",
            params![workspace_id, qualified_name],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, String>(3)?,
                ))
            },
        )
        .optional()
        .map_err(|error| format!("cannot query coverage symbol: {error}"))?;
    let (symbol_id, start_line, end_line, rel_path) = match row {
        Some(row) => row,
        None => return Ok(Value::Null),
    };
    let total_lines = end_line - start_line + 1;

    let mut stmt = conn
        .prepare(
            "SELECT line_start, hit_count FROM coverage_data \
             WHERE symbol_id = ?1 AND line_start >= ?2 AND line_end <= ?3 \
             ORDER BY line_start",
        )
        .map_err(|error| format!("cannot prepare coverage data query: {error}"))?;
    let rows = stmt
        .query_map(
            params![symbol_id, start_line, end_line],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)),
        )
        .map_err(|error| format!("cannot query coverage data: {error}"))?;
    let mut tracked_lines: i64 = 0;
    let mut covered_lines: i64 = 0;
    let mut uncovered_lines: Vec<i64> = Vec::new();
    for row in rows {
        let (line_start, hit_count) =
            row.map_err(|error| format!("cannot read coverage data row: {error}"))?;
        tracked_lines += 1;
        if hit_count > 0 {
            covered_lines += 1;
        } else {
            uncovered_lines.push(line_start);
        }
    }
    let coverage_pct = coverage_pct_round(covered_lines, tracked_lines);
    Ok(json!({
        "qualified_name": qualified_name,
        "file_path": rel_path,
        "start_line": start_line,
        "end_line": end_line,
        "total_lines": total_lines,
        "tracked_lines": tracked_lines,
        "covered_lines": covered_lines,
        "coverage_pct": coverage_pct,
        "uncovered_lines": uncovered_lines,
    }))
}

/// 复刻 Python `db_impact.py:_normalize_path`（反斜杠 → 正斜杠，去首尾空白与引号）。
fn normalize_diff_path(p: &str) -> String {
    p.replace('\\', "/").trim().trim_matches('"').to_string()
}

/// 查询行号范围与符号重叠（复刻 Python `_query_overlapping_symbols` 两段式）。
///
/// 第一段按 rel_path 精确匹配；未命中则第二段查询 workspace 内所有重叠
/// 符号，内存过滤 `rel.endswith(rel_path) || rel_path.endswith(rel)` 后缀
/// 兜底（应对路径前缀差异）。rel_path 兜底段可含 NULL（历史数据）→
/// Option 化，Python `or ""` 语义（空串跳过）。
fn query_local_overlapping_symbols(
    conn: &Connection,
    workspace_id: i64,
    rel_path: &str,
    start_line: i64,
    end_line: i64,
) -> Result<Vec<(String, String, String)>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT s.symbol_hash, s.qualified_name, fi.rel_path \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fi.rel_path = ?2 \
               AND s.start_line <= ?3 AND s.end_line >= ?4",
        )
        .map_err(|error| format!("cannot prepare overlapping symbols query: {error}"))?;
    let mut rows = stmt
        .query_map(
            params![workspace_id, rel_path, end_line, start_line],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            },
        )
        .map_err(|error| format!("cannot query overlapping symbols: {error}"))?;
    let mut out = Vec::new();
    while let Some(r) = rows.next() {
        out.push(r.map_err(|error| format!("cannot read overlapping symbols row: {error}"))?);
    }
    if !out.is_empty() {
        return Ok(out);
    }

    // 后缀匹配兜底（应对路径前缀差异）
    let mut stmt = conn
        .prepare(
            "SELECT s.symbol_hash, s.qualified_name, fi.rel_path \
             FROM symbols s \
             JOIN file_instances fi ON s.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 \
               AND s.start_line <= ?2 AND s.end_line >= ?3",
        )
        .map_err(|error| format!("cannot prepare overlapping symbols fallback: {error}"))?;
    let rows = stmt
        .query_map(
            params![workspace_id, end_line, start_line],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, Option<String>>(2)?,
                ))
            },
        )
        .map_err(|error| format!("cannot query overlapping symbols fallback: {error}"))?;
    let mut out = Vec::new();
    for row in rows {
        let (hash, qn, rel) =
            row.map_err(|error| format!("cannot read overlapping symbols fallback row: {error}"))?;
        let rel = rel.unwrap_or_default();
        // Python: `if rel and (rel.endswith(rel_path) or rel_path.endswith(rel))`
        if !rel.is_empty() && (rel.ends_with(rel_path) || rel_path.ends_with(&rel)) {
            out.push((hash, qn, rel));
        }
    }
    Ok(out)
}

/// diff 解析状态（显式 struct 替代 Python 嵌套闭包 nonlocal）。
struct DiffParseState {
    old_file: Option<String>,
    new_file: Option<String>,
    file_deleted: bool,
    hunk_old_start: i64,
    hunk_old_len: i64,
    hunk_new_start: i64,
    hunk_new_len: i64,
    hunk_added: i64,
    hunk_removed: i64,
    has_hunk: bool,
    results: Vec<Value>,
}

impl DiffParseState {
    fn new() -> Self {
        DiffParseState {
            old_file: None,
            new_file: None,
            file_deleted: false,
            hunk_old_start: 0,
            hunk_old_len: 0,
            hunk_new_start: 0,
            hunk_new_len: 0,
            hunk_added: 0,
            hunk_removed: 0,
            has_hunk: false,
            results: Vec::new(),
        }
    }
}

/// 刷新当前 hunk 缓冲区（复刻 Python `diff_to_symbol` 内部 flush()）。
///
/// 关键语义（实现即真相）：flush() 先重置 has_hunk/hunk_added/hunk_removed
/// 后判定 change_type，因此重置后的 hunk_removed/hunk_added 恒为 0，
/// "added"/"仅删行 deleted" 两个分支在 Python 中实际不可达——非文件删除
/// 场景恒为 "modified"。Rust 侧逐字节保持该行为（判定只依赖重置后的值），
/// 保证 HTTP round-trip 数据级一致。
fn flush_diff_hunk(
    conn: &Connection,
    workspace_id: i64,
    state: &mut DiffParseState,
) -> Result<(), String> {
    if !state.has_hunk {
        return Ok(());
    }
    // 删除时用旧路径 + 旧行范围；否则用新路径 + 新行范围
    let (query_path, start, length) = if state.file_deleted {
        (state.old_file.clone(), state.hunk_old_start, state.hunk_old_len)
    } else {
        (state.new_file.clone(), state.hunk_new_start, state.hunk_new_len)
    };
    // 先重置 hunk 计数，后判定 change_type（保持 Python 行为）
    state.has_hunk = false;
    state.hunk_added = 0;
    state.hunk_removed = 0;
    let query_path = match query_path {
        Some(path) => path,
        None => return Ok(()),
    };
    // Python `if not query_path: return`（空路径直接跳过，不产生符号）
    if query_path.is_empty() {
        return Ok(());
    }
    let end = start + length.max(1) - 1;
    // 判定变更类型（重置后 hunk_removed/hunk_added 恒为 0，added/仅删行
    // deleted 分支不可达，与 Python 逐字节一致）
    let change_type = if state.file_deleted { "deleted" } else { "modified" };
    let syms = query_local_overlapping_symbols(conn, workspace_id, &query_path, start, end)?;
    for (symbol_hash, qualified_name, _rel) in syms {
        state.results.push(json!({
            "symbol_hash": symbol_hash,
            "qualified_name": qualified_name,
            "file_path": query_path.clone(),
            "change_type": change_type,
        }));
    }
    Ok(())
}

/// 解析 git diff 文本并映射到受影响符号（复刻 Python `diff_to_symbol`）。
///
/// 逐行状态机：`+++ b/...` 更新新路径、`+++ /dev/null` 标记文件删除、
/// `--- a/...` 更新旧路径、`@@ -o,n +n,m @@` 开启新 hunk；hunk 内
/// +/- 行计数。按 symbol_hash 去重（HashSet 保留首次 change_type）。
fn query_local_diff_to_symbol(
    conn: &Connection,
    workspace_id: i64,
    diff_text: &str,
) -> Result<Value, String> {
    let new_file_re = Regex::new(r"^\+\+\+\s+b/(.*)$").unwrap();
    let new_devnull_re = Regex::new(r"^\+\+\+\s+/dev/null").unwrap();
    let old_file_re = Regex::new(r"^---\s+a/(.*)$").unwrap();
    let hunk_re = Regex::new(r"^@@ -(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@").unwrap();

    let mut state = DiffParseState::new();
    for raw_line in diff_text.lines() {
        // 新文件为 /dev/null → 文件被删除
        if new_devnull_re.is_match(raw_line) {
            flush_diff_hunk(conn, workspace_id, &mut state)?;
            state.new_file = None;
            state.file_deleted = true;
            continue;
        }
        if let Some(caps) = new_file_re.captures(raw_line) {
            flush_diff_hunk(conn, workspace_id, &mut state)?;
            state.new_file = Some(normalize_diff_path(
                caps.get(1).map_or("", |m| m.as_str()),
            ));
            state.file_deleted = false;
            continue;
        }
        if let Some(caps) = old_file_re.captures(raw_line) {
            flush_diff_hunk(conn, workspace_id, &mut state)?;
            state.old_file = Some(normalize_diff_path(
                caps.get(1).map_or("", |m| m.as_str()),
            ));
            continue;
        }
        if let Some(caps) = hunk_re.captures(raw_line) {
            flush_diff_hunk(conn, workspace_id, &mut state)?;
            state.hunk_old_start = caps
                .get(1)
                .map_or(0, |m| m.as_str().parse::<i64>().unwrap_or(0));
            state.hunk_old_len = caps
                .get(2)
                .map_or(1, |m| m.as_str().parse::<i64>().unwrap_or(1));
            state.hunk_new_start = caps
                .get(3)
                .map_or(0, |m| m.as_str().parse::<i64>().unwrap_or(0));
            state.hunk_new_len = caps
                .get(4)
                .map_or(1, |m| m.as_str().parse::<i64>().unwrap_or(1));
            state.hunk_added = 0;
            state.hunk_removed = 0;
            state.has_hunk = true;
            continue;
        }
        if state.has_hunk {
            if raw_line.starts_with('+') {
                state.hunk_added += 1;
            } else if raw_line.starts_with('-') {
                state.hunk_removed += 1;
            }
        }
    }
    flush_diff_hunk(conn, workspace_id, &mut state)?;

    // 按 symbol_hash 去重，保留首次出现的 change_type
    let mut seen: HashSet<String> = HashSet::new();
    let mut out = Vec::new();
    for r in state.results {
        let hash = r
            .get("symbol_hash")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string();
        if seen.insert(hash) {
            out.push(r);
        }
    }
    Ok(Value::Array(out))
}

// ============================================
// W2-2（T-1786840097330-a9e0ec69）：task 面 stats 只读查询函数
// ============================================
// 三个查询复刻 Python db 层语义（db_clone_detection.py `get_clone_stats` /
// db_jobs.py `get_job_stats` / db_clone_groups.py `get_clone_group_stats`），
// 在 snapshot query_db_path（主库只读连接）上执行，按 workspace_id 过滤。

/// 查询当前 workspace 克隆检测统计（复刻 Python `get_clone_stats`）。
///
/// 返回 {total, type1, type2, type3, affected_files, affected_symbols}；
/// SUM(CASE...) 无匹配行时为 NULL，对齐 Python `row["type1"] or 0` 语义。
fn query_local_clone_stats(conn: &Connection, workspace_id: i64) -> Result<Value, String> {
    let row = conn
        .query_row(
            "SELECT COUNT(*) as total,
                    SUM(CASE WHEN clone_type = 1 THEN 1 ELSE 0 END) as type1,
                    SUM(CASE WHEN clone_type = 2 THEN 1 ELSE 0 END) as type2,
                    SUM(CASE WHEN clone_type = 3 THEN 1 ELSE 0 END) as type3
             FROM clone_pairs WHERE workspace_id = ?1",
            [workspace_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, Option<i64>>(1)?,
                    row.get::<_, Option<i64>>(2)?,
                    row.get::<_, Option<i64>>(3)?,
                ))
            },
        )
        .map_err(|error| format!("cannot query clone stats: {error}"))?;
    let (total, type1, type2, type3) = row;

    // 受影响文件数（distinct fi.id）与受影响符号数（distinct s.id）
    let (affected_files, affected_symbols) = conn
        .query_row(
            "SELECT COUNT(DISTINCT fi.id) as files, COUNT(DISTINCT s.id) as syms
             FROM clone_pairs cp
             JOIN symbols s ON (cp.symbol_a_id = s.id OR cp.symbol_b_id = s.id)
             JOIN file_instances fi ON s.file_instance_id = fi.id
             WHERE cp.workspace_id = ?1",
            [workspace_id],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, i64>(1)?)),
        )
        .map_err(|error| format!("cannot query clone affected files: {error}"))?;

    Ok(json!({
        "total": total,
        "type1": type1.unwrap_or(0),
        "type2": type2.unwrap_or(0),
        "type3": type3.unwrap_or(0),
        "affected_files": affected_files,
        "affected_symbols": affected_symbols,
    }))
}

/// 查询当前 workspace 任务统计（复刻 Python `get_job_stats`）。
///
/// jobs 为全局任务表但每行绑定 workspace_id，统计按 workspace 隔离；
/// 返回 {pending, running, completed, cancelled, failed, total}，状态集合
/// 与 Python db_jobs.py JOB_PENDING/RUNNING/COMPLETED/CANCELLED/FAILED 一致。
fn query_local_job_stats(conn: &Connection, workspace_id: i64) -> Result<Value, String> {
    let mut stats = serde_json::Map::new();
    for status in ["pending", "running", "completed", "cancelled", "failed"] {
        stats.insert(status.into(), Value::Number(0.into()));
    }
    let mut stmt = conn
        .prepare(
            "SELECT status, COUNT(*) as cnt
             FROM jobs WHERE workspace_id = ?1
             GROUP BY status",
        )
        .map_err(|error| format!("cannot prepare job stats query: {error}"))?;
    let rows = stmt
        .query_map([workspace_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(|error| format!("cannot query job stats: {error}"))?;
    for row in rows {
        let (status, cnt) = row.map_err(|error| format!("cannot read job stats row: {error}"))?;
        stats.insert(status, Value::Number(cnt.into()));
    }
    let total: i64 = stats.values().filter_map(Value::as_i64).sum();
    stats.insert("total".into(), Value::Number(total.into()));
    Ok(Value::Object(stats))
}

// W3-2（T-1786861820151-f3cecf40）：job 读组查询函数（复刻 Python
// db_jobs.py 的 `_row_to_job` / `get_job` / `list_jobs` + Job.to_dict()）。

/// jobs 行 → Job.to_dict()（asdict 全字段）。
///
/// params / result_summary 为 JSON 文本，解析失败 → 空对象（复刻 Python
/// `_row_to_job` 中 json.loads 的 try/except 回退）；cancel_requested 为
/// 0/1 → bool（复刻 `bool(row["cancel_requested"])`）。
fn job_row_to_dict(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let id: i64 = row.get(0)?;
    let job_id: String = row.get(1)?;
    let workspace_id: i64 = row.get(2)?;
    let job_type: String = row.get(3)?;
    let status: String = row.get(4)?;
    let progress: f64 = row.get(5)?;
    let message: String = row.get(6)?;
    let params_raw: String = row.get(7)?;
    let result_raw: String = row.get(8)?;
    let error: String = row.get(9)?;
    let cancel_requested: i64 = row.get(10)?;
    let created_at: f64 = row.get(11)?;
    let started_at: f64 = row.get(12)?;
    let finished_at: f64 = row.get(13)?;

    let params: Value = serde_json::from_str(&params_raw).unwrap_or_else(|_| json!({}));
    let result_summary: Value = serde_json::from_str(&result_raw).unwrap_or_else(|_| json!({}));

    let mut m = Map::new();
    m.insert("id".into(), Value::Number(id.into()));
    m.insert("job_id".into(), Value::String(job_id));
    m.insert("workspace_id".into(), Value::Number(workspace_id.into()));
    m.insert("job_type".into(), Value::String(job_type));
    m.insert("status".into(), Value::String(status));
    m.insert(
        "progress".into(),
        Value::Number(serde_json::Number::from_f64(progress).unwrap_or_else(|| 0.into())),
    );
    m.insert("message".into(), Value::String(message));
    m.insert("params".into(), params);
    m.insert("result_summary".into(), result_summary);
    m.insert("error".into(), Value::String(error));
    m.insert(
        "cancel_requested".into(),
        Value::Bool(cancel_requested != 0),
    );
    m.insert(
        "created_at".into(),
        Value::Number(serde_json::Number::from_f64(created_at).unwrap_or_else(|| 0.into())),
    );
    m.insert(
        "started_at".into(),
        Value::Number(serde_json::Number::from_f64(started_at).unwrap_or_else(|| 0.into())),
    );
    m.insert(
        "finished_at".into(),
        Value::Number(serde_json::Number::from_f64(finished_at).unwrap_or_else(|| 0.into())),
    );
    Ok(Value::Object(m))
}

/// 查询单个 job（复刻 Python `get_job`，额外按 workspace_id 限定实现
/// 跨 workspace 隔离；不存在返回 None）。
fn query_local_get_job(
    conn: &Connection,
    workspace_id: i64,
    job_id: &str,
) -> Result<Option<Value>, String> {
    let sql = "SELECT id, job_id, workspace_id, job_type, status, progress, message,
               params, result_summary, error, cancel_requested, created_at,
               started_at, finished_at
               FROM jobs
               WHERE job_id = ?1 AND workspace_id = ?2";
    match conn.query_row(sql, params![job_id, workspace_id], job_row_to_dict) {
        Ok(v) => Ok(Some(v)),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(None),
        Err(error) => Err(format!("cannot query job: {error}")),
    }
}

/// 列出 job（复刻 Python `list_jobs`：WHERE workspace_id [+ job_type]
/// [+ status] ORDER BY created_at DESC LIMIT ?），返回 Job.to_dict() 列表。
fn query_local_list_jobs(
    conn: &Connection,
    workspace_id: i64,
    job_type: Option<&str>,
    status: Option<&str>,
    limit: i64,
) -> Result<Value, String> {
    let mut sql = String::from(
        "SELECT id, job_id, workspace_id, job_type, status, progress, message,
         params, result_summary, error, cancel_requested, created_at,
         started_at, finished_at
         FROM jobs WHERE workspace_id = ?1",
    );
    let mut values: Vec<rusqlite::types::Value> =
        vec![rusqlite::types::Value::Integer(workspace_id)];
    if let Some(job_type) = job_type {
        sql.push_str(" AND job_type = ?");
        values.push(rusqlite::types::Value::Text(job_type.to_string()));
    }
    if let Some(status) = status {
        sql.push_str(" AND status = ?");
        values.push(rusqlite::types::Value::Text(status.to_string()));
    }
    sql.push_str(" ORDER BY created_at DESC LIMIT ?");
    values.push(rusqlite::types::Value::Integer(limit));
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare list jobs query: {error}"))?;
    let rows = stmt
        .query_map(params_from_iter(values.iter()), job_row_to_dict)
        .map_err(|error| format!("cannot query list jobs: {error}"))?
        .collect::<Result<Vec<Value>, _>>()
        .map_err(|error| format!("cannot read list jobs row: {error}"))?;
    Ok(Value::Array(rows))
}

/// 判断 job 是否处于终态（completed/cancelled/failed，复刻 Python
/// `Job.is_terminal` 的 `_TERMINAL_STATES` 集合）。
fn is_job_terminal(job: &Value) -> bool {
    matches!(
        job.get("status").and_then(Value::as_str),
        Some("completed") | Some("cancelled") | Some("failed")
    )
}

/// 构造 wait_for_job 返回对象（终态/超时共用字段，复刻 Python
/// wait_for_job 的返回结构）。
fn job_wait_result_map(job_id: &str, job: &Value, elapsed: f64) -> Map<String, Value> {
    let mut m = Map::new();
    m.insert("job_id".into(), Value::String(job_id.to_string()));
    m.insert(
        "status".into(),
        job.get("status")
            .cloned()
            .unwrap_or_else(|| Value::String(String::new())),
    );
    m.insert(
        "progress".into(),
        job.get("progress").cloned().unwrap_or_else(|| 0.into()),
    );
    m.insert(
        "result_summary".into(),
        job.get("result_summary")
            .cloned()
            .unwrap_or_else(|| json!({})),
    );
    m.insert(
        "error".into(),
        job.get("error")
            .cloned()
            .unwrap_or_else(|| Value::String(String::new())),
    );
    m.insert(
        "elapsed".into(),
        Value::Number(serde_json::Number::from_f64(elapsed).unwrap_or_else(|| 0.into())),
    );
    m
}

/// 查询当前 workspace clone groups 统计（复刻 Python `get_clone_group_stats`）。
///
/// 返回 {total_groups, type1, type2, type3, total_members, affected_files,
/// affected_symbols}；SUM(CASE...)/SUM(member_count) 无匹配行时为 NULL，
/// 对齐 Python `row[...] or 0` 语义。
fn query_local_clone_group_stats(conn: &Connection, workspace_id: i64) -> Result<Value, String> {
    let row = conn
        .query_row(
            "SELECT COUNT(*) as total_groups,
                    SUM(CASE WHEN clone_type = 1 THEN 1 ELSE 0 END) as type1,
                    SUM(CASE WHEN clone_type = 2 THEN 1 ELSE 0 END) as type2,
                    SUM(CASE WHEN clone_type = 3 THEN 1 ELSE 0 END) as type3,
                    SUM(member_count) as total_members
             FROM clone_groups WHERE workspace_id = ?1",
            [workspace_id],
            |row| {
                Ok((
                    row.get::<_, i64>(0)?,
                    row.get::<_, Option<i64>>(1)?,
                    row.get::<_, Option<i64>>(2)?,
                    row.get::<_, Option<i64>>(3)?,
                    row.get::<_, Option<i64>>(4)?,
                ))
            },
        )
        .map_err(|error| format!("cannot query clone group stats: {error}"))?;
    let (total_groups, type1, type2, type3, total_members) = row;

    // 受影响符号数（distinct symbol_id）
    let affected_symbols: i64 = conn
        .query_row(
            "SELECT COUNT(DISTINCT m.symbol_id) as sym_cnt
             FROM clone_group_members m
             JOIN clone_groups g ON m.group_id = g.id
             WHERE g.workspace_id = ?1",
            [workspace_id],
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot query clone group syms: {error}"))?;

    // 受影响文件数（distinct file_instance_id）
    let affected_files: i64 = conn
        .query_row(
            "SELECT COUNT(DISTINCT fi.id) as file_cnt
             FROM clone_group_members m
             JOIN clone_groups g ON m.group_id = g.id
             JOIN symbols s ON m.symbol_id = s.id
             JOIN file_instances fi ON s.file_instance_id = fi.id
             WHERE g.workspace_id = ?1",
            [workspace_id],
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot query clone group files: {error}"))?;

    Ok(json!({
        "total_groups": total_groups,
        "type1": type1.unwrap_or(0),
        "type2": type2.unwrap_or(0),
        "type3": type3.unwrap_or(0),
        "total_members": total_members.unwrap_or(0),
        "affected_files": affected_files,
        "affected_symbols": affected_symbols,
    }))
}

// ============================================
// W2-3（T-1786840097331-fd01a3f8）：defect/edit stats 只读查询函数
// ============================================
// 两个查询复刻 Python db 层语义（db_defect_kb.py `defect_stats` /
// db_edit.py `get_edit_stats`），在 snapshot query_db_path（主库只读连接）
// 上执行。注意两个统计均为全局视图（与 Python 语义一致）：
// - defect_patterns / defect_fixes 无 workspace_id 列 → 无过滤；
// - file_edit_audit 虽有 workspace_id 列，但 Python `get_edit_stats` SQL
//   无 workspace 过滤 → Rust 同样不加 WHERE。

/// 解析时间窗口字符串为 Unix 时间戳（复刻 Python `_parse_time_window`）。
///
/// 语义与 db_edit.py:965-1006 逐条对齐：
/// - 空 / "all"（大小写不敏感）→ 0.0（不过滤）
/// - "N" + 单位（d/w/h/y）→ now - N * 单位秒数
/// - ISO 日期（YYYY-MM-DD[ T HH:MM:SS]）→ 本地时区该时刻时间戳，
///   委托 SQLite `strftime('%s', ?, 'localtime')`（naive datetime 解释为
///   本地时区，与 Python `datetime.fromisoformat().timestamp()` 语义一致）
/// - 无法解析 / SQLite 返回 NULL → 0.0（不过滤）
fn parse_time_window(conn: &Connection, time_window: &str) -> f64 {
    if time_window.is_empty() || time_window.eq_ignore_ascii_case("all") {
        return 0.0;
    }
    // 数字 + 单位格式（如 30d / 7d / 24h / 1y）
    let bytes = time_window.as_bytes();
    let last = bytes.last().copied().unwrap_or(0);
    if bytes.len() >= 2
        && last.is_ascii_alphabetic()
        && bytes[..bytes.len() - 1].iter().all(|b| b.is_ascii_digit())
    {
        if let Ok(num) = time_window[..time_window.len() - 1].parse::<f64>() {
            let now = std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            let unit = (last as char).to_ascii_lowercase();
            let seconds = match unit {
                'd' => num * 86400.0,
                'w' => num * 7.0 * 86400.0,
                'h' => num * 3600.0,
                'y' => num * 365.0 * 86400.0,
                _ => return 0.0,
            };
            return now - seconds;
        }
        return 0.0;
    }
    // ISO 日期格式（YYYY-MM-DD / YYYY-MM-DDTHH:MM:SS）：委托 SQLite
    // strftime('%s', ?, 'localtime') 把 naive datetime 解释为本地时区。
    conn.query_row(
        "SELECT strftime('%s', ?1, 'localtime')",
        [time_window],
        |row| row.get::<_, Option<String>>(0),
    )
    .ok()
    .flatten()
    .and_then(|s| s.parse::<f64>().ok())
    .unwrap_or(0.0)
}

/// 查询缺陷知识库统计（复刻 Python `defect_stats`）。
///
/// 返回 {total_patterns, total_fixes, by_category, by_severity,
/// avg_effectiveness, top_defects}；AVG 无匹配行时为 NULL，对齐 Python
/// `avg_eff if avg_eff is not None else 0.0` 语义；top_defects 为
/// case_count 倒序前 10。
fn query_local_defect_stats(conn: &Connection) -> Result<Value, String> {
    // 模式总数
    let total_patterns: i64 = conn
        .query_row("SELECT COUNT(*) as cnt FROM defect_patterns", [], |row| {
            row.get(0)
        })
        .map_err(|error| format!("cannot query defect total patterns: {error}"))?;
    // 修复总数
    let total_fixes: i64 = conn
        .query_row("SELECT COUNT(*) as cnt FROM defect_fixes", [], |row| {
            row.get(0)
        })
        .map_err(|error| format!("cannot query defect total fixes: {error}"))?;

    // 按类别分布（GROUP BY category ORDER BY cnt DESC）
    let mut by_category = Map::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT category, COUNT(*) as cnt FROM defect_patterns
                 GROUP BY category ORDER BY cnt DESC",
            )
            .map_err(|error| format!("cannot prepare defect category query: {error}"))?;
        let rows = stmt
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })
            .map_err(|error| format!("cannot query defect category: {error}"))?;
        for row in rows {
            let (key, cnt) =
                row.map_err(|error| format!("cannot read defect category row: {error}"))?;
            by_category.insert(key, Value::Number(cnt.into()));
        }
    }

    // 按严重度分布（GROUP BY severity ORDER BY cnt DESC）
    let mut by_severity = Map::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT severity, COUNT(*) as cnt FROM defect_patterns
                 GROUP BY severity ORDER BY cnt DESC",
            )
            .map_err(|error| format!("cannot prepare defect severity query: {error}"))?;
        let rows = stmt
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })
            .map_err(|error| format!("cannot query defect severity: {error}"))?;
        for row in rows {
            let (key, cnt) =
                row.map_err(|error| format!("cannot read defect severity row: {error}"))?;
            by_severity.insert(key, Value::Number(cnt.into()));
        }
    }

    // 平均有效性（AVG 无行时 NULL → 0.0）
    let avg_effectiveness: f64 = conn
        .query_row(
            "SELECT AVG(effectiveness) as avg_eff FROM defect_fixes",
            [],
            |row| row.get::<_, Option<f64>>(0),
        )
        .map_err(|error| format!("cannot query defect avg effectiveness: {error}"))?
        .unwrap_or(0.0);

    // 最常见缺陷 Top 10（case_count 倒序）
    let mut top_defects = Vec::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT pattern_id, category, description, detection_rule, severity, case_count
                 FROM defect_patterns
                 ORDER BY case_count DESC
                 LIMIT 10",
            )
            .map_err(|error| format!("cannot prepare defect top query: {error}"))?;
        let rows = stmt
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                    row.get::<_, i64>(5)?,
                ))
            })
            .map_err(|error| format!("cannot query defect top: {error}"))?;
        for row in rows {
            let (pattern_id, category, description, detection_rule, severity, case_count) =
                row.map_err(|error| format!("cannot read defect top row: {error}"))?;
            top_defects.push(json!({
                "pattern_id": pattern_id,
                "category": category,
                "description": description,
                "detection_rule": detection_rule,
                "severity": severity,
                "case_count": case_count,
            }));
        }
    }

    Ok(json!({
        "total_patterns": total_patterns,
        "total_fixes": total_fixes,
        "by_category": by_category,
        "by_severity": by_severity,
        "avg_effectiveness": avg_effectiveness,
        "top_defects": top_defects,
    }))
}

/// 查询文件编辑统计（复刻 Python `get_edit_stats`，time_window 解析对齐
/// `_parse_time_window`）。
///
/// 返回 {time_window, total, by_status, by_operation, revert_rate}；
/// by_status 四桶（pending/applied/reverted/failed）初始 0，
/// by_operation 三桶（edit/create/delete）初始 0，total 累加；
/// revert_rate = reverted/(applied+reverted)（denom=0 → 0.0，round 4）。
fn query_local_edit_stats(conn: &Connection, time_window: &str) -> Result<Value, String> {
    let since_ts = parse_time_window(conn, time_window);

    // 按状态分组统计（since_ts > 0 时带 created_at >= ? 过滤）
    let mut by_status = serde_json::Map::new();
    for status in ["pending", "applied", "reverted", "failed"] {
        by_status.insert(status.into(), Value::Number(0.into()));
    }
    let mut total: i64 = 0;
    {
        let sql = if since_ts > 0.0 {
            "SELECT status, COUNT(*) as cnt FROM file_edit_audit
             WHERE created_at >= ?1 GROUP BY status"
        } else {
            "SELECT status, COUNT(*) as cnt FROM file_edit_audit GROUP BY status"
        };
        let mut stmt = conn
            .prepare(sql)
            .map_err(|error| format!("cannot prepare edit status query: {error}"))?;
        let rows = if since_ts > 0.0 {
            stmt.query_map([since_ts], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })
            .map_err(|error| format!("cannot query edit status: {error}"))?
            .collect::<Result<Vec<(String, i64)>, _>>()
            .map_err(|error| format!("cannot read edit status row: {error}"))?
        } else {
            stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })
            .map_err(|error| format!("cannot query edit status: {error}"))?
            .collect::<Result<Vec<(String, i64)>, _>>()
            .map_err(|error| format!("cannot read edit status row: {error}"))?
        };
        for (status, cnt) in rows {
            by_status.insert(status, Value::Number(cnt.into()));
            total += cnt;
        }
    }

    // 按操作类型分组统计
    let mut by_operation = serde_json::Map::new();
    for op in ["edit", "create", "delete"] {
        by_operation.insert(op.into(), Value::Number(0.into()));
    }
    {
        let sql = if since_ts > 0.0 {
            "SELECT operation, COUNT(*) as cnt FROM file_edit_audit
             WHERE created_at >= ?1 GROUP BY operation"
        } else {
            "SELECT operation, COUNT(*) as cnt FROM file_edit_audit GROUP BY operation"
        };
        let mut stmt = conn
            .prepare(sql)
            .map_err(|error| format!("cannot prepare edit operation query: {error}"))?;
        let rows = if since_ts > 0.0 {
            stmt.query_map([since_ts], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })
            .map_err(|error| format!("cannot query edit operation: {error}"))?
            .collect::<Result<Vec<(String, i64)>, _>>()
            .map_err(|error| format!("cannot read edit operation row: {error}"))?
        } else {
            stmt.query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
            })
            .map_err(|error| format!("cannot query edit operation: {error}"))?
            .collect::<Result<Vec<(String, i64)>, _>>()
            .map_err(|error| format!("cannot read edit operation row: {error}"))?
        };
        for (op, cnt) in rows {
            by_operation.insert(op, Value::Number(cnt.into()));
        }
    }

    // 回滚率 = reverted / (applied + reverted)，避免除零，round 4
    let applied_count = by_status
        .get("applied")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let reverted_count = by_status
        .get("reverted")
        .and_then(Value::as_i64)
        .unwrap_or(0);
    let denom = applied_count + reverted_count;
    let revert_rate = if denom > 0 {
        (reverted_count as f64 / denom as f64 * 10000.0).round() / 10000.0
    } else {
        0.0
    };

    Ok(json!({
        "time_window": time_window,
        "total": total,
        "by_status": by_status,
        "by_operation": by_operation,
        "revert_rate": revert_rate,
    }))
}

// W4-3（T-1786886251769-22b94ee8-sub-3）：defect 读组 5 工具查询函数。
// 语义逐条复刻 Python db 层（db_evolution.py defect_correlation /
// get_defect_correlation_by_qn / churn_analysis + db_defect_kb.py
// defect_pattern_search / suggest_fix）。数据源全在主库，经 snapshot
// query_db_path 只读连接访问。serde_json 已启用 preserve_order，输出键序
// 与 Python dict 插入序一致；NULL 列 Option 化对齐 Python `dict(row)`
// 的 NULL → JSON null 语义。

/// 变更点（复刻 Python `defect_correlation` 变更点行中实际使用的字段）。
struct DefectChangePoint {
    file_instance_id: i64,
    version_num: i64,
    parsed_at: f64,
}

/// semgrep_findings 行（复刻 Python `defect_correlation` 输出用字段子集）。
struct DefectFindingRow {
    id: i64,
    rule_id: Option<String>,
    rule_name: Option<String>,
    severity: Option<String>,
    start_line: Option<i64>,
    end_line: Option<i64>,
    scanned_at: Option<f64>,
    message: Option<String>,
}

/// 解析演化层时间窗口（复刻 Python `db_evolution.py:_parse_time_window`）。
///
/// 与 `parse_time_window`（d/w/h/y、无空白容忍）不同，本函数正则
/// `^\s*(\d+)\s*([dwmy])\s*$`：允许前导/中间/尾部空白，单位仅 d/w/m/y
/// （m = 30*86400，按 30 天近似）。无法匹配 → 0.0（无限制）。
fn parse_time_window_evolution(time_window: &str) -> f64 {
    if time_window.is_empty() {
        return 0.0;
    }
    let bytes = time_window.as_bytes();
    let n = bytes.len();
    let mut i = 0;
    // \s* 前导空白
    while i < n && (bytes[i] as char).is_whitespace() {
        i += 1;
    }
    // (\d+) 数字
    let digits_start = i;
    while i < n && bytes[i].is_ascii_digit() {
        i += 1;
    }
    if i == digits_start {
        return 0.0;
    }
    let num_str = &time_window[digits_start..i];
    // \s* 中间空白
    while i < n && (bytes[i] as char).is_whitespace() {
        i += 1;
    }
    if i >= n {
        return 0.0;
    }
    // ([dwmy]) 单位
    let unit = bytes[i];
    i += 1;
    // \s*$ 尾部空白
    while i < n && (bytes[i] as char).is_whitespace() {
        i += 1;
    }
    if i != n {
        return 0.0;
    }
    // Python 正则 `([dwmy])` 仅匹配小写单位，大写（如 "90D"）整体不匹配 →
    // 0.0（无限制），此处不复刻 to_ascii_lowercase，保持与 Python 一致
    let seconds = match unit as char {
        'd' => 86400.0,
        'w' => 7.0 * 86400.0,
        'm' => 30.0 * 86400.0,
        'y' => 365.0 * 86400.0,
        _ => return 0.0,
    };
    let num: u64 = match num_str.parse() {
        Ok(v) => v,
        Err(_) => return 0.0,
    };
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    now - (num as f64) * seconds
}

/// 标准化严重度（复刻 Python `db_defect_kb.py:_normalize_severity`）：
/// 空 → "info"，否则小写 + 去首尾空白。
fn normalize_defect_severity(sev: &str) -> String {
    if sev.is_empty() {
        "info".to_string()
    } else {
        sev.to_lowercase().trim().to_string()
    }
}

/// 复刻 Python `time.strftime("%Y-%m-%d", time.localtime(ts))`：委托
/// SQLite strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') 本地时区格式化。
fn local_date_key(conn: &Connection, ts: f64) -> String {
    conn.query_row(
        "SELECT strftime('%Y-%m-%d', ?1, 'unixepoch', 'localtime')",
        [ts],
        |row| row.get::<_, String>(0),
    )
    .unwrap_or_default()
}

/// 查询符号变更-缺陷关联（复刻 Python `db_evolution.py:defect_correlation`
/// 的 Python 全路径；Phase 6-1 Rust 短路 `_defect_correlation_via_rust` 的
/// SQL 全在 Python 侧，daemon 端按全路径执行）。
///
/// 返回 {symbol_hash, total_changes, defects_after_change, defect_types,
/// findings}；findings 顺序 = 变更点遍历顺序（file_instance 首次出现序，
/// 即 ORDER BY file_instance_id, version_num 行序）+ 各窗口 finding 行序
/// + symbol_qualified 补充段行序，与 Python list 插入序一致。负
/// window_commits → 窗口切片为空（Python 切片语义，不报错）。
fn query_local_defect_correlation(
    conn: &Connection,
    workspace_id: i64,
    symbol_hash: &str,
    window_commits: i64,
) -> Result<Value, String> {
    // 符号的 qualified_name（用于补充匹配 semgrep_findings.symbol_qualified）
    let qualified_name: String = conn
        .query_row(
            "SELECT content_hash, qualified_name FROM symbol_contents WHERE content_hash = ?1",
            [symbol_hash],
            |row| row.get::<_, Option<String>>(1),
        )
        .optional()
        .map_err(|error| format!("cannot query defect correlation symbol: {error}"))?
        .flatten()
        .unwrap_or_default();

    // 该符号出现过的所有 file_version（变更点），按 file_instance 分组，
    // 保持 Python 变更点查询行序（ORDER BY file_instance_id, version_num）
    let mut changes: Vec<(i64, Vec<DefectChangePoint>)> = Vec::new();
    {
        let mut stmt = conn
            .prepare(
                "SELECT fv.id as fv_id, fv.file_instance_id, fv.version_num, \
                 fv.content_hash, fv.parsed_at \
                 FROM file_symbol_versions fsv \
                 JOIN file_versions fv ON fsv.file_version_id = fv.id \
                 JOIN file_instances fi ON fv.file_instance_id = fi.id \
                 WHERE fi.workspace_id = ?1 AND fsv.symbol_hash = ?2 \
                 ORDER BY fv.file_instance_id, fv.version_num ASC",
            )
            .map_err(|error| format!("cannot prepare defect correlation changes: {error}"))?;
        let rows = stmt
            .query_map(params![workspace_id, symbol_hash], |row| {
                Ok(DefectChangePoint {
                    file_instance_id: row.get(1)?,
                    version_num: row.get(2)?,
                    parsed_at: row.get(4)?,
                })
            })
            .map_err(|error| format!("cannot query defect correlation changes: {error}"))?;
        for row in rows {
            let cp = row
                .map_err(|error| format!("cannot read defect correlation change: {error}"))?;
            if let Some(entry) = changes
                .iter_mut()
                .find(|(fid, _)| *fid == cp.file_instance_id)
            {
                entry.1.push(cp);
            } else {
                changes.push((cp.file_instance_id, vec![cp]));
            }
        }
    }
    let total_changes: i64 = changes.iter().map(|(_, v)| v.len() as i64).sum();

    let mut defect_findings: Vec<Value> = Vec::new();
    let mut defect_types: Map<String, Value> = Map::new();
    let mut seen_finding_ids: HashSet<i64> = HashSet::new();

    for (file_instance_id, change_versions) in &changes {
        // 该 file_instance 所有版本（按版本号排序），构建 version_num → 索引
        let mut all_versions: Vec<(i64, i64, Option<String>, f64)> = Vec::new();
        {
            let mut stmt = conn
                .prepare(
                    "SELECT id, version_num, content_hash, parsed_at \
                     FROM file_versions WHERE file_instance_id = ?1 \
                     ORDER BY version_num ASC",
                )
                .map_err(|error| {
                    format!("cannot prepare defect correlation versions: {error}")
                })?;
            let rows = stmt
                .query_map([*file_instance_id], |row| {
                    Ok((
                        row.get::<_, i64>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, Option<String>>(2)?,
                        row.get::<_, f64>(3)?,
                    ))
                })
                .map_err(|error| format!("cannot query defect correlation versions: {error}"))?;
            for row in rows {
                all_versions.push(row.map_err(|error| {
                    format!("cannot read defect correlation version: {error}")
                })?);
            }
        }
        let version_num_to_index: HashMap<i64, usize> = all_versions
            .iter()
            .enumerate()
            .map(|(idx, v)| (v.1, idx))
            .collect();

        for change in change_versions {
            let idx = match version_num_to_index.get(&change.version_num) {
                Some(idx) => *idx,
                None => continue, // Python: idx is None → continue
            };
            // 窗口：idx+1 到 idx+1+window_commits（负 window_commits → 空切片）
            let start = (idx + 1) as i128;
            let end = start + window_commits as i128;
            let s = start as usize;
            let e = (end.max(start) as usize).min(all_versions.len());
            let mut window_hashes: Vec<String> = Vec::new();
            for v in &all_versions[s..e] {
                if let Some(h) = &v.2 {
                    if !h.is_empty() {
                        window_hashes.push(h.clone());
                    }
                }
            }
            if window_hashes.is_empty() {
                continue; // Python: if not window_hashes: continue
            }
            let placeholders = vec!["?"; window_hashes.len()].join(",");
            let sql = format!(
                "SELECT id, rule_id, rule_name, severity, start_line, end_line, \
                 content_hash, scanned_at, symbol_qualified, message \
                 FROM semgrep_findings \
                 WHERE file_instance_id = ? AND content_hash IN ({placeholders})"
            );
            let mut stmt = conn
                .prepare(&sql)
                .map_err(|error| format!("cannot prepare defect correlation findings: {error}"))?;
            let mut bind: Vec<rusqlite::types::Value> =
                Vec::with_capacity(window_hashes.len() + 1);
            bind.push(rusqlite::types::Value::Integer(*file_instance_id));
            for h in &window_hashes {
                bind.push(rusqlite::types::Value::Text(h.clone()));
            }
            let rows = stmt
                .query_map(params_from_iter(bind), |row| {
                    Ok(DefectFindingRow {
                        id: row.get(0)?,
                        rule_id: row.get::<_, Option<String>>(1)?,
                        rule_name: row.get::<_, Option<String>>(2)?,
                        severity: row.get::<_, Option<String>>(3)?,
                        start_line: row.get::<_, Option<i64>>(4)?,
                        end_line: row.get::<_, Option<i64>>(5)?,
                        scanned_at: row.get::<_, Option<f64>>(7)?,
                        message: row.get::<_, Option<String>>(9)?,
                    })
                })
                .map_err(|error| {
                    format!("cannot query defect correlation findings: {error}")
                })?;
            for row in rows {
                let f = row.map_err(|error| {
                    format!("cannot read defect correlation finding: {error}")
                })?;
                if !seen_finding_ids.insert(f.id) {
                    continue;
                }
                defect_types
                    .entry(f.rule_id.clone().unwrap_or_default())
                    .and_modify(|c| *c = (c.as_i64().unwrap_or(0) + 1).into())
                    .or_insert(Value::Number(1.into()));
                defect_findings.push(json!({
                    "rule_id": f.rule_id,
                    "rule_name": f.rule_name,
                    "severity": f.severity,
                    "start_line": f.start_line,
                    "end_line": f.end_line,
                    "scanned_at": f.scanned_at,
                    "message": f.message,
                    "after_change_at": change.parsed_at,
                }));
            }
        }
    }

    // 补充：通过 symbol_qualified 直接关联的缺陷（不局限于窗口）
    if !qualified_name.is_empty() {
        let mut stmt = conn
            .prepare(
                "SELECT id, rule_id, rule_name, severity, start_line, end_line, \
                 content_hash, scanned_at, symbol_qualified, message \
                 FROM semgrep_findings WHERE symbol_qualified = ?1",
            )
            .map_err(|error| {
                format!("cannot prepare defect correlation qualified: {error}")
            })?;
        let rows = stmt
            .query_map([&qualified_name], |row| {
                Ok(DefectFindingRow {
                    id: row.get(0)?,
                    rule_id: row.get::<_, Option<String>>(1)?,
                    rule_name: row.get::<_, Option<String>>(2)?,
                    severity: row.get::<_, Option<String>>(3)?,
                    start_line: row.get::<_, Option<i64>>(4)?,
                    end_line: row.get::<_, Option<i64>>(5)?,
                    scanned_at: row.get::<_, Option<f64>>(7)?,
                    message: row.get::<_, Option<String>>(9)?,
                })
            })
            .map_err(|error| {
                format!("cannot query defect correlation qualified: {error}")
            })?;
        for row in rows {
            let f = row.map_err(|error| {
                format!("cannot read defect correlation qualified: {error}")
            })?;
            if !seen_finding_ids.insert(f.id) {
                continue;
            }
            defect_types
                .entry(f.rule_id.clone().unwrap_or_default())
                .and_modify(|c| *c = (c.as_i64().unwrap_or(0) + 1).into())
                .or_insert(Value::Number(1.into()));
            defect_findings.push(json!({
                "rule_id": f.rule_id,
                "rule_name": f.rule_name,
                "severity": f.severity,
                "start_line": f.start_line,
                "end_line": f.end_line,
                "scanned_at": f.scanned_at,
                "message": f.message,
                "after_change_at": 0.0,
            }));
        }
    }

    Ok(json!({
        "symbol_hash": symbol_hash,
        "total_changes": total_changes,
        "defects_after_change": defect_findings.len(),
        "defect_types": defect_types,
        "findings": defect_findings,
    }))
}

/// git_file_changes 行（复刻 Python `churn_analysis` gfc 分支用字段）。
struct ChurnGfcRow {
    file_instance_id: i64,
    lines_added: Option<i64>,
    lines_deleted: Option<i64>,
    commit_ts: Option<f64>,
    rel_path: String,
}

/// file_versions 行（复刻 Python `churn_analysis` fallback 分支用字段）。
struct ChurnFvRow {
    file_instance_id: i64,
    total_lines: Option<i64>,
    parsed_at: f64,
    rel_path: String,
}

/// 按文件累计的 churn 统计（复刻 Python `by_file` dict 值）。
struct ChurnFileAcc {
    rel_path: String,
    change_count: i64,
    churned_lines: i64,
}

/// 查询代码流失分析（复刻 Python `db_evolution.py:churn_analysis`）。
///
/// 优先 git_file_changes（真实行数，LEFT JOIN git_commits 取 timestamp），
/// 无数据时 fallback file_versions 相邻版本 total_lines 差值近似。gfc 趋势
/// 分桶仅当 commit_ts 非 0（Python `if ts`，0.0 falsy）且 file_churn > 0；
/// fallback 趋势基于 parsed_at 且 diff > 0。churn_rate 整数除法 round 4。
/// top_files 按 churned_lines 降序前 10（稳定排序对齐 Python sorted
/// reverse=True）；trend 按键字典序（Python sorted(trend_buckets.items())，
/// BTreeMap 保证，日期 key 字典序 = 时间序）。
fn query_local_churn_analysis(
    conn: &Connection,
    workspace_id: i64,
    module_filter: &str,
    time_window: &str,
) -> Result<Value, String> {
    let cutoff = parse_time_window_evolution(time_window);

    // ---- 优先 git_file_changes（真实行数）----
    let mut sql_gfc = String::from(
        "SELECT gfc.file_instance_id, gfc.lines_added, gfc.lines_deleted, \
         gc.timestamp as commit_ts, fi.rel_path, fi.module_path \
         FROM git_file_changes gfc \
         JOIN file_instances fi ON gfc.file_instance_id = fi.id \
         LEFT JOIN git_commits gc ON gfc.commit_hash = gc.commit_hash \
         WHERE fi.workspace_id = ?",
    );
    let mut bind_gfc: Vec<rusqlite::types::Value> =
        vec![rusqlite::types::Value::Integer(workspace_id)];
    if !module_filter.is_empty() {
        sql_gfc.push_str(" AND fi.module_path LIKE ?");
        bind_gfc.push(rusqlite::types::Value::Text(format!("{module_filter}%")));
    }
    if cutoff > 0.0 {
        sql_gfc.push_str(" AND gc.timestamp >= ?");
        bind_gfc.push(rusqlite::types::Value::Real(cutoff));
    }
    let mut gfc_rows: Vec<ChurnGfcRow> = Vec::new();
    {
        let mut stmt = conn
            .prepare(&sql_gfc)
            .map_err(|error| format!("cannot prepare churn gfc query: {error}"))?;
        let rows = stmt
            .query_map(params_from_iter(bind_gfc), |row| {
                Ok(ChurnGfcRow {
                    file_instance_id: row.get(0)?,
                    lines_added: row.get::<_, Option<i64>>(1)?,
                    lines_deleted: row.get::<_, Option<i64>>(2)?,
                    commit_ts: row.get::<_, Option<f64>>(3)?,
                    rel_path: row.get::<_, String>(4)?,
                })
            })
            .map_err(|error| format!("cannot query churn gfc: {error}"))?;
        for row in rows {
            gfc_rows.push(
                row.map_err(|error| format!("cannot read churn gfc row: {error}"))?,
            );
        }
    }

    if !gfc_rows.is_empty() {
        let mut by_file: Vec<(i64, ChurnFileAcc)> = Vec::new();
        let mut total_churned_lines: i64 = 0;
        let mut changed_files: i64 = 0;
        let mut trend_buckets: BTreeMap<String, i64> = BTreeMap::new();

        for row in &gfc_rows {
            let fid = row.file_instance_id;
            let added = row.lines_added.unwrap_or(0);
            let deleted = row.lines_deleted.unwrap_or(0);
            let file_churn = added + deleted;

            if let Some(entry) = by_file.iter_mut().find(|(fid_, _)| *fid_ == fid) {
                entry.1.change_count += 1;
                entry.1.churned_lines += file_churn;
            } else {
                by_file.push((
                    fid,
                    ChurnFileAcc {
                        rel_path: row.rel_path.clone(),
                        change_count: 1,
                        churned_lines: file_churn,
                    },
                ));
                changed_files += 1;
            }
            total_churned_lines += file_churn;

            // 趋势分桶（按天，使用 commit timestamp）
            if let Some(ts) = row.commit_ts {
                if ts != 0.0 && file_churn > 0 {
                    let bucket_key = local_date_key(conn, ts);
                    *trend_buckets.entry(bucket_key).or_insert(0) += file_churn;
                }
            }
        }

        // 当前总行数（取 file_versions 最新版本）
        let total_lines_current: i64 = conn
            .query_row(
                "SELECT COALESCE(SUM(total_lines), 0) as total FROM file_versions \
                 WHERE is_current = 1 AND file_instance_id IN \
                 (SELECT id FROM file_instances WHERE workspace_id = ?1)",
                [workspace_id],
                |row| row.get(0),
            )
            .map_err(|error| format!("cannot query churn total lines: {error}"))?;

        let churn_rate = if total_lines_current > 0 {
            total_churned_lines as f64 / total_lines_current as f64
        } else {
            0.0
        };

        // 按流失行数降序 Top 10（稳定排序，对齐 Python sorted reverse=True）
        by_file.sort_by(|a, b| b.1.churned_lines.cmp(&a.1.churned_lines));
        let top_files: Vec<Value> = by_file
            .iter()
            .take(10)
            .map(|(fid, acc)| {
                json!({
                    "file_instance_id": fid,
                    "rel_path": acc.rel_path,
                    "change_count": acc.change_count,
                    "churned_lines": acc.churned_lines,
                })
            })
            .collect();
        let trend: Vec<Value> = trend_buckets
            .iter()
            .map(|(k, v)| json!({"date": k, "churned_lines": v}))
            .collect();

        return Ok(json!({
            "churn_rate": (churn_rate * 10000.0).round() / 10000.0,
            "total_churned_lines": total_churned_lines,
            "changed_files": changed_files,
            "total_lines_current": total_lines_current,
            "top_churned_files": top_files,
            "trend": trend,
        }));
    }

    // ---- Fallback: file_versions 相邻版本 total_lines 差值近似 ----
    let mut sql_fv = String::from(
        "SELECT fv.id, fv.file_instance_id, fv.version_num, fv.total_lines, \
         fv.parsed_at, fi.rel_path, fi.module_path \
         FROM file_versions fv \
         JOIN file_instances fi ON fv.file_instance_id = fi.id \
         WHERE fi.workspace_id = ?",
    );
    let mut bind_fv: Vec<rusqlite::types::Value> =
        vec![rusqlite::types::Value::Integer(workspace_id)];
    if !module_filter.is_empty() {
        sql_fv.push_str(" AND fi.module_path LIKE ?");
        bind_fv.push(rusqlite::types::Value::Text(format!("{module_filter}%")));
    }
    if cutoff > 0.0 {
        sql_fv.push_str(" AND fv.parsed_at >= ?");
        bind_fv.push(rusqlite::types::Value::Real(cutoff));
    }
    sql_fv.push_str(" ORDER BY fv.file_instance_id, fv.version_num ASC");

    let mut by_file_fv: Vec<(i64, Vec<ChurnFvRow>)> = Vec::new();
    {
        let mut stmt = conn
            .prepare(&sql_fv)
            .map_err(|error| format!("cannot prepare churn fv query: {error}"))?;
        let rows = stmt
            .query_map(params_from_iter(bind_fv), |row| {
                Ok(ChurnFvRow {
                    file_instance_id: row.get(1)?,
                    total_lines: row.get::<_, Option<i64>>(3)?,
                    parsed_at: row.get(4)?,
                    rel_path: row.get::<_, String>(5)?,
                })
            })
            .map_err(|error| format!("cannot query churn fv: {error}"))?;
        for row in rows {
            let r = row.map_err(|error| format!("cannot read churn fv row: {error}"))?;
            if let Some(entry) = by_file_fv
                .iter_mut()
                .find(|(fid, _)| *fid == r.file_instance_id)
            {
                entry.1.push(r);
            } else {
                by_file_fv.push((r.file_instance_id, vec![r]));
            }
        }
    }

    let mut total_churned_lines: i64 = 0;
    let mut changed_files: i64 = 0;
    let mut total_lines_current: i64 = 0;
    let mut file_churn_records: Vec<Value> = Vec::new();
    let mut trend_buckets_fv: BTreeMap<String, i64> = BTreeMap::new();

    for (file_instance_id, fversions) in &by_file_fv {
        if fversions.is_empty() {
            continue; // Python: if not fversions: continue（分组后恒非空，防御）
        }
        changed_files += 1;
        total_lines_current += fversions
            .last()
            .map(|v| v.total_lines.unwrap_or(0))
            .unwrap_or(0);

        if fversions.len() < 2 {
            file_churn_records.push(json!({
                "file_instance_id": file_instance_id,
                "rel_path": fversions[0].rel_path,
                "change_count": 1,
                "churned_lines": 0,
            }));
            continue;
        }

        let mut file_churn: i64 = 0;
        let mut change_count: i64 = 1;
        for i in 1..fversions.len() {
            let prev_lines = fversions[i - 1].total_lines.unwrap_or(0);
            let curr_lines = fversions[i].total_lines.unwrap_or(0);
            let diff = (curr_lines - prev_lines).abs();
            file_churn += diff;
            change_count += 1;
            if diff > 0 {
                let bucket_key = local_date_key(conn, fversions[i].parsed_at);
                *trend_buckets_fv.entry(bucket_key).or_insert(0) += diff;
            }
        }
        total_churned_lines += file_churn;
        file_churn_records.push(json!({
            "file_instance_id": file_instance_id,
            "rel_path": fversions[0].rel_path,
            "change_count": change_count,
            "churned_lines": file_churn,
        }));
    }

    let churn_rate = if total_lines_current > 0 {
        total_churned_lines as f64 / total_lines_current as f64
    } else {
        0.0
    };

    file_churn_records.sort_by(|a, b| {
        let a_churn = a
            .get("churned_lines")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        let b_churn = b
            .get("churned_lines")
            .and_then(|v| v.as_i64())
            .unwrap_or(0);
        b_churn.cmp(&a_churn)
    });
    let top_files: Vec<Value> = file_churn_records.into_iter().take(10).collect();
    let trend: Vec<Value> = trend_buckets_fv
        .iter()
        .map(|(k, v)| json!({"date": k, "churned_lines": v}))
        .collect();

    Ok(json!({
        "churn_rate": (churn_rate * 10000.0).round() / 10000.0,
        "total_churned_lines": total_churned_lines,
        "changed_files": changed_files,
        "total_lines_current": total_lines_current,
        "top_churned_files": top_files,
        "trend": trend,
    }))
}

/// 搜索缺陷模式（复刻 Python `db_defect_kb.py:defect_pattern_search`）。
///
/// category 前缀 LIKE 匹配（`category%`）；severity_filter 精确匹配
/// （经 `_normalize_severity`：小写 + strip，空串不进入 WHERE）。
/// ORDER BY case_count DESC, created_at DESC。defect_patterns 为全局知识库
/// （无 workspace_id 列），隔离由连接级 ACL 保证。返回 `SELECT *` 全列，
/// 键序 = 表定义列序，与 Python `dict(row)` 一致。
fn query_local_defect_search(
    conn: &Connection,
    category: &str,
    severity_filter: &str,
) -> Result<Value, String> {
    let mut sql = String::from("SELECT * FROM defect_patterns WHERE 1=1");
    let mut bind: Vec<rusqlite::types::Value> = Vec::new();
    if !category.is_empty() {
        sql.push_str(" AND category LIKE ?");
        bind.push(rusqlite::types::Value::Text(format!("{category}%")));
    }
    if !severity_filter.is_empty() {
        sql.push_str(" AND severity = ?");
        bind.push(rusqlite::types::Value::Text(normalize_defect_severity(
            severity_filter,
        )));
    }
    sql.push_str(" ORDER BY case_count DESC, created_at DESC");

    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare defect search: {error}"))?;
    let rows = stmt
        .query_map(params_from_iter(bind), |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
                row.get::<_, Option<String>>(3)?,
                row.get::<_, Option<String>>(4)?,
                row.get::<_, Option<String>>(5)?,
                row.get::<_, Option<String>>(6)?,
                row.get::<_, Option<i64>>(7)?,
                row.get::<_, f64>(8)?,
            ))
        })
        .map_err(|error| format!("cannot query defect search: {error}"))?;
    let mut out = Vec::new();
    for row in rows {
        let (
            pattern_id,
            category,
            description,
            detection_rule,
            fix_template,
            severity,
            learned_from,
            case_count,
            created_at,
        ) = row.map_err(|error| format!("cannot read defect search row: {error}"))?;
        out.push(json!({
            "pattern_id": pattern_id,
            "category": category,
            "description": description,
            "detection_rule": detection_rule,
            "fix_template": fix_template,
            "severity": severity,
            "learned_from": learned_from,
            "case_count": case_count,
            "created_at": created_at,
        }));
    }
    Ok(Value::Array(out))
}

/// 推荐修复方案（复刻 Python `db_defect_kb.py:suggest_fix`）。
///
/// finding_id > 0 → semgrep_findings 按 id 直查；否则 symbol_contents 按
/// symbol_hash 查 qualified_name → 优先按 symbol_qualified（scanned_at 最新
/// LIMIT 1），无 qualified_name 时退化按 content_hash。pattern_id =
/// "DP-{rule_id}"；defect_patterns 无匹配则 pattern_id 置空。similar_fixes
/// 取 defect_fixes 按 effectiveness DESC, created_at DESC 前 5。分数三分支：
/// 有 similar_fixes → 均值；仅 pattern_id → min(0.5, case_count*0.05)；
/// 否则 0.0，最终 round 4。snippet 仅查询不参与输出（Python 同）。
fn query_local_defect_suggest_fix(
    conn: &Connection,
    symbol_hash: &str,
    finding_id: i64,
) -> Result<Value, String> {
    let mut rule_id = String::new();
    let mut finding_fix = String::new();

    if finding_id > 0 {
        // 具体 finding：按 id 直查
        let f_row = conn
            .query_row(
                "SELECT rule_id, snippet, fix, symbol_qualified \
                 FROM semgrep_findings WHERE id = ?1",
                [finding_id],
                |row| {
                    Ok((
                        row.get::<_, Option<String>>(0)?,
                        row.get::<_, Option<String>>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| format!("cannot query suggest fix finding: {error}"))?;
        if let Some((rid, fix)) = f_row {
            rule_id = rid.unwrap_or_default();
            finding_fix = fix.unwrap_or_default();
        }
    } else {
        // 通过 symbol_hash 查找相关 finding
        let qualified_name: String = conn
            .query_row(
                "SELECT qualified_name FROM symbol_contents WHERE content_hash = ?1",
                [symbol_hash],
                |row| row.get::<_, Option<String>>(0),
            )
            .optional()
            .map_err(|error| format!("cannot query suggest fix symbol: {error}"))?
            .flatten()
            .unwrap_or_default();
        let f_row: Option<(Option<String>, Option<String>)> = if !qualified_name.is_empty() {
            conn.query_row(
                "SELECT rule_id, snippet, fix \
                 FROM semgrep_findings WHERE symbol_qualified = ?1 \
                 ORDER BY scanned_at DESC LIMIT 1",
                [&qualified_name],
                |row| {
                    Ok((
                        row.get::<_, Option<String>>(0)?,
                        row.get::<_, Option<String>>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| format!("cannot query suggest fix qualified: {error}"))?
        } else {
            // 退化：直接按 content_hash 匹配
            conn.query_row(
                "SELECT rule_id, snippet, fix \
                 FROM semgrep_findings WHERE content_hash = ?1 \
                 ORDER BY scanned_at DESC LIMIT 1",
                [symbol_hash],
                |row| {
                    Ok((
                        row.get::<_, Option<String>>(0)?,
                        row.get::<_, Option<String>>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| format!("cannot query suggest fix content: {error}"))?
        };
        if let Some((rid, fix)) = f_row {
            rule_id = rid.unwrap_or_default();
            finding_fix = fix.unwrap_or_default();
        }
    }

    // 通过 rule_id 匹配 defect_patterns
    let mut pattern_id = String::new();
    let mut pattern_fix_template = String::new();
    if !rule_id.is_empty() {
        pattern_id = format!("DP-{rule_id}");
        let p_row: Option<(String, Option<String>, Option<i64>)> = conn
            .query_row(
                "SELECT pattern_id, fix_template, case_count \
                 FROM defect_patterns WHERE pattern_id = ?1",
                [&pattern_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, Option<String>>(1)?,
                        row.get::<_, Option<i64>>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| format!("cannot query suggest fix pattern: {error}"))?;
        match p_row {
            Some((_, fix_template, _)) => {
                pattern_fix_template = fix_template.unwrap_or_default();
            }
            None => pattern_id.clear(), // 模式不存在则置空 pattern_id
        }
    }

    // 从 defect_fixes 查找类似修复案例（前 5）
    let mut similar_fixes: Vec<Value> = Vec::new();
    if !pattern_id.is_empty() {
        let mut stmt = conn
            .prepare(
                "SELECT pattern_id, symbol_hash, before_hash, after_hash, fix_diff, effectiveness \
                 FROM defect_fixes WHERE pattern_id = ?1 \
                 ORDER BY effectiveness DESC, created_at DESC LIMIT 5",
            )
            .map_err(|error| format!("cannot prepare suggest fix similar: {error}"))?;
        let rows = stmt
            .query_map([&pattern_id], |row| {
                Ok((
                    row.get::<_, Option<String>>(0)?,
                    row.get::<_, Option<String>>(1)?,
                    row.get::<_, Option<String>>(2)?,
                    row.get::<_, Option<String>>(3)?,
                    row.get::<_, Option<String>>(4)?,
                    row.get::<_, Option<f64>>(5)?,
                ))
            })
            .map_err(|error| format!("cannot query suggest fix similar: {error}"))?;
        for row in rows {
            let (pid, sym, before, after, diff, eff) =
                row.map_err(|error| format!("cannot read suggest fix similar row: {error}"))?;
            similar_fixes.push(json!({
                "pattern_id": pid,
                "symbol_hash": sym,
                "before_hash": before,
                "after_hash": after,
                "fix_diff": diff,
                "effectiveness": eff,
            }));
        }
    }

    // 计算有效性分数
    let effectiveness_score: f64 = if !similar_fixes.is_empty() {
        let sum: f64 = similar_fixes
            .iter()
            .map(|f| f.get("effectiveness").and_then(|v| v.as_f64()).unwrap_or(0.0))
            .sum();
        sum / similar_fixes.len() as f64
    } else if !pattern_id.is_empty() {
        // 无修复案例时，基于 case_count 给出保守分数
        let case_count: i64 = conn
            .query_row(
                "SELECT case_count FROM defect_patterns WHERE pattern_id = ?1",
                [&pattern_id],
                |row| row.get::<_, Option<i64>>(0),
            )
            .map_err(|error| format!("cannot query suggest fix case count: {error}"))?
            .unwrap_or(0);
        (0.5f64).min(case_count as f64 * 0.05)
    } else {
        0.0
    };

    // 优先使用 semgrep_findings.fix，其次 defect_patterns.fix_template
    let recommended_fix = if finding_fix.is_empty() {
        pattern_fix_template
    } else {
        finding_fix
    };

    Ok(json!({
        "pattern_id": pattern_id,
        "fix_template": recommended_fix,
        "similar_fixes": similar_fixes,
        "effectiveness_score": (effectiveness_score * 10000.0).round() / 10000.0,
    }))
}

/// 按限定名查询符号变更-缺陷关联（复刻 Python
/// `db_evolution.py:get_defect_correlation_by_qn`）。
///
/// 先按 is_current=1 + is_deleted=0 查 symbol_hash（LIMIT 1），未找到 →
/// 全 0 + 空列表；找到后复用 query_local_defect_correlation。recent_defects
/// 取 findings 前 3（message 截断 100 字符，对齐 Python 字符切片），
/// defect_rate = defect/change 整数除法 round 3，defect_types 透传。
fn query_local_get_defect_correlation(
    conn: &Connection,
    workspace_id: i64,
    qualified_name: &str,
    window_commits: i64,
) -> Result<Value, String> {
    let symbol_hash: Option<String> = conn
        .query_row(
            "SELECT fsv.symbol_hash FROM file_symbol_versions fsv \
             JOIN file_versions fv ON fsv.file_version_id = fv.id \
             JOIN file_instances fi ON fv.file_instance_id = fi.id \
             WHERE fi.workspace_id = ?1 AND fv.is_current = 1 AND fsv.is_deleted = 0 \
               AND fsv.qualified_name = ?2 \
             LIMIT 1",
            params![workspace_id, qualified_name],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| format!("cannot query defect correlation by qn: {error}"))?;
    let symbol_hash = match symbol_hash {
        Some(h) => h,
        None => {
            return Ok(json!({
                "qualified_name": qualified_name,
                "change_count": 0,
                "defect_count": 0,
                "defect_rate": 0.0,
                "recent_defects": [],
            }));
        }
    };

    let result = query_local_defect_correlation(conn, workspace_id, &symbol_hash, window_commits)?;
    let findings = result
        .get("findings")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();
    let change_count = result
        .get("total_changes")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let defect_count = result
        .get("defects_after_change")
        .and_then(|v| v.as_i64())
        .unwrap_or(0);
    let defect_types = result
        .get("defect_types")
        .cloned()
        .unwrap_or(Value::Object(Map::new()));
    let defect_rate = if change_count > 0 {
        defect_count as f64 / change_count as f64
    } else {
        0.0
    };

    // 取最近 3 条缺陷（message 截断 100 字符）
    let recent_defects: Vec<Value> = findings
        .iter()
        .take(3)
        .map(|f| {
            let message = f
                .get("message")
                .and_then(|v| v.as_str())
                .map(|s| s.chars().take(100).collect::<String>())
                .unwrap_or_default();
            json!({
                "rule_id": f.get("rule_id").cloned().unwrap_or(Value::Null),
                "severity": f.get("severity").cloned().unwrap_or(Value::Null),
                "message": message,
                "start_line": f.get("start_line").cloned().unwrap_or(Value::Number(0.into())),
            })
        })
        .collect();

    Ok(json!({
        "qualified_name": qualified_name,
        "change_count": change_count,
        "defect_count": defect_count,
        "defect_rate": (defect_rate * 1000.0).round() / 1000.0,
        "defect_types": defect_types,
        "recent_defects": recent_defects,
    }))
}

// W3-1（T-1786861820150-bfe5e805）：build 读组 5 工具查询函数。
// 语义逐条复刻 Python db 层 db_toolchain.py：
// - list_build_contexts：按 created_at 升序列出 workspace 全部 build context
// - get_build_context：精确匹配 + 短 hash 前缀匹配（唯一前缀才返回）
// - get_active_build_context：is_active=1 的 context（无则 Null）
// - get_resolved_edges：caller 过滤 + limit 限定（limit<=0 不限定）
// - count_resolved_edges：{"count": N}

/// workspace_build_contexts 行 → JSON 对象（对应 Python `BuildContext.to_dict()`）。
///
/// compile_flags / defines / include_paths 为 JSON 文本列，复刻 Python
/// `json.loads(v) if v else []/{}` 语义：NULL 或空串 → 空数组/空对象。
fn build_context_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let workspace_id: i64 = row.get(0)?;
    let build_context_hash: String = row.get(1)?;
    let name: String = row.get(2)?;
    let compile_flags_raw: Option<String> = row.get(3)?;
    let defines_raw: Option<String> = row.get(4)?;
    let include_paths_raw: Option<String> = row.get(5)?;
    let is_active: i64 = row.get(6)?;
    let created_at: f64 = row.get(7)?;

    let compile_flags: Value = compile_flags_raw
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| Value::Array(Vec::new()));
    let defines: Value = defines_raw
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| Value::Object(Map::new()));
    let include_paths: Value = include_paths_raw
        .and_then(|s| serde_json::from_str(&s).ok())
        .unwrap_or_else(|| Value::Array(Vec::new()));

    let mut m = Map::new();
    m.insert("workspace_id".into(), Value::Number(workspace_id.into()));
    m.insert(
        "build_context_hash".into(),
        Value::String(build_context_hash),
    );
    m.insert("name".into(), Value::String(name));
    m.insert("compile_flags".into(), compile_flags);
    m.insert("defines".into(), defines);
    m.insert("include_paths".into(), include_paths);
    m.insert("is_active".into(), Value::Bool(is_active != 0));
    m.insert(
        "created_at".into(),
        serde_json::Number::from_f64(created_at)
            .map(Value::Number)
            .unwrap_or(Value::Null),
    );
    Ok(Value::Object(m))
}

/// 列出 workspace 的所有 build context（复刻 Python `list_build_contexts`）。
///
/// 返回数组 [{workspace_id, build_context_hash, name, compile_flags, defines,
/// include_paths, is_active, created_at}]，按 created_at 升序。
fn query_local_build_context_list(conn: &Connection, workspace_id: i64) -> Result<Value, String> {
    let mut stmt = conn
        .prepare(
            "SELECT workspace_id, build_context_hash, name, compile_flags, defines,
             include_paths, is_active, created_at
             FROM workspace_build_contexts
             WHERE workspace_id = ?1 ORDER BY created_at",
        )
        .map_err(|error| format!("cannot prepare build context list query: {error}"))?;
    let rows = stmt
        .query_map([workspace_id], build_context_row)
        .map_err(|error| format!("cannot query build context list: {error}"))?
        .collect::<Result<Vec<Value>, _>>()
        .map_err(|error| format!("cannot read build context row: {error}"))?;
    Ok(Value::Array(rows))
}

/// 查询构建上下文详情（复刻 Python `get_build_context`）。
///
/// 支持短 hash 前缀匹配：精确匹配失败时尝试 `LIKE hash%`，仅当恰好 1 条
/// 前缀匹配时返回该条；0 条或多条均返回 Null（与 Python 语义一致）。
fn query_local_build_context_get(
    conn: &Connection,
    workspace_id: i64,
    build_context_hash: &str,
) -> Result<Value, String> {
    let sql = "SELECT workspace_id, build_context_hash, name, compile_flags, defines,
               include_paths, is_active, created_at
               FROM workspace_build_contexts
               WHERE workspace_id = ?1 AND build_context_hash = ?2";
    let mut params: Vec<rusqlite::types::Value> = vec![
        rusqlite::types::Value::Integer(workspace_id),
        rusqlite::types::Value::Text(build_context_hash.to_string()),
    ];
    match conn.query_row(sql, params_from_iter(params.iter()), build_context_row) {
        Ok(v) => return Ok(v),
        Err(rusqlite::Error::QueryReturnedNoRows) => {}
        Err(error) => return Err(format!("cannot query build context exact: {error}")),
    }
    // 前缀匹配（短 hash → 完整 hash）
    params[1] = rusqlite::types::Value::Text(format!("{build_context_hash}%"));
    let mut stmt = conn
        .prepare(sql)
        .map_err(|error| format!("cannot prepare build context prefix query: {error}"))?;
    let rows = stmt
        .query_map(params_from_iter(params.iter()), build_context_row)
        .map_err(|error| format!("cannot query build context prefix: {error}"))?
        .collect::<Result<Vec<Value>, _>>()
        .map_err(|error| format!("cannot read build context prefix row: {error}"))?;
    if rows.len() == 1 {
        Ok(rows.into_iter().next().unwrap())
    } else {
        Ok(Value::Null)
    }
}

/// 查询 workspace 当前 active 的 build context（复刻 Python
/// `get_active_build_context`）；无 active 时返回 Null。
fn query_local_build_context_active(conn: &Connection, workspace_id: i64) -> Result<Value, String> {
    let sql = "SELECT workspace_id, build_context_hash, name, compile_flags, defines,
               include_paths, is_active, created_at
               FROM workspace_build_contexts
               WHERE workspace_id = ?1 AND is_active = 1";
    match conn.query_row(sql, [workspace_id], build_context_row) {
        Ok(v) => Ok(v),
        Err(rusqlite::Error::QueryReturnedNoRows) => Ok(Value::Null),
        Err(error) => Err(format!("cannot query active build context: {error}")),
    }
}

/// resolved_edges 行 → JSON 对象（对应 Python `ResolvedEdge.to_dict()`）。
fn resolved_edge_row(row: &rusqlite::Row<'_>) -> rusqlite::Result<Value> {
    let id: i64 = row.get(0)?;
    let workspace_id: i64 = row.get(1)?;
    let build_context_hash: String = row.get(2)?;
    let caller_symbol_id: i64 = row.get(3)?;
    let callee_symbol_id: i64 = row.get(4)?;
    let callee_name: String = row.get(5)?;
    let callee_file: String = row.get(6)?;
    let call_line: i64 = row.get(7)?;
    let resolution_method: String = row.get(8)?;
    let created_at: f64 = row.get(9)?;

    let mut m = Map::new();
    m.insert("id".into(), Value::Number(id.into()));
    m.insert("workspace_id".into(), Value::Number(workspace_id.into()));
    m.insert(
        "build_context_hash".into(),
        Value::String(build_context_hash),
    );
    m.insert(
        "caller_symbol_id".into(),
        Value::Number(caller_symbol_id.into()),
    );
    m.insert(
        "callee_symbol_id".into(),
        Value::Number(callee_symbol_id.into()),
    );
    m.insert("callee_name".into(), Value::String(callee_name));
    m.insert("callee_file".into(), Value::String(callee_file));
    m.insert("call_line".into(), Value::Number(call_line.into()));
    m.insert("resolution_method".into(), Value::String(resolution_method));
    m.insert(
        "created_at".into(),
        serde_json::Number::from_f64(created_at)
            .map(Value::Number)
            .unwrap_or(Value::Null),
    );
    Ok(Value::Object(m))
}

/// 查询 resolved edges（复刻 Python `get_resolved_edges`）。
///
/// caller_symbol_id 指定时加 `AND caller_symbol_id = ?3` 并按 call_line 排序；
/// 未指定时按 caller_symbol_id, call_line 排序；limit > 0 时附加 LIMIT（Python
/// `limit is not None and limit > 0` 语义，limit<=0 不限定）。
fn query_local_resolved_edges(
    conn: &Connection,
    workspace_id: i64,
    build_context_hash: &str,
    caller_symbol_id: Option<i64>,
    limit: i64,
) -> Result<Value, String> {
    let mut sql = String::from(
        "SELECT id, workspace_id, build_context_hash, caller_symbol_id,
                callee_symbol_id, callee_name, callee_file, call_line,
                resolution_method, created_at
         FROM resolved_edges
         WHERE workspace_id = ?1 AND build_context_hash = ?2",
    );
    let mut params: Vec<rusqlite::types::Value> = vec![
        rusqlite::types::Value::Integer(workspace_id),
        rusqlite::types::Value::Text(build_context_hash.to_string()),
    ];
    if let Some(caller) = caller_symbol_id {
        sql.push_str(" AND caller_symbol_id = ?3");
        params.push(rusqlite::types::Value::Integer(caller));
    }
    sql.push_str(if caller_symbol_id.is_some() {
        " ORDER BY call_line"
    } else {
        " ORDER BY caller_symbol_id, call_line"
    });
    if limit > 0 {
        sql.push_str(" LIMIT ?");
        params.push(rusqlite::types::Value::Integer(limit));
    }
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare resolved edges query: {error}"))?;
    let rows = stmt
        .query_map(params_from_iter(params.iter()), resolved_edge_row)
        .map_err(|error| format!("cannot query resolved edges: {error}"))?
        .collect::<Result<Vec<Value>, _>>()
        .map_err(|error| format!("cannot read resolved edge row: {error}"))?;
    Ok(Value::Array(rows))
}

/// 统计 resolved edges 数量（复刻 Python `count_resolved_edges`），返回 {"count": int}。
fn query_local_count_resolved_edges(
    conn: &Connection,
    workspace_id: i64,
    build_context_hash: &str,
) -> Result<Value, String> {
    let count: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM resolved_edges
             WHERE workspace_id = ?1 AND build_context_hash = ?2",
            params_from_iter(
                [
                    rusqlite::types::Value::Integer(workspace_id),
                    rusqlite::types::Value::Text(build_context_hash.to_string()),
                ]
                .iter(),
            ),
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot count resolved edges: {error}"))?;
    Ok(json!({ "count": count }))
}

/// GROUP BY 单列的 {key: count} 映射（by_severity / by_language 共用）。
fn group_count_pairs(
    conn: &Connection,
    workspace_id: i64,
    group_col: &str,
) -> Result<Map<String, Value>, String> {
    let sql = format!(
        "SELECT {group_col} as grp_key, COUNT(*) as cnt
         FROM semgrep_findings sf
         JOIN file_instances fi ON sf.file_instance_id = fi.id
         WHERE fi.workspace_id = ?1
         GROUP BY {group_col} ORDER BY cnt DESC"
    );
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare semgrep group query: {error}"))?;
    let rows = stmt
        .query_map([workspace_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, i64>(1)?))
        })
        .map_err(|error| format!("cannot query semgrep group rows: {error}"))?;
    let mut m = Map::new();
    for row in rows {
        let (key, cnt) = row.map_err(|error| format!("cannot read semgrep group row: {error}"))?;
        m.insert(key, Value::Number(cnt.into()));
    }
    Ok(m)
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
        PeerCredential::new_unix(uid, 1000, 12345)
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
    fn test_enterprise_query_methods_require_a_published_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);
        let requests = [
            (
                "query.file",
                json!({"workspace_instance_id": ws_id, "file_path": "src/main.rs"}),
            ),
            (
                "query.symbol_location",
                json!({
                    "workspace_instance_id": ws_id,
                    "name": "main",
                    "file_path": "src/main.rs"
                }),
            ),
            (
                "query.grep",
                json!({"workspace_instance_id": ws_id, "patterns": ["TODO"]}),
            ),
            (
                "query.issues",
                json!({"workspace_instance_id": ws_id, "qualified_name": "crate::main"}),
            ),
            (
                "query.tests",
                json!({"workspace_instance_id": ws_id, "qualified_name": "crate::main"}),
            ),
        ];
        for (method, params) in requests {
            let response = dispatch(&mut state, peer.clone(), method, &params, &[]);
            assert_eq!(
                response["ok"], false,
                "{method} must fail closed before publish"
            );
            assert_eq!(response["error"]["code"], "snapshot_not_ready", "{method}");
        }
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

        // T-1786584793766：发布成功后，统计查询必须走原生 snapshot，不能触发
        // Python/PyErr 路径；这也是 get_stats 与其它符号查询共享的首个验收点。
        let stats = dispatch(
            &mut state,
            peer.clone(),
            "query.stats",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(stats["ok"], true);
        // GraphStore 的 by_id 保留 id=0 占位槽，stats 与发布计数一致地包含该槽。
        assert_eq!(stats["result"]["symbol_count"], 3);
        assert_eq!(stats["result"]["edge_count"], 2);

        let response = dispatch(
            &mut state,
            peer.clone(),
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
            peer.clone(),
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
            peer.clone(),
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
            peer.clone(),
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
            peer.clone(),
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
            peer.clone(),
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
    fn test_file_grep_issues_and_tests_use_published_snapshot() {
        let mut state = make_state();
        let peer = make_peer(0);
        let temp = tempfile::tempdir().unwrap();
        let source_path = temp.path().join("a.py");
        std::fs::write(
            &source_path,
            "def alpha():\n    # TODO: validate input\n    return beta()\n\ndef beta():\n    return 1\n",
        )
        .unwrap();

        let workspace = dispatch(
            &mut state,
            peer.clone(),
            "workspace.register",
            &json!({"client_view_root": temp.path().to_string_lossy()}),
            &[],
        );
        assert_eq!(workspace["ok"], true);
        let workspace_instance_id = workspace["result"]["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string();
        let workspace_id = workspace["result"]["workspace_id"].as_i64().unwrap();
        let db_path = temp.path().join("snapshot.db");
        let source_path_string = source_path.to_string_lossy();

        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(&format!(
            "
            CREATE TABLE workspaces (
                id INTEGER PRIMARY KEY,
                root_path TEXT NOT NULL
            );
            INSERT INTO workspaces VALUES ({workspace_id}, '{}');
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                abs_path TEXT NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO file_instances VALUES
                (1, {workspace_id}, 'a.py', '{}', 'active');
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
                start_col INTEGER,
                end_col INTEGER,
                signature TEXT,
                has_comment INTEGER,
                comment_status TEXT,
                comment_content TEXT,
                depth INTEGER NOT NULL
            );
            INSERT INTO symbols VALUES
                (1, 1, 'hash-alpha', 'fn', 'alpha', 'a.alpha', 'a',
                 'public', 1, 3, 1, 12, 'alpha()', 1, 'present',
                 'alpha docs', 0),
                (2, 1, 'hash-beta', 'fn', 'beta', 'a.beta', 'a',
                 'private', 5, 6, 1, 10, 'beta()', 0, 'absent',
                 '', 0),
                (3, 1, 'hash-test', 'fn', 'test_alpha', 'a.test_alpha', 'a',
                 'private', 8, 10, 1, 15, 'test_alpha()', 0, 'absent',
                 '', 0);
            CREATE TABLE calls (
                caller_id INTEGER NOT NULL,
                callee_id INTEGER NOT NULL,
                callee_name TEXT NOT NULL,
                call_line INTEGER NOT NULL,
                is_cross_file INTEGER NOT NULL
            );
            INSERT INTO calls VALUES (1, 2, 'beta', 3, 0);
            CREATE TABLE file_versions (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                is_current INTEGER NOT NULL
            );
            INSERT INTO file_versions VALUES (10, 1, 1);
            CREATE TABLE symbol_contents (
                content_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                signature TEXT,
                has_comment INTEGER,
                comment_content TEXT
            );
            INSERT INTO symbol_contents VALUES
                ('hash-alpha', 'alpha', 'fn', 'def alpha(): TODO', 'alpha()', 1, 'alpha docs'),
                ('hash-beta', 'beta', 'fn', 'def beta(): return 1', 'beta()', 0, ''),
                ('hash-test', 'test_alpha', 'fn', 'def test_alpha(): alpha()', 'test_alpha()', 0, '');
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
            INSERT INTO file_symbol_versions VALUES
                (10, 'hash-alpha', 'a.alpha', 'a', 1, 3, 0, 0),
                (10, 'hash-beta', 'a.beta', 'a', 5, 6, 0, 0),
                (10, 'hash-test', 'a.test_alpha', 'a', 8, 10, 0, 0);
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
            INSERT INTO call_versions VALUES
                (10, 'a.alpha', 'hash-alpha', 'beta', 'a', 'a.beta', 'a.py', 3);
            CREATE TABLE semgrep_findings (
                file_instance_id INTEGER NOT NULL,
                rule_id TEXT NOT NULL,
                rule_name TEXT DEFAULT '',
                severity TEXT DEFAULT 'INFO',
                confidence TEXT DEFAULT 'UNKNOWN',
                message TEXT DEFAULT '',
                start_line INTEGER DEFAULT 0,
                end_line INTEGER DEFAULT 0,
                snippet TEXT DEFAULT '',
                fix TEXT DEFAULT '',
                symbol_qualified TEXT DEFAULT ''
            );
            INSERT INTO semgrep_findings VALUES
                (1, 'python.todo', 'TODO rule', 'WARNING', 'HIGH',
                 'remove TODO', 2, 2, '# TODO: validate input', '', 'a.alpha');
            CREATE TABLE guardrail_rules (
                rule_id TEXT PRIMARY KEY,
                category TEXT NOT NULL
            );
            INSERT INTO guardrail_rules VALUES ('guard.secret', 'security');
            CREATE TABLE guardrail_findings (
                workspace_id INTEGER NOT NULL DEFAULT 0,
                rule_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                symbol_hash TEXT DEFAULT '',
                severity TEXT NOT NULL DEFAULT 'warn',
                status TEXT NOT NULL DEFAULT 'open',
                message TEXT DEFAULT '',
                detected_at REAL NOT NULL
            );
            INSERT INTO guardrail_findings VALUES
                ({workspace_id}, 'guard.secret', 'a.py', 'hash-alpha', 'warn', 'open',
                 'secret-like value', 1000.0);
            CREATE TABLE test_case_relations (
                workspace_id INTEGER NOT NULL,
                test_fn_id INTEGER NOT NULL,
                tested_fn_id INTEGER NOT NULL,
                match_method TEXT NOT NULL,
                confidence TEXT NOT NULL,
                detected_at REAL NOT NULL
            );
            INSERT INTO test_case_relations VALUES
                ({workspace_id}, 3, 1, 'direct_call', 'high', 1000.0);
            CREATE TABLE test_runs (
                workspace_id INTEGER NOT NULL,
                test_fn_id INTEGER NOT NULL,
                test_name TEXT NOT NULL,
                status TEXT NOT NULL,
                duration_ms REAL DEFAULT 0,
                error_message TEXT DEFAULT '',
                error_type TEXT DEFAULT '',
                run_at REAL NOT NULL
            );
            INSERT INTO test_runs VALUES
                ({workspace_id}, 3, 'test_alpha', 'passed', 12.5, '', '', 1001.0);
            ",
            temp.path().to_string_lossy(),
            source_path_string,
        ))
        .unwrap();
        drop(conn);

        state
            .snapshot_cache
            .get_or_create(&workspace_instance_id)
            .build_and_publish_blocking(db_path.to_str().unwrap(), workspace_id, "ctx", None)
            .unwrap();

        let file = dispatch(
            &mut state,
            peer.clone(),
            "query.file",
            &json!({
                "workspace_instance_id": workspace_instance_id,
                "file_path": "a.py"
            }),
            &[],
        );
        assert_eq!(file["ok"], true);
        assert_eq!(file["result"].as_array().unwrap().len(), 3);
        assert_eq!(file["result"][0]["qualified_name"], "a.alpha");

        let location = dispatch(
            &mut state,
            peer.clone(),
            "query.symbol_location",
            &json!({
                "workspace_instance_id": workspace_instance_id,
                "name": "alpha",
                "file_path": "a.py"
            }),
            &[],
        );
        assert_eq!(location["ok"], true);
        assert_eq!(location["result"]["qualified_name"], "a.alpha");

        let grep = dispatch(
            &mut state,
            peer.clone(),
            "query.grep",
            &json!({
                "workspace_instance_id": workspace_instance_id,
                "patterns": ["TODO"],
                "fixed": true,
                "limit": 10
            }),
            &[],
        );
        assert_eq!(grep["ok"], true);
        assert!(grep["result"].as_str().unwrap().contains("a.py:2"));
        assert!(grep["result"].as_str().unwrap().contains("a.alpha"));

        let issues = dispatch(
            &mut state,
            peer.clone(),
            "query.issues",
            &json!({
                "workspace_instance_id": workspace_instance_id,
                "qualified_name": "a.alpha"
            }),
            &[],
        );
        assert_eq!(issues["ok"], true);
        assert_eq!(issues["result"].as_array().unwrap().len(), 2);
        assert_eq!(issues["result"][0]["source"], "semgrep");

        let tests = dispatch(
            &mut state,
            peer.clone(),
            "query.tests",
            &json!({
                "workspace_instance_id": workspace_instance_id,
                "qualified_name": "a.alpha"
            }),
            &[],
        );
        assert_eq!(tests["ok"], true);
        assert_eq!(tests["result"].as_array().unwrap().len(), 1);
        assert_eq!(tests["result"][0]["test_qualified_name"], "a.test_alpha");

        let denied = dispatch(
            &mut state,
            make_peer(9999),
            "query.file",
            &json!({
                "workspace_instance_id": workspace_instance_id,
                "file_path": "a.py"
            }),
            &[],
        );
        assert_eq!(denied["ok"], false);
        assert_eq!(denied["error"]["code"], "workspace_forbidden");
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

    #[test]
    fn test_snapshot_publish_reports_real_load_error() {
        // T-1786574299601：内部加载错误以 String 传播后，snapshot.publish 的错误
        // 响应应携带真实失败原因（SQLite 查询失败的具体消息），而非"daemon 未嵌入
        // Python 解释器，无法输出 PyErr 详情"的误导性通用错误。
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        // 空文件是合法空 SQLite 库：wal_checkpoint 可通过，但无 file_instances 表，
        // 触发 GraphStore 加载的真实错误（no such table）
        let tmp = tempfile::tempdir().unwrap();
        let empty_db = tmp.path().join("empty.db");
        std::fs::write(&empty_db, b"").unwrap();

        let response = dispatch(
            &mut state,
            peer,
            "snapshot.publish",
            &json!({
                "workspace_instance_id": ws_id,
                "db_path": empty_db.to_str().unwrap()
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        let err_msg = response["error"]["message"].as_str().unwrap().to_string();
        assert!(
            err_msg.contains("no such table") || err_msg.contains("file_instances"),
            "错误应携带真实 SQLite 加载失败原因，实际: {}",
            err_msg
        );
        assert!(
            !err_msg.contains("未嵌入 Python"),
            "错误不应再被掩盖为误导性通用信息，实际: {}",
            err_msg
        );
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
            let response = dispatch(&mut state, other_peer.clone(), method, &params, &[]);
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

    // ---- W1-4-FIX：snapshot.publish 用真相源 workspace_id（W1-3 产品级 bug）----

    /// 构造带真相源 workspaces 表 + file_instances 的临时库（模拟 Python 用户级单库）
    ///
    /// - `ws_id_in_db`：workspaces 表里的真实 id（Python workspaces.id）
    /// - `root_path_in_db`：workspaces.root_path 存储值（Python norm_path 规范化后）
    /// - `file_ws_id`：file_instances.workspace_id（应为 ws_id_in_db）
    fn build_source_db(
        ws_id_in_db: i64,
        root_path_in_db: &str,
        file_ws_id: i64,
    ) -> tempfile::TempDir {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("source.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(&format!(
            "
            CREATE TABLE workspaces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                root_path TEXT UNIQUE NOT NULL,
                created_at REAL NOT NULL,
                is_active INTEGER DEFAULT 0,
                description TEXT DEFAULT ''
            );
            INSERT INTO workspaces (id, name, root_path, created_at, is_active, description)
            VALUES ({ws_id_in_db}, 'test-ws', '{root_path_in_db}', 1.0, 1, '');
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
                (1, {file_ws_id}, 'a.py', '/repo/a.py', 'active');
            INSERT INTO symbols VALUES
                (1, 1, 'hash-alpha', 'fn', 'alpha', 'a.alpha', 'a', 'public', 1, 4, 0),
                (2, 1, 'hash-beta', 'fn', 'beta', 'a.beta', 'a', 'private', 6, 8, 0);
            INSERT INTO calls VALUES (1, 2, 'beta', 3, 0);
            INSERT INTO file_versions VALUES (10, 1, 1);
            INSERT INTO symbol_contents VALUES
                ('hash-alpha', 'alpha', 'fn', 'def alpha(): beta()', 'alpha()', 1, 'alpha docs'),
                ('hash-beta', 'beta', 'fn', 'def beta(): pass', 'beta()', 0, '');
            INSERT INTO file_symbol_versions VALUES
                (10, 'hash-alpha', 'a.alpha', 'a', 1, 4, 0, 0),
                (10, 'hash-beta', 'a.beta', 'a', 6, 8, 0, 0);
            INSERT INTO call_versions VALUES
                (10, 'a.alpha', 'hash-alpha', 'beta', 'a', 'a.beta', 'a.py', 3);
            "
        ))
        .unwrap();
        drop(conn);
        temp
    }

    #[test]
    fn test_resolve_true_workspace_id_prefers_source_of_truth() {
        // 真相源有 workspaces 表且 root_path 匹配 → 返回真相源 id（而非 fallback）
        let temp = build_source_db(7, "/repo/client_view", 7);
        let db_path = temp.path().join("source.db");
        let resolved =
            resolve_true_workspace_id(db_path.to_str().unwrap(), "/repo/client_view", 129);
        assert_eq!(resolved, 7);
    }

    #[test]
    fn test_resolve_true_workspace_id_normalizes_path_variants() {
        // client_view_root 带反斜杠/尾斜杠/大写盘符（Windows 写法）也能匹配
        let temp = build_source_db(7, "c:/repo/client_view", 7);
        let db_path = temp.path().join("source.db");
        let resolved =
            resolve_true_workspace_id(db_path.to_str().unwrap(), r"C:\REPO\client_view\", 129);
        assert_eq!(resolved, 7);
    }

    #[test]
    fn test_resolve_true_workspace_id_matches_branch_suffix() {
        // 分支工作区 root_path 带 "#分支名" 后缀（db_branch.py）→ 前缀匹配兜底
        let temp = build_source_db(9, "/repo/client_view#feature-branch", 9);
        let db_path = temp.path().join("source.db");
        let resolved =
            resolve_true_workspace_id(db_path.to_str().unwrap(), "/repo/client_view", 129);
        assert_eq!(resolved, 9);
    }

    #[test]
    fn test_resolve_true_workspace_id_fallback_no_workspaces_table() {
        // 无 workspaces 表（测试 minimal_db / 非用户级库）→ fallback registry ROWID
        let tmp = tempfile::tempdir().unwrap();
        let empty_db = tmp.path().join("minimal.db");
        std::fs::write(&empty_db, b"").unwrap();
        let resolved =
            resolve_true_workspace_id(empty_db.to_str().unwrap(), "/repo/client_view", 129);
        assert_eq!(resolved, 129);
    }

    #[test]
    fn test_resolve_true_workspace_id_fallback_no_match() {
        // 有 workspaces 表但 root_path 不匹配 → fallback registry ROWID
        let temp = build_source_db(7, "/other/root", 7);
        let db_path = temp.path().join("source.db");
        let resolved =
            resolve_true_workspace_id(db_path.to_str().unwrap(), "/repo/client_view", 129);
        assert_eq!(resolved, 129);
    }

    #[test]
    fn test_resolve_true_workspace_id_fallback_unopenable_db() {
        // 无法打开（不存在）→ fallback registry ROWID
        let resolved = resolve_true_workspace_id("/nonexistent/nope.db", "/repo/client_view", 129);
        assert_eq!(resolved, 129);
    }

    #[test]
    fn test_snapshot_publish_uses_true_workspace_id_from_source_db() {
        // W1-3 产品级 bug 的端到端复现：registry ROWID（如 129）≠ Python
        // workspaces.id（如 7）。publish 必须用真相源 id 过滤 GraphStore，
        // 否则 file_instances.workspace_id=7 的行被 `AND workspace_id=129` 过滤掉，
        // 快照为空（syms=0）。
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);

        // 模拟真实用户库：workspaces.root_path = 规范化后的 client_view_root，
        // workspaces.id = 7（≠ registry ROWID 1），file_instances.workspace_id = 7
        let workspace = owned_workspace(&state.base.registry, 0, &ws_id).unwrap();
        let registry_rowid = workspace["workspace_id"].as_i64().unwrap();
        assert_ne!(
            registry_rowid, 7,
            "测试前提：registry ROWID 应不同于真相源 id"
        );
        let client_view_root = workspace["client_view_root"].as_str().unwrap();
        let root_path_norm = normalize_path_key(client_view_root);
        let temp = build_source_db(7, &root_path_norm, 7);
        let db_path = temp.path().join("source.db");

        let response = dispatch(
            &mut state,
            peer,
            "snapshot.publish",
            &json!({
                "workspace_instance_id": ws_id,
                "db_path": db_path.to_str().unwrap()
            }),
            &[],
        );
        assert_eq!(
            response["ok"], true,
            "publish 应成功，实际错误: {}",
            response["error"]["message"]
        );
        // 用真相源 id=7 过滤 → 命中 2 个符号（symbol_count 含 id=0 占位槽 = 3）
        assert_eq!(
            response["result"]["symbol_count"], 3,
            "publish 必须用真相源 workspace_id 过滤，否则快照为空"
        );
        assert_eq!(response["result"]["call_count"], 1);
    }

    #[test]
    fn test_snapshot_publish_falls_back_to_registry_rowid() {
        // 无 workspaces 表的库（测试 minimal_db / 非用户级库）→ fallback registry
        // ROWID，file_instances.workspace_id = ROWID 时仍能正常过滤加载。
        let mut state = make_state();
        let peer = make_peer(0);
        let ws_id = register_workspace_for_test(&mut state, 0);
        let workspace = owned_workspace(&state.base.registry, 0, &ws_id).unwrap();
        let registry_rowid = workspace["workspace_id"].as_i64().unwrap();

        // 构造无 workspaces 表、file_instances.workspace_id = registry ROWID 的库
        let temp = build_source_db(registry_rowid, "/repo/client_view", registry_rowid);
        let db_path = temp.path().join("source.db");
        // 删掉 workspaces 表模拟 minimal_db（无真相源可查）
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch("DROP TABLE workspaces;").unwrap();
        drop(conn);

        let response = dispatch(
            &mut state,
            peer,
            "snapshot.publish",
            &json!({
                "workspace_instance_id": ws_id,
                "db_path": db_path.to_str().unwrap()
            }),
            &[],
        );
        assert_eq!(
            response["ok"], true,
            "fallback 路径 publish 应成功，实际错误: {}",
            response["error"]["message"]
        );
        assert_eq!(response["result"]["symbol_count"], 3);
    }
}
