# PRD：CW Python 纯 Client 化 + Rust Daemon 业务下沉

| 字段 | 值 |
|---|---|
| Language | 中文 |
| Project Name | `cw-rust-client-convergence` |
| 技术栈 | Python（client 薄壳层）+ Rust（cw-daemon 权威实现层） |
| 版本 | v0.1（初稿） |
| 作者 | Alice（产品经理） |

---

## 1. 项目信息

### 1.1 原始需求复述

> "python 只实现 mcp 和 cli 命令的 client，所有业务逻辑都在 rust daemon 中实现，既确保不同 agent 同时调用 cw mcp 不冲突。"

Call Warden（代码知识图谱 + MCP 工具，`C:\git_work\callwarden`）当前 Python 侧同时承载业务逻辑与 MCP/CLI 命令壳，Rust daemon（cw-daemon，`CW_DAEMON_TRANSPORT=http/enterprise/auto`）是 HTTP transport 的权威写路径。239 个 MCP 工具按实现方式分为 5 类：

| 类别 | 数量 | 现状 |
|---|---|---|
| HTTP-Rust 原生 | 59 | Python 薄壳 → HttpDaemonRpcClient → HTTP /v1/rpc → Rust handler |
| HTTP-任务 RPC | 22 | route_task_write/read → task.* handler（daemon 权威写） |
| HTTP-compat worker | 79 | route_worker_call → daemon COMPAT_ROUTE_WHITELIST → Python worker 执行 |
| 传统-HTTP 拒止 | 61 | HTTP 模式 fail-closed（E_HTTP_COMPAT_UNSUPPORTED），仅 local/legacy 走本地 SQL/UnixDaemonRpcClient |
| 传统-纯本地 SQL | 18 | 无 daemon 路由，直接 get_db() 本地 SQLite |

**本 PRD 目标**：Python 层收敛为纯客户端薄壳（MCP 工具函数只做参数透传 + 结果返回；CLI 只做命令解析 + 调用 daemon），不承载任何业务逻辑（不直接操作 SQLite、不实现查询/写入算法）；全部业务逻辑（查询、写入、状态机、CAS、GC、规则、协同）下沉 Rust daemon；不同 Agent 同时调用 cw MCP 不冲突。

---

## 2. 产品定义

### 2.1 Product Goals（3 个正交目标）

- **G1 · Client 纯化**：Python 层 = 纯客户端薄壳（MCP 透传 + CLI 转发），零业务逻辑、零 SQLite 直连；行为与调用来源无关。
- **G2 · 业务下沉 Rust**：全部查询/写入/状态机/CAS/GC/规则/协同逻辑收敛到 cw-daemon 单一权威实现，消除 Python/Rust 双实现漂移。
- **G3 · 多 Agent 并发安全**：所有写操作走 daemon 权威路径（单一写点 + 串行化/加锁），读操作并发安全；不同 Agent 同时调用不冲突、不丢数据。

### 2.2 User Stories

1. **多 Agent 并发写**：As a 同时运行多个 coding agent 的用户，I want 两个 agent 同时通过 cw MCP 写任务/更新图谱互不冲突，so that 多 agent 工作流不会出现数据丢失、租约误冲突或状态不一致。
2. **MCP 只透传不碰库**：As a MCP 工具消费者（agent），I want 每个 MCP 工具只透传参数并原样返回 daemon 结果，so that 工具行为与调用方/调用顺序无关、结果可预期且可审计。
3. **CLI 无本地逻辑**：As a CLI 用户/脚本调用者，I want 所有 `cw` 命令都通过 daemon 权威路径执行，so that 命令行与 MCP 看到同一份状态、同一套规则，不会因本地实现而偏离。
4. **单一维护点**：As a 开发者（维护者），I want Python 层不承载业务逻辑，so that 修复/增强只需改 Rust 一处，避免 Python/Rust 双实现漂移与行为不一致。
5. **可审计写路径**：As a 平台管理员，I want daemon 成为唯一写点，so that 所有变更可统一审计、串行化、加锁与回滚。

---

## 3. 技术规范

### 3.1 Requirements Pool

#### P0（Must have — 本次迭代必须完成）

| ID | 需求 | 说明 |
|---|---|---|
| R0.1 | **79 个传统工具迁移方向决策** | 61 个传统-HTTP 拒止 + 18 个纯本地 SQL：明确每个工具的迁移路径（Rust native handler / task RPC / compat worker 过渡 / 显式废弃），产出可机器核对的迁移清单与路由矩阵 |
| R0.2 | **Python 工具层去业务化** | 239 个 MCP 工具函数全部改为「参数透传 → daemon RPC → 结果返回」；移除工具路径中的直接 SQLite 操作（get_db 业务使用）与本地查询/写入算法 |
| R0.3 | **CLI 纯 client 化** | `callwarden/cli` 只做命令解析 + 参数校验 + 调用 daemon；移除 CLI 内本地业务实现 |
| R0.4 | **写路径权威约束** | 所有写操作（create/update/delete/CAS/lease/GC/规则变更）必须经 daemon 权威路径，daemon 内串行化/加锁；Python 侧禁止直接写 SQLite |
| R0.5 | **160 个现有 HTTP 工具零回归** | 59 native + 22 task RPC + 79 compat worker 改造后行为兼容，全量回归通过 |
| R0.6 | **并发安全验证** | 双 Agent 并发写同一对象：无数据丢失、无 E_* 误冲突、结果一致（与 QA 协同） |

#### P1（Should have — 紧随其后）

| ID | 需求 | 说明 |
|---|---|---|
| R1.1 | daemon 工具覆盖率 100% | 239 个工具全部有 daemon 路由（含显式不可用声明），无「本地隐式路径」 |
| R1.2 | compat worker 过渡机制与淘汰时间表 | COMPAT_ROUTE_WHITELIST 明确保留期与 deadline |
| R1.3 | local/legacy 模式决策 | 统一强制 daemon 或保留受限只读回退，需给出结论（见 Open Questions） |
| R1.4 | Python 业务代码移除率指标与死代码清理 | 以基线行数计，清理不再被引用的 db/业务模块 |
| R1.5 | 协议与路由文档更新 | daemon RPC schema、239 工具路由矩阵、迁移指南 |

#### P2（Nice to have）

| ID | 需求 | 说明 |
|---|---|---|
| R2.1 | 迁移脚手架/自动化 | 批量生成薄壳 handler 与迁移清单的脚本 |
| R2.2 | 性能优化 | 批量 RPC、连接复用、减少往返 |
| R2.3 | 部署简化 | daemon 自动拉起/健康检查增强、多实例共享方案 |
| R2.4 | 开发者文档 | 新架构下的开发/调试指南 |

### 3.2 可量化验收标准

**must（必须满足，作为发布门槛）**

- M1：239/239 个 MCP 工具在 HTTP daemon 模式下可路由；其中 160 个现有 HTTP 工具零回归，剩余 79 个（61 拒止 + 18 本地 SQL）按 R0.1 决策迁移或显式废弃；路由矩阵可机器核对（数量/名称/路由类型/状态）。
- M2：Python 侧审计通过——`server`/`cli` 工具路径中不存在直接 SQLite 业务读写（允许配置读取），grep/静态检查 0 命中业务写。
- M3：双 Agent 并发写测试通过：同一对象并发 N 次写（QA 场景），无数据丢失、无 E_* 误冲突、最终结果一致。
- M4：CLI 全部命令在 daemon 不可用时明确报错（而非降级本地执行），错误信息与文档一致。

**should（应满足，可协商）**

- S1：Python server/cli 业务逻辑代码行数相对基线减少 ≥ 90%。
- S2：现有 pytest + 回归套件全绿，覆盖 239 工具冒烟。
- S3：单 Agent 典型写操作 P95 延迟不劣于基线（或给出可接受上限并记录）。

**could（可选的加分项）**

- C1：产出迁移前后行为差异报告（diff report），便于评审与回滚决策。
- C2：daemon 提供 `/v1/meta/tools` 等自描述接口，便于核对工具覆盖。

### 3.3 Open Questions（需澄清）

1. **79 个传统工具迁移策略**：全部迁 Rust native handler，还是部分走 compat worker（由 daemon 调度、Python worker 执行）？一次全量还是分批？——决定 P0 工作量边界与验收口径。
2. **CLI 迁移范围**：`cw` 全部子命令（含 daemon 管理、agent、analyzers）都走 daemon？例外项有哪些（如本机 daemon 启停本身）？
3. **local/legacy 模式**：`CW_DAEMON_TRANSPORT=local/legacy` 是否保留纯本地回退（degraded_mode）？若保留，是否违反「业务逻辑全在 daemon」？是否统一强制 daemon、未启动即报错（fail-closed）？
4. **18 个纯本地 SQL 工具**：迁移后是暴露通用 SQL 查询 RPC，还是逐个实现业务 handler？通用查询的安全边界（防注入/越权）如何约束？
5. **兼容窗口**：COMPAT_ROUTE_WHITELIST / compat worker 保留多久？是否设 deadline（如 2 个里程碑后移除）？
6. **daemon 部署形态**：每 Python 进程一个 daemon 实例，还是共享单一 daemon？多 Agent 跨机器调用时如何共享？并发串行化在单实例 vs 多实例下的保证范围？
7. **只读降级**：daemon 未启动时，是否允许 Python 侧只读本地查询（降级）？还是严格 fail-closed？

---

## 4. 约束

- **简洁优先**：本 PRD 面向架构师快速理解；详细任务拆解由架构师与工程师产出。
- **范围**：本次不做竞品分析（该方向为内部架构收敛，无直接竞品对标价值）。
- **兼容**：现有 MCP 工具对外协议/返回格式不因收敛而破坏（160 个 HTTP 工具零回归）。
- **协作**：与架构师（Rust 收敛设计）、工程师（handler 扩展 + Python 瘦身）、QA（并发回归）联动；R0.6 与 QA 的并发场景需提前对齐。
