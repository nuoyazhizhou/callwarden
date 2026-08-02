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

    def __init__(self, endpoint: str):
        self._endpoint = endpoint
        self._lock_id = self._derive_lock_id(endpoint)
        self._handle: Optional[int] = None  # Windows mutex handle
        self._lock_file = None  # Unix lock file object
        self._acquired = False

    @staticmethod
    def _derive_lock_id(endpoint: str) -> str:
        """从 endpoint 派生唯一的锁标识。

        对 endpoint 做 SHA-256 取前 16 位 hex，确保：
        - 不同 endpoint 对应不同锁
        - 锁名称长度可控（Windows 命名互斥体有长度限制）
        """
        return hashlib.sha256(endpoint.encode("utf-8")).hexdigest()[:16]

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


def try_acquire_daemon_mutex(endpoint: str) -> Optional[DaemonMutex]:
    """尝试获取 daemon 启动互斥。

    Returns:
        持有互斥的 DaemonMutex 实例（调用方用完后必须 release），
        或 None（其他会话正在启动 daemon）。
    """
    mutex = DaemonMutex(endpoint)
    if mutex.try_acquire():
        return mutex
    return None


def should_start_daemon(endpoint: str) -> bool:
    """判断本会话是否应该启动 daemon [Req 14.23]。

    尝试获取互斥：
    - 成功 → 返回 True（本会话负责启动）
    - 失败 → 返回 False（其他会话正在启动，只退避重试）

    注意：返回 True 时互斥已被持有，调用方必须在启动完成后释放。
    推荐使用 try_acquire_daemon_mutex() 获取可释放的实例。
    """
    mutex = DaemonMutex(endpoint)
    return mutex.try_acquire()
