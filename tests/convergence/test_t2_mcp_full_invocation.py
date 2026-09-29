"""T2:243 个 MCP 工具全参数真实调用(收敛套件正式化)。

通过 create_mcp_server() 装配全部 243 工具,用 param_provider 生成全参数,对
seed_workspace(已 build_graph)真实 call_tool,断言无 DEFECT(方法不存在/
daemon 不可用/代码级 internal_error)。

运行环境:隔离 daemon(seed_workspace fixture)。生产 daemon 持 SID 级 instance
锁时 seed_workspace skip → 本文件测试随之 skip(隔离套件运行约定,见 conftest
的 isolated_http_daemon)。此时用 deliverables 下 _run_t2.py 对生产 daemon 全量
验证(已跑:157 PASS / 72 EXPECTED_BUSINESS / 0 DEFECT(修 test_impact_selection
SQL 缺陷后)/ 14 SKIP)。

MCP 工具经 route_rpc → 全局 HttpDaemonRpcClient 单例。本文件把该单例重定向到
隔离 daemon endpoint(mcp_on_isolated_daemon fixture),使 call_tool 打隔离
daemon 而非生产 daemon。
"""
from __future__ import annotations

import asyncio

import pytest

import param_provider as pp
import t2_mcp_runner as runner


@pytest.fixture(scope="module")
def mcp_on_isolated_daemon(seed_workspace):
    """把全局 HttpDaemonRpcClient 单例重定向到隔离 daemon,并装配 MCP server。

    MCP 工具壳内部经 route_rpc → HttpDaemonRpcClient.get_instance(),默认指向
    生产 daemon(发现 manifest)。此处替换单例为隔离 daemon 的 client(seed_workspace
    已在其上 build_graph),使 call_tool 全量打隔离 daemon。teardown 恢复单例。
    """
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    from callwarden.server.mcp_server import create_mcp_server

    seed_client = seed_workspace["client"]
    saved = HttpDaemonRpcClient._instance
    # 用 seed_workspace 的隔离 client 作为全局单例,并绑定种子 root
    seed_client.configure_workspace(seed_workspace["root"])
    HttpDaemonRpcClient._instance = seed_client
    try:
        mcp = create_mcp_server()  # 内部会 configure 到 PROJECT_ROOT
        seed_client.configure_workspace(seed_workspace["root"])  # 重新指回种子
        yield {"mcp": mcp, "ctx": seed_workspace["ctx"]}
    finally:
        HttpDaemonRpcClient._instance = saved


def test_mcp_server_assembles_243_tools(mcp_on_isolated_daemon):
    mcp = mcp_on_isolated_daemon["mcp"]
    tools = mcp._tool_manager.list_tools()
    assert len(tools) >= 240, f"MCP 工具数异常: {len(tools)}(预期 243)"


def test_all_mcp_tools_no_defect(mcp_on_isolated_daemon):
    """全量真实调用 243 工具,断言无 DEFECT(真实缺陷)。

    分层:READ_ONLY 全调;PROTECTED_MUTATION 非破坏性全调,破坏性 SKIP;
    GOVERNANCE_WRITE 需 lease/状态机前置,SKIP。分类见 t2_mcp_runner。
    """
    mcp = mcp_on_isolated_daemon["mcp"]
    ctx = mcp_on_isolated_daemon["ctx"]

    report = asyncio.run(runner.run_all(mcp, ctx, op_filter="ALL"))
    counts = report["counts"]

    # 全覆盖:PASS + EXPECTED_BUSINESS + DEFECT + SKIP == 243
    total = sum(counts.values())
    assert total >= 240, f"覆盖工具数异常: {total}"

    # 核心断言:无真实缺陷
    defects = [r for r in report["results"] if r["verdict"] == "DEFECT"]
    assert not defects, "发现真实缺陷:\n" + "\n".join(
        f"  {r['name']} [{r['op']}]: {r['detail']}" for r in defects
    )

    # 至少大部分只读工具 PASS(证明种子数据真实可查,不是全业务拒绝)
    assert counts["PASS"] >= 100, f"PASS 数偏低({counts['PASS']}),疑似种子数据未就绪"


def test_provider_full_params_cover_required(seed_workspace):
    """全参数模式必须覆盖每个工具的 required 参数(provider 完整性回归)。"""
    ctx = seed_workspace["ctx"]
    schema = pp.load_mcp_schema()
    missing = []
    for tool in schema["tools"]:
        full = pp.build_mcp_params(tool, ctx, required_only=False)
        for rp in tool.get("required") or []:
            if rp not in full:
                missing.append(f"{tool['name']}.{rp}")
    assert not missing, "全参数模式漏必填参数:\n" + "\n".join(missing[:20])
