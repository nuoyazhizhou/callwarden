"""SRV-014：replicator authority 通过 Rust daemon HTTP RPC。

覆盖 rollback 查询与 refresh 参数适配，并固化 Python target symbols 不再持有
SQLite 业务 authority。
"""

import ast
from pathlib import Path

import pytest

from callwarden.server import replicator


ROOT = Path(__file__).resolve().parent.parent
CAS_METHOD = "mcp.replicator.is_rust_cas_write_rolled_back"
QUERY_METHOD = "mcp.replicator.is_rust_replicator_query_rolled_back"
REFRESH_METHOD = "mcp.replicator.daemon_handle_refresh"


class FakeReplicatorDaemon:
    def __init__(self):
        self.available = True
        self.values = {CAS_METHOD: False, QUERY_METHOD: False}
        self.calls = []
        self.refresh_result = {"status": "committed", "source": "rust"}

    def __call__(self, method, params):
        if not self.available:
            raise RuntimeError("daemon unavailable")
        self.calls.append((method, params))
        if method == REFRESH_METHOD:
            return self.refresh_result
        if method in self.values:
            return {"rolled_back": self.values[method], "source": "rust"}
        raise RuntimeError(f"unexpected method: {method}")


@pytest.fixture()
def fake_daemon(monkeypatch):
    daemon = FakeReplicatorDaemon()
    monkeypatch.setattr(replicator, "_call_daemon_rpc", daemon)
    replicator._CAS_WRITE_ROLLBACK_CACHE.update(ts=0.0, value=False)
    replicator._REPLICATOR_ROLLBACK_CACHE.update(ts=0.0, value=False)
    yield daemon
    replicator._CAS_WRITE_ROLLBACK_CACHE.update(ts=0.0, value=False)
    replicator._REPLICATOR_ROLLBACK_CACHE.update(ts=0.0, value=False)


def test_success_unset_reads_both_daemon_authorities(fake_daemon):
    assert replicator._is_rust_cas_write_rolled_back() is False
    assert replicator._is_rust_replicator_query_rolled_back() is False
    assert fake_daemon.calls == [(CAS_METHOD, {}), (QUERY_METHOD, {})]


def test_success_set_reads_both_daemon_authorities(fake_daemon):
    fake_daemon.values[CAS_METHOD] = True
    fake_daemon.values[QUERY_METHOD] = True
    assert replicator._is_rust_cas_write_rolled_back() is True
    assert replicator._is_rust_replicator_query_rolled_back() is True


def test_invalid_malformed_results_are_fail_soft(monkeypatch):
    monkeypatch.setattr(replicator, "_call_daemon_rpc", lambda _method, _params: [])
    replicator._CAS_WRITE_ROLLBACK_CACHE.update(ts=0.0, value=False)
    replicator._REPLICATOR_ROLLBACK_CACHE.update(ts=0.0, value=False)
    assert replicator._is_rust_cas_write_rolled_back() is False
    assert replicator._is_rust_replicator_query_rolled_back() is False


def test_authority_uses_exact_rpc_and_no_local_params(fake_daemon):
    replicator._is_rust_cas_write_rolled_back()
    replicator._is_rust_replicator_query_rolled_back()
    assert fake_daemon.calls == [(CAS_METHOD, {}), (QUERY_METHOD, {})]


def test_unavailable_is_fail_soft_for_both_authorities(fake_daemon):
    fake_daemon.available = False
    assert replicator._is_rust_cas_write_rolled_back() is False
    assert replicator._is_rust_replicator_query_rolled_back() is False
    assert fake_daemon.calls == []


def test_restart_rechecks_after_cache_expiry(fake_daemon):
    fake_daemon.available = False
    assert replicator._is_rust_cas_write_rolled_back() is False
    assert replicator._is_rust_replicator_query_rolled_back() is False
    fake_daemon.available = True
    fake_daemon.values[CAS_METHOD] = True
    fake_daemon.values[QUERY_METHOD] = True
    replicator._CAS_WRITE_ROLLBACK_CACHE["ts"] = 0.0
    replicator._REPLICATOR_ROLLBACK_CACHE["ts"] = 0.0
    assert replicator._is_rust_cas_write_rolled_back() is True
    assert replicator._is_rust_replicator_query_rolled_back() is True


def test_cache_avoids_hot_path_rpc(fake_daemon):
    assert replicator._is_rust_cas_write_rolled_back() is False
    assert replicator._is_rust_cas_write_rolled_back() is False
    assert replicator._is_rust_replicator_query_rolled_back() is False
    assert replicator._is_rust_replicator_query_rolled_back() is False
    assert len(fake_daemon.calls) == 2


def test_refresh_is_thin_rpc_adapter_and_serializes_bytes(fake_daemon):
    result = replicator.daemon_handle_refresh(
        peer_uid=7,
        workspace_id=11,
        msg={"rel_path": "src/main.rs", "monotonic_seq": 3},
        ws_conn=object(),
        cas_conn=object(),
        canonical_bytes=b"hello\x00rust",
        workspace_root="ignored",
        codegraph_db_path="ignored",
        workspace_root_path="ignored",
        ws_db_path="ignored",
        cas_db_path="ignored",
    )
    assert result == fake_daemon.refresh_result
    assert fake_daemon.calls == [
        (
            REFRESH_METHOD,
            {
                "rel_path": "src/main.rs",
                "monotonic_seq": 3,
                "canonical_bytes_hex": "68656c6c6f0072757374",
            },
        )
    ]


def test_dispatch_branches_are_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8"
    )
    assert f'"{CAS_METHOD}"' in src
    assert f'"{QUERY_METHOD}"' in src
    assert f'"{REFRESH_METHOD}"' in src
    assert "replicator_handlers::handle_daemon_handle_refresh" in src


def test_http_capabilities_are_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8"
    )
    for method in (CAS_METHOD, QUERY_METHOD, REFRESH_METHOD):
        assert f'"{method}"' in src
    assert "T-1787323461464-f351e600#SRV-014" in src


def test_rust_handler_semantics_are_declared():
    src = (
        ROOT / "rust_ext" / "src" / "daemon" / "replicator_handlers.rs"
    ).read_text(encoding="utf-8")
    assert "RUST_CAS_WRITE_FEATURE" in src
    assert "RUST_REPLICATOR_QUERY_FEATURE" in src
    assert "ORDER BY updated_at DESC LIMIT 1" in src
    assert "handle_workspace_file_refresh" in src


def _function_node(path, name):
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    return source, next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == name
    )


@pytest.mark.parametrize(
    "name",
    [
        "_is_rust_cas_write_rolled_back",
        "_is_rust_replicator_query_rolled_back",
        "daemon_handle_refresh",
    ],
)
def test_target_functions_have_no_python_sqlite_authority(name):
    path = ROOT / "server" / "replicator.py"
    source, target = _function_node(path, name)
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
    assert "DB_PATH" not in executable
    assert source
