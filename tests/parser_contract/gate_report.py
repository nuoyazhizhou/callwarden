"""P0-A Step 6: 机器可读门禁报告生成器。

设计文档 §5.2 / §6.3 要求：
- 16 语言 ParseFact 契约门禁必须以结构化 JSON 发布
- 未知差异 fail closed（exit code 非零）
- 报告作为 CI artifact 持久化

本脚本聚合以下契约测试套件的结果，输出单一 JSON 报告：
  - test_rust_python_alignment.py  → kind / signature / visibility 对齐门禁
  - test_identity_range.py         → identity / range / parent / call ordinal / ABI 缺口门禁
  - test_encoding_error.py         → encoding / error 契约门禁
  - test_golden_fixtures.py        → golden fixture 契约门禁
  - test_baseline.py               → baseline 与 bundle inspector 门禁

用法：
  python tests/parser_contract/gate_report.py [--output PATH] [--no-pytest]

输出：
  - stdout: 结构化 JSON 报告
  - 默认写文件: tests/parser_contract/gate_report.json
  - exit code: 0=全部通过, 1=存在失败（fail closed）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

# 添加 tests/ 目录到 path 以复用已知差异定义
_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

# 契约测试文件 → 门禁名称映射
_GATE_TEST_FILES: dict[str, str] = {
    "tests/test_rust_python_alignment.py": "kind_signature_visibility",
    "tests/parser_contract/test_identity_range.py": "identity_range",
    "tests/parser_contract/test_encoding_error.py": "encoding_error",
    "tests/parser_contract/test_golden_fixtures.py": "golden_contract",
    "tests/parser_contract/test_baseline.py": "baseline",
}

# 门禁描述（人类可读）
_GATE_DESCRIPTIONS: dict[str, str] = {
    "kind_signature_visibility": (
        "Step 2+3: symbol kind / signature / visibility 对齐门禁。"
        "验证 Rust 与 Python parser 输出的 kind、signature、visibility 一致"
        "（已知差异用 Counter 相减，残余差异必须为零）。"
    ),
    "identity_range": (
        "Step 4: symbol/call 身份与范围对齐门禁。"
        "验证 lexical parent（range-based 推导）、line range 有效性、"
        "Rust symbol_hash 确定性、call ordinal 对齐、ABI 缺口文档化、"
        "content 一致性、Rust depth 一致性。"
    ),
    "encoding_error": (
        "Step 5: encoding/error 契约门禁。"
        "验证 canonicalize_source 行为（BOM/CRLF/UTF-16/GBK）、"
        "空文件、语法错误、partial parse、大文件、no panic、error 语义。"
    ),
    "golden_contract": (
        "Step 1: golden fixture 契约门禁。"
        "验证 16 语言 golden fixture 结构完整、字段齐全、"
        "known_gaps 文档化、provenance 可追溯。"
    ),
    "baseline": (
        "Step 0: baseline 与 bundle inspector 门禁。"
        "验证 baseline.json 有效、暴露 Phase 0 缺口、"
        "bundle inspector 分类稳定且 forbid distribution 门禁通过。"
    ),
}


def _collect_known_differences() -> dict[str, Any]:
    """从 test_rust_python_alignment.py 收集已知差异白名单。

    这些差异是经评审确认的有意契约差异，不计入 fail-closed 判定。
    设计文档 §6.3: 不允许 Rust 缺失的条目进入白名单。
    """
    known: dict[str, Any] = {
        "symbol_diffs": {},
        "call_diffs": {},
        "kind_diffs": {},
        "visibility_diffs": {},
    }
    try:
        # 延迟导入：test_rust_python_alignment 依赖 pytest 装饰器
        from test_rust_python_alignment import (  # type: ignore[import-not-found]
            KNOWN_SYMBOL_DIFFS,
            KNOWN_CALL_DIFFS,
            KNOWN_KIND_DIFFS,
            KNOWN_VISIBILITY_DIFFS,
        )

        for lang, (reason, counter) in KNOWN_SYMBOL_DIFFS.items():
            known["symbol_diffs"][lang] = {
                "reason": reason,
                "count": sum(counter.values()),
                "items": [list(k) for k in counter.keys()],
            }
        for lang, (reason, counter) in KNOWN_CALL_DIFFS.items():
            known["call_diffs"][lang] = {
                "reason": reason,
                "count": sum(counter.values()),
                "items": [list(k) for k in counter.keys()],
            }
        for lang, (reason, counter) in KNOWN_KIND_DIFFS.items():
            known["kind_diffs"][lang] = {
                "reason": reason,
                "count": sum(counter.values()),
                "items": [list(k) for k in counter.keys()],
            }
        for lang, (reason, counter) in KNOWN_VISIBILITY_DIFFS.items():
            known["visibility_diffs"][lang] = {
                "reason": reason,
                "count": sum(counter.values()),
                "items": [list(k) for k in counter.keys()],
            }
    except Exception as e:
        known["_import_error"] = f"{type(e).__name__}: {e}"
    return known


def _detect_rust_available() -> bool:
    """检测 callwarden_core (Rust 扩展) 是否可用。"""
    try:
        import callwarden_core  # type: ignore[import-not-found]  # noqa: F401

        return True
    except Exception:
        return False


def _run_pytest_contract_suite(repo_root: str) -> tuple[dict[str, dict], str]:
    """运行契约测试套件，返回 (per_gate_results, junit_xml_path)。

    每个门禁的结果包含: total, passed, failed, skipped, failed_tests, duration_ms。
    """
    # 写 JUnit XML 到临时文件
    tmp_dir = Path(tempfile.mkdtemp(prefix="gate_report_"))
    junit_path = str(tmp_dir / "junit.xml")

    test_files = list(_GATE_TEST_FILES.keys())
    # 用 -p no:cacheprovider 避免缓存干扰，--tb=short 减少输出
    cmd = [
        sys.executable, "-m", "pytest",
        *test_files,
        f"--junit-xml={junit_path}",
        "-q",
        "--tb=short",
        "--no-header",
        "-p", "no:cacheprovider",
    ]

    # pytest 退出码：0=全部通过, 1=有失败, 2=内部错误, 5=没有收集到测试
    # 我们不在此处 fail，只收集结果
    try:
        subprocess.run(
            cmd,
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        # 超时视为全部失败
        return {}, junit_path
    except Exception:
        return {}, junit_path

    return _parse_junit_xml(junit_path), junit_path


def _parse_junit_xml(junit_path: str) -> dict[str, dict]:
    """解析 JUnit XML，按测试文件聚合结果。

    pytest 默认把所有测试放进单个 testsuite name="pytest"，
    因此需要按 testcase 的 classname（dotted module path）分组到对应测试文件。

    返回: {test_file_relative: {total, passed, failed, skipped, failed_tests, duration_ms}}
    """
    results: dict[str, dict] = {}
    if not os.path.isfile(junit_path):
        return results

    try:
        tree = ET.parse(junit_path)
    except ET.ParseError:
        return results

    root = tree.getroot()

    # 收集所有 testcase，按 classname 前缀分组到测试文件
    # classname 格式: "tests.parser_contract.test_encoding_error.TestCanonicalizeContract"
    # 或 "tests.test_rust_python_alignment.TestRustPythonAlignment"
    file_groups: dict[str, list] = {}
    for tc in root.iter("testcase"):
        classname = tc.get("classname", "")
        gate_file = _classname_to_gate_file(classname)
        if not gate_file:
            continue
        file_groups.setdefault(gate_file, []).append(tc)

    # 聚合每个 gate_file 的结果
    for gate_file, testcases in file_groups.items():
        total = len(testcases)
        failed = 0
        skipped = 0
        failed_tests: list[str] = []
        total_duration = 0.0

        for tc in testcases:
            duration = float(tc.get("time", "0"))
            total_duration += duration

            has_failure = tc.find("failure") is not None
            has_error = tc.find("error") is not None
            has_skipped = tc.find("skipped") is not None

            if has_failure or has_error:
                failed += 1
                tc_name = tc.get("name", "")
                tc_class = tc.get("classname", "")
                failed_tests.append(f"{tc_class}::{tc_name}")
            elif has_skipped:
                skipped += 1

        passed = total - failed - skipped
        results[gate_file] = {
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "failed_tests": failed_tests,
            "duration_ms": round(total_duration * 1000, 1),
        }

    return results


def _classname_to_gate_file(classname: str) -> str:
    """将 testcase classname 映射到 _GATE_TEST_FILES 的 key。

    classname 格式（pytest 默认）:
      - "tests.parser_contract.test_encoding_error.TestCanonicalizeContract"
      - "tests.test_rust_python_alignment.TestRustPythonAlignment"
      - "tests.parser_contract.test_baseline.test_baseline_json_is_valid"

    映射规则: 提取 dotted path 中的测试文件名，匹配到 _GATE_TEST_FILES。
    """
    if not classname:
        return ""
    parts = classname.split(".")
    # 从 parts 中查找匹配的测试文件名（不含 .py）
    for gate_file in _GATE_TEST_FILES:
        # gate_file 如 "tests/parser_contract/test_encoding_error.py"
        # 提取文件名 stem: "test_encoding_error"
        stem = gate_file.rsplit("/", 1)[-1].rsplit("\\", 1)[-1][:-3]  # 去掉 .py
        if stem in parts:
            return gate_file
    return ""


def _build_gate_report(
    repo_root: str,
    per_gate_results: dict[str, dict],
    known_diffs: dict[str, Any],
    rust_available: bool,
) -> dict[str, Any]:
    """构建完整的门禁报告 JSON 结构。

    R1-P0-2 fail-closed 策略（独立复审 P0-2）：
    - 任一门禁 failed > 0 → fail
    - 任一门禁 skipped > 0 → fail（避免假绿：skip 等同于未验证）
    - 任一门禁 total == 0 → fail（0 比较项无法构成契约）
    - Rust 扩展不可用 → fail（parser 契约门禁无意义）
    - 测试结果未收集（pytest 超时/解析错误）→ fail
    """
    timestamp = _dt.datetime.now(_dt.timezone.utc).isoformat()

    gates: dict[str, Any] = {}
    overall_pass = True
    overall_fail_reasons: list[str] = []

    # R1-P0-2: Rust 扩展不可用 → 整体 fail-closed
    if not rust_available:
        overall_pass = False
        overall_fail_reasons.append(
            "callwarden_core (Rust extension) not available — parser contract gate cannot validate"
        )

    for gate_file, gate_name in _GATE_TEST_FILES.items():
        result = per_gate_results.get(gate_file, {})
        if not result:
            # 测试未运行或解析失败 → fail closed
            gates[gate_name] = {
                "status": "fail",
                "fail_reason": "test results not collected (pytest timeout or parse error)",
                "description": _GATE_DESCRIPTIONS.get(gate_name, ""),
                "test_file": gate_file,
            }
            overall_pass = False
            overall_fail_reasons.append(
                f"{gate_name}: test results not collected"
            )
            continue

        passed = result["passed"]
        failed = result["failed"]
        skipped = result["skipped"]
        total = result["total"]

        # R1-P0-2 fail-closed 判定（按优先级）：
        # 1. failed > 0: 有测试失败
        # 2. skipped > 0: 有测试被跳过（必须为 0，避免假绿）
        # 3. total == 0: 没有收集到测试（0 比较项无法构成契约）
        fail_reason = None
        if failed > 0:
            fail_reason = f"{failed} test(s) failed"
        elif skipped > 0:
            fail_reason = (
                f"{skipped} test(s) skipped (must be 0 for fail-closed; "
                "skipped 等同于未验证，禁止假绿)"
            )
        elif total == 0:
            fail_reason = "no tests collected (0 comparison items — contract is vacuous)"

        status = "pass" if fail_reason is None else "fail"
        if fail_reason is not None:
            overall_pass = False
            overall_fail_reasons.append(f"{gate_name}: {fail_reason}")

        gate_entry: dict[str, Any] = {
            "status": status,
            "description": _GATE_DESCRIPTIONS.get(gate_name, ""),
            "test_file": gate_file,
            "total": total,
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "duration_ms": result["duration_ms"],
            "failed_tests": result["failed_tests"],
        }
        if fail_reason is not None:
            gate_entry["fail_reason"] = fail_reason
        gates[gate_name] = gate_entry

    report = {
        "schema_version": "1.0",
        "generated_at": timestamp,
        "task_id": "T-1784986236713-5c859568",
        "task_title": "P0-A 建立 16 语言 ParseFact 契约与可信放行门禁",
        "overall_status": "pass" if overall_pass else "fail",
        "fail_closed": True,
        "fail_closed_policy": (
            "R1-P0-2: 任一门禁 failed>0 / skipped>0 / total==0 / "
            "Rust 扩展不可用 / 测试结果未收集 均判 fail（exit 1）。"
            "skipped 等同于未验证，禁止假绿；0 比较项无法构成契约。"
        ),
        "rust_extension_available": rust_available,
        "overall_fail_reasons": overall_fail_reasons if overall_fail_reasons else None,
        "environment": {
            "python_version": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "repo_root": repo_root.replace("\\", "/"),
        },
        "gates": gates,
        "known_differences": known_diffs,
        "ci_artifact": {
            "artifact_type": "parser_contract_gate_report",
            "persist_as": "tests/parser_contract/gate_report.json",
            "fail_closed_policy": (
                "任何门禁失败或未知差异导致 exit code 1，CI 必须拦截。"
                "已知差异白名单不计入失败，但必须经评审确认并在 known_differences 中显式记录。"
            ),
        },
        "languages_covered": [
            "rust", "typescript", "javascript", "python", "kotlin", "go",
            "java", "c", "cpp", "csharp", "ruby", "php", "swift", "scala",
            "hcl", "elixir",
        ],
    }
    return report


def main(argv: list[str] | None = None) -> int:
    """主入口：运行契约测试 → 生成 JSON 报告 → fail closed。

    Returns:
        0 = 全部门禁通过
        1 = 存在失败（fail closed）
    """
    parser = argparse.ArgumentParser(
        description="P0-A Step 6: 机器可读门禁报告生成器"
    )
    parser.add_argument(
        "--output",
        default=None,
        help="JSON 报告输出路径（默认: tests/parser_contract/gate_report.json）",
    )
    parser.add_argument(
        "--no-pytest",
        action="store_true",
        help="跳过 pytest 运行（仅输出已知差异和元数据；fail-closed 下所有门禁标记为 fail）",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="仓库根目录（默认: 自动检测）",
    )
    args = parser.parse_args(argv)

    # 自动检测仓库根目录（向上查找 pyproject.toml）
    repo_root = args.repo_root
    if not repo_root:
        here = Path(__file__).resolve().parent
        for parent in [here, *here.parents]:
            if (parent / "pyproject.toml").exists():
                repo_root = str(parent)
                break
        if not repo_root:
            repo_root = os.getcwd()

    repo_root = os.path.abspath(repo_root)
    output_path = args.output or os.path.join(
        repo_root, "tests", "parser_contract", "gate_report.json"
    )

    rust_available = _detect_rust_available()
    known_diffs = _collect_known_differences()

    if args.no_pytest:
        per_gate_results = {}
        for gate_file, gate_name in _GATE_TEST_FILES.items():
            per_gate_results[gate_file] = {
                "total": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "failed_tests": [],
                "duration_ms": 0,
            }
    else:
        per_gate_results, _junit_path = _run_pytest_contract_suite(repo_root)

    report = _build_gate_report(
        repo_root, per_gate_results, known_diffs, rust_available
    )

    # R1-P0-2: --no-pytest 时 fail-closed（跳过 pytest 等同于未验证，禁止假绿）
    # _build_gate_report 已通过 total==0 判定每个门禁为 fail，这里只补充 fail_reason
    if args.no_pytest:
        for gate_name in report["gates"]:
            gate = report["gates"][gate_name]
            gate["status"] = "fail"
            gate["fail_reason"] = gate.get(
                "fail_reason",
                "pytest skipped via --no-pytest (fail-closed: 未验证即失败)",
            )
        report["overall_status"] = "fail"
        reasons = report.get("overall_fail_reasons") or []
        if not any("no-pytest" in r for r in reasons):
            reasons.append("pytest skipped via --no-pytest (fail-closed)")
        report["overall_fail_reasons"] = reasons

    # 写 JSON 文件
    output_dir = os.path.dirname(output_path)
    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # stdout 输出 JSON（便于 CI 捕获）
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # fail closed: 任一门禁失败 → exit 1
    if report["overall_status"] == "fail":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
