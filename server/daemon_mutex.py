"""Daemon 唤起单实例跨进程互斥（Req 14.23, 14.6）。

启动 daemon 前必须先取得跨进程互斥（Windows 命名互斥体、Linux/macOS 文件锁），
保证同一用户 Daemon_Endpoint 上最多一个 daemon 进程；未取得互斥的会话不启动进程，
只在有界等待窗口内继续退避重试。

缺这道互斥时 N 个会话并发唤起会产生 N 个 daemon 进程，也就是 N 个串行化点，
直接违反 Requirement 14.6；因此这是本级唯一的安全性要求，不得降级为"尽力去重"。

所有权：本文件（server/daemon_mutex.py）。
设计参考：docs/design/multi-llm-contract-driven-collaboration-design.md §13.5.7 Property 25
"""

from __future__ import annotations

import hashlib
import logging
import os
import sys
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)

# 互斥体名称前缀（Windows）
_MUTEX_NAME_PREFIX = "Global\\CallWarden_Daemon_"
# 锁文件目录（Unix）
_LOCK_DIR = os.path.join(tempfile.gettempdir(), "callwarden")


class DaemonMutex:
    """跨进程互斥——保证同一 Daemon_Endpoint 最多一个 daemon 进程 [Req 14.23]。

    用法：
        mutex = DaemonMutex(endpoint)
        if mutex.try_acquire():
            try:
                start_daemon(...)
            finally:
                mutex.release()
        else:
            # 其他会话正在启动 daemon，只退避重试
            pass

    也可作为上下文管理器：
        with DaemonMutex(endpoint) as acquired:
            if acquired:
                start_daemon(...)
    """

    def __init__(self, endpoint: str = "", authority_id: str = ""):
        """构造 host 级 daemon 启动互斥。

        endpoint 保留为兼容参数（旧调用方 ``DaemonMutex(endpoint)`` 不报错），
        但**不再参与锁标识派生**——D3 根因正是「按 endpoint 分锁导致两个端口
        两把锁互不冲突，第二 daemon 与权威实例共写同一主库」。锁标识改为
        host 级：仅按 authority_id + 权威 task_db 路径派生，同机所有 endpoint
        共享同一把锁。
        """
        self._endpoint = endpoint
        self._authority_id = authority_id
        # D3：显式 authority_id → host 级锁；仅 endpoint → legacy 兼容派生。
        self._lock_id = self._derive_lock_id(endpoint, authority_id)
        self._handle: Optional[int] = None  # Windows mutex handle
        self._lock_file = None  # Unix lock file object
        self._acquired = False

    @staticmethod
    def _host_identity(authority_id: str = "") -> str:
        """host 级身份串：authority_id + 权威 task_db 绝对路径。

        authority_id 缺省时取当前 authority（与 manifest 一致），task_db 取
        callwarden.config.DB_PATH。两者共同唯一标识「本机本用户本权威库」——
        同一台机同一权威库只允许一个 daemon，无论它绑定哪个 endpoint。
        """
        if not authority_id:
            try:
                from callwarden.config import get_http_authority_id
                authority_id = get_http_authority_id()
            except Exception:
                authority_id = "unknown-authority"
        try:
            from callwarden.config import DB_PATH
            task_db = os.path.realpath(DB_PATH)
        except Exception:
            task_db = "unknown-task-db"
        return f"{authority_id}|{task_db}"

    @classmethod
    def _derive_lock_id(cls, endpoint: str = "", authority_id: str = "") -> str:
        """派生锁标识：D3 host 级优先，legacy endpoint 兼容。

        - 显式 ``authority_id``（含便捷函数新调用）→ host 级身份串
          ``authority|task_db``，同机所有 endpoint 共享一把锁（D3 根修）。
          这是白名单内自愈路径（daemon_autostart / mcp_server）启用的模式。
        - 仅传 ``endpoint`` 且未给 authority_id → 保留旧签名语义，按 endpoint
          派生。旧调用方（daemon_client.py / 既有单测）不受破坏；真正的
          防双实例权威守卫在 spawn_http_daemon 内部用 host 级锁兜底，不依赖
          这层 endpoint 锁。

        SHA-256 取前 16 位 hex，名称长度可控（Windows 命名互斥体限制）。
        """
        if authority_id:
            identity = cls._host_identity(authority_id)
        else:
            identity = f"endpoint|{endpoint}"
        return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]

    def try_acquire(self) -> bool:
        """非阻塞尝试获取互斥。

        Returns:
            True 表示成功获取（本会话负责启动 daemon），
            False 表示其他会话已持有（本会话只退避重试）。
        """
        if self._acquired:
            return True

        if sys.platform == "win32":
            return self._try_acquire_windows()
        else:
            return self._try_acquire_unix()

    def release(self) -> None:
        """释放互斥。"""
        if not self._acquired:
            return

        if sys.platform == "win32":
            self._release_windows()
        else:
            self._release_unix()

        self._acquired = False

    @property
    def acquired(self) -> bool:
        """当前是否持有互斥。"""
        return self._acquired

    @property
    def lock_id(self) -> str:
        """锁标识（调试用）。"""
        return self._lock_id

    # --- 上下文管理器 ---

    def __enter__(self) -> bool:
        """进入上下文时尝试获取互斥，返回是否成功。"""
        return self.try_acquire()

    def __exit__(self, *args) -> None:
        """退出上下文时释放互斥（如果持有）。"""
        self.release()

    # --- Windows 实现：命名互斥体 ---

    def _try_acquire_windows(self) -> bool:
        """Windows: 使用命名互斥体（CreateMutexW）[Req 14.23]。"""
        try:
            import ctypes
            from ctypes import wintypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]

            mutex_name = f"{_MUTEX_NAME_PREFIX}{self._lock_id}"

            # CreateMutexW: 创建或打开命名互斥体
            # bInitialOwner=False: 不立即获取所有权
            handle = kernel32.CreateMutexW(None, False, mutex_name)
            if handle is None or handle == 0:
                logger.warning("CreateMutexW 失败: %d", ctypes.GetLastError())
                return False

            # WaitForSingleObject with timeout=0: 非阻塞尝试获取
            WAIT_OBJECT_0 = 0x00000000
            WAIT_ABANDONED = 0x00000080
            result = kernel32.WaitForSingleObject(handle, 0)

            if result == WAIT_OBJECT_0 or result == WAIT_ABANDONED:
                # 成功获取（WAIT_ABANDONED 表示前一个持有者崩溃，互斥体仍有效）
                self._handle = handle
                self._acquired = True
                logger.debug("Windows 命名互斥体已获取: %s", mutex_name)
                return True
            else:
                # WAIT_TIMEOUT: 其他进程持有
                kernel32.CloseHandle(handle)
                logger.debug("Windows 命名互斥体被其他进程持有: %s", mutex_name)
                return False
        except Exception as exc:
            logger.warning("Windows 互斥获取异常: %s", exc)
            return False

    def _release_windows(self) -> None:
        """Windows: 释放命名互斥体。"""
        if self._handle is not None:
            try:
                import ctypes

                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                kernel32.ReleaseMutex(self._handle)
                kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None

    # --- Unix 实现：文件锁 ---

    def _try_acquire_unix(self) -> bool:
        """Unix (Linux/macOS): 使用文件锁（fcntl.flock）[Req 14.23]。"""
        try:
            import fcntl

            lock_path = self._get_lock_path()

            # 确保锁目录存在
            os.makedirs(os.path.dirname(lock_path), exist_ok=True)

            # 打开（或创建）锁文件
            self._lock_file = open(lock_path, "w")

            # LOCK_EX | LOCK_NB: 排他锁 + 非阻塞
            fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            # 写入 PID 便于调试
            self._lock_file.write(str(os.getpid()))
            self._lock_file.flush()

            self._acquired = True
            logger.debug("Unix 文件锁已获取: %s", lock_path)
            return True
        except (IOError, OSError):
            # EWOULDBLOCK/EAGAIN: 其他进程持有锁
            if self._lock_file is not None:
                try:
                    self._lock_file.close()
                except Exception:
                    pass
                self._lock_file = None
            logger.debug("Unix 文件锁被其他进程持有")
            return False
        except Exception as exc:
            logger.warning("Unix 文件锁获取异常: %s", exc)
            if self._lock_file is not None:
                try:
                    self._lock_file.close()
                except Exception:
                    pass
                self._lock_file = None
            return False

    def _release_unix(self) -> None:
        """Unix: 释放文件锁。"""
        if self._lock_file is not None:
            try:
                import fcntl

                fcntl.flock(self._lock_file.fileno(), fcntl.LOCK_UN)
                self._lock_file.close()
            except Exception:
                pass
            self._lock_file = None

    def _get_lock_path(self) -> str:
        """获取 Unix 锁文件路径。"""
        return os.path.join(_LOCK_DIR, f"daemon_{self._lock_id}.lock")


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------


def try_acquire_daemon_mutex(
    endpoint: str = "", authority_id: str = ""
) -> Optional[DaemonMutex]:
    """尝试获取 daemon 启动互斥（host 级，同机所有 endpoint 共享一把锁）。

    Returns:
        持有互斥的 DaemonMutex 实例（调用方用完后必须 release），
        或 None（其他会话正在启动 daemon）。
    """
    mutex = DaemonMutex(endpoint=endpoint, authority_id=authority_id)
    if mutex.try_acquire():
        return mutex
    return None


def should_start_daemon(endpoint: str = "", authority_id: str = "") -> bool:
    """判断本会话是否应该启动 daemon [Req 14.23]。

    尝试获取互斥：
    - 成功 → 返回 True（本会话负责启动）
    - 失败 → 返回 False（其他会话正在启动，只退避重试）

    注意：返回 True 时互斥已被持有，调用方必须在启动完成后释放。
    推荐使用 try_acquire_daemon_mutex() 获取可释放的实例。
    """
    mutex = DaemonMutex(endpoint=endpoint, authority_id=authority_id)
    return mutex.try_acquire()


def acquire_host_daemon_mutex() -> Optional[DaemonMutex]:
    """D3 自愈守卫专用：获取 host 级 daemon 启动互斥。

    与 ``try_acquire_daemon_mutex(endpoint)`` 的区别：本函数**不带 endpoint**，
    lock_id 恒按 authority + task_db 派生（同机所有端口/端点共享一把锁）。
    这是「同一台机只允许一个 daemon 实例」的权威互斥，白名单内自愈路径
    （daemon_autostart.spawn_http_daemon）必须用它，而非按 endpoint 的旧锁。
    """
    if not authority_id:
        from callwarden.config import get_http_authority_id
        authority_id = get_http_authority_id()
    return try_acquire_daemon_mutex(authority_id=authority_id)
