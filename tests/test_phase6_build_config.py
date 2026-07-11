"""
Phase 6.4: 构建系统配置解析器测试

测试 compile_commands.json / Makefile / Kconfig 解析和导入。
"""

import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import importlib.util


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_parser_mod = _load_module("build_config_parser", str(Path(__file__).parent.parent / "db" / "build_config_parser.py"))
_tc_mod = _load_module("db_toolchain", str(Path(__file__).parent.parent / "db" / "db_toolchain.py"))
_schema_mod = _load_module("db_schema", str(Path(__file__).parent.parent / "db" / "schema.py"))

parse_compile_commands = _parser_mod.parse_compile_commands
compile_commands_to_build_context = _parser_mod.compile_commands_to_build_context
parse_makefile = _parser_mod.parse_makefile
parse_kconfig = _parser_mod.parse_kconfig
import_build_config = _parser_mod.import_build_config
_detect_config_type = _parser_mod._detect_config_type

init_toolchain_schema = _tc_mod.init_toolchain_schema
get_build_context = _tc_mod.get_build_context
list_build_contexts = _tc_mod.list_build_contexts
compute_build_context_hash = _tc_mod.compute_build_context_hash

SCHEMA_SQL = _schema_mod.SCHEMA_SQL


@pytest.fixture
def db_conn(tmp_path):
    db_path = str(tmp_path / "test.db")
    conn = sqlite3.connect(db_path)
    init_toolchain_schema(conn)
    conn.executescript(SCHEMA_SQL)
    conn.execute(
        "INSERT INTO workspaces (name, root_path, created_at) VALUES (?, ?, ?)",
        ("test_ws", str(tmp_path), 0.0),
    )
    conn.commit()
    ws_id = conn.execute("SELECT id FROM workspaces WHERE name='test_ws'").fetchone()[0]
    yield conn, ws_id
    conn.close()


# ============================================
# TestCompileCommands —— compile_commands.json
# ============================================

class TestCompileCommands:
    """compile_commands.json 解析测试"""

    def test_parse_basic(self, tmp_path):
        """基本解析"""
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {
                "directory": "/build",
                "file": "src/main.c",
                "command": "gcc -O2 -g -DDEBUG=1 -I/usr/include -c src/main.c -o main.o",
            }
        ]))

        entries = parse_compile_commands(str(cc_path))
        assert len(entries) == 1
        entry = entries[0]
        assert entry["file"] == "src/main.c"
        assert entry["directory"] == "/build"
        assert entry["compiler"] == "gcc"
        assert "-O2" in entry["compile_flags"]
        assert "-g" in entry["compile_flags"]
        assert entry["defines"] == {"DEBUG": "1"}
        assert "/usr/include" in entry["include_paths"]

    def test_parse_with_arguments(self, tmp_path):
        """使用 arguments 数组格式"""
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {
                "directory": "/build",
                "file": "src/foo.c",
                "arguments": ["clang", "-c", "-O3", "-DRELEASE", "-I/inc"],
            }
        ]))

        entries = parse_compile_commands(str(cc_path))
        entry = entries[0]
        assert entry["compiler"] == "clang"
        assert "-O3" in entry["compile_flags"]
        assert entry["defines"] == {"RELEASE": "1"}
        assert "/inc" in entry["include_paths"]

    def test_parse_multiple_entries(self, tmp_path):
        """多个编译单元"""
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {"directory": "/b", "file": "a.c", "command": "gcc -O2 -c a.c"},
            {"directory": "/b", "file": "b.c", "command": "gcc -O0 -g -c b.c"},
        ]))

        entries = parse_compile_commands(str(cc_path))
        assert len(entries) == 2

    def test_parse_define_without_value(self, tmp_path):
        """无值的 define"""
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {"directory": "/b", "file": "a.c", "command": "gcc -DFOO -c a.c"},
        ]))

        entries = parse_compile_commands(str(cc_path))
        assert entries[0]["defines"] == {"FOO": "1"}

    def test_parse_define_with_value(self, tmp_path):
        """有值的 define"""
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {"directory": "/b", "file": "a.c", "command": "gcc -DVERSION=\\\"1.0\\\" -c a.c"},
        ]))

        entries = parse_compile_commands(str(cc_path))
        assert entries[0]["defines"]["VERSION"] == '"1.0"'

    def test_parse_relative_include(self, tmp_path):
        """相对路径 include"""
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {"directory": "/build", "file": "a.c", "command": "gcc -Iinclude -c a.c"},
        ]))

        entries = parse_compile_commands(str(cc_path))
        assert "/build/include" in entries[0]["include_paths"]

    def test_aggregate_to_build_context(self, tmp_path):
        """聚合为 build context"""
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {"directory": "/b", "file": "a.c", "command": "gcc -O2 -DDEBUG -I/a -c a.c"},
            {"directory": "/b", "file": "b.c", "command": "gcc -g -DRELEASE -I/b -c b.c"},
        ]))

        entries = parse_compile_commands(str(cc_path))
        flags, defines, includes = compile_commands_to_build_context(entries)

        assert "-O2" in flags
        assert "-g" in flags
        assert "DEBUG" in defines
        assert "RELEASE" in defines
        assert "/a" in includes
        assert "/b" in includes


# ============================================
# TestMakefile —— Makefile 解析
# ============================================

class TestMakefile:
    """Makefile 解析测试"""

    def test_parse_basic(self, tmp_path):
        """基本解析"""
        mk_path = tmp_path / "Makefile"
        mk_path.write_text(
            "CC = gcc\n"
            "CFLAGS = -O2 -g\n"
            "DEFINES = -DDEBUG=1\n"
            "INCLUDES = -I/usr/include\n"
        )

        info = parse_makefile(str(mk_path))
        assert info["compiler"] == "gcc"
        assert "-O2" in info["compile_flags"]
        assert "-g" in info["compile_flags"]
        assert info["defines"] == {"DEBUG": "1"}
        assert "/usr/include" in info["include_paths"]

    def test_parse_cflags_with_d_and_i(self, tmp_path):
        """CFLAGS 中包含 -D 和 -I"""
        mk_path = tmp_path / "Makefile"
        mk_path.write_text(
            "CFLAGS = -O2 -DDEBUG=1 -DVERSION=2 -I/inc1 -I/inc2\n"
        )

        info = parse_makefile(str(mk_path))
        assert "-O2" in info["compile_flags"]
        assert info["defines"] == {"DEBUG": "1", "VERSION": "2"}
        assert "/inc1" in info["include_paths"]
        assert "/inc2" in info["include_paths"]

    def test_parse_variable_expansion(self, tmp_path):
        """变量展开"""
        mk_path = tmp_path / "Makefile"
        mk_path.write_text(
            "BASE_FLAGS = -O2 -g\n"
            "CFLAGS = $(BASE_FLAGS) -Wall\n"
        )

        info = parse_makefile(str(mk_path))
        assert "-O2" in info["compile_flags"]
        assert "-g" in info["compile_flags"]
        assert "-Wall" in info["compile_flags"]

    def test_parse_append(self, tmp_path):
        """+= 追加"""
        mk_path = tmp_path / "Makefile"
        mk_path.write_text(
            "CFLAGS = -O2\n"
            "CFLAGS += -g\n"
        )

        info = parse_makefile(str(mk_path))
        assert "-O2" in info["compile_flags"]
        assert "-g" in info["compile_flags"]

    def test_parse_empty_makefile(self, tmp_path):
        """空 Makefile"""
        mk_path = tmp_path / "Makefile"
        mk_path.write_text("# empty\n")

        info = parse_makefile(str(mk_path))
        assert info["compiler"] == ""
        assert info["compile_flags"] == []
        assert info["defines"] == {}
        assert info["include_paths"] == []

    def test_parse_comment(self, tmp_path):
        """注释"""
        mk_path = tmp_path / "Makefile"
        mk_path.write_text(
            "CFLAGS = -O2 # optimization\n"
            "CC = gcc # compiler\n"
        )

        info = parse_makefile(str(mk_path))
        assert "-O2" in info["compile_flags"]
        assert info["compiler"] == "gcc"


# ============================================
# TestKconfig —— Kconfig (.config) 解析
# ============================================

class TestKconfig:
    """Kconfig (.config) 解析测试"""

    def test_parse_basic(self, tmp_path):
        """基本解析"""
        config_path = tmp_path / ".config"
        config_path.write_text(
            "CONFIG_X86=y\n"
            "CONFIG_MODULES=y\n"
            'CONFIG_VERSION="6.1.0"\n'
            "CONFIG_DEBUG_INFO=n\n"
            "# CONFIG_Something is not set\n"
        )

        info = parse_kconfig(str(config_path))
        defines = info["defines"]
        assert defines["CONFIG_X86"] == "y"
        assert defines["CONFIG_MODULES"] == "y"
        assert defines["CONFIG_VERSION"] == "6.1.0"
        assert defines["CONFIG_DEBUG_INFO"] == "n"
        # "is not set" 行被跳过（不以 CONFIG_ 开头）
        assert "CONFIG_Something" not in defines

    def test_parse_module(self, tmp_path):
        """模块配置"""
        config_path = tmp_path / ".config"
        config_path.write_text(
            "CONFIG_NET=m\n"
            "CONFIG_EXT4_FS=m\n"
        )

        info = parse_kconfig(str(config_path))
        assert info["defines"]["CONFIG_NET"] == "m"
        assert info["defines"]["CONFIG_EXT4_FS"] == "m"

    def test_parse_quoted_value(self, tmp_path):
        """带引号的值"""
        config_path = tmp_path / ".config"
        config_path.write_text(
            'CONFIG_INITRAMFS_SOURCE="/path/to/initramfs"\n'
            'CONFIG_LOCALVERSION="-custom"\n'
        )

        info = parse_kconfig(str(config_path))
        assert info["defines"]["CONFIG_INITRAMFS_SOURCE"] == "/path/to/initramfs"
        assert info["defines"]["CONFIG_LOCALVERSION"] == "-custom"

    def test_parse_empty_config(self, tmp_path):
        """空配置"""
        config_path = tmp_path / ".config"
        config_path.write_text("# empty config\n")

        info = parse_kconfig(str(config_path))
        assert info["defines"] == {}

    def test_parse_comments(self, tmp_path):
        """注释行"""
        config_path = tmp_path / ".config"
        config_path.write_text(
            "# This is a comment\n"
            "# CONFIG_UNUSED is not set\n"
            "CONFIG_USED=y\n"
        )

        info = parse_kconfig(str(config_path))
        assert "CONFIG_USED" in info["defines"]
        assert "CONFIG_UNUSED" not in info["defines"]


# ============================================
# TestDetectConfigType —— 自动检测
# ============================================

class TestDetectConfigType:
    """配置类型自动检测测试"""

    def test_detect_compile_commands(self, tmp_path):
        """检测 compile_commands.json"""
        path = tmp_path / "compile_commands.json"
        path.write_text("[]")
        assert _detect_config_type(str(path)) == "compile_commands"

    def test_detect_makefile(self, tmp_path):
        """检测 Makefile"""
        path = tmp_path / "Makefile"
        path.write_text("CC = gcc\n")
        assert _detect_config_type(str(path)) == "makefile"

    def test_detect_kconfig(self, tmp_path):
        """检测 .config"""
        path = tmp_path / ".config"
        path.write_text("CONFIG_X=y\n")
        assert _detect_config_type(str(path)) == "kconfig"


# ============================================
# TestImportBuildConfig —— 导入 build config
# ============================================

class TestImportBuildConfig:
    """build config 导入测试"""

    def test_import_compile_commands(self, db_conn, tmp_path):
        """导入 compile_commands.json"""
        conn, ws_id = db_conn
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {"directory": "/b", "file": "a.c", "command": "gcc -O2 -DDEBUG=1 -I/inc -c a.c"},
        ]))

        bc = import_build_config(conn, ws_id, str(cc_path), name="cc_debug")

        assert bc.workspace_id == ws_id
        assert bc.name == "cc_debug"
        assert "DEBUG" in bc.defines
        assert "/inc" in bc.include_paths

    def test_import_makefile(self, db_conn, tmp_path):
        """导入 Makefile"""
        conn, ws_id = db_conn
        mk_path = tmp_path / "Makefile"
        mk_path.write_text(
            "CC = gcc\n"
            "CFLAGS = -O2 -g -DDEBUG=1 -I/inc\n"
        )

        bc = import_build_config(conn, ws_id, str(mk_path), name="mk_debug")

        assert bc.name == "mk_debug"
        assert bc.defines.get("DEBUG") == "1"
        assert "/inc" in bc.include_paths

    def test_import_kconfig(self, db_conn, tmp_path):
        """导入 .config"""
        conn, ws_id = db_conn
        config_path = tmp_path / ".config"
        config_path.write_text(
            "CONFIG_X86=y\n"
            "CONFIG_DEBUG_INFO=y\n"
        )

        bc = import_build_config(conn, ws_id, str(config_path), name="kernel_debug")

        assert bc.name == "kernel_debug"
        assert "CONFIG_X86" in bc.defines
        assert "CONFIG_DEBUG_INFO" in bc.defines

    def test_import_auto_detect(self, db_conn, tmp_path):
        """自动检测类型"""
        conn, ws_id = db_conn
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {"directory": "/b", "file": "a.c", "command": "gcc -DTEST=1 -c a.c"},
        ]))

        bc = import_build_config(conn, ws_id, str(cc_path), config_type="auto")

        assert "TEST" in bc.defines

    def test_import_set_active(self, db_conn, tmp_path):
        """导入并设为 active"""
        conn, ws_id = db_conn
        mk_path = tmp_path / "Makefile"
        mk_path.write_text("CFLAGS = -O2\n")

        bc = import_build_config(
            conn, ws_id, str(mk_path), name="active_build", set_active=True,
        )

        assert bc.is_active

    def test_import_auto_name(self, db_conn, tmp_path):
        """自动生成名称（使用文件名）"""
        conn, ws_id = db_conn
        mk_path = tmp_path / "MyBuild.mk"
        mk_path.write_text("CC = gcc\n")

        bc = import_build_config(conn, ws_id, str(mk_path))

        assert bc.name == "MyBuild"


# ============================================
# TestEndToEnd —— 端到端
# ============================================

class TestEndToEnd:
    """端到端测试"""

    def test_compile_commands_to_build_context(self, db_conn, tmp_path):
        """compile_commands → build context → 查询"""
        conn, ws_id = db_conn
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps([
            {"directory": "/b", "file": "a.c", "command": "gcc -O2 -DDEBUG=1 -I/inc1 -c a.c"},
            {"directory": "/b", "file": "b.c", "command": "gcc -O0 -g -DRELEASE=1 -I/inc2 -c b.c"},
        ]))

        bc = import_build_config(conn, ws_id, str(cc_path), name="full_build", set_active=True)

        # 验证
        fetched = get_build_context(conn, ws_id, bc.build_context_hash)
        assert fetched is not None
        assert "DEBUG" in fetched.defines
        assert "RELEASE" in fetched.defines
        assert "/inc1" in fetched.include_paths
        assert "/inc2" in fetched.include_paths

    def test_different_configs_different_contexts(self, db_conn, tmp_path):
        """不同配置文件 → 不同 build context"""
        conn, ws_id = db_conn

        mk_path = tmp_path / "Makefile"
        mk_path.write_text("CFLAGS = -O2 -DDEBUG=1\n")

        config_path = tmp_path / ".config"
        config_path.write_text("CONFIG_RELEASE=y\n")

        bc1 = import_build_config(conn, ws_id, str(mk_path), name="makefile_ctx")
        bc2 = import_build_config(conn, ws_id, str(config_path), name="kconfig_ctx")

        assert bc1.build_context_hash != bc2.build_context_hash
        assert "DEBUG" in bc1.defines
        assert "CONFIG_RELEASE" in bc2.defines

        # 列出所有
        bcs = list_build_contexts(conn, ws_id)
        assert len(bcs) == 2
