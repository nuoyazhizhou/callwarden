# RP-10 Handoff Summary（接续 Agent 必读）

> 生成时间：2026-09-07 15:40（GMT+8）
> 当前状态：**RP-10 卡执行中（step 2 已部署但未回报），全角色闭环未完成**
> 接续任务：完成 step2 收尾 → step3 → Reviewer → Adjudicator 全循环

---

## 1. 你正在做什么（上下文）

Role Prompt Compiler v1 系列最后一张卡 **RP-10（cross-layer E2E security and release gate）**，
idempotency key `role-prompt-v1-rp10`，parent `T-1788668721548-b8c6f4dc`，
predecessor RP-09 `T-1788737615420-538057d4`（closed/COMPLETE）。
冻结权威：`docs/design/cw-role-prompt-compiler-v1-frozen-spec.md`（SHA 95298729…CB7）。
前序卡 RP-00~RP-09、RP-06-cli 全部 closed。

**本卡治理任务 ID：`T-1788753260274-ed7e21d4`**
步骤：
| idx | step_id | action | 状态 |
|----|----|----|----|
| 0 | S-1788753260275-ed91cc98 | run_live_parity_e2e | **done**（report req-01299ce85be3）|
| 1 | S-1788753260276-ed9320d4 | run_full_regression_and_security | **done**（report req-bf3def31322e）|
| 2 | S-1788753260276-ed9378b8 | perform_single_controlled_deploy | **in_progress（部署已执行成功，未回报）**|
| 3 | S-1788753260276-ed93abe4 | freeze_release_evidence | pending |

治理身份（已注册，HTTP agent.register）：
`executor-wb-rp10-01`（implementer / inst-exec-rp10-01 / sess-exec-wb-rp10-20260907）、
`reviewer-wb-rp10-01`（independent_reviewer / inst-rev-rp10-01）、
`adjudicator-wb-rp10-01`（adjudicator / inst-adj-rp10-01）、
备用 `reviewer-wb-rp10-02`（role=reviewer 精确槽 / inst-rev-rp10-02，未用）。

## 2. 当前权威状态（刚核实）

- **新部署 daemon**：PID **32492** @ `http://127.0.0.1:6217`（动态端口，**9365 已废弃**），
  worker healthy，schema 60，`git_commit = ba6e9aae…`（= 部署 HEAD）。`cw.py daemon health` / 客户端自动发现可用。
- **部署 receipt（step2 已成功的证据）**：
  `C:\Users\wanpi\.callwarden\runtime\evidence\20260907-151430-ba6e9aae326a-4354a6e2.json`
  status=passed；git_head ba6e9aa；cw-daemon.exe sha256 `6fac603527…a1cc1`（runtime/current 已切换）。
- **live probe 已验证（2026-09-07 15:50 实测）**：对 closed 卡 RP-09 `T-1788737615420-538057d4`：
  - HTTP RPC `task.prompt.compile` → `role_prompt_bundle_v1 / prompt_kind=terminal`，
    bundle_hash `sha256:fa1c9a4a…`（正向通到 daemon 编译出口）；
  - CLI `cw.py task prompt … --format llm` → **实测 fail-closed** `E_TASK_PROMPT_TASK_ID_REQUIRED`
    （request 含未知字段 workspace_instance_id，spec §4.1-3）= ADJ-RP10-01 行为已固化，正/负双证据齐备。
- git HEAD：`ba6e9aa`（RP-09 台账提交）。
- 工作树脏文件（并行 agent 所有，**勿碰勿提交**）：
  `rust_ext/src/daemon/{cas_merge,config,fs_handlers,snapshot_state}.rs`、
  `server/tools/tools_query.py`、`deliverables/software-company/server_authority_residue_audit.json`。

## 3. 待接续动作（按序执行）

### A. step2 收尾（先做）
1. 补齐 live round-trip 记录（Read-ONLY，全部实测）：
   - `python cw.py daemon health`（PID/git_commit/worker）；
   - HTTP RPC：`task.prompt.compile` 打一张 closed 卡（建议 RP-09 `T-1788737615420-538057d4`，已验证 terminal OK）
     与一张 open/in_progress 卡（RP-10 自身 `T-1788753260274-ed7e21d4` → 应为 role_work/executor READY/CLAIM，
     **注意**：本卡合同如带裸 64-hex prompt_hash 会被 §8.3 redaction 拒绝——记录实际行为即可，勿扩 scope 修）；
   - CLI：`python cw.py task prompt <task_id> --format llm|card|json` → **已实测**（2026-09-07 15:50）
     受 ADJ-RP10-01 影响 fail-closed（`E_TASK_PROMPT_TASK_ID_REQUIRED`），负向证据已录，无需重测；
   - MCP 面与 CLI 同源（route_rpc），同 fail-closed，可在 evidence 中引用 E2E 固化用例。
2. `task report` step2（**带全三件套**：身份参数 + `--evidence-path docs/evidence/RP-10-role-prompt-compiler.json` + `--evidence-hash <最终冻结 SHA>`；
   若 evidence 尚未写，可先以 receipt 文件为 path/hash 或先写 evidence 骨架再回填——推荐：先做 D 的提交与 evidence，再补报 step2+step3 双 report（参照 RP-09 流程：step3 是最后 verify 步，先 report step2 也可以但 hash 会变，**建议顺序 = B/C/D → 提交代码 → 写 evidence → 回报 step2 → 回报 step3 → backfill**）。

### B. 提交两个新测试文件（白名单，未提交！）
```
git reset -q && git add tests/test_task_prompt_e2e.py rust_ext/tests/role_prompt_e2e.rs
git commit -m "RP-10: live parity E2E ... (task T-1788753260274-ed7e21d4)"
```
- `tests/test_task_prompt_e2e.py`（6/6）：隔离 temp daemon + HTTP/CLI/MCP parity + fail-closed；
- `rust_ext/tests/role_prompt_e2e.rs`（4/4）：全链 create→contract→context→route→compile + hash 闭包 + 确定性 + 只读 + fail-closed。
- 验收快照：Python E2E 6/6、Rust E2E 4/4（`--no-default-features` 下亦 4/4）、purity 0 违例、git diff --check 干净。
- 注意合同 prompt_hash 必须是 `sha256:` 前缀形式（spec §9.3），裸 64-hex 会被 redaction 拒。

### C. step3 freeze_release_evidence
- 写 `docs/evidence/RP-10-role-prompt-compiler.json`（allowed path；required_fields：
  task_id / step_ids / contract_hash / workspace_binding / commands / positive_results / negative_results /
  changed_paths / commit_sha / deployment_receipt_if_applicable）；**不含任何 credential/lease token**。
- contract_hash 取 `role_contract_revisions` 完整 64-hex（rcr-T-1788753260274-…）；workspace binding = `1 / 4baea3ff12c2ea5c`。
- 冻结后 `task report` step2+step3 并 backfill report_request_id（参照 RP-06-cli/RP-09 流程）。

### D. handoff → Reviewer → Adjudicator（完整闭环后才算 RP 系列完结）
- executor handoff：`_rp10_handoff.py`（短 TTL + lease 到期自轮询；report 事件三元组必须带全）。
- reviewer fresh-run：重跑 `pytest tests/test_task_prompt_e2e.py -q` + `cargo test --test role_prompt_e2e`
  + purity + evidence SHA 核对 + receipt closure（health.git_commit==HEAD、sha256 匹配）；
  双槽 lease verdict PASS/BLOCKED。**verdict 必须把 ADJ-RP10-01 与 892 pre-existing 失败纳入 findings 判定**。
- adjudicator：apply/close → 追加 `cw_task_commit_ledger.json`（t1788753260274_rp10_20260907）→ 更新工作日志。

### E. 遗留（需另行开卡，勿在本卡内修）
- **ADJ-RP10-01**：`server/daemon_client.py` `route_rpc` 对 task-scoped 方法仍无条件
  `params.setdefault("workspace_root", …)`（在 task-scoped 判别之前）→ daemon `parse_request` §4.1-3
  拒绝未知字段 → CLI/MCP 面 `task.prompt.compile` fail-closed `E_TASK_PROMPT_TASK_ID_REQUIRED`。
  修复 = workspace_root 注入纳入 task-scoped 豁免（同 `_is_task_scoped_authority_request` 判别）。
  修复后 RP-10 正向 CLI/MCP parity 测试可在 remediation 卡解锁（E2E 已把 fail-closed 行为固化）。
- pytest 全量 892 失败/3 collection 错误 = pre-existing 基线债（daemon 状态依赖族），非本卡引入。

## 4. 环境事实与坑（接续者直接用）

- **daemon 保活**（bash 后台前台 exec，勿后台嵌套）：
  ```
  CW_COMPAT_PYTHON=C:/git_work/callwarden/.venv_test/Scripts/python.exe \
  PYTHONPATH=C:/git_work CW_DAEMON_HTTP_BIND=127.0.0.1:<port> \
  exec C:/Users/wanpi/.callwarden/runtime/current/cw-daemon.exe --config C:/Users/wanpi/.callwarden/daemon_manual.json
  ```
  正斜杠 CW_COMPAT_PYTHON（反斜杠会被 bash 吃掉 → compat worker program not found）。
- **named pipe `UnixDaemonRpcClient` 当前不可连**（本会话实证）→ 身份注册/治理 RPC 全部走
  `HttpDaemonRpcClient(endpoint=http://127.0.0.1:6217).call("agent.register"/…)`，可行（与旧记忆"仅 Unix 入口"不符，待修订）。
- `cw task prompt` 是 READ_ONLY task-scoped 薄客户端（RP-06-cli 交付），`--format` 只改本地展示。
- Rust 构建：`source scripts/msvc-env.sh && unset INCLUDE && export CARGO_INCREMENTAL=0`，在 `rust_ext/` 下执行。
- pytest 全量：**必须停共享 daemon 再跑**（运行中会干扰隔离 daemon 测试族），~70 分钟。
- 身份/step 复用脚本：`_rp10_create_task.py`、`_rp06cli_handoff.py`/`_rp09_handoff.py`、
  `_rp09_reviewer_verdict.py`、`_rp09_adjudicator.py` 为模板。

## 5. 最近关键 commit（供台账引用）
- `ba6e9aa` = RP-09 ledger（当前 HEAD；部署 receipt 指向它）
- RP-10 代码 commit = 接续后由你产生（两个测试文件）
