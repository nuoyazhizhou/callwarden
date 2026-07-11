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
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

from server.staging_log import StagingLog, StagingEntry

logger = logging.getLogger(__name__)


# ============================================
# Session epoch / generation CAS —— 规范 watcher-generation-state-machine.md
# 修复 T-1783751525743-7c76
# ============================================

SESSION_SCHEMA_DDL = """
-- agent_sessions：所有 session 的注册表（含已撤销）
CREATE TABLE IF NOT EXISTS agent_sessions (
    workspace_id INTEGER NOT NULL,
    session_id TEXT NOT NULL,
    session_epoch INTEGER NOT NULL,
    activated_at INTEGER NOT NULL,
    revoked_at INTEGER,
    peer_uid INTEGER NOT NULL,
    PRIMARY KEY (workspace_id, session_id)
);

-- workspace_active_session：每个 workspace 当前唯一的 active session
CREATE TABLE IF NOT EXISTS workspace_active_session (
    workspace_id INTEGER PRIMARY KEY,
    active_session_id TEXT NOT NULL,
    active_session_epoch INTEGER NOT NULL
);

-- file_generations：per-file 消息去重 + CAS 两阶段提交
CREATE TABLE IF NOT EXISTS file_generations (
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    latest_session_id TEXT DEFAULT '',
    latest_session_epoch INTEGER DEFAULT 0,
    latest_seq INTEGER DEFAULT 0,
    latest_seen_generation TEXT DEFAULT '',
    latest_committed_generation TEXT DEFAULT '',
    PRIMARY KEY (workspace_id, rel_path)
);
"""


def init_session_schema(conn: sqlite3.Connection):
    """初始化 session 管理 schema。"""
    conn.executescript(SESSION_SCHEMA_DDL)
    conn.commit()


class ProtocolError(Exception):
    """Session epoch / generation CAS 协议错误。"""
    pass


def daemon_handle_connect(peer_uid: int, workspace_id: int, requested_session_id: str,
                          ws_conn: sqlite3.Connection) -> dict:
    """agent 连接握手——daemon 分配单调 epoch，旧 session 永久失效。

    规范：watcher-generation-state-machine.md §4.1
    修复 T-1783751525743-7c76
    """
    now = int(time.time())
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. 撤销同一 workspace 所有旧 active session
        ws_conn.execute(
            "UPDATE agent_sessions SET revoked_at = ? "
            "WHERE workspace_id = ? AND revoked_at IS NULL",
            (now, workspace_id)
        )
        # 2. 分配 new_epoch = MAX(all session_epoch) + 1
        row = ws_conn.execute(
            "SELECT COALESCE(MAX(session_epoch), 0) + 1 AS next_epoch "
            "FROM agent_sessions WHERE workspace_id = ?",
            (workspace_id,)
        ).fetchone()
        new_epoch = row["next_epoch"]
        # 3. INSERT agent_sessions
        ws_conn.execute(
            "INSERT INTO agent_sessions (workspace_id, session_id, session_epoch, "
            "activated_at, revoked_at, peer_uid) VALUES (?, ?, ?, ?, NULL, ?)",
            (workspace_id, requested_session_id, new_epoch, now, peer_uid)
        )
        # 4. INSERT OR REPLACE workspace_active_session
        ws_conn.execute(
            "INSERT OR REPLACE INTO workspace_active_session "
            "(workspace_id, active_session_id, active_session_epoch) VALUES (?, ?, ?)",
            (workspace_id, requested_session_id, new_epoch)
        )
        # 5. UPDATE file_generations SET latest_seq=0（新 session seq 从 1 开始）
        ws_conn.execute(
            "UPDATE file_generations SET latest_session_id = ?, "
            "latest_session_epoch = ?, latest_seq = 0, "
            "latest_seen_generation = '' WHERE workspace_id = ?",
            (requested_session_id, new_epoch, workspace_id)
        )
        ws_conn.execute("COMMIT")
        return {"session_epoch": new_epoch}
    except Exception:
        try:
            ws_conn.execute("ROLLBACK")
        except Exception:
            pass
        raise


def daemon_handle_refresh(peer_uid: int, workspace_id: int, msg: dict,
                          ws_conn: sqlite3.Connection,
                          cas_conn: Optional[sqlite3.Connection] = None,
                          workspace_root: str = "") -> dict:
    """处理 agent refresh 消息——session epoch 校验 + 两阶段 CAS。

    规范：watcher-generation-state-machine.md §4.3
    规范：daemon-ipc-security.md §3.2（daemon 不信任 agent 提供的 hash）
    规范：parse-input-abi.md §2（canonicalize_source 是唯一输入入口）
    修复 T-1783751525743-7c76

    完整管道：
    1. session epoch 校验（拒绝 stale session）
    2. CAS 第一阶段（seen）——原子更新 latest_seen_generation
    3. daemon 侧 re-canonicalize + re-hash + Rust parse + CAS publish
    4. CAS 第二阶段（committed）——条件更新 latest_committed_generation

    Args:
        peer_uid: agent 的 peer UID
        workspace_id: workspace ID
        msg: agent 消息，需包含 rel_path/agent_session_id/monotonic_seq/session_epoch，
             可选 abs_path（无则用 workspace_root + rel_path 推导）
        ws_conn: workspace 数据库连接
        cas_conn: CAS 数据库连接（若为 None 则跳过 CAS publish，仅做 generation CAS）
        workspace_root: workspace 根路径（用于推导 abs_path）

    Returns:
        {"status": "committed"/"stale_seq_dropped", "generation": str, ...}
    """
    rel_path = msg["rel_path"]
    incoming_session = msg["agent_session_id"]
    incoming_seq = msg["monotonic_seq"]
    incoming_epoch = msg["session_epoch"]

    # 1. 校验 session epoch——只能匹配当前 active epoch
    active = ws_conn.execute(
        "SELECT active_session_id, active_session_epoch "
        "FROM workspace_active_session WHERE workspace_id = ?",
        (workspace_id,)
    ).fetchone()
    if active is None:
        raise ProtocolError(f"no active session for workspace {workspace_id}")
    if (incoming_session != active["active_session_id"]
            or incoming_epoch != active["active_session_epoch"]):
        raise ProtocolError(
            f"stale session rejected: incoming={incoming_session}:{incoming_epoch} "
            f"active={active['active_session_id']}:{active['active_session_epoch']}"
        )

    incoming_gen = f"{incoming_epoch}:{incoming_seq}"

    # 2. CAS 第一阶段（seen）——原子更新 latest_seen_generation
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        row = ws_conn.execute(
            "SELECT latest_session_epoch, latest_seq, latest_seen_generation, "
            "latest_committed_generation FROM file_generations "
            "WHERE workspace_id = ? AND rel_path = ?",
            (workspace_id, rel_path)
        ).fetchone()

        if row is None:
            # 首次见到此文件——插入新行
            ws_conn.execute(
                "INSERT INTO file_generations "
                "(workspace_id, rel_path, latest_session_id, latest_session_epoch, "
                "latest_seq, latest_seen_generation, latest_committed_generation) "
                "VALUES (?, ?, ?, ?, ?, ?, '')",
                (workspace_id, rel_path, incoming_session, incoming_epoch,
                 incoming_seq, incoming_gen)
            )
        elif incoming_seq <= row["latest_seq"]:
            # 同 epoch 内 stale seq——直接丢弃，不报错
            ws_conn.execute("ROLLBACK")
            return {"status": "stale_seq_dropped"}
        else:
            ws_conn.execute(
                "UPDATE file_generations SET latest_session_id = ?, "
                "latest_session_epoch = ?, latest_seq = ?, latest_seen_generation = ? "
                "WHERE workspace_id = ? AND rel_path = ?",
                (incoming_session, incoming_epoch, incoming_seq, incoming_gen,
                 workspace_id, rel_path)
            )
        ws_conn.execute("COMMIT")
    except Exception:
        try:
            ws_conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    # 3. daemon 侧 re-canonicalize + re-hash + Rust parse + CAS publish
    # 规范：daemon-ipc-security.md §3.2 —— daemon 不信任 agent 提供的 hash，必须重新计算
    # 规范：parse-input-abi.md §2 —— canonicalize_source 是唯一输入入口
    cas_result = _daemon_parse_and_publish(
        rel_path=rel_path,
        abs_path=msg.get("abs_path") or _join_path(workspace_root, rel_path),
        cas_conn=cas_conn,
        workspace_id=workspace_id,
    )

    # 4. CAS 第二阶段（committed）——条件更新 latest_committed_generation
    ws_conn.execute("BEGIN IMMEDIATE")
    try:
        gen_cur = ws_conn.execute(
            "UPDATE file_generations SET latest_committed_generation = ? "
            "WHERE workspace_id = ? AND rel_path = ? "
            "AND latest_seen_generation = ?",
            (incoming_gen, workspace_id, rel_path, incoming_gen)
        )
        if gen_cur.rowcount != 1:
            ws_conn.execute("ROLLBACK")
            raise ProtocolError(f"stale manifest commit for {rel_path}")
        ws_conn.execute("COMMIT")
    except Exception:
        try:
            ws_conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    result = {"status": "committed", "generation": incoming_gen}
    if cas_result:
        result.update(cas_result)
    return result


def _join_path(workspace_root: str, rel_path: str) -> str:
    """拼接 workspace_root + rel_path，处理前后斜杠 + Windows 盘符小写。"""
    if not workspace_root:
        return rel_path.replace("\\", "/")
    root = workspace_root.replace("\\", "/").rstrip("/")
    rel = rel_path.replace("\\", "/").lstrip("/")
    # Windows 盘符统一小写（与 config.norm_path 一致）
    if len(root) >= 2 and root[1] == ":" and root[0].isalpha():
        root = root[0].lower() + root[1:]
    return f"{root}/{rel}"


def _daemon_parse_and_publish(
    rel_path: str,
    abs_path: str,
    cas_conn: Optional[sqlite3.Connection],
    workspace_id: int,
) -> Optional[Dict[str, Any]]:
    """daemon 侧 re-canonicalize + re-hash + Rust parse + CAS publish。

    规范：daemon-ipc-security.md §3.2 —— daemon 重新计算 sha256，不信任 agent 提供的 hash
    规范：parse-input-abi.md §2 —— canonicalize_source 是唯一输入入口
    规范：cas-gc-protocol.md §3 —— CAS 原子发布四阶段

    降级路径：
    1. 优先调用 Rust canonicalize_source_py（BOM+编码+CRLF 完整归一化）
    2. Rust 不可用时降级到 Python config.read_file_normalized（UTF-8→latin-1 两步降级）
    3. cas_conn 为 None 时跳过 CAS publish（仅做 generation CAS）

    Args:
        rel_path: 相对路径（用于语言检测）
        abs_path: 绝对路径（用于读取文件）
        cas_conn: CAS 数据库连接
        workspace_id: workspace ID（用于 cas_pin）

    Returns:
        {"content_hash": str, "cas_key": str, "cas_state": str, ...} 或 None
    """
    import os
    import hashlib

    # 3a. 检测语言
    try:
        from config import detect_language_from_path
        language = detect_language_from_path(rel_path)
    except ImportError:
        language = ""
    if not language:
        # 不支持的语言——跳过 parse/publish，仅保留 generation CAS
        return {"content_hash": "", "cas_key": "", "cas_state": "unsupported_language"}

    # 3b. canonicalize + re-hash —— 优先 Rust，降级 Python
    canonical_bytes = None
    content_hash = ""
    canonicalize_method = "rust"

    try:
        from callwarden_core import canonicalize_source_py
        canon = canonicalize_source_py(abs_path)
        canonical_bytes = canon["canonical_bytes"]
        content_hash = canon["content_hash"]
    except ImportError:
        canonicalize_method = "python_fallback"
    except Exception as e:
        logger.warning("Rust canonicalize_source_py failed for %s: %s, fallback to Python",
                       abs_path, e)
        canonicalize_method = "python_fallback"

    if canonicalize_method == "python_fallback":
        # Python 降级：读取文件 + 简单 UTF-8 → latin-1
        try:
            from config import read_file_normalized
            text, content_hash = read_file_normalized(abs_path)
            canonical_bytes = text.encode("utf-8")
        except Exception as e:
            logger.error("Python canonicalize fallback failed for %s: %s", abs_path, e)
            return {"content_hash": "", "cas_key": "",
                    "cas_state": "canonicalize_failed",
                    "error": str(e)}

    # 3c. CAS publish（若 cas_conn 可用）
    if cas_conn is None:
        return {"content_hash": content_hash, "cas_key": "",
                "cas_state": "no_cas_conn", "canonicalize_method": canonicalize_method}

    try:
        from db.db_cas import compute_cas_key_v1, cas_publish_with_retry, cas_lookup
    except ImportError:
        # CAS 模块不可用——只返回 content_hash
        return {"content_hash": content_hash, "cas_key": "",
                "cas_state": "cas_module_unavailable",
                "canonicalize_method": canonicalize_method}

    # 3d. 计算 CAS key
    parser_version = "0.1.0"
    callwarden_version = "0.2.0"
    extraction_config_version = "v1"
    abi_version = "v1"
    input_abi_version = "v1"
    cas_key = compute_cas_key_v1(
        content_hash, language, parser_version, callwarden_version,
        extraction_config_version, abi_version, input_abi_version
    )

    # 3e. 检查 CAS 是否已命中（state='ready'）
    existing = cas_lookup(cas_conn, cas_key)
    if existing:
        # CAS 命中——只需补 pin，无需重新 parse
        try:
            from db.db_cas import cas_pin
            cas_pin(cas_conn, cas_key, workspace_id)
        except Exception as e:
            logger.warning("cas_pin failed for cas_key=%s: %s", cas_key, e)
        return {"content_hash": content_hash, "cas_key": cas_key,
                "cas_state": "ready_cache_hit", "canonicalize_method": canonicalize_method}

    # 3f. CAS 未命中——调用可信 Rust parser 解析 canonical_bytes
    parse_result = None
    try:
        from callwarden_core import parse_file_lang
        # Rust 侧 parse_file_lang 内部会调用 canonicalize_source，但我们已经
        # canonicalize 过了。理想情况应调用 parse_canonical_bytes，但该函数
        # 目前只在 Rust impl 中，未暴露给 Python。
        # 降级方案：让 Rust parse_file_lang 自己读取并 canonicalize（幂等）
        parse_result = parse_file_lang(abs_path, "")
    except ImportError:
        parse_result = None
    except Exception as e:
        logger.warning("Rust parse_file_lang failed for %s: %s", abs_path, e)
        parse_result = None

    if parse_result is None:
        # Rust parser 不可用——CAS 发布无法完成
        return {"content_hash": content_hash, "cas_key": cas_key,
                "cas_state": "parse_failed",
                "canonicalize_method": canonicalize_method}

    # 3g. CAS 原子发布（带 retry）
    try:
        cas_publish_with_retry(
            cas_conn, cas_key, content_hash, language, parse_result,
            workspace_id=workspace_id, max_retries=3,
            parser_version=parser_version, callwarden_version=callwarden_version,
            extraction_config_version=extraction_config_version,
            abi_version=abi_version, input_abi_version=input_abi_version,
        )
        return {"content_hash": content_hash, "cas_key": cas_key,
                "cas_state": "ready_published", "canonicalize_method": canonicalize_method}
    except Exception as e:
        logger.error("CAS publish failed for %s (cas_key=%s): %s", abs_path, cas_key, e)
        return {"content_hash": content_hash, "cas_key": cas_key,
                "cas_state": "publish_failed",
                "canonicalize_method": canonicalize_method,
                "error": str(e)}


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
