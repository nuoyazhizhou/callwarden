"""CLI-005 (A′ control_plane) `cw-agent start` HTTP thin-client fixture 矩阵测试。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_005_http_rpc.py）
要求的 CLI 层 HTTP RPC 场景：
  - success：握手（ping -> workspace.connect）成功，Python 仅编排，Rust daemon 是唯一 authority；
  - daemon-unavailable：daemon 不可达 -> fail-closed，返回码 2 并清理 PID 文件；
  - 结构不变量：Python 不再持有 UnixDaemonRpcClient / Unix socket 业务路径，
    Rust daemon 是唯一 authority。

与 test_cli_006_http_rpc.py 互补：本文件聚焦 `cw-agent start` 的握手链路。
"""

import inspect

import pytest

import callwarden.cli.main as main_mod
from callwarden.server import daemon_client
from callwarden.server.daemon_client import DaemonUnavailableError


# =========================================================================
# 结构不变量：Python 不应再引用 UnixDaemonRpcClient / Unix socket 路径
# =========================================================================

def test_agent_start_no_unix_client():
    """_agent_start 不得实例化 UnixDaemonRpcClient（Unix socket 业务路径）。

    迁移后该命令只作 HTTP thin client + 编排，Rust daemon 是唯一 authority。
    """
    src = inspect.getsource(main_mod._agent_start)
    # 仅检查实际实例化（含 ASCII 括号），忽略注释中的说明文本。
    assert "UnixDaemonRpcClient(" not in src, (
        "cw-agent start 仍实例化 UnixDaemonRpcClient，违反 A′ CLI-005 迁移约束"
    )
    assert "HttpDaemonRpcClient" in src, (
        "cw-agent start 未切换到 HTTP thin client"
    )


# =========================================================================
# 成功场景：monkeypatch HTTP client 完成 ping -> workspace.connect 握手
# =========================================================================

class _FakeHttpClient:
    def __init__(self, ping=None, connect=None, register=None):
        self.ping_resp = ping or {"status": "ok", "peer_uid": "u-1", "pid": 4242}
        self.connect_resp = connect if connect is not None else {"session_epoch": 1}
        self.register_resp = register if register is not None else {}
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == "ping":
            return self.ping_resp
        if method == "workspace.connect":
            return self.connect_resp
        if method == "workspace.register":
            return self.register_resp
        raise DaemonUnavailableError(f"unknown method {method}")


class _FakeSession:
    session_id = "sess-fake-1"

    def register_workspace(self, ws):
        pass

    def set_epoch(self, ws, ep):
        pass


def _patch_runtime(monkeypatch, tmp_path, client):
    """将 _agent_start 的全部副作用重定向到临时路径与假对象，避免触碰个人目录。"""
    monkeypatch.setattr(daemon_client, "HttpDaemonRpcClient", lambda: client)
    monkeypatch.setattr(main_mod, "_agent_pid_file", lambda: str(tmp_path / "agent.pid"))
    monkeypatch.setattr(main_mod, "_agent_log_file", lambda: str(tmp_path / "agent.log"))
    monkeypatch.setattr(
        "callwarden.server.agent_session.AgentSession.create_or_load",
        lambda: _FakeSession(),
    )
    monkeypatch.setattr("callwarden.server.agent_watcher.HAS_WATCHDOG", True)
    monkeypatch.setattr(
        "callwarden.server.agent_watcher.run_agent_watcher_loop",
        lambda **kwargs: 0,
    )
    monkeypatch.setattr(
        "callwarden.server.daemon_client.derive_workspace_instance_id",
        lambda d: "ws-auto-1",
    )


def test_agent_start_handshake_success(monkeypatch, tmp_path):
    """success：ping + workspace.connect 成功，Python 仅编排，返回 0。"""
    client = _FakeHttpClient()
    _patch_runtime(monkeypatch, tmp_path, client)
    rc = main_mod._agent_start(["--watch-dir", str(tmp_path)])
    assert rc == 0
    methods = [c[0] for c in client.calls]
    assert "ping" in methods
    assert "workspace.connect" in methods


def test_agent_start_daemon_unavailable(monkeypatch, tmp_path):
    """daemon 不可达 -> fail-closed，返回码 2，并清理 PID 文件。"""

    def _boom(method, params=None):
        raise DaemonUnavailableError("E_HTTP_DAEMON_UNAVAILABLE: connection refused")

    client = _FakeHttpClient()
    client.call = _boom
    _patch_runtime(monkeypatch, tmp_path, client)
    rc = main_mod._agent_start(["--watch-dir", str(tmp_path)])
    assert rc == 2
    assert not (tmp_path / "agent.pid").exists(), "失败时应清理 PID 文件"


def test_agent_start_watch_dir_missing(monkeypatch, tmp_path):
    """watch-dir 不存在 -> 返回 2（与 daemon 无关，纯本地校验）。"""
    _patch_runtime(monkeypatch, tmp_path, _FakeHttpClient())
    rc = main_mod._agent_start(["--watch-dir", str(tmp_path / "nope")])
    assert rc == 2
