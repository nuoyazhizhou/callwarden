//! Role Prompt Compiler v1 — 路由状态机与模板选择。
//!
//! 冻结 authority: docs/design/cw-role-prompt-compiler-v1-frozen-spec.md
//! §5.1 / §6 / §7.1-7.4 / §11.1-10。
//!
//! 职责（RP-04 卡冻结 scope，仅此而已）：
//! - §6 路由状态机：next-action decision/action → prompt kind 与模板
//!   authority；`READY/PLAN` 与未知 route 在 v1 一律 hard error
//!   `E_TASK_PROMPT_UNSUPPORTED_ACTION`（不合成 Planner 派工）；
//! - §7.1 Role Contract 精确 ID+hash 模板选择（规范化 wire form 比较，
//!   禁止 fallback 到默认模板或用 current 冒充 legacy hash）；
//! - §7.4/§11.1-10：模板正文以编译期 `include_str!` 进入 runtime registry
//!   （不做 runtime 资源重解析），每次 compile 用 build-generated
//!   constants 做完整性校验（`E_TASK_PROMPT_TEMPLATE_INVALID` fail-closed）。
//!
//! 明确不做：组合/裁剪/预算（render.rs）、bundle 组装（bundle.rs）、
//! RPC dispatch（RP-05）。

use serde_json::Value;

use crate::daemon::dispatch::DaemonRpcError;
use crate::daemon::task_prompt::canonical::{normalize_sha256_form, sha256_hex};
use crate::daemon::task_prompt::redaction::{
    ensure_clean, E_TASK_PROMPT_SECRET_DETECTED,
};

// Build-generated constants（§7.4：runtime 只消费 generated constants）。
include!(concat!(env!("OUT_DIR"), "/role_prompts_generated.rs"));

/// 编译期嵌入的 compiler policy 正文（byte-exact，spec §8.1 段 1）。
pub const COMPILER_POLICY_BODY: &str =
    include_str!("../../../resources/role_prompts/v1/compiler_policy.md");

/// bundle `template.compiler_policy_id`（spec §5.1 schema 字面值）。
/// RP-06 已对齐：资产正文首行模板标识与 `COMPILER_POLICY_ID` 同为冻结
/// §5.1 字面值，资产哈希链 spec → asset → manifest → 常量一致。
pub const COMPILER_POLICY_ID: &str = "cw.role_prompt.compiler_policy.v1";

/// 稳定错误码（spec §10.2）。
pub const E_TASK_PROMPT_CONTRACT_UNRESOLVED: &str = "E_TASK_PROMPT_CONTRACT_UNRESOLVED";
pub const E_TASK_PROMPT_ROLE_NOT_ELIGIBLE: &str = "E_TASK_PROMPT_ROLE_NOT_ELIGIBLE";
pub const E_TASK_PROMPT_TEMPLATE_ID_REQUIRED: &str = "E_TASK_PROMPT_TEMPLATE_ID_REQUIRED";
pub const E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED: &str = "E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED";
pub const E_TASK_PROMPT_TEMPLATE_NOT_FOUND: &str = "E_TASK_PROMPT_TEMPLATE_NOT_FOUND";
pub const E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH: &str = "E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH";
pub const E_TASK_PROMPT_TEMPLATE_ROUTE_MISMATCH: &str = "E_TASK_PROMPT_TEMPLATE_ROUTE_MISMATCH";
pub const E_TASK_PROMPT_UNSUPPORTED_ACTION: &str = "E_TASK_PROMPT_UNSUPPORTED_ACTION";
pub use crate::daemon::task_prompt::canonical::E_TASK_PROMPT_TEMPLATE_INVALID;

/// manifest 模板类别（§7.3：kind=role | kind=system）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TemplateKind {
    Role(&'static str),
    System,
}

/// runtime registry 条目：编译期字节精确模板。
#[derive(Debug, Clone, Copy)]
pub struct TemplateEntry {
    pub template_id: &'static str,
    pub path: &'static str,
    pub body: &'static str,
    pub kind: TemplateKind,
    /// manifest routes（`READY/CLAIM` 形态；legacy/contract-pinned 为空）。
    pub routes: &'static [&'static str],
}

/// 编译期模板 registry（§7.3 生产资源全集，10 模板）。
pub static TEMPLATE_REGISTRY: &[TemplateEntry] = &[
    TemplateEntry {
        template_id: "cw.aprime.executor.startup.v1",
        path: "legacy/executor_planner_startup_v1.md",
        body: include_str!("../../../resources/role_prompts/v1/legacy/executor_planner_startup_v1.md"),
        kind: TemplateKind::Role("executor"),
        routes: &[],
    },
    TemplateEntry {
        template_id: "cw.aprime.reviewer.startup.v1",
        path: "legacy/reviewer_startup_v1.md",
        body: include_str!("../../../resources/role_prompts/v1/legacy/reviewer_startup_v1.md"),
        kind: TemplateKind::Role("reviewer"),
        routes: &[],
    },
    TemplateEntry {
        template_id: "cw.aprime.adjudicator.startup.v1",
        path: "legacy/adjudicator_startup_v1.md",
        body: include_str!("../../../resources/role_prompts/v1/legacy/adjudicator_startup_v1.md"),
        kind: TemplateKind::Role("adjudicator"),
        routes: &[],
    },
    TemplateEntry {
        template_id: "cw.aprime.executor.startup.v4",
        path: "current/executor_v4.md",
        body: include_str!("../../../resources/role_prompts/v1/current/executor_v4.md"),
        kind: TemplateKind::Role("executor"),
        routes: &["READY/CLAIM", "READY/REVISE"],
    },
    TemplateEntry {
        template_id: "cw.aprime.reviewer.startup.v4",
        path: "current/reviewer_v4.md",
        body: include_str!("../../../resources/role_prompts/v1/current/reviewer_v4.md"),
        kind: TemplateKind::Role("reviewer"),
        routes: &["READY/REVIEW"],
    },
    TemplateEntry {
        template_id: "cw.aprime.adjudicator.startup.v4",
        path: "current/adjudicator_v4.md",
        body: include_str!("../../../resources/role_prompts/v1/current/adjudicator_v4.md"),
        kind: TemplateKind::Role("adjudicator"),
        routes: &["READY/ADJUDICATE"],
    },
    TemplateEntry {
        template_id: "cw.aprime.planner.startup.v1",
        path: "current/planner_v1.md",
        body: include_str!("../../../resources/role_prompts/v1/current/planner_v1.md"),
        kind: TemplateKind::Role("planner"),
        routes: &[],
    },
    TemplateEntry {
        template_id: "cw.system.blocked_recovery.v1",
        path: "system/blocked_recovery.md",
        body: include_str!("../../../resources/role_prompts/v1/system/blocked_recovery.md"),
        kind: TemplateKind::System,
        routes: &[],
    },
    TemplateEntry {
        template_id: "cw.system.waiting.v1",
        path: "system/waiting.md",
        body: include_str!("../../../resources/role_prompts/v1/system/waiting.md"),
        kind: TemplateKind::System,
        routes: &[],
    },
    TemplateEntry {
        template_id: "cw.system.terminal.v1",
        path: "system/terminal.md",
        body: include_str!("../../../resources/role_prompts/v1/system/terminal.md"),
        kind: TemplateKind::System,
        routes: &[],
    },
];

fn err(code: &str, message: String) -> DaemonRpcError {
    DaemonRpcError::new(code, message)
}

/// 读取 context 对象中的字符串字段；非字符串返回 None。
fn context_str<'a>(context: &'a Value, obj: &str, key: &str) -> Option<&'a str> {
    context.get(obj)?.get(key)?.as_str()
}

/// registry 完整性校验（§11.1-10：runtime generated constants 与 build
/// output 一致）。每次 compile 前调用；任一不一致 fail-closed
/// `E_TASK_PROMPT_TEMPLATE_INVALID`。
pub fn verify_registry_integrity() -> Result<(), DaemonRpcError> {
    if !ROLE_PROMPTS_MANIFEST_PRESENT {
        return Err(err(
            E_TASK_PROMPT_TEMPLATE_INVALID,
            "build gate reports no production role prompt manifest".to_string(),
        ));
    }
    let policy_sha = ROLE_PROMPTS_COMPILER_POLICY_SHA256
        .map(normalize_sha256_form)
        .flatten()
        .ok_or_else(|| {
            err(
                E_TASK_PROMPT_TEMPLATE_INVALID,
                "generated compiler policy hash missing or malformed".to_string(),
            )
        })?;
    if policy_sha != normalize_sha256_form(&sha256_hex(COMPILER_POLICY_BODY.as_bytes())).unwrap() {
        return Err(err(
            E_TASK_PROMPT_TEMPLATE_INVALID,
            "embedded compiler policy body does not match build-generated hash".to_string(),
        ));
    }
    for entry in TEMPLATE_REGISTRY {
        let idx = ROLE_PROMPTS_TEMPLATE_IDS
            .iter()
            .position(|id| *id == entry.template_id)
            .ok_or_else(|| {
                err(
                    E_TASK_PROMPT_TEMPLATE_INVALID,
                    format!("registry template {} missing from generated manifest", entry.template_id),
                )
            })?;
        let generated_path = ROLE_PROMPTS_TEMPLATE_PATHS[idx];
        if generated_path != entry.path {
            return Err(err(
                E_TASK_PROMPT_TEMPLATE_INVALID,
                format!(
                    "registry path drift for {}: registry={} generated={}",
                    entry.template_id, entry.path, generated_path
                ),
            ));
        }
        let generated_sha = normalize_sha256_form(ROLE_PROMPTS_TEMPLATE_SHA256[idx]).ok_or_else(
            || {
                err(
                    E_TASK_PROMPT_TEMPLATE_INVALID,
                    format!("generated hash malformed for {}", entry.template_id),
                )
            },
        )?;
        let embedded_sha = normalize_sha256_form(&sha256_hex(entry.body.as_bytes())).unwrap();
        if generated_sha != embedded_sha {
            return Err(err(
                E_TASK_PROMPT_TEMPLATE_INVALID,
                format!("embedded body hash drift for {}", entry.template_id),
            ));
        }
    }
    Ok(())
}

/// 路由选择结果（§6 状态机一行）。
#[derive(Debug, Clone, Copy)]
pub struct RouteSelection {
    /// `role_work|blocked_recovery|waiting|terminal`（§5.1 prompt_kind）。
    pub prompt_kind: &'static str,
    /// 选定模板（role_work 必为 contract-pinned role 模板；system kinds
    /// 必为对应 system 模板）。
    pub template: &'static TemplateEntry,
}

/// §6 路由状态机 + §7.1 模板选择。
///
/// 输入是 RP-03 的 authority context（§5.1 bundle 前体）。单一职责：只
/// 决策与选择，不渲染、不计算 hash。
pub fn route_and_select(context: &Value) -> Result<RouteSelection, DaemonRpcError> {
    verify_registry_integrity()?;

    let decision = context_str(context, "routing", "decision").unwrap_or_default();
    let action = context_str(context, "routing", "action").unwrap_or_default();

    // System kinds：不需要 Role Contract（§6：模板 authority = daemon system
    // manifest；§5.2 body ID/hash 来自 system manifest）。
    let system_kind = match decision {
        "BLOCKED" => Some(("blocked_recovery", "cw.system.blocked_recovery.v1")),
        "WAITING" => Some(("waiting", "cw.system.waiting.v1")),
        "COMPLETE" => Some(("terminal", "cw.system.terminal.v1")),
        _ => None,
    };
    if let Some((kind, template_id)) = system_kind {
        let template = TEMPLATE_REGISTRY
            .iter()
            .find(|t| t.template_id == template_id)
            .expect("system template present in registry");
        return Ok(RouteSelection { prompt_kind: kind, template });
    }

    if decision != "READY" {
        return Err(err(
            E_TASK_PROMPT_UNSUPPORTED_ACTION,
            format!("unknown decision {decision:?} has no v1 route"),
        ));
    }

    // READY/PLAN：v1 不选择模板（planner_governance_v1 未声明）。
    if action == "PLAN" {
        return Err(err(
            E_TASK_PROMPT_UNSUPPORTED_ACTION,
            "READY/PLAN is not a v1 route; reserved for planner_governance_v1".to_string(),
        ));
    }

    let required_role = match action {
        "CLAIM" | "REVISE" => "executor",
        "REVIEW" => "reviewer",
        "ADJUDICATE" => "adjudicator",
        _ => {
            return Err(err(
                E_TASK_PROMPT_UNSUPPORTED_ACTION,
                format!("unknown action {action:?} under READY has no v1 route"),
            ))
        }
    };

    // required_role 必须与状态机映射一致（authority 没有可编译的当前角色 →
    // E_TASK_PROMPT_ROLE_NOT_ELIGIBLE）。
    let context_role = context_str(context, "routing", "required_role");
    match context_role {
        Some(role) if role == required_role => {}
        _ => {
            return Err(err(
                E_TASK_PROMPT_ROLE_NOT_ELIGIBLE,
                format!(
                    "routing.required_role {:?} cannot compile a {} work bundle",
                    context_role.unwrap_or("<null>"),
                    required_role
                ),
            ))
        }
    }

    // §7.1-1/2：Role Contract 缺模板 ID / 缺合法 prompt hash。
    let template_id = context_str(context, "contract", "role_contract_prompt_template_id")
        .map(str::trim)
        .filter(|s| !s.is_empty())
        .ok_or_else(|| {
            err(
                E_TASK_PROMPT_TEMPLATE_ID_REQUIRED,
                "current Role Contract has no prompt_template_id".to_string(),
            )
        })?;
    let raw_hash = context_str(context, "contract", "role_contract_prompt_hash").ok_or_else(|| {
        err(
            E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED,
            "current Role Contract has no prompt_hash".to_string(),
        )
    })?;
    let contract_hash = normalize_sha256_form(raw_hash).ok_or_else(|| {
        err(
            E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED,
            "current Role Contract prompt_hash is not a legal SHA-256 wire form".to_string(),
        )
    })?;

    // §7.1-4：manifest 无 exact ID。
    let template = TEMPLATE_REGISTRY
        .iter()
        .find(|t| t.template_id == template_id)
        .ok_or_else(|| {
            err(
                E_TASK_PROMPT_TEMPLATE_NOT_FOUND,
                format!("production manifest has no template {template_id:?}"),
            )
        })?;

    // §7.1-5：byte content hash 不一致。
    let template_hash =
        normalize_sha256_form(&sha256_hex(template.body.as_bytes())).expect("valid sha256");
    if template_hash != contract_hash {
        return Err(err(
            E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH,
            format!("manifest bytes hash does not match contract hash for {template_id:?}"),
        ));
    }

    // §7.1-6：manifest 的 role 与 next-action 不匹配。
    match template.kind {
        TemplateKind::Role(role) if role == required_role => {}
        _ => {
            return Err(err(
                E_TASK_PROMPT_TEMPLATE_ROUTE_MISMATCH,
                format!("template {template_id:?} role does not match next-action role"),
            ))
        }
    }
    // manifest routes 非空时必须包含当前 decision/action 对；为空表示
    // legacy/contract-pinned（选择完全由 Role Contract 精确 ID+hash 决定）。
    let route_pair = format!("{decision}/{action}");
    if !template.routes.is_empty() && !template.routes.contains(&route_pair.as_str()) {
        return Err(err(
            E_TASK_PROMPT_TEMPLATE_ROUTE_MISMATCH,
            format!("template {template_id:?} does not route {route_pair}"),
        ));
    }

    Ok(RouteSelection { prompt_kind: "role_work", template })
}

/// 编译前输入 secret 扫描（§8.3：compile 前执行）。模板正文与 authority
/// context 都在扫描面内。
pub fn pre_compile_secret_scan(context: &Value, template: &TemplateEntry) -> Result<(), DaemonRpcError> {
    ensure_clean(template.body).map_err(|m| err(E_TASK_PROMPT_SECRET_DETECTED, m))?;
    let context_text = serde_json::to_string(context)
        .map_err(|e| err(E_TASK_PROMPT_TEMPLATE_INVALID, format!("context serialization failed: {e}")))?;
    ensure_clean(&context_text).map_err(|m| err(E_TASK_PROMPT_SECRET_DETECTED, m))
}

