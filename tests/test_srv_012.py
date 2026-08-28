"""SRV-012：server.metrics rollback authority 通过 Rust daemon HTTP RPC。

测试覆盖 success、invalid、authority、unavailable、restart，并固化
`server/metrics.py` 的回滚探测不再持有 SQLite authority。
"""

import ast
from pathlib import Path

import pytest

from callwarden.server import metrics


ROOT = Path(__file__).resolve().parent.parent
METHOD = "mcp.metrics.is_rust_metrics_rolled_back"


class FakeMetricsDaemon:
    def __init__(self, rolled_back=False):
        self.available = True
        self.rolled_back = rolled_back
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
    daemon = FakeMetricsDaemon()
    monkeypatch.setattr(metrics, "_call_daemon_rpc", daemon)
    metrics._METRICS_ROLLBACK_CACHE.update(ts=0.0, value=False)
    yield daemon
    metrics._METRICS_ROLLBACK_CACHE.update(ts=0.0, value=False)


def test_success_unset_reads_daemon(fake_daemon):
    assert metrics._is_rust_metrics_rolled_back() is False
    assert fake_daemon.calls == [(METHOD, {})]


def test_success_set_reads_daemon(fake_daemon):
    fake_daemon.rolled_back = True
    assert metrics._is_rust_metrics_rolled_back() is True


def test_invalid_malformed_result_is_fail_soft(monkeypatch):
    monkeypatch.setattr(metrics, "_call_daemon_rpc", lambda _method, _params: [])
    metrics._METRICS_ROLLBACK_CACHE.update(ts=0.0, value=False)
    assert metrics._is_rust_metrics_rolled_back() is False


def test_authority_uses_exact_rpc_and_no_local_params(fake_daemon):
    metrics._is_rust_metrics_rolled_back()
    assert fake_daemon.calls[0] == (METHOD, {})


def test_unavailable_is_fail_soft_false(fake_daemon):
    fake_daemon.available = False
    assert metrics._is_rust_metrics_rolled_back() is False
    assert fake_daemon.calls == []


def test_restart_rechecks_after_cache_expiry(fake_daemon):
    fake_daemon.available = False
    assert metrics._is_rust_metrics_rolled_back() is False
    fake_daemon.available = True
    fake_daemon.rolled_back = True
    metrics._METRICS_ROLLBACK_CACHE["ts"] = 0.0
    assert metrics._is_rust_metrics_rolled_back() is True


def test_cache_avoids_hot_path_rpc(fake_daemon):
    assert metrics._is_rust_metrics_rolled_back() is False
    assert metrics._is_rust_metrics_rolled_back() is False
    assert len(fake_daemon.calls) == 1


def test_dispatch_branch_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8"
    )
    assert f'"{METHOD}"' in src
    assert "super::metrics_handlers::handle_is_rust_metrics_rolled_back(params)" in src


def test_http_capability_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8"
    )
    assert f'"{METHOD}"' in src
    assert "T-1787323461346-ec4e03e8#SRV-012" in src


def test_rust_handler_semantics_declared():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "metrics_handlers.rs").read_text(
        encoding="utf-8"
    )
    assert "RUST_DAEMON_METRICS_FEATURE" in src
    assert "rust_daemon_metrics_compute" in src
    assert "ORDER BY updated_at DESC LIMIT 1" in src
    assert '"source": "rust"' in src


def test_no_sqlite_authority_in_python_helper():
    src_path = ROOT / "server" / "metrics.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_is_rust_metrics_rolled_back"
    )
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
    ]
    assert "sqlite3.connect" not in calls
    assert "_sqlite3.connect" not in calls
    assert "_call_daemon_rpc" in calls
    assert "SELECT" not in ast.get_source_segment(src_path.read_text(encoding="utf-8"), target)


def test_python_module_has_no_sqlite_import():
    src = (ROOT / "server" / "metrics.py").read_text(encoding="utf-8")
    assert "import sqlite3" not in src
    assert "from callwarden.config import DB_PATH" not in src
