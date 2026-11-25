"""MCP HTTP 网关验收证据脚本（对接 LoopForge 模式 A）。

流程：
1. 起网关子进程（streamable HTTP，profile=all，随机端口，本次专用 HMAC 密钥）；
2. 用 LoopForge MCPServerDefinition 形状构造对接配置并经
   parse_mcp_server_definition 校验（transport=http + url + Bearer + timeout_sec）；
3. MCP client 连接：initialize → tools/list（拉全量工具清单）；
4. tools/call search_symbols 定位 review_readiness 符号（route_rpc 自动完成
   workspace 注册与快照发布）；
5. tools/call review_readiness 跑通一次审查就绪查询；
6. 证据 JSON 落盘 tests/acceptance/mcp/evidence/mcp_http_gateway_evidence.json，
   并附整体 sha256（manifest/hash 留存）。

运行：python tests/acceptance/mcp/mcp_http_gateway_evidence.py
"""

import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from _editable_rebind import rebind_callwarden_mapping  # noqa: E402

rebind_callwarden_mapping()

from callwarden.server import mcp_http_gateway as gw  # noqa: E402

EVIDENCE_PATH = ROOT / "tests" / "acceptance" / "mcp" / "evidence" / "mcp_http_gateway_evidence.json"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 60.0) -> None:
    import urllib.request

    deadline = time.time() + timeout
    url = f"http://127.0.0.1:{port}/health"
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    raise RuntimeError("网关 /health 未在超时内就绪")


def _parse_json_blocks(mcp_result) -> list:
    """把 MCP 结果的 TextContent 块逐块解析为 JSON 对象列表（daemon 逐符号返回）。"""
    items = []
    for block in mcp_result.content or []:
        text = getattr(block, "text", None)
        if text and text.strip().startswith("{"):
            items.append(json.loads(text))
    return items


def _pick_review_readiness_hash(items: list) -> tuple[str, str]:
    """从符号列表中定位 review_readiness 符号，返回 (symbol_hash, qualified_name)。"""
    for item in items:
        if item.get("name") == "review_readiness" and item.get("symbol_hash"):
            return str(item["symbol_hash"]), str(item.get("qualified_name", ""))
    for item in items:
        if item.get("symbol_hash"):
            return str(item["symbol_hash"]), str(item.get("qualified_name", ""))
    raise RuntimeError(f"符号列表中未找到 symbol_hash：{json.dumps(items[:3], ensure_ascii=False)[:800]}")


def _result_payload(mcp_result) -> dict:
    """优先取 structuredContent，退化解析文本块 JSON。"""
    structured = dict(mcp_result.structuredContent or {})
    if structured:
        return structured
    texts = [getattr(b, "text", "") for b in (mcp_result.content or [])]
    for text in texts:
        if text and text.strip().startswith("{"):
            return json.loads(text)
    return {"content_text": "\n".join(t for t in texts if t)[:2000]}


async def _run_client(cfg: dict) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {cfg['token']}"}
    async with streamablehttp_client(cfg["url"], headers=headers, timeout=cfg["timeout_sec"]) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            tools = await session.list_tools()
            tool_names = sorted(t.name for t in tools.tools)

            search = await session.call_tool(
                "search_symbols", {"query": "review_readiness", "limit": 5}
            )
            search_items = _parse_json_blocks(search)

            # search_symbols 不含 hash；用 get_file_symbols 定位目标符号的 symbol_hash
            file_symbols = await session.call_tool(
                "get_file_symbols", {"file_path": "c:/git_work/callwarden/server/tools/tools_summary.py"}
            )
            symbol_hash, target_qn = _pick_review_readiness_hash(_parse_json_blocks(file_symbols))

            readiness = await session.call_tool("review_readiness", {"symbol_hash": symbol_hash})
            readiness_structured = json.loads(
                json.dumps(_result_payload(readiness), ensure_ascii=False, default=str)
            )

            return {
                "initialize": {
                    "server_name": init.serverInfo.name,
                    "protocol_version": str(init.protocolVersion),
                },
                "tool_count": len(tool_names),
                "tools_head": tool_names[:12],
                "search_symbols": {
                    "query": "review_readiness",
                    "returned": len(search_items),
                },
                "target_symbol": {
                    "qualified_name": target_qn,
                    "symbol_hash": symbol_hash,
                },
                "review_readiness": readiness_structured,
            }


def main() -> int:
    # env 中的密钥按原文 UTF-8 编码使用（load_or_create_secret 的约定），
    # 父进程签发 token 与子进程校验必须持同一份字节。
    secret = os.urandom(32).hex().encode("utf-8")
    port = _free_port()
    launcher = (
        "import sys; sys.path.insert(0, r'{root}'); "
        "from _editable_rebind import rebind_callwarden_mapping; rebind_callwarden_mapping(); "
        "from callwarden.server.mcp_http_gateway import main; "
        "main(['--port', '{port}', '--profile', 'all'])"
    ).format(root=ROOT, port=port)
    env = {
        **os.environ,
        "CALLWARDEN_MCP_GATEWAY_SECRET": secret.decode("utf-8"),
        "CW_DAEMON_TRANSPORT": "http",
        # 网关进程 workspace 锚点：daemon 只解析约定实例（主仓根），
        # 从 worktree 启动时须显式指回主仓（_resolve_mcp_project_root 优先级 1）。
        "CALLWARDEN_WORKSPACE": r"c:\git_work\callwarden",
    }
    proc = subprocess.Popen(
        [sys.executable, "-c", launcher],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_health(port)
        token = gw.issue_token("harness", "callwarden", ttl_seconds=1800, secret=secret)
        # LoopForge MCPServerDefinition 形状（模式 A：external MCP HTTP）
        definition = {
            "name": "callwarden",
            "transport": "http",
            "url": f"http://127.0.0.1:{port}/mcp",
            "headers": {"Authorization": f"Bearer {token}"},
            "timeout_sec": 60,
        }
        cfg = gw.parse_mcp_server_definition(definition)
        evidence = asyncio.run(_run_client(cfg))
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    payload = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "gateway": {
            "transport": "streamable-http",
            "host": "127.0.0.1",
            "port": port,
            "profile": "all",
            "response_budget_bytes": gw.DEFAULT_RESPONSE_BUDGET_BYTES,
        },
        "loopforge_mcps_server_definition": {
            **definition,
            "headers": {"Authorization": "Bearer ***"},
        },
        **evidence,
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    payload["sha256"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    EVIDENCE_PATH.parent.mkdir(parents=True, exist_ok=True)
    EVIDENCE_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("EVIDENCE_OK", EVIDENCE_PATH)
    print("tool_count:", payload["tool_count"])
    print("review_readiness keys:", sorted(payload["review_readiness"].keys()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
