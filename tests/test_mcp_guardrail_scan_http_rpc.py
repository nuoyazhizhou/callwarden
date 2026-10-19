# -*- coding: utf-8 -*-
"""MCP-035: guardrail_scan → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 guardrail_scan（P0-H：显式 workspace_instance_id）

读面语义（2026-09-27 起，f8669e6 scan detect-only）：
- guardrail_scan 已由写面 fail-closed stub 改为纯只读 detect-only 实现：
  枚举 snapshot file_instances + 读盘检测（不可读文件跳过，对齐 Python
  content None 分支），findings 附 file_path 与 persisted=false，不写
  guardrail_findings 表；写面（builtin seed / 持久化）职责归写面调用点
  （rust_ext/src/cli/security.rs:399、bin/cw_cli.rs:2211）。
- 原 fail-closed 语义（只读快照连接恒 internal_error）实为 F-014「恒
  internal_error」缺陷的症状而非设计契约；Rust 单测已按新语义固化
  （query_compat_handlers.rs 测试区断言只读返回 findings 且 persisted=false）。

Rust 权威：query_compat_handlers.rs::handle_summary_guardrail_scan。
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


@pytest.mark.parametrize("params", [{}, {"file_filter": "server"}])
def test_guardrail_scan_read_only_detect(rpc, params):
    """guardrail_scan 为 detect-only 纯读：调用成功返回 list（不抛错）。

    2026-09-27 起 guardrail_scan 不再走写面（findings 不落库）；只读快照
    连接上返回检测结果。w3 快照唯一 file_instances 行的 abs_path
    （/w3/src/w3_seed.py）不存在于磁盘 → 读盘跳过 → 恒 `[]`。
    强契约（快照将来有可读文件时仍成立）：读实现 finding 必须附
    persisted=False——持久化职责归写面调用点（CLI）。
    """
    r = _call(rpc, "guardrail_scan", params)
    assert isinstance(r, list)
    assert r == [], "w3 快照无可读文件，detect-only 应返回空列表"
    for f in r:  # 空列表时循环体不执行；非空时逐项校验只读不持久化契约
        assert f.get("persisted") is False, f"读实现 finding 不得持久化: {f}"


def test_guardrail_scan_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        c.call("guardrail_scan", {"workspace_instance_id": CANONICAL_INSTANCE})
