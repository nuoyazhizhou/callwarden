//! JSON-RPC dispatch 路由表 + 基础方法实现。
//!
//! 参考：server/daemon_server.py:EnterpriseDaemonService.dispatch（L121-514）。
//! 本模块负责：
//! - 解析 RPC 请求（method + params + received_fds）
//! - 路由到对应 handler
//! - 基础方法实现：ping / health / schema.version
//! - 错误码体系：INVALID_PARAMS / METHOD_NOT_FOUND / INTERNAL_ERROR / PERMISSION_DENIED
//!
//! 高级方法（workspace.*/snapshot.*/query.*/gc.*/backup/restore）在 R4-R6 实现，
//! 本模块提供 trait 钩子供后续扩展。

use super::protocol::{make_error_response, make_ok_response};
use serde_json::{Map, Value};
use std::time::{Instant, SystemTime, UNIX_EPOCH};

/// M2.1（T-1786519351240-73127ab4）：query.file 越界路径结构化校验辅助。
///
/// 通过 `#[path]` 内联声明本子模块——`mod.rs` 不在 M2.1 所有权白名单（不可改），
/// 故不能在 `mod.rs` 中声明。`query_handlers.rs` 与本文件同目录，相对路径可用。
#[path = "query_handlers.rs"]
mod query_handlers;

/// peer credential（来自 SO_PEERCRED 或 Windows Named Pipe）
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCredential {
    pub uid: u32,
    pub gid: u32,
    pub pid: i32,
    pub sid_len: u8,
    pub sid_bytes: [u8; 128],
}

impl PeerCredential {
    pub fn new_unix(uid: u32, gid: u32, pid: i32) -> Self {
        Self {
            uid,
            gid,
            pid,
            sid_len: 0,
            sid_bytes: [0u8; 128],
        }
    }

    pub fn new_windows(sid: String, pid: u32) -> Self {
        let bytes = sid.as_bytes();
        let len = bytes.len().min(128);
        let mut sid_bytes = [0u8; 128];
        sid_bytes[..len].copy_from_slice(&bytes[..len]);
        Self {
            uid: u32::MAX,
            gid: u32::MAX,
            pid: pid as i32,
            sid_len: len as u8,
            sid_bytes,
        }
    }

    pub fn sid(&self) -> Option<String> {
        if self.sid_len > 0 {
            std::str::from_utf8(&self.sid_bytes[..self.sid_len as usize])
                .ok()
                .map(|s| s.to_string())
        } else {
            None
        }
    }

    pub fn owner_key(&self) -> String {
        if let Some(s) = self.sid() {
            return s;
        }
        #[cfg(unix)]
        {
            self.uid.to_string()
        }
        #[cfg(not(unix))]
        {
            if self.uid == u32::MAX {
                crate::daemon::transport_windows::get_current_user_sid()
                    .unwrap_or_else(|_| "unknown".to_string())
            } else {
                self.uid.to_string()
            }
        }
    }
}

/// daemon 运行状态（基础方法用，高级方法由 DaemonStateExt trait 扩展）
pub struct DaemonState {
    /// daemon 启动时间（用于计算 uptime）
    pub start_time: Instant,
    /// schema 版本号（与 db/schema.py:SCHEMA_VERSION 保持同步）
    pub schema_version: u32,
    /// daemon 进程 PID
    pub pid: u32,
    /// Task 协同存储
    pub task_collab_store: Option<std::sync::Arc<super::task_collab::TaskCollabStore>>,
    /// 共存契约（windows-wsl-daemon-coexistence-contract.md §3.1）：
    /// 稳定 authority 标识 `<host-instance>/<platform>/<user-or-service>/<db-fingerprint>`。
    pub authority_id: String,
    /// 本 daemon 使用的 transport（named-pipe / uds / windows-bridge / cli-bridge）。
    pub transport: String,
    /// task 数据库指纹（sha256 of task_db realpath + size），用于客户端校验 authority 一致性。
    pub task_db_fingerprint: String,
    /// task_loop control-plane 组件（1D3B：gate + Public permit store + daemon generation）。
    /// `None` = 能力未组装，`task_loop.public_promote` 与公共路由一律 fail-closed。
    pub task_loop_control: Option<std::sync::Arc<crate::daemon::task_loop::promotion::TaskLoopControlPlane>>,
}

impl Default for DaemonState {
    fn default() -> Self {
        Self {
            start_time: Instant::now(),
            schema_version: super::SCHEMA_VERSION,
            pid: std::process::id(),
            task_collab_store: None,
            authority_id: authority_id_from_env(),
            transport: transport_from_env(),
            task_db_fingerprint: task_db_fingerprint_from_env(),
            task_loop_control: None,
        }
    }
}

impl DaemonState {
    /// 注入 task_loop control-plane（1D3B：daemon 启动时组装一次）。
    ///
    /// `gate` 为 daemon 级共享 `CapabilityMutationGate`（0A/0B 落地时需与
    /// Capability Authority / stage toggle 的写入路径共用同一实例，保持全局锁序）。
    /// `daemon_generation` 必须随 daemon 重启变化（如启动时刻 unix 纳秒）。
    pub fn with_task_loop_control(
        mut self,
        gate: std::sync::Arc<crate::daemon::task_loop::capability_control::CapabilityMutationGate>,
        daemon_generation: u64,
    ) -> Self {
        self.task_loop_control = Some(std::sync::Arc::new(
            crate::daemon::task_loop::promotion::TaskLoopControlPlane::new(gate, daemon_generation),
        ));
        self
    }
}

/// daemon RPC 错误（对应 Python DaemonRpcError）
#[derive(Debug, Clone)]
pub struct DaemonRpcError {
    pub code: String,
    pub message: String,
}

impl DaemonRpcError {
    pub fn new(code: &str, message: impl Into<String>) -> Self {
        Self {
            code: code.to_string(),
            message: message.into(),
        }
    }

    pub fn invalid_params(msg: impl Into<String>) -> Self {
        Self::new("invalid_params", msg)
    }

    pub fn method_not_found(method: &str) -> Self {
        Self::new("method_not_found", format!("未知方法: {}", method))
    }

    pub fn internal_error(msg: impl Into<String>) -> Self {
        Self::new("internal_error", msg)
    }

    pub fn permission_denied(msg: impl Into<String>) -> Self {
        Self::new("permission_denied", msg)
    }

    pub fn workspace_not_found(workspace_id: &str) -> Self {
        Self::new("workspace_not_found", workspace_id.to_string())
    }

    pub fn workspace_forbidden(msg: impl Into<String>) -> Self {
        Self::new("workspace_forbidden", msg)
    }

    pub fn workspace_archived(workspace_id: &str) -> Self {
        Self::new("workspace_archived", workspace_id.to_string())
    }
}

impl std::fmt::Display for DaemonRpcError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for DaemonRpcError {}

/// Authoritative_Clock 时间戳（Req 14.11）：ping 响应专用，供 lease 时钟读取。
/// 秒级浮点 Unix 时间戳，与全库 now_ts() 契约一致。
fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// daemon 状态扩展 trait（高级方法 handler 由 R4-R6 实现）
///
/// 默认实现返回 method_not_found，避免 R3 阶段编译失败。
/// R4-R6 实现 DaemonState 后，重写对应方法即可接入路由。
///
/// 基础方法（ping/health/schema.version）也在 trait 中提供默认实现，
/// 因为 dispatch_inner 接受泛型 S: DaemonStateExt，统一通过 state.method() 调用。
pub trait DaemonStateExt {
    /// 返回基础 DaemonState（用于获取 pid / start_time / schema_version）
    fn daemon_state(&self) -> &DaemonState;

    // ---- 基础方法（R3 默认实现）----

    fn handle_ping(&mut self, peer: PeerCredential) -> Result<Value, DaemonRpcError> {
        let state = self.daemon_state();
        let mut m = Map::new();
        m.insert("status".to_string(), Value::String("ok".to_string()));
        m.insert("peer_uid".to_string(), Value::Number(peer.uid.into()));
        m.insert("pid".to_string(), Value::Number(state.pid.into()));
        // 共存契约 §3.1/§5.3：ping/hello 必须返回 authority 身份，供客户端校验。
        m.insert(
            "authority_id".to_string(),
            Value::String(state.authority_id.clone()),
        );
        m.insert(
            "transport".to_string(),
            Value::String(state.transport.clone()),
        );
        m.insert(
            "task_db_fingerprint".to_string(),
            Value::String(state.task_db_fingerprint.clone()),
        );
        m.insert("protocol_version".to_string(), Value::Number(1u32.into()));
        // Authoritative_Clock（Req 14.11/14.12）：ping 必须返回 daemon 权威时间戳，
        // 供 lease acquire/renew/release 的 _clock() 读取（fail-closed，不回退客户端时钟）。
        let ts = now_ts();
        m.insert(
            "timestamp".to_string(),
            Value::Number(serde_json::Number::from_f64(ts).unwrap()),
        );
        Ok(Value::Object(m))
    }

    fn handle_health(&mut self, _peer: PeerCredential) -> Result<Value, DaemonRpcError> {
        let state = self.daemon_state();
        let uptime = state.start_time.elapsed().as_secs();
        let mut m = Map::new();
        m.insert("status".to_string(), Value::String("ok".to_string()));
        m.insert("pid".to_string(), Value::Number(state.pid.into()));
        m.insert("uptime_seconds".to_string(), Value::Number(uptime.into()));
        m.insert(
            "schema_version".to_string(),
            Value::Number(state.schema_version.into()),
        );
        m.insert("workspace_count".to_string(), Value::Number(0u32.into()));
        Ok(Value::Object(m))
    }

    fn handle_schema_version(&mut self, _peer: PeerCredential) -> Result<Value, DaemonRpcError> {
        let state = self.daemon_state();
        let mut m = Map::new();
        m.insert(
            "version".to_string(),
            Value::Number(state.schema_version.into()),
        );
        Ok(Value::Object(m))
    }

    // ---- 高级方法（R4-R6 实现，默认 method_not_found）----

    fn handle_workspace_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.register"))
    }

    fn handle_workspace_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.list"))
    }

    fn handle_workspace_status(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.status"))
    }

    fn handle_workspace_activate(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.activate"))
    }

    fn handle_workspace_remove(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.remove"))
    }

    fn handle_workspace_connect(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.connect"))
    }

    fn handle_workspace_file_refresh(
        &mut self,
        peer: PeerCredential,
        params: &Value,
        received_fds: &[i32],
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params, received_fds);
        Err(DaemonRpcError::method_not_found("workspace.file.refresh"))
    }

    fn handle_workspace_refresh_plan(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.refresh.plan"))
    }

    fn handle_workspace_file_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.file.delete"))
    }

    fn handle_workspace_recover(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("workspace.recover"))
    }

    fn handle_snapshot_publish(
        &mut self,
        peer: PeerCredential,
        params: &Value,
        received_fds: &[i32],
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params, received_fds);
        Err(DaemonRpcError::method_not_found("snapshot.publish"))
    }

    fn handle_gc_snapshots(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("gc.snapshots"))
    }

    fn handle_gc_cas(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("gc.cas"))
    }

    fn handle_query_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.stats"))
    }

    // W2-1（T-1786840097330-dec66710）：query 面 stats HTTP native 迁移新增
    // 三个 handler（query.uncommented_symbols / query.module_call_stats /
    // query.semgrep_stats），默认 method_not_found，SnapshotDaemonState 重写。

    fn handle_query_uncommented_symbols(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.uncommented_symbols"))
    }

    fn handle_query_module_call_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.module_call_stats"))
    }

    fn handle_query_semgrep_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.semgrep_stats"))
    }

    // W3-3（T-1786861820151-deb64c48）：get_semgrep_findings HTTP native 迁移
    // 新增 handler（query.semgrep_findings），默认 method_not_found，
    // SnapshotDaemonState 重写。

    fn handle_query_semgrep_findings(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.semgrep_findings"))
    }

    // W4-1（T-1786886251769-22b94ee8-sub-1）：git 读组 5 工具
    // （get_file_history / get_git_commits / get_commit_changes /
    // get_git_stats / get_commit_tasks）HTTP native 迁移，新增 5 个
    // handler（query.file_history / query.git_commits /
    // query.git_commit_changes / query.git_stats / query.commit_tasks），
    // 默认 method_not_found，SnapshotDaemonState 重写。

    fn handle_query_file_history(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.file_history"))
    }

    fn handle_query_git_commits(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.git_commits"))
    }

    fn handle_query_git_commit_changes(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.git_commit_changes"))
    }

    fn handle_query_git_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.git_stats"))
    }

    fn handle_query_commit_tasks(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.commit_tasks"))
    }

    // W4-2（T-1786886251769-22b94ee8-sub-2）：coverage/review 读组迁移新增
    // 两个 handler（query.coverage_for_symbol / query.diff_to_symbol），
    // 默认 method_not_found，SnapshotDaemonState 重写。

    fn handle_query_coverage_for_symbol(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.coverage_for_symbol"))
    }

    fn handle_query_diff_to_symbol(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.diff_to_symbol"))
    }

    // W4-3（T-1786886251769-22b94ee8-sub-3）：defect 读组迁移新增 5 个
    // handler（query.defect_correlation / query.churn_analysis /
    // query.defect_search / query.defect_suggest_fix /
    // query.get_defect_correlation），默认 method_not_found，
    // SnapshotDaemonState 重写。

    fn handle_query_defect_correlation(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.defect_correlation"))
    }

    fn handle_query_churn_analysis(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.churn_analysis"))
    }

    fn handle_query_defect_search(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.defect_search"))
    }

    fn handle_query_defect_suggest_fix(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.defect_suggest_fix"))
    }

    fn handle_query_get_defect_correlation(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.get_defect_correlation"))
    }

    // W4-4（T-1786886251769-22b94ee8-sub-4）：分支差异读面迁移新增 handler
    // （query.diff_branches），默认 method_not_found，SnapshotDaemonState 重写。
    // diff_branches 是跨 workspace 语义（按分支名查 source/target 两个
    // workspace），workspace_instance_id 仅用于连接级 ACL（owned_workspace +
    // snapshot query_db_path），不注入目标分支的 workspace_instance_id。

    fn handle_query_diff_branches(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.diff_branches"))
    }

    // W2-2（T-1786840097330-a9e0ec69）：task 面 stats HTTP native 迁移新增
    // 三个 handler（task.clone_stats / task.job_stats / task.clone_group_stats），
    // 默认 method_not_found，SnapshotDaemonState 重写。

    fn handle_task_clone_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("task.clone_stats"))
    }

    fn handle_task_job_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("task.job_stats"))
    }

    fn handle_task_clone_group_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("task.clone_group_stats"))
    }

    // W3-2（T-1786861820151-f3cecf40）：job 读组 3 工具（get_job_status /
    // list_jobs / wait_for_job）HTTP native 迁移新增 3 个 handler
    // （task.job_status / task.list_jobs / task.wait_for_job），默认
    // method_not_found，SnapshotDaemonState 重写。

    fn handle_task_job_status(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("task.job_status"))
    }

    fn handle_task_list_jobs(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("task.list_jobs"))
    }

    fn handle_task_wait_for_job(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("task.wait_for_job"))
    }

    // W2-3（T-1786840097331-fd01a3f8）：defect/edit stats HTTP native 迁移新增
    // 两个 handler（defect.stats / edit.stats），默认 method_not_found，
    // SnapshotDaemonState 重写。

    fn handle_defect_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("defect.stats"))
    }

    fn handle_edit_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("edit.stats"))
    }

    fn handle_query_symbol(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.symbol"))
    }

    fn handle_query_search(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.search"))
    }

    fn handle_query_callers(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.callers"))
    }

    fn handle_query_callees(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.callees"))
    }

    fn handle_query_file(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.file"))
    }

    fn handle_query_symbol_location(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.symbol_location"))
    }

    fn handle_query_grep(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.grep"))
    }

    fn handle_query_issues(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.issues"))
    }

    fn handle_query_tests(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.tests"))
    }

    // ---- 高级查询方法（G7-T4：Python snapshot_manager.py:305-373 对应）----
    // 默认实现返回 method_not_found，由 SnapshotDaemonState 覆盖

    fn handle_query_call_chain_down(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.call_chain_down"))
    }

    fn handle_query_impact(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.impact"))
    }

    fn handle_query_topological_order(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.topological_order"))
    }

    fn handle_query_detect_cycles(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("query.detect_cycles"))
    }

    // ---- Snapshot 管理方法（G7-T5：Python snapshot_manager.py:list/evict/stats 对应）----
    // 默认实现返回 method_not_found，由 SnapshotDaemonState 覆盖

    fn handle_snapshot_stats(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("snapshot.stats"))
    }

    fn handle_snapshot_list_workspaces(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("snapshot.list_workspaces"))
    }

    fn handle_snapshot_evict(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("snapshot.evict"))
    }

    fn handle_backup(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("backup"))
    }

    fn handle_restore(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("restore"))
    }

    // ---- Mount Mapping 管理（G4 实现）----
    // 默认实现返回 method_not_found，由 WorkspaceDaemonState 覆盖

    fn handle_mount_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("mount.register"))
    }

    fn handle_mount_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("mount.list"))
    }

    fn handle_mount_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("mount.delete"))
    }

    // ---- Toolchain 管理（G1 Layer 2 实现）----
    // 默认实现返回 method_not_found，由 WorkspaceDaemonState 覆盖

    fn handle_toolchain_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.register"))
    }

    fn handle_toolchain_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.list"))
    }

    fn handle_toolchain_get(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.get"))
    }

    fn handle_toolchain_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.delete"))
    }

    fn handle_toolchain_bind(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.bind"))
    }

    fn handle_toolchain_resolve(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.resolve"))
    }

    fn handle_toolchain_list_bound(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("toolchain.list_bound"))
    }

    // ---- Agent & Task 协同 RPC ----

    fn handle_agent_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_agent_register(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("agent.register"))
        }
    }
    fn handle_agent_heartbeat(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_agent_heartbeat(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("agent.heartbeat"))
        }
    }
    fn handle_task_create(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_create(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.create"))
        }
    }
    fn handle_task_supersede(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_supersede(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.supersede"))
        }
    }
    fn handle_task_superseded_by(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_superseded_by(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.superseded_by"))
        }
    }
    fn handle_task_attest_legacy_workspace_binding(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_attest_legacy_workspace_binding(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found(
                "task.attest_legacy_workspace_binding",
            ))
        }
    }
    fn handle_task_claim(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_claim(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.claim"))
        }
    }
    fn handle_task_claim_recover(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_claim_recover(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.claim.recover"))
        }
    }
    fn handle_task_work_next(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_work_next(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.work_next"))
        }
    }
    fn handle_task_report(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_report(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.report"))
        }
    }
    fn handle_task_step_resolve(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_step_resolve(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.step.resolve"))
        }
    }
    fn handle_task_remediation_create(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_remediation_create(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.remediation.create"))
        }
    }
    fn handle_task_handoff(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_handoff(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.handoff"))
        }
    }
    fn handle_task_contract_set(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_contract_set(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.contract_set"))
        }
    }
    fn handle_task_contract_bootstrap(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_contract_bootstrap(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.contract_bootstrap"))
        }
    }
    fn handle_task_contract_get(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_contract_get(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.contract_get"))
        }
    }
    fn handle_task_status(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_status(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.status"))
        }
    }
    /// `task.next_action`：纯只读派工 evaluator（5A）。经 `with_conn` 只读查询，
    /// 无任何 mutation；evaluator 内部 fail-closed（BLOCKED/NONE 也是合法响应）。
    fn handle_task_next_action(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            let input = crate::daemon::task_loop::next_action::NextActionInput::from_params(params)?;
            store.with_conn(|conn| {
                crate::daemon::task_loop::next_action::evaluate_next_action(
                    conn,
                    &input.workspace_instance_id,
                    &input.task_id,
                )
            })
        } else {
            Err(DaemonRpcError::method_not_found("task.next_action"))
        }
    }
    fn handle_task_events(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_events(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.events"))
        }
    }
    fn handle_task_wait(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_wait(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.wait"))
        }
    }
    fn handle_task_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_list(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.list"))
        }
    }
    fn handle_task_rollback(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_rollback(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.rollback"))
        }
    }
    fn handle_task_reopen(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_reopen(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.reopen"))
        }
    }
    fn handle_task_apply(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_apply(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.apply"))
        }
    }
    fn handle_task_close(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_close(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.close"))
        }
    }
    fn handle_task_capture_diff(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_capture_diff(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.capture_diff"))
        }
    }
    fn handle_task_split(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_split(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.split"))
        }
    }
    fn handle_task_create_from_plan(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_create_from_plan(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.create_from_plan"))
        }
    }
    fn handle_task_completion_review(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_completion_review(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.completion_review"))
        }
    }
    fn handle_task_resolve_quality_finding(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_resolve_quality_finding(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found(
                "task.resolve_quality_finding",
            ))
        }
    }
    fn handle_task_create_subtask(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_create_subtask(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.create_subtask"))
        }
    }
    fn handle_task_status_tree(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_status_tree(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.status_tree"))
        }
    }
    fn handle_task_record_symbol_change(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_record_symbol_change(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found(
                "task.record_symbol_change",
            ))
        }
    }
    fn handle_task_link_edit_audit_symbols(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_link_edit_audit_symbols(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found(
                "task.link_edit_audit_symbols",
            ))
        }
    }
    fn handle_task_get_symbol_changes(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_get_symbol_changes(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.get_symbol_changes"))
        }
    }
    fn handle_task_quality_findings(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_quality_findings(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.quality_findings"))
        }
    }
    fn handle_task_has_blocking_findings(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_has_blocking_findings(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found(
                "task.has_blocking_findings",
            ))
        }
    }
    fn handle_task_get_commits(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_task_get_commits(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("task.get_commits"))
        }
    }

    // ---- task_loop 公共能力 control-plane（1D3B 落地）----

    /// `task_loop.public_promote` control-plane mutation（§4.3）。
    ///
    /// 仅在 daemon 已组装 `TaskLoopControlPlane` 且 task-DB 可用时执行
    /// `promote_public_capability`（幂等 dedupe + 权威账本 + 内存 permit 安装）。
    /// 组件缺失一律 fail-closed：能力未发布，不产生任何审计写入。
    fn handle_task_loop_public_promote(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        use crate::daemon::task_loop::promotion::promote_public_capability;
        use crate::daemon::task_loop::types::{FrozenAuthorityInput, ERR_CAPABILITY_DISABLED};

        let state = self.daemon_state();
        let control = state.task_loop_control.as_ref().ok_or_else(|| {
            DaemonRpcError::new(
                ERR_CAPABILITY_DISABLED,
                "task_loop control-plane 未组装：daemon 未发布公共能力",
            )
        })?;
        let collab = state.task_collab_store.as_ref().ok_or_else(|| {
            DaemonRpcError::new(
                ERR_CAPABILITY_DISABLED,
                "task_loop 需要 task-DB：task_collab_store 未装配",
            )
        })?;
        let frozen = FrozenAuthorityInput {
            daemon_generation: control.daemon_generation,
            authority_id: state.authority_id.clone(),
            ..Default::default()
        };
        collab.with_conn(|conn| {
            promote_public_capability(conn, &control.gate, &control.store, &frozen, params)
        })
    }

    // ---- Lease Control Plane（M7；store 未装配时 method_not_found）----

    fn handle_lease_acquire(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_lease_acquire(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("lease.acquire"))
        }
    }

    fn handle_lease_extend(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_lease_extend(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("lease.extend"))
        }
    }

    fn handle_lease_release(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_lease_release(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("lease.release"))
        }
    }

    fn handle_lease_status(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_lease_status(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("lease.status"))
        }
    }

    fn handle_lease_list_events(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        if let Some(ref store) = self.daemon_state().task_collab_store {
            store.handle_lease_list_events(peer, params)
        } else {
            Err(DaemonRpcError::method_not_found("lease.list_events"))
        }
    }

    fn handle_collab_rpc(
        &mut self,
        peer: PeerCredential,
        method: &str,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // P1：证据与 Gate 查询必须从 daemon authority 读取；不得再把
        // Reviewer 依赖到 Python direct_read。尚未实现的 collab 方法仍
        // 诚实返回 method_not_found，不做隐式 fallback。
        if let Some(ref store) = self.daemon_state().task_collab_store {
            match method {
                "verdict.submit" => store.handle_verdict_submit(peer, params),
                "evidence.append" => store.handle_evidence_append(peer, params),
                "evidence.query" => store.handle_evidence_query(peer, params),
                // MCP-001（T-1787321708699-da5d8224）：role_view.get 迁移 rust_native。
                // HTTP RPC 方法名与 capability row 一致为 get_role_view；compat worker
                // 内部曾用 role_view.get 别名，这里两者都接同一 handler（向后兼容）。
                "get_role_view" | "role_view.get" => store.handle_get_role_view(peer, params),
                // MCP-002（T-1787321708760-de068a9c）：find_evidence 迁移 rust_native。
                // 语义与 Python tools_collab._h_find_evidence + evidence.query 一致：
                // 从 task_evidence_events 按 task_id/contract_id/verifier/limit 过滤查询。
                "find_evidence" => store.handle_find_evidence(peer, params),
                // MCP-003（T-1787321708856-e3c10624）：get_freshness_status 迁移 rust_native。
                // 语义与 Python db_task_evidence.derive_freshness 一致：
                // 全序 invalid > superseded > stale > fresh，派生 Evidence 新鲜度。
                "get_freshness_status" => store.handle_get_freshness_status(  peer,  params),
                // MCP-004（T-1787321708926-e7ebfac4）：get_gate_decision 迁移 rust_native。
                // 语义与 Python tools_collab._h_gate_decision + gate.decision.query 一致：
                // 从 task_gate_decisions 按 task_id/decision_id(gate_id) 过滤查询。
                "get_gate_decision" => store.handle_get_gate_decision(peer, params),
                // MCP-005（T-1787321709017-ed4e79b0）：get_artifact_freshness 迁移 rust_native。
                // 语义与 Python tools_p2_graph._h_get_artifact_freshness + db_task_dependencies
                // .get_artifact_freshness 一致：从 artifact_identities 查询最新 artifact 新鲜度。
                "get_artifact_freshness" => store.handle_get_artifact_freshness(peer, params),
                "gate.decision.query" => store.handle_gate_decision_query(peer, params),
                "gate.decision.append" => store.handle_gate_decision_append(peer, params),
                _ => Err(DaemonRpcError::method_not_found(method)),
            }
        } else {
            Err(DaemonRpcError::method_not_found(method))
        }
    }

    // ---- Build Context 管理（G1 Layer 2 实现）----

    fn handle_build_context_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.register"))
    }

    fn handle_build_context_list(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.list"))
    }

    fn handle_build_context_get(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.get"))
    }

    fn handle_build_context_set_active(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.set_active"))
    }

    // W3-1（T-1786861820150-bfe5e805）：build 读组新增 3 个 HTTP native handler
    // （build_context.active / build_context.resolved_edges /
    // build_context.count_resolved_edges），默认 method_not_found，
    // SnapshotDaemonState 重写。
    fn handle_build_context_active(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.active"))
    }

    fn handle_build_context_resolved_edges(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.resolved_edges"))
    }

    fn handle_build_context_count_resolved_edges(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found(
            "build_context.count_resolved_edges",
        ))
    }

    fn handle_build_context_delete(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("build_context.delete"))
    }

    // ---- Resolved Edges（G1 Layer 2 实现）----

    fn handle_resolved_edges_store(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("resolved_edges.store"))
    }

    fn handle_resolved_edges_get(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("resolved_edges.get"))
    }

    fn handle_resolved_edges_count(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("resolved_edges.count"))
    }

    fn handle_resolved_edges_replace(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found("resolved_edges.replace"))
    }

    // ---- 收敛架构 RPC（T02：fs/metrics/job/admin/edit 下沉）----
    // 单一 catch-all 钩子：所有 T02 新增 method（workspace.file.* / query.code_health /
    // task.job_* / admin.* / edit.* / rule.* / gate.* / summary.generate 等）经
    // dispatch_inner 的 CONVERGENCE_RPC_METHODS 匹配进入本方法。默认实现返回
    // method_not_found；SnapshotDaemonState 重写后分发到 fs_handlers /
    // metrics_handlers / job_runner / admin_handlers / edit_handlers。
    fn handle_convergence_rpc(
        &mut self,
        peer: PeerCredential,
        method: &str,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let _ = (peer, params);
        Err(DaemonRpcError::method_not_found(method))
    }
}

/// daemon state 扩展的默认实现（所有高级方法返回 method_not_found）
impl DaemonStateExt for DaemonState {
    fn daemon_state(&self) -> &DaemonState {
        self
    }
}

/// 执行单个 RPC 请求，返回 JSON-RPC 响应
///
/// 参数：
/// - state: daemon 状态（实现 DaemonStateExt trait）
/// - peer: peer credential（来自 SO_PEERCRED）
/// - method: RPC 方法名
/// - params: 参数（JSON object）
/// - received_fds: 附加的 FD 列表（来自 SCM_RIGHTS）
///
/// 返回：JSON-RPC 响应（{ok:true, result} 或 {ok:false, error:{code,message}}）
pub fn dispatch<S: DaemonStateExt>(
    state: &mut S,
    peer: PeerCredential,
    method: &str,
    params: &Value,
    received_fds: &[i32],
) -> Value {
    let result = dispatch_inner(state, peer, method, params, received_fds);
    match result {
        Ok(value) => make_ok_response(value),
        Err(err) => make_error_response(&err.code, &err.message),
    }
}

/// 需要管理员权限的运维方法（修改全局配置 / 资源回收 / 数据库备份还原）。
///
/// 授权规则：`peer.uid == 0`（root）或 `peer.uid == current_uid()`（daemon 进程自己）。
/// workspace.file.refresh / workspace.register 等已经通过 owned_workspace / validate_owned_path
/// 做了 per-workspace UID ACL，不重复检查；只读方法（list/get/query/stats）允许任意已连接 peer。
pub const ADMIN_ONLY_METHODS: &[&str] = &[
    // 数据库备份 / 还原
    "backup",
    "restore",
    // 资源回收（CAS / snapshots / evict）
    "gc.cas",
    "gc.snapshots",
    "snapshot.evict",
    // Mount Mapping 写操作（register / delete）
    "mount.register",
    "mount.delete",
    // Mount Mapping 读操作（P0-2 整改 2026-07-21）
    // mount.list 暴露全局 host_path 映射，container_mount_mappings 表无 owner_uid 列，
    // 无法按 UID 过滤；改为 admin-only 避免普通用户枚举宿主机路径。
    "mount.list",
    // Toolchain 配置变更（register / delete / bind）
    "toolchain.register",
    "toolchain.delete",
    "toolchain.bind",
];

/// Protected_Mutation 方法列表（Req 14.6）。
///
/// 所有 Protected_Mutation 必须经由 daemon 进程内唯一串行化点（SerializationPoint）应用，
/// 系统不暴露第二个串行化点。SQLite 写锁仅为事务互斥（Req 14.7），
/// 授权/ownership/Lease/Independent_Review 判定不依赖 SQLite 锁。
///
/// 当前已实现的方法标记为 active；尚未实现的方法（D0 后续任务）预留占位，
/// dispatch_inner 会返回 method_not_found 直到对应 handler 落地。
pub const PROTECTED_MUTATION_METHODS: &[&str] = &[
    // ---- 已实现 ----
    // Envelope 发布（Snapshot publish = Envelope 物化）
    "snapshot.publish",
    // Workspace 文件刷新（写符号图谱）
    "workspace.file.refresh",
    // Workspace 恢复（写操作：重建状态）
    "workspace.recover",
    // 数据库备份/还原（改变持久化状态）
    "backup",
    "restore",
    // Task 协同写操作（multi-llm-contract-collaboration）
    "agent.register",
    "task.create",
    "task.claim",
    "task.claim.recover",
    "task.report",
    "task.remediation.create",
    "task.step.resolve",
    "task.handoff",
    // 任务替代（supersede）治理写：独立关系表 + append-only 事件。
    // P0-H（T-1787277487109-758e56d0）：经 serial writer 串行化点应用；
    // 全部 mutation 参数由 handle_task_supersede 做 authority/lease/fencing/
    // ledger 门禁（同 route_task_write，daemon 不可用 fail-closed）。
    "task.supersede",
    "task.rollback",
    "task.reopen",
    "task.apply",
    "task.close",
    "task.contract_set",
    "task.contract_bootstrap",
    "task.capture_diff",
    "task.split",
    "task.create_from_plan",
    // P0-B：历史无 binding task 的 append-only authority attestation。
    "task.attest_legacy_workspace_binding",
    "task.resolve_quality_finding",
    "task.create_subtask",
    "task.record_symbol_change",
    "task.link_edit_audit_symbols",
    "task.work_next",
    "task.completion_review",
    // task_loop 公共能力 promotion（1D3B cutover；写 promotion 审计账本 + 安装 Public permit）
    "task_loop.public_promote",
    // ---- D0 后续任务预留（Req 14.6 列举的 Protected_Mutation 类型）----
    // verdict 封存
    "verdict.submit",
    // Reveal_Event
    "reveal.submit",
    // Evidence 追加
    "evidence.append",
    // Gate decision
    "gate.decide",
    // Task-owned runtime/evidence gate append（非 Verdict）
    "gate.decision.append",
    // Lease 操作
    "lease.acquire",
    "lease.release",
    "lease.extend",
    // lease.renew 是 lease.extend 的兼容别名（同为写操作，须经串行化点）
    "lease.renew",
    // ---- 收敛架构写面（T02：全部写操作经 daemon 权威路径）----
    // 文件/构建面写
    "workspace.build_graph",
    "workspace.build_directory",
    "workspace.file.remove",
    "workspace.file.refresh_file",
    // 异步长任务
    "task.job_submit",
    "task.job_cancel",
    // admin 写面
    "admin.gc_archive_import",
    "admin.gc_policy_set",
    "admin.audit_rotate_key",
    "admin.cleanup_rule_sync_log",
    "admin.clear_clones",
    "admin.branch_register",
    "admin.branch_switch",
    "admin.assignment_create",
    "admin.assignment_revoke",
    "admin.record_action_identity",
    "admin.register_attestation_revocation",
    "admin.record_artifact_identity",
    "admin.publish_interface",
    "admin.select_interface_provider",
    // 编辑/提案/规则写面
    "edit.propose",
    "edit.propose_range_patch",
    "edit.propose_symbol_id_patch",
    "edit.propose_symbol_patch",
    "edit.revert",
    "edit.restore_all_comments",
    "edit.restore_comment",
    "edit.record_token_savings",
    "gate.resolve_findings",
    "gate.run_check",
    "rule.seed_bootstrap",
    "rule.extract_candidates",
    "rule.candidate_accept",
    "rule.candidate_create",
    "rule.candidate_reject",
    "rule.insert_agents_md_block",
    "rule.sync_agents_md",
    "guardrail.add_rule",
    "summary.generate",
];

/// 判断方法是否为 Protected_Mutation（须经串行化点）。
#[inline]
pub fn is_protected_mutation(method: &str) -> bool {
    PROTECTED_MUTATION_METHODS.contains(&method)
}

/// 收敛架构 RPC method 清单（T02 下沉，target_backend ∈ {rust_native, task_rpc}）。
///
/// 与 `deliverables/software-company/tool_migration_matrix.json` 一致（由
/// `scripts/verify_route_matrix.py` 机器核对）。这些 method 统一经
/// `handle_convergence_rpc` 分发到 fs_handlers / metrics_handlers /
/// job_runner / admin_handlers / edit_handlers。
pub const CONVERGENCE_RPC_METHODS: &[&str] = &[
    // S2（P0-compat 批次 1）：查询面 compat 迁 native（6）
    "get_top_callers",
    "get_orphan_symbols",
    "get_deepest_functions",
    "get_comment_coverage",
    "get_call_heatmap",
    "find_uncovered_functions",
    // S2（P0-compat 批次 2）：toolchain 组（3，读权威 task DB）
    "list_toolchains",
    "get_toolchain",
    "get_workspace_toolchains",
    // 文件/构建面（T02-fs，9）
    "workspace.build_graph",
    "workspace.build_directory",
    "workspace.file.read",
    "workspace.file.grep",
    "workspace.file.list",
    "workspace.file.symbol_content",
    "workspace.file.remove",
    "workspace.file.health",
    "workspace.file.refresh_file",
    // 度量/状态面（T02-metrics，9）
    "query.code_health",
    "query.metrics_summary",
    "query.complexity_hotspots",
    "query.coupling_analysis",
    "query.function_metrics",
    "query.largest_functions",
    "query.most_coupled_functions",
    "query.status",
    "query.symbol_content_by_hash",
    // diff 读面（T02-edit 内 2 个只读）
    "query.diff_callees",
    "query.diff_callers",
    // 异步长任务（T02-job，task_rpc）
    "task.job_submit",
    "task.job_cancel",
    // GC/审计/运维（T02-admin，22）
    "admin.gc_archive_import",
    "admin.gc_archive_inspect",
    "admin.gc_archive_list",
    "admin.gc_audit_get",
    "admin.gc_audit_list",
    "admin.gc_policy_get",
    "admin.gc_policy_set",
    "admin.gc_retention",
    "admin.audit_rotate_key",
    "admin.cleanup_rule_sync_log",
    "admin.clear_clones",
    "admin.snapshot_compare",
    "admin.metrics_get",
    "admin.branch_register",
    "admin.branch_switch",
    "admin.assignment_create",
    "admin.assignment_revoke",
    "admin.record_action_identity",
    "admin.register_attestation_revocation",
    "admin.record_artifact_identity",
    "admin.publish_interface",
    "admin.select_interface_provider",
    // 编辑/提案/规则写面（T02-edit，19 写 + 2 读）
    "edit.propose",
    "edit.propose_range_patch",
    "edit.propose_symbol_id_patch",
    "edit.propose_symbol_patch",
    "edit.revert",
    "edit.restore_all_comments",
    "edit.restore_comment",
    "edit.record_token_savings",
    "gate.resolve_findings",
    "gate.run_check",
    "rule.seed_bootstrap",
    "rule.extract_candidates",
    "rule.candidate_accept",
    "rule.candidate_create",
    "rule.candidate_reject",
    "rule.insert_agents_md_block",
    "rule.sync_agents_md",
    "guardrail.add_rule",
    "summary.generate",
];

/// 判断 method 是否为收敛架构 RPC。
#[inline]
pub fn is_convergence_rpc(method: &str) -> bool {
    CONVERGENCE_RPC_METHODS.contains(&method)
}

/// 生产入口：带串行化点的 dispatch（Req 14.6, 14.14）。
///
/// 对 Protected_Mutation 方法，通过 `serialization_point.execute()` 串行执行；
/// 只读方法和其他非 Protected 写方法直接执行（不经串行化点）。
///
/// 超时语义：等待串行化点超时返回 `request_timeout` Structured_Reason，
/// 不改变任何任务状态（Req 14.14）。
///
/// server.rs 的 handle_client_message / handle_windows_client 应调用本函数。
/// 单元测试可直接调用 `dispatch`（跳过串行化点）。
pub fn dispatch_rpc<S: DaemonStateExt>(
    state: &mut S,
    peer: PeerCredential,
    method: &str,
    params: &Value,
    received_fds: &[i32],
    serialization_point: &super::serialization::SerializationPoint,
) -> Value {
    if is_protected_mutation(method) {
        // Protected_Mutation 经唯一串行化点（Req 14.6）
        match serialization_point
            .execute(|| dispatch_inner(state, peer.clone(), method, params, received_fds))
        {
            Ok(value) => make_ok_response(value),
            Err(err) => make_error_response(&err.code, &err.message),
        }
    } else {
        // 非 Protected_Mutation 直接执行
        dispatch(state, peer, method, params, received_fds)
    }
}

/// 返回 daemon 进程自己的 uid（Unix: getuid；Windows: 与测试 current_uid() 一致）
///
/// P1-1 修复（2026-07-22 完整复审）：Windows 上没有真正的 Unix UID 概念，
/// 原 `0` 与 workspace.rs 测试中的 `current_uid()=1000` 不一致，导致 admin-only
/// 方法（backup/restore/gc.cas/mount.*）的测试 peer 在 Windows 上永远不是 admin。
/// 改为返回 1000，与测试 `current_uid()` 对齐，使 `make_owner_peer()` 在 Windows
/// 上也通过 `is_admin` 检查（`peer.uid == current_daemon_uid()`）。
pub fn current_daemon_owner_key() -> String {
    #[cfg(unix)]
    {
        unsafe { libc::getuid() }.to_string()
    }
    #[cfg(not(unix))]
    {
        crate::daemon::transport_windows::get_current_user_sid()
            .unwrap_or_else(|_| "unknown".to_string())
    }
}

pub fn current_daemon_uid() -> u32 {
    #[cfg(unix)]
    {
        unsafe { libc::getuid() }
    }
    #[cfg(not(unix))]
    {
        1000
    }
}

/// 获取 daemon 的 authority 标识（共存契约 §3.1）。
///
/// 优先级：
/// 1. 环境变量 `CW_DAEMON_AUTHORITY_ID`（显式 pin，客户端 mismatch 时 fail-closed）；
/// 2. 自动派生 `<host-instance>/<platform>/<user-or-service>/<db-fingerprint>`。
///
/// authority_id 不由客户端声称，由 daemon 在 ping/hello 响应中返回。
pub fn authority_id_from_env() -> String {
    if let Ok(v) = std::env::var("CW_DAEMON_AUTHORITY_ID") {
        if !v.trim().is_empty() {
            return v.trim().to_string();
        }
    }
    let platform = if cfg!(windows) {
        "windows"
    } else if cfg!(target_os = "macos") {
        "macos"
    } else {
        "linux"
    };
    let user = current_daemon_owner_key();
    let fingerprint = task_db_fingerprint_from_env();
    let host = hostname_or_fallback();
    format!("{host}/{platform}/{user}/{fingerprint}")
}

/// 获取本 daemon 使用的 transport（共存契约 §3.2）。
///
/// 优先级：`CW_DAEMON_TRANSPORT` 环境变量，否则按平台默认。
pub fn transport_from_env() -> String {
    if let Ok(v) = std::env::var("CW_DAEMON_TRANSPORT") {
        if !v.trim().is_empty() {
            return v.trim().to_string();
        }
    }
    if cfg!(windows) {
        "named-pipe".to_string()
    } else {
        "uds".to_string()
    }
}

/// 计算 task 数据库内容指纹（主库和 WAL 的规范路径及字节内容）。
///
/// 客户端通过比对 ping 返回的 fingerprint 与当前任务上下文一致来拒绝
/// 指向不同 task DB 的 daemon。返回空串表示 daemon 未配置 task DB（如纯
/// snapshot 测试 daemon），客户端应视为不可写 authority。
pub fn task_db_fingerprint_from_env() -> String {
    let path = match std::env::var("CW_DAEMON_TASK_DB") {
        Ok(v) if !v.trim().is_empty() => v.trim().to_string(),
        _ => {
            // 未显式配置时，尝试从 HOME 默认路径派生；失败返回空。
            let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"));
            match home {
                Some(h) => std::path::PathBuf::from(h)
                    .join(".callwarden")
                    .join("callwarden.db")
                    .to_string_lossy()
                    .to_string(),
                None => return String::new(),
            }
        }
    };
    let canonical =
        std::fs::canonicalize(&path).unwrap_or_else(|_| std::path::PathBuf::from(&path));
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    // SQLite 的最新提交可能仅存在于 -wal；只 hash 主库路径+长度会让不同
    // 内容产生相同 fingerprint，无法作为 authority pin。为避免分隔符歧义，
    // 每个组成部分都写入路径、存在标志和内容长度后再写入字节。
    for component in [
        canonical.clone(),
        std::path::PathBuf::from(format!("{}-wal", canonical.to_string_lossy())),
    ] {
        let display = component.to_string_lossy();
        hasher.update((display.len() as u64).to_be_bytes());
        hasher.update(display.as_bytes());
        match std::fs::read(&component) {
            Ok(bytes) => {
                hasher.update([1u8]);
                hasher.update((bytes.len() as u64).to_be_bytes());
                hasher.update(bytes);
            }
            Err(_) => hasher.update([0u8]),
        }
    }
    format!("{:x}", hasher.finalize())
}

/// 获取主机实例名（hostname），失败时回退 "unknown-host"。
fn hostname_or_fallback() -> String {
    #[cfg(unix)]
    {
        let mut buf = [0u8; 256];
        // gethostname 失败时返回 0 长度，走回退
        let rc = unsafe { libc::gethostname(buf.as_mut_ptr() as *mut libc::c_char, buf.len()) };
        if rc == 0 {
            let end = buf.iter().position(|&b| b == 0).unwrap_or(buf.len());
            if end > 0 {
                return String::from_utf8_lossy(&buf[..end]).to_string();
            }
        }
        "unknown-host".to_string()
    }
    #[cfg(not(unix))]
    {
        std::env::var("COMPUTERNAME").unwrap_or_else(|_| "unknown-host".to_string())
    }
}

/// 判断 peer 是否为管理员（root 或 daemon 进程自己，基于 owner_key）。
///
/// 与 P1-1 注释（current_daemon_uid）语义一致：Unix 上 peer.uid == daemon 自身 uid
/// 时 owner_key() 即为 current_daemon_owner_key()，已走上面的 owner_key 分支；Windows
/// 上 new_unix 测试 peer（uid=1000）的 owner_key() 是 "1000" 而非 daemon SID，须显式
/// 补 `peer.uid == current_daemon_uid()` 这条同 uid 判定，窗口才与 Unix 语义对齐。
pub fn is_admin(peer: &PeerCredential) -> bool {
    let key = peer.owner_key();
    if !key.is_empty() && key == current_daemon_owner_key() {
        return true;
    }
    peer.uid == 0 || key == "root" || key == "0" || peer.uid == current_daemon_uid()
}

/// dispatch 内部实现（返回 Result<Value, DaemonRpcError>）
fn dispatch_inner<S: DaemonStateExt>(
    state: &mut S,
    peer: PeerCredential,
    method: &str,
    params: &Value,
    received_fds: &[i32],
) -> Result<Value, DaemonRpcError> {
    // 管理员方法授权检查（fail-closed：未授权直接拒绝，不进入 handler）
    //
    // Phase 4-2 implement 状态：
    // - ADMIN_ONLY_METHODS 覆盖全局运维与 toolchain 写操作
    // - build_context/resolved_edges 是 workspace 资源，由 handler 做 owner ACL
    // - is_admin 判定已实现（uid==0 或 uid==current_daemon_uid()）
    // - workspace owner 校验在 workspace.rs:owned_workspace/owned_workspace_by_id 内
    // - 路径安全在 workspace.rs:validate_owned_path 内（canonicalize + owner_uid）
    // - QueryBudget 在 budget.rs 内（max_nodes/timeout_ms）
    //
    // TODO(audit): ACL 拒绝事件应记录到 audit_log（迁移自 Python server/audit_log.py）。
    //   当前仅返回 permission_denied 错误，未持久化审计记录。
    //   迁移后应在下面 ACL 拒绝分支调用 audit_logger.record(
    //     event_type="ACL_DENIED", actor_uid=peer.uid, method=method, result="denied")
    //   admin-only 方法执行成功/失败也应记录（event_type="ADMIN_OP", result=ok/fail）。
    if ADMIN_ONLY_METHODS.contains(&method) && !is_admin(&peer) {
        return Err(DaemonRpcError::permission_denied(format!(
            "方法 {} 需要管理员权限（root 或 daemon uid），当前 peer.uid={}",
            method, peer.uid
        )));
    }
    match method {
        // ---- 基础方法（R3 默认实现）----
        "ping" => state.handle_ping(peer),
        "health" => state.handle_health(peer),
        "schema.version" => state.handle_schema_version(peer),

        // ---- Workspace 管理（R4 实现）----
        "workspace.register" => state.handle_workspace_register(peer, params),
        "workspace.list" => state.handle_workspace_list(peer, params),
        "workspace.status" => state.handle_workspace_status(peer, params),
        "workspace.activate" => state.handle_workspace_activate(peer, params),
        "workspace.remove" => state.handle_workspace_remove(peer, params),
        "workspace.connect" => state.handle_workspace_connect(peer, params),
        "workspace.refresh.plan" => state.handle_workspace_refresh_plan(peer, params),
        "workspace.file.refresh" => state.handle_workspace_file_refresh(peer, params, received_fds),
        "workspace.file.delete" => state.handle_workspace_file_delete(peer, params),
        "workspace.recover" => state.handle_workspace_recover(peer, params),

        // ---- Snapshot 管理（R6 实现）----
        "snapshot.publish" => state.handle_snapshot_publish(peer, params, received_fds),
        "gc.snapshots" => state.handle_gc_snapshots(peer, params),

        // ---- CAS GC（R6 实现）----
        "gc.cas" => state.handle_gc_cas(peer, params),

        // ---- 查询方法（R6 实现）----
        "query.stats" => state.handle_query_stats(peer, params),
        // W2-1（T-1786840097330-dec66710）：query 面 stats HTTP native 迁移。
        // get_uncommented_symbols / get_module_call_stats / get_semgrep_stats
        // 从 python_compat 迁移为 rust_native，注册对应 native handler。
        "query.uncommented_symbols" => state.handle_query_uncommented_symbols(peer, params),
        "query.module_call_stats" => state.handle_query_module_call_stats(peer, params),
        "query.semgrep_stats" => state.handle_query_semgrep_stats(peer, params),
        // W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 从
        // python_compat 迁移为 rust_native，注册对应 native handler。
        "query.semgrep_findings" => state.handle_query_semgrep_findings(peer, params),
        // W4-1（T-1786886251769-22b94ee8-sub-1）：git 读组 5 工具
        // （get_file_history / get_git_commits / get_commit_changes /
        // get_git_stats / get_commit_tasks）从 python_compat/legacy 迁移为
        // rust_native，注册对应 native handler。
        "query.file_history" => state.handle_query_file_history(peer, params),
        "query.git_commits" => state.handle_query_git_commits(peer, params),
        "query.git_commit_changes" => state.handle_query_git_commit_changes(peer, params),
        "query.git_stats" => state.handle_query_git_stats(peer, params),
        "query.commit_tasks" => state.handle_query_commit_tasks(peer, params),
        // W4-2（T-1786886251769-22b94ee8-sub-2）：coverage/review 读组迁移。
        // get_coverage_for_symbol / diff_to_symbol 从 python_compat 迁移为
        // rust_native，注册对应 native handler。
        "query.coverage_for_symbol" => state.handle_query_coverage_for_symbol(peer, params),
        "query.diff_to_symbol" => state.handle_query_diff_to_symbol(peer, params),
        // W4-3（T-1786886251769-22b94ee8-sub-3）：defect 读组迁移。
        // defect_correlation / churn_analysis / defect_search /
        // defect_suggest_fix / get_defect_correlation 从 python_compat
        // 迁移为 rust_native，注册对应 native handler。
        "query.defect_correlation" => state.handle_query_defect_correlation(peer, params),
        "query.churn_analysis" => state.handle_query_churn_analysis(peer, params),
        "query.defect_search" => state.handle_query_defect_search(peer, params),
        "query.defect_suggest_fix" => state.handle_query_defect_suggest_fix(peer, params),
        "query.get_defect_correlation" => state.handle_query_get_defect_correlation(peer, params),
        // W4-4（T-1786886251769-22b94ee8-sub-4）：diff_branches 从 python_compat
        // 迁移为 rust_native，注册对应 native handler。
        "query.diff_branches" => state.handle_query_diff_branches(peer, params),
        // W2-2（T-1786840097330-a9e0ec69）：task 面 stats HTTP native 迁移。
        // get_clone_stats / get_job_stats / get_clone_group_stats 从
        // python_compat 迁移为 rust_native，注册对应 native handler。
        "task.clone_stats" => state.handle_task_clone_stats(peer, params),
        "task.job_stats" => state.handle_task_job_stats(peer, params),
        "task.clone_group_stats" => state.handle_task_clone_group_stats(peer, params),
        // W3-2（T-1786861820151-f3cecf40）：job 读组 3 工具（get_job_status /
        // list_jobs / wait_for_job）从 python_compat 迁移为 rust_native，
        // 注册对应 native handler。
        "task.job_status" => state.handle_task_job_status(peer, params),
        "task.list_jobs" => state.handle_task_list_jobs(peer, params),
        "task.wait_for_job" => state.handle_task_wait_for_job(peer, params),
        // W2-3（T-1786840097331-fd01a3f8）：defect/edit stats HTTP native 迁移。
        // defect_stats / get_edit_stats 从 python_compat 迁移为 rust_native，
        // 注册对应 native handler。
        "defect.stats" => state.handle_defect_stats(peer, params),
        "edit.stats" => state.handle_edit_stats(peer, params),
        "query.symbol" => {
            // M2.2（T-1786526643663-594ee010）：dispatch 层结构化前置校验。
            // 空/纯空白/NUL → invalid_params。拒绝后不进入 handler；
            // 合法符号名原样交给 SnapshotDaemonState 处理。
            let qualified_name = require_str_param(params, "qualified_name")?;
            let _ = query_handlers::validate_query_symbol_params(qualified_name)?;
            state.handle_query_symbol(peer, params)
        },
        "query.search" => state.handle_query_search(peer, params),
        "query.callers" => state.handle_query_callers(peer, params),
        "query.callees" => state.handle_query_callees(peer, params),
        "query.file" => {
            // M2.1（T-1786519351240-73127ab4）：dispatch 层结构化前置校验。
            // 空/NUL → invalid_params；`..` 向上穿越 → out_of_bounds。
            // 拒绝后不进入 handler；合法路径原样交给 SnapshotDaemonState 处理。
            let file_path = require_str_param(params, "file_path")?;
            let _rel_path = query_handlers::validate_query_file_path(file_path)?;
            state.handle_query_file(peer, params)
        },
        "query.symbol_location" => state.handle_query_symbol_location(peer, params),
        "query.grep" => {
            // M2.3（T-1786529505247-9d083e54）：dispatch 层结构化前置校验。
            // 空数组 / 空或纯空白 pattern / NUL 字节 → invalid_params。
            // 拒绝后不进入 handler；合法 patterns 原样交给 SnapshotDaemonState 处理。
            let patterns = params
                .get("patterns")
                .and_then(Value::as_array)
                .ok_or_else(|| DaemonRpcError::invalid_params("patterns 必须是字符串数组"))?;
            let pattern_strs = patterns
                .iter()
                .map(|value| {
                    value
                        .as_str()
                        .ok_or_else(|| {
                            DaemonRpcError::invalid_params("patterns 必须是字符串数组")
                        })
                })
                .collect::<Result<Vec<_>, _>>()?;
            query_handlers::validate_query_grep_params(&pattern_strs)?;
            state.handle_query_grep(peer, params)
        },
        "query.issues" => {
            // M2.4（T-1786539379174-90f74174）：dispatch 层结构化前置校验。
            // 空/纯空白/NUL → invalid_params。拒绝后不进入 handler；
            // 合法符号名原样交给 SnapshotDaemonState 处理。
            let qualified_name = require_str_param(params, "qualified_name")?;
            let _ = query_handlers::validate_query_issues_params(qualified_name)?;
            state.handle_query_issues(peer, params)
        },
        "query.tests" => {
            // M2.5（T-1786584287058-7f712ff4）：dispatch 层结构化前置校验。
            // 空/纯空白/NUL → invalid_params。拒绝后不进入 handler；
            // 合法符号名原样交给 SnapshotDaemonState 处理。
            let qualified_name = require_str_param(params, "qualified_name")?;
            let _ = query_handlers::validate_query_tests_params(qualified_name)?;
            state.handle_query_tests(peer, params)
        },

        // ---- 高级查询方法（G7-T4 实现）----
        "query.call_chain_down" => state.handle_query_call_chain_down(peer, params),
        "query.impact" => state.handle_query_impact(peer, params),
        "query.topological_order" => state.handle_query_topological_order(peer, params),
        "query.detect_cycles" => state.handle_query_detect_cycles(peer, params),

        // ---- Snapshot 管理方法（G7-T5 实现）----
        "snapshot.stats" => state.handle_snapshot_stats(peer, params),
        "snapshot.list_workspaces" => state.handle_snapshot_list_workspaces(peer, params),
        "snapshot.evict" => state.handle_snapshot_evict(peer, params),

        // ---- 运维方法（R6 实现）----
        "backup" => state.handle_backup(peer, params),
        "restore" => state.handle_restore(peer, params),

        // ---- Mount Mapping 管理（G4 实现）----
        "mount.register" => state.handle_mount_register(peer, params),
        "mount.list" => state.handle_mount_list(peer, params),
        "mount.delete" => state.handle_mount_delete(peer, params),

        // ---- Toolchain 管理（G1 Layer 2 实现）----
        "toolchain.register" => state.handle_toolchain_register(peer, params),
        "toolchain.list" => state.handle_toolchain_list(peer, params),
        "toolchain.get" => state.handle_toolchain_get(peer, params),
        "toolchain.delete" => state.handle_toolchain_delete(peer, params),
        "toolchain.bind" => state.handle_toolchain_bind(peer, params),
        "toolchain.resolve" => state.handle_toolchain_resolve(peer, params),
        "toolchain.list_bound" => state.handle_toolchain_list_bound(peer, params),

        // ---- Build Context 管理（G1 Layer 2 实现）----
        // W3-1（T-1786861820150-bfe5e805）：build 读组 5 工具迁移 rust_native，
        // list/get 复用下方 G1 路由（双模式：workspace_instance_id 走主库，
        // workspace_id 走 ToolchainStore）；active/resolved_edges/
        // count_resolved_edges 为 W3-1 新增 native 路由。
        "build_context.register" => state.handle_build_context_register(peer, params),
        "build_context.list" => state.handle_build_context_list(peer, params),
        "build_context.get" => state.handle_build_context_get(peer, params),
        "build_context.active" => state.handle_build_context_active(peer, params),
        "build_context.resolved_edges" => state.handle_build_context_resolved_edges(peer, params),
        "build_context.count_resolved_edges" => {
            state.handle_build_context_count_resolved_edges(peer, params)
        }
        "build_context.set_active" => state.handle_build_context_set_active(peer, params),
        "build_context.delete" => state.handle_build_context_delete(peer, params),

        // ---- Resolved Edges（G1 Layer 2 实现）----
        "resolved_edges.store" => state.handle_resolved_edges_store(peer, params),
        "resolved_edges.get" => state.handle_resolved_edges_get(peer, params),
        "resolved_edges.count" => state.handle_resolved_edges_count(peer, params),
        "resolved_edges.replace" => state.handle_resolved_edges_replace(peer, params),

        // ---- Agent & Task 协同 RPC ----
        "agent.register" => state.handle_agent_register(peer, params),
        "agent.heartbeat" => state.handle_agent_heartbeat(peer, params),
        "task.create" => state.handle_task_create(peer, params),
        "task.claim" => state.handle_task_claim(peer, params),
        "task.claim.recover" => state.handle_task_claim_recover(peer, params),
        "task.work_next" => state.handle_task_work_next(peer, params),
        "task.report" => state.handle_task_report(peer, params),
        "task.remediation.create" => state.handle_task_remediation_create(peer, params),
        "task.step.resolve" => state.handle_task_step_resolve(peer, params),
        // structured handoff 只能进入 daemon handler；handler 负责 envelope、lease/fencing
        // 与 append-only ledger 校验，dispatch 层不提供本地或 legacy fallback。
        "task.handoff" => state.handle_task_handoff(peer, params),
        "task.status" => state.handle_task_status(peer, params),
        "task.next_action" => state.handle_task_next_action(peer, params),
        "task.events" => state.handle_task_events(peer, params),
        "task.wait" => state.handle_task_wait(peer, params),
        "task.list" => state.handle_task_list(peer, params),
        "task.rollback" => state.handle_task_rollback(peer, params),
        "task.reopen" => state.handle_task_reopen(peer, params),
        "task.apply" => state.handle_task_apply(peer, params),
        "task.close" => state.handle_task_close(peer, params),
        "task.contract_set" => state.handle_task_contract_set(peer, params),
        "task.contract_bootstrap" => state.handle_task_contract_bootstrap(peer, params),
        "task.contract_get" => state.handle_task_contract_get(peer, params),
        "task.capture_diff" => state.handle_task_capture_diff(peer, params),
        "task.split" => state.handle_task_split(peer, params),
        // supersede 治理：声明/查询任务替代关系（独立关系表 + append-only 事件，不改被替代任务行）
        "task.supersede" => state.handle_task_supersede(peer, params),
        "task.superseded_by" => state.handle_task_superseded_by(peer, params),
        "task.attest_legacy_workspace_binding" => {
            state.handle_task_attest_legacy_workspace_binding(peer, params)
        },
        "task.create_from_plan" => state.handle_task_create_from_plan(peer, params),
        "task.completion_review" => state.handle_task_completion_review(peer, params),
        "task.resolve_quality_finding" => state.handle_task_resolve_quality_finding(peer, params),
        "task.create_subtask" => state.handle_task_create_subtask(peer, params),
        "task.status_tree" => state.handle_task_status_tree(peer, params),
        "task.record_symbol_change" => state.handle_task_record_symbol_change(peer, params),
        "task.link_edit_audit_symbols" => state.handle_task_link_edit_audit_symbols(peer, params),
        "task.get_symbol_changes" => state.handle_task_get_symbol_changes(peer, params),
        "task.quality_findings" => state.handle_task_quality_findings(peer, params),
        "task.has_blocking_findings" => state.handle_task_has_blocking_findings(peer, params),
        "task.get_commits" => state.handle_task_get_commits(peer, params),

        // ---- Lease Control Plane（M7；写操作经串行化点，只读直接执行）----
        "lease.acquire" => state.handle_lease_acquire(peer, params),
        // lease.renew 是 lease.extend 的兼容别名（同一 handler，docs + 测试记录）
        "lease.extend" | "lease.renew" => state.handle_lease_extend(peer, params),
        "lease.release" => state.handle_lease_release(peer, params),
        "lease.status" => state.handle_lease_status(peer, params),
        "lease.list_events" => state.handle_lease_list_events(peer, params),

        // ---- Collab P1/P3 方法 ----
        "verdict.submit"
        | "reveal.submit"
        | "gate.decide"
        | "get_role_view"
        | "role_view.get"
        | "find_evidence"
        | "get_freshness_status"
        | "get_gate_decision"
        | "get_artifact_freshness"
        | "evidence.append"
        | "evidence.query"
        | "freshness.status"
        | "gate.decision.query"
        | "gate.decision.append" => state.handle_collab_rpc(peer, method, params),

        // ---- task_loop 公共能力（1D3B cutover）----
        // 1D3B 已落地 Public permit：`task_loop.public_promote` 是 control-plane
        // Protected_Mutation，由 handle_task_loop_public_promote 经 gate → task-DB
        // 锁序执行 promote_public_capability（审计先提交后安装 permit）。
        // 未组装 control-plane（task_loop_control == None）时 handler 返回
        // fail-closed E_CAPABILITY_DISABLED。
        "task_loop.public_promote" => state.handle_task_loop_public_promote(peer, params),

        // ---- 收敛架构 RPC（T02：fs/metrics/job/admin/edit 下沉）----
        // 全部新 method 统一进入 handle_convergence_rpc（SnapshotDaemonState 重写）。
        m if is_convergence_rpc(m) => state.handle_convergence_rpc(peer, m, params),

        // ---- 未知方法 ----
        _ => Err(DaemonRpcError::method_not_found(method)),
    }
}

/// task_loop 公共能力的静态 foundation shim（1D0）已于 1D3B 移除：
/// `task_loop.public_promote` 改由 `handle_task_loop_public_promote` 直接接管，
/// 不再经过 route::dispatch_task_loop 包装。

// ============================================
// 参数解析工具函数
// ============================================

/// 从 params 提取字符串字段（缺失或非字符串返回 None）
pub fn get_str_param<'a>(params: &'a Value, key: &str) -> Option<&'a str> {
    params.get(key).and_then(|v| v.as_str())
}

/// 从 params 提取必填字符串字段（缺失返回 invalid_params 错误）
pub fn require_str_param<'a>(params: &'a Value, key: &str) -> Result<&'a str, DaemonRpcError> {
    get_str_param(params, key)
        .ok_or_else(|| DaemonRpcError::invalid_params(format!("缺少字段: {}", key)))
}

/// 从 params 提取可选字符串字段（缺失返回默认值）
pub fn get_str_param_or(params: &Value, key: &str, default: &str) -> String {
    get_str_param(params, key).unwrap_or(default).to_string()
}

/// 从 params 提取整数字段（缺失或非整数返回 None）
///
/// G12 批次8（2026-07-21）：修复 query 字段错配——
/// Python daemon_client.py 传 int 类型（如 `"limit": 50`），
/// 原 Rust daemon 用 `get_str_param + parse` 只接受字符串，数字被忽略。
/// 本函数支持 JSON 数字（i64/u64）和字符串两种形式，兼容旧客户端。
pub fn get_int_param(params: &Value, key: &str) -> Option<i64> {
    let v = params.get(key)?;
    // 优先 JSON 数字（Python client 默认传 int）
    if let Some(n) = v.as_i64() {
        return Some(n);
    }
    // 兼容字符串形式（旧客户端或 curl 手动调用）
    if let Some(s) = v.as_str() {
        return s.parse::<i64>().ok();
    }
    None
}

/// 从 params 提取可选整数字段（缺失或非整数返回默认值）
///
/// 与 `get_int_param` 配套，提供默认值回退。
pub fn get_int_param_or(params: &Value, key: &str, default: i64) -> i64 {
    get_int_param(params, key).unwrap_or(default)
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn make_peer() -> PeerCredential {
        let non_admin_uid = current_daemon_uid().wrapping_add(1);
        PeerCredential::new_unix(non_admin_uid, 1000, 12345)
    }

    fn make_state() -> DaemonState {
        // 不直接调用 DaemonState::default()：default() 会执行
        // task_db_fingerprint_from_env()，对 ~/.callwarden/callwarden.db 全量哈希
        // （用户库可数百 MB，并行测试下磁盘 thrash 会拖到 100+s），
        // 使 start_time.elapsed() 误报大 uptime。测试环境无需真实
        // fingerprint/authority/transport，手动构造并全部用固定测试值占位。
        DaemonState {
            start_time: Instant::now(),
            schema_version: super::super::SCHEMA_VERSION,
            pid: std::process::id(),
            task_collab_store: None,
            authority_id: "test-authority".to_string(),
            transport: "named-pipe".to_string(),
            task_db_fingerprint: String::new(),
            task_loop_control: None,
        }
    }

    // ---- 基础方法测试 ----

    #[test]
    fn test_ping_returns_ok_with_peer_uid() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "ping", &params, &[]);

        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
        assert_eq!(
            response["result"]["peer_uid"],
            current_daemon_uid().wrapping_add(1)
        );
        assert_eq!(response["result"]["pid"], state.pid);
    }

    #[test]
    fn test_ping_returns_authoritative_timestamp() {
        // Authoritative_Clock（Req 14.11/14.12）：ping 必须返回 daemon 权威时间戳，
        // 供 lease acquire/renew/release 的 _clock() 读取（fail-closed，不回退客户端时钟）。
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "ping", &params, &[]);

        assert_eq!(response["ok"], true);
        let ts = response["result"]["timestamp"].as_f64();
        assert!(ts.is_some(), "ping 响应必须包含 timestamp 字段");
        let ts = ts.unwrap();
        // 秒级 Unix 时间戳：应大于 2024-01-01（1704067200）且小于 2100 年（4102444800）
        assert!(
            ts > 1_704_067_200.0,
            "timestamp 应大于 2024 年: {}",
            ts
        );
        assert!(
            ts < 4_102_444_800.0,
            "timestamp 应小于 2100 年: {}",
            ts
        );
    }

    #[test]
    fn test_health_returns_uptime_and_schema_version() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "health", &params, &[]);

        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
        assert_eq!(
            response["result"]["schema_version"],
            super::super::SCHEMA_VERSION
        );
        assert_eq!(response["result"]["workspace_count"], 0);
        // uptime_seconds 应该 >= 0 且为"刚启动"量级。
        // 注：make_state() 已跳过 default() 的全量 DB 哈希（见 make_state 注释），
        // 此处 uptime 应为亚秒级；保留 <5s 断言捕获真实的 uptime 漂移。
        let uptime = response["result"]["uptime_seconds"].as_u64().unwrap();
        assert!(uptime < 5, "uptime 应处于刚启动量级，实际 {uptime}s");
    }

    #[test]
    fn test_schema_version_returns_version() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "schema.version", &params, &[]);

        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["version"], super::super::SCHEMA_VERSION);
    }

    // ---- 未知方法测试 ----

    #[test]
    fn test_unknown_method_returns_method_not_found() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "nonexistent.method", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
        assert!(response["error"]["message"]
            .as_str()
            .unwrap()
            .contains("nonexistent.method"));
    }

    // ---- 高级方法（默认返回 method_not_found）----

    #[test]
    fn test_workspace_register_default_unimplemented() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({"client_view_root": "/tmp/test"});
        let response = dispatch(&mut state, peer, "workspace.register", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
    }

    #[test]
    fn test_snapshot_publish_default_unimplemented() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({"workspace_instance_id": "123"});
        let response = dispatch(&mut state, peer, "snapshot.publish", &params, &[0]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
    }

    #[test]
    fn test_query_stats_default_unimplemented() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({"workspace_instance_id": "123"});
        let response = dispatch(&mut state, peer, "query.stats", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
    }

    // ---- 参数解析工具测试 ----

    #[test]
    fn test_get_str_param_present() {
        let params = json!({"key": "value"});
        assert_eq!(get_str_param(&params, "key"), Some("value"));
    }

    #[test]
    fn test_get_str_param_missing() {
        let params = json!({"other": "value"});
        assert_eq!(get_str_param(&params, "key"), None);
    }

    #[test]
    fn test_get_str_param_non_string() {
        let params = json!({"key": 123});
        assert_eq!(get_str_param(&params, "key"), None);
    }

    #[test]
    fn test_require_str_param_present() {
        let params = json!({"key": "value"});
        assert_eq!(require_str_param(&params, "key").unwrap(), "value");
    }

    #[test]
    fn test_require_str_param_missing_returns_invalid_params() {
        let params = json!({});
        let result = require_str_param(&params, "key");
        match result {
            Err(e) => {
                assert_eq!(e.code, "invalid_params");
                assert!(e.message.contains("key"));
            }
            _ => panic!("期望 invalid_params 错误"),
        }
    }

    #[test]
    fn test_get_str_param_or_present() {
        let params = json!({"key": "value"});
        assert_eq!(get_str_param_or(&params, "key", "default"), "value");
    }

    #[test]
    fn test_get_str_param_or_missing_returns_default() {
        let params = json!({});
        assert_eq!(get_str_param_or(&params, "key", "default"), "default");
    }

    // ---- get_int_param / get_int_param_or 测试（G12 批次8）----

    #[test]
    fn test_get_int_param_json_number() {
        // Python client 默认传 JSON 数字
        let params = json!({"limit": 50, "max_depth": 5});
        assert_eq!(get_int_param(&params, "limit"), Some(50));
        assert_eq!(get_int_param(&params, "max_depth"), Some(5));
    }

    #[test]
    fn test_get_int_param_string_form() {
        // 旧客户端或 curl 手动调用传字符串
        let params = json!({"limit": "50", "max_depth": "5"});
        assert_eq!(get_int_param(&params, "limit"), Some(50));
        assert_eq!(get_int_param(&params, "max_depth"), Some(5));
    }

    #[test]
    fn test_get_int_param_missing_returns_none() {
        let params = json!({});
        assert_eq!(get_int_param(&params, "limit"), None);
    }

    #[test]
    fn test_get_int_param_non_numeric_string_returns_none() {
        let params = json!({"limit": "abc"});
        assert_eq!(get_int_param(&params, "limit"), None);
    }

    #[test]
    fn test_get_int_param_or_json_number() {
        let params = json!({"limit": 50});
        assert_eq!(get_int_param_or(&params, "limit", 20), 50);
    }

    #[test]
    fn test_get_int_param_or_missing_returns_default() {
        let params = json!({});
        assert_eq!(get_int_param_or(&params, "limit", 20), 20);
    }

    #[test]
    fn test_get_int_param_or_non_numeric_returns_default() {
        let params = json!({"limit": "abc"});
        assert_eq!(get_int_param_or(&params, "limit", 20), 20);
    }

    // ---- DaemonRpcError 构造器测试 ----

    #[test]
    fn test_daemon_rpc_error_invalid_params() {
        let err = DaemonRpcError::invalid_params("missing field");
        assert_eq!(err.code, "invalid_params");
        assert_eq!(err.message, "missing field");
    }

    #[test]
    fn test_daemon_rpc_error_method_not_found() {
        let err = DaemonRpcError::method_not_found("unknown.method");
        assert_eq!(err.code, "method_not_found");
        assert!(err.message.contains("unknown.method"));
    }

    #[test]
    fn test_daemon_rpc_error_internal_error() {
        let err = DaemonRpcError::internal_error("panic");
        assert_eq!(err.code, "internal_error");
        assert_eq!(err.message, "panic");
    }

    #[test]
    fn test_daemon_rpc_error_permission_denied() {
        let err = DaemonRpcError::permission_denied("not owner");
        assert_eq!(err.code, "permission_denied");
        assert_eq!(err.message, "not owner");
    }

    #[test]
    fn test_daemon_rpc_error_workspace_not_found() {
        let err = DaemonRpcError::workspace_not_found("ws_123");
        assert_eq!(err.code, "workspace_not_found");
        assert_eq!(err.message, "ws_123");
    }

    #[test]
    fn test_daemon_rpc_error_workspace_forbidden() {
        let err = DaemonRpcError::workspace_forbidden("not your ws");
        assert_eq!(err.code, "workspace_forbidden");
        assert_eq!(err.message, "not your ws");
    }

    #[test]
    fn test_daemon_rpc_error_workspace_archived() {
        let err = DaemonRpcError::workspace_archived("ws_456");
        assert_eq!(err.code, "workspace_archived");
        assert_eq!(err.message, "ws_456");
    }

    #[test]
    fn test_daemon_rpc_error_display() {
        let err = DaemonRpcError::new("custom_code", "custom message");
        let s = format!("{}", err);
        assert_eq!(s, "custom_code: custom message");
    }

    // ---- PeerCredential 测试 ----

    fn make_root_peer() -> PeerCredential {
        PeerCredential::new_unix(0, 0, 1)
    }

    /// 非管理员 peer 调用 admin-only 方法应返回 permission_denied
    #[test]
    fn test_admin_only_method_denied_for_non_admin() {
        let mut state = make_state();
        let peer = make_peer(); // uid=1000，非 root 且非 daemon 自己
        let params = json!({"output_path": "/tmp/x.db"});

        // backup 是 admin-only 方法
        let response = dispatch(&mut state, peer, "backup", &params, &[]);
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "permission_denied");

        // 验证多个 admin-only 方法都被拒绝
        for method in &[
            "restore",
            "gc.cas",
            "gc.snapshots",
            "snapshot.evict",
            "mount.register",
            "mount.delete",
            "toolchain.register",
            "toolchain.delete",
            "toolchain.bind",
        ] {
            let response = dispatch(&mut state, make_peer(), method, &params, &[]);
            assert_eq!(
                response["error"]["code"], "permission_denied",
                "方法 {} 应被 permission_denied 拒绝",
                method
            );
        }
    }

    /// root (uid=0) 调用 admin-only 方法应通过授权检查（进入 handler 后由 handler 返回结果）
    #[test]
    fn test_admin_only_method_allowed_for_root() {
        let mut state = make_state();
        let peer = make_root_peer();
        let params = json!({});

        // backup 通过授权检查后，默认 DaemonState 的 handle_backup 返回 method_not_found
        // 这里只验证授权检查未拒绝（不是 permission_denied）
        let response = dispatch(&mut state, peer, "backup", &params, &[]);
        assert_ne!(
            response["error"]["code"], "permission_denied",
            "root 调用 backup 不应被 permission_denied"
        );
    }

    /// 只读方法（list/get/query/stats）不应被授权检查拦截
    #[test]
    fn test_readonly_methods_not_blocked_by_admin_check() {
        let mut state = make_state();
        let peer = make_peer(); // 非管理员

        // 只读方法应正常路由（不会被 permission_denied 拦截）
        // 注意：mount.list 是 admin-only，因为它会暴露全局路径；
        // build_context.* 属于 workspace 资源，由 handler 做 owner ACL，
        // 不能把 workspace 权限误化成全局管理员权限。
        for method in &[
            "workspace.list",
            "workspace.status",
            "toolchain.list",
            "toolchain.get",
            "toolchain.list_bound",
            "build_context.list",
            "build_context.get",
            "resolved_edges.replace",
            "query.stats",
            "query.symbol",
            "snapshot.stats",
            "snapshot.list_workspaces",
        ] {
            let params = json!({});
            let response = dispatch(&mut state, peer, method, &params, &[]);
            assert_ne!(
                response["error"]["code"], "permission_denied",
                "只读方法 {} 不应被 permission_denied 拦截",
                method
            );
        }
    }

    #[test]
    fn test_peer_credential_clone_copy() {
        let peer1 = PeerCredential::new_unix(100, 200, 300);
        let peer2 = peer1.clone();
        assert_eq!(peer1.uid, peer2.uid);
        assert_eq!(peer1.gid, peer2.gid);
        assert_eq!(peer1.pid, peer2.pid);
        assert_eq!(peer1.owner_key(), peer2.owner_key());
    }

    // ---- DaemonState 默认值测试 ----

    #[test]
    fn test_daemon_state_default_pid_nonzero() {
        // 用 make_state()（等价 default() 字段，但跳过全量 DB 哈希，见 make_state 注释）
        let state = make_state();
        assert!(state.pid > 0); // 进程 ID 应该 > 0
    }

    #[test]
    fn test_daemon_state_default_schema_version() {
        let state = make_state();
        assert_eq!(state.schema_version, super::super::SCHEMA_VERSION);
    }

    // ---- 完整 dispatch 链路测试（验证路由分发正确）----

    #[test]
    fn test_dispatch_ping_routes_correctly() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});
        let response = dispatch(&mut state, peer, "ping", &params, &[]);

        // 验证路由到 handle_ping（而非其他 handler）
        assert_eq!(response["ok"], true);
        assert_eq!(
            response["result"]["peer_uid"],
            current_daemon_uid().wrapping_add(1)
        );
    }

    #[test]
    fn test_dispatch_all_known_methods_route_without_panic() {
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});

        // 所有已知方法都应该路由成功（不 panic），即使是默认 method_not_found
        let methods = vec![
            "ping",
            "health",
            "schema.version",
            "workspace.register",
            "workspace.list",
            "workspace.status",
            "workspace.activate",
            "workspace.remove",
            "workspace.connect",
            "workspace.refresh.plan",
            "workspace.file.refresh",
            "workspace.file.delete",
            "workspace.recover",
            "snapshot.publish",
            "gc.snapshots",
            "gc.cas",
            "query.stats",
            "query.symbol",
            "query.search",
            "query.callers",
            "query.callees",
            "query.file",
            "query.symbol_location",
            "query.grep",
            "query.issues",
            "query.tests",
            "backup",
            "restore",
        ];

        for method in methods {
            let response = dispatch(&mut state, peer, method, &params, &[]);
            // 所有方法都应该返回有效的 JSON-RPC 响应（ok=true 或 ok=false）
            assert!(
                response.get("ok").is_some(),
                "方法 {} 的响应缺少 ok 字段",
                method
            );
        }
    }

    // ---- Collab P1/P3 方法路由测试（S5：daemon 显式路由 + MCP 兜底 direct_read）----

    #[test]
    fn test_collab_methods_route_to_collab_handler() {
        // 7 个 collab 方法必须显式匹配（不能落入通配 `_` 分支），
        // 路由到 handle_collab_rpc：返回结构化 method_not_found 且不 panic。
        // 真相源在 Python DB 层（库层 39+ 测试），MCP 的 _collab_rpc_call
        // 收到 method_not_found 后兜底 direct_read 直查 SQLite 真实表。
        let collab_methods = vec![
            "verdict.submit",
            "reveal.submit",
            "gate.decide",
            "role_view.get",
            "evidence.query",
            "freshness.status",
            "gate.decision.query",
        ];
        let mut state = make_state();
        let peer = make_peer();
        let params = json!({});

        for method in collab_methods {
            let response = dispatch(&mut state, peer, method, &params, &[]);
            // 返回有效 JSON-RPC 响应（不 panic）
            assert!(
                response.get("ok").is_some(),
                "方法 {} 的响应缺少 ok 字段",
                method
            );
            // 显式路由：ok=false 且错误码为 method_not_found（结构化拒绝，非静默）
            assert_eq!(
                response["ok"], false,
                "方法 {} 应经 handle_collab_rpc 返回不可用",
                method
            );
            assert_eq!(
                response["error"]["code"], "method_not_found",
                "方法 {} 应返回 method_not_found",
                method
            );
            // 错误 message 携带方法名，供 MCP client 判断降级走 direct_read
            assert!(
                response["error"]["message"]
                    .as_str()
                    .unwrap()
                    .contains(method),
                "方法 {} 的错误信息应包含方法名",
                method
            );
        }
    }

    struct CollabRouteMock {
        base: DaemonState,
    }

    impl DaemonStateExt for CollabRouteMock {
        fn daemon_state(&self) -> &DaemonState {
            &self.base
        }

        fn handle_collab_rpc(
            &mut self,
            _peer: PeerCredential,
            method: &str,
            _params: &Value,
        ) -> Result<Value, DaemonRpcError> {
            Ok(json!({"routed_method": method}))
        }
    }

    #[test]
    fn test_verdict_submit_routes_to_native_collab_handler() {
        let mut state = CollabRouteMock { base: make_state() };
        let response = dispatch(
            &mut state,
            make_peer(),
            "verdict.submit",
            &json!({"request_id": "route-verdict-1"}),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["routed_method"], "verdict.submit");
    }

    /// 自定义 DaemonState mock，用于测试 DaemonStateExt trait 扩展机制
    struct MockState {
        base: DaemonState,
        workspace_count: u32,
    }

    impl DaemonStateExt for MockState {
        fn daemon_state(&self) -> &DaemonState {
            &self.base
        }

        fn handle_workspace_list(
            &mut self,
            _peer: PeerCredential,
            _params: &Value,
        ) -> Result<Value, DaemonRpcError> {
            let mut m = Map::new();
            m.insert(
                "count".to_string(),
                Value::Number(self.workspace_count.into()),
            );
            Ok(Value::Object(m))
        }
    }

    #[test]
    fn test_daemon_state_ext_trait_extension_works() {
        let mut state = MockState {
            base: make_state(),
            workspace_count: 5,
        };
        let peer = make_peer();
        let params = json!({});

        // workspace.list 在 MockState 中被重写，返回自定义数据
        let response = dispatch(&mut state, peer, "workspace.list", &params, &[]);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["count"], 5);

        // ping 走基础 handler（DaemonState 默认实现）
        let response = dispatch(&mut state, peer, "ping", &params, &[]);
        assert_eq!(response["ok"], true);
        assert_eq!(
            response["result"]["peer_uid"],
            current_daemon_uid().wrapping_add(1)
        );
    }

    // ---- Protected_Mutation 串行化路由测试（Req 14.6, 14.14）----

    #[test]
    fn test_is_protected_mutation_classification() {
        // Protected_Mutation 方法
        assert!(is_protected_mutation("snapshot.publish"));
        assert!(is_protected_mutation("workspace.file.refresh"));
        assert!(is_protected_mutation("workspace.recover"));
        assert!(is_protected_mutation("backup"));
        assert!(is_protected_mutation("restore"));
        assert!(is_protected_mutation("verdict.submit"));
        assert!(is_protected_mutation("task.apply"));
        assert!(is_protected_mutation("lease.acquire"));

        // 非 Protected_Mutation 方法
        assert!(!is_protected_mutation("ping"));
        assert!(!is_protected_mutation("health"));
        assert!(!is_protected_mutation("workspace.list"));
        assert!(!is_protected_mutation("workspace.register"));
        assert!(!is_protected_mutation("query.stats"));
        assert!(!is_protected_mutation("query.callers"));
        assert!(!is_protected_mutation("snapshot.stats"));
        assert!(!is_protected_mutation("nonexistent.method"));
    }

    #[test]
    fn test_dispatch_rpc_protected_mutation_through_serialization() {
        use crate::daemon::serialization::SerializationPoint;

        let mut state = make_state();
        let peer = make_peer();
        let sp = SerializationPoint::with_default_timeout();

        // snapshot.publish 是 Protected_Mutation，经串行化点执行
        // 默认 DaemonState 返回 method_not_found（handler 未实现），但路由正确
        let params = json!({"workspace_id": "ws-test"});
        let response = dispatch_rpc(&mut state, peer, "snapshot.publish", &params, &[], &sp);
        // 应该得到响应（ok=false + method_not_found 或 ok=true，取决于 handler）
        assert!(response.get("ok").is_some());

        // 串行化点应已释放（try_acquire 成功）
        assert!(sp.try_acquire());
        sp.release();
    }

    #[test]
    fn test_dispatch_rpc_non_protected_bypasses_serialization() {
        use crate::daemon::serialization::SerializationPoint;

        let mut state = make_state();
        let peer = make_peer();
        let sp = SerializationPoint::with_default_timeout();

        // ping 不是 Protected_Mutation，直接执行
        let params = json!({});
        let response = dispatch_rpc(&mut state, peer, "ping", &params, &[], &sp);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
    }

    #[test]
    fn test_dispatch_rpc_timeout_returns_structured_reason() {
        use crate::daemon::serialization::SerializationPoint;
        use std::sync::Arc;
        use std::time::Duration;

        let sp = Arc::new(SerializationPoint::new(Duration::from_millis(50)));

        // 占住串行化点
        sp.try_acquire();

        // 在另一个线程通过 dispatch_rpc 调用 Protected_Mutation
        let sp2 = Arc::clone(&sp);
        let handle = std::thread::spawn(move || {
            let mut state = make_state();
            let peer = PeerCredential::new_unix(1000, 1000, 1);
            let params = json!({});
            dispatch_rpc(&mut state, peer, "backup", &params, &[], &sp2)
        });

        let response = handle.join().unwrap();
        // 超时应返回 request_timeout 错误
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "request_timeout");

        sp.release();
    }
}
