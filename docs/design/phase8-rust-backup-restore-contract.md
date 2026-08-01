# Phase 8 Rust 完整 Backup/Restore 契约

## 1. 背景

复核 `T-1785148066853-ccad23fe` 发现，Rust daemon 目前只支持把 registry
SQLite 数据库导出到一个路径；Python `BackupManager` 才覆盖完整备份格式：
registry、CAS、audit、snapshots、`backup_meta.json` 和文件校验。因此原任务
不能按“backup/restore 已完成”关闭。

## 2. 范围

Rust PyO3 核心提供完整备份协议：

- `backup_full`：创建 backup 目录，使用 SQLite 一致性快照复制 DB，递归复制
  snapshots，生成与 Python 兼容的元数据和 SHA-256；
- `backup_db_only`：只复制 registry/CAS/audit；
- `restore_backup`：先校验元数据和文件 hash，再恢复文件与 snapshots；
- `verify_backup`：不写入目标数据，只验证元数据、文件和目录；
- `list_backups`、`delete_backup`、`cleanup_backups`：备份目录运维操作。

Python wrapper 保持原有返回字典、配置路径和 rollback 行为。Rust 不接收任意
目标路径之外的备份成员名；backup id 必须是单层安全名称。

## 3. 一致性与安全

1. DB 文件先执行 WAL passive checkpoint，再通过 `VACUUM INTO` 生成一致副本；
   不存在的可选 DB 不写入 `files`。
2. 备份先写临时目录，元数据成功写入后原子 rename 为最终目录；已有同名目录
   直接失败，避免部分结果被二次命中。
3. restore 先验证 `backup_meta.json` checksum 和每个文件 SHA-256；发现任何
   必需文件缺失或 hash 不匹配时 fail closed，不覆盖目标。
4. 恢复路径只能映射到配置提供的 registry/CAS/audit/data_root，禁止通过
   `../`、绝对路径或符号链接逃逸。
5. Rust 失败时 wrapper 可按 rollback flag 回退到现有 Python 实现；默认 Rust。

## 4. 验收

- Rust/Python 对同一临时配置生成的 `files`、`backup_type`、校验和语义一致；
- registry/CAS/audit/snapshots 全部可 round-trip 恢复；
- hash 损坏、meta 损坏、路径穿越、重复 backup id 和并发创建均 fail closed；
- `cargo check`、Rust 单元测试、Python Phase 8 备份回归通过；
- `cw refresh` 覆盖修改文件后，任务只推进到 `review`。
