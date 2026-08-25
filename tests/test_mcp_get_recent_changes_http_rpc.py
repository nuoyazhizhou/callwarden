"""MCP-017（A′ task_evidence_read）get_recent_changes → Rust daemon native。

覆盖 task 要求：
  success / 缺省 since / since 解析、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_query.get_recent_changes）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_recent_changes）为
  权威：解析 since（1h/30m/1d/2h30m）→ 查 changed_files（parsed_at > cutoff）→ 对比
  前后版本符号 hash 计算 change_type → 返回 {changed_files, changed_functions,
  since_seconds}。
- 本测试直连 HTTP RPC，验证返回结构。
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
# success：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_get_recent_changes_structure(live_daemon):
    """返回结构完整：changed_files / changed_functions / since_seconds。"""
    c = live_daemon
    r = c.call("get_recent_changes", {"workspace_id": 1, "since": "1d"})
    assert isinstance(r, dict)
    assert isinstance(r.get("changed_files"), list)
    assert isinstance(r.get("changed_functions"), list)
    assert r.get("since_seconds") == 86400


def test_get_recent_changes_default_since(live_daemon):
    """缺 since → 默认 1d（since_seconds=86400）。"""
    c = live_daemon
    r = c.call("get_recent_changes", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert r.get("since_seconds") == 86400


def test_get_recent_changes_since_parse(live_daemon):
    """since=2h30m → since_seconds=9000。"""
    c = live_daemon
    r = c.call("get_recent_changes", {"workspace_id": 1, "since": "2h30m"})
    assert isinstance(r, dict)
    assert r.get("since_seconds") == 9000


def test_get_recent_changes_unknown_workspace(live_daemon):
    """未知 workspace：无数据 → changed_files=[]（不报错）。"""
    c = live_daemon
    r = c.call("get_recent_changes", {"workspace_id": 999999, "since": "1d"})
    assert isinstance(r, dict)
    assert r.get("changed_files") == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_recent_changes_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_recent_changes", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_recent_changes_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_recent_changes", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert "changed_files" in r and "changed_functions" in r
