# Phase 2-3 契约：调用边解析、resolve 与批量写入（batch_resolve_and_save_calls）

> **范围**：将 Python `db/db_build.py::_build_call_graph_multi_lang` 的 5 策略 resolve + 批量写入
> calls/call_versions 的核心路径通过 PyO3 暴露给 Python，并通过 Python↔Rust 行为差分测试
> 验证一致性。
>
> **不在范围**：
> - CSR/GraphStore 内存查询能力（已由 `GraphStore.load_from_sqlite` 提供，本契约不涉及）
> - 跨文件 resolve 的"workspace 级回扫 pass"（`resolve_unresolved_calls_in_workspace`，由
>   `daemon/cas_merge.rs` 提供，本契约不重复实现）
> - 增量 only_files 模式（L8 优化，本契约只覆盖全量模式）
> - `_from_db` 短路（消除写放大优化，本契约只覆盖需要 resolve+写入的路径）

## 1. Python 真相源盘点

### 1.1 入口函数

`db/db_build.py::_build_call_graph_multi_lang(file_results, only_files=None)`，行 2030-2445。

### 1.2 调用方

- `db_build.py:1643` `refresh_file` 全量流程（`only_files={rel_path}` 增量模式）
- `db_build.py:3521/3573` `_refresh_file` 增量刷新入口

### 1.3 5 策略 resolve 概览

| 策略 | 触发条件 | 行为 |
|---|---|---|
| 1 精确匹配 | `callee_module` 非空 | 拼 `{callee_module}.{callee_name}` 查 `all_symbols_map` |
| 2 import 映射 | 策略 1 失败 + `callee_module` 在 `file_imports[rel_path]` 中 | 用 import 完整路径的末段组合 + `callee_name` 查 `all_symbols_map`；失败时用 `suffix_index` 查后缀 |
| 3 简名唯一匹配 | 策略 1/2 失败 + `callee_name` 在 `name_index` 中 | 唯一候选直接取；多候选优先同文件（`file_local_qname`），其次 `callee_module` 匹配父级 |
| 4 同文件简名 | 策略 3 失败 + `callee_name` 在 `file_symbols[rel_path]` 中 | 取 `file_local_qname[rel_path][callee_name]` |
| 4.5 HCL 多段 name | 策略 4 失败 + `callee_name` 含 "." + 在 `name_to_qname` 中 | 唯一候选直接取；多候选优先同文件 |
| 5 external_symbols | 上述全部失败 | `callee_module` 非空查 `ext_by_qname`；为空查 `ext_by_name` 唯一候选 |

### 1.4 索引构建

- `all_symbols_map: Dict[str, {"file": str, "symbol": Dict}]` — qname → 符号元数据
- `name_index: Dict[str, List[str]]` — simple_name → qname 列表
- `name_to_qname: Dict[str, List[str]]` — symbol.name → qname 列表（含 "."）
- `file_symbols: Dict[str, Set[str]]` — rel_path → simple_name 集合
- `file_local_qname: Dict[str, Dict[str, str]]` — rel_path → (simple_name → qname)
- `suffix_index: Dict[str, List[str]]` — 后缀（含前导点）→ qname 列表
- `ext_by_qname: Dict[str, Dict]` — qname → external symbol
- `ext_by_name: Dict[str, List[Dict]]` — symbol_name → external symbol 列表
- `qname_id_map: Dict[str, int]` — qname → symbol_id（项目符号正 id，外部符号负 id）
- `file_sym_id_map: Dict[int, Dict[str, int]]` — file_instance_id → (name → symbol_id)
- `file_imports: Dict[str, Dict[str, str]]` — rel_path → (alias/module → full_module_path)

### 1.5 SQL 写入步骤

1. **DELETE** calls where caller_id IN (SELECT id FROM symbols WHERE file_instance_id IN changed_file_instance_ids) — 分批 500
2. **executemany INSERT INTO calls** — 10 列：caller_id, caller_name, caller_module, callee_name, callee_module, callee_qualified, callee_file, callee_id, call_line, is_cross_file
3. **executemany INSERT INTO call_versions** — 9 列：file_version_id, caller_qualified, caller_hash, callee_name, callee_module, callee_qualified, callee_file, call_line, is_cross_file

### 1.6 caller_id 多级 fallback

```python
if caller_qualified in qname_id_map:
    caller_id = qname_id_map[caller_qualified]
elif caller_name_raw in qname_id_map:
    caller_id = qname_id_map[caller_name_raw]
else:
    # 取末段（支持 :: . # 分隔符）
    simple_name = caller_name_raw.rsplit(sep, 1)[-1]
    caller_id = file_sym_id_map[fi_id].get(simple_name, 0)
if caller_id == 0:
    continue  # 跳过该 call
```

### 1.7 fn_hash_map 构建（caller_hash）

```python
fn_hash_map = result.get("fn_hash_map")
if fn_hash_map is None:
    fn_hash_map = {}
    for sym in result["symbols"]:
        if sym["kind"] in ("fn", "test_fn") and "content_hash" in sym:
            fn_hash_map[sym["qualified_name"]] = sym["content_hash"]
    for inline_mod in result["inline_modules"]:
        for sym in inline_mod["symbols"]:
            if sym["kind"] in ("fn", "test_fn") and "content_hash" in sym:
                fn_hash_map[sym["qualified_name"]] = sym["content_hash"]

# caller_qname 推导
caller_qualified = call.get("caller_qualified", "")
if not caller_qualified and call.get("caller_name"):
    caller_qualified = f"{mod_path}::{call['caller_name']}"
caller_hash = fn_hash_map.get(caller_qualified, "")
```

## 2. API 契约

### 2.1 主函数：`batch_resolve_and_save_calls`

```rust
#[pyfunction]
#[pyo3(signature = (
    codegraph_db_path,
    workspace_id,
    file_results,           // List[Dict] - 当前需要 resolve+写入的文件解析结果
    all_symbols,             // List[Dict] - 全局符号列表（来自 DB 或 Python 端预加载）
    external_symbols,        // List[Dict] - 全局外部符号列表（可选，空则跳过策略 5）
    changed_file_instance_ids, // List[int] - 需要删除旧 calls 的文件实例 id 列表
))]
pub fn batch_resolve_and_save_calls<'py>(
    py: Python<'py>,
    codegraph_db_path: &str,
    workspace_id: i64,
    file_results: Vec<Bound<'py, PyDict>>,
    all_symbols: Vec<Bound<'py, PyDict>>,
    external_symbols: Vec<Bound<'py, PyDict>>,
    changed_file_instance_ids: Vec<i64>,
) -> PyResult<Bound<'py, PyDict>>
```

**返回 dict**：

```python
{
    "success": bool,
    "total_calls": int,                # 总 raw_calls 数（含 caller_id=0 被跳过的）
    "resolved_count": int,             # callee_qname 非空的 call 数
    "calls_inserted": int,             # calls 表 INSERT 行数
    "call_versions_inserted": int,     # call_versions 表 INSERT 行数
    "old_calls_deleted": int,          # DELETE calls 命中行数
    "files_processed": int,            # 处理的文件数
    "error": Optional[str],
}
```

### 2.2 输入字段约束

**`file_results` 中每个 dict 必须含**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `rel_path` | str | 文件相对路径 |
| `file_instance_id` | int | 文件实例 id |
| `file_version_id` | int | 文件版本 id |
| `module_path` | str | 文件模块路径 |
| `raw_calls` | List[Dict] | 原始调用列表 |
| `imports` | List[str \| Dict] | 文件 import 列表 |
| `symbols` | List[Dict] | 文件符号列表（用于构建 fn_hash_map，可空） |
| `inline_modules` | List[Dict] | 内联模块（用于 fn_hash_map，可空） |
| `fn_hash_map` | Dict[str, str] | 预提取的 qname → content_hash（可选，None 时从 symbols 构建） |

**`raw_calls` 中每个 dict 必须含**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `caller_name` | str | 调用方名称 |
| `caller_qualified` | str | 调用方全限定名 |
| `caller_module` | str | 调用方模块 |
| `callee_name` | str | 被调用方简名 |
| `callee_module` | str | 被调用方模块 |
| `call_line` | int | 调用行号 |

**`all_symbols` 中每个 dict 必须含**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | symbols.id |
| `name` | str | 符号简名（symbol.name） |
| `qualified_name` | str | 符号全限定名 |
| `kind` | str | 符号类型 |
| `file_instance_id` | int | 所属文件实例 id |
| `rel_path` | str | 所属文件相对路径 |

**`external_symbols` 中每个 dict 必须含**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | int | external_symbols.id |
| `symbol_name` | str | 外部符号简名 |
| `qualified_name` | str | 外部符号全限定名 |
| `package_name` | str | 所在包名 |

### 2.3 事务边界

- 函数内部 `BEGIN IMMEDIATE` → 全部 SQL → `COMMIT`/`ROLLBACK`
- 与 `batch_save_symbols` 一致：失败不抛异常，返回 `{"success": false, "error": str(e)}`

## 3. 行为契约（C1-CN 差分测试矩阵）

| # | 场景 | Python | Rust | 差分断言 |
|---|---|---|---|---|
| C1 | 单文件 + 1 个 call（策略 1 精确匹配 `module.name`） | callee_qname = "{module}.{name}"，callee_id 取自 all_symbols_map | 同 | 两端 calls 表行一致（caller_id/callee_id/callee_qualified 等所有字段） |
| C2 | 单文件 + 1 个 call（策略 3 简名唯一匹配，无 callee_module） | callee_qname = candidates[0]，is_cross_file 根据文件判断 | 同 | 两端 calls 表行一致 |
| C3 | 单文件 + 1 个 call（策略 4 同文件简名匹配） | callee_qname = file_local_qname[rel_path][callee_name]，callee_file=rel_path，is_cross=0 | 同 | 两端 calls 表行一致，is_cross_file=0 |
| C4 | 单文件 + 1 个 call（策略 5 external_symbols 唯一匹配） | callee_qname = ext_sym.qualified_name，callee_file=f"external://{pkg}"，callee_id=-ext_sym.id，is_cross=1 | 同 | 两端 calls 表行一致，callee_id 为负值 |
| C5 | 单文件 + 1 个 call（import 映射，策略 2） | import_map[alias]=full_mod，后缀 `.{full_mod_last_segment}.{callee_name}` 查 suffix_index | 同 | 两端 calls 表行一致 |
| C6 | 单文件 + 1 个 call（无 callee_name，空 call） | 调用 `_make_call_entry(raw, "", "", 0, 0)`，calls 表新增一行 callee_qualified="" | 同 | 两端 calls 表行一致（callee_qualified=""） |
| C7 | 单文件 + 多个 call 混合策略（1 个策略 1 + 1 个策略 3 + 1 个未解析） | 3 行 calls，1 行 resolved (策略 1)，1 行 resolved (策略 3)，1 行 callee_id=0 | 同 | 两端 calls 表 3 行完全一致 |
| C8 | caller_id=0 fallback 全失败（caller 不在 qname_id_map 且 file_sym_id_map 无 simple_name） | 跳过该 call，calls 表不写入 | 同 | 两端 calls 表行数一致（少 1 行） |
| C9 | call_versions 写入（caller_qualified + caller_hash） | caller_hash = fn_hash_map.get(caller_qualified, "")，空时写空 | 同 | 两端 call_versions 表行一致（caller_qualified/caller_hash/callee_*） |
| C10 | caller_qualified 为空时推导 `{module_path}::{caller_name}` | caller_qualified = f"{mod_path}::{caller_name}" | 同 | 两端 call_versions 表 caller_qualified 一致 |
| C11 | DELETE 旧 calls（changed_file_instance_ids 非空） | 分批 500，DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id IN (?)) | 同 | 两端 old_calls_deleted 一致，预填的旧 calls 均被删除 |
| C12 | 多文件批量（2 个文件，每个文件 2 个 call） | 4 行 calls + 4 行 call_versions | 同 | 两端 calls + call_versions 表行完全一致 |
| C13 | HCL 多段 name（策略 4.5，callee_name="aws_security_group.this"） | name_to_qname 唯一候选直接取 | 同 | 两端 calls 表行一致 |
| C14 | 空 file_results（无文件需处理） | 无 SQL 执行，old_calls_deleted=0, calls_inserted=0 | 同 | 两端无副作用，dict 返回值一致 |

## 4. 预期差异（允许的语义等价差异）

| # | Python 行为 | Rust 行为 | 说明 |
|---|---|---|---|
| D1 | suffix_index 用 `defaultdict(list)`，append 顺序按 all_symbols 遍历顺序 | 同 | Rust 用 `HashMap<String, Vec<String>>`，append 顺序与 Python 一致（按 all_symbols 顺序） |
| D2 | external_symbols 加载用 try/except 兼容旧 DB（无表时跳过） | Rust 不传 external_symbols 时跳过策略 5 | Python 由调用方控制：表存在则传非空列表，表不存在则传空列表 |
| D3 | file_results 中 `inline_modules` 为 None 时跳过 | Rust 同：inline_modules 字段缺失或为空列表时跳过 | |
| D4 | fn_hash_map None 时从 symbols 构建，inline_modules symbols 也参与 | Rust 同 | Rust 需要明确处理 symbols + inline_modules 两条路径 |
| D5 | executemany INSERT calls/call_versions 顺序与 file_results 中 raw_calls 遍历顺序一致 | Rust 循环 execute，顺序与 Python 一致 | |

## 5. 事务与错误处理

- **BEGIN IMMEDIATE**：与 `batch_save_symbols` 一致，立刻拿写锁
- **失败 ROLLBACK**：任何 SQL 错误都触发 ROLLBACK，返回 `{"success": false, "error": "..."}`
- **不抛 Python 异常**：所有错误包装为 dict 返回

## 6. 实现计划

### 6.1 Rust 模块结构

- 新建 `rust_ext/src/batch_calls_query.rs`：
  - `pub fn batch_resolve_and_save_calls(...)` — PyO3 入口
  - `fn build_symbol_indexes(...)` — 构建 all_symbols_map、name_index、suffix_index 等
  - `fn build_external_indexes(...)` — 构建 ext_by_qname、ext_by_name
  - `fn build_import_index(...)` — 构建 file_imports
  - `fn build_qname_id_maps(...)` — 构建 qname_id_map、file_sym_id_map（从 DB 读取）
  - `fn resolve_call(...)` — 单条 call 的 5 策略 resolve
  - `fn build_fn_hash_map(...)` — 构建 fn_hash_map
  - `fn batch_resolve_and_save_calls_inner(...)` — SQL 写入逻辑
  - `struct CallInfo` — 提取的 call 信息
  - `struct SymbolInfo` — 提取的 symbol 信息（与 batch_build_query.rs 独立，避免耦合）
  - `struct ExtSymbolInfo` — 提取的外部符号信息
  - `struct BatchSaveCallsResult` — 返回值结构
- `rust_ext/src/lib.rs`：注册 `mod batch_calls_query;` + `m.add_function(wrap_pyfunction!(batch_calls_query::batch_resolve_and_save_calls, m)?)?;`

### 6.2 Python 测试文件

- `tests/test_phase2_3_behavioral_diff.py`：1 个 TestBatchResolveAndSaveCallsDiff 类，14 个 case（C1-C14）
- 复用 `tests/test_phase2_2_behavioral_diff.py` 的 _make_codegraph_db、_prep_file_instance 等 fixture
- Python 路径走 `BuildMixin._build_call_graph_multi_lang` unbound method + 最小 db-like 对象（与 Phase 2-2 一致）

### 6.3 wire-production

- 在 `db/db_build.py` 的 `_build_call_graph_multi_lang` 入口处添加 feature_flag 检测：
  ```python
  if _should_use_rust_calls():
      result = callwarden_core.batch_resolve_and_save_calls(...)
      if result.get("success"):
          return
      # 失败回退到 Python 路径
  ```
- feature_flag 走 `rollback_config` 表（与 Phase 2-2 一致）

### 6.4 verify

- 运行 `pytest tests/test_phase2_3_behavioral_diff.py -v` 验证差分
- 运行 `pytest tests/test_p7_batch_calls.py -v` 验证不破坏现有 P7 测试
- 运行 `cw refresh --all` 在真实项目上验证端到端

### 6.5 refresh

- `cw rollback register` 登记回滚配置（id 自增）
- 更新 `docs/design/migration-manifest.md` Phase 2-3 行状态为 `✅(behavioral)`

## 7. Schema 信息

涉及的表（已在 schema.py 中定义，本契约不修改 schema）：

- `symbols` (id, file_instance_id, symbol_hash, name, kind, qualified_name, ...)
- `calls` (id, caller_id, caller_name, caller_module, callee_name, callee_module, callee_qualified, callee_file, callee_id, call_line, is_cross_file)
- `call_versions` (id, file_version_id, caller_qualified, caller_hash, callee_name, callee_module, callee_qualified, callee_file, call_line, is_cross_file)
- `external_symbols` (id, symbol_name, qualified_name, package_name, ...)

## 8. 验收标准

- [ ] `batch_resolve_and_save_calls` 在 `lib.rs` 注册并可从 Python 导入
- [ ] C1-C14 差分测试全部通过（14/14）
- [ ] `cw refresh --all` 在真实项目（callwarden 自身）上端到端成功
- [ ] 现有 P7 测试 `tests/test_p7_batch_calls.py` 全部通过
- [ ] 现有 Phase 2-2 测试 `tests/test_phase2_2_behavioral_diff.py` 不受影响
- [ ] rollback_config 表登记新 feature（feature=rust_batch_resolve_and_save_calls）
- [ ] migration-manifest.md 更新 Phase 2-3 行状态为 `✅(behavioral)`

## 9. 风险与注意事项

### 9.1 索引构建性能

Python 的 7 个索引（all_symbols_map、name_index、suffix_index 等）在大规模项目（20 万符号）
下构建耗时显著。Rust 端用 `HashMap<String, Vec<String>>` 替代 Python `defaultdict(list)`，
理论上更快，但需要避免重复 hash 字符串分配。建议：
- 字符串用 `&str` 引用而不是 `String` 克隆
- 索引构建在 `py.detach` 内（释放 GIL）

### 9.2 suffix_index 内存开销

Python 注释说明 20 万符号约 80 万项/40MB。Rust 端 `HashMap<String, Vec<String>>` 内存
占用类似，但 Vec<String> 的 String 是堆分配。如需进一步优化可考虑：
- 用 `HashMap<String, Vec<u32>>` 存 symbol_id 而非 qname 字符串
- 用 `&'static str` 或 string pool 减少 heap 分配

本契约采用最直接的 `HashMap<String, Vec<String>>` 实现，与 Python 语义对齐，性能优化留待
后续。

### 9.3 caller_id 多级 fallback

Python 的 fallback 顺序：
1. caller_qualified 精确匹配 qname_id_map
2. caller_name_raw 精确匹配 qname_id_map
3. simple_name（去 :: . # 分隔符后）匹配 file_sym_id_map[fi_id]

Rust 必须严格按此顺序，且 simple_name 提取时需要支持 `::` `.` `#` 三种分隔符的 rsplit。

### 9.4 external_symbols 表不存在场景

Python 用 try/except 兼容旧 DB。Rust 端由调用方决定：调用方先检查表是否存在，存在则
预加载并传入 `external_symbols`，不存在则传入空列表。Rust 函数本身不查 DB 表存在性，
简化实现。

### 9.5 file_results._from_db 短路不在范围

Python 在循环开头检查 `result.get("_from_db")`，若为 True 则跳过 resolve+写入（消除写放大）。
本契约**不实现此短路**，因为：
- 调用方负责决定哪些 file_results 需要走 Rust 路径（_from_db=True 的不传入）
- Rust 函数假设所有传入的 file_results 都需要 resolve+写入

### 9.6 inline_modules 处理

Python 的 fn_hash_map 构建包含 `result["inline_modules"]` 中的 symbols（kind=fn/test_fn）。
Rust 必须同样处理：
```python
for inline_mod in result.get("inline_modules", []):
    for sym in inline_mod["symbols"]:
        if sym["kind"] in ("fn", "test_fn") and "content_hash" in sym:
            fn_hash_map[sym["qualified_name"]] = sym["content_hash"]
```

### 9.7 与 Phase 2-2 的协同

Phase 2-2 的 `batch_save_symbols` 在写入 symbols 后会 DELETE 旧 calls（`old_calls_deleted`）。
Phase 2-3 的 `batch_resolve_and_save_calls` 接收的是已写入 symbols 后的 file_results，
因此：
- Phase 2-3 的 `DELETE FROM calls WHERE caller_id IN (...)` 仍然必要（消除 Phase 2-2 与
  Phase 2-3 之间产生的脏 calls，以及跨文件 resolve 产生的入边）
- Phase 2-3 不应该重新 DELETE symbols（Phase 2-2 已处理）

### 9.8 差分测试隔离

测试需要为 Python 和 Rust 各创建独立的 CodeGraph DB，预填相同的 symbols + file_instances +
file_versions + external_symbols，然后分别调用 `_build_call_graph_multi_lang` 和
`batch_resolve_and_save_calls`，最后比对 calls + call_versions 表内容。

## 10. 实现顺序

1. **Step 1**：创建 `rust_ext/src/batch_calls_query.rs` 骨架 + `lib.rs` 注册
2. **Step 2**：实现 `extract_call_info` + `extract_symbol_info` + `extract_ext_symbol_info`
3. **Step 3**：实现 `build_symbol_indexes`（all_symbols_map、name_index、suffix_index、
   file_symbols、file_local_qname、name_to_qname）
4. **Step 4**：实现 `build_external_indexes`（ext_by_qname、ext_by_name、qname_id_map 负 id 部分）
5. **Step 5**：实现 `build_import_index`（file_imports）
6. **Step 6**：实现 `build_qname_id_maps`（从 DB 读取 symbols 全量，构建 qname_id_map 正 id +
   file_sym_id_map）
7. **Step 7**：实现 `resolve_call`（5 策略 + 4.5）
8. **Step 8**：实现 `build_fn_hash_map`
9. **Step 9**：实现 `batch_resolve_and_save_calls_inner`（DELETE + INSERT calls + INSERT
   call_versions）
10. **Step 10**：实现 `batch_resolve_and_save_calls` PyO3 入口（事务 + GIL 管理）
11. **Step 11**：编译验证 `cargo check --manifest-path rust_ext/Cargo.toml`
12. **Step 12**：编写 `tests/test_phase2_3_behavioral_diff.py` 14 个 case
13. **Step 13**：运行差分测试，修复差异
14. **Step 14**：wire-production（feature_flag + rollback_config）
15. **Step 15**：verify（pytest + cw refresh --all）
16. **Step 16**：refresh（更新 migration-manifest.md）

## 11. 参考

- Python 真相源：`db/db_build.py:2030-2445` `_build_call_graph_multi_lang`
- Phase 2-2 契约：`docs/design/phase2-2-batch-save-symbols-contract.md`
- Phase 2-2 实现：`rust_ext/src/batch_build_query.rs`
- Phase 2-1 实现：`rust_ext/src/cas_merge_query.rs`
- daemon cas_merge.rs 4 策略 resolve：`rust_ext/src/daemon/cas_merge.rs:513` `resolve_callee`
- GraphStore CSR 结构：`rust_ext/src/graph.rs`
- 迁移 manifest：`docs/design/migration-manifest.md`
