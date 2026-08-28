"""SRV-015：schema migrator Python authority 通过 Rust daemon RPC。"""

import ast
from pathlib import Path

import pytest

from callwarden.server import schema_migrator


ROOT = Path(__file__).resolve().parent.parent
APPLY = "mcp.schema_migrator.apply_migrations"
CURRENT = "mcp.schema_migrator.get_current_version"
HISTORY = "mcp.schema_migrator.get_migration_history"
VALIDATE = "mcp.schema_migrator.validate_schema"


class FakeSchemaDaemon:
    def __init__(self):
        self.available = True
        self.calls = []
        self.current = 3

    def __call__(self, method, params):
        if not self.available:
            raise RuntimeError("daemon unavailable")
        self.calls.append((method, params))
        if method == APPLY:
            return {
                "db_path": params["db_path"],
                "from_version": self.current,
                "to_version": self.current,
                "applied": [],
                "skipped": [],
                "failed": None,
                "error": None,
                "status": "up_to_date",
                "source": "rust",
            }
        if method == CURRENT:
            return self.current
        if method == HISTORY:
            return [{"version": 1, "applied_at": 1.0, "description": "v1"}]
        if method == VALIDATE:
            return {
                "valid": True,
                "missing_tables": [],
                "missing_indexes": [],
                "current_version": self.current,
                "source": "rust",
            }
        raise RuntimeError(f"unexpected method: {method}")


@pytest.fixture()
def fake_daemon(monkeypatch):
    daemon = FakeSchemaDaemon()
    monkeypatch.setattr(schema_migrator, "_call_daemon_rpc", daemon)
    yield daemon


def test_success_routes_all_authority_operations(fake_daemon, tmp_path):
    path = str(tmp_path / "registry.db")
    migrator = schema_migrator.SchemaMigrator(path, "registry")
    migrator.register_migrations(schema_migrator.get_registry_migrations())

    result = migrator.apply_migrations()
    assert result.status == "up_to_date"
    assert migrator.get_current_version() == 3
    assert migrator.get_migration_history()[0]["version"] == 1
    assert migrator.validate_schema(["daemon_workspaces"], ["idx_workspaces_owner"])["valid"]
    assert [method for method, _ in fake_daemon.calls] == [APPLY, CURRENT, HISTORY, VALIDATE]


def test_invalid_registration_is_rejected_before_rpc(fake_daemon, tmp_path):
    migrator = schema_migrator.SchemaMigrator(str(tmp_path / "registry.db"))
    with pytest.raises(ValueError, match="version must be > 0"):
        migrator.register_migration(0, "invalid")
    assert fake_daemon.calls == []


def test_authority_params_are_explicit_and_no_connection_object(fake_daemon, tmp_path):
    path = str(tmp_path / "audit.db")
    migrator = schema_migrator.SchemaMigrator(path, "audit")
    migrator.get_current_version(conn=object())
    assert fake_daemon.calls == [
        (CURRENT, {"db_path": path, "migration_set": "audit"})
    ]


def test_unavailable_does_not_fallback_to_local_db(fake_daemon, tmp_path):
    fake_daemon.available = False
    migrator = schema_migrator.SchemaMigrator(str(tmp_path / "registry.db"))
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        migrator.get_current_version()
    assert not list(tmp_path.iterdir())


def test_restart_retries_after_daemon_becomes_available(fake_daemon, tmp_path):
    fake_daemon.available = False
    migrator = schema_migrator.SchemaMigrator(str(tmp_path / "registry.db"))
    with pytest.raises(RuntimeError):
        migrator.get_migration_history()
    fake_daemon.available = True
    assert migrator.get_migration_history()[0]["description"] == "v1"


def test_dispatch_branches_are_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8"
    )
    for method in (APPLY, CURRENT, HISTORY, VALIDATE):
        assert f'"{method}"' in src
    assert "schema_migrator_handlers::handle_apply_migrations" in src


def test_http_capabilities_are_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8"
    )
    for method in (APPLY, CURRENT, HISTORY, VALIDATE):
        assert f'"{method}"' in src
    assert "T-1787323461541-f7e6ec24#SRV-015" in src


def test_rust_handler_has_no_python_fallback_contract():
    src = (
        ROOT / "rust_ext" / "src" / "daemon" / "schema_migrator_handlers.rs"
    ).read_text(encoding="utf-8")
    assert "handle_apply_migrations" in src
    assert "handle_get_current_version" in src
    assert "handle_get_migration_history" in src
    assert "handle_validate_schema" in src
    assert "OpenFlags::SQLITE_OPEN_READ_ONLY" in src
    assert '"source": "rust"' in src


def test_python_module_has_no_sqlite_authority_or_business_sql():
    path = ROOT / "server" / "schema_migrator.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    assert "sqlite3.connect" not in calls
    assert "_sqlite3.connect" not in calls
    assert "get_db" not in "\n".join(calls)
    assert "import sqlite3" not in source
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(token in node.value for token in ("SELECT ", "CREATE TABLE", "INSERT INTO"))
        for node in ast.walk(tree)
    )
    assert source.count("_call_daemon_rpc") >= 5
