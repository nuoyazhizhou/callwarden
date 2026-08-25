# -*- coding: utf-8 -*-
"""MCP-039: ask_codebase → Rust daemon native 的 HTTP RPC 往返测试。

与 MCP-002/003/004/005/033/034/035/036/037/038 相同的 live-daemon HTTP 往返模式：
1. 启动/复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. 用 cw 包内 HttpDaemonRpcClient 走 /v1/rpc 调用 ask_codebase
3. 校验返回结构（question / seed_functions / context_blocks / rag_context /
   estimated_tokens / truncated / metadata）
"""

import json
import os
import subprocess
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402


@pytest.fixture(scope="module")
def rpc():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c


def _call(rpc, method, params):
    """与 daemon_client 相同信封：{"jsonrpc":"2.0","id":..,"method":..,"params":..}"""
    return rpc.call(method, params)


def test_ask_codebase_empty_question(rpc):
    out = _call(rpc, "ask_codebase", {"question": "", "workspace_id": 1})
    assert isinstance(out, dict)
    assert out.get("question") == ""
    assert isinstance(out.get("seed_functions"), list)
    assert isinstance(out.get("context_blocks"), list)
    assert isinstance(out.get("rag_context"), str)
    assert "metadata" in out
    meta = out["metadata"]
    assert "total_functions_included" in meta
    assert "fallback_used" in meta


def test_ask_codebase_keyword_search(rpc):
    # 关键词较长避免误匹配；期望走 keyword_fallback 返回种子
    out = _call(rpc, "ask_codebase", {"question": "handle_task_apply", "workspace_id": 1, "top_k": 3})
    assert isinstance(out, dict)
    seeds = out.get("seed_functions") or []
    assert isinstance(seeds, list)
    for s in seeds:
        assert "qualified_name" in s
        assert "file_path" in s
    # 种子非空时 metadata.has_vector_index 应为 False（Rust daemon 无 embedder → fallback）
    if seeds:
        assert out["metadata"]["fallback_used"] in ("keyword_fallback", "")


def test_ask_codebase_rag_context_shape(rpc):
    out = _call(
        rpc,
        "ask_codebase",
        {"question": "task", "workspace_id": 1, "top_k": 2, "include_callers": 1, "include_callees": 1},
    )
    rag = out.get("rag_context") or ""
    assert isinstance(rag, str)
    assert out.get("estimated_tokens", 0) >= 0
    assert isinstance(out.get("truncated"), bool)
    # 有内容时 rag_context 应包含问题头
    if rag:
        assert "# 问题" in rag


def test_ask_codebase_unknown_workspace(rpc):
    out = _call(rpc, "ask_codebase", {"question": "handle_task_apply", "workspace_id": 999999})
    assert isinstance(out, dict)
    assert out.get("seed_functions") == []
    assert out.get("rag_context") == ""
    assert out["metadata"]["total_functions_included"] == 0


def test_ask_codebase_daemon_unavailable_fail_closed():
    """daemon 不可达时应抛连接级错误（不回退、不伪装业务错误）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("ask_codebase", {"question": "x", "workspace_id": 1})


def test_new_client_instance_stable(rpc):
    """新 client 实例（同 manifest）调用不报错、结果结构稳定。"""
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "ask_codebase", {"question": "guardrail", "workspace_id": 1, "top_k": 2})
    assert isinstance(out, dict)
    assert isinstance(out.get("context_blocks"), list)
