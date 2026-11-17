"""`cw daemon stop` 子命令单测（§9.32 P3）。

覆盖：
- 幂等：daemon 不可达 → rc 0 not_running
- POSIX happy path：SIGTERM 发送 + 轮询退出 → rc 0 stopped
- PID 复用防护：manifest pid 与 /health pid 不一致 → rc 1，绝不发信号
- 超时：daemon 在 --wait 内未退出 → rc 1 E_DAEMON_STOP_TIMEOUT
- Windows 分支：taskkill /F 调用与失败传播
- parser 注册：stop 子命令与 --wait 默认值

不依赖真实 daemon 进程：探活（HttpDaemonRpcClient.health）、manifest 解析
（resolve_http_endpoint_and_manifest）与信号发送（_send_daemon_stop_signal /
subprocess.run）全部 monkeypatch。
"""
from __future__ import annotations

import json

import pytest

import callwarden.cli.daemon_commands as dc
from callwarden.cli.daemon_commands import _parser, run_daemon_command
from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.daemon_protocol import DaemonRemoteError


def _patch_health(monkeypatch, script, calls=None):
    """把 dc.HttpDaemonRpcClient 替换为按 script 依次返回/抛出的假 client。

    script 耗尽后重复最后一项；calls 列表（若给）记录每次调用。
    """

    class _FakeClient:
        def __init__(self, *args, **kwargs):
            pass

        def health(self):
            if calls is not None:
                calls.append(1)
            item = script.pop(0) if len(script) > 1 else script[0]
            if isinstance(item, Exception):
                raise item
            return item

    monkeypatch.setattr(dc, "HttpDaemonRpcClient", _FakeClient)


def _patch_manifest(monkeypatch, pid):
    def _fake_resolve(validate=True):
        return ("https://127.0.0.1:9", {"pid": pid} if pid is not None else {})

    monkeypatch.setattr(dc, "resolve_http_endpoint_and_manifest", _fake_resolve)


def _out_json(capsys) -> dict:
    # _print_json 用 indent=2 输出多行 JSON，整段 stdout 直接解析
    return json.loads(capsys.readouterr().out)


def test_parser_registers_stop_with_wait_default():
    args = _parser().parse_args(["stop"])
    assert args.action == "stop"
    assert args.wait == 15.0


def test_stop_idempotent_when_daemon_unreachable(monkeypatch, capsys):
    _patch_manifest(monkeypatch, None)
    _patch_health(monkeypatch, [DaemonUnavailableError("no manifest/daemon")])

    rc = run_daemon_command(["stop"])

    assert rc == 0
    payload = _out_json(capsys)
    assert payload["ok"] is True
    assert payload["status"] == "not_running"


def test_stop_sigterm_happy_path(monkeypatch, capsys):
    _patch_manifest(monkeypatch, 4321)
    # 第一次探活返回 pid，发送信号后 daemon 消失（第二次探活不可达）
    _patch_health(monkeypatch, [{"pid": 4321},
                                DaemonUnavailableError("gone")])
    sent = []
    monkeypatch.setattr(dc, "_send_daemon_stop_signal",
                        lambda pid: sent.append(pid) or "sigterm")

    rc = run_daemon_command(["stop", "--wait", "2"])

    assert rc == 0
    assert sent == [4321]
    payload = _out_json(capsys)
    assert payload["ok"] is True
    assert payload["status"] == "stopped"
    assert payload["pid"] == 4321
    assert payload["mechanism"] == "sigterm"


def test_stop_refuses_on_pid_mismatch(monkeypatch, capsys):
    _patch_manifest(monkeypatch, 111)
    _patch_health(monkeypatch, [{"pid": 999999}])
    sent = []
    monkeypatch.setattr(dc, "_send_daemon_stop_signal",
                        lambda pid: sent.append(pid) or "sigterm")

    rc = run_daemon_command(["stop"])

    assert rc == 1
    assert sent == [], "PID 不一致时绝不允许发停止信号"
    payload = _out_json(capsys)
    assert payload["error"] == "E_DAEMON_STOP_PID_MISMATCH"


def test_stop_refuses_without_trustworthy_pid(monkeypatch, capsys):
    _patch_manifest(monkeypatch, None)
    _patch_health(monkeypatch, [{"status": "ok"}])  # /health 无 pid
    sent = []
    monkeypatch.setattr(dc, "_send_daemon_stop_signal",
                        lambda pid: sent.append(pid) or "sigterm")

    rc = run_daemon_command(["stop"])

    assert rc == 1
    assert sent == []
    payload = _out_json(capsys)
    assert payload["error"] == "E_DAEMON_STOP_VERIFY_FAILED"


def test_stop_timeout_returns_1(monkeypatch, capsys):
    _patch_manifest(monkeypatch, 42)
    _patch_health(monkeypatch, [{"pid": 42}])  # 永远可达 → 超时
    monkeypatch.setattr(dc, "_send_daemon_stop_signal", lambda pid: "sigterm")

    rc = run_daemon_command(["stop", "--wait", "0"])

    assert rc == 1
    payload = _out_json(capsys)
    assert payload["error"] == "E_DAEMON_STOP_TIMEOUT"


def test_stop_signal_failure_propagates(monkeypatch, capsys):
    _patch_manifest(monkeypatch, 42)
    _patch_health(monkeypatch, [{"pid": 42}])

    def _boom(pid):
        raise OSError("no such process")

    monkeypatch.setattr(dc, "_send_daemon_stop_signal", _boom)

    rc = run_daemon_command(["stop"])

    assert rc == 1
    payload = _out_json(capsys)
    assert payload["error"] == "E_DAEMON_STOP_SIGNAL_FAILED"


def test_stop_windows_taskkill_force(monkeypatch, capsys):
    _patch_manifest(monkeypatch, 777)
    _patch_health(monkeypatch, [{"pid": 777},
                                DaemonUnavailableError("gone")])
    monkeypatch.setattr(dc.os, "name", "nt")
    killed = []
    proc = type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def _fake_run(cmd, **kwargs):
        killed.append(cmd)
        return proc

    monkeypatch.setattr("subprocess.run", _fake_run)

    rc = run_daemon_command(["stop", "--wait", "2"])

    assert rc == 0
    assert killed == [["taskkill", "/PID", "777", "/F"]]
    payload = _out_json(capsys)
    assert payload["mechanism"] == "taskkill_force"


def test_stop_windows_taskkill_failure(monkeypatch, capsys):
    _patch_manifest(monkeypatch, 777)
    _patch_health(monkeypatch, [{"pid": 777}])
    monkeypatch.setattr(dc.os, "name", "nt")
    proc = type("P", (), {"returncode": 1, "stdout": "", "stderr": "access denied"})()
    monkeypatch.setattr("subprocess.run", lambda cmd, **k: proc)

    rc = run_daemon_command(["stop"])

    assert rc == 1
    payload = _out_json(capsys)
    assert payload["error"] == "E_DAEMON_STOP_SIGNAL_FAILED"
    assert "access denied" in payload["detail"]


def test_stop_remote_error_fail_closed(monkeypatch, capsys):
    # health 校验阶段抛 DaemonRemoteError（manifest_id/PID 交叉核对失败）
    _patch_manifest(monkeypatch, 42)
    _patch_health(monkeypatch, [DaemonRemoteError("E_HTTP_MANIFEST_STALE", "bad")])
    sent = []
    monkeypatch.setattr(dc, "_send_daemon_stop_signal",
                        lambda pid: sent.append(pid) or "sigterm")

    rc = run_daemon_command(["stop"])

    assert rc == 1
    assert sent == []
    payload = _out_json(capsys)
    assert payload["error"] == "E_DAEMON_STOP_VERIFY_FAILED"
