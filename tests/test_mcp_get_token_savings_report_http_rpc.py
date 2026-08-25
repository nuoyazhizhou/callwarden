# -*- coding: utf-8 -*-
"""MCP-040: get_token_savings_report → Rust daemon native 的 HTTP RPC 往返测试。

与 MCP-002~005/033~039 相同的 live-daemon HTTP 往返模式：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. 用 HttpDaemonRpcClient 走 /v1/rpc 调用 get_token_savings_report
3. 校验返回结构（time_window / total_saved / total_operations /
   avg_savings_pct / by_operation / daily_trend / headline）
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


def test_token_savings_default_window(rpc):
    out = _call(rpc, "get_token_savings_report", {})
    assert isinstance(out, dict)
    assert out.get("time_window") == "30d"
    assert isinstance(out.get("total_saved"), int)
    assert isinstance(out.get("total_operations"), int)
    assert isinstance(out.get("avg_savings_pct"), (int, float))
    assert isinstance(out.get("by_operation"), dict)
    assert isinstance(out.get("daily_trend"), list)
    assert isinstance(out.get("headline"), str)
    for d in out["daily_trend"]:
        assert "date" in d and "saved" in d and "ops" in d


def test_token_savings_custom_window(rpc):
    out = _call(rpc, "get_token_savings_report", {"time_window": "7d"})
    assert isinstance(out, dict)
    assert out.get("time_window") == "7d"
    assert isinstance(out.get("total_saved"), int)


def test_token_savings_all_window(rpc):
    out = _call(rpc, "get_token_savings_report", {"time_window": ""})
    assert isinstance(out, dict)
    assert out.get("time_window") == ""
    assert isinstance(out.get("total_saved"), int)


def test_token_savings_headline_shape(rpc):
    out = _call(rpc, "get_token_savings_report", {"time_window": "365d"})
    headline = out.get("headline") or ""
    assert isinstance(headline, str)
    assert "节省" in headline or headline == ""
    # headline 非空时含 token 单位
    if headline:
        assert "tokens" in headline


def test_token_savings_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("get_token_savings_report", {})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "get_token_savings_report", {"time_window": "30d"})
    assert isinstance(out, dict)
    assert "total_saved" in out
