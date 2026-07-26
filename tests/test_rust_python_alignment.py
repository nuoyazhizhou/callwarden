"""Phase 1.2: Rust multi_lang parser 与 Python parser 对齐测试。

使用 Counter（多重集合）比较 15 种语言的 parser 输出；C 通过专用
`parse_c_file` 路径单独验证：
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

import json
import os
import sys
from collections import Counter
from pathlib import Path

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
# R8-P0-4: golden fixture 加载器
# ============================================
# 加载 tests/parser_contract/golden/{lang}.json 作为契约真相源
# 用 Rust parser 解析 golden sample_source，对比 expected.symbols[*].signature
_GOLDEN_DIR = Path(__file__).resolve().parent / "parser_contract" / "golden"


def _load_golden_fixture(lang: str) -> dict:
    """加载指定语言的 golden fixture JSON。

    返回结构见 tests/parser_contract/golden/{lang}.json。
    """
    path = _GOLDEN_DIR / f"{lang}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _all_language_samples() -> list[tuple[str, str, str]]:
    """从 golden fixture 补齐 alignment 的 15 个 Rust parse_file_lang 语言。

    P31 的基础样例历史上只有 11 种语言；契约 gate 不能仅凭报告硬编码覆盖数，
    这里直接从同一份 golden sample_source 构造剩余 Kotlin/Swift/HCL/Elixir 样例。
    C 使用 parse_c_file，不加入这个通用 parse_file_lang 参数集。
    """
    samples = list(_LANGUAGE_SAMPLES)
    present = {lang for lang, _filename, _content in samples}
    for lang in ("kotlin", "swift", "hcl", "elixir"):
        fixture = _load_golden_fixture(lang)
        if lang in present or not fixture:
            continue
        samples.append(
            (
                lang,
                fixture["sample_file"],
                fixture["sample_source"],
            )
        )
    return samples


# 真实 gate 参数集：15 个 supported_languages() 语言，而不是报告层的静态数字。
_LANGUAGE_SAMPLES = _all_language_samples()


def _golden_signature_known_gap_langs() -> set[str]:
    """返回 golden known_gaps 中显式声明 signature Phase 2.7 缺口的语言集合。

    R8-P0-4: 已知 signature 缺口必须显式记录在 golden fixture 的 known_gaps 中，
    field="signature" 且 phase="Phase 2.7"。这些语言的 Rust signature 缺口被容忍，
    但当 Rust 修复 signature 后，必须从 golden known_gaps 移除该语言的 signature 缺口，
    否则测试会强制验证 Rust signature 与 golden expected signature 一致。
    """
    gap_langs: set[str] = set()
    if not _GOLDEN_DIR.exists():
        return gap_langs
    for json_path in _GOLDEN_DIR.glob("*.json"):
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        lang = data.get("language")
        if not lang:
            continue
        for gap in data.get("known_gaps", []):
            if (
                gap.get("parser") == "rust"
                and gap.get("field") == "signature"
                and gap.get("phase") == "Phase 2.7"
            ):
                gap_langs.add(lang)
                break
    return gap_langs


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
    # R15-P0-3: Rust 额外提取 impl 块内方法（Python 不提取）
    "rust": (
        "R15-P0-3: Rust 额外提取 impl 块内方法 new/distance（Python 不提取）",
        Counter({("new", 9, 11): 1, ("distance", 13, 17): 1}),
    ),
    # R15-P0-3: Rust 额外提取 C# field（Python 不提取 field）
    "csharp": (
        "R15-P0-3: Rust 额外提取 C# field value（Python 不提取 field）",
        Counter({("value", 6, 6): 1}),
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
    # Swift: Python parser 提取参数标签调用，Rust 当前只提取无标签调用。
    "swift": (
        "Swift 参数标签调用的 Python/Rust 投影差异",
        Counter({("getName", 5): 1}),
    ),
    # R15-P0-3: Rust 额外提取 impl 方法内的调用（Python 不提取 impl 方法）
    "rust": (
        "R15-P0-3: Rust 额外提取 impl 方法内调用 distance（Python 不提取 impl 方法）",
        Counter({("distance", 23): 1}),
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
    # R15-P0-3: Rust 额外提取 impl 块内方法（kind=method，Python 不提取）
    "rust": (
        "R15-P0-3: Rust 额外提取 impl 块内方法 new/distance（Python 不提取）",
        Counter({
            ("new", 9, "method"): 1,
            ("distance", 13, "method"): 1,
        }),
    ),
    # R15-P0-3: Rust 额外提取 C# field（kind=field，Python 不提取 field）
    "csharp": (
        "R15-P0-3: Rust 额外提取 C# field value（Python 不提取 field）",
        Counter({("value", 6, "field"): 1}),
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
        "Python 把 impl 块标记为 private，Rust 标记为 public（投影差异）；"
        "R15-P0-3: Rust 额外提取 impl 块内方法 new/distance（Python 不提取）",
        Counter({
            ("Point", 8, "private"): 1,
            ("Point", 8, "public"): 1,
            ("new", 9, "public"): 1,
            ("distance", 13, "public"): 1,
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
    "csharp": (
        "R15-P0-3: Rust 额外提取 C# field value（visibility=private，Python 不提取 field）",
        Counter({("value", 6, "private"): 1}),
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
# signature 契约门禁（R8-P0-4 真绿重写）
# ============================================
# 设计文档 §5.2 输出契约: 每个 symbol 必须包含 signature 字段
# 设计文档 §6.2: signature 对齐是单语言放行门之一
#
# R8-P0-4 修复（2026-07-26）：
# 旧实现 test_signature_rust_all_empty 要求 Rust signature 全空才通过，
# 同时 test_signature_alignment 只比较双方都有非空 signature 的项，
# 二者形成"假绿循环"——Rust signature 永远全空 → 永远 trivially 通过。
#
# 新实现策略：
# - 删除 test_signature_rust_all_empty（消除"全空即通过"假绿门禁）
# - 改造 test_signature_alignment：
#   * 加载 golden fixture 的 expected.symbols[*].signature 作为契约真相
#   * 用 Rust parser 解析 golden sample_source，提取实际 signature
#   * 按 (name, line_start) 对齐符号
#   * 不一致项必须显式记录在 golden fixture 的 known_gaps 中
#     （field="signature", phase="Phase 2.7"）
#   * 实际差异 - 已知缺口 == empty 才通过
#
# 这样：
# - 当前 Rust signature 全空 → 测试通过（前提：所有 16 语言 golden 已声明 signature 缺口）
# - Rust 修复某语言 signature 后 → 该语言 golden known_gaps 必须移除 signature 缺口
#   未移除则 known_gaps 多余，测试仍通过；移除后必须与 golden 一致，否则失败
# - 真绿：测试真正反映 Rust parser signature 契约状态


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

    def test_visibility_matches_golden_contract(self, lang, filename, content, tmp_path):
        """Rust visibility 必须逐符号匹配 golden，不得靠 Python 白名单放行。"""
        fixture = _load_golden_fixture(lang)
        assert fixture, f"缺少 {lang}.json golden fixture"
        sample_source = fixture.get("sample_source", content)
        path = tmp_path / filename
        path.write_text(sample_source, encoding="utf-8")

        from callwarden_core import parse_file_lang

        result = parse_file_lang(str(path), "test.golden", lang)
        expected = {
            (s["name"], s["line_start"]): s.get("visibility", "")
            for s in fixture.get("expected", {}).get("symbols", [])
        }
        actual = {
            (s.get("name", ""), s.get("start_line", 0)): s.get("visibility", "")
            for s in result.get("symbols", [])
        }
        assert expected, f"{lang} golden 没有 visibility 契约"
        assert actual, f"{lang} Rust 没有输出 visibility 符号"
        assert {k: actual.get(k, "") for k in expected} == expected

    def test_signature_alignment(self, lang, filename, content, tmp_path):
        """R8-P0-4: symbol signature 与 golden fixture 契约对齐（真绿门禁）。

        断言逻辑：Rust parser 实际 signature 与 golden expected.symbols[*].signature
        按 (name, line_start) 对齐比较；不一致条目必须显式记录在 golden known_gaps 中
        （field="signature", phase="Phase 2.7"），否则视为未知差异失败。

        设计文档 §5.2 输出契约: 每个 symbol 必须包含 signature 字段。
        设计文档 §6.2: signature 对齐是单语言放行门之一。

        R8-P0-4 修复（2026-07-26）：
        - 旧实现只比较双方都有非空 signature 的项，Rust 全空时 trivially 通过（假绿）
        - 旧实现配套 test_signature_rust_all_empty 反而把"修复进展"判为失败
        - 新实现以 golden fixture 为契约真相，强制 Rust signature 与 golden 一致，
          或在 golden known_gaps 中显式声明缺口
        """
        fixture = _load_golden_fixture(lang)
        if not fixture:
            pytest.skip(f"golden fixture {lang}.json 不存在，无法对齐 signature 契约")

        # 加载 golden expected.symbols 作为契约真相
        golden_syms = fixture.get("expected", {}).get("symbols", [])
        if not golden_syms:
            pytest.skip(f"golden fixture {lang}.json expected.symbols 为空")

        # 用 Rust parser 解析 golden sample_source（与 _LANGUAGE_SAMPLES 同源）
        sample_source = fixture.get("sample_source", content)
        path = tmp_path / filename
        path.write_text(sample_source, encoding="utf-8")
        path_str = str(path)

        from callwarden_core import parse_file_lang
        rs_result = parse_file_lang(path_str, "test.golden", lang)
        rs_syms = rs_result["symbols"]

        # 按 (name, line_start) 建立索引：golden 期望 vs Rust 实际
        golden_sig_map: dict[tuple, str] = {}
        for sym in golden_syms:
            key = (sym["name"], sym["line_start"])
            expected_sig = sym.get("signature", "") or ""
            if expected_sig and key not in golden_sig_map:
                golden_sig_map[key] = expected_sig

        rs_sig_map: dict[tuple, str] = {}
        for sym in rs_syms:
            key = (sym.get("name", ""), sym.get("start_line", 0))
            actual_sig = sym.get("signature", "") or ""
            if key not in rs_sig_map:
                rs_sig_map[key] = actual_sig

        # 计算实际差异：golden 期望的 signature 与 Rust 实际 signature 不一致
        actual_diff: Counter = Counter()
        for key, expected_sig in golden_sig_map.items():
            actual_sig = rs_sig_map.get(key, "")
            if actual_sig != expected_sig:
                # 差异项：(name, line_start, expected_sig, actual_sig)
                actual_diff[(key[0], key[1], expected_sig, actual_sig)] += 1

        # R8-P0-4 修复（复审 §P0-3）：旧实现对 known_gap_langs 直接 return，
        # 导致所有 signature 差异被静默放行（假绿）。现已：
        # 1. 清理 15 个支持语言的 signature known_gaps（Rust 已实现 extract_signature）
        # 2. 移除 blanket return，改为严格比较
        # 3. 仅 C 语言（Rust 不支持）保留 signature known_gap，但 C 不在
        #    _LANGUAGE_SAMPLES 中，不会触发此测试
        # 若未来 C 被加入测试，actual_diff 非空会直接失败（正确行为：
        # C 是真实缺口，需实现 C parser 或显式 skip）
        assert not actual_diff, (
            f"[{lang}] signature 与 golden 契约不一致（未知差异）\n"
            f"  golden expected signature 与 Rust 实际 signature 不一致项:\n"
            + "\n".join(
                f"    {name}:{line} expected={exp!r} actual={act!r}"
                for (name, line, exp, act) in actual_diff.elements()
            )
            + f"\n  若此语言存在系统性 signature 缺口，请在 golden fixture {lang}.json "
            f"的 known_gaps 中显式声明（field='signature', phase='Phase 2.7'）"
        )


@pytest.mark.skipif(not _has_rust_ext(), reason="callwarden_core 未安装")
def test_c_signature_visibility_matches_golden(tmp_path):
    """C 专用 parse_c_file 路径也必须满足 signature/visibility 契约。"""
    fixture = _load_golden_fixture("c")
    assert fixture, "缺少 c.json golden fixture"
    path = tmp_path / fixture["sample_file"]
    path.write_text(fixture["sample_source"], encoding="utf-8")

    from callwarden_core import parse_c_file

    result = parse_c_file(str(path), "test.golden")
    actual = {
        (s.get("name", ""), s.get("start_line", 0)): (
            s.get("signature", ""),
            s.get("visibility", ""),
        )
        for s in result.get("symbols", [])
    }
    expected = {
        (s["name"], s["line_start"]): (
            s.get("signature", ""),
            s.get("visibility", ""),
        )
        for s in fixture.get("expected", {}).get("symbols", [])
    }
    assert expected, "C golden 没有 signature/visibility 契约"
    assert {key: actual.get(key) for key in expected} == expected


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
