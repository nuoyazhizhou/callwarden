"""MCP-030（A′ task_evidence_read）project_brief → Rust daemon native。

覆盖 task 要求：
  success（项目类型/文件/函数/行数/模块/热点函数）/ unknown workspace、
  daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_summary.project_brief）已从 _SUMMARY_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_project_brief）为权威：
  汇总项目类型（扩展名分布）、file_count、function_count、total_lines、modules 列表、
  hot_functions（行数近似）→ 返回简报字典。
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
def test_project_brief_structure(live_daemon):
    """返回结构完整：project_type/file_count/function_count/total_lines/modules/
    hot_functions/health_score/health_level/avg_complexity/comment_coverage。"""
    c = live_daemon
    r = c.call("project_brief", {"workspace_id": 1})
    assert isinstance(r, dict)
    for k in ("project_type", "file_count", "function_count", "total_lines",
              "modules", "hot_functions", "health_score", "health_level",
              "avg_complexity", "comment_coverage"):
        assert k in r
    assert isinstance(r.get("modules"), list)
    assert isinstance(r.get("hot_functions"), list)
    assert isinstance(r.get("file_count"), int)


def test_project_brief_unknown_workspace(live_daemon):
    """未知 workspace：无数据 → 结构完整（count=0）。"""
    c = live_daemon
    r = c.call("project_brief", {"workspace_id": 999999})
    assert isinstance(r, dict)
    assert r.get("file_count") == 0
    assert isinstance(r.get("modules"), list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_project_brief_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("project_brief", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_project_brief_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("project_brief", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert "project_type" in r and "modules" in r
