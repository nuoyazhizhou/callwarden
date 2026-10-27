# DB 外键孤儿治理 Spec（T-1793098691536-7c39fbc4）

## 1. 背景与结论

线上 task-DB（`~/.callwarden/callwarden.db`，schema v60）长期在 `foreign_keys=OFF`
的 GC 写路径下运行，`PRAGMA foreign_key_check` 报 **79,684** 条违规（清理前基线
78,849，workspace 清理新增约 866）。性质是**设计遗留**：内容寻址版本表的外键全部
`ON DELETE NO ACTION`，而 GC 直接删父行（`file_versions` / `symbol_contents`），
子表（`file_symbol_versions` / `call_versions` / `git_symbol_changes`）未同步删除。

实测孤儿分布（按 child→parent）：

| child | parent | 行数 |
|---|---|---|
| file_symbol_versions | symbol_contents | 57,840 |
| call_versions | file_versions | 12,253 |
| file_versions | file_contents | 3,781 |
| git_symbol_changes | symbol_contents | 2,536 |
| file_versions | file_instances | 2,301 |
| role_contract_revisions | role_contract_lineages | 741 |
| task_workspace_bindings | tasks | 86 |
| file_instances | workspaces | 55 |
| role_contract_lineages | task_workspace_bindings | 39 |
| gc_runs / destructive_operations | workspaces | 16 / 16 |
| rollback_config / task_operation_ledger / token_savings_ledger / gc_policies / file_edit_audit / workspace_scan_runs | tasks / workspaces | 9 / 4 / 2 / 2 / 2 / 1 |

## 2. 关键事实（已核实）

- `db/schema.py::SCHEMA_SQL` 是唯一 schema 真相源：
  - Python 新库：`_split_schema_sql()` 派生 `SCHEMA_TABLES_SQL/SCHEMA_INDEXES_SQL`（[schema.py#L2155-L2210](file:///c:/git_work/callwarden/db/schema.py#L2155-L2210)）。
  - Rust 新库/迁移：`rust_ext/src/storage.rs` 以 `include_str!("../db/schema.py")` 内嵌并在
    `initialize_or_migrate` 执行 `canonical_schema_sql()`（[storage.rs#L31-L49](file:///c:/git_work/callwarden/rust_ext/src/storage.rs#L31-L49)、[storage.rs#L1512](file:///c:/git_work/callwarden/rust_ext/src/storage.rs#L1512)）。
  - 只改 `schema.py` 即可同时覆盖两条建库路径，无需改 Rust。
- 既有库 checksum 变更安全：`initialize_or_migrate` 在 `current_version == expected`
  且 checksum 不一致时，只要 canonical 表名齐全就重写 checksum并放行
  （[storage.rs#L1409-L1447](file:///c:/git_work/callwarden/rust_ext/src/storage.rs#L1409-L1447)）。因此**不 bump schema 版本**。
- SQLite 不支持改既有表的外键动作；老库只能靠「控清理 + GC 顺序」治理，新库靠 CASCADE。
- `_delete_file_associated_data` 漏删 `call_versions`（[db_gc.py#L343-L372](file:///c:/git_work/callwarden/db/db_gc.py#L343-L372)）。
- `gc_purge` 仅删 `archived_files`/`file_instances`，假定「归档时已删干净」
  （[db_gc.py#L613-L626](file:///c:/git_work/callwarden/db/db_gc.py#L613-L626)）。
- `_delete_orphan_symbol_contents` 的引用排除清单缺 `git_symbol_changes`
  （[db_gc.py#L1145-L1161](file:///c:/git_work/callwarden/db/db_gc.py#L1145-L1161)）。
- `delete_workspace` 漏删 `archived_files` / `git_file_changes` / `git_commits`，且不处理
  `gc_runs`（[db_base.py#L4564-L4653](file:///c:/git_work/callwarden/db/db_base.py#L4564-L4653)）。
- `PRAGMA foreign_key_check` 无法在 `foreign_keys=ON` 下边删父行边留子行；清理期需临时
  关闭 FK，结束后必须再校验。

## 3. 目标与决策

1. **新库级联**：给版本表/图数据子表外键加 `ON DELETE CASCADE`（只改 `db/schema.py`）。
2. **GC 删除契约对齐**：所有 GC 路径严格按「孙表→子表→父表→内容表」顺序删除。
3. **受控历史清理**：新增 `gc_reap_orphans`，默认 dry-run；apply 前自动 gzip 备份、
   写 `gc_runs` 审计；按 `foreign_key_check` 驱动、分批删除、收敛后复验。
4. **清理范围（重要决策）**：仅图/内容/GC 审计表
   （`file_symbol_versions`、`call_versions`、`symbols`、`calls`、`git_symbol_changes`、
   `git_file_changes`、`semgrep_findings`、`file_versions`、`archived_files`、
   `file_instances`、`file_contents`、`symbol_contents`；`gc_runs`
   孤儿置 `workspace_id=NULL`、`destructive_operations` 孤儿删除）。
   **治理/权威账本表不自动删除**（`task_workspace_bindings`、`role_contract_lineages`、
   `role_contract_revisions`、`task_step_role_contract_bindings`、`rollback_config`、
   `task_operation_ledger`、`token_savings_ledger`、`gc_policies`、`file_edit_audit`、
   `workspace_scan_runs`），仅在结果中报告，避免「为过 FK check 而销毁审计证据」。
5. **防回归基线**：新增单测，任何 GC/清理路径执行后 `PRAGMA foreign_key_check` 必须为空；
   新库 schema 的级联动作必须为 `CASCADE`。

## 4. 代码变更（精确到函数）

### 4.1 `db/schema.py`（新库 DDL）

| 表 | 外键 | 改为 |
|---|---|---|
| `file_versions` | `file_instance_id → file_instances(id)` | `ON DELETE CASCADE` |
| `file_versions` | `content_hash → file_contents(content_hash)` | `ON DELETE CASCADE` |
| `file_symbol_versions` | `file_version_id → file_versions(id)` | `ON DELETE CASCADE` |
| `file_symbol_versions` | `symbol_hash → symbol_contents(content_hash)` | `ON DELETE CASCADE` |
| `call_versions` | `file_version_id → file_versions(id)` | `ON DELETE CASCADE` |
| `symbols` | `file_instance_id → file_instances(id)` | `ON DELETE CASCADE` |
| `symbols` | `symbol_hash → symbol_contents(content_hash)` | `ON DELETE CASCADE` |
| `calls` | `caller_id → symbols(id)` | `ON DELETE CASCADE` |
| `git_symbol_changes` | `symbol_hash → symbol_contents(content_hash)` | `ON DELETE CASCADE` |
| `git_file_changes` | `file_instance_id → file_instances(id)` | `ON DELETE CASCADE` |
| `semgrep_findings` | `file_instance_id → file_instances(id)` | `ON DELETE CASCADE` |
| `archived_files` | `file_instance_id → file_instances(id)` | `ON DELETE CASCADE` |
| `archived_files` | `workspace_id → workspaces(id)` | `ON DELETE CASCADE` |

不动的表：`comments`（用户注释，靠 GC 排除清单保护）、`file_instances.workspace_id`
（删除 workspace 走显式顺序，避免隐式整库图数据删除）、所有治理账本表。

### 4.2 `db/db_gc.py`

1. `_delete_file_associated_data(file_instance_id)`：顺序改为
   `calls → call_versions → file_symbol_versions → symbols → file_versions`；
   新增 `DELETE FROM call_versions WHERE file_version_id IN (...)`
   （复用 `symbols` 同款子查询）。
2. `gc_purge`：删除 `file_instances` 前，逐个 `_delete_file_associated_data(fi_id)`；
   随后按序 `semgrep_findings → git_file_changes → archived_files → file_instances`
   （批量 `IN`）。
3. `_delete_orphan_symbol_contents`：排除清单补
   `git_symbol_changes.symbol_hash`。
4. 新增 `gc_reap_orphans(dry_run=True, backup=True, operator="cli")`：
   - 受控范围常量 `_REAP_SCOPE_TABLES`（见 §3.4）。
   - dry-run 只统计；apply：`_create_gc_db_backup("fk-orphans")` → `_start_gc_audit`
     （operation=`fk-orphans`）→ `PRAGMA foreign_keys=OFF` → 最多 10 轮：
     `foreign_key_check` 取范围内 `(table, rowid)` → 分批 `DELETE ... WHERE rowid IN (...)`；
     `gc_runs` 孤儿改 `UPDATE ... SET workspace_id=NULL`。
   - finally 恢复 `foreign_keys` 原值；commit；再次 `foreign_key_check`
     并断言范围内为 0，否则 `_fail_gc_audit` 抛错。
   - 返回 `{audit_id, dry_run, backup_path, backup_size, before, after, deleted:{table: n}, scope}`。
   - 不进入 CLI/MCP 路由（一次性治理 + 测试入口），避免扩大公开 API。

### 4.3 `db/db_base.py::delete_workspace`

在既有 child→parent 删除序列中补齐：
`git_file_changes`（按 ws 的 file_instances）→ `git_commits`（workspace_id）
→ `archived_files`（workspace_id）→ `UPDATE gc_runs SET workspace_id=NULL`
→ 再删 `file_instances` / `workspaces`。

### 4.4 `tests/test_db_fk_orphans.py`（新增）

1. `test_new_schema_version_fks_cascade`：新库 `PRAGMA foreign_key_list` 断言上表
   全部 `on_delete == 'CASCADE'`。
2. `test_cascade_deletes_version_children`：插 `file_instances → file_versions →
   file_symbol_versions/call_versions`，删除 `file_instances` 后子表归零、`foreign_key_check()==[]`。
3. `test_gc_archive_leaves_no_orphans`：构造含 `call_versions` 的归档目标，
   `gc_archive(force=True)` 后 `foreign_key_check()==[]`（覆盖漏删 call_versions 回归）。
4. `test_gc_purge_leaves_no_orphans`：构造残留版本 + `archived_files`，`gc_purge(0)` 后为空。
5. `test_reap_orphans_dry_run_and_apply`：`PRAGMA foreign_keys=OFF` 造孤儿（file_versions/
   call_versions/file_symbol_versions/git_symbol_changes），dry-run 不改数；apply 后
   `deleted` 计数 >0、受控范围孤儿为 0、合法行保留、备份文件存在。

### 4.5 内容表写入改 UPSERT（实现期发现的级联风险）

SQLite 的 `INSERT OR REPLACE` 等价 `DELETE + INSERT`，会触发 `ON DELETE CASCADE`。
`file_contents` 是内容寻址的去重表，`upsert_file_records` 每次注册文件都会 REPLACE
同一 `content_hash`，在本次给 `file_versions.content_hash` 加上 CASCADE 后会连带
删掉该内容的**全部版本历史**（数据丢失）。

因此：`db/db_cas_merge.py::upsert_file_records` 与 `rust_ext/src/daemon/cas_merge.rs::
upsert_file_records` 的 `INSERT OR REPLACE INTO file_contents` 改为
`INSERT ... ON CONFLICT(content_hash) DO UPDATE SET ...`（结果行与原语义一致，
但不产生 DELETE，因而不触发级联）。测试夹具（`test_clone_detection.py`、
`test_static_analysis_capabilities.py` 的 `_seed_symbol`）对 `symbol_contents`
的 `INSERT OR REPLACE` 同理改 UPSERT。

生产代码其余内容表写入已是 `INSERT OR IGNORE`，无需改动。

## 5. 验证
- `pytest tests/test_db_fk_orphans.py tests/test_gc.py tests/test_gc_retention.py -q`
- `ruff check db/schema.py db/db_gc.py db/db_base.py tests/test_db_fk_orphans.py`
- `pytest tests/test_gc_flow.py`（若存在）与 Rust `cargo test --lib storage`（schema 内嵌校验）。
- 部署后：对线上库执行 `gc_reap_orphans(dry_run=False)`，再跑
  `PRAGMA foreign_key_check` + 按表计数，确认图中孤儿为 0、治理孤儿数量如实报告。
- CI 全绿后回填任务卡 step 结果。

## 6. 明确不做

- 不 bump `SCHEMA_VERSION`（避免 Rust/Python 双版本源与冗余迁移）。
- 不重建既有库表来补 CASCADE（SQLite 需整表复制，风险/收益不匹配）。
- 不删除治理/权威账本孤儿（只报告）。
- 不改 CLI/MCP 公开路由。
