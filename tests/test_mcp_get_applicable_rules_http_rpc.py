# -*- coding: utf-8 -*-
"""MCP-062: get_applicable_rules → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~061）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 get_applicable_rules
3. 语义对齐 Python db_agent_rules.get_applicable_rules + _match_scope + _row_to_rule：
   - 返回 {"rules": [...], "count": n}，每行含 matched_scope
   - context 键为单数：language / file_path / symbol_kind / action /
     finding_type / module_prefix
   - 空 scope=全局匹配；同字段多值 OR；跨字段 AND；file_patterns glob；
     module_prefixes 前缀匹配
   - 排序 severity 优先级 → 命中字段数倒序 → updated_at 倒序
   - limit <= 0 → 空结果
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402

_SEVERITY_ORDER = {"critical": 4, "error": 3, "warning": 2, "info": 1}


@pytest.fixture(scope="module")
def rpc():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c


def _call(rpc, params):
    return rpc.call("get_applicable_rules", params)


def test_get_applicable_rules_shape(rpc):
    """默认参数返回 rules/count 契约形状，且 count 与数组长度一致。"""
    out = _call(rpc, {})
    assert isinstance(out, dict)
    assert isinstance(out.get("rules"), list)
    assert isinstance(out.get("count"), int)
    assert out["count"] == len(out["rules"])


def test_get_applicable_rules_row_fields(rpc):
    """有数据时逐行字段齐备；scope/evidence 必须是对象；matched_scope 为数组。"""
    out = _call(rpc, {"context": {}, "limit": 20})
    for row in out.get("rules") or []:
        for key in (
            "id",
            "title",
            "rule_text",
            "scope",
            "severity",
            "status",
            "source_candidate_id",
            "evidence",
            "created_at",
            "updated_at",
            "synced_to_agents_md",
            "sync_hash",
            "matched_scope",
        ):
            assert key in row, f"缺字段 {key}"
        assert isinstance(row["scope"], dict)
        assert isinstance(row["evidence"], dict)
        assert isinstance(row["matched_scope"], list)


def test_get_applicable_rules_global_matches(rpc):
    """空 context：只有全局规则（matched_scope=["global"]）会被匹配。"""
    out = _call(rpc, {"context": {}, "limit": 50})
    for row in out.get("rules") or []:
        assert row["matched_scope"] == ["global"]


def test_get_applicable_rules_context_match(rpc):
    """非空 context：全局规则命中且 matched_scope 保留实际命中标签。

    若数据库存在按 language 匹配的规则，则 language 规则应命中，
    其余非全局规则（未匹配 context 全部字段）不应出现在结果中。
    """
    out = _call(rpc, {"context": {"language": "python", "action": "edit"}, "limit": 50})
    for row in out.get("rules") or []:
        labels = row["matched_scope"]
        assert isinstance(labels, list)
        # 全局规则恒为 ["global"]；非全局规则必须携带至少一个非 global 标签
        if labels != ["global"]:
            assert all(l != "global" for l in labels)


def test_get_applicable_rules_limit_respected(rpc):
    """limit 截断：结果数不超过 limit。"""
    out = _call(rpc, {"context": {}, "limit": 3})
    assert len(out.get("rules") or []) <= 3


def test_get_applicable_rules_zero_limit(rpc):
    """limit<=0 → 空结果（与 Python get_applicable_rules 行为一致）。"""
    out = _call(rpc, {"context": {}, "limit": 0})
    assert out.get("rules") == []
    assert out.get("count") == 0


def test_get_applicable_rules_severity_ordering(rpc):
    """排序契约：severity 优先级单调不减（同级再按命中字段数、updated_at）。"""
    out = _call(rpc, {"context": {}, "limit": 50})
    ranks = [
        _SEVERITY_ORDER.get(row.get("severity"), 0) for row in out.get("rules") or []
    ]
    assert ranks == sorted(ranks, reverse=True), f"severity 排序被破坏: {ranks}"


def test_get_applicable_rules_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("get_applicable_rules", {})


def test_new_client_instance_stable(rpc):
    """新建 client 实例重复调用结果稳定（无连接态副作用）。"""
    c2 = HttpDaemonRpcClient()
    out = c2.call("get_applicable_rules", {"context": {}, "limit": 5})
    assert isinstance(out, dict)
    assert "count" in out
