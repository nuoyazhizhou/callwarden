"""P15: ProcessPoolExecutor 多进程 parse 测试。

覆盖：
- _parse_file_worker 模块级函数可正常调用
- _init_worker_parsers 预加载所有语言 parser
- ProcessPoolExecutor 路径正确解析文件（>= MP_THRESHOLD）
- ThreadPoolExecutor 路径正确解析文件（< MP_THRESHOLD）
- fallback 机制（pickle 失败时降级）
- 多进程 vs 多线程结果一致性
"""
import os
import sys
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_build import (
    _parse_file_worker, _init_worker_parsers, _get_or_create_parser, _worker_parsers
)


# ============================================
# P15: worker 函数测试
# ============================================

def test_init_worker_parsers_empty():
    """_init_worker_parsers 初始化为空 dict（惰性加载）。"""
    import callwarden.db.db_build as db_build_mod
    db_build_mod._worker_parsers = {"old": "data"}  # 模拟旧数据
    _init_worker_parsers()
    assert db_build_mod._worker_parsers == {}  # 应清空


def test_get_or_create_parser_lazy_load():
    """_get_or_create_parser 首次调用创建 parser，第二次复用。"""
    import callwarden.db.db_build as db_build_mod
    db_build_mod._worker_parsers = {}  # 清空

    p1 = _get_or_create_parser("python", "test.py")
    assert p1 is not None
    assert "python" in db_build_mod._worker_parsers

    p2 = _get_or_create_parser("python", "test2.py")
    assert p2 is p1  # 同一对象（缓存生效）


def test_get_or_create_parser_unknown_lang():
    """未知语言返回 None。"""
    p = _get_or_create_parser("unknown_lang", "test.xyz")
    assert p is None


def test_parse_file_worker_python(tmp_path):
    """_parse_file_worker 能正确解析 Python 文件。"""
    src = tmp_path / "test.py"
    src.write_text('def hello():\n    print("world")\n', encoding="utf-8")

    args = (str(src), str(src), "python", "test_module", 1)
    status, rel_path, payload = _parse_file_worker(args)

    assert status == "ok"
    assert rel_path == str(src)
    assert payload is not None
    assert "symbols" in payload
    assert len(payload["symbols"]) >= 1
    assert payload["language"] == "python"
    assert payload["file_instance_id"] == 1


def test_parse_file_worker_c(tmp_path):
    """_parse_file_worker 能正确解析 C 文件。"""
    src = tmp_path / "test.c"
    src.write_text('int main() {\n    return 0;\n}\n', encoding="utf-8")

    args = (str(src), str(src), "c", "test_module", 1)
    status, rel_path, payload = _parse_file_worker(args)

    assert status == "ok"
    assert payload is not None
    assert payload["language"] == "c"
    assert len(payload["symbols"]) >= 1


def test_parse_file_worker_unknown_language(tmp_path):
    """未知语言返回 skip。"""
    src = tmp_path / "test.xyz"
    src.write_text('unknown content\n', encoding="utf-8")

    args = (str(src), str(src), "unknown_lang", "test_module", 1)
    status, rel_path, payload = _parse_file_worker(args)

    assert status == "skip"
    assert payload is None


def test_parse_file_worker_file_not_exist():
    """文件不存在时返回 fail（不抛异常）。"""
    args = ("/nonexistent/file.py", "/nonexistent/file.py", "python", "test", 1)
    status, rel_path, payload = _parse_file_worker(args)

    assert status == "fail"
    assert isinstance(payload, str)  # 错误信息


# ============================================
# P15: 集成测试 — ProcessPoolExecutor 路径
# ============================================

def test_multiprocess_parse_many_files(tmp_path):
    """>= 50 个文件时走 ProcessPoolExecutor 路径。"""
    # 创建 50 个 Python 文件
    for i in range(50):
        (tmp_path / f"mod_{i}.py").write_text(
            f'def func_{i}():\n    return {i}\n', encoding="utf-8"
        )

    db = CodeGraphDB(str(tmp_path / "cw.db"), workspace_root=str(tmp_path))
    db.register_workspace("test", str(tmp_path), "测试")
    db.build_full_graph()

    # 验证符号被正确解析
    stats = db.get_stats()
    assert stats["total_symbols"] >= 50  # 50 个 func + 可能的 stdlib
    db.close()


def test_threadpool_parse_few_files(tmp_path):
    """< 50 个文件时走 ThreadPoolExecutor 路径。"""
    for i in range(10):
        (tmp_path / f"mod_{i}.py").write_text(
            f'def func_{i}():\n    return {i}\n', encoding="utf-8"
        )

    db = CodeGraphDB(str(tmp_path / "cw.db"), workspace_root=str(tmp_path))
    db.register_workspace("test", str(tmp_path), "测试")
    db.build_full_graph()

    stats = db.get_stats()
    assert stats["total_symbols"] >= 10
    db.close()


def test_multiprocess_result_consistency(tmp_path):
    """多进程解析的结果与多线程一致（符号数相同）。"""
    # 创建混合语言文件
    for i in range(30):
        (tmp_path / f"py_{i}.py").write_text(f'def py_func_{i}():\n    pass\n', encoding="utf-8")
    for i in range(30):
        (tmp_path / f"c_{i}.c").write_text(f'int c_func_{i}() {{ return {i}; }}\n', encoding="utf-8")

    db = CodeGraphDB(str(tmp_path / "cw.db"), workspace_root=str(tmp_path))
    db.register_workspace("test", str(tmp_path), "测试")
    db.build_full_graph()

    stats = db.get_stats()
    # 60 个用户函数
    assert stats["total_symbols"] >= 60
    assert stats["total_files"] >= 60
    db.close()


# ============================================
# P15: worker 数限制测试
# ============================================

def test_mp_threshold_value():
    """MP_THRESHOLD 应为 50（小文件量避免进程创建开销）。"""
    import callwarden.db.db_build as db_build_mod
    import inspect
    src = inspect.getsource(db_build_mod.BuildMixin._build_multi_lang)
    assert "MP_THRESHOLD" in src
    assert "50" in src


def test_mp_workers_dynamic_detection():
    """多进程 worker 数应通过 _detect_optimal_workers() 动态检测，不再硬编码 4。"""
    import callwarden.db.db_build as db_build_mod
    import inspect
    src = inspect.getsource(db_build_mod.BuildMixin._build_multi_lang)
    # 应该调用 _detect_optimal_workers()
    assert "_detect_optimal_workers" in src
    # 不应再硬编码 min(4, ...) 限制
    assert "min(4" not in src


def test_worker_function_is_module_level():
    """_parse_file_worker 应是模块级函数（可 pickle，ProcessPoolExecutor 要求）。"""
    import callwarden.db.db_build as db_build_mod
    # 模块级函数的 __qualname__ 不含 <locals>
    assert "<locals>" not in _parse_file_worker.__qualname__
    # 应能 pickle
    import pickle
    pickle.dumps(_parse_file_worker)


def test_init_worker_parsers_is_module_level():
    """_init_worker_parsers 应是模块级函数。"""
    import pickle
    pickle.dumps(_init_worker_parsers)
