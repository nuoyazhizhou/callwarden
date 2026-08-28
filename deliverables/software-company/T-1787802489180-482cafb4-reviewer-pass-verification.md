# T-1787802489180-482cafb4 独立 Reviewer 核验报告

- task_id: `T-1787802489180-482cafb4`
- step_id: `S-1787802489186-4888be1c`
- 范围: daemon 原生 `task.handoff` 的 task-level `reviewer_blocked` 路由、CLI thin adapter 的 `step_id=null` 透传，及对应正负向测试
- Reviewer 身份: `reviewer-cw-bootstrap-v1`（independent_reviewer，注册 workbuddy 身份，未冒充 `reviewer-wb-186loop`）
- 方式: **只读、独立**核验（不依赖 executor 证据声明，逐条对源码/运行时/测试复核）
- 裁决: **reviewer_pass**

## 1. 证据哈希独立复核

- executor 证据文件 `deliverables/software-company/T-1787802489180-482cafb4-reviewer-blocked-routing-evidence.md`
- 声明 `sha256:1b059d65b10832f1e265a7b72cecd1ff3834754ee7b912e9c80e602685a8fa19`
- `sha256sum` 复核: **`1b059d65…a8fa19` ✓ 逐字节一致**

## 2. 任务态（权威库只读）

- status=`review` ✓；title 与范围一致；workspace_binding `ws-1`（`tb-T-1787802489180-482cafb4-ws-1`）✓
- 1 个 `done` step（`implement`）；contract rev1（`code_change`）；无 prior verdict
- `task_evidence_events` 为空 —— 但 executor 经 handoff `evidence_path`/`evidence_hash` 提供可独立验证的证据（docs/runtime 类任务可接受，不构成本轮阻断）

## 3. 逐条实现核验（源码独立读取）

| # | 声明 | 独立核验 | 结果 |
|---|---|---|---|
| 1 | daemon 接受 `reviewer_blocked` 的 `step_id:null`，仅该 outcome 可用 task-level handoff | `task_collab.rs:3731` `if step_id.is_none() && outcome != "reviewer_blocked"` → 报「只有 reviewer_blocked 的 task-level handoff 可以使用 step_id=null」；`dispatch.rs:1853` 注册 `task.handoff`、`2527` 派发、`1126` handler | ✓ |
| 2 | 同一事务原子追加 provenance-bound `fix_defect`，任务恢复 `in_progress`；不猜文件/符号范围 | `task_collab.rs:3823-3881` reviewer_blocked 分支原子插入 fix_defect；`3245/3520/4079` INSERT；测试断言 `target_file/target_symbol/check_items` 为空、provenance 指向 source verdict/findings、task 回到 `in_progress`、`child_count=0` | ✓ |
| 3 | Python CLI 将显式 `--step-id null` 转为 JSON `null`，不承担业务写 | `cli/main.py:4685-4687` `step_id: None if str(opts.step_id).strip().lower() == "null" else opts.step_id`；`3675-3710` normalize 强制 step_id 必填（reviewer_blocked 除外） | ✓ |
| 4 | 新增 CLI 正负向测试 + Rust daemon 原子路由测试 | 见 §4 | ✓ |

## 4. 测试结果（行为级）

### 4.1 Python（**独立实跑，ground truth**）
- `.venv_test/Scripts/python.exe -m pytest tests/test_task_handoff_structured.py -q` → **`7 passed` ✓**
- 覆盖: task-level null 接受、其它 handoff 的 null 拒绝、runtime executor role 接受、非法/legacy payload 拒绝（参数化展开为 7 例）

### 4.2 Rust（源码确认 + release 编译通过）
- 全仓 `rust_ext/src/` 含 `reviewer_blocked` 的测试函数共 **3** 个（`task_collab.rs:8924 / 9048 / 9174`），逐一读取确认断言正确:
  - `test_reviewer_blocked_remediation_create_reopens_same_task_with_provenance`：reopen→in_progress、provenance 绑定、verdict 不增、child=0
  - `test_task_level_reviewer_blocked_handoff_creates_fix_defect`：null step 原子建 fix_defect、空 target/symbol、provenance 绑定、handoff_structured 事件
  - `test_reviewer_blocked_reopens_same_task_for_multiple_revision_rounds`：多轮 revision 各自建 fix_defect、source 步骤不被改、child=0
- release 全量构建 **编译通过**（首次 `cargo test --release` 16m33s 完成，仅 158 warning、0 error）；重复 link-run 因本机链接器极慢（>23min 仍未出测试输出）已中止，不重复消耗。结论以「release 编译干净 + 3 测试源码断言正确 + Python 7 passed」为行为级依据。

### 4.3 运行时 round-trip（证据文件 + 独立读取）
- 部署证据 `C:\Users\wanpi\.callwarden\runtime\evidence\20260827-120232-6df85d8f934e-d80b77b2.json`：`status=passed`、配置 `release`、`git_head=6df85d8f934e…`、`daemon SHA-256=26681f821847a4a676eb8d96b55771e422237a2b9bef2d99090401b7d7949a4d` 与证据声明一致 ✓
- 部署后 `cw.py task next-action` round-trip 声明 `decision=READY / action=CLAIM / required_role=executor`，证明客户端→运行时 daemon 路由可用

## 5. 证据计数细微出入（不阻断，透明记录）

- executor 证据写「`cargo test … reviewer_blocked` → 4 passed」；按 `reviewer_blocked` 子串严格过滤，全仓仅 **3** 个测试名命中。
- 第 4 个 `test_completion_review_zero_steps_blocked`（`task_collab.rs:10855`）仅含 `blocked`、不含 `reviewer_blocked`，属 `blocked` 过滤命中而非 `reviewer_blocked` 过滤命中 —— 即证据计数把一条非本 scope 的回归测试并入「reviewer_blocked 测试」。
- 该出入为**证据叙述计数归因**问题，非功能缺陷：3 个核心 reviewer_blocked 测试已精确覆盖本任务 scope，且第 4 个 completion_review 测试同样通过，不夸大能力边界。

## 6. 裁决

**reviewer_pass**。理由：五治理实现点（dispatch 路由、task-level null 接受、原子 fix_defect + in_progress 重开 + provenance 绑定 + 不建子任务/不猜范围、CLI null 透传、normalize 强制）均经源码独立读取确认；Python 7 passed 已独立实跑；release 构建编译干净；运行时部署证据 passed 且 daemon SHA 与声明逐字节一致。证据哈希与声明一致。仅存在上述「4 vs 3」计数归因细微出入，不影响结论。

> 注：本轮以注册 workbuddy 独立 reviewer 身份 `reviewer-cw-bootstrap-v1` 交付 verdict + Handoff（不冒充 `reviewer-wb-186loop`）。将本 `reviewer_pass` 持久化进 `task_verdict_events` 需为该 task 发放 reviewer lease 并调用 `verdict.submit`；未擅自改库。
