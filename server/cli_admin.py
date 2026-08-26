"""cli_admin.py —— CLI 本地维护命令的 daemon RPC 薄适配层（SRV-004）。

SRV-004（T-1787323460580-bea19180）：原模块中五个直接 open SQLite 的 Python
authority 函数已全部下沉为 Rust daemon handler
（`rust_ext/src/daemon/cli_admin_handlers.rs`，方法族 `mcp.cli_admin.*`）。
本模块不再 `import sqlite3`、不再 open 本地 DB、不再执行业务 SQL，
仅保留：daemon RPC 调用、JSON 结果整形、默认路径计算等非业务适配职责。

fail-closed：daemon 不可用时由 `_call_daemon_rpc` 抛错上抛，
绝不回退 Python SQLite 充当业务存储。

函数与 RPC 方法对应关系：
- `connection_test`       → `mcp.cli_admin.connection_test`
- `open_readonly_conn`    → `mcp.cli_admin.open_readonly_conn`（探测语义，见下）
- `read_pragmas`          → `mcp.cli_admin.read_pragmas`
- `read_task_dependencies`→ `mcp.cli_admin.read_task_dependencies`
- `scan_hash_databases`   → `mcp.cli_admin.scan_hash_databases`

`get_default_db_path` / `migrate_single_db` 不属于 SQLite authority：
前者为纯路径计算（进程启动配置读取职责），后者委托 `db_migrate` 权威模块。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Tuple

from callwarden.config import DB_PATH, CALLWARDEN_DIR


def get_default_db_path() -> str:
    """返回用户级单库默认路径（~/.callwarden/callwarden.db）。"""
    return os.environ.get("CALLWARDEN_DB") or DB_PATH


def open_readonly_conn(db_path: str = "") -> Dict[str, Any]:
    """只读连接可用性探测（SRV-004：RPC 无法传递连接对象，下沉为探测语义）。

    经 daemon `mcp.cli_admin.open_readonly_conn` 在 daemon 进程内以 mode=ro
    打开连接并执行 `SELECT 1` 后立即关闭；不返回连接对象。

    Args:
        db_path: SQLite 库路径（空 → daemon 默认用户级单库路径）。

    Returns:
        {"db_path": str, "readonly": True, "openable": bool, "error": str|None}

    Raises:
        daemon 不可用时由 `_call_daemon_rpc` 抛错（fail-closed）。
    """
    result = _call_daemon_rpc(
        "mcp.cli_admin.open_readonly_conn", {"db_path": db_path}
    )
    if not isinstance(result, dict):
        raise RuntimeError(
            f"open_readonly_conn: daemon 返回非对象结果: {result!r}"
        )
    return result


def read_pragmas(db_path: str, keys: List[str]) -> Dict[str, str]:
    """只读读取一组 PRAGMA 的实际值（失败键/未知键返回空串）。

    经 daemon `mcp.cli_admin.read_pragmas`（静态 PRAGMA 白名单分派）。

    Args:
        db_path: SQLite 库路径。
        keys: 要读取的 pragma 键（journal_mode/synchronous/busy_timeout/...）。

    Returns:
        {pragma_key: actual_value_str}；库不可打开时全部为空串。
    """
    result = _call_daemon_rpc(
        "mcp.cli_admin.read_pragmas", {"db_path": db_path, "keys": list(keys)}
    )
    if not isinstance(result, dict) or not isinstance(result.get("pragmas"), dict):
        raise RuntimeError(f"read_pragmas: daemon 返回非预期结果: {result!r}")
    return {str(k): str(v) for k, v in result["pragmas"].items()}


def connection_test(db_path: str, rounds: int = 5) -> Tuple[int, int]:
    """快速连接测试：daemon 内连续打开只读连接执行 SELECT 1。

    经 daemon `mcp.cli_admin.connection_test`。

    Args:
        db_path: SQLite 库路径。
        rounds: 测试次数（默认 5）。

    Returns:
        (success_count, fail_count)。
    """
    result = _call_daemon_rpc(
        "mcp.cli_admin.connection_test", {"db_path": db_path, "rounds": rounds}
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"connection_test: daemon 返回非对象结果: {result!r}")
    return int(result.get("success", 0)), int(result.get("fail", 0))


def scan_hash_databases(callwarden_dir: str = CALLWARDEN_DIR) -> List[Dict[str, Any]]:
    """扫描旧版 16 位 hex hash 数据库目录，读取每个库的 workspaces 表。

    经 daemon `mcp.cli_admin.scan_hash_databases`（daemon 进程内只读扫描）。
    供 `cw gc db-cleanup` 使用：CLI 侧只做孤儿判定与删除决策。

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
    result = _call_daemon_rpc(
        "mcp.cli_admin.scan_hash_databases", {"callwarden_dir": callwarden_dir}
    )
    if not isinstance(result, dict) or not isinstance(result.get("databases"), list):
        raise RuntimeError(
            f"scan_hash_databases: daemon 返回非预期结果: {result!r}"
        )
    return list(result["databases"])


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

    经 daemon `mcp.cli_admin.read_task_dependencies`；任一查询失败该列表为 []，
    db 不可打开全部为空列表（与下沉前语义一致）。

    Args:
        workspace_id: 数值 workspace id。
        task_id: 任务 id（与 contract_id 二选一）。
        contract_id: 契约 id（与 task_id 二选一）。
        revision: 契约版本（>0 时精确匹配版本）。
        db_path: 用户级单库路径（空 → daemon 默认路径）。

    Returns:
        {"dependencies": [...], "artifacts": [...], "interfaces": [...]}。
    """
    result = _call_daemon_rpc(
        "mcp.cli_admin.read_task_dependencies",
        {
            "workspace_id": workspace_id,
            "task_id": task_id,
            "contract_id": contract_id,
            "revision": revision,
            "db_path": db_path,
        },
    )
    if not isinstance(result, dict):
        raise RuntimeError(
            f"read_task_dependencies: daemon 返回非对象结果: {result!r}"
        )
    return {
        "dependencies": list(result.get("dependencies") or []),
        "artifacts": list(result.get("artifacts") or []),
        "interfaces": list(result.get("interfaces") or []),
    }


def _call_daemon_rpc(method: str, params: Dict[str, Any]) -> Any:
    """经 daemon 统一 fail-closed 客户端发起 RPC（不回退本地 SQLite）。"""
    from ._mcp_common import _call_daemon_rpc as _rpc

    return _rpc(method, params)
