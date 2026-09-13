# 卡 C-08..C-09 承接卡 adjudicator 关闭证据：T-1789290073113-6808b1ac

- 结论：Adjudicator 接受独立 reviewer PASS verdict，apply + close 收口任务。
- Reviewer verdict：`V-afd3253b4af8144eb34c23c2`（blind_first_pass / pass / findings=0）
- 绑定 step：`S-1789290073115-68271b38`（step_index 2，最后一步 test）
- Snapshot：`a87ae6c43b7bb6cd`；workspace：1
- 证据 manifest：`deliverables/software-company/T-1789290073113-6808b1ac-evidence.md`
  `sha256:00f0bdc024732dc83e6f92ff9d6d68db1812ca79754be4c77cb50d960f5ac23b`

## 1. verdict provenance 校验（adjudicator 独立只读复核）

- `task_verdict_events` id=630 最新行：phase=`blind_first_pass`、overall=`pass`、
  step_id=`S-1789290073115-68271b38`、snapshot_id=`a87ae6c43b7bb6cd`、
  view_manifest_hash=`a0cedb2bf2fbccf94d94120cf8041be4860073878c63d055be760fe10f59db10`、
  workspace_id=1，contract_hash=`sha256:79bdf6d15037e1ebb65392e7e40b445cb0d8e53d6758a7caee884b9af28360a8`
  （Task Contract `TC-T-1789290073113-6808b1ac` revision 1），role_contract_hash=
  `sha256:3f691024970453f94a17d7ddc7a5845db2eaf19f1d25af9b8c9000272a6f8863`（reviewer lineage
  `rcl-T-1789290073113-6808b1ac-reviewer` r1），findings=`[]`、clause_results=`[]`。
- `view_manifest_hash` 经 MCP `get_role_view(task_id, role=reviewer)` 取得，符合
  `role-prompt-v1-gate-task-manifest.json:141` 规定的唯一受支持来源（不派生、不发明 RPC）。
- reviewer 盲审内容：逐行复核 `server/daemon_server.py` 工作树 diff ——
  ADMIN_ONLY_METHODS 新增 `mcp.backup_restore.backup_file`（与 Rust dispatch.rs:2254 对齐），
  并把注释行号从 `L545-564` 修正为 `L2233-2255`、显式说明 Rust 端为子集；
  `server/daemon_client.py` —— `SharedTaskWriterRequiredError.__init__` 改为
  `super().__init__(f"{self.code}: {message}", code=self.code)`，修复父类
  `DaemonUnavailableError.__init__` 的实例属性遮蔽（`self.code = code` 默认
  `E_HTTP_DAEMON_UNAVAILABLE`）；`tests/test_phase8_admin_rpc_authz.py` —— 常量断言
  14→15、新增 C-08/C-09 两组共 5 例。
  独立复跑回归 `tests/test_phase8_admin_rpc_authz.py tests/test_shared_task_writer.py
  tests/test_cli_079_http_rpc.py tests/test_cli_govfix07_daemon_error_rc.py` 共 **45 passed**；
  相邻面 `tests/test_c5_s4_backup_restore_unify.py tests/test_srv_003.py` 共 **30 passed**；
  `-k "C08 or C09"` 共 **5 passed, 28 deselected**；`git diff --check` clean；
  forbidden paths（`db/**`、`scripts/refresh_shared_runtime.ps1`）未触碰。
  只读核验 Rust 端 `rust_ext/src/daemon/dispatch.rs:2233-2255` 为 12 项，Python 端 15 项，
  `rust_methods <= set(ADMIN_ONLY_METHODS)` 成立，已知差集精确等于
  `build_context.{register,set_active,delete}`（`TestBatch11RustPythonAlignment` 断言），
  方向为 fail-closed（Python 更严格）。

## 2. apply/close 依据

- `review.state=passed`（verdict `V-afd3253b4af8144eb34c23c2`，findings_count=0）；
- 不触碰 apply/close 门禁语义，仅按正常收口路径调用 `task.apply` + `task.close`；
- 收尾使用 **reviewer role lease**（`L-73f7d7245da73216`，fencing_counter=2，raw token
  仅内存使用、未落盘、用后立即 release），identity 为 adjudicator（agent_id=
  `adjudicator-w17-wb-01` / session_id=`sess-c0809-adjudicate-20260914` / model_id=`workbuddy`，
  与 lease 行逐字一致），对应契约「adjudicator 自持 reviewer-role lease」的收口模式。
- 权威事件链：`task_events` 8821 `applied`（review→applied，role=reviewer）、
  8822 `closed`（applied→closed，role=reviewer）；`tasks.status=closed`，
  `applied_at=1789339951.7396514`，`closed_at=1789339959.4783063`。
- 前置 lease 链（`task_leases`，4 条全部 released）：`L-e6fd98616ca6d602`（role=implementer，
  counter=1，`executor-pytreg-01`，released）/ `L-968fac38522f6171`（role=implementer，
  counter=2，`executor-pytreg-01`，released）/ `L-ba7171dab60a149d`（role=reviewer，
  counter=1，`reviewer-w17-wb-01`，released）/ `L-73f7d7245da73216`（role=reviewer，
  counter=2，`adjudicator-w17-wb-01`，released）——与卡①/卡② 一致的 lease 递进（counter 递增为 2）。

## 3. 结论

任务 `T-1789290073113-6808b1ac`（C-08..C-09 承接：`server/` 授权清单与错误 code 契约修复）的
3 个 executor step 全部经独立 reviewer 盲审通过，adjudicator 复核一致后关闭。

- 3 个 step 的 report（均绑定权威 snapshot_id=`a87ae6c43b7bb6cd`、证据 sha256 同上）：
  step0 `S-1789290073114-681c093c`（首报 req-99be029cf510 event 8801；provenance 补齐
  req-1552c5387c68 event 8814）；
  step1 `S-1789290073114-6826601c`（首报 req-6301af57dfbc event 8806；补齐
  req-759d8b89c083 event 8815）；
  step2 `S-1789290073115-68271b38`（首报 req-2485e306a2ae event 8811 → review；补齐
  req-a3ce69068a19 event 8816）。
- executor handoff `executor_ready_for_review`（event 8817，request_id
  `hof-c0809-6808b1ac-exec-20260914-02`，report-request-id `req-a3ce69068a19`）；
  reviewer handoff `reviewer_pass`（event 8818，request_id `hof-c0809-6808b1ac-rev-20260914`）。

终态：`lifecycle_status=closed` / `workflow_status=completed` / `next_action=finalize` /
`review=not_in_review` / verdicts=1（`V-afd3253b4af8144eb34c23c2` overall=pass）。

## 4. 诚实披露（provenance 补齐与残余项）

1. 三个 step 首次 report 未携带 `--evidence-path/--evidence-hash`，导致 `task_events` 的
   `reported` 行 evidence 三元组为空，executor handoff 被
   `E_HANDOFF_REPORT_PROVENANCE_MISMATCH` fail-closed 拒绝（handoff 门禁要求 report 行
   三元组与 handoff 逐字非空相等）。处理方式：先 release 旧 implementer lease，用**新
   request_id** 逐 step 重新 report 补齐 evidence（event 8814/8815/8816），再以 step2 新
   report 行发起 handoff。未绕过门禁、未伪造 provenance。
2. `build_context.register/set_active/delete` 三项为 Python 端较 Rust 端的已知差集
   （Rust 侧为 `method_not_found` stub），方向为 Python 更严格（fail-closed），已由
   `TestBatch11RustPythonAlignment` 精确断言；两端同步时须同时更新该断言。
3. C-13（`semgrep_handlers` 未编译接线）/ C-14（`assignment_create` 缺 `assignment_id` 列）/
   C-15（`revoke` 强制 `task_id`）为 daemon 侧越界阻断，**不在本卡 allowed_paths 内**，
   未伪造成功，按前序 backlog 登记为后续承接项。
4. 报告幂等键为 `request_id`（不同参数复用同一 id 会触发 `E_REQUEST_ID_REUSE_MISMATCH`），
   故补齐 evidence 必须使用新 request_id；`E_TASK_GOVERNANCE_BLOCKED` 在存在 active lease
   时同样会拒绝 report，流程上须先 release 再 report。
