# 覆盖审计与 Skip 分析（CallWarden 测试）

> 配套：`TESTING_PLAN.md` §5 门禁（`skip_rate ≤ 5%`）｜ `TEST_CASES.md`
> 统计基准：2026-10-10 仓库状态（`tests/**` 全量 grep + 归类）

---

## 1. 总览（v5 订正）

- 全仓 skip 站点真实计数：**`pytest.skip(` 213 处 + `pytest.mark.skipif` 234 处 ≈ 447 处**（全仓 grep）。
  > ⚠️ v4 称"约 219 处"只数了 `pytest.skip(` 且数字仍错；**171/234 个 `@pytest.mark.skipif` 是真实跳过却未计入**，真实 skip 面约 447。
- **首要发现（v5 新增）：收敛套件在主 CI 完全不可执行。**
  `tests/convergence/conftest.py:39-49` `_pick_bin()` 硬编码 `cw-daemon.exe`，而主 `ci.yml` 的 `test` job（ubuntu-latest）只 `python release/build.py --rust`（构建 Python 扩展，不构建 daemon 二进制，`ci.yml:32`），随后 `pytest tests/`（`ci.yml:82`）触发收敛套件 → `_pick_bin` 抛 RuntimeError → **T1/T2/M3/M4/T4 + `test_regression_http_tools` 全部 ERROR**。文档宣称的"243/234 全参数真实调用、100% 业务覆盖"在 CI 执行量为 **0**。（`e2e-verify-linux-x86_64.yml:97` 虽构建 daemon，但是独立 e2e workflow，不在主 CI。）
- 收敛套件（T1–T5 / M1–M4）对 **243 MCP 工具 + 234 CLI 叶子**做的是**表面可达调用**（路由通 + 占位参数 + SKIP 计数），**非功能正确性覆盖**——详见 `TESTING_PLAN.md` §1。
- 剩余 skip 几乎全部是**环境 / 平台 / 状态**类，但量级被 v4 低估（见 §2）。

---

## 2. Skip 家族归类与解锁状态（v5 订正）

> ⚠️ 下表为归类采样，量级准确；**总 skip 站点真实约 447（213 `pytest.skip(` + 234 `pytest.mark.skipif`），非 v4 的 219**。

| 家族 | 估算处数 | 含义 | 解锁动作 | 状态 |
|------|----------|------|----------|------|
| **BINARY_MISSING / CI 不可执行** | ~50 + 收敛全 ERROR | `cw-daemon` 未构建（`binary not built`）；**主 CI 不构建 daemon 致 `_pick_bin` 抛错，收敛套件全 ERROR** | 主 `ci.yml` 增 `cargo build --bin cw-daemon` + `_pick_bin` 跨平台化（见 `TESTING_PLAN.md` §12） | ❌ **未解锁（关键缺口）** |
| PIPE_IN_USE | ~22 | 默认命名管道被生产 daemon 占用 | 运行收敛套件前 `cw daemon stop` 停生产 daemon | ⏳ 待执行 |
| LANG_FIXTURE | ~22 | 某语言 fixture `{lang}.json` 不存在 | 按需生成对应语言种子 fixture | ⏳ 待补 |
| PLATFORM | ~20 | Windows 无 AF_UNIX / 符号链接 / `memfd_create` / non-Linux | **平台固有，合理 skip** | ✅ 合理 |
| OTHER_MISSING | ~11 | 其他文件/fixture 缺失（`hcl.json` / `rel_path` 等） | 按需补齐 | ⏳ 视需 |
| CLI_DAEMON_UNAVAILABLE | ~11 | `cw` / daemon 不可用（与构建/环境相关） | 构建 + 环境就绪后大部分消失 | 🔶 部分依赖构建 |
| CARGO_MISSING | ~8 | `cargo` 不在 PATH（用例临时构建新鲜二进制） | CI/本机把 `cargo` 加入 PATH | ⏳ 待配置 |
| FROZEN | ~5 | 在 frozen build 中运行 | 非 frozen 环境运行 | ✅ 合理 |
| DOC_MISSING | ~3 | `callwarden_USER_GUIDE.md` / 项目内 `.md` 缺失 | 文档补齐 | ⏳ 视需 |
| WORKSPACE | ~3 | 不足两个 workspace（多 workspace 场景） | 提供多 workspace 数据 | ⏳ 场景依赖 |
| OTHER（daemon 未运行 / Docker / Linux-only / 容器矩阵 / 大文件等） | ~59+ | 环境/CI 矩阵/平台专有 | 多数 CI 专属或平台固有 | 🔶 多为合理 |

---

## 3. `skip_rate` 含义与达标路径（v5 订正）

门禁（`TESTING_PLAN.md` §7）：**`skip_rate = skip / total ≤ 5%`**，未达标不准宣称通过。

- **真实分母/分子**：total ≈ 7133 测试函数，skip 站点 ≈ 447 → **真实 `skip_rate ≈ 6.3%`，已超 5% 阈值**（v4 用 219 分母错误，掩盖了越线）。
- **最严重未解锁项**：`BINARY_MISSING / CI 不可执行`（~50 + 收敛全 ERROR）——主 CI 不构建 daemon，收敛套件在 Linux 全 ERROR，这部分不是"合理 skip"而是**套件失效**，必须优先修复（`TESTING_PLAN.md` §12）。
- **可解锁（执行动作）**：停生产 daemon（~22）+ 配 cargo PATH（~8）+ 补语言 fixture（~22）+ 补其他文件（~11）+ 多 workspace 数据（~3）≈ 66 处，靠运行前置即可消除。
- **合理 skip（不计入"未达标"，但需在报告注明）**：平台固有（~20）+ frozen（~5）+ 多数 OTHER（Docker/Linux-only/容器矩阵）。
- 消除"CI 不可执行"失效 + 上述可解锁项后，`skip_rate` 方能达标。

---

## 4. 覆盖矩阵（套件 ↔ 表面）

| 套件 | 职责 | 覆盖表面 | 基线 / 门禁 |
|------|------|----------|--------------|
| **T1** | 基建冒烟（种子 fixture + param_provider） | 非业务，T2/T3 前提 | 必须 PASS |
| **T2** | 243 MCP 工具全参数真实调用 | 全部 243 工具（§4.1） | 157 PASS / 72 BUSINESS / **0 DEFECT**；门禁 `DEFECT==0 & PASS>=100 & 覆盖==243` |
| **T3** | 234 CLI 叶子全参数真实调用 | 全部 233 提取叶子 + 84 顶层 | 70 PASS / 114 BUSINESS / 18 DEFECT / 31 SKIP；门禁 `DEFECT 不回升` |
| **T4** | 多 workspace / 多 agent 隔离与协同 | 写类工具的并发隔离场景 | 并发无死锁、隔离性成立 |
| **T5** | 真实 LLM 按 description 选对率 | 工具 description 质量 | 选对率 ≥ 基线（需 `OPENAI_API_KEY`） |
| **M1** | 239/239 路由矩阵 | 全部 243 工具的 rpc_method 可达性 | 239/239 通过 |
| **M2** | Python 纯 client 审计 | `cli/` 包 purity | 零新增违例 |
| **M3** | 双 agent 单 workspace 并发写 | 写类工具并发正确性 | 混合读写无脏写 |
| **M4** | CLI fail-closed | 全部 CLI 命令 + §2 钉死 | daemon 不可达一律结构化错误，绝不本地执行 |

> **结论**：业务表面（243 MCP + 234 CLI）已被 T2/T3 **全量真实调用覆盖**；本仓库测试策略的核心不再是"补用例"，而是**消除环境类 skip + 守住缺陷基线**。

---

## 5. 已知缺口清单（GAP，v5 重排优先级）

| 优先级 | 缺口 | 类型 | 处理 |
|--------|------|------|------|
| **P0** | **收敛套件在主 CI 不可执行（`_pick_bin` 硬编码 `.exe` + 主 ci.yml 不构建 daemon）** | CI 失效 | 主 ci.yml 增 `cargo build --bin cw-daemon`；`_pick_bin` 跨平台化（`TESTING_PLAN.md` §12） |
| **P0** | Rust 单测 CI 仅跑 `daemon::compat_native_handlers` 一个模块（2073 单测几乎不回归） | CI 失效 | `rust-unit-test` 逐步扩到 `daemon::` 全模块（先修 snapshot_state 等挂起测试隔离） |
| **P1** | 5 个 fail-soft（rc=0 吞错）仅总量钉死，未分项 | 缺陷基线 | 钉死"空结果=FAIL"，只许修复不许新增 |
| **P1** | 3 个 traceback 仅总量钉死 | 缺陷基线 | 钉死"无 Traceback"，分项断言 |
| **P1** | 10 个 method_not_found（CLI→daemon compat RPC） | 缺陷基线 | 分项计数，只许减少不许新增 |
| **P1** | T2 SKIP 15 个治理/破坏性写工具、T3 SKIP 大量命令 → 写面/治理面无正向测试 | 覆盖缺口 | 补 deep fixture + N3/N5 专项（lease/attestation/snapshot/GC） |
| **P2** | 精确断言（callees=={add} 等）仅在 TEST_CASES.md 文字，未落地 | 度量缺口 | N7 落地 19 P0 CLI 精确断言 |
| **P2** | ~22 语言 fixture 缺失 | 覆盖缺口 | 按需生成 |
| **P2** | release 二进制未构建（当前仅 debug） | 验收严谨度 | 补 `cargo build --release --bin cw-daemon` |
| **P2** | 缺 `.gitattributes` 锁 `*.md text eol=lf` | 回归风险 | 防止 role_prompt 门禁再因 CRLF 误伤 |
| **P2** | skip 口径漂移（v4 称 219，真实 447） | 度量 | 以本表 **447** 为统计基准，并接入 `check_skip_rate.py` 自动校验 |

---

## 6. 落地建议（给审计方）

1. **先看 `BUILD_ENV.md`** 确认 daemon 二进制就绪 —— 这是整张套件的前置。
2. 跑套件前 `cw daemon stop` + 确保 `cargo` 在 PATH，消除最大两块 skip。
3. 把 T2/T3 基线写进 CI 断言（DEFECT 不回升、fail-soft/traceback 钉死）。
4. 报告里区分"合理 skip（平台/状态固有）"与"未达标 skip"，不要混为一谈。
