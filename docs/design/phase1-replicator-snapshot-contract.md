# Phase 1 子任务 4 契约：Replicator 与 SnapshotManager 只读查询 API

> 本文件是全量 Rust 迁移自举计划 Phase 1 第四个功能子任务的 contract 交付物。
>
> 关联：
> - 总计划：[rust-full-migration-self-bootstrap-plan.md](rust-full-migration-self-bootstrap-plan.md) Phase 1 §4
> - 上游契约：[phase1-manifest-contract.md](phase1-manifest-contract.md)（manifest 只读查询）
> - 真相源：[server/replicator.py](../../server/replicator.py) / [server/snapshot_manager.py](../../server/snapshot_manager.py) / [rust_ext/src/snapshot.rs](../../rust_ext/src/snapshot.rs) / [rust_ext/src/daemon/replicator.rs](../../rust_ext/src/daemon/replicator.rs)

## 1. 范围

| 项 | 说明 |
|---|---|
| **目标** | 将 Rust 端 Replicator + SnapshotManager 的**只读查询方法**通过 PyO3 暴露给 Python，与 Python `server/replicator.py` + `server/snapshot_manager.py` 路径建立 ✅(behavioral) 差分 |
| **Rust 端实现** | 部分就绪：`rust_ext/src/snapshot.rs` 已暴露 `PySnapshotManager` + `PySnapshotCache`（8+11 个方法），但 Python `SnapshotManagerService` 的部分 query_* 方法未封装暴露；`rust_ext/src/daemon/replicator.rs` 已实现但通过 daemon RPC dispatch 调用，未通过 PyO3 暴露 |
| **不在本子任务** | 1) 任何写操作（`publish_snapshot` / `replicate` / `recover` / `gc_snapshots` / `evict_workspace`）仍走 Python（AGENTS.md 规则 6）<br>2) backup/restore 的 PyO3 暴露（migration-manifest §1.3 明确 Phase 4 迁移）<br>3) daemon_handle_refresh 主流程（仍由 `server/replicator.py` 主导）<br>4) GraphStore 已暴露的 `get_callers` / `get_callees` / `search_symbols`（不重复封装） |

## 2. 现状盘点（来自调研）

### 2.1 Python `server/replicator.py` 公开方法

| 方法 | 类型 | 说明 |
|---|---|---|
| `init_session_schema(conn)` | 写 | 创建 agent_sessions + workspace_active_session + file_generations schema |
| `daemon_handle_connect(...)` | 写 | agent 连接握手，分配 epoch |
| `daemon_handle_refresh(...)` | 写 | refresh 主流程入口（session epoch 校验 + 两阶段 CAS + P0-1 merge） |
| `Replicator.replicate(...)` | 写 | 合并 staging log delta，发布新 generation |
| `Replicator.recover(...)` | 写 | crash 恢复 |
| `Replicator.get_pending_count(workspace_id)` | 只读 | 获取 pending entries 数量 |

**Rust 端状态**：`daemon/replicator.rs` 已实现上述全部方法，但通过 daemon RPC dispatch 调用，未通过 PyO3 暴露。

### 2.2 Python `server/snapshot_manager.py` 公开方法（SnapshotManagerService）

| 方法 | 类型 | Rust PyO3 状态 |
|---|---|---|
| `publish_snapshot(...)` | 写 | ✅ `PySnapshotManager::build_and_publish` |
| `get_snapshot_stats(workspace_instance_id)` | 只读 | ✅ `PySnapshotManager::snapshot_stats` |
| `get_current_generation(workspace_instance_id)` | 只读 | ✅ `PySnapshotManager::current_generation` |
| `list_workspaces()` | 只读 | ✅ `PySnapshotCache::list_workspaces` |
| `evict_workspace(workspace_instance_id)` | 写 | ✅ `PySnapshotCache::evict` |
| `gc_snapshots(keep_last)` | 写 | ✅ `PySnapshotManager::gc_generations` |
| `query_callers(workspace_instance_id, callee_name)` | 只读 | ⚠️ 走 GraphStore（已暴露，未通过 SnapshotManager 封装） |
| `query_callees(workspace_instance_id, caller_name)` | 只读 | ⚠️ 同上 |
| `search_symbols(workspace_instance_id, query)` | 只读 | ⚠️ 同上 |
| `query_symbol(workspace_instance_id, qualified_name)` | 只读 | ⚠️ 同上 |
| `query_call_chain_down(workspace_instance_id, root)` | 只读 | 🔴 未暴露 |
| `query_topological_order(workspace_instance_id)` | 只读 | 🔴 未暴露 |
| `query_detect_cycles(workspace_instance_id)` | 只读 | 🔴 未暴露 |
| `query_stats(workspace_instance_id)` | 只读 | 🔴 未暴露（GraphStore 已实现 `get_stats`，但未通过 SnapshotManager 暴露） |
| `ensure_workspace(workspace_instance_id)` | 写 | 🔴 未暴露（Phase 4 daemon RPC 处理） |

### 2.3 Rust 端 `snapshot.rs` 已通过 PyO3 暴露的方法

**PySnapshotManager（8 个方法）**：

| 方法 | 类型 | 对应 Python 方法 |
|---|---|---|
| `new(workspace_instance_id)` | — | `SnapshotManagerService.__init__` 内部 |
| `current_generation()` | 只读 | `get_current_generation` |
| `build_and_publish(db_path, workspace_id, build_context_hash, snapshot_id)` | 写 | `publish_snapshot` |
| `gc_generations(keep_last)` | 写 | `gc_snapshots` |
| `history_len()` | 只读 | （Python 端无直接对应，但等价 `len(history)`） |
| `list_generations()` | 只读 | （Python 端无直接对应） |
| `snapshot_stats()` | 只读 | `get_snapshot_stats` |
| `current_store()` | 只读 | （Python 端 `_get_rust_graph_store` 内部使用） |

**PySnapshotCache（11 个方法）**：

| 方法 | 类型 | 对应 Python 方法 |
|---|---|---|
| `new(max_workspaces)` | — | `SnapshotManagerService.__init__` |
| `get_or_create(workspace_id)` | — | `ensure_workspace` 内部 |
| `get(workspace_id)` | 只读 | （内部辅助） |
| `list_workspaces()` | 只读 | `list_workspaces` |
| `len()` | 只读 | （无直接对应） |
| `evict(workspace_id)` | 写 | `evict_workspace` |
| `diff_symbol(...)` | 只读 | （差分 API） |
| `diff_signature(...)` | 只读 | （差分 API） |
| `diff_callers(...)` | 只读 | （差分 API） |
| `diff_callees(...)` | 只读 | （差分 API） |
| `count_symbols_in_scope(...)` | 只读 | （无直接对应） |
| `compare_snapshots(...)` | 只读 | （无直接对应） |

### 2.4 未通过 PyO3 暴露的 SnapshotManager 查询方法

| Python 方法 | Rust 内部实现 | 暴露状态 |
|---|---|---|
| `query_call_chain_down` | `graph::GraphStore::compute_call_chain_down`（已实现） | 🔴 未封装到 PySnapshotManager |
| `query_topological_order` | `graph::GraphStore::topological_sort`（已实现） | 🔴 未封装到 PySnapshotManager |
| `query_detect_cycles` | `graph::GraphStore::detect_cycles`（已实现） | 🔴 未封装到 PySnapshotManager |
| `query_stats` | `graph::GraphStore::get_stats`（已实现） | 🔴 未封装到 PySnapshotManager |

**关键观察**：上述 4 个方法在 Python 端的实际实现路径是：`SnapshotManagerService.query_*` → `_get_rust_graph_store(workspace_instance_id)` → `GraphStore::*`。即业务逻辑已在 Rust，仅缺少通过 `PySnapshotManager` 的便捷封装。

### 2.5 backup/restore 范围说明

**不在本子任务范围**：

- `server/backup_restore.py` 的 Python `BackupManager` / `RestoreManager` 完整功能（`backup_full` / `backup_db_only` / `list_backups` / `get_backup_info` / `delete_backup` / `cleanup_old_backups` / `restore` / `verify_backup`）仍走 Python
- `rust_ext/src/daemon/workspace.rs::handle_backup` / `handle_restore` 仅覆盖 registry DB 单库的 backup/restore，由 daemon RPC dispatch 调用，不通过 PyO3 暴露
- migration-manifest.md §1.3 明确 backup/restore 在 Phase 4 迁移

## 3. API 契约（本子任务暴露清单）

### 3.1 Replicator 只读查询（1 个，新建 `rust_ext/src/replicator_query.rs`）

> **修正说明**：原契约误将 `staging_log` 描述为 SQLite 表。真相是 JSON Lines 文件
> （见 [server/staging_log.py](../../server/staging_log.py) 和
> [rust_ext/src/daemon/staging_log.rs](../../rust_ext/src/daemon/staging_log.rs)）。
> 因此 `replicator_get_pending_count` 走文件路径，不走 SQLite 只读连接。

```python
def replicator_get_pending_count(log_path: str, workspace_id: Optional[str] = None) -> int:
    """查询 pending staging log entries 数量

    与 Python `Replicator.get_pending_count(workspace_id)` 行为一致：
    - 文件不存在 → 返回 0
    - log_path 无 pending → 返回 0
    - 有 N 个 pending → 返回 N
    - workspace_id=None → 返回所有 pending 总数
    - workspace_id 指定 → 仅返回该 workspace 的 pending 数量

    log_path 应指向 staging log 文件（JSON Lines 格式）
    """
```

### 3.2 SnapshotManager 便捷查询（4 个，扩展 `rust_ext/src/snapshot.rs::PySnapshotManager`）

```python
def query_call_chain_down(self, root: str, max_depth: int = 10) -> list[dict]:
    """向下调用链查询

    与 Python `SnapshotManagerService.query_call_chain_down(workspace_instance_id, root)` 行为一致：
    - root 符号不存在 → 返回 []
    - root 存在 → 返回 [{name, depth, ...}, ...]
    - max_depth 限制递归深度
    """

def query_topological_order(self) -> list[str]:
    """拓扑排序

    与 Python `SnapshotManagerService.query_topological_order(workspace_instance_id)` 行为一致：
    - 返回按拓扑序排列的符号 qualified_name 列表
    - 含循环时返回部分排序结果（不抛错）
    """

def query_detect_cycles(self) -> list[list[str]]:
    """循环检测

    与 Python `SnapshotManagerService.query_detect_cycles(workspace_instance_id)` 行为一致：
    - 无循环 → 返回 []
    - 有循环 → 返回 [[node1, node2, ...], ...] 每个内层列表是一个循环
    """

def query_stats(self) -> dict:
    """统计信息

    与 Python `SnapshotManagerService.query_stats(workspace_instance_id)` 行为一致：
    - 返回 dict，含 symbols/calls/edges/files_count 等字段
    """
```

### 3.3 不暴露的 API（仍走 Python）

| API | 原因 |
|---|---|
| `daemon_handle_refresh` / `daemon_handle_connect` | 写操作（session 管理 + CAS 两阶段提交） |
| `Replicator.replicate` / `recover` | 写操作（合并 delta + 发布 generation） |
| `publish_snapshot` | 写操作（已在 PySnapshotManager.build_and_publish 暴露） |
| `evict_workspace` | 写操作（已在 PySnapshotCache.evict 暴露） |
| `gc_snapshots` | 写操作（已在 PySnapshotManager.gc_generations 暴露） |
| `ensure_workspace` | 写操作（Phase 4 daemon RPC 处理） |
| `BackupManager.*` / `RestoreManager.*` | Phase 4 迁移范围 |

## 4. 行为契约（Python ↔ Rust 必须一致）

### 4.1 replicator_get_pending_count（P1-P3）

| # | 场景 | Python `Replicator.get_pending_count` | Rust `replicator_get_pending_count` | 差分断言 |
|---|---|---|---|---|
| P1 | 文件不存在 | `staging_log.read_pending()` 返回空 → 0 | `StagingLog::new` 创建空文件 + `read_pending` 空 → 0 | `assert py == rust == 0` |
| P2 | workspace 无 pending | 返回 0 | 返回 0 | `assert py == rust == 0` |
| P3 | workspace 有 N 个 pending | 返回 N | 返回 N | `assert py == rust == N` |
| P4 | workspace_id=None | 返回所有 pending 总数 | 返回所有 pending 总数 | `assert py == rust` |

### 4.2 query_call_chain_down（Q1-Q4）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| Q1 | root 不存在 | 返回 [] | 返回 [] | `assert py == rust == []` |
| Q2 | root 存在，无下游 | 返回 [{root, 0}] | 返回 [{root, 0}] | `assert py == rust` |
| Q3 | root 存在，2 层下游 | 返回 [{root,0}, {child1,1}, {child2,2}] | 返回 list[dict] | `assert py == rust`（字段逐一比对） |
| Q4 | max_depth=1 | 只返回 depth<=1 的节点 | 只返回 depth<=1 的节点 | `assert py == rust` |

### 4.3 query_topological_order（T1-T3）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| T1 | 空 GraphStore | 返回 [] | 返回 [] | `assert py == rust == []` |
| T2 | DAG 无循环 | 返回拓扑序列表 | 返回拓扑序列表 | `assert py == rust`（顺序一致） |
| T3 | 含循环 | 返回部分排序（不抛错） | 返回部分排序 | `assert py == rust`（或两端都返回相同子集） |

### 4.4 query_detect_cycles（D1-D3）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| D1 | 无循环 | 返回 [] | 返回 [] | `assert py == rust == []` |
| D2 | 单个循环 | 返回 [[node1, node2, ...]] | 返回 [[node1, ...]] | `assert py == rust` |
| D3 | 多个循环 | 返回 [[...], [...]] | 返回 [[...], [...]] | `assert len(py) == len(rust)` |

### 4.5 query_stats（S1-S2）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| S1 | 空 GraphStore | 返回 {symbols:0, calls:0, ...} | 返回 dict | `assert py == rust` |
| S2 | 有数据 | 返回 {symbols:N, calls:M, ...} | 返回 dict | `assert py == rust`（字段逐一比对） |

## 5. 事务边界（AGENTS.md 规则 6）

### 5.1 只读策略

所有 `replicator_*` / `query_*` 查询方法必须满足：

1. **不写入文件**：`replicator_get_pending_count` 仅读取 JSON Lines 文件，不写入；其他 query_* 走内存 GraphStore
2. **不激活 workspace**：不调用 `register_workspace` / `set_active_workspace`
3. **不持有 SQLite 写锁**：纯只读，不进入写事务
4. **SnapshotManager 查询走内存**：4 个 query_* 方法访问 ArcSwap 保护的 GraphStore，无锁/无 SQLite

### 5.2 SnapshotManager 查询的特殊性

`PySnapshotManager` 的查询方法（`query_call_chain_down` / `query_topological_order` / `query_detect_cycles` / `query_stats`）**不访问 SQLite**，而是走内存中的 `GraphStore`（CSR 邻接表 + 内存索引）。无 WAL/锁问题，但需注意：

- 调用前需确保 snapshot 已通过 `build_and_publish` 加载到内存
- 调用前需确保 workspace 已通过 `PySnapshotCache::get_or_create` 注册
- 与 Python `SnapshotManagerService.query_*` 调用路径一致（都走 Rust GraphStore）

### 5.3 不与 Python 写入冲突

- Rust 只读查询 ↔ Python 写入：`replicator_get_pending_count` 读 JSON Lines 文件，与 append/compact 不冲突（最多读到旧数据，最终一致）
- SnapshotManager 查询走 ArcSwap 原子发布的 GraphSnapshot，与 publish 并发安全
- daemon 内部的 Rust 写入路径（`daemon/replicator.rs::Replicator::replicate`）独立运行，不通过 PyO3 暴露，不影响本子任务

## 6. 实现计划

### 6.1 Rust 端

**新文件 `rust_ext/src/replicator_query.rs`**：

```rust
//! Phase 1-4: Replicator 只读查询 API（PyO3 暴露层）
//!
//! 对应 Python `server/replicator.py::Replicator.get_pending_count`：
//! - `replicator_get_pending_count`——查询 pending staging log entries 数量
//!
//! 实现：基于 `daemon::staging_log::StagingLog` 读 JSON Lines 文件，
//! 不走 SQLite（与 Python 端一致）。

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;

#[pyfunction]
#[pyo3(signature = (log_path, workspace_id=None))]
pub fn replicator_get_pending_count(log_path: &str, workspace_id: Option<String>) -> PyResult<usize> {
    // StagingLog::new(log_path) → read_pending() → 按 workspace_id 过滤
    // 文件不存在/无法解析 → 返回 0
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(replicator_get_pending_count, m)?)?;
    Ok(())
}
```

**扩展 `rust_ext/src/snapshot.rs::PySnapshotManager`**，新增 4 个 `#[pymethods]`：

```rust
impl PySnapshotManager {
    /// 向下调用链查询
    pub fn query_call_chain_down(&self, root: &str, max_depth: i32) -> PyResult<Vec<PyObject>> {
        let guard = self.inner.read();
        if let Some(snapshot) = guard.as_ref() {
            let chain = snapshot.store.compute_call_chain_down(root, max_depth);
            // 转换为 Vec<dict>
        } else {
            Ok(vec![])
        }
    }

    /// 拓扑排序
    pub fn query_topological_order(&self) -> PyResult<Vec<String>> {
        let guard = self.inner.read();
        if let Some(snapshot) = guard.as_ref() {
            Ok(snapshot.store.topological_sort())
        } else {
            Ok(vec![])
        }
    }

    /// 循环检测
    pub fn query_detect_cycles(&self) -> PyResult<Vec<Vec<String>>> {
        let guard = self.inner.read();
        if let Some(snapshot) = guard.as_ref() {
            Ok(snapshot.store.detect_cycles())
        } else {
            Ok(vec![])
        }
    }

    /// 统计信息
    pub fn query_stats(&self) -> PyResult<PyObject> {
        let guard = self.inner.read();
        if let Some(snapshot) = guard.as_ref() {
            // 调用 snapshot.store.get_stats() 并转为 dict
        } else {
            // 返回空 dict
        }
    }
}
```

### 6.2 PyO3 注册（`rust_ext/src/lib.rs`）

```rust
mod replicator_query;
// ...
m.add_function(wrap_pyfunction!(replicator_query::replicator_get_pending_count, m)?)?;
```

### 6.3 差分测试（`tests/test_phase1_behavioral_diff.py` 追加）

- `TestReplicatorGetPendingCountDiff`：P1-P3（replicator_get_pending_count 差分）
- `TestSnapshotQueryCallChainDownDiff`：Q1-Q4（query_call_chain_down 差分）
- `TestSnapshotTopologicalOrderDiff`：T1-T3（query_topological_order 差分）
- `TestSnapshotDetectCyclesDiff`：D1-D3（query_detect_cycles 差分）
- `TestSnapshotQueryStatsDiff`：S1-S2（query_stats 差分）

### 6.4 wire-production（`db/db_rollback_config.py` 登记）

```powershell
cw rollback register `
  --task-id T-1785148066853-ccad23fe `
  --feature rust_replicator_snapshot_query `
  --phase 1 `
  --production-entry "rust_ext/src/replicator_query.rs + rust_ext/src/snapshot.rs::PySnapshotManager" `
  --rollback-entry "server/replicator.py:get_pending_count + server/snapshot_manager.py:query_*" `
  --window 2026-12-31T00:00:00
```

## 7. Schema 信息（真相源）

### 7.1 `staging_log` JSON Lines 格式（[server/staging_log.py:25-78](file:///c:/git_work/callwarden/server/staging_log.py) / [rust_ext/src/daemon/staging_log.rs](file:///c:/git_work/callwarden/rust_ext/src/daemon/staging_log.rs)）

**重要**：`staging_log` 不是 SQLite 表，而是 JSON Lines 文件，每行一条 entry。

每条 entry 的 JSON 字段：
```json
{
  "lsn": 1,                      // 单调递增 LSN
  "timestamp": 1722000000.0,     // epoch seconds
  "workspace_id": "ws_abc",       // workspace ID（字符串）
  "file_path": "src/main.py",
  "content_hash": "sha256...",
  "language": "python",
  "parse_delta": {},
  "resolve_delta": {},
  "frontier": {},
  "metrics_update": {},
  "status": "pending",           // pending / applied / failed
  "error": null
}
```

`Replicator.get_pending_count(workspace_id)` 等价逻辑：
```python
pending = self.staging_log.read_pending()  # 过滤 status=="pending"
if workspace_id:
    return sum(1 for e in pending if e.workspace_id == workspace_id)
return len(pending)
```

### 7.2 `GraphStore` 内存结构（[rust_ext/src/graph.rs](file:///c:/git_work/callwarden/rust_ext/src/graph.rs)）

- CSR 邻接表（caller→callees + callee→callers 双向）
- 符号 HashMap（qualified_name → SymbolInfo）
- 拓扑排序 + 循环检测用 Tarjan SCC 算法

## 8. 验收标准

- [ ] `cargo check --manifest-path rust_ext/Cargo.toml` 通过
- [ ] `maturin build -i C:\Python314\python.exe --release` 生成 cp314 wheel
- [ ] `pip install` 后 `cw server --check-imports` 通过
- [ ] `pytest tests/test_phase1_behavioral_diff.py -v` 全部通过（含新增 5 个 Test*Diff 类）
- [ ] `pytest tests/test_phase5_replicator.py tests/test_phase4_snapshot*.py -v` 不破坏（现有回归测试通过）
- [ ] Phase 1-4 行升级为 `✅(behavioral)`

## 9. 风险与注意事项

1. **不切换默认路径**：Python `SnapshotManagerService.query_*` 仍主导。Rust API 仅作为可选短路。
2. **GraphStore 方法签名对齐**：Rust 端 `compute_call_chain_down` / `topological_sort` / `detect_cycles` / `get_stats` 已实现，但方法签名/返回类型可能与 Python 端 `query_*` 不完全一致。差分测试需做字段映射。
3. **空 GraphStore 行为**：snapshot 未加载（`current_store` 为 None）时，4 个 query_* 方法应返回空列表 / 空 dict，不抛错。
4. **max_depth 参数**：Python 端 `query_call_chain_down` 默认 max_depth=10，Rust 端需对齐默认值。
5. **循环检测返回格式**：Python 端返回 `list[list[str]]`，Rust 端 `detect_cycles` 可能返回 `Vec<Vec<String>>` 或 `Vec<Vec<&str>>`。差分测试需做类型转换。
6. **rollback_flag 切换语义**：当前 Rust API 直接暴露，未在 `snapshot_manager.py` 中接入。Phase 2 切换默认路径时需在 `snapshot_manager.py` 中读取 rollback_flag 决定走 Rust 还是 Python。
7. **backup/restore 不在范围**：本子任务不迁移 `BackupManager` / `RestoreManager`，Phase 4 处理。
8. **daemon 内部路径不变**：`daemon/replicator.rs::Replicator::replicate` / `recover` / `get_pending_count` 继续按现有逻辑运行，不受本子任务影响。
