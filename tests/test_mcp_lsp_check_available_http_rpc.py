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


def test_lsp_check_available_shape(rpc):
    out = _call(rpc, "lsp_check_available", {})
    assert isinstance(out, dict)
    servers = out.get("available_servers") or {}
    assert isinstance(servers, dict)
    assert "python" in servers
    assert "typescript" in servers
    assert isinstance(out.get("total_available"), int)
    # 各服务器可用性取决于宿主机是否安装（隔离 daemon 同宿主机探测）——仅契约：bool
    for v in servers.values():
        assert isinstance(v, bool)


def test_lsp_check_available_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("lsp_check_available", {})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "lsp_check_available", {})
    assert isinstance(out, dict)
    assert "total_available" in out
