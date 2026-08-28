"""Schema migrator 的 Python 薄客户端。

数据库连接、迁移版本决策、DDL/DML、历史查询和 schema 校验均由 Rust
cw-daemon 通过 HTTP RPC 执行。本模块只保留兼容的 Python 对象形状、参数
序列化和启动期结果格式化；任何 daemon 失败都不会回退到 Python SQLite。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .daemon_config import DaemonConfig


MigrationFunc = Callable[..., None]

_APPLY_METHOD = "mcp.schema_migrator.apply_migrations"
_CURRENT_VERSION_METHOD = "mcp.schema_migrator.get_current_version"
_HISTORY_METHOD = "mcp.schema_migrator.get_migration_history"
_VALIDATE_METHOD = "mcp.schema_migrator.validate_schema"


def _call_daemon_rpc(method: str, params: Dict[str, Any]) -> Any:
    """经统一 HTTP client 调用 daemon；不打开本地数据库。"""
    from ._mcp_common import _call_daemon_rpc as _rpc

    return _rpc(method, params)


@dataclass
class MigrationSpec:
    """兼容旧注册表形状；迁移函数本身不在 Python 执行。"""

    version: int
    description: str
    up: Optional[MigrationFunc] = None
    down: Optional[MigrationFunc] = None


@dataclass
class MigrationResult:
    """daemon apply_migrations RPC 的兼容结果。"""

    db_path: str
    from_version: int
    to_version: int
    applied: List[int] = field(default_factory=list)
    skipped: List[int] = field(default_factory=list)
    failed: Optional[int] = None
    error: Optional[str] = None

    @property
    def status(self) -> str:
        if self.failed is not None:
            return "failed"
        if self.applied:
            return "migrated"
        return "up_to_date"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "db_path": self.db_path,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "applied": list(self.applied),
            "skipped": list(self.skipped),
            "failed": self.failed,
            "error": self.error,
            "status": self.status,
        }

    @classmethod
    def from_rpc(cls, value: Any, db_path: str) -> "MigrationResult":
        if not isinstance(value, dict):
            raise RuntimeError(f"schema migrator RPC returned invalid result: {value!r}")
        return cls(
            db_path=str(value.get("db_path", db_path)),
            from_version=int(value.get("from_version", 0)),
            to_version=int(value.get("to_version", 0)),
            applied=[int(v) for v in value.get("applied", [])],
            skipped=[int(v) for v in value.get("skipped", [])],
            failed=(None if value.get("failed") is None else int(value["failed"])),
            error=(None if value.get("error") is None else str(value["error"])),
        )


class SchemaMigrator:
    """绑定一个 daemon-owned 数据库路径的 HTTP 薄客户端。"""

    def __init__(self, db_path: str, migration_set: Optional[str] = None):
        self.db_path = db_path
        self.migration_set = migration_set or self._infer_migration_set(db_path)
        self._migrations: Dict[int, MigrationSpec] = {}

    @staticmethod
    def _infer_migration_set(db_path: str) -> str:
        return "audit" if os.path.basename(db_path).lower().startswith("audit") else "registry"

    def _params(self) -> Dict[str, Any]:
        return {"db_path": self.db_path, "migration_set": self.migration_set}

    def register_migration(
        self,
        version: int,
        description: str,
        up: Optional[MigrationFunc] = None,
        down: Optional[MigrationFunc] = None,
    ) -> None:
        """保留迁移注册 API；真正的迁移由 daemon 的固定注册表执行。"""
        if version <= 0:
            raise ValueError(f"version must be > 0, got {version}")
        if version in self._migrations:
            raise ValueError(f"migration v{version} already registered")
        self._migrations[version] = MigrationSpec(version, description, up, down)

    def register_migrations(self, specs: List[MigrationSpec]) -> None:
        for spec in specs:
            self.register_migration(spec.version, spec.description, spec.up, spec.down)

    @property
    def target_version(self) -> int:
        return max(self._migrations.keys()) if self._migrations else 0

    def get_current_version(self, conn: Any = None) -> int:
        """经 daemon 只读获取版本；兼容参数 conn 被有意忽略。"""
        del conn
        result = _call_daemon_rpc(_CURRENT_VERSION_METHOD, self._params())
        if isinstance(result, dict):
            result = result.get("version", result.get("current_version", 0))
        return int(result or 0)

    def get_migration_history(self, conn: Any = None) -> List[Dict[str, Any]]:
        """经 daemon 只读获取迁移历史；兼容参数 conn 被有意忽略。"""
        del conn
        result = _call_daemon_rpc(_HISTORY_METHOD, self._params())
        if not isinstance(result, list):
            raise RuntimeError(f"schema history RPC returned invalid result: {result!r}")
        return [dict(item) for item in result if isinstance(item, dict)]

    def get_pending_versions(self, conn: Any = None) -> List[int]:
        current = self.get_current_version(conn)
        return sorted(version for version in self._migrations if version > current)

    def apply_migrations(self) -> MigrationResult:
        """经 daemon 应用固定迁移注册表；失败不打开 Python SQLite。"""
        result = _call_daemon_rpc(_APPLY_METHOD, self._params())
        return MigrationResult.from_rpc(result, self.db_path)

    def validate_schema(
        self,
        expected_tables: List[str],
        expected_indexes: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """经 daemon 只读校验 schema。"""
        params = self._params()
        params["expected_tables"] = list(expected_tables)
        params["expected_indexes"] = list(expected_indexes or [])
        result = _call_daemon_rpc(_VALIDATE_METHOD, params)
        if not isinstance(result, dict):
            raise RuntimeError(f"schema validation RPC returned invalid result: {result!r}")
        return result


def get_registry_migrations() -> List[MigrationSpec]:
    """返回 daemon 固定 registry migration 的描述元数据。"""
    return [
        MigrationSpec(1, "初始 schema: daemon_workspaces + container_mount_mappings + daemon_state"),
        MigrationSpec(2, "新增 backup_history 表"),
        MigrationSpec(3, "新增 schema_migrations_log 表"),
    ]


def get_audit_migrations() -> List[MigrationSpec]:
    """返回 daemon 固定 audit migration 的描述元数据。"""
    return [
        MigrationSpec(1, "初始 schema: audit_log"),
        MigrationSpec(2, "新增 timestamp/event_type/actor_uid/result 索引"),
    ]


def migrate_daemon_dbs(
    cfg: DaemonConfig,
    extra_migrators: Optional[List[SchemaMigrator]] = None,
) -> Dict[str, MigrationResult]:
    """按 registry → audit → extra 顺序请求 daemon 执行迁移。"""
    results: Dict[str, MigrationResult] = {}

    registry = SchemaMigrator(cfg.registry_db_path, "registry")
    registry.register_migrations(get_registry_migrations())
    results["registry"] = registry.apply_migrations()

    audit_path = cfg.audit_log_path
    if audit_path and _db_exists(audit_path):
        audit = SchemaMigrator(audit_path, "audit")
        audit.register_migrations(get_audit_migrations())
        results["audit"] = audit.apply_migrations()

    for index, migrator in enumerate(extra_migrators or []):
        results[f"extra_{index}"] = migrator.apply_migrations()
    return results


def validate_daemon_dbs(cfg: DaemonConfig) -> Dict[str, Dict[str, Any]]:
    """经 daemon 只读校验 registry/audit schema。"""
    registry = SchemaMigrator(cfg.registry_db_path, "registry")
    registry.register_migrations(get_registry_migrations())
    results: Dict[str, Dict[str, Any]] = {
        "registry": registry.validate_schema(
            expected_tables=[
                "daemon_workspaces",
                "container_mount_mappings",
                "daemon_state",
                "schema_version",
            ],
            expected_indexes=[
                "idx_workspaces_owner",
                "idx_workspaces_snapshot",
                "idx_workspaces_status",
            ],
        )
    }

    audit_path = cfg.audit_log_path
    if audit_path and _db_exists(audit_path):
        audit = SchemaMigrator(audit_path, "audit")
        audit.register_migrations(get_audit_migrations())
        results["audit"] = audit.validate_schema(
            expected_tables=["audit_log", "schema_version"],
            expected_indexes=["idx_audit_log_timestamp", "idx_audit_log_event_type"],
        )
    return results


def _db_exists(db_path: str) -> bool:
    """只检查路径，不打开或创建数据库。"""
    return os.path.isfile(db_path)


def _can_open_db(db_path: str) -> bool:
    """保留旧 API 的路径可访问性检查，不执行数据库操作。"""
    parent = os.path.dirname(db_path)
    return not parent or os.path.isdir(parent)
