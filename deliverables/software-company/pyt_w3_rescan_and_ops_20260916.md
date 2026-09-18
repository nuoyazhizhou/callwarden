# §W3 家族实扫结果 + 运维盘点（PYT 残项承接·第二任 agent 交付）

- 生成：2026-09-16（+08:00）；基线 HEAD `b4c5756`；承接自 [pyt_remaining_handoff_20260915.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_remaining_handoff_20260915.md)
- 关联：C-02 修复计划另见 [c02_defect_learn_daemon_fix_plan_20260915.md](file:///c:/git_work/callwarden/deliverables/software-company/c02_defect_learn_daemon_fix_plan_20260915.md)
- 权威口径仍以 [pyt_regression_step4_handoff_backlog.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_handoff_backlog.md) §W1–§W20 为准。

---

## 1. 方法与环境（先讲清，便于复现）

- 拉起部署 daemon：`runtime/current/cw-daemon.exe --http-bind=127.0.0.1:8535`，health=200，schema 60，git_commit=`b4c5756`。
- 测试环境按交接 §6.2：`PYTHONPATH=C:/git_work`、`CW_TEST_MODE=1`、`USERPROFILE/HOME=Temp/cw_ci_home`（预建 `.callwarden`）、**pin `RUSTUP_HOME/CARGO_HOME`**。
- 解释器：`.venv_test/Scripts/python.exe`（含 pytest/pytest-timeout/lxml）。

**关键坑（本轮血泪，务必记入接手认知）**：

1. **单进程全量跑 `pytest tests` 不可行**：任意一个慢/挂测试（夹具内 `cargo build`、或像 `test_i18n_cleanup_c5` 遍历全仓的慢测试、或 Windows 下 `subprocess.communicate` reader 线程 join 卡死）会让 `--timeout-method=thread` 升级成 `SystemExit` 打断整个 session → **junitxml 不落盘**。交接「重跑未收敛」的真因即此，不是配置。
2. **逐文件串行 + 外层硬 SIGKILL** 是唯一可靠仪器：单文件挂只损失它自己。
3. **多 daemon 并发争用会造成秒级假失败**：本轮我第一次全量 run 后，某个 harness 测试拉起的**残留隔离 daemon**（`--socket …Temp…`）与后续测试的 autostart 抢 `.callwarden`/端口 → 一批文件「1s 内退出、不写 xml」。**按父 PID 精确杀掉该残留 daemon + 其 compat_worker 后，重扫即恢复正常**（18 harness 文件 17 通过）。→ 呼应规则 34：daemon 是任务库唯一写入口，并发 autostart 危险。**本仓疑似有并行 agent**（见 §4 运维观察：`cargo test --test pipeline_line_conservation` 非本会话发起），扫描须**单写串行**。
4. `-q` 的 `N passed` 摘要行经 python `subprocess` 捕获时正则不稳 → 本轮**以 rc（0/1）+ 显式 FAILED/ERROR 签名为准**，不引用被吞的用例计数。

---

## 2. §W3 实扫结论（干净单写环境 + 正确 env）

### 2.1 Run A —— 已用 `_w3_harness` 的文件（18 个）

| 结果 | 数 | 说明 |
|---|---|---|
| 通过 | **17** | rc=0 |
| 失败 | 1 | `test_cli_task_claim_recover.py`（交接 §W3 亦点名，属需逐条核实） |

> **路径①得到实证**：推广 `_w3_harness`（复用预建二进制 + 发布 manifest/snapshot）后，「需 live daemon」族基本变绿，无需 Planner 改分层 acceptance。

### 2.2 Run B —— 含夹具内 `cargo build` 的迁移候选（32 个，静态判定「cargo-build 且尚未用 `_w3_harness`」）

rc 分类：**23 通过 / 8 失败 / 1 挂起**。对 8+1 逐条归因（四类，无留空）：

| 文件 | 失败签名（实测） | 归因 |
|---|---|---|
| `test_http_capability_registry.py` | 夹具 setup `Failed: 隔离 daemon 未发布 manifest` ×N | 环境前置/harness 迁移（manifest 未发布） |
| `test_http_native_read_cutover.py` | 同上 `隔离 daemon 未发布 manifest` ×2 | 环境前置/harness 迁移 |
| `test_rust_cli_diff.py` | >90s **HANG**（夹具 `cargo build` cw.exe） | 需 harness 迁移（复用预建二进制，勿夹具内编译） |
| `test_windows_bridge_e2e.py` | `subprocess.communicate → stdout_thread.join` 超时挂起 | 环境前置（Windows 进程 spawn 挂），非代码缺陷 |
| `test_l9_rust_multilang.py` | `ModuleNotFoundError: tree_sitter_elixir / tree_sitter_hcl` | 环境前置（可选 grammar 包未装） |
| `test_windows_daemon_e2e.py` | `E_TASK_WORKSPACE_UNBOUND: workspace_id 无法解析为整数: ws-shared` ×6 | 陈旧断言（工作区权威收紧后命名/绑定预期过期） |
| `test_windows_wsl_authority_e2e.py` | `E_TASK_WORKSPACE_UNBOUND: 缺少显式 workspace_id(>0)；禁止用 active/cwd 补齐` ×3 | 陈旧断言（e2e 未显式带 workspace_id，被 daemon 正确 fail-closed） |
| `test_lease_gate_empirical.py` | 8 条 lease/gate 语义断言 FAILED（过期租约/错持有者/request_id 重放/单活跃 reviewer/子任务门禁/业务错误不被伪装成连接失败/fail-closed 不回退） | **真缺陷候选（需逐条核实）** |
| `test_task_prompt_e2e.py` | `test_cli_live_parity_positive` 抛 `TypeError`；`missing_task_fails_closed` 失败 | **真缺陷候选（需逐条核实）** |

### 2.3 净结论（补交接 §8「未给可信通过率」的窟窿）

- 交接所称「约 60 需 live daemon」族，在**干净单写 + 正确环境**下：绝大多数**要么 harness 一跑即绿、要么是环境前置/陈旧断言导致的假失败**；
- 真正值得开独立卡核实的**只剩 2 个**：`test_lease_gate_empirical.py`、`test_task_prompt_e2e.py`（且是**租约/门禁与 prompt 编译的语义**问题，属**实现域**，不是 W3 的 live-daemon 基建域）；
- harness 推广（路径①）可关闭 §W3 基建类；`test_cli_task_claim_recover` + 上述 2 真缺陷候选另立小卡。

---

## 3. 建卡/推进建议（§W3）

- 走 **路径①**（推广 `tests/_w3_harness.py`）：把 manifest 发布 + 复用预建二进制（不夹具内 `cargo build`）铺到 §2.2 的环境前置/harness 类；陈旧断言类（windows_daemon/wsl）按 daemon 现行工作区权威契约更新测试预期。
- 独立小卡（真缺陷候选）：`test_lease_gate_empirical.py`、`test_task_prompt_e2e.py` —— 先复现、判「实现缺陷 vs 陈旧断言」再路由（AGENTS 双轨）。
- 扫描纪律（写进卡 step0）：**单写串行**、pin `RUSTUP_HOME/CARGO_HOME`、预建隔离 HOME `.callwarden`、**每轮结束按父 PID 清理残留隔离 daemon**（否则下一批假失败）。

---

## 4. 运维盘点（本轮实测）

| 路径 | 体积 | 处置建议 |
|---|---|---|
| `rust_ext/target` | 37.9 GB | 主 cargo 缓存（gitignore）。**未动**，见下建议 |
| `rust_ext/target-nf1` | 2.13 GB | git status 里的未跟踪项，NF1 期构建缓存（gitignore） |
| `rust_ext/target_cwcheck` | 25 MB | 同上 |
| `runtime/versions/*.core-backup` | **0 个** | 交接担心的保留栈当前不存在，无需处理 |

- 合计 ~40 GB 构建缓存，**全在 gitignore**，不影响 `git add`/提交纪律（本轮全程只读，未 `git add .`）。
- **清理建议（不 `rm -rf`，待用户拍板）**：
  1. 先确认**无并行 agent**（现观测到 `cargo test --test pipeline_line_conservation` + rustc 在跑，疑似他人会话）；有则**绝不清**（会打断别人编译、且可能触发规则 48 类并发问题）。
  2. 若确认独占：`target-nf1`、`target_cwcheck` 是过期一次性构建缓存，价值低可优先；主 `target/` 清了会触发下次全量重编（耗时长），非急需不建议清。
  3. 清理须走系统回收站路径（规则：禁 `rm -rf`/递归删）；本环境沙箱无 coreutils，`SendToRecycleBin` 走 powershell。**本轮未执行任何删除**（用户此前选「先盘点再建议」，且现检测到并行活动，判定为不宜动）。
- **daemon**：8535 上共享 daemon 现由本轮拉起并保持运行；**残留隔离 daemon 已按父 PID 精确清理**。若不再需要共享 daemon，可保留（无害，规则 34 单写点）。

---

## 5. 三项状态小结

| 项 | 状态 |
|---|---|
| ① C-02 / §W9 | 已出 **daemon 侧修复计划**（推翻交接「翻 op_class」建议，见另文件）；根因在 `rust_ext/src/daemon/**`，须走 Planner 重规划扩白名单。未改代码。 |
| ② §W3 | 本轮**实扫并归因完成**（§2）：路径①实证、真缺陷仅 2 例、假失败根因锁定并沉淀环境坑（§1）。未改测试代码（推广 harness 属承接卡 step1 实现）。 |
| ③ 运维 | 盘点完成（§4）；**建议暂不清**（检测到并行 agent + 全 gitignore）。 |

> 诚实披露：§2 用例级**精确计数**因 §1.4 正则问题以 rc 分类 + 签名代替；「23 通过」等以 rc=0 为据（未含各文件用例数明细）。逐条真值可由承接卡用修好的 junit 解析器复核。
