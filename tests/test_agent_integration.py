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


def test_propose_symbol_id_patch_updates_exact_symbol():
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)

    target = os.path.join(tmpdir, "sample.py")
    with open(target, "w", encoding="utf-8") as f:
        f.write("def hello():\n    return 'hi'\n\ndef keep():\n    return 'stay'\n")

    db.build_full_graph(force=True)
    sym = db.get_symbol_by_name_and_file("hello", "sample.py")
    assert sym is not None
    row = db.conn.execute(
        "SELECT id, symbol_hash FROM symbols WHERE qualified_name = ?",
        (sym["qualified_name"],),
    ).fetchone()
    assert row is not None

    preview = db.propose_symbol_id_patch(
        row["id"],
        "def hello():\n    return 'hello'",
        dry_run=True,
        expected_symbol_hash=row["symbol_hash"],
    )
    assert preview["success"] is True
    assert preview["status"] == "preview"
    assert preview["patch_scope"]["symbol_id"] == row["id"]
    assert preview["guardrail"]["decision"] in ("pass", "warn")

    mismatch = db.propose_symbol_id_patch(
        row["id"],
        "def hello():\n    return 'bad'",
        dry_run=True,
        expected_symbol_hash="bad-hash",
    )
    assert mismatch["success"] is False

    applied = db.propose_symbol_id_patch(
        row["id"],
        "def hello():\n    return 'hello'",
        expected_symbol_hash=row["symbol_hash"],
    )
    assert applied["success"] is True
    assert applied["status"] == "applied"

    with open(target, "r", encoding="utf-8") as f:
        assert f.read() == "def hello():\n    return 'hello'\n\ndef keep():\n    return 'stay'\n"


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


def test_work_next_job_includes_symbol_id_when_target_symbol_exists():
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)

    target = os.path.join(tmpdir, "sample.py")
    with open(target, "w", encoding="utf-8") as f:
        f.write("def hello():\n    return 'hi'\n")

    db.build_full_graph(force=True)
    sym = db.get_symbol_by_name_and_file("hello", "sample.py")
    assert sym is not None

    task_id = db.task_create(
        "symbol job",
        steps=[{
            "action": "edit",
            "target_file": "sample.py",
            "target_symbol": sym["qualified_name"],
        }],
    )

    job = db.work_next_job(task_id)
    assert job is not None
    assert job["context"]["target_symbol"]["symbol_id"] > 0
    assert job["allowed_edit_scope"]["symbol_id"] == job["context"]["target_symbol"]["symbol_id"]


def test_new_mcp_tools_registered():
    import asyncio

    mcp = create_mcp_server()
    tools = asyncio.run(mcp.list_tools())
    names = {tool.name for tool in tools}
    assert "work_next_job" in names
    assert "propose_range_patch" in names
    assert "propose_symbol_patch" in names
    assert "propose_symbol_id_patch" in names
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
    import callwarden.server._mcp_common as mcp_common_mod

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
        # monkey-patch _db_instance 让 MCP 工具使用我们的临时 db
        # 注意：拆分后工具函数从 _mcp_common 导入 get_db，patch mcp_server.get_db 不再生效，
        # 必须 patch _mcp_common._db_instance（get_db 函数体内读取该全局）。
        orig_db_instance = mcp_common_mod._db_instance
        mcp_common_mod._db_instance = db
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
            mcp_common_mod._db_instance = orig_db_instance
    finally:
        db.close()


def test_task_completion_review_mcp_end_to_end():
    """task_completion_review MCP 工具端到端（空数据库 pass）"""
    import asyncio
    import callwarden.server._mcp_common as mcp_common_mod

    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    try:
        task_id = db.task_create("review-test", steps=[{"action": "edit"}])

        mcp = create_mcp_server()
        orig_db_instance = mcp_common_mod._db_instance
        mcp_common_mod._db_instance = db
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
            mcp_common_mod._db_instance = orig_db_instance
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
    assert os.path.isfile(os.path.join(out_dir, "claude-code", "settings.snippet.json"))
    assert os.path.isfile(os.path.join(out_dir, "cursor", "callwarden.mdc"))


# ============================================
# Agent Rule Memory 注入集成测试
# ============================================


def _setup_rules_for_injection(db):
    """辅助：在 db 中创建两条 active 规则（python+edit 作用域规则 + 全局规则）

    返回 (rule_id_scoped, rule_id_global)。
    """
    cid_scoped = db.rule_candidate_create(
        title="i18n-rule",
        rule_text="用户可见输出必须走 i18n key，不要硬编码",
        scope={"languages": ["python"], "actions": ["edit"]},
        severity="warning",
    )
    rid_scoped = db.rule_candidate_accept(cid_scoped, reviewer="tester")

    cid_global = db.rule_candidate_create(
        title="global-rule",
        rule_text="所有任务都适用的全局规则",
        scope={},
        severity="info",
    )
    rid_global = db.rule_candidate_accept(cid_global, reviewer="tester")
    return rid_scoped, rid_global


def test_task_next_step_returns_applicable_rules_when_rule_accepted():
    """task_next_step 应在返回值中注入 applicable_rules（accepted 规则）"""
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    try:
        _setup_rules_for_injection(db)

        task_id = db.task_create(
            "rule-inject-test",
            steps=[{
                "action": "edit",
                "target_file": "cli/main.py",
                "target_symbol": "cli.main.handle",
                "check_items": "modify handle",
            }],
        )

        step = db.task_next_step(task_id)
        assert step is not None
        assert "applicable_rules" in step, "task_next_step 必须返回 applicable_rules"

        titles = [r["title"] for r in step["applicable_rules"]]
        assert "i18n-rule" in titles, "python+edit 作用域规则应被匹配"
        assert "global-rule" in titles, "全局规则应被匹配"

        # i18n-rule 应排在 global-rule 前面（warning > info）
        scoped = next(r for r in step["applicable_rules"] if r["title"] == "i18n-rule")
        assert scoped["severity"] == "warning"
        assert "language:python" in scoped["matched_scope"]
        assert "action:edit" in scoped["matched_scope"]

        # 全局规则的 matched_scope 应是 ['global']
        global_r = next(r for r in step["applicable_rules"] if r["title"] == "global-rule")
        assert global_r["matched_scope"] == ["global"]
    finally:
        db.close()


def test_task_next_step_applicable_rules_empty_when_no_active_rules():
    """没有 active 规则时 applicable_rules 应为空列表（不是 None）"""
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    try:
        # 只有 pending 候选规则，没有 accept
        db.rule_candidate_create("pending-rule", "should not inject")

        task_id = db.task_create(
            "no-rule-test",
            steps=[{"action": "edit", "target_file": "x.py"}],
        )
        step = db.task_next_step(task_id)
        assert step is not None
        assert step["applicable_rules"] == [], "pending 规则不应被注入"
    finally:
        db.close()


def test_task_next_step_structured_instruction_contains_project_rules():
    """structured_instruction.project_rules 应包含规则摘要"""
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    try:
        _setup_rules_for_injection(db)

        task_id = db.task_create(
            "si-test",
            steps=[{
                "action": "edit",
                "target_file": "cli/main.py",
                "check_items": "x",
            }],
        )
        step = db.task_next_step(task_id)
        assert step is not None

        si = step.get("structured_instruction") or {}
        assert "project_rules" in si, "structured_instruction 必须包含 project_rules"
        assert len(si["project_rules"]) >= 2

        # project_rules 只返回摘要字段（id/title/severity）
        for r in si["project_rules"]:
            assert "id" in r
            assert "title" in r
            assert "severity" in r
            assert "rule_text" not in r, "摘要不应包含 rule_text"
    finally:
        db.close()


def test_work_next_job_returns_project_rules_when_rule_accepted():
    """work_next_job 应在 job 顶层返回 project_rules，并在 context 返回摘要"""
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    try:
        _setup_rules_for_injection(db)

        task_id = db.task_create(
            "work-rule-test",
            steps=[{
                "action": "edit",
                "target_file": "cli/main.py",
                "check_items": "x",
            }],
        )

        job = db.work_next_job(task_id)
        assert job is not None

        # 顶层 project_rules
        assert "project_rules" in job, "work_next_job 必须返回 project_rules"
        assert len(job["project_rules"]) >= 2

        # context.applicable_rules 是精简摘要
        assert "applicable_rules" in job["context"], "context 必须有 applicable_rules"
        assert len(job["context"]["applicable_rules"]) >= 2

        # 摘要字段只含 id/title/severity
        for r in job["context"]["applicable_rules"]:
            assert set(r.keys()) == {"id", "title", "severity"}
    finally:
        db.close()


def test_rule_injection_fail_soft_on_missing_table():
    """fail-soft：agent_rules 表异常时注入降级为空列表，不阻塞任务领取"""
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    try:
        _setup_rules_for_injection(db)

        # 模拟表损坏：DROP agent_rules 表
        db.conn.execute("DROP TABLE agent_rules")
        db.conn.commit()

        # 创建新任务（之前的 step 已被领取过）
        task_id = db.task_create(
            "failsoft-test",
            steps=[{"action": "edit", "target_file": "cli/main.py"}],
        )
        # task_next_step 应正常返回，applicable_rules 是空列表
        step = db.task_next_step(task_id)
        assert step is not None, "fail-soft 时任务仍应正常领取"
        assert step["applicable_rules"] == [], "fail-soft 时 applicable_rules 应为空列表"

        # structured_instruction.project_rules 也应为空
        si = step.get("structured_instruction") or {}
        assert si.get("project_rules", []) == []
    finally:
        db.close()


def test_rule_injection_filters_by_action_scope():
    """规则按 action 作用域过滤：edit 规则不应在 annotate 步骤注入"""
    tmpdir = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(tmpdir, "test.db"), workspace_root=tmpdir)
    try:
        # 只在 edit 动作下生效的规则
        cid = db.rule_candidate_create(
            title="edit-only-rule",
            rule_text="only for edit",
            scope={"actions": ["edit"]},
            severity="warning",
        )
        db.rule_candidate_accept(cid)

        # edit 步骤应匹配
        tid_edit = db.task_create(
            "edit-test",
            steps=[{"action": "edit", "target_file": "x.py"}],
        )
        step_edit = db.task_next_step(tid_edit)
        assert step_edit is not None
        titles = [r["title"] for r in step_edit["applicable_rules"]]
        assert "edit-only-rule" in titles

        # annotate 步骤不应匹配
        tid_anno = db.task_create(
            "annotate-test",
            steps=[{"action": "annotate_function", "target_file": "x.py"}],
        )
        step_anno = db.task_next_step(tid_anno)
        assert step_anno is not None
        titles_anno = [r["title"] for r in step_anno["applicable_rules"]]
        assert "edit-only-rule" not in titles_anno, "edit-only 规则不应在 annotate 步骤注入"
    finally:
        db.close()
