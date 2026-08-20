# 实施计划：修复 task.split 丢失子任务步骤

**任务 ID**: T-1786332208647-eb4a39c0
**缺陷根因**: `rust_ext/src/daemon/task_collab.rs:1444-1461` plan_file 路径只 INSERT tasks 不调用 insert_task_steps，违反该函数自身 doc-comment 的设计契约。

## S1: 修复 plan_file 路径调用 insert_task_steps
- **target_file**: rust_ext/src/daemon/task_collab.rs
- **target_symbol**: handle_task_split (行 1404-1481)
- **验收标准**:
  - plan_file 路径解析后必须为每个子任务调用 insert_task_steps
  - 子任务创建与步骤写入处于同一 unchecked_transaction
  - 任一步骤写入失败时整个事务回滚，不留下半成品子任务
- **测试命令**: `cargo test --manifest-path rust_ext/Cargo.toml task_collab::tests --lib`
- **禁止**: 不得旁路 SQLite，不得在 Python/Rust 两边重复实现不同语义

## S2: 补充 task_steps 字段完整性
- **target_file**: rust_ext/src/daemon/task_collab.rs
- **target_symbol**: insert_task_steps (行 188-218), parse_subtasks_from_plan_text (行 220-240)
- **验收标准**:
  - 每个 step 保留: action, target_file, target_symbol, check_items, step_index
  - parse_subtasks_from_plan_text 解析出的 steps 结构与 subtasks 参数路径一致
- **测试命令**: `cargo test --manifest-path rust_ext/Cargo.toml task_collab::tests --lib`

## S3: 增加回归测试
- **target_file**: rust_ext/src/daemon/task_collab.rs (#[cfg(test)] 模块)
- **验收标准**:
  - split 含 2+ steps 的子任务，查询 task status 能看到完整 steps
  - 顺序和字段保持一致
  - 多个子任务不互相串步骤
  - 中途失败时不产生部分子任务或部分步骤
- **测试命令**: `cargo test --manifest-path rust_ext/Cargo.toml task_collab::tests --lib`

## S4: Windows daemon/CLI 真实入口测试
- **target_file**: tests/test_task_split_steps.py (新建)
- **验收标准**:
  - 通过 route_task_write 走 daemon RPC 路径验证 split 后 steps 完整
  - 通过 cw task split --plan 命令行验证
- **测试命令**: `python -m pytest tests/test_task_split_steps.py -v`
- **状态**: ✅ 完成（4 passed，含进程级 E2E）
  - E2E 验证 `cw task split --plan` 后 sub-1=2 步（implement/test）、sub-2=1 步（implement）
  - E2E fixture 修正：`_client` 捕获 `subprocess.TimeoutExpired`（管道未就绪时 cw-client 会阻塞等待），轮询前预留 1.5s 初始化时间

## S5（追加）: 修复 status_tree 显示层丢失 steps（验证期间新发现）
- **背景**: S1-S3 修复后数据已正确落库（`task.work_next` 返回 3 步），但 `cw task show` / `task.status_tree` 仍显示 Steps(0)。定位为显示层 bug，非数据层。
- **target_file**: rust_ext/src/daemon/task_collab.rs
- **target_symbol**: build_task_tree_node (行 1966-2099)
- **根因 1**: `r.get::<_, i64>(0)` 把 TEXT 类型的 `step_id` 当 i64 读 → 行转换失败 → `rows.flatten()` 静默丢弃所有行
- **根因 2**: `r.get::<_, f64>(9)` 读 nullable 的 `completed_at`，pending 步骤为 NULL → 该行转换失败同样被 flatten 丢弃（即使修了根因 1 仍全空）
- **修复**: step_id 改 `String` 读取 + `Value::String`；completed_at 改 `Option<f64>` 读取，None 输出 `Value::Null`（与 Python 侧 `row["completed_at"]` 语义一致）
- **验收**:
  - `cargo test --manifest-path rust_ext/Cargo.toml --lib task_collab` 10 passed（新增回归测试 `test_task_status_tree_shows_pending_child_steps`）
  - 真实 CLI `cw task show T-1786420289506-d7927a70-sub-1` 显示 Steps (3) edit/test/verify
  - 任务 A 子任务 `T-1786332208647-eb4a39c0-sub-1/sub-2` 各显示 Steps (4)（非 0，Coordinator 验收达成）

## 执行记录
| 步骤 | 状态 | 验证 |
|---|---|---|
| S1 | ✅ | `cargo test` 通过；`handle_task_split` plan_file 分支调用 `insert_task_steps(&tx, &sub_id, &step_values, ts)?` |
| S2 | ✅ | 解析产出 action/target_file/target_symbol/check_items 四字段；代码块围栏跳过 |
| S3 | ✅ | 新增 4 个 Rust split 测试 + 1 个 status_tree 回归测试，10 passed |
| S4 | ✅ | `python -m pytest tests/test_task_split_steps.py -v` 4 passed（含进程级 E2E） |
| S5 | ✅ | 真实 CLI 显示 Steps(3)/(4)，steps 非 0 |
