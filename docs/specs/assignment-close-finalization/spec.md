# close 终局 assignment 收口（Spec）

## 1. 目标

任务 close 之后，不得残留任何 `queued` / `claimed` assignment。

实证缺陷：卡 `T-1793614942434-83a49fb4` 关闭后，其 adjudicator assignment
`A-fbdc95b51dc29015025f5230` 仍为 `queued`（`project_task_assignments` 投影与物理
`task_assignments` 均未收敛）。原因：`handle_task_close` 只写 `tasks.status='closed'`
与 `closed` 事件，从不终结 assignment；Python `task_close` 亦只改任务状态。

## 2. 不变量

对于任意任务 T，一旦 `tasks.status = 'closed'`：

1. `task_events` 投影中 T 的全部 active assignment（`queued`/`claimed`）均被追加
   `assignment_completed` 事件，投影状态为 `completed`；
2. 物理表 `task_assignments` 中 T 的 `status='active'` 行全部收敛为 `completed`；
3. `current_assignment(conn, T, ..)` 返回 `None`。

## 3. 改动清单

### 3.1 `rust_ext/src/daemon/assignment_queue.rs`

新增（放在 `complete_assignments` 之后、`heartbeat_assignment` 之前）：

```rust
/// close 终局：把任务的全部 active assignment 收敛为 completed。
///
/// 事件投影（权威）与物理表（Python 治理面）必须在同一事务内同时收敛，
/// 否则 close 后仍可观察到可领取 assignment（T-1793614942434-83a49fb4）。
/// 返回被收敛的事件投影 assignment_id。
pub(crate) fn finalize_task_assignments(
    tx: &Transaction<'_>,
    task_id: &str,
    actor_identity: &str,
    actor_session_id: &str,
    seq: i64,
    ts: f64,
) -> Result<Vec<String>, DaemonRpcError> {
    let completed = complete_assignments(
        tx, task_id, None, None, actor_identity, actor_session_id, seq, ts,
    )?;
    let result = tx.execute(
        "UPDATE task_assignments SET status = 'completed'
         WHERE task_id = ?1 AND status = 'active'",
        params![task_id],
    );
    match result {
        Ok(_) => Ok(completed),
        Err(e) if e.to_string().contains("no such table") => Ok(completed),
        Err(e) => Err(DaemonRpcError::internal_error(format!(
            "close 收敛 task_assignments 失败: {e}"
        ))),
    }
}
```

说明：
- `role` / `step_id` 过滤参数传 `None`，即任务级全量收敛（含 adjudicator）。
- `no such table` 沿用 `persist_claimed_assignment` 的旧库兼容语义，其它错误上抛。
- 不新增物理表列、不做数据迁移（`status='completed'` 为既有词表外的收口值，
  Python 侧只按 `status='active'` 判定可领取，见 `db/db_task_leases.py#get_assignment`）。

### 3.2 `rust_ext/src/daemon/task_collab_lifecycle_apply.rs`

`handle_task_close`（当前 204–377 行）：
- 在 `closed` 事件 INSERT（约 346–353 行）之后、`record_action_identity`（355 行）之前：

```rust
let close_session = identity.as_ref().map(|id| id.session_id.clone()).unwrap_or_default();
let completed_assignments = assignment_queue::finalize_task_assignments(
    &tx, task_id, &owner_key, &close_session, self.next_seq(), ts,
)?;
```

- 响应对象新增 `"completed_assignments": string[]`（与既有 `task_id/status/closed_at/reviewer` 并列）。

`handle_task_cascade_close`（407 行起）：
- 在 `cascade_closed` 事件 INSERT（677–691 行）之后，对当前节点调用同一 helper：

```rust
assignment_queue::finalize_task_assignments(
    &tx, node, &owner_key,
    identity.as_ref().map(|id| id.session_id.as_str()).unwrap_or(""),
    self.next_seq(), ts,
)?;
```

- 响应结构不变（级联契约不为 assignment 扩字段）。

### 3.3 `rust_ext/src/daemon/task_collab_tests_lease.rs`

新增 `test_task_close_finalizes_all_active_assignments`：

1. `temp_db()` + `TaskCollabStore::new(...).with_clock(...)`；`peer` 同既有 close 测试。
2. `seed_task(&store, "T-CLS", "", "applied", true)`（叶子 + done step）。
3. `seed_reviewer_lease(&store, "T-CLS", "tok-c", 1, "agent-r", "sess-r", "model-r")`
   （同时插入 workspace id=1，供 `task_assignments.workspace_id` FK）。
4. 直写两条 `task_events`（`monotonic_seq=1001/1002`，payload 含
   `assignment_id`/`task_id`/`step_id`/`role`/`status`/`queued_at`）：
   - `assignment_queued`：executor，`A-exec-close`
   - `assignment_claimed`：reviewer，`A-rev-close`（含 holder 三字段）
5. 直写一条物理行：
   `INSERT INTO task_assignments (workspace_id, assignment_id, task_id, role, agent_id, session_id, model_id, status, created_at) VALUES (1, 'A-exec-close', 'T-CLS', 'executor', 'agent-e', 'sess-e', 'model-e', 'active', 1700000000.0)`。
6. 调用 `handle_task_close`（`lease_token="tok-c"`, `fencing_counter=1`, 带 close identity
   `agent-r/sess-r/model-r/role=reviewer`）。
7. 断言：
   - 返回 `status == "closed"`；
   - `completed_assignments` 同时含 `A-exec-close` 与 `A-rev-close`；
   - `project_task_assignments(&conn, "T-CLS")` 中无 `is_active()` 项，且两条均为 `completed`；
   - `SELECT COUNT(*) FROM task_assignments WHERE task_id='T-CLS' AND status='active'` 为 0，
     且存在 `status='completed'` 行。

### 3.4 `db/db_tasks.py`

新增私有方法（放在 `task_close` 之前）：

```python
def _finalize_active_assignments(self, task_id: str) -> List[str]:
    """close 终局：把该任务全部 active assignment 收敛为 completed。

    与 daemon `task.close` 的 assignment 收口语义对齐：close 后不得残留可领取
    assignment。返回被收敛的 assignment_id 列表。
    """
    try:
        cur = self.conn.execute(
            "SELECT assignment_id FROM task_assignments "
            "WHERE task_id = ? AND status = 'active'",
            (task_id,),
        )
        ids = [row["assignment_id"] for row in cur.fetchall()]
        if ids:
            self.conn.execute(
                "UPDATE task_assignments SET status = 'completed' "
                "WHERE task_id = ? AND status = 'active'",
                (task_id,),
            )
        return ids
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return []
        raise
```

`task_close` 中：
- 在 `UPDATE tasks SET status = closed ...`（2916–2919 行）之后、`self.conn.commit()`
  （2920 行）之前调用 `completed_assignments = self._finalize_active_assignments(task_id)`。
- 成功响应新增 `"completed_assignments": completed_assignments`。

### 3.5 `tests/test_task_close.py`

新增 `test_task_close_finalizes_active_assignments`：

1. `_db_with_workspace()`；建任务（带 1 step），置 `review`，`task_apply`。
2. 用 `SELECT id FROM workspaces LIMIT 1` 取 workspace_id，直写物理 active 行
   （`assignment_id="ASG-close-fixture"`, role=`executor`, holder 三字段非空）。
3. `db.task_close(task_id, reviewer="closer")`。
4. 断言：`status == TASK_STATUS_CLOSED`；`completed_assignments == ["ASG-close-fixture"]`；
   `SELECT status FROM task_assignments WHERE assignment_id='ASG-close-fixture'` 为
   `completed`；该任务无 `status='active'` 行。

### 3.6 `docs/design/cli-mcp-matrix-backlog.md`

追加 `## 34. close 终局 assignment 收口（T-<本卡>）`，含：
- §34.1 现象（实证 assignment 残留 + 根因：close 不终结 assignment）
- §34.2 处置（daemon close/cascade_close + Python close 同一事务收敛；事件权威 + 物理表）
- §34.3 验证（下表命令与结果）
- §34.4 边界（非目标：reopen 后重建 assignment、`revoked` 语义、历史残留批量清理）

## 4. 非目标

- 不批量清理历史已关闭任务遗留的 assignment（另卡或运维脚本）。
- 不改 `reopen` 语义（reopen 后由 handoff/report 重新排 assignment）。
- 不改 `assignment_show` / `get_assignment` 的查询面。
- 不新增 schema 迁移。

## 5. 验证

| 项 | 命令 | 期望 |
| --- | --- | --- |
| Rust 单测（helper） | `cargo test --manifest-path rust_ext/Cargo.toml finalize_task_assignments` | PASS |
| Rust 集成（close） | `cargo test --manifest-path rust_ext/Cargo.toml test_task_close_finalizes_all_active_assignments` | PASS |
| Python close 回归 | `$env:PYTHONPATH='c:\git_work'; C:\Python314\python.exe -m pytest tests/test_task_close.py -o addopts='' -q` | 全 PASS（含新增用例） |
| 验收矩阵 | `python scripts/check_e2e_matrix_closure.py` | PASS（exit 0） |
| 静态检查 | `cargo fmt --check`（rust_ext） | 无 diff |

## 6. 交付流程（A′ 四角色）

1. `cw task create`：steps 的 `target_file` 覆盖上述 6 个文件（含 docs、测试）。
2. executor：`lease acquire --role implementer` → 逐步 `task report` → `collab publish`
   → `task handoff executor_ready_for_review`。
3. reviewer：独立身份 `lease acquire --role reviewer` → `collab verdict`（pass）
   → `task handoff reviewer_pass`，证据落 `.trae-cn/evidence/`。
4. adjudicator：reviewer-scope lease（fencing=2，同身份）→ `task apply` / `task close`
   → `governance-projection` 为 closed/completed/COMPLETE/finalize → release lease。
5. commit（message 内嵌 task_id）+ `cw_task_commit_ledger.json` 台账条目单独提交
   → 复跑验收矩阵。
