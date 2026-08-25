# -*- coding: utf-8 -*-
"""MCP-057: lsp_diagnostics → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~056）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 lsp_diagnostics
3. Rust daemon 无 LSP 子进程池 → 降级 available=False（Python 无 server 时 parity）
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


def test_lsp_diagnostics_shape(rpc):
    out = _call(rpc, "lsp_diagnostics", {"file_path": "test.py"})
    assert isinstance(out, dict)
    assert out.get("file_path") == "test.py"
    assert out.get("diagnostics") == []
    assert out.get("available") is False  # daemon 无 LSP server → 降级


def test_lsp_diagnostics_defaults(rpc):
    out = _call(rpc, "lsp_diagnostics", {})
    assert isinstance(out, dict)
    assert out.get("file_path") == ""
    assert out.get("available") is False


def test_lsp_diagnostics_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("lsp_diagnostics", {"file_path": "a.py"})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "lsp_diagnostics", {"file_path": "a.py"})
    assert isinstance(out, dict)
    assert "available" in out
