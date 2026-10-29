# T-1793240742557-50dfd468 收口证据：遗留项承接映射

- 原卡：T-1793240742557-50dfd468（[BACKLOG] CLI/MCP 验收矩阵 Wave 3/4 与 MCP 写工具补测）
- 承接卡：T-1793248351476-e7d6650c（同名 BACKLOG，workspace_instance_id=4baea3ff12c2ea5c，ws=1）
- 收口方式：`task.supersede`（scope 承接）+ `task.cascade_close`（被替代卡遗留 pending 步骤豁免，GOV-FIX-03）
- 依据：原卡描述要求遗留项「不计入关闭条件但不得消失」，故先建立承接卡逐项保留，再退役原卡。

## 逐项映射（原卡步骤 → 承接卡步骤）

| # | 原卡步骤（pending） | 承接卡步骤 ID | 承接卡步骤 |
|---|---|---|---|
| 0 | Wave 3：治理写路径环 E2E（claim/report/verdict/apply/close 全链路 + lease 门禁）并产出 deliverables/e2e-matrix-wave3-2026*.md | S-1793248351479-e7f6e6ec | 同左（目标文件 deliverables/e2e-matrix-wave3.md） |
| 1 | Wave 4：并发/崩溃/多实例场景（并发部署、lease 抢占、daemon 重启恢复）并产出 deliverables/e2e-matrix-wave4-2026*.md | S-1793248351479-e7f769c8 | 同左（目标文件 deliverables/e2e-matrix-wave4.md） |
| 2 | 67 个 MCP 写工具补测（隔离 workspace + 变更后回滚），产出 MCP 写面矩阵 | S-1793248351479-e7f78e1c | 同左（目标文件 deliverables/mcp-write-matrix.md） |
| 3 | B 类 5 条双契约重测 + F-001 registry 数据治理 + build_graph 增量化收尾 | S-1793248351479-e7f7a910 | 同左（目标文件 docs/design/cli-mcp-matrix-backlog.md） |

## 状态

- 原卡 4 个步骤全部处于 `pending`，未执行、未伪造完成证据。
- 承接卡 4 个步骤全部 `pending`，后续按 A′ 环（claim/report/verdict/apply/close）逐项实证闭环。
- 收口判定沿用 `python scripts/check_e2e_matrix_closure.py`，承接卡关闭前同样需逐项实证。
