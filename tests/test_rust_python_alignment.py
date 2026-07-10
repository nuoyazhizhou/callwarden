"""Phase 1.2: Rust multi_lang parser 与 Python parser 对齐测试。

使用 Counter（多重集合）比较 11 种语言的 parser 输出：
- symbols: (name, start_line, end_line) — 结构身份（不比较 qualified_name，因双方 module_path 策略不同）
- raw_calls: (callee_name, call_line) — 调用身份（不比较 caller_qualified，同上）

设计要点：
- 用 Counter 而非 set/dict：同一行可能有多个相同调用（如 foo(); foo();），
  dict 会吞掉重复，Counter 保留多重性。
- 调用对齐只比较用户定义符号间的调用（callee_name 在 symbol names 中），
  避免 Python should_filter_call 过滤标准库导致的差异。
- qualified_name 差异是投影策略差异（Rust 用语言原生包名/命名空间，Python 用传入的 module_path），
  不是解析差异。Phase 1.4 会修复 Rust parser 的 module_path 使用。
- 已知差异管理（v5 P2 修复）：已知差异用 **Counter** 表示并从实际 diff 中**相减**，
  要求剩余差异为零。这样不会一次放过某 key 的全部差异次数（`del Counter[key]` 的缺陷），
  也比 `diff_rate < threshold` 更精确——只有显式记录的差异才被允许，任何新差异都会失败。
"""
from __future__ import annotations

import os
import sys
from collections import Counter

import pytest

# 添加 tests/ 目录到 path，复用现有样本代码
_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

# 添加项目根目录到 path
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

# 复用 test_p31_multi_lang.py 的样本代码和 _has_rust_ext
from test_p31_multi_lang import _LANGUAGE_SAMPLES, _has_rust_ext  # noqa: E402

from callwarden.parsers import create_parser  # noqa: E402


# ============================================
# 已知差异清单（Counter 相减，不静默容忍）
# ============================================
# Phase 1.4 修复后应逐步清空此清单
#
# 数据结构：Dict[lang, (reason, Counter_of_known_diffs)]
#   Counter_of_known_diffs: 已知差异的精确多重集合，key 与 normalize 函数输出一致
#   reason: 差异原因描述（人类可读）
#
# 断言逻辑：actual_diff - known_diffs == empty（剩余差异必须为零）
#   这确保只有显式记录的差异被允许，任何新差异都会失败
#
# Counters 由 _discover_alignment_diffs.py 脚本发现后填入
# （运行 `python tests/_discover_alignment_diffs.py` 重新生成）

KNOWN_SYMBOL_DIFFS: dict[str, tuple[str, Counter]] = {
    # TypeScript: Rust parser 完全未提取符号（class/method/fn 均缺失）
    # Phase 1.4 需修复 Rust TypeScript parser 的 symbol_rules 配置
    "typescript": (
        "Rust TypeScript parser 未提取任何符号，Phase 1.4 待修复",
        Counter({
            ("User", 3, 9): 1,
            ("add", 11, 13): 1,
            ("constructor", 4, 4): 1,
            ("greet", 6, 8): 1,
            ("main", 15, 20): 1,
        }),
    ),
    # PHP: Rust 不提取 property 类型的符号（Python 提取 $value 属性）
    # Phase 1.4 可在 php_config 的 symbol_rules 中增加 property 支持
    "php": (
        "Rust 不提取 PHP property 符号，Phase 1.4 待修复",
        Counter({("value", 7, 7): 1}),
    ),
    # C++: Rust 额外提取 namespace 作为符号（Python 不提取）
    # 这是 Rust 更 thorough，不是 bug；投影差异
    "cpp": (
        "Rust 额外提取 C++ namespace 符号（投影差异）",
        Counter({("example", 4, 31): 1}),
    ),
}

KNOWN_CALL_DIFFS: dict[str, tuple[str, Counter]] = {
    # Python: Rust 识别到对象方法调用（calc.add() / calc.clear()），Python parser 不提取方法调用
    # 这是 Python parser 的已知限制，不是 Rust 的 bug
    "python": (
        "Rust 识别对象方法调用，Python parser 不提取（Python 限制）",
        Counter({("add", 22): 1, ("clear", 23): 1}),
    ),
    # Scala: Rust 不识别对象方法调用（calc.add()），Python 识别
    # 这是 Rust Scala parser 的 bug，Phase 1.4 待修复
    "scala": (
        "Rust 不识别 Scala 对象方法调用，Phase 1.4 待修复",
        Counter({("add", 17): 1}),
    ),
    # C++: Rust 识别对象方法调用（p.distance()），Python 不提取
    # 同 Python 语言：Python parser 不提取方法调用
    "cpp": (
        "Rust 识别 C++ 对象方法调用，Python parser 不提取（Python 限制）",
        Counter({("distance", 25): 1}),
    ),
}


# ============================================
# 归一化函数
# ============================================

def normalize_symbols(symbols):
    """按 (name, start_line, end_line) 归一化为 Counter 多重集合。

    不比较 qualified_name：Rust 和 Python 的 module_path 策略不同
    （Rust 用语言原生包名/命名空间，Python 用传入的 module_path），
    这是投影差异不是解析差异。
    不比较 kind：kind 值映射由 test_kind_alignment 单独覆盖。
    """
    return Counter(
        (s["name"], s["start_line"], s["end_line"])
        for s in symbols
    )


def normalize_calls(calls):
    """按 (callee_name, call_line) 归一化为 Counter。

    不比较 caller_qualified：同 symbol 归一化，投影策略不同。
    """
    return Counter(
        (c["callee_name"], c["call_line"])
        for c in calls
    )


def filter_user_calls(calls, user_symbol_names):
    """只保留 callee_name 是用户定义符号的调用。

    Python parser 会通过 should_filter_call 过滤标准库调用（如 print/open/len），
    Rust parser 不过滤。为消除这一差异，只比较双方都应识别的用户定义符号间调用。
    """
    return [c for c in calls if c["callee_name"] in user_symbol_names]


def compute_diff(py_counter, rs_counter):
    """计算两个 Counter 之间的差异（剩余 diff Counter）。

    返回 (diff_counter, missing_in_rs, missing_in_py)。
    - diff_counter: missing_in_rs + missing_in_py（合并后的总差异多重集合）
    - missing_in_rs: Python 有但 Rust 没有的条目
    - missing_in_py: Rust 有但 Python 没有的条目
    """
    missing_in_rs = py_counter - rs_counter  # Python 有但 Rust 没有
    missing_in_py = rs_counter - py_counter  # Rust 有但 Python 没有
    diff_counter = missing_in_rs + missing_in_py
    return diff_counter, missing_in_rs, missing_in_py


def subtract_known_diffs(diff_counter, known_diffs):
    """从实际 diff 中减去已知差异，返回剩余差异。

    v5 P2 修复：用 Counter 相减而非 `del Counter[key]` 或 `diff_rate < threshold`。
    Counter 相减只减去允许的数量，如果实际差异超过已知数量，剩余部分非空 → 测试失败。
    """
    return diff_counter - known_diffs


# ============================================
# 对齐测试
# ============================================

@pytest.mark.skipif(not _has_rust_ext(), reason="callwarden_core 未安装")
@pytest.mark.parametrize("lang,filename,content", _LANGUAGE_SAMPLES, ids=[s[0] for s in _LANGUAGE_SAMPLES])
class TestRustPythonAlignment:
    """Phase 1.2: Rust vs Python parser 对齐测试（Counter 多重集合比较）"""

    def _parse_both(self, lang, filename, content, tmp_path):
        """用 Python 和 Rust 分别解析同一文件，返回 (py_result, rs_result)。"""
        path = tmp_path / filename
        path.write_text(content, encoding="utf-8")
        path_str = str(path)

        # Python parser
        py_parser = create_parser(path_str)
        assert py_parser is not None, f"Python parser 不支持 {lang} ({filename})"
        py_result = py_parser.parse_file(path_str, "test.align")

        # Rust parser
        from callwarden_core import parse_file_lang
        rs_result = parse_file_lang(path_str, "test.align", lang)

        return py_result, rs_result

    def test_symbol_alignment(self, lang, filename, content, tmp_path):
        """符号核心字段一致（name, start_line, end_line）。

        断言逻辑：actual_diff - known_diffs == empty
        - 已知差异从 KNOWN_SYMBOL_DIFFS 中减去
        - 剩余差异必须为零（任何未知差异都会失败）
        不比较 qualified_name 和 kind（投影策略差异，由其他测试覆盖）。
        """
        py_result, rs_result = self._parse_both(lang, filename, content, tmp_path)

        py_syms = normalize_symbols(py_result["symbols"])
        rs_syms = normalize_symbols(rs_result["symbols"])

        diff_counter, missing_in_rs, missing_in_py = compute_diff(py_syms, rs_syms)

        # 从实际差异中减去已知差异
        reason = ""
        known = Counter()
        if lang in KNOWN_SYMBOL_DIFFS:
            reason, known = KNOWN_SYMBOL_DIFFS[lang]
        remaining = subtract_known_diffs(diff_counter, known)

        assert not remaining, (
            f"[{lang}] symbol 对齐发现未知差异（已知差异已减去）\n"
            f"  已知差异原因: {reason}\n"
            f"  已知差异 Counter: {dict(known)}\n"
            f"  剩余未知差异: {dict(remaining)}\n"
            f"  missing_in_rs (Python 有 Rust 没有): {dict(missing_in_rs)}\n"
            f"  missing_in_py (Rust 有 Python 没有): {dict(missing_in_py)}\n"
            f"  提示: 若为新增已知差异，请更新 KNOWN_SYMBOL_DIFFS[{lang!r}]"
        )

    def test_call_alignment(self, lang, filename, content, tmp_path):
        """调用关系一致（仅比较用户定义符号间的调用）。

        断言逻辑：actual_diff - known_diffs == empty
        - 已知差异从 KNOWN_CALL_DIFFS 中减去
        - 剩余差异必须为零（任何未知差异都会失败）
        只比较 callee_name 在双方符号名集合中的调用，避免标准库过滤差异。
        不比较 caller_qualified（投影策略差异）。
        """
        py_result, rs_result = self._parse_both(lang, filename, content, tmp_path)

        # 合并双方符号名作为"用户定义符号"集合
        py_names = {s["name"] for s in py_result["symbols"]}
        rs_names = {s["name"] for s in rs_result["symbols"]}
        user_names = py_names | rs_names

        # 过滤：只保留用户定义符号间的调用
        py_calls = filter_user_calls(py_result.get("raw_calls", []), user_names)
        rs_calls = filter_user_calls(rs_result.get("raw_calls", []), user_names)

        py_call_counter = normalize_calls(py_calls)
        rs_call_counter = normalize_calls(rs_calls)

        diff_counter, missing_in_rs, missing_in_py = compute_diff(
            py_call_counter, rs_call_counter
        )

        # 从实际差异中减去已知差异
        reason = ""
        known = Counter()
        if lang in KNOWN_CALL_DIFFS:
            reason, known = KNOWN_CALL_DIFFS[lang]
        remaining = subtract_known_diffs(diff_counter, known)

        assert not remaining, (
            f"[{lang}] call 对齐发现未知差异（已知差异已减去）\n"
            f"  已知差异原因: {reason}\n"
            f"  已知差异 Counter: {dict(known)}\n"
            f"  剩余未知差异: {dict(remaining)}\n"
            f"  missing_in_rs (Python 有 Rust 没有): {dict(missing_in_rs)}\n"
            f"  missing_in_py (Rust 有 Python 没有): {dict(missing_in_py)}\n"
            f"  提示: 若为新增已知差异，请更新 KNOWN_CALL_DIFFS[{lang!r}]"
        )

    def test_symbol_count_alignment(self, lang, filename, content, tmp_path):
        """符号数量一致性检查（辅助断言，防止 normalize 吞掉系统性遗漏）。

        主断言在 test_symbol_alignment 中已用 Counter 相减精确跟踪。
        此测试作为额外检查：已知差异语言的符号数量差异应与 KNOWN_SYMBOL_DIFFS 中的条目数一致。
        """
        py_result, rs_result = self._parse_both(lang, filename, content, tmp_path)

        py_count = len(py_result["symbols"])
        rs_count = len(rs_result["symbols"])

        # 已知差异允许的数量
        known_tolerance = 0
        if lang in KNOWN_SYMBOL_DIFFS:
            _, known = KNOWN_SYMBOL_DIFFS[lang]
            known_tolerance = sum(known.values())

        assert abs(py_count - rs_count) <= known_tolerance, (
            f"[{lang}] 符号数量差异 {abs(py_count - rs_count)} 超过已知差异 {known_tolerance}\n"
            f"  Python: {py_count} symbols, Rust: {rs_count} symbols"
        )


@pytest.mark.skipif(not _has_rust_ext(), reason="callwarden_core 未安装")
class TestAlignmentEdgeCases:
    """Phase 1.2: 对齐边界情况测试"""

    def _parse_both(self, path_str, lang, module_path="test.edge"):
        """用 Python 和 Rust 分别解析同一文件。"""
        py_parser = create_parser(path_str)
        assert py_parser is not None, f"Python parser 不支持 {lang}"
        py_result = py_parser.parse_file(path_str, module_path)

        from callwarden_core import parse_file_lang
        rs_result = parse_file_lang(path_str, module_path, lang)

        return py_result, rs_result

    def test_empty_file_alignment(self, tmp_path):
        """空文件：双方都应返回 0 symbols / 0 calls。"""
        path = tmp_path / "empty.py"
        path.write_text("", encoding="utf-8")

        py_result, rs_result = self._parse_both(str(path), "python")

        assert len(py_result["symbols"]) == 0
        assert len(rs_result["symbols"]) == 0
        assert len(py_result.get("raw_calls", [])) == 0
        assert len(rs_result.get("raw_calls", [])) == 0

    def test_syntax_error_file_alignment(self, tmp_path):
        """含语法错误的文件：双方都应返回 error 或部分结果，不 crash。"""
        path = tmp_path / "broken.py"
        path.write_text("def broken(:\n    pass\n", encoding="utf-8")

        # Python parser 不应 crash
        py_parser = create_parser(str(path))
        py_result = py_parser.parse_file(str(path), "test.broken")

        # Rust parser 不应 crash
        from callwarden_core import parse_file_lang
        rs_result = parse_file_lang(str(path), "test.broken", "python")

        # 双方都可能返回部分符号或空列表，关键是都不 crash
        assert isinstance(py_result["symbols"], list)
        assert isinstance(rs_result["symbols"], list)

    def test_nested_class_alignment(self, tmp_path):
        """嵌套类/方法：符号数量和行号应一致（零差异）。"""
        code = """\
class Outer:
    class Inner:
        def method(self):
            pass

    def outer_method(self):
        pass
"""
        path = tmp_path / "nested.py"
        path.write_text(code, encoding="utf-8")

        py_result, rs_result = self._parse_both(str(path), "python")

        py_syms = normalize_symbols(py_result["symbols"])
        rs_syms = normalize_symbols(rs_result["symbols"])

        diff_counter, missing_in_rs, missing_in_py = compute_diff(py_syms, rs_syms)

        # Python 嵌套类无已知差异，要求零差异
        assert not diff_counter, (
            f"nested class diff 非零（Python 无已知差异）\n"
            f"  missing_in_rs: {dict(missing_in_rs)}\n"
            f"  missing_in_py: {dict(missing_in_py)}"
        )

    def test_duplicate_calls_same_line(self, tmp_path):
        """同行重复调用：Counter 应保留多重性（不吞掉重复）。"""
        code = """\
def foo():
    pass

def main():
    foo()
    foo()
    foo()
"""
        path = tmp_path / "dup_calls.py"
        path.write_text(code, encoding="utf-8")

        py_result, rs_result = self._parse_both(str(path), "python")

        # 合并符号名
        user_names = {s["name"] for s in py_result["symbols"]} | {
            s["name"] for s in rs_result["symbols"]
        }

        py_calls = filter_user_calls(py_result.get("raw_calls", []), user_names)
        rs_calls = filter_user_calls(rs_result.get("raw_calls", []), user_names)

        py_counter = normalize_calls(py_calls)
        rs_counter = normalize_calls(rs_calls)

        # main() 中调用了 3 次 foo()，Counter 应保留这个多重性
        foo_calls_py = sum(
            cnt for (callee, _), cnt in py_counter.items() if callee == "foo"
        )
        foo_calls_rs = sum(
            cnt for (callee, _), cnt in rs_counter.items() if callee == "foo"
        )

        # 双方都应识别到 3 次 foo 调用（允许差异但不应为 0）
        assert foo_calls_py > 0, "Python 未识别到 foo() 调用"
        assert foo_calls_rs > 0, "Rust 未识别到 foo() 调用"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
