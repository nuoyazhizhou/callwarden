//! Role Prompt Compiler v1 — 唯一生产 RPC `task.prompt.compile` 的域 handler。
//!
//! 冻结 authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md
//! §4.1 / §4.2 / §8.1-3 / §10.2 / §11.3。
//!
//! 职责（RP-05 卡冻结 scope，仅此而已）：
//! - §4.1 严格 request schema：只接受 `task_id`（必填）与可选
//!   `expected_workspace_instance_id`；未知/禁用字段 fail closed；
//! - §4.1-5 workspace 自解析：真实 workspace 从 task 不可变 binding 行
//!   （`workspace_capture_id`）读取，客户端不得选择 workspace 或合成 ID；
//! - §4.2 workspace guard：caller assertion 仅用于过期检测，mismatch →
//!   `E_TASK_PROMPT_AUTHORITY_MISMATCH`，guard 值不进入任何 hash；
//! - §11.3 capability `role_prompt_compiler_v1`：未启用时 public RPC fail
//!   closed；health/capability 投影返回 schema version、manifest hash、
//!   compiler policy hash 与 enabled 状态；
//! - 组装完全复用 RP-03/RP-04 域 API（aggregate_authority_context →
//!   route_and_select → render → compile_bundle），全部读操作发生在
//!   caller 提供的同一只读 Connection 上（单 snapshot，不递归 JSON-RPC）；
//! - §8.1-3：task title/description 等不可信文本只进入第 3 段
//!   （CW_UNTRUSTED_TASK_DATA canonical JSON 块），绝不插值进模板正文。
//!
//! 明确不做：任何 DB 写入、lease/claim/assignment/event/lifecycle mutation、
//! dispatch 业务逻辑（dispatch.rs 只做薄注册）、CLI/MCP/Skill 层语义。

use rusqlite::{Connection, OptionalExtension};
use serde_json::{json, Value};

use crate::daemon::dispatch::DaemonRpcError;
use crate::daemon::task_prompt::bundle::compile_bundle;
use crate::daemon::task_prompt::canonical::normalize_sha256_form;
use crate::daemon::task_prompt::context::aggregate_authority_context;
use crate::daemon::task_prompt::route::{
    route_and_select, verify_registry_integrity, E_TASK_PROMPT_TEMPLATE_INVALID,
};

// Build-generated constants（经 route.rs 的 include! 进入 route 模块，
// 这里按路径引用；runtime 只消费 generated constants，§7.4）。
use crate::daemon::task_prompt::route::{
    ROLE_PROMPTS_COMPILER_POLICY_SHA256, ROLE_PROMPTS_MANIFEST_PRESENT,
    ROLE_PROMPTS_MANIFEST_SHA256,
};

/// §11.3：正式 capability 名称。
pub const CAPABILITY_NAME: &str = "role_prompt_compiler_v1";
/// capability 投影自身的 schema 版本。
pub const CAPABILITY_SCHEMA_VERSION: &str = "role_prompt_capability_v1";

/// §10.2：task_id 缺失/空/类型错误，或 request 含未知字段（spec §4.1-3
/// 未知字段 fail closed；表内无独立 request-invalid 码，归入本码，
/// message 说明具体原因）。
pub const E_TASK_PROMPT_TASK_ID_REQUIRED: &str = "E_TASK_PROMPT_TASK_ID_REQUIRED";
/// §4.2/§10.2：optional caller guard 过期（retry: after_authority_refresh）。
pub const E_TASK_PROMPT_AUTHORITY_MISMATCH: &str = "E_TASK_PROMPT_AUTHORITY_MISMATCH";

/// 严格解析 §4.1 request schema。
struct CompileRequest {
    task_id: String,
    expected_workspace_instance_id: Option<String>,
}

fn parse_request(params: &Value) -> Result<CompileRequest, DaemonRpcError> {
    let obj = params.as_object().ok_or_else(|| {
        DaemonRpcError::new(
            E_TASK_PROMPT_TASK_ID_REQUIRED,
            "params 必须是 JSON object（仅接受 task_id 与可选 expected_workspace_instance_id）",
        )
    })?;
    // §4.1-3/4：未知字段 fail closed；role/mode/preview_role/context_profile/
    // format/identity/credential/lease/fencing/DB 路径等一律不在此列。
    for key in obj.keys() {
        if key != "task_id" && key != "expected_workspace_instance_id" {
            return Err(DaemonRpcError::new(
                E_TASK_PROMPT_TASK_ID_REQUIRED,
                format!("request 含未知字段 {key:?}，已 fail closed（spec §4.1-3）"),
            ));
        }
    }
    let task_id = obj
        .get("task_id")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            DaemonRpcError::new(
                E_TASK_PROMPT_TASK_ID_REQUIRED,
                "task_id 必填且为非空字符串",
            )
        })?
        .to_string();
    let expected_workspace_instance_id = match obj.get("expected_workspace_instance_id") {
        None | Some(Value::Null) => None,
        Some(Value::String(s)) => {
            let s = s.trim();
            if s.is_empty() {
                return Err(DaemonRpcError::new(
                    E_TASK_PROMPT_TASK_ID_REQUIRED,
                    "expected_workspace_instance_id 为空字符串时必须省略",
                ));
            }
            Some(s.to_string())
        }
        Some(_) => {
            return Err(DaemonRpcError::new(
                E_TASK_PROMPT_TASK_ID_REQUIRED,
                "expected_workspace_instance_id 必须是字符串",
            ))
        }
    };
    Ok(CompileRequest {
        task_id,
        expected_workspace_instance_id,
    })
}

/// §11.3：capability enabled 状态。
///
/// enabled = build-generated manifest 存在（RP-02 起 build gate fail-closed
/// 保证）且 runtime registry 完整性校验通过（§11.1-10，RP-04）。
pub fn capability_enabled() -> bool {
    ROLE_PROMPTS_MANIFEST_PRESENT && verify_registry_integrity().is_ok()
}

/// §11.3：health/capability 投影。hash 来自 build-generated constants
/// （不重解析 runtime manifest），规范化为 lowercase `sha256:<64-hex>`。
pub fn capability_projection() -> Value {
    let manifest_hash = if ROLE_PROMPTS_MANIFEST_PRESENT {
        normalize_sha256_form(ROLE_PROMPTS_MANIFEST_SHA256).unwrap_or_default()
    } else {
        String::new()
    };
    let policy_hash = ROLE_PROMPTS_COMPILER_POLICY_SHA256
        .as_deref()
        .and_then(normalize_sha256_form)
        .unwrap_or_default();
    json!({
        "name": CAPABILITY_NAME,
        "schema_version": CAPABILITY_SCHEMA_VERSION,
        "manifest_sha256": manifest_hash,
        "compiler_policy_sha256": policy_hash,
        "enabled": capability_enabled(),
    })
}

/// 从 task 不可变 binding 自解析真实 workspace instance（§4.1-5：客户端
/// 不得选择 workspace）。解析链：task 存在性 → binding 行 →
/// `workspace_authority_captures.workspace_instance_id`；任一环缺失 →
/// fail-closed（task 不存在 → `E_TASK_PROMPT_TASK_NOT_FOUND`，binding/
/// capture 不可解析 → `E_TASK_PROMPT_AUTHORITY_UNAVAILABLE`）。
fn resolve_workspace_instance(conn: &Connection, task_id: &str) -> Result<String, DaemonRpcError> {
    // 0. task 存在性（§10.2 错误码顺序：NOT_FOUND 先于 AUTHORITY_UNAVAILABLE）。
    let task_exists: bool = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE id = ?1)",
            [task_id],
            |row| row.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("tasks 存在性读取失败: {e}")))?;
    if !task_exists {
        return Err(DaemonRpcError::new(
            crate::daemon::task_prompt::context::E_TASK_PROMPT_TASK_NOT_FOUND,
            format!("task {task_id} 在当前 authority 中不存在"),
        ));
    }
    // 1. binding 行 → capture id。
    let capture_id: String = conn
        .query_row(
            "SELECT workspace_capture_id FROM task_workspace_bindings WHERE task_id = ?1",
            [task_id],
            |row| row.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace binding 读取失败: {e}")))?
        .ok_or_else(|| {
            DaemonRpcError::new(
                crate::daemon::task_prompt::context::E_TASK_PROMPT_AUTHORITY_UNAVAILABLE,
                format!("task {task_id} 没有可解析的不可变 workspace binding"),
            )
        })?;
    // 2. capture 行 → 权威 workspace_instance_id。
    conn.query_row(
        "SELECT workspace_instance_id FROM workspace_authority_captures \
         WHERE workspace_capture_id = ?1",
        [&capture_id],
        |row| row.get(0),
    )
    .optional()
    .map_err(|e| DaemonRpcError::internal_error(format!("workspace capture 读取失败: {e}")))?
    .ok_or_else(|| {
        DaemonRpcError::new(
            crate::daemon::task_prompt::context::E_TASK_PROMPT_AUTHORITY_UNAVAILABLE,
            format!("task {task_id} 的 workspace capture 无法解析"),
        )
    })
}

/// §4.2：optional caller guard 只做过期检测，不进入任何 hash。
fn check_workspace_guard(
    expected: Option<&str>,
    resolved: &str,
    task_id: &str,
) -> Result<(), DaemonRpcError> {
    if let Some(expected) = expected {
        if expected != resolved {
            return Err(DaemonRpcError::new(
                E_TASK_PROMPT_AUTHORITY_MISMATCH,
                format!(
                    "expected_workspace_instance_id 与 daemon 权威 binding 不一致 \
                    （task {task_id}），请刷新 authority 后发起新请求（retry: \
                     after_authority_refresh）"
                ),
            ));
        }
    }
    Ok(())
}

/// §8.1-3：不可信数据面。v1 只包含 daemon 读到的 task title/description，
/// 经 render.rs 的 8KiB scalar-safe 裁剪进入第 3 段 canonical JSON 块。
fn read_untrusted_data_plane(conn: &Connection, task_id: &str) -> Result<Value, DaemonRpcError> {
    let row: Option<(String, String)> = conn
        .query_row(
            "SELECT title, description FROM tasks WHERE id = ?1",
            [task_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("task 元数据读取失败: {e}")))?;
    let (title, description) = row.unwrap_or_default();
    Ok(json!({
        "task_title": title,
        "task_description": description,
    }))
}

/// display-only RFC3339 UTC 时间戳（§9.7：generated_at 不进入任何 hash）。
/// 无 chrono/time 依赖，用 civil-from-days（Howard Hinnant 算法）换算。
fn rfc3339_now() -> String {
    let secs = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let (h, m, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    // civil_from_days
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = if month <= 2 { y + 1 } else { y };
    format!("{year:04}-{month:02}-{d:02}T{h:02}:{m:02}:{s:02}Z")
}

/// `task.prompt.compile` 域 handler（spec §4.1）。
///
/// `conn` 由 dispatch 薄层经 `with_conn` 提供的只读 closure 传入——
/// 全部读操作在同一 connection/同一 snapshot 上完成；本函数零写入。
pub fn compile(conn: &Connection, params: &Value) -> Result<Value, DaemonRpcError> {
    let req = parse_request(params)?;

    // §11.3：capability 未启用 → public RPC fail closed（不合成 bundle）。
    if !capability_enabled() {
        return Err(DaemonRpcError::new(
            E_TASK_PROMPT_TEMPLATE_INVALID,
            format!(
                "capability {CAPABILITY_NAME} 未启用（generated manifest 缺失或 \
                 registry 完整性校验失败），public RPC fail closed"
            ),
        ));
    }

    // 1. workspace 自解析 + §4.2 guard（guard 值不进入任何 hash）。
    let resolved = resolve_workspace_instance(conn, &req.task_id)?;
    check_workspace_guard(req.expected_workspace_instance_id.as_deref(), &resolved, &req.task_id)?;

    // 2. RP-03 单 snapshot authority context（同一 connection）。
    let context = aggregate_authority_context(conn, &resolved, &req.task_id)?;

    // 3. RP-04 §6/§7.1 路由与精确模板选择（fail-closed，无 fallback）。
    let selection = route_and_select(&context)?;

    // 4. §8.1-3 不可信数据面（仅 title/description，进第 3 段）。
    let untrusted = read_untrusted_data_plane(conn, &req.task_id)?;

    // 5. RP-04 render + §5.1 bundle 组装（含全部预算/secret/hash 闭集）。
    let compiled = compile_bundle(&context, &selection, &untrusted, &rfc3339_now())?;
    Ok(compiled.bundle)
}
