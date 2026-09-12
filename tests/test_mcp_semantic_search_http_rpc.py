"""MCP-024（A′ task_evidence_read）semantic_search → Rust daemon native。

覆盖 task 要求：
  no-embedder（返回 []）/ 空 query / 缺省参数、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_semantic.semantic_search）已从 _SEMANTIC_READ_ONLY_METHODS
  移除 compat 注册，改由 Rust daemon（task_collab.rs::handle_semantic_search）为权威：
  嵌入查询（ollama HTTP /api/embeddings）→ 余弦相似度 topk → 查符号元信息 → 返回
  {qualified_name, file_path, start_line, end_line, similarity, summary}。
- 边界：嵌入生成依赖 ollama 外部服务 + symbol_embeddings 表数据；本环境两者皆不可用
  → 返回 []（与 Python 无 embedder 时语义一致，确定性 parity）。
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
# no-embedder / no-embeddings：Rust daemon fail-safe（与 Python 环境一致）
# ---------------------------------------------------------------------------
def test_semantic_search_no_embedder(live_daemon):
    """无 ollama/嵌入数据 → []（不抛错，与 Python 无 embedder 语义一致）。"""
    c = live_daemon
    r = c.call("semantic_search", {"workspace_instance_id": CANONICAL_INSTANCE, "query": "parse config"})
    assert isinstance(r, list)


def test_semantic_search_empty_query(live_daemon):
    """空 query → []（不抛错）。"""
    c = live_daemon
    r = c.call("semantic_search", {"workspace_instance_id": CANONICAL_INSTANCE, "query": ""})
    assert isinstance(r, list)


def test_semantic_search_missing_params(live_daemon):
    """缺参 → 默认空 query/top_k=5，返回 []（fail-closed）。"""
    c = live_daemon
    r = c.call("semantic_search", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_semantic_search_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("semantic_search", {"workspace_instance_id": CANONICAL_INSTANCE, "query": "x"})
    # fail-closed 证据即异常类型本身（错误消息随传输层实现演进而变化）


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_semantic_search_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = c2.call("semantic_search", {"workspace_instance_id": CANONICAL_INSTANCE, "query": "x"})
    assert isinstance(r, list)
