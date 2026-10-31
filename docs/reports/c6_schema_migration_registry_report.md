# C6 修复总结报告 · Schema 迁移注册表缺口

**日期：** 2026-10-31　**任务卡：** `T-1793368261327-97e8e5fc`　**提交：** `c0f8696`
**台账：** [cli-mcp-matrix-backlog.md](../design/cli-mcp-matrix-backlog.md) §5（C6 行）、§11
**结论：** 已修复并闭环（`closed` / `COMPLETE`），矩阵门禁 PASS。

---

## 一、问题摘要

`db_base.CodeGraphBase._get_migrations` 的迁移注册表实测仅覆盖 `2..52, 59, 61`（53 条），
缺 `53/54/55/56/57/58/60` 七条；而 `_migrate_schema` 对未注册版本直接 `continue` 静默跳过，
随后仍把 `schema_version` 打标到目标版本。

**后果（fail-open）：** 在 `version=52` 的库上执行 52→61 迁移，最终 `MAX(version)=61`，但
v53 的 `workspace_authority_captures` / `task_workspace_bindings`、v55 的
`role_contract_lineages` / `role_contract_revisions`、v57 的 `verdict_normalization_rules`
/ `..._revocations` / `task_step_role_contract_bindings` 等全部缺失，库被"打标"成 v61
却缺表缺列，后续读写直接报 `no such table/column`。

Rust 路径 `storage_initialize_or_migrate` 会执行 canonical `SCHEMA_SQL`，掩盖了缺口；
一旦 Rust 扩展缺失或被 rollback，纯 Python 路径必现该问题。

---

## 二、根因（4 层）

| 层 | 根因 |
|---|---|
| L1 注册表 | `_get_migrations()` 只登记到 v52 与 v59/v61，v53-v58/v60 从未登记 |
| L2 调度器 | `_migrate_schema` 对缺失版本静默跳过，不校验注册表完整性，最后无条件写入目标版本 |
| L3 DDL 真相源 | v53-v57 的建表语句只存在于 `schema.py` 整段 SQL，迁移函数内无对应 DDL，无法幂等补建 |
| L4 版本语义 | v58 无独立 DDL：它等价于"补 v53-v57 表/列漂移"的收敛点（commit 030fd8c 补 11 表 + 20 列但未 bump 版本号） |

---

## 三、修复内容（仅 `db/db_base.py`）

### 3.1 单一 DDL 真相源与幂等工具（L107-L214）

- `canonical_table_statements()`（[db_base.py L152](../../db/db_base.py#L152)）：按行尾 `;`
  切分 `SCHEMA_TABLES_SQL`，用 `_CREATE_TABLE_RE` 提取表名 → `{表名: 完整 DDL}`（共 88 张表），
  **不复制 SQL**，从根上消除 Python 侧与 `schema.py` 的二次真相源漂移。
- `table_exists()` / `ensure_canonical_tables()`（L185）/ `ensure_table_columns()`（L198）：
  统一幂等建表/补列；缺 canonical DDL 时抛 `RuntimeError`（fail-closed），表缺失则跳过补列。
- `TASK_CONTRACT_V57_COLUMNS`（L122）+ `_ensure_task_contract_v57_columns()`（L210）：
  v57 权威 20 列（`task_contract_revisions` 2 列、`task_revision_attestations` 9 列、
  `task_contract_dependencies` 9 列），原 `_ensure_compat_columns` 内联字典改为调用该常量。

### 3.2 七条缺失迁移

| 版本 | 迁移函数 | 内容 |
|---|---|---|
| v53 | `_migrate_v52_to_v53` (L3275) | `workspace_authority_captures`、`task_workspace_bindings` |
| v54 | `_migrate_v53_to_v54` (L3287) | `task_loop_capability_promotion_events` + `idx_promotion_events_workspace` |
| v55 | `_migrate_v54_to_v55` (L3299) | `role_contract_lineages`、`role_contract_revisions` |
| v56 | `_migrate_v55_to_v56` (L3308) | `task_step_role_contract_bindings` + `idx_step_binding_current` / `idx_step_binding_revision_id` |
| v57 | `_migrate_v56_to_v57` (L3326) | `verdict_normalization_rules`、`..._revocations` + v57 20 列 |
| v58 | `_migrate_v57_to_v58` (L3341) | 按序重放 v52→v57（补 11 表 + 20 列漂移，幂等） |
| v60 | `_migrate_v59_to_v60` (L3535，既有) | `task_quality_findings.severity` + Role Worker 三表 |

### 3.3 注册表补齐与调度器 fail-closed

- `_get_migrations()`（L4363）：补入 53/54/55/56/57/58/60，注册表由 53 条 → **60 条**，`2..61` 无缺口。
- `_migrate_schema()`（L4315）：迁移前校验 `range(from+1, to+1)` 全量登记，缺口即抛
  `RuntimeError("schema migration registry missing target versions: ...")`（L4338），
  **杜绝"跳过 + 打标"**。

### 3.4 Role Worker 三表事务安全

`ensure_role_worker_schema()`（L95）由 `executescript` 改为 `ROLE_WORKER_SCHEMA_STATEMENTS`
元组逐条 `execute` + `commit`——`executescript` 的隐式提交会破坏 `_migrate_schema`
外层事务的原子回滚。

---

## 四、验证证据

| 验证项 | 结果 |
|---|---|
| 注册表完整性 | `2..61` 连续 60 条，`func`/`description` 均非空 |
| canonical DDL 提取 | 88 张表；11 张治理表全部命中（v53/v55/v57 目标表） |
| v52 内存库 52→61 | `MAX(version)=61`；11 张治理表齐备；v57 20 列齐备；`task_quality_findings.severity` 就位 |
| 缺口 fail-closed | 去掉 v55 条目 → `RuntimeError: schema migration registry missing target versions: 55 (from_version=52 to_version=61)` |
| 幂等性 | v52-v58 补建重放两次无异常、无重复对象 |
| 新增单测 | `tests/test_schema_migration_registry_gap.py` **12 passed** |
| 回归（治理/质量/审计链） | `test_a14_audit_fix`、`test_task_quality_gate`、`test_audit_chain`、`test_db_v2_to_v3_migration_fk`、`test_abi_contract` 合计 **202 passed** |
| 回归（任务契约/DB/CLI） | `test_db_task_contracts`、`test_db_tests`、`test_task_supersede_cli`、`test_task_quality_gate` 合计 **178 passed** |
| 文档门禁 | `test_architecture_doc`、`test_abi_contract`、`test_migration_manifest` 全绿 |
| 矩阵门禁 | `scripts/check_e2e_matrix_closure.py` → **PASS** |

---

## 五、治理闭环（A′ 三契约）

| 项 | 值 |
|---|---|
| Task Contract | `TC-T-1793368261327-97e8e5fc` rev 1 · `sha256:346e38c1...86dcad` |
| Reviewer Role Contract | `rcl-...-reviewer` rev 1 · `sha256:3e8debc9...34f0b` |
| Adjudicator Role Contract | `rcl-...-adjudicator` rev 1 · `sha256:3a0004bb...b21b25` |
| Review snapshot | `ccbe452e0e59cb36`（generation 7） |
| Reviewer verdict | `V-8c1f417a39a1132d663f766a` · `pass` · event 816 |
| Reviewer lease | `L-f7e1b908b9ab73fe` · fencing 1 · released |
| Adjudicator lease | `L-2adebf65ecb02d31` · fencing 2 · released |
| apply / close | `applied` → `closed`（结构化身份 agent/instance/session/model 全量记录） |
| 终态投影 | `workflow_status=completed`、`decision=COMPLETE`、`next_action=finalize` |

步骤：#0 `triage` → #1 `implement` → #2 `verify_and_document`，全部 `done`。

---

## 六、边界与残留

1. **不扩表级兜底**：仅补 v53-v57 与 v60 的权威表/列，不为历史 compat 表做泛化补建。
2. **checksum 归 Rust**：schema checksum 校验仍由 Rust `storage_initialize_or_migrate` 权威执行，Python 侧不重复实现。
3. **v57 初始 rule row 不播种**：`verdict_normalization_rules` 的 `verdict-normalization/v1` 初始行由 daemon/bootstrap 负责，迁移只建表。
4. **v58 语义固化**：v58 继续定义为"重放 v52→v57 补漂移"，不引入新 DDL；若后续需要独立语义，应在 `schema.py` 明确 bump。

---

## 七、改动文件

| 文件 | 类型 | 说明 |
|---|---|---|
| `db/db_base.py` | 修改 | 注册表 + 7 迁移 + 幂等工具 + fail-closed + 事务安全 |
| `tests/test_schema_migration_registry_gap.py` | 新增 | 12 项缺口/迁移/幂等单测 |
| `docs/design/cli-mcp-matrix-backlog.md` | 修改 | §5 C6 行 + §11 明细 |

统计：`3 files changed, 551 insertions(+), 86 deletions(-)`
