# T10 独立评审报告（tester 复跑取证单）

- 任务: `T-1795837042787-91db8edc`（action=test，role=tester 纪有据交付）
- 评审人: reviewer-t1-t7（独立身份 sess-reviewer-t1-t7-t10，instance≠session）
- 评审方式: 独立盲评（blind_first_pass），未采信 tester 自报结论，关键数字全部独立取证
- 评审日期: 2026-11-28

## 结论：PASS

五项评审重点全部独立核验通过，无 🔴 blocker；2 条 💭 nit 不阻断（见 Findings）。

## 评审输入

- manifest: `docs/evidence/prerequisite_rerun_manifest.md`（sha256 145c411b…843303，八段式）
- 日志: `docs/evidence/prerequisite-rerun-*.log`（9 份，sha256 全量复算与 manifest §9 逐条匹配）
- 复跑树: `C:/git_work/callwarden-worker/runs/preq-t10-rerun-j2k7d`（detached @ 78d7ddc，工作树全程干净）
- 治理轨迹: daemon task_events 18365→18416（含 tester 五报 + 收尾报 18415）

## 五项评审重点逐项核验

### ① 三项复跑足以关闭 F-1 / T3-04 / T6-02 —— 成立

| 原 finding | 复跑证据 | 独立核验 |
|---|---|---|
| F-1（T5 证据 executor 自证） | 复跑① `cargo test capability --lib` → 17 passed / 0 failed / RC=0 / 2147 filtered（0.06s） | 与原 T5 日志（T-1795663552556-ba42830c-capability-test.log，16 passed）双向 comm 逐名：**0 丢失、恰 +1**（`daemon::task_collab::tests::prerequisites::capability_projection_exposes_prerequisite_gate_v1`，T6 新增，本轮亦 ok） |
| T3-04（全量 --lib 未跑） | 复跑② `cargo test --lib`（无过滤）→ **2164 passed / 0 failed / RC=0**（2179.61s） | 日志 test result 行与 RC 逐字核验；task_collab 子集对账见③ |
| T6-02（flaky 单测） | 复跑④ `cargo test test_rpc_multipart_refresh_returns_202_async --lib` → **1 passed / RC=0**（2.50s，2163 filtered） | 且全量②中该测试亦通过——本轮两次观测均绿，未复现 flaky |

### ② T10-01 登记完整性（可重放 / 局域化 / 不刷绿）—— 成立

- **可重放**: 5 次全量尝试 3 次病态。attempt1 STATUS_STACK_OVERFLOW（1151 ok / 0 failed 后崩溃，落盘）；attempt3 看门狗逐 15s 采样 35MB→696MB→**19,479,116 KB（~18.6GB）**触发 8GB 阈值终止（watchdog 日志落盘）。
- **局域化**: `cargo test snapshot_state --lib` 模块隔离复现——16 个 `test_query_*` / `test_gc_snapshots_*` / `test_handle_health_*` / `test_build_context_*` 挂起 >60s + RSS 22,164,456 KB（~21.1GB）被看门狗终止；病灶锁定 snapshot_state 注册路径（tempdir + workspace.register + validate_owned_path_any）。
- **不刷绿**: manifest ③ 明确 ⚠️ 部分 PASS，未宣称全绿；finding 候选 T10-01 已登记（stability/evidence，不阻断 T10 结论）；后续单 `T-1795847624350-48202e98`（register_workspace_for_test fix）已立。

### ③ 口径 delta（17 vs 16、194 vs 190）双向逐名如实性 —— 成立

- **17 vs 16**: 双向 comm 逐名闭合（0 丢失、恰 +1，见①）。原 16 名单取自落盘的原 T5 日志，可信。
- **194 vs 190**: 原 190 回归日志未落盘（仅 review-t3.md 记数 + daemon 报告），无法直接 comm。以**源码结构核对闭环**：`git grep -c "#\[test\]"` 逐文件计数——68e57a4（T3 基线）合计恰 **190**（governance 3 + planning 8 + bridge 6 + cascade 3 + core 40 + tests_governance 19 + lease 42 + owner_acl 6 + p0l_repair 7 + parent_create 8 + projection 35 + verdict_compat 2 + verdict_gate 4 + ws_authority 7）；78d7ddc 合计恰 **194**；两 commit 逐文件全部一致、唯一差异 `task_collab_tests_prerequisites.rs` +4。重跑日志 task_collab 行恰 **194**，4 条新测试以 `daemon::task_collab::tests::prerequisites::` 全路径出现并 ok。manifest「194=190+4 算术闭合、无丢失」属实。
- **附带如实性**: 全量总数 2163→2164 与 +4 新增并存意味同期有测试移除/合并——tester 已在 manifest §4.3 主动披露并声明「非本复跑范围，不深究，仅记录事实」。如实，非隐瞒。

### ④ 独立性 —— 成立

- 五元组: DB 核验 tester 注册行（inst-t10-x4n9q ≠ sess-t10-x4n9q，未借用既有身份）。
- 独立 worktree: preq-t10-rerun-j2k7d detached @ 78d7ddc，工作树全程干净（git status --porcelain 空）。
- 污染处置: 指派基线树被并行 executor 在途改动（5 文件 mtime 11:43-11:44），依纪律换独立 worktree 并通报。
- 树外隔离构建: 独立 CARGO_TARGET_DIR + MSVC 环境（PATH 剥离 /usr/bin 等），与 T1 评审确立的隔离方法一致。

### ⑤ manifest sha256 抽查 —— 成立

manifest 自身 + 9 份日志全量复算，与 manifest §9 清单逐条匹配（含 manifest 145c411b…843303）。

## Findings（2×💭，均不阻断）

- 💭 **attempt2 的 14.1GB 数字未落日志**（`docs/evidence/prerequisite-rerun-t6-full-lib-attempt2.log` vs manifest §4.2/§5）：attempt2 的「RSS 14.1GB」仅 manifest 文字记载，attempt2 日志本身无任何 RSS 采样（日志尾仅 `CARGO_RC=TERMINATED_BY_TESTER(machine_protection)`），严格意义上不可由日志独立重建。病态结论不单独依赖该数字（attempt1 栈溢出、attempt3 看门狗逐 15s 采样、隔离诊断 21.1GB 三路均已落盘）。建议：T10-01 排查单补齐 attempt2 资源采样证据，或 manifest 标注该数字来源（tester 口述/外部监控）。
- 💭 **「projection 测试」命名欠精确**（manifest 行 17/55）：manifest 将 task_collab +4 称为「projection 测试」，实际为 `task_collab::tests::prerequisites` 模块（capability 投影 1 条 + claim 门禁集成 3 条：disabled 兼容 / satisfied 放行+投影空 / unmet 阻断+三投影一致）。计数正确（194=190+4 双向核对 0 丢失），仅措辞问题。

## 覆盖矩阵

| 评审重点 | 证据 | 覆盖 | 缺口 |
|---|---|---|---|
| ① F-1/T3-04/T6-02 关闭 | 复跑①②④日志 + 原 T5 日志 comm | 全覆盖 | 无 |
| ② T10-01 完整性 | attempt1/attempt3/隔离诊断日志 + 后续单 | 全覆盖 | attempt2 RSS 数字（见 💭） |
| ③ delta 如实性 | 双向 comm（T5）+ 双 commit 逐文件计数（T3） | 全覆盖（源码闭环替代缺失的原 190 日志） | 原 190 日志未落盘（结构核对替代） |
| ④ 独立性 | DB 五元组 + worktree 状态 + 构建隔离 | 全覆盖 | 无 |
| ⑤ sha256 | 全量复算 | 全覆盖 | 无 |

## 未覆盖路径

1. attempt2 的 14.1GB RSS 数字（💭，不可由日志独立重建）。
2. 原 T3 190 回归的原始日志未落盘——以双 commit 源码计数闭环替代（190/194 逐文件一致）。
3. T10-01 根因未定位（tester 已如实声明；后续单 T-1795847624350-48202e98 排查，本评审不越界）。
4. 全量 2163→2164 的测试移除/合并明细（tester 已披露不深究，非本复跑范围）。

## 证据等级声明

- 本评审未改任何源码/文档/证据/任务状态（只读评审 + A′ 环 verdict 流）。
- 关键数字均独立取证：sha256 全量复算、双向 comm、双 commit 逐文件 #[test] 计数、DB 只读查询（tasks/task_events/task_leases/task_verdict_events/role_contract_lineages）。
- 原 190 日志缺失以源码结构核对替代，已在③声明方法与局限。

## 治理过程记录（A′ 环）

1. 注册: sess-reviewer-t1-t7-t10（agent_instance_id=reviewer-t1-t7-inst，instance≠session，未借用）。
2. get_role_view: reviewer blind 视图，view_manifest_hash 3047cea8…e32c。
3. lease.acquire: L-9883582c4592adb7（fencing=1）→ verdict 首提被拒（见下）→ release → 重取 L-d4ad981d74034339（**fencing=2**，如实记）。
4. verdict.submit: **V-e578422ed60d1e4087dde9be**（verdict event id 936，phase=blind_first_pass，overall=pass，replayed=false，snapshot 5fa3d5281290b9e3）。
5. lease.release: L-d4ad981d74034339 已释放（1795852522）。
6. raw token 存档 git 外本地文件（`%USERPROFILE%\.callwarden-reviewer\lease_t10_token*.txt`，chmod 600）。

### 治理事故披露：daemon 陈旧读导致 verdict 三度被拒

- 现象: verdict.submit 三度报 E_VERDICT_TASK_NOT_IN_REVIEW（任务 in_progress），同期 task.status_tree（同一 daemon 同一 store）返回 review、DB 只读连接与 Python IMMEDIATE 事务均读 review。
- 根因: tester 收尾报（事件 18415，任务 in_progress→review）当时仅落 WAL 未 checkpoint 进主文件；daemon verdict 写事务路径读到陈旧页（主文件仍 in_progress），autocommit 读路径可见 WAL 新帧。另：首次失败后同 request_id 的两次重试被 daemon dedup 缓存拦截重放（E_REQUEST_ID_REUSE_MISMATCH 证实），未真实执行——checkpoint 修复一度未被公平测试。
- 处置: ①`PRAGMA wal_checkpoint(PASSIVE)`（693/693 帧合并，无损，仅持久化已提交状态）；②换新 request_id（…-02）+ 新租约（fencing=2）真实重提 → 成功。
- 未做: 未直写 SQLite 治理库（守「治理写必走 daemon RPC」红线）、未重启 daemon、未改任何任务状态。
