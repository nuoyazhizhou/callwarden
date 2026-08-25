# P0-G 批量任务卡与合同回填缺陷审计（step0 交付物）

- **审计角色：** executor
- **审计日期：** 2026-08-24
- **权威库：** `~/.callwarden/callwarden.db`（用户级活跃库，890+ 任务）
- **范围：** Epic `T-1787293451688-c14b1e44`（A′ 恢复父任务）下 133 个 review 子任务 + 其 governance 投影
- **审计方式：** 只读 SQL 统计 + `task.next_action` 派工投影实测 + 合同 envelope 反序列化检查
- **结论先行：** 128 张机械推导 revision-1 合同存在系统性缺陷（JSON 数组字符串化 100%、空 dependencies 100%），
  导致 42 个 review 子任务在 `task.next_action` 上 fail-closed BLOCKED；另有 132 个 active reviewer lease 遗留。
  修复必须走 append-only `task.contract_revise` 追加 revision-2，禁止原地改库。

---

## 1. 任务树与合同覆盖总览

| 类别 | 数量 | 说明 |
|---|---|---|
| Epic 下 review 子任务 | 133 | CLI/MCP/SRV/INT 迁移卡 |
| 有 Task Contract revision-1 | 133 | 100% 覆盖（无缺合同） |
| 其中机械推导（created_by=adjudicator-workbuddy-v1） | **128** | 批量 bootstrap 回填，缺陷集中 |
| 其中人工/受控补签（created_by=adjudicator-wb-186loop） | 5 | CLI-02/03、MCP-001/002/003，结构正常 |
| review 但 role_contracts=0 | 0 | 派生源不缺 |
| active verdict normalization rule sets | 1 | 规则存在，但 42 个任务未绑定/不匹配 |

## 2. 128 张机械合同的系统性缺陷（实测）

对 128 张 `adjudicator-workbuddy-v1` 生成的 revision-1 envelope 逐一反序列化检查：

| 缺陷 | 命中数 | 命中率 | 证据样本 |
|---|---|---|---|
| **JSON 数组被字符串化**（`acceptance_clauses` / `allowed_edit_scope` 的值是 `["[\\"...\\"]"]` 嵌套字符串，而非真数组） | **128** | **100%** | MCP-017 `T-1787321709894-21a00d8c`：`acceptance_clauses` = `["[\"public MCP name/parameters/result shape stable\", ...]"]` |
| **dependencies 为空数组** | **128** | **100%** | 全部机械合同 `dependencies=[]` |
| 通用风险/rollback（无真实 port/Gate 语义） | 128（推定） | 100% | 与 JSON 字符串化同源，`risks` 为模板化占位 |
| 缺失真实 port/Gate 语义 | 128（推定） | 100% | 机械模板不含任务唯一范围 |

**典型 envelope 反序列化结果（MCP-017）：**

```json
{
  "contract_id": "TC-T-1787321709894-21a00d8c",
  "revision": 1,
  "acceptance_clauses": ["[\"public MCP name/parameters/result shape stable\", \"Rust handler owns business logic\", ...]"],
  "allowed_edit_scope": ["[\"server/tools/tools_query.py\", \"server/compat_registry.py\", ...]"],
  "dependencies": []
}
```

根因：批量 bootstrap 回填时把"JSON list 再字符串化"写入，违反 Task Envelope 结构化契约
（`task_contract_payload` 校验要求数组字段为真数组）。

## 3. next_action 派工投影实测（BLOCKED 根因）

对 review 子任务逐一调用 `task.next_action`（reviewer 身份）：

| 派工决策 | 数量 | 典型任务 | 阻断原因 |
|---|---|---|---|
| READY / REVIEW | 88 | P0-K、MCP-068~070、CLI-005~096、INT-001 | `required_role=reviewer`，正常可审 |
| **BLOCKED / NONE** | **42** | MCP-017~053、MCP-067、CLI-004、P0-G、P0-J-D | `verdict 无法按持久化 normalization 规则验证（UNVERIFIED），保持 fail-closed` |
| OPEN / IN_PROGRESS | 2 | SRV-001~019、CLI-079/083~088 | 执行中 |
| CLOSED | 1 | P0-H | 已闭环 |

**BLOCKED 直接原因（MCP-017 实测返回）：**

```json
{
  "decision": "BLOCKED",
  "blocking_conditions": ["verdict 无法按持久化 normalization 规则验证（UNVERIFIED），保持 fail-closed"],
  "source": { "task_status": "review", "task_contract_hash": "" }
}
```

**对比对照组（人工补签的 CLI-02，结构正常）：** `decision=WAITING`、`required_role=reviewer`、`action=WAIT` —— 可审。
→ 证明：**合同 envelope 结构正确与否直接决定可审性**；42 个 BLOCKED 全部可归因于机械合同缺陷。

## 4. lease 遗留审计

| 类别 | 数量 | 说明 |
|---|---|---|
| 遗留 active reviewer lease（Epic 下 review 任务） | **132** | 含 127 个历史机械签发 + 5 个近期受控 acquire |
| 单任务多 active 冲突 | 待查 | scope-3 要求"每任务同角色仅一个可写 active lease" |
| 无 release 收口 | 普遍 | 机械签发的 lease 无 token 回传（缺陷 #4 同源），无法正式 release |

**遗留 lease 样本：**

| task | lease_id | holder |
|---|---|---|
| CLI-02 `d292ab3c` | L-0c71d0523e5cb988 | reviewer-wb-186loop |
| MCP-017 `21a00d8c` | L-c46e74f77be646fd | reviewer-wb-186loop |
| MCP-018 `2537b8b4` | L-cfda19086e0f2a0f | reviewer-wb-186loop |
| …（共 132 条） | | |

## 5. 修复约束（本任务必须遵守）

1. **append-only**：只能通过 `task.contract_revise` 追加 revision-2，禁止原地 UPDATE/DELETE/重跑 bootstrap。
2. **禁止直接 SQL 改合同/lease/status**：全部经 daemon RPC。
3. **不批量伪造**：128 张卡的 revision-2 必须逐卡有 source provenance + 独立审查，不得自动写入。
4. **revision-2 严格结构化**：JSON array 必须为真数组；必须含 objective/profile/allowed_edit_scope/interfaces/
   acceptance_clauses/risks/rollback/dependencies/handoff/source provenance/supersedes revision+hash。
5. **task.create 原子化**：同一 daemon transaction 写 task + binding + steps + contract rev1 + 三角色 lineage/revision +
   executor step binding + created event，任一步失败整体回滚。

## 6. 后续依赖（P0-G applied 后）

- 创建"revision-2 修订批次"任务（以每张真实 manifest 为输入）。
- 先修 CLI-02/CLI-03 未投影状态（注：CLI-02/03 已于 2026-08-24 受控补签，WAITING 可审，本审计确认满足）。
- 按 port Gate 与人工语义确认顺序处理 A′ 批量卡；SRV-019 与业务迁移卡不得因 P0-G 代码完成自动放行。

---

*审计证据均来自权威库只读查询与 daemon RPC 实测；无任何生产写入。*
