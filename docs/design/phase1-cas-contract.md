# Phase 1 子任务 2 契约：Global CAS、Local CAS 与 pending refs

> 本文件是全量 Rust 迁移自举计划 Phase 1 第二个功能子任务的 contract 交付物。

## 1. 范围

| 项 | 说明 |
|---|---|
| **目标** | 将 Rust `CasStore` 的**只读查询方法**和 `compute_cas_key_v1` 纯函数通过 PyO3 暴露给 Python，与 Python `db/db_cas.py` 路径建立 ✅(behavioral) 差分 |
| **Rust 端实现** | 已就绪（`rust_ext/src/daemon/cas.rs`，2280 行，schema 与 Python 100% 对齐），本子任务只补 PyO3 暴露层 |
| **不在本子任务** | 任何写操作（`publish` / `pin` / `gc` / `file_generation_*` / `merge_cas_to_codegraph`）仍走 Python（AGENTS.md 规则 6：写操作走 CLI/Python） |

## 2. 现状盘点（来自调研）

### 2.1 Python CAS 公开方法

| 方法 | 类型 | 说明 |
|---|---|---|
| `compute_cas_key_v1(...)` | 纯函数 | SHA-256 CAS key 计算 |
| `cas_lookup(conn, cas_key)` | 只读 | 只命中 `state='ready'` |
| `cas_publish(...)` | 写 | 四阶段原子发布（building → ready） |
| `cas_publish_with_retry(...)` | 写 | retry + pin 包装层 |
| `cas_pin(conn, cas_key, ws_id, ttl=3600)` | 写 | 添加 pending ref（GC 保护窗口） |
| `cas_gc(conn, live_keys, grace=7)` | 写 | mark-sweep GC |
| `file_generation_seen(...)` | 写 | 第一阶段 seen |
| `file_generation_committed(...)` | 写 | 第二阶段 committed |
| `compute_symbol_content_hash(content)` | 纯函数 | SHA-256 符号正文去重 key |

### 2.2 Rust CasStore 已实现但未暴露的方法

全部 14 个 public 方法已实现，均无 `#[pyclass]` 或 `#[pyfunction]` 包装：

- `CasStore::open(db_path)` / `open_in_memory()`
- `lookup(cas_key) -> Option<CasFileCacheRow>` — 只读
- `publish(...)` / `publish_with_status(..., final_state)` — 写
- `pin(cas_key, ws_id, ttl)` — 写
- `gc(live_keys, grace)` / `gc_unreferenced(grace_days)` — 写
- `file_generation_seen/committed/uncommit(...)` — 写
- `get_file_generation(ws_id, rel_path) -> Option<FileGenerationRow>` — 只读
- `count_cas_files() -> i64` — 只读
- `get_cas_state(cas_key) -> Option<String>` — 只读

模块级函数：`compute_cas_key_v1(...)` / `compute_symbol_content_hash(content)` — 纯函数

### 2.3 Local CAS vs Global CAS（关键澄清）

调研结论：**Python 代码无 Local/Global CAS 显式区分**，但语义上：

| 层级 | 表 | workspace 隔离 | 共享性 |
|---|---|---|---|
| **内容层**（Global） | `cas_file_cache` / `cas_symbols` / `cas_raw_calls` / `cas_imports` / `cas_symbol_contents` | **不带 `workspace_id`** | 跨 workspace 全局共享（相同内容 = 相同 cas_key） |
| **引用层**（Local） | `cas_pending_refs` / `file_generations` | **带 `workspace_id`** | 按 workspace 独立维护 |
| **数据库文件** | — | daemon 侧每 workspace 一个 `cas.db` 物理文件 | — |

本子任务将这两个概念在 API 命名中显式化：`cas_global_*` 操作内容层，`cas_local_*` 操作引用层（只读查询）。

## 3. API 契约（本子任务暴露清单）

### 3.1 纯函数（无副作用，可直接暴露）

```python
def compute_cas_key_v1(
    content_hash: str,
    language: str,
    parser_version: str,
    callwarden_version: str,
    extraction_config_version: str,
    abi_version: str,
    input_abi_version: str,
) -> str:
    """计算 CAS key（SHA-256 hex）

    输入字段拼接顺序与 Python db_cas.compute_cas_key_v1 完全一致：
    content_hash|language|parser_version|callwarden_version|extraction_config_version|abi_version|input_abi_version
    """

def compute_symbol_content_hash(content: str) -> str:
    """计算符号正文 content_hash（SHA-256 hex）"""
```

### 3.2 只读查询（连接级只读 + WAL checkpoint）

```python
def cas_global_lookup(db_path: str, cas_key: str) -> Optional[dict]:
    """查询 cas_file_cache 表，只命中 state='ready'

    与 Python db_cas.cas_lookup(conn, cas_key) 行为一致：
    - state='ready' → 返回 dict（含 12 个字段）
    - state='building' / 'partial' / 不存在 → 返回 None
    """

def cas_global_get_state(db_path: str, cas_key: str) -> Optional[str]:
    """查询 cas_file_cache.state，不经过 state 过滤

    与 Python 路径行为对齐：
    - 行存在 → 返回 state 字符串（'ready' / 'building' / 'partial'）
    - 行不存在 → 返回 None
    """

def cas_global_count_files(db_path: str) -> int:
    """统计 cas_file_cache 行数（含所有 state）"""

def cas_local_get_file_generation(
    db_path: str, workspace_id: int, rel_path: str
) -> Optional[dict]:
    """查询 file_generations 表（Local 引用层）

    与 Python db_cas.file_generation_*（内部使用的查询逻辑）行为一致：
    - 行存在 → 返回 dict（含 latest_seen_generation / latest_committed_generation）
    - 不存在 → 返回 None
    """
```

### 3.3 不暴露的 API（仍走 Python）

| API | 原因 |
|---|---|
| `cas_publish` / `cas_publish_with_retry` / `publish_with_status` | 写操作（INSERT/UPDATE cas_file_cache 及子表） |
| `cas_pin` | 写操作（INSERT OR REPLACE cas_pending_refs） |
| `cas_gc` / `gc_unreferenced` | 写操作（DELETE + 级联清理） |
| `file_generation_seen` / `committed` / `uncommit` | 写操作（INSERT/UPDATE file_generations） |
| `merge_cas_to_codegraph` | 写操作（写 CodeGraph DB 主表） |

## 4. 行为契约（Python ↔ Rust 必须一致）

### 4.1 compute_cas_key_v1（B1-B4）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| B1 | 同输入 | 返回 64 字符 SHA-256 hex | 返回 64 字符 SHA-256 hex | `assert py == rust` |
| B2 | 不同 content_hash | 返回不同 key | 返回不同 key | `assert py == rust` |
| B3 | 不同 language | 返回不同 key | 返回不同 key | `assert py == rust` |
| B4 | 空字符串输入 | 不抛错，返回哈希 | 不抛错，返回哈希 | `assert py == rust` |

### 4.2 cas_global_lookup（C1-C5）

| # | 场景 | Python `cas_lookup` | Rust `cas_global_lookup` | 差分断言 |
|---|---|---|---|---|
| C1 | 不存在的 cas_key | 返回 None | 返回 None | `assert py == rust is None` |
| C2 | state='ready' | 返回 dict（12 字段） | 返回 dict（12 字段） | `assert py == rust`（字段逐一比对） |
| C3 | state='building' | 返回 None | 返回 None | `assert py == rust is None` |
| C4 | state='partial' | 返回 None（Python 无此状态但若手动改 state 仍过滤） | 返回 None | `assert py == rust is None` |
| C5 | 不存在的 db_path | sqlite3.OperationalError | PyIOError | 两端都抛错 |

### 4.3 cas_global_get_state（D1-D3）

| # | 场景 | Python 路径 | Rust 路径 | 差分断言 |
|---|---|---|---|---|
| D1 | state='ready' | 返回 'ready' | 返回 'ready' | `assert py == rust == 'ready'` |
| D2 | state='building' | 返回 'building' | 返回 'building' | `assert py == rust == 'building'` |
| D3 | 不存在 | 返回 None | 返回 None | `assert py == rust is None` |

### 4.4 cas_global_count_files（E1-E2）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| E1 | 空数据库（无表） | sqlite3.OperationalError | 返回 0 或抛 PyIOError | 两端对齐（待定） |
| E2 | 有表 + N 行 ready + M 行 building | COUNT(*) = N+M | COUNT(*) = N+M | `assert py == rust` |

### 4.5 cas_local_get_file_generation（F1-F2）

| # | 场景 | Python 路径 | Rust 路径 | 差分断言 |
|---|---|---|---|---|
| F1 | 不存在（ws+rel_path 未 seen） | 返回 None | 返回 None | `assert py == rust is None` |
| F2 | 已 seen + 已 committed | 返回 dict（含 latest_seen/committed_generation） | 返回 dict | `assert py == rust`（字段逐一比对） |

## 5. 事务边界（AGENTS.md 规则 6）

### 5.1 只读策略

所有 `cas_global_*` / `cas_local_*` 查询方法必须满足：

1. **连接级别只读**：`OpenFlags::SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI`（非 `immutable=1`，规避 AGENTS.md 规则 7 的 WAL 读旧数据陷阱）
2. **不激活 workspace**：不调用 `register_workspace` / `set_active_workspace`
3. **不持有写锁**：纯 SELECT，不进入写事务
4. **busy_timeout=5000**：与 Phase 1-1 一致
5. **WAL checkpoint(PASSIVE)**：查询前执行，确保 WAL 已 flush
6. **短连接**：每次调用新建 + 关闭，避免与 Python 长连接撞锁

### 5.2 纯函数策略

`compute_cas_key_v1` 和 `compute_symbol_content_hash` 不访问数据库，无副作用，可直接调用。

### 5.3 不与 Python 写入冲突

- Rust 只读查询 ↔ Python 写入（`cas_publish` / `cas_pin` / `cas_gc`）：WAL 模式下并发安全
- Rust **不写入** CAS 表（publish/pin/gc/file_generation 仍由 Python 主导）
- daemon 启动时若需 CAS 校验，应通过 RPC 调用 Python（Phase 4 任务）

## 6. 实现计划

### 6.1 Rust 端（`rust_ext/src/cas_query.rs` 新文件）

```rust
//! Phase 1-2: CAS 只读查询 API（PyO3 暴露层）
//!
//! 设计原则（见 docs/design/phase1-cas-contract.md §5）：
//! - 只读连接（SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI）
//! - WAL checkpoint(PASSIVE) 后读取
//! - busy_timeout=5000
//! - 短连接，不复用
//! - 纯函数（compute_cas_key_v1 / compute_symbol_content_hash）不访问数据库

use pyo3::exceptions::PyIOError;
use pyo3::prelude::*;
use rusqlite::OpenFlags;

/// 计算 CAS key（SHA-256 hex，与 Python compute_cas_key_v1 完全一致）
#[pyfunction]
pub fn compute_cas_key_v1(
    content_hash: &str,
    language: &str,
    parser_version: &str,
    callwarden_version: &str,
    extraction_config_version: &str,
    abi_version: &str,
    input_abi_version: &str,
) -> String {
    crate::daemon::cas::compute_cas_key_v1(
        content_hash, language, parser_version,
        callwarden_version, extraction_config_version,
        abi_version, input_abi_version,
    )
}

/// 计算符号正文 content_hash（SHA-256 hex）
#[pyfunction]
pub fn compute_symbol_content_hash(content: &str) -> String {
    crate::daemon::cas::compute_symbol_content_hash(content)
}

/// 打开只读连接的辅助函数（内部）
fn open_readonly(db_path: &str) -> PyResult<rusqlite::Connection> {
    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    ).map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    Ok(conn)
}

/// 查询 cas_file_cache（只命中 state='ready'）
#[pyfunction]
pub fn cas_global_lookup(db_path: &str, cas_key: &str) -> PyResult<Option<PyObject>> {
    let conn = open_readonly(db_path)?;
    // SELECT 全部 12 个字段 WHERE cas_key=? AND state='ready'
    // 失败/无行 → None
    // 成功 → 构建 dict 返回
    // ...
}

#[pyfunction]
pub fn cas_global_get_state(db_path: &str, cas_key: &str) -> PyResult<Option<String>> {
    // SELECT state FROM cas_file_cache WHERE cas_key=?
}

#[pyfunction]
pub fn cas_global_count_files(db_path: &str) -> PyResult<i64> {
    // SELECT COUNT(*) FROM cas_file_cache
}

#[pyfunction]
pub fn cas_local_get_file_generation(
    db_path: &str, workspace_id: i64, rel_path: &str,
) -> PyResult<Option<PyObject>> {
    // SELECT * FROM file_generations WHERE workspace_id=? AND rel_path=?
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(compute_cas_key_v1, m)?)?;
    m.add_function(wrap_pyfunction!(compute_symbol_content_hash, m)?)?;
    m.add_function(wrap_pyfunction!(cas_global_lookup, m)?)?;
    m.add_function(wrap_pyfunction!(cas_global_get_state, m)?)?;
    m.add_function(wrap_pyfunction!(cas_global_count_files, m)?)?;
    m.add_function(wrap_pyfunction!(cas_local_get_file_generation, m)?)?;
    Ok(())
}
```

### 6.2 PyO3 注册（`rust_ext/src/lib.rs`）

```rust
mod cas_query;
// ...
m.add_function(wrap_pyfunction!(cas_query::compute_cas_key_v1, m)?)?;
m.add_function(wrap_pyfunction!(cas_query::compute_symbol_content_hash, m)?)?;
m.add_function(wrap_pyfunction!(cas_query::cas_global_lookup, m)?)?;
m.add_function(wrap_pyfunction!(cas_query::cas_global_get_state, m)?)?;
m.add_function(wrap_pyfunction!(cas_query::cas_global_count_files, m)?)?;
m.add_function(wrap_pyfunction!(cas_query::cas_local_get_file_generation, m)?)?;
```

### 6.3 差分测试（`tests/test_phase1_behavioral_diff.py` 追加 `TestCasQueryDiff`）

- `TestComputeCasKeyDiff`：B1-B4（纯函数差分）
- `TestCasGlobalLookupDiff`：C1-C5（lookup 差分）
- `TestCasGlobalGetStateDiff`：D1-D3（state 差分）
- `TestCasGlobalCountFilesDiff`：E1-E2（count 差分）
- `TestCasLocalGetFileGenerationDiff`：F1-F2（file_generations 差分）

## 7. Schema 信息（真相源）

### 7.1 `cas_file_cache`（[db/db_cas.py:71-86](file:///c:/git_work/callwarden/db/db_cas.py)）

```sql
CREATE TABLE IF NOT EXISTS cas_file_cache (
    cas_key TEXT PRIMARY KEY,
    content_hash TEXT NOT NULL,
    language TEXT NOT NULL,
    file_size INTEGER DEFAULT 0,
    total_lines INTEGER DEFAULT 0,
    parser_version TEXT NOT NULL,
    callwarden_version TEXT NOT NULL,
    extraction_config_version TEXT NOT NULL,
    abi_version TEXT NOT NULL,
    input_abi_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'ready',
    parsed_at REAL NOT NULL
);
```

### 7.2 `cas_pending_refs`（GC 保护窗口）

```sql
CREATE TABLE IF NOT EXISTS cas_pending_refs (
    cas_key TEXT NOT NULL,
    workspace_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (cas_key, workspace_id)
);
```

### 7.3 `file_generations`（两阶段 CAS）

```sql
CREATE TABLE IF NOT EXISTS file_generations (
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    latest_seen_generation TEXT,
    latest_committed_generation TEXT,
    session_id TEXT,
    last_updated_at REAL NOT NULL,
    PRIMARY KEY (workspace_id, rel_path)
);
```

## 8. 验收标准

- [ ] `cargo check --manifest-path rust_ext/Cargo.toml` 通过
- [ ] `maturin build -i C:\Python314\python.exe --release` 生成 cp314 wheel
- [ ] `pip install` 后 `cw server --check-imports` 通过
- [ ] `pytest tests/test_phase1_behavioral_diff.py -v` 全部通过（含新增 TestCas* 差分）
- [ ] `pytest tests/test_phase3_cas.py tests/test_phase3_cas_protocol.py` 不破坏
- [ ] manifest Phase 1 第二行 differential-test 升级为 `✅(behavioral)`

## 9. 风险与注意事项

1. **不切换默认路径**：Python `db_cas.cas_lookup` 仍主导。Rust API 仅作为可选短路。
2. **`cas_global_count_files` 在空数据库的行为**：Python sqlite3 会抛 OperationalError（表不存在），Rust 端需明确返回 0 或抛 PyIOError，与 Python 行为对齐。**决策：表不存在时两端都返回 0**（与 Phase 1-1 `sqlite_query_schema_version` 行为一致），需要在 Python 路径中也补 try/except 返回 0。
3. **字段类型对齐**：`cas_file_cache.parsed_at` 是 REAL（浮点），Rust 端用 f64，Python 用 float。`file_size` 是 INTEGER，两端用 i64。
4. **`cas_pending_refs` 不暴露查询 API**：Python 端无 `cas_list_pending_refs` 函数，本子任务也不暴露，保持对齐。pending_refs 表由 `cas_pin`（写）和 `cas_gc`（读+清理）使用，均不走 Rust PyO3。
5. **WAL checkpoint 时序**：若 Python daemon 正在写 cas.db，Rust 只读连接的 `PRAGMA wal_checkpoint(PASSIVE)` 可能读到 checkpoint 前状态。这与 Python 端 `sqlite3.connect` 行为一致，可接受。
6. **daemon binary 已使用 CasStore**：Rust daemon 内部已用 `CasStore::open`（READWRITE）操作 cas.db，本子任务的 PyO3 暴露层只用 READ_ONLY 连接，不影响 daemon 行为。
7. **rollback_flag 切换语义**：当前 Rust API 直接暴露，未在 db_cas.py 中接入。Phase 2 切换默认路径时需在 db_cas.py 中读取 rollback_flag 决定走 Rust 还是 Python。
