"""MCP 工具 task_create 的 workspace 归属契约回归（无 daemon）。

历史背景：
- 2026-09-16 §W3 卡 T-1789529126780-6cf84728 step3 实证 `task_create` 签名缺
  workspace 参数、返回注解 `-> str` 与 dict 不符（fastmcp pydantic 拒绝）。
- 2026-09-24 HEAD `bd5cd60`「任务单归属按调用项目根 fail-closed，禁止跨项目建卡」
  进一步把归属改为由调用方项目根解析出的 canonical 配对决定：
  `_task_create_context_pair()` 先 `resolve_workspace_pair_from_daemon(root)`，
  解析失败返回 `E_WORKSPACE_ROOT_UNRESOLVED`；agent 传入的 workspace 配对只作
  "声明"，与当前项目不一致即 `E_WORKSPACE_PROJECT_MISMATCH`；缺省 0/"" 由服务端
  按当前项目补齐，绝不使用连接默认项目。

本测试经 fastmcp 真实校验层（call_tool + convert_result）验证：
- 归属配对（ctx）逐字进入 task.create 载荷，调用方显式声明的同值配对不改变归属；
- 缺省 0/"" 由 ctx 补齐（不再保留旧的 falsy 隐式注入路径）；
- 跨项目声明被拒绝且不发 RPC；
- 项目根无法解析时 fail-closed 且不发 RPC；
- 返回 dict 不再被 pydantic 拒绝。
"""
from __future__ import annotations

import asyncio

import pytest
from mcp.server.fastmcp import FastMCP

import callwarden.server.tools.tools_task as tools_task
from callwarden.server.daemon_client import DaemonUnavailableError

TASK_ID = "T-1789552669940-fea7bacc"
CTX_PAIR = {"workspace_id": 1, "workspace_instance_id": "4baea3ff12c2ea5c"}


@pytest.fixture
def captured_route(monkeypatch):
    """替换 tools_task 模块内的 _route（route_rpc 别名），捕获载荷并返假 dict。"""
    calls = []

    def _fake_route(rpc_method, params, policy):
        calls.append((rpc_method, params, policy))
        return {
            "task_id": TASK_ID,
            "status": "open",
            "request_id": "req-test-create",
            "snapshot_id": "deadbeef",
        }

    monkeypatch.setattr(tools_task, "_route", _fake_route)
    return calls


@pytest.fixture
def fixed_ctx(monkeypatch):
    """固定本 MCP 实例所属项目的 canonical 配对，避免依赖真实 daemon。"""
    monkeypatch.setattr(
        tools_task, "_task_create_context_pair", lambda: (dict(CTX_PAIR), None)
    )
    return CTX_PAIR


@pytest.fixture
def unresolved_ctx(monkeypatch):
    """模拟调用方项目根无法解析出唯一权威配对。"""

    def _unresolved():
        return None, {
            "ok": False,
            "code": "E_WORKSPACE_ROOT_UNRESOLVED",
            "root": "C:/tmp/unregistered",
            "error": "无法解析当前项目 workspace 归属",
        }

    monkeypatch.setattr(tools_task, "_task_create_context_pair", _unresolved)


def _call_task_create(arguments: dict) -> str:
    """只注册被测工具，避开 create_mcp_server 的全量注册与 HTTP workspace 配置。"""
    mcp = FastMCP("callwarden-task-create-test")
    tools_task.register(mcp)
    res = asyncio.run(mcp.call_tool("task_create", arguments))
    items = res[0] if isinstance(res, tuple) else res
    blocks = items if isinstance(items, list) else [items]
    return "\n".join(str(getattr(b, "text", "") or "") for b in blocks)


def test_task_create_forwards_context_pair(captured_route, fixed_ctx):
    """调用方声明与当前项目一致时，载荷使用 ctx 配对（归属由项目根决定）。"""
    _call_task_create({
        "title": "ws-pair create",
        "workspace_id": 1,
        "workspace_instance_id": "4baea3ff12c2ea5c",
    })
    assert len(captured_route) == 1
    method, params, policy = captured_route[0]
    assert method == "task.create"
    assert policy == "PROTECTED_MUTATION"
    assert params["workspace_id"] == 1
    assert params["workspace_instance_id"] == "4baea3ff12c2ea5c"
    assert params["title"] == "ws-pair create"


def test_task_create_defaults_are_filled_from_context(captured_route, fixed_ctx):
    """缺省 0/"" 由服务端按当前项目补齐，不再走 falsy 隐式注入。"""
    _call_task_create({"title": "legacy implicit"})
    _, params, _ = captured_route[0]
    assert params["workspace_id"] == 1
    assert params["workspace_instance_id"] == "4baea3ff12c2ea5c"


def test_task_create_rejects_foreign_workspace_id(captured_route, fixed_ctx):
    """声明他项目 workspace_id → E_WORKSPACE_PROJECT_MISMATCH，且不发 RPC。"""
    text = _call_task_create({"title": "cross project", "workspace_id": 7})
    assert "E_WORKSPACE_PROJECT_MISMATCH" in text
    assert captured_route == []


def test_task_create_rejects_foreign_instance(captured_route, fixed_ctx):
    """声明他项目 workspace_instance_id → E_WORKSPACE_PROJECT_MISMATCH，且不发 RPC。"""
    text = _call_task_create({
        "title": "cross project",
        "workspace_instance_id": "inst-abc",
    })
    assert "E_WORKSPACE_PROJECT_MISMATCH" in text
    assert captured_route == []


def test_task_create_fails_closed_when_root_unresolved(captured_route, unresolved_ctx):
    """项目根无法解析权威配对 → E_WORKSPACE_ROOT_UNRESOLVED，且不发 RPC。"""
    text = _call_task_create({"title": "no authority"})
    assert "E_WORKSPACE_ROOT_UNRESOLVED" in text
    assert captured_route == []


def test_task_create_returns_dict_without_pydantic_rejection(captured_route, fixed_ctx):
    """dict 返回经 fastmcp convert_result 校验通过（旧 `-> str` 注解会拒绝）。"""
    text = _call_task_create({"title": "dict return"})
    assert TASK_ID in text
    assert "open" in text


def test_task_create_context_pair_fail_closed_on_daemon_unavailable(monkeypatch):
    """`_task_create_context_pair` 在 daemon 不可用时返回错误字典，不回退默认项目。"""
    def _boom(_root):
        raise DaemonUnavailableError("daemon down")

    monkeypatch.setattr(tools_task, "resolve_workspace_pair_from_daemon", _boom)
    monkeypatch.setattr(tools_task, "_resolve_mcp_project_root", lambda: "C:/tmp/proj")
    pair, err = tools_task._task_create_context_pair()
    assert pair is None
    assert err is not None
    assert err["code"] == "E_WORKSPACE_ROOT_UNRESOLVED"
    assert err["ok"] is False
    assert err["root"] == "C:/tmp/proj"
