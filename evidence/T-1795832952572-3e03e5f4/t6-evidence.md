# T6 证据：启动加载回归锁与逐 ws 启动日志（step#1）

- 任务：T-1795832952572-3e03e5f4（step S-1795832952577-3e4ce574，action=test）
- 日期：2026-11-28
- 交付 commit：e210f3c（单文件路径限定提交，rust_ext/src/bin/cw_daemon.rs，+406/-4）
- step#0 证据：`.design/daemon-resource-governance/evidence/startup-load-inventory.md`（commit 387a22e）

## 改动清单（全部在 rust_ext/src/bin/cw_daemon.rs）

| 符号 | 类型 | 说明 |
|---|---|---|
| `classify_startup_recover` | 新增纯函数（平台无关公共区） | 逐 ws 状态判定：cache 已有→reused；handler 成功且有构建产出（applied/retry_recovered/retry_snapshot_published 任一>0）→built；其余（0-pending 快路径/deferred/dispatch 失败）→skipped |
| `format_startup_recover_ws_line` | 新增纯函数 | 逐 ws 日志行：`[cw_daemon] [INFO] startup recover: ws=<id> status=<built\|reused\|skipped> applied=<n> retry_recovered=<n> pending=<n>` |
| `format_startup_recover_summary_line` | 新增纯函数 | 汇总日志行：`... startup recover summary: total=<n> built=<n> reused=<n> skipped=<n>` |
| `recover_all_workspaces_with_snapshot`（unix L1231 起 / windows L3158 起，两份生产副本同步改造） | 修改 | fn→pub(crate) fn（供平台无关测试引用）；循环内 dispatch 前 `SnapshotCache::get` 判 reused；dispatch 后按 handler JSON 判 built/skipped；逐 ws eprintln + 末尾汇总 eprintln；state 构造前克隆 `cache_for_status` 判定引用（原 Arc move 进 state 后仍需查询） |
| `mod startup_recover_tests` | 新增测试模块（平台无关） | cfg 切换引用 unix/windows 两份生产副本，Windows/Linux CI 共用同一测试集 |

main 调用点（L442-448）与函数签名（返回 usize）未变；workspace.rs 本单只读未改。

## 测试结果（Windows 本机实测）

命令：`CARGO_BUILD_JOBS=2 cargo test --manifest-path rust_ext/Cargo.toml --bin cw-daemon`

```
running 7 tests
test server_cert_expiry_tests::test_expiry_message_boundaries ... ok
test startup_recover_tests::classify_startup_recover_states_and_priority ... ok
test startup_recover_tests::startup_recover_log_line_formats ... ok
test startup_recover_tests::ac_rg_03_1_zero_pending_workspace_no_snapshot_build ... ok
test startup_recover_tests::ac_rg_03_1b_empty_staging_log_zero_pending ... ok
test startup_recover_tests::ac_rg_03_1c_pending_without_publisher_deferred_no_build ... ok
test startup_recover_tests::ac_rg_03_2_cache_prepopulated_is_reused_not_rebuilt ... ok

test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 84.89s
```

测试与 AC 对应：

- **AC-RG-03-1**（0-pending 不构建回归锁）：
  - `ac_rg_03_1_zero_pending_workspace_no_snapshot_build`：注册 ws（无 staging.log）→ 启动恢复 → 断言 `recovered==0` 且 `cache.get(ws_id).is_none()` 且 `cache.len()==0`
  - `ac_rg_03_1b_empty_staging_log_zero_pending`：空 staging.log（0-pending 另一形态）→ 同上断言
  - `ac_rg_03_1c_pending_without_publisher_deferred_no_build`：有 3 条 pending 但 codegraph db/publisher 不可用 → deferred 分支不构建（cache 无）+ durable pending 保留 3 条（不丢数据）
- **AC-RG-03-2**（逐 ws 启动日志 built/reused/skipped）：
  - `classify_startup_recover_states_and_priority`：7 组判定断言（0-pending→skipped、applied>0→built、retry 重放→built、cache 优先→reused、deferred→skipped、handler 失败即使有计数也不记 built）
  - `startup_recover_log_line_formats`：逐 ws 行含 ws=/status=/applied=/retry_recovered=/pending=；汇总行含 total=/built=/reused=/skipped=
  - `ac_rg_03_2_cache_prepopulated_is_reused_not_rebuilt`：cache 预置条目 → 启动恢复不驱逐不重建，条目存活
- **AC-RG-03-3**（盘点证据文档）：见 step#0 证据（startup-load-inventory.md，五项 check_items 全覆盖）

## 耗时对照（排除本次改动引入）

端到端测试单测试 ~64s（Windows 本机）。对照既有 lib 测试（本次未改动）：
`cargo test --lib test_recover_returns_zero_when_no_staging_log` → 102.62s 单测试通过。
结论：耗时为 handle_workspace_recover handler 链路在 Windows 本机的固有行为，与本次改动无关（本次改动仅加判定与日志，无等待逻辑）。

## 偏差登记（需 reviewer 知悉）

1. **AC-RG-03-2 证据采集口径**：check_items 要求「集成测试捕获 eprintln 断言」。Rust 标准库无跨平台 stderr 捕获机制（libtest 内部捕获 unstable；gag 等仅 unix），且本机为 Windows 无法跑 unix mod tests。替代方案：把日志内容抽成**纯函数**（`format_startup_recover_ws_line` / `format_startup_recover_summary_line`），生产路径 `eprintln!("{}", format_...(...))` 与测试断言**同一字符串来源**；状态判定同样抽成纯函数 `classify_startup_recover` 并以 7 组断言覆盖。偏差实质：断言的是「将被打出的日志内容」而非「stderr fd 实际字节」。若评审要求 fd 级捕获，需 unix 专用测试（gag dev-dependency）或集成 harness，建议另行派单。
2. **reused 判定口径**：`cache.get(ws_id).is_some()`（SnapshotCache 存在该 ws 条目）而非 `current_store().is_some()`（条目内已有已加载 store）。启动场景 cache 刚构造为空，两种口径生产行为一致；测试注入场景用前者可轻量构造。已在 classify 注释注明。
3. **built 正向路径无端到端集成测试**：有 pending 且完整构建（built）需真实可解析源文件 + codegraph db + publisher 全链路，构造成本超出本单回归锁范围；built 的判定逻辑由 classify 单测覆盖，构建链路本身由 workspace.rs 既有 R16 测试（test_recover_applies_pending_entries / test_recover_success_path_e2e 等）覆盖。
4. **测试放置位置**：平台无关 `mod startup_recover_tests`（文件顶部公共区）而非 unix mod tests 内——通过 `#[cfg(unix)]/#[cfg(windows)]` 切换引用两份逐字一致的生产副本，使 Windows 本机与 Linux CI 共用同一测试集。副作用：两份生产副本的 `recover_all_workspaces_with_snapshot` 可见性从私有改为 pub(crate)。

## 三元组

- evidence-path：`evidence/T-1795832952572-3e03e5f4/t6-evidence.md`
- evidence-hash：见 report 记录（sha256 于 report 时计算）
