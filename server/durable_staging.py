"""Durable Staging SQLite WAL 状态机。

任务：T-1783974522648-e2d3 Step #3
规范：enterprise-watcher-benefit-production-plan.md §3.4

替代现有 JSONL staging log（每条 mark_applied 重写整个文件）。
使用 SQLite WAL 模式，支持：
- 状态机：pending → applying → applied 或 pending/applying → failed
- 崩溃恢复：daemon 启动时扫描 pending/applying entries
- 幂等重放：已提交 manifest 的 entry 补记 applied，未提交的重放
"""

import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ============================================
# Schema
# ============================================

STAGING_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS staging_entries (
    lsn INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    session_epoch INTEGER NOT NULL,
    monotonic_seq INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    content_hash TEXT DEFAULT '',
    delta_blob BLOB NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending',
    created_at REAL NOT NULL,
    applied_generation INTEGER DEFAULT 0,
    error TEXT DEFAULT NULL,
    UNIQUE(workspace_id, rel_path, session_epoch, monotonic_seq)
);

CREATE INDEX IF NOT EXISTS idx_staging_state
    ON staging_entries(state);
CREATE INDEX IF NOT EXISTS idx_staging_workspace
    ON staging_entries(workspace_id, state);
"""

# 允许的状态转换
VALID_TRANSITIONS = {
    "pending": {"applying", "failed"},
    "applying": {"applied", "failed"},
    "applied": set(),    # 终态
    "failed": set(),     # 终态
}


@dataclass
class StagingWALEntry:
    """WAL staging entry。"""
    lsn: int = 0
    workspace_id: str = ""
    rel_path: str = ""
    session_epoch: int = 0
    monotonic_seq: int = 0
    event_kind: str = "modify"
    content_hash: str = ""
    delta_blob: bytes = b""
    state: str = "pending"
    created_at: float = 0.0
    applied_generation: int = 0
    error: Optional[str] = None


class DurableStagingLog:
    """SQLite WAL 持久化 staging log。

    用法：
        log = DurableStagingLog("/path/to/staging.db")
        lsn = log.append(entry)
        log.transition(lsn, "applying")
        log.transition(lsn, "applied", generation=42)
        pending = log.read_pending()
        log.recover()  # daemon 启动恢复
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        self._conn = sqlite3.connect(db_path, timeout=10.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._init_schema()

    def _init_schema(self):
        """初始化 schema。"""
        self._conn.executescript(STAGING_SCHEMA_DDL)
        self._conn.commit()

    def append(
        self,
        workspace_id: str,
        rel_path: str,
        session_epoch: int,
        monotonic_seq: int,
        event_kind: str,
        content_hash: str = "",
        delta_blob: bytes = b"",
    ) -> int:
        """追加一条 staging entry。返回 LSN。

        delta 在响应成功前提交 WAL。
        """
        now = time.time()
        delta_json = json.dumps(delta_blob if isinstance(delta_blob, dict) else {
            "raw": delta_blob.hex() if isinstance(delta_blob, bytes) else str(delta_blob)
        }).encode("utf-8")

        cur = self._conn.execute(
            "INSERT OR REPLACE INTO staging_entries "
            "(workspace_id, rel_path, session_epoch, monotonic_seq, "
            "event_kind, content_hash, delta_blob, state, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
            (workspace_id, rel_path, session_epoch, monotonic_seq,
             event_kind, content_hash, delta_json, now),
        )
        self._conn.commit()
        lsn = cur.lastrowid
        logger.debug("staging append lsn=%d ws=%s path=%s", lsn, workspace_id, rel_path)
        return lsn

    def transition(
        self,
        lsn: int,
        new_state: str,
        generation: int = 0,
        error: Optional[str] = None,
    ) -> bool:
        """状态转换。返回 True 如果成功。

        状态机：pending → applying → applied 或 pending/applying → failed
        """
        if new_state not in ("applying", "applied", "failed"):
            raise ValueError(f"invalid state: {new_state}")

        row = self._conn.execute(
            "SELECT state FROM staging_entries WHERE lsn = ?", (lsn,)
        ).fetchone()
        if row is None:
            return False

        current_state = row["state"]
        if new_state not in VALID_TRANSITIONS.get(current_state, set()):
            logger.warning(
                "invalid transition lsn=%d: %s → %s", lsn, current_state, new_state
            )
            return False

        if new_state == "applied":
            self._conn.execute(
                "UPDATE staging_entries SET state = 'applied', "
                "applied_generation = ? WHERE lsn = ?",
                (generation, lsn),
            )
        elif new_state == "failed":
            self._conn.execute(
                "UPDATE staging_entries SET state = 'failed', "
                "error = ? WHERE lsn = ?",
                (error, lsn),
            )
        else:
            self._conn.execute(
                "UPDATE staging_entries SET state = ? WHERE lsn = ?",
                (new_state, lsn),
            )
        self._conn.commit()
        return True

    def read_pending(self, workspace_id: Optional[str] = None) -> List[StagingWALEntry]:
        """读取 pending + applying 状态的 entries（用于恢复）。"""
        if workspace_id:
            rows = self._conn.execute(
                "SELECT * FROM staging_entries "
                "WHERE workspace_id = ? AND state IN ('pending', 'applying') "
                "ORDER BY lsn",
                (workspace_id,),
            ).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT * FROM staging_entries "
                "WHERE state IN ('pending', 'applying') "
                "ORDER BY lsn",
            ).fetchall()

        return [self._row_to_entry(r) for r in rows]

    def read_all(
        self,
        workspace_id: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 1000,
    ) -> List[StagingWALEntry]:
        """读取 entries（支持过滤）。"""
        conditions = []
        params = []
        if workspace_id:
            conditions.append("workspace_id = ?")
            params.append(workspace_id)
        if state:
            conditions.append("state = ?")
            params.append(state)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = self._conn.execute(
            f"SELECT * FROM staging_entries WHERE {where} "
            f"ORDER BY lsn LIMIT ?",
            params + [limit],
        ).fetchall()

        return [self._row_to_entry(r) for r in rows]

    def compact_applied(self, workspace_id: Optional[str] = None, keep_last_n: int = 100):
        """清理已 applied 的 entries，保留最近 N 条。"""
        if workspace_id:
            self._conn.execute(
                "DELETE FROM staging_entries WHERE workspace_id = ? "
                "AND state = 'applied' AND lsn NOT IN "
                "(SELECT lsn FROM staging_entries WHERE workspace_id = ? "
                "ORDER BY lsn DESC LIMIT ?)",
                (workspace_id, workspace_id, keep_last_n),
            )
        else:
            self._conn.execute(
                "DELETE FROM staging_entries WHERE state = 'applied' AND lsn NOT IN "
                "(SELECT lsn FROM staging_entries ORDER BY lsn DESC LIMIT ?)",
                (keep_last_n,),
            )
        self._conn.commit()

    def recover(self) -> List[StagingWALEntry]:
        """daemon 启动恢复：返回所有需要重放的 entries。

        规范：enterprise-watcher-benefit-production-plan.md §3.4
        恢复顺序：
        1. 加载 pending/applying entries
        2. 检查 manifest 的 file generation
        3. 已提交则补记 applied，未提交则重放
        """
        entries = self.read_pending()
        logger.info("staging recovery: %d entries to process", len(entries))
        return entries

    def stats(self) -> Dict:
        """返回 staging log 统计。"""
        counts = {}
        for state in ("pending", "applying", "applied", "failed"):
            row = self._conn.execute(
                "SELECT COUNT(*) as cnt FROM staging_entries WHERE state = ?",
                (state,),
            ).fetchone()
            counts[state] = row["cnt"] if row else 0

        max_lsn_row = self._conn.execute(
            "SELECT MAX(lsn) as max_lsn FROM staging_entries"
        ).fetchone()

        return {
            "counts": counts,
            "total": sum(counts.values()),
            "max_lsn": max_lsn_row["max_lsn"] if max_lsn_row else 0,
            "db_path": self.db_path,
        }

    def close(self):
        """关闭连接。"""
        if self._conn:
            self._conn.close()
            self._conn = None

    @staticmethod
    def _row_to_entry(row) -> StagingWALEntry:
        """将 SQLite Row 转换为 StagingWALEntry。"""
        return StagingWALEntry(
            lsn=row["lsn"],
            workspace_id=row["workspace_id"],
            rel_path=row["rel_path"],
            session_epoch=row["session_epoch"],
            monotonic_seq=row["monotonic_seq"],
            event_kind=row["event_kind"],
            content_hash=row["content_hash"],
            delta_blob=row["delta_blob"],
            state=row["state"],
            created_at=row["created_at"],
            applied_generation=row["applied_generation"],
            error=row["error"],
        )
