# Phase 2-4 契约：批量文件历史版本写入（batch_save_file_versions）

> **范围**：将 Python `db/db_build.py::_save_file_version` 的 file_versions 历史版本写入路径
> 通过 PyO3 暴露给 Python，并通过 Python↔Rust 行为差分测试验证一致性。
>
> **不在范围**：
> - `_compute_and_apply_symbol_diff`（符号 diff 计算，复杂逻辑留作 Python 回调，Rust 返回
>   `prev_version_id` 后由 Python 调用）
> - `_get_head_commit_cached`（git subprocess fork，由 Python 预计算后传入 commit_hash）
> - `detect_language_from_path`（Python 语言检测，由 Python 预计算后传入 language）
> - `os.path.getmtime`（文件系统访问，由 Python 预计算后传入 mtime）
> - `read_file_normalized` / `file_content_hash`（文件读取+归一化，由 Python 预计算后传入 ast_cache_metadata）

## 1. Python 真相源盘点

### 1.1 入口函数

`db/db_build.py::_save_file_version(self, file_instance_id: int, result: Dict[str, Any]) -> int`，行 2611-2679。

### 1.2 调用方

- `db_build.py:839` `build` 全量构建流程
- `db_build.py:1607` `_build_multi_lang` 批量构建流程（优化路径，DROP FTS/索引后批量写入）
- `db_build.py:3515` `_refresh_file_rust` Rust 解析器增量刷新
- `db_build.py:3568` `_refresh_file_generic` 通用语言增量刷新

注意：调用方 3/4（refresh 路径）在调用 `_save_file_version` 前已有自己的 content_hash 短路检查，
所以 `_save_file_version` 内部的短路分支主要在 build 流程中触发。

### 1.3 两分支逻辑

#### 分支 A：短路分支（内容未变，content_hash 与 latest 相同）

```python
if latest and latest["content_hash"] == content_hash:
    # 1. UPDATE file_versions SET mtime=?, commit_hash=? WHERE id=latest.id
    # 2. UPDATE file_instances SET current_content_hash=?, last_parsed=?, total_lines=?, mtime=? WHERE id=?
    # 3. _update_ast_cache(latest.id, result, content_hash, parsed_at)  # 更新 ast_cache 元数据
    return latest["id"]
```

#### 分支 B：新版本分支（内容变化或首次写入）

```python
# 1. UPDATE file_versions SET is_current=0 WHERE id=latest.id  (如有 latest)
# 2. INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime,
#    total_lines, parsed_at, is_current, is_deleted, commit_hash)
#    VALUES (?, ?, ?, ?, ?, ?, 1, 0, ?)
# 3. UPDATE file_instances SET current_content_hash=?, last_parsed=?, total_lines=?, mtime=? WHERE id=?
# 4. _update_ast_cache(new_version_id, result, content_hash, parsed_at)
# 5. _compute_and_apply_symbol_diff(prev_version_id, new_version_id)  # 如有 prev
return new_version_id
```

### 1.4 _update_ast_cache 元数据

`_update_ast_cache`（db_build.py:2681-2733）构建 JSON 元数据并写入 `file_versions.ast_cache` BLOB 字段：

```python
metadata = {
    "content_hash": content_hash,           # parser 的 content_hash
    "file_content_hash": file_content_hash, # read_file_normalized 的 hash
    "parsed_at": parsed_at,
    "incremental": result.get("incremental", False),
    "changed_ranges_count": len(result.get("changed_ranges", [])),
    "language": result.get("language", ""),
}
# JSON 序列化为 bytes，UPDATE file_versions SET ast_cache=? WHERE id=?
```

- `file_content_hash` 由 `read_file_normalized(abs_path)` 计算（Python 侧文件读取+归一化）
- 如 ast_cache 字段不存在（v27 库未迁移到 v28），`sqlite3.OperationalError` 被静默吞掉

### 1.5 version_num 计算

- 有 latest：`version_num = latest["version_num"] + 1`
- 无 latest（首次写入）：`version_num = 1`

### 1.6 _get_file_version 辅助函数

`_get_file_version(file_instance_id)` 返回当前 `is_current=1` 的版本（如存在），用于短路判断和 version_num 推导。

### 1.7 _get_head_commit_cached

`_get_head_commit_cached`（db_build.py:2588-2609）fork `git rev-parse HEAD` 一次，缓存到 `self._cached_head_commit`。
非 git 仓库或 fork 失败时返回空字符串 `""`。

## 2. API 契约

### 2.1 主函数：`batch_save_file_versions`

```rust
#[pyfunction]
#[pyo3(signature = (
    codegraph_db_path,
    file_results,  // List[Dict] - 每个文件预计算好的版本信息
))]
pub fn batch_save_file_versions<'py>(
    py: Python<'py>,
    codegraph_db_path: &str,
    file_results: Vec<Bound<'py, PyDict>>,
) -> PyResult<Bound<'py, PyDict>>
```

**返回 dict**：

```python
{
    "success": bool,
    "files_processed": int,
    "new_versions": int,          # 新建版本数（分支 B）
    "short_circuited": int,       # 短路数（分支 A）
    "results": [                  # 每个文件的结果（与输入顺序一致）
        {
            "file_instance_id": int,
            "file_version_id": int,     # 新版本或现有版本的 id
            "is_new_version": bool,      # True=新建版本，False=短路
            "prev_version_id": Optional[int],  # 新建版本时的前一版本 id（用于 Python 回调 _compute_and_apply_symbol_diff）
        },
        ...
    ],
    "error": Optional[str],
}
```

### 2.2 输入字段约束

**`file_results` 中每个 dict 必须含**：

| 字段 | 类型 | 说明 | Python 真相源对应 |
|---|---|---|---|
| `file_instance_id` | int | 文件实例 id | `result["file_instance_id"]` |
| `content_hash` | str | 内容 hash（parser 计算） | `result["content_hash"]` |
| `mtime` | float | 文件修改时间（Python 预计算 `os.path.getmtime`） | `os.path.getmtime(result["abs_path"])` |
| `total_lines` | int | 总行数 | `result["total_lines"]` |
| `parsed_at` | float | 解析时间戳 | `time.time()` |
| `language` | str | 语言（Python 预计算 `detect_language_from_path`） | `detect_language_from_path(result.get("rel_path", ""))` |
| `commit_hash` | str | Git HEAD commit hash（Python 预计算 `_get_head_commit_cached`） | `self._get_head_commit_cached()` |
| `ast_cache_metadata` | Dict | ast_cache 元数据（Python 预构建） | `_update_ast_cache` 内部 metadata dict |

**`ast_cache_metadata` dict 字段**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `content_hash` | str | parser 的 content_hash |
| `file_content_hash` | str | read_file_normalized 的 hash |
| `parsed_at` | float | 解析时间戳 |
| `incremental` | bool | 是否增量解析 |
| `changed_ranges_count` | int | 变更范围数 |
| `language` | str | 语言 |

### 2.3 事务边界

- 函数内部 `BEGIN IMMEDIATE` → 全部 SQL → `COMMIT`/`ROLLBACK`
- 与 `batch_save_symbols` / `batch_resolve_and_save_calls` 一致：失败不抛异常，返回 `{"success": false, "error": "..."}`
- 单一事务覆盖所有文件：任一文件失败则整批 ROLLBACK

### 2.4 ast_cache 字段不存在时的降级

- Python 真相源用 try/except `sqlite3.OperationalError` 兼容 v27 库（ast_cache 字段不存在）
- Rust 端：先检测 ast_cache 字段是否存在（`PRAGMA table_info(file_versions)` 查列名），不存在则跳过 ast_cache UPDATE，不报错

## 3. 行为契约（V1-VN 差分测试矩阵）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| V1 | 单文件 + 首次写入（无 latest） | version_num=1，is_current=1，INSERT 新行 | 同 | 两端 file_versions 表行一致（version_num/content_hash/mtime/total_lines/parsed_at/is_current/is_deleted/commit_hash），file_instances 更新一致，file_contents 有记录 |
| V2 | 单文件 + 内容变化（latest 存在，content_hash 不同） | version_num=latest+1，旧版本 is_current=0，新版本 is_current=1 | 同 | 两端 file_versions 表行一致（旧版本 is_current=0，新版本 is_current=1），version_num 递增 |
| V3 | 单文件 + 内容未变（短路分支，content_hash 相同） | UPDATE mtime+commit_hash，不新增版本 | 同 | 两端 file_versions 表行数不变，mtime/commit_hash 更新一致，file_instances 更新一致 |
| V4 | 单文件 + ast_cache 写入（新版本分支） | ast_cache BLOB = JSON(metadata) | 同 | 两端 file_versions.ast_cache 字节一致（JSON 解析后字段一致） |
| V5 | 单文件 + ast_cache 写入（短路分支） | ast_cache BLOB 更新 | 同 | 两端 file_versions.ast_cache 字节一致 |
| V6 | 单文件 + commit_hash 为空（非 git 仓库） | commit_hash="" | 同 | 两端 file_versions.commit_hash="" |
| V7 | 单文件 + file_contents 已存在（INSERT OR IGNORE 去重） | INSERT OR IGNORE 不报错 | 同 | 两端 file_contents 表行数不变（已存在） |
| V8 | 多文件批量（3 个文件，混合首次/变化/短路） | 3 个文件分别处理 | 同 | 两端 file_versions 表行一致，results 列表长度=3，顺序与输入一致 |
| V9 | 空 file_results（无文件需处理） | 无 SQL 执行 | 同 | 两端无副作用，dict 返回值一致（files_processed=0） |
| V10 | is_current toggle 验证（多次版本写入） | 每次 INSERT 新版本时旧版本 is_current=0 | 同 | 两端 file_versions 表所有历史版本 is_current=0，最新版本 is_current=1 |
| V11 | version_num 递增（连续 3 次内容变化） | 1→2→3 | 同 | 两端 version_num 序列一致 |
| V12 | ast_cache 字段不存在（v27 库降级） | try/except 静默吞错 | Rust 跳过 UPDATE | 两端 file_versions 表行一致（ast_cache=NULL），不报错 |

## 4. 预期差异（允许的语义等价差异）

| # | Python 行为 | Rust 行为 | 说明 |
|---|---|---|---|
| D1 | `_save_file_version` 单文件调用，外层循环 | `batch_save_file_versions` 单事务批量 | Rust 单事务覆盖所有文件，Python 外层可能多次事务。差分测试用单文件或多文件同事务场景验证 |
| D2 | `executemany` 不适用（每文件 SQL 不同） | 循环 `execute`，每文件一组 SQL | 行为等价 |
| D3 | `_update_ast_cache` 用 `json.dumps(metadata).encode("utf-8")` | Rust 用 `serde_json::to_vec` | JSON 字节序列可能不同（key 顺序），但 JSON 解析后字段一致。差分测试断言 JSON 解析后字段一致 |
| D4 | `_compute_and_apply_symbol_diff` 在 `_save_file_version` 内部调用 | Rust 不调用，返回 `prev_version_id` 由 Python 回调 | Python 调用方在 Rust 返回后执行 `_compute_and_apply_symbol_diff` |

## 5. 事务与错误处理

- **BEGIN IMMEDIATE**：与 `batch_save_symbols` / `batch_resolve_and_save_calls` 一致，立刻拿写锁
- **失败 ROLLBACK**：任何 SQL 错误都触发 ROLLBACK，返回 `{"success": false, "error": "..."}`
- **不抛 Python 异常**：所有错误包装为 dict 返回
- **ast_cache 降级**：字段不存在时跳过 UPDATE，不视为错误

## 6. 实现计划

### 6.1 Rust 模块结构

- 新建 `rust_ext/src/batch_file_versions_query.rs`：
  - `pub fn batch_save_file_versions(...)` — PyO3 入口
  - `fn extract_file_version_info(dict: &Bound<PyDict>) -> PyResult<FileVersionInfo>` — 提取单个文件信息
  - `fn extract_ast_cache_metadata(dict: &Bound<PyDict>) -> PyResult<AstCacheMetadata>` — 提取 ast_cache 元数据
  - `fn check_ast_cache_column_exists(conn: &Connection) -> bool` — 检测 ast_cache 字段是否存在
  - `fn get_latest_version(conn: &Connection, file_instance_id: i64) -> PyResult<Option<LatestVersion>>` — 查询当前 is_current=1 的版本
  - `fn save_single_file_version(conn: &Connection, info: &FileVersionInfo, ast_cache_exists: bool) -> PyResult<SaveResult>` — 单文件版本写入
  - `struct FileVersionInfo` — 提取的文件版本信息
  - `struct AstCacheMetadata` — ast_cache 元数据
  - `struct LatestVersion` — 当前版本信息（id, version_num, content_hash）
  - `struct SaveResult` — 单文件保存结果
  - `struct BatchSaveVersionsResult` — 批量返回值结构
- `rust_ext/src/lib.rs`：注册 `mod batch_file_versions_query;` + `m.add_function(wrap_pyfunction!(batch_file_versions_query::batch_save_file_versions, m)?)?;`

### 6.2 Python 测试文件

- `tests/test_phase2_4_behavioral_diff.py`：1 个 TestBatchSaveFileVersionsDiff 类，12 个 case（V1-V12）
- 复用 `tests/test_phase2_2_behavioral_diff.py` 的 _make_codegraph_db、_prep_file_instance 等 fixture
- Python 路径走 `BuildMixin._save_file_version` unbound method + 最小 db-like 对象（与 Phase 2-2/2-3 一致）
- Python 路径需要模拟 `_get_head_commit_cached`、`_update_ast_cache`、`detect_language_from_path`、`_compute_and_apply_symbol_diff`

### 6.3 wire-production

- 在 `db/db_build.py` 的 `_save_file_version` 入口处添加 feature_flag 检测：
  ```python
  if _should_use_rust_file_versions():
      result = callwarden_core.batch_save_file_versions(...)
      if result.get("success"):
          # 处理 results，对 is_new_version=True 的文件回调 _compute_and_apply_symbol_diff
          return result["results"][0]["file_version_id"]
      # 失败回退到 Python 路径
  ```
- feature_flag 走 `rollback_config` 表（与 Phase 2-2/2-3 一致）

### 6.4 verify

- 运行 `pytest tests/test_phase2_4_behavioral_diff.py -v` 验证差分
- 运行 `pytest tests/test_phase2_2_behavioral_diff.py tests/test_phase2_3_behavioral_diff.py -v` 验证不破坏现有 Phase 2 测试
- 运行 `cw refresh --all` 在真实项目上验证端到端

### 6.5 refresh

- `cw rollback register` 登记回滚配置（id 自增）
- 更新 `docs/design/migration-manifest.md` Phase 2-4 行状态为 `✅(behavioral)`

## 7. Schema 信息

涉及的表（已在 schema.py 中定义，本契约不修改 schema）：

- `file_versions` (id, file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current, is_deleted, commit_hash, ast_cache)
- `file_instances` (id, current_content_hash, last_parsed, total_lines, mtime, ...)
- `file_contents` (content_hash, language, total_lines, first_seen_at)

## 8. 验收标准

- [ ] `batch_save_file_versions` 在 `lib.rs` 注册并可从 Python 导入
- [ ] V1-V12 差分测试全部通过（12/12）
- [ ] `cw refresh --all` 在真实项目（callwarden 自身）上端到端成功
- [ ] 现有 Phase 2-2/2-3 测试不受影响
- [ ] rollback_config 表登记新 feature（feature=rust_batch_save_file_versions）
- [ ] migration-manifest.md 更新 Phase 2-4 行状态为 `✅(behavioral)`

## 9. 风险与注意事项

### 9.1 _compute_and_apply_symbol_diff 不在范围

`_compute_and_apply_symbol_diff`（db_build.py:2918）是一个复杂的符号 diff 计算，涉及：
- 读取旧 file_symbol_versions
- 读取新 symbols
- 计算 added/removed/modified 符号
- 标记旧 file_symbol_versions 为 is_deleted=1
- 可能插入新的 file_symbol_versions

本契约**不实现此逻辑**，Rust 返回 `prev_version_id` 后由 Python 调用方回调 `_compute_and_apply_symbol_diff`。

### 9.2 ast_cache JSON 字节序列

Python 用 `json.dumps(metadata).encode("utf-8")`，Rust 用 `serde_json::to_vec`。两者的 JSON 字节序列可能不同：
- Python `json.dumps` 默认有空格（`", "` `": "`），Rust `serde_json::to_vec` 默认无空格
- key 顺序可能不同（Python 3.7+ dict 保序，Rust serde_json 按结构体字段顺序）

**差分测试策略**：断言 JSON 解析后字段一致，不断言字节一致。或在 Rust 端用 `serde_json::to_string_pretty` 对齐 Python 默认格式。

### 9.3 短路分支的 mtime 更新

Python 短路分支更新 `mtime` 为 `os.path.getmtime(result["abs_path"])`，Rust 端由 Python 预计算后传入。差分测试中两端传入相同的 mtime 值即可。

### 9.4 单事务 vs 多事务

Python `_save_file_version` 在外层 `build` 的事务中调用（`build` 方法开启事务）。Rust `batch_save_file_versions` 在内部开启单一事务覆盖所有文件。差分测试中用单文件或多文件场景验证，事务边界差异在 Phase 2-5 评估。

### 9.5 _get_file_version 查询

Python `_get_file_version` 查询 `WHERE file_instance_id=? AND is_current=1 ORDER BY version_num DESC LIMIT 1`。Rust 端用相同 SQL。如无 latest，version_num=1，无 is_current toggle。

### 9.6 file_instances UPDATE 字段对齐

Python 更新 file_instances 的 4 个字段：`current_content_hash`, `last_parsed`, `total_lines`, `mtime`。Rust 端必须更新相同字段，避免遗漏导致 file_instances 与 file_versions 不一致。
