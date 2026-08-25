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

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError

A_PRIME_EPIC = "T-1787293451688-c14b1e44"
CLI_02_TASK = "T-1787321708568-d292ab3c"
WS_ID = 1


@pytest.fixture()
def live_daemon():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行，跳过 HTTP RPC round-trip 用例")
    return c


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


def test_http_rpc_task_daemon_unavailable_fail_closed():
    from callwarden.config import get_http_authority_id
    c = HttpDaemonRpcClient(
        endpoint="http://127.0.0.1:9", authority_id=get_http_authority_id()
    )
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("task.list", {"status": "", "limit": 5, "workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)
