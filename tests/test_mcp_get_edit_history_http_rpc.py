# -*- coding: utf-8 -*-
"""MCP-050: get_edit_history → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 get_edit_history
3. 校验返回结构（id / file_path / operation / status / created_at 等）
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


def test_edit_history_all(rpc):
    out = _call(rpc, "get_edit_history", {})
    assert isinstance(out, list)
    for item in out:
        assert "id" in item
        assert "file_path" in item
        assert "operation" in item
        assert "status" in item
        assert "created_at" in item
        assert "file_hash_before" in item
        assert "file_hash_after" in item


def test_edit_history_limit(rpc):
    out = _call(rpc, "get_edit_history", {"limit": 3})
    assert isinstance(out, list)
    assert len(out) <= 3


def test_edit_history_by_path(rpc):
    out = _call(rpc, "get_edit_history", {"file_path": "NO_SUCH_FILE_xyz.py"})
    assert isinstance(out, list)


def test_edit_history_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("get_edit_history", {})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "get_edit_history", {"limit": 5})
    assert isinstance(out, list)
