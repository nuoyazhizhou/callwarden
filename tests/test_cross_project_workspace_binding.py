"""跨项目任务单归属 fail-closed 回归测试（CLI 侧见 test_task_create_workspace_binding.py）。

覆盖：
- MCP 项目上下文解析三级优先级（CALLWARDEN_WORKSPACE → cwd 探测 → PROJECT_ROOT）
- MCP task_create 服务端归属校验：他项目配对拒绝 / 缺失补齐 / 解析失败 fail-closed
- install-agent 项目级 MCP 条目钉住项目根，全局合并配置不钉
"""

import json
import os

import pytest

from callwarden.cli import main as climain
from callwarden.server import _mcp_common
from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.tools import tools_task


# ----------------------------------------------------------------------
# MCP 项目上下文解析
# ----------------------------------------------------------------------

def test_resolve_mcp_project_root_env_priority(monkeypatch, tmp_path):
    monkeypatch.setenv("CALLWARDEN_WORKSPACE", str(tmp_path))
    monkeypatch.setattr("callwarden.config.detect_project_root",
                        lambda _p: "/should/not/be/used")
    assert _mcp_common._resolve_mcp_project_root() == os.path.abspath(str(tmp_path))


def test_resolve_mcp_project_root_cwd_detection(monkeypatch, tmp_path):
    monkeypatch.delenv("CALLWARDEN_WORKSPACE", raising=False)
    monkeypatch.setattr("callwarden.config.detect_project_root",
                        lambda _p: str(tmp_path))
    assert _mcp_common._resolve_mcp_project_root() == os.path.abspath(str(tmp_path))


def test_resolve_mcp_project_root_fallback_package_root(monkeypatch):
    monkeypatch.delenv("CALLWARDEN_WORKSPACE", raising=False)
    monkeypatch.setattr("callwarden.config.detect_project_root", lambda _p: None)
    from callwarden.config import PROJECT_ROOT
    assert _mcp_common._resolve_mcp_project_root() == os.path.abspath(PROJECT_ROOT)


# ----------------------------------------------------------------------
# MCP task_create 服务端归属校验
# ----------------------------------------------------------------------

class _FakeMCP:
    """最小 FastMCP 替身：收集注册的工具函数。"""

    def __init__(self):
        self.tools = {}

    def tool(self):
        def decorator(fn):
            self.tools[fn.__name__] = fn
            return fn
        return decorator


def _register_tools():
    mcp = _FakeMCP()
    tools_task.register(mcp)
    return mcp.tools


def _patch_ctx(monkeypatch, pair, root="C:\\proj"):
    monkeypatch.setattr(tools_task, "_resolve_mcp_project_root", lambda: root)
    monkeypatch.setattr(
        tools_task, "resolve_workspace_pair_from_daemon", lambda r: dict(pair))


def test_task_create_rejects_cross_project_pair(monkeypatch):
    _patch_ctx(monkeypatch, {"workspace_id": 10,
                             "workspace_instance_id": "tokenslim-inst"})
    routed = []
    monkeypatch.setattr(tools_task, "_route",
                        lambda m, p, mode: routed.append((m, p)) or {})
    create = _register_tools()["task_create"]
    result = create(title="cross", workspace_id=1,
                    workspace_instance_id="callwarden-inst")
    assert result["ok"] is False
    assert result["code"] == "E_WORKSPACE_PROJECT_MISMATCH"
    assert routed == []


def test_task_create_rejects_cross_project_instance_only(monkeypatch):
    _patch_ctx(monkeypatch, {"workspace_id": 10,
                             "workspace_instance_id": "tokenslim-inst"})
    routed = []
    monkeypatch.setattr(tools_task, "_route",
                        lambda m, p, mode: routed.append((m, p)) or {})
    create = _register_tools()["task_create"]
    result = create(title="cross", workspace_instance_id="callwarden-inst")
    assert result["code"] == "E_WORKSPACE_PROJECT_MISMATCH"
    assert routed == []


def test_task_create_fills_missing_pair_from_server_context(monkeypatch):
    _patch_ctx(monkeypatch, {"workspace_id": 10,
                             "workspace_instance_id": "tokenslim-inst"})
    routed = []

    def fake_route(method, params, mode):
        routed.append((method, dict(params)))
        return {"task_id": "T-ok"}

    monkeypatch.setattr(tools_task, "_route", fake_route)
    create = _register_tools()["task_create"]
    result = create(title="mine")
    assert result == {"task_id": "T-ok"}
    assert routed[0][0] == "task.create"
    assert routed[0][1]["workspace_id"] == 10
    assert routed[0][1]["workspace_instance_id"] == "tokenslim-inst"


def test_task_create_unresolved_context_fails_closed(monkeypatch):
    monkeypatch.setattr(tools_task, "_resolve_mcp_project_root",
                        lambda: "C:\\unregistered")
    monkeypatch.setattr(
        tools_task, "resolve_workspace_pair_from_daemon",
        lambda r: (_ for _ in ()).throw(DaemonUnavailableError("matches=0")))
    routed = []
    monkeypatch.setattr(tools_task, "_route",
                        lambda m, p, mode: routed.append((m, p)) or {})
    create = _register_tools()["task_create"]
    result = create(title="nowhere", workspace_id=1,
                    workspace_instance_id="x")
    assert result["ok"] is False
    assert result["code"] == "E_WORKSPACE_ROOT_UNRESOLVED"
    assert routed == []


# ----------------------------------------------------------------------
# install-agent：项目级 pin，全局不 pin
# ----------------------------------------------------------------------

def test_mcp_entry_pins_workspace_only_when_requested():
    pinned = climain._mcp_callwarden_entry(r"C:\proj", pin_workspace=True)
    assert pinned["env"] == {"CALLWARDEN_WORKSPACE": os.path.abspath(r"C:\proj")}
    plain = climain._mcp_callwarden_entry(r"C:\proj")
    assert "env" not in plain


def test_global_mcp_config_not_pinned(monkeypatch, tmp_path):
    target = tmp_path / "mcp.json"
    monkeypatch.setattr(climain, "_global_mcp_path", lambda spec: str(target))
    climain._write_global_mcp_config(
        {"global_mcp_format": "merge_mcpServers"}, r"C:\proj", force=True)
    data = json.loads(target.read_text(encoding="utf-8"))
    entry = data["mcpServers"]["callwarden"]
    assert "env" not in entry, "全局配置被多项目共享，钉死 CALLWARDEN_WORKSPACE 会串项目"
