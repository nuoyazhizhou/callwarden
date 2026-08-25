# A′ 身份链路缺口与 instance 门禁误判修复方案

> 触发：executor 循环提示词执行时，CLI-080 claim 被 `E_ROLE_INDEPENDENCE_VIOLATION`
> 确定性拒绝，根因为 `agent_instance_id` 恒为空导致 `''=''` 误判。
> 本文档定位这是**工具链/身份链路实现缺口**，不是提示词缺陷，也不是 A′ 设计错误；
> 并给出 agent 可获取的 ID 清单与最小修复路径。

## 一、现象还原（实测）

executor 循环窗口领取 CLI-080（`T-1787322799482-d215a638`）时，daemon evaluator 返回
`READY/CLAIM, required_role=executor, blocking_conditions=[]`（任务本身可实现），但
实际 `cw task claim` 被门禁拒绝：

```
E_ROLE_INDEPENDENCE_VIOLATION: 角色 independent_reviewer (agent=reviewer-w2-3, instance=, session=sess-w2-3-review)
与 角色 implementer 冲突，instance/session 不可共享（当前 agent=implementer-workbuddy-v1）
```

报错里 executor 的 `instance=` 为空，reviewer 的 `instance=` 也为空 → `''=''` 触发冲突判定。

## 二、根因（代码事实，非猜测）

### 2.1 `agent_instance_id` 是"幽灵字段"——上游没有任何入口填它

- **CLI 不传**：`cw task claim/report/apply/close` 的 `_collect_identity(opts)` 只收集
  `agent_id / session_id / model_id / role` 四字段（`cli/main.py:3901-3907` 只定义这四组
  `--agent-id/--session-id/--model-id/--role` 参数）。**没有任何 `--instance-id` 参数。**
- **daemon 默认空**：`storage.rs` 定义 `agent_instance_id TEXT DEFAULT ''`，CLI 没传 →
  `parse_action_identity` 解析出的 `agent_instance_id` 恒为 `""`。
- **注册路径也不强制**：`task_collab.rs:2489` 仅在"注册 instance 非空"时才校验一致性；
  空 instance 注册合法，于是大量身份（含 `implementer-workbuddy-v1`、`reviewer-w2-3`、
  `TRAE-REV-0A`、`reviewer-cw-bootstrap-v1`）都是空 instance。

### 2.2 独立性门禁对空 instance 的判定缺陷

`task_collab.rs:1648-1690` `check_role_independence`：

```sql
WHERE status='active' AND role != ?1
  AND (agent_instance_id = ?2 OR (session_id != '' AND session_id = ?3))
```

- 当 `?2`（当前 instance）为空时，本应只靠 `session_id` 区分；
- 但冲突方 `reviewer-w2-3` 的注册 `session_id` 在查询时也取不到非空值（其注册走的是
  `agent.register` RPC 的另一分支，未稳定写入 session），于是 `agent_instance_id=''`
  命中 `''=''` → 误判为"共享 instance" → 报冲突。

**结论**：这不是 executor 提示词写错了，也不是 A′ 状态机设计错误。是
**身份链路工具链（CLI 未暴露 instance + 注册未强制 instance + 门禁对空 instance 不鲁棒）
的共同缺口**，导致"角色独立性"这道 fail-closed 门禁在 instance 全空的环境下
**对全部 executor claim 确定性拒绝**。

## 三、你的第二个问题：agent/LLM 到底能拿到哪几个 ID？

直接回答：**大部分 ID agent 自己无法获取，当前工具链只稳定提供其中 1–2 个。**

| ID | agent/LLM 能否自行获取 | 来源 | 现状 |
|---|---|---|---|
| `agent_id` | ❌ 不能（需 prompt 显式告知/预注册） | 启动时由 driver 注入或查注册表 | 模板已写死（`implementer-workbuddy-v1`） |
| `session_id` | ⚠️ 部分能 | `CW_AGENT_SESSION_ID` 环境变量 / daemon 回退 owner_key | CLI 会自动解析（`_resolve_action_session`），但 agent 不一定知道值 |
| `model_id` | ❌ 不能 | 启动配置 / driver 注入 | 模板写死 |
| `role` | ❌ 不能（治理角色，非自由文本） | 预注册 / driver 注入 | 模板写死 |
| `agent_instance_id` | ❌ **完全不能** | **无任何 CLI 参数、无环境变量、无查询命令** | **幽灵字段，恒空** |
| `workspace_instance_id` | ⚠️ 可推导 | `derive_workspace_instance_id(watch_dir)` 从目录算 | CLI 内部推导，但 agent 不直接拿到 |

**关键事实**：`agent_instance_id` 是你设计里"区分独立角色实例"的核心键，但**整个工具链
（CLI 参数、环境变量、注册 RPC、查询命令）都没有任何入口让 agent 填或查它**。agent
既不知道自己的 instance id，也没有地方去拿——你的判断完全正确。

## 四、修复方案（按代价从小到大）

### 方案 A：门禁侧最小修复（推荐，先止血）

让 `check_role_independence` 对**空 instance 不参与共享判定**（空 instance 视为"未声明实例"，
不与他人冲突）：

```rust
// task_collab.rs check_role_independence
// 仅当 ?2 非空时才用 instance 判定；空 instance 只靠 session 区分
WHERE status='active' AND role != ?1
  AND ((?2 != '' AND agent_instance_id = ?2)
       OR (session_id != '' AND session_id = ?3))
```

代价：1 行 SQL 条件改动。效果：空 instance 的 executor 不再与空 instance 的 reviewer 误判冲突，
CLI-080 及同类 claim 立即通过。**不改变"instance 非空时强隔离"的语义。**

### 方案 B：CLI 暴露 `--instance-id` 并打通注册（中期，补齐设计意图）

- `cli/main.py` 给 `claim/report/apply/close` 加 `--instance-id` 参数，纳入 `_collect_identity`；
- `agent.register` RPC 强制 `agent_instance_id` 非空（或自动生成 `inst-<random>`）；
- 每个角色 worker 启动时由 driver 注入唯一 instance（如 `inst-executor-wb-<task_short>`）。

代价：CLI + 注册 + driver 三处改动。效果：恢复"instance 区分独立角色实例"的设计语义，
多 reviewer 并行、同角色多实例隔离才真正成立。

### 方案 C：提示词侧临时绕过（不推荐，仅应急）

为 executor 指定一个**已注册非空 instance 的替代身份**（如 `executor-workbuddy-v1` /
`inst-executor-wb-a26cd12e`）。但这是"换身份绕过"，未解决根因，且 reviewer 侧空 instance
仍在，只是碰巧不冲突。

## 五、对"正常应该有循环 planner / adjudicator 处理"的回应

你的直觉对一半：

- **这个阻塞不是 planner/adjudicator 该处理的**——它是**身份注册/门禁的 infra 缺陷**，
  planner 补范围解决不了"instance 空导致 claim 被拒"。正确归属是**基础设施修复
  （方案 A 或 B）**，或临时由 **user/adjudicator 作为"身份管理员"** 通过 `agent.register`
 给 executor 补 instance。
- **但你的更大的判断成立**：当前自动循环里**没有"身份/注册自愈"角色**。建议把
  "身份注册与健康检查"作为 driver（Coordinator）的内置职责，或设一个轻量
  `identity-bootstrap` 步骤：driver 在派工前先 `cw agent register` 确保 executor/reviewer/
  adjudicator 都有非空 instance + active 状态，避免 worker 窗口一启动就撞门禁。

## 六、落地建议

1. **立即**：应用方案 A（1 行 SQL 改动），CLI-080 等 claim 立刻放行，恢复自动循环试点。
2. **短期**：应用方案 B 的注册强制非空，让 driver 注入 instance，恢复设计语义。
3. **模板侧**：在 v2 启动模板 §1 增加"启动前自检 agent_instance_id 非空；若 daemon 报
   `E_ROLE_INDEPENDENCE_VIOLATION` 且 instance 为空，记录为 infra 缺陷并 handoff user，
   不视为任务本身阻塞"——防止 worker 把 infra 缺陷误判成"任务需补范围"。

## 七、结论一句话

这次事故**不是提示词缺陷，也不是 A′ 状态机设计错误**，而是身份链路工具链
（CLI 未暴露 instance + 注册未强制 + 门禁对空 instance 不鲁棒）的实现缺口，
导致"角色独立性"fail-closed 门禁在 instance 全空环境下对全部 executor claim 确定性拒绝。
agent 确实拿不到 `agent_instance_id`（无任何入口），需由 driver/注册层补齐；
最小修复是方案 A（门禁空 instance 不参与共享判定）。
