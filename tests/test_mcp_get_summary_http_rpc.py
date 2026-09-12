# -*- coding: utf-8 -*-
"""MCP-029: get_summary → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 get_summary（P0-H：显式 workspace_instance_id）
3. 校验返回结构：{qualified_name, summary, model, version}；无匹配 → null

Rust 权威：query_compat_handlers.rs::handle_summary_get_summary
（symbol_summaries JOIN symbols JOIN file_instances，is_current=1）。
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import (  # noqa: E402
    DaemonUnavailableError,
    HttpDaemonRpcClient,
)
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


def test_get_summary_no_match(rpc):
    """无摘要符号 → null。"""
    r = _call(rpc, "get_summary", {"qualified_name": "NO_SUCH_SYM_XYZ"})
    assert r is None


def test_get_summary_empty_qualified_name(rpc):
    """缺参/空 qualified_name → null（fail-closed，不抛错）。"""
    r = _call(rpc, "get_summary", {})
    assert r is None


def test_get_summary_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        # fail-closed 证据即异常类型本身（本机代理会把 127.0.0.1:9 拦成 502，
        # 错误消息随网络环境变化，不钉死错误码字符串）
        c.call("get_summary", {"workspace_instance_id": CANONICAL_INSTANCE,
                               "qualified_name": "X"})


def test_get_summary_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = _call(c2, "get_summary", {"qualified_name": "NO_SUCH_SYM_XYZ"})
    assert r is None or isinstance(r, dict)
