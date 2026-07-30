# Phase 5-1 契约：Rust CLI 命令树与配置加载

**Task ID**: `T-1785233570754-b08ecf14`（Phase 5-1）
**状态**: contract
**日期**: 2026-07-28

## 1. 范围

Phase 5-1 是 Phase 5 的第一个子任务，迁移 Python CLI 命令树骨架和配置加载器到 Rust。本阶段**仅实现骨架**（命令解析 + 配置加载 + 只读识别），不实现任何子命令的业务逻辑。

**涉及**：
- **A.1 配置加载器**：Rust 对齐 `release/config_loader.py` 的 TOML + env + CLI 三层优先级
- **A.2 clap 命令树骨架**：59 个子命令的 clap 枚举对齐 `cli/main.py:_SUBCOMMANDS`，仅解析不执行
- **A.3 只读命令识别**：移植 `_is_readonly_command` / `_is_readonly_args`，为后续锁优化提供基础

**不涉及**（留给后续阶段）：
- 子命令业务逻辑实现（Phase 5-1 C 阶段）
- daemon client/agent（Phase 5-2）
- local/enterprise/auto 路由决策（Phase 5-3）
- 安装器/打包（Phase 5-4）
- Python CLI 的任何修改（Python 保持真相源，Rust 仅新增）

## 2. Python 真相源

| 文件 | 行号 | 函数/类 | 迁移方式 |
|---|---|---|---|
| `release/config_loader.py` | 29-66 | `PlatformPaths` | Rust struct + 平台检测 |
| `release/config_loader.py` | 73-106 | `ConfigValue` / `Config` | Rust struct |
| `release/config_loader.py` | 109-148 | `load_config()` | Rust fn（TOML + env + CLI 三层） |
| `release/config_loader.py` | 151-172 | `_load_toml_into` / `_flatten_toml` | Rust fn（toml crate） |
| `release/config_loader.py` | 179-206 | `SUPPORTED_ROLES` / `PLATFORM_ROLE_SUPPORT` / `check_role_supported` / `fail_closed_unsupported` | Rust const + fn |
| `cli/main.py` | 35-58 | `_SUBCOMMANDS`（59 个） | Rust clap 枚举 |
| `cli/main.py` | 60-102 | `_READONLY_*_ACTIONS` / `_WRITE_FLAGS` | Rust const |
| `cli/main.py` | 1098-1180 | `_is_readonly_command(cmd, sub_argv)` | Rust fn |
| `cli/main.py` | 1183-1197 | `_is_readonly_args(args)` | Rust fn（flag 模式） |
| `config.py` | 1319-1383 | `DAEMON_SOCKET_PATH` / `DAEMON_MODE` / `get_daemon_mode` / `is_daemon_required` / `is_daemon_available` | Rust const + fn（Phase 5-3 路由用，本阶段仅加载） |

## 3. API 契约

### 3.1 配置加载（A.1）

#### `platform_paths_detect() -> PlatformPaths`

**行为**：根据当前平台返回标准配置/数据目录路径。

**Python 真相源**：`release/config_loader.py:PlatformPaths.detect()` (L38-66)

**平台路径映射**：

| 平台 | system_config | user_config | system_data | user_data | runtime |
|---|---|---|---|---|---|
| Windows | `%ProgramData%\CallWarden\config.toml` | `%LOCALAPPDATA%\CallWarden\config.toml` | `%ProgramData%\CallWarden\data` | `%LOCALAPPDATA%\CallWarden\data` | None |
| macOS | `/Library/Application Support/CallWarden/config.toml` | `~/Library/Application Support/CallWarden/config.toml` | `/Library/Application Support/CallWarden/data` | `~/Library/Application Support/CallWarden/data` | None |
| Linux | `/etc/callwarden/config.toml` | `$XDG_CONFIG_HOME/callwarden/config.toml` | `/var/lib/callwarden` | `$XDG_STATE_HOME/callwarden` | `/run/callwarden` |

#### `load_config(cli_overrides: Option<HashMap<String, String>>, env_prefix: &str) -> Config`

**行为**：按优先级加载配置：CLI > env > user_config > system_config > default。

**Python 真相源**：`release/config_loader.py:load_config()` (L109-148)

**默认值**：

| key | default (Linux) | default (Windows/macOS) |
|---|---|---|
| `daemon_socket` | `/run/callwarden/callwarden.sock` | `""` |
| `log_level` | `info` | `info` |
| `max_workers` | `16` | `16` |
| `watcher_debounce_ms` | `250` | `250` |
| `cas_grace_days` | `7` | `7` |

#### `config_explain(config: &Config) -> Vec<ConfigEntry>`

**行为**：输出每个有效值的来源，secret 字段（含 `token`/`secret`/`password`/`api_key`/`private_key`）显示为 `***`。

**Python 真相源**：`release/config_loader.py:Config.explain()` (L95-106)

#### `check_role_supported(role: &str, platform: Option<&str>) -> bool`

**行为**：检查平台是否支持指定角色。

**角色矩阵**：

| 平台 | 支持的角色 |
|---|---|
| Windows | `local`, `client` |
| macOS | `local`, `client` |
| Linux | `local`, `client`, `agent`, `daemon`, `all` |

### 3.2 clap 命令树骨架（A.2）

#### `Cli` 枚举（59 个子命令）

**行为**：clap derive 枚举，对齐 `cli/main.py:_SUBCOMMANDS` 的 59 个子命令。每个子命令仅解析参数，不执行业务逻辑（返回 "not implemented" 错误）。

**Python 真相源**：`cli/main.py:_SUBCOMMANDS` (L35-58)

**子命令清单**（59 个）：

```
# 代码守护者架构（四大支柱）
guardrail, impact, review, evolution, hotspot, churn, defect,
task, vuln-blast, symbol-history, check-gate, test-impact,

# 运维
gc, doctor, install-agent, install-hook, rule, audit, bootstrap,
clone, fts,

# C8 Step #1: 8 大类 subcommand
workspace, refresh, stats, status,
search, grep, symbol, file, query, issues, tests,
callers, callees, call-chain, topo,
metrics, complexity, coupling, comment-coverage, uncommented,
function-issues, largest-fns, coupled-fns, fn-metrics,
git, semgrep,
coverage, who, ownership-map,
brief, map,
health-report,

# L5/N4
build-context, toolchain,
graph, config,

# 驾驶舱
dashboard,

# 回滚
rollback
```

**Rust binary**：新增 `rust_ext/src/bin/cw_cli.rs`，`Cargo.toml` 新增 `[[bin]] name = "cw"`。

#### 子命令参数解析

每个子命令的 clap struct 仅定义顶层参数（`--help` / `<action>`），不深入嵌套子命令的完整 argparse 定义（留给 Phase 5-1 C 阶段逐命令迁移）。

### 3.3 只读命令识别（A.3）

#### `is_readonly_command(cmd: &str, sub_argv: &[String]) -> bool`

**行为**：判断子命令是否为只读（不修改数据库）。

**Python 真相源**：`cli/main.py:_is_readonly_command()` (L1098-1180)

**只读规则**：

| 命令 | 只读条件 |
|---|---|
| `task` | action ∈ {list, show, findings} |
| `rule` | action ∈ {list, candidate, applicable, extract} |
| `audit` | action ∈ {verify, keys} |
| `bootstrap` | action == "status" |
| `clone` | action ∈ {list, stats} |
| `workspace` | action == "list" |
| `git` | action ∈ {log, show, stats, check-task, destructive-log} |
| `semgrep` | action ∈ {list, stats} |
| `coverage` | action ∈ {fn, uncovered} |
| `fts` | action == "status" |
| `graph` | action == "build-from-c" |
| `config` | action ∈ {explain, paths} |
| `rollback` | action ∈ {config, show, is-rolled-back} |
| `defect` | action ∈ {stats, list, show} |
| `gc` | action ∈ {list, inspect, db-cleanup} |
| `tests` | 不含 `--build` 且不含 `--import` |
| `doctor`/`check-gate`/`test-impact`/`hotspot`/`churn`/`evolution`/`impact`/`review`/`vuln-blast`/`symbol-history`/`guardrail` | 始终只读 |
| `search`/`grep`/`symbol`/`file`/`query`/`callers`/`callees`/`call-chain`/`topo`/`metrics`/`complexity`/`coupling`/`comment-coverage`/`uncommented`/`function-issues`/`largest-fns`/`coupled-fns`/`fn-metrics`/`who`/`ownership-map`/`brief`/`map`/`stats`/`status`/`health-report`/`dashboard` | 始终只读 |
| `refresh` | 始终写 |
| 其他 | 默认写（fail-safe） |

#### `is_readonly_args(flags: &CommandFlags) -> bool`

**行为**：判断 flag 模式命令是否为只读。

**Python 真相源**：`cli/main.py:_is_readonly_args()` (L1183-1197)

**写 flag 集合**：`refresh_all` / `refresh` / `watch` / `register_workspace` / `set_workspace` / `delete_workspace` / `restore_comment` / `restore_all_comments` / `coverage_import`

**规则**：不在 `_WRITE_FLAGS` 集合内的 flag 命令均为只读。

## 4. 行为契约

### D1: platform_paths_detect

| 场景 | 平台 | 期望输出 |
|---|---|---|
| D1.1 | Linux | system_config=/etc/callwarden/config.toml, runtime=/run/callwarden |
| D1.2 | Windows | system_config=%ProgramData%\CallWarden\config.toml, runtime=None |
| D1.3 | macOS | system_config=/Library/Application Support/CallWarden/config.toml, runtime=None |
| D1.4 | Linux + XDG_CONFIG_HOME | user_config=$XDG_CONFIG_HOME/callwarden/config.toml |
| D1.5 | Linux + XDG_STATE_HOME | user_data=$XDG_STATE_HOME/callwarden |

### D2: load_config

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D2.1 | 无配置文件 + 无 env + 无 CLI | 默认值（daemon_socket=/run/... on Linux） |
| D2.2 | 系统配置文件含 `log_level = "debug"` | log_level=debug, source=system_config |
| D2.3 | 环境变量 `CW_LOG_LEVEL=warning` | log_level=warning, source=env |
| D2.4 | CLI override `log_level=error` | log_level=error, source=cli |
| D2.5 | 三层都有，CLI 优先 | log_level=error, source=cli |
| D2.6 | 嵌套 TOML `[daemon] socket = "..."` | daemon.socket=..., source=user_config |
| D2.7 | 配置文件损坏 | 跳过（静默忽略，用默认值） |

### D3: config_explain

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D3.1 | 含 `api_key` 字段 | value=`***` |
| D3.2 | 含 `token` 字段 | value=`***` |
| D3.3 | 普通字段 `log_level` | value=实际值 |
| D3.4 | 按 key 字母排序 | 输出有序 |

### D4: check_role_supported

| 场景 | 平台 | 角色 | 期望 |
|---|---|---|---|
| D4.1 | linux | daemon | true |
| D4.2 | linux | agent | true |
| D4.3 | win32 | daemon | false |
| D4.4 | darwin | agent | false |
| D4.5 | win32 | local | true |
| D4.6 | unknown | local | false |

### D5: is_readonly_command

| 场景 | cmd | sub_argv | 期望 |
|---|---|---|---|
| D5.1 | task | [list] | true |
| D5.2 | task | [create] | false |
| D5.3 | search | [] | true |
| D5.4 | refresh | [--all] | false |
| D5.5 | audit | [verify] | true |
| D5.6 | audit | [rotate-key] | false |
| D5.7 | tests | [--history] | true |
| D5.8 | tests | [--build] | false |
| D5.9 | rollback | [config] | true |
| D5.10 | rollback | [register] | false |
| D5.11 | unknown_cmd | [] | false（fail-safe） |

### D6: clap 命令树骨架

| 场景 | 输入 | 期望 |
|---|---|---|
| D6.1 | `cw --help` | 列出 59 个子命令 |
| D6.2 | `cw stats` | "not implemented"（仅骨架） |
| D6.3 | `cw unknown-cmd` | clap 错误（exit 2） |
| D6.4 | `cw --version` | 版本号 |

## 5. 预期差异

1. **TOML 解析**：Python 用 `tomllib`（标准库 3.11+）或 `tomli`（fallback），Rust 用 `toml` crate。两者都遵循 TOML v1.0 规范，解析结果一致。
2. **环境变量类型**：Python env 值始终是字符串，Rust env 值也是 `String`。`max_workers` 等数值字段需在 `Config::get()` 时按需转换（与 Python `int(env_value)` 行为对齐）。
3. **clap vs argparse**：clap 的 `--help` 输出格式与 argparse 不同（clap 更紧凑），但子命令列表和参数定义对齐。差分测试聚焦于"子命令是否被识别"而非 help 文本逐字符对比。
4. **平台检测**：Python `sys.platform` 返回 `win32`/`darwin`/`linux`，Rust `std::env::consts::OS` 返回 `windows`/`macos`/`linux`。需映射对齐。
5. **Path 分隔符**：Python `pathlib.Path` 自动跨平台，Rust `std::path::Path` 也自动跨平台。差异在 Windows 路径分隔符（`\` vs `/`），但 `Path::join` 在两端都正确。
6. **fail_closed_unsupported 退出码**：Python `sys.exit(2)`，Rust `std::process::exit(2)`。

## 6. 实现计划

### A.1 配置加载器

1. **Rust 实现**：新增 `rust_ext/src/cli/config.rs`
   - `PlatformPaths` struct + `detect()`
   - `ConfigValue` / `Config` struct
   - `load_config()` fn（toml crate + env + CLI 三层）
   - `config_explain()` fn（secret 隐藏）
   - `check_role_supported()` / `fail_closed_unsupported()`
2. **差分测试**：D1-D4 测试矩阵
3. **PyO3 暴露**：在 `rust_ext/src/lib.rs` 注册 `load_config` / `platform_paths_detect` / `config_explain` / `check_role_supported` 4 个函数（供 Python wire-production 调用）

### A.2 clap 命令树骨架

1. **Rust 实现**：新增 `rust_ext/src/bin/cw_cli.rs`
   - `Cli` clap derive 枚举（59 个子命令变体）
   - 每个子命令 struct 仅含 `--help` + 位置参数 `[args]...`
   - `main()` 解析后返回 "not implemented" 错误
2. **Cargo.toml**：新增 `[[bin]] name = "cw" path = "src/bin/cw_cli.rs"`
3. **差分测试**：D6 测试矩阵（`cw --help` 子命令列表对比）

### A.3 只读命令识别

1. **Rust 实现**：新增 `rust_ext/src/cli/readonly.rs`
   - `READONLY_TASK_ACTIONS` / `READONLY_RULE_ACTIONS` 等 const 集合
   - `WRITE_FLAGS` const 集合
   - `is_readonly_command(cmd, sub_argv)` fn
   - `is_readonly_args(flags)` fn
2. **PyO3 暴露**：注册 `is_readonly_command` / `is_readonly_args` 2 个函数
3. **差分测试**：D5 测试矩阵（59 个子命令 × 多种 action）

## 7. 验收标准

1. **D1-D6 测试矩阵全部通过**：配置加载 + clap 骨架 + 只读识别行为与 Python 一致
2. **`cargo build --release --bin cw` 编译通过**：新增 `cw` binary 可执行
3. **`cw --help` 列出 59 个子命令**：与 Python `cw --help` 子命令列表一致
4. **`cw stats` 返回 "not implemented"**：骨架阶段不实现业务逻辑
5. **PyO3 暴露 6 个函数**：`load_config` / `platform_paths_detect` / `config_explain` / `check_role_supported` / `is_readonly_command` / `is_readonly_args`
6. **migration-manifest.md §32 Review 清单完整**
7. **迁移状态跟踪表 Phase 5-1 行更新**

## 8. 风险与注意事项

- **59 个子命令迁移工作量大**：本阶段仅骨架（clap 枚举 + "not implemented"），业务逻辑留给 Phase 5-1 C 阶段逐命令迁移
- **TOML 配置文件格式**：需确保 Rust `toml` crate 与 Python `tomllib` 解析结果一致（TOML v1.0 规范）
- **clap derive 宏编译时间**：59 个变体的枚举可能增加编译时间，需评估是否拆分为子模块
- **不修改 Python CLI**：Phase 5-1 是纯新增 Rust 实现，Python CLI 保持真相源。wire-production（Python 调 Rust）留给后续阶段
- **不涉及 rollback_config 登记**：骨架阶段不接入生产路径，无需登记 rollback

## 9. 与后续阶段的关系

| 阶段 | 交付物 | Phase 5-1 关系 |
|---|---|---|
| 5-1 A | 配置加载 + clap 骨架 + 只读识别 | **本阶段** |
| 5-1 B | local/enterprise/auto 路由 | 依赖 A.1 配置加载 |
| 5-1 C | 子命令业务逻辑垂直切片 | 依赖 A.2 clap 骨架 + A.3 只读识别 |
| 5-2 | Rust client/agent | 依赖 A.2 clap 框架 |
| 5-3 | 路由与兼容输出 | 依赖 A.1 配置加载 |
| 5-4 | 安装器/smoke | 依赖 5-1 稳定 binary |
