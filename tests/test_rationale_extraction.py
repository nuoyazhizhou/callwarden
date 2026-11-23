"""rationale 语义注释识别与提取单测（任务 T-1795451980839-5932a434 step0）。

覆盖：
- 7 种前缀（NOTE/IMPORTANT/HACK/WHY/RATIONALE/TODO/FIXME）均能识别
- Python `#` 注释与 JS/TS/Rust `//` 行注释
- 非前缀注释不误判（小写、行中、缺冒号、字符串内、文档字符串内）
- 命中行按行号归属到对应符号
"""

from callwarden.db.db_build import (
    RATIONALE_PREFIXES,
    _attach_rationale_to_result,
    extract_rationale_comments,
)

PREFIXES = ["NOTE", "IMPORTANT", "HACK", "WHY", "RATIONALE", "TODO", "FIXME"]


def test_prefixes_constant_covers_seven():
    assert list(RATIONALE_PREFIXES) == PREFIXES


def test_python_all_prefixes():
    """Python `# PREFIX:` 7 种前缀全部识别，行号与正文正确。"""
    source = "\n".join(f"# {p}: py {p.lower()} reason" for p in PREFIXES)
    entries = extract_rationale_comments(source, "sample.py")
    assert [e["prefix"] for e in entries] == PREFIXES
    assert [e["line"] for e in entries] == list(range(1, 8))
    assert entries[0]["text"] == "py note reason"


def test_slash_all_prefixes():
    """JS/TS/Rust `// PREFIX:` 7 种前缀全部识别（允许行首缩进）。"""
    source = "\n".join(f"    // {p}: slash {p.lower()} reason" for p in PREFIXES)
    entries = extract_rationale_comments(source, "sample.ts")
    assert [e["prefix"] for e in entries] == PREFIXES
    assert entries[-1]["text"] == "slash fixme reason"


def test_slash_comment_for_rust_and_js_extensions():
    source = "// WHY: 避免重复解析\nfn main() {}\n"
    for path in ("a.rs", "a.js", "a.jsx", "a.tsx", "a.mjs", "a.cjs"):
        entries = extract_rationale_comments(source, path)
        assert len(entries) == 1, path
        assert entries[0]["prefix"] == "WHY"


def test_non_prefix_comments_not_matched():
    """非前缀形式不得误判。"""
    source = "\n".join([
        "# note: 小写前缀不算",
        "# this NOTE: 前缀不在行首",
        "# NOTEWORTHY: 形近词不算",
        "# FIXME 缺少冒号",
        "'# TODO: 字符串内不算'",
        "// simple comment",
        "const s = '// WHY: 字符串内不算'",
    ])
    assert extract_rationale_comments(source, "sample.py") == []
    assert extract_rationale_comments(source, "sample.ts") == []
    # 块注释形式（/* ... */）不在识别范围
    block = "/* # NOTE: 块注释不算 */\n"
    assert extract_rationale_comments(block, "sample.ts") == []


def test_trailing_comments_are_matched():
    """行尾注释同样是 rationale：Python 与 JS/TS/Rust 都要识别。"""
    py_source = "x = 1  # HACK: 规避上游 bug\n"
    assert [e["prefix"] for e in extract_rationale_comments(py_source, "a.py")] == ["HACK"]

    ts_source = "const x = 1; // WHY: 保持旧行为\n"
    assert [e["prefix"] for e in extract_rationale_comments(ts_source, "a.ts")] == ["WHY"]


def test_python_docstring_lines_not_matched():
    """文档字符串中的 `# NOTE:` 行不是注释 token，不应命中。"""
    source = (
        "def f():\n"
        "    \"\"\"说明\n"
        "    # NOTE: 文档字符串内不算\n"
        "    \"\"\"\n"
        "    return 1\n"
    )
    assert extract_rationale_comments(source, "sample.py") == []


def test_attach_rationale_to_symbols(tmp_path):
    """命中行按行号归属符号：行内注释归当前符号，紧邻上方注释归下一个符号。"""
    path = tmp_path / "sample.py"
    path.write_text(
        "def outer():\n"
        "    # WHY: 保留旧路径兼容\n"
        "    x = 1\n"
        "    return x\n"
        "\n"
        "# TODO: 补缓存\n"
        "def next_fn():\n"
        "    pass\n",
        encoding="utf-8",
    )
    result = {
        "rel_path": "sample.py",
        "abs_path": str(path),
        "symbols": [
            {"name": "outer", "start_line": 1, "end_line": 4},
            {"name": "next_fn", "start_line": 7, "end_line": 8},
        ],
        "inline_modules": [],
    }
    _attach_rationale_to_result(result)
    assert [e["prefix"] for e in result["rationale"]] == ["WHY", "TODO"]
    assert result["symbols"][0]["rationale"][0]["prefix"] == "WHY"
    assert result["symbols"][1]["rationale"][0]["prefix"] == "TODO"


def test_no_comment_file_untouched(tmp_path):
    """无命中文件不新增任何字段（零影响）。"""
    path = tmp_path / "plain.py"
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    result = {
        "rel_path": "plain.py",
        "abs_path": str(path),
        "symbols": [{"name": "f", "start_line": 1, "end_line": 2}],
        "inline_modules": [],
    }
    _attach_rationale_to_result(result)
    assert "rationale" not in result
    assert "rationale" not in result["symbols"][0]
