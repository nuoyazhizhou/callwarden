"""SRV-016：snapshot GC 的数据库 authority 通过 Rust daemon HTTP RPC。"""

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from callwarden.server import snapshot_gc


ROOT = Path(__file__).resolve().parent.parent
METHODS = {
    "delete_backup": "mcp.snapshot_gc.delete_backup_history_record",
    "delete_audit": "mcp.snapshot_gc.delete_expired_audit_logs",
    "delete_migration": "mcp.snapshot_gc.delete_migration_log_record",
    "registered": "mcp.snapshot_gc.get_registered_snapshot_ids",
    "scan_audit": "mcp.snapshot_gc.scan_expired_audit_logs",
    "scan_backup": "mcp.snapshot_gc.scan_expired_backup_history",
    "scan_migration": "mcp.snapshot_gc.scan_expired_migrations_log",
    "scan_workspaces": "mcp.snapshot_gc.scan_orphaned_workspaces",
    "vacuum": "mcp.snapshot_gc.vacuum_databases",
}


class FakeSnapshotGCDaemon:
    def __init__(self):
        self.available = True
        self.calls = []
        self.items = {
            METHODS["scan_backup"]: [{
                "item_type": "backup_history",
                "key": "backup-1",
                "size_bytes": 12,
                "reason": "expired",
                "metadata": {"created_at": 1},
            }],
            METHODS["scan_migration"]: [{
                "item_type": "migration_log",
                "key": "7",
                "reason": "old_log",
                "metadata": {"db_name": "registry"},
            }],
            METHODS["scan_audit"]: [{
                "item_type": "audit_log",
                "key": "expired_batch",
                "reason": "expired",
                "metadata": {"count": 2, "cutoff": 10.0},
            }],
            METHODS["scan_workspaces"]: [{
                "item_type": "workspace_cache",
                "key": "workspace-1",
                "reason": "unregistered",
                "metadata": {"last_active_at": 1},
            }],
        }

    def __call__(self, method, params):
        if not self.available:
            raise RuntimeError("daemon unavailable")
        self.calls.append((method, params))
        if method == METHODS["registered"]:
            return ["snapshot-1", "snapshot-2"]
        if method in self.items:
            return self.items[method]
        if method in {
            METHODS["delete_backup"],
            METHODS["delete_audit"],
            METHODS["delete_migration"],
            METHODS["vacuum"],
        }:
            return {"deleted": 1, "source": "rust"}
        raise RuntimeError(f"unexpected method: {method}")


@pytest.fixture()
def fake_daemon(monkeypatch, tmp_path):
    daemon = FakeSnapshotGCDaemon()
    monkeypatch.setattr(snapshot_gc, "_call_daemon_rpc", daemon)
    cfg = SimpleNamespace(
        data_root=str(tmp_path),
        registry_db_path=str(tmp_path / "registry.db"),
        audit_log_path=str(tmp_path / "audit.db"),
    )
    return snapshot_gc.SnapshotGC(
        cfg,
        policy=snapshot_gc.GCPolicy(retention_count=4, max_age_seconds=99),
        enable_audit_gc=True,
    ), daemon, cfg


def test_success_routes_all_database_authority_operations(fake_daemon):
    gc, daemon, cfg = fake_daemon
    assert gc._scan_expired_backup_history()[0].key == "backup-1"
    assert gc._scan_expired_migrations_log()[0].key == "7"
    (Path(cfg.audit_log_path)).touch()
    assert gc._scan_expired_audit_logs()[0].key == "expired_batch"
    assert gc._scan_orphaned_workspaces()[0].key == "workspace-1"
    assert gc._get_registered_snapshot_ids() == {"snapshot-1", "snapshot-2"}
    gc._delete_backup_history_record("backup-1")
    gc._delete_migration_log_record("7")
    gc._delete_expired_audit_logs(10.0)
    gc._vacuum_databases()
    assert [method for method, _ in daemon.calls] == [
        METHODS["scan_backup"], METHODS["scan_migration"], METHODS["scan_audit"],
        METHODS["scan_workspaces"], METHODS["registered"], METHODS["delete_backup"],
        METHODS["delete_migration"], METHODS["delete_audit"], METHODS["vacuum"],
    ]
    assert daemon.calls[0][1] == {
        "registry_db_path": cfg.registry_db_path,
        "max_age_seconds": 99,
    }
    assert daemon.calls[1][1]["retention_count"] == 4


def test_invalid_parameters_are_rejected_without_local_database(fake_daemon):
    gc, daemon, _ = fake_daemon
    with pytest.raises(ValueError):
        gc._delete_migration_log_record("not-an-integer")
    assert daemon.calls == []


def test_invalid_rpc_shape_is_not_silently_accepted(fake_daemon):
    gc, daemon, _ = fake_daemon
    daemon.items[METHODS["scan_backup"]] = {"items": []}
    with pytest.raises(RuntimeError, match="invalid items"):
        gc._scan_expired_backup_history()


def test_unavailable_does_not_fallback_or_create_database(fake_daemon):
    gc, daemon, cfg = fake_daemon
    daemon.available = False
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        gc._scan_expired_backup_history()
    with pytest.raises(RuntimeError, match="daemon unavailable"):
        gc._delete_backup_history_record("backup-1")
    assert not Path(cfg.registry_db_path).exists()


def test_restart_retries_after_daemon_becomes_available(fake_daemon):
    gc, daemon, _ = fake_daemon
    daemon.available = False
    with pytest.raises(RuntimeError):
        gc._get_registered_snapshot_ids()
    daemon.available = True
    assert gc._get_registered_snapshot_ids() == {"snapshot-1", "snapshot-2"}


def test_dispatch_branches_are_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8"
    )
    for method in METHODS.values():
        assert f'"{method}"' in src
    assert "snapshot_gc_handlers::handle_vacuum_databases" in src


def test_http_capabilities_are_registered():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "http_server.rs").read_text(
        encoding="utf-8"
    )
    for method in METHODS.values():
        assert f'"{method}"' in src
    assert "T-1787323461623-fcc66abc#SRV-016" in src


def test_rust_handlers_cover_all_authority_operations():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "snapshot_gc_handlers.rs").read_text(
        encoding="utf-8"
    )
    for name in (
        "handle_delete_backup_history_record", "handle_delete_expired_audit_logs",
        "handle_delete_migration_log_record", "handle_get_registered_snapshot_ids",
        "handle_scan_expired_audit_logs", "handle_scan_expired_backup_history",
        "handle_scan_expired_migrations_log", "handle_scan_orphaned_workspaces",
        "handle_vacuum_databases",
    ):
        assert name in src
    assert "SQLITE_OPEN_READ_ONLY" in src
    assert '"source": "rust"' in src


def test_python_module_has_no_sqlite_authority_or_business_sql():
    path = ROOT / "server" / "snapshot_gc.py"
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
        and any(token in node.value for token in ("SELECT ", "CREATE TABLE", "INSERT INTO", "DELETE FROM"))
        for node in ast.walk(tree)
    )
    assert source.count("_call_daemon_rpc") >= 5
