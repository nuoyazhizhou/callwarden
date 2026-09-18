# §W3 step5 —— 族级逐文件零失败回归汇总 + executor_ready_for_review 交接

- 卡：`T-1789529126780-6cf84728`（父 `T-1788871227327-45c94bd8`，§W3 承接 rev2）
- 前置基线：HEAD `6b86bd9`（step0 文档 `22580e9` 已落 master）
- 环境：干净单写（全程无其他 daemon / 无并行 agent）；`.venv_test/Scripts/python.exe`；`PYTHONPATH=C:/git_work`；`CW_TEST_MODE=1`；`RUSTUP_HOME/CARGO_HOME` 显式 pin；MSVC 经 `msvc_env_emit.py` 注入（VS2022 14.44.35207 + SDK 10.0.26100.0）；每轮按父 PID 清理残留隔离 daemon（实测清理后无残留）
- 运行方式：逐文件串行（`cw_run_step3.py`，外层 pytest `--timeout=1200 -p no:cacheprovider`），以 rc + 汇总行为准；禁整树单进程

## 1. 族级回归结果（2026-09-16，零失败）

| 文件 | 结果 | 耗时 | 备注 |
|---|---|---|---|
| `tests/test_windows_daemon_e2e.py` | 8 passed / 1 skipped | 474s | skip = 默认管道被占用的保护性跳过（本轮干净环境下该 skip 未触发为失败） |
| `tests/test_windows_wsl_authority_e2e.py` | 4 passed | 88s | 8 源并发 claim 全链路（创建→竞争→report→review） |
| `tests/test_lease_gate_empirical.py` | 20 passed | 22s | release 二进制；13 拒绝场景 + 7 门禁全绿 |
| `tests/test_task_prompt_e2e.py` | 6 passed | 5s | RP-10 三面 parity |
| **合计** | **38 passed / 1 skipped / 0 failed** | ~10min | |

## 2. step3 陈旧断言修复清单（tests-only，未碰 rust_ext/src/** 与 db/**）

### 2.1 `test_windows_wsl_authority_e2e.py`（r3→r9 迭代，实证链）

| # | 失败签名（before） | 根因（实证） | 改法（after） |
|---|---|---|---|
| A | `E_HTTP_MANIFEST_MISSING`（test 1） | 进程内 MCP route 的 `CALLWARDEN_DIR` 在模块 import 时冻结为真实 HOME；fixture 把 USERPROFILE 重定向到 fake_home 后读不到隔离 manifest | fixture 显式 `CW_DAEMON_TRANSPORT=named-pipe`：daemon 不启 HTTP，route 走 `_inject_workspace_id`（daemon 侧按进程 env 的 USERPROFILE 解析权威库 = 隔离 task-DB）；bridge 不读该 env 不受影响 |
| B | `E_WORKSPACE_AUTHORITY_MISMATCH`（test 2/3，r4 singleton 方案引出） | singleton 注入 + `create_mcp_server().configure_workspace(PROJECT_ROOT)` 会注册**真实仓库根**（instance `d0135046dee33dfd`）污染后续 capture；探针实证：注册仓库根得 id=2（不复用 id=1） | 放弃 singleton 注入（r5 调试转储确认污染确定性），改 A 的 named-pipe 方案后 test 2/3/4 稳定通过 |
| C | `E_TASK_WORKSPACE_INSTANCE_REQUIRED`（test 1，r6/r7） | 8 源竞争的主任务原经**进程内 MCP 工具 `task_create`** 创建；该工具签名只转发 `title/description/steps/creator`，**无 workspace 参数**（产品缺陷，见 §4） | 主任务改经 bridge client RPC `task.create`（原生 RPC 支持 workspace 配对，同 test 2 路径）；claim 阶段仍保留 3 个真实 MCP 工具（`task_next_step` 无此缺陷），测试意图不变 |
| D | report 后 `in_progress != review`（r8） | `task.report` 未带 `step_id` → `task_collab_lifecycle.rs` 的 `remaining`（pending/in_progress 步骤）=1 → 停在 in_progress | 先 `task.status` 取未完成步骤的 `step_id`，report 携带 `step_id` + `success=True` |

### 2.2 `test_windows_daemon_e2e.py`

- CLI 测试 fixture 补 `CW_DAEMON_REGISTRY_DB`（隔离 daemon 权威 registry；仅 USERPROFILE 不足以隔离 CSIDL 解析的 registry）。
- `_wait_daemon` 超时 40s → 150s（干净环境下 daemon 冷启动两段 ~24s 空耗 + 管道绑定 + worker 池预热到首次应答 ~89s；旧 40s 恒假失败——证实 step0 §5 的 (a) 超时偏短，非 transport 回归）。
- 陈旧断言对齐 BR-01/BR-02：显式整数 `workspace_id` + 非空 `workspace_instance_id`（`_seed_task_workspace`：`workspace.register` → registry 整数 id 同写 task-DB `workspaces` 行）。

## 3. step4 陈旧断言修复清单（全部定性为「陈旧断言」，非 daemon 行为回归）

`test_lease_gate_empirical.py` 16 passed → 20 passed（4 失败全修）：

| 用例 | 失败签名（before） | 根因（实证） | 改法（after） |
|---|---|---|---|
| `test_single_active_reviewer_holder_competition` | `DID NOT RAISE E_LEASE_ACTIVE_EXISTS` | daemon orphan-lease 回收语义（`task_collab_lease.rs` Req 11.2）：holder 未注册/停用/心跳 stale（15min）时，同事务回收旧 lease 并授予新 lease → 竞争退化为「接管」 | 先 `agent.register`（`agent_id` 顶层显式 + identity）注册 holder，双活门禁真正触发 |
| `test_close_keeps_child_gate` | `E_TASK_IDENTITY_POLICY_REQUIRED` → `E_TASK_CONTRACT_BOOTSTRAP_ROLE_SOURCE: 缺少 current legacy role contract: reviewer` | 带 `role_contracts` 的 task.create：identity_policy 决策矩阵要求显式 `legacy_identity_v1`；`bootstrap_task_governance_contracts` 要求 executor/reviewer/adjudicator **三角色** legacy 行齐全 | 补 `identity_policy=legacy_identity_v1`；`_CHILD_ROLE_CONTRACTS` 补齐三角色（A' 模板同款 prompt_hash，与 `test_task_prompt_e2e.py` LEGACY_CONTRACTS 一致） |
| `test_enterprise/auto_daemon_unavailable_fail_closed_no_local_fallback` ×2 | `DaemonUnavailableError('daemon 返回未预期 HTTP status=502')`，断言文案 `"daemon 连接失败"` 不命中 | ① HTTP 迁移期 `route_task_write` 默认走 `HttpDaemonRpcClient`，旧 monkeypatch 只替 `UnixDaemonRpcClient` → 命中真实 HTTP 端点（环境漂移）；② `_inject_workspace_id` 在 route_task_write 的 try **之外**，未带 workspace_id 时注入阶段即抛原始文案，无包装 | monkeypatch transport 无关 seam `_get_rpc_client_for_route`；params 显式 `workspace_id: 1` 使故障落在 try 内（走文档化的包装路径，文案含「daemon 连接失败」）；fail-closed 本身始终成立（`fallback_calls == []` 未变） |

`test_task_prompt_e2e.py`：2 个用例在干净单写环境下直接通过（step0 所称 TypeError 未复现——属污染环境假失败）。

## 4. 越界发现——产品缺陷（另立卡，**已修复并验证**）

`server/tools/tools_task.py:51-65` 的 MCP 工具 `task_create` 存在两处缺陷（实证：r4 pydantic ValidationError + r7 `E_TASK_WORKSPACE_INSTANCE_REQUIRED`）：

1. **缺 workspace 参数**：签名仅 `(title, description, steps, creator)`，不转发 `workspace_id`/`workspace_instance_id` → 无法满足 BR-01/BR-02 权威契约（named-pipe 路由下 `_inject_workspace_id` 需 capture 链有 instance，MCP 入口无法显式声明）。
2. **返回注解与实际不符**：声明 `-> str`，实际 `_route(...)` 返回 dict → fastmcp pydantic 校验 `task_createOutput: Input should be a valid string` 拒绝（兄弟工具 `task_next_step` 用 `Optional[dict]`）。

→ **已闭环（2026-09-16）**：立卡 `T-1789564402123-9b47d988`（C-23 承接，父 `T-1788871227327-45c94bd8`），修复提交 `e4b335d`——签名补 `workspace_id: int = 0` / `workspace_instance_id: str = ""` 并逐字转发 `task.create` 载荷；返回注解改 `Optional[dict]`。验证双链：
- 单元回归 `tests/test_tools_task_create_workspace_params.py` 3 passed（fastmcp 真实校验层：配对逐字转发 + dict 返回不被 pydantic 拒绝 + 缺省线缆形状保持）。
- 共享 daemon live e2e（`refresh_shared_runtime.ps1 -TaskId T-1789529126780-6cf84728` 拉起，PID 40860、health.git_commit==HEAD、三方 sha256 一致）：`workspace.register` 取真实配对 → 进程内 MCP `task_create` 显式配对 → 返回 `task_id=T-1789563951668-ba0c338c / status=open`（旧码必被 pydantic 拒绝或 `E_TASK_WORKSPACE_INSTANCE_REQUIRED`）；缺省调用对照为 `E_TASK_WORKSPACE_INSTANCE_REQUIRED` fail-closed（与修复前一致，非回归）。
- 部署门禁回执：evidence `20260916-205814-f59890c1340e-f4e04602.json`（status=passed，core 双目标 sha256 一致，smoke 2/0）。

## 5. 交付物

- 本文件（族级零失败汇总 + 交接）
- `tests/test_windows_wsl_authority_e2e.py`（+152/-43：A–D 四类陈旧断言）
- `tests/test_windows_daemon_e2e.py`（+206：registry 隔离 + 超时 + workspace 权威）
- `tests/test_lease_gate_empirical.py`（5 元组解包 17 处 + step4 三类陈旧断言）
- 台账 `cw_task_commit_ledger.json`：补 `22580e9` 与本轮 tests commit 两条
- 回执日志：`cw_final_*.log`（Temp，4 文件 rc=0）

## 6. executor_ready_for_review 交接

- **Role**: executor → **Handoff**: reviewer
- **Task**: `T-1789529126780-6cf84728`
- **Scope**: `tests/**`（3 文件）+ `deliverables/software-company/w3_family_regression_20260916.md` + `cw_task_commit_ledger.json`
- **Forbidden**: `rust_ext/src/**`、`db/**`、`scripts/refresh_shared_runtime.ps1`、直写 SQLite、apply/close/supersede
- **验收要点**：干净单写环境复跑本文件 §1 四文件（rc=0 + 汇总行）；`git diff --check`；确认无产品码改动；§4 产品缺陷是否同意另立卡
- **残留风险**：`test_windows_daemon_e2e.py` 的 1 skipped（管道占用保护）；§4 产品缺陷已由 C-23 卡 `T-1789564402123-9b47d988` 修复（提交 `e4b335d`，live e2e 通过），MCP `task_create` 已可用
