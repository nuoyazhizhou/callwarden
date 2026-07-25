"""L5: 构建上下文感知测试

覆盖：
  - compile_commands.json 解析器（analyzers/compile_commands.py）
  - build-context CLI 子命令（cli/main.py）
  - db_toolchain.py 的 build_context CRUD
  - MCP 工具注册验证（server/mcp_server.py）
"""

import json
import os
import sys
import tempfile
import pytest

# 确保项目根在 sys.path（callwarden 包已 pip install -e . 安装）
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# 通过 callwarden 包导入（确保相对导入正确解析）
from callwarden.analyzers.compile_commands import (
    parse_compile_commands,
    aggregate_build_context,
    import_compile_commands,
    split_compile_flags,
    CompileEntry,
    AggregatedBuildContext,
)


# ============================================
# compile_commands.json 解析器测试
# ============================================

@pytest.fixture
def sample_compile_commands():
    """生成示例 compile_commands.json"""
    return [
        {
            "directory": "/project/build",
            "command": "gcc -DDEBUG=1 -DBOARD=\"A98\" -I./include -I./src -O2 -g main.c -o main.o",
            "file": "main.c",
            "output": "main.o"
        },
        {
            "directory": "/project/build",
            "arguments": ["arm-none-eabi-gcc", "-DCONFIG_DEBUG", "-I./lib", "-std=c11", "-Os", "sensor.c", "-o", "sensor.o"],
            "file": "sensor.c",
            "output": "sensor.o"
        },
        {
            "directory": "/project/build",
            "command": "clang++ -DRELEASE=1 -I./include -I/opt/sysroot/usr/include -std=c++17 -O3 driver.cpp",
            "file": "driver.cpp"
        },
    ]


@pytest.fixture
def temp_compile_commands_file(tmp_path, sample_compile_commands):
    """创建临时 compile_commands.json 文件"""
    f = tmp_path / "compile_commands.json"
    f.write_text(json.dumps(sample_compile_commands), encoding="utf-8")
    return str(f)


class TestParseCompileCommands:
    """解析器测试"""

    def test_parse_basic(self, temp_compile_commands_file):
        """基本解析：3 条记录"""
        entries = parse_compile_commands(temp_compile_commands_file)
        assert len(entries) == 3

    def test_parse_command_form(self, temp_compile_commands_file):
        """command 字符串形式解析"""
        entries = parse_compile_commands(temp_compile_commands_file)
        # 第一条用 command 字符串
        e = entries[0]
        assert e.file == "main.c"
        assert e.compiler_path == "gcc"
        assert "DEBUG" in e.defines
        assert e.defines["DEBUG"] == "1"
        assert "BOARD" in e.defines
        assert "./include" in e.include_paths
        assert "./src" in e.include_paths
        assert "-O2" in e.compile_flags
        assert "-g" in e.compile_flags

    def test_parse_arguments_form(self, temp_compile_commands_file):
        """arguments 数组形式解析"""
        entries = parse_compile_commands(temp_compile_commands_file)
        # 第二条用 arguments 数组
        e = entries[1]
        assert e.file == "sensor.c"
        assert e.compiler_path == "arm-none-eabi-gcc"
        assert "CONFIG_DEBUG" in e.defines
        assert e.defines["CONFIG_DEBUG"] == ""
        assert "./lib" in e.include_paths
        assert "-std=c11" in e.compile_flags
        assert "-Os" in e.compile_flags

    def test_parse_clang_pp(self, temp_compile_commands_file):
        """clang++ 解析"""
        entries = parse_compile_commands(temp_compile_commands_file)
        e = entries[2]
        assert e.compiler_path == "clang++"
        assert "RELEASE" in e.defines
        assert e.defines["RELEASE"] == "1"
        assert "-std=c++17" in e.compile_flags
        assert "-O3" in e.compile_flags

    def test_parse_isystem(self, tmp_path):
        """-isystem 选项解析"""
        data = [{
            "directory": "/build",
            "command": "gcc -isystem /opt/sysroot/include -isystem /usr/include main.c",
            "file": "main.c"
        }]
        f = tmp_path / "cc.json"
        f.write_text(json.dumps(data))
        entries = parse_compile_commands(str(f))
        assert "/opt/sysroot/include" in entries[0].include_paths
        assert "/usr/include" in entries[0].include_paths

    def test_parse_include_option(self, tmp_path):
        """-include 强制包含头文件"""
        data = [{
            "directory": "/build",
            "command": "gcc -include config.h main.c",
            "file": "main.c"
        }]
        f = tmp_path / "cc.json"
        f.write_text(json.dumps(data))
        entries = parse_compile_commands(str(f))
        assert any("config.h" in flag for flag in entries[0].compile_flags)

    def test_parse_define_without_value(self, tmp_path):
        """-D 无值（布尔宏）"""
        data = [{
            "directory": "/build",
            "command": "gcc -DENABLE_FEATURE main.c",
            "file": "main.c"
        }]
        f = tmp_path / "cc.json"
        f.write_text(json.dumps(data))
        entries = parse_compile_commands(str(f))
        assert "ENABLE_FEATURE" in entries[0].defines
        assert entries[0].defines["ENABLE_FEATURE"] == ""

    def test_parse_file_not_found(self):
        """文件不存在"""
        with pytest.raises(FileNotFoundError):
            parse_compile_commands("/nonexistent/compile_commands.json")

    def test_parse_invalid_json(self, tmp_path):
        """无效 JSON"""
        f = tmp_path / "bad.json"
        f.write_text("{invalid")
        with pytest.raises(json.JSONDecodeError):
            parse_compile_commands(str(f))

    def test_parse_non_array_json(self, tmp_path):
        """非数组 JSON"""
        f = tmp_path / "obj.json"
        f.write_text('{"key": "value"}')
        with pytest.raises(ValueError, match="should be a JSON array"):
            parse_compile_commands(str(f))


class TestAggregateBuildContext:
    """聚合测试"""

    def test_aggregate_basic(self, sample_compile_commands):
        """基本聚合"""
        entries = parse_compile_commands.__wrapped__ if hasattr(parse_compile_commands, '__wrapped__') else None
        # 直接用 import_compile_commands
        # 先写临时文件
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            json.dump(sample_compile_commands, f)
            tmp_path = f.name

        try:
            agg = import_compile_commands(tmp_path, workspace_root="/project")
            assert agg.file_count == 3
            # defines 并集
            assert "DEBUG" in agg.defines
            assert "CONFIG_DEBUG" in agg.defines
            assert "RELEASE" in agg.defines
            # include_paths 并集去重（路径被规范化为绝对路径）
            inc_str = " ".join(agg.include_paths)
            assert "include" in inc_str
            assert "src" in inc_str
            assert "lib" in inc_str
            # compile_flags 去重
            assert "-O2" in agg.compile_flags
            assert "-Os" in agg.compile_flags
            assert "-O3" in agg.compile_flags
            # compiler_path 取第一个非空
            assert agg.compiler_path == "gcc"
        finally:
            os.unlink(tmp_path)

    def test_aggregate_dedup(self):
        """去重测试"""
        entries = [
            CompileEntry(file="a.c", defines={"A": "1"}, include_paths=["./inc", "./src"], compile_flags=["-O2"]),
            CompileEntry(file="b.c", defines={"A": "1", "B": "2"}, include_paths=["./inc", "./lib"], compile_flags=["-O2", "-g"]),
        ]
        agg = aggregate_build_context(entries)
        assert agg.defines == {"A": "1", "B": "2"}
        assert agg.include_paths == ["./inc", "./src", "./lib"]
        assert agg.compile_flags == ["-O2", "-g"]


class TestSplitCompileFlags:
    """split_compile_flags 函数测试"""

    def test_split_basic(self):
        """基本拆分"""
        tokens = ["gcc", "-DDEBUG=1", "-I./inc", "-O2", "main.c"]
        compiler, defines, includes, flags = split_compile_flags(tokens)
        assert compiler == "gcc"
        assert defines == {"DEBUG": "1"}
        assert includes == ["./inc"]
        assert "-O2" in flags

    def test_split_empty(self):
        """空 token"""
        compiler, defines, includes, flags = split_compile_flags([])
        assert compiler == ""
        assert defines == {}
        assert includes == []
        assert flags == []


# ============================================
# build-context CLI 测试
# ============================================

class TestBuildContextCLI:
    """cw build-context CLI 子命令测试"""

    def test_register_and_list(self, tmp_path):
        """注册 + 列表"""
        from callwarden.cli.main import _handle_build_context
        from callwarden.db import CodeGraphDB

        db = CodeGraphDB(str(tmp_path / "test.db"))
        # 先注册一个 workspace
        ws_id = db.register_workspace("test", str(tmp_path))

        # 注册 build context（用 -- 分隔避免 argparse 把 -O2 误认为选项）
        ok = _handle_build_context(
            ["register", str(ws_id), "debug", "--flags", "O2", "g",
             "--defines", "DEBUG=1", "BOARD=A98", "--includes", "./inc"],
            db,
        )
        assert ok

        # 列表
        ok = _handle_build_context(["list", str(ws_id)], db)
        assert ok

    def test_import_compile_commands(self, tmp_path, sample_compile_commands):
        """从 compile_commands.json 导入"""
        from callwarden.cli.main import _handle_build_context
        from callwarden.db import CodeGraphDB

        # 创建 compile_commands.json
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps(sample_compile_commands))

        db = CodeGraphDB(str(tmp_path / "test.db"))
        ws_id = db.register_workspace("test", str(tmp_path))

        ok = _handle_build_context(
            ["import-compile-commands", str(cc_path), str(ws_id),
             "--name", "firmware-debug", "--activate"],
            db,
        )
        assert ok

        # 验证列表中有导入的 context
        ok = _handle_build_context(["list", str(ws_id)], db)
        assert ok


# ============================================
# db_toolchain 集成测试
# ============================================

class TestDbToolchain:
    """db_toolchain.py build_context CRUD 测试"""

    def test_build_context_crud(self, tmp_path):
        """CRUD 完整流程"""
        from callwarden.db import CodeGraphDB
        from callwarden.db.db_toolchain import (
            init_toolchain_schema, register_build_context,
            get_build_context, list_build_contexts,
            set_active_build_context, get_active_build_context,
            delete_build_context, compute_build_context_hash,
        )

        db = CodeGraphDB(str(tmp_path / "test.db"))
        init_toolchain_schema(db.conn)
        ws_id = db.register_workspace("test", str(tmp_path))

        # register
        ctx = register_build_context(
            db.conn, ws_id, "debug",
            compile_flags=["-O2", "-g"],
            defines={"DEBUG": "1"},
            include_paths=["./inc"],
            set_active=True,
        )
        assert ctx.name == "debug"
        assert ctx.build_context_hash
        assert ctx.is_active

        # get
        fetched = get_build_context(db.conn, ws_id, ctx.build_context_hash)
        assert fetched is not None
        assert fetched.name == "debug"
        assert fetched.defines == {"DEBUG": "1"}

        # list
        ctxs = list_build_contexts(db.conn, ws_id)
        assert len(ctxs) == 1

        # active
        active = get_active_build_context(db.conn, ws_id)
        assert active is not None
        assert active.build_context_hash == ctx.build_context_hash

        # register another
        ctx2 = register_build_context(
            db.conn, ws_id, "release",
            compile_flags=["-O3"],
            defines={"NDEBUG": "1"},
            set_active=True,
        )

        # 验证 active 切换
        active = get_active_build_context(db.conn, ws_id)
        assert active.build_context_hash == ctx2.build_context_hash

        # delete
        assert delete_build_context(db.conn, ws_id, ctx.build_context_hash)
        assert get_build_context(db.conn, ws_id, ctx.build_context_hash) is None

    def test_build_context_hash_deterministic(self):
        """hash 确定性"""
        from callwarden.db.db_toolchain import compute_build_context_hash
        h1 = compute_build_context_hash(["-O2"], {"DEBUG": "1"}, ["./inc"])
        h2 = compute_build_context_hash(["-O2"], {"DEBUG": "1"}, ["./inc"])
        assert h1 == h2

    def test_build_context_hash_order_independent(self):
        """hash 顺序无关"""
        from callwarden.db.db_toolchain import compute_build_context_hash
        h1 = compute_build_context_hash(["-O2", "-g"], {"A": "1", "B": "2"}, ["./a", "./b"])
        h2 = compute_build_context_hash(["-g", "-O2"], {"B": "2", "A": "1"}, ["./b", "./a"])
        assert h1 == h2


# ============================================
# MCP 工具注册验证
# ============================================

class TestMcpToolsRegistration:
    """验证 L5 MCP 工具已注册"""

    def test_l5_tools_exist(self):
        """L5 相关 MCP 工具应该已注册"""
        # 由于 MCP 工具在 create_mcp_server() 内部定义，
        # 我们通过检查源码来验证工具存在
        import server.mcp_server as ms
        source = open(ms.__file__, encoding='utf-8').read()

        l5_tools = [
            "list_toolchains",
            "get_toolchain",
            "get_workspace_toolchains",
            "list_build_contexts",
            "get_build_context",
            "get_active_build_context",
            "get_resolved_edges",
            "count_resolved_edges",
        ]
        for tool_name in l5_tools:
            assert f"def {tool_name}(" in source, f"MCP tool {tool_name} not found in mcp_server.py"

    def test_l5_tools_count(self):
        """L5 新增 8 个 MCP 工具"""
        import server.mcp_server as ms
        source = open(ms.__file__, encoding='utf-8').read()
        # 统计 @mcp.tool() 装饰器
        count = source.count("@mcp.tool()")
        # 原来是 196，新增 8 个 L5 + 1 个 get_metrics = 205+
        assert count >= 205, f"Expected >=205 MCP tools, got {count}"


# ============================================
# 端到端测试
# ============================================

class TestE2E:
    """端到端：compile_commands.json → import → MCP 查询"""

    def test_full_flow(self, tmp_path, sample_compile_commands):
        """完整流程"""
        from callwarden.db import CodeGraphDB
        from callwarden.db.db_toolchain import (
            init_toolchain_schema, register_build_context,
            list_build_contexts, get_active_build_context,
        )
        # import_compile_commands 已在模块顶部通过 importlib 加载

        # 1. 创建 compile_commands.json
        cc_path = tmp_path / "compile_commands.json"
        cc_path.write_text(json.dumps(sample_compile_commands))

        # 2. 设置 DB
        db = CodeGraphDB(str(tmp_path / "test.db"))
        init_toolchain_schema(db.conn)
        ws_id = db.register_workspace("firmware", str(tmp_path))

        # 3. 导入 compile_commands.json
        agg = import_compile_commands(str(cc_path), workspace_root=str(tmp_path))

        # 4. 注册 build context
        ctx = register_build_context(
            db.conn, ws_id, "firmware-debug",
            compile_flags=agg.compile_flags,
            defines=agg.defines,
            include_paths=agg.include_paths,
            set_active=True,
        )

        # 5. 验证
        ctxs = list_build_contexts(db.conn, ws_id)
        assert len(ctxs) == 1

        active = get_active_build_context(db.conn, ws_id)
        assert active is not None
        assert active.name == "firmware-debug"
        assert "DEBUG" in active.defines
        assert "CONFIG_DEBUG" in active.defines
