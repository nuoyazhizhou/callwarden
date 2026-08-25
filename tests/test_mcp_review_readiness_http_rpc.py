# -*- coding: utf-8 -*-
"""MCP-043: review_readiness → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~042）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 review_readiness
3. 校验返回结构（impact_scope / risk_level / total_impacted / must_test /
   review_points / by_layer）
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


def test_review_readiness_unknown_symbol(rpc):
    out = _call(rpc, "review_readiness", {"symbol_hash": "no_such_hash_xyz"})
    assert isinstance(out, dict)
    # blast_radius 对未知符号返回空结果 → scope 为 low
    assert out.get("impact_scope") in ("high", "medium", "low")
    assert isinstance(out.get("total_impacted"), int)
    assert isinstance(out.get("must_test"), list)
    assert isinstance(out.get("review_points"), list)
    assert isinstance(out.get("by_layer"), dict)


def test_review_readiness_known_symbol(rpc):
    out = _call(rpc, "review_readiness", {"symbol_hash": "handle_task_apply"})
    assert isinstance(out, dict)
    assert out.get("impact_scope") in ("high", "medium", "low")
    assert out.get("risk_level") == out.get("impact_scope")
    assert isinstance(out.get("must_test"), list)
    for mt in out["must_test"]:
        assert "qualified_name" in mt
        assert "name" in mt
        assert "file_path" in mt
    assert isinstance(out.get("review_points"), list)


def test_review_readiness_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("review_readiness", {"symbol_hash": "x"})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "review_readiness", {"symbol_hash": "handle_task_apply"})
    assert isinstance(out, dict)
    assert "must_test" in out
