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

_REPLICATOR_CAS_ROLLBACK_METHOD = "mcp.replicator.is_rust_cas_write_rolled_back"
_REPLICATOR_QUERY_ROLLBACK_METHOD = (
    "mcp.replicator.is_rust_replicator_query_rolled_back"
)
_REPLICATOR_REFRESH_METHOD = "mcp.replicator.daemon_handle_refresh"

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
    """检查 Rust CAS 写路径是否被显式回滚（daemon authority）。"""
    now = time.time()
    if now - _CAS_WRITE_ROLLBACK_CACHE["ts"] < _CAS_WRITE_ROLLBACK_CACHE_TTL:
        return bool(_CAS_WRITE_ROLLBACK_CACHE["value"])
    try:
        result = _call_daemon_rpc(_REPLICATOR_CAS_ROLLBACK_METHOD, {})
        value = bool(isinstance(result, dict) and result.get("rolled_back"))
    except Exception:
        # fail-soft：authority daemon 不可用时保持旧的未回滚语义。
        value = False
    _CAS_WRITE_ROLLBACK_CACHE["ts"] = now
    _CAS_WRITE_ROLLBACK_CACHE["value"] = value
    return value


def _is_rust_replicator_query_rolled_back() -> bool:
    """检查 replicator query Rust feature 是否已回滚（60s daemon RPC 缓存）。

    权威 rollback_config 由 Rust daemon 持有，Python 只负责缓存和 fail-soft。
    """
    now = time.time()
    if now - _REPLICATOR_ROLLBACK_CACHE["ts"] < _REPLICATOR_ROLLBACK_CACHE_TTL:
        return _REPLICATOR_ROLLBACK_CACHE["value"]  # type: ignore[return-value]
    try:
        result = _call_daemon_rpc(_REPLICATOR_QUERY_ROLLBACK_METHOD, {})
        value = bool(isinstance(result, dict) and result.get("rolled_back"))
    except Exception:
        # fail-soft：只读 authority 读失败时视为未回滚。
        value = False
    _REPLICATOR_ROLLBACK_CACHE["ts"] = now
    _REPLICATOR_ROLLBACK_CACHE["value"] = value
    return value


def _call_daemon_rpc(method: str, params: Dict[str, Any]) -> Any:
    """经 daemon 统一客户端发起 RPC，避免 replicator 与 client 循环依赖。"""
    from ._mcp_common import _call_daemon_rpc as _rpc

    return _rpc(method, params)

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
    """经 Rust daemon RPC 执行 refresh 主链，Python 只做参数序列化。

    旧签名参数保留为兼容形状，但不再被读取；Rust daemon 负责 workspace ACL、
    session epoch、CAS 两阶段、canonical bytes 校验、CodeGraph merge、manifest
    和 replicate。canonical bytes 使用 hex 传输，避免把 bytes 对象交给 JSON 序列化器。
    """
    del peer_uid, workspace_id, ws_conn, cas_conn, workspace_root
    del codegraph_db_path, workspace_root_path, ws_db_path, cas_db_path
    params = dict(msg)
    if canonical_bytes is not None:
        params["canonical_bytes_hex"] = canonical_bytes.hex()
    return _call_daemon_rpc(_REPLICATOR_REFRESH_METHOD, params)


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
