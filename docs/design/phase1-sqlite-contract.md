# Phase 1 子任务 1 契约：Rust SQLite 连接、schema migration 与事务边界

> 本文件是全量 Rust 迁移自举计划 Phase 1 第一个功能子任务的 contract 交付物。
> 维护规则：完成 implement/differential-test/wire-production/verify/refresh/review 各步后，回填对应小节的状态。

## 1. 范围

| 项 | 说明 |
|---|---|
| **Rust 端实现** | 通过 `rusqlite`（已 bundled）打开 SQLite 数据库（只读模式），查询 `schema_version` 表的 `MAX(version)` |
| **PyO3 暴露** | `sqlite_query_schema_version(db_path: str) -> int` |
| **差分测试** | `tests/test_phase1_behavioral_diff.py::TestSqliteSchemaQueryDiff` 从 xfail 转为正向断言 |
| **生产接入** | 仅注册 PyO3 API，**不并行写入**；Python 写入路径不变（AGENTS.md 规则 6） |
| **不在本子任务** | schema migration（`_migrate_schema`）、表创建、索引管理、workspace 激活 |

## 2. API 契约

```python
def sqlite_query_schema_version(db_path: str) -> int:
    """查询 SQLite 数据库的 schema_version

    Args:
        db_path: SQLite 数据库文件绝对路径

    Returns:
        schema_version 表中的 MAX(version)；
        若表不存在或为空，返回 0（与 Python 端 _get_current_version 一致）

    Raises:
        PyIOError: 数据库文件无法打开（路径不存在 / 权限不足 / 文件损坏）
        PyValueError: db_path 为空字符串
    """
```

## 3. 行为契约（Python ↔ Rust 必须一致）

| # | 场景 | Python `sqlite3` 行为 | Rust `rusqlite` 行为 | 差分断言 |
|---|---|---|---|---|
| B1 | 空数据库（无表） | `_get_current_version` 在 except 中返回 0 | 查询失败 → 返回 0 | `assert py == rust == 0` |
| B2 | 有 schema_version 表但无记录 | `SELECT MAX(version)` 返回 NULL → 0 | `query_row::<Option<i64>>` 返回 None → 0 | `assert py == rust == 0` |
| B3 | 单条记录 v=42 | 返回 42 | 返回 42 | `assert py == rust == 42` |
| B4 | 多条记录 (v=40, v=42) | `MAX(version)` 返回 42 | `MAX(version)` 返回 42 | `assert py == rust == 42` |
| B5 | WAL 模式数据库 | 正常读取（自动 checkpoint 后可见） | 用 `PRAGMA wal_checkpoint(PASSIVE)` 后读取 | `assert py == rust` |
| B6 | 不存在的 db_path | `sqlite3.OperationalError` | `PyIOError` | 差分允许两端都抛错（异常类型不要求一致，但应都失败）|

## 4. 事务边界（AGENTS.md 规则 6）

### 4.1 只读策略

`sqlite_query_schema_version` 必须满足以下只读约束：

1. **连接级别只读**：`PRAGMA query_only=1`，禁止任何写操作
2. **不激活 workspace**：不调用 `register_workspace` / `set_active_workspace`（避免与 MCP Server 写锁冲突）
3. **不持有写锁**：纯 SELECT，不进入写事务
4. **busy_timeout=5000**：与 AGENTS.md 规则 6 一致，5 秒后超时返回错误（不无限等待）

### 4.2 WAL 模式兼容

Python 端 `CodeGraphDB` 用 `PRAGMA journal_mode=WAL` 写入。Rust 只读连接需：

1. **打开方式**：`OpenFlags::SQLITE_OPEN_READ_ONLY | SQLITE_OPEN_URI`（不 SQLITE_OPEN_READWRITE）
   - 注意：`immutable=1` URI 会跳过 WAL，导致读到旧数据（AGENTS.md 规则 7 已记录此坑）
   - 因此**不用** `immutable=1`，用 `read_only=1`
2. **读取前 checkpoint**：`PRAGMA wal_checkpoint(PASSIVE)` 确保 WAL 已 flush 到主库
3. **不复用连接**：每次调用新建 + 关闭（短连接，避免与 Python 长连接撞锁）

### 4.3 不与 Python 写入冲突

- Rust 只读查询 ↔ Python 写入：WAL 模式下并发安全（一个写 + 多个读）
- Rust **不写入** schema_version 表（migration 仍由 Python 主导）
- daemon 启动时若需 schema 校验，应通过 RPC 调用 Python（Phase 4 任务）

## 5. Schema 信息（真相源）

`schema_version` 表定义（[db/db_base.py:2443](file:///c:/git_work/callwarden/db/db_base.py#L2443)）：

```sql
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY,
    applied_at REAL NOT NULL,
    description TEXT DEFAULT ''
)
```

Python 端查询（[db/db_base.py:2554](file:///c:/git_work/callwarden/db/db_base.py#L2554)）：

```python
"SELECT MAX(version) as v FROM schema_version"
# row["v"] 为 NULL 时返回 0
```

Rust 端查询应等价：

```rust
let v: Option<i64> = conn.query_row(
    "SELECT MAX(version) FROM schema_version",
    [],
    |row| row.get(0),
).ok().flatten();
v.unwrap_or(0)
```

## 6. 错误处理

| 错误码 | 触发场景 | 返回策略 |
|---|---|---|
| `PARSE_OK`（隐喻） | 正常查询成功 | 返回 int |
| `DB_LOCKED` | busy_timeout 超时 | 抛 `PyIOError("database is locked")`（与 AGENTS.md 错误码 12 一致） |
| `PARSE_FAILED`（隐喻） | db_path 不存在 / 损坏 | 抛 `PyIOError`，含原始错误信息 |
| `PARSE_UNSUPPORTED`（隐喻） | schema_version 表不存在 | 返回 0（与 Python 一致，不抛错） |

## 7. 差分测试矩阵（TestSqliteSchemaQueryDiff）

差分测试从 xfail 转为正向断言：

```python
def test_schema_version_empty_db(self, tmp_path):
    """B1: 空数据库 → 两端都返回 0"""
    db_path = tmp_path / "empty.db"
    py_version = sqlite3.connect(str(db_path)).execute(
        "SELECT MAX(version) FROM schema_version"
    ).fetchone()[0] or 0
    rust_version = callwarden_core.sqlite_query_schema_version(str(db_path))
    assert py_version == rust_version == 0

def test_schema_version_v42(self, tmp_path):
    """B3: 单条 v=42 记录 → 两端都返回 42"""
    # ...建表+插入...
    assert py_version == rust_version == 42

def test_schema_version_multi_records(self, tmp_path):
    """B4: 多条记录 → 两端都返回 MAX"""
    # ...插入 v=40, v=42...
    assert py_version == rust_version == 42

def test_schema_version_wal_mode(self, tmp_path):
    """B5: WAL 模式数据库 → 两端一致"""
    # ...PRAGMA journal_mode=WAL; INSERT...
    assert py_version == rust_version
```

旧契约骨架测试 `test_rust_sqlite_api_not_yet_exposed` 应被删除或改为反向断言（API 已暴露）。

## 8. 实现计划

### 8.1 Rust 端（`rust_ext/src/sqlite_query.rs` 新文件）

```rust
//! Phase 1-1: SQLite 只读查询 API
//!
//! 设计原则：
//! - 只读连接（read_only=1，非 immutable=1）
//! - 不激活 workspace，不持有写锁
//! - WAL checkpoint 后读取，确保数据一致

use rusqlite::OpenFlags;
use pyo3::prelude::*;
use pyo3::exceptions::{PyIOError, PyValueError};

/// 查询 SQLite 数据库的 schema_version
#[pyfunction]
pub fn sqlite_query_schema_version(db_path: &str) -> PyResult<i64> {
    if db_path.is_empty() {
        return Err(PyValueError::new_err("db_path 不能为空"));
    }

    let conn = rusqlite::Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_URI,
    ).map_err(|e| PyIOError::new_err(format!("打开数据库失败: {}", e)))?;

    // busy_timeout=5000（与 Python 端一致）
    conn.busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| PyIOError::new_err(format!("设置 busy_timeout 失败: {}", e)))?;

    // WAL checkpoint：确保 WAL 已 flush（AGENTS.md 规则 7）
    let _ = conn.pragma_query(None, "wal_checkpoint(PASSIVE)");

    // 查询 MAX(version)，表不存在或空返回 0
    let v: Option<i64> = conn
        .query_row(
            "SELECT MAX(version) FROM schema_version",
            [],
            |row| row.get(0),
        )
        .ok()
        .flatten();
    Ok(v.unwrap_or(0))
}
```

### 8.2 PyO3 注册（`rust_ext/src/lib.rs`）

在 `register_functions` 中添加：

```rust
m.add_function(&wrap_pyfunction!(sqlite_query::sqlite_query_schema_version, m)?)?;
```

并 `mod sqlite_query;`。

### 8.3 差分测试改造（`tests/test_phase1_behavioral_diff.py`）

`TestSqliteSchemaQueryDiff` 类：
- 删除 `test_rust_sqlite_api_not_yet_exposed`（已实现，反向断言无意义）
- `test_schema_version_diff_skeleton` 改名为 `test_schema_version_v42`，启用 Rust 路径正向断言
- 新增 `test_schema_version_empty_db` / `test_schema_version_multi_records` / `test_schema_version_wal_mode`

## 9. 验收标准

- [ ] `cargo check` 通过（仅 warnings）
- [ ] `maturin build -i C:\Python314\python.exe` 生成 cp314 wheel
- [ ] `pip install` 后 `cw server --check-imports` 通过
- [ ] `pytest tests/test_phase1_behavioral_diff.py -v` 全部通过（含 TestSqliteSchemaQueryDiff 4 个新 case）
- [ ] `pytest tests/test_p0_4_rollback_config.py` 不破坏（27 passed）
- [ ] manifest Phase 1 第一行 differential-test 升级为 `✅(behavioral)`

## 10. 风险与注意事项

1. **rusqlite bundled vs 系统 SQLite**：rusqlite 用 bundled feature，与 Python sqlite3 可能是不同 SQLite 版本。差分测试需验证两端查询结果一致（理论上 SQL 标准函数 `MAX` 行为相同）。
2. **WAL checkpoint 时机**：`PRAGMA wal_checkpoint(PASSIVE)` 不阻塞写连接，但若 MCP Server 正在写，可能读到旧数据。这与 Python 端 `sqlite3.connect` 行为一致（Python 端不主动 checkpoint），可接受。
3. **Windows 文件锁**：Windows 上 SQLite 文件锁与 Unix 不同，只读连接仍可能被写连接阻塞。busy_timeout=5000 应能覆盖。
4. **schema_version 表不存在的差异**：Python 端 `_get_current_version` 在 except 中返回 0；Rust 端 `query_row` 失败也返回 0。但 Rust 端需明确区分"表不存在"（返回 0）和"查询失败"（抛错），避免误报。
5. **不实现 schema migration**：本子任务只读查询，不写入。Phase 1 后续子任务（如 ManifestService）才考虑 Rust 端 schema migration。
