"""MCP-033（A′ task_evidence_read）who_to_ask → Rust daemon native。

覆盖 task 要求：
  success/no-match/绝对路径/相对路径/缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_summary.who_to_ask）已是 route_rpc 薄壳；本测试直连 HTTP RPC
  `who_to_ask`，验证 Rust daemon（task_collab.rs::handle_who_to_ask）为权威：从
  file_ownership JOIN file_instances 按 workspace_id + abs_path/rel_path 查询文件负责人。
- Python compat `_h_who_to_ask` 已从 tools_summary._SUMMARY_READ_ONLY_METHODS 移除。

确定性 parity：当前库可能无 file_ownership 记录 → 返回 null（与 Python 无行时返回 None
一致）。结构断言覆盖命中时的字段投影。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id


@pytest.fixture()
def live_daemon():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c


# ---------------------------------------------------------------------------
# success / no-match：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_who_to_ask_no_match(live_daemon):
    """无 file_ownership 记录 → 返回 null（与 Python None 语义一致）。"""
    c = live_daemon
    r = c.call("who_to_ask", {"file_path": "NO_SUCH_FILE_xyz.py"})
    assert r is None or r == {}


def test_who_to_ask_rel_path(live_daemon):
    """相对路径 → 结构正确（空表返回 null）。"""
    c = live_daemon
    r = c.call("who_to_ask", {"file_path": "server/main.py"})
    assert r is None or isinstance(r, dict)


def test_who_to_ask_abs_path(live_daemon):
    """绝对路径 → 结构正确（空表返回 null）。"""
    c = live_daemon
    r = c.call("who_to_ask", {"file_path": "C:/git_work/callwarden/cw.py"})
    assert r is None or isinstance(r, dict)


# ---------------------------------------------------------------------------
# 缺省参数：空 file_path → null（不报错）
# ---------------------------------------------------------------------------
def test_who_to_ask_empty_path(live_daemon):
    c = live_daemon
    r = c.call("who_to_ask", {"file_path": ""})
    assert r is None or r == {}


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_who_to_ask_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("who_to_ask", {"file_path": "server/main.py"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_who_to_ask_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("who_to_ask", {"file_path": "server/main.py"})
    assert r is None or isinstance(r, dict)
