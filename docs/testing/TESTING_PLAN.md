# CallWarden 测试方案（v6 · 全景闭环与工程落地版）

> 版本：**v6**｜ 日期：2026-10-10 ｜ 适用仓库：`callwarden`
> 配套文档：
>
> - `TEST_CASES.md` —— 分级测试用例清单（由 `gen_test_cases.py` 从权威源生成）
> - `COVERAGE_AUDIT.md` —— 覆盖矩阵与 skip 审计
> - `BUILD_ENV.md` —— 构建前置（Rust 工具链如何打通，cw-daemon 二进制如何产出）
> - `gen_test_cases.py` —— 可复现生成器
>
> **事实基线声明**：本文所有数字均经 2026-10-10 实仓扫描核对，标注了证据来源。
> 凡未核实的断言一律写「待核实」，**不写估算冒充实测**。

---

## 0. 修订要点（v5 → v6 改了什么）

| # | v5 的问题 | v6 的修正 |
|---|---|---|
| 1 | 收敛套件 CI 断链、Rust 单测漏跑、skip 统计偏低的诊断正确 | **保留**，并补上 `_pick_bin` 现址核实（`conftest.py:40-49`） |
| 2 | 覆盖矩阵「20 类」从未建立，测试金字塔止于 L4 名称罗列 | 新增 §4 五层金字塔 + §6 N1–N8 专项矩阵，**每层给出可执行判据** |
| 3 | 4 角色治理、16 语言、三层存储 CAS 竞态均为「零正向覆盖」，未立项 | 新增 **G-Suite**（N3 深化）、**L-Matrix-16**（N8）、**S-Suite**（N5 深化）三套专项 |
| 4 | 18 项缺陷仅 `defects <= 18` 总量监控 | 改为**原子指纹钉死**（5 fail-soft / 3 traceback / 10 method_not_found 分项） |
| 5 | 4 套分裂 daemon harness 未立项 | 新增统一基建 `EphemeralDaemonFixture` 规格（§7） |
| 6 | 文档自称 v6 但内容仍是 v5 | 本次同步修正 `TEST_CASES.md` 尾注与 `gen_test_cases.py` 中的失真硬编码 |

> ⚠️ **v6 的自我修正声明**：本文上一轮修订中曾出现三处数字造假/口径错误，已于本次修正，
> 详见 §13「文档勘误」。保留此节以留痕，避免同类错误复现。

---

## 1. 覆盖现状事实核查（必读）

### 1.1 真实规模（2026-10-10 实测）

| 指标 | 真实值 | 证据 |
|---|---|---|
| 全仓 `def test_` 级函数 | **7133** | `grep -rn "^def test_\|    def test_\|^async def test_" tests/ --include=*.py \| wc -l` |
| 收敛套件（T1–T5/M1–M4 + 3 附加）测试函数 | **58**（占全仓 0.8%） | `tests/convergence/` 12 个测试文件 + 2 runner |
| Rust 单测函数 | **2091**（121 个含 `#[cfg(test)]` 的 `.rs` 文件） | `grep -rn "#\[test\]\|#\[tokio::test\]" rust_ext/src --include=*.rs \| wc -l`；口径为「`#[test]` + `#[tokio::test]` 属性数」，**v5 的 2073 口径未注明，本次已订正为 2091** |
| 含 skip 的测试文件数 | **131** | `grep -rl "pytest.skip(\|pytest.mark.skipif" tests/ --include=*.py \| wc -l` |
| 真实 skip 站点 | **447**（213 `pytest.skip(` + 234 `pytest.mark.skipif`） | 全仓 grep 复核一致，**该数字成立** |
| 权威 MCP 工具清单 | **243**（`src_tool_count` / `matrix_tool_count` / `matrix_total_tools_field` 三者一致，`only_in_source_code=0`） | `tests/convergence/fixtures/audit_mcp_inventory.json` |
| 权威 CLI 叶子 | **234**（已提取 233，skip 1 = `server`） | `tests/convergence/fixtures/cli_full_params.json` |

### 1.2 三个致命落差

**落差 A —— 旗舰收敛套件在主 CI 完全无法运行（最严重）。**

- `tests/convergence/conftest.py:40-49` `_pick_bin()` **硬编码** `cw-daemon.exe` / `debug/cw-daemon.exe`，两者均缺失即 `raise RuntimeError`。
- 主 `.github/workflows/ci.yml` 的 `test` job 跑在 `ubuntu-latest`，其构建步（`ci.yml:32`）仅执行 `python release/build.py --rust`（构建 Python 扩展 `.pyd`，**不构建 `cw-daemon` 二进制**），随后 `pytest tests/`（`ci.yml:82`）触发收敛套件。
- 结果：Linux 上 `_pick_bin` 抛 `RuntimeError` → `isolated_http_daemon` 夹具 setup 失败 → **T1/T2/M3/M4/T4 + `test_regression_http_tools` 全部 ERROR**；T3 因 `_http_daemon_available()` 探测失败（`test_t3_cli_full_invocation.py:20-36`）而 SKIP。
- `e2e-verify-linux-x86_64.yml` **有** `cargo build --release --bin cw-daemon`，但那是独立 workflow，**不在主 `ci.yml`**。
- **结论**：文档宣称的「243/234 全参数真实调用」在主 CI 中执行量为 **0**。

**落差 B —— Rust 单测 CI 几乎不跑。**

- `ci.yml:141-145` 的 `rust-unit-test` job 仅 `cargo test --no-default-features --lib daemon::compat_native_handlers`，注释自承「未纳入 daemon:: 全模块」。
- 2091 个 Rust 单测中，dispatch 路由、storage、daemon HTTP handler、snapshot、lease、attestation 等绝大多数 daemon 逻辑**无 CI 回归保护**。

**落差 C —— 「全参数真实调用」的参数大量是占位，断言只数总量。**

- `tests/convergence/param_provider.py` 的 `_resolve_by_name` 对未匹配参数返回确定性占位：`"seed"`、`"0"*64`（hash）、`"T-seed-..."` 等。
- `seed_workspace`（`conftest.py:286-401`）只建符号图谱，**不预建 task/lease/agent/snapshot**。依赖真实实体的工具被喂假 ID，结果多为 `EXPECTED_BUSINESS`（not_found），**不验证业务正确性**。
- T2 断言仅 `DEFECT==0 & PASS>=100 & 覆盖==243`（`test_t2_mcp_full_invocation.py:57-80`）；T3 仅断言 `defects <= 18` + `PASS+BUSINESS >= 150`（`test_t3_cli_full_invocation.py:37,90-114`）。

### 1.3 被 SKIP 的真实工具/命令（覆盖盲区）

- **T2 SKIP_TOOLS（15 个，`t2_mcp_runner.py:36-54`）**：`rotate_audit_signing_key`、`delete_workspace`、`remove_file`、`prune_external_symbols`、`gc_retention`、`clear_clones`、`task_rollback`、`register_attestation_revocation`、`assignment_revoke`、`task_apply`、`task_close`、`build_graph`、`import_git_history`、`build_directory`（清单实际 14 项，以代码为准）。即**治理写面与破坏性写面主要靠 SKIP**，无正向正确性断言。
- **T3 SKIP_CMD_PREFIXES（`t3_cli_runner.py:36-55`）**：30 个前缀，覆盖 `workspace delete/register/set`、`gc *`、`task apply/close/rollback/reopen/revert`、`clone clear/detect`、`fts rebuild`、`audit rotate`、`assignment *`、`rule *`、`refresh`、`* import`、`server`、`watch`、`daemon *`。

---

## 2. 测试哲学（三条铁律 + 三条补充）

| # | 铁律 | 含义 | 违反后果 |
|---|---|---|---|
| 1 | **fail-closed** | 任何异常必须显式失败或返回结构化错误，**绝不静默降级 / 绝不本地兜底执行** | daemon 不可达 → `DaemonUnavailableError`；rc=0 但返回空/垃圾 → 判 FAIL |
| 2 | **整类关闭** | 一个分类（21 CLI 类 / 17 MCP 类）内全部用例通过后才整体关闭该类的 `cw task` 节点 | 不允许「挑几个过了就关类」 |
| 3 | **门禁不降级** | 已知缺陷基线只许减少不许回升；新增 skip 必须带原因且计入 `skip_rate` | DEFECT / fail-soft / traceback 数只能降 |

**补充铁律**：

| # | 铁律 | 理由 |
|---|---|---|
| 4 | **可执行优先** | 任何「覆盖声明」必须有对应的、能在主 CI 跑通的测试代码；CI 跑不起来的套件不计入覆盖 |
| 5 | **断言可证伪** | 「全参数真实调用」必须配合精确断言，仅 `rc==0` 或「调用可达」不算覆盖 |
| 6 | **跨平台无硬编码** | 测试基建（二进制路径、管道/端口、路径分隔）必须跨 Windows/Linux/macOS，禁止 `.exe` 写死 |
| 7 | **数字须可复算**（v6 新增） | 文档中每个比率、总数必须给出**分子/分母/口径**；分项估算之和若不等于总数，须显式标注「差额未归因」 |
| 8 | **缺失即证据**（v6 新增） | 本地未安装 / 未授权 / 扫描截断的维度一律标「未实测」，**不得写成通过或达标** |

---

## 3. 被测系统范围（两轴，措辞修正）

| 轴 | 真相源 | 规模 |
|---|---|---|
| **CLI 轴** | `cli/categories.py` | **21 类 / 84 顶层命令**（[18]-[21] 为 CLI-only） |
| | `tests/convergence/fixtures/cli_full_params.json` | **234 个叶子**（提取 233 + 跳过 1 = `server`） |
| **MCP 轴** | `server/tools/_categories.py` | **17 类 / 243 工具** |
| | `tests/convergence/fixtures/audit_mcp_inventory.json` | 243 工具清单，三处总数一致、`only_in_source_code=0` |

**诚实区分两种「覆盖」**：

- **表面可达覆盖**（当前收敛套件做到的）：每个工具/命令的路由存在、参数可被构造、调用能往返。
- **功能正确性覆盖**（v6 目标）：对给定真实种子，返回结构/字段/集合/数量可被精确断言；负向输入返回结构化错误而非崩溃/空结果。

> 当前仅达成前者。

**种子事实**（用于精确断言）：
- `tests/convergence/seed_sample/calc.py`：`add(a,b)` / `multiply(a,b)`，且 **multiply → add 存在真实调用边**
- `tests/convergence/seed_sample/service.ts`：`MemoryRepo.find` / `MemoryRepo.save`、`Service` 类

---

## 4. 测试架构：五层金字塔（L0–L4）

```
       ▲
      ╱ ╲     L4 专项层  N1–N8（混沌 / 安全 Lease / 16 语言 / CAS 并发 / 10M 压测 …）
     ╱───╲    ────────────────────────────────────────────────────────────────
    ╱     ╲   L3 多 Agent 协同与系统集成  T4 多租户隔离 / T5 LLM 意图 / G-Suite 四角色
   ╱───────╲  ────────────────────────────────────────────────────────────────
  ╱         ╲ L2 跨端矩阵与架构不变量  M1 路由四端一致 / M2 纯 Client 审计 / M4 Fail-Closed
 ╱───────────╲────────────────────────────────────────────────────────────────
╱             ╲L1 业务全量调用与语义断言  T2 243 MCP / T3 234 CLI 叶子
──────────────  ───────────────────────────────────────────────────────────────
L0 统一基建层  EphemeralDaemonFixture 瞬态进程 / L-Matrix-16 Golden Fixtures
```

| 层 | 现有资产 | v6 目标 | 判据 |
|---|---|---|---|
| **L0** | `conftest.py` / `_w3_harness.py` / `test_http_daemon_release_acceptance.py` / `conftest.RouteStub`（**4 套分裂**） | 统一为 `EphemeralDaemonFixture`（§7） | 四套 harness 收敛为一套，其余转调 |
| **L1** | T2（157 PASS/72 BUSINESS/0 DEFECT）、T3（70/114/18/31） | 语义断言落地 | P0 精确断言从 0 例 → 有实现；DEFECT 分项可观测 |
| **L2** | M1 路由矩阵、M2 纯 client、M3 并发写、M4 fail-closed | M4 端到端化 | M4 从 monkeypatch → 真实子进程 |
| **L3** | T4 隔离、T5 LLM（无 key 时 skip） | **G-Suite 补齐四角色闭环** | 越权阻断率 100% |
| **L4** | 仅 `perf_daemon_baseline.py`（未接 CI） | N1–N8 全部专项 | 每项有独立通过判据 |

---

## 5. M1 路由矩阵口径订正（v6 勘误）

> **v5/v6 初稿均误写「239/239」，实测为 243/243。**

| 证据 | 值 |
|---|---|
| `test_m1_route_matrix.py:30` | `_EXPECTED_TOTAL = 243` |
| `deliverables/software-company/tool_migration_matrix.json` | `total_tools = 243`，`len(tools) = 243` |
| `audit_mcp_inventory.json` | `src_tool_count = matrix_tool_count = matrix_total_tools_field = 243`，`consistent = True` |

**遗留技术债**：`test_m1_route_matrix.py:75` 的函数名仍为 `test_matrix_has_239_tools`，名实不符，易误导后续读者。**建议随 Phase 1 一并重命名**（本文档不改测试代码）。

---

## 6. N1–N8 专项矩阵（v6 核心）

| 编号 | 专项 | 覆盖的空缺 | 关键判据 | 规模预估 |
|---|---|---|---|---|
| **N1** | 负向 / 边界矩阵 | 每类工具的非法输入、空符号、未 build 空间、超大输入 | 返回结构化 `error`/`code`；**禁止 `rc==0` 配空 payload**（铁律 #1） | 中 |
| **N2** | 故障注入 / 混沌 | M4 仅进程内 monkeypatch，未用真实子进程 | 真实 `python cw.py` 子进程在 daemon kill/超时/500/畸形 JSON 下 fail-closed | 中 |
| **N3** | 安全：Lease / Identity / Attestation | 治理写面零正向测试 | ① lease 过期拒写；② 孤儿 lease 心跳回收；③ attestation 撤销即时失效；④ **G-Suite 越权阻断**；⑤ 任务树级联关闭 | 大 |
| **N4** | 迁移语义对比 | 只验证路由存在，无 native vs compat 输出一致性 | 双跑对比结构同构、关键字段值一致、错误码一致 | 中 |
| **N5** | 三层存储 CAS 一致性 | CAS 写入与 GC 锁互斥无极端竞态测试 | 高频 CAS 写入 × 并发 `gc-cas` 无误删；`snapshot.publish` 1000 QPS 无锁切换不撕裂；WAL 崩溃自愈；schema 迁移幂等 | 大 |
| **N6** | 性能回归门禁 | `perf_daemon_baseline.py` 未接 CI | P95 / 吞吐不低于固化基线，超阈 FAIL | 小 |
| **N7** | 精确断言落地（deep fixture） | 19 个 P0 CLI 精确断言 0 例落地 | `seed_workspace` 预建 task/lease/agent/snapshot；断言从文字变 `assert` | 大 |
| **N8** | L-Matrix-16 多语言矩阵 | `seed_sample` 仅 `calc.py` + `service.ts`（**2/16**），14 种语言在常规回归中为盲区 | 16 语言 × 3 类样本（valid / error 容错 / unicode ident）无 panic 且正确建边 | 大 |

### 6.1 G-Suite：四角色治理全链路（N3 深化）

- **越权防御**：同一 `agent_id`/`session_id` 既当 Executor 又当 Reviewer 提交 PASS → 必须 `E_ROLE_INDEPENDENCE_VIOLATION`。
- **租约防脑裂**：Agent A 持 lease 挂起 → B 抢占（fencing counter 递增）→ A 唤醒后写操作必须 `E_LEASE_FENCED`。
- **身份吊销即时失效**：`register_attestation_revocation` 后该 identity 后续写操作立即阻断。
- **任务树级联关闭**：末个子任务完成时父任务原子关闭；越权直接关父任务被拒。

### 6.2 L-Matrix-16 样本规格（N8）

`rust_ext/src/languages/` 实测含 **15 个** grammar 模块（`cpp.rs` / `csharp.rs` / `elixir.rs` / `go.rs` / `hcl.rs` / `java.rs` / `javascript.rs` / `kotlin.rs` / `php.rs` / `python.rs` / `ruby.rs` / `rust.rs` / `scala.rs` / `swift.rs` / `typescript.rs`），即 **JS 与 TS 分列** → 16 种语言口径成立。

每种语言三套样本：`syntax_valid.*`（正常定义+调用）、`syntax_error.*`（残缺代码，验 tree-sitter 容错不崩）、`unicode_ident.*`（非 ASCII 符号 + NFC 规范化安全）。

### 6.3 S-Suite：三层存储与 CAS（N5 深化）

- CAS 写入与 Mark-Sweep GC 的 `fs2` 跨平台文件锁互斥（**注意**：fs2 排他锁连读都阻，`os error 33`；`try_lock` 失败才是持锁权威信号）
- Snapshot 基于 `arc-swap` 的无锁原子发布，读线程 1000 QPS 期间不撕裂
- SQLite WAL 并发写 + 检查点 + 断电式杀进程后自愈
- Schema 迁移幂等性（升级到当前 v50 后表结构 checksum 一致）

---

## 7. 统一守护进程测试基建（`EphemeralDaemonFixture`）

**要解决的真问题**：`PIPE_IN_USE` 类 skip、Named Pipe 持续占用、孤儿进程残留、12487 端口争用。

| 要求 | 规格 | 依据 |
|---|---|---|
| **动态端口** | 绑定 0 端口或在 20000–30000 探测可用端口 | 终结端口争用 |
| **目录沙箱** | 重定向 `USERPROFILE` / `HOME` 至临时目录 | 不污染 `~/.callwarden` |
| **跨平台二进制探测** | 按 `sys.platform` 解析 `cw-daemon` / `cw-daemon.exe`，多候选路径探测 | 修 `conftest.py:40-49` 硬编码 |
| **进程树自毁（Zero Leakage）** | Windows 挂 Win32 Job Object（`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`）；Unix 用 `os.setsid` + 进程组 `SIGKILL` | 保证父进程退出时 100% 回收 |
| **统一入口** | 现有 4 套 harness（`conftest.py` / `_w3_harness.py` / `test_http_daemon_release_acceptance.py` / `conftest.RouteStub`）全部转调此 fixture | 消除分裂 |

---

## 8. 18 项已知缺陷的原子指纹钉死

**废弃** `assert len(defects) <= 18`（总量监控存在「新掩盖旧」的假阳性风险）。

| 缺陷类 | 数量 | 代表 | 钉死方式 |
|---|---|---|---|
| rc=0 掩盖真 bug（fail-soft 吞异常） | 5 | `call-chain`, `coupled-fns`, `largest-fns`, `rule applicable`, `status` | 断言 `rc==0` 时 payload 非空，空结果 = FAIL |
| traceback | 3 | `collab publish`, `daemon publish`, `daemon snapshot-stats` | stdout/stderr 不含 `Traceback (most recent call last)` |
| method_not_found（CLI→daemon compat RPC） | 10 | 由探测得出 | 白名单指纹，**出现第 11 个即刻阻断合入** |

> 数量口径：5 + 3 + 10 = 18，与基线 18 自洽。**分项名单需在 Phase 1 从 `report["results"]` 落盘固化**，当前基线来自首轮（生产 daemon `b495919`），**换版本后须重新探测**。

---

## 9. 447 处 Skip 的解锁行动矩阵

> 完整归因与算术修正见 `COVERAGE_AUDIT.md`。本节仅给行动口径。

| 家族 | 数量 | 解锁动作 | 状态 |
|---|---|---|---|
| `callwarden_core 未安装` / Rust 扩展未构建 | **39** | CI 装好扩展（36 处集中在 `tests/parser_contract/`） | 🟢 构建可解 |
| BINARY_MISSING / CI 执行断链 | ~50 | `ci.yml` 增 `cargo build --bin cw-daemon`；跨平台二进制探针 | 🔴 P0 |
| PIPE_IN_USE / 管道冲突 | ~45 | `EphemeralDaemonFixture` 统一瞬态隔离 | 🟡 P1 |
| LANG_FIXTURE / 语言缺失 | ~22 | L-Matrix-16 补齐 | 🟡 P1 |
| PLATFORM_INHERENT（Windows 无 AF_UNIX / 无 memfd_create、Linux 无 Named Pipe、Docker 专有） | ~55 | 标 `@pytest.mark.platform_specific`，从有效 skip 率中**单列而非扣除分母** | ⚪ 合理保留 |
| DATA_MISSING / 业务实体缺失 | ~14 | Deep Fixture 预建实体 | 🟡 P1 |
| CLI_DAEMON_UNAVAILABLE | ~11 | Daemon 统一自举 | 🟢 随构建解除 |
| CARGO_MISSING | ~8 | 统一使用已编译产物，不在测试内实时 `cargo build` | 🟢 环境可解 |
| DOC_MISSING | ~3 | 补文档或修断链路径 | 🟢 文档可解 |
| **未归因差额** | **239** | **逐文件归因（见 COVERAGE_AUDIT §2.1）** | ⬜ 待补 |

### 9.1 skip 率目标（算术已修正）

```
当前真实基线：447 / 7133 = 6.27%（超 ≤5% 红线）

P0 解除 BINARY_MISSING  ~50  → 397/7133 = 5.57%
P1 解除 PIPE_IN_USE 等    ~64  → 333/7133 = 4.67%  ✅ 达标
P2 解除 LANG/DATA         ~36  → 297/7133 = 4.16%
P3 归因并解除剩余         ~239 → 需逐项核实
```

> **v6 勘误**：上一稿宣称「扣除平台固有 55 处 → 有效 skip 率 < 1.8%」。该结论**算术不成立**——
> `(447−55)/7133 = 5.5%`，分子分母同减一个数不可能使比率降到 1.8%。若要真达 1.8%，分子须 ≤ 128，
> 即**必须真解 319 处**。平台固有 skip 应**单列统计**（`platform_specific` 标记），
> 不允许通过「扣除分项」伪造达标。

---

## 10. CI/CD 分级流水线与门禁

### 10.1 Tier 0–5 分级

| Tier | 内容 | 目标时长 | 触发 |
|---|---|---|---|
| **T0** | lint + 客户端纯度（M2 静态） | < 2 min | push |
| **T1** | 单元 + 快速子集（离线） | < 5 min | push |
| **T2** | 全量 `pytest tests/ -n auto` + 门禁脚本 | < 20 min | push / PR |
| **T3** | 收敛套件（需 daemon 二进制） | < 15 min | PR |
| **T4** | `cargo test --lib daemon::` 全模块 | < 30 min | PR |
| **T5** | 性能基线 + 混沌专项 | 定时 | nightly |

### 10.2 主 `ci.yml` 改造（**注意 `--no-default-features` 不可省**）

```yaml
# 1. 补齐 daemon 可执行程序（消除主 CI 收敛套件全崩断链）
#    --no-default-features 必须保留：PyO3 需关闭 extension-module 才能链接 libpython，
#    否则 CI 链接阶段失败（本项目已多次踩坑，见 --no-default-features 在 ci.yml:144 的既有用法）
- name: Build cw-daemon binary and Python extension
  run: |
    python release/build.py --rust
    cargo build --release --manifest-path rust_ext/Cargo.toml \
      --no-default-features --bin cw-daemon
  shell: bash

# 2. 补齐 Rust 全模块单测（解除仅跑 compat 模块的盲区）
#    注意：snapshot_state 等既有测试依赖进程单例/真实环境，直接扩面会 CI 雪崩，
#    需先修测试隔离，再逐步纳入（建议顺序：compat → dispatch → storage → http_server → lease）
- name: Run Full Rust Unit Tests
  run: |
    cargo test --manifest-path rust_ext/Cargo.toml \
      --no-default-features --lib daemon:: \
      -- --skip snapshot_state::tests::test_heavy_realworld
  shell: bash

# 3. 全量测试 + 覆盖率 + skip 率门禁
- name: Run Pytest with Strict Gates
  run: |
    pytest tests/ -n auto --tb=short --maxfail=10 --timeout=300 \
      --timeout-method=thread \
      --cov=callwarden --cov-report=term-missing --cov-report=xml \
      --junitxml=report.xml
  shell: bash

# 4. 门禁判定（脚本需新建，见下）
- name: Enforce Gates
  run: |
    python scripts/check_ci_gates.py --junit report.xml \
      --max-skip-rate 0.05 --min-cov 60 \
      --t2-defect-baseline 0 --t3-defect-baseline 18
  shell: bash
```

### 10.3 跨平台修复 `_pick_bin`（`tests/convergence/conftest.py:40-49`）

```python
import sys
_SUFFIX = ".exe" if sys.platform == "win32" else ""
_TARGET = os.path.join(_REPO_ROOT, "rust_ext", "target")
_RELEASE_BIN = os.path.join(_TARGET, "release", f"cw-daemon{_SUFFIX}")
_DEBUG_BIN = os.path.join(_TARGET, "debug", f"cw-daemon{_SUFFIX}")
```

### 10.4 门禁脚本（**当前均不存在，须新建**）

| 脚本 | 状态 | 职责 |
|---|---|---|
| `scripts/check_ci_gates.py` | ❌ **不存在** | 汇总 T2/T3/M1 分项门禁 + skip 率 + 覆盖率，单点裁决 |
| `scripts/check_skip_rate.py` | ❌ **不存在** | 解析 junitxml 计算 `skip/total`，>5% 非零退出 |
| `scripts/verify_route_matrix.py` | ✅ 存在 | 路由矩阵 7 道门禁 |
| `scripts/check_client_purity.py` | ✅ 存在 | 客户端纯度 0 违例 |

> ⚠️ 引用不存在的脚本是上一稿的缺陷之一：门禁清单必须区分「已有」与「待建」，
> 否则 CI 改造照抄即失败。

---

## 11. 分级法（P0 / P1 / P2 + 实现状态）

| 优先级 | 含义 | 执行环境 | 放行要求 |
|---|---|---|---|
| **P0** | 核心只读查询（精确断言）+ 已知缺陷钉死 | 种子 workspace（已 build_graph + deep fixture） | **必须通过**；DEFECT/fail-soft/traceback 不许回升 |
| **P1** | 常规契约 / 写路径 | 隔离 daemon + 临时 DB 实例 | 隔离环境通过即可合入 |
| **P2** | 破坏性 / 重操作 | **专用隔离沙箱**，不进主回归 | 单独环境验证，禁止污染生产 daemon |

**实现状态（v6 复核）**：

| 项 | 状态 | 证据 |
|---|---|---|
| P0 CLI 19 例精确断言 | ❌ **0 例落地** | `test_t3_cli_full_invocation.py` 仅总量断言 |
| T2/T3 分项钉死 | ❌ 未实现 | 仅 `defects <= 18` |
| M1 路由矩阵 | ✅ 已落地 | `_EXPECTED_TOTAL = 243`，但函数名遗留 239 |
| M2 纯 client 审计 | ✅ 已落地 | `scripts/check_client_purity.py` |
| M4 fail-closed | ⚠️ 仅入口层 | `test_m4_cli_fail_closed.py:57-121` monkeypatch，非端到端 |
| 18 缺陷分项指纹 | ❌ 未实现 | 仅总量断言 |
| 统一 daemon fixture | ❌ 未实现 | 4 套 harness 并存 |

---

## 12. 分阶段实施 backlog

| Phase | 目标 | 关键交付 | 门禁影响 |
|---|---|---|---|
| **P0 紧急** | 收敛套件在 CI 跑通 | `ci.yml` 增 `cargo build --no-default-features --bin cw-daemon`；`_pick_bin` 跨平台；`check_ci_gates.py` / `check_skip_rate.py` 新建；M1 函数名 239→243 订正 | 收敛 ERROR → 0 |
| **P1** | 精确断言 + 缺陷分项 | `seed_workspace` deep fixture；19 P0 CLI 断言；18 缺陷原子指纹；统一 `EphemeralDaemonFixture` 收敛 4 套 harness | T3 从「总量≤18」升级「分项可观测」 |
| **P2** | 负向 + 故障注入 | N1 负向矩阵；N2 真实子进程 fail-closed | M4 端到端化 |
| **P3** | 安全 + 存储一致性 | N3（含 G-Suite）；N4 语义对比；N5 CAS/GC/竞态 | 治理写面 SKIP → 正向测试 |
| **P4** | 多语言 + 性能 | N8 L-Matrix-16；N6 性能门禁 | 16 语言纳入回归 |
| **P5** | skip 归因收口 | 逐文件归因 239 处差额；平台 skip 单列标记 | 有效 skip 率真实达标 |

> 每个 Phase 对应一个或多个 `cw task` 子卡，沿用四角色交接流；Phase 内「整类关闭」后才统一关闭（铁律 #2）。

---

## 13. 文档勘误记录（留痕）

| 稿次 | 错误陈述 | 实测 | 处置 |
|---|---|---|---|
| v4 | skip 219 处 | 447 处 | v5 已修 |
| v5 | Rust 单测 2073 | 2091（口径：`#[test]`+`#[tokio::test]`） | v6 已修并注明口径 |
| v5 / v6初稿 | M1 路由 **239/239** | **243/243** | §5 已订正 |
| v6初稿 | 「扣除平台固有 55 处 → 有效 skip 率 < 1.8%」 | `(447−55)/7133 = 5.5%`，算术不成立 | §9.1 已拆穿并重算 |
| v6初稿 | 「447 处已完成四类归因」 | 已归因 208，**差额 239 未归因** | `COVERAGE_AUDIT.md` §2.1 已标注 |
| v6初稿 | CI 命令缺 `--no-default-features` | PyO3 需该 flag 才能链接 libpython | §10.2 已补 |
| v6初稿 | 引用 `check_ci_gates.py` / `check_skip_rate.py` | 两脚本均不存在 | §10.4 已标「待建」 |

> 保留此表以留痕。**教训**：凡未跑过命令的数字，不得写入方案作为门禁依据。

---

## 14. 交付物清单

| 文件 | 内容 |
|---|---|
| `TESTING_PLAN.md` | 本文件：v6 测试策略（现状核查 + 五层金字塔 + N1–N8 + CI 改造 + backlog + 勘误） |
| `TEST_CASES.md` | 分级测试用例清单（84 CLI / 233 叶 / 243 MCP，P0/P1/P2 + 实现状态列） |
| `COVERAGE_AUDIT.md` | 覆盖矩阵 + skip 审计（447 站点，含**未归因差额标注**） |
| `BUILD_ENV.md` | 构建前置（Rust 工具链打通记录） |
| `gen_test_cases.py` | 可复现生成器（改权威源后重跑刷新 `TEST_CASES.md`） |
| `.bak_v5/` | v5 原件备份（本次修正前快照，可回溯） |

---

## 15. 门禁总表

| 门禁 | 指标 | 阈值 | 当前基线 | CI 强制 |
|---|---|---|---|---|
| 收敛套件可执行 | T1–T4/M1–M4 在 Linux CI 跑通 | 0 ERROR | ❌ 全 ERROR | 待建 |
| 主回归 skip 率 | `skip / total` | ≤ 5% | 447/7133 = **6.27%**（超阈） | 待建 |
| 行覆盖率 | `pytest --cov` | ≥ 60%（逐步升） | 无门禁 | 待建 |
| Rust 单测 | `cargo test` 覆盖模块 | `daemon::` 全模块 | 仅 1 模块 | 待建 |
| T2（MCP） | DEFECT / PASS / 覆盖 | `DEFECT==0` & `PASS>=100` & `覆盖==243` | 157/72/0 | 已有，须 CI 真跑 |
| T3（CLI） | DEFECT 基线 | 不回升（基线 18） | 70/114/18/31 | 已有，须 CI 真跑 |
| T3 分项钉死 | 5 fail-soft / 3 traceback / 10 method_not_found | 各自不新增 | 仅总量钉死 | **Phase 1** |
| M1 | 路由矩阵 | **243/243** | 243/243 | 随 `pytest tests/` 跑 ✓ |
| M2 | 纯 client | 0 违例 | 0 | ✓ |
| M3/M4/T4/T5 | 并发/fail-closed/隔离 | 全部通过 | — | 须 CI 真跑 |
| N6 性能 | P95 / 吞吐 | ≥ 基线阈值 | 无门禁 | Phase 4 |

> 未达 `skip_rate ≤ 5%` 与收敛套件可执行前，**不准宣称「通过」**。
> 门禁脚本在 `check_ci_gates.py` 建成前，上表「待建」项均为**文档约定而非可执行约束**。

---

## 附：v4 → v6 一句话总结

> v4 把「路由可达 + 占位参数 + SKIP 计数」包装成「100% 业务覆盖、精确断言、门禁化」；
> v5 戳破这层包装（CI 零执行、Rust 单测仅 1 模块、精确断言未落地、skip 447 非 219）；
> v6 给出五层金字塔 + N1–N8 专项 + 统一瞬态基建 + 缺陷原子钉死 + CI 分级流水线，
> **并同时勘误自身的算术与口径错误**——把「数字可复算」立为铁律 #7，避免方案本身成为新的假阳性来源。
