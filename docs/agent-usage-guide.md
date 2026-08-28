# Call Warden 使用指南（面向 AI Agent）

> 本文档从项目根 [AGENTS.md](../AGENTS.md) 抽取面向**使用者**的内容。
> AGENTS.md 保留项目开发规范（给 Call Warden 本身的贡献者），本文档面向
> **使用 Call Warden 的 AI Agent 和开发者**。
>
> 如果你的 AI Agent 读取 AGENTS.md（如 Claude Code / Gemini CLI / Trae IDE），
> 可在项目根 AGENTS.md 顶部加一行链接指向本文档，避免内部开发规范干扰使用。

## Call Warden 是什么

Call Warden 是面向 AI Agent 的代码知识图谱工具，基于 tree-sitter + SQLite + MCP 构建，
提供 237 个 MCP 工具和 145+ CLI 命令。

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
- 237 MCP 工具 + 145+ CLI 命令

## 技术栈

- **语言**：Python 3.9+
- **解析引擎**：tree-sitter（16 种语言）
- **存储**：SQLite（向量嵌入以 BLOB 存储 + Rust/numpy 余弦相似度）
- **MCP SDK**：fastmcp
- **性能加速**：PyO3 Rust 扩展（callwarden-core）
- **文件监控**：watchdog
- **安全扫描**：Semgrep
- **向量嵌入**：sentence-transformers

## 代码读取工具按场景分工

> **核心原则**：符号级查询（callers/callees/call-chain/impact/symbol）必须用 cw，
> Grep 做不到或做不好。读文件全文/浏览目录/编辑文件可用 IDE 内置工具。

| 操作类型 | MCP 未激活（开发期）| MCP 激活后 |
|---------|-------------------|-----------|
| 任务编排（task create/next/report）| **CLI** `cw task ...` | **CLI**（保持，写操作避免与 MCP 长连接撞锁）|
| 刷新数据库（refresh/refresh-all）| **CLI** `cw --refresh ...` | **CLI**（保持，写操作）|
| 读文件内容 / 搜索代码 / 浏览目录 | **CLI** `cw --file <PATH>` / `cw --search <Q>`；IDE 内置 Read/Grep/Glob 作为降级 | **MCP** `file_read` / `file_grep` / `file_list`（只读）|
| 符号内容 / 符号查询 | **CLI** `cw symbol <QN>` / `cw callers` / `cw callees` | **MCP** `file_symbol_content` / `get_symbol` / `get_callers` / `get_callees`（只读）|
| 符号静态检查 | **CLI** `cw issues <QN>` | **MCP** `get_symbol_issues`（只读）|
| 符号测试 case | **CLI** `cw tests <QN>` | **MCP** `get_test_cases` / `get_tested_functions` / `get_test_coverage_summary`（只读）|
| 变更-缺陷关联 | **CLI** `cw evolution <QN> --defects` | **MCP** `get_defect_correlation`（只读）|
| 带符号上下文的文本搜索 | **CLI** `cw grep <pattern...>` | **CLI 保持**（依赖 rg 二进制 + `find_symbols_at_lines` 组合）|
| 规则匹配查询 | **CLI** `cw rule applicable` | **MCP** `get_applicable_rules`（只读）|

**背景**：MCP Server 是 stdio 长连接，与 CLI 新进程并发时会触发 SQLite `database is locked`。
已通过 `PRAGMA journal_mode=WAL` + `busy_timeout=5000` 缓解，但**写操作仍有 5% 撞锁概率**，
故写操作永久走 CLI；只读操作在 MCP 激活后走 MCP。

**MCP 激活状态判断**：会话开始时若无法调用 `file_grep` 等 MCP 工具，则视为 MCP 未激活，全部走 CLI。

## MCP Server 启动

```bash
cw server              # 或
python -m callwarden.server   # 默认 stdio 模式
```

## 数据库路径

`$HOME/.callwarden/callwarden.db`（用户级单库架构）。一个用户一个数据库，所有项目共用，
通过 `workspace_id` 逻辑隔离。相同文件跨项目只解析一次（Global CAS 共享）。

## 开发阶段开启 watcher

长时间开发时，使用 `cw --watch` 启动文件监控，修改后自动刷新数据库。

## 任务驱动编排

Call Warden 提供任务/步骤/审计状态机，用于编排复杂工作：

1. **创建任务**：`cw task create --title "..." --desc "..."` 或 Python API `task_create(parent_id=...)`
2. **推进步骤**：`cw task next <task_id>` 获取下一步
3. **报告步骤**：`cw task report <step_id> --status done --summary "..."`
4. **大任务拆分**：涉及 3+ 文件或 5+ 步骤时，用 `cw task split` 拆为父子任务树

### 任务 reopen 机制

任务状态机支持 `review`/`applied`/`closed` → `in_progress` 的回退（reopen）：

- **自动触发**：向已 closed 的父任务挂入新子任务时，检查兄弟子任务状态决定是否 reopen
- **手动触发**：`cw task reopen <task_id> --reason "..."` 直接 reopen 整条祖先链

## 自举使用

Call Warden 自身就是第一个用户，开发时可以用 `cw` 命令分析本项目代码。

## P0 盲评对照实验

Call Warden 内置 P0 盲评对照实验，用于评估"盲评审查是否值得产品化"。
实验不修改数据库 schema，复用现有任务状态机，所有记录标记为**非产品 Evidence**。

### 典型工作流

```bash
# 1. 创建并锁定批次
cw experiment batch-create --seed 42 --min-valid 30 --min-nontrivial 20

# 2. 对符合条件的任务纳样（自动分组 + 构建 blind view）
cw experiment admit <task_id> <batch_id> --strata "python,high,large"

# 3. 审查完成后记录指标
cw experiment record-metrics <task_id> <batch_id> --tp 2 --fp 0 --misses 0 --duration 120

# 4. 记录 verdict 变更（reveal 前后）
cw experiment record-verdict <task_id> <batch_id> --changed no --reason-code no_change

# 5. 查看 G0 决策
cw experiment report <batch_id> --json
```

### 关键语义

- **G0 判定**：`eligible_for_p1=true` 表示可以讨论是否启用 P1，不代表 P1 已实现
- **灰区**：误报率差 10–20pp 或延迟增 25–50% 时进入灰区观察，不暂停但不授权 P1
- **暂停**：6 种硬阈值触发器（详见 [CLI 参考](cli_reference.md#暂停与恢复req-12211224)），暂停后保留全部记录，规则变更需新建批次
- **非产品 Evidence**：实验记录不得用于 P1 hard-gate 声明或替代确定性验证

### 阶段可用性

P0/D0/P1/P2/P3 均已实现并可用（详见 [CLI 参考 · 阶段可用性](cli_reference.md#阶段可用性req-131)）。
P4（安全租约与分派）当前为 planned / unavailable，正式启用前不得在输出或判定中
暗示其已实现（Req 13.1）。

**P3 身份审计要点**：
- `cw task report/apply/close/reopen` 支持 `--agent-id/--session-id/--model-id/--role`
  结构化 Identity 输入；`task apply` 强制 Reviewer Session 与 Implementer Session 分离
- Identity 仅作 actor attribution，不等于 assignment/lease/ownership（Req 10.5, 10.7）
- `cw identity revoke --revocation-mode compromised|rotated` 追加撤销账本；
  Attestation 只能由 daemon 签发，客户端自签不作为授权证明（Req 14.13）

详细命令参数见 [CLI 参考 · cw experiment](cli_reference.md#cw-experimentp0-盲评对照实验)。

## 工具参考

- [TOOLS.md](../TOOLS.md) — 工具使用指南（CLI/MCP/场景映射）
- [docs/mcp_tools.md](mcp_tools.md) — MCP 工具参考
- [docs/cli_reference.md](cli_reference.md) — CLI 命令参考
- [docs/architecture.md](architecture.md) — 架构设计
