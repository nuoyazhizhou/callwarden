# Web 观测面 MVP：读模型映射

> 任务卡：T-1795469778065-1686c8a0（Step 0 / S-1795469778070-16d932ac）
> 定位：**观测面，不是操控台** —— 本文档定义「人面看板」所依赖的全部只读数据通道，
> 是 Step 1（`server/web_gateway.py`）与 Step 2（`server/web_static/index.html`）的合同依据。

## 1. 不变量（全文档有效）

| # | 不变量 | 依据 |
|---|---|---|
| I1 | 全链路只读：网关只调用 READ_ONLY 类 RPC，不实现任何写动作 | 任务卡约束 |
| I2 | 不复用 daemon mTLS 面：网关是独立进程，经 `server/daemon_client.py` 的薄客户端（`route_rpc` / `route_task_read`）访问 daemon，浏览器只见网关 HTTP | 任务卡约束；mTLS 客户端证书 = agent 全权，绝不发给浏览器 |
| I3 | daemon / rust_ext 零改动；零新增第三方依赖（starlette 1.3.1 / uvicorn 0.49.0 已随 `mcp[rich]` 就位） | 任务卡验收 5 |
| I4 | **双命名空间必须同时明示**：task 侧 `workspace_id=1` / `workspace_instance_id=4baea3ff12c2ea5c`；daemon 注册表同一 root 为 `registry_workspace_id=1193`。页面不得隐藏任一侧 | 任务卡验收 4 |
| I5 | fail-closed：workspace 未解析、daemon 不可达、投影失败时，页面显示明确的错误态，**禁止静默回退或猜测 workspace** | 对齐 daemon workspace authority 语义 |
| I6 | 网关进程必须从仓库根（`C:\git_work\callwarden`）启动：daemon 侧 workspace 解析按 cwd/root_path 匹配（`mcp.daemon_client.inject_workspace_id` 上送 cwd），cwd 错 = 解析错 | `server/daemon_client.py:3404` `_inject_workspace_id` |

## 2. 端点设计（Step 1 合同）

网关默认 `127.0.0.1:8765`，入口 `python -m callwarden.server.web_gateway`。
四个只读端点 + 静态页，全部 GET：

| 端点 | 承载视图 | 数据来源 |
|---|---|---|
| `GET /api/health` | 健康横幅 + workspace 绑定横幅 | `health`、`bootstrap_status`、workspace 配对解析 |
| `GET /api/dashboard` | 7-section 总览 | `get_project_dashboard` |
| `GET /api/tasks` | 任务板（列表 + 内嵌治理投影） | `task.list`（每行已内嵌 `tree_governance_projection`） |
| `GET /api/tasks/{task_id}` | 单卡下钻（树 + 派工投影 + 证据新鲜度） | `task.status_tree` + `task.next_action` + `get_freshness_status` |
| `GET /`（及静态资源） | 单页看板 | `server/web_static/index.html`（package-data） |

错误约定：daemon 不可达/未绑定 → HTTP 503 + `{"error": {"code", "message"}}`；
参数缺失/任务不存在 → HTTP 400/404 + 结构化错误。前端按 code 渲染，不吞错。

## 3. 视图 → 只读 RPC 映射总表

| 页面视图 | RPC method | 关键参数（网关侧固定值/透传） | workspace 双参数要求 | op_class | 失败/未绑定时的降级行为 |
|---|---|---|---|---|---|
| Workspace 绑定横幅 | `workspace.list` → `workspace.status`（复用 `resolve_workspace_pair_from_daemon(项目根)`） | `project_root`=仓库根（显式，禁止回退连接默认） | 本身即 workspace_id/instance 的**权威来源**；解析 0 个或多个候选一律 fail-closed | READ_ONLY | 红色横幅「workspace 未绑定/解析失败」，其余数据面板全部禁用，不猜 id |
| 7-section 总览 | `get_project_dashboard` | `quick=true`、`with_cycles=false`、`with_evolution=false`、`top_n=5`（quick 模式约 280ms@100K 文件） | **不需要**显式 workspace_id：convergence RPC，daemon 按 peer/cwd 权威解析 workspace 主库 | READ_ONLY | 面板显示错误卡片；7 个 section 槽位保留，缺数据显示「暂无数据」而非隐藏 |
| 任务板列表 | `task.list` | `workspace_id`（必填，来自绑定横幅解析值）、`status`（可选）、`limit`（默认 100）、`offset`、`parent_id`（可选）。**默认（无 status）看板模式由网关合并 open/in_progress/review 三个活跃 lifecycle 查询**（F-1 整改，见 §4） | **需要 workspace_id**（daemon fail-closed：无显式 workspace 禁止全表列出）；instance 不参与本调用 | READ_ONLY | 503/400 结构化错误；列表为空 ≠ 隐藏面板。**读源实证（task_collab_query.rs `handle_task_list`）**：每行已内嵌 `tree_governance_projection`（`lifecycle_status / workflow_status / current_role / next_role / next_action / review / blocking_reasons` + 完整 `governance` 对象），并返回 `total / limit / offset / workspace` 分页与投影元数据——列表可见性零额外调用 |
| 逐卡派工投影（下钻详情） | `task.next_action` | `task_id`、`workspace_instance_id` —— **双参数严格必填**（`NextActionInput::from_params` 缺一即 invalid_params） | **双参数都要**；instance 来自绑定横幅（task DB 侧 `4baea3ff…`） | READ_ONLY（纯只读派工 evaluator，无 mutation；BLOCKED/NONE 是合法响应） | 该卡显示「治理投影不可用」+ 错误原因，**不得静默隐藏该卡** |
| 任务树下钻 | `task.status_tree` | `task_id` | **不需要**显式 workspace_id：与 `task.status` 同一 workspace 权威，按 task binding 解析并做 owner ACL | READ_ONLY | 任务不存在 → 404「任务不存在」 |
| 证据新鲜度（下钻区） | `get_freshness_status` | `task_id`（或 `evidence_id`） | **不需要** workspace 双参数 | READ_ONLY | 无证据事件 → 显示「暂无证据」，不视为错误 |
| 健康/自举新鲜度横幅 | `health`（daemon peer 级）+ `bootstrap_status` | 无参 | `bootstrap_status` 依赖 cwd 在 git 仓库根（workspace_root 检查 `.git`，见 `server/tools/tools_task.py:1259`） | READ_ONLY | daemon 不可达 → 503 + 顶部降级横幅；`db_stale=true` 显示「图谱滞后于 HEAD」警示条 |

补充说明：

- `get_freshness_status` 是**任务合同/证据新鲜度**（当前 contract revision vs evidence 事件），
  不是代码图谱新鲜度；后者由 `bootstrap_status.latest_scan_run` / `db_stale` 承担。两者在页面上分属不同区域，不得混用术语。
- `task.list` 返回字段：`task_id / title / description / parent_id / status / creator / created_at / updated_at`。
  树节点（`task.status_tree`）另含 `depth / sort_order / claimed_by / steps[] / children[]`。
- `task.next_action` 投影字段（治理可见性的唯一来源）：
  `lifecycle_status / workflow_status / decision / action / required_role / next_role / next_action /
  assignment / assignment_status / identity_policy / identity_policy_status /
  claim_requirements（含 blocked、reason）/ blocking_reasons / blocking_conditions / routing`。

## 4. 任务可见性规则（本卡核心验收 3）

**governance_blocked 不允许被静默过滤。** 事实依据（2026-11-24 读源确认，两处）：

1. `task.list` 的 `status` 过滤只作用于 **lifecycle_status**（open/in_progress/review/applied/closed）。
   且 `handle_task_list`（task_collab_query.rs）对每一行内嵌 `tree_governance_projection`：
   daemon 注释明言「列表与 status/status_tree 使用同一份 daemon 治理投影……
   tree_governance_projection 会明确返回 governance_blocked，而不是按 raw status 猜测下一动作」。

   **F-1 整改更正（2026-11-29）**：本条原断言「lifecycle 是 open，所以列表里一定可见」不成立——
   `handle_task_list` 按 `created_at DESC` 返回，全量 total 上千时默认 `limit=100` 的页只含最新卡，
   老 blocked 卡会被挤出默认页（静默消失）。修复：网关 `_task_list_payload` 在默认（无 status）
   看板模式改为**合并 open / in_progress / review 三个活跃 lifecycle 查询**——终态卡按 GOV-FIX-04
   短路不可能 blocked，活跃三态并集即 blocked 卡的完全覆盖（实测 36 行覆盖全部 blocked 卡）；
   合并后按 `created_at` 倒序重排，前端再 blocked 置顶展示。某 lifecycle 截断时 payload 携带
   `truncated` 说明（非静默丢弃）；显式 `status` 过滤保持 daemon 原单查语义。
2. 阻挡语义字段：`workflow_status="governance_blocked"`、`action/decision="BLOCKED"`、
   `blocking_reasons[]`（列表行内嵌）；下钻时 `task.next_action` 另含 `blocking_conditions[]`（与 reasons 同源写入）、
   `claim_requirements.blocked=true + reason`、`next_role="adjudicator"`、`next_action="system_repair_required"`、
   `assignment / assignment_status / identity_policy(+status)`。
3. 终态卡（closed/reverted）短路：`task.next_action` 投影**不会**被覆盖成 governance_blocked（GOV-FIX-04），
   页面对终态卡不再请求派工投影，避免「已关单卡永远像待处置」的假信号。

前端规则（Step 2 合同）：

- 任务板渲染以 `task.list` 行内嵌投影为准：`workflow_status="governance_blocked"` 或
  `action=BLOCKED` 的卡：保留在列表中、红色描边 + 「被治理门禁阻挡」徽标 + 展开显示 `blocking_reasons` 全文。
- **blocked 置顶**（F-1 整改）：渲染前对行做稳定排序，blocked 卡排在最前（组内保持
  `created_at` 倒序），保证默认页第一眼可见。
- **孤儿卡不静默丢弃**（F-5 整改）：父卡不在当前页时，其子卡仍作为顶层行渲染
  （`parent_id` 指向页外父卡的行直接进顶层），递归渲染不因父卡缺失而跳过子树。
- 网关返回 `truncated` 字段时在任务板顶部显示琥珀色提示条，说明哪些 lifecycle 被截断——
  截断必须可见，不允许静默过滤。
- 单卡下钻时再调 `task.next_action` 补齐 `claim_requirements / blocking_conditions / assignment`；
  调用失败：卡片保留并显示错误，不得移除。
- 列表空且无错误时才显示「无任务」——「运行器说无任务、其实卡被 governance_blocked」的盲区在本页不存在。

## 5. workspace 绑定横幅（本卡核心验收 4）

页面顶部固定横幅，同时显示三个值 + 解析时间：

```
task workspace_id = 1      workspace_instance_id = 4baea3ff12c2ea5c      daemon registry id = 1193
```

- 数据来自 `resolve_workspace_pair_from_daemon(仓库根)`：`workspace.list` 按 root 匹配 active 行 →
  `workspace.status` 校验 `registry_instance_id == task_db_instance_id` 且两侧 id 均 >0，任一不满足即 fail-closed。
- 解析失败 → 横幅红色「未绑定」+ 错误码；所有数据面板禁用。**禁止**用缓存的 id 或「最小 id 回退」假装绑定成功。

## 6. 明确不纳入本卡（后续单独建卡）

| 排除项 | 原因 |
|---|---|
| 写操作：append-only 说明、reopen（reviewer 会话 + lease 预检） | 操控台语义，需独立治理设计 |
| SSE 实时推送 / 自动刷新 | 本卡静态拉取即可验证价值 |
| 符号下钻：callers / callees / impact / call-chain 页面 | 读通道现成但页面信息架构未定，避免本卡膨胀 |
| 244 工具清单 / API 调试器 | 明确不做（做出的是调试器不是观测面） |
| 局域网证书、登录认证、多用户权限 | 本机 HTTP（localhost 为安全上下文）即可验证；`--host 0.0.0.0` 仅保留 `--token` 参数位并要求显式传入，完整认证后续卡 |
| lease / 审计链明细页 | governance 投影已覆盖「为什么被挡」的最低需求 |

## 7. 验收对照（任务卡验收 1–6）

| 验收项 | 本文档保障 |
|---|---|
| 1) `python -m callwarden.server.web_gateway` 启动、127.0.0.1:8765 可见 | §2 端点设计；入口模块挂 `callwarden.server` 包 |
| 2) 7 个 section 可见 | §3 `get_project_dashboard`（overview/code_scale/code_quality/call_graph/task_risk/audit/evolution） |
| 3) governance_blocked 卡可见且显示 blocking_reason | §4 可见性规则 |
| 4) 明示 workspace_id / workspace_instance_id | §5 绑定横幅（双命名空间） |
| 5) daemon 与 rust_ext 零改动、无新增依赖 | §1 I3；全部经既有 `route_rpc` / `route_task_read` |
| 6) 前端无构建步骤 | §2 单文件 `index.html` + 原生 JS，静态目录入 pyproject package-data |
