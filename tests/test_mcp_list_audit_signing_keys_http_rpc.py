# -*- coding: utf-8 -*-
"""MCP-065: list_audit_signing_keys → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~064）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 list_audit_signing_keys
3. 语义对齐 Python db_audit_chain.list_signing_keys：
   - 查询 audit_key_rotations 表，按 rotated_at DESC 返回密钥轮换记录
   - 每项含 key_id/rotated_at/is_active
   - 不返回 key_secret（避免泄露）
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402

# P0-COMPAT-v3（T-1788963104058-fdb2e848）：Rust native 路由要求显式 workspace 绑定
CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入  # noqa: E402


@pytest.fixture()
def rpc(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


def _call(rpc, params=None):
    return rpc.call("list_audit_signing_keys", {"workspace_instance_id": CANONICAL_INSTANCE, **(params or {})})


def test_list_audit_signing_keys_default_shape(rpc):
    """默认调用：返回数组（可能为空），每项含 key_id/rotated_at/is_active。"""
    out = _call(rpc)
    assert isinstance(out, list), f"期望数组，实际 {type(out)}"
    for item in out:
        assert isinstance(item, dict), f"每项应为 dict，实际 {type(item)}"
        for key in ("key_id", "rotated_at", "is_active"):
            assert key in item, f"缺字段 {key}"
        assert isinstance(item["key_id"], str)
        assert isinstance(item["rotated_at"], (int, float))
        assert item["is_active"] in (0, 1)


def test_list_audit_signing_keys_no_secret(rpc):
    """安全性：绝不返回 key_secret（避免泄露密钥内容）。"""
    out = _call(rpc)
    for item in out:
        assert "key_secret" not in item, "禁止返回 key_secret"


def test_list_audit_signing_keys_sorted_desc(rpc):
    """排序：rotated_at 按倒序排列（有 ≥2 条时验证严格递减）。"""
    out = _call(rpc)
    times = [item["rotated_at"] for item in out]
    assert times == sorted(times, reverse=True), "rotated_at 必须倒序"


def test_list_audit_signing_keys_repeatable(rpc):
    """重复调用结果稳定（无连接态副作用）。"""
    a = _call(rpc)
    b = _call(rpc)
    assert a == b


def test_list_audit_signing_keys_ignore_params(rpc):
    """工具无参数：传入任意参数不影响语义（只读，按表全量返回）。"""
    out = _call(rpc, {"workspace": "ws-does-not-exist", "limit": 999})
    assert isinstance(out, list)


def test_list_audit_signing_keys_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("list_audit_signing_keys", {})
