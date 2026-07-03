"""P2 功能验证脚本

验证四个 P2 级别功能：
- F2: RAG 管道 ask_codebase（补齐 P0 RAG 缺失）
- F3: Token 节省账本（Schema v10→v11）
- F4: 分支感知图谱（独立工作区方案）
- F5: 安全文件编辑 propose_edit（Agent OS 核心，Schema v11→v12）

运行方式:
    cd c:\\git_work\\callwarden\\scripts
    cw test test_p2_features
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db import CodeGraphDB


def test_f2_ask_codebase():
    """F2: RAG 管道 ask_codebase（跳过网络依赖，只验证方法存在性）"""
    print("--- F2: RAG 管道 ask_codebase ---")
    from callwarden.db import CodeGraphDB
    assert hasattr(CodeGraphDB, "ask_codebase"), "ask_codebase 方法不存在"
    print("PASS F2.1: ask_codebase 方法存在")

    # 验证辅助方法存在
    assert hasattr(CodeGraphDB, "_keyword_fallback_search"), "_keyword_fallback_search 方法不存在"
    assert hasattr(CodeGraphDB, "_build_rag_block"), "_build_rag_block 方法不存在"
    assert hasattr(CodeGraphDB, "_format_rag_context"), "_format_rag_context 方法不存在"
    print("PASS F2.2: 3 个辅助方法都存在（keyword_fallback / build_rag_block / format_rag_context）")
    print("PASS F2: RAG 管道方法验证完成（跳过网络测试）\n")


def test_f3_token_savings():
    """F3: Token 节省账本"""
    print("--- F3: Token 节省账本 ---")
    from callwarden.db import CodeGraphDB
    assert hasattr(CodeGraphDB, "record_token_savings"), "record_token_savings 方法不存在"
    assert hasattr(CodeGraphDB, "get_token_savings_report"), "get_token_savings_report 方法不存在"
    print("PASS F3.1: record_token_savings / get_token_savings_report 方法存在")

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    db = CodeGraphDB(db_path)
    db.register_workspace("test-ws", os.getcwd())

    # 记录一次节省
    result = db.record_token_savings(
        operation="rag_context",
        original_tokens=8000,
        actual_tokens=1000,
        agent_task_id="test-task",
        detail={"symbols": 5, "files": 3},
    )
    assert result["tokens_saved"] == 7000, f"tokens_saved 应为 7000，实际 {result['tokens_saved']}"
    assert result["savings_pct"] == 87.5, f"savings_pct 应为 87.5，实际 {result['savings_pct']}"
    print(f"PASS F3.2: record_token_savings 记录成功（saved={result['tokens_saved']}, pct={result['savings_pct']}%）")

    # 获取报告
    report = db.get_token_savings_report(time_window="30d")
    assert report["total_saved"] == 7000, f"total_saved 应为 7000，实际 {report['total_saved']}"
    assert report["total_operations"] == 1, f"total_operations 应为 1，实际 {report['total_operations']}"
    assert "rag_context" in report["by_operation"], "by_operation 应包含 rag_context"
    assert "headline" in report, "应包含 headline 字段"
    print(f"PASS F3.3: get_token_savings_report 返回正确（headline={report['headline']}）")
    print("PASS F3: Token 节省账本完成\n")


def test_f4_branch_aware():
    """F4: 分支感知图谱"""
    print("--- F4: 分支感知图谱 ---")
    from callwarden.db import CodeGraphDB
    assert hasattr(CodeGraphDB, "register_branch_workspace"), "register_branch_workspace 方法不存在"
    assert hasattr(CodeGraphDB, "list_branch_workspaces"), "list_branch_workspaces 方法不存在"
    assert hasattr(CodeGraphDB, "diff_branches"), "diff_branches 方法不存在"
    assert hasattr(CodeGraphDB, "switch_branch_context"), "switch_branch_context 方法不存在"
    assert hasattr(CodeGraphDB, "merge_preview"), "merge_preview 方法不存在"
    print("PASS F4.1: 5 个分支方法都存在")

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    db = CodeGraphDB(db_path)

    # 注册分支
    result = db.register_branch_workspace("main", os.getcwd())
    assert "workspace_id" in result, "应包含 workspace_id"
    assert result["branch_name"] == "main", f"branch_name 应为 main，实际 {result['branch_name']}"
    print(f"PASS F4.2: register_branch_workspace 成功（workspace_id={result['workspace_id']}）")

    # 列出分支
    branches = db.list_branch_workspaces()
    assert isinstance(branches, list), "应返回列表"
    assert len(branches) >= 1, "应至少有 1 个分支"
    print(f"PASS F4.3: list_branch_workspaces 返回 {len(branches)} 个分支")

    # 注册第二个分支
    result2 = db.register_branch_workspace("feature-x", os.getcwd())
    branches2 = db.list_branch_workspaces()
    assert len(branches2) >= 2, "应至少有 2 个分支"
    print(f"PASS F4.4: 注册第二个分支后共有 {len(branches2)} 个分支")

    # diff_branches（空数据库应返回空差异）
    diff = db.diff_branches("main", "feature-x")
    assert isinstance(diff, dict), "应返回字典"
    assert "added" in diff and "removed" in diff and "modified" in diff, "应包含 added/removed/modified"
    print(f"PASS F4.5: diff_branches 返回正确结构（added={len(diff['added'])}, removed={len(diff['removed'])}）")
    print("PASS F4: 分支感知图谱完成\n")


def test_f5_safe_edit():
    """F5: 安全文件编辑 propose_edit"""
    print("--- F5: 安全文件编辑 propose_edit ---")
    from callwarden.db import CodeGraphDB
    assert hasattr(CodeGraphDB, "propose_edit"), "propose_edit 方法不存在"
    assert hasattr(CodeGraphDB, "revert_edit"), "revert_edit 方法不存在"
    assert hasattr(CodeGraphDB, "get_edit_history"), "get_edit_history 方法不存在"
    assert hasattr(CodeGraphDB, "get_edit_stats"), "get_edit_stats 方法不存在"
    print("PASS F5.1: 4 个安全编辑方法都存在")

    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    db = CodeGraphDB(db_path)
    db.register_workspace("test-ws", os.getcwd())

    # 创建测试文件
    test_file = os.path.join(tmpdir, "test_edit.txt")
    with open(test_file, "w", encoding="utf-8") as f:
        f.write("line 1\nline 2\nline 3\n")

    # dry_run 测试
    result = db.propose_edit(
        file_path=test_file,
        new_content="line 1\nline 2\nline 3\nline 4\n",
        operation="edit",
        dry_run=True,
    )
    assert result["status"] == "preview", f"dry_run 应返回 preview，实际 {result['status']}"
    assert "file_hash_before" in result, "应包含 file_hash_before"
    assert "file_hash_after" in result, "应包含 file_hash_after"
    assert "diff_summary" in result, "应包含 diff_summary"
    print(f"PASS F5.2: dry_run 返回 preview（diff_summary={result['diff_summary']}）")

    # 实际编辑
    result2 = db.propose_edit(
        file_path=test_file,
        new_content="line 1\nline 2\nline 3\nline 4\nline 5\n",
        operation="edit",
        dry_run=False,
    )
    assert result2["status"] == "applied", f"实际编辑应返回 applied，实际 {result2['status']}"
    assert result2["success"] == True, "应成功"
    print(f"PASS F5.3: 实际编辑成功（status=applied, audit_id={result2.get('audit_id')}）")

    # 验证文件内容已更新
    with open(test_file, "r", encoding="utf-8") as f:
        content = f.read()
    assert "line 5" in content, "文件内容应包含 line 5"
    print("PASS F5.4: 文件内容已正确更新")

    # 查询编辑历史
    history = db.get_edit_history(file_path=test_file)
    assert isinstance(history, list), "应返回列表"
    print(f"PASS F5.5: get_edit_history 返回 {len(history)} 条记录")

    # 查询统计
    stats = db.get_edit_stats(time_window="30d")
    assert isinstance(stats, dict), "应返回字典"
    print(f"PASS F5.6: get_edit_stats 返回正确结构")
    print("PASS F5: 安全文件编辑完成\n")


def test_mcp_total_tools():
    """验证 MCP 工具总数"""
    print("--- MCP 工具总数验证 ---")
    import asyncio
    from callwarden.server.mcp_server import create_mcp_server

    mcp = create_mcp_server()
    tools = asyncio.run(mcp.list_tools())
    tool_names = {t.name for t in tools}

    # 验证 P2 新增的 11 个工具
    p2_tools = [
        "ask_codebase",                    # F2
        "record_token_savings",            # F3
        "get_token_savings_report",        # F3
        "register_branch",                 # F4
        "list_branches",                   # F4
        "diff_branches",                   # F4
        "switch_branch",                   # F4
        "merge_preview",                   # F4
        "propose_edit",                    # F5
        "revert_edit",                     # F5
        "get_edit_history",                # F5
        "get_edit_stats",                  # F5
    ]
    for name in p2_tools:
        assert name in tool_names, f"MCP 工具 {name} 未注册"
        print(f"  ✓ {name}")

    print(f"PASS: MCP 工具总数 = {len(tools)}（>= 108）")
    assert len(tools) >= 108, f"工具数应 >= 108，实际 {len(tools)}"
    print("PASS: MCP 工具验证完成\n")


def main():
    print("=" * 60)
    print("P2 功能验证")
    print("=" * 60)
    print()
    test_f2_ask_codebase()
    test_f3_token_savings()
    test_f4_branch_aware()
    test_f5_safe_edit()
    test_mcp_total_tools()
    print("=" * 60)
    print("=== ALL P2 TESTS PASSED ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
