"""Enterprise daemon 的独立 CLI，所有管理与查询请求均走 UDS。"""

from __future__ import annotations

import argparse
import json
import os
import time
from typing import Any, Dict, Optional, Sequence

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


def _parser(include_serve: bool = True) -> argparse.ArgumentParser:
    """构造 daemon/client 共用的 argparse parser。

    Args:
        include_serve: 是否注册 `serve` 子命令。`cw daemon` 含 serve，
            `cw-client` 不含（纯 client 视角，禁止启动 daemon 本身）。
    """
    parser = argparse.ArgumentParser(
        prog="cw daemon", description="Enterprise daemon UDS 管理与查询"
    )
    parser.add_argument("--socket", default=DAEMON_SOCKET_PATH,
                        help="UDS 路径（默认 CW_DAEMON_SOCKET）")
    sub = parser.add_subparsers(dest="action", required=True)

    if include_serve:
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
    query.add_argument("query_type", choices=[
        "stats", "symbol", "search", "callers", "callees",
        # J8 协议闭合：补齐 Rust daemon 已实现的 3 个高级查询 method
        "call_chain_down", "topological_order", "detect_cycles",
    ])
    query.add_argument("value", nargs="?", default="")
    query.add_argument("--qualified-name", default=None)
    query.add_argument("--kind", default=None)
    query.add_argument("--limit", type=int, default=20)
    query.add_argument("--max-depth", type=int, default=10,
                       help="call_chain_down / detect_cycles 的最大深度")

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

    # ---- Snapshot 缓存运维命令（J8 协议闭合：Rust daemon 已实现 3 个 method）----
    sub.add_parser("snapshot-stats",
                   help="查询 daemon 内 SnapshotCache 统计（hit/miss/evictions）")
    sub.add_parser("snapshot-list",
                   help="列出 daemon 已知的所有 workspace snapshot")
    snap_evict = sub.add_parser("snapshot-evict",
                                help="驱逐指定 workspace 的 snapshot 缓存")
    snap_evict.add_argument("workspace_id",
                            help="workspace instance ID（如驱逐失败可检查 daemon 日志）")

    # ---- Metrics 查询命令（Phase 8 metrics endpoint 闭合）----
    # G13（2026-07-20）：默认通过 RPC 拉取 daemon 进程的指标；
    # --local 降级为本进程 MetricsCollector 单例（用于离线调试）。
    metrics_cmd = sub.add_parser("metrics",
                                help="查询 daemon 运行时指标（counters/gauges/histograms）")
    metrics_cmd.add_argument("--format", choices=["prometheus", "json"],
                             default="json",
                             help="输出格式（默认 json；prometheus 输出 Prometheus 文本格式）")
    metrics_cmd.add_argument("--name",
                             help="仅显示指定指标名（缺省显示全部）")
    metrics_cmd.add_argument("--reset", action="store_true",
                             help="重置所有计数器/仪表/直方图（谨慎使用，仅测试或重启后场景；"
                                  "仅 --local 模式有效")
    metrics_cmd.add_argument("--local", action="store_true",
                             help="本进程直读（不走 daemon RPC），用于离线调试；"
                                  "默认走 RPC 拉 daemon 进程指标")

    # ---- Mount Mapping 管理命令（G4）----
    mount = sub.add_parser("mount", help="容器挂载映射管理")
    mount_sub = mount.add_subparsers(dest="mount_action", required=True)

    mount_register = mount_sub.add_parser("register", help="注册/更新容器挂载映射")
    mount_register.add_argument("container_id", help="容器标识（如 ubuntu_2204）")
    mount_register.add_argument("container_path", help="容器内路径前缀")
    mount_register.add_argument("host_path", help="宿主机真实路径")
    mount_register.add_argument("--type", dest="mapping_type",
                                choices=["bind", "volume", "smb"], default="bind",
                                help="映射类型（默认 bind）")

    mount_list = mount_sub.add_parser("list", help="列出容器挂载映射")
    mount_list.add_argument("--container-id", default=None,
                            help="按 container_id 过滤（缺省列出全部）")

    mount_delete = mount_sub.add_parser("delete", help="删除容器挂载映射")
    mount_delete.add_argument("container_id", help="容器标识")
    mount_delete.add_argument("container_path", help="容器内路径前缀")

    # ---- Toolchain / Build Context / Resolved Edges 管理命令（G1 Layer 2）----
    toolchain = sub.add_parser("toolchain", help="工具链与 build context 管理")
    toolchain_sub = toolchain.add_subparsers(dest="toolchain_action", required=True)

    # toolchain register
    tc_reg = toolchain_sub.add_parser("register", help="注册工具链")
    tc_reg.add_argument("name", help="工具链名称（唯一）")
    tc_reg.add_argument("compiler_path", help="编译器可执行文件路径")
    tc_reg.add_argument("--compiler-type", default="", help="编译器类型（gcc/clang/...）")
    tc_reg.add_argument("--version", default="", help="编译器版本")
    tc_reg.add_argument("--target-triple", default="", help="目标三元组")
    tc_reg.add_argument("--sysroot", default="", help="sysroot 路径")
    tc_reg.add_argument("--include-dirs", default="", help="额外 include 目录（分号分隔）")
    tc_reg.add_argument("--fingerprint", default="",
                        help="显式指定 fingerprint（不指定则按字段计算）")
    tc_reg.add_argument("--description", default="", help="工具链描述")

    # toolchain list / get / delete
    toolchain_sub.add_parser("list", help="列出所有工具链")
    tc_get = toolchain_sub.add_parser("get", help="查询工具链（按 name 或 id）")
    tc_get.add_argument("name_or_id", help="工具链名称或 ID")
    tc_del = toolchain_sub.add_parser("delete", help="删除工具链")
    tc_del.add_argument("name_or_id", help="工具链名称或 ID")

    # toolchain bind
    tc_bind = toolchain_sub.add_parser("bind", help="绑定工具链到 workspace")
    tc_bind.add_argument("workspace_id", type=int, help="workspace ID")
    tc_bind.add_argument("toolchain_id", type=int, help="toolchain ID")
    tc_bind.add_argument("--build-context-hash", default="",
                         help="build context hash（同一 workspace 不同 variant）")

    # toolchain resolve
    tc_resolve = toolchain_sub.add_parser("resolve",
                                          help="解析 workspace+build_context 对应的 toolchain")
    tc_resolve.add_argument("workspace_id", type=int, help="workspace ID")
    tc_resolve.add_argument("--build-context-hash", default=None,
                            help="build context hash（缺省用 active）")

    # build-context 子命令组
    bc = toolchain_sub.add_parser("build-context", help="build context 管理")
    bc_sub = bc.add_subparsers(dest="bc_action", required=True)

    bc_reg = bc_sub.add_parser("register", help="注册 build context")
    bc_reg.add_argument("workspace_id", type=int, help="workspace ID")
    bc_reg.add_argument("name", help="build context 名称（如 debug/release）")
    bc_reg.add_argument("--compile-flags", default="",
                        help="编译选项（分号分隔，如 -O2;-g）")
    bc_reg.add_argument("--defines", default="",
                        help="预定义宏（key=value;key=value 格式）")
    bc_reg.add_argument("--include-paths", default="",
                        help="额外 include 路径（分号分隔）")
    bc_reg.add_argument("--set-active", action="store_true",
                        help="设为当前 active context")

    bc_sub.add_parser("list", help="列出 build context").add_argument(
        "workspace_id", type=int, help="workspace ID"
    )
    bc_set = bc_sub.add_parser("set-active", help="设置 active build context")
    bc_set.add_argument("workspace_id", type=int, help="workspace ID")
    bc_set.add_argument("build_context_hash", help="build context hash")
    bc_del = bc_sub.add_parser("delete", help="删除 build context")
    bc_del.add_argument("workspace_id", type=int, help="workspace ID")
    bc_del.add_argument("build_context_hash", help="build context hash")

    # resolved-edges 子命令组
    re_cmd = toolchain_sub.add_parser("resolved-edges", help="resolved edges 管理")
    re_sub = re_cmd.add_subparsers(dest="re_action", required=True)

    re_store = re_sub.add_parser("store", help="批量存储 resolved edges")
    re_store.add_argument("workspace_id", type=int, help="workspace ID")
    re_store.add_argument("build_context_hash", help="build context hash")
    re_store.add_argument("--edges-json", default="",
                          help="edges JSON 数组（每个元素含 caller_symbol_id, "
                               "callee_symbol_id, callee_name, callee_file, "
                               "call_line, resolution_method）")

    re_get = re_sub.add_parser("get", help="查询 resolved edges")
    re_get.add_argument("workspace_id", type=int, help="workspace ID")
    re_get.add_argument("build_context_hash", help="build context hash")
    re_get.add_argument("--caller-symbol-id", type=int, default=None,
                        help="按 caller 过滤")
    re_get.add_argument("--limit", type=int, default=None, help="限制返回条数")

    re_count = re_sub.add_parser("count", help="统计 resolved edges 数量")
    re_count.add_argument("workspace_id", type=int, help="workspace ID")
    re_count.add_argument("build_context_hash", help="build context hash")

    return parser


def _print_json(value) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, default=str))


def _parse_semi_list(s: str) -> list:
    """将分号分隔字符串解析为列表（空字符串 → 空列表）。"""
    if not s:
        return []
    return [p for p in s.split(";") if p]


def _parse_defines(s: str) -> dict:
    """将 'key=value;key=value' 格式解析为 dict。"""
    if not s:
        return {}
    result = {}
    for part in s.split(";"):
        if not part:
            continue
        if "=" in part:
            k, v = part.split("=", 1)
            result[k] = v
        else:
            result[part] = ""
    return result


def _dispatch_toolchain_cli(client, args) -> dict:
    """toolchain / build-context / resolved-edges 子命令分发到对应 RPC。"""
    action = args.toolchain_action

    if action == "register":
        # 若调用方未提供 fingerprint，让 daemon 端按字段计算
        params = {
            "name": args.name,
            "compiler_path": os.path.abspath(args.compiler_path),
            "compiler_type": args.compiler_type,
            "version": args.version,
            "target_triple": args.target_triple,
            "sysroot": args.sysroot,
            "include_dirs": _parse_semi_list(args.include_dirs),
            "predefined_macros": {},  # CLI 不支持复杂宏，留给 daemon API
            "description": args.description,
        }
        if args.fingerprint:
            params["fingerprint"] = args.fingerprint
        return client.call("toolchain.register", params)

    if action == "list":
        return client.call("toolchain.list", {})

    if action == "get":
        return client.call("toolchain.get", {"name_or_id": args.name_or_id})

    if action == "delete":
        return client.call("toolchain.delete", {"name_or_id": args.name_or_id})

    if action == "bind":
        return client.call("toolchain.bind", {
            "workspace_id": args.workspace_id,
            "toolchain_id": args.toolchain_id,
            "build_context_hash": args.build_context_hash,
        })

    if action == "resolve":
        params = {"workspace_id": args.workspace_id}
        if args.build_context_hash is not None:
            params["build_context_hash"] = args.build_context_hash
        return client.call("toolchain.resolve", params)

    # build-context 子命令
    if action == "build-context":
        bc_action = args.bc_action
        if bc_action == "register":
            return client.call("build_context.register", {
                "workspace_id": args.workspace_id,
                "name": args.name,
                "compile_flags": _parse_semi_list(args.compile_flags),
                "defines": _parse_defines(args.defines),
                "include_paths": _parse_semi_list(args.include_paths),
                "set_active": args.set_active,
            })
        if bc_action == "list":
            return client.call("build_context.list", {
                "workspace_id": args.workspace_id,
            })
        if bc_action == "set-active":
            return client.call("build_context.set_active", {
                "workspace_id": args.workspace_id,
                "build_context_hash": args.build_context_hash,
            })
        if bc_action == "delete":
            return client.call("build_context.delete", {
                "workspace_id": args.workspace_id,
                "build_context_hash": args.build_context_hash,
            })
        raise AssertionError(bc_action)

    # resolved-edges 子命令
    if action == "resolved-edges":
        re_action = args.re_action
        if re_action == "store":
            edges = json.loads(args.edges_json) if args.edges_json else []
            return client.call("resolved_edges.store", {
                "workspace_id": args.workspace_id,
                "build_context_hash": args.build_context_hash,
                "edges": edges,
            })
        if re_action == "get":
            params = {
                "workspace_id": args.workspace_id,
                "build_context_hash": args.build_context_hash,
            }
            if args.caller_symbol_id is not None:
                params["caller_symbol_id"] = args.caller_symbol_id
            if args.limit is not None:
                params["limit"] = args.limit
            return client.call("resolved_edges.get", params)
        if re_action == "count":
            return client.call("resolved_edges.count", {
                "workspace_id": args.workspace_id,
                "build_context_hash": args.build_context_hash,
            })
        raise AssertionError(re_action)

    raise AssertionError(action)


def run_daemon_command(argv: Optional[Sequence[str]] = None,
                       include_serve: bool = True) -> int:
    """daemon/client 共用的命令分发入口。

    Args:
        argv: 命令行参数（不含程序名）
        include_serve: 是否允许 `serve` 子命令。`cw daemon` 允许，
            `cw-client` 禁止（纯 client 视角，不能启动 daemon 本身）。
    """
    args = _parser(include_serve).parse_args(argv)
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

    # G13（2026-07-20）：metrics 默认走 RPC 拉 daemon 进程指标；
    # --local 走本进程直读（离线调试）；--reset 仅 --local 模式支持。
    if args.action == "metrics":
        from callwarden.server.metrics import get_metrics_collector

        if args.reset:
            if not args.local:
                print("ERROR: --reset 仅支持 --local 模式（不能重置远端 daemon 指标）",
                      file=__import__("sys").stderr)
                return 2
            collector = get_metrics_collector()
            collector.reset()
            _print_json({"status": "reset", "timestamp": time.time()})
            return 0

        # 默认走 RPC；连不上 daemon 时降级 --local（除非用户显式指定 --local）
        if not args.local:
            client = UnixDaemonRpcClient(args.socket)
            rpc_method = ("metrics.prometheus" if args.format == "prometheus"
                          else "metrics.snapshot")
            try:
                rpc_result = client.call(rpc_method)
                if args.format == "prometheus":
                    # Prometheus 文本直接打印
                    print(rpc_result)
                elif args.name:
                    # 按 name 过滤 RPC 返回的 JSON
                    filtered: Dict[str, Any] = {
                        "timestamp": rpc_result.get("timestamp"),
                        "uptime": rpc_result.get("uptime"),
                        "name_filter": args.name,
                    }
                    found = False
                    for category in ("counters", "gauges", "histograms"):
                        cat_data = rpc_result.get(category, {})
                        if args.name in cat_data:
                            filtered[category] = {args.name: cat_data[args.name]}
                            found = True
                        else:
                            filtered[category] = {}
                    filtered["found"] = found
                    _print_json(filtered)
                else:
                    _print_json(rpc_result)
                return 0
            except Exception as e:
                # daemon 未启动 / RPC 失败 → 降级本地直读
                # DaemonUnavailableError 是 RuntimeError 子类，不捕获会冒泡
                print(f"WARNING: daemon RPC 失败 ({e})，降级本进程直读",
                      file=__import__("sys").stderr)
                args.local = True  # 触发下方本地分支

        if args.local:
            collector = get_metrics_collector()
            if args.format == "prometheus":
                text = collector.to_prometheus()
                print(text)
            else:
                data = collector.to_json()
                if args.name:
                    filtered = {"timestamp": data["timestamp"],
                                "uptime": data["uptime"],
                                "name_filter": args.name}
                    found = False
                    for category in ("counters", "gauges", "histograms"):
                        if args.name in data[category]:
                            filtered[category] = {args.name: data[category][args.name]}
                            found = True
                        else:
                            filtered[category] = {}
                    filtered["found"] = found
                    _print_json(filtered)
                else:
                    _print_json(data)
            return 0
        return 0

    if args.action == "serve":
        if not include_serve:
            # argparse 已在 _parser(include_serve=False) 时拒绝 serve，
            # 此分支理论上不可达，保险起见显式报错
            print("ERROR: 'serve' is not available in client mode.",
                  file=__import__("sys").stderr)
            return 2
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
        elif args.query_type == "call_chain_down":
            # J8 协议闭合：value 是 qualified_name，--max-depth 控制深度
            params.update(qualified_name=args.value, max_depth=args.max_depth)
        elif args.query_type == "topological_order":
            params.update(limit=args.limit)
        elif args.query_type == "detect_cycles":
            params.update(max_depth=args.max_depth)
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
    elif args.action == "snapshot-stats":
        # J8 协议闭合：Rust daemon snapshot.stats method
        result = client.call("snapshot.stats", {})
    elif args.action == "snapshot-list":
        # J8 协议闭合：Rust daemon snapshot.list_workspaces method
        result = client.call("snapshot.list_workspaces", {})
    elif args.action == "snapshot-evict":
        # J8 协议闭合：Rust daemon snapshot.evict method
        result = client.call("snapshot.evict", {
            "workspace_instance_id": args.workspace_id,
        })
    elif args.action == "mount":
        if args.mount_action == "register":
            result = client.call("mount.register", {
                "container_id": args.container_id,
                "container_path": args.container_path,
                "host_path": os.path.abspath(args.host_path),
                "mapping_type": args.mapping_type,
            })
        elif args.mount_action == "list":
            params = {}
            if args.container_id:
                params["container_id"] = args.container_id
            result = client.call("mount.list", params)
        elif args.mount_action == "delete":
            result = client.call("mount.delete", {
                "container_id": args.container_id,
                "container_path": args.container_path,
            })
        else:
            raise AssertionError(args.mount_action)
    elif args.action == "toolchain":
        result = _dispatch_toolchain_cli(client, args)
    else:
        raise AssertionError(args.action)
    _print_json(result)
    return 0


def add_daemon_subcommands(_subparsers):
    """兼容旧导入；daemon 现在由主 CLI 提前分派。"""


def handle_daemon_command(args) -> int:
    """兼容旧调用；新代码应使用 run_daemon_command。"""
    return run_daemon_command(getattr(args, "daemon_argv", None))
