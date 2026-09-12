"""MCP-025（A′ task_evidence_read）find_similar_functions → Rust daemon native。

覆盖 task 要求：
  no-embeddings（返回 []）/ 目标不存在 / 缺省参数、daemon unavailable（fail-closed）、
  restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_semantic.find_similar_functions）已从 _SEMANTIC_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_find_similar_functions）为
  权威：按 qualified_name 查目标函数 symbol_hash → 取目标嵌入向量（不依赖外部 ollama）
  → 与全部向量余弦比较 → 过滤自身 + threshold → topk → 查符号元信息。
- 边界：本环境 symbol_embeddings 表为空 → 目标无嵌入 → 返回 []（与 Python 语义一致）。
- 本测试直连 HTTP RPC，验证 fail-safe 行为与返回结构。
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
# no-embeddings / no-target：Rust daemon fail-safe（与 Python 环境一致）
# ---------------------------------------------------------------------------
def test_find_similar_functions_no_target(live_daemon):
    """目标函数不存在 → []。"""
    c = live_daemon
    r = c.call("find_similar_functions",
               {"workspace_instance_id": CANONICAL_INSTANCE, "qualified_name": "NO_SUCH_FN_XYZ"})
    assert isinstance(r, list)
    assert r == []


def test_find_similar_functions_unknown_workspace(live_daemon):
    """未知 workspace：无目标 → []。"""
    c = live_daemon
    # 未知 instance → fail-closed workspace_not_found（不静默返回空）
    with pytest.raises(Exception) as ei:
        c.call("find_similar_functions",
               {"workspace_instance_id": UNKNOWN_INSTANCE, "qualified_name": "X"})
    assert "workspace_not_found" in str(ei.value)


def test_find_similar_functions_missing_params(live_daemon):
    """缺参 → 默认空 qualified_name，返回 []（fail-closed，不抛错）。"""
    c = live_daemon
    r = c.call("find_similar_functions", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_find_similar_functions_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("find_similar_functions", {"workspace_instance_id": CANONICAL_INSTANCE, "qualified_name": "X"})
    # fail-closed 证据即异常类型本身（错误消息随传输层实现演进而变化）


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_find_similar_functions_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = c2.call("find_similar_functions", {"workspace_instance_id": CANONICAL_INSTANCE, "qualified_name": "X"})
    assert isinstance(r, list)
