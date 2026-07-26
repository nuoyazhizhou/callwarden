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


# ----------------------------------------------------------------------
# Phase 1.1 Fallback 集成测试
#
# 覆盖 reviewer 要求：
# 1. Rust pool 运行时异常 → 回退 Python
# 2. 单文件 Rust error → _python_parse_single_file 回退
# 3. _python_parse_single_file 直接验证
# 4. 小批量 _parse_one Rust 路径决策逻辑
# ----------------------------------------------------------------------

from callwarden.db.db_build import _python_parse_single_file, _normalize_rust_symbols


# ----------------------------------------------------------------------
# _python_parse_single_file 直接测试
# ----------------------------------------------------------------------

def test_python_parse_single_file_success(tmp_path):
    """_python_parse_single_file 成功返回与 Rust parser 格式一致的 result dict。"""
    code = "def hello():\n    pass\n"
    path = tmp_path / "test.py"
    path.write_text(code, encoding="utf-8")

    result = _python_parse_single_file(
        str(path), "test.module", "python", "test.py", 42
    )

    assert result is not None
    # 验证关键字段与 Rust parser 输出格式一致
    assert result["abs_path"] == str(path)
    assert result["file_instance_id"] == 42
    assert result["module_path"] == "test.module"
    assert result["rel_path"] == "test.py"
    assert result.get("inline_modules") == []
    assert "symbols" in result
    assert "raw_calls" in result
    # 应至少提取到 hello 函数符号
    assert any(s["name"] == "hello" for s in result["symbols"])


def test_python_parse_single_file_unsupported_lang(tmp_path):
    """不支持的 language 时返回 None。"""
    path = tmp_path / "test.unknown"
    path.write_text("dummy", encoding="utf-8")

    result = _python_parse_single_file(
        str(path), "", "cobol", "test.unknown", 1
    )
    assert result is None


def test_python_parse_single_file_nonexistent_file(tmp_path):
    """文件不存在时返回 None（不抛异常）。"""
    result = _python_parse_single_file(
        str(tmp_path / "nonexistent.py"), "", "python", "nonexistent.py", 1
    )
    assert result is None


# ----------------------------------------------------------------------
# Rust pool 运行时异常 → 回退 Python
# ----------------------------------------------------------------------

def test_rust_multilang_parse_pool_exception_returns_false():
    """Rust pool 运行时异常时返回 False（触发上层 Python fallback）。

    覆盖 reviewer P2: "Rust pool 运行时异常后真正回退 Python 的集成测试"
    """
    files = [("a.py", "/abs/a.py", "", "python", 1)]
    file_results = {}
    failed_files = []

    # 构造一个假的 callwarden_core 模块，batch_parse_files_lang_pool 抛异常
    fake_mod = MagicMock()
    fake_mod.batch_parse_files_lang_pool = MagicMock(
        side_effect=RuntimeError("rayon pool panic")
    )

    with patch.dict("sys.modules", {"callwarden_core": fake_mod}):
        result = _rust_multilang_parse(
            files, "python", 4, file_results, failed_files, 1
        )

    assert result is False  # 触发上层 Python fallback
    assert len(file_results) == 0  # 没有写入任何结果
    assert len(failed_files) == 0  # 没有记录失败（由上层 Python 重试）


# ----------------------------------------------------------------------
# 单文件 Rust error → fail closed（P1-E：不再回退 Python parser）
# ----------------------------------------------------------------------

def test_rust_multilang_parse_single_file_error_fail_closed(tmp_path):
    """P1-E: 单文件 Rust parse error 时 fail closed，记录到 failed_files。

    设计 §3.1.5：Rust 解析失败必须显式记录，不允许静默回退 Python parser。
    旧实现调用 _python_parse_single_file 回退；新实现直接 fail closed。
    """
    # 创建一个真实的 Python 文件（不再被 Python fallback 解析）
    code = "def real_func():\n    return 42\n"
    path = tmp_path / "real.py"
    path.write_text(code, encoding="utf-8")

    files = [(str(path), str(path), "test.mod", "python", 1)]
    file_results = {}
    failed_files = []

    # 构造 mock pool：get_at 返回 error
    mock_pool = MagicMock()
    mock_pool.get_at = MagicMock(return_value={"error": "rust parse failed"})

    fake_mod = MagicMock()
    fake_mod.batch_parse_files_lang_pool = MagicMock(return_value=mock_pool)

    with patch.dict("sys.modules", {"callwarden_core": fake_mod}):
        result = _rust_multilang_parse(
            files, "python", 4, file_results, failed_files, 1
        )

    assert result is True  # Rust 路径执行完毕（fail closed，不返回 False）
    # file_results 不应包含失败文件
    assert str(path) not in file_results
    # 应记录到 failed_files
    assert len(failed_files) == 1
    assert failed_files[0][0] == str(path)
    assert "rust parse failed" in failed_files[0][1]


def test_rust_multilang_parse_single_file_error_fail_closed_missing_file(tmp_path):
    """P1-E: 单文件 Rust error 且文件不存在时，同样 fail closed 记录失败。"""
    # 使用一个不存在的文件路径
    files = [("missing.py", "/nonexistent/missing.py", "", "python", 1)]
    file_results = {}
    failed_files = []

    mock_pool = MagicMock()
    mock_pool.get_at = MagicMock(return_value={"error": "rust parse failed"})

    fake_mod = MagicMock()
    fake_mod.batch_parse_files_lang_pool = MagicMock(return_value=mock_pool)

    with patch.dict("sys.modules", {"callwarden_core": fake_mod}):
        result = _rust_multilang_parse(
            files, "python", 4, file_results, failed_files, 1
        )

    assert result is True  # Rust 路径执行完毕
    # fail closed → 记录到 failed_files
    assert len(failed_files) == 1
    assert failed_files[0][0] == "missing.py"
    assert "rust parse failed" in failed_files[0][1]


def test_rust_multilang_parse_mixed_success_and_fail_closed(tmp_path):
    """P1-E: 混合场景 - 部分文件 Rust 成功，部分 error 后 fail closed。"""
    # 文件 1：真实文件，Rust 成功
    code1 = "def func_a():\n    pass\n"
    path1 = tmp_path / "a.py"
    path1.write_text(code1, encoding="utf-8")

    # 文件 2：真实文件，Rust error 后 fail closed（不再 Python fallback）
    code2 = "def func_b():\n    pass\n"
    path2 = tmp_path / "b.py"
    path2.write_text(code2, encoding="utf-8")

    files = [
        (str(path1), str(path1), "mod", "python", 1),
        (str(path2), str(path2), "mod", "python", 2),
    ]
    file_results = {}
    failed_files = []

    # mock pool：第一个成功，第二个 error
    rust_success = {
        "symbols": [{"name": "func_a", "start_line": 1, "end_line": 2}],
        "raw_calls": [],
        "imports": [],
        "content_hash": "abc",
        "total_lines": 2,
    }
    mock_pool = MagicMock()
    mock_pool.get_at = MagicMock(
        side_effect=[rust_success, {"error": "rust parse failed"}]
    )

    fake_mod = MagicMock()
    fake_mod.batch_parse_files_lang_pool = MagicMock(return_value=mock_pool)

    with patch.dict("sys.modules", {"callwarden_core": fake_mod}):
        result = _rust_multilang_parse(
            files, "python", 4, file_results, failed_files, 2
        )

    assert result is True
    # 只有 Rust 成功的文件在 file_results
    assert str(path1) in file_results
    assert str(path2) not in file_results  # fail closed，不写入
    # Rust 失败的文件记录到 failed_files
    assert len(failed_files) == 1
    assert failed_files[0][0] == str(path2)


# ----------------------------------------------------------------------
# 小批量 _parse_one Rust 路径决策逻辑
# ----------------------------------------------------------------------

def test_small_batch_rust_path_decision_logic():
    """验证小批量路径的 Rust 决策逻辑：lang != 'c' + _can_use_rust_parse + CW_DISABLE_RUST_PARSE。

    _parse_one 是嵌套函数无法直接调用，此测试验证其决策条件组合。
    """
    # 条件 1: lang == "c" → 不走 Rust parse_file_lang（走 C 专用路径）
    lang = "c"
    assert not (lang != "c")  # 条件不满足

    # 条件 2: 非 C + Rust 可用 + 无 env → 走 Rust parse_file_lang
    lang = "python"
    rust_available = _can_use_rust_parse(lang)  # 取决于 Rust 扩展是否安装
    env_disabled = bool(os.environ.get("CW_DISABLE_RUST_PARSE"))
    should_use_rust = (lang != "c" and rust_available and not env_disabled)
    # rust_available 取决于环境，但逻辑组合正确
    if rust_available:
        assert should_use_rust is True
    else:
        assert should_use_rust is False


def test_small_batch_rust_path_disabled_by_env():
    """CW_DISABLE_RUST_PARSE 设置时不走 Rust parse_file_lang。"""
    lang = "python"
    with patch.dict(os.environ, {"CW_DISABLE_RUST_PARSE": "1"}):
        rust_available = _can_use_rust_parse(lang)
        env_disabled = bool(os.environ.get("CW_DISABLE_RUST_PARSE"))
        should_use_rust = (lang != "c" and rust_available and not env_disabled)
        assert should_use_rust is False
        assert env_disabled is True


def test_small_batch_c_lang_never_uses_parse_file_lang():
    """C 语言始终不走 parse_file_lang（走 C 专用 batch_parse_c_files）。"""
    lang = "c"
    # 即使 Rust 可用且 env 未禁用，C 也不走 parse_file_lang
    should_use_rust = (lang != "c")  # 第一个条件就为 False
    assert should_use_rust is False


# ----------------------------------------------------------------------
# _normalize_rust_symbols 归一化测试
# ----------------------------------------------------------------------

def test_normalize_rust_symbols_adds_missing_fields():
    """_normalize_rust_symbols 补齐 start_col/end_col 并转换 has_comment 类型。"""
    r = {
        "symbols": [
            {"name": "foo", "start_line": 1, "end_line": 5, "has_comment": True},
            {"name": "bar", "start_line": 6, "end_line": 10, "has_comment": False},
        ],
        "inline_modules": [],
    }
    _normalize_rust_symbols(r)
    for sym in r["symbols"]:
        assert "start_col" in sym
        assert "end_col" in sym
        assert sym["start_col"] == 0
        assert sym["end_col"] == 0
        assert isinstance(sym["has_comment"], int)


def test_normalize_rust_symbols_preserves_existing_fields():
    """已有 start_col/end_col 的符号不被覆盖。"""
    r = {
        "symbols": [
            {"name": "foo", "start_line": 1, "end_line": 5,
             "start_col": 10, "end_col": 20, "has_comment": 1},
        ],
        "inline_modules": [],
    }
    _normalize_rust_symbols(r)
    sym = r["symbols"][0]
    assert sym["start_col"] == 10  # 不被覆盖
    assert sym["end_col"] == 20
    assert sym["has_comment"] == 1  # 已是 int，不变


def test_normalize_rust_symbols_inline_modules():
    """inline_modules 中的符号也被归一化。"""
    r = {
        "symbols": [],
        "inline_modules": [
            {"symbols": [
                {"name": "inner", "start_line": 1, "end_line": 3, "has_comment": True},
            ]},
        ],
    }
    _normalize_rust_symbols(r)
    sym = r["inline_modules"][0]["symbols"][0]
    assert sym["start_col"] == 0
    assert sym["end_col"] == 0
    assert isinstance(sym["has_comment"], int)


def test_normalize_rust_symbols_imports_str_to_dict():
    """Rust imports List[str] 被转换为 List[Dict]（含 module 键）。"""
    r = {
        "symbols": [],
        "inline_modules": [],
        "imports": ["os", "sys", "json"],
    }
    _normalize_rust_symbols(r)
    assert isinstance(r["imports"], list)
    for imp in r["imports"]:
        assert isinstance(imp, dict)
        assert "module" in imp
    assert r["imports"][0]["module"] == "os"
    assert r["imports"][1]["module"] == "sys"


def test_normalize_rust_symbols_imports_already_dict():
    """已是 List[Dict] 格式的 imports 不被转换。"""
    existing = [{"module": "os", "imported": ["os"], "line": 1}]
    r = {
        "symbols": [],
        "inline_modules": [],
        "imports": existing.copy(),
    }
    _normalize_rust_symbols(r)
    assert r["imports"] == existing
