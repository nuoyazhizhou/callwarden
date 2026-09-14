# Role Prompt Compiler v1 bootstrap recovery amendment

> 状态：`PLANNER_APPEND_ONLY_BOOTSTRAP_REVISION`
>
> 日期：2026-09-01
>
> 规划责任：Planner
>
> 基础 Gate manifest：`deliverables/software-company/role-prompt-v1-gate-task-manifest.json`
> SHA-256 `0DF148FD18C365EE2F84A50F8427973960D969A70DBD5CAC17BFD190CBA2F947`
>
> 冻结规范：`docs/design/cw-role-prompt-compiler-v1-frozen-spec.md`
> SHA-256 `95298729F3357CDBE76D8F8E91F12067B54D2661D6E80ABFF561A2E2A8C86CB7`

## 1. 修订目的

本修订只处理 Gate manifest 首个 review vehicle 暴露的 `task.create` workspace
bootstrap 缺口，不改写基础 manifest、冻结规范、历史任务或历史 Contract。

失败尝试必须保留：

- task：`T-1788253722521-3b2f8420`
- step：`S-1788253727027-47bec72c`
- Task Contract：`TC-T-1788253722521-3b2f8420` revision 1
- Task Contract hash：`sha256:dff6148524fbb96cc4f34b9267f4b503bb7c162bbfe9c7258c11496e058964e5`
- Executor Role Contract hash：`sha256:ee8ce769d8166f2d3b298677708583d27263e7537aea37706f2c63402ca4779f`
- manifest hash：`0DF148FD18C365EE2F84A50F8427973960D969A70DBD5CAC17BFD190CBA2F947`

该任务不得被重写为“创建正确”，不得复用其 idempotency key 创建另一个同名
vehicle，也不得领取其 `freeze_gate_manifest_review_input` 步骤来实施生产代码。

## 2. 独立复现与根因

当前 runtime 为 `C:\Users\wanpi\.callwarden\runtime\current\cw.exe`
`0.3.23`。Callwarden 的 daemon workspace authority 为：

```text
workspace_id=1088
workspace_instance_id=4baea3ff12c2ea5c
client_view_root=C:\git_work\callwarden
```

复现事实：

1. Rust runtime CLI 的 enterprise `task.create` 请求只发送 title、description、
   steps 和 Role Contracts，没有发送 `workspace_id` 或 `workspace_instance_id`。
2. Python `cw task create` 的命令层同样不显式发送 `workspace_instance_id`；旧路由最多补
   numeric workspace ID。
3. daemon `handle_task_create` 要求 numeric `workspace_id`，但允许空
   `workspace_instance_id` 进入 `bind_task_to_workspace`。
4. `bind_task_to_workspace` 把空 instance 合成为 `ws-{workspace_id}`。该值不是
   workspace registry 返回的 immutable instance，不能作为多 workspace/multi-agent authority。
5. create 响应已有 `workspace_binding_id`、`workspace_capture_id`、`assignment_id`，
   但缺 `workspace_instance_id`；`task.status`/`task show` 也不能完整回查这组 ID。
6. `task.list` 在显式提供 instance 后仍可能因缺 numeric workspace ID 返回
   `E_TASK_WORKSPACE_UNBOUND`，证明客户端没有完成 instance-to-numeric authority resolution。

结论：这是 client/daemon bootstrap contract 断裂，不是 Reviewer 或 Executor 身份问题。

## 3. 不变量

1. enterprise/auto 请求的 workspace authority 必须来自 daemon `workspace.status` 或 task
   immutable binding；不得来自 local active workspace、cwd hash、客户端 numeric 猜测或 `ws-{id}`。
2. `task.create` 必须同时携带 daemon 返回的 numeric `workspace_id` 和稳定
   `workspace_instance_id`；两者不一致时 fail closed。
3. 缺少/空 instance 必须在任何 task、step、capture、binding、assignment 写入前拒绝。
4. create 成功响应和只读 status 回查必须暴露同一组 task/binding/capture/assignment ID。
5. Python CLI 与 runtime Rust CLI 必须共享同一 daemon contract；任何一侧不得保留本地业务 fallback。
6. `T-1788253722521-3b2f8420` 及其 rev1、step、事件保持 append-only。
7. 新 review vehicle 必须使用新 idempotency key，不能把旧 vehicle 的 task_id 或 step_id 当作成功载体。

## 4. 串行微任务

### BR-01：daemon strict workspace binding 与只读回查

目标：把 `task.create` 的 workspace authority 变成严格双字段契约，并补齐只读 provenance。

Allowed paths：

- `rust_ext/src/daemon/task_collab.rs`，仅允许最小参数门禁/薄接线；不得继续堆积新 domain 逻辑
- `rust_ext/src/daemon/task_collab_query.rs`
- `rust_ext/src/daemon/task_loop/create.rs`
- `rust_ext/src/daemon/task_loop/create_test.rs`
- 新增的 task-create binding 定向测试模块
- `docs/evidence/role-prompt-v1-bootstrap-br01-daemon.json`

Ownership 门禁：`rust_ext/src/daemon/task_collab.rs` 同时处于大文件拆分治理范围。BR-01
不得与 `T-1787850432491-f42a2b8c` 或其后继拆分任务并发编辑该文件；领取前必须由 daemon
确认没有冲突 assignment/lease，并将该文件的修改限制为参数门禁与薄接线。若共享 domain 已迁出，按
当前 source-of-truth 路径修订 Contract，而不是把业务逻辑搬回巨型文件。

Excluded paths：

- `cli/**`
- `server/**`
- `db/**`
- 基础 Gate manifest、冻结规范和 R1～R4 历史文件
- 旧 review vehicle 的 task/Contract/event 历史

交付：

1. missing/blank `workspace_instance_id` 返回稳定错误
   `E_TASK_WORKSPACE_INSTANCE_REQUIRED`，且零领域写入。
2. numeric ID 与 instance registry 映射不一致返回
   `E_WORKSPACE_AUTHORITY_MISMATCH`。
3. 删除生产 `task.create`/`task.create_from_plan`/`task.create_subtask` 路径上的
   `ws-{id}` 合成；测试 fixture 可使用显式测试 instance，但不得证明生产 fallback 合法。
4. create 响应至少返回 `task_id`、`workspace_id`、`workspace_instance_id`、
   `workspace_binding_id`、`workspace_capture_id`、`assignment_id`、首个 `step_id`。
5. `task.status` 返回同一组只读 `workspace_binding`、capture 和 current assignment 标识。

### BR-02：Python thin-client parity

前置：BR-01 `closed` 且 runtime 部署证据成功。

Allowed paths：

- `cli/main.py`
- `server/daemon_client.py`
- `tests/test_task_create_workspace_binding.py`
- `docs/cli_reference.md`
- `i18n/en_US.json`
- `i18n/zh_CN.json`
- `docs/evidence/role-prompt-v1-bootstrap-br02-python-client.json`

Excluded paths：daemon Rust domain、SQLite mixin、Gate/frozen design 历史。

交付：

1. CLI 只以显式 instance 调用 `workspace.status`，消费 daemon 返回的 numeric ID。
2. `task.create` 同时透传两字段；不调用 local active workspace，不推导 `ws-{id}`。
3. CLI 输出 create 响应中的 binding/capture/assignment/step ID，并立即执行只读回查比较。
4. daemon 不可达、status 不完整或回查不一致时 fail closed，不回退 Python DB。

### BR-03：runtime Rust `cw.exe` parity

前置：BR-02 `closed`。BR-02 与 BR-03 都会修改 `docs/cli_reference.md`，必须按
BR-02 → BR-03 串行合并，不得在不同 checkout 中并发制造文档冲突。

Allowed paths：

- `rust_ext/src/bin/cw_cli.rs`，仅允许薄接线和参数声明
- `rust_ext/src/cli/runtime.rs`
- `rust_ext/src/cli/task.rs`
- 新增 `rust_ext/src/cli/task_enterprise.rs` 及其定向测试
- `docs/cli_reference.md`
- `docs/evidence/role-prompt-v1-bootstrap-br03-rust-cli.json`

Excluded paths：daemon task domain、Python client、SQLite 直连、Gate/frozen design 历史。

交付：

1. enterprise `task create` 使用当前显式 instance 调用 `workspace.status`，读取 numeric ID。
2. 请求同时传两字段；local 模式的 numeric active compatibility 不得进入 enterprise 请求。
3. create 输出及 `task show` 显示 daemon provenance ID，并验证 readback 一致。
4. 当前兼容参数 `--workspace-id` 在 enterprise 语境必须明确解释为 instance；后续可增加
   `--workspace-instance-id` 正式别名，但不能改变 local numeric 语义。

### BR-04：创建正确绑定的 review vehicle v2

前置：BR-01、BR-02、BR-03 均 `COMPLETE`，且当前 runtime deployment receipt 为 passed。

该步骤是受治理的 task creation，不修改生产代码。使用 corrected runtime CLI 创建新 root task：

- title：`Role Prompt v1 Gate manifest independent review vehicle v2`
- idempotency key：`role-prompt-v1-gate-manifest-review-vehicle-v2`
- 输入绑定：基础 manifest hash、本修订 hash、machine work-order hash、冻结规范 hash
- workspace：`workspace_id` 与 `workspace_instance_id` 均来自同一次 daemon status
- 创建后立即只读回查 task/Contract/Role Contract/step/binding/capture/assignment IDs

旧任务 `T-1788253722521-3b2f8420` 保持原状态和历史；新任务不得复用其 task、step、
Contract、binding、capture、assignment 或 idempotency key。

## 5. 最低验收矩阵

正向：

- Callwarden 与 TokenSlim 两个 workspace 同时 active 时，各自显式 instance 创建任务，binding 不串线。
- create 响应与 status 回查的全部 ID 一致。
- 同 request ID/同参数重放返回同一结果，不新增 task/capture/assignment。

负向：

- 缺 instance、空 instance、`ws-{id}`、未知 instance、numeric/instance mismatch 均拒绝。
- 在 Callwarden cwd 给 TokenSlim instance，或反之，必须由 registry/root authority 拒绝。
- daemon 不可达时两个 CLI 均不得 local fallback 创建。
- 任何失败不得留下 task-only、capture-only、binding-only 或 assignment-only 半成品。

回归：

- `task.split`、`task.create_from_plan`、`task.create_subtask` 采用同一 strict binding helper。
- local 模式保留显式 numeric workspace compatibility，但不影响 enterprise。
- 旧任务和旧 manifest hash 均可查询且字节/事件不变。

## 6. 发布顺序与当前授权状态

```text
本修订 + machine work-order 冻结
  -> 核验 T-1787850432491-f42a2b8c 已完成，或 daemon 证明 BR-01 不再触及其 ownership
  -> BR-01 daemon
  -> controlled runtime deploy
  -> BR-02 Python CLI
  -> BR-03 Rust runtime CLI
  -> controlled runtime deploy + cross-client E2E
  -> BR-04 new review vehicle v2
  -> task-bound Reviewer PASS
  -> 原 Gate manifest 的 GATE-0
```

当前 `task.create` 与 `task.split` 均不能保证真实 instance binding，因此本修订不伪造
BR-01～BR-03 已入库。导入器只有在获得受支持的 exact-binding bootstrap 路径后才能把 machine
work-order 写入 daemon；禁止 generic RPC、SQLite 或 local-mode task write 旁路。
