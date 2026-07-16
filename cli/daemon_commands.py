"""Enterprise daemon 的独立 CLI，所有管理与查询请求均走 UDS。"""

from __future__ import annotations

import argparse
import json
import os
from typing import Optional, Sequence

from callwarden.config import (
    DAEMON_REGISTRY_DB,
    DAEMON_SOCKET_PATH,
    get_daemon_mode,
    is_daemon_available,
    is_daemon_required,
)
from callwarden.server.daemon_client import UnixDaemonRpcClient
from callwarden.server.daemon_server import (
    EnterpriseDaemonServer,
    EnterpriseDaemonService,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cw daemon", description="Enterprise daemon UDS 管理与查询"
    )
    parser.add_argument("--socket", default=DAEMON_SOCKET_PATH,
                        help="UDS 路径（默认 CW_DAEMON_SOCKET）")
    sub = parser.add_subparsers(dest="action", required=True)

    serve = sub.add_parser("serve", help="前台启动 daemon")
    serve.add_argument("--registry", default=DAEMON_REGISTRY_DB)
    serve.add_argument("--workers", type=int, default=16)

    sub.add_parser("ping", help="检查 daemon 与 peer credential")

    register = sub.add_parser("register", help="注册当前 UID 的 workspace")
    register.add_argument("root")
    register.add_argument("--git-remote", default="")
    register.add_argument("--git-head", default="")
    register.add_argument("--toolchain", default="")

    sub.add_parser("list", help="列出当前 UID 的 workspace")

    status = sub.add_parser("status", help="查询 workspace 和 snapshot 状态")
    status.add_argument("workspace_id")

    publish = sub.add_parser("publish", help="发布已刷新 DB 为共享 snapshot")
    publish.add_argument("workspace_id")
    publish.add_argument("db_path")
    publish.add_argument("--build-context", default="")

    query = sub.add_parser("query", help="查询共享 snapshot")
    query.add_argument("workspace_id")
    query.add_argument("query_type", choices=["stats", "symbol", "search", "callers", "callees"])
    query.add_argument("value", nargs="?", default="")
    query.add_argument("--qualified-name", default=None)
    query.add_argument("--kind", default=None)
    query.add_argument("--limit", type=int, default=20)

    mode = sub.add_parser("mode", help="查看 daemon 模式")
    mode.add_argument("--set", choices=["auto", "enterprise", "local"])

    # 运维命令（runbook 使用，不需要 workspace_id）
    sub.add_parser("health", help="检查 daemon 健康状态")
    sub.add_parser("schema-version", help="查询 registry DB schema 版本")

    backup = sub.add_parser("backup", help="备份 registry DB")
    backup.add_argument("--output", required=True, help="备份输出路径")

    restore = sub.add_parser("restore", help="从备份恢复 registry DB")
    restore.add_argument("--from", dest="from_path", required=True, help="备份文件路径")

    gc_cas = sub.add_parser("gc-cas", help="GC CAS 存储（清理未引用 content）")
    gc_cas.add_argument("--grace-days", type=int, default=7, help="清理 grace_days 天前的未引用 content")
    gc_cas.add_argument("workspace_id", help="workspace instance ID")

    gc_snapshots = sub.add_parser("gc-snapshots", help="GC 快照（保留最近 N 个）")
    gc_snapshots.add_argument("--keep-last", type=int, default=3, help="每个 workspace 保留的快照数量")
    return parser


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def run_daemon_command(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    if args.action == "mode":
        if args.set:
            print(f"请设置环境变量 CW_DAEMON_MODE={args.set}")
        _print_json({
            "mode": args.set or get_daemon_mode(),
            "available": is_daemon_available(),
            "required": is_daemon_required(),
            "socket": args.socket,
        })
        return 0

    if args.action == "serve":
        service = EnterpriseDaemonService(args.registry)
        server = EnterpriseDaemonServer(
            args.socket, service, max_workers=max(1, args.workers)
        )
        print(f"Call Warden Enterprise daemon listening: {args.socket}")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
        return 0

    client = UnixDaemonRpcClient(args.socket)
    if args.action == "ping":
        result = client.call("ping")
    elif args.action == "register":
        result = client.call("workspace.register", {
            "client_view_root": os.path.abspath(args.root),
            "git_remote_url": args.git_remote,
            "git_head_commit_sha": args.git_head,
            "toolchain_fingerprint": args.toolchain,
        })
    elif args.action == "list":
        result = client.call("workspace.list")
    elif args.action == "status":
        result = client.call("workspace.status", {
            "workspace_instance_id": args.workspace_id,
        })
    elif args.action == "publish":
        result = client.publish_snapshot(
            args.workspace_id,
            os.path.abspath(args.db_path),
            args.build_context,
        )
    elif args.action == "query":
        params = {"workspace_instance_id": args.workspace_id}
        method = f"query.{args.query_type}"
        if args.query_type == "symbol":
            params["qualified_name"] = args.value
        elif args.query_type == "search":
            params.update(query=args.value, kind=args.kind, limit=args.limit)
        elif args.query_type == "callers":
            params.update(callee_name=args.value, qualified_name=args.qualified_name)
        elif args.query_type == "callees":
            params.update(caller_name=args.value, qualified_name=args.qualified_name)
        result = client.call(method, params)
    elif args.action == "health":
        result = client.call("health", {})
    elif args.action == "schema-version":
        result = client.call("schema.version", {})
    elif args.action == "backup":
        result = client.call("backup", {"output_path": os.path.abspath(args.output)})
    elif args.action == "restore":
        result = client.call("restore", {"source_path": os.path.abspath(args.from_path)})
    elif args.action == "gc-cas":
        result = client.call("gc.cas", {
            "workspace_instance_id": args.workspace_id,
            "grace_days": args.grace_days,
        })
    elif args.action == "gc-snapshots":
        result = client.call("gc.snapshots", {"keep_last": args.keep_last})
    else:
        raise AssertionError(args.action)
    _print_json(result)
    return 0


def add_daemon_subcommands(_subparsers):
    """兼容旧导入；daemon 现在由主 CLI 提前分派。"""


def handle_daemon_command(args) -> int:
    """兼容旧调用；新代码应使用 run_daemon_command。"""
    return run_daemon_command(getattr(args, "daemon_argv", None))
