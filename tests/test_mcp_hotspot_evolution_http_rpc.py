# -*- coding: utf-8 -*-
"""MCP-046: hotspot_evolution → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~045）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 hotspot_evolution
3. 校验返回结构（hotspot_score / change_count / defect_count / complexity / label）
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


def test_hotspot_all(rpc):
    out = _call(rpc, "hotspot_evolution", {})
    assert isinstance(out, list)
    # 按 hotspot_score 降序
    scores = [x.get("hotspot_score", 0) for x in out]
    assert scores == sorted(scores, reverse=True)
    for item in out:
        assert "qualified_name" in item
        assert "symbol_hash" in item
        assert "hotspot_score" in item
        assert "change_count" in item
        assert "defect_count" in item
        assert "complexity" in item
        assert "label" in item


def test_hotspot_module_filter(rpc):
    out = _call(rpc, "hotspot_evolution", {"module_filter": "db"})
    assert isinstance(out, list)
    for item in out:
        assert str(item.get("module_path", "")).startswith("db") or item.get("module_path") in ("", None)


def test_hotspot_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("hotspot_evolution", {})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "hotspot_evolution", {"module_filter": ""})
    assert isinstance(out, list)
