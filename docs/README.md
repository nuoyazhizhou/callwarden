# Call Warden 用户文档

## 简介

Call Warden 是一个面向 AI Agent 的代码知识图谱工具。它通过 tree-sitter 解析多语言代码库，将符号、调用关系、文件版本、Git 历史、缺陷模式等信息结构化存储到 SQLite 数据库中，为 AI Agent 提供符号搜索、调用链分析、变更影响半径、安全编辑审计、Semgrep 集成、LSP 集成和跨仓库分析等能力，解决 Agent 在大型代码库中"找不到符号、看不懂依赖、改了不知道影响谁"的核心问题。

## 核心能力

| 能力 | 说明 |
|------|------|
| 符号图谱 | 解析 16 种语言（Rust/TypeScript/JavaScript/Python/Kotlin/Go/Java/C/C++/C#/Ruby/PHP/Swift/Scala/HCL/Elixir）的函数、类、结构体等符号，构建调用关系图 |
| 调用链分析 | 向上/向下追踪调用链，检测循环调用，计算拓扑排序与调用深度 |
| Semgrep 集成 | 多语言静态安全扫描，结果按内容去重入库，支持按严重度/语言/规则过滤 |
| 安全编辑（propose_edit） | Agent 编辑文件前 SHA-256 校验、原子写入、审计日志、可回滚 |
| LSP 集成 | 通过 LSP 协议获取 hover/定义/引用/诊断/补全，补充 tree-sitter 静态分析 |
| 跨仓库分析 | 检测跨仓库依赖、查找共享符号、分析跨仓库影响传播 |
| 向量搜索 | 基于 sqlite-vec 的语义搜索，支持自然语言查找函数 |
| 注释恢复 | 从历史版本恢复丢失的函数注释，支持批量预览与写入 |
| 安全护栏 | DB/API/Incident 三类可阻断规则，编辑前阻断式检查 |
| 任务驱动编排 | 任务/步骤/审计日志的状态机，护栏阻断后自动插入修复步骤 |
| 代码度量 | 圈复杂度、耦合度、扇入扇出、调用深度、健康评分 |
| Git 集成 | 导入 commit 历史、符号级变更追踪、文件变更详情 |
| 分支感知 | 独立工作区方案，分支注册/差异对比/切换/合并预览 |

## 文档索引

| 文档 | 说明 |
|------|------|
| [快速开始](quickstart.md) | 安装、初始化、基本查询、MCP Server 启动、完整示例会话 |
| [CLI 命令参考](cli_reference.md) | 全部 CLI 子命令与 --flag 命令的用法、参数、示例 |
| [MCP 工具参考](mcp_tools.md) | 120 个 MCP 工具按功能分组、关键参数、返回值格式、配置方法 |
| [架构设计](architecture.md) | 整体架构、数据库 Schema、Mixin 多继承、安全机制、扩展指南 |
| [部署指南](deployment.md) | 本地部署、Docker 部署、MCP 配置、备份恢复、升级指南 |
| [Dockerfile](Dockerfile) | Docker 镜像构建文件 |

## 快速开始（3 步）

```bash
# 1. 一键安装依赖（核心 + 16 种语言 grammar + 可选依赖）
cd /path/to/callwarden
cw install            # 默认安装
# cw install --all    # 含 semgrep / 向量搜索等可选依赖

# 2. 初始化数据库（构建代码图谱）
cd /path/to/your/project
cw --init

# 3. 查询符号
cw --search "login"
cw --call-chain "module::function_name"
```

详细流程见 [快速开始](quickstart.md)。

## 系统要求

| 依赖 | 版本 | 说明 |
|------|------|------|
| Python | 3.10+ | 必需 |
| tree-sitter | 最新 | 必需，多语言解析引擎 |
| fastmcp | 最新 | 必需（MCP Server 模式） |
| Semgrep | 可选 | 缺陷扫描，未安装时相关命令自动降级 |
| LSP 服务器 | 可选 | 支持 pyright/tsserver/gopls/rust-analyzer |
| sentence-transformers | 可选 | 向量嵌入，未安装时语义搜索自动回退到关键词匹配 |
| sqlite-vec | 可选 | 向量索引扩展 |
| Git | 2.20+ | 可选，Git 历史集成功能需要 |

## 工作目录

数据库按项目隔离，路径格式：

```
$HOME/.callwarden/<16位hash>/callwarden.db
```

其中 16 位 hash 是项目根路径绝对路径的 SHA-256 前 16 位，确保不同项目的数据库互不干扰。详见 [架构设计](architecture.md#数据库架构)。

## 许可证

详见项目根目录 LICENSE 文件。
