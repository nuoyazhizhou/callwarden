"""P15.2: 真懒加载 + 动态资源检测测试。

覆盖：
- _get_or_create_parser 按语言直接 import 具体模块（不经过 parsers/__init__.py 聚合入口）
- _detect_optimal_workers 根据 CPU/内存动态计算 worker 数
- _get_available_memory_mb 跨平台内存检测
"""
import os
import sys
import inspect

import pytest

from callwarden.db.db_build import (
    _get_or_create_parser,
    _detect_optimal_workers,
    _get_available_memory_mb,
    _WORKER_MEM_BUDGET_MB,
    _HOST_RESERVED_MEM_MB,
    _MAX_WORKERS_CAP,
    _MIN_WORKERS,
    _worker_parsers,
)


# ============================================
# 真懒加载测试
# ============================================

def test_get_or_create_parser_no_aggregate_import():
    """_get_or_create_parser 不应使用 `from ..parsers import ...` 聚合 import。

    用户指出：parsers/__init__.py 顶层 import 了所有 16 个 parser 模块，
    每个 parser 模块又顶层 import tree-sitter grammar。
    所以 `from ..parsers import CParser` 会全量加载所有 grammar（~300MB）。

    应改为 `from ..parsers.c_parser import CParser` 只加载需要的 grammar。
    """
    src = inspect.getsource(_get_or_create_parser)
    # 不应再有聚合 import
    assert "from ..parsers import (" not in src
    assert "from ..parsers import RustParser" not in src
    # 应该是按语言直接 import 具体模块
    assert "from ..parsers.c_parser import CParser" in src
    assert "from ..parsers.rust import RustParser" in src
    assert "from ..parsers.python_parser import PythonParser" in src


def test_get_or_create_parser_lazy_load_python():
    """Python parser 按需创建并缓存。"""
    import callwarden.db.db_build as db_build_mod
    db_build_mod._worker_parsers = {}

    p1 = _get_or_create_parser("python", "test.py")
    assert p1 is not None
    assert "python" in db_build_mod._worker_parsers

    p2 = _get_or_create_parser("python", "test2.py")
    assert p2 is p1  # 缓存生效


def test_get_or_create_parser_lazy_load_c():
    """C parser 按需创建（真懒加载，只加载 tree_sitter_c）。"""
    import callwarden.db.db_build as db_build_mod
    db_build_mod._worker_parsers = {}

    p = _get_or_create_parser("c", "test.c")
    assert p is not None
    assert "c" in db_build_mod._worker_parsers


def test_get_or_create_parser_unknown_lang():
    """未知语言返回 None。"""
    p = _get_or_create_parser("unknown_lang", "test.xyz")
    assert p is None


def test_parse_one_uses_get_or_create_parser():
    """多线程路径 _parse_one 应复用 _get_or_create_parser（统一懒加载入口）。

    不应在 _parse_one 内重复写 `from ..parsers import (...)` 聚合 import。
    """
    import callwarden.db.db_build as db_build_mod
    src = inspect.getsource(db_build_mod.BuildMixin._build_multi_lang)
    # 应该调用 _get_or_create_parser
    assert "_get_or_create_parser" in src
    # 不应再有内联的聚合 import
    assert "from ..parsers import (\n                            RustParser" not in src


# ============================================
# 动态资源检测测试
# ============================================

def test_detect_optimal_workers_returns_int():
    """_detect_optimal_workers 应返回整数 worker 数。"""
    workers = _detect_optimal_workers()
    assert isinstance(workers, int)
    assert workers >= _MIN_WORKERS
    assert workers <= _MAX_WORKERS_CAP


def test_detect_optimal_workers_respects_min():
    """worker 数不应低于 _MIN_WORKERS（1）。"""
    workers = _detect_optimal_workers()
    assert workers >= _MIN_WORKERS


def test_detect_optimal_workers_respects_max():
    """worker 数不应超过 _MAX_WORKERS_CAP（8）。"""
    workers = _detect_optimal_workers()
    assert workers <= _MAX_WORKERS_CAP


def test_detect_optimal_workers_uses_cpu_count():
    """worker 数应考虑 CPU 核心数（不超过 cpu_count - 1）。"""
    cpu_count = os.cpu_count() or 4
    workers = _detect_optimal_workers()
    # 不超过 cpu_count - 1（留 1 核给主进程）
    assert workers <= max(_MIN_WORKERS, cpu_count - 1)


def test_detect_optimal_workers_uses_memory():
    """worker 数应考虑可用内存（每 worker 预算 _WORKER_MEM_BUDGET_MB）。"""
    workers = _detect_optimal_workers()
    available = _get_available_memory_mb()

    if available is not None:
        # 可用内存减去保留量后，按预算计算的最大 worker 数
        usable = available - _HOST_RESERVED_MEM_MB
        if usable <= 0:
            assert workers == _MIN_WORKERS
        else:
            max_by_mem = max(_MIN_WORKERS, int(usable / _WORKER_MEM_BUDGET_MB))
            assert workers <= min(max_by_mem, _MAX_WORKERS_CAP)


def test_get_available_memory_mb_returns_positive_or_none():
    """_get_available_memory_mb 应返回正数或 None。"""
    result = _get_available_memory_mb()
    if result is not None:
        assert result > 0
        # 至少应该有几十 MB（任何现代机器都满足）
        assert result > 10


def test_constants_reasonable():
    """常量值合理性检查。"""
    assert _WORKER_MEM_BUDGET_MB > 0
    assert _HOST_RESERVED_MEM_MB > 0
    assert _MAX_WORKERS_CAP >= 1
    assert _MIN_WORKERS >= 1
    assert _MIN_WORKERS < _MAX_WORKERS_CAP


def test_detect_optimal_workers_is_module_level():
    """_detect_optimal_workers 应是模块级函数（可 pickle）。"""
    import pickle
    pickle.dumps(_detect_optimal_workers)
    assert "<locals>" not in _detect_optimal_workers.__qualname__


def test_get_available_memory_mb_is_module_level():
    """_get_available_memory_mb 应是模块级函数。"""
    import pickle
    pickle.dumps(_get_available_memory_mb)


# ============================================
# 集成测试：动态 worker 数实际生效
# ============================================

def test_multiprocess_uses_dynamic_workers(tmp_path):
    """多进程路径应使用 _detect_optimal_workers() 决定 worker 数。"""
    # 创建 50+ 文件触发多进程路径
    for i in range(60):
        (tmp_path / f"mod_{i}.py").write_text(
            f'def func_{i}():\n    return {i}\n', encoding="utf-8"
        )

    # 验证 _detect_optimal_workers 能正常工作
    workers = _detect_optimal_workers()
    assert workers >= _MIN_WORKERS

    # 验证 build 能正常完成
    from callwarden.db.db import CodeGraphDB
    db = CodeGraphDB(str(tmp_path / "cw.db"), workspace_root=str(tmp_path))
    db.register_workspace("test", str(tmp_path), "测试")
    db.build_full_graph()

    stats = db.get_stats()
    assert stats["total_symbols"] >= 60
    db.close()
