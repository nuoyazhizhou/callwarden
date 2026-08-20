//! `CapabilityMutationGate`（foundation 独占，计划 §3.3）。
//!
//! workspace-keyed、daemon 内权威的变更门禁。每个公共 mutation 必须在**打开任何
//! authority-store 或 task-DB 写 transaction 之前**取得 gate，并持续持有到所有
//! 相关 transaction commit 或 rollback 完成。
//!
//! 全局锁序冻结为 `CapabilityMutationGate → Capability Authority store transaction →
//! task-DB transaction`；其中不使用的后两项可跳过，但任何路径都不得持有 DB
//! transaction 等待 gate，或在持有 task-DB transaction 后再取得 authority-store lock。
//!
//! 内存 permit 检查不是授权终点：`acquire` 之后、提交之前仍必须通过
//! `revalidate_public_permit` 完成最终复核。foundation 阶段该复核 fail-closed。

use std::sync::{Mutex, MutexGuard};

use rusqlite::Connection;

use crate::daemon::dispatch::DaemonRpcError;
use super::preflight;
use super::types::{FrozenAuthorityInput, PublicPreflightPermit};

/// 持有一座 workspace 门禁的守卫；生命周期内调用方可安全地进行 authority/task 写事务。
pub struct GateGuard<'a> {
    _guard: MutexGuard<'a, ()>,
}

/// workspace-keyed 的变更门禁（foundation 占位）。
///
/// foundation 阶段以单一串行化门禁承载任意 workspace 的变更，保证锁序
/// `CapabilityMutationGate → authority-store → task-DB` 不被破坏。按 workspace 的
/// 独立并行门禁（提升吞吐）交由领域任务在 `CapabilityMutationGate` 内部精化。
#[derive(Debug, Default)]
pub struct CapabilityMutationGate {
    inner: Mutex<()>,
}

impl CapabilityMutationGate {
    /// 在写任意 authority/task 事务前取得 gate。`workspace_id` 用于语义归属；foundation
    /// 阶段所有 workspace 收敛到同一串行化点。
    pub fn acquire(&self, _workspace_id: &str) -> Result<GateGuard<'_>, DaemonRpcError> {
        let guard = self
            .inner
            .lock()
            .map_err(|_| DaemonRpcError::internal_error("capability gate poisoned"))?;
        Ok(GateGuard { _guard: guard })
    }

    /// gate 内最终复核：在提交前重新核对 authority/fingerprint/evidence（§4.3）。
    ///
    /// 由公共 route 每次 mutation 前的 admission 终点与每次公共 mutation 的提交前
    /// revalidation 调用。`conn` 为 task-DB 只读借用，用于重算当前 schema/rules
    /// fingerprint；0A/0B 完整 authority store 落地后在本函数内叠加有效性复核。
    pub fn revalidate_public_permit(
        &self,
        conn: &Connection,
        permit: &PublicPreflightPermit,
        frozen: &FrozenAuthorityInput,
    ) -> Result<(), DaemonRpcError> {
        preflight::revalidate_public_permit(conn, frozen, permit)
    }
}

// GateGuard 借有 `MutexGuard<()>`（其 `!Send`），因此 GateGuard 天然不可跨线程发送，
// 无需显式负向 trait bound。