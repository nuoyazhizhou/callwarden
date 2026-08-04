# Phase 2-6-1 契约：增量构建（incremental build）

> **范围**：将 Python `db/db_build.py` 中增量构建的两个核心函数通过 PyO3 暴露给 Rust，
> 并通过 Python↔Rust 行为差分测试验证一致性。
>
> 1. `_compute_and_apply_symbol_diff`：符号 diff 计算 + 应用 `is_deleted=1` 删除标记
>    （当前作为 `_save_file_version_via_rust` 的 Python 回调，本子任务迁移到 Rust）
> 2. `_load_file_result_from_db`：从 DB 加载已解析文件结果（全量构建 `_from_db` 短路用，只读）
>
> **不在范围**：
> - `_from_db` 过滤逻辑（已在 Phase 2-4 `_build_call_graph_multi_lang_via_rust` 中实现）
> - `only_files` 增量 resolve 模式（已在 Phase 2-4 `_build_call_graph_multi_lang_via_rust` 中实现）
> - `_try_ast_cache_short_circuit`（AST 缓存短路，涉及文件系统读取，留作 Python）
> - `_save_file_version` 本体（已在 Phase 2-4 `batch_save_file_versions` 中实现）
> - `_save_symbols_for_version` 本体（已在 Phase 2-2 `batch_save_symbols` 中实现）

## 1. Python 真相源盘点

### 1.1 _compute_and_apply_symbol_diff

**入口**：`db/db_build.py:3169-3209`

**调用方**：
- `_save_file_version_via_rust`（2856 行）：Rust `batch_save_file_versions` 返回 `is_new_version=True` + `prev_version_id` 后回调
- `_save_file_version_python`（2928 行）：Python 路径新建版本后调用

**逻辑**：
1. 查询 `prev_version_id` 的所有符号（`file_symbol_versions` 表，`symbol_hash` + `qualified_name`）
2. 查询 `curr_version_id` 的所有符号（`id` + `symbol_hash` + `qualified_name`）
3. 找出删除的符号：`prev_symbols.keys() - curr_symbols.keys()`
4. 对每个删除的符号，从 prev_version 查询位置信息（`start_line, end_line, module_path, depth`）
5. INSERT 到 curr_version，标记 `is_deleted=1`

**SQL**：
```sql
-- 步骤 1
SELECT symbol_hash, qualified_name FROM file_symbol_versions WHERE file_version_id = ?
-- 步骤 2
SELECT id, symbol_hash, qualified_name FROM file_symbol_versions WHERE file_version_id = ?
-- 步骤 4（每个删除符号）
SELECT start_line, end_line, module_path, depth FROM file_symbol_versions
  WHERE file_version_id = ? AND qualified_name = ?
-- 步骤 5
INSERT INTO file_symbol_versions
  (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted)
  VALUES (?, ?, ?, ?, ?, ?, ?, 1)
```

**事务边界**：在外层事务中执行（`_save_file_version_via_rust` 或 `_save_file_version_python` 不显式 COMMIT，由调用方 `refresh_file` / `_build_multi_lang` 统一 COMMIT）。

### 1.2 _load_file_result_from_db

**入口**：`db/db_build.py:1936-1993`

**调用方**：
- `_build_multi_lang`（1212 行）：全量构建时，mtime 未变的文件从 DB 加载结果，标记 `_from_db=True`

**逻辑**：
1. 查询 `file_versions` 获取 `content_hash` + `total_lines`
2. 查询 `file_symbol_versions JOIN symbol_contents` 获取符号详情（`is_deleted=0`）
3. 查询 `calls JOIN symbols` 获取调用关系（通过 `file_instance_id` 关联）
4. 组装结果 dict，标记 `_from_db=True`

**SQL**：
```sql
-- 步骤 1
SELECT content_hash, total_lines FROM file_versions WHERE id = ?
-- 步骤 2
SELECT sv.id, sv.symbol_hash, sv.qualified_name, sv.start_line, sv.end_line,
       sv.module_path, sv.depth, sv.is_deleted,
       sc.name, sc.kind, sc.content, sc.signature, sc.has_comment,
       sc.comment_content as doc_comment
FROM file_symbol_versions sv
JOIN symbol_contents sc ON sv.symbol_hash = sc.content_hash
WHERE sv.file_version_id = ? AND sv.is_deleted = 0
-- 步骤 3
SELECT c.caller_name, c.caller_module, c.callee_name, c.callee_module,
       c.callee_qualified, c.callee_file, c.callee_id, c.call_line, c.is_cross_file
FROM calls c
JOIN symbols s ON c.caller_id = s.id
WHERE s.file_instance_id = ?
```

**事务边界**：只读查询，无事务。

### 1.3 增量构建整体流程

```
_build_multi_lang (全量构建)
  ├─ mtime 未变 → _load_file_result_from_db → _from_db=True → 跳过 parse + 跳过写入
  └─ mtime 变化 → parse → _save_file_version → _save_symbols_for_version → _build_call_graph_multi_lang

refresh_file (单文件增量刷新)
  ├─ _try_ast_cache_short_circuit (AST 缓存短路)
  ├─ content_hash 短路 (latest.content_hash == result.content_hash)
  └─ 新版本 → _save_file_version → _compute_and_apply_symbol_diff → _save_symbols_for_version
              → _build_call_graph_multi_lang(only_files={rel_path})
```

## 2. Rust API 契约

### 2.1 compute_and_apply_symbol_diff

```rust
/// 计算符号 diff 并应用 is_deleted=1 删除标记
///
/// 对比 prev_version 和 curr_version 的符号集合，找出 prev 有但 curr 没有的符号，
/// 在 curr_version 中插入 is_deleted=1 的标记记录。
///
/// 返回 dict：
/// {
///     "success": bool,
///     "removed_count": usize,  // 插入的 is_deleted=1 记录数
///     "removed_names": Vec<String>,  // 删除的符号 qualified_name 列表
/// }
#[pyfunction]
fn compute_and_apply_symbol_diff(
    codegraph_db_path: &str,
    prev_version_id: i64,
    curr_version_id: i64,
) -> PyResult<PyObject>
```

**特性**：
- 写操作：BEGIN IMMEDIATE 事务，失败 ROLLBACK
- 复用 `open_readwrite` helper（与 `batch_save_symbols` / `batch_save_file_versions` 一致）
- `busy_timeout=5000`

### 2.2 load_file_result_from_db

```rust
/// 从 DB 加载已解析的文件结果（增量构建 _from_db 短路用）
///
/// 组装 file_versions + file_symbol_versions + symbol_contents + calls 的完整结果 dict。
/// 返回 None 表示文件版本不存在或查询失败。
///
/// 返回 dict 结构与 Python _load_file_result_from_db 一致：
/// {
///     "abs_path": String,
///     "rel_path": String,
///     "module_path": String,
///     "file_instance_id": i64,
///     "file_version_id": i64,
///     "symbols": Vec<Dict>,  // 每个 dict 含 id/symbol_hash/qualified_name/start_line/end_line/...
///     "raw_calls": Vec<Dict>,
///     "imports": Vec<>,  // 空列表
///     "content_hash": String,
///     "total_lines": i64,
///     "inline_modules": Vec<>,  // 空列表
///     "_from_db": true,
/// }
#[pyfunction]
fn load_file_result_from_db(
    codegraph_db_path: &str,
    file_instance_id: i64,
    file_version_id: i64,
    rel_path: String,
    abs_path: String,
    module_path: String,
) -> PyResult<Option<PyObject>>
```

**特性**：
- 只读查询：`open_readonly` helper（与 `cas_query` / `manifest_query` 一致）
- **不执行 WAL checkpoint(PASSIVE)**（T-1785831377543-8d626745）：只读连接经
  WAL + `-shm` 总能读到最新已提交数据，checkpoint 冗余；且 Windows + WAL 下
  register 写事务后 checkpoint 会进入 SQLite 内部 sleep 循环不受 busy_timeout
  控制，导致 refresh-all 无限阻塞。open 加 8s 有界超时 + 全局降级标记：超时后
  本次进程后续只读连接快速失败，Python 侧 `_load_file_result_from_db_python`
  用主连接降级查询，不挂死。
- 返回 Python dict（通过 `PyDict`），与 Python 路径结构完全一致

## 3. 行为契约

### 3.1 compute_and_apply_symbol_diff 行为契约

| ID | 场景 | 输入 | 预期行为 |
|---|---|---|---|
| D1 | 无删除符号 | prev={A,B}, curr={A,B} | `removed_count=0`，无 INSERT |
| D2 | 全部删除 | prev={A,B}, curr={} | `removed_count=2`，2 条 is_deleted=1 记录 |
| D3 | 部分删除 | prev={A,B,C}, curr={B} | `removed_count=2`（A,C），2 条 is_deleted=1 记录 |
| D4 | 新增符号 | prev={A}, curr={A,B} | `removed_count=0`，B 不受影响（B 由 `_save_symbols_for_version` 写入） |
| D5 | 混合变更 | prev={A,B,C}, curr={B,C,D} | `removed_count=1`（A），D 不受影响 |
| D6 | prev 为空 | prev={}, curr={A,B} | `removed_count=0`，无 INSERT |
| D7 | curr 为空 | prev={A,B}, curr={} | `removed_count=2`，2 条 is_deleted=1 记录 |
| D8 | prev_version_id 不存在 | prev_version_id=999999 | `success=false`，无 INSERT（与 Python 一致：prev_symbols 为空，removed_names 为空） |
| D9 | curr_version_id 不存在 | curr_version_id=999999 | `success=false`，无 INSERT（与 Python 一致：curr_symbols 为空，但 prev_symbols 非空时 removed_names = prev 全集，但 INSERT 到不存在的 curr_version_id 会 FK 失败 → Rust 端返回 success=false） |
| D10 | 位置信息正确性 | prev 符号 start_line=10, end_line=20, module_path="m", depth=1 | INSERT 的 is_deleted=1 记录位置信息与 prev 一致 |

### 3.2 load_file_result_from_db 行为契约

| ID | 场景 | 输入 | 预期行为 |
|---|---|---|---|
| L1 | 正常加载 | file_version_id 存在，有 3 符号 + 2 calls | 返回 dict，symbols=[3项], raw_calls=[2项], _from_db=True |
| L2 | 空版本 | file_version_id 存在，0 符号 0 calls | 返回 dict，symbols=[], raw_calls=[] |
| L3 | version 不存在 | file_version_id=999999 | 返回 None |
| L4 | is_deleted 过滤 | file_version 有 5 符号，其中 2 个 is_deleted=1 | symbols 只含 3 个 is_deleted=0 的符号 |
| L5 | content_hash + total_lines | file_versions.content_hash="abc", total_lines=100 | dict 的 content_hash="abc", total_lines=100 |
| L6 | calls 关联 | calls 通过 caller_id JOIN symbols.file_instance_id 关联 | raw_calls 只含当前 file_instance_id 的 calls |
| L7 | 字段完整性 | 符号含 name/kind/content/signature/has_comment/doc_comment | dict 字段与 Python 路径完全一致 |

### 3.3 差分测试矩阵

| 测试类 | 场景 | Python 路径 | Rust 路径 | 断言 |
|---|---|---|---|---|
| TestComputeSymbolDiffDiff | D1-D10 | `_compute_and_apply_symbol_diff` | `compute_and_apply_symbol_diff` | removed_count 一致 + DB 行数一致 + is_deleted 标记一致 |
| TestLoadFileResultFromDbDiff | L1-L7 | `_load_file_result_from_db` | `load_file_result_from_db` | 返回 dict 结构一致（symbols/raw_calls/content_hash/total_lines/_from_db） |

## 4. 事务边界

| API | 锁类型 | 事务 | 回滚 |
|---|---|---|---|
| `compute_and_apply_symbol_diff` | 写锁 | BEGIN IMMEDIATE → COMMIT | 失败 ROLLBACK，返回 success=false |
| `load_file_result_from_db` | 无写锁 | 只读连接，无事务 | N/A |

**与 Python 路径的差异**：
- Python `_compute_and_apply_symbol_diff` 在外层事务中执行（无显式 BEGIN/COMMIT）
- Rust `compute_and_apply_symbol_diff` 是独立子事务（BEGIN IMMEDIATE → COMMIT）
- 与 Phase 2-2 / 2-3 / 2-4 的写操作模式一致

## 5. Schema 信息

涉及的表（已在 `db/schema.py` 中定义，无需新增）：

```sql
-- file_symbol_versions（符号版本快照）
CREATE TABLE file_symbol_versions (
    id INTEGER PRIMARY KEY,
    file_version_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER,
    end_line INTEGER,
    module_path TEXT,
    depth INTEGER,
    is_deleted INTEGER DEFAULT 0,
    FOREIGN KEY (file_version_id) REFERENCES file_versions(id)
);

-- symbol_contents（符号内容去重）
CREATE TABLE symbol_contents (
    content_hash TEXT PRIMARY KEY,
    name TEXT, kind TEXT, content TEXT,
    signature TEXT, has_comment INTEGER,
    comment_content TEXT
);

-- file_versions（文件版本历史）
CREATE TABLE file_versions (
    id INTEGER PRIMARY KEY,
    file_instance_id INTEGER NOT NULL,
    version_num INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    mtime REAL, total_lines INTEGER,
    parsed_at REAL, is_current INTEGER,
    is_deleted INTEGER, commit_hash TEXT,
    ast_cache BLOB
);

-- calls（调用关系快照）
CREATE TABLE calls (
    id INTEGER PRIMARY KEY,
    caller_id INTEGER NOT NULL,
    callee_id INTEGER,
    caller_name TEXT, caller_module TEXT,
    callee_name TEXT, callee_module TEXT,
    callee_qualified TEXT, callee_file TEXT,
    call_line INTEGER, is_cross_file INTEGER
);

-- symbols（符号快照）
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY,
    file_instance_id INTEGER NOT NULL,
    name TEXT, kind TEXT,
    qualified_name TEXT, module_path TEXT,
    start_line INTEGER, end_line INTEGER,
    symbol_hash TEXT, depth INTEGER, has_comment INTEGER
);
```

## 6. 实现计划

### 6.1 Rust 实现（`rust_ext/src/incremental_build_query.rs`）

```rust
// 1. compute_and_apply_symbol_diff
//    - open_readwrite(codegraph_db_path)
//    - PRAGMA journal_mode=WAL + busy_timeout=5000 + wal_checkpoint(PASSIVE)
//    - BEGIN IMMEDIATE
//    - SELECT prev symbols (symbol_hash, qualified_name) WHERE file_version_id = prev
//    - SELECT curr symbols (id, symbol_hash, qualified_name) WHERE file_version_id = curr
//    - removed_names = prev_keys - curr_keys
//    - for name in removed_names:
//        SELECT start_line, end_line, module_path, depth FROM prev WHERE qualified_name = name
//        INSERT INTO file_symbol_versions (..., is_deleted=1)
//    - COMMIT
//    - return {"success": true, "removed_count": n, "removed_names": [...]}

// 2. load_file_result_from_db
//    - open_readonly(codegraph_db_path)（8s 有界超时 + 全局降级标记；
//      不执行 wal_checkpoint(PASSIVE)，T-1785831377543-8d626745）
//    - SELECT content_hash, total_lines FROM file_versions WHERE id = file_version_id
//    - SELECT sv.*, sc.* FROM file_symbol_versions sv JOIN symbol_contents sc
//        WHERE sv.file_version_id = ? AND sv.is_deleted = 0
//    - SELECT c.* FROM calls c JOIN symbols s ON c.caller_id = s.id
//        WHERE s.file_instance_id = ?
//    - 组装 PyDict，返回 Some(dict)
```

### 6.2 PyO3 注册（`rust_ext/src/lib.rs`）

```rust
mod incremental_build_query;
m.add_function(incremental_build_query::compute_and_apply_symbol_diff)?;
m.add_function(incremental_build_query::load_file_result_from_db)?;
```

### 6.3 Python wire-production

```python
# db/db_build.py _save_file_version_via_rust 中：
# 原：self._compute_and_apply_symbol_diff(single["prev_version_id"], file_version_id)
# 新：
if not self.is_feature_rolled_back("rust_compute_symbol_diff"):
    from callwarden_core import compute_and_apply_symbol_diff
    compute_and_apply_symbol_diff(self.db_path, single["prev_version_id"], file_version_id)
else:
    self._compute_and_apply_symbol_diff(single["prev_version_id"], file_version_id)

# db/db_build.py _build_multi_lang 中：
# 原：old_result = self._load_file_result_from_db(...)
# 新：
if not self.is_feature_rolled_back("rust_load_file_result"):
    from callwarden_core import load_file_result_from_db
    old_result = load_file_result_from_db(self.db_path, ...)
else:
    old_result = self._load_file_result_from_db(...)
```

### 6.4 rollback_config 登记

```
cw rollback register --task-id T-1785204662320-4df57f0c \
    --feature rust_compute_symbol_diff --phase 2
cw rollback register --task-id T-1785204662320-4df57f0c \
    --feature rust_load_file_result --phase 2
```

## 7. 验收标准

| 验收项 | 标准 |
|---|---|
| `cargo check` | 0 error（仅 warnings） |
| `maturin build` | cp314 wheel 构建成功 |
| 差分测试 | D1-D10 + L1-L7 全部通过（两端行为一致） |
| 回归测试 | Phase 1 + Phase 2-1~2-5 差分测试不破坏 |
| `cw server --check-imports` | MCP imports OK |
| `cw rollback config` | Phase 2-6-1 已登记（2 个 feature） |
| `cw refresh <文件>` | 成功，DB 同步 |

## 8. 风险与注意事项

### 8.1 预期差异

1. **事务边界差异**：Python `_compute_and_apply_symbol_diff` 在外层事务中执行（无显式 COMMIT），Rust 是独立子事务。与 Phase 2-2/2-3/2-4 一致，差分测试用 BEGIN/COMMIT 包裹 Python 路径模拟外层事务。

2. **短连接开销**：Rust `compute_and_apply_symbol_diff` 每次调用新建 + 关闭连接。WAL 模式下安全，但有性能开销。Phase 2-6-3 评估连接复用。

3. **`load_file_result_from_db` 返回 dict 的 Python 类型**：Rust 端用 `PyDict` 组装，`symbols` / `raw_calls` 是 `PyList`，每个元素是 `PyDict`。字段名与 Python 路径完全一致。

### 8.2 不切换默认路径（与 Phase 2-2~2-5 一致）

- Python `_compute_and_apply_symbol_diff` / `_load_file_result_from_db` 仍作为 fallback
- Rust API 通过 `is_feature_rolled_back` 控制切换
- Rust 失败时 fail-soft 降级到 Python

### 8.3 性能考量

- `_compute_and_apply_symbol_diff` 的 N+1 查询模式（每个删除符号一次 SELECT + INSERT）在大量删除时较慢。Rust 端可优化为批量查询 + 批量 INSERT，但本子任务保持与 Python 一致的行为，优化留待 Phase 2-6-3。
- `load_file_result_from_db` 是只读查询，性能影响小。

### 8.4 WAL 模式与只读连接

- `compute_and_apply_symbol_diff`：READWRITE 连接，busy_timeout=5000
- `load_file_result_from_db`：READONLY 连接，**不执行 WAL checkpoint(PASSIVE)**
  （T-1785831377543-8d626745：Windows + WAL 下 register 写事务后 checkpoint 会
  无限阻塞；只读连接经 WAL/-shm 总能读到最新已提交数据）+ 8s 有界超时 + 全局
  降级标记（超时后本次进程后续只读连接快速失败，Python 主连接降级查询）
- `busy_timeout=5000`，写锁冲突时最多等 5 秒
