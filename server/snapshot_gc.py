"""Phase 8.7: Snapshot GC（垃圾回收）。

清理 daemon 不再需要的 snapshot 资源：

- **旧 generation 快照**：同一 workspace 发布新 generation 后，旧 generation
  的内存快照可回收（Rust ArcSwap 会在最后一个 reader 释放后自动 drop，
  但 Python 侧的 GraphStore 缓存需显式清理）
- **孤立 workspace 快照**：workspace 从 registry 注销后，其在 SnapshotCache
  中的快照应回收
- **过期 CAS blob**：CAS 中未被任何 snapshot 引用的 blob 应标记并清理
- **过期 snapshots 目录文件**：``data_root/snapshots/`` 下未被引用的快照
  二进制文件应回收

回收策略（与 db_gc.py 的 DEFAULT_GC_POLICY 保持一致）：
- ``retention_count``：每个 workspace 保留的最近 N 个 generation
- ``max_age_seconds``：超过此时间的 generation 标记为可回收
- ``dry_run``：只统计不删除（用于审计和预检）

设计原则（与 AGENTS.md 一致）：
- 读不锁，写才锁。``collect_garbage_stats`` 只读不删；``run_gc`` 才执行删除
- 回收前必须确认引用计数为 0（workspace 引用 + generation 引用）
- 回收操作分两阶段：mark（标记可回收）→ sweep（执行删除）
- 支持按 DB、按目录、按 SnapshotCache 三种作用域
- 失败不中断：单个 snapshot 回收失败不影响其他
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Callable

from .daemon_config import DaemonConfig


_GC_METHODS = {
    "delete_backup_history": "mcp.snapshot_gc.delete_backup_history_record",
    "delete_audit": "mcp.snapshot_gc.delete_expired_audit_logs",
    "delete_migration": "mcp.snapshot_gc.delete_migration_log_record",
    "registered_snapshots": "mcp.snapshot_gc.get_registered_snapshot_ids",
    "scan_audit": "mcp.snapshot_gc.scan_expired_audit_logs",
    "scan_backup": "mcp.snapshot_gc.scan_expired_backup_history",
    "scan_migration": "mcp.snapshot_gc.scan_expired_migrations_log",
    "scan_workspaces": "mcp.snapshot_gc.scan_orphaned_workspaces",
    "vacuum": "mcp.snapshot_gc.vacuum_databases",
}


def _call_daemon_rpc(method: str, params: Dict[str, Any]) -> Any:
    """经统一 HTTP client 调用 daemon；不打开本地数据库。"""
    from ._mcp_common import _call_daemon_rpc as _rpc

    return _rpc(method, params)


def _rpc_items(method: str, params: Dict[str, Any]) -> List["GarbageItem"]:
    result = _call_daemon_rpc(method, params)
    if not isinstance(result, list):
        raise RuntimeError(f"snapshot GC RPC returned invalid items: {result!r}")
    return [
        GarbageItem(
            item_type=str(item.get("item_type", "")),
            key=str(item.get("key", "")),
            size_bytes=int(item.get("size_bytes", 0)),
            reason=str(item.get("reason", "")),
            metadata=dict(item.get("metadata", {})),
        )
        for item in result
        if isinstance(item, dict)
    ]


@dataclass
class GCPolicy:
    """GC 策略配置。

    Attributes:
        retention_count: 每个 workspace 保留的最近 generation 数（默认 3）
        max_age_seconds: generation 超过此时间（秒）标记为可回收（默认 7 天）
        dry_run: 只统计不删除（默认 False）
        vacuum_db: 回收后是否 VACUUM 数据库（默认 False，因为耗时）
        batch_size: 单次 sweep 最多删除的文件数（默认 1000）
    """
    retention_count: int = 3
    max_age_seconds: int = 7 * 24 * 3600  # 7 天
    dry_run: bool = False
    vacuum_db: bool = False
    batch_size: int = 1000


@dataclass
class GarbageItem:
    """单个可回收项的描述。"""
    item_type: str  # "snapshot_file" / "cas_blob" / "generation" / "workspace_cache"
    path: str = ""
    key: str = ""
    size_bytes: int = 0
    reason: str = ""  # "orphaned" / "expired" / "old_generation" / "unregistered"
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class GCStats:
    """GC 结果统计。"""
    marked: List[GarbageItem] = field(default_factory=list)
    swept: List[GarbageItem] = field(default_factory=list)
    failed: List[Dict[str, Any]] = field(default_factory=list)
    total_marked_bytes: int = 0
    total_swept_bytes: int = 0
    duration_ms: int = 0

    @property
    def marked_count(self) -> int:
        return len(self.marked)

    @property
    def swept_count(self) -> int:
        return len(self.swept)

    @property
    def failed_count(self) -> int:
        return len(self.failed)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "marked_count": self.marked_count,
            "swept_count": self.swept_count,
            "failed_count": self.failed_count,
            "total_marked_bytes": self.total_marked_bytes,
            "total_swept_bytes": self.total_swept_bytes,
            "duration_ms": self.duration_ms,
            "dry_run": len(self.marked) > 0 and len(self.swept) == 0,
        }


class SnapshotGC:
    """Snapshot 垃圾回收器。

    用法::

        gc = SnapshotGC(cfg, policy=GCPolicy(retention_count=3))
        stats = gc.run_gc()
        print(f"回收 {stats.swept_count} 项，释放 {stats.total_swept_bytes} 字节")

    回收范围：
    1. ``data_root/snapshots/`` 目录下的孤立快照文件
    2. registry.db 中 backup_history 表的过期记录
    3. registry.db 中 schema_migrations_log 表的过期记录（保留最近 N 条）
    4. audit.db 中超过 max_age_seconds 的审计日志（可选，需 enable_audit_gc=True）
    5. SnapshotManagerService 中已注销 workspace 的缓存（通过回调）

    设计要点：
    - mark 阶段只读扫描，不修改任何数据
    - sweep 阶段执行删除，每个删除独立 try-except，失败记录到 failed 列表
    - dry_run=True 时只执行 mark，不执行 sweep
    """

    def __init__(self, cfg: DaemonConfig,
                 policy: Optional[GCPolicy] = None,
                 snapshot_cache_evictor: Optional[Callable[[str], bool]] = None,
                 enable_audit_gc: bool = False):
        """初始化 SnapshotGC。

        Args:
            cfg: DaemonConfig 实例
            policy: GC 策略（None 使用默认）
            snapshot_cache_evictor: 回调函数，用于从 SnapshotCache 中
                驱逐指定 workspace 的缓存。签名: (workspace_id) -> bool
            enable_audit_gc: 是否启用 audit 日志 GC（默认关闭，审计日志
                通常需要长期保留）
        """
        self.cfg = cfg
        self.policy = policy or GCPolicy()
        self._snapshot_cache_evictor = snapshot_cache_evictor
        self._enable_audit_gc = enable_audit_gc

    # ------------------------------------------------------------------
    # mark 阶段（只读扫描）
    # ------------------------------------------------------------------

    def collect_garbage_stats(self) -> List[GarbageItem]:
        """扫描所有可回收项（只读，不删除）。

        Returns:
            可回收项列表
        """
        items: List[GarbageItem] = []
        items.extend(self._scan_orphaned_snapshot_files())
        items.extend(self._scan_expired_backup_history())
        items.extend(self._scan_expired_migrations_log())
        if self._enable_audit_gc:
            items.extend(self._scan_expired_audit_logs())
        items.extend(self._scan_orphaned_workspaces())
        return items

    def _scan_orphaned_snapshot_files(self) -> List[GarbageItem]:
        """扫描 snapshots 目录下的孤立文件。

        孤立文件定义：
        - 文件名不在 registry.db 的 snapshot_id 列表中
        - 文件修改时间超过 max_age_seconds
        """
        snapshot_dir = os.path.join(self.cfg.data_root, "snapshots")
        if not os.path.isdir(snapshot_dir):
            return []

        # 获取所有已注册的 snapshot_id
        registered_snapshots = self._get_registered_snapshot_ids()

        items: List[GarbageItem] = []
        now = time.time()
        cutoff = now - self.policy.max_age_seconds

        for entry in os.listdir(snapshot_dir):
            full_path = os.path.join(snapshot_dir, entry)
            if not os.path.isfile(full_path):
                continue

            # 检查是否已注册
            if entry in registered_snapshots:
                continue

            # 检查修改时间
            try:
                mtime = os.path.getmtime(full_path)
                if mtime > cutoff:
                    continue  # 未过期
            except OSError:
                continue

            size = os.path.getsize(full_path) if os.path.exists(full_path) else 0
            items.append(GarbageItem(
                item_type="snapshot_file",
                path=full_path,
                key=entry,
                size_bytes=size,
                reason="orphaned",
                metadata={"mtime": mtime, "age_seconds": now - mtime},
            ))

        return items

    def _scan_expired_backup_history(self) -> List[GarbageItem]:
        """扫描 registry.db 中已标记删除或过期的 backup_history 记录。"""
        return _rpc_items(_GC_METHODS["scan_backup"], {
            "registry_db_path": self.cfg.registry_db_path,
            "max_age_seconds": self.policy.max_age_seconds,
        })

    def _scan_expired_migrations_log(self) -> List[GarbageItem]:
        """扫描 schema_migrations_log 表中的过期记录。

        保留最近 ``retention_count * 10`` 条记录（每个 DB 通常有少量迁移）。
        """
        return _rpc_items(_GC_METHODS["scan_migration"], {
            "registry_db_path": self.cfg.registry_db_path,
            "retention_count": self.policy.retention_count,
        })

    def _scan_expired_audit_logs(self) -> List[GarbageItem]:
        """扫描 audit.db 中过期的审计日志。

        注意：审计日志通常需要长期保留（合规要求），此方法仅在
        enable_audit_gc=True 时调用。
        """
        if not self.cfg.audit_log_path or not os.path.isfile(self.cfg.audit_log_path):
            return []
        return _rpc_items(_GC_METHODS["scan_audit"], {
            "audit_db_path": self.cfg.audit_log_path,
            "max_age_seconds": self.policy.max_age_seconds,
        })

    def _scan_orphaned_workspaces(self) -> List[GarbageItem]:
        """扫描 registry.db 中状态为 archived 的 workspace，标记为可回收。

        这些 workspace 的 SnapshotCache 条目应被驱逐。
        不删除 registry.db 中的记录（保留审计轨迹），只驱逐内存缓存。
        """
        return _rpc_items(_GC_METHODS["scan_workspaces"], {
            "registry_db_path": self.cfg.registry_db_path,
            "max_age_seconds": self.policy.max_age_seconds,
        })

    def _get_registered_snapshot_ids(self) -> set:
        """获取 registry.db 中所有已注册的 snapshot_id。"""
        result = _call_daemon_rpc(_GC_METHODS["registered_snapshots"], {
            "registry_db_path": self.cfg.registry_db_path,
        })
        if not isinstance(result, list):
            raise RuntimeError(f"snapshot GC RPC returned invalid snapshot IDs: {result!r}")
        return {str(value) for value in result}

    # ------------------------------------------------------------------
    # sweep 阶段（执行删除）
    # ------------------------------------------------------------------

    def run_gc(self) -> GCStats:
        """执行完整的 GC 流程：mark → sweep。

        Returns:
            GCStats 统计结果
        """
        start = time.time()
        stats = GCStats()

        # mark
        items = self.collect_garbage_stats()
        stats.marked = items
        stats.total_marked_bytes = sum(i.size_bytes for i in items)

        # sweep（dry_run 时跳过）
        if self.policy.dry_run:
            stats.duration_ms = int((time.time() - start) * 1000)
            return stats

        batch_count = 0
        for item in items:
            if batch_count >= self.policy.batch_size:
                break
            try:
                self._sweep_item(item)
                stats.swept.append(item)
                stats.total_swept_bytes += item.size_bytes
                batch_count += 1
            except Exception as e:
                stats.failed.append({
                    "item": item.key or item.path,
                    "error": f"{type(e).__name__}: {e}",
                })

        # 可选 VACUUM
        if self.policy.vacuum_db and not self.policy.dry_run:
            self._vacuum_databases()

        stats.duration_ms = int((time.time() - start) * 1000)
        return stats

    def _sweep_item(self, item: GarbageItem) -> None:
        """执行单个回收项的删除。"""
        if item.item_type == "snapshot_file":
            if os.path.exists(item.path):
                os.remove(item.path)
        elif item.item_type == "backup_history":
            self._delete_backup_history_record(item.key)
        elif item.item_type == "migration_log":
            self._delete_migration_log_record(item.key)
        elif item.item_type == "audit_log":
            self._delete_expired_audit_logs(item.metadata.get("cutoff", 0))
        elif item.item_type == "workspace_cache":
            if self._snapshot_cache_evictor is not None:
                self._snapshot_cache_evictor(item.key)

    def _delete_backup_history_record(self, backup_id: str) -> None:
        """从 backup_history 表删除记录。"""
        _call_daemon_rpc(_GC_METHODS["delete_backup_history"], {
            "registry_db_path": self.cfg.registry_db_path,
            "backup_id": backup_id,
        })

    def _delete_migration_log_record(self, log_id: str) -> None:
        """从 schema_migrations_log 表删除记录。"""
        _call_daemon_rpc(_GC_METHODS["delete_migration"], {
            "registry_db_path": self.cfg.registry_db_path,
            "log_id": int(log_id),
        })

    def _delete_expired_audit_logs(self, cutoff: float) -> None:
        """删除过期的审计日志。"""
        audit_path = self.cfg.audit_log_path
        if not audit_path or not os.path.isfile(audit_path):
            return
        _call_daemon_rpc(_GC_METHODS["delete_audit"], {
            "audit_db_path": audit_path,
            "cutoff": cutoff,
        })

    def _vacuum_databases(self) -> None:
        """VACUUM 所有 daemon 管理的数据库。"""
        audit_path = self.cfg.audit_log_path
        params: Dict[str, Any] = {"registry_db_path": self.cfg.registry_db_path}
        if audit_path and os.path.isfile(audit_path):
            params["audit_db_path"] = audit_path
        _call_daemon_rpc(_GC_METHODS["vacuum"], params)

    # ------------------------------------------------------------------
    # 便捷方法
    # ------------------------------------------------------------------

    def get_stats_summary(self) -> Dict[str, Any]:
        """获取 GC 相关统计信息（只读，不执行 GC）。"""
        items = self.collect_garbage_stats()
        by_type: Dict[str, int] = {}
        total_bytes = 0
        for item in items:
            by_type[item.item_type] = by_type.get(item.item_type, 0) + 1
            total_bytes += item.size_bytes
        return {
            "total_items": len(items),
            "by_type": by_type,
            "total_bytes": total_bytes,
            "policy": {
                "retention_count": self.policy.retention_count,
                "max_age_seconds": self.policy.max_age_seconds,
                "dry_run": self.policy.dry_run,
            },
        }
