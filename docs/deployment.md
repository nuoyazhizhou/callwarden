# 部署指南

本文档介绍 Call Warden 的本地部署、Docker 部署、MCP Server 配置、环境变量、数据库备份恢复和升级流程。

## 本地部署（开发环境）

### 1. 一键安装依赖（推荐）

Call Warden 提供级联安装脚本，自动安装核心依赖 + 全部已支持语言 grammar + 可选依赖：

```bash
cd /path/to/callwarden

# 默认安装：核心依赖 + 9 种已支持语言 + C# / Ruby 扩展语言
cw install

# 包含可选依赖（semgrep / 向量搜索等）
cw install --all

# 仅检查依赖状态
cw install --check

# 仅安装指定语言的 grammar
cw install --lang csharp ruby
```

详见 [快速开始 - 安装](quickstart.md#1-安装)。

#### 手动安装（不使用一键脚本）

如需手动安装，最小依赖为：

```bash
pip install tree-sitter tree-sitter-languages fastmcp
```

各语言 grammar 按需安装：

```bash
# 已支持语言（9 种）
pip install tree-sitter-rust tree-sitter-typescript tree-sitter-python \
            tree-sitter-kotlin tree-sitter-go tree-sitter-java \
            tree-sitter-c tree-sitter-cpp tree-sitter-javascript

# P0 扩展语言（C# / Ruby）
pip install tree-sitter-c-sharp tree-sitter-ruby

# P1 扩展语言（PHP / Swift）
pip install tree-sitter-php tree-sitter-swift

# P2 扩展语言（Scala / Terraform/HCL）
pip install tree-sitter-scala tree-sitter-hcl

# P3 扩展语言（Elixir）
pip install tree-sitter-elixir

# 可选依赖
pip install semgrep                  # 多语言静态安全扫描
pip install sentence-transformers    # 向量嵌入（语义搜索）
pip install sqlite-vec               # 向量索引扩展
```

> 未安装某语言 grammar 时，扫描该语言文件会跳过并打印警告，不会崩溃。

### 2. 获取代码

```bash
git clone https://github.com/nuoyazhizhou/callwarden.git
cd callwarden
```

Call Warden 通过 `cw.py` 入口脚本运行，使用 `cw` 命令调用所有功能。

### 3. 验证安装

```bash
# 验证 CLI
cw --help

# 验证 MCP Server（应能启动并等待 stdio 输入）
cw server --help

# 验证依赖状态（全部应为 [OK]）
cw install --check
```

### 4. 设置别名（可选）

`cw` 命令通过 `cw.py` 入口脚本运行。建议设置别名以便全局使用：

```bash
# Linux/macOS（写入 ~/.bashrc 或 ~/.zshrc）
alias cw="python /path/to/callwarden/cw.py"

# Windows PowerShell（写入 $PROFILE）
Set-Alias -Name cw -Value "python C:\path\to\callwarden\cw.py"
```

### 5. 构建代码图谱

```bash
cd /path/to/your/project
cw --init
```

数据库将创建在 `$HOME/.code_graph/<16位hash>/code_graph.db`。

### 6. （可选）编译 Rust 扩展

如需 PyO3 性能加速：

```bash
cd rust_ext
pip install maturin
maturin develop --release
```

未编译时自动回退到 Python 实现，功能不受影响。

## Docker 部署

### 1. 构建镜像

Dockerfile 见 [Dockerfile](Dockerfile)。

```bash
cd callwarden
docker build -t call-warden:latest -f docs/Dockerfile .
```

### 2. 运行容器（CLI 模式）

```bash
# 挂载项目目录和 $HOME/.code_graph（数据库持久化）
docker run --rm \
  -v /path/to/your/project:/workspace \
  -v $HOME/.code_graph:/root/.code_graph \
  -w /workspace \
  code-graph:latest --init
```

### 3. 运行容器（MCP Server 模式）

```bash
docker run --rm \
  -v /path/to/your/project:/workspace \
  -v $HOME/.code_graph:/root/.code_graph \
  -w /workspace \
  -e CODE_GRAPH_WORKSPACE=/workspace \
  code-graph:latest server
```

### 4. 多容器共享部署

Call Warden 支持多容器共享同一个数据库：

```bash
# 宿主机安装一次（或用容器）
# 数据库放在 $HOME/.code_graph/（所有容器共享挂载）
# 每个容器配置 MCP client 指向同一个数据库路径

# 容器 A：构建图谱
docker run --rm \
  -v /path/to/project-a:/workspace \
  -v $HOME/.code_graph:/root/.code_graph \
  code-graph:latest --init

# 容器 B：查询（共享数据库）
docker run --rm \
  -v /path/to/project-a:/workspace \
  -v $HOME/.code_graph:/root/.code_graph \
  code-graph:latest --search "login"
```

> SQLite WAL 模式支持多读者单写者，写入自动排队，多进程安全。

## MCP Server 配置

### stdio 模式（默认，推荐）

适用于 MCP client 直接启动并管理 Server 进程。

**Claude Desktop**（`~/Library/Application Support/Claude/claude_desktop_config.json`）：

```json
{
  "mcpServers": {
    "code-graph": {
      "command": "python",
      "args": ["-m", "code_graph.server"],
      "env": {
        "CODE_GRAPH_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

**Trae IDE**（`.trae/mcp.json` 或项目设置）：

```json
{
  "mcpServers": {
    "code-graph": {
      "command": "python",
      "args": ["-m", "code_graph.server"],
      "cwd": "/path/to/callwarden",
      "env": {
        "CODE_GRAPH_WORKSPACE": "/path/to/your/project"
      }
    }
  }
}
```

**使用 Docker 容器作为 Server**：

```json
{
  "mcpServers": {
    "code-graph": {
      "command": "docker",
      "args": [
        "run", "--rm",
        "-v", "/path/to/your/project:/workspace",
        "-v", "/path/to/.code_graph:/root/.code_graph",
        "-w", "/workspace",
        "code-graph:latest", "server"
      ]
    }
  }
}
```

### SSE 模式

适用于远程访问或多客户端共享。

```bash
# 启动 SSE Server
cw server --transport sse
```

Client 配置指向 `http://<host>:<port>/sse`。

### 工作区切换

MCP Server 启动后，可通过工具切换工作区：

```python
# 通过 MCP 工具切换
mcp.call_tool("set_active_workspace", {"workspace_id_or_name": "my_project"})
```

或启动时通过环境变量 `CODE_GRAPH_WORKSPACE` 指定默认工作区。

## 环境变量

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `CODE_GRAPH_WORKSPACE` | 默认工作区根路径 | 自动检测当前目录 |
| `CODE_GRAPH_DB_PATH` | 自定义数据库路径 | `$HOME/.code_graph/<hash>/code_graph.db` |
| `HOME` | 用户主目录（决定 `.code_graph` 位置） | 系统默认 |

## 数据库备份与恢复

### 备份

SQLite 数据库是单文件，备份简单：

```bash
# 方法 1：直接复制（确保没有写入操作进行中）
cp $HOME/.code_graph/<hash>/code_graph.db backup_$(date +%Y%m%d).db

# 方法 2：使用 .backup 命令（在线备份，推荐）
sqlite3 $HOME/.code_graph/<hash>/code_graph.db ".backup backup_$(date +%Y%m%d).db"

# 方法 3：备份整个 .code_graph 目录
tar -czf code_graph_backup_$(date +%Y%m%d).tar.gz -C $HOME .code_graph/
```

### 恢复

```bash
# 停止所有 Call Warden 进程
# 恢复数据库
cp backup_20260101.db $HOME/.code_graph/<hash>/code_graph.db
```

### 自动备份（可选）

```bash
# 添加到 crontab，每天凌晨 3 点备份
0 3 * * * sqlite3 $HOME/.code_graph/*/code_graph.db ".backup '/backups/cg_$(date +\%Y\%m\%d).db'"
```

## 升级指南

### Schema 迁移自动执行

Call Warden 的 Schema 迁移在启动时自动执行：

```bash
# 升级代码后，首次运行会自动检测版本并迁移
git pull
cw --status    # 自动迁移并显示状态
```

迁移逻辑在 `db_base.py` 中：
1. 读取当前 Schema 版本（从 `schema_version` 表）
2. 对比 `SCHEMA_VERSION` 常量
3. 逐版本执行 `ALTER TABLE` / `CREATE TABLE`
4. 更新版本号

> 迁移是增量且幂等的，可安全重复执行。建议升级前备份。

### 升级步骤

```bash
# 1. 备份数据库
sqlite3 $HOME/.code_graph/<hash>/code_graph.db ".backup '/tmp/cg_backup.db'"

# 2. 拉取新代码
cd callwarden
git pull

# 3. 更新依赖（重新运行一键安装脚本）
cw install

# 4. （可选）重新编译 Rust 扩展
cd rust_ext && maturin develop --release && cd -

# 5. 触发 Schema 迁移
cw --status

# 6. （可选）增量更新图谱
cw --init
```

### Docker 升级

```bash
# 1. 备份
docker run --rm \
  -v $HOME/.code_graph:/root/.code_graph \
  code-graph:latest \
  sh -c "sqlite3 /root/.code_graph/*/code_graph.db '.backup /root/.code_graph/backup.db'"

# 2. 拉取新镜像
docker pull code-graph:latest

# 3. 运行（自动迁移）
docker run --rm \
  -v /path/to/project:/workspace \
  -v $HOME/.code_graph:/root/.code_graph \
  code-graph:latest --status
```

## 故障排查

### 数据库锁定

```bash
# 查看锁状态
sqlite3 $HOME/.code_graph/<hash>/code_graph.db "PRAGMA journal_mode;"

# WAL 模式下不应有锁定问题，如遇到：
# 1. 确保没有多个写入进程
# 2. 删除 -wal 和 -shm 文件（停止所有进程后）
rm $HOME/.code_graph/<hash>/code_graph.db-wal
rm $HOME/.code_graph/<hash>/code_graph.db-shm
```

### 数据库损坏

```bash
# 尝试修复
sqlite3 $HOME/.code_graph/<hash>/code_graph.db ".recover" > recovered.sql
sqlite3 new.db < recovered.sql

# 或从备份恢复
cp backup.db $HOME/.code_graph/<hash>/code_graph.db
```

### Semgrep 不可用

```bash
# 检查安装
semgrep --version

# 如未安装
pip install semgrep
```

未安装时，Semgrep 相关命令会自动降级并提示。

### LSP 服务器不可用

```bash
# 通过 MCP 工具检查
mcp.call_tool("lsp_check_available", {"language": "python"})

# 安装对应 LSP 服务器
pip install pyright              # Python
npm install -g typescript        # TypeScript
go install golang.org/x/tools/gopls@latest  # Go
rustup component add rust-analyzer  # Rust
```

### 向量嵌入不可用

```bash
# 检查依赖
pip install sentence-transformers sqlite-vec

# 重新生成嵌入
cw --embed-force
```

向量服务不可用时，语义搜索自动回退到关键词匹配。

## 下一步

- [快速开始](quickstart.md)：从零开始使用
- [CLI 命令参考](cli_reference.md)：全部命令
- [架构设计](architecture.md)：理解 Schema 和 Mixin
- [Dockerfile](Dockerfile)：Docker 镜像构建文件
