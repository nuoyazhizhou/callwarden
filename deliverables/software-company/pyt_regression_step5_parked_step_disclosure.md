# PYT 回归卡 Step#5 —— Parked remediation step 如实披露（`owner_route=planner`）

> 本文件是 `T-1788871227327-45c94bd8` / step#5 `fix_defect`（`step_id = T-1789293259780-5c3d5fd8`）
> 的验收证据与治理缺口披露载体。
>
> **协议依据（唯一单源）**：
> - [role-protocol.md §3「Parked remediation step（pre-cutover 无退出路径，如实披露）」](file:///c:/git_work/callwarden/.agents/skills/cw-task-loop/references/role-protocol.md#L129-L136)
> - [role-protocol.md §3「双轨整改」pre-cutover 桥接第 2 步](file:///c:/git_work/callwarden/.agents/skills/cw-task-loop/references/role-protocol.md#L115-L127)
> - [AGENTS.md 第 4 条（BLOCKED 双轨路由）](file:///c:/git_work/callwarden/AGENTS.md#L144-L153)
> - [cw-executor-senior-engineer SKILL.md 实施原则「先按 owner_route 复查」](file:///c:/git_work/callwarden/.agents/skills/cw-executor-senior-engineer/SKILL.md#L36-L39)

---

## 0. 结论

step#5 是对 step#4 已判定为 **`owner_route=planner`** 的计划缺陷的**机械重复派工**（daemon
`system_evaluator` 对 unresolved failed step 的派生结果）。按协议，Executor 复查确认后
**不得实施代码、不得完成该 step、不得把技术问题升级给用户**；本文件如实披露：

1. parked 状态与「重复派工≠新授权」；
2. 既有计划缺口事件与 finding_id 引用；
3. 所需 capability（内部 capability gap）；
4. 交 Planner 的精确可执行修订 scope 方向（Planner 裁决，非 Executor 自定验收）。

**本 step 未重做全量 sweep、未实施未冻结代码（forbidden_paths 改动 0 行）、未升级用户。**
该 step 按协议**保持未完成**——这正是 role-protocol §3 规定的预期状态。

---

## 1. 权威派工与复查结论

| 项 | 值 |
|---|---|
| `task_id` | `T-1788871227327-45c94bd8` |
| step#5 | `T-1789293259780-5c3d5fd8`（`step_index=5`，`action=fix_defect`，`target=tests/`） |
| 派工决策 | `decision=READY` / `action=CLAIM` / `required_role=executor` |
| 派工来源 | `routing.origin_kind=system_evaluator`，`reason=[「存在 unresolved failed step，唯一可领取目标为 remediation step T-1789293259780-5c3d5fd8」]` |
| assignment | `A-a60cc7ce484a96b2e7800c7a`（`source_request_id=req-b58dc52cd552`） |
| 授权 | `lease_role=implementer`、`lease_required=true`、`fencing_required=true` |
| 角色契约 | `rcl-T-1788871227327-45c94bd8-executor` / r1 / `sha256:9979cebf…`；skill `cw-executor-senior-engineer`；prompt `cw.aprime.executor.startup.v4`；`handoff_to=reviewer` |
| Task Contract | `TC-T-1788871227327-45c94bd8` / revision 1 / `sha256:2de09e59c693ea491650a4c7f8a5e091b62f47ea270f3e86d340371c02603436` |
| claim 结果 | `in_progress`（本 session 领取成功） |
| **复查结论** | **`owner_route=planner`** —— acceptance ② 在本卡冻结 `allowed_paths` 内结构性不可达，属计划/验收边界缺陷，非实现缺陷 |

不含 `allowed_paths` / `forbidden_paths` 的判定变更（与 step#4 合同逐字一致）：

- `allowed_paths = ["deliverables/software-company/**", "tests/**"]`
- `forbidden_paths = ["cli/**", "db/**", "rust_ext/src/**", "rust_ext/src/daemon/**", "scripts/refresh_shared_runtime.ps1", "server/**"]`

---

## 2. 复查依据（引用既有事件，不重做）

role-protocol §3 明确「Executor 重复领取时应**直接引用既有计划缺口事件与 finding_id**，
继续走内部 capability 修复，**不得重做**」。故本 step 不重跑全量 sweep，只做 HEAD 级复核。

### 2.1 前序 failed step（daemon 权威 `work_order.prior_attempts`）

| step | `step_id` | action | status |
|---|---|---|---|
| step#3 | `S-1788871227330-45f32070` | `rerun_full_pytest_and_record` | failed |
| step#4 | `T-1789139378194-02f1f66c` | `fix_defect` | failed |

### 2.2 step#4 的全量定界（本 step 继承的权威证据）

| 项 | 值 |
|---|---|
| evidence path | `deliverables/software-company/pyt_regression_step4_acceptance_evidence.md` |
| evidence sha256 | `80a5d5225573c52b1412e2ce2729f29204e0f01ed32bd9ed6bf1eb9038f55cd8` |
| `report_request_id` | `req-b58dc52cd552` |
| 结论 | `result=failure`；acceptance ①③④ 达标、② 未达标但已**全量定界**且**零退化** |
| 落地 commit | `bfc88313b56641a0ce8c2abba30f04b86c724e3b`（前缀 `[T-1788871227327-45c94bd8]`） |

定界摘要（逐文件对照旧基线，无绿转红）：
`failed 541 → 28`（−94.8%）、`error 156 → 8`（−94.9%）、`rc≠0 文件 129 → 28`；
28 个 `rc≠0` = **C 桶 8** + B 桶环境/数据态 6 + 环境锁 7 + `rc=5` 无用例 7。

### 2.3 HEAD 源码级复核（本 step 新增，确认 C 桶 finding 仍成立）

| finding | 落点（HEAD 实测行号） | 复核结果 |
|---|---|---|
| C-04 | [cli/main.py:3448](file:///c:/git_work/callwarden/cli/main.py#L3448) `for t in trend[:20]:` | 循环变量 `t` 仍遮蔽 i18n 函数 `t`（同作用域内 `t("cli.messages.churn_trend_item", …)` 被遮蔽）→ 成立 |
| C-10 | [cli/main.py:2559-2563](file:///c:/git_work/callwarden/cli/main.py#L2559-L2563) `for r in rules:` + `r["id"]` | 仍按裸 list 解包，未解 MCP-061 信封 `{"rules":…,"count":n}` → 成立 |
| C-11 | [cli/main.py:14892](file:///c:/git_work/callwarden/cli/main.py#L14892) `UnixDaemonRpcClient(socket_path=get_default_daemon_endpoint())` | `_agent_start` 仍未迁移 HTTP thin-client → 成立 |
| C-12 | [cli/main.py:15056](file:///c:/git_work/callwarden/cli/main.py#L15056) 同上 | `_agent_status` 仍未迁移 → 成立 |

四个落点**全部位于 `cli/**`（forbidden_paths）**，本卡无权修改。

---

## 3. 计划缺口：acceptance ② 与冻结 scope 不相容（`owner_route=planner`）

**acceptance ② 原文（daemon `work_order.acceptance_checks[1]`，逐字）**：

```text
python -m pytest tests/ -n auto --tb=short --maxfail=10 --timeout=300 --timeout-method=thread 零失败
```

**冲突证明**：

| 维度 | 事实 |
|---|---|
| 剩余 `rc≠0` 文件 | 28（step#4 全量定界，零退化） |
| 其中**真实生产缺陷** | C 桶 8 文件 / 17 项（15 failed + 2 error） |
| C 桶落点 | `cli/**`（C-04/C-05/C-06/C-10/C-11/C-12）、`server/**`（C-08）、`rust_ext/src/daemon/**`（C-03） |
| 本卡可修改集合 | 仅 `tests/**` + `deliverables/software-company/**` |
| 结论 | **不存在**只改 `tests/**` 即可让 C 桶 8 文件转绿的路径：这些用例断言的是**期望行为**，生产实现未达标；改测试只能造假绿 |

其余 20 个 `rc≠0` 亦不构成「本卡可实现零失败」的理由：

- **环境锁（7）**：运行中生产 daemon（pid 21012，`127.0.0.1:1615`）独占
  `rust_ext/target/release/cw-daemon.exe` → fixture 内 `cargo build` 报
  `拒绝访问。 (os error 5)`。已由 `test_windows_bridge_e2e.py` 900s 重跑**全绿（14 passed）**证伪，
  属环境前置（需停生产 daemon），非代码缺陷。
- **B 桶环境/数据态（6）**：隔离 daemon 未发布 manifest（2 文件 6 error）、
  registry↔task-DB 配对数据态（4 文件 13 failed），属 harness/环境可见性，非本 step 未完成的整改。
- **`rc=5`（7）**：`no tests ran`（文件内无测试函数）。单文件 `rc=5` 在**全量单次运行**中不产生失败，
  仅是本机逐文件 sweep 驱动的伪影，不进入 acceptance ② 的失败口径。
- **`-n auto` 本身**：`prior_attempts` step#3 记录 15 次 `node down: Not properly terminated`
  （xdist worker OOM），合同该条在当前机器上**不可稳定执行**（见
  [pyt_regression_step4_executor_escalation.md §5.1](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_executor_escalation.md#L86-L95)）。

> 即：step#4 已把 ② 的残余面**全部**推到「跨 scope 生产缺陷」+「环境前置」两类，
> 本 card 的 `allowed_paths` 内**已无可闭合的失败项**。继续在同一 step 口径下重复整改，
> 只会在相同结论上循环。

---

## 4. finding_id 与承接关系（既有台账，不新建）

C 桶 finding 台账见
[pyt_regression_step4_handoff_backlog.md W13/W14](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_handoff_backlog.md)；
逐条根因/复现见
[pyt_regression_step4_acceptance_evidence.md §3.1](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_acceptance_evidence.md#L135-L154)。

| finding | 复现文件 | 落点 | 承接卡（已建，daemon 权威） |
|---|---|---|---|
| C-03 | `tests/test_wsl_local_daemon_e2e.py` | `rust_ext/src/daemon/{transport,http_server,daemon_autostart_handlers}.rs` | `T-1789290072972-5fad5b5c` |
| C-04 | `tests/test_cli_020_http_rpc.py` | `cli/main.py:3448` | `T-1789290073049-6442e268` |
| C-05 | `tests/test_cli_066_http_rpc.py` | `cli/main.py:7701` | `T-1789290073049-6442e268` |
| C-06 | `tests/test_cli_011_http_rpc.py` | `cli/main.py:17685` / `:17702` | `T-1789290073049-6442e268` |
| C-08 | `tests/test_phase8_admin_rpc_authz.py` | `server/daemon_server.py:252-275` | `T-1789290073113-6808b1ac` |
| C-10 | `tests/test_cli_057_http_rpc.py` | `cli/main.py:2559-2563` | `T-1789290073049-6442e268` |
| C-11 | `tests/test_cli_005_http_rpc.py` | `cli/main.py:14892` | `T-1789290073049-6442e268` |
| C-12 | `tests/test_cli_006_http_rpc.py` | `cli/main.py:15056` | `T-1789290073049-6442e268` |

**计划缺口 finding（本 step 的 `owner_route=planner` 主体）**：无独立 finding_id——
它由 step#4 的 `result` 文本与 `report_request_id=req-b58dc52cd552` 承载（daemon `prior_attempts`
逐字保留）。本文件是其**可哈希的独立证据载体**，供 Planner/Reviewer 引用。

---

## 5. 所需 capability（内部 capability gap，登记备后续实现）

按 role-protocol 顶部 capability 分层声明，本 card 的缺口是**协议已设计但 daemon 未声明**的能力：

| capability | 状态 | 本卡因此缺失的能力 |
|---|---|---|
| `planner_governance_v1` | **未声明** | 无 `READY/PLAN`、无 `planning_*`/`replanning_*` 投影；`task.create`/`--role-contracts` 拒绝 `planner` 角色；`replan` 路由不可用 |
| （outcome 层）`executor_replan_requested` | **design-only** | pre-cutover 发送会被 daemon 结构化拒绝（fail-closed，预期行为），**不得**客户端本地补持久化 |

**退出路径分析（与 role-protocol §3 parking 段逐字一致）**：

| 候选退出路径 | 是否可用 |
|---|---|
| 完成 step#5 并 `step-resolve` | ✗ 完成即意味着实施未冻结代码 / 造假绿 |
| `executor_replan_requested`（executor→planner） | ✗ design-only，daemon 结构化拒绝 |
| `executor_blocked_to_user`（executor→user） | ✗ 协议禁止把技术计划缺陷升级用户；不得用其伪装客户阻塞 |
| `reviewer_blocked`（reviewer→executor） | ✗ 本角色非 reviewer；且 `from_role` 校验拒绝 |
| 手工创建整改 step / 通用 RPC | ✗ 协议明令禁止（`§4`「不得改用通用 RPC、伪造整改 step」） |
| **保持 parked + 内部治理缺口登记** | ✓ **当前唯一合法路径（本文件）** |

> 因此 step#5 **保持未完成属预期状态**；`workflow_status_for()` 对
> `in_progress + revise_current_step` 仍投影 `remediation_in_progress`（可派工），
> **重复派工不构成新授权**。parked step 的 daemon 终止/取消语义列入
> v2 amendment §3.3 未实施清单。

---

## 6. 交 Planner 的精确可执行修订 scope（**方向建议**，Planner 裁决）

> 按 AGENTS.md「不得把 finding 扩写为 Executor 才有权制定的新 scope、验收或 capture 方案」，
> 本节仅为**缺口描述 + 最小修订方向方向建议**，**不构成本卡的验收口径**，最终裁决权在 Planner。

**缺口定义**：Task Contract revision 1 的 acceptance ② 把「全量零失败」设为单卡验收目标，
而其失败源**结构性落在该卡 `forbidden_paths`**。这在冻结时即应被复杂度预检拦截
（role-protocol §3：「单个 Executor 无法在原 scope 内安全修复」即默认拆分或重规划）。

**可供 Planner 选择的修订方向**：

| 方向 | 内容 | 影响 |
|---|---|---|
| **P-A（推荐）** | 把 acceptance ② 改为**分层口径**：本卡范围 = `tests/**` 内测试全绿且相对基线**零退化**；跨 scope 生产缺陷由已建的 C 桶承接卡（`T-1789290072972-5fad5b5c` / `T-1789290073049-6442e268` / `T-1789290073113-6808b1ac`）分别验收 | 合同修订留痕、历史不动；本卡可立即进入 review |
| P-B | 按 A/B/C 三桶拆分子卡，本卡仅承载 `tests/**` 可闭合部分 | 需新建子卡与依赖边 |
| P-C | 若坚持「全量零失败」为唯一口径：须同时 (a) 将 `cli/**`、`server/**`、`rust_ext/src/daemon/**` 纳入 allowed_paths，(b) 提供稳定隔离 daemon/manifest harness 与停止生产 daemon 的环境前置，(c) 解决 `-n auto` 的 xdist OOM | 三处均为跨角色/跨卡授权，超出本卡 |

**不建议路径**：把 C 桶 8 文件在 `tests/**` 内改成 `xfail`/`skip` 以凑「零失败」——
该做法使缺陷检测静默失效，与 `AGENTS.md`「不得修改旧 verdict/证据来补绿」同源被禁。

**后续内部维护路径**：本缺口（`planner_governance_v1` 未声明 → 无 replan 路由 →
remediation step 无退出路径）应登记为内部 capability/governance gap，交
Planner/治理维护路径补齐；非用户可解事项。

---

## 7. 合规声明（本 step 的实际动作边界）

| 项 | 事实 |
|---|---|
| 是否重做全量 sweep | **否**（role-protocol §3「不得重做」） |
| 是否实施未冻结代码 | **否**——`git log --grep=<本卡 id> -- cli server db rust_ext scripts` 命中 0；本 step 仅新增本文件（`deliverables/software-company/**`，allowed） |
| 是否触碰 `tests/**` | 未改动任何测试（C 桶缺陷在生产侧，改测试即造假绿） |
| 是否升级用户 | **否**（未使用 `executor_blocked_to_user`；技术/计划缺陷不交用户） |
| step#5 终态 | **保持未完成**（parked，预期状态） |
| 本文件落点 | `deliverables/software-company/`（`allowed_paths` ∩ `required_evidence`） |

---

## 8. 复现与引用命令（环境口径）

```powershell
$env:PYTHONPATH="C:\git_work"; $env:CW_TEST_MODE="1"; $env:NO_PROXY="127.0.0.1,localhost"
$env:CW_DAEMON_HTTP_ENDPOINT="http://127.0.0.1:1615"
# 隔离 HOME（绕过真实 HOME 的 stale manifest，防 E_HTTP_MANIFEST_STALE）
$env:USERPROFILE="$env:LOCALAPPDATA\Temp\cw_pyt_step4_home"; $env:HOME=$env:USERPROFILE

python cw.py task next-action T-1788871227327-45c94bd8 --workspace-instance-id 4baea3ff12c2ea5c --json
```

权威 workspace pair：`workspace_id=1` / `workspace_instance_id=4baea3ff12c2ea5c`。
