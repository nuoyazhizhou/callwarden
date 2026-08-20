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

from callwarden.server.staging_log import StagingLog, StagingEntry

# Phase 1-4 wire-production: Rust 短路 replicator_get_pending_count
try:
    import callwarden_core as _callwarden_core  # type: ignore
    _RUST_REPLICATOR_QUERY_AVAILABLE = True
except ImportError:
    _callwarden_core = None
    _RUST_REPLICATOR_QUERY_AVAILABLE = False

# C3（Global/Local CAS 迁移）：Rust file_generations facade 可用性探测。
# seen/committed/reset 短连接复用 CasStore 原子语义；函数缺失（旧 pyd）时
# 回退 Python SQL（fallback 分支，验收标准 3 允许）。
_RUST_FILE_GEN_AVAILABLE = (
    _callwarden_core is not None
    and callable(getattr(_callwarden_core, "cas_file_generation_seen", None))
    and callable(getattr(_callwarden_core, "cas_file_generation_committed", None))
    and callable(getattr(_callwarden_core, "cas_file_generation_reset", None))
)

# rollback_config 查询缓存（60s TTL，与 staging_log.py 一致）
_REPLICATOR_ROLLBACK_CACHE: Dict[str, float] = {"ts": 0.0, "value": False}
_REPLICATOR_ROLLBACK_CACHE_TTL = 60.0
_CAS_WRITE_ROLLBACK_CACHE: Dict[str, float] = {"ts": 0.0, "value": False}
_CAS_WRITE_ROLLBACK_CACHE_TTL = 60.0

# C6（S2）：失败 generation 保护——镜像 Rust snapshot_guard.rs 的状态分类
# （rust_ext/src/daemon/snapshot_guard.rs）。语义：非 ready 状态（parse 失败 /
# unsupported / partial / stale / dirty overlay）不得推进 latest_committed_generation、
# 不得追加 staging、不得 replicate——保护上一代好 snapshot（S2 验收点 5/7）。
#
# no_cas_conn 对齐说明（P1-2 复审）：本集合含 no_cas_conn 与 Rust
# snapshot_guard.rs::is_parse_failure_state 静态一致。但保护启用条件为
# `cas_conn is not None and cas_result`（下方门控），而 Python 侧
# `_daemon_parse_and_publish` 仅在 cas_conn=None 时返回 no_cas_conn，故该状态
# 永远不会进入保护评估——与 Rust 主链 `cas_store=None → cas_result=None →
# cas_state="" → 不保护` 的行为语义完全等价（Rust 的 no_cas_conn 同样只在
# `_daemon_parse_and_publish` 被直接调用且传 None 时产生，主链 daemon_handle_refresh
# 中 cas_store=None 时 cas_result=None）。加入集合仅为静态对齐 + 防御未来启用
# 条件放宽时漏拦截。
_SNAPSHOT_GUARD_PARSE_FAILURE_STATES = frozenset({
    "parse_failed",
    "canonicalize_failed",
    "publish_failed",
    "cas_lookup_failed",
    "no_abs_path",
    "no_cas_conn",
})
_SNAPSHOT_GUARD_STALE_STATES = frozenset({"stale_seq_dropped", "stale_generation"})
_SNAPSHOT_GUARD_PARTIAL_STATES = frozenset({"partial_published"})


def _is_rust_cas_write_rolled_back() -> bool:
    """检查 Rust CAS 写路径是否被显式回滚。"""
    now = time.time()
    if now - _CAS_WRITE_ROLLBACK_CACHE["ts"] < _CAS_WRITE_ROLLBACK_CACHE_TTL:
        return bool(_CAS_WRITE_ROLLBACK_CACHE["value"])
    try:
        from callwarden.config import DB_PATH
        conn = sqlite3.connect(DB_PATH)
        try:
            row = conn.execute(
                "SELECT rollback_flag FROM rollback_config "
                "WHERE feature_name = ? ORDER BY updated_at DESC LIMIT 1",
                ("rust_cas_write",),
            ).fetchone()
            value = bool(row and row[0] == 1)
        finally:
            conn.close()
    except Exception:
        value = False
    _CAS_WRITE_ROLLBACK_CACHE["ts"] = now
    _CAS_WRITE_ROLLBACK_CACHE["value"] = value
    return value


def _is_rust_replicator_query_rolled_back() -> bool:
    """检查 rust_replicator_snapshot_query feature 是否已回滚（60s 缓存）

    Replicator 是独立类（非 CodeGraphDB Mixin），无法用 self.is_feature_rolled_back。
    通过短连接查询 rollback_config 表，结果缓存 60s 避免频繁开 DB。
    与 staging_log.py:_is_rust_staging_log_rolled_back 模式一致。
    """
    now = time.time()
    if now - _REPLICATOR_ROLLBACK_CACHE["ts"] < _REPLICATOR_ROLLBACK_CACHE_TTL:
        return _REPLICATOR_ROLLBACK_CACHE["value"]  # type: ignore[return-value]
    try:
        import sqlite3 as _sqlite3
        from callwarden.config import DB_PATH as _DB_PATH
        conn = _sqlite3.connect(_DB_PATH)
        try:
            cur = conn.execute(
                "SELECT rollback_flag FROM rollback_config WHERE feature_name = ? "
                "ORDER BY updated_at DESC LIMIT 1",
                ("rust_replicator_snapshot_query",),
            )
            row = cur.fetchone()
            value = bool(row and row[0] == 1)
        finally:
            conn.close()
    except Exception:
        value = False
    _REPLICATOR_ROLLBACK_CACHE["ts"] = now
    _REPLICATOR_ROLLBACK_CACHE["value"] = value
    return value

# file_generations DDL 从 db_cas.py 导入（K6 去重，避免两处不一致）
# 延迟导入避免触发 db 包的完整初始化链
def _get_file_generations_ddl() -> str:
    from callwarden.db.db_cas import FILE_GENERATIONS_DDL
    return FILE_GENERATIONS_DDL

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
"""

# file_generations DDL 从 db_cas.py 延迟导入（K6 去重，避免两处不一致）


def init_session_schema(conn: sqlite3.Connection):
    """初始化 session 管理 schema（含 file_generations，DDL 从 db_cas.py 共享导入）。"""
    conn.executescript(SESSION_SCHEMA_DDL)
    conn.executescript(_get_file_generations_ddl())
    conn.commit()


class ProtocolError(Exception):
    """Session epoch / generation CAS 协议错误。

    code 字段用于让上层（daemon_server）透传给 client，让 agent 端可基于 code
    决定是否触发 auto-reconnect（session_not_active / stale_session → 重连；
    其他 code → 上抛或重试）。

    向后兼容：code 默认为 "protocol_error"，老调用方不传 code 也能用。
    """
    def __init__(self, message: str, code: str = "protocol_error"):
        super().__init__(message)
        self.code = code
        self.message = message


def daemon_handle_connect(peer_uid: int, workspace_id: int, requested_session_id: str,
                          ws_conn: sqlite3.Connection, ws_db_path: str = "") -> dict:
    """agent 连接握手——daemon 分配单调 epoch，旧 session 永久失效。

    规范：watcher-generation-state-machine.md §4.1
    修复 T-1783751525743-7c76
    C3：file_generations 会话重置优先走 Rust CasStore facade（独立短连接事务），
    因此从原单一事务中移出到 session 激活提交之后。会话级 epoch 单调递增，
    reset 单独提交不会破坏 stale 拦截语义（新 session epoch 恒大于旧 epoch）。
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
        ws_conn.execute("COMMIT")
    except Exception:
        try:
            ws_conn.execute("ROLLBACK")
        except Exception:
            pass
        raise

    # 5. UPDATE file_generations SET latest_seq=0（新 session seq 从 1 开始）
    if ws_db_path and _RUST_FILE_GEN_AVAILABLE:
        try:
            _callwarden_core.cas_file_generation_reset(
                ws_db_path, workspace_id, requested_session_id, new_epoch)
        except Exception as e:
            raise ProtocolError(
                f"cas_file_generation_reset failed: {e}",
                code="cas_reset_failed",
            )
    else:
        # fallback
        ws_conn.execute("BEGIN IMMEDIATE")
        try:
            ws_conn.execute(
                "UPDATE file_generations SET latest_session_id = ?, "
                "latest_session_epoch = ?, latest_seq = 0, "
                "latest_seen_generation = '' WHERE workspace_id = ?",
                (requested_session_id, new_epoch, workspace_id)
            )
            ws_conn.execute("COMMIT")
        except Exception:
            try:
                ws_conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
    return {"session_epoch": new_epoch}


# ============================================
# C6（S2）：失败 generation 保护（镜像 Rust snapshot_guard.rs）
# ============================================


def _is_dirty_overlay_path(abs_path: str, rel_path: str) -> bool:
    """镜像 Rust snapshot_guard.rs::is_dirty_overlay（设计 §9.3）。

    dirty overlay 路径（.git / .callwarden / 临时备份 / ~ / .bak / .orig / .rej）
    不进 CAS、不发布 snapshot，保护上一代好 snapshot。
    """
    abs_p = (abs_path or "").replace("\\", "/")
    rel_p = (rel_path or "").replace("\\", "/")
    if "/.git/" in abs_p or rel_p.startswith(".git/") or "/.git/" in rel_p:
        return True
    if ("/.callwarden/" in abs_p
            or rel_p.startswith(".callwarden/") or "/.callwarden/" in rel_p):
        return True
    if "/.callwarden-tmp-" in abs_p:
        return True
    if (abs_path or "").endswith("~") or (rel_path or "").endswith("~"):
        return True
    if (abs_path or "").endswith(".bak") or (rel_path or "").endswith(".bak"):
        return True
    if (abs_path or "").endswith(".orig") or (rel_path or "").endswith(".orig"):
        return True
    if (abs_path or "").endswith(".rej") or (rel_path or "").endswith(".rej"):
        return True
    return False


def _evaluate_generation_protection(
    cas_state: str, abs_path: str, rel_path: str
) -> Dict[str, Any]:
    """镜像 Rust snapshot_guard.rs::evaluate_generation_protection（设计 §5.3 + §9.3）。

    判断顺序与 Rust 一致：
    1. dirty overlay 优先 → blocked（parse_status=stale，不重试）
    2. parse 失败 → blocked（parse_status=failed，允许重试）
    3. unsupported → blocked（parse_status=unsupported，不重试）
    4. stale → blocked（parse_status=stale，不重试）
    5. partial → blocked（parse_status=partial，不重试，保留上一代好 snapshot）
    6. 其余（ready_published / ready_cache_hit / 空等）→ 不阻塞

    Returns:
        {"blocked": bool, "reason": str, "cas_state": str, "parse_status": str,
         "dirty_overlay": bool, "allows_retry": bool}
    """
    if _is_dirty_overlay_path(abs_path, rel_path):
        return {
            "blocked": True,
            "reason": f"dirty overlay rejected (设计 §9.3): rel_path={rel_path}",
            "cas_state": cas_state,
            "parse_status": "stale",
            "dirty_overlay": True,
            "allows_retry": False,
        }
    if cas_state in _SNAPSHOT_GUARD_PARSE_FAILURE_STATES:
        return {
            "blocked": True,
            "reason": f"parse failure (设计 §5.3 failed): cas_state={cas_state}",
            "cas_state": cas_state,
            "parse_status": "failed",
            "dirty_overlay": False,
            "allows_retry": True,
        }
    if cas_state == "unsupported_language":
        return {
            "blocked": True,
            "reason": (
                f"unsupported language (设计 §5.3 unsupported): cas_state={cas_state}"
            ),
            "cas_state": cas_state,
            "parse_status": "unsupported",
            "dirty_overlay": False,
            "allows_retry": False,
        }
    if cas_state in _SNAPSHOT_GUARD_STALE_STATES:
        return {
            "blocked": True,
            "reason": f"stale generation (设计 §5.3 stale): cas_state={cas_state}",
            "cas_state": cas_state,
            "parse_status": "stale",
            "dirty_overlay": False,
            "allows_retry": False,
        }
    if cas_state in _SNAPSHOT_GUARD_PARTIAL_STATES:
        return {
            "blocked": True,
            "reason": (
                f"partial parse (设计 §5.3 partial): cas_state={cas_state}, "
                f"保留上一代好 snapshot"
            ),
            "cas_state": cas_state,
            "parse_status": "partial",
            "dirty_overlay": False,
            "allows_retry": False,
        }
    return {
        "blocked": False,
        "reason": "",
        "cas_state": cas_state,
        "parse_status": "ok",
        "dirty_overlay": False,
        "allows_retry": False,
    }


def daemon_handle_refresh(peer_uid: int, workspace_id: int, msg: dict,
                          ws_conn: sqlite3.Connection,
                          cas_conn: Optional[sqlite3.Connection] = None,
                          canonical_bytes: Optional[bytes] = None,
                          workspace_root: str = "",
                          codegraph_db_path: str = "",
                          workspace_root_path: str = "",
                          ws_db_path: str = "",
                          cas_db_path: str = "") -> dict:
    """处理 agent refresh 消息——session epoch 校验 + 两阶段 CAS + P0-1 save-to-query merge。

    规范：watcher-generation-state-machine.md §4.3
    规范：daemon-ipc-security.md §3.2（daemon 不信任 agent 提供的 hash）
    规范：parse-input-abi.md §2（canonicalize_source 是唯一输入入口）
    修复 T-1783751525743-7c76
    修复 T-1783952125417-7a09（消除 TOCTOU + 禁止读客户端 abs_path）
    修复 T-1784644413771-8f1a2d37（P0-1 save-to-query 数据链闭合 2026-07-21）

    完整管道：
    1. session epoch 校验（拒绝 stale session）
    2. CAS 第一阶段（seen）——原子更新 latest_seen_generation
    3. daemon 侧 canonical bytes 解析（或 re-canonicalize + re-hash + Rust parse + CAS publish）
    4. CAS 第二阶段（committed）——条件更新 latest_committed_generation
    5. **P0-1 整改**：CAS committed 后，把 CAS 中的解析结果 merge 到主 CodeGraph DB
       （file_instances / symbols / calls），并 upsert workspace_manifests。
       任一步失败不得 mark staging applied——抛异常让上层 staging entry 不追加。

    Args:
        peer_uid: agent 的 peer UID
        workspace_id: workspace ID（数字主键，与 daemon_workspaces.workspace_id 对应）
        msg: agent 消息，需包含 rel_path/agent_session_id/monotonic_seq/session_epoch
        ws_conn: workspace 数据库连接（含 workspace_active_session / file_generations /
                 workspace_manifests 表）
        cas_conn: CAS 数据库连接（若为 None 则跳过 CAS publish，仅做 generation CAS）
        canonical_bytes: 来自 UDS bytes frame 或 FD 的规范化文件内容（优先使用）；
                         为 None 时降级为从 abs_path 读取（仅用于兼容旧路径）
        workspace_root: workspace 根路径（仅在 canonical_bytes 为 None 时使用）
        codegraph_db_path: 主 CodeGraph DB 路径（如 ~/.callwarden/callwarden.db）；
                          非空时触发 P0-1 merge 步骤（断点 B 修复）
        workspace_root_path: workspace 根路径（用于 workspaces.root_path 字段）

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
        raise ProtocolError(
            f"no active session for workspace {workspace_id}",
            code="session_not_active",
        )
    if (incoming_session != active["active_session_id"]
            or incoming_epoch != active["active_session_epoch"]):
        raise ProtocolError(
            f"stale session rejected: incoming={incoming_session}:{incoming_epoch} "
            f"active={active['active_session_id']}:{active['active_session_epoch']}",
            code="stale_session",
        )

    incoming_gen = f"{incoming_epoch}:{incoming_seq}"

    # 2. CAS 第一阶段（seen）——原子更新 latest_seen_generation
    # C3：优先 Rust CasStore facade（BEGIN IMMEDIATE 单事务 + stale 拦截）；
    # 不可用时回退 Python SQL。
    if ws_db_path and _RUST_FILE_GEN_AVAILABLE:
        try:
            seen_ok = _callwarden_core.cas_file_generation_seen(
                ws_db_path, workspace_id, rel_path, incoming_session,
                incoming_epoch, incoming_seq)
        except Exception as e:
            raise ProtocolError(
                f"cas_file_generation_seen failed: {e}",
                code="cas_seen_failed",
            )
        if not seen_ok:
            return {"status": "stale_seq_dropped"}
    else:
        # fallback
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
            elif incoming_seq < row["latest_seq"]:
                # 严格小于——stale seq 直接丢弃，不报错
                ws_conn.execute("ROLLBACK")
                return {"status": "stale_seq_dropped"}
            elif (incoming_seq == row["latest_seq"]
                  and row["latest_committed_generation"] == incoming_gen):
                # 同 seq 且已 committed——幂等返回，避免重复 merge
                ws_conn.execute("ROLLBACK")
                return {"status": "stale_seq_dropped"}
            else:
                # P0-2 整改（2026-07-22）：同 seq 但 latest_committed_generation 为空
                # （上次 seen 后 step 5 merge 失败）→ 允许重新处理。
                # 旧逻辑 `incoming_seq <= row["latest_seq"]` 会把这种重试判 stale 丢弃，
                # 导致事件永久无法恢复。新逻辑只在已 committed 时才幂等丢弃。
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

    # 3. daemon 侧解析 + CAS publish
    # 规范：daemon-ipc-security.md §3.2 —— daemon 不信任 agent 提供的 hash，必须重新计算
    # 规范：parse-input-abi.md §2 —— canonical bytes 是唯一输入入口
    cas_result = _daemon_parse_and_publish(
        rel_path=rel_path,
        canonical_bytes=canonical_bytes,
        abs_path=msg.get("abs_path") or _join_path(workspace_root, rel_path),
        cas_conn=cas_conn,
        workspace_id=workspace_id,
    )

    # C6（S2）：失败 generation 保护——镜像 Rust workspace.rs P0-4 R3 保护块
    # （snapshot_guard.rs::evaluate_generation_protection）。非 ready 状态
    # （parse_failed / publish_failed / unsupported_language / dirty overlay /
    # partial_published 等）不得推进 latest_committed_generation、不得追加
    # staging、不得 replicate——保护上一代好 snapshot（S2 验收点 5/7）。
    # 仅当真实 CAS 主链存在（cas_conn 非 None）时启用——镜像 Rust
    # cas_store=None → cas_result=None → cas_state="" → 不保护 的语义，
    # 保留 cas_conn=None 协议测试（committed）不变。
    if cas_conn is not None and cas_result:
        guard_abs_path = msg.get("abs_path") or _join_path(workspace_root, rel_path)
        protection = _evaluate_generation_protection(
            cas_result.get("cas_state", ""), guard_abs_path, rel_path)
        if protection["blocked"]:
            result = {"status": "blocked", "generation": incoming_gen}
            result.update(cas_result)
            result["protection"] = protection
            logger.warning(
                "C6 generation protection blocked (ws=%d, rel_path=%s, cas_state=%s, "
                "parse_status=%s, allows_retry=%s, reason=%s)",
                workspace_id, rel_path, protection["cas_state"],
                protection["parse_status"], protection["allows_retry"],
                protection["reason"],
            )
            return result

    # 5. P0-1 整改（2026-07-21）：CAS → CodeGraph DB merge + workspace_manifests 接入
    # 复审报告 §3 P0-1 / §8.1 第 1 条：建立真实 save-to-query 数据链。
    # 断点 B 修复：把 CAS 中的 cas_symbols / cas_raw_calls merge 到主 CodeGraph DB
    # 的 file_instances / symbols / calls 表，让 publish_snapshot 加载到新数据。
    # 断点 A 修复：upsert workspace_manifests，让 daemon 能回答"当前 workspace 有哪些文件"。
    # 失败语义：任一步失败抛异常，上层 daemon_server.py 不会追加 staging entry，
    # 不标 applied（满足 §8.1 "任一步失败不得 mark staging applied"）。
    merge_result = None
    if cas_result and cas_result.get("cas_state") in ("ready_published", "ready_cache_hit"):
        cas_key = cas_result.get("cas_key", "")
        content_hash_for_merge = cas_result.get("content_hash", "")

        # 5a. merge 到 CodeGraph DB（Rust 生产写路径优先短路）
        # C5 C2：区分"Rust 模块不可用"（fail-closed，C4 契约）与"Rust 可用但
        # merge 数据失败"（对齐 Rust workspace.rs merge_ok 门控：失败不阻断，
        # staging 由上层 append 为 pending、不 committed generation、不 replicate，
        # 同 seq 重试可恢复）。
        if codegraph_db_path and cas_key:
            merged_via_rust = False
            rust_merge_available = False
            merge_error = None
            # C5 C2 (P1-3 修复)：Rust facade 返回的 merge_status 原样透传
            # （merged/no_symbols=成功；cas_miss/error/open_failed=merge 失败门控）。
            # 修复前 Python 只看 success 字段，cas_miss（success=true）会被误判为
            # merge 成功并推进 committed，与 Rust workspace.rs merge_ok 门控不一致。
            rust_merge_status = ""
            # 语言探测：函数内 import（与兼容回滚路径的局部 import 一致，
            # 避免 UnboundLocalError——该名字在函数内被视为局部变量）
            from config import detect_language_from_path
            language_for_merge = detect_language_from_path(rel_path) or ""
            try:
                from callwarden_core import cas_merge_to_codegraph
                rust_merge_available = True
                rust_res = cas_merge_to_codegraph(
                    cas_db_path=cas_db_path or (_sqlite_main_db_path(cas_conn) or ""),
                    codegraph_db_path=codegraph_db_path,
                    cas_key=cas_key,
                    workspace_id=workspace_id,
                    rel_path=rel_path,
                    abs_path=msg.get("abs_path") or _join_path(workspace_root, rel_path),
                    content_hash=content_hash_for_merge,
                    language=language_for_merge,
                    workspace_root_path=workspace_root_path,
                )
                if rust_res and rust_res.get("success"):
                    # P1-3：success=true 不代表 merge_ok——Rust 的 cas_miss 走
                    # success=true + merge_status=cas_miss（Result Ok 分支）。
                    rust_merge_status = rust_res.get("merge_status", "merged") or "merged"
                    if rust_merge_status in ("cas_miss", "error", "open_failed"):
                        # C5 C2：对齐 Rust merge_ok 门控，按 merge 数据失败处理
                        merge_error = (
                            f"cas_merge_to_codegraph merge_status={rust_merge_status}"
                        )
                    else:
                        merged_via_rust = True
                        # C4：Rust 成功时同样构造 merge_result（结构对齐 Python fallback），
                        # 保证调用方 result["merge"] 契约不因短路路径丢失。
                        merge_result = {
                            "cas_key": cas_key,
                            "workspace_id": workspace_id,
                            "file_instance_id": rust_res.get("file_instance_id", 0),
                            "symbols_inserted": rust_res.get("symbols_inserted", 0),
                            "calls_inserted": rust_res.get("calls_inserted", 0),
                            "merge_status": rust_merge_status,
                        }
                        logger.info(
                            "P0-1 Rust merge done: cas_key=%s ws=%d symbols=%d calls=%d status=%s",
                            cas_key, workspace_id,
                            rust_res.get("symbols_inserted", 0),
                            rust_res.get("calls_inserted", 0),
                            rust_merge_status,
                        )
                else:
                    # C5 C2：Rust 返回 success=false（SQL 异常等）→ merge 数据失败门控
                    merge_error = f"cas_merge_to_codegraph success=false: {rust_res}"
            except ImportError as ie:
                logger.debug("Rust cas_merge_to_codegraph unavailable: %s", ie)
            except Exception as re:
                # C5 C2：Rust 可用但 merge 数据失败 → 不阻断（对齐 Rust merge_ok
                # 门控），记录 error，后续由上层 append staging（pending）、
                # 不 committed、不 replicate
                merge_error = str(re)
                logger.warning(
                    "P0-1 Rust cas_merge_to_codegraph failed (cas_key=%s, ws=%d): %s",
                    cas_key, workspace_id, re,
                )

            if not merged_via_rust:
                if not rust_merge_available:
                    # Rust 模块不可用 → fail-closed（C4 契约：未显式回滚时禁止
                    # 打开 Python DB 写事务）
                    if not _is_rust_cas_write_rolled_back():
                        raise ProtocolError(
                            "Rust CAS merge unavailable or failed; "
                            "set rollback_config.rust_cas_write=1 for an explicit compatibility rollback",
                            code="rust_cas_merge_unavailable",
                        )
                elif merge_error is not None and not _is_rust_cas_write_rolled_back():
                    # C5 C2：Rust 可用但 merge 数据失败 → 对齐 Rust merge_ok 门控：
                    # 不抛异常；构造 error/cas_miss/open_failed merge_result；跳过
                    # upsert_manifest 与 committed（latest_committed_generation 不
                    # 推进），返回 CAS 层 committed 状态（与 Rust RefreshResult.status
                    # 一致），上层据此 append staging（pending）但不 replicate。
                    # P1-3：merge_status 透传 Rust 原值（cas_miss/error/open_failed），
                    # 不再统一折叠为 error，消除 daemon_server 层死代码判断。
                    merge_status_out = rust_merge_status or "error"
                    merge_result = {
                        "cas_key": cas_key,
                        "workspace_id": workspace_id,
                        "file_instance_id": 0,
                        "symbols_inserted": 0,
                        "calls_inserted": 0,
                        "merge_status": merge_status_out,
                        "error": merge_error,
                    }
                    logger.error(
                        "C5 C2 merge failed (cas_key=%s, ws=%d, status=%s): %s — "
                        "staging 由上层 append 为 pending，不 committed/不 replicate，同 seq 可重试",
                        cas_key, workspace_id, merge_status_out, merge_error,
                    )
                    result = {"status": "committed", "generation": incoming_gen}
                    if cas_result:
                        result.update(cas_result)
                    result["merge"] = merge_result
                    return result

                # 兼容回滚路径：只有 rollback_config 明确允许时才打开 Python DB 写事务。
                cg_conn = None
                try:
                    from callwarden.db.db_cas_merge import merge_cas_to_codegraph

                    # 打开 CodeGraph DB 连接（用户级单库，schema 假设已初始化）
                    # WAL 模式下与 CLI/MCP 并发安全（AGENTS.md 规则 2）
                    cg_conn = sqlite3.connect(codegraph_db_path, timeout=10.0)
                    cg_conn.row_factory = sqlite3.Row
                    schema_row = cg_conn.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type='table' AND name='file_instances'"
                    ).fetchone()
                    if schema_row is None:
                        raise RuntimeError(
                            f"CodeGraph DB schema 未初始化：缺少 file_instances 表，db={codegraph_db_path}"
                        )

                    abs_path_for_merge = msg.get("abs_path") or _join_path(
                        workspace_root, rel_path
                    )
                    language_for_merge = ""
                    try:
                        from config import detect_language_from_path
                        language_for_merge = detect_language_from_path(rel_path) or ""
                    except ImportError:
                        pass

                    merge_result = merge_cas_to_codegraph(
                        cas_conn=cas_conn,
                        codegraph_conn=cg_conn,
                        cas_key=cas_key,
                        workspace_id=workspace_id,
                        rel_path=rel_path,
                        abs_path=abs_path_for_merge,
                        content_hash=content_hash_for_merge,
                        language=language_for_merge,
                        workspace_root_path=workspace_root_path,
                    )
                    logger.warning(
                        "P0-1 Python compatibility merge used after explicit rollback: "
                        "cas_key=%s ws=%d symbols=%d calls=%d status=%s",
                        cas_key, workspace_id,
                        merge_result.get("symbols_inserted", 0),
                        merge_result.get("calls_inserted", 0),
                        merge_result.get("merge_status", ""),
                    )
                except Exception as e:
                    logger.error(
                        "P0-1 compatibility merge failed (cas_key=%s, ws=%d): %s",
                        cas_key, workspace_id, e,
                    )
                    raise ProtocolError(
                        f"CAS merge to CodeGraph DB failed: {e}",
                        code="cas_merge_failed",
                    )
                finally:
                    if cg_conn is not None:
                        cg_conn.close()

        # 5b. upsert workspace_manifests（断点 A 修复）
        # ws_conn 已含 workspace_manifests 表（init_session_schema 已初始化）
        try:
            from callwarden.db.db_workspace_manifest import upsert_manifest
            # 检测表是否存在（首次使用可能未初始化）
            manifest_check = ws_conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='workspace_manifests'"
            ).fetchone()
            if manifest_check is None:
                # 延迟初始化 manifest schema
                from callwarden.db.db_workspace_manifest import init_manifest_schema
                init_manifest_schema(ws_conn)

            upsert_manifest(
                conn=ws_conn,
                workspace_id=workspace_id,
                rel_path=rel_path,
                content_hash=content_hash_for_merge,
                cas_key=cas_key,
                file_size=len(canonical_bytes) if canonical_bytes else 0,
                is_dirty=True,
            )
        except Exception as e:
            # manifest 失败——抛异常让上层不追加 staging entry
            logger.error(
                "P0-1 upsert_manifest failed (ws=%d, rel_path=%s): %s",
                workspace_id, rel_path, e,
            )
            raise ProtocolError(
                f"upsert_manifest failed: {e}",
                code="manifest_upsert_failed",
            )

    # 4. CAS 第二阶段（committed）——条件更新 latest_committed_generation
    # P0-2 整改（2026-07-22）：移到 merge/manifest 之后，避免后半段失败后
    # latest_committed_generation 已提交但 merge 未完成，同一 seq 重试被判 stale 丢弃
    # C3：优先 Rust CasStore facade（条件 UPDATE 单事务）；不可用时回退 Python SQL。
    if ws_db_path and _RUST_FILE_GEN_AVAILABLE:
        try:
            committed_ok = _callwarden_core.cas_file_generation_committed(
                ws_db_path, workspace_id, rel_path, incoming_epoch, incoming_seq)
        except Exception as e:
            raise ProtocolError(
                f"cas_file_generation_committed failed: {e}",
                code="cas_committed_failed",
            )
        if not committed_ok:
            raise ProtocolError(
                f"stale manifest commit for {rel_path}",
                code="stale_manifest_commit",
            )
    else:
        # fallback
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
                raise ProtocolError(
                    f"stale manifest commit for {rel_path}",
                    code="stale_manifest_commit",
                )
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
    if merge_result:
        result["merge"] = merge_result
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


def _sqlite_main_db_path(conn: sqlite3.Connection) -> Optional[str]:
    """返回连接的主数据库路径，供 Rust 短连接复用同一个 CAS 文件。"""
    try:
        for _seq, name, path in conn.execute("PRAGMA database_list").fetchall():
            if name == "main" and path:
                return str(path)
    except Exception:
        logger.debug("无法探测 CAS SQLite 主库路径", exc_info=True)
    return None


def _daemon_parse_and_publish(
    rel_path: str,
    canonical_bytes: Optional[bytes] = None,
    abs_path: str = "",
    cas_conn: Optional[sqlite3.Connection] = None,
    workspace_id: int = 0,
) -> Optional[Dict[str, Any]]:
    """daemon 侧解析 + CAS publish——消除 TOCTOU。

    规范：daemon-ipc-security.md §3.2 —— daemon 重新计算 sha256，不信任 agent 提供的 hash
    规范：parse-input-abi.md §2 —— canonical bytes 是唯一输入入口
    规范：cas-gc-protocol.md §3 —— CAS 原子发布四阶段
    修复：T-1783952125417-7a09（TOCTOU + 禁止读客户端 abs_path）

    输入优先级：
    1. canonical_bytes 非 None：直接 hash + parse_canonical_bytes_py（不读文件）
    2. canonical_bytes 为 None：降级从 abs_path 读取（兼容旧路径）

    Args:
        rel_path: 相对路径（用于语言检测）
        canonical_bytes: 来自 UDS bytes frame 或 FD 的规范化文件内容
        abs_path: 绝对路径（仅在 canonical_bytes 为 None 时使用）
        cas_conn: CAS 数据库连接
        workspace_id: workspace ID（用于 cas_pin）

    Returns:
        {"content_hash": str, "cas_key": str, "cas_state": str, ...} 或 None
    """
    import hashlib

    # 3a. 检测语言
    try:
        from config import detect_language_from_path
        language = detect_language_from_path(rel_path)
    except ImportError:
        language = ""
    if not language:
        return {"content_hash": "", "cas_key": "", "cas_state": "unsupported_language"}

    # 3b. canonicalize + re-hash
    canonicalize_method = "direct_bytes"
    content_hash = ""

    if canonical_bytes is not None:
        # 优先路径：daemon 已从 UDS bytes frame / FD 获得规范化内容
        content_hash = hashlib.sha256(canonical_bytes).hexdigest()
    else:
        # 降级路径：从 abs_path 读取（兼容旧 refresh 模式）
        canonicalize_method = "abs_path_fallback"
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
        from callwarden.db.db_cas import (
            compute_cas_key_v1,
            cas_publish_with_retry as python_cas_publish_with_retry,
            cas_lookup as python_cas_lookup,
            cas_pin as python_cas_pin,
        )
    except ImportError:
        return {"content_hash": content_hash, "cas_key": "",
                "cas_state": "cas_module_unavailable",
                "canonicalize_method": canonicalize_method}

    # 企业 daemon 与兼容 server 必须共用 Rust CasStore 的发布协议。
    # 只有旧 wheel、内存数据库或显式 rollback 时才保留 Python 兼容路径。
    cas_db_path = _sqlite_main_db_path(cas_conn)
    rust_cas_lookup = getattr(_callwarden_core, "cas_global_lookup", None)
    rust_cas_pin = getattr(_callwarden_core, "cas_pin", None)
    rust_cas_publish = getattr(_callwarden_core, "cas_publish_with_retry", None)
    use_rust_cas = bool(
        cas_db_path
        and not _is_rust_cas_write_rolled_back()
        and rust_cas_lookup
        and rust_cas_pin
        and rust_cas_publish
    )

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
    if use_rust_cas:
        existing = rust_cas_lookup(cas_db_path, cas_key)
    else:
        existing = python_cas_lookup(cas_conn, cas_key)
    if existing:
        try:
            if use_rust_cas:
                rust_cas_pin(cas_db_path, cas_key, workspace_id, 3600.0)
            else:
                python_cas_pin(cas_conn, cas_key, workspace_id)
        except Exception as e:
            logger.warning("cas_pin failed for cas_key=%s: %s", cas_key, e)
        return {"content_hash": content_hash, "cas_key": cas_key,
                "cas_state": "ready_cache_hit", "canonicalize_method": canonicalize_method}

    # 3f. CAS 未命中——用同一份 canonical_bytes 做 parse（消除 TOCTOU）
    parse_result = None
    try:
        from callwarden_core import parse_canonical_bytes_py
        module_path = rel_path.rsplit(".", 1)[0].replace("/", ".").replace("\\", ".")
        parse_result = parse_canonical_bytes_py(
            canonical_bytes, module_path, language, content_hash
        )
    except ImportError:
        # parse_canonical_bytes_py 不可用——降级 parse_file_lang
        if abs_path:
            try:
                from callwarden_core import parse_file_lang
                parse_result = parse_file_lang(abs_path, "")
            except Exception as e:
                logger.warning("Rust parse_file_lang fallback failed for %s: %s", abs_path, e)
    except Exception as e:
        logger.warning("Rust parse_canonical_bytes_py failed: %s", e)

    if parse_result is None:
        return {"content_hash": content_hash, "cas_key": cas_key,
                "cas_state": "parse_failed",
                "canonicalize_method": canonicalize_method}

    # 3g. CAS 原子发布（带 retry）
    try:
        if use_rust_cas:
            rust_cas_publish(
                cas_db_path, cas_key, content_hash, language, parse_result,
                workspace_id=workspace_id, max_retries=3,
                parser_version=parser_version, callwarden_version=callwarden_version,
                extraction_config_version=extraction_config_version,
                abi_version=abi_version, input_abi_version=input_abi_version,
            )
        else:
            python_cas_publish_with_retry(
                cas_conn, cas_key, content_hash, language, parse_result,
                workspace_id=workspace_id, max_retries=3,
                parser_version=parser_version, callwarden_version=callwarden_version,
                extraction_config_version=extraction_config_version,
                abi_version=abi_version, input_abi_version=input_abi_version,
            )
        return {"content_hash": content_hash, "cas_key": cas_key,
                "cas_state": "ready_published", "canonicalize_method": canonicalize_method}
    except Exception as e:
        logger.error("CAS publish failed (cas_key=%s): %s", cas_key, e)
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
        workspace_id_num: int = 0,
    ) -> ReplicationResult:
        """
        执行一次 replication：读取 pending → 发布新 generation → 标记 applied。

        参数：
            workspace_id: workspace 实例 ID（字符串）
            db_path: SQLite 数据库路径（用于 publish_snapshot）
            build_context_hash: build context 哈希
            workspace_id_num: workspace 数字主键（P0-2 整改：用于 GraphStore SQL 过滤，
                0 表示不过滤，生产路径应传 >0）

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
                        workspace_id=workspace_id_num,
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

            # 4. 批量标记 entries 为 applied（单次文件重写）
            result.applied_lsns = [entry.lsn for entry in pending]
            if result.applied_lsns:
                self.staging_log.mark_applied_batch(result.applied_lsns)

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

    def recover(self, workspace_id: str, db_path: str = "", workspace_id_num: int = 0) -> ReplicationResult:
        """
        从 crash 恢复：读取所有 pending entries 并重新 replication。

        在 daemon 启动时调用。

        参数：
            workspace_id: workspace 实例 ID
            db_path: SQLite 数据库路径
            workspace_id_num: workspace 数字主键（P0-2 整改，0=不过滤）

        返回：ReplicationResult
        """
        logger.info("recovering from staging log for ws=%s", workspace_id)
        return self.replicate(workspace_id, db_path, workspace_id_num=workspace_id_num)

    def get_pending_count(self, workspace_id: Optional[str] = None) -> int:
        """
        获取 pending entries 数量。

        参数：
            workspace_id: 如果指定，只返回该 workspace 的 pending 数量

        返回：pending 数量

        Phase 1-4 wire-production: 优先走 Rust 短路(callwarden_core.replicator_get_pending_count),
        rollback_config 控制(feature=rust_replicator_snapshot_query);未安装或 rolled back 时降级到 Python。
        Rust 路径在 Rust 端完成读取+过滤,只返回 count,避免跨语言数据传输。
        """
        # Phase 1-4 wire-production: Rust 短路 + rollback 控制
        if _RUST_REPLICATOR_QUERY_AVAILABLE and not _is_rust_replicator_query_rolled_back():
            try:
                return _callwarden_core.replicator_get_pending_count(
                    self.staging_log.log_path, workspace_id
                )
            except Exception:
                pass  # Rust 路径异常,降级到 Python
        pending = self.staging_log.read_pending()
        if workspace_id:
            return sum(1 for e in pending if e.workspace_id == workspace_id)
        return len(pending)

    def __repr__(self) -> str:
        return f"Replicator(staging_log={self.staging_log})"
