"""SRV-017：Stage_Toggle P0 迁移通过 Rust daemon HTTP RPC。"""

import ast
import json
from pathlib import Path

import pytest

from callwarden.server import stage_toggle_migration


ROOT = Path(__file__).resolve().parent.parent
METHOD = "mcp.stage_toggle_migration.migrate_p0_toggles"


class FakeStageToggleDaemon:
    def __init__(self):
        self.available = True
        self.calls = []

    def __call__(self, method, params):
        if not self.available:
            raise RuntimeError("daemon unavailable")
        self.calls.append((method, params))
        if method != METHOD:
            raise RuntimeError(f"unexpected method: {method}")
        return {
            "migrated_count": len(params["toggles"]),
            "dry_run": params["dry_run"],
            "source": "rust",
        }


@pytest.fixture()
def fake_daemon(monkeypatch, tmp_path):
    daemon = FakeStageToggleDaemon()
    monkeypatch.setattr(stage_toggle_migration, "_call_daemon_rpc", daemon)
    return daemon, str(tmp_path / "stage_toggle.db")


def test_config_loader_preserves_all_p0_scopes(tmp_path):
    config = tmp_path / "experiment_batch_config.json"
    config.write_text(json.dumps({
        "p0_enabled": True,
        "p0_actor": "global-actor",
        "workspaces": {"ws-1": {"p0_enabled": False}},
        "tasks": {"task-1": {"p0_enabled": True, "p0_actor": "task-actor"}},
    }), encoding="utf-8")
    toggles = stage_toggle_migration.load_p0_toggles_from_config(config)
    assert [(t["scope_key"], t["enabled"]) for t in toggles] == [
        ("global", True), ("workspace:ws-1", False), ("task:task-1", True)
    ]


def test_success_routes_migration_and_dry_run_to_daemon(fake_daemon):
    daemon, db_path = fake_daemon
    toggles = [
        {"scope_key": "global", "enabled": True, "actor": "legacy", "changed_at": 1},
        {"scope_key": "workspace:ws-1", "enabled": False, "actor": "legacy", "changed_at": 2},
    ]
    assert stage_toggle_migration.migrate_p0_toggles(db_path, toggles) == 2
    assert stage_toggle_migration.migrate_p0_toggles(db_path, toggles, dry_run=True) == 2
    assert [call[0] for call in daemon.calls] == [METHOD, METHOD]
    assert daemon.calls[0][1] == {
        "db_path": db_path,
        "toggles": toggles,
        "migration_actor": "stage_toggle_migration",
        "dry_run": False,
    }
    assert daemon.calls[1][1]["dry_run"] is True
    assert not Path(db_path).exists()


def test_empty_config_does_not_call_daemon(fake_daemon):
    daemon, db_path = fake_daemon
    assert stage_toggle_migration.migrate_p0_toggles(db_path, []) == 0
    assert daemon.calls == []


def test_invalid_rpc_result_is_not_silently_accepted(fake_daemon, monkeypatch):
    daemon, db_path = fake_daemon
    monkeypatch.setattr(stage_toggle_migration, "_call_daemon_rpc", lambda _m, _p: [])
    with pytest.raises(RuntimeError, match="invalid result"):
        stage_toggle_migration.migrate_p0_toggles(
            db_path, [{"scope_key": "global", "enabled": True}]
        )


def test_unavailable_does_not_fallback_or_create_database(fake_daemon):
    daemon, db_path = fake_daemon
    daemon.available = False
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        stage_toggle_migration.migrate_p0_toggles(
            db_path, [{"scope_key": "global", "enabled": True}]
        )
    assert not Path(db_path).exists()


def test_restart_retries_after_daemon_becomes_available(fake_daemon):
    daemon, db_path = fake_daemon
    daemon.available = False
    with pytest.raises(RuntimeError):
        stage_toggle_migration.migrate_p0_toggles(
            db_path, [{"scope_key": "global", "enabled": True}]
        )
    daemon.available = True
    assert stage_toggle_migration.migrate_p0_toggles(
        db_path, [{"scope_key": "global", "enabled": True}]
    ) == 1


def test_dispatch_branches_are_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8"
    )
    assert f'"{METHOD}"' in src
    assert "stage_toggle_migration_handlers::handle_migrate_p0_toggles" in src


def test_http_capability_is_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8"
    )
    assert f'"{METHOD}"' in src
    assert "T-1787323461683-0059e5a0#SRV-017" in src


def test_rust_handler_has_migration_and_audit_semantics():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "stage_toggle_migration_handlers.rs").read_text(
        encoding="utf-8"
    )
    assert "handle_migrate_p0_toggles" in src
    assert "toggle_audit_log" in src
    assert "migrated_count" in src
    assert '"source": "rust"' in src


def test_python_module_has_no_sqlite_authority_or_business_sql():
    path = ROOT / "server" / "stage_toggle_migration.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        ast.unparse(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    ]
    assert "sqlite3.connect" not in calls
    assert "_sqlite3.connect" not in calls
    assert "import sqlite3" not in source
    assert "ensure_stage_toggle_schema" not in source
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(token in node.value for token in ("SELECT ", "CREATE TABLE", "INSERT INTO", "UPDATE "))
        for node in ast.walk(tree)
    )
    assert source.count("_call_daemon_rpc") >= 2
