"""经 daemon authority 创建 W12 承接卡：`query.metrics_summary` 越 scope 契约缺陷修复。

背景：PYT 回归卡 step#4（tests-only 边界）改写真 HTTP transport 后暴露两个生产真缺陷，
修复必须落在其合同 forbidden_paths（rust_ext/src/**、server/**）。按用户 2026-09-13 裁决
（方案 A），把这批改动剥离为独立 remediation 卡承接，PYT 卡保持 tests-only。

本合同与 `w12_metrics_summary_remediation_contract.md` 一并作为任务描述。

用法（先确保 daemon 在 127.0.0.1:1615 就绪）：
    python deliverables/software-company/create_w12_metrics_summary_remediation_task.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.server.daemon_client import HttpDaemonRpcClient

# 承接来源：PYT 回归卡（其 step#4 = T-1789139378194-02f1f66c, fix_defect）
PARENT_ID = "T-1788871227327-45c94bd8"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "4baea3ff12c2ea5c"
ENDPOINT = "http://127.0.0.1:1615"

TITLE = "W12 承接：query.metrics_summary 越 scope 契约缺陷 remediation（方案 A 剥离）"
CONTRACT_PATH = (
    PROJECT_ROOT / "deliverables" / "software-company"
    / "w12_metrics_summary_remediation_contract.md"
)
TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def digest(name: str) -> str:
    """计算 role 启动模板 sha256（大写），与 manifest 的 content_sha256 口径一致。"""
    return hashlib.sha256((TEMPLATE_DIR / name).read_bytes()).hexdigest().upper()


def contracts() -> list[dict]:
    """A′ 三角色合同：executor 的 allowed_paths 显式覆盖 3 个生产文件 + tests + deliverables。"""
    executor_allowed = [
        "rust_ext/src/daemon/metrics_handlers.rs",
        "rust_ext/src/daemon/query_compat_handlers.rs",
        "server/tools/tools_workspace.py",
        "tests/",
        "deliverables/software-company/",
    ]
    return [
        {
            "role": "executor", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.executor.startup.v1",
            "prompt_hash": digest("executor_planner_startup_v1.md"),
            "allowed_paths": json.dumps(executor_allowed),
            "forbidden_paths": json.dumps([
                "cli/", "db/", "scripts/refresh_shared_runtime.ps1",
                "direct SQLite writes", "task.apply", "task.close",
                "task.supersede", "status forgery",
            ]),
            "commands": "cargo test; pytest; git diff --check",
            "acceptance_checks": (
                "empty workspace no longer crashes; query.metrics_summary returns the "
                "8-field legacy contract from the single source of truth; 046 and 084-088 green"
            ),
            "required_evidence": (
                "cargo test output; pytest output; outbound-envelope assertion hitting "
                "query.metrics_summary; git diff; evidence manifest/hash"
            ),
            "handoff_to": "reviewer", "independence": "required",
        },
        {
            "role": "reviewer", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.reviewer.startup.v1",
            "prompt_hash": digest("reviewer_startup_v1.md"),
            "allowed_paths": "read-only W12 source, tests, evidence and daemon projection",
            "forbidden_paths": "production edits; task.apply; task.close; direct database writes",
            "commands": "read-only review",
            "acceptance_checks": (
                "no second field-set; single source of truth honored; forbidden paths untouched; "
                "empty-workspace negative case proven"
            ),
            "required_evidence": "independent PASS or BLOCKED record",
            "handoff_to": "adjudicator", "independence": "required",
        },
        {
            "role": "adjudicator", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
            "prompt_hash": digest("adjudicator_startup_v1.md"),
            "allowed_paths": "W12 lifecycle finalization only",
            "forbidden_paths": "production edits; direct database writes",
            "commands": "task.apply; task.close; task.next_action after reviewer PASS",
            "acceptance_checks": "independent reviewer PASS; apply then close then COMPLETE",
            "required_evidence": "finalization manifest",
            "handoff_to": "complete", "independence": "required",
        },
    ]


def main() -> None:
    # 显式 endpoint + 关闭 manifest 校验/健康交叉核对：避免命中真实 HOME 的 stale
    # manifest（E_HTTP_MANIFEST_STALE）。显式 loopback endpoint 本身即合法发现路径。
    client = HttpDaemonRpcClient(
        endpoint=ENDPOINT, verify_health=False, validate_manifest=False,
    )

    listing = client.call("task.list", {
        "parent_id": PARENT_ID, "status": "", "limit": 200,
        "workspace_id": WORKSPACE_ID, "workspace_instance_id": WORKSPACE_INSTANCE_ID,
    })
    existing = listing.get("tasks", listing) if isinstance(listing, dict) else listing
    for item in existing if isinstance(existing, list) else []:
        if isinstance(item, dict) and item.get("title") == TITLE:
            print(json.dumps({"result": "exists", "task": item}, ensure_ascii=False))
            return

    response = client.call("task.create", {
        "title": TITLE,
        "description": CONTRACT_PATH.read_text(encoding="utf-8"),
        "parent_id": PARENT_ID,
        "workspace_id": WORKSPACE_ID,
        "workspace_instance_id": WORKSPACE_INSTANCE_ID,
        # 顶层显式 identity_policy（白名单字段）：envelope 缺失时 daemon 生成
        # generic envelope 并注入该 policy；非法/缺失 → E_TASK_IDENTITY_POLICY_REQUIRED。
        "identity_policy": "legacy_identity_v1",
        "steps": [
            {
                "action": "implement",
                "target_file": "rust_ext/src/daemon/metrics_handlers.rs",
                "target_symbol": "handle_metrics_summary",
                "check_items": [
                    "empty workspace returns zeroed 8-field summary instead of internal_error",
                    "reuse summary_metrics_summary (no second field-set)",
                    "remove dead scalar_f64",
                ],
            },
            {
                "action": "implement",
                "target_file": "rust_ext/src/daemon/query_compat_handlers.rs",
                "target_symbol": "summary_metrics_summary",
                "check_items": [
                    "promote fn to pub(crate) fn",
                    "document as the single source of truth for the 8-field contract",
                ],
            },
            {
                "action": "implement",
                "target_file": "server/tools/tools_workspace.py",
                "target_symbol": "get_code_metrics_summary",
                "check_items": [
                    "add CodeMetricsSummary TypedDict aligned field-by-field with the Rust impl",
                    "return annotation -> CodeMetricsSummary so FastMCP emits outputSchema",
                ],
            },
            {
                "action": "test",
                "target_file": "tests/",
                "target_symbol": "test_cli_046_http_rpc.py and test_cli_084 to test_cli_088 http_rpc",
                "check_items": [
                    "all green",
                    "046 outbound-envelope assertion hits query.metrics_summary (real HTTP transport)",
                ],
            },
            {
                "action": "release_verify",
                "target_file": "runtime/current",
                "target_symbol": "W12 regression closure",
                "check_items": [
                    "cargo test metrics_handlers",
                    "pytest 046+084..088",
                    "git diff --check clean",
                    "commit prefix uses THIS card task_id, never the PYT card id",
                ],
            },
        ],
        "role_contracts": contracts(),
    })
    print(json.dumps({"result": "created", "response": response}, ensure_ascii=False))


if __name__ == "__main__":
    main()
