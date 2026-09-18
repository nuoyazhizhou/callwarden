"""MCP 工具 task_create 的 workspace 参数转发与返回注解回归（无 daemon）。

越界产品缺陷（2026-09-16 由 §W3 卡 T-1789529126780-6cf84728 step3 实证发现）：
`server/tools/tools_task.py` 的 MCP 工具 `task_create`
1. 签名缺 workspace_id / workspace_instance_id → 无法满足 BR-01/BR-02
   权威契约（named-pipe 路由下 _inject_workspace_id 需要 capture 链有
   instance，MCP 入口无法显式声明 → E_TASK_WORKSPACE_INSTANCE_REQUIRED）；
2. 返回注解 `-> str` 与 `_route` 实际返回的 dict 不符 → fastmcp pydantic
   校验拒绝（task_createOutput: Input should be a valid string）。
   兄弟工具 task_next_step 用 `Optional[dict]`。

本测试经 fastmcp 真实校验层（call_tool + convert_result）验证修复：
- 传入 workspace 配对时，配对被逐字转发到 task.create RPC 载荷；
- 返回 dict 不再被 pydantic 拒绝（旧注解会抛 ValidationError）；
- 缺省（0 / ""）保持旧的隐式注入路径（falsy → route_task_write 的
  _inject_workspace_id 仍会解析 active workspace），不破坏既有调用方。
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP

import callwarden.server.tools.tools_task as tools_task


@pytest.fixture
def captured_route(monkeypatch):
    """替换 tools_task 模块内的 _route（route_rpc 别名），捕获载荷并返假 dict。"""
    calls = []

    def _fake_route(rpc_method, params, policy):
        calls.append((rpc_method, params, policy))
        return {
            "task_id": "T-1789552669940-fea7bacc",
            "status": "open",
            "request_id": "req-test-create",
            "snapshot_id": "deadbeef",
        }

    monkeypatch.setattr(tools_task, "_route", _fake_route)
    return calls


def _call_task_create(arguments: dict) -> str:
    """只注册被测工具，避开 create_mcp_server 的全量注册与 HTTP workspace 配置。"""
    mcp = FastMCP("callwarden-task-create-test")
    tools_task.register(mcp)
    res = asyncio.run(mcp.call_tool("task_create", arguments))
    items = res[0] if isinstance(res, tuple) else res
    blocks = items if isinstance(items, list) else [items]
    return "\n".join(str(getattr(b, "text", "") or "") for b in blocks)


def test_task_create_forwards_workspace_pair(captured_route):
    """显式配对逐字进入 task.create 载荷（BR-01/BR-02）。"""
    _call_task_create({
        "title": "ws-pair create",
        "workspace_id": 7,
        "workspace_instance_id": "inst-abc",
    })
    assert len(captured_route) == 1
    method, params, policy = captured_route[0]
    assert method == "task.create"
    assert policy == "PROTECTED_MUTATION"
    assert params["workspace_id"] == 7
    assert params["workspace_instance_id"] == "inst-abc"
    assert params["title"] == "ws-pair create"


def test_task_create_returns_dict_without_pydantic_rejection(captured_route):
    """dict 返回经 fastmcp convert_result 校验通过（旧 `-> str` 注解会拒绝）。"""
    text = _call_task_create({
        "title": "dict return",
        "workspace_id": 1,
        "workspace_instance_id": "inst-x",
    })
    # 返回的是结构化对象文本，含 task_id 与 status，而非裸 task_id 字符串
    assert "T-1789552669940-fea7bacc" in text
    assert "open" in text


def test_task_create_defaults_preserve_implicit_injection(captured_route):
    """缺省 0/"" 保持旧行为：载荷带 falsy 配对 → route_task_write 的
    _inject_workspace_id 仍会解析 active workspace（兼容未显式传参的调用方）。"""
    _call_task_create({"title": "legacy implicit"})
    _, params, _ = captured_route[0]
    assert params["workspace_id"] == 0
    assert params["workspace_instance_id"] == ""
