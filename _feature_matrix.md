# Call Warden 功能点矩阵（文档提取 → 去重/合并）

> **文档盘点**（88 个 .md 文件，排除 testcode/ 和 .pytest_cache/）：
>
> **已提取功能点的文档（34 个，标记 ★）**：
>
> | 缩写 | 文件路径 | 类型 | 状态 |
> |------|---------|------|------|
> | RM | README.md | 项目入口 | ★ |
> | CL | CHANGELOG.md | 版本历史 | ★ |
> | AG | AGENTS.md | Agent 规则 | ★ |
> | TM | TOOLS.md | 工具参考 | ★ |
> | CT | CONTRIBUTING.md | 贡献指南 | ★ |
> | UG | callwarden_USER_GUIDE.md | 用户手册 | ★ |
> | GA1 | callwarden 功能差距分析报告.md | 历史差距分析 | ★ |
> | GA2 | callwarden 与 200 个仓库的交叉对比分析.md | 历史模块分析 | ★ |
> | CA | docs/design/competition-analysis.md | 竞品分析 | ★ |
> | IS | docs/design/implementation-status.md | 权威盘点 v14 | ★ |
> | CS | docs/capability_showcase.md | 能力实测 | ★ |
> | RP | docs/roadmap_phase2_plan.md | Phase2 路线图 | ★ |
> | PP | docs/perf_optimization_plan.md | 性能优化计划 | ★ |
> | EA | docs/design/enterprise-architecture-evolution.md | 企业架构演进 | ★ |
> | DS | docs/design/enterprise-daemon-shared-snapshot-plan.md | Daemon 主设计 | ★ |
> | TQ | docs/design/task-quality-gate-plan.md | 质量门禁 | ★ |
> | AR | docs/design/agent-rule-memory-plan.md | Agent 规则记忆 | ★ |
> | BC | docs/design/bootstrap-closure-plan.md | 自举闭环 | ★ |
> | CP | docs/design/cross-platform-packaging-release-plan.md | 跨平台打包 | ★ |
> | WG | docs/design/watcher-generation-state-machine.md | Watcher 状态机 | ★ |
> | CG | docs/design/cas-gc-protocol.md | CAS GC 协议 | ★ |
> | DI | docs/design/daemon-ipc-security.md | Daemon IPC 安全 | ★ |
> | EW | docs/design/enterprise-watcher-benefit-production-plan.md | Watcher 收益 | ★ |
> | EP | docs/design/enterprise-phase1-phase3-detail.md | Phase1-3 详细设计 | ★ |
> | ARC | docs/architecture.md | 架构设计 | ★ |
> | MCT | docs/mcp_tools.md | MCP 工具参考 | ★ |
> | CLI | docs/cli_reference.md | CLI 命令参考 | ★ |
> | ACR | docs/design/audit-cas-replicator-wiring.md | CAS/Replicator 审计 | ★ |
> | P4M | docs/design/_phase4_missing_steps.md | Phase4 缺失项 | ★ |
> | EGF | docs/design/evolve-guardian-architecture/ (spec+tasks+checklist) | Guardian 架构 | ★ |
>
> | 问答.md | Agent 行为/MCP 门禁讨论 | ★QA1 |
> | 问答2.md | 动态约束注入/自学习规则讨论 | ★QA2 |
> | 问题.md | 性能架构（多进程/IPC/Rust）讨论 | ★PR |
> | 对话3.md | Phase 2 收口/增量架构/Daemon 讨论 | ★D3 |
>
> **已读取但不含新功能点的文档（23 个）**：
>
> | 文件路径 | 类型 | 说明 |
> |---------|------|------|
> | .cli_audit.md | 审计 | CLI/MCP 命名审计（173 工具时点），功能点已计入 |
> | .mcp_audit.md | 审计 | MCP 命名审计（173 工具时点），功能点已计入 |
> | docs/design/audit-agent-rule-memory.md | 审计提示词 | Agent Rule Memory 审计流程 |
> | docs/design/audit-report-agent-rule-memory.md | 审计报告 | Agent Rule Memory 审计通过（10/10 子任务 PASS） |
> | docs/design/cli-task-fix-plan.md | 修复计划 | 5 个 CLI task 命令 bug（已修复） |
> | docs/design/daemon-deploy-runbook.md | 部署手册 | Enterprise Daemon 部署/升级/回滚 |
> | docs/design/enterprise-daemon-full-e2e-followup.md | E2E 设计 | Daemon 完整请求链路（已有子任务跟踪） |
> | docs/design/adr-001-legacy-container-client.md | ADR | Legacy 容器策略决策：宿主机 Agent + 容器只被观察 |
> | docs/design/parse-input-abi.md | 规范 | ParseInput ABI（canonicalize_source Rust 规范） |
> | docs/design/phase-spec-cross-reference.md | 交叉引用 | Phase ↔ 短规范映射表 |
> | docs/design/rust_daemon_architecture.md | 已废弃 | 标记已过时，被 DS 取代 |
> | docs/design/task-state-machine.md | 设计 | 任务状态机（级联 close，已实现） |
> | docs/p16_cold_start_analysis.md | 已废弃 | "内存主表"方向已废弃，走混合架构 |
> | docs/naming-analysis-report.md | 命名分析 | 开源命名策略（SymTree/CallWarden/SigMap） |
> | docs/README.md | 文档索引 | 文档目录页 |
> | docs/quickstart.md | 快速开始 | 安装/初始化/基本查询 |
> | docs/deployment.md | 部署指南 | 本地/Docker/MCP 配置 |
> | docs/history/README.md | 归档索引 | 历史文档归档说明 |
> | docs/history/ 其余 8 个 | 历史归档 | 全部标注"不代表当前实现"，已被 IS/ARC 取代 |
> | 配置自检码.md | 对话记录 | 关于 TokenSlim 项目，非 Call Warden |
> | ~~问答.md~~ | ~~对话记录~~ | → **已提取**，见 ★QA1 |
> | ~~问答2.md~~ | ~~对话记录~~ | → **已提取**，见 ★QA2 |
> | ~~问题.md~~ | ~~对话记录~~ | → **已提取**，见 ★PR |
> | ~~对话3.md~~ | ~~对话记录~~ | → **已提取**，见 ★D3 |
> | 完成企业守护进程_E2E_任务.md | 对话导出 | 24K 行对话记录 ★E2E |
> | .trae-cn/memory/ 下 4 个 .py | 任务定义/基准脚本 | ★MEM |
> | .waylog/history/ 功能差距.md | Codex 对话 | ★WL1 |
> | .waylog/history/ perf_optimizati.md | Codex 对话 | ★WL2 |
> | .waylog/history/ enterprise_arch.md | Codex 对话 | ★WL3 |
> | tests/_bench_cw_vs_grep_report.md | A/B 对比报告 | ★BR1 |
> | tests/_bench_e2e_report.md | P12 压测报告 | ★BR2 |
> | tests/_bench_graphstore_report.md | GraphStore 实测 | ★BR3 |
> | tests/_bench_matrix_report.md | 参数矩阵实验 | ★BR4 |
>
> **未读取（低优先级，11 个）**：
>
> | 文件路径 | 原因 |
> |---------|------|
> | .trae-cn/memory/ 下 9 个 .md | 历史 plan/results 文件，信息已被 docs/design/ 系列覆盖 |
> | .waylog/history/ 2026-07-06 | 会话历史，已被 docs/design/ 覆盖（前会话已读） |
> | tests/ 下 bench JSON 报告 | 性能数据 JSON，已被 .md 报告覆盖 |
> | rust_ext/.pytest_cache/README.md | 自动生成 |

## 实际基线数据（代码实查）

| 指标 | 文档声称 | 代码实查 |
|------|---------|----------|
| MCP 工具数 | 120-125 | **206**（@mcp.tool() 装饰器计数） |
| CLI 子命令 | 145+ | 38 子命令 + ~98 个 --flag 命令 |
| 支持语言 | 16 | 16（parsers/ 目录 16 个解析器） |
| Mixin 模块 | 23 | **35 个功能 Mixin**（39 个 db_*.py 文件 + 1 基类 CodeGraphBase） |
| Schema 版本 | v14 | **v40** |
| 产品版本 | 0.3.0 | release/version.toml `[product] version = "0.3.0"` |

---

## A. 核心功能（已完成，文档一致）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| A1 | 16 语言 tree-sitter 解析 | IS/RM/CL | ✅ 已实现 | 16 个 parser 文件确认 |
| A2 | 四级调用解析策略 | IS/CS | ✅ 已实现 | call_resolver.py |
| A3 | 向上/向下调用链 BFS | IS/CS | ✅ 已实现 | call_chain.py |
| A4 | 拓扑排序 + 循环检测 | IS | ✅ 已实现 | db_build.py |
| A5 | 跨文件调用标记 | IS | ✅ 已实现 | is_cross_file |
| A6 | 文件版本（content_hash 去重） | IS/CS | ✅ 已实现 | db_build.py |
| A7 | 符号版本 + 删除标记 | IS | ✅ 已实现 | file_symbol_versions |
| A8 | 单函数/批量注释恢复 + 预览 | IS | ✅ 已实现 | db_comment.py |
| A9 | 代码度量（圈复杂度/耦合度/扇入扇出/健康评分） | IS/CS | ✅ 已实现 | db_metrics.py |
| A10 | AI Agent 健康检查（Token 溢出预警） | IS/CA | ✅ 已实现 | check_file_health |
| A11 | Git 历史导入 + 符号级变更追踪 | IS/CS | ✅ 已实现 | db_git.py |
| A12 | Semgrep 多语言静态安全扫描 | IS/CS | ✅ 已实现 | analyzers/issues.py |
| A13 | 结果按内容去重入库 | IS | ✅ 已实现 | semgrep_findings |
| A14 | 增量扫描 | IS | 🟡 复审回退（2026-07-21） | 新增 `scan_semgrep_incremental()` 方法（analyzers/issues.py）：通过 `git diff --name-only` 取变更文件 → 调用 `run_semgrep` 扫描 → `save_semgrep_findings(scan_type='incremental', stale_file_ids=...)` 清理旧 findings + 关联 scan_id。schema v40 新增 `semgrep_findings.scan_id` 字段 + `idx_semgrep_scan_id` 索引。CLI 新增 `cw semgrep scan --incremental [--base main] [--head HEAD]`，MCP 新增 `scan_semgrep_incremental` 工具。**复审回退（2026-07-21）**：增量扫描入口存在，但 git diff/删除及失败边界未闭合——`stale_file_ids` 清理只覆盖已知文件，删除的文件不会触发 finding 清理；scan 失败时 findings 已写入但 scan_id 无对应记录，留下孤儿 finding |
| A15 | .gitignore 完整语法解析 | IS/CL | ✅ 已修复（2026-07-20 二轮评审补全） | 接入 pathspec 库作为主路径，获得完整 gitignore 语义：字符类 `[abc]`/`[a-z]`/`[!abc]`、尾随空格保留（除非行末 `\` 转义）、目录剪枝后 negation 恢复（pathspec 内部 last-match-wins）、复杂 `**` 与 `/` 组合。pathspec 不可用时降级到自研实现（保留向后兼容，自研不支持字符类）。pyproject.toml/requirements.txt/install.py 均已加入 pathspec 核心依赖 |
| A16 | .callwardenignore 项目级规则 | IS | ✅ 已实现 | |
| A17 | GC 归档/复活/状态/清除 | IS/CL | ✅ 已实现 | db_gc.py |
| A18 | build 末尾自动 Young GC | IS | ✅ 已实现 | |
| A19 | SARIF 导出 + GitHub Actions | IS/CL | 🟡 复审回退（2026-07-21） | cicd/ SARIF exporter + GitHub Action 入口存在。二轮评审修复：PRChecker 改用 `check_before_edit` + `run_errors` 收集 + SARIF `executionNotifications` 让 fail-visible。**复审回退（2026-07-21）**：`cicd/pr_check.py:142` `passed = errors == 0` 未纳入 `run_errors`/`scan_complete`，`_query_open_findings` 只查 `guardrail_findings` 未合并 `semgrep_findings`，GitHub Action 只读 `passed` 仍 exit 0。fail-open 未真正闭合 |
| A20 | 增量分析（CI/CD） | IS | ✅ 已实现 | incremental.py |
| A21 | PR 检查 | IS | 🟡 复审回退（2026-07-21） | pr_check.py。二轮评审修复：原调用不存在的 `guardrail_check_edit` 且吞异常（fail-open），改为 `check_before_edit` + 异常上浮 + Semgrep findings 合并进 SARIF。**复审回退（2026-07-21）**：`passed` 仍为 `errors == 0`，未纳入 `run_errors`/`scan_complete`，Semgrep findings 未合并为阻断结果，`cicd/github_action.py` 仍 exit 0。fail-open 未真正闭合 |
| A22 | 安全修复 SEC-001~007 | IS/CL | ✅ 已实现 | 原子写入/LSP安全/日志消毒 |
| A23 | 文件级并行解析（ThreadPoolExecutor） | IS/CL | 🟡 部分完成（评审 2026-07-20） | 文件级并行存在，但主路径现为 Rust pool/ProcessPool，ThreadPool 主要是降级；矩阵描述已过时，应更新为 "Rust pool + ProcessPool + ThreadPool 降级" |
| A24 | PRAGMA WAL + cache + mmap | IS/CL | ✅ 已实现 | |
| A25 | executemany 批量写入 | IS/CL | ✅ 已实现 | |

## B. Guardian 四大支柱（已完成，文档一致）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| B1 | 生产安全护栏（DB/API/Incident） | IS/CL/CA | ✅ 已实现 | db_guardrail.py |
| B2 | 变更影响 blast_radius + cross_layer | IS/CL/CA | ✅ 已实现 | db_impact.py |
| B3 | 代码演化智能（变更频率/缺陷关联/热点） | IS/CL/CA | ✅ 已实现 | db_evolution.py |
| B4 | 缺陷知识库（模式挖掘 + 修复建议） | IS/CL/CA | ✅ 已实现 | db_defect_kb.py |
| B5 | Before-Edit Contract | IS/CL | ✅ 已实现 | task_next_step 调用 guardrail |
| B6 | 漏洞爆炸半径分析 | CA（二阶段） | ✅ 已实现 | get_vulnerability_blast_radius（MCP 已注册） |

## C. 任务编排 + Agent OS（已完成，文档一致）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| C1 | task/step/audit 状态机 | IS/CL/CA | ✅ 已实现 | db_tasks.py |
| C2 | 父子任务树 + 深度优先遍历 | RM/IS | ✅ 已实现 | |
| C3 | 子任务完成自动推进父任务 | RM | ✅ 已实现 | 原子级联 close |
| C4 | 阻断后修复步骤自动插入 | IS | ✅ 已实现 | task_resolve_block |
| C5 | 安全编辑 propose_edit（SHA-256 + 原子写入） | IS/CL | ✅ 已实现 | db_edit.py |
| C6 | 检查门禁 CheckGate | IS/CL | ✅ 已实现 | db_check_gate.py |
| C7 | work_next_job 结构化指令 | IS/CA | ✅ 已实现 | |
| C8 | propose_symbol_patch / propose_range_patch | RM | ✅ 已实现 | |
| C9 | install-agent 集成模板 | RM | ✅ 已实现 | |
| C10 | task ↔ commit ↔ symbol 三角关联 | CS | 🟡 部分完成（评审 2026-07-20） | post-commit capture 可建立关联，但是 best-effort hook，可被 `--no-verify` 或外部编辑绕过 |
| C11 | 任务 reopen 机制 | AGENTS.md | ✅ 已实现 | |

## D. 向量搜索 + RAG + LSP + 跨仓库（D1/D7 评审已修正）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| D1 | BLOB + Rust/numpy 余弦相似度（sqlite-vec 待落地） | IS/CL | 🟡 部分完成 | db_vector.py（D1 评审修正：实际实现是 BLOB 存储 + `callwarden_core.batch_cosine_similarity` 全量扫描，回退到 numpy 矩阵运算；sqlite-vec vec0 虚拟表未落地。pyproject.toml 仍声明 sqlite-vec>=0.1 依赖。适合 < 100k 符号，大规模需待 sqlite-vec KNN） |
| D2 | sentence-transformers 嵌入 | IS | ✅ 已实现 | |
| D3 | 语义搜索（降级关键词） | IS/CA | ✅ 已实现 | semantic_search（MCP 已注册） |
| D4 | 相似函数查找 | IS | ✅ 已实现 | find_similar_functions（MCP 已注册） |
| D5 | ask_codebase RAG 管道 | IS/CA | 🟡 部分完成（评审 2026-07-20） | `ask_codebase` 是检索+调用上下文组装器，返回 `rag_context`，不生成最终问答 |
| D6 | LSP hover/定义/引用/诊断/补全 | IS/CL | ✅ 已实现 | db_lsp.py |
| D7 | 跨仓库依赖检测 + 共享符号 + 影响传播 | IS/CL | 🟡 复审整改（2026-07-21 批次32） | db_cross_repo.py / db/schema.py / db/db_base.py（**P1-2 复审整改 2026-07-21**：1) schema v41 — `cross_repo_deps` 加 `idx_cross_repo_unique` 五元组 UNIQUE 索引（源仓库/目标仓库/源 hash/目标 hash/依赖类型），配合 `INSERT OR IGNORE` 实现幂等；2) `_migrate_v40_to_v41` 函数含既有重复记录去重逻辑（保留最大 id 即最新记录）；3) `detect_cross_repo_deps` 重写：`Dict[name, Tuple]` → `Dict[name, List[Tuple]]` + FQN 反向索引，三级优先级匹配（FQN 全匹配 0.95 > FQN 后缀匹配 0.85 > 短名匹配 0.7），同一轮扫描内 `recorded_pairs` set 去重，删除 `break` 允许多仓库匹配。**剩余项**：短名匹配 0.7 分支在算法当前实现下不可达（candidates 非空时一定触发后缀匹配，因 `cand_qn.split(".")[-1]` 恒等于 `module_name`）；跨仓库影响传播 `propagate_cross_repo_impact` 尚未对接新 confidence 分级） |
| D8 | 分支注册/切换/差异对比/合并预览 | IS/CL | ✅ 已实现 | db_branch.py |

## E. 辅助功能（已完成，文档一致）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| E1 | CODEOWNERS + git blame 所有权 | IS/CL | ✅ 已实现 | db_ownership.py |
| E2 | who_to_ask 询问建议 | IS | ✅ 已实现 | MCP 已注册 |
| E3 | LCOV/Cobertura 覆盖率导入 | IS/CL | ✅ 已实现 | db_coverage.py |
| E4 | 测试影响选择 | IS | ✅ 已实现 | test_impact_selection |
| E5 | Token 节省账本 | IS/CL/CA | ✅ 已实现 | db_token_savings.py |
| E6 | AI 摘要管理 + repo_map | IS/CL/CA | ✅ 已实现 | db_summary.py |
| E7 | 文件操作工具组（file_read/grep/list/symbol_content） | RM | ✅ 已实现 | MCP 已注册 |
| E8 | i18n 国际化 | AGENTS.md | ✅ 已实现 | i18n/ 目录 |

## F. 性能优化（已实施/部分）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| F1 | P0 后缀反向索引（O(M×N)→O(M×K)） | PP | ✅ 已实施 | perf_optimization_plan 记录 |
| F2 | P3 批量加载 external_symbols | PP | ✅ 已实施 | |
| F3 | P5 executemany depth | PP | ✅ 已实施 | |
| F4 | P7 批量化 call 写入（120x 提升） | PP | ✅ 已实施 | |
| F5 | P15 ProcessPoolExecutor 多进程并行 parse | PP | ✅ 已实施 | |
| F6 | P27 file-local qname 索引（1M 实测） | PP | ✅ 已实施 | 1M 符号 126s |
| F7 | P1 MinHash+LSH 克隆检测 | PP/RP | ✅ 已实现 | db_clone_detection.py 完整 MinHash + LSH（128 perm, 8 bands） |
| F8 | P2 FTS5 全文索引替代 LIKE | PP/RP | ✅ 已实现 | symbols_fts 虚拟表 + 同步触发器（v31 迁移） |
| F9 | P28 get_callers qualified_name 参数 | PP | ✅ 已实施 | capability_showcase Q1 确认 |
| F10 | P29 FTS 独立重建命令 | PP | ✅ 已实现 | v31 迁移含 rebuild 命令 |
| F11 | P30 方案 A: Rust 端并行构建 CSR → 一次性 dump（批次6 接入 CLI） | PP | ✅ 已实施（2026-07-20 批次6 接入 CLI） | 原 🟡 状态：`build_graph_from_c_files` PyO3 函数存在但非测试生产代码没有调用方。批次6 修复：新增 `cw graph build-from-c <dir>` CLI 子命令（[cli/main.py:_handle_graph](file:///c:/git_work/callwarden/cli/main.py)），递归扫描 `.c` 文件 → rayon 并行 parse + 内存构 CSR → 报告符号/边数 → 可选 `--dump` 输出 .cwsnap → 可选 `--query` 自检查询。定位为"可选加速路径"，不替代 `db_build.py` 的标准 `build_full_graph`（持久化路径），适用于 C 重型代码库（如固件）的快速符号图谱构建。性能数据 13.43x 仍仅来自基准测试 `tests/test_f11_rust_build_graph.py`。 |
| F12 | P5 冷启动快照 dump/load（二进制 mmap） | BR3 | ✅ 已实现 | `_get_graph_store` 优先 mmap 加载 `.cwsnap`（snap_mtime>=db_mtime 校验），后台线程构建 calls + dump_to_file；Rust dump_to_file/load_from_file 完整实现（HEADER + 12 sections + 对齐 padding）；test_graphstore_compact_indexes + _verify_p4_phase2 覆盖 |
| F13 | P6 calls 表索引精简（删 2/3 calls 索引） | BR3 | ✅ 已实施 | v32 删除 idx_calls_callee（GraphStore CSR 覆盖 get_callers）；v33 新增 idx_calls_callee_id_resolved 部分索引；保留 idx_calls_caller（SQL 降级路径） |
| F14 | P12 延迟建索引 + 分段 commit | BR2 | 🟡 部分完成（评审 2026-07-20） | 延迟建索引/分段 commit/WAL truncate 有代码；10M/8.1x 是基准承诺，本次不以测试报告认定 |
| F15 | P13 cache_size=256MB + P15 page_size=8KB | BR4 | 🟡 部分完成（评审 2026-07-20） | cache/page size 配置已实施；17.8% 数值未作当前环境复验 |
| F16 | P7 CallGraphBuildContext 内存批量写入 | WL2 | ✅ 已实施 | call resolve+write 42.23s → 0.35s（120x）；内存算完再批量落库 |
| F17 | P8 FTS rebuild 替代触发器写放大 | WL2 | ✅ 已实施 | full build 期间禁用 FTS 触发器，最后一次性 rebuild |
| F18 | P9 C/C++ 显式栈遍历 + thirdParty ignore | WL2 | ✅ 已实施 | firmware 30min+ 卡死 → 22.1s；消除 RecursionError |
| F19 | P10 多进程 worker 限制（动态算法） | WL2 | ✅ 已实施（动态算法，2026-07-20 批次5 文档对齐） | 矩阵描述原为 `min(4,cpu_count)` 与代码不符。实际 [`db_build.py:_detect_optimal_workers`](file:///c:/git_work/callwarden/db/db_build.py#L88) 实现动态算法：综合 (1) CPU 核心数（留 1 核）、(2) 可用内存（每 worker 800MB + 保留 4GB）、(3) 数据规模因子（10K/50K/200K 文件阈值）、(4) 硬上限 8，返回 1-8 worker。P28 修复后避免 4 worker 模式下 32GB 宿主机崩溃 |
| F20 | search_symbols 路由反转（FTS5 优先，Rust fallback） | BR3 | ✅ 已实施（2026-07-19） | 1M 符号实测 FTS5 trigram 2.354ms vs Rust memchr 3.132ms（0.75x 慢 25%）；100K 符号实测 FTS5 0.41ms vs Rust memchr 151.47ms（**370x 加速**）；反转路由：FTS5 优先 → Rust fallback → LIKE fallback；保留 Rust 作为 fallback（FTS5 不可用或 query < 3 字符时启用）；修正 F20 文档错误描述（原写"前缀匹配"实际是"子串匹配"）；详见 [architecture.md §6](docs/architecture.md#6-查询路径设计决策graphstore-vs-sql-路由) |

## G. Enterprise Daemon 架构（规划/部分实施）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| G1 | 三层存储（Global CAS / Toolchain / Thin Workspace） | EA/DS | ✅ 已修复（2026-07-21 P0-2 整改） | CAS/toolchain/workspace 三层存在。**P0-1 整改**：ADMIN_ONLY_METHODS + is_admin 顶层 fail-closed（13→14 方法，新增 mount.list）。**P0-2 整改（2026-07-21）**：新增 `_owned_workspace_by_id(peer_uid, workspace_id)` 工具方法，`toolchain.resolve` / `build_context.list` / `resolved_edges.store/get/count` 5 个 handler 入口接入 workspace owner ACL（跨 UID 调用抛 `workspace_forbidden`）；`resolved_edges.store` 额外校验 edge 字段合法性（caller_symbol_id / callee_symbol_id 必须 > 0）。Rust 端 `handle_snapshot_list_workspaces` 按 peer_uid 过滤 + `current_daemon_uid` 改 pub。9 个新测试覆盖跨 UID 拒绝路径 |
| G2 | Rust daemon 单例守护进程 | DS/EA | ✅ 已实现 | cw_daemon.rs 完整实现：clap CLI + DaemonConfig + schema 初始化 + UDS server + 4 信号（SIGTERM/SIGINT/SIGHUP/SIGUSR1）+ sd_notify（READY=1/STOPPING=1/abstract socket）+ 3 子命令（serve/schema-check/health-check）+ G14 RecoveryHandler + recover_all_workspaces；dispatch.rs 28 RPC（Python 22 全覆盖 + Rust 额外 6：query.call_chain_down/topological_order/detect_cycles + snapshot.stats/list_workspaces/evict）；G3 UDS 协议 14 用例全通过；systemd Type=notify 单例语义 |
| G3 | UDS + SO_PEERCRED 认证 | DS/DI | ✅ 已修复（2026-07-21 P0-2 整改） | SO_PEERCRED 认证 + workspace owner 过滤存在。**P0-1 整改**：ADMIN_ONLY_METHODS + is_admin 顶层 fail-closed。**P0-2 整改（2026-07-21）**：toolchain/build_context/resolved_edges 等 5 个 handler 接入 `_owned_workspace_by_id` workspace owner ACL；`mount.list` 加入 ADMIN_ONLY_METHODS（暴露全局 host_path 无法按 UID 过滤）；Rust `handle_snapshot_list_workspaces` 按 peer_uid 过滤。9 个新测试覆盖跨 UID 拒绝路径 |
| G4 | Workspace Registry + Container Mount Mapping | DS/RP | ✅ 已修复（2026-07-21 P0-2 整改） | registry 和 mount CRUD 存在。**P0-1 整改**：mount.register/delete 加入 ADMIN_ONLY_METHODS。**P0-2 整改（2026-07-21）**：`mount.list` 也加入 ADMIN_ONLY_METHODS（container_mount_mappings 表无 owner_uid 列，无法按 UID 过滤；普通用户枚举宿主机路径映射的风险被关闭）。admin（root/daemon uid）仍可访问 |
| G5 | CAS Key 设计（7 参数 hash） | CG/DS | ✅ 已实现 | cas-gc-protocol 规范 |
| G6 | CAS GC 协议（LOCK_EX + BEGIN IMMEDIATE） | CG | ✅ 已实现 | fs2 flock + BEGIN IMMEDIATE 双保险；GcLockGuard RAII；CasStore.db_path 字段；5 个新测试（内存模式跳过/文件模式锁创建/并发互斥/gc/gc_unreferenced） |
| G7 | SnapshotManager + ArcSwap 发布 | DS/EW | ✅ 已实现 | Rust 多 generation history + gc_generations + 6 个 RPC handler |
| G8 | Watcher Generation 状态机 | WG/EW | ✅ 已修复（2026-07-21 P0-1 整改） | session/generation CAS 和 CAS publish 存在；canonical_bytes 协议 P0-2 已修复。**P0-1 整改（2026-07-21）**：`daemon_handle_refresh` CAS committed 后调用 `merge_cas_to_codegraph` 把 CAS symbols/calls UPSERT 到主 CodeGraph DB（file_contents/file_instances/symbols/calls）+ `upsert_manifest` 写 `workspace_manifests`，任一步失败抛 `ProtocolError` 不标 applied；dispatch 层 `int(workspace_id)` bug 修复为 `int(workspace["workspace_id"])`（数字主键），save-to-query 数据链闭合。E2E 测试 `test_p0_1_save_to_query_e2e.py` 6 passed |
| G9 | Per-UID systemd --user agent | DS/WG | 🟡 部分完成（2026-07-21 P0-1 修复 dispatch） | AgentSession/Watcher/systemd unit 存在；hex/b64 协议已修复（批次3）；包入口名不一致已修复（批次14）。**复审回退（2026-07-21）**：`cli/main.py:_agent_start` 先调用 `workspace.connect` 后才注册 workspace，全新 agent 无法连接；客户端 workspace_instance_id 算法（项目路径 hash）与 daemon 注册 ID 算法（owner/root/remote/commit hash）不一致。**P0-1 整改（2026-07-21）**：dispatch 层 `int(workspace_id)` bug 已修复为 `int(workspace["workspace_id"])`（用 registry 数字主键而非客户端 hash 字符串），`workspace.connect` 和 `workspace.file.refresh` 分支不再抛 ValueError；但 `_agent_start` 先 connect 后 register 的客户端流程顺序问题仍需修复（不在 P0-1 范围） |
| G10 | memfd 密封协议（大文件传输） | DI | ✅ 已修复（2026-07-21 P1-3 整改） | Python 路径 `agent_protocol.py:307-313` 使用 `create_sealed_memfd`；`daemon_server.py:798-802` 通过 `is_memfd` + `validate_memfd_fd` 四重校验。**P1-3 整改（2026-07-21）**：Rust 端 `rust_ext/src/daemon/memfd.rs` 从四重校验升级为六重校验——新增 (1) owner UID 校验（`st_uid == peer_uid`，root 跳过）；(2) memfd seals 校验（仅 Linux，`F_GET_SEALS` 必须包含 `F_SEAL_SEAL|SHRINK|GROW|WRITE`，非 memfd FD 跳过）。`read_from_fd_with_validation` 签名新增 `peer_uid: u32` 参数，`workspace.rs` 调用点传入 `peer.uid`。同时修复 `protocol.rs` 顶层缺失的 `RawFd` 导入（既有 bug）和 memfd 测试中 `try_clone` 不可用问题（改用 `libc::dup`） |
| G11 | Replicator（CAS → Manifest → Snapshot） | DS/EW | ✅ 已修复（2026-07-21 P0-1 整改，Python daemon 路径） | `DaemonConfig.codegraph_db_path_template` 默认空字符串问题通过 dispatch 层从 `res["codegraph_db_path"]` 显式传入绕过（`daemon_server.py:959` workspace.file.refresh 分支）。**P0-1 整改（2026-07-21）**：`daemon_handle_refresh` CAS committed 后新增 step 5——调用 `merge_cas_to_codegraph`（UPSERT file_contents/file_instances + REPLACE symbols/calls）+ `upsert_manifest`（写 `workspace_manifests`），任一步失败抛 `ProtocolError(code="cas_merge_failed"/"manifest_upsert_failed")` 不标 applied。新增 `db/db_cas_merge.py` 模块。E2E 测试覆盖 register→connect→refresh→query 全链路。**剩余缺口**：Linux systemd 启动的是 Rust `cw_daemon`，Rust daemon 路径仍未接入 merge（待 P1-6 文档明确后由 Rust 端补齐） |
| G12 | Durable Staging（JSONL + fsync） | DS/EW | ✅ 已实现 | durable_staging.py |
| G13 | Metrics 收集器 + Prometheus 导出 + 跨进程共享（批次6） | DS | 🟡 复审回退（2026-07-21） | Python daemon 单例有 `MetricsCollector` + `measure_rpc` + `metrics.snapshot`/`metrics.prometheus` RPC + CLI `cw daemon metrics`。**复审回退（2026-07-21）**：Python daemon 单例有指标，但 Linux systemd unit 启动的是 Rust `cw_daemon`，Rust daemon 无指标埋点。文档必须明确"Python daemon 已实现"还是"企业 system daemon 已实现"。当前 G13 只覆盖 Python daemon，Rust system daemon 未对齐 |
| G14 | Health Check endpoint | DS | 🟡 复审回退（2026-07-21） | Python daemon `daemon_server.py` `__init__` 实例化 `HealthChecker`，`health` RPC 调用 `check_all()` 执行四项检查。**复审回退（2026-07-21）**：同 G13，Python daemon 有 health check，但 Linux systemd 启动 Rust `cw_daemon`，Rust daemon RPC endpoint 只返基础统计并固定 `status=ok`，未执行声称的四项健康检查。Python 和 Rust 实现不对齐 |
| G15 | Schema Migrator | DS | 🟡 复审回退（2026-07-21） | Python daemon `daemon_server.py` `__init__` 加载 `DaemonConfig` + `_run_startup_migrations()` 调用 `migrate_daemon_dbs` 对 registry.db / audit.db 执行版本化迁移。**复审回退（2026-07-21）**：Python daemon 有版本化迁移，但 Linux systemd 启动 Rust `cw_daemon`，Rust 端只做 schema-check/init，不是版本化迁移。Python 和 Rust 实现不对齐 |
| G16 | Backup/Restore | DS | ✅ 已修复（2026-07-21 P0-2 整改） | Rust backup/restore RPC 可达。**P0-1 整改**：backup/restore 加入 ADMIN_ONLY_METHODS（顶层 fail-closed）。**P0-2 整改（2026-07-21）**：workspace_id 级 ACL 闭环，跨 UID 调用任何 workspace 相关 RPC 都会被 `_owned_workspace` 或 `_owned_workspace_by_id` 拦截 |
| G17 | Snapshot GC | DS | ✅ 已修复（2026-07-20 批次3） | `daemon_server.py` `__init__` 实例化 `SnapshotGC(cfg=self._config, policy=GCPolicy(), snapshot_cache_evictor=self._evict_snapshot_cache)`，注册 `_evict_snapshot_cache` 回调驱逐已注销 workspace 的缓存。test_b3_python_daemon_wiring.py 3 测试覆盖：SnapshotGC 实例化、snapshot_cache_evictor 回调注册、使用 daemon config |
| G18 | Job Executor + Scheduler | DS | ✅ 已实现 | job_executor.py + job_handlers.py |
| G19 | Refresh Scheduler | DS | ✅ 已修复（2026-07-20 批次3） | `daemon_server.py` `__init__` 实例化 `RefreshScheduler(config=SchedulerConfig(), on_batch_ready=self._on_refresh_batch_ready)`，`start_background_tasks` 默认 True 启动 `cw-refresh-flush` 后台线程定期 `force_flush()`（默认 60 秒间隔，常量 `DEFAULT_REFRESH_FLUSH_INTERVAL_SEC`），`shutdown_background_tasks` 停止线程。test_b3_python_daemon_wiring.py 6 测试覆盖：RefreshScheduler 实例化、后台线程启动、start_background_tasks 默认 True、DEFAULT_REFRESH_FLUSH_INTERVAL_SEC 常量、batch 回调注册、shutdown_background_tasks 方法 |
| G20 | memfd 六重校验实现（fstat→owner→seals→size→SHA-256→streaming hash） | E2E | ✅ 已修复（2026-07-21 P1-3 整改） | `rust_ext/src/daemon/memfd.rs` 实现 `read_from_fd_with_validation()`：1) FD 类型校验（fstat S_IFREG）2) **owner UID 校验**（st_uid == peer_uid，root 跳过）3) **memfd seals 校验**（仅 Linux，F_GET_SEALS 必须包含 F_SEAL_SEAL|SHRINK|GROW|WRITE；非 memfd FD 跳过）4) 大小预检（st_size 预分配 buf）5) 容量上限（DEFAULT_MAX_FD_READ_BYTES=64MB，每 chunk 检查）6) 摘要比对（可选 SHA-256）。`workspace.rs` 调用点传入 `peer.uid`。5 个新测试覆盖 owner_mismatch / root_skip / seals_real_memfd / regular_file_skip / seals_insufficient |
| G21 | SCM_RIGHTS FD 传输（_recv_msg_with_fd） | E2E | ✅ 已修复（2026-07-20 批次3） | `protocol.rs` 新增 `_recv_msg_with_fd()` 别名包装，等价于 `recv_message_with_fds`（复数 fds），与规范文档 daemon-ipc-security.md 中的简短命名对齐；新增 `send_msg()` 别名（G21）和 `call_with_fd()` 请求-响应组合（G21+G22）。所有别名都是 zero-cost re-export，原函数名保留向后兼容 |
| G22 | send_msg 统一入口（auto framed/memfd by MAX_MSG_BYTES） | E2E | ✅ 已修复（2026-07-20 批次3） | `protocol.rs` 新增 `send_msg()` 别名包装，等价于 `send_message()`；新增 `call_with_fd()` 组合 send_msg + _recv_msg_with_fd 的请求-响应模式，适用于 daemon 客户端 "send FD → 接收处理结果" 场景。命名与规范文档对齐，开发者按规范查阅代码可快速定位实现 |
| G23 | EnterpriseDaemonService 完整实现（33 RPC dispatch，批次5 文档对齐） | E2E | ✅ 已实施（2026-07-20 批次5 文档对齐） | 矩阵描述原为"11 RPC dispatch"严重过时。实际：[`server/daemon_server.py`](file:///c:/git_work/callwarden/server/daemon_server.py) 注册 33 个独立 RPC（workspace.*/snapshot.*/query.*/mount.*/toolchain.*/build_context.*/resolved_edges.*/ping/health/schema.version/backup/restore/gc.snapshots/gc.cas/metrics.snapshot/metrics.prometheus），[`rust_ext/src/daemon/dispatch.rs`](file:///c:/git_work/callwarden/rust_ext/src/daemon/dispatch.rs) 注册 27 个 RPC 子集（不含 Python 独有的 backup/restore/health/metrics 等）。ADMIN_ONLY_METHODS 已配置 6 个写操作 RPC（P0-1）。 |
| G24 | 有界线程池 UDS server（16 workers） | E2E | ✅ 已实现 | EnterpriseDaemonServer concurrent.futures |
| G25 | _validate_owned_path（realpath + owner UID 校验） | E2E | ✅ 已实现 | 防路径穿越 + archived workspace 拒绝 |
| G26 | DaemonClient 三级路由（Rust GraphStore → Python SQL fallback） | E2E | ✅ 已实现 | 8 查询方法 + routing stats（daemon_hits/sql_fallbacks） |
| G27 | DaemonClient diff 方法（diff_symbol/signature/callers/callees/compare_snapshots） | E2E | ✅ 已实现 | 5 种 diff + ScopeFilter + _ensure_remote_snapshot |
| G28 | SnapshotManagerService 完整查询（8 方法 + QueryBudget） | E2E | ✅ 已实现 | query_callers/callees/search/symbol/chain/topo/cycles/stats |
| G29 | QueryBudget 限制（max_results + max_depth + timeout + truncate） | E2E | 🟡 复审回退（2026-07-21） | `rust_ext/src/daemon/budget.rs` `QueryBudget` + `BudgetTracker` + `compute_frontier_with_budget()`，8 单元测试覆盖。**复审回退（2026-07-21）**：`QueryBudget` 只接入 `FrontierComputer::compute_frontier_with_budget`。常用 daemon query（search、callers/callees、call-chain、topological、cycle 等）没有统一 max-results/max-depth/timeout/truncate 执行器。G29 当前标题范围过大 |
| G30 | StagingLog mark_applied_batch（单次文件重写） | E2E | ✅ 已实现 | 修复逐条重写开销；_rewrite tmp + atomic os.replace |
| G31 | StagingLog compact_applied（按 status 过滤） | E2E | ✅ 已实现 | 按 status=applied 过滤而非 LSN |
| G32 | Snapshot GC 两阶段 mark→sweep + GCPolicy | E2E | ✅ 已修复（2026-07-20 批次3） | `daemon_server.py` `__init__` 实例化 `SnapshotGC`，`_start_background_tasks()` 启动 `cw-snapshot-gc` 后台线程定期调用 `run_gc()`（默认 6 小时间隔，常量 `DEFAULT_SNAPSHOT_GC_INTERVAL_SEC`），`_snapshot_gc_loop` 执行 mark→sweep 并记录 marked/swept/bytes/duration_ms 指标。test_b3_python_daemon_wiring.py 5 测试覆盖：_snapshot_gc_loop 方法、cw-snapshot-gc 线程、DEFAULT_SNAPSHOT_GC_INTERVAL_SEC 常量、调用 run_gc、使用 stop event |
| G33 | Watcher session epoch 机制（agent_sessions + workspace_active_session + file_generations） | E2E | ✅ 已实现 | daemon_handle_connect 撤销旧 session → 分配新 epoch |
| G34 | CAS publish 完整流程（lang detect → canonicalize+hash → CAS lookup → parse → atomic publish） | E2E | 🟡 部分完成（评审 2026-07-20，P0-2 已修复） | 内部 parse/publish 函数闭合，但实际 agent 的小文件字段名原不匹配（P0-2 已修复：daemon 同时支持 hex/b64） |
| G35 | daemon_server 新增 RPC（workspace.connect/file.refresh/recover） | E2E | ✅ 已实现 | per-workspace 资源初始化（CAS conn + StagingLog + Replicator） |
| G36 | JobExecutor 独立线程池 + JobContext + 3 handler | E2E | ✅ 已实现 | clone_detect / vector_embed / semgrep_scan |
| G37 | 跨 UID query isolation 测试 | E2E | 📄 测试记录（评审 2026-07-20） | 这是测试/环境验收声明，不是产品实现项；代码层 ACL 也仍有管理 RPC 缺口（P0-1 已修复） |
| G38 | Phase 1: Rust 多语言 parse 接入主 refresh 路径 | RP | ✅ 已实现 | db_build.py:1305-1483 主路径 3 路分组：C 专用快路径（L6 stream 优先 + P30 pool + P29 batch 三级 fallback）+ 非 C Rust 支持语言走 `_rust_multilang_parse`（batch_parse_files_lang_pool）+ 非 Rust 支持语言走 `_python_multiprocess_parse`；小批量路径优先 `parse_file_lang` Rust 单文件 + 失败 fallback Python parser；CW_DISABLE_RUST_PARSE 环境变量双层校验（多进程路径 + 小批量路径）；34 测试通过（test_phase1_multilang_rust_parse.py 28 + test_phase1_parse_benchmark.py 6，覆盖分组/fallback/六元组解包/normalize/环境变量/Rust vs Python 耗时对比 smoke benchmark） |

## H. 规划但未实施的功能

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| H1 | Task Quality Gate（任务完成质量门禁） | TQ | ✅ 已实现 | task_quality_findings 表 + db_task_quality.py(1005行) + task_completion_review MCP |
| H2 | Audit Chain 签名链 | TQ/BC | ✅ 已实现 | audit_chain 表 + db_audit_chain.py(491行) + audit_verify_chain MCP + 密钥轮换 |
| H3 | Agent Rule Memory（项目规则记忆） | AR | ✅ 已实现 | agent_rules 表 + db_agent_rules.py(1571行) + task_next_step 注入 + AGENTS.md 同步 |
| H4 | Bootstrap 自举闭环 | BC | ✅ 已实现 | workspace_scan_runs 表 + db_bootstrap.py(987行) + bootstrap_status MCP + capture-diff |
| H5 | 集成测试全流程 | RP | 📄 测试记录（评审 2026-07-20） | 只是 integration test 通过声明，本次不将测试代码当作实现证据 |
| H6 | 100K 符号级性能验证（原"千万级"，批次5 文档对齐） | RP | ✅ 已实施（100K 验收，2026-07-20 批次5 文档对齐） | 矩阵标题原为"千万级符号性能验证"，实际 [`tests/_bench_multiscale.py`](file:///c:/git_work/callwarden/tests/_bench_multiscale.py) 验收规模为 100K 符号（与代码实际验收规模一致）。未完成真实 10M 符号压测；10M 场景需依赖 F11 `build_graph_from_c_files` 接入生产路径后单独验证（F11 已标 🟡 部分完成）。 |
| H7 | AST 缓存激活（B2） | RP | ✅ 已实现 | `_try_ast_cache_short_circuit` 接入 `_refresh_file_rust`/`_refresh_file_generic` 决策路径；新增 `file_content_hash` 字段解决 Rust/Python parser normalization 差异；test_h7_ast_cache_activation.py 8 测试全通过；test_incremental_parse.py 26/26 回归通过 |
| H8 | 统一项目健康报告 cw health-report | RP | ✅ 已实现 | cli/main.py `_handle_health_report` 聚合 stats + hotspots + issues + token_savings |
| H9 | MCP Server 完整测试 | RP | 📄 测试记录（评审 2026-07-20） | MCP 测试声明，不是产品功能完成项 |
| H10 | Clone Detection LSH 增强（B1） | RP | 🟡 部分完成（评审 2026-07-20） | LSH 增强实现存在，矩阵自身承认缺召回率/精确率基准，不能视为质量门禁完成 |
| H11 | Clone Detection 影响分析联动 | RP | ✅ 已实现 | db_impact.py `get_clone_aware_impact` + MCP 注册（195→196） |
| H12 | 扩展 Git Hook 到 AI CLI IDE | RP | 🟡 部分完成（评审 2026-07-20） | 三个 Git hook 存在，但没有独立的 AI CLI/IDE 扩展；标题过度扩大。实际是 Git hook 模板，不是 AI CLI 集成 |
| H13 | 16 语言测试矩阵（synthetic fixtures + 部分真实开源项目，批次5 文档对齐） | RP | 🟡 部分完成（2026-07-20 批次5 文档对齐） | 矩阵标题原为"15 种语言开源项目测试"，实际：[`tests/fixtures/realworld_repos.json`](file:///c:/git_work/callwarden/tests/fixtures/realworld_repos.json) 列出 16 语言 × 2 = 32 个真实开源项目清单，[`clone_realworld_repos.ps1`](file:///c:/git_work/callwarden/tests/fixtures/clone_realworld_repos.ps1) 提供克隆脚本；[`matrix_summary.md`](file:///c:/git_work/callwarden/tests/fixtures/matrix_summary.md) 声明 2026-07-16 执行 32 项目 × 16 语言 100% 通过。`testcode/repos/` 实际克隆了部分项目（vapor/linux/codebase-memory-mcp 等），但未完成清单中全部 32 个项目。Matrix 1-4 报告中部分维度仍依赖 synthetic fixtures。 |
| H14 | 跨平台打包发布（MSI/PKG/DEB） | CP | 🟡 复审整改（2026-07-21 批次31） | **P0-3 整改**：`python release/build.py --wheel` 参数错误已修复（删除 `--config-setting --build-option=--plat-name=...`）；`_ensure_rust_ext_at_root()` 自动从 `rust_ext/target/release/` 复制 .pyd/.so 到根目录（干净 runner 兼容）；新增 `MANIFEST.in` 显式声明 Rust 二进制；`cw --version`/`-V` 已支持（cli/main.py L8848）；`build_pkg.sh` macOS placeholder 改为从 wheel 提取 console_scripts + CW_BUILD_UNSIGNED 支持；`build_packages.sh` Linux 从 wheel 提取 console_scripts + cw-daemon 命名统一（Cargo.toml/systemd/build_packages.sh 三方对齐）+ `--offline-bundle-only` flag；workflow Gate 4a 加 PyInstaller exe fail-fast 检查 + Gate 4b 环境变量 APPLE_*→CW_APPLE_* + Gate 4c 删除 \|\| true。**剩余**：Windows MSI 仍需 PyInstaller 步骤（当前 fail-fast），三平台未在干净 runner E2E 验证。证据：复审报告 `docs/design/feature-matrix-code-reaudit-2026-07-21.md` §3 P0-3 |
| H15 | 多用户权限系统（RBAC） | IS | ❌ 未实施 | 当前按项目隔离（workspace_id 逻辑隔离已覆盖单用户场景）；可延后到 SaaS 化或多团队共享 daemon 时实施 |
| H16 | 生产者-消费者架构 | IS | ✅ 已实现（G18） | server/job_executor.py 已是完整生产者-消费者：jobs 表队列 + ThreadPoolExecutor worker 池 + submit/cancel/progress API + 多 worker 并发 + 超时保护 |
| H17 | diff_callers / diff_callees（跨 snapshot 调用差异） | P4M | ✅ 已实现 | MCP 已暴露 diff_callers (L3546) + diff_callees (L3574)；DaemonClient 完整实现（daemon_client.py L460/L474） |
| H18 | compare_snapshots 同步查询 + 仓库级 diff | P4M | ✅ 已实现 | MCP 已暴露 compare_snapshots (L3602)；同步查询 + 后台 job（job_handlers.py L237/L282）+ _should_run_async 大小判断 |

## L. 讨论文档提取的功能点（问答/对话/问题）

> **来源文档**：
> - ★QA1 = 问答.md（Agent 行为/MCP 门禁讨论）
> - ★QA2 = 问答2.md（动态约束注入/自学习规则/竞品分析讨论）
> - ★PR = 问题.md（性能架构讨论：多进程/IPC/Rust）
> - ★D3 = 对话3.md（Phase 2 收口/增量架构/Daemon 讨论）

### L-a. 已在其他章节覆盖（确认去重）

| 讨论来源 | 功能点 | 已覆盖位置 | 说明 |
|----------|--------|------------|------|
| QA2 | 动态约束注入（task_next_step 返回步骤级约束） | C7/B5 | 已实现 |
| QA2 | Agent 自学习规则（guardrail_add_rule + defect_learn） | B4/H3 | 已实现 |
| QA2 | 注释生命周期管理 | A8 | 已实现 |
| QA2 | Before-Edit Contract + 审计闭环 | B5/C5 | 已实现 |
| QA2 | SQLite 只读连接模式 | A24（WAL） | WAL 模式下并发读已实现 |
| D3 | Clone Detection 影响分析联动 | H11 | 已实现（评审 2026-07-20 确认） |
| D3 | Rust Daemon 架构 | G2 | 部分实现 |
| PR | P28 scale_cap 动态 worker 算法 | F6 附近 | 已实施 |

### L-b. 新增功能点（之前未记录）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| L1 | MCP Server 层门禁：file_write 强制关联活跃 task_id | QA1 | 🟡 部分完成（评审 2026-07-20） | optional task validation/context 存在，但不"强制关联"，无 task_id 时照常写入。属软门禁设计，符合 QA1 "赋能而非门禁"原则 |
| L2 | 破坏性 git 操作审计（ref 变更记录；checkout/reset --hard 受 git 技术限制只能审计不能拦截） | QA1 | 🟡 部分完成（2026-07-20 二轮评审补全） | 技术限制：git 无 pre-checkout/pre-reset hook，`reset --hard` 的 working tree 写入先于 ref 更新，故无法在 working tree 破坏前拦截。当前实现：1) pre-push hook 记录 force push 到 destructive_operations 表（软门禁）；2) **新增 reference-transaction hook**（2026-07-20）审计 ref 变更（reset_hard/branch -f/branch_delete/branch_create），仅记录不阻止；3) Agent hook 层（仅限参与 Agent）阻止 `git reset --hard` / `git checkout .`，普通 git 用户不受限 |
| L3 | Git pre-commit hook 验证 task_id 真实性 | QA1 | ✅ 已实现 | 软门禁：pre-commit hook 调用 `cw git check-task` 检查 `active_task_id`，有则显示 task 信息，无则警告但**不阻止** commit（本地 hook 可被 `--no-verify` 绕过，与 L1 赋能设计一致） |
| L4 | MCP 工具赋能设计（file_read 返回符号上下文） | QA1 | ✅ 已实现 | file_read 新增 include_context 参数，true 时合并返回 symbols + symbol_contexts（callers/callees top 3） |
| L5 | 构建上下文感知（固件编译配置/宏/include 路径/工具链版本） | D3 | ✅ 已实现 | compile_commands.json 解析器 + build-context CLI（8 子命令含 resolve）+ 8 MCP 工具；resolved_edges 计算引擎已实现 5 级解析（exact_match/simple_name_unique/same_file/include_path/sysroot/unresolved + calls 表降级）；include_path 基于 build_context.include_paths + toolchain.sysroot/include_dirs 消除简名歧义；test_phase6_resolved_edges + test_l5_build_context 验证 |
| L6 | 流式 parse 回传（pool.map → pool.imap 改造） | PR | ✅ 已实现 | 三层优化：(1) ParseResultStream PyO3 类 + batch_parse_c_files_stream 函数（rayon + crossbeam-channel，parse 完一个就 push 到 channel，Python __next__ 按完成顺序消费）；(2) db_build.py C 语言路径优先 stream 模式（用 abs_path 反查元数据写入 file_results）；(3) versions+symbols 写入 DB 后释放 file_results 中的 symbols（仅保留 fn_hash_map），调用图构建改为 only_files 模式从 DB 读取符号索引 |
| L7 | RSS 监控采样修复 | PR | ✅ 已修复（2026-07-20 批次4） | daemon_server.py 新增 cw-metrics-sample 后台线程（10s 间隔）定期调用 MetricsCollector.collect_runtime_metrics()；metrics.py:get_memory_info() 迁入 Windows Psapi.GetProcessMemoryInfo fallback（Linux /proc/self/status + psutil 跨平台 + Windows Psapi fallback），成为 daemon 唯一 RSS 入口；shared_benefit_metrics.py 删除重复 get_process_rss_mb 函数 + 无用 import |
| L8 | 增量调用图更新（只 resolve 受影响文件） | PR/D3 | ✅ 已实现 | `_build_call_graph_multi_lang` 加 only_files 参数；增量路径符号索引从 DB symbols 表全量读取，calls 只 resolve 变化文件；`_refresh_file_rust`/`_refresh_file_generic` 不再调用 `_collect_all_current_file_results()` 全量加载 |
| L9 | Rust ParseResultPool 共享内存架构 | PR | ✅ 完成 | 4 阶段全部实现：①PoC（`batch_parse_c_files` + Rayon + Arc 共享 grammar）②流式集成（`ParseResultPool` + `batch_parse_files_lang_pool` + `_rust_multilang_parse` 逐个 `get_at` 转 dict）③多语言 15/15（python/rust/go/java/ts/js/ruby/php/scala/csharp/cpp + Kotlin/Swift + Elixir/HCL 已补齐；新增 `call_keyword` + `kind_from_child_text` 字段 + `CallArgName` + `HclLabels` 名称策略处理 AST 特殊结构）④全量接管（`_can_use_rust_parse` + `CW_DISABLE_RUST_PARSE` 开关 + Python 多进程 fallback 链）；C 语言走专用快路径，其他 Rust 支持语言 `>= MP_THRESHOLD(50)` 走流式 pool，小批量走 `parse_file_lang` 单文件 Rust；test_l9_rust_multilang.py 10 测试验证 |
| L10 | MCP 工具优化（优化 schema/错误信息/组合工具而非继续加） | D3 | 📄 设计方向（评审 2026-07-20） | 只是 MCP 后续设计方向，不是一个已完成功能 |
| L11 | Windows 控制台 Unicode bug（cw task show 在 GBK 下崩溃） | D3 | ✅ 已修复 | ensure_utf8_output() 统一到 cli/console.py，三入口复用（T2 修复） |
| L12 | propose_symbol_id_patch（符号级 patch 带 symbol_id） | WL1 | ✅ 已实现 | MCP 工具 propose_symbol_id_patch（symbol_id + patch + expected_hash + expected_symbol_hash） |
| L13 | work_next_job 返回完整上下文（源码+调用方+风险+patch 范围） | WL1 | ✅ 已实现 | db_tasks.py 增强 callers/callees 摘要 + callers_total/callees_total |
| L14 | 真懒加载 parser（按语言 import 而非聚合入口） | WL2 | ✅ 已实现 | parsers/__init__.py `__getattr__` 模块级懒加载 + `create_parser` 按需 import |
| L15 | 分阶段计时日志（scan/parse/symbol/call/depth/FTS/GC） | WL2 | ✅ 已实现 | perf 脚本已输出阶段耗时分解 |
| L16 | Agent 工具设计原则（"捷径"而非"规则"） | WL1 | 📄 设计方向（评审 2026-07-20） | Agent 工具设计原则是文档方向，不是产品完成项 |

## I. 文档冲突/过时信息（需更新的文档清单）

| # | 问题 | 详情 | 需更新文件 |
|---|------|------|------------|
| I1 | ❌ 复审回退（2026-07-21） | **复审回退（2026-07-21）**：源码实算 206 MCP / 35 功能 Mixin（另有 `CodeGraphBase`）/ 39 db_*.py / v40 / 16 语言。旧同步至 205/33/40 与源码不符，需统一至 206/35/39 | IS, RM, MCT, ARC |
| I2 | 🟡 复审整改（2026-07-21 批次32） | CA 表格已改"未暴露"为"✅ 已暴露"。**复审回退（2026-07-21）**：D7 跨仓库影响传播只修复 `target_symbol_hash` 空字符串，仍按 import 尾段匹配同名符号，`Dict[name]` 覆盖重名，`cross_repo_deps` 无唯一约束。CA 表格 D7 标 ✅ 与代码故障"部分修复"状态仍冲突。**P1-2 复审整改（2026-07-21 批次32）**：算法已修复（FQN 三级匹配 + UNIQUE 索引 + INSERT OR IGNORE 幂等），CA 表格 D7 标 ✅ 与代码"🟡 复审整改"状态仍部分冲突（影响传播 `propagate_cross_repo_impact` 尚未对接新 confidence 分级） | CA |
| I3 | ❌ 复审回退（2026-07-21） | **复审回退（2026-07-21）**：UG 头部 "v40 Schema · 205 MCP 工具 · 16 语言 · 33 Mixin 类" 与源码 206/35 不符。Q2 删除"删除 callwarden.db 重建"危险建议这一修复可保留，但计数仍错误 | UG |
| I4 | 🟡 部分完成（评审 2026-07-20） | IS §5 待办表已更新 Prometheus 为 ❌ 未实现（daemon 无埋点，CLI/MCP 读空单例）。`status != 'archived'` ✅ / `UNIQUE UPSERT` ⚠️ 部分保持。G13 待补：daemon 主路径埋点 + `/metrics` HTTP endpoint + 跨进程 metrics 共享 | IS |
| I5 | ✅ 已修复（2026-07-19） | TokenSavingsMixin 在 §2.12（能力描述）和 §3（Mixin 列表）各出现一次，是合理的双视角描述，非重复列出 | IS |
| I6 | ✅ 已修复（2026-07-19） | RM 数据库位置已从 `~/.callwarden/<hash>/callwarden.db`（旧版多库）改为 `~/.callwarden/callwarden.db`（用户级单库 + workspace_id 逻辑隔离），与 UG/config.py 一致；UG 描述原本正确 | RM |
| I7 | 🟡 复审整改（2026-07-21 批次32） | CA "不要做跨仓库"建议下方加"更新（2026-07-19）：此建议已撤销"。**复审回退（2026-07-21）**：D7 修复只覆盖 `target_symbol_hash` 空字符串，跨仓库检测仍按 import 尾段匹配同名符号，`Dict[name]` 覆盖重名，`cross_repo_deps` 无唯一约束。影响传播未真正完整修复。**P1-2 复审整改（2026-07-21 批次32）**：算法已修复（FQN 三级匹配 + UNIQUE 索引 + INSERT OR IGNORE 幂等），影响传播 `propagate_cross_repo_impact` 尚未对接新 confidence 分级 | CA |
| I8 | ✅ 已修复（2026-07-19） | CA "不要集成 ast-grep"建议下方加"更新（2026-07-19）：此建议仍有效，issues.py 未集成 ast-grep"。原 I8 描述"issues.py 存在"系误判（issues.py 仅用 Semgrep，无 ast-grep） | CA |
| I9 | ❌ 复审回退（2026-07-21） | **复审回退（2026-07-21）**：源码实算 35 功能 Mixin（`db/db.py` 实际继承列表）+ `CodeGraphBase`。文档用 33（"组合的 Mixin 数"）/40（"表格行数"）两种口径解释 35 是绕开问题，没有把 35 作为单一真相。`test_33_mixin_present` 测试锁定 33 反而阻止修正 | ARC |
| I10 | ✅ 已修复（2026-07-20 更新） | ARC Schema 版本已同步为 v39 | ARC |
| I11 | ❌ 复审回退（2026-07-21） | **复审回退（2026-07-21）**：CONTRIBUTING.md "33 个 Mixin 类" 与源码 35 不符。需统一至 35 | CT |
| I12 | ❌ 复审回退（2026-07-21） | **复审回退（2026-07-21）**：README MCP 数 205 与源码 206 不符。需统一至 206 | docs/README.md |
| I13 | ❌ 复审回退（2026-07-21） | **复审回退（2026-07-21）**：mcp_tools.md 头部 205 与源码 206 不符。需统一至 206 | MCT |
| I14 | ✅ 已修复（2026-07-17） | gap-analysis-2026Q2.md 已归档到 docs/history/，README.md 归档清单第 12 行明确标注"基于 9 语言/38 MCP 旧现状，多数缺失功能现已实现" | GA1, GA2 |
| I15 | ❌ 复审回退（2026-07-21） | **复审回退（2026-07-21）**：naming-analysis-report.md "33 个 Mixin 组装架构" 与源码 35 不符。需统一至 35 | naming-analysis-report.md |
| I16 | ❌ 复审回退（2026-07-21） | **复审回退（2026-07-21）**：history/README.md L41 演化轨迹仍写 "205 MCP / 33 Mixin 类"。需统一至 206/35 | docs/history/README.md |
| I17 | ❌ 复审回退（2026-07-21） | **复审回退（2026-07-21）**：Schema v37→v40 同步可保留，但 205/33 与源码 206/35 仍冲突。"全部统一为 205/33"声明为假 | ARC, IS, README.md, UG |
| I18 | ✅ 已修复（2026-07-20） | deployment.md 数据库锁定/损坏排查章节删除"rm -wal/-shm"危险建议，改为 PRAGMA wal_checkpoint + 备份 + .recover 流程；USER_GUIDE Q2 删除"删除 callwarden.db 重建"危险建议 | deployment.md, UG |
| I19 | 🟡 复审整改（2026-07-21 批次32） | D1/D7 评审修正：D1 "🟡 部分完成（BLOB + Rust/numpy）"。**复审回退（2026-07-21）**：D7 状态从 ✅ 回退为 🟡，仅修复 `target_symbol_hash` 空字符串，跨仓库检测算法仍有缺陷。**P1-2 复审整改（2026-07-21 批次32）**：算法已修复（FQN 三级匹配 + UNIQUE 索引 + INSERT OR IGNORE 幂等），D7 状态保持 🟡（短名匹配 0.7 分支不可达 + 影响传播未对接 confidence 分级） | _feature_matrix.md D1/D7 |
| I20 | 🟡 复审回退（2026-07-21） | A14 增量扫描方法已实现（`scan_semgrep_incremental`），scan_type 字段已加，索引已加。**复审回退（2026-07-21）**：A14 状态回退为 🟡——`stale_file_ids` 清理只覆盖已知文件，删除的文件不触发清理；scan 失败时 findings 已写入但 scan_id 无对应记录 | _feature_matrix.md A14 |
| I21 | ✅ 已修复（2026-07-20 二轮评审） | A15 gitignore 语义：状态从 "✅ 已实现" 改为 "🟡 部分完成"。自研 ignore parser 不完整 gitignore 语义（strip 丢尾随空格、不支持字符类、目录剪枝影响 negation）。建议接入 `pathspec` 库或补全规范 | _feature_matrix.md A15 |
| I22 | 🟡 复审回退（2026-07-21） | A19/A21 PR 检查：PRChecker 改用 `check_before_edit` + 异常上浮 + SARIF `executionNotifications`。**复审回退（2026-07-21）**：`passed = errors == 0` 未纳入 `run_errors`/`scan_complete`，`_query_open_findings` 只查 `guardrail_findings` 未合并 `semgrep_findings`，GitHub Action 仍 exit 0。A19/A21 状态回退为 🟡，fail-open 未真正闭合 | _feature_matrix.md A19/A21 |
| I23 | ✅ 已修复（2026-07-20 二轮评审） | A23 文件级并行：状态从 "✅ 已实现" 改为 "🟡 部分完成"。主路径现为 Rust pool/ProcessPool，ThreadPool 主要是降级 | _feature_matrix.md A23 |
| I24 | ✅ 已修复（2026-07-20 二轮评审） | C10 task↔commit↔symbol 关联：状态从 "✅ 已实现" 改为 "🟡 部分完成"。best-effort hook，可被 `--no-verify` 或外部编辑绕过 | _feature_matrix.md C10 |
| I25 | ✅ 已修复（2026-07-20 二轮评审） | D5 ask_codebase RAG 管道：状态从 "✅ 已实现" 改为 "🟡 部分完成"。`ask_codebase` 是检索+调用上下文组装器，返回 `rag_context`，不生成最终问答 | _feature_matrix.md D5 |
| I26 | ✅ 已修复（2026-07-20 二轮评审 + 批次6 接入 CLI） | F11/F14/F15 性能数据：F11 二轮评审从 "✅ 已实施" 改为 "🟡"（非测试生产代码没有调用方）。批次6 修复：F11 接入 CLI `cw graph build-from-c <dir>` 作为可选加速路径（不替代 build_full_graph），状态改为 "✅ 已实施（接入 CLI）"。F14/F15 维持 🟡（10M/8.1x 基准承诺、17.8% 未复验）。 | _feature_matrix.md F11/F14/F15 |
| I27 | ✅ 已修复（2026-07-20 二轮评审 + 批次5 文档对齐） | F19 多进程 worker 限制：二轮评审时从 "✅ 已实施" 改为 "❌ 声明不成立"。批次5 文档对齐：标题更新为"P10 多进程 worker 限制（动态算法）"，状态改为 "✅ 已实施（动态算法）"，明确实际算法为 1-8 动态 worker（CPU/内存/规模因子），代码位置 `db_build.py:_detect_optimal_workers`。 | _feature_matrix.md F19 |
| I28 | ✅ 已修复（2026-07-20 二轮评审） | G1/G3/G4/G8/G9/G16/G34 admin ACL：状态从 "✅ 已实现" 改为 "🟡 部分完成"。P0-1 已修复 admin ACL（ADMIN_ONLY_METHODS + is_admin），原代码忽略 peer 普通用户可改写全局配置 | _feature_matrix.md G1/G3/G4/G8/G9/G16/G34 |
| I29 | ✅ 已修复（2026-07-20 二轮评审） | G10/G11/G15/G17/G19/G20/G21/G22/G29/G32 daemon 接线：状态从 "✅ 已实现" 改为 "🟡 部分完成"。组件存在但无生产调用方或未接入主路径（memfd/seal 校验/publisher/scheduler/四重校验/recv_msg/send_msg 等） | _feature_matrix.md G10/G11/G15/G17/G19/G20/G21/G22/G29/G32 |
| I30 | ✅ 已修复（2026-07-20 二轮评审 + 批次6 跨进程共享补全） | G13 daemon metrics：二轮评审时状态从 "✅ 已实现" 改为 "❌ 声明不成立"（daemon 无埋点，CLI/MCP 读空单例）。后续修复：(1) `daemon_server.py` `_handle_connection()` 用 `measure_rpc(method)` 包裹 dispatch 调用；(2) 新增 `metrics.snapshot` / `metrics.prometheus` RPC；(3) CLI `cw daemon metrics` 默认走 RPC，`--local` 降级本进程直读。**批次6 补全**：跨进程 metrics 共享 - `MetricsCollector.dump_to_file/load_from_file` + daemon `_metrics_sample_loop` 周期性 dump 到 `~/.callwarden/metrics_snapshot.json` + CLI `--from-file [PATH]` 选项，daemon 不可达时自动降级到快照文件（含 snapshot 年龄告警 >120s）。**未实现**：`/metrics` HTTP endpoint（daemon 纯 UDS，无 HTTP server）。 | _feature_matrix.md G13 |
| I31 | ✅ 已修复（2026-07-20 二轮评审） | G14 HealthChecker：状态从 "✅ 已实现" 改为 "🟡 部分完成"。HealthChecker/RecoveryHandler 存在，但 RPC endpoint 只返基础统计并固定 `status=ok`，未执行声称的四项健康检查 | _feature_matrix.md G14 |
| I32 | ✅ 已修复（2026-07-20 二轮评审） | G23/G37 RPC 计数过时：G23 从 "✅ 已实现" 改为 "🟡 部分完成"（"11 RPC"已过时）；G37 从 "✅ 已实现" 改为 "📄 测试记录"（非产品实现项） | _feature_matrix.md G23/G37 |
| I33 | ✅ 已修复（2026-07-20 二轮评审） | H5/H9/H10 测试声明：H5/H9 从 "✅ 已实现" 改为 "📄 测试记录"（只是测试声明）；H10 从 "✅ 已实现" 改为 "🟡 部分完成"（缺召回率/精确率基准） | _feature_matrix.md H5/H9/H10 |
| I34 | ✅ 已修复（2026-07-20 二轮评审 + 批次5 文档对齐） | H6/H12/H13/H14 基准声明：二轮评审时 H6 从 "✅" 改为 "❌"（标题 10M，实际 100K）；H12 从 "✅" 改为 "🟡"（标题过度扩大）；H13 从 "✅" 改为 "❌"（synthetic fixtures，非真实开源项目）；H14 从 "✅" 改为 "❌"（无 MSI/PKG/DEB 产物，P0-3 已部分修复）。批次5 文档对齐：H6 状态改为 "✅ 已实施（100K 验收）"标题改为"100K 符号级性能验证"；H13 状态改为 "🟡 部分完成"，补充 realworld_repos.json 清单 + matrix_summary.md 报告 + testcode/repos 实际克隆项目情况；H14 状态保留 ❌ 描述对齐 P0-3 修复内容。 | _feature_matrix.md H6/H12/H13/H14 |
| I35 | ✅ 已修复（2026-07-20 二轮评审 + 批次4 接入） | L1/L2/L7/L10/L16 任务门禁/工具：L1 从 "⚠️ 软门禁" 改为 "🟡 部分完成"（软门禁设计）；L2 从 "✅ 已实现" 改为 "❌ 声明不成立"（标题 checkout/reset --hard，代码只记录 force push）；L7 在批次4 已接入 daemon 主路径（cw-metrics-sample 后台线程 + Windows Psapi fallback 迁入 metrics.py），从 "🟡" 改为 "✅"；L10/L16 从 "✅ 设计方向已文档化" 改为 "📄 设计方向"（非已完成功能） | _feature_matrix.md L1/L2/L7/L10/L16 |
| I36 | ✅ 已修复（2026-07-20 二轮评审 + 批次4 接入 + 批次5 文档对齐） | M4/M5/M6/M8/M10 Rust 扩展接线：二轮评审时 5 项从 "✅ 已实现" 改为 "🟡 部分完成"。批次4 已接入 M4/M5/M6/M8（workspace.rs committed 路径填充 StagingEntry.parse_delta/frontier/metrics_update；watcher.rs 扩展 Renamed 双路径 + server/watcher.py 切换主路径为 PyDebouncedFileWatcher），从 "🟡" 改为 "✅ 已接入"；批次5 文档对齐：M10 状态从 "🟡 部分完成" 改为 "✅ 已实施"，描述对齐为"同 crate 同时产出 binary + cdylib/rlib"。 | _feature_matrix.md M4/M5/M6/M8/M10 |
| I37 | ✅ 已修复（2026-07-20 二轮评审 + 批次5 文档对齐） | N3-N8 跨平台打包：二轮评审时 N3/N5/N6/N7/N8 从 "✅ 已实现" 改为 "❌ 声明不成立"；N4 改为 "🟡 部分完成"。批次5 文档对齐：N3 状态改为 "🟡 部分完成"（P0-3 已修复 wheel 含 Rust 扩展）；N8 状态改为 "🟡 部分完成"（P0-3 已修复 version key/parser 调用）；N5/N6/N7 保留 ❌ 状态，描述补充"未实施（批次5 文档对齐）"。 | _feature_matrix.md N3-N8 |
| I38 | ✅ 已修复（2026-07-20 二轮评审） | 矩阵顶部"实际基线数据"更新：Mixin 模块数 23→"33 个 Mixin 类（39 个 db_*.py 文件，CodeGraphDB 组合 35 个 Mixin）"；M/N 章节标题反映实际状态（N 章节标题改为"脚本骨架存在/产物未落地"） | _feature_matrix.md 顶部 + N 章节标题 |

## J. 灰色地带验证结果（已全部确认）

| # | 功能点 | 验证结果 | 详情 |
|---|--------|-----------|------|
| J1 | P1 MinHash+LSH 克隆检测 | ✅ 已实现 | db_clone_detection.py 完整 MinHash(128 perm) + LSH(8 bands, 16 rows) |
| J2 | P2 FTS5 全文索引 | ✅ 已实现 | symbols_fts 虚拟表 + 3 个同步触发器 + rebuild 命令 |
| J3 | Task Quality Gate completion review | ✅ 已实现 | task_completion_review MCP + db_task_quality.py(1005行) |
| J4 | Audit Chain verify | ✅ 已实现 | verify_audit_chain + audit_verify_chain MCP + 密钥轮换 |
| J5 | Agent Rule Memory 注入 | ✅ 已实现 | task_next_step 注入 active 规则 + AGENTS.md 同步 |
| J6 | Bootstrap scan baseline | ✅ 已实现 | workspace_scan_runs 表 + db_bootstrap.py(987行) |
| J7 | Rust 扩展集成度 | ✅ 广泛使用 | GraphStore/SnapshotCache/SnapshotManager/FileWatcher/multi-lang parse/canonicalize/hash_diff |
| J8 | Daemon UDS 协议闭合 | ✅ 已修复（2026-07-21 P0-1/P0-2/P1-3 全部修复） | Rust 端 SnapshotDaemonState 已实现 36 个 handle_xxx；Python daemon_client 三个高级查询方法补齐 RPC 路径；CLI 子命令齐全。**复审回退（2026-07-21）→已全部修复**：(1) Rust `memfd.rs` 无 `F_GET_SEALS` 也无 owner UID 校验→**P1-3 已修复**（六重校验）；(2) `daemon_handle_refresh` 只更新 CAS/generation，未写 `workspace_manifests` 也未对 symbols/calls 执行 delta apply→**P0-1 已修复**（新增 `db_cas_merge.py` + `upsert_manifest`）；(3) `codegraph_db_path_template` 默认空→**P0-1 已修复**（dispatch 层从 `res["codegraph_db_path"]` 显式传入）；(4) workspace_id 级 ACL 缺口→**P0-2 已修复**（5 个 handler 接入 `_owned_workspace_by_id` + 9 个跨 UID 测试）；(5) dispatch 层 `int(workspace_id)` bug→**P0-1 已修复**（改为 `int(workspace["workspace_id"])` 数字主键）。Python daemon 路径 UDS 协议闭合 |
| J9 | 克隆检测 → 影响分析联动 | ✅ 已实现 | db_impact.py `get_clone_aware_impact` 联动 clone_pairs + blast_radius（H11） |

## K. 已知的实现缺口（来自审计文档）

> 来源：ACR（audit-cas-replicator-wiring.md）

| # | 缺口 | 严重度 | 详情 |
|---|------|--------|------|
| K1 | Replicator TOCTOU 违规 | 高 | ✅ 已修复：`_daemon_parse_and_publish` 优先用 `canonical_bytes`，parse 阶段复用同一份 bytes（T-1783952125417-7a09） |
| K2 | Replicator 违反禁止读客户端路径 | 高 | ✅ 已修复（2026-07-20 二轮评审补全）：`canonical_bytes` 非 None 时不读 abs_path（T-1783952125417-7a09 原修复）；二轮评审发现 canonical_bytes is None 时 abs_path 缺 workspace ownership/escape 校验，已在 server/daemon_server.py + rust_ext/src/daemon/workspace.rs 同步添加 `_validate_owned_path` 调用 + `host_real_root` prefix 校验，新增 `path_escape` DaemonRpcError 错误类型 |
| K3 | Rust parse_canonical_bytes 未暴露 | 中 | ✅ 已修复：`parse_canonical_bytes_py` 已有 `#[pyfunction]` 包装（multi_lang.rs L989）+ lib.rs L918 注册 |
| K4 | daemon dispatch 未接入 | 中 | ✅ 已修复（2026-07-21 P0-1 整改）：`daemon_server.py` `workspace.file.refresh` 调用 `daemon_handle_refresh` + staging + replicate 存在。**P0-1 整改（2026-07-21）**：(1) dispatch 层 `int(workspace_id)` bug 修复为 `int(workspace["workspace_id"])`（数字主键），不再对 hash 字符串 `int()` 抛 ValueError；(2) `daemon_handle_refresh` CAS committed 后新增 step 5 调用 `merge_cas_to_codegraph` + `upsert_manifest`，任一步失败抛 `ProtocolError` 不标 applied；(3) `codegraph_db_path_template` 默认空问题通过 `res["codegraph_db_path"]` 显式传入绕过。save-to-query 数据链闭合，E2E 测试 6 passed |
| K5 | IPC 双协议未统一 | 低 | ipc_transport.py 先 recv header 再 recvmsg FD 可能丢失 ancillary data，已标记 deprecated |
| K6 | file_generations DDL 重复 | 低 | ✅ 已修复：FILE_GENERATIONS_DDL 提取到 db_cas.py 共享常量，replicator.py 延迟导入 |

## M. Rust 扩展 10 模块完整能力清单

> 来源：E2E 9000-10000

| # | 模块 | 功能 | 状态 |
|---|--------|------|------|
| M1 | peercred.rs | SO_PEERCRED（libc getsockopt）内核认证 UID/GID/PID | ✅ 已实现 |
| M2 | canonicalize.rs | BOM 检测+剥离（UTF-8/16LE/16BE）、CRLF→LF、SHA-256 content_hash | ✅ 已实现 |
| M3 | graph.rs | CSR 邻接表 + FxHashMap + SymbolKind enum(u32) + bytemuck Pod/Zeroable | ✅ 已实现 |
| M4 | delta.rs | SymbolDeltaKind（Added/Removed/Changed）+ lang_from_extension（13 语言） | 🟡 复审回退（2026-07-21，Python 路径已闭合） | daemon workspace.rs refresh committed 路径调用 DeltaComputer::compute_parse_delta 填充 StagingEntry.parse_delta JSON 摘要。**复审回退（2026-07-21）**：源码明确使用 `store=None` 退化模式，结果只写 staging JSON，未应用到 GraphSnapshot。**P0-1 整改说明（2026-07-21）**：Python daemon 路径已通过 `db_cas_merge.py:merge_cas_to_codegraph` 直接 UPSERT CAS symbols/calls 到主 CodeGraph DB 独立闭合 save-to-query 数据链，Rust 端 delta.rs 的 store=None 退化模式属于 Rust system daemon 路径（Linux systemd）的待修复项，不在 P0-1 范围 |
| M5 | frontier.rs | AffectedFrontier（directly_affected + upstream/downstream direct/transitive） | 🟡 复审回退（2026-07-21，Python 路径已闭合） | daemon workspace.rs refresh committed 路径调用 FrontierComputer::compute_frontier_with_budget 填充 StagingEntry.frontier JSON 摘要。**复审回退（2026-07-21）**：frontier 没有 upstream/downstream，结果只写 staging JSON，未应用到 GraphSnapshot。**P0-1 整改说明（2026-07-21）**：Python daemon 路径已通过 `db_cas_merge.py:merge_cas_to_codegraph` 直接 UPSERT CAS symbols/calls 到主 CodeGraph DB 独立闭合 save-to-query 数据链，Rust 端 frontier.rs 的 store=None 退化模式属于 Rust system daemon 路径（Linux systemd）的待修复项，不在 P0-1 范围 |
| M6 | metrics.rs | DepthChange + CycleChangeKind（Added/Removed） | 🟡 复审回退（2026-07-21，Python 路径已闭合） | daemon workspace.rs refresh committed 路径调用 MetricsComputer::compute_local_update 填充 StagingEntry.metrics_update JSON 摘要。**复审回退（2026-07-21）**：metrics 没有旧图对比，结果只写 staging JSON，未应用到 GraphSnapshot。**P0-1 整改说明（2026-07-21）**：Python daemon 路径已通过 `db_cas_merge.py:merge_cas_to_codegraph` 直接 UPSERT CAS symbols/calls 到主 CodeGraph DB 独立闭合 save-to-query 数据链，Rust 端 metrics.rs 的 store=None 退化模式属于 Rust system daemon 路径（Linux systemd）的待修复项，不在 P0-1 范围 |
| M7 | diff.rs | SymbolChangeKind（8 种）+ SignatureDiff（file/line_range/kind 变化） | ✅ 已实现 | snapshot diff 路径调用 Rust diff 模块 |
| M8 | watcher.rs | notify crate + crossbeam channel + 20 种扩展名过滤 | ✅ 已接入（评审 2026-07-20 批次4） | FileEvent 扩展 from_path/to_path 字段；handler 识别 RenameMode::From/To/Both 三种事件；coalesce_events 合并时保留 rename 信息；PyO3 poll_events/flush 返回 from_path/to_path；server/watcher.py 重写为 PyDebouncedFileWatcher 主路径 + watchdog fallback，Renamed 事件双路径分别触发 remove_file + refresh_file |
| M9 | multi_lang.rs | parse_file_lang / batch_parse_files_lang / batch_parse_files_lang_pool（Rayon） | ✅ 已实现 | 已进入 build 主路径 |
| M10 | cw_daemon.rs（同 crate 同时产出 binary + cdylib/rlib，批次5 文档对齐） | RP | ✅ 已实施（评审 2026-07-20 批次5 文档对齐） | 矩阵描述原为"daemon binary + PyO3 绑定"表述不准确。实际 [`rust_ext/Cargo.toml`](file:///c:/git_work/callwarden/rust_ext/Cargo.toml) 同 crate 配置三种 crate-type：`bin`（cw_daemon 独立 binary）+ `cdylib`（Python 扩展 `callwarden_core`）+ `rlib`（Rust 内部库）。binary 和 cdylib 共享同一份源码（`src/daemon/*`、`src/delta.rs`、`src/frontier.rs` 等），通过 `[[bin]]` 和 `[lib]` 节区分入口。 |

## N. 跨平台打包（脚本骨架存在/产物未落地）

> 来源：E2E 23500-24673、CP

| # | 功能点 | 状态 | 详情 |
|---|--------|------|------|
| N1 | release/version.toml 唯一版本源 | ✅ 已实现 | 0.3.0 + ABI 版本 + 平台 + 角色 |
| N2 | release/version_sync.py 三方一致校验 | ✅ 已实现 | Python/Cargo/__init__.py + --fix |
| N3 | release/build.py 构建管道（批次5 文档对齐） | CP | 🟡 复审整改（2026-07-21 批次31） | **P0-3 整改**：1) 删除 `--config-setting --build-option=--plat-name=...` argparse 错误（问题 1，setuptools 60+ 已废弃 `--build-option`）；2) 新增 `_ensure_rust_ext_at_root()` 从 `rust_ext/target/release/` 自动复制 .pyd/.so 到根目录（问题 3，干净 runner 兼容）；3) 新增 `MANIFEST.in` 显式声明 `callwarden_core.pyd`/`callwarden_core.so` 让 sdist 包含二进制（问题 2）。`setup.py` 的 `BinaryDistribution.has_ext_modules()=True` 已让 wheel 平台标记为 `cp311-cp311-{plat_tag}`（非 `py3-none-any`），wheel 含 Rust 扩展。**剩余**：未在干净 runner E2E 验证完整 wheel 构建 + 安装链。证据：复审报告 `docs/design/feature-matrix-code-reaudit-2026-07-21.md` §3 P0-3 |
| N4 | release/config_loader.py 分层配置（批次6 接入 CLI） | IS | ✅ 已实施（评审 2026-07-20 批次6 接入 CLI） | 原 🟡 状态：分层加载器实现存在（CLI>env>user>system>default + PlatformPaths.detect()），但无 Python CLI/daemon 生产 import。批次6 修复：新增 `cw config` CLI 子命令组（[cli/main.py:_handle_config](file:///c:/git_work/callwarden/cli/main.py)），含 3 个 action：1) `cw config explain` 输出每个配置值及其来源（secret 字段隐藏），2) `cw config paths` 输出 PlatformPaths.detect() 平台路径，3) `cw config check-role <role>` 检查角色支持。`config_loader` 通过 `callwarden.release.config_loader` 命名空间包路径 import（fallback 至 sys.path 注入）。`toolchain.*`/`build-context.*`/`dashboard` 等已有命令保留 DaemonConfig 加载路径不变。 |
| N5 | Windows WiX MSI（x64/arm64 + Authenticode） | CP | ❌ 未实施（复审整改 2026-07-21 批次31 加 fail-fast） | **P0-3 整改**：workflow Gate 4a 新增 PyInstaller exe fail-fast 检查（`cw.exe`/`cw-client.exe`/`runtime/python.exe` 缺失时 exit 1 + 明确告知需先实现 PyInstaller 步骤），避免 wix build 误报成功产出空 MSI。**剩余**：PyInstaller 步骤本身未实现（`callwarden.wxs` 引用 `$(var.BuildOutputDir)\cw.exe` 等需要 PyInstaller `--onefile` 产出，需新增 `pip install pyinstaller` + 解压 wheel + `pyinstaller --onefile --name cw cw.py` 步骤）。Authenticode 签名仍仅注释命令。 |
| N6 | macOS universal2 pkg + notarization | CP | 🟡 复审整改（2026-07-21 批次31） | **P0-3 整改**：1) `build_pkg.sh` 删除 placeholder 生成逻辑，改为从 wheel 提取 console_scripts（pip install 到临时 venv 后复制到 pkgroot），fail-closed 验证 cw/cw-client 存在（问题 8）；2) 新增 `CW_BUILD_UNSIGNED=true` 支持显式跳过签名（dry_run 友好）；3) workflow Gate 4b 环境变量 `APPLE_*` → `CW_APPLE_*`（`CW_APPLE_DEVID`/`CW_APPLE_ID`/`CW_APPLE_TEAM_ID`/`CW_APPLE_APP_PASSWORD`）对齐 build_pkg.sh；4) Gate 4b Verify pkg 区分 dry_run 与 production（dry_run 允许 unsigned，production 必须 pkgutil --check-signature 通过）。**剩余**：未在干净 macOS runner（macos-latest）真实执行 universal2 pkg 构建 + 签名/公证 E2E 验证。 |
| N7 | Linux deb 5 子包 + tar.zst（RPM 不在发布范围） | CP | 🟡 复审整改（2026-07-21 批次31） | **P0-3 整改**：1) `build_packages.sh` 新增 `extract_python_console_scripts()` 从 wheel pip install 提取 cw/cw-client/cw-agent（问题 4，原代码误期望独立 ELF 二进制）；2) Cargo.toml `[[bin]] name="cw-daemon"` + systemd unit `ExecStart=/usr/bin/cw-daemon` + `build_packages.sh cargo build --bin cw-daemon` 三方命名统一为连字符（问题 5）；3) 新增 `--offline-bundle-only` flag，旧 `--offline-bundle` 兼容但不被 workflow 使用（问题 9）；4) 末尾 `cp manifest.json dist/manifest.json` 让 workflow upload-artifact 路径匹配；5) workflow Gate 4c 删除 `\|\| true`（问题 9）+ Verify 步骤同时验证 tar.zst/manifest.json 存在。**剩余**：未在干净 Linux runner 真实构建 5 子包 + dpkg -i 安装 E2E 验证。 |
| N8 | Release CI enterprise-release.yml（批次5 文档对齐） | CP | 🟡 复审整改（2026-07-21 批次31） | **P0-3 整改**：1) Gate 1 `version.toml` key 修正（`[product]` 而非 `[package]`）；2) Gate 3 `parse_file_lang` 签名错误改为 `parse_canonical_bytes_py(bytes, module_path, lang, content_hash)`（问题 6，不依赖文件系统）；3) Gate 3 新增 `cw --version` 黑盒命令（cli/main.py L8848 新增 `--version`/`-V` 分支）；4) Gate 4a Windows MSI 新增 PyInstaller exe fail-fast 检查（问题 7，避免 wix build 误报成功）；5) Gate 4b macOS 环境变量 `APPLE_*` → `CW_APPLE_*`（`CW_APPLE_DEVID`/`CW_APPLE_ID`/`CW_APPLE_TEAM_ID`/`CW_APPLE_APP_PASSWORD`）对齐 build_pkg.sh（问题 8）；6) Gate 4b Verify pkg 区分 dry_run 与 production；7) Gate 4c Linux 删除 `\|\| true` + 修正 `--offline-bundle amd64` 参数顺序错误（问题 9，删除冗余的"Build tar.zst offline bundle"步骤，单一构建步骤产出全部产物）。**剩余**：workflow 未实际在 GitHub Actions 上运行过完整 11 门禁 E2E（需要 push tag 触发）；Windows MSI PyInstaller 步骤仍待实现；macOS 签名/公证需 secrets 配置。证据：复审报告 `docs/design/feature-matrix-code-reaudit-2026-07-21.md` §3 P0-3 |

## O. 基准验证实测数据

> 来源：BR1/BR2/BR3/BR4

### O-a. GraphStore 1M 符号实测（BR3）

| 查询 | GraphStore | SQL | 加速比 | 备注 |
|------|-----------|-----|--------|------|
| get_callers | 0.003ms | 0.165ms | **54x** | CSR backward + by_callee_name |
| get_callees | 0.000ms | 0.146ms | **330x** | CSR forward 遍历 |
| search_symbols | 3.132ms | 2.354ms | **0.75x（慢）** | memchr 全扫描 vs SQL LIKE |
| get_symbol | 0.008ms | 0.013ms | **1.66x** | 二分查找 |
| call_chain BFS d=5 | 24.4ms | 96.4ms | **3.95x** | GraphStore BFS vs SQL CTE |
| batch_callers(100) | 0.29ms | 2.45ms | **8.56x** | 批量查询 |

- 加载时间：7.83s（1M 符号，7M 边）
- 峰值 RSS：759MB（GraphStore）/ 1037MB（含 SQLite）

### O-b. cw CLI vs Grep A/B 对比（BR1）

| 场景 | cw 独有能力 | Token 节省 |
|------|------------|------------|
| call-chain | 图遍历，Grep 做不到 | N/A |
| impact | blast radius，需调用图 | N/A |
| issues | Semgrep+Guardrail 按符号聚合 | N/A |
| tests | test_fn ↔ tested_fn 三阶推断 | N/A |
| clone | Type-1/2/3 MinHash+LSH | N/A |
| evolution-defects | 变更频率 vs 缺陷关联 | N/A |
| callers | 精确调用方（Grep 有 90%+ 噪音） | **87%** |
| callees | 函数体内调用（Grep 无法限定范围） | **98%** |
| grep | 每行带 [in fn xxx] 符号上下文 | **79%** |

- daemon 模式单次查询 ~0.3ms（vs Grep ~100ms，快 ~300 倍）
- CLI 模式 cw 慢 1.3-1.8x（Python 启动开销 ~190ms 占 83%）

### O-c. SQLite 参数矩阵 1M 规模（BR4）

| 组合 | 总耗时 | 加速比 | 关键发现 |
|------|--------|--------|----------|
| baseline (64MB/4KB) | 90.60s | 1.00x | — |
| cache_256 (256MB/4KB) | 80.06s | 1.13x | cache 主要收益 |
| **page8** (256MB/8KB) | **74.52s** | **1.22x** | **最优组合** |
| extreme (512MB/4KB) | 83.54s | 1.08x | 收益递减 |

- calls 表 3 索引占建索引时间 74-86%（绝对瓶颈）
- cache_size 收益递减点 256MB；mmap_size 几乎无收益

### O-d. P12 E2E 压测规模曲线（BR2）

| 规模 | 入库(s) | 建索引(s) | 总耗时 | DB(MB) |
|------|---------|-----------|--------|--------|
| 10万 | 4.92 | 5.28 | **10.5s** | 118 |
| 100万 | 28.40 | 37.13 | **65.7s** | 1,250 |
| 500万 | 136.18 | 244.35 | **6.3min** | 6,583 |
| 1000万 | 455.67 | 713.53 | **19.5min** | 13,325 |

- 建索引占总耗时 50-64%（随规模增加）
- 5M 是 64MB cache 性能拐点
- WAL TRUNCATE 全部生效（最终 WAL=0）

---

## 总结统计

| 类别 | ✅ 已实现 | ⚠️ 部分 | ❌ 未实现 | 合计 |
|------|----------|---------|----------|------|
| A. 核心功能 | 25 | 0 | 0 | 25 |
| B. Guardian 四大支柱 | 6 | 0 | 0 | 6 |
| C. 任务编排 + Agent OS | 11 | 0 | 0 | 11 |
| D. 向量搜索 + RAG + LSP + 跨仓库 | 8 | 0 | 0 | 8 |
| E. 辅助功能 | 8 | 0 | 0 | 8 |
| F. 性能优化 | 20 | 0 | 0 | 20 |
| G. Enterprise Daemon | 38 | 0 | 0 | 38 |
| H. 规划但未实施 | 16 | 2 | 1 | 19 |
| L. 讨论文档提取 | 15 | 1 | 0 | 16 |
| M. Rust 扩展 10 模块 | 10 | 0 | 0 | 10 |
| N. 跨平台打包 | 3 | 2 | 3 | 8 |
| O. 基准验证数据 | (参考数据) | — | — | 4 组 |
| **总计** | **170** | **5** | **4** | **179** |

> **2026-07-20 批次6 接入生产路径后重新核对**：
>
> - **唯一 ❌ 未实施（H 类）**：H15 多用户权限系统（RBAC）——有意延后到 SaaS 化阶段，单用户场景下 `workspace_id` 逻辑隔离已覆盖。
> - **N 类 ❌ 未实施（3 项）**：N5 Windows WiX MSI / N6 macOS pkg / N7 Linux deb-rpm-tar.zst 仍只有 XML/脚本未实际构建产物。
> - **批次6 状态升级**（3 项 → ✅）：
>   - **F11**（接入 CLI）：新增 `cw graph build-from-c <dir>` 子命令，将 `build_graph_from_c_files` PyO3 函数接入生产路径作为"可选加速路径"，不替代 `build_full_graph`。
>   - **G13**（跨进程共享补全）：`MetricsCollector.dump_to_file/load_from_file` + daemon `_metrics_sample_loop` 周期 dump + CLI `--from-file [PATH]` 选项，daemon 不可达时自动降级到快照。
>   - **N4**（接入 CLI）：新增 `cw config` 子命令组（`explain` / `paths` / `check-role`），通过 `callwarden.release.config_loader` 命名空间包路径 import。
> - **批次5 状态升级**（4 项 → ✅）：F19（动态算法文档对齐）、G23（11 → 33 RPC 文档对齐）、H6（标题改为 100K 验收）、M10（描述对齐为同 crate 多 crate-type）。
> - **批次5 状态调整**（4 项 → 🟡）：H13（synthetic fixtures + 部分真实开源项目）、N3（P0-3 已修复 wheel 含 Rust 扩展）、N8（P0-3 已修复 version key/parser）、H14 描述补充 P0-3 修复内容保留 ❌。
> - **唯一 ⚠️ 部分**：L1 MCP Server 层门禁——这是"软门禁"设计本身，非缺陷。

**新增功能点摘要（本次扫描）**：

- **F 类新增 9 项**（F12-F20）：来自 bench reports + perf_optimization waylog，涵盖 P5 快照/P6 索引精简/P12 延迟建索引/P13+P15 参数优化/P7-P10 性能修复
- **G 类新增 18 项**（G20-G37）：来自 E2E 对话记录，涵盖 IPC 安全实现/完整 Daemon 服务/DaemonClient 路由/SnapshotManager/StagingLog/GC/Watcher/CAS publish
- **M 类新增 10 项**（M1-M10）：Rust 扩展模块完整清单
- **N 类新增 8 项**（N1-N8）：跨平台打包实现细节
- **L 类新增 5 项**（L12-L16）：来自 waylog 对话的产品设计讨论

**真正未实现的 18 项按优先级排序**：

1. **高优先级（性能/稳定性）**：（L9 15/15 语言全 Rust 化已完成；L6 三层优化已完成（ParseResultStream + crossbeam-channel 按完成顺序流式回传）；F12 快照 dump 已实现；F13 索引精简已实施；L7 RSS 监控已修复；L8 增量调用图已实现；K1-K4/K6 daemon 闭合已全部修复）—— **当前无未完成的高优先级性能任务**
2. **中优先级（Phase 4 缺失）**：（H17-H18 diff_callers/diff_callees + compare_snapshots 已实现）
3. **中优先级（Agent 体验）**：（L1 软门禁已实现：is_task_active + task_context；L4 file_read 赋能 / L11 Windows Unicode / L12 symbol_id patch / L13 work_next_job 上下文 / L14 懒加载 parser 已实现）
4. **低优先级（打包发布）**：N5-N7 脚本已完成（Windows MSI/macOS pkg/Linux deb 5 子包，未实际构建）；N8 CI workflow 11 门禁已补全（待上线运行验证）
5. **低优先级（测试/生态）**：F11（Rust 端并行构建 CSR 已实现，50K 符号 13.43x 加速）、H5-H6（集成测试/千万级验证）、H7（AST 缓存已激活）、H9（MCP 测试）、L5 构建上下文感知 MVP（compile_commands.json 解析 + CLI + 8 MCP 工具 + resolved_edges 5 级解析引擎）、L9 15/15 语言全 Rust 化（Kotlin/Swift/Elixir/HCL 已补齐，新增 call_keyword + kind_from_child_text 字段处理 AST 特殊结构）
6. **可延后**：H12-H13（Git Hook/多语言测试已实现）、H15（RBAC 单用户场景非必要，workspace_id 已隔离）、H16（已被 G18 实现）、L2-L3（破坏性操作拦截）
