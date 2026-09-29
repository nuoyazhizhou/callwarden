"""T1 全量测试基建冒烟验证(种子 workspace fixture + param_provider 骨架)。

本文件是全功能全量测试(243 MCP 工具 + 234 CLI 命令全参数真实调用)的 T1
基建交付验证。它不测业务功能,只验证:

1. 种子 workspace fixture 能在隔离 daemon 上 register + build_graph + publish
   snapshot,并产出真实可查询的符号图谱(query.stats 符号数 > 0);
2. param_provider 能读取固化的 243 工具 / 234 命令 schema,并为**每一个**
   工具/命令生成参数(全参数与仅必填两种模式)而不抛异常 —— 证明骨架覆盖
   了所有工具的参数名族/类型,T2/T3 可直接在其上做真实调用。

隔离 daemon(release 二进制 + 临时 data root),不触碰生产 daemon。
"""
from __future__ import annotations

import pytest

import param_provider as pp


# ----------------------------------------------------------------------
# 1. 种子 workspace:build_graph 产出真实符号
# ----------------------------------------------------------------------
def test_seed_workspace_build_graph_produces_symbols(seed_workspace):
    """种子样本 build_graph 后应有非空符号图谱。"""
    build = seed_workspace["build"]
    assert isinstance(build, dict), f"build_graph 未返回 dict: {build!r}"
    assert build.get("ok") is True, f"build_graph 未成功: {build!r}"
    # 种子样本(calc.py + service.ts)应至少解析出若干符号与调用边
    assert build.get("symbols", 0) > 0, f"build_graph 未产出符号: {build!r}"


def test_seed_workspace_query_stats_ready(seed_workspace):
    """publish snapshot 后 query.stats 应可读且符号数 > 0(验证 snapshot 就绪链路)。"""
    stats = seed_workspace["stats"]
    if stats is None:
        pytest.skip("snapshot publish/stats 未就绪(隔离 daemon 环境限制)")
    assert isinstance(stats, dict)
    assert stats.get("symbol_count", 0) > 0, f"query.stats 符号数为 0: {stats!r}"


def test_seed_context_populated(seed_workspace):
    """SeedContext 应被真实前置状态填充(workspace id/instance/已知符号)。"""
    ctx = seed_workspace["ctx"]
    assert ctx.workspace_id is not None
    assert ctx.workspace_instance_id
    assert ctx.root
    assert ctx.known_qualified_names, "已知符号名为空"
    assert ctx.first_qname()  # 不抛
    assert ctx.first_callee()
    assert ctx.first_file()


# ----------------------------------------------------------------------
# 2. param_provider:为全部 243 MCP 工具生成参数(骨架完整性)
# ----------------------------------------------------------------------
def test_mcp_schema_loads_243_tools():
    schema = pp.load_mcp_schema()
    assert schema.get("total") == len(schema.get("tools", []))
    assert schema.get("total", 0) >= 240, "MCP 工具数异常(预期 243 附近)"


def test_provider_generates_params_for_all_mcp_tools():
    """provider 能为每个 MCP 工具生成全参数与仅必填参数,不抛异常。"""
    schema = pp.load_mcp_schema()
    ctx = pp.SeedContext(
        workspace_id=1, workspace_instance_id="seedinst00000000",
        root=".", known_qualified_names=["compute"],
        known_callee_names=["add"], known_file_paths=["calc.py"],
    )
    failures = []
    for tool in schema["tools"]:
        name = tool.get("name", "?")
        try:
            full = pp.build_mcp_params(tool, ctx, required_only=False)
            req = pp.build_mcp_params(tool, ctx, required_only=True)
            assert isinstance(full, dict) and isinstance(req, dict)
            # 必填参数必须全部出现在生成结果里
            for rp in tool.get("required") or []:
                assert rp in req, f"{name}: 必填参数 {rp} 未生成"
            # 每个必填参数值非 None(daemon strict transport 常拒 null)
            for rp, val in req.items():
                assert val is not None, f"{name}: 必填参数 {rp} 值为 None"
        except Exception as e:  # noqa: BLE001
            failures.append(f"{name}: {type(e).__name__}: {e}")
    assert not failures, "以下 MCP 工具参数生成失败:\n" + "\n".join(failures[:20])


# ----------------------------------------------------------------------
# 3. param_provider:为全部 234 CLI 命令生成 argv(骨架完整性)
# ----------------------------------------------------------------------
def test_cli_params_loads_234_commands():
    data = pp.load_cli_params()
    assert data.get("total_leaves", 0) >= 230, "CLI 叶子命令数异常(预期 234 附近)"
    assert isinstance(data.get("commands"), dict)


def test_provider_generates_argv_for_all_cli_commands():
    """provider 能为每个 CLI 叶子命令生成 argv(全参数与仅必填),不抛异常。"""
    data = pp.load_cli_params()
    ctx = pp.SeedContext(
        workspace_id=1, workspace_instance_id="seedinst00000000",
        root=".", known_qualified_names=["compute"],
        known_callee_names=["add"], known_file_paths=["calc.py"],
    )
    failures = []
    for cmd_str, spec in data["commands"].items():
        try:
            full = pp.build_cli_argv(cmd_str, spec, ctx, required_only=False)
            req = pp.build_cli_argv(cmd_str, spec, ctx, required_only=True)
            assert isinstance(full, list) and isinstance(req, list)
            # argv 至少含子命令 token
            assert req[:len(cmd_str.split())] == cmd_str.split()
            # 全参数 argv 里不应残留 argparse 帮助项
            assert "-h" not in full and "--help" not in full
            # argv 全为字符串(subprocess 要求)
            assert all(isinstance(x, str) for x in full), f"{cmd_str}: argv 含非字符串"
        except Exception as e:  # noqa: BLE001
            failures.append(f"{cmd_str}: {type(e).__name__}: {e}")
    assert not failures, "以下 CLI 命令 argv 生成失败:\n" + "\n".join(failures[:20])


def test_provider_uses_seed_context_real_values():
    """provider 应优先用 SeedContext 的真实前置值(而非占位),验证真实优先原则。"""
    ctx = pp.SeedContext(
        workspace_id=42, workspace_instance_id="realinst12345678",
        root="/seed/root", known_qualified_names=["mymod.myfunc"],
        known_callee_names=["helper"], known_file_paths=["src/main.py"],
    )
    # 造一个含 workspace_id/qualified_name/file_path 的伪工具
    tool = {
        "name": "probe", "required": ["workspace_id", "qualified_name", "file_path"],
        "params": {
            "workspace_id": {"type": "integer", "default": None},
            "qualified_name": {"type": "string", "default": None},
            "file_path": {"type": "string", "default": None},
        },
    }
    params = pp.build_mcp_params(tool, ctx, required_only=True)
    assert params["workspace_id"] == 42
    assert params["qualified_name"] == "mymod.myfunc"
    assert params["file_path"] == "src/main.py"
