# P0 部署 + CLI/MCP 双入口验收测试矩阵 · 方案

日期：2026-09-23 ｜ 状态：**待评审** ｜ 前置：P0 单实例守卫已提交 `02a67ce`（未部署）

---

## 0. 结论先行（对"你觉得应该吗"的回答）

**赞同方向，但按"每个工具/命令一张卡、逐个手工测"的原始形态做，302 张卡会不可维护。必须改三处设计：**

| # | 原始设想 | 问题 | 建议改法 |
|---|---|---|---|
| 1 | 243 MCP + 59 CLI = 302 条全手工 | 纯查询占 ~65%，手工跑不现实，且人眼对账易漏 | **分层**：L0 脚本批量+自动三路对账（覆盖 ~190 条）；手工只留给写路径/治理环/并发边界（~30 条 L2） |
| 2 | 测试任务卡进当前 cw 数据库 | 创建/关闭任务污染生产 backlog；测试 lease 与真实 agent 冲突 | **物理隔离**：独立 authority + 独立任务库（顺带就是 P0 authority-scoped 锁的第一个验收用例） |
| 3 | 先逐个测试、再 bugfix | P2 审计**已经**确认 35 个 query_local 无 daemon 路径——再"测"一遍是浪费 | **已知缺口直接转 P2-1 bugfix**；测试矩阵只聚焦**未知**问题（接线行为、双入口一致性、并发崩溃边界） |

---

## 1. 盘点结果（方案的数字依据）

| 面 | 数量 | 来源（本轮实证） |
|---|---|---|
| MCP 工具 | **243**（12 个功能域模块） | `server/tools/tools_*.py` 的 `@mcp.tool()` 逐模块提取 |
| daemon 注册 RPC 方法 | **220** | `rust_ext/src/daemon/dispatch.rs` 点分方法名，去 `mcp.*` compat 内部键与文件名噪声 |
| Rust CLI 顶层命令 | **59**（含 17 组嵌套子命令） | P2 审计（`cw_cli.rs` enum 配平扫描） |
| Python CLI 命令 | **65**（Python 独有 6） | P2 审计（`_SUBCOMMANDS`） |
| CLI 已接线 daemon（dual_rpc） | **19** | P2 审计 |
| CLI 无 daemon 路径（query_local） | **35** ← 已知缺口 | P2 审计 |

### 1.1 Wave 0.5 离线对账（本轮已完成，零成本）

不部署就能做的第一个验收——**方法名集合比对**（脚本 `.workbuddy/scripts/e2e_rpc_name_reconcile.py`）：

| 集合 | 数量 | 结论 |
|---|---|---|
| MCP 工具引用的**点分 daemon method** | **145** | 走 `route_rpc` → daemon RPC |
| MCP 工具引用的**无点 compat 键**（`_route('ask_codebase', ...)`） | **79** | 走 Python compat 本地实现 = **Rust native 迁移 backlog** |
| dispatch.rs 注册的 RPC 方法 | **220** | 去 `mcp.*` compat 键与 `.rs` 文件名噪声 |
| **MISSING_IN_DISPATCH**（调用了未注册） | **0** ✓ | MCP 侧无"声明接线但 404"缺口 |
| **TRUE_ORPHAN_RPC**（注册但全仓无入口） | **0** ✓ | 83 个"无 MCP 工具调用"的方法经二次甄别全部有 Rust CLI / 治理脚本 / 测试入口 |

**两个直接产出：**
1. **MCP 侧接线静态完整**（0 缺口 / 0 孤儿）→ MCP 测试重心不是"查缺口"，而是**运行时活验证**（注册了不代表行为对、不代表双入口结果一致）。
2. **79 个 compat 无点键**是"Python compat → Rust native"迁移进度的**精确 backlog 清单**——可直接转一张治理卡排期。

> **方法学教训（已修进脚本）**：提取正则的段字符类最初是 `[a-z0-9]`，漏掉下划线，导致 `build_context.count_resolved_edges` / `lease.list_events` 这类含 `_` 的方法名**全部**漏提（dispatch 少算了 27 个，并短暂误报 5 个假缺口）。修正为 `[a-z0-9_]` 后结论反转。**任何源码级集合对账都要先拿含下划线/数字的样本验正则**。

> CLI 侧的 35 个 query_local 缺口是已知问题（P2 审计），转 P2-1 直接修，不进测试矩阵。

---

## 2. 部署拓扑：生产与测试物理隔离

### 2.1 生产侧（P0 部署后）

| 项 | 值 |
|---|---|
| authority | 默认 |
| 任务库 | `~/.callwarden/callwarden.db` |
| manifest | `~/.callwarden/http-daemon.<SID>.manifest.json` |
| 部署方式 | `refresh_shared_runtime.ps1 -TaskId <lineage 卡号>` |

### 2.2 测试侧（验收测试专用）

**方案 A（推荐）：独立 authority 完全隔离**

| 项 | 值 |
|---|---|
| authority | `cw-e2e-20260923` |
| 任务库 / manifest_dir | `~/.callwarden-e2e/` |
| daemon | 独立进程，由用户在真实终端启动（沙箱 Job Object 会杀会话外进程——本轮已实证，见 §5） |
| fixture 仓库 | **不用 callwarden 自身**（daemon 启动期 `recover_all_workspaces` 对全仓做 durable snapshot 重建，`refresh` 脚本的 120s ping 窗口刚够，测试再叠负载易超时）。用小型独立 repo |

**隔离带来的免费验收点**（P0 活验证的第一批用例）：
1. 不同 authority 的两个 daemon **可以共存**（锁按 authority 分作用域）
2. 同 authority 的第二个 daemon 被 `E_DAEMON_ALREADY_RUNNING` 拒绝（exit 1，一个 DB 都不打开）
3. 杀掉测试 daemon 后立即可重启（flock 随进程退出释放）

**方案 B（备选）**：同库 + `[E2E]` 标题前缀 + status 过滤。简单但有污染风险，仅当无法起第二个 daemon 时用。

---

## 3. 测试分层

### L0 · 自动化批量（~190 条，脚本跑、自动判定）

**对象**：只读查询类——`tools_query`(32) / `tools_rules`(9) / `tools_security` 中 LSP 与只读类 / `tools_summary` 中只读类 / `tools_semantic` 中只读类。

**方法**：对 fixture 仓库，同一查询走 **三路** 并自动对账：
1. MCP 工具（Python `route_rpc` → daemon）
2. Rust CLI（`cw <cmd> --mode enterprise`，仅对 19 个 dual_rpc）
3. daemon RPC 直连（`cw-client` 或 Python `HttpDaemonRpcClient`）

**判定**：三路结果 diff = 空 → `PASS`；diff 非空但落在 documented 容差内 → `PASS(with note)`；否则 `FAIL` 并自动记录差异快照。

### L1 · 半自动（~80 条，脚本驱动状态机 + 人工核对返回 schema）

**对象**：写路径——`task.*` / `lease.*` / `edit.*` / `assignment.*` / `snapshot.*` / `branch.*`。

需要身份切换与状态机推进（claim→report→resolve），脚本不能完全自判，人工核对返回 schema 与 `error_codes.rs` 契约。

### L2 · 手工深测（~30 条，人盯）

**对象**（真正需要人盯的四类）：
1. **治理环全链路**：claim → 逐 step report → verdict → apply → close → supersede → cascade_close（多角色、双独立 agent）
2. **P0 单实例并发竞争**：双启、kill 后重启、Windows 锁文件"读不得"行为（os error 33）
3. **daemon 崩溃/被杀后锁释放**：flock 内核对象自动释放语义
4. **MCP 多实例冷启动去重**：`DaemonMutex`（Windows `WAIT_ABANDONED` 崩溃恢复）

---

## 4. 任务卡模板（进 cw 数据库）

每张卡绑定**测试 authority 的 workspace_instance_id**，字段：

```yaml
title: "[E2E-MCP-001] get_stats"
入口面: MCP                          # MCP | CLI
调用: "route_rpc('query.stats', {}, 'READ_ONLY')"
期望:
  路由: "enterprise/auto → daemon RPC query.stats"
  断言: "返回含 files/functions/calls；与本地 SQL 统计一致（容差 0）"
判定: PASS | FAIL | BLOCKED           # BLOCKED = 环境不具备，不计缺陷
FAIL_bugfix: ""                      # 关联 P2-1..P2-4 或新开卡
wave: W1
隔离: "authority=cw-e2e-20260923"
```

**FAIL → bugfix 衔接规则**：FAIL 卡必须携带可复现的失败快照（命令、exit code、stdout diff、daemon 日志片段），直接作为 bugfix 卡的输入，不做二次人工转述。

---

## 5. P0 部署前置清单

| # | 项 | 状态 | 说明 |
|---|---|---|---|
| 1 | P0 代码 | ✅ 已提交 `02a67ce` | `single_instance.rs` + `cw_daemon.rs::serve()` 早期加锁 |
| 2 | release 构建预热 | ⚠️ **放弃沙箱构建** | 本轮在沙箱后台跑 cargo release，cargo 已开始编译、无 error，但进程被 Job Object 在 turn 边界切断（exit 标记文件未写）。半截增量产物**会被 cargo 增量复用**，不浪费 |
| 3 | lineage 卡号 | ❌ **用户未提供** | `refresh_shared_runtime.ps1` 强制 `-TaskId` 匹配 `^T-[0-9]+-[0-9a-z-]+$`，禁止占位符 |
| 4 | 脚本执行环境 | ⚠️ **必须用户真实终端** | 同一 Job Object 机制：脚本内 `Ensure-DaemonSingleInstance` 启动的 daemon 需要长期存活，沙箱里跑会被杀 |

**部署执行（用户侧一条命令）**：

```powershell
.\scripts\refresh_shared_runtime.ps1 -TaskId T-<真实卡号> -Configuration release -RunSmokeTests
```

脚本内部自动完成：MSVC 环境注入 → cargo release 构建（target `rust_ext/target/stage-refresh`）→ `callwarden_core` 部署（带 hash 校验 + dumpbin 依赖验证）→ `runtime/current` 原子切换 → daemon 探针去重启动 → 240×500ms ping 窗口 → 运行中 daemon 路径/hash 双校验 → 证据 JSON → 失败自动回滚。

**部署后我侧验证**：
1. `cw daemon ping` 通过
2. **二进制字符串扫描**（确认 single_instance 代码已编入）：在 `runtime/current/cw-daemon.exe` 中搜 `daemon-instance.` / `E_DAEMON_ALREADY_RUNNING` / `acquire_default_instance_lock`
3. 同 authority 二次启动 → 观察是否 `E_DAEMON_ALREADY_RUNNING` exit 1

---

## 6. Wave 排期

| Wave | 内容 | 层 | 条数（估） | 前置 |
|---|---|---|---|---|
| **0.5** | 离线 method 名对账 | 自动 | 220 方法 + 79 compat 键 | ✅ **本轮已完成**（0 缺口 / 0 孤儿 / 79 迁移 backlog） |
| **0** | P0 单实例活验证 | L2 | 5–8 | P0 部署完成 |
| **1** | MCP 243 工具活验证（三路对账） | L0 | ~190 | Wave 0 + fixture 仓库 |
| **2** | CLI 19 dual_rpc 三路对账 + 35 query_local fail-closed 回归 | L0+L1 | 59 | P2-1 修复后做回归段 |
| **3** | 治理写路径多角色环 | L1/L2 | ~40 | Wave 1 |
| **4** | 并发 / 崩溃 / 多实例去重边界 | L2 | ~10 | Wave 0 |

**顺序原则**：Wave 0 → 1 → 2 → 3 → 4。先验证"新能力"（P0），再验证"最大面"（MCP 243），再收敛到已知缺口（CLI 35），最后啃硬骨头（治理环 + 并发）。

---

## 7. 交付物清单

| 文件 | 用途 |
|---|---|
| `deliverables/p0-deploy-and-e2e-matrix-plan-20260923.md` | 本方案 |
| `.workbuddy/scripts/e2e_rpc_name_reconcile.py` | Wave 0.5 对账脚本（固化，含扩展搜索范围） |
| `.workbuddy/scripts/e2e_l0_harness.py` | Wave 1 三路对账框架（待写） |
| `deliverables/e2e-matrix-<wave>.md` | 每个 Wave 的逐卡结果矩阵 |

---

## 8. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 部署失败导致生产 daemon 不可用 | `refresh_shared_runtime.ps1` 自带回滚（core extensions 回退 + `current` 目录回滚 + 旧 daemon 重启） |
| 测试污染生产任务库 | 方案 A 物理隔离（独立 authority + 独立库）；测试卡绝不写默认 authority |
| daemon 启动期 snapshot 重建超时 | fixture 仓库用小型 repo；不拿 callwarden 自身压测 |
| L0 自动对账的"容差"被滥用成免检 | 容差必须逐方法显式声明在矩阵里，未声明容差的方法默认零容差 |

---

## 9. 需要你决策的四件事

1. **lineage 卡号**？（P0 部署的唯一缺失输入）
2. **fixture 仓库**用哪个：新建小型 repo / 复用 tokenslim / 其他？
3. 测试隔离走**方案 A（独立 authority）**还是 **B（同库前缀）**？
4. Wave 1 的 L0 自动化对账，"第三路"用 `cw-client` 直连 RPC 还是 Python `HttpDaemonRpcClient`？
