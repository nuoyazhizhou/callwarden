# 前置条件告知（Prerequisite Gate）— 需求 brief

- Feature: `prerequisite-gate`
- 链路: 精简链（存量仓迭代）
- 任务记录: `T-1795661715269-f3611f48`
- 日期: 2026-11-26
- 状态: R3 已用户确认并冻结（基线提交 `8b1a1e3ee4a2a98f5cb7707d705bd7414a036abe`，2026-11-26）；R4 建单中
- 基线冻结提交: `8b1a1e3ee4a2a98f5cb7707d705bd7414a036abe`（.design/preq/ 六文件，独立设计基线提交，不混源码 hunk）

## 1. 背景与问题（已核验）

依赖建模早已存在但只被查询层消费：

| 数据结构 | 位置 | 语义 |
|---|---|---|
| `task_dependencies` | `db/schema.py:1433` | 四类声明：requires_existing / requires_artifact / provides_interface / requires_interface；`is_informational` |
| `dependency_edges` | `db/schema.py:1472` | provider→consumer 物化边；`is_hard=1` 阻断性 / `0` 信息性；`edge_type` 携带依赖类型 |
| `artifact_identities` | `db/schema.py:1455` | `freshness_status`：producing / fresh / stale |
| `interface_provider_selections` | `db/schema.py:1486` | interface 的 provider 选择 |

消费面现状：仅查询 RPC（`get_dependency_edges` / `detect_cycle` / `read_task_dependencies`）。**claim（`task_loop/claim.rs`）、投影（`task_loop/next_action.rs`）、派工（`task_collab_lease.rs:929 handle_task_work_next`）零引用**——agent 领取有硬前置的卡时 daemon 不给任何信号。

schema 注释已预设判定语义（`db/schema.py:1433`）：「consumer 的 `requires_artifact` 条款仅在 provider artifact fresh 时满足」。

## 2. 需求条目（MoSCoW）

| ID | 需求 | MoSCoW | 层 | 批次 |
|---|---|---|---|---|
| REQ-PREQ-04 | 判定引擎：按依赖类型分层判定 | must | 引擎 | 1 |
| REQ-PREQ-01 | 投影层：next_action 投影与 task.status 治理投影增加 `prerequisites` 字段（provider task_id、lifecycle_status、dep_type、缺失物） | must | 投影 | 1 |
| REQ-PREQ-02 | claim 层：硬依赖未满足 fail-closed `E_PREREQUISITE_NOT_MET`（消息指向 provider task_id）；信息性边附 warning 不阻断 | must | 领取 | 1 |
| REQ-PREQ-03 | capability：`prerequisite_gate_v1` 声明 + enabled/projection + claim 门禁接线 | must | 门禁 | 1 |
| REQ-PREQ-06 | v2 amendment 追加 prerequisite gate 设计章节 | should | 文档 | 1 |
| REQ-PREQ-05 | work_next 派工过滤 + provider 卡推荐 | should | 派工 | 2 |

## 3. NFR

- 性能：判定引擎纯只读查询（claim 路径新增 1-2 次索引查询）；具体数值无来源 → **open**
- 兼容：capability 未启用时 claim 行为不变（存量客户端零破坏）
- 安全：daemon 单一 authority，客户端不自行推算；prerequisites 查询按 workspace_id 过滤不跨区泄露

## 4. 需求评审四件套

### 4.1 方案对比（整体实现路线）

| 维度 | 方案 A：daemon 三层渐进（推荐） | 方案 B：一步到位全量上线 | 方案 C：客户端自行查询拼装 |
|---|---|---|---|
| 做法 | 投影→claim（capability 门禁）→work_next，两批实施 | 三层同批全上 | agent 自己调 get_dependency_edges 判断 |
| 优点 | 风险递进，每批独立验收；符合既有 capability 惯例 | 一次交付完整 | 无 daemon 改动 |
| 缺点/代价 | work_next 收益延后 | 回归面大，capability 未验证即全量 | 客户数重复实现、口径漂移、违背单一 authority |
| 失效模式 | 第二批排期漂移→投影已可见、claim 仍放行（可接受：告知优先于阻断） | 任一层缺陷全量回滚 | 各客户端判定不一致→同卡不同结论 |
| 适用条件 | — | 有全量回归带宽 | 一次性脚本/外部降级 |
| 结论 | ✅ 选它：用户已确认（三层设计、两批实施） | ❌ 否决（见 4.2） | ❌ 否决（见 4.2） |

### 4.2 被否决项及理由

- **否决 B（一步到位）**：三层耦合同一批次，任一缺陷需整批回滚；在「有全量回归带宽 + capability 预验证」条件下可重新考虑。
- **否决 C（客户端拼装）**：客户端自行推算前置状态产生口径漂移（freshness/lifecycle 快照时点不一致），与 AGENTS.md「daemon 是唯一 authority、客户端不自行推算」直接冲突；仅在 daemon 不可用的降级场景可临时使用（须声明 degraded）。
- **否决（子决策）统一按 applied/closed 判定**：stale artifact 放行击穿「artifact fresh 才满足」的 schema 既定语义；在「无 artifact 依赖场景占主导」时可重新考虑。
- **否决（子决策）claim 全告警**：硬依赖形同虚设，与 fail-closed 哲学冲突；capability 关闭态的默认行为（存量兼容）与此不同——那是「未启用」而非「仅告警」。

### 4.3 异常场景矩阵（8 类）

| # | 场景 | 结论 |
|---|---|---|
| 1 | 空值/缺省 | task 无依赖声明 → `hard_unmet=[]`、`all_satisfied=true`，投影 `prerequisites=[]`，claim 放行（不报错） |
| 2 | 无权限 | claim/投影走既有 peer 授权链；prerequisites 查询按 workspace_id 过滤，不泄露跨区信息 |
| 3 | 重复请求 | 判定引擎纯只读无副作用；claim 幂等性由既有 lease 原子发放保证，本设计不新增写路径 |
| 4 | 冲突 | provider/consumer 并发状态变化：判定以查询时点快照为准；claim 的 gate 在事务内、insert 前复查 |
| 5 | 依赖失败 | dependency_edges/artifact_identities 查询失败 → `DaemonRpcError` 向上抛，claim 中止（不静默放行） |
| 6 | 重试 | 只读判定天然可重试；claim 重试走既有 lease 冲突路径（`E_LEASE_*`），不放大副作用 |
| 7 | 取消/恢复 | 无半成品状态（纯读）；provider 任务 `reverted` → 判定恢复为未满足（lifecycle 不在 {applied, closed}） |
| 8 | 数据边界 | 环依赖：建边期 `detect_cycle` 已对 is_hard=1 阻断；本设计对环不特殊处理 |

### 4.4 未决项清单（三态）

| 类型 | 内容 | 责任人 | 验证方式 / 推翻条件 |
|---|---|---|---|
| confirmed | 满足判定按依赖类型分层（existing→lifecycle；artifact→lifecycle+fresh；interface→selection 生效） | 用户 | R0 第 1 问（2026-11-26） |
| confirmed | claim 硬依赖 fail-closed，信息性边仅 warning | 用户 | R0 第 2 问 |
| confirmed | capability 名 `prerequisite_gate_v1`，设计文档走 v2 amendment 追加章节 | 用户 | R0 第 3 问 |
| confirmed | 三层设计、两批实施（第一批投影+claim，第二批 work_next） | 用户 | R0 第 4 问 |
| assumption | requires_interface 的「selection 生效」= `interface_provider_selections` 中存在该 interface 的 active selection 行 | Planner | R3 设计复核；若 selection 状态语义不同，executor 实现前回本卡修订（revision+1） |
| assumption | task.status 治理投影与 next_action 投影的 `prerequisites` 同构（同一判定引擎输出） | Planner | T2/T3 验收时对比两投影字段一致性 |
| open | work_next 推荐排序规则（provider 卡位置/数量） | Planner | 第二批设计时定；不定则第二批仅缺推荐不阻塞 |
| open | capability `schema_version` 起始值 | Executor | 实现时按 task_prompt capability 惯例取值 |

## 5. 环境记录（降级声明）

- MCP daemon 端口不一致（配置 12019 / 实际 10528），MCP 工具本会话不可用；代码核验走 IDE Read/Grep + CLI（**锚定降级已声明**，R2 符号定位全部带 file:line 证据）
- canonical workspace binding 曾断裂（registry 1193 offline + principal 授权缺失）；按 `tests/test_task_cascade_close.py` 文档机制修复：`daemon register` + grants.json 追加 1193 + task-DB 种行（id=1 已存在未重复插入）。修复脚本为一次性中间产物，已清理
