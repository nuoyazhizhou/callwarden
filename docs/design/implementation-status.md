# 实现状态总览

> 最后更新：2026-08-28 · Schema v60 · 43 个功能 Mixin（+ CodeGraphBase，48 个 db_*.py 文件）· 237 MCP 工具 · 16 语言

本文档是 callwarden 当前能力的权威盘点，对照 [Guardian 规格](evolve-guardian-architecture/spec.md) + [战略分析](competition-analysis.md) + 实际代码逐项核查。历史盘点请参阅 [history/implementation-snapshot-v13.md](../history/implementation-snapshot-v13.md)。

## 一、总览

| 维度        | 数量 | 说明                                                                                      |
| ----------- | ---- | ----------------------------------------------------------------------------------------- |
| 支持语言    | 16   | Rust/TypeScript/JavaScript/Python/Kotlin/Go/Java/C/C++/C#/Ruby/PHP/Swift/Scala/HCL/Elixir |
| 数据库表    | 30+  | 含 5 张 Guardian 表 + 1 张 archived_files 归档表                                          |
| Schema 版本 | v60  | v3→v60 版本化迁移，事务化执行（v44: P2 artifact/interface 依赖与环检测 schema；v45: P3 Identity/Attestation schema：action_identities / attestation_records / attestation_revocation_records；v46: P4 assignment/lease schema；v50: Agent Identity + Role Contract：agent_registrations 扩展 identity 最小字段 + role_contracts 冻结合同表；v51–v60: task_loop operation store / workspace authority binding / promotion 权威账本 / Task Contract normalization 绑定 / Role Worker 稳定凭据域，逐项见 `db/schema.py` 迁移注记） |
| Mixin 模块  | 43   | CodeGraphBase + 43 个功能 Mixin（48 个 db_*.py 文件）                                     |
| MCP 工具    | 237  | FastMCP @mcp.tool() 注册（含 10 个 P2 依赖图工具 + 7 个 P3 Identity/Attestation 工具 + 8 个 P4 assignment/lease 工具）    |
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

## 三、Mixin 模块清单（35 个功能 Mixin + 1 基类，40 个 db_*.py 文件）

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
| `workspace.file.refresh` 流程 | `rust_ext/src/daemon/workspace.rs:1154, 1222-1235` | daemon save 时构造 frontier | ⚠️ 硬编码 `QueryBudget::default()` + `store=None` 退化模式 |

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
| `query_callers` | snapshot_manager.py:235-246 | `b.truncate_results(result)` 后置截断 | ⚠️ 未传播到 Rust，max_nodes/timeout_ms 不生效 |
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

## 七、多 LLM 契约协作分阶段状态（P0–P4）

> 规格冻结文档：[requirements.md](requirements.md) · [multi-llm-contract-driven-collaboration-design.md](multi-llm-contract-driven-collaboration-design.md) · [tasks.md](tasks.md)
> 最后更新：2026-08-02

| 阶段 | 名称 | 状态 | 关键产出 |
|------|------|------|---------|
| 设计冻结 | 规格冻结 | ✅ 已冻结 | requirements.md（29×5 条验收标准）、design doc、tasks.md |
| P0 | 盲评对照实验 | ✅ 已实现 | `experiments/` 模块 + `cw experiment` CLI（13 子命令）+ JSONL 记录 + 评估器 |
| D0 | 跨平台 daemon 化 | ✅ 已实现 | 三平台端点（UDS/命名管道）、Peer_Credential、串行化点、Authoritative_Clock、Attestation、Stage_Toggle、稳定错误码、自动唤起与互斥、Degraded_Mode 分流、4 个只读 MCP 工具 |
| P1 | 契约驱动协作 | ✅ 已实现 | Canonical Envelope + Role View allowlist + Blind_First_Pass_Verdict / Reveal_Event / Post_Reveal_Amendment + Evidence_Ledger + Evidence_Gate + CLI/MCP 工具（Req 1.1-1.12, 2.x, 6.x） |
| P2 | DAG 依赖调度 | ✅ 已实现 | 四类依赖（requires_existing/artifact/provides_interface/requires_interface）+ artifact freshness + interface identity/version/hash + 硬依赖图 + 环检测 + provider 选择 + CLI/MCP 工具（Req 9.1-9.10, 13.13） |
| P3 | Agent 身份审计 | ✅ 已实现 | action_identities / attestation_records / attestation_revocation_records + Identity 完整性校验与 session/agent/model 家族分离 + Attestation 签发校验与撤销（compromised/rotated）+ blind verdict 独立审核证明 + Identity fail-closed 接入 Evidence Gate + task mutation Identity 审计 + CLI/MCP 工具（Req 10.1-10.18, 1.4-1.5, 6.22） |
| P4 | 安全租约与分派 | 🔲 planned / unavailable | 需 P1 + P3 启用（Req 13.13） |

**约束（Req 13.1）**：P4 在启用前，其所有能力均表示为 planned 且 unavailable。
文档、CLI 输出、MCP 工具描述中不得暗示该阶段已实现。

### P0 已实现能力清单

| 能力 | 关键文件 | 需求覆盖 |
|------|---------|---------|
| 分层随机分组（5 维度） | `experiments/blind_review_protocol.py` | Req 12.3 |
| 协议冻结 + 指纹 | 同上 | Req 12.3, 12.5 |
| Minimal_Blind_View 构建 | `experiments/blind_review_views.py` | Req 12.24, 12.25 |
| 盲法披露检测 + 无效样本 | 同上 | Req 12.8, 12.18 |
| JSONL 追加写入 + 截断恢复 | `experiments/blind_review_jsonl.py` | Req 12.22, 12.23 |
| 成功/灰区/暂停评估 | `experiments/blind_review_evaluator.py` | Req 12.10–12.17, 12.27–12.29 |
| G0 决策输出 | `cli/main.py` `_handle_experiment` | Req 12.14 |
| P0 Stage_Toggle（3 级作用域） | `experiments/blind_review_protocol.py` | Req 13.17, 13.18 |
| 暂停 6 触发器 + fail-safe | 同上 + evaluator | Req 12.15–12.21, 12.24 |
| 非产品 Evidence 标记 | 全部 JSONL 记录 | Req 12.23 |

### D0 已实现能力清单

> D0 是 P1 的前置阶段，与 P0 相互独立（Requirement 13.17）。D0 已交付跨平台 daemon
> 基座；P1/P2/P3 已在 D0 之上产品化（见下方各阶段能力清单），P4 仍标记 planned/unavailable（Requirement 13.1）。

| 能力 | 关键文件 | 需求覆盖 |
|------|---------|---------|
| 跨平台传输抽象（UDS + 命名管道） | `rust_ext/src/daemon/transport/` | Req 14.2, 14.3 |
| 三平台 Peer_Credential → Peer_Identity | `rust_ext/src/daemon/peercred.rs` | Req 14.4, 14.5 |
| Windows SID 等强度 ACL（SDDL） | `rust_ext/src/daemon/transport/named_pipe.rs` | Req 14.18, 14.19 |
| 端点排除（AF_UNIX/TCP/HTTPS 不提供 Peer_Credential） | 设计文档决策记录 | Req 14.20, 14.21 |
| 进程内唯一串行化点 + 请求队列 | `rust_ext/src/daemon/server.rs` | Req 14.6 |
| Authoritative_Clock API | `rust_ext/src/daemon/clock.rs` | Req 14.11 |
| daemon 侧 Attestation 签发 | `rust_ext/src/daemon/attestation.rs` | Req 14.13 |
| 并发 gate 判定隔离 | `rust_ext/src/daemon/gate.rs` | Req 14.14 |
| 昂贵 verifier 执行移出 SQLite 写事务 | `rust_ext/src/daemon/verifier.rs` | Req 14.16 |
| Stage_Toggle 存储 + 作用域解析 + 前置校验 | `rust_ext/src/daemon/stage_toggle.rs` | Req 13.11, 13.18–13.21 |
| Independence_Policy 存储 + 变更审计 | `rust_ext/src/daemon/independence_policy.rs` | Req 5.13 |
| 稳定错误码 + 双语 message key + 恢复指引 | `server/degraded_mode.py` + `rust_ext/src/daemon/error_codes.rs` + `i18n/*.json` | Req 1.12, 14.15 |
| 经 daemon 的 CLI 写命令面 | `cli/main.py` + `server/daemon_client.py` | Req 14.17 |
| 只读 MCP 查询工具面（4 工具） | `server/mcp_server.py` | Req 14.17 |
| macOS launchd 打包 | `rust_ext/packaging/launchd/` | Req 14.25 |
| daemon 自动唤起 + 有界等待窗口（10s） | `server/daemon_client.py` `call_with_autostart` | Req 14.22 |
| 唤起单实例跨进程互斥 | `server/daemon_client.py` + `rust_ext/src/daemon/mutex.rs` | Req 14.23 |
| 三平台唤起方式（systemd/launchd/detached） | `server/daemon_client.py` | Req 14.24–14.26 |
| Degraded_Mode 操作分类 + 分流策略 | `server/degraded_mode.py` | Req 14.27–14.30 |
| daemon 客户端接线降级分流 + Degraded_Mode 标记 | `server/daemon_client.py` | Req 14.33 |
| 无有效 Attestation 记录判 invalid（不设物理写屏障） | `rust_ext/src/daemon/attestation.rs` | Req 14.31 |
| 混合类操作按组成部分分级 | `server/degraded_mode.py` + `cli/main.py` | Req 14.34–14.39 |
| P4 Lease 边界正面陈述（代码注释/CLI/文档） | 全部 D0 产出 | Req 14.32, 11.13 |

### P2 已实现能力清单

> **注意**：P2 的"DAG 依赖调度"仅指**依赖关系校验与环检测**，不包含自动排程、资源优化、自动 assignment 或复杂 DAG scheduler（Req 9.10）。

| 能力 | 关键文件 | 需求覆盖 |
|------|---------|---------|
| 四类依赖区分导入（requires_existing/artifact/provides_interface/requires_interface） | `db/db_task_dependencies.py` | Req 9.1 |
| requires_existing 存在性验证（不建边） | `db/db_task_dependencies.py` `resolve_requires_existing` | Req 9.2 |
| artifact identity 与 freshness（producing/fresh/stale） | `db/db_task_dependencies.py` `record_artifact_identity`/`get_artifact_freshness` | Req 9.3 |
| interface identity/version/hash 发布与查询 | `db/db_task_dependencies.py` `publish_interface`/`get_interface_providers` | Req 9.4-9.5 |
| 硬依赖图边构建（provider→consumer，去重） | `db/db_task_dependencies.py` `build_hard_dependency_edges` | Req 9.6 |
| 环检测与最小 cycle path（原子拒绝 revision） | `db/db_task_dependencies.py` `detect_cycle` + `db/db_task_contracts.py` `publish_envelope_revision` | Req 9.7, 13.6-13.8 |
| informational 关系不阻断、不参与排序 | `db/db_task_dependencies.py` `is_informational` 字段 | Req 9.8 |
| 多 provider 显式选择（无 Planner 选择立即拒绝） | `db/db_task_dependencies.py` `select_interface_provider`/`get_provider_selection` | Req 9.9 |
| 只做无环校验和诊断，不做资源优化/自动 assignment/DAG 调度 | 全部 P2 产出 | Req 9.10 |
| Envelope 发布路径复用（依赖导入 + 原子构图 + cycle 拒绝） | `db/db_task_contracts.py` `publish_envelope_revision` | Req 9.1, 9.7 |
| Gate 依赖 freshness 判定（artifact fresh + interface provider 匹配） | `db/db_task_gate.py` `_check_dependency_freshness` | Req 9.3-9.5 |
| P2 CLI 诊断（inspect/list/cycle/explain/provider-select） | `cli/main.py` `_handle_dependency` | Req 9.1-9.10 |
| P2 MCP 工具（10 个薄包装器） | `server/mcp_server.py` | Req 9.1-9.10, 13.10 |
| P2 schema 迁移（5 张表 + 16 索引，幂等） | `db/schema.py` + `db/db_base.py` `_migrate_v43_to_v44` | Req 9.1-9.9, 13.10 |
| i18n 本地化诊断文案（zh_CN/en_US） | `i18n/zh_CN.json` + `i18n/en_US.json` | Req 1.12 |

### P3 已实现能力清单

> **身份不等同 ownership**：Identity（agent_id/session_id/model_id/role）仅作 actor attribution，
> 不等于 assignment/lease/ownership/SQLite lock（Req 10.5, 10.7）。Attestation 只能由 daemon 签发（Req 14.13）。

| 能力 | 关键文件 | 需求覆盖 |
|------|---------|---------|
| action_identities / attestation_records / attestation_revocation_records 三表（含约束） | `db/schema.py`（SCHEMA_VERSION=45） | Req 10.1-10.4, 10.8 |
| v44→v45 幂等迁移 | `db/db_base.py` `_migrate_v44_to_v45` | Req 10.1-10.18, 13.10 |
| Identity 完整性校验 + session/agent/model 家族分离 | `db/db_task_identity.py` `validate_action_identity`/`validate_session_separation`/`validate_agent_family_separation`/`validate_model_family_separation` | Req 10.1-10.4 |
| Attestation 签发与校验（issuer=daemon、禁自签、绑定 hash、有效期窗口） | `db/db_task_identity.py` `issue_attestation`/`validate_attestation` | Req 10.8-10.9, 14.13 |
| Attestation 撤销（单条 Revocation_Record，compromised/rotated，invalid 查询时派生） | `db/db_task_identity.py` `register_attestation_revocation`/`derive_attestation_validity` | Req 10.10-10.18 |
| blind verdict 独立审核证明（allowlisted manifest、verdict-before-reveal、daemon attestation、session 分离、high_risk 家族分离 + 独立 Tester） | `db/db_task_reviews.py` `verify_blind_verdict_proofs` | Req 1.4-1.5, 10.2-10.4 |
| Identity fail-closed 接入 Evidence Gate（缺失/不完整排除 verdict、apply session 分离、attestation 越窗/自签/issuer 不符/被撤销判 invalid、gate decision 记录 issuer/signing_key_id/issued_at） | `db/db_task_gate.py` `evaluate_evidence_gate` | Req 1.5, 10.2, 10.8-10.18, 6.22 |
| task mutation 身份审计（report/apply/close/reopen 记录 Identity；apply 强制 session 分离） | `db/db_tasks.py` `task_report_step`/`task_apply`/`task_close`/`task_reopen` | Req 10.1, 10.5-10.7, 13.3-13.5 |
| P3 CLI（`--agent-id/--session-id/--model-id/--role` 输入 + `identity revoke` 强制 `--revocation-mode` + 拒绝自由文本 reviewer） | `cli/main.py` | Req 10.1-10.18, 1.12 |
| P3 MCP 工具（7 个薄包装器：record/get/check_action_identity、check_session_separation、get_attestation_validity、list/register_attestation_revocations） | `server/mcp_server.py` | Req 10.1-10.18, 13.10 |
| i18n 本地化 identity/attestation reason（zh_CN/en_US） | `i18n/zh_CN.json` + `i18n/en_US.json` | Req 1.12 |

### P4 已实现能力清单

> **Lease 边界（正面陈述，Req 14.32/11.13）**：Lease 保证的是 daemon 在线期间的并发正确性——
> 同一 task/role 任一时刻只有一个有效持有者，旧持有者在新 lease 生效后无法再写入（fencing）。
> 防篡改保证归属于 Attestation 校验与追加式 Evidence_Ledger；本模块不把 Lease 描述为能防止
> 离线直接改库。Lease 校验通过不代表 mutation 被授权——角色权限、Independent Review 与
> Evidence Gate 仍适用（Req 11.11）。不提供自动 dispatch、抢占或中央调度（Req 14.32）。

| 能力 | 关键文件 | 需求覆盖 |
|------|---------|---------|
| task_assignments / task_leases / task_lease_events 三表 + 单 active 部分唯一索引（永不存 raw token，只存 sha256 hash） | `db/schema.py`（SCHEMA_VERSION=46） | Req 11.1-11.3, 11.6, 13.10 |
| v45→v46 幂等迁移 | `db/db_base.py` `_migrate_v45_to_v46` | Req 11.1-11.13, 13.4-13.10 |
| Assignment 绑定（task+role+holder Identity，不依赖 workspace active_task_id；assignment 可无 lease） | `db/db_task_leases.py` `create_assignment`/`get_assignment`/`revoke_assignment` | Req 11.1, 11.12, 13.4 |
| Lease 生命周期（acquire 原子比较 + 单调递增 fencing counter；renew 要求 token/holder/counter 且未过期、幂等不递增；release 追加事件且幂等） | `db/db_task_leases.py` `acquire_lease`/`renew_lease`/`release_lease` | Req 11.2-11.7 |
| protected mutation 校验（token hash/expiry/role/Identity/当前 fencing，失败不改变 task data） | `db/db_task_leases.py` `validate_lease_for_mutation` | Req 11.8-11.9, 11.11 |
| 权威时钟（时间字段与过期判定一律读取 daemon Authoritative_Clock；客户端时间戳只作参考元数据） | `db/db_task_leases.py` `_clock` | Req 14.11, 14.12 |
| Lease 审计事件账本（append-only，不写 raw token） | `db/db_task_leases.py` `_append_lease_event`/`list_lease_events` | Req 11.6, 11.12 |
| protected task mutation 接入 lease/fencing（report/apply/close/reopen + blind verdict/reveal + evidence + contract publish；提供 lease 凭证时启用受保护路径，fail-closed；不带则向后兼容） | `db/db_tasks.py`、`db/db_task_reviews.py`、`db/db_task_evidence.py`、`db/db_task_contracts.py` | Req 11.8-11.12, 13.2-13.5 |
| P4 CLI（`cw lease`/`cw assignment` 子命令 + protected mutation `--lease-token/--fencing-counter`；raw token 仅 acquire 成功响应返回一次） | `cli/main.py` | Req 11.1-11.13, 1.12 |
| P4 MCP 工具（8 个：lease_acquire/renew/release/status/list_events + assignment_create/show/revoke） | `server/mcp_server.py` | Req 11.1-11.12, 13.10 |
| i18n 本地化 lease/assignment reason + contract_lease_denied（zh_CN/en_US） | `i18n/zh_CN.json` + `i18n/en_US.json` | Req 1.12 |

### G0 检查点

G0 是 P0 → P1 的门禁。`cw experiment report <batch_id> --json` 输出
`g0_decision.eligible_for_p1=true` 当且仅当所有成功阈值满足且无未解决灰区/暂停。
G0 通过**仅表示可以讨论是否启用 P1**，不自动触发任何 schema 变更或能力解锁。

### GD 检查点

GD 是 D0 → P1 的门禁，与 G0 相互独立（Requirement 13.17）。GD 通过条件：

- 命名管道 SDDL 与实例保活（Req 14.18, 14.19）
- 端点负向约束（排除 AF_UNIX/TCP/HTTPS，Req 14.20, 14.21）
- 三平台自动唤起 + 单实例互斥（Req 14.22–14.26）
- Degraded_Mode 分级 + 恢复指引（Req 14.27–14.30）
- 混合类操作按组成部分分级：Index_Write 执行、Governance_Write fail closed、
  无状态推进、无 Evidence 或 gate decision（Req 14.34–14.39）
- Stage_Toggle 存储迁移保值（Req 13.19）
- daemon 配置存储同时承载 Stage_Toggle 与 Independence_Policy，变更可审计
  （Independence_Policy 语义在 P1 由 4.5 落地，Req 5.13）
- 无有效 Attestation 记录判 invalid（Req 14.31）

GD 通过**仅表示 D0 基座已就绪**，P1 仍需 G0 同时通过才能启动（Req 13.13）。

### 非目标（Req 13.6）

本特性范围限于契约驱动任务协作，明确排除：通用项目管理、实时 Agent 聊天、
共享隐藏推理历史、中央多 Agent 调度器、任意自然语言证明、以 LLM verdict
替代确定性验证。
