//! 公共 `dispatch_task_loop` 路由与内部 validation 路由（计划 §3.3、1D3A/1D3B）。
//!
//! `dispatch.rs` 的静态 `dispatch_task_loop` shim **永远**调用本模块的公共入口。
//! 1D3B 落地后：
//! - `ExternalTransport` 只接受当前已安装并经最终复核的 `PublicPreflightPermit`：
//!   单一串行化点 `CapabilityMutationGate` → `revalidate_public_permit`（重算当前
//!   schema/rules/generation fingerprint）通过后，才真实分派领域 handler；
//! - 无 permit / permit fingerprint 失配一律 `E_TASK_LOOP_CAPABILITY_DISABLED`。
//!
//! 内部 validation 路由 `dispatch_internal_validation` 只接受
//! `InvocationClass::InternalValidation` + 经 `preflight::verify_internal_permit`
//! 最终复核的 `InternalPreflightPermit`（daemon 内私有 in-process validation API
//! 使用），**绝不**安装到 public route（§4.3 / §6 行 622、630）。

use rusqlite::Connection;

use crate::daemon::dispatch::DaemonRpcError;
use super::capability_control::CapabilityMutationGate;
use super::create::{create_task, CreateTaskInput, LedgerKey, WorkspaceCaptureInput};
use super::preflight;
use super::promotion::PublicPermitStore;
use super::types::{
    ERR_CAPABILITY_DISABLED, FrozenAuthorityInput, InternalPreflightPermit, InvocationClass,
    PublicPreflightPermit, StrictParsedEnvelope,
};

/// 已知的公共 RPC 方法（1D3B 后仅在 permit 到位时开放）。
pub const KNOWN_PUBLIC_METHODS: &[&str] = &["task_loop.public_promote", "task.create"];

/// 路由上下文：dispatch shim（`dispatch.rs`）从 daemon state 构造并提供。
pub struct RouteContext<'a> {
    /// task-DB connection（写路径；经 executor 使用）。
    pub conn: &'a mut Connection,
    /// 进入路由前冻结的权威输入（registry/clock/identity/recheck 结果）。
    pub frozen: &'a FrozenAuthorityInput,
    /// 内存 Public permit 注册表（1D3B promotion 安装）。
    pub store: &'a PublicPermitStore,
    /// workspace-keyed 变更门禁（锁序 `gate → task-DB transaction`）。
    pub gate: &'a CapabilityMutationGate,
}

/// 公共路由入口（`ExternalTransport` 专用）。
///
/// admission：仅当该 workspace 已安装 `PublicPreflightPermit` 且最终复核通过才
/// 真实分派；无 permit / fingerprint 失配一律 disabled。最终复核在打开任何写
/// transaction 之前执行，内存 permit 不是授权终点。
pub fn dispatch_task_loop(
    ctx: &mut RouteContext<'_>,
    envelope: &StrictParsedEnvelope,
) -> Result<serde_json::Value, DaemonRpcError> {
    if envelope.invocation_class != InvocationClass::ExternalTransport {
        return Err(DaemonRpcError::new(
            ERR_CAPABILITY_DISABLED,
            "公共路由只受理 ExternalTransport：内部 validation 请走私有路径",
        ));
    }

    // 1. 内存 permit 检查（不是授权终点）。
    let permit = ctx.store.get(&envelope.workspace_instance_id).ok_or_else(|| {
        DaemonRpcError::new(
            ERR_CAPABILITY_DISABLED,
            format!(
                "task_loop 公共能力未启用（workspace 未发布 permit: {}）",
                envelope.workspace_instance_id
            ),
        )
    })?;

    // 2. 单一串行化点：gate → task-DB transaction（§4.3 锁序）。
    let _guard = ctx.gate.acquire(&permit.workspace_id)?;

    // 3. admission 终点：最终复核（重算当前 schema/rules/generation fingerprint）。
    ctx.gate.revalidate_public_permit(ctx.conn, &permit, ctx.frozen)?;

    match envelope.canonical_method.as_str() {
        "task.create" => dispatch_create(ctx, envelope, &permit),
        _ => Err(DaemonRpcError::new(
            ERR_CAPABILITY_DISABLED,
            format!("task_loop 方法尚未开放公共能力：{}", envelope.canonical_method),
        )),
    }
}

/// 内部 validation 路由（1D3A 内部路由表；daemon 私有 in-process validation API）。
///
/// admission：仅 `InvocationClass::InternalValidation`；permit 必须经
/// `preflight::verify_internal_permit` 重算指纹复核通过（内存 permit 不是授权
/// 终点）。通过后按 `canonical_method` 真实分派领域 handler。
pub fn dispatch_internal_validation(
    ctx: &mut RouteContext<'_>,
    envelope: &StrictParsedEnvelope,
    permit: &InternalPreflightPermit,
    ws: Option<&WorkspaceCaptureInput>,
) -> Result<serde_json::Value, DaemonRpcError> {
    if envelope.invocation_class != InvocationClass::InternalValidation {
        return Err(DaemonRpcError::new(
            ERR_CAPABILITY_DISABLED,
            "内部 validation 路由拒绝 ExternalTransport：请经公共路由受理",
        ));
    }

    // admission 终点：在任务打开任何写 transaction 前完成 permit 最终复核。
    preflight::verify_internal_permit(ctx.conn, ctx.frozen, permit)?;

    match envelope.canonical_method.as_str() {
        "task.create" => {
            let ws = ws.ok_or_else(|| {
                DaemonRpcError::invalid_params(
                    "task.create 需要 daemon 校验后的 workspace capture 输入",
                )
            })?;
            // 锁序：gate → task-DB transaction。
            let _guard = ctx.gate.acquire(&ws.workspace_id.to_string())?;
            let ledger_key = LedgerKey {
                workspace_instance_id: envelope.workspace_instance_id.clone(),
                method: envelope.canonical_method.clone(),
                request_id: envelope.request_id.clone(),
            };
            let input = CreateTaskInput::from_params(&envelope.params)?;
            create_task(ctx.conn, ctx.frozen, &ledger_key, &input, ws)
        }
        _ => Err(DaemonRpcError::new(
            ERR_CAPABILITY_DISABLED,
            format!("task_loop 方法尚未接入内部路由：{}", envelope.canonical_method),
        )),
    }
}

/// `task.create` 的公共分派。
///
/// registry 数据管道尚未接入 Rust daemon：workspace capture 数据（
/// `client_view_root_hash` 等）暂由请求携带并经 `WorkspaceCaptureInput::from_params`
/// 严格解析，且必须与 permit 绑定在同一 workspace；registry 管道落地后应由 daemon
/// 从 registry 填充并移除客户端提交字段。
fn dispatch_create(
    ctx: &mut RouteContext<'_>,
    envelope: &StrictParsedEnvelope,
    permit: &PublicPreflightPermit,
) -> Result<serde_json::Value, DaemonRpcError> {
    let ws = WorkspaceCaptureInput::from_params(&envelope.params)?;
    if ws.workspace_instance_id != permit.workspace_id {
        return Err(DaemonRpcError::new(
            ERR_CAPABILITY_DISABLED,
            format!(
                "task.create workspace 与 permit 不一致 (request={} permit={})",
                ws.workspace_instance_id, permit.workspace_id
            ),
        ));
    }
    let ledger_key = LedgerKey {
        workspace_instance_id: envelope.workspace_instance_id.clone(),
        method: envelope.canonical_method.clone(),
        request_id: envelope.request_id.clone(),
    };
    let input = CreateTaskInput::from_params(&envelope.params)?;
    create_task(ctx.conn, ctx.frozen, &ledger_key, &input, &ws)
}
