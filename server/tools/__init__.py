"""按功能域注册全部 MCP 工具（保持 mcp_server.py 原行号顺序）"""
from typing import List

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
    "tools_p2_graph",
    "tools_p3_identity",
    "tools_p4_lease",
]


def register_all(mcp: FastMCP) -> None:
    for mod_name in _MODULES:
        mod = __import__(f"callwarden.server.tools.{mod_name}", fromlist=["register"])
        mod.register(mcp)
