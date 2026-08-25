# 当前任务树 vs 四份参考文档差距分析

> 生成时间：2026-08-20 17:10
> 分析人：Software Architect（架构评审视角）
> 数据来源：`~/.callwarden/callwarden.db` 任务树 + 四份参考文档 + `tool_migration_matrix.json`（实时）

---

## 一、T-1787209886781-48b4cb0c 的父任务与兄弟任务（直接回答）

`T-1787209886781-48b4cb0c`（S1 CLI 迁移·重建·authority 绑定）的父任务是 **`T-1787203926824-9f873bfc`**（CW 业务逻辑全量下沉 Rust daemon 三阶段收尾）。

该父任务下**当前共 5 个子任务**，全部已列出：

| 子任务 ID | 标题 | 状态 | 性质 |
|---|---|---|---|
| `T-1787203937193-0993d120` | S1 T04-followup：cli/main.py 迁移 daemon RPC | review | 旧 S1（无 workspace 绑定，遗留） |
| `T-1787203937201-0a156564` | S2 compat 79 工具 M2 迁 Rust handler | open | 旧 S2（无绑定，遗留） |
| `T-1787203937208-0a795c68` | S3 PyO3 直调清理 + db/ 目录下线 | open | S3（无绑定，遗留） |
| `T-1787209886781-48b4cb0c` | S1 ...（重建·authority 绑定） | **closed** | 新 S1（已闭环） |
| `T-1787209948470-a59bcf9c` | S2 ...（重建·authority 绑定） | open | 新 S2（实施中） |

**结论：父任务已完整列出（5 个子任务都在）。** 但存在结构性问题：旧 S1/S2/S3 三个无绑定任务（review/open/open）与重建后的新 S1/S2 并存，属于**同 scope 双任务残留**，需清理或标记废弃。

---

## 二、当前任务树 vs 四份文档的差距（逐项）

### 差距 G1：任务树目标与 WFL 蓝图（文档 1）错位
| 维度 | WFL 蓝图要求 | 当前任务树 |
|---|---|---|
| 核心目标 | 本地优先工作事实层：单 authority + Task Envelope + event ledger + 可迁移工作包 | 仅"业务下沉 Rust"（239 工具迁移），**无 Envelope/ledger/工作包/私有复制任务** |
| 数据分类 | A 不可变事实 / B CAS 工件 / C 可重建 / D 秘密，四类分级 | 无对应任务 |
| 断链修复 | ① manifest/health 恢复 ② workspace binding ③ task operation ledger ④ 时钟边界 | ①未建任务 ②S0 已在实施中做了 binding（schema v58 + capture 基建）但**无 CW 任务记录** ③未建任务 ④未建任务 |
| 建议任务树 | P-WFL: R0-R10 十个阶段 | 无 R0-R10 对应任务 |

### 差距 G2：任务树目标与 MCP 功能恢复评审（文档 2）冲突
文档 2 明确 **BLOCKED**：不建议按 S1→S2→S3 直接推进，理由：
1. **S1 应退回并缩小**：check_client_purity 静态门禁 ≠ daemon RPC 可用性证明 —— 当前 S1 已 closed，但其验收仅基于静态门禁 + QA 测试，无"每个工具经 daemon RPC 的真实 round-trip fixture"
2. **S2 应拆分并延后**：79 个 compat 全是 read-only，应作为 worker bridge 过渡而非一次性 Rust 重写 —— 当前 S2 正在逐批 Rust 化（批次 1/2/3 共 9 个），**与文档 2 建议直接冲突**
3. **S3 必须冻结**：db/ 删除是 retirement 不是恢复前提 —— 当前 S3 仍 open 待执行
4. 文档 2 建议 R0-R10 渐进切换（先控制面→read-only→index→protected→governance），当前树无此分层

### 差距 G3：与 Lease/Fencing 评审（文档 3）的关系
文档 3 是**只读评审**（CHANGES REQUESTED），B1-B4 阻断问题：
- B1 持久化 mutation 去重未接管（TaskCollabStore.dedup_cache 内存态）
- B2 task.report 无 lease/fencing 校验
- B3 assignment→role→identity 链式绑定缺失
- B4 HTTP dev profile 身份不可证明

**当前任务树完全没有覆盖 B1-B4** —— 无"operation ledger cutover"、无"MutationAuthContext 覆盖"、无"assignment 绑定"、无"CSPRNG token"任务。文档 3 的修订顺序（1-6）在任务树中无对应项。

### 差距 G4：任务树自身结构问题（不依赖文档）
| 问题 | 详情 |
|---|---|
| G4.1 同 scope 双任务 | S1 旧（review）+ S1 新（closed）；S2 旧（open）+ S2 新（open）并存 |
| G4.2 S0 无任务记录 | S0（schema v58 + workspace capture 基建）已实际完成，但**任务库中无对应任务**（创建时被 E_TASK_WORKSPACE_UNBOUND 拦截未落库） |
| G4.3 父任务无步骤 | 父任务 T-1787203926824 无 steps 记录，无法核验进度 |
| G4.4 矩阵状态漂移 | 矩阵实时：129 rust_native / 70 python_compat / 40 task_rpc（migrated 88 / transition 70 / stable 81）；文档 2 引用时点是 126/73/40 —— 两份真相源语义（可用 vs 目标可迁移）不一致的批评依然部分成立 |

### 差距 G5：与 mcp-tools-implementation-map.md（文档 4）的一致性
文档 4 是静态盘点（160 HTTP / 79 传统），当前进展已超越它：S2 批次迁移后 129 native。但文档 4 的"传统 79"中 70 个仍 transition，**文档未更新**（还是 8-20 早上的版本）。

---

## 三、建议（架构视角）

1. **父任务改写**：将 `T-1787203926824-9f873bfc` 定位为 long-horizon Epic，按文档 2 建议冻结直接执行，改为渐进切换（R0-R10 分层）
2. **清理双任务**：废弃旧 S1/S2/S3（无绑定），保留重建任务
3. **补 S0 任务记录**：为已完成的 schema v58 + capture 基建补建任务并 closed（事实已存在，补记录）
4. **补文档 3 的 B1-B4 任务**：operation ledger cutover、MutationAuthContext、assignment 绑定、CSPRNG token —— 这些是"多 Agent 并发不冲突"目标真正成立的前提
5. **补文档 1 的 WFL 核心任务**：Task Envelope v1、CW work package 导出/导入、manifest/health 恢复 —— 这些是"可迁移工作事实"目标的前提
