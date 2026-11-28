# daemon 资源治理面 — 任务图与建单回执（R4）

- **feature**: daemon-resource-governance
- **阶段**: R4（建单回执；唯一 REQ↔卡片↔task_id 映射表）
- **设计基线**: `02f761a`（.design/daemon-resource-governance/ 六工件基线提交，2026-11-28）
- **建单时间**: 2026-11-28 10:27-10:31 +0800
- **workspace 配对**: workspace_id=1 / instance=4baea3ff12c2ea5c（identity_policy=legacy_identity_v1，daemon 回执为准）
- **建单通道**: `cw task create` CLI（daemon PID 28728 存活；MCP task_create 对 steps 数组有序列化问题，沿用 preq §13 先例走 CLI 直连）

## 批次与任务表（8 张，23 steps）

| # | task_id | REQ | 卡片（符号锚定 title） | steps | blocked-by | 批次 |
|---|---|---|---|---|---|---|
| T1 | `T-1795832874419-0bc0dac8` | REQ-RG-01 | `rust_ext/src/daemon/resources.rs:ResourceConfig implement 环境侦测与 daemon.toml [resources] 配置解析模块` | 4 | 无（根任务） | 1 |
| T2 | `T-1795832914331-56a90368` | REQ-RG-01 | `rust_ext/src/bin/cw_daemon.rs:main implement 启动接线资源侦测写回 [resources] 与恢复日志` | 3 | T1 | 2 |
| T3 | `T-1795832915536-9e838eec` | REQ-RG-02 | `rust_ext/src/snapshot.rs:SnapshotCache implement 真实 LRU 空闲驱逐与字节预算` | 3 | 无 | 1 |
| T4 | `T-1795832951726-0b99c5fc` | REQ-RG-02 | `rust_ext/src/daemon/snapshot_state.rs:get_store_or_load implement 查询路径冷加载自愈` | 3 | T3 | 2 |
| T5 | `T-1795832992065-7000fb9c` | REQ-RG-02 | `rust_ext/src/bin/cw_daemon.rs:main implement resource tick 空闲驱逐与字节预算驱逐` | 2 | T2 T3 | 3 |
| T6 | `T-1795832952572-3e03e5f4` | REQ-RG-03 | `rust_ext/src/bin/cw_daemon.rs:recover_all_workspaces_with_snapshot audit 启动加载路径盘点证据与回归锁` | 2 | 无 | 1 |
| T7 | `T-1795832993019-a8df41f8` | REQ-RG-04 | `rust_ext/src/daemon/fs_handlers.rs:run_build_graph implement build 线程低优先级注入` | 2 | T1 | 2 |
| T8 | `T-1795833067655-097fc648` | REQ-RG-05 | `rust_ext/src/daemon/resources.rs:MemoryGovernor implement 内存软限状态机与 build gate 联动` | 4 | T1 T5 | 4 |

建议认领顺序：**批次1**（T1/T3/T6 并行）→ **批次2**（T2/T4/T7 并行）→ **批次3**（T5）→ **批次4**（T8）。

> `blocked-by:` 仅为 description 文本约定，daemon 不强制；派工时须按上表批次显式确认前置单已闭环。同文件冲突提示：T2/T5/T8 都动 `cw_daemon.rs` 主循环区域（已按批次串行）；T4 动 `snapshot_state.rs` 大范围调用点切换，与 T3（snapshot.rs）不同文件可并行。

## REQ ↔ AC ↔ 任务覆盖

| REQ | AC（ac-map.md） | 任务 |
|---|---|---|
| REQ-RG-01 | AC-RG-01-1 ~ 01-5 | T1, T2 |
| REQ-RG-02 | AC-RG-02-1 ~ 02-6 | T3, T4, T5 |
| REQ-RG-03 | AC-RG-03-1 ~ 03-3 | T6 |
| REQ-RG-04 | AC-RG-04-1 ~ 04-2 | T7 |
| REQ-RG-05 | AC-RG-05-1 ~ 05-5 | T8 |

21 条 AC 全部有承载任务与自动化证据方式，无遗漏。

## 建单回执核验（G6）

- [x] title 全部符号锚定、无行号
- [x] description 首行全部 REQ-RG-xx（无 REQ-GAP 需求：5 个 REQ 均有 formal 编号）
- [x] check_items 全部为**字符串**（T1 经 task_status RPC 抽检确认：4 个 step 的 check_items 均为可读字符串，非转义 JSON 数组）
- [x] steps 全部非空（2-4 步/单，短任务模式：每步可独立 commit+report）
- [x] action 只用 implement/fix/test/audit（本批 15 implement + 2 test + 2 audit + 4 implement=23 steps 无保留值）
- [x] 三角色合同自动注入（executor/reviewer/adjudicator，A' 模板）
- [x] workspace 配对 + capture/binding/assignment 回执齐全（T1 抽检：tb-/wc-/A- 三 id 在案）
- [x] 治理状态 READY（next_action=claim_current_step，executor 可直接领取）

## 交接包清单

`.design/daemon-resource-governance/`：`requirements-brief.md` + `ac-map.md` + `mindmap.md` + `flowchart.md` + `symbol-anchor.md` + `contracts.md` + `task-map.md`（本文件）+ `payloads/t{1..8}-steps.json`（建单 payload 存档）。

executor 用自身注册身份领取，身份不跨会话借用；评审/执行身份隔离预判留给 Phase 3（主理人治理约束）。

## 未决项回执（详见 contracts.md §2.4）

- O1（assumption，映射表数值）——不阻塞建卡结构，T1 常量可调
- O2（assumption，磁盘侦测 fail-soft）——T1 容错分支覆盖
- O3（open，approximate_bytes 口径 ±20%）——T3 实现时校准
- O4（open，[resources] 写回竞态）——T2 临时文件+rename+存在即跳过
- O5（open，IO 优先级）——v1 范围已收敛，无阻塞
- O6（open，history 自动 GC）——v1 明确不动（T5 只驱逐 current）
- O7（confirmed）——配置落点/驱逐粒度/软限动作/低优先级默认
