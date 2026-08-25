# -*- coding: utf-8 -*-
"""MCP-042: get_clone_aware_impact → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~041）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 get_clone_aware_impact
3. 校验返回结构（source_symbol / original_blast_radius / clones /
   clone_blast_radii / total_impacted_with_clones）
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


def test_clone_aware_unknown_symbol(rpc):
    out = _call(rpc, "get_clone_aware_impact", {"qualified_name": "NO_SUCH_SYMBOL_xyz_987"})
    assert isinstance(out, dict)
    assert "error" in out
    assert "符号不存在" in out["error"]


def test_clone_aware_known_symbol(rpc):
    # 用真实符号（取一个 symbols 里的 qualified_name）
    out = _call(rpc, "get_clone_aware_impact", {"qualified_name": "handle_task_apply", "depth": 1})
    if "error" in out:
        pytest.skip("符号不在当前 workspace，跳过")
    assert isinstance(out, dict)
    src = out.get("source_symbol") or {}
    assert isinstance(src, dict)
    assert "original_blast_radius" in out
    assert isinstance(out.get("clones"), list)
    assert isinstance(out.get("clone_blast_radii"), list)
    assert isinstance(out.get("total_impacted_with_clones"), int)


def test_clone_aware_custom_depth(rpc):
    out = _call(rpc, "get_clone_aware_impact", {"qualified_name": "handle_task_apply", "depth": 2})
    assert isinstance(out, dict)
    if "error" not in out:
        assert isinstance(out.get("total_impacted_with_clones"), int)


def test_clone_aware_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("get_clone_aware_impact", {"qualified_name": "x"})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "get_clone_aware_impact", {"qualified_name": "handle_task_apply", "depth": 1})
    assert isinstance(out, dict)
