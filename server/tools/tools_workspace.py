"""工作区与文件面：构建刷新/工作区/Git/文件/状态/符号内容/度量/健康检查

拆分自 server/mcp_server.py（558-1259 行区间），由 register(mcp) 注册。

H4B-N（T-1786590214634-9e740cdc-h4b-native-read）：HTTP daemon 原生读/查路由归类说明
- rust_native（H4A 已建路由）：list_workspaces（workspace.list）、
  get_active_workspace（workspace.activate），与 HEAD 基线一致，保留。
- python_compat / legacy_local：其余工具由矩阵标注为 python_compat
  或 legacy_local（file_grep/file_list 为 legacy_local）。
- 更新（T-1788923523728-7a316cc4，2026-09-09）：daemon dispatch.rs 已新增
  workspace.build_graph / workspace.file.refresh_file / workspace.build_directory
  原生分支（含 tree-sitter 符号落库），build_graph/refresh_file/build_directory
  三个工具改为直达 daemon RPC；输出模型从 bool 放宽为 dict 以匹配
  daemon 结构化返回（旧 bool 签名会使 FastMCP 输出校验炸掉）。
"""

# [L1] 构建刷新与工作区管理工具（build_graph / list_workspaces / set_active_workspace 等）
# [L2] 文件操作与符号内容工具（file_read / file_grep / file_symbol_content 等）
# [L4] 代码度量与健康检查工具（get_code_metrics_summary / get_code_health_check 等）
# [L8] Git 集成工具（import_git_history / get_git_commits / get_commit_changes 等）

import contextlib
import os
import re
import tempfile
from typing import Any, Dict, List, Optional, TypedDict

from .._mcp_common import _call_daemon_rpc, get_db  # noqa: F401 —— re-export facade：测试与 legacy HTTP 路径经本模块访问（lint 第三批曾误删）

from mcp.server.fastmcp import FastMCP


from ..daemon_client import HttpDaemonRpcClient, route_rpc as _route
from ..daemon_protocol import DaemonRemoteError

# ----------------------------------------------------------------------
# ADR-002 S2（§16.4）：远端只读面——客户端本机执行层
#
# bind_scope=remote 的 daemon 部署在远端宿主上，文件面工具若仍把每个
# file.read/grep/list 请求发往 daemon，每次都消耗完整 RPC 往返 + daemon
# 侧磁盘 I/O。S2 把五个文件/构建工具改为客户端本机执行（结果与 daemon
# handler 同构），只有 build_graph/build_directory 的产物经
# snapshot.upload 发布回 daemon（权威面）。
#
# 同构口径：与 rust_ext/src/daemon/fs_handlers.rs 的 handler 保持字段级
# 一致（错误码、返回形态、剪枝规则、glob 语义），只是执行位置从 daemon
# 宿主换到客户端本机。路径解析键 = 客户端 workspace 根（HttpDaemonRpcClient
# `_project_root`，同 F-5 修复的注入层口径），相对路径一律以它为基准。
# ----------------------------------------------------------------------

_EXEC_ORIGIN_CLIENT = "client"

# 与 daemon fs_handlers.rs SKIP_DIRS 同源（build/ 为歧义目录，交由 ignore 契约）
_REMOTE_SKIP_DIRS = frozenset({
    ".git", "node_modules", "target", "dist", ".next", "__pycache__",
})

# 与 daemon fs_handlers.rs INDEXABLE_EXTS 同源
_REMOTE_INDEXABLE_EXTS = frozenset({
    "py", "rs", "c", "h", "cpp", "hpp", "cc", "go", "java", "js", "jsx",
    "ts", "tsx", "rb", "php", "scala", "cs", "kt", "swift", "ex", "exs",
    "hcl", "vue", "svelte",
})

# 豁免 §16.1 绝对路径扫描的参数键（载荷字段，不是路径）
_REMOTE_PATH_EXEMPT_KEYS = frozenset({
    "payload_b64", "chunk_b64", "canonical_bytes_gz_b64", "canonical_bytes_b64",
})


def _remote_client() -> HttpDaemonRpcClient:
    """client 层复用 route_rpc 同款 thin client 单例。"""
    return HttpDaemonRpcClient.get_instance()


def _is_remote_profile() -> bool:
    """当前 daemon manifest 是否 bind_scope=remote（读取失败按 loopback 兼容）。"""
    try:
        return _remote_client().is_remote_profile()
    except Exception:
        return False


def _looks_like_path(value: str) -> bool:
    """与 daemon snapshot_guard.rs looks_like_path 同构（§16.1 路径判定）。"""
    return (
        value.startswith("/")
        or value.startswith("\\")
        or re.match(r"^[A-Za-z]:", value) is not None
    )


def _reject_remote_params(params: Dict[str, Any], tool_name: str) -> None:
    """§16.1/S2-05：remote profile 下绝对路径参数 fail-closed，不发起 RPC。"""
    for key, value in params.items():
        if key in _REMOTE_PATH_EXEMPT_KEYS:
            continue
        if isinstance(value, str) and _looks_like_path(value):
            raise DaemonRemoteError(
                "E_REMOTE_PATH_FORBIDDEN",
                f"远端 profile 禁止绝对路径参数（{tool_name}/{key}）：{value}"
                "（客户端本机路径一律以 workspace 相对位置表达）",
            )


def _remote_workspace_root() -> str:
    """客户端 workspace 根（客户端执行面的路径基准，§16.4）。"""
    client = _remote_client()
    root = client._project_root or os.getcwd()
    return os.path.realpath(root)


def _rust_lines(text: str) -> List[str]:
    """镜像 Rust `str::lines()`：末尾换行可选、`\r\n` 按一个换行、剥离行尾 `\r`。"""
    body = text[:-1] if text.endswith("\n") else text
    return [line.rstrip("\r") for line in body.split("\n")]


def _remote_load_ignore_dir_names(root: str) -> set:
    """与 daemon load_ignore_dir_names 同构：收集 ignore 契约的目录名。

    读 `.callwardenignore` / `.gitignore`，逐行 trim，跳过空行/`#`/`!`/
    含 `/` 的行，剥首尾 `/`，跳过含 glob 字符或 `.` 的条目。
    """
    names = set()
    for fname in (".callwardenignore", ".gitignore"):
        path = os.path.join(root, fname)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                raw_lines = fh.readlines()
        except OSError:
            continue
        for raw in raw_lines:
            entry = raw.strip()
            if (
                not entry
                or entry.startswith("#")
                or entry.startswith("!")
                or "/" in entry
            ):
                continue
            entry = entry.rstrip("/").lstrip("/")
            if not entry or any(ch in entry for ch in "*?[") or "." in entry:
                continue
            names.add(entry)
    return names


def _remote_is_indexable_rel(rel: str) -> bool:
    """与 daemon is_indexable_path 同构：隐藏段/跳过目录/扩展名白名单。"""
    norm = rel.replace("\\", "/")
    if norm.startswith(".") or "/." in norm:
        return False
    for seg in norm.split("/"):
        if seg in _REMOTE_SKIP_DIRS:
            return False
    lower = norm.lower()
    ext = lower.rsplit(".", 1)[-1] if "." in lower else ""
    return ext in _REMOTE_INDEXABLE_EXTS


def _remote_scan_indexable(
    root: str, base_rel: str, recursive: bool, ignore_dirs: set
) -> List[str]:
    """与 daemon scan_files 同构的扫描（readdir 序，返回排序后的 posix rel 列表）。

    recursive=False 时只扫 base 这一层的文件（目录不深入），对应 daemon
    scan_files(root, false) 与 file.list 的非递归口径。
    """
    skip = _REMOTE_SKIP_DIRS | set(ignore_dirs)
    base = root if not base_rel else os.path.join(root, base_rel)
    out: List[str] = []
    stack = [base]
    while stack:
        cur = stack.pop()
        try:
            entries = os.listdir(cur)
        except OSError:
            continue
        for name in entries:
            full = os.path.join(cur, name)
            if os.path.isdir(full):
                if recursive and not name.startswith(".") and name not in skip:
                    stack.append(full)
                continue
            rel = os.path.relpath(full, root).replace("\\", "/")
            if _remote_is_indexable_rel(rel):
                out.append(rel)
    out.sort()
    return out


def _remote_glob_match(rel: str, glob: str) -> bool:
    """与 daemon glob_match 同构：空 glob 全匹配；`**`→`.*`、`*`→`[^/]*`。

    正则编译失败退化为字面 contains（同 daemon 容错口径）。
    """
    if not glob:
        return True
    # 占位符两步替换（同 Rust fs_handlers.rs glob_match 的 \u{0} 收 **）：
    # 直接 replace 会把第一步生成的 ".*" 里的 "*" 二次替换——
    # "**/*.py" 会坏成 ".[^/]*/[^/]*.py"（深层文件全部漏匹配）
    placeholder = "\x00"
    pattern = (
        glob.replace("**", placeholder)
        .replace("*", "[^/]*")
        .replace(placeholder, ".*")
    )
    try:
        return re.fullmatch(pattern, rel) is not None
    except re.error:
        return glob in rel


def _remote_validate(root: str, rel: str, require_file: bool) -> str:
    """与 daemon resolve_owned_path 的客户端等价校验，返回规范绝对路径。

    无属主检查（客户端本机执行，调用方即属主）；保留存在性、文件/目录
    类型与 workspace 根内限制（fail-closed，错误码与 daemon 同构）。
    """
    if _looks_like_path(rel) and os.path.isabs(rel):
        # 绝对路径已由 _reject_remote_params 在工具入口挡掉；此分支仅为
        # 防御：把绝对路径锚定到根下后再做根内校验，越根即 path_escape。
        pass
    anchored = rel if os.path.isabs(rel) else os.path.join(root, rel)
    real = os.path.realpath(anchored)
    root_real = os.path.realpath(root)
    if (
        real != root_real
        and not real.startswith(root_real + os.sep)
    ):
        raise DaemonRemoteError(
            "path_escape", f"路径不在 workspace 根内：{real}"
        )
    if not os.path.exists(real):
        raise DaemonRemoteError("path_not_found", f"路径不存在：{rel}")
    if require_file and not os.path.isfile(real):
        raise DaemonRemoteError("path_not_found", f"不是文件：{rel}")
    return real


@contextlib.contextmanager
def _remote_codegraph(root: str):
    """S2 §16.3 客户端临时工作库（db/ 纯度守卫：函数内延迟导入）。

    每次远端构建新建临时库（全量扫描语义、无跨调用状态），构建完成后
    调 `cg.checkpoint_and_close()`（WAL checkpoint(TRUNCATE) + 关连接），
    保证读到的 db 字节是完整的（CodeGraphDB 默认 WAL 模式，直接拷主文件
    会丢 -wal 数据），随后删除 db/-wal/-shm。
    """
    from callwarden.db import CodeGraphDB

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="cw_remote_build_")
    os.close(fd)
    cg = None
    try:
        cg = CodeGraphDB(db_path=db_path, workspace_root=root)
        yield cg
    finally:
        try:
            if cg is not None:
                cg.checkpoint_and_close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                try:
                    os.unlink(db_path + suffix)
                except OSError:
                    pass


def _remote_build_stats(cg: Any) -> Dict[str, int]:
    """§16.4 build_graph 返回字段：daemon handler 统计口径的客户端映射。

    临时工作库为全新库，`_stage_timings` 即本轮扫描统计，`get_stats()`
    总量即本轮解析总量；Python 年轻代 GC 不外暴露删除/归档计数，对应
    字段以 0 占位保持形态同构。
    """
    timings = getattr(cg, "_stage_timings", {}) or {}
    stats = cg.get_stats() or {}
    files_total = int(timings.get("files_total", 0) or 0)
    files_parsed = int(timings.get("files_parsed", 0) or 0)
    files_unchanged = int(timings.get("files_unchanged", 0) or 0)
    files_skipped = int(timings.get("files_skipped", 0) or 0)
    symbols = int(stats.get("total_symbols", 0) or 0)
    calls = int(stats.get("total_calls", 0) or 0)
    resolved = int(stats.get("resolved_calls", 0) or 0)
    return {
        "scanned": files_total,
        "inserted": files_parsed,
        "unchanged": files_unchanged,
        "skipped_cached": files_unchanged,
        "deleted": 0,
        "archived": 0,
        "skipped_unparsed": files_skipped,
        "symbols": symbols,
        "calls": calls,
        "calls_resolved": resolved,
        "calls_ambiguous": max(0, calls - resolved),
    }


def _remote_upload_built_db(cg: Any) -> Dict[str, Any]:
    """读取临时工作库字节并 snapshot.upload 发布回 daemon（S2-03 链路）。"""
    db_path = getattr(cg, "db_path", "")
    if not db_path or not os.path.isfile(db_path):
        raise DaemonRemoteError("file_read_failed", f"客户端工作库不可读：{db_path}")
    with open(db_path, "rb") as fh:
        raw = fh.read()
    client = _remote_client()
    ws_id = client.ensure_remote_workspace()
    result = client.upload_snapshot(ws_id, raw)
    if not isinstance(result, dict):
        result = {"ok": True}
    result.setdefault("ok", True)
    return result


class CodeMetricsSummary(TypedDict):
    """`get_code_metrics_summary` 的显式 output 契约（冻结）。

    逐字段对齐 legacy 权威实现 `db/db_metrics.py::get_code_metrics_summary`
    与 daemon 端 `rust_ext/src/daemon/query_compat_handlers.rs::summary_metrics_summary`
    （即 `query.metrics_summary` RPC 的实现）。字段缺失或改名即视为契约破坏：
    FastMCP 会依据本注解生成 outputSchema，并在工具返回时校验、fail-closed。
    """

    file_count: int
    function_count: int
    total_lines: int
    total_calls: int
    avg_complexity: float
    max_complexity: int
    complexity_distribution: Dict[str, int]
    comment_coverage: float


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def build_graph(workspace_instance_id: str = "") -> dict:
        """完整构建代码知识图谱（增量重建：未变化文件免解析）

        Args:
            workspace_instance_id: 目标 workspace 的稳定实例 ID（可选）。
                留空时由 route_rpc 注入当前活动 workspace（多数场景正确）。
                显式传入可定向构建指定 workspace——多 workspace 环境下必须
                显式传入，否则会构建活动 workspace（主仓 1145 文件级扫描，
                隔离 ws 场景将耗时数百秒并误把主仓算作目标）。

        Returns:
            daemon 结构化统计：
            scanned（本轮访问的可索引文件数）、inserted（首次入库或内容变化的
            文件数）、unchanged（可解析且内容未变化的文件数）、
            skipped_cached（其中靠 mtime 缓存直接跳过读取/解析的文件数）、
            deleted（磁盘上已消失、本轮清理掉图行的文件数）、
            skipped_unparsed（语言无 parser 被跳过的文件数，如 .vue/.svelte）、
            symbols/calls（本轮解析产出的符号/调用数）、
            calls_resolved/calls_ambiguous（仅针对本轮受影响名字的边重算结果）。
        """
        params = {"workspace_instance_id": workspace_instance_id} if workspace_instance_id else {}
        if _is_remote_profile():
            # S2 §16.4：remote profile 下 build_graph 在客户端本机全量扫描，
            # 产物经 snapshot.upload 发布回 daemon（权威面）。workspace_instance_id
            # 指向 daemon 侧宿主视图，远端语义下由 ensure_remote_workspace 注册。
            _reject_remote_params(params, "build_graph")
            root = _remote_workspace_root()
            with _remote_codegraph(root) as cg:
                cg.build_full_graph(force=True)
                result = _remote_build_stats(cg)
                result.update(_remote_upload_built_db(cg))
            result["exec_origin"] = _EXEC_ORIGIN_CLIENT
            return result
        return _route('workspace.build_graph', params, 'PROTECTED_MUTATION')

    @mcp.tool()
    def refresh_file(file_path: str) -> dict:
        """刷新单个文件（增量更新）

        Args:
            file_path: 文件路径（相对或绝对路径）

        Returns:
            daemon 结构化结果（ok/file_path/content_hash/total_lines/symbols/calls）
        """
        return _route('workspace.file.refresh_file', {"file_path": file_path}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def list_workspaces() -> list:
        """列出所有工作区

        HTTP 模式（H6，CW_DAEMON_TRANSPORT=http）：经 RPC workspace.list
        （无参，Rust handler `handle_workspace_list` 按 peer uid 返回
        daemon_workspaces 行数组，无需 workspace_instance_id）。返回 daemon
        视图（每行字段：workspace_id/workspace_instance_id/snapshot_id/
        owner_uid/git_remote_url/git_head_commit_sha/client_view_root/
        host_real_root/toolchain_fingerprint/registered_at/last_active_at/
        status）。与 legacy db.list_workspaces()（workspaces 表行：
        id/name/root_path/created_at/is_active/description/active_task_id）
        字段差异做最小兼容映射：每行 client_view_root→root_path、name 用
        os.path.basename(client_view_root) 兜底；daemon 行无 is_active/
        created_at/description 字段。
        """
        return _route('workspace.list', {}, 'READ_ONLY')

    @mcp.tool()
    def register_workspace(name: str, root_path: str, description: str = "") -> dict:
        """注册新工作区

        Args:
            name: 工作区名称（唯一）
            root_path: 工作区根目录绝对路径
            description: 描述

        Returns:
            daemon workspace.register 的结构化行（workspace_id /
            workspace_instance_id / snapshot_id / owner_uid / git_remote_url /
            git_head_commit_sha / client_view_root / host_real_root /
            toolchain_fingerprint / registered_at / last_active_at / status）。

        HTTP 模式（H6，CW_DAEMON_TRANSPORT=http）：SQLite workspaces 表为
        真相源（先 db.register_workspace，name/root_path 重复时幂等返回已有
        id），再经 HttpDaemonRpcClient.workspace_register 同步 daemon 注册表
        （daemon_workspaces 是读面 workspace.list/status 的数据源，必须同步
        否则读面不可见）。daemon 不可用 → DaemonUnavailableError（fail-closed，
        禁止静默回退纯 SQL 造成双表分裂）；重试即自愈（register 两侧幂等）。

        F-012（2026-09-24）：输出模型原标注 ``-> int``（legacy SQLite
        register_workspace 返回数字 id），但收敛后 route_rpc 原样透传 daemon
        ``workspace.register`` 的结构化 dict。旧 int 签名使 FastMCP 输出校验
        失败（isError=True，尽管注册本身成功）——与 build_graph 等三工具
        bool→dict 放宽（见本文件头注释）同一类漏网。
        """
        return _route('workspace.register', {"name": name, "client_view_root": root_path, "description": description}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def set_active_workspace(workspace_id_or_name: str) -> dict:
        """设置活动工作区

        Args:
            workspace_id_or_name: 工作区 ID（数字字符串）或名称

        Returns:
            daemon workspace.activate 后的 workspace 行（status=active，
            字段同 register_workspace）。

        HTTP 模式（H6）：SQLite workspaces 表为真相源（先
        db.set_active_workspace 更新 is_active，workspace 不存在返回 False），
        再经 HttpDaemonRpcClient.workspace_activate 同步 daemon 注册表状态。
        daemon 不可用 → DaemonUnavailableError（fail-closed，不静默成功）。

        F-012（2026-09-24）：输出模型原标注 ``-> bool``（legacy SQLite
        语义），但 route_rpc 原样透传 daemon ``workspace.activate`` 的结构化
        行（Rust handle_workspace_activate 返回 get_workspace_status 行）。
        旧 bool 签名使 FastMCP 输出校验失败——与 register_workspace 同类。
        """
        return _route('workspace.activate', {"workspace_id_or_name": workspace_id_or_name}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def delete_workspace(workspace_id_or_name: str) -> dict:
        """删除工作区（级联删除所有实例和版本）

        Args:
            workspace_id_or_name: 工作区 ID（数字字符串）或名称

        Returns:
            daemon workspace.remove 后的 workspace 行（status=archived，
            字段同 register_workspace）。

        HTTP 模式（H6）：SQLite workspaces 表为真相源（先硬删，workspace
        不存在返回 False），再经 HttpDaemonRpcClient.workspace_remove 同步
        daemon 注册表（Rust remove 为 archive 软删语义，读面 owned ACL 已
        排除 archived 行）。daemon 不可用 → DaemonUnavailableError
        （fail-closed）；此时 SQLite 已删、daemon 行保留，重试会因 SQLite
        行不存在返回 False（不重复删），daemon 残留行由后续同 root 注册
        INSERT OR REPLACE 覆盖或 workspace_remove 归档自愈。

        F-012（2026-09-24）：输出模型原标注 ``-> bool``（legacy SQLite
        语义），但 route_rpc 原样透传 daemon ``workspace.remove`` 的结构化
        行（Rust handle_workspace_remove 返回 get_workspace_status 行）。
        旧 bool 签名使 FastMCP 输出校验失败——与 register_workspace 同类。
        """
        return _route('workspace.remove', {"workspace_id_or_name": workspace_id_or_name}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_active_workspace() -> Optional[dict]:
        """获取当前活动工作区信息

        HTTP 模式（H6，CW_DAEMON_TRANSPORT=http）：经 HttpDaemonRpcClient
        workspace_status 便捷方法（W1-1，T-1786808777378-bbcbf059）查询——
        便捷方法先 _ensure_remote_snapshot(db_path) 注册当前 workspace 拿权威
        workspace_instance_id 后调 workspace.status（Rust native 读方法，
        read_only + owned ACL：owner_uid 匹配且非 archived；缺注入时 Rust 强制
        require_str_param 返回 invalid_params——即修复前调 workspace.activate {}
        的缺陷）。"当前活动工作区"在 HTTP 模式下即当前配置 workspace 的
        daemon 视图（daemon_workspaces 行）。返回结构透传 daemon 行
        （workspace_id/workspace_instance_id/snapshot_id/owner_uid/
        git_remote_url/git_head_commit_sha/client_view_root/host_real_root/
        toolchain_fingerprint/registered_at/last_active_at/status），并做与
        legacy workspaces 表行（id/name/root_path/created_at/is_active/
        description/active_task_id）的最小兼容映射：client_view_root→root_path、
        host_real_root 保留、name 用 os.path.basename(client_view_root) 兜底；
        daemon 行无 is_active/created_at/description 字段（HTTP 模式以单
        workspace 语义替代 legacy is_active）。
        """
        return _route('workspace.status', {}, 'READ_ONLY')

    @mcp.tool()
    def file_read(file_path: str, offset: int = 0, limit: int = 200, include_context: bool = False) -> Optional[dict]:
        """读取文件内容（可选附带符号上下文）

        Agent 通过此工具读取文件，替代 IDE 内置 Read 工具。
        支持行号偏移和行数限制，避免一次性返回过大内容。

        L4 赋能：include_context=True 时合并返回文件中的符号列表 + 每个函数符号的
        调用方/被调用方摘要（top 3），减少 Agent 3+N 次 MCP 往返为 1 次。

        Args:
            file_path: 文件路径（相对工作区根目录或绝对路径）
            offset: 起始行号（从 0 开始，默认 0）
            limit: 读取行数（默认 200）
            include_context: 是否附带符号上下文（符号列表+调用方/被调用方摘要，默认 False）

        Returns:
            dict: {path, total_lines, offset, limit, content}，include_context=True 时
                  额外返回 {symbols, symbol_contexts}，文件不存在返回 None
        """
        if _is_remote_profile():
            # S2 §16.4：remote profile 下 file.read 客户端本机执行，零 RPC。
            # 与 daemon handle_file_read 同构（Rust str::lines 语义 + 入参
            # file_path 原样回传）；include_context 契约漂移项同样不处理。
            _reject_remote_params({"file_path": file_path}, "file_read")
            root = _remote_workspace_root()
            real = _remote_validate(root, file_path, require_file=True)
            try:
                with open(real, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except (OSError, UnicodeDecodeError) as e:
                raise DaemonRemoteError("file_read_failed", f"读取 {real} 失败: {e}")
            lines = _rust_lines(content)
            offset = max(0, offset)
            limit = max(1, limit)
            start = min(offset, len(lines))
            end = min(start + limit, len(lines))
            return {
                "file_path": file_path,
                "offset": offset,
                "limit": limit,
                "total_lines": len(lines),
                "content": "\n".join(lines[start:end]),
                "exec_origin": _EXEC_ORIGIN_CLIENT,
            }
        return _route('workspace.file.read', {"file_path": file_path, "offset": offset, "limit": limit, "include_context": include_context}, 'READ_ONLY')

    @mcp.tool()
    def file_grep(pattern: str, path: str = "", glob: str = "", output_mode: str = "files_with_matches", head_limit: int = 50) -> dict:
        """在工作区内搜索文件内容（ripgrep 风格）

        Agent 通过此工具搜索代码内容，替代 IDE 内置 Grep 工具。
        支持正则表达式、glob 过滤、多种输出模式。

        Args:
            pattern: 搜索模式（支持正则表达式）
            path: 搜索起始路径（相对或绝对，默认为工作区根）
            glob: 文件 glob 过滤（如 "*.py"、"**/*.ts"）
            output_mode: 输出模式（files_with_matches / content / count）
            head_limit: 最大返回结果数（默认 50）

        Returns:
            dict: {results, count}
        """
        if _is_remote_profile():
            # S2 §16.4：remote profile 下 file.grep 客户端本机执行。
            # 与 daemon handle_file_grep 同构：大小写不敏感子串匹配（非正则）、
            # 递归扫描（SKIP_DIRS + ignore 契约 + 可索引扩展名）、glob 过滤、
            # head_limit 双层截断；output_mode 契约漂移项同样不处理。
            params = {"pattern": pattern, "path": path, "glob": glob, "head_limit": head_limit}
            _reject_remote_params(params, "file_grep")
            if not pattern:
                raise DaemonRemoteError("invalid_params", "pattern 不能为空")
            root = _remote_workspace_root()
            base_rel = path.replace("\\", "/") if path else ""
            if base_rel:
                _remote_validate(root, base_rel, require_file=False)
            head_limit = max(1, head_limit)
            files = _remote_scan_indexable(
                root, base_rel, True, _remote_load_ignore_dir_names(root)
            )
            needle = pattern.lower()
            matches: List[Dict[str, Any]] = []
            for rel in files:
                if len(matches) >= head_limit:
                    break
                if glob and not _remote_glob_match(rel, glob):
                    continue
                try:
                    with open(os.path.join(root, rel), "r", encoding="utf-8", errors="replace") as fh:
                        content = fh.read()
                except OSError:
                    continue
                for idx, line in enumerate(_rust_lines(content)):
                    if len(matches) >= head_limit:
                        break
                    if needle in line.lower():
                        matches.append({"file_path": rel, "line": idx + 1, "text": line})
            return {
                "matches": matches,
                "count": len(matches),
                "exec_origin": _EXEC_ORIGIN_CLIENT,
            }
        return _route('workspace.file.grep', {"pattern": pattern, "path": path, "glob": glob, "output_mode": output_mode, "head_limit": head_limit}, 'READ_ONLY')

    @mcp.tool()
    def file_list(path: str = "", glob: str = "") -> list:
        """列出目录下的文件

        Agent 通过此工具浏览目录结构，替代 IDE 内置 Glob/LS 工具。

        Args:
            path: 目录路径（相对或绝对，默认为工作区根）
            glob: 文件 glob 过滤（如 "*.py"）

        Returns:
            文件/目录列表
        """
        if _is_remote_profile():
            # S2 §16.4：remote profile 下 file.list 客户端本机执行，返回裸数组
            # （与 daemon 一致，不加 exec_origin）。非递归列目录，不隐藏
            # dot 文件（daemon read_dir 原样列出），含 is_dir/size。
            _reject_remote_params({"path": path, "glob": glob}, "file_list")
            root = _remote_workspace_root()
            base_rel = path.replace("\\", "/") if path else ""
            if base_rel:
                _remote_validate(root, base_rel, require_file=False)
            base = os.path.join(root, base_rel) if base_rel else root
            rows: List[Dict[str, Any]] = []
            try:
                entries = os.listdir(base)
            except OSError:
                entries = []
            for name in entries:
                full = os.path.join(base, name)
                rel = os.path.relpath(full, root).replace("\\", "/")
                if glob and not _remote_glob_match(rel, glob):
                    continue
                try:
                    size = os.path.getsize(full)
                except OSError:
                    size = 0
                rows.append({"rel_path": rel, "size": size, "is_dir": os.path.isdir(full)})
            rows.sort(key=lambda r: r["rel_path"])
            return rows
        return _route('workspace.file.list', {"path": path, "glob": glob}, 'READ_ONLY')

    @mcp.tool()
    def file_symbol_content(file_path: str, symbol_name: str) -> Optional[dict]:
        """读取文件中指定符号的源码内容

        结合数据库中的符号位置信息，精确读取函数/类/方法的源码，
        比 file_read 更高效，Agent 无需计算行号范围。

        Args:
            file_path: 文件路径（相对或绝对）
            symbol_name: 符号名称

        Returns:
            dict: {symbol_name, file, start_line, end_line, content}
        """
        return _route('workspace.file.symbol_content', {"file_path": file_path, "symbol_name": symbol_name}, 'READ_ONLY')

    @mcp.tool()
    def import_git_history(max_commits: int = 100) -> dict:
        """导入 Git 历史记录到数据库

        W4-4（T-1786886251769-22b94ee8-sub-4）：写面通道决策——本工具是
        governance_write（INSERT OR IGNORE git_commits）+ 依赖 git 子进程
        （workspace_root 下 .git + `git log`），不适合 rust_native 迁移；
        HTTP 模式 fail-closed（明确写面不支持声明，见 ledger §9.25），
        local/legacy 模式保持本地 SQL 语义（代码见下）。

        Args:
            max_commits: 最大导入 commit 数量（默认 100）

        Returns:
            导入结果，包含成功状态和导入数量
        """
        _res = _route('task.job_submit', {**{"max_commits": max_commits}, "job_type": "git_history", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def get_git_commits(limit: int = 20, offset: int = 0) -> list:
        """获取 Git commit 列表

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.git_commits，经
        snapshot query_db_path 访问主库 git_commits，注入权威
        workspace_instance_id）；local/legacy 模式保留本地 SQL 语义。

        Args:
            limit: 返回数量限制（默认 20）
            offset: 偏移量（默认 0）

        Returns:
            commit 列表，按时间倒序排列
        """
        return _route('query.git_commits', {"limit": limit, "offset": offset}, 'READ_ONLY')

    @mcp.tool()
    def get_commit_changes(commit_hash: str) -> dict:
        """获取指定 commit 的变更详情

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.git_commit_changes，
        经 snapshot query_db_path 访问主库 git_commits + git_file_changes，
        注入权威 workspace_instance_id）；local/legacy 模式保留本地 SQL 语义。

        Args:
            commit_hash: commit 哈希值

        Returns:
            commit 详情和变更文件列表
        """
        return _route('query.git_commit_changes', {"commit_hash": commit_hash}, 'READ_ONLY')

    @mcp.tool()
    def get_git_stats() -> dict:
        """获取 Git 集成统计信息

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.git_stats，经
        snapshot query_db_path 访问主库 git_commits + git_file_changes，
        注入权威 workspace_instance_id）；local/legacy 模式保留本地 SQL 语义。

        Returns:
            Git 相关统计数据（commit 数、文件变更数、变更类型分布）
        """
        return _route('query.git_stats', {}, 'READ_ONLY')

    @mcp.tool()
    def get_status() -> dict:
        """获取代码图谱的【完整状态概览】（workspace/文件/语言/符号分布 + 注释覆盖
        + 缺陷统计等全面信息）。

        这是面向"当前整体健康状况"的综合视图。若只需要文件数/函数数/调用数这类
        **纯计数指标**，请用更轻量的 get_stats。注意区分：get_status=状态概览（全面），
        get_stats=统计数值（计数）。

        Returns:
            完整的状态概览字典
        """
        return _route('query.status', {}, 'READ_ONLY')

    @mcp.tool()
    def remove_file(file_path: str) -> dict:
        """从图谱中移除指定文件（标记为删除，保留历史）

        Args:
            file_path: 文件的绝对路径或相对工作区路径

        Returns:
            daemon 结构化结果 ``{"ok": true, "removed": true}``（物理删除
            + file_instances 行删除 + destructive_operations 审计记录）。

        F-012（2026-09-24）：输出模型原标注 ``-> bool``，但 route_rpc 原样
        透传 daemon ``workspace.file.remove`` 的结构化 dict（Rust
        handle_file_remove 返回 json!({"ok": true, "removed": true})）。
        旧 bool 签名使 FastMCP 输出校验失败——与 register_workspace 同类。
        """
        return _route('workspace.file.remove', {"file_path": file_path}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def build_directory(dir_path: str) -> dict:
        """构建指定目录的代码图谱

        Args:
            dir_path: 目录的绝对路径

        Returns:
            daemon 结构化结果：ok（布尔） / scanned（该目录扫描到的可索引文件数） /
            refreshed（本轮刷新入库的文件数）/ symbols/calls（本轮解析产出的
            符号/调用数）/ calls_resolved/calls_ambiguous（本轮受影响名字的
            边重算结果）。
        """
        if _is_remote_profile():
            # S2 §16.4：remote profile 下 build_directory 在客户端本机做目录
            # 子集构建（recursive=false，与 daemon handler 默认一致），产物经
            # snapshot.upload 发布回 daemon。
            _reject_remote_params({"dir_path": dir_path}, "build_directory")
            if not dir_path:
                raise DaemonRemoteError("invalid_params", "dir_path 不能为空")
            root = _remote_workspace_root()
            base_rel = dir_path.replace("\\", "/")
            _remote_validate(root, base_rel, require_file=False)
            files = _remote_scan_indexable(
                root, base_rel, False, _remote_load_ignore_dir_names(root)
            )
            stage_timings: Dict[str, Any] = {}
            stat_dict: Dict[str, Any] = {}
            with _remote_codegraph(root) as cg:
                cg._build_multi_lang(files, force=True)
                stage_timings = getattr(cg, "_stage_timings", {}) or {}
                stat_dict = cg.get_stats() or {}
                result: Dict[str, Any] = {
                    "ok": True,
                    "scanned": len(files),
                    "refreshed": int(stage_timings.get("files_parsed", 0) or 0),
                    "symbols": int(stat_dict.get("total_symbols", 0) or 0),
                    "calls": int(stat_dict.get("total_calls", 0) or 0),
                    "calls_resolved": int(stat_dict.get("resolved_calls", 0) or 0),
                }
                result["calls_ambiguous"] = max(0, result["calls"] - result["calls_resolved"])
                result.update(_remote_upload_built_db(cg))
            result["exec_origin"] = _EXEC_ORIGIN_CLIENT
            return result
        return _route('workspace.build_directory', {"dir_path": dir_path}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_symbol_content_by_hash(content_hash: str) -> Optional[dict]:
        """根据内容哈希获取符号的完整内容

        Args:
            content_hash: 符号内容的 SHA-256 哈希

        Returns:
            符号内容详情，包含完整代码
        """
        return _route('query.symbol_content_by_hash', {"content_hash": content_hash}, 'READ_ONLY')

    @mcp.tool()
    def get_code_metrics_summary() -> CodeMetricsSummary:
        """获取代码度量汇总统计

        包含文件数、函数数、总代码行、调用关系数、
        平均/最高圈复杂度、复杂度分布、注释覆盖率等。

        Returns:
            CodeMetricsSummary：显式 output 契约（见该 TypedDict），
            由 FastMCP 生成 outputSchema 并校验 daemon 回包。
        """
        return _route('query.metrics_summary', {}, 'READ_ONLY')

    @mcp.tool()
    def get_complexity_hotspots(limit: int = 20, module_filter: str = "") -> list:
        """获取圈复杂度最高的函数（复杂度热点）

        用于识别需要重构的复杂函数。

        Args:
            limit: 返回数量限制（默认 20）
            module_filter: 模块路径前缀过滤

        Returns:
            按圈复杂度降序排列的函数列表
        """
        return _route('query.complexity_hotspots', {"limit": limit, "module_filter": module_filter}, 'READ_ONLY')

    @mcp.tool()
    def get_coupling_analysis(limit: int = 30) -> list:
        """获取模块耦合度分析

        分析模块间调用关系，计算每个模块的传入/传出耦合度和不稳定性。

        Args:
            limit: 返回数量限制（默认 30）

        Returns:
            按总耦合度降序排列的模块列表
        """
        return _route('query.coupling_analysis', {"limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_function_metrics(qualified_name: str) -> Optional[dict]:
        """获取单个函数的度量详情

        Args:
            qualified_name: 函数限定名

        Returns:
            度量字典，包含圈复杂度、行数、扇入扇出、深度等
        """
        return _route('query.function_metrics', {"qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def get_largest_functions(limit: int = 20, module_filter: str = "") -> list:
        """获取代码行数最多的函数

        Args:
            limit: 返回数量限制（默认 20）
            module_filter: 模块路径前缀过滤

        Returns:
            按行数降序排列的函数列表
        """
        return _route('query.largest_functions', {"limit": limit, "module_filter": module_filter}, 'READ_ONLY')

    @mcp.tool()
    def get_most_coupled_functions(limit: int = 20) -> list:
        """获取耦合度最高的函数（扇入+扇出最大）

        Args:
            limit: 返回数量限制（默认 20）

        Returns:
            按总耦合度降序排列的函数列表
        """
        return _route('query.most_coupled_functions', {"limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_code_health_check(severity: str = "all") -> dict:
        """代码健康检查：识别大文件、复杂函数、高耦合模块等问题

        ⚠️  AI Agent 修改代码前强烈建议先调用此工具！

        检查内容：
        - 过大文件（>500/1000/2000 行）
        - 复杂函数（圈复杂度 >10/20/30）
        - 超长函数（>50/100/200 行）
        - 高耦合模块（不稳定性 >0.7/0.9）

        使用建议：
        1. 优先修改小文件，大文件修改前先考虑拆分
        2. 复杂函数修改前先理解完整逻辑，或先拆分成小函数
        3. 如果一个函数超过 200 行或复杂度 >30，建议先重构再修改

        Args:
            severity: 过滤严重程度（all / high / medium / low）

        Returns:
            健康检查报告，包含评分、问题分类列表和 Agent 指导
        """
        return _route('query.code_health', {"severity": severity}, 'READ_ONLY')

    @mcp.tool()
    def check_file_health(file_path: str) -> Optional[dict]:
        """检查单个文件的健康状态（Agent 修改文件前必调用）

        ⚠️  重要：在读取或修改任何文件之前，先调用此工具检查文件健康状态！

        如果返回 should_split_first = true，说明文件或其中函数过大，
        直接全量修改可能导致：
        - Token 溢出，AI 无法完整理解
        - 写入失败，文件过大难以一次性写回
        - 逻辑错误，复杂函数修改容易引入 bug

        建议：先拆分成小文件/小函数，再逐步修改。

        Args:
            file_path: 文件的绝对路径或相对工作区路径

        Returns:
            文件健康报告，包含大小、复杂度、是否建议先拆分等
        """
        return _route('workspace.file.health', {"file_path": file_path}, 'READ_ONLY')
