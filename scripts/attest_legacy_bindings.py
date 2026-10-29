#!/usr/bin/env python3
"""Legacy task binding attestation 批处理入口（Linux 多用户 / 多 workspace）。

背景：`task.create` 之前的旧任务没有 `task_workspace_bindings`，无法参与
registry owner ACL。daemon 已提供权威写点 `task.attest_legacy_workspace_binding`
（adjudicator 身份 + anchor task 的 reviewer lease + evidence），本脚本只做：

1. 只读发现指定状态下仍无 binding 的任务；
2. 以**同一 workspace 内已绑定**的 anchor task 为权威锚点；
3. 生成一次性 evidence manifest（含候选清单与发现时间）并计算 sha256；
4. 逐条调用现有 CLI（daemon 权威路径），默认 dry-run，`--apply` 才落库。

安全约束：不修改任务行，不绕过 daemon；锚点与 lease 必须由调用方显式给出，
脚本不会自动挑选 anchor，避免把 legacy 卡挂到错误 owner 的 workspace。

示例（dry-run）：
  python scripts/attest_legacy_bindings.py \
    --workspace-id 1 --workspace-instance-id 4baea3ff12c2ea5c \
    --anchor-task-id T-1793245502129-7d79c198 \
    --agent-id agent-ds4-bayes-adj --session-id sess-adj \
    --model-id deepseek-v4.1-flash --status closed

落库时追加 `--apply`，并提供 anchor 的 reviewer lease：
  --lease-token TOKEN --fencing-counter N
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


def default_db_path() -> Path:
    env = os.environ.get("CW_DAEMON_TASK_DB")
    if env:
        return Path(env)
    return Path(os.path.expanduser("~/.callwarden/callwarden.db"))


def find_unbound(db_path: Path, status: str, limit: int) -> list[str]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        query = (
            "SELECT t.id FROM tasks t WHERE NOT EXISTS "
            "(SELECT 1 FROM task_workspace_bindings b WHERE b.task_id = t.id) "
        )
        params: tuple[object, ...] = ()
        if status != "all":
            query += "AND t.status = ? "
            params = (status,)
        query += "ORDER BY t.created_at ASC LIMIT ?"
        params = params + (limit,)
        return [row[0] for row in con.execute(query, params).fetchall()]
    finally:
        con.close()


def write_evidence(evidence_dir: Path, task_ids: list[str], anchor: str) -> tuple[Path, str]:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = evidence_dir / f"attest-legacy-{stamp}.md"
    lines = [
        "# Legacy task workspace binding attestation",
        "",
        f"- generated_at: {time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"- anchor_task_id: {anchor}",
        f"- candidate_count: {len(task_ids)}",
        "",
        "## Candidates",
        "",
        *[f"- {tid}" for tid in task_ids],
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest


def attest(
    legacy: str,
    anchor: str,
    workspace_id: int,
    workspace_instance: str,
    evidence_path: Path,
    evidence_hash: str,
    lease_token: str,
    fencing_counter: int,
    identity: argparse.Namespace,
) -> int:
    request_id = f"attest-{legacy}-{int(time.time())}"
    cmd = [
        "cw", "task", "attest-legacy-workspace-binding", legacy, anchor,
        "--workspace-id", str(workspace_id),
        "--workspace-instance-id", workspace_instance,
        "--request-id", request_id,
        "--evidence-path", str(evidence_path),
        "--evidence-hash", evidence_hash,
        "--lease-token", lease_token,
        "--fencing-counter", str(fencing_counter),
        "--agent-id", identity.agent_id,
        "--session-id", identity.session_id,
        "--model-id", identity.model_id,
        "--role", "adjudicator",
    ]
    if identity.agent_instance_id:
        cmd += ["--agent-instance-id", identity.agent_instance_id]
    return subprocess.call(cmd)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, default=default_db_path())
    ap.add_argument("--workspace-id", type=int, required=True)
    ap.add_argument("--workspace-instance-id", required=True)
    ap.add_argument("--anchor-task-id", required=True)
    ap.add_argument("--status", choices=["open", "closed", "all"], default="open")
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--evidence-dir", type=Path, default=Path("deliverables"))
    ap.add_argument("--task-id", action="append", default=[],
                    help="显式指定候选任务（可重复）；给定时忽略 --status 扫描")
    ap.add_argument("--apply", action="store_true", help="实际落库（默认 dry-run）")
    ap.add_argument("--lease-token", default="")
    ap.add_argument("--fencing-counter", type=int, default=0)
    ap.add_argument("--agent-id", required=True)
    ap.add_argument("--session-id", required=True)
    ap.add_argument("--model-id", required=True)
    ap.add_argument("--agent-instance-id", default="")
    args = ap.parse_args()

    candidates = args.task_id or find_unbound(args.db, args.status, args.limit)
    candidates = [tid for tid in candidates if tid != args.anchor_task_id]
    if not candidates:
        print(f"无候选任务（status={args.status}）")
        return 0

    print(f"候选 {len(candidates)} 个：")
    for tid in candidates:
        print(f"  - {tid}")
    if not args.apply:
        print("dry-run：未调用 daemon。追加 --apply 并携带 lease 才会落库。")
        return 0
    if not args.lease_token or args.fencing_counter <= 0:
        print("--apply 需要 --lease-token 与 --fencing-counter > 0", file=sys.stderr)
        return 2

    evidence_path, evidence_hash = write_evidence(
        args.evidence_dir, candidates, args.anchor_task_id
    )
    print(f"evidence: {evidence_path} sha256={evidence_hash}")

    identity = argparse.Namespace(
        agent_id=args.agent_id,
        session_id=args.session_id,
        model_id=args.model_id,
        agent_instance_id=args.agent_instance_id,
    )
    failed: list[str] = []
    for tid in candidates:
        code = attest(
            tid, args.anchor_task_id, args.workspace_id,
            args.workspace_instance_id, evidence_path, evidence_hash,
            args.lease_token, args.fencing_counter, identity,
        )
        print(f"{tid}: {'ok' if code == 0 else f'failed(rc={code})'}")
        if code != 0:
            failed.append(tid)
    if failed:
        print(f"失败 {len(failed)} 个：{failed}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
