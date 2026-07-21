# 实现状态总览

> 最后更新：2026-07-21 · Schema v41 · 35 功能 Mixin（+ CodeGraphBase，39 个 db_*.py 文件）· 206 MCP 工具 · 16 语言

本文档是 callwarden 当前能力的权威盘点，对照 [Guardian 规格](evolve-guardian-architecture/spec.md) + [战略分析](competition-analysis.md) + 实际代码逐项核查。历史盘点请参阅 [history/implementation-snapshot-v13.md](../history/implementation-snapshot-v13.md)。

## 一、总览

| 维度        | 数量 | 说明                                                                                      |
| ----------- | ---- | ----------------------------------------------------------------------------------------- |
| 支持语言    | 16   | Rust/TypeScript/JavaScript/Python/Kotlin/Go/Java/C/C++/C#/Ruby/PHP/Swift/Scala/HCL/Elixir |
| 数据库表    | 30+  | 含 5 张 Guardian 表 + 1 张 archived_files 归档表                                          |
| Schema 版本 | v41  | v3→v41 版本化迁移，事务化执行（v41: P1-2 cross_repo_deps 五元组 UNIQUE 索引）            |
| Mixin 模块  | 35   | CodeGraphBase + 35 个功能 Mixin（39 个 db_*.py 文件）                                     |
| MCP 工具    | 206  | FastMCP @mcp.tool() 注册                                                                  |
| CLI 命令    | 145+ | 子命令 + --flag 双风格                                                                    |
| 解析器文件  | 18   | tree-sitter 多语言（含 base/call_filter/call_resolver 等辅助模块）                        |
| 测试套件    | 8    | P0/P1/P2/P3/csharp_ruby/p1_p3/stress/fuzz/gc                                              |

## 二、核心能力矩阵

### 2.1 多语言符号图谱

| 能力                                   | 状态 | 关键文件                                 |
| -------------------------------------- | ---- | ---------------------------------------- |
| 16 语言 tree-sitter 解析               | ✅    | `parsers/*.py`（18 个解析器文件）        |
| 100% 覆盖 Semgrep GA 语言              | ✅    | `config.py` LANGUAGE_CONFIG              |
| 函数/类/结构体/接口/枚举提取           | ✅    | `parsers/base.py` BaseParser             |
| 注释检测（/// 与 //）                  | ✅    | BaseParser._has_comment                  |
| 模块路径推断（src/lib/app/main 前缀）  | ✅    | `db_build.py` _infer_module_path_generic |
| Cargo.toml / package.json crate 名检测 | ✅    | _detect_crate_name                       |

### 2.2 调用链分析

| 能力                                        | 状态 | 关键文件                     |
| ------------------------------------------- | ---- | ---------------------------- |
| 四级调用解析（精确→import→简名唯一→同文件） | ✅    | `analyzers/call_resolver.py` |
| 向上调用链（BFS 分层 + 扁平列表）           | ✅    | `analyzers/call_chain.py`    |
| 向下调用链                                  | ✅    | `analyzers/call_chain.py`    |
| 拓扑排序 + 循环检测                         | ✅    | `db_build.py` _build_depth   |
| 跨文件调用标记（is_cross_file）             | ✅    | _make_call_entry             |

### 2.3 版本历史与注释恢复

| 能力                                                    | 状态 | 关键文件                         |
| ------------------------------------------------------- | ---- | -------------------------------- |
| 文件版本（content_hash 去重 + version_num 自增）        | ✅    | `db_build.py` _save_file_version |
| 符号版本（file_symbol_versions + symbol_contents 去重） | ✅    | _save_symbols_for_version        |
| 调用版本（call_versions + caller_hash 引用）            | ✅    | _save_calls_for_version          |
| 删除标记（is_deleted）                                  | ✅    | _compute_symbol_diff             |
| 单函数注释恢复（fn@vN / fn@hash）                       | ✅    | `db_comment.py` restore_comment  |
| 批量注释恢复（按文件分组 + 行号倒序）                   | ✅    | restore_all_comments             |
| 预览模式（不写入返回 new_content_preview）              | ✅    | restore_comment(preview=True)    |

### 2.4 Guardian 四大支柱（v10）

| 支柱         | Mixin                            | 表                                   | MCP 工具 | 状态 |
| ------------ | -------------------------------- | ------------------------------------ | -------- | ---- |
| 生产安全护栏 | `db_guardrail.py` GuardrailMixin | guardrail_rules + guardrail_findings | 4        | ✅    |
| 变更影响智能 | `db_impact.py` ImpactMixin       | change_impacts                       | 4        | ✅    |
| 代码演化智能 | `db_evolution.py` EvolutionMixin | evolution_metrics                    | 4        | ✅    |
| 缺陷知识库   | `db_defect_kb.py` DefectKbMixin  | defect_patterns + defect_fixes       | 4        | ✅    |

**生产安全护栏内置规则**：
- DB Safety：ALTER TABLE / DROP TABLE / 字段缩减 / 迁移缺失
- API Compatibility：函数签名变更（参数增删 / 类型改变 / 可见性变化）
- Incident Readiness：错误处理缺失 / 日志缺失 / 可回滚性

**变更影响分析**：
- `blast_radius(symbol_hash, depth=3)`：BFS 多层影响树
- `cross_layer_impact(symbol_hash)`：代码 + DB + API + 配置四层
- `review_readiness_report(symbol_hash)`：影响范围 + 风险等级 + 必测项
- `diff_to_symbol(diff_text)`：git diff → 受影响符号

### 2.5 任务驱动编排（Agent OS）

| 能力                                                            | 状态 | 关键文件                                                           |
| --------------------------------------------------------------- | ---- | ------------------------------------------------------------------ |
| 任务/步骤/审计日志状态机                                        | ✅    | `db_tasks.py` TasksMixin                                           |
| task_create / task_next_step / task_report_step / task_rollback | ✅    | 6 个 MCP 工具                                                      |
| 编辑前护栏阻断（block 级别告警）                                | ✅    | task_next_step 调用 guardrail_check_edit                           |
| 阻断后修复步骤自动插入                                          | ✅    | task_resolve_block MCP 工具                                        |
| 安全编辑（propose_edit）                                        | ✅    | `db_edit.py` EditSafetyMixin（SHA-256 校验 + 原子写入 + 审计日志） |
| 检查门禁（CheckGate）                                           | ✅    | `db_check_gate.py` CheckGateMixin                                  |

### 2.6 向量搜索与 RAG

| 能力                       | 状态 | 关键文件                                             |
| -------------------------- | ---- | ---------------------------------------------------- |
| BLOB + Rust/numpy 余弦相似度 | ✅    | `db_vector.py` VectorMixin（D1 评审修正：实际实现是 BLOB 存储 + `callwarden_core.batch_cosine_similarity`，回退到 numpy；sqlite-vec vec0 虚拟表待落地） |
| sentence-transformers 嵌入 | ✅    | embed_symbols / embed_symbol                         |
| 语义搜索（自然语言找函数） | ✅    | semantic_search（embedder 不可用时降级关键词）       |
| 相似函数查找               | ✅    | find_similar_functions                               |
| ask_codebase RAG 管道      | ✅    | db_vector.py:502（~170 行，关键词回退 + 上下文拼接） |

### 2.7 Semgrep 集成与缺陷检测

| 能力                                | 状态 | 关键文件                                   |
| ----------------------------------- | ---- | ------------------------------------------ |
| 多语言静态安全扫描                  | ✅    | `analyzers/issues.py` IssueAnalyzerMixin   |
| 结果按内容去重入库                  | ✅    | semgrep_findings 表                        |
| 按严重度/语言/规则过滤              | ✅    | get_semgrep_findings                       |
| 符号级关联（symbol_qualified 过滤） | ✅    | get_semgrep_findings(symbol_qualified=...) |
| 增量扫描                            | ✅    | scan_semgrep_incremental                   |
| 缺陷模式库 + 修复建议               | ✅    | `db_defect_kb.py` suggest_fix              |

### 2.8 Git 集成与分支感知

| 能力                                 | 状态 | 关键文件                                |
| ------------------------------------ | ---- | --------------------------------------- |
| 导入 commit 历史                     | ✅    | `db_git.py` GitMixin import_git_history |
| 符号级变更追踪（git_symbol_changes） | ✅    | get_symbol_commit_history               |
| 文件变更详情                         | ✅    | get_commit_changes                      |
| 分支注册/切换/差异对比               | ✅    | `db_branch.py` BranchMixin              |
| 分支合并预览                         | ✅    | diff_branches                           |

### 2.9 代码度量与所有权

| 能力                                | 状态 | 关键文件                                                          |
| ----------------------------------- | ---- | ----------------------------------------------------------------- |
| 圈复杂度 / 认知复杂度               | ✅    | `db_metrics.py` MetricsMixin                                      |
| 耦合度 / 扇入扇出 / 调用深度        | ✅    | db_metrics.py                                                     |
| 健康评分（get_code_health_check）   | ✅    | db_metrics.py                                                     |
| AI Agent 健康检查（Token 溢出预警） | ✅    | check_file_health                                                 |
| CODEOWNERS 导入                     | ✅    | `db_ownership.py` OwnershipMixin import_ownership_from_codeowners |
| git blame 所有权映射                | ✅    | import_ownership_from_git_blame                                   |
| who_to_ask 询问建议                 | ✅    | who_to_ask MCP 工具                                               |

### 2.10 覆盖率与测试影响

| 能力                                 | 状态 | 关键文件                                       |
| ------------------------------------ | ---- | ---------------------------------------------- |
| LCOV / Cobertura 导入                | ✅    | `db_coverage.py` CoverageMixin import_coverage |
| 符号级覆盖率查询                     | ✅    | get_coverage_for_symbol                        |
| 未覆盖函数查找                       | ✅    | find_uncovered_functions                       |
| 测试影响选择（改了函数需跑哪些测试） | ✅    | test_impact_selection                          |

### 2.11 LSP 集成与跨仓库分析

| 能力                                            | 状态 | 关键文件                          |
| ----------------------------------------------- | ---- | --------------------------------- |
| LSP hover / 定义 / 引用 / 诊断 / 补全           | ✅    | `db_lsp.py` LSPMixin              |
| 支持 pyright / tsserver / gopls / rust-analyzer | ✅    | LSPMixin 服务器管理               |
| 跨仓库依赖检测                                  | ✅    | `db_cross_repo.py` CrossRepoMixin |
| 共享符号查找                                    | ✅    | find_shared_symbols               |
| 跨仓库影响传播                                  | ✅    | cross_repo_impact                 |

### 2.12 Token 节省账本

| 能力                                      | 状态 | 关键文件                                |
| ----------------------------------------- | ---- | --------------------------------------- |
| Token 节省记录（token_savings_ledger 表） | ✅    | `db_token_savings.py` TokenSavingsMixin |
| 装饰器自动统计                            | ✅    | @track_token_savings                    |
| 节省统计报表                              | ✅    | get_token_savings_stats                 |

### 2.13 GC 机制（v14 新增）

| 能力                                                     | 状态 | 关键文件                                 |
| -------------------------------------------------------- | ---- | ---------------------------------------- |
| .gitignore 完整语法解析（!/ \*\*/ 锚定/目录后缀/转义）   | ✅    | `analyzers/ignore_spec.py` IgnoreMatcher |
| .callwardenignore 项目级规则                             | ✅    | load_workspace_ignores                   |
| 默认基线规则（autogen/prebuilt/build output）            | ✅    | DEFAULT_IGNORE_RULES（30+ 条）           |
| 归档（gc_archive）：删除关联数据释放空间                 | ✅    | `db_gc.py` GCMixin                       |
| 复活（gc_restore）：status='pending' 下次 build 重新解析 | ✅    | gc_restore                               |
| 状态统计（gc_status）：active/archived/deleted 计数      | ✅    | gc_status                                |
| 清除老归档（gc_purge）：N 天前归档彻底删除               | ✅    | gc_purge(older_than_days)                |
| build 末尾自动触发 Young GC                              | ✅    | `db_build.py` _build_multi_lang 步骤 6/6 |
| 预演模式（dry_run）                                      | ✅    | gc_archive(dry_run=True)                 |
| repo manifest 自动检测（AOSP 多仓库）                    | ✅    | _detect_repo_manifest                    |

### 2.14 性能优化

| 优化项                            | 状态 | 效果                      |
| --------------------------------- | ---- | ------------------------- |
| PRAGMA WAL + synchronous=NORMAL   | ✅    | 写入并发不阻塞读          |
| cache_size=64MB / mmap_size=256MB | ✅    | 内存缓存命中率提升        |
| executemany 批量写入              | ✅    | N×3 次 SQL → 5 次 SQL     |
| INSERT OR IGNORE 去重             | ✅    | 替代 SELECT-then-INSERT   |
| 文件级 Map-Reduce 并行解析        | ✅    | ThreadPoolExecutor 8 线程 |
| 10w 符号构建                      | ✅    | 2.36 秒                   |
| 1w 符号批量插入                   | ✅    | 0.23 秒                   |

### 2.15 安全修复

| 安全项                   | 状态 | 修复方式                               |
| ------------------------ | ---- | -------------------------------------- |
| SEC-001 非原子文件写入   | ✅    | 写入临时文件 + os.replace 原子替换     |
| SEC-002 LSP 子进程安全   | ✅    | 命令白名单 + 超时 + 输出大小限制       |
| SEC-003 错误日志路径消毒 | ✅    | 路径转义去除                           |
| SEC-004 ~ SEC-007        | ✅    | 详见 security_best_practices_report.md |

### 2.16 CI/CD 集成

| 能力                               | 状态 | 关键文件                           |
| ---------------------------------- | ---- | ---------------------------------- |
| SARIF 输出（GitHub Security 集成） | ✅    | `cicd/sarif_exporter.py`           |
| 增量分析                           | ✅    | `cicd/incremental.py`              |
| PR 检查                            | ✅    | `cicd/pr_check.py`                 |
| GitHub Actions workflow            | ✅    | `.github/workflows/callwarden.yml` |

## 三、Mixin 模块清单（35 个功能 Mixin + 1 基类，39 个 db_*.py 文件）

权威清单见 [architecture.md §Mixin 架构](../architecture.md#mixin-架构)。下表为按文件路径分组的快速索引：

| Mixin                   | 文件                          | 职责                                  |
| ----------------------- | ----------------------------- | ------------------------------------- |
| CodeGraphBase           | `db_base.py`                  | 连接管理 + Schema 迁移 + 工作区       |
| BuildMixin              | `db_build.py`                 | 文件扫描 + 多语言解析 + 调用图 + 增量 |
| QueryMixin              | `db_query.py`                 | 符号/调用/拓扑查询                    |
| CommentMixin            | `db_comment.py`               | 注释恢复                              |
| MetricsMixin            | `db_metrics.py`               | 圈复杂度 + 健康评分                   |
| GitMixin                | `db_git.py`                   | Git 历史 + 符号变更                   |
| VectorMixin             | `db_vector.py`                | 向量嵌入 + RAG                        |
| SummaryMixin            | `db_summary.py`               | AI 摘要 + repo map                    |
| OwnershipMixin          | `db_ownership.py`             | CODEOWNERS + git blame                |
| TaskMixin               | `db_tasks.py`                 | 任务编排状态机                        |
| CoverageMixin           | `db_coverage.py` + analyzers  | LCOV/Cobertura + 测试影响             |
| LspMixin                | `db_lsp.py`                   | LSP 协议集成                          |
| CrossRepoMixin          | `db_cross_repo.py`            | 跨仓库分析                            |
| BranchMixin             | `db_branch.py`                | 分支感知                              |
| TokenSavingsMixin       | `db_token_savings.py`         | Token 账本                            |
| EditSafetyMixin         | `db_edit.py`                  | 安全编辑                              |
| CheckGateMixin          | `db_check_gate.py`            | 检查门禁                              |
| GuardrailMixin          | `db_guardrail.py`             | 生产安全护栏                          |
| ImpactMixin             | `db_impact.py`                | 变更影响                              |
| EvolutionMixin          | `db_evolution.py`             | 代码演化                              |
| DefectKbMixin           | `db_defect_kb.py`             | 缺陷知识库                            |
| GcMixin                 | `db_gc.py`                    | 归档/复活/清除（v14）                 |
| CallChainMixin          | `analyzers/call_chain.py`     | 调用链分析                            |
| IssueAnalyzerMixin      | `analyzers/issues.py`         | 缺陷检测                              |
| AgentRulesMixin         | `db_agent_rules.py`           | Agent Rule Memory                     |
| BootstrapMixin          | `db_bootstrap.py`             | 自举闭环                              |
| AuditChainMixin         | `db_audit_chain.py`           | 审计签名链                            |
| CasMixin                | `db_cas.py`                   | Global CAS                            |
| CloneDetectionMixin     | `db_clone_detection.py`       | 重复代码检测                          |
| CloneGroupsMixin        | `db_clone_groups.py`          | Clone Groups 存储                     |
| DaemonMixin             | `db_daemon.py`                | Enterprise daemon workspace registry  |
| ExternalMixin           | `db_external.py`              | 第三方包解析                          |
| JobsMixin               | `db_jobs.py`                  | 后台任务系统                          |
| MigrateMixin            | `db_migrate.py`               | 数据库迁移工具                        |
| StdlibMixin             | `db_stdlib.py`                | 标准库符号表                          |
| TaskAttributionMixin    | `db_task_attribution.py`      | 任务-符号变更归因                     |
| TaskQualityMixin        | `db_task_quality.py`          | 任务质量门禁                          |
| TestsMixin              | `db_tests.py`                 | 测试关联                              |
| ToolchainMixin          | `db_toolchain.py`             | Toolchain CAS                         |
| WorkspaceManifestMixin  | `db_workspace_manifest.py`    | Workspace manifest                    |

## 四、独占优势（竞品空白）

基于 [战略分析](competition-analysis.md) 的竞品调研，以下能力**没有任何竞品同时具备**：

1. **符号版本历史 + 注释恢复**：file_symbol_versions + content_hash 去重 + is_deleted 标记 + restore_comment()
2. **Semgrep 叠加代码图谱**：漏洞扫描结果入库关联符号，支持漏洞爆炸半径分析
3. **任务驱动 MCP（Agent OS）**：task_create → task_next_step → task_report_step → task_rollback 状态机
4. **AI Agent 健康检查**：check_file_health 警告 Token 溢出风险，建议拆分大文件
5. **生产安全护栏（蓝海）**：DB/API/Incident 三类可阻断规则 + Before-Edit Contract
6. **Java GC 机制**：.gitignore 解析 + 归档/复活/清除，自动管理代码库膨胀
7. **跨层影响分析**：代码变更 → DB schema / API 契约 / 配置层影响传播

## 五、待办与后续方向

| 方向                                       | 状态   | 备注                                                   |
| ------------------------------------------ | ------ | ------------------------------------------------------ |
| 查询接口添加 status != 'archived' 过滤     | ✅ 已实现 | `db_query.py` 所有查询已加 `AND status != 'archived'` |
| 生产者-消费者架构（解析→Queue→写入流水线） | 待办   | 进一步提升构建吞吐                                     |
| symbols 表 UNIQUE 索引 + 真正 UPSERT       | ⚠️ 部分 | `db_build.py` 已用 `ON CONFLICT(file_instance_id, name, start_line) DO UPDATE`（UPSERT），但 UNIQUE 键不是 `symbol_hash` |
| PyO3 Rust 扩展（向量计算加速）             | ⚠️ 部分 | `rust_ext/` 已搭建 parse/graphstore/canonicalize/hash_diff/multi_lang/daemon；向量计算加速未集成 |
| 多用户权限系统                             | 待办   | 当前按 workspace_id 逻辑隔离，无 RBAC                  |
| Prometheus 指标导出                        | ✅ 已实现 | G13（2026-07-20 二轮评审补全）：`server/metrics.py` 新增 `measure_rpc` 上下文管理器 + `request_duration_seconds` 内置直方图；`server/daemon_server.py` `_handle_connection()` 用 `measure_rpc(method)` 包裹 `dispatch()` 调用，新增 `metrics.snapshot`（JSON）/ `metrics.prometheus`（Prometheus 文本）两个只读 RPC 方法；CLI `cw daemon metrics` 默认走 RPC 拉 daemon 进程指标，`--local` 降级本进程直读；MCP `get_metrics` 新增 `source` 参数（auto/rpc/local），默认 auto 优先 RPC 失败降级 local。**注**：daemon 是纯 UDS（无 HTTP server），外部 Prometheus 需通过 `cw daemon metrics --format prometheus` 拉取后由 sidecar 暴露。**P1-6 限制（2026-07-21）**：仅 Python daemon 已实现，Rust system daemon（`cw_daemon`）无指标埋点 |

## 六、复审整改（2026-07-21 批次33：P1-4 + P1-6）

本章节记录复审报告 `feature-matrix-code-reaudit-2026-07-21.md` §P1-4 和 §P1-6 的代码核查结果与剩余项。
**未修复代码缺陷**，仅做能力区分文档化（矩阵从 ✅ 回退为 🟡 后已正确反映实际状态，本次补充详细缺口清单）。

### P1-4 QueryBudget 不是通用 daemon 查询预算

`QueryBudget` 只接入 `FrontierComputer::compute_frontier_with_budget`。常用 daemon query（search、callers/callees、call-chain、topological、cycle 等）没有统一 max-results/max-depth/timeout/truncate 执行器。G29 标题范围过大，已收紧为「Frontier budget（仅 `compute_frontier_with_budget`，通用 query budget 未实现）」。

#### 6.1 QueryBudget 定义位置（Rust/Python 不对齐）

| 维度 | Rust | Python |
| ---- | ---- | ------ |
| 文件 | `rust_ext/src/daemon/budget.rs:34-41` | `server/query_budget.py:28-103` |
| 字段 | max_depth / max_nodes / timeout_ms（3 字段） | max_depth / max_nodes / timeout_ms / max_results / frontier_limit（5 字段，多了 max_results/frontier_limit） |
| 运行时追踪 | `BudgetTracker`（visit_node / is_exceeded / is_partial / elapsed_ms） | `QueryBudget.start()` / `visit_node()` / `exhausted` / `truncate_results()` |
| 预设工厂 | 无（仅 `with_depth()`） | `default_budget()` / `deep_budget()` / `shallow_budget()` / `unlimited_budget()` |

#### 6.2 QueryBudget 的消费点

| 消费点 | 文件:行 | 用途 | 实际效果 |
| ------ | ------- | ---- | -------- |
| `FrontierComputer::compute_frontier_with_budget` | `rust_ext/src/frontier.rs:182-278` | Rust 唯一真正接入预算的执行点 | ✅ 完整使用 max_depth/max_nodes/timeout_ms + partial 标记 |
| `bfs_upstream_with_budget` / `bfs_downstream_with_budget` | `rust_ext/src/frontier.rs:367, 415` | frontier 计算内部 BFS | ✅ 由 tracker.is_exceeded() 检查 |
| Python `SnapshotManagerService` 6 个查询方法 | `server/snapshot_manager.py:232, 253, 275, 310, 336, 355` | 接受可选 `budget` 参数 | ⚠️ 残缺：仅调用 `b.truncate_results()` 后置截断，未调用 `b.visit_node()` |
| `workspace.file.refresh` 流程 | `rust_ext/src/daemon/workspace.rs:1154, 1222-1227` | daemon save 时构造 frontier | ⚠️ 硬编码 `QueryBudget::default()` + `store=None` 退化模式 |

#### 6.3 Daemon 查询 RPC 的 budget 接入状态

| RPC 方法 | Rust handler（snapshot_state.rs） | Python daemon（daemon_server.py） | MCP 工具 | 接入 budget |
| -------- | --------------------------------- | --------------------------------- | -------- | ----------- |
| `query.stats` | ✅ 实现 | ✅ 实现 | `get_stats` | ❌ 无 |
| `query.symbol` | ✅ 实现 | ✅ 实现 | `get_symbol` | ❌ 无 |
| `query.search` | ✅ 实现（limit 默认 20） | ✅ 实现（limit 默认 20） | `search_symbols` | ❌ 仅 `limit`，无 timeout/truncate |
| `query.callers` | ✅ 实现（无 limit/max_depth/timeout） | ✅ 实现（无 limit） | `get_callers` | ❌ 完全无 budget |
| `query.callees` | ✅ 实现（同上） | ✅ 实现（同上） | `get_callees` | ❌ 完全无 budget |
| `query.call_chain_down` | ✅ 实现（max_depth 默认 5） | ❌ **未实现** | `get_call_chain_down` | ⚠️ 仅 max_depth，无 timeout/truncate |
| `query.topological_order` | ✅ 实现（无 limit） | ❌ **未实现** | `get_topological_order` | ❌ 全量返回，无 budget |
| `query.detect_cycles` | ✅ 实现（无 max_depth/limit/timeout） | ❌ **未实现** | `detect_cycles` | ❌ 完全无 budget |

#### 6.4 Python `SnapshotManagerService` budget 接入残缺详情

| 方法 | 文件:行 | budget 用法 | 残缺 |
| ---- | ------- | ----------- | ---- |
| `query_callers` | snapshot_manager.py:227-246 | `b.truncate_results(result)` 后置截断 | ⚠️ 未传播到 Rust，max_nodes/timeout_ms 不生效 |
| `query_callees` | snapshot_manager.py:248-267 | 同上 | ⚠️ 同上 |
| `search_symbols` | snapshot_manager.py:269-292 | 用 `b.max_results` 作 limit | ⚠️ 不调用 `truncate_results()` |
| `query_symbol` | snapshot_manager.py:294-303 | 不接 budget 参数 | ❌ 无 budget |
| `query_call_chain_down` | snapshot_manager.py:305-331 | `b.start()` + `b.truncate_results()` | ❌ **从不调用 `b.visit_node()`**，max_nodes/timeout_ms 形同虚设 |
| `query_topological_order` | snapshot_manager.py:333-350 | 仅 `b.truncate_results()` | ⚠️ 未传播到 Rust |
| `query_detect_cycles` | snapshot_manager.py:352-373 | `b.start()` + `b.truncate_results()` | ❌ **同 query_call_chain_down，max_nodes/timeout_ms 不生效** |

#### 6.5 MCP 工具层完全不暴露 timeout/truncate

| MCP 工具 | 文件:行 | 当前参数 | 缺失 |
| -------- | ------- | --------- | ---- |
| `search_symbols` | mcp_server.py:129 | query, kind="", limit=20 | timeout, truncate |
| `get_callers` | mcp_server.py:175 | callee_name, qualified_name=None | limit, max_depth, timeout, truncate |
| `get_callees` | mcp_server.py:191 | caller_name, qualified_name=None | 同上 |
| `get_topological_order` | mcp_server.py:237 | limit=50 | max_depth, timeout, truncate |
| `get_impact` | mcp_server.py:253 | qualified_name, max_depth=10 | limit, timeout, truncate |
| `get_call_chain_down` | mcp_server.py:265 | qualified_name, max_depth=10 | limit, timeout, truncate |
| `detect_cycles` | mcp_server.py:328 | max_depth=10 | limit, timeout, truncate |

#### 6.6 P1-4 剩余工作（实现通用 query budget 的步骤）

1. **统一 Rust/Python `QueryBudget` 字段**：Rust 加 `max_results` / `frontier_limit`，或 Python 删除（决定单一真相源）
2. **Rust daemon handler 接入 `BudgetTracker`**：在 `snapshot_state.rs` 的 6 个查询 RPC 中构造 `BudgetTracker`，循环内调用 `tracker.visit_node()` + `tracker.is_exceeded()` 检查
3. **dispatch.rs 协议层新增参数**：`max_results` / `max_depth` / `timeout_ms` / `truncate` 字段，handler 解析后构造 `QueryBudget`
4. **Python daemon 补齐 3 个查询 RPC**：`call_chain_down` / `topological_order` / `detect_cycles`
5. **修复 `SnapshotManagerService` 残缺 budget**：`query_call_chain_down` / `query_detect_cycles` 在循环内调用 `b.visit_node()`
6. **MCP 工具层暴露 `timeout` / `truncate` 参数**：通过 `DaemonClient` wire protocol 传递到 Rust

### P1-6 Python daemon 与 Rust system daemon 能力被混为一谈

Python `server/daemon_server.py` 确实接入 metrics、health、migration；Linux systemd unit 启动的是 Rust `cw_daemon`。两者 RPC 集、指标导出和启动迁移并不完全相同。文档必须明确「Python daemon 已实现」还是「企业 system daemon 已实现」。

#### 6.7 daemon 能力区分矩阵

| 能力 | Python daemon（`server/daemon_server.py`） | Rust system daemon（`rust_ext/src/daemon/`，systemd 启动） | 差距 |
| ---- | ------------------------------------------ | ---------------------------------------------------------- | ---- |
| **Metrics 收集器（G13）** | ✅ `MetricsCollector` 单例 + `measure_rpc` 上下文 + `metrics.snapshot`/`metrics.prometheus` RPC + CLI `cw daemon metrics` | ❌ 无指标埋点，无 RPC endpoint | Python 已实现，Rust 未对齐 |
| **Health Check（G14）** | ✅ `HealthChecker` 实例化 + `health` RPC 调用 `check_all()` 四项检查 | ❌ RPC endpoint 只返基础统计并固定 `status=ok`，未执行四项检查 | Python 已实现，Rust 未对齐 |
| **Schema Migrator（G15）** | ✅ `_run_startup_migrations()` 调用 `migrate_daemon_dbs` 对 registry.db/audit.db 版本化迁移 | ❌ 只做 schema-check/init，不是版本化迁移 | Python 已实现，Rust 未对齐 |
| **Replicator CAS→Manifest→Snapshot（G11）** | ✅ `daemon_handle_refresh` step 5 调用 `merge_cas_to_codegraph` + `upsert_manifest`（P0-1 批次30） | ❌ Rust daemon 路径未接入 merge | Python 已实现，Rust 未对齐 |
| **memfd 六重校验（G10）** | ✅ `agent_protocol.py:307-313` `create_sealed_memfd` + `daemon_server.py:798-802` `is_memfd`/`validate_memfd_fd` | ✅ `rust_ext/src/daemon/memfd.rs` 六重校验（P1-3 批次28） | ✅ 两侧对齐 |
| **Workspace ACL（G3/G4/G16）** | ✅ dispatch 层 `_owned_workspace` / `_owned_workspace_by_id`（P0-2 批次29） | ✅ Rust daemon 路径 workspace_id 级 ACL（P0-2 批次29） | ✅ 两侧对齐 |
| **Backup/Restore（G16）** | ✅ 加入 `ADMIN_ONLY_METHODS` 顶层 fail-closed | ✅ Rust daemon RPC 可达 + 顶层 fail-closed | ✅ 两侧对齐 |
| **Snapshot GC（G17/G32）** | ✅ `SnapshotGC` 实例化 + `cw-snapshot-gc` 后台线程 | ⚠️ Rust daemon 无 GC 后台线程 | Python 已实现，Rust 未对齐 |
| **Refresh Scheduler（G19）** | ✅ `RefreshScheduler` 实例化 + `cw-refresh-flush` 后台线程 | ⚠️ Rust daemon 无 scheduler 后台线程 | Python 已实现，Rust 未对齐 |
| **Job Executor（G18）** | ✅ `job_executor.py` + `job_handlers.py` | ⚠️ Rust daemon 无 job executor | Python 已实现，Rust 未对齐 |

#### 6.8 P1-6 结论

1. **不能用 Python 单例证明 Rust 服务具备相同能力**：G13/G14/G15 在 Python daemon 已实现，但 Rust system daemon 未对齐
2. **systemd 启动 Rust `cw_daemon`**：Linux 生产环境实际启动的是 Rust daemon，Python daemon 是开发期/Windows 降级路径
3. **G13/G14/G15 状态保持 🟡**：能力区分已明确文档化，但 Rust daemon 路径仍未补齐 metrics/health/migration
4. **G11 剩余缺口**：Rust daemon 路径未接入 CAS→Manifest→Snapshot merge（依赖 P1-6 文档明确后由 Rust 端补齐）
5. **未来 Rust daemon 补齐工作**：metrics 埋点 + health check 四项检查 + 版本化迁移 + CAS merge + snapshot GC 后台线程 + refresh scheduler 后台线程 + job executor

