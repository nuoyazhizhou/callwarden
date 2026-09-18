"""经 daemon authority 创建 §W3 承接卡（tests-only：live-daemon 族推广 _w3_harness /
修夹具/陈旧断言，去「夹具内 cargo build」）。承接自 pyt_remaining_handoff_20260915.md §3.2。

范围据实收窄（据 2026-09-16 干净单写环境实扫，见 pyt_w3_rescan_and_ops_20260916.md）：
交接所称「约 60 需 live daemon」实际远少——32 cargo-build 候选里 23 个本就直接通过，
真残留仅 9（分类见卡 step0）。C-02(§W9) 属独立 rust_ext 产品代码卡，不在本卡。
"""
from __future__ import annotations
import hashlib, json, os, sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT.parent))
from callwarden.server.daemon_client import HttpDaemonRpcClient

PARENT_ID = "T-1788871227327-45c94bd8"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "4baea3ff12c2ea5c"
ENDPOINT = os.environ.get("CW_DAEMON_ENDPOINT", "http://127.0.0.1:8535")
TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"
PLAN_DOC = "deliverables/software-company/w3_harness_rollout_plan_20260916.md"


def digest(name): return hashlib.sha256((TEMPLATE_DIR / name).read_bytes()).hexdigest().upper()


def contracts(allowed, acceptance, evidence):
    return [
        {"role": "executor", "skill_id": "none", "skill_version": "",
         "prompt_template_id": "cw.aprime.executor.startup.v1",
         "prompt_hash": digest("executor_planner_startup_v1.md"),
         "allowed_paths": json.dumps(allowed),
         "forbidden_paths": json.dumps([
             "db/", "rust_ext/src/", "scripts/refresh_shared_runtime.ps1",
             "direct SQLite writes", "task.apply", "task.close", "task.supersede",
             "status forgery"]),
         "commands": "pytest; git diff --check",
         "acceptance_checks": acceptance, "required_evidence": evidence,
         "handoff_to": "reviewer", "independence": "required"},
        {"role": "reviewer", "skill_id": "none", "skill_version": "",
         "prompt_template_id": "cw.aprime.reviewer.startup.v1",
         "prompt_hash": digest("reviewer_startup_v1.md"),
         "allowed_paths": "read-only source under review, tests, evidence and daemon projection",
         "forbidden_paths": "production edits; task.apply; task.close; direct database writes",
         "commands": "read-only review",
         "acceptance_checks": ("residual list grounded in current clean-env scan; only tests/** "
             "touched (no rust_ext/db); every migrated file verified green; no false-fix masking"),
         "required_evidence": "independent PASS or BLOCKED record",
         "handoff_to": "adjudicator", "independence": "required"},
        {"role": "adjudicator", "skill_id": "none", "skill_version": "",
         "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
         "prompt_hash": digest("adjudicator_startup_v1.md"),
         "allowed_paths": "governance records, verdicts, ledger and projection read-only",
         "forbidden_paths": "source edits; direct database writes",
         "commands": "adjudication",
         "acceptance_checks": "verdict grounded in reviewer evidence; apply/close only after acceptance",
         "required_evidence": "ACCEPT or REJECT verdict record",
         "handoff_to": "", "independence": "not_applicable"},
    ]


ALLOWED = ["tests/", "deliverables/software-company/", "docs/evidence/", "cw_task_commit_ledger.json"]
ACCEPTANCE = (
    "§W3 live-daemon 族在干净单写环境逐文件零失败（以 rc + FAILED/ERROR 签名为准，非整树单进程跑）；"
    "manifest-未发布类经 _w3_harness（setup_w3_client 复用预建二进制+发布 snapshot）或修其夹具发布 manifest；"
    "「夹具内 cargo build」挂起类改为复用预建二进制（find_daemon_binary），禁止测试内 cargo build；"
    "windows_daemon/wsl 2 例陈旧断言对齐现行 workspace 权威契约（E_TASK_WORKSPACE_UNBOUND：需显式整数 workspace_id / task binding）；"
    "真缺陷候选 test_lease_gate_empirical / test_task_prompt_e2e 逐条定性：实现缺陷→本卡修或另立缺陷卡，陈旧断言→更新断言；"
    "禁止改 rust_ext/src/** 与 db/**（越界即 BLOCKED，C-02 属独立产品代码卡）；"
    "git diff --check clean；commit 前缀用本卡自身 task_id"
)
EVIDENCE = (
    "step0 盘点文档（" + PLAN_DOC + "）：以 2026-09-16 实扫为基线，逐文件列 文件名/用例数/失败签名/归因四类"
    "（真缺陷/陈旧断言/环境前置/需 harness 迁移），锁定真残留清单（不照抄交接的『约 60』，须实测）；"
    "扫描纪律记录（单写串行、pin RUSTUP_HOME/CARGO_HOME、预建隔离 HOME、每轮按父PID清理残留隔离daemon）；"
    "每个迁移文件的 before（挂起/manifest未发布/断言失败）/ after（pytest 通过）实测回执（junitxml 或 rc+签名）；"
    "族级回归：全部受影响文件零失败汇总；git diff --check"
)

CARD = {
    "title": "§W3 承接(rev2·文件级target_file)：live-daemon 族推广 tests/_w3_harness + 修陈旧断言（tests-only，去夹具内 cargo build/内联复制，零失败）",
    "origin": (
        "承接自 pyt_remaining_handoff_20260915.md §3.2 / backlog §W3。"
        "2026-09-16 干净单写环境实扫（pyt_w3_rescan_and_ops_20260916.md）：18 个 _w3_harness 文件 17 通过；"
        "32 个 cargo-build 候选 23 通过/8 失败/1 挂起，真残留仅 9（多非代码缺陷）。"
        "本卡据此据实收窄范围，逐条四类归因后清零真残留。"
    ),
    "backlog_section": "§W3",
}


def steps():
    return [
        {"action": "adjudicate", "target_file": PLAN_DOC,
         "target_symbol": "§W3 实际残留盘点与迁移方案（强制前置，不改代码）",
         "check_items": [
             "在本 HEAD 复跑并核对残留清单（不照抄交接『约60』）：区分 a) 需 harness 迁移（夹具内 cargo build 挂 / manifest 未发布）b) 陈旧断言（windows_daemon/wsl 工作区权威）c) 环境前置（可选 grammar 包）d) 真缺陷候选（lease_gate_empirical / task_prompt_e2e）",
             "逐文件给出目标夹具改法：manifest 未发布→setup_w3_client；夹具内 cargo build→find_daemon_binary 复用预建；陈旧断言→对齐 E_TASK_WORKSPACE_UNBOUND 现行契约（显式 workspace_id + task_workspace_bindings seed）",
             "记录扫描纪律（单写串行；pin RUSTUP_HOME/CARGO_HOME；预建隔离 HOME .callwarden；每轮结束按父 PID 精确清理残留隔离 daemon，避免秒级假失败）",
             "明确真缺陷候选的路由（实现缺陷本卡修/另立卡 vs 陈旧断言更新），本卡只碰 tests/**，C-02(rust_ext) 排除在外",
             "本 step 不改任何代码，产出落该 deliverables 证据文档",
         ]},
        {"action": "implement",
         "target_file": ";".join([
             "tests/test_http_capability_registry.py",
             "tests/test_http_native_read_cutover.py",
         ]),
         "target_symbol": "manifest-未发布 类迁移到 _w3_harness.setup_w3_client",
         "check_items": [
             "删除文件内联复制的 _find_daemon_binary/_spawn_isolated_daemon/_wait_manifest（其 _wait_manifest 读父进程 get_http_manifest_dir()，与子 daemon 写 manifest 的 data_root/userhome/.callwarden 不同处 → 假『未发布 manifest』），改用 tests/_w3_harness.setup_w3_client（按 data_root/userhome 正确找 manifest + register + seed + snapshot.publish）",
             "复用既有 harness，不复制第二套；改动最小化到夹具/装配层，不改被测语义",
             "逐文件 pytest 通过（rc=0），before/after 留证",
         ]},
        {"action": "implement",
         "target_file": ";".join([
             "tests/test_rust_cli_diff.py",
             "tests/test_windows_bridge_e2e.py",
             "tests/test_l9_rust_multilang.py",
         ]),
         "target_symbol": "去夹具内 cargo build / spawn 挂起 / 可选依赖 importorskip",
         "check_items": [
             "test_rust_cli_diff：夹具内 cargo build 改用 _w3_harness.find_daemon_binary 复用预建二进制（禁止测试内编译）；若为不可跳过的 Rust 差分前置，改走 harness 隔离 daemon 或显式 skip+披露",
             "test_windows_bridge_e2e：裸 subprocess spawn communicate 挂起 → 加有界超时/改走 harness 客户端，避免阻塞",
             "test_l9_rust_multilang：tree_sitter_elixir/hcl 属可选 grammar 包，用 pytest.importorskip 守门，判纯依赖缺失则 skip，**不得伪绿**，并在证据记录本机未装该包",
             "逐文件 pytest 通过或合法 skip（rc=0），留证",
         ]},
        {"action": "implement",
         "target_file": ";".join([
             "tests/test_windows_daemon_e2e.py",
             "tests/test_windows_wsl_authority_e2e.py",
         ]),
         "target_symbol": "windows_daemon/wsl 陈旧断言对齐工作区权威契约",
         "check_items": [
             "期望已过期处（如 'ws-shared' 解析为整数、缺显式 workspace_id）按 daemon 现行 fail-closed 契约更新：显式带整数 workspace_id + 经 _w3_harness.seed_cli_task_authority / task_workspace_bindings 建立权威绑定，而非放宽 daemon 侧",
             "禁止改 daemon/rust_ext 侧权威逻辑；仅调测试夹具/期望",
             "逐文件 pytest 通过",
         ]},
        {"action": "implement",
         "target_file": ";".join([
             "tests/test_lease_gate_empirical.py",
             "tests/test_task_prompt_e2e.py",
         ]),
         "target_symbol": "真缺陷候选逐条定性（实现缺陷 vs 陈旧断言）",
         "check_items": [
             "test_lease_gate_empirical：逐条 8 断言核实是 daemon 租约/门禁行为回归（实现缺陷）还是测试期望过期；若根因落 rust_ext → 本卡 forbidden 不修，登记为独立产品缺陷卡并在此说明；tests 侧可修（如 request_id 重放/身份夹具装配）则修并验证",
             "test_task_prompt_e2e：定位 test_cli_live_parity_positive 的 TypeError 根因（测试面 or 实现面），tests-only 可修则修，越界另立卡",
             "本步产出不含未授权 rust_ext 改动；越界项以 finding/说明交回",
         ]},
        {"action": "verify", "target_file": "deliverables/software-company/w3_family_regression_20260916.md",
         "target_symbol": "族级零失败回归 + 披露 + 交接准备",
         "check_items": [
             "全部受影响文件在干净单写环境逐文件零失败汇总（junitxml 或 rc+签名），逐条真缺陷候选定性收口",
             "每轮结束按父 PID 精确清理本轮拉起的隔离 daemon + compat_worker",
             "git diff --check clean；证据汇总落该文档",
         ]},
    ]


def main():
    client = HttpDaemonRpcClient(endpoint=ENDPOINT, verify_health=False, validate_manifest=False)
    listing = client.call("task.list", {"parent_id": PARENT_ID, "status": "", "limit": 200,
        "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID})
    existing = listing.get("tasks", listing) if isinstance(listing, dict) else listing
    titles = {it.get("title") for it in (existing or []) if isinstance(it, dict)}
    if CARD["title"] in titles:
        print(json.dumps({"result": "exists", "title": CARD["title"]}, ensure_ascii=False, indent=2)); return
    description = (f"承接来源：{CARD['origin']}。登记 finding：§W3。\n"
        f"权威 finding 明细见 backlog §W3；本轮实扫与归因见 pyt_w3_rescan_and_ops_20260916.md。\n"
        f"合同边界：仅 tests/** + deliverables + docs/evidence + 台账；禁改 rust_ext/src/**、db/**；"
        f"不直写 SQLite；提交前缀用本卡 task_id。\n验收：{ACCEPTANCE}\n证据：{EVIDENCE}\n")
    resp = client.call("task.create", {"title": CARD["title"], "description": description,
        "parent_id": PARENT_ID, "workspace_id": WORKSPACE_ID,
        "workspace_instance_id": WORKSPACE_INSTANCE_ID, "identity_policy": "legacy_identity_v1",
        "steps": steps(), "role_contracts": contracts(ALLOWED, ACCEPTANCE, EVIDENCE)})
    print(json.dumps({"result": "created", "title": CARD["title"],
        "task_id": resp.get("task_id") if isinstance(resp, dict) else None,
        "governance_projection": resp.get("governance_projection") if isinstance(resp, dict) else None,
        "status": resp.get("status") if isinstance(resp, dict) else None}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
