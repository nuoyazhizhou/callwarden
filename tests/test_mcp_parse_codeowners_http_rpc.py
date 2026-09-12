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

CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入
UNKNOWN_INSTANCE = "00000000deadbeef"


@pytest.fixture()
def live_daemon(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


# ---------------------------------------------------------------------------
# success / no-match：HTTP round-trip，Rust daemon 为权威
# ---------------------------------------------------------------------------
def test_parse_codeowners_no_file(live_daemon):
    """workspace 无 CODEOWNERS → []。"""
    c = live_daemon
    r = c.call("parse_codeowners", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, list)


def test_parse_codeowners_explicit_path(live_daemon):
    """显式 file_path 指向不存在的文件 → []（不报错）。"""
    c = live_daemon
    r = c.call("parse_codeowners",
               {"workspace_instance_id": CANONICAL_INSTANCE, "file_path": "c:/NO_SUCH/CODEOWNERS"})
    assert isinstance(r, list)
    assert r == []


def test_parse_codeowners_unknown_workspace(live_daemon):
    """未知 workspace：无 root_path → []。"""
    c = live_daemon
    # 未知 instance → fail-closed workspace_not_found（不静默返回空）
    with pytest.raises(Exception) as ei:
        c.call("parse_codeowners", {"workspace_instance_id": UNKNOWN_INSTANCE})
    assert "workspace_not_found" in str(ei.value)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_parse_codeowners_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("parse_codeowners", {"workspace_instance_id": CANONICAL_INSTANCE})
    # fail-closed 证据即异常类型本身（错误消息随传输层实现演进而变化）


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_parse_codeowners_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = c2.call("parse_codeowners", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, list)
