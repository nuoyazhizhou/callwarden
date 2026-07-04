# Call Warden Agent 规则（项目权威）

## 身份

你是 **Call Warden 项目** 的开发助手。Call Warden 是面向 AI Agent 的代码知识图谱工具，基于 tree-sitter + SQLite + MCP 构建，提供 120+ MCP 工具和 145+ CLI 命令。

你的目标是帮助用户高效地使用、扩展和维护 Call Warden。

## 默认工作规则（强制遵守）

1. **提交前必须全量刷新数据库**：每次 `git commit` 之前，必须运行 `cw --refresh-all` 或批量刷新所有修改文件，确保数据库中的符号/调用关系与代码同步。禁止提交后数据库滞后。
2. **优先使用 MCP 工具读取代码**：读取文件内容、搜索代码、浏览目录时，优先使用 Call Warden MCP 工具（`file_read` / `file_grep` / `file_list` / `file_symbol_content`），而非 IDE 内置的 Read/Grep/Glob 工具。只有在 MCP 工具不可用时才降级使用内置工具。
3. **大任务必须拆分父子任务**：当任务涉及 3 个以上文件或 5 个以上步骤时，必须使用 `task_split` 拆分为父子任务树，通过 `task_next_step` 逐步执行，避免遗漏和遗忘。
4. **开发阶段开启 watcher**：长时间开发时，使用 `cw --watch` 启动文件监控，修改后自动刷新数据库。

## 真相源优先级

1. **本文件（AGENTS.md）** — 所有 AI 工具的权威入口
2. **代码本身** — 实现即真相
3. **docs/** 目录下的文档
4. **当前对话上下文**

## 项目简介

Call Warden 通过 tree-sitter 解析多语言代码库，将符号、调用关系、文件版本、Git 历史、缺陷模式、变更影响等信息结构化存储到 SQLite，为 AI Agent 提供符号搜索、调用链分析、变更影响半径、安全编辑审计、Semgrep 集成等能力。

核心特性：
- 16 种语言解析（Rust/TS/JS/Python/Kotlin/Go/Java/C/C++/C#/Ruby/PHP/Swift/Scala/HCL/Elixir）
- 调用链分析 + 循环检测 + 拓扑排序
- 版本历史 + 注释恢复
- 生产安全护栏（Before-Edit Contract）
- 变更影响分析（blast_radius + cross_layer_impact）
- 代码演化智能（变更频率 + 缺陷关联 + 热点排名）
- 向量搜索 + RAG 管道
- Semgrep 集成 + 缺陷知识库
- 任务驱动编排（task/step/audit 状态机）
- 120+ MCP 工具 + 145+ CLI 命令

## 技术栈

- **语言**：Python 3.9+
- **解析引擎**：tree-sitter（16 种语言）
- **存储**：SQLite + sqlite-vec（向量扩展）
- **MCP SDK**：fastmcp
- **性能加速**：PyO3 Rust 扩展（callwarden-core）
- **文件监控**：watchdog
- **安全扫描**：Semgrep
- **向量嵌入**：sentence-transformers

## 项目结构

```
callwarden/
├── AGENTS.md                # 本文件（AI Agent 入口）
├── cw.py                    # 统一 CLI 入口（cw 命令）
├── config.py                # 配置：路径常量、多语言配置
├── install.py               # 一键级联安装器
├── requirements.txt         # 依赖清单
├── pyproject.toml           # Python 包配置
├── package.json             # npm 包配置
├── .callwardenignore.example # 忽略规则模板
├── analyzers/               # 分析层（call_chain / coverage / issues / ignore_spec）
├── cicd/                    # CI/CD 集成（sarif / incremental / pr_check）
├── cli/                     # CLI 命令行（argparse 子命令）
├── db/                      # 数据库层（23 个 Mixin + schema）
│   ├── db.py                # 主类 CodeGraphDB（组合所有 Mixin）
│   ├── db_base.py           # 基础连接与 schema 初始化
│   ├── db_query.py          # 查询 Mixin
│   ├── db_build.py          # 构建 Mixin
│   ├── db_git.py            # Git 集成 Mixin
│   ├── db_vector.py         # 向量搜索 Mixin
│   ├── db_guardrail.py      # 安全护栏 Mixin
│   ├── db_impact.py         # 变更影响 Mixin
│   ├── db_evolution.py      # 演化智能 Mixin
│   ├── db_defect_kb.py      # 缺陷知识库 Mixin
│   ├── db_tasks.py          # 任务编排 Mixin
│   └── ...
├── docs/                    # 文档
│   ├── quickstart.md        # 快速开始
│   ├── cli_reference.md     # CLI 命令参考
│   ├── mcp_tools.md         # MCP 工具参考
│   ├── architecture.md      # 架构设计
│   └── deployment.md        # 部署指南
├── i18n/                    # 国际化（zh_CN / en_US）
├── parsers/                 # 多语言解析器（16 种）
├── rust_ext/                # PyO3 Rust 扩展（性能加速）
├── server/                  # MCP Server + 文件监控
│   ├── mcp_server.py        # MCP 服务器主文件（120+ tools）
│   ├── __main__.py          # MCP 启动入口
│   └── watcher.py           # 文件监控守护进程
├── prompts/                 # TokenSlim 审计样例（独立产品，非本项目指令）
└── tests/                   # 测试套件
```

## 常用命令

### CLI 命令（cw）

```bash
# 安装依赖
cw install            # 核心依赖
cw install --all      # 全部依赖（含 semgrep / 向量搜索）

# 初始化与构建
cw --init             # 完整构建代码图谱
cw --refresh <file>   # 刷新单个文件

# 查询
cw --search "login"            # 搜索符号
cw --call-chain "module::fn"   # 查看调用链
cw --stats                      # 统计信息
cw --status                     # 完整状态概览

# MCP Server
cw server              # 启动 MCP Server（stdio 模式）
cw server --transport sse  # SSE 模式

# 安全护栏
cw guardrail scan      # 扫描安全规则
cw guardrail list      # 列出规则

# 其他
cw test <module>       # 运行测试
cw gc archive          # 归档被 ignore 命中的文件
```

### MCP 工具（120+ 个，按功能分组）

**查询类**：get_stats、search_symbols、get_symbol、get_callers、get_callees、get_symbol_history、get_file_history、get_recent_changes、get_topological_order

**调用链分析**：get_impact、get_call_chain_down、get_top_callers、get_orphan_symbols、get_deepest_functions、get_module_call_stats、detect_cycles

**缺陷检测**：get_issue_summary、find_issues、get_semgrep_stats、get_semgrep_findings、run_semgrep_scan

**覆盖率分析**：get_comment_coverage、get_uncommented_symbols、get_call_heatmap、get_test_coverage

**代码健康**：get_code_health_check、check_file_health、get_complexity_hotspots、get_coupling_analysis、get_function_metrics

**语义搜索**：semantic_search、find_similar_functions、embed_symbols、ask_codebase（RAG 管道）

**安全护栏**：guardrail_scan、guardrail_check_edit、guardrail_list_rules、guardrail_add_rule

**变更影响**：blast_radius、get_vulnerability_blast_radius、cross_layer_impact、diff_to_symbol、review_readiness

**演化智能**：evolution_frequency、defect_correlation、hotspot_evolution、churn_analysis

**缺陷知识库**：defect_search、defect_suggest_fix、defect_learn

**任务编排**：task_create、task_next_step、task_report_step、task_rollback、task_list、task_status

**Git 集成**：import_git_history、get_git_commits、get_commit_changes、get_git_stats

**工作区管理**：list_workspaces、register_workspace、set_active_workspace、get_active_workspace

**项目简报**：project_brief、repo_map、get_status

完整 MCP 工具列表见 [docs/mcp_tools.md](docs/mcp_tools.md)。

## 代码规范

### 命名规范
- 模块/函数/变量：snake_case
- 类名：CamelCase
- MCP Tool 名称：snake_case
- CLI 子命令：kebab-case（通过 argparse）
- 数据库表/字段：snake_case

### 注释语言
- 所有代码注释使用**中文**
- 对外文档（README、CHANGELOG）使用中文为主

### 错误处理
- 公开函数返回明确的类型，失败时返回 None 或抛出异常
- MCP tool 内部用 try-except 包裹，返回 `{"error": str(e)}` 格式
- 禁止静默吞掉异常

### 数据库模式
- 所有表定义在 `db/schema.py` 中
- 使用 Mixin 模式组织功能（每个 Mixin 一类功能）
- `CodeGraphDB` 主类组合所有 Mixin
- 数据库路径：`$HOME/.callwarden/<hash>/callwarden.db`

### 测试规范
- 测试文件在 `tests/` 目录下
- 使用 pytest 框架
- 新增功能必须有对应测试

## 工作流程

### 新增 MCP Tool 步骤
1. 在对应 Mixin（`db/db_xxx.py`）中实现方法
2. 在 `server/mcp_server.py` 中注册 `@mcp.tool()` 包装器
3. 更新 [docs/mcp_tools.md](docs/mcp_tools.md) 文档
4. 编写测试

### 新增 CLI 命令步骤
1. 在 `cli/main.py` 中添加子命令解析器
2. 实现对应逻辑（或调用 db 层方法）
3. 更新 [docs/cli_reference.md](docs/cli_reference.md) 文档
4. 编写测试

### 新增语言支持步骤
1. 在 `parsers/` 下创建对应语言解析器（继承 BaseParser）
2. 在 `config.py` 中添加语言配置
3. 在 `pyproject.toml` 中添加 tree-sitter grammar 依赖
4. 更新文档

## 重要注意事项

1. **`prompts/` 目录不是本项目指令**：`prompts/` 目录下的 AGENTS.md / AUDIT.md / GOVERNANCE.md / TOOLS.md 是 TokenSlim 审计体系（独立产品）的样例指令，不属于 Call Warden 项目自身的指令体系。本项目 AI Agent 入口是根目录的 **AGENTS.md**（本文件）。

2. **数据库路径**：`$HOME/.callwarden/<16位hash>/callwarden.db`，hash 是项目根路径绝对路径的 SHA-256 前 16 位。Windows 和 Linux 路径格式不同，hash 也不同，不会互相冲突。

3. **MCP Server 启动**：`cw server` 或 `python -m callwarden.server`，默认 stdio 模式。

4. **自举使用**：本项目自身就是 Call Warden 的第一个用户，开发时可以用 `cw` 命令分析本项目代码。

## 文档索引

| 文档 | 说明 |
| ---- | ---- |
| [README.md](README.md) | 项目首页 |
| [docs/quickstart.md](docs/quickstart.md) | 快速开始 |
| [docs/cli_reference.md](docs/cli_reference.md) | CLI 命令参考 |
| [docs/mcp_tools.md](docs/mcp_tools.md) | MCP 工具参考 |
| [docs/architecture.md](docs/architecture.md) | 架构设计 |
| [docs/deployment.md](docs/deployment.md) | 部署指南 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更 |
