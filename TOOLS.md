# TOOLS.md — Call Warden 工具使用指南

> 本文件是 AGENTS.md 的工具参考附录，不在每次会话中强制加载。
> AGENTS.md 引用本文件用于查询具体命令参数和场景映射。

## 场景 → 命令映射（Agent 优先用 cw 的场景）

| 场景 | cw 命令 | 为什么不用 Grep/Read |
|------|---------|---------------------|
| 查符号定义 | `cw --symbol <QN>` | 精确返回符号内容，不含无关代码 |
| 找调用方 | `cw --callers <QN>` | Grep 误匹配注释/字符串/同名函数 |
| 找被调用方 | `cw --callees <QN>` | 同上 |
| 调用链 | `cw --call-chain <QN>` | 图遍历，Grep 做不到 |
| 变更影响 | `cw --impact <QN>` | blast radius，独有能力 |
| 符号搜索 | `cw --search <Q>` | 结构化结果，含符号类型/位置 |
| 符号静态检查 | `cw issues <QN>` | 整合 Semgrep + Guardrail findings，按符号聚合 |
| 符号测试 case | `cw tests <QN>` | 回答"foo() 有哪些 test 在测它"；`--build` 重建关联；`--reverse` 反向查询；`--history` 查测试稳定性；`--import` 导入 JUnit XML |
| 代码重复检测 | `cw clone detect` + `cw clone list --symbol <QN>` | 按符号查重复代码；Type-1/2/3 克隆检测 |
| 变更-缺陷关联 | `cw evolution <QN> --defects` | 变更频率 vs 缺陷关联（change_count / defect_count / defect_rate）|
| 带符号上下文的文本搜索 | `cw grep <pattern> [--fixed] [--limit N]` | 每行带 `[in fn xxx]` 标注，agent 一眼看出匹配行属于哪个函数；rg 只给 file:line:content |
| 编辑前检查 | `cw guardrail scan` | 安全规则匹配 |
| 改后刷新 | `cw --refresh <file>` | 保持数据库同步 |

## 可以用自带工具的场景

| 场景 | 工具 | 理由 |
|------|------|------|
| 读文件全文 | Read | cw --file 也是返回全文 |
| 浏览目录 | Glob/LS | cw 无目录浏览命令 |
| 编辑文件 | Edit | cw 无编辑命令 |
| 运行命令 | RunCommand | cw 无此能力 |

## CLI 命令速查（cw）

```bash
# 安装依赖
cw install            # 核心依赖
cw install --all      # 全部依赖（含 semgrep / 向量搜索）

# 初始化与构建
cw --refresh-all      # 增量刷新代码图谱（仅解析变更文件）
cw --refresh-all --force  # 强制全量重新解析
cw --refresh <file>   # 刷新单个文件

# 查询
cw --search "login"            # 搜索符号
cw --call-chain "module::fn"   # 查看调用链
cw --callers "module::fn"      # 调用方
cw --callees "module::fn"       # 被调用方
cw --symbol <QN>                # 查符号定义内容（含 calls_out/called_by/issues 前5条）
cw issues <QN>                  # 查符号的静态检查问题（Semgrep + Guardrail findings）
cw issues <QN> --include-info   # 包含 INFO 级别（默认只 WARNING+）
cw tests <QN>                   # 查符号的测试 case 列表（按 confidence 降序）
cw tests <QN> --reverse         # 反向：查 test_fn 测了哪些函数
cw tests --build                # 全量重建测试关联表（refresh 测试文件后调用）
cw tests --build --force        # 强制全量重建（清空已有关联）
cw tests <QN> --history         # 查符号关联测试的运行历史与稳定性（pass_rate / failures / by_test）
cw tests --import <junit.xml>   # 导入 JUnit XML 测试运行结果（pytest --junitxml 生成）
cw tests --import <file> --ci-run-id ID --ci-url URL  # 关联 CI 运行信息
# 代码重复检测
cw clone detect                 # 检测 Type-1/2/3 克隆（结果存 clone_pairs 表）
cw clone detect --min-lines 10 --similarity 0.7  # 自定义阈值
cw clone list --type 1 --limit 20               # 列出 Type-1 克隆
cw clone list --symbol <QN>                     # 查某符号的重复代码
cw clone stats                   # 克隆统计
cw clone clear                   # 清空检测结果
# 变更-缺陷关联
cw evolution <QN>               # 函数变更频率（时间窗口、变更者、时间线）
cw evolution <QN> --defects     # 变更-缺陷关联（change_count / defect_count / defect_rate / recent_defects）
cw --stats                      # 统计信息
cw --status                     # 完整状态概览

# 带符号上下文的文本搜索（差异化工具）
cw grep daemon_handle_refresh              # 默认正则模式，limit=200，默认只显示有符号归属的行
cw grep daemon_handle_refresh --fixed      # 固定字符串模式（避免正则转义）
cw grep daemon_handle_refresh --include-all  # 显示全部匹配（含 import/文档/注释等无符号行）
cw grep import time --fixed                # 多关键词 AND：找同时含 "import" 和 "time" 的行
cw grep "import time" --fixed              # 单 pattern：找含 "import time" 连续子串的行（引号包住）
cw grep TODO --limit 20                    # 限制结果数（已过滤无符号行后再截断）
cw grep TODO --path server                 # 限定搜索目录
# 输出格式：file:line [in fn qualified_name] content
# 默认过滤 [no symbol] 行（agent 要有效信息，不要文档/import 噪音）
# --include-all 时才显示 [no symbol] 行（如 import/顶层语句/文档）
# 多关键词 AND：空格分隔，每行必须同时含所有关键词；引号包住含空格的字符串作为单 pattern

# MCP Server
cw server              # 启动 MCP Server（stdio 模式）
cw server --transport sse  # SSE 模式

# 安全护栏
cw guardrail scan      # 扫描安全规则
cw guardrail list      # 列出规则

# Daemon
cw daemon serve        # 启动 daemon
cw daemon ping         # 测试 daemon 连通性
cw daemon status       # daemon 状态

# 其他
cw test <module>       # 运行测试
cw gc archive          # 归档被 ignore 命中的文件
```

## cw task 子命令参数参考（权威，与代码同步）

> 以下参数清单通过 `cw task <subcommand> --help` 校验，是**唯一权威参数源**。
> CLI 与 MCP 的 task_create/task_next_step 等方法名不同：CLI 是 `cw task create`，Python API 是 `db.task_create()`。

### 写命令（需 workspace 激活，可能撞锁）

```bash
# 创建根任务（不支持 --parent，要挂子任务见下方 Python API）
cw task create --title "任务标题" [--desc "描述"] [--steps '[{"action":"annotate","target_file":"a.py"}]']

# 认领当前 pending step
cw task next <task_id>

# 报告 step 结果
cw task report <task_id> <step_id> [--result "结果描述"] [--fail]

# 回滚 step 变更
cw task rollback <task_id> <step_id>

# 审核通过（review → applied，由另一个会话的 LLM 调用）
cw task apply <task_id> [--reviewer <审核人>]

# 关闭任务（applied → closed，由另一个会话的 LLM 调用）
cw task close <task_id> [--reviewer <审核人>]

# 重开任务（review/applied/closed → in_progress，用于 code review 发现问题）
cw task reopen <task_id> [--reviewer <审核人>] [--reason "重开原因"]

# 从 Markdown 计划拆分子任务
cw task split --plan <plan.md> <parent_task_id>

# 捕获外部 agent 的文件变更到任务/审计闭环
cw task capture-diff [<task_id>] [--auto] [--dry-run]

# 解决质量门禁 finding
cw task resolve-finding <finding_id> [--resolution <解决方案>]

# 任务完成质量审核（check_gate + 5 个扩展检查器）
cw task completion-review <task_id>
```

### 只读命令（跳过 workspace 激活，不撞锁）

```bash
cw task list [--blocked] [--status <S>] [--limit <N>] [--flat]
cw task show <task_id> [--flat]
cw task status-tree <task_id>           # task show 的 tree 模式别名
cw task findings <task_id> [--status <S>] [--severity <S>]
```

### Python API（用于脚本批量操作或挂载子任务）

```python
from callwarden.db.db import CodeGraphDB
db = CodeGraphDB()

# 创建根任务
task_id = db.task_create(title="任务标题", description="描述", steps=[...])

# 创建子任务（CLI 不支持 --parent，必须用 Python API）
subtask_id = db.task_create(
    title="子任务标题",
    description="描述",
    parent_id="<父任务 task_id>",   # ← 挂载到父任务
    steps=[],
)

# 无 steps 的任务直接用 SQL 更新状态到 closed
# （task_apply/task_close 要求先走 task_next_step 流程）
import time
now = time.time()
db.conn.execute(
    "UPDATE tasks SET status = ?, applied_at = ?, closed_at = ? WHERE id = ?",
    ("closed", now, now, task_id),
)
db.conn.commit()
```

**脚本模板**：[docs/task_create_subtask.py](docs/task_create_subtask.py) — 挂载子任务的标准脚本

## 只读/写命令分类

### 只读命令（跳过 workspace 激活，不撞锁）

- **子命令**：`task list/show/findings/status-tree`、`rule list/candidate/applicable/extract`、`doctor`、`check-gate`、`test-impact`、`hotspot`、`churn`、`evolution`、`impact`、`review`、`vuln-blast`、`symbol-history`、`guardrail scan/rules`、`defect stats/list/show`、`gc list/inspect`、`issues`、`tests`
- **flag**：`--search`、`--symbol`、`--call-chain`、`--callers`、`--callees`、`--topo`、`--file`、`--history`、`--diff`、`--changes`、`--comment-coverage`、`--stats`、`--status`、`--query`、`--top-callers`、`--orphan-symbols`、`--deepest`、`--module-calls`、`--detect-cycles`、`--export-module-graph`、`--call-heatmap`、`--impact`、`--uncommented`、`--who`、`--ownership-map`

### 写命令（需激活 workspace，可能撞锁）

- **子命令**：`task create/next/report/apply/close/rollback/reopen/capture-diff/resolve-finding/completion-review/split`、`rule sync/insert-block`、`defect import/add`、`gc archive/import`
- **flag**：`--refresh-all`、`--refresh`、`--watch`、`--register-workspace`、`--set-workspace`、`--delete-workspace`、`--restore-comment`、`--restore-all-comments`、`--coverage-import`

## MCP 工具分组（120+ 个）

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

## A/B 对比评估数据（cw vs Grep）

> 基于 `tests/_bench_cw_vs_grep.py` 在 callwarden 自身（6113 符号、10079 调用边）上
> 跑出的实测数据，10 个函数 × 6 个场景 × 3 次取中位数。

### 场景必要性结论

| 场景 | cw vs Grep | 必要性 | 说明 |
|------|-----------|--------|------|
| **call-chain** | cw 独有 | **强制 cw** | Grep 无法做图遍历 |
| **impact** | cw 独有 | **强制 cw** | Grep 无法算 blast radius |
| **callers** | cw token 节省 87% | **强制 cw** | Grep 误匹配主要来自文档提及 |
| **callees** | cw token 节省 98% | **强制 cw** | Grep 无法限定在函数体内 |
| **grep** | cw 每行带 `[in fn xxx]` | **优先 cw** | 知道匹配行属于哪个函数，省去读上下文；rg 只给原始 file:line:content |
| **symbol** | cw token 多 100% | **优先 cw** | cw 含 calls_out/called_by/comment，信息密度高 |

### 性能对比的关键澄清（重要）

A/B 脚本测出 "cw 慢于 Grep 1.3-1.8 倍" 是 **CLI 模式固有启动开销**导致的假象，
不是查询本身慢。用 `tests/_bench_query_cost.py` 拆解 cw CLI 一次调用的耗时构成：

| 阶段 | 耗时 | 占比 |
|------|------|------|
| import 模块（含 numpy/parsers/watchdog） | ~190 ms | 83% |
| init db（SQLite 连接 + WAL 加载） | ~6 ms | 3% |
| 实际查询（callers / symbol） | 1-2 ms | <1% |

换算到三种部署模式的单次查询成本：

| 模式 | 单次查询成本 | 对比 Grep (~100ms) |
|------|------------|------------------|
| cw CLI（每次重启 Python） | ~200 ms | 慢 2x（启动开销主导） |
| **cw daemon / MCP（常驻）** | **~0.3 ms** | **快 ~300 倍** |
| Grep (rg)（Rust 二进制） | ~100 ms | 基准 |

**结论**：真正公平的对比是 cw daemon vs Grep，那时 cw 比 Grep 快约 300 倍。
CLI 模式的"慢"是 Python 解释器启动 + 模块导入的固定成本，与查询逻辑无关。
建议在生产环境启用 daemon 或 MCP 常驻模式以发挥 cw 的真实性能。

### 已知 bug（待根治）

- **`call_versions.callee_qualified` 字段长期为空**：解析器只填充 `callee_name`（短名如
  `raw.startswith`），未解析 `callee_qualified`（限定名）。当前 `db_query.get_symbol()`
  SQL 已用 `COALESCE(NULLIF(cv.callee_qualified, ''), cv.callee_name)` fallback 到短名，
  保证 UI 可用，但仍是数据层面的不完整。修复方向：解析器在写 call_versions 时填充
  `callee_qualified` 字段（需多文件符号解析后回填）。

### 脚本与报告

- **A/B 测试脚本**：[tests/_bench_cw_vs_grep.py](tests/_bench_cw_vs_grep.py)
  - 10 函数 × 5 场景 × N 次重复，取中位数
  - 内置 Grep 误匹配采样分析（按 .py/.md 分类）
  - 用法：`python tests/_bench_cw_vs_grep.py --runs 5`
- **最新报告**：[tests/_bench_cw_vs_grep_report.md](tests/_bench_cw_vs_grep_report.md)
- **原始数据**：[tests/_bench_cw_vs_grep_report_raw.json](tests/_bench_cw_vs_grep_report_raw.json)
