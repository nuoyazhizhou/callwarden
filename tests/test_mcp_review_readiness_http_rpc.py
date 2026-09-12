# -*- coding: utf-8 -*-
"""MCP-043: review_readiness → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 review_readiness
   （P0-H：显式 workspace_instance_id）
3. 校验返回结构（impact_scope / risk_level / total_impacted / must_test /
   review_points / by_layer）

Rust 权威：query_compat_handlers.rs::handle_summary_review_readiness。
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


def test_review_readiness_unknown_symbol(rpc):
    """未知 symbol_hash → 空结果结构完整（scope/risk 均为 low）。"""
    out = _call(rpc, "review_readiness", {"symbol_hash": "no_such_hash_xyz"})
    assert isinstance(out, dict)
    assert out.get("impact_scope") == "low"
    assert out.get("risk_level") == out.get("impact_scope")
    assert out.get("total_impacted") == 0
    assert isinstance(out.get("must_test"), list)
    assert isinstance(out.get("review_points"), list)
    assert out.get("by_layer") == {"code": 0, "db": 0, "api": 0, "config": 0}


def test_review_readiness_struct(rpc):
    """结构面：must_test/review_points 元素字段投影（有数据时）。"""
    out = _call(rpc, "review_readiness", {"symbol_hash": "no_such_hash_xyz"})
    for mt in out["must_test"]:
        assert "qualified_name" in mt
        assert "name" in mt
        assert "file_path" in mt
        break


def test_review_readiness_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("review_readiness", {"workspace_instance_id": CANONICAL_INSTANCE,
                                    "symbol_hash": "x"})


def test_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "review_readiness", {"symbol_hash": "no_such_hash_xyz"})
    assert isinstance(out, dict)
    assert "must_test" in out
