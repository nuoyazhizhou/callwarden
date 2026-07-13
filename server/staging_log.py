"""
Phase 5.6: Staging Durable Log

设计参考：enterprise-daemon-shared-snapshot-plan.md §6.1, §9.1

Worker 产生的 delta（parse_delta + resolve_delta + frontier + metrics_update）
先写入 staging log，再由 Replicator 合并并发布新 generation。
daemon crash 后可从 staging log 恢复未应用的 delta。

存储格式：JSON Lines（每行一条 entry），append-only，崩溃安全。
"""

import json
import os
import time
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List, Optional, Dict, Any


# ============================================
# 数据结构
# ============================================

@dataclass
class StagingEntry:
    """单条 staging 记录"""
    lsn: int                              # log sequence number（单调递增）
    timestamp: float                      # 时间戳（epoch seconds）
    workspace_id: str                      # workspace ID
    file_path: str                         # 变更文件路径
    content_hash: str                      # 文件内容 SHA-256
    language: str                          # 语言 ID
    parse_delta: Dict[str, Any] = field(default_factory=dict)      # 序列化的 ParseDelta
    resolve_delta: Dict[str, Any] = field(default_factory=dict)    # 序列化的 ResolveDelta
    frontier: Dict[str, Any] = field(default_factory=dict)         # 序列化的 AffectedFrontier
    metrics_update: Dict[str, Any] = field(default_factory=dict)    # 序列化的 LocalMetricsUpdate
    status: str = "pending"                # pending / applied / failed
    error: Optional[str] = None            # 失败原因（status=failed 时）

    def to_dict(self) -> Dict[str, Any]:
        """序列化为 dict（用于 JSON）"""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "StagingEntry":
        """从 dict 反序列化"""
        return cls(
            lsn=data["lsn"],
            timestamp=data["timestamp"],
            workspace_id=data["workspace_id"],
            file_path=data["file_path"],
            content_hash=data["content_hash"],
            language=data["language"],
            parse_delta=data.get("parse_delta", {}),
            resolve_delta=data.get("resolve_delta", {}),
            frontier=data.get("frontier", {}),
            metrics_update=data.get("metrics_update", {}),
            status=data.get("status", "pending"),
            error=data.get("error"),
        )

    def to_json_line(self) -> str:
        """序列化为 JSON line（单行 JSON）"""
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json_line(cls, line: str) -> "StagingEntry":
        """从 JSON line 反序列化"""
        return cls.from_dict(json.loads(line))

    def summary(self) -> str:
        """变更摘要"""
        return (
            f"StagingEntry(lsn={self.lsn}, {self.file_path}, "
            f"{self.language}, status={self.status})"
        )


# ============================================
# StagingLog —— 持久化 staging log
# ============================================

class StagingLog:
    """
    持久化 staging log，记录 delta 变更。

    - Append-only：新 entry 追加到文件末尾
    - JSON Lines：每行一条 entry，崩溃安全（部分写入的行会被跳过）
    - LSN：单调递增的 log sequence number
    - Truncate：Replicator 应用后可截断已应用的 entries

    用法：
        log = StagingLog("~/.callwarden/<hash>/staging.log")
        lsn = log.append(entry)
        entries = log.read(since_lsn=0)
        log.mark_applied(lsn)
        log.truncate(applied_lsn)
    """

    def __init__(self, log_path: str):
        """
        初始化 staging log。

        参数：
            log_path: log 文件路径
        """
        self.log_path = str(log_path)
        self._lock = threading.Lock()
        self._next_lsn = 1

        # 确保目录存在
        log_dir = os.path.dirname(self.log_path)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        # 如果 log 文件已存在，恢复 next_lsn
        self._recover_lsn()

    def _recover_lsn(self):
        """从现有 log 文件恢复 next_lsn"""
        if not os.path.exists(self.log_path):
            return

        max_lsn = 0
        try:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = StagingEntry.from_json_line(line)
                        if entry.lsn > max_lsn:
                            max_lsn = entry.lsn
                    except (json.JSONDecodeError, KeyError):
                        # 跳过损坏的行（部分写入）
                        continue
        except IOError:
            pass

        self._next_lsn = max_lsn + 1

    def append(self, entry: StagingEntry) -> int:
        """
        追加一条 staging entry。

        参数：
            entry: 要追加的 entry（lsn 会被自动分配）

        返回：分配的 LSN
        """
        with self._lock:
            entry.lsn = self._next_lsn
            entry.timestamp = entry.timestamp or time.time()
            self._next_lsn += 1

            line = entry.to_json_line() + "\n"
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())

            return entry.lsn

    def read(self, since_lsn: int = 0) -> List[StagingEntry]:
        """
        读取从 since_lsn 开始的所有 entries。

        参数：
            since_lsn: 起始 LSN（不包含）

        返回：entries 列表（按 LSN 升序）
        """
        entries = []
        if not os.path.exists(self.log_path):
            return entries

        with self._lock:
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = StagingEntry.from_json_line(line)
                        if entry.lsn > since_lsn:
                            entries.append(entry)
                    except (json.JSONDecodeError, KeyError):
                        continue

        return entries

    def read_pending(self) -> List[StagingEntry]:
        """读取所有 status=pending 的 entries"""
        return [e for e in self.read() if e.status == "pending"]

    def mark_applied(self, lsn: int):
        """标记指定 LSN 的 entry 为 applied"""
        self._update_status(lsn, "applied")

    def mark_applied_batch(self, lsns: List[int]):
        """批量标记多个 LSN 为 applied——单次文件重写。

        修复 T-1783952125417-7a09：减少 mark_applied 逐条重写整个文件的开销。
        """
        if not lsns:
            return
        target_lsns = set(lsns)
        with self._lock:
            entries = []
            if os.path.exists(self.log_path):
                with open(self.log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = StagingEntry.from_json_line(line)
                            if entry.lsn in target_lsns:
                                entry.status = "applied"
                            entries.append(entry)
                        except (json.JSONDecodeError, KeyError):
                            continue
            self._rewrite(entries)

    def mark_failed(self, lsn: int, error: str):
        """标记指定 LSN 的 entry 为 failed"""
        self._update_status(lsn, "failed", error)

    def _update_status(self, lsn: int, status: str, error: Optional[str] = None):
        """更新指定 LSN 的 entry 状态"""
        with self._lock:
            # 读取所有 entries
            entries = []
            with open(self.log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = StagingEntry.from_json_line(line)
                        if entry.lsn == lsn:
                            entry.status = status
                            if error:
                                entry.error = error
                        entries.append(entry)
                    except (json.JSONDecodeError, KeyError):
                        continue

            # 重写整个文件
            self._rewrite(entries)

    def truncate(self, up_to_lsn: int):
        """
        截断 log，删除所有 LSN <= up_to_lsn 的 entries。

        参数：
            up_to_lsn: 截断到的 LSN（包含）
        """
        with self._lock:
            entries = []
            if os.path.exists(self.log_path):
                with open(self.log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = StagingEntry.from_json_line(line)
                            if entry.lsn > up_to_lsn:
                                entries.append(entry)
                        except (json.JSONDecodeError, KeyError):
                            continue

            self._rewrite(entries)

    def compact_applied(self, workspace_id: Optional[str] = None):
        """
        压缩 log，删除所有 status=applied 的 entries。

        与 truncate 不同，compact_applied 按 status 过滤而非 LSN，
        避免误删其他 workspace 的 pending entries。

        参数：
            workspace_id: 如果指定，只删除该 workspace 的 applied entries
        """
        with self._lock:
            entries = []
            if os.path.exists(self.log_path):
                with open(self.log_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = StagingEntry.from_json_line(line)
                            # 保留非 applied 的，或不是目标 workspace 的 applied
                            if entry.status == "applied":
                                if workspace_id is None or entry.workspace_id == workspace_id:
                                    continue  # 删除
                            entries.append(entry)
                        except (json.JSONDecodeError, KeyError):
                            continue

            self._rewrite(entries)

    def _rewrite(self, entries: List[StagingEntry]):
        """重写整个 log 文件"""
        tmp_path = self.log_path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            for entry in entries:
                f.write(entry.to_json_line() + "\n")
            f.flush()
            os.fsync(f.fileno())

        # 原子替换
        os.replace(tmp_path, self.log_path)

    def stats(self) -> Dict[str, Any]:
        """返回 log 统计信息"""
        entries = self.read()
        pending = [e for e in entries if e.status == "pending"]
        applied = [e for e in entries if e.status == "applied"]
        failed = [e for e in entries if e.status == "failed"]

        return {
            "total_entries": len(entries),
            "pending": len(pending),
            "applied": len(applied),
            "failed": len(failed),
            "next_lsn": self._next_lsn,
            "log_path": self.log_path,
        }

    def close(self):
        """关闭 log（目前无资源需要释放，保留接口）"""
        pass

    def __repr__(self) -> str:
        return f"StagingLog(path={self.log_path}, next_lsn={self._next_lsn})"


# ============================================
# 辅助函数
# ============================================

def create_staging_entry(
    workspace_id: str,
    file_path: str,
    content_hash: str,
    language: str,
    parse_delta: Optional[Dict[str, Any]] = None,
    resolve_delta: Optional[Dict[str, Any]] = None,
    frontier: Optional[Dict[str, Any]] = None,
    metrics_update: Optional[Dict[str, Any]] = None,
) -> StagingEntry:
    """
    创建 StagingEntry（不写入 log）。

    用法：
        entry = create_staging_entry(...)
        lsn = log.append(entry)
    """
    return StagingEntry(
        lsn=0,  # 由 log.append 分配
        timestamp=time.time(),
        workspace_id=workspace_id,
        file_path=file_path,
        content_hash=content_hash,
        language=language,
        parse_delta=parse_delta or {},
        resolve_delta=resolve_delta or {},
        frontier=frontier or {},
        metrics_update=metrics_update or {},
    )
