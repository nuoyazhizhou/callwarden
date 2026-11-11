"""Phase 4.8: Daemon Client —— MCP 查询工具的统一入口。

职责：
- 高频查询工具（get_callers/get_callees/search_symbols/get_symbol 等）通过 daemon client 查询
- daemon client 优先走 Rust GraphSnapshot（内存只读，无锁）
- snapshot 未发布时自动回退到 Python SQL 查询（兼容 local 模式）

设计参考：enterprise-daemon-shared-snapshot-plan.md §13.2 MCP 过渡策略

路由策略：
1. workspace 已发布 snapshot → 走 Rust GraphStore 查询（零 SQL，零磁盘 I/O）
2. snapshot 未发布 → 回退到 CodeGraphDB SQL 查询（兼容现有行为）
3. 查询结果记录路由来源（daemon/sql），用于监控和验证
"""

import base64
import contextlib
import gzip
import hashlib
import json
import logging
import os
import ssl
import subprocess
import tempfile
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from typing import Optional, List, Dict, Any

# 3.28: 自动唤起 + 互斥 + 降级分流接线
from callwarden.server.daemon_autostart import (
    ensure_http_daemon,
    resolve_http_endpoint_and_manifest,
)
from callwarden.server.daemon_mutex import DaemonMutex
from callwarden.server.degraded_mode import (
    route_degraded,
)

from callwarden.config import (
    HTTP_DEFAULT_TIMEOUT,
    HTTP_MAX_BODY_BYTES,
    HTTP_PROTOCOL_VERSION,
    E_HTTP_DAEMON_UNAVAILABLE,
    E_HTTP_MANIFEST_MISSING,
    E_HTTP_MANIFEST_STALE,
    E_HTTP_REQUEST_TIMEOUT,
    E_MODE_DEPRECATED,
    E_MTLS_CLIENT_CERT_MISSING,
    E_MTLS_UNKNOWN_CA,
    E_PROTOCOL_VERSION_UNSUPPORTED,
    E_REQUEST_TOO_LARGE,
    E_TLS_HANDSHAKE_FAILED,
    get_daemon_mode,
    get_http_authority_id,
    get_task_write_policy,
    http_method_timeout,
    is_daemon_required,
    is_http_transport_enabled,
    resolve_client_cert_paths,
)
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.snapshot_manager import SnapshotManagerService, get_snapshot_service
from callwarden.server.query_budget import default_budget

logger = logging.getLogger(__name__)
_NO_REMOTE = object()

# ADR-002 S2 §16.2：snapshot.upload 客户端参数契约（与 daemon 侧上限一致）。
# hash 契约针对 gunzip 后的原始 SQLite 字节（sha256 + raw_size 双校验）。
SNAPSHOT_UPLOAD_INLINE_MAX_BYTES = 32 * 1024 * 1024   # gzip 流 ≤32MiB 走内联
SNAPSHOT_UPLOAD_CHUNK_MAX_BYTES = 16 * 1024 * 1024    # 单块上限（total_chunks ≥ 2 时切块）
SNAPSHOT_UPLOAD_MAX_CHUNKS = 4096                     # 块数上限
SNAPSHOT_UPLOAD_BODY_MAX_BYTES = 64 * 1024 * 1024     # 上传面 body 口径（覆盖通用 8MiB）


class DaemonUnavailableError(RuntimeError):
    """enterprise 模式要求 enterprise daemon，但 endpoint 不可用。

    统一结构化错误码（T03/Q7 fail-closed）：默认 `.code == E_HTTP_DAEMON_UNAVAILABLE`；
    local/legacy 非测试模式抛 E_MODE_DEPRECATED 时显式传 `code=E_MODE_DEPRECATED`。
    CLI/MCP 薄壳 `except DaemonUnavailableError` 可检查 `.code`。
    """

    code = E_HTTP_DAEMON_UNAVAILABLE

    def __init__(self, message: str, code: str = E_HTTP_DAEMON_UNAVAILABLE) -> None:
        super().__init__(message)
        self.code = code


class MultipartUnsupportedError(DaemonUnavailableError):
    """daemon 不支持 `POST /v1/rpc/multipart`（版本早于 Q10）。

    caller 捕获本异常后应降级到 JSON 内联路径（`call` + gz_b64），
    保证 client 新版本对旧 daemon 的前向兼容（滚动部署窗口）。
    """

    def __init__(self, message: str = "daemon 不支持 /v1/rpc/multipart（旧版本）") -> None:
        super().__init__(message, code="E_MULTIPART_ROUTE_UNAVAILABLE")


class SharedTaskWriterRequiredError(DaemonUnavailableError):
    """共享任务库禁止 local 进程绕过 daemon 单写点。"""

    code = "E_SHARED_TASK_WRITER_REQUIRED"

    def __init__(self, message: str):
        # C-09：‘code’ 必须显式回传，否则父类 DaemonUnavailableError.__init__
        # 会把实例属性覆盖回 E_HTTP_DAEMON_UNAVAILABLE（类属性被实例属性遮蔽），
        # 导致调用方 `except DaemonUnavailableError` 侧无法据 .code 判别该写点拒绝。
        super().__init__(f"{self.code}: {message}", code=self.code)


def _is_task_write(rpc_method: str) -> bool:
    """判断 RPC 是否会修改权威任务状态或任务归属。"""
    return rpc_method.startswith("task.")


def _rpc_timeout_for(method: str, base_timeout: float) -> float:
    """按 RPC method 与 caller 基准超时选择实际同步超时（秒）。

    HTTP / pipe 两种 transport 共用此规则（build_graph 等慢方法在两种
    transport 上都会超过默认 30s）：

    - caller 显式传入短于 HTTP_DEFAULT_TIMEOUT 的超时（诊断/测试场景，
      如 5s 快速失败）→ 尊重之，原样返回；
    - 其余（默认值或更长）→ 慢方法放宽到 HTTP_SLOW_METHOD_TIMEOUT，
      快方法保持 base_timeout（通常即 HTTP_DEFAULT_TIMEOUT）。
    """
    if base_timeout < HTTP_DEFAULT_TIMEOUT:
        return base_timeout
    return http_method_timeout(method)


# P0-H（T-1787277487109-758e56d0）：task.supersede 治理路由策略（显式声明，供
# route_task_write / route_task_read 与测试断言）。task.supersede 是 daemon-native
# governance mutation：非 local 模式下 daemon 不可用一律 fail-closed，绝不回退本地
# SQLite；task.superseded_by 只读投影同样仅由 daemon 提供（无 local fallback）。
TASK_SUPERSEDE_ROUTE_POLICY = {
    "task.supersede": {"class": "governance_write", "fallback": "forbidden"},
    "task.superseded_by": {"class": "read_only", "fallback": "forbidden"},
}


def get_supersede_route_policy(method: str) -> dict:
    """P0-H：返回 task.supersede / task.superseded_by 的显式路由策略（缺省 fail-closed）。"""
    return TASK_SUPERSEDE_ROUTE_POLICY.get(
        method, {"class": "unknown", "fallback": "forbidden"}
    )


def assert_supersede_no_local_fallback(method: str, mode: str):
    """P0-H：非 local 模式下禁止回退本地 SQLite（fail-closed，供测试断言）。

    local 模式由调用方 fallback_func 决定（task.supersede 的 fallback 本身即
    raise DaemonUnavailableError，见 cli/main.py _local_supersede_forbidden）。
    """
    if mode != "local":
        raise DaemonUnavailableError(
            f"{method} 仅由 daemon 权威提供；daemon 不可用时禁止回退本地 SQLite（fail-closed）"
        )


# ----------------------------------------------------------------------
# workspace_instance_id 推导
# ----------------------------------------------------------------------

def derive_workspace_instance_id(project_root: str) -> str:
    """从项目根路径推导 workspace_instance_id（跨进程标识符）。

    用项目根路径的 SHA-256 前 16 位作为 workspace_instance_id，确保同一项目
    在不同进程（CLI / MCP / daemon）中标识一致。
    注意：此 hash 仅用于 workspace 标识，不再用于数据库路径（数据库已改为用户级统一路径）。

    CR7（2026-09-09）：路径写法归一化——realpath 解析符号链接/junction 后，
    反斜杠→正斜杠、Windows 全路径 ASCII 小写（NTFS 大小写不敏感）、strip 尾部斜杠。
    此前 `C:/…` 与 `c:/…`（或大小写差异）会派生不同 id → 客户端 cwd 写法
    差异即产生新实例（与 daemon 侧 normalize_identity_root 同语义）。
    """
    abs_root = os.path.realpath(os.path.abspath(project_root))
    norm_root = abs_root.replace("\\", "/")
    if os.name == "nt":
        # 与 Rust normalize_identity_root 对齐：仅 ASCII 小写，非 ASCII 原样
        norm_root = "".join(
            ch.lower() if "A" <= ch <= "Z" else ch for ch in norm_root
        )
    while len(norm_root) > 1 and norm_root.endswith("/"):
        norm_root = norm_root.rstrip("/")
    return hashlib.sha256(norm_root.encode("utf-8")).hexdigest()[:16]


def _norm_root(root_path: str) -> str:
    """规范化 root_path 作映射缓存 key（对齐 config.norm_path：正斜杠 + 盘符小写）。

    W1-2（T-1786808777379-15702f0c）：避免 `C:\foo` 与 `c:/foo` 在
    `_workspace_instance_by_root` 缓存中分裂为两个 key。
    """
    if not root_path:
        return root_path
    normalized = root_path.replace("\\", "/")
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        normalized = normalized[0].lower() + normalized[1:]
    return normalized


def _workspace_snapshot_metadata(project_root: str) -> Dict[str, str]:
    """读取注册 workspace 所需的稳定 Git provenance。

    daemon 根据 remote/head/toolchain 生成权威 snapshot_id。客户端不能自行
    生成或猜测该 ID；这里只把当前 checkout 的 Git 元数据传给 authority。
    Git 不可用时保持空值，让 daemon 返回明确的无 snapshot 身份结果。
    """
    if not project_root:
        return {}

    # FIX(2026-10-29 B 类重测挂死)：MCP stdio 宿主的 stdin 是一条长期打开、
    # 由宿主按需写入的 JSON-RPC 管道。子进程默认继承该句柄——`git` 一旦在该
    # 管道上阻塞读取，`subprocess.run` 永不返回，单线程 MCP server 随之整体
    # 卡死（表现为所有 tools/call 超时，daemon 侧却全程空闲）。故显式切断
    # stdin，并加超时兜底：元数据是可选 provenance，取不到就退化为空值，
    # 绝不阻塞宿主。
    def _git(args: list) -> str:
        try:
            return subprocess.run(
                ["git", "-C", project_root, *args],
                capture_output=True, text=True, check=False,
                stdin=subprocess.DEVNULL, timeout=10,
            ).stdout.strip()
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return ""

    remote = _git(["config", "--get", "remote.origin.url"])
    head = _git(["rev-parse", "HEAD"])
    if remote and head:
        return {"git_remote_url": remote, "git_head_commit_sha": head}
    return {}


# ----------------------------------------------------------------------
# DaemonClient
# ----------------------------------------------------------------------

class DaemonClient:
    """MCP 查询工具的 daemon client。

    优先走 Rust GraphSnapshot（内存只读），回退到 Python SQL。

    用法：
        client = DaemonClient.get_instance()
        callers = client.get_callers("function_name", qualified_name="mod.fn")
    """

    _instance: Optional["DaemonClient"] = None

    # H2：标识该 client 是否走 HTTP MVP transport（legacy 恒为 False）。
    is_http_client: bool = False

    def __init__(self, socket_path: Optional[str] = None):
        self._svc: SnapshotManagerService = get_snapshot_service()
        # 单轨（ADR-002）：唯一 RPC 传输是 HTTPS+mTLS。`socket_path` 参数仅为
        # 调用点兼容保留——其值现在是 HTTPS endpoint 字符串，而非 socket/pipe 路径；
        # 缺省时由 HttpDaemonRpcClient.discover() 经 manifest 解析。
        self._rpc = HttpDaemonRpcClient(endpoint=socket_path)
        self._workspace_instance_id: Optional[str] = None
        self._remote_workspace_id: Optional[str] = None
        self._remote_snapshot_id: Optional[str] = None
        self._remote_snapshot_ready = False
        self._project_root: Optional[str] = None
        # 路由统计
        self._daemon_hits: int = 0
        self._sql_fallbacks: int = 0
        # 3.28: Degraded_Mode 计数 [Req 14.33]
        self._degraded_count: int = 0

    @classmethod
    def get_instance(cls) -> "DaemonClient":
        """获取单例。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """重置单例（测试用）。"""
        cls._instance = None

    def configure_workspace(self, project_root: str):
        """配置当前 workspace。

        Args:
            project_root: 项目根目录路径
        """
        self._project_root = project_root
        self._workspace_instance_id = derive_workspace_instance_id(project_root)
        self._remote_workspace_id = None
        self._remote_snapshot_id = None
        self._remote_snapshot_ready = False
        logger.debug(
            "DaemonClient 配置 workspace: root=%s id=%s",
            project_root, self._workspace_instance_id,
        )

    @property
    def workspace_instance_id(self) -> Optional[str]:
        return self._workspace_instance_id

    @property
    def daemon_hits(self) -> int:
        """通过 daemon（Rust snapshot）查询的次数。"""
        return self._daemon_hits

    @property
    def sql_fallbacks(self) -> int:
        """回退到 SQL 查询的次数。"""
        return self._sql_fallbacks

    @property
    def degraded_count(self) -> int:
        """Degraded_Mode 下执行的操作次数 [Req 14.33]。"""
        return self._degraded_count

    def is_daemon_ready(self) -> bool:
        """daemon snapshot 是否已就绪（已发布且 Rust 后端可用）。"""
        if get_daemon_mode() != "local":
            try:
                self._rpc.verify_health()
                return self._remote_snapshot_ready
            except Exception:
                if is_daemon_required():
                    raise
        if self._workspace_instance_id is None:
            return False
        return self._svc.ensure_workspace(self._workspace_instance_id)

    def rpc_call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        """公开的底层 RPC 入口，供 CLI 管理命令使用。"""
        return self._rpc.call(method, params)

    def call_with_autostart(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """带自动唤起与降级分流的 RPC 调用 [Req 14.22–14.30, 14.33]。

        流程：
        1. 尝试 RPC 调用
        2. 连接失败 → 获取互斥 → 自动唤起 daemon → 退避重试
        3. 唤起成功 → 在新连接上继续原请求
        4. 唤起失败（窗口耗尽）→ 按 class(op) 分流：
           - read_only: 返回降级标记，由调用方走 SQL 回退
           - Index_Write: 返回降级标记，由调用方直连写入
           - Governance_Write: fail closed，抛出 DaemonUnavailableError

        Returns:
            {"result": ..., "degraded": False} 正常路径
            {"result": None, "degraded": True, "mode": "direct_read"/"direct_write",
             "op_class": ...} 降级路径（read_only/Index_Write）

        Raises:
            DaemonUnavailableError: Governance_Write 在 Degraded_Mode 下被拒绝
        """
        # 第一次尝试
        try:
            result = self._rpc.call(method, params)
            return {"result": result, "degraded": False}
        except DaemonUnavailableError:
            pass

        # 连接失败：尝试自动唤起（带互斥）[Req 14.22, 14.23]
        try:
            endpoint = self._rpc.discover()
        except Exception:
            endpoint = ""
        mutex = DaemonMutex(endpoint)
        if mutex.try_acquire():
            try:
                ready = ensure_http_daemon(
                    endpoint, readiness_check=lambda ep: self._rpc.verify_health(ep)
                )
                if ready is not None:
                    # daemon 已就绪，重试原请求
                    result = self._rpc.call(method, params)
                    return {"result": result, "degraded": False}
            finally:
                mutex.release()
        else:
            # 其他会话正在启动 daemon，等待窗口内退避重试
            ready = ensure_http_daemon(
                endpoint, readiness_check=lambda ep: self._rpc.verify_health(ep)
            )
            if ready is not None:
                result = self._rpc.call(method, params)
                return {"result": result, "degraded": False}

        # 唤起失败：进入 Degraded_Mode [Req 14.27–14.30]
        import sys
        platform = "windows" if sys.platform == "win32" else (
            "macos" if sys.platform == "darwin" else "linux"
        )
        decision = route_degraded(method, endpoint, platform)
        self._degraded_count += 1

        if decision.allowed:
            # read_only 或 Index_Write：返回降级标记
            logger.info(
                "Degraded_Mode: %s → %s (method=%s)",
                decision.op_class.value, decision.mode, method,
            )
            return {
                "result": None,
                "degraded": True,
                "mode": decision.mode,
                "op_class": decision.op_class.value,
            }
        else:
            # Governance_Write: fail closed [Req 14.30]
            reason = decision.reason
            raise DaemonUnavailableError(
                f"Degraded_Mode: Governance_Write 被拒 (method={method}, "
                f"code={reason.code if reason else 'unknown'}, "
                f"recovery={reason.recovery_guidance if reason else 'N/A'})"
            )

    def _ensure_daemon_endpoint(self) -> bool:
        """确认 daemon 可接受请求，auto 模式必要时自动唤起。

        单轨 HTTPS+mTLS：不再检查 socket 路径存在性（不存在该回退），
        以 HTTP `/health` 交叉核对作为活性探针；不可达时 auto 模式进入
        Degraded_Mode，enterprise 模式 fail-closed。
        """
        mode = get_daemon_mode()
        if mode == "local":
            return False

        try:
            self._rpc.verify_health()
            return True
        except Exception as exc:
            if mode == "enterprise":
                raise DaemonUnavailableError(
                    f"enterprise 模式要求 enterprise daemon，但 endpoint 不可用: {exc}"
                ) from exc
        return False

    def _ensure_remote_snapshot(self, db_path: Optional[str]) -> Optional[str]:
        """在 auto/enterprise 模式注册 workspace 并发布 snapshot。"""
        mode = get_daemon_mode()
        if mode == "local":
            return None
        if not self._ensure_daemon_endpoint():
            return None
        # 共存契约：windows-host authority 不可用时 fail-closed（auto 模式也不回退）
        from callwarden.config import get_daemon_authority
        remote_authority = get_daemon_authority() == "windows-host"
        try:
            if self._remote_workspace_id is None:
                root = self._project_root or os.getcwd()
                register_params = {"client_view_root": root}
                register_params.update(_workspace_snapshot_metadata(root))
                workspace = self._rpc.call("workspace.register", register_params)
                self._remote_workspace_id = workspace["workspace_instance_id"]
                self._remote_snapshot_id = workspace.get("snapshot_id") or None
            if db_path and not self._remote_snapshot_ready:
                published = self._rpc.publish_snapshot(
                    self._remote_workspace_id,
                    db_path,
                    snapshot_id=self._remote_snapshot_id or "",
                )
                published_id = published.get("snapshot_id") if isinstance(published, dict) else None
                if self._remote_snapshot_id and published_id != self._remote_snapshot_id:
                    raise DaemonUnavailableError(
                        "snapshot.publish 未返回与 workspace.register 一致的 snapshot_id",
                        code="E_SNAPSHOT_ID_MISMATCH",
                    )
                self._remote_snapshot_id = published_id or self._remote_snapshot_id
                self._remote_snapshot_ready = True
            return self._remote_workspace_id if self._remote_snapshot_ready else None
        except Exception:
            if mode == "enterprise" or remote_authority:
                raise
            logger.warning("daemon UDS 请求失败，auto 模式回退 local", exc_info=True)
            return None

    def _remote_query(self, method: str, params: Dict[str, Any],
                      db_path: Optional[str]) -> Any:
        # 共存契约：windows-host authority（WSL 访问 Windows daemon）不可用时
        # 必须 fail-closed，禁止 auto 模式回退本地 SQLite（本地是 WSL 库，非
        # Windows authority）。wsl-local/linux-system 且 auto 模式才允许回退。
        from callwarden.config import get_daemon_authority
        remote_authority = get_daemon_authority() == "windows-host"
        try:
            workspace_id = self._ensure_remote_snapshot(db_path)
        except DaemonUnavailableError:
            if get_daemon_mode() == "enterprise" or remote_authority:
                raise
            return _NO_REMOTE
        if workspace_id is None:
            return _NO_REMOTE
        request = dict(params)
        request["workspace_instance_id"] = workspace_id
        try:
            result = self._rpc.call(method, request)
        except DaemonUnavailableError:
            self._remote_snapshot_ready = False
            if get_daemon_mode() == "enterprise" or remote_authority:
                raise
            return _NO_REMOTE
        self._daemon_hits += 1
        return result

    # ------------------------------------------------------------------
    # 内部：确保 snapshot 已发布
    # ------------------------------------------------------------------

    def _ensure_snapshot(self, db_path: str) -> bool:
        """确保 workspace 的 snapshot 已发布。

        如果 snapshot 未发布且 Rust 后端可用，自动从 db_path 发布。
        """
        if self._workspace_instance_id is None:
            # 从 db_path 反推 workspace_instance_id
            parent_dir = os.path.basename(os.path.dirname(db_path))
            if len(parent_dir) == 16:
                self._workspace_instance_id = parent_dir

        if self._workspace_instance_id is None:
            return False

        if self._svc.ensure_workspace(self._workspace_instance_id):
            return True

        # 自动发布 snapshot
        if self._svc.rust_available and os.path.exists(db_path):
            try:
                result = self._svc.publish_snapshot(
                    self._workspace_instance_id, db_path
                )
                return result is not None
            except Exception as e:
                logger.warning("自动发布 snapshot 失败: %s", e)
                return False

        return False

    # ------------------------------------------------------------------
    # 查询接口（与 MCP 工具签名对齐）
    # ------------------------------------------------------------------

    def get_callers(
        self,
        callee_name: str,
        qualified_name: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询谁调用了指定函数。"""
        remote = self._remote_query("query.callers", {
            "callee_name": callee_name,
            "qualified_name": qualified_name,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_callers(
                self._workspace_instance_id, callee_name, qualified_name
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_get_callers(callee_name, qualified_name)

    def get_callees(
        self,
        caller_name: str,
        qualified_name: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询指定函数调用了哪些函数。"""
        remote = self._remote_query("query.callees", {
            "caller_name": caller_name,
            "qualified_name": qualified_name,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_callees(
                self._workspace_instance_id, caller_name, qualified_name
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_get_callees(caller_name, qualified_name)

    def search_symbols(
        self,
        query: str,
        kind: Optional[str] = None,
        limit: int = 20,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """搜索符号。"""
        remote = self._remote_query("query.search", {
            "query": query,
            "kind": kind,
            "limit": limit,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.search_symbols(
                self._workspace_instance_id, query, kind, limit
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_search_symbols(query, kind, limit)

    def get_symbol(
        self,
        qualified_name: str,
        db_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """按 qualified_name 精确查询符号，enterprise/auto 走 daemon RPC query.symbol。

        M2.2（T-1786526643663-594ee010）：daemon 不可用时 fail-closed，
        禁止静默回退本地 SQLite（统一验收标准第 5 项）。仅 local 模式
        （显式配置无 daemon）允许走本地 SQL。
        """
        mode = get_daemon_mode()
        remote = self._remote_query("query.symbol", {
            "qualified_name": qualified_name,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if mode == "local":
            self._sql_fallbacks += 1
            return self._sql_fallback_get_symbol(qualified_name)
        raise DaemonUnavailableError(
            "daemon 不可用（非 local 模式），query.symbol 不静默回退本地 SQLite"
        )

    def get_symbol_location(
        self,
        name: str,
        file_path: str = "",
        db_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """在指定文件中定位符号，优先走 enterprise snapshot RPC。

        M2.2（T-1786519211817-fcc40690）：daemon 不可用时 fail-closed，
        禁止静默回退本地 SQLite（统一验收标准第 5 项）。仅 local 模式
        （显式配置无 daemon）允许走本地 SQL。
        """
        remote = self._remote_query("query.symbol_location", {
            "name": name,
            "file_path": file_path,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        raise DaemonUnavailableError(
            "daemon 不可用，query.symbol_location 不静默回退本地 SQLite"
            "（SRV-006：local 模式本地 SQL 路径已随 _get_db 退役，fail-closed）"
        )

    def get_file_symbols(
        self,
        file_path: str,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询文件符号，enterprise/auto 走 daemon RPC query.file。

        M2.1（T-1786519351240-73127ab4）：daemon 不可用时 fail-closed，
        禁止静默回退本地 SQLite（统一验收标准第 5 项）。仅 local 模式
        （显式配置无 daemon）允许走本地 SQL。
        """
        remote = self._remote_query("query.file", {"file_path": file_path}, db_path)
        if remote is not _NO_REMOTE:
            return remote
        raise DaemonUnavailableError(
            "daemon 不可用，query.file 不静默回退本地 SQLite"
            "（SRV-006：local 模式本地 SQL 路径已随 _get_db 退役，fail-closed）"
        )

    def query_grep(
        self,
        patterns: List[str],
        fixed: bool = False,
        limit: int = 200,
        path: Optional[str] = None,
        include_all: bool = False,
        kind: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Any:
        """按 patterns 在 enterprise workspace 文本搜索（rg 风格），走 daemon RPC query.grep。

        M2.3（T-1786529505247-9d083e54）：daemon 不可用时 fail-closed，禁止
        静默返回 _NO_REMOTE 哨兵。query.grep 无本地 SQLite 回退（本地 grep 由
        CLI / MCP file_grep 负责），local 模式明确返回 None 表示"由本地 grep 组件处理"。
        """
        mode = get_daemon_mode()
        remote = self._remote_query("query.grep", {
            "patterns": patterns,
            "fixed": fixed,
            "limit": limit,
            "path": path,
            "include_all": include_all,
            "kind": kind,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if mode == "local":
            # local 模式无 daemon：grep 由本地 CLI/MCP file_grep 负责，client 不承担 SQL 回退
            return None
        raise DaemonUnavailableError(
            "daemon 不可用（非 local 模式），query.grep 不静默回退本地 SQLite"
        )

    def query_issues(
        self,
        qualified_name: str,
        include_info: bool = False,
        db_path: Optional[str] = None,
    ) -> Any:
        """按 qualified_name 查询符号缺陷（semgrep + guardrail findings），走 daemon RPC query.issues。

        M2.4（T-1786539379174-90f74174）：daemon 不可用时 fail-closed，禁止
        静默回退本地 SQLite（统一验收标准第 5 项）。query.issues 语义为按符号
        查询（与 get_symbol_issues 一致），全局正则缺陷扫描
        （get_issue_summary / find_issues）不走本方法（语义不对应）；local 模式
        （显式配置无 daemon）返回 None，表示由本地缺陷分析组件处理。
        """
        mode = get_daemon_mode()
        remote = self._remote_query("query.issues", {
            "qualified_name": qualified_name,
            "include_info": include_info,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if mode == "local":
            # local 模式无 daemon：缺陷分析由本地 IssueAnalyzerMixin 负责，
            # client 不承担 SQL 回退（与 M2.3 query.grep 约定一致）
            return None
        raise DaemonUnavailableError(
            "daemon 不可用（非 local 模式），query.issues 不静默回退本地 SQLite"
        )

    def get_symbol_issues(
        self,
        qualified_name: str,
        include_info: bool = False,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """查询符号问题，优先走 enterprise snapshot RPC。

        M2.4（T-1786519211831-fd9a5380，HTTP 轮次）：daemon 不可用时
        fail-closed，禁止静默回退本地 SQLite（统一验收标准第 5 项，对齐
        query_issues L1238 与 M2.2 get_symbol_location 修复模式）。仅 local
        模式（显式配置无 daemon）保留本地 SQL 路径（该路径是设计决策，非
        fallback）。CLI `cw issues` 直接走 db 层 CodeGraphDB.get_symbol_issues
        （cli/main.py L7864），不依赖本方法的 SQL 回退，无依赖链影响。
        """
        remote = self._remote_query("query.issues", {
            "qualified_name": qualified_name,
            "include_info": include_info,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        raise DaemonUnavailableError(
            "daemon 不可用，query.issues 不静默回退本地 SQLite"
            "（SRV-006：local 模式本地 SQL 路径已随 _get_db 退役，fail-closed）"
        )

    def query_tests(
        self,
        qualified_name: str,
        reverse: bool = False,
        history: bool = False,
        limit: int = 50,
        db_path: Optional[str] = None,
    ) -> Any:
        """按 qualified_name 查询测试关系，走 daemon RPC query.tests。

        M2.5（T-1786584287058-7f712ff4）：daemon 不可用时 fail-closed，禁止
        静默回退本地 SQLite（统一验收标准第 5 项）。query.tests 语义为按符号
        查询（test cases / tested functions / stability 由 reverse/history
        区分），无参全项目测试率统计（get_test_coverage，tools_query.py）
        语义不对应，不走本方法（遵循 M2.4 get_issue_summary 先例）；local
        模式（显式配置无 daemon）返回 None，表示由本地测试关系组件处理。
        """
        mode = get_daemon_mode()
        remote = self._remote_query("query.tests", {
            "qualified_name": qualified_name,
            "reverse": reverse,
            "history": history,
            "limit": limit,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if mode == "local":
            # local 模式无 daemon：测试关系查询由本地 TestRelationMixin 负责，
            # client 不承担 SQL 回退（与 M2.3 query.grep 约定一致）
            return None
        raise DaemonUnavailableError(
            "daemon 不可用（非 local 模式），query.tests 不静默回退本地 SQLite"
        )

    def get_test_cases(
        self,
        qualified_name: str,
        db_path: Optional[str] = None,
    ) -> Any:
        """查询正向测试关系，优先走 enterprise snapshot RPC（M2.5 fail-closed）。"""
        return self.query_tests(
            qualified_name, reverse=False, history=False, limit=50, db_path=db_path
        )

    def get_tested_functions(
        self,
        test_qualified_name: str,
        db_path: Optional[str] = None,
    ) -> Any:
        """反向查询测试覆盖函数，优先走 enterprise snapshot RPC（M2.5 fail-closed）。"""
        return self.query_tests(
            test_qualified_name, reverse=True, history=False, limit=50, db_path=db_path
        )

    def get_test_stability(
        self,
        qualified_name: str,
        limit: int = 50,
        db_path: Optional[str] = None,
    ) -> Any:
        """查询测试稳定性历史，优先走 enterprise snapshot RPC（M2.5 fail-closed）。"""
        return self.query_tests(
            qualified_name, reverse=False, history=True, limit=limit, db_path=db_path
        )

    def get_test_coverage_summary(
        self,
        qualified_name: str,
        db_path: Optional[str] = None,
    ) -> Any:
        """查询符号的测试覆盖摘要，优先走 enterprise snapshot RPC。

        M2.5（T-1786584287058-7f712ff4）：从 query.tests RPC 的 test cases
        结果聚合出 has_tests/test_count/high_confidence_count/tests[:10]，
        与 db 层 TestRelationMixin.get_test_coverage_summary 语义一致；
        daemon 不可用时 fail-closed，local 模式返回 None。
        """
        tests = self.query_tests(
            qualified_name, reverse=False, history=False, limit=50, db_path=db_path
        )
        if tests is None:
            return None
        high_count = sum(1 for t in tests if t.get("confidence") == "high")
        return {
            "has_tests": len(tests) > 0,
            "test_count": len(tests),
            "high_confidence_count": high_count,
            "tests": tests[:10],  # 最多返回 10 条
        }

    def get_stats(self, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取统计信息。"""
        remote = self._remote_query("query.stats", {}, db_path)
        if remote is not _NO_REMOTE:
            return remote
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_stats(self._workspace_instance_id)
        self._sql_fallbacks += 1
        return self._sql_fallback_get_stats()

    def get_topological_order(
        self, limit: int = 50, db_path: Optional[str] = None,
    ) -> List[str]:
        """获取拓扑排序。"""
        # J8 协议闭合：优先走 RPC（query.topological_order），Rust daemon 端已实现
        remote = self._remote_query("query.topological_order", {"limit": limit}, db_path)
        if remote is not _NO_REMOTE:
            return remote if isinstance(remote, list) else []
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            result = self._svc.query_topological_order(self._workspace_instance_id)
            return result[:limit] if limit > 0 else result
        self._sql_fallbacks += 1
        return self._sql_fallback_get_topological_order(limit)

    def get_call_chain_down(
        self,
        qualified_name: str,
        max_depth: int = 10,
        db_path: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """向下调用链（BFS）。"""
        # J8 协议闭合：优先走 RPC（query.call_chain_down），Rust daemon 端已实现
        remote = self._remote_query("query.call_chain_down", {
            "qualified_name": qualified_name,
            "max_depth": max_depth,
        }, db_path)
        if remote is not _NO_REMOTE:
            return remote if isinstance(remote, list) else []
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            budget = default_budget()
            budget.max_depth = min(max_depth, budget.max_depth)
            return self._svc.query_call_chain_down(
                self._workspace_instance_id, qualified_name, max_depth, budget
            )
        self._sql_fallbacks += 1
        return self._sql_fallback_get_call_chain_down(qualified_name, max_depth)

    def detect_cycles(
        self, max_depth: int = 10, db_path: Optional[str] = None,
    ) -> List[List[str]]:
        """检测循环依赖。"""
        # J8 协议闭合：优先走 RPC（query.detect_cycles），Rust daemon 端已实现
        remote = self._remote_query("query.detect_cycles", {"max_depth": max_depth}, db_path)
        if remote is not _NO_REMOTE:
            return remote if isinstance(remote, list) else []
        if db_path and self._ensure_snapshot(db_path):
            self._daemon_hits += 1
            return self._svc.query_detect_cycles(self._workspace_instance_id)
        self._sql_fallbacks += 1
        return self._sql_fallback_detect_cycles(max_depth)

    def diff_symbol(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        qualified_name: str,
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中同一符号的差异。"""
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        return cache.diff_symbol(left_workspace_id, right_workspace_id, qualified_name)

    def diff_signature(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        qualified_name: str,
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中同一符号的签名差异。"""
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        return cache.diff_signature(left_workspace_id, right_workspace_id, qualified_name)

    def diff_callers(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        qualified_name: str,
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中同一符号的 caller 边集合（基于 resolved edge delta）。"""
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        return cache.diff_callers(left_workspace_id, right_workspace_id, qualified_name)

    def diff_callees(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        qualified_name: str,
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中同一符号的 callee 边集合（基于 resolved edge delta）。"""
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        return cache.diff_callees(left_workspace_id, right_workspace_id, qualified_name)

    def compare_snapshots(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        scope_type: str = "repo",
        scope_value: str = "",
    ) -> Optional[Dict[str, Any]]:
        """对比两个 workspace 中指定 scope 内的所有符号差异（同步查询）。

        同步路径：小 scope（file/module）直接返回结果。
        仓库级 scope 应先调用 count_symbols_in_scope 检查大小，
        超阈值时改用 start_snapshot_diff 转后台 job。

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            scope_type: "file" / "module" / "repo"
            scope_value: 文件路径或模块路径（repo 时忽略）

        Returns:
            {"changes": [...], "scope_type": str, "scope_value": str, "count": int}
            Rust 不可用时返回 None
        """
        if not self._svc.rust_available:
            return None
        cache = self._svc._cache
        if cache is None:
            return None
        changes = cache.compare_snapshots(
            left_workspace_id, right_workspace_id, scope_type, scope_value
        )
        return {
            "changes": changes,
            "scope_type": scope_type,
            "scope_value": scope_value,
            "count": len(changes),
        }

    def count_symbols_in_scope(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        scope_type: str = "repo",
        scope_value: str = "",
    ) -> int:
        """统计两个 workspace 中匹配 scope 的符号数量（并集）。

        用于判断 compare_snapshots 是否应走同步路径还是转后台 job。

        Returns:
            符号数量（并集），Rust 不可用时返回 0
        """
        if not self._svc.rust_available:
            return 0
        cache = self._svc._cache
        if cache is None:
            return 0
        return cache.count_symbols_in_scope(
            left_workspace_id, right_workspace_id, scope_type, scope_value
        )

    def start_snapshot_diff(
        self,
        left_workspace_id: str,
        right_workspace_id: str,
        scope_type: str = "repo",
        scope_value: str = "",
    ) -> Optional[str]:
        """启动仓库级 snapshot diff 后台 job。

        设计参考：enterprise-daemon-shared-snapshot-plan.md §12.4 start_snapshot_diff

        Args:
            left_workspace_id: 左 workspace ID
            right_workspace_id: 右 workspace ID
            scope_type: "file" / "module" / "repo"
            scope_value: 文件路径或模块路径（repo 时忽略）

        Returns:
            job_id 字符串，Rust 不可用时返回 None
        """
        if not self._svc.rust_available:
            return None
        # 延迟导入避免循环依赖
        from callwarden.config import get_project_db_path
        from callwarden.server.job_executor_singleton import get_job_executor
        db_path = get_project_db_path(self._project_root or ".")
        executor = get_job_executor(db_path)
        params = {
            "left_workspace_id": left_workspace_id,
            "right_workspace_id": right_workspace_id,
            "scope_type": scope_type,
            "scope_value": scope_value,
        }
        job = executor.submit("snapshot_diff", params)
        return job.job_id

    # ------------------------------------------------------------------
    # SQL 回退（SRV-006：authority 已下沉 Rust daemon，薄 RPC 客户端）
    # 原实现经 _get_db() 直调 CodeGraphDB 业务 SQL；下沉后改走 daemon RPC
    # `mcp.daemon_client.sql_fallback_*`（Rust 权威实现，语义逐一对齐），
    # fail-closed：daemon 不可用直接抛 DaemonUnavailableError，禁止本地业务回退。
    # ------------------------------------------------------------------

    def _sql_fallback_get_callers(self, callee_name, qualified_name=None):
        result = self._rpc.call("mcp.daemon_client.sql_fallback_get_callers", {
            "callee_name": callee_name,
            "qualified_name": qualified_name,
        })
        return result.get("callers", []) if isinstance(result, dict) else []

    def _sql_fallback_get_callees(self, caller_name, qualified_name=None):
        result = self._rpc.call("mcp.daemon_client.sql_fallback_get_callees", {
            "caller_name": caller_name,
            "qualified_name": qualified_name,
        })
        return result.get("callees", []) if isinstance(result, dict) else []

    def _sql_fallback_search_symbols(self, query, kind=None, limit=20):
        result = self._rpc.call("mcp.daemon_client.sql_fallback_search_symbols", {
            "query": query,
            "kind": kind,
            "limit": limit,
        })
        return result.get("symbols", []) if isinstance(result, dict) else []

    def _sql_fallback_get_symbol(self, qualified_name):
        result = self._rpc.call("mcp.daemon_client.sql_fallback_get_symbol", {
            "qualified_name": qualified_name,
        })
        # daemon 对不存在符号返回 {"symbol": null}，保持原 Optional 语义
        return result.get("symbol") if isinstance(result, dict) else None

    def _sql_fallback_get_stats(self):
        result = self._rpc.call("mcp.daemon_client.sql_fallback_get_stats", {})
        return result.get("stats") if isinstance(result, dict) else None

    def _sql_fallback_get_topological_order(self, limit=50):
        result = self._rpc.call("mcp.daemon_client.sql_fallback_get_topological_order", {
            "limit": limit,
        })
        return result.get("order", []) if isinstance(result, dict) else []

    def _sql_fallback_get_call_chain_down(self, qualified_name, max_depth=10):
        result = self._rpc.call("mcp.daemon_client.sql_fallback_get_call_chain_down", {
            "qualified_name": qualified_name,
            "max_depth": max_depth,
        })
        # Rust 权威实现返回 {"start","edges","levels",...}；保留 dict→list 归一化
        if isinstance(result, dict):
            return result.get("chain", result.get("edges", []))
        return result if isinstance(result, list) else []

    def _sql_fallback_detect_cycles(self, max_depth=10):
        result = self._rpc.call("mcp.daemon_client.sql_fallback_detect_cycles", {
            "max_depth": max_depth,
        })
        return result.get("cycles", []) if isinstance(result, dict) else []

    # ------------------------------------------------------------------
    # 路由统计
    # ------------------------------------------------------------------

    def get_routing_stats(self) -> Dict[str, Any]:
        """获取路由统计信息。"""
        total = self._daemon_hits + self._sql_fallbacks
        daemon_ratio = (self._daemon_hits / total * 100) if total > 0 else 0
        return {
            "daemon_hits": self._daemon_hits,
            "sql_fallbacks": self._sql_fallbacks,
            "total_queries": total,
            "daemon_ratio_percent": round(daemon_ratio, 2),
            "workspace_instance_id": self._workspace_instance_id,
            "daemon_ready": self.is_daemon_ready(),
        }


# ----------------------------------------------------------------------
# 便捷函数
# ----------------------------------------------------------------------

def get_daemon_client():
    """获取 daemon client 单例（HTTP MVP transport 下返回 HTTP thin client）。

    H2 factory 接线：`CW_DAEMON_TRANSPORT=http` 时返回 `HttpDaemonRpcClient`
    （不含业务 SQL、不回退 SQLite/Named Pipe/UDS）；否则返回 legacy
    `DaemonClient`（Named Pipe/UDS + SQL 回退）。
    """
    if is_http_transport_enabled():
        return HttpDaemonRpcClient.get_instance()
    return DaemonClient.get_instance()


# ======================================================================
# H2：HTTP/JSON-RPC thin client（dev_loopback_unauthenticated）
# ======================================================================
# 仅用于 HTTP MVP transport profile。客户端：
# - 仅经 authority-scoped manifest 或显式 loopback endpoint 发现 daemon；
# - 联网前校验 manifest hash / protocol / security_profile / authority / PID；
# - 业务错误映射为已有结构化 DaemonRemoteError（保留 error.data.code）；
# - 连接/发现/authority 失败一律 fail-closed，绝不打开 SQLite，也不回退
#   Named Pipe / UDS / 本地 Python SQL fallback。
# 详见 docs/design/http-daemon-mvp-compatibility-contract.md §4。


def _build_multipart_body(parts, boundary: str) -> bytes:
    """Q10（2026-09-17）：按 RFC 7578 编码 multipart/form-data body。

    stdlib 无自带 multipart 编码器，此处实现最小子集（daemon 侧由 axum
    `multipart` feature 解析，双方字段语义一致）：

    - 每段：``--{boundary}\\r\\n`` + Content-Disposition + Content-Type
      + 空行 + 数据 + ``\\r\\n``
    - 结尾：``--{boundary}--\\r\\n``
    - 二进制 part 原样嵌入（不做 base64 / 转义），这是 Q10 消除编码税的关键

    Args:
        parts: ``[(name, bytes, content_type), ...]``
        boundary: 分界符（caller 传入随机值，避免与 payload 冲突）

    Returns:
        完整 body 字节流。
    """
    chunks = []
    for name, data, ctype in parts:
        chunks.append(f"--{boundary}\r\n".encode("ascii"))
        chunks.append(
            f'Content-Disposition: form-data; name="{name}"\r\n'.encode("ascii")
        )
        chunks.append(f"Content-Type: {ctype}\r\n".encode("ascii"))
        chunks.append(b"\r\n")
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks)


class HttpDaemonRpcClient:
    """Python 3.14 thin HTTP/JSON-RPC client（H0-frozen protocol）。

    该 client 不含任何业务 SQL，也不在任何失败路径打开 SQLite/CodeGraphDB。
    所有读/写请求都经 HTTP POST /v1/rpc 透传到 cw-daemon（Rust）。
    """

    # H2：标识该 client 走 HTTP MVP transport（供 production factory 选择）。
    is_http_client: bool = True

    # Q10（2026-09-17）：显式声明本 client 支持 multipart raw-gz-body 传输
    # （POST /v1/rpc/multipart）。agent_protocol 按本标志做分档选择；
    # mock/UDS client 无此标志 → 自动降级 gz_b64 JSON 内联（零回归）。
    supports_multipart: bool = True

    _instance: Optional["HttpDaemonRpcClient"] = None

    @classmethod
    def get_instance(cls) -> "HttpDaemonRpcClient":
        """获取 HTTP thin client 单例（HTTP MVP transport 下的 production factory 入口）。"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """重置单例（测试用）。"""
        cls._instance = None

    def configure_workspace(self, project_root: str) -> None:
        """配置当前 workspace（与 legacy DaemonClient 签名对齐）。

        Args:
            project_root: 项目根目录路径（HTTP 模式注册 workspace 时作为
                client_view_root 使用，避免依赖进程 cwd）
        """
        self._project_root = project_root
        self._remote_workspace_id = None
        self._remote_snapshot_id = None
        self._remote_snapshot_ready = False

    def _ensure_remote_snapshot(self, db_path: Optional[str]) -> Optional[str]:
        """确保 workspace 已注册且 snapshot 已发布，返回权威 workspace_instance_id。

        M2.1（T-1786519172968-f13db464）：HTTP 模式查询前需要 workspace_instance_id
        （Rust handler 侧 `handle_query_file` 强制 require），本方法与 legacy
        `_ensure_remote_snapshot` 对齐：先 workspace.register（以返回值为权威），
        再按需 snapshot.publish。与 legacy 不同的是 HTTP thin client 不做
        SQLite checkpoint（那是 Python 本地 DB 的职责），直接透传 db_path。
        """
        if self._remote_workspace_id is None:
            # MCP/后台宿主的 cwd 可能是 runtime 日志目录，不是用户项目。
            # 未显式 configure_workspace 时使用包解析出的项目根，禁止以
            # cwd 作为 workspace authority；显式配置仍优先用于多项目客户端。
            from callwarden.config import PROJECT_ROOT
            root = self._project_root or PROJECT_ROOT
            if self.is_remote_profile():
                # S2 §16.1/§16.3（ADR-002）：remote profile 下 daemon bind_scope=remote，
                # 绝对路径 client_view_root 会被 §16.1 路径拒绝表挡掉；snapshot.publish
                # 的 db_path 同样被拒（远端只读面快照入口唯一为 snapshot.upload）。
                # 以 "." 注册：daemon canonicalize 到其 cwd（属主=daemon owner，
                # mTLS 流 peer.uid=current_daemon_owner_key()），git provenance
                # 取客户端本机工作树（非路径字段，不受路径扫描影响）。
                register_params = {"client_view_root": "."}
                register_params.update(_workspace_snapshot_metadata(root))
            else:
                register_params = {"client_view_root": root}
                if db_path:
                    register_params.update(_workspace_snapshot_metadata(root))
            workspace = self.call("workspace.register", register_params)
            if not isinstance(workspace, dict) or "workspace_instance_id" not in workspace:
                raise DaemonUnavailableError(
                    f"workspace.register 响应缺少 workspace_instance_id: {workspace!r}"
                )
            self._remote_workspace_id = workspace["workspace_instance_id"]
            self._remote_snapshot_id = workspace.get("snapshot_id") or None
        if db_path and not self._remote_snapshot_ready and not self.is_remote_profile():
            publish_result = self.call("snapshot.publish", {
                "workspace_instance_id": self._remote_workspace_id,
                "build_context_hash": "",
                "db_path": os.path.abspath(db_path),
                "snapshot_id": self._remote_snapshot_id or "",
            })
            published_id = publish_result.get("snapshot_id") if isinstance(publish_result, dict) else None
            if self._remote_snapshot_id and published_id != self._remote_snapshot_id:
                raise DaemonUnavailableError(
                    "snapshot.publish 未返回与 workspace.register 一致的 snapshot_id",
                    code="E_SNAPSHOT_ID_MISMATCH",
                )
            self._remote_snapshot_id = published_id or self._remote_snapshot_id
            self._remote_snapshot_ready = True
        return self._remote_workspace_id

    def __init__(
        self,
        endpoint: Optional[str] = None,
        timeout: float = HTTP_DEFAULT_TIMEOUT,
        manifest_path: Optional[str] = None,
        authority_id: Optional[str] = None,
        verify_health: bool = True,
        validate_manifest: bool = True,
    ):
        """初始化 HTTP client。

        Args:
            endpoint: 显式 loopback http endpoint（覆盖发现优先级）；仍须 loopback 校验。
            timeout: 同步请求超时（秒），默认 30s。
            manifest_path: 显式 authority-scoped manifest 路径。
            authority_id: 期望 authority id（用于 manifest 校验）；默认本机用户 authority。
            verify_health: 发现后是否调用 /health 交叉核对 manifest_id/PID（默认 True）。
            validate_manifest: 是否对 manifest 做完整校验（默认 True）。
        """
        self._explicit_endpoint = endpoint
        self._timeout = timeout
        self._manifest_path = manifest_path
        self._authority_id = authority_id or get_http_authority_id()
        self._verify_health = verify_health
        self._validate_manifest = validate_manifest
        self._manifest: Optional[Dict[str, Any]] = None
        self._resolved_endpoint: Optional[str] = None
        # M2.1（T-1786519172968-f13db464）：HTTP 模式 workspace 注册与 snapshot
        # 发布状态（与 legacy DaemonClient 对齐）。workspace_instance_id 以
        # workspace.register 返回值为权威（daemon canonicalize 路径与本地
        # derive_workspace_instance_id 可能不一致）。
        self._remote_workspace_id: Optional[str] = None
        self._remote_snapshot_ready = False
        self._project_root: Optional[str] = None
        # W1-2（T-1786808777379-15702f0c）：root_path（_norm_root 规范化）→
        # 权威 workspace_instance_id 内存映射。workspaces 表无对应列（禁改
        # schema），进程内缓存避免重复 register；映射可随时经幂等 register
        # 确定性重建（instance_id = sha256(owner_uid|host_real_root|...)[:16]）。
        self._workspace_instance_by_root: Dict[str, str] = {}
        # 测试/诊断钩子：最近一次出向信封与 request id
        self.last_request_body: Optional[Dict[str, Any]] = None
        self.last_request_id: Optional[str] = None
        # ADR-002：懒构建的 mTLS 上下文（CA pin + 客户端证书）。
        self._ssl_ctx: Optional[ssl.SSLContext] = None

    # ------------------------------------------------------------------
    # 发现与校验（fail-closed 门禁）
    # ------------------------------------------------------------------

    def discover(self) -> str:
        """解析并校验 endpoint（含 manifest），返回可用的 base endpoint。

        幂等：已解析则直接返回。
        """
        if self._resolved_endpoint is not None:
            return self._resolved_endpoint
        endpoint, manifest = resolve_http_endpoint_and_manifest(
            explicit_endpoint=self._explicit_endpoint,
            manifest_path=self._manifest_path,
            authority_id=self._authority_id,
            validate=self._validate_manifest,
        )
        self._manifest = manifest
        if self._verify_health and manifest is not None:
            # 交叉核对 /health 的 manifest_id/PID；不匹配 fail-closed
            self.verify_health(endpoint, manifest)
        self._resolved_endpoint = endpoint
        return endpoint

    def verify_health(self, endpoint: Optional[str] = None,
                      manifest: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用 GET /health 核对 manifest_id / PID；不匹配抛结构化错误。

        Returns:
            /health 响应 dict。
        Raises:
            DaemonUnavailableError: /health 不可达。
            DaemonRemoteError(E_HTTP_MANIFEST_STALE): manifest_id/PID 不一致。
        """
        from callwarden.server.daemon_protocol import DaemonRemoteError

        endpoint = endpoint or self._resolved_endpoint or self.discover()
        manifest = manifest or self._manifest
        health = self._http_get(endpoint.rstrip("/") + "/health")
        if not isinstance(health, dict):
            raise DaemonUnavailableError("HTTP /health 响应非 JSON 对象")
        if manifest is not None:
            m_id = manifest.get("manifest_id")
            h_id = health.get("manifest_id")
            if m_id is not None and h_id is not None and m_id != h_id:
                raise DaemonRemoteError(
                    E_HTTP_MANIFEST_STALE,
                    f"/health manifest_id {h_id!r} 与 manifest {m_id!r} 不一致",
                )
            if (manifest.get("pid") is not None
                    and health.get("pid") is not None
                    and int(manifest["pid"]) != int(health["pid"])):
                raise DaemonRemoteError(
                    E_HTTP_MANIFEST_STALE,
                    "/health PID 与 manifest 不一致",
                )
        return health

    # ------------------------------------------------------------------
    # 底层 HTTP
    # ------------------------------------------------------------------

    def _ssl_context(self) -> ssl.SSLContext:
        """构建/复用 mTLS 上下文（ADR-002 §7）。

        - CA pin：优先用 manifest `tls.ca_cert_pem`（权威且已通过 manifest_hash
          + 指纹自洽校验）；回退到本地 CA 文件。
        - 客户端证书：`CW_CLIENT_CERT/CW_CLIENT_KEY/CW_CA_CERT` → 身份目录 →
          manifest 同源；缺失 fail-closed 抛 `E_MTLS_CLIENT_CERT_MISSING`。
        - `check_hostname=True`（默认）+ `minimum_version=TLSv1_2`。
        """
        if self._ssl_ctx is not None:
            return self._ssl_ctx

        cert, key, ca_file = resolve_client_cert_paths()
        if not cert or not key:
            raise DaemonUnavailableError(
                "mTLS 客户端证书缺失：请先 `cw pki init`/`cw pki enroll`，或用 "
                "CW_CLIENT_CERT/CW_CLIENT_KEY 指定（ADR-002 单轨，禁止回退明文）",
                code=E_MTLS_CLIENT_CERT_MISSING,
            )

        manifest = self._manifest or {}
        tls_block = manifest.get("tls") if isinstance(manifest, dict) else None
        ca_pem = tls_block.get("ca_cert_pem") if isinstance(tls_block, dict) else ""
        if not ca_pem and not ca_file:
            raise DaemonUnavailableError(
                "缺少 daemon CA 证书（manifest tls.ca_cert_pem 与本地 CA 均不可得）",
                code=E_MTLS_UNKNOWN_CA,
            )

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if ca_pem:
            ctx.load_verify_locations(cadata=ca_pem)
        else:
            ctx.load_verify_locations(cafile=ca_file)
        ctx.load_cert_chain(certfile=cert, keyfile=key)
        self._ssl_ctx = ctx
        return ctx

    def _http_get(self, url: str) -> Any:
        """GET 请求；返回解析后的 JSON。连接失败抛 DaemonUnavailableError。"""
        # nosec B310：url 为本地 daemon endpoint（manifest 注册的
        # unix-socket / 127.0.0.1:port），非用户输入；B310 黑名单对本地 RPC
        # 客户端为误报，请求经 Request 封装且带超时。
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(  # nosec B310
                req, timeout=self._timeout, context=self._ssl_context()
            ) as resp:
                raw = resp.read()
            return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as e:
            try:
                raw = e.read()
            except Exception:
                raw = b""
            if raw:
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    pass
            raise DaemonUnavailableError(f"HTTP GET {url} 失败 status={e.code}")
        except ssl.SSLError as e:
            raise DaemonUnavailableError(
                f"TLS 握手失败（GET {url}）: {e}", code=E_TLS_HANDSHAKE_FAILED
            )
        except OSError as e:
            raise self._connection_error(e, url)
        except (ValueError, UnicodeDecodeError) as exc:
            raise DaemonUnavailableError(f"HTTP GET {url} 响应非 JSON: {exc}")

    def _timeout_for(self, method: str) -> float:
        """按 RPC method 选择同步超时（秒）。

        慢方法（workspace.build_graph 全量索引、import.* 导入、snapshot.publish
        等）放宽到 HTTP_SLOW_METHOD_TIMEOUT；其余方法保持 HTTP_DEFAULT_TIMEOUT。
        caller 显式传入短于默认值的超时（诊断/测试场景，如 5s 快速失败）时尊重之；
        传入默认值或更长超时则按方法分级（慢方法可能要 ~130s，见 config.py 注释）。
        """
        return _rpc_timeout_for(method, self._timeout)

    # ------------------------------------------------------------------
    # 公开 RPC 调用
    # ------------------------------------------------------------------

    def call(self, method: str, params: Optional[Dict[str, Any]] = None,
             request_id: Optional[str] = None,
             max_body_bytes: Optional[int] = None) -> Any:
        """发起一次 JSON-RPC 调用，返回 result。

        同一个 request_id 在重连/重试中原样复用，供 daemon 侧 mutation dedup
        （frozen contract §4.2）。

        Args:
            method: capability registry 中的 RPC method。
            params: JSON object 参数（缺省为 {}）。
            request_id: 显式 request id（1–128 字节非空 UTF-8）；None 时按
                params["request_id"]（CLI 路由注入的 uuid）生成，再缺省 uuid4
                全局唯一（H6-FIX：杜绝 CLI 短生命周期进程复用 "1" 触发
                E_REQUEST_ID_REUSE_MISMATCH）。
            max_body_bytes: 单次请求体上限（默认 HTTP_MAX_BODY_BYTES 8MiB）；
                S2 §16.2 snapshot.upload 上传面按 64MiB 口径传入。

        Returns:
            response 的 result 字段。

        Raises:
            DaemonRemoteError: 结构化业务错误（保留 error.data.code）。
            DaemonUnavailableError: 连接/超时/发现/authority 失败（fail-closed）。
        """
        endpoint = self.discover()
        # H6-FIX：request_id 生成缺陷修复（E_REQUEST_ID_REUSE_MISMATCH）。
        # 旧实现默认 id = str(next(self._ids))，CLI 短生命周期进程每次从 "1"
        # 开始，与 daemon 持久化 dedup 表 http_dedup（保留 24h）中同名 method
        # 的旧记录冲突。修复策略（幂等重试语义不变）：
        #   1) 显式 request_id 参数优先（重试复用同一 id 命中 Replay）；
        #   2) 其次 params["request_id"]（CLI 路由注入的 uuid）用作 envelope id，
        #      统一幂等语义——重试复用同一 params 即命中 Replay；
        #   3) 缺省 uuid4 全局唯一，杜绝跨进程复用。
        rid = request_id
        if rid is None and isinstance(params, dict):
            rid = params.get("request_id") or None
        if rid is None:
            rid = str(uuid.uuid4())
        self.last_request_id = rid
        envelope = {
            "jsonrpc": "2.0",
            "id": rid,
            "protocol_version": HTTP_PROTOCOL_VERSION,
            "method": method,
            "params": params if params is not None else {},
        }
        # 暴露原始出向信封（便于测试断言 frozen envelope 结构）
        self.last_request_body = envelope

        url = endpoint.rstrip("/") + "/v1/rpc"
        body = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        body_limit = HTTP_MAX_BODY_BYTES if max_body_bytes is None else max_body_bytes
        if len(body) > body_limit:
            raise DaemonRemoteError(
                E_REQUEST_TOO_LARGE,
                f"请求体 {len(body)} 字节超过 {body_limit} 字节上限",
            )
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json"},
            )
            # nosec B310：url = 本地 daemon endpoint + "/v1/rpc"（见上行拼接），
            # 非用户输入；B310 对本地 RPC 客户端为误报。
            with urllib.request.urlopen(  # nosec B310
                req, timeout=self._timeout_for(method), context=self._ssl_context()
            ) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                raw = e.read()
            except Exception:
                raw = b""
            return self._handle_response(status, raw, rid, url)
        except ssl.SSLError as e:
            raise DaemonUnavailableError(
                f"TLS 握手失败（POST {url}）: {e}", code=E_TLS_HANDSHAKE_FAILED
            )
        except OSError as e:
            raise self._connection_error(e, url)
        return self._handle_response(status, raw, rid, url)

    def call_multipart(
        self,
        method: str,
        params: Optional[Dict[str, Any]] = None,
        payload: Optional[bytes] = None,
        gz: bool = False,
        request_id: Optional[str] = None,
    ) -> Any:
        """Q10（2026-09-17）：multipart raw-gz-body 传输。

        与 `call` 完全同构的 JSON-RPC 语义（envelope / request_id 去重 /
        Protected_Mutation 串行化 / 错误处理全部复用 `_handle_response`），
        唯一差异是数据面：二进制 payload 不进 JSON 信封（消除 base64 税），
        而是作为 multipart part 原样传输。

        成帧（与 daemon `rpc_multipart_handler` 一致）：
        - part ``params``：完整 JSON-RPC envelope（application/json）
        - part ``payload`` / ``payload_gz``：canonical_bytes 裸 / gzip
          （application/octet-stream，二者互斥；由 ``gz`` 选择）

        Args:
            method: RPC method（仅 workspace.file.refresh / snapshot.publish
                会消费 payload；其余方法忽略 payload）。
            params: JSON object 参数（缺省 {}）。大文件场景 caller 应写入
                ``canonical_len``，daemon 侧据此限制 gz 解压容量（防 bomb）。
            payload: 带外二进制字节流；None 时不携带 payload part（退化为
                纯控制面调用，但走 multipart 路由）。
            gz: True → 以 `payload_gz` 名发送 gzip 流；False → 以 `payload`
                名发送裸字节。
            request_id: 显式 request id（重试复用同一 id 以命中 dedup Replay）。

        Returns:
            response 的 result 字段。

        Raises:
            DaemonRemoteError: 结构化业务错误（含 E_PAYLOAD_CONFLICT /
                E_PAYLOAD_TOO_LARGE / E_GZ_DECOMPRESS_FAILED 等 Q10 新码）。
            MultipartUnsupportedError: daemon 返回 404（旧版本无本路由），
                caller 应降级到 `call` 的 JSON 内联路径。
            DaemonUnavailableError: 连接/超时/发现失败（fail-closed）。
        """
        endpoint = self.discover()
        rid = request_id
        if rid is None and isinstance(params, dict):
            rid = params.get("request_id") or None
        if rid is None:
            rid = str(uuid.uuid4())
        self.last_request_id = rid
        envelope = {
            "jsonrpc": "2.0",
            "id": rid,
            "protocol_version": HTTP_PROTOCOL_VERSION,
            "method": method,
            "params": params if params is not None else {},
        }
        self.last_request_body = envelope

        params_bytes = json.dumps(envelope, ensure_ascii=False).encode("utf-8")
        parts = [("params", params_bytes, "application/json")]
        if payload is not None:
            part_name = "payload_gz" if gz else "payload"
            parts.append((part_name, payload, "application/octet-stream"))

        boundary = "cw-q10-" + uuid.uuid4().hex
        body = _build_multipart_body(parts, boundary)
        if len(body) > HTTP_MAX_BODY_BYTES:
            raise DaemonRemoteError(
                E_REQUEST_TOO_LARGE,
                f"multipart 请求体 {len(body)} 字节超过上限",
            )

        url = endpoint.rstrip("/") + "/v1/rpc/multipart"
        content_type = f'multipart/form-data; boundary="{boundary}"'
        try:
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": content_type},
            )
            # nosec B310：url = 本地 daemon endpoint + "/v1/rpc/multipart"（见
            # 上行拼接），非用户输入；B310 对本地 RPC 客户端为误报。
            with urllib.request.urlopen(  # nosec B310
                req, timeout=self._timeout_for(method), context=self._ssl_context()
            ) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
            if status == 404:
                # 旧 daemon 无 multipart 路由 → 标记降级
                raise MultipartUnsupportedError(
                    f"daemon 未部署 /v1/rpc/multipart（status=404, url={url}）"
                )
            try:
                raw = e.read()
            except Exception:
                raw = b""
            return self._handle_response(status, raw, rid, url)
        except ssl.SSLError as e:
            raise DaemonUnavailableError(
                f"TLS 握手失败（POST {url}）: {e}", code=E_TLS_HANDSHAKE_FAILED
            )
        except OSError as e:
            raise self._connection_error(e, url)
        return self._handle_response(status, raw, rid, url)

    def _handle_response(self, status: int, raw: bytes, request_id: str,
                        url: str) -> Any:
        """解析 HTTP 响应，映射为结构化错误或 result。"""
        from callwarden.server.daemon_protocol import DaemonRemoteError

        payload: Any = None
        if raw:
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None

        if not isinstance(payload, dict):
            # 非 JSON 业务信封：按 transport 级 HTTP status 映射
            self._map_transport_status(status, url)
            # _map_transport_status 总会抛；到达这里说明未识别 status
            raise DaemonUnavailableError(
                f"daemon 返回非 JSON 响应 (status={status}, url={url})"
            )

        # request id 必须匹配（重放/并发安全）
        if payload.get("id") != request_id:
            raise DaemonUnavailableError(
                f"daemon 响应 request id 不匹配: {payload.get('id')!r} != {request_id!r}"
            )

        # 业务错误信封（HTTP 200 仍保留 error；frozen contract §4.3）
        if payload.get("error") is not None:
            err = payload["error"]
            if isinstance(err, dict):
                code = err.get("data", {}).get("code") or err.get("code")
                message = err.get("message", "unknown daemon error")
                # 保留标准整数 error.code 与 data.code；向上抛结构化错误
                raise DaemonRemoteError(str(code), str(message))
            raise DaemonRemoteError("daemon_error", str(err))

        if "result" in payload:
            return payload["result"]
        raise DaemonUnavailableError(
            "daemon 响应缺少 result/error 字段"
        )

    def _map_transport_status(self, status: int, url: str) -> None:
        """将非 JSON / 非 200 的 transport 级 HTTP status 映射为结构化错误。"""
        from callwarden.server.daemon_protocol import DaemonRemoteError

        if status == 413:
            raise DaemonRemoteError(
                E_REQUEST_TOO_LARGE, f"请求体超过 8 MiB (url={url})"
            )
        if status == 415:
            raise DaemonRemoteError(
                "E_UNSUPPORTED_MEDIA_TYPE",
                f"Content-Type 非 application/json (url={url})",
            )
        if status == 426:
            raise DaemonRemoteError(
                E_PROTOCOL_VERSION_UNSUPPORTED,
                f"协议版本无交集 (url={url})",
            )
        if status == 429:
            raise DaemonRemoteError(
                "E_CAPACITY_EXCEEDED", f"队列/任务容量已满 (url={url})"
            )
        if status == 503:
            raise DaemonUnavailableError(
                f"{E_HTTP_DAEMON_UNAVAILABLE}: daemon 暂不可用 (url={url})"
            )
        if status == 504:
            raise DaemonUnavailableError(
                f"{E_HTTP_REQUEST_TIMEOUT}: 传输 deadline 超时 (url={url})"
            )
        raise DaemonUnavailableError(
            f"daemon 返回未预期 HTTP status={status} (url={url})"
        )

    def _connection_error(self, exc: OSError, url: str) -> DaemonUnavailableError:
        """将 URLError（连接失败/超时）转换为 fail-closed 的 DaemonUnavailableError。"""
        import socket as _socket

        reason = getattr(exc, "reason", exc)
        if isinstance(reason, (_socket.timeout, TimeoutError)) or "timed out" in str(reason).lower():
            return DaemonUnavailableError(
                f"{E_HTTP_REQUEST_TIMEOUT}: 连接/读取超时 (url={url}): {reason}"
            )
        return DaemonUnavailableError(
            f"{E_HTTP_DAEMON_UNAVAILABLE}: 无法连接 daemon (url={url}): {reason}"
        )

    # ------------------------------------------------------------------
    # 便捷只读方法
    # ------------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        """GET /health。"""
        return self._http_get(self.discover().rstrip("/") + "/health")

    def capabilities(self) -> Dict[str, Any]:
        """GET /capabilities。"""
        return self._http_get(self.discover().rstrip("/") + "/capabilities")

    # ------------------------------------------------------------------
    # 单轨传输能力（HTTPS+mTLS）：原 UDS/pipe 客户端的公开契约在此保持
    # ------------------------------------------------------------------

    def publish_snapshot(self, workspace_instance_id: str, db_path: str,
                         build_context_hash: str = "", snapshot_id: str = "") -> Any:
        """发布快照给 daemon（纯 HTTP，无本地 checkpoint / 无 FD 传递）。

        先经 `mcp.daemon_client.publish_snapshot` 取得权威归一化 db_path
        （daemon 侧执行 busy_timeout 等待 + PASSIVE checkpoint），再调用
        `snapshot.publish` 发布。fail-closed：daemon 不可用抛
        DaemonUnavailableError（不回退本地 sqlite checkpoint）。
        """
        payload = self.call("mcp.daemon_client.publish_snapshot", {
            "workspace_instance_id": workspace_instance_id,
            "db_path": os.path.abspath(db_path),
            "build_context_hash": build_context_hash,
            "snapshot_id": snapshot_id,
        })
        if not isinstance(payload, dict):
            raise RuntimeError(f"publish_snapshot: daemon 返回非对象结果 {payload!r}")
        effective_db_path = payload.get("db_path") or os.path.abspath(db_path)
        return self.call("snapshot.publish", {
            "workspace_instance_id": workspace_instance_id,
            "build_context_hash": build_context_hash,
            "db_path": effective_db_path,
            "snapshot_id": snapshot_id,
        })

    def bind_scope(self) -> str:
        """manifest v2 的 bind_scope（daemon 写入："loopback" / "remote"）。

        S2（ADR-002 §16.1）客户端据此决定只读工具执行位置。manifest 缺失/
        非 dict 时按 loopback（回滚兼容）。**不触发网络往返**：若从未
        discover 过，只读 manifest 本地文件（resolve … validate=False），
        避免远端只读面在 daemon 暂不可达时误判 loopback 回退 RPC（S2-09）。
        仅缓存 `_manifest`，不设 `_resolved_endpoint`——后续真正 discover
        仍做完整 health 校验。
        """
        manifest = getattr(self, "_manifest", None)
        if not isinstance(manifest, dict):
            try:
                _, manifest = resolve_http_endpoint_and_manifest(
                    explicit_endpoint=getattr(self, "_explicit_endpoint", None),
                    manifest_path=getattr(self, "_manifest_path", None),
                    authority_id=getattr(self, "_authority_id", None),
                    validate=False,
                )
            except Exception:
                return "loopback"
            if isinstance(manifest, dict):
                self._manifest = manifest
        if not isinstance(manifest, dict):
            return "loopback"
        scope = manifest.get("bind_scope")
        return scope if isinstance(scope, str) and scope else "loopback"

    def is_remote_profile(self) -> bool:
        """bind_scope == "remote"（远端部署面：只读工具客户端本机执行）。"""
        return self.bind_scope() == "remote"

    def upload_snapshot(self, workspace_instance_id: str, raw_bytes: bytes,
                        build_context_hash: str = "",
                        snapshot_id: Optional[str] = None) -> Any:
        """§16.2 snapshot.upload 客户端实现：本机工作库字节 → daemon 发布面。

        gzip 流 ≤32MiB 走内联 mode=inline；否则 uuid4().hex 生成 upload_id
        按 16MiB 切块逐块 chunk → commit。hash 契约针对 gunzip 后原始
        SQLite 字节（sha256 + raw_size 双校验）。所有调用经
        max_body_bytes=SNAPSHOT_UPLOAD_BODY_MAX_BYTES（64MiB 上传面口径）。
        """
        gz = gzip.compress(raw_bytes)
        raw_sha256 = hashlib.sha256(raw_bytes).hexdigest()
        raw_size = len(raw_bytes)
        extra: Dict[str, Any] = {}
        if build_context_hash:
            extra["build_context_hash"] = build_context_hash
        if snapshot_id:
            extra["snapshot_id"] = snapshot_id
        if len(gz) <= SNAPSHOT_UPLOAD_INLINE_MAX_BYTES:
            return self.call("snapshot.upload", {
                "workspace_instance_id": workspace_instance_id,
                "mode": "inline",
                "content_encoding": "gzip",
                "payload_b64": base64.b64encode(gz).decode("ascii"),
                "sha256": raw_sha256,
                "raw_size": raw_size,
                **extra,
            }, max_body_bytes=SNAPSHOT_UPLOAD_BODY_MAX_BYTES)
        upload_id = uuid.uuid4().hex
        total = (len(gz) + SNAPSHOT_UPLOAD_CHUNK_MAX_BYTES - 1) // SNAPSHOT_UPLOAD_CHUNK_MAX_BYTES
        if total > SNAPSHOT_UPLOAD_MAX_CHUNKS:
            raise DaemonRemoteError(
                "E_PAYLOAD_TOO_LARGE",
                f"组装上限 {SNAPSHOT_UPLOAD_MAX_CHUNKS} 块超出"
                f"（{total} 块，raw {raw_size} 字节）",
            )
        for idx in range(total):
            chunk = gz[idx * SNAPSHOT_UPLOAD_CHUNK_MAX_BYTES:(idx + 1) * SNAPSHOT_UPLOAD_CHUNK_MAX_BYTES]
            self.call("snapshot.upload.chunk", {
                "workspace_instance_id": workspace_instance_id,
                "upload_id": upload_id,
                "chunk_index": idx,
                "total_chunks": total,
                "chunk_b64": base64.b64encode(chunk).decode("ascii"),
                "chunk_sha256": hashlib.sha256(chunk).hexdigest(),
            }, max_body_bytes=SNAPSHOT_UPLOAD_BODY_MAX_BYTES)
        return self.call("snapshot.upload.commit", {
            "workspace_instance_id": workspace_instance_id,
            "upload_id": upload_id,
            "sha256": raw_sha256,
            "raw_size": raw_size,
            **extra,
        }, max_body_bytes=SNAPSHOT_UPLOAD_BODY_MAX_BYTES)

    def ensure_remote_workspace(self) -> str:
        """S2 §16.3：remote profile 下确保 workspace 已注册并返回 instance id。

        remote profile 快照入口唯一为 snapshot.upload（§16.1 拒绝
        snapshot.publish 的 db_path），本方法只注册不发布。
        """
        ws_id = self._ensure_remote_snapshot(None)
        if not ws_id:
            raise DaemonUnavailableError("workspace.register 未返回 workspace_instance_id")
        return ws_id

    def call_with_fd(self, method: str, **kwargs: Any) -> Dict[str, Any]:
        """FD 传递能力探测（单轨 HTTPS：仅能力元数据，无 SCM_RIGHTS 物理传递）。

        经 daemon RPC `mcp.daemon_client.call_with_fd` 返回能力元数据
        `{"supported": bool, "transport": ...}`。物理 FD 发送在 HTTPS 单轨下
        不可用（见 `_transport_call_with_fd`）。
        """
        params: Dict[str, Any] = {"method": method}
        params.update(kwargs)
        result = self.call("mcp.daemon_client.call_with_fd", params)
        if not isinstance(result, dict):
            raise RuntimeError(f"call_with_fd: daemon 返回非对象结果 {result!r}")
        return result

    def _transport_call_with_fd(self, method: str, params: Dict[str, Any], fd: int) -> Any:
        """单轨 HTTPS 不支持 SCM_RIGHTS FD 物理传递（fail-closed）。"""
        raise DaemonUnavailableError("单轨 HTTPS 不支持 SCM_RIGHTS FD 传递")

    # ------------------------------------------------------------------
    # 查询方法（与 DaemonClient 签名对齐，通过 call() 转发到 daemon RPC）
    # 仅覆盖 H4A 自举集：stats / query / workspace / task
    # ------------------------------------------------------------------

    def get_stats(self, db_path: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取统计信息（RPC query.stats）。

        W2-1（T-1786840097330-dec66710）：HTTP 模式 query.stats 必须携带
        workspace_instance_id（Rust handler `handle_query_stats` 强制 require，
        缺省返回 invalid_params——即此前 HTTP 模式 get_stats 恒失败的缺陷，
        H6 同类 bug）。先经 `_ensure_remote_snapshot(db_path)` 注册 workspace
        并发布 snapshot（db_path 由 MCP 工具层传入；db_path 为 None 时跳过
        snapshot.publish，仅注册 workspace），再注入权威 workspace_instance_id
        后发起查询。返回结构为 Rust stats_rust()（symbol_count/call_count/...）
        + generation/source_db_path 元信息（既有 H4A 契约，无需映射）。
        """
        params: Dict[str, Any] = {}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.stats", params)

    def get_uncommented_symbols(
        self,
        kind: str = "fn",
        module_filter: str = "",
        limit: int = 100,
        db_path: Optional[str] = None,
    ) -> list:
        """获取未注释符号列表（RPC query.uncommented_symbols）。

        W2-1（T-1786840097330-dec66710）：HTTP 模式 query.uncommented_symbols
        必须携带 workspace_instance_id（Rust handler 强制 require，缺省返回
        invalid_params）。先经 `_ensure_remote_snapshot` 注入权威
        workspace_instance_id 后发起查询。返回结构与 db 层
        `get_uncommented_symbols`（analyzers/coverage.py）一致：扁平 list，
        元素含 qualified_name/module_path/start_line/end_line/depth/name/kind/
        signature/file_path，按 depth DESC/rel_path/start_line 排序后截断 limit。
        """
        params: Dict[str, Any] = {
            "kind": kind,
            "module_filter": module_filter,
            "limit": limit,
        }
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.uncommented_symbols", params)

    def get_module_call_stats(self, limit: int = 30,
                              db_path: Optional[str] = None) -> list:
        """获取模块间调用统计（RPC query.module_call_stats）。

        W2-1（T-1786840097330-dec66710）：HTTP 模式 query.module_call_stats
        必须携带 workspace_instance_id（Rust handler 强制 require）。先经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id 后发起查询。
        返回结构与 db 层 `get_module_call_stats`（analyzers/call_chain.py）
        一致：list，元素含 caller_module/callee_module/call_count/
        unique_caller_count/unique_callee_count，按 call_count DESC 排序截断。
        """
        params: Dict[str, Any] = {"limit": limit}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.module_call_stats", params)

    def get_semgrep_stats(self, db_path: Optional[str] = None) -> dict:
        """获取 Semgrep 缺陷统计（RPC query.semgrep_stats）。

        W2-1（T-1786840097330-dec66710）：HTTP 模式 query.semgrep_stats 必须
        携带 workspace_instance_id（Rust handler 强制 require）。先经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id 后发起查询。
        返回结构与 db 层 `get_semgrep_stats`（analyzers/issues.py）一致：
        {by_severity, by_language, by_rule(20), by_symbol(20), total_findings}。
        """
        params: Dict[str, Any] = {}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.semgrep_stats", params)

    def get_semgrep_findings(self, severity: str = "", language: str = "",
                             rule_id: str = "", limit: int = 50,
                             db_path: Optional[str] = None) -> list:
        """查询 Semgrep 发现的缺陷（RPC query.semgrep_findings）。

        W3-3（T-1786861820151-deb64c48）：HTTP 模式 query.semgrep_findings
        必须携带 workspace_instance_id（Rust handler 强制 require +
        owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。semgrep_findings
        表无 workspace_id 列，Rust handler 经 `JOIN file_instances` +
        `WHERE fi.workspace_id = ?` 限定查询实现跨 workspace 隔离（与
        query.semgrep_stats 同构）；severity（upper）/language（精确）/
        rule_id（LIKE 模糊）过滤与 limit 由 Rust 侧复刻 db 层语义
        （limit<0 → invalid_params fail-closed）。返回结构与 db 层
        `get_semgrep_findings`（analyzers/issues.py）一致：list，元素含
        sf.* 全列 + file_path，按 ERROR/WARNING 优先、id 降序截断 limit。
        """
        params: Dict[str, Any] = {
            "severity": severity,
            "language": language,
            "rule_id": rule_id,
            "limit": limit,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.semgrep_findings", params)

    def get_file_history(self, file_path: str,
                         db_path: Optional[str] = None) -> list:
        """查询文件版本历史（RPC query.file_history）。

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式 query.file_history
        必须携带 workspace_instance_id（Rust handler 强制 require +
        owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。file_path
        为已规范化的 rel_path（绝对路径→relpath 的规范化在 Python 工具层完成，
        workspaces.root_path 为真相源，daemon client_view_root 不同源）；Rust
        侧按 fi.workspace_id + fi.rel_path 精确匹配（status != 'archived'），
        跨 workspace 隔离。返回结构与 db 层 `get_file_history`（db_query.py）
        一致：list（fv.* + fi.rel_path，按 version_num 倒序）。
        """
        params: Dict[str, Any] = {"file_path": file_path}
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.file_history", params)

    def get_git_commits(self, limit: int = 20, offset: int = 0,
                        db_path: Optional[str] = None) -> list:
        """获取 Git commit 列表（RPC query.git_commits）。

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式 query.git_commits
        必须携带 workspace_instance_id（Rust handler 强制 require +
        owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。limit/offset
        原样透传（limit<0/offset<0 由 Rust handler fail-closed
        invalid_params）。git_commits 表含 workspace_id 列，按 workspace 隔离。
        返回结构与 db 层 `get_git_commits`（db_git.py）一致：list（按
        timestamp 倒序分页）。
        """
        params: Dict[str, Any] = {"limit": limit, "offset": offset}
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.git_commits", params)

    def get_commit_changes(self, commit_hash: str,
                           db_path: Optional[str] = None) -> dict:
        """获取指定 commit 的变更详情（RPC query.git_commit_changes）。

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式
        query.git_commit_changes 必须携带 workspace_instance_id（Rust handler
        强制 require + owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。commit_hash
        原样透传；Rust 两段式：先按 workspace_id + commit_hash 确认归属
        （跨 workspace commit → {"commit": null, "file_changes": []}
        fail-closed），再按 commit_hash 查 git_file_changes LEFT JOIN
        file_instances。返回结构与 db 层 `get_commit_changes`（db_git.py）
        一致：{"commit": dict, "file_changes": [dict]}。
        """
        params: Dict[str, Any] = {"commit_hash": commit_hash}
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.git_commit_changes", params)

    def get_git_stats(self, db_path: Optional[str] = None) -> dict:
        """获取 Git 集成统计（RPC query.git_stats）。

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式 query.git_stats
        必须携带 workspace_instance_id（Rust handler 强制 require +
        owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。
        git_file_changes 无 workspace_id 列，Rust 侧经 JOIN git_commits
        （含 workspace_id）限定统计范围。返回结构与 db 层 `get_git_stats`
        （db_git.py）一致：{commit_count, file_change_count, change_types}。
        """
        params: Dict[str, Any] = {}
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.git_stats", params)

    def get_commit_tasks(self, commit_hash: str,
                         include_task_details: bool = True,
                         db_path: Optional[str] = None) -> list:
        """查询 commit 关联的所有 task（RPC query.commit_tasks）。

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式 query.commit_tasks
        必须携带 workspace_instance_id（Rust handler 强制 require +
        owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。commit_hash
        原样透传（空 commit_hash 由 Rust handler 返回空数组）；task_symbol_changes
        无 workspace 维度，按全局 task_id 查询（与 Python 全局语义一致）。
        include_task_details=True 时 LEFT JOIN tasks 补 title/status/parent_id。
        返回结构与 db 层 `get_commit_tasks`（db_task_attribution.py）一致：
        list（按 task_id 分组、last_change_at 倒序）。
        """
        params: Dict[str, Any] = {
            "commit_hash": commit_hash,
            "include_task_details": include_task_details,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.commit_tasks", params)

    def get_coverage_for_symbol(self, qualified_name: str,
                                db_path: Optional[str] = None) -> Optional[dict]:
        """查询函数覆盖率（RPC query.coverage_for_symbol）。

        W4-2（T-1786886251769-22b94ee8-sub-2）：HTTP 模式
        query.coverage_for_symbol 必须携带 workspace_instance_id（Rust handler
        强制 require + owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。qualified_name
        原样透传；Rust 两段式：symbols JOIN file_instances WHERE
        fi.workspace_id（隔离）+ qualified_name LIMIT 1，再按 symbol_id 查
        coverage_data（全局主键，不跨 workspace）。未找到符号返回 None。
        返回结构与 db 层 `get_coverage_for_symbol`（db_coverage.py）一致：
        {qualified_name, file_path, start_line, end_line, total_lines,
        tracked_lines, covered_lines, coverage_pct, uncovered_lines}。
        """
        params: Dict[str, Any] = {"qualified_name": qualified_name}
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.coverage_for_symbol", params)

    def diff_to_symbol(self, diff_text: str,
                       db_path: Optional[str] = None) -> list:
        """解析 git diff 映射到受影响符号（RPC query.diff_to_symbol）。

        W4-2（T-1786886251769-22b94ee8-sub-2）：HTTP 模式
        query.diff_to_symbol 必须携带 workspace_instance_id（Rust handler
        强制 require + owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。diff_text
        为原始 diff 文本（原样透传）；Rust 侧按 Python 逐行状态机复刻
        （4 个 regex + 显式 DiffParseState，change_type 判定保持 Python
        先重置后判定行为）。返回结构与 db 层 `diff_to_symbol`（db_impact.py）
        一致：list（按 symbol_hash 去重，含 symbol_hash / qualified_name /
        file_path / change_type）。
        """
        params: Dict[str, Any] = {"diff_text": diff_text}
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.diff_to_symbol", params)

    def defect_correlation(self, symbol_hash: str, window_commits: int = 5,
                           db_path: Optional[str] = None) -> Optional[dict]:
        """缺陷关联分析（RPC query.defect_correlation）。

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式
        query.defect_correlation 必须携带 workspace_instance_id（Rust handler
        强制 require + owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。symbol_hash
        原样透传；window_commits 负数语义为 Python 空窗口（不报错，Rust 复刻
        该切片行为）。Rust 两段式：symbol_contents 查 qualified_name → 变更点
        窗口扫描 semgrep_findings（JOIN file_instances 按 workspace 隔离），
        symbol_qualified 直连补充 after_change_at=0 记录。返回结构与 db 层
        `defect_correlation`（db_evolution.py）一致：{symbol_hash,
        total_changes, defects_after_change, defect_types, findings}。
        """
        params: Dict[str, Any] = {
            "symbol_hash": symbol_hash,
            "window_commits": window_commits,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.defect_correlation", params)

    def churn_analysis(self, module_filter: str = "", time_window: str = "90d",
                       db_path: Optional[str] = None) -> Optional[dict]:
        """代码流失分析（RPC query.churn_analysis）。

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式
        query.churn_analysis 必须携带 workspace_instance_id（Rust handler
        强制 require + owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。module_filter
        原样透传（Rust 侧 LIKE '{filter}%'）；time_window 走演化层语义
        （d/w/m/y 单位 + 空格容忍，Rust `parse_time_window_evolution`）。
        Rust 优先 git_file_changes（JOIN git_commits timestamp 过滤），无数据
        回退 file_versions 相邻版本 total_lines 差值。返回结构与 db 层
        `churn_analysis`（db_evolution.py）一致：{churn_rate, total_churned_lines,
        changed_files, total_lines_current, top_churned_files, trend}。
        """
        params: Dict[str, Any] = {
            "module_filter": module_filter,
            "time_window": time_window,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.churn_analysis", params)

    def defect_search(self, category: str = "", severity_filter: str = "",
                      db_path: Optional[str] = None) -> Optional[list]:
        """缺陷模式搜索（RPC query.defect_search）。

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式
        query.defect_search 必须携带 workspace_instance_id（Rust handler
        强制 require + owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。category
        前缀匹配（LIKE '{category}%'）；severity_filter 经 Rust
        `normalize_defect_severity` 标准化（空→"info"、小写+trim），与 Python
        `_normalize_severity` 对齐。Rust 直接 SELECT defect_patterns（无
        workspace 维度，全局视图）。返回结构与 db 层 `defect_pattern_search`
        （db_defect_kb.py）一致：list（9 列，ORDER BY case_count DESC,
        created_at DESC）。
        """
        params: Dict[str, Any] = {
            "category": category,
            "severity_filter": severity_filter,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.defect_search", params)

    def defect_suggest_fix(self, symbol_hash: str, finding_id: int = 0,
                           db_path: Optional[str] = None) -> Optional[dict]:
        """修复建议（RPC query.defect_suggest_fix）。

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式
        query.defect_suggest_fix 必须携带 workspace_instance_id（Rust handler
        强制 require + owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。finding_id>0
        直接按 id 查 semgrep_findings，否则经 symbol_contents 查 qualified_name
        → symbol_qualified 或退化 content_hash 最新一条。Rust 按 Python 三分支
        复刻分数计算（effectiveness_score round 4）。返回结构与 db 层
        `suggest_fix`（db_defect_kb.py）一致：{pattern_id, fix_template,
        similar_fixes, effectiveness_score}。
        """
        params: Dict[str, Any] = {
            "symbol_hash": symbol_hash,
            "finding_id": finding_id,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.defect_suggest_fix", params)

    def get_defect_correlation(self, qualified_name: str, window_commits: int = 5,
                               db_path: Optional[str] = None) -> Optional[dict]:
        """变更-缺陷关联（RPC query.get_defect_correlation）。

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式
        query.get_defect_correlation 必须携带 workspace_instance_id（Rust
        handler 强制 require + owned_workspace ACL + snapshot_not_ready
        保护），经 `_ensure_remote_snapshot` 注入权威 workspace_instance_id。
        Rust 先查 symbol_hash（is_current=1 + is_deleted=0 LIMIT 1），未找到
        返回全 0 报告；找到后复用 query_local_defect_correlation 并计算
        defect_rate（round 3）+ recent_defects 前 3（message 截断 100 字符）。
        返回结构与 db 层 `get_defect_correlation_by_qn`（db_evolution.py）
        一致：{qualified_name, change_count, defect_count, defect_rate,
        defect_types, recent_defects}。
        """
        params: Dict[str, Any] = {
            "qualified_name": qualified_name,
            "window_commits": window_commits,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.get_defect_correlation", params)

    def diff_branches(self, source_branch: str, target_branch: str,
                      db_path: Optional[str] = None) -> dict:
        """比较两个分支的符号差异（RPC query.diff_branches）。

        W4-4（T-1786886251769-22b94ee8-sub-4）：HTTP 模式
        query.diff_branches 必须携带 workspace_instance_id（Rust handler
        强制 require + owned_workspace ACL + snapshot_not_ready 保护），经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id。source_branch
        / target_branch 为分支名（workspace name），原样透传；Rust 按名称
        精确匹配查两个 workspace（复刻 db_branch.py `diff_branches`），任一
        分支不存在 → {"error": "源分支不存在: <名>"} / {"error": "目标分支
        不存在: <名>"}（正常响应体，非 RPC 错误）。跨 workspace 语义：数据在
        peer 合法可访问的 snapshot 库内（连接级 ACL），workspace_instance_id
        仅用于打开该库，不绑定任一分支。返回结构与 db 层 `diff_branches`
        （db_branch.py）一致：{added, removed, modified, unchanged_count}。
        """
        params: Dict[str, Any] = {
            "source_branch": source_branch,
            "target_branch": target_branch,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("query.diff_branches", params)

    def get_clone_stats(self, db_path: Optional[str] = None) -> dict:
        """获取克隆检测统计（RPC task.clone_stats）。

        W2-2（T-1786840097330-a9e0ec69）：HTTP 模式 task.clone_stats 必须
        携带 workspace_instance_id（Rust handler 强制 require）。先经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id 后发起查询。
        返回结构与 db 层 `get_clone_stats`（db_clone_detection.py）一致：
        {total, type1, type2, type3, affected_files, affected_symbols}。
        """
        params: Dict[str, Any] = {}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("task.clone_stats", params)

    def get_job_stats(self, db_path: Optional[str] = None) -> dict:
        """获取任务统计（RPC task.job_stats）。

        W2-2（T-1786840097330-a9e0ec69）：HTTP 模式 task.job_stats 必须携带
        workspace_instance_id（Rust handler 强制 require）。先经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id 后发起查询。
        jobs 为全局任务表但每行绑定 workspace_id，统计按 workspace 隔离。
        返回结构与 db 层 `get_job_stats`（db_jobs.py）一致：
        {pending, running, completed, cancelled, failed, total}。
        """
        params: Dict[str, Any] = {}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("task.job_stats", params)

    def get_job_status(self, job_id: str,
                       db_path: Optional[str] = None) -> dict:
        """查询后台任务状态（RPC task.job_status）。

        W3-2（T-1786861820151-f3cecf40）：HTTP 模式 task.job_status 必须携带
        workspace_instance_id（Rust handler 强制 require + owned_workspace
        ACL + snapshot_not_ready 保护），经 `_ensure_remote_snapshot` 注入
        权威 workspace_instance_id。Rust handler 按 job_id + workspace_id
        限定查询（跨 workspace 隔离，job 不属于当前 workspace → not found
        fail-closed）。返回结构与 db 层 `get_job` + `Job.to_dict()`
        （db_jobs.py）一致：dict（asdict 全字段）或
        {"error": "job not found: <job_id>"}。
        """
        params: Dict[str, Any] = {"job_id": job_id}
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("task.job_status", params)

    def list_jobs(self, job_type: str = "", status: str = "", limit: int = 100,
                  db_path: Optional[str] = None) -> list:
        """列出后台任务（RPC task.list_jobs）。

        W3-2（T-1786861820151-f3cecf40）：HTTP 模式 task.list_jobs 必须携带
        workspace_instance_id（Rust handler 强制 require + owned_workspace
        ACL + snapshot_not_ready 保护），经 `_ensure_remote_snapshot` 注入
        权威 workspace_instance_id。job_type / status 空字符串不过滤（复刻
        Python `job_type or None`），limit<0 由 Rust 侧 fail-closed
        invalid_params。返回结构与 db 层 `list_jobs`（db_jobs.py）一致：
        list（Job.to_dict()，按 created_at 降序）。
        """
        params: Dict[str, Any] = {
            "job_type": job_type,
            "status": status,
            "limit": limit,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("task.list_jobs", params)

    def wait_for_job(self, job_id: str, timeout: float = 30.0,
                     poll_interval: float = 0.5,
                     db_path: Optional[str] = None) -> dict:
        """等待后台任务完成并返回结果（RPC task.wait_for_job）。

        W3-2（T-1786861820151-f3cecf40）：HTTP 模式 task.wait_for_job 必须
        携带 workspace_instance_id（Rust handler 强制 require + owned_workspace
        ACL + snapshot_not_ready 保护），经 `_ensure_remote_snapshot` 注入
        权威 workspace_instance_id。轮询语义由 Rust handler 复刻 Python
        wait_for_job（deadline 内循环查询 jobs 表，终态即返回，否则按
        poll_interval sleep；超时返回 status="timeout"），查询按 workspace_id
        限定实现跨 workspace 隔离。返回结构：
        {job_id, status, progress, result_summary, error, elapsed}。
        """
        params: Dict[str, Any] = {
            "job_id": job_id,
            "timeout": timeout,
            "poll_interval": poll_interval,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("task.wait_for_job", params)

    def get_clone_group_stats(self, db_path: Optional[str] = None) -> dict:
        """获取 clone groups 统计（RPC task.clone_group_stats）。

        W2-2（T-1786840097330-a9e0ec69）：HTTP 模式 task.clone_group_stats
        必须携带 workspace_instance_id（Rust handler 强制 require）。先经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id 后发起查询。
        返回结构与 db 层 `get_clone_group_stats`（db_clone_groups.py）一致：
        {total_groups, type1, type2, type3, total_members, affected_files,
        affected_symbols}。
        """
        params: Dict[str, Any] = {}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("task.clone_group_stats", params)

    def defect_stats(self, db_path: Optional[str] = None) -> dict:
        """获取缺陷知识库统计（RPC defect.stats）。

        W2-3（T-1786840097331-fd01a3f8）：HTTP 模式 defect.stats 必须携带
        workspace_instance_id（Rust handler 强制 require，owned_workspace ACL）。
        defect_patterns / defect_fixes 无 workspace_id 列，统计为**全局视图**
        （与 Python db 层 `defect_stats` 语义一致；workspace_instance_id 仅
        用于 ACL）。先经 `_ensure_remote_snapshot` 注入权威 workspace_instance_id
        后发起查询。返回结构与 db 层 `defect_stats`（db_defect_kb.py）一致：
        {total_patterns, total_fixes, by_category, by_severity,
        avg_effectiveness, top_defects}。
        """
        params: Dict[str, Any] = {}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("defect.stats", params)

    def get_edit_stats(self, time_window: str = "30d",
                       db_path: Optional[str] = None) -> dict:
        """获取文件编辑统计（RPC edit.stats）。

        W2-3（T-1786840097331-fd01a3f8）：HTTP 模式 edit.stats 必须携带
        workspace_instance_id（Rust handler 强制 require，owned_workspace ACL）。
        file_edit_audit 统计为**全局视图**（与 Python db 层 `get_edit_stats`
        语义一致，无 workspace 过滤；workspace_instance_id 仅用于 ACL）。
        time_window 原样透传，由 Rust handler 复刻 `_parse_time_window` 语义
        解析（空/all→0、Nd/Nw/Nh/Ny→now-N*秒、ISO 日期→本地时区时间戳）。
        返回结构与 db 层 `get_edit_stats`（db_edit.py）一致：
        {time_window, total, by_status, by_operation, revert_rate}。
        """
        params: Dict[str, Any] = {"time_window": time_window}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("edit.stats", params)

    def list_build_contexts(self, workspace_id: int,
                            db_path: Optional[str] = None,
                            workspace_instance_id: Optional[str] = None) -> list:
        """列出 workspace 的构建上下文（RPC build_context.list）。

        W3-1（T-1786861820150-bfe5e805）：HTTP 模式 build_context.list 必须
        携带 workspace_instance_id（Rust handler 强制 require，owned_workspace
        ACL + snapshot_not_ready 保护），并经 `require_bound_workspace_id`
        校验 workspace_id 与权威 workspace_id 一致（不一致 → invalid_params
        fail-closed，防跨 workspace 越权读取）。未显式传入
        `workspace_instance_id` 时先经 `_ensure_remote_snapshot` 注入当前
        workspace 的权威 instance（P3-2：显式传入可定向已发布 snapshot 的
        workspace）。返回结构与 db 层 `list_build_contexts`（db_toolchain.py）
        一致：list，元素含 workspace_id/build_context_hash/name/compile_flags/
        defines/include_paths/is_active/created_at，按 created_at 升序。
        """
        params: Dict[str, Any] = {"workspace_id": workspace_id}
        ws_id = self._ensure_remote_snapshot(db_path)
        if workspace_instance_id:
            params["workspace_instance_id"] = workspace_instance_id
        elif ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("build_context.list", params)

    def get_build_context(self, workspace_id: int, build_context_hash: str,
                          db_path: Optional[str] = None,
                          workspace_instance_id: Optional[str] = None) -> Optional[dict]:
        """查询构建上下文详情（RPC build_context.get）。

        W3-1（T-1786861820150-bfe5e805）：HTTP 模式 build_context.get 必须
        携带 workspace_instance_id（Rust handler 强制 require + workspace_id
        绑定校验 fail-closed）。P3-2：显式 `workspace_instance_id` 优先，
        否则经 `_ensure_remote_snapshot` 注入当前 workspace。Rust 侧支持短
        hash 前缀匹配（唯一前缀才返回，0/多返回 None，复刻 Python
        db_toolchain.get_build_context）。返回结构与 db 层一致：dict 或
        None（不存在时）。
        """
        params: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "build_context_hash": build_context_hash,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if workspace_instance_id:
            params["workspace_instance_id"] = workspace_instance_id
        elif ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("build_context.get", params)

    def get_active_build_context(self, workspace_id: int,
                                 db_path: Optional[str] = None,
                                 workspace_instance_id: Optional[str] = None) -> Optional[dict]:
        """查询当前活跃的构建上下文（RPC build_context.active）。

        W3-1（T-1786861820150-bfe5e805）：HTTP 模式 build_context.active 必须
        携带 workspace_instance_id（Rust handler 强制 require + workspace_id
        绑定校验 fail-closed）。P3-2：显式 `workspace_instance_id` 优先，
        否则经 `_ensure_remote_snapshot` 注入当前 workspace。返回结构与 db 层
        `get_active_build_context`（db_toolchain.py）一致：dict 或 None
        （无 active 时）。
        """
        params: Dict[str, Any] = {"workspace_id": workspace_id}
        ws_id = self._ensure_remote_snapshot(db_path)
        if workspace_instance_id:
            params["workspace_instance_id"] = workspace_instance_id
        elif ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("build_context.active", params)

    def get_resolved_edges(
        self,
        workspace_id: int,
        build_context_hash: str,
        caller_symbol_id: Optional[int] = None,
        limit: int = 50,
        db_path: Optional[str] = None,
    ) -> list:
        """查询解析后的跨文件调用边（RPC build_context.resolved_edges）。

        W3-1（T-1786861820150-bfe5e805）：HTTP 模式 build_context.resolved_edges
        必须携带 workspace_instance_id（Rust handler 强制 require + workspace_id
        绑定校验 fail-closed）。caller_symbol_id 指定时按 call_line 排序，
        未指定时按 caller_symbol_id, call_line 排序；limit<=0 不限定
        （与 Python `limit is not None and limit > 0` 语义一致）。返回结构与
        db 层 `get_resolved_edges`（db_toolchain.py）一致：list。
        """
        params: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "build_context_hash": build_context_hash,
            "caller_symbol_id": caller_symbol_id,
            "limit": limit,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("build_context.resolved_edges", params)

    def count_resolved_edges(self, workspace_id: int, build_context_hash: str,
                             db_path: Optional[str] = None) -> dict:
        """统计构建上下文下的 resolved_edges 数量（RPC build_context.count_resolved_edges）。

        W3-1（T-1786861820150-bfe5e805）：HTTP 模式必须携带
        workspace_instance_id（Rust handler 强制 require + workspace_id 绑定
        校验 fail-closed）。返回 {"count": int}，与 db 层 `count_resolved_edges`
        （db_toolchain.py）一致。
        """
        params: Dict[str, Any] = {
            "workspace_id": workspace_id,
            "build_context_hash": build_context_hash,
        }
        ws_id = self._ensure_remote_snapshot(db_path)
        if ws_id is not None:
            params["workspace_instance_id"] = ws_id
        return self.call("build_context.count_resolved_edges", params)

    def search_symbols(self, query: str, kind: Optional[str] = None,
                       limit: int = 20, db_path: Optional[str] = None) -> list:
        """搜索符号（RPC query.search）。"""
        return self.call("query.search", {
            "query": query, "kind": kind, "limit": limit,
        })

    def get_symbol(self, qualified_name: str, db_path: Optional[str] = None) -> Optional[dict]:
        """获取符号详情（RPC query.symbol）。

        M2.2（T-1786519211817-fcc40690）：HTTP 模式 query.symbol 必须携带
        workspace_instance_id（Rust handler `handle_query_symbol` 强制 require，
        缺省返回 invalid_params）。先经 `_ensure_remote_snapshot` 注册 workspace
        并发布 snapshot（db_path 由 MCP 工具层传入），再注入权威
        workspace_instance_id 后发起查询。与 M2.1 get_file_symbols 同构。
        """
        params = {"qualified_name": qualified_name}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.symbol", params)

    def get_symbol_location(self, name: str, file_path: str = "",
                            db_path: Optional[str] = None) -> Optional[dict]:
        """获取符号位置（RPC query.symbol_location）。

        M2.2（T-1786519211817-fcc40690）：HTTP 模式 query.symbol_location 必须携带
        workspace_instance_id（Rust handler `handle_query_symbol_location` 强制
        require，缺省返回 invalid_params）。与 get_symbol 同构：先经
        `_ensure_remote_snapshot` 注入权威 workspace_instance_id 后发起查询。
        """
        params = {"name": name, "file_path": file_path}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.symbol_location", params)

    def get_file_symbols(self, file_path: str, db_path: Optional[str] = None) -> list:
        """获取文件中的符号（RPC query.file）。

        M2.1（T-1786519172968-f13db464）：HTTP 模式 query.file 必须携带
        workspace_instance_id（Rust handler `handle_query_file` 强制 require，
        缺省返回 invalid_params）。先经 `_ensure_remote_snapshot` 注册 workspace
        并发布 snapshot（db_path 由 MCP 工具层传入），再注入权威
        workspace_instance_id 后发起查询。H6-FIX request_id 逻辑不受影响。
        """
        params = {"file_path": file_path}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.file", params)

    def query_grep(
        self,
        patterns: List[str],
        fixed: bool = False,
        limit: int = 200,
        path: Optional[str] = None,
        include_all: bool = False,
        kind: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Any:
        """按 patterns 在 snapshot 已索引内容文本搜索（rg 风格），RPC query.grep。

        M2.3（T-1786519211823-fd25bb10，HTTP 轮次）：HTTP 模式 query.grep 必须携带
        workspace_instance_id（Rust handler `handle_query_grep` 强制 require，
        缺省返回 invalid_params）。先经 `_ensure_remote_snapshot` 注册 workspace
        并发布 snapshot（db_path 由 MCP 工具层传入），再注入权威
        workspace_instance_id 后发起查询。参数契约与 legacy `DaemonClient.query_grep`
        （L1179）对齐：patterns/fixed/limit/path/include_all/kind。
        Rust handler 返回 `Value::String`（格式化文本，含符号归属上下文），
        无匹配时返回 `No matches for: <pattern>` 文本。
        """
        params: Dict[str, Any] = {
            "patterns": patterns,
            "fixed": fixed,
            "limit": limit,
            "path": path,
            "include_all": include_all,
            "kind": kind,
        }
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.grep", params)

    def query_issues(
        self,
        qualified_name: str,
        include_info: bool = False,
        db_path: Optional[str] = None,
    ) -> Any:
        """按 qualified_name 查询符号缺陷（semgrep + guardrail findings），RPC query.issues。

        M2.4（T-1786519211831-fd9a5380，HTTP 轮次）：HTTP 模式 query.issues 必须携带
        workspace_instance_id（Rust handler `handle_query_issues` 强制 require，
        缺省返回 invalid_params）。先经 `_ensure_remote_snapshot` 注册 workspace
        并发布 snapshot（db_path 由 MCP 工具层传入，以 daemon 返回的
        workspace_instance_id 为权威），再注入后发起查询。参数契约与 legacy
        `DaemonClient.query_issues`（L1213）对齐：qualified_name/include_info。
        Rust dispatch 层对空/纯空白/NUL qualified_name 返回 invalid_params
        （query_handlers.rs M2.4 前置校验）；snapshot 未发布返回 snapshot_not_ready。
        """
        params: Dict[str, Any] = {
            "qualified_name": qualified_name,
            "include_info": include_info,
        }
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.issues", params)

    def query_tests(
        self,
        qualified_name: str,
        reverse: bool = False,
        history: bool = False,
        limit: int = 50,
        db_path: Optional[str] = None,
    ) -> Any:
        """按 qualified_name 查询测试关系（test cases / tested functions / stability），RPC query.tests。

        M2.5（T-1786519211837-fdfffe10，HTTP 轮次）：HTTP 模式 query.tests 必须携带
        workspace_instance_id（Rust handler `handle_query_tests` 强制 require，
        缺省返回 invalid_params）。先经 `_ensure_remote_snapshot` 注册 workspace
        并发布 snapshot（db_path 由 MCP 工具层传入，以 daemon 返回的
        workspace_instance_id 为权威），再注入后发起查询。参数契约与 legacy
        `DaemonClient.query_tests`（L1271）对齐：qualified_name/reverse/history/limit
        + db_path 透传。reverse/history 区分查询语义：reverse=False/history=False →
        test cases，reverse=True → tested functions，history=True → test stability
        （与 Rust `handle_query_tests` 三分支一致）。Rust dispatch 层对空/纯空白/NUL
        qualified_name 返回 invalid_params（query_handlers.rs M2.5 前置校验）；
        snapshot 未发布返回 snapshot_not_ready。
        """
        params: Dict[str, Any] = {
            "qualified_name": qualified_name,
            "reverse": reverse,
            "history": history,
            "limit": limit,
        }
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("query.tests", params)

    def workspace_status(self, db_path: Optional[str] = None) -> Any:
        """查询当前 workspace 状态（RPC workspace.status）。

        W1-1（T-1786808777378-bbcbf059，workspace 读面 HTTP native 修复）：
        HTTP 模式 workspace.status 必须携带 workspace_instance_id（Rust handler
        `handle_workspace_status` 强制 require_str_param，缺省返回 invalid_params
        ——即此前 MCP get_active_workspace HTTP 分支调 workspace.activate {}
        缺注入的缺陷）。先经 `_ensure_remote_snapshot(db_path)` 注册当前
        workspace（db_path 由 MCP 工具层传入，以 daemon 返回的
        workspace_instance_id 为权威），再注入后发起查询。

        workspace.status 是 Rust native 读方法（read_only，owned ACL：
        owner_uid 匹配 + 非 archived，越权/不存在返回 workspace_not_found）。
        "当前活动工作区"在 HTTP 模式下即当前配置 workspace 的 daemon 视图
        （daemon_workspaces 行：workspace_id/workspace_instance_id/snapshot_id/
        owner_uid/git_remote_url/git_head_commit_sha/client_view_root/
        host_real_root/toolchain_fingerprint/registered_at/last_active_at/
        status），legacy 的 is_active 概念由 HTTP 模式单 workspace 语义替代。
        """
        params: Dict[str, Any] = {}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("workspace.status", params)

    # ------------------------------------------------------------------
    # W1-2（T-1786808777379-15702f0c）：workspace 写面（register / activate /
    # remove）daemon 同步便捷方法
    # ------------------------------------------------------------------

    def workspace_register(self, root_path: str) -> dict:
        """把 workspace 同步进 daemon 注册表（RPC workspace.register，幂等）。

        W1-2：MCP register_workspace 工具在 HTTP 模式经本方法同步 daemon
        注册表（daemon_workspaces 是读面 workspace.list/status 的数据源），
        SQLite workspaces 表仍为真相源（tools_workspace.py 先写 SQLite）。
        Rust `handle_workspace_register`（workspace.rs L1332）强制
        client_view_root（validate_owned_path 要求路径为目录），返回行以
        `workspace_instance_id` 为权威（compute = sha256(owner_uid|
        host_real_root) 前 16 位）；Git remote/HEAD 只形成 snapshot provenance；
        响应缺该字段抛 `DaemonUnavailableError`（fail-closed，与
        `_ensure_remote_snapshot` 同款校验）。

        与 `_ensure_remote_snapshot` 的差异：本方法注册任意 root_path 的
        workspace（register_workspace 工具可注册非当前项目目录），不做
        snapshot.publish（新 workspace 的 snapshot 由读面工具首次查询时经
        `_ensure_remote_snapshot` 懒发布，写入工具无需发布）。

        Args:
            root_path: workspace 根目录绝对路径（即 client_view_root）

        Returns:
            daemon 行（workspace_id/workspace_instance_id/.../status=active）
        """
        register_params = {"client_view_root": root_path}
        register_params.update(_workspace_snapshot_metadata(root_path))
        workspace = self.call("workspace.register", register_params)
        if not isinstance(workspace, dict) or "workspace_instance_id" not in workspace:
            raise DaemonUnavailableError(
                f"workspace.register 响应缺少 workspace_instance_id: {workspace!r}"
            )
        self._workspace_instance_by_root[_norm_root(root_path)] = workspace["workspace_instance_id"]
        return workspace

    def _resolve_workspace_instance(self, root_path: str) -> str:
        """按 root_path 解析权威 workspace_instance_id（缓存优先，缺省幂等 register）。

        W1-2：workspaces 表无 workspace_instance_id 列（禁改 schema），
        set_active/delete 用 root_path（workspaces.root_path UNIQUE）做
        join key 映射到 daemon workspace_instance_id。workspace.register 是
        provenance-preserving UPSERT；instance_id 只由 (owner_uid,
        host_real_root) 的确定性 hash 决定，Git metadata 只更新 snapshot，
        因此重复 register 不会旋转物理 workspace 身份。
        """
        key = _norm_root(root_path)
        cached = self._workspace_instance_by_root.get(key)
        if cached is not None:
            return cached
        register_params = {"client_view_root": root_path}
        register_params.update(_workspace_snapshot_metadata(root_path))
        workspace = self.call("workspace.register", register_params)
        if not isinstance(workspace, dict) or "workspace_instance_id" not in workspace:
            raise DaemonUnavailableError(
                f"workspace.register 响应缺少 workspace_instance_id: {workspace!r}"
            )
        self._workspace_instance_by_root[key] = workspace["workspace_instance_id"]
        return workspace["workspace_instance_id"]

    def workspace_activate(self, root_path: str) -> dict:
        """激活 workspace 的 daemon 注册表状态（RPC workspace.activate）。

        W1-2：MCP set_active_workspace 在 HTTP 模式先写 SQLite（is_active
        真相源），再经本方法同步 daemon 侧状态。先按 root_path 解析权威
        workspace_instance_id（缓存/幂等 register），再调 workspace.activate
        （Rust `handle_workspace_activate` 强制 require_str_param，owned ACL
        `owned_workspace_any_status`：owner_uid 匹配即可，任意状态均可激活）。
        返回 daemon 行（status=active）。

        Args:
            root_path: workspace 根目录绝对路径

        Returns:
            daemon 行（status=active）
        """
        instance_id = self._resolve_workspace_instance(root_path)
        return self.call("workspace.activate", {"workspace_instance_id": instance_id})

    def workspace_remove(self, root_path: str) -> dict:
        """删除 workspace 的 daemon 注册表条目（RPC workspace.remove，archive 软删）。

        W1-2：MCP delete_workspace 在 HTTP 模式先写 SQLite（硬删，真相源），
        再经本方法同步 daemon 侧。Rust `handle_workspace_remove` 语义为
        archive（update_workspace_status("archived")）——daemon 注册表无硬删
        RPC，软删即最接近等价物（读面 workspace.list/status 的 owned ACL
        已排除 archived 行，对调用方呈现为"已删除"）。重复调用幂等
        （owned_workspace_any_status 允许任意状态）。

        Args:
            root_path: workspace 根目录绝对路径

        Returns:
            daemon 行（status=archived）
        """
        instance_id = self._resolve_workspace_instance(root_path)
        return self.call("workspace.remove", {"workspace_instance_id": instance_id})

    # ------------------------------------------------------------------
    # W1-3（T-1786808777379-c87171e7）：snapshot 管理（stats /
    # list_workspaces / evict）HTTP 便捷方法
    # ------------------------------------------------------------------

    def snapshot_stats(self, db_path: Optional[str] = None) -> Any:
        """查询当前 workspace snapshot 统计（RPC snapshot.stats）。

        W1-3：HTTP 模式 snapshot.stats 必须携带 workspace_instance_id
        （Rust `handle_snapshot_stats`（snapshot_state.rs L1054）强制
        require_str_param，缺省返回 invalid_params）。先经
        `_ensure_remote_snapshot(db_path)` 注册 workspace 并按需发布
        snapshot（db_path 由调用方传入，以 daemon 返回的
        workspace_instance_id 为权威），再注入后发起查询；db_path 为
        None 时不发布（未发布 snapshot → Rust 返回 snapshot_not_ready，
        fail-closed，不静默回退本地 SQL）。

        snapshot.stats 是 Rust native 读方法（read_only，owned ACL：
        `owned_workspace` owner_uid 匹配 + 非 archived，越权/不存在返回
        workspace_not_found）。返回字段：workspace_instance_id/generation/
        symbol_count/call_count/file_count/build_duration_ms/last_error/
        source_db_path/history_len（对齐 Python snapshot_manager.py:162-192
        get_snapshot_stats 语义）。

        Args:
            db_path: 本地 SQLite 库路径（None 时跳过 snapshot.publish，
                未发布则 Rust 返回 snapshot_not_ready）

        Returns:
            snapshot 统计 dict（含 workspace_instance_id）
        """
        params: Dict[str, Any] = {}
        workspace_id = self._ensure_remote_snapshot(db_path)
        if workspace_id is not None:
            params["workspace_instance_id"] = workspace_id
        return self.call("snapshot.stats", params)

    def snapshot_list_workspaces(self) -> Any:
        """列出已发布 snapshot 的 workspace（RPC snapshot.list_workspaces）。

        W1-3：无参数方法，不需要 workspace_instance_id（Rust
        `handle_snapshot_list_workspaces`（snapshot_state.rs L1111）不
        require 任何参数）。安全边界：按 peer UID 过滤——admin
        （peer.uid == 0 或 daemon uid）可查看所有 workspace，非 admin 只能
        看到自己的 workspace（P0-2 整改：原实现忽略 peer 导致跨 UID 泄露，
        现经 registry owner_uid 交集过滤）。返回 snapshot_cache 中每个
        workspace 的统计条目数组（workspace_instance_id/generation/
        history_len/symbol_count/call_count/file_count/build_duration_ms）。

        Returns:
            snapshot 统计条目数组（按 peer UID 过滤）
        """
        return self.call("snapshot.list_workspaces", {})

    def snapshot_evict(self, workspace_instance_id: str) -> Any:
        """驱逐 workspace 的 snapshot 缓存（RPC snapshot.evict）。

        W1-3：Rust `handle_snapshot_evict`（snapshot_state.rs L1173）强制
        require_str_param(workspace_instance_id)（缺省返回 invalid_params），
        且为写方法（mutation，受 protected mutation 列表约束）——修改
        snapshot cache 前经 `owned_workspace` ACL 校验（owner_uid 匹配 +
        非 archived，越权/不存在返回 workspace_not_found）。幂等：重复
        驱逐已不在 cache 的 workspace 返回 {"evicted": false}（但 workspace
        未注册时仍被 ACL 拒绝）。

        Args:
            workspace_instance_id: 权威 workspace instance id
                （来自 workspace.register 返回值）

        Returns:
            {"evicted": bool, "workspace_instance_id": str}
        """
        return self.call("snapshot.evict", {
            "workspace_instance_id": workspace_instance_id,
        })

    def get_callers(self, callee_name: str, qualified_name: Optional[str] = None,
                    db_path: Optional[str] = None) -> list:
        """查询调用者（RPC query.callers）。"""
        return self.call("query.callers", {
            "callee_name": callee_name, "qualified_name": qualified_name,
        })

    def get_callees(self, caller_name: str, qualified_name: Optional[str] = None,
                    db_path: Optional[str] = None) -> list:
        """查询被调者（RPC query.callees）。"""
        return self.call("query.callees", {
            "caller_name": caller_name, "qualified_name": qualified_name,
        })

    def get_topological_order(self, limit: int = 50, db_path: Optional[str] = None) -> list:
        """获取拓扑排序（RPC query.topological_order）。"""
        return self.call("query.topological_order", {"limit": limit})

    def get_call_chain_down(self, qualified_name: str, max_depth: int = 10,
                             db_path: Optional[str] = None) -> list:
        """向下调用链（RPC query.call_chain_down）。"""
        return self.call("query.call_chain_down", {
            "qualified_name": qualified_name, "max_depth": max_depth,
        })

    def detect_cycles(self, max_depth: int = 10, db_path: Optional[str] = None) -> list:
        """检测循环调用（RPC query.detect_cycles）。"""
        return self.call("query.detect_cycles", {"max_depth": max_depth})

    # ------------------------------------------------------------------
    # 工作区方法（RPC workspace.*）
    # ------------------------------------------------------------------

    def list_workspaces(self) -> list:
        """列出工作区（RPC workspace.list）。"""
        return self.call("workspace.list", {})

    def get_active_workspace(self) -> Optional[Dict[str, Any]]:
        """获取活动工作区（RPC workspace.activate）。"""
        return self.call("workspace.activate", {})

    def set_active_workspace(self, workspace_id_or_name: str) -> bool:
        """设置活动工作区（RPC workspace.activate 写入）。"""
        return self.call("workspace.activate", {"workspace_id_or_name": workspace_id_or_name})

    def register_workspace(self, name: str, root_path: str, description: str = "") -> int:
        """注册工作区（RPC workspace.register）。"""
        return self.call("workspace.register", {
            "name": name, "root_path": root_path, "description": description,
        })

    def get_workspace_status(self, workspace_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """获取工作区状态（RPC workspace.status）。"""
        return self.call("workspace.status", {"workspace_id": workspace_id})


# ----------------------------------------------------------------------
# 统一 Task 写/读 路由规则函数
# ----------------------------------------------------------------------

def _get_rpc_client_for_route():
    """获取 RPC client（单轨：唯一传输是 HTTPS+mTLS）。"""
    return HttpDaemonRpcClient.get_instance()


def _inject_workspace_id(params: dict) -> dict:
    """为 daemon RPC 注入显式 `workspace_id`（fail-closed，SRV-006 薄客户端化）。

    abi-error-code-contract.md：生产路径必须显式传入 `workspace_id > 0`。
    authority 下沉 Rust daemon 后，注入由 daemon RPC
    `mcp.daemon_client.inject_workspace_id` 完成：daemon 基于权威库状态解析
    active workspace（无 active workspace 时返回 internal_error，fail-closed），
    本函数不再读取本地 CodeGraphDB（无 get_db、无本地 workspace 推导）。
    """
    if params.get("workspace_id"):
        return params
    client = _get_rpc_client_for_route()
    # cwd 显式上送：daemon 侧按 root_path 匹配解析 workspace（起点优先），
    # 避免多 active workspace 时静默取最小 id 导致跨项目任务混串
    # （cwd 在 callwarden 却拿到 TokenSlim 任务的根因）。
    result = client.call("mcp.daemon_client.inject_workspace_id", {
        "params": dict(params),
        "cwd": os.getcwd(),
    })
    if not isinstance(result, dict) or not isinstance(result.get("params"), dict):
        raise RuntimeError(f"_inject_workspace_id: daemon 返回非对象结果 {result!r}")
    return result["params"]


def _is_task_scoped_authority_request(params: dict) -> bool:
    """Return whether ``params`` identifies an immutable task binding.

    A task-scoped daemon method must resolve its numeric workspace through
    ``task_workspace_bindings``.  Injecting the legacy active workspace here
    is both unnecessary and unsafe in a multi-project daemon: the active row
    can belong to a different registered project while the task binding is
    still authoritative.  The presence of a non-blank task ID is deliberately
    the discriminator, rather than a hand-maintained RPC allowlist, so newly
    added task/lease methods inherit the same rule.

    ``superseded_id`` is recognized as the same-strength discriminator: it
    identifies the source task of ``task.supersede`` / ``task.superseded_by``
    (SR-01).  Both daemon handlers resolve an omitted numeric workspace from
    the immutable binding and fail closed on an explicit mismatch, so the
    legacy active-workspace injection must never preempt them.
    """
    for key in ("task_id", "superseded_id"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return True
    return False


def resolve_workspace_pair_from_daemon(project_root: Optional[str] = None) -> dict:
    """Resolve the current project's canonical daemon workspace pair.

    Task creation must not use the task DB's global ``is_active`` row: when
    CallWarden and TokenSlim share a daemon, that row can resolve to another
    project (for example ``10/ws-10``).  This read-only resolver instead finds
    the current project in the daemon registry and accepts it only when the
    registry and task DB agree on the same immutable instance ID.

    No numeric ID is inferred, no ``ws-{id}`` value is synthesized, and zero
    or multiple reconciled candidates fail closed.
    """
    client = _get_rpc_client_for_route()
    call = getattr(client, "call", None)
    if call is None:
        raise DaemonUnavailableError("RPC client 缺少 call 方法（transport 未就绪）")

    if not project_root:
        # 多项目共用一个 daemon 时，任何"默认项目"回退都会把调用方静默绑到
        # 另一个项目（例如在 TokenSlim 目录下建卡却落到 callwarden）。调用方
        # 必须显式提供项目根；缺失即 fail-closed。
        raise DaemonUnavailableError(
            "E_WORKSPACE_ROOT_REQUIRED: 解析 workspace 配对必须显式提供项目根"
            "（禁止回退到连接/客户端默认项目）"
        )
    target_root = _norm_root(os.path.abspath(str(project_root)).replace("\\\\?\\", ""))

    rows = call("workspace.list", {})
    if not isinstance(rows, list):
        raise DaemonUnavailableError(
            f"resolve_workspace_pair: workspace.list 返回非数组: {rows!r}"
        )

    matches = []
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "active":
            continue
        row_root = row.get("client_view_root") or row.get("host_real_root") or ""
        if _norm_root(str(row_root).replace("\\\\?\\", "")) != target_root:
            continue
        instance_id = row.get("workspace_instance_id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            continue
        try:
            status = call("workspace.status", {"workspace_instance_id": instance_id})
        except DaemonRemoteError:
            continue
        if not isinstance(status, dict):
            continue
        registry_id = status.get("registry_workspace_id")
        registry_instance = status.get("registry_instance_id")
        task_db_id = status.get("task_db_workspace_id")
        task_db_instance = status.get("task_db_instance_id")
        if (
            not isinstance(registry_id, int)
            or registry_id <= 0
            or not isinstance(task_db_id, int)
            or task_db_id <= 0
            or registry_instance != instance_id
            or task_db_instance != instance_id
        ):
            continue
        # ``registry_workspace_id`` identifies the daemon registry row, while
        # task.create persists the task-DB workspace ID.  Return the latter so
        # the client forwards the same numeric authority that will appear in
        # its immutable binding/readback; reconciliation remains a server-side
        # safety net, not a hidden client-side ID translation.
        matches.append((task_db_id, instance_id))

    if len(matches) != 1:
        raise DaemonUnavailableError(
            "resolve_workspace_pair: 当前项目未获得唯一的 registry/task-DB "
            f"canonical workspace binding（root={target_root!r}, matches={len(matches)}）"
        )
    workspace_id, workspace_instance_id = matches[0]
    return {"workspace_id": workspace_id, "workspace_instance_id": workspace_instance_id}


# 连接/发现级错误码：这些不是 daemon 的业务结论，而是 daemon 不可达或
# endpoint/manifest 发现校验失败。它们虽以 DaemonRemoteError 抛出，但语义
# 等同连接故障——路由层必须走 fail-closed（或允许的回退）路径，而不是当作
# 业务错误透传给客户端（否则 CLI 会看到 "E_HTTP_MANIFEST_STALE: manifest PID
# 14400 已不存活" 这类环境噪音，无法区分业务冲突与 daemon 不可用）。
_CONNECT_LEVEL_DAEMON_ERROR_CODES = frozenset({
    E_HTTP_MANIFEST_MISSING,
    E_HTTP_MANIFEST_STALE,
    E_HTTP_DAEMON_UNAVAILABLE,
})


def _is_connect_level_daemon_error(exc: BaseException) -> bool:
    """判断异常是否为 daemon 连接/发现级故障（而非远端业务结论）。

    - DaemonRemoteError：仅当其 code 命中 manifest/连接级错误码时为连接故障；
      其余（task_not_found / permission_denied / task_conflict 等）是业务结论。
    - 非 DaemonRemoteError 的异常（超时/拒绝连接/OSError 等）一律视为连接故障。
    """
    if isinstance(exc, DaemonRemoteError):
        return getattr(exc, "code", "") in _CONNECT_LEVEL_DAEMON_ERROR_CODES
    return True


def route_task_write(rpc_method: str, params: dict, fallback_func):
    """统一任务写操作路由规则：
    1. local 模式 -> 直接执行 fallback_func（本地 SQLite）
    2. enterprise / auto 模式 -> 通过 daemon RPC 执行
    3. HTTP 模式（CW_DAEMON_TRANSPORT=http）-> 通过 HttpDaemonRpcClient 执行
    4. enterprise / auto 模式下若 daemon 不可用，禁止 fallback 本地 SQLite，抛出 DaemonUnavailableError (fail-closed)
    """
    mode = get_daemon_mode()
    governed_create = rpc_method == "task.create" and (
        bool(params.get("parent_id")) or
        any(key in params for key in ("task_contract_envelope", "identity_policy", "task_id")))
    if governed_create and mode == "local":
        raise DaemonUnavailableError("Governed task.create requires daemon authority; no local fallback")
    if mode == "local":
        if _is_task_write(rpc_method) and get_task_write_policy() != "isolated":
            raise SharedTaskWriterRequiredError(
                "共享任务写入要求当前用户 daemon 单写点；"
                "仅单进程测试/离线维护可设置 CW_TASK_WRITE_POLICY=isolated"
            )
        return fallback_func()

    if isinstance(params, dict) and "request_id" not in params:
        import uuid
        params["request_id"] = f"req-{uuid.uuid4().hex[:12]}"

    # A task report is the review-input handoff.  In HTTP mode the thin client
    # asks the daemon for its authority DB path, publishes a snapshot through
    # the daemon, then forwards the returned identity.  Calling
    # _ensure_remote_snapshot(None) would only register the workspace and can
    # never manufacture a snapshot for a fresh client.  Callers must not
    # calculate or hand-copy this value.  The daemon remains the final
    # fail-closed validator, including direct callers and non-HTTP transports.
    if rpc_method == "task.report" and not str(params.get("snapshot_id") or "").strip():
        if is_http_transport_enabled():
            client = HttpDaemonRpcClient.get_instance()
            if client._project_root is None:
                # 兜底按调用进程 cwd（同 route_rpc：禁止硬编码 PROJECT_ROOT）
                client.configure_workspace(os.getcwd())
            db_result = client.call("mcp.common.get_db_path_for_daemon", {})
            snapshot_db_path = (
                db_result.get("db_path") if isinstance(db_result, dict) else None
            )
            if not snapshot_db_path:
                raise DaemonUnavailableError(
                    "daemon 未返回可用于 task.report snapshot.publish 的权威 db_path",
                    code="E_TASK_REPORT_SNAPSHOT_REQUIRED",
                )
            workspace_instance_id = client._ensure_remote_snapshot(snapshot_db_path)
            snapshot_id = client._remote_snapshot_id
            if not workspace_instance_id or not snapshot_id:
                raise DaemonUnavailableError(
                    "task.report 需要 daemon workspace.register 签发的 snapshot_id",
                    code="E_TASK_REPORT_SNAPSHOT_REQUIRED",
                )
            params["workspace_instance_id"] = workspace_instance_id
            params["snapshot_id"] = snapshot_id

    rpc_client = _get_rpc_client_for_route()
    # workspace_id 注入在 try 之外：无 active workspace 时直接抛错（fail-closed），
    # 绝不落入“daemon 不可用 → 本地回退”路径（本地读同样缺少 workspace 隔离）。
    # SR-01：task.supersede / task.superseded_by 是 task-scoped authority 请求，
    # 数字 workspace 由 daemon 从不可变 task_workspace_bindings 解析（省略时）或
    # 显式一致性校验（提供时）；注入 legacy active workspace 会在多项目 daemon
    # 下用其他项目的 workspace 抢占 authority（E_WORKSPACE_AUTHORITY_MISMATCH /
    # 错误的 workspace 过滤投影），故跳过注入。
    if rpc_method in TASK_SUPERSEDE_ROUTE_POLICY and _is_task_scoped_authority_request(params):
        rpc_params = params
    else:
        rpc_params = _inject_workspace_id(params)
    try:
        call = (
            rpc_client.call_with_autostart
            if hasattr(rpc_client, "call_with_autostart")
            else rpc_client.call
        )
        return call(rpc_method, rpc_params)
    except DaemonRemoteError as dre:
        if not _is_connect_level_daemon_error(dre):
            # 远端结构化业务错误（task_conflict / permission_denied / task_not_found 等）
            # 原样透传，不得伪装成 "daemon 连接失败"，否则客户端无法区分业务冲突与连接故障
            raise
        exc: BaseException = dre  # 连接/发现级（stale/missing manifest、daemon 不可达）
    except Exception as error:
        exc = error
    # 连接级故障统一处理（enterprise/auto fail-closed，绝无本地 SQLite 回退）
    if rpc_method in TASK_SUPERSEDE_ROUTE_POLICY:
        # P0-H：governance mutation 无任何本地回退（即使非 enterprise/auto）
        assert_supersede_no_local_fallback(rpc_method, mode)
    if is_daemon_required() or mode == "auto":
        raise DaemonUnavailableError(f"enterprise/auto 模式下任务写操作 daemon 连接失败: {exc}") from exc
    raise


def route_task_read(rpc_method: str, params: dict, fallback_func):
    """统一任务读操作路由规则：
    1. local 模式 -> 直接执行 fallback_func
    2. HTTP 模式（CW_DAEMON_TRANSPORT=http）-> 通过 HttpDaemonRpcClient 执行
    3. enterprise 模式 -> 走 daemon RPC，不可用时 fail-closed
    4. auto 模式 -> 优先走 daemon RPC；对 workspace 隔离敏感的任务读方法
       （task.* / lease.*）不可用时 fail-closed 上抛，**不回退本地 SQLite**
       （本地 db.task_list 无 workspace 过滤，回退会跨 workspace 泄漏）；
       其余方法不可用时降级执行 fallback_func
    """
    mode = get_daemon_mode()
    if mode == "local":
        return fallback_func()

    rpc_client = _get_rpc_client_for_route()
    # workspace_id 注入在 try 之外：无 active workspace 时直接抛错（fail-closed），
    # 绝不落入“daemon 不可用 → 本地回退”路径。
    # SR-01：task.supersede / task.superseded_by 为 task-scoped authority 请求，
    # 跳过 legacy active workspace 注入（同 route_task_write 内注释），
    # 否则 task.superseded_by 只读投影会按错误 workspace 过滤而漏行。
    # 5D：task.next_action 携带 workspace_instance_id 时同理——daemon 侧按
    # instance→capture→binding 三重校验解析 workspace，cwd 注入既冗余又可能注入
    # 不匹配的 workspace_id（未注册 cwd 直接 invalid_params，见 next_action.rs）。
    if (
        rpc_method in TASK_SUPERSEDE_ROUTE_POLICY
        and _is_task_scoped_authority_request(params)
    ) or params.get("workspace_instance_id"):
        rpc_params = params
    else:
        rpc_params = _inject_workspace_id(params)
    try:
        return rpc_client.call(rpc_method, rpc_params)
    except DaemonRemoteError as dre:
        if not _is_connect_level_daemon_error(dre):
            # 业务错误（task_not_found / permission_denied 等）原样透传：
            # auto 模式下不得把"远端明确返回的业务结论"降级为本地读（数据可能不一致）
            raise
        exc: BaseException = dre  # 连接/发现级（stale/missing manifest、daemon 不可达）
    except Exception:
        pass
    # 连接级故障统一处理
    if rpc_method in TASK_SUPERSEDE_ROUTE_POLICY:
        # P0-H：task.superseded_by 只读投影仅由 daemon 提供，任何模式下
        # daemon 不可用一律 fail-closed（不回退本地 SQLite）
        assert_supersede_no_local_fallback(rpc_method, mode)
    if is_daemon_required():
        raise DaemonUnavailableError(f"enterprise 模式下任务读操作 daemon 连接失败: {exc}") from exc
    # auto 模式：workspace 隔离敏感方法禁止本地回退（本地任务层无 workspace 过滤）
    if rpc_method.startswith(("task.", "lease.")):
        raise DaemonUnavailableError(
            f"auto 模式下 {rpc_method} 为 workspace 隔离敏感读操作，"
            f"daemon 不可达时 fail-closed，不回退本地 SQLite: {exc}"
        ) from exc
    return fallback_func()


def route_worker_call(rpc_method: str, params: dict, fallback_func):
    """H4C-1：compat worker 路由便捷方法（符号/任务组 read_only 工具用）。

    为 H4C-2/3 的 python_compat 工具提供统一 worker 路由调用，规则：
    1. local 模式（非 HTTP）-> 直接执行 fallback_func（本地 SQLite，legacy
       语义不变）
    2. HTTP 模式（CW_DAEMON_TRANSPORT=http）-> 经 worker 执行：方法未在
       compat_route 白名单（Python 镜像）注册时 fail-closed 返回结构化
       E_HTTP_COMPAT_UNSUPPORTED，不产生 method_not_found 泄漏；worker
       结构化错误（E_COMPAT_*）与连接失败一律 fail-closed 上抛，不回退本地
       SQLite
    3. enterprise 模式 -> 走 daemon RPC：白名单外 fail-closed unsupported；
       worker 错误原样透传；连接失败抛 DaemonUnavailableError
    4. auto 模式（非 HTTP）-> 优先走 daemon RPC：白名单未声明或
       worker/连接失败时降级执行 fallback_func
    """
    http_enabled = is_http_transport_enabled()
    mode = get_daemon_mode()
    if mode == "local" and not http_enabled:
        return fallback_func()

    # 白名单前置检查：未注册方法不得直传 daemon（HTTP 端会泄漏
    # method_not_found），fail-closed 返回结构化 unsupported。
    # 注意：必须用顶层 `server.compat_registry`（与 compat_worker.py / 工具模块
    # 装配注册同一单例）。`callwarden.server.compat_registry` 是另一个模块对象，
    # 未装载 H4C-2/3 注册，会让全部新方法误判为 unsupported（模块单例风险）。
    from server import compat_registry as _creg

    if _creg.compat_route(rpc_method) is None:
        if mode == "auto" and not http_enabled:
            return fallback_func()
        return {
            "error": "E_HTTP_COMPAT_UNSUPPORTED",
            "tool": rpc_method,
            "backend": "python_compat",
            "message": (
                f"{rpc_method} 未注册 compat_route（backend=python_compat），"
                "fail-closed 拒绝执行，不回退本地 SQLite"
            ),
        }

    rpc_client = _get_rpc_client_for_route()
    try:
        return rpc_client.call(rpc_method, params)
    except DaemonRemoteError:
        # worker 结构化错误（E_COMPAT_*，含 UNAVAILABLE/TIMEOUT 等 retryable
        # 基础设施错误）：HTTP/enterprise 原样透传；纯 auto 模式降级本地读
        if mode == "auto" and not http_enabled:
            return fallback_func()
        raise
    except Exception as exc:
        if http_enabled or is_daemon_required():
            raise DaemonUnavailableError(
                f"{'HTTP' if http_enabled else 'enterprise'} 模式下 "
                f"compat worker 调用失败: {exc}"
            ) from exc
        return fallback_func()


# ---------------------------------------------------------------------------
# T03 收敛：统一薄壳路由（route_rpc）
# ---------------------------------------------------------------------------
# 设计契约（cw-rust-client-convergence-design.md §8）：
# - 所有 MCP 工具函数 = 「参数 → route_rpc() → 结果原样返回」，无本地业务逻辑；
# - fail-closed：daemon 不可用抛 DaemonUnavailableError(E_HTTP_DAEMON_UNAVAILABLE)，
#   绝不回退本地 SQLite/CodeGraphDB；
# - local/legacy 模式仅 CW_TEST_MODE=1 下可用（E_MODE_DEPRECATED）；
# - workspace_instance_id 只透传 workspace.register 权威值（_ensure_remote_snapshot），
#   禁止 cwd/本地派生兜底。

# 无需 workspace 上下文的 RPC（设计 §8.2 例外清单）
_NO_WORKSPACE_METHODS = frozenset({
    "ping",
    "health",
    "schema.version",
    "workspace.list",
    # Role Worker lifecycle status is owner-scoped by the daemon transport
    # peer and does not inspect a workspace snapshot.  Injecting a workspace
    # here would make this credential-free read depend on snapshot publication
    # and could report a transport problem as a workspace problem.
    "role_worker.status",
})


def _is_test_mode() -> bool:
    """CW_TEST_MODE=1 时允许 local/legacy（测试专用，Q3 决策）。"""
    return os.environ.get("CW_TEST_MODE", "") == "1"


# F-013（2026-09-25）：workspace.activate / workspace.remove 的 id-or-name
# 参数路由断裂修复。MCP 工具 set_active_workspace / delete_workspace 与 CLI
# 只传 workspace_id_or_name（数字字符串或名称），而 Rust handler
# （handle_workspace_activate / handle_workspace_remove）只 require_str_param
# "workspace_instance_id"。若 route_rpc 不在此解析，下方 workspace 权威注入
# 会把当前活动 workspace 的 instance 顶替上去——delete_workspace("1711")
# 曾因此静默归档主仓 1193（instance 4baea3ff12c2ea5c）而非目标隔离 ws。
_WS_ID_OR_NAME_METHODS = frozenset({"workspace.activate", "workspace.remove"})


def _resolve_ws_instance_by_id_or_name(rpc_client, id_or_name: str) -> str:
    """把 workspace_id_or_name（数字字符串或名称）解析为 workspace_instance_id。

    匹配规则与 list_workspaces 的兼容映射一致（name 用
    os.path.basename(client_view_root) 兜底，daemon_workspaces 无 name 列）：
    - 纯数字 → 精确匹配 daemon_workspaces.workspace_id；
    - 非数字 → os.path.basename(client_view_root) 精确匹配（名称语义）。

    Args:
        rpc_client: 已就绪的 RPC client（HTTP 或 pipe），用于调 workspace.list。
        id_or_name: CLI/MCP 传入的 workspace_id_or_name 原始值。

    Returns:
        目标 workspace 的 workspace_instance_id。

    Raises:
        DaemonRemoteError: workspace_not_found（fail-closed，绝不回退活动 ws）。
    """
    from callwarden.server.daemon_protocol import DaemonRemoteError

    raw = rpc_client.call("workspace.list", {})
    rows = raw if isinstance(raw, list) else []
    key = str(id_or_name).strip()
    for row in rows:
        if not isinstance(row, dict):
            continue
        instance = row.get("workspace_instance_id")
        if not instance:
            continue
        if key.isdigit():
            if str(row.get("workspace_id")) == key:
                return str(instance)
        else:
            client_root = row.get("client_view_root") or ""
            if client_root and os.path.basename(client_root) == key:
                return str(instance)
    raise DaemonRemoteError(
        "workspace_not_found",
        f"workspace_id_or_name={id_or_name!r} 无法解析为已注册 workspace"
        "（fail-closed：禁止回退当前活动 workspace）",
    )


def _normalize_write_rel_path(path: str) -> str:
    """与 daemon fs_handlers::normalize_write_rel_path 同构。"""
    rel = (path or "").replace("\\", "/").strip()
    if not rel or rel.startswith("/") or ":" in rel:
        raise DaemonRemoteError(
            "E_WRITE_PATH_FORBIDDEN", f"write path 必须是 workspace 相对路径: {path!r}"
        )
    parts: List[str] = []
    for segment in rel.split("/"):
        if segment in ("", "."):
            continue
        if segment == "..":
            raise DaemonRemoteError(
                "E_WRITE_PATH_FORBIDDEN", f"write path 禁止上跳穿越: {path!r}"
            )
        parts.append(segment)
    if not parts:
        raise DaemonRemoteError(
            "E_WRITE_PATH_FORBIDDEN", f"write path 无有效路径段: {path!r}"
        )
    return "/".join(parts)


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _atomic_write_bytes(target: str, data: bytes) -> None:
    """本机原子替换：同目录临时文件 + fsync + os.replace。"""
    parent = os.path.dirname(target) or "."
    mode: Optional[int] = None
    try:
        mode = os.stat(target).st_mode & 0o7777
    except OSError:
        mode = None
    fd, tmp_path = tempfile.mkstemp(prefix=".cw-write-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        os.replace(tmp_path, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def governed_write_file(
    file_path: str,
    content: Any,
    task_id: str,
    step_id: str,
    workspace_instance_id: str = "",
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    """S3（ADR-002 §8.2）客户端治理写：grant → 本机原子写 → commit。

    - 仅 remote profile 启用；loopback 直接 fail-closed，杜绝本地旁路；
    - commit 失败不回滚已落地的本地文件，错误信息显式标注且不自动重试；
    - CLI 与 MCP 共用本函数，语义唯一。
    """
    client = HttpDaemonRpcClient.get_instance()
    if not client.is_remote_profile():
        raise DaemonRemoteError(
            "E_WRITE_REMOTE_ONLY",
            "治理写仅支持 bind_scope=remote 的 daemon；loopback 不允许旁路写",
        )
    root = getattr(client, "_project_root", None) or os.getcwd()
    root = os.path.realpath(root)
    rel = _normalize_write_rel_path(file_path)
    target = os.path.realpath(os.path.join(root, rel))
    if os.path.commonpath([root, target]) != root:
        raise DaemonRemoteError(
            "E_WRITE_PATH_FORBIDDEN", f"write path 越出 workspace 根: {file_path!r}"
        )

    data = content if isinstance(content, bytes) else str(content).encode("utf-8")
    try:
        with open(target, "rb") as handle:
            base_hash = _sha256_hex(handle.read())
    except FileNotFoundError:
        base_hash = ""
    except OSError as exc:
        raise DaemonRemoteError(
            "E_WRITE_LOCAL_IO", f"读取本地文件失败（{rel}）: {exc}"
        ) from exc

    ws_id = workspace_instance_id or client._ensure_remote_snapshot(None) or ""
    grant_request_id = request_id or str(uuid.uuid4())
    grant = client.call(
        "write.grant",
        {
            "workspace_instance_id": ws_id,
            "task_id": task_id,
            "step_id": step_id,
            "path": rel,
            "base_hash": base_hash,
            "request_id": grant_request_id,
        },
        request_id=grant_request_id,
    )
    grant_id = grant["grant_id"]
    new_hash = _sha256_hex(data)

    try:
        _atomic_write_bytes(target, data)
    except OSError as exc:
        raise DaemonRemoteError(
            "E_WRITE_LOCAL_IO",
            f"本地原子写失败（{rel}, grant={grant_id}）: {exc}；grant 未 commit，将在过期后释放",
        ) from exc

    commit_request_id = str(uuid.uuid4())
    try:
        commit = client.call(
            "write.commit",
            {
                "workspace_instance_id": ws_id,
                "grant_id": grant_id,
                "path": rel,
                "new_hash": new_hash,
                "content": data.decode("utf-8", errors="strict"),
                "request_id": commit_request_id,
            },
            request_id=commit_request_id,
        )
    except DaemonRemoteError as exc:
        raise DaemonRemoteError(
            exc.code,
            "本地文件已更新但 write.commit 失败，未自动回滚且不会静默重试；"
            f"path={rel} new_hash={new_hash} grant_id={grant_id}；daemon: {exc.message}",
        ) from exc
    except Exception as exc:  # DaemonUnavailableError 等连接类失败
        raise DaemonRemoteError(
            "E_WRITE_COMMIT_UNCONFIRMED",
            "本地文件已更新但 write.commit 结果未确认，未自动回滚且不会静默重试；"
            f"path={rel} new_hash={new_hash} grant_id={grant_id}；{exc}",
        ) from exc

    return {
        "ok": True,
        "path": rel,
        "grant_id": grant_id,
        "base_hash": base_hash,
        "new_hash": new_hash,
        "bytes_written": len(data),
        "commit": commit,
    }


def route_rpc(rpc_method: str, params: dict, op_class: str = "READ_ONLY") -> Any:
    """统一薄壳路由（T03 收敛）：参数 → daemon RPC → 结果原样返回。

    Args:
        rpc_method: 矩阵中的 daemon RPC method（python_compat 时=工具名）。
        params: MCP 工具参数（原样透传 + 权威 workspace 注入）。
        op_class: READ_ONLY / PROTECTED_MUTATION / GOVERNANCE_WRITE。
            写操作自动附加幂等 request_id（daemon 侧 mutation dedup）。

    Returns:
        daemon 响应的 result 字段（原样返回，无映射/无本地逻辑）。

    Raises:
        DaemonUnavailableError: daemon 不可用/模式废弃（fail-closed）。
        DaemonRemoteError: daemon 结构化业务错误（原样透传）。
    """
    from callwarden.server.daemon_protocol import DaemonRemoteError

    mode = get_daemon_mode()
    if mode in ("local", "legacy") and not _is_test_mode():
        raise DaemonUnavailableError(
            f"{E_MODE_DEPRECATED}: local/legacy 模式仅 CW_TEST_MODE=1 下可用，"
            "生产请使用 daemon（CW_DAEMON_TRANSPORT=http/auto）",
            code=E_MODE_DEPRECATED,
        )

    params = dict(params or {})
    http_enabled = is_http_transport_enabled()

    # 写操作附加幂等 request_id（daemon mutation dedup，重试命中 Replay）
    if op_class in ("PROTECTED_MUTATION", "GOVERNANCE_WRITE"):
        if "request_id" not in params:
            import uuid
            params["request_id"] = f"req-{uuid.uuid4().hex[:12]}"

    # F-013（2026-09-25）：workspace.activate / workspace.remove 的
    # id-or-name → instance 解析必须在权威注入之前完成，否则注入块会把
    # 当前活动 workspace 的 instance 顶替为目标（delete_workspace("1711")
    # 曾因此静默归档主仓 1193）。解析失败 fail-closed，绝不回退活动 ws。
    if (
        rpc_method in _WS_ID_OR_NAME_METHODS
        and "workspace_id_or_name" in params
        and "workspace_instance_id" not in params
    ):
        from callwarden.server.daemon_protocol import DaemonRemoteError

        resolver_client = _get_rpc_client_for_route()
        try:
            params["workspace_instance_id"] = _resolve_ws_instance_by_id_or_name(
                resolver_client, params["workspace_id_or_name"]
            )
        except DaemonRemoteError:
            # workspace_not_found 透传：调用方应看到真实的解析失败
            raise
        except Exception as exc:
            raise DaemonUnavailableError(
                f"{E_HTTP_DAEMON_UNAVAILABLE}: 解析 workspace_id_or_name 失败 "
                f"({rpc_method}): {exc}（fail-closed，不回退活动 workspace）"
            ) from exc
        # 已定位目标 workspace，移除 daemon 不认的兼容键
        params.pop("workspace_id_or_name", None)

    # workspace 权威注入（HTTP 模式经 _ensure_remote_snapshot，非 HTTP 沿用 _inject_workspace_id）
    if rpc_method not in _NO_WORKSPACE_METHODS:
        if http_enabled:
            client = HttpDaemonRpcClient.get_instance()
            # route_rpc 是 MCP/CLI 的共同入口。兜底语义：调用方未显式配置
            # workspace 时，按调用进程 cwd 绑定（用户裁决：读本地环境每次
            # 传过去，禁止硬编码 callwarden 仓库根——硬编码会使 CLI 跨项目
            # 查询静默落到 callwarden 自身 workspace，报 snapshot_not_ready）。
            # MCP 宿主场景由 create_mcp_server 提前 configure，不触发本兜底。
            if client._project_root is None:
                client.configure_workspace(os.getcwd())

            # workspace.status 与原生 query 面需要一个真实 snapshot 才能形成
            # 可核验的 authority projection。DB 路径由 daemon 返回，随后
            # _ensure_remote_snapshot 以 workspace.register 的返回值发布，避免
            # 本地推导 workspace ID 或打开本地 SQLite。
            #
            # FIX(全量测试遗留项1,2026-09-30):此前只有 workspace.status 会取
            # 权威 db_path 触发 snapshot.publish;而所有 query.* RPC(query.stats /
            # query.symbol / query.callers ...)在 daemon 侧同样经 get_store /
            # open_query_connection 依赖已发布 snapshot,却走 snapshot_db_path=None
            # 分支只 register 不 publish,导致 build_graph/refresh 后 `cw stats`
            # 等恒报 snapshot_not_ready。修复:对 query.* 面同样取权威 db_path
            # 触发 publish(与 task.report 的正确样板一致)。db_path 由 daemon
            # 返回(~/.callwarden/callwarden.db,与 build_graph 写入同一主库),
            # 薄客户端不本地推导。
            snapshot_db_path = None
            # FIX(全量测试深度轮,2026-09-30):workspace-scoped mutation 面
            # (admin.* / gate.* / rule.* / edit.* / guardrail.* / summary.*)在
            # daemon 侧同样 require_str_param(workspace_instance_id)(经
            # open_codegraph_db_write 做 ACL),但此前只有 workspace.status / query.*
            # 走 snapshot publish 注入 instance,task.*/lease.* 走 _inject_workspace_id
            # 注入数字 id,这批 mutation 落入注入 gap → daemon 报 "缺少字段:
            # workspace_instance_id"。此处把它们纳入与 query.* 相同的 db_path+publish
            # 注入路径,确保 _ensure_remote_snapshot 产生有效 workspace_instance_id。
            _WS_SCOPED_MUTATION_PREFIXES = (
                "admin.", "gate.", "rule.", "edit.", "guardrail.", "summary.",
            )
            needs_snapshot = (
                rpc_method == "workspace.status"
                or rpc_method.startswith("query.")
                or rpc_method.startswith(_WS_SCOPED_MUTATION_PREFIXES)
            )
            # S2：remote profile 下不解析 daemon db_path（快照改由
            # snapshot.upload 客户端本机构建上传），上方的 snapshot_db_path
            # 保持 None，_ensure_remote_snapshot 只注册不发布。
            if needs_snapshot and not client.is_remote_profile():
                # 需 snapshot 的 authority projection / query 面 / workspace-scoped
                # mutation 必须能解析权威 workspace。DB 路径由 daemon 提供,
                # 避免薄客户端读取本地 SQLite。
                # S2（ADR-002 §16.1）：remote profile 下快照入口唯一为
                # snapshot.upload（客户端本机构建上传），不再向 daemon 取
                # db_path 做 publish（db_path 会被远端拒绝）；query.* 的
                # 快照由 build_graph 上传面提供（S2-03 链路）。
                db_result = client.call("mcp.common.get_db_path_for_daemon", {})
                if not isinstance(db_result, dict) or not db_result.get("db_path"):
                    raise DaemonUnavailableError(
                        "daemon 未返回可用于 snapshot.publish 的权威 db_path"
                    )
                snapshot_db_path = db_result["db_path"]
            # ADJ-RP10-01 fix: task-scoped authority requests (non-blank
            # task_id / superseded_id) resolve their numeric workspace through
            # the immutable task_workspace_bindings on the daemon side.
            # Injecting workspace_instance_id / workspace_root here puts
            # unknown fields on the wire (spec 4.1-3) and the daemon
            # parse_request fails closed (e.g. task.prompt.compile ->
            # E_TASK_PROMPT_TASK_ID_REQUIRED).  Skip the whole workspace
            # injection block for those requests.
            is_task_scoped = _is_task_scoped_authority_request(params)
            # FIX(全量测试遗留项2,2026-09-30):workspace.activate / workspace.remove
            # 已在 L4150 由 workspace_id_or_name 解析出明确的 workspace_instance_id,
            # 不能再走 _ensure_remote_snapshot 的 CWD 兜底 register —— daemon 的
            # workspace.register 会隐式把注册的 workspace 设为 active,导致
            # `cw workspace set 1714` 后紧接的 get_active_workspace()(workspace.status)
            # 又把 CWD(callwarden)注册并激活,顶替掉刚 set 的目标 1714,表现为
            # "set 数字 ID 却激活了 CWD workspace"。这类目标已明确的管理命令跳过
            # 隐式 register/snapshot(它们不查 snapshot,只需权威 workspace_instance_id)。
            target_already_bound = (
                rpc_method in _WS_ID_OR_NAME_METHODS
                and "workspace_instance_id" in params
            )
            # FIX(全量测试深度轮,2026-09-30):workspace-scoped mutation 前缀
            # (admin./gate./rule./edit./guardrail./summary.)在 daemon 侧按
            # workspace_instance_id 解析权威 workspace(open_codegraph_db_write),
            # 与 task binding 无关。即使这类请求带 task_id(如 gate.run_check 的
            # task_id/step_id/changed_files),也必须注入 workspace_instance_id,
            # 否则被 is_task_scoped 判定吞掉 → daemon 报 "缺少字段:
            # workspace_instance_id"。故这批前缀强制注入 instance,不受 task-scoped
            # 判断阻挡。
            is_ws_scoped_mutation = rpc_method.startswith(_WS_SCOPED_MUTATION_PREFIXES)
            if (not is_task_scoped or is_ws_scoped_mutation) and not target_already_bound:
                ws_id = client._ensure_remote_snapshot(snapshot_db_path)
                if ws_id is not None and "workspace_instance_id" not in params:
                    params["workspace_instance_id"] = ws_id
                # compat 面（Python worker）需要 workspace_root 解析权威数字 id：
                # worker 的 _bind_readonly_db 按 root_path 查 workspaces 表（2026-09-05，
                # 修 get_impact 等 compat 方法 "没有 active workspace" fail-closed）。
                if client._project_root:
                    params.setdefault("workspace_root", client._project_root)
            # task-scoped methods already have the stronger authority source:
            # task_id -> immutable task_workspace_bindings.  Never replace it
            # with the legacy active workspace numeric ID: in a multi-project
            # daemon that active row can belong to another project.  The Rust
            # handler resolves an omitted numeric ID from the binding and
            # rejects an explicitly supplied mismatch before mutation.
            #
            # Workspace-scoped methods (including task.create and lease event
            # listing without task_id) still require the existing explicit
            # numeric workspace injection path.
            if (
                rpc_method.startswith(("task.", "lease."))
                and not is_task_scoped
            ):
                params = _inject_workspace_id(params)
        else:
            if "workspace_id" not in params and "workspace_instance_id" not in params:
                params = _inject_workspace_id(params)

    rpc_client = _get_rpc_client_for_route()
    try:
        call = (
            rpc_client.call_with_autostart
            if hasattr(rpc_client, "call_with_autostart")
            else rpc_client.call
        )
        return call(rpc_method, params)
    except DaemonRemoteError:
        # 结构化业务错误原样透传（不伪装成连接失败）
        raise
    except Exception as exc:
        raise DaemonUnavailableError(
            f"{E_HTTP_DAEMON_UNAVAILABLE}: daemon RPC 调用失败 "
            f"({rpc_method}): {exc}（fail-closed，不回退本地 SQLite）"
        ) from exc


@contextmanager
def executor_lease(rpc_client, task_id: str, identity: Any = None,
                   role: str = "implementer", ttl_seconds: float = 3600.0):
    """ec89dbe4 S1：executor lease 必须 finally 释放的 guard。

    runner/executor 在任务执行开始 acquire 本角色 implementer lease，无论
    normal 返回、异常、取消还是超时退出，均通过本 context manager 的
    finally 幂等释放（覆盖 S1 check 的四类终态）。release 本身幂等，释放
    失败不掩盖业务异常——fencing 由 daemon 权威强制（Req 11.2-11.9）。

    S0 记录：当前仓库不存在主动 acquire 的 executor/orchestrator runner
    （cli/main.py 与 server/ 均无对应 lifecycle entrypoint），本 guard 供
    future runner 与 CLI implementer 写路径使用；不得在缺少对应 runner 时
    扩大范围引入全新 runner。rpc_client 仅需暴露 lease_acquire / lease_release
    （raw token 仅返回一次）。
    """
    acquired = rpc_client.lease_acquire(
        task_id, role, identity=identity, ttl_seconds=ttl_seconds)
    try:
        yield acquired
    finally:
        try:
            rpc_client.lease_release(
                task_id, role, acquired["token"], identity=identity)
        except Exception:
            # release 幂等；释放失败不得掩盖业务异常（fencing 由 daemon 兜底）。
            pass
