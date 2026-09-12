# -*- coding: utf-8 -*-
"""MCP-036: guardrail_check_edit → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 guardrail_check_edit
   （P0-H：显式 workspace_instance_id）
3. 校验返回结构 {decision, findings, message}：只读面（不落库），
   DROP TABLE → block；无风险 → pass；文件不可读 → pass。

Rust 权威：query_compat_handlers.rs::handle_summary_guardrail_check_edit。
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import (  # noqa: E402
    DaemonUnavailableError,
    HttpDaemonRpcClient,
)
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


def test_guardrail_check_edit_block_on_drop_table(rpc):
    """拟议修改含 DROP TABLE → decision=block。"""
    r = _call(rpc, "guardrail_check_edit", {
        "file_path": "server/x.sql",
        "proposed_change": "ALTER TABLE users ADD COLUMN x;\nDROP TABLE legacy;",
    })
    assert isinstance(r, dict)
    assert "decision" in r and "findings" in r and "message" in r
    assert r["decision"] == "block"


def test_guardrail_check_edit_pass(rpc):
    """无风险内容 → decision=pass。"""
    r = _call(rpc, "guardrail_check_edit", {
        "file_path": "server/x.rs",
        "proposed_change": "fn add(a: i32, b: i32) -> i32 { a + b }",
    })
    assert isinstance(r, dict)
    assert r["decision"] in ("pass", "warn")


def test_guardrail_check_edit_missing_file(rpc):
    """文件不可读 → pass（fail-open on missing file，不抛错）。"""
    r = _call(rpc, "guardrail_check_edit", {"file_path": "NO_SUCH_FILE_xyz.rs"})
    assert isinstance(r, dict)
    assert r["decision"] == "pass"


def test_guardrail_check_edit_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        c.call("guardrail_check_edit", {"workspace_instance_id": CANONICAL_INSTANCE,
                                        "file_path": "x.rs", "proposed_change": "x"})


def test_guardrail_check_edit_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = _call(c2, "guardrail_check_edit", {
        "file_path": "server/x.rs",
        "proposed_change": "fn f() {}",
    })
    assert isinstance(r, dict) and "decision" in r
