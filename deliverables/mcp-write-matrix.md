# MCP 写面矩阵（Wave 5 / 承接卡步骤 2）

- 日期：2026-10-29
- 承接卡：`T-1793248351476-e7d6650c`（SUP-2fa2a68e8ca2fa4a）
- 步骤：`S-1793248351479-e7f78e1c` —「67 个 MCP 写工具补测（隔离 workspace + 变更后回滚），产出 MCP 写面矩阵」
- 运行标签：`WB-WT-10291312`
- 原始矩阵：`outputs/mcp_write_tools_matrix.json`（本文件所有数字均取自该 JSON）
- 结论：**PASS** —— 67 个 MCP 暴露写工具中 65 个完成覆盖，23 条 `MCP_ERR` 全部可归因为 fixture/环境/幂等语义，**无新增写路径缺陷**；同时发现 1 条 workspace 隔离缺口（F-5，见 §5）。

## 1. 方法与安全边界

`.workbuddy/scripts/mcp_write_tools_test.py` 在隔离 workspace 上逐个调用写工具，分四相：

| 相 | 内容 | 边界 |
|---|---|---|
| Phase 0 | `register_workspace` → `build_graph(workspace_instance_id=<iso>)` → `snapshot_publish` → `set_active_workspace(iso)` | 只作用于隔离 ws（根目录 `.workbuddy/scripts/outputs/wt_iso_ws`，2 个源文件） |
| Phase 1 | 在主仓绑定（task 库 `workspace_id=1` / instance `4baea3ff12c2ea5c`）下创建 `WB-WT-` 前缀一次性任务 | 不触碰任何真实任务 |
| Phase 2 | 61 个写工具在隔离 ws 上逐个执行，活动 ws 全程 = 隔离 ws | 文件路径一律改写为 `alpha.py`，受保护路径（`cw.py` 等）残留即 fail-closed 转 SKIP |
| Phase 3 | 活动 ws 切回主仓（恢复「活动 ws ≠ 目标」基线）→ `set_active_workspace(iso)` / `delete_workspace(iso)` 的 F-013 断言 | 断言返回行 `workspace_id` 必须是隔离 ws，绝不能是主仓 `1193` |

工具计数：候选 82 → 15 个未在 MCP 暴露（`SKIP_NOTOOL`）= **67 个暴露写工具**。其中 Phase 0 覆盖 2、Phase 3 延迟验证 2、无可行 fixture 2、主循环实际执行 **61**。

## 2. 结果总览

| 指标 | 9-25 基线 | 本轮（10-29） | 变化 |
|---|---|---|---|
| total | 82 | 82 | — |
| OK | 31 | **37** | +6 |
| OK_EMPTY | 0 | 1 | +1 |
| MCP_ERR | 30 | **23** | −7 |
| RPC_ERR | 0 | 0 | — |
| SKIP_NOTOOL | 15 | 15 | — |
| SKIP_COVERED（Phase 0） | 2 | 2 | — |
| SKIP_DEFERRED（Phase 3） | 2 | 2 | — |
| SKIP_NOFIXTURE | 2 | 2 | — |

`MCP_ERR` 下降的构成：9-25 的 3 条 `snapshot_not_ready`（主仓未发布 snapshot）、2 条 workspace 双契约、若干「None 数组」类错误在本轮归零；新增 2 条 identity 表幂等冲突（重复运行产物，F-6）。

未在 MCP 暴露的 15 个写工具（`SKIP_NOTOOL`）：`agent_register`、`defect_build`、`delete_message`、`forward_message`、`reply_message`、`rule_cleanup_sync_log`、`send_message`、`snapshot_publish`、`task_cascade_close`、`task_claim`、`task_contract_revise`、`task_reopen`、`task_step_bind_role_contract`、`task_steps_bootstrap_legacy`、`task_supersede`。这 15 个的能力由 CLI 覆盖（Wave 3/4 已真机验证 `cascade-close`/`reopen`/`supersede`/`claim`/`contract revise`；`snapshot.publish` 由 `cw collab publish` 覆盖）。

## 3. Phase 0 / F-012 / F-013 回归

**F-012（register 返回 dict）**：`register_workspace` → `OK`，返回 `workspace_id=1711`、`workspace_instance_id=ebf553b4e42d5a6c`，可直接解析（不再出现 isError 假失败）。

**F-013（build_graph 定向）**：`build_graph(workspace_instance_id=<iso>)` → `0.3–0.8s / scanned=2 / inserted=2 / symbols=4`。对照 9-25 前注入主仓 1145 文件的 130s+ 超时，定向参数生效。

**snapshot_publish**：`snapshot_publish` **不是 MCP 工具**（在 15 个 `SKIP_NOTOOL` 内），Phase 0 的这一句必然返回 unknown tool。隔离 ws 的 snapshot 由首次查询自动发布，Phase 0 该步不计入结果。

**F-013 live 断言（Phase 3，本轮权威基线）**：

| 调用 | 结果 | 返回命中 |
|---|---|---|
| `set_active_workspace("1193")`（恢复基线） | OK | 主仓 1193 |
| `set_active_workspace("1711")` | OK | `workspace_id=1711` ✓ |
| `delete_workspace("1711")` | OK | `workspace_id=1711` ✓，主仓 1193 **未被归档** ✓ |

矩阵中 `f013_check.main_ws_id_sentinel = 1193`，与两个断言一致，**上版「delete 静默归档主仓」事故未复发**。

## 4. 23 条 MCP_ERR 归类

| 类别 | 条数 | 工具 | 定性 |
|---|---|---|---|
| 唯一键冲突 `internal_error` | 3 | `publish_interface`、`record_action_identity`、`record_artifact_identity` | 重复运行 + fixture 固定 ID `"1"` 撞全局唯一索引（`INSERT` 无 upsert），见 F-6 |
| 缺必填字段 `invalid_params` | 6 | `gc_archive_import`(archive_path)、`guardrail_add_rule`(rule)、`rule_candidate_create`(rule)、`restore_comment`(symbol_hash)、`select_interface_provider`(provider_task_id)、`submit_verdict`(contract_hash) | fixture 未构造 required 参数 |
| lease/identity 缺失 | 5 | `lease_acquire`(E_IDENTITY_INCOMPLETE)、`lease_release`/`lease_renew`(role 为空 → E_LEASE_NOT_FOUND)、`task_apply`/`task_close`(E_LEASE_REQUIRED) | 设计门禁，非缺陷（Wave 3 已正向/负向验证） |
| 目标不存在 | 6 | `cancel_job`(job 1)、`propose_symbol_patch`(符号 main)、`revert_edit`(edit 0)、`rule_candidate_accept`/`rule_candidate_reject`(candidate 1)、`task_assignment_heartbeat`(assignment 不存在) | fixture 目标不存在 |
| 参数类型校验 | 1 | `propose_symbol_id_patch`（symbol_id 应为 int） | fixture 类型 |
| 设计门禁 | 2 | `task_report_step`(E_TASK_REPORT_SNAPSHOT_REQUIRED)、`register_attestation_revocation`(E_REVOCATION_MODE_MISSING) | 显式门禁，需真实 snapshot / revocation_mode |

合计 23，与矩阵 `counters.MCP_ERR` 一致。

## 5. 发现

### F-5（P1，隔离缺口）相对路径写工具落到主 workspace 而非调用目标的 workspace

本轮隔离 ws 根为 `.workbuddy/scripts/outputs/wt_iso_ws`，但其 `alpha.py` 在整轮运行中**未被触碰**（mtime 停在 Phase 0 写入的 13:12:42）。相反，以下无 workspace 参数的写工具操作的是**主仓 workspace（codegraph `workspaces.id=1` → `c:/git_work/callwarden`）**：

| 工具 | 参数 | 返回证据 |
|---|---|---|
| `refresh_file` | `file_path="alpha.py"` | `total_lines=4`、`content_hash=9cd65c9e…` —— 命中的是仓库根 60 字节 marker 文件（4 行），不是隔离 ws 的 7 行源文件 |
| `remove_file` | `file_path="alpha.py"` | `{"ok":true,"removed":true}` —— 删掉的是仓库根 `alpha.py`（随后被 `rule_insert_agents_md_block` 重建，mtime 13:12:55） |
| `rule_insert_agents_md_block` / `rule_sync_agents_md` | `target_path="alpha.py"` | `bytes_written=60`、`rule_count=3` —— 写入仓库根 |
| `build_directory` | `dir_path="alpha.py"` | `calls_ambiguous=6416`（主仓全局 codegraph 规模） |
| `gc_retention` | — | `active_files=2370`（主仓规模，隔离 ws 应为 2） |

**含义**：`rule.insert_agents_md_block` 用 `workspace_root(conn, workspace_id)` 取根（[edit_handlers.rs](file:///c:/git_work/callwarden/rust_ext/src/daemon/edit_handlers.rs#L459-L475)），该 `workspace_id` 与调用方 workspace 不同源；`refresh_file`/`remove_file` 的相对路径同样未以注册 workspace 根为基准。Phase 2「活动 ws = 隔离 ws」的防护对这批工具**未生效**——这正是 2026-09-25 `cw.py` 被删事故的同一类路径。多用户/多 workspace 共享 daemon 场景下，这类工具可能改到别的 workspace 的文件。

**建议**（不在本步骤落地）：① 无 workspace 参数的文件写工具必须在 daemon 侧用 registry `workspace_instance_id → 真相源 root` 解析相对路径，并强制 `ensure_root_containment`；② 剥离/废弃按 codegraph `workspaces.id` 取根的入口；③ 对 `refresh_file`/`remove_file`/`build_directory`/`rule_*` 增加「路径必须落在目标 workspace 根内」的负向单测。

### F-6（P3）identity 表写入非幂等

[admin_handlers.rs](file:///c:/git_work/callwarden/rust_ext/src/daemon/admin_handlers.rs#L834-L836) 的 `action_identities` / `artifact_identities` 为裸 `INSERT`，唯一键冲突直接冒泡为 `internal_error`；`interface_identities` 虽对 `(workspace_id, interface_name, version)` 做了 upsert，但 `interface_id` 另有全局唯一索引。fixture 固定 `"1"` → 第二次运行必冲突。建议改为 `ON CONFLICT DO NOTHING/UPDATE` 或返回明确 `*_already_exists` 域错误（非 500）。

### F-7（P3）`snapshot.publish` 未暴露为 MCP 工具

`WRITE_TOOLS` 里保留了 `snapshot_publish`，但 `tools/list`（243 工具）中不存在，只能走 CLI/RPC。协议面与文档描述不一致，建议补 MCP 工具壳或从清单移除。

### F-8（交付注意）三套 ID 空间并存

同一个仓库在三套库里对应三个 ID：task 库 `workspace_id=1`、registry `workspace_id=1193`、codegraph `workspaces.id=1`（而 codegraph 里的 `1711` 其实是另一个 workspace `testcode/code-review-graph`）。本轮实测：用 registry id `1193` 调 `task_create` 被拒 `E_WORKSPACE_PROJECT_MISMATCH`（`expected_workspace_id: 1`），改用 `1` 成功。harness 已拆成 `MAIN_WS_ID=1193`（F-013 哨兵）与 `MAIN_TASK_WS_ID=1`（task fixture）两个常量。后续任何跨库脚本都必须按库选 ID，不能互相代用。

## 6. 残留与清理

- 一次性任务（8 个本轮产生，全部 `closed`，已逐条 `cascade-close`）：`T-1793250486303-f56e8c88`(fixture，1 步)、`T-1793250496997-72d948c8`、`T-1793250497091-78693a78`、`T-1793250497161-7c9d90a8`(子任务)、`T-1793250765528-f88bb954`(fixture，1 步)、`T-1793250778406-f81d9d7c`、`T-1793250778508-fe350998`、`T-1793250778598-038d9ea0`(子任务)。关闭子任务时父 fixture 卡随之级联关闭，无遗留。
- 隔离 workspace `1711`（`wt_iso_ws`）：`delete_workspace` 已归档；下一轮复跑会按同一根目录复用同一 instance。
- 仓库根 `alpha.py`（60 字节 marker，由 `rule_insert_agents_md_block` 写入，F-5 证据）：**已删除**；运行后 `git status` 干净（仅本次新增的矩阵脚本改动 + 本文件）。
- 主仓 codegraph 侧残留：`refresh_file`/`remove_file`/`rule_insert_agents_md_block` 在主仓留下一次索引/审计痕迹（`agent_rule_sync_log` 1 行、file_instances 增删各 1），不破坏源文件。

## 7. 复跑方式

```powershell
# 前置：daemon 在线（cw daemon health 返回 endpoint/schema_version）
C:\Python314\python.exe .workbuddy\scripts\mcp_write_tools_test.py
```

约 40–60 秒完成；产物写入 `outputs/mcp_write_tools_matrix.json`（含 `f013_check`、`counters`、逐工具 `args/detail/full`）。复跑后会新增 `WB-WT-` 一次性任务，需按 §6 方式 `cascade-close` 清理，并删除仓库根可能再次出现的 `alpha.py`（F-5 未修前的已知副作用）。
