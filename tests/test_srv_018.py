"""SRV-018：StagingLog rollback authority 通过 Rust daemon HTTP RPC。"""

import ast
from pathlib import Path

import pytest

from callwarden.server import staging_log


ROOT = Path(__file__).resolve().parent.parent
METHOD = "mcp.staging_log.is_rust_staging_log_rolled_back"


class FakeStagingLogDaemon:
    def __init__(self):
        self.available = True
        self.rolled_back = False
        self.calls = []

    def __call__(self, method, params):
        if not self.available:
            raise RuntimeError("daemon unavailable")
        self.calls.append((method, params))
        if method != METHOD:
            raise RuntimeError(f"unexpected method: {method}")
        return {"rolled_back": self.rolled_back, "source": "rust"}


@pytest.fixture()
def fake_daemon(monkeypatch):
    daemon = FakeStagingLogDaemon()
    monkeypatch.setattr(staging_log, "_call_daemon_rpc", daemon)
    staging_log._ROLLBACK_CACHE.update(ts=0.0, value=False)
    yield daemon
    staging_log._ROLLBACK_CACHE.update(ts=0.0, value=False)


def test_success_reads_daemon_rollback_authority(fake_daemon):
    fake_daemon.rolled_back = True
    assert staging_log._is_rust_staging_log_rolled_back() is True
    assert fake_daemon.calls == [(METHOD, {})]


def test_cache_avoids_repeated_authority_calls(fake_daemon):
    assert staging_log._is_rust_staging_log_rolled_back() is False
    assert staging_log._is_rust_staging_log_rolled_back() is False
    assert len(fake_daemon.calls) == 1


def test_invalid_rpc_result_is_fail_soft(monkeypatch):
    monkeypatch.setattr(staging_log, "_call_daemon_rpc", lambda _m, _p: [])
    staging_log._ROLLBACK_CACHE.update(ts=0.0, value=False)
    assert staging_log._is_rust_staging_log_rolled_back() is False


def test_unavailable_daemon_is_fail_soft_without_local_fallback(fake_daemon):
    fake_daemon.available = False
    assert staging_log._is_rust_staging_log_rolled_back() is False
    assert fake_daemon.calls == []


def test_restart_rechecks_after_cache_expiry(fake_daemon):
    fake_daemon.available = False
    assert staging_log._is_rust_staging_log_rolled_back() is False
    fake_daemon.available = True
    fake_daemon.rolled_back = True
    staging_log._ROLLBACK_CACHE["ts"] = 0.0
    assert staging_log._is_rust_staging_log_rolled_back() is True


def test_dispatch_branch_is_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8"
    )
    assert f'"{METHOD}"' in src
    assert "staging_log_handlers::handle_is_rust_staging_log_rolled_back" in src


def test_http_capability_is_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8"
    )
    assert f'"{METHOD}"' in src
    assert "T-1787323461742-03e6a000#SRV-018" in src


def test_rust_handler_has_stable_fail_soft_semantics():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "staging_log_handlers.rs").read_text(
        encoding="utf-8"
    )
    assert "handle_is_rust_staging_log_rolled_back" in src
    assert "rust_staging_log" in src
    assert "db_open_failed" in src
    assert '"source": "rust"' in src


def test_python_target_has_no_sqlite_authority_or_rollback_sql():
    path = ROOT / "server" / "staging_log.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    target = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_is_rust_staging_log_rolled_back"
    )
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
    ]
    assert "sqlite3.connect" not in calls
    assert "_sqlite3.connect" not in calls
    assert "_call_daemon_rpc" in calls
    assert "import sqlite3" not in source
    body = list(target.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(
        getattr(body[0].value, "value", None), str
    ):
        body = body[1:]
    executable = "\n".join(ast.unparse(node) for node in body)
    assert "rollback_config" not in executable
    assert "SELECT " not in executable
