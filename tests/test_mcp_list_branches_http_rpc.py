# -*- coding: utf-8 -*-
"""MCP-048: list_branches → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~047）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 list_branches
3. 校验返回结构（id / name / root_path / created_at / symbol_count）
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


def test_list_branches_shape(rpc):
    out = _call(rpc, "list_branches", {})
    assert isinstance(out, list)
    for item in out:
        assert "id" in item
        assert "name" in item
        assert "root_path" in item
        assert "created_at" in item
        assert "symbol_count" in item
    # 按 id 升序
    ids = [x.get("id", 0) for x in out]
    assert ids == sorted(ids)


def test_list_branches_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("list_branches", {})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "list_branches", {})
    assert isinstance(out, list)
