"""
Phase 6.2: workspace 绑定 build context 测试

测试 build context 的注册、查询、active 切换、toolchain 解析、降级策略。
"""

import sqlite3
import sys
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


_tc_mod = _load_module("db_toolchain", str(Path(__file__).parent.parent / "db" / "db_toolchain.py"))
_schema_mod = _load_module("db_schema", str(Path(__file__).parent.parent / "db" / "schema.py"))

init_toolchain_schema = _tc_mod.init_toolchain_schema
register_toolchain = _tc_mod.register_toolchain
bind_toolchain_to_workspace = _tc_mod.bind_toolchain_to_workspace
get_workspace_toolchains = _tc_mod.get_workspace_toolchains

compute_build_context_hash = _tc_mod.compute_build_context_hash
register_build_context = _tc_mod.register_build_context
get_build_context = _tc_mod.get_build_context
list_build_contexts = _tc_mod.list_build_contexts
set_active_build_context = _tc_mod.set_active_build_context
get_active_build_context = _tc_mod.get_active_build_context
delete_build_context = _tc_mod.delete_build_context
resolve_toolchain = _tc_mod.resolve_toolchain

BuildContext = _tc_mod.BuildContext
SCHEMA_SQL = _schema_mod.SCHEMA_SQL


@pytest.fixture
def db_conn(tmp_path):
    """创建带 toolchain + workspace schema 的 DB

    返回 (conn, ws_id) 元组。
    """
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    init_toolchain_schema(conn)
    conn.executescript(SCHEMA_SQL)
    # 创建 workspace
    conn.execute(
        "INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
        ("test_ws", str(tmp_path), 0.0),
    )
    conn.commit()
    ws_id = conn.execute("SELECT id FROM workspaces WHERE name='test_ws'").fetchone()[0]
    yield conn, ws_id
    conn.close()


# ============================================
# TestBuildContextHash —— hash 计算
# ============================================

class TestBuildContextHash:
    """build context hash 计算测试"""

    def test_same_fields_same_hash(self):
        """相同字段 → 相同 hash"""
        h1 = compute_build_context_hash(["-O2", "-g"], {"DEBUG": "1"}, ["/usr/include"])
        h2 = compute_build_context_hash(["-O2", "-g"], {"DEBUG": "1"}, ["/usr/include"])
        assert h1 == h2

    def test_different_flags_different_hash(self):
        """不同 flags → 不同 hash"""
        h1 = compute_build_context_hash(["-O2"], {}, [])
        h2 = compute_build_context_hash(["-O0"], {}, [])
        assert h1 != h2

    def test_different_defines_different_hash(self):
        """不同 defines → 不同 hash"""
        h1 = compute_build_context_hash([], {"DEBUG": "1"}, [])
        h2 = compute_build_context_hash([], {"RELEASE": "1"}, [])
        assert h1 != h2

    def test_different_includes_different_hash(self):
        """不同 include_paths → 不同 hash"""
        h1 = compute_build_context_hash([], {}, ["/usr/include"])
        h2 = compute_build_context_hash([], {}, ["/opt/include"])
        assert h1 != h2

    def test_flags_order_independent(self):
        """flags 顺序无关"""
        h1 = compute_build_context_hash(["-O2", "-g", "-Wall"], {}, [])
        h2 = compute_build_context_hash(["-Wall", "-g", "-O2"], {}, [])
        assert h1 == h2

    def test_defines_order_independent(self):
        """defines 顺序无关"""
        h1 = compute_build_context_hash([], {"A": "1", "B": "2"}, [])
        h2 = compute_build_context_hash([], {"B": "2", "A": "1"}, [])
        assert h1 == h2

    def test_includes_order_independent(self):
        """include_paths 顺序无关"""
        h1 = compute_build_context_hash([], {}, ["/a", "/b"])
        h2 = compute_build_context_hash([], {}, ["/b", "/a"])
        assert h1 == h2

    def test_path_separator_normalized(self):
        """路径分隔符规范化"""
        h1 = compute_build_context_hash([], {}, ["C:\\msys\\include"])
        h2 = compute_build_context_hash([], {}, ["C:/msys/include"])
        assert h1 == h2

    def test_empty_fields(self):
        """空字段"""
        h = compute_build_context_hash([], {}, [])
        assert len(h) == 64  # SHA-256 hex

    def test_hash_is_hex(self):
        """hash 是 64 字符 hex"""
        h = compute_build_context_hash(["-O2"], {"DEBUG": "1"}, ["/usr/include"])
        assert len(h) == 64
        int(h, 16)


# ============================================
# TestRegisterBuildContext —— 注册
# ============================================

class TestRegisterBuildContext:
    """build context 注册测试"""

    def test_register_basic(self, db_conn):
        """基本注册"""
        conn, ws_id = db_conn
        bc = register_build_context(
            conn, ws_id, "debug",
            compile_flags=["-O0", "-g"],
            defines={"DEBUG": "1"},
            include_paths=["/usr/include"],
        )
        assert bc.workspace_id == ws_id
        assert bc.name == "debug"
        assert bc.build_context_hash != ""
        assert bc.compile_flags == ["-O0", "-g"]
        assert bc.defines == {"DEBUG": "1"}
        assert bc.include_paths == ["/usr/include"]
        assert not bc.is_active

    def test_register_with_set_active(self, db_conn):
        """注册时设为 active"""
        conn, ws_id = db_conn
        bc = register_build_context(
            conn, ws_id, "debug",
            set_active=True,
        )
        assert bc.is_active

        # DB 中也是 active
        active = get_active_build_context(conn, ws_id)
        assert active is not None
        assert active.build_context_hash == bc.build_context_hash

    def test_register_duplicate_same_hash(self, db_conn):
        """相同字段 → 相同 hash（INSERT OR REPLACE 更新名称）"""
        conn, ws_id = db_conn
        bc1 = register_build_context(
            conn, ws_id, "debug1",
            compile_flags=["-O0"],
        )
        bc2 = register_build_context(
            conn, ws_id, "debug2",  # 不同名称
            compile_flags=["-O0"],
        )
        assert bc1.build_context_hash == bc2.build_context_hash

    def test_register_multiple(self, db_conn):
        """注册多个 build context"""
        conn, ws_id = db_conn
        for name, flags in [("debug", ["-O0"]), ("release", ["-O2"]), ("relwithdebinfo", ["-O2", "-g"])]:
            register_build_context(conn, ws_id, name, compile_flags=flags)

        bcs = list_build_contexts(conn, ws_id)
        assert len(bcs) == 3


# ============================================
# TestGetBuildContext —— 查询
# ============================================

class TestGetBuildContext:
    """build context 查询测试"""

    def test_get_by_hash(self, db_conn):
        """按 hash 查询"""
        conn, ws_id = db_conn
        bc = register_build_context(
            conn, ws_id, "debug",
            compile_flags=["-O0"],
        )
        fetched = get_build_context(conn, ws_id, bc.build_context_hash)
        assert fetched is not None
        assert fetched.name == "debug"
        assert fetched.compile_flags == ["-O0"]

    def test_get_nonexistent(self, db_conn):
        """查询不存在的"""
        conn, ws_id = db_conn
        assert get_build_context(conn, ws_id, "nonexistent") is None

    def test_list_contexts(self, db_conn):
        """列出所有"""
        conn, ws_id = db_conn
        register_build_context(conn, ws_id, "debug", compile_flags=["-O0"])
        register_build_context(conn, ws_id, "release", compile_flags=["-O2"])

        bcs = list_build_contexts(conn, ws_id)
        assert len(bcs) == 2
        names = {bc.name for bc in bcs}
        assert names == {"debug", "release"}

    def test_list_empty(self, db_conn):
        """空列表"""
        conn, ws_id = db_conn
        bcs = list_build_contexts(conn, ws_id)
        assert bcs == []


# ============================================
# TestActiveContext —— active 切换
# ============================================

class TestActiveContext:
    """active build context 测试"""

    def test_set_active(self, db_conn):
        """设置 active"""
        conn, ws_id = db_conn
        bc1 = register_build_context(conn, ws_id, "debug")
        bc2 = register_build_context(conn, ws_id, "release")

        assert not bc1.is_active
        assert not bc2.is_active

        # 设置 bc1 为 active
        assert set_active_build_context(conn, ws_id, bc1.build_context_hash)

        active = get_active_build_context(conn, ws_id)
        assert active.build_context_hash == bc1.build_context_hash

    def test_switch_active(self, db_conn):
        """切换 active"""
        conn, ws_id = db_conn
        bc1 = register_build_context(conn, ws_id, "debug", compile_flags=["-O0"], set_active=True)
        bc2 = register_build_context(conn, ws_id, "release", compile_flags=["-O2"])

        # 初始 active 是 bc1
        active = get_active_build_context(conn, ws_id)
        assert active.build_context_hash == bc1.build_context_hash

        # 切换到 bc2
        set_active_build_context(conn, ws_id, bc2.build_context_hash)
        active = get_active_build_context(conn, ws_id)
        assert active.build_context_hash == bc2.build_context_hash

    def test_only_one_active(self, db_conn):
        """同时只有一个 active"""
        conn, ws_id = db_conn
        bc1 = register_build_context(conn, ws_id, "debug")
        bc2 = register_build_context(conn, ws_id, "release")
        bc3 = register_build_context(conn, ws_id, "minsizerel")

        set_active_build_context(conn, ws_id, bc1.build_context_hash)
        set_active_build_context(conn, ws_id, bc2.build_context_hash)
        set_active_build_context(conn, ws_id, bc3.build_context_hash)

        # 只有 bc3 是 active
        active = get_active_build_context(conn, ws_id)
        assert active.build_context_hash == bc3.build_context_hash

        bcs = list_build_contexts(conn, ws_id)
        active_count = sum(1 for bc in bcs if bc.is_active)
        assert active_count == 1

    def test_set_active_nonexistent(self, db_conn):
        """设置不存在的为 active"""
        conn, ws_id = db_conn
        assert not set_active_build_context(conn, ws_id, "nonexistent")

    def test_no_active(self, db_conn):
        """没有 active"""
        conn, ws_id = db_conn
        register_build_context(conn, ws_id, "debug")
        assert get_active_build_context(conn, ws_id) is None


# ============================================
# TestDeleteBuildContext —— 删除
# ============================================

class TestDeleteBuildContext:
    """build context 删除测试"""

    def test_delete(self, db_conn):
        """删除"""
        conn, ws_id = db_conn
        bc = register_build_context(conn, ws_id, "debug")
        assert delete_build_context(conn, ws_id, bc.build_context_hash)
        assert get_build_context(conn, ws_id, bc.build_context_hash) is None

    def test_delete_nonexistent(self, db_conn):
        """删除不存在的"""
        conn, ws_id = db_conn
        assert not delete_build_context(conn, ws_id, "nonexistent")

    def test_delete_active(self, db_conn):
        """删除 active context"""
        conn, ws_id = db_conn
        bc = register_build_context(conn, ws_id, "debug", set_active=True)
        assert delete_build_context(conn, ws_id, bc.build_context_hash)
        assert get_active_build_context(conn, ws_id) is None


# ============================================
# TestResolveToolchain —— toolchain 解析
# ============================================

class TestResolveToolchain:
    """toolchain 解析测试"""

    def test_resolve_exact(self, db_conn, tmp_path):
        """精确匹配 build context"""
        conn, ws_id = db_conn
        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        tc = register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        bc = register_build_context(conn, ws_id, "debug", compile_flags=["-O0"])

        bind_toolchain_to_workspace(conn, ws_id, tc.id, bc.build_context_hash)

        resolved = resolve_toolchain(conn, ws_id, bc.build_context_hash)
        assert resolved is not None
        assert resolved.id == tc.id

    def test_resolve_via_active(self, db_conn, tmp_path):
        """通过 active context 解析"""
        conn, ws_id = db_conn
        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        tc = register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        bc = register_build_context(
            conn, ws_id, "debug",
            compile_flags=["-O0"], set_active=True,
        )
        bind_toolchain_to_workspace(conn, ws_id, tc.id, bc.build_context_hash)

        # 不指定 build_context_hash → 使用 active
        resolved = resolve_toolchain(conn, ws_id)
        assert resolved is not None
        assert resolved.id == tc.id

    def test_resolve_fallback_default(self, db_conn, tmp_path):
        """降级到默认 context（空 hash）"""
        conn, ws_id = db_conn
        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        tc = register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        # 绑定到默认 context（空 hash）
        bind_toolchain_to_workspace(conn, ws_id, tc.id, "")

        # 指定不存在的 context hash → 降级到默认
        resolved = resolve_toolchain(conn, ws_id, "nonexistent_hash")
        assert resolved is not None
        assert resolved.id == tc.id

    def test_resolve_none_when_no_binding(self, db_conn, tmp_path):
        """无任何绑定 → 返回 None"""
        conn, ws_id = db_conn
        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        register_toolchain(conn, "test_gcc", str(compiler), probe=False)
        register_build_context(conn, ws_id, "debug")

        resolved = resolve_toolchain(conn, ws_id)
        assert resolved is None

    def test_resolve_prefers_exact_over_active(self, db_conn, tmp_path):
        """精确匹配优先于 active"""
        conn, ws_id = db_conn
        compiler1 = tmp_path / "gcc"
        compiler1.write_text("gcc1")
        compiler2 = tmp_path / "clang"
        compiler2.write_text("clang")

        tc1 = register_toolchain(conn, "gcc_tc", str(compiler1), probe=False, sysroot="sys1")
        tc2 = register_toolchain(conn, "clang_tc", str(compiler2), probe=False, sysroot="sys2")

        bc_active = register_build_context(
            conn, ws_id, "debug",
            compile_flags=["-O0"], set_active=True,
        )
        bc_exact = register_build_context(
            conn, ws_id, "release",
            compile_flags=["-O2"],
        )

        bind_toolchain_to_workspace(conn, ws_id, tc1.id, bc_active.build_context_hash)
        bind_toolchain_to_workspace(conn, ws_id, tc2.id, bc_exact.build_context_hash)

        # 精确匹配应返回 tc2
        resolved = resolve_toolchain(conn, ws_id, bc_exact.build_context_hash)
        assert resolved is not None
        assert resolved.id == tc2.id

        # 不指定 → 使用 active → 返回 tc1
        resolved = resolve_toolchain(conn, ws_id)
        assert resolved is not None
        assert resolved.id == tc1.id


# ============================================
# TestBuildContextDataclass —— 数据类
# ============================================

class TestBuildContextDataclass:
    """BuildContext 数据类测试"""

    def test_to_dict(self):
        """序列化为 dict"""
        bc = BuildContext(
            workspace_id=1,
            build_context_hash="abc123",
            name="debug",
            compile_flags=["-O0"],
            defines={"DEBUG": "1"},
            include_paths=["/usr/include"],
            is_active=True,
        )
        d = bc.to_dict()
        assert d["workspace_id"] == 1
        assert d["name"] == "debug"
        assert d["compile_flags"] == ["-O0"]
        assert d["is_active"] is True

    def test_summary(self):
        """summary 字符串"""
        bc = BuildContext(
            workspace_id=1,
            build_context_hash="abcdef1234567890",
            name="debug",
        )
        s = bc.summary()
        assert "debug" in s
        assert "abcdef123456" in s  # hash 前 12 字符


# ============================================
# TestEndToEnd —— 端到端
# ============================================

class TestEndToEnd:
    """端到端流程测试"""

    def test_full_lifecycle(self, db_conn, tmp_path):
        """完整生命周期：注册 → 绑定 → 切换 → 解析"""
        conn, ws_id = db_conn
        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        tc = register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        # 注册两个 build context
        debug_bc = register_build_context(
            conn, ws_id, "debug",
            compile_flags=["-O0", "-g"],
            defines={"DEBUG": "1"},
            set_active=True,
        )
        release_bc = register_build_context(
            conn, ws_id, "release",
            compile_flags=["-O2"],
            defines={"NDEBUG": "1"},
        )

        # 绑定 toolchain 到两个 context
        bind_toolchain_to_workspace(conn, ws_id, tc.id, debug_bc.build_context_hash)
        bind_toolchain_to_workspace(conn, ws_id, tc.id, release_bc.build_context_hash)

        # 通过 active 解析
        resolved = resolve_toolchain(conn, ws_id)
        assert resolved is not None
        assert resolved.id == tc.id

        # 切换到 release
        set_active_build_context(conn, ws_id, release_bc.build_context_hash)
        active = get_active_build_context(conn, ws_id)
        assert active.name == "release"

        # 精确解析 debug
        resolved = resolve_toolchain(conn, ws_id, debug_bc.build_context_hash)
        assert resolved is not None
        assert resolved.id == tc.id

        # 删除 debug context
        delete_build_context(conn, ws_id, debug_bc.build_context_hash)
        assert get_build_context(conn, ws_id, debug_bc.build_context_hash) is None
        assert len(list_build_contexts(conn, ws_id)) == 1

    def test_multi_workspace_isolation(self, db_conn, tmp_path):
        """多 workspace 隔离"""
        conn, ws_id = db_conn
        # 创建第二个 workspace
        conn.execute(
            "INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
            ("ws2", str(tmp_path / "ws2"), 0.0),
        )
        conn.commit()
        ws2_id = conn.execute("SELECT id FROM workspaces WHERE name='ws2'").fetchone()[0]

        compiler = tmp_path / "gcc"
        compiler.write_text("gcc")
        tc = register_toolchain(conn, "test_gcc", str(compiler), probe=False)

        # ws1 有 debug context，ws2 有 release context
        bc1 = register_build_context(conn, ws_id, "debug", compile_flags=["-O0"])
        bc2 = register_build_context(conn, ws2_id, "release", compile_flags=["-O2"])

        # 两个 context hash 不同
        assert bc1.build_context_hash != bc2.build_context_hash

        # 绑定
        bind_toolchain_to_workspace(conn, ws_id, tc.id, bc1.build_context_hash)
        bind_toolchain_to_workspace(conn, ws2_id, tc.id, bc2.build_context_hash)

        # 各自解析
        r1 = resolve_toolchain(conn, ws_id, bc1.build_context_hash)
        r2 = resolve_toolchain(conn, ws2_id, bc2.build_context_hash)
        assert r1 is not None
        assert r2 is not None
        assert r1.id == tc.id
        assert r2.id == tc.id

        # ws1 不能解析 ws2 的 context
        r_cross = resolve_toolchain(conn, ws_id, bc2.build_context_hash)
        # 精确匹配失败 → 无 active → 无默认 → None
        assert r_cross is None
