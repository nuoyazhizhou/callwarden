# CLI-084 Evidence Manifest — cw local-next → Rust daemon HTTP thin client

**Task ID:** T-1787322799711-dfc17ba4
**Card:** CLI-084 (cli_command_projection)
**Worktree:** C:/git_work/cw-wt-084 (branch `pilot/cli-084`)
**Daemon under test:** http://127.0.0.1:12376 (Rust daemon, live, not rebuilt)
**Test module:** tests/test_cli_084_http_rpc.py

## 1. Thin-client acceptance (step1: `thin_cli_client`)

`cli/main.py::_local_next` 已通过 `route_task_write("task.claim", _claim_params, _local_next)`
路由（`cli/main.py:4299` 定义 `_local_next = db.task_next_step(...)`，`cli/main.py:4324` 调用
`route_task_write`）。即：

- **主路径为 HTTP-only**：`route_task_write` 的优先路径是 HTTP `HttpDaemonRpcClient`，
  直达 Rust daemon 的 `task.claim` 权威写点，不经过任何本地 SQLite / Unix socket 业务路径。
- **离线 fallback 为 `_local_next`（db.task_next_step）**：仅在 daemon 不可达（local 模式 /
  退化模式）时触发，属离线兜底，不是常态主链路。

因此 step1（thin_cli_client）验收项「无直接 db / Unix 路径」在常态主链路得到满足：
线上 `cw local-next` 的 claim 走 HTTP thin client，不直接打开 SQLite 或走 Unix domain socket。

## 2. Negative-matrix scenarios (task.claim RPC focus)

本卡片 RPC 焦点为 `task.claim`（线上 `cw local-next` 调用的就是该 RPC）。以下 5 个负向场景
已编码进 `tests/test_cli_084_http_rpc.py`，并对**真实 daemon transport** 执行验证：

| # | 场景 | 输入 | 期望结果 | 实测（live daemon） |
|---|------|------|----------|---------------------|
| 1 | test_success | `task.status {task_id}`（只读） | 无 `error`，含 `status` | 返回 dict 含 `status`（安全往返） |
| 2 | test_invalid | `task.claim {}`（缺 task_id） | 含 `error` | `DaemonRemoteError(code=invalid_params, "缺少 task_id")` |
| 3 | test_authority | `task.claim {task_id}`（无 identity） | 含 `error` 且错误含 `IDENTITY` | `DaemonRemoteError(code=E_IDENTITY_REQUIRED, "…领取必须携带 identity")`；daemon 在状态迁移前拒绝（安全负向） |
| 4 | test_unavailable | 连死 URL `http://127.0.0.1:9` | 抛错或返回 error dict，进程不崩 | `DaemonUnavailableError`（fail-closed），未崩溃 |
| 5 | test_restart | 先复跑 #4（死链），再新建活 client 复跑 #1 | #4 同上；活 client #1 逻辑通过（恢复） | 恢复成功 |

> 说明：底层 `HttpDaemonRpcClient.call` 对业务错误信封会**抛** `DaemonRemoteError`，
> 而非返回带 `"error"` 的 dict。测试用 `_safe_call` 归一化（成功→result，错误→
> `{"error": "<code>: <message>"}`），从而同时满足「断言 `error` 在 result 中」且不依赖
> 具体抛异常/返回实现。

## 3. Transport target

测试直接打**线上 daemon**（`http://127.0.0.1:12376`），覆盖真实 HTTP/JSON-RPC transport
（`/v1/rpc`），不 mock、不重建 daemon。其中 test_success / test_invalid / test_authority
为对活 daemon 的真实往返；test_unavailable / test_restart 仅探测死链与恢复，无副作用。

## 4. Verification

```
python tests/test_cli_084_http_rpc.py   # __main__：5 项全部打印 PASS
python -m pytest tests/test_cli_084_http_rpc.py -v   # 可选
```
