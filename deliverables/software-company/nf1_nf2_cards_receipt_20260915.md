# NF1/NF2 独立缺陷卡建卡回执（2026-09-15）

## 1. 建卡结果

经 daemon authority 裸 RPC `task.create`（`identity_policy=legacy_identity_v1` + A′ 三角色合同），幂等脚本
`deliverables/software-company/create_nf_defect_cards.py` 执行成功：

| 卡 | task_id | 合同 | reviewer 合同 hash |
|---|---|---|---|
| NF1 承接：gate.resolve_findings SQL 引用不存在列 tasks.workspace_id 修复（§W20 F1 同族，C-21 期新发现） | `T-1789436398881-877c169c` | `TC-T-1789436398881-877c169c` rev1 `sha256:c0e39894d57505fa5eacc359b9c81d743499baf7f2eb70d830ce4219b9fd4a15` | `sha256:3f691024970453f94a17d7ddc7a5845db2eaf19f1d25af9b8c9000272a6f8863`（与 C-21/C-22 模板级一致） |
| NF2 承接：summary.generate upsert ON CONFLICT(symbol_hash) 无匹配 UNIQUE 约束修复（版本化语义重写，C-21 期新发现） | `T-1789436399100-948b9498` | `TC-T-1789436399100-948b9498` rev1 `sha256:84899c7e27e1783e804ea358a36f1f453a49036b73e60584ac7901418d35b46b` | 同上（模板级） |

- 父卡：`T-1788871227327-45c94bd8`（PYT 回归卡）；workspace：1 / `4baea3ff12c2ea5c`。
- daemon 端点：`http://127.0.0.1:8535`（PID 25756，git_commit `b7fa16f`，`cw daemon health` 回执为准）。
- 幂等复跑：2 exists / 0 created（task.list 按 title 判重）。
- 两卡各 4 步，全部 step `target_file` **文件级**（F5 教训落实）；step0 预声明确定性文档名
  `nf1_remediation_inventory.md` / `nf2_remediation_inventory.md`。
- executor_allowed（合同 allowed_paths，可目录前缀）：`rust_ext/src/daemon/edit_handlers.rs`、
  `tests/test_c21_edit_rule_route_workspace_authority.py`、`deliverables/software-company/`、
  `cw_task_commit_ledger.json`、`docs/evidence/`。

## 2. 侦察结论（建卡前已实测，已写入卡 origin/acceptance）

### NF1（与 §W20 F1 同族，C-19 未覆盖）

- 缺陷点：`rust_ext/src/daemon/edit_handlers.rs` `handle_resolve_gate_findings` 的 UPDATE SQL
  `WHERE decision_id = ?3 AND task_id IN (SELECT id FROM tasks WHERE workspace_id = ?4)`——
  tasks 表无 `workspace_id` 列（C-19 修复期 PRAGMA 实证同款）→ 恒 prepare 失败。
- 修法（C-19 先例直接套用）：子查询改
  `task_id IN (SELECT task_id FROM task_workspace_bindings WHERE workspace_id = ?4)`
  （`admin_handlers.rs` `handle_gc_audit_get` 已是修复后形态，同文件可对照）。
- xfail 锚：`test_gate_resolve_findings_nf1_known_defect`
  （tests/test_c21_edit_rule_route_workspace_authority.py，xfail(strict) 正例体）。
- 路由臂（dispatch.rs / snapshot_state.rs）已在 C-21 修复，卡内明令零触碰路由层。

### NF2（C-21 step2 A/B 实测新发现）

- 缺陷点：`edit_handlers.rs` `handle_summary_generate` 的
  `INSERT INTO symbol_summaries ... ON CONFLICT(symbol_hash) DO UPDATE`——权威库
  `symbol_summaries` 仅 `idx_summaries_hash` / `idx_summaries_current` 两个**非唯一**索引
  （PRAGMA index_list 实证）→ 恒运行时错误。曾被路由缺陷双重掩盖，自上线从未端到端可用。
- **修复方向裁决**（step0 复核后正式落定）：symbol_summaries 设计语义是版本化多行
  （`db/db_base.py` `_migrate_v5_to_v6` docstring「同一符号可保留多版本历史摘要」；
  Python 侧 `db/db_summary.py` `generate_summary` 用 UPDATE is_current=0 → version=MAX+1 →
  INSERT 事务）→ 修法 = Rust handler 重写为同源版本化语义，**不是**加 UNIQUE 约束
  （全列 UNIQUE 破坏版本化设计与 Python 侧实现）。可选兜底（部分唯一索引
  `UNIQUE(symbol_hash) WHERE is_current=1`）由 step0 评估后裁决。
- 合法形态锚：`job_runner.rs` 的 `ON CONFLICT(symbol_hash)`（`symbol_embeddings`）合法——
  `symbol_hash` 是 PRIMARY KEY（`sqlite_autoindex_symbol_embeddings_1`，pk 实证），不在范围。
- 权威库现状：`symbol_summaries` 当前 0 行数据（无存量重复 hash 风险）。
- xfail 锚：`test_summary_generate_nf2_known_defect`（同测试文件）。

## 3. 两卡均为产品代码卡

落点 `rust_ext/src/daemon/edit_handlers.rs` → **部署门禁必走**（`scripts/refresh_shared_runtime.ps1
-TaskId 本卡 task_id`、health.git_commit==HEAD、三方 sha256 一致、PID 记录、rollback=false）+ 部署后
生产只读 probe 前后对照。commit 前缀必须用各卡自身 task_id（严禁复用父卡/C-21/彼此 id）。

## 4. 诚实披露

1. 建卡编排 commit 前缀使用父卡 `T-1788871227327-45c94bd8`（原始 C 桶批量建卡即 PYT 卡编排产物，先例
   `step4_identity_correction_20260912` 批次）——此为编排产物提交，非修复 commit，不违反
   「承接卡提交前缀必须用自身 task_id」纪律（该纪律约束 NF1/NF2 各自的修复 commit）。
2. 本回执与建卡脚本、backlog 更新同 commit 提交；建卡编排不新增台账条目（台账登记修复闭环，
   与 C-16/C-17 建卡先例一致——由回执文档承载编排记录，commit hash 以 git log 为准）。
3. NF1/NF2 测试 step 均落同一测试文件 `tests/test_c21_edit_rule_route_workspace_authority.py`
   （xfail 锚迁移），两卡须串行承接（A′ 环一次一卡），避免同文件并行改动互相覆盖（C-22 Edit 竞态教训）。
4. 建卡脚本 `create_nf_defect_cards.py` 未修改 `create_c_bucket_remediation_tasks.py`（C-22 已闭环工件，
   保持证据一致性）。

## 5. 承接顺序建议

NF1 → NF2（NF1 修法零争议、先例成熟；NF2 有修复方向裁决需 step0 落定）。两卡互不阻塞，但共用
测试文件与 `edit_handlers.rs`，串行执行。
