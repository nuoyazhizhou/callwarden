//! Task 读面 handler —— task_projection 端口只读工具。
//!
//! 对应 `tool_migration_matrix.json` 中 port_type=task_projection、
//! target_backend=rust_native 的只读工具。
//!
//! 当前实现：
//! - MCP-070（T-1787321713551-fb94f87c）：task_plan_template 从 python_compat
//!   迁移为 rust_native。返回静态模板字符串（与 Python db/db_tasks.py
//!   task_plan_template 默认值一致）。

use serde_json::{json, Value};

use crate::daemon::dispatch::DaemonRpcError;

/// MCP-070（T-1787321713551-fb94f87c）：task_plan_template 从 python_compat
/// 迁移为 rust_native。语义与 Python db/db_tasks.py::task_plan_template 一致：
/// 返回 task_create_from_plan 的标准格式 Markdown 模板字符串。
///
/// 本 handler 为纯静态实现，不依赖任何 DB 连接。返回的模板与 Python 默认值
/// `t("cli.messages.task_plan_template", default="...")` 保持一致。
pub fn handle_task_plan_template() -> Result<Value, DaemonRpcError> {
    let template = r#"# {Root task title}
{Root task description (plain text)}

## {Subtask 1 title}
{Subtask 1 description (optional)}

- {Step 1 description}
- {Step 2 description}
- [ ] {Incomplete step (checkbox format)}
- [x] {Completed step}

### {Step group title (optional)}
- {Step 3 description}
- {Step 4 description}

## {Subtask 2 title}
{Subtask 2 description (optional)}

- {Step 5 description}
- {Step 6 description}
"#;

    Ok(json!({
        "template": template,
    }))
}