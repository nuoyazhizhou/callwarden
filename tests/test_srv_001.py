"""SRV-001：server mcp common Python authority → Rust daemon 收敛测试。

fixture 矩阵（task SRV-001 step[2] check_items）：
- success：daemon 返回权威任务库路径；
- invalid：daemon 返回缺 db_path 的响应（graceful，不回退 SQLite）；
- authority：daemon 拒绝（DaemonUnavailableError）；
- unavailable：daemon 不可用（DaemonUnavailableError，fail-closed）；
- restart：首次不可用后恢复。

不变量断言：迁移后 `_get_db_path_for_daemon` 不再调用 `get_db`（无本地 SQLite authority）。
"""

import pytest

from callwarden.server import _mcp_common
from callwarden.server.daemon_client import DaemonUnavailableError


def _patch_rpc(monkeypatch, fn):
    monkeypatch.setattr(_mcp_common, "_call_daemon_rpc", fn)


def test_success_returns_db_path(monkeypatch):
    _patch_rpc(
        monkeypatch,
        lambda method, params=None: {"db_path": "/home/u/.callwarden/callwarden.db"},
    )
    assert (
        _mcp_common._get_db_path_for_daemon() == "/home/u/.callwarden/callwarden.db"
    )


def test_success_echoes_workspace_instance_id(monkeypatch):
    _patch_rpc(
        monkeypatch,
        lambda method, params=None: {
            "db_path": "/home/u/.callwarden/callwarden.db",
            "workspace_instance_id": "ws-abc",
        },
    )
    assert _mcp_common._get_db_path_for_daemon() == "/home/u/.callwarden/callwarden.db"


def test_invalid_response_no_db_path_is_graceful(monkeypatch):
    # daemon 返回缺 db_path 字段的响应：graceful 返回空串，不抛、不回退 SQLite。
    _patch_rpc(monkeypatch, lambda method, params=None: {"unexpected": True})
    assert _mcp_common._get_db_path_for_daemon() == ""


def test_authority_denied_raises(monkeypatch):
    def boom(method, params=None):
        raise DaemonUnavailableError("daemon 拒绝（authority denied）")

    _patch_rpc(monkeypatch, boom)
    with pytest.raises(DaemonUnavailableError):
        _mcp_common._get_db_path_for_daemon()


def test_daemon_unavailable_raises(monkeypatch):
    def boom(method, params=None):
        raise DaemonUnavailableError("daemon 不可用")

    _patch_rpc(monkeypatch, boom)
    with pytest.raises(DaemonUnavailableError):
        _mcp_common._get_db_path_for_daemon()


def test_restart_recovers(monkeypatch):
    states = {"n": 0}

    def flaky(method, params=None):
        states["n"] += 1
        if states["n"] == 1:
            raise DaemonUnavailableError("daemon 重启中")
        return {"db_path": "/home/u/.callwarden/callwarden.db"}

    _patch_rpc(monkeypatch, flaky)
    with pytest.raises(DaemonUnavailableError):
        _mcp_common._get_db_path_for_daemon()
    assert _mcp_common._get_db_path_for_daemon() == "/home/u/.callwarden/callwarden.db"


def test_no_get_db_called_after_migration(monkeypatch):
    # 迁移后 _get_db_path_for_daemon 不得再调用 get_db（无本地 SQLite authority）。
    calls = []

    def fake_get_db(*a, **k):
        calls.append(1)
        raise AssertionError("_get_db_path_for_daemon 不应再调用 get_db")

    monkeypatch.setattr(_mcp_common, "get_db", fake_get_db)
    _patch_rpc(
        monkeypatch,
        lambda method, params=None: {"db_path": "/x/callwarden.db"},
    )
    assert _mcp_common._get_db_path_for_daemon() == "/x/callwarden.db"
    assert calls == []
