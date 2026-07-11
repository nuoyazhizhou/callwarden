"""Phase 5 T-1783751519227-18d8: canonicalize_source 单元测试

测试覆盖：
1. canonicalize_source_py 函数存在且可调用（Rust 扩展已编译时）
2. UTF-8 无 BOM：canonical_bytes == input，newline_style == "lf"
3. CRLF → LF：b"a\\r\\nb" → canonical b"a\\nb"，newline_style == "crlf"
4. UTF-8 BOM 剥离：b"\\xef\\xbb\\xbfa" → canonical b"a"，bom_kind == "utf-8"
5. content_hash 是 canonical_bytes 的 SHA-256
6. delta.rs compute_parse_delta 调用 canonicalize_source（源码检查）

注意：Rust 扩展预编译为 .pyd，新增的 canonicalize_source_py 函数需要
重新编译才能在 Python 侧调用。当 .pyd 未包含新函数时，功能测试跳过，
源码检查仍然运行。
"""
import hashlib
import os
import sys
import tempfile

import pytest

# ============================================
# Rust 扩展加载（与 test_phase5_delta.py 相同的路径配置）
# ============================================

_pyinstall = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rust_ext", "target", "pyinstall"
)
if os.path.isdir(_pyinstall):
    sys.path.insert(0, _pyinstall)

try:
    from callwarden_core import canonicalize_source_py
    HAS_CANONICALIZE = True
except ImportError:
    HAS_CANONICALIZE = False

# ============================================
# 源码路径常量
# ============================================

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUST_SRC = os.path.join(_PROJECT_ROOT, "rust_ext", "src")
_CANONICALIZE_RS = os.path.join(_RUST_SRC, "canonicalize.rs")
_DELTA_RS = os.path.join(_RUST_SRC, "delta.rs")
_MULTI_LANG_RS = os.path.join(_RUST_SRC, "multi_lang.rs")
_LIB_RS = os.path.join(_RUST_SRC, "lib.rs")


# ============================================
# 源码检查测试（不依赖 .pyd 编译）
# ============================================


class TestSourceCodeInspection:
    """源码检查：验证 canonicalize.rs / delta.rs / multi_lang.rs / lib.rs 包含预期代码"""

    def test_canonicalize_rs_exists(self):
        """canonicalize.rs 文件存在"""
        assert os.path.isfile(_CANONICALIZE_RS), f"文件不存在: {_CANONICALIZE_RS}"

    def test_canonicalize_rs_contains_canonicalize_source(self):
        """canonicalize.rs 包含 canonicalize_source 函数定义"""
        with open(_CANONICALIZE_RS, "r", encoding="utf-8") as f:
            content = f.read()
        assert "pub fn canonicalize_source" in content, \
            "canonicalize.rs 缺少 canonicalize_source 函数"

    def test_canonicalize_rs_contains_structs(self):
        """canonicalize.rs 包含 CanonicalizeResult 和 SourceMetadata 结构"""
        with open(_CANONICALIZE_RS, "r", encoding="utf-8") as f:
            content = f.read()
        assert "pub struct CanonicalizeResult" in content, \
            "canonicalize.rs 缺少 CanonicalizeResult 结构"
        assert "pub struct SourceMetadata" in content, \
            "canonicalize.rs 缺少 SourceMetadata 结构"

    def test_canonicalize_rs_contains_bom_detection(self):
        """canonicalize.rs 包含 BOM 检测逻辑"""
        with open(_CANONICALIZE_RS, "r", encoding="utf-8") as f:
            content = f.read()
        assert "detect_and_strip_bom" in content, \
            "canonicalize.rs 缺少 detect_and_strip_bom"
        # UTF-8 BOM: EF BB BF
        assert "0xEF" in content and "0xBB" in content and "0xBF" in content, \
            "canonicalize.rs 缺少 UTF-8 BOM 检测"
        # UTF-16 LE BOM: FF FE
        assert "0xFF" in content and "0xFE" in content, \
            "canonicalize.rs 缺少 UTF-16 BOM 检测"

    def test_canonicalize_rs_contains_crlf_normalization(self):
        """canonicalize.rs 包含 CRLF → LF 归一化逻辑"""
        with open(_CANONICALIZE_RS, "r", encoding="utf-8") as f:
            content = f.read()
        assert "cr_pending" in content or "saw_crlf" in content, \
            "canonicalize.rs 缺少 CRLF 归一化状态机"

    def test_canonicalize_rs_contains_sha256(self):
        """canonicalize.rs 使用 sha2 crate 计算 hash"""
        with open(_CANONICALIZE_RS, "r", encoding="utf-8") as f:
            content = f.read()
        assert "sha256_hex" in content, "canonicalize.rs 缺少 sha256_hex 函数"
        assert "Sha256" in content or "sha2" in content, \
            "canonicalize.rs 未使用 sha2 crate"

    def test_delta_rs_calls_canonicalize_source(self):
        """delta.rs 的 compute_parse_delta 调用 canonicalize_source"""
        with open(_DELTA_RS, "r", encoding="utf-8") as f:
            content = f.read()
        assert "canonicalize_source" in content, \
            "delta.rs 未调用 canonicalize_source"
        assert "canonicalize::canonicalize_source" in content or \
               "crate::canonicalize::canonicalize_source" in content, \
            "delta.rs 未通过完整路径调用 canonicalize::canonicalize_source"

    def test_delta_rs_calls_parse_canonical_bytes(self):
        """delta.rs 调用 parse_canonical_bytes 而非 parse_file"""
        with open(_DELTA_RS, "r", encoding="utf-8") as f:
            content = f.read()
        assert "parse_canonical_bytes" in content, \
            "delta.rs 未调用 parse_canonical_bytes"

    def test_delta_rs_no_direct_parse_file_in_compute_parse_delta(self):
        """delta.rs 的 compute_parse_delta 不再直接调用 parse_file"""
        with open(_DELTA_RS, "r", encoding="utf-8") as f:
            content = f.read()
        # 找到 compute_parse_delta 函数体
        idx = content.find("fn compute_parse_delta")
        assert idx != -1, "delta.rs 缺少 compute_parse_delta 函数"
        # 截取函数体（到下一个 fn 或文件末尾）
        func_body = content[idx:]
        # 不应包含 parser.parse_file 调用（只应包含 parse_canonical_bytes）
        assert "parser.parse_file(" not in func_body, \
            "compute_parse_delta 仍直接调用 parse_file，未改为 canonicalize"

    def test_multi_lang_rs_has_parse_canonical_bytes(self):
        """multi_lang.rs 包含 parse_canonical_bytes 方法"""
        with open(_MULTI_LANG_RS, "r", encoding="utf-8") as f:
            content = f.read()
        assert "fn parse_canonical_bytes" in content, \
            "multi_lang.rs 缺少 parse_canonical_bytes 方法"

    def test_lib_rs_registers_canonicalize(self):
        """lib.rs 注册了 canonicalize 模块和 PyO3 函数"""
        with open(_LIB_RS, "r", encoding="utf-8") as f:
            content = f.read()
        assert "mod canonicalize;" in content, \
            "lib.rs 缺少 mod canonicalize 声明"
        assert "canonicalize_source_py" in content, \
            "lib.rs 缺少 canonicalize_source_py 函数注册"
        assert "wrap_pyfunction!(canonicalize_source_py" in content, \
            "lib.rs 未在 module 中注册 canonicalize_source_py"


# ============================================
# 功能测试（需要 Rust 扩展已编译包含 canonicalize_source_py）
# ============================================


def _write_temp_file(raw_bytes: bytes) -> str:
    """将字节写入临时文件，返回路径"""
    fd, path = tempfile.mkstemp(suffix=".py")
    try:
        os.write(fd, raw_bytes)
        os.close(fd)
    except Exception:
        os.close(fd)
        raise
    return path


@pytest.mark.skipif(not HAS_CANONICALIZE, reason="callwarden_core 未导出 canonicalize_source_py")
class TestCanonicalizeSourceFunctional:
    """功能测试：调用 canonicalize_source_py 验证规范化逻辑"""

    def test_function_exists_and_callable(self):
        """canonicalize_source_py 存在且可调用"""
        assert callable(canonicalize_source_py)

    def test_utf8_no_bom(self):
        """UTF-8 无 BOM：canonical_bytes == input，newline_style == lf"""
        raw = b"a\nb"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["canonical_bytes"] == b"a\nb"
            assert result["metadata"]["bom_kind"] == "none"
            assert result["metadata"]["source_encoding"] == "utf-8"
            assert result["metadata"]["newline_style"] == "lf"
            assert result["canonical_total"] == 3
            assert result["raw_total"] == 3
        finally:
            os.unlink(path)

    def test_crlf_to_lf(self):
        """CRLF to LF: b"a\\r\\nb" -> canonical b"a\\nb", newline_style == crlf"""
        raw = b"a\r\nb"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["canonical_bytes"] == b"a\nb"
            assert result["metadata"]["newline_style"] == "crlf"
            assert result["metadata"]["bom_kind"] == "none"
        finally:
            os.unlink(path)

    def test_utf8_bom_stripped(self):
        """UTF-8 BOM stripped: b"\\xef\\xbb\\xbfa" -> canonical b"a", bom_kind == utf-8"""
        raw = b"\xef\xbb\xbfa"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["canonical_bytes"] == b"a"
            assert result["metadata"]["bom_kind"] == "utf-8"
            assert result["metadata"]["source_encoding"] == "utf-8"
        finally:
            os.unlink(path)

    def test_content_hash_is_sha256_of_canonical(self):
        """content_hash 是 canonical_bytes 的 SHA-256"""
        raw = b"hello\n"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            expected_hash = hashlib.sha256(result["canonical_bytes"]).hexdigest()
            assert result["content_hash"] == expected_hash
        finally:
            os.unlink(path)

    def test_raw_hash_differs_when_bom_present(self):
        """有 BOM 时 raw_hash != content_hash"""
        raw = b"\xef\xbb\xbfhello"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["metadata"]["raw_hash"] != result["content_hash"]
            # raw_hash 是原始字节的 SHA-256
            assert result["metadata"]["raw_hash"] == hashlib.sha256(raw).hexdigest()
        finally:
            os.unlink(path)

    def test_utf16_le_bom(self):
        """UTF-16 LE BOM + "A"：→ canonical b"A" """
        raw = b"\xff\xfe\x41\x00"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["canonical_bytes"] == b"A"
            assert result["metadata"]["bom_kind"] == "utf-16-le"
            assert result["metadata"]["source_encoding"] == "utf-16-le"
        finally:
            os.unlink(path)

    def test_utf16_be_bom(self):
        """UTF-16 BE BOM + "A"：→ canonical b"A" """
        raw = b"\xfe\xff\x00\x41"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["canonical_bytes"] == b"A"
            assert result["metadata"]["bom_kind"] == "utf-16-be"
        finally:
            os.unlink(path)

    def test_mixed_crlf_lf(self):
        """\\r\\n\\n：CRLF + lone LF → \\n\\n"""
        raw = b"\r\n\n"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["canonical_bytes"] == b"\n\n"
            assert result["metadata"]["newline_style"] == "crlf"
        finally:
            os.unlink(path)

    def test_lone_cr(self):
        """lone CR: a\\rb -> a\\nb, newline_style == cr"""
        raw = b"a\rb"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["canonical_bytes"] == b"a\nb"
            assert result["metadata"]["newline_style"] == "cr"
        finally:
            os.unlink(path)

    def test_empty_file(self):
        """空文件"""
        raw = b""
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["canonical_bytes"] == b""
            assert result["canonical_total"] == 0
            assert result["raw_total"] == 0
            assert result["metadata"]["bom_kind"] == "none"
            assert result["metadata"]["newline_style"] == "none"
        finally:
            os.unlink(path)

    def test_nonexistent_file_raises(self):
        """不存在的文件报错"""
        with pytest.raises(Exception):
            canonicalize_source_py("/nonexistent/path/file.txt")

    def test_cjk_utf8(self):
        """CJK 字符 UTF-8 编码保持不变"""
        raw = b"\xe4\xb8\xad"  # "中"
        path = _write_temp_file(raw)
        try:
            result = canonicalize_source_py(path)
            assert result["canonical_bytes"] == b"\xe4\xb8\xad"
            assert result["metadata"]["source_encoding"] == "utf-8"
        finally:
            os.unlink(path)


# ============================================
# delta.rs 集成验证（需要 Rust 扩展已编译）
# ============================================


@pytest.mark.skipif(not HAS_CANONICALIZE, reason="callwarden_core 未导出 canonicalize_source_py")
class TestDeltaIntegrationWithCanonicalize:
    """验证 delta.rs compute_parse_delta 通过 canonicalize_source 解析"""

    def test_delta_computes_with_crlf_file(self):
        """带 CRLF 的文件能正确解析（CRLF 已归一化为 LF）"""
        try:
            from callwarden_core import PyDeltaComputer
        except ImportError:
            pytest.skip("PyDeltaComputer 不可用")

        raw = b"def hello():\r\n    return 42\r\n"
        path = _write_temp_file(raw)
        try:
            delta = PyDeltaComputer.compute_parse_delta(path)
            # content_hash 应基于 canonical bytes（CRLF → LF 后）
            expected_canonical = b"def hello():\n    return 42\n"
            expected_hash = hashlib.sha256(expected_canonical).hexdigest()
            assert delta.content_hash == expected_hash
            assert delta.language == "python"
            assert delta.total_lines >= 2  # canonical 后至少 2 行（parser 可能计末尾换行）
        finally:
            os.unlink(path)

    def test_delta_computes_with_utf8_bom_file(self):
        """带 UTF-8 BOM 的文件能正确解析（BOM 已剥离）"""
        try:
            from callwarden_core import PyDeltaComputer
        except ImportError:
            pytest.skip("PyDeltaComputer 不可用")

        raw = b"\xef\xbb\xbfdef hello():\n    return 42\n"
        path = _write_temp_file(raw)
        try:
            delta = PyDeltaComputer.compute_parse_delta(path)
            # content_hash 应基于 canonical bytes（BOM 剥离后）
            expected_canonical = b"def hello():\n    return 42\n"
            expected_hash = hashlib.sha256(expected_canonical).hexdigest()
            assert delta.content_hash == expected_hash
        finally:
            os.unlink(path)
