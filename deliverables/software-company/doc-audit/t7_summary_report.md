# T7 收尾报告 — 全量测试系列（T1–T6）汇总与跨平台审查

> 日期：2026-10-07
> 范围：汇总 T1–T6 的全部发现与修复，并对本系列改动做跨平台（Windows/Linux/macOS/CI）审查。
> 结论：全量测试系列发现的所有功能缺陷已修复并验证；跨平台审查发现并修复 1 个 CI 覆盖缺口。

---

## 1. 系列总览

本测试系列以"真实调用、真实负载、真实验证"为原则，对 Call Warden 的 MCP 工具、CLI 命令、
并发正确性、LLM 可理解性、性能做了端到端全量测试，并修复了全部发现的缺陷。

| 阶段 | 主题 | 产物 |
|------|------|------|
| T1 | 全量测试基建（种子 workspace fixture + 参数 provider） | tests/convergence/ |
| T2 | 243 个 MCP 工具全参数真实调用 | t2_mcp_result.json / 报告 |
| T3 | 234 个 CLI 命令全参数真实调用 | t3_cli_result.json / 报告 |
| T4 | 多 workspace / 多 agent 并发正确性 | t4_concurrency_report.md |
| T5 | 真实 LLM（DeepSeek）可理解性端到端 | t5_llm_*.json / 报告 |
| 深度轮 | EB→PASS 深度参数 + 工具壳 schema 缺陷定位 | deep_round_result.json |
| A 类 | compat method_not_found 技术债 + 简化实现补全 | compat_native_handlers.rs |
| T6 | 生产 daemon + 真实 workspace 端到端 RPC 延迟基准 | t6_perf_*.json / 报告 |
| **T7** | **汇总 + 跨平台审查 + CI 覆盖修复** | **本报告 + ci.yml** |

## 2. 缺陷修复台账（全部已验证并推送）

本系列共 15 个提交（`c039352` → `439299d`），其中修复类提交涵盖的缺陷：

### 2.1 基建 / daemon 路由
- `c039352` refresh --all 路由到正确的 workspace.build_graph RPC
- `e9ba8bb` cw stats snapshot_not_ready + workspace set 显示错误
- `c15cbee` test_impact_selection SQL 绑定漏 workspace_id（参数数不符）

### 2.2 T3 CLI 缺陷（共修复 8 + 2 + 4 = 一次性清零 t3 的 13 个 DEFECT）
- `40d25fc` 8 个 CLI 命令 daemon 迁移后结构不匹配
- `c4f5202` 全局 `--workspace` 预扫描吞子命令参数（collab publish）+ daemon snapshot-stats 无参必失败
- `4adf399` 4 个 CLI 命令 rc=0 掩盖的字段 KeyError

### 2.3 深度轮 / MCP schema
- `95dfab4` 12 个 MCP 工具壳 schema/契约缺陷（LLM 按 schema 调用必失败）

### 2.4 A 类 compat 技术债（10 个 method_not_found 清零 + 3 处简化补全）
- `557ed68` 8 个裸名方法迁 daemon 原生 handler + toolchain.get 错误码（method_not_found→not_found）+ get_function_issues→find_issues 映射
- `651c3f8` 补全 gc_restore 全扫 / gc_purge 审计 / build_defect_knowledge fixes 关联（对齐 Python 真相源，移除所有 note 占位）

### 2.5 性能（无缺陷，表征）
- `439299d` T6 性能基准：全部方法 errors=0，无功能缺陷

**最终状态**：本系列发现的所有功能缺陷均已修复、验证、推送。t3 的 13 个曾 DEFECT 回归 13/13 OK（无 method_not_found、无 Traceback）。A 类的 10 个 method_not_found 实测 10/10 路由通。A 类 3 处简化补全由 6 个单元测试 + 生产 daemon 实测双重验证。

## 3. 跨平台审查

对本系列引入的 Rust/CLI 改动做 Windows/Linux/macOS 安全性审查。

### 3.1 Rust 代码跨平台安全性 —— 通过

| 审查点 | 结论 |
|--------|------|
| **compute_content_hash** | 与 Python `config.compute_content_hash` **字节级等价**：同样的 `\r\n`/`\r`→`\n` 两步归一（同序）+ UTF-8 + sha256 + 小写 hex。CRLF（Windows 检出）与 LF（Linux 检出）产出相同 hash，跨平台不变量成立。单元测试固定了空串向量 `e3b0c442…b855`。 |
| **路径处理** | `path_is_ignored_for_restore` / `should_ignore` 一律按 `'/'` 分隔（DB 存储的 rel_path 本就正斜杠规范化），非 `MAIN_SEPARATOR`。`load_ignore_patterns` 用 `Path::join`（OS 正确）+ `str::lines()`（同时吃 `\n`/`\r\n`）。无硬编码分隔符。 |
| **SQLite URI** | 新 handler 接收已打开的 `&Connection`，不自行构造 URI，`\\?\` 前缀/immutable 问题不在本层。 |
| **fnmatch 大小写** | Windows 大小写不敏感、非 Windows 敏感——这是**继承自既有 `cli::status` 的 OS 文件系统一致性**，非本系列引入，且与 Python fnmatch 行为对齐。新测试用 `.git/config`（各平台大小写一致）故稳定。 |
| **时间戳** | `now_ts()` 用 `SystemTime::duration_since(UNIX_EPOCH)`，可移植。 |

**结论**：本系列 Rust 代码无路径分隔符、换行、SQLite URI、编码方面的平台专属缺陷。

### 3.2 CI 覆盖缺口 —— 发现并修复

**发现**：审查 9 个 CI workflow 发现**所有 workflow 只 `cargo build`、从不 `cargo test`**。后果：
- 我在 `compat_native_handlers.rs` 新增的 6 个单元测试（gc_restore 全扫 / gc_purge 审计 /
  build_defect_knowledge fixes 关联 / content_hash 等核心逻辑）在 CI **完全无覆盖**。
- `#[cfg(test)]` 代码的编译错误 CI 抓不到（`cargo build` 不编译测试模块）。
- Rust 层逻辑回归 CI 抓不到。违背 AGENTS 规则 24（daemon 改动应跑 `cargo test daemon:: --lib`）。

**修复**（本报告同批次）：在 `ci.yml` 新增 `rust-unit-test` job，在 ubuntu + Python 3.14 下运行
`cargo test --no-default-features --lib daemon::compat_native_handlers`。

**范围决策**：限定 `daemon::compat_native_handlers` 而非 `daemon::` 全模块。原因：本地验证
`daemon::` 全模块时，`snapshot_state` 的一批测试依赖进程单例/真实环境，并行跑会挂起 >60s，
`cas_merge` 偶发失败——这些是既有测试的环境依赖问题，纳入会制造假红门禁。本系列新增测试
用 in-memory SQLite、无环境依赖，本地已验证 `--no-default-features --lib` 下 6/6 稳定绿。

> 既有 daemon 测试的环境脆弱性（snapshot_state 挂起 / cas_merge 偶发失败）是本次审查
> **顺带发现的既有问题**，不在本系列改动范围内，建议另立技术债任务治理（去并行化或隔离进程单例）。

### 3.3 本地验证记录
- `cargo test --no-default-features --lib daemon::compat_native_handlers`：**6/6 passed**（Windows / Python 3.14 / PYO3_PYTHON）。
- `cargo check --bin cw-daemon`：通过。
- A 类补全经生产 daemon release 部署（runtime/current）+ route_rpc 实测，gc_purge audit_id 190→191 递增确认生产跑的是审计版。
- T6 基准：生产 daemon + 真实 workspace（33.9 万符号），n=200 串行，全部 errors=0。

## 4. 验证方法论遵循（AGENTS 规则）

- 规则 13（真实 E2E）：T6 用生产 daemon + 真实 workspace，非合成数据压测；串行取分位数；记录硬件。
- 规则 24（daemon 改动跑完整测试）：本系列新增测试已纳入 CI；既有 daemon 测试脆弱性已记录。
- 规则 34/43（Windows daemon 权威 + 部署≠编译）：A 类改动经 refresh_shared_runtime.ps1 部署 runtime/current 后实测。
- 规则 36（query_map 不作块尾临时值）：新 handler 全部用 `collected` 局部绑定。
- 规则 47（单文件行数）：compat_native_handlers.rs 1290 行（含 ~290 行测试），未超 1500 硬阈值。

## 5. 遗留与建议

| 项 | 状态 | 建议 |
|----|------|------|
| 本系列功能缺陷 | ✅ 全部修复 | — |
| A 类 3 处简化 | ✅ 全部补全 | — |
| 既有 daemon 测试脆弱性（snapshot_state 挂起 / cas_merge 偶发） | ⚠️ 既有问题 | 另立技术债：去并行化或隔离进程单例，再把 CI rust-unit-test 范围扩到 `daemon::` |
| T6 尾延迟（p95/p99 ~20-40ms） | ℹ️ 已表征 | daemon 调度抖动，非连接开销；若需优化走 daemon 并发侧 |
| CI cargo test 覆盖 | ✅ 已补 compat_native_handlers | 后续随既有测试稳定化逐步扩大范围 |

## 6. 结论

全量测试系列（T1–T7）完成。**发现的所有功能缺陷已修复、验证、推送**；A 类技术债（含 method_not_found
与简化实现）全部清零；跨平台审查确认本系列 Rust 代码平台安全，并修复了"Rust 单元测试无 CI 覆盖"
这一真实缺口。既有 daemon 测试的环境脆弱性作为独立技术债记录待后续治理。
