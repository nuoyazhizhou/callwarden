"""CLI daemon 子命令——daemon client 和 enterprise/auto/local 模式切换。

提供 cw daemon register / list / status / mode 命令。
"""

import argparse
import json
import sys
from typing import Optional

from callwarden.config import (
    get_daemon_mode,
    is_daemon_available,
    is_daemon_required,
    resolve_container_path,
)
from callwarden.server.daemon_server import (
    api_register_workspace,
    api_list_workspaces,
    api_get_workspace_status,
    api_update_workspace_status,
)


def add_daemon_subcommands(subparsers):
    """添加 daemon 子命令到 argparse。"""
    daemon_parser = subparsers.add_parser("daemon", help="Enterprise daemon 管理")
    daemon_sub = daemon_parser.add_subparsers(dest="daemon_command")

    # daemon register
    register_parser = daemon_sub.add_parser("register", help="注册 workspace")
    register_parser.add_argument("--client-root", required=True, help="客户端视图根目录")
    register_parser.add_argument("--host-root", required=True, help="宿主机真实根目录")
    register_parser.add_argument("--git-remote", default="", help="Git remote URL")
    register_parser.add_argument("--git-head", default="", help="Git HEAD commit SHA")
    register_parser.add_argument("--uid", type=int, default=0, help="owner UID")

    # daemon list
    list_parser = daemon_sub.add_parser("list", help="列出 workspace")
    list_parser.add_argument("--uid", type=int, default=None, help="按 UID 过滤")

    # daemon status
    status_parser = daemon_sub.add_parser("status", help="查看 workspace 状态")
    status_parser.add_argument("workspace_id", help="workspace_instance_id")

    # daemon mode
    mode_parser = daemon_sub.add_parser("mode", help="查看/设置 daemon 模式")
    mode_parser.add_argument("--set", choices=["auto", "enterprise", "local"],
                             help="设置 daemon 模式")


def handle_daemon_command(args) -> int:
    """处理 daemon 子命令。"""
    if not args.daemon_command:
        print("Usage: cw daemon <register|list|status|mode>")
        return 1

    if args.daemon_command == "register":
        result = api_register_workspace(
            owner_uid=args.uid,
            client_view_root=args.client_root,
            host_real_root=args.host_root,
            git_remote_url=args.git_remote,
            git_head_commit_sha=args.git_head,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0

    elif args.daemon_command == "list":
        workspaces = api_list_workspaces(args.uid)
        print(json.dumps(workspaces, indent=2, default=str))
        return 0

    elif args.daemon_command == "status":
        ws = api_get_workspace_status(args.workspace_id)
        if ws:
            print(json.dumps(ws, indent=2, default=str))
        else:
            print(f"workspace not found: {args.workspace_id}")
            return 1
        return 0

    elif args.daemon_command == "mode":
        if args.set:
            print(f"设置 daemon 模式为: {args.set}")
            print(f"（需要设置环境变量 CW_DAEMON_MODE={args.set}）")
        else:
            mode = get_daemon_mode()
            available = is_daemon_available()
            required = is_daemon_required()
            print(f"当前模式: {mode}")
            print(f"daemon 可用: {available}")
            print(f"强制 daemon: {required}")
        return 0

    return 1
