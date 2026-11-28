"""Web 观测面网关（只读）—— T-1795469778065-1686c8a0 Step 1 / S-1795469778070-16d9e468。

callwarden 第一个面向人类的 HTTP 观测面网关。定位是**观测面，不是操控台**：

- 只读：四个 GET 端点，全部数据经 ``server/daemon_client.py`` 的
  ``route_rpc`` / ``route_task_read`` / ``resolve_workspace_pair_from_daemon``
  获取。本文件不打开任何 SQLite 连接、不直连任务库、不实现任何写动作。
- 不复用 daemon mTLS 面：浏览器只连本网关（默认 ``127.0.0.1:8765``，普通 HTTP，
  localhost 是浏览器安全上下文）。daemon ``/v1/rpc`` 的 mTLS 客户端证书等于
  agent 全权，绝不发给浏览器。
- 读模型合同：``docs/design/web-observer-read-model.md``（Step 0）。
  端点与 RPC 的对应关系、workspace 双参数要求、governance_blocked 可见性
  规则、fail-closed 降级行为均以该文档为准。
- 依赖仅 starlette / uvicorn（已随 ``mcp[rich]`` 安装），零新增第三方依赖；
  前端为 ``server/web_static/index.html`` 单文件，无构建链。

启动::

    python -m callwarden.server.web_gateway [--host H] [--port P] [--root DIR] [--token T]

- ``--root``（默认当前目录）：进程会 ``chdir`` 到该目录——daemon 侧 workspace
  权威解析按 cwd/root_path 匹配（``mcp.daemon_client.inject_workspace_id``），
  cwd 错 = 绑定错，因此入口强制对齐。
- 默认只绑 loopback。显式 ``--host`` 指向非 loopback 地址时必须携带 ``--token``：
  本卡仅保留参数位（token 校验由后续认证卡实现，当前版本 token 不产生实际
  鉴权），服务会打印显著警告——观测面在完成认证卡之前不得对局域网暴露。
- fail-closed：workspace 解析不出唯一绑定 / daemon 不可达时，端点返回 503 +
  结构化错误（``{"error": {"code", "message"}}``），绝不猜测 workspace。
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    resolve_workspace_pair_from_daemon,
    route_rpc,
    route_task_read,
)
from callwarden.server.daemon_protocol import DaemonRemoteError

# ---------------------------------------------------------------- 常量

WEB_STATIC_DIR = Path(__file__).resolve().parent / "web_static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# dashboard 默认口径：quick 模式（约 280ms@100K 文件），不开循环检测/演化趋势。
DASHBOARD_DEFAULTS = {
    "quick": True,
    "with_cycles": False,
    "with_evolution": False,
    "top_n": 5,
}

# task.list 行内嵌治理投影（tree_governance_projection）已覆盖列表可见性；
# task.next_action（派工 evaluator，含 assignment/claim_requirements/identity_policy）
# 属下钻详情，仅在单卡端点调用。终态卡不再请求派工投影（GOV-FIX-04 短路）。
_TERMINAL_TASK_STATUSES = frozenset({"applied", "closed", "reverted", "superseded"})

# 非法查询参数钳制范围
_MIN_LIMIT, _MAX_LIMIT = 1, 500

# 观测面禁止本地回退：fallback_func 仅在 local/legacy（测试模式）下会被
# route_task_read 调用；生产模式 task.* 读不可用即 fail-closed 上抛。
def _no_local_fallback():
    raise DaemonUnavailableError(
        "web_gateway 禁止本地回退：daemon 不可达即显示错误态（fail-closed）"
    )


# ---------------------------------------------------------------- 工具

def _code_of(exc: BaseException) -> str:
    if isinstance(exc, DaemonRemoteError):
        return exc.code
    if isinstance(exc, DaemonUnavailableError):
        return getattr(exc, "code", "") or "E_HTTP_DAEMON_UNAVAILABLE"
    return "E_WEB_GATEWAY_INTERNAL"


def _error_payload(exc: BaseException) -> dict:
    return {"error": {"code": _code_of(exc), "message": str(exc)}}


def _status_for(exc: BaseException) -> int:
    if isinstance(exc, DaemonUnavailableError):
        return 503
    if isinstance(exc, DaemonRemoteError):
        if exc.code in ("invalid_params", "E_INVALID_PARAMS"):
            return 400
        if exc.code in ("task_not_found", "not_found"):
            return 404
        return 502
    return 500


def _json_error(exc: BaseException) -> JSONResponse:
    return JSONResponse(_error_payload(exc), status_code=_status_for(exc))


async def _guard(request: Request, work: Callable[[], Any]) -> JSONResponse:
    """在线程池执行阻塞 RPC 聚合，统一把路由层异常映射为结构化 HTTP 错误。"""
    try:
        result = await run_in_threadpool(work)
    except (DaemonUnavailableError, DaemonRemoteError) as exc:
        return _json_error(exc)
    except Exception as exc:  # 防御：未知异常也不裸 500 文本
        return JSONResponse(_error_payload(exc), status_code=500)
    return JSONResponse(result)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


def _clamp_int(value: str | None, default: int, lo: int, hi: int) -> int:
    try:
        parsed = int(value) if value not in (None, "") else default
    except ValueError:
        return default
    return max(lo, min(hi, parsed))


# ---------------------------------------------------------------- 数据聚合

def _resolve_workspace(root: str) -> dict:
    """workspace 绑定横幅数据：双命名空间同时给出，任一侧失败即结构化错误。

    - 配对解析复用 ``resolve_workspace_pair_from_daemon(root)``：按 root 在
      registry 匹配 active 行，并校验 registry/task-DB instance 一致，
      0 个或多个候选一律 fail-closed（不猜 id、不取最小 id 回退）。
    - registry 侧 id 经 ``workspace.status`` 只读 RPC 补齐，供横幅同时展示
      task DB 与 daemon registry 两个命名空间。
    """
    pair = resolve_workspace_pair_from_daemon(root)
    banner = {
        "workspace_id": pair.get("workspace_id"),
        "workspace_instance_id": pair.get("workspace_instance_id"),
    }
    try:
        status = route_rpc(
            "workspace.status",
            {"workspace_instance_id": pair["workspace_instance_id"]},
            "READ_ONLY",
        )
        if isinstance(status, dict):
            banner.update(
                {
                    "registry_workspace_id": status.get("registry_workspace_id"),
                    "registry_instance_id": status.get("registry_instance_id"),
                    "task_db_workspace_id": status.get("task_db_workspace_id"),
                    "task_db_instance_id": status.get("task_db_instance_id"),
                }
            )
    except (DaemonUnavailableError, DaemonRemoteError) as exc:
        banner["registry_error"] = _error_payload(exc)["error"]
    return banner


def _health_payload(root: str) -> dict:
    """健康横幅：health / bootstrap_status / workspace 三部分，逐部分容错。

    整体 ``ok`` 仅当三部分全部成功；单部分失败保留其余数据并附 error 对象，
    前端据此渲染降级横幅（不吞错、不猜测）。
    """
    result: dict[str, Any] = {"ok": True, "readonly": True, "auth": "none (MVP)"}

    try:
        result["daemon"] = route_rpc("health", {}, "READ_ONLY")
    except (DaemonUnavailableError, DaemonRemoteError) as exc:
        result["daemon"] = _error_payload(exc)
        result["ok"] = False

    # bootstrap_status：图谱/自举新鲜度（db_stale、latest_scan_run、任务分组计数）。
    # 依赖 cwd 在 git 仓库根（workspace_root 检查 .git）。
    try:
        result["bootstrap"] = route_rpc("bootstrap_status", {}, "READ_ONLY")
    except (DaemonUnavailableError, DaemonRemoteError) as exc:
        result["bootstrap"] = _error_payload(exc)
        result["ok"] = False

    try:
        result["workspace"] = _resolve_workspace(root)
    except (DaemonUnavailableError, DaemonRemoteError) as exc:
        result["workspace"] = _error_payload(exc)
        result["ok"] = False

    return result


def _dashboard_payload(params: dict) -> dict:
    """7-section 总览：get_project_dashboard（workspace 主库只读，convergence RPC）。"""
    merged = dict(DASHBOARD_DEFAULTS)
    merged.update(params or {})
    dashboard = route_rpc("get_project_dashboard", merged, "READ_ONLY")
    # 原样透传 daemon 结果；7 个 section 键名与 CLI `cw dashboard` 一致。
    return dashboard if isinstance(dashboard, dict) else {"data": dashboard}


def _task_list_payload(
    root: str, status: str | None, limit: int, offset: int, parent_id: str | None
) -> dict:
    """任务板：task.list（行内嵌治理投影）。可见性规则见读模型文档 §4。"""
    pair = resolve_workspace_pair_from_daemon(root)
    params: dict[str, Any] = {
        "workspace_id": pair["workspace_id"],  # daemon fail-closed：禁止无 workspace 全表列出
        "limit": limit,
        "offset": offset,
    }
    if status:
        params["status"] = status
    if parent_id:
        params["parent_id"] = parent_id
    listing = route_task_read("task.list", params, _no_local_fallback)
    if not isinstance(listing, dict):
        listing = {"tasks": listing if isinstance(listing, list) else []}
    return {"workspace": pair, **listing}


def _task_detail_payload(root: str, task_id: str, with_projection: bool) -> dict:
    """单卡下钻：状态树（workspace 由 task binding 权威解析）+ 派工投影 + 证据新鲜度。"""
    pair = resolve_workspace_pair_from_daemon(root)
    detail: dict[str, Any] = {"workspace": pair, "task_id": task_id}

    detail["tree"] = route_task_read("task.status_tree", {"task_id": task_id}, _no_local_fallback)

    if with_projection and isinstance(detail["tree"], dict):
        lifecycle = str(detail["tree"].get("status", ""))
        if lifecycle not in _TERMINAL_TASK_STATUSES:
            # 双参数严格必填（NextActionInput::from_params）。
            detail["next_action"] = route_task_read(
                "task.next_action",
                {
                    "task_id": task_id,
                    "workspace_instance_id": pair["workspace_instance_id"],
                },
                _no_local_fallback,
            )
        else:
            detail["next_action"] = None
            detail["next_action_skipped"] = "terminal status（GOV-FIX-04 短路）"

    try:
        detail["freshness"] = route_task_read(
            "get_freshness_status", {"task_id": task_id}, _no_local_fallback
        )
    except DaemonRemoteError as exc:
        # 证据新鲜度缺失不是错误（无证据事件时展示「暂无证据」）。
        detail["freshness"] = {"error": {"code": exc.code, "message": exc.message}}
    return detail


# ---------------------------------------------------------------- 端点

async def api_health(request: Request) -> JSONResponse:
    root = request.app.state.project_root
    return await _guard(request, lambda: _health_payload(root))


async def api_dashboard(request: Request) -> JSONResponse:
    q = request.query_params
    params = {
        "quick": not _truthy(q.get("full")),
        "with_cycles": _truthy(q.get("with_cycles")),
        "with_evolution": _truthy(q.get("with_evolution")),
        "top_n": _clamp_int(q.get("top_n"), DASHBOARD_DEFAULTS["top_n"], 1, 50),
    }
    return await _guard(request, lambda: _dashboard_payload(params))


async def api_tasks(request: Request) -> JSONResponse:
    root = request.app.state.project_root
    q = request.query_params
    status = (q.get("status") or "").strip() or None
    parent_id = (q.get("parent_id") or "").strip() or None
    limit = _clamp_int(q.get("limit"), 100, _MIN_LIMIT, _MAX_LIMIT)
    offset = max(0, _clamp_int(q.get("offset"), 0, 0, 10**9))
    return await _guard(
        request,
        lambda: _task_list_payload(root, status, limit, offset, parent_id),
    )


async def api_task_detail(request: Request) -> JSONResponse:
    root = request.app.state.project_root
    task_id = request.path_params.get("task_id", "").strip()
    if not task_id:
        return JSONResponse(
            {"error": {"code": "invalid_params", "message": "缺少 task_id"}},
            status_code=400,
        )
    with_projection = not _truthy(request.query_params.get("no_projection"))
    return await _guard(
        request, lambda: _task_detail_payload(root, task_id, with_projection)
    )


# ---------------------------------------------------------------- 应用工厂

def create_app(project_root: str) -> Starlette:
    """创建网关应用。全部路由显式 methods=['GET']：观测面无任何写端点。"""
    if not WEB_STATIC_DIR.is_dir():
        raise RuntimeError(
            f"web_static 目录缺失: {WEB_STATIC_DIR}（Step 2 交付物未就位）"
        )
    app = Starlette(
        routes=[
            Route("/api/health", api_health, methods=["GET"]),
            Route("/api/dashboard", api_dashboard, methods=["GET"]),
            Route("/api/tasks", api_tasks, methods=["GET"]),
            Route("/api/tasks/{task_id}", api_task_detail, methods=["GET"]),
            Mount("/", app=StaticFiles(directory=str(WEB_STATIC_DIR), html=True), name="static"),
        ]
    )
    app.state.project_root = project_root
    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m callwarden.server.web_gateway",
        description="Call Warden Web 观测面（只读）：本机项目进展看板",
    )
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help=f"绑定地址（默认 {DEFAULT_HOST}；非 loopback 属对外暴露）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help=f"绑定端口（默认 {DEFAULT_PORT}）")
    parser.add_argument("--root", default=os.getcwd(),
                        help="项目根（workspace 权威解析依据；默认当前目录，进程将 chdir 到此）")
    parser.add_argument("--token", default="",
                        help="对外暴露时的 token 参数位（本卡未实现校验，仅占位）")
    args = parser.parse_args(argv)

    root = os.path.abspath(args.root)
    if not os.path.isdir(root):
        print(f"[web-gateway] --root 不是有效目录: {root}", file=sys.stderr)
        return 2
    os.chdir(root)  # daemon workspace 权威解析按 cwd 匹配；入口强制对齐

    loopback = {"127.0.0.1", "::1", "localhost"}
    if args.host not in loopback:
        if not args.token:
            print(
                "[web-gateway] 拒绝启动：非 loopback 绑定必须显式携带 --token"
                "（fail-closed；本卡 token 仅参数位，完整认证见后续卡）",
                file=sys.stderr,
            )
            return 2
        print(
            "[web-gateway] ⚠ 警告：正在对外暴露只读观测面，且 token 校验尚未实现"
            "（本卡仅保留参数位）。认证卡落地前不得用于局域网。",
            file=sys.stderr,
        )

    import uvicorn

    app = create_app(root)

    # D3/F3：把本 webui 进程注册进 host 进程注册表（role=webui，独立于
    # daemon 单实例守卫——webui 启停不触发、也不受 daemon 锁约束）。
    # register_process 内部写 pid + process_start_time 双键，供判活防 PID 复用。
    # 注册表只是轻量索引：注册失败不阻断网关本身（fail-open 到观测面），
    # 但必须留痕；正常退出（含 KeyboardInterrupt / uvicorn 异常）在 finally 注销。
    _webui_pid = os.getpid()
    _webui_bind = f"{args.host}:{args.port}"
    try:
        from callwarden.config import get_http_authority_id
        from callwarden.server.host_process_registry import register_process
        _webui_authority = get_http_authority_id()
        register_process(
            authority_id=_webui_authority, role="webui", pid=_webui_pid,
            endpoint_or_bind=_webui_bind)
    except Exception as _reg_exc:
        _webui_authority = ""
        print(
            f"[web-gateway] ⚠ webui 进程注册失败（不阻断观测面）: {_reg_exc}",
            file=sys.stderr,
        )

    try:
        print(
            f"[web-gateway] 只读观测面 http://{args.host}:{args.port}/  "
            f"root={root}  (观测面，非操控台；全部端点 GET-only)"
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0
    finally:
        # 正常退出注销自身 entry（role=webui）；崩溃残留由权威 daemon 的
        # reconcile_stale 按 pid + process_start_time 判活后标 stale（不删历史）。
        if _webui_authority:
            try:
                from callwarden.server.host_process_registry import unregister_process
                unregister_process(_webui_authority, "webui", _webui_pid)
            except Exception as _unreg_exc:
                print(
                    f"[web-gateway] ⚠ webui 注销失败（将由 reconcile 收尾）: {_unreg_exc}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    sys.exit(main())
