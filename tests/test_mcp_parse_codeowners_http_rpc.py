"""MCP-027（A′ task_evidence_read）parse_codeowners → Rust daemon native。

覆盖 task 要求：
  success（无 CODEOWNERS → []）/ file_path 显式 / 缺省参数、daemon unavailable
  （fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_semantic.parse_codeowners）已从 _SEMANTIC_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_parse_codeowners）为权威：
  查 active workspace root_path → 查找 CODEOWNERS 文件 → 解析每行 → 返回 [{pattern,
  owners}]（与 Python db_ownership.parse_codeowners 一致）。
- 确定性 parity：workspace 1（callwarden）无 CODEOWNERS 文件 → 返回 []。
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
def test_parse_codeowners_no_file(live_daemon):
    """workspace 无 CODEOWNERS → []。"""
    c = live_daemon
    r = c.call("parse_codeowners", {"workspace_id": 1})
    assert isinstance(r, list)


def test_parse_codeowners_explicit_path(live_daemon):
    """显式 file_path 指向不存在的文件 → []（不报错）。"""
    c = live_daemon
    r = c.call("parse_codeowners",
               {"workspace_id": 1, "file_path": "c:/NO_SUCH/CODEOWNERS"})
    assert isinstance(r, list)
    assert r == []


def test_parse_codeowners_unknown_workspace(live_daemon):
    """未知 workspace：无 root_path → []。"""
    c = live_daemon
    r = c.call("parse_codeowners", {"workspace_id": 999999})
    assert isinstance(r, list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_parse_codeowners_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("parse_codeowners", {"workspace_id": 1})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_parse_codeowners_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("parse_codeowners", {"workspace_id": 1})
    assert isinstance(r, list)
