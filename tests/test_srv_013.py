"""SRV-013：server.query_budget rollback authority 通过 Rust daemon RPC。

覆盖 success、invalid、authority、unavailable、restart，并固化 Python
query budget rollback 探测不再持有 SQLite authority。
"""

import ast
from pathlib import Path

import pytest

from callwarden.server import query_budget


ROOT = Path(__file__).resolve().parent.parent
METHOD = "mcp.query_budget.is_rust_budget_rolled_back"


class FakeBudgetDaemon:
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
    daemon = FakeBudgetDaemon()
    monkeypatch.setattr(query_budget, "_call_daemon_rpc", daemon)
    query_budget._BUDGET_ROLLBACK_CACHE.update(ts=0.0, value=False)
    yield daemon
    query_budget._BUDGET_ROLLBACK_CACHE.update(ts=0.0, value=False)


def test_success_unset_reads_daemon(fake_daemon):
    assert query_budget._is_rust_budget_rolled_back() is False
    assert fake_daemon.calls == [(METHOD, {})]


def test_success_set_reads_daemon(fake_daemon):
    fake_daemon.rolled_back = True
    assert query_budget._is_rust_budget_rolled_back() is True


def test_invalid_malformed_result_is_fail_soft(monkeypatch):
    monkeypatch.setattr(query_budget, "_call_daemon_rpc", lambda _method, _params: [])
    query_budget._BUDGET_ROLLBACK_CACHE.update(ts=0.0, value=False)
    assert query_budget._is_rust_budget_rolled_back() is False


def test_authority_uses_exact_rpc_and_no_local_params(fake_daemon):
    query_budget._is_rust_budget_rolled_back()
    assert fake_daemon.calls[0] == (METHOD, {})


def test_unavailable_is_fail_soft_false(fake_daemon):
    fake_daemon.available = False
    assert query_budget._is_rust_budget_rolled_back() is False
    assert fake_daemon.calls == []


def test_restart_rechecks_after_cache_expiry(fake_daemon):
    fake_daemon.available = False
    assert query_budget._is_rust_budget_rolled_back() is False
    fake_daemon.available = True
    fake_daemon.rolled_back = True
    query_budget._BUDGET_ROLLBACK_CACHE["ts"] = 0.0
    assert query_budget._is_rust_budget_rolled_back() is True


def test_cache_avoids_hot_path_rpc(fake_daemon):
    assert query_budget._is_rust_budget_rolled_back() is False
    assert query_budget._is_rust_budget_rolled_back() is False
    assert len(fake_daemon.calls) == 1


def test_dispatch_branch_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8"
    )
    assert f'"{METHOD}"' in src
    assert "query_budget_handlers::handle_is_rust_budget_rolled_back(params)" in src


def test_http_capability_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8"
    )
    assert f'"{METHOD}"' in src
    assert "T-1787323461404-efba3d30#SRV-013" in src


def test_rust_handler_semantics_declared():
    src = (
        ROOT / "rust_ext" / "src" / "daemon" / "query_budget_handlers.rs"
    ).read_text(encoding="utf-8")
    assert "RUST_DAEMON_ACL_PATH_BUDGET_FEATURE" in src
    assert "rust_daemon_acl_path_budget" in src
    assert "ORDER BY updated_at DESC LIMIT 1" in src
    assert '"source": "rust"' in src


def test_no_sqlite_authority_in_python_helper():
    src_path = ROOT / "server" / "query_budget.py"
    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    target = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_is_rust_budget_rolled_back"
    )
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(target)
        if isinstance(node, ast.Call)
    ]
    assert "sqlite3.connect" not in calls
    assert "_sqlite3.connect" not in calls
    assert "_call_daemon_rpc" in calls
    body = list(target.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(
        getattr(body[0].value, "value", None), str
    ):
        body = body[1:]
    executable = "\n".join(ast.unparse(node) for node in body)
    assert "SELECT" not in executable
    assert "rollback_config" not in executable


def test_python_module_has_no_sqlite_import():
    src = (ROOT / "server" / "query_budget.py").read_text(encoding="utf-8")
    assert "import sqlite3" not in src
    assert "from callwarden.config import DB_PATH" not in src
