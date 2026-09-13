"""Phase 8.6: schema migration 测试（daemon authority / HTTP thin-client 迁移后）。

stale 依据（A 类：被测模块已是 daemon 薄客户端）：
- ``server/schema_migrator.py:1-6`` 明确写「Schema migrator 的 Python 薄客户端」：
  数据库连接、迁移版本决策、DDL/DML、历史查询与 schema 校验全部下沉 Rust cw-daemon，
  经 HTTP RPC 执行，daemon 失败不回退 Python SQLite。
- ``server/schema_migrator.py:25-29`` ``_call_daemon_rpc`` 委托
  ``server/_mcp_common.py:27-44`` 的纯 client 薄壳；失败抛 DaemonUnavailableError。
- ``server/schema_migrator.py:126-163`` ``get_current_version`` / ``get_migration_history`` /
  ``apply_migrations`` / ``validate_schema`` 全部经 RPC（method 常量见 :19-22）；
  ``:104-124`` ``register_migration`` / ``register_migrations`` / ``target_version``
  仍是本地兼容 API。

因此旧用例「tmp_path DB + ``SchemaMigrator.apply_migrations`` 后 ``sqlite3.connect``
直接断言本地建表 / DDL / 事务回滚」的期望已整体过期。现改为 **mock RPC seam**：
断言路由到正确 method + 正确 params + 回包透传 + 失败 fail-closed（不回退本地 DB）。
纯 Python 数据类（MigrationSpec / MigrationResult）与本地注册元数据的断言保留。
"""

import os
import sqlite3
from pathlib import Path

import pytest

from callwarden.server import schema_migrator
from callwarden.server.schema_migrator import (
    SchemaMigrator,
    MigrationSpec,
    MigrationResult,
    get_registry_migrations,
    get_audit_migrations,
    migrate_daemon_dbs,
    validate_daemon_dbs,
)
from callwarden.server.daemon_config import DaemonConfig


# RPC method 命名空间（server/schema_migrator.py:19-22）
APPLY = "mcp.schema_migrator.apply_migrations"
CURRENT = "mcp.schema_migrator.get_current_version"
HISTORY = "mcp.schema_migrator.get_migration_history"
VALIDATE = "mcp.schema_migrator.validate_schema"

REGISTRY_TARGET = 3
AUDIT_TARGET = 2


class FakeSchemaDaemon:
    """内存态 schema 迁移 daemon：模拟 Rust 侧版本决策与 schema 校验。"""

    def __init__(self):
        self.available = True
        self.calls = []
        self.versions = {}
        self.apply_override = None
        self.current_override = None

    @staticmethod
    def _target(migration_set):
        return AUDIT_TARGET if migration_set == "audit" else REGISTRY_TARGET

    def __call__(self, method, params):
        if not self.available:
            raise RuntimeError("daemon unavailable")
        self.calls.append((method, params))
        db_path = params["db_path"]
        migration_set = params["migration_set"]
        target = self._target(migration_set)
        current = self.versions.get(db_path, 0)

        if method == CURRENT:
            return self.current_override if self.current_override is not None else current
        if method == HISTORY:
            return [
                {"version": v, "description": f"v{v}", "applied_at": float(v)}
                for v in range(1, current + 1)
            ]
        if method == APPLY:
            if self.apply_override is not None:
                return self.apply_override
            self.versions[db_path] = target
            return {
                "db_path": db_path,
                "from_version": current,
                "to_version": target,
                "applied": list(range(current + 1, target + 1)),
                "skipped": [],
                "failed": None,
                "error": None,
            }
        if method == VALIDATE:
            ready = current >= target
            return {
                "valid": ready,
                "missing_tables": [] if ready else list(params.get("expected_tables", [])),
                "missing_indexes": [] if ready else list(params.get("expected_indexes", [])),
                "current_version": current,
                "source": "rust",
            }
        raise RuntimeError(f"unexpected method: {method}")


@pytest.fixture()
def fake_daemon(monkeypatch):
    daemon = FakeSchemaDaemon()
    monkeypatch.setattr(schema_migrator, "_call_daemon_rpc", daemon)
    return daemon


def _cfg(tmp_path, audit=True):
    data_root = str(tmp_path / "data")
    os.makedirs(data_root, exist_ok=True)
    audit_path = os.path.join(data_root, "audit.db") if audit else ""
    return DaemonConfig.load_from_dict({
        "data_root": data_root,
        "security": {"admin_uids": [0], "audit_log_path": audit_path},
    })


# ======================================================================
# SchemaMigrator 基础测试（本地兼容 API，仍有效）
# ======================================================================


class TestSchemaMigratorBasic:
    """SchemaMigrator 本地注册表 / target_version 行为（不触发 RPC）。"""

    def test_register_migration(self, fake_daemon, tmp_path):
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "init", lambda conn: None)
        assert m.target_version == 1
        assert fake_daemon.calls == []  # 注册纯本地，不应触发 RPC

    def test_register_multiple(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: None)
        m.register_migration(2, "v2", lambda c: None)
        m.register_migration(3, "v3", lambda c: None)
        assert m.target_version == 3

    def test_register_invalid_version_zero(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        with pytest.raises(ValueError):
            m.register_migration(0, "zero", lambda c: None)

    def test_register_invalid_version_negative(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        with pytest.raises(ValueError):
            m.register_migration(-1, "neg", lambda c: None)

    def test_register_duplicate_version(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1", lambda c: None)
        with pytest.raises(ValueError):
            m.register_migration(1, "dup", lambda c: None)

    def test_register_migrations_batch(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        specs = [
            MigrationSpec(1, "v1", lambda c: None),
            MigrationSpec(2, "v2", lambda c: None),
        ]
        m.register_migrations(specs)
        assert m.target_version == 2

    def test_empty_migrator_target_version(self, tmp_path):
        db_path = str(tmp_path / "test.db")
        m = SchemaMigrator(db_path)
        assert m.target_version == 0


# ======================================================================
# get_current_version：经 daemon 只读
# ======================================================================


class TestGetCurrentVersion:
    """get_current_version 经 daemon RPC（server/schema_migrator.py:126-132）。"""

    def test_fresh_db_returns_zero(self, fake_daemon, tmp_path):
        """daemon 报告 0 时返回 0，且路由到 CURRENT。"""
        db_path = str(tmp_path / "fresh.db")
        m = SchemaMigrator(db_path)
        assert m.get_current_version() == 0
        assert fake_daemon.calls[-1] == (
            CURRENT, {"db_path": db_path, "migration_set": "registry"}
        )

    def test_daemon_version_passthrough(self, fake_daemon, tmp_path):
        """daemon 返回的版本号原样透传。"""
        db_path = str(tmp_path / "registry.db")
        fake_daemon.versions[db_path] = 3
        m = SchemaMigrator(db_path)
        assert m.get_current_version() == 3

    def test_dict_result_is_unwrapped(self, fake_daemon, tmp_path):
        """daemon 返回 dict 时兼容提取 version/current_version。"""
        db_path = str(tmp_path / "registry.db")
        fake_daemon.current_override = {"current_version": 5}
        assert SchemaMigrator(db_path).get_current_version() == 5

    def test_migration_set_inferred_from_basename(self, fake_daemon, tmp_path):
        """basename 以 audit 开头时 migration_set 推断为 audit。"""
        db_path = str(tmp_path / "audit.db")
        m = SchemaMigrator(db_path)
        assert m.migration_set == "audit"
        m.get_current_version(conn=object())  # 兼容 conn 参数被有意忽略
        assert fake_daemon.calls == [
            (CURRENT, {"db_path": db_path, "migration_set": "audit"})
        ]

    def test_after_migration(self, fake_daemon, tmp_path):
        """apply 后 daemon 版本更新，get_current_version 反映新版本。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        m.apply_migrations()
        assert m.get_current_version() == REGISTRY_TARGET


# ======================================================================
# apply_migrations：经 daemon 写操作
# ======================================================================


class TestApplyMigrations:
    """apply_migrations 经 daemon RPC（server/schema_migrator.py:146-149）。"""

    def test_apply_single_migration(self, fake_daemon, tmp_path):
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        result = m.apply_migrations()

        assert fake_daemon.calls[-1] == (
            APPLY, {"db_path": db_path, "migration_set": "registry"}
        )
        assert result.status == "migrated"
        assert result.from_version == 0
        assert result.to_version == REGISTRY_TARGET
        assert result.applied == [1, 2, 3]
        assert result.failed is None
        assert result.error is None

    def test_apply_multiple_migrations_in_order(self, fake_daemon, tmp_path):
        """daemon 返回的有序 applied 列表原样透传。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        result = m.apply_migrations()
        assert result.applied == [1, 2, 3]
        assert result.to_version == REGISTRY_TARGET

    def test_apply_idempotent(self, fake_daemon, tmp_path):
        """二次 apply：daemon 报告已是最新，返回 up_to_date。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())

        r1 = m.apply_migrations()
        assert r1.applied == [1, 2, 3]

        r2 = m.apply_migrations()
        assert r2.status == "up_to_date"
        assert r2.applied == []

    def test_apply_routes_without_local_registration(self, fake_daemon, tmp_path):
        """薄客户端：apply 不读取本地注册表，结果完全由 daemon 决定。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        assert m.target_version == 0
        result = m.apply_migrations()
        assert fake_daemon.calls[-1][0] == APPLY
        assert result.to_version == REGISTRY_TARGET

    def test_migration_history_passthrough(self, fake_daemon, tmp_path):
        """get_migration_history 经 HISTORY 路由并透传 daemon 历史。"""
        db_path = str(tmp_path / "registry.db")
        fake_daemon.versions[db_path] = 2
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        history = m.get_migration_history()
        assert fake_daemon.calls[-1] == (
            HISTORY, {"db_path": db_path, "migration_set": "registry"}
        )
        assert len(history) == 2
        assert history[0]["version"] == 1
        assert history[1]["version"] == 2

    def test_get_pending_versions(self, fake_daemon, tmp_path):
        """get_pending_versions 依赖 daemon 当前版本（:142-144）。"""
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migration(1, "v1")
        m.register_migration(2, "v2")
        m.register_migration(3, "v3")

        # 全新 DB：daemon 返回 0，本地注册的 1/2/3 都待应用
        assert m.get_pending_versions() == [1, 2, 3]

        # daemon 报告当前版本 3 后，无待应用
        fake_daemon.versions[db_path] = 3
        assert m.get_pending_versions() == []
        assert [c[0] for c in fake_daemon.calls] == [CURRENT, CURRENT]


# ======================================================================
# 迁移失败契约（事务回滚已下沉 daemon）
# ======================================================================


class TestMigrationFailureContract:
    """迁移失败 / 回滚语义已下沉 daemon，Python 侧只透传并 fail-closed。"""

    def test_failed_result_passthrough(self, fake_daemon, tmp_path):
        db_path = str(tmp_path / "registry.db")
        fake_daemon.apply_override = {
            "db_path": db_path,
            "from_version": 0,
            "to_version": 0,
            "applied": [],
            "skipped": [],
            "failed": 2,
            "error": "simulated failure",
        }
        result = SchemaMigrator(db_path).apply_migrations()
        assert result.status == "failed"
        assert result.failed == 2
        assert "simulated failure" in result.error

    def test_daemon_unavailable_is_fail_closed(self, fake_daemon, tmp_path):
        """daemon 不可用时原样抛错，且不落地任何本地 DB 文件。"""
        fake_daemon.available = False
        m = SchemaMigrator(str(tmp_path / "registry.db"))
        with pytest.raises(RuntimeError, match="daemon unavailable"):
            m.apply_migrations()
        assert not list(tmp_path.glob("*.db")), list(tmp_path.glob("*.db"))

    def test_invalid_rpc_result_is_rejected(self, fake_daemon, tmp_path):
        """daemon 返回非 dict 结果时应显式报错，而非静默接受。"""
        fake_daemon.apply_override = "not-a-dict"
        with pytest.raises(RuntimeError, match="invalid result"):
            SchemaMigrator(str(tmp_path / "registry.db")).apply_migrations()


# ======================================================================
# registry.db / audit.db 迁移元数据（本地元数据 + RPC 路由）
# ======================================================================


class TestRegistryMigrations:
    """registry 迁移元数据本地保留，实际迁移经 daemon。"""

    def test_registry_metadata_versions(self):
        specs = get_registry_migrations()
        assert [s.version for s in specs] == [1, 2, 3]

    def test_registry_apply_routes(self, fake_daemon, tmp_path):
        db_path = str(tmp_path / "registry.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_registry_migrations())
        result = m.apply_migrations()
        assert m.target_version == REGISTRY_TARGET
        assert fake_daemon.calls[-1] == (
            APPLY, {"db_path": db_path, "migration_set": "registry"}
        )
        assert result.applied == [1, 2, 3]


class TestAuditMigrations:
    """audit 迁移元数据本地保留，实际迁移经 daemon。"""

    def test_audit_metadata_versions(self):
        specs = get_audit_migrations()
        assert [s.version for s in specs] == [1, 2]

    def test_audit_apply_routes(self, fake_daemon, tmp_path):
        db_path = str(tmp_path / "audit.db")
        m = SchemaMigrator(db_path)
        m.register_migrations(get_audit_migrations())
        result = m.apply_migrations()
        assert fake_daemon.calls[-1] == (
            APPLY, {"db_path": db_path, "migration_set": "audit"}
        )
        assert result.applied == [1, 2]
        assert result.to_version == AUDIT_TARGET


# ======================================================================
# migrate_daemon_dbs 统一入口
# ======================================================================


class TestMigrateDaemonDbs:
    """migrate_daemon_dbs 统一入口（server/schema_migrator.py:183-202）。"""

    def test_migrate_all_dbs(self, fake_daemon, tmp_path):
        cfg = _cfg(tmp_path)
        Path(cfg.audit_log_path).touch()  # daemon-owned audit DB 已存在
        results = migrate_daemon_dbs(cfg)

        assert set(results) == {"registry", "audit"}
        assert results["registry"].status == "migrated"
        assert results["audit"].status == "migrated"
        assert [c[0] for c in fake_daemon.calls] == [APPLY, APPLY]
        assert fake_daemon.calls[0][1]["migration_set"] == "registry"
        assert fake_daemon.calls[1][1]["migration_set"] == "audit"

    def test_migrate_idempotent(self, fake_daemon, tmp_path):
        cfg = _cfg(tmp_path)
        Path(cfg.audit_log_path).touch()
        migrate_daemon_dbs(cfg)
        results = migrate_daemon_dbs(cfg)
        assert results["registry"].status == "up_to_date"
        assert results["audit"].status == "up_to_date"

    def test_migrate_without_audit_path(self, fake_daemon, tmp_path):
        cfg = _cfg(tmp_path, audit=False)
        results = migrate_daemon_dbs(cfg)
        assert "registry" in results
        assert "audit" not in results
        assert [c[0] for c in fake_daemon.calls] == [APPLY]

    def test_migrate_with_extra_migrators(self, fake_daemon, tmp_path):
        cfg = _cfg(tmp_path, audit=False)
        extra_db = str(tmp_path / "extra.db")
        extra_m = SchemaMigrator(extra_db)
        extra_m.register_migration(1, "extra v1")

        results = migrate_daemon_dbs(cfg, extra_migrators=[extra_m])
        assert "extra_0" in results
        assert results["extra_0"].status == "migrated"
        assert fake_daemon.calls[-1][1]["db_path"] == extra_db


# ======================================================================
# validate_daemon_dbs：经 daemon 只读校验
# ======================================================================


class TestValidateDaemonDbs:
    """validate_daemon_dbs 经 daemon RPC（server/schema_migrator.py:205-233）。"""

    def test_validate_before_migration(self, fake_daemon, tmp_path):
        cfg = _cfg(tmp_path)
        results = validate_daemon_dbs(cfg)
        assert results["registry"]["valid"] is False
        assert "daemon_workspaces" in results["registry"]["missing_tables"]
        assert fake_daemon.calls[-1][0] == VALIDATE
        # 期望表清单由 Python 侧显式下发
        assert "daemon_workspaces" in fake_daemon.calls[-1][1]["expected_tables"]
        assert "idx_workspaces_owner" in fake_daemon.calls[-1][1]["expected_indexes"]

    def test_validate_after_migration(self, fake_daemon, tmp_path):
        cfg = _cfg(tmp_path)
        Path(cfg.audit_log_path).touch()
        migrate_daemon_dbs(cfg)
        results = validate_daemon_dbs(cfg)
        assert results["registry"]["valid"] is True
        assert results["registry"]["missing_tables"] == []
        assert results["audit"]["valid"] is True

    def test_validate_returns_current_version(self, fake_daemon, tmp_path):
        cfg = _cfg(tmp_path)
        Path(cfg.audit_log_path).touch()
        migrate_daemon_dbs(cfg)
        results = validate_daemon_dbs(cfg)
        assert results["registry"]["current_version"] == REGISTRY_TARGET
        assert results["audit"]["current_version"] == AUDIT_TARGET

    def test_validate_skips_nonexistent_audit(self, fake_daemon, tmp_path):
        cfg = _cfg(tmp_path)
        # audit DB 不存在 → 不校验 audit
        results = validate_daemon_dbs(cfg)
        assert "registry" in results
        assert "audit" not in results


# ======================================================================
# MigrationResult 数据类测试（纯 Python，仍有效）
# ======================================================================


class TestMigrationResult:
    """MigrationResult 数据类行为测试。"""

    def test_status_up_to_date(self):
        r = MigrationResult(db_path="x", from_version=1, to_version=1)
        assert r.status == "up_to_date"

    def test_status_migrated(self):
        r = MigrationResult(db_path="x", from_version=0, to_version=2, applied=[1, 2])
        assert r.status == "migrated"

    def test_status_failed(self):
        r = MigrationResult(db_path="x", from_version=0, to_version=0, failed=1, error="boom")
        assert r.status == "failed"

    def test_to_dict(self):
        r = MigrationResult(
            db_path="/tmp/test.db",
            from_version=0,
            to_version=2,
            applied=[1, 2],
        )
        d = r.to_dict()
        assert d["db_path"] == "/tmp/test.db"
        assert d["from_version"] == 0
        assert d["to_version"] == 2
        assert d["applied"] == [1, 2]
        assert d["status"] == "migrated"


# ======================================================================
# 边界情况测试
# ======================================================================


class TestEdgeCases:
    """边界情况：本地注册元数据 + daemon 路由 / fail-closed。"""

    def test_version_gap_registration_is_local_only(self, fake_daemon, tmp_path):
        """版本跳号仅是本地元数据；实际 applied 由 daemon 决定。"""
        m = SchemaMigrator(str(tmp_path / "test.db"))
        m.register_migration(1, "v1")
        m.register_migration(5, "v5")
        assert m.target_version == 5
        m.apply_migrations()
        assert fake_daemon.calls[-1][0] == APPLY

    def test_migration_functions_are_not_executed_in_python(self, fake_daemon, tmp_path):
        """迁移函数由 daemon 执行，Python 侧不得调用注册的 up()。"""
        called = []

        def up(conn):
            called.append(conn)

        m = SchemaMigrator(str(tmp_path / "test.db"))
        m.register_migration(1, "multi", up)
        m.apply_migrations()
        assert called == []
        assert fake_daemon.calls[-1][0] == APPLY

    def test_get_history_empty(self, fake_daemon, tmp_path):
        m = SchemaMigrator(str(tmp_path / "test.db"))
        assert m.get_migration_history() == []
        assert fake_daemon.calls[-1][0] == HISTORY

    def test_concurrent_migrators_different_dbs(self, fake_daemon, tmp_path):
        """不同 DB 的 migrator 各自携带独立 db_path 路由到 daemon。"""
        db1 = str(tmp_path / "db1.db")
        db2 = str(tmp_path / "db2.db")

        r1 = SchemaMigrator(db1).apply_migrations()
        r2 = SchemaMigrator(db2).apply_migrations()

        assert r1.to_version == REGISTRY_TARGET
        assert r2.to_version == REGISTRY_TARGET
        assert fake_daemon.calls[0][1]["db_path"] == db1
        assert fake_daemon.calls[1][1]["db_path"] == db2

    def test_migration_spec_dataclass(self):
        def up(conn):
            pass

        spec = MigrationSpec(version=1, description="test", up=up)
        assert spec.version == 1
        assert spec.description == "test"
        assert spec.up is up
        assert spec.down is None

    def test_migrator_does_not_touch_local_db(self, fake_daemon, tmp_path):
        """已有本地 DB 的数据不应被 Python 侧迁移逻辑触碰（迁移在 daemon）。"""
        db_path = str(tmp_path / "test.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE legacy (id INTEGER)")
        conn.execute("INSERT INTO legacy VALUES (1)")
        conn.commit()
        conn.close()

        m = SchemaMigrator(db_path)
        m.register_migration(1, "add table")
        result = m.apply_migrations()

        assert result.status == "migrated"
        conn = sqlite3.connect(db_path)
        row = conn.execute("SELECT * FROM legacy").fetchone()
        conn.close()
        assert row is not None
