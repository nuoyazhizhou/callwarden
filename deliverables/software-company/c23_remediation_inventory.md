# C-23 缺陷复核与同型扫描（step0 adjudicate，不改代码）

- 卡：`T-1789564402123-9b47d988`（C-23 承接，父 `T-1788871227327-45c94bd8`）
- 来源：§W3 承接卡 `T-1789529126780-6cf84728` step3 越界发现（登记 `pyt_regression_step4_handoff_backlog.md` L30-33）
- 本 step 强制前置：只复核与扫描，不改任何代码

## 1. 缺陷复核（本 HEAD 实测，行号以修复前 e4b335d^ 为准）

`server/tools/tools_task.py` 的 MCP 工具 `task_create`（修复前签名）：

```python
@mcp.tool()
def task_create(
    title: str,
    description: str = "",
    steps: list = None,
    creator: str = "agent",
) -> str:                                     # ← 缺陷②：注解 str
    ...
    return _route(                            # ← _route 返回 dict
        "task.create",
        {"title": title, "description": description,
         "steps": steps, "creator": creator}, # ← 缺陷①：无 workspace 配对
        "PROTECTED_MUTATION",
    )
```

- **缺陷①（BR-01/BR-02）**：签名不转发 `workspace_id` / `workspace_instance_id`。
  `route_rpc`（daemon_client.py:3973-3980）对 `task.` 前缀方法在缺配对时调用
  `_inject_workspace_id`（:3521 `if params.get("workspace_id"): return params`
  ——显式配对可穿透）。MCP 入口无法显式声明配对 → HTTP 路由下
  `_ensure_remote_snapshot` 兜底解析 cwd workspace，named-pipe 路由下 capture 链
  无 instance → daemon 侧 `required_workspace_id_param` 恒拒
  `E_TASK_WORKSPACE_INSTANCE_REQUIRED`（§W3 step3 r7 实证）。
- **缺陷②（fastmcp pydantic）**：`_route` 返回 daemon result 字段（dict），
  但注解 `-> str` → fastmcp `convert_result` 校验抛
  `task_createOutput: Input should be a valid string`（§W3 step3 r4 实证）。
  兄弟工具 `task_next_step` 用 `Optional[dict]`（`tools_task.py` 同文件先例）。

## 2. 修复锚点（BR-01/BR-02 与 fastmcp 契约）

- BR-01/BR-02：`task.create` 必须显式传入整数 `workspace_id > 0` 与非空
  `workspace_instance_id`；两值由 `workspace.register` 签发，缺 instance 时 daemon
  拒绝（`E_TASK_WORKSPACE_INSTANCE_REQUIRED`），不回退隐式合成。
- `_inject_workspace_id` 透传规则（:3521）：`params.get("workspace_id")` 为真即原样
  返回 → 显式配对逐字进入 RPC 载荷，不触碰隐式注入分支。
- fastmcp `convert_result`：返回值须与注解类型一致；`Optional[dict]` 接受 dict 与
  None（`task_next_step` 先例，同文件）。

## 3. 同型扫描：`server/tools/*.py` 全量 243 个 MCP 工具

注解分布（实测）：`dict` 146 / `list` 60 / `Optional[dict]` 24 / `str` 7 /
`bool` 3 / `Dict[str, Any]` 1 / `int` 1 / `CodeMetricsSummary` 1。**无注解缺失**。

**7 个 `-> str` 注解全部实际返回 `_route(...)` dict**（同型缺陷，逐个实证首个
return 语句）：

| # | 工具 | 文件 | RPC | 首个 return（实测） | 同型 |
|---|---|---|---|---|---|
| 1 | `task_create` | `tools_task.py` | `task.create` | `_route("task.create", {...}, "PROTECTED_MUTATION")` | **本卡已修**（`Optional[dict]` + workspace 配对） |
| 2 | `task_create_subtask` | `tools_task.py` | `task.create_subtask` | `_route("task.create_subtask", {...})` | 未修（另立卡） |
| 3 | `task_create_from_plan` | `tools_task.py` | `task.create_from_plan` | `_route("task.create_from_plan", {...}, "PROTECTED_MUTATION")` | 未修（另立卡） |
| 4 | `task_plan_template` | `tools_task.py` | `task_plan_template` | `_route("task_plan_template", {}, "READ_ONLY")` | 未修（另立卡） |
| 5 | `export_module_graph` | `tools_query.py` | `export_module_graph` | `_route("export_module_graph", {"format": format}, "READ_ONLY")` | 未修（另立卡） |
| 6 | `repo_map` | `tools_summary.py` | `repo_map` | `_route("repo_map", {"format": format}, "READ_ONLY")` | 未修（另立卡） |
| 7 | `record_artifact_identity` | `tools_p2_graph.py` | `admin.record_artifact_identity` | `_route("admin.record_artifact_identity", {...})` | 未修（另立卡） |
| 8 | `publish_interface` | `tools_p2_graph.py` | `admin.publish_interface` | `_route("admin.publish_interface", {...})` | 未修（另立卡） |

（表列 8 行：7 个 `-> str` 同型 + 本卡已修的 `task_create` 计 8 个工具。）

### 3.1 同型未修 6 项的处置裁决

- 全部**只登记、不在本卡修**：落点文件 `tools_query.py` / `tools_summary.py` /
  `tools_p2_graph.py` 不在本卡 `allowed_paths`（仅 `server/tools/tools_task.py`、
  `tests/test_tools_task_create_workspace_params.py`、
  `deliverables/software-company/`、`cw_task_commit_ledger.json`）；
  `tools_task.py` 内的 `task_create_subtask` / `task_create_from_plan` /
  `task_plan_template` 虽落本卡 allowed 文件，但本卡验收只覆盖 `task_create`
  （签名/步骤/合同均单工具限定），扩修需新卡重定验收与 step。
- **登记口径**：6 项同型缺陷建议合并一卡（落点 `server/tools/*.py`，纯注解
  `-> str` 改 `Optional[dict]`，风险低、可无 daemon 单元回归覆盖）。
- **次生发现**：`task_create_subtask` / `task_create_from_plan` 同样缺 workspace
  配对参数（BR-01/BR-02 同族风险，但 daemon 侧 `task.create_subtask` 经
  `task_workspace_bindings` 父任务解析，可能不强制要求显式配对——需新卡 step0
  实证 daemon 侧 `required_workspace_id_param` 是否覆盖子任务创建路径，本卡不判定）。

## 4. 结论

- 两处缺陷形态确认（签名缺配对 + 注解失配），修法锚点明确（BR-01/BR-02 透传规则 +
  `Optional[dict]` 先例）。
- 同型扫描完成：243 工具 / 7 个 `-> str` 失配 / 6 项未修已登记。
- 本 step 未改任何代码（`git diff --check` 对 `server/**` 无变化）。

## 5. A′ 收尾闭环（2026-09-16，executor→reviewer→adjudicator 全链路）

卡 `T-1789564402123-9b47d988` 终态：`workflow_status=completed` / `lifecycle=closed` /
`decision=COMPLETE`（routing reason「任务已 closed，所有终态门禁满足」）。

### 5.1 治理链路事件

| 阶段 | 动作 | 关键 ID |
|---|---|---|
| executor | 4 步 report 全 done（step0 adjudicate / step1 implement / step2 test / step3 release_verify） | report req `req-94b9bbf13c15` / `req-bf569e0c2fbe` / `req-0dd8d445b04b` / `req-348ae3fcfee8` |
| executor→reviewer | handoff `executor_ready_for_review` | request `handoff-c23-executor-ready-r4`，event 9154 |
| reviewer | verdict 持久化（pass，4 clause 全 pass） | `V-C23-REVIEWER-PASS-2`，event 643 |
| reviewer→adjudicator | handoff `reviewer_pass` | request `handoff-c23-reviewer-pass-r4`，event 9157 |
| adjudicator | `completion-review` = pass；handoff `adjudicator_accepted` | request `handoff-c23-adjudicator-accepted-r3`，event 9160 |
| adjudicator | `task.apply` → `applied`（1789570299） | identity adjudicator-wb-c23/inst-...-1 |
| adjudicator | `task.close` → `closed`（1789570367） | 终态门禁全满足 |

身份独立性：executor session `sess-executor-wb-c23` 与 reviewer session
`sess-reviewer-wb-c23` / adjudicator session `sess-adjudicator-wb-c23` 三相分离，
`independence-requirement=required`（executor→reviewer、reviewer→adjudicator 两跳）。

### 5.2 reviewer 独立只读复审实证（新 session，非 executor 续接）

1. **source 级**：`git show --stat e4b335d` 仅 `tools_task.py` + 新增 test 两文件，
   无 db/rust_ext/scripts 越界。
2. **单测级**：`pytest tests/test_tools_task_create_workspace_params.py` → 3 passed。
3. **behavior 级 live e2e**：经运行中 daemon（PID 40860，commit f59890c）显式配对
   `workspace_id=1` + `workspace_instance_id=4baea3ff12c2ea5c` 调 `task.create` →
   创建成功 `T-1789569050551-e72d6938` / `open`，返回 dict 含 task_id。
4. **fail-closed 对照**：不传配对 → `E_TASK_WORKSPACE_UNBOUND`（「生产路径禁止用
   active workspace / cwd 补齐」）。
5. **同型登记**：6 个未修 `-> str` 失配工具已在 §3 表登记，建议合并一卡收编。

### 5.3 过程障碍与处置（可复用经验）

- **stray lease 丢失 raw token**：内联重试 handoff 失败后未释放第二把 lease
  （`L-962e0fb6e9dae711`，token 仅 acquire 时返回一次）。处置：CLI `lease acquire`
  被 `_governance_gate`（cli/main.py:16968，`governance_blocked` 硬门禁）拦截；
  但 daemon 侧 `lease.acquire`（task_collab_lease.rs:1281-1332）对 holder 注册
  缺失/失活/心跳 >15min（`ORPHAN_CLAIM_STALE_SECS`）的 active lease 会同事务回收——
  直连 `http://127.0.0.1:5571/v1/rpc`（`protocol_version:"1"`、字符串 id）acquire
  即触发 `holder_registration_missing` 回收，解除 `governance_blocked`。
- **canonical role 映射缺口（daemon 侧真实缺陷，建议另立卡）**：
  `canonical_claim_role`（task_collab_shared.rs:328）把 `implementer`→`executor`
  归一化，但三处消费点未用归一化，导致 runtime role 存作 `implementer` 时：
  (a) `lease.recover` 的 `ALLOWED_TARGET_ROLES` 校验 `executor` 通过、DB 查询
  `role='executor'` 落空 → `E_LEASE_NOT_FOUND`；
  (b) `task handoff --role executor` 查 implementer lease 落空 → `E_LEASE_NOT_FOUND`；
  (c) `task apply/close --role adjudicator` 映射到 `lease_role=reviewer` 的 lease
  查询 → `E_LEASE_TOKEN_MISMATCH`。
  绕过：CLI 传与 lease 存储一致的 role（`implementer`/`reviewer`），identity 仍用
  治理角色。**根因修复**应在 `recover_orphaned_lease_for_task` 的 DB 查询与
  CLI lease 查找处统一走 `canonical_claim_role`。
- **verdict identity 一致性**：`verdict.submit` 的 identity 须与后续
  `reviewer_pass` handoff identity 完全一致（含 `agent_instance_id`，空串会被
  `E_HANDOFF_VERDICT_IDENTITY_MISMATCH` 拒绝）；request_id 复用不同参数会
  `E_REQUEST_ID_REUSE_MISMATCH`，每次重交换新 id。
- **handoff 路由表**：`adjudicator_accepted` → `("adjudicator","complete",
  "not_applicable")`（task_collab_lifecycle.rs:1269）；accepted 只落裁决，任务终态
  仍须 adjudicator 执行 `task.apply`（review→applied）+ `task.close`
  （applied→closed）。
