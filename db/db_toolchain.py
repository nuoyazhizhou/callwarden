"""db_toolchain.py —— db/ 退休过渡期重导出壳

db/ 退休 phase-3：toolchain 存储层已整体搬迁至
``callwarden.server.toolchain_store``（git mv 保留历史）。本模块仅保留
重导出，供仍在过渡期引用 ``callwarden.db.db_toolchain`` 的代码使用
（含 tests/test_phase6_* 通过文件路径动态加载的场景）；新代码应直接
``from callwarden.server.toolchain_store import ...``。
"""
from __future__ import annotations

from callwarden.server.toolchain_store import *  # noqa: F401,F403
from callwarden.server.toolchain_store import (  # noqa: F401
    # 私有内部名一并重导出，供文件路径动态加载的测试使用
    _detect_compiler_type,
    _parse_include_dirs,
    _parse_predefined_macros,
    _probe_include_dirs,
    _probe_predefined_macros,
    _probe_target_triple,
    _probe_version,
    _row_to_build_context,
    _row_to_resolved_edge,
    _row_to_toolchain,
)
