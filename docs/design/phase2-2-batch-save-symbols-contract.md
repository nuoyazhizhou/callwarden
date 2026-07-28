# Phase 2 子任务 2 契约：批量 symbols 写入 PyO3 暴露与差分

> 本文件是全量 Rust 迁移自举计划 Phase 2 第二个功能子任务的 contract 交付物。
>
> 关联：
> - 总计划：[rust-full-migration-self-bootstrap-plan.md](rust-full-migration-self-bootstrap-plan.md) Phase 2 §1（拆分自原"批量文件注册、ParseFact 与 symbol 写入"）
> - 上游契约：[phase2-1-cas-merge-py暴露-contract.md](phase2-1-cas-merge-py暴露-contract.md)（CAS→CodeGraph Merge PyO3 暴露）
> - 真相源：[db/db_build.py:2961-3061](../../db/db_build.py) `_save_symbols_for_version`

## 1. 范围

| 项 | 说明 |
|---|---|
| **目标** | 将 Python `db_build.py:_save_symbols_for_version` 的批量化写入通过 PyO3 暴露给 Rust，建立 ✅(behavioral) 差分。让 `cw refresh` / `cw --refresh-all` 可选短路走 Rust 批量写入。 |
| **背景** | Phase 2-1 已暴露 `cas_merge_to_codegraph`（daemon 单文件逐条路径）。本子任务处理 CLI 批量化路径 `_save_symbols_for_version`，与 daemon 路径**写入语义不同**（批量化 + file_symbol_versions 历史版本）。 |
| **不在本子任务** | 1) `_build_call_graph_multi_lang` 的 5 策略 in-memory resolve（Phase 2-3）<br>2) `_save_file_version` 的 file_versions 历史版本写入（Phase 2-4）<br>3) `_build_call_graph_multi_lang` 的批量 calls 写入（Phase 2-5，依赖 2-3 resolve）<br>4) FTS5 触发器 DROP/REBUILD（保持 Python 端管理）<br>5) 二级索引 DROP/CREATE（保持 Python 端管理）<br>6) `_invalidate_qname_cache` / `_invalidate_graph_store`（保持 Python 端调用） |

## 2. 现状盘点（来自调研）

### 2.1 Python `_save_symbols_for_version` 入口

**位置**：[db/db_build.py:2961-3061](file:///c:/git_work/callwarden/db/db_build.py)

**调用路径**：
```
cw refresh <path> / cw --refresh-all
  └─ CodeGraphDB.refresh_file / build_full_graph
       └─ _build_multi_lang (db_build.py:1154)
            ├─ _save_file_version (step 3.5, db_build.py:1602-1609)
            ├─ _save_symbols_for_version (step 3.5, db_build.py:1602-1609)  ← 本子任务
            └─ _build_call_graph_multi_lang (step 4.5, db_build.py:1642)  ← Phase 2-3/2-5
```

### 2.2 写入语义（批量化）

| 行号 | SQL | 批量方式 |
|---|---|---|
| 2990 | `INSERT OR IGNORE INTO symbol_contents` | `executemany` |
| 3004 | `UPDATE symbol_contents SET has_comment=1, comment_content=?` | `executemany`（仅 has_comment 符号） |
| 3014 | `DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id=?)` | `execute`（单条） |
| 3018 | `DELETE FROM symbols WHERE file_instance_id=?` | `execute`（单条） |
| 3034 | `INSERT INTO symbols ... ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET ...` | `executemany` |
| 3055 | `INSERT INTO file_symbol_versions` | `executemany` |

**事务边界**：在外层 `_build_multi_lang` 的单一大事务中执行（db_build.py:1676 commit），本方法无显式 BEGIN/COMMIT。

### 2.3 涉及表

| 表 | 操作 | 行号 |
|---|---|---|
| `symbol_contents` | INSERT OR IGNORE + UPDATE（comment_content 补丁） | 2990, 3004 |
| `calls` | DELETE（清理旧 file_instance 的 calls） | 3014 |
| `symbols` | DELETE + executemany INSERT（ON CONFLICT DO UPDATE） | 3018, 3034 |
| `file_symbol_versions` | executemany INSERT | 3055 |

### 2.4 调用方

| 调用方 | 路径 | 替换策略 |
|---|---|---|
| `db_build.py:_build_multi_lang` step 3.5 | [db_build.py:1602-1609](file:///c:/git_work/callwarden/db/db_build.py) | 调用 Python `_save_symbols_for_version`，建议改为可选走 Rust |

## 3. API 契约

### 3.1 批量 symbols 写入（新建 `rust_ext/src/batch_build_query.rs`）

```python
def batch_save_symbols(
    codegraph_db_path: str,
    workspace_id: int,
    file_instance_id: int,
    file_version_id: int,
    symbols: list[dict],
) -> dict:
    """批量写入 symbols + symbol_contents + file_symbol_versions

    与 Python `db_build.py:_save_symbols_for_version` 行为一致：
    - INSERT OR IGNORE INTO symbol_contents（按 content_hash 去重）
    - UPDATE symbol_contents SET has_comment=1, comment_content=? （仅 has_comment 符号）
    - DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id=?)
    - DELETE FROM symbols WHERE file_instance_id=?
    - INSERT INTO symbols ... ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET ...
    - INSERT INTO file_symbol_versions

    事务边界：BEGIN IMMEDIATE → 全部 SQL → COMMIT
    失败：返回 dict {"success": False, "error": str(e)}，不抛异常

    Args:
        codegraph_db_path: CodeGraph DB 路径
        workspace_id: workspace_id（用于校验，不写入）
        file_instance_id: file_instances.id
        file_version_id: file_versions.id（用于 file_symbol_versions.file_version_id）
        symbols: list of dict，每个 dict 含：
            - symbol_content_hash (str): symbol_contents.content_hash
            - name (str)
            - kind (str)
            - qualified_name (str)
            - visibility (str)
            - start_line (int)
            - end_line (int)
            - start_col (int)
            - end_col (int)
            - signature (str)
            - has_comment (int: 0 or 1)
            - comment_content (str): 仅 has_comment=1 时写入
            - module_path (str)
            - depth (int)

    Returns:
        {
            "success": True/False,
            "symbol_contents_inserted": usize,    # INSERT OR IGNORE 命中数
            "symbol_contents_comment_updated": usize,  # UPDATE comment 命中数
            "symbols_inserted": usize,            # INSERT/UPSERT 命中数
            "file_symbol_versions_inserted": usize,
            "old_calls_deleted": usize,           # DELETE calls 命中数
            "old_symbols_deleted": usize,         # DELETE symbols 命中数
            "error": Optional[str],
        }
    """
```

### 3.2 不暴露的 API

| API | 原因 |
|---|---|
| `_save_file_version` | Phase 2-4 范围（含 git commit_hash + ast_cache） |
| `_build_call_graph_multi_lang` 的 resolve 部分 | Phase 2-3 范围（5 策略 in-memory resolve） |
| `_build_call_graph_multi_lang` 的 calls 写入部分 | Phase 2-5 范围（依赖 2-3 resolve） |
| FTS5 触发器管理 | Python 端继续管理（_build_multi_lang 已有 DROP/REBUILD） |
| 二级索引管理 | Python 端继续管理（_drop_indexes_for_build / _create_indexes_after_build） |

## 4. 行为契约（Python ↔ Rust 必须一致）

### 4.1 batch_save_symbols（B1-B6）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| B1 | 单文件 3 个 symbols（无 comment） | INSERT 3 个 symbols + 3 个 symbol_contents + 3 个 file_symbol_versions | 同 | 两端 count 一致 |
| B2 | 单文件含 has_comment=1 的 symbol | INSERT OR IGNORE symbol_contents + UPDATE comment_content | 同 | 两端 comment_content 一致 |
| B3 | 重复调用同一 file_instance_id（幂等性） | 第二次 DELETE + INSERT，symbols 数不变 | 同 | 两端幂等性一致 |
| B4 | file_instance_id 已有旧 symbols（替换语义） | DELETE 旧 symbols + DELETE 关联 calls + INSERT 新 | 同 | 两端替换语义一致 |
| B5 | ON CONFLICT 更新（同 file_instance_id + name + start_line） | ON CONFLICT DO UPDATE SET visibility/end_line/... | 同 | 两端 conflict 解决一致 |
| B6 | 空 symbols 列表 | 直接 return（不写入 symbols，不 DELETE 旧 symbols + calls） | 同 | 两端行为一致（空列表时无任何 SQL 执行） |

> **B6 行为修正说明**（2026-07-27）：契约原描述为"空列表 → 仍 DELETE 旧 symbols + calls"，但 Python 真相源 [db_build.py:2977-2978](../../db/db_build.py) `_save_symbols_for_version` 在 `all_symbols` 为空时**直接 return**（不执行任何 SQL）。Rust 实现已修正以 Python 真相源为准，空列表时直接返回空结果。差分测试 `test_b6_empty_symbols_list` 验证两端预填的旧数据均保留。

### 4.2 预期差异清单

| 字段 | Python | Rust | 处理策略 |
|---|---|---|---|
| 批量写入方式 | `executemany` | Rust prepared statement + 循环 bind/step | 行为等价，差分测试不强制内部实现 |
| 事务边界 | 外层 `_build_multi_lang` 单一大事务 | BEGIN IMMEDIATE → COMMIT 独立事务 | **预期差异**：Rust 独立事务，中途失败回滚；Python 在外层事务中，失败由外层回滚。差分测试在单文件场景验证一致性，多文件事务边界差异在 Phase 2-5 评估 |
| FTS5 触发器 | Python 外层已 DROP，写入后 REBUILD | Rust 写入时触发器状态由 Python 控制 | Rust 端不管理 FTS，与 Python 一致 |
| qname_cache 失效 | Python `_invalidate_qname_cache` | Python 端继续调用（Rust 不知道缓存） | 不影响差分 |
| GraphStore 失效 | Python `_invalidate_graph_store` | Python 端继续调用 | 不影响差分 |

## 5. 事务边界（AGENTS.md 规则 6）

### 5.1 写策略

`batch_save_symbols` 是**写操作**，必须满足：

1. **持有写锁**：BEGIN IMMEDIATE → 全部 SQL → COMMIT
2. **busy_timeout=5000**：写锁冲突时最多等 5 秒
3. **不与 Python 写入并发**：Python 端 `_save_symbols_for_version` 也持有写锁，两者并发会撞锁
4. **回滚语义**：失败时 ROLLBACK，不留半成品

### 5.2 与既有路径的关系

- **Python `_save_symbols_for_version`**：保留为 rollback 入口
- **Rust `batch_save_symbols`**：作为可选生产入口，通过 `rollback_flag` 切换
- **`_build_multi_lang` 外层事务**：Python 端继续管理外层事务，Rust 调用是独立子事务。若外层回滚，Rust 已写入的数据也会被回滚（同一 DB 文件）。

### 5.3 调用方决策

`db_build.py:_build_multi_lang` step 3.5 应在 wire-production 步骤中：
1. 读取 rollback_flag（通过 `is_feature_rolled_back("rust_batch_save_symbols")`）
2. flag=0 → 走 Rust `batch_save_symbols`
3. flag=1 → 走 Python `_save_symbols_for_version`

## 6. 实现计划

### 6.1 Rust 端

**新文件 `rust_ext/src/batch_build.rs`**（核心实现）：

```rust
//! Phase 2-2: 批量 symbols 写入核心实现
//!
//! 对应 Python `db_build.py:_save_symbols_for_version`

use rusqlite::{params, Connection};

pub struct BatchSaveResult {
    pub symbol_contents_inserted: usize,
    pub symbol_contents_comment_updated: usize,
    pub symbols_inserted: usize,
    pub file_symbol_versions_inserted: usize,
    pub old_calls_deleted: usize,
    pub old_symbols_deleted: usize,
}

pub fn batch_save_symbols(
    conn: &Connection,
    workspace_id: i64,  // 校验用，不写入
    file_instance_id: i64,
    file_version_id: i64,
    symbols: &[SymbolInfo],
) -> Result<BatchSaveResult, rusqlite::Error> {
    // 1. INSERT OR IGNORE INTO symbol_contents（批量）
    // 2. UPDATE symbol_contents SET has_comment=1, comment_content=?（仅 has_comment=1）
    // 3. DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id=?)
    // 4. DELETE FROM symbols WHERE file_instance_id=?
    // 5. INSERT INTO symbols ... ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET ...
    // 6. INSERT INTO file_symbol_versions
}
```

**新文件 `rust_ext/src/batch_build_query.rs`**（PyO3 暴露层）：

```rust
//! Phase 2-2: 批量 symbols 写入 PyO3 暴露层

use pyo3::prelude::*;
use pyo3::types::PyDict;
use rusqlite::Connection;

#[pyfunction]
#[pyo3(signature = (codegraph_db_path, workspace_id, file_instance_id, file_version_id, symbols))]
pub fn batch_save_symbols<'py>(
    py: Python<'py>,
    codegraph_db_path: &str,
    workspace_id: i64,
    file_instance_id: i64,
    file_version_id: i64,
    symbols: Vec<Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyDict>> {
    // 1. open codegraph_conn (readwrite) + busy_timeout=5000
    // 2. BEGIN IMMEDIATE
    // 3. 调用 batch_build::batch_save_symbols
    // 4. COMMIT
    // 5. 返回 dict
}
```

### 6.2 PyO3 注册（`rust_ext/src/lib.rs`）

```rust
mod batch_build;
mod batch_build_query;
// ...
m.add_function(wrap_pyfunction!(batch_build_query::batch_save_symbols, m)?)?;
```

### 6.3 差分测试（`tests/test_phase2_2_behavioral_diff.py` 新建）

- `TestBatchSaveSymbolsDiff`：B1-B6（batch_save_symbols 差分）

### 6.4 wire-production（`db/db_rollback_config.py` 登记）

```powershell
cw rollback register `
  --task-id T-<new-task-id> `
  --feature rust_batch_save_symbols `
  --phase 2 `
  --production-entry "rust_ext/src/batch_build_query.rs::batch_save_symbols" `
  --rollback-entry "db/db_build.py:_save_symbols_for_version" `
  --window 2026-12-31T00:00:00
```

## 7. Schema 信息（真相源）

### 7.1 涉及表 schema

| 表 | 关键字段 | 来源 |
|---|---|---|
| `symbol_contents` | content_hash PK, name, kind, content, signature, has_comment, comment_content, qualified_name | [db/schema.py:111-120](file:///c:/git_work/callwarden/db/schema.py) |
| `symbols` | id PK, file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line, ..., ON CONFLICT(file_instance_id, name, start_line) | [db/schema.py:46-65](file:///c:/git_work/callwarden/db/schema.py) |
| `file_symbol_versions` | id PK, file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted | [db/schema.py:123-135](file:///c:/git_work/callwarden/db/schema.py) |
| `calls` | caller_id FK symbols(id), callee_id, ... | [db/schema.py:68-81](file:///c:/git_work/callwarden/db/schema.py) |

### 7.2 字段映射

| Python 字段 | Rust 字段 | 说明 |
|---|---|---|
| `symbol_contents.content_hash` | `String` | SHA-256 hex64 |
| `symbols.symbol_hash` | `String` | 与 symbol_contents.content_hash 一致 |
| `symbols.visibility` | `String` | public/private/protected |
| `symbols.depth` | `i64` | -1 表示未计算 |
| `file_symbol_versions.file_version_id` | `i64` | 来自 `_save_file_version` 返回值 |
| `file_symbol_versions.is_deleted` | `0` | 始终 0（新插入） |

## 8. 验收标准

- [ ] `cargo check --manifest-path rust_ext/Cargo.toml` 通过
- [ ] `maturin build -i C:\Python314\python.exe --release` 生成 cp314 wheel
- [ ] `pip install` 后 `cw server --check-imports` 通过
- [ ] `pytest tests/test_phase2_2_behavioral_diff.py -v` 全部通过（含 B1-B6）
- [ ] `pytest tests/test_phase2_behavioral_diff.py -v` 不破坏（Phase 2-1 差分测试通过）
- [ ] `pytest tests/test_phase1_behavioral_diff.py -v` 不破坏（Phase 1 差分测试通过）
- [ ] Phase 2-2 行升级为 `✅(behavioral)`

## 9. 风险与注意事项

1. **不切换默认路径**：Python `_save_symbols_for_version` 仍主导。Rust API 仅作为可选短路，通过 rollback_flag 切换。
2. **外层事务边界**：Python `_build_multi_lang` 在 db_build.py:1676 才 commit，Rust 调用是独立子事务。若外层回滚，Rust 已写入的数据也会被回滚（同一 DB 文件）。Phase 2-5 切换默认路径时需评估是否需要把整个 `_build_multi_lang` 迁移到 Rust。
3. **FTS5 触发器**：Python `_build_multi_lang` 在 db_build.py:1593-1595 DROP FTS 触发器，db_build.py:1660-1664 REBUILD。Rust 写入时触发器状态由 Python 控制，Rust 端不管理。
4. **二级索引**：Python `_drop_indexes_for_build` / `_create_indexes_after_build` 在外层管理，Rust 端不管理。
5. **`_invalidate_qname_cache` / `_invalidate_graph_store`**：Python 端在写入后继续调用，Rust 端不管理。
6. **ON CONFLICT 语义**：SQLite 的 `ON CONFLICT(file_instance_id, name, start_line) DO UPDATE` 需要 UNIQUE INDEX `idx_symbols_unique`（db/schema.py:204）。Rust 端依赖此索引，不主动创建。
7. **executemany vs 循环 execute**：Python `executemany` 在 sqlite3 中是单条 prepared + 多次 bind/step，Rust 用同样模式（prepare + 循环 bind/step），性能等价。
8. **file_version_id 校验**：file_version_id 必须已存在于 `file_versions` 表中（由 `_save_file_version` 创建）。Rust 端不校验，依赖 Python 端调用顺序。
9. **workspace_id 校验**：Rust 端不校验 workspace_id（仅作为 metadata 传递），依赖 Python 端确保 file_instance_id 属于正确 workspace。
10. **回滚窗口**：rollback_window_until=2026-12-31，Phase 7 删除 rollback_entry。
