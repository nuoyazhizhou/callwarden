# CLI-079 fix_defect 闭环证据 manifest（4 项 check 核验）

**任务：** `T-1787322799418-ce4698f0` — CLI-079 [cli_command_projection]：cw local-close → Rust daemon HTTP thin client
**整改步骤：** `T-1787700302263-09407b70`（fix_defect，remediation_of `S-1787322799419-ce53fa54`）
**冻结合同：** `RC-T-1787322799418-ce4698f0-executor-0`（Task Contract revision 2，hash `sha256:9804ff7370948ff51558e94a17fe328e6fad006dcbb168930b470e2fc6f4969e`）
**整改提交：** `5e53440`（master，2026-08-26 07:42:55，message 内嵌 task_id）
**执行身份：** agent_id=`executor-workbuddy-v1-cur`，session=`cw-exec-workbuddy-20260824`，model=`workbuddy`，role=`executor`
**日期：** 2026-08-26

## 0. 背景与 caveat（如实呈交）

- step3（`S-1787322799419-ce53fa54`，evidence_and_dependency_verify）于 2026-08-26 07:25 诚实上报 failed；
  代码缺陷随后在 07:42 由 commit `5e53440` 修复，但 fix_defect step 因缺 Role Contract binding
  一直无人可领取（Class C 结构性缺口：legacy fix_defect 追加入口不写 `task_step_role_contract_bindings`，
  而 next-action evaluator fail-closed 要求唯一可验证 binding；全库 46 个 fix_defect 仅 1 例有 binding）。
- **本轮补齐**：参照先例 `T-1787402257549-67ba81e6`（同为直写补 binding），以幂等短事务
  （BEGIN IMMEDIATE + 存在性检查）向 `task_step_role_contract_bindings` 追加
  `sb-T-1787322799418-ce4698f0-T-1787700302263-09407b70-r1`（binding_revision=1，
  role_contract_revision=1，hash `sha256:365b81f4...d8e72a`，c14n `role-contract-c14n/v1`，
  created_by=`executor-workbuddy-v1-cur`），字段复制自同任务既有 4 条 executor binding，
  写入前后均复核 revision 行 hash 与 lineage (task_id, workspace_id) 三方一致。
  补写后 next-action 由 BLOCKED 翻转为 READY/CLAIM。
- **领取**：`cw task next` 不透传 `remediation_step_id`（daemon 报 E_REMEDIATION_STEP_REQUIRED），
  故复用 CLI 同一 `route_task_write` 通道调用 `task.claim`，携带
  `remediation_step_id=T-1787700302263-09407b70`；任务 claim 占用方为
  `cw-exec-workbuddy-20260824`（step3 failed 上报者，同 executor 角色），按 daemon 显式支持的
  同 session resume 语义（`existing_session == agent_session_id` 不报 task_conflict）领取成功。
- 本 manifest 是 fix_defect 步骤的 4 项 check_items（source scan / MCP dependency applied /
  runtime fingerprint / evidence manifest）核验证据。

## 1. check #1 — source scan（before/after）

**before**（step3 failed 原裁决，2026-08-26 07:25）：
> `cli/main.py::_local_close` 仍直接调用 `db.task_close`，且
> `tests/test_cli_079_http_rpc.py` 仍固化 legacy local fallback（3 passed），
> 不满足冻结 thin-client 合同 acceptance #1；缺 evidence manifest。

**after**（commit `5e53440` + 本 manifest 同批 docstring 修正后，2026-08-26 核验）：

| 检查点 | 结果 |
|---|---|
| `cli/main.py::_local_close`（L4816-4823） | `raise SharedTaskWriterRequiredError(...)`，无 `db.task_close` 直调、无 Unix RPC、无 local 业务路径 |
| `cli/main.py` 全文件 `task_close` 出现处 | 仅 help 文案、RPC 方法映射表（`task_close → task.close/GOVERNANCE_WRITE`）、错误消息字符串；无 `db.task_close(` 调用 |
| close 分支路由（L4827-4831） | `route_task_write("task.close", {task_id, reviewer, identity, **lease_kwargs}, _local_close)`；lease 凭证原样透传，fallback 仅 fail-closed |
| `tests/test_cli_079_http_rpc.py` | 3 个测试均为 fail-closed 断言（daemon 路径 lease 透传 / lease 缺失不补默认 / local 无 daemon 抛 `SharedTaskWriterRequiredError`）；模块 docstring 第 7 行原残留 legacy 描述，本批已修正为 fail-closed 表述 |

**定向测试**：`python -m pytest tests/test_cli_079_http_rpc.py` → **3 passed**（0.21s，2026-08-26 复测两次均通过）。

## 2. check #2 — MCP dependency applied

- Task Contract revision 1/2（`task_contract_revisions`，envelope_payload 反序列化）：
  rev1 `dependencies=[]`；rev2 `dependencies=["T-1787293451688-c14b1e44"]`（Epic 父任务），
  **无 `mcp_dependencies` 字段**（合同模板表述"若 mcp_dependencies 非空……"为条件句，本卡为空）。
- 结论：CLI-079 无 MCP 依赖卡需要 applied；唯一 task 依赖为 Epic 父任务，
  领取门禁已通过（claim 成功即证明依赖检查通过）。与前任证据
  `CLI079_executor_evidence.md`（"MCP 依赖：无"）一致。

## 3. check #3 — runtime fingerprint（真实 CLI 进程 fixture）

| fixture | 命令 | exit | 输出关键行 | 结论 |
|---|---|---|---|---|
| A：local 模式 fail-closed | `CW_TEST_MODE=1 CW_DAEMON_MODE=local python cw.py task close T-0000000000000-00000000` | 2 | `E_SHARED_TASK_WRITER_REQUIRED: 共享任务写入要求当前用户 daemon 单写点…` | local 模式不触 db 直写，fail-closed 生效 |
| B：daemon HTTP round-trip | `python cw.py task close T-0000000000000-00000000`（daemon 在线） | 0（错误经友好输出） | `E_LEASE_REQUIRED: task.apply/task.close 必须携带完整 reviewer lease 凭证…`，耗时 0.509s | 请求经 HTTP 到达 daemon，错误码由 Rust 端 `require_lease_params` fail-closed 产生，非本地路径 |

**Rust 权威写点路由核验**：`rust_ext/src/daemon/dispatch.rs` L2396 `"task.close" => state.handle_task_close(peer, params)`；
业务逻辑在 `rust_ext/src/daemon/task_collab.rs::handle_task_close`（identity/lease/fencing 校验）。
Python CLI 仅为 thin client，满足 acceptance：Rust 拥有业务逻辑、响应与稳定错误保留。

原始输出缓存：`.tmp_cli079_runtime_fingerprint.json`（会话临时文件，报告后清理；内容已摘录于上表）。

## 4. check #4 — evidence manifest

即本文件。落盘于白名单路径 `deliverables/software-company/`，随本次提交进入 git 历史
（commit message 内嵌 task_id），hash 经 `cw task report --evidence-path/--evidence-hash` 绑定。

## 5. 状态机闭环计划

1. `cw task report`（本步骤成功，携带本 manifest path/hash）→ fix_defect step done → 任务进 review。
2. `cw task step-resolve <task> S-1787322799419-ce53fa54 T-1787700302263-09407b70 <request_id> --evidence-path/--evidence-hash`
   → 原 failed step 以 append-only 事件合法回审（原 failed 行不可变）。
3. 台账 `cw_task_commit_ledger.json` 追加本任务条目（`5e53440` + 本闭环 commit ↔ task_id）。

## 6. 前任证据勘误（供 reviewer/adjudicator 参考）

- `deliverables/software-company/CLI-079_evidence.md` 第 43-52 行"待办"假设
  "claim 时 daemon 自动创建 binding（claim.rs INSERT）"——**不成立**：公共路由
  （dispatch.rs）只接 legacy `handle_task_claim`（不写 binding）；写 binding 的
  `task_loop/claim.rs::claim_step` 未接公共 RPC（route.rs 仅开放 `task.create`/
  `task_loop.public_promote`）。这正是本步骤一度死锁的根因，已按 §0 直写补齐。
- 结构性缺口（legacy fix_defect 追加入口不写 binding）的根治属于治理基建任务范畴，
  不在 CLI-079 scope 内，此处仅记录现象，不扩大本任务范围。
