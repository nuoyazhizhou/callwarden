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

---

# 整改回路追加（BLOCKED，loop_count=1）— 2026-11-26 23:58:06 +0800 planner（蓝图远）

> 本节为**追加**记录，上方 §批次1/§批次2/§放行判据/§交接包清单（原 1–32 行）保持原样，不覆盖、不重排。
> **依据 HEAD**：`0d6fa326a75451c1f5aeadaf3c04a691af3a0a3c`（commit 0d6fa32，D3 自愈守卫，5 文件 +1080/-71）。
> **评审对象**：根修卡 `T-1795507699058-41d9fcbc`（合同 r3 hash `c347042f…14e`, identity_policy=legacy_identity_v1）/ Step 3 = `S-1795507699843-70a0bea0`；reviewer `reviewer-roofix-alm01` / `sess-reviewer-roofix-alm01-20261125` 判 **BLOCKED**。
> **REQ 首行决策**：根修卡 description 仅「REQ：…」散文 + D1/D2/D3 目标，DB 复核 `REQ-xx` token = []（无 formal 编号），故五张整改单首行统一 `REQ-GAP`（G6.5：无对应需求写 REQ-GAP）。

## 13. daemon 可达性与建单状态（**已建单**，2026-11-27 由 team lead 代为落库）

> **落库回执（2026-11-27）**：daemon 以 detached 方式恢复（PID 30908，commit 1af24548d239，manifest 双键一致）后，经 `cw task create` CLI（parent-aware + identity_policy=legacy_identity_v1 + workspace 显式配对 1/4baea3ff12c2ea5c）成功落库 5/5。§14 表 task_id 列已回填，daemon 回执为准。

- planner 自有 session 注册身份五元组：`agent_id=planner-roofix-alm01` / `agent_instance_id=planner-roofix-alm01-inst` / `session_id=sess-planner-roofix-alm01-20261125-l1` / `model_id=hunyuan-hy` / `role=planner`（未借用 executor/reviewer 身份）。
- **卡在建单第 1 步：身份注册即失败**。实测三条路径全部 `E_HTTP_MANIFEST_STALE`：
  1. `mcp__callwarden__bootstrap_status` → `E_HTTP_MANIFEST_STALE: manifest PID 6520 已不存活（stale manifest）`；
  2. `cw daemon ping` → 同上；`agent.register`（rpc_helper 直连）→ `DaemonRemoteError E_HTTP_MANIFEST_STALE`；
  3. `task.create`（rpc_helper 直连，含三角色合同 + identity_policy + workspace 配对）→ `DaemonRemoteError E_HTTP_MANIFEST_STALE`。
- stale manifest：`~/.callwarden/http-daemon.S-1-5-21-1583625257-826939952-3615027596-1001.manifest.json`（pid=6520，endpoint https://127.0.0.1:7473）。
- 次生问题：MCP `task_create` 工具层将 `steps`/`role_contracts` 数组序列化为字符串导致参数校验失败（非 daemon 问题）；daemon 恢复后应走 RPC 直连通道（见 §12 先例），显式传 `role_contracts` + `identity_policy` + `workspace_id=1` + `workspace_instance_id=4baea3ff12c2ea5c`。
- **结论：本轮 0 张整改单落库、0 个 task_id 产生**。下方 §14 为完整 payload 文本，daemon 恢复后代为落库或指示重试。

## 14. findings → 整改单 payload 映射（追加；findings 原文一字不改）

| finding | 处置 | 优先级 | 依赖 | action | title（符号锚定，无行号） | REQ 首行 | 落库 task_id |
|---|---|---|---|---|---|---|---|
| F1 | 建单 | P0 🔴 | 无（F2 依赖本单） | fix | `daemon_mutex.py:acquire_host_daemon_mutex fix 定义 authority_id 消除 UnboundLocalError 并补真实测试` | REQ-GAP | `T-1795744093604-26d3cb28` |
| F2 | 建单 | P1 🔴 | **F1 先修** | fix | `daemon_autostart.py:spawn_http_daemon fix host 锁设为唯一权威守卫并补双会话并发负向测试` | REQ-GAP | `T-1795744094146-4722555c` |
| F3 | 建单 | P2 🟡 | 无 | fix | `web_gateway.py:main fix 启动钩子注册 role=webui 条目并补集成测试` | REQ-GAP | `T-1795744094756-6b7ea004` |
| F4 | 建单 | P2 🟡 | 无 | fix | `T-1795507699058-41d9fcbc.Step3 fix 回填 commit 0d6fa32 其余 4 文件 changes 记录` | REQ-GAP | `T-1795744095778-a85ae668` |
| F5 | 建单（低优先级/可选） | P3 💭 | 无 | fix | `host_process_registry.py:compare_with_manifest fix 删除死代码或在 authority_daemon_diagnostic 复用` | REQ-GAP | `T-1795744096454-d0aa2a70` |
| F6（追加 2026-11-27） | 建单 | P2 🟡 | 与 F3/F5 串行（同测试文件） | fix | `daemon_autostart.py:spawn_http_daemon fix 追加 CREATE_BREAKAWAY_FROM_JOB 带降级 fallback 使自愈 spawn 脱离 job object` | REQ-GAP | `T-1795829524399-0ef7f8ac` |
| F7（追加 2026-11-28） | 建单 | P2 🟡 | **F6 之后串行**（同 daemon_autostart.py + 测试文件） | fix | `host_process_registry.py:reconcile_stale fix 接入生产周期/启动清理消除崩溃残留永活` | REQ-GAP | `T-1795831521518-0c880ea4` |

- 全部条目 provenance：`finding=F1..F5 / loop=1 / verdict=BLOCKED / source_reviewer=reviewer-roofix-alm01 / root_card=T-1795507699058-41d9fcbc / step=S-1795507699843-70a0bea0`。
- F6 追加条目 provenance：`finding=F6 / loop=1 / verdict=BLOCKED / source_reviewer=reviewer-roofix-alm01 / root_card=T-1795507699058-41d9fcbc / step=S-1795507699843-70a0bea0`（reviewer 复评 F1 时立的新 finding，归根修卡 D3 scope）。
- F7 追加条目 provenance：`finding=F7 / loop=1 / verdict=BLOCKED / source_reviewer=reviewer-roofix-alm01 / root_card=T-1795507699058-41d9fcbc / step=S-1795507699843-70a0bea0`（reviewer 复评 F3 时立的新 finding，归根修卡 D3 scope；附录 `evidence/T-1795744094756-6b7ea004/f3-evidence.md`，引用时 sha256 以文件当前值为准 `510ac75c…`，team-lead 转述的 `5151a68f…` 为早期版本）。
- 执行顺序：F1 → F2 → F3/F4（可并行）→ F5（可选）→ F6 → F7。F2 的 `blocked-by:` 为 description 文本约定，daemon 不强制，须在派工时显式确认 F1 已闭环。
- F6 执行顺序约束：与 F3/F5 **串行**（F6 测试与 F3/F5 改同一个 `tests/test_host_process_registry.py`，避免同文件并发）；F6 与 F3/F4/F5 源文件无冲突（F6 动 daemon_autostart.py 两处 spawn flags + 测试，F3 动 web_gateway.py，F4 动任务记录，F5 动 host_process_registry.py）。
- F7 执行顺序约束：**F6 之后串行**（F7 与 F6 同改 `server/daemon_autostart.py` + `tests/test_host_process_registry.py`）；F7 与 F3/F4/F5 源文件无冲突（F7 动 daemon_autostart.py 启动清理落点 + web_gateway.py 注释 + f3-evidence.md 更正 + 测试；host_process_registry.py 预期零改动，复用 reconcile_stale 既有签名）。
- 一条 finding 一张单，**未合并、未改写、未删减** findings 原文。F5 建为低优先级可选单（不并入口径，便于主理人取舍）。
- **无计划缺陷判定**：F1–F4 均指向实现/治理证据缺口，非契约漏项或 AC 写错或锚定错符号；无需回退 R0/R2/R3。F3 的「书面说明由哪个组件承担」为 finding 原文给出的合规出口，已写入 steps。

### 14.1 公共 payload 段（每张单都带）

```json
"creator": "planner-roofix-alm01",
"identity_policy": "legacy_identity_v1",
"workspace_id": 1,
"workspace_instance_id": "4baea3ff12c2ea5c",
"parent_id": "T-1795507699058-41d9fcbc",
"role_contracts": [
  {"role":"executor","prompt":"cw.aprime.executor.startup.v1","description":"Execute steps under the frozen contract; stop and report on failure.","instructions":"Read the contract and steps before acting. Execute within the authorized paths. Record evidence for each check item. On failure stop and escalate."},
  {"role":"reviewer","prompt":"cw.aprime.reviewer.audit.v1","description":"Verify step evidence against acceptance criteria; request changes when unmet.","instructions":"Audit the evidence only; do not rewrite the implementation. Verify each check item against the diff. Escalate unresolved findings."},
  {"role":"adjudicator","prompt":"cw.aprime.adjudicator.decision.v1","description":"Adjudicate escalation and completion decisions from reviewer evidence.","instructions":"Decide on the reviewer's evidence. Allowed decisions: approve, request-changes, reject. Do not execute tasks."}
]
```

> `task_id` 未预生成：避免假 task_id，daemon `task.create` 回执为准。

### 14.2 F1 payload（P0 🔴）

```json
{
  "title": "daemon_mutex.py:acquire_host_daemon_mutex fix 定义 authority_id 消除 UnboundLocalError 并补真实测试",
  "description": "REQ-GAP | finding=F1 | 🔴 BLOCKER | loop=1 | verdict=BLOCKED | source_reviewer=reviewer-roofix-alm01\n归属：根修卡 T-1795507699058-41d9fcbc / Step 3（S-1795507699843-70a0bea0）/ commit 0d6fa32，合同 r3 hash c347042f…14e。\nREQ 说明：根修卡无 formal REQ-xx 编号（DB 复核 token=[]），首行按 G6.5 记 REQ-GAP。\n依赖：无；F2 依赖本单先修（blocked-by 文本约定，daemon 不强制）。\n【reviewer finding 原文，一字不改】\nF1【🔴 BLOCKER】`server/daemon_mutex.py:304-315` `acquire_host_daemon_mutex()` 引用未定义局部变量\n- 位置：`if not authority_id:`（:312）—函数无任何参数、模块级也无 `authority_id` 定义。\n- 问题：每次调用必抛 `UnboundLocalError`。\n- 影响：① `spawn_http_daemon:555` 在\"无存活 daemon\"路径崩溃，整个合法启动/重启路径（显式 bind :569-589、注册表 upsert :621-636、health 等待 :638-652）全部成为死代码；② host 级互斥（消 TOCTOU 的 :553-567 临界区）从未真正生效；③ `cw daemon start`（无活 daemon 时）、`call_with_autostart` 调用将抛未捕获异常。\n- 修正方向：函数内先定义 `authority_id = \"\"`（或 `authority_id = get_http_authority_id()`）再判空；**补一个不 mock 的真实测试**直接调用 `acquire_host_daemon_mutex()` 并覆盖 spawn 的 alive=False 全路径。\n【G6】title 符号锚定无行号；check_items 字符串；action=fix；steps 非空。",
  "steps": [
    {"action":"fix","target_file":"server/daemon_mutex.py","target_symbol":"acquire_host_daemon_mutex","check_items":"函数内先定义 authority_id=''（或调用 get_http_authority_id()）再判空；不 mock 直接调用 acquire_host_daemon_mutex() 不再抛 UnboundLocalError；host 级锁按 authority+task_db 派生、同机所有 endpoint 共享"},
    {"action":"fix","target_file":"server/daemon_autostart.py","target_symbol":"spawn_http_daemon","check_items":"alive=False 路径不再崩溃：显式 bind、注册表 upsert、health 等待三段均可达；消 TOCTOU 的 :553-567 临界区 host 锁真正获取"},
    {"action":"fix","target_file":"tests/test_host_process_registry.py","target_symbol":"spawn_http_daemon","check_items":"补不 mock 的真实测试直接调用 acquire_host_daemon_mutex() 并覆盖 spawn alive=False 全路径；相关既有回归零破坏（基线 51 passed 9 skipped）"}
  ]
}
```

### 14.3 F2 payload（P1 🔴，依赖 F1）

```json
{
  "title": "daemon_autostart.py:spawn_http_daemon fix host 锁设为唯一权威守卫并补双会话并发负向测试",
  "description": "REQ-GAP | finding=F2 | 🔴 BLOCKER | loop=1 | verdict=BLOCKED | source_reviewer=reviewer-roofix-alm01\n归属：根修卡 T-1795507699058-41d9fcbc / Step 3 / commit 0d6fa32。\nREQ 说明：根修卡无 formal REQ-xx 编号，首行按 G6.5 记 REQ-GAP。\n依赖：F1 先修（blocked-by=F1 payload，daemon 不强制，派工须显式确认 F1 已闭环）。\n【reviewer finding 原文，一字不改】\nF2【🔴 BLOCKER】host 级防双实例守卫实际不可用，双 daemon bypass 未消除\n- 位置：`daemon_mutex.py:304-315` + `daemon_client.py:1931` + `daemon_autostart.py:554-555`。\n- 问题：F1 导致 spawn 内 host 锁从未获取；外层 `call_with_autostart` 仍用旧 endpoint 级锁。\n- 影响：D3 核心目标\"禁止两个 daemon 共写同一主库\"未强制，两会话不同 endpoint 可各自通过。\n- 修正方向：F1 修复后，确认 `spawn_http_daemon` 的 host 锁为唯一权威守卫并实测并发两会话竞争；若保留 `call_with_autostart` 的 endpoint 锁，须显式标注其为非权威并在 spawn 前统一走 host 锁，补并发负向测试。\n【G6】title 符号锚定无行号；check_items 字符串；action=fix；steps 非空。",
  "steps": [
    {"action":"fix","target_file":"server/daemon_autostart.py","target_symbol":"spawn_http_daemon","check_items":"F1 修复后确认 spawn 内 host 锁（acquire_host_daemon_mutex）为唯一权威守卫；消 TOCTOU 的临界区真实生效"},
    {"action":"fix","target_file":"server/daemon_client.py","target_symbol":"call_with_autostart","check_items":"若保留 endpoint 级 DaemonMutex(:511/:1931)，须显式标注非权威，并在 spawn 前统一走 host 锁；不得出现两套锁各自放行的路径"},
    {"action":"fix","target_file":"tests/test_host_process_registry.py","target_symbol":"concurrent_spawn","check_items":"补并发负向测试：两会话不同 endpoint 并发竞争，仅一个 daemon 成功启动，杜绝双 daemon 共写同一主库"}
  ]
}
```

### 14.4 F3 payload（P2 🟡）

```json
{
  "title": "web_gateway.py:main fix 启动钩子注册 role=webui 条目并补集成测试",
  "description": "REQ-GAP | finding=F3 | 🟡 需整改 | loop=1 | verdict=BLOCKED | source_reviewer=reviewer-roofix-alm01\n归属：根修卡 T-1795507699058-41d9fcbc / Step 3 / commit 0d6fa32。\nREQ 说明：根修卡无 formal REQ-xx 编号，首行按 G6.5 记 REQ-GAP。\n依赖：无。\n【reviewer finding 原文，一字不改】\nF3【🟡 需整改】webui role=webui 生产注册未落地\n- 位置：`host_process_registry.py:264-304` 支持 role=webui，但 `web_gateway.py`（不在 commit）无注册调用。\n- 影响：验收栏②仅单测通过，生产环境注册表无 webui 条目。\n- 修正方向：在 web_gateway 启动钩子注册 role=webui 条目并正常退出注销，或书面说明由哪个组件承担；补集成测试。\n【G6】title 符号锚定无行号；check_items 字符串；action=fix；steps 非空。",
  "steps": [
    {"action":"fix","target_file":"server/web_gateway.py","target_symbol":"main","check_items":"启动钩子调用 host_process_registry 注册 role=webui 条目（pid+process_start_time 双键），正常退出注销；或书面指定承担组件并留痕"},
    {"action":"fix","target_file":"tests/test_host_process_registry.py","target_symbol":"webui_registration_integration","check_items":"补集成测试：生产启动路径在注册表可见 role=webui 条目、退出后注销；daemon 重启不误清 webui 条目"}
  ]
}
```

### 14.5 F4 payload（P2 🟡）

```json
{
  "title": "T-1795507699058-41d9fcbc.Step3 fix 回填 commit 0d6fa32 其余 4 文件 changes 记录",
  "description": "REQ-GAP | finding=F4 | 🟡 治理证据缺陷 | loop=1 | verdict=BLOCKED | source_reviewer=reviewer-roofix-alm01\n归属：根修卡 T-1795507699058-41d9fcbc / Step 3 / commit 0d6fa32。\nREQ 说明：根修卡无 formal REQ-xx 编号，首行按 G6.5 记 REQ-GAP。\n依赖：无。\n【reviewer finding 原文，一字不改】\nF4【🟡 治理证据缺陷】changes 覆盖不完整\n- 位置：task card Step3 target_file 与 changes-json 仅登记 daemon_autostart.py；commit 实含 5 文件。\n- 修正方向：planner 回填其余 4 文件（host_process_registry.py/daemon_mutex.py/mcp_server.py/test_host_process_registry.py）的 changes 记录。\n【G6】title 符号锚定无行号（根修卡 Step3 changes 字段，非源码行号）；check_items 字符串；action=fix；steps 非空。",
  "steps": [
    {"action":"fix","target_file":"server/daemon_autostart.py","target_symbol":"spawn_http_daemon","check_items":"回填 commit 0d6fa32 其余 4 文件（host_process_registry.py/daemon_mutex.py/mcp_server.py/test_host_process_registry.py）的 changes 记录到 Step3 target_file 与 changes-json；5 文件全覆盖、逐文件可追溯"},
    {"action":"fix","target_file":".design/preq/task-map.md","target_symbol":"task-map","check_items":"task-map 追加 F4 整改条目并注明 finding=F4/loop=1/verdict=BLOCKED；不改历史行"}
  ]
}
```

### 14.6 F5 payload（P3 💭 可选）

```json
{
  "title": "host_process_registry.py:compare_with_manifest fix 删除死代码或在 authority_daemon_diagnostic 复用",
  "description": "REQ-GAP | finding=F5 | 💭 可选 | loop=1 | verdict=BLOCKED | source_reviewer=reviewer-roofix-alm01\n归属：根修卡 T-1795507699058-41d9fcbc / Step 3 / commit 0d6fa32。\nREQ 说明：根修卡无 formal REQ-xx 编号，首行按 G6.5 记 REQ-GAP。\n依赖：无；低优先级可选，主理人可决定书面记录取舍并搁置。\n【reviewer finding 原文，一字不改】\nF5【💭 可选】`compare_with_manifest()` 为生产死代码（仅测试调用），可删除或在 `authority_daemon_diagnostic` 中复用。\n【G6】title 符号锚定无行号；check_items 字符串；action=fix；steps 非空。",
  "steps": [
    {"action":"fix","target_file":"server/host_process_registry.py","target_symbol":"compare_with_manifest","check_items":"二选一并留痕：删除 compare_with_manifest 生产死代码，或在 authority_daemon_diagnostic（daemon_autostart.py:446）中复用；补相应测试；低优先级可选"}
  ]
}
```

## 15. 未决项（open）

- **O-R1（resolved 2026-11-27）— 建单落库已完成**：daemon detached 恢复后经 `cw task create` 落库 5/5（回执见 §13）。验证方式：5 张卡 `task.status` 均 open、workspace provenance 与 §14.1 公共段一致（workspace_id=1 / 4baea3ff12c2ea5c / parent=T-1795507699058-41d9fcbc）。
- **O-R2（open）— task_id 派工序**：F2 的 blocked-by 仅为文本约定，daemon 不强制；派工时须人工确认 F1 已闭环再放 F2。F1=`T-1795744093604-26d3cb28`，F2=`T-1795744094146-4722555c`。验证方式：F1 回执状态闭环后派工。
- **O-R3（open）— F3 承担组件**：若不在 web_gateway 注册，须书面指定承担组件；验证方式：整改单 step 1 的 check_items 留痕，复评核验。
- **O-R4（resolved 2026-11-27）— MCP task_create 数组序列化**：F6 建单改走 `cw task create` CLI 通道（parent-aware + 显式 identity_policy + workspace 配对），绕开 MCP 工具层数组序列化 bug；回执 task_id=`T-1795829524399-0ef7f8ac`，读回验证 3 步 check_items 全为字符串、blocking_reasons=[]、workflow=queued。该工具缺口仍建议登记主理人工具缺口清单（后续 MCP 面修复）。
- **O-R5（open）— F6 与 F3/F5 同文件串行**：F6 测试与 F3/F5 都可能改 `tests/test_host_process_registry.py`，派工须串行；验证方式：F3/F5 任一闭环后再放 F6，或在 F6 description 已注明的串行约束下由 executor 自律。
- **O-R6（open）— F6 定级可能升 🔴**：reviewer 定性若实锤 6520/46340 死因即 job 连坐，建议对根修卡升格 🔴（daemon 可用性是 D3 目标本体）；该裁决属 reviewer/adjudicator 权限，planner 仅登记不预设结论。验证方式：F6 整改中复现「IDE job object 内 spawn 被回收」证据后由 reviewer 复核定级。
- **O-R7（open）— F7 与 F6 同文件串行派工**：F7 与 F6 同改 `server/daemon_autostart.py` + `tests/test_host_process_registry.py`，F7 须排 F6（`T-1795829524399-0ef7f8ac`）之后；验证方式：F6 回执状态闭环后派工 F7。
- **O-R8（open）— F7 选型留痕与①周期清理备选**：F7 选 reviewer 修正方向②「启动时 reconcile 一次」（落点 `spawn_http_daemon` 临界区，覆盖「重启清理」+长期运行积压）；①周期清理（Rust watchdog/Python 常驻循环）列为备选，因更重（需常驻循环/跨语言）暂不纳入本单。若长效 daemon 永不重启场景被实锤为高频，①是否补入由 reviewer 复核 F7 整改时裁决。验证方式：F7 整改 result 留痕选型理由 + reviewer 复核。

## 16. 本轮边界声明

planner 本轮**未**写生产代码、**未**改判 BLOCKED、**未**动 `task.apply`/`task.close`/`task.supersede`、**未**认领/lease 任何步骤、**未**改写上方历史行；仅追加本节 + 准备 §14 payload 文本。

> F6 追加（2026-11-27）：另见 §17 建单回执 / §18 payload 存档 / §19 F6 边界声明。

---

# F6 追加建单（BLOCKED loop=1 新 finding）— 2026-11-27 planner（蓝图远）

> 本节为**追加**记录，上方 §1–§16 历史行保持原样，不覆盖、不重排。
> 背景：daemon 恢复（PID 28728 / endpoint 6374 / health ok）；reviewer 复评 F1 时立新 finding **F6**，按分岔流程由 planner 建独立整改单。
> **planner 身份已注册成功**：`agent_id=planner-roofix-alm01` / `agent_instance_id=planner-roofix-alm01-inst` / `session_id=sess-planner-roofix-alm01-20261125-l1` / `model_id=hunyuan-hy` / `role=planner`（daemon 回执 `status=registered`，未借用他人身份）。

## 17. F6 建单回执（daemon 返回原文）

- **task_id**：`T-1795829524399-0ef7f8ac`
- **title**：`daemon_autostart.py:spawn_http_daemon fix 追加 CREATE_BREAKAWAY_FROM_JOB 带降级 fallback 使自愈 spawn 脱离 job object`
- **通道**：`cw task create` CLI（cwd c:\git_work\callwarden，绕开 MCP 数组序列化 bug O-R4），参数：`--parent-id T-1795507699058-41d9fcbc --identity-policy legacy_identity_v1 --workspace-id 1 --workspace-instance-id 4baea3ff12c2ea5c`
- **读回验证（task.status）**：
  - `status=open` / `lifecycle=open` / `workflow=queued` / `blocking_reasons=[]`
  - `parent_id=T-1795507699058-41d9fcbc`；workspace 配对 `workspace_id=1` / `workspace_instance_id=4baea3ff12c2ea5c` / `workspace_binding_id=tb-T-1795829524399-0ef7f8ac-4baea3ff12c2ea5c`
  - steps=3，action 全 `fix`，check_items 全为**字符串**（len 214/110/81，非转义 JSON），覆盖两处 spawn 点（`_start_daemon_windows` / `spawn_http_daemon`）+ fallback + 测试
  - description 首行 `REQ-GAP | finding=F6 | 🟡 需整改 | loop=1 | verdict=BLOCKED | source_reviewer=reviewer-roofix-alm01`；含 reviewer 定性原文一字不改 + 「与 F3/F5 串行执行，避免同文件并发」依赖说明
- **无 role_contracts 缺省注入缺口**：CLI `task create` 缺省注入 A' 三角色模板（executor/reviewer/adjudicator legacy contract），与 §14.1 公共段一致（governance 投影 READY，blocking_reasons=[] 印证）。

## 18. F6 payload 与 reviewer 定性原文（存档，daemon 已落库）

```json
{
  "title": "daemon_autostart.py:spawn_http_daemon fix 追加 CREATE_BREAKAWAY_FROM_JOB 带降级 fallback 使自愈 spawn 脱离 job object",
  "description": "REQ-GAP | finding=F6 | 🟡 需整改 | loop=1 | verdict=BLOCKED | source_reviewer=reviewer-roofix-alm01\n归属：根修卡 T-1795507699058-41d9fcbc / Step 3 / commit 0d6fa32（D3 scope），合同 r3 hash c347042f…14e。\nREQ 说明：根修卡无 formal REQ-xx 编号（DB 复核 token=[]），首行按 G6.5 记 REQ-GAP。\n依赖：与在途 F3/F5 无文件冲突（F3 动 web_gateway.py，F5 动 host_process_registry.py），但 F6 测试可能与 F3/F5 改同一个测试文件 test_host_process_registry.py——**与 F3/F5 串行执行，避免同文件并发**。\n【reviewer 定性原文，一字不改】\n（定性全文见 §17 建单回执 description，daemon 已原样落库，此处不重复粘贴以免漂移）\n【G6】title 符号锚定无行号；check_items 字符串；action=fix；steps 非空。",
  "steps": [
    {"action":"fix","target_file":"server/daemon_autostart.py","target_symbol":"_start_daemon_windows","check_items":"Windows spawn flags（:319）追加 CREATE_BREAKAWAY_FROM_JOB(0x01000000)；必须带 fallback：job 未开放 breakaway 权限时 CreateProcess 报 ERROR_ACCESS_DENIED，捕获后降级为原 flags（DETACHED_PROCESS|CREATE_NEW_PROCESS_GROUP|CREATE_NO_WINDOW）重试成功"},
    {"action":"fix","target_file":"server/daemon_autostart.py","target_symbol":"spawn_http_daemon","check_items":"第二处 spawn 点（:605）追加 CREATE_BREAKAWAY_FROM_JOB + 同一降级 fallback；两处行为一致，父进程退出后 daemon 仍存活（符合 :303 自述意图），且不破坏单实例保证"},
    {"action":"fix","target_file":"tests/test_host_process_registry.py","target_symbol":"spawn_breakaway_job","check_items":"补「IDE job object 内 spawn 存活」集成测试或人工验证步骤留痕（6520/46340 死法特征复现）；与 F3/F5 串行执行，避免同文件并发"}
  ]
}
```

## 19. F6 追加边界声明

planner 本轮 F6 建单**未**写生产代码、**未**改判 BLOCKED/🟡 定级、**未**动 `task.apply`/`task.close`/`task.supersede`、**未**认领/lease 步骤、**未**改写上方历史行；仅追加 F6 行（§14 表）+ §17 建单回执 / §18 payload 存档 + §15/§16 未决项与边界更新 + 本节声明。

---

# F7 追加建单（BLOCKED loop=1 新 finding）— 2026-11-28 planner（蓝图远）

> 本节为**追加**记录，上方 §1–§19 历史行保持原样，不覆盖、不重排。
> 背景：reviewer 复评 F3 时立新 finding **F7**（附录 `evidence/T-1795744094756-6b7ea004/f3-evidence.md`），按分岔流程由 planner 建独立整改单；executor 当前 F5 刚完成、F6 待做，F7 排 F6 之后。
> planner 身份沿用已注册：`agent_id=planner-roofix-alm01` / `agent_instance_id=planner-roofix-alm01-inst` / `session_id=sess-planner-roofix-alm01-20261125-l1` / `model_id=hunyuan-hy` / `role=planner`。

## 20. F7 建单回执（daemon 返回原文）

- **task_id**：`T-1795831521518-0c880ea4`
- **title**：`host_process_registry.py:reconcile_stale fix 接入生产周期/启动清理消除崩溃残留永活`
- **通道**：`cw task create` CLI（cwd c:\git_work\callwarden，绕开 MCP 数组序列化 bug O-R4），参数：`--parent-id T-1795507699058-41d9fcbc --identity-policy legacy_identity_v1 --workspace-id 1 --workspace-instance-id 4baea3ff12c2ea5c`
- **读回验证（task.status）**：
  - `status=open` / `lifecycle=open` / `workflow=queued` / `blocking_reasons=[]`（governance READY，三角色合同已注入）
  - `parent_id=T-1795507699058-41d9fcbc`；workspace 配对 `workspace_id=1` / `workspace_instance_id=4baea3ff12c2ea5c` / `workspace_binding_id=tb-T-1795831521518-0c880ea4-4baea3ff12c2ea5c`
  - steps=3，action 全 `fix`，check_items 全为**字符串**（len 312/186/199，非转义 JSON），非空
  - **F4 防复发全量声明落实**：step#0 逗号声明 `server/daemon_autostart.py,server/host_process_registry.py`；step#1 逗号声明 `server/web_gateway.py,evidence/T-1795744094756-6b7ea004/f3-evidence.md`；step#2 `tests/test_host_process_registry.py`——实际会动的全部文件逐 step 全量声明，禁止只写一个
  - description 首行 `REQ-GAP | finding=F7 | 🟡 需整改 | loop=1 | verdict=BLOCKED | source_reviewer=reviewer-roofix-alm01`；reviewer 定性原文一字不改（5 处关键句逐字校验通过）；含「F7 排 F6 之后串行」依赖说明 + F4 防复发全量声明说明
- **planner 锚定备注（写入 description）**：reviewer 引 `:297` 行号在当前 HEAD（`0d5f3170`）已漂移至 `host_process_registry.py:323`（`def reconcile_stale`），故 title/steps 全用符号锚定不用行号；spawn 路径 upsert 落点实测 `daemon_autostart.py:663-676`。
- **F7 选型留痕（写入 step#0 check_items）**：选 reviewer 修正方向②「启动时 reconcile 一次」，落点 `spawn_http_daemon` 临界区（registry upsert 前后调 `reconcile_stale(authority_id)`），理由=自然「重启清理」点+处理长期运行积压+单文件收敛（与 D3 守卫同文件）；①周期清理列为备选（更重），长效运行永不重启残留场景登记 O-R8 交 reviewer 复核。

## 21. F7 payload 存档（daemon 已落库）

```json
{
  "title": "host_process_registry.py:reconcile_stale fix 接入生产周期/启动清理消除崩溃残留永活",
  "steps": [
    {"action":"fix","target_file":"server/daemon_autostart.py,server/host_process_registry.py","target_symbol":"spawn_http_daemon","check_items":"选型留痕：选 reviewer 修正方向②「启动时 reconcile 一次」——在 spawn_http_daemon 临界区内、registry upsert 前后调 reconcile_stale(authority_id) 一次；理由=自然「重启清理」点+处理长期运行积压+单文件收敛（与 D3 守卫同文件）；①周期清理列为备选（需 Rust watchdog/Python 常驻循环，更重），长效运行永不重启的残留场景登记 open 项交 reviewer 裁决。复用 reconcile_stale 既有签名（预期 host_process_registry.py 零改动；如需改动须在 result 留痕理由）"},
    {"action":"fix","target_file":"server/web_gateway.py,evidence/T-1795744094756-6b7ea004/f3-evidence.md","target_symbol":"main","check_items":"修正 web_gateway.py:403 误导注释：改为「崩溃残留由 daemon spawn 路径启动时调 reconcile_stale 标 stale」，不再声称权威 daemon 周期清理；F3 commit message 为不可变历史不改写，在 f3-evidence.md 追加更正说明留痕（实际调用方=daemon_autostart spawn 路径）"},
    {"action":"fix","target_file":"tests/test_host_process_registry.py","target_symbol":"reconcile_stale_startup","check_items":"补测试：①崩溃残留 active 条目经 spawn 路径启动清理被标 stale（read_all 不再把死进程当活进程返回）；②webui 残留条目同样被 reconcile 且不误伤活 webui；③回归零破坏。与 F6 串行执行（F7 与 F6 同改 daemon_autostart.py + tests/test_host_process_registry.py），F7 排 F6 之后"}
  ]
}
```

> F7 description 完整文本（含 reviewer 定性原文一字不改）见 daemon 卡 `T-1795831521518-0c880ea4`，此处不重复粘贴以免漂移。

## 22. F7 追加边界声明

planner 本轮 F7 建单**未**写生产代码、**未**改判 BLOCKED/🟡 定级、**未**动 `task.apply`/`task.close`/`task.supersede`、**未**认领/lease 步骤、**未**改写上方历史行；仅追加 F7 行（§14 表）+ provenance/执行顺序约束 + §15 新增 O-R7/O-R8 + §20 建单回执 / §21 payload 存档 + 本节声明。临时脚本（_f7_steps.json / _create_f7.py / _verify_f7.py）已清理。
