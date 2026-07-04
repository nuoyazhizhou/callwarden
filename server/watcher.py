"""
watcher.py
==========

文件监控模块：监控项目目录的文件变化，自动增量刷新代码知识图谱。
支持多语言、文件创建/修改/删除/重命名、防抖批量处理。
"""

import os
import time
import threading
from collections import defaultdict
from typing import Dict, List, Set

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler, FileModifiedEvent, FileCreatedEvent, FileDeletedEvent, FileMovedEvent
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False

from ..config import PROJECT_ROOT, norm_path, get_supported_extensions, detect_language_from_path
from ..i18n import t


class FileWatcher:
    """文件监控器：监听项目目录变化，增量更新知识图谱"""

    def __init__(self, db, watch_dir: str = None):
        self.db = db
        # 优先使用 db 的 workspace_root，其次是传入的 watch_dir，最后用 PROJECT_ROOT
        self.watch_dir = watch_dir or db.workspace_root or PROJECT_ROOT
        self._observer = None
        self._handler = None
        self._supported_exts = get_supported_extensions()

    def start(self):
        """启动文件监控"""
        if not HAS_WATCHDOG:
            print(t("cli.messages.watcher_no_watchdog"))
            return False

        self._handler = _ChangeHandler(self.db, self._supported_exts)

        self._observer = Observer()
        self._observer.schedule(self._handler, self.watch_dir, recursive=True)
        self._observer.start()

        print(t("cli.messages.watcher_started", dir=self.watch_dir))
        print(t("cli.messages.watcher_listening_langs", langs=', '.join(sorted(set(detect_language_from_path('test.' + e) for e in self._supported_exts if e)))))
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

    def stop(self):
        """停止监控"""
        if self._observer:
            self._observer.stop()
            self._observer.join()
            self._observer = None
        if self._handler:
            self._handler.shutdown()
            self._handler = None


class _ChangeHandler(FileSystemEventHandler):
    """文件变化处理器，带防抖和批量处理"""

    def __init__(self, db, supported_exts: Set[str]):
        self.db = db
        self.supported_exts = supported_exts
        self._debounce_time = 1.0  # 1 秒防抖
        self._pending_changes: Dict[str, float] = {}  # path -> 最后变更时间
        self._pending_lock = threading.Lock()
        self._timer = None
        self._running = True

    def _is_supported(self, path: str) -> bool:
        """检查文件是否在支持的语言列表中"""
        if not path:
            return False
        ext = os.path.splitext(path)[1].lower()
        return ext in self.supported_exts

    def _schedule_process(self):
        """调度批量处理（防抖）"""
        if self._timer:
            self._timer.cancel()

        self._timer = threading.Timer(self._debounce_time, self._process_pending)
        self._timer.daemon = True
        self._timer.start()

    def _process_pending(self):
        """批量处理待处理的文件变更"""
        with self._pending_lock:
            if not self._pending_changes:
                return

            now = time.time()
            # 只处理已经稳定的变更（距离最后变更时间超过 debounce）
            ready = {
                p: t for p, t in self._pending_changes.items()
                if now - t >= self._debounce_time
            }
            # 保留还在抖动的
            self._pending_changes = {
                p: t for p, t in self._pending_changes.items()
                if now - t < self._debounce_time
            }

        if not ready:
            # 还有未稳定的，继续等
            if self._pending_changes:
                self._schedule_process()
            return

        # 按变更类型分组处理
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

        # 如果还有未处理的，继续调度
        with self._pending_lock:
            if self._pending_changes:
                self._schedule_process()

    def _handle_modified(self, paths: List[str]):
        """处理文件修改/创建"""
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
        """处理文件删除"""
        print(t("cli.messages.watcher_deleted_detected", count=len(paths)))
        for path in paths:
            try:
                rel_path = os.path.relpath(path, self.db.workspace_root)
                rel_path = norm_path(rel_path)
                self.db.remove_file(rel_path)
                print(t("cli.messages.watcher_delete_item", path=rel_path))
            except Exception as e:
                print(t("cli.messages.watcher_delete_fail", path=path, error=e))
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
        """文件重命名/移动"""
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
        """关闭处理器"""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
