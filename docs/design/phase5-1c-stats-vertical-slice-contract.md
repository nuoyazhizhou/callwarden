# Phase 5-1 C 契约：stats 子命令垂直切片

**Task ID**: `T-1785247722054-804e963c`（Phase 5-1 C）
**状态**: contract
**日期**: 2026-07-28
**依赖**: Phase 5-1 A（clap 骨架）、Phase 5-3（兼容输出层）

## 1. 范围

Phase 5-1 C 选择 `stats` 子命令作为 59 个子命令迁移的**垂直切片示例**，验证端到端流程：
参数解析 → 业务逻辑 → 输出格式化 → exit code。

`stats` 子命令是所有子命令中最简单的（无参数、纯查询、JSON 输出），适合作为迁移模板。

**涉及**：
- **C.1 业务逻辑函数**：`stats_command_run(stats_json: &str) -> StatsResult`
- **C.2 PyO3 暴露**：`stats_command_run_py(stats_json: &str) -> (i32, String, String)`
- **C.3 cw_cli binary 接入**：Stats 分支从 "not implemented" 升级为 "data source wiring pending"
- **C.4 差分测试**：Python `_handle_stats` vs Rust `stats_command_run`

**不涉及**（留给后续阶段）：
- 数据查询层迁移（`db.get_stats()` 的 SQL 仍在 Python，wire-production 时通过 PyO3 桥接）
- daemon client 接入（Phase 5-2）
- 直接 SQL 查询（Phase 1-1 sqlite_query 模块的扩展）
- 其他 58 个子命令的迁移

## 2. Python 真相源

| 文件 | 行号 | 函数/行为 | 迁移方式 |
|---|---|---|---|
| `cli/main.py` | 6623-6636 | `_handle_stats(args, db)` | Rust `stats_command_run` |
| `cli/main.py` | 6634 | `db.get_stats()` | **不迁移**（数据查询层保持 Python） |
| `cli/main.py` | 6635 | `json.dumps(stats, indent=2, ensure_ascii=False)` | 复用 Phase 5-3 `json_dumps_pretty` |
| `cli/main.py` | 6636 | `return True`（exit 0） | Rust `StatsResult.exit_code = 0` |

### Python 行为详解

```python
def _handle_stats(args, db):
    parser = argparse.ArgumentParser(prog="cw stats", description="...")
    parser.parse_args(args)          # 无参数，仅 --help
    stats = db.get_stats()           # 数据查询层（不迁移）
    print(json.dumps(stats, indent=2, ensure_ascii=False))  # JSON 输出
    return True                      # exit 0
```

**关键行为**：
1. 无参数子命令：`args` 为空列表，`parse_args(args)` 不报错
2. `--help` 由 argparse 处理，输出 help 后 exit 0
3. `db.get_stats()` 返回 dict，包含 total_files/total_symbols/by_kind 等字段
4. `json.dumps` 输出带 2 空格缩进、非 ASCII 不转义的 JSON
5. 输出到 stdout，返回 True（exit 0）

## 3. API 契约

### 3.1 StatsResult 结构（C.1）

```rust
/// stats 子命令执行结果。
///
/// 对齐 Python `_handle_stats` 的返回值（True/False）和副作用（print）。
pub struct StatsResult {
    /// exit code：0 成功，1 失败
    pub exit_code: i32,
    /// stdout 输出内容（已格式化的 JSON）
    pub stdout: String,
    /// stderr 输出内容（错误信息）
    pub stderr: String,
}
```

### 3.2 stats_command_run 函数（C.1）

```rust
/// 执行 stats 子命令业务逻辑。
///
/// 对齐 Python `cli/main.py:_handle_stats()` (L6623-6636)
///
/// 参数：
/// - `stats_json`: 已序列化的 stats 数据（JSON 字符串），由调用方提供。
///   生产环境：Python `db.get_stats()` 的结果通过 `json.dumps(stats)` 序列化后传入。
///   测试环境：直接传入测试 JSON 字符串。
///
/// 行为：
/// 1. 解析 `stats_json`（若无效，输出错误到 stderr，返回 exit 1）
/// 2. 用 `json_dumps_pretty` 重新格式化为带 2 空格缩进的 JSON
/// 3. 输出到 stdout
/// 4. 返回 exit code 0
///
/// 返回：`StatsResult`
pub fn stats_command_run(stats_json: &str) -> StatsResult {
    // 复用 Phase 5-3 的 json_dumps_pretty
    let pretty = json_dumps_pretty(stats_json);
    if pretty == "null" && stats_json.trim() != "null" {
        // JSON 解析失败
        return StatsResult {
            exit_code: 1,
            stdout: String::new(),
            stderr: format!("error: invalid JSON input for stats command"),
        };
    }
    StatsResult {
        exit_code: 0,
        stdout: pretty,
        stderr: String::new(),
    }
}
```

### 3.3 PyO3 暴露（C.2）

```rust
/// Python 暴露的 stats_command_run。
///
/// 返回 (exit_code, stdout, stderr) 三元组，便于 Python 调用方处理。
#[pyfunction]
pub fn stats_command_run_py(stats_json: &str) -> (i32, String, String) {
    let result = stats_command_run(stats_json);
    (result.exit_code, result.stdout, result.stderr)
}
```

### 3.4 cw_cli binary 接入（C.3）

```rust
// rust_ext/src/bin/cw_cli.rs
Commands::Stats => {
    // Phase 5-1 C: 业务逻辑已在 lib 实现（stats_command_run），
    // 但 cw_cli binary 无数据库连接，无法获取 stats_json。
    // wire-production 阶段通过 daemon client（Phase 5-2）或直接 SQL（后续）接入。
    eprintln!("cw stats: data source not available in standalone mode");
    eprintln!("  (Phase 5-1 C: business logic implemented in lib, awaiting data source wiring)");
    eprintln!("  Use 'python cw.py stats' for now, or wait for Phase 5-2 daemon client.");
    std::process::exit(1);
}
```

## 4. 差分测试矩阵

### D1: 有效 JSON 输入

| 场景 | 输入 | 期望 exit_code | 期望 stdout |
|---|---|---|---|
| D1.1 简单对象 | `{"a": 1}` | 0 | `{\n  "a": 1\n}` |
| D1.2 嵌套对象 | `{"outer": {"inner": "v"}}` | 0 | `{\n  "outer": {\n    "inner": "v"\n  }\n}` |
| D1.3 数组 | `[1, 2, 3]` | 0 | `[\n  1,\n  2,\n  3\n]` |
| D1.4 中文 | `{"name": "中文"}` | 0 | `{\n  "name": "中文"\n}` |
| D1.5 emoji | `{"emoji": "🎉"}` | 0 | `{\n  "emoji": "🎉"\n}` |
| D1.6 空对象 | `{}` | 0 | `{}` |
| D1.7 空数组 | `[]` | 0 | `[]` |
| D1.8 null | `null` | 0 | `null` |
| D1.9 数字 | `42` | 0 | `42` |
| D1.10 字符串 | `"hello"` | 0 | `"hello"` |

### D2: 无效 JSON 输入

| 场景 | 输入 | 期望 exit_code | 期望 stderr |
|---|---|---|---|
| D2.1 空字符串 | `""` | 1 | `error: invalid JSON...` |
| D2.2 损坏 JSON | `{invalid}` | 1 | `error: invalid JSON...` |
| D2.3 不完整 JSON | `{"a":` | 1 | `error: invalid JSON...` |

### D3: 与 Python `_handle_stats` 行为对齐

| 场景 | Python 行为 | Rust 行为 |
|---|---|---|
| D3.1 有效 stats | `json.dumps(stats, indent=2, ensure_ascii=False)` | `json_dumps_pretty(stats_json)` |
| D3.2 exit code | `return True` → exit 0 | `exit_code = 0` |
| D3.3 输出目标 | `print()` → stdout | `stdout` 字段 |

## 5. 实现计划

1. **创建 `rust_ext/src/cli/stats.rs`**：实现 `StatsResult` + `stats_command_run` + `stats_command_run_py` + 单元测试
2. **修改 `rust_ext/src/cli/mod.rs`**：声明 `pub mod stats;`
3. **修改 `rust_ext/src/lib.rs`**：注册 `stats_command_run_py` PyO3 函数
4. **修改 `rust_ext/src/bin/cw_cli.rs`**：Stats 分支从 "not implemented" 升级为 "data source wiring pending"
5. **创建 `tests/test_phase5_1c_stats_diff.py`**：D1-D3 差分测试
6. **更新 `migration-manifest.md`**：添加 §35 Phase 5-1 C 章节

## 6. 预期差异

1. **数据查询层分离**：Python `_handle_stats` 内部调用 `db.get_stats()`，Rust `stats_command_run` 接收已序列化的 JSON。这是设计差异，便于测试和组合（业务逻辑不持有数据库连接）。

2. **错误处理**：Python `db.get_stats()` 抛异常时由上层捕获；Rust 端假设 `stats_json` 已是有效的 JSON 字符串（数据查询层的异常处理在调用方）。

3. **--help 处理**：Python `argparse` 自动处理 `--help`；Rust `clap` 在 cw_cli binary 中自动处理，lib 层 `stats_command_run` 不处理 `--help`。

## 7. 验收标准

1. **D1-D3 测试矩阵全部通过**
2. **Rust 单元测试全部通过**
3. **`cargo build --release --bin cw` 编译通过**
4. **`cw stats` 输出 "data source wiring pending" 提示**（非 "not implemented"）
5. **PyO3 暴露 `stats_command_run_py` 函数**
6. **migration-manifest.md §35 Review 清单完整**

## 8. 与后续阶段的关系

| 阶段 | 交付物 | Phase 5-1 C 关系 |
|---|---|---|
| 5-1 C（本阶段） | stats 子命令垂直切片 | **本阶段** |
| 5-1 C 扩展 | 其他 58 个子命令迁移 | 参照 stats 模板 |
| 5-2 | daemon client | cw_cli binary 通过 daemon 获取 stats_json |
| wire-production | Python CLI 调用 Rust | Python `_handle_stats` 调用 `stats_command_run_py` |
