# -*- coding: utf-8 -*-
"""MCP-047: defect_learn → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 defect_learn
   （P0-H：显式 workspace_instance_id）
3. 校验返回结构（learned_patterns / learned_fixes / details）

写面语义（probeproven 2026-09-10）：
- 无 qualifying 变更（空 hash / 未知 commit）→ 零结果 {learned_patterns:0,
  learned_fixes:0, details:[]}（先判定后写，与 Python 一致）。
- 存在 qualifying 变更时 INSERT defect_fixes/defect_patterns → 只读快照连接
  必拒（fail-closed parity），live 库不构造该场景。

Rust 权威：query_compat_handlers.rs::handle_summary_defect_learn。
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


def test_defect_learn_unknown_commit(rpc):
    """未知 commit → 零结果结构完整。"""
    out = _call(rpc, "defect_learn", {"fix_commit_hash": "no_such_commit_xyz"})
    assert isinstance(out, dict)
    assert out.get("learned_patterns") == 0
    assert out.get("learned_fixes") == 0
    assert out.get("details") == []


def test_defect_learn_empty_hash(rpc):
    """空 hash → 零结果（不抛错）。"""
    out = _call(rpc, "defect_learn", {"fix_commit_hash": ""})
    assert isinstance(out, dict)
    assert out.get("learned_patterns") == 0
    assert out.get("learned_fixes") == 0
    assert isinstance(out.get("details"), list)


def test_defect_learn_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("defect_learn", {"workspace_instance_id": CANONICAL_INSTANCE,
                                "fix_commit_hash": "x"})


def test_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "defect_learn", {"fix_commit_hash": ""})
    assert isinstance(out, dict)
    assert "learned_fixes" in out
