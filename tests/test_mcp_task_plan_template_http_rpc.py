# -*- coding: utf-8 -*-
"""MCP-070: task_plan_template → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~069）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 task_plan_template
3. 语义对齐 Python db/db_tasks.py::task_plan_template：
   - 返回 dict 含 template 字段（Markdown 模板字符串）
   - 纯静态实现，无参数
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


def _call(rpc, params=None):
    return rpc.call("task_plan_template", params or {})


def test_task_plan_template_default_shape(rpc):
    """默认调用：返回 dict 含 template 字段（字符串）。"""
    out = _call(rpc)
    assert isinstance(out, dict), f"期望 dict，实际 {type(out)}"
    assert "template" in out, "缺 template 字段"
    assert isinstance(out["template"], str), f"template 应为字符串，实际 {type(out['template'])}"
    assert len(out["template"]) > 0, "template 不应为空"
    # 验证模板内容包含关键占位符
    assert "{Root task title}" in out["template"]
    assert "{Subtask 1 title}" in out["template"]
    assert "{Step 1 description}" in out["template"]


def test_task_plan_template_ignores_params(rpc):
    """传入额外参数被忽略，行为不变。"""
    out = _call(rpc, {"unexpected": "value"})
    assert isinstance(out, dict)
    assert "template" in out
    assert isinstance(out["template"], str)
    assert len(out["template"]) > 0


def test_task_plan_template_repeatable(rpc):
    """重复调用结果稳定（无连接态副作用）。"""
    a = _call(rpc)
    b = _call(rpc)
    assert a == b


def test_task_plan_template_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("task_plan_template", {})