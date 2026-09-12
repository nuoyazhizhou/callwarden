# -*- coding: utf-8 -*-
"""MCP-038: blast_radius → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 blast_radius（P0-H：显式 workspace_instance_id）
3. 校验返回结构：{source_symbol, source_hash, depth, layers, total_impacted,
   by_layer{code,db,api,config}}；源符号不存在 → 空结构。

Rust 权威：query_compat_handlers.rs::handle_summary_blast_radius
（复用 security_blast_radius_sql BFS）。
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


def test_blast_radius_unknown_symbol(rpc):
    """源符号不存在 → 空结构。"""
    r = _call(rpc, "blast_radius", {"symbol_hash": "NO_SUCH_HASH_038"})
    assert isinstance(r, dict)
    assert r["total_impacted"] == 0
    assert r["layers"] == []
    assert r["by_layer"] == {"code": 0, "db": 0, "api": 0, "config": 0}
    assert r["source_hash"] == "NO_SUCH_HASH_038"


def test_blast_radius_custom_depth(rpc):
    """自定义 depth → 结构正确。"""
    r = _call(rpc, "blast_radius", {"symbol_hash": "NO_SUCH_HASH_038", "depth": 5})
    assert isinstance(r, dict)
    assert r["depth"] == 5


def test_blast_radius_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        c.call("blast_radius", {"workspace_instance_id": CANONICAL_INSTANCE,
                                "symbol_hash": "H-X"})


def test_blast_radius_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = _call(c2, "blast_radius", {"symbol_hash": "NO_SUCH_HASH_038"})
    assert isinstance(r, dict) and "by_layer" in r
