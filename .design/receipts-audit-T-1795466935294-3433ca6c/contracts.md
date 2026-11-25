# contracts — receipts 审计契约与门禁核验

- **feature**：receipts 审计：build_graph 成功/失败/转后台均落可验证审计回执
- **R 阶段**：R3 契约与基线
- **生成时间**：2026-11-24 15:32:19
- **依据 HEAD**：`1fbcd9bad21d58b7ed76e7e1710883213d5495cc`
- **任务**：`T-1795466935294-3433ca6c`

## 1. 函数级 contract 卡片

### CARD-1 · 回执写入（对应 step 0 / AC-01/AC-02/AC-03）
- **目标符号**：`fs_handlers::build_flight::BuildFlights::finish`（extend）+ `run_build_graph` 出口调用点（reuse/改造）
- **入参**：`root_key: &str`（canonical root posix 化）、`stats: Value`（含 ok/scanned/inserted/unchanged/deleted/...，失败时含 error/message，完成时注入 build_seq/completed_at）
- **出参**：`()`（副作用：lastBuild 更新 + 新增 1 条审计回执 + 1 条链记录）
- **不变量**：每条回执字段集完整；`build_seq` 与 begin 序号一致；成功 ok=true、失败 ok=false 且 error 非空；回执与链一一对应
- **异常路径**：审计写失败 → E_WRITE_AUDIT_UNAVAILABLE fail-closed，SAVEPOINT 回滚，不得吞异常。**fail-closed 边界留痕（🟡3，2026-11-24 planner 登记、reviewer 复评确认）**：
  - (a) 超时转后台路径 fail-closed **不可达**：客户端已收 accepted、tx 接收端已 drop，后台审计失败退化为"回执缺失"，lastBuild 照常发布，只能靠 build_seq 对账发现——须写入工具文档/AC-03 证据；
  - (b) 同步路径 build 成功但审计失败时返回 Err：客户端可能误判"数据未写入"（实际图已更新）——**这是本卡片约定行为，保留**，reviewer 支持保留，复评按此口径。
- **校验归属**：字段完整性在写入前校验（`detail_json` 序列化）；枚举映射（commit/allow|deny）在写入点
- **测试要点**：AC-01/02/03 的正向 + E2/E4/E5/E7 负向；build_seq 对账

### CARD-2 · 链完整（对应 step 1 / AC-04）
- **目标符号**：`sign_audit_chain`（reuse，`cli/task.rs:3768`）+ `append_write_audit_inner`（reuse，`fs_handlers.rs:2569`）
- **入参**：conn、table_name=`remote_write_events`、record_id=event_id、payload=回执字段 canonical JSON
- **出参**：`Result<(), String>`；event 回填 `audit_record_id`
- **不变量**：payload_hash 链连续；既有签名序列不被破坏
- **异常路径**：链写失败 → 整体回滚（SAVEPOINT）
- **校验归属**：verify 由 `handle_audit_verify_chain` 承担
- **测试要点**：AC-04；`broken_count=0`；event↔chain 一一对应（参照 `fs_handlers.rs:4920-4928`）

### CARD-3 · 读面幂等（对应 step 1 / AC-05）
- **目标符号**：既有审计查询读面（reuse，不新增 MCP 工具）+ `handle_audit_verify_chain`（reuse）
- **入参**：workspace_id + start_time/end_time（+ 必要过滤）
- **出参**：回执列表（JSON）
- **不变量**：同一查询重复读结果一致（纯读，无副作用）
- **异常路径**：读面不支持时间范围 → capability gap（U-03），上报 Planner，不得新增工具
- **校验归属**：读面过滤与幂等由查询层保证
- **测试要点**：AC-05 两次查询 diff 为空

### CARD-4 · 回归（对应 step 2 / AC-06）
- **目标符号**：既有审计测试集合（reuse）
- **入参/出参**：测试命令 → 全绿 + 退出码 0
- **测试要点**：AC-06 的 Rust + Python 两套命令（见 ac-map）

## 2. 门禁四件套结论

| 件 | 要求 | 结论 |
|---|---|---|
| ①方案对比 ≥2（含优缺点与失效模式） | 至少两个可行方案，各有优缺点与失效模式 | **齐**：方案 A/B/C 三选一，A 推荐，B/C 有被否决理由与失效模式（`requirements-brief.md` §4） |
| ②被否决方案的理由 | 每个被否方案写明决定性理由 | **齐**：B（无签名链硬缺口 + 独立库）、C（schema/工具禁令）；均指向硬约束而非偏好 |
| ③异常场景矩阵 8 类 | 8 类异常场景 + 期望行为 + 证据 | **齐**：E1-E8（`requirements-brief.md` §5） |
| ④未决项三态 | 每项带 confirmed/assumption/open + 阻塞 | **齐**：U-01~U-05（`requirements-brief.md` §6） |

**四件套：齐。**

## 3. 契约与 step binding 一致性核验

| 核验项 | 结果 |
|---|---|
| task_contract id/revision/hash | `TC-T-1795466935294-3433ca6c` / r1 / `sha256:ed3769f1...` — 与 daemon task.status 一致 |
| role_contract id/revision/hash | `rcl-T-...-executor` / r1 / `sha256:2ea2a4c4...`；skill_id=none；prompt=cw.aprime.executor.startup.v1 — 一致 |
| step binding | 3 步：S-...34636a9c（finish 写回执）、S-...3463c53c（复用 audit_log handler + verify_chain + 幂等）、S-...34640f10（测试） — 与符号锚定一致 |
| workspace binding | workspace_id=1 / instance=`4baea3ff12c2ea5c` / binding_id=`tb-T-...-4baea3ff12c2ea5c` — G1 权威配对 |
| identity policy | `legacy_identity_v1`（declared）；required_role=executor；lease=implementer；fencing=true — 齐 |
| governance decision | `READY / CLAIM`；step S-...34636a9c 可领取；eligibility verdict=not_required_for_claim — 齐 |
| G6 建单格式 | 现有 3 步 `check_items` 为转义 JSON 字符串（G6.3 反模式）— 瑕疵但 contract 已 frozen，登记 U-04，不改历史 |
| 依赖声明 | task_create 无结构化依赖字段；执行侧按 step 顺序认领（G6.4） |

## 4. Contract 核验结论

**沿用现有 daemon binding，不新建子任务。** 理由：

1. governance decision 已是 `READY/CLAIM`，唯一 verified Role Contract binding（daemon 明确写"当前步骤可领取"），task_contract/role_contract/step/workspace/identity 五要素齐全且 hash 自洽（§3）。
2. 冻结的 3 个 step 与本轮符号锚定结果**逐条对齐**（`BuildFlights::finish` → `handle_build_graph_async` → 测试），scope 落在 `build_flight.rs` + `fs_handlers.rs` 单一 ownership，未超出 allowed_paths。
3. 所有 AC（AC-01~06）均可在现有 3 步 scope 内达成；U-01/U-02/U-03 是**实现方案选择/读面确认**，不是需要独立 ownership 或独立验收目标的新工作，故不构成拆子任务条件（cw-planner-architect 复杂度判据）。
4. 若强行建整改/子任务，反而会与已 frozen 的 contract（hash 固定）产生分叉、破坏 `task-map.md` 追溯闭环，且 daemon 当前不接受 planner 原生派工（design-only）。

**但设一道开工前置（非阻塞建单、阻塞 step 1 落库）**：Executor 必须在 implementation plan 中锁定方案 A 的借壳映射（U-01）与超时回执落点（U-02）；若判定 U-01 的枚举借壳不可接受、需要 schema 变更，则属于 out-of-scope，须走 `executor_replan_requested` 路径回 Planner，**不得**在冻结 scope 内擅自改 schema 或新增 MCP 工具。

## 5. 基线提交

- 分析基线 HEAD：`1fbcd9bad21d58b7ed76e7e1710883213d5495cc`（设计工件以此为准）
- 工作树在途 dirty/未跟踪文件（含 `build_flight.rs`、`test_build_graph_async_client.py` 等本 feature 相关源码）**不属于本设计基线**，由 executor 在独立 worktree / whitelist capture 中处理（规则 40/48）
- Planner 本轮只产出 `.design/receipts-audit-T-1795466935294-3433ca6c/` 文档，不提交生产代码
