# -*- coding: utf-8 -*-
"""MCP-044: cross_layer_impact → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~043）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 cross_layer_impact
3. 校验返回结构（code / db / api / config 四层）
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


def test_cross_layer_unknown_symbol(rpc):
    out = _call(rpc, "cross_layer_impact", {"symbol_hash": "no_such_hash_xyz"})
    assert isinstance(out, dict)
    assert isinstance(out.get("code"), list)
    assert isinstance(out.get("db"), list)
    assert isinstance(out.get("api"), list)
    assert isinstance(out.get("config"), list)


def test_cross_layer_known_symbol(rpc):
    out = _call(rpc, "cross_layer_impact", {"symbol_hash": "handle_task_apply"})
    assert isinstance(out, dict)
    assert "code" in out and "db" in out and "api" in out and "config" in out
    # db 层条目结构
    for item in out.get("db") or []:
        assert "table" in item
        assert "source" in item
    # code 层条目结构
    for item in out.get("code") or []:
        assert "qualified_name" in item


def test_cross_layer_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("cross_layer_impact", {"symbol_hash": "x"})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "cross_layer_impact", {"symbol_hash": "handle_task_apply"})
    assert isinstance(out, dict)
    assert "api" in out
