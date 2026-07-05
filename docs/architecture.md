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
│  子命令 + --flag 双风格  │     │   120 个 @mcp.tool() 工具    │
│  145+ 命令               │     │   stdio / SSE 传输           │
└────────────┬─────────────┘     └──────────────┬───────────────┘
             │                                  │
             └──────────────┬───────────────────┘
                            ▼
┌───────────────────────────────────────────────────────────────┐
│                  CodeGraphDB (db.py)                          │
│         23 个 Mixin 多继承组装的统一数据库类                  │
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
│              SQLite 数据库（每个项目一个）                    │
│   $HOME/.callwarden/<16位hash>/callwarden.db                  │
│   Schema v13 / WAL 模式 / 30+ 表 / 23 个 Mixin 模块           │
└───────────────────────────────────────────────────────────────┘
```

### 分层说明

| 层 | 职责 | 关键文件 |
|----|------|----------|
| 接入层 | CLI 命令解析、MCP 协议处理 | `cli/main.py`、`server/mcp_server.py` |
| 业务层 | 23 个 Mixin 组合的数据库类 | `db.py` + `db_*.py`（23 个文件） |
| 解析层 | tree-sitter 多语言解析、调用关系提取 | `parsers/`（10 个文件） |
| 分析层 | 调用链、覆盖率、缺陷检测 | `analyzers/`（3 个文件） |
| 加速层 | PyO3 Rust 扩展（可选） | `rust_ext/` |
| 存储层 | SQLite 项目级数据库 | `schema.py` |

## 数据库架构

### 项目级隔离

每个项目使用独立的 SQLite 数据库，路径格式：

```
$HOME/.callwarden/<16位hash>/callwarden.db
```

- 16 位 hash = 项目根路径绝对路径的 SHA-256 前 16 位
- 不同项目互不干扰，体积小、查询快
- 实现见 `config.py:get_project_db_path()`

### Schema 版本

当前 Schema 版本：**v13**

```
v4  Git 集成表（git_commits / git_file_changes / git_symbol_changes）
v5  向量嵌入表（symbol_embeddings，sqlite-vec）
v6  符号摘要表（symbol_summaries，AI 摘要版本化）
v7  任务管理表（tasks / task_steps / change_audit）
v8  文件所有权表（file_ownership）
v9  覆盖率数据表（coverage_data，LCOV/Cobertura 行级覆盖率）
v10 守护者架构表（guardrail_rules/findings + change_impacts + evolution_metrics + defect_patterns/fixes）
v11 Token 节省账本表（token_savings_ledger）
v12 安全文件编辑审计表（file_edit_audit，propose_edit 流水线）
v13 跨仓库分析表（cross_repo_deps）
```

Schema 迁移在 `db_base.py` 中自动执行（启动时检测版本并增量 ALTER TABLE）。

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

### 任务与编辑审计表（v7 + v12）

| 表 | 说明 |
|----|------|
| tasks | 任务（open/in_progress/review/applied/closed/reverted） |
| task_steps | 任务步骤（pending/in_progress/done/failed/blocked） |
| change_audit | 变更审计日志（hash + diff） |
| file_edit_audit | propose_edit 审计流水线（pending/applied/reverted/failed） |

### Agent Rule Memory 表（v23）

| 表 | 说明 |
|----|------|
| agent_rule_candidates | 候选规则（pending/accepted/rejected），由 Agent 观察或从 task_quality_findings 自动提取 |
| agent_rules | 已生效规则（active/deprecated/removed），accept 后写入，按 scope 匹配注入到上下文 |
| agent_rule_sync_log | AGENTS.md 同步日志（dry_run/apply 都记录，含 before/after hash） |

## Mixin 架构

### 设计原理

CodeGraphDB 通过 **24 个 Mixin 多继承**组装，每个 Mixin 负责一个功能领域。这种设计：

- **单一职责**：每个 Mixin 只关心自己的表和查询
- **按需组合**：主类只需声明继承即可获得功能
- **易于扩展**：新增功能只需添加新 Mixin
- **避免上帝类**：`db.py` 仅 92 行，职责在 24 个文件中分散

### 24 个 Mixin 列表

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

### 组装方式

`db.py` 的核心实现：

```python
class CodeGraphDB(
    CodeGraphBase,
    BuildMixin,
    QueryMixin,
    CommentMixin,
    # ... 共 24 个 Mixin
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

## 性能优化

### 1. PyO3 加速（rust_ext/）

可选的 Rust 扩展模块，通过 PyO3 暴露给 Python：
- 高频路径（如 hash 计算、路径规范化）用 Rust 实现
- release 构建零开销
- 未编译时自动回退到 Python 实现

构建：`cd rust_ext && maturin develop --release`

### 2. 向量索引（sqlite-vec）

语义搜索使用 sqlite-vec 扩展：
- 768 维向量（jina-v2-base-code 模型）
- 余弦相似度查询
- 嵌入结果缓存在 `symbol_embeddings` 表
- 向量服务不可用时自动回退到关键词匹配

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

## 下一步

- [MCP 工具参考](mcp_tools.md)：120 个工具详情
- [CLI 命令参考](cli_reference.md)：145+ 命令详情
- [部署指南](deployment.md)：Docker 部署与升级
