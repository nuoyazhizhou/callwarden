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
- [x] dry-run 复用同一收敛删除路径并在事务内 `ROLLBACK`，返回 `projected_deleted`：
      线上备份副本验证投影 716,829 与历史实删逐表一致（原实现只报 78,805 违规条目）。
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
- [x] 提交推送后 CI 绿（f697fdb、111b2e5 均 14/14 jobs success，Python 3.10-3.14 全绿）。
- [x] `gc_reap_orphans` 覆盖 `destructive_operations` / `gc_policies` /
      `workspace_scan_runs`（删除）与 `gc_runs` / `token_savings_ledger` /
      `file_edit_audit`（置空保留），`out_of_scope` 仅剩治理/权威账本。
- [x] `delete_workspace` 同步处理上述五张表，含这些行的工作区可正常删除且
      `foreign_key_check()` 为空。
