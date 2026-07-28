# Phase 2 子任务 1 契约：CAS→CodeGraph Merge PyO3 暴露与差分

> 本文件是全量 Rust 迁移自举计划 Phase 2 第一个功能子任务的 contract 交付物。
>
> 关联：
> - 总计划：[rust-full-migration-self-bootstrap-plan.md](rust-full-migration-self-bootstrap-plan.md) Phase 2 §1
> - 上游契约：[phase1-replicator-snapshot-contract.md](phase1-replicator-snapshot-contract.md)（Replicator + SnapshotManager 只读查询）
> - 真相源：[db/db_cas_merge.py](../../db/db_cas_merge.py) / [rust_ext/src/daemon/cas_merge.rs](../../rust_ext/src/daemon/cas_merge.rs) / [server/replicator.py](../../server/replicator.py)

## 1. 范围

| 项 | 说明 |
|---|---|
| **目标** | 将 Rust 端 `daemon/cas_merge.rs` 的 `merge_cas_to_codegraph` + `init_codegraph_schema` 通过 PyO3 暴露给 Python，与 Python `db_cas_merge.py` 路径建立 ✅(behavioral) 差分。让 `server/replicator.py:daemon_handle_refresh` 可选短路走 Rust merge。 |
| **背景**：Phase 2-1 范围决策 | 调研发现 Python 端有**两条独立**写入路径：<br>1) `db_build.py` 批量化路径（CLI `cw build`/`cw refresh`，executemany）<br>2) `db_cas_merge.py` 逐条 execute 路径（daemon 推送）<br>Rust `daemon/cas_merge.rs` 只对应第 2 条路径，且未通过 PyO3 暴露。**本子任务只暴露 cas_merge（最小侵入）**，db_build 批量化迁移留待 Phase 2-2。 |
| **不在本子任务** | 1) `db_build.py:_save_symbols_for_version` / `_build_call_graph_multi_lang` 的批量化迁移（Phase 2-2）<br>2) `file_versions` + `file_symbol_versions` + `call_versions` 历史版本写入（Phase 2-3）<br>3) `build_graph_from_c_files` 内存图构建的扩展（保持 C 专用）<br>4) daemon_handle_refresh 主流程切换（Phase 5 wire-production）<br>5) FTS5 显式 `_rebuild_and_enable_fts`（保持触发器自动触发） |

## 2. 现状盘点（来自调研）

### 2.1 Python `db_cas_merge.py` 入口

| 方法 | 类型 | 说明 |
|---|---|---|
| `merge_cas_to_codegraph(...)` | 写 | CAS→CodeGraph DB 合并主入口 |
| `_ensure_workspace_row(...)` | 写（私有） | INSERT OR IGNORE `workspaces` |
| `_upsert_file_records(...)` | 写（私有） | UPSERT `file_contents` + `file_instances` |
| `_replace_symbols_and_calls(...)` | 写（私有） | DELETE 旧 + INSERT 新 symbols/calls（**逐条 execute**） |

#### 写入语义
- symbols：**逐条 execute()**（非 executemany），INSERT OR IGNORE `symbol_contents` 后 INSERT `symbols`
- calls：**逐条 execute()**，单文件内 caller_id 通过 `name→id` 字典 resolve
- **不写入** file_versions / file_symbol_versions / call_versions 历史版本
- **不做** 跨文件 resolve（`cas_raw_calls` 只含单文件内调用）
- FTS5：依赖 schema v31 触发器自动触发
- 事务边界：`codegraph_conn.commit()` 显式提交（[db_cas_merge.py:385](file:///c:/git_work/callwarden/db/db_cas_merge.py)）

### 2.2 Rust `daemon/cas_merge.rs` 实现

| 函数 | 类型 | 说明 | 是否暴露 |
|---|---|---|---|
| `merge_cas_to_codegraph(cas_conn, codegraph_conn, cas_key, workspace_id, rel_path, abs_path, content_hash, language, workspace_root_path)` | 写 | CAS→CodeGraph DB 合并主入口 | 🔴 未通过 PyO3 暴露 |
| `init_codegraph_schema(conn)` | 写 | 幂等 schema 初始化（CREATE TABLE IF NOT EXISTS） | 🔴 未暴露 |
| `replace_symbols_and_calls(...)` | 写（私有） | DELETE 旧 + INSERT 新 symbols/calls | 🟡 内部函数 |
| `resolve_callee(...)` | 只读（私有） | 4 策略 resolve + ORDER BY s.id ASC LIMIT 1 | 🟡 内部函数 |
| `resolve_unresolved_calls_in_workspace(...)` | 写（私有） | workspace 级回扫 pass，merge 完成后扫描 `callee_id=0` 的 calls 重新 resolve | 🟡 内部函数 |
| `upsert_manifest(...)` | 写（私有） | UPSERT workspace_manifests | 🟡 内部函数 |

#### Rust 相对 Python 的额外能力
- `resolve_callee`：4 策略 + **ORDER BY s.id ASC LIMIT 1**（P1-2 v2 修复，消除同名符号歧义；Python `db_cas_merge.py` 无此修复）
- `resolve_unresolved_calls_in_workspace`：workspace 级回扫 pass，merge 完成后扫描所有 `callee_id=0` 的 calls 重新 resolve（Python 无此能力）
- `init_codegraph_schema`：幂等 schema 初始化
- 14 个单元测试覆盖 cas_miss / idempotent / fresh DB / inbound edge cleanup / workspace backfill / file_size correctness / ORDER BY stability / E2E fresh DB

#### Rust 实现的局限
- symbols / calls 仍是**逐条 execute()**（与 Python 一致，未批量化）
- 不写入 file_versions / file_symbol_versions / call_versions（与 Python 一致）

### 2.3 调用方（Python）

| 调用方 | 路径 | 替换策略 |
|---|---|---|
| `server/replicator.py:daemon_handle_refresh` | [replicator.py:310](file:///c:/git_work/callwarden/server/replicator.py) | 调用 Python `merge_cas_to_codegraph`，建议改为可选走 Rust |

### 2.4 调用方（Rust daemon 内部）

| 调用方 | 路径 |
|---|---|
| `rust_ext/src/daemon/workspace.rs::handle_workspace_file_refresh` | 在 `daemon_handle_refresh` 返回后、`replicator.replicate` 调用前，调用 `cas_merge::merge_cas_to_codegraph` |
| `rust_ext/src/daemon/replicator.rs` | 不直接调用 cas_merge，由 workspace.rs 编排 |

## 3. API 契约（本子任务暴露清单）

### 3.1 CAS→CodeGraph Merge 主入口（新建 `rust_ext/src/cas_merge_query.rs`）

> 命名说明：模块叫 `cas_merge_query` 是为了与既有 `cas_query.rs`（只读查询）保持风格一致。暴露的是**写操作**，但通过 PyO3 调用方传连接对象，写锁由调用方持有。

```python
def cas_merge_to_codegraph(
    cas_db_path: str,
    codegraph_db_path: str,
    cas_key: str,
    workspace_id: int,
    rel_path: str,
    abs_path: str,
    content_hash: str,
    language: str,
    workspace_root_path: str = "",
) -> dict:
    """CAS→CodeGraph DB 合并

    与 Python `db_cas_merge.merge_cas_to_codegraph` 行为一致：
    - 从 cas_db_path 读取 cas_key 对应的 ParseResult（symbols + calls + imports）
    - 写入 codegraph_db_path：
      - INSERT OR IGNORE workspaces
      - UPSERT file_contents + file_instances（status='parsed'）
      - DELETE 旧 symbols + calls WHERE file_instance_id
      - INSERT OR IGNORE symbol_contents
      - INSERT symbols
      - INSERT calls（含 4 策略 caller_id resolve + ORDER BY s.id ASC LIMIT 1）
      - UPSERT workspace_manifests（is_dirty=1, file_size 来自 cas_file_cache）
    - workspace 级回扫：resolve_unresolved_calls_in_workspace
    - 事务边界：BEGIN IMMEDIATE → 全部 SQL → COMMIT
    - 失败：返回 dict {"success": False, "error": str(e)}，不抛异常（与 Python 行为一致）

    Returns:
        {
            "success": True/False,
            "symbols_inserted": usize,
            "calls_inserted": usize,
            "calls_resolved": usize,  # callee_id != 0 的 calls 数
            "error": Optional[str],
        }
    """
```

### 3.2 Schema 初始化（可选，供 fresh DB 场景使用）

```python
def cas_merge_init_schema(codegraph_db_path: str) -> bool:
    """幂等初始化 CodeGraph DB schema

    与 Python `db_base.init_schema` 行为一致：
    - CREATE TABLE IF NOT EXISTS（workspaces / file_instances / symbols / calls / symbol_contents / file_symbol_versions / call_versions / workspace_manifests / ...）
    - CREATE INDEX IF NOT EXISTS（幂等）
    - 不修改 schema_version（保持 Python 端管理）

    Returns:
        True 表示成功初始化（或已存在）
        False 表示 schema 初始化失败（db_path 不可写等）
    """
```

### 3.3 不暴露的 API

| API | 原因 |
|---|---|
| `replace_symbols_and_calls` | 内部函数，由 `cas_merge_to_codegraph` 编排 |
| `resolve_callee` | 内部函数 |
| `resolve_unresolved_calls_in_workspace` | 内部函数，由 `cas_merge_to_codegraph` 编排 |
| `upsert_manifest` | 内部函数，由 `cas_merge_to_codegraph` 编排 |
| `db_build._save_symbols_for_version` / `_build_call_graph_multi_lang` | Phase 2-2 范围 |
| `file_versions` / `file_symbol_versions` / `call_versions` 写入 | Phase 2-3 范围 |

## 4. 行为契约（Python ↔ Rust 必须一致）

### 4.1 cas_merge_to_codegraph（M1-M8）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| M1 | CAS miss（cas_key 不在 cas_file_cache） | 抛 `ProtocolError(code="cas_miss")` | 返回 `{"success": False, "error": "cas_miss"}` | 两端均失败，断言 error 字段一致 |
| M2 | CAS hit + fresh CodeGraph DB（无表） | 抛 `sqlite3.OperationalError: no such table` | `init_codegraph_schema` 幂等创建后正常合并 | Rust 自动建表，Python 失败（除非显式 init_schema） |
| M3 | CAS hit + 已有 CodeGraph DB | 成功合并，返回 symbols/calls 数 | 成功合并，返回 dict | 两端 symbols/calls 数一致 |
| M4 | 重复 merge 同一 cas_key（幂等性） | 第二次 DELETE+INSERT，symbols/calls 数不变 | 同上 | 两端幂等性一致 |
| M5 | merge 后入站调用边清理（inbound edge cleanup） | INSERT calls 时直接写 `callee_id=0`，不做任何 resolve | INSERT calls 时立即调用 `resolve_callee`（4 策略 + ORDER BY s.id ASC），命中本文件或跨文件 callee；merge 完成后 `resolve_unresolved_calls_in_workspace` workspace 级回扫 | **预期差异**：Rust 多 resolve N 个 calls（N = 命中的 calls 数，含本文件 + 跨文件），Python 始终为 0 |
| M6 | workspace 不存在 | INSERT OR IGNORE 自动创建 | 同上 | 两端 workspace_id 一致 |
| M7 | file_size 字段来源 | `len(canonical_bytes)` | `cas_file_cache.file_size` | 两端数值一致（同一 canonical_bytes） |
| M8 | callee resolve 行为差异（单文件同名符号） | INSERT calls 时直接写 `callee_id=0`，不 resolve | INSERT 时立即 `resolve_callee`，本文件内按 qualified_name/name 短名匹配 + ORDER BY s.id ASC LIMIT 1 | **预期差异**：Rust resolve 本文件 calls（callee_id != 0），Python 不 resolve（callee_id = 0）；两端 calls 总数一致 |

### 4.2 cas_merge_init_schema（S1-S2）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| S1 | fresh DB（无任何表） | `init_schema` 创建全部表 | `init_codegraph_schema` 创建核心表（不含 schema_version 等） | 两端核心表存在性一致 |
| S2 | 已有 schema 的 DB | 幂等，不报错 | 幂等，不报错 | 两端都成功 |

### 4.3 预期差异清单（需在差分测试中显式标注）

| 字段 | Python | Rust | 处理策略 |
|---|---|---|---|
| M5：callee resolve（INSERT 时） | 不 resolve，`callee_id=0` | INSERT 时立即 `resolve_callee`，命中本文件或跨文件 callee | **差分测试分两组**：M5a（单文件，Rust resolve 本文件 calls，断言 rust_resolved == 本文件 calls 数，py_resolved == 0）；M5b（多文件，Rust resolve 本文件 + 跨文件 calls，断言 rust_resolved == 本文件 + 跨文件 calls 数，py_resolved == 0） |
| M5：inbound edge cleanup（workspace 回扫） | 不回扫 | `resolve_unresolved_calls_in_workspace` workspace 级回扫，resolve `callee_id=0` calls | 与 M5 INSERT 时 resolve 合并验证；M5b 构造 A→B 跨文件 calls，验证 Rust resolve N 个 |
| M8：ORDER BY 稳定性 | 不 resolve，无 ORDER BY 问题 | `s.id ASC LIMIT 1`，行为稳定 | **差分测试用单文件场景**：断言两端 calls 总数一致，rust_resolved == 本文件 calls 数，py_resolved == 0；Rust 行为更稳定可预测 |
| file_versions 历史版本 | 不写入 | 不写入 | 两端一致 ✅ |
| workspace_manifests.is_dirty | True | True | 两端一致 ✅ |
| workspace_manifests.file_size | `len(canonical_bytes)` | `cas_file_cache.file_size` | **需对齐**：差分测试用相同 canonical_bytes，断言 file_size 一致 |

## 5. 事务边界（AGENTS.md 规则 6）

### 5.1 写策略

`cas_merge_to_codegraph` 是**写操作**，必须满足：

1. **持有写锁**：BEGIN IMMEDIATE → 全部 SQL → COMMIT
2. **busy_timeout=5000**：写锁冲突时最多等 5 秒
3. **不与 Python 写入并发**：Python 端 `db_cas_merge.merge_cas_to_codegraph` 也持有写锁，两者并发会撞锁
4. **回滚语义**：失败时 ROLLBACK，不留半成品

### 5.2 与既有路径的关系

- **Python `db_cas_merge.merge_cas_to_codegraph`**：保留为 rollback 入口
- **Rust `cas_merge_to_codegraph`**：作为可选生产入口，通过 `rollback_flag` 切换
- **Rust daemon 内部 `daemon/cas_merge.rs`**：保持不变，不通过 PyO3 暴露的内部函数继续在 daemon 内部使用

### 5.3 调用方决策

`server/replicator.py:daemon_handle_refresh` 应在 wire-production 步骤中：
1. 读取 rollback_flag（通过 `is_feature_rolled_back("rust_cas_merge")`）
2. flag=0 → 走 Rust `cas_merge_to_codegraph`
3. flag=1 → 走 Python `db_cas_merge.merge_cas_to_codegraph`

## 6. 实现计划

### 6.1 Rust 端

**新文件 `rust_ext/src/cas_merge_query.rs`**：

```rust
//! Phase 2-1: CAS→CodeGraph Merge PyO3 暴露层
//!
//! 对应 Python `db/db_cas_merge.py::merge_cas_to_codegraph`：
//! - `cas_merge_to_codegraph` —— CAS→CodeGraph DB 合并主入口
//! - `cas_merge_init_schema` —— 幂等 schema 初始化

use pyo3::prelude::*;
use rusqlite::Connection;
use std::path::Path;

#[pyfunction]
#[pyo3(signature = (cas_db_path, codegraph_db_path, cas_key, workspace_id, rel_path, abs_path, content_hash, language, workspace_root_path=""))]
pub fn cas_merge_to_codegraph(
    cas_db_path: &str,
    codegraph_db_path: &str,
    cas_key: &str,
    workspace_id: i64,
    rel_path: &str,
    abs_path: &str,
    content_hash: &str,
    language: &str,
    workspace_root_path: &str,
) -> PyResult<PyObject> {
    // 1. open cas_conn (readonly) + codegraph_conn (readwrite)
    // 2. busy_timeout=5000
    // 3. BEGIN IMMEDIATE
    // 4. 调用 daemon::cas_merge::merge_cas_to_codegraph
    // 5. COMMIT
    // 6. 调用 resolve_unresolved_calls_in_workspace（在事务外或新事务）
    // 7. 返回 dict {success, symbols_inserted, calls_inserted, calls_resolved, error}
}

#[pyfunction]
pub fn cas_merge_init_schema(codegraph_db_path: &str) -> bool {
    // 1. open codegraph_conn (readwrite)
    // 2. busy_timeout=5000
    // 3. 调用 daemon::cas_merge::init_codegraph_schema
    // 4. 返回 true/false
}

pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(cas_merge_to_codegraph, m)?)?;
    m.add_function(wrap_pyfunction!(cas_merge_init_schema, m)?)?;
    Ok(())
}
```

### 6.2 PyO3 注册（`rust_ext/src/lib.rs`）

```rust
mod cas_merge_query;
// ...
m.add_function(wrap_pyfunction!(cas_merge_query::cas_merge_to_codegraph, m)?)?;
m.add_function(wrap_pyfunction!(cas_merge_query::cas_merge_init_schema, m)?)?;
```

### 6.3 差分测试（`tests/test_phase2_behavioral_diff.py` 新建）

- `TestCasMergeDiff`：M1-M8（cas_merge_to_codegraph 差分）
- `TestCasMergeInitSchemaDiff`：S1-S2（schema 初始化差分）

### 6.4 wire-production（`db/db_rollback_config.py` 登记）

```powershell
cw rollback register `
  --task-id T-1785167249689-453c6ca1 `
  --feature rust_cas_merge `
  --phase 2 `
  --production-entry "rust_ext/src/cas_merge_query.rs::cas_merge_to_codegraph + cas_merge_init_schema" `
  --rollback-entry "db/db_cas_merge.py:merge_cas_to_codegraph + db/db_base.py:init_schema" `
  --window 2026-12-31T00:00:00
```

## 7. Schema 信息（真相源）

### 7.1 CodeGraph DB schema（[db/schema.py](../../db/schema.py)）

cas_merge 涉及的表：
- `workspaces`（id, name, root_path, owner, ...）
- `file_instances`（id, workspace_id, rel_path, status, content_hash, module_path, ...）
- `file_contents`（content_hash PRIMARY KEY, content BLOB, total_lines, language, ...）
- `symbols`（id, file_instance_id, kind, name, qualified_name, module_path, start_line, end_line, depth, content, signature, visibility, symbol_hash, has_comment, local_id, lexical_parent_local_id, ...）
- `calls`（caller_id, callee_id, callee_name, call_line, is_cross_file, caller_name, callee_module, ...）
- `symbol_contents`（content_hash PRIMARY KEY, content TEXT, has_comment, comment_content, ...）
- `workspace_manifests`（workspace_id, rel_path, content_hash, cas_key, file_size, is_dirty, raw_hash, source_encoding, updated_at, ...）

### 7.2 CAS DB schema

cas_merge 读取的表：
- `cas_file_cache`（cas_key PRIMARY KEY, content BLOB, parse_result BLOB, state, file_size, language, content_hash, total_lines, created_at, ...）

### 7.3 字段映射

| Python 字段 | Rust 字段 | 说明 |
|---|---|---|
| `file_instances.status='parsed'` | 同 | 两端一致 |
| `file_instances.module_path` | `module_path_from_rel` | Rust 是简化版（缺少 src/lib 去除），**M6 差分测试需对齐** |
| `symbols.depth=-1` | 同 | 两端一致（pending，后续 Kahn BFS 更新） |
| `symbols.comment_status='pending'` | 同 | 两端一致 |
| `calls.callee_id=0` | 同 | 未 resolve 的 calls |
| `workspace_manifests.is_dirty=1` | 同 | daemon merge 路径标记 dirty |
| `workspace_manifests.file_size` | `cas_file_cache.file_size` | **M7 差分断言**：两端应一致 |

## 8. 验收标准

- [ ] `cargo check --manifest-path rust_ext/Cargo.toml` 通过
- [ ] `maturin build -i C:\Python314\python.exe --release` 生成 cp314 wheel
- [ ] `pip install` 后 `cw server --check-imports` 通过
- [ ] `pytest tests/test_phase2_behavioral_diff.py -v` 全部通过（含 M1-M8 + S1-S2）
- [ ] `pytest tests/test_phase3_cas.py tests/test_phase3_cas_protocol.py -v` 不破坏（现有回归测试通过）
- [ ] `pytest tests/test_phase1_behavioral_diff.py -v` 不破坏（Phase 1 差分测试通过）
- [ ] Phase 2-1 行升级为 `✅(behavioral)`

## 9. 风险与注意事项

1. **不切换默认路径**：Python `db_cas_merge.merge_cas_to_codegraph` 仍主导。Rust API 仅作为可选短路，通过 rollback_flag 切换。
2. **预期差异 M5/M8 需显式标注**：M5（inbound edge cleanup）和 M8（ORDER BY 稳定性）是 Rust 相对 Python 的行为差异。差分测试需分两组：M5a/M8a（无差异场景）+ M5b/M8b（Rust 行为更优场景）。
3. **file_size 字段来源差异**：Python `len(canonical_bytes)` vs Rust `cas_file_cache.file_size`。差分测试用相同 canonical_bytes，断言两端 file_size 一致。若 cas_file_cache.file_size 与 len(canonical_bytes) 不一致，需在 Rust 端改为读取 canonical_bytes 长度。
4. **module_path 差异**：Python `_infer_module_path_generic`（含 src/lib 去除）vs Rust `module_path_from_rel`（简化版）。M6 差分测试用不带 src/lib 前缀的 rel_path 避免差异。Phase 2-2 应在 Rust 端补全 module_path 推断逻辑。
5. **WAL 模式与只读连接**：cas_db_path 用 READONLY 连接读取 cas_file_cache，codegraph_db_path 用 READWRITE 连接写入。WAL checkpoint 在 codegraph_db_path 写入前执行。
6. **daemon 内部路径不变**：Rust daemon 内部的 `daemon/cas_merge.rs` 继续按现有逻辑运行，不受本子任务影响。
7. **db_build 路径不变**：本子任务不修改 `db_build.py` 的 `_save_symbols_for_version` / `_build_call_graph_multi_lang`。Phase 2-2 处理。
8. **历史版本不写入**：与 Python `db_cas_merge.py` 一致，不写入 file_versions / file_symbol_versions / call_versions。Phase 2-3 处理。
9. **busy_timeout=5000**：写锁冲突时最多等 5 秒，与 Python `db_cas_merge.py` 一致。
10. **回滚窗口**：rollback_window_until=2026-12-31，Phase 7 删除 rollback_entry。
