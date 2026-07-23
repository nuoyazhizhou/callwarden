# Call Warden Agent 规则（项目权威）

## 身份

你是 **Call Warden 项目** 的开发助手。Call Warden 是面向 AI Agent 的代码知识图谱工具，基于 tree-sitter + SQLite + MCP 构建，提供 206+ MCP 工具和 145+ CLI 命令。

你的目标是帮助用户高效地使用、扩展和维护 Call Warden。

## 默认工作规则（强制遵守）

1. **提交前必须全量刷新数据库**：每次 `git commit` 之前，必须运行 `cw --refresh-all` 或批量刷新所有修改文件，确保数据库中的符号/调用关系与代码同步。禁止提交后数据库滞后。
2. **代码读取工具按场景分工**（避免 SQLite 跨进程锁冲突）：

   | 操作类型 | 当前（MCP 未激活/开发期）| MCP 激活后 |
   |---------|------------------------|-----------|
   | 任务编排（task create/next/report/rollback）| **CLI** `cw task ...` | **CLI**（保持，写操作避免与 MCP 长连接撞锁）|
   | 刷新数据库（refresh/refresh-all）| **CLI** `cw --refresh ...` | **CLI**（保持，写操作）|
   | 读文件内容 / 搜索代码 / 浏览目录 | **CLI** `cw --file <PATH>` / `cw --search <Q>` / `cw --query <NAME> <FILE>`；IDE 内置 Read/Grep/Glob 作为降级 | **MCP** `file_read` / `file_grep` / `file_list`（只读，WAL 模式下与 CLI 写并发安全）|
   | 符号内容 / 符号查询 | **CLI** `cw symbol <QN>` / `cw callers` / `cw callees` | **MCP** `file_symbol_content` / `get_symbol` / `get_callers` / `get_callees`（只读）|
   | 符号静态检查 | **CLI** `cw issues <QN>`（整合 Semgrep + Guardrail findings，按符号聚合）| **MCP** `get_symbol_issues`（只读）|
   | 符号测试 case | **CLI** `cw tests <QN>`（`--build` 重建关联 / `--import` 导入 JUnit XML 为写操作走 CLI）；`--history` 查稳定性 | **MCP** `get_test_cases` / `get_tested_functions` / `get_test_coverage_summary` / `get_test_stability`（只读，WAL 安全）|
   | 变更-缺陷关联 | **CLI** `cw evolution <QN> --defects` | **MCP** `get_defect_correlation`（只读）|
   | 带符号上下文的文本搜索 | **CLI** `cw grep <pattern...> [--fixed] [--limit N] [--include-all]`（默认过滤无符号行，多关键词空格分隔为 AND）| **CLI 保持**（依赖 rg 二进制 + `find_symbols_at_lines` 组合，非纯 db 查询；通用文本搜索用 MCP `file_grep`）|
   | 规则匹配查询（get_applicable_rules）| **CLI** `cw rule applicable` | **MCP** `get_applicable_rules`（只读）|

   **背景**：MCP Server 是 stdio 长连接，与 CLI 新进程并发时会触发 SQLite `database is locked`。已通过 `PRAGMA journal_mode=WAL` + `busy_timeout=5000` 缓解，但**写操作仍有 5% 撞锁概率**，故写操作永久走 CLI；只读操作在 MCP 激活后走 MCP（吃狗粮），未激活时走 CLI。

   **MCP 激活状态判断**：会话开始时若无法调用 `file_grep` 等 MCP 工具，则视为 MCP 未激活，全部走 CLI。MCP 激活由用户手工配置，不在 AGENTS.md 中自动判断。
3. **任何任务必须在 cw 数据库创建任务记录**（强制）：无论大小任务，开始前必须用 `cw task create` 或 `cw task split` 在数据库创建对应任务。可以创建独立父任务，也可以挂载到已存在的父任务下（通过 Python API `task_create(parent_id=...)`）。禁止"无任务记录就开始编码"。

   **子任务挂载方式**（重要）：
   - **CLI `cw task create` 当前不支持 `--parent` 参数**（只有 `--title`/`--desc`/`--steps`）
   - 需要挂载子任务到父任务时，用 Python 脚本调用 `CodeGraphDB.task_create(title=..., description=..., parent_id=..., steps=[])`
   - 脚本模板见 [docs/task_create_subtask.py](docs/task_create_subtask.py)
   - 或用 `cw task split --plan plan.md <parent_task_id>` 从 Markdown 计划拆分子任务

4. **大任务必须拆分父子任务**：当任务涉及 3 个以上文件或 5 个以上步骤时，必须使用 `task_split` 拆分为父子任务树，通过 `task_next_step` 逐步执行，避免遗漏和遗忘。
5. **开发阶段开启 watcher**：长时间开发时，使用 `cw --watch` 启动文件监控，修改后自动刷新数据库。
6. **读不锁，写才锁**（CLI 锁优化原则）：所有只读命令（查询/搜索/统计/分析类）不得触发数据库写操作，只有写命令（refresh/task next/report/apply/close/rule sync 等）才允许持有写锁。

   - **只读命令跳过 workspace 激活**：CLI 启动时默认会执行 `register_workspace` + `set_active_workspace`（UPDATE workspaces 写操作）。只读命令通过 `_is_readonly_command()`（子命令模式）或 `_is_readonly_args()`（flag 模式）识别后跳过此写操作，避免被 MCP Server 写锁卡住。
   - **set_active_workspace 内部短路**：即使写命令进入此方法，若目标 workspace 已是 active，直接返回不写（`is_active == 1` 短路）。
   - **busy_timeout=5000**：写命令遇到锁时最多等 5 秒（非 30 秒），超时后抛 `sqlite3.OperationalError`，由上层捕获并打印 `errors.db_locked` 友好提示（"数据库正忙，请几秒后重试"），exit code 2。
   - **只读/写命令分类**：详见 [TOOLS.md](TOOLS.md) 的"只读/写命令分类"小节。

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
- 206+ MCP 工具 + 145+ CLI 命令

## 技术栈

- **语言**：Python 3.9+
- **解析引擎**：tree-sitter（16 种语言）
- **存储**：SQLite（向量嵌入以 BLOB 存储 + Rust/numpy 余弦相似度，sqlite-vec vec0 虚拟表待落地）
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
├── db/                      # 数据库层（35 个功能 Mixin + 1 基类，40 个 db_*.py 文件 + schema）
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
│   ├── mcp_server.py        # MCP 服务器主文件（206+ tools）
│   ├── __main__.py          # MCP 启动入口
│   └── watcher.py           # 文件监控守护进程
├── prompts/                 # TokenSlim 审计样例（独立产品，非本项目指令）
└── tests/                   # 测试套件
```

## 工具参考

CLI 命令速查、cw task 子命令参数、MCP 工具分组、只读/写命令分类、场景→命令映射，详见 [TOOLS.md](TOOLS.md)。

**核心原则**：符号级查询（callers/callees/call-chain/impact/symbol）必须用 cw，Grep 做不到或做不好。读文件全文/浏览目录/编辑文件可用 IDE 内置工具。

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
- 数据库路径：`$HOME/.callwarden/callwarden.db`（用户级单库，多 workspace 通过 `workspace_id` 逻辑隔离）

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

### 任务 reopen 机制

任务状态机支持 `review`/`applied`/`closed` → `in_progress` 的回退（reopen），用于
code review 发现已 applied/closed 的任务有问题需要修复，或向已 closed 的父任务
挂入新子任务。

**两种触发方式**：

1. **自动触发**（`task_create(parent_id=closed_task)`）：
   向已 `closed`/`applied`/`review` 的父任务挂入新子任务时，**检查兄弟子任务状态**
   决定是否 reopen 父任务：
   - 所有兄弟子任务都是 `closed`（或无兄弟子任务）→ reopen 父任务为 `in_progress`
   - 有兄弟子任务非 `closed`（如 `open`/`in_progress`）→ 直接挂，**不 reopen** 父任务
   - 父任务 `open`/`in_progress` 时直接挂，不改状态
   - 父任务被 reopen 后，递归向上 reopen 祖父任务链（无条件，不检查兄弟）

2. **手动触发**（`cw task reopen <task_id> [--reviewer <S>] [--reason "..."]`）：
   用户主动 reopen 一个任务，**不检查兄弟子任务状态**，直接 reopen 整条祖先链。

**设计原则**：挂新子任务时需要同时考虑父任务状态和兄弟子任务状态。若父任务已
`closed` 但还有 `open` 的兄弟子任务，直接挂新子任务即可；若所有兄弟都已 `closed`，
说明之前的工作完成，应 reopen 父任务。手动 reopen 是用户明确操作，直接 reopen
整条链。

详细设计见 [docs/architecture.md §8. 任务 reopen 机制](docs/architecture.md#8-任务-reopen-机制t-1783413215675-3aae)。

## 重要注意事项

1. **`prompts/` 目录不是本项目指令**：`prompts/` 目录下的 AGENTS.md / AUDIT.md / GOVERNANCE.md / TOOLS.md 是 TokenSlim 审计体系（独立产品）的样例指令，不属于 Call Warden 项目自身的指令体系。本项目 AI Agent 入口是根目录的 **AGENTS.md**（本文件）。

2. **数据库路径**：`$HOME/.callwarden/callwarden.db`（用户级单库架构）。一个用户一个数据库，所有项目共用，通过 `workspaces` 表的 `workspace_id` 字段在所有业务表中逻辑隔离（所有查询自动带 `WHERE workspace_id = ?` 过滤）。相同文件跨项目只解析一次（Global CAS 共享）。**禁止删除 `~/.callwarden/callwarden.db` 及其 `-shm`、`-wal` 文件**，其中包含任务编排数据、符号图谱、调用链等不可恢复的工作成果。如遇 DB 锁定或 WAL 状态异常，应排查进程持有锁或 WAL checkpoint 时序问题，不得通过删除 DB 文件解决。

   **旧版多库迁移**：旧版按项目 hash 隔离的数据库（`~/.callwarden/<16位hash>/callwarden.db`）可通过 `cw gc db-migrate-single --apply` 迁移到用户级单库（迁移 workspaces/tasks/task_steps 表，符号图谱数据建议迁移后运行 `cw refresh --all` 重建）。迁移后旧 `<hash>/` 目录保留作备份，用户确认后可手动删除。

3. **MCP Server 启动**：`cw server` 或 `python -m callwarden.server`，默认 stdio 模式。

4. **自举使用**：本项目自身就是 Call Warden 的第一个用户，开发时可以用 `cw` 命令分析本项目代码。

5. **PowerShell Heredoc 不可用**：在 Windows PowerShell 环境中，`git commit -m "$(cat <<'EOF' ... EOF)"` 等 heredoc 语法不工作（报 "Missing file specification after redirection operator"）。多行 commit message 应使用多个 `-m` 参数：`git commit -m "标题" -m "正文行1" -m "正文行2"`。或用 `git commit -F 文件路径` 从文件读取。

6. **工具调用错误日志与 AGENTS.md 持续改进（强制执行）**：

   **机制**：每次工具调用报错时，将错误摘要追加到 `.trae-cn/memory/tool_errors.log`（格式：`时间 | 工具名 | 错误类型 | 错误摘要 | 是否已记录到 AGENTS.md`）。当同一类错误（按错误类型+根因归类）累积出现 **2 次**时，必须执行以下闭环：
   - **分析共同点**：对比 2 次以上同类错误的上下文，提取共同根因（如"PowerShell 不支持 heredoc"、"沙箱拦截 -shm 文件创建"）
   - **写入 AGENTS.md**：在"重要注意事项"小节新增一条规则，包含：根因说明 + 规避方法 + 具体替代命令
   - **标记已记录**：在日志中将该类错误标记为"已记录到 AGENTS.md"，后续不再重复写入

   **触发阈值**：同类错误出现 2 次即触发（不是等到 3 次或更多）。第 1 次记录到日志，第 2 次分析并写入 AGENTS.md。

   **已沉淀的常见错误**（持续维护）：
   - PowerShell heredoc `$(cat <<'EOF')` 不工作 → 用多个 `-m` 或 `-F 文件`（见第 5 条）
   - SQLite WAL 模式下 `immutable=1` 只读连接读到旧数据 → 加载前 `PRAGMA wal_checkpoint(PASSIVE)`（见第 7 条）
   - rusqlite `SQLITE_OPEN_NOMUTEX` 常量名错误 → 正确为 `SQLITE_OPEN_NO_MUTEX`
   - Windows `.pyd` 文件锁定导致 `pip install` 失败 → 解压 wheel 到 `target/pyinstall` + `PYTHONPATH`
   - PowerShell 中复杂 `rg` alternation/括号转义易产生 `regex parse error: unclosed group` → 拆成多个简单 `-e` 模式或改用 `Select-String`（见第 10 条）
   - `rg` 无匹配会返回 exit code 1，直接放入 `Promise.all` 会让整组检索失败 → 在每个并行分支捕获非零返回，或在 PowerShell 命令末尾把无匹配转换为成功（见第 11 条）

7. **SQLite WAL 模式与只读连接**：GraphStore 用 `immutable=1` URI 打开 SQLite（跳过 WAL），因此新建数据库的 schema 和数据可能还在 WAL 中未被 checkpoint。`_get_graph_store()` 加载前必须先执行 `PRAGMA wal_checkpoint(PASSIVE)`，否则会读到旧数据（报 "no such table"）。同理，任何用 `immutable=1` 或只读模式打开 SQLite 的场景，都需确保写入方已 checkpoint。

8. **Python vs Rust SQL 驱动效率**：Python sqlite3 和 Rust rusqlite 底层都是同一个 C SQLite 库，纯 SQL 执行效率几乎相同。差异来自数据转换层：
   - **单行/少量行查询**（如 SELECT COUNT）：Python sqlite3 更快（PyO3 跨语言固定开销 ~1μs 占比大）
   - **批量查询**（100+ 行，如 get_callers/get_callees/search_symbols）：Python 调用 Rust 仍快 ~2.5x（行数据转换是大头，Rust 闭包 ~0.1μs/行 vs Python dict ~0.5μs/行，PyO3 固定开销被摊薄）
   - **图遍历**（get_callers 等）：用 Rust 内存索引（CSR HashMap），完全跳过 SQL，5x 加速

   B-P7b 设计原则：单值查询（get_stats）保持 Python SQL；多行查询（get_callers/get_callees/search_symbols）走 Rust 短路。

9. **PowerShell 下避免单条复杂 `rg` 正则**：PowerShell 双引号、反斜杠和括号混用时，复杂 alternation 容易在传给 `rg` 前破坏转义，报 `regex parse error: unclosed group`。多个关键词应使用独立的简单模式，例如 `rg -n -e "parse_file_lang" -e "MP_THRESHOLD" db tests`；包含大量括号或引号时改用 `Get-Content ... | Select-String -Pattern 'pattern1|pattern2'`，不要把代码片段转义塞进一个巨大正则。

10. **并行调用必须容忍预期的非零退出**：`rg` 未找到内容、探测可选模块不存在等预期情况会返回非零，这不是执行故障；若把这类命令直接放进 `Promise.all`，一个分支会中止整组调用并丢失其他结果。并行脚本应在每个分支捕获结果，或在 PowerShell 中把预期的非零状态显式转换为成功，例如 `rg -n -e "pattern" path; if ($LASTEXITCODE -eq 1) { exit 0 }`。无法方便转换时单独执行该探测；只有非预期的非零状态才按真正错误处理。

11. **后台 watcher 用长运行 exec cell 承载**：在桌面工具执行器中，`Start-Process` 启动的后代进程可能被持续跟踪，即使重定向标准输出仍会使父工具调用超时。启动 `cw --watch` 时直接运行长命令并保留返回的 cell id，开发结束后显式终止该 cell；不要用 `Start-Process` 脱离。

12. **`cw task create` 不支持 `--parent` 参数**：CLI 的 `cw task create` 只有 `--title`/`--desc`/`--steps` 三个参数。挂载子任务必须用 Python API `db.task_create(title=..., description=..., parent_id=..., steps=[])`，模板见 [docs/task_create_subtask.py](docs/task_create_subtask.py)。参数清单见 [TOOLS.md](TOOLS.md)，不确定时先 `cw task <subcommand> --help`。

13. **合成数据压测 ≠ 真实 E2E**（方法论教训）：用 `generate_data()` 一次性生成全部合成数据到内存再批量入库，会挤压系统页缓存、未覆盖解析/CAS/watcher/daemon、多规模并行互相干扰。正确做法：流式生成、串行运行取中位数、分开报告 storage_build_time 和 end_to_end_time、记录硬件型号。不要发展"内存主表+SQLite 从表"架构，Call Warden 走混合架构：SQLite/CAS 持久化真相，Rust GraphStore/CSR 内存查询，daemon 共享发布。

14. **PowerShell 不展开传给 `rg` 的路径通配符**：在 Windows PowerShell 中，`rg pattern tests/test_*.py` 或 `rg pattern *.ps1` 会把通配符原样交给 `rg`，随后报 `os error 123`。文件类型筛选必须使用 `rg -g`，例如 `rg -n -g "test_*.py" pattern tests`；多个后缀使用多个 `-g`。不要把 `*` 放在传给 `rg` 的路径参数中。

15. **读取测试文件前先确认真实路径**：不要根据功能名连续猜测 `tests/test_xxx.py`。先运行 `rg --files tests | rg "关键词"` 或 `rg -l -g "test_*.py" "符号名" tests`，再对实际返回的路径使用 `Get-Content` / `cw file`。缺失的候选文件不是检索失败，不应让并行读取整组中止。

16. **`cargo fmt` 不能限定单文件范围**：`cargo fmt --check -- src/graph.rs` 仍会扫描整个 crate，可能被任务之外的既有未格式化文件阻断。只格式化当前修改文件时使用 `rustfmt --edition 2021 rust_ext/src/graph.rs`，随后用 `cargo check --manifest-path rust_ext/Cargo.toml` 验证；不要运行会机械改写整个 crate 的 `cargo fmt`。

17. **Rust 懒批对象必须在服务边界物化**：`CallersBatch` / `SymbolSearchBatch` 等 PyO3 懒批对象用于降低 Rust→Python 转换开销，但 MCP、daemon service 和公开 Python API 若声明返回 `List[...]`，必须在边界执行 `list(result)`。不要把自定义懒批对象直接交给 JSON 序列化或依赖 list 契约的调用方；内部 db 查询短路可继续保留懒批。

18. **连续修改同一文件前刷新补丁上下文**：前一个 `apply_patch` 可能已删除、移动或格式化后续补丁依赖的锚点，继续使用旧上下文会触发 `PatchContextMismatch`。对同一文件分阶段修改时，先用 `rg -n` 或读取目标局部确认当前内容，再生成小范围补丁；不要复用前一轮读取到的 import 或函数上下文。

19. **WSL 验收先检查隔离测试依赖**：精简 Ubuntu/WSL 镜像可能同时缺少 `pytest` 和 `python3-venv`，直接创建 venv 会因无 `ensurepip` 失败。运行 Linux 专属验收前先检查 `python3 -m pip --version`、`python3 -m venv --help` 和 `import pytest`；缺少 venv 支持时先安装匹配版本的 `python3-venv`，再在 `/tmp` 创建临时环境，禁止把 Linux wheel 或测试依赖装进 Windows Python 环境。

20. **PowerShell 调 WSL 时避免嵌套代码字符串**：`PowerShell -> wsl -> bash -lc -> python -c/cargo --config` 的三层引号很容易被提前展开或截断。WSL 验收应把构建、文件准备和 Python 测试拆成独立的简单命令；复杂 Python 逻辑放入仓库已有测试文件，由 `python3 -m pytest` 调用，不要在 `bash -lc` 尾部拼接带引号和括号的 `python -c`。

21. **跨平台路径断言先输出模块来源和实际值**：Windows 对 Linux 风格绝对路径的 `os.path.abspath/join/normpath` 行为可能加入盘符或反斜杠，且 `PYTHONPATH` 可能命中不同安装副本。配置探测连续失败时，先输出 `module.__file__`、原始环境变量和实际配置值，再基于目标平台语义断言；不要连续猜测字符串规范化结果。

22. **代码变更必须同步更新文档（文档同步规则）**：当代码变更涉及以下"关键指标"时，**必须在同一次 commit 中同步更新相关文档**，禁止"代码改了文档没改"：

    **关键指标清单（变更时必须同步文档）**：
    - MCP 工具数量（新增/删除 `@mcp.tool()` 装饰器时）→ 更新 [docs/mcp_tools.md](docs/mcp_tools.md) 头部 + [README.md](README.md) + [docs/design/implementation-status.md](docs/design/implementation-status.md)
    - Schema 版本号（[db/schema.py](db/schema.py) 的 `SCHEMA_VERSION` 变更时）→ 更新 [docs/architecture.md](docs/architecture.md) + [docs/design/implementation-status.md](docs/design/implementation-status.md)
    - Mixin 数量（新增/删除 `db/db_*.py` 文件时）→ 更新 [AGENTS.md](AGENTS.md) 项目结构 + [docs/architecture.md](docs/architecture.md) + [CONTRIBUTING.md](CONTRIBUTING.md)
    - CLI 子命令数量（新增/删除 `cli/main.py` 子命令时）→ 更新 [docs/cli_reference.md](docs/cli_reference.md) + [TOOLS.md](TOOLS.md)
    - 支持语言数量（新增 parser 时）→ 更新 [README.md](README.md) + [AGENTS.md](AGENTS.md) 技术栈 + [pyproject.toml](pyproject.toml)

    **自检方法**：commit 前运行以下命令快速核对关键指标是否与文档一致：
    ```powershell
    # MCP 工具数
    (Select-String -Path "server\mcp_server.py" -Pattern "@mcp\.tool\(\)" | Measure-Object).Count
    # Mixin 数
    (Get-ChildItem db\db_*.py | Measure-Object).Count
    # Schema 版本
    (Select-String -Path "db\schema.py" -Pattern "SCHEMA_VERSION").Line
    ```

    **违反示例**：新增了 3 个 MCP 工具但未更新 `docs/mcp_tools.md`，导致文档说 195 个实际 198 个 → 禁止。
    **正确示例**：新增 MCP 工具时，同一次 commit 更新 `docs/mcp_tools.md` 头部数字 + 工具列表 + `README.md` 中的数字。

23. **TRAE IDE 沙箱拦截 sh.exe 子进程对 `~/.callwarden/` 的写操作（SQLITE_CANTOPEN）**：在 TRAE IDE 中通过 `git commit` 触发 Git Bash `sh.exe` 执行 pre-commit hook 时，`cw --refresh-all` 调用 `sqlite3.connect()` + `PRAGMA journal_mode=WAL` 会因沙箱拦截文件创建/写操作而抛 `sqlite3.OperationalError: unable to open database file`（SQLITE_CANTOPEN, code 14），导致 hook 退出非零，commit 被取消，迫使用户 `--no-verify` 绕过。

    **根因**：TRAE IDE 沙箱是**进程树型**拦截，基于父进程链判断，无法通过 `powershell.exe` / `cmd.exe` 中转绕过；同一命令在 PowerShell 终端中直接执行不会触发沙箱（PowerShell 进程不在 sh.exe 进程树下）。

    **症状区分**：
    - **间歇性 SQLITE_CANTOPEN**：MCP Server 或其他 cw 进程持有 `-shm` 文件锁 → 重试可恢复（hook 已内置 3 次重试，间隔 2 秒）
    - **持续性 SQLITE_CANTOPEN**：TRAE 沙箱拦截 → 重试无效，必须改用 PowerShell 终端执行 `cw refresh --all` 后用 `git commit --no-verify` 跳过 hook

    **规避方法**（按优先级）：
    1. **首选**：在 TRAE IDE 的 PowerShell 终端中手动运行 `python cw.py --refresh-all`，然后运行 `git commit --no-verify` 跳过 hook（DB 已刷新，满足规则 1）
    2. **配置沙箱白名单**：Settings → Conversation → Custom Sandbox Configuration，添加允许规则：`C:\Users\<user>\.callwarden\`（写权限）
    3. **停 MCP Server**：若间歇性失败，`cw server --stop` 释放 `-shm` 锁后再 commit
    4. **用 `python cw.py` 替代 `cw.exe`**：entry_point 启动时 sqlite3 偶发失败，`python cw.py` 更稳定

    **已沉淀修复**（见 [install.py](install.py) `_pre_commit_hook()`）：
    - pre-commit hook 重试 3 次（间隔 2 秒）覆盖间歇性锁场景
    - 重试耗尽后打印 TRAE 沙箱排查建议 + PowerShell + `--no-verify` 绕过指引
    - 保持 `exit 1` 硬门禁（AGENTS.md 规则 1：提交前必须全量刷新数据库）

24. **Rust daemon ACL 变更必须跑完整 daemon 测试集**：扩展 `ADMIN_ONLY_METHODS` 或 workspace owner 校验后，只跑新增 ACL 用例会漏掉旧测试契约失配。必须运行 `cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib`，并逐项处理失败；backup/restore/GC/mount 等 admin-only handler 的测试必须使用 admin peer，readonly 方法清单也必须同步更新。不得用局部模块测试通过替代完整 daemon 回归结果。

## 文档索引

| 文档 | 说明 |
| ---- | ---- |
| [TOOLS.md](TOOLS.md) | 工具使用指南（CLI/MCP/场景映射） |
| [README.md](README.md) | 项目首页 |
| [docs/quickstart.md](docs/quickstart.md) | 快速开始 |
| [docs/cli_reference.md](docs/cli_reference.md) | CLI 命令参考 |
| [docs/mcp_tools.md](docs/mcp_tools.md) | MCP 工具参考 |
| [docs/architecture.md](docs/architecture.md) | 架构设计 |
| [docs/deployment.md](docs/deployment.md) | 部署指南 |
| [docs/task_create_subtask.py](docs/task_create_subtask.py) | 挂载子任务的标准脚本模板 |
| [CONTRIBUTING.md](CONTRIBUTING.md) | 贡献指南 |
| [CHANGELOG.md](CHANGELOG.md) | 版本变更 |


## Call Warden 自动沉淀规则

<!-- CALLWARDEN_RULES_START -->
<!-- 自动同步区域，请通过 cw rule sync 更新，不要手改 -->
<!-- CALLWARDEN_RULES_END -->
