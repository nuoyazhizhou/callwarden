#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""深度轮:用深度前置工厂建真实实体,重跑 MCP + CLI,统计 EB→PASS 转化。"""
import asyncio
import json
import os
import subprocess
import sys

sys.path.insert(0, r"C:\git_work\callwarden")
sys.path.insert(0, r"C:\git_work\callwarden\tests\convergence")
os.environ["CW_DAEMON_TRANSPORT"] = "http"
os.environ["PYTHONUTF8"] = "1"

from deep_factory import build_deep_context

OUT = r"C:\git_work\callwarden\deliverables\software-company\doc-audit\deep_round_result.json"


async def run_mcp(ctx):
    from callwarden.server.mcp_server import create_mcp_server
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    import t2_mcp_runner as runner

    # MCP server 复用深度工厂已配置的单例 client(指向种子 workspace)
    mcp = create_mcp_server()
    HttpDaemonRpcClient.get_instance().configure_workspace(ctx.root)
    report = await runner.run_all(mcp, ctx, op_filter="ALL")
    return report


def run_cli(ctx):
    import t3_cli_runner as runner
    return runner.run_all(ctx, cwd=ctx.root, timeout=60)


def main():
    ctx, meta, _ = build_deep_context()
    print("FACTORY:", json.dumps(meta["ctx_summary"], ensure_ascii=False))

    mcp_report = asyncio.run(run_mcp(ctx))
    cli_report = run_cli(ctx)

    out = {
        "factory": meta["ctx_summary"],
        "factory_steps": [s for s in meta["steps"] if not s["ok"]],  # 只记失败的预建步骤
        "mcp": mcp_report["counts"],
        "cli": cli_report["counts"],
        "mcp_results": mcp_report["results"],
        "cli_results": cli_report["results"],
    }
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("DONE")
    print("MCP:", mcp_report["counts"])
    print("CLI:", cli_report["counts"])


main()
