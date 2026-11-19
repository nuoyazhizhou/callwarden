"""CLI-03 (A′ control_plane) `cw task show/list/status-tree` HTTP RPC fixture 矩阵。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_03_http_rpc.py）
要求的 HTTP RPC 层场景：
  success（live daemon + workspace_id 注入的 HTTP round-trip）、
  authority failure（伪造 workspace_id 被 scope 拒绝，返回空列表）、
  daemon unavailable（端点不可达 fail-closed E_HTTP_DAEMON_UNAVAILABLE）。

与 test_cli03_task_read_authority.py 互补：本文件聚焦 HTTP RPC 传输层与
authority 边界；那文件覆盖只读不变异与 status_tree 结构。两者共同证明
Rust daemon（task_collab.rs）为 task 只读查询权威、Python 无 SQLite fallback。
"""

import os
import shutil
import sqlite3
import subprocess
import time

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

A_PRIME_EPIC = "T-1787293451688-c14b1e44"
CLI_02_TASK = "T-1787321708568-d292ab3c"
WS_ID = 1
SUBTASK_COUNT = 165  # 生产 A′ 有 160+ 直接子任务；隔离夹具按 165 对齐规模断言


def _seed_read_authority(task_db):
    """seed 只读任务权威：2 张有 binding 的卡 + A′ 的 165 个直接子任务。

    `task.status` / `task.status_tree` 经 `read_task_bound_workspace` 反查
    `task_workspace_bindings`（无 binding → E_TASK_BINDING_MISSING fail-closed）；
    `task.list` 直接 JOIN 该表过滤 workspace。故父卡必须有 binding，子任务仅按
    parent_id 递归读取，无需 binding。不 seed `workspace_authority_captures`：
    owner ACL 在无 capture 时放行，避免夹具伪造真实用户的 registry 身份。
    """
    now = time.time()
    conn = sqlite3.connect(task_db, timeout=10)
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO tasks "
            "(id, title, description, creator, status, created_at, updated_at, parent_id)"
            " VALUES (?, ?, '', 'cli03-fixture', ?, ?, ?, ?)",
            [
                (CLI_02_TASK, "CLI-02 fixture task", "open", now, now, ""),
                (A_PRIME_EPIC, "A' epic fixture", "open", now - 1, now - 1, ""),
            ]
            + [
                (f"T-CLI03-SUB-{i:03d}", f"A' subtask {i}", "open", now - 2, now - 2,
                 A_PRIME_EPIC)
                for i in range(SUBTASK_COUNT)
            ],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO task_workspace_bindings "
            "(task_id, workspace_id, workspace_binding_id, workspace_capture_id,"
            " created_by, authoritative_created_at)"
            " VALUES (?, ?, ?, ?, 'cli03-fixture', ?)",
            [
                (tid, WS_ID, f"tb-{tid}", f"wc-{tid}", now)
                for tid in (CLI_02_TASK, A_PRIME_EPIC)
            ],
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def live_daemon(tmp_path_factory):
    """隔离 HTTPS daemon + 同源 mTLS 身份 + seeded task 权威。

    不再依赖本机常驻 daemon（旧实现 `HttpDaemonRpcClient().health()` 在测试进程
    内发现不到生产 endpoint → 4 条 round-trip 长期 skip）。前置缺失（二进制/PKI/
    端口冲突）仍由 `setup_w3_client` 归一为 `None` 后 skip，其余异常保持红。
    """
    from _w3_harness import restore_w3_env, setup_w3_client

    prev_pki = os.environ.pop("CW_PKI_DIR", None)
    tmp = str(tmp_path_factory.mktemp("cli03_rpc"))
    proc = None
    try:
        client, _instance_id, _endpoint, proc = setup_w3_client(
            tmp, _REPO_ROOT, ws_id=WS_ID
        )
        if client is None:
            pytest.skip("隔离 HTTPS daemon 前置不可用（二进制/PKI/端口）")
        _seed_read_authority(os.path.join(tmp, "task.db"))
        yield client
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
        raise
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)
        restore_w3_env()
        if prev_pki is not None:
            os.environ["CW_PKI_DIR"] = prev_pki


def test_http_rpc_task_status_success(live_daemon):
    c = live_daemon
    r = c.call("task.status", {"task_id": CLI_02_TASK})
    assert isinstance(r, dict)
    assert r.get("task_id") == CLI_02_TASK
    assert r["status"] in ("open", "in_progress", "review", "applied", "closed", "blocked")


def test_http_rpc_task_list_success(live_daemon):
    c = live_daemon
    r = c.call("task.list", {"status": "", "limit": 200, "workspace_id": WS_ID})
    assert isinstance(r, dict)
    ids = [(t.get("task_id") or t.get("id")) for t in (r.get("tasks") or [])]
    assert CLI_02_TASK in ids


def test_http_rpc_task_status_tree_success(live_daemon):
    c = live_daemon
    r = c.call("task.status_tree", {"task_id": A_PRIME_EPIC})
    assert isinstance(r, dict)
    assert r.get("task_id") == A_PRIME_EPIC
    assert len(r.get("subtasks") or []) >= 160


def test_http_rpc_task_authority_failure(live_daemon):
    """伪造 workspace_id：authority-scoped fail-closed，未知 workspace 返回空列表。"""
    c = live_daemon
    r = c.call("task.list", {"status": "", "limit": 5, "workspace_id": 999999})
    tasks = r.get("tasks") if isinstance(r, dict) else (r or [])
    assert tasks == []


def test_http_rpc_task_daemon_unavailable_fail_closed(tmp_path, monkeypatch):
    """端点不可达 → fail-closed E_HTTP_DAEMON_UNAVAILABLE（禁止回落本地 SQLite）。

    ADR-002 单轨 mTLS 后客户端会先解析身份；本机无真实 PKI 时该步会以
    「mTLS 客户端证书缺失」提前 fail-closed，掩盖传输层用例。故显式提供
    一套自签测试材料，使断言落在真正的「TCP 连接被拒绝」路径上。
    """
    import test_mtls_control_plane as _mtls  # noqa: PLC0415

    tls = _mtls.make_fake_tls_material(str(tmp_path / "tls"))
    monkeypatch.setenv("CW_CLIENT_CERT", tls["client_cert"])
    monkeypatch.setenv("CW_CLIENT_KEY", tls["client_key"])
    monkeypatch.setenv("CW_CA_CERT", tls["ca_cert"])
    monkeypatch.delenv("CW_PKI_DIR", raising=False)

    from callwarden.config import get_http_authority_id
    c = HttpDaemonRpcClient(
        endpoint="https://127.0.0.1:9", authority_id=get_http_authority_id()
    )
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("task.list", {"status": "", "limit": 5, "workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)
