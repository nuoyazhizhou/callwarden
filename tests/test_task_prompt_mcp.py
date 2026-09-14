# -*- coding: utf-8 -*-
"""RP-08 MCP thin client 单元测试（mock daemon client，不依赖 live daemon）

覆盖卡 T-1788710667638-0e8eb8b8 acceptance：
- task_get_role_prompt 只接受 task_id（spec §4.4：无 role/format/workspace/
  credential/lease 参数）；
- 精确透传 daemon RPC task.prompt.compile（READ_ONLY，参数原样）；
- daemon 不可达 fail-closed（DaemonUnavailableError 向上传播，无本地 fallback）；
- 薄壳纯净度（无 sqlite3/get_db/db 业务模块 import，check_client_purity HARD 纪律）；
- 注册面：FastMCP register 后工具名 task_get_role_prompt 存在。

注：live RPC round-trip 需部署含 RP-05/RP-08 的新 daemon（当前 974bddcf 旧构建），
按卡片 scope 本测试面以 mock 为准，live probe 由部署后验证承接。
"""
from __future__ import annotations

import ast
import importlib
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp.server.fastmcp import FastMCP  # noqa: E402

import callwarden.server.tools.tools_task_prompt as ttp  # noqa: E402
from callwarden.server.daemon_client import DaemonUnavailableError  # noqa: E402

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULE_PATH = os.path.join(_REPO_ROOT, "server", "tools", "tools_task_prompt.py")


def _registered_fn():
    """经真实 FastMCP 注册路径取得底层函数（验证 register 面与签名）。"""
    mcp = FastMCP("rp08-test")
    ttp.register(mcp)
    tools = mcp._tool_manager._tools
    assert "task_get_role_prompt" in tools, "register 后必须存在 task_get_role_prompt"
    return tools["task_get_role_prompt"].fn


def test_signature_only_task_id() -> None:
    """spec §4.4：工具不接受 role/format/workspace/credential/lease 参数。"""
    fn = _registered_fn()
    sig = inspect.signature(fn)
    params = list(sig.parameters)
    assert params == ["task_id"], f"只允许 task_id 参数，实际: {params}"
    # 显式禁止参数名不得以默认值形式混入
    for banned in ("role", "format", "workspace", "credential", "lease",
                   "workspace_instance_id", "workspace_id"):
        assert banned not in params


def test_forwards_exact_rpc_method_and_params(monkeypatch) -> None:
    """透传 task.prompt.compile + 原样 task_id 参数 + READ_ONLY。"""
    captured = {}

    def fake_route(rpc_method, params, op_class="READ_ONLY"):
        captured["rpc_method"] = rpc_method
        captured["params"] = params
        captured["op_class"] = op_class
        return {"schema_version": "role_prompt_bundle_v1", "task_id": params["task_id"]}

    monkeypatch.setattr(ttp, "_route", fake_route)
    fn = _registered_fn()
    result = fn(task_id="T-1788710667638-0e8eb8b8")
    assert captured["rpc_method"] == "task.prompt.compile"
    assert captured["params"] == {"task_id": "T-1788710667638-0e8eb8b8"}
    assert captured["op_class"] == "READ_ONLY"
    assert result["schema_version"] == "role_prompt_bundle_v1"


def test_fail_closed_no_local_fallback(monkeypatch) -> None:
    """daemon 不可达时 DaemonUnavailableError 原样向上传播（无 fallback）。"""
    def failing_route(rpc_method, params, op_class="READ_ONLY"):
        raise DaemonUnavailableError("E_HTTP_DAEMON_UNAVAILABLE: fail-closed")

    monkeypatch.setattr(ttp, "_route", failing_route)
    fn = _registered_fn()
    with pytest.raises(DaemonUnavailableError):
        fn(task_id="T-1788710667638-0e8eb8b8")


def test_no_blind_get_role_view_reuse() -> None:
    """§4.4：不复用 blind get_role_view（AST 调用级：不得出现对该名的调用）。"""
    import ast as _ast

    tree = _ast.parse(open(_MODULE_PATH, encoding="utf-8").read())
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Call):
            name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
            assert name != "get_role_view", "禁止调用 blind get_role_view"


def test_purity_no_sqlite_no_db_imports() -> None:
    """薄壳纯净度（check_client_purity HARD gate 纪律）：AST 级断言。"""
    tree = ast.parse(open(_MODULE_PATH, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {a.name for a in node.names}
            assert not any(n.startswith("sqlite3") for n in names), "禁止 sqlite3"
            assert not any(n.startswith("callwarden.db") for n in names), "禁止 db 模块"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("sqlite3"), "禁止 sqlite3"
            assert not mod.startswith("callwarden.db"), "禁止 db 业务模块"
            assert "get_db" not in {a.name for a in node.names}, "禁止 get_db"


def test_module_is_thin_single_tool() -> None:
    """模块只注册一个工具（单一薄工具原则，§4.4）。"""
    mcp = FastMCP("rp08-test-single")
    ttp.register(mcp)
    tools = mcp._tool_manager._tools
    assert set(tools) == {"task_get_role_prompt"}
