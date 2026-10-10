# 覆盖审计与 Skip 分析（CallWarden 测试 · v6 对应版）

> 配套文档：`TESTING_PLAN.md`（v6 全景闭环与工程落地版）｜ `TEST_CASES.md` ｜ `BUILD_ENV.md`
> 统计基准：2026-10-10 实仓代码与测试用例全量扫描

---

## 1. 核心现状与真实覆盖审计（v6 权威核准）

- **全仓 Skip 真实站点统计**：**`pytest.skip(` 213 处 + `pytest.mark.skipif` 234 处 = 447 处**（全仓 grep 严格去重与分类）。
  - *历史演进说明*：v4 仅粗略统计 219 处直接 skip；v5 纠正了遗漏的 234 处 skipif；v6 深入各个文件完成四类归因与解锁路径设计。
- **主 CI 核心套件执行断裂确认**：
  - `tests/convergence/conftest.py:39-49` 中的 `_pick_bin()` 仅检索 `cw-daemon.exe`；
  - 主 `.github/workflows/ci.yml` 的 `test` job（Linux 环境）仅执行 `python release/build.py --rust`（仅编译 Python 扩展，不编译 `cw-daemon` 二进制），导致收敛套件（T1–T5 / M1–M4）在 CI 中全部触发 `RuntimeError` 异常退出；
  - 所谓的"243 MCP 工具与 234 CLI 命令全参数真实调用覆盖"，在主 CI 环境中真实执行量为 **0**。
- **真实覆盖深度定性**：
  - 收敛套件目前主要完成**表面路由可达性校验（Surface Reachability）**；
  - 核心只读工具未在测试代码层落实集合与图结构断言，依赖大量占位参数（`param_provider.py`）；
  - 真实的业务功能正确性目前依赖存量 7133 个单测与集成测试文件保障。

---

## 2. 447 处 Skip 家族剖析与解锁路径

### 2.0 实测分布（2026-10-10 全量扫描）

| 维度 | 实测值 | 证据 |
|---|---|---|
| skip 站点总数 | **447** | 213 `pytest.skip(` + 234 `pytest.mark.skipif` |
| 含 skip 的测试文件数 | **131** | `grep -rl` 计数 |
| 目录分布（Top） | **`tests/parser_contract/` 44 处**，其余分散在 130 个文件 | 按目录聚合 |
| 原因分布（Top） | **`callwarden_core 未安装` 36 处**、`numpy 不可用` 6、`git 不可用` 4、`callwarden_core Rust 扩展未构建` 3 | 提取 `reason=` 字面量聚合 |

> **最大单一家族被上一稿遗漏**：`callwarden_core 未安装` + `Rust 扩展未构建` 合计 **39 处**，
> 且 36 处集中在 `tests/parser_contract/`。这批**不是**平台固有，而是**CI 构建链未完成**，
> 属构建可解——上一稿把它归入「平台固有合理保留」是错的。

### 2.1 已归因家族与解锁路径

| Skip 家族 | 数量估算 | 触发根因 | 解锁动作 | 状态 |
|---|---|---|---|---|
| **EXT_MISSING / Python 扩展未构建** | **39**（实测） | `callwarden_core 未安装`(36) + `Rust 扩展未构建`(3)，集中于 `tests/parser_contract/` | CI 装好 `callwarden_core` 扩展；测试前置显式检测并 fail-fast 而非静默 skip | 🟢 **P0 构建可解** |
| **BINARY_MISSING / CI 执行断链** | ~50 | `cw-daemon` 二进制未编译；`_pick_bin` 硬编码 `.exe`（`conftest.py:40-49`） | 1. `ci.yml` 增 `cargo build --no-default-features --bin cw-daemon`<br>2. 跨平台动态二进制探针 | 🔴 **P0（阻塞主 CI）** |
| **PIPE_IN_USE / 管道冲突** | ~45 | 4 套 harness 并存；竞争默认 Named Pipe `\\.\pipe\callwarden-*` 或进程残留 | 落地 `EphemeralDaemonFixture`，动态端口 + Job Object 进程树清理 | 🟡 P1 |
| **LANG_FIXTURE / 语言缺失** | ~22 | `seed_sample/` 仅 `calc.py` + `service.ts`，缺其余 14 种语言 | 建立 `L-Matrix-16` Golden Fixtures | 🟡 P1 |
| **DATA_MISSING / 业务实体缺失** | ~14 | 缺多 Workspace、clone_group、Semgrep findings 等 | Deep Fixture 预建真实 Task/Lease/Audit | 🟡 P1 |
| **CLI_DAEMON_UNAVAILABLE** | ~11 | CLI 单测未拉起测试 daemon | Daemon 统一自举后消除 | 🟢 随构建解除 |
| **CARGO_MISSING** | ~8 | 测试内实时 `cargo build`，PATH 未含 cargo | 统一使用已编译产物，不在测试内构建 | 🟢 环境可解 |
| **NUMPY_MISSING** | ~6（实测） | Python baseline 依赖 numpy 不可用 | CI 装齐 dev 依赖 | 🟢 环境可解 |
| **GIT_MISSING** | ~4（实测） | git 不可用 | CI 预装 git | 🟢 环境可解 |
| **DOC_MISSING** | ~3 | `callwarden_USER_GUIDE.md` 等文档缺失 | 补文档或修断链路径 | 🟢 文档可解 |
| **PLATFORM_INHERENT** | ~55 | Windows 无 `AF_UNIX` / `memfd_create`；Linux 无 Named Pipe / SDDL；Docker 专有 | 标 `@pytest.mark.platform_specific`，**单列统计，不从分母扣除** | ⚪ 合理保留 |
| **⬜ 未归因差额** | **239（54%）** | **尚未逐文件归因** | **P5 逐文件归因** | ⬜ **待补** |

> ⚠️ **算术校验**：上表已归因合计 = 39+50+45+22+14+11+8+6+4+3+55 = **257**；
> 447 − 257 = **190**，与「239」不等的部分源于各项为**估算**（标 `~`）而非精确计数。
> **诚实表述**：精确归因仅 39 + 6 + 4 + 3 = **52 处**（占 11.6%）有实测支撑，
> 其余 195 处为估算，合计估算覆盖 257 处，**仍有 190 处完全未归因**。
> P5 的任务就是把这 190 处落到实测。

---

## 3. `skip_rate` 门禁与收敛路径

### 3.1 真实基线核对
- **分母**：全仓 `def test_` 测试函数共 **7133** 个；
- **分子**：真实 Skip 站点共 **447** 处；
- **当前真实 Skip 率**：**447 / 7133 ≈ 6.27%**（已超过 `≤ 5%` 的质量红线）。

### 3.2 阶段压降目标（算术已修正）

```
当前基线 (6.27%)
   │
   ├─► [Phase 0] 解除 EXT_MISSING (39) + BINARY_MISSING (~50) ──► 358/7133 = 5.02%
   │
   ├─► [Phase 1] 解除 PIPE_IN_USE 等 (~64) ────────────────────► 294/7133 = 4.12%  ✅ 达标
   │
   ├─► [Phase 2/3] 解除 LANG/DATA fixture (~36) ──────────────► 258/7133 = 3.62%
   │
   ├─► [Phase 4] 解除 NUMPY/GIT/DOC 环境类 (~13) ──────────────► 245/7133 = 3.44%
   │
   └─► [Phase 5] 归因并处理剩余 ~190 处 ─────────────────────► 逐项核实后重算
```

> **v6 勘误**：上一稿宣称「扣除平台固有 ~55 处 → 实际有效 skip 率 < 1.8%」。该结论**算术不成立**——
> `(447−55)/7133 = 5.49%`；分子分母同减一个数不可能把比率降到 1.8%。
> 要真达 1.8%，分子须 ≤ 128，即**必须真解 319 处**。
> **正确做法**：平台固有 skip 单列（`platform_specific` 标记），
> **有效 skip 率 = (总 skip − 平台固有 skip) / (总用例数 − 平台固有用例数)**，
> 两个分项都要实测，不允许靠「扣除」伪造达标。

---

## 4. 全景覆盖矩阵（测试套件 ↔ 架构能力表面）

| 套件标识 | 架构层级 | 覆盖表面与核心职责 | v6 预期基线与断言标准 |
|---|---|---|---|
| **L0: T1** | 基建层 | 种子 Workspace 自举与参数装配（`SeedContext`） | 必须 100% PASS；预建真实 Task/Lease/Snapshot 实体 |
| **L1: T2** | 业务层 (MCP) | 243 个 MCP 工具真实调用（覆盖 17 个同构分类） | `DEFECT == 0`，`PASS >= 100`（代码实测口径），核心只读工具精准图断言 |
| **L1: T3** | 业务层 (CLI) | 234 个 CLI 叶子命令真实调用（覆盖 21 个命令分类） | 18 项已知缺陷原子锁定，禁止新增任何未知缺陷；P0 语义断言 |
| **L2: M1** | 不变量 | 路由四端一致性（矩阵 ↔ dispatch ↔ http_server ↔ compat） | **243/243** 100% 通过（v6 订正：上一稿误写 239）；无未暴露隐式方法 |
| **L2: M2** | 不变量 | Python Client 纯度审计（`cli/` 零直接依赖数据库引擎） | 0 违规，严禁任何直接 SQL/本地读写降级 |
| **L2: M3** | 协同层 | 多 Agent 并发写入与单调 Lease 互斥 | 无数据丢失，无写冲突脏写，单调 Fencing 计数生效 |
| **L2: M4** | 鲁棒层 | 全链路 Fail-Closed（真实子进程故障注入） | Daemon 不可达时统一输出结构化错误，严禁本地兜底 |
| **L3: T4** | 协同层 | 多 Workspace 隔离与租户状态互不污染 | 工作区数据强隔离，快照发布彼此独立 |
| **L3: G-Suite** | 治理层 | 4 角色编排、Task Tree 级联完成、Attestation 吊销 | 越权阻断率 100%；子任务完成自动原子级联推进父任务 |
| **L4: N1-N8** | 专项层 | 负向输入、混沌注入、CAS 并发写入/GC、16 语言矩阵、性能微基准 | 16 语言无崩溃；极端并发零损坏；P95 延迟不退化 |

> **门槛冲突待裁决（T2）**：本文 §4 上一稿写 `PASS >= 180`，而
> `test_t2_mcp_full_invocation.py` 实测门禁为 `PASS >= 100`，实测基线为 **157 PASS**。
> 三方不一致。**本文订正为 `PASS >= 100`（与代码一致）**；
> 若要提到 180，需先在 N7 落地 deep fixture 使 PASS 提升，属 P1 之后的目标。

---

## 5. 已知缺口清单与排期追踪（GAP Backlog）

| 优先级 | 缺口项编号 | 核心缺陷与短板描述 | 影响范畴 | 计划消解阶段 |
|---|---|---|---|---|
| **P0** | **GAP-01** | `_pick_bin` 硬编码 `.exe` 且主 CI 漏编 daemon，导致收敛套件在 Linux CI 全面崩溃 | CI/CD 基础设施 | Phase 0 |
| **P0** | **GAP-02** | Rust 2073 个单元测试在 CI 仅跑 `compat_native_handlers` 单一模块，大量核心算法无回归保护 | Rust 质量安全 | Phase 0 |
| **P1** | **GAP-03** | 4 套分裂的测试 Daemon Harness（`conftest` / `_w3_harness` / `release_acceptance` / `RouteStub`）引发端口管道竞争 | 测试稳定性与并发 | Phase 1 |
| **P1** | **GAP-04** | 18 项既有缺陷仅通过总量 `defects <= 18` 监控，存在"新缺陷掩盖旧缺陷"的假阳性风险 | 缺陷回归防线 | Phase 1 |
| **P1** | **GAP-05** | T2/T3 写操作工具大量依赖 SKIP，治理与破坏性写路径缺乏正向确定性断言 | 业务功能覆盖 | Phase 2 |
| **P1** | **GAP-06** | 4 角色流转（Planner/Executor/Reviewer/Adjudicator）与单调 Lease Fencing 缺乏端到端测试 | Agent 治理体系 | Phase 3 |
| **P2** | **GAP-07** | `seed_sample` 仅支持 Python/TS，16 种编程语言中有 14 种在图谱构建中未纳入常规集成回归 | 多语言核心能力 | Phase 4 |
| **P2** | **GAP-08** | CAS 并发写入与 Mark-Sweep GC 的互斥锁安全性未做极端竞态注入校验 | 存储数据安全 | Phase 3 |
| **P2** | **GAP-09** | 性能测试脚本（`perf_daemon_baseline.py`）未接入 CI 自动化门禁，存在静默性能退化风险 | 性能稳定性 | Phase 4 |
| **P2** | **GAP-10** | **447 处 skip 中仅 52 处（11.6%）有实测归因，190 处完全未归因**，覆盖率结论存在盲区 | 测试审计可信度 | Phase 5 |
| **P2** | **GAP-11** | `test_m1_route_matrix.py:75` 函数名 `test_matrix_has_239_tools` 与实际常量 243 名实不符，易误导 | 可维护性 | Phase 0 |
| **P2** | **GAP-12** | 门禁脚本 `check_ci_gates.py` / `check_skip_rate.py` **均不存在**，CI 改造清单不可直接执行 | CI/CD 基础设施 | Phase 0 |
| **P2** | **GAP-13** | `tests/parser_contract/` 44 处 skip 中 36 处因 `callwarden_core 未安装`，被误归为「平台固有合理保留」 | 覆盖失真 | Phase 0 |

---

## 6. 审核结论

**结论：不通过（有条件）。**

- ✅ 诊断层（§1 的三个落差）**证据充分、成立**，可直接作为 Phase 0 立项依据。
- ❌ 归因层（§2）**存在算术造假与最大家族遗漏**，已在本轮修正，但 190 处缺口未补。
- ❌ 门槛层（§4）存在 `PASS>=180` vs `>=100` 的三方冲突，已订正为代码口径。
- ❌ 落地层（`TESTING_PLAN.md` §10.4）引用了不存在的门禁脚本，照抄即失败。

**复评条件**（全部满足方可改判「有条件通过」）：

1. P0 完成：`ci.yml` 补 `cargo build --no-default-features --bin cw-daemon`，主 CI 收敛套件 ERROR → 0；
2. 门禁脚本 `check_ci_gates.py` / `check_skip_rate.py` 落盘并接入 CI；
3. M1 函数名 239 → 243 订正，`parser_contract` 的 39 处误分类归位；
4. P5 完成 190 处 skip 的逐文件实测归因，有效 skip 率按正确公式重算。
