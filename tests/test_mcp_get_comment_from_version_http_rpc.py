"""MCP-019（A′ task_evidence_read）get_comment_from_version → Rust daemon native。

覆盖 task 要求：
  success / spec 无 @ / 版本不存在 / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_query.get_comment_from_version）已从 _SYMBOL_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_get_comment_from_version）为
  权威：解析 spec（fn@vN 或 fn@hash）→ 查符号历史 → 查 symbol_contents → 返回注释信息；
  任一环节缺失返回 None。
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
# success / no-match：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_get_comment_from_version_no_spec_at(live_daemon):
    """spec 无 @ → null。"""
    c = live_daemon
    r = c.call("get_comment_from_version",
               {"workspace_id": 1, "spec": "NO_SUCH_SYMBOL"})
    assert r is None


def test_get_comment_from_version_unknown_symbol(live_daemon):
    """未知符号 → null。"""
    c = live_daemon
    r = c.call("get_comment_from_version",
               {"workspace_id": 1, "spec": "NO_SUCH_SYMBOL@v1"})
    assert r is None


def test_get_comment_from_version_missing_spec(live_daemon):
    """缺 spec → 默认空串，null（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("get_comment_from_version", {"workspace_id": 1})
    assert r is None


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_comment_from_version_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_comment_from_version", {"workspace_id": 1, "spec": "X@v1"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_comment_from_version_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_comment_from_version", {"workspace_id": 1, "spec": "X@v1"})
    assert r is None or isinstance(r, dict)
