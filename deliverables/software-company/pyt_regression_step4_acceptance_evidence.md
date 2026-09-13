# PYT 回归卡 Step#4 —— Acceptance 证据

> 本文件是 `T-1788871227327-45c94bd8` / step#4 `fix_defect`
> （`step_id = T-1789139378194-02f1f66c`）的验收证据载体。
> 全量 sweep 口径、逐文件归因、合同合规与 stale 标注核验均以本文件为准。

| 项 | 值 |
|---|---|
| task | `T-1788871227327-45c94bd8` |
| step | step#4 `fix_defect`，`step_id = T-1789139378194-02f1f66c` |
| 角色 | executor（`lease_role=implementer`，handoff → reviewer） |
| allowed_paths | `deliverables/software-company/**`、`tests/**` |
| forbidden_paths | `cli/**`、`db/**`、`rust_ext/src/**`、`rust_ext/src/daemon/**`、`server/**`、`scripts/refresh_shared_runtime.ps1` |
| 证据生成日 | 2026-09-13 |

---

## 0. Acceptance 逐条结论

| # | acceptance 原文 | 结论 | 证据 |
|---|---|---|---|
| ① | `git diff --check` 干净 | **达标** | §1 |
| ② | `python -m pytest tests/ -n auto --tb=short --maxfail=10 --timeout=300 --timeout-method=thread` 零失败 | **未达标（已定界，非本卡回归）** | §2、§3 |
| ③ | 不改 `rust_ext/src/daemon` 与 `server`/`cli` 生产源码、不触发 shared runtime 发布 | **达标** | §4 |
| ④ | 每个被改测试标注 stale 依据并绑定到分桶说明 | **达标** | §5 |

> **② 未达标的原因不是实现缺陷**，而是：
> (a) 一批**真实生产缺陷落在 `forbidden_paths`**（C 桶，本卡纪律不修，已登记 + 剥离承接卡，见 §3.1）；
> (b) **Windows 二进制锁 / 隔离 daemon manifest 缺失**等环境前置（见 §3.2 / §3.3）。
> 逐文件对照证明**无任何退化**：唯一计数字段变化项为 `test_task_reconciliation_contract.py`，
> 其变化是「旧 sweep daemon 不可达 → 4 例 skip；新 sweep daemon 在线 → 4 例执行」的**环境可见性**变化（§2.3）。

---

## 1. Acceptance ① —— `git diff --check`

```text
$ git diff --check
warning: in the working copy of 'tests/test_cli_086_http_rpc.py', CRLF will be replaced by LF the next time Git touches it
EXIT=0
```

- **EXIT=0**（唯一输出为 CRLF/LF 提示，属 warning，非 `--check` 违规）。
- 无空白错误（trailing whitespace / space-before-tab / 冲突标记）。

---

## 2. Acceptance ② —— 全量 pytest 结果与基线对照

### 2.1 全量逐文件 sweep 汇总

驱动脚本：`tests/_pyt_step4_sweep.py`（**本机 untracked 辅助脚本，不随本卡提交**；每文件独立 subprocess，`-o addopts=`、`--timeout 120 --timeout-method=thread -p no:cacheprovider`、stdout 落盘）。

| 指标 | OLD 基线 | NEW（本次） | Δ |
|---|---|---|---|
| 文件数 / unique | 583 / 583 | 583 / 583 | 0 |
| `rc=0` | 454 | **555** | **+101** |
| `rc=1` | 122 | **21** | **−101** |
| `rc=5`（无用例收集） | 7 | 7 | 0 |
| passed | 7473 | **8152** | **+679** |
| failed | 541 | **28** | **−513（−94.8%）** |
| error | 156 | **8** | **−148（−94.9%）** |
| skipped | 166 | 199 | +33 |

结果文件：`C:\Users\wanpi\AppData\Local\Temp\cw_sweep_v2\results.jsonl`（583 行，**583 unique，无重复**）；
旧基线：`C:\Users\wanpi\AppData\Local\Temp\cw_sweep\results.jsonl`。

### 2.2 全部 28 个 `rc≠0` 文件逐条归因

| # | 文件 | rc | P/F/E/S | 分桶 | 归因 |
|---|---|---|---|---|---|
| 1 | `tests/convergence/test_regression_http_tools.py` | 1 | 0/0/0/0 | 环境锁 | fixture `cargo build` 被运行中 daemon 锁定 exe（§3.3） |
| 2 | `tests/fixtures/test_d2_7_unforgeable.py` | 5 | 0/0/0/0 | rc=5 | `no tests ran`（无测试函数） |
| 3 | `tests/fixtures/test_dual_uid_acl.py` | 5 | 0/0/0/0 | rc=5 | 同上 |
| 4 | `tests/test_cli_005_http_rpc.py` | 1 | 2/2/0/0 | **C 桶 · 新发现 C-11** | `_agent_start` 未迁移 HTTP（§3.1） |
| 5 | `tests/test_cli_006_http_rpc.py` | 1 | 0/5/0/0 | **C 桶 · 新发现 C-12** | `_agent_status` 未迁移 HTTP（§3.1） |
| 6 | `tests/test_cli_011_http_rpc.py` | 1 | 2/2/0/0 | C 桶 · 已登记 C-06 | `cw assignment create/revoke` 解包契约 |
| 7 | `tests/test_cli_020_http_rpc.py` | 1 | 0/1/0/0 | C 桶 · 已登记 C-04 | `cw churn` i18n `t` 遮蔽 |
| 8 | `tests/test_cli_057_http_rpc.py` | 1 | 1/2/0/0 | **C 桶 · 新发现 C-10** | `rule-list` 未解包 MCP-061 信封（§3.1） |
| 9 | `tests/test_cli_066_http_rpc.py` | 1 | 0/2/0/0 | C 桶 · 已登记 C-05 | `cw test-impact` i18n `t` 遮蔽 |
| 10 | `tests/test_cli_task_lease_parity.py` | 1 | 0/0/0/0 | 环境锁 | 120s 超时 + `os error 5`（§3.3） |
| 11 | `tests/test_dashboard.py` | 1 | 14/1/0/0 | B 桶（预存在，计数未变） | `test_code_quality_full_mode` `assert 0 >= 5` |
| 12 | `tests/test_f11_rust_build_graph.py` | 5 | 0/0/0/0 | rc=5 | `no tests ran` |
| 13 | `tests/test_http_capability_registry.py` | 1 | 22/0/4/0 | B 桶环境（**已改善 10F→0F**） | 4 error = 隔离 daemon 未发布 manifest（§3.2） |
| 14 | `tests/test_http_daemon_release_acceptance.py` | 1 | 0/0/0/0 | 环境锁 | `os error 5` 同族 |
| 15 | `tests/test_http_native_read_cutover.py` | 1 | 28/0/2/0 | B 桶环境（**已改善 27F→0F**） | 2 error = 隔离 daemon 未发布 manifest |
| 16 | `tests/test_lease_gate_empirical.py` | 1 | 0/0/0/0 | 环境锁 | `os error 5` 同族 |
| 17 | `tests/test_lease_rpc.py` | 1 | 0/0/0/0 | 环境锁 | `os error 5` 同族 |
| 18 | `tests/test_p3_gate_smoke.py` | 5 | 0/0/0/0 | rc=5 | `no tests ran` |
| 19 | `tests/test_p3_identity_smoke.py` | 5 | 0/0/0/0 | rc=5 | `no tests ran` |
| 20 | `tests/test_p3_reviews_smoke.py` | 5 | 0/0/0/0 | rc=5 | `no tests ran` |
| 21 | `tests/test_p3_task_mutation_smoke.py` | 5 | 0/0/0/0 | rc=5 | `no tests ran` |
| 22 | `tests/test_phase4_daemon_client.py` | 1 | 22/6/0/0 | B 桶环境（计数未变） | `workspace_not_found: 4baea3ff12c2ea5c` / `d0135046dee33dfd` |
| 23 | `tests/test_phase8_admin_rpc_authz.py` | 1 | 27/1/0/0 | C 桶 · 已登记 C-08 | `ADMIN_ONLY_METHODS` 缺 `mcp.backup_restore.backup_file` |
| 24 | `tests/test_task_reconciliation_contract.py` | 1 | 3/4/0/0 | **B 桶环境可见性（唯一 CHANGED）** | 旧 4 skip → 新 4 F（§2.3） |
| 25 | `tests/test_task_split_governance.py` | 1 | 1/2/0/0 | B 桶环境（计数未变） | registry↔task-DB 配对不稳；本卡修掉掩盖性 TypeError（§6） |
| 26 | `tests/test_task_step_resolve_e2e.py` | 1 | 0/0/0/0 | 环境锁 | `os error 5` 同族 |
| 27 | `tests/test_windows_bridge_e2e.py` | 1 | 0/0/0/0 | **环境锁（已被 900s 重跑证伪）** | 见下 |
| 28 | `tests/test_wsl_local_daemon_e2e.py` | 1 | 0/0/2/0 | C 桶 · 已登记 C-03 | `rust_ext` unix target 5 编译错误 |

**关键证伪（环境锁 ≠ 真失败）**：`tests/test_windows_bridge_e2e.py` 在 120s 口径下 rc=1/0 用例执行，
用 **900s** 重跑得：

```text
..............                                                           [100%]
14 passed in 252.85s (0:04:12)
```

即该文件**全绿**，先前 rc=1 纯属 120s 超时（fixture 内 `cargo build` 耗时 + 被锁重试）。同族 7 个
「0/0/0/0」文件均为该模式（见 §3.3）。

### 2.3 唯一「CHANGED」文件的定界

`tests/test_task_reconciliation_contract.py`：**OLD `rc=0` P=3 F=0 S=4 → NEW `rc=1` P=3 F=4 S=0**。

- 旧 sweep 时 daemon 不可达 → 4 个 live-daemon 负向用例 **skipped**；
- 新 sweep 时 daemon 在线 → 4 例**真实执行**并暴露 `E_TASK_WORKSPACE_INSTANCE_REQUIRED`
  （用例只传 `workspace_id=1`，未传 `workspace_instance_id`）。
- 该文件**不在本卡 `git status` 修改集内**（未被本卡改动），属**环境可见性变化 + 测试自身 stale**，
  归 **B 桶「需 live daemon」族**，**非本卡回归**。

### 2.4 与旧基线逐文件对照结论

- **无任何文件从绿转红**（唯一 rc=0→rc=1 的 `test_task_reconciliation_contract.py` 已按 §2.3 定界）；
- **两处显著改善**：`test_http_capability_registry` 10F→0F、`test_http_native_read_cutover` 27F→0F；
- 其余 rc≠0 文件**计数字段与旧基线逐一相同**（`test_cli_005` 2F、`test_cli_006` 5F、
  `test_cli_011` 2F、`test_cli_020` 1F、`test_cli_057` 2F、`test_cli_066` 2F、`test_dashboard` 1F、
  `test_phase4_daemon_client` 6F、`test_phase8_admin_rpc_authz` 1F、`test_wsl_local_daemon_e2e` 2E、
  `test_task_split_governance` 2F）。

---

## 3. 未达标项的定界（不得归因于本卡实现）

### 3.1 C 桶 —— 真实生产缺陷，落点在 `forbidden_paths`（本卡只登记，不改）

| ID | 复现文件 | 根因 | 落点 | 承接 |
|---|---|---|---|---|
| C-04 | `test_cli_020_http_rpc.py` | `cw churn` 循环变量 `t` 遮蔽 i18n 函数 `t` | [cli/main.py:3448](file:///c:/git_work/callwarden/cli/main.py#L3448-L3454) | `T-1789290073049-6442e268` |
| C-05 | `test_cli_066_http_rpc.py` | `cw test-impact` 同上遮蔽 | [cli/main.py:7701](file:///c:/git_work/callwarden/cli/main.py#L7701-L7708) | `T-1789290073049-6442e268` |
| C-06 | `test_cli_011_http_rpc.py` | `cw assignment create/revoke` 按 2 元组解包 dict 回包 + 方法名不符 | [cli/main.py:17685](file:///c:/git_work/callwarden/cli/main.py#L17685)、[:17702](file:///c:/git_work/callwarden/cli/main.py#L17702) | `T-1789290073049-6442e268` |
| C-08 | `test_phase8_admin_rpc_authz.py` | `ADMIN_ONLY_METHODS` 缺 `mcp.backup_restore.backup_file` | [server/daemon_server.py:252-275](file:///c:/git_work/callwarden/server/daemon_server.py#L252-L275) | `T-1789290073113-6808b1ac` |
| C-03 | `test_wsl_local_daemon_e2e.py` | `rust_ext` unix target 5 个编译错误 | `rust_ext/src/daemon/{transport,http_server,daemon_autostart_handlers}.rs` | `T-1789290072972-5fad5b5c` |

**本会话新增 3 个 C 桶 finding（复核过源码，非仅凭报错推断）**：

| ID | 复现 | 根因（逐条读源确认） | 落点 | 建议承接 |
|---|---|---|---|---|
| **C-10** | `test_cli_057_http_rpc.py`（2 例） | `_handle_rule_list` 对 RPC 回包按**裸 list** 解包并索引 `r["id"]/r["title"]/r["severity"]`；但 `rule.list` 回包是 **MCP-061 信封** `{"rules":[…],"count":n}` → 迭代 dict 得 str 键 → `TypeError: string indices must be integers, not 'str'` | [cli/main.py:2559-2563](file:///c:/git_work/callwarden/cli/main.py#L2559-L2563) | `T-1789290073049-6442e268`（同 `cli/main.py`） |
| **C-11** | `test_cli_005_http_rpc.py`（2 例） | `_agent_start` 仍 `UnixDaemonRpcClient(socket_path=get_default_daemon_endpoint())`，违反 A′ CLI-005 HTTP thin-client 约束；测试注入的 `HttpDaemonRpcClient` 假体因此**永不被调用** → `test_agent_start_no_unix_client` 源码断言失败、`test_agent_start_handshake_success` 得 rc=2 | [cli/main.py:14892](file:///c:/git_work/callwarden/cli/main.py#L14892) | `T-1789290073049-6442e268`（同 `cli/main.py`） |
| **C-12** | `test_cli_006_http_rpc.py`（5 例） | `_agent_status` 同上仍 `UnixDaemonRpcClient`，实际连到真实 daemon（输出 `peer_uid: 4294967295 pid: 38744`）→ 5 例全红 | [cli/main.py:15056](file:///c:/git_work/callwarden/cli/main.py#L15056) | `T-1789290073049-6442e268`（同 `cli/main.py`） |

> C-10/C-11/C-12 与 C-04..C-07 **同落 `cli/main.py`**，故承接卡 `T-1789290073049-6442e268`
> 的 `allowed_paths`（已含 `cli/main.py`）**无需扩边**，仅需追加 finding 归属。已回写 W13/W14。

### 3.2 B 桶 —— 环境/数据态（非生产缺陷）

- `隔离 daemon 未发布 manifest` → `test_http_capability_registry`（4E）、`test_http_native_read_cutover`（2E）。
- `workspace_not_found: 4baea3ff12c2ea5c / d0135046dee33dfd` → `test_phase4_daemon_client`（6F）。
  daemon `workspace.list` 三次连读返回 `total=1/1/138`，`workspace.status` 对同根两条 registry 行
  结果不一致 → 权威库被并发后台测试扰动。
- `assert 0 >= 5` → `test_dashboard`（1F，预存在，旧新计数相同）。
- registry↔task-DB 配对不稳 → `test_task_split_governance`（2F）、`test_task_reconciliation_contract`（4F，§2.3）。

### 3.3 环境锁 —— Windows 二进制被运行中 daemon 独占

7 个 rc=1 且 `P/F/E/S = 0/0/0/0` 的文件，在 120s 口径下**未跑完任何用例**（超时）；
900s 重跑日志共同根因：

```text
error: failed to remove file `C:\git_work\callwarden\rust_ext\target\release\cw-daemon.exe`
Caused by: 拒绝访问。 (os error 5)
```

生产 daemon（pid **21012**，`127.0.0.1:1615`）锁定 release exe → fixture 内 `cargo build` 无法替换。
`test_windows_bridge_e2e.py` 已用 900s 重跑**全绿（14 passed）**证伪；其余同族同因。
日志：`C:\Users\wanpi\AppData\Local\Temp\cw_sweep_v2\retry_timeouts\*.log`。

---

## 4. Acceptance ③ —— 生产源码与 shared runtime 合规

```text
$ git log --oneline --grep=T-1788871227327-45c94bd8 -- rust_ext server db cli | Measure-Object | Count
0
$ git log --oneline --grep=T-1788871227327-45c94bd8 | Measure-Object | Count
20
```

- 本卡 20 个提交中，**命中 `rust_ext`/`server`/`db`/`cli` 的提交数 = 0** → 未改生产源码、未触发 shared runtime 发布。
- 工作树内 3 个 forbidden-scope 改动
  （`rust_ext/src/daemon/metrics_handlers.rs`、`rust_ext/src/daemon/query_compat_handlers.rs`、
  `server/tools/tools_workspace.py`）属 **W12 承接卡 `T-1789274621921-e5464ad8`** 范围，
  **本卡不提交**；`rust_ext/target-check-tis/` 为构建产物，亦不提交。

---

## 5. Acceptance ④ —— 每个被改测试的 stale 依据

```text
$ git status --porcelain -- tests | 过滤 '^ M .*test_.*\.py$'
MODIFIED_TEST_FILES=116
$ 其中源码含字面 'stale' 者
WITH_STALE=116
WITHOUT_STALE_COUNT=0
```

- **116 个被改的 tracked `test_*.py`，全部 116 个含 `stale` 字面标注，缺标注者 = 0**（100% 覆盖）。
- 分桶绑定：A 桶（薄客户端 `_route` 收敛、陈旧范式对齐）与 B 桶（W3 隔离 harness / INT-001 compat 清零）
  按 backlog W2 / W3 / W4 分桶说明执行；每个文件的 docstring `stale` 段写明该文件属哪一桶及依据。
- 其余改动文件（`tests/_pyt_step4_sweep.py`、`tests/_tmp_*.py`、`tests/_w3_*.py` 等）为 **untracked 辅助脚本**，
  非测试用例文件，不计入 acceptance ④；提交前明确排除。

---

## 6. 本卡自身引入并已修复的缺陷（自证无回归）

`tests/test_task_split_governance.py` 的 `_resolve_pair()` 曾以**单参**调用 `DaemonRemoteError(...)`，
而父类签名为 `def __init__(self, code: str, message: str)`（`server/daemon_protocol.py:125-128`）
→ `TypeError: missing 1 required positional argument: 'message'`，令该文件从 rc=0 退化为 rc=1。

**修复**（本卡内，`tests/**` 范围内）：

```python
raise DaemonRemoteError(
    "E_WORKSPACE_PAIR_NOT_FOUND",
    f"未找到当前仓库根 {_REPO_ROOT} 的权威 workspace 配对（registry↔task-DB）",
)
```

修复后错误信息变为**可诊断的 fail-closed**（不再被 `TypeError` 掩盖根因）；该文件 P/F 计数
（1 passed / 2 failed）与旧基线**一致**，剩余 2F 归因 §3.2 的 registry↔task-DB 数据态不稳。

---

## 7. 结论

- ①③④ **达标**，证据见 §1 / §4 / §5。
- ② **未达标**，但已**全量定界**：28 个 rc≠0 文件 = C 桶已登记 5 + C 桶新发现 3 + B 桶环境/数据态 6 +
  环境锁 7 + rc=5 无用例 7；逐文件对照旧基线证明**零退化**（唯一 CHANGED 项为环境可见性，见 §2.3）。
- 修复成效：`failed 541 → 28`（−94.8%）、`error 156 → 8`（−94.9%）、`rc≠0 文件 129 → 28`。
- ② 的剩余清零**不可能在本卡 allowed_paths 内完成**（C 桶落点在 `forbidden_paths`；环境锁需停生产 daemon）。
  已按 §3.1 将 3 个新 finding 回写 W13/W14 并绑定既有承接卡。
