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


@pytest.fixture(scope="module")
def rpc():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c


def _call(rpc, method, params):
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
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "find_shared_symbols", {})
    assert isinstance(out, dict)
