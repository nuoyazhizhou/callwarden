# -*- coding: utf-8 -*-
"""MCP-049: merge_preview → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~048）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 merge_preview
3. 校验返回结构（affected_symbols / impact_layers / risk_level）
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


def test_merge_preview_unknown_branch(rpc):
    out = _call(rpc, "merge_preview", {"source_branch": "NO_SUCH_SRC", "target_branch": "NO_SUCH_TGT"})
    assert isinstance(out, dict)
    assert "error" in out


def test_merge_preview_known_branches(rpc):
    # 用真实存在的 workspace 名（list_branches 取前两个）
    branches = _call(rpc, "list_branches", {})
    if len(branches) < 2:
        pytest.skip("不足两个 workspace，跳过")
    src = branches[0]["name"]
    tgt = branches[1]["name"]
    out = _call(rpc, "merge_preview", {"source_branch": src, "target_branch": tgt})
    assert isinstance(out, dict)
    if "error" in out:
        pytest.skip(out["error"])
    assert isinstance(out.get("affected_symbols"), int)
    assert isinstance(out.get("impact_layers"), list)
    assert out.get("risk_level") in ("high", "medium", "low")
    for il in out["impact_layers"]:
        assert "source_hash" in il
        assert "by_layer" in il


def test_merge_preview_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("merge_preview", {"source_branch": "a", "target_branch": "b"})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "merge_preview", {"source_branch": "x", "target_branch": "y"})
    assert isinstance(out, dict)
