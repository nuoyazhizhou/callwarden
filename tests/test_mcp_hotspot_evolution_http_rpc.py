# -*- coding: utf-8 -*-
"""MCP-046: hotspot_evolution → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 hotspot_evolution
   （P0-H：显式 workspace_instance_id）
3. 校验返回结构（hotspot_score / change_count / defect_count / complexity /
   label），按 hotspot_score 降序。

Rust 权威：query_compat_handlers.rs::handle_summary_hotspot_evolution。
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


def test_hotspot_all(rpc):
    """全量热点：按 hotspot_score 降序 + 元素字段投影。"""
    out = _call(rpc, "hotspot_evolution", {})
    assert isinstance(out, list)
    assert out, "canonical workspace 应有热点数据"
    scores = [x.get("hotspot_score", 0) for x in out]
    assert scores == sorted(scores, reverse=True)
    for item in out:
        assert "qualified_name" in item
        assert "symbol_hash" in item
        assert "module_path" in item
        assert "hotspot_score" in item
        assert "change_count" in item
        break


def test_hotspot_module_filter(rpc):
    """module_filter 过滤 → 命中项 module_path 前缀匹配。"""
    out = _call(rpc, "hotspot_evolution", {"module_filter": "lib::rust_ext"})
    assert isinstance(out, list)
    for item in out:
        mp = str(item.get("module_path", ""))
        assert mp.startswith("lib::rust_ext") or mp == ""


def test_hotspot_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("hotspot_evolution", {"workspace_instance_id": CANONICAL_INSTANCE})


def test_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "hotspot_evolution", {"module_filter": ""})
    assert isinstance(out, list)
