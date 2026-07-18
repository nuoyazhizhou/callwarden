"""Enterprise daemon UDS server 与 workspace/snapshot RPC 路由。

Linux 部署时只信任 ``SO_PEERCRED`` 提供的 UID/GID/PID，客户端请求体中的
身份字段不会参与授权。Python 负责 IPC/ACL 编排，查询数据由 Rust
GraphSnapshot 提供。
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import socket
import sqlite3
import stat
import struct
import sys
import threading
import time
from contextlib import closing
from typing import Optional, List, Dict, Any

from callwarden.db.db_daemon import (
    init_daemon_schema,
    register_workspace,
    list_workspaces,
    get_workspace_status,
    update_workspace_status,
    register_mount_mapping,
    list_mount_mappings,
    delete_mount_mapping,
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

logger = logging.getLogger(__name__)


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
    """带 UID ACL 的 workspace registry + shared snapshot RPC 服务。

    T-1783952125417-7a09：接通 CAS/Replicator/StagingLog，
    refresh 管道走 daemon_handle_refresh → CAS → StagingLog → Replicator → SnapshotManager。
    """

    def __init__(self, registry_db: str = DAEMON_REGISTRY_DB,
                 snapshot_service: Optional[SnapshotManagerService] = None,
                 data_root: str = ""):
        self.registry_db = os.path.abspath(registry_db)
        self.snapshot_service = snapshot_service or get_snapshot_service()
        self._data_root = data_root or os.path.join(
            os.path.dirname(self.registry_db), "enterprise"
        )
        os.makedirs(self._data_root, exist_ok=True)
        os.makedirs(os.path.dirname(self.registry_db), exist_ok=True)
        # workspace_id → {cas_conn, staging_log, replicator, ws_conn}
        self._workspace_resources: Dict[str, Dict] = {}
        self._resources_lock = threading.Lock()
        import time as _time
        self._start_time = _time.time()
        with closing(self._registry_conn()):
            pass

    def _get_workspace_resources(self, workspace_id: str) -> Dict:
        """懒初始化 per-workspace 的 CAS conn / StagingLog / Replicator / ws_conn。"""
        with self._resources_lock:
            if workspace_id in self._workspace_resources:
                return self._workspace_resources[workspace_id]

            ws_dir = os.path.join(self._data_root, workspace_id)
            os.makedirs(ws_dir, exist_ok=True)

            # CAS 数据库
            from callwarden.db.db_cas import init_cas_schema
            cas_db_path = os.path.join(ws_dir, "cas.db")
            cas_conn = sqlite3.connect(cas_db_path, timeout=5.0)
            cas_conn.row_factory = sqlite3.Row
            cas_conn.execute("PRAGMA busy_timeout=5000")
            cas_conn.execute("PRAGMA journal_mode=WAL")
            init_cas_schema(cas_conn)

            # Workspace session 数据库
            from callwarden.server.replicator import init_session_schema
            ws_db_path = os.path.join(ws_dir, "workspace.db")
            ws_conn = sqlite3.connect(ws_db_path, timeout=5.0)
            ws_conn.row_factory = sqlite3.Row
            ws_conn.execute("PRAGMA busy_timeout=5000")
            ws_conn.execute("PRAGMA journal_mode=WAL")
            init_session_schema(ws_conn)

            # StagingLog
            from callwarden.server.staging_log import StagingLog
            staging_log_path = os.path.join(ws_dir, "staging.log")
            staging_log = StagingLog(staging_log_path)

            # Replicator
            from callwarden.server.replicator import Replicator
            replicator = Replicator(staging_log, self.snapshot_service)

            resources = {
                "cas_conn": cas_conn,
                "ws_conn": ws_conn,
                "staging_log": staging_log,
                "replicator": replicator,
            }
            self._workspace_resources[workspace_id] = resources
            return resources

    def recover_all_workspaces(self):
        """daemon 启动时扫描所有 workspace 的 pending staging entries 并恢复。

        规范：enterprise-daemon-full-e2e-followup.md §4.2
        单 workspace 单写，跨 workspace 可并行。
        """
        for ws_id in list(self._workspace_resources.keys()):
            try:
                res = self._workspace_resources[ws_id]
                pending = res["staging_log"].read_pending()
                ws_pending = [e for e in pending if e.workspace_id == ws_id]
                if ws_pending:
                    logger.info("recovering %d pending entries for ws=%s", len(ws_pending), ws_id)
                    res["replicator"].recover(ws_id)
            except Exception as e:
                logger.error("recovery failed for ws=%s: %s", ws_id, e)

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

        # workspace.connect：agent 连接握手，分配 session epoch
        if method == "workspace.connect":
            workspace_id = str(params.get("workspace_instance_id") or "")
            if not workspace_id:
                raise DaemonRpcError("invalid_params", "缺少 workspace_instance_id")
            self._owned_workspace(uid, workspace_id)
            session_id = str(params.get("agent_session_id") or "")
            if not session_id:
                raise DaemonRpcError("invalid_params", "缺少 agent_session_id")
            from callwarden.server.replicator import daemon_handle_connect
            res = self._get_workspace_resources(workspace_id)
            try:
                result = daemon_handle_connect(
                    peer_uid=uid,
                    workspace_id=int(workspace_id),
                    requested_session_id=session_id,
                    ws_conn=res["ws_conn"],
                )
                result["workspace_instance_id"] = workspace_id
                return result
            except Exception as e:
                raise DaemonRpcError("connect_failed", str(e))

        # ---- 全局方法（不需要 workspace_id）----

        if method == "health":
            """daemon 健康检查，返回运行状态"""
            import time as _time
            with closing(self._registry_conn()) as conn:
                ws_count = conn.execute(
                    "SELECT COUNT(*) FROM workspaces"
                ).fetchone()[0]
            return {
                "status": "ok",
                "pid": os.getpid(),
                "uptime_seconds": int(_time.time() - self._start_time),
                "workspace_count": ws_count,
                "registry_db": self.registry_db,
                "data_root": self._data_root,
            }

        if method == "schema.version":
            """查询 registry DB 的 schema 版本"""
            with closing(self._registry_conn()) as conn:
                row = conn.execute(
                    "SELECT value FROM schema_meta WHERE key = 'schema_version'"
                ).fetchone()
                version = int(row[0]) if row else 0
            return {"schema_version": version, "registry_db": self.registry_db}

        if method == "backup":
            """备份 registry DB 到指定路径"""
            output_path = str(params.get("output_path") or "")
            if not output_path:
                raise DaemonRpcError("invalid_params", "缺少 output_path")
            output_path = os.path.abspath(output_path)
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with closing(self._registry_conn()) as conn:
                conn.execute(f"VACUUM INTO '{output_path}'")
            return {"backup_path": output_path, "status": "ok"}

        if method == "restore":
            """从备份恢复 registry DB"""
            source_path = str(params.get("source_path") or "")
            if not source_path:
                raise DaemonRpcError("invalid_params", "缺少 source_path")
            source_path = os.path.abspath(source_path)
            if not os.path.isfile(source_path):
                raise DaemonRpcError("backup_not_found", source_path)
            # 关闭当前连接，替换文件，重新打开
            import shutil
            shutil.copy2(source_path, self.registry_db)
            return {"restored_from": source_path, "registry_db": self.registry_db}

        if method == "gc.snapshots":
            """GC 快照，保留最近 N 个，删除旧的"""
            keep_last = int(params.get("keep_last", 3))
            deleted = self.snapshot_service.gc_snapshots(keep_last)
            return {"deleted_count": deleted, "keep_last": keep_last}

        # ---- Mount Mapping 管理（G4 实现）----
        # mount.register / mount.list / mount.delete 不依赖 workspace_id，
        # 在下方 workspace_id 必填检查之前处理。
        if method == "mount.register":
            container_id = str(params.get("container_id") or "")
            container_path = str(params.get("container_path") or "")
            host_path = str(params.get("host_path") or "")
            if not container_id:
                raise DaemonRpcError("invalid_params", "缺少 container_id")
            if not container_path:
                raise DaemonRpcError("invalid_params", "缺少 container_path")
            if not host_path:
                raise DaemonRpcError("invalid_params", "缺少 host_path")
            mapping_type = str(params.get("mapping_type") or "bind")
            with closing(self._registry_conn()) as conn:
                try:
                    return register_mount_mapping(
                        conn,
                        container_id=container_id,
                        container_path=container_path,
                        host_path=host_path,
                        mapping_type=mapping_type,
                    )
                except ValueError as e:
                    raise DaemonRpcError("invalid_params", str(e))

        if method == "mount.list":
            container_id = params.get("container_id")
            if container_id is not None:
                container_id = str(container_id)
            with closing(self._registry_conn()) as conn:
                return list_mount_mappings(conn, container_id=container_id)

        if method == "mount.delete":
            container_id = str(params.get("container_id") or "")
            container_path = str(params.get("container_path") or "")
            if not container_id:
                raise DaemonRpcError("invalid_params", "缺少 container_id")
            if not container_path:
                raise DaemonRpcError("invalid_params", "缺少 container_path")
            with closing(self._registry_conn()) as conn:
                deleted = delete_mount_mapping(
                    conn,
                    container_id=container_id,
                    container_path=container_path,
                )
            return {"deleted": deleted}

        # gc.cas 需要 workspace_id，在下方处理

        workspace_id = str(params.get("workspace_instance_id") or "")
        if not workspace_id:
            raise DaemonRpcError("invalid_params", "缺少 workspace_instance_id")
        workspace = self._owned_workspace(uid, workspace_id)

        if method == "gc.cas":
            """GC CAS 存储，清理 grace_days 天前未引用的 content"""
            grace_days = int(params.get("grace_days", 7))
            resources = self._get_workspace_resources(workspace_id)
            cas_conn = resources["cas_conn"]
            cutoff = int(time.time()) - grace_days * 86400
            cursor = cas_conn.execute(
                "DELETE FROM cas_contents WHERE ref_count = 0 AND created_at < ?",
                (cutoff,),
            )
            cas_conn.commit()
            return {"deleted_count": cursor.rowcount, "grace_days": grace_days}

        if method == "workspace.status":
            result = dict(workspace)
            result["snapshot"] = self.snapshot_service.get_snapshot_stats(workspace_id)
            return result

        # workspace.file.refresh：增量 refresh 经 CAS/Replicator
        if method == "workspace.file.refresh":
            from callwarden.server.replicator import daemon_handle_refresh
            res = self._get_workspace_resources(workspace_id)
            # 从 UDS bytes frame 或 FD 获取 canonical bytes
            canonical_bytes = None
            received_fds = received_fds or []
            if received_fds:
                # FD 模式：从 FD 读取文件内容
                fd = received_fds[0]
                try:
                    info = os.fstat(fd)
                    canonical_bytes = os.read(fd, info.st_size)
                except OSError as e:
                    raise DaemonRpcError("fd_read_failed", str(e))
            elif "canonical_bytes_b64" in params:
                import base64
                canonical_bytes = base64.b64decode(params["canonical_bytes_b64"])
            # 调用 daemon_handle_refresh
            try:
                result = daemon_handle_refresh(
                    peer_uid=uid,
                    workspace_id=int(workspace_id),
                    msg=params,
                    ws_conn=res["ws_conn"],
                    cas_conn=res["cas_conn"],
                    canonical_bytes=canonical_bytes,
                )
                # 成功后追加 staging entry 并 replicate
                if result.get("status") == "committed":
                    from callwarden.server.staging_log import create_staging_entry
                    entry = create_staging_entry(
                        workspace_id=workspace_id,
                        file_path=params.get("rel_path", ""),
                        content_hash=result.get("content_hash", ""),
                        language=params.get("language", ""),
                    )
                    res["staging_log"].append(entry)
                    # 触发 replicate 发布新 generation
                    repl_result = res["replicator"].replicate(workspace_id)
                    result["replication"] = {
                        "generation": repl_result.generation,
                        "applied_count": repl_result.applied_count,
                        "duration_ms": repl_result.duration_ms,
                    }
                return result
            except Exception as e:
                raise DaemonRpcError("refresh_failed", str(e))

        # workspace.recover：崩溃恢复，重放 pending staging entries
        if method == "workspace.recover":
            res = self._get_workspace_resources(workspace_id)
            try:
                repl_result = res["replicator"].recover(workspace_id)
                return {
                    "status": "recovered",
                    "generation": repl_result.generation,
                    "applied_count": repl_result.applied_count,
                    "pending_count": repl_result.pending_count,
                    "duration_ms": repl_result.duration_ms,
                    "error": repl_result.error,
                }
            except Exception as e:
                raise DaemonRpcError("recover_failed", str(e))

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
