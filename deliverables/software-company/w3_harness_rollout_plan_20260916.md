# §W3 承接卡 step0 —— live-daemon 族实际残留盘点与迁移方案（据实收窄，不改代码）

- 卡：`T-1789527301374-6a465114`（父 `T-1788871227327-45c94bd8`）
- 基线：HEAD `b4c5756`；干净单写环境实测（2026-09-16，daemon 8535/schema60，正确 §6.2 环境，已按父 PID 清理残留隔离 daemon）
- 证据源：[pyt_w3_rescan_and_ops_20260916.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_w3_rescan_and_ops_20260916.md)
- 本 step0 不改任何代码；仅锁定迁移范围与逐文件改法，供 step1–3 执行。

## 0. 范围修正（不照抄交接「约 60」）

交接 §W3 称「约 60 需 live daemon」。实测：18 个已用 `_w3_harness` 者 **17/18 通过**；32 个 cargo-build 候选中 **23 直接 rc=0 通过**。**真残留 = 9**（下表），且多为环境/陈旧断言而非代码缺陷。本卡据此收窄，不空跑 60。

## 1. 真残留清单与四类归因（逐条，无留空）

| 文件 | 用例 | 失败签名（实测） | 归因 | 目标改法（tests-only） |
|---|---|---|---|---|
| `test_http_capability_registry.py` | 4 setup | `隔离 daemon 未发布 manifest` | 需 harness 迁移 | 夹具改 `setup_w3_client`（register+seed+`snapshot.publish`） |
| `test_http_native_read_cutover.py` | 2 setup | `隔离 daemon 未发布 manifest`（`TestRealDaemonRpcNameAlignment`） | 需 harness 迁移 | 同上，仅该类夹具 |
| `test_rust_cli_diff.py` | 全 | >90s **挂起**（夹具 `cargo build` cw.exe） | 需 harness 迁移 | 用 `find_daemon_binary` 复用预建，禁测试内 cargo build |
| `test_windows_bridge_e2e.py` | 部分 | `subprocess.communicate→reader join` 挂起 | 环境前置（Windows spawn 挂） | 加超时/改走 harness 客户端，避免裸 spawn 阻塞 |
| `test_l9_rust_multilang.py` | 2 | `ModuleNotFoundError: tree_sitter_elixir / tree_sitter_hcl` | 环境前置（可选 grammar 包未装） | `pytest.importorskip` 守门，判纯依赖缺失则跳过，**不得伪绿** |
| `test_windows_daemon_e2e.py` | 6 | `E_TASK_WORKSPACE_UNBOUND: workspace_id 无法解析为整数: ws-shared` | 陈旧断言 | 对齐现行契约：显式整数 workspace_id + `seed_cli_task_authority`/`task_workspace_bindings`，不改 daemon |
| `test_windows_wsl_authority_e2e.py` | 3 | `E_TASK_WORKSPACE_UNBOUND: 缺少显式 workspace_id(>0)` | 陈旧断言 | 同上：测试端补显式权威绑定 |
| `test_lease_gate_empirical.py` | 8 | lease/gate 语义断言 FAILED（过期租约/错持有者/request_id 重放/单活跃 reviewer/子任务门禁/业务错误不被伪装成连接失败/fail-closed 不回退） | **真缺陷候选** | step1 前逐条核实：是断言陈旧还是 daemon 行为回归；若实现缺陷且落 `rust_ext`→**另立产品卡**（不在本 tests-only 卡） |
| `test_task_prompt_e2e.py` | 2 | `test_cli_live_parity_positive` 抛 `TypeError`；`missing_task_fails_closed` 失败 | **真缺陷候选** | 同上：先定位 TypeError 根因（测试面 or 实现面），tests-only 可修则修，越界另立卡 |

## 2. 迁移优先级（step1→3）

1. **step1（harness 迁移，最干净）**：`test_http_capability_registry`、`test_http_native_read_cutover`（manifest 未发布 → `setup_w3_client`）；`test_rust_cli_diff`（去 cargo → `find_daemon_binary`）。
2. **step1 环境类**：`test_windows_bridge_e2e`（spawn 挂，加界/走 harness）；`test_l9_rust_multilang`（importorskip，非伪绿）。
3. **step2 陈旧断言**：`test_windows_daemon_e2e`、`test_windows_wsl_authority_e2e`（补显式 workspace 权威，不放宽 daemon）。
4. **step3 真缺陷候选**：`test_lease_gate_empirical`、`test_task_prompt_e2e` 逐条定性；越界到 `rust_ext/src/**` 的一律**另立产品代码卡**（本卡 forbidden 含 `rust_ext/src/`），tests-only 不可修即 BLOCKED 交回。

## 3. 扫描纪律（写进每个执行步，防假失败/防并发事故）

- **单写串行**：一次只跑一个 pytest 扫描；发现并行 agent（`cargo test`/多 `cw-daemon`）即暂缓（规则 34/48）。
- 环境：`PYTHONPATH=C:/git_work`、`CW_TEST_MODE=1`、`USERPROFILE/HOME=Temp/cw_ci_home` 且**预建 `.callwarden`**、**pin `RUSTUP_HOME/CARGO_HOME`**。
- **每轮结束按父 PID 精确清理本轮拉起的隔离 daemon + 其 compat_worker**（否则下一批「1s 退出、不写 xml」假失败）。
- 整树 `pytest tests` 单进程**不可用**（任一慢/挂测试 → thread-timeout SystemExit → junitxml 不落盘）；一律**逐文件 + 外层硬 SIGKILL**，以 rc + FAILED/ERROR 签名为准。
- 解释器 `.venv_test/Scripts/python.exe`；GBK 控制台勿 print 捕获文本，结果写 UTF-8 文件再读。

## 4. 边界声明

- 本卡 allowed：`tests/**`、`deliverables/software-company/**`、`docs/evidence/**`、`cw_task_commit_ledger.json`。
- forbidden：`rust_ext/src/**`、`db/**`、`scripts/refresh_shared_runtime.ps1`、直写 SQLite、apply/close/supersede、伪造状态。
- **C-02(§W9) 不在本卡**：其根因在 `rust_ext/src/daemon/**`（`defect_learn` 只读连接 fail-closed 桩 + 缺 INSERT 移植），须独立产品代码卡并走部署门禁；见 [c02_defect_learn_daemon_fix_plan_20260915.md](file:///c:/git_work/callwarden/deliverables/software-company/c02_defect_learn_daemon_fix_plan_20260915.md)。

---

## 5. step3 执行期新发现（2026-09-16，推翻 §1 初判，须下一任复核）

执行到 step3（`test_windows_daemon_e2e` / `test_windows_wsl_authority_e2e`）时，实证 §1 对它们的归因（「陈旧断言：字符串 ws-id」）**不成立或被污染**：

1. **run B 的 `E_TASK_WORKSPACE_UNBOUND: ws-shared` 疑为串扰**：本会话一直保有共享 daemon（`--http-bind 127.0.0.1:8535`，同时占用默认命名管道 `\\.\pipe\callwarden-S-<SID>`）。这些 E2E 用固定默认管道名 spawn 自己的 daemon；当默认管道已被本会话 daemon 占时，E2E 客户端很可能连到了**在场的共享 daemon**而非自起的隔离 daemon → 观察到的是共享 daemon 对字符串 ws-id 的拒绝，**不是被测 daemon 的真实行为**。
2. **停掉共享 daemon 腾出默认管道后**，`test_windows_daemon_e2e` 不再假 skip，而是 `_wait_daemon(...) is False`（第 329 行等，"daemon 未在超时内响应"）——日志显示被测 daemon 已在 HTTP 侧 pre-bind 并 schema healthy，但**默认命名管道在 `_wait_daemon` 的 40s 窗口内未应答**。这既可能是：
   - (a) `_wait_daemon timeout=40s` 偏短（daemon 绑管道晚于 HTTP）→ tests-only 可修（提高超时）；也可能
   - (b) daemon 默认命名管道 transport 真实回归 → **产品侧**，越出本卡 tests-only 边界。
   二者**在「共享 daemon 在场」的受污染环境下无法干净区分**（本卡治理环需保留共享 daemon，与 E2E 抢同一默认管道）。
3. 另：`test_windows_daemon_e2e` 的 `ensure_fresh_binaries`（module autouse）会跑 `cargo build --bin cw-daemon --bin cw-client`（共享 debug target，增量），与并行 agent 编译争用时会拖慢/挂。

**给下一任的处置建议**：
- 在**完全无其他 daemon、无并行 agent** 的干净单写机上重跑这两个文件，先区分 (a) 超时偏短 vs (b) transport 真挂：若把 `_wait_daemon` 超时提到 ~90s 即绿 → tests-only，本卡内修；若仍不响应 → 登记为 **daemon 命名管道 transport 缺陷产品卡**（rust_ext），从本卡 step3 移除。
- 字符串 ws-id（`ws-shared`/`ws-cli`/`ws-v46`/`ws-proc`/`ws-p*`）问题在 daemon 真正应答后才会暴露为 `E_TASK_WORKSPACE_UNBOUND`，届时按 §1 step3 计划改（register→整数 / seed binding）。**不要在污染环境下改**（会把串扰当缺陷）。

## 6. step4 未决（本轮未展开）

`test_lease_gate_empirical`（8 断言）、`test_task_prompt_e2e`（TypeError）为真缺陷候选，需同样在干净单写环境复现定根因（实现缺陷→可能 rust_ext 另立产品卡；陈旧断言→本卡改）。
