"""MCP-031（A′ task_evidence_read）repo_map → Rust daemon native。

覆盖 task 要求：
  success（text/mermaid）/ unknown workspace、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_summary.repo_map）已从 _SUMMARY_READ_ONLY_METHODS 移除
  compat 注册，改由 Rust daemon（task_collab.rs::handle_repo_map）为权威：查跨模块
  调用边 → 生成 text/mermaid 仓库模块依赖图（返回字符串）。
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
def test_repo_map_text(live_daemon):
    """默认 text → 返回仓库模块依赖图文本。"""
    c = live_daemon
    r = c.call("repo_map", {"workspace_id": 1})
    assert isinstance(r, str)
    assert "仓库模块依赖图" in r


def test_repo_map_mermaid(live_daemon):
    """format=mermaid → 返回 graph TD 文本。"""
    c = live_daemon
    r = c.call("repo_map", {"workspace_id": 1, "format": "mermaid"})
    assert isinstance(r, str)
    assert r.startswith("graph TD")


def test_repo_map_unknown_workspace(live_daemon):
    """未知 workspace：无调用边 → 空图骨架（不报错）。"""
    c = live_daemon
    r = c.call("repo_map", {"workspace_id": 999999})
    assert isinstance(r, str)
    assert "仓库模块依赖图" in r


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_repo_map_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("repo_map", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_repo_map_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("repo_map", {"workspace_id": 1})
    assert isinstance(r, str)
