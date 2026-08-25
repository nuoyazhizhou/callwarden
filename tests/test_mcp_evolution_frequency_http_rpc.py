# -*- coding: utf-8 -*-
"""MCP-045: evolution_frequency → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~044）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 evolution_frequency
3. 校验返回结构（change_count / changers / timeline / intervals /
   distribution{daily,weekly,monthly}）
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


def test_evolution_unknown_symbol(rpc):
    out = _call(rpc, "evolution_frequency", {"qualified_name": "no_such_symbol_xyz"})
    assert isinstance(out, dict)
    assert out.get("change_count") == 0
    assert isinstance(out.get("changers"), list)
    assert isinstance(out.get("timeline"), list)
    assert isinstance(out.get("intervals"), list)
    dist = out.get("distribution") or {}
    assert "daily" in dist and "weekly" in dist and "monthly" in dist


def test_evolution_known_symbol(rpc):
    out = _call(rpc, "evolution_frequency", {"qualified_name": "handle_task_apply"})
    assert isinstance(out, dict)
    assert isinstance(out.get("change_count"), int)
    assert isinstance(out.get("first_seen"), (int, float))
    assert isinstance(out.get("last_changed"), (int, float))
    assert isinstance(out.get("changers"), list)
    assert isinstance(out.get("timeline"), list)
    assert isinstance(out.get("avg_interval"), (int, float))
    dist = out.get("distribution") or {}
    assert isinstance(dist.get("daily"), dict)
    assert isinstance(dist.get("weekly"), dict)
    assert isinstance(dist.get("monthly"), dict)
    # 有变更时 timeline 条目结构
    for tl in out["timeline"]:
        assert "timestamp" in tl
        assert "commit_hash" in tl


def test_evolution_time_window(rpc):
    out = _call(rpc, "evolution_frequency", {"qualified_name": "handle_task_apply", "time_window": "30d"})
    assert isinstance(out, dict)
    assert isinstance(out.get("change_count"), int)


def test_evolution_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("evolution_frequency", {"qualified_name": "x"})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "evolution_frequency", {"qualified_name": "handle_task_apply"})
    assert isinstance(out, dict)
    assert "distribution" in out
