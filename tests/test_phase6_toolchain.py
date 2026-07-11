"""
Phase 6.0: Toolchain CAS 测试

测试 toolchain 注册、查询、绑定、探测、fingerprint 计算。
"""

import os
import sys
import sqlite3
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# 直接导入模块，避免 db/__init__.py 的相对导入链
import importlib.util


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_toolchain_path = Path(__file__).parent.parent / "db" / "db_toolchain.py"
_tc_mod = _load_module("db_toolchain", str(_toolchain_path))

init_toolchain_schema = _tc_mod.init_toolchain_schema
register_toolchain = _tc_mod.register_toolchain
get_toolchain = _tc_mod.get_toolchain
list_toolchains = _tc_mod.list_toolchains
delete_toolchain = _tc_mod.delete_toolchain
bind_toolchain_to_workspace = _tc_mod.bind_toolchain_to_workspace
get_workspace_toolchains = _tc_mod.get_workspace_toolchains
compute_toolchain_fingerprint = _tc_mod.compute_toolchain_fingerprint
probe_compiler = _tc_mod.probe_compiler
Toolchain = _tc_mod.Toolchain


# ============================================
# TestSchema —— Schema 初始化
# ============================================

class TestSchema:
    """Schema 初始化测试"""

    def test_init_schema(self, tmp_path):
        """初始化 schema"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        # 验证表存在
        tables = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t[0] for t in tables]
        assert "toolchains" in table_names
        assert "workspace_toolchains" in table_names

        conn.close()

    def test_init_schema_idempotent(self, tmp_path):
        """重复初始化不报错"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)
        init_toolchain_schema(conn)  # 第二次不报错
        conn.close()


# ============================================
# TestRegisterToolchain —— 工具链注册
# ============================================

class TestRegisterToolchain:
    """工具链注册测试"""

    def test_register_no_probe(self, tmp_path):
        """注册工具链（不探测）"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        # 创建假的编译器文件
        fake_compiler = tmp_path / "gcc"
        fake_compiler.write_text("#!/bin/sh\necho gcc")

        tc = register_toolchain(
            conn=conn,
            name="test_gcc",
            compiler_path=str(fake_compiler),
            probe=False,
        )

        assert tc.id > 0
        assert tc.name == "test_gcc"
        assert tc.compiler_path == str(fake_compiler)
        assert tc.fingerprint != ""

        conn.close()

    def test_register_duplicate_fingerprint(self, tmp_path):
        """相同 fingerprint 不重复注册"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        fake_compiler = tmp_path / "gcc"
        fake_compiler.write_text("gcc")

        tc1 = register_toolchain(
            conn=conn, name="gcc1",
            compiler_path=str(fake_compiler),
            probe=False,
        )
        tc2 = register_toolchain(
            conn=conn, name="gcc2",  # 不同名称
            compiler_path=str(fake_compiler),
            probe=False,
        )

        # 相同 fingerprint → 返回已有的
        assert tc1.fingerprint == tc2.fingerprint
        assert tc1.id == tc2.id

        conn.close()

    def test_register_with_sysroot(self, tmp_path):
        """带 sysroot 注册"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        sysroot = tmp_path / "sysroot"
        sysroot.mkdir()

        tc = register_toolchain(
            conn=conn, name="arm_gcc",
            compiler_path=str(compiler),
            sysroot=str(sysroot),
            description="ARM toolchain",
            probe=False,
        )

        assert tc.sysroot == str(sysroot)
        assert tc.description == "ARM toolchain"

        conn.close()


# ============================================
# TestGetToolchain —— 工具链查询
# ============================================

class TestGetToolchain:
    """工具链查询测试"""

    def test_get_by_name(self, tmp_path):
        """按名称查询"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")

        register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        tc = get_toolchain(conn, "test_gcc")
        assert tc is not None
        assert tc.name == "test_gcc"

        conn.close()

    def test_get_by_id(self, tmp_path):
        """按 ID 查询"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")

        registered = register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        tc = get_toolchain(conn, registered.id)
        assert tc is not None
        assert tc.id == registered.id

        conn.close()

    def test_get_nonexistent(self, tmp_path):
        """查询不存在的工具链"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        assert get_toolchain(conn, "nonexistent") is None
        assert get_toolchain(conn, 999) is None

        conn.close()

    def test_list_toolchains(self, tmp_path):
        """列出所有工具链"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        # 使用不同的 sysroot 让 fingerprint 不同
        for name in ["gcc1", "gcc2", "gcc3"]:
            compiler = tmp_path / name
            compiler.write_text("gcc")
            register_toolchain(conn, name, str(compiler), sysroot=name, probe=False)

        tcs = list_toolchains(conn)
        assert len(tcs) >= 3

        conn.close()


# ============================================
# TestDeleteToolchain —— 工具链删除
# ============================================

class TestDeleteToolchain:
    """工具链删除测试"""

    def test_delete_by_name(self, tmp_path):
        """按名称删除"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")

        register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        assert delete_toolchain(conn, "test_gcc")
        assert get_toolchain(conn, "test_gcc") is None

        conn.close()

    def test_delete_nonexistent(self, tmp_path):
        """删除不存在的工具链"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        assert not delete_toolchain(conn, "nonexistent")

        conn.close()


# ============================================
# TestBindToolchain —— 工具链绑定
# ============================================

class TestBindToolchain:
    """工具链绑定测试"""

    def test_bind_to_workspace(self, tmp_path):
        """绑定工具链到 workspace"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        tc = register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        # 创建一个 workspace（简化：直接插入）
        _schema_path = Path(__file__).parent.parent / "db" / "schema.py"
        _schema_mod = _load_module("db_schema", str(_schema_path))
        SCHEMA_SQL = _schema_mod.SCHEMA_SQL
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
                     ("test_ws", str(tmp_path), 0.0))
        conn.commit()
        ws_id = conn.execute("SELECT id FROM workspaces WHERE name='test_ws'").fetchone()[0]

        assert bind_toolchain_to_workspace(conn, ws_id, tc.id)

        bound = get_workspace_toolchains(conn, ws_id)
        assert len(bound) == 1
        assert bound[0].id == tc.id

        conn.close()

    def test_bind_with_build_context(self, tmp_path):
        """带 build_context_hash 绑定"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        tc = register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        _schema_path = Path(__file__).parent.parent / "db" / "schema.py"
        _schema_mod = _load_module("db_schema", str(_schema_path))
        SCHEMA_SQL = _schema_mod.SCHEMA_SQL
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
                     ("test_ws", str(tmp_path), 0.0))
        conn.commit()
        ws_id = conn.execute("SELECT id FROM workspaces WHERE name='test_ws'").fetchone()[0]

        # 同一 toolchain 绑定不同 build context
        bind_toolchain_to_workspace(conn, ws_id, tc.id, "build_debug")
        bind_toolchain_to_workspace(conn, ws_id, tc.id, "build_release")

        debug_tcs = get_workspace_toolchains(conn, ws_id, "build_debug")
        assert len(debug_tcs) == 1

        release_tcs = get_workspace_toolchains(conn, ws_id, "build_release")
        assert len(release_tcs) == 1

        all_tcs = get_workspace_toolchains(conn, ws_id)
        assert len(all_tcs) == 2  # 两个 build context

        conn.close()

    def test_bind_duplicate(self, tmp_path):
        """重复绑定（幂等）"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)

        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        tc = register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        _schema_path = Path(__file__).parent.parent / "db" / "schema.py"
        _schema_mod = _load_module("db_schema", str(_schema_path))
        SCHEMA_SQL = _schema_mod.SCHEMA_SQL
        conn.executescript(SCHEMA_SQL)
        conn.execute("INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
                     ("test_ws", str(tmp_path), 0.0))
        conn.commit()
        ws_id = conn.execute("SELECT id FROM workspaces WHERE name='test_ws'").fetchone()[0]

        assert bind_toolchain_to_workspace(conn, ws_id, tc.id)
        # 重复绑定不报错（INSERT OR IGNORE）
        assert bind_toolchain_to_workspace(conn, ws_id, tc.id)

        bound = get_workspace_toolchains(conn, ws_id)
        assert len(bound) == 1

        conn.close()


# ============================================
# TestFingerprint —— 指纹计算
# ============================================

class TestFingerprint:
    """指纹计算测试"""

    def test_same_fields_same_fingerprint(self):
        """相同字段 → 相同指纹"""
        fp1 = compute_toolchain_fingerprint(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/include"], {"__GNUC__": "10"},
        )
        fp2 = compute_toolchain_fingerprint(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/include"], {"__GNUC__": "10"},
        )
        assert fp1 == fp2

    def test_different_path_different_fingerprint(self):
        """不同路径 → 不同指纹"""
        fp1 = compute_toolchain_fingerprint(
            "/usr/bin/gcc", "gcc", "10.0", "x86_64-linux", "",
            [], {},
        )
        fp2 = compute_toolchain_fingerprint(
            "/opt/gcc/bin/gcc", "gcc", "10.0", "x86_64-linux", "",
            [], {},
        )
        assert fp1 != fp2

    def test_different_version_different_fingerprint(self):
        """不同版本 → 不同指纹"""
        fp1 = compute_toolchain_fingerprint(
            "gcc", "gcc", "10.0", "x86_64-linux", "", [], {},
        )
        fp2 = compute_toolchain_fingerprint(
            "gcc", "gcc", "11.0", "x86_64-linux", "", [], {},
        )
        assert fp1 != fp2

    def test_include_dirs_order_independent(self):
        """include_dirs 顺序无关"""
        fp1 = compute_toolchain_fingerprint(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/include", "/usr/local/include"], {},
        )
        fp2 = compute_toolchain_fingerprint(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/local/include", "/usr/include"], {},
        )
        assert fp1 == fp2  # 排序后相同

    def test_predefined_macros_order_independent(self):
        """predefined_macros 顺序无关"""
        fp1 = compute_toolchain_fingerprint(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            [], {"A": "1", "B": "2"},
        )
        fp2 = compute_toolchain_fingerprint(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            [], {"B": "2", "A": "1"},
        )
        assert fp1 == fp2  # 排序后相同


# ============================================
# TestProbeCompiler —— 编译器探测
# ============================================

class TestProbeCompiler:
    """编译器探测测试"""

    def test_detect_compiler_type(self, tmp_path):
        """从路径推断编译器类型"""
        _detect_compiler_type = _tc_mod._detect_compiler_type
        assert _detect_compiler_type("/usr/bin/gcc") == "gcc"
        assert _detect_compiler_type("/usr/bin/g++") == "g++"
        assert _detect_compiler_type("/usr/bin/clang") == "clang"
        assert _detect_compiler_type("/usr/bin/arm-none-eabi-gcc") == "arm-none-eabi-gcc"

    def test_probe_nonexistent_compiler(self):
        """探测不存在的编译器"""
        info = probe_compiler("/nonexistent/path/gcc")
        assert info["version"] == ""
        assert info["target_triple"] == ""
        assert info["include_dirs"] == []
        assert info["predefined_macros"] == {}

    def test_probe_python_as_compiler(self, tmp_path):
        """用 python 模拟编译器（有 --version 输出）"""
        import sys
        info = probe_compiler(sys.executable)
        # python 有 --version 输出
        assert info["version"] != ""


# ============================================
# TestToolchainDataclass —— Toolchain 数据结构
# ============================================

class TestToolchainDataclass:
    """Toolchain 数据结构测试"""

    def test_toolchain_summary(self):
        """Toolchain summary"""
        tc = Toolchain(
            id=1,
            name="test_gcc",
            compiler_path="/usr/bin/gcc",
            compiler_type="gcc",
            version="10.0",
            target_triple="x86_64-linux",
        )
        summary = tc.summary()
        assert "Toolchain" in summary
        assert "test_gcc" in summary
        assert "gcc" in summary

    def test_toolchain_to_dict(self):
        """Toolchain to_dict"""
        tc = Toolchain(
            id=1,
            name="test_gcc",
            compiler_path="/usr/bin/gcc",
            compiler_type="gcc",
            include_dirs=["/usr/include"],
            predefined_macros={"A": "1"},
        )
        d = tc.to_dict()
        assert d["name"] == "test_gcc"
        assert d["include_dirs"] == ["/usr/include"]
        assert d["predefined_macros"] == {"A": "1"}


# ============================================
# TestEndToEnd —— 端到端测试
# ============================================

class TestEndToEnd:
    """端到端测试"""

    def test_full_lifecycle(self, tmp_path):
        """完整生命周期：注册 → 查询 → 绑定 → 删除"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        init_toolchain_schema(conn)
        _schema_path = Path(__file__).parent.parent / "db" / "schema.py"
        _schema_mod = _load_module("db_schema", str(_schema_path))
        SCHEMA_SQL = _schema_mod.SCHEMA_SQL
        conn.executescript(SCHEMA_SQL)

        # 1. 注册
        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        tc = register_toolchain(conn, "test_gcc", str(compiler), probe=False)
        assert tc.id > 0

        # 2. 查询
        tc2 = get_toolchain(conn, "test_gcc")
        assert tc2.fingerprint == tc.fingerprint

        # 3. 创建 workspace 并绑定
        conn.execute("INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
                     ("test_ws", str(tmp_path), 0.0))
        conn.commit()
        ws_id = conn.execute("SELECT id FROM workspaces WHERE name='test_ws'").fetchone()[0]

        bind_toolchain_to_workspace(conn, ws_id, tc.id, "build_debug")

        # 4. 查询绑定
        bound = get_workspace_toolchains(conn, ws_id, "build_debug")
        assert len(bound) == 1

        # 5. 删除
        assert delete_toolchain(conn, "test_gcc")
        assert get_toolchain(conn, "test_gcc") is None

        conn.close()
