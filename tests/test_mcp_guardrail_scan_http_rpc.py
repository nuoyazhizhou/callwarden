# -*- coding: utf-8 -*-
"""MCP-035: guardrail_scan → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 guardrail_scan（P0-H：显式 workspace_instance_id）

写面语义（probeproven 2026-09-10）：
- guardrail_scan 首行 `_init_builtin_rules` 含 INSERT → 只读快照连接必拒。
- Rust fail-closed 返回 internal_error，消息对齐 Python worker 的
  `OperationalError attempt to write a readonly database` parity；
  错误类型 + "write-face" 关键字即 fail-closed 证据。

Rust 权威：query_compat_handlers.rs::handle_summary_guardrail_scan。
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import (  # noqa: E402
    DaemonRemoteError,
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


@pytest.mark.parametrize("params", [{}, {"file_filter": "server"}])
def test_guardrail_scan_write_face_fail_closed(rpc, params):
    """写面方法 → 只读快照连接拒绝（fail-closed，与 Python ro worker parity）。"""
    with pytest.raises(DaemonRemoteError) as ei:
        _call(rpc, "guardrail_scan", params)
    assert "write-face" in str(ei.value)


def test_guardrail_scan_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        c.call("guardrail_scan", {"workspace_instance_id": CANONICAL_INSTANCE})
