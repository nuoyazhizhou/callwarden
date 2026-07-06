# 任务状态机设计文档

> 任务 ID: T-1783309017863-a1b6
> 父任务: T-1783253838055-f033 (Agent Rule Memory)
> 设计原则: 写代码的 Agent 不能自己 apply/close 任务，必须由其他会话的 LLM 审核执行

## 1. 状态机总览

### 1.1 任务状态

```
任务状态: open → in_progress → review → applied → closed
                                       ↘ reverted
```

| 状态 | 含义 | 进入条件 |
|------|------|---------|
| `open` | 任务已创建，未开始 | task_create |
| `in_progress` | 任务执行中 | 第一个子任务被领取 / 自身步骤被领取 |
| `review` | 开发完成，待审核 | 所有步骤 done + 所有子任务 review/applied/closed |
| `applied` | 审核通过 | 其他会话 LLM 执行 task_apply（仅叶子任务）|
| `closed` | 任务关闭 | 其他会话 LLM 执行 task_close（仅叶子任务）/ 级联触发 |

### 1.2 步骤状态

```
步骤状态: pending → in_progress → done / failed / blocked
                                ↘ failed 后自动插入 fix_defect 步骤
```

## 2. 父任务状态自动推进

### 2.1 推进规则

父任务（有子任务的任务）状态**不能手动 apply/close**，由系统根据子任务状态自动推进：

| 父任务状态转换 | 触发条件 | 触发动作 |
|---------------|---------|---------|
| `open → in_progress` | 第一个子任务被 `task_next_step` 领取 | 遍历父任务链，所有 open 状态的父任务推进到 in_progress |
| `in_progress → review` | 所有子任务都是 `review`/`applied`/`closed` + 自身步骤全 done | `_update_parent_status` 递归向上检查 |
| `review → applied → closed` | 最后一个子任务被 apply 时级联触发 | `_cascade_close_if_ready` 原子操作 |

### 2.2 父任务禁止手动 apply/close

```python
# task_apply 检查
if subtask_count > 0:
    return {
        "error": "Parent task cannot be applied manually; it is auto-cascaded when all subtasks are applied",
        "reason": "parent_task_must_cascade",
        "subtask_count": subtask_count,
    }

# task_close 检查
if subtask_count > 0:
    return {
        "error": "Parent task cannot be closed manually; it is auto-cascaded when all subtasks are applied",
        "reason": "parent_task_must_cascade",
        "subtask_count": subtask_count,
    }
```

## 3. 级联 close 机制

### 3.1 触发条件

**触发点**: 最后一个子任务被 `task_apply` 时

**检查逻辑**:
1. 子任务被 apply（review → applied）
2. 查询所有兄弟子任务状态
3. 若全部 `applied`/`closed` → 触发级联 close
4. 否则保持 applied，等待其他兄弟任务被 apply

### 3.2 级联 close 原子操作

`_cascade_close_if_ready(parent_id, reviewer, now)` 方法执行：

```
1. close 所有 applied 状态的兄弟任务 (applied → closed)
2. 父任务 review → applied → closed（一次性推进）
3. 递归向上：若父任务也有父任务（祖父），检查祖父的所有子任务（父任务的兄弟）是否都已 closed
   - 是 → 继续级联 close 祖父层
   - 否 → 停止递归
```

### 3.3 级联示例

**场景**: 父任务 P 有 3 个子任务 A、B、C

```
初始状态:
  P: review
  A: review
  B: review
  C: review

Step 1: apply A (reviewer-A)
  A: applied  (不级联，B 和 C 还是 review)
  P: review   (保持)

Step 2: apply B (reviewer-B)
  B: applied  (不级联，C 还是 review)
  P: review   (保持)

Step 3: apply C (reviewer-C)  ← 最后一个，触发级联
  C: applied → closed
  A: applied → closed  ← 自动 close
  B: applied → closed  ← 自动 close
  P: review → applied → closed  ← 自动推进

返回值:
  {
    "task_id": "C",
    "status": "applied",
    "cascaded_close": ["A", "B", "C", "P"]  ← 所有自动 close 的 task_id
  }
```

### 3.4 多层嵌套级联

**场景**: 祖父 G → 父 P → 子 A、B

```
Step 1: apply A
  A: applied (不级联)

Step 2: apply B  ← 触发级联
  B: applied → closed
  A: applied → closed
  P: review → applied → closed
  ↑ 检查 G 的所有子任务（P 的兄弟）：若都是 closed，继续级联 close G
  G: review → applied → closed  ← 递归向上

返回值:
  {
    "task_id": "B",
    "status": "applied",
    "cascaded_close": ["A", "B", "P", "G"]
  }
```

## 4. 状态机约束

### 4.1 状态约束表

| 任务类型 | 可手动 apply | 可手动 close | 状态自动推进 |
|---------|-------------|-------------|-------------|
| 叶子任务（无子任务） | ✅ | ✅ | open → in_progress → review |
| 父任务（有子任务） | ❌ | ❌ | open → in_progress → review → applied → closed |

### 4.2 状态一致性保证

- **原子性**: 级联 close 在单个 SQLite 事务中完成，要么全部成功，要么全部回滚
- **幂等性**: `_cascade_close_if_ready` 检查父任务状态，只有 `review` 状态才级联（避免重复 close）
- **递归向上**: 级联 close 父任务后，继续检查祖父任务是否满足级联条件

## 5. API 参考

### 5.1 task_apply

```python
def task_apply(task_id: str, reviewer: str = "reviewer") -> Dict[str, Any]:
```

**返回值**:
- 成功（无级联）: `{"task_id": ..., "status": "applied", "applied_at": ..., "reviewer": ...}`
- 成功（触发级联）: 多一个 `cascaded_close: List[str]` 字段
- 失败: `{"error": ..., "task_id": ..., "status": ...}`

**拒绝场景**:
- 父任务手动 apply（reason: `parent_task_must_cascade`）
- 状态不是 review（reason: `invalid_status`）
- 任务不存在

### 5.2 task_close

```python
def task_close(task_id: str, reviewer: str = "reviewer") -> Dict[str, Any]:
```

**返回值**:
- 成功: `{"task_id": ..., "status": "closed", "closed_at": ..., "reviewer": ...}`
- 失败: `{"error": ..., "task_id": ..., "status": ..., "reason": ..., "subtask_count": ...}`

**拒绝场景**:
- 父任务手动 close（reason: `parent_task_must_cascade`）
- 状态不是 applied（reason: `invalid_status`）
- 任务不存在

### 5.3 _cascade_close_if_ready

```python
def _cascade_close_if_ready(parent_id: str, reviewer: str, now: float) -> List[str]:
```

**内部方法**，由 `task_apply` 在子任务 apply 后调用。

**返回值**: 被自动 close 的 task_id 列表（含子任务和父任务）

## 6. 设计原则

### 6.1 写代码的 Agent 不能自己 apply/close

**问题**: 如果写代码的 Agent 可以自己 apply/close 任务，会基于奖励函数激励直接 close 任务，跳过审核。

**解决**: 写代码的 Agent 只能把任务推进到 `review` 状态（通过完成所有步骤），`apply` 和 `close` 必须由其他会话的 LLM 调用。

### 6.2 父任务由系统自动推进

**问题**: 父任务的 apply/close 需要手动调用吗？

**解决**: 父任务禁止手动 apply/close，由系统在最后一个子任务 apply 时自动级联触发。这样保证父任务状态与子任务状态一致，不会出现"父任务 closed 但子任务还 applied"的不一致状态。

### 6.3 原子性保证

**问题**: 级联 close 中途失败怎么办？

**解决**: 级联 close 在单个 SQLite 事务中完成（`self.conn.commit()` 在最后调用）。中途任何异常都会导致整个事务回滚，不会产生中间不一致状态。

## 7. 测试覆盖

测试文件: [tests/test_task_cascade_close.py](../../tests/test_task_cascade_close.py)

| 测试 | 场景 |
|------|------|
| `test_single_subtask_apply_no_cascade` | 单子任务 apply 不触发级联（兄弟未完成）|
| `test_last_subtask_apply_triggers_cascade_close` | 最后一个子任务 apply 触发级联 |
| `test_parent_task_manual_apply_forbidden` | 父任务禁止手动 apply |
| `test_parent_task_manual_close_forbidden` | 父任务禁止手动 close |
| `test_multilevel_cascade_close` | 多层嵌套（祖父-父-子）级联 |
| `test_parent_auto_promote_to_in_progress_on_next_step` | 领取子任务时父任务 → in_progress |
| `test_parent_auto_promote_to_review_when_all_subtasks_review` | 所有子任务 review 时父任务 → review |
| `test_cascade_not_triggered_when_subtask_not_review` | 部分子任务未 review 时不级联 |
| `test_cascade_close_writes_timestamps` | 级联 close 时间戳正确写入 |
| `test_cascade_close_reviewer_passthrough` | reviewer 字段正确传递 |

## 8. 相关代码

| 文件 | 说明 |
|------|------|
| [db/db_tasks.py](../../db/db_tasks.py) | TaskMixin 实现 |
| [db/schema.py](../../db/schema.py) | SCHEMA_VERSION = 24，tasks 表含 applied_at 字段 |
| [tests/test_task_cascade_close.py](../../tests/test_task_cascade_close.py) | 级联 close 测试 |
| [tests/test_task_close.py](../../tests/test_task_close.py) | 基础 apply/close 测试 |

## 9. 历史演进

| 版本 | 变更 |
|------|------|
| v23 | 新增 Agent Rule Memory 表 |
| v24 | tasks 表新增 `applied_at` 字段，支持 review → applied 状态转换 |
| T-1783302795079-0017 | 实现 task_apply 和 task_close 基础命令 |
| T-1783309017863-a1b6 | 实现级联 close 与父任务状态自动推进（本文档）|
