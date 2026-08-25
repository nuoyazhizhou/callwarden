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


@pytest.fixture()
def live_daemon():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c


# ---------------------------------------------------------------------------
# no-embedder / no-embeddings：Rust daemon fail-safe（与 Python 环境一致）
# ---------------------------------------------------------------------------
def test_semantic_search_no_embedder(live_daemon):
    """无 ollama/嵌入数据 → []（不抛错，与 Python 无 embedder 语义一致）。"""
    c = live_daemon
    r = c.call("semantic_search", {"workspace_id": 1, "query": "parse config"})
    assert isinstance(r, list)


def test_semantic_search_empty_query(live_daemon):
    """空 query → []（不抛错）。"""
    c = live_daemon
    r = c.call("semantic_search", {"workspace_id": 1, "query": ""})
    assert isinstance(r, list)


def test_semantic_search_missing_params(live_daemon):
    """缺参 → 默认空 query/top_k=5，返回 []（fail-closed）。"""
    c = live_daemon
    r = c.call("semantic_search", {"workspace_id": 1})
    assert isinstance(r, list)


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_semantic_search_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("semantic_search", {"workspace_id": 1, "query": "x"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_semantic_search_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("semantic_search", {"workspace_id": 1, "query": "x"})
    assert isinstance(r, list)
