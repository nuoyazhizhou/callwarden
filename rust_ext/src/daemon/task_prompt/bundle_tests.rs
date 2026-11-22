//! bundle.rs 单元测试（§5.1 / §9.6 / §9.7 + §11.2 golden 与确定性矩阵；
//! 测试名携带 `task_prompt_renderer` 验收 filter token）。
//!
//! Golden 冻结值（GOLDEN_* 常量）由本测试首次运行打印后冻结：
//! 先跑 `cargo test task_prompt_renderer_golden_print -- --nocapture`，
//! 再把打印值填入常量（冻结前 golden 测试自动跳过断言）。

use serde_json::{json, Value};

use crate::daemon::task_prompt::bundle::compile_bundle;
use crate::daemon::task_prompt::canonical::canonical_json;
use crate::daemon::task_prompt::render_tests::{
    fixture_context, GOLDEN_GENERATED_AT, ADJ_V1_HASH, EXEC_V1_HASH, EXEC_V4_HASH, REV_V1_HASH,
};
use crate::daemon::task_prompt::route::ROLE_PROMPTS_MANIFEST_SHA256;

/// Golden 冻结 hash（spec §11.2）。首次运行打印，随后冻结。
/// 当前值 = 提示词三层整合（executor v4 漂移句修正 + 模板指针化）后的重冻结。
const GOLDEN_EXECUTOR_LEGACY: Option<(&str, &str, &str, &str, &str)> = Some((
    "sha256:663807e4ab7567fe44c39d96df38e45a698d08166409c3fbc52c2740e19a567d",
    "sha256:59a459f7786097c671d48fbeec6e361c12d7a95bdec4e3722169d68d5d6a73f6",
    "sha256:ca00503fd550ad9d07b542140160bab32263e87e967296e1cdbd735f7a96740d",
    "sha256:1af10505b9319819c6c44a867478622c1bf05cc93417610bd143d46cc6df2d95",
    "sha256:1520099a81c94366635f07d3e207fc7252cf64ce233b131099620f2effde6a26",
));
const GOLDEN_REVIEWER_LEGACY: Option<(&str, &str, &str, &str, &str)> = Some((
    "sha256:663807e4ab7567fe44c39d96df38e45a698d08166409c3fbc52c2740e19a567d",
    "sha256:6415033d8f134392de16fca130bfb762cb6c70d9f466c770ec18a20fc4ce139e",
    "sha256:15e85a708af0c512689d8fa36ccb01e78d96e5c4e3d51a90053093cbe073681e",
    "sha256:1eac28014ac0c1aa8fc14ab8e03898283a646c1ebfb3baf7e69a3646c02de857",
    "sha256:15aff6c859a042c85adcca24a90fdadc4c097869437824a017d43f55dbff3bd2",
));
const GOLDEN_ADJUDICATOR_LEGACY: Option<(&str, &str, &str, &str, &str)> = Some((
    "sha256:663807e4ab7567fe44c39d96df38e45a698d08166409c3fbc52c2740e19a567d",
    "sha256:42a5f1defa81008b009058c1baf5d1a14b3ef4521e291b7b55c19bb473a77c3e",
    "sha256:5c178fefeceae5f0966f52fc5e4a2411ac74c89df3854f7024edf45a76acff7d",
    "sha256:69c477c47acb68925c4f981e08553cb3f6856c6d2c926c4e141bd9d00f83ada6",
    "sha256:b914fd89f5a1b4176c68fd5240437c7490e5e119e8e6a5593c2fd73f13f07374",
));
const GOLDEN_EXECUTOR_CURRENT: Option<(&str, &str, &str, &str, &str)> = Some((
    "sha256:663807e4ab7567fe44c39d96df38e45a698d08166409c3fbc52c2740e19a567d",
    "sha256:bdae65f57cc8d1a632bfcdb4be80f4b7574845c2cb01f1cfc762899f481e8155",
    "sha256:90de9c81a452621bc2616d52614b91a165094888452a0568ea0b194a7d1f328d",
    "sha256:d4b89b89a9846cd1f02095e9c762861b7838d3e9686f41a841a9312a2e2ffdb5",
    "sha256:a056dcf9b8b68cc75444df04ff214d6043a014de68028116fc1aa754fa15f939",
));
const GOLDEN_BLOCKED: Option<(&str, &str, &str, &str, &str)> = Some((
    "sha256:663807e4ab7567fe44c39d96df38e45a698d08166409c3fbc52c2740e19a567d",
    "sha256:80805d678832126ca1c5cad76cc474b0b896e9431182ffdea9f023078b9c3b85",
    "sha256:608e56b56c1d6606759850d4500404cc4c4f2e9d688594d2ef3df916ede95e22",
    "sha256:b462a9eefbf8fa012a2ef9c9bed22465e6bd4fe2ec066b9ff8e279da19a1af6a",
    "sha256:0f31825e8ec8f2e573ddb84887017f140f99099c8833ce51601c1a1a62412cef",
));
const GOLDEN_WAITING: Option<(&str, &str, &str, &str, &str)> = Some((
    "sha256:663807e4ab7567fe44c39d96df38e45a698d08166409c3fbc52c2740e19a567d",
    "sha256:56874c7329c9c62bac92ddd4be9720a04417a585f904de1d484f8abc7653f56b",
    "sha256:044fa14d4f588ee477f9477040c1e0d8a42fc467aba979d780617cbfab1dd2b3",
    "sha256:64f19dfebb58fc43fb64235aebb34dc2a9d275937b5ff4d808e35920f7fc4b63",
    "sha256:8d6c47f9621ea065753670137e2217bf6d4f0484832b113ceb64f6835301fea0",
));
const GOLDEN_TERMINAL: Option<(&str, &str, &str, &str, &str)> = Some((
    "sha256:663807e4ab7567fe44c39d96df38e45a698d08166409c3fbc52c2740e19a567d",
    "sha256:450a850620529dd9a2fa1b1f6e791a6a2f4de6cfcac2d1affbe4473142d6257a",
    "sha256:d1f95cc4cced2e3de4cf754a71dabdeb7afb44af95f3d129a25a793b32edcbcb",
    "sha256:6f030c61d770be7a6474a07db7b9586500b64d42e1d46af4d8c985ab980faa11",
    "sha256:620362134e6a610e1fe8e9f3109310d2657aa5460bb315b27b5ab216b2fee698",
));

fn bundle_for(context: &Value) -> Value {
    let selection = crate::daemon::task_prompt::route::route_and_select(context)
        .expect("route ok");
    compile_bundle(context, &selection, &json!({}), GOLDEN_GENERATED_AT)
        .expect("compile ok")
        .bundle
}

fn golden_values(bundle: &Value) -> (String, String, String, String, String) {
    let t = &bundle["template"];
    (
        t["compiler_policy_hash"].as_str().unwrap_or_default().to_string(),
        t["body_template_hash"].as_str().unwrap_or_default().to_string(),
        bundle["context_hash"].as_str().unwrap_or_default().to_string(),
        bundle["prompt"]["sha256"].as_str().unwrap_or_default().to_string(),
        bundle["bundle_hash"].as_str().unwrap_or_default().to_string(),
    )
}

fn print_golden(name: &str, bundle: &Value) {
    let (p, b, c, pr, bu) = golden_values(bundle);
    println!(
        "GOLDEN {name}: policy={p} body={b} context={c} prompt={pr} bundle={bu}"
    );
}

// ------------------------------------------------------------ schema shape

#[test]
fn task_prompt_renderer_bundle_matches_role_prompt_bundle_v1_schema() {
    let context = fixture_context(
        "READY",
        "CLAIM",
        Some("executor"),
        Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)),
    );
    let bundle = bundle_for(&context);
    assert_eq!(bundle["schema_version"], json!("role_prompt_bundle_v1"));
    assert_eq!(bundle["prompt_kind"], json!("role_work"));
    // role_work 的 body ID/hash 必须与 Role Contract 精确匹配（§5.2）。
    assert_eq!(bundle["template"]["source"], json!("role_contract"));
    assert_eq!(bundle["template"]["body_template_id"], json!("cw.aprime.executor.startup.v1"));
    assert_eq!(
        bundle["template"]["body_template_hash"],
        json!(crate::daemon::task_prompt::canonical::sha256_prefixed(
            crate::daemon::task_prompt::route::TEMPLATE_REGISTRY[0].body.as_bytes()
        ))
    );
    // authority / routing / contract 透传自 context。
    assert_eq!(bundle["authority"], context["authority"]);
    assert_eq!(bundle["routing"], context["routing"]);
    assert_eq!(bundle["contract"], context["contract"]);
    assert_eq!(bundle["authorization"]["valid_for_claim"], json!(false));
    assert_eq!(bundle["authorization"]["mutation_recheck_required"], json!(true));
    // generated_at 只显示，不参与 hash（§9.7）。
    assert_eq!(bundle["generated_at"], json!(GOLDEN_GENERATED_AT));
}

#[test]
fn task_prompt_renderer_system_bundles_are_non_actionable_with_manifest_source() {
    for (decision, action, kind) in [
        ("BLOCKED", "RESOLVE", "blocked_recovery"),
        ("WAITING", "WAIT", "waiting"),
        ("COMPLETE", "NONE", "terminal"),
    ] {
        let bundle = bundle_for(&fixture_context(decision, action, None, None));
        assert_eq!(bundle["prompt_kind"], json!(kind));
        assert_eq!(bundle["template"]["source"], json!("system"));
        assert_eq!(bundle["authorization"]["routing_state"], json!("non_actionable"));
        assert_eq!(bundle["authorization"]["valid_for_claim"], json!(false));
    }
}

#[test]
fn task_prompt_renderer_bundle_hash_excludes_generated_at() {
    // §9.7：generated_at 不进入 context_hash/prompt.sha256/bundle_hash。
    let context = fixture_context(
        "READY",
        "CLAIM",
        Some("executor"),
        Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)),
    );
    let selection = crate::daemon::task_prompt::route::route_and_select(&context).expect("route");
    let a = compile_bundle(&context, &selection, &json!({}), "2026-09-06T12:00:00Z")
        .expect("ok")
        .bundle;
    let b = compile_bundle(&context, &selection, &json!({}), "2027-01-01T00:00:00Z")
        .expect("ok")
        .bundle;
    assert_ne!(a["generated_at"], b["generated_at"]);
    assert_eq!(a["context_hash"], b["context_hash"]);
    assert_eq!(a["prompt"]["sha256"], b["prompt"]["sha256"]);
    assert_eq!(a["bundle_hash"], b["bundle_hash"]);
}

#[test]
fn task_prompt_renderer_bundle_hash_covers_minimal_closure_only() {
    // §9.6：bundle_hash 输入只含 schema_version/task_id/prompt_kind/
    // context_hash/prompt.sha256 五键（通过重算验证闭集）。
    let context = fixture_context(
        "READY",
        "CLAIM",
        Some("executor"),
        Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)),
    );
    let bundle = bundle_for(&context);
    let expected_input = json!({
        "schema_version": "role_prompt_bundle_v1",
        "task_id": bundle["task_id"],
        "prompt_kind": bundle["prompt_kind"],
        "context_hash": bundle["context_hash"],
        "prompt": { "sha256": bundle["prompt"]["sha256"] },
    });
    let recomputed = crate::daemon::task_prompt::canonical::sha256_prefixed(
        &canonical_json(&expected_input).expect("canonical"),
    );
    assert_eq!(bundle["bundle_hash"], json!(recomputed));
}

// ---------------------------------------------------------------- goldens

// ⚠️ Golden 生命周期警告（TP-11G 2a-P0-B，2026-10-15）：
// manifest.json 的任何变更都会经 build.rs 改变 ROLE_PROMPTS_MANIFEST_SHA256
// → template.manifest_hash → **全部 7 个 golden** 的 context_hash/bundle_hash
// 漂移（policy/body/prompt 三件不变即 manifest-only 漂移的签名；序断言在
// 第一个 golden panic 即中止，后续漂移被掩盖——CI 只看得见第一个）。
// 重冻结流程：GOLDEN_* 全部置 None → 跑 print_unfrozen_values 取新值 →
// 更新下方 7 个常量 **和** GOLDEN_MANIFEST_HASH_AT_FREEZE → 守卫测试
// task_prompt_golden_manifest_hash_drift_guard 同步更新。缺任何一步，
// 守卫测试会以可操作提示先行失败。

/// manifest_hash 冻结记录值（重冻结时与 ROLE_PROMPTS_MANIFEST_SHA256 同步更新）。
/// 当前值 = 提示词三层整合（executor v4 content_sha256 更新）之后的 manifest 裸字节 hash。
const GOLDEN_MANIFEST_HASH_AT_FREEZE: &str =
    "sha256:12cc8aa5b211af04baa28f3fcbc1e332de8713e163ddac1900aeabfb8d4a4a9c";

fn assert_golden(
    name: &str,
    frozen: Option<(&str, &str, &str, &str, &str)>,
    bundle: &Value,
) {
    match frozen {
        Some((policy, body, ctx, prompt, bundle_hash)) => {
            let actual = golden_values(bundle);
            let expected = (
                policy.to_string(),
                body.to_string(),
                ctx.to_string(),
                prompt.to_string(),
                bundle_hash.to_string(),
            );
            assert_eq!(actual, expected, "golden {name} drifted");
        }
        None => {
            // 冻结前打印实际值。
            print_golden(name, bundle);
        }
    }
}

#[test]
fn task_prompt_renderer_golden_print_unfrozen_values() {
    // 冻结辅助：--nocapture 运行后把打印值填入 GOLDEN_* 常量。
    let exec_legacy = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)));
    assert_golden("executor-legacy", GOLDEN_EXECUTOR_LEGACY, &bundle_for(&exec_legacy));
    let reviewer_legacy = fixture_context("READY", "REVIEW", Some("reviewer"), Some(("cw.aprime.reviewer.startup.v1", REV_V1_HASH)));
    assert_golden("reviewer-legacy", GOLDEN_REVIEWER_LEGACY, &bundle_for(&reviewer_legacy));
    let adjudicator_legacy = fixture_context("READY", "ADJUDICATE", Some("adjudicator"), Some(("cw.aprime.adjudicator.startup.v1", ADJ_V1_HASH)));
    assert_golden("adjudicator-legacy", GOLDEN_ADJUDICATOR_LEGACY, &bundle_for(&adjudicator_legacy));
    let exec_current = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.v4", EXEC_V4_HASH)));
    assert_golden("executor-current", GOLDEN_EXECUTOR_CURRENT, &bundle_for(&exec_current));
    assert_golden("blocked", GOLDEN_BLOCKED, &bundle_for(&fixture_context("BLOCKED", "RESOLVE", None, None)));
    assert_golden("waiting", GOLDEN_WAITING, &bundle_for(&fixture_context("WAITING", "WAIT", None, None)));
    assert_golden("terminal", GOLDEN_TERMINAL, &bundle_for(&fixture_context("COMPLETE", "NONE", None, None)));
}

#[test]
fn task_prompt_renderer_goldens_freeze_seven_bundle_shapes() {
    // §11.2：三个 legacy role + 一个 current role + 三个 system golden，
    // 每个固定 policy/body/context/prompt/bundle 五 hash。
    let exec_legacy = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)));
    assert_golden("executor-legacy", GOLDEN_EXECUTOR_LEGACY, &bundle_for(&exec_legacy));
    let reviewer_legacy = fixture_context("READY", "REVIEW", Some("reviewer"), Some(("cw.aprime.reviewer.startup.v1", REV_V1_HASH)));
    assert_golden("reviewer-legacy", GOLDEN_REVIEWER_LEGACY, &bundle_for(&reviewer_legacy));
    let adjudicator_legacy = fixture_context("READY", "ADJUDICATE", Some("adjudicator"), Some(("cw.aprime.adjudicator.startup.v1", ADJ_V1_HASH)));
    assert_golden("adjudicator-legacy", GOLDEN_ADJUDICATOR_LEGACY, &bundle_for(&adjudicator_legacy));
    let exec_current = fixture_context("READY", "CLAIM", Some("executor"), Some(("cw.aprime.executor.startup.v4", EXEC_V4_HASH)));
    assert_golden("executor-current", GOLDEN_EXECUTOR_CURRENT, &bundle_for(&exec_current));
    assert_golden("blocked", GOLDEN_BLOCKED, &bundle_for(&fixture_context("BLOCKED", "RESOLVE", None, None)));
    assert_golden("waiting", GOLDEN_WAITING, &bundle_for(&fixture_context("WAITING", "WAIT", None, None)));
    assert_golden("terminal", GOLDEN_TERMINAL, &bundle_for(&fixture_context("COMPLETE", "NONE", None, None)));
}

// ------------------------------------------------------------- determinism

#[test]
fn task_prompt_renderer_same_snapshot_100x_byte_determinism() {
    // §11.2：同一 snapshot 连续 100 次组合输出字节级确定。
    let context = fixture_context(
        "READY",
        "CLAIM",
        Some("executor"),
        Some(("cw.aprime.executor.startup.v1", EXEC_V1_HASH)),
    );
    let selection = crate::daemon::task_prompt::route::route_and_select(&context).expect("route");
    let first = compile_bundle(&context, &selection, &json!({}), GOLDEN_GENERATED_AT)
        .expect("ok");
    for _ in 0..100 {
        let again =
            compile_bundle(&context, &selection, &json!({}), GOLDEN_GENERATED_AT).expect("ok");
        assert_eq!(first.render.prompt_text, again.render.prompt_text);
        assert_eq!(first.bundle_json, again.bundle_json);
        assert_eq!(first.bundle["bundle_hash"], again.bundle["bundle_hash"]);
        assert_eq!(first.bundle["context_hash"], again.bundle["context_hash"]);
    }

/// TP-11G 2a-P0-B：manifest_hash 漂移守卫（无声漂移 → 可操作早期失败）。
///
/// manifest.json 的任何变更都会经 build.rs 改变 ROLE_PROMPTS_MANIFEST_SHA256，
/// 进而传播到全部 7 个 golden 的 context_hash/bundle_hash（2026-10-15 37c825b
/// 实证）。序断言在第一个 golden panic 即中止，后续漂移被掩盖。本守卫在任何
/// golden 断言之前失败，把无声漂移变成可操作早期失败。
#[test]
fn task_prompt_golden_manifest_hash_drift_guard() {
    assert_eq!(
        ROLE_PROMPTS_MANIFEST_SHA256,
        GOLDEN_MANIFEST_HASH_AT_FREEZE,
        "manifest 已变更 → 须同步重冻结全部 7 个 golden \
         （policy/body/prompt 不变、context/bundle 变是 manifest-only 正常形态）；\
         流程：GOLDEN_* 置 None → print_unfrozen_values 取新值 → 更新 7 常量 \
         + GOLDEN_MANIFEST_HASH_AT_FREEZE"
    );
}
}

