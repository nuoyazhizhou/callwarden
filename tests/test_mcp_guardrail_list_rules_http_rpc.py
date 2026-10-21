# -*- coding: utf-8 -*-
"""MCP-037: guardrail_list_rules → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 guardrail_list_rules
   （P0-H：显式 workspace_instance_id）

读面语义（2026-09-27 起，c004d17 list_rules 只读化）：
- guardrail_list_rules 已由写面 fail-closed stub 改为纯只读实现：直接 SELECT
  guardrail_rules 表（缺表时返回空列表，对齐 Python 新库首跑前语义），内置
  规则优先、按创建时间升序；builtin 初始化不再随读发生，职责归写面调用点
  （CLI guardrail rules / scan 事务内，rust_ext/src/cli/security.rs:399、
  bin/cw_cli.rs:2211）。
- 原 fail-closed 语义（只读快照连接恒 internal_error）实为 F-014「恒
  internal_error」缺陷的症状而非设计契约；Rust 单测已按新语义固化
  （query_compat_handlers.rs 测试区断言只读连接返回规则数组 / 缺表返回 []）。

附注（设计决策，待 owner 裁定）：fresh workspace 未经 CLI seed 时
guardrail_rules 表为空（w3 快照甚至无该表）→ 首调即见 []；若产品要求
「读面首调即见内置规则」，需在 snapshot 注册时 seed，属产品决策非缺陷。

Rust 权威：query_compat_handlers.rs::handle_summary_guardrail_list_rules。
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


@pytest.mark.parametrize("params", [{}, {"category_filter": "db_safety"}])
def test_guardrail_list_rules_read_only(rpc, params):
    """guardrail_list_rules 为纯只读：调用成功返回 list（不抛错）。

    2026-09-27 起 guardrail_list_rules 不再随读初始化 builtin 规则
    （写面职责归 CLI 事务内）；fresh workspace 未经 CLI seed → 快照无
    guardrail_rules 表（或缺行）→ 只读实现按设计返回 `[]`。
    """
    r = _call(rpc, "guardrail_list_rules", params)
    assert isinstance(r, list)
    assert r == [], "w3 快照无 guardrail_rules 表/无 seed，只读实现应返回 []"


def test_guardrail_list_rules_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        c.call("guardrail_list_rules", {"workspace_instance_id": CANONICAL_INSTANCE})
