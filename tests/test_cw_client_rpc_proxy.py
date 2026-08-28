"""H6: cw-client RPC proxy 测试

验证 cw-client 角色化入口：
1. 平台门禁：linux/win32/darwin 允许（client 角色跨平台），其余平台 return 2
   （与 cw-daemon / cw-agent 一致；T04-followup S1 后平台门禁放宽至 win32/darwin）
2. 无参数打印简介
3. serve 子命令被拒绝（纯 client 角色不能启动 daemon）
4. 其他子命令委托 run_daemon_command(include_serve=False)
5. _parser(include_serve=False) 不注册 serve subparser

测试通过 mock sys.platform 模拟 Linux / 不受支持平台，避免在 Windows 上跳过测试。
"""
import argparse
import os
import sys
from unittest import mock

import pytest

from callwarden.cli.daemon_commands import _parser, run_daemon_command
from callwarden.cli.main import run_client_mode


# ----------------------------------------------------------------------
# _parser(include_serve=...) 参数化构造
# ----------------------------------------------------------------------

def test_parser_with_serve_includes_serve_subcommand():
    """_parser(include_serve=True) 注册 serve 子命令。"""
    parser = _parser(include_serve=True)
    assert _has_subcommand(parser, "serve"), "include_serve=True 必须注册 serve"


def test_parser_without_serve_excludes_serve_subcommand():
    """_parser(include_serve=False) 不注册 serve 子命令。"""
    parser = _parser(include_serve=False)
    assert not _has_subcommand(parser, "serve"), "include_serve=False 不能注册 serve"


def test_parser_without_serve_still_has_all_client_subcommands():
    """_parser(include_serve=False) 必须保留所有 client 子命令（15 个）。"""
    parser = _parser(include_serve=False)
    expected = {
        "ping", "register", "list", "status", "publish", "query", "mode",
        "health", "schema-version", "backup", "restore",
        "gc-cas", "gc-snapshots", "mount", "toolchain",
    }
    actual = set(_iter_subcommands(parser))
    missing = expected - actual
    assert not missing, f"client parser 缺少子命令: {missing}"


# ----------------------------------------------------------------------
# run_daemon_command(include_serve=False) 分发
# ----------------------------------------------------------------------

def test_run_daemon_command_accepts_include_serve_kwarg():
    """run_daemon_command 接受 include_serve 关键字参数（不报错）。"""
    # 仅验证签名，不实际执行（Windows 上 daemon 不可用）
    import inspect
    sig = inspect.signature(run_daemon_command)
    assert "include_serve" in sig.parameters, "run_daemon_command 必须接受 include_serve 参数"
    assert sig.parameters["include_serve"].default is True, "include_serve 默认应为 True"


def test_run_daemon_command_serve_rejected_when_include_serve_false():
    """run_daemon_command(argv=['serve'], include_serve=False) 应被 argparse 拒绝。"""
    # argparse 拒绝未注册的子命令，会 SystemExit(2)
    with pytest.raises(SystemExit) as exc_info:
        run_daemon_command(["serve"], include_serve=False)
    assert exc_info.value.code == 2


def test_daemon_status_sends_numeric_workspace_id_as_numeric_key(monkeypatch, capsys):
    """daemon status 的数字参数不能被误发成 workspace_instance_id。"""
    calls = []

    class FakeClient:
        def __init__(self, socket):
            self.socket = socket

        def call(self, method, params):
            calls.append((method, params))
            return {"workspace_id": 730, "workspace_instance_id": "ws-730"}

    monkeypatch.setattr(
        "callwarden.cli.daemon_commands.UnixDaemonRpcClient", FakeClient
    )
    assert run_daemon_command(["status", "730"]) == 0
    assert calls == [("workspace.status", {"workspace_id": 730})]
    assert '"workspace_id": 730' in capsys.readouterr().out


def test_daemon_status_preserves_workspace_instance_id(monkeypatch):
    """daemon status 的稳定 instance id 仍使用 instance 参数。"""
    calls = []

    class FakeClient:
        def __init__(self, socket):
            self.socket = socket

        def call(self, method, params):
            calls.append((method, params))
            return {"workspace_id": 730, "workspace_instance_id": "4baea3ff12c2ea5c"}

    monkeypatch.setattr(
        "callwarden.cli.daemon_commands.UnixDaemonRpcClient", FakeClient
    )
    assert run_daemon_command(["status", "4baea3ff12c2ea5c"]) == 0
    assert calls == [
        ("workspace.status", {"workspace_instance_id": "4baea3ff12c2ea5c"})
    ]


# ----------------------------------------------------------------------
# run_client_mode 平台门禁（linux/win32/darwin 允许，其余平台 return 2）
# ----------------------------------------------------------------------

_UNSUPPORTED_PLATFORM = "freebsd"  # 不在 ("linux","win32","darwin") 白名单内


def test_run_client_mode_unsupported_platform_returns_2(capsys):
    """不受支持平台 run_client_mode 直接 return 2。"""
    with mock.patch("sys.platform", _UNSUPPORTED_PLATFORM):
        rc = run_client_mode(["ping"])
    assert rc == 2, "不受支持平台应返回 2"
    captured = capsys.readouterr()
    assert "only supported on Linux/Windows/macOS" in captured.err, \
        "应打印 Linux/Windows/macOS-only 错误"


def test_run_client_mode_unsupported_platform_ignores_argv(capsys):
    """不受支持平台无论 argv 是什么都返回 2（不委托 daemon_command）。"""
    # 传入各种 argv 都应直接返回 2
    for argv in ([], ["ping"], ["serve"], ["--help"], ["toolchain", "list"]):
        with mock.patch("sys.platform", _UNSUPPORTED_PLATFORM):
            rc = run_client_mode(argv)
        assert rc == 2, f"argv={argv} 应返回 2"


def test_run_client_mode_win32_delegates_to_daemon_command():
    """win32（Windows）上运行 client 模式委托 run_daemon_command(include_serve=False)。

    T04-followup S1 后平台门禁放宽：win32/darwin 与 linux 同等支持纯 client
    角色（UDS/NamedPipe RPC 转发），不再 return 2。
    """
    captured_calls = []

    def fake_run_daemon_command(argv, include_serve=True):
        captured_calls.append({"argv": list(argv), "include_serve": include_serve})
        return 0

    with mock.patch("sys.platform", "win32"), \
         mock.patch(
             "callwarden.cli.daemon_commands.run_daemon_command",
             fake_run_daemon_command,
         ):
        rc = run_client_mode(["ping"])

    assert rc == 0
    assert len(captured_calls) == 1, "win32 应调用一次 run_daemon_command"
    assert captured_calls[0]["argv"] == ["ping"], "应原样透传 argv"
    assert captured_calls[0]["include_serve"] is False, \
        "cw-client 必须以 include_serve=False 调用"


# ----------------------------------------------------------------------
# run_client_mode 在 Linux 上委托 run_daemon_command
# ----------------------------------------------------------------------

def test_run_client_mode_no_args_prints_intro_on_linux(capsys):
    """Linux 上无参数时打印简介 + 子命令列表。"""
    with mock.patch("sys.platform", "linux"):
        rc = run_client_mode([])
    assert rc == 0, "无参数应返回 0"
    captured = capsys.readouterr()
    assert "Call Warden Client Mode" in captured.out
    assert "Connects to Enterprise Daemon via UDS" in captured.out
    assert "ping" in captured.out
    assert "toolchain" in captured.out


def test_run_client_mode_delegates_to_daemon_command_on_linux():
    """Linux 上有参数时委托 run_daemon_command(include_serve=False)。"""
    captured_calls = []

    def fake_run_daemon_command(argv, include_serve=True):
        captured_calls.append({"argv": list(argv), "include_serve": include_serve})
        return 0

    with mock.patch("sys.platform", "linux"), \
         mock.patch(
             "callwarden.cli.daemon_commands.run_daemon_command",
             fake_run_daemon_command,
         ):
        rc = run_client_mode(["ping"])

    assert rc == 0
    assert len(captured_calls) == 1, "应调用一次 run_daemon_command"
    assert captured_calls[0]["argv"] == ["ping"], "应原样透传 argv"
    assert captured_calls[0]["include_serve"] is False, \
        "cw-client 必须以 include_serve=False 调用"


def test_run_client_mode_unsupported_platform_does_not_call_daemon_command():
    """不受支持平台不应调用 run_daemon_command。"""
    call_count = {"count": 0}

    def fake_run_daemon_command(argv, include_serve=True):
        call_count["count"] += 1
        return 0

    with mock.patch("sys.platform", _UNSUPPORTED_PLATFORM), \
         mock.patch(
             "callwarden.cli.daemon_commands.run_daemon_command",
             fake_run_daemon_command,
         ):
        rc = run_client_mode(["ping"])

    assert rc == 2, "不受支持平台应返回 2"
    assert call_count["count"] == 0, "不受支持平台不应委托 run_daemon_command"


# ----------------------------------------------------------------------
# run_client_mode serve 子命令拒绝
# ----------------------------------------------------------------------

def test_run_client_mode_serve_rejected_on_linux(capsys):
    """Linux 上 cw-client serve 应被 argparse 拒绝（SystemExit 2）。"""
    with mock.patch("sys.platform", "linux"):
        # serve 不在 _parser(include_serve=False) 的子命令中，
        # argparse 会 SystemExit(2) 并打印 invalid choice
        with pytest.raises(SystemExit) as exc_info:
            run_client_mode(["serve"])
        assert exc_info.value.code == 2
    captured = capsys.readouterr()
    # argparse 错误信息应包含 serve 和 invalid choice
    combined = captured.err + captured.out
    assert "serve" in combined, "错误信息应提及 serve"
    assert "invalid choice" in combined or "invalid" in combined.lower(), \
        "应提示 invalid choice"


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------

def _has_subcommand(parser: argparse.ArgumentParser, name: str) -> bool:
    """检查 argparse parser 是否注册了指定子命令。"""
    return name in _iter_subcommands(parser)


def _iter_subcommands(parser: argparse.ArgumentParser):
    """迭代 parser 注册的所有子命令名称。"""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return list(action.choices.keys())
    return []
