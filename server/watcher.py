"""
watcher.py
==========

文件监控模块：监控项目目录的文件变化，自动增量刷新代码知识图谱。
支持多语言、文件创建/修改/删除/重命名、防抖批量处理。

M8（2026-07-20 批次4）：
- 优先使用 Rust 实现的 ``PyDebouncedFileWatcher``（基于 notify crate + crossbeam channel）
- 失败时回退到 ``watchdog`` 实现（``_WatchdogChangeHandler``）
- ``Renamed`` 事件携带 ``from_path`` / ``to_path``，由本模块分别触发
  ``remove_file``（src）和 ``refresh_file``（dest）
- Rust watcher 内部已完成 debounce（默认 500ms）+ coalescing，
  Python 轮询线程只需处理已稳定的事件
"""

import os
import time
import threading
from typing import Dict, List, Set, Optional

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
    FileSystemEventHandler = object  # type: ignore
    HAS_WATCHDOG = False

# M8：优先尝试 Rust 实现的 PyDebouncedFileWatcher（notify crate + crossbeam）
try:
    from callwarden_core import PyDebouncedFileWatcher  # type: ignore
    HAS_RUST_WATCHER = True
except ImportError:
    PyDebouncedFileWatcher = None  # type: ignore
    HAS_RUST_WATCHER = False

from ..config import PROJECT_ROOT, norm_path, get_supported_extensions, detect_language_from_path
from ..i18n import t


class FileWatcher:
    """文件监控器：监听项目目录变化，增量更新知识图谱。

    M8（2026-07-20 批次4）：优先使用 Rust 实现的 ``PyDebouncedFileWatcher``，
    回退到 ``watchdog`` 实现。Renamed 事件携带 ``from_path`` / ``to_path``，
    分别触发 ``remove_file`` 和 ``refresh_file``。
    """

    def __init__(self, db, watch_dir: str = None):
        """初始化文件监控器。

        Args:
            db: CodeGraphDB 实例（用于增量刷新）
            watch_dir: 监控目录，为空时使用 db.workspace_root 或 PROJECT_ROOT
        """
        self.db = db
        self.watch_dir = watch_dir or db.workspace_root or PROJECT_ROOT
        self._supported_exts = get_supported_extensions()

        # Rust watcher 状态
        self._rust_watcher = None
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()
        self._debounce_ms = 500  # Rust watcher 默认 debounce 毫秒

        # watchdog fallback 状态
        self._observer = None
        self._handler = None

    # ----------------------------------------------------------------
    # 启动入口
    # ----------------------------------------------------------------

    def start(self):
        """启动文件监控（优先 Rust，回退 watchdog）。"""
        if HAS_RUST_WATCHER:
            started = self._start_with_rust()
            if started is not None:
                return started
            # Rust 启动失败且可回退：继续走 watchdog
        if not HAS_WATCHDOG:
            print(t("cli.messages.watcher_no_watchdog"))
            return False
        return self._start_with_watchdog()

    def stop(self):
        """停止监控。"""
        # Rust watcher
        if self._rust_watcher is not None:
            try:
                self._rust_watcher.stop()
            except Exception:
                pass
            self._rust_watcher = None
        if self._poll_thread is not None:
            self._poll_stop.set()
            self._poll_thread.join(timeout=2.0)
            self._poll_thread = None
            self._poll_stop.clear()
        # watchdog
        if self._observer is not None:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        if self._handler is not None:
            self._handler.shutdown()
            self._handler = None

    # ----------------------------------------------------------------
    # Rust 实现（M8 主路径）
    # ----------------------------------------------------------------

    def _start_with_rust(self):
        """M8：用 Rust PyDebouncedFileWatcher 启动监控。

        Returns:
            True：成功启动并进入阻塞循环（KeyboardInterrupt 后返回）
            False：Rust watcher 启动失败，回退到 watchdog
            None：Rust 模块未安装（调用方应回退）
        """
        if not HAS_RUST_WATCHER:
            return None

        # watchdog 的 supported_exts 是带点的（'.py'），Rust 的不带点（'py'）
        rust_exts = [e.lstrip(".") for e in self._supported_exts if e]
        try:
            self._rust_watcher = PyDebouncedFileWatcher(
                self.watch_dir,
                extensions=rust_exts,
                debounce_ms=self._debounce_ms,
            )
            self._rust_watcher.start()
        except Exception as e:
            print(f"[M8] Rust watcher 启动失败，回退到 watchdog: {e}")
            self._rust_watcher = None
            return False

        langs_str = ", ".join(
            sorted(
                set(
                    detect_language_from_path("test." + e)
                    for e in rust_exts
                    if e
                )
            )
        )
        print(t("cli.messages.watcher_started", dir=self.watch_dir))
        print(t("cli.messages.watcher_listening_langs", langs=langs_str))
        print(t("cli.messages.watcher_stop_hint"))
        print()

        # Python 轮询线程：定期 poll_events 并处理
        self._poll_thread = threading.Thread(
            target=self._poll_loop,
            name="cw-watcher-poll",
            daemon=True,
        )
        self._poll_thread.start()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(t("cli.messages.watcher_stopping"))
            self.stop()
            print(t("cli.messages.watcher_stopped"))

        return True

    def _poll_loop(self):
        """轮询 Rust watcher 事件并处理。

        Rust watcher 内部已做 debounce + coalescing，poll_events 返回的事件
        已稳定，可直接处理。无事件时短休眠以避免空转。
        """
        while not self._poll_stop.is_set():
            try:
                events = self._rust_watcher.poll_events()
                if events:
                    self._process_rust_events(events)
                else:
                    # 无事件时短休眠
                    self._poll_stop.wait(timeout=0.1)
            except Exception as e:
                print(f"[M8] poll_events error: {e}")
                self._poll_stop.wait(timeout=1.0)

    def _process_rust_events(self, events: List[Dict]) -> None:
        """处理一批 Rust watcher 事件。

        Rust watcher 已做 coalescing（同路径多次事件取最新 kind），
        本方法只需按 kind 分组处理。

        Renamed 事件携带 from_path / to_path：
        - from_path 非空 → remove_file（src 已不存在）
        - to_path 非空 → refresh_file（dest 新路径）
        """
        modified: List[str] = []
        deleted: List[str] = []

        for ev in events:
            kind = ev.get("kind", "")
            path = ev.get("path", "")
            from_path = ev.get("from_path")
            to_path = ev.get("to_path")

            if kind == "renamed":
                # M8：Renamed 事件双路径处理
                if from_path and self._is_supported(from_path):
                    deleted.append(from_path)
                if to_path and self._is_supported(to_path) and os.path.exists(to_path):
                    modified.append(to_path)
                continue

            if not path:
                continue
            if kind in ("modified", "created"):
                if os.path.exists(path):
                    modified.append(path)
            elif kind == "removed":
                deleted.append(path)

        if modified:
            self._handle_modified(modified)
        if deleted:
            self._handle_deleted(deleted)

    # ----------------------------------------------------------------
    # watchdog fallback
    # ----------------------------------------------------------------

    def _start_with_watchdog(self):
        """watchdog 实现（fallback）。"""
        self._handler = _WatchdogChangeHandler(self.db, self._supported_exts)

        self._observer = Observer()
        self._observer.schedule(self._handler, self.watch_dir, recursive=True)
        self._observer.start()

        # watchdog 的 supported_exts 是带点的（'.py'）
        exts_no_dot = [e.lstrip(".") for e in self._supported_exts if e]
        langs_str = ", ".join(
            sorted(set(detect_language_from_path("test." + e) for e in exts_no_dot))
        )
        print(t("cli.messages.watcher_started", dir=self.watch_dir))
        print(t("cli.messages.watcher_listening_langs", langs=langs_str))
        print(t("cli.messages.watcher_stop_hint"))
        print()

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print(t("cli.messages.watcher_stopping"))
            self.stop()
            print(t("cli.messages.watcher_stopped"))

        return True

    # ----------------------------------------------------------------
    # 共享处理逻辑
    # ----------------------------------------------------------------

    def _is_supported(self, path: str) -> bool:
        """检查文件是否在支持的语言列表中。"""
        if not path:
            return False
        ext = os.path.splitext(path)[1].lower()
        return ext in self._supported_exts

    def _handle_modified(self, paths: List[str]):
        """处理文件修改/创建。"""
        print(t("cli.messages.watcher_modified_detected", count=len(paths)))
        success = 0
        for path in paths:
            try:
                rel_path = os.path.relpath(path, self.db.workspace_root)
                rel_path = norm_path(rel_path)
                self.db.refresh_file(rel_path)
                success += 1
                print(t("cli.messages.watcher_update_item", path=rel_path))
            except Exception as e:
                print(t("cli.messages.watcher_update_fail", path=path, error=e))
        print(t("cli.messages.watcher_update_done", success=success, total=len(paths)))

    def _handle_deleted(self, paths: List[str]):
        """处理文件删除。"""
        print(t("cli.messages.watcher_deleted_detected", count=len(paths)))
        for path in paths:
            try:
                rel_path = os.path.relpath(path, self.db.workspace_root)
                rel_path = norm_path(rel_path)
                self.db.remove_file(rel_path)
                print(t("cli.messages.watcher_delete_item", path=rel_path))
            except Exception as e:
                print(t("cli.messages.watcher_update_fail", path=path, error=e))
        print(t("cli.messages.watcher_delete_done"))


class _WatchdogChangeHandler(FileSystemEventHandler):
    """watchdog fallback 处理器（带防抖和批量处理）。

    M8（2026-07-20 批次4）：从原 ``_ChangeHandler`` 重命名，
    主路径已切换到 Rust PyDebouncedFileWatcher，本类仅作 fallback。
    """

    def __init__(self, db, supported_exts: Set[str]):
        self.db = db
        self.supported_exts = supported_exts
        self._debounce_time = 1.0  # 1 秒防抖
        self._pending_changes: Dict[str, float] = {}  # path -> 最后变更时间
        self._pending_lock = threading.Lock()
        self._timer = None
        self._running = True

    def _is_supported(self, path: str) -> bool:
        if not path:
            return False
        ext = os.path.splitext(path)[1].lower()
        return ext in self.supported_exts

    def _schedule_process(self):
        """调度批量处理（防抖）。"""
        if self._timer:
            self._timer.cancel()
        self._timer = threading.Timer(self._debounce_time, self._process_pending)
        self._timer.daemon = True
        self._timer.start()

    def _process_pending(self):
        """批量处理待处理的文件变更。"""
        with self._pending_lock:
            if not self._pending_changes:
                return

            now = time.time()
            ready = {
                p: t for p, t in self._pending_changes.items()
                if now - t >= self._debounce_time
            }
            self._pending_changes = {
                p: t for p, t in self._pending_changes.items()
                if now - t < self._debounce_time
            }

        if not ready:
            if self._pending_changes:
                self._schedule_process()
            return

        modified = []
        deleted = []
        for path in ready:
            if os.path.exists(path):
                modified.append(path)
            else:
                deleted.append(path)

        if modified:
            self._handle_modified(modified)
        if deleted:
            self._handle_deleted(deleted)

        with self._pending_lock:
            if self._pending_changes:
                self._schedule_process()

    def _handle_modified(self, paths: List[str]):
        print(t("cli.messages.watcher_modified_detected", count=len(paths)))
        success = 0
        for path in paths:
            try:
                rel_path = os.path.relpath(path, self.db.workspace_root)
                rel_path = norm_path(rel_path)
                self.db.refresh_file(rel_path)
                success += 1
                print(t("cli.messages.watcher_update_item", path=rel_path))
            except Exception as e:
                print(t("cli.messages.watcher_update_fail", path=path, error=e))
        print(t("cli.messages.watcher_update_done", success=success, total=len(paths)))

    def _handle_deleted(self, paths: List[str]):
        print(t("cli.messages.watcher_deleted_detected", count=len(paths)))
        for path in paths:
            try:
                rel_path = os.path.relpath(path, self.db.workspace_root)
                rel_path = norm_path(rel_path)
                self.db.remove_file(rel_path)
                print(t("cli.messages.watcher_delete_item", path=rel_path))
            except Exception as e:
                print(t("cli.messages.watcher_update_fail", path=path, error=e))
        print(t("cli.messages.watcher_delete_done"))

    def on_modified(self, event):
        if event.is_directory:
            return
        if not self._is_supported(event.src_path):
            return
        with self._pending_lock:
            self._pending_changes[event.src_path] = time.time()
        self._schedule_process()

    def on_created(self, event):
        if event.is_directory:
            return
        if not self._is_supported(event.src_path):
            return
        with self._pending_lock:
            self._pending_changes[event.src_path] = time.time()
        self._schedule_process()

    def on_deleted(self, event):
        if event.is_directory:
            return
        if not self._is_supported(event.src_path):
            return
        with self._pending_lock:
            self._pending_changes[event.src_path] = time.time()
        self._schedule_process()

    def on_moved(self, event):
        """文件重命名/移动。"""
        if event.is_directory:
            return

        src_supported = self._is_supported(event.src_path)
        dest_supported = self._is_supported(event.dest_path)

        if src_supported:
            with self._pending_lock:
                self._pending_changes[event.src_path] = time.time()

        if dest_supported:
            with self._pending_lock:
                self._pending_changes[event.dest_path] = time.time()

        if src_supported or dest_supported:
            self._schedule_process()

    def shutdown(self):
        """关闭处理器。"""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
