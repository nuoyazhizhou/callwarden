# 架构设计

本文档说明 Call Warden 的整体架构、数据库 Schema、Mixin 多继承设计、安全机制、性能优化和扩展指南。

## 整体架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         AI Agent / CLI                          │
└──────────────┬───────────────────────────────┬──────────────────┘
               │                               │
               ▼                               ▼
┌──────────────────────────┐     ┌──────────────────────────────┐
│      CLI (cli/main.py)   │     │   MCP Server (FastMCP)       │
│  子命令 + --flag 双风格  │     │   206 个 @mcp.tool() 工具    │
│  145+ 命令               │     │   stdio / SSE 传输           │
└────────────┬─────────────┘     └──────────────┬───────────────┘
             │                                  │
             └──────────────┬───────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────────┐
│                  CodeGraphDB (db.py)                          │
│         35 个功能 Mixin + 1 基类多继承组装的统一数据库类      │
│  CodeGraphBase + BuildMixin + QueryMixin + ... + CheckGateMixin│
└────────────────────────────┬──────────────────────────────────┘
                             │
         ┌───────────────────┼───────────────────┐
         ▼                   ▼                   ▼
┌─────────────────┐ ┌─────────────────┐ ┌─────────────────┐
│  Parsers        │ │  Analyzers      │ │  Rust Ext (PyO3)│
│  tree-sitter    │ │  call_chain     │ │  性能加速       │
│  16 种语言      │ │  coverage       │ │  lib.rs         │
└─────────────────┘ └─────────────────┘ └─────────────────┘
                             │
                             ▼
┌───────────────────────────────────────────────────────────────┐
│              SQLite 数据库（用户级单库）                       │
│   $HOME/.callwarden/callwarden.db                              │
│   Schema v41 / WAL 模式 / 40+ 表 / 35 个功能 Mixin + 1 基类    │
│   多 workspace 通过 workspace_id 逻辑隔离                      │
└───────────────────────────────────────────────────────────────┘
```

### 分层说明

| 层 | 职责 | 关键文件 |
|----|------|----------|
| 接入层 | CLI 命令解析、MCP 协议处理 | `cli/main.py`、`server/mcp_server.py` |
| 业务层 | 35 个功能 Mixin + 1 基类（含 analyzers 3 个） | `db.py` + `db_*.py`（40 个文件） |
| 解析层 | tree-sitter 多语言解析、调用关系提取 | `parsers/`（18 个文件） |
| 分析层 | 调用链、覆盖率、缺陷检测 | `analyzers/`（6 个文件） |
| 加速层 | PyO3 Rust 扩展（可选） | `rust_ext/` |
| 存储层 | SQLite 项目级数据库 | `schema.py` |

## 数据库架构

### 用户级单库 + workspace 逻辑隔离

每个用户使用一个统一的 SQLite 数据库，路径格式：

```
$HOME/.callwarden/callwarden.db
```

- 一个用户一个数据库，所有项目共用
- 项目间通过 `workspaces` 表的 `workspace_id` 字段在所有业务表中逻辑隔离
- 所有查询自动带 `WHERE workspace_id = ?` 过滤
- 相同文件跨项目只解析一次（Global CAS 共享）
- 实现见 `config.py:get_project_db_path()`
- 旧版多库迁移：`cw gc db-migrate-single --apply`

### Schema 版本

当前 Schema 版本：**v41**

```
v4  Git 集成表（git_commits / git_file_changes / git_symbol_changes）
v5  向量嵌入表（symbol_embeddings，BLOB 存储 + Rust/numpy 余弦相似度，sqlite-vec 待落地）
v6  符号摘要表（symbol_summaries，AI 摘要版本化）
v7  任务管理表（tasks / task_steps / change_audit）
v8  文件所有权表（file_ownership）
v9  覆盖率数据表（coverage_data，LCOV/Cobertura 行级覆盖率）
v10 守护者架构表（guardrail_rules/findings + change_impacts + evolution_metrics + defect_patterns/fixes）
v11 Token 节省账本表（token_savings_ledger）
v12 安全文件编辑审计表（file_edit_audit，propose_edit 流水线）
v13 跨仓库分析表（cross_repo_deps）
v14 归档文件表（archived_files，GC 可恢复归档闭环）
v15 父子任务表（tasks 加 parent_id / depth / sort_order 字段，支持任务树）
v16 外部依赖表（external_symbols + package_versions，追踪外部包符号）
v17 任务-符号变更归因表（task_symbol_changes，任务编辑与符号变更的关联）
v18 package_versions 增强（加 last_seen_at / last_used_at / import_source，冷数据追踪）
v19 GC 策略表（gc_policies，retention 策略配置）
v20 GC 运行审计表（gc_runs，GC 运行记录与归档明细）
v21 任务质量门禁表（task_quality_findings，阻塞/警告级别 finding）
v22 审计签名链表（audit_chain，HMAC 签名的审计链）
v23 Agent Rule Memory 表（agent_rule_candidates / agent_rules / agent_rule_sync_log）
v24 任务状态机完整化（tasks 加 applied_at 字段，支持 review → applied → closed 流转）
v25 自举闭环扫描基线表（workspace_scan_runs，扫描运行记录与变化检测）
v26 symbols 表 UNIQUE 索引（file_instance_id, name, start_line）+ UPSERT，防止重复符号、支持并发安全写入
v27 重复代码对表（clone_pairs，记录 Type-1/2/3 克隆检测结果，支持重构决策）
v28 file_versions 表 ast_cache BLOB 字段（tree-sitter AST 序列化，支持 AST 级增量解析）
v29 审计签名密钥轮换表（audit_key_rotations，记录密钥轮换时间点，支持按 key_id 验证旧记录）
v30 workspaces 表 active_task_id 字段（active task 持久化，替代 CALLWARDEN_TASK_ID 环境变量）
v31 FTS5 全文索引（symbols_fts 虚拟表 + 同步触发器，search_symbols 从 LIKE 改为 FTS5 子串匹配）
v32 删除 calls(callee_name) 索引，当前调用查询优先使用 Rust GraphStore
v33 用 calls(callee_id) resolved 部分整数索引替代 calls(callee_qualified) 长文本索引
v37 L2 破坏性 git 操作记录表 destructive_operations（force push 等记录，软门禁）
v38 get_stats 加速索引（by_kind / depth_distribution GROUP BY 优化，部分索引 + ANALYZE）
v39 call_chain_up/down 加速索引 idx_call_versions_callee_current（BFS 按 callee_qualified 走索引）
v40 A14 增量扫描 — semgrep_findings 加 scan_id 字段 + 索引（关联 finding 到 scan，支持增量清理）
```

Schema 迁移在 `db_base.py` 中自动执行（启动时检测版本并增量 ALTER TABLE）。每个版本迁移函数命名为 `_migrate_v<N>_to_v<N+1>`，使用 `CREATE TABLE IF NOT EXISTS` + `ALTER TABLE ADD COLUMN` 保证幂等性。

### WAL 模式

SQLite 启用 WAL（Write-Ahead Logging）模式：
- 多读者并发，单写者排队
- 读写不互相阻塞
- 适合 MCP Server 长连接场景

## 核心表结构

### 内容寻址存储（Hash 去重）

Call Warden 采用**内容寻址**设计：相同内容只存一次，通过 hash 关联。

```
file_contents (content_hash PK)  ←─── file_instances (path 维度)
        │
        └─── file_versions (历史版本)

symbol_contents (content_hash PK) ←─── symbols (当前快照)
        │
        ├─── file_symbol_versions (版本关联)
        ├─── comments (注释)
        ├─── symbol_embeddings (向量)
        └─── symbol_summaries (摘要)
```

### file_contents（文件内容表）

按 content_hash 唯一存储，相同内容只存一次。

| 字段 | 类型 | 说明 |
|------|------|------|
| content_hash | TEXT PK | SHA-256 内容哈希 |
| language | TEXT | 语言（rust/typescript/...） |
| total_lines | INTEGER | 总行数 |
| first_seen_at | REAL | 首次发现时间戳 |

### file_instances（文件实例表）

一个内容可出现在多个工作区的多个路径（path 为副键）。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 实例 ID |
| workspace_id | INTEGER FK | 工作区 ID |
| rel_path | TEXT | 相对路径 |
| abs_path | TEXT | 绝对路径 |
| current_content_hash | TEXT FK | 当前内容 hash |
| mtime | REAL | 修改时间 |
| status | TEXT | pending/parsed/stale/deleted |
| module_path | TEXT | 模块路径 |

UNIQUE 约束：`(workspace_id, rel_path)`

### symbol_contents（符号内容表）

按 content_hash 唯一，相同函数体只存一次（跨文件/跨仓库去重）。

| 字段 | 类型 | 说明 |
|------|------|------|
| content_hash | TEXT PK | 符号内容 SHA-256 |
| name | TEXT | 符号名 |
| kind | TEXT | fn/method/class/struct/enum/trait |
| content | TEXT | 完整源代码 |
| signature | TEXT | 签名 |
| has_comment | INTEGER | 是否有注释 |
| comment_content | TEXT | 注释内容 |
| qualified_name | TEXT | 限定名 |

### symbols（符号表 — 当前快照）

查询优化用的当前快照表。

| 字段 | 类型 | 说明 |
|------|------|------|
| id | INTEGER PK | 符号 ID |
| file_instance_id | INTEGER FK | 文件实例 |
| symbol_hash | TEXT FK | 符号内容 hash |
| name | TEXT | 符号名 |
| kind | TEXT | 类型 |
| visibility | TEXT | public/private |
| start_line / end_line | INTEGER | 位置 |
| signature | TEXT | 签名 |
| has_comment | INTEGER | 注释标志 |
| qualified_name | TEXT | 限定名 |
| depth | INTEGER | 调用深度 |

### calls（调用关系表 — 当前快照）

| 字段 | 类型 | 说明 |
|------|------|------|
| caller_id | INTEGER FK | 调用者符号 ID |
| caller_name | TEXT | 调用者名 |
| callee_name | TEXT | 被调用者名 |
| callee_qualified | TEXT | 被调用者限定名 |
| callee_file | TEXT | 被调用者文件 |
| callee_id | INTEGER | 被调用者符号 ID（解析后） |
| call_line | INTEGER | 调用行号 |
| is_cross_file | INTEGER | 是否跨文件调用 |

### file_versions（文件版本表）

记录每个文件实例的所有历史版本。

| 字段 | 类型 | 说明 |
|------|------|------|
| file_instance_id | INTEGER FK | 文件实例 |
| version_num | INTEGER | 版本号（递增） |
| content_hash | TEXT FK | 内容 hash |
| mtime | REAL | 修改时间 |
| parsed_at | REAL | 解析时间 |
| is_current | INTEGER | 是否当前版本 |
| is_deleted | INTEGER | 是否已删除 |
| commit_hash | TEXT | 关联 commit |
| ast_cache | BLOB | tree-sitter AST 序列化字节流（v28 新增，支持增量解析） |

### file_symbol_versions（文件-符号关联表）

记录每个文件版本包含哪些符号及位置，用于历史回溯和注释恢复。

| 字段 | 类型 | 说明 |
|------|------|------|
| file_version_id | INTEGER FK | 文件版本 |
| symbol_hash | TEXT FK | 符号内容 hash |
| qualified_name | TEXT | 限定名 |
| start_line / end_line | INTEGER | 位置 |
| module_path | TEXT | 模块路径 |
| depth | INTEGER | 调用深度 |
| is_deleted | INTEGER | 是否已删除 |

### 守护者架构表（v10）

| 表 | 说明 |
|----|------|
| guardrail_rules | 安全护栏规则定义（DB/API/Incident 三类） |
| guardrail_findings | 规则扫描结果（open/resolved/wontfix） |
| change_impacts | 跨层影响分析结果 |
| evolution_metrics | 演化指标缓存（变更频率、缺陷数、热点分） |
| defect_patterns | 缺陷模式库 |
| defect_fixes | 缺陷修复案例 |

### 任务与编辑审计表（v7 + v12 + v15 + v24）

| 表 | 说明 |
|----|------|
| tasks | 任务（open/in_progress/review/applied/closed/reverted）。v15 加 `parent_id` / `depth` / `sort_order` 支持父子任务树；v24 加 `applied_at` 字段支持 review → applied → closed 流转 |
| task_steps | 任务步骤（pending/in_progress/done/failed/blocked） |
| change_audit | 变更审计日志（hash + diff） |
| file_edit_audit | propose_edit 审计流水线（pending/applied/reverted/failed） |

### 归档与 GC 表（v14 + v19 + v20）

| 表 | 说明 |
|----|------|
| archived_files | 归档文件元数据（v14）。`file_instances.status='archived'` 时记录归档原因、符号/调用数快照、归档时间戳，便于 GC 可恢复归档闭环 |
| gc_policies | GC 策略配置（v19）。每个 workspace 一行，包含 `older_than_days` / `keep_versions` / `include_external` / `backup_enabled` 等 retention 参数 |
| gc_runs | GC 运行审计（v20）。每次 retention/archive/purge 记一行，含 `policy_json`（策略参数）/ `candidate_counts`（候选明细）/ `deleted_counts`（实删明细）/ `backup_path`（备份路径）/ `status`（running/completed/failed） |

### 外部依赖表（v16 + v18）

| 表 | 说明 |
|----|------|
| external_symbols | 外部符号表（v16）。存储标准库和第三方包的函数/类/常量，用于跨文件调用解析时查找项目外的被调符号。`qualified_name` 唯一，关联 `package_versions` |
| package_versions | 包版本表（v16，v18 增强）。`package_name` + `package_version` 联合主键。v18 加 `last_seen_at`（依赖清单最近看到时间）/ `last_used_at`（最近被调用解析命中时间）/ `import_source`（manifest/manual/stdlib）用于冷数据追踪 |

### 任务-符号变更归因表（v17）

| 表 | 说明 |
|----|------|
| task_symbol_changes | 任务-符号变更归因（v17）。记录一次任务/步骤/编辑行为为什么导致某个符号版本变化，含 `task_id` / `step_id` / `edit_audit_id` / `symbol_hash_before` / `symbol_hash_after` / `change_type` / `source`。事实层仍是 `file_symbol_versions` / `symbol_contents`，本表只做归因 |

### 任务质量门禁表（v21）

| 表 | 说明 |
|----|------|
| task_quality_findings | 任务质量门禁发现（v21）。把 Semgrep、复杂度、调用链一致性、scope violation、i18n 硬编码等质量问题挂到 task/step 上。`severity`：info/warn/error/block（block 阻止任务进入 done）；`status`：open/resolved/wontfix；`source`：semgrep/file_health/call_chain/scope/i18n/manual |

### 审计签名链表（v22 + v29）

| 表 | 说明 |
|----|------|
| audit_chain | 审计签名链（v22）。为 `task_quality_findings` / `change_audit` / `file_edit_audit` 等关键审计表生成可验证的 hash/HMAC 链。每条记录含 `payload_hash` + `prev_signature` + `record_signature`，形成链式结构。`signing_key_id`：`'local'` 表示本地 SHA-256 链；可通过环境变量 `CALLWARDEN_AUDIT_HMAC_KEY` 或 `~/.callwarden/audit.key` 切换到 HMAC-SHA256 |
| audit_key_rotations | 审计签名密钥轮换表（v29）。记录每次密钥轮换的 `key_id` / `key_secret` / `rotated_at` / `is_active`，支持按时间点选择对应密钥验证旧记录。轮换后新记录用新 key 签名，旧记录保持原签名；验证时按 `signing_key_id` 查找对应密钥 |

### Agent Rule Memory 表（v23）

| 表 | 说明 |
|----|------|
| agent_rule_candidates | 候选规则（pending/accepted/rejected），由 Agent 观察或从 task_quality_findings 自动提取 |
| agent_rules | 已生效规则（active/deprecated/removed），accept 后写入，按 scope 匹配注入到上下文 |
| agent_rule_sync_log | AGENTS.md 同步日志（dry_run/apply 都记录，含 before/after hash） |

### 自举闭环与代码克隆表（v25 + v27）

| 表 | 说明 |
|----|------|
| workspace_scan_runs | 工作区扫描基线记录（id / workspace_id / purpose / task_id / step_id / baseline_type / git_head / git_merge_base / git_status_hash / root_mtime / file_count / manifest_hash / changed_files_json / metadata_json / started_at / completed_at / status）。`purpose` 取值 `bootstrap`（启动时基线）/ `task_capture`（task_capture_diff 触发）；`status` 走 `running → completed/failed`；三个索引 `idx_workspace_scan_runs_workspace/task/git_head` |
| clone_pairs | 重复代码检测对（id / workspace_id / symbol_a_id / symbol_b_id / clone_type / similarity / token_hash / lines_a / lines_b / detected_at）。`clone_type` 取值 1=Type-1 完全相同 / 2=Type-2 重命名 / 3=Type-3 微调；`similarity` 0.0-1.0；五个索引 `idx_clone_pairs_workspace/symbol_a/symbol_b/type` + `idx_clone_pairs_unique`（UNIQUE）|

## Mixin 架构

### 设计原理

CodeGraphDB 通过 **35 个功能 Mixin 多继承**组装（不含 db_base.py 基类；其中 3 个来自 analyzers/），每个 Mixin 负责一个功能领域。这种设计：

- **单一职责**：每个 Mixin 只关心自己的表和查询
- **按需组合**：主类只需声明继承即可获得功能
- **易于扩展**：新增功能只需添加新 Mixin
- **避免上帝类**：`db.py` 仅 92 行，职责在 40 个文件中分散

### Mixin 列表（35 个功能 Mixin + 1 基类 = 36 项）

| # | Mixin | 文件 | 职责 |
|---|-------|------|------|
| 1 | CodeGraphBase | db_base.py | 核心基类：连接、schema 迁移、工作区管理 |
| 2 | BuildMixin | db_build.py | 构建：文件扫描、解析、调用图构建 |
| 3 | QueryMixin | db_query.py | 查询：符号查询、状态、模块图 |
| 4 | CommentMixin | db_comment.py | 注释恢复 |
| 5 | GitMixin | db_git.py | Git 集成 |
| 6 | MetricsMixin | db_metrics.py | 代码度量（圈复杂度、耦合度、健康检查） |
| 7 | SummaryMixin | db_summary.py | 代码摘要与项目简报 |
| 8 | VectorMixin | db_vector.py | 向量嵌入与语义搜索 |
| 9 | OwnershipMixin | db_ownership.py | 文件所有权（CODEOWNERS + git blame） |
| 10 | TaskMixin | db_tasks.py | 任务驱动 MCP（任务/步骤/审计） |
| 11 | CallChainMixin | analyzers/call_chain.py | 调用链分析 |
| 12 | IssueAnalyzerMixin | analyzers/issues.py | 缺陷检测 |
| 13 | CoverageMixin | analyzers/coverage.py + db_coverage.py | 覆盖率统计与智能分析 |
| 14 | GuardrailMixin | db_guardrail.py | 生产安全护栏 |
| 15 | ImpactMixin | db_impact.py | 变更影响智能（blast_radius、跨层） |
| 16 | EvolutionMixin | db_evolution.py | 代码演化智能（频率、热点、churn） |
| 17 | DefectKbMixin | db_defect_kb.py | 缺陷知识库 |
| 18 | TokenSavingsMixin | db_token_savings.py | Token 节省账本 |
| 19 | BranchMixin | db_branch.py | 分支感知图谱 |
| 20 | EditSafetyMixin | db_edit.py | 安全文件编辑（propose_edit） |
| 21 | CrossRepoMixin | db_cross_repo.py | 跨仓库分析 |
| 22 | LspMixin | db_lsp.py | LSP 集成 |
| 23 | CheckGateMixin | db_check_gate.py | 检查门禁（F6） |
| 24 | AgentRulesMixin | db_agent_rules.py | Agent Rule Memory：候选规则审核、scope 匹配注入、AGENTS.md 同步 |
| 25 | BootstrapMixin | db_bootstrap.py | 自举闭环：扫描基线检测（workspace_scan_runs）、task_capture_diff 闭环入口、bootstrap_status 健康摘要 |
| 26 | AuditChainMixin | db_audit_chain.py | 审计签名链：关键审计表的签名记录与验证 |
| 27 | CasMixin | db_cas.py | Global CAS（Content-Addressable Storage）缓存池，相同文件跨工作区只解析一次 |
| 28 | CloneDetectionMixin | db_clone_detection.py | 重复代码检测：基于 tree-sitter token 序列检测 Type-1/2/3 克隆 |
| 29 | CloneGroupsMixin | db_clone_groups.py | Clone Groups 存储：替代 clone_pairs 的分组存储 |
| 30 | DaemonMixin | db_daemon.py | Enterprise daemon workspace registry：workspace 注册、查询、状态管理 |
| 31 | ExternalMixin | db_external.py | 第三方包解析（多语言通用版）：从已安装包提取函数/类/常量符号 |
| 32 | GcMixin | db_gc.py | 代码图谱 GC：分代回收机制（新生代/老年代） |
| 33 | JobsMixin | db_jobs.py | 后台任务系统：clone/vector/semgrep 等耗时操作异步执行 |
| 34 | MigrateMixin | db_migrate.py | 数据库迁移工具：旧版多库架构迁移到用户级单库架构 |
| 35 | StdlibMixin | db_stdlib.py | 标准库符号表：管理 Python 标准库符号信息，用于跨文件调用解析 |
| 36 | TaskAttributionMixin | db_task_attribution.py | 任务-符号变更归因层：链接 edit audit 到符号版本变更 |
| 37 | TaskQualityMixin | db_task_quality.py | 任务质量门禁：承载任务完成门禁发现 |
| 38 | TestsMixin | db_tests.py | 测试关联：建立 test_fn ↔ 被测 fn 的关联关系 |
| 39 | ToolchainMixin | db_toolchain.py | Toolchain CAS：工具链注册与存储 |
| 40 | WorkspaceManifestMixin | db_workspace_manifest.py | Workspace manifest：clean snapshot 和 dirty overlay |

### 组装方式

`db.py` 的核心实现：

```python
class CodeGraphDB(
    CodeGraphBase,
    BuildMixin,
    QueryMixin,
    CommentMixin,
    # ... 共 35 个功能 Mixin（不含 CodeGraphBase）
    CheckGateMixin,
    AgentRulesMixin,
):
    """代码知识图谱数据库 - 整合所有功能模块的主类"""

    def __init__(self, db_path: str = "", workspace_root: Optional[str] = None):
        super().__init__(db_path=db_path, workspace_root=workspace_root)
```

所有 Mixin 共享同一个 SQLite 连接和游标（由 CodeGraphBase 提供），通过 `self.conn` 和 `self.cursor` 访问数据库。

## Agent Rule Memory 架构

Agent Rule Memory 是 Call Warden 的项目规则记忆系统，让 Agent 能够把任务执行中观察到的规律沉淀为可复用的规则，并在后续任务中按上下文自动注入，形成"观察 → 沉淀 → 审核 → 注入 → 同步"的闭环。

### 数据流

```
[Agent 观察]                     [自动提取]
     │                               │
     ▼                               ▼
 rule_candidate_create     extract_rule_candidates_from_quality_findings
     │                               │
     └──────────┬────────────────────┘
                ▼
   agent_rule_candidates (pending)
                │
       ┌────────┴────────┐
       ▼                 ▼
 rule_candidate_     rule_candidate_
 accept              reject
       │
       ▼
   agent_rules (active)
       │
       ├──► get_applicable_rules(context) ──► 注入到 task_next_step /
       │                                         work_next_job /
       │                                         get_symbol /
       │                                         file_symbol_content
       │
       └──► rule_sync_agents_md(dry_run/apply) ──► AGENTS.md 标记区
                                                    + agent_rule_sync_log
```

### Scope 匹配

`agent_rules.scope_json` 是一个 JSON 对象，支持以下字段：

| 字段 | 类型 | 匹配方式 |
|------|------|----------|
| `languages` | `list[str]` | 上下文 `languages` 任一命中即匹配（OR） |
| `file_patterns` | `list[str]` | glob 匹配（如 `src/api/**/*.py`） |
| `symbol_kinds` | `list[str]` | `fn` / `method` / `class` / `struct` 等 |
| `actions` | `list[str]` | `edit` / `delete` / `create` 等 |
| `finding_types` | `list[str]` | 与 `task_quality_findings.finding_type` 对齐 |
| `module_prefixes` | `list[str]` | 前缀匹配（如 `crate::payment::`） |

**匹配规则**：
- 空 scope = 全局匹配
- 同字段内多值 OR
- 不同字段间 AND
- 命中字段越多，匹配精度越高

**排序**：severity 优先级（`critical > error > warning > info`）→ 命中字段数（越多越靠前）→ `updated_at` 倒序。

### 注入点（fail-soft）

规则注入采用 **fail-soft** 模式：规则查询失败时降级为空列表，不阻塞主流程。已接入的注入点：

| 注入点 | 返回字段 | 上下文来源 |
|--------|----------|-----------|
| `task_next_step` | `applicable_rules` | 任务关联的 file/symbol/kind |
| `work_next_job` | `project_rules` + `context.applicable_rules` | 当前 job 的符号上下文 |
| `build_structured_instruction` | `project_rules` | 全局 active 规则 |
| `get_symbol` | `applicable_rules` | 符号的语言/类型/文件 |
| `file_symbol_content` | `applicable_rules` | 文件的语言/路径 |

### AGENTS.md 同步

`rule_sync_agents_md` 把 active 规则同步到 AGENTS.md 的标记区（`<!-- CALLWARDEN_RULES_START -->` ~ `<!-- CALLWARDEN_RULES_END -->`），不触碰人工维护区域。

**安全策略**：
- `dry_run=True`（默认）：只返回 `preview`，不写文件
- `dry_run=False`（apply）：只替换标记区之间内容，保留标记区外的人工内容
- 标记区不存在时返回 `error` + `suggested_block`（需先调用 `rule_insert_agents_md_block`）
- 每次同步写入 `agent_rule_sync_log`，记录 `before_hash` / `after_hash` / `rule_count`

### 启动时自动同步（C2）

Call Warden 在以下两个入口点自动触发 `rule_sync_agents_md(dry_run=False)`，让规则无需手动同步即可生效：

| 入口点 | actor | 触发时机 | fail-soft |
|--------|-------|----------|-----------|
| `cw server` (MCP Server 启动) | `mcp_server_startup` | `create_mcp_server()` 之后、`server.run()` 之前 | 同步失败不阻断启动，输出到 stderr |
| `cw --refresh-all` (CLI 刷新) | `cli_refresh_all` | `db.build_full_graph()` 之后 | 同步失败不阻断 refresh |

**设计要点**：
- **fail-soft 原则**：同步失败（标记区不存在、权限不足、DB 异常等）不阻断主流程，仅输出提示
- **stdio 协议安全**：MCP Server 启动时的同步摘要输出到 `stderr`，避免污染 stdio 协议输出
- **幂等性**：多次调用结果一致，`after_hash` 在内容未变时保持相同
- **日志追溯**：每次同步写入 `agent_rule_sync_log`，通过 `actor` 字段区分触发来源

## 自举闭环架构（Bootstrap Closure）

自举闭环（Bootstrap Closure）是 Call Warden 把"外部 Agent 真实文件改动"反向
同步回任务/变更/符号/审计事实层的闭环机制，让 Call Warden 自身也能作为"被观测
对象"接受完整性校验。

### 解决的问题

| 场景 | 没有 capture-diff 时 | 有 capture-diff 后 |
|------|---------------------|--------------------|
| 外部 Agent（Claude Code/Codex）直接编辑磁盘文件 | 图谱与磁盘脱节，task 无法归因真实变更 | 自动捕获变更、归因到 task/step、生成 quality findings |
| 任务完成审查 | 只靠 Agent 自报 `task_report`，缺验证 | `task_capture_diff` 提供磁盘事实，与 Agent 声明交叉验证 |
| 跨会话审计 | 多个 Agent 改动难以追溯 | 每次 capture 写 `workspace_scan_runs` + `change_audit` + `audit_chain` |
| 规则注入空转 | active_rules 表为空，注入点返回空列表 | `rule_seed_bootstrap --apply` 写入 5 条核心规约，注入稳定 |

### 数据流

```
[work_next_job]                    [外部 Agent 编辑]
     │                                   │
     ▼                                   ▼
 task_next_step                     磁盘文件变更
     │                                   │
     │     ┌─────────────────────────────┘
     │     │
     │     ▼
     │  task_capture_diff(task_id, step_id, dry_run=False)
     │     │
     │     ├─► get_workspace_changes_since(scan_baseline)
     │     │       └─► 变更文件清单
     │     │
     │     ├─► workspace_scan_runs（status: running → completed）
     │     │       └─► 新基线 git_head / manifest_hash
     │     │
     │     ├─► change_audit（每文件一条，hash_before/after）
     │     │       └─► audit_chain（签名链：payload_hash + prev_sig + record_sig）
     │     │
     │     ├─► task_symbol_changes（best-effort 关联）
     │     │
     │     └─► run_task_completion_review
     │             └─► task_quality_findings（scope / call_chain / file_health / i18n）
     │
     ▼
 [quality_decision]                 [next_action]
   pass / warn / block               review / fix / noop / commit
     │
     ▼
 task_report_step（Agent 声明完成）→ task_apply → task_close
```

### 关键表与字段

- `workspace_scan_runs`（v25）：扫描基线记录，17 个字段
  - `purpose`：`bootstrap`（启动时基线）/ `task_capture`（task_capture_diff 触发）
  - `baseline_type`：`git`（默认）/ `manifest` / `mtime`
  - `git_head` / `git_merge_base` / `git_status_hash`：基线锚点
  - `changed_files_json`：本次扫描检测到的变更文件清单
  - `status`：`running → completed/failed`
  - 三个索引：`idx_workspace_scan_runs_workspace/task/git_head`

- `change_audit`：每文件一条变更记录
  - `hash_before` / `hash_after`：内容哈希前后对比
  - `author`：`capture-diff` 标识本条由 task_capture_diff 写入

- `audit_chain`：每条 change_audit 对应一条签名记录
  - `payload_hash`：当前记录内容的 SHA-256
  - `prev_signature`：上一条记录的 `record_signature`
  - `record_signature`：本条签名（SHA-256 链或 HMAC-SHA256）
  - `signing_key_id`：`local` / `hmac`
  - `security_level`：`hash_only` / `hmac`

### 闭环命令映射

| 阶段 | CLI | MCP | 说明 |
|------|-----|-----|------|
| 种子规则 | `cw rule seed-bootstrap --apply` | `rule_seed_bootstrap(dry_run=False)` | 写入 5 条 `AR-bootstrap-*` |
| 健康摘要 | `cw bootstrap status` | `bootstrap_status()` | 一行汇总 db_stale/规则/质量发现/审计链/扫描基线/任务/推荐 |
| 捕获改动 | `cw task capture-diff <T> --apply` | `task_capture_diff(task_id, dry_run=False)` | 写 scan_runs + change_audit + audit_chain + findings |
| 审计验证 | `cw audit verify` | `audit_chain_verify(table_name, limit)` | 验证签名链完整性与签名匹配 |
| 任务报告 | `cw task report <T> <S> --result "..."` | `task_report_step(task_id, step_id, result)` | Agent 声明完成 step |
| 任务应用 | `cw task apply <T> --reviewer <S>` | `task_apply(task_id, reviewer)` | review → applied |
| 任务关闭 | `cw task close <T> --reviewer <S>` | `task_close(task_id, reviewer)` | applied → closed（带级联） |

### BootstrapMixin 职责

`BootstrapMixin`（第 25 个 Mixin，`db/db_bootstrap.py`）承载自举闭环的核心方法：
- `bootstrap_status()`：聚合 health 摘要（不写库）
- `task_capture_diff()`：捕获磁盘变更到任务上下文（写库）
- `get_workspace_changes_since()`：检测自基线以来的变更文件（只读）

`rule_seed_bootstrap()` 在 `AgentRulesMixin`（`db/db_agent_rules.py`）中实现，
通过固定 ID `AR-bootstrap-*` 实现幂等。

### 与其他闭环的关系

- **Agent Rule Memory 闭环**：自举闭环负责"种子化"和"注入稳定数据源"，
  Agent Rule Memory 闭环负责"自动沉淀"和"scope 匹配注入"。二者共同构成
  Call Warden 的行为约束系统。
- **任务状态机**：自举闭环的 `task_capture_diff` 在 `task_report_step`
  之前调用，提供磁盘事实；任务状态机的 `apply/close` 依赖 quality findings
  决策。
- **审计链**：自举闭环写入的 `change_audit` 与 `task_quality_findings`
  都会被 `sign_audit_record` 签名，纳入 `audit_chain`，统一由
  `audit_chain_verify` 验证。

## 安全机制

### 1. 原子写入（atomic_write_file）

所有文件写入走 `config.py:atomic_write_file()`：

1. 在同目录创建临时文件（保证同一文件系统）
2. 写入内容并 `flush` + `fsync` 确保落盘
3. `os.replace` 原子替换目标文件（Windows/Linux 均支持）
4. 失败时清理临时文件

避免半写入状态导致数据损坏（SEC-001 安全修复）。

### 2. 路径校验

所有涉及文件路径的操作（propose_edit、restore_comment 等）都进行：
- 路径规范化（`norm_path` 统一正斜杠）
- 工作区边界检查（防止越权访问）
- 绝对路径解析（消除 `..` 等相对路径歧义）

### 3. 编辑审计（propose_edit 流水线）

Agent 每次编辑文件都记录完整审计：

```
1. 计算编辑前文件 SHA-256 (file_hash_before)
2. 计算编辑后内容 SHA-256 (file_hash_after)
3. 生成 diff 摘要（新增/删除行数）
4. 写入 file_edit_audit 表 (status=pending)
5. dry_run=True → 返回预览
   dry_run=False → 原子写入文件
6. 更新 audit 记录 status=applied, applied_at=now
```

支持回滚（`revert_edit` 标记 status=reverted）和完整历史查询（`get_edit_history`）。

### 4. 护栏阻断（Before-Edit Contract）

任务驱动编排中，编辑类步骤在领取时（`task_next_step`）自动调用护栏检查：

- `decision=block`：步骤状态置为 `blocked`，Agent 必须先处理告警再调用 `task_resolve_block` 恢复
- `decision=warn`：步骤可执行，但返回 `guardrail_warning` 提醒
- `decision=pass`：正常执行

护栏规则分三类：
- `db_safety`：数据库安全（如禁止删除迁移文件）
- `api_compat`：API 兼容性
- `incident`：事故预防

### 5. 检查门禁（F6）

`task_report_step` 在步骤成功且有文件变更时自动触发检查门禁：
- 语法检查
- Semgrep 扫描

失败会自动在任务中插入 `fix_gate_failure` 步骤，Agent 必须修复后才能继续。

### 6. 内容 Hash 去重

所有内容（文件/符号）按 SHA-256 hash 去重存储：
- 防止重复存储相同内容
- 跨文件/跨仓库的相同函数自动关联
- 历史版本通过 hash 关联，不存储完整副本

### 7. 任务级联 close（T-1783309017863-a1b6）

任务状态机 `open → in_progress → review → applied → closed` 实现级联 close 机制：

**父任务状态自动推进**：
- `open → in_progress`：第一个子任务被 `task_next_step` 领取时，遍历父任务链推进
- `in_progress → review`：所有子任务都是 review/applied/closed 时，由 `_update_parent_status` 递归推进
- `review → applied → closed`：最后一个子任务 apply 时由 `_cascade_close_if_ready` 原子推进

**级联 close 触发**：最后一个子任务被 `task_apply` 时
1. 子任务自己 review → applied
2. 查询所有兄弟子任务状态
3. 全部 applied/closed → 原子级联 close：
   - close 所有 applied 兄弟任务
   - 父任务 review → applied → closed 一次性推进
   - 递归向上检查祖父层级联

**状态约束**：
- 叶子任务（无子任务）：可手动 apply + close
- 父任务（有子任务）：禁止手动 apply/close，由系统自动级联触发

设计原则：写代码的 Agent 不能自己 apply/close，必须由其他会话的 LLM 审核执行。
详细设计见 [docs/design/task-state-machine.md](design/task-state-machine.md)。

### 7.1 父任务自身步骤完成后自动级联 close（T-1783428495806-71f6）

**问题背景**：当父任务既有自身步骤（如 `verify`/`build`）又有子任务时，存在一个
边缘场景导致父任务卡在 `review` 状态无法 `closed`：

1. 子任务先被 `task_apply` 推到 `applied`
2. `_update_parent_status` 检查时把父任务推到 `review`（所有子任务视为完成）
3. 之后父任务自身步骤完成，`task_report_step` 进入 review 分支
4. 但父任务状态已经是 `review`（不是 `open`/`in_progress`），原 `if` 分支被跳过
5. 级联 close 调用不会触发，父任务永远卡在 `review`

**修复方案**：在 `task_report_step` 中，父任务自身步骤全部完成后，**无论**
`t_status` 是刚推到 `review` 还是已经是 `review`，都重新查询当前状态并检查：

```python
if pending_count == 0:
    if t_status in (OPEN, IN_PROGRESS):
        UPDATE tasks SET status = REVIEW WHERE id = ?
    # 关键：重新查询当前状态（可能已被 _update_parent_status 提前推到 REVIEW）
    current_status = SELECT status FROM tasks WHERE id = ?
    if current_status == REVIEW:
        has_subtasks = SELECT COUNT(*) FROM tasks WHERE parent_id = ?
        if has_subtasks:
            cascaded = _cascade_close_if_ready(task_id, "system", now)
```

**触发条件**（全部满足才级联）：
- 任务自身所有步骤完成（`pending_count == 0`）
- 任务当前状态为 `review`（无论是刚推入还是已被提前推入）
- 任务有子任务（`has_subtasks == True`）
- 所有子任务都是 `applied`/`closed`（由 `_cascade_close_if_ready` 内部检查）

**不影响叶子任务**：叶子任务（无子任务）保持人工 apply/close 流程，不会自动级联。
**不影响失败步骤**：步骤失败（`success=False`）时不进入此分支。

**测试覆盖**：`tests/test_task_parent_cascade_fix.py`（10 个测试）
- 父任务有 verify 步骤 + 单子任务 → 自动 close
- 父任务有多个步骤 + 多个子任务 → 自动 close
- 多层任务树（祖父-父-子）递归级联
- 步骤失败时不会自动 close
- 部分子任务未完成时不会自动 close
- 叶子任务不受影响

### 8. 任务 reopen 机制（T-1783413215675-3aae）

任务状态机支持 `review`/`applied`/`closed` → `in_progress` 的回退（reopen），
用于 code review 发现已 applied/closed 的任务有问题需要修复，
或向已 closed 的父任务挂入新子任务。

**状态转换**：
- `review`/`applied`/`closed` → `in_progress`（清理 `applied_at`/`closed_at` 时间戳）
- `open`/`in_progress` → 不变（无需 reopen）
- 在 `audit_chain` 中记录 reopen 事件（fail-soft，签名失败不阻断）

**两种触发方式**：

1. **自动触发**（`task_create(parent_id=closed_task)`）：
   向已 `closed`/`applied`/`review` 的父任务挂入新子任务时，`_reopen_parent_chain_if_needed`
   检查父任务状态和兄弟子任务状态：
   - 父任务 `open`/`in_progress` → 直接挂，不改状态
   - 父任务 `closed`/`applied`/`review` + **检查兄弟子任务状态**（`check_siblings=True`）：
     - 所有兄弟子任务都是 `closed`（或无兄弟）→ reopen 父任务为 `in_progress`
     - 有兄弟子任务非 `closed`（如 `open`/`in_progress`）→ 直接挂，不 reopen
       （父任务下还有工作在进行中，不需要重新激活）
   - 父任务被 reopen 后，递归向上检查祖父任务（`check_siblings=False`，无条件 reopen），
     确保整条链回到工作状态

2. **手动触发**（`cw task reopen <task_id>`）：
   用户主动 reopen 一个任务，**不检查兄弟子任务状态**，直接 reopen 整条祖先链。
   递归向上时同样用 `check_siblings=False`（无条件 reopen）。

**设计理由**：挂新子任务时需要同时考虑父任务状态和兄弟子任务状态。若父任务已
`closed` 但还有 `open` 的兄弟子任务（数据不一致或误操作），直接挂新子任务即可，
不自动改父任务状态；若所有兄弟都已 `closed`，说明之前的工作完成，新子任务表示新
需求来了，应 reopen 父任务。手动 reopen 是用户明确操作，直接 reopen 整条链。

**CLI 命令**：`cw task reopen <task_id> [--reviewer <identity>] [--reason "<原因>"]`
**MCP 工具**：`task_reopen(task_id, reviewer, reason)`
**实现**：`db/db_tasks.py` 的 `_reopen_parent_chain_if_needed()` 和 `task_reopen()` 方法

## 性能优化

### 1. PyO3 加速（rust_ext/）

可选的 Rust 扩展模块，通过 PyO3 暴露给 Python：
- 高频路径（如 hash 计算、路径规范化）用 Rust 实现
- release 构建零开销
- 未编译时自动回退到 Python 实现

构建：`cd rust_ext && maturin develop --release`

### 2. 向量索引（BLOB + Rust/numpy 余弦相似度，sqlite-vec 待落地）

> D1 文档声明修正（评审报告 2026-07-20）：实际实现是 BLOB 存储 + Rust 批量余弦相似度（`callwarden_core.batch_cosine_similarity`），回退到 numpy 矩阵运算。`pyproject.toml` 仍声明 `sqlite-vec>=0.1` 依赖，但 `vec0` 虚拟表尚未在 schema 中落地。

语义搜索使用 BLOB 存储 + Rust/numpy 余弦相似度：
- 768 维向量（jina-v2-base-code 模型）
- `_load_all_embeddings()` 读取全部 embedding 到内存，`_batch_cosine()` 批量计算余弦相似度
- 嵌入结果缓存在 `symbol_embeddings` 表（BLOB 列）
- 向量服务不可用时自动回退到关键词匹配
- 适合中小规模代码库（< 100k 符号）；大规模场景待落地 sqlite-vec vec0 虚拟表 + KNN 查询

### 3. SQLite WAL 模式

- 多读者并发，单写者排队
- 读写不互相阻塞
- 适合 MCP Server 长连接场景

### 4. 索引优化

Schema 中为所有高频查询字段创建索引：
- `idx_symbols_name` / `idx_symbols_qualified` / `idx_symbols_module`
- `idx_calls_caller` / `idx_calls_callee`
- `idx_file_versions_hash` / `idx_file_versions_current`
- 30+ 索引覆盖所有查询路径

### 5. 内容寻址去重

相同内容只存一次（file_contents / symbol_contents），大幅减少存储体积和重复解析。

### 6. 查询路径设计决策（GraphStore vs SQL 路由）

> 对应 [_feature_matrix.md F20](../_feature_matrix.md) 设计决策项。

Call Warden 同时维护两套查询路径：**Rust GraphStore（CSR 内存索引）** 和 **SQLite SQL**。`DaemonClient` 按查询类型选择最优路径（详见 [server/daemon_client.py](../server/daemon_client.py) `_remote_query` / `_sql_fallback_*`）。

**路由策略**（基于 BR3 实测，1M 符号）：

| 查询 | GraphStore | SQL | 选用 | 原因 |
|------|-----------|-----|------|------|
| `get_callers` | 0.003ms | 0.165ms | **GraphStore**（54x 加速） | CSR backward 遍历 + `by_callee_name` 哈希索引，完全跳过 SQL |
| `get_callees` | 0.000ms | 0.146ms | **GraphStore**（330x 加速） | CSR forward 遍历，零 SQL |
| `get_symbol` | 0.008ms | 0.013ms | **GraphStore**（1.66x 加速） | `by_qualified_name` HashMap O(1) |
| `call_chain BFS d=5` | 24.4ms | 96.4ms | **GraphStore**（3.95x 加速） | CSR 内存遍历 vs SQL CTE 递归 |
| `batch_callers(100)` | 0.29ms | 2.45ms | **GraphStore**（8.56x 加速） | 单次 CSR 遍历多次查询 |
| `search_symbols` | 3.132ms（memchr 子串） | 2.354ms（FTS5 trigram） | **FTS5**（**1.33x 加速**） | 见下方根因 |

**F20 设计决策根因**：`search_symbols` 走 SQL 是有意的，不是遗漏：

1. **GraphStore 实现路径**（[rust_ext/src/graph.rs:756 `search_symbols`](../rust_ext/src/graph.rs) + [rust_ext/src/graph.rs:2066 `search_symbols_rust`](../rust_ext/src/graph.rs)）：把所有符号名拼接成 `search_pool_lower`，用 `memchr::memmem::find` SIMD 一次扫描整个池查找 query 子串。复杂度 O(N×L)（N=符号数，L=query 长度），即使有 SIMD 加速，符号表 1M 时仍需扫 1M 次。

2. **SQL 实现路径**（`_sql_fallback_search_symbols` → `db_query.search_symbols`）：实际有三层 fallback：
   - **FTS5 trigram 倒排索引**（主路径，v31 schema 引入）：`symbols_fts MATCH ?`，trigram tokenizer 把 name/qname 切成 3-gram 倒排索引，查询时把 query 切 3-gram 取交集。复杂度 O(log N + M)（M=匹配数），是真正的倒排索引路径
   - **Rust GraphStore memchr 子串扫描**（fallback 1）：FTS5 不可用或 query < 3 字符时启用
   - **LIKE `%query%` 全表扫描**（fallback 2）：FTS5 + Rust 都不可用时启用，O(N) 全表扫描

3. **设计选型依据**：
   - `search_symbols` 是**子串匹配**场景（`LIKE '%query%'`），不是前缀匹配（`LIKE 'query%'`）
   - 子串匹配不能走 B-tree 索引范围扫描（只有前缀匹配可以），必须用倒排索引
   - FTS5 trigram 是真正的倒排索引（3-gram → rowid 列表），Rust memchr 是线性 SIMD 扫描
   - GraphStore 的内存索引结构（CSR + HashMap）适合**精确查找**（`get_callers` 按 callee_name / `get_symbol` 按 qname），不适合范围扫描
   - 强行用 memchr 全扫描做子串搜索，比 FTS5 trigram 倒排索引慢 25%

4. **路由反转决策**（2026-07-19 修正）：
   - **原路由**（2026-07）：Rust memchr 优先 → FTS5 fallback → LIKE fallback
   - **新路由**：FTS5 trigram 优先 → Rust memchr fallback → LIKE fallback
   - **修正原因**：实测 FTS5 trigram 比 Rust memchr 快 25%（1M 符号 2.354ms vs 3.132ms），原路由把更慢的 Rust 放在前面是 B-P7b Rust 短路原则的误用
   - **保留 Rust 作为 fallback**：FTS5 不可用（schema < v31）或 query < 3 字符（trigram 不支持）时启用，Rust memchr 仍比 LIKE 全表快

5. **未来优化方向**（不在当前路线图）：
   - 若追求 daemon 路径一致性，可在 Rust 端构建 trigram 倒排索引（3-gram → symbol_ids HashMap），与 FTS5 等效但跳过 SQL 调用
   - 或保留 FTS5 路径作为永久选择（当前决策），承认 GraphStore 不是银弹

**结论**：`search_symbols` 走 FTS5 trigram 是经过实测验证的设计决策，不是技术债。`DaemonClient.search_symbols` 仍尝试 `_remote_query("query.search")`（如果 daemon 已发布 snapshot 且远端实现了 SQL 路径），fallback 到本地 FTS5/Rust/LIKE。

## 扩展指南

### 添加新 Mixin

1. 创建 `db_<feature>.py`，定义 Mixin 类：

```python
# db_feature.py
class FeatureMixin:
    """新功能 Mixin"""

    def feature_method(self, param: str) -> dict:
        """功能方法"""
        # 通过 self.cursor 访问数据库
        self.cursor.execute("SELECT ...")
        return {"result": ...}
```

2. 在 `schema.py` 中添加新表（更新 `SCHEMA_SQL` 和 `SCHEMA_VERSION`）：

```python
CREATE TABLE IF NOT EXISTS feature_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ...
);
```

3. 在 `db_base.py` 的迁移逻辑中添加版本升级（如 v13 → v14）。

4. 在 `db.py` 的 `CodeGraphDB` 类继承列表中添加 `FeatureMixin`。

5. （可选）在 `server/mcp_server.py` 中注册 MCP 工具：

```python
@mcp.tool()
def feature_tool(param: str) -> dict:
    """新功能工具"""
    db = get_db()
    return db.feature_method(param)
```

6. （可选）在 `cli/main.py` 中添加 CLI 命令（子命令或 --flag）。

### 添加新 MCP 工具

在 `server/mcp_server.py` 的 `create_mcp_server()` 函数内添加：

```python
@mcp.tool()
def new_tool(param: str) -> dict:
    """工具描述（会暴露给 AI Agent）"""
    try:
        db = get_db()
        return db.some_method(param)
    except Exception as e:
        return {"error": str(e)}
```

> 建议所有工具用 `try/except` 包裹，返回 `{"error": str(e)}` 而非抛异常，避免 MCP 协议错误。

### 添加新 CLI 命令

**子命令风格**（适合复杂功能）：

1. 在 `cli/main.py` 的 `_SUBCOMMANDS` 集合添加命令名
2. 在 `_SUBCOMMAND_HELP` 添加帮助文本
3. 在 `_dispatch_subcommand` 添加调度分支
4. 实现 `_handle_<command>(args, db)` 函数

**--flag 风格**（适合简单查询）：

1. 在 `create_parser()` 中 `parser.add_argument("--my-flag", ...)`
2. 在 `main()` 中添加 `elif args.my_flag:` 分支

### 添加新语言解析器

1. 在 `parsers/` 创建 `<lang>_parser.py`，继承 `base.py`
2. 在 `config.py:LANGUAGE_CONFIG` 添加语言配置（扩展名、注释符、入口文件）
3. 在 `parsers/__init__.py` 注册解析器
4. 安装对应的 tree-sitter 语法包

## 命令风格统一规范（C8）

Call Warden 在 C8 系列改造中确立了 **"subcommand 为主，--flag deprecated 为辅"** 的长期命令风格方向。本节说明总体方向、12 主分类设计、迁移时间线与设计决策。

> 相关审计文档：`.cli_audit.md`（CLI/MCP 参数一致性审计）、`.mcp_audit.md`（MCP 工具命名审计）。
> 相关用户文档：[CLI 命令参考 - 命令概览（按 12 大功能分类）](cli_reference.md#命令概览按-12-大功能分类)、[MCP 工具参考 - 按 12 大功能分类](mcp_tools.md#按-12-大功能分类)。

### 总体方向

| 命令风格 | 状态 | 长期定位 |
|----------|------|----------|
| **subcommand**（`cw <subcommand> [options]`） | ✅ 推荐 | 长期支持，所有新功能必须以 subcommand 形式提供 |
| **--flag**（`cw --flag [options]`） | ⚠️ deprecated | 兼容期保留，使用时打印 `deprecated` 警告，将在未来版本移除 |

**原则**：
1. 新增 CLI 命令必须使用 subcommand 风格
2. 现有 `--flag` 不删除执行逻辑，仅添加 `deprecated` 警告
3. subcommand 与 `--flag` 在兼容期内并存，用户可渐进迁移
4. MCP 工具命名不重命名，仅审计与归档

### 12 主分类设计

Call Warden 把 145+ 个 CLI 命令和 206 个 MCP 工具按功能聚合为 12 个主分类，CLI 与 MCP 共用同一套分类体系。

| # | 主分类 | CLI 涵盖范围 | MCP 工具数 |
|---|--------|-------------|-----------|
| 1 | **Workspace & Database** | 工作区管理、数据库刷新、状态概览、watcher、分支感知 | 16 |
| 2 | **Query & Search** | 符号查询、搜索、文件读取、语义搜索、摘要、RAG、版本恢复 | 24 |
| 3 | **Call Chain Analysis** | 调用链、拓扑、循环、孤儿、模块图、热力图 | 12 |
| 4 | **Code Health & Metrics** | 复杂度、耦合、度量、健康检查、演化、热点、流失 | 12 |
| 5 | **Task Orchestration** | 任务创建/认领/上报/回滚/审批/关闭、capture-diff | 22 |
| 6 | **Agent Rule Memory** | 规则候选/审核/生效/同步/提取/清理/种子化 | 11 |
| 7 | **Audit & Bootstrap** | 审计链验证、密钥轮换、自举健康、检查门禁、安全护栏 | 10 |
| 8 | **Git Integration** | git 历史、commit、变更、blame、分支感知 | 5 |
| 9 | **Semgrep & Defects** | Semgrep 扫描、缺陷检测、缺陷知识库、漏洞爆炸半径 | 14 |
| 10 | **Coverage & Ownership** | 注释覆盖、测试覆盖、CODEOWNERS、所有权映射 | 15 |
| 11 | **GC** | 归档、恢复、清理、策略、备份、审计 | 11 |
| 12 | **Diagnostics** | doctor、安装集成、install-hook、clone 检测、LSP、跨仓库、安全编辑 | 21 |

> 详细子分类设计见 `.cli_audit.md` §3；MCP 工具分组明细见 `.mcp_audit.md` §4。

### 迁移时间线（三阶段）

#### 阶段 1：CLI subcommand 改造（✅ 已完成）

| 工作项 | 状态 | 说明 |
|--------|------|------|
| 新增 27 个 subcommand handler | ✅ | 覆盖 8 个主分类（workspace/db/query/call-chain/metrics/git/semgrep/coverage/diagnostics 部分） |
| 60 个 `--flag` deprecated 警告 | ✅ | 通过 `_DEPRECATED_FLAG_MAPPING` 字典实现，使用 `--flag` 时打印推荐 subcommand |
| `cw --help` 12 组分组 | ✅ | main help 按 12 主分类组织 subcommand 列表 |
| 子命令 `--help` 统一模板 | ✅ | 所有 subcommand 共享统一的 help 输出格式 |
| `--refresh` 多 path 支持 | ✅ | `cw --refresh <path1> <path2> ...` 支持同时刷新多文件 |
| readonly 命令集合扩展 | ✅ | `_READONLY_*_ACTIONS` 集合覆盖 workspace/git/semgrep/coverage 查询类 |

完整 flag → subcommand 映射见 [CLI 命令参考 - Deprecated --flag 清单](cli_reference.md#deprecated---flag-清单c8-step-2) 和 `deprecated_flag_mapping.json`。

#### 阶段 2：MCP 工具审计与文档（🔄 进行中）

| 工作项 | 状态 | 说明 |
|--------|------|------|
| MCP 工具命名审计 | ✅ | 206 个 `@mcp.tool()` 全量审计，结论：无严重不一致（详见 `.mcp_audit.md` §3） |
| 12 大类分组注释 | ✅ | `server/mcp_server.py` 中用统一注释格式标注每个分类起点 |
| CLI↔MCP 映射对照表 | ✅ | `docs/mcp_tools.md` 末尾添加 171 条 CLI↔MCP 命名映射 |
| CLI 命令参考 12 大类重构 | ✅ | `docs/cli_reference.md` 开头概览表替换为 12 大功能分类 |
| MCP 工具参考 12 大类重组 | ✅ | `docs/mcp_tools.md` 开头概览表替换为 12 大功能分类 |
| 架构规范文档化 | ✅ | 本节（architecture.md 命令风格统一规范） |

#### 阶段 3：深度完善（📋 计划中）

| 工作项 | 状态 | 说明 |
|--------|------|------|
| MCP 工具内部 i18n 改造 | 📋 | MCP 工具的错误消息/提示走 i18n.t()，与 CLI 输出一致 |
| 文档深度完善 | 📋 | 补充各 subcommand 的完整参数表、退出码、示例 |
| 测试覆盖加强 | 📋 | 为 27 个新增 subcommand handler 编写端到端测试 |
| `--flag` 移除评估 | 📋 | 评估 deprecated `--flag` 的使用率，制定移除时间表 |

### 设计决策

#### 1. 不修改现有 `--flag` 执行逻辑

**决策**：所有 `--flag` 命令保持原有执行逻辑，仅在入口处添加 `deprecated` 警告。

**理由**：
- 避免破坏向后兼容（已有脚本、CI 配置依赖 `--flag`）
- 降低迁移风险（用户可渐进切换到 subcommand）
- 减少回归测试范围（不触碰已有 `_handle_*` 函数）

**实现**：`cli/main.py` 中的 `_DEPRECATED_FLAG_MAPPING` 字典维护 flag → subcommand 映射，`--flag` 被触发时先打印 `[deprecated] --flag is deprecated, use 'cw <subcommand>' instead`，然后正常执行原逻辑。

#### 2. 不重命名 MCP 工具

**决策**：206 个 MCP 工具的命名保持不变，仅审计和归档。

**理由**：
- MCP 工具名是 Agent 集成的稳定接口，重命名会破坏已部署的 Agent workflow
- 现有命名前缀（`get_` / `list_` / `find_` / `task_` / `gc_` / `rule_` 等）基本符合约定
- 部分工具名（如 `blast_radius` / `who_to_ask` / `bootstrap_status`）使用业务领域术语而非动词前缀，但已成为稳定接口

**实现**：在 `server/mcp_server.py` 中添加 12 大类分组注释（`# === [N] <Category> ===`），不修改任何 `@mcp.tool()` 注册。详细审计结论见 `.mcp_audit.md` §5。

#### 3. subcommand 与 `--flag` 并存

**决策**：在兼容期内，subcommand 与 `--flag` 并存，`--flag` 将在未来版本移除。

**理由**：
- 给用户充分的迁移时间
- `--flag` 的 deprecated 警告会引导用户迁移
- 移除时间表将根据使用率数据制定（阶段 3 评估）

**兼容矩阵**：

| 命令类型 | MCP 未激活 | MCP 激活 |
|---------|-----------|----------|
| subcommand（推荐） | ✅ CLI 执行 | ✅ CLI 执行（写操作避免与 MCP 撞锁） |
| `--flag`（deprecated） | ✅ CLI 执行 + deprecated 警告 | ✅ CLI 执行 + deprecated 警告 |
| MCP 工具（只读） | ❌ 不可用 | ✅ MCP 执行（WAL 模式下与 CLI 写并发安全） |
| MCP 工具（写操作） | ❌ 不可用 | ⚠️ 5% 撞锁概率，建议用 CLI subcommand 替代 |

> 数据库锁策略详见 [AGENTS.md - 代码读取工具按场景分工](../AGENTS.md)。

## 下一步

- [MCP 工具参考](mcp_tools.md)：206 个工具详情
- [CLI 命令参考](cli_reference.md)：145+ 命令详情
- [部署指南](deployment.md)：Docker 部署与升级
