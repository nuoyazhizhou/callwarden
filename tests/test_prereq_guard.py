"""C7 后续：`tests/_prereq_guard.py` 判定边界单测。

只验证「明确前置缺失 → skip」与「真实错误 → 不降级」两条边界；
不启动真实 daemon / cargo（保持快速、无环境依赖）。
"""
from types import SimpleNamespace

import pytest

from _prereq_guard import (
    native_build_skip_reason,
    prereq_unavailable_reason,
    require_cargo,
    require_daemon_binary,
    skip_if_daemon_exited_before_manifest,
    skip_if_isolated_daemon_pipe_conflict,
    skip_if_native_build_prereq_unavailable,
)
from callwarden.server.daemon_client import DaemonUnavailableError


class _Proc:
    def __init__(self, rc, stderr_text=""):
        self._rc = rc
        self.stderr = _Stderr(stderr_text)

    def poll(self):
        return self._rc


class _Stderr:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text.encode()


def _build(returncode: int, stdout: str = "", stderr: str = ""):
    return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def test_require_daemon_binary_missing_skips():
    with pytest.raises(pytest.skip.Exception):
        require_daemon_binary(None)


def test_require_daemon_binary_present_passthrough():
    assert require_daemon_binary("cw-daemon.exe") == "cw-daemon.exe"


def test_require_cargo_missing_skips(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    with pytest.raises(pytest.skip.Exception):
        require_cargo()


def test_require_cargo_present_returns_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: r"C:\cargo\cargo.exe")
    assert require_cargo() == r"C:\cargo\cargo.exe"


def test_daemon_exited_before_manifest_skips_with_returncode():
    with pytest.raises(pytest.skip.Exception) as exc:
        skip_if_daemon_exited_before_manifest(_Proc(1), "CreateNamedPipe error 5")
    assert "returncode=1" in str(exc.value)
    assert "CreateNamedPipe" in str(exc.value)


def test_daemon_alive_without_manifest_does_not_skip():
    # 进程存活但未发布 manifest：调用方必须保持 pytest.fail（真实卡死嫌疑）。
    assert skip_if_daemon_exited_before_manifest(_Proc(None), "") is None


@pytest.mark.parametrize(
    "blob",
    [
        "LINK : fatal error LNK1181: 无法打开输入文件 “sqlite3.lib”",
        "could not find native static library `sqlite3`",
        "link.exe not found",
    ],
)
def test_native_build_prereq_markers_skip(blob):
    build = _build(1, stderr=blob)
    assert native_build_skip_reason(build) is not None
    with pytest.raises(pytest.skip.Exception):
        skip_if_native_build_prereq_unavailable(build)


def test_rustc_compile_error_not_downgraded():
    build = _build(1, stderr="error[E0308]: mismatched types\n --> src/main.rs:1:1")
    assert native_build_skip_reason(build) is None
    assert skip_if_native_build_prereq_unavailable(build) is None


def test_successful_build_never_skips():
    assert native_build_skip_reason(_build(0, stderr="LNK1181")) is None


def test_prereq_reason_alias_matches_harness_semantics():
    exc = DaemonUnavailableError("boom", code="E_HTTP_DAEMON_UNAVAILABLE")
    assert prereq_unavailable_reason(exc, False) is not None


def test_prereq_reason_keeps_business_error_red():
    exc = DaemonUnavailableError("business", code="E_SHARED_TASK_WRITER_REQUIRED")
    assert prereq_unavailable_reason(exc, False) is None


@pytest.mark.parametrize(
    "stderr_text",
    [
        "[ERROR] server 启动失败: CreateNamedPipe 失败 (error 5): \\\\.\\pipe\\callwarden-S-1-5",
        "E_HTTP_DAEMON_UNAVAILABLE: [WinError 10061] 由于目标计算机积极拒绝，无法连接。",
        "E_HTTP_DAEMON_UNAVAILABLE: [WinError 10054] 远程主机强迫关闭了一个现有的连接。",
    ],
)
def test_pipe_conflict_after_manifest_skips(stderr_text):
    with pytest.raises(pytest.skip.Exception) as exc:
        skip_if_isolated_daemon_pipe_conflict(
            _Proc(1, stderr_text), settle_seconds=0
        )
    assert "returncode=1" in str(exc.value)
    assert "CreateNamedPipe" in str(exc.value) or "10061" in str(exc.value) \
        or "10054" in str(exc.value)


def test_pipe_conflict_keeps_alive_daemon_red():
    # 进程存活：不降级（可能是真实卡死/业务失败）。
    assert skip_if_isolated_daemon_pipe_conflict(
        _Proc(None, "CreateNamedPipe 失败"), settle_seconds=0
    ) is None


def test_pipe_conflict_keeps_unrelated_crash_red():
    # 已退出但报文无管道冲突标记：保持红（可能是真实崩溃）。
    assert skip_if_isolated_daemon_pipe_conflict(
        _Proc(1, "panic: assertion failed"), settle_seconds=0
    ) is None
