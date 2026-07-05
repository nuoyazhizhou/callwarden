"""Agent 集成闭环测试

覆盖 work_next_job、范围补丁、符号补丁工具注册和 install-agent 生成器。
"""

import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db import CodeGraphDB
from callwarden.server.mcp_server import create_mcp_server


def test_propose_range_patch_updates_only_target_lines():
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)

    target = os.path.join(tmpdir, "sample.py")
    with open(target, "w", encoding="utf-8") as f:
        f.write("line1\nline2\nline3\n")

    preview = db.propose_range_patch(
        file_path=target,
        start_line=2,
        end_line=2,
        replacement="changed",
        dry_run=True,
    )
    assert preview["success"] is True
    assert preview["status"] == "preview"

    applied = db.propose_range_patch(
        file_path=target,
        start_line=2,
        end_line=2,
        replacement="changed",
    )
    assert applied["success"] is True
    assert applied["status"] == "applied"

    with open(target, "r", encoding="utf-8") as f:
        assert f.read() == "line1\nchanged\nline3\n"


def test_work_next_job_returns_agent_context():
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    task_id = db.task_create(
        "小任务",
        steps=[{"action": "edit", "target_file": "sample.py", "check_items": ["只改一处"]}],
    )

    job = db.work_next_job(task_id)
    assert job is not None
    assert job["job_type"] == "edit"
    assert job["target_file"] == "sample.py"
    assert job["allowed_edit_scope"]["preferred_tool"] == "propose_range_patch"
    assert "propose_range_patch" in job["recommended_tools"]
    assert "report_with" in job


def test_new_mcp_tools_registered():
    import asyncio

    mcp = create_mcp_server()
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "work_next_job" in names
    assert "propose_range_patch" in names
    assert "propose_symbol_patch" in names
    assert "record_task_symbol_change" in names
    assert "link_edit_audit_symbols" in names
    assert "get_task_symbol_changes" in names
    assert "get_symbol_change_tasks" in names
    assert "get_project_dependencies" in names
    assert "import_project_dependencies" in names
    assert "prune_external_symbols" in names
    assert "gc_retention" in names
    assert "gc_policy_get" in names
    assert "gc_policy_set" in names
    assert "gc_archive_list" in names
    assert "gc_archive_inspect" in names
    assert "gc_audit_list" in names
    assert "gc_audit_get" in names
    assert "gc_archive_import" in names


def test_task_quality_gate_mcp_tools_registered():
    """任务质量门禁 3 个 MCP 工具已注册"""
    import asyncio

    mcp = create_mcp_server()
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "task_completion_review" in names, "task_completion_review 未注册"
    assert "task_quality_findings" in names, "task_quality_findings 未注册"
    assert "task_resolve_quality_finding" in names, "task_resolve_quality_finding 未注册"


def test_task_quality_findings_mcp_end_to_end():
    """task_quality_findings / task_resolve_quality_finding 端到端"""
    import asyncio
    import callwarden.server.mcp_server as mcp_mod

    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    try:
        task_id = db.task_create("quality-gate-test", steps=[{"action": "edit"}])

        fid = db.record_task_quality_finding(
            task_id, severity="warn", message="test-warn",
        )
        db.record_task_quality_finding(
            task_id, severity="error", message="test-error",
        )

        mcp = create_mcp_server()
        # monkey-patch get_db 让 MCP 工具使用我们的临时 db
        orig_get_db = mcp_mod.get_db
        mcp_mod.get_db = lambda workspace=None: db
        try:
            # 调用 task_quality_findings 工具（返回 list[dict]）
            open_result = asyncio.run(
                mcp.call_tool("task_quality_findings",
                              {"task_id": task_id, "status": "open", "severity": ""})
            )
            open_findings = _extract_tool_payload_list(open_result)
            assert len(open_findings) == 2
            messages = {f["message"] for f in open_findings}
            assert "test-warn" in messages
            assert "test-error" in messages

            # severity 过滤
            error_result = asyncio.run(
                mcp.call_tool("task_quality_findings",
                              {"task_id": task_id, "status": "open", "severity": "error"})
            )
            error_only = _extract_tool_payload_list(error_result)
            assert len(error_only) == 1
            assert error_only[0]["message"] == "test-error"

            # 解决 finding（返回单个 dict）
            resolve_result = asyncio.run(
                mcp.call_tool("task_resolve_quality_finding",
                              {"finding_id": fid, "resolution": "fixed", "resolved_by": "agent"})
            )
            resolved = _extract_tool_payload(resolve_result)
            assert resolved["success"] is True
            assert resolved["status"] == "resolved"

            # resolved 过滤
            resolved_list = _extract_tool_payload_list(asyncio.run(
                mcp.call_tool("task_quality_findings",
                              {"task_id": task_id, "status": "resolved", "severity": ""})
            ))
            assert len(resolved_list) == 1
            assert resolved_list[0]["message"] == "test-warn"

            # 剩余 open
            open_after = _extract_tool_payload_list(asyncio.run(
                mcp.call_tool("task_quality_findings",
                              {"task_id": task_id, "status": "open", "severity": ""})
            ))
            assert len(open_after) == 1
        finally:
            mcp_mod.get_db = orig_get_db
    finally:
        db.close()


def test_task_completion_review_mcp_end_to_end():
    """task_completion_review MCP 工具端到端（空数据库 pass）"""
    import asyncio
    import callwarden.server.mcp_server as mcp_mod

    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    try:
        task_id = db.task_create("review-test", steps=[{"action": "edit"}])

        mcp = create_mcp_server()
        orig_get_db = mcp_mod.get_db
        mcp_mod.get_db = lambda workspace=None: db
        try:
            result = _extract_tool_payload(asyncio.run(
                mcp.call_tool("task_completion_review",
                              {"task_id": task_id, "step_id": ""})
            ))
            # 无变更文件 → pass（无发现）
            assert result["decision"] == "pass"
            assert isinstance(result["findings"], list)
            assert len(result["findings"]) == 0
            assert "counts" in result
            assert result["counts"]["error"] == 0
        finally:
            mcp_mod.get_db = orig_get_db
    finally:
        db.close()


def _extract_tool_payload_list(tool_result):
    """从 FastMCP call_tool 返回值中提取业务 payload（始终返回 list）

    FastMCP 1.x 返回 list[TextContent]，每个 TextContent.text 是 JSON 字符串。
    本辅助函数解析所有 TextContent 并返回解析后的 list。

    Args:
        tool_result: FastMCP call_tool 返回值

    Returns:
        list：每个 TextContent 解析为 dict/list/str
    """
    import json

    if isinstance(tool_result, list):
        items = []
        for c in tool_result:
            if hasattr(c, "text"):
                try:
                    items.append(json.loads(c.text))
                except Exception:
                    items.append(c.text)
            elif isinstance(c, (dict, list)):
                items.append(c)
        return items
    return [tool_result]


def _extract_tool_payload(tool_result):
    """从 FastMCP call_tool 返回值中提取业务 payload（自动判断单值/列表）

    FastMCP 总是返回 list[TextContent]，无法区分底层返回的是单个 dict 还是 list[dict]。
    本辅助函数解析所有 TextContent：
    - 若仅 1 个元素，返回该元素（适合返回单个 dict 的工具）
    - 若多个元素，返回 list（适合返回 list 的工具）

    详见 _extract_tool_payload_list。
    """
    items = _extract_tool_payload_list(tool_result)
    if len(items) == 1:
        return items[0]
    return items


def test_install_agent_generates_templates():
    tmpdir = tempfile.mkdtemp()
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(tmpdir, "integrations")
    env = {
        **os.environ,
        "CALLWARDEN_LANG": "en_US",
        "PYTHONIOENCODING": "utf-8",
    }
    result = subprocess.run(
        [sys.executable, os.path.join(repo, "cw.py"), "install-agent", "all", "--output-dir", out_dir, "--force"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert os.path.isfile(os.path.join(out_dir, "codex", "callwarden-plugin", ".codex-plugin", "plugin.json"))
    assert os.path.isfile(os.path.join(out_dir, "claude", "settings.snippet.json"))
    assert os.path.isfile(os.path.join(out_dir, "cursor", "callwarden.mdc"))
