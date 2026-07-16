# Call Warden

> 面向 AI Agent 的代码知识图谱工具 · 基于 tree-sitter + SQLite + MCP

Call Warden 通过 tree-sitter 解析多语言代码库，将符号、调用关系、文件版本、Git 历史、缺陷模式、变更影响等信息结构化存储到 SQLite，为 AI Agent 提供符号搜索、调用链分析、变更影响半径、安全编辑审计、Semgrep 集成等能力，解决 Agent 在大型代码库中"找不到符号、看不懂依赖、改了不知道影响谁"的核心问题。

## 核心能力

- **16 种语言解析**：Rust / TypeScript / JavaScript / Python / Kotlin / Go / Java / C / C++ / C# / Ruby / PHP / Swift / Scala / HCL / Elixir（100% 覆盖 Semgrep GA）
- **调用链分析**：四级解析策略 + 向上/向下 BFS 分层 + 跨文件标记 + 循环检测
- **版本历史**：content_hash 去重 + 删除标记 + 注释恢复（防止 git checkout 丢失注释）
- **生产安全护栏**：DB/API/Incident 三类可阻断规则 + Before-Edit Contract
- **变更影响智能**：blast_radius + cross_layer_impact（代码 → DB → API → 配置）
- **代码演化智能**：函数变更频率 + 缺陷关联 + 热点排名 + churn 分析
- **缺陷知识库**：从 Semgrep + git 修复中挖掘模式，推荐修复方案
- **任务驱动编排**：task/step/audit 状态机，**父子任务树**支持大任务自动拆分，深度优先遍历 + 子任务完成自动推进父任务，护栏阻断后自动插入修复步骤。完整状态机 `open → in_progress → review → applied → closed`，最后一个子任务 apply 时**原子级联 close**（兄弟 + 父任务），父任务禁止手动 apply/close，必须由其他会话 LLM 审核执行
- **Agent 集成闭环**：`work_next_job` 返回下一步最小上下文，`propose_symbol_patch` / `propose_range_patch` 支持手术刀式局部编辑，`install-agent` 生成 Codex/Claude/Cursor 集成模板
- **文件操作工具组**：file_read / file_grep / file_list / file_symbol_content，Agent 完全通过 MCP 读取代码，无需 IDE 内置工具
- **向量搜索 + RAG**：sqlite-vec + sentence-transformers，自然语言查找函数
- **Semgrep 集成**：多语言静态安全扫描，结果按内容去重入库
- **LSP 集成**：hover / 定义 / 引用 / 诊断 / 补全
- **跨仓库分析**：依赖检测 + 共享符号 + 影响传播
- **Git 集成**：commit 历史 + 符号级变更追踪
- **分支感知**：独立工作区方案 + 差异对比 + 合并预览
- **Java GC 机制**：.gitignore/.callwardenignore 解析 + 归档/复活/清除
- **204+ MCP 工具 + 145+ CLI 命令**

## 快速开始

```bash
# 1. 一键安装依赖（核心 + 16 种语言 grammar + 可选依赖）
cw install            # 默认安装
# cw install --all    # 含 semgrep / 向量搜索等可选依赖

# 2. 初始化数据库（构建代码图谱）
cd /path/to/your/project
cw --refresh-all

# 3. 查询符号
cw --search "login"
cw --call-chain "module::function_name"

# 4. 生成 Agent 集成模板（MCP + Skill/Rules + Hooks）
cw install-agent all
```

详细流程见 [快速开始](docs/quickstart.md)。

## 典型场景

### 场景一：大任务拆分与追踪（父子任务）

当任务过大时，拆分为子任务避免 Agent 遗漏或遗忘上下文：

```python
# 1. 创建大任务
task_id = mcp.task_create(
    title="代码质量全面改进",
    steps=[{"action": "verify", "check_items": ["最终验证"]}]
)

# 2. 拆分为多个子任务
mcp.task_split(task_id, subtasks=[
    {"title": "修复 parser 调用关系", "steps": [...]},
    {"title": "i18n 国际化改造", "steps": [...]},
    {"title": "默认语言自动检测", "steps": [...]},
])

# 3. 正常领取步骤，系统自动深度优先下钻
step = mcp.task_next_step(task_id)
# → 自动返回第一个子任务的第一个步骤
# → 附带 parent_task_chain 祖先链，明确上下文

# 4. 子任务全完成后自动推进父任务
mcp.task_report_step(task_id, step["step_id"], result="...", success=True)

# 5. 随时查看任务树进度
tree = mcp.task_status_tree(task_id)
# → 每层显示 progress: {total, done, progress百分比}
```

### 场景二：Agent 通过 MCP 读取代码（不依赖 IDE 内置工具）

```python
# 读取文件内容
content = mcp.file_read("db/db_tasks.py", offset=0, limit=100)

# 搜索代码（支持正则 + glob 过滤）
results = mcp.file_grep("task_create", glob="*.py", output_mode="content")

# 浏览目录
files = mcp.file_list("db/", glob="*.py")

# 读取函数源码（结合数据库位置信息）
sym = mcp.file_symbol_content("db_tasks.py", "task_next_step")
```

## 文档导航

| 文档                                                                                   | 说明                                    |
| -------------------------------------------------------------------------------------- | --------------------------------------- |
| [docs/README.md](docs/README.md)                                                       | 用户文档总入口                          |
| [docs/quickstart.md](docs/quickstart.md)                                               | 安装、初始化、基本查询、MCP Server 启动 |
| [docs/cli_reference.md](docs/cli_reference.md)                                         | 全部 CLI 子命令与 --flag 用法           |
| [docs/mcp_tools.md](docs/mcp_tools.md)                                                 | 204+ MCP 工具按功能分组                 |
| [docs/architecture.md](docs/architecture.md)                                           | 整体架构、Schema、Mixin 设计、扩展指南  |
| [docs/deployment.md](docs/deployment.md)                                               | 本地/Docker 部署、MCP 配置、备份恢复    |
| [docs/design/implementation-status.md](docs/design/implementation-status.md)           | 当前实现状态权威盘点（v15）             |
| [docs/design/competition-analysis.md](docs/design/competition-analysis.md)             | 竞品分析与独占优势                      |
| [docs/design/evolve-guardian-architecture/](docs/design/evolve-guardian-architecture/) | Guardian 架构设计规格                   |
| [docs/history/](docs/history/)                                                         | 历史归档文档（已过时，仅供回顾）        |

## 系统要求

| 依赖                  | 版本  | 说明                                       |
| --------------------- | ----- | ------------------------------------------ |
| Python                | 3.10+ | 必需                                       |
| tree-sitter           | 最新  | 必需，多语言解析引擎                       |
| fastmcp               | 最新  | 必需（MCP Server 模式）                    |
| Semgrep               | 可选  | 缺陷扫描，未安装时自动降级                 |
| LSP 服务器            | 可选  | pyright / tsserver / gopls / rust-analyzer |
| sentence-transformers | 可选  | 向量嵌入，未安装时降级关键词搜索           |
| sqlite-vec            | 可选  | 向量索引扩展                               |
| Git                   | 2.20+ | 可选，Git 历史集成需要                     |

## 数据库位置

按项目隔离，路径格式：

```
$HOME/.callwarden/<16位hash>/callwarden.db
```

16 位 hash 是项目根路径绝对路径的 SHA-256 前 16 位，确保不同项目互不干扰。

## 工作目录结构

```
callwarden/
├── README.md                  # 本文件（项目入口）
├── LICENSE                    # MIT
├── CONTRIBUTING.md            # 贡献指南
├── CHANGELOG.md               # 版本演化
├── cw.py                      # 统一命令行入口（cw 命令）
├── config.py                  # 配置：路径常量、多语言配置
├── install.py                 # 一键级联安装器
├── requirements.txt           # 依赖清单
├── .callwardenignore.example   # 忽略规则模板
├── analyzers/                 # 分析层（call_chain / coverage / issues / ignore_spec）
├── cicd/                      # CI/CD 集成（sarif / incremental / pr_check）
├── cli/                       # CLI 命令行
├── db/                        # 数据库层（23 个 Mixin + schema）
├── docs/                      # 文档
├── i18n/                      # 国际化
├── parsers/                   # 多语言解析器（16 种）
├── rust_ext/                  # PyO3 Rust 扩展（性能加速）
├── server/                    # MCP Server + 文件监控
└── tests/                     # 测试套件
```

## 许可证

[MIT](LICENSE)

## 贡献

欢迎提交 Issue 和 PR。请先阅读 [CONTRIBUTING.md](CONTRIBUTING.md)。
