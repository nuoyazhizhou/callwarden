# -*- coding: utf-8 -*-
"""MCP-053: cross_repo_summary → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~052）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 cross_repo_summary
3. 校验返回结构（total_repos / repos / total_cross_deps /
   total_shared_symbols / deps_by_type）
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


def test_cross_repo_summary_shape(rpc):
    out = _call(rpc, "cross_repo_summary", {})
    assert isinstance(out, dict)
    assert isinstance(out.get("total_repos"), int)
    assert isinstance(out.get("repos"), list)
    assert isinstance(out.get("total_cross_deps"), int)
    assert isinstance(out.get("total_shared_symbols"), int)
    assert isinstance(out.get("deps_by_type"), dict)
    for repo in out["repos"]:
        assert "id" in repo
        assert "name" in repo
        assert "symbol_count" in repo


def test_cross_repo_summary_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("cross_repo_summary", {})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "cross_repo_summary", {})
    assert isinstance(out, dict)
    assert "total_repos" in out
