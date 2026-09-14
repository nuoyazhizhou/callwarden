//! Role Prompt Compiler v1 — bundle 组装（§5.1 schema）与 bundle_hash。
//!
//! 冻结 authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md
//! §5 / §9.3 / §9.6 / §9.7。
//!
//! 职责（RP-04 卡冻结 scope，仅此而已）：
//! - 以 RP-03 authority context + route 选择 + renderer 输出组装
//!   `role_prompt_bundle_v1` 完整对象；
//! - §9.6 `bundle_hash` 只对最小闭集 canonical object 计算；
//! - §9.7 排除项（`generated_at`、request_id、display 字段等）不进入任何
//!   hash；`prompt.sha256` 仍按最终完整 `prompt.text` 字节计算；
//! - §9.3 canonical bundle JSON ≤256KiB（超限失败）。
//!
//! `generated_at` 由调用方注入（display-only RFC3339），使同一 snapshot 的
//! 组合输出字节级确定（§11.2 确定性测试的前提）；不参与任何 hash。
//!
//! 明确不做：RPC 暴露 / capability 发布（RP-05）、CLI/MCP/Skill（RP-06/09）。

use serde_json::{json, Value};

use crate::daemon::dispatch::DaemonRpcError;
use crate::daemon::task_prompt::canonical::{
    canonical_json, normalize_sha256_form, sha256_prefixed, E_TASK_PROMPT_TEMPLATE_INVALID,
};
use crate::daemon::task_prompt::render::{
    build_context_hash_input, render_prompt, BUNDLE_JSON_BUDGET, E_TASK_PROMPT_BUDGET_EXCEEDED,
    RenderOutput,
};
use crate::daemon::task_prompt::route::{
    RouteSelection, COMPILER_POLICY_ID, ROLE_PROMPTS_COMPILER_POLICY_SHA256,
    ROLE_PROMPTS_MANIFEST_PRESENT, ROLE_PROMPTS_MANIFEST_SHA256,
};

fn err(code: &str, message: String) -> DaemonRpcError {
    DaemonRpcError::new(code, message)
}

/// bundle 组装产物（供 RP-05 与测试复用）。
#[derive(Debug, Clone)]
pub struct CompiledBundle {
    pub bundle: Value,
    pub bundle_json: Vec<u8>,
    pub render: RenderOutput,
}

fn compiler_policy_hash_display() -> String {
    ROLE_PROMPTS_COMPILER_POLICY_SHA256
        .as_deref()
        .and_then(normalize_sha256_form)
        .unwrap_or_default()
}

fn manifest_hash_display() -> String {
    if ROLE_PROMPTS_MANIFEST_PRESENT {
        normalize_sha256_form(ROLE_PROMPTS_MANIFEST_SHA256).unwrap_or_default()
    } else {
        String::new()
    }
}

/// 组装 `role_prompt_bundle_v1`（§5.1）。
///
/// - `context`：RP-03 authority context（bundle 前体）；
/// - `selection`：route 状态机选择；
/// - `untrusted`：不可信动态内容（v1 数据面为空对象时同样按 §8.2 输出空
///   canonical 区块，保持机制可见与确定性）；
/// - `generated_at`：display-only RFC3339 时间戳，不参与任何 hash。
pub fn compile_bundle(
    context: &Value,
    selection: &RouteSelection,
    untrusted: &Value,
    generated_at: &str,
) -> Result<CompiledBundle, DaemonRpcError> {
    let render = render_prompt(context, selection, untrusted)?;

    // prompt.sha256 按最终完整 prompt.text 字节计算（§9.7）。
    let prompt_sha256 = sha256_prefixed(render.prompt_text.as_bytes());

    // context_hash：§9.5 唯一 include 集合。
    let context_hash_input = build_context_hash_input(context, selection, &render.omissions)?;
    let context_hash_bytes =
        canonical_json(&context_hash_input).map_err(|m| err(E_TASK_PROMPT_TEMPLATE_INVALID, m))?;
    let context_hash = sha256_prefixed(&context_hash_bytes);

    // bundle_hash：§9.6 最小闭集。
    let bundle_hash_input = json!({
        "schema_version": "role_prompt_bundle_v1",
        "task_id": context.get("task_id").cloned().unwrap_or(Value::Null),
        "prompt_kind": selection.prompt_kind,
        "context_hash": context_hash,
        "prompt": { "sha256": prompt_sha256 },
    });
    let bundle_hash_bytes =
        canonical_json(&bundle_hash_input).map_err(|m| err(E_TASK_PROMPT_TEMPLATE_INVALID, m))?;
    let bundle_hash = sha256_prefixed(&bundle_hash_bytes);

    let template_body_hash = sha256_prefixed(selection.template.body.as_bytes());
    let omissions: Vec<Value> = render.omissions.iter().map(|o| o.to_value()).collect();
    let template_source = if selection.prompt_kind == "role_work" {
        "role_contract"
    } else {
        "system"
    };

    let bundle = json!({
        "schema_version": "role_prompt_bundle_v1",
        "task_id": context.get("task_id").cloned().unwrap_or(Value::Null),
        "prompt_kind": selection.prompt_kind,
        "authority": context.get("authority").cloned().unwrap_or(Value::Null),
        "routing": context.get("routing").cloned().unwrap_or(Value::Null),
        "contract": context.get("contract").cloned().unwrap_or(Value::Null),
        "template": {
            "source": template_source,
            "body_template_id": selection.template.template_id,
            "body_template_hash": template_body_hash,
            "compiler_policy_id": COMPILER_POLICY_ID,
            "compiler_policy_hash": compiler_policy_hash_display(),
            "manifest_hash": manifest_hash_display(),
        },
        "authorization": {
            "routing_state": context.get("authorization")
                .and_then(|a| a.get("routing_state")).cloned().unwrap_or(Value::Null),
            "valid_for_claim": false,
            "mutation_recheck_required": true,
        },
        "omissions": omissions,
        "context_hash": context_hash,
        "prompt": {
            "text": render.prompt_text,
            "sha256": prompt_sha256,
        },
        "bundle_hash": bundle_hash,
        "generated_at": generated_at,
    });

    let bundle_json =
        canonical_json(&bundle).map_err(|m| err(E_TASK_PROMPT_TEMPLATE_INVALID, m))?;
    if bundle_json.len() > BUNDLE_JSON_BUDGET {
        return Err(err(
            E_TASK_PROMPT_BUDGET_EXCEEDED,
            format!(
                "canonical bundle JSON {} bytes exceeds 256KiB budget",
                bundle_json.len()
            ),
        ));
    }

    Ok(CompiledBundle { bundle, bundle_json, render })
}

