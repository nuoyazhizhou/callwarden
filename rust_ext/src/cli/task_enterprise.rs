//! BR-03：Rust `cw` task.create 的 daemon workspace 绑定 parity。
//!
//! 对齐 `cli/main.py` BR-02 的同一治理语义（role-prompt-v1 work order BR-03，
//! predecessor BR-02_COMPLETE）：
//! - steps[0] `resolve_workspace_pair_from_daemon_status`：从 daemon 解析权威
//!   `(workspace_id, workspace_instance_id)` 配对，任一步失败 fail-closed，
//!   绝不本地推导（forbid_numeric_guess）、绝不合成 `ws-{id}`
//!   （forbid_synthetic_ws_id）、绝不回退 active workspace
//!   （forbid_active_workspace_fallback）；
//! - steps[1] `forward_workspace_id_and_workspace_instance_id`：显式 flag 优先，
//!   缺失项用 daemon 解析结果补全，pair 原样转发 `task.create`
//!   （daemon 0c 配对校验兜底）；
//! - steps[2] `render_create_provenance_and_compare_readback`：create 响应渲染
//!   task/binding/capture/assignment 标识，并与 `task.status` readback 逐一对比，
//!   不一致 fail-closed；
//! - 本地 numeric active 语义（`resolve_local_workspace_id`）**绝不进入**
//!   enterprise 参数（workspace_authority: forbid_active_workspace_fallback）。

use serde_json::{json, Value};

use super::runtime::RuntimeOptions;

/// daemon `task.create` / `task.status` 响应中的 provenance 五键
/// （对齐 Python `cli/main.py:_CREATE_PROVENANCE_KEYS`）。
pub const CREATE_PROVENANCE_KEYS: [&str; 5] = [
    "workspace_id",
    "workspace_instance_id",
    "workspace_binding_id",
    "workspace_capture_id",
    "assignment_id",
];

/// 从 `mcp.daemon_client.inject_workspace_id` 响应解析权威 `workspace_id`。
///
/// 响应形状：`{"params": {"workspace_id": N, ...}, "injected": bool}`。
/// 缺 workspace_id / 非整数 / ≤ 0 → fail-closed（不猜数字、不补 active）。
pub fn parse_injected_workspace_id(injected: &Value) -> Result<i64, String> {
    let raw = injected
        .get("params")
        .and_then(|params| params.get("workspace_id"))
        .ok_or_else(|| {
            "resolve_workspace_pair: daemon 未解析出 workspace_id（无 active workspace，fail-closed）"
                .to_string()
        })?;
    let workspace_id = raw
        .as_i64()
        .or_else(|| raw.as_str().and_then(|s| s.trim().parse::<i64>().ok()))
        .ok_or_else(|| {
            format!("resolve_workspace_pair: workspace_id 非整数，无法解析: {raw}")
        })?;
    if workspace_id <= 0 {
        return Err(format!(
            "resolve_workspace_pair: workspace_id 必须 > 0，实际 {workspace_id}"
        ));
    }
    Ok(workspace_id)
}

/// 从 `workspace.status` 响应解析权威 `workspace_instance_id`。
///
/// 缺 instance / 空白 → fail-closed（不合成 `ws-{id}`、不回退 active workspace）。
pub fn parse_status_instance_id(status: &Value) -> Result<String, String> {
    status
        .get("workspace_instance_id")
        .and_then(|v| v.as_str())
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .map(str::to_string)
        .ok_or_else(|| {
            format!(
                "resolve_workspace_pair: workspace.status 未返回权威 workspace_instance_id: {status}"
            )
        })
}

/// BR-03 steps[0]：从 daemon 解析 `(workspace_id, workspace_instance_id)` 权威配对。
///
/// 1. `mcp.daemon_client.inject_workspace_id` → daemon 基于权威库解析 active
///    workspace（无 active workspace 时 fail-closed）；
/// 2. `workspace.status {workspace_id}` → daemon 注册表行，取 `workspace_instance_id`；
/// 3. 任一步失败（无 active / 未注册 / daemon 不可达）→ 上抛，绝不本地推导。
pub fn resolve_workspace_pair_from_daemon(
    runtime: &RuntimeOptions,
) -> Result<(i64, String), String> {
    let injected = runtime.daemon_call(
        "mcp.daemon_client.inject_workspace_id",
        json!({"params": {}}),
    )?;
    let workspace_id = parse_injected_workspace_id(&injected)?;
    let status = runtime
        .daemon_call("workspace.status", json!({"workspace_id": workspace_id}))
        .map_err(|error| {
            format!(
                "resolve_workspace_pair: workspace.status 无法解析权威配对（workspace_id={workspace_id}）: {error}"
            )
        })?;
    let workspace_instance_id = parse_status_instance_id(&status)?;
    Ok((workspace_id, workspace_instance_id))
}

/// 解析显式 `--workspace-id`（全局 flag，String 承载；对齐 Python argparse
/// `type=int` 的校验语义）。
///
/// - `None` / 空白 → `Ok(None)`（未提供，走 daemon 解析）；
/// - 可解析整数且 > 0 → `Ok(Some(id))`；
/// - 非整数或 ≤ 0 → `Err` fail-closed（显式提供但非法，绝不静默忽略、
///   绝不回退 active workspace / 本地 numeric 推导）。
pub fn parse_explicit_workspace_id(explicit: Option<&str>) -> Result<Option<i64>, String> {
    let Some(raw) = explicit else {
        return Ok(None);
    };
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return Ok(None);
    }
    let workspace_id = trimmed.parse::<i64>().map_err(|_| {
        format!("--workspace-id 必须是整数（daemon 权威 numeric id），实际 {raw:?}")
    })?;
    if workspace_id <= 0 {
        return Err(format!(
            "--workspace-id 必须 > 0（daemon 权威 numeric id），实际 {workspace_id}"
        ));
    }
    Ok(Some(workspace_id))
}

/// 将 JSON 值规范化为可比较字符串（对齐 Python `str(created) != str(rb_val)`）。
fn value_str(value: &Value) -> String {
    match value {
        Value::Null => String::new(),
        Value::Number(number) => number.to_string(),
        Value::String(string) => string.clone(),
        other => other.to_string(),
    }
}

/// BR-03 steps[2] compare_readback：create 响应与 `task.status` readback 逐一对比。
///
/// 对比键 = [`CREATE_PROVENANCE_KEYS`]；`assignment_id` 可缺省/为 null
/// （create 或 readback 一方缺失/null 时容忍），其余键缺失或值不一致 →
/// fail-closed 上抛。
pub fn compare_provenance_readback(create_res: &Value, readback: &Value) -> Result<(), String> {
    for key in CREATE_PROVENANCE_KEYS {
        // Python 语义：`create_res.get(key)` 对 json null 返回 None → 不参与对比。
        let created = create_res.get(key).filter(|value| !value.is_null());
        let read = readback.get(key).filter(|value| !value.is_null());
        match (created, read) {
            (None, _) => continue,
            (Some(_), None) if key == "assignment_id" => continue,
            (Some(_), None) => {
                return Err(format!(
                    "task.status readback 缺少 {key}（create 有值）→ fail-closed"
                ))
            }
            (Some(created_value), Some(read_value)) => {
                let created_text = value_str(created_value);
                let read_text = value_str(read_value);
                if created_text != read_text {
                    return Err(format!(
                        "task.create/task.status provenance 不一致：{key} \
                         create={created_text} vs readback={read_text}（fail-closed）"
                    ));
                }
            }
        }
    }
    Ok(())
}

/// BR-03 steps[2]：create 后调用 `task.status` 做 readback 校验（fail-closed）。
pub fn verify_create_readback(runtime: &RuntimeOptions, create_res: &Value) -> Result<(), String> {
    let task_id = create_res
        .get("task_id")
        .and_then(|value| value.as_str())
        .unwrap_or("")
        .trim();
    if task_id.is_empty() {
        return Ok(());
    }
    let readback = runtime.daemon_call("task.status", json!({"task_id": task_id}))?;
    compare_provenance_readback(create_res, &readback)
}

/// BR-03 steps[2] render_create_provenance：渲染 create 返回的
/// task/binding/capture/assignment 标识（对齐 Python `_render_create_provenance`）。
///
/// 缺失键不渲染（不伪造）；`step_count` 一并渲染。纯文本行，供 enterprise
/// create 输出复用与测试断言。
pub fn render_create_provenance(create_res: &Value, zh_cn: bool) -> String {
    let mut lines = vec![
        if zh_cn {
            "  [Workspace 权威绑定]".to_string()
        } else {
            "  [Workspace Authority Binding]".to_string()
        },
    ];
    for key in CREATE_PROVENANCE_KEYS {
        if let Some(value) = create_res.get(key) {
            if !value.is_null() {
                lines.push(format!("    {key}: {}", value_str(value)));
            }
        }
    }
    if let Some(count) = create_res.get("step_count") {
        if !count.is_null() {
            lines.push(format!("    step_count: {}", value_str(count)));
        }
    }
    lines.join("\n")
}

#[cfg(test)]
mod tests {
    use super::*;

    fn runtime() -> RuntimeOptions {
        RuntimeOptions {
            mode: crate::cli::router::DaemonMode::Enterprise,
            // 显式指向不存在的 pipe：Windows 上 daemon_call 对非 `\\.\pipe\` 前缀
            // socket 会 fallback 到真实 `\\.\pipe\callwarden-{SID}`（daemon 存活时
            // 会真连上，破坏 fail-closed 测试假设）；用明确的空 pipe 保证任何环境
            // 下都不可达（CreateFileW → INVALID_HANDLE_VALUE → fail-closed）。
            socket_path: std::path::PathBuf::from(r"\\.\pipe\callwarden-test-nonexistent"),
            db_path: std::path::PathBuf::from("unused.db"),
            workspace_id: None,
            timeout: std::time::Duration::from_secs(1),
        }
    }

    #[test]
    fn parses_pair_from_daemon_responses() {
        let injected = json!({"params": {"workspace_id": 10, "q": 1}, "injected": true});
        assert_eq!(parse_injected_workspace_id(&injected).unwrap(), 10);
        let status = json!({"workspace_id": 10, "workspace_instance_id": "2bba6e894ee2546f"});
        assert_eq!(
            parse_status_instance_id(&status).unwrap(),
            "2bba6e894ee2546f"
        );
    }

    #[test]
    fn parses_numeric_string_workspace_id() {
        let injected = json!({"params": {"workspace_id": "72"}, "injected": true});
        assert_eq!(parse_injected_workspace_id(&injected).unwrap(), 72);
    }

    #[test]
    fn injected_missing_workspace_id_fails_closed() {
        let injected = json!({"params": {}, "injected": true});
        let error = parse_injected_workspace_id(&injected).unwrap_err();
        assert!(error.contains("未解析出 workspace_id"));
    }

    #[test]
    fn injected_non_numeric_fails_closed() {
        let injected = json!({"params": {"workspace_id": "ws-not-numeric"}, "injected": true});
        let error = parse_injected_workspace_id(&injected).unwrap_err();
        assert!(error.contains("非整数"));
    }

    #[test]
    fn injected_zero_or_negative_fails_closed() {
        let error = parse_injected_workspace_id(&json!({"params": {"workspace_id": 0}}))
            .unwrap_err();
        assert!(error.contains("必须 > 0"));
        let error = parse_injected_workspace_id(&json!({"params": {"workspace_id": -3}}))
            .unwrap_err();
        assert!(error.contains("必须 > 0"));
    }

    #[test]
    fn status_missing_instance_fails_closed() {
        let status = json!({"workspace_id": 10});
        let error = parse_status_instance_id(&status).unwrap_err();
        assert!(error.contains("未返回权威 workspace_instance_id"));
        let error = parse_status_instance_id(&json!({"workspace_instance_id": "  "})).unwrap_err();
        assert!(error.contains("未返回权威 workspace_instance_id"));
    }

    #[test]
    fn readback_exact_match_passes() {
        let create = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "2bba6e894ee2546f",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": "wc-1",
            "assignment_id": "a-1",
        });
        let readback = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "2bba6e894ee2546f",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": "wc-1",
            "assignment_id": "a-1",
        });
        assert!(compare_provenance_readback(&create, &readback).is_ok());
    }

    #[test]
    fn readback_numeric_vs_string_workspace_id_tolerated() {
        let create = json!({"workspace_id": 10, "workspace_instance_id": "ws-i"});
        let readback = json!({"workspace_id": "10", "workspace_instance_id": "ws-i"});
        assert!(compare_provenance_readback(&create, &readback).is_ok());
    }

    #[test]
    fn readback_mismatch_fails_closed() {
        let create = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "2bba6e894ee2546f",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": "wc-1",
            "assignment_id": "a-1",
        });
        let readback = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "other-instance",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": "wc-1",
            "assignment_id": "a-1",
        });
        let error = compare_provenance_readback(&create, &readback).unwrap_err();
        assert!(error.contains("workspace_instance_id"));
        assert!(error.contains("fail-closed"));
    }

    #[test]
    fn readback_missing_non_assignment_key_fails_closed() {
        let create = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "ws-i",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": "wc-1",
        });
        let readback = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "ws-i",
            "workspace_binding_id": "wb-1",
            // workspace_capture_id 缺失
        });
        let error = compare_provenance_readback(&create, &readback).unwrap_err();
        assert!(error.contains("readback 缺少 workspace_capture_id"));
    }

    #[test]
    fn readback_null_assignment_tolerated() {
        let create = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "ws-i",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": "wc-1",
            "assignment_id": null,
        });
        let readback = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "ws-i",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": "wc-1",
            "assignment_id": "a-1",
        });
        // create 侧 null → 不参与对比；readback 侧有值不影响
        assert!(compare_provenance_readback(&create, &readback).is_ok());
    }

    #[test]
    fn readback_created_missing_keys_skipped() {
        let create = json!({"task_id": "T-x", "workspace_id": 10});
        let readback = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "ws-i",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": "wc-1",
            "assignment_id": "a-1",
        });
        assert!(compare_provenance_readback(&create, &readback).is_ok());
    }

    #[test]
    fn render_provenance_lists_five_keys_and_step_count() {
        let create = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "2bba6e894ee2546f",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": "wc-1",
            "assignment_id": "a-1",
            "step_count": 3,
        });
        let rendered = render_create_provenance(&create, false);
        for key in CREATE_PROVENANCE_KEYS {
            assert!(rendered.contains(key), "missing {key} in {rendered}");
        }
        assert!(rendered.contains("step_count: 3"));
        assert!(rendered.contains("Workspace Authority Binding"));
    }

    #[test]
    fn render_provenance_skips_null_and_missing_keys() {
        let create = json!({
            "task_id": "T-x",
            "workspace_id": 10,
            "workspace_instance_id": "ws-i",
            "workspace_binding_id": "wb-1",
            "workspace_capture_id": null,
            "assignment_id": null,
        });
        let rendered = render_create_provenance(&create, true);
        assert!(rendered.contains("workspace_binding_id: wb-1"));
        assert!(!rendered.contains("workspace_capture_id: null"));
        assert!(!rendered.contains("assignment_id"));
        assert!(rendered.contains("Workspace 权威绑定"));
    }

    #[test]
    fn explicit_workspace_id_none_or_blank_resolves_from_daemon() {
        assert_eq!(parse_explicit_workspace_id(None).unwrap(), None);
        assert_eq!(parse_explicit_workspace_id(Some("  ")).unwrap(), None);
    }

    #[test]
    fn explicit_workspace_id_numeric_parsed() {
        assert_eq!(parse_explicit_workspace_id(Some("72")).unwrap(), Some(72));
        assert_eq!(
            parse_explicit_workspace_id(Some("  72 ")).unwrap(),
            Some(72)
        );
    }

    #[test]
    fn explicit_workspace_id_non_numeric_fails_closed() {
        let error = parse_explicit_workspace_id(Some("ws-inst-test")).unwrap_err();
        assert!(error.contains("必须是整数"));
    }

    #[test]
    fn explicit_workspace_id_non_positive_fails_closed() {
        let error = parse_explicit_workspace_id(Some("0")).unwrap_err();
        assert!(error.contains("必须 > 0"));
        let error = parse_explicit_workspace_id(Some("-5")).unwrap_err();
        assert!(error.contains("必须 > 0"));
    }

    #[test]
    fn resolve_pair_uses_daemon_call_chain() {
        // resolve_workspace_pair_from_daemon 无法注入假 daemon，这里验证它
        // 对不可达 socket 是 fail-closed（而不是 panic 或返回占位 pair）。
        let runtime = runtime();
        let error = resolve_workspace_pair_from_daemon(&runtime).unwrap_err();
        assert!(
            error.contains("failed") || error.contains("失败") || error.contains("RPC"),
            "unexpected error: {error}"
        );
    }
}
