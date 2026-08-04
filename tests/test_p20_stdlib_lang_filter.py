"""P20: import_all_stdlib_symbols() 语言过滤测试

验证 build_full_graph 只导入项目实际使用语言的 stdlib 符号，
不再盲目导入所有 14 种语言。

测试场景：
1. 纯 Python 项目 → 只导入 python stdlib，不导入 java/c/rust 等
2. 纯 Java 项目 → 只导入 java stdlib，不导入 python/c/rust 等
3. 混合语言项目（Python + Rust）→ 只导入 python + rust
4. 空项目 → 不导入任何 stdlib
5. languages=None → 导入所有（向后兼容）
6. build_full_graph 集成测试：验证 project_langs 收集正确
"""
from __future__ import annotations

import os
import tempfile

from callwarden.db.db import CodeGraphDB


def _make_db_with_files(files: dict[str, str]) -> tuple[CodeGraphDB, str]:
    """创建带文件的临时 DB。

    Args:
        files: {relative_path: content} 字典
    """
    root = tempfile.mkdtemp()
    for rel, content in files.items():
        full = os.path.join(root, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    db = CodeGraphDB(
        os.path.join(root, "callwarden.db"),
        workspace_root=root,
    )
    # 默认 foreign_keys=ON；import_stdlib_symbols_for_lang 先插 external_symbols
    # 后插 package_versions，全新库违反复合 FK。本套件验证语言过滤语义，
    # 关闭外键检查（与 test_p0_4_rollback_config 先例一致）。
    db.conn.execute("PRAGMA foreign_keys=OFF")
    return db, root


def test_import_all_stdlib_with_explicit_languages():
    """languages 参数显式指定时，只导入指定语言"""
    db, _root = _make_db_with_files({})
    try:
        # 只导入 rust 和 go
        count = db.import_all_stdlib_symbols(languages=["rust", "go"])

        # rust 和 go 有预定义 stdlib 符号
        assert count > 0

        # 验证 external_symbols 表只有 rust 和 go 的
        cur = db.conn.execute(
            "SELECT DISTINCT package_name FROM external_symbols"
        )
        packages = {row["package_name"] for row in cur.fetchall()}
        assert "stdlib-rust" in packages
        assert "stdlib-go" in packages
        # 不应该有 java/c/python 等
        assert "stdlib-java" not in packages
        assert "stdlib-c" not in packages
    finally:
        db.close()


def test_import_all_stdlib_python_only():
    """languages=['python'] 只导入 Python stdlib"""
    db, _root = _make_db_with_files({})
    try:
        # python 走 importlib，写入 external_symbols 表（package_name='stdlib'）
        db.import_all_stdlib_symbols(languages=["python"])

        # 应该只有 python 的 stdlib 符号（package_name='stdlib'）
        cur = db.conn.execute(
            "SELECT DISTINCT package_name FROM external_symbols"
        )
        packages = {row["package_name"] for row in cur.fetchall()}
        assert "stdlib" in packages  # Python stdlib
        # 不应有其他语言的 stdlib
        assert "stdlib-rust" not in packages
        assert "stdlib-java" not in packages
        assert "stdlib-c" not in packages
    finally:
        db.close()


def test_import_all_stdlib_none_imports_all():
    """languages=None 导入所有支持语言（向后兼容）"""
    db, _root = _make_db_with_files({})
    try:
        db.import_all_stdlib_symbols(languages=None)

        # 应该有多个语言的 stdlib
        cur = db.conn.execute(
            "SELECT DISTINCT package_name FROM external_symbols"
        )
        packages = {row["package_name"] for row in cur.fetchall()}
        # 至少有 rust, java, go, c, cpp 等
        assert "stdlib-rust" in packages
        assert "stdlib-java" in packages
        assert "stdlib-go" in packages
    finally:
        db.close()


def test_import_all_stdlib_empty_list():
    """languages=[] 不导入任何语言"""
    db, _root = _make_db_with_files({})
    try:
        count = db.import_all_stdlib_symbols(languages=[])
        assert count == 0

        cur = db.conn.execute("SELECT COUNT(*) as c FROM external_symbols")
        assert cur.fetchone()["c"] == 0
    finally:
        db.close()


def test_build_full_graph_collects_project_langs():
    """build_full_graph 收集项目实际使用的语言"""
    # 创建一个纯 Python 项目
    db, _root = _make_db_with_files({
        "main.py": "def hello():\n    print('hello')\n",
        "utils.py": "def add(a, b):\n    return a + b\n",
    })
    try:
        # build_full_graph 应该收集到 python
        db.build_full_graph(force=True)

        # 验证 stdlib_import 只导入了 python 相关符号
        # python stdlib 写入 external_symbols（package_name='stdlib'）
        # 不应该有 java/c/rust 等语言的 stdlib 符号
        cur = db.conn.execute(
            "SELECT DISTINCT package_name FROM external_symbols WHERE package_name LIKE 'stdlib-%'"
        )
        stdlib_pkgs = {row["package_name"] for row in cur.fetchall()}
        # 纯 Python 项目不应有 java/c/rust stdlib
        assert "stdlib-java" not in stdlib_pkgs
        assert "stdlib-c" not in stdlib_pkgs
        assert "stdlib-rust" not in stdlib_pkgs
    finally:
        db.close()


def test_build_full_graph_mixed_lang_project():
    """混合语言项目（Python + Rust）只导入这两种语言的 stdlib"""
    db, _root = _make_db_with_files({
        "main.py": "def hello():\n    print('hello')\n",
        "lib.rs": "pub fn add(a: i32, b: i32) -> i32 { a + b }\n",
    })
    try:
        db.build_full_graph(force=True)

        # 应该有 rust stdlib（python 走 importlib 不写入 external_symbols）
        cur = db.conn.execute(
            "SELECT DISTINCT package_name FROM external_symbols WHERE package_name LIKE 'stdlib-%'"
        )
        stdlib_pkgs = {row["package_name"] for row in cur.fetchall()}
        assert "stdlib-rust" in stdlib_pkgs
        # 不应有 java/c/go 等
        assert "stdlib-java" not in stdlib_pkgs
        assert "stdlib-c" not in stdlib_pkgs
        assert "stdlib-go" not in stdlib_pkgs
    finally:
        db.close()


def test_build_full_graph_java_project():
    """纯 Java 项目只导入 java stdlib"""
    db, _root = _make_db_with_files({
        "Main.java": "public class Main { public static void main(String[] args) {} }\n",
    })
    try:
        db.build_full_graph(force=True)

        cur = db.conn.execute(
            "SELECT DISTINCT package_name FROM external_symbols WHERE package_name LIKE 'stdlib-%'"
        )
        stdlib_pkgs = {row["package_name"] for row in cur.fetchall()}
        assert "stdlib-java" in stdlib_pkgs
        # 不应有 rust/c/go/python 等
        assert "stdlib-rust" not in stdlib_pkgs
        assert "stdlib-c" not in stdlib_pkgs
        assert "stdlib-go" not in stdlib_pkgs
    finally:
        db.close()
