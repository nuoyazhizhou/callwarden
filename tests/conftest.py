"""pytest 全局配置：测试数据库隔离（防止污染 ~/.callwarden/callwarden.db）"""
import pytest

from callwarden import config as _cw_config
from callwarden.db import db_base as _db_base


@pytest.fixture(autouse=True)
def _isolate_db_path(tmp_path, monkeypatch):
    """自动隔离测试数据库路径，避免污染 ~/.callwarden/callwarden.db"""
    def _fake_get_project_db_path(project_root: str = "") -> str:
        return str(tmp_path / "test_isolated.db")

    monkeypatch.setattr(_cw_config, "get_project_db_path", _fake_get_project_db_path)
    monkeypatch.setattr(_db_base, "get_project_db_path", _fake_get_project_db_path)
    yield
