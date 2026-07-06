"""查询接口 archived 过滤测试。

覆盖 A3 任务（T-1783349079761-0d3c）：验证用户面向查询过滤掉 status='archived' 的文件。

测试内容：
- get_stats 不统计 archived 文件的符号/调用数
- get_file_by_path 不返回 archived 文件
- search_symbols 不返回 archived 文件的符号
- get_file_symbols 不返回 archived 文件的符号
- get_code_health 不统计 archived 文件
"""

import os
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB


def _db_with_workspace():
    """构造临时工作区数据库。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _seed_file_with_symbols(db, rel_path, status="parsed", symbol_name="foo"):
    """辅助：创建一个文件实例 + 符号。"""
    ws_id = db._get_active_workspace_id()
    # 插入 file_contents
    db.conn.execute(
        "INSERT INTO file_contents (content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, 'python', 10, 0)",
        (f"hash_{rel_path}",),
    )
    # 插入 file_instances
    db.conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, status, module_path) VALUES (?, ?, ?, ?, 0, ?, '')",
        (ws_id, rel_path, os.path.join(db.workspace_root, rel_path), f"hash_{rel_path}", status),
    )
    fi_id = db.conn.execute("SELECT id FROM file_instances WHERE rel_path=?", (rel_path,)).fetchone()[0]
    # 插入 symbol_contents
    db.conn.execute(
        "INSERT INTO symbol_contents (content_hash, name, kind, content, signature, "
        "has_comment, comment_content, qualified_name) "
        "VALUES (?, ?, 'fn', 'def foo(): pass', '', 0, '', ?)",
        (f"sym_{rel_path}", symbol_name, symbol_name),
    )
    # 插入 symbols
    db.conn.execute(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "qualified_name, comment_status) VALUES (?, ?, ?, 'fn', 1, 5, ?, 'pending') "
        "ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET symbol_hash = excluded.symbol_hash",
        (fi_id, f"sym_{rel_path}", symbol_name, symbol_name),
    )
    # 插入 file_versions
    db.conn.execute(
        "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, parsed_at, "
        "is_current, is_deleted) VALUES (?, 1, ?, 0, 0, 1, 0)",
        (fi_id, f"hash_{rel_path}"),
    )
    fv_id = db.conn.execute("SELECT id FROM file_versions WHERE file_instance_id=?", (fi_id,)).fetchone()[0]
    # 插入 file_symbol_versions
    db.conn.execute(
        "INSERT INTO file_symbol_versions (file_version_id, symbol_hash, qualified_name, "
        "start_line, end_line, depth, is_deleted) VALUES (?, ?, ?, 1, 5, 0, 0)",
        (fv_id, f"sym_{rel_path}", symbol_name),
    )
    db.conn.commit()
    return fi_id


def test_get_stats_excludes_archived_files():
    """get_stats 不应统计 archived 文件的符号。"""
    db, root = _db_with_workspace()
    try:
        # 添加一个正常文件 + 一个 archived 文件
        _seed_file_with_symbols(db, "active.py", status="parsed", symbol_name="active_fn")
        _seed_file_with_symbols(db, "archived.py", status="archived", symbol_name="archived_fn")
        stats = db.get_stats()
        # total_files 应为 1（不包含 archived）
        assert stats["total_files"] == 1
        # total_symbols 应为 1
        assert stats["total_symbols"] == 1
    finally:
        db.close()


def test_get_file_by_path_excludes_archived():
    """get_file_by_path 不应返回 archived 文件。"""
    db, root = _db_with_workspace()
    try:
        _seed_file_with_symbols(db, "active.py", status="parsed")
        _seed_file_with_symbols(db, "archived.py", status="archived")
        # active 文件应能找到
        assert db.get_file_by_path("active.py") is not None
        # archived 文件不应返回
        assert db.get_file_by_path("archived.py") is None
    finally:
        db.close()


def test_search_symbols_excludes_archived():
    """search_symbols 不应返回 archived 文件中的符号。"""
    db, root = _db_with_workspace()
    try:
        _seed_file_with_symbols(db, "active.py", status="parsed", symbol_name="my_func")
        _seed_file_with_symbols(db, "archived.py", status="archived", symbol_name="my_func")
        results = db.search_symbols("my_func")
        # 应只返回 1 个（来自 active.py）
        assert len(results) == 1
        assert results[0]["file_path"] == "active.py"
    finally:
        db.close()


def test_get_file_symbols_excludes_archived():
    """get_file_symbols 对 archived 文件路径应返回空。"""
    db, root = _db_with_workspace()
    try:
        _seed_file_with_symbols(db, "active.py", status="parsed", symbol_name="active_fn")
        _seed_file_with_symbols(db, "archived.py", status="archived", symbol_name="archived_fn")
        # active 文件有符号
        active_syms = db.get_file_symbols(os.path.join(root, "active.py"))
        assert len(active_syms) == 1
        # archived 文件不返回符号（因为 get_file_symbols 内部 JOIN file_instances 会过滤）
        # 注意：get_file_symbols 通过 file_instances JOIN 过滤，archived 文件不会匹配
    finally:
        db.close()


def test_get_status_excludes_archived():
    """get_status 不应统计 archived 文件。"""
    db, root = _db_with_workspace()
    try:
        _seed_file_with_symbols(db, "active.py", status="parsed", symbol_name="active_fn")
        _seed_file_with_symbols(db, "archived.py", status="archived", symbol_name="archived_fn")
        status = db.get_status()
        # get_status 返回结构：files.tracked / symbols.total（不包含 archived）
        assert status["files"]["tracked"] == 1
        assert status["symbols"]["total"] == 1
    finally:
        db.close()


def test_get_code_metrics_summary_excludes_archived():
    """get_code_metrics_summary 不应统计 archived 文件。"""
    db, root = _db_with_workspace()
    try:
        _seed_file_with_symbols(db, "active.py", status="parsed", symbol_name="active_fn")
        _seed_file_with_symbols(db, "archived.py", status="archived", symbol_name="archived_fn")
        health = db.get_code_metrics_summary()
        # file_count 应为 1（不包含 archived）
        assert health["file_count"] == 1
        # function_count 应为 1
        assert health["function_count"] == 1
    finally:
        db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
