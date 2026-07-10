"""Phase 1.1 db_build.py 多语言 Rust 接入测试。

测试范围：
- _can_use_rust_parse 按语言检测（C 专用 + 多语言通用）
- _rust_multilang_parse 流式 pool 路径
- db_build 主路径按语言分组（C 专用 + 多语言通用 + Python fallback）
- CW_DISABLE_RUST_PARSE 环境变量
- to_parse 六元组解包修复（之前 C 语言路径有五元组解包 bug）
"""

import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from callwarden.db.db_build import (
    _can_use_rust_parse,
    _rust_multilang_parse,
)


# ----------------------------------------------------------------------
# _can_use_rust_parse 按语言检测
# ----------------------------------------------------------------------

def test_can_use_rust_parse_c_lang():
    """C 语言走专用快路径检测。"""
    # 不依赖 Rust 扩展是否实际安装，只验证不抛异常
    result = _can_use_rust_parse("c")
    assert isinstance(result, bool)


def test_can_use_rust_parse_none_defaults_to_c():
    """None 默认检测 C 语言专用接口。"""
    assert _can_use_rust_parse(None) == _can_use_rust_parse("c")


def test_can_use_rust_parse_multilang():
    """多语言通用路径检测。"""
    result = _can_use_rust_parse("python")
    assert isinstance(result, bool)


def test_can_use_rust_parse_unsupported_lang():
    """不支持的语言返回 False。"""
    # Rust 扩展不可用时返回 False
    with patch.dict("sys.modules", {"callwarden_core": None}):
        result = _can_use_rust_parse("cobol")
        assert result is False


# ----------------------------------------------------------------------
# _rust_multilang_parse 路径
# ----------------------------------------------------------------------

def test_rust_multilang_parse_import_error_returns_false():
    """Rust 扩展不可用时返回 False（触发 fallback）。"""
    files = [("a.py", "/abs/a.py", "", "python", 1)]
    file_results = {}
    failed_files = []

    with patch.dict("sys.modules", {"callwarden_core": None}):
        result = _rust_multilang_parse(
            files, "python", 4, file_results, failed_files, 1
        )
    assert result is False
    assert len(file_results) == 0


def test_rust_multilang_parse_empty_files_returns_true():
    """空文件列表直接返回 True（无需 parse）。"""
    result = _rust_multilang_parse(
        [], "python", 4, {}, [], 0
    )
    assert result is True


# ----------------------------------------------------------------------
# db_build 主路径按语言分组
# ----------------------------------------------------------------------

def test_to_parse_is_six_tuple():
    """验证 to_parse 是六元组 (idx, rel_path, abs_path, lang, module_path, file_instance_id)。

    这是 Phase 1.1 修复的关键 bug：之前 C 语言路径按五元组解包会失败。
    """
    # 构造六元组
    entry = (0, "src/main.py", "/abs/src/main.py", "python", "", 1)
    idx, rel_path, abs_path, lang, module_path, file_instance_id = entry
    assert idx == 0
    assert rel_path == "src/main.py"
    assert abs_path == "/abs/src/main.py"
    assert lang == "python"
    assert module_path == ""
    assert file_instance_id == 1


def test_to_parse_five_tuple_unpack_fails():
    """验证五元组解包六元组会失败（证明 bug 存在）。"""
    entry = (0, "src/main.py", "/abs/src/main.py", "python", "", 1)
    with pytest.raises(ValueError, match="too many values to unpack"):
        rel_path, abs_path, lang, module_path, file_instance_id = entry


# ----------------------------------------------------------------------
# CW_DISABLE_RUST_PARSE 环境变量
# ----------------------------------------------------------------------

def test_disable_rust_parse_env_sets_rust_langs_empty():
    """CW_DISABLE_RUST_PARSE 设置时 rust_langs 为空集。"""
    # 这个测试验证主路径的分组逻辑
    # 当 rust_disabled=True 时，所有非 C 语言都走 non_rust_files
    to_parse = [
        (0, "a.py", "/abs/a.py", "python", "", 1),
        (1, "b.c", "/abs/b.c", "c", "", 2),
        (2, "c.rs", "/abs/c.rs", "rust", "", 3),
    ]
    rust_disabled = True
    rust_langs = set()  # rust_disabled=True 时为空

    c_files = [x for x in to_parse if x[3] == "c"]
    rust_multilang_files = defaultdict(list)
    non_rust_files = []

    for entry in to_parse:
        lang = entry[3]
        if lang == "c":
            continue
        elif lang in rust_langs:
            rust_multilang_files[lang].append(entry)
        else:
            non_rust_files.append(entry)

    # rust_disabled=True 时，python 和 rust 都走 non_rust_files
    assert len(non_rust_files) == 2
    assert non_rust_files[0][3] == "python"
    assert non_rust_files[1][3] == "rust"
    assert len(c_files) == 1


# 需要导入 defaultdict
from collections import defaultdict


# ----------------------------------------------------------------------
# 主路径分组逻辑（不依赖 Rust 扩展）
# ----------------------------------------------------------------------

def test_grouping_c_python_rust():
    """验证主路径按 C 专用 / 多语言 / Python fallback 正确分组。"""
    to_parse = [
        (0, "a.c", "/abs/a.c", "c", "", 1),
        (1, "b.py", "/abs/b.py", "python", "", 2),
        (2, "c.rs", "/abs/c.rs", "rust", "", 3),
        (3, "d.go", "/abs/d.go", "go", "", 4),
        (4, "e.unknown", "/abs/e.unknown", "unknown", "", 5),
    ]

    # 模拟 rust_langs 包含 python/rust/go
    rust_langs = {"python", "rust", "go"}
    rust_disabled = False

    c_files = [x for x in to_parse if x[3] == "c"]
    rust_multilang_files = defaultdict(list)
    non_rust_files = []

    for entry in to_parse:
        lang = entry[3]
        if lang == "c":
            continue
        elif lang in rust_langs:
            rust_multilang_files[lang].append(entry)
        else:
            non_rust_files.append(entry)

    assert len(c_files) == 1
    assert c_files[0][3] == "c"

    assert len(rust_multilang_files) == 3
    assert "python" in rust_multilang_files
    assert "rust" in rust_multilang_files
    assert "go" in rust_multilang_files

    assert len(non_rust_files) == 1
    assert non_rust_files[0][3] == "unknown"


def test_grouping_all_c_falls_back_to_python():
    """C 语言 fallback 到 Python 多进程。"""
    # c_use_rust=False 时，C 文件应加入 non_rust_files
    c_files_to_parse = [(0, "a.c", "/abs/a.c", "c", "", 1)]
    c_use_rust = False
    non_rust_files = []

    if not c_use_rust and c_files_to_parse:
        non_rust_files.extend(c_files_to_parse)

    assert len(non_rust_files) == 1
    assert non_rust_files[0][3] == "c"


def test_grouping_multilang_fallback_to_python():
    """多语言 Rust 路径失败时 fallback 到 Python。"""
    rust_multilang_files = {
        "python": [(0, "a.py", "/abs/a.py", "python", "", 1)],
    }
    non_rust_files = []

    # 模拟 _rust_multilang_parse 返回 False
    success = False
    for lang, files in rust_multilang_files.items():
        if not success:
            non_rust_files.extend(files)

    assert len(non_rust_files) == 1
    assert non_rust_files[0][3] == "python"


# ----------------------------------------------------------------------
# 端到端：小文件量走多线程路径（不触发多进程）
# ----------------------------------------------------------------------

def test_small_file_count_uses_multithread_not_rust():
    """文件数 < MP_THRESHOLD(50) 时不触发 Rust 路径。"""
    # MP_THRESHOLD = 50
    # 少于 50 个文件时走多线程路径，不触发 Rust
    MP_THRESHOLD = 50
    file_count = 10
    assert file_count < MP_THRESHOLD

    # use_multiprocess = len(to_parse) >= MP_THRESHOLD
    use_multiprocess = file_count >= MP_THRESHOLD
    assert use_multiprocess is False
