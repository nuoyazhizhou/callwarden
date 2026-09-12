# -*- coding: utf-8 -*-
"""MCP-052: cross_repo_impact → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~051）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 cross_repo_impact
3. 校验返回结构（source_symbol / impacted_repos / risk_level 等）
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


def test_cross_repo_unknown_symbol(rpc):
    out = _call(rpc, "cross_repo_impact", {"symbol_hash": "no_such_hash_xyz"})
    assert isinstance(out, dict)
    assert out.get("source_symbol") == ""
    assert out.get("total_impacted_repos") == 0
    assert out.get("risk_level") == "none"
    assert isinstance(out.get("impacted_repos"), list)


def test_cross_repo_known_symbol(rpc):
    out = _call(rpc, "cross_repo_impact", {"symbol_hash": "handle_task_apply"})
    assert isinstance(out, dict)
    if out.get("source_symbol") == "":
        pytest.skip("符号不在库中，跳过（unknown 分支已由 unknown_symbol 用例覆盖）")
    assert "source_symbol" in out
    assert isinstance(out.get("impacted_repos"), list)
    assert isinstance(out.get("local_impacted_count"), int)
    for repo in out["impacted_repos"]:
        assert "workspace" in repo
        assert "impacted_symbols" in repo
        assert "dependency_type" in repo
        assert "confidence" in repo


def test_cross_repo_custom_depth(rpc):
    out = _call(rpc, "cross_repo_impact", {"symbol_hash": "handle_task_apply", "depth": 3})
    assert isinstance(out, dict)
    assert isinstance(out.get("impacted_repos"), list)


def test_cross_repo_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("cross_repo_impact", {"symbol_hash": "x"})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "cross_repo_impact", {"symbol_hash": "handle_task_apply", "depth": 1})
    assert isinstance(out, dict)
