# CallWarden 测试方案（v5 · 优化版 · 锚定代码事实）

> 版本：v5（优化版）｜ 日期：2026-10-10 ｜ 适用仓库：`callwarden`
> 配套文档：
> - `TEST_CASES.md` —— 分级测试用例清单（由 `gen_test_cases.py` 从权威源生成）
> - `COVERAGE_AUDIT.md` —— 覆盖矩阵与 skip 审计（**数字已据代码事实订正**）
> - `BUILD_ENV.md` —— 构建前置（Rust 工具链如何打通，cw-daemon 二进制如何产出）
> - `gen_test_cases.py` —— 可复现生成器

---

## 0. 修订要点（v4 → v5 改了什么）

v4 是一份**结构良好但覆盖声明失真**的方案。v5 在不推翻其骨架（测试哲学、两轴范围、L0–L3 分层、复用基建）的前提下，做四处实质性修正：

| # | v4 的问题 | v5 的修正 |
|---|-----------|-----------|
| 1 | 宣称"T2+T3 对全部 243 MCP / 234 CLI 做全参数真实调用，100% 业务表面覆盖" | 纠正为"**路由/调用可达 + 占位参数 + SKIP 计数**"，非功能正确性覆盖；列出被 SKIP 的真实工具/命令清单 |
| 2 | 断言"收敛套件是不可重造的唯一执行层" | 纠正：收敛套件仅占全仓测试函数 0.8%，且**在主 CI（Linux）因 `_pick_bin` 硬编码 `.exe` 而完全无法运行** |
| 3 | 门禁只有"skip_rate≤5%"，但 CI 无任何强制 | 新增**可落地的 CI 门禁配置**：覆盖率门禁、skip_rate 强制、基线回归、Rust 全模块单测、性能回归 |
| 4 | 缺 7 大测试维度（负向/边界、故障注入、安全、迁移语义、快照一致性、性能、精确断言落地） | 新增 §5 七大维度 + §13 分阶段 backlog |

> ⚠️ **阅读前提**：本方案的价值在于"诚实"。v4 把"路由可达 + 占位参数 + SKIP 计数"描述为"全参数真实调用、100% 覆盖、精确断言、门禁化"，但代码层面并不成立。v5 先讲清**现状事实**，再给**可达成**的优化路径。

---

## 1. 覆盖现状事实核查（必读）

以下数字均经代码核实（file:line 见各条）。**这是 v5 一切决策的依据。**

### 1.1 真实规模

| 指标 | 真实值 | 证据 |
|------|--------|------|
| 全仓 `def test_` 级函数 | **7133** | `tests/` 下 595 文件含测试函数 |
| 收敛套件（T1–T5/M1–M4 + 3 附加）测试函数 | **58**（占全仓 0.8%） | `tests/convergence/` 12 文件 |
| Rust `#[cfg(test)]` 单测函数 | **2073**（121 文件） | `rust_ext/src/**` |
| Rust 集成测试（e2e/perf/role_prompt） | 存在但未接入 CI | `rust_ext/tests/`：`daemon_e2e.rs`、`perf_daemon_baseline.py`、`role_prompt_e2e.rs` 等 |
| 真实 skip 站点 | **~447**（213 `pytest.skip(` + 234 `pytest.mark.skipif`） | 全仓 grep（v4 称"219"仅数了前者且数字仍错） |

### 1.2 三个致命落差

**落差 A —— 旗舰收敛套件在主 CI 完全无法运行（最严重）。**
- `tests/convergence/conftest.py:39-49` `_pick_bin()` 硬编码 `cw-daemon.exe` / `debug/cw-daemon.exe`，缺失即 `raise RuntimeError`。
- 主 `ci.yml` 的 `test` job 跑在 `ubuntu-latest`，其"Build Rust extension"步仅执行 `python release/build.py --rust`（构建 Python 扩展 `.pyd`，**不构建 `cw-daemon` 二进制**，`ci.yml:32`），随后 `pytest tests/`（`:82`）会触发收敛套件。
- 结果：Linux 上 `_pick_bin` 抛 RuntimeError → `isolated_http_daemon` 夹具 setup 失败 → **T1/T2/M3/M4/T4 + `test_regression_http_tools` 全部 ERROR**；T3 因无 daemon 而 SKIP。
- 注意：`e2e-verify-linux-x86_64.yml:97` **有** `cargo build --release --bin cw-daemon`，但那是独立的 e2e workflow，**不在主 `ci.yml`/`callwarden.yml`**。`cw-daemon` 的构建与收敛套件的运行在主 CI 中是断链的。
- **结论**：文档宣称的"243/234 全参数真实调用、100% 业务覆盖"在 CI 中执行量为 **0**。

**落差 B —— Rust 单测 CI 几乎不跑。**
- `ci.yml` 的 `rust-unit-test` job 仅 `cargo test --lib daemon::compat_native_handlers`（`ci.yml:143-144`），注释自承"未纳入 daemon:: 全模块"。
- 2073 个 Rust 单测中，dispatch 路由、storage、daemon HTTP handler（含 `test_capabilities_methods_map` `http_server.rs:5233`）、snapshot、lease、attestation 等绝大多数 daemon 逻辑**无 CI 回归保护**。

**落差 C —— "全参数真实调用"的参数大量是占位，断言只数总量。**
- `tests/convergence/param_provider.py` 的 `_resolve_by_name` 对未匹配参数返回确定性占位：`"seed"`、`"0"*64`（hash）、`"T-seed-..."`、`"tok-deep-0001"` 等。
- `seed_workspace`（`conftest.py:286-401`）只建符号图谱，**不预建 task/lease/agent/snapshot**。凡依赖真实实体的工具被喂假 ID，结果多为 `EXPECTED_BUSINESS`（not_found），**不验证业务正确性**。
- T2 断言仅 `DEFECT==0 & PASS>=100`（`test_t2_mcp_full_invocation.py:57-80`）；T3 仅断言 `defects <= 18`（`test_t3_cli_full_invocation.py:37,93-114`）。`TEST_CASES.md` 里的 `callees=={"add"}`、`file calc.py=={add,multiply}` 等"精确断言"**从未在测试代码中实现**。

### 1.3 被 SKIP 的真实工具/命令（覆盖盲区）

- **T2 SKIP_TOOLS（15 个，`t2_mcp_runner.py:36-54`）**：`rotate_audit_signing_key`、`delete_workspace`、`remove_file`、`prune_external_symbols`、`gc_retention`、`clear_clones`、`task_rollback`、`register_attestation_revocation`、`assignment_revoke`、`task_apply`、`task_close`、`build_graph`、`import_git_history`、`build_directory`。即**治理写面与破坏性写面主要靠 SKIP**，无正向正确性断言。
- **T3 SKIP_CMD_PREFIXES（`t3_cli_runner.py:36`）**：`workspace delete/register/set`、`gc *`、`task apply/close/rollback/reopen/revert`、`clone clear/detect`、`fts rebuild`、`audit rotate`、`assignment revoke/create`、`rule sync/insert-block`、`refresh`、`git/coverage/defect/semgrep import`、`server`、`watch`、`daemon *` 等。

> **一句话**：真实的"覆盖"主要来自那 6985 个 legacy 测试 + M1/M2/M4 的静态/单元校验，而非文档主角收敛套件。v5 的目标是把收敛套件**真正做成可执行、可断言、可在 CI 跑通的执行层**。

---

## 2. 测试哲学（三条铁律 + 三条补充）

| # | 铁律 | 含义 | 违反后果 |
|---|------|------|----------|
| 1 | **fail-closed** | 任何异常必须显式失败或返回结构化错误，**绝不静默降级 / 绝不本地兜底执行** | daemon 不可达 → `DaemonUnavailableError`；rc=0 但返回空/垃圾 → 判 FAIL |
| 2 | **整类关闭** | 一个分类（21 CLI 类 / 17 MCP 类）内全部用例通过后才整体关闭该类的 `cw task` 节点 | 不允许"挑几个过了就关类" |
| 3 | **门禁不降级** | 已知缺陷基线只许减少不许回升；新增 skip 必须带原因且计入 `skip_rate` | DEFECT 数、fail-soft 数、traceback 数只能降 |

> 第 1 条是最高优先级：rc=0 却返回垃圾比崩溃更危险——它让上游以为成功了。

**补充铁律（v5 新增）：**

| # | 铁律 | 理由 |
|---|------|------|
| 4 | **可执行优先** | 任何"覆盖声明"必须有对应的、能在主 CI 跑通的测试代码；CI 跑不起来的套件不计入覆盖。|
| 5 | **断言可证伪** | "全参数真实调用"必须配合精确断言（返回结构/字段/集合/数量），仅 `rc==0` 或"调用可达"不算覆盖。|
| 6 | **跨平台无硬编码** | 测试基建（二进制路径、管道/端口、路径分隔）必须跨 Windows/Linux/macOS，禁止 `.exe` 写死。|

---

## 3. 被测系统范围（两轴，措辞修正）

测试表面以两条正交轴定义，全部取自权威真相源：

| 轴 | 真相源 | 规模 |
|----|--------|------|
| **CLI 轴** | `cli/categories.py` | **21 类 / 84 顶层命令**（[18]-[21] 为 CLI-only） |
| | `tests/convergence/fixtures/cli_full_params.json` | **234 个叶子命令**（提取 233 + 跳过 1） |
| **MCP 轴** | `server/tools/_categories.py` | **17 类 / 243 工具**（与 CLI [1]-[17] 同构） |
| | `tests/convergence/fixtures/mcp_full_schema.json` | 243 工具真 schema |

**诚实区分两种"覆盖"**：
- **表面可达覆盖**（当前收敛套件做到的）：每个工具/命令的路由存在、参数可被构造、调用能往返（返回 PASS 或 EXPECTED_BUSINESS）。
- **功能正确性覆盖**（v5 目标）：对给定真实种子，返回结构/字段/集合/数量可被精确断言；负向输入返回结构化错误而非崩溃/空结果。

> 当前仅达成前者。v5 §5.7 给出把"表面可达"升级为"功能正确"的具体路径（deep fixture + 精确断言）。

**种子事实**（用于精确断言，非 substring 匹配）：

- `tests/convergence/seed_sample/calc.py`：`add(a,b)` / `multiply(a,b)`，且 **multiply → add 存在真实调用边**
- `tests/convergence/seed_sample/service.ts`：`MemoryRepo.find` / `MemoryRepo.save`、`Service` 类

由此可精确断言：`callees("multiply") == {"add"}`、`file("calc.py")` 符号集 == `{add, multiply}`。

---

## 4. 测试架构（分层，新增 L4 专项）

```
L0  基建      T1  种子 workspace fixture + param_provider 骨架（T2/T3 的前提）
   ──────────────────────────────────────────────────────────────────
L1  全参数    T2  243 MCP 工具真实调用（表面可达）
             T3  234 CLI 叶子真实调用（表面可达）
   ──────────────────────────────────────────────────────────────────
L2  横切质量  M1  239/239 路由矩阵（每个工具 rpc_method ∈ dispatch.rs）
             M2  Python 纯 client 审计（cli/ 无新违例）
             M3  双 agent 单 workspace 并发写一致性
             M4  CLI fail-closed（daemon 不可达不降级本地）
   ──────────────────────────────────────────────────────────────────
L3  系统级    T4  多 workspace / 多 agent 隔离与协同
             T5  真实 LLM 按工具 description 选对率（需 OPENAI_API_KEY）
   ──────────────────────────────────────────────────────────────────
L4  专项(v5)  N1 负向/边界矩阵   N2 故障注入/混沌   N3 安全(lease/identity)
             N4 迁移语义对比     N5 快照一致性/GC  N6 性能回归
             N7 精确断言落地(deep fixture)
```

> **覆盖结论（修正 v4）**：T2+T3 当前只做到"全部 243 MCP 工具与 234 CLI 叶子的**表面可达调用**"，并借助 SKIP + 占位参数 + 总量计数维持门禁。**功能正确性覆盖与 L4 专项目前基本为空**，是 v5 的主攻方向。

---

## 5. 新增七大测试维度（v5 核心）

### 5.1 N1 · 负向与边界测试矩阵
针对每类工具/命令，补充"错误输入"用例，断言**结构化错误而非崩溃/静默**：
- 符号不存在：`callees nonexistent_symbol` → 结构化 not_found，非空结果伪装成功。
- 空 workspace：未 build_graph 即查询 → 明确错误，不挂起。
- 非法参数：类型错/缺必填/越界 → 参数校验错误，rc≠0 或结构化错误。
- 超大输入 / 空字符串 / 特殊字符路径。
- **断言要求**：返回 JSON 含 `error`/`code` 字段；rc 与文档契约一致；**禁止** `rc==0` 配空 payload（铁律 #1）。

### 5.2 N2 · 故障注入 / fail-closed 端到端
M4 当前是**进程内 monkeypatch**（`test_m4_cli_fail_closed.py:57-121` 只测 `call_daemon`/`CliDispatcher`/`route_rpc` 三入口），未用真实 `python cw.py` 子进程打死端点。
- 新增：用 `subprocess` 真实启动 `cw` CLI，在 daemon 不可达（不启动 / 杀掉 / 错误端口）时，断言每条 CLI 命令返回结构化 `DaemonUnavailableError`，**绝不本地兜底执行**（铁律 #1）。
- 故障场景：daemon 进程被 kill 中段、HTTP 超时、返回 500、返回畸形 JSON、pipe 被占用。
- **断言要求**：rc 非 0 或结构化错误；输出不得含"falling back to local"类降级日志。

### 5.3 N3 · 安全：lease / identity / attestation
当前 T2 SKIP 了 `assignment_revoke`、`register_attestation_revocation`、`task_apply/close` 等治理写面，安全路径**零正向测试**。补齐：
- **lease 生命周期**：acquire → renew → 心跳 → release；lease 过期（TTL）后操作必须 `lease_expired` 拒绝（**当前缺"过期"用例**）。
- **并发抢锁**：M3 已有 lease 争用与 fencing，但需补"持锁者崩溃后孤儿 lease 由心跳超时回收"的确定性用例。
- **attestation 撤销**：`register_attestation_revocation` 后，被撤销 identity 的后续操作必须拒绝（**当前 T2 SKIP**）。
- **identity 会话隔离**：`check_session_separation` 的正向断言（两 session 不可串号）。
- **A′ 流水线门禁**：verdict→apply→close 的 reviewer lease 绑定、identity 三元组一致性（参考项目 memory 中的 E_ROLE_INDEPENDENCE_VIOLATION 陷阱）。

### 5.4 N4 · 迁移语义对比（Rust-native vs python_compat）
M1/`test_regression_http_tools` 只验证"路由存在/注册不丢"，**没有对比工具在 Rust-native 与旧 python_compat 下的输出一致性**。
- 对每个已迁移工具，双跑（native + compat 适配层），断言：返回结构同构、关键字段值一致、错误码一致。
- 重点覆盖：capability 广告键（`query.*` 真名，见 T-1790151978451 漂移修复）、dispatch 路由、租户/workspace 隔离语义。
- 命中"行为漂移"即 FAIL（禁止"路由通但语义变"的静默迁移）。

### 5.5 N5 · 快照一致性 / 回滚 / GC
`seed_workspace` 仅做 publish + `stats>0`（`conftest.py:339-352`），无快照收敛断言。
- 快照发布后，查询必须命中已发布快照（query.* 不被过期快照拦截，参考 memory 中"推新提交后须重发 snapshot.publish"陷阱）。
- 多 workspace 快照互不串扰（T4 协同场景的确定性断言）。
- GC 策略/归档/审计：保留期、归档导入、审计清单的正确性（对应 T2 SKIP 的 `gc_retention`/`gc_archive_*`）。

### 5.6 N6 · 性能回归门禁
`rust_ext/tests/perf_daemon_baseline.py` 存在但**未接入 CI**。新增：
- 在 CI 跑 baseline，断言 P95 延迟 / 吞吐不低于基线阈值（阈值随基线文件提交固化）。
- 关键路径：build_graph（种子样本）、query 热路径、MCP 单次 RPC 往返。
- 超阈值即 FAIL，防止"功能过了但变慢"的静默退化。

### 5.7 N7 · 精确断言落地（deep fixture）
把 §3 的"表面可达"升级为"功能正确"的关键工程：
- **扩充 `seed_workspace`**：除符号图谱外，预建确定性 task / lease / agent / snapshot（deep fixture），使 `param_provider` 能用**真实 ID** 而非占位（`"0"*64`）。
- **落地 `TEST_CASES.md` 的 19 个 P0 CLI 精确断言**：`callees=={"add"}`、`callers multiply=={}`、`file calc.py=={add,multiply}`、`stats.symbols>0` 等，从"文字清单"变为 T3 中真实 `assert`。
- **分项钉死**（替代当前仅总量 `defects<=18`）：
  - 5 个 fail-soft（call-chain / coupled-fns / largest-fns / rule applicable / status）：断言 `rc==0` 时 payload 非空，空结果=FAIL。
  - 3 个 traceback（collab publish / daemon publish / daemon snapshot-stats）：断言 stdout/stderr 不含 `Traceback (most recent call last)`。
  - 10 个 method_not_found（CLI→daemon compat RPC）：分项计数，只许减少不许新增。
- **MCP 侧重读工具精确断言**：对 seed 事实做集合/数量断言（如 `get_callees(multiply)==[add]`、`get_symbol(multiply).name=="multiply"`）。

---

## 6. 分级法（P0 / P1 / P2，补充"实现状态"）

| 优先级 | 含义 | 执行环境 | 放行要求 |
|--------|------|----------|----------|
| **P0** | 核心只读查询（精确断言）+ 已知缺陷钉死（fail-closed） | 种子 workspace（已 build_graph + deep fixture） | **必须通过**；DEFECT/fail-soft/traceback 不许回升 |
| **P1** | 常规契约 / 写路径 | 隔离 daemon + 临时 DB 实例 | 隔离环境通过即可合入 |
| **P2** | 破坏性 / 重操作（全量 refresh、clone clear、daemon *、server、watch 等） | **专用隔离沙箱**，**不进主回归** | 单独环境验证，禁止污染生产 daemon |

CLI 叶子当前分布：`P0=19`、`P1=187`、`P2=27`（合计 233 提取叶子）。
MCP 工具当前分布：`读≈171（P0）`、`写≈72（P1）`（合计 243）。

**实现状态标记**（v5 新增，详见 `TEST_CASES.md` 状态列）：
- P0 CLI 19 例：精确断言**目前 0 例落地**（仅总量分类），状态 ⚠️ 待 N7 落地。
- T2/T3 门禁：仅总量计数，**分项钉死未实现** ⚠️。
- M1/M2/M4：静态/单元校验**已落地 ✓**，但 M4 仅入口层非端到端 ⚠️。

**P0 判定规则**（CLI 叶子，`gen_test_cases.py::classify_cli`）：

- 命中 `KNOWN_FAILSOFT` → **P0 · FAIL_SOFT**
- 命中 `KNOWN_TRACEBACK` → **P0 · TRACEBACK**
- 命中 `READONLY_CORE` 且无上述 → **P0 · READONLY**
- 命中 `DESTRUCTIVE` → **P2**
- 命中 `WRITE_ISO` → **P1**
- 其余 → **P1 · GENERAL**

---

## 7. 门禁与放行标准（Gates，v5 强化）

| 门禁 | 指标 | 阈值 | 当前基线 | CI 强制 |
|------|------|------|----------|---------|
| **收敛套件可执行** | T1–T4/M1–M4 在 Linux CI 跑通 | 0 ERROR / 0 因 `_pick_bin` 失败 | ❌ 当前全 ERROR | **新增强制** |
| 主回归 skip 率 | `skip_rate = skip / total` | **≤ 5%** | 真实 ~447/7133≈6.3%（超阈值） | **新增强制**（当前仅文档） |
| 行覆盖率 | `pytest --cov` | **≥ 阈值（首版定 60%，逐步升）** | 无门禁 | **新增强制** |
| Rust 单测 | `cargo test` 覆盖模块 | **daemon:: 全模块**（非仅 compat_native_handlers） | 仅 1 模块 | **新增强制** |
| T2（MCP） | DEFECT / PASS / 覆盖 | `DEFECT==0` 且 `PASS>=100` 且 `覆盖==243` | 157/72/0 | 已有，须 CI 真跑 |
| T3（CLI） | DEFECT 基线 | **不回升**（基线 18） | 70/114/18/31 | 已有，须 CI 真跑 |
| T3 fail-soft | 5 个 rc=0 吞错命令 | 不得新增，断言"空结果=FAIL" | 仅总量钉死 | **分项钉死新增** |
| T3 traceback | 3 个命令 | 输出不含 `Traceback` | 仅总量钉死 | **分项钉死新增** |
| T3 method_not_found | CLI→daemon compat RPC | 不得新增 | 10（系统性） | **分项计数新增** |
| M1 | 路由矩阵 | 239/239 | 239/239 | 随 `pytest tests/` 跑 ✓ |
| M2/M3/M4/T4/T5 | 静态/并发/fail-closed/隔离 | 全部通过 | — | 须 CI 真跑 |
| N6 性能 | P95 延迟/吞吐 | ≥ 基线阈值 | 无门禁 | **新增强制** |

> 未达 `skip_rate ≤ 5%` 与收敛套件可执行前，**不准宣称"通过"**——否则"通过率"是自欺。

---

## 8. 已知缺陷基线（必须钉死不许回升）

T3 首轮（生产 daemon `b495919`）：**70 PASS / 114 EXPECTED_BUSINESS / 18 DEFECT / 31 SKIP**

| 缺陷类 | 数量 | 代表 | 钉死方式（v5 须分项） |
|--------|------|------|----------------------|
| rc=0 掩盖真 bug（fail-soft 吞异常） | 5 | `call-chain`, `coupled-fns`, `largest-fns`, `rule applicable`, `status` | **分项**：rc==0 时必须返回有效载荷，空结果/吞异常 = FAIL |
| traceback | 3 | `collab publish`, `daemon publish`, `daemon snapshot-stats` | **分项**：stdout/stderr 不得含 `Traceback (most recent call last)` |
| method_not_found（CLI→daemon compat RPC 未实现） | 10 | 由探测得出 | **分项计数**：不得新增，只许减少 |

> **关键**：rc=0 却返回空/错误 = 比崩溃更危险。这 5 个 fail-soft + 3 个 traceback 是当前最高价值用例，v5 §6/§7 要求把它们从"总量 ≤18"升级为**逐项可观测**。

---

## 9. 前置条件与解锁步骤（修正）

收敛套件依赖以下前置，**按依赖顺序解锁**：

1. **构建 cw-daemon（CI 必须做，当前缺 ❌）**
   `cargo build --release --bin cw-daemon`（或 debug）。**主 `ci.yml` 当前漏了这步**（只 `build.py --rust`）。
   > **跨平台修复**：`conftest._pick_bin` 硬编码 `.exe` 必须改为按 `sys.platform` 选 `cw-daemon`/`cw-daemon.exe`（见 §12）。本机工具链打通记录见 `BUILD_ENV.md`。

2. **停掉生产 daemon 消"管道被占用"skip（待执行）**
   约 17 处 skip 因默认命名管道被现有生产 daemon 占用。收敛套件用隔离 daemon（临时 data_root），需在**生产 daemon 停止时**运行（`cw daemon stop`）。

3. **配置 `OPENAI_API_KEY`（解锁 T5）**
   T5 LLM 可理解性测试在无 key 时 skip。配置 `.env` 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 即解锁。

4. **补齐语言 fixture（消 ~22 处 skip）**
   `fixture {lang}.json 不存在` 类 skip 需对应语言的种子 fixture；视需要生成。

5. **确保 cargo 在 PATH（消 ~8 处 skip）**
   部分用例会临时 `cargo build` 新鲜二进制，需 `cargo` 可达。

> 其余 skip（Windows 无 AF_UNIX 4 处、frozen build 5 处等）为平台/状态固有，属**合理 skip**，不计入"未达标"，但需在报告注明。

---

## 10. 执行顺序（dogfooding 在 `cw task` 树上）

```
1. [Phase 0 紧急] 修复 CI 可执行性：
   - 主 ci.yml 增 cargo build --bin cw-daemon
   - _pick_bin 跨平台化（去掉 .exe 硬编码）
   - rust-unit-test 扩到 daemon:: 全模块
2. [Phase 1] 精确断言 + deep fixture（N7）：
   - 扩充 seed_workspace 预建 task/lease/agent/snapshot
   - 落地 19 个 P0 CLI 精确断言 + fail-soft/traceback 分项钉死
3. [Phase 2] 负向/边界 + 故障注入（N1/N2）：
   - N1 负向矩阵；N2 真实子进程打死端点验证 fail-closed
4. [Phase 3] 安全/迁移/快照/性能（N3/N4/N5/N6）：
   - N3 lease 过期/attestation 撤销/会话隔离
   - N4 Rust-native vs compat 语义对比
   - N5 快照一致性/GC；N6 性能回归门禁
5. 每关一类，整体关闭该类 `cw task` 节点（铁律 #2）
6. 把基线（T2:157/72/0；T3:70/114/18/31）写进 CI 断言（DEFECT 不回升）
```

---

## 11. 复用基建（修正路径错误）

| 已有资产 | 用途 | 备注 |
|----------|------|------|
| `tests/convergence/param_provider.py` | 从权威 JSON + SeedContext 生成参数 | **需 N7 改用真实 ID 替代占位** |
| `tests/convergence/conftest.py`（`isolated_http_daemon` / `seed_workspace` / `qa_workspace`） | 隔离 daemon + 种子 workspace | **`_pick_bin` 须跨平台化（§12）** |
| `tests/test_http_daemon_release_acceptance.py` | 隔离 daemon 启动/等待/清理助手 | 同源复用 |
| `tests/conftest.py::_isolate_db_path` | autouse 隔离 DB 路径 | 不污染 `~/.callwarden` |
| `scripts/verify_route_matrix.py` | 路由矩阵门禁（7 道） | 随 `pytest tests/` 跑 ✓ |
| `scripts/check_client_purity.py` | 客户端纯净度硬门禁（0 违例） | 随 `pytest tests/` 跑 ✓ |
| **`tests/test_category_source.py`** | 分类完整性 + CLI↔MCP 同构校验 | ⚠️ **v4 §9 误写为 `scripts/test_category_source.py`，实际在 `tests/`** |

---

## 12. CI 改造清单（v5 落地关键）

### 12.1 主 `ci.yml`：构建 daemon + 跨平台

```yaml
# 在 "Build Rust extension" 之后新增：
- name: Build cw-daemon binary
  run: cargo build --release --manifest-path rust_ext/Cargo.toml --no-default-features --bin cw-daemon

# "Run tests" 步骤补充门禁参数：
- name: Run tests (with gates)
  run: |
    pytest tests/ -n auto --tb=short --maxfail=10 --timeout=300 \
      --cov=callwarden --cov-report=term-missing --cov-fail-under=60
    python scripts/check_skip_rate.py   # 自定义：skip_rate <= 5% 才 exit 0
```

### 12.2 修复 `_pick_bin` 跨平台（`tests/convergence/conftest.py:39-49`）

```python
import sys
_SUFFIX = ".exe" if sys.platform == "win32" else ""
_RELEASE_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", f"cw-daemon{_SUFFIX}")
_DEBUG_BIN   = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug",   f"cw-daemon{_SUFFIX}")
```

### 12.3 `rust-unit-test` 扩展模块

```yaml
# 逐步纳入 daemon:: 全模块（先 compat_native_handlers → dispatch → storage → http_server → snapshot → lease）
run: >-
  cargo test --manifest-path rust_ext/Cargo.toml
  --no-default-features --lib daemon::
```
> 注意：snapshot_state 等既有测试依赖进程单例/真实环境会挂起（ci.yml 注释已记），需先修测试隔离再扩面，避免 CI 雪崩。

### 12.4 新增门禁脚本（建议落 `scripts/`）
- `scripts/check_skip_rate.py`：解析 `pytest --report-log` 或 `junitxml`，计算 `skip/total`，>5% 则非零退出。
- `scripts/check_coverage.py`：封装 `--cov-fail-under`，输出未覆盖的关键模块清单。

### 12.5 性能门禁
- 在 `rust-unit-test` 或独立 job 跑 `rust_ext/tests/perf_daemon_baseline.py`，对比固化的基线 JSON，超阈值 FAIL。

---

## 13. 分阶段实施 backlog（映射到 `cw task` 树）

| Phase | 目标 | 关键交付 | 门禁影响 |
|-------|------|----------|----------|
| **P0 紧急** | 收敛套件在 CI 跑通 | ci.yml 构建 daemon；`_pick_bin` 跨平台；rust-unit-test 扩模块；`check_skip_rate.py` | 收敛 ERROR→0 |
| **P1** | 精确断言落地 | `seed_workspace` deep fixture；19 P0 CLI 断言；fail-soft/traceback 分项钉死；MCP 读工具精确断言 | T3 从"总量≤18"升级"分项可观测" |
| **P2** | 负向 + 故障注入 | N1 负向矩阵；N2 真实子进程 fail-closed | M4 端到端化 |
| **P3** | 安全 + 迁移 + 快照 | N3 lease 过期/attestation 撤销/会话隔离；N4 语义对比；N5 快照一致性/GC | 治理写面从 SKIP→正向测试 |
| **P4** | 性能 + 覆盖率 | N6 性能门禁；`--cov-fail-under` 逐步升阈值 | 防静默退化 |

> 每个 Phase 对应一个或多个 `cw task` 子卡，沿用项目 Planner/Executor/Reviewer/Adjudicator 四角色交接流；Phase 内"整类关闭"后才统一关闭（铁律 #2）。

---

## 14. 交付物清单（本目录）

| 文件 | 内容 |
|------|------|
| `TESTING_PLAN.md` | 本文件：v5 优化测试策略（含现状事实核查 + 七大维度 + CI 改造 + backlog） |
| `TEST_CASES.md` | 分级测试用例清单（84 CLI / 233 叶 / 243 MCP，P0/P1/P2，**新增实现状态列**） |
| `COVERAGE_AUDIT.md` | 覆盖矩阵 + **订正后的 skip 审计（447 站点，非 219）** + 收敛套件 CI 不可执行发现 |
| `BUILD_ENV.md` | 构建前置（Rust 工具链打通记录） |
| `gen_test_cases.py` | 可复现生成器（改权威源后重跑即可刷新 `TEST_CASES.md`） |

---

## 15. 废弃与迁移

以下旧资产因 fixture/分类口径与实仓不符，已停用（保留 `qa-test-audit-report.md` / `callwarden-test-plan-review.md` 作过程证据）：

`callwarden-test-io-spec.md`、`callwarden-test-taxonomy.md`、`callwarden-test-golden-spec.md`、
`callwarden-test-p0-spec.md`、`callwarden-test-decomposition.md`、`build_test_tree.py`、
`gen_io_spec.py`、`class_owners.json`。

> 这些旧文件位于工作会话目录，**尚未执行 `--apply`**，未污染 `cw` 任务树。

---

## 附：v4 → v5 一句话总结

> v4 把"路由可达 + 占位参数 + SKIP 计数"包装成了"100% 业务覆盖、精确断言、门禁化"；v5 先戳破这层包装（收敛套件在主 CI 实际零执行、Rust 单测仅跑 1 模块、精确断言未落地、skip 真实 447 非 219），再给出**可落地的 CI 改造 + 七大测试维度 + 分阶段 backlog**，把测试方案从"文档漂亮"升级为"代码可执行、断言可证伪、门禁可强制"。
