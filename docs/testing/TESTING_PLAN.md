# CallWarden 测试方案（完整版 · 锚定权威真相源）

> 版本：v4（完整版）｜ 日期：2026-10-10 ｜ 适用仓库：`callwarden`
> 配套文档：
> - `TEST_CASES.md` —— 分级测试用例清单（由 `gen_test_cases.py` 从权威源生成）
> - `COVERAGE_AUDIT.md` —— 覆盖矩阵与 219 处 `pytest.skip` 审计
> - `BUILD_ENV.md` —— 构建前置（Rust 工具链如何打通，cw-daemon 二进制如何产出）
> - `gen_test_cases.py` —— 可复现生成器

---

## 0. 目的与适用范围

本方案为 CallWarden 定义**完整测试策略**，目标有三：

1. **不重造**：实仓已有 `tests/convergence/**`（T1–T5 + M1–M4）收敛套件，它用权威 JSON 全量驱动 243 MCP 工具 + 234 CLI 叶子。本方案把它**扶正为唯一执行层**，并补它没覆盖的洞。
2. **可审计**：把"测什么 / 怎么分级 / 什么算通过"写成人可读、机器可复现的文档，供其他 agent 评审。
3. **门禁化**：测试通过不放行；修改后重部署需 reopen 回归；一整类测完再统一关闭（dogfooding 在 `cw task` 树上）。

**禁止**：再手写/虚构 fixture、再自建一套与 `cli/categories.py` / `server/tools/_categories.py` 口径冲突的分类树。

---

## 1. 测试哲学（三条铁律）

| # | 铁律 | 含义 | 违反后果 |
|---|------|------|----------|
| 1 | **fail-closed** | 任何异常必须显式失败或返回结构化错误，**绝不静默降级 / 绝不本地兜底执行** | daemon 不可达 → `DaemonUnavailableError`；rc=0 但返回空/垃圾 → 判 FAIL |
| 2 | **整类关闭** | 一个分类（21 CLI 类 / 17 MCP 类）内全部用例通过后才整体关闭该类的 `cw task` 节点 | 不允许"挑几个过了就关类" |
| 3 | **门禁不降级** | 已知缺陷基线只许减少不许回升；新增 skip 必须带原因且计入 `skip_rate` | DEFECT 数、fail-soft 数、traceback 数只能降 |

> 第 1 条是最高优先级：rc=0 却返回垃圾比崩溃更危险——它让上游以为成功了。

---

## 2. 被测系统范围（两轴）

测试表面以两条正交轴定义，全部取自权威真相源：

| 轴 | 真相源 | 规模 |
|----|--------|------|
| **CLI 轴** | `cli/categories.py` | **21 类 / 84 顶层命令**（[18]-[21] 为 CLI-only，MCP 无对应分类） |
| | `tests/convergence/fixtures/cli_full_params.json` | **234 个叶子命令**（提取 233 + 跳过 1） |
| **MCP 轴** | `server/tools/_categories.py` | **17 类 / 243 工具**（与 CLI [1]-[17] 同构） |
| | `tests/convergence/fixtures/mcp_full_schema.json` | 243 工具真 schema（name/description/required/params） |

**种子事实**（用于精确断言，非 substring 匹配）：

- `tests/convergence/seed_sample/calc.py`：`add(a,b)` / `multiply(a,b)`，且 **multiply → add 存在真实调用边**
- `tests/convergence/seed_sample/service.ts`：`MemoryRepo.find` / `MemoryRepo.save`、`Service` 类

由此可精确断言：`callees("multiply") == {"add"}`、`file("calc.py")` 符号集 == `{add, multiply}`。

---

## 3. 测试架构（分层）

收敛套件按"基建 → 全参数 → 横切 → 系统级"四层组织，本方案沿用其结构作为执行层：

```
L0  基建      T1  种子 workspace fixture + param_provider 骨架（T2/T3 的前提）
   ──────────────────────────────────────────────────────────────────
L1  全参数    T2  243 MCP 工具全参数真实调用
             T3  234 CLI 叶子全参数真实调用
   ──────────────────────────────────────────────────────────────────
L2  横切质量  M1  239/239 路由矩阵（每个工具 rpc_method ∈ dispatch.rs）
             M2  Python 纯 client 审计（cli/ 无新违例）
             M3  双 agent 单 workspace 并发写一致性
             M4  CLI fail-closed（daemon 不可达不降级本地）
   ──────────────────────────────────────────────────────────────────
L3  系统级    T4  多 workspace / 多 agent 隔离与协同
             T5  真实 LLM 按工具 description 选对率（需 OPENAI_API_KEY）
```

> **覆盖结论**：T2+T3 已对**全部 243 MCP 工具与 234 CLI 叶子**做全参数真实调用。本方案的 `TEST_CASES.md` 即这两层的"人工可读映射层"。

---

## 4. 分级法（P0 / P1 / P2）

| 优先级 | 含义 | 执行环境 | 放行要求 |
|--------|------|----------|----------|
| **P0** | 核心只读查询（精确断言）+ 已知缺陷钉死（fail-closed） | 种子 workspace（已 build_graph） | **必须通过**；DEFECT/fail-soft/traceback 不许回升 |
| **P1** | 常规契约 / 写路径 | 隔离 daemon + 临时 DB 实例 | 隔离环境通过即可合入 |
| **P2** | 破坏性 / 重操作（全量 refresh、clone clear、daemon *、server、watch 等） | **专用隔离沙箱**，**不进主回归** | 单独环境验证，禁止污染生产 daemon |

CLI 叶子当前分布：`P0=19`、`P1=187`、`P2=27`（合计 233 提取叶子）。
MCP 工具当前分布：`读≈171（P0）`、`写≈72（P1）`（合计 243）。

**P0 判定规则**（CLI 叶子，见 `gen_test_cases.py::classify_cli`）：

- 命中 `KNOWN_FAILSOFT`（call-chain / coupled-fns / largest-fns / rule applicable / status）→ **P0 · FAIL_SOFT**
- 命中 `KNOWN_TRACEBACK`（collab publish / daemon publish / daemon snapshot-stats）→ **P0 · TRACEBACK**
- 命中 `READONLY_CORE` 且无上述 → **P0 · READONLY**
- 命中 `DESTRUCTIVE` → **P2**
- 命中 `WRITE_ISO`（task create/next/report/split/list、audit verify、identity revoke、lease、gc policy）→ **P1**
- 其余 → **P1 · GENERAL**

---

## 5. 门禁与放行标准（Gates）

| 门禁 | 指标 | 阈值 | 当前基线 |
|------|------|------|----------|
| 主回归 skip 率 | `skip_rate = skip / total` | **≤ 5%** | 见 `COVERAGE_AUDIT.md`（当前 219 处 skip，多数为环境/平台类） |
| T2（MCP） | DEFECT / PASS / 覆盖 | `DEFECT==0` 且 `PASS>=100` 且 `覆盖合计==243` | 157 PASS / 72 BUSINESS / **0 DEFECT** |
| T3（CLI） | DEFECT 基线 | **不回升**（基线 18） | 70 PASS / 114 BUSINESS / 18 DEFECT / 31 SKIP |
| T3 fail-soft | 5 个 rc=0 吞错命令 | 不得新增，且断言"空结果=FAIL" | 5 个钉死 |
| T3 traceback | 3 个命令 | 输出不得含 `Traceback (most recent call last)` | 3 个钉死 |
| T3 method_not_found | CLI→daemon compat RPC | 不得新增，只许减少 | 10 个（系统性） |
| M1 | 路由矩阵 | 239/239 通过 | 239/239 |
| M2/M3/M4/T4 | 静态/并发/fail-closed/隔离 | 全部通过 | — |
| T5 | LLM 选对率 | ≥ 基线阈值（需 `OPENAI_API_KEY`） | 未配置则 skip |

> 未达 `skip_rate ≤ 5%` 前，**不准宣称"通过"**——否则"通过率"是自欺。

---

## 6. 已知缺陷基线（T3 首轮，必须钉死不许回升）

T3 首轮（生产 daemon `b495919`）：**70 PASS / 114 EXPECTED_BUSINESS / 18 DEFECT / 31 SKIP**

| 缺陷类 | 数量 | 代表 | 钉死方式 |
|--------|------|------|----------|
| rc=0 掩盖真 bug（fail-soft 吞异常） | 5 | `call-chain`, `coupled-fns`, `largest-fns`, `rule applicable`, `status` | rc==0 时必须返回有效载荷，**空结果/吞异常 = FAIL** |
| traceback | 3 | `collab publish`, `daemon publish`, `daemon snapshot-stats` | stdout/stderr 不得含 `Traceback (most recent call last)` |
| method_not_found（CLI→daemon compat RPC 未实现） | 10 | 由探测得出 | 不得新增，只许减少 |

---

## 7. 前置条件与解锁步骤

收敛套件依赖以下前置，**按依赖顺序解锁**：

1. **构建 cw-daemon（已满足 ✅）**
   `cargo build --bin cw-daemon` → `rust_ext/target/{release,debug}/cw-daemon.exe`。
   本机工具链打通记录见 `BUILD_ENV.md`。这步解锁了全仓约 **42 处**"二进制未构建"类 skip。
   > 收敛套件 `_pick_bin()` 优先用 **release** 二进制；本机当前为 debug（功能等价，仅慢）。验收建议补 `cargo build --release --bin cw-daemon`。

2. **停掉生产 daemon 消"管道被占用"skip（待执行）**
   约 **17 处** skip 因默认命名管道被现有生产 daemon 占用。收敛套件用隔离 daemon（临时 data_root），需在**生产 daemon 停止时**运行（`cw daemon stop`）。

3. **配置 `OPENAI_API_KEY`（解锁 T5）**
   T5 LLM 可理解性测试在无 key 时 skip。配置 `.env` 的 `OPENAI_API_KEY` / `OPENAI_BASE_URL` 即解锁。

4. **补齐语言 fixture（消 17 处 skip）**
   `fixture {lang}.json 不存在` 类 skip 需对应语言的种子 fixture；视需要生成。

5. **确保 cargo 在 PATH（消 6 处 skip）**
   部分用例会临时 `cargo build` 新鲜二进制，需 `cargo` 可达。

> 其余 skip（Windows 无 AF_UNIX 4 处、frozen build 4 处等）为平台/状态固有，属**合理 skip**，不计入"未达标"。

---

## 8. 执行顺序（dogfooding 在 `cw task` 树上）

```
1. 解锁前置（构建 release 二进制 / 停生产 daemon / 配 key / 补 fixture）
2. 跑 T1 基建冒烟 → 通过才进 T2/T3
3. 扶正 T2/T3：把基线（T2:157/72/0；T3:70/114/18/31）写进 CI 断言（DEFECT 不回升）
4. 补 P0 钉死用例：尤其 5 个 fail-soft + 3 个 traceback（铁律 #1）
5. 补负向/边界：符号不存在、空仓、非法参数、lease 过期、撤销后操作、并发抢锁
6. P1 写隔离 → P2 沙箱，逐步放开
7. 每关一类，整体关闭该类 `cw task` 节点（铁律 #2）
```

---

## 9. 复用基建（禁止重造）

| 已有资产 | 用途 |
|----------|------|
| `tests/convergence/param_provider.py` | 从权威 JSON + SeedContext 生成**确定性真实参数** |
| `tests/convergence/conftest.py`（`isolated_http_daemon` / `seed_workspace` / `qa_workspace`） | 隔离 daemon + 种子 workspace（返回 `workspace_instance_id`） |
| `tests/test_http_daemon_release_acceptance.py` | 隔离 daemon 启动/等待/清理助手（同源复用） |
| `tests/conftest.py::_isolate_db_path` | autouse 隔离 DB 路径，不污染 `~/.callwarden` |
| `scripts/verify_route_matrix.py` / `check_client_purity.py` | 路由矩阵与客户端纯净度既有门禁 |
| `test_category_source.py` | 校验"每个顶层命令恰好归一类 / 17 分类恰好划分全部工具" |

---

## 10. 交付物清单（本目录）

| 文件 | 内容 |
|------|------|
| `TESTING_PLAN.md` | 本文件：完整测试策略 |
| `TEST_CASES.md` | 分级测试用例清单（84 CLI / 233 叶 / 243 MCP，P0/P1/P2） |
| `COVERAGE_AUDIT.md` | 覆盖矩阵 + 219 处 skip 审计 |
| `BUILD_ENV.md` | 构建前置（Rust 工具链打通记录） |
| `gen_test_cases.py` | 可复现生成器（改权威源后重跑即可刷新 `TEST_CASES.md`） |

---

## 11. 废弃与迁移

以下旧资产因 fixture/分类口径与实仓不符，已停用（保留 `qa-test-audit-report.md` / `callwarden-test-plan-review.md` 作过程证据）：

`callwarden-test-io-spec.md`、`callwarden-test-taxonomy.md`、`callwarden-test-golden-spec.md`、
`callwarden-test-p0-spec.md`、`callwarden-test-decomposition.md`、`build_test_tree.py`、
`gen_io_spec.py`、`class_owners.json`。

> 这些旧文件位于工作会话目录，**尚未执行 `--apply`**，未污染 `cw` 任务树。
