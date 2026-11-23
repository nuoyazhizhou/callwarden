"""MCP HTTP 网关原型：把 callwarden 工具面暴露为 streamable HTTP MCP（对接 LoopForge 模式 A）。

调用链（入口 → 出口）：

    LoopForge MCPServerDefinition（transport=http, url, headers, timeout_sec）
      → HTTP POST <url>/mcp，Header: Authorization: Bearer <token>
        → BearerAuthMiddleware 校验 token（角色 / 项目 / 有效期，HMAC 签名）
          → FastMCP streamable HTTP（stateless + JSON 响应，路径 /mcp）
            → 工具函数（server/tools/*，注册面受 CALLWARDEN_MCP_TOOL_PROFILE 控制）
              → route_rpc（server/daemon_client.py，写操作幂等、workspace 权威注入）
                → install_response_budget 响应预算出口（tools/call 包装，超限截断）
                  → daemon RPC（mTLS，上游凭据只存在于网关进程，不进沙箱）

环境变量：
    CALLWARDEN_MCP_GATEWAY_HOST           监听地址，默认 127.0.0.1
    CALLWARDEN_MCP_GATEWAY_PORT           监听端口，默认 8765
    CALLWARDEN_MCP_TOOL_PROFILE           core | graph | security | all，默认 all
    CALLWARDEN_MCP_RESPONSE_BUDGET_BYTES  单次 tools/call 响应预算字节数，默认 262144
    CALLWARDEN_MCP_GATEWAY_SECRET         HMAC 密钥（base64 或原文）；
                                          缺省时使用/生成 ~/.callwarden/mcp_gateway_secret
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_PROFILE = "all"
DEFAULT_RESPONSE_BUDGET_BYTES = 262144
DEFAULT_TOKEN_TTL_SECONDS = 3600
DEFAULT_PROFILE_ENV = "CALLWARDEN_MCP_TOOL_PROFILE"
MCP_PATH = "/mcp"
HEALTH_PATH = "/health"
TOKEN_PREFIX = "v1"

# ---------------------------------------------------------------------------
# 工具面 profile：把 server/tools/_categories.py 的 17 个分类归并为四档。
# 真相源是 _categories.TOB_CATEGORIES（分类名 → 工具名清单），本表只做
# 分类 → 档位映射；"all" 恒等于全量注册（不做任何移除）。
# ---------------------------------------------------------------------------

PROFILE_CATEGORY_MAP: dict[str, tuple[str, ...]] = {
    # 日常开发主干：工作区/检索/健康度/任务编排/覆盖率归属/git
    "core": (
        "workspace_database",
        "query_search",
        "code_health",
        "task",
        "coverage_ownership",
        "git",
    ),
    # 图谱与诊断：调用链/依赖/构建上下文/诊断/数据治理
    "graph": (
        "call_chain",
        "diagnostics",
        "build_context",
        "dependency",
        "gc",
    ),
    # 治理与安全：规则记忆/审计引导/缺陷扫描/协作/任务租约/身份
    "security": (
        "rule_memory",
        "audit_bootstrap",
        "semgrep_defects",
        "collab",
        "assignment_lease",
        "identity",
    ),
}

_VALID_PROFILES = ("core", "graph", "security", "all")


def normalize_profile(profile: Optional[str]) -> str:
    """归一化 profile 名：空白/大小写容忍，未知档位 fail-closed。"""
    p = (profile or DEFAULT_PROFILE).strip().lower()
    if p not in _VALID_PROFILES:
        raise ValueError(
            f"E_MCP_PROFILE: 未知工具面 profile={profile!r}，"
            f"允许值 {_VALID_PROFILES}（环境变量 CALLWARDEN_MCP_TOOL_PROFILE）"
        )
    return p


def profile_tool_names(profile: Optional[str]) -> set[str]:
    """展开 profile 对应的工具名集合（基于 _categories 的分类真相源）。"""
    from .tools._categories import TOOL_CATEGORIES

    p = normalize_profile(profile)
    if p == "all":
        names: set[str] = set()
        for cat in TOOL_CATEGORIES:
            names.update(cat.tools)
        return names
    categories = PROFILE_CATEGORY_MAP[p]
    names = set()
    for cat in TOOL_CATEGORIES:
        if cat.key in categories:
            names.update(cat.tools)
    return names


def apply_profile(mcp: FastMCP, profile: Optional[str]) -> int:
    """按 profile 裁剪已注册的工具面，返回被移除的工具数。

    实现走 ToolManager 的注册表（_tools）：register_all 完成后调用本函数，
    不在 profile 档位内的工具直接 remove_tool，tools/list 即只暴露档位内工具。
    """
    allowed = profile_tool_names(profile)
    tool_manager = mcp._tool_manager
    registered = set(tool_manager._tools.keys())
    removed = 0
    for name in sorted(registered - allowed):
        mcp.remove_tool(name)
        removed += 1
    logger.info(
        "MCP 网关工具面 profile=%s：注册 %d，保留 %d，移除 %d",
        normalize_profile(profile), len(registered), len(registered) - removed, removed,
    )
    return removed


# ---------------------------------------------------------------------------
# Bearer token 签发/校验（对齐 lease token 思路：角色 + 项目 + 过期时间，
# HMAC-SHA256 签名，密钥不出网关进程）。
#
# token 形状：v1.<base64url(payload_json)>.<base64url(hmac_sha256(secret, body))>
# payload：{"role": str, "project": str, "exp": epoch_seconds, "nonce": hex}
# ---------------------------------------------------------------------------


def issue_token(
    role: str,
    project: str,
    ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS,
    secret: bytes | str | None = None,
    now: Optional[float] = None,
) -> str:
    """按角色/项目签发 Bearer token（HMAC 签名，带过期时间）。"""
    if not role or not isinstance(role, str):
        raise ValueError("E_MCP_TOKEN_ROLE: role 必须为非空字符串")
    if not project or not isinstance(project, str):
        raise ValueError("E_MCP_TOKEN_PROJECT: project 必须为非空字符串")
    secret_bytes = _coerce_secret(secret) if secret is not None else load_or_create_secret()
    issued_at = time.time() if now is None else now
    payload = {
        "role": role,
        "project": project,
        "exp": int(issued_at + ttl_seconds),
        "nonce": uuid.uuid4().hex,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).decode("ascii").rstrip("=")
    sig = base64.urlsafe_b64encode(
        hmac.new(secret_bytes, body.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    return f"{TOKEN_PREFIX}.{body}.{sig}"


def verify_token(
    token: str,
    secret: bytes | str | None = None,
    now: Optional[float] = None,
) -> tuple[bool, Optional[dict], str]:
    """校验 Bearer token：格式 → 签名 → 过期。

    返回 (ok, payload, reason)；ok=False 时 payload 为 None，reason 供审计日志。
    """
    if not token or not isinstance(token, str):
        return False, None, "token 为空"
    parts = token.strip().split(".")
    if len(parts) != 3 or parts[0] != TOKEN_PREFIX:
        return False, None, "token 格式非法（期望 v1.<body>.<sig>）"
    body, sig = parts[1], parts[2]
    secret_bytes = _coerce_secret(secret) if secret is not None else load_or_create_secret()
    expected = base64.urlsafe_b64encode(
        hmac.new(secret_bytes, body.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    if not hmac.compare_digest(expected, sig):
        return False, None, "token 签名不匹配"
    padded = body + "=" * (-len(body) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, UnicodeDecodeError):
        return False, None, "token payload 无法解码"
    if not isinstance(payload, dict):
        return False, None, "token payload 结构非法"
    current = time.time() if now is None else now
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or current >= float(exp):
        return False, None, "token 已过期或缺少有效 exp"
    if not payload.get("role") or not payload.get("project"):
        return False, None, "token 缺少 role/project"
    return True, payload, "ok"


def _coerce_secret(secret: bytes | str) -> bytes:
    if isinstance(secret, bytes):
        return secret
    return secret.encode("utf-8")


def load_or_create_secret(path: Optional[str | Path] = None) -> bytes:
    """加载网关 HMAC 密钥：环境变量优先，其次密钥文件（不存在则生成）。

    密钥只存放在本机（~/.callwarden/mcp_gateway_secret），上游 daemon 的
    mTLS 凭据不经过该文件，也不进 LoopForge 沙箱——沙箱侧只持有签发的
    Bearer token。
    """
    env_secret = os.environ.get("CALLWARDEN_MCP_GATEWAY_SECRET", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    secret_path = Path(path) if path else Path.home() / ".callwarden" / "mcp_gateway_secret"
    if secret_path.is_file():
        data = secret_path.read_bytes().strip()
        if data:
            return data
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    data = os.urandom(32)
    secret_path.write_bytes(data + b"\n")
    logger.info("MCP 网关密钥已生成：%s", secret_path)
    return data


def make_token_verifier(secret: bytes | str) -> Callable[[str], tuple[bool, Optional[dict], str]]:
    """构造 Authorization header 校验器（供 BearerAuthMiddleware 使用）。"""

    def verify(authorization_header: str) -> tuple[bool, Optional[dict], str]:
        header = (authorization_header or "").strip()
        if not header.lower().startswith("bearer "):
            return False, None, "缺少 Authorization: Bearer <token>"
        return verify_token(header[7:].strip(), secret)

    return verify


# ---------------------------------------------------------------------------
# 响应预算出口：tools/call 的统一出口包装。
# 任务描述所称的 enforce_budget 在现有 daemon_client.py 中并不存在（调研
# 结论），此处按同一语义在网关出口落地：单次工具调用结果序列化超过预算
# 即 fail-closed，不返回超限数据，改为返回预算说明 + 截断预览。
# ---------------------------------------------------------------------------


def enforce_budget(result: Any, max_bytes: int) -> tuple[Any, dict]:
    """响应预算检查：未超限原样返回；超限替换为预算说明（含截断预览）。

    FastMCP.call_tool 返回 (unstructured_contents, structured_result) 二元组，
    超限时保持该形状：unstructured 换成单条预算说明 TextContent，
    structured 换成预算信息 dict（fail-closed，不返回超限数据）。
    """
    raw = json.dumps(result, ensure_ascii=False, default=str).encode("utf-8")
    info = {
        "budget_bytes": max_bytes,
        "actual_bytes": len(raw),
        "exceeded": len(raw) > max_bytes,
    }
    if not info["exceeded"]:
        return result, info
    preview = raw[: max_bytes].decode("utf-8", errors="replace")
    budget_payload = {
        "budget_exceeded": True,
        "budget_bytes": max_bytes,
        "actual_bytes": len(raw),
        "preview": preview,
    }
    message = json.dumps(budget_payload, ensure_ascii=False)
    replacement = ([TextContent(type="text", text=message)], budget_payload)
    return replacement, info


def install_response_budget(mcp: FastMCP, max_bytes: int) -> None:
    """在 ToolManager.call_tool 出口安装预算包装（tools/call 统一出口）。

    FastMCP 在 __init__ 阶段已把 self.call_tool 的引用注册进低层 Server，
    因此包装点下移到 ToolManager.call_tool（运行时经实例属性查找，包装生效）。
    """
    tool_manager = mcp._tool_manager
    original_call_tool = tool_manager.call_tool

    async def call_tool_with_budget(name, arguments, context=None, convert_result=False):
        result = await original_call_tool(
            name, arguments, context=context, convert_result=convert_result
        )
        budgeted, info = enforce_budget(result, max_bytes)
        if info["exceeded"]:
            logger.warning(
                "MCP 网关响应预算超限：tool=%s actual=%d budget=%d（已截断）",
                name, info["actual_bytes"], info["budget_bytes"],
            )
        return budgeted

    tool_manager.call_tool = call_tool_with_budget


# ---------------------------------------------------------------------------
# LoopForge MCPServerDefinition 形状解析（模式 A 对接契约）。
# ---------------------------------------------------------------------------


def parse_mcp_server_definition(definition: dict) -> dict:
    """校验 LoopForge MCPServerDefinition 形状并提取网关客户端配置。

    期望形状（模式 A：external MCP HTTP）：
        {
          "name": "callwarden",
          "transport": "http",
          "url": "http://127.0.0.1:8765/mcp",
          "headers": {"Authorization": "Bearer <代理token>"},
          "timeout_sec": 30
        }
    """
    if not isinstance(definition, dict):
        raise ValueError("E_MCP_DEF_TYPE: MCPServerDefinition 必须为 JSON 对象")
    transport = str(definition.get("transport", "")).strip().lower()
    if transport != "http":
        raise ValueError(
            f"E_MCP_DEF_TRANSPORT: 仅支持 transport=http（模式 A），得到 {transport!r}"
        )
    url = str(definition.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("E_MCP_DEF_URL: url 必须为 http(s) 绝对地址")
    headers = definition.get("headers") or {}
    if not isinstance(headers, dict):
        raise ValueError("E_MCP_DEF_HEADERS: headers 必须为字符串映射")
    authorization = ""
    for key, value in headers.items():
        if str(key).lower() == "authorization":
            authorization = str(value)
            break
    if not authorization.startswith("Bearer "):
        raise ValueError(
            "E_MCP_DEF_AUTH: headers 必须携带 Authorization: Bearer <代理token>"
        )
    timeout_sec = definition.get("timeout_sec", 30)
    try:
        timeout_sec = float(timeout_sec)
    except (TypeError, ValueError):
        raise ValueError("E_MCP_DEF_TIMEOUT: timeout_sec 必须为数字")
    if timeout_sec <= 0:
        raise ValueError("E_MCP_DEF_TIMEOUT: timeout_sec 必须为正数")
    return {
        "name": str(definition.get("name", "")).strip(),
        "url": url,
        "token": authorization[7:].strip(),
        "timeout_sec": timeout_sec,
    }


# ---------------------------------------------------------------------------
# ASGI 组装：Bearer 认证中间件 + FastMCP streamable HTTP app + /health。
# ---------------------------------------------------------------------------


class BearerAuthMiddleware:
    """纯 ASGI 中间件：校验 Authorization: Bearer <token> 后放行。

    - 非 http scope（lifespan）原样透传，保证 FastMCP session manager 正常启停；
    - /health 豁免认证，供 daemon/LoopForge 健康检查；
    - 校验通过后把 token payload（role/project/exp）写入 scope["state"]，
      供后续审计使用；
    - 校验失败返回 401 JSON（fail-closed，不透传任何工具面信息）。
    """

    def __init__(
        self,
        app,
        verifier: Callable[[str], tuple[bool, Optional[dict], str]],
        exempt_paths: tuple[str, ...] = (HEALTH_PATH,),
    ):
        self.app = app
        self.verifier = verifier
        self.exempt_paths = exempt_paths

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        path = scope.get("path", "")
        if path in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        headers = {k.lower(): v for k, v in (scope.get("headers") or [])}
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        ok, payload, reason = self.verifier(authorization)
        if not ok:
            logger.warning("MCP 网关认证失败：path=%s reason=%s", path, reason)
            body = json.dumps(
                {"error": "unauthorized", "reason": reason}, ensure_ascii=False
            ).encode("utf-8")
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json; charset=utf-8"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            })
            await send({"type": "http.response.body", "body": body})
            return
        state = scope.setdefault("state", {})
        state["mcp_identity"] = {
            "role": payload.get("role"),
            "project": payload.get("project"),
            "exp": payload.get("exp"),
        }
        await self.app(scope, receive, send)


async def _health_endpoint(request):
    """网关健康检查（豁免认证）。"""
    return JSONResponse({"ok": True, "service": "callwarden-mcp-gateway"})


def create_gateway_app(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    profile: Optional[str] = None,
    secret: bytes | str | None = None,
    budget_bytes: int = DEFAULT_RESPONSE_BUDGET_BYTES,
    workspace_root: Optional[str] = None,
    bind_workspace: bool = True,
) -> tuple[Any, FastMCP]:
    """组装网关 ASGI app：(wrapped_app, fastmcp)。

    步骤：FastMCP(stateless + JSON) → register_all 注册全量工具面 →
    apply_profile 裁剪 → install_response_budget 预算出口 →
    streamable_http_app + /health → BearerAuthMiddleware 包裹。

    bind_workspace=False 供单测使用：跳过 daemon workspace 绑定（不产生
    daemon 侧 register 副作用）。
    """
    resolved_profile = normalize_profile(profile or os.environ.get(DEFAULT_PROFILE_ENV, DEFAULT_PROFILE))
    mcp = FastMCP(
        "callwarden-gateway",
        host=host,
        port=port,
        stateless_http=True,
        json_response=True,
        instructions="callwarden 代码治理工具面（streamable HTTP 网关原型）",
    )

    # 与 stdio 入口（create_mcp_server）同语义：http transport 启用时把
    # 网关进程的 workspace 绑定到 daemon（route_rpc 的权威注入依赖它）。
    if bind_workspace:
        from .daemon_client import HttpDaemonRpcClient, is_http_transport_enabled

        if is_http_transport_enabled():
            if workspace_root:
                HttpDaemonRpcClient.get_instance().configure_workspace(workspace_root)
            else:
                from ._mcp_common import _resolve_mcp_project_root

                HttpDaemonRpcClient.get_instance().configure_workspace(
                    _resolve_mcp_project_root()
                )

    from .tools import register_all

    register_all(mcp)
    apply_profile(mcp, resolved_profile)
    install_response_budget(mcp, budget_bytes)

    app: Starlette = mcp.streamable_http_app()
    app.router.routes.append(Route(HEALTH_PATH, endpoint=_health_endpoint))

    verifier = make_token_verifier(secret if secret is not None else load_or_create_secret())
    wrapped = BearerAuthMiddleware(app, verifier)
    return wrapped, mcp


def main(argv: Optional[list[str]] = None) -> None:
    """网关进程入口：解析环境变量/参数后用 uvicorn 拉起 streamable HTTP 服务。"""
    parser = argparse.ArgumentParser(description="callwarden MCP HTTP 网关（streamable HTTP 原型）")
    parser.add_argument("--host", default=os.environ.get("CALLWARDEN_MCP_GATEWAY_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port", type=int,
        default=int(os.environ.get("CALLWARDEN_MCP_GATEWAY_PORT", str(DEFAULT_PORT))),
    )
    parser.add_argument("--profile", default=os.environ.get(DEFAULT_PROFILE_ENV, DEFAULT_PROFILE))
    parser.add_argument(
        "--budget-bytes", type=int,
        default=int(os.environ.get("CALLWARDEN_MCP_RESPONSE_BUDGET_BYTES", str(DEFAULT_RESPONSE_BUDGET_BYTES))),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # daemon 探活自愈（后台线程，fail-soft；与 stdio 入口共用实现）。
    try:
        from .mcp_server import _launch_daemon_startup_probe

        _launch_daemon_startup_probe()
    except Exception as exc:  # noqa: BLE001 - 探活失败不阻断网关启动
        logger.warning("daemon 启动探针未拉起：%s", exc)

    app, _mcp = create_gateway_app(
        host=args.host,
        port=args.port,
        profile=args.profile,
        budget_bytes=args.budget_bytes,
    )
    logger.info(
        "MCP HTTP 网关启动：http://%s:%d%s profile=%s budget_bytes=%d",
        args.host, args.port, MCP_PATH, args.profile, args.budget_bytes,
    )
    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
