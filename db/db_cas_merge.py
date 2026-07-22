"""CAS → CodeGraph DB merge 层（P0-1 整改 2026-07-21）。

复审报告 §3 P0-1 / §8.1 第 1 条要求：建立真实
`agent start → register/connect → refresh → apply manifest/query DB → publish → query(min_generation)`
E2E；任一步失败不得 mark staging applied。

**断点 B 修复**：daemon_handle_refresh CAS committed 后，CAS 中的解析结果
（`cas_symbols` / `cas_raw_calls` / `cas_file_cache`）从未 merge 到主 CodeGraph DB
（`~/.callwarden/callwarden.db`）的 `file_instances` / `symbols` / `calls` 表。
导致 `publish_snapshot` 加载到 GraphSnapshot 的是 STALE 数据，查询仍返回旧符号。

本模块实现最小侵入的 merge：
1. UPSERT `workspaces`（按 workspace_id 数字主键，name 用 `daemon_ws_{id}` 保证唯一）
2. UPSERT `file_contents`（content_hash, language, total_lines）
3. UPSERT `file_instances`（workspace_id, rel_path, abs_path, current_content_hash, ...）
4. DELETE 旧 `symbols` WHERE file_instance_id = ?（同文件符号全替换）
5. INSERT 新 `symbols`（从 cas_symbols 读，symbol_hash 直接用 symbol_content_hash）
6. DELETE 旧 `calls` WHERE caller_id IN (file_instance 旧 symbols)
7. INSERT 新 `calls`（从 cas_raw_calls 读，caller_id 关联到新插入的 symbols.id）

**范围说明**（最小侵入）：
- 跨文件调用解析（resolve）不在本步骤范围；cas_raw_calls 只含单文件内调用
- FTS5 索引同步通过 `symbols_fts` 触发器自动触发（schema v31 已建立）
- file_versions 历史版本不在本步骤写入（保留给后续完整 build_full_graph 路径）
- lexical_parent_local_id 不转换（CodeGraph DB symbols 表无对应字段，用 depth 代替）

规范：
- AGENTS.md 规则 2：CodeGraph DB 用户级单库 `~/.callwarden/callwarden.db`
- AGENTS.md 规则 8：写操作走 CLI / Python API（本函数由 daemon_handle_refresh 直接调用）
- schema.py：workspaces / file_instances / symbols / calls / file_contents 表定义
- db_cas.py：cas_symbols / cas_raw_calls / cas_file_cache 表定义
"""

import os
import sqlite3
import time
from typing import Any, Dict, Optional, Tuple


def _module_path_from_rel(rel_path: str) -> str:
    """从 rel_path 推导 module_path（简化版）。

    完整实现见 db_build.py:_infer_module_path_generic（含 src/lib/app/main 前缀去除、
    index/__init__ 处理）。P0-1 整改（2026-07-21）使用简化版，避免引入 db_build.py
    依赖；后续如需更精确的模块路径，可在 daemon 启动时注入 _infer_module_path。

    Args:
        rel_path: 文件相对路径（如 "src/server/main.py"）

    Returns:
        模块路径（如 "src.server.main"）
    """
    path = rel_path.replace("\\", "/")
    # 去掉扩展名
    basename = os.path.basename(path)
    if "." in basename:
        path = path.rsplit(".", 1)[0]
    return path.replace("/", ".")


def _ensure_workspace_row(
    codegraph_conn: sqlite3.Connection,
    workspace_id: int,
    root_path: str,
) -> None:
    """确保 CodeGraph DB 中有对应 workspace_id 的 workspaces 行。

    INSERT OR IGNORE：若 workspace_id 已存在则跳过（不覆盖 name/root_path，
    避免与 CLI `cw --workspace` 注册的 workspace 冲突）。

    name 用 `daemon_ws_{workspace_id}` 格式保证 UNIQUE 约束不冲突；
    root_path 用传入的 host_real_root 或 client_view_root。

    Args:
        codegraph_conn: CodeGraph DB 连接
        workspace_id: 数字主键（与 daemon_workspaces.workspace_id 对应）
        root_path: workspace 根路径（用于 name 和 root_path 字段）
    """
    now = time.time()
    # INSERT OR IGNORE：已存在则跳过
    codegraph_conn.execute(
        "INSERT OR IGNORE INTO workspaces "
        "(id, name, root_path, created_at, is_active, description) "
        "VALUES (?, ?, ?, ?, 0, 'daemon-managed workspace')",
        (int(workspace_id), f"daemon_ws_{int(workspace_id)}", root_path, now),
    )


def _upsert_file_records(
    codegraph_conn: sqlite3.Connection,
    workspace_id: int,
    rel_path: str,
    abs_path: str,
    content_hash: str,
    language: str,
    total_lines: int,
    mtime: Optional[float] = None,
) -> int:
    """UPSERT file_contents + file_instances，返回 file_instance_id。

    Args:
        codegraph_conn: CodeGraph DB 连接
        workspace_id: 数字 workspace_id
        rel_path: 文件相对路径
        abs_path: 绝对路径
        content_hash: 文件内容 SHA-256
        language: 语言 ID
        total_lines: 总行数
        mtime: 修改时间（None 用当前时间）

    Returns:
        file_instance_id（CodeGraph DB file_instances.id）
    """
    now = mtime if mtime is not None else time.time()
    module_path = _module_path_from_rel(rel_path)

    # file_contents：INSERT OR REPLACE（content_hash 主键）
    codegraph_conn.execute(
        "INSERT OR REPLACE INTO file_contents "
        "(content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, ?, ?, ?)",
        (content_hash, language, int(total_lines), now),
    )

    # file_instances：先查现有 id
    row = codegraph_conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
        (int(workspace_id), rel_path),
    ).fetchone()

    if row is not None:
        file_instance_id = int(row["id"]) if isinstance(row, sqlite3.Row) else int(row[0])
        # UPDATE 现有行
        codegraph_conn.execute(
            "UPDATE file_instances SET "
            "abs_path = ?, current_content_hash = ?, mtime = ?, "
            "total_lines = ?, last_parsed = ?, status = 'parsed', module_path = ? "
            "WHERE id = ?",
            (abs_path, content_hash, now, int(total_lines), now, module_path,
             file_instance_id),
        )
    else:
        # INSERT 新行
        cur = codegraph_conn.execute(
            "INSERT INTO file_instances "
            "(workspace_id, rel_path, abs_path, current_content_hash, mtime, "
            "total_lines, last_parsed, status, module_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', ?)",
            (int(workspace_id), rel_path, abs_path, content_hash, now,
             int(total_lines), now, module_path),
        )
        file_instance_id = int(cur.lastrowid)

    return file_instance_id


def _replace_symbols_and_calls(
    codegraph_conn: sqlite3.Connection,
    file_instance_id: int,
    cas_symbols: list,
    cas_raw_calls: list,
    rel_path: str,
) -> Tuple[int, int]:
    """替换 file_instance 对应的 symbols + calls。

    流程：
    1. 查询旧 symbol_ids（file_instance_id 下所有 symbols.id）
    2. DELETE calls WHERE caller_id IN (旧 symbol_ids)
    3. DELETE symbols WHERE file_instance_id = ?
    4. INSERT 新 symbols（从 cas_symbols 读）
    5. INSERT 新 calls（从 cas_raw_calls 读，caller_id 关联到新 symbols.id）

    Args:
        codegraph_conn: CodeGraph DB 连接
        file_instance_id: file_instances.id
        cas_symbols: cas_symbols 行列表（dict-like，含 local_symbol_id /
            symbol_content_hash / name / local_qualified_name / kind /
            start_line / end_line / start_col / end_col / visibility /
            signature / has_comment / depth）
        cas_raw_calls: cas_raw_calls 行列表（含 caller_local_id /
            caller_name / callee_name / call_line）
        rel_path: 文件相对路径（用于 calls.callee_file）

    Returns:
        (inserted_symbols_count, inserted_calls_count)
    """
    module_path = _module_path_from_rel(rel_path)

    # 1. 查询旧 symbol_ids
    old_sym_ids = [
        int(r["id"]) if isinstance(r, sqlite3.Row) else int(r[0])
        for r in codegraph_conn.execute(
            "SELECT id FROM symbols WHERE file_instance_id = ?",
            (file_instance_id,),
        ).fetchall()
    ]

    # 2. 删除旧 calls（通过 caller_id 关联）
    # 2b. 入边清理——把指向旧 symbols 的 callee_id 置 0（P0-2 整改 2026-07-22）
    if old_sym_ids:
        placeholders = ",".join("?" * len(old_sym_ids))
        codegraph_conn.execute(
            f"DELETE FROM calls WHERE caller_id IN ({placeholders})",
            old_sym_ids,
        )
        # 旧 symbols 即将被删除，指向它们的入边需置 0 避免悬空引用
        codegraph_conn.execute(
            f"UPDATE calls SET callee_id = 0 WHERE callee_id IN ({placeholders})",
            old_sym_ids,
        )

    # 3. 删除旧 symbols
    codegraph_conn.execute(
        "DELETE FROM symbols WHERE file_instance_id = ?",
        (file_instance_id,),
    )

    # 4. INSERT 新 symbols，构建 local_symbol_id → 全局 id 映射
    local_to_global: Dict[int, int] = {}
    for sym in cas_symbols:
        # 兼容 sqlite3.Row 和 dict
        def _get(key: str, default: Any = None) -> Any:
            if isinstance(sym, sqlite3.Row):
                return sym[key] if key in sym.keys() else default
            return sym.get(key, default)

        local_id = int(_get("local_symbol_id", 0))
        sym_hash = str(_get("symbol_content_hash", ""))
        name = str(_get("name", ""))
        kind = str(_get("kind", "function"))
        visibility = str(_get("visibility", "private"))
        start_line = int(_get("start_line", 0))
        end_line = int(_get("end_line", 0))
        start_col = int(_get("start_col", 0))
        end_col = int(_get("end_col", 0))
        signature = str(_get("signature", ""))
        has_comment = int(_get("has_comment", 0))
        depth = int(_get("depth", -1))
        qualified_name = str(_get("local_qualified_name", ""))

        # P0-2 整改（2026-07-22）：UPSERT symbol_contents
        # symbols.symbol_hash 指向 symbol_contents.content_hash，
        # 未写入 symbol_contents 会导致 JOIN 查询断链
        if sym_hash:
            codegraph_conn.execute(
                "INSERT OR IGNORE INTO symbol_contents "
                "(content_hash, name, kind, content, signature, has_comment, "
                "comment_content, qualified_name) "
                "VALUES (?, ?, ?, '', ?, ?, '', ?)",
                (sym_hash, name, kind, signature, has_comment, qualified_name),
            )

        cur = codegraph_conn.execute(
            "INSERT INTO symbols "
            "(file_instance_id, symbol_hash, name, kind, visibility, "
            "start_line, end_line, start_col, end_col, signature, has_comment, "
            "comment_status, module_path, qualified_name, depth) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (file_instance_id, sym_hash, name, kind, visibility,
             start_line, end_line, start_col, end_col, signature, has_comment,
             module_path, qualified_name, depth),
        )
        local_to_global[local_id] = int(cur.lastrowid)

    # 5. INSERT 新 calls
    inserted_calls = 0
    for call in cas_raw_calls:
        def _getc(key: str, default: Any = None) -> Any:
            if isinstance(call, sqlite3.Row):
                return call[key] if key in call.keys() else default
            return call.get(key, default)

        caller_local_id = _getc("caller_local_id")
        caller_global_id = (
            int(local_to_global[int(caller_local_id)])
            if caller_local_id is not None and int(caller_local_id) in local_to_global
            else 0
        )
        caller_name = str(_getc("caller_name", ""))
        callee_name = str(_getc("callee_name", ""))
        call_line = int(_getc("call_line", 0))

        # caller_id 为 0 时仍写入（保留调用关系，即使 caller 符号未找到）
        # 注意：calls.caller_id 有 FOREIGN KEY 约束指向 symbols.id，
        # 但 SQLite 默认不强制 FK（需 PRAGMA foreign_keys=ON），故 0 不会触发约束
        codegraph_conn.execute(
            "INSERT INTO calls "
            "(caller_id, caller_name, caller_module, callee_name, callee_module, "
            "callee_file, callee_id, call_line, is_cross_file) "
            "VALUES (?, ?, ?, ?, '', ?, 0, ?, 0)",
            (caller_global_id, caller_name, module_path,
             callee_name, rel_path, call_line),
        )
        inserted_calls += 1

    return (len(cas_symbols), inserted_calls)


def merge_cas_to_codegraph(
    cas_conn: sqlite3.Connection,
    codegraph_conn: sqlite3.Connection,
    cas_key: str,
    workspace_id: int,
    rel_path: str,
    abs_path: str,
    content_hash: str,
    language: str,
    workspace_root_path: str = "",
    mtime: Optional[float] = None,
) -> Dict[str, Any]:
    """把 CAS 中的解析结果 merge 到 CodeGraph DB 主表。

    规范：复审报告 §3 P0-1 / §8.1 第 1 条
    修复：T-1784644413771-8f1a2d37 Step 2（2026-07-21）

    Args:
        cas_conn: CAS 数据库连接（daemon 侧 per-workspace cas.db）
        codegraph_conn: 主 CodeGraph DB 连接（~/.callwarden/callwarden.db）
        cas_key: CAS key（用于查询 cas_symbols / cas_raw_calls / cas_file_cache）
        workspace_id: 数字 workspace_id（与 daemon_workspaces.workspace_id 对应）
        rel_path: 文件相对路径
        abs_path: 绝对路径
        content_hash: 文件内容 SHA-256（与 cas_file_cache.content_hash 一致）
        language: 语言 ID
        workspace_root_path: workspace 根路径（用于 workspaces.root_path，可空）
        mtime: 修改时间（None 用当前时间）

    Returns:
        {"cas_key": str, "workspace_id": int, "file_instance_id": int,
         "symbols_inserted": int, "calls_inserted": int,
         "merge_status": "merged" / "cas_miss" / "no_symbols"}
    """
    workspace_id = int(workspace_id)

    # 1. 查 CAS file cache（取 file_size / total_lines）
    cas_file = cas_conn.execute(
        "SELECT file_size, total_lines FROM cas_file_cache WHERE cas_key = ?",
        (cas_key,),
    ).fetchone()
    if cas_file is None:
        return {
            "cas_key": cas_key,
            "workspace_id": workspace_id,
            "file_instance_id": 0,
            "symbols_inserted": 0,
            "calls_inserted": 0,
            "merge_status": "cas_miss",
        }
    total_lines = int(cas_file["total_lines"]) if isinstance(cas_file, sqlite3.Row) else int(cas_file[1])

    # 2. 查 CAS symbols
    cas_symbols = cas_conn.execute(
        "SELECT local_symbol_id, symbol_content_hash, name, local_qualified_name, "
        "kind, start_line, end_line, start_col, end_col, visibility, signature, "
        "has_comment, depth "
        "FROM cas_symbols WHERE cas_key = ? ORDER BY local_symbol_id",
        (cas_key,),
    ).fetchall()

    # 3. 查 CAS raw calls
    cas_raw_calls = cas_conn.execute(
        "SELECT caller_local_id, caller_name, callee_name, call_line "
        "FROM cas_raw_calls WHERE cas_key = ?",
        (cas_key,),
    ).fetchall()

    # 4. 确保 CodeGraph DB 中有对应 workspace 行
    _ensure_workspace_row(
        codegraph_conn, workspace_id,
        root_path=workspace_root_path or abs_path,
    )

    # 5. UPSERT file_contents + file_instances
    file_instance_id = _upsert_file_records(
        codegraph_conn, workspace_id, rel_path, abs_path,
        content_hash, language, total_lines, mtime,
    )

    # 6. 替换 symbols + calls
    sym_count, call_count = _replace_symbols_and_calls(
        codegraph_conn, file_instance_id, cas_symbols, cas_raw_calls, rel_path,
    )

    # 7. 提交事务
    codegraph_conn.commit()

    return {
        "cas_key": cas_key,
        "workspace_id": workspace_id,
        "file_instance_id": file_instance_id,
        "symbols_inserted": sym_count,
        "calls_inserted": call_count,
        "merge_status": "merged" if cas_symbols else "no_symbols",
    }
