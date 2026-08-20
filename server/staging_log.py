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
# Phase 3-4-5 wire-production: Rust 短路
# ============================================
# StagingLog 默认走 Rust PyO3 API（callwarden_core.staging_log_*），
# rollback_config 中 feature=rust_staging_log 置为 1 时回退 Python。
# Rust 失败时 fail-soft 降级到 Python 路径（与 Phase 2-6 模式一致）。

_RUST_STAGING_LOG_AVAILABLE = False
_callwarden_core = None
try:
    import callwarden_core as _callwarden_core  # type: ignore
    _RUST_STAGING_LOG_AVAILABLE = True
except ImportError:
    _callwarden_core = None

# rollback_config 查询缓存（60s TTL，避免每次方法调用都打开 DB）
_ROLLBACK_CACHE: Dict[str, float] = {"ts": 0.0, "value": False}
_ROLLBACK_CACHE_TTL = 60.0


def _is_rust_staging_log_rolled_back() -> bool:
    """检查 rust_staging_log feature 是否已回滚（60s 缓存）

    StagingLog 是独立类（非 CodeGraphDB Mixin），无法用 self.is_feature_rolled_back。
    通过短连接查询 rollback_config 表，结果缓存 60s 避免频繁开 DB。
    """
    now = time.time()
    if now - _ROLLBACK_CACHE["ts"] < _ROLLBACK_CACHE_TTL:
        return _ROLLBACK_CACHE["value"]  # type: ignore[return-value]
    try:
        import sqlite3 as _sqlite3
        from callwarden.config import DB_PATH as _DB_PATH
        conn = _sqlite3.connect(_DB_PATH)
        try:
            cur = conn.execute(
                "SELECT rollback_flag FROM rollback_config WHERE feature_name = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                ("rust_staging_log",),
            )
            row = cur.fetchone()
            value = bool(row and row[0] == 1)
        finally:
            conn.close()
    except Exception:
        value = False
    _ROLLBACK_CACHE["ts"] = now
    _ROLLBACK_CACHE["value"] = value
    return value


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
    operation: str = "refresh"             # refresh / delete（C5 C3：对齐 Rust
                                           # StagingEntry.operation，旧日志缺省按 refresh）
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
            operation=data.get("operation", "refresh"),
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

        Phase 3-4-5 wire-production：默认走 Rust ``staging_log_append`` 短路。
        rollback_config 中 feature=rust_staging_log 置为 1 时回退 Python。
        Rust 失败时 fail-soft 降级到 Python 路径。
        """
        # Phase 3-4-5 wire-production: Rust 短路
        if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
            try:
                lsn = _callwarden_core.staging_log_append(self.log_path, entry.to_json_line())
                entry.lsn = lsn
                if lsn >= self._next_lsn:
                    self._next_lsn = lsn + 1
                return lsn
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
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

        Phase 3-4-5 wire-production：默认走 Rust ``staging_log_read`` 短路。
        """
        # Phase 3-4-5 wire-production: Rust 短路
        if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
            try:
                json_str = _callwarden_core.staging_log_read(self.log_path, since_lsn)
                data = json.loads(json_str)
                return [StagingEntry.from_dict(d) for d in data]
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
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
        """读取所有 status=pending 的 entries

        Phase 3-4-5 wire-production：默认走 Rust ``staging_log_read_pending`` 短路
        （Rust 在文件读取时直接过滤，比 Python 读全部再过滤更高效）。
        """
        # Phase 3-4-5 wire-production: Rust 短路
        if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
            try:
                json_str = _callwarden_core.staging_log_read_pending(self.log_path)
                data = json.loads(json_str)
                return [StagingEntry.from_dict(d) for d in data]
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
        return [e for e in self.read() if e.status == "pending"]

    def mark_applied(self, lsn: int):
        """标记指定 LSN 的 entry 为 applied

        Phase 3-4-5 wire-production：走 Rust ``staging_log_mark_applied_batch([lsn])`` 短路。
        """
        # Phase 3-4-5 wire-production: Rust 短路
        if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
            try:
                _callwarden_core.staging_log_mark_applied_batch(self.log_path, [lsn])
                return
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
        self._update_status(lsn, "applied")

    def mark_applied_batch(self, lsns: List[int]):
        """批量标记多个 LSN 为 applied——单次文件重写。

        修复 T-1783952125417-7a09：减少 mark_applied 逐条重写整个文件的开销。

        Phase 3-4-5 wire-production：走 Rust ``staging_log_mark_applied_batch`` 短路。
        """
        if not lsns:
            return
        # Phase 3-4-5 wire-production: Rust 短路
        if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
            try:
                _callwarden_core.staging_log_mark_applied_batch(self.log_path, lsns)
                return
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
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
        """标记指定 LSN 的 entry 为 failed

        Phase 3-4-5 wire-production：走 Rust ``staging_log_mark_failed`` 短路。
        """
        # Phase 3-4-5 wire-production: Rust 短路
        if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
            try:
                _callwarden_core.staging_log_mark_failed(self.log_path, lsn, error)
                return
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
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

        Phase 3-4-5 wire-production：走 Rust ``staging_log_truncate`` 短路。
        """
        # Phase 3-4-5 wire-production: Rust 短路
        if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
            try:
                _callwarden_core.staging_log_truncate(self.log_path, up_to_lsn)
                return
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
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

        Phase 3-4-5 wire-production：走 Rust ``staging_log_compact_applied`` 短路。
        """
        # Phase 3-4-5 wire-production: Rust 短路
        if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
            try:
                _callwarden_core.staging_log_compact_applied(self.log_path, workspace_id)
                return
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
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
        """返回 log 统计信息

        Phase 3-4-5 wire-production：走 Rust ``staging_log_stats`` 短路
        （Rust 直接统计，无需 Python 读取+过滤全部 entries）。
        """
        # Phase 3-4-5 wire-production: Rust 短路
        if _RUST_STAGING_LOG_AVAILABLE and not _is_rust_staging_log_rolled_back():
            try:
                json_str = _callwarden_core.staging_log_stats(self.log_path)
                stats = json.loads(json_str)
                # 同步 _next_lsn（Rust 端可能已推进）
                rust_next = stats.get("next_lsn", 0)
                if rust_next > self._next_lsn:
                    self._next_lsn = rust_next
                return stats
            except Exception:
                pass  # fail-soft → 降级 Python 路径
        # Python 降级路径
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
    operation: str = "refresh",  # C5 C3：refresh / delete，对齐 Rust StagingEntry.operation
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
        operation=operation,
        parse_delta=parse_delta or {},
        resolve_delta=resolve_delta or {},
        frontier=frontier or {},
        metrics_update=metrics_update or {},
    )
