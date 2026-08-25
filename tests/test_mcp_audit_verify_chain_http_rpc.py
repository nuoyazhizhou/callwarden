# -*- coding: utf-8 -*-
"""MCP-064: audit_verify_chain → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~063）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 audit_verify_chain
3. 语义对齐 Python db_audit_chain.verify_audit_chain：
   - 验证 audit_chain 签名链连续性与签名匹配
   - 检查项：record_signature 重算匹配 / prev_signature 连续 / 首条 prev 为空
   - table_name 为空时验证全部，否则只验证指定表
   - limit 默认 1000；返回 table_name/total_count/verified_count/
     broken_count/broken_records/security_level
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
    return rpc.call("audit_verify_chain", params)


def test_audit_verify_chain_default_shape(rpc):
    """默认参数（table_name='', limit=1000）：返回结构完整、计数自洽。"""
    out = _call(rpc, {})
    assert isinstance(out, dict)
    for key in (
        "table_name",
        "total_count",
        "verified_count",
        "broken_count",
        "broken_records",
        "security_level",
    ):
        assert key in out, f"缺字段 {key}"
    assert out["table_name"] == ""
    assert out["total_count"] == out["verified_count"] + out["broken_count"]
    assert isinstance(out["broken_records"], list)
    assert out["security_level"] in ("hmac", "hash_only")


def test_audit_verify_chain_broken_records_shape(rpc):
    """broken_records 每项含 id/table_name/record_id/reasons（数组）。"""
    out = _call(rpc, {})
    for rec in out["broken_records"]:
        for key in ("id", "table_name", "record_id", "reasons"):
            assert key in rec, f"缺字段 {key}"
        assert isinstance(rec["reasons"], list)


def test_audit_verify_chain_table_filter(rpc):
    """table_name 过滤：指定不存在的表 → 空链（0 条验证，不崩溃）。"""
    out = _call(rpc, {"table_name": "__nonexistent_table_zzz__", "limit": 10})
    assert isinstance(out, dict)
    assert out["table_name"] == "__nonexistent_table_zzz__"
    assert out["total_count"] == 0
    assert out["verified_count"] == 0
    assert out["broken_count"] == 0


def test_audit_verify_chain_limit_respected(rpc):
    """limit 截断：total_count 不超过 limit。"""
    out = _call(rpc, {"limit": 3})
    assert isinstance(out, dict)
    assert out["total_count"] <= 3


def test_audit_verify_chain_explicit_limit_zero(rpc):
    """limit=0 → 无记录可验证（total_count=0）。"""
    out = _call(rpc, {"limit": 0})
    assert isinstance(out, dict)
    assert out["total_count"] == 0
    assert out["verified_count"] == 0


def test_audit_verify_chain_unknown_workspace_ignored(rpc):
    """未知 workspace 参数不改变语义（read-only 工具按 table_name/limit 验证）。"""
    out = _call(rpc, {"table_name": "", "limit": 5, "workspace": "ws-does-not-exist"})
    assert isinstance(out, dict)
    assert out["total_count"] <= 5


def test_audit_verify_chain_invalid_limit_default(rpc):
    """非法 limit（非整数）→ 落到默认 1000，不崩溃。"""
    out = _call(rpc, {"table_name": "", "limit": "not-a-number"})
    assert isinstance(out, dict)
    assert out["total_count"] >= 0


def test_audit_verify_chain_repeatable(rpc):
    """重复调用结果稳定（无连接态副作用）。"""
    a = _call(rpc, {})
    b = _call(rpc, {})
    assert a["total_count"] == b["total_count"]
    assert a["verified_count"] == b["verified_count"]
    assert a["broken_count"] == b["broken_count"]


def test_audit_verify_chain_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("audit_verify_chain", {})
