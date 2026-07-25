"""探测 Python 与 Rust parser 在编码/错误边界下的实际行为。

只读不写，结果打印到 stdout，供 test_encoding_error.py 编写参考。
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

_PKG_ROOT = Path(__file__).resolve().parents[2]
_TESTS_DIR = Path(__file__).resolve().parents[1]
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from callwarden.parsers import create_parser  # noqa: E402

try:
    from callwarden_core import (  # type: ignore  # noqa: E402
        canonicalize_source_py as canonicalize_source,
        parse_file_lang,
    )
    HAS_RUST = True
except Exception:
    HAS_RUST = False
    canonicalize_source = None  # type: ignore
    parse_file_lang = None  # type: ignore


# 简单 Python 源码样本（含一个符号）
_PY_SAMPLE = b"def hello(name):\n    return name\n"

# 简单 Rust 源码样本
_RS_SAMPLE = b"pub fn hello(name: &str) -> &str {\n    name\n}\n"

# 简单 Go 源码样本
_GO_SAMPLE = b"package main\n\nfunc hello(name string) string {\n    return name\n}\n"


def _probe(label: str, content: bytes, lang: str, filename: str):
    print(f"\n=== {label} (lang={lang}, file={filename}, {len(content)} bytes) ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, filename)
        with open(path, "wb") as f:
            f.write(content)

        # canonicalize (Rust only)
        if HAS_RUST:
            try:
                can = canonicalize_source(path)
                meta = can["metadata"]
                print(f"  canonicalize: encoding={meta['source_encoding']}, "
                      f"bom={meta['bom_kind']}, newline={meta['newline_style']}, "
                      f"raw={can['raw_total']}B, canonical={can['canonical_total']}B")
                print(f"  canonical_bytes preview: {can['canonical_bytes'][:60]!r}")
            except Exception as e:
                print(f"  canonicalize ERROR: {type(e).__name__}: {e}")

        # Python parser
        try:
            py_parser = create_parser(path)
            if py_parser is None:
                print(f"  Python parser: 不支持 {lang}")
            else:
                py_result = py_parser.parse_file(path, "test.probe")
                print(f"  Python parser: symbols={len(py_result['symbols'])}, "
                      f"calls={len(py_result.get('raw_calls', []))}, "
                      f"imports={len(py_result.get('imports', []))}, "
                      f"total_lines={py_result.get('total_lines', 0)}")
                # 错误信息（如果有）
                if "error" in py_result:
                    print(f"    Python error field: {py_result['error']}")
        except Exception as e:
            print(f"  Python parser ERROR: {type(e).__name__}: {e}")

        # Rust parser
        if HAS_RUST:
            try:
                if lang == "c":
                    from callwarden_core import parse_c_file
                    rs_result = parse_c_file(path, "test.probe")
                else:
                    rs_result = parse_file_lang(path, "test.probe", lang)
                print(f"  Rust parser:   symbols={len(rs_result['symbols'])}, "
                      f"calls={len(rs_result.get('raw_calls', []))}, "
                      f"imports={len(rs_result.get('imports', []))}, "
                      f"total_lines={rs_result.get('total_lines', 0)}")
                if "error" in rs_result:
                    print(f"    Rust error field: {rs_result['error']}")
            except Exception as e:
                print(f"  Rust parser ERROR: {type(e).__name__}: {e}")


def main():
    if not HAS_RUST:
        print("WARNING: callwarden_core 未安装，仅探测 Python parser")

    # 1. 空文件
    _probe("empty_file", b"", "python", "empty.py")
    _probe("empty_file_rust", b"", "rust", "empty.rs")

    # 2. UTF-8 BOM
    _probe("utf8_bom_python", b"\xef\xbb\xbf" + _PY_SAMPLE, "python", "bom.py")
    _probe("utf8_bom_rust", b"\xef\xbb\xbf" + _RS_SAMPLE, "rust", "bom.rs")

    # 3. CRLF
    _probe("crlf_python", _PY_SAMPLE.replace(b"\n", b"\r\n"), "python", "crlf.py")
    _probe("crlf_rust", _RS_SAMPLE.replace(b"\n", b"\r\n"), "rust", "crlf.rs")

    # 4. UTF-16 LE BOM
    py_utf16le = b"\xff\xfe" + _PY_SAMPLE.decode("utf-8").encode("utf-16-le")
    _probe("utf16le_python", py_utf16le, "python", "utf16le.py")
    rs_utf16le = b"\xff\xfe" + _RS_SAMPLE.decode("utf-8").encode("utf-16-le")
    _probe("utf16le_rust", rs_utf16le, "rust", "utf16le.rs")

    # 5. UTF-16 BE BOM
    py_utf16be = b"\xfe\xff" + _PY_SAMPLE.decode("utf-8").encode("utf-16-be")
    _probe("utf16be_python", py_utf16be, "python", "utf16be.py")
    rs_utf16be = b"\xfe\xff" + _RS_SAMPLE.decode("utf-8").encode("utf-16-be")
    _probe("utf16be_rust", rs_utf16be, "rust", "utf16be.rs")

    # 6. GBK 编码（中文注释）
    gbk_content = "# 中文注释\n".encode("gbk") + _PY_SAMPLE
    _probe("gbk_python", gbk_content, "python", "gbk.py")

    # 7. 语法错误
    _probe("syntax_error_python", b"def :\n    pass\n", "python", "err.py")
    _probe("syntax_error_rust", b"pub fn {\n    name\n}\n", "rust", "err.rs")

    # 8. 部分代码（截断）
    _probe("truncated_python", b"def hello(name):\n    return ", "python", "trunc.py")
    _probe("truncated_rust", b"pub fn hello(name: &str) -> &", "rust", "trunc.rs")

    # 9. 超大文件（重复 5000 次相同样本）
    big_content = (_PY_SAMPLE * 5000)
    _probe("large_python", big_content, "python", "large.py")

    # 10. Go 样本（验证其他语言）
    _probe("go_sample", _GO_SAMPLE, "go", "sample.go")
    _probe("go_crlf", _GO_SAMPLE.replace(b"\n", b"\r\n"), "go", "crlf.go")


if __name__ == "__main__":
    main()
