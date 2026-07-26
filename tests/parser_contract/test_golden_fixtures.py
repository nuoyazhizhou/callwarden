"""P0-A Step 1: golden contract fixtures 结构与 provenance 校验测试。

验证 16 语言 golden fixture 的结构完整性和 provenance 可追溯性。
本测试**不**与当前 parser 实际输出对比（那会在 Step 2-5 的 alignment 测试中做），
只验证 fixture 本身是有效的、人工确认的契约真相。

设计文档 §6.1：
    - 人工确认预期符号、调用、引用、签名、可见性和范围
    - 是移除 Python reference 后的长期真相源
    - 任何输出变化必须显式更新 fixture 和原因
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

_GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# 16 语言全集（与 generate_baseline.py _ALL_LANGUAGE_SAMPLES 对齐）
_EXPECTED_LANGUAGES = {
    "python", "rust", "go", "java", "typescript", "javascript",
    "ruby", "php", "scala", "csharp", "cpp",
    "kotlin", "swift", "elixir",
    "hcl", "c",
}


def _load_fixture(lang: str) -> dict:
    """加载指定语言的 golden fixture JSON。"""
    path = _GOLDEN_DIR / f"{lang}.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _fixture_exists(lang: str) -> bool:
    return (_GOLDEN_DIR / f"{lang}.json").exists()


# ============================================
# 结构完整性测试
# ============================================

@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_exists(lang):
    """每种语言的 golden fixture 文件必须存在。"""
    assert _fixture_exists(lang), (
        f"golden fixture 缺失: {_GOLDEN_DIR / f'{lang}.json'}"
    )


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_has_required_top_level_fields(lang):
    """每个 fixture 必须包含 5 个顶层字段：language/sample_file/sample_source/provenance/expected/known_gaps。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    required = {"language", "sample_file", "sample_source", "provenance", "expected", "known_gaps"}
    missing = required - set(data.keys())
    assert not missing, f"{lang}.json 缺失字段: {missing}"


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_language_matches_filename(lang):
    """fixture 的 language 字段必须与文件名一致。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    assert data["language"] == lang, (
        f"{lang}.json language={data['language']!r} 与文件名不匹配"
    )


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_sample_source_nonempty(lang):
    """sample_source 必须非空（至少 20 字节，确保是真实源码）。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    source = data["sample_source"]
    assert isinstance(source, str), f"{lang}.json sample_source 必须是字符串"
    assert len(source) >= 20, f"{lang}.json sample_source 过短 ({len(source)} bytes)"


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_expected_has_required_subfields(lang):
    """expected 必须包含 symbols/raw_calls/imports/references 4 个子字段。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    expected = data["expected"]
    required = {"symbols", "raw_calls", "imports", "references"}
    missing = required - set(expected.keys())
    assert not missing, f"{lang}.json expected 缺失子字段: {missing}"


# ============================================
# 符号契约测试（设计文档 §5.2 输出契约）
# ============================================

@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_symbols_nonempty(lang):
    """每语言 expected.symbols 必须非空（不允许整语言零符号）。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    symbols = data["expected"]["symbols"]
    assert len(symbols) > 0, (
        f"{lang}.json expected.symbols 为空（违反设计文档 §6.3 不允许整种语言零 symbols）"
    )


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_symbol_has_required_fields(lang):
    """每个 symbol 必须包含 name/kind/signature/visibility/lexical_parent/line_start/line_end。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    required = {
        "name", "kind", "signature", "visibility",
        "lexical_parent", "line_start", "line_end",
    }
    for i, sym in enumerate(data["expected"]["symbols"]):
        missing = required - set(sym.keys())
        assert not missing, (
            f"{lang}.json expected.symbols[{i}] 缺失字段: {missing} (symbol={sym.get('name')})"
        )


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_symbol_lines_valid(lang):
    """每个 symbol 的 line_start <= line_end，且都在 sample_source 行数范围内。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    source_lines = data["sample_source"].count("\n") + 1
    for i, sym in enumerate(data["expected"]["symbols"]):
        assert sym["line_start"] <= sym["line_end"], (
            f"{lang}.json symbols[{i}] line_start={sym['line_start']} > line_end={sym['line_end']}"
        )
        assert 1 <= sym["line_start"] <= source_lines, (
            f"{lang}.json symbols[{i}] line_start={sym['line_start']} 越界 (源码 {source_lines} 行)"
        )
        assert 1 <= sym["line_end"] <= source_lines, (
            f"{lang}.json symbols[{i}] line_end={sym['line_end']} 越界 (源码 {source_lines} 行)"
        )


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_symbol_visibility_valid(lang):
    """每个 symbol 的 visibility 必须是已知枚举值。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    valid = {"public", "private", "protected", "internal", "package", "package-private"}
    for i, sym in enumerate(data["expected"]["symbols"]):
        assert sym["visibility"] in valid, (
            f"{lang}.json symbols[{i}] visibility={sym['visibility']!r} 不在 {valid}"
        )


# ============================================
# 调用契约测试
# ============================================

@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_call_has_required_fields(lang):
    """每个 raw_call 必须包含 caller_name/callee_name/callee_module/ordinal/line。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    required = {"caller_name", "callee_name", "callee_module", "ordinal", "line"}
    for i, call in enumerate(data["expected"]["raw_calls"]):
        missing = required - set(call.keys())
        assert not missing, (
            f"{lang}.json expected.raw_calls[{i}] 缺失字段: {missing}"
        )


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_call_lines_within_source(lang):
    """每个 call 的 line 必须在 sample_source 行数范围内。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    source_lines = data["sample_source"].count("\n") + 1
    for i, call in enumerate(data["expected"]["raw_calls"]):
        assert 1 <= call["line"] <= source_lines, (
            f"{lang}.json raw_calls[{i}] line={call['line']} 越界 (源码 {source_lines} 行)"
        )


# ============================================
# import 契约测试
# ============================================

@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_import_has_required_fields(lang):
    """每个 import 必须包含 source_text 和 normalized_target。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    required = {"source_text", "normalized_target"}
    for i, imp in enumerate(data["expected"]["imports"]):
        missing = required - set(imp.keys())
        assert not missing, (
            f"{lang}.json expected.imports[{i}] 缺失字段: {missing}"
        )


# ============================================
# references 契约测试（HCL 等）
# ============================================

def test_hcl_fixture_has_references():
    """HCL fixture 必须包含至少 1 个 reference（attribute traversal 引用）。"""
    if not _fixture_exists("hcl"):
        pytest.skip("hcl.json 不存在")
    data = _load_fixture("hcl")
    refs = data["expected"]["references"]
    assert len(refs) > 0, "hcl.json references 必须非空（HCL 的 attribute 引用是核心契约）"
    required = {"caller_name", "callee_name", "line", "reference_kind", "source_text"}
    for i, ref in enumerate(refs):
        missing = required - set(ref.keys())
        assert not missing, f"hcl.json references[{i}] 缺失字段: {missing}"


# ============================================
# provenance 可追溯性测试
# ============================================

@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_provenance_complete(lang):
    """每个 fixture 的 provenance 必须包含 source/confirmed_by/confirmed_at/commit_sha/method 5 字段。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    prov = data["provenance"]
    required = {"source", "confirmed_by", "confirmed_at", "commit_sha", "method"}
    missing = required - set(prov.keys())
    assert not missing, f"{lang}.json provenance 缺失字段: {missing}"


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_provenance_source_documented(lang):
    """provenance.source 必须非空且指向测试样本来源。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    source = data["provenance"]["source"]
    assert source, f"{lang}.json provenance.source 为空"
    # source 应指向 test_p31_multi_lang.py 或 generate_baseline.py
    assert (
        "test_p31_multi_lang.py" in source
        or "generate_baseline.py" in source
        or "test_l9_rust_multilang.py" in source
    ), f"{lang}.json provenance.source={source!r} 未指向已知样本来源"


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_provenance_commit_sha_is_git_sha(lang):
    """provenance.commit_sha 必须是 40 字符的 git SHA1。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    sha = data["provenance"]["commit_sha"]
    assert len(sha) == 40, f"{lang}.json commit_sha={sha!r} 长度应为 40"
    assert all(c in "0123456789abcdef" for c in sha), (
        f"{lang}.json commit_sha={sha!r} 包含非 hex 字符"
    )


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_provenance_method_documents_hand_curation(lang):
    """provenance.method 必须明确说明是手工校对的契约真相。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    method = data["provenance"]["method"]
    assert "hand-curated" in method or "hand" in method.lower(), (
        f"{lang}.json method 必须说明是手工校对"
    )
    assert "contract truth" in method or "truth" in method.lower(), (
        f"{lang}.json method 必须说明是契约真相"
    )


# ============================================
# known_gaps 测试
# ============================================

@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_known_gaps_is_list(lang):
    """known_gaps 必须是 list（可以为空，但字段必须存在）。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    assert isinstance(data["known_gaps"], list), (
        f"{lang}.json known_gaps 必须是 list"
    )


@pytest.mark.parametrize("lang", sorted(_EXPECTED_LANGUAGES))
def test_fixture_known_gaps_have_required_fields(lang):
    """每个 known_gap 必须包含 parser/field/description/phase 4 字段。"""
    if not _fixture_exists(lang):
        pytest.skip(f"fixture {lang}.json 不存在")
    data = _load_fixture(lang)
    required = {"parser", "field", "description", "phase"}
    for i, gap in enumerate(data["known_gaps"]):
        missing = required - set(gap.keys())
        assert not missing, (
            f"{lang}.json known_gaps[{i}] 缺失字段: {missing}"
        )
        assert gap["parser"] in {"rust", "python"}, (
            f"{lang}.json known_gaps[{i}].parser={gap['parser']!r} 必须是 'rust' 或 'python'"
        )


# ============================================
# Phase 0 完成门：4 个关键语言缺口必须被记录
# ============================================

def test_typescript_fixture_documents_symbol_gap():
    """TypeScript fixture 的 known_gaps 必须记录符号提取缺口。"""
    if not _fixture_exists("typescript"):
        pytest.skip("typescript.json 不存在")
    data = _load_fixture("typescript")
    # 当前 Rust 提取 5 个符号（User/constructor/greet/add/main），但缺少构造调用解析
    # 主要缺口是 signature/kind/构造调用
    gap_fields = {gap["field"] for gap in data["known_gaps"]}
    assert "signature" in gap_fields, "typescript fixture 应记录 signature 缺口"


def test_php_fixture_documents_property_gap():
    """PHP fixture 的 property 契约（P0-C Step 2 已修复 Rust 缺口）。"""
    if not _fixture_exists("php"):
        pytest.skip("php.json 不存在")
    data = _load_fixture("php")
    # expected.symbols 必须包含 property 符号（golden 契约真相）
    kinds = {s["kind"] for s in data["expected"]["symbols"]}
    assert "property" in kinds, "php fixture expected.symbols 必须包含 property"
    # P0-C Step 2: Rust 现已提取 PHP property，known_gaps 不再记录此 Rust 缺口
    has_rust_property_gap = any(
        gap["parser"] == "rust" and "property" in gap["description"].lower()
        for gap in data["known_gaps"]
    )
    assert not has_rust_property_gap, (
        "php fixture known_gaps 不应再记录 Rust property 缺失（P0-C Step 2 已修复）"
    )


def test_scala_fixture_documents_object_method_call_gap():
    """Scala fixture 的对象方法调用契约（P0-C Step 3 已修复 Rust 缺口）。"""
    if not _fixture_exists("scala"):
        pytest.skip("scala.json 不存在")
    data = _load_fixture("scala")
    # expected.raw_calls 必须包含 calc.add 对象方法调用（golden 契约真相）
    has_add_call = any(
        call["callee_name"] == "add" and call["callee_module"] == "calc"
        for call in data["expected"]["raw_calls"]
    )
    assert has_add_call, "scala fixture expected.raw_calls 必须包含 calc.add 对象方法调用"
    # P0-C Step 3: Rust 现已提取 calc.add() 和 new Calculator，known_gaps 不再记录此 Rust 缺口
    has_rust_call_gap = any(
        gap["parser"] == "rust" and (
            "calc.add" in gap["description"] or "object method" in gap["description"].lower()
            or "new Calculator" in gap["description"]
        )
        for gap in data["known_gaps"]
    )
    assert not has_rust_call_gap, (
        "scala fixture known_gaps 不应再记录 Rust 对象方法/构造调用缺失（P0-C Step 3 已修复）"
    )


def test_hcl_fixture_documents_reference_gap():
    """HCL fixture 的引用契约（P0-D 已修复 Rust 缺口）。"""
    if not _fixture_exists("hcl"):
        pytest.skip("hcl.json 不存在")
    data = _load_fixture("hcl")
    # expected.references 必须包含 attribute traversal 引用
    assert len(data["expected"]["references"]) > 0, (
        "hcl fixture expected.references 必须非空"
    )
    # P0-D: Rust 现已支持 HCL（加入 supported_languages + ReferenceRule 提取引用），
    # known_gaps 不再记录 Rust 不支持 HCL / 不提取引用的缺口
    has_rust_hcl_unsupported_gap = any(
        gap["parser"] == "rust"
        and (
            "不支持 hcl" in gap["description"].lower()
            or "不在 supported_languages" in gap["description"].lower()
            or "不提取 hcl attribute traversal" in gap["description"].lower()
        )
        for gap in data["known_gaps"]
    )
    assert not has_rust_hcl_unsupported_gap, (
        "hcl fixture known_gaps 不应再记录 Rust 不支持 HCL / 不提取引用的缺口"
        "（P0-D 已修复：HCL 加入 supported_languages + ReferenceRule 实现）"
    )


# ============================================
# 整体一致性测试
# ============================================

def test_all_16_language_fixtures_present():
    """确保 16 个语言的 fixture 文件全部存在（无遗漏）。"""
    missing = [lang for lang in _EXPECTED_LANGUAGES if not _fixture_exists(lang)]
    assert not missing, f"缺失 golden fixture: {missing}"


def test_no_extra_fixture_files():
    """确保 golden/ 目录下没有多余的语言 fixture 文件。"""
    actual = {
        p.stem for p in _GOLDEN_DIR.glob("*.json")
    }
    extra = actual - _EXPECTED_LANGUAGES
    assert not extra, f"golden/ 目录存在未预期的 fixture: {extra}"


def test_sample_source_matches_baseline_definition():
    """golden fixture 的 sample_source 必须与 generate_baseline.py 中的样本一致。

    防止 fixture 与 baseline 脱节（同一份 canonical bytes）。
    """
    # 加载 generate_baseline.py 中的样本
    import sys
    sys.path.insert(0, str(_GOLDEN_DIR.parent))
    from generate_baseline import _ALL_LANGUAGE_SAMPLES  # type: ignore

    samples_by_lang = {lang: content for lang, _, content in _ALL_LANGUAGE_SAMPLES}
    for lang, expected_source in samples_by_lang.items():
        if not _fixture_exists(lang):
            pytest.fail(f"golden fixture {lang}.json 缺失（与 generate_baseline.py 不一致）")
        data = _load_fixture(lang)
        assert data["sample_source"] == expected_source, (
            f"{lang}.json sample_source 与 generate_baseline.py _ALL_LANGUAGE_SAMPLES 不一致"
        )
