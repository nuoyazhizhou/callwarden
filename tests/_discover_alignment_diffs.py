"""临时脚本：发现 Python/Rust parser 对齐差异，用于填充 KNOWN_DIFFS Counter。

用法：python tests/_discover_alignment_diffs.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from collections import Counter

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)
_PKG_ROOT = os.path.dirname(_TESTS_DIR)
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from test_p31_multi_lang import _LANGUAGE_SAMPLES
from callwarden.parsers import create_parser
from callwarden_core import parse_file_lang


def normalize_symbols(symbols):
    return Counter(
        (s["name"], s["start_line"], s["end_line"])
        for s in symbols
    )


def normalize_calls(calls):
    return Counter(
        (c["callee_name"], c["call_line"])
        for c in calls
    )


def filter_user_calls(calls, user_symbol_names):
    return [c for c in calls if c["callee_name"] in user_symbol_names]


def discover():
    tmpdir = tempfile.mkdtemp(prefix="cw_align_")
    try:
        for lang, filename, content in _LANGUAGE_SAMPLES:
            path = os.path.join(tmpdir, filename)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)

            # Python parser
            py_parser = create_parser(path)
            py_result = py_parser.parse_file(path, "test.align")

            # Rust parser
            rs_result = parse_file_lang(path, "test.align", lang)

            # Symbol diff
            py_syms = normalize_symbols(py_result["symbols"])
            rs_syms = normalize_symbols(rs_result["symbols"])
            missing_in_rs_sym = py_syms - rs_syms
            missing_in_py_sym = rs_syms - py_syms
            sym_diff = missing_in_rs_sym + missing_in_py_sym

            # Call diff (user calls only)
            py_names = {s["name"] for s in py_result["symbols"]}
            rs_names = {s["name"] for s in rs_result["symbols"]}
            user_names = py_names | rs_names
            py_calls = filter_user_calls(py_result.get("raw_calls", []), user_names)
            rs_calls = filter_user_calls(rs_result.get("raw_calls", []), user_names)
            py_call_c = normalize_calls(py_calls)
            rs_call_c = normalize_calls(rs_calls)
            missing_in_rs_call = py_call_c - rs_call_c
            missing_in_py_call = rs_call_c - py_call_c
            call_diff = missing_in_rs_call + missing_in_py_call

            if sym_diff or call_diff:
                print(f"\n{'='*60}")
                print(f"Language: {lang}")
                if sym_diff:
                    print(f"\n  SYMBOL diff (missing_in_rs + missing_in_py):")
                    for key, cnt in sorted(sym_diff.items()):
                        print(f"    {cnt}: {key}")
                if call_diff:
                    print(f"\n  CALL diff (missing_in_rs + missing_in_py):")
                    for key, cnt in sorted(call_diff.items()):
                        print(f"    {cnt}: {key}")

                # Print as Counter literal for copy-paste
                if sym_diff:
                    print(f"\n  SYMBOL Counter literal:")
                    items = ", ".join(f"{key}: {cnt}" for key, cnt in sorted(sym_diff.items()))
                    print(f"    Counter({{{items}}})")
                if call_diff:
                    print(f"\n  CALL Counter literal:")
                    items = ", ".join(f"{key}: {cnt}" for key, cnt in sorted(call_diff.items()))
                    print(f"    Counter({{{items}}})")
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    discover()
