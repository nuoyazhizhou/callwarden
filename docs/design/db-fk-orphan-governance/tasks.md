# DB 外键孤儿治理 Tasks（T-1793098691536-7c39fbc4）

依据：[spec.md](file:///c:/git_work/callwarden/docs/design/db-fk-orphan-governance/spec.md)

## - [x] T1 [design] — 删除契约与清理范围冻结
- 产出：`spec.md` §2 §3 §4（孤儿分布、GC 缺口、治理账本不删的决策）。
- 验收：范围表、CASCADE 清单、不 bump 版本的依据齐全。

## - [x] T2 [implement] — `db/schema.py` 新库级联
- 文件：`db/schema.py`
- 改动：`file_versions`(2 FK)、`file_symbol_versions`(2)、`call_versions`(1)、
  `symbols`(2)、`calls`(1)、`git_symbol_changes`(1)、`git_file_changes`(1)、
  `semgrep_findings`(1)、`archived_files`(2) 全部改 `ON DELETE CASCADE`。
- 验收：新库 `PRAGMA foreign_key_list('<table>')` 的 `on_delete` 全为 `CASCADE`。

## - [x] T3 [implement] — `db/db_gc.py` 删除顺序 + 受控清理
- 文件：`db/db_gc.py`
- 改动：
  1. `_delete_file_associated_data` 补 `call_versions`，顺序 `calls → call_versions →
     file_symbol_versions → symbols → file_versions`。
  2. `gc_purge` 前置 `_delete_file_associated_data`，并按
     `semgrep_findings → git_file_changes → archived_files → file_instances` 批量删。
  3. `_delete_orphan_symbol_contents` 排除 `git_symbol_changes.symbol_hash`。
  4. 新增 `gc_reap_orphans(dry_run=True, backup=True, operator="cli")`：
     备份 → 审计 → FK 关闭 → 迭代删范围内孤儿 → 恢复 FK → 复验为 0。
- 验收：`test_db_fk_orphans.py` 全部通过；`deleted` 合计 > 0；备份文件存在。

## - [x] T4 [implement] — `db/db_base.py` 删除工作区补齐
- 文件：`db/db_base.py::delete_workspace`
- 改动：补 `git_file_changes → git_commits → archived_files`；`gc_runs` 孤儿置 NULL；
  再删 `file_instances/workspaces`。
- 验收：删除一个含归档/git 记录的 workspace 后 `foreign_key_check()` 为空。

## - [x] T5 [verify] — CI 基线断言
- 文件：`tests/test_db_fk_orphans.py`（新增，6 个用例）
- 验收：任一 GC/清理路径后 `PRAGMA foreign_key_check` 为空；级联 DDL 断言通过；
  本地 `pytest` 目标套件 + `ruff check` 通过；提交推送后 CI 绿（f697fdb 全绿）。

## - [x] T6 [implement] — workspace 外键审计表孤儿治理（补充）
- 文件：`db/db_gc.py`、`db/db_base.py`、`tests/test_db_fk_orphans.py`
- 改动：
  1. `_REAP_SCOPE_TABLES` 增加 `gc_policies`、`workspace_scan_runs`（连同既有
     `destructive_operations` 一起删除）。
  2. 新增 `_REAP_NULLIFY_WORKSPACE_TABLES`（`gc_runs`、`token_savings_ledger`、
     `file_edit_audit`）：孤儿只置 `workspace_id=NULL`，保留审计记录。
  3. `delete_workspace` 同步补齐上述五张表（NOT NULL 的删除、可空的解除归属），
     否则 `DELETE FROM workspaces` 会 FK 失败、工作区删不掉。
  4. 测试：reap 与 delete_workspace 用例覆盖五张表，断言删除/置空与
     `foreign_key_check` 为空。
- 验收：`tests/test_db_fk_orphans.py` 6 例通过；相关套件 69 例通过；ruff 通过。
