"""共享任务写入路由的 fail-closed 契约测试。"""

import pytest
import subprocess
import sys
from pathlib import Path

from callwarden.server.daemon_client import (
    SharedTaskWriterRequiredError,
    route_task_write,
)


def test_local_task_write_requires_shared_daemon(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_MODE", "local")
    monkeypatch.delenv("CW_TASK_WRITE_POLICY", raising=False)
    called = []

    with pytest.raises(SharedTaskWriterRequiredError) as exc_info:
        route_task_write("task.report", {"task_id": "T-1"}, lambda: called.append(True))

    assert exc_info.value.code == "E_SHARED_TASK_WRITER_REQUIRED"
    assert called == []


def test_local_task_write_can_be_explicitly_isolated(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_MODE", "local")
    monkeypatch.setenv("CW_TASK_WRITE_POLICY", "isolated")

    assert route_task_write("task.report", {"task_id": "T-1"}, lambda: "local-test") == "local-test"


def test_non_task_local_write_is_not_blocked(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_MODE", "local")
    monkeypatch.delenv("CW_TASK_WRITE_POLICY", raising=False)

    assert route_task_write("rule.sync", {}, lambda: "non-task-local") == "non-task-local"


def test_task_write_uses_daemon_autostart(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_MODE", "auto")
    calls = []

    class FakeRpc:
        def call_with_autostart(self, method, params):
            calls.append((method, params))
            return {"task_id": "T-1"}

    monkeypatch.setattr(
        "callwarden.server.daemon_client.UnixDaemonRpcClient",
        lambda: FakeRpc(),
    )
    result = route_task_write("task.create", {"title": "x"}, lambda: "local")
    assert result["task_id"] == "T-1"
    assert calls[0][0] == "task.create"


def test_cli_shared_task_write_has_nonzero_exit_code():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, "cw.py", "task", "create", "--title", "shared-policy-smoke"],
        cwd=root,
        env={
            **__import__("os").environ,
            "CW_DAEMON_MODE": "local",
            "CW_TASK_WRITE_POLICY": "shared",
            "CW_USE_RUST_STORAGE": "0",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert result.returncode == 2
    assert "E_SHARED_TASK_WRITER_REQUIRED" in result.stdout + result.stderr
