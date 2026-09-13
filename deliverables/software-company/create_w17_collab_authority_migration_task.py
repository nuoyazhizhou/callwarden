"""经 daemon authority 创建 W17 承接卡：`cw collab` 治理写命令面迁移到 HTTP authority。

背景：W12 承接卡（T-1789274621921-e5464ad8）的独立 Reviewer 盲审实测发现——
`cw collab verdict` 经本地 Named Pipe（`DaemonClient` → `UnixDaemonRpcClient`）发送，
而 `cw lease acquire` / `cw task handoff` 走 HTTP authority（`HttpDaemonRpcClient`），
同一 task 两侧 authority 不一致，Reviewer 按默认 CLI 会 fail-closed。

修复落点 `cli/**` 超出 W12 卡 allowed_paths，且 PYT 卡 forbidden_paths 含 `cli/**`，
故按用户 2026-09-13 裁决（新建独立承接卡 + 4 个治理写方法一并迁移）建卡。

本合同与 `w17_collab_authority_migration_contract.md` 一并作为任务描述。

用法（先确保 daemon 在 127.0.0.1:1615 就绪）：
    python deliverables/software-company/create_w17_collab_authority_migration_task.py
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

# 承接来源：PYT 回归卡（W12/W17 backlog 的宿主卡）
PARENT_ID = "T-1788871227327-45c94bd8"
WORKSPACE_ID = 1
WORKSPACE_INSTANCE_ID = "4baea3ff12c2ea5c"
ENDPOINT = "http://127.0.0.1:1615"

TITLE = "W17 承接：cw collab 治理写命令面迁移到 HTTP authority（消除传输面不一致）"
CONTRACT_PATH = (
    PROJECT_ROOT / "deliverables" / "software-company"
    / "w17_collab_authority_migration_contract.md"
)
TEMPLATE_DIR = PROJECT_ROOT / "deliverables" / "software-company" / "aprime_role_contracts"


def digest(name: str) -> str:
    """计算 role 启动模板 sha256（大写），与 manifest 的 content_sha256 口径一致。"""
    return hashlib.sha256((TEMPLATE_DIR / name).read_bytes()).hexdigest().upper()


def contracts() -> list[dict]:
    """A′ 三角色合同：executor 的 allowed_paths 覆盖 cli/main.py + 受影响测试 + 文档。"""
    executor_allowed = [
        "cli/main.py",
        "server/daemon_client.py",
        "tests/",
        "docs/cli_reference.md",
        "TOOLS.md",
        ".agents/skills/cw-task-loop/references/role-protocol.md",
        "deliverables/software-company/",
    ]
    return [
        {
            "role": "executor", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.executor.startup.v1",
            "prompt_hash": digest("executor_planner_startup_v1.md"),
            "allowed_paths": json.dumps(executor_allowed),
            "forbidden_paths": json.dumps([
                "rust_ext/", "db/", "scripts/refresh_shared_runtime.ps1",
                "direct SQLite writes", "task.apply", "task.close",
                "task.supersede", "status forgery",
                "second transport abstraction", "local SQLite fallback",
            ]),
            "commands": (
                "pytest tests/test_task_verdict_cli.py "
                "tests/test_cli_collab_snapshot_publish.py; git diff --check"
            ),
            "acceptance_checks": (
                "no DaemonClient/UnixDaemonRpcClient left in _handle_collab; all 4 governance "
                "writes (snapshot.publish/verdict.submit/reveal.submit/gate.decide) go through "
                "route_rpc HTTP authority; the 6 transport-pinning tests updated and green; "
                "new fail-closed negative case proven"
            ),
            "required_evidence": (
                "source-level assertion that _handle_collab has zero DaemonClient references; "
                "pytest output for the 2 affected files; new negative-case output; git diff; "
                "evidence manifest/hash"
            ),
            "handoff_to": "reviewer", "independence": "required",
        },
        {
            "role": "reviewer", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.reviewer.startup.v1",
            "prompt_hash": digest("reviewer_startup_v1.md"),
            "allowed_paths": "read-only W17 source, tests, evidence and daemon projection",
            "forbidden_paths": "production edits; task.apply; task.close; direct database writes",
            "commands": "read-only review",
            "acceptance_checks": (
                "single authority source honored; no second transport abstraction; forbidden "
                "paths untouched; fail-closed semantics preserved; tests do not pin the defect"
            ),
            "required_evidence": "independent PASS or BLOCKED record",
            "handoff_to": "adjudicator", "independence": "required",
        },
        {
            "role": "adjudicator", "skill_id": "none", "skill_version": "",
            "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
            "prompt_hash": digest("adjudicator_startup_v1.md"),
            "allowed_paths": "W17 lifecycle finalization only",
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
                "target_file": "cli/main.py",
                "target_symbol": "_handle_collab",
                "check_items": [
                    "route verdict.submit through route_rpc(..., 'GOVERNANCE_WRITE')",
                    "preserve _collect_identity / identity payload exactly",
                    "preserve _collab_governance_rejection fail-closed on DaemonUnavailableError",
                ],
            },
            {
                "action": "implement",
                "target_file": "cli/main.py",
                "target_symbol": "_handle_collab",
                "check_items": [
                    "route snapshot.publish / reveal.submit / gate.decide through the same route_rpc",
                    "remove the local DaemonClient.get_instance() call site entirely",
                    "prove whether gate.decide needs a _NO_WORKSPACE_METHODS entry (no guessing)",
                ],
            },
            {
                "action": "test",
                "target_file": "tests/test_task_verdict_cli.py",
                "target_symbol": "test_task_bound_verdict_*",
                "check_items": [
                    "replace the DaemonClient.get_instance monkeypatch with an authority-route fake",
                    "assert verdict.submit reaches the injected HTTP rpc client",
                    "keep the malformed-JSON and missing-instance negative cases green",
                ],
            },
            {
                "action": "test",
                "target_file": "tests/test_cli_collab_snapshot_publish.py",
                "target_symbol": "test_collab_publish_*",
                "check_items": [
                    "assert publish routes through the authority face (HTTP fake), not Named Pipe",
                    "keep the fail-closed case for a missing authoritative workspace_instance_id",
                ],
            },
            {
                "action": "implement",
                "target_file": ".agents/skills/cw-task-loop/references/role-protocol.md",
                "target_symbol": "Adjudicator/Reviewer command blocks",
                "check_items": [
                    "collab verdict command form matches the implemented CLI surface",
                    "document the mandatory --view-manifest-hash flag",
                    "fix the stale signature in docs/cli_reference.md and TOOLS.md",
                ],
            },
            {
                "action": "release_verify",
                "target_file": "runtime/current",
                "target_symbol": "W17 regression closure",
                "check_items": [
                    "pytest the 2 affected files green",
                    "git diff --check clean",
                    "commit prefix uses THIS card task_id",
                ],
            },
        ],
        "role_contracts": contracts(),
    })
    print(json.dumps({"result": "created", "response": response}, ensure_ascii=False))


if __name__ == "__main__":
    main()
