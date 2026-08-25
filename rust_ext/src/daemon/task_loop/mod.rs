//! task_loop_public 能力交付的 Executor foundation（计划 §3.3，1D0）。
//!
//! 本模块只承载 **foundation 私有类型** 与 **module declarations**，是
//! `task_loop_public` 公开能力在 daemon 内 authority/门禁的编译期骨架。
//! 它不做任何领域判断：在未通过 preflight/promotion（0A、1D3）之前，公共路由与
//! 全部已知公共 RPC 一律 fail-closed 为 `E_TASK_LOOP_CAPABILITY_DISABLED`。
//!
//! ## 所有权边界（谁可以编辑什么）
//! - foundation（本任务）独占：`mod.rs` 的 module declarations、`types.rs`、
//!   `executor.rs`、`operation_store.rs`、`route.rs`、`strict_transport.rs`、
//!   `preflight.rs`、`capability_control.rs`，以及 `dispatch.rs` 的静态
//!   `dispatch_task_loop` shim。
//! - 领域任务只替换自己的 stub：1A=`create.rs`、1B=`contract_set.rs`、
//!   1C=`claim.rs`、1F=`lifecycle_lease.rs`、任务 2=`report_handoff.rs`、
//!   任务 3=`verdict_evidence_gate.rs`。
//! - 领域模块文件由对应任务各自创建，**不在此处预声明引用**（引用不存在的文件会
//!   破坏编译，fail-closed）。其 `mod xxx;` 声明由该模块的拥有任务在 `mod.rs`
//!   中追加，避免与 foundation 并发编辑。
//!
//! ## failure 语义
//! - 无 permit → `E_TASK_LOOP_CAPABILITY_DISABLED`（cutover 前 route 唯一返回）。
//! - 已装载 permit 但 revalidation 失效 → `E_TASK_LOOP_CAPABILITY_REVOKED`。

pub mod types;
pub mod executor;
pub mod operation_store;
pub mod route;
pub mod strict_transport;
pub mod preflight;
pub mod capability_control;
pub mod create;
pub mod contract_set;
pub mod claim;
pub mod lifecycle_lease;
pub mod promotion;
pub mod report_handoff;
pub mod verdict_evidence_gate;
pub mod next_action;
pub mod task_contract_bootstrap;

#[cfg(test)]
mod capability_control_test;
#[cfg(test)]
mod create_test;
#[cfg(test)]
mod contract_set_test;
#[cfg(test)]
mod claim_test;
#[cfg(test)]
mod route_test;
#[cfg(test)]
mod promotion_test;
#[cfg(test)]
mod report_handoff_test;
#[cfg(test)]
mod verdict_evidence_gate_test;
#[cfg(test)]
mod lifecycle_lease_test;
#[cfg(test)]
mod next_action_test;
#[cfg(test)]
mod task_contract_bootstrap_test;
