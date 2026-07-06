"""C3: bootstrap_status CI 门禁测试。

覆盖 T-1783349079762-13b6 任务：
- run_bootstrap_gate() 通过场景（exit 0）
- run_bootstrap_gate() db_stale 失败（exit 1）
- run_bootstrap_gate() blocking_findings 失败
- run_bootstrap_gate() audit_chain broken 失败
- run_bootstrap_gate() 异常 fail-soft
- _print_gate_result() 输出格式

测试内容：
1. 空数据库通过门禁（所有指标正常）
2. db_stale=True 触发失败
3. blocking_findings_count > 0 触发失败
4. audit_verify.broken_count > 0 触发失败
5. 多项同时失败时 failed_checks 列表完整
6. bootstrap_status 异常时 fail-soft
7. 通过时输出 PASS 摘要
8. 失败时输出 FAIL 详情和推荐修复
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

import pytest

# 确保项目根目录在 path 中
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.cicd.bootstrap_check import (
    _print_gate_result,
    main,
    run_bootstrap_gate,
)
from callwarden.db.db import CodeGraphDB


# ============================================
# 辅助函数
# ============================================


def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _make_mock_db(status_result):
    """构造 mock db，bootstrap_status 返回指定结果"""
    mock_db = MagicMock()
    mock_db.bootstrap_status.return_value = status_result
    mock_db.close = MagicMock()
    return mock_db


# ============================================
# run_bootstrap_gate 测试
# ============================================


def test_gate_passes_on_healthy_db():
    """空数据库通过门禁（所有指标正常）"""
    db, _root = _db_with_workspace()
    try:
        result = run_bootstrap_gate(db)
        assert result["passed"] is True
        assert result["failed_checks"] == []
        assert result["reason"] == ""
    finally:
        db.close()


def test_gate_passes_with_mock_status():
    """用 mock status 验证通过场景"""
    status = {
        "db_stale": False,
        "blocking_findings_count": 0,
        "audit_verify": {"broken_count": 0, "total_count": 10, "verified_count": 10},
        "active_rules_count": 5,
        "open_findings_count": 2,
        "recommended_next_action": "cw task list",
    }
    mock_db = _make_mock_db(status)
    result = run_bootstrap_gate(mock_db)
    assert result["passed"] is True
    assert result["failed_checks"] == []


def test_gate_fails_on_db_stale():
    """db_stale=True 触发失败"""
    status = {
        "db_stale": True,
        "blocking_findings_count": 0,
        "audit_verify": {"broken_count": 0},
    }
    mock_db = _make_mock_db(status)
    result = run_bootstrap_gate(mock_db)
    assert result["passed"] is False
    assert "db_stale" in result["failed_checks"]


def test_gate_fails_on_blocking_findings():
    """blocking_findings_count > 0 触发失败"""
    status = {
        "db_stale": False,
        "blocking_findings_count": 3,
        "audit_verify": {"broken_count": 0},
    }
    mock_db = _make_mock_db(status)
    result = run_bootstrap_gate(mock_db)
    assert result["passed"] is False
    assert "blocking_findings" in result["failed_checks"]


def test_gate_fails_on_audit_broken():
    """audit_verify.broken_count > 0 触发失败"""
    status = {
        "db_stale": False,
        "blocking_findings_count": 0,
        "audit_verify": {"broken_count": 2, "total_count": 10, "verified_count": 8},
    }
    mock_db = _make_mock_db(status)
    result = run_bootstrap_gate(mock_db)
    assert result["passed"] is False
    assert "audit_broken" in result["failed_checks"]


def test_gate_multiple_failures():
    """多项同时失败时 failed_checks 列表完整"""
    status = {
        "db_stale": True,
        "blocking_findings_count": 5,
        "audit_verify": {"broken_count": 3},
    }
    mock_db = _make_mock_db(status)
    result = run_bootstrap_gate(mock_db)
    assert result["passed"] is False
    assert len(result["failed_checks"]) == 3
    assert "db_stale" in result["failed_checks"]
    assert "blocking_findings" in result["failed_checks"]
    assert "audit_broken" in result["failed_checks"]


def test_gate_fail_soft_on_exception():
    """bootstrap_status 异常时 fail-soft 返回失败"""
    mock_db = MagicMock()
    mock_db.bootstrap_status.side_effect = RuntimeError("DB connection lost")
    result = run_bootstrap_gate(mock_db)
    assert result["passed"] is False
    assert "status_query" in result["failed_checks"]
    assert "DB connection lost" in result["reason"]


# ============================================
# _print_gate_result 测试
# ============================================


def test_print_gate_result_pass():
    """通过时输出 PASS 摘要"""
    result = {
        "passed": True,
        "reason": "",
        "failed_checks": [],
        "status": {
            "active_rules_count": 5,
            "open_findings_count": 2,
            "audit_verify": {"verified_count": 10, "total_count": 10},
        },
    }
    out_buf = io.StringIO()
    with redirect_stdout(out_buf):
        _print_gate_result(result)
    output = out_buf.getvalue()
    assert "PASS" in output
    assert "5" in output  # active_rules


def test_print_gate_result_fail():
    """失败时输出 FAIL 详情"""
    result = {
        "passed": False,
        "reason": "Bootstrap gate failed: db_stale",
        "failed_checks": ["db_stale"],
        "status": {
            "db_stale": True,
            "blocking_findings_count": 0,
            "audit_verify": {"broken_count": 0},
            "recommended_next_action": "cw --refresh-all",
        },
    }
    out_buf = io.StringIO()
    with redirect_stdout(out_buf):
        _print_gate_result(result)
    output = out_buf.getvalue()
    assert "FAIL" in output
    assert "db_stale" in output
    assert "cw --refresh-all" in output  # 推荐修复


# ============================================
# 端到端集成测试
# ============================================


def test_main_returns_0_on_healthy_db():
    """main() 在健康数据库上返回 0"""
    # 使用 mock 避免真实 db 初始化
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(os.path.join(tmp, "callwarden.db"), workspace_root=tmp)
        db.close()

        # Mock CodeGraphDB 返回健康 db
        mock_db = MagicMock()
        mock_db.bootstrap_status.return_value = {
            "db_stale": False,
            "blocking_findings_count": 0,
            "audit_verify": {"broken_count": 0},
        }
        mock_db.close = MagicMock()

        with patch(
            "callwarden.cicd.bootstrap_check.CodeGraphDB", return_value=mock_db
        ):
            exit_code = main()
        assert exit_code == 0


def test_main_returns_1_on_failed_gate():
    """main() 在门禁失败时返回 1"""
    mock_db = MagicMock()
    mock_db.bootstrap_status.return_value = {
        "db_stale": True,
        "blocking_findings_count": 0,
        "audit_verify": {"broken_count": 0},
    }
    mock_db.close = MagicMock()

    with patch(
        "callwarden.cicd.bootstrap_check.CodeGraphDB", return_value=mock_db
    ):
        exit_code = main()
    assert exit_code == 1


def test_gate_with_real_db_no_scan():
    """真实数据库无 scan_run 时通过（不 stale）"""
    db, _root = _db_with_workspace()
    try:
        result = run_bootstrap_gate(db)
        # 无 scan_run 时 db_stale=False（无法对比）
        assert result["passed"] is True
    finally:
        db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
