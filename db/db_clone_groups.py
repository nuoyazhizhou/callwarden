"""
Phase 7.0: Clone Groups 存储

设计参考：enterprise-daemon-shared-snapshot-plan.md §Phase 7

替代 clone_pairs 的无界 pair 展开。把克隆结果组织成 group：
- 同一 token_hash / 同一相似度簇的符号归入一个 group
- group 有唯一 representative（代表符号）+ N 个 members
- list_clones 从 group 读取，避免 N×N pair 爆炸

设计要点：
- group_hash = sha256(workspace_id | clone_type | token_hash | similarity_bucket)
- 每个 group 记录 representative_symbol_id（通常是 ID 最小的成员）
- clone_group_members 表存储所有成员符号 ID
- 查询时按 group 返回，每 group 最多返回 members_limit 个成员

向后兼容：
- 旧 clone_pairs 表保留（不删除）
- list_clones 优先读 clone_groups，无数据时降级到 clone_pairs
"""

from __future__ import annotations

import hashlib
import sqlite3
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional


# ============================================
# Schema
# ============================================

CLONE_GROUPS_SCHEMA_DDL = """
-- clone groups：把克隆结果组织成 group，避免无界 pair 展开
CREATE TABLE IF NOT EXISTS clone_groups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    group_hash TEXT NOT NULL,                  -- 去重 hash
    clone_type INTEGER NOT NULL,               -- 1/2/3
    token_hash TEXT NOT NULL DEFAULT '',
    similarity REAL DEFAULT 0.0,               -- Type-3 的相似度（组内最小）
    representative_symbol_id INTEGER NOT NULL,
    member_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    UNIQUE(workspace_id, group_hash),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id) ON DELETE CASCADE,
    FOREIGN KEY (representative_symbol_id) REFERENCES symbols(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_clone_groups_ws ON clone_groups(workspace_id);
CREATE INDEX IF NOT EXISTS idx_clone_groups_type ON clone_groups(workspace_id, clone_type);
CREATE INDEX IF NOT EXISTS idx_clone_groups_sim ON clone_groups(workspace_id, similarity);
CREATE INDEX IF NOT EXISTS idx_clone_groups_repr ON clone_groups(representative_symbol_id);

-- clone group members：每个 group 的成员符号
CREATE TABLE IF NOT EXISTS clone_group_members (
    group_id INTEGER NOT NULL,
    symbol_id INTEGER NOT NULL,
    PRIMARY KEY (group_id, symbol_id),
    FOREIGN KEY (group_id) REFERENCES clone_groups(id) ON DELETE CASCADE,
    FOREIGN KEY (symbol_id) REFERENCES symbols(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_clone_group_members_sym ON clone_group_members(symbol_id);
"""


# ============================================
# 数据结构
# ============================================

@dataclass
class CloneGroup:
    """克隆组信息"""
    id: int = 0
    workspace_id: int = 0
    group_hash: str = ""
    clone_type: int = 0
    token_hash: str = ""
    similarity: float = 0.0
    representative_symbol_id: int = 0
    member_count: int = 0
    created_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        return (
            f"CloneGroup(id={self.id}, type={self.clone_type}, "
            f"members={self.member_count}, sim={self.similarity:.3f})"
        )


@dataclass
class CloneGroupDetail:
    """克隆组详情（含成员符号）"""
    group: CloneGroup = field(default_factory=CloneGroup)
    members: List[Dict[str, Any]] = field(default_factory=list)
    # members 字段含每个成员的 symbol_id / name / qualified_name / file_path / start_line


# ============================================
# Schema 初始化
# ============================================

def init_clone_groups_schema(conn: sqlite3.Connection):
    """初始化 clone_groups schema。"""
    conn.executescript(CLONE_GROUPS_SCHEMA_DDL)
    conn.commit()


# ============================================
# Group hash 计算
# ============================================

def compute_group_hash(
    workspace_id: int,
    clone_type: int,
    token_hash: str,
    similarity: float,
) -> str:
    """计算 clone group 的去重 hash

    相同 (workspace, clone_type, token_hash, similarity_bucket) 视为同一组。
    similarity_bucket 把相似度量化到 0.05 粒度，避免浮点抖动。
    """
    # 量化相似度到 0.05 粒度（Type-1/2 的 similarity=1.0 不受影响）
    sim_bucket = round(float(similarity) * 20) / 20
    raw = f"{workspace_id}|{clone_type}|{token_hash}|{sim_bucket}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# ============================================
# 存储
# ============================================

def store_clone_groups(
    conn: sqlite3.Connection,
    workspace_id: int,
    groups: List[Dict[str, Any]],
) -> int:
    """批量存储 clone groups

    参数：
        workspace_id: workspace ID
        groups: 每个元素是 dict，必须含字段：
            - clone_type: int (1/2/3)
            - token_hash: str
            - similarity: float
            - members: List[int]（symbol IDs，第一个为 representative）

    返回：写入的 group 数量

    行为：
    - 同 group_hash 的记录 UPSERT（覆盖 member_count/similarity）
    - 清空旧 members 并重新写入（保证一致性）
    - 不删除其他 group（增量更新由调用方决定）
    """
    if not groups:
        return 0

    now = time.time()
    written = 0

    for g in groups:
        clone_type = int(g["clone_type"])
        token_hash = str(g.get("token_hash", ""))
        similarity = float(g.get("similarity", 0.0))
        members: List[int] = list(g.get("members", []))
        if not members:
            continue

        representative_id = members[0]
        group_hash = compute_group_hash(
            workspace_id, clone_type, token_hash, similarity
        )

        # UPSERT group
        conn.execute(
            """INSERT INTO clone_groups
               (workspace_id, group_hash, clone_type, token_hash,
                similarity, representative_symbol_id, member_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(workspace_id, group_hash)
               DO UPDATE SET
                   similarity = excluded.similarity,
                   representative_symbol_id = excluded.representative_symbol_id,
                   member_count = excluded.member_count,
                   created_at = excluded.created_at""",
            (workspace_id, group_hash, clone_type, token_hash,
             similarity, representative_id, len(members), now),
        )

        # 查 group.id
        row = conn.execute(
            """SELECT id FROM clone_groups
               WHERE workspace_id = ? AND group_hash = ?""",
            (workspace_id, group_hash),
        ).fetchone()
        if not row:
            continue
        group_id = row["id"]

        # 清空旧 members（保证一致性）
        conn.execute(
            "DELETE FROM clone_group_members WHERE group_id = ?",
            (group_id,),
        )
        # 批量写入新 members
        conn.executemany(
            "INSERT OR IGNORE INTO clone_group_members (group_id, symbol_id) VALUES (?, ?)",
            [(group_id, sid) for sid in members],
        )
        written += 1

    conn.commit()
    return written


def clear_clone_groups(
    conn: sqlite3.Connection,
    workspace_id: int,
) -> int:
    """清空 workspace 的所有 clone groups

    返回：被删除的 group 数量
    """
    cur = conn.execute(
        "DELETE FROM clone_groups WHERE workspace_id = ?",
        (workspace_id,),
    )
    conn.commit()
    return cur.rowcount


# ============================================
# 查询
# ============================================

def list_clone_groups(
    conn: sqlite3.Connection,
    workspace_id: int,
    clone_type: int = 0,
    min_similarity: float = 0.0,
    limit: int = 100,
) -> List[CloneGroup]:
    """列出 clone groups

    参数：
        clone_type: 0=全部，1/2/3 对应 Type-N
        min_similarity: 最低相似度过滤
        limit: 返回上限

    返回：按相似度降序的 CloneGroup 列表
    """
    sql = "SELECT * FROM clone_groups WHERE workspace_id = ?"
    params_list: List[Any] = [workspace_id]
    if clone_type in (1, 2, 3):
        sql += " AND clone_type = ?"
        params_list.append(clone_type)
    if min_similarity > 0:
        sql += " AND similarity >= ?"
        params_list.append(min_similarity)
    sql += " ORDER BY similarity DESC, member_count DESC LIMIT ?"
    params_list.append(limit)
    rows = conn.execute(sql, params_list).fetchall()
    return [_row_to_clone_group(r) for r in rows]


def get_clone_group_members(
    conn: sqlite3.Connection,
    group_id: int,
    limit: int = 1000,
) -> List[Dict[str, Any]]:
    """获取 clone group 的成员符号

    返回：每个成员含 symbol_id / name / qualified_name / file_path / start_line
    """
    rows = conn.execute(
        """SELECT m.symbol_id, s.name, s.qualified_name, s.start_line,
                  fi.rel_path as file_path
           FROM clone_group_members m
           JOIN symbols s ON m.symbol_id = s.id
           JOIN file_instances fi ON s.file_instance_id = fi.id
           WHERE m.group_id = ?
           ORDER BY m.symbol_id
           LIMIT ?""",
        (group_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_clone_group_detail(
    conn: sqlite3.Connection,
    group_id: int,
    members_limit: int = 100,
) -> Optional[CloneGroupDetail]:
    """获取 clone group 详情（含成员符号）"""
    row = conn.execute(
        "SELECT * FROM clone_groups WHERE id = ?",
        (group_id,),
    ).fetchone()
    if not row:
        return None
    group = _row_to_clone_group(row)
    members = get_clone_group_members(conn, group_id, members_limit)
    return CloneGroupDetail(group=group, members=members)


def get_clone_group_stats(
    conn: sqlite3.Connection,
    workspace_id: int,
) -> Dict[str, Any]:
    """获取 clone groups 统计信息

    返回：group 数量 / 按类型分布 / 总成员数 / 受影响文件数
    """
    row = conn.execute(
        """SELECT
               COUNT(*) as total_groups,
               SUM(CASE WHEN clone_type = 1 THEN 1 ELSE 0 END) as type1,
               SUM(CASE WHEN clone_type = 2 THEN 1 ELSE 0 END) as type2,
               SUM(CASE WHEN clone_type = 3 THEN 1 ELSE 0 END) as type3,
               SUM(member_count) as total_members
           FROM clone_groups WHERE workspace_id = ?""",
        (workspace_id,),
    ).fetchone()
    if not row:
        return {
            "total_groups": 0, "type1": 0, "type2": 0, "type3": 0,
            "total_members": 0, "affected_files": 0, "affected_symbols": 0,
        }

    # 受影响符号数（distinct）
    sym_row = conn.execute(
        """SELECT COUNT(DISTINCT m.symbol_id) as sym_cnt
           FROM clone_group_members m
           JOIN clone_groups g ON m.group_id = g.id
           WHERE g.workspace_id = ?""",
        (workspace_id,),
    ).fetchone()
    sym_cnt = sym_row["sym_cnt"] if sym_row else 0

    # 受影响文件数（distinct）
    file_row = conn.execute(
        """SELECT COUNT(DISTINCT fi.id) as file_cnt
           FROM clone_group_members m
           JOIN clone_groups g ON m.group_id = g.id
           JOIN symbols s ON m.symbol_id = s.id
           JOIN file_instances fi ON s.file_instance_id = fi.id
           WHERE g.workspace_id = ?""",
        (workspace_id,),
    ).fetchone()
    file_cnt = file_row["file_cnt"] if file_row else 0

    return {
        "total_groups": row["total_groups"] or 0,
        "type1": row["type1"] or 0,
        "type2": row["type2"] or 0,
        "type3": row["type3"] or 0,
        "total_members": row["total_members"] or 0,
        "affected_files": file_cnt,
        "affected_symbols": sym_cnt,
    }


def _row_to_clone_group(row: sqlite3.Row) -> CloneGroup:
    """把数据库行转换为 CloneGroup 对象"""
    return CloneGroup(
        id=row["id"],
        workspace_id=row["workspace_id"],
        group_hash=row["group_hash"],
        clone_type=row["clone_type"],
        token_hash=row["token_hash"] or "",
        similarity=row["similarity"],
        representative_symbol_id=row["representative_symbol_id"],
        member_count=row["member_count"],
        created_at=row["created_at"],
    )


# ============================================
# CloneGroupMixin（集成到 CodeGraphDB）
# ============================================

class CloneGroupMixin:
    """Clone Groups Mixin

    通过 self.conn 访问数据库连接。
    与 CloneDetectionMixin 配合：detect_clones 写 groups，list_clones 读 groups。
    """

    def store_clone_groups(
        self,
        groups: List[Dict[str, Any]],
    ) -> int:
        """批量存储 clone groups"""
        ws_id = self._get_active_workspace_id()
        return store_clone_groups(self.conn, ws_id, groups)

    def clear_clone_groups(self) -> int:
        """清空 workspace 的所有 clone groups"""
        ws_id = self._get_active_workspace_id()
        return clear_clone_groups(self.conn, ws_id)

    def list_clone_groups(
        self,
        clone_type: int = 0,
        min_similarity: float = 0.0,
        limit: int = 100,
    ) -> List[CloneGroup]:
        """列出 clone groups"""
        ws_id = self._get_active_workspace_id()
        return list_clone_groups(
            self.conn, ws_id, clone_type, min_similarity, limit
        )

    def get_clone_group_detail(
        self,
        group_id: int,
        members_limit: int = 100,
    ) -> Optional[CloneGroupDetail]:
        """获取 clone group 详情"""
        return get_clone_group_detail(self.conn, group_id, members_limit)

    def get_clone_group_stats(self) -> Dict[str, Any]:
        """获取 clone groups 统计"""
        ws_id = self._get_active_workspace_id()
        return get_clone_group_stats(self.conn, ws_id)
