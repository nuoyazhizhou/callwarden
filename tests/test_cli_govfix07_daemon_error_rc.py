"""GOV-FIX-07 回归：daemon 业务拒绝（DaemonRemoteError）在 _dispatch_subcommand 层必须 RC=2。

此前通用 except Exception 打 ✗ 后 return True → RC=0 假成功，波及全部子命令家族
（attest/supersede/cascade_close error 路径等）。修复后仅 daemon 权威拒绝 fail-closed
RC=2；普通本地异常维持原语义（不扩大化）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_PARENT = PROJECT_ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from callwarden.cli import main as cli_main
from callwarden.server.daemon_protocol import DaemonRemoteError


@pytest.fixture()
def task_argv(monkeypatch):
    """把 sys.argv 伪装成 `cw task ...` 以驱动 _dispatch_subcommand 的 cmd 分派。"""
    monkeypatch.setattr(sys, "argv", ["cw", "task", "show", "T-PROBE"])
    yield


def test_daemon_remote_error_exits_2(task_argv, monkeypatch):
    """daemon 结构化业务拒绝 → SystemExit(2)，禁止 RC=0 假成功。"""
    def _raise(*args, **kwargs):
        raise DaemonRemoteError(
            "E_LEGACY_BIND_TASK_NOT_FOUND", "引用的任务不存在: T-PROBE")

    monkeypatch.setattr(cli_main, "_handle_task", _raise)
    with pytest.raises(SystemExit) as excinfo:
        cli_main._dispatch_subcommand(["show", "T-PROBE"], db=None)
    assert excinfo.value.code == 2


def test_local_exception_keeps_legacy_semantics(task_argv, monkeypatch):
    """普通本地异常维持原语义（return True → RC=0），修复不扩大化。"""
    def _raise(*args, **kwargs):
        raise ValueError("本地非 daemon 异常（如锁错误友好分支以外的既有行为）")

    monkeypatch.setattr(cli_main, "_handle_task", _raise)
    # 既有语义：不抛 SystemExit，返回 True（进程 RC=0）
    assert cli_main._dispatch_subcommand(["show", "T-PROBE"], db=None) is True


def test_shared_writer_required_still_exits_2(task_argv, monkeypatch):
    """既有 SharedTaskWriterRequiredError 特例（RC=2）不被回归破坏。"""
    from callwarden.server.daemon_client import SharedTaskWriterRequiredError

    def _raise(*args, **kwargs):
        raise SharedTaskWriterRequiredError("共享任务写入要求 daemon 单写点")

    monkeypatch.setattr(cli_main, "_handle_task", _raise)
    with pytest.raises(SystemExit) as excinfo:
        cli_main._dispatch_subcommand(["show", "T-PROBE"], db=None)
    assert excinfo.value.code == 2


def test_attest_error_dict_path_exits_2(monkeypatch, capsys):
    """attest 分支 socket 传输 error dict 路径：daemon 拒绝 → SystemExit(2)。"""
    monkeypatch.setattr(
        cli_main, "route_task_write",
        lambda *a, **k: {"error": "E_LEGACY_BIND_TASK_NOT_FOUND: 引用的任务不存在"})
    argv = [
        "attest-legacy-workspace-binding", "T-LEGACY", "T-ANCHOR",
        "--workspace-id", "1", "--workspace-instance-id", "1",
        "--request-id", "req-govfix07-test",
        "--evidence-path", "docs/evidence/probe.json",
        "--evidence-hash", "deadbeef",
        "--lease-token", "tok", "--fencing-counter", "1",
        "--agent-id", "a", "--session-id", "s", "--model-id", "m",
        "--role", "adjudicator",
    ]
    with pytest.raises(SystemExit) as excinfo:
        cli_main._handle_task(argv, None)
    assert excinfo.value.code == 2
    assert "E_LEGACY_BIND_TASK_NOT_FOUND" in capsys.readouterr().out
