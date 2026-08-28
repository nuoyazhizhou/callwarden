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
#[path = "task_collab_tests_projection.rs"]
mod projection;
