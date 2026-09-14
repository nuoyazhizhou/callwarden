//! route.rs 单元测试（§6 路由状态机 / §7.1 精确选择 / §11.1-10 registry
//! integrity；测试名携带 `task_prompt_renderer` 验收 filter token）。

use serde_json::{json, Value};

use crate::daemon::task_prompt::route::{
    route_and_select, verify_registry_integrity, TemplateKind, E_TASK_PROMPT_UNSUPPORTED_ACTION,
};
use crate::daemon::task_prompt::render_tests::{
    err_code, fixture_context, ADJ_V1_HASH, EXEC_V1_HASH, EXEC_V4_HASH, REV_V1_HASH,
};

fn route_ok(context: &Value) -> (String, String, String) {
    let selection = route_and_select(context).expect("route ok");
    (
        selection.prompt_kind.to_string(),
        selection.template.template_id.to_string(),
        format!("{:?}", selection.template.kind),
    )
}

fn route_err(context: &Value) -> String {
    let e = route_and_select(context).expect_err("route must fail");
    err_code(&e)
}

// ------------------------------------------------------- registry integrity

#[test]
fn task_prompt_renderer_registry_integrity_matches_generated_constants() {
    // §11.1-10：runtime 嵌入正文与 build-generated hash 一致，否则 fail-closed。
    verify_registry_integrity().expect("registry integrity must pass");
}

#[test]
fn task_prompt_renderer_registry_covers_all_ten_templates() {
    use crate::daemon::task_prompt::route::TEMPLATE_REGISTRY;
    assert_eq!(TEMPLATE_REGISTRY.len(), 10);
    let system: Vec<_> = TEMPLATE_REGISTRY
        .iter()
        .filter(|t| matches!(t.kind, TemplateKind::System))
        .collect();
    assert_eq!(system.len(), 3);
    // system 模板不产生 action-ready route（§11.1-7）。
    for t in &system {
        assert!(t.routes.is_empty(), "system template {} must not route", t.template_id);
    }
}

// ------------------------------------------------------------ state machine

#[test]
fn task_prompt_renderer_route_maps_ready_claim_to_executor_contract() {
    let context = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)));
    assert_eq!(
        route_ok(&context),
        (
            "role_work".to_string(),
            "cw.aprime.executor.startup.v1".to_string(),
            format!("{:?}", TemplateKind::Role("executor"))
        )
    );
}

#[test]
fn task_prompt_renderer_route_maps_ready_revise_review_adjudicate() {
    let revise = fixture_context("READY", "REVISE", Some("executor"), Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)));
    assert_eq!(route_ok(&revise).0, "role_work");
    let review = fixture_context("READY", "REVIEW", Some("reviewer"), Some(("cw.aprime.reviewer.startup.v1", REV_V1_HASH)));
    assert_eq!(
        route_ok(&review),
        (
            "role_work".to_string(),
            "cw.aprime.reviewer.startup.v1".to_string(),
            format!("{:?}", TemplateKind::Role("reviewer"))
        )
    );
    let adjudicate = fixture_context("READY", "ADJUDICATE", Some("adjudicator"), Some(("cw.aprime.adjudicator.startup.v1", ADJ_V1_HASH)));
    assert_eq!(route_ok(&adjudicate).1, "cw.aprime.adjudicator.startup.v1");
}

#[test]
fn task_prompt_renderer_route_selects_current_v4_contract_template() {
    // current 模板带 action route：READY/CLAIM 命中 executor v4 routes。
    let context = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.v4", EXEC_V4_HASH)));
    assert_eq!(route_ok(&context).1, "cw.aprime.executor.startup.v4");
}

#[test]
fn task_prompt_renderer_route_maps_system_kinds() {
    for (decision, action, kind, template_id) in [
        ("BLOCKED", "RESOLVE", "blocked_recovery", "cw.system.blocked_recovery.v1"),
        ("WAITING", "WAIT", "waiting", "cw.system.waiting.v1"),
        ("COMPLETE", "NONE", "terminal", "cw.system.terminal.v1"),
    ] {
        let context = fixture_context(decision, action, None, None);
        assert_eq!(
            route_ok(&context),
            (kind.to_string(), template_id.to_string(), format!("{:?}", TemplateKind::System)),
            "decision {decision} must map to {kind}"
        );
    }
}

#[test]
fn task_prompt_renderer_route_hard_errors_on_ready_plan_and_unknown_routes() {
    // READY/PLAN：v1 不合成 Planner 派工（planner_governance_v1 未声明）。
    let plan = fixture_context("READY", "PLAN", Some("planner"), None);
    assert_eq!(route_err(&plan), E_TASK_PROMPT_UNSUPPORTED_ACTION);
    // 未知 decision / 未知 action。
    let unknown_decision = fixture_context("PENDING", "CLAIM", Some("executor"), None);
    assert_eq!(route_err(&unknown_decision), E_TASK_PROMPT_UNSUPPORTED_ACTION);
    let unknown_action = fixture_context("READY", "DELEGATE", Some("executor"), None);
    assert_eq!(route_err(&unknown_action), E_TASK_PROMPT_UNSUPPORTED_ACTION);
}

// --------------------------------------------------------- §7.1 selection

#[test]
fn task_prompt_renderer_selection_requires_contract_template_id_and_hash() {
    let missing_id = fixture_context("READY", "CLAIM", Some("executor"), None);
    assert_eq!(route_err(&missing_id), "E_TASK_PROMPT_TEMPLATE_ID_REQUIRED");
    // prompt_hash 缺失：template_id 有、hash 为 null。
    let mut missing_hash = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)));
    missing_hash["contract"]["role_contract_prompt_hash"] = Value::Null;
    assert_eq!(route_err(&missing_hash), "E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED");
    // 非法 wire form。
    let mut bad_hash = missing_hash;
    bad_hash["contract"]["role_contract_prompt_hash"] = json!("md5:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa");
    assert_eq!(route_err(&bad_hash), "E_TASK_PROMPT_TEMPLATE_HASH_REQUIRED");
}

#[test]
fn task_prompt_renderer_selection_fails_closed_on_unknown_id_and_hash_mismatch() {
    let unknown = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.nonexistent", EXEC_V1_HASH)));
    assert_eq!(route_err(&unknown), "E_TASK_PROMPT_TEMPLATE_NOT_FOUND");
    let mismatch = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.v1", REV_V1_HASH)));
    assert_eq!(route_err(&mismatch), "E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH");
    // 禁止用 current 冒充 legacy hash（§7.1-7）。
    let impersonate = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.v1", EXEC_V4_HASH)));
    assert_eq!(route_err(&impersonate), "E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH");
}

#[test]
fn task_prompt_renderer_selection_accepts_uppercase_and_bare_hash_wire_forms() {
    // §7.1-3：64-hex 与 sha256: 形式、任意大小写，比较前规范化。
    let upper_hex_prefixed = fixture_context(
        "READY",
        "CLAIM",
        Some("executor"),
        Some((
            "cw.aprime.executor.startup.v1",
            &format!("sha256:{}", &EXEC_V1_HASH[7..].to_ascii_uppercase()),
        )),
    );
    assert_eq!(route_ok(&upper_hex_prefixed).1, "cw.aprime.executor.startup.v1");
    let bare_upper = fixture_context(
        "READY",
        "CLAIM",
        Some("executor"),
        Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH.trim_start_matches("sha256:"))),
    );
    assert_eq!(route_ok(&bare_upper).1, "cw.aprime.executor.startup.v1");
}

#[test]
fn task_prompt_renderer_selection_rejects_role_and_route_mismatch() {
    // manifest role 与 next-action 不匹配（§7.1-6）。
    let role_mismatch = fixture_context("READY", "REVIEW", Some("reviewer"), Some(("cw.aprime.executor.startup.v4", EXEC_V4_HASH)));
    assert_eq!(route_err(&role_mismatch), "E_TASK_PROMPT_TEMPLATE_ROUTE_MISMATCH");
    // current 模板 route 对不匹配：executor v4 不路由 READY/REVIEW。
    let route_mismatch = fixture_context("READY", "REVIEW", Some("reviewer"), Some(("cw.aprime.reviewer.startup.v4", "sha256:0000000000000000000000000000000000000000000000000000000000000000")));
    let err = route_err(&route_mismatch);
    assert!(
        err == "E_TASK_PROMPT_TEMPLATE_HASH_MISMATCH" || err == "E_TASK_PROMPT_TEMPLATE_ROUTE_MISMATCH",
        "reviewer v4 on REVIEW must hash-check first; got {err}"
    );
}

#[test]
fn task_prompt_renderer_route_rejects_required_role_inconsistency() {
    // required_role 与状态机映射不一致 → ROLE_NOT_ELIGIBLE。
    let inconsistent = fixture_context("READY", "REVIEW", Some("executor"), Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)));
    assert_eq!(route_err(&inconsistent), "E_TASK_PROMPT_ROLE_NOT_ELIGIBLE");
    let null_role = fixture_context("READY", "CLAIM", None, Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)));
    assert_eq!(route_err(&null_role), "E_TASK_PROMPT_ROLE_NOT_ELIGIBLE");
}

#[test]
fn task_prompt_renderer_route_never_synthesizes_fallback_template() {
    // 合同缺 prompt 信息时绝不 fallback 到默认 Executor 模板（§7.1-7）。
    let mut context = fixture_context("READY", "CLAIM", Some("executor"), None);
    context["contract"]["role_contract_prompt_template_id"] = json!("");
    assert_eq!(route_err(&context), "E_TASK_PROMPT_TEMPLATE_ID_REQUIRED");
}
