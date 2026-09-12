# -*- coding: utf-8 -*-
"""MCP-045: evolution_frequency → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 evolution_frequency
   （P0-H：显式 workspace_instance_id）
3. 校验返回结构（change_count / changers / timeline / intervals /
   distribution{daily,weekly,monthly}）

Rust 权威：query_compat_handlers.rs::handle_summary_evolution_frequency。
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


def test_evolution_unknown_symbol(rpc):
    """未知符号 → 零结果结构完整。"""
    out = _call(rpc, "evolution_frequency", {"qualified_name": "no_such_symbol_xyz"})
    assert isinstance(out, dict)
    assert out.get("qualified_name") == "no_such_symbol_xyz"
    assert out.get("change_count") == 0
    assert out.get("changers") == []
    assert out.get("timeline") == []
    assert out.get("intervals") == []
    assert out.get("first_seen") == 0.0
    assert out.get("last_changed") == 0.0
    dist = out.get("distribution") or {}
    assert dist.get("daily") == {} and dist.get("weekly") == {} and dist.get("monthly") == {}


def test_evolution_struct(rpc):
    """结构面：数值字段类型 + distribution 三键（有数据时）。"""
    out = _call(rpc, "evolution_frequency", {"qualified_name": "no_such_symbol_xyz"})
    assert isinstance(out.get("change_count"), int)
    assert isinstance(out.get("first_seen"), (int, float))
    assert isinstance(out.get("last_changed"), (int, float))
    assert isinstance(out.get("avg_interval"), (int, float))
    dist = out.get("distribution") or {}
    assert isinstance(dist.get("daily"), dict)
    assert isinstance(dist.get("weekly"), dict)
    assert isinstance(dist.get("monthly"), dict)
    for tl in out["timeline"]:
        assert "timestamp" in tl
        assert "commit_hash" in tl
        break


def test_evolution_time_window(rpc):
    """time_window 参数被接受且结构稳定。"""
    out = _call(rpc, "evolution_frequency", {"qualified_name": "no_such_symbol_xyz",
                                             "time_window": "30d"})
    assert isinstance(out, dict)
    assert isinstance(out.get("change_count"), int)


def test_evolution_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("evolution_frequency", {"workspace_instance_id": CANONICAL_INSTANCE,
                                       "qualified_name": "x"})


def test_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "evolution_frequency", {"qualified_name": "no_such_symbol_xyz"})
    assert isinstance(out, dict)
    assert "distribution" in out
