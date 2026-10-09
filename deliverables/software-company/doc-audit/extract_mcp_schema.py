#!/usr/bin/env python3
"""extract_mcp_schema.py —— 提取 243 个 MCP 工具的完整 inputSchema + description。

用于 T1 全量测试基建:全参数测试需要每个工具的完整参数定义(必填/可选/类型)。
经 fastmcp 的 create_mcp_server + list_tools 提取(工具由 @mcp.tool() 从函数签名+docstring 生成)。

设 CALLWARDEN_SKIP_AUTO_SETUP=1 + 不调 main() 避免 daemon probe / semgrep 预下载副作用。
"""
from __future__ import annotations
import asyncio
import json
import os
import sys

os.environ["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
os.environ.setdefault("PYTHONUTF8", "1")
# 提取 schema 不需要连 daemon;用 local 避免 configure_workspace 触发 register 阻塞
os.environ["CW_DAEMON_TRANSPORT"] = "named-pipe"  # 非 http,create_mcp_server 跳过 configure

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
sys.path.insert(0, os.path.dirname(_REPO))

_OUT = os.path.join(_HERE, "mcp_full_schema.json")


def main():
    import importlib
    pkg = os.path.basename(_REPO)
    mcp_server = importlib.import_module(f"{pkg}.server.mcp_server")
    mcp = mcp_server.create_mcp_server()

    tools = asyncio.run(mcp.list_tools())
    out = []
    for t in tools:
        schema = t.inputSchema or {}
        props = schema.get("properties", {})
        required = schema.get("required", [])
        out.append({
            "name": t.name,
            "description": t.description or "",
            "required": required,
            "params": {
                pname: {
                    "type": pinfo.get("type"),
                    "default": pinfo.get("default", None),
                    "title": pinfo.get("title"),
                    "description": pinfo.get("description", ""),
                }
                for pname, pinfo in props.items()
            },
        })
    out.sort(key=lambda x: x["name"])
    json.dump({"total": len(out), "tools": out}, open(_OUT, "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print(f"提取 {len(out)} 个 MCP 工具 schema -> {_OUT}")
    # 抽样
    for x in out[:2]:
        print(f"  {x['name']}: required={x['required']} params={list(x['params'].keys())}")
    # 统计:有必填参数的工具数
    with_required = sum(1 for x in out if x["required"])
    print(f"  有必填参数的工具: {with_required} / 无参工具: {sum(1 for x in out if not x['params'])}")


if __name__ == "__main__":
    main()
