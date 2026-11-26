# 前置条件告知（preq）— 任务图与交接包

- **snapshot**: preq-r3（must = REQ-PREQ-04/01/02/03；指定 should = REQ-PREQ-06；deferred = REQ-PREQ-05）
- **设计基线**: `8b1a1e3ee4a2a98f5cb7707d705bd7414a036abe`（.design/preq/ 六文件）
- **父任务**: `T-1795661715269-f3611f48`
- **workspace 配对**: workspace_id=1 / instance=4baea3ff12c2ea5c（identity_policy=legacy_identity_v1）

## 批次 1（已建单，7 张）

| # | task_id | REQ | 文件 | blocked-by |
|---|---|---|---|---|
| T7 | `T-1795663574847-eaea3d24` | REQ-PREQ-06 \| should | docs/design/cw-role-handoff-task-loop-v2-amendment.md | 无（文档先行） |
| T1 | `T-1795663519023-eb82b9a8` | REQ-PREQ-04 \| must | rust_ext/src/daemon/task_loop/prerequisites.rs | 无（根任务） |
| T2 | `T-1795663530764-a75be4a4` | REQ-PREQ-01 \| must | rust_ext/src/daemon/task_loop/next_action.rs | T1 |
| T3 | `T-1795663535104-aa05bd64` | REQ-PREQ-01 \| must | rust_ext/src/daemon/task_collab_query.rs | T1 |
| T4 | `T-1795663549865-19deda64` | REQ-PREQ-02 \| must | rust_ext/src/daemon/task_loop/claim.rs | T1 |
| T5 | `T-1795663552556-ba42830c` | REQ-PREQ-03 \| must | rust_ext/src/daemon/dispatch.rs | T1 |
| T6 | `T-1795663571791-34b69f20` | REQ-PREQ-01 \| must | rust_ext/src/daemon/task_collab_tests_prerequisites.rs | T1 T2 T3 T4 T5 |

建议认领顺序：T7 并行 → T1 → T2/T3/T4/T5（并行）→ T6。

## 批次 2（deferred，放行判据满足后注入）

- T8 work_next 派工过滤（REQ-PREQ-05 should，task_collab_lease.rs:929）——排序规则设计时定（open 项 O2）。

## 放行判据（R5 前置，只读判定）

全部 7 单到 review + 全程无 E_* 门禁错误 + ≥1 单完成 apply/close 全链路 → 注入 T8。

## 交接包清单

`task-map.md`（本文件）+ `requirements-brief.md` + `ac-map.md` + `symbol-anchor.md` + `contracts.md` + `mindmap.md` + `flowchart.md`。executor 用自身注册身份领取，身份不跨会话借用。
