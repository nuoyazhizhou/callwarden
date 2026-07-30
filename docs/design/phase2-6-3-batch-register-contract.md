# Phase 2-6-3 契约：大规模性能（连接复用 + 批量注册 + 压测基线）

> **范围**：将 Python `db/db_build.py` 中 `_build_multi_lang` 的 register 阶段循环
> （`_register_file_db` + `_get_file_version` 逐文件 SQL）通过 PyO3 批量化暴露给 Rust，
> 消除 N×3 次 SQLite round-trip，建立 Phase 2-6-3 性能压测基线。
>
> **核心优化**：
> 1. **连接复用**：Rust 单一读写连接处理全部文件（vs Python 每次调用复用 `self.conn` 但仍有 per-call Python↔C 跨语言开销）
> 2. **预处理语句**：SELECT/UPDATE/INSERT 各 prepare 一次，N 次执行（vs Python 每次重新 prepare）
> 3. **单事务批量化**：BEGIN IMMEDIATE → 全部注册+版本查询 → COMMIT（vs Python 在外层事务中逐条 execute）
>
> **不在范围**：
> - `_load_file_result_from_db`（已在 Phase 2-6-1 通过 PyO3 暴露，Python 在 batch_register_files 返回后逐文件调用）
> - `_save_file_version` / `batch_save_file_versions`（已在 Phase 2-4 完成）
> - `import_all_stdlib_symbols`（独立优化目标，涉及符号去重 + 多语言数据查找，留作后续）
> - 索引管理 / `_drop_indexes_for_build` / `_create_indexes_after_build`（DDL，AGENTS.md 规则 8 不迁移）
> - `_infer_module_path_generic` / `detect_language_from_path`（Python 预计算后传入）

## 1. Python 真相源盘点

### 1.1 _register_file_db

**入口**：`db/db_build.py:2754-2787`

**调用方**：
- `_build_multi_lang`（1205 行）：全量构建 register 阶段，逐文件调用
- `refresh_file`（3898 行）：单文件刷新
- `refresh_file_with_result`（3952 行）：单文件刷新（带结果）

**逻辑**：
1. 获取 `workspace_id` + `rel_path` + `mtime`
2. `SELECT id FROM file_instances WHERE workspace_id=? AND rel_path=?`
3. 若存在：`UPDATE file_instances SET mtime=?, module_path=?, status='pending' WHERE id=?`，返回 `id`
4. 若不存在：`INSERT INTO file_instances (...)`，返回 `lastrowid`

**SQL**（每文件 2 次 round-trip）：
```sql
-- 步骤 1（每次调用都执行）
SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?
-- 步骤 2a（存在时）
UPDATE file_instances SET mtime = ?, module_path = ?, status = 'pending' WHERE id = ?
-- 步骤 2b（不存在时）
INSERT INTO file_instances
  (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
  VALUES (?, ?, ?, '', ?, 0, 0, 'pending', ?)
```

### 1.2 _get_file_version

**入口**：`db/db_build.py:2790-2796`

**调用方**：
- `_build_multi_lang`（1209 行）：register 阶段，mtime 变化检查
- `refresh_file`：单文件刷新

**逻辑**：
1. `SELECT * FROM file_versions WHERE file_instance_id=? ORDER BY version_num DESC LIMIT 1`

**SQL**（每文件 1 次 round-trip）：
```sql
SELECT * FROM file_versions WHERE file_instance_id = ? ORDER BY version_num DESC LIMIT 1
```

### 1.3 register 阶段整体循环

**入口**：`db/db_build.py:1185-1223`

**逻辑**：
```python
for i, rel_path in enumerate(files, 1):
    abs_path = os.path.join(self.workspace_root, rel_path)
    lang = detect_language_from_path(rel_path)
    if not lang or not RustParserFacade.supports_language(lang):
        skipped += 1
        continue
    project_langs.add(lang)
    try:
        module_path = self._infer_module_path_generic(rel_path, lang)
        file_instance_id = self._register_file_db(abs_path, module_path)       # 2 SQL
        if not force:
            current_mtime = os.path.getmtime(abs_path)
            latest_fv = self._get_file_version(file_instance_id)              # 1 SQL
            if latest_fv and abs(latest_fv["mtime"] - current_mtime) < 0.001:
                old_result = self._load_file_result_from_db(...)               # 3+ SQL（已 Rust 化）
                if old_result:
                    file_results[rel_path] = old_result
                    unchanged += 1
                    continue
    except OSError as e:
        failed += 1; failed_files.append(...)
        continue
    to_parse.append((i, rel_path, abs_path, lang, module_path, file_instance_id))
```

**性能特征**（460 文件基线）：
- 每文件 3 次 SQL round-trip（register 2 + version 1）
- 460 × 3 = 1380 次 round-trip
- 实测 0.44s（占总 refresh 2.57s 的 17%）
- 平均每文件 0.95ms

**优化目标**：
- 单事务 + 预处理语句：1380 round-trip → 1 事务 + 3 prepared statement × N 执行
- 预期 0.44s → 0.05s（~8-10x 提升）

## 2. Rust API 契约

### 2.1 batch_register_files

```rust
/// 批量注册文件到 file_instances 表，并查询最新 file_versions
///
/// 对应 Python `_build_multi_lang` register 阶段的 `_register_file_db` + `_get_file_version` 循环。
/// 单一读写连接 + 预处理语句 + 单事务，消除 N×3 次 round-trip。
///
/// 参数：
/// - codegraph_db_path: CodeGraph 数据库路径
/// - workspace_id: 工作区 ID
/// - files: 文件信息列表，每个元素为 dict：
///     {
///         "rel_path": String,      // 相对路径（用于 UNIQUE 索引）
///         "abs_path": String,      // 绝对路径（仅写入 DB，不读文件系统）
///         "module_path": String,   // 模块路径
///         "mtime": f64,            // 文件修改时间
///     }
/// - skip_version_lookup: bool，True 时跳过 file_versions 查询（对应 force=True）
///
/// 返回 dict：
/// {
///     "success": bool,
///     "error": String,             // 失败时的错误信息
///     "results": Vec<RegisterResult>,  // 每文件结果
/// }
///
/// RegisterResult:
/// {
///     "rel_path": String,
///     "file_instance_id": i64,
///     "version_id": Option<i64>,         // None 表示无版本或 skip_version_lookup=True
///     "version_mtime": Option<f64>,
///     "version_content_hash": Option<String>,
///     "version_total_lines": Option<i64>,
/// }
#[pyfunction]
fn batch_register_files(
    codegraph_db_path: &str,
    workspace_id: i64,
    files: Vec<Bound<PyDict>>,
    skip_version_lookup: bool,
) -> PyResult<PyObject>
```

**特性**：
- 写操作：BEGIN IMMEDIATE 事务，失败 ROLLBACK
- 复用 `open_readwrite` helper（与 `batch_save_file_versions` / `batch_save_symbols` 一致）
- `busy_timeout=5000`，WAL checkpoint 前置
- 预处理 3 条 SQL 语句，循环执行
- 失败不抛异常，返回 `{"success": False, "error": str(e)}`（fail-soft，与现有 Rust API 一致）

### 2.2 SQL 执行计划

```sql
-- 预处理语句 1：查询现有 file_instance
SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?
-- 预处理语句 2：更新现有 file_instance
UPDATE file_instances SET mtime = ?, module_path = ?, status = 'pending' WHERE id = ?
-- 预处理语句 3：插入新 file_instance
INSERT INTO file_instances
  (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
  VALUES (?, ?, ?, '', ?, 0, 0, 'pending', ?)
-- 预处理语句 4：查询最新版本（skip_version_lookup=False 时）
SELECT id, mtime, content_hash, total_lines FROM file_versions
  WHERE file_instance_id = ? AND is_current = 1
  ORDER BY version_num DESC LIMIT 1
```

**注意**：Python `_get_file_version` 查询 `ORDER BY version_num DESC LIMIT 1`，但 `is_current=1` 的版本就是最新版本。Rust 用 `is_current=1` 过滤更高效（有索引），且与 Python 行为一致（新版本写入时旧版本 `is_current` 设为 0）。

## 3. 行为契约

### 3.1 注册行为契约

| ID | 场景 | 输入 | 预期行为 |
|---|---|---|---|
| R1 | 新文件（DB 无记录） | rel_path 不在 file_instances | INSERT 新行，返回新 id；version_id=None |
| R2 | 已有文件（DB 有记录） | rel_path 在 file_instances | UPDATE mtime/module_path/status，返回现有 id |
| R3 | 多文件混合 | 3 新 + 2 旧 | 5 个结果，3 INSERT + 2 UPDATE |
| R4 | 空文件列表 | files=[] | success=True, results=[] |
| R5 | 重复 rel_path | 同一 rel_path 出现两次 | 第一次 INSERT，第二次 UPDATE（与 Python 逐文件调用一致） |
| R6 | workspace 隔离 | workspace_id=2 查询 workspace_id=1 的文件 | 不命中，走 INSERT（与 Python WHERE workspace_id=? 一致） |

### 3.2 版本查询行为契约

| ID | 场景 | skip_version_lookup | 预期行为 |
|---|---|---|---|
| V1 | 有版本 | False | version_id=Some(id), version_mtime/content_hash/total_lines 填充 |
| V2 | 无版本（首次注册） | False | version_id=None |
| V3 | skip=True | True | version_id=None（不执行 SQL） |
| V4 | is_current 切换 | False | 只返回 is_current=1 的版本（最新） |

### 3.3 事务与错误处理契约

| ID | 场景 | 预期行为 |
|---|---|---|
| T1 | 全部成功 | COMMIT，返回所有结果 |
| T2 | 中间 SQL 失败 | ROLLBACK，返回 `{"success": False, "error": ...}`，results=[] |
| T3 | 数据库锁超时 | busy_timeout=5000 后返回 `{"success": False, "error": "database is locked"}` |
| T4 | 数据库路径不存在 | 返回 `{"success": False, "error": "unable to open database file"}` |
| T5 | file_instances 表不存在 | 返回 `{"success": False, "error": "no such table: file_instances"}` |

### 3.4 数据一致性契约

| ID | 场景 | Python 行为 | Rust 行为 |
|---|---|---|---|
| C1 | mtime 精度 | `os.path.getmtime` 返回 float | Python 预计算 mtime 传入 Rust，精度一致 |
| C2 | rel_path 规范化 | `norm_path(os.path.relpath(...))` | Python 预计算 rel_path 传入 Rust，Rust 不做规范化 |
| C3 | abs_path 规范化 | `norm_path(abs_path)` | Python 预计算 abs_path 传入 Rust |
| C4 | status 默认值 | 'pending' | 'pending'（硬编码在 SQL 中） |
| C5 | current_content_hash | '' （空字符串） | '' （硬编码在 SQL 中） |

## 4. wire-production 接入方案

### 4.1 Python 调用方修改

`db/db_build.py:_build_multi_lang` register 阶段（1185-1223 行）重构为：

```python
# Phase 2-6-3: Rust 批量注册
t_register_start = time.perf_counter()

# 预计算文件信息（语言检测 + module_path 推断 + mtime）
files_to_register = []
for i, rel_path in enumerate(files, 1):
    abs_path = os.path.join(self.workspace_root, rel_path)
    lang = detect_language_from_path(rel_path)
    if not lang or not RustParserFacade.supports_language(lang):
        skipped += 1
        continue
    project_langs.add(lang)
    try:
        module_path = self._infer_module_path_generic(rel_path, lang)
        mtime = os.path.getmtime(abs_path)
        files_to_register.append({
            "rel_path": rel_path,
            "abs_path": norm_path(abs_path),
            "module_path": module_path,
            "mtime": mtime,
        })
    except OSError as e:
        failed += 1
        failed_files.append((rel_path, f"OSError: {e}"))
        cprint(f"  ⚠ 跳过不可访问文件: {rel_path} ({e})", "yellow")
        continue

# Rust 批量注册
if not self.is_feature_rolled_back("rust_batch_register_files"):
    register_result = self._batch_register_files_via_rust(files_to_register, force=force)
    if register_result is not None:
        # Rust 成功：处理结果
        for r in register_result["results"]:
            rel_path = r["rel_path"]
            file_instance_id = r["file_instance_id"]
            abs_path = os.path.join(self.workspace_root, rel_path)
            lang = detect_language_from_path(rel_path)
            module_path = self._infer_module_path_generic(rel_path, lang)

            if not force and r["version_id"] is not None:
                current_mtime = os.path.getmtime(abs_path)
                if abs(r["version_mtime"] - current_mtime) < 0.001:
                    old_result = self._load_file_result_from_db(...)
                    if old_result:
                        file_results[rel_path] = old_result
                        unchanged += 1
                        continue
            to_parse.append((i, rel_path, abs_path, lang, module_path, file_instance_id))
    else:
        # Rust 失败 → 降级 Python 路径（fail-soft）
        # 保留原 Python 循环
        ...
else:
    # rollback_config 置为 1 → Python 路径
    # 保留原 Python 循环
    ...

t_register = time.perf_counter() - t_register_start
```

### 4.2 rollback_config 登记

```bash
cw rollback register \
    --task-id T-1785210025924-48e66da3 \
    --feature rust_batch_register_files \
    --phase 2 \
    --production-entry "db/db_build.py:_build_multi_lang register stage (Rust short-circuit via callwarden_core.batch_register_files)" \
    --rollback-entry "db/db_build.py:_register_file_db + _get_file_version (Python per-file loop)" \
    --window "2026-08-31T23:59:59Z"
```

## 5. 差分测试矩阵

### 5.1 行为差分（D1-D10）

| ID | 场景 | Python 输入 | Rust 输入 | 验证 |
|---|---|---|---|---|
| D1 | 单新文件 | `[(rel, abs, mod, mtime)]` | 同 | file_instance_id 一致（新 INSERT），version_id=None |
| D2 | 单已有文件 | 同 | 同 | file_instance_id 一致（UPDATE），version_id 一致 |
| D3 | 混合 5 文件 | 3 新 + 2 旧 | 同 | 5 个 file_instance_id，3 新 + 2 已有 |
| D4 | 空列表 | `[]` | `[]` | results=[] |
| D5 | skip_version_lookup=False | 有版本文件 | 同 | version_id/mtime/content_hash/total_lines 一致 |
| D6 | skip_version_lookup=True | 有版本文件 | 同 | version_id=None |
| D7 | workspace 隔离 | workspace_id=2 | workspace_id=2 | 不读 workspace_id=1 的文件 |
| D8 | 重复 rel_path | `[a, a]` | `[a, a]` | 第一个 INSERT，第二个 UPDATE，file_instance_id 一致 |
| D9 | mtime 精度 | mtime=1234567890.123 | 同 | DB 中 mtime 一致 |
| D10 | module_path 空 | module_path="" | 同 | DB 中 module_path="" |

### 5.2 数据一致性差分

```python
def test_differential_register(tmp_path):
    """Python 路径与 Rust 路径在同一组文件上产生完全一致的 DB 状态"""
    # 1. 准备两个相同 DB 副本
    # 2. Python 路径：逐文件 _register_file_db + _get_file_version
    # 3. Rust 路径：batch_register_files
    # 4. 对比 file_instances 表：id/workspace_id/rel_path/abs_path/mtime/module_path/status
    # 5. 对比 file_versions 表：无变更（register 不写 file_versions）
```

## 6. 性能压测基线

### 6.1 当前基线（2026-07-28，优化前）

| 指标 | 值 | 说明 |
|---|---|---|
| 文件总数 | 460 | 项目实际文件数 |
| register 阶段耗时 | 0.44s | 460 × 3 SQL = 1380 round-trip |
| 平均每文件 | 0.95ms | 包含 Python↔C 跨语言开销 |
| 占总 refresh 比例 | 17% | 总 2.57s |
| 硬件 | Windows | 开发机 |

### 6.2 目标

| 指标 | 目标 | 改善倍数 |
|---|---|---|
| register 阶段耗时 | < 0.10s | 4.4x+ |
| 平均每文件 | < 0.2ms | 4.75x+ |
| 占总 refresh 比例 | < 5% | 从 17% 降到 5% |

### 6.3 验证方法

```powershell
# 优化前基线（已记录）
python cw.py --refresh-all  # 记录 register 阶段耗时

# 优化后验证
python cw.py --refresh-all  # 对比 register 阶段耗时

# 差分测试
pytest tests/test_phase2_6_3_batch_register_diff.py -v
```

## 7. 验收标准

| 验收项 | 标准 |
|---|---|
| Rust 单元测试 | batch_register_files 所有边界 case 通过 |
| Python 差分测试 | D1-D10 全部通过 |
| 数据一致性 | Python 与 Rust 路径产生完全一致的 DB 状态 |
| 性能提升 | register 阶段耗时降低 ≥ 4x |
| rollback_config | 已登记，rollback_flag=0（normal） |
| 文档同步 | migration-manifest.md 更新 Phase 2-6-3 状态 |
| 无回归 | 现有测试套件不破坏 |

## 8. 风险与注意事项

1. **Python 预计算开销**：语言检测 + module_path 推断 + mtime 读取仍在 Python 中执行，无法完全消除 Python 开销。这些是文件系统操作，Rust 化收益不大（且需要 Rust 端复刻 `_infer_module_path_generic` 逻辑）。
2. **fail-soft 路径**：Rust 失败时降级到 Python 循环，需要保留原 Python 代码作为 rollback_entry。
3. **事务隔离**：Rust BEGIN IMMEDIATE 会持有写锁，若 MCP Server 同时写入会等待 busy_timeout。与现有 `batch_save_file_versions` 行为一致。
4. **is_current vs ORDER BY**：Python `_get_file_version` 用 `ORDER BY version_num DESC LIMIT 1`，Rust 用 `is_current=1`。两者在正常情况下等价（新版本写入时旧版本 is_current 设为 0），但若历史数据有 is_current 异常，可能不一致。差分测试需覆盖此场景。
5. **返回值规模**：460 文件 × 6 字段 = 2760 个值，PyO3 转换开销可接受（< 1ms）。
