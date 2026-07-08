"""FTS5 全文索引优化（P2）测试

验证 search_symbols 的 FTS5 路径：
- snake_case/camelCase/::./ 自动分词
- 新增/删除符号时 FTS5 索引同步
- LIKE 回退路径可用
- 迁移幂等性（v31 重复执行不报错）
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


def _seed_symbol(db, rel_path, name, qualified_name=None, kind="fn", symbol_hash=None):
    """辅助：插入一个符号（触发 FTS5 同步触发器）。"""
    ws_id = db._get_active_workspace_id()
    qname = qualified_name or name
    sh = symbol_hash or f"hash_{qname}"
    # file_contents
    db.conn.execute(
        "INSERT INTO file_contents (content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, 'python', 10, 0)",
        (f"fc_{rel_path}",),
    )
    # file_instances
    db.conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, status, module_path) VALUES (?, ?, ?, ?, 0, 'parsed', '')",
        (ws_id, rel_path, os.path.join(db.workspace_root, rel_path), f"fc_{rel_path}"),
    )
    fi_id = db.conn.execute("SELECT id FROM file_instances WHERE rel_path=?", (rel_path,)).fetchone()[0]
    # symbol_contents
    db.conn.execute(
        "INSERT INTO symbol_contents (content_hash, name, kind, content, signature, "
        "has_comment, comment_content, qualified_name) VALUES (?, ?, ?, 'def x(): pass', '', 0, '', ?)",
        (sh, name, kind, qname),
    )
    # symbols（触发 FTS5 AFTER INSERT 触发器）
    db.conn.execute(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "qualified_name, comment_status) VALUES (?, ?, ?, ?, 1, 5, ?, 'pending') "
        "ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET symbol_hash = excluded.symbol_hash",
        (fi_id, sh, name, kind, qname),
    )
    # file_versions
    db.conn.execute(
        "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, parsed_at, "
        "is_current, is_deleted) VALUES (?, 1, ?, 0, 0, 1, 0)",
        (fi_id, f"fc_{rel_path}"),
    )
    fv_id = db.conn.execute("SELECT id FROM file_versions WHERE file_instance_id=?", (fi_id,)).fetchone()[0]
    db.conn.execute(
        "INSERT INTO file_symbol_versions (file_version_id, symbol_hash, qualified_name, "
        "start_line, end_line, depth, is_deleted) VALUES (?, ?, ?, 1, 5, 0, 0)",
        (fv_id, sh, qname),
    )
    db.conn.commit()
    return fi_id


def test_fts5_snake_case_tokenization():
    """snake_case 符号名应被 FTS5 自动分词，搜子串可命中。"""
    db, root = _db_with_workspace()
    try:
        _seed_symbol(db, "a.py", "user_login_handler", "module::user_login_handler")
        # 搜 "login" 应命中（FTS5 把 user_login_handler 分成 user/login/handler）
        results = db.search_symbols("login")
        assert len(results) == 1
        assert results[0]["name"] == "user_login_handler"
    finally:
        db.close()


def test_fts5_camel_case_tokenization():
    """camelCase 符号名应被 FTS5 自动分词。"""
    db, root = _db_with_workspace()
    try:
        _seed_symbol(db, "a.py", "processOrderItem", "module::processOrderItem")
        # 搜 "order" 应命中（FTS5 把 processOrderItem 分成 process/Order/Item）
        results = db.search_symbols("order")
        assert len(results) == 1
        assert results[0]["name"] == "processOrderItem"
    finally:
        db.close()


def test_fts5_qualified_name_search():
    """搜索应匹配 qualified_name 中的 token。"""
    db, root = _db_with_workspace()
    try:
        _seed_symbol(db, "a.py", "handler", "app::controllers::UserController")
        # 搜 "controller" 应命中（qualified_name 中的 token）
        results = db.search_symbols("controller")
        assert len(results) == 1
        assert "UserController" in results[0]["qualified_name"]
    finally:
        db.close()


def test_fts5_prefix_match():
    """FTS5 前缀匹配：搜 "log" 应命中 loginHandler / logout。"""
    db, root = _db_with_workspace()
    try:
        _seed_symbol(db, "a.py", "loginHandler", "module::loginHandler")
        _seed_symbol(db, "b.py", "logout", "module::logout")
        # "log*" 前缀匹配两个
        results = db.search_symbols("log")
        names = {r["name"] for r in results}
        assert "loginHandler" in names
        assert "logout" in names
    finally:
        db.close()


def test_fts5_insert_sync():
    """新增符号后 FTS5 索引应自动同步（触发器）。"""
    db, root = _db_with_workspace()
    try:
        _seed_symbol(db, "a.py", "initial_func", "module::initial_func")
        assert len(db.search_symbols("initial")) == 1
        # 新增第二个符号
        _seed_symbol(db, "b.py", "added_func", "module::added_func")
        assert len(db.search_symbols("added")) == 1
        assert len(db.search_symbols("initial")) == 1
    finally:
        db.close()


def test_fts5_delete_sync():
    """删除符号后 FTS5 索引应移除（触发器）。"""
    db, root = _db_with_workspace()
    try:
        _seed_symbol(db, "a.py", "to_delete", "module::to_delete")
        assert len(db.search_symbols("delete")) == 1
        # 删除 symbols 行（触发 AFTER DELETE 触发器）
        db.conn.execute("DELETE FROM symbols WHERE name = ?", ("to_delete",))
        db.conn.commit()
        # FTS5 索引应同步删除
        assert len(db.search_symbols("delete")) == 0
    finally:
        db.close()


def test_fts5_update_sync():
    """更新符号名后 FTS5 索引应更新（触发器）。"""
    db, root = _db_with_workspace()
    try:
        _seed_symbol(db, "a.py", "old_name", "module::old_name")
        assert len(db.search_symbols("old")) == 1
        assert len(db.search_symbols("new")) == 0
        # 更新符号名（触发 AFTER UPDATE 触发器）
        db.conn.execute(
            "UPDATE symbols SET name = ?, qualified_name = ? WHERE name = ?",
            ("new_name", "module::new_name", "old_name"),
        )
        db.conn.commit()
        # 旧名不再命中
        assert len(db.search_symbols("old")) == 0
        # 新名命中
        assert len(db.search_symbols("new")) == 1
    finally:
        db.close()


def test_search_kind_filter():
    """FTS5 路径下 kind 过滤应正确工作。"""
    db, root = _db_with_workspace()
    try:
        _seed_symbol(db, "a.py", "my_func", "module::my_func", kind="fn")
        _seed_symbol(db, "b.py", "my_method", "module::my_method", kind="method")
        results = db.search_symbols("my", kind="fn")
        assert len(results) == 1
        assert results[0]["kind"] == "fn"
    finally:
        db.close()


def test_fts5_special_chars_fallback():
    """query 含 FTS5 特殊字符时应回退 LIKE 路径。"""
    db, root = _db_with_workspace()
    try:
        # qualified_name 含 ::，FTS5 MATCH 可能无法直接匹配
        _seed_symbol(db, "a.py", "foo", "app::module::foo")
        # 搜 "app" 应能通过 FTS5 命中（app 是一个 token）
        results = db.search_symbols("app")
        assert len(results) == 1
        # 搜含 :: 的串可能触发回退，但应仍返回结果
        results = db.search_symbols("app::module")
        # FTS5 或 LIKE 至少一种应命中
        assert len(results) >= 1
    finally:
        db.close()


def test_fts5_empty_query():
    """空 query 不应崩溃（FTS5 会抛异常 → 回退 LIKE）。"""
    db, root = _db_with_workspace()
    try:
        _seed_symbol(db, "a.py", "foo", "module::foo")
        # 空 query 走 LIKE 路径，返回所有（LIKE '%%'）
        results = db.search_symbols("")
        assert len(results) >= 1
    finally:
        db.close()
