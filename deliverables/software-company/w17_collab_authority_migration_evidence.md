# W17 证据清单：`cw collab` 治理写命令面迁移到 HTTP authority

- **承接卡**：`T-1789301330757-87f33c34`（W17 承接：cw collab 治理写命令面迁移到 HTTP authority（消除传输面不一致））
- **合同**：[w17_collab_authority_migration_contract.md](file:///c:/git_work/callwarden/deliverables/software-company/w17_collab_authority_migration_contract.md)
  （`sha256:9ea4204cf2e70010bdfe330911f1798ca8f8c8aca72b83a6f18ac6b5167a6fe3`，contract_revision=1）
- **workspace 绑定**：`tb-T-1789301330757-87f33c34-4baea3ff12c2ea5c`
- **执行身份**：`executor-workbuddy-186loop` / `inst-executor-wb-186loop` / `sess-w17-exec-20260913` / `workbuddy`（`--role implementer`）
- **step（6 个，daemon 权威投影）**：
  - `S-1789301330761-882ce1a0`（`implement`，`cli/main.py`：verdict 路由到权威面）
  - `S-1789301330761-882d4a50`（`implement`，`cli/main.py`：其余 3 方法 + 删除本地传输构造）
  - `S-1789301330761-882d7098`（`test`，`tests/test_task_verdict_cli.py`）
  - `S-1789301330761-882d90a0`（`test`，`tests/test_cli_collab_snapshot_publish.py`）
  - `S-1789301330761-882dacc0`（`implement`，`role-protocol.md` / `docs/cli_reference.md` / `TOOLS.md`）
  - `S-1789301330761-882dc8e0`（`release_verify`，`runtime/current`）
- **时间**：2026-09-13

---

## 1. 变更清单（工作树 → 本卡提交）

| 文件 | +/-（numstat） | SHA-256（工作树） |
|---|---|---|
| `cli/main.py` | +25 / -37 | `505420644926613100c7fb733d04ed862f8b68f75b8f3bcfe11e90a9559e0aa3` |
| `tests/test_task_verdict_cli.py` | +56 / -24 | `ff84cb5f3e7a08676063542d6ffc0135930edeb6e7cbab085f2da3f8319e44cf` |
| `tests/test_cli_collab_snapshot_publish.py` | +65 / -36 | `094f4b982d3b6f80a41fef0adbf699901d6c1c47377b7f0fc583452aa18b5f39` |
| `TOOLS.md` | +6 / -5 | `9f284e04d6ef017d0256e7ba589d43cf30ec11b572709c581102c0e0746399f2` |
| `docs/cli_reference.md` | +21 / -3 | `d3cf59ca59ace07c8dd65621ee4913187d708fdeaf10cbb86c258514b442521d` |
| `.agents/skills/cw-task-loop/references/role-protocol.md` | +23 / -0 | `15ea34118e5081ba2f526bb46dcbb215adc560df848e07e830c1b4bf4ece72c1` |

`git diff --numstat` 汇总：`6 files changed, 196 insertions(+), 105 deletions(-)`。

关键改动（与合同 §3.1 允许面逐条对应）：

1. **`cli/main.py`**：`_handle_collab` 的 4 个治理写方法（`snapshot.publish` / `verdict.submit` /
   `reveal.submit` / `gate.decide`）统一改为经 **`route_rpc(method, params, "GOVERNANCE_WRITE")`**
   （HTTP authority face，与 `cw lease` / `cw task` 写命令面同一真相源）；删除本地
   `DaemonClient.get_instance()` 构造、`call_with_autostart` 调用与 `degraded` 信封处理
   （`route_rpc` 原样返回 daemon `result`）。
2. **`snapshot.publish` 显式 workspace 绑定**：HTTP 模式下先
   `HttpDaemonRpcClient.get_instance().configure_workspace(params["workspace_root"])`，
   避免 `route_rpc` 兜底按调用进程 cwd 注册 workspace（同 `RpcDBProxy` 的 Blocker A 修复）；
   `workspace_instance_id` 只取 daemon `workspace.register` 的权威返回值，CLI 不自行派生。
3. **fail-closed 语义修正**：`DaemonUnavailableError` → `_collab_governance_rejection(...)` 输出
   Structured_Reason（`E_GOVERNANCE_WRITE_DEGRADED` + 平台恢复指引）后 `raise SystemExit(1)`；
   不再经 dispatcher 通用异常分支（那里会把失败表达成 `RC=0` 假成功）。无本地 SQLite 回退、无静默降级。
4. **import 面收敛**：新增 `HttpDaemonRpcClient` / `is_http_transport_enabled`；
   移除迁移后无引用的 `_workspace_snapshot_metadata`。
5. **文档与协议对齐**：`docs/cli_reference.md` 与 `TOOLS.md` 的 `collab verdict` 过期签名
   （`--verdict-id/--decision`）替换为实现真实必填签名（含 daemon 强制的 `--view-manifest-hash`）；
   `role-protocol.md` §7 新增「Reviewer verdict 提交（task-bound provenance）」命令形态。

## 2. 验收命令与结果

### 2.1 源码级断言（合同验收 ①）

```
python -c "<AST 提取 _handle_collab 函数体后正则计数>"
```

```
DaemonClient= 0
UnixDaemonRpcClient= 0
route_rpc calls= 1
```

即 `_handle_collab` 函数体内不再出现任何本地传输客户端构造，4 个方法均经 `route_rpc`。

### 2.2 6 例回归锁改写后全绿（合同验收 ②、③ 前半）

```
python -m pytest tests/test_task_verdict_cli.py tests/test_cli_collab_snapshot_publish.py -q
```

```
.......                                                                  [100%]
7 passed
```

（`test_task_verdict_cli.py` 5 例，其中 `test_task_bound_verdict_rejects_malformed_structured_inputs`
为 2 参数化；`test_cli_collab_snapshot_publish.py` 2 例。）

断言要点（对 HTTP authority 假体出向调用）：

| 用例 | 断言 |
|---|---|
| `test_task_bound_verdict_submits_complete_provenance` | 出向仅 `verdict.submit`；完整 provenance + identity + lease/fencing；显式 `--request-id` 未被覆盖（幂等）；**task-scoped 不得注入** `workspace_instance_id` / `workspace_root` |
| `test_task_bound_verdict_rejects_malformed_structured_inputs` | 非法 JSON → 本地拒绝、零出向调用 |
| `test_task_bound_verdict_requires_reviewer_instance_identity` | 缺 `--agent-instance-id` → 本地拒绝、零出向调用 |
| `test_task_bound_verdict_daemon_unavailable_fails_closed` | 非零 RC + `E_GOVERNANCE_WRITE_DEGRADED` + 恢复指引；已提交 1 次（无本地回退） |
| `test_collab_publish_routes_through_http_authority` | 出向序列 `["workspace.register", "snapshot.publish"]`；`client_view_root` = 绝对 workspace 路径；`workspace_instance_id` 取注册权威值；`request_id` 匹配 `req-[0-9a-f]{12}`；`snapshot_id` 由 daemon 继承故**不在** params |
| `test_collab_publish_fails_closed_without_authoritative_workspace_id` | 注册未返回权威 instance id → 非零 RC + 结构化拒绝；未继续发 `snapshot.publish` |

### 2.3 HTTP RPC 回归面零退化（合同验收 ④）

```
python -m pytest tests/test_cli_088_http_rpc.py tests/test_cli_087_http_rpc.py \
  tests/test_cli_086_http_rpc.py tests/test_cli_004_http_rpc.py tests/test_cli_02_http_rpc.py \
  tests/test_cli_090_http_rpc.py tests/test_cli_03_http_rpc.py tests/test_cli_096_http_rpc.py
```

```
47 passed, 4 skipped in 41.62s
```

（`test_cli_096` 断言 `task.*` / `lease.*` 写面映射 `GOVERNANCE_WRITE`；`test_cli_090` 断言
`task.governance_projection.get` 各恰 1 次；均为本卡的相邻权威面。）

### 2.4 `git diff --check`（合同验收 ⑤）

```
git diff --check
（无输出）  exit 0
```

## 3. `gate.decide` 风险实测定论（合同 §4 已知风险）

合同 §4 要求「实测定论，不得臆断」：`gate.decide` 入参无 `task_id`，`route_rpc` 会按
workspace-scoped 注入 `workspace_instance_id` / `workspace_root`。若 daemon 对该方法拒绝未知字段，
则须按 §3.1 对 `server/daemon_client.py` 做最小增补。

实测（HTTP 模式，`is_http_transport_enabled() = True`）：

```
ROUTE_ERR gate.decide DaemonRemoteError method_not_found method_not_found: 未知方法: gate.decide
ROUTE_ERR reveal.submit DaemonRemoteError method_not_found method_not_found: 未知方法: reveal.submit
```

**结论**：daemon 返回的是 `method_not_found`（而非 `invalid_params`），证明 workspace 注入
（`workspace_instance_id` + `workspace_root`）**未被 daemon 拒绝**。根因是 daemon 侧
`rust_ext/src/daemon/dispatch.rs::handle_collab_rpc` 尚未实现这两个方法
（`_ => Err(DaemonRpcError::method_not_found(method))`，与 Rust 测试
`test_collab_methods_route_to_collab_handler` 一致）。

因此合同 §3.1 对 `server/daemon_client.py` 的「最小增补」**条件不成立**，本卡**未修改**该文件；
`reveal.submit` / `gate.decide` 的 daemon 侧实现属后续 daemon 任务（本卡 forbidden 面含 `rust_ext/**`）。
CLI 已按统一权威路由提交，daemon 补齐后无需再改 CLI。

## 4. 披露：与本卡无关的既有失败

`tests/test_cli_011_http_rpc.py`（`_handle_assignment`）与 `tests/test_cli_057_http_rpc.py`
（`rule_list`）在本次运行中共 4 例失败：

```
FAILED tests/test_cli_057_http_rpc.py::test_cli057_rule_list_routes_to_daemon
FAILED tests/test_cli_057_http_rpc.py::test_cli057_rule_list_empty
FAILED tests/test_cli_011_http_rpc.py::test_cli011_assignment_create_routes_to_daemon
FAILED tests/test_cli_011_http_rpc.py::test_cli011_assignment_revoke_routes_to_daemon
```

复核方式：`git stash push cli/main.py tests/test_task_verdict_cli.py tests/test_cli_collab_snapshot_publish.py`
后重跑同两文件，失败集合与堆栈**逐字相同**（`ValueError: too many values to unpack` /
`TypeError: string indices must be integers`），确认与本卡无关（属 assignment/rules 命令面，
疑似其他承接卡范围）。本卡未触碰这些代码路径。

## 5. 纪律声明（合同 §3.2 / 验收 ⑥）

- 未修改 `rust_ext/**`、`db/**`、`scripts/refresh_shared_runtime.ps1`。
- 未做 direct SQLite writes；未执行 `task.apply` / `task.close` / `task.supersede` / 状态伪造。
- 未新增第二套 transport 抽象，未引入本地 SQLite 回退路径。
- reviewer lease raw token 仅进程内内存使用，未落入任何文件 / 日志 / evidence。
- 提交前缀使用本卡 `task_id`（`[T-1789301330757-87f33c34]`），未复用
  `T-1788871227327-45c94bd8`（PYT 卡）或 W12 卡 id。
