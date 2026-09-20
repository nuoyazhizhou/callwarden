# PYT 回归卡 Step#6 —— remediation 实施与定向复测证据

> 本文件是 `T-1788871227327-45c94bd8` / step#6 `fix_defect`
> （`step_id = T-1789293736662-649f0c98`，`remediation_of_step_id = T-1789293259780-5c3d5fd8`）
> 的验收证据载体。定向复测口径、逐文件归因、合同合规与 stale 标注核验均以本文件为准。

| 项 | 值 |
|---|---|
| task | `T-1788871227327-45c94bd8` |
| step | step#6 `fix_defect`，`step_id = T-1789293736662-649f0c98` |
| 角色 | executor（`lease_role=implementer`，handoff → reviewer） |
| allowed_paths | `deliverables/software-company/**`、`tests/**` |
| forbidden_paths | `cli/**`、`db/**`、`rust_ext/src/**`、`rust_ext/src/daemon/**`、`server/**`、`scripts/refresh_shared_runtime.ps1` |
| 证据生成日 | 2026-09-20 |

---

## 0. 结论

step#6 是对 step#5（parked，`owner_route=planner`）的 remediation 派工。与 step#5「不实施代码」不同，
本 step **在 `allowed_paths` 内实施了真实修复**，并把 acceptance ② 的残余失败从 **21 个 rc=1 文件压到 8 个**。

**核心结论：`allowed_paths`（`tests/**`）内可修的 stale 期望已全部修完；剩余 8 个 rc=1 文件无一可通过改测试修复**
（改了就是造假绿）：1 个落 `forbidden_paths` 的真实生产缺陷（已登记 C-13 承接卡）、
1 个需 `wsl.exe`/隔离 daemon 子进程（沙箱安全策略阻断）、1 个因运行中 daemon 二进制过期
（修复已提交但需重建重启）、5 个需起隔离 daemon 或替换被锁 exe（沙箱/运行中 daemon 阻断）。

---

## 1. 本 step 在 `allowed_paths` 内的实施（3 个 stale 测试修复）

| 文件 | 修复内容 | stale 依据 | 复测 |
|---|---|---|---|
| `tests/convergence/test_regression_http_tools.py` | `test_get_impact_compat_matrix_and_dispatch` 断言 `target_backend == "python_compat"` → `"rust_native"` | 权威迁移矩阵 `tool_migration_matrix.json` 该项已是 `rust_native`；旧断言与权威矩阵自相矛盾（A 桶·矩阵漂移） | **7 passed** rc=0 |
| `tests/test_task_reconciliation_contract.py` | `_make_neg_task()` 的 `task.create` 补 `workspace_instance_id`（从 `workspace.status` 动态取 `task_db_instance_id`） | daemon 已强制 `E_TASK_WORKSPACE_INSTANCE_REQUIRED`（step#4 §2.3 定界为 stale 期望，非本卡回归） | **3 passed + 4 skipped** rc=0 |
| `tests/test_http_native_read_cutover.py` | 4 处 `patch("...get_db")` 替换为源码级断言（`inspect.getsource` + regex） | `get_db` 已从 `tools_query` 移除（0 引用）；`tools_workspace` 仅 `_mcp_common` 死导入、工具体内无调用点——patch 假体已无真实拦截对象（A 桶·cutover 残留） | **30 passed** rc=0 |

新增复现脚本：`deliverables/software-company/pyt_step6_c13_repro.py`（C-13 必现复现，见 §3.1）。

---

## 2. Acceptance ② 定向复测：21 → 8

驱动脚本：`.workbuddy/scripts/pyt_step6_sweep.py`（会话内辅助脚本，不随本卡提交）。
口径与 step#4 一致：每文件独立 subprocess、`-o addopts=`、`--timeout 600`、`-p no:cacheprovider`、
venv python、`PYTHONPATH=C:/git_work`、`CW_TEST_MODE=1`、禁代理。
结果：`C:\Users\wanpi\AppData\Local\Temp\cw_step6\results.jsonl`（28 行）。

### 2.1 已转绿（13 个，step#4 rc=1 → 本 step rc=0）

| # | 文件 | step#4 | 本 step | 归因 |
|---|---|---|---|---|
| 1 | `tests/test_cli_005_http_rpc.py` | 1（C-11） | **rc=0 4P** | 承接卡 `T-1789290073049-6442e268` 已 closed |
| 2 | `tests/test_cli_006_http_rpc.py` | 1（C-12） | **rc=0 5P** | 同上 |
| 3 | `tests/test_cli_011_http_rpc.py` | 1（C-06） | **rc=0 4P** | 同上 |
| 4 | `tests/test_cli_020_http_rpc.py` | 1（C-04） | **rc=0 1P** | 同上 |
| 5 | `tests/test_cli_057_http_rpc.py` | 1（C-10） | **rc=0 3P** | 同上 |
| 6 | `tests/test_cli_066_http_rpc.py` | 1（C-05） | **rc=0 2P** | 同上 |
| 7 | `tests/test_phase8_admin_rpc_authz.py` | 1（C-08） | **rc=0 33P** | 承接卡 `T-1789290073113-6808b1ac` 已 closed |
| 8 | `tests/test_http_capability_registry.py` | 1（4E） | **rc=0 26P** | daemon 在线，manifest 可见 |
| 9 | `tests/test_http_native_read_cutover.py` | 1（2E） | **rc=0 30P** | **本 step 修复**（§1） |
| 10 | `tests/test_phase4_daemon_client.py` | 1（6F） | **rc=0 22P+6S** | daemon 在线，workspace 可见 |
| 11 | `tests/test_task_reconciliation_contract.py` | 1（4F） | **rc=0 3P+4S** | **本 step 修复**（§1） |
| 12 | `tests/convergence/test_regression_http_tools.py` | 1（envlock） | **rc=0 7P** | **本 step 修复**（§1） |
| 13 | `tests/test_windows_bridge_e2e.py` | 1（envlock） | **rc=0 14P** | 900s 口径证伪延续：13.6s 全绿 |

### 2.2 剩余 8 个 rc=1（逐条归因，均非本卡可修）

| # | 文件 | 现象 | 根因 | 可修性 |
|---|---|---|---|---|
| 1 | `tests/test_dashboard.py` | 15 errors（12.58s） | `sqlite3.IntegrityError: FOREIGN KEY constraint failed` —— `db/db_stdlib.py::import_stdlib_symbols_for_lang` 在符号 skipped 后仍 INSERT，父行缺失 | **落 `db/**`（forbidden）**，已登记 **C-13** 承接卡 `T-1789651472`（open） |
| 2 | `tests/test_wsl_local_daemon_e2e.py` | 2 errors（0.42s） | `PermissionError: [WinError 5]` —— fixture 起子进程被沙箱安全策略阻断（`wsl.exe` 在程序黑名单） | 环境锁（C-03 承接卡已 closed，但测试本身需起子进程） |
| 3 | `tests/test_task_split_governance.py` | 6 failed / 4 passed | 「DID NOT RAISE DaemonRemoteError」——空 title/空 description 拒绝的修复已提交于 `6591ae5`（09-19 22:18）+ `ab551bf`（09-20 09:28），落 `rust_ext/src/daemon/task_collab_planning.rs`（**forbidden**）；运行中 daemon exe 构建于 **09-19 00:24**，早于两提交 → 二进制过期 | 需 daemon 重建+重启（本卡 forbidden，且重启会打断其他工作） |
| 4 | `tests/test_cli_task_lease_parity.py` | 12 errors（34.45s） | fixture 起隔离 daemon 被沙箱阻断 | 环境锁 |
| 5 | `tests/test_http_daemon_release_acceptance.py` | Timeout（605.2s） | `isolated_http_daemon` fixture 等 daemon 启动 stdout 超时 | 环境锁 |
| 6 | `tests/test_lease_gate_empirical.py` | 20 errors（28.79s） | 同族：fixture cargo build/起 daemon 被阻断 | 环境锁 |
| 7 | `tests/test_lease_rpc.py` | 10 errors（32.04s） | 同上 | 环境锁 |
| 8 | `tests/test_task_step_resolve_e2e.py` | 14 errors（96.66s） | 同上 | 环境锁 |

**7 个 rc=5 文件**（`test_d2_7_unforgeable`、`test_dual_uid_acl`、`test_f11_rust_build_graph`、
`test_p3_{gate,identity,reviews,task_mutation}_smoke`）维持 rc=5（`no tests ran`，无用例收集），与 step#4 一致。

### 2.3 全量口径推算

| 指标 | step#4 | 本 step（推算） | Δ |
|---|---|---|---|
| `rc=0` | 555 / 583 | **568 / 583** | **+13** |
| `rc=1` | 21 | **8** | **−13** |
| `rc=5` | 7 | 7 | 0 |

> 推算依据：step#4 全量 583 文件结果（`C:\Users\wanpi\AppData\Local\Temp\cw_sweep_v2\results.jsonl`）
> + 本 step 对全部 21 个 rc=1 文件的定向重跑；rc=0 与 rc=5 文件未重跑（本卡未改动它们，无退化风险）。

---

## 3. C-13 新登记：`test_dashboard` 15 errors 根因

### 3.1 根因（源码级 + 必现复现）

`db/db_stdlib.py::import_stdlib_symbols_for_lang` 在 `symbols` 导入被 skip 后仍执行 INSERT，
父行（语言/模块）缺失触发 `FOREIGN KEY constraint failed`。空库 `build_full_graph()` 必现。

复现脚本 `deliverables/software-company/pyt_step6_c13_repro.py`（空库直接调 `build_full_graph()`）：

```text
FAIL: IntegrityError: FOREIGN KEY constraint failed
created=0, skipped=65 → 抛异常，退出码 1（缺陷存在）
```

### 3.2 承接

| 项 | 值 |
|---|---|
| C-13 承接卡 | `T-1789651472`（`status=open`，parent `T-1787203926824-9f873bfc`） |
| 标题 | C-13 承接：db_stdlib stdlib 符号导入 FK 约束失败修复（test_dashboard 15 errors 根因） |
| 落点 | `db/db_stdlib.py`（本卡 `forbidden_paths`） |

---

## 4. Acceptance 逐条结论

| # | acceptance 原文 | 结论 | 证据 |
|---|---|---|---|
| ① | `git diff --check` 干净 | **达标** | §5 |
| ② | `pytest tests/ … 零失败` | **未达标，但 `allowed_paths` 内已到极限**：21→8，剩余 8 个 = 1×forbidden 生产缺陷（C-13 已登记）+ 1×沙箱子进程黑名单 + 1×daemon 二进制过期（修复已提交）+ 5×环境锁 | §2 |
| ③ | 不改 `rust_ext/src/daemon` 与 `server`/`cli` 生产源码、不触发 shared runtime 发布 | **达标** | §5 |
| ④ | 每个被改测试标注 stale 依据并绑定到分桶说明 | **达标**（3 个被改文件均含 stale 依据，见 §1） | §1、§5 |

**② 的剩余清零路径（均在本卡 `allowed_paths` 之外）**：
- C-13（`db/db_stdlib.py`）→ 承接卡 `T-1789651472`；
- 空 title 拒绝（`rust_ext/src/daemon/task_collab_planning.rs`）→ 已由 `6591ae5`/`ab551bf` 提交，
  待 daemon 重建重启（属运维动作，非本卡代码改动）；
- 6 个需起隔离 daemon / `wsl.exe` 的文件 → 需停生产 daemon 或在非沙箱环境运行。

---

## 5. 合规与提交核验

- 本 step 工作树改动（`git status`）：
  - `M tests/convergence/test_regression_http_tools.py`
  - `M tests/test_http_native_read_cutover.py`
  - `M tests/test_task_reconciliation_contract.py`
  - `?? deliverables/software-company/pyt_step6_c13_repro.py`（新增复现脚本）
  - `?? deliverables/software-company/pyt_regression_step6_remediation_evidence.md`（本文件）
- 全部落在 `allowed_paths`（`tests/**` + `deliverables/software-company/**`），**`forbidden_paths` 命中数 = 0**。
- `git diff --check`：EXIT=0（无空白错误/冲突标记）。
- 本卡提交不含 `rust_ext`/`server`/`db`/`cli` 改动（acceptance ③ 达标）。
- 3 个被改测试文件均带 stale 依据说明（acceptance ④ 达标）。
