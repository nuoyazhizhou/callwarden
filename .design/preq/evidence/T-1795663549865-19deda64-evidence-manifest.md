# 证据清单（evidence manifest）— T4 claim 硬依赖门禁

- **Task**: T-1795663549865-19deda64（T4 claim 硬依赖门禁，REQ-PREQ-02 / AC-07/08/09/10）
- **Role**: tester（纪有据）
- **身份**: tester-t4-jyj41 / sess-tester-t4-jyj41，independence=local_declaration_new_session（daemon 不可用 E_HTTP_MANIFEST_STALE，本地声明全新会话身份；与 executor inst-t4-exec-9c2f、reviewer reviewer-t4-9c2f-4d17 均不同，未复用任何已有身份）
- **取证时间**: 2026-11-27 09:36–09:41 +0800
- **对象**: worktree `C:/git_work/callwarden-worker/runs/impl-t4-claim-9c2f`，分支 `impl/t4-claim`，HEAD `40a21d4c184b7d45f28ee5f83c53112c7d73f31b`（= origin/master）；未提交改动仅 `claim.rs`（+126/-20）与 `claim_test.rs`（+267/-1），合计 +393/-21

## 一、证据文件清单与 sha256

| 文件 | sha256 | 说明 |
|---|---|---|
| `T-1795663549865-19deda64-AC-07.md` | 3bf795b4af1e8ff1e0e64757eee5fb335e31173aaeaa4b065bc9f135fa386da6 | AC-07 八段证据（PASS） |
| `T-1795663549865-19deda64-AC-08.md` | f16b9502d106b1362b758a09991fbdae19def00c6243ffdb810838d1e6200dd3 | AC-08 八段证据（PASS） |
| `T-1795663549865-19deda64-AC-09.md` | 625224824e0239168a5da28bf29ce64871fb9ab7d2afbb770bea4bc3f43ac099 | AC-09 八段证据（PASS） |
| `T-1795663549865-19deda64-AC-10.md` | 9c895190c8566ea19e8cb4e4042b8b8ed682a19847e342b7906c94c6d33cad94 | AC-10 八段证据（PASS） |
| `T-1795663549865-19deda64-rerun-raw.log` | 013add3c6434acba92518c56470bf75d04f96954a4511bdec2ef2d80bbb83098 | 本轮独立重跑原始日志（1346 行，含 CARGO_RC=0） |
| `T-1795663549865-19deda64-test-list.txt` | 92ca0cba99592095485f28f66db24e3b3f5320994cb79c694fa900f74c461a61 | `cargo test claim --lib -- --list` 输出（57 tests, 0 benchmarks，CARGO_RC=0） |

参考件（executor 绿轮日志，存于 worktree 根，未复制改动）：`.t4_test.log` sha256=96c5c0dbd47261db7fa4c106596a4b68297d3a78bd1c57918a0b7ea8a82db8d1（1346 行，line 1344 `test result: ok. 57 passed; 0 failed; 0 ignored; 0 measured; 2098 filtered out; finished in 4.07s`，line 1346 CARGO_RC=0）。

## 二、环境指纹

- OS：Microsoft Windows 10.0.26300.9974（x86_64）；Git Bash（MINGW64）3.6.9
- rustc 1.93.1 (01f6ddf75 2026-02-11)；cargo 1.93.1 (083ac5135 2025-12-15)
- MSVC link.exe 14.44.35207（Hostx64/x64）
- Python 3.14.3（PYO3_PYTHON=C:\Python314\python.exe）
- 隔离构建：CARGO_TARGET_DIR=C:/Users/wanpi/AppData/Local/Temp/cw_t4_target；PATH 前置 MSVC link.exe 目录、剥除 /usr/bin、保留 ~/.cargo/bin、补 /c/Python314
- 源文件 sha256（运行时状态）：claim.rs=05efa7895eb8214e9a5c23b76b09897a6fec9f8941f1e2b50ca4c20b688ba978；claim_test.rs=a76cb4e2e30a85978b393bcfbff9b225cc65d60d7f25fca4581825f7633bb34a
- 基线（HEAD）sha256：claim.rs=105fe116…、claim_test.rs=d62feced…（与主检出树当前文件一致——主树未含 T4 改动，本取证未触碰主树）
- 时间链：源最后修改 09:23（claim_test.rs 09:23:07 / claim.rs 09:23:14）→ executor 绿轮 09:25:14 → 本轮独立重跑 09:36:42，全程源未变

## 三、命令原文

```bash
cd /c/git_work/callwarden-worker/runs/impl-t4-claim-9c2f/rust_ext
STRIPPED=$(echo "$PATH" | tr ':' '\n' | grep -v '^/usr/bin$' | tr '\n' ':')
export PATH="/c/Program Files/Microsoft Visual Studio/2022/Community/VC/Tools/MSVC/14.44.35207/Hostx64/x64:/c/Users/wanpi/.cargo/bin:/c/Python314:$STRIPPED"
export CARGO_TARGET_DIR="C:/Users/wanpi/AppData/Local/Temp/cw_t4_target"
export PYO3_PYTHON="C:\\Python314\\python.exe"
cargo test claim --lib > /c/git_work/callwarden/.design/preq/evidence/T-1795663549865-19deda64-rerun-raw.log 2>&1
echo "CARGO_RC=$?" >> /c/git_work/callwarden/.design/preq/evidence/T-1795663549865-19deda64-rerun-raw.log
cargo test claim --lib -- --list > /c/git_work/callwarden/.design/preq/evidence/T-1795663549865-19deda64-test-list.txt 2>&1
```

## 四、统计

| 项 | 值 |
|---|---|
| collected（`--list`） | 57 tests, 0 benchmarks |
| passed | 57 |
| failed | 0 |
| ignored / skipped | 0 / 0（cargo 无 xfail 概念，`0 ignored` 见 test result 行） |
| filtered out | 2098（不含 "claim" 子串的其它模块测试，非 required 被 deselect） |
| executed | 57 |
| rc | CARGO_RC=0 |

## 五、六条件判定（test-cw 判据，Rust cargo 适配口径）

1. **双 revision 一致 ✅**：run_revision = code_revision = 工作树状态（HEAD 40a21d4 + 未提交 T4 改动）；源 sha256 运行前后一致；测试期间无任何 git 写操作（无 commit/stash/checkout），worktree 全程仅 2 个改动文件。
2. **required nodeid 全 passed ✅**：57/57 ok，含 5 个新 prereq 测试（rerun line 1303-1307）与 17 个存量 claim_test 测试（line 1294-1317）及 35 个跨模块 claim 测试。
3. **required 集 skip/xfail=0 ✅**：`0 ignored`；cargo 无 xfail 概念，skipped=0。
4. **无 deselect ✅**：命令仅含定义 required 集的 `claim` 子串过滤（AC 验收方式即「`cargo test` claim 门禁测试」），未加任何额外过滤参数；2098 filtered out 全部为不匹配 "claim" 的其它模块测试，**不是**过滤掉 required。
5. **deselected ∩ required = ∅ ✅**：required 集定义 = "claim" 过滤匹配集 = 57 个测试；filtered out 2098 与其交集为空。
6. **rc=0 且 collected 校验 ✅**：CARGO_RC=0；collected=57（--list）；executed=57（test result 行 `57 passed`）；**collected == executed == len(required) == 57**。

**可复现性对账**：本轮重跑与 executor 绿轮（.t4_test.log）的 57 测试集合排序后逐字节一致（SET_IDENTICAL）；差异仅构建耗时（0.99s/2.47s）、并行完成顺序与总耗时（4.07s/3.15s）——符合 Rust 并行测试的预期非确定性。

## 六、必需 nodeid 完成表（脚本自 rerun-raw.log 生成，非手写）

完整 57 测试（行号 = rerun-raw.log 行号，状态全部 ok）：

```
1286 | cli::status::tests::enterprise_payload_does_not_claim_filesystem_freshness | ok
1287 | daemon::assignment_queue::tests::claim_persists_normalized_row_into_task_assignments | ok
1288 | daemon::assignment_queue::tests::replayed_queue_and_same_holder_claim_are_idempotent | ok
1289 | daemon::assignment_queue::tests::queue_claim_heartbeat_and_complete_are_replayable_events | ok
1290 | cli::task::tests::blocking_finding_blocks_normal_step_claim | ok
1291 | cli::task::tests::claim_report_and_reopen_preserve_parent_state_machine | ok
1292 | cli::task::tests::immediate_transaction_allows_only_one_claim | ok
1293 | daemon::task_collab::tests::lease::test_task_claim_concurrent_session_conflict | ok
1294 | daemon::task_loop::claim_test::claim_rejects_broken_binding_chain | ok
1295 | daemon::task_loop::claim_test::claim_rejects_cross_task_lineage | ok
1296 | daemon::task_loop::claim_test::claim_rejects_missing_revision | ok
1297 | daemon::task_loop::claim_test::claim_rejects_step_not_owned_by_task | ok
1298 | daemon::task_loop::claim_test::claim_rejects_task_without_binding | ok
1299 | daemon::task_loop::claim_test::deterministic_reject_persists_replayable_ledger_error | ok
1300 | daemon::task_loop::claim_test::from_params_rejects_missing_fields | ok
1301 | daemon::task_loop::claim_test::first_claim_writes_binding_revision_1 | ok
1302 | daemon::task_loop::claim_test::no_remediation_but_provided_remediation_id_rejected | ok
1303 | daemon::task_loop::claim_test::prereq_engine_db_error_rolls_back_without_partial_write | ok
1304 | daemon::task_loop::claim_test::prereq_disabled_bypasses_gate_and_matches_baseline | ok
1305 | daemon::task_loop::claim_test::prereq_hard_satisfied_allows_claim | ok
1306 | daemon::task_loop::claim_test::prereq_hard_unmet_blocks_claim_with_zero_side_effect | ok
1307 | daemon::task_loop::claim_test::prereq_informational_unmet_warns_but_allows | ok
1308 | daemon::task_loop::claim_test::read_projection_unverified_when_lineage_mismatch | ok
1309 | daemon::task_loop::claim_test::remediation_exact_claim_succeeds | ok
1310 | daemon::task_loop::claim_test::remediation_requires_explicit_step | ok
1311 | daemon::task_loop::claim_test::replay_of_committed_key_does_not_dup | ok
1312 | daemon::task_loop::claim_test::second_claim_appends_revision_2_superseding_1 | ok
1313 | daemon::task_loop::claim_test::task_level_provenance_cannot_bypass_failed_step | ok
1314 | daemon::task_loop::claim_test::task_level_remediation_claim_succeeds | ok
1315 | daemon::dispatch::tests::test_task_next_action_projection_carries_policy_and_claim_requirements | ok
1316 | daemon::task_loop::next_action_test::fresh_claimed_assignment_keeps_in_progress_step_waiting | ok
1317 | daemon::task_loop::claim_test::task_level_remediation_without_provenance_rejected | ok
1318 | daemon::task_loop::next_action_test::orphaned_in_progress_step_without_claimed_assignment_is_ready_to_reclaim | ok
1319 | daemon::task_loop::next_action_test::stale_claimed_assignment_is_ready_for_atomic_takeover | ok
1320 | daemon::task_prompt::route_tests::task_prompt_renderer_route_maps_ready_claim_to_executor_contract | ok
1321 | daemon::task_loop::next_action_test::unclaimed_executor_step_ready_claim | ok
1322 | daemon::task_prompt::context_tests::task_prompt_context_aggregates_ready_claim_snapshot | ok
1323 | daemon::task_collab::tests::lease::test_same_role_fresh_claim_remains_conflict | ok
1324 | daemon::task_prompt::rpc_tests::task_prompt_rpc_happy_round_trip_ready_claim | ok
1325 | daemon::task_collab::tests::governance::test_stale_claim_cannot_be_taken_over_by_different_role | ok
1326 | daemon::task_collab::tests::governance::test_claim_contract_skill_mismatch_rejected | ok
1327 | daemon::task_collab::tests::lease::test_task_claim_dedup_idempotent | ok
1328 | daemon::task_collab::tests::governance::test_claim_contract_task_requires_identity | ok
1329 | daemon::task_collab::tests::governance::test_claim_unregistered_identity_fail_closed | ok
1330 | daemon::task_collab::tests::governance::test_claim_envelope_returns_role_contract | ok
1331 | daemon::task_collab::tests::governance::test_orphan_claim_recovery_rejects_fresh_owner | ok
1332 | daemon::task_collab::tests::projection::test_task_claim_unknown_or_unresolved_policy_is_fail_closed | ok
1333 | daemon::task_collab::tests::core::test_failed_report_preserves_scope_and_requires_remediation_claim | ok
1334 | daemon::task_collab::tests::governance::test_orphan_claim_recovery_requires_stale_owner_and_preserves_step_state | ok
1335 | daemon::task_collab::tests::lease::test_task_claim_returns_step_details_contract | ok
1336 | daemon::task_collab::tests::projection::test_task_claim_role_worker_v1_role_anchor_is_worker_mapping_not_runtime_identity | ok
1337 | daemon::task_collab::tests::lease::test_task_claim_marks_step_in_progress | ok
1338 | daemon::task_collab::tests::lease::test_task_claim_resume_same_session_returns_in_progress_step | ok
1339 | daemon::task_collab::tests::lease::test_task_claim_without_steps_omits_step_fields | ok
1340 | daemon::task_collab::tests::projection::test_task_claim_role_worker_v1_requires_expected_worker_and_records_provenance | ok
1341 | daemon::task_collab::tests::lease::test_same_role_stale_claim_is_taken_over_atomically | ok
1342 | daemon::task_collab::tests::projection::test_task_claim_legacy_and_policyless_tasks_keep_original_path | ok
```

## 七、判定汇总

| AC | 判定 | 主测试 | 证据文件 |
|---|---|---|---|
| AC-07 | **PASS** | prereq_hard_unmet_blocks_claim_with_zero_side_effect（+ prereq_engine_db_error_rolls_back_without_partial_write） | T-1795663549865-19deda64-AC-07.md |
| AC-08 | **PASS** | prereq_hard_satisfied_allows_claim | T-1795663549865-19deda64-AC-08.md |
| AC-09 | **PASS** | prereq_informational_unmet_warns_but_allows | T-1795663549865-19deda64-AC-09.md |
| AC-10 | **PASS** | prereq_disabled_bypasses_gate_and_matches_baseline + 存量套 57/57 全绿 | T-1795663549865-19deda64-AC-10.md |

无 N/A 项，无 FAIL 项。

## 八、红→绿链路登记（如实，不得粉饰）

首轮红（`prereq_hard_unmet` panic@claim_test.rs:757，FAILED 56/1）的原始日志（143KB 版本）已被后续重写覆盖丢失，**现仅存最终绿轮**（executor 绿轮 `.t4_test.log` + 本轮独立重跑）。红→绿链路以二手证据为据：

1. 主理人核验记录：首轮日志曾含 `line 2770 test result: FAILED. 56 passed; 1 failed` 与 `line 4119 test result: ok. 57 passed`；
2. reviewer 静态分析：`message.contains` 断言可再红（删掉 `prerequisite_not_met` 中 provider 拼接即失败，review-t4.md 审查点 6），首轮红的修复在代码层面合法（领域 message 由 gate 正确产生；transport 层只验稳定 code 符合 foundation 既有语义）。

此登记已写入每份 AC 证据的「缺口与例外」节。

## 九、环境与漂移风险

- **漂移**：无。运行前后源 sha256 一致、无 git 写操作、worktree 改动文件集合不变（仅 claim.rs / claim_test.rs）。
- **主检出树隔离**：主树（/c/git_work/callwarden）存在他人 in-flight 改动（fs_handlers.rs +1216/-76、task-map.md +129、HEAD 已前移至 1af24548）——本取证全程未触碰、未参考主树源码（哈希对账已确认主树 claim 文件=基线）。
- **daemon**：不可用（E_HTTP_MANIFEST_STALE），本验证不依赖 daemon，未启停、未写。
- **构建缓存**：全热（与 executor 共用 CARGO_TARGET_DIR，源未变、cargo 指纹校验通过）；全冷复现需清空该目录后重跑同一命令。
- **编译警告**：167 条均为存量（无一涉及 claim.rs / claim_test.rs），不影响判定。
