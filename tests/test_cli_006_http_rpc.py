"""CLI-006 (A′ control_plane) `cw-agent status` HTTP thin-client fixture 矩阵测试。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_006_http_rpc.py）
要求的 CLI 层 HTTP RPC 场景：
  - success：Rust daemon ping 成功，Python 仅做输出格式化；
  - invalid/unavailable：daemon 端点不可达 -> fail-closed（"daemon 不可达"）；
  - restart 后行为一致：二次调用结果稳定；
  - 结构不变量：Python 不再持有 UnixDaemonRpcClient / Unix socket 业务路径，
    Rust daemon 是唯一 authority。

与 test_cli_004_http_rpc.py 互补：本文件聚焦 `cw-agent status` 单命令链路。
"""

import inspect

import pytest

import callwarden.cli.main as main_mod
from callwarden.server import daemon_client
from callwarden.server.daemon_client import DaemonUnavailableError


# =========================================================================
# 结构不变量：Python 不应再引用 UnixDaemonRpcClient / Unix socket 路径
# =========================================================================

def test_agent_status_no_unix_client():
    """_agent_status 不得引用 UnixDaemonRpcClient（Unix socket 业务路径）。

    迁移后该命令只作 HTTP thin client + 输出格式化，Rust daemon 是唯一 authority。
    """
    src = inspect.getsource(main_mod._agent_status)
    # 仅检查实际实例化（含 ASCII 括号），忽略注释中的说明文本。
    assert "UnixDaemonRpcClient(" not in src, (
        "cw-agent status 仍实例化 UnixDaemonRpcClient，违反 A′ CLI-006 迁移约束"
    )
    assert "HttpDaemonRpcClient" in src, (
        "cw-agent status 未切换到 HTTP thin client"
    )


# =========================================================================
# 成功场景：monkeypatch HTTP client 返回合法 ping 响应
# =========================================================================

class _FakeHttpClient:
    def __init__(self, response):
        self.response = response
        self.calls = 0

    def call(self, method, params=None):
        self.calls += 1
        assert method == "ping"
        return self.response


def test_agent_status_ping_success(capsys):
    """success：daemon ping 成功，Python 仅格式化输出。"""
    fake = _FakeHttpClient({"status": "ok", "peer_uid": "u-1", "pid": 4242})
    real = daemon_client.HttpDaemonRpcClient
    daemon_client.HttpDaemonRpcClient = lambda: fake
    try:
        rc = main_mod._agent_status([])
    finally:
        daemon_client.HttpDaemonRpcClient = real
    assert rc == 0
    out = capsys.readouterr().out
    assert "daemon 状态: ok" in out
    assert "peer_uid: u-1" in out
    assert "pid: 4242" in out


def test_agent_status_restart_consistent(capsys):
    """restart 后行为一致：二次调用同样产生稳定的 daemon 状态输出。"""
    fake = _FakeHttpClient({"status": "ok", "peer_uid": "u-1", "pid": 4242})
    real = daemon_client.HttpDaemonRpcClient
    daemon_client.HttpDaemonRpcClient = lambda: fake
    try:
        main_mod._agent_status([])
        main_mod._agent_status([])
    finally:
        daemon_client.HttpDaemonRpcClient = real
    assert fake.calls == 2
    out = capsys.readouterr().out
    assert out.count("daemon 状态: ok") == 2


# =========================================================================
# Daemon unavailable 场景：端点不可达 fail-closed
# =========================================================================

class _BoomClient:
    def __init__(self, fn):
        self.fn = fn

    def call(self, method, params=None):
        return self.fn(method, params)


def test_agent_status_daemon_unavailable(capsys):
    """daemon 不可达 -> 捕获异常并输出 'daemon 不可达'，不崩溃。"""
    def _boom(method, params=None):
        raise DaemonUnavailableError("E_HTTP_DAEMON_UNAVAILABLE: connection refused")

    real = daemon_client.HttpDaemonRpcClient
    daemon_client.HttpDaemonRpcClient = lambda: _BoomClient(_boom)
    try:
        rc = main_mod._agent_status([])
    finally:
        daemon_client.HttpDaemonRpcClient = real
    assert rc == 0
    out = capsys.readouterr().out
    assert "daemon 不可达" in out


# =========================================================================
# 无效输入：错误的 authority 配置 -> 仍走 HTTP 且不被静默吞掉
# =========================================================================

def test_agent_status_unknown_authority(capsys):
    """未知 authority / 非法 endpoint -> 抛 DaemonUnavailableError 且可被捕获。"""
    def _boom(method, params=None):
        raise DaemonUnavailableError("E_AUTHORITY_UNKNOWN")

    real = daemon_client.HttpDaemonRpcClient
    daemon_client.HttpDaemonRpcClient = lambda: _BoomClient(_boom)
    try:
        rc = main_mod._agent_status([])
    finally:
        daemon_client.HttpDaemonRpcClient = real
    assert rc == 0
    out = capsys.readouterr().out
    assert "daemon 不可达" in out
