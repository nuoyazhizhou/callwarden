# W17 承接卡：`cw collab` 治理写命令面迁移到 HTTP authority

- **承接卡 task_id**：`T-1789301330757-87f33c34`（daemon 权威 `task.create` 创建，2026-09-13）
- **合同哈希**：`sha256:9ea4204cf2e70010bdfe330911f1798ca8f8c8aca72b83a6f18ac6b5167a6fe3`（contract_revision=1）
- **workspace 绑定**：`tb-T-1789301330757-87f33c34-4baea3ff12c2ea5c`（capture `wc-4baea3ff12c2ea5c-2283608820`）
- **承接来源**：`T-1788871227327-45c94bd8`（PYT 回归卡）
- **发现场景**：W12 承接卡 `T-1789274621921-e5464ad8` 的独立 Reviewer 盲审（2026-09-13）
- **裁决**：用户 2026-09-13 裁决 —— **新建独立承接卡** + `_handle_collab` 现有 **4 个治理写方法一并迁移**
- **裁决依据**：[pyt_regression_step4_handoff_backlog.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_handoff_backlog.md#L384-L408) 的 W17 节
- **建卡脚本**：[create_w17_collab_authority_migration_task.py](file:///c:/git_work/callwarden/deliverables/software-company/create_w17_collab_authority_migration_task.py)
- **状态**：改动尚未开始（工作树干净），由本卡承接全部实现、测试与文档更新。

---

## 1. 缺陷（逐行实证）

`cw collab`（`cli/main.py::_handle_collab`）的 4 个治理写方法均硬编码本地传输：

- `cli/main.py:16366-16387`：`from ..server.daemon_client import DaemonClient` →
  `client = DaemonClient.get_instance()` → `client.call_with_autostart(method, params)`。
- `server/daemon_client.py:1135-1141`：`DaemonClient.__init__` **恒定**构造
  `UnixDaemonRpcClient(...)`（`is_http_client = False`）→ 该路径**永不**走 HTTP authority。

而同一进程内的 lease 写路径走 HTTP authority：

- `cli/main.py:17345 _route_lease_write` → `cli/main.py:17369`
  `HttpDaemonRpcClient.get_instance() if is_http_transport_enabled() else UnixDaemonRpcClient()`。
- `cli/main.py:5119 route_task_write("task.handoff", …)`、`cli/main.py:5277 task.apply`、
  `cli/main.py:5340 task.close` 同样走 `route_task_write`（HTTP authority face）。

**后果**：同一 task 上两侧 authority 不一致。W12 Reviewer 实测（注册身份 `reviewer-wb-186loop`）：

| 传输面 | 命令 | 结果 |
|---|---|---|
| 本地（Named Pipe，经 `DaemonClient`） | `cw collab verdict …` | `E_TASK_WORKSPACE_UNBOUND`（`task_workspace_bindings` 缺失）/ `E_LEASE_NOT_FOUND`（`task=T-1789274621921-e5464ad8 role=reviewer 无 active lease`） |
| HTTP authority（`HttpDaemonRpcClient`） | `cw lease acquire` / `cw task handoff` | 成功 |

即：Reviewer 若按 `cw collab verdict` 默认传输面提交 verdict，会 fail-closed 卡死；
本次是绕过 CLI、直接经权威 HTTP 面完成 verdict（`V-47b9fa19cfbd42932dc24bc6`）与 handoff。

## 2. 为什么需要独立卡

- 落点在 `cli/**`，**不在** W12 卡 `allowed_paths`（W12 已闭环，`closed/completed`）。
- PYT 回归卡 `forbidden_paths` 明确含 `cli/**`，无法在其内修复。
- 与既有 C 桶承接卡 `T-1789290073049-6442e268` 的 C-11/C-12（`_agent_start`/`_agent_status`
  未迁移 `UnixDaemonRpcClient`）**同族但不同方法族**；本卡不触碰 `_agent_*`，避免与其重复实施。

## 3. 改动范围

### 3.1 allowed（本卡自有）

| 文件 | 改动 |
|---|---|
| `cli/main.py` | `_handle_collab` 4 个治理写方法（`snapshot.publish` / `verdict.submit` / `reveal.submit` / `gate.decide`）改为经 `route_rpc(..., 'GOVERNANCE_WRITE')`（HTTP authority face）；删除本地 `DaemonClient` 构造 |
| `server/daemon_client.py` | **仅当实证需要**时做最小增补（例如 `_NO_WORKSPACE_METHODS` 增补一个方法名）；必须在证据中给出「不做则 fail-closed」的实测依据，不得顺手重构 |
| `tests/test_task_verdict_cli.py` | 4 例改为对权威路由（`HttpDaemonRpcClient` 假体）断言 |
| `tests/test_cli_collab_snapshot_publish.py` | 2 例改为对权威路由断言 |
| `tests/` | 新增负例（HTTP 模式 + daemon 不可达 fail-closed） |
| `docs/cli_reference.md` | `collab` 小节命令形态更新（现文档仍为过期签名 `--verdict-id/--decision`） |
| `TOOLS.md` | 同上（`collab verdict` 行） |
| `.agents/skills/cw-task-loop/references/role-protocol.md` | 命令形态与必填参数与实现一致（含 daemon 强制的 `--view-manifest-hash`） |
| `deliverables/software-company/` | 卡片 / 合同 / 证据 |

### 3.2 forbidden

`rust_ext/**`、`db/**`、`scripts/refresh_shared_runtime.ps1`；
以及受保护生命周期写 `task.apply` / `task.close` / `task.supersede` / 状态伪造。
不得新增第二套 transport 抽象，不得引入本地 SQLite 回退路径。

## 4. 契约要点（单一真相源）

- **权威路由** = `server/daemon_client.py::route_rpc`（与 MCP 侧
  `server/tools/tools_collab.py:277` 已迁移实现同一函数）。
  MCP 侧 `submit_verdict` 已用 `_route('verdict.submit', {…}, 'GOVERNANCE_WRITE')`，
  CLI 侧必须收敛到同一真相源，而不是再写一套传输选择逻辑。
- `_handle_collab` 内**不得再出现** `DaemonClient.get_instance()` / `UnixDaemonRpcClient`。
- **fail-closed**：daemon 不可达时必须走既有 `_collab_governance_rejection` 结构化拒绝路径
  （Governance_Write fail closed），不得本地回退、不得静默降级。
- **幂等**：`verdict` 沿用显式 `--request-id`；其余方法由 `route_rpc` 生成
  `req-<uuid12>`，不得在 CLI 内重复造 request_id。
- **已知风险（须实测定论，不得臆断）**：`gate.decide` 入参只有 `gate_id`（无 `task_id`），
  `route_rpc` 会按 workspace-scoped 注入 `workspace_instance_id` / `workspace_root`。
  若 daemon `parse_request` 对该方法拒绝未知字段，则按 §3.1 对
  `server/daemon_client.py` 做**最小**增补并留证；`reveal.submit` / `verdict.submit`
  含 `task_id`，属 task-scoped，不应发生注入。

## 5. 受影响测试（必须一并更新，否则只是把缺陷换个地方）

| 测试 | 现状（钉死缺陷传输） |
|---|---|
| `tests/test_task_verdict_cli.py` | `_mock_daemon` monkeypatch `DaemonClient.get_instance`，断言 `call_with_autostart`（4 例） |
| `tests/test_cli_collab_snapshot_publish.py` | `_FakeDaemonClient.call_with_autostart` + 断言 `workspace.register` → `snapshot.publish` 序列（2 例） |

两文件共 6 例是当前缺陷行为的**回归锁**；迁移必须同步改写，并新增「HTTP 假体被调用」的正向断言。

## 6. 验收

1. **源码级断言**：`_handle_collab` 函数体内 `DaemonClient` / `UnixDaemonRpcClient`
   出现次数 = 0；4 个方法均经 `route_rpc`。
2. `pytest tests/test_task_verdict_cli.py tests/test_cli_collab_snapshot_publish.py` 全绿。
3. 新增负例：HTTP 模式下 `cw collab verdict` 命中注入的 `HttpDaemonRpcClient` 假体；
   daemon 不可达时 fail-closed（结构化拒绝 + 非零退出，无本地回退）。
4. 相关 HTTP RPC 回归面（`tests/test_cli_0*_http_rpc.py` 中与 collab / governance 相关者）零退化。
5. `git diff --check` exit 0。
6. 提交前缀使用**本卡 task_id**，不得复用 `T-1788871227327-45c94bd8` 或 W12 卡 id。

## 7. 与 W12 / C 桶卡的关系

- W12 卡 `T-1789274621921-e5464ad8` 已 `closed/completed`；本卡承接其 Reviewer 阶段披露的
  非阻塞 finding（W17），不修改 W12 的提交与结论。
- C 桶卡 `T-1789290073049-6442e268` 覆盖 C-04..C-07、C-10..C-12（`cli/main.py` 的 i18n 遮蔽、
  RPC 契约、`_agent_*` 未迁移）。本卡与其中 C-11/C-12 同族但不同函数，**不共享实施**；
  若后续要合并，需 Planner 裁决（当前 `planner_governance_v1` 未声明，见 W16）。
