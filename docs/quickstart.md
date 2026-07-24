# 快速开始

本文档引导你从零开始使用 Call Warden：安装、初始化数据库、执行基本查询、启动 MCP Server，并演示一个完整的编辑会话。

## 1. 安装

### 1.1 一键安装（推荐）

Call Warden 提供级联安装脚本，自动安装核心依赖 + 全部已支持语言 grammar：

```bash
# 进入代码目录
cd /path/to/callwarden

# 一键安装：核心依赖 + 16 种语言 grammar + 可选依赖
cw install

# 包含可选依赖（semgrep / 向量搜索等）
cw install --all

# 仅检查依赖状态，不安装
cw install --check
```

安装脚本特性：
- **级联安装**：核心 → 已支持语言 → 扩展语言 → 可选依赖
- **失败不中断**：单个包失败只警告，继续安装其他包
- **幂等**：重复运行不会出错，已安装的包会跳过

### 1.2 按语言安装 grammar

如果只需特定语言支持，可指定语言名（避免下载全部 grammar）：

```bash
# 仅安装 C# 和 Ruby 的 grammar
cw install --lang csharp ruby

# 仅安装 Rust 和 Python
cw install --lang rust python
```

#### 支持的语言与对应包

| 语言 | pip 包名 | 文件扩展名 | 状态 |
|------|----------|-----------|------|
| Rust | `tree-sitter-rust` | `.rs` | 已支持 |
| TypeScript | `tree-sitter-typescript` | `.ts` / `.tsx` | 已支持 |
| JavaScript | `tree-sitter-javascript` | `.js` / `.jsx` | 已支持 |
| Python | `tree-sitter-python` | `.py` | 已支持 |
| Kotlin | `tree-sitter-kotlin` | `.kt` / `.kts` | 已支持 |
| Go | `tree-sitter-go` | `.go` | 已支持 |
| Java | `tree-sitter-java` | `.java` | 已支持 |
| C | `tree-sitter-c` | `.c` / `.h` | 已支持 |
| C++ | `tree-sitter-cpp` | `.cpp` / `.hpp` / `.cc` | 已支持 |
| **C#** | `tree-sitter-c-sharp` | `.cs` | **P0 扩展** |
| **Ruby** | `tree-sitter-ruby` | `.rb` | **P0 扩展** |
| **PHP** | `tree-sitter-php` | `.php` | **P1 扩展** |
| **Swift** | `tree-sitter-swift` | `.swift` | **P1 扩展** |
| **Scala** | `tree-sitter-scala` | `.scala` / `.sc` | **P2 扩展** |
| **Terraform/HCL** | `tree-sitter-hcl` | `.tf` / `.hcl` | **P2 扩展** |
| **Elixir** | `tree-sitter-elixir` | `.ex` / `.exs` | **P3 扩展** |

> 如果不安装某语言的 grammar 包，扫描该语言文件时会跳过并打印警告，不会崩溃。

### 1.3 可选依赖

可选依赖按需安装，未安装时相关功能会自动降级：

```bash
# 多语言静态安全扫描（守护者架构必需）
pip install semgrep

# 向量嵌入（语义搜索；当前实现为 BLOB + Rust/numpy 余弦相似度，sqlite-vec/vec0 虚拟表待落地）
pip install sentence-transformers

# 或通过一键脚本安装全部可选依赖
cw install --all
```

> `sqlite-vec` 当前未在生产代码中加载，安装不会带来额外收益；语义搜索使用 sentence-transformers + BLOB 存储 + Rust/numpy 余弦相似度（D1 状态）。

| 可选包 | 功能 | 未安装时行为 |
|--------|------|-------------|
| `semgrep` | 多语言静态安全扫描 | 安全扫描命令自动禁用并提示 |
| `sentence-transformers` | 向量嵌入（语义搜索） | 语义搜索降级为关键词匹配 |
| `numpy` | 向量计算加速 | 同上 |

### 1.4 获取代码

```bash
git clone https://github.com/nuoyazhizhou/callwarden.git
cd callwarden
```

Call Warden 通过 `cw.py` 入口脚本运行，使用 `cw` 命令调用所有功能。

### 1.5 验证安装

```bash
# 验证 CLI
cw --help

# 验证依赖状态
cw install --check
```

如果看到 CLI 帮助信息（含子命令概览和 --flag 列表），且 `--check` 输出全部 `[OK]`，说明安装成功。

### 1.6 配置 AI 工具集成

```bash
# 探测已安装的 AI 编码工具并自动配置 MCP 集成
cw setup

# 仅探测不写入（查看已安装哪些 AI 工具）
cw setup --dry-run
```

`cw setup` 会自动检测本机已安装的 AI 工具（Claude Code、Cursor、Trae 等 23 种），并将 Call Warden MCP Server 配置写入对应的配置文件。首次运行 `cw` 时也会自动触发（可通过 `--no-auto-setup` 禁用）。

## 2. 初始化数据库

### 2.1 构建代码图谱

进入你的项目目录，执行初始化命令：

```bash
cd /path/to/your/project
cw --refresh-all
```

- `--refresh-all`：增量刷新（仅解析有变化的文件，不会清空数据）
- `--refresh-all --force`：强制全量重新解析所有文件

构建过程会：
1. 自动检测项目根目录（查找 `.git`、`Cargo.toml`、`package.json`、`go.mod` 等标记）
2. 扫描所有支持语言的源文件（Rust/TS/JS/Python/Kotlin/Go/Java/C/C++/C#/Ruby/PHP/Swift/Scala/HCL/Elixir 共 16 种）
3. 用 tree-sitter 解析符号和调用关系
4. 写入项目级 SQLite 数据库

> 各语言的 tree-sitter grammar 包需提前安装，详见 [1.2 按语言安装 grammar](#12-按语言安装-grammar)。未安装 grammar 的语言会被跳过。

### 2.2 查看构建状态

```bash
cw --status
```

输出示例：

```
=== 代码图谱状态 ===
  工作区: my_project
  路径: /path/to/your/project
  数据库大小: 1.2 MB
  上次构建: 刚刚

  ── 文件 ──
    磁盘文件: 120  (已跟踪: 120)

  ── 符号 ──
    总符号数: 1850
    类型分布: 函数: 980, 方法: 320, 结构体: 150, ...

  ── 调用关系 ──
    总调用数: 4200
    已解析: 3800  (解析率: 90%)
```

### 2.3 工作目录结构

数据库为用户级单库，所有项目共用一个数据库，通过 `workspace_id` 逻辑隔离：

```
$HOME/.callwarden/callwarden.db
```

示例（Linux/macOS）：

```
/home/user/.callwarden/callwarden.db
```

- 一个用户一个数据库，所有项目共用
- 不同项目通过 `workspaces` 表的 `workspace_id` 字段在业务表中隔离（所有查询自动带 `WHERE workspace_id = ?` 过滤）
- 多个分支可通过分支感知功能注册为独立工作区
- ⚠ **禁止删除** `~/.callwarden/callwarden.db` 及其 `-wal`/`-shm` 文件

> 旧版按项目 hash 隔离的多库架构（`~/.callwarden/<16位hash>/callwarden.db`）已废弃，可通过 `cw gc db-migrate-single --apply` 迁移到用户级单库。

## 3. 基本查询

### 3.1 符号搜索

```bash
# 模糊搜索符号名
cw --search "login"

# 按类型过滤（fn/method/class/struct/enum/trait/interface）
cw --search "User" --search-kind class

# 限制返回数量
cw --search "handle" --search-limit 20
```

### 3.2 调用链分析

```bash
# 向上追踪：谁调用了我
cw --impact "module::function_name"

# 向下追踪：我调用了谁
cw --call-chain "module::function_name"

# 设置最大深度
cw --call-chain "module::function_name" --chain-depth 5
```

### 3.3 缺陷扫描

```bash
# Semgrep 快速扫描（只显示汇总）
cw --semgrep --semgrep-quick

# 详细扫描并存入数据库
cw --semgrep --semgrep-save

# 查看 Semgrep 统计
cw --semgrep-stats

# 查看缺陷列表
cw --semgrep-list
```

## 4. 启动 MCP Server

MCP（Model Context Protocol）Server 模式让 AI Agent 通过标准协议调用 229 个工具。

### 4.1 stdio 模式（默认）

```bash
cw server
```

或显式指定：

```bash
cw server --transport stdio
```

stdio 模式适用于 MCP client 直接启动并管理 Server 进程的场景（如 Claude Desktop、Trae IDE）。

### 4.2 SSE 模式

```bash
cw server --transport sse
```

SSE 模式适用于远程访问或多客户端共享同一个 Server 实例。

### 4.3 MCP Client 配置示例

在 MCP client 的配置文件中添加（以 Claude Desktop 为例）：

```json
{
  "mcpServers": {
    "callwarden": {
      "command": "python",
      "args": ["-m", "callwarden.server"],
      "env": {
        "CALLWARDEN_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

详细配置见 [部署指南](deployment.md#mcp-server-配置) 和 [MCP 工具参考](mcp_tools.md)。

## 5. 完整示例会话

下面演示一个从构建到编辑的完整工作流：查找函数 → 分析影响 → 安全编辑 → 回滚。

### 5.1 步骤 1：构建图谱

```bash
cd /home/user/my_project
cw --refresh-all
```

### 5.2 步骤 2：查找目标函数

```bash
cw --search "process_payment"
```

输出：

```
搜索结果: 'process_payment'（共 2 个，显示前 2 个）:

  [1] depth=  3 [✓] fn       my_project::payment::process_payment
         src/payment/mod.rs:45
         fn process_payment(order: &Order) -> Result<Receipt, Error>
  [2] depth=  4 [ ] fn       my_project::api::process_payment_handler
         src/api/payment.rs:12
```

### 5.3 步骤 3：查看符号详情

```bash
cw --symbol "my_project::payment::process_payment"
```

输出包含：类型、深度、文件位置、签名、注释、调用的函数、被谁调用。

### 5.4 步骤 4：分析变更影响

```bash
# 向上追踪所有调用者（影响半径）
cw --impact "my_project::payment::process_payment"
```

### 5.5 步骤 5：通过 MCP 执行安全编辑

启动 MCP Server 后，AI Agent 调用 `propose_edit` 工具：

```python
# Agent 调用示例（伪代码）
result = mcp.call_tool("propose_edit", {
    "file_path": "src/payment/mod.rs",
    "new_content": "<编辑后的完整文件内容>",
    "operation": "edit",
    "agent_task_id": "task-001",
    "dry_run": False
})
```

返回值：

```json
{
  "audit_id": 42,
  "file_path": "src/payment/mod.rs",
  "file_hash_before": "a1b2c3...",
  "file_hash_after": "d4e5f6...",
  "diff_summary": "+5 行 / -2 行",
  "status": "applied",
  "success": true
}
```

### 5.6 步骤 6：查看编辑历史

```bash
# CLI 查看编辑历史（通过 MCP 工具 get_edit_history）
# 或在 Agent 中调用 get_edit_history 工具
```

### 5.7 步骤 7：如需回滚

Agent 调用 `revert_edit` 工具，传入 `audit_id`：

```python
result = mcp.call_tool("revert_edit", {"audit_id": 42})
```

> 注意：审计表不存储完整文件内容，实际内容回滚需依赖 git checkout 或外部备份。

## 6. 下一步

- [CLI 命令参考](cli_reference.md)：了解全部 145+ CLI 命令
- [MCP 工具参考](mcp_tools.md)：了解全部 229 个 MCP 工具
- [架构设计](architecture.md)：理解数据库 Schema 和 Mixin 架构
- [部署指南](deployment.md)：Docker 部署、多容器共享、备份恢复
