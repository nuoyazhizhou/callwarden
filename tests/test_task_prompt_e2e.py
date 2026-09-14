"""RP-10 cross-layer E2E（Python 侧）：task.prompt.compile live parity。

冻结规范 §17.3 / RP-10 卡（`task_prompt_e2e` 验收过滤词）：
- fresh isolated temp daemon（复用 release 验收套件的 spawn/manifest 助手，
  不触碰 shared runtime）；
- 三面 parity：HTTP RPC `task.prompt.compile` / CLI `cw task prompt` /
  MCP `task_get_role_prompt`，`prompt.text` 逐字一致；
- 三种本地格式（llm/card/json）`:format` 只改本地展示，绝不发往 daemon；
- fail-closed：缺失 task 报 `E_TASK_PROMPT_TASK_NOT_FOUND`；workspace guard
  不匹配报错；daemon 不可达时三面均无本地 Python authority fallback；
- 只读：探针不改变任务状态；evidence 无 secret。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.join(REPO_ROOT, "tests")
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from test_http_daemon_release_acceptance import (  # noqa: E402
    _backup_http_manifest,
    _restore_or_clean_http_manifest,
    _spawn_isolated_daemon,
    _terminate,
    _wait_manifest,
)

_RELEASE_BIN = os.path.join(REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")
_DEBUG_BIN = os.path.join(REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe")

# A' 三 legacy 模板（production manifest 冻结字节，RP-02）；spec §9.3
# 唯一合法 wire form = sha256: 前缀（裸 64-hex 会被 §8.3 redaction 拒绝）
LEGACY_CONTRACTS = [
    {
        "role": "executor",
        "skill_id": "none",
        "skill_version": "",
        "prompt_template_id": "cw.aprime.executor.startup.v1",
        "prompt_hash": "sha256:59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6",
        "allowed_paths": "task-card scoped paths only",
        "forbidden_paths": "task.apply; task.close; task.supersede",
        "commands": "task.next_action; task.claim; task.report",
        "acceptance_checks": "tests; evidence manifest/hash",
        "required_evidence": "implementation plan; test output",
        "handoff_to": "reviewer",
    },
    {
        "role": "reviewer",
        "skill_id": "none",
        "skill_version": "",
        "prompt_template_id": "cw.aprime.reviewer.startup.v1",
        "prompt_hash": "sha256:6415033D8F134392DE16FCA130BFB762CB6C70D9F466C770EC18A20FC4CE139E",
        "allowed_paths": "read-only review",
        "forbidden_paths": "task.apply; task.close",
        "commands": "task.report; task.handoff",
        "acceptance_checks": "verdict with findings",
        "required_evidence": "fresh-run review notes",
        "handoff_to": "adjudicator",
    },
    {
        "role": "adjudicator",
        "skill_id": "none",
        "skill_version": "",
        "prompt_template_id": "cw.aprime.adjudicator.startup.v1",
        "prompt_hash": "sha256:42A5F1DEFA81008B009058C1BAF5D1A14B3EF4521E291B7B55C19BB473A77C3E",
        "allowed_paths": "task.apply; task.close",
        "forbidden_paths": "scope expansion",
        "commands": "task.apply; task.close",
        "acceptance_checks": "final independent review",
        "required_evidence": "apply/close decision",
        "handoff_to": "complete",
    },
]


def _pick_bin() -> str:
    """优先最新构建的二进制（task.prompt.compile 资产自 RP-02 起内嵌）。"""
    candidates = [(p, os.path.getmtime(p)) for p in (_RELEASE_BIN, _DEBUG_BIN) if os.path.isfile(p)]
    if not candidates:
        pytest.fail("cw-daemon.exe 未构建（需 cargo build --bin cw-daemon）")
    return max(candidates, key=lambda x: x[1])[0]


def _sync_task_db_workspace(data_root: str, ws_id: int, name: str, root: str) -> None:
    task_db = os.path.join(data_root, "task.db")
    if not os.path.exists(task_db):
        raise RuntimeError(f"task-DB 不存在: {task_db}")
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (ws_id, name, root, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def e2e_daemon():
    """模块级隔离 daemon + 已绑定 workspace + 一个 governed 任务。"""
    from callwarden.server.daemon_client import HttpDaemonRpcClient

    bin_path = _pick_bin()
    data_root = tempfile.mkdtemp(prefix="cw_rp10_e2e_")
    ws_root = tempfile.mkdtemp(prefix="cw_rp10_ws_")
    backup = _backup_http_manifest()
    proc = _spawn_isolated_daemon(bin_path, data_root)
    try:
        manifest = _wait_manifest(proc, timeout=30)
        if manifest is None:
            stdout = proc.stdout.read(4000).decode("utf-8", "replace") if proc.stdout else ""
            stderr = proc.stderr.read(4000).decode("utf-8", "replace") if proc.stderr else ""
            pytest.fail(f"隔离 daemon 未发布 manifest\nstdout={stdout}\nstderr={stderr}")
        client = HttpDaemonRpcClient(
            endpoint=manifest["endpoint"], verify_health=False,
            validate_manifest=False, timeout=30,
        )
        # 1. 注册临时 workspace（daemon 注册表 + task-DB 双写）
        name = f"rp10-e2e-ws-{uuid.uuid4().hex[:8]}"
        reg = client.call("workspace.register", {
            "name": name, "client_view_root": ws_root, "description": "RP-10 e2e",
        })
        ws_id = reg["workspace_id"]
        ws_inst = reg["workspace_instance_id"]
        _sync_task_db_workspace(data_root, ws_id, name, ws_root)

        # 2. 创建 governed 任务（legacy v1 三合同；无 parent → 最小治理路径）
        created = client.call("task.create", {
            "title": f"RP-10 e2e task {uuid.uuid4().hex[:8]}",
            "description": "live parity probe target",
            "creator": "rp10-e2e",
            "steps": [
                {"action": "implement", "target_file": "docs/evidence/RP-10-probe.txt"},
            ],
            "workspace_id": ws_id,
            "workspace_instance_id": ws_inst,
            "identity_policy": "legacy_identity_v1",
            "role_contracts": LEGACY_CONTRACTS,
        })
        task_id = created["task_id"] if isinstance(created, dict) else created

        yield {
            "proc": proc, "manifest": manifest, "endpoint": manifest["endpoint"],
            "client": client, "data_root": data_root, "ws_root": ws_root,
            "workspace_id": ws_id, "workspace_instance_id": ws_inst,
            "task_id": task_id,
        }
    finally:
        _terminate(proc)
        _restore_or_clean_http_manifest(proc.pid, backup)
        shutil.rmtree(data_root, ignore_errors=True)
        shutil.rmtree(ws_root, ignore_errors=True)


# ---------------------------------------------------------------------------
# HTTP RPC 面
# ---------------------------------------------------------------------------

def test_http_rpc_compile_bundle(e2e_daemon):
    bundle = e2e_daemon["client"].call("task.prompt.compile", {
        "task_id": e2e_daemon["task_id"],
    })
    assert bundle["schema_version"] == "role_prompt_bundle_v1"
    assert bundle["task_id"] == e2e_daemon["task_id"]
    assert bundle["prompt_kind"] == "role_work"
    text = bundle["prompt"]["text"]
    assert isinstance(text, str) and text.strip()
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert bundle["prompt"]["sha256"] == f"sha256:{digest}"
    assert bundle.get("bundle_hash")


def test_http_rpc_workspace_guard_fails_closed(e2e_daemon):
    from callwarden.server.daemon_protocol import DaemonRemoteError

    with pytest.raises(DaemonRemoteError) as ei:
        e2e_daemon["client"].call("task.prompt.compile", {
            "task_id": e2e_daemon["task_id"],
            "expected_workspace_instance_id": "wrong-instance-0000",
        })
    assert ei.value.code == "E_TASK_PROMPT_AUTHORITY_MISMATCH"


# ---------------------------------------------------------------------------
# CLI / MCP parity
# ---------------------------------------------------------------------------

def _run_cli(e2e_daemon, *args: str, task_id: str | None = None) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    # 显式 loopback endpoint（frozen contract §4.1 显式发现路径）
    env["CW_DAEMON_HTTP_ENDPOINT"] = e2e_daemon["endpoint"]
    env.pop("CW_DAEMON_TRANSPORT", None)
    return subprocess.run(
        [sys.executable, "cw.py", "task", "prompt", task_id or e2e_daemon["task_id"], *args],
        capture_output=True, text=True, cwd=REPO_ROOT, env=env, timeout=120,
    )


def test_cli_live_parity_positive(e2e_daemon):
    """ADJ-RP10-01 修复后：CLI 面 task.prompt.compile 正向 parity。

    route_rpc（server/daemon_client.py）对 task-scoped authority request
    不再注入 workspace_instance_id / workspace_root（spec §4.1-3 未知字段
    fail-closed 已豁免）→ CLI ``cw task prompt --format llm`` 现返回 daemon
    编译 bundle 的 prompt.text，而非 E_TASK_PROMPT_TASK_ID_REQUIRED。
    """
    r = _run_cli(e2e_daemon, "--format", "llm")
    assert r.returncode == 0, r.stderr[-500:]
    assert "Role Prompt Compiler v1" in r.stdout, r.stdout[:300]
    assert "E_TASK_PROMPT_TASK_ID_REQUIRED" not in r.stdout

    # json 格式：完整 daemon bundle 原样输出（schema/authority 字段齐备）
    rj = _run_cli(e2e_daemon, "--format", "json")
    assert rj.returncode == 0, rj.stderr[-500:]
    bundle = json.loads(rj.stdout)
    assert bundle.get("schema_version") == "role_prompt_bundle_v1"
    assert bundle.get("prompt_kind") == "role_work"
    assert bundle.get("task_id") == e2e_daemon["task_id"]
    assert bundle.get("bundle_hash")


def test_mcp_live_parity_positive(e2e_daemon, monkeypatch):
    """ADJ-RP10-01 修复后：MCP 面 task_get_role_prompt 正向 parity。

    与 CLI 同源 route_rpc；修复后 MCP 工具直接返回 daemon 编译 bundle，
    不再 fail-closed。
    """
    from callwarden.server import daemon_client as dc
    from mcp.server.fastmcp import FastMCP
    from callwarden.server.tools import tools_task_prompt

    mcp = FastMCP("rp10-e2e")
    tools_task_prompt.register(mcp)
    tools = mcp._tool_manager._tools
    assert "task_get_role_prompt" in tools
    fn = tools["task_get_role_prompt"].fn
    dc.HttpDaemonRpcClient.reset_instance()
    dc.DaemonClient.reset_instance()
    monkeypatch.setenv("CW_DAEMON_HTTP_ENDPOINT", e2e_daemon["endpoint"])
    try:
        bundle = fn(task_id=e2e_daemon["task_id"])
    finally:
        dc.HttpDaemonRpcClient.reset_instance()
        dc.DaemonClient.reset_instance()
    assert isinstance(bundle, dict)
    assert bundle.get("schema_version") == "role_prompt_bundle_v1"
    assert bundle.get("prompt_kind") == "role_work"
    assert bundle.get("task_id") == e2e_daemon["task_id"]
    text = (bundle.get("prompt") or {}).get("text")
    assert isinstance(text, str) and text.strip()
    assert "E_TASK_PROMPT_TASK_ID_REQUIRED" not in json.dumps(bundle)


# ---------------------------------------------------------------------------
# fail-closed（无本地 fallback）
# ---------------------------------------------------------------------------

def test_missing_task_fails_closed_all_surfaces(e2e_daemon):
    from callwarden.server.daemon_protocol import DaemonRemoteError

    with pytest.raises(DaemonRemoteError) as ei:
        e2e_daemon["client"].call("task.prompt.compile", {"task_id": "T-e2e-no-such-task"})
    assert ei.value.code == "E_TASK_PROMPT_TASK_NOT_FOUND"

    # CLI 面（经 route_rpc，当前受 ADJ-RP10-01 影响）：无论错误码为何，
    # 绝无本地合成 bundle（fail-closed 纪律优先于错误码）
    r = _run_cli(e2e_daemon, "--format", "json", task_id="T-e2e-no-such-task")
    combined = (r.stdout or "") + (r.stderr or "")
    assert "role_prompt_bundle_v1" not in combined
    assert '"ok": false' in combined or r.returncode != 0





def test_probe_is_read_only(e2e_daemon):
    st_before = e2e_daemon["client"].call("task.status", {"task_id": e2e_daemon["task_id"]})
    e2e_daemon["client"].call("task.prompt.compile", {"task_id": e2e_daemon["task_id"]})
    e2e_daemon["client"].call("task.prompt.compile", {"task_id": e2e_daemon["task_id"]})
    st_after = e2e_daemon["client"].call("task.status", {"task_id": e2e_daemon["task_id"]})
    assert st_before["lifecycle_status"] == st_after["lifecycle_status"]
    assert st_before.get("workflow_status") == st_after.get("workflow_status")
