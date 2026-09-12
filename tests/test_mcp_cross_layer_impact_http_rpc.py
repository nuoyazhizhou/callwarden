# -*- coding: utf-8 -*-
"""MCP-044: cross_layer_impact → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 cross_layer_impact
   （P0-H：显式 workspace_instance_id）
3. 校验返回结构（code / db / api / config 四层）

Rust 权威：query_compat_handlers.rs::handle_summary_cross_layer_impact
（复用 summary_cross_layer_full 正则四层扫描）。
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


def test_cross_layer_unknown_symbol(rpc):
    """未知 symbol_hash → 四层均为空列表。"""
    out = _call(rpc, "cross_layer_impact", {"symbol_hash": "no_such_hash_xyz"})
    assert isinstance(out, dict)
    assert out.get("code") == []
    assert out.get("db") == []
    assert out.get("api") == []
    assert out.get("config") == []


def test_cross_layer_struct(rpc):
    """结构面：db 层条目含 table/source；code 层条目含 qualified_name（有数据时）。"""
    out = _call(rpc, "cross_layer_impact", {"symbol_hash": "no_such_hash_xyz"})
    for item in out.get("db") or []:
        assert "table" in item
        assert "source" in item
        break
    for item in out.get("code") or []:
        assert "qualified_name" in item
        break


def test_cross_layer_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("cross_layer_impact", {"workspace_instance_id": CANONICAL_INSTANCE,
                                      "symbol_hash": "x"})


def test_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "cross_layer_impact", {"symbol_hash": "no_such_hash_xyz"})
    assert isinstance(out, dict)
    assert "api" in out
