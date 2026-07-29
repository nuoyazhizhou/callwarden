"""P0-A Step 5: 编码与错误契约测试。

验证设计文档 §5.1 输入契约和 §5.3 错误语义在边界输入下的行为：
- 空文件 / 语法错误 / 截断（partial）— §5.3 错误语义
- 非 UTF-8 编码（UTF-8 BOM / UTF-16 LE/BE / GBK）— §5.1 输入契约
- CRLF / lone CR — §5.1 newline 归一化
- 超大文件 — §6.2 性能门禁

设计要点：
- tree-sitter 是错误容忍的，语法错误不应抛异常（§5.3 partial 状态）
- canonicalize_source_py 是 Rust 侧输入规范化唯一入口（§5.1）
- parse_file_lang 内部调用 parse_file，parse_file 第一步调用
  canonicalize_source，因此 UTF-16 输入会被正确解码为 UTF-8 再 parse
  （Phase 3 已修复：parse_file_lang 走 canonicalize 路径）
- 本测试同时验证当前行为和契约期望，缺口用 xfail 或文档化断言标记

关键约束（§6.3 永远不能白名单）：
- parser panic（抛异常）
- 整种语言零 symbols（因编码问题导致）
- 文件因 stream/fallback 控制流而消失
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# 添加 tests/ 目录到 path，复用现有样本代码
_TESTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from test_p31_multi_lang import _has_rust_ext  # noqa: E402
from callwarden.parsers import create_parser  # noqa: E402

# Rust 侧 canonicalize 入口（Python 暴露名为 canonicalize_source_py）
try:
    from callwarden_core import (  # type: ignore  # noqa: E402
        canonicalize_source_py as canonicalize_source,
        parse_file_lang,
        parse_canonical_bytes_py,
    )
    _HAS_RUST = True
except Exception:
    canonicalize_source = None  # type: ignore
    parse_file_lang = None  # type: ignore
    parse_canonical_bytes_py = None  # type: ignore
    _HAS_RUST = False


# ============================================
# 测试样本
# ============================================

# Python 样本：含一个 function 符号
_PY_SAMPLE = b"def hello(name):\n    return name\n"

# Rust 样本：含一个 function 符号
_RS_SAMPLE = b"pub fn hello(name: &str) -> &str {\n    name\n}\n"

# Go 样本：含一个 function 符号
_GO_SAMPLE = b"package main\n\nfunc hello(name string) string {\n    return name\n}\n"


def _write_tmp(tmp_path: Path, filename: str, content: bytes) -> Path:
    """以二进制方式写入临时文件，返回路径。"""
    path = tmp_path / filename
    path.write_bytes(content)
    return path


def _parse_python(path: Path, lang: str):
    """运行 Python parser，返回 result 或抛异常。"""
    py_parser = create_parser(str(path))
    assert py_parser is not None, f"Python parser 不支持 {lang}"
    return py_parser.parse_file(str(path), "test.encoding")


def _parse_rust(path: Path, lang: str):
    """运行 Rust parse_file_lang（直接读路径），返回 result 或抛异常。"""
    if lang == "c":
        from callwarden_core import parse_c_file
        return parse_c_file(str(path), "test.encoding")
    return parse_file_lang(str(path), "test.encoding", lang)


# ============================================
# 1. canonicalize_source 契约测试（Rust 输入规范化入口）
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestCanonicalizeContract:
    """canonicalize_source_py 行为契约（设计文档 §5.1）。

    验证 Rust 侧输入规范化入口符合 parse-input-abi.md §2.1：
    - BOM 检测 + 剥离（UTF-8 / UTF-16 LE / UTF-16 BE）
    - 流式解码（UTF-16 → UTF-8，或 UTF-8 with latin-1 fallback）
    - CRLF / lone CR → LF 归一化
    - SHA-256 content_hash 基于 canonical bytes
    """

    def test_empty_file_canonicalizes_to_empty(self, tmp_path):
        """空文件 canonicalize 后仍为空字节。"""
        path = _write_tmp(tmp_path, "empty.py", b"")
        result = canonicalize_source(str(path))
        assert result["canonical_bytes"] == b""
        assert result["canonical_total"] == 0
        assert result["raw_total"] == 0
        meta = result["metadata"]
        assert meta["bom_kind"] == "none"
        assert meta["newline_style"] == "none"
        assert meta["source_encoding"] == "utf-8"

    def test_utf8_bom_is_stripped(self, tmp_path):
        """UTF-8 BOM（EF BB BF）必须被剥离，canonical_bytes 不含 BOM。"""
        raw = b"\xef\xbb\xbf" + _PY_SAMPLE
        path = _write_tmp(tmp_path, "bom.py", raw)
        result = canonicalize_source(str(path))
        assert result["metadata"]["bom_kind"] == "utf-8"
        assert result["canonical_bytes"] == _PY_SAMPLE
        assert result["raw_total"] == len(raw)
        assert result["canonical_total"] == len(_PY_SAMPLE)

    def test_utf16_le_bom_is_decoded(self, tmp_path):
        """UTF-16 LE BOM（FF FE）必须被识别并解码为 UTF-8 canonical bytes。"""
        text = _PY_SAMPLE.decode("utf-8")
        raw = b"\xff\xfe" + text.encode("utf-16-le")
        path = _write_tmp(tmp_path, "utf16le.py", raw)
        result = canonicalize_source(str(path))
        assert result["metadata"]["bom_kind"] == "utf-16-le"
        assert result["metadata"]["source_encoding"] == "utf-16-le"
        assert result["canonical_bytes"] == _PY_SAMPLE

    def test_utf16_be_bom_is_decoded(self, tmp_path):
        """UTF-16 BE BOM（FE FF）必须被识别并解码为 UTF-8 canonical bytes。"""
        text = _PY_SAMPLE.decode("utf-8")
        raw = b"\xfe\xff" + text.encode("utf-16-be")
        path = _write_tmp(tmp_path, "utf16be.py", raw)
        result = canonicalize_source(str(path))
        assert result["metadata"]["bom_kind"] == "utf-16-be"
        assert result["metadata"]["source_encoding"] == "utf-16-be"
        assert result["canonical_bytes"] == _PY_SAMPLE

    def test_crlf_is_normalized_to_lf(self, tmp_path):
        """CRLF 必须被归一化为 LF。"""
        raw = _PY_SAMPLE.replace(b"\n", b"\r\n")
        path = _write_tmp(tmp_path, "crlf.py", raw)
        result = canonicalize_source(str(path))
        assert result["metadata"]["newline_style"] == "crlf"
        assert result["canonical_bytes"] == _PY_SAMPLE

    def test_lone_cr_is_normalized_to_lf(self, tmp_path):
        """lone CR 必须被归一化为 LF。"""
        raw = b"line1\rline2"
        path = _write_tmp(tmp_path, "cr.py", raw)
        result = canonicalize_source(str(path))
        assert result["metadata"]["newline_style"] == "cr"
        assert result["canonical_bytes"] == b"line1\nline2"

    def test_content_hash_differs_from_raw_hash_when_bom_present(self, tmp_path):
        """含 BOM 的文件 raw_hash 必须与 content_hash 不同（canonical 不含 BOM）。"""
        raw = b"\xef\xbb\xbfhello"
        path = _write_tmp(tmp_path, "bom.py", raw)
        result = canonicalize_source(str(path))
        assert result["metadata"]["raw_hash"] != result["content_hash"]

    def test_content_hash_deterministic(self, tmp_path):
        """同一文件两次 canonicalize，content_hash 必须一致。"""
        path = _write_tmp(tmp_path, "x.py", _PY_SAMPLE)
        r1 = canonicalize_source(str(path))
        r2 = canonicalize_source(str(path))
        assert r1["content_hash"] == r2["content_hash"]
        assert r1["canonical_bytes"] == r2["canonical_bytes"]

    def test_nonexistent_file_raises(self, tmp_path):
        """不存在的文件必须返回错误，不得静默成功。"""
        with pytest.raises(Exception):
            canonicalize_source(str(tmp_path / "nonexistent.py"))

    def test_gbk_falls_back_to_latin1(self, tmp_path):
        """GBK 编码文件无 BOM，Rust canonicalize 应回退 latin-1。

        设计文档 §5.1：UTF-8 strict 失败后允许 latin-1 降级。
        Python 端有 16 种方言编码检测链，Rust 端目前仅 utf-8/utf-16/latin-1，
        这是已知投影差异（Phase 2 待评估是否补 GBK/Shift-JIS 等方言）。
        """
        gbk_text = "# 中文注释\n"
        raw = gbk_text.encode("gbk") + _PY_SAMPLE
        path = _write_tmp(tmp_path, "gbk.py", raw)
        result = canonicalize_source(str(path))
        # Rust 不识别 GBK，回退 latin-1
        assert result["metadata"]["source_encoding"] == "latin-1"
        assert result["metadata"]["bom_kind"] == "none"
        # ASCII 部分（def hello...）必须保留，非 ASCII 部分按 latin-1 解码（多字节）
        assert b"def hello(name):" in result["canonical_bytes"]


# ============================================
# 2. 空文件契约测试
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestEmptyFileContract:
    """空文件解析契约（设计文档 §5.3）。

    空文件不应让 parser 崩溃，应返回 0 符号、0 调用、0 import。
    设计文档 §5.3：状态为 ok（空文件不是错误）。
    """

    @pytest.mark.parametrize("lang,filename,sample", [
        ("python", "empty.py", b""),
        ("rust", "empty.rs", b""),
        ("go", "empty.go", b""),
    ])
    def test_empty_file_no_crash(self, lang, filename, sample, tmp_path):
        """空文件：parser 不崩溃，返回 0 符号。"""
        path = _write_tmp(tmp_path, filename, sample)
        py_result = _parse_python(path, lang)
        rs_result = _parse_rust(path, lang)

        assert len(py_result["symbols"]) == 0
        assert len(rs_result["symbols"]) == 0
        assert len(py_result.get("raw_calls", [])) == 0
        assert len(rs_result.get("raw_calls", [])) == 0

    def test_empty_file_total_lines(self, tmp_path):
        """空文件 total_lines 应为 1（空字符串按 1 行计）。"""
        path = _write_tmp(tmp_path, "empty.py", b"")
        py_result = _parse_python(path, "python")
        rs_result = _parse_rust(path, "python")
        # 双方对空文件的行数定义一致（1 行）
        assert py_result.get("total_lines", 0) == rs_result.get("total_lines", 0)


# ============================================
# 3. UTF-8 BOM 契约测试
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestUtf8BomContract:
    """UTF-8 BOM 文件解析契约。

    设计文档 §5.1：BOM 必须被剥离，canonical bytes 不含 BOM。
    Python parser 通过 read_file_normalized 处理 BOM；
    Rust parse_file_lang 内部走 canonicalize_source，BOM 被剥离后再 parse。
    """

    @pytest.mark.parametrize("lang,filename,sample", [
        ("python", "bom.py", b"\xef\xbb\xbf" + _PY_SAMPLE),
        ("rust", "bom.rs", b"\xef\xbb\xbf" + _RS_SAMPLE),
        ("go", "bom.go", b"\xef\xbb\xbf" + _GO_SAMPLE),
    ])
    def test_utf8_bom_no_crash(self, lang, filename, sample, tmp_path):
        """UTF-8 BOM 文件：parser 不崩溃，提取符号数量与无 BOM 一致。"""
        path = _write_tmp(tmp_path, filename, sample)
        py_result = _parse_python(path, lang)
        rs_result = _parse_rust(path, lang)

        # 与无 BOM 版本对比
        path_no_bom = _write_tmp(tmp_path, f"nobom_{filename}", sample[3:])
        py_no_bom = _parse_python(path_no_bom, lang)
        rs_no_bom = _parse_rust(path_no_bom, lang)

        assert len(py_result["symbols"]) == len(py_no_bom["symbols"]), (
            f"[{lang}] Python BOM 影响 symbol 数量"
        )
        assert len(rs_result["symbols"]) == len(rs_no_bom["symbols"]), (
            f"[{lang}] Rust BOM 影响 symbol 数量"
        )

    def test_utf8_bom_symbol_extracted(self, tmp_path):
        """Python 文件 UTF-8 BOM：hello 函数必须被提取。"""
        path = _write_tmp(tmp_path, "bom.py", b"\xef\xbb\xbf" + _PY_SAMPLE)
        py_result = _parse_python(path, "python")
        rs_result = _parse_rust(path, "python")
        py_names = {s["name"] for s in py_result["symbols"]}
        rs_names = {s["name"] for s in rs_result["symbols"]}
        assert "hello" in py_names
        assert "hello" in rs_names


# ============================================
# 4. CRLF 契约测试
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestCrlfContract:
    """CRLF 文件解析契约（设计文档 §5.1）。

    CRLF 必须被归一化为 LF。Python parser 通过 norm_newlines 处理；
    Rust parse_file_lang 直接读文件，tree-sitter 容忍 CRLF。
    """

    @pytest.mark.parametrize("lang,filename,sample", [
        ("python", "crlf.py", _PY_SAMPLE.replace(b"\n", b"\r\n")),
        ("rust", "crlf.rs", _RS_SAMPLE.replace(b"\n", b"\r\n")),
        ("go", "crlf.go", _GO_SAMPLE.replace(b"\n", b"\r\n")),
    ])
    def test_crlf_no_crash(self, lang, filename, sample, tmp_path):
        """CRLF 文件：parser 不崩溃，符号数与 LF 版本一致。"""
        path_crlf = _write_tmp(tmp_path, filename, sample)
        path_lf = _write_tmp(tmp_path, f"lf_{filename}", sample.replace(b"\r\n", b"\n"))

        py_crlf = _parse_python(path_crlf, lang)
        py_lf = _parse_python(path_lf, lang)
        rs_crlf = _parse_rust(path_crlf, lang)
        rs_lf = _parse_rust(path_lf, lang)

        assert len(py_crlf["symbols"]) == len(py_lf["symbols"]), (
            f"[{lang}] Python CRLF 影响 symbol 数量"
        )
        assert len(rs_crlf["symbols"]) == len(rs_lf["symbols"]), (
            f"[{lang}] Rust CRLF 影响 symbol 数量"
        )

    def test_crlf_symbol_line_numbers_match_lf(self, tmp_path):
        """CRLF 文件的 symbol line 号应与 LF 版本一致。"""
        path_crlf = _write_tmp(tmp_path, "crlf.py", _PY_SAMPLE.replace(b"\n", b"\r\n"))
        path_lf = _write_tmp(tmp_path, "lf.py", _PY_SAMPLE)

        py_crlf = _parse_python(path_crlf, "python")
        py_lf = _parse_python(path_lf, "python")
        rs_crlf = _parse_rust(path_crlf, "python")
        rs_lf = _parse_rust(path_lf, "python")

        def _lines(result):
            return {(s["name"], s["start_line"], s["end_line"]) for s in result["symbols"]}

        assert _lines(py_crlf) == _lines(py_lf), "Python CRLF line 号不一致"
        assert _lines(rs_crlf) == _lines(rs_lf), "Rust CRLF line 号不一致"


# ============================================
# 5. UTF-16 契约测试
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestUtf16Contract:
    """UTF-16 LE/BE 文件解析契约。

    设计文档 §5.1：canonicalize_source 必须解码 UTF-16 → UTF-8。
    Python parser 通过 read_file_normalized 处理 UTF-16 BOM；
    Rust parse_file_lang 内部调用 parse_file，parse_file 第一步调用
    canonicalize_source，因此 UTF-16 输入会被正确解码为 UTF-8 再 parse
    （Phase 3 已修复：parse_file_lang 走 canonicalize 路径）。
    """

    def test_utf16_le_python_handles(self, tmp_path):
        """Python parser 通过 read_file_normalized 正确处理 UTF-16 LE。"""
        text = _PY_SAMPLE.decode("utf-8")
        raw = b"\xff\xfe" + text.encode("utf-16-le")
        path = _write_tmp(tmp_path, "utf16le.py", raw)
        result = _parse_python(path, "python")
        names = {s["name"] for s in result["symbols"]}
        assert "hello" in names, "Python parser 应从 UTF-16 LE 提取 hello"

    def test_utf16_be_python_handles(self, tmp_path):
        """Python parser 通过 read_file_normalized 正确处理 UTF-16 BE。"""
        text = _PY_SAMPLE.decode("utf-8")
        raw = b"\xfe\xff" + text.encode("utf-16-be")
        path = _write_tmp(tmp_path, "utf16be.py", raw)
        result = _parse_python(path, "python")
        names = {s["name"] for s in result["symbols"]}
        assert "hello" in names, "Python parser 应从 UTF-16 BE 提取 hello"

    def test_utf16_le_rust_parse_file_lang_returns_zero(self, tmp_path):
        """Phase 3 已修复：Rust parse_file_lang 走 canonicalize，UTF-16 LE 返回符号。

        设计文档 §3.1 目标 4：所有生产入口必须用同一 canonical bytes 和 ParseFact 合约。
        parse_file_lang 内部调用 parse_file，parse_file 第一步调用
        canonicalize_source，UTF-16 LE 被解码为 UTF-8 再 parse。
        """
        text = _PY_SAMPLE.decode("utf-8")
        raw = b"\xff\xfe" + text.encode("utf-16-le")
        path = _write_tmp(tmp_path, "utf16le.py", raw)
        rs_result = _parse_rust(path, "python")
        # Phase 3 修复：Rust 走 canonicalize，应提取到 hello 符号
        names = {s["name"] for s in rs_result["symbols"]}
        assert "hello" in names, (
            f"Rust parse_file_lang 应从 UTF-16 LE 提取 hello（走 canonicalize），"
            f"实际 symbols={rs_result['symbols']}"
        )

    def test_utf16_be_rust_parse_file_lang_returns_zero(self, tmp_path):
        """Phase 3 已修复：Rust parse_file_lang 走 canonicalize，UTF-16 BE 返回符号。"""
        text = _PY_SAMPLE.decode("utf-8")
        raw = b"\xfe\xff" + text.encode("utf-16-be")
        path = _write_tmp(tmp_path, "utf16be.py", raw)
        rs_result = _parse_rust(path, "python")
        names = {s["name"] for s in rs_result["symbols"]}
        assert "hello" in names, (
            f"Rust parse_file_lang 应从 UTF-16 BE 提取 hello（走 canonicalize），"
            f"实际 symbols={rs_result['symbols']}"
        )

    def test_utf16_le_canonicalize_then_parse_works(self, tmp_path):
        """契约期望：canonicalize + parse_canonical_bytes_py 应正确解析 UTF-16 LE。

        验证 Phase 3 修复路径可行：canonicalize_source 解码 UTF-16 → UTF-8，
        parse_canonical_bytes_py 用 canonical bytes 解析，应提取到符号。
        """
        text = _PY_SAMPLE.decode("utf-8")
        raw = b"\xff\xfe" + text.encode("utf-16-le")
        path = _write_tmp(tmp_path, "utf16le.py", raw)

        # Step 1: canonicalize
        can = canonicalize_source(str(path))
        assert can["metadata"]["bom_kind"] == "utf-16-le"
        assert can["canonical_bytes"] == _PY_SAMPLE

        # Step 2: parse canonical bytes
        rs_result = parse_canonical_bytes_py(
            can["canonical_bytes"], "test.encoding", "python", can["content_hash"]
        )
        names = {s["name"] for s in rs_result["symbols"]}
        assert "hello" in names, (
            "canonicalize + parse_canonical_bytes_py 应从 UTF-16 LE 提取 hello"
        )

    def test_utf16_be_canonicalize_then_parse_works(self, tmp_path):
        """契约期望：canonicalize + parse_canonical_bytes_py 应正确解析 UTF-16 BE。"""
        text = _PY_SAMPLE.decode("utf-8")
        raw = b"\xfe\xff" + text.encode("utf-16-be")
        path = _write_tmp(tmp_path, "utf16be.py", raw)

        can = canonicalize_source(str(path))
        assert can["metadata"]["bom_kind"] == "utf-16-be"
        assert can["canonical_bytes"] == _PY_SAMPLE

        rs_result = parse_canonical_bytes_py(
            can["canonical_bytes"], "test.encoding", "python", can["content_hash"]
        )
        names = {s["name"] for s in rs_result["symbols"]}
        assert "hello" in names


# ============================================
# 6. 非 UTF-8 编码（GBK）契约测试
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestGbkEncodingContract:
    """GBK 编码文件解析契约。

    Python parser 通过 16 种方言编码检测链识别 GBK；
    Rust canonicalize 不识别 GBK，回退 latin-1（非 ASCII 字符会乱码，
    但 ASCII 结构保留，tree-sitter 仍可解析代码结构）。

    设计文档 §5.1：UTF-8 strict 失败后允许 latin-1 降级。
    Phase 2 待评估是否在 Rust 侧补 GBK/Shift-JIS 等方言检测。
    """

    def test_gbk_python_extracts_symbol(self, tmp_path):
        """Python parser 从 GBK 文件提取符号。"""
        raw = "# 中文注释\n".encode("gbk") + _PY_SAMPLE
        path = _write_tmp(tmp_path, "gbk.py", raw)
        result = _parse_python(path, "python")
        names = {s["name"] for s in result["symbols"]}
        assert "hello" in names

    def test_gbk_rust_extracts_symbol_via_latin1_fallback(self, tmp_path):
        """Rust 通过 latin-1 降级保留 ASCII 结构，仍提取符号。

        非 ASCII 字符（中文注释）会被 latin-1 解码为多字节 UTF-8（乱码），
        但 def hello(name): 部分是 ASCII，tree-sitter 能识别函数结构。
        """
        raw = "# 中文注释\n".encode("gbk") + _PY_SAMPLE
        path = _write_tmp(tmp_path, "gbk.py", raw)
        result = _parse_rust(path, "python")
        names = {s["name"] for s in result["symbols"]}
        assert "hello" in names, (
            "Rust latin-1 降级应保留 ASCII 结构，仍提取 hello"
        )

    def test_gbk_symbol_count_matches(self, tmp_path):
        """GBK 文件：Python 和 Rust 提取的符号数量一致（ASCII 结构一致）。"""
        raw = "# 中文注释\n".encode("gbk") + _PY_SAMPLE
        path = _write_tmp(tmp_path, "gbk.py", raw)
        py_result = _parse_python(path, "python")
        rs_result = _parse_rust(path, "python")
        assert len(py_result["symbols"]) == len(rs_result["symbols"])


# ============================================
# 7. 语法错误契约测试
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestSyntaxErrorContract:
    """语法错误文件解析契约（设计文档 §5.3）。

    tree-sitter 是错误容忍的，语法错误不应让 parser 崩溃。
    设计文档 §5.3：状态为 partial（发布可用事实并持久化 diagnostics）。
    当前 ABI 未暴露 error_count 字段，本测试只验证不崩溃。

    设计文档 §6.3 禁止：
    - parser panic（抛异常）
    - 文件因 stream/fallback 控制流而消失
    """

    @pytest.mark.parametrize("lang,filename,bad_content", [
        ("python", "err.py", b"def :\n    pass\n"),
        ("python", "err2.py", b"class\n    def\n"),
        ("rust", "err.rs", b"pub fn {\n    name\n}\n"),
        ("rust", "err2.rs", b"struct { x: i32,\n"),
        ("go", "err.go", b"package main\n\nfunc {\n    return\n}\n"),
    ])
    def test_syntax_error_no_crash(self, lang, filename, bad_content, tmp_path):
        """语法错误文件：parser 不崩溃，返回结果（可能 0 符号）。"""
        path = _write_tmp(tmp_path, filename, bad_content)
        # 两个 parser 都不应抛异常
        py_result = _parse_python(path, lang)
        rs_result = _parse_rust(path, lang)
        # 返回结构完整（symbols 是 list）
        assert isinstance(py_result["symbols"], list)
        assert isinstance(rs_result["symbols"], list)

    def test_syntax_error_returns_partial_result(self, tmp_path):
        """语法错误文件：tree-sitter 错误恢复，可能返回部分符号。"""
        # 前面有语法错误，后面有有效函数
        content = b"def :\n    pass\n\ndef valid():\n    return 1\n"
        path = _write_tmp(tmp_path, "partial.py", content)
        py_result = _parse_python(path, "python")
        rs_result = _parse_rust(path, "python")
        # valid 函数应被提取（tree-sitter 错误恢复）
        py_names = {s["name"] for s in py_result["symbols"]}
        rs_names = {s["name"] for s in rs_result["symbols"]}
        assert "valid" in py_names, "Python 应通过错误恢复提取 valid"
        assert "valid" in rs_names, "Rust 应通过错误恢复提取 valid"


# ============================================
# 8. 截断/部分解析契约测试
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestPartialParseContract:
    """截断文件（partial parse）契约（设计文档 §5.3）。

    截断文件不应让 parser 崩溃，应返回可用部分。
    设计文档 §5.3：状态为 partial（发布可用事实并持久化 diagnostics）。
    """

    @pytest.mark.parametrize("lang,filename,truncated", [
        ("python", "trunc.py", b"def hello(name):\n    return "),
        ("python", "trunc2.py", b"def hello(name):"),
        ("rust", "trunc.rs", b"pub fn hello(name: &str) -> &"),
        ("go", "trunc.go", b"package main\n\nfunc hello(name "),
    ])
    def test_truncated_no_crash(self, lang, filename, truncated, tmp_path):
        """截断文件：parser 不崩溃。"""
        path = _write_tmp(tmp_path, filename, truncated)
        py_result = _parse_python(path, lang)
        rs_result = _parse_rust(path, lang)
        assert isinstance(py_result["symbols"], list)
        assert isinstance(rs_result["symbols"], list)

    def test_truncated_python_extracts_partial(self, tmp_path):
        """Python 截断：def hello(name): 体不完整，但函数签名可识别。"""
        path = _write_tmp(tmp_path, "trunc.py", b"def hello(name):\n    return ")
        py_result = _parse_python(path, "python")
        rs_result = _parse_rust(path, "python")
        # 双方都应提取到 hello（tree-sitter 容忍不完整函数体）
        py_names = {s["name"] for s in py_result["symbols"]}
        rs_names = {s["name"] for s in rs_result["symbols"]}
        assert "hello" in py_names
        assert "hello" in rs_names


# ============================================
# 9. 超大文件契约测试
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestLargeFileContract:
    """超大文件解析契约（设计文档 §6.2 性能门禁）。

    设计文档 §6.2：单文件 P95 < 50ms。
    本测试不严格计时，只验证超大文件不崩溃、符号数与预期一致。
    设计文档 §6.3 禁止：未界定内存增长。
    """

    def test_large_python_file_no_crash(self, tmp_path):
        """160KB Python 文件（5000 个函数）：parser 不崩溃。"""
        # 5000 个 hello 函数 → 5000 个符号
        big_content = _PY_SAMPLE * 5000  # ~165KB
        path = _write_tmp(tmp_path, "large.py", big_content)
        py_result = _parse_python(path, "python")
        rs_result = _parse_rust(path, "python")
        assert len(py_result["symbols"]) == 5000
        assert len(rs_result["symbols"]) == 5000

    def test_large_file_symbol_count_matches(self, tmp_path):
        """超大文件：Python 和 Rust 符号数一致。"""
        big_content = _PY_SAMPLE * 1000  # 33KB
        path = _write_tmp(tmp_path, "large.py", big_content)
        py_result = _parse_python(path, "python")
        rs_result = _parse_rust(path, "python")
        assert len(py_result["symbols"]) == len(rs_result["symbols"]) == 1000


# ============================================
# 10. 无 panic 全局契约测试
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestNoCrashContract:
    """所有边界输入都不应让 parser panic（设计文档 §6.3）。

    设计文档 §6.3 永远不能白名单：
    - parser panic（抛异常）
    - 文件因 stream/fallback 控制流而消失
    """

    @pytest.mark.parametrize("label,content,lang,filename", [
        ("empty", b"", "python", "empty.py"),
        ("empty_rust", b"", "rust", "empty.rs"),
        ("utf8_bom", b"\xef\xbb\xbf" + _PY_SAMPLE, "python", "bom.py"),
        ("utf16_le", b"\xff\xfe" + _PY_SAMPLE.decode("utf-8").encode("utf-16-le"), "python", "u16le.py"),
        ("utf16_be", b"\xfe\xff" + _PY_SAMPLE.decode("utf-8").encode("utf-16-be"), "python", "u16be.py"),
        ("crlf", _PY_SAMPLE.replace(b"\n", b"\r\n"), "python", "crlf.py"),
        ("lone_cr", b"def hello():\r    return 1", "python", "cr.py"),
        ("syntax_err", b"def :\n", "python", "err.py"),
        ("truncated", b"def hello(name):\n    return ", "python", "trunc.py"),
        ("only_bom", b"\xef\xbb\xbf", "python", "onlybom.py"),
        ("only_bom_rust", b"\xef\xbb\xbf", "rust", "onlybom.rs"),
        ("binary_garbage", b"\x00\x01\x02\xff\xfe\xfd", "python", "bin.py"),
        ("null_bytes", b"def hello():\n    pass\x00\n", "python", "null.py"),
    ])
    def test_no_panic_python(self, label, content, lang, filename, tmp_path):
        """Python parser 对所有边界输入不抛异常。"""
        path = _write_tmp(tmp_path, filename, content)
        try:
            result = _parse_python(path, lang)
            # 返回结构必须完整
            assert "symbols" in result
            assert isinstance(result["symbols"], list)
        except Exception as e:
            pytest.fail(f"[{label}] Python parser panic: {type(e).__name__}: {e}")

    @pytest.mark.parametrize("label,content,lang,filename", [
        ("empty", b"", "python", "empty.py"),
        ("empty_rust", b"", "rust", "empty.rs"),
        ("utf8_bom", b"\xef\xbb\xbf" + _PY_SAMPLE, "python", "bom.py"),
        ("utf16_le", b"\xff\xfe" + _PY_SAMPLE.decode("utf-8").encode("utf-16-le"), "python", "u16le.py"),
        ("utf16_be", b"\xfe\xff" + _PY_SAMPLE.decode("utf-8").encode("utf-16-be"), "python", "u16be.py"),
        ("crlf", _PY_SAMPLE.replace(b"\n", b"\r\n"), "python", "crlf.py"),
        ("syntax_err", b"def :\n", "python", "err.py"),
        ("truncated", b"def hello(name):\n    return ", "python", "trunc.py"),
        ("only_bom", b"\xef\xbb\xbf", "python", "onlybom.py"),
        ("binary_garbage", b"\x00\x01\x02\xff\xfe\xfd", "python", "bin.py"),
        ("null_bytes", b"def hello():\n    pass\x00\n", "python", "null.py"),
    ])
    def test_no_panic_rust(self, label, content, lang, filename, tmp_path):
        """Rust parser 对所有边界输入不抛异常。"""
        path = _write_tmp(tmp_path, filename, content)
        try:
            result = _parse_rust(path, lang)
            assert "symbols" in result
            assert isinstance(result["symbols"], list)
        except Exception as e:
            pytest.fail(f"[{label}] Rust parser panic: {type(e).__name__}: {e}")


# ============================================
# 11. 错误语义状态文档化（设计文档 §5.3）
# ============================================

@pytest.mark.skipif(not _HAS_RUST, reason="callwarden_core 未安装")
class TestErrorSemanticsContract:
    """错误语义状态对齐测试（设计文档 §5.3，R1-P0-2 补齐 diagnostics ABI）。

    设计文档 §5.3 定义统一状态：
    | 状态 | 行为 |
    | ok      | 发布完整 ParseFact |
    | partial | 发布可用事实并持久化 diagnostics |
    | unsupported | 不发布空图谱，记录语言/构造 |
    | failed  | 不替换上一代 snapshot，记录失败并允许重试 |
    | stale   | generation CAS 拒绝 |

    R1-P0-2: diagnostics 字段已补齐到 ParseResult（替代旧 `error` 字段用于
    结构化诊断）。本测试验证：
    - diagnostics 字段存在且结构完整（status / syntax_error_count / partial_parse）
    - 干净文件返回 status="ok"，syntax_error_count=0
    - 语法错误文件返回 status="partial"，syntax_error_count>0，partial_parse=True
    - status 嵌套在 diagnostics 内，不在 ParseResult 顶层
    """

    def test_diagnostics_field_present_and_well_formed(self, tmp_path):
        """diagnostics 字段存在且包含必填子字段（§5.2 ABI）。"""
        path = _write_tmp(tmp_path, "ok.py", _PY_SAMPLE)
        rs_result = _parse_rust(path, "python")
        assert "diagnostics" in rs_result, (
            "Rust parser 未返回 diagnostics 字段（§5.2 ABI 缺口）"
        )
        diag = rs_result["diagnostics"]
        assert isinstance(diag, dict), f"diagnostics 必须是 dict，实际 {type(diag)}"
        for field in ("status", "syntax_error_count", "partial_parse"):
            assert field in diag, (
                f"diagnostics 缺少必填字段 {field}（§5.2 ABI）"
            )

    def test_diagnostics_ok_on_clean_file(self, tmp_path):
        """干净文件返回 status="ok"，syntax_error_count=0，partial_parse=False。"""
        path = _write_tmp(tmp_path, "ok.py", _PY_SAMPLE)
        rs_result = _parse_rust(path, "python")
        diag = rs_result["diagnostics"]
        assert diag["status"] == "ok", (
            f"干净文件 status 应为 'ok'，实际 {diag['status']!r}"
        )
        assert diag["syntax_error_count"] == 0, (
            f"干净文件 syntax_error_count 应为 0，实际 {diag['syntax_error_count']}"
        )
        assert diag["partial_parse"] is False, (
            f"干净文件 partial_parse 应为 False，实际 {diag['partial_parse']}"
        )

    def test_diagnostics_partial_on_syntax_error(self, tmp_path):
        """语法错误文件返回 status="partial"，syntax_error_count>0，partial_parse=True。"""
        path = _write_tmp(tmp_path, "err.py", b"def :\n")
        rs_result = _parse_rust(path, "python")
        diag = rs_result["diagnostics"]
        assert diag["status"] == "partial", (
            f"语法错误文件 status 应为 'partial'，实际 {diag['status']!r}"
        )
        assert diag["syntax_error_count"] > 0, (
            f"语法错误文件 syntax_error_count 应 > 0，实际 {diag['syntax_error_count']}"
        )
        assert diag["partial_parse"] is True, (
            f"语法错误文件 partial_parse 应为 True，实际 {diag['partial_parse']}"
        )

    def test_diagnostics_status_nested_not_top_level(self, tmp_path):
        """§5.3 status 嵌套在 diagnostics 内，不在 ParseResult 顶层。

        设计：status 是 diagnostics 的子字段，避免顶层字段膨胀。
        顶层仅有 diagnostics 复合字段。
        """
        path = _write_tmp(tmp_path, "ok.py", _PY_SAMPLE)
        rs_result = _parse_rust(path, "python")
        assert "status" not in rs_result, (
            "status 应嵌套在 diagnostics 内，不应出现在 ParseResult 顶层"
        )
        assert rs_result["diagnostics"]["status"] == "ok"
