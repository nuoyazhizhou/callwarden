# SRV-002 完成证据（zero-authority evidence）

**任务：** `T-1787323460404-b425b074` — server audit log Python authority → Rust daemon
**父任务：** `T-1787293451688-c14b1e44`（Call Warden A′ migration）
**port_type：** `service_projection`
**执行角色：** Executor（worktree `pilot-srv-002`，路径 `C:/git_work/callwarden-wt/callwarden`）
**执行路线：** Route B —— 完整 thin-client 重构（经 AskUserQuestion 由用户选定，替代原 SRV-002
侦察中发现的"仅退休 2 个符号"不自洽 scope）

---

## 1. 不变量（invariants）

- Python `AuditLogger` 退化为纯 daemon RPC 薄客户端：不再 `import sqlite3`、不再持有内存/磁盘
  缓冲、不再打开本地 `audit.db`；所有记录/查询经 `mcp.audit_log.*` 发往 daemon。
- daemon（Rust `audit_log_handlers`）成为 `audit_log` 表（SQLite，路径 = `config.audit_db_path`，
  默认 `/var/log/callwarden/audit.log`）的**唯一写者**。
- fail-closed：`audit_db_path` 为空时 `handle_get_conn`/`handle_init_db` 返回稳定错误码
  `audit_db_unconfigured`；`_call_daemon_rpc` 在 daemon 不可用时抛 `DaemonUnavailableError`，
  **绝不**回退本地 SQLite/内存缓冲充当业务存储。
- 与 Python `schema_migrator`（`_audit_v1`/`_audit_v2`）共用同一 `audit_log` 表 schema 与
  4 个索引，保证 daemon 与 Python daemon 指向同一权威 DB 文件。

## 2. Before / After AST scan（server/audit_log.py）

### Before（迁移前）
`AuditLogger` 是 SQLite 权威拥有者：模块级 `import sqlite3` + `AUDIT_LOG_DDL`，类内
`_get_conn` / `_init_db` / `_buffer` / `_flush_to_db` / `_write_single` 打开/写入本地
`audit.db`，并维护内存缓冲做批量 flush。

### After（迁移后，route B）
模块**删除全部 SQLite 权威**：无 `import sqlite3`、无 `AUDIT_LOG_DDL`、无 `_get_conn` /
`_init_db` / `get_db` / `_buffer`。`AuditLogger` 仅保留枚举、`AuditEvent`、便捷方法与
`_ensure_init()`（懒调 `mcp.audit_log.init_db`）；所有动作经模块底新增的
`_call_daemon_rpc(method, params)`（包装 `from ._mcp_common import _call_daemon_rpc`）。

```python
# 记录（薄客户端，即写即发）
def log(self, event: AuditEvent) -> None:
    self._ensure_init()
    _call_daemon_rpc("mcp.audit_log.append", {"event": event.to_dict()})

# 查询（薄客户端）
def query(self, **filters) -> List[Dict[str, Any]]:
    self._ensure_init()
    return _call_daemon_rpc("mcp.audit_log.query", params) or []
```

### 静态命中核对（AST scan，`tests/test_srv_002.py::test_no_sqlite_authority_in_source`）
| 检查项 | 结果 |
|---|---|
| `import sqlite3` / `from sqlite3 import` | **0**（已移除） |
| 函数/类/属性名 `sqlite3` | **0** |
| `_get_conn` 符号 | **0**（已移除） |
| `_init_db` 符号 | **0**（已移除） |
| `get_db` 符号 | **0**（已移除） |
| `AUDIT_LOG_DDL` 符号 | **0**（已移除） |

> 运行时指纹：同上测试用 monkeypatch 将 `callwarden.server.audit_log._call_daemon_rpc`
> 替换为内存态 `FakeAuditDaemon`，并断言 `AuditLogger` 实例**不**具有 `_get_conn` /
> `_init_db` / `get_db` 属性（33 行 `test_audit_logger_has_no_local_sqlite_authority`）。

## 3. Rust native authority 接线

新增模块 `rust_ext/src/daemon/audit_log_handlers.rs`（7 个 handler + `AUDIT_LOG_DDL` + 3 单测），
并在收敛面 4 处 + 配置链路接线：

| # | 文件:行 | 改动 |
|---|---|---|
| 1 | `rust_ext/src/daemon/mod.rs:165` | `pub mod audit_log_handlers;`（新模块声明） |
| 2 | `rust_ext/src/daemon/dispatch.rs:1788-1794` | `CONVERGENCE_RPC_METHODS` 新增 7 项 `mcp.audit_log.{get_conn,init_db,append,query,count,clear,get_stats}`（门控进入收敛） |
| 3 | `rust_ext/src/daemon/snapshot_state.rs:2303,2332,2388-2413` | `handle_convergence_rpc` 新增 `use super::audit_log_handlers as audit;` + `open_audit` 闭包（类比 `open_write`，空路径返回 `audit_db_unconfigured`）+ 7 个 match 分支 |
| 4 | `rust_ext/src/daemon/http_server.rs:1584-1590` | `build_capability_registry` 新增 7 个 `add(...)`，owner 全为 `T-1787323460404-b425b074#SRV-002`，op 类按语义（get_conn/query/count/get_stats=read_only；init_db/append/clear=write），scope=authority |
| 5 | `rust_ext/src/daemon/config.rs:87-88,116,142,244-246` | `DaemonConfig` 新增 `audit_db_path: PathBuf`（default `/var/log/callwarden/audit.log`，`CW_DAEMON_AUDIT_DB` 可覆盖） |
| 6 | `rust_ext/src/daemon/workspace.rs:1092,1132,1147,1179` | `WorkspaceDaemonState` 新增 `audit_db_path` 直接字段（对齐既有 `codegraph_db_path_template` 模式）+ `with_audit_db_path` builder |
| 7 | `rust_ext/src/daemon/snapshot_state.rs:134-139` | `SnapshotDaemonState::with_audit_db_path` 透传到 base（与 `with_codegraph_db_path_template` 对称） |
| 8 | `rust_ext/src/bin/cw_daemon.rs:346,2313` | 两处生产 `serve` 路径经 `with_audit_db_path(config.audit_db_path.clone())` 将配置注入 daemon 运行时状态 |

### Handler 语义（`audit_log_handlers.rs`）
- 数据源：`config.audit_db_path`；写操作在本模块内 `Connection::open(path)` 打开 SQLite。
- `handle_get_conn(&Path)`：返回权威路径 `{"db_path": ...}`；空路径 → `audit_db_unconfigured`
  （fail-closed，绝不回退）。
- `handle_init_db`：执行 `AUDIT_LOG_DDL`（建 `audit_log` 表 + 4 索引，与 `schema_migrator` 一致）。
- `handle_append`：取 `params.event`，必填 `event_id/event_type/action`，否则 `invalid_params`；
  `INSERT OR REPLACE`。
- `handle_query`/`handle_count`：start_time/end_time/event_type/actor_uid/result 过滤，倒序。
- `handle_clear`：`DELETE FROM audit_log`（仅测试/运维）。
- `handle_get_stats`：total / by_type / by_result。
- 身份控制：传输层已保证 peer 身份（SO_PEERCRED / 命名管道 SID）。

## 4. Fixture 负向矩阵（tests/test_srv_002.py + tests/test_phase8_audit_log.py，52/52 通过）

`tests/test_srv_002.py` 覆盖 task step[2] `fixture_negative_matrix`：
`["success", "invalid", "authority", "unavailable", "restart"]` + 零权威 AST 扫描。

| 场景 | 期望 |
|---|---|
| success | `logger.log(...)` → `query()` 返回 1 条且 `action` 正确 |
| invalid | 缺 `action` → daemon 端 `invalid_params`（经 `FakeAuditDaemon`） |
| authority（路径归属） | `mcp.audit_log.get_conn` 返回 daemon 权威路径 `/var/log/callwarden/audit.log` |
| authority（无本地权威） | `AuditLogger` 实例无 `_get_conn`/`_init_db`/`get_db` 属性 |
| authority（unconfigured） | 空 `audit_db_path` → `audit_db_unconfigured` |
| unavailable | daemon 不可用 → 抛 `DaemonUnavailableError`，不回退本地 SQLite |
| restart | 首次不可用 → 恢复后第二次成功 |

`tests/test_phase8_audit_log.py`（原 839 行本地 SQLite 套件）全部迁到 daemon-backed
`FakeAuditDaemon` 内存权威：枚举 / `AuditEvent`（序列化·ID 格式·唯一性）/ 记录·查询·统计 /
6 类便捷方法 / 跨实例共享（daemon 权威）/ 全局单例 / 7 类安全审计场景（越权·symlink 逃逸·
TCP token 错误·token 生成/撤销·审计轨迹完整性），共 45 项。

运行命令（worktree 已落位于 `callwarden-wt/callwarden`，pytest 以 `callwarden` 为根包名）：
```bash
cd C:/git_work/callwarden-wt/callwarden
PYTHONPATH=C:/git_work/callwarden-wt .venv_test/Scripts/python.exe -m pytest \
  tests/test_srv_002.py tests/test_phase8_audit_log.py -q --import-mode=importlib
# 结果：52 passed
```

## 5. Rust 编译验证

- `cargo check -p callwarden-core`（MSVC 环境，`source scripts/msvc-env.sh`）通过（lib + bin，仅既有警告）。
- `cargo test -p callwarden-core audit_log_handlers`：**3 passed**
  （`test_get_conn_contract` / `test_init_append_query_count_clear_stats` /
  `test_append_invalid_params`，全部基于 `:memory:` 连接，不触碰本地 SQLite）。

## 6. Handoff manifest

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现 HTTP/daemon success 与负向矩阵，并以静态扫描（AST 禁 sqlite3/
              _get_conn/_init_db/get_db/AUDIT_LOG_DDL）与运行时探针（monkeypatch
              _call_daemon_rpc 为 FakeAuditDaemon）核验 server/audit_log.py 已无
              SQLite 权威或业务 fallback；并确认 Rust 7 handler 与 capability 注册一致。
  reason: SRV-002 将 server audit log 子系统的 Python SQLite authority 完整下沉至
          Rust daemon；完成态要求 Python 仅为 thin client/adapter（route B 完整重构）。
  independence_requirement: required
  note: 执行路线 Route B（完整 thin-client 重构）由用户在侦察阶段经 AskUserQuestion 选定；
        原"仅退休 _get_conn/_init_db 2 符号"scope 与"no SQLite"检查不自洽，已结构化
        交接后升级为完整重构。worktree 由 srv-002 重命名为 callwarden（pytest 根包名
        须为 callwarden，否则 __init__.py 被以 srv-002 名称导入导致相对导入失败）。
```

## 7. step 完成映射

| step | PK | action | 文件 | status |
|---|---|---|---|---|
| 0 | `S-1787323460406-b43de748` | port_rust_authority | `audit_log_handlers.rs` + dispatch/snapshot/http_server/mod 接线 + config/workspace 字段链路 | 完成（cargo check + 3 单测通过） |
| 1 | `S-1787323460406-b4408534` | retire_python_authority | `server/audit_log.py`（删 sqlite3/`_get_conn`/`_init_db`/`get_db`/`AUDIT_LOG_DDL`/缓冲） | 完成（AST 扫描 0 命中） |
| 2 | `S-1787323460406-b4419d48` | fixture_negative_matrix | `tests/test_srv_002.py` + `tests/test_phase8_audit_log.py` | 完成（52/52 通过） |
| 3 | `S-1787323460406-b4426f70` | zero_authority_evidence | `deliverables/software-company/SRV-002_evidence.md` | 完成（本文件） |
