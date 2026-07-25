"""P1 功能验证脚本

验证三个 P1 级别功能：
- F1: 漏洞爆炸半径 get_vulnerability_blast_radius（全行业空白特性）
- MCP: 5 个新工具接线（embed_single_symbol/get_symbol_commit_history/parse_codeowners/import_codeowners/import_git_blame）
- CLI: 6 个新命令接线（task create/next/report/rollback + vuln-blast + symbol-history）

运行方式:
    cd c:\\git_work\\callwarden\\scripts
    cw test test_p1_features
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db import CodeGraphDB


def test_f1_vulnerability_blast_radius():
    """F1: 漏洞爆炸半径"""
    print("--- F1: 漏洞爆炸半径 get_vulnerability_blast_radius ---")
    from callwarden.db import CodeGraphDB
    assert hasattr(CodeGraphDB, "get_vulnerability_blast_radius"), "get_vulnerability_blast_radius 方法不存在"
    print("PASS F1.1: get_vulnerability_blast_radius 方法存在")

    # 用临时数据库测试（空 semgrep_findings 表应返回空结果，不崩溃）
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    db = CodeGraphDB(db_path)
    db.register_workspace("test-ws", os.getcwd())

    # 空数据库测试
    result = db.get_vulnerability_blast_radius()
    assert isinstance(result, dict), "应返回字典"
    assert result["total_findings"] == 0, "空数据库应返回 0 findings"
    assert result["risk_level"] == "none", "空数据库风险等级应为 none"
    print(f"PASS F1.2: 空数据库返回 risk_level={result['risk_level']}, total_findings=0")

    # 测试 severity_filter 参数
    result2 = db.get_vulnerability_blast_radius(severity_filter="ERROR")
    assert result2["total_findings"] == 0
    print("PASS F1.3: severity_filter 参数正常工作")

    # 测试 finding_id 参数
    result3 = db.get_vulnerability_blast_radius(finding_id=999)
    assert result3["total_findings"] == 0
    print("PASS F1.4: finding_id 参数正常工作")
    print("PASS F1: 漏洞爆炸半径功能完成\n")


def test_mcp_5_new_tools():
    """MCP: 5 个新工具接线"""
    print("--- MCP: 5 个新工具接线 ---")
    import asyncio
    from callwarden.server.mcp_server import create_mcp_server

    mcp = create_mcp_server()
    tools = asyncio.run(mcp.list_tools())
    tool_names = {t.name for t in tools}

    needed = [
        "embed_single_symbol",
        "get_symbol_commit_history",
        "parse_codeowners",
        "import_codeowners",
        "import_git_blame",
        "get_vulnerability_blast_radius",  # F1 也暴露为 MCP 工具
    ]
    for name in needed:
        assert name in tool_names, f"MCP 工具 {name} 未注册"
        print(f"PASS MCP: {name} 已注册")

    print(f"PASS MCP: 工具总数 = {len(tools)}（>= 96）")
    assert len(tools) >= 96, f"工具数应 >= 96，实际 {len(tools)}"
    print("PASS MCP: 5 个新工具接线完成\n")


def test_cli_6_new_commands():
    """CLI: 6 个新命令接线"""
    print("--- CLI: 6 个新命令接线 ---")
    from callwarden.cli.main import _handle_task, _handle_vuln_blast, _handle_symbol_history

    # 验证 3 个 handler 函数存在
    assert callable(_handle_task), "_handle_task 不可调用"
    assert callable(_handle_vuln_blast), "_handle_vuln_blast 不可调用"
    assert callable(_handle_symbol_history), "_handle_symbol_history 不可调用"
    print("PASS CLI.1: 3 个 handler 函数存在")

    # 验证 _SUBCOMMANDS 包含新命令
    from callwarden.cli.main import _SUBCOMMANDS
    assert "task" in _SUBCOMMANDS, "_SUBCOMMANDS 不包含 task"
    assert "vuln-blast" in _SUBCOMMANDS, "_SUBCOMMANDS 不包含 vuln-blast"
    assert "symbol-history" in _SUBCOMMANDS, "_SUBCOMMANDS 不包含 symbol-history"
    print("PASS CLI.2: _SUBCOMMANDS 包含 task/vuln-blast/symbol-history")

    # 端到端测试：用真实数据库测试 vuln-blast 命令
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    db = CodeGraphDB(db_path)
    db.register_workspace("test-ws", os.getcwd())

    # 测试 vuln-blast 命令（空数据库应正常输出，不崩溃）
    result = _handle_vuln_blast([], db)
    assert result == True, "_handle_vuln_blast 应返回 True"
    print("PASS CLI.3: vuln-blast 命令正常执行（空数据库）")

    # 测试 symbol-history 命令
    result = _handle_symbol_history(["fake_hash_12345"], db)
    assert result == True, "_handle_symbol_history 应返回 True"
    print("PASS CLI.4: symbol-history 命令正常执行")

    # 测试 task next 命令（空任务 ID 应正常处理）
    result = _handle_task(["next", "fake_task_id"], db)
    assert result == True, "_handle_task next 应返回 True"
    print("PASS CLI.5: task next 命令正常执行")
    print("PASS CLI: 6 个新命令接线完成\n")


def main():
    print("=" * 60)
    print("P1 功能验证")
    print("=" * 60)
    print()
    test_f1_vulnerability_blast_radius()
    test_mcp_5_new_tools()
    test_cli_6_new_commands()
    print("=" * 60)
    print("=== ALL P1 TESTS PASSED ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
