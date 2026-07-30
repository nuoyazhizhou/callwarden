"""Phase 5-3 差分测试：Rust 兼容输出层 vs Python 真相源

覆盖契约 D1-D6 测试矩阵：
- D1: colorize
- D2: should_use_color
- D3: 预定义消息函数（success/error/warning/info/dim/bold）
- D4: format_duration
- D5: format_size
- D6: json_dumps_pretty

契约：docs/design/phase5-3-output-layer-contract.md §4
"""

import json
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ============================================================
# Python 真相源（内联，对齐 cli/console.py）
# ============================================================

# 对齐 cli/console.py L17-33
_COLORS = {
    "reset": "\033[0m",
    "bold": "\033[1m",
    "dim": "\033[2m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "bright_red": "\033[91m",
    "bright_green": "\033[92m",
    "bright_yellow": "\033[93m",
    "bright_blue": "\033[94m",
    "bright_cyan": "\033[96m",
}


def py_colorize(text: str, color: str, use_color: bool) -> str:
    """Python 真相源：cli/console.py:colorize() (L115-130)"""
    if not use_color:
        return text
    code = _COLORS.get(color, "")
    if not code:
        return text
    return f"{code}{text}{_COLORS['reset']}"


def py_should_use_color(no_color: bool, is_tty: bool, force_color: bool, vt_enabled: bool) -> bool:
    """Python 真相源：cli/console.py:should_use_color() (L84-112)

    参数化版本（原函数从环境变量和 stdout 检测）。
    """
    if no_color:
        return False
    if not is_tty:
        return False
    if force_color:
        return True
    return vt_enabled


def py_success(msg: str, use_color: bool) -> str:
    """Python 真相源：cli/console.py:success() (L149-158)"""
    return py_colorize(f"\u2713 {msg}", "green", use_color)


def py_error(msg: str, use_color: bool) -> str:
    """Python 真相源：cli/console.py:error() (L161-170)"""
    return py_colorize(f"\u2717 {msg}", "red", use_color)


def py_warning(msg: str, use_color: bool) -> str:
    """Python 真相源：cli/console.py:warning() (L173-182)"""
    return py_colorize(f"\u26a0 {msg}", "yellow", use_color)


def py_info(msg: str, use_color: bool) -> str:
    """Python 真相源：cli/console.py:info() (L185-194)"""
    return py_colorize(f"\u2139 {msg}", "blue", use_color)


def py_dim(msg: str, use_color: bool) -> str:
    """Python 真相源：cli/console.py:dim() (L197-206)"""
    return py_colorize(msg, "dim", use_color)


def py_bold(msg: str, use_color: bool) -> str:
    """Python 真相源：cli/console.py:bold() (L209-220)"""
    return py_colorize(msg, "bold", use_color)


def py_format_duration(seconds: float) -> str:
    """Python 真相源：cli/console.py:format_duration() (L274-297)"""
    if seconds < 0.001:
        return f"{seconds*1000:.1f}ms"
    if seconds < 1:
        return f"{seconds*1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    m = int(seconds // 60)
    s = seconds % 60
    if m < 60:
        return f"{m}m{s:.0f}s"
    h = int(m // 60)
    m = m % 60
    return f"{h}h{m}m"


def py_format_size(n: int) -> str:
    """Python 真相源：cli/console.py:format_size() (L300-313)"""
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f} KB"
    return f"{n/1024/1024:.1f} MB"


def py_json_dumps_pretty(data) -> str:
    """Python 真相源：json.dumps(data, indent=2, ensure_ascii=False)

    输入是 Python 对象（dict/list/str/int 等）。
    """
    return json.dumps(data, indent=2, ensure_ascii=False)


# ============================================================
# Rust 实现（通过 PyO3 调用）
# ============================================================

import callwarden_core as cc


# ============================================================
# D1: colorize
# ============================================================

def test_d1_colorize():
    """D1: colorize — ANSI 颜色码包裹"""
    print("=== D1: colorize ===")

    test_cases = [
        # (text, color, use_color, expected)
        ("hello", "red", True, f"\033[31mhello\033[0m"),      # D1.1
        ("hello", "red", False, "hello"),                      # D1.2
        ("hello", "unknown", True, "hello"),                   # D1.3
        ("", "red", True, f"\033[31m\033[0m"),                 # D1.4
        ("test", "green", True, f"\033[32mtest\033[0m"),
        ("test", "bold", True, f"\033[1mtest\033[0m"),
        ("test", "bright_cyan", True, f"\033[96mtest\033[0m"),
    ]

    all_match = True
    for text, color, use_color, expected in test_cases:
        py_result = py_colorize(text, color, use_color)
        rs_result = cc.colorize_py(text, color, use_color)
        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} text={text!r:8s} color={color:12s} use_color={str(use_color):5s} "
              f"expected={expected!r} py={py_result!r} rs={rs_result!r}")

    assert all_match, "D1: colorize mismatch"
    print("  D1: ALL PASS\n")


# ============================================================
# D2: should_use_color
# ============================================================

def test_d2_should_use_color():
    """D2: should_use_color — 三层判定"""
    print("=== D2: should_use_color ===")

    test_cases = [
        # (no_color, is_tty, force_color, vt_enabled, expected)
        (True,  True,  True,  True,  False),  # D2.1
        (False, False, True,  True,  False),  # D2.2
        (False, True,  True,  False, True),   # D2.3
        (False, True,  False, True,  True),   # D2.4
        (False, True,  False, False, False),  # D2.5
    ]

    all_match = True
    for no_color, is_tty, force_color, vt_enabled, expected in test_cases:
        py_result = py_should_use_color(no_color, is_tty, force_color, vt_enabled)
        rs_result = cc.should_use_color_py(no_color, is_tty, force_color, vt_enabled)
        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} no_color={str(no_color):5s} is_tty={str(is_tty):5s} "
              f"force_color={str(force_color):5s} vt_enabled={str(vt_enabled):5s} "
              f"expected={str(expected):5s} py={str(py_result):5s} rs={str(rs_result):5s}")

    assert all_match, "D2: should_use_color mismatch"
    print("  D2: ALL PASS\n")


# ============================================================
# D3: 预定义消息函数
# ============================================================

def test_d3_predefined_messages():
    """D3: success/error/warning/info/dim/bold"""
    print("=== D3: 预定义消息函数 ===")

    test_cases = [
        # (function_name, msg, use_color, expected)
        ("success", "done", True, f"\033[32m\u2713 done\033[0m"),    # D3.1
        ("error", "fail", True, f"\033[31m\u2717 fail\033[0m"),     # D3.2
        ("warning", "warn", True, f"\033[33m\u26a0 warn\033[0m"),   # D3.3
        ("info", "info", True, f"\033[34m\u2139 info\033[0m"),       # D3.4
        ("dim", "dim", True, f"\033[2mdim\033[0m"),                 # D3.5
        ("bold", "bold", True, f"\033[1mbold\033[0m"),               # D3.6
        ("success", "done", False, "\u2713 done"),                   # D3.7
    ]

    all_match = True
    for func_name, msg, use_color, expected in test_cases:
        # Python 真相源
        py_funcs = {
            "success": py_success,
            "error": py_error,
            "warning": py_warning,
            "info": py_info,
            "dim": py_dim,
            "bold": py_bold,
        }
        # Rust 实现
        rs_funcs = {
            "success": cc.success_py,
            "error": cc.error_py,
            "warning": cc.warning_py,
            "info": cc.info_py,
            "dim": cc.dim_py,
            "bold": cc.bold_py,
        }

        py_result = py_funcs[func_name](msg, use_color)
        rs_result = rs_funcs[func_name](msg, use_color)
        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} {func_name:8s} msg={msg!r:8s} use_color={str(use_color):5s} "
              f"expected={expected!r} py={py_result!r} rs={rs_result!r}")

    assert all_match, "D3: predefined messages mismatch"
    print("  D3: ALL PASS\n")


# ============================================================
# D4: format_duration
# ============================================================

def test_d4_format_duration():
    """D4: format_duration — 时长格式化"""
    print("=== D4: format_duration ===")

    test_cases = [
        # (seconds, expected)
        (0.0005, "0.5ms"),     # D4.1
        (0.12, "120ms"),       # D4.2
        (3.5, "3.5s"),         # D4.3
        (150.0, "2m30s"),      # D4.4
        (3900.0, "1h5m"),      # D4.5
        (0.0, "0.0ms"),        # D4.6
        (0.999, "999ms"),
        (1.0, "1.0s"),
        (59.9, "59.9s"),
        (60.0, "1m0s"),
        (3600.0, "1h0m"),
        (5400.0, "1h30m"),
    ]

    all_match = True
    for seconds, expected in test_cases:
        py_result = py_format_duration(seconds)
        rs_result = cc.format_duration_py(seconds)
        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} seconds={seconds:8.4f} expected={expected:8s} "
              f"py={py_result:8s} rs={rs_result}")

    assert all_match, "D4: format_duration mismatch"
    print("  D4: ALL PASS\n")


# ============================================================
# D5: format_size
# ============================================================

def test_d5_format_size():
    """D5: format_size — 字节大小格式化"""
    print("=== D5: format_size ===")

    test_cases = [
        # (n, expected)
        (0, "0 B"),           # D5.1
        (512, "512 B"),       # D5.2
        (1023, "1023 B"),     # D5.3
        (1024, "1.0 KB"),     # D5.4
        (1536, "1.5 KB"),     # D5.5
        (1048576, "1.0 MB"),  # D5.6
        (1572864, "1.5 MB"),  # D5.7
        (2048, "2.0 KB"),
        (512000, "500.0 KB"),
        (5242880, "5.0 MB"),
    ]

    all_match = True
    for n, expected in test_cases:
        py_result = py_format_size(n)
        rs_result = cc.format_size_py(n)
        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} n={n:10d} expected={expected:10s} py={py_result:10s} rs={rs_result}")

    assert all_match, "D5: format_size mismatch"
    print("  D5: ALL PASS\n")


# ============================================================
# D6: json_dumps_pretty
# ============================================================

def test_d6_json_dumps_pretty():
    """D6: json_dumps_pretty — JSON 序列化（缩进 + 非 ASCII 不转义）"""
    print("=== D6: json_dumps_pretty ===")

    # Python 真相源接受 Python 对象，Rust 接受 JSON 字符串
    # 差分测试：Python 先 dumps 为紧凑 JSON，再传给 Rust 重新序列化
    test_cases = [
        # (python_obj, expected_json_str)
        ({"a": 1, "b": 2}, '{\n  "a": 1,\n  "b": 2\n}'),                    # D6.1
        ({"name": "中文"}, '{\n  "name": "中文"\n}'),                        # D6.2
        ([1, 2, 3], '[\n  1,\n  2,\n  3\n]'),                               # D6.3
        ({"outer": {"inner": "value"}}, '{\n  "outer": {\n    "inner": "value"\n  }\n}'),  # D6.5
        ({"nested": {"deep": {"deeper": "end"}}},
         '{\n  "nested": {\n    "deep": {\n      "deeper": "end"\n    }\n  }\n}'),
        ([{"a": 1}, {"b": 2}], '[\n  {\n    "a": 1\n  },\n  {\n    "b": 2\n  }\n]'),
        ({"emoji": "🎉"}, '{\n  "emoji": "🎉"\n}'),  # Unicode emoji
        ({"empty": {}}, '{\n  "empty": {}\n}'),
        ([], '[]'),
        ({}, '{}'),
        (None, 'null'),
        (True, 'true'),
        ("string", '"string"'),
        (42, '42'),
    ]

    all_match = True
    for py_obj, expected in test_cases:
        # Python 真相源：接受 Python 对象
        py_result = py_json_dumps_pretty(py_obj)
        # Rust 实现：接受 JSON 字符串，先紧凑序列化再传给 Rust
        compact_json = json.dumps(py_obj, ensure_ascii=False)
        rs_result = cc.json_dumps_pretty_py(compact_json)
        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} obj={str(py_obj)[:30]:30s} expected={expected!r}")
        if not match:
            print(f"         py={py_result!r}")
            print(f"         rs={rs_result!r}")

    # D6.4: 无效 JSON 返回 null
    rs_invalid = cc.json_dumps_pretty_py("not valid json")
    match = rs_invalid == "null"
    status = "PASS" if match else "FAIL"
    if not match:
        all_match = False
    print(f"  {status} invalid_json expected='null' rs={rs_invalid!r}")

    assert all_match, "D6: json_dumps_pretty mismatch"
    print("  D6: ALL PASS\n")


# ============================================================
# 主入口
# ============================================================

def main():
    print("Phase 5-3 差分测试：Rust 兼容输出层 vs Python 真相源\n")

    tests = [
        test_d1_colorize,
        test_d2_should_use_color,
        test_d3_predefined_messages,
        test_d4_format_duration,
        test_d5_format_size,
        test_d6_json_dumps_pretty,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ASSERTION FAILED: {e}\n")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}\n")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Phase 5-3 差分测试结果：{passed} passed, {failed} failed")
    print(f"{'='*60}")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
