# Phase 1 子任务 3 契约：workspace manifest、projection 与 refresh commit

> 本文件是全量 Rust 迁移自举计划 Phase 1 第三个功能子任务的 contract 交付物。
>
> 关联：
> - 总计划：[rust-full-migration-self-bootstrap-plan.md](rust-full-migration-self-bootstrap-plan.md) Phase 1 §3
> - 上游契约：[phase1-cas-contract.md](phase1-cas-contract.md)（CAS 只读查询）
> - 真相源：[db/db_workspace_manifest.py](../../db/db_workspace_manifest.py) / [rust_ext/src/daemon/cas_merge.rs](../../rust_ext/src/daemon/cas_merge.rs)

## 1. 范围

| 项 | 说明 |
|---|---|
| **目标** | 将 Rust 端 `workspace_manifests` 表的**只读查询方法**通过 PyO3 暴露给 Python，与 Python `db/db_workspace_manifest.py` 路径建立 ✅(behavioral) 差分 |
| **Rust 端实现** | 部分就绪：`rust_ext/src/daemon/cas_merge.rs` 中已有 `ensure_manifest_schema` + `upsert_manifest`（私有函数，未 PyO3 暴露）；本子任务补 `manifest_query.rs` 模块，新增 5 个只读查询方法 |
| **不在本子任务** | 任何写操作（`upsert_manifest` / `init_manifest_schema` / `link_to_snapshot`）仍走 Python（AGENTS.md 规则 6：写操作走 CLI/Python），refresh commit 主流程仍由 `server/replicator.py:daemon_handle_refresh` 主导 |

## 2. 现状盘点（来自调研）

### 2.1 Python `db_workspace_manifest.py` 公开方法（7 个）

| 方法 | 类型 | 说明 |
|---|---|---|
| `init_manifest_schema(conn)` | 写 | 创建 `workspace_manifests` + `workspace_snapshot_map` 两表及 3 索引 |
| `upsert_manifest(conn, workspace_id, rel_path, content_hash, cas_key, raw_hash, source_encoding, bom_kind, newline_style, file_size, mtime_ns, is_dirty)` | 写 | INSERT OR REPLACE manifest 行（12 字段） |
| `get_manifest(conn, workspace_id, rel_path)` | 只读 | 查询单个文件 manifest（返回 dict 或 None） |
| `list_manifests(conn, workspace_id, dirty_only=False)` | 只读 | 列出 workspace 所有 manifest（支持 dirty 过滤） |
| `link_to_snapshot(conn, snapshot_id, rel_path, content_hash, cas_key)` | 写 | INSERT OR REPLACE workspace_snapshot_map |
| `get_snapshot_files(conn, snapshot_id)` | 只读 | 查询 snapshot 的所有文件 |
| `verify_raw_hash(conn, workspace_id, rel_path, expected_raw_hash)` | 只读 | 校验磁盘文件 raw_hash 与 manifest 记录一致 |

### 2.2 Rust 端已有实现（`rust_ext/src/daemon/cas_merge.rs`，未暴露）

| 函数 | 可见性 | 类型 | 与 Python 对齐情况 |
|---|---|---|---|
| `ensure_manifest_schema(conn)` | 私有 `fn` | 写 | ⚠️ 缺 `workspace_snapshot_map` 表创建 |
| `upsert_manifest(conn, workspace_id, rel_path, content_hash, cas_key, file_size)` | 私有 `fn` | 写 | ⚠️ 缺 `raw_hash` / `source_encoding` / `bom_kind` / `newline_style` / `mtime_ns` / `is_dirty` 参数，使用硬编码默认值 |
| 5 个查询方法 | **未实现** | 只读 | 🔴 完全缺失 |

### 2.3 refresh commit 流程（`server/replicator.py:daemon_handle_refresh`）

```
1. session epoch 校验（拒绝 stale session）
2. CAS 第一阶段（seen）—— 原子更新 latest_seen_generation
3. daemon 侧解析 + CAS publish（canonical bytes → parse → CAS）
4. CAS 第二阶段（committed）—— 条件更新 latest_committed_generation
5. P0-1 整改：
   5a. CAS → CodeGraph DB merge（调 Python `db_cas_merge.merge_cas_to_codegraph`，
       内部触发 Rust `daemon::cas_merge::merge_cas_to_codegraph` 写主表 + 私有 upsert_manifest）
   5b. upsert workspace_manifests（调 Python `db_workspace_manifest.upsert_manifest`）
```

**关键观察**：refresh commit 写入 manifest 有两条路径：

1. **Rust daemon 路径**（私有）：`cas_merge.rs::upsert_manifest`（在 `merge_cas_to_codegraph` 内部调用，写 CodeGraph DB 中的 `workspace_manifests` 表，标记 `is_dirty=1`，硬编码默认值）
2. **Python replicator 路径**（公开）：`db_workspace_manifest.upsert_manifest`（在 `daemon_handle_refresh` 末尾调用，写 workspace DB 中的 `workspace_manifests` 表，接收完整 12 字段参数）

**双库分离**：

| 数据库 | 表 | 写入方 |
|---|---|---|
| CodeGraph DB（`~/.callwarden/callwarden.db`） | `workspace_manifests` | Rust `cas_merge::upsert_manifest`（merge 时写入，记录 CAS key 关联） |
| workspace DB（daemon 每 workspace 一个 cas.db） | `workspace_manifests` | Python `db_workspace_manifest.upsert_manifest`（refresh commit 时写入，记录完整 file scan 元数据） |

**本子任务不修改双库写入语义**，仅暴露 Rust 端只读查询方法。

## 3. API 契约（本子任务暴露清单）

### 3.1 只读查询方法（5 个，新建 `rust_ext/src/manifest_query.rs`）

```python
def manifest_get(db_path: str, workspace_id: int, rel_path: str) -> Optional[dict]:
    """查询单个文件 manifest

    与 Python db_workspace_manifest.get_manifest(conn, workspace_id, rel_path) 行为一致：
    - 行存在 → 返回 dict（含 12 字段）
    - 行不存在 → 返回 None

    返回 dict 字段（与 Python 端 SELECT * 顺序一致）：
    - workspace_id: int
    - rel_path: str
    - content_hash: str
    - cas_key: str (可空字符串)
    - raw_hash: str (可空字符串)
    - source_encoding: str
    - bom_kind: str
    - newline_style: str
    - file_size: int
    - mtime_ns: int
    - is_dirty: int (0/1)
    - updated_at: float
    """

def manifest_list(db_path: str, workspace_id: int, dirty_only: bool = False) -> list[dict]:
    """列出 workspace 的所有 manifest

    与 Python db_workspace_manifest.list_manifests(conn, workspace_id, dirty_only) 行为一致：
    - dirty_only=True → 只返回 is_dirty=1 的行
    - dirty_only=False → 返回所有行
    - 空表或无行 → 返回空列表 []
    """

def manifest_count(db_path: str, workspace_id: int, dirty_only: bool = False) -> int:
    """统计 workspace manifest 行数

    Python 端无直接对应方法，但行为等价于 len(list_manifests(...))。
    用于快速计数场景，避免序列化全表。
    - 表不存在 → 返回 0
    - dirty_only=True → COUNT WHERE is_dirty=1
    """

def snapshot_get_files(db_path: str, snapshot_id: str) -> list[dict]:
    """查询 snapshot 的所有文件

    与 Python db_workspace_manifest.get_snapshot_files(conn, snapshot_id) 行为一致：
    - snapshot 存在 → 返回 list[dict]（每 dict 含 snapshot_id / rel_path / content_hash / cas_key）
    - snapshot 不存在 → 返回空列表 []
    """

def manifest_verify_raw_hash(db_path: str, workspace_id: int, rel_path: str, expected_raw_hash: str) -> bool:
    """校验磁盘文件 raw_hash 与 manifest 记录一致

    与 Python db_workspace_manifest.verify_raw_hash(conn, workspace_id, rel_path, expected_raw_hash) 行为一致：
    - manifest 不存在 → 返回 False
    - manifest 存在但 raw_hash 不匹配 → 返回 False
    - manifest 存在且 raw_hash 匹配 → 返回 True
    """
```

### 3.2 不暴露的 API（仍走 Python）

| API | 原因 |
|---|---|
| `init_manifest_schema` | 写操作（CREATE TABLE + CREATE INDEX） |
| `upsert_manifest` | 写操作（INSERT OR REPLACE） |
| `link_to_snapshot` | 写操作（INSERT OR REPLACE workspace_snapshot_map） |
| `merge_cas_to_codegraph`（Rust 私有 upsert_manifest 调用方） | 写操作（写 CodeGraph DB 主表 + manifest） |

## 4. 行为契约（Python ↔ Rust 必须一致）

### 4.1 manifest_get（G1-G5）

| # | 场景 | Python `get_manifest` | Rust `manifest_get` | 差分断言 |
|---|---|---|---|---|
| G1 | 不存在的 (ws, rel_path) | 返回 None | 返回 None | `assert py == rust is None` |
| G2 | 存在的 manifest | 返回 dict（12 字段） | 返回 dict（12 字段） | `assert py == rust`（字段逐一比对） |
| G3 | 表不存在 | sqlite3.OperationalError | PyIOError | 两端都抛错 |
| G4 | workspace_id 为 0 | 返回 None（无行匹配） | 返回 None | `assert py == rust is None` |
| G5 | rel_path 为空字符串 | 返回 None（无行匹配） | 返回 None | `assert py == rust is None` |

### 4.2 manifest_list（L1-L5）

| # | 场景 | Python `list_manifests` | Rust `manifest_list` | 差分断言 |
|---|---|---|---|---|
| L1 | 空 workspace（无行） | 返回 [] | 返回 [] | `assert py == rust == []` |
| L2 | workspace 有 3 行 | 返回 [dict × 3] | 返回 [dict × 3] | `assert len(py) == len(rust) == 3` 且字段逐一比对 |
| L3 | dirty_only=True | 返回 is_dirty=1 的行 | 返回 is_dirty=1 的行 | `assert py == rust` |
| L4 | dirty_only=False | 返回所有行 | 返回所有行 | `assert py == rust` |
| L5 | 表不存在 | sqlite3.OperationalError | PyIOError | 两端都抛错 |

### 4.3 manifest_count（C1-C4）

| # | 场景 | Python 等价（`len(list_manifests(...))`） | Rust `manifest_count` | 差分断言 |
|---|---|---|---|---|
| C1 | 空 workspace | `len([]) == 0` | 返回 0 | `assert py == rust == 0` |
| C2 | 5 行 ready + 3 行 dirty | `len(list) == 8` | 返回 8 | `assert py == rust == 8` |
| C3 | dirty_only=True | `len(dirty) == 3` | 返回 3 | `assert py == rust == 3` |
| C4 | 表不存在 | sqlite3.OperationalError（需 Python 端 try/except 返回 0） | 返回 0 | 两端对齐（决策：表不存在时都返回 0） |

### 4.4 snapshot_get_files（S1-S3）

| # | 场景 | Python `get_snapshot_files` | Rust `snapshot_get_files` | 差分断言 |
|---|---|---|---|---|
| S1 | snapshot 不存在 | 返回 [] | 返回 [] | `assert py == rust == []` |
| S2 | snapshot 有 2 个文件 | 返回 [dict × 2] | 返回 [dict × 2] | `assert py == rust`（字段逐一比对） |
| S3 | 表不存在 | sqlite3.OperationalError | PyIOError | 两端都抛错 |

### 4.5 manifest_verify_raw_hash（V1-V4）

| # | 场景 | Python `verify_raw_hash` | Rust `manifest_verify_raw_hash` | 差分断言 |
|---|---|---|---|---|
| V1 | manifest 不存在 | 返回 False | 返回 False | `assert py == rust is False` |
| V2 | manifest 存在，raw_hash 匹配 | 返回 True | 返回 True | `assert py == rust is True` |
| V3 | manifest 存在，raw_hash 不匹配 | 返回 False | 返回 False | `assert py == rust is False` |
| V4 | manifest 存在，raw_hash 为空字符串（默认值）且 expected 也为空 | 返回 True（""  == ""） | 返回 True | `assert py == rust is True` |

## 5. 事务边界（AGENTS.md 规则 6）

### 5.1 只读策略

所有 `manifest_*` / `snapshot_*` 查询方法必须满足：

1. **连接级别只读**：`OpenFlags::SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI`（非 `immutable=1`，规避 AGENTS.md 规则 7 的 WAL 读旧数据陷阱）
2. **不激活 workspace**：不调用 `register_workspace` / `set_active_workspace`
3. **不持有写锁**：纯 SELECT，不进入写事务
4. **busy_timeout=5000**：与 Phase 1-1 / 1-2 一致
5. **WAL checkpoint(PASSIVE)**：查询前执行，确保 WAL 已 flush
6. **短连接**：每次调用新建 + 关闭，避免与 Python 长连接撞锁

### 5.2 不与 Python 写入冲突

- Rust 只读查询 ↔ Python 写入（`upsert_manifest` / `init_manifest_schema` / `link_to_snapshot`）：WAL 模式下并发安全
- Rust **不写入** manifest 表（upsert/init/link 仍由 Python 主导）
- daemon 内部的 Rust 写入路径（`cas_merge.rs::upsert_manifest` 私有）独立运行，不通过 PyO3 暴露，不影响本子任务

### 5.3 双库查询的语义说明

由于 manifest 在 CodeGraph DB 和 workspace DB 中都存在（§2.3），调用方需明确传 `db_path`：

| 调用场景 | db_path 应传 | 行来源 |
|---|---|---|
| 查询 refresh commit 写入的 manifest | workspace DB 路径 | Python `upsert_manifest` 写入 |
| 查询 CAS merge 写入的 manifest | CodeGraph DB 路径 | Rust `cas_merge::upsert_manifest` 私有写入 |

本子任务不在 API 层强制区分，由调用方决定查询哪个 DB。

## 6. 实现计划

### 6.1 Rust 端（`rust_ext/src/manifest_query.rs` 新文件）

```rust
//! Phase 1-3: workspace manifest 只读查询 API（PyO3 暴露层）
//!
//! 设计原则（见 docs/design/phase1-manifest-contract.md §5）：
//! - 只读连接（SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI）
//! - WAL checkpoint(PASSIVE) 后读取
//! - busy_timeout=5000
//! - 短连接，不复用
//! - 不暴露写操作（upsert_manifest / init_schema / link_to_snapshot 仍走 Python）

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use pyo3::types::PyDict;
use rusqlite::OpenFlags;

/// 打开只读连接（与 cas_query.rs / sqlite_query.rs 完全一致）
fn open_readonly(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    )
    .map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    Ok(conn)
}

// 5 个 #[pyfunction]：manifest_get / manifest_list / manifest_count /
// snapshot_get_files / manifest_verify_raw_hash

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(manifest_get, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_list, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_count, m)?)?;
    m.add_function(wrap_pyfunction!(snapshot_get_files, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_verify_raw_hash, m)?)?;
    Ok(())
}
```

### 6.2 PyO3 注册（`rust_ext/src/lib.rs`）

```rust
// Phase 1-3: workspace manifest 只读查询 API
mod manifest_query;
// ...
m.add_function(wrap_pyfunction!(manifest_query::manifest_get, m)?)?;
m.add_function(wrap_pyfunction!(manifest_query::manifest_list, m)?)?;
m.add_function(wrap_pyfunction!(manifest_query::manifest_count, m)?)?;
m.add_function(wrap_pyfunction!(manifest_query::snapshot_get_files, m)?)?;
m.add_function(wrap_pyfunction!(manifest_query::manifest_verify_raw_hash, m)?)?;
```

### 6.3 差分测试（`tests/test_phase1_behavioral_diff.py` 追加 `TestManifestQueryDiff`）

- `TestManifestGetDiff`：G1-G5（manifest_get 差分）
- `TestManifestListDiff`：L1-L5（manifest_list 差分）
- `TestManifestCountDiff`：C1-C4（manifest_count 差分）
- `TestSnapshotGetFilesDiff`：S1-S3（snapshot_get_files 差分）
- `TestManifestVerifyRawHashDiff`：V1-V4（verify_raw_hash 差分）

### 6.4 wire-production（`db/db_rollback_config.py` 登记）

通过 CLI 注册一条 rollback_config 记录：

```powershell
cw rollback register `
  --task-id T-1785163105003-0a59e5a6 `
  --feature rust_manifest_query `
  --phase 1 `
  --production-entry "rust_ext/src/manifest_query.rs" `
  --rollback-entry "db/db_workspace_manifest.py:get_manifest/list_manifests" `
  --window 2026-12-31T00:00:00
```

## 7. Schema 信息（真相源）

### 7.1 `workspace_manifests`（[db/db_workspace_manifest.py:13-29](file:///c:/git_work/callwarden/db/db_workspace_manifest.py)）

```sql
CREATE TABLE IF NOT EXISTS workspace_manifests (
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    cas_key TEXT,
    raw_hash TEXT,
    source_encoding TEXT DEFAULT 'utf-8',
    bom_kind TEXT DEFAULT 'none',
    newline_style TEXT DEFAULT 'lf',
    file_size INTEGER DEFAULT 0,
    mtime_ns INTEGER DEFAULT 0,
    is_dirty INTEGER DEFAULT 0,
    updated_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, rel_path)
);
```

### 7.2 `workspace_snapshot_map`（[db/db_workspace_manifest.py:31-38](file:///c:/git_work/callwarden/db/db_workspace_manifest.py)）

```sql
CREATE TABLE IF NOT EXISTS workspace_snapshot_map (
    snapshot_id TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    cas_key TEXT,
    PRIMARY KEY (snapshot_id, rel_path)
);
```

### 7.3 Rust 端 schema（`rust_ext/src/daemon/cas_merge.rs:705-727`）

⚠️ **已知差距**：Rust 端 `ensure_manifest_schema` 只创建 `workspace_manifests` 表，**不创建 `workspace_snapshot_map` 表**。本子任务不修复此差距（因 Rust 端不暴露 `link_to_snapshot` 写入路径，snapshot_map 表的实际写入仍由 Python 主导）。差分测试中 `snapshot_get_files` 测试用例若依赖 Rust 端 DB 文件，需通过 Python 端 `init_manifest_schema` 预初始化 schema。

## 8. 验收标准

- [ ] `cargo check --manifest-path rust_ext/Cargo.toml` 通过
- [ ] `maturin build -i C:\Python314\python.exe --release` 生成 cp314 wheel
- [ ] `pip install` 后 `cw server --check-imports` 通过
- [ ] `pytest tests/test_phase1_behavioral_diff.py -v` 全部通过（含新增 TestManifest* 差分）
- [ ] `pytest tests/test_phase3_cas.py -v` 不破坏（现有 TestWorkspaceManifest 7 个测试仍通过）
- [ ] manifest Phase 1 第三行 differential-test 升级为 `✅(behavioral)`

## 9. 风险与注意事项

1. **不切换默认路径**：Python `db_workspace_manifest.get_manifest` 等仍主导。Rust API 仅作为可选短路。
2. **表不存在的处理**：Python 端 `sqlite3.OperationalError` 需在调用方 try/except；Rust 端 `manifest_count` 和 `manifest_list` 在表不存在时返回 0 / []，`manifest_get` 抛 PyIOError。**决策**：差分测试中先用 Python `init_manifest_schema` 初始化表，避免依赖表不存在场景。
3. **字段类型对齐**：
   - `updated_at` 是 REAL（浮点），Rust 用 f64，Python 用 float
   - `is_dirty` 是 INTEGER（0/1），Rust 用 i64，Python 用 int
   - `mtime_ns` 是 INTEGER（纳秒），Rust 用 i64
4. **`workspace_snapshot_map` 表在 Rust 端不存在**：snapshot_get_files 的 Rust 路径要求 db_path 指向的 SQLite 文件已由 Python `init_manifest_schema` 初始化（含 snapshot_map 表）。差分测试 fixture 必须使用 Python 端初始化。
5. **WAL checkpoint 时序**：若 Python daemon 正在写 manifest，Rust 只读连接的 `PRAGMA wal_checkpoint(PASSIVE)` 可能读到 checkpoint 前状态。这与 Python 端 `sqlite3.connect` 行为一致，可接受。
6. **rollback_flag 切换语义**：当前 Rust API 直接暴露，未在 `db_workspace_manifest.py` 中接入。Phase 2 切换默认路径时需在 `db_workspace_manifest.py` 中读取 rollback_flag 决定走 Rust 还是 Python。
7. **不修改 refresh commit 流程**：本子任务只暴露查询 API，不修改 `server/replicator.py:daemon_handle_refresh` 中的写入路径。Rust daemon binary 内部使用的 `cas_merge::upsert_manifest`（私有）继续按现有逻辑运行，不受本子任务影响。
8. **PyO3 返回类型**：`manifest_get` 返回 `Option<Bound<PyAny>>`（None 或 dict），`manifest_list` 返回 `Vec<Bound<PyAny>>`（list[dict]），`manifest_count` 返回 `i64`，`snapshot_get_files` 返回 `Vec<Bound<PyAny>>`，`manifest_verify_raw_hash` 返回 `bool`。与 [cas_query.rs](../../rust_ext/src/cas_query.rs) 模式一致。
