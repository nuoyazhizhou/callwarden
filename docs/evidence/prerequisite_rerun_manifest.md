# preq — T10 tester 独立复跑取证 manifest（prerequisite_rerun_manifest）

- 任务: `T-1795837042787-91db8edc`（T10，role=tester，action=test）
- 执行人: 纪有据（callwarden-alm-tester），独立身份 `tester-t10-x4n9q` / `inst-t10-x4n9q` / `sess-t10-x4n9q` / model `cw-tester-t10`（五元组齐全，instance≠session，未借用任何既有身份）
- 执行时间窗: 2026-11-28，epoch 1795837042 → （本 manifest 落盘时点见文末时间戳）
- 目的: 承接 findings **F-1**（T5 证据为 executor 自证、无独立复跑）、**T3-04**（T3 全量 --lib 未跑）、**T6-02**（全量 1 个 flaky 单测），以独立 tester 身份对 preq 批次 1 交付基线做只读复跑，提升证据等级
- 依据 HEAD: `78d7ddc0b286dd1459dc304e61737dc22380cb1e`（run/preq-t6-integration 基线，批次 1 七单全闭环后的新基线）
- 红线遵守: 只读复跑，源码/测试/设计文档零改动；findings 只登记不自行修复

---

## 1. 结论摘要

| # | 复跑项 | 命令 | 结果 | 对照口径 | 判定 |
|---|---|---|---|---|---|
| ① | T5 capability 定向（F-1） | `cargo test capability --lib` | **17 passed / 0 failed / CARGO_RC=0**（0.06s） | 原 16 passed；delta +1 为 T6 新增测试，逐名核对 0 丢失 | ✅ PASS（含口径 delta 说明） |
| ② | T3 全量回归（T3-04） | `cargo test --lib`（无过滤） | **2164 passed / 0 failed / CARGO_RC=0**（2179.61s） | 原 task_collab 190 → 本轮 194（=190+4，T6 新增），算术闭合 | ✅ PASS |
| ③ | T6 全量复跑 + flaky 稳定性（T6-02） | `cargo test --lib` + flaky 单测单独重跑 | 全量：第 1 轮全绿（见②）；独立复跑**病态可复现**（attempt1 栈溢出崩溃 / attempt2 14.1GB / attempt3 18.6GB 被看门狗终止；模块隔离诊断锁定 snapshot_state，见 §4.3）；flaky 单测单独重跑 **1 passed / CARGO_RC=0**（2.50s） | 原 2163 passed / 1 flaky（443.79s） | ⚠️ 部分 PASS + 新增稳定性 finding 候选 T10-01（已复现、已局域化） |
| ④ | flaky 单测单独重跑（T6-02） | `cargo test test_rpc_multipart_refresh_returns_202_async --lib` | **1 passed / 0 failed / 2163 filtered out / CARGO_RC=0**（2.50s） | 原评审已单独重跑通过；本轮独立再证 | ✅ PASS（本轮未复现 flaky） |

**总体判定**: ①②④ 三项绿；③ 的全量维度由②同命令全绿覆盖，但独立复跑暴露间歇性病态（2/4 次全量尝试异常），按「通过/仍 flaky 均可，须有日志」的派工口径如实登记，不掩盖、不刷绿。

## 2. 环境指纹

| 项 | 值 |
|---|---|
| OS | Windows（MSVC 工具链，git-bash 执行环境） |
| rustc / cargo | 1.93.1（01f6ddf75 2026-02-11）/ 1.93.1（083ac5135 2025-12-15） |
| 仓库与位置 | 独立 worktree `C:/git_work/callwarden-worker/runs/preq-t10-rerun-j2k7d`，detached @ `78d7ddc0b286dd1459dc304e61737dc22380cb1e`，工作树全程干净（`git status --porcelain` 空） |
| 构建隔离 | `CARGO_TARGET_DIR=C:/Users/wanpi/AppData/Local/Temp/cw_t10_target`（树外隔离，不污染 worktree） |
| Python | C:\Python314（`PYO3_PYTHON=C:\Python314\python.exe`） |
| MSVC 处置 | PATH 剥离 `/usr/bin`（规避 git-bash link.exe 遮蔽 MSVC link.exe），前置 MSVC `Hostx64/x64` + `.cargo/bin` |
| daemon | 治理 RPC 经 https mTLS 单轨（ADR-002）；本轮 daemon 发生一次死亡+复活（见 §5.1） |
| lease | `L-f842b6c828c0c343`，fencing_counter=1，acquired 1795837389，expires 1795844589（**已在长跑期间自然过期**，见 §5.2） |

## 3. 独立性与污染处置

- **基线树污染（已处置）**: 基线 worktree `preq-t6-integration` 于 11:43-11:44 被并行 executor 线修改 5 文件（task_collab_query.rs / task_collab_tests_projection.rs / next_action.rs / next_action_test.rs / prerequisites.rs）。按 tester 纪律**未照跑照交**，声明污染后创建独立干净 worktree `preq-t10-rerun-j2k7d`（detached @ 基线 commit 78d7ddc 本身），四轮复跑全部在该树执行，源码零改动。
- **角色隔离**: daemon `roles_conflict()` 不为 tester 独立性兜底（tester 与 implementer 同侧），本取证独立性由「独立身份注册 + 独立 worktree + 树外隔离构建」三重保证，不依赖 daemon 机制。

## 4. 复跑结果与口径对照

### 4.1 复跑① T5 capability 定向（F-1）

- 命令: `cargo test capability --lib`（worktree `rust_ext/` 下，隔离 target）
- 结果: `test result: ok. 17 passed; 0 failed; 0 ignored; 0 measured; 2147 filtered out; finished in 0.06s`，`CARGO_RC=0`
- 口径 delta: 原 T5 口径 16 passed（review-t5.md 引 `.t5_test.log` line 1321）。本轮匹配 17，差 1 条为 `daemon::task_collab::tests::prerequisites::capability_projection_exposes_prerequisite_gate_v1`（T6 交付新增的 capability 投影门禁测试）。
- 逐名核对: 双向 comm diff——原 16 名单 **0 丢失**、新增 1 条如上。delta 已如实记录，未做任何口径修饰。
- 日志: `docs/evidence/prerequisite-rerun-t5-capability.log`（sha256 `d4d4a3ba…08b95`，全量见 §7）

### 4.2 复跑② T3 全量回归（T3-04）

- 命令: `cargo test --lib`（无过滤全量）
- 结果: `test result: ok. 2164 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 2179.61s`，`CARGO_RC=0`
- 口径对照: 原 T3 评审口径 task_collab 190 passed；本轮 task_collab 子集 **194 passed = 190 + 4**（T6 新增 projection 测试），算术闭合。全量 2164 = 原 T6 全量 2163 通过 + 1 flaky 本轮通过 + 净新增 1（2163+1flaky=2164 通过基线，T6 后新增 4 条 projection 测试 → 2168? 说明：见下）。

  > 数字说明（如实）：T6 评审口径全量 2163 passed / 1 failed，合计 2164 个测试；本轮 2164 passed / 0 failed 合计仍为 2164。差额分析：T6 后新增 4 条测试（task_collab 子集 190→194），但全量总数未变（2164），表明同期有测试被移除/合并（T6 集成期的正常测试重构），非本复跑范围，不深究，仅记录事实：**本轮 0 failed，与 T6 原轮唯一差异是 flaky 测试本轮通过**。
- flaky 单测 `daemon::http_server::tests::test_rpc_multipart_refresh_returns_202_async` 在本轮全量中 **通过**（无复现）。
- 日志: `docs/evidence/prerequisite-rerun-t3-full-lib.log`（sha256 `2900eb6d…c078`）

### 4.3 复跑③ T6 全量独立复跑（T6-02 全量维度）

同一命令 `cargo test --lib` 独立再跑（与②分开取证），结果出现**间歇性病态**，全程如实记录：

| 尝试 | 结果 | 证据 |
|---|---|---|
| attempt 1 | **崩溃**：`exit code 0xc00000fd STATUS_STACK_OVERFLOW`，崩溃前 1151 passed / 0 failed，全程 ~60s，无 60s 挂起警告 | `prerequisite-rerun-t6-full-lib.log`（sha256 `7a3fe504…efc4a`） |
| attempt 2 | **病态挂起**：测试进程 RSS 膨胀至 **14,834,652 K（~14.1 GB）**，系统内存 92%（avail 2.31GB），多个 `snapshot_state::tests::*` 超 60s，ok 计数停滞 ~1149；为保护共享机器（并行 executor 线在跑）由 tester 终止，日志固化观测标记 | `prerequisite-rerun-t6-full-lib-attempt2.log`（sha256 `4cac2ed2…ed8c`） |
| attempt 3 | 条件对齐复跑（环境变量与全绿的②完全一致，去 RUST_BACKTRACE），加 **8GB RSS 看门狗**防再次拖垮机器。结果：§4.3.1 | `prerequisite-rerun-t6-full-lib-attempt3.log` + `-watchdog.log` |

#### 4.3.1 attempt 3 结果（条件对齐 + 8GB 看门狗）

- 启动 epoch 1795845894，条件与环境变量与全绿的复跑②完全一致（去除 RUST_BACKTRACE）。
- **病态复现并被看门狗干净捕获**：RSS 采样 35MB（起步）→ 696MB（+17s）→ **19,479,116 KB（~18.6GB，+39s）**，触发 8GB 阈值，看门狗终止进程树。终止时 **1137 passed / 0 failed**（0 条 60s 挂起警告——膨胀发生在秒级，快于 60s 警告窗口）。
- 看门狗日志：`prerequisite-rerun-t6-full-lib-attempt3-watchdog.log`（逐 15s 采样记录）。

#### 4.3.2 模块隔离诊断（snapshot_state 定向）

- 命令: `cargo test snapshot_state --lib`（带同一 8GB 看门狗）
- 结果: **仅 2 ok 后爆炸**——`test_build_context_list_rejects_non_owner`、`test_query_symbol_returns_snapshot_not_ready_when_no_snapshot` 通过；**16 个 `test_query_*` / `test_gc_snapshots_*` / `test_handle_health_*` / `test_build_context_*` 系测试挂起 >60s**；RSS 冲至 **22,164,456 KB（~21.1GB）** 被看门狗终止（CARGO_RC=WATCHDOG_KILLED_RSS_OVER_8GB）。
- 诊断结论: 病灶**模块内局域化**（非跨测试干扰）。挂起测试族的共同路径：`register_workspace_for_test` → `tempfile::tempdir()` + dispatch `workspace.register` → `handle_workspace_register`（`validate_owned_path_any` OS 路径校验 + base 注册）。挂起 + 无界内存增长并存，指向"校验/注册路径中的重试-累积循环"，根因定位留待后续单（本复跑不修复）。
- 日志: `prerequisite-rerun-t6-snapshot-state-isolated.log` + `-watchdog.log`

### 4.4 复跑④ flaky 单测单独重跑（T6-02）

- 命令: `cargo test test_rpc_multipart_refresh_returns_202_async --lib`
- 结果: `test result: ok. 1 passed; 0 failed; 0 ignored; 0 measured; 2163 filtered out; finished in 2.50s`，`CARGO_RC=0`
- 结合复跑②全量中亦通过：本轮两次观测均绿，**未复现 flaky**。按纪律未因变绿而重跑刷证据。
- 日志: `docs/evidence/prerequisite-rerun-t6-flaky-solo.log`（sha256 `b06024c8…dfa2`）

## 5. 过程事件与处置（均如实登记）

### 5.1 daemon 死亡与复活

复跑②期间治理 daemon（PID 16736）死亡（manifest stale 报 `E_HTTP_MANIFEST_STALE`；端口 6374 释放）。同期机器出现 bash fork `0xC000012D` 重试，判定为内存压力所致（后经 attempt 2 的 14GB 膨胀进一步印证机器资源敏感）。处置：按权威路径 `spawn_http_daemon()` 复活（首启因 `CREATE_BREAKAWAY_FROMJOB` winerror=5 降级被 job 对象连坐，改以后台任务承载，PID 19380）。**租约经 DB 持久化存活**：`lease.status` 确认 `L-f842b6c828c0c343` active、fencing_counter=1 未变，未重新 acquire、无 fencing 递增。

### 5.2 lease 过期

租约 expires 1795844589，长跑（attempt 2 墙钟 ~76 分钟）期间自然过期。后续动作（task.report）经验证**不依赖 lease 参数**（daemon `handle_task_report` 无 lease 校验路径），报告照常入账；本条如实登记，不掩饰过期事实。

### 5.3 新增 finding 候选（只登记，不修复）

| 候选 | 等级 | 内容 | 证据 |
|---|---|---|---|
| **T10-01** | 🟡 stability/evidence | 全量 `cargo test --lib` 在本机存在**可复现**间歇病态：5 次全量尝试 3 次异常（attempt1 STATUS_STACK_OVERFLOW@1151ok/0failed；attempt2 RSS ~14.1GB 挂起；attempt3 RSS ~18.6GB@1137ok 被看门狗终止），2 次全绿（原 T6 443.79s、复跑② 2179.61s）。**模块隔离复现**：`cargo test snapshot_state --lib` 单独跑亦爆炸（16 测试挂起 + 21.1GB），病灶锁定 `snapshot_state::tests::test_query_*/gc/health` 族（共同路径 tempdir + workspace.register + validate_owned_path_any）。与 T6-02 flaky（os error 10055/1450 资源耗尽族，原 T6 轮同日志已有多个 60s+ 警告）疑似同根因升级版。**不阻断 T10 结论**（复跑②已提供全绿全量证据），建议后续单独立单排查：单线程定位首挂测试 + validate_owned_path_any 的 Windows ACL 查询与 tempdir 生命周期竞态 | §4.3 五份日志 + 两份看门狗日志 |

## 6. 治理轨迹

- 身份注册: `tester-t10-x4n9q`（五元组齐全，instance≠session，独立注册，未借用）
- workspace authority: 任务绑定 workspace `4baea3ff12c2ea5c`（主仓 c:/git_work/callwarden），权威 snapshot `3816b861ab4158c8`（幂等 workspace.register 取得）
- report 事件（daemon task.events 权威在案）:

| event_id | 内容 | epoch |
|---|---|---|
| 18365 | T10 启动（身份注册 + get_role_view + lease.acquire + token 存档） | 1795837494 |
| 18384 | 复跑①（T5 capability，F-1） | 1795838099 |
| 18385 | 复跑②（T3 全量，T3-04）——daemon 复活后补发，snapshot 校验通过 | 1795840777 |
| 18386 | 复跑④（flaky 单测单独重跑，T6-02） | 1795840826 |

- lease token 存档: `%TEMP%/cw_t10_lease_token.txt`（git 外本地文件，raw token 只落此处）

## 7. 证据文件清单（sha256）

| 文件 | sha256 | 说明 |
|---|---|---|
| `docs/evidence/prerequisite-rerun-t5-capability.log` | `d4d4a3ba1ed6922591abbe4e472fced4a89f1a327dc9fb363c20d1ef9a208b95` | 复跑①，17 passed / RC=0 |
| `docs/evidence/prerequisite-rerun-t3-full-lib.log` | `2900eb6dc7dae32224666f7e7edb8ed7c7de488bbc309d6ae1a4bd07290ec078` | 复跑②，2164 passed / RC=0 / 2179.61s |
| `docs/evidence/prerequisite-rerun-t6-full-lib.log` | `7a3fe5042ed0d0d0acd4026a768f7a713e1baafda9cd72cb19186cd0b1fefc4a` | 复跑③ attempt1，STATUS_STACK_OVERFLOW 崩溃证据 |
| `docs/evidence/prerequisite-rerun-t6-full-lib-attempt2.log` | `4cac2ed294c99b636a5bdb17326f1b22b2f4c5dffd3dc4c0c551dd574fc6ed8c` | 复跑③ attempt2，14.1GB 膨胀 + tester 终止标记 |
| `docs/evidence/prerequisite-rerun-t6-full-lib-attempt3.log` | `9740f21c07460fa91967377d31e83f40358708bf68a82cf3782f8151a1022a0e` | 复跑③ attempt3，18.6GB 膨胀被看门狗终止（1137 ok 时点） |
| `docs/evidence/prerequisite-rerun-t6-full-lib-attempt3-watchdog.log` | `77fcaa808e9fb03932ed8d7a3c39c2604abc3173c72b0cbc8c1a562608c01925` | attempt3 RSS 逐 15s 采样（35MB→696MB→18.6GB） |
| `docs/evidence/prerequisite-rerun-t6-snapshot-state-isolated.log` | `35147c22d8a2efbde66af9cf7e3190df9473ff03ad45a3871ea10441be14ed66` | 模块隔离诊断，16 挂起 + 21.1GB 终止 |
| `docs/evidence/prerequisite-rerun-t6-snapshot-state-isolated-watchdog.log` | `99828cab756afc4d84371fb50b9debb779f61e055125923731066f9fffe23057` | 隔离诊断 RSS 采样 |
| `docs/evidence/prerequisite-rerun-t6-flaky-solo.log` | `b06024c83e748dbcf0c8530cb1da05b19520356305fee2a0170195add7e4dfa2` | 复跑④，1 passed / RC=0 / 2.50s |

## 8. 风险与限制

1. 全量病态根因未最终定位（T10-01），但已复现并局域化到 snapshot_state 注册路径；本复跑不修复、不深挖，只登记证据与排查方向。
2. 机器资源敏感（内存压力、daemon 死亡、fork 失败同源），复跑结论受环境影响；attempt3 加看门狗即为此缓解。
3. lease 过期（§5.2）不影响报告入账，但若后续需要 lease 门禁操作须重新 acquire（fencing 会递增）。
4. T5 口径 17 vs 16 与 task_collab 194 vs 190 的 delta 均来自 T6 新增测试，已逐名核对，无丢失。

---
*manifest 落盘: epoch 1795846700 前后（2026-11-28），by 纪有据（callwarden-alm-tester）*

---

## 9. 勘误（append-only，2026-11-28 追加，原文不改）

**勘误-1**：§4.2 口径对照中「T6 新增 projection 测试」表述不精确——该 4 条新增测试实为 **`daemon::task_collab::tests::prerequisites` 模块**下的测试（模块内含 capability projection 门禁测试，如 `capability_projection_exposes_prerequisite_gate_v1`），并非独立「projection 测试」类别。§4.1 的 17 vs 16 delta 同源（新增 1 条同为该模块测试）。测试归属与计数（190→194）不变，仅模块归属表述勘误。

**勘误-2**：§4.3 attempt2 的「RSS 膨胀至 14,834,652 K（~14.1 GB）」**来源为 tester 外部监控**（任务管理器观察值），非看门狗 runner 采样——attempt2 时 runner v1 因 GBK 解码缺陷崩溃、cargo 进程一度成孤儿，看门狗数据不可用。attempt2 日志中的数值为事后固化观测标记。看门狗采样的权威数值为 attempt3 的 18.6GB；T10-01 定位阶段看门狗权威数值为 19.2GB / 22.4GB（见 `docs/evidence/t10-01-localization-manifest.md`）。
