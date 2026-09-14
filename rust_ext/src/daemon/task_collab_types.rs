//! `task_collab` 的共享状态与身份类型。
//!
//! 领域实现分散在多个 sibling module 中，但所有 RPC 仍共享同一个
//! `TaskCollabStore`。本模块只放跨领域的持久状态和 action provenance 类型，
//! 不放任何 handler 或数据库业务流程。

use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use rusqlite::Connection;
use serde_json::Value;

use crate::daemon::clock::AuthoritativeClock;

/// 任务协同 daemon 的唯一共享存储。
///
/// `conn`、sequence、dedup cache 与 authoritative clock 的 ownership 保持
/// 与拆分前一致；各领域模块通过 `impl TaskCollabStore` 添加 handler，禁止
/// 创建第二连接或绕过该锁直接写任务库。
pub struct TaskCollabStore {
    pub(crate) conn: Arc<Mutex<Connection>>,
    pub(crate) seq_counter: Arc<Mutex<i64>>,
    pub(crate) dedup_cache: Arc<Mutex<HashMap<String, Value>>>,
    /// daemon 权威时钟（lease 受保护写校验必需）。
    /// 为 None 时，携带 lease 凭证的写操作 fail-closed（E_LEASE_CLOCK_UNAVAILABLE）。
    pub(crate) clock: Option<Arc<AuthoritativeClock>>,
    /// daemon 启动配置指定的 workspace registry。Task governance 的
    /// snapshot/binding 校验必须读取同一 authority，不能在 Windows runtime
    /// 回退到 Linux 的默认 `/var/lib/callwarden/registry.db`。
    pub(crate) registry_db_path: PathBuf,
    /// 单元测试的隔离 workspace registry。生产路径只能使用 daemon 配置的
    /// registry；该字段在非测试构建中不存在，不能成为运行时 authority fallback。
    #[cfg(test)]
    pub(crate) registry_db_path_for_tests: Option<std::path::PathBuf>,
}

/// 解析并记录到 `action_identities` 的运行身份。
///
/// 该类型是跨 identity、contract、lease、lifecycle 和 verdict domain 的
/// 共享 provenance 数据；字段语义保持与原 `task_collab.rs` 完全一致。
#[derive(Clone, Debug)]
pub(crate) struct ActionIdentity {
    pub(crate) agent_id: String,
    pub(crate) agent_instance_id: String,
    pub(crate) client_id: String,
    pub(crate) provider: String,
    pub(crate) model_id: String,
    pub(crate) model_mode: String,
    pub(crate) system_fingerprint: String,
    pub(crate) session_id: String,
    pub(crate) role: String,
    pub(crate) runtime_hash: String,
}
