"""§46 报告型命令 rc 新语义回归：opt-in --fail-on-findings → rc=3。

口径（见 `.specs/backlog-46-report-rc-semantics/spec.md` 与
`docs/cli_reference.md` 全局退出码契约）：
- 默认（不带 flag）行为不变：报告型命令即使报告 findings 也 rc=0；
- `--fail-on-findings` 开启且报告了 findings → rc=3（报告发现失败）；
- 连带收口（§43-45 口径「真失败 → rc=1」）：`semgrep scan` 各分支
  ``success=false`` 时 rc=1（原先打印 error 后静默 rc=0）。

本文件沿用 `tests/test_cli_exit_code_local_input.py` 的 harness 模式：
直接调用 `_handle_*`（db 用最小 fake），不触达 daemon。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.cli import main as cli_main  # noqa: E402 —— sys.path 注入须先于 import


class _FakeDB:
    """最小 db 替身：按测试需要注入方法返回值。"""

    client_workspace_root = "."

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _call(fn, argv, db):
    """正常调用 handler，返回 (ret, out)。"""
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    with redirect_stdout(buf):
        ret = fn(argv, db)
    return ret, buf.getvalue()


def _exit_code(fn, argv, db):
    with pytest.raises(SystemExit) as excinfo:
        fn(argv, db)
    return excinfo.value.code


# ----------------------------------------------------------------------
# issues
# ----------------------------------------------------------------------

_ISSUES = [{"source": "semgrep", "severity": "WARNING", "rule_id": "r1",
            "rule_name": "r1", "message": "m", "start_line": 1,
            "end_line": 1, "confidence": "", "status": "", "snippet": "",
            "fix": ""}]


def test_issues_findings_with_flag_exit3(capsys):
    db = _FakeDB(get_symbol_issues=lambda *a, **k: list(_ISSUES))
    code = _exit_code(cli_main._handle_issues, ["m::f", "--fail-on-findings"], db)
    assert code == 3
    assert "fail-on-findings" in (capsys.readouterr().out + capsys.readouterr().err)


def test_issues_findings_without_flag_rc0():
    db = _FakeDB(get_symbol_issues=lambda *a, **k: list(_ISSUES))
    ret, _ = _call(cli_main._handle_issues, ["m::f"], db)
    assert ret is True


def test_issues_empty_with_flag_rc0():
    db = _FakeDB(get_symbol_issues=lambda *a, **k: [])
    ret, _ = _call(cli_main._handle_issues, ["m::f", "--fail-on-findings"], db)
    assert ret is True


# ----------------------------------------------------------------------
# detect-cycles / orphan-symbols
# ----------------------------------------------------------------------

def test_detect_cycles_findings_with_flag_exit3(capsys):
    db = _FakeDB(detect_cycles=lambda **k: [["a", "b", "a"]])
    code = _exit_code(cli_main._handle_detect_cycles, ["--fail-on-findings"], db)
    assert code == 3
    assert "fail-on-findings" in (capsys.readouterr().out + capsys.readouterr().err)


def test_detect_cycles_without_flag_rc0():
    db = _FakeDB(detect_cycles=lambda **k: [["a", "b", "a"]])
    ret, _ = _call(cli_main._handle_detect_cycles, [], db)
    assert ret is True


def test_orphan_symbols_findings_with_flag_exit3():
    db = _FakeDB(get_orphan_symbols=lambda **k: [
        {"qualified_name": "m::lonely", "module_path": "m"}])
    code = _exit_code(cli_main._handle_orphan_symbols, ["--fail-on-findings"], db)
    assert code == 3


def test_orphan_symbols_empty_with_flag_rc0():
    db = _FakeDB(get_orphan_symbols=lambda **k: [])
    ret, _ = _call(cli_main._handle_orphan_symbols, ["--fail-on-findings"], db)
    assert ret is True


# ----------------------------------------------------------------------
# function-issues（单函数 / 列表模式；--summary 不参与 §46）
# ----------------------------------------------------------------------

_FN_RESULT = [{"qualified_name": "m::f", "module_path": "m",
               "issue_count": 2, "issues": []}]


def test_function_issues_fn_mode_with_flag_exit3():
    db = _FakeDB(get_function_issues=lambda **k: list(_FN_RESULT))
    code = _exit_code(cli_main._handle_function_issues,
                      ["m::f", "--fail-on-findings"], db)
    assert code == 3


def test_function_issues_list_mode_with_flag_exit3():
    db = _FakeDB(get_function_issues=lambda **k: list(_FN_RESULT))
    code = _exit_code(cli_main._handle_function_issues,
                      ["", "--fail-on-findings"], db)
    assert code == 3


def test_function_issues_no_findings_with_flag_rc0():
    db = _FakeDB(get_function_issues=lambda **k: [])
    ret, _ = _call(cli_main._handle_function_issues,
                   ["m::f", "--fail-on-findings"], db)
    assert ret is True


# ----------------------------------------------------------------------
# semgrep scan：失败 → rc=1（§46 连带收口）；findings + flag → rc=3
# ----------------------------------------------------------------------

def test_semgrep_scan_incremental_failure_exit1():
    db = _FakeDB(scan_semgrep_incremental=lambda **k:
                 {"success": False, "error": "boom"})
    code = _exit_code(cli_main._handle_semgrep, ["scan", "--incremental"], db)
    assert code == 1


def test_semgrep_scan_save_failure_exit1():
    db = _FakeDB(run_semgrep_and_save=lambda **k:
                 {"success": False, "error": "boom"})
    code = _exit_code(cli_main._handle_semgrep, ["scan", "--save"], db)
    assert code == 1


def test_semgrep_scan_default_failure_exit1():
    db = _FakeDB(run_semgrep=lambda **k: {"success": False, "error": "boom"})
    code = _exit_code(cli_main._handle_semgrep, ["scan"], db)
    assert code == 1


def test_semgrep_scan_incremental_findings_with_flag_exit3():
    db = _FakeDB(scan_semgrep_incremental=lambda **k:
                 {"success": True, "changed_files": 1, "scanned_files": 1,
                  "saved_findings": 2, "total_findings": 2,
                  "stale_file_ids": []})
    code = _exit_code(cli_main._handle_semgrep,
                      ["scan", "--incremental", "--fail-on-findings"], db)
    assert code == 3


def test_semgrep_scan_default_findings_with_flag_exit3():
    db = _FakeDB(run_semgrep=lambda **k:
                 {"success": True, "total_findings": 1, "results": []})
    code = _exit_code(cli_main._handle_semgrep,
                      ["scan", "--fail-on-findings"], db)
    assert code == 3


def test_semgrep_scan_zero_findings_with_flag_rc0():
    db = _FakeDB(scan_semgrep_incremental=lambda **k:
                 {"success": True, "changed_files": 1, "scanned_files": 1,
                  "saved_findings": 0, "total_findings": 0,
                  "stale_file_ids": []})
    ret, _ = _call(cli_main._handle_semgrep,
                   ["scan", "--incremental", "--fail-on-findings"], db)
    assert ret is True


# ----------------------------------------------------------------------
# clone detect
# ----------------------------------------------------------------------

def test_clone_detect_findings_with_flag_exit3():
    db = _FakeDB(detect_clones=lambda **k:
                 {"total_pairs": 2, "type1_pairs": 1, "type2_pairs": 1,
                  "type3_pairs": 0, "scanned_symbols": 10,
                  "skipped_symbols": 0, "similarity_threshold": 0.8,
                  "min_lines": 5})
    code = _exit_code(cli_main._handle_clone, ["detect", "--fail-on-findings"], db)
    assert code == 3


def test_clone_detect_without_flag_rc0():
    db = _FakeDB(detect_clones=lambda **k:
                 {"total_pairs": 2, "type1_pairs": 1, "type2_pairs": 1,
                  "type3_pairs": 0, "scanned_symbols": 10,
                  "skipped_symbols": 0, "similarity_threshold": 0.8,
                  "min_lines": 5})
    ret, _ = _call(cli_main._handle_clone, ["detect"], db)
    assert ret is True


# ----------------------------------------------------------------------
# helper 单测：无 flag 属性时静默通过
# ----------------------------------------------------------------------

def test_exit_on_findings_without_attr_no_exit():
    class _Opts:
        pass

    cli_main._exit_on_findings(_Opts(), 5)  # 不触发 SystemExit 即通过
