"""TP-08M MCP wire 集合/schema 提取器。

提取方式（真实子进程 wire，非进程内 manager）：
- 以真实子进程启动 `cw.py server`（FastMCP stdio 传输），经 mcp 官方客户端
  `stdio_client` + `ClientSession` 说 wire 协议：`initialize` → `list_tools`
  （按 `nextCursor` 翻页直至穷尽）→ 断开。
- 逐工具提取 name / title / description / inputSchema / outputSchema。

startup 隔离（沿用 TP-07 教训）：MCP server 子进程的 USERPROFILE / HOME /
CALLWARDEN_HOME / CALLWARDEN_DB / CALLWARDEN_DAEMON_REGISTRY_DB 全部指向 run 根，
避免 server 启动时发现生产 daemon 或写生产 manifest / db。

输出：docs/testing/manifests/mcp/mcp_tools.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

REPO_ROOT = Path(__file__).resolve().parents[4]
OUT = REPO_ROOT / "docs" / "testing" / "manifests" / "mcp" / "mcp_tools.json"
RUN_ROOT = Path(os.environ.get(
    "CW_TP08M_RUN_ROOT", r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-08M"))
PYTHON = sys.executable
# TP-06 冻结 daemon 候选（sha256 6324df12…）。MCP server 启动时 daemon_autostart
# 会按 `_find_daemon_binary()` 解析并可能 spawn 一个 daemon；必须把它钉到本候选，
# 否则 ambient 环境里的 CW_DAEMON_BIN（本机指向生产 runtime/current）会让 server
# 去启动**生产 daemon**——这正是「禁止 production 发现」要堵的漏洞。
DAEMON_CANDIDATE = Path(os.environ.get(
    "CW_TP06_DAEMON_EXE",
    r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-06/src/rust_ext/target/release/cw-daemon.exe"))
TP06_DAEMON_SHA256 = "6324df120c850e45cee43349e3353e2fb88808e436127cbf9025c9e0a0e8859d"


def isolated_env() -> dict:
    env = dict(os.environ)
    home = RUN_ROOT / "home"
    (home / ".callwarden").mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["CALLWARDEN_HOME"] = str(home)
    env["CALLWARDEN_DB"] = str(home / ".callwarden" / "callwarden.db")
    env["CALLWARDEN_DAEMON_REGISTRY_DB"] = str(home / ".callwarden" / "daemon" / "registry.db")
    # 不让 server 发现/连生产 daemon（manifest 目录已在 run 根）
    for k in ("CW_DAEMON_TRANSPORT", "CW_DAEMON_HTTP_BIND", "CW_DAEMON_BINARY"):
        env.pop(k, None)
    # 钉死 autostart 目标为 TP-06 冻结候选；ambient CW_DAEMON_BIN 常指向生产
    # runtime/current，必须显式覆盖
    env["CW_DAEMON_BIN"] = str(DAEMON_CANDIDATE)
    env["CALLWARDEN_LANG"] = "en_US"  # 固定语言，提取结果可复现
    return env


def tool_dict(tool) -> dict:
    return {
        "name": tool.name,
        "title": getattr(tool, "title", None),
        "description": (tool.description or "")[:600],
        "inputSchema": tool.inputSchema,
        "outputSchema": getattr(tool, "outputSchema", None),
        "annotations": (tool.annotations.model_dump(exclude_none=True)
                        if getattr(tool, "annotations", None) else None),
    }


def schema_stats(schema: dict | None) -> dict:
    if not isinstance(schema, dict):
        return {"present": False}
    props = schema.get("properties") or {}
    return {
        "present": True,
        "type": schema.get("type"),
        "property_count": len(props),
        "required": schema.get("required") or [],
        "properties": sorted(props.keys()),
    }


async def extract() -> dict:
    params = StdioServerParameters(
        command=PYTHON,
        args=[str(REPO_ROOT / "cw.py"), "server"],
        env=isolated_env(),
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = []
            cursor = None
            pages = 0
            seen: set[str] = set()
            duplicates: list[str] = []
            while True:
                pages += 1
                result = await session.list_tools(cursor=cursor)
                for t in result.tools:
                    if t.name in seen:
                        duplicates.append(t.name)
                    seen.add(t.name)
                    tools.append(tool_dict(t))
                cursor = result.nextCursor
                if not cursor:
                    break
                if pages > 50:  # 翻页保护
                    raise RuntimeError("pagination exceeded 50 pages")

    return {
        "schema": "mcp-tools/v1",
        "card": "TP-08M",
        "source_revision": _head(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "transport": "stdio (真实子进程 cw.py server)",
        "isolation": {
            "daemon_bin_pinned": str(DAEMON_CANDIDATE),
            "daemon_bin_sha256_expected": TP06_DAEMON_SHA256,
            "popped": ["CW_DAEMON_TRANSPORT", "CW_DAEMON_HTTP_BIND", "CW_DAEMON_BINARY"],
            "overridden": ["USERPROFILE", "HOME", "CALLWARDEN_HOME",
                           "CALLWARDEN_DB", "CALLWARDEN_DAEMON_REGISTRY_DB", "CW_DAEMON_BIN"],
            "note": "ambient CW_DAEMON_BIN 指向生产 runtime/current，不覆盖则 MCP server "
                    "启动时会去启动生产 daemon（_find_daemon_binary 第 1 优先级）",
        },
        "initialize": {
            "protocolVersion": getattr(init, "protocolVersion", None),
            "serverName": (init.serverInfo.name if init.serverInfo else None),
            "serverVersion": (init.serverInfo.version if init.serverInfo else None),
            "instructions_present": bool(getattr(init, "instructions", "")),
        },
        "tool_count": len(tools),
        "pagination_pages": pages,
        "duplicate_names": duplicates,
        "tools": tools,
        "schema_summary": {
            t["name"]: schema_stats(t["inputSchema"]) for t in tools
        },
    }


def _head() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                       capture_output=True, text=True)
    return r.stdout.strip()


def main() -> int:
    import anyio

    (RUN_ROOT / "reports").mkdir(parents=True, exist_ok=True)
    log = RUN_ROOT / "reports" / "mcp_extract.log"
    t0 = time.time()
    data = anyio.run(extract)
    dt = time.time() - t0
    data["elapsed_seconds"] = round(dt, 2)

    bad = [t["name"] for t in data["tools"]
           if not isinstance(t["inputSchema"], dict) or not t["inputSchema"]]
    data["tools_without_input_schema"] = bad

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes((json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    log.write_text(json.dumps({"elapsed": dt, "tool_count": data["tool_count"],
                               "pages": data["pagination_pages"]},
                              indent=1), encoding="utf-8")
    print(f"tools={data['tool_count']} pages={data['pagination_pages']} "
          f"dups={len(data['duplicate_names'])} no_schema={len(bad)} elapsed={dt:.1f}s")
    print(f"written: {OUT} ({OUT.stat().st_size} bytes)")
    if data["duplicate_names"]:
        print("DUPLICATES:", data["duplicate_names"][:10])
    if bad:
        print("NO INPUT SCHEMA:", bad[:10])
    return 0


if __name__ == "__main__":
    sys.exit(main())
