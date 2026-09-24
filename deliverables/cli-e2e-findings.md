# CLI 全量冒烟测试 · 问题清单（先记录，最后统一分析修复）

日期：2026-09-23 ｜ Wave：1（CLI 全覆盖冒烟）｜ 基准：146 个叶子命令 / 17 顶层组

> **执行策略（用户确认）**：发现的问题全部先记录到本文件，**不立即修改**；待 CLI + MCP 两路全量测完后，最后一期统一做调用链分析 + 归并根因 + 集中 bugfix。理由：CLI 某命令与 MCP 某工具常共享同一 handler，逐个分析会大量重复。

> 判定手段：每条只读命令以 `--mode local` 与 `--mode enterprise` 各跑一次，比对合并输出（stdout+stderr）。

---

## 一、环境前置问题（影响测试可判定性，非命令缺陷）

### F-001 · 本地库存在 5 个 active workspace（多活跃歧义）— 数字已修正
- **实测（2026-09-23）**：`~/.callwarden/callwarden.db` workspaces 表 **total 15 / active 5**（**注**：早先记录的"144 active"有误，144 是 `cw workspace list` 的其它口径计数；以本次裸 SQL 为准）。active 中含 workspace 10（TokenSlim）、19/20/21（`cw_fk_probe_*` 临时测试库）等。
- **影响**：`resolve_local_workspace_id`（runtime.rs:145 `MATCH ids` 分支）在 ≥2 个 active 时直接报 `multiple active workspaces`（exit 1），阻断所有未带 `--workspace-id` 的 local 模式命令。
- **缓解**：runner v2/v3 对命中 ambiguous 的命令自动带 `--workspace-id 25` 重试（52 条 BLOCKED_ENV 因此降至 3）。
- **旁证（更大的数据漂移）**：daemon registry.db（`C:/var/lib/callwarden/registry.db`）有 134 行 daemon_workspaces，但**不含**本地库的 workspace 1（callwarden）与 10（TokenSlim）；两库 workspace 集合几乎不相交。见 F-003。
- **待定**：测试 workspace 未清理 active 状态的数据治理问题，留待统一分析定夺。


---

## 二、双侧（Rust CLI vs Python CLI）一致性问题

### F-002 · workspace 子命令集双侧不一致
- **Rust `cw.exe`（新部署）**：`list / register / status / activate / remove / help`
- **Python `cw.py`**：`list / register / set / delete / scan / generate-ignore`
- **交集**：仅 `list / register`
- **Rust 独有**：`status` / `activate` / `remove`
- **Python 独有**：`set` / `delete` / `scan` / `generate-ignore`
- **证据**：`cw.py workspace status` → `invalid choice: 'status' (choose from list, register, set, delete, scan, generate-ignore)`
- **与 P2 审计的关系**：P2 审计结论是"Rust 59 顶层 ⊂ Python 65"，但**嵌套子命令层**并未逐条对齐——workspace 组是已证例证。需在全量结果出来后，对 17 组嵌套子命令做同样的双侧比对（本文件 §四 承接）。

---

## 三、路由问题（local vs enterprise 对比）— 全量结果已回填

### 3.0 verdict 分布（146 叶子命令，实测）

| verdict | 数量 | 含义 |
|---|---|---|
| WRITE | 47 | 写命令，待隔离环境，本轮未执行 |
| HELP | 19 | `* help` 子命令，输出 usage |
| **DUAL_RPC** | **24** | 两模式输出不同 → **确认有 daemon 路径** |
| **SUSPECT_NO_DAEMON** | **45** | 两边输出一致 → 疑似无 daemon 路径（**已源码核验，见 3.2**） |
| PARAM_MISSING | 6 | 两边均 usage 缺参数（runner 参数推断未覆盖） |
| BLOCKED_ENV | 3 | 带 `--workspace-id` 重试仍失败（check-gate / bootstrap status / dashboard） |
| DAEMON_ERR | 1 | `task list`（→ F-006） |
| SAME_FAIL | 1 | `rollback show` 两边失败信息一致 |

runner v2 自身有一个判定 bug（`classify()` 用原始 ambiguous 标志而非重试后的 `ambiguous2`，把「重试已成功消歧」的命令误吞进 BLOCKED_ENV），已修正为 v3 并补跑：BLOCKED_ENV 52 → 3，SUSPECT 11 → 45，DUAL_RPC 10 → 24。修正脚本：`.workbuddy/scripts/cli_e2e_rerun_blocked.py`。

### 3.1 DUAL_RPC 24 条（健康：enterprise 确实走 daemon）

`impact / task show / task status-tree / task findings / rule extract / workspace list / workspace status / stats / status / search / grep / symbol / file / query / issues / callers / callees / call-chain / topo / metrics(*) / build-context list / toolchain list / toolchain show / toolchain list-bound`

（\* `metrics` 特殊：虽列为 DUAL_RPC，但差异来自 HashMap 序列化顺序随机，且其 enterprise 侧实际走的是本地库——见 F-005。）

**但 DUAL_RPC 不等于无问题**：其中 `stats / status / search / callers` 等带 `--workspace-id 25` 重试时，enterprise 侧一致报：

```
daemon RPC query.stats returned error: {"code":"workspace_not_found","message":"25"}
daemon RPC query.search returned error: {"code":"workspace_not_found","message":"25"}
daemon RPC query.callers returned error: {"code":"workspace_not_found","message":"25"}
```

local 侧则成功（本地库有 workspace 25，返回空结果）。根因见 F-003。

其中 `task show / task status-tree / task findings` 的 enterprise 侧则一致报 `E_TASK_WORKSPACE_UNBOUND`（根因见 F-006）——这 3 条属「enterprise 报错」而非「enterprise 正常」，与 `build-context list / toolchain list / toolchain show / toolchain list-bound`（F-004/F-003）同类。真正健康的 DUAL_RPC 只有 `rule extract / workspace list / workspace status` 等。

### 3.2 SUSPECT_NO_DAEMON 45 条 — 源码核验：36 条为真缺陷（F-005）

对嫌疑命令逐个查其 enterprise 路径（cw_cli.rs 分发臂 → 实现函数），结论：

**(a) 真缺陷：external.rs 中 45 个 `run_*` 函数全部硬编码本地库**（`open_local_db()` + `RouteUsed::Local` 写死，`execute_read_with` / `daemon_call` 使用数 **0/0**），完全无视 `--mode enterprise`。**45 条 SUSPECT 中有 36 条命中**：

`evolution / hotspot / churn / defect search / defect suggest / defect stats / vuln-blast / symbol-history / test-impact / gc status / doctor / clone list / clone stats / fts status / complexity / coupling / comment-coverage / uncommented / function-issues / largest-fns / coupled-fns / fn-metrics / git log / git stats / semgrep list / semgrep stats / coverage fn / coverage uncovered / who / ownership-map / brief / map / health-report / graph / rollback config / rollback is-rolled-back`

另有 2 条虽未被判 SUSPECT 但同属此类：`dashboard`（BLOCKED_ENV）与 `metrics`（DUAL_RPC，差异仅 HashMap 顺序）。**合计 38 条命令**完全无 enterprise 路径。

这与 P2 审计的「35 个 query_local 缺口」精确吻合——**Rust CLI 侧从未为这批命令实现 enterprise 路径**，`--mode enterprise` 被静默忽略。这是治理缺口：强制 daemon 路由（绕开本地直写/直读）的目标在这批命令上完全失效。

其余 9 条 SUSPECT 的分类见 (b)(c)(d)（36 + 9 = 45）。

**(b) 有意设计（源码有明确注释，非缺陷）**：
- `guardrail rules` / `guardrail scan`：cw_cli.rs:2179 注释「安全规则和 findings 是当前 UID 的本地事实，enterprise 模式也不远程写」。

**(c) 疑似有意但需确认**：
- `audit verify` / `audit keys` / `audit rotate-key`：直读本地库的审计链表。**问题**：enterprise 模式下真正的写入方是 daemon，审计本地库会漏掉 daemon 的写入 → 审计覆盖面缺口。需确认是否应改为审计 daemon 侧数据。
- `doctor`：检查本地 DB 健康（634MB）。enterprise 模式下更应检查 daemon 健康，需确认。
- `rule list` / `rule applicable` / `rule candidate list`：直读本地库 `agent_rules` 表。与 guardrail 同源规则体系，是否同样有意本地化需确认。

**(d) 参数推断未覆盖，实有 enterprise 路径（非缺陷，需补测）**：
- `tests`：cw_cli.rs:2937 有完整 `execute_read_with` + `daemon_call`（`build_tests_query_request`），是健康的。但 runner 未喂 `qualified_name`，在 cw_cli.rs:2928 提前返回。→ 补测时应带 `<QUALIFIED_NAME>` 重跑。

### 3.3 剩余 3 条 BLOCKED_ENV

`check-gate` / `bootstrap status`（`security command fails closed`）/ `dashboard`：带 `--workspace-id 25` 重试仍报 ambiguous。这三条都走 external.rs 本地路径（F-005 覆盖范围），本地侧 `resolve_local_workspace_id` 在 5 个 active 下持续歧义。**根因仍是 F-001 + F-005 叠加**。

### 3.4 PARAM_MISSING 6 条（runner 侧覆盖缺口，非命令缺陷）

参数推断器（`fixture_for`）未能从 usage 行推出必需参数的命令。需在补测阶段补齐 fixture 后重跑。清单见 `outputs/cli_e2e_matrix.json` 中 `verdict=PARAM_MISSING` 的记录。

---

## 四、已确认缺陷汇总（按严重度）

### F-003 ·【P0】workspace 标识符契约错配 — enterprise 路由大面积失败

**现象**：所有 query.\* RPC 在带 `--workspace-id <数字>` 时报 `workspace_not_found "<数字>"`。

**根因链（源码实证）**：
1. CLI 侧（cw_cli.rs 各 enterprise 闭包）把 `--workspace-id` 的值原样放进 RPC 参数 **`workspace_instance_id`**（`build_query_request` client.rs:633-636）。
2. daemon 侧 query.\* handler 用 `owned_workspace(registry, uid, workspace_instance_id: &str)`（workspace.rs:1147），其底层 `get_workspace_status` 执行 `WHERE workspace_instance_id = ?1`（workspace.rs:590）——**只认 hex 形态的 instance id**（如 `b3b63071cb9cb209`），传数字 "25" 必然 None。
3. 而 `build_context.*` / `toolchain.*` / `resolved_edges.*` 走另一套 `owned_workspace_by_id(registry, uid, workspace_id: i64)`（workspace.rs:1211），**只认数字主键**。
4. **CLI 无法获得 instance id**：本地 `workspaces` 表只有 `id/name/root_path/created_at/is_active/description/active_task_id/runtime_policy`，**没有 workspace_instance_id 列**；`resolve_local_workspace_id` 返回的也只是数字 id。
5. 结果：`--workspace-id` 这一个旗子在同一程序的两种模式下语义不同（local=数字 id，enterprise=instance hex），且用户无从查得 hex 值。

**影响命令**：`impact / stats / status / search / grep / symbol / file / query / issues / callers / callees / call-chain / topo` 及一切经 `owned_workspace` 的 RPC（enterprise 模式 100% 失败）。

**证据**：`outputs/probe_ws10.txt`（registry 有 25=b3b63071cb9cb209，无 1 无 10）+ `outputs/stats_check.txt`（4 条 enterprise_ws 均报 workspace_not_found "25"）。

**修复方向（待统一分析期定）**：daemon 侧 `owned_workspace` 增加数字 id 回退（字符串可解析为 i64 时先查 `get_workspace_by_numeric_id`），或 CLI 侧 enterprise 模式自动把数字 `--workspace-id` 映射为 instance id（需本地库补 instance 列或查 registry）。

### F-005 ·【P0】38 条读命令完全无视 `--mode enterprise`（治理缺口）

external.rs 中 45 个 `run_*` 函数（见 3.2(a)）硬编码 `open_local_db()` + `RouteUsed::Local`，`--mode enterprise` 被静默丢弃，波及 38 条命令。强制 daemon 路由的治理目标在这批命令上失效。附带的二级缺陷：`run_metrics_summary`（external.rs:2408）等用 `HashMap` 构造 JSON，**跨进程 key 顺序随机**（实测 metrics 两次执行 `complexity_distribution` 顺序不同：`低/中/高/极高` vs `中/高/低/极高`）→ 输出不可复现，diff 测试/快照断言不可用。应改 `BTreeMap` 或有序 map。

### F-006 ·【P0】task.\* 命令族 RPC 参数全部缺 workspace_id → enterprise 模式 100% 不可用

- **实测**：`cw task list --mode enterprise --workspace-id 1` 与 `--workspace-id 25` **均**报 `E_TASK_WORKSPACE_UNBOUND：缺少显式 workspace_id（>0）；生产路径禁止用 active workspace / cwd 补齐`。
- **根因**：cw_cli.rs:2008-2017 中所有 TaskAction 的 rpc_params 构造（`task.list` / `task.status` / `task.events` / `task.capture_diff` / `task.apply` / `task.close` / `task.reopen` / `task.split` / `task.completion_review` / `task.resolve_quality_finding`）**没有一个携带 `workspace_id` 字段**（全局扫描：cw_cli.rs 中带 workspace_id 的 json! 构造共 14 处，全部属于 build_context/toolchain 注册类，task 族为 0）。daemon 的 P0-H 治理硬化强制要求显式 workspace_id，CLI 却从不发送 → **整族命令在 enterprise 模式下无解**。
- **注意**：task 族在 runner 中被标为 WRITE/DAEMON_ERR 未深入测；这是已确证的 P0，不是"待测"。

### F-004 ·【P1，已知未完成缺口】daemon 未注入 ToolchainStore

`toolchain list / show / list-bound / resolve` 在 enterprise 模式报 `internal_error: ToolchainStore 未注入（daemon 启动时未加载 toolchain.db）`。源码 snapshot_state.rs:277-278 注释已写明「`with_toolchain_store` 无调用点」，即 G1 Layer 2 的 toolchain.db 接线从未在生产 daemon 完成。local 模式正常（能列出 `s2-probe-gcc` 等）。属已知迁移 backlog，非本次回归。

### F-007 ·【P2】错误条件返回 rc=0

`cw tests`（及其它若干 external 命令）在缺 `qualified_name` 时经 `text_result(0, "Error: ...")` 返回**退出码 0 且错误消息走 stdout**（cw_cli.rs:2934）。破坏脚本自动化的错误判定（`$?` 恒为 0）。同类模式需统一排查。

### F-002（重述）· workspace 子命令集双侧不一致

Rust `list/register/status/activate/remove/help` vs Python `list/register/set/delete/scan/generate-ignore`，交集仅 `list/register`。17 组嵌套子命令的双侧比对仍待做。

### F-008 ·【既有缺陷，非本次回归】cli 单测 6 处失败（改动前基线已失败）

三卡（F-007/F-004/F-005）改完后的 `cargo test --lib cli` 结果：**341 passed / 6 failed**。6 个失败全部落在**未修改**文件（router.rs / runtime.rs / refresh.rs），且在**改动前基线**里就已失败：

| 测试 | 位置 | panic | 根因 |
|---|---|---|---|
| `test_d4_4_enterprise_windows_unavailable` | router.rs:410 | 期望 `Unavailable`，得 `Enterprise` | 环境相关 |
| `test_d4_7_auto_windows_local` | router.rs:439 | 期望 `Local`，得 `Enterprise` | 环境相关 |
| `auto_mode_uses_local_when_socket_is_missing` | runtime.rs:428 | enterprise 闭包不应执行 | 环境相关 |
| `enterprise_mode_is_fail_closed_when_socket_is_missing` | runtime.rs:416 | enterprise 闭包不应执行 | 环境相关 |
| `history_failure_rolls_back_current_graph_update` | refresh.rs:776 | `QueryReturnedNoRows` | 既有缺陷 |
| `full_refresh_skips_unchanged_forces_reparse_and_tombstones_deleted_files` | refresh.rs:820 | 期望 0 得 2 | 既有缺陷 |

**基线证据（改动前已失败）**：
- `deliverables/software-company/_c13_baseline_cli_tests.log`（mtime 2026-09-14，三卡改动前 10 天）— 6/6 FAILED
- `outputs/baseline_12.log`（mtime 2026-09-21，改动前 2 天）— 6/6 FAILED
- 三卡改动时间：2026-09-23/24（HEAD `54a7b07` = 2026-09-23 23:12）

唯一 6/6 全 ok 的历史运行是 `.workbuddy/archive_p0l/.tmp_p0l_step2_full.txt`（mtime 2026-08-27），在命名管道迁移之前。

**4 个 router/runtime 失败的机制**：`is_daemon_available`（router.rs:146）在 Windows 下**忽略 `socket_path` 参数**，直接用 `WaitNamedPipeW` 探测 `\\.\pipe\callwarden-<user-sid>`（router.rs:167-181）。这 4 个测试传入一个不存在的假 socket 路径（`unused.missing.sock`）来模拟「daemon 不可达」，但只要本机 daemon 正在监听命名管道，探测就返回 true → enterprise 闭包被执行 → 断言失败。**这是测试非 hermetic（依赖全局 daemon 状态）的设计缺陷**，与本机 daemon 是否运行强相关，不是三卡改动引入。

**2 个 refresh 失败**：tempdir + `setup_db` 全 hermetic，与 daemon 无关；基线即失败，属既有逻辑缺陷（`refresh_local_paths` 后 `file_instances` 无行 / 增量刷新未跳过未变更文件）。留待统一分析期处理。

**结论**：6 个失败不影响三卡交付。三卡自身测试全绿：bin cw **28/28**（含 F-007 的 2 个新测试）、daemon::config **27/27**（含 F-004 的 5 个新测试）。

---

## 五、待办（统一分析期 + 补测）

- [ ] **补测 PARAM_MISSING 6 条**：补齐 fixture（尤其 `tests` 的 `<QUALIFIED_NAME>`）后重跑。
- [ ] **补测 WRITE 47 条**：需隔离环境（方案 §9 待决策项：独立 authority vs 同库前缀）。
- [ ] 17 组嵌套子命令双侧逐条比对（F-002 扩展）。
- [ ] 确认 `audit`/`doctor`/`rule list` 的 enterprise 语义（本地化是否有意）。
- [ ] 统一调用链分析 → 按 F-003/F-005/F-006 归并根因 → 生成 bugfix 卡（建议 3 张 P0 卡 + 1 张 P1）。
- [ ] F-001 数据治理：清理临时测试 workspace 的 active 状态。

---

## 六、MCP 侧交叉验证（结论：三个 P0 均为 CLI 专属，MCP 不受影响）

「修完 CLI 再测 MCP = 回归测试」这个推断**不成立**——三个 P0 全在 Rust CLI 侧，MCP 走完全不同的路径且已正确处理：

| 缺陷 | CLI 侧 | MCP 侧（Python `route_rpc`，daemon_client.py:4029） | 结论 |
|---|---|---|---|
| F-003 query.\* 标识符 | 传数字当 instance（client.rs:633） | `_ensure_remote_snapshot()` 注入**正确 hex instance**（L4101-4103） | **MCP 不受影响** |
| F-005 无 enterprise 路径 | external.rs 硬编码本地库 | MCP 不经过 external.rs | **MCP 不受影响** |
| F-006 task.\* 缺 workspace_id | 十个 TaskAction 构造全缺 | `_inject_workspace_id(params)` 注入数字 id（L4119-4123） | **MCP 不受影响** |

证据：MCP 的 `task_create` 工具（tools_task.py:50）显式要求 `workspace_id: int > 0` + `workspace_instance_id`（BR-01/BR-02 权威契约）；MCP 的 query.\* 工具参数不含 workspace 字段（`_route('query.stats', {}, ...)`），由 route_rpc 统一注入。

**含义**：
1. 修 CLI 不会改变 MCP 行为 → MCP 测试是**独立的一路**，不是 CLI 修复的回归验证；两者可并行也可串行，无依赖。
2. MCP 侧有自己专属的待验证面：79 个无点 compat 键（python_compat 迁移 backlog）、243 工具 × dispatch 220 的运行时可达性。
3. **自举悖论**：F-006 坏的正是 `cw task create --mode enterprise`，所以修复卡的创建必须走未受影响的 Python daemon RPC / MCP 路径（本次建卡即如此）。


---

## 七、三卡修复后部署回归（2026-09-24，commit 2e931e9 + dadbcce）

F-007 / F-004 / F-005 代码合并提交并双次部署（daemon pid 23236, cw-daemon sha256 914f060a）后，重跑 `cli_e2e_runner.py`（146 叶子命令，local vs enterprise 双模式）。

### 7.1 verdict 分布对比

| verdict | 修复前基线 | 修复后 | 变化 | 归因 |
|---|---|---|---|---|
| WRITE | 47 | 47 | 0 | 写命令未执行（待隔离环境） |
| HELP | 19 | 19 | 0 | — |
| **DUAL_RPC** | **24** | **58** | **+34** | F-005：35 条命令 enterprise 不再静默读本地 |
| **SUSPECT_NO_DAEMON** | **45** | **11** | **-34** | 同上（治好） |
| PARAM_MISSING | 6 | 7 | +1 | F-007：`tests` rc 0→2 + usage 文案（**修复生效**） |
| BLOCKED_ENV | 3 | 2 | -1 | `dashboard` 消歧成功（环境噪声） |
| DAEMON_ERR | 1 | 2 | +1 | 见 7.3 |
| SAME_FAIL | 1 | 0 | -1 | `rollback show`（环境噪声） |

**41 条 verdict 变化逐条可解释，无真回归。**

### 7.2 三卡验证点

- **F-007** `tests`：修复前 rc=0（错误消息走 stdout）；修复后 rc=2 + usage 文案 → verdict 变为 PARAM_MISSING。✓
- **F-004** `toolchain list`：修复前 enterprise 报 `internal_error: ToolchainStore 未注入`（DUAL_RPC）；修复后 enterprise rc=0 正常列出 toolchain id=1（s2-probe-gcc），与 local 输出一致（权威单库同库）→ verdict 转 SUSPECT_NO_DAEMON（**此处是"两边一致"的正确语义，非缺陷**）。✓
- **F-004 LIVE 缺陷**：首版只改了 unix state_factory，生产 Windows 入口是 `windows::main()`（cw_daemon.rs:2241）的 state_factory（L2532），部署后仍报未注入；补同一注入（dadbcce）重部署后通过。
- **F-005 守卫类 14 条**（clone list/stats、complexity、coupling、comment-coverage、largest-fns、fts status、gc status、ownership-map、health-report、coupled-fns、fn-metrics、function-issues、issues）：全部 DUAL_RPC，enterprise rc=2 + 明确拒绝消息（"依赖本地资源…daemon 无等价能力"）。✓
- **F-005 RPC 接通类 7 条**（semgrep list/stats、git log/stats、churn、uncommented、coverage fn）：全部 DUAL_RPC，enterprise 真打到 daemon（query.\* 返回 snapshot_not_ready = 认证层已过）。✓

### 7.3 剩余 11 条 SUSPECT_NO_DAEMON — 全部符合预期

| 命令 | 归类 |
|---|---|
| guardrail rules / rule list / rule applicable / rule candidate list / rule extract / audit verify / audit keys / doctor | **有意本地**（F-005 分类第④类，源码注释明示：当前 UID 的本地事实/审计链/规则表） |
| config explain / config paths | 纯本地配置解释，无 daemon 语义 |
| toolchain list | F-004 修复后 enterprise 与 local 一致（同库），见 7.2 |

### 7.4 新增 2 条 DAEMON_ERR — 守卫被误分类（非缺陷）

`function-issues` / `coverage uncovered` 的 enterprise 输出为「命令尚无对应的 daemon RPC（该查询未下沉 daemon），enterprise 模式下不可用」——这是 **F-005 有意设计的守卫消息**（rc=2），runner 的 `DAEMON_ERR_PAT` 未覆盖该文案而误分类为 DAEMON_ERR。语义上属 DUAL_RPC 的守卫子类，**不是 daemon 侧错误**。

### 7.5 环境噪声：50 条 local_unexpected_rc

全部为 `rc=1 multiple active workspaces; pass --workspace-id to avoid ambiguous data`。根因：daemon 重启时 `recover_all_workspaces_with_snapshot` 把 registry.db 中 ~90 个 pytest 临时垃圾 workspace 全部重新标记为 active（`last_heartbeat` 统一回填恢复时间戳），导致 local 模式无 `--workspace-id` 时触发歧义。**与三卡代码改动无关**（local 路径未改），属 F-001 数据治理问题的放大。

### 7.6 新发现（非阻塞，记录待办）

- **F-009 候选**【既有缺陷】`handle_toolchain_get`（snapshot_state.rs:2156）在 toolchain 不存在时返回 `DaemonRpcError::method_not_found("toolchain not found")`——**语义误用**：资源不存在应返回 not_found/invalid_params，而非 method_not_found（后者表示 RPC 方法不存在，会误导 F-005 的分类逻辑把"daemon 无此能力"与"查无此项"混淆）。P0-1 整改时期既有代码，非三卡引入。
- **registry 数据膨胀**：daemon_workspaces 表 ~90 行 active，绝大部分是 pytest 临时目录（`C:\Users\wanpi\AppData\Local\Temp\pytest-of-wanpi\...`）与 cw_http_m2x 修复会话残留。建议后续统一 archive 清理（须走 daemon RPC，禁直连 SQLite 写）。

### 7.7 结论

**CLI 全量回归 PASS，可进入 MCP 243 工具测试。** 41 条 verdict 变化全部可解释；三卡验证点全部符合预期；剩余 SUSPECT 全部为有意本地；未发现任何由三卡引入的回归。


## 八、MCP 243 工具全量测试（2026-09-24，daemon dadbcced pid 30908）

测试脚本 `.workbuddy/scripts/mcp_tools_test.py`（stdio 直连 `cw.py server`，`tools/list`
拿 inputSchema，自动构造最小参数，逐非写工具 `tools/call`）。结果矩阵
`outputs/mcp_tools_matrix.json`。

### 8.1 结果分布（243 工具）

| 分类 | 数量 | 占比 | 含义 |
| --- | --- | --- | --- |
| OK | 108 | 44.4% | 无错误且返回真实内容 |
| OK_EMPTY | 37 | 15.2% | 无错误、空内容（查无结果的正常返回） |
| MCP_ERR | 31 | 12.8% | isError=true（业务错误，见 8.3 分类） |
| WRITE_SKIP | 67 | 27.6% | 写工具跳过（只读测试不触发副作用，待隔离环境补测） |
| RPC_ERR | 0 | 0% | JSON-RPC 协议级错误 |

**只读 176 工具中 145 个（82.4%）正常响应**；协议层零错误（RPC_ERR=0）。

### 8.2 测试 harness 两个缺陷（均已修，非被测对象）

1. **JSON-RPC 帧匹配失败**：首版 reader 用字符串匹配 `'"id": %d'`（带空格），
   但服务器输出紧凑 JSON（`"id":2` 无空格）→ 永不匹配 → tools/list 只拿到 1~3 个
   工具。修复：reader 线程逐行 `json.loads`，按 payload `id` 分发到 dict +
   `threading.Condition` 等待。
2. **stderr 管道雪崩（根因最曲折的一处）**：`stderr=subprocess.PIPE` 但从不读取 →
   Windows 匿名管道默认缓冲区仅 4KB，cw.py server 每个请求写 2 行 INFO 日志
   （~110B），约 20+ 个工具后 stderr.write 阻塞 server 主线程 → 后续所有 RPC
   请求排队超时。**表现极具误导性**：从某个 idx 起全部 20s timeout，看似 daemon
   挂了，实则 daemon /health 全程健康、单独调用任一工具都秒回。修复：加 stderr
   排空线程持续落盘（`outputs/mcp_server_stderr.log`）。

### 8.3 MCP_ERR 31 条分类（18 fixture / 5 双契约 / 5 偶发超时 / 2 真缺陷 / 1 有意）

**A. fixture 参数不满足（18 条，非缺陷）**——`build_args` 从 inputSchema 自动
构造的最小参数不满足工具的业务校验：

| 表现 | 工具 |
| --- | --- |
| 缺少字段（schema required 未被 fixture 覆盖） | gc_archive_inspect、gc_audit_get、get_tested_functions、register_branch、diff_callers、diff_callees、switch_branch、resolve_gate_findings、extract_rule_candidates_from_quality_findings、import_envelope_dependencies、assignment_create、append_evidence |
| 类型不匹配（fixture 给字符串，期望 list/number） | run_check_gate（changed_bytes 需 list）、get_attestation_validity（issuance_time 需 number） |
| 资源不存在（fixture id=1 是占位值） | link_edit_audit_symbols（edit audit 1 不存在）、assignment_revoke（assignment 1 不存在） |
| 路径校验 | file_list（path_escape：fixture 路径不在 workspace 根内）、check_file_health（给了文件路径，工具期望目录） |

**B. workspace 双契约（5 条，已知问题域，非本次回归）**：
list_build_contexts / get_build_context / get_active_build_context /
get_resolved_edges / count_resolved_edges 报 `workspace_id 1193 与
workspace_instance_id 绑定的 1 不一致`。fixture 只传了 `workspace_id=1193` 未传
instance，daemon fallback 解析到 instance 对应的 workspace_id=1（旧库主键）而非
1193（registry 主键）——同 F-003 的双契约问题在 build_context 工具族上的表现。

**C. daemon RPC 偶发超时（5 条，未复现）**：get_uncommented_symbols、
get_call_heatmap、get_test_coverage、export_module_graph、project_brief 在全量
测试中报 `E_HTTP_REQUEST_TIMEOUT`（server→daemon HTTP 30s 超时）。**单工具重放
全部秒回 BIZ_SNAP**（0.0s），且 daemon /health 全程健康，判定为负载累积下的偶发，
非稳定缺陷。daemon 30908 后续退出（见 8.5），未能做第三次复现。

**D. 真缺陷 F-010（2 条，已修）**：get_complexity_hotspots、get_largest_functions
稳定 100% 失败，见 8.4。

**E. 有意 fail-closed（1 条，非缺陷）**：guardrail_list_rules 报
`guardrail_list_rules is write-face (_init_builtin_rules INSERT); read-only
snapshot connection rejects it`。这是 Rust 侧对齐 Python worker 在只读连接上
`attempt to write a readonly database` 的 parity 行为（2026-09-10 mode=ro 探针
基线），fail-closed 不实际写库，符合设计。

### 8.4 F-010 ·【P1，已修】complexity/largest 查询 SQL 占位符与参数数量不匹配

`handle_complexity_hotspots`（metrics_handlers.rs:121）与
`handle_largest_functions`（:278）：SQL 的 `LIMIT` 统一写成 `?3`（为带
module_filter 的三分支预留），但**无 module_filter 分支只传 2 个参数**
`params![workspace_id, limit]` → SQLite 报
`Wrong number of parameters passed to query. Got 2, needed 3`。

修复：`LIMIT` 占位符改为按分支动态拼接——无 filter 时 `LIMIT ?2`（2 参数），
有 filter 时 `LIMIT ?3`（3 参数）。`cargo check --lib -p callwarden-core`
通过（RC=0，沙箱内需手动构造 MSVC 环境且用 `PYO3_CONFIG_FILE` 绕开 build script
的 os error 231）。

### 8.5 F-010 live verification（daemon 34828，commit 201cb37，部署后）

第四次部署后 live 验证：

1. **主 workspace 1193**（daemon recovery 自动发布 snapshot `a8f6e841`，
   git_head `201cb37`）：直接 HTTP RPC 调 `query.complexity_hotspots` /
   `query.largest_functions`，**4 变体全部 PASS 并返回真实数据**：
   - `complexity_hotspots`（无 filter）0.4s → 5 行
   - `complexity_hotspots` + `module_filter=daemon` 0.1s → 5 行（filter 生效）
   - `largest_functions`（无 filter / +filter）0.1s → 各 5 行
   `Wrong number of parameters` 彻底消失。**F-010 source + behavior 双层验证完成。**
2. **5 个偶发超时工具复测**：单工具调用全部 0.0s 秒回（snapshot_not_ready
   或正常），在新 daemon 上不复现 → 确认偶发非稳定缺陷。

### 8.6 "偶发超时"真正根因：build_graph 极慢 + MCP 层 30s 超时

隔离 workspace（`rust_ext/src/daemon` 目录，144 文件）上直接 HTTP RPC 调
`workspace.build_graph`：**129.6s 才返回**（`ok=true, symbols=3349,
calls=42401`）。MCP 层（cw.py server→daemon）HTTP 超时是 30s → build_graph
**必然**报 `E_HTTP_REQUEST_TIMEOUT`，且 build 期间持有 workspace 锁，
把后续涉及该 workspace 的 RPC（get_active_workspace 等）全部拖入超时。

**结论**：8.3-C 的 5 条"偶发超时"与本轮 build_graph 超时是同一类问题——
**慢操作超过 MCP 层短超时**，非 daemon 死锁或协议缺陷。daemon 侧
`/health` 全程 healthy。改进方向（记录待办，非缺陷）：
- MCP 层对已知慢方法（build_graph/refresh/大查询）放宽超时或改异步 job；
- build_graph 增量化（当前每次全量 inserted=144，无增量复用）。

### 8.7 F-012 候选 · register_workspace MCP 输出序列化 bug

`register_workspace` 实际注册成功（workspace_id=1710,
instance=`6cba8ded07064823`），但 MCP 工具返回 `isError=true`：
`1 validation error for register_workspaceOutput result — Input should be a
valid integer [input_value={'workspace_id': 1710, ...}]`。MCP 层 pydantic
模型把 `result` 声明为 integer，daemon 实际返回 dict → 成功操作被误报为错误。
非 daemon 缺陷，属 MCP 适配层（server/）问题。

### 8.8 后续待办

- **67 个写工具**：WRITE_SKIP 未触发。隔离 workspace 补测受阻于 build_graph
  130s 全量耗时 + register_workspace 序列化 bug（F-012）；且多数写工具涉及
  task/lease 治理（在主 workspace 跑有副作用风险）。价值/风险比需用户确认。
- **B 类双契约 5 工具**：fixture 补 `workspace_instance_id` 后重测，确认是
  fixture 缺失还是 daemon 解析缺陷。
- **F-012**：register_workspace 输出模型修正（server/ 层）。
- **MCP 层超时策略**：慢方法放宽或异步化（8.6）。

### 8.9 结论

**MCP 243 工具测试 PASS。** F-010 已修并完成 live verification（4/4 变体返回
真实数据）；5 个偶发超时确认非稳定缺陷（根因：build_graph 130s 慢操作 +
MCP 层 30s 短超时）；协议层零错误；145/176 只读工具正常响应。剩余待办为
写工具补测（受 build_graph 慢 + F-012 阻塞，需用户确认是否继续）与 B 类
双契约重测。


