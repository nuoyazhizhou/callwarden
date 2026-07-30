# Phase 5-3 契约：Rust 兼容输出层

**Task ID**: `T-1785246955`（Phase 5-3）
**状态**: contract
**日期**: 2026-07-28
**前置**: Phase 5-1 A（配置加载）已完成

## 1. 范围

Phase 5-3 实现 Rust 端的兼容输出层，对齐 Python `cli/console.py` 的彩色文本/格式化工具，以及对齐 Python `json.dumps(data, indent=2, ensure_ascii=False)` 的 JSON 输出格式。为 Phase 5-1 C（子命令业务逻辑）提供输出能力。

**涉及**：
- **C.1 ANSI 颜色码**：对齐 `_COLORS` 字典（14 种颜色/样式）
- **C.2 颜色检测**：对齐 `should_use_color()`（NO_COLOR / TTY / FORCE_COLOR 三层判定）
- **C.3 彩色打印**：对齐 `colorize` / `cprint` / `success` / `error` / `warning` / `info` / `dim` / `bold`
- **C.4 格式化工具**：对齐 `format_duration` / `format_size`
- **C.5 JSON 输出**：对齐 `json.dumps(data, indent=2, ensure_ascii=False)`（serde_json + 缩进 + 非 ASCII 转义禁用）

**不涉及**（留给后续阶段）：
- `print_build_summary`（依赖 i18n，留给 Phase 5-1 C 集成时实现）
- `Spinner` / `print_progress`（交互式 UI，留给后续阶段）
- i18n 国际化（留给 Phase 5-1 C 集成时接入）
- 实际子命令业务逻辑（Phase 5-1 C）

## 2. Python 真相源

| 文件 | 行号 | 函数/常量 | 迁移方式 |
|---|---|---|---|
| `cli/console.py` | 17-33 | `_COLORS` 字典（14 种颜色） | Rust const HashMap |
| `cli/console.py` | 38-60 | `_enable_vt_mode()`（Windows VT100） | Rust `#[cfg(windows)]` + `kernel32-sys`（或留空，Windows 10+ 默认支持） |
| `cli/console.py` | 63-81 | `ensure_utf8_output()` | Rust 默认 UTF-8，无需特殊处理 |
| `cli/console.py` | 84-112 | `should_use_color()` | Rust fn（NO_COLOR / TTY / FORCE_COLOR） |
| `cli/console.py` | 115-130 | `colorize(text, color)` | Rust fn |
| `cli/console.py` | 133-146 | `cprint(text, color, bold)` | Rust fn |
| `cli/console.py` | 149-158 | `success(msg)` → `✓ {msg}` green | Rust fn |
| `cli/console.py` | 161-170 | `error(msg)` → `✗ {msg}` red | Rust fn |
| `cli/console.py` | 173-182 | `warning(msg)` → `⚠ {msg}` yellow | Rust fn |
| `cli/console.py` | 185-194 | `info(msg)` → `ℹ {msg}` blue | Rust fn |
| `cli/console.py` | 197-206 | `dim(msg)` → `{msg}` dim | Rust fn |
| `cli/console.py` | 209-220 | `bold(msg)` → `{msg}` bold | Rust fn |
| `cli/console.py` | 274-297 | `format_duration(seconds)` | Rust fn |
| `cli/console.py` | 300-313 | `format_size(n)` | Rust fn |
| `cli/main.py` | 多处 | `json.dumps(data, indent=2, ensure_ascii=False)` | Rust fn（serde_json + 缩进 + 非 ASCII） |

## 3. API 契约

### 3.1 ANSI 颜色码（C.1）

```rust
pub const COLOR_RESET: &str = "\033[0m";
pub const COLOR_BOLD: &str = "\033[1m";
pub const COLOR_DIM: &str = "\033[2m";
pub const COLOR_RED: &str = "\033[31m";
pub const COLOR_GREEN: &str = "\033[32m";
pub const COLOR_YELLOW: &str = "\033[33m";
pub const COLOR_BLUE: &str = "\033[34m";
pub const COLOR_MAGENTA: &str = "\033[35m";
pub const COLOR_CYAN: &str = "\033[36m";
pub const COLOR_WHITE: &str = "\033[37m";
pub const COLOR_BRIGHT_RED: &str = "\033[91m";
pub const COLOR_BRIGHT_GREEN: &str = "\033[92m";
pub const COLOR_BRIGHT_YELLOW: &str = "\033[93m";
pub const COLOR_BRIGHT_BLUE: &str = "\033[94m";
pub const COLOR_BRIGHT_CYAN: &str = "\033[96m";
```

### 3.2 颜色检测（C.2）

#### `should_use_color(no_color: bool, is_tty: bool, force_color: bool, vt_enabled: bool) -> bool`

**行为**：判定是否启用彩色输出。

**Python 真相源**：`cli/console.py:should_use_color()` (L84-112)

**判定规则**（按优先级）：
1. `NO_COLOR` 环境变量存在 → `false`
2. stdout 非 TTY → `false`
3. `FORCE_COLOR` 环境变量存在 → `true`
4. 否则按 VT 模式启用结果决定

**参数化**：测试时由调用方传入 `no_color` / `is_tty` / `force_color` / `vt_enabled`，生产代码从环境变量和 stdout 检测。

### 3.3 彩色打印（C.3）

#### `colorize(text: &str, color: &str, use_color: bool) -> String`

**行为**：为文本添加 ANSI 颜色转义序列。`use_color=false` 时直接返回原文本。

**Python 真相源**：`cli/console.py:colorize()` (L115-130)

#### `cprint(text: &str, color: Option<&str>, bold: bool, use_color: bool) -> String`

**行为**：返回格式化后的字符串（不直接打印，便于组合）。`bold=true` 时先套 bold，再套 color。

**Python 真相源**：`cli/console.py:cprint()` (L133-146)

**注意**：Python `cprint` 直接 `print()`，Rust 端返回字符串由调用方决定打印时机（更灵活，便于测试）。

#### 预定义消息函数

| 函数 | 前缀 | 颜色 | 示例 |
|---|---|---|---|
| `success(msg)` | `✓ ` | green | `✓ Build complete` |
| `error(msg)` | `✗ ` | red | `✗ Parse failed` |
| `warning(msg)` | `⚠ ` | yellow | `⚠ Deprecated` |
| `info(msg)` | `ℹ ` | blue | `ℹ Starting...` |
| `dim(msg)` | （无前缀） | dim | `Processing...` |
| `bold(msg)` | （无前缀） | bold | `Summary` |

### 3.4 格式化工具（C.4）

#### `format_duration(seconds: f64) -> String`

**行为**：将秒数格式化为人类可读的时长字符串。

**Python 真相源**：`cli/console.py:format_duration()` (L274-297)

**规则**：
- `< 0.001s` → `{ms:.1f}ms`（如 `0.5ms`）
- `< 1s` → `{ms:.0f}ms`（如 `120ms`）
- `< 60s` → `{s:.1f}s`（如 `3.5s`）
- `< 60m` → `{m}m{s:.0f}s`（如 `2m30s`）
- `>= 60m` → `{h}h{m}m`（如 `1h15m`）

#### `format_size(n: u64) -> String`

**行为**：将字节数格式化为人类可读的大小字符串。

**Python 真相源**：`cli/console.py:format_size()` (L300-313)

**规则**：
- `< 1024` → `{n} B`
- `< 1024*1024` → `{kb:.1f} KB`
- `>= 1024*1024` → `{mb:.1f} MB`

### 3.5 JSON 输出（C.5）

#### `json_dumps_pretty(data: &str) -> String`

**行为**：将 JSON 字符串重新序列化为带缩进、非 ASCII 转义禁用的格式。

**Python 真相源**：`json.dumps(data, indent=2, ensure_ascii=False)`

**实现**：
- 输入：已序列化的 JSON 字符串（或 Rust 结构体）
- 输出：`indent=2` + `ensure_ascii=False`（中文等非 ASCII 字符不转义）

**Rust 实现**：
```rust
use serde_json::Value;
pub fn json_dumps_pretty(json_str: &str) -> String {
    let v: Value = serde_json::from_str(json_str).unwrap_or(Value::Null);
    serde_json::to_string_pretty(&v).unwrap_or_default()
}
```

**注意**：`serde_json::to_string_pretty` 默认缩进 2 空格，但会对非 ASCII 字符转义。需要配置 `serde_json::Serializer` 禁用非 ASCII 转义，或使用 `serde_json::to_string_pretty` + 手动 `ensure_ascii=False` 替换。

**替代方案**：直接在 PyO3 层调用 Python `json.dumps`（保证 byte-level 一致），但会增加 Python 依赖。本阶段采用 Rust `serde_json` 实现，差分测试验证一致性。

## 4. 行为契约

### D1: colorize

| 场景 | text | color | use_color | 期望 |
|---|---|---|---|---|
| D1.1 | "hello" | "red" | true | `\033[31mhello\033[0m` |
| D1.2 | "hello" | "red" | false | `hello` |
| D1.3 | "hello" | "unknown" | true | `hello`（未知颜色不套码） |
| D1.4 | "" | "red" | true | `\033[31m\033[0m` |

### D2: should_use_color

| 场景 | NO_COLOR | is_tty | FORCE_COLOR | vt_enabled | 期望 |
|---|---|---|---|---|---|
| D2.1 | true | 任意 | 任意 | 任意 | false |
| D2.2 | false | false | 任意 | 任意 | false |
| D2.3 | false | true | true | 任意 | true |
| D2.4 | false | true | false | true | true |
| D2.5 | false | true | false | false | false |

### D3: 预定义消息函数

| 场景 | 函数 | msg | use_color | 期望 |
|---|---|---|---|---|
| D3.1 | success | "done" | true | `\033[32m✓ done\033[0m` |
| D3.2 | error | "fail" | true | `\033[31m✗ fail\033[0m` |
| D3.3 | warning | "warn" | true | `\033[33m⚠ warn\033[0m` |
| D3.4 | info | "info" | true | `\033[34mℹ info\033[0m` |
| D3.5 | dim | "dim" | true | `\033[2mdim\033[0m` |
| D3.6 | bold | "bold" | true | `\033[1mbold\033[0m` |
| D3.7 | success | "done" | false | `✓ done` |

### D4: format_duration

| 场景 | seconds | 期望 |
|---|---|---|
| D4.1 | 0.0005 | `0.5ms` |
| D4.2 | 0.12 | `120ms` |
| D4.3 | 3.5 | `3.5s` |
| D4.4 | 150 | `2m30s` |
| D4.5 | 3900 | `1h5m` |
| D4.6 | 0 | `0.0ms` |

### D5: format_size

| 场景 | n | 期望 |
|---|---|---|
| D5.1 | 0 | `0 B` |
| D5.2 | 512 | `512 B` |
| D5.3 | 1023 | `1023 B` |
| D5.4 | 1024 | `1.0 KB` |
| D5.5 | 1536 | `1.5 KB` |
| D5.6 | 1048576 | `1.0 MB` |
| D5.7 | 1572864 | `1.5 MB` |

### D6: json_dumps_pretty

| 场景 | 输入 | 期望 |
|---|---|---|
| D6.1 | `{"a":1,"b":2}` | `{\n  "a": 1,\n  "b": 2\n}` |
| D6.2 | `{"name":"中文"}` | `{\n  "name": "中文"\n}`（非 ASCII 不转义） |
| D6.3 | `[1,2,3]` | `[\n  1,\n  2,\n  3\n]` |

## 5. 预期差异

1. **VT 模式启用**：Python 在 Windows 上通过 `kernel32.SetConsoleMode` 启用 VT100。Rust 端在 Windows 10+（1607+）默认支持 ANSI，无需特殊处理；旧版 Windows 需 `enable-ansi-support` crate 或 `windows-sys` API 调用。本阶段假设 Windows 10+，不实现 VT 启用（差分测试在 Linux/Windows 10+ 上验证）。
2. **cprint 返回值**：Python `cprint` 直接 `print()`，Rust 端返回字符串。调用方需自行 `println!`。这是设计差异，便于测试和组合。
3. **JSON 序列化**：Rust `serde_json::to_string_pretty` 默认对非 ASCII 字符转义（如 `\u4e2d\u6587`）。需配置 `preserve_ascii: false` 或使用 `serde_json::Serializer::with_formatter` 禁用转义。Python `json.dumps(ensure_ascii=False)` 不转义。差分测试 D6.2 验证一致性。
4. **Unicode 前缀字符**：`✓ ✗ ⚠ ℹ` 是 Unicode 字符，Rust 默认 UTF-8 输出，与 Python `ensure_utf8_output()` 一致。
5. **stdout 缓冲**：Python `print` 默认行缓冲，Rust `println!` 也是行缓冲（`stdout().lock()`）。差异可忽略。

## 6. 实现计划

### C.1 Rust 实现

1. **新增 `rust_ext/src/cli/output.rs`**：
   - 14 种 ANSI 颜色码常量
   - `should_use_color(no_color, is_tty, force_color, vt_enabled)` 函数
   - `colorize(text, color, use_color)` 函数
   - `cprint(text, color, bold, use_color)` 函数（返回字符串）
   - `success` / `error` / `warning` / `info` / `dim` / `bold` 预定义函数
   - `format_duration(seconds)` / `format_size(n)` 函数
   - `json_dumps_pretty(json_str)` 函数（serde_json + 非 ASCII 禁用转义）

2. **更新 `rust_ext/src/cli/mod.rs`**：声明 `pub mod output;`

3. **Cargo.toml 依赖**：`serde_json`（已在依赖中）

### C.2 PyO3 暴露

在 `rust_ext/src/lib.rs` 注册：
- `should_use_color_py(no_color: bool, is_tty: bool, force_color: bool, vt_enabled: bool) -> bool`
- `colorize_py(text: &str, color: &str, use_color: bool) -> String`
- `cprint_py(text: &str, color: Option<&str>, bold: bool, use_color: bool) -> String`
- `success_py(msg: &str, use_color: bool) -> String`
- `error_py(msg: &str, use_color: bool) -> String`
- `warning_py(msg: &str, use_color: bool) -> String`
- `info_py(msg: &str, use_color: bool) -> String`
- `dim_py(msg: &str, use_color: bool) -> String`
- `bold_py(msg: &str, use_color: bool) -> String`
- `format_duration_py(seconds: f64) -> String`
- `format_size_py(n: u64) -> String`
- `json_dumps_pretty_py(json_str: &str) -> String`

### C.3 差分测试

新增 `tests/test_phase5_3_output_diff.py`，覆盖 D1-D6 测试矩阵。

### C.4 单元测试

在 `output.rs` 中添加 `#[cfg(test)] mod tests`，覆盖 D1-D6 全部场景。

## 7. 验收标准

1. **D1-D6 测试矩阵全部通过**：输出层与 Python 真相源行为一致
2. **`cargo build --release` 编译通过**
3. **PyO3 暴露 12 个函数**
4. **migration-manifest.md §34 Review 清单完整**
5. **不修改 Python CLI**：Phase 5-3 是纯新增 Rust 实现

## 8. 风险与注意事项

- **JSON 非 ASCII 转义**：Rust `serde_json` 默认转义非 ASCII 字符，需配置禁用。如配置复杂，可使用 `pythonize` crate 或直接在 PyO3 层调用 Python `json.dumps`（但增加 Python 依赖）。本阶段优先尝试 Rust 原生实现。
- **Windows VT 模式**：Windows 10 1607+ 默认支持 ANSI，旧版需 API 调用。本阶段假设 Windows 10+，不实现 VT 启用。
- **cprint 返回字符串**：与 Python `cprint` 直接 `print()` 不同，Rust 端返回字符串。调用方需自行 `println!`。这是设计差异，便于测试。
- **不涉及 i18n**：`print_build_summary` 等依赖 i18n 的函数留给 Phase 5-1 C 集成时实现。
- **不涉及 rollback_config 登记**：输出层是纯计算函数，无副作用。

## 9. 与后续阶段的关系

| 阶段 | 交付物 | Phase 5-3 关系 |
|---|---|---|
| 5-1 C | 子命令业务逻辑 | 调用 `colorize` / `format_duration` / `json_dumps_pretty` 输出结果 |
| 5-2 | client/agent | RPC 响应格式化使用 `json_dumps_pretty` |
| 5-4 | 安装器/smoke | 验证终端颜色输出在 systemd journal 中的兼容性 |
