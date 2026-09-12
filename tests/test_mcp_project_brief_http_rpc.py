# -*- coding: utf-8 -*-
"""MCP-030: project_brief → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 project_brief（P0-H：显式 workspace_instance_id）
3. 校验返回结构：project_type / file_count / function_count / total_lines /
   modules / hot_functions / health_score / health_level / avg_complexity /
   comment_coverage

Rust 权威：query_compat_handlers.rs::handle_summary_project_brief。
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


def test_project_brief_structure(rpc):
    """返回结构完整：project_type/file_count/function_count/total_lines/
    modules/hot_functions/health_score/health_level/avg_complexity/
    comment_coverage。"""
    r = _call(rpc, "project_brief", {})
    assert isinstance(r, dict)
    for k in ("project_type", "file_count", "function_count", "total_lines",
              "modules", "hot_functions", "health_score", "health_level",
              "avg_complexity", "comment_coverage"):
        assert k in r, f"缺字段 {k}"
    assert isinstance(r.get("modules"), list)
    assert isinstance(r.get("hot_functions"), list)
    assert isinstance(r.get("file_count"), int)
    # canonical workspace 有真实数据
    assert r.get("file_count", 0) > 0
    assert isinstance(r.get("project_type"), str) and r["project_type"]


def test_project_brief_modules_shape(rpc):
    """modules 元素含 module/function_count。"""
    r = _call(rpc, "project_brief", {})
    for item in r.get("modules", []):
        assert "module" in item
        assert "function_count" in item
        break


def test_project_brief_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(DaemonUnavailableError):
        # fail-closed 证据即异常类型本身（本机代理会把 127.0.0.1:9 拦成 502，
        # 错误消息随网络环境变化，不钉死错误码字符串）
        c.call("project_brief", {"workspace_instance_id": CANONICAL_INSTANCE})


def test_project_brief_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    r = _call(c2, "project_brief", {})
    assert isinstance(r, dict)
    assert "project_type" in r and "modules" in r
