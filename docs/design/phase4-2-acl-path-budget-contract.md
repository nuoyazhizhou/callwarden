# Phase 4-2 契约：UID/workspace ACL、路径安全与资源预算

**Task ID**: `T-1785218261435-5ec3c722` 的兄弟任务 `T-1785218296649-04b5c2eb`
**状态**: contract
**日期**: 2026-07-28

## 1. 范围

Phase 4-2 迁移 daemon 的 UID/workspace ACL 边界、路径安全校验和资源预算（QueryBudget）到 Rust PyO3 暴露层。

**涉及**：
- 路径安全校验：`validate_owned_path` / `check_path_within_workspace`
- admin UID 判定：`is_admin_uid` / `current_daemon_uid_py`
- workspace owner 校验：`check_workspace_owner`
- QueryBudget 配置与运行时跟踪

**不涉及**（保持 Python）：
- AuditLogger（Python 已完整实现，不在性能热路径）
- WorkspaceRegistry（涉及 DB 连接管理，保持 Python）
- `_validate_snapshot_fd`（Linux fcntl 特定，保持 Python）

## 2. Python 真相源

| Python 文件 | 函数/类 | 迁移方式 |
|---|---|---|
| `server/daemon_server.py:_validate_owned_path` | 路径规范化 + UID 校验 | Rust 纯函数 |
| `server/daemon_server.py:_is_admin_peer` | admin UID 三层判定 | Rust 纯函数（不含 admin_uids 配置扩展） |
| `server/daemon_server.py:_current_uid` | 当前进程 UID | Rust 纯函数 |
| `server/daemon_server.py` workspace.file.refresh | 路径逃逸检查 | Rust 纯函数 |
| `server/query_budget.py:QueryBudget` | 资源预算配置 + 运行时跟踪 | Rust pyclass |

## 3. API 契约

### 3.1 路径安全校验

#### `validate_owned_path(path: str, peer_uid: u32, require_file: bool) -> String`

**行为**：
1. `canonicalize(path)` 解析所有 symlink 为绝对路径
2. `require_file=True` 校验是文件；`False` 校验是目录
3. Unix + `peer_uid != 0` 时，校验 `metadata.uid() == peer_uid`
4. Windows 跳过 UID 检查

**返回**：canonicalize 后的绝对路径

**错误码**（PyRuntimeError "code: message" 格式）：
- `path_not_found`: 路径不存在 / 类型不匹配
- `path_forbidden`: Unix 下 `metadata.uid() != peer_uid` 且 `peer_uid != 0`

**Python 真相源**：`server/daemon_server.py:_validate_owned_path` (L1438-1455)

#### `check_path_within_workspace(abs_path: str, host_real_root: str) -> ()`

**行为**：
1. `canonicalize(abs_path)` 和 `canonicalize(host_real_root)`
2. 校验 `real_abs == real_host_root` 或 `real_abs.startswith(real_host_root + sep)`
3. 否则抛 `path_escape` 错误

**Python 真相源**：`server/daemon_server.py` workspace.file.refresh (L944-956)

### 3.2 admin UID 判定

#### `is_admin_uid(uid: u32) -> bool`

**行为**：
- `uid == 0`（root）→ true
- `uid == current_daemon_uid()`（daemon 自己）→ true
- 其他 → false

**注意**：不含 `DaemonConfig.admin_uids` 配置扩展。Python 端的第三层判定（`uid in admin_uids`）由 Python 调用方在 Rust 调用后补充检查。

**Python 真相源**：`server/daemon_server.py:_is_admin_peer` (L1416-1420)

#### `current_daemon_uid_py() -> u32`

**行为**：
- Unix: `libc::getuid()`
- Windows: 返回 1000（P1-1 修复，与测试 `current_uid()` 对齐）

**Python 真相源**：`server/daemon_server.py:_current_uid` (L1410-1412)

### 3.3 workspace owner 校验

#### `check_workspace_owner(owner_uid: i64, peer_uid: u32) -> ()`

**行为**：
- `owner_uid == peer_uid` → 通过
- `peer_uid == 0`（root）→ 通过
- 否则抛 `workspace_forbidden` 错误

**注意**：不查 DB，只做纯比较。Python 调用方先查 `daemon_workspaces` 表获取 `owner_uid`，再调用此函数校验。

**Python 真相源**：`server/daemon_server.py:_owned_workspace` 内部比较逻辑

### 3.4 QueryBudget

#### `budget_create(max_depth, max_nodes, timeout_ms, max_results, frontier_limit) -> dict`

**行为**：创建预算配置 dict，对齐 Python `QueryBudget` 5 字段。

**默认值**（对齐 Python）：
- `max_depth=5`, `max_nodes=1000`, `timeout_ms=5000`, `max_results=100`, `frontier_limit=500`

#### `budget_preset(name: str) -> dict`

**行为**：返回预设预算配置。

| name | max_depth | max_nodes | timeout_ms | max_results | frontier_limit |
|---|---|---|---|---|---|
| `default` | 5 | 1000 | 5000 | 100 | 500 |
| `deep` | 10 | 5000 | 10000 | 500 | 500 |
| `shallow` | 3 | 100 | 1000 | 20 | 500 |
| `unlimited` | 100 | 1000000 | 300000 | 100000 | 10000 |

**Python 真相源**：`server/query_budget.py:default_budget` / `deep_budget` / `shallow_budget` / `unlimited_budget`

#### `budget_tracker_new(budget: dict) -> dict`

**行为**：创建运行时跟踪器，包含：
- `budget`: 预算配置
- `start_time`: 当前时间戳
- `visited_count`: 0
- `exceeded`: false
- `exhausted_reason`: null

#### `budget_tracker_visit_node(tracker: dict) -> bool`

**行为**：
1. `visited_count += 1`
2. 检查 `visited_count > max_nodes` → 设 `exceeded=true`, `exhausted_reason="max_nodes"`, 返回 false
3. 检查超时 `elapsed > timeout_ms` → 设 `exceeded=true`, `exhausted_reason="timeout"`, 返回 false
4. 返回 true

**Python 真相源**：`server/query_budget.py:QueryBudget.visit_node`

#### `budget_tracker_truncate_results(tracker: dict, results: list) -> list`

**行为**：截断 results 到 `max_results` 长度。

**Python 真相源**：`server/query_budget.py:QueryBudget.truncate_results`

## 4. 行为契约

### D1: validate_owned_path 基本行为

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D1.1 | 存在的文件路径，require_file=True，owner 匹配 | 返回 canonicalize 后路径 |
| D1.2 | 存在的目录路径，require_file=False，owner 匹配 | 返回 canonicalize 后路径 |
| D1.3 | 不存在的路径 | PyRuntimeError("path_not_found: ...") |
| D1.4 | require_file=True 但路径是目录 | PyRuntimeError("path_not_found: ...") |
| D1.5 | Unix 下 owner 不匹配（peer_uid != 0） | PyRuntimeError("path_forbidden: ...") |
| D1.6 | peer_uid=0（root）跳过 owner 检查 | 返回 canonicalize 后路径 |
| D1.7 | Windows 跳过 UID 检查 | 返回 canonicalize 后路径 |

### D2: check_path_within_workspace 路径逃逸检查

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D2.1 | abs_path == host_real_root | 通过（无异常） |
| D2.2 | abs_path 是 host_real_root 的子路径 | 通过 |
| D2.3 | abs_path 是 host_real_root 的兄弟路径 | PyRuntimeError("path_escape: ...") |
| D2.4 | abs_path 完全在 host_real_root 之外 | PyRuntimeError("path_escape: ...") |

### D3: is_admin_uid / current_daemon_uid_py

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D3.1 | uid=0（root） | true |
| D3.2 | uid=current_daemon_uid() | true |
| D3.3 | uid=其他 | false |
| D3.4 | current_daemon_uid_py() | Unix: getuid()，Windows: 1000 |

### D4: check_workspace_owner

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D4.1 | owner_uid == peer_uid | 通过 |
| D4.2 | peer_uid == 0（root） | 通过 |
| D4.3 | owner_uid != peer_uid | PyRuntimeError("workspace_forbidden: ...") |

### D5: budget_create / budget_preset

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D5.1 | budget_create() 默认参数 | dict 含 5 字段，值与 Python default_budget 一致 |
| D5.2 | budget_create(max_depth=10) | dict max_depth=10 |
| D5.3 | budget_preset("default") | 与 Python default_budget 一致 |
| D5.4 | budget_preset("deep") | 与 Python deep_budget 一致 |
| D5.5 | budget_preset("shallow") | 与 Python shallow_budget 一致 |
| D5.6 | budget_preset("unlimited") | 与 Python unlimited_budget 一致 |
| D5.7 | budget_preset("invalid") | PyRuntimeError("unknown_preset: ...") |

### D6: budget_tracker 行为

| 场景 | 输入 | 期望输出 |
|---|---|---|
| D6.1 | visit_node 未超限 | true，visited_count=1 |
| D6.2 | visit_node 超过 max_nodes | false，exhausted_reason="max_nodes" |
| D6.3 | visit_node 超时 | false，exhausted_reason="timeout" |
| D6.4 | truncate_results 超过 max_results | 截断到 max_results 长度 |
| D6.5 | truncate_results 未超 max_results | 原样返回 |

## 5. 预期差异

1. **路径规范化**：Rust `std::fs::canonicalize` 单步解析所有 symlink；Python `os.path.realpath(os.path.abspath(path))` 双步。在无 symlink 时行为一致；有 symlink 时 Rust 更严格（完全解析），Python `realpath` 也会解析 symlink，行为基本一致。

2. **is_admin_uid 不含 admin_uids 配置扩展**：Python `_is_admin_peer` 第三层判定（`uid in admin_uids`）由 Python 调用方补充。Rust 只暴露前两层（root + daemon 自己）。

3. **QueryBudget 字段**：Rust `daemon/budget.rs` 原有 3 字段（max_depth/max_nodes/timeout_ms），本契约扩展到 5 字段（补 max_results/frontier_limit）。BudgetTracker 用 dict 而非 pyclass（简化 PyO3 暴露）。

4. **Windows UID**：Rust `current_daemon_uid_py()` 返回 1000；Python `_current_uid()` 在无 `os.getuid` 时返回 0。差分测试需处理此差异。

## 6. 实现计划

1. **contract**（本步骤）：编写契约文档
2. **implement**：在 `rust_ext/src/daemon_query.rs` 扩展 9 个 PyO3 API（validate_owned_path / check_path_within_workspace / is_admin_uid / current_daemon_uid_py / check_workspace_owner / budget_create / budget_preset / budget_tracker_new / budget_tracker_visit_node / budget_tracker_truncate_results）
3. **differential-test**：编写差分测试（D1-D6 共 ~30 个用例）
4. **wire-production**：在 `server/daemon_server.py` 和 `server/query_budget.py` 接入 Rust 短路
5. **verify + refresh + review**：验证 + 刷新 + 更新 manifest

## 7. 验收标准

1. `cargo check` 通过（0 error）
2. 差分测试全部通过（D1-D6）
3. wire-production 接入后 Python 端行为不变
4. rollback_config 登记 `rust_daemon_acl_path_budget`
5. rollback 开关切换验证通过
