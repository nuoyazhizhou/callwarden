"""cli_admin.py —— CLI 本地维护命令的 daemon 侧数据库辅助（T04-followup S1）。

设计契约（cw-rust-client-convergence-design.md §1.3.2 / Q2 例外清单）：
- `cli/` 与 `server/tools/` 只实现 client 薄壳；本模块位于 `server/`（daemon
  宿主侧，非 MCP 薄壳层，不在 check_client_purity 扫描范围），承载 doctor /
  gc db-cleanup / db-migrate-single / dependency inspect 等本地维护命令所需的
  数据库直查逻辑——这些命令在 Q2 例外清单（doctor/config/install 等 daemon
  管理/自举命令保持本地），不进入 daemon RPC 写路径；
- CLI 侧禁止 `import sqlite3` / `db.conn` / `CodeGraphDB(`（硬门禁），因此
  本模块集中提供只读连接与 PRAGMA 读取，CLI 只调用纯函数接口；
- 本模块只读不写（gc 删除、db 迁移等写操作仍由 CLI 在拿到扫描结果后决定，
  或由 `db_migrate` 权威模块执行），不持有写锁、不触发 workspace 激活。
"""

from __future__ import annotations

import os
import sqlite3
from typing import Any, Dict, List, Tuple

from callwarden.config import DB_PATH, CALLWARDEN_DIR


def get_default_db_path() -> str:
    """返回用户级单库默认路径（~/.callwarden/callwarden.db）。"""
    return os.environ.get("CALLWARDEN_DB") or DB_PATH


def open_readonly_conn(db_path: str = ""):
    """打开只读 sqlite3 连接（供 CLI 侧只读分析引擎复用）。

    调用方负责 close()；连接为 mode=ro（不持有写锁、不触发 WAL 写入）。
    仅用于 daemon RPC 无法覆盖的只读本地分析（如 resolved_edges 计算），
    不用于业务写路径。
    """
    target = db_path or get_default_db_path()
    conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=3)
    conn.row_factory = sqlite3.Row
    return conn


# PRAGMA 检查：SQLite 不支持绑定参数，用静态 SQL 分派避免字符串拼接
# （semgrep: sqlalchemy-execute-raw-query / formatted-sql-query）。
_PRAGMA_QUERIES: Dict[str, str] = {
    "journal_mode": "PRAGMA journal_mode",
    "synchronous": "PRAGMA synchronous",
    "busy_timeout": "PRAGMA busy_timeout",
    "cache_size": "PRAGMA cache_size",
    "mmap_size": "PRAGMA mmap_size",
}


def read_pragmas(db_path: str, keys: List[str]) -> Dict[str, str]:
    """只读连接读取一组 PRAGMA 的实际值（失败键返回空串）。

    Args:
        db_path: SQLite 库路径。
        keys: 要读取的 pragma 键（journal_mode/synchronous/busy_timeout/...）。

    Returns:
        {pragma_key: actual_value_str}；库不可打开时全部为空串。
    """
    result: Dict[str, str] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        try:
            for key in keys:
                query = _PRAGMA_QUERIES.get(key)
                if not query:
                    result[key] = ""
                    continue
                try:
                    row = conn.execute(query).fetchone()
                    result[key] = str(row[0]) if row else ""
                except sqlite3.Error:
                    result[key] = ""
        finally:
            conn.close()
    except sqlite3.Error:
        for key in keys:
            result.setdefault(key, "")
    return result


def connection_test(db_path: str, rounds: int = 5) -> Tuple[int, int]:
    """快速连接测试：连续打开只读连接执行 SELECT 1。

    Args:
        db_path: SQLite 库路径。
        rounds: 测试次数（默认 5）。

    Returns:
        (success_count, fail_count)。
    """
    success = 0
    fail = 0
    for _ in range(rounds):
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
            success += 1
        except sqlite3.Error:
            fail += 1
    return success, fail


def scan_hash_databases(callwarden_dir: str = CALLWARDEN_DIR) -> List[Dict[str, Any]]:
    """扫描旧版 16 位 hex hash 数据库目录，读取每个库的 workspaces 表。

    供 `cw gc db-cleanup` 使用：CLI 侧只做孤儿判定与删除决策，SQL 读取集中
    在本模块（只读连接 mode=ro）。

    Args:
        callwarden_dir: ~/.callwarden 目录。

    Returns:
        每个 hash 目录一条记录：
        {
            "hash": 目录名,
            "dir": 目录绝对路径,
            "db_file": callwarden.db 路径,
            "workspaces": [{"id","name","root_path"}, ...] 或 [],
            "error": 读取失败原因（None 表示成功）,
        }
    """
    results: List[Dict[str, Any]] = []
    if not os.path.isdir(callwarden_dir):
        return results
    for name in sorted(os.listdir(callwarden_dir)):
        if len(name) != 16 or not all(c in "0123456789abcdef" for c in name):
            continue
        dir_path = os.path.join(callwarden_dir, name)
        db_file = os.path.join(dir_path, "callwarden.db")
        if not os.path.isfile(db_file):
            continue
        entry: Dict[str, Any] = {
            "hash": name,
            "dir": dir_path,
            "db_file": db_file,
            "workspaces": [],
            "error": None,
        }
        try:
            conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=3)
            conn.row_factory = sqlite3.Row
            try:
                rows = conn.execute(
                    "SELECT id, name, root_path FROM workspaces ORDER BY id"
                ).fetchall()
                entry["workspaces"] = [
                    {
                        "id": row["id"],
                        "name": row["name"] or "",
                        "root_path": row["root_path"] or "",
                    }
                    for row in rows
                ]
            finally:
                conn.close()
        except sqlite3.Error as exc:
            entry["error"] = f"read_error: {exc}"
        results.append(entry)
    return results


def migrate_single_db(dry_run: bool = True, backup: bool = True) -> Dict[str, Any]:
    """旧版多库 → 用户级单库迁移（委托 db_migrate 权威实现）。

    Args:
        dry_run: True 只报告不写入。
        backup: 迁移前创建备份。

    Returns:
        db_migrate.migrate_to_single_db 的结果 dict。
    """
    from callwarden.db.db_migrate import migrate_to_single_db

    return migrate_to_single_db(dry_run=dry_run, backup=backup)


def read_task_dependencies(
    workspace_id: int,
    task_id: str = "",
    contract_id: str = "",
    revision: int = 0,
    db_path: str = "",
) -> Dict[str, List[Dict[str, Any]]]:
    """只读查询任务/契约的依赖声明、产物与接口（供 `cw dependency inspect`）。

    Args:
        workspace_id: 数值 workspace id。
        task_id: 任务 id（与 contract_id 二选一）。
        contract_id: 契约 id（与 task_id 二选一）。
        revision: 契约版本（>0 时精确匹配版本）。
        db_path: 用户级单库路径（默认 config.DB_PATH）。

    Returns:
        {"dependencies": [...], "artifacts": [...], "interfaces": [...]}；
        任一查询失败时该列表为 []。
    """
    result: Dict[str, List[Dict[str, Any]]] = {
        "dependencies": [],
        "artifacts": [],
        "interfaces": [],
    }
    target = db_path or get_default_db_path()
    try:
        conn = sqlite3.connect(f"file:{target}?mode=ro", uri=True, timeout=3)
        conn.row_factory = sqlite3.Row
        try:
            if task_id:
                deps = conn.execute(
                    "SELECT dependency_type, target_ref, target_task_id, "
                    "is_informational, contract_id, contract_revision, declared_at "
                    "FROM task_dependencies WHERE workspace_id = ? AND task_id = ?",
                    (workspace_id, task_id),
                ).fetchall()
                result["dependencies"] = [dict(r) for r in deps]

                arts = conn.execute(
                    "SELECT artifact_id, artifact_type, artifact_ref, artifact_hash, "
                    "freshness_status, produced_at "
                    "FROM artifact_identities WHERE workspace_id = ? AND task_id = ?",
                    (workspace_id, task_id),
                ).fetchall()
                result["artifacts"] = [dict(r) for r in arts]

                ifaces = conn.execute(
                    "SELECT interface_id, interface_name, version, interface_hash "
                    "FROM interface_identities WHERE workspace_id = ? "
                    "AND provider_task_id = ?",
                    (workspace_id, task_id),
                ).fetchall()
                result["interfaces"] = [dict(r) for r in ifaces]
            elif contract_id:
                if revision > 0:
                    deps = conn.execute(
                        "SELECT dependency_type, target_ref, target_task_id, "
                        "is_informational, task_id, declared_at FROM task_dependencies "
                        "WHERE workspace_id = ? AND contract_id = ? "
                        "AND contract_revision = ?",
                        (workspace_id, contract_id, revision),
                    ).fetchall()
                else:
                    deps = conn.execute(
                        "SELECT dependency_type, target_ref, target_task_id, "
                        "is_informational, task_id, contract_revision, declared_at "
                        "FROM task_dependencies "
                        "WHERE workspace_id = ? AND contract_id = ?",
                        (workspace_id, contract_id),
                    ).fetchall()
                result["dependencies"] = [dict(r) for r in deps]
        finally:
            conn.close()
    except sqlite3.Error:
        pass
    return result
