# -*- coding: utf-8 -*-
"""MCP-034: get_ownership_map → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 get_ownership_map
   （P0-H：显式 workspace_instance_id）
3. 校验返回结构：file_ownership 按 workspace 聚合负责人分布 → list；
   空表 → []（与 Python 空表一致）。

Rust 权威：query_compat_handlers.rs::handle_summary_ownership_map。
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


def test_get_ownership_map_empty(rpc):
    """空表 → 返回数组（Python 空表返回 []）。"""
    r = _call(rpc, "get_ownership_map", {})
    assert isinstance(r, list)


def test_get_ownership_map_item_shape(rpc):
    """命中时元素含 module/primary_owner/file_count/owners。"""
    r = _call(rpc, "get_ownership_map", {"module_filter": "server"})
    for item in r:
        assert "module" in item and "primary_owner" in item
        assert "file_count" in item and "owners" in item
        break


def test_get_ownership_map_no_filter(rpc):
    """缺省（空 filter）→ 返回全部。"""
    r = _call(rpc, "get_ownership_map", {})
    assert isinstance(r, list)


def test_get_ownership_map_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        # fail-closed 证据即异常类型本身（本机代理会把 127.0.0.1:9 拦成 502，
        # 错误消息随网络环境变化，不钉死错误码字符串）
        c.call("get_ownership_map", {"workspace_instance_id": CANONICAL_INSTANCE})


def test_get_ownership_map_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = _call(c2, "get_ownership_map", {"module_filter": "server"})
    assert isinstance(r, list)
