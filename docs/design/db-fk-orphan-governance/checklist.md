# DB 外键孤儿治理 Checklist（T-1793098691536-7c39fbc4）

- [x] `db/schema.py`：`file_versions`(file_instance_id/content_hash)、
      `file_symbol_versions`(file_version_id/symbol_hash)、`call_versions`(file_version_id)、
      `symbols`(file_instance_id/symbol_hash)、`calls`(caller_id)、
      `git_symbol_changes`(symbol_hash)、`git_file_changes`(file_instance_id)、
      `semgrep_findings`(file_instance_id)、`archived_files`(file_instance_id/workspace_id)
      的 `on_delete` 均为 `CASCADE`；`comments` 仍为 `NO ACTION`。
- [x] `db/db_gc.py::_delete_file_associated_data` 删除顺序为
      `calls → call_versions → file_symbol_versions → symbols → file_versions`。
- [x] `db/db_gc.py::gc_purge` 先逐个清子表，再按
      `semgrep_findings → git_file_changes → archived_files → file_instances` 删父行。
- [x] `db/db_gc.py::_delete_orphan_symbol_contents` 引用排除清单含
      `git_symbol_changes.symbol_hash`。
- [x] `db/db_gc.py::gc_reap_orphans` 默认 dry-run；apply 前 gzip 备份 + `gc_runs` 审计；
      清理期临时关闭 FK 并在 finally 恢复；复验受控范围孤儿为 0 否则抛 `FK_ORPHANS_REMAIN`；
      治理账本孤儿只报告不删。
- [x] `db/db_base.py::delete_workspace` 补齐 `calls`、`git_symbol_changes`、`git_file_changes`、
      `git_commits`、`archived_files`，并把 `gc_runs.workspace_id` 置 NULL。
- [x] 内容表写入不再用 `INSERT OR REPLACE`（会 DELETE 触发级联）：
      `db/db_cas_merge.py` 与 `rust_ext/src/daemon/cas_merge.rs` 改 `ON CONFLICT DO UPDATE`，
      两个克隆测试夹具同步改 UPSERT。
- [x] 未 bump `SCHEMA_VERSION`，未新增 CLI/MCP 路由。
- [x] 本地 `pytest tests/test_db_fk_orphans.py tests/test_gc.py tests/test_gc_retention.py
      tests/test_clone_detection.py tests/test_static_analysis_capabilities.py
      tests/test_schema_checksum_regression.py tests/test_f013_workspace_id_or_name.py`
      共 105 例全绿。
- [x] `ruff check` 对全部改动文件通过。
- [x] 提交推送后 CI 绿（f697fdb：14/14 jobs success，Python 3.10-3.14 全绿）。
