//! CLI-083：`cw task findings` 的 daemon-native transport handler。
//!
//! `task_quality_findings` 的查询语义与稳定错误码由既有 `TaskCollabStore`
//! 权威实现保留。本模块只将 CLI 专用 dispatch 路由显式收敛到该 authority，
//! 使 Python `cli/main.py::_local_findings` 不再拥有本地数据库回退路径。

use serde_json::Value;

use super::{DaemonRpcError, PeerCredential};
use crate::daemon::task_collab::TaskCollabStore;

/// 处理 `task.quality_findings` 的 CLI/HTTP 请求。
///
/// 直接委托权威 TaskCollabStore，保留原有参数筛选、响应字段与错误码；不读取
/// Python 本地数据库，也不实现任何兼容回退。
pub(crate) fn handle_get_task_quality_findings(
    store: &TaskCollabStore,
    peer: PeerCredential,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    store.handle_task_quality_findings(peer, params)
}
