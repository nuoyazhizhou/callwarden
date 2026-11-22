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
