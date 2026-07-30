"""Phase 5-1 C 差分测试：Rust stats 子命令业务逻辑 vs Python 真相源

覆盖契约 D1-D4 测试矩阵：
- D1: 有效 JSON 输入（10 场景，含嵌套/数组/中文/emoji/空对象/null/数字/字符串）
- D2: 无效 JSON 输入（3 场景，空字符串/损坏 JSON/不完整 JSON）
- D3: 与 Python `_handle_stats` 行为对齐（输出格式 + exit code + 输出目标）
- D4: 真实 stats 数据样例（对齐 db.get_stats() 输出结构）

契约：docs/design/phase5-1c-stats-vertical-slice-contract.md §4
"""

import json
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import callwarden_core as cc


# ============================================================
# Python 真相源（内联，对齐 cli/main.py:_handle_stats）
# ============================================================

def py_handle_stats(stats_dict):
    """Python 真相源：cli/main.py:_handle_stats() (L6623-6636)

    输入：stats dict（模拟 db.get_stats() 返回值）
    输出：(exit_code, stdout, stderr) 三元组
    """
    try:
        # 对齐 Python: print(json.dumps(stats, indent=2, ensure_ascii=False))
        stdout = json.dumps(stats_dict, indent=2, ensure_ascii=False)
        return (0, stdout, "")
    except Exception as e:
        return (1, "", f"error: {e}")


# ============================================================
# D1: 有效 JSON 输入
# ============================================================

def test_d1_valid_json():
    """D1: 有效 JSON 输入（10 场景）"""
    print("=== D1: 有效 JSON 输入 ===")

    # D1.1 - D1.10：测试数据（Python 对象 + 期望的 pretty JSON 字符串）
    test_cases = [
        # (name, py_obj, expected_json_string)
        ("简单对象", {"a": 1}, '{\n  "a": 1\n}'),
        ("嵌套对象", {"outer": {"inner": "v"}}, '{\n  "outer": {\n    "inner": "v"\n  }\n}'),
        ("数组", [1, 2, 3], '[\n  1,\n  2,\n  3\n]'),
        ("中文", {"name": "中文"}, '{\n  "name": "中文"\n}'),
        ("emoji", {"emoji": "🎉"}, '{\n  "emoji": "🎉"\n}'),
        ("空对象", {}, '{}'),
        ("空数组", [], '[]'),
        ("null", None, 'null'),
        ("数字", 42, '42'),
        ("字符串", "hello", '"hello"'),
    ]

    all_pass = True
    for name, py_obj, expected in test_cases:
        # Python 真相源
        py_exit, py_stdout, py_stderr = py_handle_stats(py_obj)

        # Rust 实现：传入 compact JSON 字符串（模拟 db.get_stats() 结果序列化后传入）
        compact_json = json.dumps(py_obj, ensure_ascii=False)
        rs_exit, rs_stdout, rs_stderr = cc.stats_command_run_py(compact_json)

        # 差分对比
        exit_match = py_exit == rs_exit
        stdout_match = py_stdout == rs_stdout == expected
        stderr_match = py_stderr == rs_stderr

        ok = exit_match and stdout_match and stderr_match
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False

        print(f"  {status} {name:8s}")
        if not ok:
            print(f"    expected: exit={py_exit} stdout={expected!r} stderr={py_stderr!r}")
            print(f"    py:       exit={py_exit} stdout={py_stdout!r} stderr={py_stderr!r}")
            print(f"    rs:       exit={rs_exit} stdout={rs_stdout!r} stderr={rs_stderr!r}")

    if all_pass:
        print("  D1: ALL PASS")
    else:
        print("  D1: FAILED")
    return all_pass


# ============================================================
# D2: 无效 JSON 输入
# ============================================================

def test_d2_invalid_json():
    """D2: 无效 JSON 输入（3 场景）"""
    print("\n=== D2: 无效 JSON 输入 ===")

    test_cases = [
        ("空字符串", ""),
        ("损坏 JSON", "{invalid}"),
        ("不完整 JSON", '{"a":'),
    ]

    all_pass = True
    for name, invalid_json in test_cases:
        # Python 真相源：json.dumps 不会失败（输入是 dict），
        # 但如果传入字符串，json.dumps 会输出 quoted 字符串。
        # 对齐场景：Rust 端接收的是已序列化的 JSON，无效 JSON 应返回 exit 1。
        # Python 端在 _handle_stats 中，db.get_stats() 总是返回 dict，不会传入无效 JSON。
        # 所以这里只验证 Rust 行为。

        rs_exit, rs_stdout, rs_stderr = cc.stats_command_run_py(invalid_json)

        # 期望：exit 1，stdout 空，stderr 非空
        ok = (rs_exit == 1 and rs_stdout == "" and rs_stderr != "")
        status = "PASS" if ok else "FAIL"
        if not ok:
            all_pass = False

        print(f"  {status} {name:12s} exit={rs_exit} stderr={rs_stderr!r}")

    if all_pass:
        print("  D2: ALL PASS")
    else:
        print("  D2: FAILED")
    return all_pass


# ============================================================
# D3: 与 Python `_handle_stats` 行为对齐
# ============================================================

def test_d3_python_alignment():
    """D3: 与 Python `_handle_stats` 行为对齐"""
    print("\n=== D3: 与 Python `_handle_stats` 行为对齐 ===")

    all_pass = True

    # D3.1: 有效 stats - Python json.dumps vs Rust json_dumps_pretty
    stats_dict = {"total_files": 100, "total_symbols": 5000, "by_kind": {"fn": 3000, "struct": 200}}
    py_exit, py_stdout, py_stderr = py_handle_stats(stats_dict)
    compact_json = json.dumps(stats_dict, ensure_ascii=False)
    rs_exit, rs_stdout, rs_stderr = cc.stats_command_run_py(compact_json)

    ok = (py_exit == rs_exit == 0 and py_stdout == rs_stdout and py_stderr == rs_stderr)
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  {status} D3.1 有效 stats - 输出格式一致")
    if not ok:
        print(f"    py: exit={py_exit} stdout={py_stdout!r}")
        print(f"    rs: exit={rs_exit} stdout={rs_stdout!r}")

    # D3.2: exit code - Python return True → exit 0；Rust exit_code = 0
    ok = (py_exit == 0 and rs_exit == 0)
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  {status} D3.2 exit code - 都是 0")

    # D3.3: 输出目标 - Python print() → stdout；Rust stdout 字段
    ok = (py_stdout == rs_stdout and py_stdout is not None)
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  {status} D3.3 输出目标 - stdout 字段一致")

    if all_pass:
        print("  D3: ALL PASS")
    else:
        print("  D3: FAILED")
    return all_pass


# ============================================================
# D4: 真实 stats 数据样例
# ============================================================

def test_d4_real_stats_structure():
    """D4: 真实 stats 数据样例（对齐 db.get_stats() 输出结构）"""
    print("\n=== D4: 真实 stats 数据样例 ===")

    all_pass = True

    # D4.1: 完整的 db.get_stats() 输出结构（所有字段）
    stats_dict = {
        "total_files": 150,
        "unique_symbol_contents": 8000,
        "total_symbols": 12000,
        "commented": 5000,
        "total_calls": 45000,
        "cross_file_calls": 12000,
        "resolved_calls": 38000,
        "total_file_versions": 200,
        "current_files": 150,
        "multi_version_files": 50,
        "total_file_symbol_links": 18000,
        "total_call_versions": 60000,
        "by_kind": {"fn": 8000, "struct": 1500, "enum": 500},
        "depth_distribution": {"0": 1000, "1": 3000, "2": 2500},
    }

    py_exit, py_stdout, py_stderr = py_handle_stats(stats_dict)
    compact_json = json.dumps(stats_dict, ensure_ascii=False)
    rs_exit, rs_stdout, rs_stderr = cc.stats_command_run_py(compact_json)

    # 完全一致
    ok = (py_exit == rs_exit == 0 and py_stdout == rs_stdout)
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  {status} D4.1 完整 stats 结构 - 完全一致")
    if not ok:
        print(f"    py: {py_stdout!r}")
        print(f"    rs: {rs_stdout!r}")

    # D4.2: 验证关键字段都在输出中
    key_fields = [
        "total_files", "total_symbols", "commented", "total_calls",
        "cross_file_calls", "resolved_calls", "by_kind", "depth_distribution",
    ]
    missing_fields = [f for f in key_fields if f'"{f}":' not in rs_stdout]
    ok = len(missing_fields) == 0
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  {status} D4.2 关键字段完整 - 缺失字段: {missing_fields}")

    # D4.3: 空工作区（stats_dict 全为 0/空）
    empty_stats = {
        "total_files": 0,
        "total_symbols": 0,
        "total_calls": 0,
        "by_kind": {},
        "depth_distribution": {},
    }
    py_exit, py_stdout, _ = py_handle_stats(empty_stats)
    compact_json = json.dumps(empty_stats, ensure_ascii=False)
    rs_exit, rs_stdout, _ = cc.stats_command_run_py(compact_json)
    ok = (py_exit == rs_exit == 0 and py_stdout == rs_stdout)
    status = "PASS" if ok else "FAIL"
    if not ok:
        all_pass = False
    print(f"  {status} D4.3 空工作区 - 输出一致")

    if all_pass:
        print("  D4: ALL PASS")
    else:
        print("  D4: FAILED")
    return all_pass


# ============================================================
# 主入口
# ============================================================

def main():
    print("Phase 5-1 C 差分测试：Rust stats 子命令业务逻辑 vs Python 真相源\n")

    tests = [
        test_d1_valid_json,
        test_d2_invalid_json,
        test_d3_python_alignment,
        test_d4_real_stats_structure,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            result = test()
            if result:
                passed += 1
            else:
                failed += 1
        except Exception as e:
            print(f"  ERROR: {e}\n")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Phase 5-1 C 差分测试结果：{passed} passed, {failed} failed")
    print(f"{'='*60}")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
