"""Enterprise daemon UDS server 与 workspace/snapshot RPC 路由。

Linux 部署时只信任 ``SO_PEERCRED`` 提供的 UID/GID/PID，客户端请求体中的
身份字段不会参与授权。Python 负责 IPC/ACL 编排，查询数据由 Rust
GraphSnapshot 提供。
"""

from __future__ import annotations

import concurrent.futures
import os
import socket
import sqlite3
import stat
import struct
import sys
import threading
from contextlib import closing
from typing import Optional, List, Dict, Any

from callwarden.db.db_daemon import (
    init_daemon_schema,
    register_workspace,
    list_workspaces,
    get_workspace_status,
    update_workspace_status,
)
from callwarden.config import DAEMON_REGISTRY_DB
from callwarden.server.daemon_protocol import (
    DEFAULT_MAX_MESSAGE_BYTES,
    ProtocolError,
    recv_message_with_fds,
    send_message,
)
from callwarden.server.snapshot_manager import (
    SnapshotManagerService,
    get_snapshot_service,
)


class DaemonRpcError(RuntimeError):
    """可安全返回给客户端的 RPC 错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _current_uid() -> int:
    return os.getuid() if hasattr(os, "getuid") else 0


def get_peer_credentials(conn: socket.socket) -> Dict[str, int]:
    """从已连接 UDS 获取内核认证的 peer credential。"""
    if hasattr(socket, "SO_PEERCRED"):
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                              struct.calcsize("3i"))
        pid, uid, gid = struct.unpack("3i", raw)
        return {"pid": pid, "uid": uid, "gid": gid}
    # 非 Linux 仅用于开发测试；企业部署门禁要求 SO_PEERCRED。
    return {"pid": os.getpid(), "uid": _current_uid(), "gid": 0}


def get_registry_conn() -> sqlite3.Connection:
    """获取 workspace registry DB 连接。"""
    conn = sqlite3.connect(DAEMON_REGISTRY_DB)
    conn.row_factory = sqlite3.Row
    init_daemon_schema(conn)
    return conn


def api_register_workspace(owner_uid: int,
                           client_view_root: str,
                           host_real_root: str,
                           git_remote_url: str = "",
                           git_head_commit_sha: str = "",
                           toolchain_fingerprint: str = "") -> Dict[str, Any]:
    """API: 注册 workspace。"""
    conn = get_registry_conn()
    try:
        return register_workspace(
            conn, owner_uid, client_view_root, host_real_root,
            git_remote_url, git_head_commit_sha, toolchain_fingerprint
        )
    finally:
        conn.close()


def api_list_workspaces(owner_uid: Optional[int] = None) -> List[Dict[str, Any]]:
    """API: 列出 workspace。"""
    conn = get_registry_conn()
    try:
        return list_workspaces(conn, owner_uid)
    finally:
        conn.close()


def api_get_workspace_status(workspace_instance_id: str) -> Optional[Dict[str, Any]]:
    """API: 获取 workspace 状态。"""
    conn = get_registry_conn()
    try:
        return get_workspace_status(conn, workspace_instance_id)
    finally:
        conn.close()


def api_update_workspace_status(workspace_instance_id: str, status: str):
    """API: 更新 workspace 状态。"""
    conn = get_registry_conn()
    try:
        update_workspace_status(conn, workspace_instance_id, status)
    finally:
        conn.close()


class EnterpriseDaemonService:
    """带 UID ACL 的 workspace registry + shared snapshot RPC 服务。"""

    def __init__(self, registry_db: str = DAEMON_REGISTRY_DB,
                 snapshot_service: Optional[SnapshotManagerService] = None):
        self.registry_db = os.path.abspath(registry_db)
        self.snapshot_service = snapshot_service or get_snapshot_service()
        os.makedirs(os.path.dirname(self.registry_db), exist_ok=True)
        with closing(self._registry_conn()):
            pass

    def _registry_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.registry_db, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        init_daemon_schema(conn)
        return conn

    def _owned_workspace(self, peer_uid: int,
                         workspace_id: str) -> Dict[str, Any]:
        with closing(self._registry_conn()) as conn:
            workspace = get_workspace_status(conn, workspace_id)
        if workspace is None:
            raise DaemonRpcError("workspace_not_found", workspace_id)
        if int(workspace["owner_uid"]) != peer_uid:
            raise DaemonRpcError("workspace_forbidden", "workspace 不属于当前 UID")
        if workspace.get("status") == "archived":
            raise DaemonRpcError("workspace_archived", workspace_id)
        return workspace

    @staticmethod
    def _validate_owned_path(path: str, peer_uid: int,
                             require_file: bool = False) -> str:
        real_path = os.path.realpath(os.path.abspath(path))
        if require_file and not os.path.isfile(real_path):
            raise DaemonRpcError("path_not_found", real_path)
        if not require_file and not os.path.isdir(real_path):
            raise DaemonRpcError("path_not_found", real_path)
        if hasattr(os, "getuid") and peer_uid != 0:
            owner_uid = os.stat(real_path).st_uid
            if owner_uid != peer_uid:
                raise DaemonRpcError(
                    "path_forbidden",
                    f"路径 owner_uid={owner_uid}，peer_uid={peer_uid}",
                )
        return real_path

    def dispatch(self, peer: Dict[str, int], method: str,
                 params: Dict[str, Any], received_fds: Optional[List[int]] = None) -> Any:
        """执行单个 RPC；身份始终取自 peer credential。"""
        uid = int(peer["uid"])
        if method == "ping":
            return {"status": "ok", "peer_uid": uid, "pid": os.getpid()}

        if method == "workspace.register":
            client_root = str(params.get("client_view_root") or "")
            if not client_root:
                raise DaemonRpcError("invalid_params", "缺少 client_view_root")
            host_root = self._validate_owned_path(client_root, uid)
            with closing(self._registry_conn()) as conn:
                return register_workspace(
                    conn,
                    owner_uid=uid,
                    client_view_root=client_root,
                    host_real_root=host_root,
                    git_remote_url=str(params.get("git_remote_url") or ""),
                    git_head_commit_sha=str(params.get("git_head_commit_sha") or ""),
                    toolchain_fingerprint=str(params.get("toolchain_fingerprint") or ""),
                )

        if method == "workspace.list":
            with closing(self._registry_conn()) as conn:
                return list_workspaces(conn, owner_uid=uid)

        workspace_id = str(params.get("workspace_instance_id") or "")
        if not workspace_id:
            raise DaemonRpcError("invalid_params", "缺少 workspace_instance_id")
        workspace = self._owned_workspace(uid, workspace_id)

        if method == "workspace.status":
            result = dict(workspace)
            result["snapshot"] = self.snapshot_service.get_snapshot_stats(workspace_id)
            return result

        if method in ("snapshot.publish", "workspace.refresh"):
            received_fds = received_fds or []
            if received_fds:
                db_path = self._validate_snapshot_fd(received_fds[0], uid)
            else:
                db_path = self._validate_owned_path(
                    str(params.get("db_path") or ""), uid, require_file=True
                )
                # 本地兼容路径：immutable 读取前确保 WAL 数据可见。
                with sqlite3.connect(db_path, timeout=5.0) as db_conn:
                    db_conn.execute("PRAGMA busy_timeout=5000")
                    db_conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            result = self.snapshot_service.publish_snapshot(
                workspace_instance_id=workspace_id,
                db_path=db_path,
                build_context_hash=str(params.get("build_context_hash") or ""),
                snapshot_id=workspace.get("snapshot_id"),
            )
            if result is None:
                raise DaemonRpcError("snapshot_unavailable", "Rust snapshot 后端不可用")
            return result

        if not self.snapshot_service.ensure_workspace(workspace_id):
            raise DaemonRpcError("snapshot_not_ready", workspace_id)

        if method == "query.stats":
            return self.snapshot_service.query_stats(workspace_id)
        if method == "query.symbol":
            return self.snapshot_service.query_symbol(
                workspace_id, str(params.get("qualified_name") or "")
            )
        if method == "query.search":
            return self.snapshot_service.search_symbols(
                workspace_id,
                str(params.get("query") or ""),
                params.get("kind"),
                int(params.get("limit", 20)),
            )
        if method == "query.callers":
            return self.snapshot_service.query_callers(
                workspace_id,
                str(params.get("callee_name") or ""),
                params.get("qualified_name"),
            )
        if method == "query.callees":
            return self.snapshot_service.query_callees(
                workspace_id,
                str(params.get("caller_name") or ""),
                params.get("qualified_name"),
            )
        raise DaemonRpcError("method_not_found", method)

    @staticmethod
    def _validate_snapshot_fd(fd: int, peer_uid: int) -> str:
        """校验用户传入的只读常规文件 FD，并返回 Linux proc 路径。"""
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise DaemonRpcError("invalid_snapshot_fd", "snapshot FD 不是常规文件")
        if info.st_uid != peer_uid:
            raise DaemonRpcError(
                "snapshot_fd_forbidden",
                f"FD owner_uid={info.st_uid}，peer_uid={peer_uid}",
            )
        max_bytes = int(os.environ.get("CW_MAX_SNAPSHOT_DB_BYTES", str(64 << 30)))
        if info.st_size <= 0 or info.st_size > max_bytes:
            raise DaemonRpcError(
                "snapshot_too_large", f"snapshot bytes={info.st_size}, max={max_bytes}"
            )
        if sys.platform == "linux":
            import fcntl
            if fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY:
                raise DaemonRpcError("invalid_snapshot_fd", "snapshot FD 必须只读")
            return f"/proc/self/fd/{fd}"
        raise DaemonRpcError("unsupported_platform", "FD snapshot 仅支持 Linux")


class EnterpriseDaemonServer:
    """有界线程池 UDS server；每个连接处理一个请求。"""

    def __init__(self, socket_path: str, service: EnterpriseDaemonService,
                 max_workers: int = 16, max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
                 request_timeout: float = 30.0, socket_mode: int = 0o660):
        self.socket_path = os.path.abspath(socket_path)
        self.service = service
        self.max_workers = max_workers
        self.max_message_bytes = max_message_bytes
        self.request_timeout = request_timeout
        self.socket_mode = socket_mode
        self.ready = threading.Event()
        self._stop = threading.Event()
        self._listener: Optional[socket.socket] = None

    def _prepare_socket_path(self) -> None:
        os.makedirs(os.path.dirname(self.socket_path), exist_ok=True)
        if os.path.lexists(self.socket_path):
            mode = os.lstat(self.socket_path).st_mode
            if not stat.S_ISSOCK(mode):
                raise RuntimeError(f"拒绝覆盖非 socket 路径: {self.socket_path}")
            os.unlink(self.socket_path)

    def serve_forever(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("当前平台不支持 Unix domain socket")
        self._prepare_socket_path()
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener = listener
        listener.bind(self.socket_path)
        os.chmod(self.socket_path, self.socket_mode)
        listener.listen(self.max_workers)
        listener.settimeout(0.5)
        self.ready.set()
        try:
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="cw-daemon"
            ) as pool:
                while not self._stop.is_set():
                    try:
                        conn, _ = listener.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        if self._stop.is_set():
                            break
                        raise
                    pool.submit(self._handle_connection, conn)
        finally:
            listener.close()
            self._listener = None
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)

    def _handle_connection(self, conn: socket.socket) -> None:
        request_id = None
        received_fds: List[int] = []
        with conn:
            conn.settimeout(self.request_timeout)
            try:
                peer = get_peer_credentials(conn)
                request, received_fds = recv_message_with_fds(
                    conn, self.max_message_bytes
                )
                request_id = request.get("id")
                method = request.get("method")
                params = request.get("params", {})
                if not isinstance(method, str) or not isinstance(params, dict):
                    raise DaemonRpcError("invalid_request", "method/params 类型错误")
                result = self.service.dispatch(peer, method, params, received_fds)
                response = {"id": request_id, "ok": True, "result": result}
            except DaemonRpcError as exc:
                response = {
                    "id": request_id,
                    "ok": False,
                    "error": {"code": exc.code, "message": exc.message},
                }
            except (ProtocolError, socket.timeout) as exc:
                response = {
                    "id": request_id,
                    "ok": False,
                    "error": {"code": "protocol_error", "message": str(exc)},
                }
            except Exception as exc:
                response = {
                    "id": request_id,
                    "ok": False,
                    "error": {"code": "internal_error", "message": str(exc)},
                }
            try:
                send_message(conn, response, self.max_message_bytes)
            except (OSError, ProtocolError):
                pass
            finally:
                for fd in received_fds:
                    os.close(fd)

    def shutdown(self) -> None:
        self._stop.set()
        listener = self._listener
        if listener is not None:
            try:
                listener.close()
            except OSError:
                pass
