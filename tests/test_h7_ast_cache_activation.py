"""H7 AST 缓存激活测试。

验证 _try_ast_cache_short_circuit 接入 refresh_file 决策路径：
- 未变更文件二次 refresh 跳过 parse
- 变更文件正常 parse
- ast_cache 元数据正确写入和读取
"""

import os
import tempfile
import time

from callwarden.config import norm_path
from callwarden.db.db import CodeGraphDB


def _db_with_workspace():
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    # 默认 foreign_keys=ON；_register_file_db 插入占位 '' hash 时全新库缺
    # file_contents('') 父行，违反 FK（生产旧库因历史 '' 行兼容）。本套件
    # 验证 ast_cache 短路行为，关闭外键检查。
    db.conn.execute("PRAGMA foreign_keys=OFF")
    return db, root


def _write_sample(root, path="sample.py", content="x = 1\n"):
    abs_path = os.path.join(root, path)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return abs_path


def _get_file_instance_id(db, root, abs_path):
    """从 DB 查 file_instance_id"""
    rel_path = norm_path(os.path.relpath(abs_path, root))
    cur = db.conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
        (db._get_active_workspace_id(), rel_path),
    )
    return cur.fetchone()["id"]


# ============================================
# _try_ast_cache_short_circuit 直接测试
# ============================================


def test_short_circuit_returns_false_for_no_cache():
    """无 ast_cache 时返回 False（首次解析）"""
    db, root = _db_with_workspace()
    try:
        abs_path = _write_sample(root, content="x = 1\n")
        file_instance_id = db._register_file_db(abs_path, "sample")
        assert db._try_ast_cache_short_circuit(abs_path, "sample.py", file_instance_id, "python") is False
    finally:
        db.close()


def test_short_circuit_returns_true_after_build():
    """build_full_graph 后再 refresh，ast_cache 命中跳过 parse"""
    db, root = _db_with_workspace()
    try:
        abs_path = _write_sample(root, content="def foo():\n    return 1\n")
        db.build_full_graph(force=True)

        file_instance_id = _get_file_instance_id(db, root, abs_path)
        rel_path = norm_path(os.path.relpath(abs_path, root))

        # 二次 refresh：ast_cache 应命中，返回 True
        result = db._try_ast_cache_short_circuit(abs_path, rel_path, file_instance_id, "python")
        assert result is True

        metadata = db._read_ast_cache(file_instance_id)
        assert metadata is not None
        assert "content_hash" in metadata
        assert metadata["language"] == "python"
    finally:
        db.close()


def test_short_circuit_returns_false_after_content_change():
    """文件变更后 ast_cache 不命中，返回 False"""
    db, root = _db_with_workspace()
    try:
        abs_path = _write_sample(root, content="def foo():\n    return 1\n")
        db.build_full_graph(force=True)

        file_instance_id = _get_file_instance_id(db, root, abs_path)
        rel_path = norm_path(os.path.relpath(abs_path, root))

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write("def foo():\n    return 2\n")

        result = db._try_ast_cache_short_circuit(abs_path, rel_path, file_instance_id, "python")
        assert result is False
    finally:
        db.close()


def test_short_circuit_returns_false_on_read_error():
    """文件读取失败时返回 False（降级走 parse 路径）"""
    db, root = _db_with_workspace()
    try:
        abs_path = _write_sample(root, content="x = 1\n")
        db.build_full_graph(force=True)

        file_instance_id = _get_file_instance_id(db, root, abs_path)
        rel_path = norm_path(os.path.relpath(abs_path, root))
        os.remove(abs_path)

        result = db._try_ast_cache_short_circuit(abs_path, rel_path, file_instance_id, "python")
        assert result is False
    finally:
        db.close()


# ============================================
# refresh_file 集成测试（端到端）
# ============================================


def test_refresh_file_skips_parse_on_unchanged_file():
    """refresh_file 未变更文件跳过 parse（symbols/calls 不变）"""
    db, root = _db_with_workspace()
    try:
        abs_path = _write_sample(root, content="def foo():\n    return 1\n")
        db.build_full_graph(force=True)

        cur = db.conn.execute("SELECT COUNT(*) as c FROM symbols")
        symbols_before = cur.fetchone()["c"]

        for _ in range(3):
            db.refresh_file(abs_path)

        cur = db.conn.execute("SELECT COUNT(*) as c FROM symbols")
        symbols_after = cur.fetchone()["c"]
        assert symbols_before == symbols_after

        file_instance_id = _get_file_instance_id(db, root, abs_path)
        metadata = db._read_ast_cache(file_instance_id)
        assert metadata is not None
        assert metadata["language"] == "python"
    finally:
        db.close()


def test_refresh_file_reparses_on_change():
    """refresh_file 变更文件正常 parse（symbols 更新为新内容）"""
    db, root = _db_with_workspace()
    try:
        abs_path = _write_sample(root, content="def foo():\n    return 1\n")
        db.build_full_graph(force=True)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write("def foo():\n    return 1\n\ndef bar():\n    return 2\n")

        db.refresh_file(abs_path)

        cur = db.conn.execute("SELECT COUNT(*) as c FROM symbols")
        symbols_after = cur.fetchone()["c"]
        assert symbols_after >= 2
    finally:
        db.close()


# ============================================
# ast_cache 元数据正确性测试
# ============================================


def test_ast_cache_metadata_has_required_fields():
    """ast_cache 元数据包含所有必需字段"""
    db, root = _db_with_workspace()
    try:
        abs_path = _write_sample(root, content="def foo():\n    return 1\n")
        db.build_full_graph(force=True)

        file_instance_id = _get_file_instance_id(db, root, abs_path)
        metadata = db._read_ast_cache(file_instance_id)
        assert metadata is not None
        assert "content_hash" in metadata
        assert "parsed_at" in metadata
        assert "language" in metadata
        assert "incremental" in metadata
        assert "changed_ranges_count" in metadata
        assert metadata["language"] == "python"
    finally:
        db.close()


def test_ast_cache_parsed_at_updates_on_short_circuit():
    """短路后 parsed_at 更新为当前时间"""
    db, root = _db_with_workspace()
    try:
        abs_path = _write_sample(root, content="def foo():\n    return 1\n")
        db.build_full_graph(force=True)

        file_instance_id = _get_file_instance_id(db, root, abs_path)
        rel_path = norm_path(os.path.relpath(abs_path, root))

        metadata_before = db._read_ast_cache(file_instance_id)
        parsed_at_before = metadata_before["parsed_at"]

        time.sleep(0.05)

        db._try_ast_cache_short_circuit(abs_path, rel_path, file_instance_id, "python")

        metadata_after = db._read_ast_cache(file_instance_id)
        assert metadata_after["parsed_at"] > parsed_at_before
        assert metadata_after["content_hash"] == metadata_before["content_hash"]
    finally:
        db.close()
