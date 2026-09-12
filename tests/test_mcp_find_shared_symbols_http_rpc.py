# -*- coding: utf-8 -*-
"""MCP-051: find_shared_symbols → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~050）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 find_shared_symbols
3. 校验返回结构（total_shared / shared_symbols）
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


def test_find_shared_all(rpc):
    out = _call(rpc, "find_shared_symbols", {})
    assert isinstance(out, dict)
    assert isinstance(out.get("total_shared"), int)
    assert isinstance(out.get("shared_symbols"), list)
    for s in out["shared_symbols"]:
        assert "content_hash" in s
        assert "workspace_a" in s
        assert "workspace_b" in s
        assert "qualified_name_a" in s
        assert "qualified_name_b" in s


def test_find_shared_with_a(rpc):
    out = _call(rpc, "find_shared_symbols", {"workspace_a": "NO_SUCH_WS"})
    assert isinstance(out, dict)
    assert out.get("total_shared") == 0
    assert out.get("shared_symbols") == []


def test_find_shared_with_a_and_b(rpc):
    branches = _call(rpc, "list_branches", {})
    if len(branches) < 2:
        pytest.skip("不足两个 workspace，跳过")
    out = _call(rpc, "find_shared_symbols", {"workspace_a": branches[0]["name"], "workspace_b": branches[1]["name"]})
    assert isinstance(out, dict)
    assert "total_shared" in out


def test_find_shared_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("find_shared_symbols", {})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "find_shared_symbols", {})
    assert isinstance(out, dict)
