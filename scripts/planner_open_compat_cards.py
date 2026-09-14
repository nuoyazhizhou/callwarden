#!/usr/bin/env python
"""P0-compat 迁移批次 v2：按 tools_*.py module 组开迁移卡（planner 派工）。

背景：T-1787293451688 父任务 187 卡全 closed，矩阵仍余 58 个 python_compat
（transition）。按 module 分组成 6 张卡（同文件方法同卡，避免共享文件碎片化），
每卡 4 step：port_rust_handler / thin_python_client / fixture_matrix / matrix_verify。

用法：
    PYTHONPATH=C:/git_work python scripts/planner_open_compat_cards.py [--dry-run] [--groups g1,g2]

依赖：daemon 存活（cw.py task create 走 governed RPC，缺省 A' 三角色合同模板）。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

REPO = "C:/git_work/callwarden"
MATRIX = "deliverables/software-company/tool_migration_matrix.json"

# 每卡覆盖的 module 组（module 名 → client 文件）
GROUPS: dict[str, list[str]] = {
    "identity-lease-small": ["server/tools/tools_p3_identity.py", "server/tools/tools_p4_lease.py"],
    "tools_query": ["server/tools/tools_query.py"],
    "tools_task": ["server/tools/tools_task.py"],
    "tools_semantic": ["server/tools/tools_semantic.py"],
    "tools_security": ["server/tools/tools_security.py"],
    "tools_summary": ["server/tools/tools_summary.py"],
}

RUST_TARGETS = (
    "rust_ext/src/daemon/task_collab.rs; "
    "rust_ext/src/daemon/dispatch.rs; "
    "rust_ext/src/daemon/http_server.rs"
)


def load_matrix_methods() -> dict[str, list[dict]]:
    """按 module 分组返回 compat 方法条目。"""
    with open(f"{REPO}/{MATRIX}", encoding="utf-8") as f:
        items = json.load(f)
    if isinstance(items, dict):
        items = items.get("tools") or items.get("entries") or list(items.values())[0]
    groups: dict[str, list[dict]] = {}
    for it in items:
        if it.get("target_backend") == "python_compat":
            groups.setdefault(it["module"], []).append(it)
    return groups


def build_card(group_key: str, client_files: list[str], methods: list[dict]) -> dict:
    names = [m["name"] for m in methods]
    test_file = f"tests/test_mcp_compat_{group_key}_http_rpc.py"
    steps = [
        {
            "action": "port_rust_handler",
            "target_file": RUST_TARGETS,
            "check_items": (
                f"逐方法复刻 Python 真相源 SQL 语义（server/tools/ + db/）；"
                "dispatch handle_collab_rpc 加 arm + capability-list；"
                "http_server capability 行 backend 改 rust_native"
            ),
        },
        {
            "action": "thin_python_client",
            "target_file": "; ".join(client_files),
            "check_items": "从 _XXX_READ_ONLY_METHODS 摘除对应 _h_* 条目（双侧同删）",
        },
        {
            "action": "fixture_matrix",
            "target_file": test_file,
            "check_items": "live-daemon fixture + fail-closed 断言；success/invalid/unavailable 矩阵",
        },
        {
            "action": "matrix_verify",
            "target_file": MATRIX,
            "check_items": (
                "矩阵条目 target_backend→rust_native、batch→P0-COMPAT-v3、"
                "status→migrated；verify_route_matrix.py 全绿"
            ),
        },
    ]
    return {
        "title": f"P0-COMPAT-v3 [{group_key}]：{len(methods)} 方法 Python compat → Rust native",
        "desc": (
            "P0-compat 迁移批次 v3（planner 开卡，2026-09-09）。"
            "取代批次 v2 同名卡（v2 开卡误传 --identity-policy 触发 CLI governed 通道，"
            "跳过缺省三角色合同模板 → 无 Task/Role Contract，governance_blocked，"
            "待 supersede 收尾）。"
            f"覆盖方法（{len(methods)}）：{', '.join(names)}。"
            "流程契约：cw-task-loop（skill callwarden-mcp-card-migration 仍适用）。"
            "每方法复刻 server/tools/ + db/ 的 Python 真相源 SQL 语义；"
            "Rust handler 落 task_collab.rs（卡片级共享文件，串行执行）；"
            "退役 Python 只读注册条目；live-daemon fixture 回归；矩阵行翻转。"
            "前置备份：~/.callwarden/backup_20260909-pre-migration/（DB+codegraph+workspaces+worktree zip）。"
        ),
        "steps": steps,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--groups", default=",".join(GROUPS), help="逗号分隔的组 key")
    ap.add_argument("--workspace-instance-id", default="4baea3ff12c2ea5c")
    ap.add_argument("--workspace-id", type=int, default=1)
    args = ap.parse_args()

    methods_by_module = load_matrix_methods()
    module_to_group = {}
    for gk, files in GROUPS.items():
        for f in files:
            module_to_group[f.replace("server/tools/", "").replace(".py", "")] = gk

    # 组 → 方法（按 GROUPS 里 client 文件对应的 module 名聚合）
    group_methods: dict[str, list[dict]] = {}
    for module, methods in methods_by_module.items():
        gk = module_to_group.get(module)
        if gk is None:
            print(f"WARN: module {module} 无组归属，跳过", file=sys.stderr)
            continue
        group_methods.setdefault(gk, []).extend(methods)

    for gk in args.groups.split(","):
        gk = gk.strip()
        methods = group_methods.get(gk, [])
        if not methods:
            print(f"WARN: 组 {gk} 无 compat 方法，跳过", file=sys.stderr)
            continue
        card = build_card(gk, GROUPS[gk], methods)
        print(f"== 组 {gk}: {len(methods)} 方法")
        if args.dry_run:
            print(json.dumps(card, ensure_ascii=False, indent=2)[:600])
            continue
        # ⚠️ 不传 --identity-policy：CLI 的 governed_create 检测会把显式
        # identity_policy 视为 governed 请求并跳过缺省三角色合同模板
        # （cli/main.py task create：role_contracts 仅在 `opts.role_contracts or
        # not governed_create` 时注入），导致 daemon 收到空 role_contracts 裸建
        # 任务 → task_contract_revisions/role_contract_lineages 均不落库 →
        # next_action 规则 4 判 governance_blocked（T-1788962298.. 批次实证）。
        # 缺省（非 governed）通道由 CLI 自动注入 A′ 三角色 legacy 模板 +
        # identity_policy=legacy_identity_v1，daemon 走完整 contract bootstrap。
        cmd = [
            "C:/Python314/python.exe", "cw.py", "task", "create",
            "--title", card["title"],
            "--desc", card["desc"],
            "--steps", json.dumps(card["steps"], ensure_ascii=False),
            "--workspace-instance-id", args.workspace_instance_id,
            "--workspace-id", str(args.workspace_id),
        ]
        r = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        out = (r.stdout or "") + (r.stderr or "")
        print(out[-800:])
        if r.returncode != 0:
            print(f"FAIL: 组 {gk} 开卡失败 (rc={r.returncode})", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
