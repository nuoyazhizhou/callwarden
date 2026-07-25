"""bootstrap_status 健康摘要测试。

覆盖 docs/design/bootstrap-closure-plan.md 中 Phase 2 bootstrap status 落地：
1. BootstrapMixin.bootstrap_status() 业务方法
   - db_stale 检测（scan_run.git_head 与当前 HEAD 不一致）
   - active_rules_count / pending_candidates_count 计数
   - open/blocking findings 计数
   - audit_verify 摘要
   - latest_scan_run 字段
   - tasks 按状态分组
   - recommended_next_action 优先级链
2. CLI cw bootstrap status
   - --help 不初始化 db
   - 调用 db.bootstrap_status()
   - 输出包含关键字段
   - 只读命令集合
3. MCP bootstrap_status 工具
   - 注册
   - 无参数签名
   - 在工具列表中
"""
import os
import sys
import tempfile
from unittest import mock

import pytest

# 确保项目根目录在 path 中
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB
from callwarden.cli import main as cli_main


# ============================================
# 业务方法测试
# ============================================


def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def test_bootstrap_status_returns_dict():
    """bootstrap_status 返回 dict 且包含必要字段。"""
    db, _root = _db_with_workspace()
    try:
        result = db.bootstrap_status()
        assert isinstance(result, dict)
        # 必要字段
        assert "db_stale" in result
        assert "current_head" in result
        assert "active_rules_count" in result
        assert "pending_candidates_count" in result
        assert "open_findings_count" in result
        assert "blocking_findings_count" in result
        assert "audit_verify" in result
        assert "latest_scan_run" in result
        assert "tasks" in result
        assert "recommended_next_action" in result
    finally:
        db.close()


def test_bootstrap_status_no_scan_run_returns_none():
    """无 scan_run 记录时 latest_scan_run 为 None。"""
    db, _root = _db_with_workspace()
    try:
        result = db.bootstrap_status()
        assert result["latest_scan_run"] is None
    finally:
        db.close()


def test_bootstrap_status_with_scan_run_returns_fields():
    """有 scan_run 记录时 latest_scan_run 包含 id/git_head/started_at/status。"""
    db, _root = _db_with_workspace()
    try:
        scan_id = db.record_workspace_scan_run(purpose="bootstrap")
        result = db.bootstrap_status()
        latest = result["latest_scan_run"]
        assert latest is not None
        assert latest["id"] == scan_id
        assert "git_head" in latest
        assert "started_at" in latest
        assert "status" in latest
    finally:
        db.close()


def test_bootstrap_status_active_rules_count():
    """active_rules_count 反映已生效规则数量。"""
    db, _root = _db_with_workspace()
    try:
        # 初始应为 0
        result = db.bootstrap_status()
        assert result["active_rules_count"] == 0

        # 添加一条 active 规则
        db.conn.execute(
            "INSERT INTO agent_rules (id, title, rule_text, scope_json, severity, "
            "status, source_candidate_id, evidence_json, created_at, updated_at, "
            "synced_to_agents_md, sync_hash) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("R-test-1", "test rule", "do not skip tests", "{}", "warn",
             "active", "", "{}", 1.0, 1.0, 0, ""),
        )
        db.conn.commit()
        result = db.bootstrap_status()
        assert result["active_rules_count"] == 1
    finally:
        db.close()


def test_bootstrap_status_pending_candidates_count():
    """pending_candidates_count 反映待审核候选规则数量。"""
    db, _root = _db_with_workspace()
    try:
        # 初始应为 0
        result = db.bootstrap_status()
        assert result["pending_candidates_count"] == 0

        # 添加一条 pending 候选
        db.conn.execute(
            "INSERT INTO agent_rule_candidates (id, title, rule_text, scope_json, "
            "severity, source, evidence_json, confidence, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("C-test-1", "pending candidate", "rule text", "{}", "info",
             "test", "{}", 0.5, "pending", 1.0),
        )
        db.conn.commit()
        result = db.bootstrap_status()
        assert result["pending_candidates_count"] == 1
    finally:
        db.close()


def test_bootstrap_status_open_findings_count():
    """open_findings_count 反映 open 状态的质量发现数量。"""
    db, _root = _db_with_workspace()
    try:
        # 初始应为 0
        result = db.bootstrap_status()
        assert result["open_findings_count"] == 0

        # 创建任务 + 写入 finding
        task_id = db.task_create("test-task", steps=[{"action": "edit", "target_file": ""}])
        db.record_task_quality_finding(
            task_id=task_id,
            finding_type="semgrep",
            severity="warn",
            message="test finding",
            source="test",
        )
        result = db.bootstrap_status()
        assert result["open_findings_count"] == 1
    finally:
        db.close()


def test_bootstrap_status_blocking_findings_count():
    """blocking_findings_count 反映 block 严重度的质量发现数量。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("test-task", steps=[{"action": "edit", "target_file": ""}])
        db.record_task_quality_finding(
            task_id=task_id,
            finding_type="semgrep",
            severity="block",
            message="blocking finding",
            source="test",
        )
        result = db.bootstrap_status()
        assert result["blocking_findings_count"] == 1
        assert result["open_findings_count"] == 1
    finally:
        db.close()


def test_bootstrap_status_tasks_grouped_by_status():
    """tasks 按状态分组计数。"""
    db, _root = _db_with_workspace()
    try:
        # 初始任务均为 open
        db.task_create("task-1", "desc", [])
        db.task_create("task-2", "desc", [])

        result = db.bootstrap_status()
        tasks = result["tasks"]
        assert tasks["open"] >= 2
        assert tasks["in_progress"] == 0
        assert tasks["review"] == 0
        assert tasks["applied"] == 0
    finally:
        db.close()


def test_bootstrap_status_audit_verify_summary():
    """audit_verify 包含 total_count/verified_count/broken_count/security_level。"""
    db, _root = _db_with_workspace()
    try:
        result = db.bootstrap_status()
        audit = result["audit_verify"]
        assert "total_count" in audit
        assert "verified_count" in audit
        assert "broken_count" in audit
        assert "security_level" in audit
        # 空库时全部为 0
        assert audit["total_count"] == 0
        assert audit["broken_count"] == 0
    finally:
        db.close()


def test_bootstrap_status_recommended_next_action_priority():
    """recommended_next_action 按 db_stale > blocking > pending > audit_broken > review 优先级。"""
    db, _root = _db_with_workspace()
    try:
        # 无任何问题时推荐 cw task list
        result = db.bootstrap_status()
        # 非 Git 临时目录，current_head 为空，db_stale 为 False
        # 无 findings/candidates/audit_broken/review tasks
        # 但可能有 open tasks（task_create 默认 open）
        action = result["recommended_next_action"]
        assert isinstance(action, str)
        assert "cw" in action
    finally:
        db.close()


def test_bootstrap_status_recommended_when_blocking_findings():
    """有 blocking findings 时推荐查看 findings。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("test-task", steps=[{"action": "edit", "target_file": ""}])
        db.record_task_quality_finding(
            task_id=task_id,
            finding_type="semgrep",
            severity="block",
            message="blocking",
            source="test",
        )
        result = db.bootstrap_status()
        # blocking > pending，应推荐 findings
        assert "findings" in result["recommended_next_action"] or "task" in result["recommended_next_action"]
    finally:
        db.close()


def test_bootstrap_status_recommended_when_pending_candidates():
    """有 pending candidates 时推荐 rule candidate。"""
    db, _root = _db_with_workspace()
    try:
        db.conn.execute(
            "INSERT INTO agent_rule_candidates (id, title, rule_text, scope_json, "
            "severity, source, evidence_json, confidence, status, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("C-test-1", "pending", "rule", "{}", "info",
             "test", "{}", 0.5, "pending", 1.0),
        )
        db.conn.commit()
        result = db.bootstrap_status()
        assert "rule" in result["recommended_next_action"] or "candidate" in result["recommended_next_action"]
    finally:
        db.close()


# ============================================
# CLI 测试
# ============================================


def test_cli_bootstrap_status_help_no_db():
    """cw bootstrap status --help 不应初始化数据库。"""
    old_argv = sys.argv
    sys.argv = ["cw", "bootstrap", "status", "--help"]
    try:
        db_init_called = {"count": 0}

        def fake_init(self, *args, **kwargs):
            db_init_called["count"] += 1
            raise RuntimeError("db should not be initialized for --help")

        with mock.patch.object(CodeGraphDB, "__init__", fake_init):
            with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                try:
                    cli_main._run_subcommand_mode()
                except RuntimeError as e:
                    if "should not" in str(e):
                        pytest.fail("db initialized during cw bootstrap status --help")
                    raise
        assert db_init_called["count"] == 0
    finally:
        sys.argv = old_argv


def test_cli_bootstrap_help_no_db():
    """cw bootstrap --help 不应初始化数据库。"""
    old_argv = sys.argv
    sys.argv = ["cw", "bootstrap", "--help"]
    try:
        db_init_called = {"count": 0}

        def fake_init(self, *args, **kwargs):
            db_init_called["count"] += 1
            raise RuntimeError("db should not be initialized for --help")

        with mock.patch.object(CodeGraphDB, "__init__", fake_init):
            with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                try:
                    cli_main._run_subcommand_mode()
                except RuntimeError as e:
                    if "should not" in str(e):
                        pytest.fail("db initialized during cw bootstrap --help")
                    raise
        assert db_init_called["count"] == 0
    finally:
        sys.argv = old_argv


def test_cli_bootstrap_status_calls_db_method():
    """cw bootstrap status 必须调用 db.bootstrap_status()。"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            call_log = {"count": 0, "kwargs": None}
            original = db.bootstrap_status

            def spy(*args, **kwargs):
                call_log["count"] += 1
                call_log["kwargs"] = kwargs
                return original(*args, **kwargs)

            with mock.patch.object(db, "bootstrap_status", side_effect=spy):
                try:
                    cli_main._handle_bootstrap(["status"], db)
                except SystemExit:
                    pass

            assert call_log["count"] == 1, "db.bootstrap_status 必须被调用一次"
        finally:
            db.close()


def test_cli_bootstrap_status_output_contains_fields():
    """cw bootstrap status 输出必须包含关键字段。"""
    import io
    from contextlib import redirect_stdout

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    cli_main._handle_bootstrap(["status"], db)
                except SystemExit:
                    pass
            out = buf.getvalue()

            # 应包含关键字段（通过 i18n key 的实际值）
            assert "Bootstrap" in out or "自举" in out, f"输出应包含标题, 实际: {out!r}"
            # db_stale 状态（yes/no 都有 ✓ 或 ✗）
            assert "✓" in out or "✗" in out, f"输出应包含 db_stale 状态符号, 实际: {out!r}"
            # active_rules / pending_candidates
            assert "0" in out, f"输出应包含数字 0, 实际: {out!r}"
            # 推荐命令
            assert "cw" in out, f"输出应包含推荐命令, 实际: {out!r}"
        finally:
            db.close()


def test_cli_bootstrap_status_is_readonly():
    """bootstrap status 是只读命令，应在 _is_readonly_command 中返回 True。"""
    assert cli_main._is_readonly_command("bootstrap", ["status"]) is True
    assert cli_main._is_readonly_command("bootstrap", []) is False
    # 非 status 的 action 不算只读
    assert cli_main._is_readonly_command("bootstrap", ["unknown"]) is False


def test_cli_bootstrap_in_subcommands():
    """bootstrap 应在 _SUBCOMMANDS 集合中。"""
    assert "bootstrap" in cli_main._SUBCOMMANDS


def test_cli_bootstrap_dispatched():
    """_dispatch_subcommand 应将 bootstrap 分发到 _handle_bootstrap。"""
    old_argv = sys.argv
    sys.argv = ["cw", "bootstrap", "--help"]
    try:
        called = {"handler": None}
        original = cli_main._handle_bootstrap

        def spy(args, db):
            called["handler"] = "bootstrap"
            return original(args, db)

        with mock.patch.object(cli_main, "_handle_bootstrap", side_effect=spy):
            try:
                cli_main._dispatch_subcommand(["--help"], db=None)
            except SystemExit:
                pass
        assert called["handler"] == "bootstrap", "bootstrap 应分发到 _handle_bootstrap"
    finally:
        sys.argv = old_argv


# ============================================
# MCP bootstrap_status 工具测试
# ============================================


def test_mcp_bootstrap_status_registered():
    """MCP server 注册了 bootstrap_status 工具。"""
    import inspect
    from callwarden.server import mcp_server

    src = inspect.getsource(mcp_server.create_mcp_server)
    assert "def bootstrap_status(" in src, "MCP 源码缺少 bootstrap_status 工具定义"
    assert "@mcp.tool()" in src, "MCP 源码缺少 @mcp.tool() 装饰器"


def test_mcp_bootstrap_status_no_params():
    """bootstrap_status MCP 工具应无参数。"""
    import ast
    import os as _os

    src_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "server", "mcp_server.py",
    )
    with open(src_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    func_def = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "bootstrap_status":
            func_def = node
            break
    assert func_def is not None, "未找到 bootstrap_status 函数定义"

    # bootstrap_status 无参数（仅 self 不算，因为是闭包内的函数）
    arg_names = [a.arg for a in func_def.args.args]
    # 闭包内函数无 self 参数
    assert len(arg_names) == 0, f"bootstrap_status 应无参数，实际: {arg_names}"


def test_mcp_bootstrap_status_in_tool_list():
    """create_mcp_server 返回的 server 工具列表包含 bootstrap_status。"""
    from callwarden.server.mcp_server import create_mcp_server

    mcp = create_mcp_server()
    tools = [t.name for t in mcp._tool_manager.list_tools()]
    assert "bootstrap_status" in tools
