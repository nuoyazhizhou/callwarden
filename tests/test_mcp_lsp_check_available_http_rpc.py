# -*- coding: utf-8 -*-
"""MCP-059: lsp_check_available → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~058）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 lsp_check_available
3. Rust daemon 无 LSP 子进程池 → 各语言 False（Python 无 server 时 parity）
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


def test_lsp_check_available_shape(rpc):
    out = _call(rpc, "lsp_check_available", {})
    assert isinstance(out, dict)
    servers = out.get("available_servers") or {}
    assert isinstance(servers, dict)
    assert "python" in servers
    assert "typescript" in servers
    assert isinstance(out.get("total_available"), int)
    # daemon 无 LSP server → 全部 False
    for v in servers.values():
        assert v is False


def test_lsp_check_available_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("lsp_check_available", {})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "lsp_check_available", {})
    assert isinstance(out, dict)
    assert "total_available" in out
