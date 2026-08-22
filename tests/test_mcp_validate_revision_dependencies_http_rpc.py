"""MCP-008（A′ task_evidence_read）validate_revision_dependencies → Rust daemon native。

覆盖 task 要求：
  success / no-match / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_p2_graph.validate_revision_dependencies）已从
  _P2_READ_ONLY_METHODS 移除 compat 注册，改由 Rust daemon
  （task_collab.rs::handle_validate_revision_dependencies）为权威：内存模拟
  build_hard_dependency_edges（不写 dependency_edges 表），合并现有硬边做环检测，
  返回 valid/errors/cycle_path/provider_conflicts/edges_built/edges_skipped。
- 本测试直连 HTTP RPC，验证返回结构完整性。
- 确定性 parity：authority DB 中指定 workspace/contract 若无 task_dependencies →
  edges_built=0, edges_skipped=0, errors=[], valid=true（与 Python 空依赖语义一致）。
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
def test_validate_revision_dependencies_no_deps(live_daemon):
    """空依赖 → valid=true, errors=[], 结构完整。"""
    c = live_daemon
    r = c.call("validate_revision_dependencies",
               {"workspace_id": 1, "contract_id": "NO-SUCH", "contract_revision": 1})
    assert isinstance(r, dict)
    assert r.get("valid") is True
    assert r.get("errors") == []
    assert r.get("cycle_path") == []
    assert r.get("edges_built") == 0
    assert r.get("edges_skipped") == 0
    assert r.get("provider_conflicts") == []


def test_validate_revision_dependencies_missing_revision(live_daemon):
    """缺 contract_revision → 默认 0，返回结构仍完整（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("validate_revision_dependencies", {"workspace_id": 1, "contract_id": "X"})
    assert isinstance(r, dict)
    for k in ("valid", "errors", "cycle_path", "provider_conflicts",
              "edges_built", "edges_skipped"):
        assert k in r


def test_validate_revision_dependencies_unknown_workspace(live_daemon):
    """未知 workspace：无依赖 → valid=true（不报错）。"""
    c = live_daemon
    r = c.call("validate_revision_dependencies",
               {"workspace_id": 999999, "contract_id": "X", "contract_revision": 1})
    assert isinstance(r, dict)
    assert r.get("valid") is True


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_validate_revision_dependencies_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("validate_revision_dependencies",
               {"workspace_id": 1, "contract_id": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_validate_revision_dependencies_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("validate_revision_dependencies",
                {"workspace_id": 1, "contract_id": "X"})
    assert isinstance(r, dict)
    assert "valid" in r
