# -*- coding: utf-8 -*-
"""MCP-047: defect_learn → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~046）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 defect_learn
3. 校验返回结构（learned_patterns / learned_fixes / details）
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


def test_defect_learn_unknown_commit(rpc):
    out = _call(rpc, "defect_learn", {"fix_commit_hash": "no_such_commit_xyz"})
    assert isinstance(out, dict)
    assert isinstance(out.get("learned_patterns"), int)
    assert isinstance(out.get("learned_fixes"), int)
    assert isinstance(out.get("details"), list)


def test_defect_learn_empty_hash(rpc):
    out = _call(rpc, "defect_learn", {"fix_commit_hash": ""})
    assert isinstance(out, dict)
    assert isinstance(out.get("details"), list)


def test_defect_learn_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("defect_learn", {"fix_commit_hash": "x"})


def test_new_client_instance_stable(rpc):
    c2 = HttpDaemonRpcClient()
    out = _call(c2, "defect_learn", {"fix_commit_hash": ""})
    assert isinstance(out, dict)
    assert "learned_fixes" in out
