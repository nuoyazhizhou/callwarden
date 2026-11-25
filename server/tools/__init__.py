"""按功能域注册全部 MCP 工具（保持 mcp_server.py 原行号顺序）

工具面 profile（参考 CodeGraph README L165-176 tool profiles）：
全量工具面会膨胀 agent 的 prompt context，经 ``CALLWARDEN_MCP_TOOL_PROFILE``
按功能域裁剪注册面。默认 ``all``（与冻结清单 244 工具一致，不变更契约）：

- ``core``：日常代码问答（query / workspace / summary）
- ``graph``：图谱分析（query / semantic / dependency_graph / summary）
- ``task``：任务协同（task / collab / identity / lease / task_prompt）
- ``security``：安全审计（security / rules）
- ``all``：全量（默认）

非法值 fail-closed（与 GitNexus 非法 env 启动失败同款）。
"""

import sys

from mcp.server.fastmcp import FastMCP

_MODULES = [
    "tools_query",
    "tools_workspace",
    "tools_semantic",
    "tools_task",
    "tools_summary",
    "tools_security",
    "tools_rules",
    "tools_collab",
    "tools_dependency_graph",
    "tools_identity",
    "tools_lease",
    "tools_task_prompt",
]

# profile → 模块白名单（"all" 不在此表，恒为全量）
_PROFILE_MODULES = {
    "core": {"tools_query", "tools_workspace", "tools_summary"},
    "graph": {"tools_query", "tools_semantic", "tools_dependency_graph", "tools_summary"},
    "task": {
        "tools_task", "tools_collab", "tools_identity",
        "tools_lease", "tools_task_prompt",
    },
    "security": {"tools_security", "tools_rules"},
}

_ENV_VAR = "CALLWARDEN_MCP_TOOL_PROFILE"


def active_profile() -> str:
    """读取并校验 CALLWARDEN_MCP_TOOL_PROFILE（默认 all）。"""
    raw = __import__("os").environ.get(_ENV_VAR, "").strip().lower() or "all"
    if raw != "all" and raw not in _PROFILE_MODULES:
        sys.stderr.write(
            f"[callwarden] {_ENV_VAR} 非法值 {raw!r}："
            f"可选 all/core/graph/task/security\n"
        )
        sys.stderr.flush()
        raise SystemExit(1)
    return raw


def modules_for_profile(profile: str) -> list:
    """给定 profile 应注册的模块名列表（保持 _MODULES 顺序）。"""
    if profile == "all":
        return list(_MODULES)
    allowed = _PROFILE_MODULES[profile]
    return [name for name in _MODULES if name in allowed]


def register_all(mcp: FastMCP, profile: str | None = None) -> None:
    profile = profile if profile is not None else active_profile()
    for mod_name in modules_for_profile(profile):
        mod = __import__(f"callwarden.server.tools.{mod_name}", fromlist=["register"])
        mod.register(mcp)
