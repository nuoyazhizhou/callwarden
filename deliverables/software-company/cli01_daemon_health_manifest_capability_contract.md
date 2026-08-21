# CLI-01：`cw daemon health / manifest / capability` 诊断链路 Rust daemon 化

**父任务：** `T-1787293451688-c14b1e44`（A′ 逐链路 Rust daemon 迁移恢复）  
**port_type：** `control_plane`  
**port_key：** `cw.py|cli/daemon_commands.py|server/daemon_autostart.py::resolve_http_endpoint_and_manifest|rust_ext/src/daemon/http_server.rs|health/capability fixture family`  
**gate：** `true`  
**successor_rule：** 在独立 Reviewer PASS 且独立 Adjudicator 对本任务 `apply` 前，禁止创建 `control_plane` 后继 `CLI-02`/`CLI-03`，也禁止创建任何 MCP 首端口 Gate。

## 目标

迁移且只迁移 `cw daemon health / manifest / capability` 的诊断链路，使 Python 层成为 HTTP API client/格式化 thin shell，Rust daemon 成为 health、manifest authority 和 capability 状态的唯一业务实现。该任务不包含 `cli/main.py` 的旧 S1 全量引用清理，也不包含 MCP 工具迁移。

```text
cw daemon health / manifest / capability
  → cw.py / cli daemon command parser
  → server.daemon_autostart resolve_http_endpoint_and_manifest
  → HttpDaemonRpcClient
  → HTTP daemon health / capability response
  → Rust authority
```

## Executor 范围

| 类别 | 必须修改或核验的唯一目标 |
|---|---|
| Python 入口 | `cw.py`、`cli/daemon_commands.py` 中该命令的 parser、调用与输出格式化 |
| Python client | `server/daemon_autostart.py::resolve_http_endpoint_and_manifest` 及已存在 `server/daemon_client.py` 的 health/capability thin wrapper |
| Rust 业务 | `rust_ext/src/daemon/http_server.rs` 的 `/health`、manifest/capability response；仅当分离既有函数所必需时，新增同模块内私有 health helper |
| Rust transport | 仅该命令使用的 HTTP JSON response、capability registry 查询；不得新增本地 SQL 或 Unix/local fallback |
| 测试 | CLI process fixture + daemon HTTP fixture，覆盖 success、missing manifest、stale PID、wrong authority、daemon unavailable 与 fresh daemon；至少一项真实 `get_stats` MCP round-trip |
| 真相源 | 更新该 CLI command 的 route/matrix generator 输入与生成结果；记录 daemon binary/source fingerprint 和 capability response evidence |

## 不变量与负向验收

1. manifest 缺失、stale PID、wrong authority、daemon unavailable 均返回稳定且可区分的错误，不得静默启动未验证的 daemon 或降级为本地 SQLite。
2. fresh daemon health/capability response 必须带可复核 runtime endpoint、authority、schema/capability 信息；至少一个真实 MCP `get_stats` 调用成功。
3. Python 不得包含 health/capability 的业务判定、SQLite 查询或由异常隐藏地回落到 `get_db()`。
4. 不改 `cli/main.py` 的 296 处旧 S1 引用清理；不改 `db/schema.py`、`task_collab.rs` 治理 mutation、lease/assignment/verdict/gate 语义。
5. 失败时保留当前 runtime；只有通过 Rust/HTTP/CLI fixture 后才可更新命令链路的迁移矩阵状态。

## 必须证据

Executor 在 `task.report` 中逐步骤引用证据 manifest，至少包括：Python 3.14 路径和版本、Rust build/test 输出、CLI process output、HTTP round-trip output、fail-closed denial matrix、runtime/current 与运行 PID 的 hash/manifest 对照，以及 capability registry row。

## Handoff

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立复现实 daemon health/manifest/capability 的成功与四类 fail-closed 场景；核验 Python 仅为 HTTP thin client 且 CLI-01 未越界触碰旧 S1 的 cli/main.py 全量清理。
  reason: CLI-01 是 A′ control_plane 的首卡 Gate；仅在 Rust daemon authority 与诊断可观测性均可复现时才允许后继 control-plane/MCP Gate 建卡。
  independence_requirement: required
```
