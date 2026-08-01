"""G9: Agent 端 watchdog 文件监控 → daemon RPC。

对应设计：
- `docs/design/enterprise-architecture-evolution.md` §v8 "systemd --user agent 回传 canonical bytes"
- `docs/design/parse-input-abi.md` §2（canonicalize_source 是唯一输入入口）

职责：
1. 监控 workspace 目录的文件变更（watchdog Observer）
2. 文件变化时触发 `user_agent_handle_refresh()`：
   - 调用 Rust `canonicalize_source_py(abs_path)` 生成规范化字节流
   - 调用 `send_refresh_to_daemon()` 通过 UDS 发送到 daemon
3. 支持防抖（避免连续保存触发多次 refresh）
4. 支持增量扫描（启动时扫描所有文件，发送初始 refresh）

设计要点：
- agent 进程是 per-UID 的，每个用户独立 systemd --user 实例
- canonicalize_source_py 通过 PyO3 调用 Rust canonicalize 模块（零拷贝 bytes）
- agent 不直接访问数据库，所有写入通过 daemon RPC
- watcher 失败时记录错误，不退出（避免 systemd 重启循环）
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Dict, Optional, Set

logger = logging.getLogger(__name__)

# watchdog 是可选依赖（Linux 才安装）
try:
    from watchdog.observers import Observer
    from watchdog.events import (
        FileSystemEventHandler,
        FileModifiedEvent,
        FileCreatedEvent,
        FileDeletedEvent,
        FileMovedEvent,
    )
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
    Observer = None
    FileSystemEventHandler = object

# Rust notify watcher 是 agent 的首选实现；watchdog 仅作为兼容性降级。
try:
    from callwarden_core import PyDebouncedFileWatcher  # type: ignore
    HAS_RUST_WATCHER = True
except ImportError:
    PyDebouncedFileWatcher = None  # type: ignore
    HAS_RUST_WATCHER = False

# 延迟导入 canonicalize_source_py（Rust 扩展可能未编译）
def _get_canonicalize_fn():
    try:
        import callwarden_core  # type: ignore
        return callwarden_core.canonicalize_source_py
    except (ImportError, AttributeError):
        return None


# ============================================
# AgentWatcher 主类
# ============================================


class AgentWatcher:
    """G9: per-UID agent watcher。

    监控 workspace 目录文件变更，触发 daemon refresh RPC。

    用法：
    ```python
    watcher = AgentWatcher(
        agent_session=session,
        daemon_rpc_client=rpc_client,
        workspace_instance_id="abc123...",
        watch_dir="/home/user/project",
        supported_exts={".py", ".rs", ".ts", ...},
    )
    watcher.start()
    # ... 运行中 ...
    watcher.stop()
    ```

    线程安全：watchdog Observer 内部多线程回调，AgentWatcher 通过 _lock 保护。
    """

    def __init__(
        self,
        agent_session,
        daemon_rpc_client,
        workspace_instance_id: str,
        watch_dir: str,
        supported_exts: Optional[Set[str]] = None,
        debounce_time: float = 1.0,
        auto_reconnect: bool = True,
    ):
        """初始化 agent watcher。

        Args:
            agent_session: AgentSession 实例（持有 session_id/epoch/seq）
            daemon_rpc_client: DaemonClient 单例（或 UnixDaemonRpcClient）
            workspace_instance_id: workspace 标识符（16 位 hex）
            watch_dir: 监控的目录绝对路径
            supported_exts: 支持的文件扩展名集合（如 {".py", ".rs"}）
            debounce_time: 防抖时间（秒）
            auto_reconnect: 收到 session_not_active/stale_session 错误时
                是否自动重新握手并重试一次（默认 True）
        """
        self.agent_session = agent_session
        self.daemon_rpc_client = daemon_rpc_client
        self.workspace_instance_id = workspace_instance_id
        self.watch_dir = os.path.abspath(watch_dir)
        self.supported_exts = supported_exts or set()
        self.debounce_time = debounce_time
        self.auto_reconnect = auto_reconnect

        self._observer: Optional[Any] = None
        self._handler: Optional["_AgentChangeHandler"] = None
        self._rust_watcher: Optional[Any] = None
        self._rust_poll_thread: Optional[threading.Thread] = None
        self._rust_poll_stop = threading.Event()
        self._lock = threading.Lock()
        self._running = False
        # 重连串行化：避免 watchdog 多线程同时触发 reconnect
        self._reconnect_lock = threading.Lock()

        # canonicalize 函数（Rust 扩展，可能未编译）
        self._canonicalize_fn = _get_canonicalize_fn()
        if self._canonicalize_fn is None:
            logger.warning(
                "callwarden_core.canonicalize_source_py 不可用，"
                "agent 将跳过 canonicalize 直接读文件",
            )

    @property
    def is_running(self) -> bool:
        """watcher 是否正在运行。"""
        return self._running

    def start(self) -> bool:
        """启动文件监控。

        Returns:
            True 启动成功；False 启动失败（watchdog 未安装）

        Raises:
            RuntimeError: watch_dir 不存在
        """
        if self._running:
            logger.warning("agent watcher 已在运行")
            return True

        if not os.path.isdir(self.watch_dir):
            raise RuntimeError(f"watch_dir 不存在：{self.watch_dir}")

        if HAS_RUST_WATCHER:
            try:
                rust_exts = [ext.lstrip(".") for ext in self.supported_exts if ext]
                self._rust_watcher = PyDebouncedFileWatcher(
                    self.watch_dir,
                    extensions=rust_exts,
                    debounce_ms=max(1, int(self.debounce_time * 1000)),
                )
                self._rust_watcher.start()
                self._rust_poll_stop.clear()
                self._rust_poll_thread = threading.Thread(
                    target=self._rust_poll_loop,
                    name="cw-agent-rust-watcher",
                    daemon=True,
                )
                self._rust_poll_thread.start()
                with self._lock:
                    self._running = True
                logger.info(
                    "agent Rust watcher 启动：dir=%s ws=%s session=%s",
                    self.watch_dir,
                    self.workspace_instance_id,
                    self.agent_session.session_id,
                )
                return True
            except Exception as e:
                logger.warning("Rust watcher 启动失败，回退 watchdog：%s", e)
                self._rust_watcher = None
                self._rust_poll_stop.set()
                if self._rust_poll_thread is not None:
                    self._rust_poll_thread.join(timeout=2.0)
                    self._rust_poll_thread = None

        if not HAS_WATCHDOG:
            logger.error("watchdog 未安装且 Rust watcher 不可用，agent watcher 无法启动")
            return False

        self._handler = _AgentChangeHandler(
            agent_watcher=self,
            supported_exts=self.supported_exts,
            debounce_time=self.debounce_time,
        )

        self._observer = Observer()
        self._observer.schedule(
            self._handler, self.watch_dir, recursive=True,
        )
        self._observer.start()
        self._running = True

        logger.info(
            "agent watcher 启动：dir=%s ws=%s session=%s",
            self.watch_dir,
            self.workspace_instance_id,
            self.agent_session.session_id,
        )
        return True

    def stop(self) -> None:
        """停止文件监控。"""
        if not self._running:
            return

        if self._rust_watcher is not None:
            # 先取出 debounce 窗口内剩余事件，再停止 notify 后台线程。
            try:
                self._process_rust_events(self._rust_watcher.flush())
            except Exception as e:
                logger.warning("Rust watcher flush 失败：%s", e)
            self._rust_poll_stop.set()
            if self._rust_poll_thread is not None:
                self._rust_poll_thread.join(timeout=2.0)
                self._rust_poll_thread = None
            try:
                self._rust_watcher.stop()
            except Exception:
                pass
            self._rust_watcher = None

        if self._observer:
            self._observer.stop()
            self._observer.join(timeout=5.0)
            self._observer = None

        # 处理剩余的 pending changes
        if self._handler:
            self._handler.flush_pending()

        with self._lock:
            self._running = False
        logger.info("agent watcher 已停止")

    def _rust_poll_loop(self) -> None:
        """轮询 Rust notify watcher，并把事件送入现有 RPC 处理链。"""
        while not self._rust_poll_stop.is_set():
            try:
                events = self._rust_watcher.poll_events() if self._rust_watcher else []
                if events:
                    self._process_rust_events(events)
                else:
                    self._rust_poll_stop.wait(timeout=0.05)
            except Exception as e:
                logger.error("Rust watcher poll 失败：%s", e)
                self._rust_poll_stop.wait(timeout=0.5)

    def _process_rust_events(self, events) -> None:
        """处理 Rust watcher 已防抖/合并的事件。"""
        for event in events or []:
            kind = event.get("kind", "")
            path = event.get("path") or ""
            from_path = event.get("from_path")
            to_path = event.get("to_path")
            try:
                if kind == "renamed":
                    if from_path and self._is_supported(from_path):
                        self.handle_file_delete(from_path)
                    if to_path and self._is_supported(to_path) and os.path.isfile(to_path):
                        self.handle_file_change(to_path)
                elif kind in ("created", "modified"):
                    if path and os.path.isfile(path):
                        self.handle_file_change(path)
                elif kind == "removed" and path:
                    self.handle_file_delete(path)
            except Exception as e:
                logger.error("处理 Rust watcher 事件失败 %s：%s", path, e)

    def _is_supported(self, path: str) -> bool:
        """判断事件路径是否属于 agent 支持的源码扩展名。"""
        if not path:
            return False
        return os.path.splitext(path)[1].lower() in self.supported_exts

    def join(self, timeout: Optional[float] = None) -> None:
        """阻塞等待 watcher 退出（用于 systemd 优雅停止）。"""
        if self._observer:
            self._observer.join(timeout=timeout)

    # ============================================
    # 文件变更处理（由 _AgentChangeHandler 调用）
    # ============================================

    def handle_file_change(self, abs_path: str) -> Dict[str, Any]:
        """处理单个文件变更。

        触发 refresh 流程：
        1. canonicalize_source_py（Rust）→ 规范化字节流 + content_hash
        2. 计算 rel_path（相对 watch_dir）
        3. send_refresh_to_daemon()

        规范：parse-input-abi.md §2（canonicalize 是唯一输入入口）

        G9 auto-reconnect（runbook §9.7.3）：当 send_refresh_to_daemon 抛
        AgentProtocolError 且 code 为 session_not_active / stale_session 时，
        自动调用 user_agent_connect() 重新协商 epoch 并重试一次 refresh。
        重连串行化（_reconnect_lock）避免 watchdog 多线程同时触发 reconnect。

        Args:
            abs_path: 变更文件的绝对路径

        Returns:
            daemon 响应 dict（如 `{"status": "committed"}`）

        Raises:
            Exception: refresh 失败（由调用方决定是否重试）
        """
        if not os.path.isfile(abs_path):
            # 文件已删除（on_deleted 单独处理）
            logger.debug("跳过已删除的文件：%s", abs_path)
            return {"status": "skipped_deleted", "path": abs_path}

        # 1. canonicalize（Rust 扩展）
        canonical_bytes: Optional[bytes] = None
        content_hash: Optional[str] = None
        if self._canonicalize_fn is not None:
            try:
                result = self._canonicalize_fn(abs_path)
                # result 是 dict：{canonical_bytes, content_hash, ...}
                canonical_bytes = result.get("canonical_bytes")
                content_hash = result.get("content_hash")
            except Exception as e:
                logger.warning(
                    "canonicalize_source_py 失败 %s: %s，降级读取原文件",
                    abs_path, e,
                )

        # 降级：直接读取文件
        if canonical_bytes is None:
            try:
                with open(abs_path, "rb") as f:
                    canonical_bytes = f.read()
                import hashlib
                content_hash = hashlib.sha256(canonical_bytes).hexdigest()
            except OSError as e:
                logger.error("读取文件失败 %s: %s", abs_path, e)
                raise

        # 2. 计算 rel_path（相对 watch_dir）
        rel_path = os.path.relpath(abs_path, self.watch_dir).replace("\\", "/")

        # 3. 发送 refresh RPC
        from callwarden.server.agent_protocol import (
            send_refresh_to_daemon, AgentProtocolError,
        )
        try:
            return self._send_refresh(
                rel_path, abs_path, canonical_bytes, content_hash,
            )
        except AgentProtocolError as e:
            if not self.auto_reconnect:
                raise
            if e.code not in ("session_not_active", "stale_session"):
                raise
            # session 失效 → 自动重连一次并重试 refresh
            logger.warning(
                "session 失效（code=%s），自动重新握手 workspace=%s: %s",
                e.code, self.workspace_instance_id, e.message,
            )
            if not self._reconnect():
                # 重连失败：上抛原异常（已记录错误日志）
                raise
            # 重连成功 → 重试一次 refresh
            logger.info(
                "重连成功，重试 refresh %s workspace=%s",
                abs_path, self.workspace_instance_id,
            )
            return self._send_refresh(
                rel_path, abs_path, canonical_bytes, content_hash,
            )

    def _send_refresh(
        self,
        rel_path: str,
        abs_path: str,
        canonical_bytes: bytes,
        content_hash: Optional[str],
    ) -> Dict[str, Any]:
        """实际调用 send_refresh_to_daemon（抽出以便重试复用）。"""
        from callwarden.server.agent_protocol import send_refresh_to_daemon
        return send_refresh_to_daemon(
            daemon_rpc_client=self.daemon_rpc_client,
            agent_session=self.agent_session,
            workspace_instance_id=self.workspace_instance_id,
            rel_path=rel_path,
            abs_path=abs_path,
            canonical_bytes=canonical_bytes,
            content_hash=content_hash,
        )

    def _reconnect(self) -> bool:
        """调用 user_agent_connect 重新握手。

        串行化：多个 watchdog 线程同时撞上 session 失效时，只让第一个
        执行重连，其余等待并返回 True（让调用方走重试 refresh 路径，
        若新 epoch 仍无效再各自上抛）。

        Returns:
            True 重连成功；False 重连失败（已记录错误日志）
        """
        from callwarden.server.agent_protocol import (
            user_agent_connect, AgentProtocolError,
        )
        # 用非阻塞 acquire：拿不到锁说明已有重连在进行，本调用方直接
        # 走重试路径（让重连后的新 epoch 决定结果）
        if not self._reconnect_lock.acquire(blocking=False):
            return True
        try:
            user_agent_connect(
                daemon_rpc_client=self.daemon_rpc_client,
                workspace_instance_id=self.workspace_instance_id,
                agent_session=self.agent_session,
            )
            return True
        except AgentProtocolError as e:
            logger.error(
                "自动重连失败 workspace=%s: code=%s msg=%s",
                self.workspace_instance_id, e.code, e.message,
            )
            return False
        except Exception as e:
            logger.error(
                "自动重连异常 workspace=%s: %s",
                self.workspace_instance_id, e,
            )
            return False
        finally:
            self._reconnect_lock.release()

    def handle_file_delete(self, abs_path: str) -> Dict[str, Any]:
        """处理文件删除事件。

        发送 `workspace.file.delete` RPC 通知 daemon 删除符号。

        Args:
            abs_path: 被删除文件的绝对路径

        Returns:
            daemon 响应 dict
        """
        rel_path = os.path.relpath(abs_path, self.watch_dir).replace("\\", "/")
        try:
            response = self.daemon_rpc_client.call(
                "workspace.file.delete",
                {
                    "workspace_instance_id": self.workspace_instance_id,
                    "rel_path": rel_path,
                    "agent_session_id": self.agent_session.session_id,
                },
            )
            return response
        except Exception as e:
            logger.error(
                "workspace.file.delete RPC 失败 %s: %s", rel_path, e,
            )
            raise


# ============================================
# watchdog 事件处理器（带防抖）
# ============================================


class _AgentChangeHandler(FileSystemEventHandler):
    """watchdog 事件处理器，带防抖和批量处理。

    设计与 server/watcher.py 的 _ChangeHandler 一致，但触发 daemon RPC
    而非本地 db.refresh。
    """

    def __init__(
        self,
        agent_watcher: AgentWatcher,
        supported_exts: Set[str],
        debounce_time: float = 1.0,
    ):
        self.agent_watcher = agent_watcher
        self.supported_exts = supported_exts
        self.debounce_time = debounce_time
        # path → 最后变更时间戳
        self._pending_changes: Dict[str, float] = {}
        self._pending_lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None
        self._running = True

    def _is_supported(self, path: str) -> bool:
        """检查文件是否在支持的语言列表中。"""
        if not path:
            return False
        ext = os.path.splitext(path)[1].lower()
        return ext in self.supported_exts

    def _schedule_process(self) -> None:
        """调度批量处理（防抖）。"""
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(
            self.debounce_time, self._process_pending,
        )
        self._timer.daemon = True
        self._timer.start()

    def _process_pending(self) -> None:
        """批量处理待处理的文件变更。"""
        with self._pending_lock:
            if not self._pending_changes:
                return
            # 取出所有 pending，清空字典
            paths = list(self._pending_changes.keys())
            self._pending_changes.clear()

        # 逐个处理（顺序不重要，每个文件独立 RPC）
        for path in paths:
            self._handle_single_change(path)

    def _handle_single_change(self, abs_path: str) -> None:
        """处理单个文件变更（调用 agent_watcher.handle_file_change）。"""
        try:
            response = self.agent_watcher.handle_file_change(abs_path)
            status = response.get("status", "unknown")
            if status == "committed":
                logger.info(
                    "refresh 成功 %s: generation=%s",
                    abs_path, response.get("generation", ""),
                )
            elif status == "stale_seq_dropped":
                logger.warning(
                    "refresh 被丢弃（stale seq）：%s", abs_path,
                )
            else:
                logger.info(
                    "refresh 响应 %s: %s", abs_path, status,
                )
        except Exception as e:
            logger.error(
                "处理文件变更失败 %s: %s", abs_path, e,
            )
            # 不重新加入 pending，避免无限循环（下次文件再变时会再触发）

    # ============================================
    # watchdog 事件回调
    # ============================================

    def on_modified(self, event):
        if event.is_directory or not self._running:
            return
        path = event.src_path
        if not self._is_supported(path):
            return
        with self._pending_lock:
            self._pending_changes[path] = time.time()
        self._schedule_process()

    def on_created(self, event):
        if event.is_directory or not self._running:
            return
        path = event.src_path
        if not self._is_supported(path):
            return
        with self._pending_lock:
            self._pending_changes[path] = time.time()
        self._schedule_process()

    def on_deleted(self, event):
        if event.is_directory or not self._running:
            return
        path = event.src_path
        if not self._is_supported(path):
            return
        # 删除事件立即处理（无需防抖）
        try:
            self.agent_watcher.handle_file_delete(path)
        except Exception as e:
            logger.error("处理文件删除失败 %s: %s", path, e)

    def on_moved(self, event):
        if event.is_directory or not self._running:
            return
        # 移动 = 删除旧路径 + 创建新路径
        old_path = event.src_path
        new_path = event.dest_path
        if self._is_supported(old_path):
            try:
                self.agent_watcher.handle_file_delete(old_path)
            except Exception as e:
                logger.error("处理文件移动（旧路径删除）失败 %s: %s", old_path, e)
        if self._is_supported(new_path):
            with self._pending_lock:
                self._pending_changes[new_path] = time.time()
            self._schedule_process()

    def flush_pending(self) -> None:
        """立即处理所有 pending changes（用于 watcher.stop() 前的清理）。"""
        if self._timer:
            self._timer.cancel()
            self._timer = None
        self._running = False
        self._process_pending()

    def shutdown(self) -> None:
        """关闭 handler。"""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None


# ============================================
# 便捷函数：启动 watcher 并阻塞
# ============================================


def run_agent_watcher_loop(
    agent_session,
    daemon_rpc_client,
    workspace_instance_id: str,
    watch_dir: str,
    supported_exts: Optional[Set[str]] = None,
    stop_event: Optional[threading.Event] = None,
) -> int:
    """G9: 启动 agent watcher 并阻塞直到 stop_event 被设置。

    用于 systemd --user 服务的主循环：
    ```python
    # cw-agent start 命令入口
    session = AgentSession.create_or_load()
    user_agent_connect(rpc_client, ws_id, session)
    run_agent_watcher_loop(session, rpc_client, ws_id, watch_dir, exts, stop_event)
    ```

    Args:
        agent_session: AgentSession 实例
        daemon_rpc_client: DaemonClient 单例
        workspace_instance_id: workspace 标识符
        watch_dir: 监控目录
        supported_exts: 支持的扩展名集合
        stop_event: 停止事件（systemd 信号处理时设置）

    Returns:
        0 成功；非 0 失败
    """
    if not HAS_RUST_WATCHER and not HAS_WATCHDOG:
        logger.error("Rust watcher 与 watchdog 均未安装，无法启动 agent watcher")
        return 2

    watcher = AgentWatcher(
        agent_session=agent_session,
        daemon_rpc_client=daemon_rpc_client,
        workspace_instance_id=workspace_instance_id,
        watch_dir=watch_dir,
        supported_exts=supported_exts or set(),
    )

    try:
        if not watcher.start():
            return 2

        # 阻塞等待 stop_event
        if stop_event is None:
            stop_event = threading.Event()
        while not stop_event.is_set():
            time.sleep(1.0)
        return 0
    except KeyboardInterrupt:
        return 0
    finally:
        watcher.stop()
