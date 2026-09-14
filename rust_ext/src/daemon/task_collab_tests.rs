//! task_collab 测试模块入口。

use super::*;

#[path = "task_collab_test_support.rs"]
mod support;
#[path = "task_collab_tests_core.rs"]
mod core;
#[path = "task_collab_tests_lease.rs"]
mod lease;
#[path = "task_collab_tests_governance.rs"]
mod governance;
#[path = "task_collab_tests_p0l_repair.rs"]
mod p0l_repair;
#[path = "task_collab_tests_projection.rs"]
mod projection;
#[path = "task_collab_tests_parent_create.rs"]
mod parent_create;
#[path = "task_collab_tests_ws_authority.rs"]
mod ws_authority;
#[path = "task_collab_tests_bridge.rs"]
mod bridge;
#[path = "task_collab_tests_verdict_compat.rs"]
mod verdict_compat;
#[path = "task_collab_tests_cascade.rs"]
mod cascade;
