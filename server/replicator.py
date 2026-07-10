"""
Phase 5.7: Replicator 合并 delta 并发布新 generation

设计参考：enterprise-daemon-shared-snapshot-plan.md §6.1, §9.1

Replicator 是 Coordinator 的一部分，负责：
1. 从 staging log 读取 pending entries
2. 合并 delta（parse_delta + resolve_delta + frontier + metrics_update）
3. 发布新的 GraphSnapshot generation（通过 SnapshotManagerService）
4. 标记 entries 为 applied，截断 log

daemon crash 后，Replicator 可从 staging log 恢复未应用的 entries 并重新发布。
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

from server.staging_log import StagingLog, StagingEntry

logger = logging.getLogger(__name__)


# ============================================
# 数据结构
# ============================================

@dataclass
class ReplicationResult:
    """单次 replication 的结果"""
    success: bool = True
    workspace_id: str = ""
    generation: int = 0
    applied_lsns: List[int] = field(default_factory=list)
    pending_count: int = 0
    applied_count: int = 0
    error: Optional[str] = None
    duration_ms: float = 0.0

    def summary(self) -> str:
        status = "ok" if self.success else "failed"
        return (
            f"ReplicationResult({status}, ws={self.workspace_id}, "
            f"gen={self.generation}, {self.applied_count}/{self.pending_count} applied)"
        )


# ============================================
# Replicator —— 合并 delta 并发布新 generation
# ============================================

class Replicator:
    """
    合并 staging log 中的 delta 并发布新 generation。

    用法：
        replicator = Replicator(staging_log, snapshot_service)
        result = replicator.replicate("ws_abc", db_path="/path/to/callwarden.db")

    Replicator 是单线程的（由 Coordinator 调用），不需要内部锁。
    """

    def __init__(
        self,
        staging_log: StagingLog,
        snapshot_service=None,
    ):
        """
        初始化 Replicator。

        参数：
            staging_log: StagingLog 实例
            snapshot_service: SnapshotManagerService 实例（None 时只更新 log，不发布 snapshot）
        """
        self.staging_log = staging_log
        self.snapshot_service = snapshot_service
        self._lock = threading.Lock()

    def replicate(
        self,
        workspace_id: str,
        db_path: str = "",
        build_context_hash: str = "",
    ) -> ReplicationResult:
        """
        执行一次 replication：读取 pending → 发布新 generation → 标记 applied。

        参数：
            workspace_id: workspace 实例 ID
            db_path: SQLite 数据库路径（用于 publish_snapshot）
            build_context_hash: build context 哈希

        返回：ReplicationResult
        """
        start_time = time.time()
        result = ReplicationResult(workspace_id=workspace_id)

        with self._lock:
            # 1. 读取 pending entries
            all_pending = self.staging_log.read_pending()
            # 过滤当前 workspace 的 entries
            pending = [e for e in all_pending if e.workspace_id == workspace_id]
            result.pending_count = len(pending)

            if not pending:
                logger.debug("no pending entries for ws=%s", workspace_id)
                result.duration_ms = (time.time() - start_time) * 1000
                return result

            logger.info(
                "replicating %d pending entries for ws=%s",
                len(pending), workspace_id,
            )

            # 2. 合并 delta（目前简单汇总，实际可做更复杂的 merge）
            merged = self._merge_deltas(pending)

            # 3. 发布新 generation
            if self.snapshot_service is not None and db_path:
                try:
                    pub_result = self.snapshot_service.publish_snapshot(
                        workspace_instance_id=workspace_id,
                        db_path=db_path,
                        build_context_hash=build_context_hash,
                    )
                    if pub_result is not None:
                        result.generation = pub_result.get("generation", 0)
                    else:
                        logger.warning("publish_snapshot returned None, Rust backend unavailable")
                except Exception as e:
                    result.success = False
                    result.error = f"publish failed: {e}"
                    logger.error("publish_snapshot failed for ws=%s: %s", workspace_id, e)

                    # 标记 entries 为 failed
                    for entry in pending:
                        self.staging_log.mark_failed(entry.lsn, str(e))
                    result.duration_ms = (time.time() - start_time) * 1000
                    return result

            # 4. 标记 entries 为 applied
            for entry in pending:
                self.staging_log.mark_applied(entry.lsn)
                result.applied_lsns.append(entry.lsn)

            result.applied_count = len(result.applied_lsns)

            # 5. 压缩已应用的 entries（按 status 而非 LSN，避免误删其他 workspace）
            if result.applied_lsns:
                self.staging_log.compact_applied(workspace_id)

            result.duration_ms = (time.time() - start_time) * 1000
            logger.info(
                "replication done for ws=%s: gen=%d, %d applied in %.1fms",
                workspace_id, result.generation, result.applied_count, result.duration_ms,
            )

        return result

    def _merge_deltas(self, entries: List[StagingEntry]) -> Dict[str, Any]:
        """
        合并多个 staging entries 的 delta。

        目前是简单汇总，实际可做更复杂的 merge（如冲突检测、去重等）。

        参数：
            entries: 待合并的 entries

        返回：合并后的 delta summary
        """
        merged = {
            "files": [],
            "total_added_symbols": 0,
            "total_removed_symbols": 0,
            "total_changed_symbols": 0,
            "total_added_edges": 0,
            "total_removed_edges": 0,
        }

        for entry in entries:
            merged["files"].append({
                "file_path": entry.file_path,
                "content_hash": entry.content_hash,
                "language": entry.language,
            })

            parse_delta = entry.parse_delta or {}
            symbol_delta = parse_delta.get("symbol_delta", {})
            merged["total_added_symbols"] += len(symbol_delta.get("added", []))
            merged["total_removed_symbols"] += len(symbol_delta.get("removed", []))
            merged["total_changed_symbols"] += len(symbol_delta.get("changed", []))

            resolve_delta = entry.resolve_delta or {}
            merged["total_added_edges"] += len(resolve_delta.get("added", []))
            merged["total_removed_edges"] += len(resolve_delta.get("removed", []))

        return merged

    def recover(self, workspace_id: str, db_path: str = "") -> ReplicationResult:
        """
        从 crash 恢复：读取所有 pending entries 并重新 replication。

        在 daemon 启动时调用。

        参数：
            workspace_id: workspace 实例 ID
            db_path: SQLite 数据库路径

        返回：ReplicationResult
        """
        logger.info("recovering from staging log for ws=%s", workspace_id)
        return self.replicate(workspace_id, db_path)

    def get_pending_count(self, workspace_id: Optional[str] = None) -> int:
        """
        获取 pending entries 数量。

        参数：
            workspace_id: 如果指定，只返回该 workspace 的 pending 数量

        返回：pending 数量
        """
        pending = self.staging_log.read_pending()
        if workspace_id:
            return sum(1 for e in pending if e.workspace_id == workspace_id)
        return len(pending)

    def __repr__(self) -> str:
        return f"Replicator(staging_log={self.staging_log})"
