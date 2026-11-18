r"""§9.34 B：客户端身份证书临期自动 renew（fail-open）。

- 单测（monkeypatch subprocess.run，平台无关）：days_left 边界触发/不触发、
  env 外部证书跳过、逃生开关、防重入、子进程失败/超时 fail-open、
  renew 成功后重新 resolve（新证书生效路径）。
- 不依赖真实 daemon / TLS：`_maybe_auto_renew` 的数据源与换发子进程全部替身。
"""

import logging
import os
import subprocess

import pytest

from callwarden.server import daemon_client as dc
from callwarden.server.daemon_client import (
    CLIENT_CERT_RENEW_THRESHOLD_DAYS,
    HttpDaemonRpcClient,
)

_DEAD_ENDPOINT = "https://127.0.0.1:9"  # fail-closed 快速失败端点（不真正连接）


class _FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _fake_run(calls, *, status_rc=0, days_left=90, renew_rc=0,
              status_stdout=None, renew_fail_exc=None):
    """构造按命令分流的 subprocess.run 替身；calls 记录 (argv[1:], rc)。"""

    def _run(argv, **kwargs):
        tail = argv[1:] if isinstance(argv, list) else ()
        if "cert-status" in tail:
            out = status_stdout
            if out is None:
                out = '{"days_left": %d, "expired": %s}' % (
                    days_left, str(days_left < 0).lower())
            calls.append((list(tail), status_rc))
            return _FakeProc(returncode=status_rc, stdout=out)
        if "renew" in tail:
            calls.append((list(tail), renew_rc))
            if renew_fail_exc is not None:
                raise renew_fail_exc
            return _FakeProc(returncode=renew_rc, stdout='{"ok": true}')
        raise AssertionError(f"unexpected argv: {argv}")

    return _run


def _client(monkeypatch, binary="C:/fake/cw-daemon.exe"):
    client = HttpDaemonRpcClient(endpoint=_DEAD_ENDPOINT, verify_health=False)
    client._auto_renew_attempted = False
    monkeypatch.setattr(client, "_resolve_daemon_binary", lambda: binary)
    return client


def _clear_env(monkeypatch):
    for var in ("CW_CLIENT_CERT", "CW_CLIENT_KEY", "CW_AUTO_RENEW_DISABLED"):
        monkeypatch.delenv(var, raising=False)


# ----------------------------------------------------------------------
# 触发边界
# ----------------------------------------------------------------------

def test_days_left_above_threshold_skips_renew(monkeypatch):
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, days_left=8))
    client = _client(monkeypatch)
    client._maybe_auto_renew()
    assert [c[0] for c in calls] == [["pki", "cert-status"]], "只查状态，不 renew"


def test_days_left_at_threshold_triggers_renew_and_invalidates_ctx(monkeypatch):
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, days_left=CLIENT_CERT_RENEW_THRESHOLD_DAYS))
    client = _client(monkeypatch)
    sentinel = object()
    client._ssl_ctx = sentinel
    client._maybe_auto_renew()
    assert [c[0] for c in calls] == [
        ["pki", "cert-status"], ["pki", "renew"]], "7 天 → 触发 renew"
    assert client._ssl_ctx is None, "renew 成功后必须置空 _ssl_ctx 重新加载"


def test_days_left_expired_triggers_renew(monkeypatch):
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, days_left=-1))
    client = _client(monkeypatch)
    client._maybe_auto_renew()
    assert [c[0] for c in calls] == [["pki", "cert-status"], ["pki", "renew"]]


# ----------------------------------------------------------------------
# 跳过条件
# ----------------------------------------------------------------------

def test_env_cert_skips_auto_renew(monkeypatch):
    monkeypatch.setenv("CW_CLIENT_CERT", "C:/external/client.crt")
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, days_left=0))
    client = _client(monkeypatch)
    client._maybe_auto_renew()
    assert calls == [], "env 外部证书不参与自动续期"


def test_escape_hatch_disables_auto_renew(monkeypatch):
    _clear_env(monkeypatch)
    monkeypatch.setenv("CW_AUTO_RENEW_DISABLED", "1")
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, days_left=0))
    client = _client(monkeypatch)
    client._maybe_auto_renew()
    assert calls == [], "CW_AUTO_RENEW_DISABLED=1 为逃生开关"


def test_single_attempt_guard(monkeypatch):
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, days_left=3))
    client = _client(monkeypatch)
    client._maybe_auto_renew()
    client._maybe_auto_renew()
    assert len([c for c in calls if c[0][-1] == "cert-status"]) == 1, "同实例至多一次"


def test_missing_binary_is_silent(monkeypatch):
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, days_left=0))
    client = _client(monkeypatch, binary="")
    client._maybe_auto_renew()  # binary=None → 直接返回
    assert calls == []


# ----------------------------------------------------------------------
# fail-open：失败不抛异常、不换证书
# ----------------------------------------------------------------------

def test_fail_open_on_renew_failure(monkeypatch, caplog):
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(
        dc.subprocess, "run",
        _fake_run(calls, days_left=5, renew_rc=2, renew_fail_exc=None))
    client = _client(monkeypatch)
    sentinel = object()
    client._ssl_ctx = sentinel
    with caplog.at_level(logging.WARNING, logger="callwarden.server.daemon_client"):
        client._maybe_auto_renew()
    assert client._ssl_ctx is sentinel, "renew 失败不得置空旧上下文"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("renew" in m.getMessage() for m in warnings)


def test_fail_open_on_subprocess_timeout(monkeypatch):
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(
        dc.subprocess, "run",
        _fake_run(calls, days_left=5,
                  renew_fail_exc=subprocess.TimeoutExpired(cmd="pki", timeout=20)))
    client = _client(monkeypatch)
    client._maybe_auto_renew()  # 超时 → fail-open，不抛异常
    assert [c[0] for c in calls] == [["pki", "cert-status"], ["pki", "renew"]]


def test_status_failure_is_silent(monkeypatch):
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, status_rc=1))
    client = _client(monkeypatch)
    client._maybe_auto_renew()
    assert [c[0] for c in calls] == [["pki", "cert-status"]], "状态查询失败不 renew"


# ----------------------------------------------------------------------
# _ssl_context 集成点：renew 成功后重新 resolve
# ----------------------------------------------------------------------

def test_ssl_context_re_resolves_after_renew(monkeypatch):
    """renew 先于 resolve：_ssl_context 首次 resolve 即拿到新证书。"""
    _clear_env(monkeypatch)
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, days_left=3))
    resolve_calls = []

    def _fake_resolve():
        resolve_calls.append(1)
        # renew 已完成 → 返回"新"证书路径（模拟磁盘已被 renew 覆盖）
        if any(c[0][-1] == "renew" for c in calls):
            return ("new.crt", "new.key", "")
        return ("old.crt", "old.key", "")

    monkeypatch.setattr(dc, "resolve_client_cert_paths", _fake_resolve)
    client = _client(monkeypatch)
    with pytest.raises(dc.DaemonUnavailableError):
        # "new.crt" 不存在 → load_cert_chain 前的路径检查按预期 fail-closed；
        # 本测试只关心续期后的 resolve 拿到新证书。
        client._ssl_context()
    assert len(resolve_calls) == 1, "renew 先于 resolve，首次即取新证书"


def test_ssl_context_skips_auto_renew_when_env_cert(monkeypatch):
    """CW_CLIENT_CERT env → _ssl_context 不触发自动续期。"""
    monkeypatch.setenv("CW_CLIENT_CERT", "C:/external/client.crt")
    monkeypatch.setenv("CW_CLIENT_KEY", "C:/external/client.key")
    calls = []
    monkeypatch.setattr(dc.subprocess, "run", _fake_run(calls, days_left=0))
    monkeypatch.setattr(dc, "resolve_client_cert_paths",
                        lambda: ("C:/external/client.crt",
                                 "C:/external/client.key", ""))
    client = _client(monkeypatch)
    with pytest.raises(dc.DaemonUnavailableError):
        client._ssl_context()
    assert calls == [], "env 证书路径下 _ssl_context 不触发自动续期"
