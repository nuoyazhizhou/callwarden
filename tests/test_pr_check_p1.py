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
    """构造 mock db，默认提供 check_before_edit + run_semgrep_and_save。"""
    db = MagicMock()
    db.conn = None  # 避免 _query_open_findings 真实查表
    # 默认方法存在
    db.check_before_edit = method_mocks.get("check_before_edit", MagicMock(return_value={}))
    db.run_semgrep_and_save = method_mocks.get(
        "run_semgrep_and_save", MagicMock(return_value=0)
    )
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
    """P1：Semgrep 抛异常时应收集到 run_errors。"""
    db = _make_db_with_methods(
        run_semgrep_and_save=MagicMock(side_effect=RuntimeError("semgrep boom"))
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
    """P1：db 缺 check_before_edit / run_semgrep_and_save 时应记录到 run_errors。"""
    db = MagicMock()
    db.conn = None
    # 故意不提供 check_before_edit / run_semgrep_and_save
    db.check_before_edit = None
    db.run_semgrep_and_save = None
    checker = _make_checker_with_changed_files(db, ["src/main.py"])

    result = checker.run_pr_check(base_branch="main", head="HEAD")

    assert result["scan_complete"] is False
    assert any("check_before_edit" in err for err in result["run_errors"])
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
