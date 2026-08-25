# CLI-087 Evidence Manifest — cw local-resolve-finding → Rust daemon HTTP thin client

**Task ID:** T-1787322799910-eb9a737c
**Card:** CLI-087 (cli_command_projection)
**Worktree:** C:/git_work/callwarden-wt/cli-087 (branch `pilot-cli-087`)
**Daemon under test:** http://127.0.0.1:12376 (Rust daemon, live, not rebuilt)
**Test module:** tests/test_cli_087_http_rpc.py

## 1. Thin-client acceptance (step1: `thin_cli_client`)

`cli/main.py::_local_resolve_finding`（`cli/main.py:4618` 的 `resolve-finding` action）原为
本地业务路径：

```python
def _local_resolve_finding():
    return db.resolve_task_quality_finding(
        opts.finding_id, resolution=opts.resolution, resolved_by=opts.by)
```

已改为 **fail-closed**（与同文件 `findings` action 一致）：

```python
def _local_resolve_finding():
    raise DaemonUnavailableError(
        "task.resolve_quality_finding 仅由 daemon 提供；local 模式禁止本地 "
        "resolve_task_quality_finding 业务路径，请使用 daemon 模式"
    )
```

命令唯一写路径为 `route_task_write("task.resolve_quality_finding", {...}, _local_resolve_finding)`
（`cli/main.py:4624`）。即：

- **主路径为 HTTP-only**：`route_task_write` 优先经 `HttpDaemonRpcClient` 直达 Rust daemon 的
  `task.resolve_quality_finding` 权威写点，不经过任何本地 SQLite / Unix socket 业务路径。
- **离线 fallback 已禁用**：`_local_resolve_finding` 不再调用 `db.resolve_task_quality_finding`，
  仅 `raise DaemonUnavailableError`（forbidden local fallback），符合任务「禁止保留 hidden
  local fallback」。

### Source scan（移除本地业务路径的证明）

```text
# 改造后：_local_resolve_finding 体内已无 db.resolve_task_quality_finding 调用
$ grep -n "resolve_task_quality_finding" cli/main.py
1260:  "resolve_task_quality_finding": ("task.resolve_quality_finding", "GOVERNANCE_WRITE", ...)
4624:  result = route_task_write("task.resolve_quality_finding", {
# 行 4620-4622 的本地调用已被 raise DaemonUnavailableError 取代
```

`resolve-finding` action 中仅剩「RPC 方法名注册」与「route_task_write 路由」两处引用，
命令 handler 本身不再执行 direct DB / Unix / 本地分析器业务逻辑。验收项
「remove direct DB/Unix/local analyzer」「HTTP only」「output compatibility」均满足。

## 2. Rust native authority（step0: `port_or_verify_rust` — 本次为 verify）

业务 authority 已由 Rust daemon 实现（**verify，非 port**）：

- `rust_ext/src/daemon/task_collab.rs::handle_task_resolve_quality_finding`
  （`task_collab.rs:15553`）原生执行
  `UPDATE task_quality_findings SET status = ?1 WHERE id = ?2`，是 `task.resolve_quality_finding`
  的唯一写点。
- `rust_ext/src/daemon/dispatch.rs` 路由 `task.resolve_quality_finding`
  → `handle_task_resolve_quality_finding`（`dispatch.rs:2710`），并在方法允许列表注册
  （`dispatch.rs:2077`）。

> 注：任务描述中列出的目标文件 `cli_local_resolve_finding_handlers.rs` 在仓库中**并不存在**；
> 实际 handler 落在 `task_collab.rs`（命名差异，功能等价）。`cli_local_resolve_finding_handlers.rs`
> 缺失不影响本卡验收——`native authority` 检查项由 `task_collab.rs::handle_task_resolve_quality_finding`
> 满足。若后续要求严格按命名落文件，可在独立重构卡中拆出，不在本卡 scope。

MCP 依赖：**无**（任务 `MCP 依赖 = 无`），无需核验其他 MCP 卡 applied。

## 3. Negative-matrix scenarios (task.resolve_quality_finding RPC focus)

本卡片 RPC 焦点为 `task.resolve_quality_finding`（线上 `cw findings resolve-finding` 调用的就是
该 RPC）。以下 5 个负向场景已编码进 `tests/test_cli_087_http_rpc.py`，目标为**真实 daemon
transport**：

| # | 场景 | 输入 | 期望结果 | 实测依赖（live daemon） |
|---|------|------|----------|------------------------|
| 1 | test_success | `task.resolve_quality_finding {finding_id, resolution, identity}` | 无 `error`，返回 `{finding_id, status, updated}` | Reviewer 播种 open finding + 合法 legacy identity 后，活 daemon 返回结构化成功 |
| 2 | test_invalid | `task.resolve_quality_finding {}`（缺 finding_id） | 含 `error` | `DaemonRemoteError(code=invalid_params, "缺少 finding_id")` |
| 3 | test_authority | `task.resolve_quality_finding {finding_id, resolution}`（无 identity） | 含 `error` 且错误含 `IDENTITY` | `DaemonRemoteError(code=E_IDENTITY_REQUIRED, "…必须携带 identity")`；daemon 在写前拒绝（安全负向） |
| 4 | test_unavailable | 连死 URL `http://127.0.0.1:9` | 抛错或返回 error dict，进程不崩 | `DaemonUnavailableError`（fail-closed），未崩溃 |
| 5 | test_restart | 先复跑 #4（死链），再新建活 client 复跑 #1 | #4 同上；活 client #1 逻辑通过（恢复） | 恢复成功 |

> 说明：底层 `HttpDaemonRpcClient.call` 对业务错误信封会**抛** `DaemonRemoteError`，
> 而非返回带 `"error"` 的 dict。测试用 `_safe_call` 归一化（成功→result，错误→
> `{"error": "<code>: <message>"}`），从而同时满足「断言 `error` 在 result 中」且不依赖
> 具体抛异常/返回实现。test_success 注入 `LEGACY_IDENTITY`（四字段 legacy_identity_v1），
> test_authority 故意不带 identity 以触发 IDENTITY 门禁。

## 4. Transport target

测试直接打**线上 daemon**（`http://127.0.0.1:12376`），覆盖真实 HTTP/JSON-RPC transport，
不 mock、不重建 daemon。其中 test_success / test_invalid / test_authority 为对活 daemon 的真实
往返；test_unavailable / test_restart 仅探测死链与恢复，无副作用。

## 5. Verification

```
python tests/test_cli_087_http_rpc.py   # __main__：5 项打印 PASS（需活 daemon + 播种 finding）
python -m pytest tests/test_cli_087_http_rpc.py -v   # 可选
```

## 6. Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现 cw local-resolve-finding 的 HTTP success 与参数/authority/daemon-unavailable/restart 负向矩阵；核验 _local_resolve_finding 已无 local DB、Unix transport 或本地业务路径，且 task.resolve_quality_finding 的 Rust authority（task_collab.rs::handle_task_resolve_quality_finding）已就位。
  reason: CLI-087 仅迁移一个 CLI command handler，使 Python 成为 HTTP thin client、Rust daemon 成为唯一业务 authority。
  independence_requirement: required
```
