//! Role Prompt Compiler v1 task_prompt domain。
//!
//! RP-03：单 snapshot authority context（`context.rs`）。
//! RP-04：路由与模板选择（`route.rs`）、renderer 组合/裁剪/预算/context
//! hash（`render.rs`）、canonical JSON v1（`canonical.rs`）、secret
//! denylist（`redaction.rs`）、bundle 组装与 bundle_hash（`bundle.rs`）。
//! RP-05：唯一生产 RPC `task.prompt.compile` 域 handler 与 capability
//! `role_prompt_compiler_v1` 投影（`handler.rs`）。本文件仅做模块
//! 声明，不含业务逻辑。

pub mod bundle;
pub mod canonical;
pub mod context;
pub mod handler;
pub mod redaction;
pub mod render;
pub mod route;

#[cfg(test)]
mod bundle_tests;
#[cfg(test)]
mod canonical_tests;
#[cfg(test)]
mod context_tests;
#[cfg(test)]
mod redaction_tests;
#[cfg(test)]
mod render_tests;
#[cfg(test)]
mod route_tests;
#[cfg(test)]
mod rpc_tests;
