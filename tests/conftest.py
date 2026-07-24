"""pytest 全局配置：测试数据库隔离（防止污染 ~/.callwarden/callwarden.db）

问题背景：
    CodeGraphDB(workspace_root=tmp) 不传 db_path 时，__init__ 会调用
    get_project_db_path(workspace_root) 返回用户级统一库 ~/.callwarden/callwarden.db。
    若不隔离，测试数据会污染用户的生产数据库。

方案：
    autouse fixture 在测试期间 monkey-patch get_project_db_path，
    让它返回 tmp_path 下的路径，确保测试数据库完全隔离在临时目录中，
    测试结束自动回收，不污染 ~/.callwarden/callwarden.db。

    需要 patch 两处：
    1. callwarden.config.get_project_db_path（原始定义）
    2. callwarden.db.db_base.get_project_db_path（from ..config import 引入的引用）
"""
import sys

import pytest

from callwarden import config as _cw_config
from callwarden.db import db_base as _db_base

# 子包别名：package-dir={callwarden=.} 布局下，CI 安装后
# callwarden.i18n / callwarden.server 等子包可能解析为 namespace package
# （无 __init__.py），导致 ImportError / AttributeError。
# 直接从文件系统加载各子包 __init__.py 并注册别名，确保测试能找到。
import importlib.util as _importlib_util
import os as _os

_project_root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))


def _register_subpackage(dir_name: str, module_name: str) -> None:
    """从文件系统加载子包并注册到 sys.modules（含子模块搜索路径）"""
    init_file = _os.path.join(_project_root, dir_name, '__init__.py')
    if _os.path.isfile(init_file):
        spec = _importlib_util.spec_from_file_location(
            module_name, init_file,
            submodule_search_locations=[_os.path.join(_project_root, dir_name)],
        )
        mod = _importlib_util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        sys.modules[module_name] = mod


_register_subpackage('i18n', 'callwarden.i18n')
_register_subpackage('server', 'callwarden.server')


@pytest.fixture(autouse=True)
def _isolate_db_path(tmp_path, monkeypatch):
    """自动隔离测试数据库路径，避免污染 ~/.callwarden/callwarden.db

    所有测试自动生效，无需在测试代码中显式引用。
    patch 后 CodeGraphDB(workspace_root=tmp) 的数据库会建在
    tmp_path/test_isolated.db，而非用户级统一库。
    """
    def _fake_get_project_db_path(project_root: str = "") -> str:
        # 用 tmp_path 下的路径，确保测试结束自动回收
        return str(tmp_path / "test_isolated.db")

    monkeypatch.setattr(_cw_config, "get_project_db_path", _fake_get_project_db_path)
    # db_base.py 顶层 from ..config import get_project_db_path，
    # 形成了独立引用，需要单独 patch
    monkeypatch.setattr(_db_base, "get_project_db_path", _fake_get_project_db_path)
    yield
