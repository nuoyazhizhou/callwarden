"""快速探查 Rust/Python parser 的 qualified_name 格式差异。"""
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

from test_p31_multi_lang import _LANGUAGE_SAMPLES  # noqa: E402
from callwarden.parsers import create_parser  # noqa: E402
from callwarden_core import parse_file_lang  # type: ignore  # noqa: E402

# 测试 ruby 样本（最简单）
for lang, filename, content in _LANGUAGE_SAMPLES:
    if lang not in ("ruby", "rust", "go"):
        continue
    print(f"\n=== {lang} ({filename}) ===")
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        py_parser = create_parser(path)
        py_result = py_parser.parse_file(path, "test.id")
        rs_result = parse_file_lang(path, "test.id", lang)
        print(f"  Python symbols ({len(py_result['symbols'])}):")
        for s in py_result["symbols"]:
            print(f"    name={s['name']!r}, qname={s.get('qualified_name', '')!r}, "
                  f"line={s.get('start_line')}-{s.get('end_line')}")
        print(f"  Rust symbols ({len(rs_result['symbols'])}):")
        for s in rs_result["symbols"]:
            print(f"    name={s['name']!r}, qname={s.get('qualified_name', '')!r}, "
                  f"line={s.get('start_line')}-{s.get('end_line')}")
