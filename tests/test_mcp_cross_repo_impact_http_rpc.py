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
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "cross_repo_impact", {"symbol_hash": "handle_task_apply", "depth": 1})
    assert isinstance(out, dict)
