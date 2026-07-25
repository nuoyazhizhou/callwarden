"""pytest 全局配置：测试数据库隔离（防止污染 ~/.callwarden/callwarden.db）

子包别名已在项目根目录 conftest.py 中注册（先于本文件加载）。
"""
import pytest

from callwarden import config as _cw_config
from callwarden.db import db_base as _db_base

# 设置父模块属性（使 getattr(callwarden, 'X') 可用）
import callwarden as _cw  # noqa: E402
import sys as _sys

for _attr in ('i18n', 'server', 'db', 'analyzers', 'cli', 'parsers', 'cicd'):
    _mod_key = f'callwarden.{_attr}'
    if _mod_key in _sys.modules and not hasattr(_cw, _attr):
        setattr(_cw, _attr, _sys.modules[_mod_key])


@pytest.fixture(autouse=True)
def _isolate_db_path(tmp_path, monkeypatch):
    """自动隔离测试数据库路径，避免污染 ~/.callwarden/callwarden.db"""
    def _fake_get_project_db_path(project_root: str = "") -> str:
        return str(tmp_path / "test_isolated.db")

    monkeypatch.setattr(_cw_config, "get_project_db_path", _fake_get_project_db_path)
    monkeypatch.setattr(_db_base, "get_project_db_path", _fake_get_project_db_path)
    yield
