# T-1787888909289-881595e0：角色协议更正证据 v2（追加，不覆盖旧证据）

## 更正关系

本证据**追加并更正** `T-1787850432491-f42a2b8c-role-protocol-v1-correction-evidence.md` 的三处失实声明；
旧证据文件保持原样（append-only provenance），不删除、不改写。更正关系同时记入 `cw_task_commit_ledger.json`
追加条目 `t1787888909289_provenance_correction_20260828`。

背景：审计确认 `5452bdc`、`e70b0b7`、`7c4edda` 均绑定到 `T-1787850432491-f42a2b8c`（scope 为拆分
`task_collab.rs`），但其中包含的角色治理文档改动（AGENTS、模板、Skill、role-protocol、设计文档、校验器）
越出该任务白名单。角色治理修订的合法 scope 是本任务 `T-1787888909289-881595e0`；旧 ledger 条目不改动，
仅由本任务追加更正关系。

## 对旧证据失实声明的更正

| 旧证据声明 | 审计时实际情况 | 本轮处置 |
|---|---|---|
| 「`workflow_status` 17 项唯一枚举集中到共享协议，AGENTS/task-loop 不再维护独立枚举列表」 | AGENTS.md 与 cw-task-loop SKILL.md 仍各自保留不完整状态表（漂移） | 两处状态表删除，改为引用 `role-protocol.md §2` 单源；协议枚举补全为 18 项（含 `reverted`、`governance_blocked`）并给出「已实现/协议保留」分层 |
| 「统一 finding 字段」 | Executor/Reviewer 模板仍各自复制不同 finding schema | Reviewer v4 等模板改为引用 `role-protocol.md §4` 统一 finding schema（含 `owner_route` 归属），不再内联字段列表 |
| 「校验器增加唯一枚举……检查」 | 校验器只是 substring 存在性检查，对本轮全部问题误报 PASS | 结构化重写 `scripts/validate_template_compliance.py`（详见下节），配 14 用例负向 self-test |

## 本轮修订内容

1. **任务 provenance 纠正**：创建独立任务 `T-1787888909289-881595e0` 承载角色治理修订；ledger 追加
   更正关系（不删改旧条目）。
2. **归档恢复**：`archive/role-loop/templates/legacy/Callwarden 无人值守循环启动模板：Executor _ Planner v3.md`
   恢复为原始 blob `8ba6501fb8c1ba30dad66729997584133df62fd0`（迁移提交 `e70b0b7` 曾改写为 `fd03368…`，
   删除旧 Handoff 段并调整格式；本次以 `git cat-file blob` 二进制写回，避免行尾转换——期间发现 CRLF
   转换会再次破坏字节一致性，已用 sha1 blob hash 复核）。归档 README 增补字节级原件纪律与 blob id 表。
3. **设计 v2 amendment**：新增 `docs/design/cw-role-handoff-task-loop-v2-amendment.md`，冻结 v1 不再直接
   修改，以精确到节的 supersede 映射声明四角色模型、capability cutover 与双轨整改状态机。
4. **capability cutover**：以 daemon capability `planner_governance_v1` 作为 Planner 原生派工的唯一门禁；
   未声明前 `task.next_action` 不返回 `READY/PLAN`、不 emit `planning_*`/`replanning_*`/`waiting_*`，
   `task.create`/`--role-contracts` 拒绝 `planner` 角色，`task.handoff` 只接受已实现六种 outcome。
   Planner v1 模板标记为 design-only。
5. **双轨整改状态机**：实现缺陷 → daemon 同任务追加 provenance-bound `fix_defect` → Executor；
   scope/Contract/架构缺陷 → post-cutover 交 Planner（`replanning_pending`），pre-cutover 升级用户，
   不得塞给 Executor 当 `fix_defect`。AGENTS.md、cw-task-loop SKILL.md、user-guide、四模板全部对齐。
6. **单源去重**：状态枚举、Handoff 字段块（含固定字段顺序）、finding schema 唯一单源收敛到
   `role-protocol.md` §2/§4/§5；AGENTS.md、SKILL.md、user-guide、四模板只引用不复制。
7. **命令纪律修正**：role-protocol.md §7 统一为 PowerShell 语法（`$COMMIT = git rev-parse HEAD`、
   `"$env:CW_AGENT_SESSION_ID"`）与 `C:\Python314\python.exe` 限定调用，禁用 Bash `$(...)` 与未限定 `python`。
8. **AGENTS.md Python 直连修正**：明确禁止在 enterprise/auto（daemon 模式）下用 Python 直连
   `CodeGraphDB.task_create(parent_id=...)` 挂子任务；子任务挂载首选 `cw task split --plan`（daemon 路径）。

## 校验器结构化升级

`scripts/validate_template_compliance.py` 从 substring 检查重写为结构化校验：

- 协议内部一致性：枚举唯一（E_PROTO_ENUM_DUP）、已实现/协议保留分层完备且不相交
  （E_PROTO_LAYER_MISMATCH）、Handoff 字段顺序 task_id 首位/identity 末位且无重复
  （E_PROTO_HANDOFF_ORDER）、outcome 分层完备（E_PROTO_OUTCOME_LAYER_MISMATCH）；
- 单源纪律：派生文档复制状态枚举/outcome 枚举（3 行窗口 ≥6 值）、内联 Handoff 字段块、
  复制 finding schema 均报错；
- 双轨路由 guard：「交/给/→ Planner」路由必须带 pre/post-cutover 限定（E_ROUTE_PLANNER_NO_CUTOVER）；
  引用 `executor_replan_requested` 必须说明 pre-cutover 无法持久化（E_REPLAN_NO_CUTOVER）；
  引用 `READY/PLAN` 必须标注协议保留（E_RESERVED_DISPATCH_NO_GUARD）；
- capability guard：Planner 模板必须含 design-only 声明（E_PLANNER_DESIGN_ONLY_MISSING）；
- 归档字节级校验：README 声明的 blob id 与归档文件 sha1 blob hash 一致（E_ARCHIVE_BLOB_MISMATCH）；
- `--self-test` 负向测试：14 个故意破坏用例逐项断言错误码命中。

## 验证

```text
& C:\Python314\python.exe scripts/validate_template_compliance.py
  结构化合规检查通过：协议单源、4 个角色模板、Skill/user-guide、设计 supersede 与归档 blob 均一致
& C:\Python314\python.exe scripts/validate_template_compliance.py --self-test
  self-test: 14 通过, 0 失败（共 14 用例）
归档 blob 复核: 8ba6501fb8c1ba30dad66729997584133df62fd0（19831 bytes, LF）
```

## 未实施能力（后续 daemon/CLI/MCP 任务）

本轮仅修订文档、协议与校验器；daemon 侧 `planner_governance_v1` 的实现清单不变：

- `planner` 原生 Role Contract 注册与 `task.create`/`--role-contracts` 四角色支持（当前
  `cli/main.py` 仅允许 executor/reviewer/adjudicator，`rust_ext/src/daemon/task_loop/next_action.rs`
  将 planner 映射为 executor）；
- `READY/PLAN` 派工与 `planning_*`/`replanning_*`/`waiting_*` 状态 emit；
- `planner_ready_for_execution`/`planner_replan_required`/`executor_replan_requested` outcome 持久化
  （当前 `report_handoff.rs` 只接受旧六种）；
- `decision_request`/`decision.respond` 落库与响应、自动 `fix_defect`、`adjacent_defect → related_to`。

上述能力上线前，所有客户端文档以 capability 未声明的 pre-cutover 行为为准。
