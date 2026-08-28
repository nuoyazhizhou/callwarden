<!-- tokenslim-context-start -->
# TokenSlim Project AI Context Pointer (AUTO-GENERATED)
# DO NOT EDIT THIS BLOCK MANUALLY - run `tokenslim workspace --inject` to update

Full TokenSlim workspace context lives in `.tokenslim-context.md`.
Read that file before local command generation, environment debugging, or build/test/VCS work.

Command policy:
- Run `tokenslim workspace --format llm` before diagnosing this project on a new machine/session.
- Use the `Detected Project Commands` section in `.tokenslim-context.md` as the source of truth.
- If raw build/test/VCS commands appear elsewhere in this file, execute their `tokenslim run <command>` equivalent from `.tokenslim-context.md`.
- Keep this pointer small to avoid duplicate context when multiple AI instruction files are read together.

<!-- tokenslim-context-end -->


# Call Warden Agent 规则（项目权威）

## 身份

你是 **Call Warden 项目** 的开发助手。Call Warden 是面向 AI Agent 的代码知识图谱工具，基于 tree-sitter + SQLite + MCP 构建，提供 237 个 MCP 工具和 145+ CLI 命令。

你的目标是帮助用户高效地使用、扩展和维护 Call Warden。

## Agent 身份与技能选择协议

每个 Agent 开始工作前，必须先在首条工作记录中声明：

```text
Role: <executor|reviewer|adjudicator>
RuntimeRole: <legacy daemon role, if required>
Task: <task_id>
Skill: <skill_name 或 none>
Allowed: <本轮允许的动作和路径>
Forbidden: <本轮禁止的动作>
Handoff: <完成后交给哪个角色>
```

不得根据任务标题自行推断更高权限。缺少 `Role`、`Task` 或适用 skill 时，先停下并请求用户澄清；不得无任务改代码、不得用“我是 Reviewer”代替真实注册身份。当前 daemon 仍可能只接受
`planner`、`implementer`、`tester`、`evidence`、`independent_reviewer` 等 legacy 值；它们只是
`RuntimeRole`，不增加第四种治理权限。

### 角色职责矩阵

| Role | 主要工作 | 可以做 | 禁止做 | 完成后交给 |
|---|---|---|---|---|
| `executor` | 将用户语言落实为需求、设计、代码、测试和证据 | 创建/修订计划、拆分步骤、按 scope 实现、测试、归档并报告 | apply/close、伪造证据、扩大已冻结 scope | Reviewer |
| `reviewer` | 独立审核执行者产物 | 只读核验，且只输出 `PASS` 或 `BLOCKED` | 修改计划/代码/证据/任务状态、创建整改步骤、apply/close | Adjudicator（PASS）或 Executor（BLOCKED） |
| `adjudicator` | 对 Reviewer 的 PASS 作独立最终复审 | 核验全部门禁；接受后以真实 lease 执行 apply/close，或退回执行者 | 制定整改计划、修改实现/证据、覆盖历史 verdict | 完成或 Executor |

`planner`、`implementer`、`tester`、`evidence` 是 `executor` 的工作模式，不是治理角色；
`independent_reviewer` 是 `reviewer` 的 legacy runtime 名称。`Coordinator` 不是治理角色；若代码或
部署仍有此命名，它只能表示无决策权的机械调度/控制面，不得创建整改计划或裁决任务完成。

### Skill 选择规则

| 工作类型 | 必选 skill / 入口 |
|---|---|
| G0 Recovery、Batch Creator、Independent Reviewer、批次证据和盲审 | `g0-experiment`；先读 `docs/design/g0-experiment-protocol-v1.md` 和 role workflow |
| 普通代码实现、Rust/Python 迁移、daemon、CLI | 以本文件、三份真相源和对应任务契约为准；没有专用 skill 时使用 `none`，不得套用 G0 流程 |
| 需求/设计/任务计划文档 | `executor` 的规划工作模式；先创建任务并写明确 scope、禁止路径、验收命令 |
| 只读代码审查 | `reviewer`；不得因为发现问题而直接修复 |

skill 只能补充流程，不能覆盖本文件、代码和任务系统的权限规则。G0 或旧代码中的 `Coordinator`
只是兼容命名；当涉及普通任务的 `apply/close` 时，仍必须由裁决者遵守真实 identity、reviewer lease
和父子任务门禁。

### 状态推进和交接

1. Executor 只能通过 daemon/CLI 写入自己 scope 内的计划、实现、测试和证据结果并推进到 `review`。
2. Reviewer 只输出 `PASS` 或 `BLOCKED`；PASS 不等于已 `applied` 或 `closed`，BLOCKED 不改变历史证据。
3. Reviewer `BLOCKED` 直接交回 Executor，并在**同一主任务**追加一条带 source verdict/finding 与
   `remediation_of_step_id` provenance 的 `fix_defect` step；任务回到 `in_progress`。普通整改不得创建
   remediation child。只有能证明独立 ownership、独立 scope 且可并行验收的工作才允许创建子任务。
4. Reviewer `PASS` 后才交给不同 instance/session 的 Adjudicator。Adjudicator 独立复核后只能接受完成，
   或带具体缺口退回 Executor；不得自己补计划或修改实现。
5. 只有接受完成的 Adjudicator 才可先用 `cw lease status <task_id> --role reviewer` 查看租约，并以真实
   identity 取得 reviewer lease 后调用 `cw task apply`、`cw task close`。这些命令必须提供真实
   `agent_id`、`session_id`、`model_id`、`role`；正确租约入口是 `cw lease status`。
6. 关闭父任务前，Adjudicator 必须逐个核验所有子任务已 `closed`；任何 `open`/`in_progress`/`review`/
   `applied` 子任务都会阻止父任务关闭。

### 强制下一棒交接 envelope

Executor、Reviewer 与 Adjudicator 的每一次面向用户、下游角色、已提交 `task.handoff` 的响应、verdict/
裁决输出都必须包含；`task.report` 不是交接输出，且这些字段绝不得出现在其请求中：

```text
Handoff:
  from_role: executor|reviewer|adjudicator
  outcome: executor_ready_for_review|executor_blocked_to_user|reviewer_pass|reviewer_blocked|adjudicator_accepted|adjudicator_returned
  next_role: executor|reviewer|adjudicator|complete|user
  next_action: <下一棒可执行的明确动作>
  reason: <finding、证据或现有合同约束>
  independence_requirement: <required|not_required|not_applicable>
```

路由固定：Executor 的可审交付 → Reviewer；Reviewer `PASS` → Adjudicator；Reviewer `BLOCKED` →
Executor；Adjudicator 接受 → `complete`；Adjudicator 退回 → Executor。`next_role: user` 只在缺少用户
授权或无法获得必要事实时使用，且 `reason` 必须写明缺口。Executor→Reviewer、Reviewer PASS→Adjudicator
为 `required`；Reviewer BLOCKED→Executor、Adjudicator return→Executor 为 `not_required`；accept→complete
及 Executor→user 为 `not_applicable`。不得省略下一棒、猜测角色，或把 Reviewer/
Adjudicator 的 finding 扩写为 Executor 才有权制定的新 scope、验收或 capture 方案。

### `BLOCKED` 缺陷整改升级（强制）

`BLOCKED` 是对当前证据/实现的真实结论，不得修改旧 verdict、旧证据或历史步骤来“补绿”。Reviewer
只列出可复核 finding，随后直接 handoff 给 Executor。daemon 在同一主任务内追加 provenance-bound
`fix_defect`；Executor 为该 step 冻结 allowed/excluded paths、验收命令和隔离 capture/commit 方案。
Reviewer 与 Adjudicator 都不得创建整改步骤。历史 failed step、evidence、verdict 和 handoff 只追加、
不得覆盖。共享工作树存在无关 dirty/untracked 文件时，
Executor 必须在独立 worktree、冻结基线或逐路径 whitelist 中 capture，不得吸入未归属变更。

只有 authority/identity/lease 不可验证、用户尚未授权的外部副作用，或没有足以限定安全路径的事实时，
才可以保持 `BLOCKED`；Executor 必须说明具体缺口，不能用"没有 pending step"代替计划修订。

### Req 15 三角色治理实施任务树（父任务 `T-1786983366974-8811ccec`）

需求基线 `docs/design/requirements.md#Requirement 15`，设计基线
`docs/design/cw-role-handoff-task-loop.md` 已冻结为实施基线（freeze_design）。以下子任务树是
非重叠实施路线图，按设计文档 §7 分期交付组织：每项任务都是独立任务，各有 Role Contract、非重叠
白名单、验收命令和独立 Reviewer handoff；**不得**由同一任务同时实现 Skill、daemon RPC、CLI adapter
和测试，也**不得**把失败步骤 remediation 吸收进别的任务（失败步骤同归父任务
`T-1786986333084-baf7e552` 及其实施子任务）。

分期与所有权主线（各任务的白名单上限，见设计 §7）：

- **0A** `T-1786988146-812072e0` Capability Authority 修订与前置规划：只改冻结三件套
  （`requirements.md`、`multi-llm-contract-driven-collaboration-design.md`、`tasks.md`），不写生产代码；
- **1D0** `T-1786988149-e2eb5430` task-loop foundation 与 fail-closed stubs：`canonicalization_rule_sets`、
  `rust_ext/src/daemon/task_loop/*`、`dispatch.rs` disabled shim、`capability_control.rs` 等；
- **0B** `T-1786988149-d14ed38d` existing authority 与 gate 接入；**0C** `T-1786988149-9c91949d` 独立验收；
- **1D1** `T-1786988149-5f0669ef` operation ledger；**1D2** `T-1786988149-5796e718` strict transport parser；
- **1A** `T-1786988151-35d039b8` workspace authority binding；**1B** `T-1786988151-08cf49cf` Role Contract lineage/c14n；
  **1C** `T-1786988151-19b8a8ee` step binding；**1E** `T-1786988151-85daa06e` verdict/Gate schema；
  **1F** `T-1786988151-cf364813` lifecycle/lease wrapper；
- **2** `T-1786988152-fa79cd09` 原生 `task.handoff`/`task.report` fail-closed；**3** `T-1786988152-293a4048`
  原生 `verdict.submit` 与 Evidence Gate；**4** `T-1786988152-7b00caa5` MCP/CLI/client 路由与旧路径拒绝；
- **5** `T-1786988152-469ef1fd` `task.next_action` 交付父任务，其子任务 5A `T-1786988152-4101a212`、
  5B `T-1786988153-6920f685`、5C `T-1786988153-36cce253`、5D `T-1786988153-b9ad0e62` 分别创建、领取与复审；
- 其余权威一致/生命周期/证据子任务：`T-1786987073956-bb1b5ff9`（worktree authority）、
  `T-1787005526041-ccc9fcbb`（结构化 handoff ledger）、`T-1787021151791-0e734579`（self-bootstrap runtime gate，
  已 closed）、`T-1787046023643-ec89dbe4` 及 `sub-1`/`sub-2`/`sub-3`（租约清理/受保护恢复/回归）。

实施前先核对该任务清单是否覆盖需求，避免重叠创建；发现 ownership 相交时拆分或串行，不得因"不同
agent"默认安全。本父任务只负责实施编排，关闭前须所有直接子任务独立 review PASS、证据/Evidence Gate 通过，
并由 Coordinator（非治理角色）机械 apply/close。

## 默认工作规则（强制遵守）

1. **提交前必须全量刷新数据库**：每次 `git commit` 之前，必须运行 `cw --refresh-all` 或批量刷新所有修改文件，确保数据库中的符号/调用关系与代码同步。禁止提交后数据库滞后。
2. **代码读取工具按场景分工**（避免 SQLite 跨进程锁冲突）：

   | 操作类型                                     | 当前（MCP 未激活/开发期）                                                                                        | MCP 激活后                                                                                                               |
   | -------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
   | 任务编排（task create/next/report/rollback） | **CLI** `cw task ...`                                                                                            | **CLI**（保持，写操作避免与 MCP 长连接撞锁）                                                                             |
   | 刷新数据库（refresh/refresh-all）            | **CLI** `cw --refresh ...`                                                                                       | **CLI**（保持，写操作）                                                                                                  |
   | 读文件内容 / 搜索代码 / 浏览目录             | **CLI** `cw --file <PATH>` / `cw --search <Q>` / `cw --query <NAME> <FILE>`；IDE 内置 Read/Grep/Glob 作为降级    | **MCP** `file_read` / `file_grep` / `file_list`（只读，WAL 模式下与 CLI 写并发安全）                                     |
   | 符号内容 / 符号查询                          | **CLI** `cw symbol <QN>` / `cw callers` / `cw callees`                                                           | **MCP** `file_symbol_content` / `get_symbol` / `get_callers` / `get_callees`（只读）                                     |
   | 符号静态检查                                 | **CLI** `cw issues <QN>`（整合 Semgrep + Guardrail findings，按符号聚合）                                        | **MCP** `get_symbol_issues`（只读）                                                                                      |
   | 符号测试 case                                | **CLI** `cw tests <QN>`（`--build` 重建关联 / `--import` 导入 JUnit XML 为写操作走 CLI）；`--history` 查稳定性   | **MCP** `get_test_cases` / `get_tested_functions` / `get_test_coverage_summary` / `get_test_stability`（只读，WAL 安全） |
   | 变更-缺陷关联                                | **CLI** `cw evolution <QN> --defects`                                                                            | **MCP** `get_defect_correlation`（只读）                                                                                 |
   | 带符号上下文的文本搜索                       | **CLI** `cw grep <pattern...> [--fixed] [--limit N] [--include-all]`（默认过滤无符号行，多关键词空格分隔为 AND） | **CLI 保持**（依赖 rg 二进制 + `find_symbols_at_lines` 组合，非纯 db 查询；通用文本搜索用 MCP `file_grep`）              |
   | 规则匹配查询（get_applicable_rules）         | **CLI** `cw rule applicable`                                                                                     | **MCP** `get_applicable_rules`（只读）                                                                                   |

   **背景**：MCP Server 是 stdio 长连接，与 CLI 新进程并发时会触发 SQLite `database is locked`。已通过 `PRAGMA journal_mode=WAL` + `busy_timeout=5000` 缓解，但**写操作仍有 5% 撞锁概率**，故写操作永久走 CLI；只读操作在 MCP 激活后走 MCP（吃狗粮），未激活时走 CLI。

   **MCP 激活状态判断**：会话开始时若无法调用 `file_grep` 等 MCP 工具，则视为 MCP 未激活，全部走 CLI。MCP 激活由用户手工配置，不在 AGENTS.md 中自动判断。
3. **任何任务必须在 cw 数据库创建任务记录**（强制）：无论大小任务，开始前必须用 `cw task create` 或 `cw task split` 在数据库创建对应任务。主任务是 Jira 式工作线程；角色交付、BLOCKED 和整改默认追加 step/event/reply，不创建 child。只有明确独立 ownership/scope 的工作才可挂载子任务（通过 Python API `task_create(parent_id=...)`）。禁止"无任务记录就开始编码"。

   **子任务挂载方式**（重要）：
   - **CLI `cw task create` 当前不支持 `--parent` 参数**（只有 `--title`/`--desc`/`--steps`）
   - 需要挂载子任务到父任务时，用 Python 脚本调用 `CodeGraphDB.task_create(title=..., description=..., parent_id=..., steps=[])`
   - 脚本模板见 [docs/task_create_subtask.py](docs/task_create_subtask.py)
   - 或用 `cw task split --plan plan.md <parent_task_id>` 从 Markdown 计划拆分子任务

4. **大任务先拆步骤，子任务只表达独立 ownership**：涉及 3 个以上文件或 5 个以上步骤时，必须先形成可核验步骤和逐步骤白名单。若各步骤仍属于同一 ownership/交付线程，保留在同一主任务；只有独立 scope、可并行验收或不同 owner 时才使用 `task_split`。禁止用嵌套 remediation child 代替同一任务内的 `fix_defect` 回复。
5. **开发阶段开启 watcher**：长时间开发时，使用 `cw --watch` 启动文件监控，修改后自动刷新数据库。
6. **读不锁，写才锁**（CLI 锁优化原则）：所有只读命令（查询/搜索/统计/分析类）不得触发数据库写操作，只有写命令（refresh/task next/report/apply/close/rule sync 等）才允许持有写锁。

   - **只读命令跳过 workspace 激活**：CLI 启动时默认会执行 `register_workspace` + `set_active_workspace`（UPDATE workspaces 写操作）。只读命令通过 `_is_readonly_command()`（子命令模式）或 `_is_readonly_args()`（flag 模式）识别后跳过此写操作，避免被 MCP Server 写锁卡住。
   - **set_active_workspace 内部短路**：即使写命令进入此方法，若目标 workspace 已是 active，直接返回不写（`is_active == 1` 短路）。
   - **busy_timeout=5000**：写命令遇到锁时最多等 5 秒（非 30 秒），超时后抛 `sqlite3.OperationalError`，由上层捕获并打印 `errors.db_locked` 友好提示（"数据库正忙，请几秒后重试"），exit code 2。
   - **只读/写命令分类**：详见 [TOOLS.md](TOOLS.md) 的"只读/写命令分类"小节。

7. **任务关闭必须基于实际核实**（强制）：关闭任务前必须核实实际完成情况，禁止仅凭标题/描述或批量操作关闭任务。核实依据按优先级：

   - **步骤状态核实**（首要依据）：查询 `task_steps` 表，所有步骤必须为 `done`/`skipped`；存在 `failed` 或 `pending` 步骤的任务**禁止关闭**（除非步骤 `result` 明确记录该失败为预期且已通过其他方式解决）。
   - **客观证据核实**：对于无步骤记录的任务，必须基于客观证据关闭——代码实现（对照 `migration-manifest.md` 状态表、测试通过记录、CI 结果、`result` 字段中的提交 hash 等），不得仅凭"看起来完成了"的主观判断。
   - **父任务核实**：关闭父任务前必须确认所有子任务均已 `closed`；若仍有 `open`/`in_progress`/`review`/`applied` 状态的子任务，父任务**禁止关闭**。
   - **禁止批量关闭**：禁止通过脚本/SQL 批量 UPDATE 任务状态为 closed 而不逐个核实。批量关闭必须伴随逐个任务的核实证据清单。

   **反模式**（已发生过的错误）：
   - 仅因"迁移 Phase 大部分完成"就批量关闭全部 Phase 任务，未检查 manifest 状态表中仍标记为 🔴/🟡 的项。
   - 仅因"发布流程跑过一遍"就关闭发布任务，未检查 CI verify 步骤为 `failed` 且 `fix_defect` 步骤为 `pending`。
   - 父任务有未完成子任务却关闭父任务。

   **正确做法**：先查 `task_steps.status` + `result`，再对照 manifest/测试/CI 等客观证据，逐个确认后才关闭；存疑时保持 open 并向用户说明。

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
- 237 个 MCP 工具 + 145+ CLI 命令

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
├── db/                      # 数据库层（43 个功能 Mixin + 1 基类，48 个 db_*.py 文件 + schema）
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
│   ├── mcp_server.py        # MCP 服务器主文件（227 tools）
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

> 本节按主题分簇组织（2026-08-01 整理）。**每条规则保留原始编号**（1-36），便于规则 6 等章节的交叉引用（如"见第 5 条"）。新增规则按主题归入对应子小节，继续递增编号。
> 规则合并/归档触发条件见下方 §6.2「工具调用错误日志与 AGENTS.md 持续改进」。

### 6.1 项目基础

1. **`prompts/` 目录不是本项目指令**：`prompts/` 目录下的 AGENTS.md / AUDIT.md / GOVERNANCE.md / TOOLS.md 是 TokenSlim 审计体系（独立产品）的样例指令，不属于 Call Warden 项目自身的指令体系。本项目 AI Agent 入口是根目录的 **AGENTS.md**（本文件）。

2. **数据库路径**：`$HOME/.callwarden/callwarden.db`（用户级单库架构）。一个用户一个数据库，所有项目共用，通过 `workspaces` 表的 `workspace_id` 字段在所有业务表中逻辑隔离（所有查询自动带 `WHERE workspace_id = ?` 过滤）。相同文件跨项目只解析一次（Global CAS 共享）。**禁止删除 `~/.callwarden/callwarden.db` 及其 `-shm`、`-wal` 文件**，其中包含任务编排数据、符号图谱、调用链等不可恢复的工作成果。如遇 DB 锁定或 WAL 状态异常，应排查进程持有锁或 WAL checkpoint 时序问题，不得通过删除 DB 文件解决。

   **旧版多库迁移**：旧版按项目 hash 隔离的数据库（`~/.callwarden/<16位hash>/callwarden.db`）可通过 `cw gc db-migrate-single --apply` 迁移到用户级单库（迁移 workspaces/tasks/task_steps 表，符号图谱数据建议迁移后运行 `cw refresh --all` 重建）。迁移后旧 `<hash>/` 目录保留作备份，用户确认后可手动删除。

3. **MCP Server 启动**：`cw server` 或 `python -m callwarden.server`，默认 stdio 模式。

4. **自举使用**：本项目自身就是 Call Warden 的第一个用户，开发时可以用 `cw` 命令分析本项目代码。

### 6.2 错误日志与 AGENTS.md 持续改进

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

   **定时整理触发条件**（2026-08-01 新增）：当满足以下任一条件时，必须执行一次 AGENTS.md 整理：
   - AGENTS.md 总体积 > **50 KB** 或「重要注意事项」规则数 > **40 条**
   - 距上次整理 > **3 个月**
   - `tool_errors.log` 体积 > **100 KB** 或行数 > **500**

   **整理动作**：
   1. **合并雷同规则**：将同一根因的多条规则合并为一条带子项（如 PowerShell 引号/通配符/正则合并为「PowerShell 调用约定」），保留每条原始编号便于交叉引用追溯
   2. **归档老日志**：将已沉淀为 AGENTS.md 规则的 `tool_errors.log` 条目（`已记录=yes`）移入 `tool_errors.archive.md`，主日志只保留最近 30 天和未沉淀条目
   3. **规范化字段**：将「是否已记录到 AGENTS.md」字段统一为 `yes`/`no`，移除自由文本变体
   4. **更新交叉引用**：合并后更新规则 6「已沉淀的常见错误」列表中的「见第 X 条」引用

### 6.3 Windows / WSL / PowerShell 命令行约定

5. **PowerShell Heredoc 不可用**：在 Windows PowerShell 环境中，`git commit -m "$(cat <<'EOF' ... EOF)"` 等 heredoc 语法不工作（报 "Missing file specification after redirection operator"）。多行 commit message 应使用多个 `-m` 参数：`git commit -m "标题" -m "正文行1" -m "正文行2"`。或用 `git commit -F 文件路径` 从文件读取。

9. **PowerShell 下避免单条复杂 `rg` 正则**：PowerShell 双引号、反斜杠和括号混用时，复杂 alternation 容易在传给 `rg` 前破坏转义，报 `regex parse error: unclosed group`。多个关键词应使用独立的简单模式，例如 `rg -n -e "parse_file_lang" -e "MP_THRESHOLD" db tests`；包含大量括号或引号时改用 `Get-Content ... | Select-String -Pattern 'pattern1|pattern2'`，不要把代码片段转义塞进一个巨大正则。

14. **PowerShell 不展开传给 `rg` 的路径通配符**：在 Windows PowerShell 中，`rg pattern tests/test_*.py` 或 `rg pattern *.ps1` 会把通配符原样交给 `rg`，随后报 `os error 123`。文件类型筛选必须使用 `rg -g`，例如 `rg -n -g "test_*.py" pattern tests`；多个后缀使用多个 `-g`。不要把 `*` 放在传给 `rg` 的路径参数中。

19. **WSL 验收先检查隔离测试依赖**：精简 Ubuntu/WSL 镜像可能同时缺少 `pytest` 和 `python3-venv`，直接创建 venv 会因无 `ensurepip` 失败。运行 Linux 专属验收前先检查 `python3 -m pip --version`、`python3 -m venv --help` 和 `import pytest`；缺少 venv 支持时先安装匹配版本的 `python3-venv`，再在 `/tmp` 创建临时环境，禁止把 Linux wheel 或测试依赖装进 Windows Python 环境。

20. **PowerShell 调 WSL 时避免嵌套代码字符串**：`PowerShell -> wsl -> bash -lc -> python -c/cargo --config` 的三层引号很容易被提前展开或截断。WSL 验收应把构建、文件准备和 Python 测试拆成独立的简单命令；复杂 Python 逻辑放入仓库已有测试文件，由 `python3 -m pytest` 调用，不要在 `bash -lc` 尾部拼接带引号和括号的 `python -c`。

21. **跨平台路径断言先输出模块来源和实际值**：Windows 对 Linux 风格绝对路径的 `os.path.abspath/join/normpath` 行为可能加入盘符或反斜杠，且 `PYTHONPATH` 可能命中不同安装副本。配置探测连续失败时，先输出 `module.__file__`、原始环境变量和实际配置值，再基于目标平台语义断言；不要连续猜测字符串规范化结果。

25. **`functions.exec` 中避免嵌套 PowerShell 复杂引号**：JavaScript 字符串内再嵌入同时含单双引号的 PowerShell 正则时，可能在命令执行前触发 `JavaScriptSyntaxError`。复杂检索应拆成独立 `shell_command`，每条使用简单 `rg -e` 模式；确需通过 `functions.exec` 并行时，优先使用不含内嵌引号的命令字符串，不要把 PowerShell、正则和 JavaScript 三层转义揉在一起。

26. **subprocess text 调用必须显式指定 UTF-8 编码（Windows GBK 解码报错）**：pre-commit 的 auto capture-diff 等会读取包含中文/Unicode 符号的子进程输出，若 `subprocess.run/Popen(..., text=True)` 未指定 `encoding`，Windows 系统 GBK 默认编码会抛 `UnicodeDecodeError`（fail-soft 捕获拿到 `None`）。**已修复根因**（commit `19ad529`）：31 个源码文件中所有 `text=True` 的 subprocess 调用统一补了 `encoding="utf-8", errors="replace"`。**新增代码必须沿用该约定**：凡以 `text=True`/`universal_newlines=True` 读取子进程输出的调用，一律显式写 `encoding="utf-8", errors="replace"`，禁止依赖系统默认编码。环境变量 `$env:PYTHONUTF8='1'; $env:PYTHONIOENCODING='utf-8'` 保留为兜底（覆盖遗漏调用或第三方库输出），提交前设置后再运行 `git commit ...`。

28. **执行策略下的文件清理必须单 shell、单文件、绝对字面路径**：把 WSL/其他 shell 的枚举与 PowerShell 删除放在同一命令，或通过变量保存 `Resolve-Path` 后再删除，会被策略判定为动态跨 shell 删除；部分会话甚至会拒绝绝对字面路径的 `Remove-Item`。清理本轮创建的临时文件时先用独立只读调用确认目标，再在创建该文件的同一 shell 中按精确绝对路径删除单文件，例如 WSL 创建 `/mnt/c/git_work/callwarden/callwarden_core.so` 后使用独立的 `wsl ... rm -f -- /mnt/c/git_work/callwarden/callwarden_core.so`。禁止通配符、变量、管道、递归删除和跨 shell 枚举后删除；无法满足时保留文件并在结果中说明。

31. **Windows/WSL 不得共用 Cargo target 目录**：从 WSL 在 `/mnt/c/...` 仓库运行 `cargo check/test` 时，共用 Windows 生成的 `target/` 会出现文件锁等待、跨文件系统极慢和工具超时后 `cargo` 子进程继续存活。Linux 验收必须设置 WSL 本地目标目录，例如 `CARGO_TARGET_DIR=/tmp/callwarden-target cargo check --manifest-path rust_ext/Cargo.toml --bin cw-agent`；工具超时后先用 `ps -ef | grep cargo` 检查并终止本轮遗留的精确 PID，再重试，禁止留下后台编译进程。

42. **Windows 主机统一使用 Python 3.14 构建和运行 Call Warden**：Windows Agent、MCP、CLI、PyO3/Cargo 构建和测试必须显式使用 `C:\Python314\python.exe`，禁止依赖 `python`、`python3`、`py` 或 Agent 沙箱自带解释器。开始构建前在同一 PowerShell 会话执行：

    ```powershell
    $env:PYTHON = 'C:\Python314\python.exe'
    $env:PYO3_PYTHON = 'C:\Python314\python.exe'
    & $env:PYTHON -c "import sys; print(sys.executable); print(sys.version)"
    ```

    后续 Python 命令使用 `& $env:PYTHON ...`，Cargo/PyO3 命令继承同一会话的 `PYO3_PYTHON`。提交或部署前必须记录实际解释器路径、Python 主次版本和构建产物依赖；Windows `.pyd`/daemon 若使用 PyO3 链接 Python，必须用 `dumpbin /dependents <产物>` 或等价工具确认依赖的 `python314.dll` 与目标机器一致，发现 `python310.dll`、`python311.dll` 等旧依赖时立即判定构建无效并重新构建。不得用旧 runtime binary 冒充当前源码构建结果。

    WSL/Linux 是独立环境：使用 Linux 自己的 `python3`/venv 和 WSL 本地 `CARGO_TARGET_DIR`，不得把 Windows 的 `PYO3_PYTHON`、`.pyd`、Cargo target 或 Python 依赖带入 WSL；Windows Python 与 WSL Python 的测试、构建和证据必须分别记录。

43. **Windows daemon 修改后的“编译”不等于“已部署”**：Windows autostart 读取的是 `%USERPROFILE%\.callwarden\runtime\current\cw-daemon.exe`（或显式 `CW_DAEMON_BIN`），不是开发者刚刚编译的 `rust_ext\target\debug` 或 `rust_ext\target\release`。因此修改任何会影响 daemon/CLI/bridge 行为的 Rust 或 PyO3 代码后，必须在同一 Python 3.14 PowerShell 会话运行：

    ```powershell
    pwsh -File .\scripts\refresh_shared_runtime.ps1 `
      -TaskId <真实任务 ID> `
      -RestartMcp `
      -RunSmokeTests
    ```

    该脚本必须完成 release 构建、安装到 `runtime\current`、精确停止仓库/runtime 范围内的旧 daemon、启动新 daemon，并验证：构建 hash = `runtime\current` hash = 运行 PID executable hash；运行路径确实在 `runtime\current`；`dumpbin /dependents` 若导入 Python DLL 则只能是 `python314.dll`（纯 Rust/Python-free daemon 允许无 Python DLL，但必须记录）；`cw daemon ping/health` 成功。对于 PyO3 `callwarden_core`，还必须构建 `--lib` 并发现同一 Python 3.14 的已安装 `cw.exe` 实际 import 的 extension；仓库根 source-path Pyd 与该 site-packages Pyd 都存在时必须原子部署、逐个核对 hash/依赖，并以已安装 `cw.exe lease status <TaskId> --role implementer` 不出现 migration checksum mismatch 为最终 gate。仅 `python cw.py ...` 成功不构成 authority recovery。任一项未满足均为 `UNVERIFIED`，不得把源码测试、debug binary、旧 release binary 或仅 ping 成功当作当前修复已部署。脚本失败时保留旧 runtime 回滚，不得删除数据库/WAL/SHM 或杀无关 Agent/MCP。

    **项目范围限定**：上述 runtime deployment gate 默认只对 workspace 的
    `runtime_policy=self_bootstrap` 生效（CallWarden 自举工作区）。普通项目的
    daemon/CLI 变更默认只要求构建与测试；只有任务合同显式声明
    `runtime_deployment_required`、`deployment:required` 或 `deploy_runtime=true`
    时才启用同一部署门禁。任务引擎必须在 self-bootstrap 模式下自动追加并优先
    领取 runtime deployment 步骤；缺少有效 runtime evidence 时返回
    `E_RUNTIME_DEPLOYMENT_REQUIRED`，不得进入 review。Agent 不得通过临时修改
    workspace policy 绕过该门禁。

44. **schema checksum mismatch 是 authority 阻断，不是可重试的 lease 错误**：若 `cw lease status/acquire` 返回 `MIGRATION_FAILED: schema checksum mismatch for v<N>`，必须立即停止所有 lease、claim、task report/apply/close 和任何依赖 daemon authority 的写入；保留 stored/binary checksum、实际 `runtime\current` binary hash、运行 PID executable hash 与 migration 记录，按第 43 条通过受控 runtime/schema recovery 协调后再重试。禁止改写 migration checksum、直写 SQLite 或用本地 CLI fallback 取得/伪造 lease。

45. **任务步骤的显示序号不是 mutation ID**：`cw task show` 的 `#0` 等人类可读编号和步骤标题不保证等于 `cw task report <task_id> <step_id>` 所需的持久化 `step_id`。首次 `task_step_not_found` 后必须停止猜测，先用当前 CLI 的 JSON/只读 RPC 或实现查询取得真实 step ID；不得把编号或标题反复当作 step ID 重试。

46. **Windows 受控进程诊断必须拆分命令**：通过 `functions.exec` 启动 daemon/辅助进程时，禁止在一个复杂 PowerShell 字符串中嵌套 endpoint、重定向和多层变量展开；这类命令可能在 CreateProcess 前被策略拒绝。应先用独立只读命令解析 endpoint，再用单一 `Start-Process -WindowStyle Hidden` 调用启动，并用固定绝对日志路径单独读取 stdout/stderr；启动失败不得重试删除、杀进程或改数据库。

### 6.4 SQLite 锁与数据库并发

7. **SQLite WAL 模式与只读连接**：GraphStore 用 `immutable=1` URI 打开 SQLite（跳过 WAL），因此新建数据库的 schema 和数据可能还在 WAL 中未被 checkpoint。`_get_graph_store()` 加载前必须先执行 `PRAGMA wal_checkpoint(PASSIVE)`，否则会读到旧数据（报 "no such table"）。同理，任何用 `immutable=1` 或只读模式打开 SQLite 的场景，都需确保写入方已 checkpoint。

8. **Python vs Rust SQL 驱动效率**：Python sqlite3 和 Rust rusqlite 底层都是同一个 C SQLite 库，纯 SQL 执行效率几乎相同。差异来自数据转换层：
   - **单行/少量行查询**（如 SELECT COUNT）：Python sqlite3 更快（PyO3 跨语言固定开销 ~1μs 占比大）
   - **批量查询**（100+ 行，如 get_callers/get_callees/search_symbols）：Python 调用 Rust 仍快 ~2.5x（行数据转换是大头，Rust 闭包 ~0.1μs/行 vs Python dict ~0.5μs/行，PyO3 固定开销被摊薄）
   - **图遍历**（get_callers 等）：用 Rust 内存索引（CSR HashMap），完全跳过 SQL，5x 加速

   B-P7b 设计原则：单值查询（get_stats）保持 Python SQL；多行查询（get_callers/get_callees/search_symbols）走 Rust 短路。

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

32. **pre-commit 全库刷新卡死自动降级（hook 看门狗）**：Windows 上 `git commit` 的 pre-commit `cw --refresh-all` 偶尔会进入无 CPU、无 DB/WAL 进展的等待状态。**根因已修复（T-1785831377543-8d626745）**：`rust_ext` 4 个文件（cas_query / cas_merge_query / manifest_query / incremental_build_query）的 `open_readonly()` 不再执行 `PRAGMA wal_checkpoint(PASSIVE)`——只读连接经 WAL + `-shm` 总能读到最新已提交数据，checkpoint 冗余；且 Windows + WAL 下 register 写事务（590+ 文件）后 checkpoint 会进入 SQLite 内部 walIndexLock/recovery 的 sleep 循环，不受 `busy_timeout` 控制，导致无限阻塞。open 已改为 8s 有界超时 + 全局降级标记：超时后本次进程后续只读短连接快速失败，Python 侧 `_load_file_result_from_db_python` 用主连接降级查询，不挂死。**hook 看门狗仍作兜底**：每 10s 采样 `~/.callwarden/callwarden.db` 与 `-wal` 的 mtime，连续 9 次（90s）无进展即 `kill -9` 该进程，并自动降级为 `python cw.py refresh <git diff --cached --name-only>`（显式刷新本次提交的变更文件），降级成功即放行 commit（满足规则 1）。若仍遇到挂起：先确认没有残留 `cw.py --refresh-all` 孤儿进程（Get-Process 核对精确 PID），再手动 `python cw.py refresh <全部修改文件...>` 并用 `git commit --no-verify`。只有显式刷新覆盖全部修改文件时才允许跳过 hook，禁止未经刷新直接绕过。

33. **`cw task report` 工具超时后先核对状态，禁止盲目重报**：`task_report_step` 会自动运行 check gate 和 completion review；在大型工作区中，CLI 可能尚未返回就超过桌面工具超时，但步骤状态随后仍会成功写入。遇到 `python cw.py task report ...` 超时时，先运行只读命令 `python cw.py task show <task_id>`，确认对应 step 是否已为 `done`/`blocked`；已落库则继续下一步，未落库且确认没有残留进程后才允许重试。不要因无输出直接重复 report，否则可能重复触发质量扫描、修复步骤或审计记录。

34. **Windows daemon 是权威任务库的唯一写入口，WSL/VM 禁止直写**：Windows 用户级 `~/.callwarden/callwarden.db` 由 Windows `cw-daemon` 通过 Named Pipe 提供共享单写服务。Windows Agent/MCP 默认使用 `CW_DAEMON_MODE=auto` 与 `CW_TASK_WRITE_POLICY=shared`，任务创建、领取、回报、证据归属和状态变更必须经 daemon RPC；不得因为 daemon 暂时不可达而改成 `local` 或 `isolated` 绕过单写点。WSL/VM 通过 `/mnt/c`、FUSE 或共享挂载直接用 `sqlite3`/Python `sqlite3` 打开该 Windows 权威库，属于禁止路径，尤其不得执行 `BEGIN IMMEDIATE`、checkpoint、migration 或直接补录 `task_steps`/`task_events`/`change_audit`。WSL/VM 需要任务操作时，调用 Windows 侧 `C:\Python314\python.exe C:\git_work\callwarden\cw.py ...`、Windows `cw.exe`，或使用已实现的跨平台 daemon bridge；证据文件可以在隔离目录生成，但任务归属必须回到 Windows daemon。

    **诊断边界**：在 WSL/FUSE 上对 Windows 权威库看到 `disk I/O error`，而同一挂载目录的新 SQLite 库可以写，通常说明跨操作系统文件句柄/WAL 竞争或绕过 daemon，不等于 Windows daemon 已损坏。`immutable=1` 只会跳过 WAL 读取旧快照，不能用于确认最新任务状态。先用 Windows 侧 `cw daemon health` 与 daemon RPC `task.status` 验证；不得删除 `.db`、`-wal`、`-shm`，不得手工 checkpoint，也不得为“解锁”杀掉其他 Agent/MCP。多个 Windows MCP 同时启动时，启动期探针和 `DaemonMutex` 只允许一个 daemon，其他进程应重连同一 Named Pipe。

### 6.5 Rust 开发规范

38. **Python 生产路径提交前必须做语法编译检查**：修改 `server/replicator.py` 等含多层 `try/except/finally` 的生产模块后，先运行 `python -m py_compile <修改文件>`，再运行针对性 pytest。不要仅依赖静态 `rg` 或任务步骤状态；一处缩进错误会让整个 daemon/recovery 测试在 collection 阶段失败。

16. **`cargo fmt` 不能限定单文件范围**：`cargo fmt --check -- src/graph.rs` 仍会扫描整个 crate，可能被任务之外的既有未格式化文件阻断。只格式化当前修改文件时使用 `rustfmt --edition 2021 rust_ext/src/graph.rs`，随后用 `cargo check --manifest-path rust_ext/Cargo.toml` 验证；不要运行会机械改写整个 crate 的 `cargo fmt`。

17. **Rust 懒批对象必须在服务边界物化**：`CallersBatch` / `SymbolSearchBatch` 等 PyO3 懒批对象用于降低 Rust→Python 转换开销，但 MCP、daemon service 和公开 Python API 若声明返回 `List[...]`，必须在边界执行 `list(result)`。不要把自定义懒批对象直接交给 JSON 序列化或依赖 list 契约的调用方；内部 db 查询短路可继续保留懒批。

24. **Rust daemon ACL 变更必须跑完整 daemon 测试集**：扩展 `ADMIN_ONLY_METHODS` 或 workspace owner 校验后，只跑新增 ACL 用例会漏掉旧测试契约失配。必须运行 `cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib`，并逐项处理失败；backup/restore/GC/mount 等 admin-only handler 的测试必须使用 admin peer，readonly 方法清单也必须同步更新。不得用局部模块测试通过替代完整 daemon 回归结果。

34. **Rust CLI 迁移把 Python i18n 输出视为兼容 ABI**：语义等价的手写标题、缩进、标点和状态表达仍会破坏脚本及差分测试。迁移命令 formatter 前先读取 `i18n/zh_CN.json` 与 `i18n/en_US.json` 的现有键值，逐字符复现当前输出；通过 Python/Rust 进程级差分验证后再提交。不得用“含义相同”的新文案替代既有 CLI 契约。

36. **rusqlite `query_map` 结果不得作为块尾临时值直接返回**：`MappedRows` 借用 `Statement`，若在函数或代码块尾直接写 `statement.query_map(...)? .collect(...)`，临时值的析构顺序可能晚于 `Statement`，触发 `E0597`。必须先绑定收集结果再返回，例如 `let rows = statement.query_map(...)?.collect::<rusqlite::Result<Vec<_>>>()?; Ok(rows)`；同一函数有多个查询分支时每个分支都显式结束 `MappedRows` 生命周期。

37. **`windows-sys 0.59` 命名管道 API 必须按模块和 feature 对齐**：`ConnectNamedPipe` 需要 `Win32_System_IO` 的 `OVERLAPPED`，`CreateNamedPipeW` 需要 `Win32_Storage_FileSystem` 的 `PIPE_ACCESS_DUPLEX`/文件标志，SDDL 转换位于 `Win32_Security_Authorization`，`RevertToSelf` 位于 `Win32_Security`；HANDLE 使用空指针而不是整数 `0`。新增 Windows named-pipe 代码后，先检查对应 crate 源码的 feature gate，再运行 `cargo check --manifest-path rust_ext/Cargo.toml --bin cw --no-default-features`，不要凭旧 windows-sys 版本的导入路径猜测。

37. **Cargo test 只接受一个测试过滤器**：`cargo test --lib` 的位置参数只能有一个 `TESTNAME`，例如同时传 `daemon::dispatch::tests daemon::snapshot_state::tests` 会直接报 `unexpected argument`，测试不会运行。需要验证多个模块时分别执行多个命令，或使用一个共同的上层过滤器（如 `daemon::`），并记录每次结果。

### 6.6 PyInstaller 与发布验收

27. **GitHub Release 大资产先探测再下载**：当前网络到 `release-assets.githubusercontent.com` 可能出现 HEAD 正常、GET 长时间近零速的情况。下载几十 MB 以上资产前先用 `curl.exe -I -L --max-time 30 <URL>` 验证重定向和长度，再用短时 GET 观察实际吞吐；连续低速时立即停止残留 `curl.exe`，改用 BITS、GitHub API 或 CI 生成的内容清单，不要让多个大文件并行占满工具超时窗口。

29. **PyInstaller 发布验收必须实例化 MCP Server**：`cw --version` / `cw --help` 只覆盖 CLI 启动，不能发现 FastMCP 间接导入缺失。修改 hidden imports 或 excludes 后，必须对冻结产物运行 `cw server --check-imports`；该命令会注册全部 MCP 工具但不写数据库、不下载 Semgrep 规则。遇到缺失模块时输出原始 `ImportError`，只恢复真实依赖并重建验证，禁止为了省事恢复 `collect_submodules('fastmcp')` 或全量云 SDK。标准库也可能是框架间接依赖，例如 `mimetypes` 被 FastMCP 资源层使用，不得仅凭 Call Warden 源码无直接 import 就排除。

30. **PyInstaller 排除包前必须审计生产顶层导入**：`Analysis.excludes` 只阻止模块进入冻结产物，不会自动消除生产代码中的 `from package import ...`。若仍有顶层导入，被排除的包会在最早入口直接触发 `ModuleNotFoundError`，即使 PyInstaller 构建和静态 inspector 都通过。删除或外置依赖时，先用 `rg` 追踪所有生产 import，把可选依赖改为使用点懒加载或拆到不参与冻结入口的模块；构建后必须实际运行 `cw --version`、`cw --help`、`cw server --check-imports`，三者任一失败都不得发布。静态模块清单和单元测试不能替代冻结可执行文件 smoke。

### 6.7 任务编排与工具调用

10. **并行调用必须容忍预期的非零退出**：`rg` 未找到内容、探测可选模块不存在等预期情况会返回非零，这不是执行故障；若把这类命令直接放进 `Promise.all`，一个分支会中止整组调用并丢失其他结果。并行脚本应在每个分支捕获结果，或在 PowerShell 中把预期的非零状态显式转换为成功，例如 `rg -n -e "pattern" path; if ($LASTEXITCODE -eq 1) { exit 0 }`。无法方便转换时单独执行该探测；只有非预期的非零状态才按真正错误处理。

11. **后台 watcher 用长运行 exec cell 承载**：在桌面工具执行器中，`Start-Process` 启动的后代进程可能被持续跟踪，即使重定向标准输出仍会使父工具调用超时。启动 `cw --watch` 时直接运行长命令并保留返回的 cell id，开发结束后显式终止该 cell；不要用 `Start-Process` 脱离。

12. **`cw task create` 不支持 `--parent` 参数**：CLI 的 `cw task create` 只有 `--title`/`--desc`/`--steps` 三个参数。挂载子任务必须用 Python API `db.task_create(title=..., description=..., parent_id=..., steps=[])`，模板见 [docs/task_create_subtask.py](docs/task_create_subtask.py)。参数清单见 [TOOLS.md](TOOLS.md)，不确定时先 `cw task <subcommand> --help`。

15. **读取测试文件前先确认真实路径**：不要根据功能名连续猜测 `tests/test_xxx.py`。先运行 `rg --files tests | rg "关键词"` 或 `rg -l -g "test_*.py" "符号名" tests`，再对实际返回的路径使用 `Get-Content` / `cw file`。缺失的候选文件不是检索失败，不应让并行读取整组中止。

18. **连续修改同一文件前刷新补丁上下文**：前一个 `apply_patch` 可能已删除、移动或格式化后续补丁依赖的锚点，继续使用旧上下文会触发 `PatchContextMismatch`。对同一文件分阶段修改时，先用 `rg -n` 或读取目标局部确认当前内容，再生成小范围补丁；不要复用前一轮读取到的 import 或函数上下文。

### 6.8 代码质量与测试规范

13. **合成数据压测 ≠ 真实 E2E**（方法论教训）：用 `generate_data()` 一次性生成全部合成数据到内存再批量入库，会挤压系统页缓存、未覆盖解析/CAS/watcher/daemon、多规模并行互相干扰。正确做法：流式生成、串行运行取中位数、分开报告 storage_build_time 和 end_to_end_time、记录硬件型号。不要发展"内存主表+SQLite 从表"架构，Call Warden 走混合架构：SQLite/CAS 持久化真相，Rust GraphStore/CSR 内存查询，daemon 共享发布。

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

    **违反示例**：新增了 3 个 MCP 工具但未更新 `docs/mcp_tools.md`，导致文档说 226 个实际 229 个 → 禁止。
    **正确示例**：新增 MCP 工具时，同一次 commit 更新 `docs/mcp_tools.md` 头部数字 + 工具列表 + `README.md` 中的数字。

35. **业务测试不得只断言单一自然语言错误文本**：DB 层部分历史错误仍是硬编码中文，`CALLWARDEN_LANG=en_US` 只影响 i18n 层，无法切换这些字符串。测试拒绝路径时应优先断言结构化状态、错误码和数据库不变；确需检查文本时使用中英文语义关键词集合，不得仅靠 `parent`/`manual` 等英文子串判定业务是否正确。

### 6.9 审计缺陷修复与并行执行

39. **审计发现的挂载三原则**：Code Review / 审计发现的缺陷按以下方式挂载，禁止混淆：
    - **挂回原任务**：能定位到"声称完成该功能的原任务"的缺陷，用 `cw task reopen <task_id> --reason "<缺陷证据>"` 挂回原任务（reason 写清函数名、行号、实证），修完重验再关闭（AGENTS.md 规则 7）；不得新建平行任务掩盖原任务的假关闭。
    - **直接执行原任务单**：计划里本就存在但从未执行的工作（pending/open 任务，如官方计划 10.x、G4、根任务 verify），直接按原任务执行并补真实证据，不新建。
    - **新建独立任务单**：跨任务或先前阶段遗留的维护（FK 夹具修复、MCP 静态扫描测试适配、schema 版本断言同步、migration-manifest 更新等），新建任务单并在描述中写死所有权白名单。
40. **并行执行的判据是所有权文件不相交**："同一 wave 不得并行编辑同一文件"是文件级约束，不是任务级约束。并行批次挂载时：
    - 每个子任务描述写清**所有权白名单**（唯一可改文件列表）与**并行组号**；
    - 两任务所有权文件相交即不能并行（拆分所有权或串行），不能因"不同 agent"就默认安全；
    - 文件不相交的并行组可同时开工；跨组依赖单独声明（如"verify 依赖某组完成"）。
41. **修复批次开工前，并行 agent 必须对共享在途文件 checkpoint**：共享文件（如 `server/tools/*`、`db/db_task_leases.py`）被其他 agent 在途编辑时，先要求其提交/停手并确认文件 mtime 稳定，再领取对应任务执行；发现双写冲突立即停下报告，不得自行覆盖对方改动。

48. **共享 git 对象库保护（多 agent 并行的硬性纪律，2026-08-28 事故后立规）**：本仓库由多个 agent 并行开发、共享同一个 `.git`。**严禁执行 `git gc --prune=now` / `git prune` / `git filter-repo` / `git gc --aggressive` / `git repack -ad`**——它们会删除仓库中"当前无引用"的对象，而并行 agent 的暂存、worktree refs、checkpoint refs 正引用这些对象，一旦被删会导致整个仓库 `git status`/`git add`/`git commit`/`git write-tree` 报 `unable to read tree`、`git log --all` 报 `Failed to traverse parents`、`git fsck` 报大量 `missing blob/tree/commit`，所有 agent 都无法提交（2026-08-28 实发：回收站恢复约 440 个对象才救回仓库）。

    - **唯一允许的清理**：`git gc --prune=2.weeks.ago`（保守保留期），且执行前必须先把 `.git/objects`、`.git/index`、`.git/refs` 备份到 `~/.callwarden/git_recovery_<date>/`；
    - **任何 `git gc` 前先确认没有其他 agent 正在操作**（`tasklist | grep git`，或确认 `refs/codex/turn-diffs/*`、worktree refs 不再增长）；
    - **commit 前检查 index**：若 index 含其他 agent 的暂存内容（`git status --short` 出现大量不属于本任务的 `M`/`D`/`??`），`git commit` 会**一次提交整个 index**。必须先 `git reset` 清空暂存区（工作树不变），只 `git add` 本任务白名单路径，再 `git commit`；严禁 `git add .` / `git commit -a` 吸收并行改动；
    - **事故恢复路径**（已沉淀，见 `deliverables/software-company/git_recovery_record_20260828.md`）：被 prune 的 loose objects 会进 Windows 回收站，可恢复——解析 `$Recycle.Bin\<SID>\$I*`（offset 28 起 UTF-16LE 原始路径）筛 `.git\objects\`，对 `$R*` 内容做 `zlib 解压 + sha1(header+content)==对象名` 指纹匹配，迭代 `git fsck --full` 写回 `.git/objects/xx/yyy`；
    - **失败留痕**：任何 `git gc`/`prune`/commit 事故必须在当日 memory 日志与任务 report 中记录，禁止静默跳过。

## 文档索引

| 文档                                                       | 说明                                     |
| ---------------------------------------------------------- | ---------------------------------------- |
| [TOOLS.md](TOOLS.md)                                       | 工具使用指南（CLI/MCP/场景映射）         |
| [README.md](README.md)                                     | 项目首页                                 |
| [docs/quickstart.md](docs/quickstart.md)                   | 快速开始                                 |
| [docs/cli_reference.md](docs/cli_reference.md)             | CLI 命令参考                             |
| [docs/mcp_tools.md](docs/mcp_tools.md)                     | MCP 工具参考                             |
| [docs/architecture.md](docs/architecture.md)               | 架构设计                                 |
| [docs/deployment.md](docs/deployment.md)                   | 部署指南                                 |
| [docs/agent-usage-guide.md](docs/agent-usage-guide.md)     | 面向 AI Agent 的使用指南（从本文件抽取） |
| [docs/task_create_subtask.py](docs/task_create_subtask.py) | 挂载子任务的标准脚本模板                 |
| [CONTRIBUTING.md](CONTRIBUTING.md)                         | 贡献指南                                 |
| [CHANGELOG.md](CHANGELOG.md)                               | 版本变更                                 |


## Call Warden 自动沉淀规则

<!-- CALLWARDEN_RULES_START -->
<!-- 自动同步区域，请通过 cw rule sync 更新，不要手改 -->
<!-- CALLWARDEN_RULES_END -->
