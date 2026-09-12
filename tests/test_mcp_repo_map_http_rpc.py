# -*- coding: utf-8 -*-
"""MCP-031: repo_map → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 repo_map（P0-H：显式 workspace_instance_id）
3. 校验返回结构：text（仓库模块依赖图） / mermaid（graph TD）

Rust 权威：query_compat_handlers.rs::handle_summary_repo_map。
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


def test_repo_map_text(rpc):
    """默认 text → 返回仓库模块依赖图文本。"""
    r = _call(rpc, "repo_map", {})
    assert isinstance(r, str)
    assert "仓库模块依赖图" in r


def test_repo_map_mermaid(rpc):
    """format=mermaid → 返回 graph TD 文本。"""
    r = _call(rpc, "repo_map", {"format": "mermaid"})
    assert isinstance(r, str)
    assert r.startswith("graph TD")


def test_repo_map_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        # fail-closed 证据即异常类型本身（本机代理会把 127.0.0.1:9 拦成 502，
        # 错误消息随网络环境变化，不钉死错误码字符串）
        c.call("repo_map", {"workspace_instance_id": CANONICAL_INSTANCE})


def test_repo_map_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = _call(c2, "repo_map", {})
    assert isinstance(r, str)
