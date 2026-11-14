# Legacy 任务卡 workspace 补绑批量迁移技术总结

- 日期：2026-11-14
- 范围：`callwarden.db` task-DB 中全部 793 张历史未绑定任务卡（均为 `closed` 终态）
- 性质：纯运营迁移（无代码变更，未建治理任务卡）
- RPC 通道：`task.attest_legacy_workspace_binding`（daemon 权威路径，P0-B）
- 证据：`C:\Users\wanpi\AppData\Local\Temp\cw_legacy_bind\legacy_bind_evidence.json`
  （文件 sha256 `67a54520…398e`，已由 task_operation_ledger 固化；批清单
  `bind_plan.json`，逐卡审计 `done.jsonl`）

## 1. 迁移前画像

| 归因信号 | 数量 | 结论 |
|---|---|---|
| created_at < TokenSlim 建仓（1786546103） | 628 | 当时 callwarden 是唯一业务 ws → ws1 |
| post-TS 无 ws9/10/17 冲突信号 | ~147 | ws1（callwarden 后续开发） |
| action_identities → ws10 | 4 | TokenSlim（anchor: T-1794384007791-7f12a250） |
| action_identities → ws9 (DeepProbe) | 4 | 无 anchor，需 bootstrap |
| action_identities → ws17 (worktree) | 6 | 无 anchor，需 bootstrap |
| 双信号歧义（T-1786539379174-90f74174，同有 ws9+ws10） | 1 | 当批归 ws9；2026-11-14 取证更正为误绑（真实归属 ws1，见 §6 与 correction 文件） |

已知污染源：`record_action_identity` 对 legacy 卡回退写 active workspace，故
action_identities 归因可信度有限，仅作辅助信号；时间桶为主信号。

## 2. 执行结果

| workspace | 迁移卡数 | anchor | instance | attest fencing |
|---|---|---|---|---|
| ws1 callwarden | 779 | T-1794523701342-6f876f50 | `4baea3ff12c2ea5c` | 4 |
| ws10 TokenSlim | 4 | T-1794384007791-7f12a250 | `d079f60cac8d6186` | 8 |
| ws9 DeepProbe | 4 | T-1794614115928-b8cbaa40（bootstrap） | `3b5e466df3dab607` | 1 |
| ws17 cw-design-repair | 6 | T-1794614209133-6c4adff4（bootstrap） | `219b3d4afe066846` | 1→2 |

- 未绑定数：793 → **0**
- 绑定分布终态：ws1=1659、ws9=5（4+anchor）、ws10=57（4 迁移 + 2 并发期正常新建）、ws17=7（6+anchor）、1707=1
- 每卡一条 append-only 事件 `legacy_workspace_binding_attested` + `record_action_identity`；
  未改动任何 legacy task 行；request_id 形如 `legacy-bind-ws{X}-{n}-{tid后8位}`，幂等可重放

## 3. 复验（全部通过）

1. 跨 ws 读取双向拒绝：ws1 卡在 TokenSlim cwd、ws10 卡在 callwarden cwd、
   ws9/ws17 卡在 callwarden cwd 均返回 `E_TASK_WORKSPACE_SCOPE_MISMATCH`（绑定 ws vs 请求 ws 一致性校验）。
2. 同 ws 放行：各卡在正确 cwd 下正常 `task show`。
3. 全部 reviewer lease 已释放（ws17 旧 token 经 fencing 递增作废后以新 token 释放）。

## 4. 关键技术结论（实证获得）

1. **attest 的 lease 持有者必须是注册 role=reviewer 的 agent**：trae-adjudicator 持有
   reviewer lease 会被 `E_GOVERNANCE_REVIEWER_INVALID` 拒绝。正确形态 =
   trae-reviewer（sess-1793414579172-rev）持 lease + trae-adjudicator 作请求身份
   （role=adjudicator），与 adjudication 流程一致。
2. **`attest-legacy-workspace-binding` 子命令不支持 `--json`**。
3. **virgin workspace bootstrap 唯一通路 = RPC `task.create_from_plan`**（task_collab_planning.rs:478）：
   接受显式 workspace_id + workspace_instance_id，不经客户端
   `resolve_workspace_pair`、不经 `resolve_create_authority`，直接
   `bind_task_to_workspace` 为无 capture 的 workspace 建立首个 authority capture。
4. **客户端解析是硬门禁**：`resolve_workspace_pair_from_daemon` 要求 workspace.list
   active 行匹配 root 且 `workspace.status` 对账 registry_instance == task_db_instance；
   task-DB 侧无 capture/alias 时死锁。`task.create` / `task.split` 均绕不过
   （split 还要求父卡已绑定）；仅 `task.create_from_plan` 可。
5. **workspace 注册**：daemon RPC `workspace.register`（name + client_view_root）
   即时生效并颁发 canonical instance（形如 `3b5e466df3dab607`，非简单 sha256(root) 截断）。
   CLI `cw workspace register` 写的是 codegraph registry（数字 id，与 daemon
   registry 是两个命名空间，numeric id 会漂移，按 client_view_root 查询才稳定）。
6. **daemon 生产 registry 在 `C:\Users\wanpi\.callwarden\daemon\registry.db`**；
   `C:\Users\wanpi\.callwarden\registry.db` 是 pytest 残留（owner_uid=4294967295）。
7. ws9/ws17 若未来需新任务走常规 `task create`，现在客户端解析已可用
   （registry 行 + capture 均已就位，对账可通过）。

## 5. 审计与可追溯

- 批量 runner（支持断点续跑/错误收集/lease 失效中止）与全部审计产物在
  `C:\Users\wanpi\AppData\Local\Temp\cw_legacy_bind\`：
  `bind_plan.json`（783 条待绑清单）、`legacy_bind_evidence.json`（政策+排除明细）、
  `done.jsonl`（783 条逐卡结果，含 request_id）、`batch_attest.py` / `batch_ws9.py` / `batch_ws17.py`。
- 稳定错误码族：`E_LEGACY_BIND_*`（task_supersede.rs）；attest 治理门禁：
  ALREADY_BOUND / ANCHOR_UNBOUND / MISMATCH / FENCED 等，任何一步失败即拒绝，
  绝不部分提交。

## 6. 遗留事项与更正记录

- **双信号卡误绑更正（2026-11-14）**：`T-1786539379174-90f74174`（「M2.4：query.issues
  daemon RPC」）取证确认真实归属为 **ws1 (callwarden)**：卡内容为 callwarden 功能；
  首末两条真实操作（08-12 22:27/08-13 00:40）均归 ws1；ws10 记录（23:00~00:28）与
  ws9 close 记录（09:22）均为 `record_action_identity` 的 active-cwd 回退污染。
  绑定 append-only 无 unbind/rebind RPC，按用户决策维持 ws9 绑定现状，更正事件
  记录于 `C:\Users\wanpi\AppData\Local\Temp\cw_legacy_bind\legacy_bind_correction.json`。
  影响：该卡在 callwarden cwd 下不可读（fail-closed），但已 closed 终态，仅影响历史查询。
- evidence hash 更正：批跑动态计算并发送的 evidence 文件 hash 为
  `67a545206e40a7e8165fcc2d6384a4efbff42b454c720cd483a7e670f203398e`（已由
  task_operation_ledger 固化）；此前记录的 `3d99ec84…358e` 为 build_plan 阶段的
  canonical blob hash（非文件 hash），仅作历史脚注。
- ws17 worktree 属临时目录（`~/.codex/worktrees/`），若目录被清理，该 ws 后续
  常规 task create 的 authority capture 校验将失败；届时需重新评估。
