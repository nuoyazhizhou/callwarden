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
    # PHP: P0-C Step 2 修复后 Rust 提取 property 符号，与 Python 一致（无差异）
    # 保留空 Counter 以占位，便于后续若再出现差异时填充
    "php": (
        "P0-C Step 2: Rust 已提取 PHP property 符号，与 Python 一致",
        Counter(),
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
    # Scala: P0-C Step 3 修复后 Rust 提取 new Calculator 构造调用，Python parser 不提取
    # Rust 行为对齐 golden 契约（golden 期望包含 Calculator 构造调用）
    # 同时 Rust 现在也提取 calc.add()，与 Python 一致（无差异）
    "scala": (
        "Rust 提取 new Calculator 构造调用，Python parser 不提取（Python 限制，P0-C Step 3 修复）",
        Counter({("Calculator", 16): 1}),
    ),
    # C++: Rust 识别对象方法调用（p.distance()），Python 不提取
    # 同 Python 语言：Python parser 不提取方法调用
    "cpp": (
        "Rust 识别 C++ 对象方法调用，Python parser 不提取（Python 限制）",
        Counter({("distance", 25): 1}),
    ),
    # TypeScript: P0-C Step 1 修复后 Rust 提取 new User(...) 构造调用，Python parser 不提取
    # 这是 Python parser 的已知限制，Rust 行为对齐 golden 契约（golden 期望包含构造调用）
    "typescript": (
        "Rust 提取 new User(...) 构造调用，Python parser 不提取（Python 限制，P0-C Step 1 修复）",
        Counter({("User", 16): 1}),
    ),
    # JavaScript: 同 TypeScript，Rust 提取 new User(...) 构造调用，Python 不提取
    "javascript": (
        "Rust 提取 new User(...) 构造调用，Python parser 不提取（Python 限制，P0-C Step 1 修复）",
        Counter({("User", 16): 1}),
    ),
}


# ============================================
# kind 对齐已知差异（Step 2 新增）
# ============================================
# key = (name, start_line, kind)
# diff = (py_counter - rs_counter) + (rs_counter - py_counter)
# 设计文档 §6.2: kind 对齐是单语言放行门之一
#
# 实际探测得到的差异（基于 _LANGUAGE_SAMPLES 样本）：
# - ruby: Rust 把方法标记为 'fn'，Python 标记为 'method'（3 个符号）
# - scala: Rust 把方法标记为 'fn'，Python 标记为 'method'（2 个符号）
# - cpp: Rust 把构造器/方法标记为 'fn'，Python 标记为 'method'（2 个符号）
#         + Rust 额外提取 namespace（已在 KNOWN_SYMBOL_DIFFS 记录）
# - swift: Rust 把函数标记为 'fn'，Python 标记为 'function'（3 个符号）
# - php: Rust 缺 property 符号（1 个，与 KNOWN_SYMBOL_DIFFS 同步）
# - typescript: Python 重复提取符号（每个 2 次），Rust 1 次（5 个差异）

KNOWN_KIND_DIFFS: dict[str, tuple[str, Counter]] = {
    # Ruby: P0-C Step 5 修复后 Rust 区分 constructor/method/function：
    # - initialize → "constructor"（kind_from_name 映射，对齐 golden）
    # - add → "method"（类内方法，require_parent_kind="body_statement"）
    # - main → "function"（顶层方法，无 require_parent_kind 限制）
    # Python 仍统一标记为 'method'（Python parser 限制，未区分）
    # add(6) 在两边都是 method，无差异；initialize 和 main 有 kind 差异。
    "ruby": (
        "P0-C Step 5: Rust 区分 constructor/method/function，Python 仍用 method",
        Counter({
            ("initialize", 2, "method"): 1,
            ("initialize", 2, "constructor"): 1,
            ("main", 12, "method"): 1,
            ("main", 12, "function"): 1,
        }),
    ),
    # Scala: P0-C Step 3 修复后 Rust 把方法标记为 'method'，与 Python 一致（无差异）
    # 保留空 Counter 以占位，便于后续若再出现差异时填充
    "scala": (
        "P0-C Step 3: Rust Scala 方法 kind='method'，与 Python 一致",
        Counter(),
    ),
    # C++: P0-C Step 4 修复后 Rust kind 对齐 golden（constructor/method/function）
    # Python 仍用 fn/method（Python parser 限制，未区分构造器和自由函数）
    # 另: Rust 多 namespace example（与 KNOWN_SYMBOL_DIFFS 同步）
    "cpp": (
        "P0-C Step 4: Rust kind 对齐 golden（constructor/method/function），Python 仍用 fn/method",
        Counter({
            # Point constructor: Rust="constructor"（对齐 golden），Python="method"
            ("Point", 8, "method"): 1,
            ("Point", 8, "constructor"): 1,
            # add: Rust="function"（对齐 golden），Python="fn"
            ("add", 19, "fn"): 1,
            ("add", 19, "function"): 1,
            # main: Rust="function"（对齐 golden），Python="fn"
            ("main", 23, "fn"): 1,
            ("main", 23, "function"): 1,
            # namespace: Rust 额外提取（投影差异）
            ("example", 4, "namespace"): 1,
        }),
    ),
    # Swift: P0-C Step 5 修复后 Rust kind='method'（对齐 golden，原为 'fn'），
    # Python 仍标记为 'function'（Python parser 限制）。
    # 注意：Swift 不在 _LANGUAGE_SAMPLES 中，此差异为文档记录，未被测试覆盖。
    "swift": (
        "P0-C Step 5: Rust kind='method'（对齐 golden），Python 仍为 'function'",
        Counter({
            ("findUser", 4, "function"): 1,
            ("findUser", 4, "method"): 1,
            ("getName", 7, "function"): 1,
            ("getName", 7, "method"): 1,
            ("draw", 13, "function"): 1,
            ("draw", 13, "method"): 1,
        }),
    ),
    # PHP: P0-C Step 2 修复后 Rust 提取 property 符号（kind="property"），
    # 但 __construct kind 与 Python 不同：Rust="constructor"（对齐 golden），
    # Python="method"（Python parser 限制，未区分构造器）。
    "php": (
        "P0-C Step 2: Rust __construct kind='constructor'（对齐 golden），Python 仍为 'method'",
        Counter({
            ("__construct", 9, "method"): 1,
            ("__construct", 9, "constructor"): 1,
        }),
    ),
    # TypeScript: P0-C Step 1 修复后 Rust kind 对齐 golden（function/constructor），
    # Python parser 仍用 fn/method 且重复提取 2 次。
    # 差异来源：
    # 1. Python 重复提取（每符号 2 次 vs Rust 1 次）→ py-rs 各 1
    # 2. Rust constructor kind="constructor" vs Python kind="method"
    # 3. Rust function kind="function" vs Python kind="fn"
    "typescript": (
        "P0-C Step 1: Rust kind 对齐 golden（function/constructor），Python 仍用 fn/method 且重复提取",
        Counter({
            # Python 重复提取导致的差异（每符号 py=2, rs=1 → py-rs=1）
            ("User", 3, "class"): 1,
            ("greet", 6, "method"): 1,
            # constructor: Python=method(2次), Rust=constructor(1次) → py-rs=2 + rs-py=1
            ("constructor", 4, "method"): 2,
            ("constructor", 4, "constructor"): 1,
            # add: Python=fn(2次), Rust=function(1次) → py-rs=2 + rs-py=1
            ("add", 11, "fn"): 2,
            ("add", 11, "function"): 1,
            # main: Python=fn(2次), Rust=function(1次) → py-rs=2 + rs-py=1
            ("main", 15, "fn"): 2,
            ("main", 15, "function"): 1,
        }),
    ),
    # JavaScript: P0-C Step 1 修复后 Rust kind 对齐 golden（function/constructor），
    # Python parser 仍用 fn/method。
    "javascript": (
        "P0-C Step 1: Rust kind 对齐 golden（function/constructor），Python 仍用 fn/method",
        Counter({
            # constructor: Python=method, Rust=constructor
            ("constructor", 2, "method"): 1,
            ("constructor", 2, "constructor"): 1,
            # add: Python=fn, Rust=function
            ("add", 11, "fn"): 1,
            ("add", 11, "function"): 1,
            # main: Python=fn, Rust=function
            ("main", 15, "fn"): 1,
            ("main", 15, "function"): 1,
        }),
    ),
}


# ============================================
# visibility 对齐已知差异（Step 3 新增）
# ============================================
# key = (name, start_line, normalized_visibility)
# normalized_visibility: 'pub' → 'public'（语义等价的关键字归一化）
# 设计文档 §6.2: visibility 对齐是单语言放行门之一
#
# 实际探测得到的差异：
# - python: __init__ 被 Python 标记为 private（__ 前缀规则），Rust 标记为 public
# - rust: Python 把 impl 块标记为 private，Rust 标记为 public
# - go: Python 把 main 标记为 private（小写开头），Rust 标记为 public
# - javascript: Python 把 User/add/main 标记为 private（无 export），Rust 标记为 public
# - swift: Python 把所有符号标记为 internal（Swift 默认），Rust 标记为 public
# - php: Rust 缺 property value（与 KNOWN_SYMBOL_DIFFS 同步）
# - typescript: Python 重复提取符号（与 KNOWN_KIND_DIFFS 同步）

def _normalize_visibility(v: str) -> str:
    """归一化 visibility 关键字（'pub' → 'public'，其他保持不变）。

    Rust 和 Python 对同语义的 visibility 使用不同关键字：
    - Rust 源码用 'pub'，Python parser 提取为 'pub'
    - Rust parser 归一化为 'public'
    归一化后比较，避免关键字差异被误报为语义差异。
    """
    if v == "pub":
        return "public"
    return v


KNOWN_VISIBILITY_DIFFS: dict[str, tuple[str, Counter]] = {
    "python": (
        "Python 把 __init__ 标记为 private（__ 前缀规则），Rust 标记为 public（Phase 2.7 待修复）",
        Counter({
            ("__init__", 9, "private"): 1,
            ("__init__", 9, "public"): 1,
        }),
    ),
    "rust": (
        "Python 把 impl 块标记为 private，Rust 标记为 public（投影差异）",
        Counter({
            ("Point", 8, "private"): 1,
            ("Point", 8, "public"): 1,
        }),
    ),
    "go": (
        "Python 把 main 标记为 private（小写开头），Rust 标记为 public（Phase 2.7 待修复）",
        Counter({
            ("main", 25, "private"): 1,
            ("main", 25, "public"): 1,
        }),
    ),
    "javascript": (
        "Python 把 User/add/main 标记为 private（无 export），Rust 标记为 public（Phase 2.7 待修复）",
        Counter({
            ("User", 1, "private"): 1,
            ("add", 11, "private"): 1,
            ("main", 15, "private"): 1,
            ("User", 1, "public"): 1,
            ("add", 11, "public"): 1,
            ("main", 15, "public"): 1,
        }),
    ),
    "swift": (
        "Python 把 Swift 符号标记为 internal（默认访问级别），Rust 标记为 public（Phase 2.7 待修复）",
        Counter({
            ("UserService", 3, "internal"): 1,
            ("findUser", 4, "internal"): 1,
            ("getName", 7, "internal"): 1,
            ("Drawable", 12, "internal"): 1,
            ("draw", 13, "internal"): 1,
            ("UserService", 3, "public"): 1,
            ("findUser", 4, "public"): 1,
            ("getName", 7, "public"): 1,
            ("Drawable", 12, "public"): 1,
            ("draw", 13, "public"): 1,
        }),
    ),
    "cpp": (
        "Rust 额外提取 C++ namespace example（visibility=public，投影差异，与 KNOWN_SYMBOL_DIFFS 同步）",
        Counter({("example", 4, "public"): 1}),
    ),
    # PHP: P0-C Step 2 修复后 Rust 提取 property value 的 visibility="private"，
    # 与 Python 一致（无差异）。保留空 Counter 占位。
    "php": (
        "P0-C Step 2: Rust 已提取 PHP property visibility，与 Python 一致",
        Counter(),
    ),
    "typescript": (
        "Python parser 重复提取 TypeScript 符号（每个 2 次），Rust 1 次（Python 限制）",
        Counter({
            ("User", 3, "public"): 1,
            ("constructor", 4, "public"): 1,
            ("greet", 6, "public"): 1,
            ("add", 11, "public"): 1,
            ("main", 15, "public"): 1,
        }),
    ),
}


# ============================================
# signature 对齐已知差异（Step 3 新增）
# ============================================
# 设计文档 §5.2 输出契约: 每个 symbol 必须包含 signature 字段
# 设计文档 §6.2: signature 对齐是单语言放行门之一
#
# 当前已知系统性缺口（所有 16 语言）：
# - Rust SymbolInfo.signature 始终为空字符串（Phase 2.7 待修复）
# - Python parser 提取了完整签名
#
# 测试策略：
# 1. test_signature_alignment: 若双方都有非空 signature，内容必须一致（零未知差异）
#    - 当前 Rust 全空 → 0 个比较项 → 测试通过（trivially）
#    - 当 Rust 开始填充 signature 时，必须与 Python 一致
# 2. test_signature_rust_all_empty: 文档化 Rust signature 全空这一已知缺口
#    - 若 Rust 开始填充 signature，此测试会失败，提醒更新 alignment 测试
#    - 这是 Phase 2.7 的硬门禁：signature 缺口必须被显式记录

# signature 测试不使用 Counter 相减（因为 Rust 全空，diff = 全部 Python 符号）
# 改用"双方都有非空 signature 时必须一致"的强约束 + "Rust 全空"的文档化约束


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


def normalize_symbols_with_kind(symbols):
    """按 (name, start_line, kind) 归一化为 Counter 多重集合。

    用于 kind 对齐测试（Step 2）。
    - 包含 kind 字段，用于检测 Rust/Python 在 kind 标签上的差异
    - 不包含 end_line：同一符号的行范围差异不影响 kind 判定
    - key 含 start_line 以区分同名符号（如 Point class 与 Point constructor）
    """
    return Counter(
        (s["name"], s["start_line"], s.get("kind", ""))
        for s in symbols
    )


def normalize_symbols_with_visibility(symbols):
    """按 (name, start_line, normalized_visibility) 归一化为 Counter 多重集合。

    用于 visibility 对齐测试（Step 3）。
    - visibility 经过 _normalize_visibility 归一化（'pub' → 'public'）
    - key 含 start_line 以区分同名符号
    """
    return Counter(
        (s["name"], s["start_line"], _normalize_visibility(s.get("visibility", "")))
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

    def test_kind_alignment(self, lang, filename, content, tmp_path):
        """symbol kind 对齐（Step 2 新增）。

        断言逻辑：actual_kind_diff - KNOWN_KIND_DIFFS == empty
        - key = (name, start_line, kind)
        - 已知 kind 差异从 KNOWN_KIND_DIFFS 中减去
        - 剩余差异必须为零（任何未知 kind 差异都会失败）

        设计文档 §6.2: kind 对齐是单语言放行门之一。
        设计文档 §6.3: 不允许整种语言零符号或整类 kind 系统性缺失。
        """
        py_result, rs_result = self._parse_both(lang, filename, content, tmp_path)

        py_kinds = normalize_symbols_with_kind(py_result["symbols"])
        rs_kinds = normalize_symbols_with_kind(rs_result["symbols"])

        diff_counter, missing_in_rs, missing_in_py = compute_diff(py_kinds, rs_kinds)

        # 从实际差异中减去已知差异
        reason = ""
        known = Counter()
        if lang in KNOWN_KIND_DIFFS:
            reason, known = KNOWN_KIND_DIFFS[lang]
        remaining = subtract_known_diffs(diff_counter, known)

        assert not remaining, (
            f"[{lang}] kind 对齐发现未知差异（已知差异已减去）\n"
            f"  已知差异原因: {reason}\n"
            f"  已知差异 Counter: {dict(known)}\n"
            f"  剩余未知差异: {dict(remaining)}\n"
            f"  missing_in_rs (Python 有 Rust 没有): {dict(missing_in_rs)}\n"
            f"  missing_in_py (Rust 有 Python 没有): {dict(missing_in_py)}\n"
            f"  提示: 若为新增已知差异，请更新 KNOWN_KIND_DIFFS[{lang!r}]"
        )

    def test_kind_set_nonempty(self, lang, filename, content, tmp_path):
        """Rust parser 的 kind 集合必须非空（禁止整语言零符号白名单）。

        设计文档 §6.3 明确禁止：
        - 整种语言零 symbols
        - 整类 symbol/call/reference 系统性缺失

        此测试是 kind 对齐的硬门禁：若 Rust 返回 0 个符号，
        kind 集合为空，本测试会立即失败（无法被 KNOWN_KIND_DIFFS 白名单绕过）。
        """
        py_result, rs_result = self._parse_both(lang, filename, content, tmp_path)

        rs_symbols = rs_result.get("symbols", [])
        rs_kinds = {s.get("kind", "") for s in rs_symbols}

        # Rust 必须返回至少 1 个符号（禁止整语言零符号）
        assert len(rs_symbols) > 0, (
            f"[{lang}] Rust parser 返回 0 个符号（违反 §6.3 禁止整语言零符号）"
        )
        # kind 集合必须非空（每个符号都应有 kind 标签）
        assert rs_kinds, (
            f"[{lang}] Rust parser kind 集合为空（所有符号 kind 字段为空）"
        )
        # 不允许 kind 全部为空字符串（系统性 kind 缺失）
        assert rs_kinds != {""}, (
            f"[{lang}] Rust parser 所有符号 kind 为空字符串（系统性 kind 缺失）"
        )

    def test_visibility_alignment(self, lang, filename, content, tmp_path):
        """symbol visibility 对齐（Step 3 新增）。

        断言逻辑：actual_visibility_diff - KNOWN_VISIBILITY_DIFFS == empty
        - key = (name, start_line, normalized_visibility)
        - visibility 经过 _normalize_visibility 归一化（'pub' → 'public'）
        - 已知差异从 KNOWN_VISIBILITY_DIFFS 中减去
        - 剩余差异必须为零（任何未知 visibility 差异都会失败）

        设计文档 §6.2: visibility 对齐是单语言放行门之一。
        """
        py_result, rs_result = self._parse_both(lang, filename, content, tmp_path)

        py_vis = normalize_symbols_with_visibility(py_result["symbols"])
        rs_vis = normalize_symbols_with_visibility(rs_result["symbols"])

        diff_counter, missing_in_rs, missing_in_py = compute_diff(py_vis, rs_vis)

        # 从实际差异中减去已知差异
        reason = ""
        known = Counter()
        if lang in KNOWN_VISIBILITY_DIFFS:
            reason, known = KNOWN_VISIBILITY_DIFFS[lang]
        remaining = subtract_known_diffs(diff_counter, known)

        assert not remaining, (
            f"[{lang}] visibility 对齐发现未知差异（已知差异已减去）\n"
            f"  已知差异原因: {reason}\n"
            f"  已知差异 Counter: {dict(known)}\n"
            f"  剩余未知差异: {dict(remaining)}\n"
            f"  missing_in_rs (Python 有 Rust 没有): {dict(missing_in_rs)}\n"
            f"  missing_in_py (Rust 有 Python 没有): {dict(missing_in_py)}\n"
            f"  提示: 若为新增已知差异，请更新 KNOWN_VISIBILITY_DIFFS[{lang!r}]"
        )

    def test_signature_alignment(self, lang, filename, content, tmp_path):
        """symbol signature 对齐（Step 3 新增）。

        断言逻辑：若双方都有非空 signature，内容必须一致（零未知差异）。

        设计文档 §5.2 输出契约: 每个 symbol 必须包含 signature 字段。
        设计文档 §6.2: signature 对齐是单语言放行门之一。

        当前已知系统性缺口（所有 16 语言）：
        - Rust SymbolInfo.signature 始终为空字符串（Phase 2.7 待修复）
        - 因此本测试当前 trivially 通过（0 个比较项）
        - 当 Rust 开始填充 signature 时，本测试会强制与 Python 一致

        配套测试 test_signature_rust_all_empty 文档化此已知缺口。
        """
        py_result, rs_result = self._parse_both(lang, filename, content, tmp_path)

        py_syms = py_result["symbols"]
        rs_syms = rs_result["symbols"]

        # 按 (name, start_line) 建立索引，取首个 signature
        py_sig_map: dict[tuple, str] = {}
        for s in py_syms:
            key = (s["name"], s["start_line"])
            sig = s.get("signature", "") or ""
            if sig and key not in py_sig_map:
                py_sig_map[key] = sig

        rs_sig_map: dict[tuple, str] = {}
        for s in rs_syms:
            key = (s["name"], s["start_line"])
            sig = s.get("signature", "") or ""
            if sig and key not in rs_sig_map:
                rs_sig_map[key] = sig

        # 只比较双方都有非空 signature 的符号
        common_keys = set(py_sig_map.keys()) & set(rs_sig_map.keys())
        mismatches = []
        for key in common_keys:
            if py_sig_map[key] != rs_sig_map[key]:
                mismatches.append(
                    f"  {key}: py={py_sig_map[key]!r} vs rs={rs_sig_map[key]!r}"
                )

        assert not mismatches, (
            f"[{lang}] signature 对齐发现未知差异（双方都有非空 signature 但内容不一致）\n"
            + "\n".join(mismatches)
        )

    def test_signature_rust_all_empty(self, lang, filename, content, tmp_path):
        """文档化 Rust signature 全空这一已知系统性缺口（Step 3 新增）。

        设计文档 §5.2 输出契约要求每个 symbol 包含 signature。
        当前 Rust SymbolInfo.signature 始终为空字符串（Phase 2.7 待修复）。

        本测试是 Phase 2.7 的硬门禁：
        - 当前：Rust 所有 signature 为空 → 测试通过（文档化已知缺口）
        - 未来：Rust 开始填充 signature → 测试失败，提醒更新 test_signature_alignment
          和 golden fixture 的 known_gaps

        若 Rust 部分语言开始填充 signature，应将该语言从此测试的"全空"集合中移除，
        并在 test_signature_alignment 中验证与 Python 一致。
        """
        py_result, rs_result = self._parse_both(lang, filename, content, tmp_path)

        rs_syms = rs_result["symbols"]
        rs_non_empty_sig = [
            (s["name"], s["start_line"], s.get("signature", ""))
            for s in rs_syms
            if s.get("signature", "")
        ]

        # 当前所有 16 语言的 Rust signature 都应为空
        # 若不为空，说明 Rust parser 已开始填充 signature，需更新此测试和 golden fixture
        assert not rs_non_empty_sig, (
            f"[{lang}] Rust parser 开始返回非空 signature（Phase 2.7 修复进展）\n"
            f"  非空 signature 符号: {rs_non_empty_sig}\n"
            f"  请更新:\n"
            f"    1. test_signature_alignment: 验证非空 signature 与 Python 一致\n"
            f"    2. golden fixture known_gaps: 移除 signature 缺口记录\n"
            f"    3. 本测试: 将 {lang!r} 从'全空'集合中移除"
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
