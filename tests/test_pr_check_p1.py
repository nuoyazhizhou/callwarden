"""P1 评审修复验证：PRChecker 不再 fail-open。

评审 P1：cicd/pr_check.py:66 用 getattr(db, "guardrail_check_edit", None) 调用
不存在的方法，异常被 try-except pass 吞掉，本次 Semgrep 结果也没有完整
进入 SARIF。

修复：
1. 改用 check_before_edit（真实方法名）
2. run_errors 收集所有异常，写入 SARIF invocations.executionNotifications
3. 返回结果加 run_errors + scan_complete 字段
"""
from unittest.mock import MagicMock

from callwarden.cicd.pr_check import PRChecker
from callwarden.cicd.sarif_exporter import SarifExporter


def _make_db_with_methods(**method_mocks):
    """构造 mock db，默认提供 check_before_edit + scan_semgrep_incremental。

    注意：A14 后 PRChecker 优先调用 db.scan_semgrep_incremental()，
    缺失时才 fallback 到 db.run_semgrep_and_save()。MagicMock 默认任意属性
    都返回 MagicMock，需显式将 scan_semgrep_incremental 设为 None 才能
    触发 fallback 路径。
    """
    db = MagicMock()
    db.conn = None  # 避免 _query_open_findings 真实查表
    # 默认方法存在
    db.check_before_edit = method_mocks.get("check_before_edit", MagicMock(return_value={}))
    # A14：优先使用增量扫描方法
    if "scan_semgrep_incremental" in method_mocks:
        db.scan_semgrep_incremental = method_mocks["scan_semgrep_incremental"]
    else:
        db.scan_semgrep_incremental = MagicMock(return_value=0)
    # run_semgrep_and_save 作为 fallback（仅当 scan_semgrep_incremental=None 时触发）
    if "run_semgrep_and_save" in method_mocks:
        db.run_semgrep_and_save = method_mocks["run_semgrep_and_save"]
    else:
        db.run_semgrep_and_save = MagicMock(return_value=0)
    return db


def _make_checker_with_changed_files(db, changed_files):
    """构造 PRChecker，patching IncrementalAnalyzer.get_changed_files。"""
    checker = PRChecker(db)
    # patching incremental.get_changed_files 返回指定文件列表
    checker.incremental.get_changed_files = MagicMock(return_value=changed_files)
    return checker


# ============================================
# 测试 1：真实方法名 check_before_edit 被调用
# ============================================


def test_pr_check_calls_real_method_check_before_edit():
    """P1：PRChecker 应调用 check_before_edit（不是 guardrail_check_edit）。"""
    db = _make_db_with_methods()
    checker = _make_checker_with_changed_files(db, ["src/main.py"])

    checker.run_pr_check(base_branch="main", head="HEAD")

    db.check_before_edit.assert_called_once_with("src/main.py")


# ============================================
# 测试 2：异常不再被静默吞掉
# ============================================


def test_pr_check_collects_guardrail_exception_to_run_errors():
    """P1：guardrail 抛异常时应收集到 run_errors，不再静默吞掉。"""
    db = _make_db_with_methods(
        check_before_edit=MagicMock(side_effect=RuntimeError("guardrail boom"))
    )
    checker = _make_checker_with_changed_files(db, ["src/main.py"])

    result = checker.run_pr_check(base_branch="main", head="HEAD")

    assert result["scan_complete"] is False, "guardrail 失败应导致 scan_complete=False"
    assert any(
        "guardrail boom" in err for err in result["run_errors"]
    ), f"run_errors 应包含 guardrail 异常信息，实际: {result['run_errors']}"


def test_pr_check_collects_semgrep_exception_to_run_errors():
    """P1：Semgrep 抛异常时应收集到 run_errors。

    A14 后优先调用 db.scan_semgrep_incremental()，mock 它的 side_effect
    才能触发异常路径（旧测试 mock run_semgrep_and_save 不会触发，因为
    scan_semgrep_incremental 默认是 MagicMock 不会抛异常）。
    """
    db = _make_db_with_methods(
        scan_semgrep_incremental=MagicMock(side_effect=RuntimeError("semgrep boom"))
    )
    checker = _make_checker_with_changed_files(db, ["src/main.py"])

    result = checker.run_pr_check(base_branch="main", head="HEAD")

    assert result["scan_complete"] is False
    assert any(
        "semgrep boom" in err for err in result["run_errors"]
    ), f"run_errors 应包含 semgrep 异常信息，实际: {result['run_errors']}"


# ============================================
# 测试 3：SARIF 包含 executionNotifications
# ============================================


def test_pr_check_sarif_contains_execution_notifications_on_error():
    """P1：失败时 SARIF 应包含 invocations[0].toolExecutionNotifications。"""
    db = _make_db_with_methods(
        check_before_edit=MagicMock(side_effect=ValueError("test error"))
    )
    checker = _make_checker_with_changed_files(db, ["src/main.py"])

    result = checker.run_pr_check(base_branch="main", head="HEAD")

    sarif = result["sarif_report"]
    assert "runs" in sarif
    run = sarif["runs"][0]
    assert "invocations" in run, "SARIF 应包含 invocations 字段"
    inv = run["invocations"][0]
    assert inv["executionSuccessful"] is False
    notifications = inv["toolExecutionNotifications"]
    assert len(notifications) >= 1
    assert notifications[0]["level"] == "error"
    assert "test error" in notifications[0]["message"]["text"]


def test_pr_check_sarif_no_invocations_when_no_errors():
    """P1：成功时 SARIF 不应包含 invocations（保持向后兼容）。"""
    db = _make_db_with_methods()
    checker = _make_checker_with_changed_files(db, ["src/main.py"])

    result = checker.run_pr_check(base_branch="main", head="HEAD")

    sarif = result["sarif_report"]
    run = sarif["runs"][0]
    # 当所有步骤成功时，run_errors 应为空列表
    assert result["scan_complete"] is True
    assert result["run_errors"] == []
    # invocations 仍存在但 executionSuccessful=True
    assert "invocations" in run
    assert run["invocations"][0]["executionSuccessful"] is True
    assert run["invocations"][0]["toolExecutionNotifications"] == []


# ============================================
# 测试 4：db 缺方法时也记录到 run_errors（不再 fail-open）
# ============================================


def test_pr_check_records_missing_methods_to_run_errors():
    """P1：db 缺 check_before_edit / scan_semgrep_incremental / run_semgrep_and_save 时应记录到 run_errors。"""
    db = MagicMock()
    db.conn = None
    # 故意不提供 check_before_edit / scan_semgrep_incremental / run_semgrep_and_save
    db.check_before_edit = None
    db.scan_semgrep_incremental = None
    db.run_semgrep_and_save = None
    checker = _make_checker_with_changed_files(db, ["src/main.py"])

    result = checker.run_pr_check(base_branch="main", head="HEAD")

    assert result["scan_complete"] is False
    assert any("check_before_edit" in err for err in result["run_errors"])
    assert any("scan_semgrep_incremental" in err for err in result["run_errors"])
    assert any("run_semgrep_and_save" in err for err in result["run_errors"])


# ============================================
# 测试 5：SarifExporter.export_findings 直接验证
# ============================================


def test_sarif_exporter_run_errors_parameter_writes_notifications():
    """P1：SarifExporter.export_findings 接受 run_errors 参数写入 notifications。"""
    exporter = SarifExporter()
    report = exporter.export_findings(
        findings=[],
        run_errors=["error1", "error2"],
    )
    run = report["runs"][0]
    assert "invocations" in run
    inv = run["invocations"][0]
    assert inv["executionSuccessful"] is False
    assert len(inv["toolExecutionNotifications"]) == 2
    assert inv["toolExecutionNotifications"][0]["message"]["text"] == "error1"


def test_sarif_exporter_no_run_errors_keeps_backward_compat():
    """P1：export_findings 不传 run_errors 时不输出 invocations（向后兼容）。"""
    exporter = SarifExporter()
    report = exporter.export_findings(findings=[])
    run = report["runs"][0]
    assert "invocations" not in run, "未传 run_errors 时不应输出 invocations"


# ============================================
# 测试 6：复审回退修复（2026-07-21 P1-1）—— passed 纳入 scan_complete
# ============================================


def test_pr_check_passed_false_when_scan_incomplete_even_with_zero_findings():
    """复审 P1-1：扫描未完成（run_errors 非空）时即使零 finding 也必须阻断。

    旧实现 passed = errors == 0，扫描失败但零 finding 时 passed=True（fail-open），
    GitHub Action 会 exit 0 放行 PR。修复后 passed = (errors == 0) and scan_complete，
    任一条件不满足都阻断。
    """
    db = _make_db_with_methods(
        check_before_edit=MagicMock(side_effect=RuntimeError("guardrail crashed"))
    )
    checker = _make_checker_with_changed_files(db, ["src/main.py"])

    result = checker.run_pr_check(base_branch="main", head="HEAD")

    # 零 finding（db.conn=None 让 _query_open_findings 返回空列表）
    assert result["total_findings"] == 0
    assert result["errors"] == 0
    # 但扫描未完成
    assert result["scan_complete"] is False
    assert len(result["run_errors"]) > 0
    # 关键断言：passed 必须为 False，避免 fail-open 放行 PR
    assert result["passed"] is False, (
        "复审 P1-1：扫描未完成时 passed 必须为 False，"
        "即使零 finding 也不能放行 PR（避免 fail-open）"
    )


def test_pr_check_passed_true_only_when_zero_errors_and_scan_complete():
    """复审 P1-1：正常路径下 passed=True 需要同时满足 errors==0 和 scan_complete。"""
    db = _make_db_with_methods()  # 默认所有方法都正常
    checker = _make_checker_with_changed_files(db, ["src/main.py"])

    result = checker.run_pr_check(base_branch="main", head="HEAD")

    assert result["errors"] == 0
    assert result["scan_complete"] is True
    assert result["run_errors"] == []
    assert result["passed"] is True


# ============================================
# 测试 7：复审回退修复（2026-07-21 P1-1）—— _query_open_findings 合并 semgrep_findings
# ============================================


def test_pr_check_query_open_findings_merges_semgrep_findings():
    """复审 P1-1：_query_open_findings 应合并 semgrep_findings 进阻断结果。

    旧实现只查 guardrail_findings，未合并 semgrep_findings，导致 Semgrep 发现的
    error 级 finding 无法阻断 PR。修复后两类 findings 都纳入。
    """
    # 构造一个 in-memory SQLite db，包含 guardrail_findings + semgrep_findings + file_instances
    import sqlite3
    db = MagicMock()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # guardrail_findings 表
    conn.execute("""
        CREATE TABLE guardrail_findings (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER NOT NULL DEFAULT 0,
            rule_id TEXT,
            file_path TEXT,
            severity TEXT,
            status TEXT,
            message TEXT,
            detected_at REAL
        )
    """)
    # file_instances 表（semgrep_findings 通过 file_instance_id 关联）
    conn.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER,
            rel_path TEXT,
            abs_path TEXT
        )
    """)
    # semgrep_findings 表（schema v40 字段）
    conn.execute("""
        CREATE TABLE semgrep_findings (
            id INTEGER PRIMARY KEY,
            file_instance_id INTEGER,
            content_hash TEXT,
            rule_id TEXT,
            rule_name TEXT,
            message TEXT,
            severity TEXT,
            confidence TEXT,
            language TEXT,
            start_line INTEGER,
            end_line INTEGER,
            snippet TEXT,
            fix TEXT,
            symbol_id INTEGER,
            symbol_qualified TEXT,
            scanned_at REAL,
            scan_id INTEGER
        )
    """)
    db.conn = conn

    # 插入测试数据
    # file_instances: src/main.py -> id=1
    conn.execute(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path) "
        "VALUES (1, 1, 'src/main.py', '/abs/src/main.py')"
    )
    # guardrail_findings: 1 个 warning 级 finding
    conn.execute(
        "INSERT INTO guardrail_findings (id, workspace_id, rule_id, file_path, severity, status, message, detected_at) "
        "VALUES (1, 1, 'G-001', 'src/main.py', 'warning', 'open', 'guardrail warn', 1000.0)"
    )
    # semgrep_findings: 1 个 error 级 finding（severity='error'）
    conn.execute(
        "INSERT INTO semgrep_findings (id, file_instance_id, rule_id, severity, message, scanned_at) "
        "VALUES (1, 1, 'S-001', 'error', 'semgrep error finding', 2000.0)"
    )
    conn.commit()

    checker = PRChecker(db)
    # 直接调用 _query_open_findings 验证合并
    findings = checker._query_open_findings(["src/main.py"])

    # 应该有 2 条 finding：1 guardrail + 1 semgrep
    assert len(findings) == 2, f"应合并 2 条 finding，实际: {len(findings)}"

    sources = {f.get("source") for f in findings}
    assert sources == {"guardrail", "semgrep"}, (
        f"source 应包含 guardrail 和 semgrep，实际: {sources}"
    )

    # 验证 semgrep finding 的字段映射正确
    semgrep_finding = next(f for f in findings if f["source"] == "semgrep")
    assert semgrep_finding["severity"] == "error"
    assert semgrep_finding["file_path"] == "src/main.py"
    assert semgrep_finding["status"] == "open"
    assert "semgrep error finding" in semgrep_finding["message"]


def test_pr_check_semgrep_error_finding_blocks_pr():
    """复审 P1-1：Semgrep error 级 finding 应让 PR 阻断（passed=False）。

    集成测试：guardrail 零 error，但 semgrep 有 1 条 error 级 finding，
    passed 必须为 False。
    """
    import sqlite3
    db = _make_db_with_methods()  # 所有方法正常
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE guardrail_findings (
            id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL DEFAULT 0,
            rule_id TEXT, file_path TEXT,
            severity TEXT, status TEXT, message TEXT, detected_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY, workspace_id INTEGER,
            rel_path TEXT, abs_path TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE semgrep_findings (
            id INTEGER PRIMARY KEY, file_instance_id INTEGER, content_hash TEXT,
            rule_id TEXT, rule_name TEXT, message TEXT, severity TEXT,
            confidence TEXT, language TEXT, start_line INTEGER, end_line INTEGER,
            snippet TEXT, fix TEXT, symbol_id INTEGER, symbol_qualified TEXT,
            scanned_at REAL, scan_id INTEGER
        )
    """)
    db.conn = conn

    conn.execute(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path) "
        "VALUES (1, 1, 'src/main.py', '/abs/src/main.py')"
    )
    # 1 条 semgrep error finding，无 guardrail finding
    conn.execute(
        "INSERT INTO semgrep_findings (id, file_instance_id, rule_id, severity, message, scanned_at) "
        "VALUES (1, 1, 'S-001', 'error', 'semgrep error', 2000.0)"
    )
    conn.commit()

    checker = _make_checker_with_changed_files(db, ["src/main.py"])
    result = checker.run_pr_check(base_branch="main", head="HEAD")

    # scan_complete=True（方法都正常），但 errors=1（semgrep error finding）
    assert result["scan_complete"] is True
    assert result["errors"] == 1, (
        f"errors 应为 1（semgrep error finding），实际: {result['errors']}"
    )
    assert result["passed"] is False, (
        "复审 P1-1：semgrep error finding 必须阻断 PR，passed=False"
    )


# ============================================
# P1-1 补充测试（2026-07-22）：三条 fail-open 路径闭合验证
# ============================================


def test_p1_1_git_diff_failure_blocks_pr():
    """P1-1 路径 1：git diff 失败时不能 fail-open。

    旧实现 get_changed_files() 在 git diff 非零时返回空列表，
    PRChecker 将其解释为"无改动"并通过 → fail-open。
    修复后 get_changed_files 抛 GitDiffError，run_pr_check 捕获后
    记录到 run_errors，scan_complete=False，passed=False。
    """
    from callwarden.cicd.incremental import GitDiffError

    db = _make_db_with_methods()
    checker = PRChecker(db)
    # mock incremental.get_changed_files 抛 GitDiffError
    checker.incremental.get_changed_files = MagicMock(
        side_effect=GitDiffError("git diff failed: base branch not found")
    )
    result = checker.run_pr_check(base_branch="nonexistent", head="HEAD")

    # git diff 失败 → run_errors 非空 → scan_complete=False → passed=False
    assert result["scan_complete"] is False, (
        "git diff 失败时 scan_complete 必须为 False"
    )
    assert result["passed"] is False, (
        "git diff 失败时 passed 必须为 False（不能 fail-open）"
    )
    assert any("git diff" in err for err in result["run_errors"]), (
        f"run_errors 应包含 git diff 错误，实际: {result['run_errors']}"
    )


def test_p1_1_semgrep_success_false_blocks_pr():
    """P1-1 路径 2：scan_semgrep_incremental 返回 success=False 不能 fail-open。

    旧实现只 try-except 异常，不检查返回值。
    scan_semgrep_incremental 用 {success: false, error: ...} 表示扫描失败，
    旧代码不检查 → run_errors 为空 → scan_complete=True → fail-open。
    修复后显式检查返回值的 success 字段。
    """
    db = _make_db_with_methods(
        scan_semgrep_incremental=MagicMock(
            return_value={"success": False, "error": "semgrep CLI crashed"}
        )
    )
    # mock incremental.get_changed_files 返回非空列表（进入 semgrep 扫描路径）
    checker = _make_checker_with_changed_files(db, ["src/main.py"])
    result = checker.run_pr_check(base_branch="main", head="HEAD")

    # semgrep success=false → run_errors 非空 → scan_complete=False → passed=False
    assert result["scan_complete"] is False, (
        "semgrep success=false 时 scan_complete 必须为 False"
    )
    assert result["passed"] is False, (
        "semgrep success=false 时 passed 必须为 False（不能 fail-open）"
    )
    assert any("success=false" in err for err in result["run_errors"]), (
        f"run_errors 应包含 success=false 错误，实际: {result['run_errors']}"
    )


def test_p1_1_sql_failure_blocks_pr():
    """P1-1 路径 3：_query_open_findings SQL 异常不能静默 pass。

    旧实现 guardrail/semgrep 两个 SQL 异常都静默 pass，
    查询损坏变成零 finding → scan_complete=True → fail-open。
    修复后捕获异常记录到 _findings_query_errors，run_pr_check 合并到 run_errors。
    """
    import sqlite3
    db = _make_db_with_methods()
    # 构造一个故意损坏的 conn：表名不存在
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    # 不创建任何表 → SQL 查询会抛 OperationalError
    db.conn = conn

    checker = _make_checker_with_changed_files(db, ["src/main.py"])
    result = checker.run_pr_check(base_branch="main", head="HEAD")

    # SQL 失败 → run_errors 非空 → scan_complete=False → passed=False
    assert result["scan_complete"] is False, (
        "SQL 查询失败时 scan_complete 必须为 False"
    )
    assert result["passed"] is False, (
        "SQL 查询失败时 passed 必须为 False（不能 fail-open）"
    )
    assert any("查询失败" in err for err in result["run_errors"]), (
        f"run_errors 应包含查询失败错误，实际: {result['run_errors']}"
    )


def test_p1_1_workspace_id_filter_prevents_cross_workspace_leak():
    """P1-1 路径 4：Semgrep SQL 添加 workspace_id 过滤防止跨 workspace 混入。

    旧实现 semgrep_findings JOIN file_instances 只按 rel_path 过滤，
    无 workspace_id 条件，可能把另一个 workspace 同路径的 finding 混入。
    修复后添加 fi.workspace_id = ? 条件。
    """
    import sqlite3
    db = _make_db_with_methods()
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE guardrail_findings (
            id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL DEFAULT 0,
            rule_id TEXT, file_path TEXT,
            severity TEXT, status TEXT, message TEXT, detected_at REAL
        )
    """)
    conn.execute("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY, workspace_id INTEGER,
            rel_path TEXT, abs_path TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE semgrep_findings (
            id INTEGER PRIMARY KEY, file_instance_id INTEGER, content_hash TEXT,
            rule_id TEXT, rule_name TEXT, message TEXT, severity TEXT,
            confidence TEXT, language TEXT, start_line INTEGER, end_line INTEGER,
            snippet TEXT, fix TEXT, symbol_id INTEGER, symbol_qualified TEXT,
            scanned_at REAL, scan_id INTEGER
        )
    """)

    # workspace_id=1 和 workspace_id=2 都有 src/main.py
    conn.execute(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path) "
        "VALUES (1, 1, 'src/main.py', '/abs1/src/main.py')"
    )
    conn.execute(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path) "
        "VALUES (2, 2, 'src/main.py', '/abs2/src/main.py')"
    )
    # workspace_id=2 的 finding（不应被 workspace_id=1 的 PR 查到）
    conn.execute(
        "INSERT INTO semgrep_findings (id, file_instance_id, rule_id, severity, message, scanned_at) "
        "VALUES (100, 2, 'S-OTHER', 'error', 'other workspace finding', 3000.0)"
    )
    # v48（W2.3 P1-1）：guardrail_findings 的 workspace 隔离也要验证
    conn.execute(
        "INSERT INTO guardrail_findings "
        "(id, workspace_id, rule_id, file_path, severity, status, message, detected_at) "
        "VALUES (200, 2, 'G-OTHER', 'src/main.py', 'warning', 'open', 'other workspace guardrail', 3000.0)"
    )
    conn.commit()
    db.conn = conn
    # mock _get_active_workspace_id 返回 1
    db._get_active_workspace_id = MagicMock(return_value=1)

    checker = PRChecker(db)
    findings = checker._query_open_findings(["src/main.py"])

    # workspace_id=2 的 finding 不应被查到
    assert len(findings) == 0, (
        f"workspace_id=1 的 PR 不应查到 workspace_id=2 的 finding，"
        f"实际查到: {len(findings)} 条"
    )
