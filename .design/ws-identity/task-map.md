# task-map · ws-identity（R4 首批）

> 建单 2026-11-22 · 基线 e9135e4 · workspace_id=1 / instance=4baea3ff12c2ea5c
> 首批 8 单全部 must（快照内）；C-09（REQ-05 should）第二批注入。

| 卡 | task_id | title | 依赖（blocked-by） |
|---|---|---|---|
| C-01 | T-1795331116899-84fdc1a8 | workspace.rs:compute_workspace_instance_id 扩展 仓库级三级身份派生+remote归一 | — |
| C-02 | T-1795331141372-37b25098 | workspace.rs:register_workspace_preferred 扩展 register 链消费 remote/main_root 入参 | C-01 |
| C-03 | T-1795331141445-3c0ce7c0 | daemon_client.py:derive_workspace_instance_id 扩展 Python 同语义三级派生+metadata 增 main_root | — |
| C-04 | T-1795331141527-40ed26ec | workspace_migration.rs:migrate_repo_identity 新建 存量 workspace 一次性迁移 | C-01 |
| C-05 | T-1795331153724-17ec0f80 | workspace.rs:handle_workspace_migrate 扩展 workspace.migrate RPC 薄壳 | C-04 |
| C-06 | T-1795331159944-8aade1c8 | dispatch.rs:dispatch 扩展 workspace.migrate 路由接线 | C-05 |
| C-07 | T-1795331166364-09564dc0 | cli/main.py:cmd_workspace_migrate 新建 cw workspace migrate 子命令 | C-06 |
| C-08 | T-1795331153822-1dc403f4 | tests/test_workspace_identity.py:test_workspace_identity 新建 Python 身份矩阵单测 | C-03 |

**可并行组**：C-01 ∥ C-03（起手）；C-02 ∥ C-04（均只依赖 C-01；注意 C-02 与 C-01 同文件 workspace.rs，须 C-01 完成后串行认领）；C-08 与 C-04..C-07 并行（依赖 C-03）。

**关键路径**：C-01 → C-04 → C-05 → C-06 → C-07。

**放行判据**（requirements-cw R4）：全部到 review + 全程无 E_* 门禁错误 + ≥1 单完成 apply/close 全链路；满足后注入第二批（C-09，REQ-05 should）。

**职责边界**：apply/close 由 executor/reviewer 执行；Architect 只读判定进度。

## R5 审查整改单（code-review-cw，2026-11-22）

基线 `5912f52` 未提交改动审查发现，全部已修复并转 `review`：

| 发现 | REQ | 严重度 | task_id | 修复摘要 |
|---|---|---|---|---|
| 迁移排除 `task_workspace_bindings` 导致存量任务不可见 + claim 三方一致性破裂 | REQ-03 | 🔴 | `T-1795339793129-9c9d0788` | 同事务改写 binding（workspace_id + capture 指针），同步 `task_step_role_contract_bindings` / `role_contract_lineages` |
| 模块文档谎称 handler 内重放 register 合并 registry | REQ-03 | 🟡 | `T-1795339793247-a3a5a7b0` | 文档改为「registry 靠下次自然 register 收敛」 |
| `MigrationReport.groups/scanned` 语义与文档不符 | REQ-04 | 🟡 | `T-1795339801279-82672b58` | 新增 `total_groups`，明确 scanned/groups 口径 |
| 权限矩阵缺 human/service 用例；`resolve_main_repo_root` 公开面冗余 | REQ-03 | 🟡 | `T-1795339801423-8af32c40` | 补 2 例测试；收窄为 `pub(crate)` |

**变更单说明（C-04 合同反转）**：原合同将 `task_workspace_bindings` 列为「不可改写审计壳」并排除；审查证明该表是 `task.list` 可见性与 claim 一致性的权威来源，排除会同时丢任务与破坏一致性。经本变更改为「同步 rewire + capture 指针指向追加 capture，历史 capture 行不改写」，理由记录于 `workspace_migration.rs` 模块文档。

**证据**：`.design/ws-identity/evidence/AC-01..05.md`（C-09 完成后 AC-01/02 已翻转为 PASS，5/5 全 PASS）。

## 第二批（REQ-05 should，2026-11-22）

| 卡 | task_id | title | 依赖 | 状态 |
|---|---|---|---|---|
| C-09 | `T-1795349801610-e48b9438` | 三 worktree 仓库级身份与跨 worktree 协同端到端验收 | C-01/02/03/07 | review（2026-11-22） |

- 交付：`tests/test_worktree_identity_e2e.py`（7 用例，隔离 daemon + 真实 git 仓库/worktree/clone）。
- 执行：`python -m pytest tests/test_worktree_identity_e2e.py -v` → **7 passed**；批次回归 35 passed（基线 28 + 新增 7）。
- 证据：AC-01.md / AC-02.md 翻转为 PASS（附用例矩阵与执行记录）；AC-04 registry 侧「同 repo 仅一条 active 行」进程级验证随 `test_registry_single_active_row` 补齐。

