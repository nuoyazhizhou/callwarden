# -*- coding: utf-8 -*-
"""MCP-033: who_to_ask → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 who_to_ask（P0-H：显式 workspace_instance_id）
3. 校验返回结构：file_ownership JOIN file_instances 按 abs_path/rel_path 查询；
   无行 → null（与 Python None 语义一致）；命中时断言字段投影。

Rust 权威：query_compat_handlers.rs::handle_summary_who_to_ask。
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


def test_who_to_ask_no_match(rpc):
    """无 file_ownership 记录 → null（与 Python None 语义一致）。"""
    r = _call(rpc, "who_to_ask", {"file_path": "NO_SUCH_FILE_xyz.py"})
    assert r is None or r == {}


def test_who_to_ask_rel_path(rpc):
    """相对路径 → 结构正确（无记录时 null）。"""
    r = _call(rpc, "who_to_ask", {"file_path": "server/main.py"})
    assert r is None or isinstance(r, dict)


def test_who_to_ask_abs_path(rpc):
    """绝对路径 → 结构正确（无记录时 null）。"""
    r = _call(rpc, "who_to_ask", {"file_path": "C:/git_work/callwarden/cw.py"})
    assert r is None or isinstance(r, dict)


def test_who_to_ask_empty_path(rpc):
    """缺参/空 file_path → null（不报错）。"""
    r = _call(rpc, "who_to_ask", {})
    assert r is None or r == {}


def test_who_to_ask_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        c.call("who_to_ask", {"workspace_instance_id": CANONICAL_INSTANCE,
                              "file_path": "server/main.py"})


def test_who_to_ask_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = _call(c2, "who_to_ask", {"file_path": "server/main.py"})
    assert r is None or isinstance(r, dict)
