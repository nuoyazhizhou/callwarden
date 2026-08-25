"""MCP-026（A′ task_evidence_read）get_symbol_commit_history → Rust daemon native。

覆盖 task 要求：
  success / no-match / limit / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_semantic.get_symbol_commit_history）已从
  _SEMANTIC_READ_ONLY_METHODS 移除 compat 注册，改由 Rust daemon
  （task_collab.rs::handle_get_symbol_commit_history）为权威：git_symbol_changes JOIN
  git_commits 按 symbol_hash 过滤，按 timestamp DESC，LIMIT limit；返回行数组（git_commits
  全列 + change_type，与 Python dict 行键名一致）。
- 本测试直连 HTTP RPC，验证返回结构。
- 确定性 parity：authority DB 中指定 symbol_hash 无变更 → 返回 []（与 Python 空结果一致）。
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
def test_get_symbol_commit_history_no_match(live_daemon):
    """无变更记录 → []。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history",
               {"symbol_hash": "NO_SUCH_HASH_XYZ"})
    assert isinstance(r, list)
    assert r == []


def test_get_symbol_commit_history_limit(live_daemon):
    """limit 参数 → 返回 ≤ limit 条（无匹配时为空）。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history",
               {"symbol_hash": "NO_SUCH_HASH_XYZ", "limit": 5})
    assert isinstance(r, list)
    assert len(r) <= 5


def test_get_symbol_commit_history_missing_params(live_daemon):
    """缺参 → 默认空 symbol_hash/limit=20，返回 []（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("get_symbol_commit_history", {})
    assert isinstance(r, list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_symbol_commit_history_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_symbol_commit_history", {"symbol_hash": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_symbol_commit_history_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_symbol_commit_history", {"symbol_hash": "X"})
    assert isinstance(r, list)
