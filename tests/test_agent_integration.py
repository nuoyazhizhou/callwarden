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
