# -*- coding: utf-8 -*-
"""MCP-039: ask_codebase → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 ask_codebase
   （P0-H：显式 workspace_instance_id）
3. 校验返回结构（question / seed_functions / context_blocks / rag_context /
   estimated_tokens / truncated / metadata）

Rust 权威：query_compat_handlers.rs::handle_summary_ask_codebase
（Rust daemon 无 embedder → keyword_fallback 路径）。
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402
from callwarden.config import get_http_authority_id


@pytest.fixture()
def rpc(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入


def _call(rpc, method, params):
    params = dict(params)
    params.setdefault("workspace_instance_id", CANONICAL_INSTANCE)
    return rpc.call(method, params)


def test_ask_codebase_empty_question(rpc):
    """空问题 → 零结果结构完整（fallback=no_results）。"""
    out = _call(rpc, "ask_codebase", {"question": ""})
    assert isinstance(out, dict)
    assert out.get("question") == ""
    assert out.get("seed_functions") == []
    assert isinstance(out.get("context_blocks"), list)
    assert isinstance(out.get("rag_context"), str)
    assert out.get("rag_context") == ""
    meta = out["metadata"]
    assert meta["total_functions_included"] == 0
    assert meta["fallback_used"] == "no_results"


def test_ask_codebase_keyword_search(rpc):
    """关键词检索 → keyword_fallback 返回种子（Rust 无 embedder）。

    注：query 归一化与 Python parity（仅替换 ','/'.'，不拆下划线），
    下划线连接词不会命中（no_results），必须用空格分词。
    """
    out = _call(rpc, "ask_codebase", {"question": "handle task apply", "top_k": 3})
    assert isinstance(out, dict)
    seeds = out.get("seed_functions") or []
    assert isinstance(seeds, list)
    assert seeds, "keyword_fallback 应命中种子函数"
    for s in seeds:
        assert "qualified_name" in s
        assert "file_path" in s
        assert "similarity" in s
    assert out["metadata"]["fallback_used"] == "keyword_fallback"


def test_ask_codebase_rag_context_shape(rpc):
    """rag_context / estimated_tokens / truncated 结构。"""
    out = _call(rpc, "ask_codebase", {"question": "task", "top_k": 2,
                                      "include_callers": 1, "include_callees": 1})
    rag = out.get("rag_context") or ""
    assert isinstance(rag, str)
    assert out.get("estimated_tokens", 0) >= 0
    assert isinstance(out.get("truncated"), bool)
    if rag:
        assert "# 问题" in rag


def test_ask_codebase_daemon_unavailable_fail_closed():
    """daemon 不可达 → 连接级错误（不回退、不伪装业务错误）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("ask_codebase", {"workspace_instance_id": CANONICAL_INSTANCE,
                                "question": "x"})


def test_new_client_instance_stable(rpc):
    """新 client 实例（同 manifest）调用不报错、结果结构稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "ask_codebase", {"question": "guardrail", "top_k": 2})
    assert isinstance(out, dict)
    assert isinstance(out.get("context_blocks"), list)
