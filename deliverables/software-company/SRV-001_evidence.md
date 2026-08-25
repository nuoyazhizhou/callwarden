# SRV-001 完成证据（zero-authority evidence）

**任务：** `T-1787323460311-ae9d7d30` — server mcp common Python authority → Rust daemon
**父任务：** `T-1787293451688-c14b1e44`（Call Warden A′ migration）
**port_type：** `service_projection`
**执行角色：** Executor（worktree `pilot-srv-001` @ `75cf101`）

---

## 1. 不变量（invariants）

Python 最终只保留 HTTP client / JSON 序列化 / 配置读取等非业务适配职责；数据库连接、
schema/authority decision、业务 SQL 全部位于 Rust daemon。禁止将失败降级回 Python SQLite。

## 2. Before / After AST scan（server/_mcp_common.py）

### Before（迁移前）
`_get_db_path_for_daemon` 直接调用 `get_db()` + `get_project_db_path(db.workspace_root)`，
在 Python 侧打开/解析本地 SQLite 路径：

```python
def _get_db_path_for_daemon() -> str:
    db = get_db()
    return get_project_db_path(db.workspace_root)
```

### After（迁移后，行 47–57）
仅经 daemon RPC 取权威路径，无任何 SQLite / `get_db` / `get_project_db_path` 本地计算：

```python
def _get_db_path_for_daemon() -> str:
    result = _call_daemon_rpc("mcp.common.get_db_path_for_daemon", {})
    if isinstance(result, dict):
        return result.get("db_path", "")
    return result or ""
```

### 静态命中核对（grep）
| 检查项 | 结果 |
|---|---|
| `_get_db_path_for_daemon` 函数体内 `get_db` 调用 | **0**（已移除） |
| `_get_db_path_for_daemon` 函数体内 `get_project_db_path` 调用 | **0**（已移除，导入同步删除） |
| `_get_db_path_for_daemon` 函数体内 `sqlite3` / 业务 SQL | **0** |
| 模块内 `get_db` 残留 | 保留 `get_db()` 定义（行 62）供 13 个其他 importer 使用，**不**被本函数调用 |
| `_call_daemon_rpc` fail-closed | 保留：daemon 不可用抛 `DaemonUnavailableError`，绝不回退本地 SQLite/CodeGraphDB |

> 语义等价性：`config.get_project_db_path(project_root)` 忽略入参、固定返回
> `$HOME/.callwarden/callwarden.db`；Rust `default_authority_task_db_path()` 解析同一路径
> （Windows `USERPROFILE` / 其余 `HOME`）。故 RPC 返回值与原本地路径**完全一致**。

## 3. Rust native authority 接线（4 处）

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `rust_ext/src/daemon/mod.rs:160` | `pub mod _mcp_common_handlers;`（新模块声明） |
| 2 | `rust_ext/src/daemon/dispatch.rs:1786` | `CONVERGENCE_RPC_METHODS` 数组新增 `"mcp.common.get_db_path_for_daemon"`（门控进入收敛） |
| 3 | `rust_ext/src/daemon/snapshot_state.rs:2294,2355` | `handle_convergence_rpc` 新增 `use super::_mcp_common_handlers as mcp;` + match 分支 `=> mcp::handle_get_db_path_for_daemon(params)` |
| 4 | `rust_ext/src/daemon/http_server.rs:1582` | `build_capability_registry` 新增 `add("mcp.common.get_db_path_for_daemon", ..., "rust_native", "available", "read_only", "authority", ...)` capability 注册 |

### Handler 语义（`_mcp_common_handlers.rs:23`）
- 数据源：`config::default_authority_task_db_path()`（daemon 配置，不打开 SQLite）。
- 身份控制：传输层已保证 peer 身份（SO_PEERCRED / 命名管道 SID）；可选
  `workspace_instance_id` 仅回显归因，不改写返回的全局权威路径。
- fail-closed：`HOME`/`USERPROFILE` 缺失时返回稳定错误码
  `authority_task_db_unconfigured`，绝不回退本地 SQLite。
- 返回：`{"db_path": "<绝对路径>", "workspace_instance_id"?": "<可选>"}`。

## 4. Fixture 负向矩阵（tests/test_srv_001.py，7/7 通过）

| 场景 | 期望 |
|---|---|
| success | daemon 返回 `db_path` → 函数返回该路径 |
| success（带 workspace_instance_id） | 正常返回路径 |
| invalid | daemon 返回缺 `db_path` 的响应 → graceful 返回 `""`，不抛、不回退 SQLite |
| authority | daemon 拒绝 → 抛 `DaemonUnavailableError`（fail-closed） |
| unavailable | daemon 不可用 → 抛 `DaemonUnavailableError` |
| restart | 首次不可用后恢复 → 第二次返回路径 |
| 不变量 | `_get_db_path_for_daemon` 不再调用 `get_db`（monkeypatch 断言 `calls == []`） |

运行命令（worktree 经临时 junction 暴露为 `callwarden` 包）：

```bash
PYTHONPATH=C:/git_work/callwarden-wt .venv_test/Scripts/python.exe -m pytest \
  --import-mode=importlib deliverables?/.../tests/test_srv_001.py -q
# 结果：.......  7 passed
```

## 5. Rust 编译验证

`cargo check`（MSVC 环境，`source scripts/msvc-env.sh`）通过；`_mcp_common_handlers` 模块
含单测 `test_handler_contract` / `test_echoes_workspace_instance_id`。

## 6. Handoff manifest

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现 HTTP/daemon success 与负向矩阵，并以静态扫描和运行时探针核验
              server/_mcp_common.py 已无 DB authority 或业务 fallback。
  reason: SRV-001 将 server mcp common 子系统的 Python authority 下沉至 Rust daemon；
          完成态要求 Python 仅为 thin client/adapter。
  independence_requirement: required
```

## 7. step 完成映射

| step | action | 文件 | status |
|---|---|---|---|
| 0 | port_rust_authority | `_mcp_common_handlers.rs` + dispatch/snapshot/http_server 接线 | 完成（cargo check 通过） |
| 1 | retire_python_authority | `server/_mcp_common.py::_get_db_path_for_daemon` | 完成（无 get_db / 无 SQLite） |
| 2 | fixture_negative_matrix | `tests/test_srv_001.py` | 完成（7/7 通过） |
| 3 | zero_authority_evidence | `deliverables/software-company/SRV-001_evidence.md` | 完成（本文件） |
