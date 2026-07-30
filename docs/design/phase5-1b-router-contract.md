# Phase 5-1 B 契约：Rust local/enterprise/auto 路由决策

**Task ID**: `T-1785233570754-b08ecf14`（Phase 5-1 B 子任务）
**状态**: contract
**日期**: 2026-07-28
**前置**: Phase 5-1 A（配置加载 + clap 骨架 + 只读识别）已完成

## 1. 范围

Phase 5-1 B 实现 Rust 端的命令路由决策模块，对齐 Python `config.py` 中的 `get_daemon_mode` / `is_daemon_required` / `is_daemon_available` 逻辑，并新增 `route_command()` 决策函数，为后续 Phase 5-2（client/agent）和 Phase 5-3（兼容输出）提供路由能力。

**涉及**：
- **B.1 DaemonMode 枚举**：`Local` / `Enterprise` / `Auto` 三种模式
- **B.2 路由决策函数**：`route_command(mode, socket_path, platform) -> RouteDecision`
- **B.3 辅助查询函数**：`get_daemon_mode` / `is_daemon_required` / `is_daemon_available` 的 Rust 对齐

**不涉及**（留给后续阶段）：
- 实际执行 local 或 enterprise 路径（Phase 5-1 C / 5-2）
- 兼容输出格式（Phase 5-3）
- 安装器/打包（Phase 5-4）
- 修改 Python CLI（Python 保持真相源，Rust 仅新增）

## 2. Python 真相源

| 文件 | 行号 | 函数/常量 | 迁移方式 |
|---|---|---|---|
| `config.py` | 1319-1321 | `DAEMON_SOCKET_PATH` 环境变量 + 默认 `/run/callwarden/callwarden.sock` | Rust const + env 读取 |
| `config.py` | 1343 | `DAEMON_MODE = os.environ.get("CW_DAEMON_MODE", "auto")` | Rust env 读取 |
| `config.py` | 1366-1368 | `get_daemon_mode() -> str` | Rust fn |
| `config.py` | 1371-1373 | `is_daemon_required() -> bool`（mode == "enterprise"） | Rust fn |
| `config.py` | 1376-1383 | `is_daemon_available() -> bool`（Windows False，否则检测 socket 存在） | Rust fn（跨平台） |
| `cli/main.py` | 10196-10198 | `cw daemon` 子命令直接委托给 `run_daemon_command` | Phase 5-2 迁移 |
| `cli/main.py` | 12352-12387 | `run_daemon_mode` exec Rust cw_daemon binary | Phase 5-2 迁移 |

## 3. API 契约

### 3.1 DaemonMode 枚举（B.1）

```rust
pub enum DaemonMode {
    Local,       // 强制走本地 SQLite
    Enterprise,  // 强制走 daemon RPC
    Auto,        // 自动检测：有 daemon 用 daemon，没有用 local
}
```

**行为**：
- 从 `CW_DAEMON_MODE` 环境变量读取，默认 `"auto"`
- `"local"` → `Local`
- `"enterprise"` → `Enterprise`
- `"auto"` → `Auto`
- 未知值 → `Auto`（fail-soft，不 fail-closed）

### 3.2 RouteDecision（B.2）

```rust
pub enum RouteDecision {
    Local,           // 走本地 SQLite 路径
    Enterprise,      // 走 daemon RPC 路径
    Unavailable,     // daemon 不可用但 mode=enterprise（fail-closed 场景）
}
```

**决策矩阵**：

| mode | platform | socket 存在 | decision |
|---|---|---|---|
| Local | 任意 | 任意 | Local |
| Enterprise | Linux | 是 | Enterprise |
| Enterprise | Linux | 否 | Unavailable |
| Enterprise | Windows/macOS | 任意 | Unavailable（UDS 不可用） |
| Auto | Linux | 是 | Enterprise |
| Auto | Linux | 否 | Local |
| Auto | Windows/macOS | 任意 | Local |

#### `route_command(mode: &DaemonMode, socket_path: &Path, platform: &str) -> RouteDecision`

**行为**：根据 mode、socket 存在性、平台返回路由决策。

**Python 真相源**：综合 `config.py:is_daemon_required` + `is_daemon_available` + `cli/main.py:main()` 的路由逻辑（当前 Python 未显式实现该函数，但隐含逻辑是：`cw daemon` 走 daemon，其他走 local；本契约将其显式化为 `route_command`）。

### 3.3 辅助查询函数（B.3）

#### `get_daemon_mode() -> DaemonMode`

**行为**：从 `CW_DAEMON_MODE` 环境变量读取模式。

**Python 真相源**：`config.py:get_daemon_mode()` (L1366-1368)

#### `is_daemon_required() -> bool`

**行为**：返回 `mode == Enterprise`。

**Python 真相源**：`config.py:is_daemon_required()` (L1371-1373)

#### `is_daemon_available(socket_path: &Path, platform: &str) -> bool`

**行为**：
- Windows/macOS → `false`（UDS 不可用）
- Linux → `socket_path.exists()`

**Python 真相源**：`config.py:is_daemon_available()` (L1376-1383)

#### `daemon_socket_path() -> PathBuf`

**行为**：从 `CW_DAEMON_SOCKET` 环境变量读取，默认 `/run/callwarden/callwarden.sock`。

**Python 真相源**：`config.py:DAEMON_SOCKET_PATH` (L1319-1321)

## 4. 行为契约

### D1: get_daemon_mode

| 场景 | 环境变量 | 期望 |
|---|---|---|
| D1.1 | `CW_DAEMON_MODE=local` | `Local` |
| D1.2 | `CW_DAEMON_MODE=enterprise` | `Enterprise` |
| D1.3 | `CW_DAEMON_MODE=auto` | `Auto` |
| D1.4 | `CW_DAEMON_MODE` 未设置 | `Auto`（默认） |
| D1.5 | `CW_DAEMON_MODE=unknown` | `Auto`（fail-soft） |

### D2: is_daemon_required

| 场景 | mode | 期望 |
|---|---|---|
| D2.1 | Local | false |
| D2.2 | Enterprise | true |
| D2.3 | Auto | false |

### D3: is_daemon_available

| 场景 | platform | socket 存在 | 期望 |
|---|---|---|---|
| D3.1 | linux | 是 | true |
| D3.2 | linux | 否 | false |
| D3.3 | windows | 任意 | false |
| D3.4 | macos | 任意 | false |

### D4: route_command

| 场景 | mode | platform | socket | 期望 |
|---|---|---|---|---|
| D4.1 | Local | linux | 任意 | Local |
| D4.2 | Enterprise | linux | 是 | Enterprise |
| D4.3 | Enterprise | linux | 否 | Unavailable |
| D4.4 | Enterprise | windows | 任意 | Unavailable |
| D4.5 | Auto | linux | 是 | Enterprise |
| D4.6 | Auto | linux | 否 | Local |
| D4.7 | Auto | windows | 任意 | Local |
| D4.8 | Local | windows | 任意 | Local |

### D5: daemon_socket_path

| 场景 | 环境变量 | 期望 |
|---|---|---|
| D5.1 | `CW_DAEMON_SOCKET=/tmp/x.sock` | `/tmp/x.sock` |
| D5.2 | 未设置 | `/run/callwarden/callwarden.sock` |

## 5. 预期差异

1. **Python 隐式 vs Rust 显式**：Python CLI 当前没有显式 `route_command` 函数，路由逻辑散落在 `main()` 和 `run_daemon_mode` 中。Rust 端将其显式化为单一函数，便于测试和复用。
2. **平台检测**：Python `os.name == "nt"` 检测 Windows，Rust `std::env::consts::OS == "windows"`。两者等价。
3. **socket 存在性检查**：Python `os.path.exists`，Rust `Path::exists`。两者行为一致。
4. **未知 mode 值处理**：Python `DAEMON_MODE` 变量是字符串，未做枚举校验，`is_daemon_required()` 只检查 `== "enterprise"`，其他值（包括 `"unknown"`）等同于 `auto`。Rust 端 `DaemonMode::from_str` 对未知值返回 `Auto`，与 Python 行为一致。

## 6. 实现计划

### B.1 Rust 实现

1. **新增 `rust_ext/src/cli/router.rs`**：
   - `DaemonMode` enum + `from_str()`
   - `RouteDecision` enum
   - `get_daemon_mode()` / `is_daemon_required()` / `is_daemon_available()` / `daemon_socket_path()`
   - `route_command(mode, socket_path, platform) -> RouteDecision`

2. **更新 `rust_ext/src/cli/mod.rs`**：声明 `pub mod router;`

3. **PyO3 暴露**（在 `rust_ext/src/lib.rs` 注册）：
   - `get_daemon_mode_py() -> String`
   - `is_daemon_required_py() -> bool`
   - `is_daemon_available_py(socket_path: &str, platform: &str) -> bool`
   - `daemon_socket_path_py() -> String`
   - `route_command_py(mode: &str, socket_path: &str, platform: &str) -> String`

### B.2 差分测试

新增 `tests/test_phase5_1b_router_diff.py`，覆盖 D1-D5 测试矩阵。

### B.3 单元测试

在 `router.rs` 中添加 `#[cfg(test)] mod tests`，覆盖 D1-D5 全部场景。

## 7. 验收标准

1. **D1-D5 测试矩阵全部通过**：路由决策与 Python 真相源行为一致
2. **`cargo build --release` 编译通过**
3. **PyO3 暴露 5 个函数**：`get_daemon_mode_py` / `is_daemon_required_py` / `is_daemon_available_py` / `daemon_socket_path_py` / `route_command_py`
4. **migration-manifest.md §33 Review 清单完整**
5. **不修改 Python CLI**：Phase 5-1 B 是纯新增 Rust 实现

## 8. 风险与注意事项

- **不涉及实际执行路径**：本阶段仅实现路由决策函数，不实际执行 local 或 enterprise 路径。Phase 5-1 C / 5-2 将根据 `RouteDecision` 分发到对应执行器。
- **不涉及 rollback_config 登记**：路由决策是纯计算函数，无副作用，无需登记 rollback。
- **fail-soft vs fail-closed**：未知 mode 值 fail-soft 为 `Auto`（不阻断），但 `Enterprise` 模式下 daemon 不可用时返回 `Unavailable`（fail-closed，由调用方决定是否退出）。

## 9. 与后续阶段的关系

| 阶段 | 交付物 | Phase 5-1 B 关系 |
|---|---|---|
| 5-1 C | 子命令业务逻辑垂直切片 | 调用 `route_command()` 决定走 local 还是 enterprise |
| 5-2 | Rust client/agent | client 根据 `RouteDecision::Enterprise` 发起 RPC |
| 5-3 | 路由与兼容输出 | 输出层根据 `RouteDecision` 选择格式 |
| 5-4 | 安装器/smoke | 验证 `CW_DAEMON_MODE` 环境变量在 systemd unit 中的传递 |
