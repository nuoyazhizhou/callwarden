"""Phase 8.6: daemon schema 迁移管理器。

为 daemon 管理的多个 SQLite 数据库（registry.db / audit.db / cas.db /
toolchain.db）提供版本化迁移能力，与主 callwarden.db 的 schema_version
机制保持一致：

- 使用 ``schema_version`` 表跟踪每个 DB 的当前版本
- 支持增量迁移（v1 → v2 → v3 …），每个迁移在单独事务中执行
- 幂等：已应用的迁移不会重复执行
- 可注册自定义迁移函数，便于后续 phase 扩展
- 提供 ``validate_schema`` 做表/索引存在性校验，便于启动期自检

设计原则（与 AGENTS.md 一致）：
- 读不锁，写才锁。``get_current_version`` / ``get_migration_history``
  只读不写；``apply_migrations`` 是写操作。
- 迁移函数签名统一为 ``Callable[[sqlite3.Connection], None]``，由
  ``SchemaMigrator`` 在外层事务中调用，迁移函数内部不应再 ``commit``。
- 单一真相源：迁移注册表由 ``db/daemon_migrations.py`` 集中维护，
  ``SchemaMigrator`` 只负责执行，不内置迁移逻辑。
"""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Any

from .daemon_config import DaemonConfig


# 迁移函数签名：接收一个 connection，执行 DDL/DML，不 commit
MigrationFunc = Callable[[sqlite3.Connection], None]


@dataclass
class MigrationSpec:
    """单个迁移规范。

    Attributes:
        version: 目标版本号（递增，从 1 开始）
        description: 人类可读描述（写入 schema_version.description）
        up: 升级函数，在事务中执行
        down: 可选回滚函数（仅记录，不一定能真正回滚 DDL）
    """
    version: int
    description: str
    up: MigrationFunc
    down: Optional[MigrationFunc] = None


@dataclass
class MigrationResult:
    """单次 apply_migrations 的结果。"""
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


class SchemaMigrator:
    """Daemon schema 迁移管理器。

    每个 ``SchemaMigrator`` 实例绑定一个 SQLite 数据库文件，管理其
    schema 版本。典型用法::

        migrator = SchemaMigrator("/var/lib/callwarden/registry.db")
        migrator.register_migration(1, "初始 schema", _up_v1)
        migrator.register_migration(2, "新增 backup_history", _up_v2)
        result = migrator.apply_migrations()
        assert result.status in ("migrated", "up_to_date")

    迁移版本约定：
    - version=0 表示全新数据库，未应用任何迁移
    - 首个迁移（version=1）通常负责创建初始 schema
    - 后续迁移增量修改 schema

    事务模型：
    - ``apply_migrations`` 在外层开启事务
    - 每个迁移函数执行后，插入 ``schema_version`` 记录
    - 全部成功后统一 commit；任一失败则 rollback
    - 迁移函数内部不应再调用 ``conn.commit()`` / ``conn.rollback()``
    """

    SCHEMA_VERSION_DDL = """
    CREATE TABLE IF NOT EXISTS schema_version (
        version INTEGER PRIMARY KEY,
        applied_at REAL NOT NULL,
        description TEXT DEFAULT ''
    );
    """

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._migrations: Dict[int, MigrationSpec] = {}

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register_migration(self, version: int, description: str,
                           up: MigrationFunc,
                           down: Optional[MigrationFunc] = None) -> None:
        """注册一个迁移。

        Args:
            version: 目标版本号（必须 > 0 且未注册过）
            description: 迁移描述
            up: 升级函数
            down: 可选回滚函数

        Raises:
            ValueError: version <= 0 或已注册
        """
        if version <= 0:
            raise ValueError(f"version must be > 0, got {version}")
        if version in self._migrations:
            raise ValueError(f"migration v{version} already registered")
        self._migrations[version] = MigrationSpec(
            version=version, description=description, up=up, down=down
        )

    def register_migrations(self, specs: List[MigrationSpec]) -> None:
        """批量注册迁移。"""
        for spec in specs:
            self.register_migration(spec.version, spec.description, spec.up, spec.down)

    @property
    def target_version(self) -> int:
        """已注册的最高迁移版本号，未注册返回 0。"""
        return max(self._migrations.keys()) if self._migrations else 0

    # ------------------------------------------------------------------
    # 只读查询（不触发写）
    # ------------------------------------------------------------------

    def get_current_version(self, conn: Optional[sqlite3.Connection] = None) -> int:
        """获取当前 DB 的 schema 版本。

        只读操作。若 DB 文件不存在或 schema_version 表不存在，返回 0。
        注意：sqlite3.connect 会创建空文件，因此无法用文件存在性区分
        "全新数据库"和"已存在但无 schema_version 表"——两者都返回 0。
        """
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
        try:
            try:
                cur = conn.execute("SELECT MAX(version) AS v FROM schema_version")
                row = cur.fetchone()
                if row is None:
                    return 0
                # 兼容 Row 和 tuple 两种返回（取决于调用方是否设了 row_factory）
                if isinstance(row, sqlite3.Row):
                    v = row["v"]
                else:
                    v = row[0]
                return v if v is not None else 0
            except sqlite3.OperationalError:
                # schema_version 表不存在 → 全新数据库
                return 0
        finally:
            if own_conn:
                conn.close()

    def get_migration_history(self,
                              conn: Optional[sqlite3.Connection] = None
                              ) -> List[Dict[str, Any]]:
        """获取已应用的迁移历史（按版本升序）。"""
        own_conn = conn is None
        if own_conn:
            conn = sqlite3.connect(self.db_path)
            conn.row_factory = sqlite3.Row
        try:
            try:
                rows = conn.execute(
                    "SELECT version, applied_at, description "
                    "FROM schema_version ORDER BY version ASC"
                ).fetchall()
                return [dict(r) for r in rows]
            except sqlite3.OperationalError:
                return []
        finally:
            if own_conn:
                conn.close()

    def get_pending_versions(self,
                             conn: Optional[sqlite3.Connection] = None
                             ) -> List[int]:
        """获取待应用的迁移版本号列表（升序）。"""
        current = self.get_current_version(conn)
        return sorted(v for v in self._migrations if v > current)

    # ------------------------------------------------------------------
    # 写操作：应用迁移
    # ------------------------------------------------------------------

    def apply_migrations(self) -> MigrationResult:
        """应用所有待应用的迁移。

        流程：
        1. 连接 DB，创建 schema_version 表（IF NOT EXISTS，幂等）
        2. 读取当前版本
        3. 按版本升序应用待应用的迁移
        4. 每个迁移在事务中执行，失败则整体回滚到失败前状态
        5. 返回 MigrationResult

        Returns:
            MigrationResult
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            # 幂等创建 schema_version 表
            conn.executescript(self.SCHEMA_VERSION_DDL)
            conn.commit()

            current = self.get_current_version(conn)
            result = MigrationResult(
                db_path=self.db_path, from_version=current, to_version=current
            )

            pending = sorted(v for v in self._migrations if v > current)
            if not pending:
                # 已是最新
                result.to_version = self.target_version if self._migrations else current
                return result

            for v in pending:
                spec = self._migrations[v]
                try:
                    # 外层事务包裹单个迁移 + 版本记录
                    conn.execute("BEGIN")
                    spec.up(conn)
                    conn.execute(
                        "INSERT INTO schema_version (version, applied_at, description) "
                        "VALUES (?, ?, ?)",
                        (v, time.time(), spec.description),
                    )
                    conn.execute("COMMIT")
                    result.applied.append(v)
                    result.to_version = v
                except Exception as e:
                    conn.execute("ROLLBACK")
                    result.failed = v
                    result.error = f"{type(e).__name__}: {e}"
                    # to_version 保持失败前的版本
                    return result

            return result
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # 校验（只读）
    # ------------------------------------------------------------------

    def validate_schema(self,
                        expected_tables: List[str],
                        expected_indexes: Optional[List[str]] = None
                        ) -> Dict[str, Any]:
        """校验 DB schema 是否符合预期（只读）。

        用于 daemon 启动期自检：若关键表/索引缺失，应拒绝启动或触发恢复。

        Args:
            expected_tables: 期望存在的表名列表
            expected_indexes: 期望存在的索引名列表（可选）

        Returns:
            dict: {
                "valid": bool,
                "missing_tables": [...],
                "missing_indexes": [...],
                "current_version": int,
            }
        """
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            existing_tables = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            existing_indexes = {
                r["name"] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                ).fetchall()
            }
            missing_tables = [t for t in expected_tables if t not in existing_tables]
            missing_indexes = []
            if expected_indexes:
                missing_indexes = [i for i in expected_indexes if i not in existing_indexes]
            current_version = self.get_current_version(conn)
            return {
                "valid": not missing_tables and not missing_indexes,
                "missing_tables": missing_tables,
                "missing_indexes": missing_indexes,
                "current_version": current_version,
            }
        finally:
            conn.close()


# ======================================================================
# daemon 各 DB 的迁移注册表
# ======================================================================


def _registry_v1(conn: sqlite3.Connection) -> None:
    """registry.db v1: 初始 schema。

    包含 daemon_workspaces / container_mount_mappings / daemon_state 三张表
    及其索引。与 db_daemon.WORKSPACE_REGISTRY_DDL 保持一致。
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS daemon_workspaces (
            workspace_id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_instance_id TEXT NOT NULL UNIQUE,
            snapshot_id TEXT,
            owner_uid INTEGER NOT NULL,
            git_remote_url TEXT DEFAULT '',
            git_head_commit_sha TEXT DEFAULT '',
            client_view_root TEXT NOT NULL,
            host_real_root TEXT NOT NULL,
            toolchain_fingerprint TEXT DEFAULT '',
            registered_at REAL NOT NULL,
            last_active_at REAL NOT NULL,
            status TEXT DEFAULT 'active'
        );

        CREATE TABLE IF NOT EXISTS container_mount_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            container_id TEXT NOT NULL,
            container_path TEXT NOT NULL,
            host_path TEXT NOT NULL,
            mapping_type TEXT DEFAULT 'bind',
            UNIQUE(container_id, container_path)
        );

        CREATE TABLE IF NOT EXISTS daemon_state (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_workspaces_owner
            ON daemon_workspaces(owner_uid);
        CREATE INDEX IF NOT EXISTS idx_workspaces_snapshot
            ON daemon_workspaces(snapshot_id);
        CREATE INDEX IF NOT EXISTS idx_workspaces_status
            ON daemon_workspaces(status);
    """)


def _registry_v2(conn: sqlite3.Connection) -> None:
    """registry.db v2: 新增 backup_history 表，记录备份历史。

    与 server/backup_restore.py 配合，daemon 每次 backup 后写入记录，
    便于查询历史备份和清理策略。
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS backup_history (
            backup_id TEXT PRIMARY KEY,
            backup_type TEXT NOT NULL,
            created_at REAL NOT NULL,
            file_count INTEGER DEFAULT 0,
            total_size_bytes INTEGER DEFAULT 0,
            checksum TEXT DEFAULT '',
            deleted_at REAL DEFAULT 0
        );

        CREATE INDEX IF NOT EXISTS idx_backup_history_created
            ON backup_history(created_at);
        CREATE INDEX IF NOT EXISTS idx_backup_history_type
            ON backup_history(backup_type);
    """)


def _registry_v3(conn: sqlite3.Connection) -> None:
    """registry.db v3: 新增 schema_migrations_log 表。

    记录每次迁移执行的详细日志（独立于 schema_version 表，后者只记最终版本）。
    用于审计和故障排查。
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS schema_migrations_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            db_name TEXT NOT NULL,
            from_version INTEGER NOT NULL,
            to_version INTEGER NOT NULL,
            applied_at REAL NOT NULL,
            duration_ms INTEGER DEFAULT 0,
            status TEXT DEFAULT 'success',
            error TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_migrations_log_db
            ON schema_migrations_log(db_name);
        CREATE INDEX IF NOT EXISTS idx_migrations_log_applied
            ON schema_migrations_log(applied_at);
    """)


def get_registry_migrations() -> List[MigrationSpec]:
    """返回 registry.db 的迁移规范列表。"""
    return [
        MigrationSpec(1, "初始 schema: daemon_workspaces + container_mount_mappings + daemon_state", _registry_v1),
        MigrationSpec(2, "新增 backup_history 表", _registry_v2),
        MigrationSpec(3, "新增 schema_migrations_log 表", _registry_v3),
    ]


def _audit_v1(conn: sqlite3.Connection) -> None:
    """audit.db v1: 初始 audit_log 表。

    与 server/audit_log.py 的 AuditLogger 建表逻辑保持一致。
    """
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS audit_log (
            event_id TEXT PRIMARY KEY,
            timestamp REAL NOT NULL,
            event_type TEXT NOT NULL,
            actor_uid INTEGER NOT NULL,
            actor_role TEXT DEFAULT '',
            action TEXT NOT NULL,
            target TEXT DEFAULT '',
            result TEXT DEFAULT '',
            details TEXT DEFAULT '{}',
            client_ip TEXT DEFAULT ''
        );
    """)


def _audit_v2(conn: sqlite3.Connection) -> None:
    """audit.db v2: 新增索引，加速按时间/类型查询。"""
    conn.executescript("""
        CREATE INDEX IF NOT EXISTS idx_audit_log_timestamp
            ON audit_log(timestamp);
        CREATE INDEX IF NOT EXISTS idx_audit_log_event_type
            ON audit_log(event_type);
        CREATE INDEX IF NOT EXISTS idx_audit_log_actor_uid
            ON audit_log(actor_uid);
        CREATE INDEX IF NOT EXISTS idx_audit_log_result
            ON audit_log(result);
    """)


def get_audit_migrations() -> List[MigrationSpec]:
    """返回 audit.db 的迁移规范列表。"""
    return [
        MigrationSpec(1, "初始 schema: audit_log", _audit_v1),
        MigrationSpec(2, "新增 timestamp/event_type/actor_uid/result 索引", _audit_v2),
    ]


# ======================================================================
# 统一入口：迁移 daemon 所有 DB
# ======================================================================


def migrate_daemon_dbs(cfg: DaemonConfig,
                       extra_migrators: Optional[List[SchemaMigrator]] = None
                       ) -> Dict[str, MigrationResult]:
    """迁移 daemon 管理的所有 SQLite 数据库。

    按 registry → audit → cas → toolchain 的顺序迁移（registry 优先，
    因为其他 DB 的迁移可能依赖 registry 中的 workspace 信息）。

    Args:
        cfg: DaemonConfig 实例
        extra_migrators: 额外的 migrator 列表（如 cas/toolchain 自定义迁移）

    Returns:
        dict: {db_name: MigrationResult}
    """
    results: Dict[str, MigrationResult] = {}

    # 1. registry.db
    registry_migrator = SchemaMigrator(cfg.registry_db_path)
    registry_migrator.register_migrations(get_registry_migrations())
    results["registry"] = registry_migrator.apply_migrations()

    # 2. audit.db（若配置了且目录可访问）
    audit_path = cfg.audit_log_path
    if audit_path and _can_open_db(audit_path):
        audit_migrator = SchemaMigrator(audit_path)
        audit_migrator.register_migrations(get_audit_migrations())
        results["audit"] = audit_migrator.apply_migrations()

    # 3. cas.db / toolchain.db 等额外 migrator
    if extra_migrators:
        for i, m in enumerate(extra_migrators):
            results[f"extra_{i}"] = m.apply_migrations()

    return results


def validate_daemon_dbs(cfg: DaemonConfig) -> Dict[str, Dict[str, Any]]:
    """校验 daemon 所有 DB 的 schema 完整性（只读）。

    用于 daemon 启动期自检：若关键表缺失，应触发恢复或拒绝启动。

    Args:
        cfg: DaemonConfig 实例

    Returns:
        dict: {db_name: validation_result}
    """
    results: Dict[str, Dict[str, Any]] = {}

    # registry.db
    registry_migrator = SchemaMigrator(cfg.registry_db_path)
    registry_migrator.register_migrations(get_registry_migrations())
    results["registry"] = registry_migrator.validate_schema(
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

    # audit.db
    audit_path = cfg.audit_log_path
    if audit_path and _db_exists(audit_path):
        audit_migrator = SchemaMigrator(audit_path)
        audit_migrator.register_migrations(get_audit_migrations())
        results["audit"] = audit_migrator.validate_schema(
            expected_tables=["audit_log", "schema_version"],
            expected_indexes=[
                "idx_audit_log_timestamp",
                "idx_audit_log_event_type",
            ],
        )

    return results


def _db_exists(db_path: str) -> bool:
    """检查 DB 文件是否存在（只读，不创建）。"""
    import os
    return os.path.isfile(db_path)


def _can_open_db(db_path: str) -> bool:
    """检查 DB 是否可打开（目录存在，可写入）。

    用于 migrate_daemon_dbs 判断 audit.db 等可选 DB 是否可访问：
    若父目录不存在则跳过该 DB 的迁移。
    """
    import os
    parent = os.path.dirname(db_path)
    if not parent:
        return True
    return os.path.isdir(parent)
