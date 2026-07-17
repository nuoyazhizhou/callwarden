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
| MCP 工具数 | 120-125 | **196**（@mcp.tool() 装饰器计数） |
| CLI 子命令 | 145+ | 38 子命令 + ~98 个 --flag 命令 |
| 支持语言 | 16 | 16（parsers/ 目录 16 个解析器） |
| Mixin 模块 | 23 | **37**（db_*.py 文件数） |
| Schema 版本 | v14 | **v36** |

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
| A14 | 增量扫描 | IS | ✅ 已实现 | scan_semgrep_incremental |
| A15 | .gitignore 完整语法解析 | IS/CL | ✅ 已实现 | ignore_spec.py |
| A16 | .callwardenignore 项目级规则 | IS | ✅ 已实现 | |
| A17 | GC 归档/复活/状态/清除 | IS/CL | ✅ 已实现 | db_gc.py |
| A18 | build 末尾自动 Young GC | IS | ✅ 已实现 | |
| A19 | SARIF 导出 + GitHub Actions | IS/CL | ✅ 已实现 | cicd/ |
| A20 | 增量分析（CI/CD） | IS | ✅ 已实现 | incremental.py |
| A21 | PR 检查 | IS | ✅ 已实现 | pr_check.py |
| A22 | 安全修复 SEC-001~007 | IS/CL | ✅ 已实现 | 原子写入/LSP安全/日志消毒 |
| A23 | 文件级并行解析（ThreadPoolExecutor） | IS/CL | ✅ 已实现 | |
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
| C10 | task ↔ commit ↔ symbol 三角关联 | CS | ✅ 已实现 | post-commit hook |
| C11 | 任务 reopen 机制 | AGENTS.md | ✅ 已实现 | |

## D. 向量搜索 + RAG + LSP + 跨仓库（已完成，文档一致）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| D1 | sqlite-vec 向量索引 | IS/CL | ✅ 已实现 | db_vector.py |
| D2 | sentence-transformers 嵌入 | IS | ✅ 已实现 | |
| D3 | 语义搜索（降级关键词） | IS/CA | ✅ 已实现 | semantic_search（MCP 已注册） |
| D4 | 相似函数查找 | IS | ✅ 已实现 | find_similar_functions（MCP 已注册） |
| D5 | ask_codebase RAG 管道 | IS/CA | ✅ 已实现 | ask_codebase（MCP 已注册） |
| D6 | LSP hover/定义/引用/诊断/补全 | IS/CL | ✅ 已实现 | db_lsp.py |
| D7 | 跨仓库依赖检测 + 共享符号 + 影响传播 | IS/CL | ✅ 已实现 | db_cross_repo.py |
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
| F11 | P30 并行 INSERT | PP | ❌ 未实施 | 仅停留在计划阶段 |
| F12 | P5 冷启动快照 dump/load（二进制 mmap） | BR3 | ✅ 已实现 | `_get_graph_store` 优先 mmap 加载 `.cwsnap`（snap_mtime>=db_mtime 校验），后台线程构建 calls + dump_to_file；Rust dump_to_file/load_from_file 完整实现（HEADER + 12 sections + 对齐 padding）；test_graphstore_compact_indexes + _verify_p4_phase2 覆盖 |
| F13 | P6 calls 表索引精简（删 2/3 calls 索引） | BR3 | ✅ 已实施 | v32 删除 idx_calls_callee（GraphStore CSR 覆盖 get_callers）；v33 新增 idx_calls_callee_id_resolved 部分索引；保留 idx_calls_caller（SQL 降级路径） |
| F14 | P12 延迟建索引 + 分段 commit | BR2 | ✅ 已实施 | 10M 符号 19.5min（vs 基线 2h+，8.1x 加速）；WAL TRUNCATE 全生效 |
| F15 | P13 cache_size=256MB + P15 page_size=8KB | BR4 | ✅ 已实施 | 联合 17.8% 加速（90.60s → 74.52s @1M）；cache 收益递减点在 256MB |
| F16 | P7 CallGraphBuildContext 内存批量写入 | WL2 | ✅ 已实施 | call resolve+write 42.23s → 0.35s（120x）；内存算完再批量落库 |
| F17 | P8 FTS rebuild 替代触发器写放大 | WL2 | ✅ 已实施 | full build 期间禁用 FTS 触发器，最后一次性 rebuild |
| F18 | P9 C/C++ 显式栈遍历 + thirdParty ignore | WL2 | ✅ 已实施 | firmware 30min+ 卡死 → 22.1s；消除 RecursionError |
| F19 | P10 多进程 worker 限制 min(4, cpu_count) | WL2 | ✅ 已实施 | 每 worker ~300MB，4 进程 ~1.2GB（原 8 进程 ~2.4GB） |
| F20 | search_symbols 保留 SQL（GraphStore 反而慢 25%） | BR3 | ⚠️ 设计决策 | memchr 全扫描 vs SQL LIKE B-tree 索引；建议保留 SQL 或改 FTS5 |

## G. Enterprise Daemon 架构（规划/部分实施）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| G1 | 三层存储（Global CAS / Toolchain / Thin Workspace） | EA/DS | ⚠️ 部分 | CAS 表已建（db_cas.py 509行），Toolchain DB（db_toolchain.py 1028行），manifest 已有 |
| G2 | Rust daemon 单例守护进程 | DS/EA | ⚠️ 部分 | daemon_server.py + Rust cw_daemon.rs 已有 binary，但完整 UDS 协议未闭合 |
| G3 | UDS + SO_PEERCRED 认证 | DS/DI | ⚠️ 部分 | ipc_transport.py 已有 |
| G4 | Workspace Registry + Container Mount Mapping | DS/RP | ❓ 待验证 | daemon_workspaces 表已有 |
| G5 | CAS Key 设计（7 参数 hash） | CG/DS | ✅ 已实现 | cas-gc-protocol 规范 |
| G6 | CAS GC 协议（LOCK_EX + BEGIN IMMEDIATE） | CG | ❓ 待验证 | |
| G7 | SnapshotManager + ArcSwap 发布 | DS/EW | ⚠️ 部分 | snapshot_manager.py 已有 |
| G8 | Watcher Generation 状态机 | WG/EW | ⚠️ 部分 | watcher.py 已有，generation 协议部分 |
| G9 | Per-UID systemd --user agent | DS/WG | ❌ 未实施 | Linux 专属，Windows 不适用 |
| G10 | memfd 密封协议（大文件传输） | DI | ❌ 未实施 | Linux 专属 |
| G11 | Replicator（CAS → Manifest → Snapshot） | DS/EW | ⚠️ 部分 | replicator.py 已有 |
| G12 | Durable Staging（JSONL + fsync） | DS/EW | ✅ 已实现 | durable_staging.py |
| G13 | Metrics 收集器 + Prometheus 导出 | DS | ✅ 已实现 | metrics.py（691行完整实现） |
| G14 | Health Check endpoint | DS | ❓ 待验证 | health_check.py 已有 |
| G15 | Schema Migrator | DS | ✅ 已实现 | schema_migrator.py |
| G16 | Backup/Restore | DS | ✅ 已实现 | backup_restore.py |
| G17 | Snapshot GC | DS | ✅ 已实现 | snapshot_gc.py |
| G18 | Job Executor + Scheduler | DS | ✅ 已实现 | job_executor.py + job_handlers.py |
| G19 | Refresh Scheduler | DS | ✅ 已实现 | refresh_scheduler.py |
| G20 | memfd 四重校验实现（seals→size→SHA-256→streaming hash） | E2E | ✅ 已实现 | _validate_snapshot_fd + _sha256_streaming(64KB chunk) |
| G21 | SCM_RIGHTS FD 传输（_recv_msg_with_fd） | E2E | ✅ 已实现 | ancillary data 接收 + ProtocolError 处理 |
| G22 | send_msg 统一入口（auto framed/memfd by MAX_MSG_BYTES） | E2E | ✅ 已实现 | daemon_protocol.py 透明路径选择 |
| G23 | EnterpriseDaemonService 完整实现（11 RPC dispatch） | E2E | ✅ 已实现 | ping/workspace.register/list/status/snapshot.publish/query.* |
| G24 | 有界线程池 UDS server（16 workers） | E2E | ✅ 已实现 | EnterpriseDaemonServer concurrent.futures |
| G25 | _validate_owned_path（realpath + owner UID 校验） | E2E | ✅ 已实现 | 防路径穿越 + archived workspace 拒绝 |
| G26 | DaemonClient 三级路由（Rust GraphStore → Python SQL fallback） | E2E | ✅ 已实现 | 8 查询方法 + routing stats（daemon_hits/sql_fallbacks） |
| G27 | DaemonClient diff 方法（diff_symbol/signature/callers/callees/compare_snapshots） | E2E | ✅ 已实现 | 5 种 diff + ScopeFilter + _ensure_remote_snapshot |
| G28 | SnapshotManagerService 完整查询（8 方法 + QueryBudget） | E2E | ✅ 已实现 | query_callers/callees/search/symbol/chain/topo/cycles/stats |
| G29 | QueryBudget 限制（max_results + max_depth + timeout + truncate） | E2E | ✅ 已实现 | default_budget() + truncate_results 所有查询统一接入 |
| G30 | StagingLog mark_applied_batch（单次文件重写） | E2E | ✅ 已实现 | 修复逐条重写开销；_rewrite tmp + atomic os.replace |
| G31 | StagingLog compact_applied（按 status 过滤） | E2E | ✅ 已实现 | 按 status=applied 过滤而非 LSN |
| G32 | Snapshot GC 两阶段 mark→sweep + GCPolicy | E2E | ✅ 已实现 | retention=3/max_age=7d/batch=1000；5 类扫描范围 |
| G33 | Watcher session epoch 机制（agent_sessions + workspace_active_session + file_generations） | E2E | ✅ 已实现 | daemon_handle_connect 撤销旧 session → 分配新 epoch |
| G34 | CAS publish 完整流程（lang detect → canonicalize+hash → CAS lookup → parse → atomic publish） | E2E | ✅ 已实现 | _daemon_parse_and_publish 优先 canonical_bytes |
| G35 | daemon_server 新增 RPC（workspace.connect/file.refresh/recover） | E2E | ✅ 已实现 | per-workspace 资源初始化（CAS conn + StagingLog + Replicator） |
| G36 | JobExecutor 独立线程池 + JobContext + 3 handler | E2E | ✅ 已实现 | clone_detect / vector_embed / semgrep_scan |
| G37 | 跨 UID query isolation 测试 | E2E | ✅ 已实现 | 30 passed, 6 skipped；双 UID 隔离验证 |

## H. 规划但未实施的功能

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| H1 | Task Quality Gate（任务完成质量门禁） | TQ | ✅ 已实现 | task_quality_findings 表 + db_task_quality.py(1005行) + task_completion_review MCP |
| H2 | Audit Chain 签名链 | TQ/BC | ✅ 已实现 | audit_chain 表 + db_audit_chain.py(491行) + audit_verify_chain MCP + 密钥轮换 |
| H3 | Agent Rule Memory（项目规则记忆） | AR | ✅ 已实现 | agent_rules 表 + db_agent_rules.py(1571行) + task_next_step 注入 + AGENTS.md 同步 |
| H4 | Bootstrap 自举闭环 | BC | ✅ 已实现 | workspace_scan_runs 表 + db_bootstrap.py(987行) + bootstrap_status MCP + capture-diff |
| H5 | 集成测试全流程 | RP | ❌ 未实施 | 所有 checklist 未勾选 |
| H6 | 千万级符号性能验证 | RP | ❌ 未实施 | 1M 已测，10M 未测 |
| H7 | AST 缓存激活（B2） | RP | ✅ 已实现 | `_try_ast_cache_short_circuit` 接入 `_refresh_file_rust`/`_refresh_file_generic` 决策路径；新增 `file_content_hash` 字段解决 Rust/Python parser normalization 差异；test_h7_ast_cache_activation.py 8 测试全通过；test_incremental_parse.py 26/26 回归通过 |
| H8 | 统一项目健康报告 cw health-report | RP | ✅ 已实现 | cli/main.py `_handle_health_report` 聚合 stats + hotspots + issues + token_savings |
| H9 | MCP Server 完整测试 | RP | ❌ 未实施 | 所有 checklist 未勾选 |
| H10 | Clone Detection LSH 增强（B1） | RP | ✅ 已实现 | 3-gram shingle + _MAX_BUCKET_SIZE=200 + LSH(8 bands, 16 rows) + 降级策略 + 稳定 hash 全部就位；test_phase7_minhash_stable.py 覆盖稳定性；缺召回率/精确率基准测试 |
| H11 | Clone Detection 影响分析联动 | RP | ✅ 已实现 | db_impact.py `get_clone_aware_impact` + MCP 注册（195→196） |
| H12 | 扩展 Git Hook 到 AI CLI IDE | RP | ✅ 已实现 | `install.py:323-466` 三 hook 模板（pre-commit refresh-all + pre-push check-gate + post-commit capture-diff --auto）；marker 卸载机制保留用户其他 hook；`cw install --hooks` 统一入口；test_install_hooks_unified + test_git_hook_capture 覆盖 |
| H13 | 15 种语言开源项目测试 | RP | ✅ 已实现 | 16/16 语言全覆盖（test_p1_p3_languages 覆盖 PHP/Swift/Scala/HCL/Elixir；test_csharp_ruby；test_p9_c_parser_stack；test_p29_rust_parse；test_p31_multi_lang TS/Scala；test_kotlin_go 覆盖 Kotlin+Go 端到端：语言检测/工厂分发/符号提取/import/调用关系/db_build 集成） |
| H14 | 跨平台打包发布（MSI/PKG/DEB） | CP | ❌ 未实施 | 所有 checklist 未勾选 |
| H15 | 多用户权限系统（RBAC） | IS | ❌ 未实施 | 当前按项目隔离 |
| H16 | 生产者-消费者架构 | IS | ❌ 未实施 | 当前 Map-Reduce |
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
| D3 | Clone Detection 影响分析联动 | H11 | 未实现 |
| D3 | Rust Daemon 架构 | G2 | 部分实现 |
| PR | P28 scale_cap 动态 worker 算法 | F6 附近 | 已实施 |

### L-b. 新增功能点（之前未记录）

| # | 功能点 | 来源 | 状态 | 备注 |
|---|--------|------|------|------|
| L1 | MCP Server 层门禁：file_write 强制关联活跃 task_id | QA1 | ⚠️ 软门禁 | QA1 最终结论"赋能而非门禁"已落地：`db_tasks.is_task_active()` 校验真实性 + `get_task_context()` 赋能字段；`propose_edit` 系列（4 工具）在 `agent_task_id` 非空时返回 `task_validation`（valid/invalid/error）+ `task_context`（title/status/steps 概况）；软门禁语义：不拒绝写入，只在返回值标记；完全向后兼容（`agent_task_id=""` 时跳过） |
| L2 | 破坏性 git 操作拦截（git checkout/reset --hard） | QA1 | ✅ 已实现 | 软门禁设计（与 L1 一致）：pre-push hook 检测 force push（`git merge-base --is-ancestor`）并记录到 `destructive_operations` 表（schema v37）；`cw git check-push` 供 hook 调用；`cw git destructive-log` 查询历史；记录但不阻止操作 |
| L3 | Git pre-commit hook 验证 task_id 真实性 | QA1 | ✅ 已实现 | 软门禁：pre-commit hook 调用 `cw git check-task` 检查 `active_task_id`，有则显示 task 信息，无则警告但**不阻止** commit（本地 hook 可被 `--no-verify` 绕过，与 L1 赋能设计一致） |
| L4 | MCP 工具赋能设计（file_read 返回符号上下文） | QA1 | ✅ 已实现 | file_read 新增 include_context 参数，true 时合并返回 symbols + symbol_contexts（callers/callees top 3） |
| L5 | 构建上下文感知（固件编译配置/宏/include 路径/工具链版本） | D3 | ⚠️ 部分 | compile_commands.json 解析器 + build-context CLI（8 子命令含 resolve）+ 8 MCP 工具；resolved_edges 计算引擎已实现 5 级解析（exact_match/simple_name_unique/same_file/include_path/sysroot/unresolved + calls 表降级）；include_path 基于 build_context.include_paths + toolchain.sysroot/include_dirs 消除简名歧义 |
| L6 | 流式 parse 回传（pool.map → pool.imap 改造） | PR | ⚠️ 部分 | versions + symbols 写入 DB 后释放 file_results 中的 symbols 数据，调用图构建改为 only_files 模式从 DB 读取符号索引；parse 阶段流式回传（pool.imap）未实现 |
| L7 | RSS 监控采样修复 | PR | ✅ 已实现 | psutil 优先 + Windows ctypes Psapi.GetProcessMemoryInfo fallback（T3 修复） |
| L8 | 增量调用图更新（只 resolve 受影响文件） | PR/D3 | ✅ 已实现 | `_build_call_graph_multi_lang` 加 only_files 参数；增量路径符号索引从 DB symbols 表全量读取，calls 只 resolve 变化文件；`_refresh_file_rust`/`_refresh_file_generic` 不再调用 `_collect_all_current_file_results()` 全量加载 |
| L9 | Rust ParseResultPool 共享内存架构 | PR | ✅ 完成 | 4 阶段全部实现：①PoC（`batch_parse_c_files` + Rayon + Arc 共享 grammar）②流式集成（`ParseResultPool` + `batch_parse_files_lang_pool` + `_rust_multilang_parse` 逐个 `get_at` 转 dict）③多语言 15/15（python/rust/go/java/ts/js/ruby/php/scala/csharp/cpp + Kotlin/Swift + Elixir/HCL 已补齐；新增 `call_keyword` + `kind_from_child_text` 字段 + `CallArgName` + `HclLabels` 名称策略处理 AST 特殊结构）④全量接管（`_can_use_rust_parse` + `CW_DISABLE_RUST_PARSE` 开关 + Python 多进程 fallback 链）；C 语言走专用快路径，其他 Rust 支持语言 `>= MP_THRESHOLD(50)` 走流式 pool，小批量走 `parse_file_lang` 单文件 Rust；test_l9_rust_multilang.py 10 测试验证 |
| L10 | MCP 工具优化（优化 schema/错误信息/组合工具而非继续加） | D3 | ⚠️ 设计方向 | 讨论结论：195 个工具已够用，应优化组合查询路径而非继续扩功能面 |
| L11 | Windows 控制台 Unicode bug（cw task show 在 GBK 下崩溃） | D3 | ✅ 已修复 | ensure_utf8_output() 统一到 cli/console.py，三入口复用（T2 修复） |
| L12 | propose_symbol_id_patch（符号级 patch 带 symbol_id） | WL1 | ✅ 已实现 | MCP 工具 propose_symbol_id_patch（symbol_id + patch + expected_hash + expected_symbol_hash） |
| L13 | work_next_job 返回完整上下文（源码+调用方+风险+patch 范围） | WL1 | ✅ 已实现 | db_tasks.py 增强 callers/callees 摘要 + callers_total/callees_total |
| L14 | 真懒加载 parser（按语言 import 而非聚合入口） | WL2 | ✅ 已实现 | parsers/__init__.py `__getattr__` 模块级懒加载 + `create_parser` 按需 import |
| L15 | 分阶段计时日志（scan/parse/symbol/call/depth/FTS/GC） | WL2 | ✅ 已实现 | perf 脚本已输出阶段耗时分解 |
| L16 | Agent 工具设计原则（“捷径”而非“规则”） | WL1 | ⚠️ 设计方向 | 核心结论：高层工作流工具 > 底层工具集合；工具名/描述/参数影响 Agent 选择 |

## I. 文档冲突/过时信息（需更新的文档清单）

| # | 问题 | 详情 | 需更新文件 |
|---|------|------|------------|
| I1 | MCP 工具数严重过时 | IS 说 120，RM 说 125，MCT 头部说 173 又说 193，概览表说 179，ARC 说 166，实际 **195** | IS, RM, MCT, ARC, .cli_audit, .mcp_audit |
| I2 | competition-analysis "只差接线"已全部接线 | CA 说向量/任务/所有权/摘要/覆盖率"未暴露"，实际 MCP 全部注册 | CA |
| I3 | USER_GUIDE 严重过时 | UG 说"v3 Schema / 一个用户一个库"，实际 v36 / hash 隔离 | UG（建议废弃或重写） |
| I4 | implementation-status 自相矛盾 | IS 第五节说 Prometheus "待办"，但 metrics.py 已完整实现 | IS |
| I5 | IS TokenSavingsMixin 重复列出 | IS 第三节列出两次 | IS |
| I6 | 数据库路径描述不一致 | UG 说 ~/.callwarden/callwarden.db，IS/RM 说 ~/.callwarden/<hash>/callwarden.db | UG |
| I7 | CA "不要做跨仓库" 但已实现 | CA 建议不做跨仓库，但 db_cross_repo.py 已实现 | CA |
| I8 | CA "不要集成 ast-grep" 但 issues.py 存在 | CA 不建议，但实际 issues.py 可能已集成 | CA |
| I9 | ✅ 已修复（2026-07-17） | ARC Mixin 数已同步为 40 个，db_*.py 38 个文件 | ARC |
| I10 | ✅ 已修复（2026-07-17） | ARC Schema 版本已同步为 v37 | ARC |
| I11 | ✅ 已修复（2026-07-17） | CONTRIBUTING.md Mixin 数已同步为 40 | CT |
| I12 | ✅ 已修复（2026-07-17） | README.md MCP 工具数已同步为 204 | docs/README.md |
| I13 | ✅ 已修复（2026-07-17） | mcp_tools.md 头部已统一为 204；概览表 179 是 12 主分类独有工具合计，注释已说明与 204 的差异（跨分类工具 + 8 L5 工具） | MCT |
| I14 | ✅ 已修复（2026-07-17） | gap-analysis-2026Q2.md 已归档到 docs/history/，README.md 归档清单第 12 行明确标注"基于 9 语言/38 MCP 旧现状，多数缺失功能现已实现" | GA1, GA2 |
| I15 | ✅ 已修复（2026-07-17） | naming-analysis-report Mixin 数已同步为 40 | naming-analysis-report.md |
| I16 | ✅ 已修复（2026-07-17） | history/README.md 演化轨迹已扩展到 v37（含 v15-v25 治理期、v26-v33 优化期、v37 L2 破坏性操作）；implementation-snapshot-v13 归档原因已更新 | docs/history/README.md |

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
| J8 | Daemon 真实运行状态 | ⚠️ 部分 | cw_daemon.rs binary 已有，daemon_server.py + daemon_client.py 已有，但完整 UDS 协议未闭合 |
| J9 | 克隆检测 → 影响分析联动 | ✅ 已实现 | db_impact.py `get_clone_aware_impact` 联动 clone_pairs + blast_radius（H11） |

## K. 已知的实现缺口（来自审计文档）

> 来源：ACR（audit-cas-replicator-wiring.md）

| # | 缺口 | 严重度 | 详情 |
|---|------|--------|------|
| K1 | Replicator TOCTOU 违规 | 高 | ✅ 已修复：`_daemon_parse_and_publish` 优先用 `canonical_bytes`，parse 阶段复用同一份 bytes（T-1783952125417-7a09） |
| K2 | Replicator 违反禁止读客户端路径 | 高 | ✅ 已修复：`canonical_bytes` 非 None 时不读 abs_path，仅降级 fallback 使用（T-1783952125417-7a09） |
| K3 | Rust parse_canonical_bytes 未暴露 | 中 | ✅ 已修复：`parse_canonical_bytes_py` 已有 `#[pyfunction]` 包装（multi_lang.rs L989）+ lib.rs L918 注册 |
| K4 | daemon dispatch 未接入 | 中 | ✅ 已修复：daemon_server.py L396 `workspace.file.refresh` 调用 `daemon_handle_refresh` + staging + replicate |
| K5 | IPC 双协议未统一 | 低 | ipc_transport.py 先 recv header 再 recvmsg FD 可能丢失 ancillary data，已标记 deprecated |
| K6 | file_generations DDL 重复 | 低 | ✅ 已修复：FILE_GENERATIONS_DDL 提取到 db_cas.py 共享常量，replicator.py 延迟导入 |

## M. Rust 扩展 10 模块完整能力清单

> 来源：E2E 9000-10000

| # | 模块 | 功能 | 状态 |
|---|--------|------|------|
| M1 | peercred.rs | SO_PEERCRED（libc getsockopt）内核认证 UID/GID/PID | ✅ 已实现 |
| M2 | canonicalize.rs | BOM 检测+剥离（UTF-8/16LE/16BE）、CRLF→LF、SHA-256 content_hash | ✅ 已实现 |
| M3 | graph.rs | CSR 邻接表 + FxHashMap + SymbolKind enum(u32) + bytemuck Pod/Zeroable | ✅ 已实现 |
| M4 | delta.rs | SymbolDeltaKind（Added/Removed/Changed）+ lang_from_extension（13 语言） | ✅ 已实现 |
| M5 | frontier.rs | AffectedFrontier（directly_affected + upstream/downstream direct/transitive） | ✅ 已实现 |
| M6 | metrics.rs | DepthChange + CycleChangeKind（Added/Removed） | ✅ 已实现 |
| M7 | diff.rs | SymbolChangeKind（8 种）+ SignatureDiff（file/line_range/kind 变化） | ✅ 已实现 |
| M8 | watcher.rs | notify crate + crossbeam channel + 20 种扩展名过滤 | ✅ 已实现 |
| M9 | multi_lang.rs | parse_file_lang / batch_parse_files_lang / batch_parse_files_lang_pool（Rayon） | ✅ 已实现 |
| M10 | cw_daemon.rs | daemon binary + PyO3 绑定 | ✅ 已实现 |

## N. 跨平台打包完整实现

> 来源：E2E 23500-24673、CP

| # | 功能点 | 状态 | 详情 |
|---|--------|------|------|
| N1 | release/version.toml 唯一版本源 | ✅ 已实现 | 0.3.0 + ABI 版本 + 平台 + 角色 |
| N2 | release/version_sync.py 三方一致校验 | ✅ 已实现 | Python/Cargo/__init__.py + --fix |
| N3 | release/build.py 构建管道 | ✅ 已实现 | cargo build → setuptools wheel → wheelhouse → artifact-manifest.json |
| N4 | release/config_loader.py 分层配置 | ✅ 已实现 | CLI>env>user>system>default + PlatformPaths.detect() |
| N5 | Windows WiX MSI（x64/arm64 + Authenticode） | ⚠️ 部分 | callwarden.wxs 完成（perUserOrMachine + 数据保留 Feature + arm64 + PATH Feature），未实际构建 |
| N6 | macOS universal2 pkg + notarization | ⚠️ 部分 | build_pkg.sh 完成（hardened runtime + entitlements + notarytool + spctl + tar.gz），未实际构建 |
| N7 | Linux deb 5 子包 + rpm + tar.zst | ⚠️ 部分 | build_packages.sh + 5 control + 14 maintainer scripts + systemd/sysusers/tmpfiles + offline bundle 完成，未实际构建 |
| N8 | Release CI（enterprise-release.yml 11 门禁） | ⚠️ 部分 | 11 门禁已补全（源码测试→wheels→黑盒→MSI/pkg/deb→安装→N-1 升级→签名→SBOM→staging→批准→production），未上线运行 |

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
| F. 性能优化 | 17 | 1 | 1 | 19 |
| G. Enterprise Daemon | 26 | 4 | 2 | 32+ |
| H. 规划但未实施 | 11 | 1 | 6 | 18 |
| L. 讨论文档提取 | 10 | 5 | 1 | 16 |
| M. Rust 扩展 10 模块 | 10 | 0 | 0 | 10 |
| N. 跨平台打包 | 4 | 4 | 0 | 8 |
| O. 基准验证数据 | (参考数据) | — | — | 4 组 |
| **总计** | **136** | **13** | **10** | **161** |

**新增功能点摘要（本次扫描）**：

- **F 类新增 9 项**（F12-F20）：来自 bench reports + perf_optimization waylog，涵盖 P5 快照/P6 索引精简/P12 延迟建索引/P13+P15 参数优化/P7-P10 性能修复
- **G 类新增 18 项**（G20-G37）：来自 E2E 对话记录，涵盖 IPC 安全实现/完整 Daemon 服务/DaemonClient 路由/SnapshotManager/StagingLog/GC/Watcher/CAS publish
- **M 类新增 10 项**（M1-M10）：Rust 扩展模块完整清单
- **N 类新增 8 项**（N1-N8）：跨平台打包实现细节
- **L 类新增 5 项**（L12-L16）：来自 waylog 对话的产品设计讨论

**真正未实现的 18 项按优先级排序**：

1. **高优先级（性能/稳定性）**：（L9 15/15 语言全 Rust 化已完成；L6 部分实现：versions 写入后释放 symbols + 调用图从 DB 读取符号索引，parse 阶段流式回传未实现；F12 快照 dump 已实现；F13 索引精简已实施；L7 RSS 监控已修复；L8 增量调用图已实现；K1-K4/K6 daemon 闭合已全部修复）—— **当前无未完成的高优先级性能任务**
2. **中优先级（Phase 4 缺失）**：（H17-H18 diff_callers/diff_callees + compare_snapshots 已实现）
3. **中优先级（Agent 体验）**：（L1 软门禁已实现：is_task_active + task_context；L4 file_read 赋能 / L11 Windows Unicode / L12 symbol_id patch / L13 work_next_job 上下文 / L14 懒加载 parser 已实现）
4. **低优先级（打包发布）**：N5-N7 脚本已完成（Windows MSI/macOS pkg/Linux deb 5 子包，未实际构建）；N8 CI workflow 11 门禁已补全（待上线运行验证）
5. **低优先级（测试/生态）**：F11（并行 INSERT）、H5-H6（集成测试/千万级验证）、H7（AST 缓存已激活）、H9（MCP 测试）、L5 构建上下文感知 MVP（compile_commands.json 解析 + CLI + 8 MCP 工具，resolved_edges 计算引擎待实现）、L9 15/15 语言全 Rust 化（Kotlin/Swift/Elixir/HCL 已补齐，新增 call_keyword + kind_from_child_text 字段处理 AST 特殊结构）
6. **可延后**：H12-H13（Git Hook/多语言测试已实现）、H15-H16（RBAC/生产者-消费者）、L2-L3（破坏性操作拦截）
