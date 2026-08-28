# TOOLS.md — Call Warden 工具使用指南

> 本文件是 AGENTS.md 的工具参考附录，不在每次会话中强制加载。
> AGENTS.md 引用本文件用于查询具体命令参数和场景映射。

## 场景 → 命令映射（Agent 优先用 cw 的场景）

按 8 类能力维度组织。每个场景的 CLI 命令在 MCP 激活后也有对应 MCP 工具（见 [docs/mcp_tools.md](docs/mcp_tools.md)）。

### 1. 符号基本属性（symbols 表）

| 场景 | cw 命令 | 为什么不用 Grep/Read |
|------|---------|---------------------|
| 查符号定义 | `cw --symbol <QN>` | 精确返回符号内容（含 calls_out/called_by/issues 前 5 条），不含无关代码 |
| 符号搜索 | `cw --search <Q>` | 结构化结果，含符号类型/位置 |
| 精确查询位置 | `cw --query <NAME> <FILE>` | 比 Grep 精确（按符号名+文件，不误匹配字符串/注释）|
| 文件内符号列表 | `cw --file <PATH>` | 结构化列出文件的所有符号（含签名/类型/行号）|

### 2. 代码度量（db_metrics.py）

| 场景 | cw 命令 | 为什么不用 Grep/Read |
|------|---------|---------------------|
| 度量汇总 | `cw --metrics` | 全项目符号数/调用边/文件数/平均行数等 |
| 圈复杂度热点 | `cw --complexity [N]` | 按复杂度排序，找出最复杂的 N 个函数 |
| 模块耦合度 | `cw --coupling` | 模块间调用统计，识别高耦合模块 |
| 最大函数 | `cw --largest-fns [N]` | 按行数排序的 N 个最大函数 |
| 高耦合函数 | `cw --coupled-fns [N]` | 按调用关系数排序的 N 个高耦合函数 |
| 单函数度量 | `cw --fn-metrics <NAME>` | 指定函数的详细度量（行数/复杂度/调用数/被调用数）|

### 3. 调用关系 / 爆炸半径（db_impact.py）

| 场景 | cw 命令 | 为什么不用 Grep/Read |
|------|---------|---------------------|
| 找调用方 | `cw --callers <QN>` | Grep 误匹配注释/字符串/同名函数 |
| 找被调用方 | `cw --callees <QN>` | 同上 |
| 调用链 | `cw --call-chain <QN>` | 图遍历，Grep 做不到 |
| 变更影响（向上爆炸半径）| `cw --impact <QN>` | blast radius，独有能力 |
| 拓扑排序 | `cw --topo` | 调用图拓扑序，Grep 做不到 |
| 循环调用检测 | `cw --detect-cycles` | 调用图环检测 |
| 模块间调用统计 | `cw --module-calls [N]` | 跨模块调用热力图 |
| 调用频率热力图 | `cw --call-heatmap [GROUP]` | 按模块/文件聚合的调用频率 |
| 孤立符号 | `cw --orphan-symbols [KIND]` | 无调用方/被调用方的符号 |
| 调用深度最深 | `cw --deepest [N]` | 调用链最深的 N 个函数 |
| 跨层影响 | `cw defect cross-layer` | 跨层（API/Service/DAO）影响传播 |

### 4. 覆盖率（db_coverage.py）

| 场景 | cw 命令 | 为什么不用 Grep/Read |
|------|---------|---------------------|
| 注释覆盖率 | `cw --comment-coverage` | 全项目注释覆盖率统计 |
| 无注释符号 | `cw --uncommented` | 列出没有注释的符号 |
| 测试覆盖率 | `cw --test-coverage` | 全项目测试覆盖率统计 |
| 导入覆盖率 | `cw coverage import <file>` | 导入 lcov/jacoco 覆盖率报告 |
| 函数覆盖率 | `cw coverage fn <QN>` | 指定函数的覆盖率详情 |
| 未覆盖函数 | `cw coverage uncovered` | 列出未被测试覆盖的函数 |
| 测试影响选择 | `cw test-impact <QN>` | 改了该函数后需要运行的测试列表 |
| 谁最懂这个符号 | `cw who <QN>` | 按 git blame + CODEOWNERS 推断负责人 |
| 所有权映射 | `cw ownership-map` | 符号 → 文件 → 负责人的映射 |

### 5. Git 历史 / 演化智能（db_git.py + db_evolution.py）

| 场景 | cw 命令 | 为什么不用 Grep/Read |
|------|---------|---------------------|
| 导入 git 历史 | `cw git import` | 把 commit log 结构化入库 |
| commit 历史 | `cw git log [--author X] [--since Y]` | 按条件查询 commit |
| commit 详情 | `cw git show <hash>` | 单个 commit 的文件变更 |
| git 统计 | `cw git stats` | 提交者/文件/时间段统计 |
| 符号 commit 历史 | `cw symbol-history <hash>` | 单符号的 commit 时间线 |
| 函数变更频率 | `cw evolution <QN>` | 变更次数/变更者/时间线（按时间窗口）|
| 热点函数排名 | `cw hotspot` | 变更次数 + 缺陷数 + 复杂度综合排名 |
| 代码流失分析 | `cw churn [--window 90d]` | 按时间窗口的代码增删统计 |

### 6. 静态检查（Semgrep + Guardrail + issues + tests + clone + defects）

| 场景 | cw 命令 | 为什么不用 Grep/Read |
|------|---------|---------------------|
| 符号静态检查 | `cw issues <QN>` | 整合 Semgrep + Guardrail findings，按符号聚合（行范围交集）|
| 符号测试 case | `cw tests <QN>` | test_fn ↔ tested_fn 三阶推断（direct_call > name_convention > indirect）|
| 反向测试查询 | `cw tests <QN> --reverse` | test_fn 测了哪些被测函数 |
| 测试覆盖摘要 | `cw tests <QN> --coverage` | has_tests / test_count / high_confidence_count |
| 测试稳定性 | `cw tests <QN> --history` | 基于 test_runs 历史的 pass_rate / recent_failures |
| 导入 JUnit XML | `cw tests --import <file>` | 解析 pytest --junitxml 输出，关联 test_fn |
| 重建测试关联 | `cw tests --build [--force]` | refresh 测试文件后重建 test_case_relations |
| 代码重复检测 | `cw clone detect [--min-lines N] [--similarity F]` | Type-1/2/3 克隆检测（MinHash + LSH + token 归一化）|
| 按符号查重复 | `cw clone list --symbol <QN>` | 查指定符号的重复代码对 |
| 列出克隆 | `cw clone list [--type 1] [--limit N]` | 按类型/相似度过滤克隆列表 |
| 克隆统计 | `cw clone stats` | 克隆对数量 / 影响文件数 / 类型分布 |
| 变更-缺陷关联 | `cw evolution <QN> --defects` | 变更频率 vs 缺陷关联（change_count / defect_count / defect_rate / recent_defects）|
| Semgrep 扫描 | `cw semgrep scan [PATH...]` | 扫描指定路径，findings 入库 |
| 函数缺陷检测 | `cw function-issues [FN]` | 按函数聚合 Semgrep findings |
| 缺陷知识库搜索 | `cw defect search <pattern>` | 按模式搜历史缺陷知识 |
| 漏洞爆炸半径 | `cw vuln-blast <vuln_id>` | 从漏洞点到调用方的反向影响 |
| 安全护栏扫描 | `cw guardrail scan` | 编辑前的安全规则匹配 |

### 7. 注释恢复（db_comment.py）

| 场景 | cw 命令 | 为什么不用 Grep/Read |
|------|---------|---------------------|
| 恢复函数注释 | `cw --restore-comment <SPEC>` | 从历史版本恢复函数的中文注释 |
| 批量恢复注释 | `cw --restore-all-comments` | 全项目扫描无注释符号，从 git 历史恢复 |
| 恢复文件版本 | `cw --restore-file <PATH>` | 从指定 hash 恢复文件内容 |
| 函数历史版本 | `cw --history <NAME>` | 函数的所有历史版本列表 |
| 版本对比 | `cw --diff <H1> <H2>` | 对比两个版本的内容差异 |
| 从版本查注释 | `cw symbol comment-from-version <QN> <hash>` | 从指定 commit 的版本提取注释 |

### 8. 编辑前检查与刷新

| 场景 | cw 命令 | 为什么不用 Grep/Read |
|------|---------|---------------------|
| 带符号上下文的文本搜索 | `cw grep <pattern> [--fixed] [--limit N] [--include-all]` | 每行带 `[in fn xxx]` 标注，agent 一眼看出匹配行属于哪个函数；rg 只给 file:line:content |
| 编辑前检查 | `cw guardrail scan` | 安全规则匹配 |
| 编辑前符号契约 | `cw guardrail check-edit` | 符号级 Before-Edit Contract 校验 |
| 改后刷新 | `cw --refresh <file>` | 保持数据库同步 |
| 全量刷新 | `cw --refresh-all` | 增量刷新代码图谱 |
| 强制全量刷新 | `cw --refresh-all --force` | 重新解析所有文件 |

## 9. 阶段收口与共享 Runtime 刷新（Windows）

阶段代码和测试通过后，使用 `scripts/refresh_shared_runtime.ps1` 统一切换
Windows daemon/CLI 二进制，避免测试或 Reviewer 继续连接旧的 `cw-daemon.exe`。

```powershell
# 独立 target 编译 release，安装、启动并 ping 新 daemon
pwsh -File .\scripts\refresh_shared_runtime.ps1 `
  -TaskId T-... `
  -RunSmokeTests

# 同时停止仓库内 Call Warden MCP Server；MCP 不由脚本启动，交给 IDE supervisor 重连
pwsh -File .\scripts\refresh_shared_runtime.ps1 `
  -TaskId T-... `
  -RestartMcp `
  -RunSmokeTests

# 同时启动 Windows bridge，供 WSL MCP/CLI 访问 Windows authority
pwsh -File .\scripts\refresh_shared_runtime.ps1 `
  -TaskId T-... `
  -StartBridge `
  -RestartBridge `
  -RunSmokeTests
```

脚本边界：

- 先在独立 `rust_ext/target/stage-refresh` 编译；构建失败不会停止现有服务。
- 只匹配仓库路径下的 `cw-daemon.exe` 和 `cw.py server`，拒绝按名称杀任意 Python/daemon 进程。
- 安装到 `%USERPROFILE%\.callwarden\runtime\current`，上一版本保留用于失败回滚。
- 启动后等待真实 `cw daemon ping` 成功，并记录产物 SHA-256、Git HEAD、PID 和测试结果。
- `refresh_shared_runtime.ps1` 强制使用 `C:\Python314\python.exe` 作为 `PYTHON`/
  `PYO3_PYTHON`，并用 `dumpbin /dependents` 拒绝导入 `python310.dll`、`python311.dll`
  等非 `python314.dll` 的 daemon。纯 Rust/Python-free daemon 可以合法不导入 Python DLL；
  它仍必须记录该事实，不能把“无导入”误报为 Python 3.14 直接链接。
- ping 后还会核验运行 PID 的 executable path 和 SHA-256 均等于
  `%USERPROFILE%\.callwarden\runtime\current\cw-daemon.exe`。仅构建
  `rust_ext\target\debug`、仅重建 `target\release`、或仅修改源码，均不构成部署成功。
- 修改 Rust daemon/CLI/bridge 后，必须运行该脚本完成“release build → runtime/current
  install → 精确停止旧 runtime daemon → restart → hash/PID/ping/health evidence”闭环，
  再允许 Implementer 报告 `review` 或 Reviewer 采用 runtime 结果。
- `-StartBridge` 会探测 WSL 默认网关，启动受 token 保护的 `cw-bridge`，固定写入
  `%USERPROFILE%\.callwarden\bridge.manifest.json`、`bridge.token` 和
  `bridge.wsl.env`；WSL 侧使用 `source /mnt/c/Users/<user>/.callwarden/bridge.wsl.env`
  后，`cw daemon bridge` 必须返回 `ok=true`。
- WSL MCP 必须使用已安装 MCP SDK 的 venv，不能使用系统 `/usr/bin/python3`：
  `wsl.exe -d Ubuntu -- bash -lc "cd /mnt/c/git_work/callwarden && source /mnt/c/Users/<user>/.callwarden/bridge.wsl.env && exec /root/.callwarden/wsl-mcp-venv/bin/python cw.py server"`。
  该 venv 位于 WSL ext4，不与 Windows Python 或 Windows `callwarden_core` 混用。
- 成功后将 `CW_DAEMON_BIN` 写入用户环境变量，后续 MCP/Agent 可找到新二进制。
- 脚本不会启动 stdio MCP Server；IDE 负责重连 MCP。若 IDE 不自动重连，只需重启 MCP Server，不需要重启 Agent 会话。
- 失败时 fail-closed 并尝试恢复上一版本；禁止删除 `callwarden.db`、WAL/SHM 或直接写 SQLite。

这一步是运行时切换，不等于任务 `apply/close`。实现 Agent 只能报告到 `review`，正式收口仍由 Coordinator 和 Independent Reviewer 完成。

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

# 自动配置 AI 工具集成
cw setup               # 探测已安装的 AI 工具并配置 MCP 集成
cw setup --dry-run     # 仅探测不写入
cw setup --force       # 强制重新配置

# 安全护栏
cw guardrail scan      # 扫描安全规则
cw guardrail list      # 列出规则

# Daemon
cw daemon serve        # 启动 daemon
cw daemon ping         # 测试 daemon 连通性
cw daemon status <workspace_id|workspace_instance_id>  # workspace 状态

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
cw task capture-diff [<task_id>] [--auto] [--dry-run] [--source-commit-hash <HASH>]
# --source-commit-hash：填写后写入 task_symbol_changes.source_commit_hash（v35+），
#                       支持 get_task_commits / get_commit_tasks 三角关联查询。
#                       --auto 模式自动取当前 HEAD commit hash，无需手动指定。

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

- **子命令**：`task create/next/report/apply/close/rollback/reopen/capture-diff/resolve-finding/completion-review/split`、`rule sync/insert-block`、`defect import/add`、`gc archive/import`、`identity revoke`、`setup`
- **flag**：`--refresh-all`、`--refresh`、`--watch`、`--register-workspace`、`--set-workspace`、`--delete-workspace`、`--restore-comment`、`--restore-all-comments`、`--coverage-import`

## MCP 工具分组（237 个）

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

**Identity / Attestation（P3）**：record_action_identity、check_action_identity、get_action_identity、get_attestation_validity、list_attestation_revocations、register_attestation_revocation

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

---

## cw experiment（P0 盲评对照实验）

| 子命令 | 说明 | 读写 |
|--------|------|------|
| `experiment batch-create --seed N` | 创建批次 + 默认协议 + 锁定 | 写（JSON 配置） |
| `experiment batch-lock <id>` | 冻结协议 | 写 |
| `experiment batch-list` | 列出所有批次 | 只读 |
| `experiment toggle-set --scope S --value V` | 设置 P0 Stage_Toggle | 写 |
| `experiment toggle-show` | 显示解析后的 P0 开关 | 只读 |
| `experiment admit <task> <batch>` | 纳样（分组 + blind view + JSONL） | 写 |
| `experiment record-metrics <task> <batch>` | 记录 review 指标 | 写（JSONL） |
| `experiment record-verdict <task> <batch>` | 记录 verdict 变更 | 写（JSONL） |
| `experiment record-reveal <task> <batch>` | 记录 reveal 事件 | 写（JSONL） |
| `experiment record-invalid <task> <batch>` | 记录无效样本 | 写（JSONL） |
| `experiment record-incident <task> <batch>` | 记录披露/完整性事件 | 写（JSONL） |
| `experiment pause <batch> --trigger T` | 手动暂停批次 | 写 |
| `experiment report <batch>` | 汇总评估 + G0 决策 | 只读 |

所有子命令支持 `--json` 输出机器可读 JSON。失败路径输出 Structured_Reason，exit code 1。
所有记录标记 `non_product_evidence=True`（P0 实验，非产品 Evidence）。

## cw collab（多 LLM 契约协同治理写命令）

| 命令 | 说明 | 读写 |
|------|------|------|
| `collab publish --workspace PATH` | 发布 Envelope（snapshot.publish） | 写（daemon） |
| `collab verdict --verdict-id ID --decision D` | 提交 Verdict 并封存（verdict.submit） | 写（daemon） |
| `collab reveal --event-id ID --task-id ID` | 提交 Reveal_Event（reveal.submit） | 写（daemon） |
| `collab gate-trigger --gate-id ID --clause C --value V` | 触发 Gate 判定（gate.decide） | 写（daemon） |

所有操作经 Daemon_Endpoint 序列化点，不可绕过。daemon 不可用时 Governance_Write fail closed，
输出 Structured_Reason（E_GOVERNANCE_WRITE_DEGRADED + 平台恢复指引），exit code 1。

## 路由矩阵与收敛架构（T01/T05）

239 个 MCP 工具的路由矩阵（单一真相源）：
`deliverables/software-company/tool_migration_matrix.json`。
daemon 自描述接口 `GET /v1/meta/tools` 返回工具级
`{name, module, target_backend, rpc_method, op_class, batch, status}`；
一致性由 `scripts/verify_route_matrix.py`（239/239）+ `scripts/check_client_purity.py`
（0 业务 SQL）门禁。详见 `docs/design/rust-client-convergence-protocol.md` 与
`docs/design/cw-rust-client-convergence-migration-guide.md`。
