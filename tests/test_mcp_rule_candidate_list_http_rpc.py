# -*- coding: utf-8 -*-
"""MCP-060: rule_candidate_list → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~059）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 rule_candidate_list
3. 语义对齐 Python db_agent_rules.rule_candidate_list + _row_to_candidate：
   - 返回 {"candidates": [...], "count": n}
   - status 默认 "pending"，空串表示不过滤
   - scope / evidence 由 *_json 反序列化为对象（非法/空 → {}）
   - ORDER BY created_at DESC，limit 透传 SQL LIMIT
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


def _call(rpc, params):
    return rpc.call("rule_candidate_list", params)


def test_rule_candidate_list_shape(rpc):
    """默认参数返回 candidates/count 契约形状，且 count 与数组长度一致。"""
    out = _call(rpc, {})
    assert isinstance(out, dict)
    assert isinstance(out.get("candidates"), list)
    assert isinstance(out.get("count"), int)
    assert out["count"] == len(out["candidates"])


def test_rule_candidate_list_row_fields(rpc):
    """有数据时逐行字段齐备；scope/evidence 必须是对象（反序列化后）。"""
    out = _call(rpc, {"status": "", "limit": 5})
    for row in out.get("candidates") or []:
        for key in (
            "id",
            "title",
            "rule_text",
            "scope",
            "severity",
            "source",
            "evidence",
            "confidence",
            "status",
            "created_at",
            "reviewed_at",
            "reviewer",
            "linked_rule_id",
        ):
            assert key in row, f"缺字段 {key}"
        assert isinstance(row["scope"], dict)
        assert isinstance(row["evidence"], dict)


def test_rule_candidate_list_limit_respected(rpc):
    """limit 透传 SQL LIMIT：结果数不超过 limit。"""
    out = _call(rpc, {"status": "", "limit": 2})
    assert len(out.get("candidates") or []) <= 2


def test_rule_candidate_list_status_filter(rpc):
    """显式 status 过滤后，所有行的 status 必须等于该值。"""
    out = _call(rpc, {"status": "pending", "limit": 20})
    for row in out.get("candidates") or []:
        assert row["status"] == "pending"


def test_rule_candidate_list_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("rule_candidate_list", {})


def test_new_client_instance_stable(rpc):
    """新建 client 实例重复调用结果稳定（无连接态副作用）。"""
    c2 = HttpDaemonRpcClient()
    out = c2.call("rule_candidate_list", {})
    assert isinstance(out, dict)
    assert "count" in out
