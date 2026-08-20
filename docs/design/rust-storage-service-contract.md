# Rust StorageService 完整迁移契约

## 1. 当前缺口

`db/db_base.py` 仍负责 registry SQLite 的连接初始化、schema 建表、版本迁移和事务；
Rust 当前只提供 `sqlite_query_schema_version` 和若干局部写入 API。不能把“Rust 能读取
schema_version”宣称为 StorageService 完成。

## 2. 目标边界

Rust 成为 registry SQLite 的唯一 schema/migration/transaction 真相：

- Python `CodeGraphDB` 只通过 facade 请求连接、迁移、事务和健康状态；
- Rust 统一设置 WAL、busy_timeout、foreign_keys、synchronous 和 checkpoint 语义；
- migration 必须幂等、单事务、可恢复，并拒绝未来版本数据库；
- 迁移失败不得留下半完成版本；失败时保留原库和可验证备份；
- Python fallback 只保留一个版本周期，并由 rollback feature 显式控制。

## 3. Rust API 契约

实现 `rust_ext/src/storage.rs`，通过 PyO3 和 daemon service 暴露：

```text
storage_open(path, mode) -> StorageHandle
storage_schema_version(path) -> u32
storage_initialize_or_migrate(path, expected_version) -> MigrationReport
storage_begin(path, kind) -> TransactionHandle
storage_commit(tx) -> CommitReport
storage_rollback(tx) -> void
storage_checkpoint(path, mode) -> CheckpointReport
storage_integrity_check(path) -> IntegrityReport
storage_backup_before_migration(path, destination) -> BackupReport
```

Rust API 必须返回稳定错误码：`DB_OPEN_FAILED`、`DB_LOCKED`、`SCHEMA_TOO_NEW`、
`MIGRATION_FAILED`、`INTEGRITY_FAILED`、`CHECKPOINT_FAILED`、`TX_ABORTED`。

## 4. Schema 真相与版本迁移

1. 将现有 `SCHEMA_VERSION=42` 的 schema 和 v1→v42 migration 转成 Rust 可审计的
   versioned migration manifest；禁止运行时 import Python `db.schema`。
2. 每个 migration 记录 `version`、`checksum`、`description` 和 applied timestamp；
   相同版本重复执行必须 no-op，checksum 变化必须拒绝启动。
3. 目标版本高于二进制版本时 fail closed；目标版本低于二进制版本时只允许显式
   `storage migrate`，不在普通查询/refresh 中隐式降级。
4. 迁移前执行 integrity check 和 backup；迁移过程使用 `BEGIN IMMEDIATE`，每个版本
   原子提交，失败回滚到上一个版本。

## 5. 生产接线与回滚

- `db_base.py` 的初始化、`_migrate_schema`、`_init_schema` 和事务 helper 改走 Rust
  facade；不得只接 `_get_current_version`。
- `CW_USE_RUST_STORAGE=1` 为灰度开关；默认切换前必须完成 Python/Rust 同 fixture
  的 schema、pragma、事务、迁移报告和失败状态差分。
- `rollback_config.feature=rust_storage_service` 置 1 时回退 Python；回滚必须记录
  audit，不能静默双写。
- Rust 与 Python 不得同时持有同一业务写事务；facade 返回连接/transaction owner
  后，Python 不得绕过 service 直接 `sqlite3.connect` 写 registry。

## 6. 验收矩阵

- 新库、v1、v2、v10、v41、v42 fixture：迁移后 schema checksum 和关键表/索引一致；
- 并发读写、WAL checkpoint、busy_timeout、锁冲突和进程 kill -9：无半迁移版本；
- 未来版本、损坏数据库、缺表、错误 checksum：fail closed 且目标库不被覆盖；
- Python/Rust 事务提交、回滚、异常和重复迁移行为逐项差分；
- `cw refresh`、daemon 启动、backup/restore、任务库读写均走同一 StorageService；
- Linux x86_64/aarch64、Windows、macOS 至少执行 storage unit + smoke，Linux 再执行
  双 UID/锁冲突 E2E。

## 7. 完成定义

只有在 `db_base.py` 不再拥有默认 schema/migration/registry 写事务路径、上述差分和
恢复矩阵通过、rollback 可验证、文档刷新并由独立 Reviewer 审查后，C2 才能进入
`closed`。仅新增 Rust 查询函数或通过 `cargo check` 不算完成。

## 8. 2026-08-01 复审状态

本轮复核已补齐并验证以下基础能力：

- Rust 编译期嵌入完整 `db/schema.py` SQL，不再使用 `storage.rs` 的部分表清单作为生产 schema；
- `schema_version` 为主版本来源，同时写入 `PRAGMA user_version` 兼容旧库；
- `schema_migrations` 保存 v42 canonical schema 的 SHA-256 checksum，checksum 不一致时 fail-closed；
- 暴露 `storage_open`、`storage_begin`、`storage_commit`、`storage_rollback`，统一 WAL、busy timeout、外键和 checkpoint；
- Rust 初始化失败不会再被 `db_base.py` 静默吞掉；构造失败会主动关闭 Python 连接，避免留下数据库锁；
- 预迁移执行完整性检查、TRUNCATE checkpoint 和主库备份；
- 识别到旧 `files` 表或 v2/v3 数据形状时拒绝直接盖章 v42，避免数据重建尚未实现却丢失旧语义。

因此 C2 当前只能进入 `review`，不能进入 `closed`。仍然阻塞关闭的项目是：

1. v1→v3 的数据形状迁移（`files` 到 `file_instances`、symbols/calls/file_versions 重建）尚未以 Rust versioned migration 实现；当前实现会安全拒绝，而不是假装迁移成功。
2. `CodeGraphDB` 的业务写事务仍使用 Python `sqlite3` 连接，尚未完全改为 Rust-owned transaction；这需要后续 BuildService/Manifest/Task 写路径迁移共同完成。
3. v1/v2/v10/v41/v42 fixtures、kill-9、跨平台和双 UID 矩阵尚未全部执行；现有 focused tests 不能替代完整验收。

## 9. 2026-08-08 收尾记录

- 修复 `rust_ext/src/storage.rs` SCHEMA_VERSION 漂移（44→47）：`T-1785919930949` 已同步 daemon/mod.rs 与 abi_contract.rs 至 47，但遗漏 storage.rs；现全部对齐 `db/schema.py`（47）。
- 确认 `SCHEMA_TABLES_SQL`/`SCHEMA_INDEXES_SQL` 仅作参考常量，生产 schema 走编译期嵌入 `db/schema.py` 的 `canonical_schema_sql()`，无需同步。
- 修复后 `cargo test --lib storage::` 8 passed / 0 failed（建库、legacy v1-v3 迁移、checksum 策略 A、backup/checkpoint、SCHEMA_TOO_NEW）。
- 阻塞关闭项目不变：业务写事务 Rust-owned 化仍依赖 C4/C6（Manifest/BuildService 写路径迁移）；完整验收矩阵由 C9/C10 覆盖。
