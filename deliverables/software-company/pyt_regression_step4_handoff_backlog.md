# PYT 回归卡 Step#4 —— 未完成任务交接清单

- **生成时间**：2026-09-12 20:50 (+08:00)
- **来源**：`cw task next-action T-1788871227327-45c94bd8 --json`（权威，`evaluated_at=1789217238`）+ 本机实测
- **仓库**：`C:/git_work/callwarden`（主干 master，HEAD `593adf6`；worktree `master-27ef0740` 已对齐同 commit）
- **daemon**：`http://127.0.0.1:1615`，`worker_status=healthy`（**注意**：后台常驻易被回收，见 W8）

> **➡️ 2026-09-15 最新交接入口**：[pyt_remaining_handoff_20260915.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_remaining_handoff_20260915.md)
> —— PYT 父卡 16 张承接子卡（W12/C-03..C-22/NF1/NF2）**已全部 closed**，台账 #189；
> 剩余待做 = **C-02（§W9 defect_learn op_class）/ §W3 需-live-daemon 家族重扫 / 运维决策**，
> 含本机环境前置（`RUSTUP_HOME` pin、隔离 HOME 目录、MSVC 注入、daemon 拉起）与 A′ 环参数面。
> **接手请从该文档读起。** 当前部署端点已由 1615 迁至 **8535**（`runtime/current/cw-daemon.exe`，
> 内嵌 commit `00ca39b`），且 daemon 当前**未运行**（详见该文档 §6.1）。

> **➡️ 2026-09-16 承接进展（第二任 agent，daemon 已拉起 8535）**：三项残项均已推进，交付两份新文档：
> - **C-02 / §W9** → 已出 **daemon 侧修复计划** [c02_defect_learn_daemon_fix_plan_20260915.md](file:///c:/git_work/callwarden/deliverables/software-company/c02_defect_learn_daemon_fix_plan_20260915.md)：
>   **推翻交接「翻 op_class」建议**——实测真根因在 `rust_ext/src/daemon/**`（`handle_summary_defect_learn` 只读连接 fail-closed 桩 + 缺 INSERT 移植），
>   单改 Python 标签是假修复且超交接白名单，属 scope 缺陷须 **Planner 重规划**（建议 P2→P1）。**未改代码。**
> - **§W3 / 运维** → [pyt_w3_rescan_and_ops_20260916.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_w3_rescan_and_ops_20260916.md)：干净单写环境下实扫完成——
>   18 个 `_w3_harness` 文件 **17 通过**（路径①推广 harness 得到实证）；32 个 cargo-build 迁移候选 **23 通过/8 失败/1 挂**，逐条四类归因，
>   **真缺陷仅 2 例**（`test_lease_gate_empirical`、`test_task_prompt_e2e`）；余为环境前置/陈旧断言假失败（含「夹具内 cargo build 挂起」「多 daemon 并发争用」两坑，见其 §1）。
>   运维：`rust_ext/target-*` 合计 ~40 GB（全 gitignore），**因检测到并行 agent 在编 → 暂不清**（不 `rm -rf`）；残留隔离 daemon 已按父 PID 清理。

> **➡️ 2026-09-16 §W3 承接卡收口（T-1789529126780-6cf84728，已提交 `0d106e1` + `7555b2f`）**：
> step3+4 四文件在干净单写环境**零失败**——`test_windows_daemon_e2e` 8P/1S、`test_windows_wsl_authority_e2e` 4P、
> `test_lease_gate_empirical` 20P、`test_task_prompt_e2e` 6P（合计 38 passed/1 skipped/0 failed）。
> step0 初判的 2 例「真缺陷候选」经实证**全部定性为陈旧断言**（lease holder 未注册致 orphan 回收 / 三角色合同缺角色 /
> fail-closed monkeypatch 命中真实 HTTP 端点），无一改 daemon。汇总见
> [w3_family_regression_20260916.md](file:///c:/git_work/callwarden/deliverables/software-company/w3_family_regression_20260916.md)。
> **新增越界产品缺陷（待建卡，§W9 同族·Python 侧）**：`server/tools/tools_task.py:51-65` MCP 工具 `task_create`
> ①签名缺 `workspace_id`/`workspace_instance_id`（无法满足 BR-01/BR-02，named-pipe 路由下恒 `E_TASK_WORKSPACE_INSTANCE_REQUIRED`）；
> ②返回注解 `-> str` 与实际 dict 不符（fastmcp pydantic 拒绝；兄弟工具 `task_next_step` 用 `Optional[dict]`）。
> 建卡须先拉起共享 daemon（当前未运行，`cw daemon health` = `E_HTTP_MANIFEST_MISSING`）。
>
> **已闭环（2026-09-16）**：共享 daemon 已由 `refresh_shared_runtime.ps1 -TaskId T-1789529126780-6cf84728` 拉起（PID 40860，health.git_commit==HEAD）；立卡 `T-1789564402123-9b47d988`（C-23 承接），修复提交 `e4b335d`：签名补 `workspace_id: int = 0` / `workspace_instance_id: str = ""` 逐字转发，返回注解改 `Optional[dict]`。单元回归 3 passed + live e2e 显式配对创建成功（`task_id=T-1789563951668-ba0c338c / open`）。

---

## 0. 权威任务状态（接手前必读）

| 项 | 值 |
|---|---|
| task | `T-1788871227327-45c94bd8` — 「PYT-回归: pytest stale 期望与 fail-closed/Rust-authority 现状对齐（2026-09-08 全量 126 失败清零）」 |
| lifecycle / workflow | `in_progress` / `execution_in_progress`，3/5 步 |
| 当前目标 step | **step#4 `fix_defect`**，`step_id = T-1789139378194-02f1f66c`（**注意：这是 step_id，不是 task_id**） |
| decision / action | `READY` / `CLAIM`；`required_role=executor`，`lease_role=implementer`，`lease_required=true`，`fencing_required=true` |
| assignment | `A-ef7be0c68b3e36afc9615455`（`queued`，未 claim） |
| **allowed_paths** | `deliverables/software-company/**`、`tests/**` |
| **forbidden_paths** | `cli/**`、`db/**`、`rust_ext/src/**`、`rust_ext/src/daemon/**`、`server/**`、`scripts/refresh_shared_runtime.ps1` |
| acceptance | ①`git diff --check` 干净；②`python -m pytest tests/ -n auto --tb=short --maxfail=10 --timeout=300 --timeout-method=thread` **零失败**；③不改 server/cli/rust_ext 生产源码、不触发 shared runtime 发布；④**每个被改测试标注 stale 依据**并绑定分桶说明 |
| 角色契约 | skill `cw-executor-senior-engineer`，prompt `cw.aprime.executor.startup.v4`，handoff → reviewer |

> ⚠️ 已确认的**提交消息前缀纪律**：commit 必须写 **`[T-1788871227327-45c94bd8]`**（task_id）；
> 写 `[T-1789139378194-02f1f66c]` 是错把 **step_id** 当 task_id（本卡历史上有 9 个提交犯过，已在
> `cw_task_commit_ledger.json` 追加更正条目，提交 `c4ae6d0`）。

---

## 1. 环境前置（不做这步，后面全是假失败）

| # | 项 | 说明 |
|---|---|---|
| E1 | **venv 补齐 dev extra** | `C:/Users/wanpi/.workbuddy/binaries/python/envs/cw314/Scripts/python.exe`。canonical 环境是 `pip install -e .[dev]`（= `pytest` + `callwarden[parser-reference]`）。**本机已补装**：`tree-sitter-{php,swift,scala,hcl,elixir}`、`pytest-cov`、`numpy`、`sqlite-vec`。效果：抽样 4 文件 **153 → 58 FAILED（零改码）** |
| E2 | **`CW_TEST_MODE=1`** | 未设时 local/legacy 模式报 `E_MODE_DEPRECATED`，成片用例失败。`tests/convergence/test_m4_cli_fail_closed.py` 会自行 `delenv` 后单独断言该错误，故全局设 1 安全 |
| E3 | **隔离 HOME** | `USERPROFILE=HOME=<temp>`。否则真实 HOME 的 stale manifest 致 `E_HTTP_MANIFEST_STALE` |
| E4 | **去代理** | `unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy; NO_PROXY=127.0.0.1,localhost` |
| E5 | **`PYTHONPATH=C:/git_work`** | 仓库目录名即包名，需父目录入 path |
| E6 | **stdout 落盘** | Git Bash 重定向 MSYS python 的 stdout 会**整段丢输出**；且 `subprocess.run(capture_output=True)` 跑 pytest 会**永久挂死**（pytest 经 autostart 起 `cw-daemon`，孙进程继承 stdout 管道 → `communicate()` 不返回）。必须 `stdout=open(f,'w')` 落盘 |

**daemon 启动口径**（Windows）：
```bash
cd C:/git_work/callwarden
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY=127.0.0.1,localhost
export CW_DAEMON_TASK_DB="C:/Users/wanpi/.callwarden/callwarden.db"
export CW_DAEMON_DATA_ROOT="C:/Users/wanpi/.callwarden"
export CALLWARDEN_SKIP_AUTO_SETUP=1
rust_ext/target/release/cw-daemon.exe --http-bind=127.0.0.1:1615     # 用 Bash run_in_background 常驻
```
- **不要**传 `--socket http://…`（Windows 上它要命名管道，传 URL 会 `os error 123 创建数据目录失败`）；只给 `--http-bind=<host:port>`。
- 启动后 1–3 秒内首请求会 `E_HTTP_REQUEST_TIMEOUT`（启动竞态），重试即可。

---

## 2. 未完成任务清单

### W1 · 全量逐文件清单只跑完一半【优先级 P0】→ 已收敛（2026-09-13）

- **已收敛**：583 文件逐文件 sweep **全部跑完**，结果落盘
  `C:/Users/wanpi/AppData/Local/Temp/cw_sweep/results.jsonl`（**583 行**，实测 `Measure-Object -Line`=583）。
  基线汇总（修复前快照）：rc=0 → **454** 文件；rc=1 → **122** 文件；rc=5 → 7 文件（无用例收集）；
  累计 passed=7473 / failed=541 / errors=0。逐文件失败清单经三分类落 `triage_x1.log` / `triage_x2.log`。
- **原现状（保留备查）**：583 个测试文件中只测了 **276** 个 → 127 绿 / 72 个 `rc=1`（180 条失败）/ 67 个 `rc=120`（**驱动被杀时的连带噪声，非真失败**）/ 5 个超时。
- **剩余**：307 个文件未测；67 个 `rc=120` 需重测。
- **工具**：`C:/Users/wanpi/AppData/Local/Temp/chunk_driver3.py`（分片：`SHARD=<n> TOTAL=3`；结果 `chunks_<shard>.txt`）。**该驱动可续跑**，但需保证后台进程不被回收（见 W8）。
- **阻塞**：`-n auto` / `-n 4` 必 OOM（15 次 `node down: Not properly terminated`）；顺序全量 9m50s 被杀。`tests/_gen` 含 100k/1m 规模生成夹具是内存主因。
- **owner**：executor（可无 daemon 跑）

### W2 · A 桶剩余修复：6 个已确证陈旧范式的文件【P0】

**主体范式**：MCP 工具已全面 `_route` 化（`server/tools/tools_summary.py:44`、`tools_task.py:46` 均为
`from ..daemon_client import route_rpc as _route`），工具体是
`return _route('<rpc method>', {...}, 'READ_ONLY')` 一行式。旧测试仍 patch
`tools_mod._get_daemon_client` 并断言「客户端便捷方法被调用」——该模块属性**仍在**（`setattr` 不报错）
但**已无调用点** → 断言恒为 `Called 0 times`。
**修法**：`monkeypatch.setattr(<tools_mod>, "_route", fake_route)`，断言 `(method, params, op_class)`
三元组 + 回包透传 + 失败 fail-closed（`get_db` 不被打）。工具层**已无** HTTP/local 分支。

| 文件 | 失败数 | 备注 |
|---|---|---|
| `tests/test_build_read_rpc_http.py` | 1 | 含 `_get_daemon_client` ×1、`_get_db_path_for_daemon` ×1 |
| `tests/test_cli_080_http_rpc.py` | 1 | `route_task_*` ×8 |
| `tests/test_cli_090_http_rpc.py` | 2 | `route_task_*` ×6 |
| `tests/test_cli_093_http_rpc.py` | 2 | `route_task_*` ×7 |
| `tests/test_cli03_task_read_authority.py` | 1 | `route_task_*` ×1 |
| `tests/test_p0b_legacy_attestation_fail_closed.py` | 3 | `route_task_*` ×1；另含 ERROR 级 |

**已完成（勿重做）**：`tests/test_defect_read_rpc_http.py`（16→0，`f9705be`）、`tests/test_cli_081_http_rpc.py`（6→0，`443e274`）。

**已确证的语义变更（修这类断言时的通用依据）**：
- GOV-FIX-07/08：`error dict` / `DaemonRemoteError` 路径已从 `return True`（RC=0 假成功）改为 `sys.exit(2)`；RC 转换点在 `_run_subcommand_mode`（`cli/main.py:1774-1783`），`_handle_task` 层不吞 `DaemonRemoteError`。
- `{result, degraded}` 信封**不在** CLI 路由上产生：`_get_rpc_client_for_route()`（`daemon_client.py:3498`）只返回 `HttpDaemonRpcClient`/`UnixDaemonRpcClient`；`UnixDaemonRpcClient.call_with_autostart`（`:903`）是 `return self.call(...)`。带信封的 `DaemonClient.call_with_autostart`（`:1217`）不走该路由。
- 「本地回退 forbidden」的真实机制是 `RpcDBProxy.__getattr__` 转发到 `route_rpc`（`cli/main.py:1312`），不是返回 error dict、也不是抛 `DaemonUnavailableError`。

### W3 · 「需 live daemon / manifest / snapshot 前置」失败族（约 60 个文件）【P0，需决策】

- **现状**：剩余 72 个失败文件里，**约 60 个不含**上述陈旧范式，失败签名是
  `E_HTTP_MANIFEST_MISSING` / `E_HTTP_MANIFEST_STALE` / `snapshot_not_ready` /
  `invalid_params: 缺少字段: workspace_instance_id` / `E_HTTP_REQUEST_TIMEOUT`。
  典型：`test_mcp_*_http_rpc.py` 家族（每个 1–6 例）、`test_cli_task_lease_parity.py`(12)、
  `test_cli_task_claim_recover.py`(9)、`test_convergence/test_m3_concurrent_writes.py`(8)、
  `test_c6_snapshot_guard_replicator.py`(7)、`test_c5_s4_backup_restore_unify.py`(5)。
- **两条可选路径，需用户/Planner 定**：
  1. **提供隔离 daemon harness**（推荐）：给这些用例注入 `CW_DAEMON_HTTP_ENDPOINT` + 隔离 manifest，
     或让 conftest 起一个 session 级隔离 daemon。合同 acceptance 原文即写「预期隔离 daemon 型用例正常通过」。
  2. **Planner 改 acceptance 为分层**：本卡内 tests-only 子集零失败 + 「需 daemon 族」独立卡/CI 承接。
- **owner**：路径 1 = executor（`tests/**` 内可实现）；路径 2 = Planner（合同修订）
- **处理（横向推广，垂直 slice 先行）**：新建 `tests/_w3_harness.py`（模式A USERPROFILE 重定向
  隔离 daemon + workspace.register + task-DB seed + 空 codegraph snapshot.publish，镜像
  refresh.rs 运行时 schema 建全表）与 conftest 共享模块级 `w3_live` fixture。已推广
  **tools_query P0-compat S2 组 8 文件**（get_impact / get_symbol_history / get_recent_changes /
  get_issue_summary / find_issues / get_test_coverage / export_module_graph /
  get_comment_from_version）：`workspace_id`（旧 int）→ `workspace_instance_id`（新 str；
  路由层 snapshot_state.rs S2 要求），未知 workspace 由「空结果」改为「权威拒绝
  DaemonRemoteError」（fail-closed 语义）。**8 文件 41 例全绿**。其余 ~70 家族文件
  （tools_task / governance / lsp / identity / compat 等）同模板后续批承接。
- **B4 批（compat 家族 + lsp，5 文件 104 例全绿）**：`tools_security` /
  `tools_semantic` / `tools_summary` / `tools_task` compat 套件 + `test_mcp_lsp_check_available`
  迁移至 `w3_live`（动态注入 `workspace_instance_id` / endpoint，替换 CANONICAL_INSTANCE
  硬编码）。harness 补 seed：merge_preview 分支 workspace（callwarden/test-h9-ws）、
  cross_repo_impact TokenSlim workspace + 真实 hash 符号、ask_codebase dispatch 符号、
  audit_chain 20 条（table_name='tasks'，signature 失配 → 全 broken）、git 历史 3 条
  （首行对齐 MCP-064 commit_hash/change_type 断言）；并放宽环境依赖断言
  （get_project_dependencies 空 root → 契约 dict 形状；lsp_check_available 可用性
  依宿主机安装仅断 bool）。daemon 二进制按修改时间取最新（release 优先，防
  E_COMPAT_METHOD_NOT_FOUND），manifest 显式传隔离路径（防 E_HTTP_MANIFEST_STALE）。
- **B5 批（全量 77 文件横向推广，77/77 全绿、0 失败）**：`_w3_run_batch.py` 顺序跑
  全部 `tests/test_mcp_*_http_rpc.py`（含 identity / governance / lsp / clone / audit /
  guardrail / rules / cross_repo 等剩余家族），总计约 504 例（502 passed + 2 skipped：
  cross_repo_impact / get_vulnerability_blast_radius 各 1 例 skip 为宿主环境特性）。
  无需额外改码，复用 B4 批的 harness seed + 环境依赖放宽 + 最新 daemon 二进制选取
  策略即达成全量绿。至此 W3「需 live daemon」家族中 `test_mcp_*_http_rpc.py` 已全部
  完成隔离 harness 迁移与验证。
- **B 桶（需 live daemon）横向收敛完成 —— 2026-09-13**：整族迁移至 `w3_live` 隔离
  daemon harness（模式 A：`USERPROFILE` 重定向 + `workspace.register` + task-DB seed +
  空 codegraph `snapshot.publish`），B1/B2/B3 组**实测 0 failed**。harness 复用入口见
  [tests/_w3_harness.py](file:///c:/git_work/callwarden/tests/_w3_harness.py)
  （`find_daemon_binary` / `spawn_isolated_daemon` / `wait_manifest` / `setup_w3_client`）
  与 [tests/conftest.py](file:///c:/git_work/callwarden/tests/conftest.py) 的模块级
  `w3_live` + `route_stub`。

### W4 · conftest 隔离缺口：manifest / registry 未按测试隔离【P1】

- **现状**：`tests/conftest.py` 仅 82 行，唯一 autouse fixture 只 patch 了
  `config.get_project_db_path` / `db_base.get_project_db_path`。
- **问题**：`CALLWARDEN_DIR`（→ `http-daemon.<authority>.manifest.json`、registry、dedup）
  **未隔离**。并发/连续测试共享同一 manifest 文件 → 被写空 → `json.decoder.JSONDecodeError`
  → `E_HTTP_MANIFEST_STALE: HTTP manifest 读取/解析失败`（本轮实测复现）。
- **修法**（`tests/conftest.py` 属 allowed）：扩展 autouse fixture，
  `monkeypatch.setattr(_cw_config, "CALLWARDEN_DIR", str(tmp_path / "callwarden_dir"))` 并确保目录存在。
- **owner**：executor

### W5 · `db/**` 越 scope 的 D1–D5 修复待裁决【P1，需用户裁决】

- **现状**：D1–D5 是真缺陷且已修复、有回归，但落在合同 **forbidden** 的 `db/**`：
  - `db/db_build.py`（D1 新库首文件注册空 hash FK）— 提交 `9d41931`
  - `db/db_base.py`（D2 legacy 升级缺列；D3 v2→v3 空 hash FK；D4/D5 两处过早 DROP）— `9d41931` / `b87f545` / `fd23c89`
- **选项**：**A（推荐）** 剥离为独立 remediation 卡承接（保留提交、不改历史）；**B** Adjudicator 显式 ratify 扩 allowed_paths；**C** `git revert` 三个提交、D1–D5 转独立卡（D1 阻塞的 9 个用例将以 finding 挂起）。
- **owner**：用户 / Adjudicator / Planner

### W6 · `docs/evidence/**` 4 个文件不在 allowed_paths【P2】

- 合同 `required_evidence` 指定的是 `deliverables/software-company/**`。4 个 `docs/evidence/T-1789139378194-*` 文件属越界（内容有效，只是落点不合合同）。

### W7 · 5 个超时文件需单独定界【P1】

- `tests/test_cli_046_http_rpc.py`、`tests/test_gc_retention.py`、`tests/test_git_hook_capture.py`、
  `tests/test_h7_ast_cache_activation.py`、`tests/test_http_daemon_integration.py`
  —— 单文件 300s 未跑完。需判断是真慢、真挂，还是依赖缺失导致重试超时。
- **处理**：`test_http_daemon_integration` 根因=隔离 daemon manifest 发现错位（H6 后写
  USERPROFILE/.callwarden，旧 `_wait_manifest` 只查 data_root）→ `fb07740` 统一为模式 A
  （USERPROFILE 重定向）；`task.create` 双库 authority 分裂经方案A（`87b08e7` 种 task-DB
  `workspaces` 走第4步合法 capture）后 **10/10 全绿（20s）**。
- **C 定界（其余 4 个）→ 全部顺序运行绿，无需改码**：`test_gc_retention`(38)、
  `test_h7_ast_cache_activation`(8)、`test_git_hook_capture`(18)、`test_cli_046_http_rpc`(1)
  单独运行均快速通过。根因=W4 隔离前的 manifest/registry 竞争挂起 + W8 后台不稳（非真失败、
  非慢），W4 隔离（`2f31dd4`）后消除。仅需在稳定无并发环境跑，无代码修复。

### W8 · 后台常驻不稳（daemon / 长任务被回收）【P1，环境】

- 本轮 3 次后台 `cw-daemon` 与 3 个分片驱动**均被中途回收**（只剩 stale manifest 与 rc=120 噪声）。
- 需要更稳的常驻方式（或用 taskkill/进程守护），否则任何多小时的 sweep 都拿不到完整结果。

### W9 · advisory：`defect_learn` 的 op_class 标为 `READ_ONLY`【P2，需生产侧决策】

- `server/tools/tools_summary.py:492` 为 `_route('defect_learn', {...}, 'READ_ONLY')`，
  但该工具语义是**写面**（INSERT `defect_fixes` / `defect_patterns`）。是否收紧为 `PROTECTED_MUTATION`
  属 `server/**` 决策，**不在本卡 scope**。

### W10 · 三角色闭环尚未开始【P1】

- Executor 段未完成（step#4 未 claim），故 **Reviewer / Adjudicator 未进入**。
- 一旦 step#4 交付：Executor `report` → daemon 投影 `review_pending` → 交 Reviewer（只读核验，只输出 PASS/BLOCKED）→ Adjudicator 用真实 lease 执行 apply/close。

### W11 · 提交消息前缀改写（可选）【P2】

- 9 个历史提交消息中的 `[T-1789139378194-02f1f66c]` 仍是 step_id。已在 ledger 追加更正映射（非破坏性）。
  若需彻底修历史，需对未推送区间做 interactive rebase（**尚未做，需用户指示**）。

### W12 · `query.metrics_summary` 契约缺陷修复落在 forbidden 路径 → 已裁决=方案 A（剥离独立承接卡）→ ✅ 承接卡已闭环【P1】

- **触发**：`tests/test_cli_046_http_rpc.py` 按「覆盖真实 HTTP transport」改写后（移除 `route_rpc` 打桩、
  改打 `w3_live` 隔离 daemon），实跑暴露两个**生产真缺陷**：
  - **(A) 空 workspace 崩溃**：`rust_ext/src/daemon/metrics_handlers.rs` 的
    `SELECT AVG(s.depth)` 无匹配行返回 NULL，`scalar_f64` 转换失败 → `internal_error`。
    即任何空 workspace 上 `cw metrics` 必崩。
  - **(B) 返回契约不一致**：`query.metrics_summary` 原返回私有 7 字段
    (`symbols/calls/files/commented_symbols/functions/avg_depth/comment_coverage`)，
    而客户端渲染契约（[cli/main.py:10710](file:///c:/git_work/callwarden/cli/main.py#L10710-L10732)、
    [cli/main.py:14209](file:///c:/git_work/callwarden/cli/main.py#L14209-L14233)、
    [db/db_dashboard.py:309](file:///c:/git_work/callwarden/db/db_dashboard.py#L309-L327)、
    [db/db_summary.py:184](file:///c:/git_work/callwarden/db/db_summary.py#L184-L198)）与
    legacy 权威 [db/db_metrics.py:390](file:///c:/git_work/callwarden/db/db_metrics.py#L390-L399)
    均为 8 字段 → `cw metrics` 报 `KeyError: 'file_count'`。
- **修复落地（越 scope）**：
  - `rust_ext/src/daemon/metrics_handlers.rs`：`handle_metrics_summary` 改为复用既有
    `summary_metrics_summary`（8 字段 legacy 契约，单一真相源）；`AVG` 加 `COALESCE`；删除失效的 `scalar_f64`。
  - `rust_ext/src/daemon/query_compat_handlers.rs`：`summary_metrics_summary` 提升为 `pub(crate)`。
  - `server/tools/tools_workspace.py`：新增 `CodeMetricsSummary(TypedDict)` 显式 output schema（工具返回注解
    `-> dict` 改为 `-> CodeMetricsSummary`），8 字段全部 required；由 FastMCP 生成 `outputSchema` 并在工具返回时
    fail-closed 校验（实测：真实回包通过 / 多余键容忍 / 缺键 `ValidationError`）。
- **均在合同 forbidden_paths**（`rust_ext/src/**`、`server/**`），与 W5 同类，**已由用户 2026-09-13 裁决为方案 A**：
  A（推荐）剥离为独立 remediation 卡承接；B 显式 ratify 扩 allowed_paths；C revert 并转独立卡。
- **裁决执行（方案 A）**：
  - **承接卡**：`T-1789274621921-e5464ad8`（daemon 权威 `task.create` 创建，
    `parent_id=T-1788871227327-45c94bd8`、`workspace_id=1`、`workspace_instance_id=4baea3ff12c2ea5c`、
    `identity_policy=legacy_identity_v1`、5 steps / 3 role contracts / governance_projection `ok=true`、status=`open`）。
  - **闭环（2026-09-13）**：承接卡 `T-1789274621921-e5464ad8` 已 **closed / workflow=completed / decision=COMPLETE**（`blocking_conditions=[]`）。链：executor 提交 `18b00ba`（修复；`cargo test --no-default-features --lib metrics_handlers` 7 passed / `pytest tests/test_cli_046_http_rpc.py + 084..088` 26 passed / `git diff --check` exit 0）→ handoff `he-35296c7a58b3035106585567` → 独立 Reviewer 盲审 PASS（verdict `V-47b9fa19cfbd42932dc24bc6`，findings=0，evidence SHA-256 实测 `fb3647f0…` 与冻结值一致）→ handoff `he-61529463dfd96c99a639cfef` → 独立 Adjudicator 收尾（legacy 路径：adjudicator 自持 reviewer lease `L-adc3caa67e680396` fencing=9；`apply → 验证 applied_pending_close → close → release`）。台账 `cw_task_commit_ledger.json` 已回写 `status=complete`。
  - **承接合同**：[w12_metrics_summary_remediation_contract.md](file:///c:/git_work/callwarden/deliverables/software-company/w12_metrics_summary_remediation_contract.md)（同目录）。
  - **建卡脚本**：[create_w12_metrics_summary_remediation_task.py](file:///c:/git_work/callwarden/deliverables/software-company/create_w12_metrics_summary_remediation_task.py)（幂等：先 `task.list` 按 title 判重）。
  - **剥离方式**：3 处改动**全部仍在工作树、未提交**，`git log --grep=<PYT卡 id> -- rust_ext server db cli` 无输出
    → PYT 卡无越界提交，**无需改写历史**；由承接卡的 executor 合同（`allowed_paths` 含
    `rust_ext/src/daemon/metrics_handlers.rs`、`rust_ext/src/daemon/query_compat_handlers.rs`、
    `server/tools/tools_workspace.py`、`tests/`）负责提交与回归闭环。
  - **PYT 卡边界**：保持 tests-only；其 step#4 不提交上述 3 个生产文件，提交前缀亦不得复用承接卡 task_id。
- **验证**：`tests/test_cli_046_http_rpc.py` + CLI-084..088 = **26 passed**；
  Rust `daemon::metrics_handlers` 单测 **6 passed**；`cargo build --bin cw-daemon` 成功。
  回归门中的 `test_mcp_server_full.py` 5 例失败经 `git stash` 硬证为**预存在**
  （`E_HTTP_MANIFEST_MISSING/STALE`，属 W3/W4 未迁移族），与本改动无关。
- **消费者影响（仓库内）**：已穷尽检索确认**无任何消费者依赖旧 7 字段**
  (`symbols/calls/files/commented_symbols/functions/avg_depth/comment_coverage`)：
  `avg_depth` 全仓库 0 引用；`commented_symbols` 两处均为 `dashboard["code_scale"]`
  （[cli/main.py:9366](file:///c:/git_work/callwarden/cli/main.py#L9366)）与
  `stats.get("commented")`（[db/db_dashboard.py:214](file:///c:/git_work/callwarden/db/db_dashboard.py#L214)）
  等**不同来源**，非 `query.metrics_summary` 回包。
- **消费者影响（仓库外）**：同样**无字段级依赖**，证据：
  - **7 个外部 MCP 客户端**声明了 callwarden server（`c:\Users\wanpi\.mcp.json`、`.trae\mcp.json`、
    `.workbuddy\mcp.json`、`.cline\mcp.json`、`.kimi-code\mcp.json`、`.kiro\mcp.json`、
    `AppData\Roaming\TRAE SOLO CN\User\mcp.json`）——全部**仅含 server 启动声明**
    (`command`+`args`)，`args` 一律 `[cw.py, server]`，零输出字段引用。
    `.codebuddy` / `.lingma`(×3) / `AppData\Local\Claude\claude_desktop_config.json` **未注册** callwarden。
  - **TRAE 缓存的 tool 描述符**（`.trae-cn\mcps\*\mcp_callwarden\tools\get_code_metrics_summary.json`，
    14 份）只含 input schema（`arguments.properties={}`），**无 `outputSchema`**，`description` 为改动前旧
    docstring（`Returns: 全局度量统计字典`）→ 属**过期缓存**、不含任何字段名；客户端重启即从活跃 server 刷新。
  - **`c:\Users\wanpi\.trae-cn\mcps` 全量缓存中 `avg_depth` 0 命中。**
  - **外部项目**（`c:\git_work` 下 20+ 目录）：仅 `TokenSlim` 命中，且仅是它**自带的 code_graph 拷贝**
    （`scripts\code_graph\db\db_metrics.py` 等）；TokenSlim 无 `avg_depth`，`commented_symbols` 命中均为
    `get_uncommented_symbols` 的子串；其消费 callwarden 的方式是 CLI
    （`cw.py --workspace <ROOT> <子命令>`），**不经 metrics 字段**。
  - **记忆/会话日志**（`.trae-cn\memory\projects\-c-git-work-TokenSlim`、`.kiro\sessions\*`）命中亦仅为
    `get_uncommented_symbols` 子串，属历史记录、非活跃消费者。
  详见 §7。

### W13 · C 桶 finding 登记（跨 scope 生产缺陷，需承接卡）【P1】

> C 桶 = 在 PYT 卡内被识别、但修复落点属 `forbidden_paths`（`cli/**`、`db/**`、`rust_ext/src/**`、`server/**`、`scripts/**`）的**真实生产缺陷**。
> 一律不在本卡内修，登记为 finding，剥离为独立 remediation 卡（裁决口径见 W5 / W12）。
> 下表 C-04..C-12 均为 **2026-09-13 现场复核**（逐条读源码确认，非仅凭 sweep 报错推断）。
> C-10..C-12 为 step#4 全量 sweep 收口时**新发现**（证据见
> [pyt_regression_step4_acceptance_evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_acceptance_evidence.md#L1) §3.1）。

| ID | 严重度 | finding（根因） | 落点（行号为复核时值） | 复现/影响 | 承接 |
|---|---|---|---|---|---|
| C-01 | P0 | `query.metrics_summary` 空 workspace `AVG` NULL → `scalar_f64` 失败 → `internal_error`；返回契约 7 字段 vs 客户端 8 字段 | `rust_ext/src/daemon/metrics_handlers.rs`、`rust_ext/src/daemon/query_compat_handlers.rs`、`server/tools/tools_workspace.py` | 空 workspace 上 `cw metrics` 必崩 | 承接卡 `T-1789274621921-e5464ad8`（方案A，见 W12） |
| C-02 | P2 | `defect_learn` 的 `op_class` 标 `READ_ONLY`，实际为写面（INSERT `defect_fixes`/`defect_patterns`） | [server/tools/tools_summary.py:492](file:///c:/git_work/callwarden/server/tools/tools_summary.py#L492) | 写操作被当只读路由（无 request_id 幂等） | 待建承接卡（见 W9） |
| C-03 | **P0（阻断）** | **`rust_ext` unix/Linux target 无法编译**（5 个编译错误，见 W15 明细） | `rust_ext/src/daemon/transport.rs`、`http_server.rs`、`daemon_autostart_handlers.rs` | **整族 WSL 共存契约（子任务7）不可验证**；`tests/test_wsl_local_daemon_e2e.py` fixture 直接 fail（2 errors） | 承接卡 `T-1789290072972-5fad5b5c`（见 W15）✅ 2026-09-13 已闭环 |
| C-04 | P1 | `cw churn` 在 `trend` 非空时**必崩**：`for t in trend[:20]` 遮蔽模块级 i18n 函数 `t`，循环内 `t("cli.messages.churn_trend_item", …)` → `TypeError: 'dict' object is not callable` | [cli/main.py:3448](file:///c:/git_work/callwarden/cli/main.py#L3448-L3454)（`t` 定义见 [cli/main.py:36](file:///c:/git_work/callwarden/cli/main.py#L36)） | `cw churn` 只要有流失趋势记录即崩 | 承接卡 `T-1789290073049-6442e268` |
| C-05 | P1 | `cw test-impact` 在命中 ≥1 个测试时**必崩**：`for i, t in enumerate(tests, 1)` 同样遮蔽 i18n `t`，循环内调用 `t(...)` | [cli/main.py:7701](file:///c:/git_work/callwarden/cli/main.py#L7701-L7708) | `cw test-impact <fn>` 命中测试即崩 | 承接卡 `T-1789290073049-6442e268` |
| C-06 | P1 | `cw assignment create/revoke` 在 daemon 模式**必崩**：CLI 按 2 元组解包 `ok, result = db.create_assignment(...)` / `db.revoke_assignment(...)`，而 `RpcDBProxy` 的 RPC 回包是 **dict**；且方法名与 daemon RPC 名不符（`admin.assignment_create` / `admin.assignment_revoke`）→ 回包非 2 元素 → `ValueError` | [cli/main.py:17685](file:///c:/git_work/callwarden/cli/main.py#L17685)、[cli/main.py:17702](file:///c:/git_work/callwarden/cli/main.py#L17702)；daemon 侧 `rust_ext/src/daemon/route_matrix.rs:115-116` | `cw assignment create/revoke` 必崩 | 承接卡 `T-1789290073049-6442e268` |
| C-07 | P2 | `db.get_semgrep_summary(target_paths)` **位置参数被静默丢弃**：`_METHOD_MAP` 无 `get_semgrep_summary` 条目 → 走「按方法名原样路由」兜底并把位置参数降级为 `arg0`；Rust `handle_run_semgrep` 只读 `target_paths`（缺省 `["."]`）→ 用户指定路径被忽略，**退化为全工作区扫描** | [cli/main.py:1079-1287](file:///c:/git_work/callwarden/cli/main.py#L1079-L1287)（`_METHOD_MAP`）、[cli/main.py:1290-1298](file:///c:/git_work/callwarden/cli/main.py#L1290-L1298)（兜底）、`rust_ext/src/daemon/semgrep_handlers.rs:178-187` | `cw semgrep stats <PATH>` 静默全量扫描（性能/语义偏差） | 承接卡 `T-1789290073049-6442e268` |
| C-08 | P1 | `server/daemon_server.py` 的 `ADMIN_ONLY_METHODS` 缺 `mcp.backup_restore.backup_file`，而**同文件 :242 注释声称「与 Rust 端完全对齐」**；Rust `dispatch.rs:2254` 已含该方法 → 文件级备份写操作在 Python legacy daemon 上**未按 admin-only fail-closed** | [server/daemon_server.py:252-275](file:///c:/git_work/callwarden/server/daemon_server.py#L252-L275)（对照 `rust_ext/src/daemon/dispatch.rs:2254`） | `tests/test_phase8_admin_rpc_authz.py` 失败（未授权 peer 可调 backup_file） | 承接卡 `T-1789290073113-6808b1ac` ✅ 2026-09-14 已闭环 |
| C-09 | P2 | `SharedTaskWriterRequiredError` 的 `code` 类属性被父类实例属性覆盖：`__init__` 调 `super().__init__(f"{self.code}: {message}")` **未传 `code=`**，父类默认写 `self.code = E_HTTP_DAEMON_UNAVAILABLE` → 调用方读到错误 code | [server/daemon_client.py:93-99](file:///c:/git_work/callwarden/server/daemon_client.py#L93-L99)（父类 `:88-90`） | `except DaemonUnavailableError` 侧无法据 `code` 判别共享任务写点拒绝 | 承接卡 `T-1789290073113-6808b1ac` ✅ 2026-09-14 已闭环 |
| C-10 | P1 | `cw rule-list` 必崩：`_handle_rule_list` 对 RPC 回包按**裸 list** 解包并索引 `r["id"]/r["title"]/r["severity"]`，但 `rule.list` 回包是 **MCP-061 信封** `{"rules":[…],"count":n}` → 迭代 dict 得 str 键 → `TypeError: string indices must be integers, not 'str'` | [cli/main.py:2559-2563](file:///c:/git_work/callwarden/cli/main.py#L2559-L2563) | `cw rule-list` 在 daemon（信封）模式必崩；`tests/test_cli_057_http_rpc.py` 2 例复现 | 承接卡 `T-1789290073049-6442e268`（同 `cli/main.py`） |
| C-11 | P1 | `cw-agent start` 未完成 A′ CLI-005 HTTP 迁移：`_agent_start` 仍 `UnixDaemonRpcClient(socket_path=get_default_daemon_endpoint())`，违反「Python 仅 HTTP thin client + 编排、Rust daemon 唯一 authority」约束；下游副作用是注入的 `HttpDaemonRpcClient` 假体**永不被调用** | [cli/main.py:14892](file:///c:/git_work/callwarden/cli/main.py#L14892) | `tests/test_cli_005_http_rpc.py` 2 例红（源码断言失败 + 握手用例得 rc=2） | 承接卡 `T-1789290073049-6442e268`（同 `cli/main.py`） |
| C-12 | P1 | `cw-agent status` 同上未迁移：`_agent_status` 仍 `UnixDaemonRpcClient`，实际连到真实 Unix socket daemon（输出 `peer_uid: 4294967295 pid: 38744`），使 HTTP 假体路径与负向用例全部失效 | [cli/main.py:15056](file:///c:/git_work/callwarden/cli/main.py#L15056) | `tests/test_cli_006_http_rpc.py` 5 例全红 | 承接卡 `T-1789290073049-6442e268`（同 `cli/main.py`） |
| C-13 | P1 | `semgrep_handlers.rs`（CLI-061 复刻 `run_semgrep`/`run_semgrep_and_save`/`scan_semgrep_incremental`/`get_semgrep_summary`）**从未被编译**：`rust_ext/src/daemon/mod.rs` 无 `pub mod semgrep_handlers;`，dispatch/http_server 亦无对应 RPC 分支 → daemon 对上述方法一律 `method_not_found` | `rust_ext/src/daemon/mod.rs`（缺模块声明）、`rust_ext/src/daemon/semgrep_handlers.rs`（未接线） | `cw semgrep scan cli` → `method_not_found: 未知方法: run_semgrep`；`cw semgrep scan cli --quick` → `未知方法: get_semgrep_summary`。使 C-07 的 CLI 侧修复无法端到端验证 | 承接卡 `T-1789340885170-02a8fe9c`（`rust_ext/**`；2026-09-14 裁决单列一卡）✅ 2026-09-14 已闭环 |
| C-14 | P1 | daemon `handle_assignment_create` 的 INSERT 未包含 `assignment_id` 列，而 schema 为 `assignment_id TEXT NOT NULL UNIQUE` → 任何 create 必 `NOT NULL constraint failed` | `rust_ext/src/daemon/admin_handlers.rs:557-562`（对照 `db/schema.py:1626-1639`） | `cw assignment create …` → `internal_error: assignment_create: NOT NULL constraint failed: task_assignments.assignment_id`（RC=2） | 承接卡 `T-1789340885245-071cb9b4`（与 C-15 合并一卡；step0 先做契约单源裁决）✅ 2026-09-14 已闭环 |
| C-15 | P1 | daemon `handle_assignment_revoke` 强制 `task_id`(+可选 `role`)，与已文档化契约 `assignment_revoke(assignment_id)` 及 CLI `cw assignment revoke <assignment-id>` 不一致；且无 `assignment_id → task_id` 反查 RPC → CLI 侧无法兼容 | `rust_ext/src/daemon/admin_handlers.rs:577-600`（对照 [docs/mcp_tools.md:1972](file:///c:/git_work/callwarden/docs/mcp_tools.md#L1972)、[docs/cli_reference.md:3246-3257](file:///c:/git_work/callwarden/docs/cli_reference.md#L3246-L3257)） | `cw assignment revoke ASG-no-such --json` → `invalid_params: 缺少字段: task_id`（RC=2） | 承接卡 `T-1789340885245-071cb9b4`（与 C-14 合并一卡；契约单源裁决即该卡 step0）✅ 2026-09-14 已闭环 |
| C-16 | P1 | `assignment_show` 的 **positive 分支对生产调用方不可达**：CLI/MCP 侧不给数值 `workspace_id`，daemon 侧 `workspace_id` 缺失时直接走 `none` 分支 → 即便 assignment 存在也返回 `{'status':'none'}`，`show → create → revoke` 往返在 CLI 端仍不可用 | `rust_ext/src/daemon/task_collab_lease.rs`（`handle_assignment_show` workspace 解析）、`server/daemon_client.py`（`workspace_id` 注入白名单） | 卡 A 实测：不带 `workspace_id` 时 `assignment_show` 恒 `{'status':'none'}`；带 `workspace_id=1` 才命中 id=449 的 active 行 | **待建承接卡**（出界，见 §W18） |
| C-17 | P1 | admin 路由经 `owned_workspace` 传入的 `workspace_id` 是 **daemon registry 代理 id**，与 task DB `workspaces.id` 不同命名空间，而 `task_assignments.workspace_id` 带 `FOREIGN KEY (workspace_id) REFERENCES workspaces(id)` → create 恒 FK 失败 / revoke 恒误报 `assignment_not_found`。卡 A 仅收口 assignment 两处 | `rust_ext/src/daemon/snapshot_state.rs`（admin 路由块，其余 18 个 handler 同类风险） | 卡 A 实测：修复前 create 报 FK 失败；revoke 恒 not_found（根因是命名空间不一致，非 assignment 不存在） | assignment 两处 ✅ 2026-09-14 已随卡 A 闭环；其余 19 个 handler ✅ 2026-09-14 随承接卡 `T-1789365537230-c3f02eb4`（卡 D）闭环（commit `186582e`，见 §W18；残留缺陷转 §W20） |

> **C-13/C-14/C-15 发现背景（2026-09-13，卡②执行期）**：卡② `T-1789290073049-6442e268`
> 已完成 C-04..C-07 + C-10..C-12 全部 **CLI 侧**修复并回归全绿，但 `cw assignment create|revoke`
> 与 `cw semgrep scan` 的**端到端**仍红；逐条读源确认阻断点已前移至 `rust_ext/**`（越出卡②
> `executor_allowed`），故按 C 桶纪律登记，未伪造本地回退。证据：
> [T-1789290073049-6442e268-evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789290073049-6442e268-evidence.md) §2.3/§4。

- **✅ 承接卡已闭环（2026-09-14）**：承接卡 `T-1789290073049-6442e268`（C-04/C-05/C-06/C-07
  + 追加 C-10/C-11/C-12）。代码提交 `b2c6752`（前缀为本卡 task_id，未复用 PYT 卡 id）。
  修复：C-04/C-05 循环变量遮蔽 i18n `t` → `item`；C-06 新增 `_unwrap_bool_result` +
  `_resolve_assignment_workspace_instance_id` + `_METHOD_MAP.create_assignment` 扁平 holder
  identity；C-07 补 `get_semgrep_summary` READ_ONLY 具名 `target_paths`；C-10 `_handle_rule_list`
  信封解包；C-11/C-12 `_agent_start`/`_agent_status` 迁 `HttpDaemonRpcClient`。验收：
  `tests/test_cli_005/006/011/020/057/061/066_http_rpc.py` **23 passed**；`git diff --check` clean；
  forbidden paths（`db/**`、`scripts/refresh_shared_runtime.ps1`）未触碰。证据
  [T-1789290073049-6442e268-evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789290073049-6442e268-evidence.md)
  `sha256:6105bece…3a74`；终审记录
  [T-1789290073049-6442e268-adjudicator-close.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789290073049-6442e268-adjudicator-close.md)。
  治理闭环：5 step 全 done → executor handoff `executor_ready_for_review` → 独立 reviewer
  verdict `V-f60e227d7fe59c5fde3eb27c`（pass）→ 独立 adjudicator apply/close →
  `lifecycle_status=closed` / `workflow_status=completed`。
- **未闭合边界（如实披露，非本卡可解）**：C-07 / C-06 的**端到端**仍红，阻断点已前移至
  `rust_ext/**`（C-13 `semgrep_handlers` 未编译接线、C-14 `assignment_id` 列缺失、
  C-15 `revoke` 强制 `task_id`），越出本卡 `allowed_paths`，未以本地旁路伪造成功。
- **✅ 承接卡已闭环（2026-09-14）：C-13 → 卡 B `T-1789340885170-02a8fe9c`**。代码提交
  `1afabc8`（前缀为本卡 task_id，未复用 PYT 卡 id）。修复：`mod.rs` 新增
  `pub mod semgrep_handlers;`（根因：模块从未声明 → 4 个方法从未参与编译）；
  `snapshot_state.rs` 在 `handle_convergence_rpc` 新增 4 个方法名 arm（3 写经
  `open_codegraph_db_write`、`get_semgrep_summary` 只读经 `open_query_connection`）
  并新增进程内 dispatch 可达性回归；`dispatch.rs` 的 `PROTECTED_MUTATION_METHODS`
  增 3 写面、`CONVERGENCE_RPC_METHODS` 增 4 方法。验收：`cargo build` 零 error；
  `scripts/verify_route_matrix.py` 门禁全绿（243 方法一致、http_server 白名单=0）；
  `tests/test_c13_semgrep_dispatch_wiring.py` **9 passed**（源码契约层）；
  `cargo test --lib` 1776 passed / 7 failed 与基线**同集**（零新增失败）；
  `git diff --check` clean；`forbidden_paths`（`db/**`、`scripts/refresh_shared_runtime.ps1`）未触碰。
  证据 [T-1789340885170-02a8fe9c-evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789340885170-02a8fe9c-evidence.md)
  `sha256:390ca6b4…49b28`；独立复核报告
  [T-1789340885170-02a8fe9c-reviewer-review.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789340885170-02a8fe9c-reviewer-review.md)
  `sha256:F3090DDB…FE489`；终审记录
  [T-1789340885170-02a8fe9c-adjudicator-close.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789340885170-02a8fe9c-adjudicator-close.md)。
  治理闭环：5 step 全 done → executor handoff `executor_ready_for_review`（event 8838 转 review）
  → 独立 reviewer verdict `V-76f8df41df79cc908c9d7875`（blind_first_pass / pass / findings=0）
  → handoff `reviewer_pass`（event 8840，`he-38a8e38225d430c878607808`）→ 独立 adjudicator
  apply/close（event 8843/8844）→ `lifecycle_status=closed` / `workflow_status=completed`。
  **诚实披露**：C-07 端到端「后」回执仍未取得（共享 daemon 二进制早于本卡修复 + Windows 管道
  名由 SID 派生无法并起隔离实例 + 重建 runtime 属本卡 `forbidden_paths`），本卡以「前」真实回执
  + 进程内 dispatch 可达性证明交付，未冒充端到端已完成；`dispatch.rs` 因不在任何 step 的
  `target_file` 而未能入 `change_audit`，以证据 + 真实 diff + 提交留痕替代（未伪造目录级路径）。

- **✅ 承接卡已闭环（2026-09-14）**：承接卡 `T-1789290073113-6808b1ac`（C-08/C-09）。代码提交
  `740fed0fcfe9de1b43e4442bc999046a91b2a056`（前缀为本卡 task_id，未复用 PYT 卡 id）。修复：C-08
  `server/daemon_server.py` 的 `ADMIN_ONLY_METHODS` 补入 `mcp.backup_restore.backup_file`
  （14→15，与 Rust `dispatch.rs:2254` 对齐）并把注释行号修正为 `L2233-2255` + 显式说明 Rust
  端为子集；C-09 `server/daemon_client.py` 的 `SharedTaskWriterRequiredError.__init__` 改为
  `super().__init__(f"{self.code}: {message}", code=self.code)`，消除父类实例属性遮蔽。验收：
  `tests/test_phase8_admin_rpc_authz.py tests/test_shared_task_writer.py tests/test_cli_079_http_rpc.py
  tests/test_cli_govfix07_daemon_error_rc.py` **45 passed**；相邻面
  `tests/test_c5_s4_backup_restore_unify.py tests/test_srv_003.py` **30 passed**；
  `-k "C08 or C09"` **5 passed**；`git diff --check` clean；forbidden paths（`db/**`、
  `scripts/refresh_shared_runtime.ps1`）未触碰。证据
  [T-1789290073113-6808b1ac-evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789290073113-6808b1ac-evidence.md)
  `sha256:00f0bdc0…5ac23b`；终审记录
  [T-1789290073113-6808b1ac-adjudicator-close.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789290073113-6808b1ac-adjudicator-close.md)。
  治理闭环：3 step 全 done → executor handoff `executor_ready_for_review`（event 8817）→
  独立 reviewer verdict `V-afd3253b4af8144eb34c23c2`（pass）→ 独立 adjudicator apply/close
  （event 8821/8822）→ `lifecycle_status=closed` / `workflow_status=completed`。
  **诚实披露**：三 step 首次 report 未携带 evidence，executor handoff 被
  `E_HANDOFF_REPORT_PROVENANCE_MISMATCH` fail-closed 拒绝；处理方式为 release 旧 lease 后以
  **新 request_id** 逐 step 重新 report 补齐 evidence（event 8814/8815/8816），未绕过门禁。

> **未列为缺陷（本卡判定）**：`server/replicator.py:115-119` 的函数内延迟相对导入
> （`from ._mcp_common import …`）是**刻意的循环依赖规避**，在 canonical 包路径
> （`callwarden.server.replicator`）下工作正常；先前测试失败根因是**测试侧**用顶层
> `server.*` 包导入导致的越界相对导入（A 类，已修，见 §3）。**不作 C 桶登记**。

### W14 · C 桶承接纪律【P1】

- **登记即可，本卡不落地**：C 桶 finding 在 PYT 卡内只产出证据（复现步骤 + 根因 + 建议修法），不产生 `forbidden_paths` 提交。
- **提交前缀**：承接卡 executor 必须使用**其自身 task_id**，**严禁**复用 PYT 卡 `[T-1788871227327-45c94bd8]`（前缀纪律见 §0）。
- **PYT 卡边界**：保持 tests-only；step#4 不提交任何 `cli/**`、`db/**`、`rust_ext/src/**`、`server/**` 生产文件。
- **承接模板**：`task.create`（`parent_id` = PYT task_id）+ 合同 `allowed_paths` 显式含缺陷文件 + `tests/` 回归 + `governance_projection ok=true`（范式见 W12 承接卡）。
- **建议切分**：C-03 单独一卡（Rust unix 目标可编译，解除 WSL 共存契约阻断）；C-04/C-05/C-06/C-07 合并一卡（`cli/main.py` i18n 遮蔽 + RPC 契约）；C-08/C-09 合并一卡（`server/` 授权与错误 code）。
- **建卡回执（2026-09-13，daemon 权威 `task.create`，均 `governance_projection ok=true`、status=`open`）**：

  | 卡 | task_id | findings | 覆盖文件 | parent_id |
  |---|---|---|---|---|
  | C-03 | `T-1789290072972-5fad5b5c` | C-03 | `rust_ext/src/daemon/{transport,http_server,daemon_autostart_handlers,server}.rs`、`rust_ext/Cargo.toml`、`tests/` | `T-1788871227327-45c94bd8` |
  | C-04..C-07 | `T-1789290073049-6442e268` | C-04/C-05/C-06/C-07 | `cli/main.py`、`tests/` | 同上 |
  | C-08..C-09 | `T-1789290073113-6808b1ac` | C-08/C-09 | `server/daemon_server.py`、`server/daemon_client.py`、`tests/` | 同上 |

  - **追加归属（step#4 收口，无需新卡）**：C-10/C-11/C-12 与 C-04..C-07 **同落 `cli/main.py`**，
    承接卡 `T-1789290073049-6442e268` 的 `allowed_paths` 已含该文件，**无需扩边**；
    执行该卡时请一并覆盖 C-10（`_handle_rule_list` 信封解包）、C-11（`_agent_start` HTTP 迁移）、
    C-12（`_agent_status` HTTP 迁移）三处，回归文件分别为
    `tests/test_cli_057_http_rpc.py` / `tests/test_cli_005_http_rpc.py` / `tests/test_cli_006_http_rpc.py`。
    证据：[pyt_regression_step4_acceptance_evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step4_acceptance_evidence.md) §3.1。

  - **建卡脚本**：[create_c_bucket_remediation_tasks.py](file:///c:/git_work/callwarden/deliverables/software-company/create_c_bucket_remediation_tasks.py)（幂等：先 `task.list` 按 title 判重；`identity_policy=legacy_identity_v1`）。
  - **未建卡**：C-02（`defect_learn` op_class）仍属 W9 advisory，待用户裁决 op_class 口径后再剥离。

- **追加裁决（2026-09-14，用户批准）：C-13/C-14/C-15 剥离为 2 张承接卡**
  - **切分口径**：按「落点文件 + 契约耦合」切，**不按目录切**（沿用 C-03 单卡 /
    C-04..C-07 / C-08..C-09 的既有粒度）。
  - **卡 B（C-13）单独一卡**：落点 `rust_ext/src/daemon/{mod,snapshot_state,dispatch,
    http_server,route_matrix,semgrep_handlers}.rs`，与 assignment SQL 契约**零耦合**；缺陷类为
    「模块未编译 + 路由未接线」；**无契约前置**；闭环后解锁 **C-07 端到端**。
  - **卡 A（C-14 + C-15）合并一卡**：同落 `rust_ext/src/daemon/admin_handlers.rs`，且为同一份
    已文档化契约（`assignment_show → assignment_id` → `assignment_create → assignment_id` →
    `assignment_revoke(assignment_id)`，见 [docs/mcp_tools.md:1968-1972](file:///c:/git_work/callwarden/docs/mcp_tools.md#L1968-L1972)）
    的**两半**；分开修必产半截契约且无法凑出 `show → create → revoke` 完整验收。step0 须先做
    `assignment_revoke` **契约单源裁决**（`assignment_id` vs `task_id`，必要时含
    `assignment_id → task_id` 反查），故该卡 lead time 更长；闭环后解锁 **C-06 端到端**。
  - **不采用三合一**：三者仅共享目录 `rust_ext/src/daemon/**`，目录不是变更面，合并会模糊各组
    acceptance/evidence 边界，并让单次盲审跨越两个互不相关的授权面。
  - **不继续 parked**：三者均为可复现真缺陷（semgrep RPC 面整体不可达 / assignment 写路径必
    `NOT NULL constraint failed` / revoke 契约与文档不符），继续挂起会把已闭环的 C-06、C-07
    长期定格在「仅单测绿」。
  - **建卡回执（2026-09-14，daemon 权威 `task.create`，均 `governance_projection ok=true`）**：

    | 卡 | task_id | findings | Contract（r1） | 覆盖文件 |
    |---|---|---|---|---|
    | C-13 | `T-1789340885170-02a8fe9c` | C-13 | `sha256:152d3c90…9487` | `rust_ext/src/daemon/{mod,snapshot_state,dispatch,http_server,route_matrix,semgrep_handlers}.rs`、`tests/` |
    | C-14..C-15 | `T-1789340885245-071cb9b4` | C-14/C-15 | `sha256:8050e081…a500` | `rust_ext/src/daemon/admin_handlers.rs`、`docs/mcp_tools.md`、`docs/cli_reference.md`、`tests/` |

    - 两张卡 `parent_id` 均为 `T-1788871227327-45c94bd8`；建卡脚本同上（幂等，已扩表）。
    - **执行顺序**：**卡 B（C-13）先行**（无契约前置、落码路径短）；卡 A 待其 step0 契约裁决后落地。
  - **✅ 承接卡已闭环（2026-09-14）：C-14 / C-15 → 卡 A `T-1789340885245-071cb9b4`**。
    治理闭环：5 step 全 done → 独立 reviewer verdict `V-da5ad4a015f77515dc890d76`（pass /
    findings=0，blind_first_pass）→ handoff `reviewer_pass`（event 8861）→ 独立 adjudicator
    apply（event 8864）/ close（event 8865）→ `lifecycle_status=closed` /
    `workflow_status=completed`。修复：
    - `handle_assignment_create` 的 INSERT 补 `assignment_id`（`ASG-<16hex>`，`getrandom`+`hex`，
      不引新 crate），返回值由 `last_insert_rowid()`（整数）改为字符串 assignment_id —— 根因是
      该列 `TEXT NOT NULL UNIQUE`，缺列恒 NOT NULL 失败。
    - `handle_assignment_revoke` 入参由 `task_id`/`role`/`reason` 改为 `assignment_id`，
      **契约单源裁决以 `assignment_id` 为准且不保留回退**（`db_task_leases.revoke_assignment`
      同源），返回 `{ok, assignment_id, revoked_at}`。
    - 卡内实测**新发现 C-17**：admin 路由经 `owned_workspace` 传入的 `workspace_id` 是 daemon
      registry 代理 id，与 task DB `workspaces.id` 不同命名空间（`FOREIGN KEY (workspace_id)
      REFERENCES workspaces(id)`），令 create 恒 FK 失败 / revoke 恒误报 not_found；已在本卡
      改走权威 resolver `crate::daemon::task_collab::task_bound_workspace_id` 收口。
    - 证据：[T-1789340885245-071cb9b4-evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789340885245-071cb9b4-evidence.md)、
      [reviewer-review](file:///c:/git_work/callwarden/deliverables/software-company/T-1789340885245-071cb9b4-reviewer-review.md)、
      [adjudicator-close](file:///c:/git_work/callwarden/deliverables/software-company/T-1789340885245-071cb9b4-adjudicator-close.md)。
    - **仍未闭合（出界，见 §W18）**：C-16（`assignment_show` positive 分支对生产调用方
      不可达，落点 `task_collab_lease.rs` + `server/daemon_client.py`）与 C-17 其余 admin
      handler 的同类命名空间风险（计数见 §W18 的 18 vs 19 差异待澄清），均越出本卡
      `executor_allowed`，另建承接卡。

- **追加裁决（2026-09-14，接续卡 A 转交）：C-16 / C-17 剥离为 2 张承接卡**
  - **切分口径**：按「落点文件 + 是否有前置盘点」切，不合并。C-16 = 缺省值未走权威 resolver
    （落 `task_collab_lease.rs`，直接修）；C-17 = 路由层传入的 workspace 命名空间错配
    （落 `snapshot_state.rs`，**必须先盘点再定修法**），两者根因与节奏均不同。
  - **不继续 parked**：两者均为**已复现**真缺陷（非推断），继续挂起会让刚闭环的 C-14/C-15
    长期停在「仅 daemon RPC 层可用、CLI 端不可用」。
  - **建卡回执（2026-09-14，daemon 权威 `task.create`，均 `governance_projection ok=true`、
    `identity_policy=legacy_identity_v1`、`status=open`）**：

    | 卡 | task_id | findings | Contract（r1） | 覆盖文件 |
    |---|---|---|---|---|
    | C-16 | `T-1789365537146-bef4c2e4` | C-16 | `sha256:e6d41da96fdd13a4…` | `rust_ext/src/daemon/task_collab_lease.rs`、`server/daemon_client.py`、`tests/` |
    | C-17 | `T-1789365537230-c3f02eb4` | C-17 | `sha256:36535bd04d6c3260…` | `rust_ext/src/daemon/snapshot_state.rs`、`rust_ext/src/daemon/admin_handlers.rs`、`tests/` |

    - 两张卡 `parent_id` 均为 `T-1788871227327-45c94bd8`；均为 4 step
      （`adjudicate → implement → test → release_verify`），step0 = 裁决/盘点。
    - 建卡脚本：[create_c_bucket_remediation_tasks.py](file:///c:/git_work/callwarden/deliverables/software-company/create_c_bucket_remediation_tasks.py)
      （幂等，已扩 `build_cards()` 至 7 卡；端点改为 `CW_DAEMON_ENDPOINT` 环境变量覆盖，
      默认值保持历史值 —— 本次实测 1615 已下线，权威端口以 `cw daemon health` 为准）。
    - 自包含工单：[c16_c17_remediation_handoff_20260914.md](file:///c:/git_work/callwarden/deliverables/software-company/c16_c17_remediation_handoff_20260914.md)。
    - **执行顺序无硬依赖**（落点不同），可按需并行；**卡 D 务必先做盘点 step**。

### W15 · C-03 明细：`rust_ext` unix/Linux target 编译阻断【P0，阻断 WSL 共存契约】→ ✅ 承接卡 `T-1789290072972-5fad5b5c` 已闭环

- **复现**（WSL Ubuntu 2，`/root/.cargo/bin/cargo`）：
  ```bash
  wsl.exe -d ubuntu -- bash -lc 'export PATH="/root/.cargo/bin:$PATH"; export HOME=/root; \
    export CARGO_TARGET_DIR=/root/callwarden-wsl-e2e-target-sub7; cd /mnt/c/git_work/callwarden; \
    cargo build --no-default-features --manifest-path rust_ext/Cargo.toml --bin cw-daemon'
  ```
- **结果**：`error: could not compile callwarden-core (lib) due to 5 previous errors; 85 warnings emitted`。
- **5 个错误（全部 unix/cfg(not(windows)) 分支，与 feature 无关）**：

  | # | code | 落点 | 错误 | 建议修法 |
  |---|---|---|---|---|
  | 1 | E0063 | `rust_ext/src/daemon/transport.rs:153` | `ServerConfig` 初始化缺字段 `http`（unix 版 `ServerConfig` 定义见 `server.rs:65-85`，含 `pub http`） | 补 `http: None` |
  | 2 | E0603 | `rust_ext/src/daemon/http_server.rs:1453` | `struct Permissions is private`（`std::os::unix::fs::Permissions`） | 改用 `std::fs::Permissions` |
  | 3 | E0599 | `rust_ext/src/daemon/http_server.rs:1453` | `Permissions::from_mode` 未找到（缺 trait 导入） | 加 `use std::os::unix::fs::PermissionsExt;` |
  | 4 | E0599 | `rust_ext/src/daemon/daemon_autostart_handlers.rs:111` | `SocketAddr::from_path` 不存在（应为 `from_pathname`，且返回 `Result` 非 `Option`） | 改 `from_pathname(...).ok()` |
  | 5 | E0599 | `rust_ext/src/daemon/daemon_autostart_handlers.rs:113` | `UnixStream::connect_timeout` 不存在（std 无此 API） | 改 `UnixStream::connect` 或引入 `socket2` 设置超时 |

- **影响面**：Windows target 不受影响（这些分支被 `#[cfg(unix)]` / `#[cfg(not(windows))]` 排除），
  故生产（Windows release exe）无感 → 属**平台盲区长期积累的 latent 缺陷**。
  直接后果：WSL 共存契约子任务7（`tests/test_wsl_local_daemon_e2e.py`）**无法验证**，
  fixture 在「1. 构建」步 `pytest.fail`，2 个用例均报 error。
- **契约影响**：该测试**无缺陷**（其构建命令正确），因此**不在 tests/** 内以 xfail/skip 掩盖**；
  按 C 桶纪律仅登记 finding，待承接卡修复 Rust 后可自动转绿。
- **host 前置已就绪**：`wsl.exe -l -v` → `ubuntu`（Stopped，可启动）；WSL 内 `/usr/bin/python3`=3.10.12、
  `/root/.cargo/bin/cargo` 均存在 → `_require_wsl_ready()` 返回 True，测试**不会 skip**（是 fail 而非 skip）。

- **✅ 承接卡已闭环（2026-09-13）**：承接卡 `T-1789290072972-5fad5b5c`（C-03，P0 编译阻断）。
  代码提交 `d71d117`（前缀为本卡 task_id，未复用 PYT 卡 id）。3 处平台盲区修复：
  [transport.rs](file:///c:/git_work/callwarden/rust_ext/src/daemon/transport.rs) `create_listener` 补 `http: None`（E0063）；
  `http_server.rs` 去除私有 `Permissions::from_mode`，改 `std::os::unix::fs::PermissionsExt`
  + `std::fs::Permissions::from_mode(0o600)`（E0603/E0599）；
  `daemon_autostart_handlers.rs` 去掉 `SocketAddr::from_path` 与 `UnixStream::connect_timeout`，
  改 `UnixStream::connect`（E0599 两处）。验收 4 项判据全绿：WSL 内
  `cargo build --no-default-features --bin cw-daemon` 零 error；
  `tests/test_wsl_local_daemon_e2e.py` **2 errors → 2 passed**；Windows `cargo build` 未退化；
  `git diff --check` clean。
- **诚实披露（判据②转绿含夹具修正）**：`tests/test_wsl_local_daemon_e2e.py` 夹具按新增权威门禁
  commit `4b1380a` 与 WSL 共存契约 §7.2 做了 3 类 5 处**权威前置修正**（`workspace.register`
  与隔离 task-DB `workspaces` 权威行前置、`task.create` 显式传入 `workspace_id`/
  `workspace_instance_id`、启动脚本注入 `CW_DAEMON_TRANSPORT=uds`）；**未弱化任何断言、
  未使用 xfail/skip**（与上文「不在 tests/ 内以 xfail/skip 掩盖」一致）。
- 证据 [c03_p0_compile_blocker_evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/c03_p0_compile_blocker_evidence.md)
  `sha256:9b9657f6…94a7`；终审记录
  [T-1789290072972-5fad5b5c-adjudicator-close.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789290072972-5fad5b5c-adjudicator-close.md)。
  治理闭环：5 step 全 done → executor handoff `executor_ready_for_review` → 独立 reviewer
  verdict `V-d861ac076d1289cc01ee74a1`（pass）→ 独立 adjudicator apply/close →
  `lifecycle_status=closed` / `workflow_status=completed`。

### W16 · step#5 `fix_defect` 停驻（parked）与治理缺口披露【预期状态，勿重复整改】

- **step#5**：`T-1789293259780-5c3d5fd8`（`step_index=5`，`action=fix_defect`，`target=tests/`）
  —— 由 daemon `system_evaluator` 对 unresolved failed step 派生的**机械重复派工**
  （`origin_kind=system_evaluator`，reason「存在 unresolved failed step，唯一可领取目标为 remediation step」）。
- **复查结论：`owner_route=planner`**（计划/验收边界缺陷，非实现缺陷）。
  acceptance ②「`python -m pytest tests/ -n auto … 零失败`」的残余失败**结构性落在本卡
  `forbidden_paths`**（`cli/**`、`server/**`、`rust_ext/src/daemon/**`）→ 只改 `tests/**` 无法闭合，
  改测试只能造假绿。与 step#4 合同逐字一致，**allowed/forbidden 无变更**。
- **HEAD 源码级复核（本 step 新增，确认 C 桶 finding 仍成立，不重做全量 sweep）**：

  | finding | 落点（HEAD 实测行号） | 结论 |
  |---|---|---|
  | C-04 | `cli/main.py:3448` `for t in trend[:20]:` | 循环变量 `t` 仍遮蔽 i18n 函数 `t` |
  | C-10 | `cli/main.py:2559-2563` `for r in rules:` + `r["id"]` | 仍按裸 list 解包，未解 MCP-061 信封 |
  | C-11 | `cli/main.py:14892` `UnixDaemonRpcClient(...)` | `_agent_start` 仍未迁移 HTTP thin-client |
  | C-12 | `cli/main.py:15056` 同上 | `_agent_status` 仍未迁移 |

- **协议依据**：[role-protocol.md §3「Parked remediation step（pre-cutover 无退出路径，如实披露）」](file:///c:/git_work/callwarden/.agents/skills/cw-task-loop/references/role-protocol.md#L129-L136)
  —— Executor 复查确认 `owner_route=planner` 后**不得实施代码、不得完成该 step、不得把技术问题升级用户**；
  该 step **保持未完成属预期状态**，**重复派工不构成新授权**。
- **证据载体**：[pyt_regression_step5_parked_step_disclosure.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_regression_step5_parked_step_disclosure.md)
  （8 节：权威派工/复查依据/计划缺口证明/finding_id 承接/capability 缺口与退出路径穷举/交 Planner 修订方向 P-A~P-C/合规声明/复现命令）。
- **交 Planner 的修订方向（方向建议，最终裁决在 Planner）**：
  **P-A（推荐）** 把 acceptance ② 改为**分层口径**（本卡 = `tests/**` 零退化；跨 scope 生产缺陷由已建
  C 桶承接卡 `T-1789290072972-5fad5b5c` / `T-1789290073049-6442e268` / `T-1789290073113-6808b1ac` 分别验收）；
  P-B 按 A/B/C 三桶拆子卡；P-C 若坚持全量零失败则需扩 `allowed_paths` + 稳定 harness + 停生产 daemon + 解 `-n auto` OOM。
- **capability 缺口（内部治理维护，非用户可解）**：`planner_governance_v1` **未声明**
  （无 `READY/PLAN`、无 `planning_*`/`replanning_*` 投影、`task.create` 拒绝 `planner`、`replan` 路由不可用）；
  `executor_replan_requested` 为 **design-only**（daemon 结构化拒绝）。
  → 唯一合法路径 = **保持 parked + 登记本缺口**。
- **合规事实**：本 step **未重做全量 sweep、未实施未冻结代码（forbidden_paths 改动 0 行）、未触碰 `tests/**`、未升级用户**；
  step#5 终态 = **保持未完成**（parked）。
- **report 回执与派生（2026-09-13）**：`task report ... --fail` 已提交（`report_request_id=req-9093e553d6d6`，
  证据 = 本披露文档 `sha256:514d1a70de09d9f4d797d7c2aa09b1d3c577aa41b7c6ead2b3392bfe418154b5`）→ step#5 `status=failed`；
  daemon 随即派生 **step#6 `T-1789293736662-649f0c98`**（`fix_defect`，`queued`，`decision=READY/CLAIM`）。
  此即 §3 所述**重复派工活锁**：**不构成新授权**，后续收到 step#6 派工时**直接引用本段与披露文档**，
  **不得重做、不得实施代码、不得升级用户**。

### W17 · `cw collab verdict` 与 `cw lease` 传输面 authority 不一致【P2，跨 scope 生产缺陷】→ ✅ 承接卡 `T-1789301330757-87f33c34` 已闭环

- **发现场景**：W12 承接卡 `T-1789274621921-e5464ad8` 的独立 Reviewer 盲审（2026-09-13，
  注册身份 `reviewer-wb-186loop`），提交 verdict 时实测。
- **缺陷（已逐行实证）**：
  - `cw collab verdict` 走**本地传输**：`cli/main.py:16368-16387` 直接
    `from ..server.daemon_client import DaemonClient` → `DaemonClient.get_instance()` →
    `client.call_with_autostart("verdict.submit", params)`。
  - `cw lease acquire/renew/release` 走 **HTTP 权威面**：`cli/main.py:17345` `_route_lease_write(...)`
    （`lease.acquire` @ `:17538`、`lease.renew` @ `:17570`、`lease.release` @ `:17593`），
    与 `route_task_write("task.handoff", …)`（`cli/main.py:5119`）同一 HTTP 路由。
  - 后果：同一 task 上，本地传输侧报 `E_TASK_WORKSPACE_UNBOUND`（`task_workspace_bindings` 缺失）/
    `E_LEASE_NOT_FOUND`（`task=T-1789274621921-e5464ad8 role=reviewer 无 active lease，受保护写操作需要先 acquire_lease`），
    而 HTTP 侧同一 lease 可见且成功。
- **影响**：Reviewer 若按默认传输面提交 verdict 会 fail-closed 卡死；本次通过显式 HTTP 权威面完成
  （verdict `V-47b9fa19cfbd42932dc24bc6`、handoff `reviewer_pass` 均成功）。
- **两点次生问题**：(1) 实际子命令是 `cw collab verdict`，**不存在** `cw task verdict`
  （实测 `cw task: error: argument action: invalid choice: 'verdict'`）；
  (2) daemon 强制要求 `--view-manifest-hash`，`role-protocol.md` 示例未列。
- **未在本卡修复的原因**：落点在 `cli/**`，不在 W12 卡 `allowed_paths`；且属跨 scope 生产缺陷
  （与 W16 表内 C-11/C-12 `UnixDaemonRpcClient` 未迁移同类）。
- **建议承接**：新建独立卡，把 `cw collab verdict`（及 `_handle_collab` 其余 Governance_Write 方法）
  迁移到 `_route_*` HTTP 权威面，并同步更新 `role-protocol.md` 的命令形态与必填参数。
- **✅ 承接卡已闭环（2026-09-13）**：用户裁决「新建独立承接卡 + 全部 4 个方法一并迁移」→ 承接卡
  `T-1789301330757-87f33c34`。代码提交 `f0b7268`（后随台账提交 `25ee96b`），落点
  [cli/main.py](file:///c:/git_work/callwarden/cli/main.py) `_handle_collab`：`snapshot.publish` /
  `verdict.submit` / `reveal.submit` / `gate.decide` 由本地 `DaemonClient` 传输面统一收敛到
  `route_rpc(..., 'GOVERNANCE_WRITE')`，删除本地回退与 degraded 信封，fail-closed 改为
  `E_GOVERNANCE_WRITE_DEGRADED` + 非零 RC；回归锁改写 + 负例：`tests/test_task_verdict_cli.py` +
  `tests/test_cli_collab_snapshot_publish.py` **7 passed**、`git diff --check` exit 0。
  证据 [w17_collab_authority_migration_evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/w17_collab_authority_migration_evidence.md)
  `sha256:bcad75d8…7704`；终审记录
  [T-1789301330757-87f33c34-adjudicator-close.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789301330757-87f33c34-adjudicator-close.md)。
  治理闭环：6 step 全 done → executor handoff → 独立 reviewer verdict `V-23583814b61df9f3985a69e6`
  (pass) → 独立 adjudicator apply/close → `workflow_status=completed`。
- **二次结论（本卡未覆盖的相邻面）**：`E_TASK_WORKSPACE_UNBOUND` / `E_LEASE_NOT_FOUND` 在本卡后
  不再出现于 `cw collab` 写路径；`role-protocol.md` 已补 `--view-manifest-hash` 必填与
  `cw collab verdict` 命令形态。`_handle_collab` 之外仍存在的 `UnixDaemonRpcClient` 使用点
  （`cli/main.py` 的 `_agent_*` / assignment 族）属 C-11/C-12 同族未迁移面，由 C 桶承接卡
  `T-1789290073049-6442e268` / `T-1789290072972-5fad5b5c` / `T-1789290073113-6808b1ac` 跟踪。

### W18 · C-16 / C-17 出界登记（卡 A 实测新发现，需承接卡）【P1】→ ✅ 承接卡已建（2026-09-14）

- **来源**：卡 A `T-1789340885245-071cb9b4`（C-14 + C-15）执行与独立复核期间实测发现，
  越出该卡 `executor_allowed`（`rust_ext/src/daemon/admin_handlers.rs`、`tests/`、
  `deliverables/software-company/`），**未在本卡修**，按 C 桶纪律登记。
- **C-16 · `assignment_show` positive 分支对生产调用方不可达**：
  - 复现（reviewer 独立复现）：同名 `assignment_show` 调用**不带**数值 `workspace_id`
    → 恒返回 `{'status': 'none', ...}`；显式 `workspace_id=1` 才命中 `id=449` 的 active 行。
  - 根因：CLI / MCP 调用面不注入数值 `workspace_id`，daemon 侧缺省即走 `none` 分支。
    落点 `rust_ext/src/daemon/task_collab_lease.rs`（`handle_assignment_show` 的 workspace 解析）
    与 `server/daemon_client.py`（`workspace_id` 注入白名单），**均不在卡 A `executor_allowed`**。
  - 后果：即便 C-14/C-15 已修，`cw assignment show` 的 CLI 端 `show → create → revoke` 往返
    仍不可用（卡 A 只能以 daemon RPC 层显式 `workspace_id` 取证，未伪装 CLI 端到端）。
  - 建议承接卡范围：`assignment_show` workspace 解析改走权威 resolver
    `task_bound_workspace_id`；`server/daemon_client.py` 注入白名单同步；补 positive 分支测试。
  - **机制表述更正（2026-09-14，交接文档 §3.2 逐行复核）**：本条上文「CLI / MCP 调用面不注入
    数值 `workspace_id`」的**调用侧机制表述需修正** —— 本 HEAD 上的实际门禁是**通用**的
    `_is_task_scoped_authority_request()`（参数含非空 `task_id`/`superseded_id` 即判定为
    task-scoped 请求，据此**整体跳过** workspace 注入块），**不是**卡 A 证据 §4.1 所写的
    「只对 `task.` / `lease.` 前缀方法注入」（陈旧表述）。行号会漂移，接手方须在自己 HEAD 上
    重新逐行确认，不得照抄卡 A 证据。**设计意图本身自洽**（task-scoped 方法的数字 workspace
    应由 daemon 从不可变 `task_workspace_bindings` 解析），断裂点在 daemon 侧
    `handle_assignment_show` 未实现该契约（回落常量缺省值）。
  - **承接**：✅ 承接卡 `T-1789365537146-bef4c2e4`（`status=open`，4 step，
    step0 = 根因复核与调用侧注入门禁裁决）。
  - **闭环（2026-09-14）**：✅ 承接卡 `T-1789365537146-bef4c2e4` 已 `closed`
    （`workflow_status=completed`）。修法 = **daemon 单侧**：`handle_assignment_show`
    改走权威 resolver `task_bound_workspace_id`（无 binding → `E_TASK_WORKSPACE_UNBOUND`
    fail-closed；显式不一致 → `E_WORKSPACE_AUTHORITY_MISMATCH`；空/纯空白 `task_id` →
    `invalid_params`）。**`server/daemon_client.py` 裁决为不改**（契约单源：task-scoped 的数字
    workspace 应由 daemon 从不可变 binding 解析；改门禁会注入 legacy active workspace 且 blast
    radius 覆盖全部 `task.*`/`lease.*`，见证据 §1）。提交 `5cfb158`。
    独立复核：对**已部署二进制** `87C6200…` 重跑 10 例负向矩阵 10/10 通过；反证修复前基线
    `BE67915C…` 得 7 failed/3 passed。详见
    [T-1789365537146-bef4c2e4-adjudicator-close.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789365537146-bef4c2e4-adjudicator-close.md)。
    ⚠️ **残留（转 W19）**：修复后 `tests/test_mcp_assignment_show_http_rpc.py` 4 例陈旧断言
    转红 → 承接卡 C-18。
- **C-17 · 其余 18 个 admin handler 的 workspace 命名空间风险**（**计数存在 18 vs 19 差异，
  待承接卡澄清**）：
  - `snapshot_state.rs` admin 路由块经 `owned_workspace` 传入的 `workspace_id` 是 daemon registry
    代理 id，与 task DB `workspaces.id` 不同命名空间（`FOREIGN KEY (workspace_id) REFERENCES
    workspaces(id)`），同类 handler 可能同样恒失败或误报 not_found。
  - 卡 A 仅收口 `handle_assignment_create` / `handle_assignment_revoke` 两处；其余 handler
    **未改、未评估**（未做盘点即下结论）。
  - 建议承接卡范围：先做 admin 路由 workspace 命名空间盘点（列出每个 handler 的 workspace 来源），
    再决定统一走 `task_bound_workspace_id` 还是改路由签名。
  - **计数差异（2026-09-14，交接文档 §4.2 逐行复核）**：本条与卡 A 证据 §4.2 记作「**18 个**」；
    交接文档在本 HEAD 上**逐行数 admin 路由块为 21 个方法名，扣卡 A 已修 2 个 = 19 个待盘点**。
    该差异**必须在承接卡证据中澄清并回报本 backlog**，不得在未复核时沿用 18。
  - **承接**：✅ 承接卡 `T-1789365537230-c3f02eb4`（`status=open`，4 step，
    step0 = 强制前置「workspace 命名空间盘点」）。盘点结论若为「多数 handler 无风险」，
    应把范围**收窄**并写入卡 D 合同，不得硬把 19 个都改。
  - **✅ 承接卡闭环（2026-09-14，卡 D）**：`closed / completed / COMPLETE`，commit
    `186582e`。step0 盘点结论 = 19 待盘点中 **15 有缺陷**（4 无风险 / 3 恒失败 FK /
    8 静默空或误报 / 3 静默误写），不适用「多数无风险→收窄」分支，范围维持全部 19；
    **计数差异已澄清**：guard 列表 21 方法名，原记 18 系漏计 `_ =>` 兜底臂的
    `admin.select_interface_provider`，正确计数 = 21 − 2（卡 A）= **19**。
    修法 = 路由层统一改走 `open_codegraph_db_write`（复用 semgrep 写面先例，
    `client_view_root` 规范化匹配 `workspaces.root_path` 取真 id），handler 层零改动。
    验证：隔离判别矩阵 18/18（夹具以 dummy registry 行复现「代理 ROWID≠真 id」分裂）+
    基线反证 10 failed/8 passed + 生产实例只读实测 `gc_retention` 151/2205、
    `snapshot_compare` 151/2205/39723（修复前恒 0）。verdict
    `V-59ba9c1b4ba50de7efb2f804`（pass/findings=4）。证据：
    [T-1789365537230-c3f02eb4-evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789365537230-c3f02eb4-evidence.md)、
    [-reviewer-review.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789365537230-c3f02eb4-reviewer-review.md)、
    [-adjudicator-close.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789365537230-c3f02eb4-adjudicator-close.md)。
    **残留转 §W20**（F1/F2/F3 + edit.*/rule.* 第二路由块 + 模板白名单缺陷）。
- **证据**：[T-1789340885245-071cb9b4-evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789340885245-071cb9b4-evidence.md) §4.1/§4.2、
  [T-1789340885245-071cb9b4-reviewer-review.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789340885245-071cb9b4-reviewer-review.md) §3、
  [T-1789340885245-071cb9b4-adjudicator-close.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789340885245-071cb9b4-adjudicator-close.md) §3。

---

### W19 · MCP-015 `assignment_show` 陈旧断言（卡 C 回归对照暴露，需承接卡 C-18）【P1】

- **来源**：卡 C `T-1789365537146-bef4c2e4`（C-16）step3 `release_verify` 的「同集基线对照」实测发现。
- **现象**：`tests/test_mcp_assignment_show_http_rpc.py` 在 C-16 修复后 **4 例新增失败**：
  - `test_assignment_show_no_match` / `test_assignment_show_with_role` /
    `test_assignment_show_unknown_workspace` / `test_assignment_show_new_client_instance_stable`
  - 统一签名：`E_TASK_WORKSPACE_UNBOUND: task=X 未绑定不可变 workspace（task_workspace_bindings 缺失），拒绝操作`
- **A/B 反证（同集 20 例）**：
  | 侧 | 二进制 sha256 | 结果 |
  |---|---|---|
  | 基线（C-16 前） | `BE67915CBC5D4641AE3FBC255AC160D21ADA8D791B163CB98B0A0ED19DD3DF92` | 20 passed |
  | 卡 C（C-16） | `87C6200950A91713F74B5F49E9239E685F5B8CB231D8FD9A446F523EEC4250FE` | 4 failed / 16 passed |
- **根因**：该文件以合成 task_id（`X` / `NO-SUCH-TASK` / `999999`）断言 `assignment_show`
  在无 active assignment 时返回 `{"status":"none"}`——编码的是**权威模型落地之前**的「静默 none」语义。
  C-16 让 `handle_assignment_show` 走权威 resolver 后，无 binding 的 task **必须** fail-closed，
  二者不可同时成立 → 属**陈旧断言**（A 桶同族，但失败签名与 §W2 的 `_route` 范式、§W3 的
  manifest/snapshot 族**均不同**，是本卡新识别的第三类根因）。
- **判定依据**：① 兄弟 handler（`lease.acquire` / `task.apply` / `task.report` / `task.supersede`）
  一律 `task_bound_workspace_id` fail-closed；`task_workspace_id_or_active` 仅 1 处 provenance helper
  （`task_collab_shared.rs:170`）；② resolver 自述 `task_collab_shared.rs:465-470`「绝不回退 active
  workspace 或客户端 numeric id」；③ 该文件所用 `w3_live` harness（`setup_w3_client`）**只 seed
  `workspaces`、不 seed `task_workspace_bindings`**（`tests/_w3_harness.py:697-708`）→ 该 harness
  内 task 天然 unbound。
- **为何未在本卡修**：落点 `tests/test_mcp_assignment_show_http_rpc.py` 属 MCP-015
  （`T-1788963088148-495d7208`）产物；期望修法（种 binding 以**保留**「no-match → none」原覆盖）
  还须动全 W3 家族共享的 `tests/_w3_harness.py`。两者均越出卡 C 的 step 白名单与 scope，按 C 桶纪律登记。
- **建议承接卡 C-18 范围**：① 在 `_w3_harness.py` **追加**（不改既有行）一个 well-known 已绑定
  task 的 `task_workspace_bindings` seed；② 把 4 例的合成 task_id 指向该已绑定 task，
  `test_assignment_show_unknown_workspace` 改断 `E_WORKSPACE_AUTHORITY_MISMATCH`；
  ③ 另补「无 binding → `E_TASK_WORKSPACE_UNBOUND`」正例；④ 复核 W3 家族其余 `test_mcp_*_http_rpc.py`。
- **附带基建隐患（同卡登记）**：`tests/_w3_harness.py::find_daemon_binary()` 取候选 **mtime 最新**者，
  且 `CW_DAEMON_BIN` 中的 **MSYS 风格路径**（`/c/Users/...`）在 Windows Python 下 `os.path.isfile`
  判 False → 候选被**静默跳过**，回落到陈旧 `rust_ext/target/debug/cw-daemon.exe`。本卡实验中已复现
  一次假阳性（「20 passed」实为陈旧 debug 构建）。建议 C-18 一并收敛：候选判定加
  `os.path.exists` 兼容 + 选中即打印路径/sha256。
- **证据**：[T-1789365537146-bef4c2e4-evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789365537146-bef4c2e4-evidence.md) §4.3/§4.5。

---

### W20 · C-17 承接卡相邻缺陷（卡 D 盘点/实测新发现，需承接卡）【P1】

- **来源**：卡 D `T-1789365537230-c3f02eb4`（C-17）step0 逐 handler 盘点 + 隔离矩阵 + verdict
  `V-59ba9c1b4ba50de7efb2f804` findings。
- **F1 · `admin.gc_audit_get/list` 引用不存在的列**【P1】【✅ 已闭环 C-19】：SQL `tasks.workspace_id`
  （`admin_handlers.rs:138/:176`）——schema 的 `tasks` 表无该列，prepare 恒失败（与命名空间
  无关，C-17 路由修复不掩盖）。**已由承接卡 C-19 `T-1789392878852-bb9bef18` 闭环**（2026-09-14）：
  两处 SQL 改经 `task_workspace_bindings` join 作用域（与 `task_bound_workspace_id` 权威语义同源，
  unbound task 行不可见 fail-closed）；C-17 F1 known-defect 锚迁移为 4 条正例/隔离负例；
  A/B 旧二进制 4F/17P → 新二进制 21/21；commit `e2a2853`，部署二进制 sha `1AC3E759…`；
  verdict `V-17b7a6a8a1df30d61f369d25`，生产只读 probe 修复前恒 `no such column` → 修复后返回 5 行。
- **F2/F3 · INSERT 缺 NOT NULL UNIQUE 列**【P1】【✅ 已闭环 C-20】：`record_action_identity` 缺
  `action_identities.action_id`（`admin_handlers.rs:675`）、`register_attestation_revocation`
  缺 `attestation_revocation_records.revocation_id`（`:713`）——恒 `NOT NULL constraint failed`。
  **已由承接卡 C-20 `T-1789397153198-ee7baf18` 闭环**（2026-09-15，F2+F3 合并一卡）：对齐卡 A
  `gen_assignment_id()` 先例新增 `gen_action_id()` / `gen_revocation_id()`（OS CSPRNG 8B → 16 hex，
  前缀 `ACT-` / `REV-`，无 uuid crate）；`action_id` 调用方入参优先、为空回退生成（镜像 Python
  `db/db_task_identity.py:170-203`），`revocation_id` 内部生成（镜像同文件 `:609`）；
  两处 INSERT 列补齐（11 列/11 参、8 列/8 参），`db/schema.py` 零触碰。
  验收：隔离 A/B —— C-19 基线二进制（含 C-19 修复、不含 C-20 修复）19P/3F（错误即两处 NOT NULL）
  → 修复后 22P/0F，**反证由独立 reviewer 复现**；`cargo build` 零 error；
  `cargo test --release --lib` 1778P/6F，失败集为 C-13 归档基线真子集（零新增）；
  部署门禁 `refresh_shared_runtime.ps1 -TaskId` 回执 `status=passed`、`git_head==878e9242==HEAD`、
  三方 sha256 一致 `bd3ad671…`、PID 41792、`rollback=false`；部署同 sha 二进制复跑 22/22；
  生产只读核查两表零 NULL/空 id。commit `878e924`（前缀本卡 task_id）；
  verdict `V-0c82595367c1f79caa180eb8`（blind_first_pass / pass）；
  reviewer 独立复跑 + 反证 + 哈希核对 9 项全过；adjudicator ACCEPT 后 apply/close 完成。
  **诚实披露**：部署脚本旧版本清理步骤抛 `[safe-delete][SAFE_DELETE_FAIL_CLOSED]`（发生在
  build/switch/start/回执之后，仅旧版本目录未清理，回执 status=passed）；生产为只读核查、
  未做合成写 probe；cargo test 6 条失败为 Windows `is_daemon_available` 忽略 `socket_path`
  导致的陈旧断言（失败集为 C-13 基线真子集）。
- **F4 · `edit.*/rule.*` 第二路由块同类代理 id 缺陷**【P1】【✅ 已闭环 C-21】：
  `snapshot_state.rs:3262-3273`
  与 admin 路由块同构（`owned_workspace` 代理 ROWID → handler），不在 C-17 登记范围
  （交接文档仅圈「admin 路由块」）。**已由承接卡 C-21 `T-1789397153231-f07a8d84` 闭环**
  （2026-09-15）：第二路由块（edit/rule 写面 19 方法，arm ~L3284）整体改走同文件既有
  `open_codegraph_db_write` 路由层单点（client_view_root 匹配物理库 `workspaces.root_path`
  取真 id），方法名列表 / match 臂与全部 handler 零改动（19+/10-）。
  验收：22 测试隔离矩阵（registry dummy 行占 ROWID=1 复现「代理≠真 id」）——
  修复前部署二进制（bd3ad671…）10F/10P/2xfail（3×FK、edit_not_found、2×symbol_not_found、
  restored=0、candidates=0、2×查根失败），修复后 0F/20P/2xfail exit=0，**反证由独立
  reviewer 复跑复现**（失败集与 before 腿 10 严格判别锚逐一吻合）；`cargo build` 零 error；
  `cargo test --release --lib` 1778P/6F 与 C-13 基线同集（零新增）；C-17 admin 块矩阵 22/22 不受影响。
  部署门禁回执 `status=passed`、`git_head==b7fa16f==HEAD`、三方 sha256 一致 `4ea587d3…`、
  PID 25756（endpoint 8535）、`rollback=false`。commit `b7fa16f`（前缀本卡 task_id）；
  verdict `V-ae6e85b13bfe707d0b9adb3e`（blind_first_pass / pass）；
  adjudicator ACCEPT 后 apply/close 完成，lease 全 released。
  **新发现登记**：NF1 `gate.resolve_findings` 引用不存在的 `tasks.workspace_id` 列（与 F1 同族，
  C-19 未覆盖此处）；NF2 `summary.generate` upsert `ON CONFLICT(symbol_hash)` 在权威 schema
   无匹配 UNIQUE 约束（曾被路由缺陷双重掩盖，自上线从未端到端可用）——均以 xfail(strict)
   正例体锚登记于卡矩阵。**📦 已建卡承接（2026-09-15）**：NF1 = `T-1789436398881-877c169c`
   （修法=C-19 handle_gc_audit_get 先例套用：子查询改 task_workspace_bindings；产品代码卡，
   部署门禁必走）**→ ✅ 已闭环（2026-09-15，A′ 全环 closed/COMPLETE）**：根因经 A/B 实证更正为
   **SQLite 相关名解析**（子查询未知列解析到外层 UPDATE 表 task_gate_decisions 的 schema 保留列
   workspace_id，261 行全 NULL → 子查询恒空 → 自上线恒发 gate_not_found，非 prepare 失败）；
   修复 = edit_handlers.rs 单点 1+/1-（子查询改 `(SELECT task_id FROM task_workspace_bindings WHERE
   workspace_id = ?4)`，C-19 同款），commit `5c9806b`（前缀本卡 task_id）；C-21 NF1 xfail 锚迁移
   正例（4 行 FK 种子四元组），隔离 A/B before 1F(gate_not_found 恒发)→after 21P/1xf/0F；
   cargo lib 同集 1778P/6F 零新增；部署门禁 passed（git_commit==HEAD==5c9806b、sha256 `26519773…`
   三方一致、PID 39428 endpoint 8535、rollback=false）；生产只读探针无合成写（判别力披露如实）；
   verdict `V-93325c2aa5b3dd4cefd916be`（blind_first_pass / pass，reviewer 对部署二进制独立复跑
   22/0/0/1xf）；adjudicator ACCEPT 后 apply/close 完成；台账 #188；
   证据 `T-1789436398881-877c169c-step3-…-1050.json` + `-review-…-1125.json`；NF2 = `T-1789436399100-948b9498`**→ ✅ 已闭环（2026-09-15，A′ 全环 closed/COMPLETE）**：
   根因证实为**真 prepare 失败**（与 NF1 的 schema 保留列相关名遮蔽不同类）——symbol_summaries 权威
   schema 无任何 PRIMARY KEY/UNIQUE（仅非唯一索引 idx_summaries_hash），`ON CONFLICT(symbol_hash)`
   自上线即恒 internal_error，路由缺陷期被代理 id WHERE 恒空双重掩盖；修复 = 弃 upsert，按
   db/db_summary.py generate_summary L56-76 版本化语义重写（UPDATE is_current=0 →
   COALESCE(MAX(version),0)+1 → INSERT，整批 unchecked_transaction；**不加 UNIQUE 约束**——
   symbol_summaries 设计语义即版本化多行（db/db_base.py _migrate_v5_to_v6 docstring），全列 UNIQUE
   会破坏 Python 侧实现）+ dispatch 唯一调用点 `&mut Connection` 联动（snapshot_state.rs，白名单外
   按 C-13 先例证据留痕、reviewer 判 in_scope_acceptable）；job_runner.rs symbol_embeddings 的
   ON CONFLICT(symbol_hash) 合法（PRIMARY KEY）不在范围；commit `00ca39b`（前缀本卡 task_id，
   6 文件 +257/−38）；C-21 NF2 xfail 锚迁移正例 + 双调用多版本断言（v1 压 0 / v2 version=2
   is_current=1），隔离 A/B before 1F（错误文案逐字命中 prepare 根因）→ after 22/0/0；
   cargo lib 同集 1778P/6F 零新增；部署门禁 passed（git_commit==HEAD==回执==00ca39b 四方一致、
   sha256 `48b58ed2…` 三方一致、PID 14740 endpoint 8535、rollback=false）；生产探针零写
   （零命中非判别，披露如实）；verdict `V-f7836ac941bc4f94b9baac72`（blind_first_pass / pass，
   reviewer 对部署二进制独立复跑 22/0/0）；adjudicator ACCEPT 后 apply/close 完成；三角色 lease 全
   released；台账 #189；证据 `T-1789436399100-948b9498-step3-…-1154.json` +
   `-review-…-1158.json`。两卡 step target_file 全文件级（step0 预声明
   nf1/nf2_remediation_inventory.md），幂等建卡脚本
   `create_nf_defect_cards.py`（复跑 2 exists/0 created），回执
   `nf1_nf2_cards_receipt_20260915.md`。**诚实披露**：部署脚本本次以 pipe 模式拉起的
  daemon 随脚本会话终止（按先例以 current 二进制手动 HTTP 重启，新端点 8535）；脚本收尾
  genie-trash fail-closed（swap 已完成）；executor tester lease 在 step4 报告后过期，handoff 前
  重领（fencing 1）。NF2 卡执行期同源坑复现并已按先例处置：部署脚本 pipe 模式 daemon（PID 24300）
  随脚本会话终止（8535 呈 502 upstream connect failed，以 current 二进制 HTTP 手动重启 PID 14740）；
  脚本退出码 1 系末尾旧 core-backup 清理 safe-delete 13065>50 门槛（三度同源，回执 status=passed 为准）；
  新增环境坑=沙箱 bash 缺 basename/dirname（`scripts/msvc-env.sh` 不可 source，shebang shim 撞 WSL
  bash 被安全策略拦），MSVC 环境改由 python 探测 + eval 注入等价获得；同文件并行 Edit 覆盖竞态
  自伤 1 次（snapshot_state.rs），git diff 复核发现后顺序补写，严格串行编辑纪律。
- **F5 · C 桶建卡模板目录级白名单缺陷**【P2】【✅ 已闭环 C-22】：step `target_file` 为目录（`tests/`、`rust_ext`、
  `deliverables/software-company/`、`runtime/current`，全量 15 处）时 `changes[]` 全等比对必拒
  （`E_CHANGE_PATH_NOT_ALLOWED`，卡 D step2 实测；C-13 卡先例 §4.1 同源；代码依据
  `task_collab_lifecycle.rs` L285-290 解析 + L307-316 全等，盘点见 `c22_remediation_inventory.md`）。
  **C-22 `T-1789397153261-f23f38b8` 已闭环（2026-09-15，commit `6986826`）**：模板 9 处残留目录级
  target_file 全量文件级化（已闭环卡引用实际产物名 `T-<ts>-<id>-evidence/-inventory.md`，C-03 三
  rust 文件与台账 scope 逐字对应，多文件 `;` 连接）+ docstring SyntaxWarning 根治与计数修正（15 处）
  + F5 语义注记（`target_file` 文件级全等 vs `executor_allowed` 可目录前缀，混用即本缺陷根因）；
  新增 `tests/test_c_bucket_template_target_file_whitelist.py` 6 用例固化不变量（daemon 解析语义复刻、
  多文件拆分、docstring 防回退、C-20/21/22 预声明名在列）。校验 6/6 绿；幂等回归 12 exists/0 created
  （daemon 8535）；未部署（tests/deliverables-only，daemon 与 C-21 部署态一致）。A′ 环 verdict
  `V-e287a2cc0b4ee3f1758c41df`（blind_first_pass/pass，reviewer 独立复跑：校验测试 6/6 + 幂等回归
  12/0 + commit scope/sha256 核验；event 9067/9068/9071 → apply+close）。**诚实披露**：①前轮 14 次
  连续 Edit 对模板脚本出现持久化竞态（约 8 处丢失），弃用连续 Edit 改单次原子脚本重做并同进程
  即时复验；②step2 report 曾误传占位符 evidence-hash（req-2e786f171ea9），按 C-21 先例同参数重报
  补登真值（req-3824e3e7b4aa，latest reported event 权威）；③executor handoff request_id 一次烧号
  （req-handoff-exec-c22-01 → -02）；④executor lease role 命名与 C-21 范式不符（executor vs
  implementer），handoff 前按范式补领 implementer-role lease。
- **证据**：[T-1789365537230-c3f02eb4-evidence.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789365537230-c3f02eb4-evidence.md)
  §1.4/§3、[-reviewer-review.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789365537230-c3f02eb4-reviewer-review.md) §2、
  [-adjudicator-close.md](file:///c:/git_work/callwarden/deliverables/software-company/T-1789365537230-c3f02eb4-adjudicator-close.md) §4。

---

## 3. 已完成（勿重做）

| 项 | 证据 |
|---|---|
| D1/D2（db 侧，越 scope 待裁决） | `9d41931` + 证据 `8211219` |
| D3/D4/D5（v2→v3 空 hash + 两处过早 DROP） | `b87f545` / `fd23c89`，回归 `tests/test_db_v2_to_v3_migration_fk.py` 4/4 绿 |
| A 桶识别集（agent_rules / audit_chain / audit_key_rotation / bootstrap_* / cli_task_fix / cli_task_reopen / sync_log_cleanup） | `306b744` / `15de3c8` / `eb15a7e` / `3e47950` |
| 环境欠配（parser-reference extra 等） | 本机已补装，抽样 153→58 |
| 身份前缀 ledger 更正 | `c4ae6d0`（9 条） |
| A 桶续修 2 文件（22 例） | `f9705be` / `443e274` |
| W2 A 桶 build 读组 + CLI 080/090/093（8→0） | `ff9bf3a` |
| W4 conftest 隔离 CALLWARDEN_DIR（manifest 防共享写空） | `2f31dd4` |
| W3 隔离 daemon harness 统一模式A：`test_http_daemon_integration` 300s 超时→17s 7/10（manifest 发现稳定） | `fb07740` |
| 方案A 解决 `task.create` 双库 authority 分裂：`_seed_task_db_workspace` 种隔离 task-DB `workspaces` 表 id，走 `resolve_create_authority` 第4步合法建 capture → `test_http_daemon_integration` **10/10 全绿（20s）** | `87b08e7` |

---

## 5. 工作区内其它 open 卡（非本卡 scope，供参考）

daemon 的 workspace 作用域列表（`cw task list --flat`）当前只暴露 2 条：

| 卡 | 状态 | 说明 |
|---|---|---|
| `T-1788313854785-dd64cebc` | `open` | `BR-03 e2e B TokenSlim ws-10` —— 属 TokenSlim 项目，非 Call Warden |
| `T-1788253722521-3b2f8420` | `closed` | `Role Prompt v1 Gate manifest independent review vehicle` |

> 说明：`cw task list` 是按 active workspace 过滤的投影，**不等于**权威库全量。
> 权威库 `~/.callwarden/callwarden.db` 有 1372 张卡；要列其它卡需先切 active workspace
> 或按 task_id 直查（`cw task show <id>`）。

---

## 6. 一页速查：给接手 agent 的最短路径

```bash
REPO=C:/git_work/callwarden ; PY=/c/Users/wanpi/.workbuddy/binaries/python/envs/cw314/Scripts/python.exe
cd $REPO
unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy ALL_PROXY all_proxy
export NO_PROXY=127.0.0.1,localhost PYTHONPATH="C:/git_work" CW_TEST_MODE=1
export USERPROFILE="C:/Users/wanpi/AppData/Local/Temp/cw_ci_home" HOME="$USERPROFILE"

# 1) 权威状态（必读，勿臆测 task_id）
$PY cw.py task show        T-1788871227327-45c94bd8
$PY cw.py task next-action T-1788871227327-45c94bd8 --json

# 2) 单文件验证（改完即验）
OUT=... $PY <runner>.py  tests/<file>.py -q --tb=short -p no:cacheprovider --timeout=60

# 3) 逐文件清单（可续跑）
SHARD=0 TOTAL=3 $PY chunk_driver3.py
```

---

## 7. 备查：`db/**` Python 层的迁移定位（为何未被迁移）

> 结论先行：**`db/*.py` 从来不是迁移单元**。迁移是按「一个 MCP 工具 / 一条 CLI 链路」切片，
> 不是按 Python 文件切片；「没动 db/」是**设计上的刻意保留**，不是遗漏。

**证据 1 · 审计口径把 db/ 排除在「业务残留」之外**
`deliverables/software-company/audit_python_business_residue.py:8`
`SCAN_ROOTS = [ROOT / "server" / "tools", ROOT / "cli", ROOT / "cw.py"]`；
命中条件 `:33` `name.startswith("db.")` —— 即「残留」指 **CLI/MCP 里对 `db.<method>` 的调用点**，
`db/` 本身是被调用目标、不在扫描范围内。93 张 A′ CLI 卡的 `python_file` 全部是 `cli/*.py`，
无一张以 `db/` 为 owner。

**证据 2 · db/ 是 Rust handler 的「语义真相源」**
`docs/design/daemon-rust-migration-ledger.md` 多处标注，如 `:2090`「语义真相源：`db/db_query.py`
get_file_history」、`:2185`「语义真相源 `db/db_impact.py` L138-270」。做法是**把语义逐条复刻进 Rust handler**，
而非搬运 db 文件。

**证据 3 · 一部分被「明确决策为不迁移」**
`daemon-rust-migration-ledger.md:2197` `review_readiness → 保持 python_compat（明确决策，不迁移）`；
`:2310` `defect_learn → 不迁移 rust_native`；`:2440` `import_git_history → 不迁移`；
`:478` `get_test_coverage：不迁移 daemon，保留本地 SQLite`。

**证据 4 · db/ 在迁移任务里通常是 forbidden**
本 backlog `:20` `forbidden_paths: cli/**、db/**、rust_ext/src/**`；
`daemon-rust-migration-ledger.md:1607` / `:1660` 同样把 `db/` 列为禁止改动。

**证据 5 · 当前三重身份并存（既非权威、也非死代码）**

| 身份 | 说明 |
|---|---|
| Rust handler 的语义真相源 | 被逐条复刻（证据 2） |
| 测试/本地模式回退 | `local`/`legacy` 仅 `CW_TEST_MODE=1` 可用（`cw-rust-client-convergence-design.md:82-84`），生产不允许回退 |
| `python_compat` 工具的执行体 | compat worker 的 `_h_*` → `_bind_readonly_db().<db 方法>`；P0-COMPAT-v3 已清空白名单，`_h_*` 退化为「真相源参照」（`server/tools/tools_summary.py:690-695`） |

**证据 6 · 唯一前瞻处置是「条件性删除」**
`cw-rust-client-convergence-design.md:199-207` §2.5 T05：`db/ 下不再被引用的业务模块 →（删）`，
触发条件是引用扫描（`scripts/check_client_purity.py`）确认无引用。

**退役路径（非整目录搬迁）**：
按工具链 slice 把语义搬进 Rust → 删该 slice 的 Python fallback → 最后按引用扫描条件清理。
