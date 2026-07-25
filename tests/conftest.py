"""pytest 全局配置：测试数据库隔离 + 子包别名修复

1. 子包别名：package-dir={callwarden=.} 布局下，CI 安装后
   callwarden.i18n / callwarden.server 等子包可能解析为 namespace package
   （无 __init__.py），导致 ImportError / AttributeError。
   必须在导入 callwarden 之前注册别名，并设置父模块属性。

2. 测试数据库隔离：防止污染 ~/.callwarden/callwarden.db
"""
import importlib.util as _importlib_util
import os as _os
import sys

import pytest

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


# ── 第一步：注册子包别名（必须在导入 callwarden 之前）──
_register_subpackage('i18n', 'callwarden.i18n')
_register_subpackage('server', 'callwarden.server')

# DEBUG: 验证别名注册
_i18n_mod = sys.modules.get('callwarden.i18n')
print(f"[conftest] i18n registered: {_i18n_mod is not None}, __file__={getattr(_i18n_mod, '__file__', 'N/A')}")

# ── 第二步：导入 callwarden（此时子包别名已在 sys.modules 中）──
from callwarden import config as _cw_config  # noqa: E402
from callwarden.db import db_base as _db_base  # noqa: E402

# DEBUG: 导入后再次验证
_i18n_mod2 = sys.modules.get('callwarden.i18n')
print(f"[conftest] after import callwarden, i18n __file__={getattr(_i18n_mod2, '__file__', 'N/A')}")

# ── 第三步：设置父模块属性（使 getattr(callwarden, 'X') 可用）──
import callwarden as _cw  # noqa: E402

for _attr in ('i18n', 'server', 'db', 'analyzers', 'cli', 'parsers', 'cicd'):
    _mod_key = f'callwarden.{_attr}'
    if _mod_key in sys.modules and not hasattr(_cw, _attr):
        setattr(_cw, _attr, sys.modules[_mod_key])


@pytest.fixture(autouse=True)
def _isolate_db_path(tmp_path, monkeypatch):
    """自动隔离测试数据库路径，避免污染 ~/.callwarden/callwarden.db"""
    def _fake_get_project_db_path(project_root: str = "") -> str:
        return str(tmp_path / "test_isolated.db")

    monkeypatch.setattr(_cw_config, "get_project_db_path", _fake_get_project_db_path)
    monkeypatch.setattr(_db_base, "get_project_db_path", _fake_get_project_db_path)
    yield
