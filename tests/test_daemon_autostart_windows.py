"""daemon_autostart Windows 启动链：验证权威任务库注入（P2 复审项）。

第二轮独立复审指出：共享库测试只证明 Python DB 层共享，未覆盖 daemon autostart
注入链。本测试 mock `_start_daemon_windows` 的 subprocess.Popen，验证生产启动路径
确实通过环境变量 `CW_DAEMON_TASK_DB` 注入 Python 权威任务库（config.py:DB_PATH），
使 daemon 与 `cw task` CLI 共享同一套任务状态。
"""

import os
import sys

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="daemon_autostart Windows 分支注入链仅在 Windows 验证",
)


def test_start_daemon_windows_injects_authority_task_db(monkeypatch):
    """`_start_daemon_windows` 必须注入权威任务库，且注入值 == config.py:DB_PATH。"""
    from callwarden import config as cw_config
    from callwarden.server import daemon_autostart as da

    captured = {}

    monkeypatch.setattr(da, "_find_daemon_binary", lambda: r"C:\fake\cw-daemon.exe")

    class _FakeProc:
        pass

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _FakeProc()

    monkeypatch.setattr(da.subprocess, "Popen", _fake_popen)

    ok = da._start_daemon_windows(r"\\.\pipe\callwarden-test")
    assert ok is True
    assert captured["cmd"] == [r"C:\fake\cw-daemon.exe", "--socket", r"\\.\pipe\callwarden-test"]
    assert captured["env"] is not None, "Popen 未传递 child_env"
    assert captured["env"].get("CW_DAEMON_TASK_DB") == cw_config.DB_PATH, (
        f"daemon 启动未注入权威任务库：CW_DAEMON_TASK_DB={captured['env'].get('CW_DAEMON_TASK_DB')!r}，"
        f"期望 {cw_config.DB_PATH!r}"
    )
