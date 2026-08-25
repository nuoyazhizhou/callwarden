# CLI-079 Executor Evidence Manifest

## 执行身份与任务边界

| 字段 | 值 |
|---|---|
| 治理角色 | `executor` |
| RuntimeRole | `implementer` |
| 执行身份 | `implementer-workbuddy-v1` |
| 会话 | `cw-exec-workbuddy-20260824` |
| 任务 | `T-1787322799418-ce4698f0` |
| 当前步骤 | `S-1787322799419-ce53fa54` — `evidence_and_dependency_verify` |
| 任务范围 | CLI-079 的 `cw local-close → task.close` Rust daemon HTTP thin-client 迁移 |
| 本轮允许写入 | `deliverables/software-company/CLI079_executor_evidence.md` |

本证据只记录当前任务已领取步骤的独立核验结果。任务投影显示实现与 fixture 步骤已为 `done`；本轮没有修改生产代码、测试、任务治理路径或其他任务文件。

## 结论

> **FAIL：当前工作树仍保留禁止的本地业务回退，不能作为“Python HTTP thin client、Rust daemon 为唯一 authority”的可审交付转交 reviewer。**

`cli/main.py` 的 close 分支仍定义 `_local_close()` 并直接调用 `db.task_close(opts.task_id, **close_kwargs)`，随后把该函数作为 `route_task_write("task.close", ..., _local_close)` 的 fallback 传入。该实现与 CLI-079 冻结合同中“必须移除 `db.task_close`、禁止 hidden local fallback”的要求冲突。

## Source Scan

| 目标 | 观察 | 判定 |
|---|---|---|
| `cli/main.py::_local_close` | 第 4805–4806 行仍定义 `_local_close()`，其函数体为 `return db.task_close(opts.task_id, **close_kwargs)`。 | **失败**：存在 direct DB 业务路径。 |
| `route_task_write("task.close", ...)` | 第 4810–4814 行仍将 `_local_close` 作为 fallback 参数传入。 | **失败**：存在可执行本地 fallback。 |
| `tests/test_cli_079_http_rpc.py` | 第 7、61–81 行仍把“local 模式 legacy fallback：fallback_func 调 db.task_close”作为测试契约。 | **失败**：fixture 固化了被任务禁止的行为，不能证明 fail-closed。 |
| `rust_ext/src/daemon/dispatch.rs` | Source scan 发现 `task.close` dispatch route。 | 仅说明 RPC route 存在；不能抵消 Python fallback。 |
| `rust_ext/src/daemon/http_server.rs` | 任务白名单包含 capability registry；本轮没有发现足以推翻 Python 回退的证据。 | 不影响上述失败结论。 |

## MCP 依赖核验

任务冻结合同明确声明 **MCP 依赖：无**。因此本步骤不需要等待或绕过任何 MCP dependency 卡；失败原因仅是 CLI-079 自身源码与测试契约不符合其唯一范围。

## 功能测试

在 Windows PowerShell、Python `C:\Python314\python.exe`（`3.14.3`）环境下执行：

```text
tokenslim run pytest tests/test_cli_079_http_rpc.py -q
...                                                                      [100%]
3 passed
```

该结果不改变失败判定：现有测试的第三项明确要求 legacy fallback 调用 `db.task_close`，因此它验证的是与任务合同相反的旧行为，而非“daemon unavailable 时 fail-closed 且不触发本地 DB”的负向矩阵。

## Runtime Fingerprint

| 项目 | 实测值 |
|---|---|
| Python | `C:\Python314\python.exe`，`3.14.3` |
| Daemon endpoint | `http://127.0.0.1:14350` |
| Daemon PID | `21560` |
| Daemon health | `worker_status=healthy` |
| Daemon Git commit | `8abc4a8c37a12d8744c207dc8ad58b4b19ea383d` |
| Daemon schema version | `58` |
| Capability registry revision | `http-mvp-cap-registry-v1` |

## 范围与后续处置

本步骤的 target 是证据目录，且先前的实现步骤已标为 `done`。为避免越过当前步骤边界，本执行者没有改写 `cli/main.py`、Rust dispatch/registry 或 fixture。应由任务状态机将当前证据步骤以失败结果回写，并由同一主任务生成或领取带 provenance 的 `fix_defect` 步骤，随后由 executor 在其冻结白名单内移除 `_local_close` / `db.task_close` fallback，并以真实的 HTTP success、authority error、daemon-unavailable、restart-consistency 测试替换 legacy fallback 断言。

```text
Handoff:
  from_role: executor
  outcome: executor_blocked_to_user
  next_role: executor
  next_action: 在同一主任务创建或领取 provenance-bound fix_defect 步骤；仅在该步骤的冻结白名单中移除 local-close 的 DB fallback 并修正定向测试。
  reason: 当前 evidence step 证实 CLI-079 仍含 direct db.task_close 与 legacy local fallback，无法合法转入 review。
  independence_requirement: not_applicable
```
