# CLI 命令参考

Call Warden CLI 提供两种命令风格：

1. **子命令风格**：`cw <subcommand> [options]`（12 个子命令，对应"代码守护者架构"四大支柱）
2. **Flag 风格**：`cw --flag [options]`（传统命令，覆盖构建/查询/编辑/度量等）

> 下文用 `cw` 作为命令前缀。

## 命令概览（按功能分组）

| 分组 | 命令 | 风格 | 说明 |
|------|------|------|------|
| **构建** | `--init` | flag | 构建/增量更新代码图谱 |
| | `--init --force` | flag | 强制全量重新解析 |
| | `--refresh <PATH>` | flag | 刷新单个文件 |
| | `--watch` | flag | 启动文件监控，自动增量更新 |
| | `--status` | flag | 查看图谱状态概览 |
| | `--stats` | flag | 查看统计信息（JSON） |
| **查询** | `--search <QUERY>` | flag | 模糊搜索符号 |
| | `--symbol <QN>` | flag | 查看符号详情 |
| | `--file <PATH>` | flag | 查看文件内符号 |
| | `--query <NAME> <FILE>` | flag | 精确查询符号位置 |
| | `--callers <NAME>` | flag | 查询调用者 |
| | `--callees <NAME>` | flag | 查询被调用者 |
| | `--topo` | flag | 拓扑排序 |
| **调用链** | `--impact <QN>` | flag | 向上追踪影响面 |
| | `--call-chain <QN>` | flag | 向下追踪调用链 |
| | `--top-callers [N]` | flag | 被调用最多排行 |
| | `--deepest [N]` | flag | 调用深度最深排行 |
| | `--detect-cycles` | flag | 检测循环调用 |
| | `--module-calls [N]` | flag | 模块间调用统计 |
| | `--call-heatmap [GROUP]` | flag | 调用频率热力图 |
| | `--orphan-symbols [KIND]` | flag | 孤立符号（未被调用） |
| **安全** | `vuln-blast` | sub | 漏洞爆炸半径分析 |
| | `guardrail scan/rules` | sub | 安全护栏扫描/规则 |
| | `--semgrep [PATH...]` | flag | Semgrep 扫描 |
| | `--semgrep-stats` | flag | Semgrep 统计 |
| | `--semgrep-list [FILTER]` | flag | Semgrep 缺陷列表 |
| **编辑** | `--restore-comment <SPEC>` | flag | 恢复函数注释 |
| | `--restore-all-comments` | flag | 批量恢复注释 |
| | `--preview` | flag | 预览模式（配合恢复） |
| | `--history <NAME>` | flag | 函数历史版本 |
| | `--diff <H1> <H2>` | flag | 对比两个版本 |
| **任务** | `task create/next/report/rollback` | sub | 任务管理 |
| | `--task-list` | flag | 列出任务 |
| | `--task-show <ID>` | flag | 查看任务详情 |
| | `check-gate <TASK_ID>` | sub | 检查门禁 |
| **度量** | `--metrics` | flag | 代码度量汇总 |
| | `--complexity [N]` | flag | 圈复杂度热点 |
| | `--coupling` | flag | 模块耦合度 |
| | `--largest-fns [N]` | flag | 代码行数最多函数 |
| | `--coupled-fns [N]` | flag | 耦合度最高函数 |
| | `--fn-metrics <NAME>` | flag | 单函数度量 |
| | `--comment-coverage` | flag | 注释覆盖率 |
| | `--test-coverage` | flag | 测试覆盖率 |
| **演化** | `evolution <QN>` | sub | 函数变更频率 |
| | `hotspot` | sub | 热点函数排名 |
| | `churn` | sub | 代码流失分析 |
| | `symbol-history <HASH>` | sub | 符号 Git 历史 |
| | `test-impact <QN>` | sub | 测试影响选择 |
| **影响** | `impact <HASH>` | sub | 变更影响半径 |
| | `review <HASH>` | sub | 审查就绪报告 |
| **缺陷** | `defect search/suggest/learn/stats/build` | sub | 缺陷知识库 |
| | `--function-issues [FN]` | flag | 函数缺陷检测 |
| | `--issue-summary` | flag | 缺陷汇总 |
| **跨仓库** | （通过 MCP 工具） | mcp | 跨仓库依赖/共享符号/影响 |
| **LSP** | （通过 MCP 工具） | mcp | hover/定义/引用/诊断/补全 |
| **Git** | `--git-import [N]` | flag | 导入 Git 历史 |
| | `--git-log [N]` | flag | Git commit 历史 |
| | `--git-show <COMMIT>` | flag | commit 变更详情 |
| | `--git-stats` | flag | Git 集成统计 |
| **向量** | `--semantic-search <QUERY>` | flag | 语义搜索 |
| | `--embed` | flag | 生成向量嵌入 |
| | `--embed-force` | flag | 强制重新嵌入 |
| | `--similar <NAME>` | flag | 查找相似函数 |
| **概览** | `--brief` | flag | 项目简报 |
| | `--map` | flag | 仓库模块依赖图 |
| **覆盖率** | `--coverage-import <FILE>` | flag | 导入覆盖率报告 |
| | `--coverage-fn <NAME>` | flag | 函数覆盖率 |
| | `--coverage-uncovered` | flag | 未覆盖函数 |
| **所有权** | `--who <FILE>` | flag | 文件负责人 |
| | `--ownership-map` | flag | 所有权映射 |
| **工作区** | `--list-workspaces` | flag | 列出工作区 |
| | `--register-workspace <NAME> <ROOT>` | flag | 注册工作区 |
| | `--set-workspace <ID_OR_NAME>` | flag | 切换工作区 |
| | `--delete-workspace <ID_OR_NAME>` | flag | 删除工作区 |
| **导出** | `--export-module-graph [FORMAT]` | flag | 导出模块依赖图 |
| | `--graph-output <FILE>` | flag | 输出到文件 |
| **安装** | `install` | sub | 一键级联安装依赖 |
| | `install --all` | sub | 安装全部依赖（含可选） |
| | `install --lang <LANG...>` | sub | 仅安装指定语言 grammar |
| | `install --check` | sub | 检查依赖状态 |

> `install` 是独立子命令，调用方式为 `cw install [options]`。

---

## 安装命令

### `install`：一键级联安装依赖

Call Warden 提供独立的安装器脚本，级联安装核心依赖 + 各语言 tree-sitter grammar + 可选依赖。

**调用方式**：`cw install [options]`

#### 默认安装

```bash
# 安装核心依赖 + 9 种已支持语言 + C# / Ruby 扩展语言
cw install
```

#### 安装全部依赖（含可选）

```bash
cw install --all
```

#### 按语言安装 grammar

```bash
# 仅安装 C# 和 Ruby 的 grammar
cw install --lang csharp ruby

# 仅安装 Rust 和 Python
cw install --lang rust python
```

支持的语言名：`rust` / `typescript` / `javascript` / `python` / `kotlin` / `go` / `java` / `c` / `cpp` / `csharp` / `ruby` / `php` / `swift` / `scala` / `hcl` / `elixir`

#### 检查依赖状态

```bash
cw install --check
```

输出示例：

```
--- 核心依赖 ---
  [OK]   tree-sitter                      AST 解析引擎
  [OK]   tree-sitter-languages            多语言 grammar 预编译包（备份方案）
  [OK]   fastmcp                          MCP Server 框架

--- 已支持语言 grammar（9 种） ---
  [OK]   tree-sitter-rust                 Rust grammar (rust)
  [OK]   tree-sitter-python               Python grammar (python)
  ...

--- P0 扩展语言 grammar（C# / Ruby） ---
  [OK]   tree-sitter-c-sharp              C# grammar（Semgrep 170+ Pro 规则） (csharp)
  [OK]   tree-sitter-ruby                 Ruby grammar（Semgrep 40+ Pro 规则） (ruby)
```

#### 命令行参数

| 参数 | 说明 |
|------|------|
| `--all` | 安装全部依赖（含可选依赖：semgrep / sentence-transformers / sqlite-vec / numpy） |
| `--lang <LANG...>` | 仅安装指定语言的 grammar（空格分隔多个语言名） |
| `--check` | 仅检查依赖状态，不安装 |
| `--no-optional` | 显式跳过可选依赖（默认行为） |
| `--verbose` | 显示详细安装日志（pip 输出） |

#### 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 全部成功 |
| 1 | 部分失败（查看输出中的 `[失败]` 行） |
| 2 | pip 不可用或网络错误 |

#### 设计原则

1. **级联安装**：核心 → 已支持语言 → 扩展语言 → 可选依赖
2. **失败不中断**：单个包安装失败只警告，继续安装其他包
3. **状态可见**：每个包安装前后打印状态（已安装 / 安装中 / 成功 / 失败）
4. **幂等**：重复运行不会出错，已安装的包会跳过

---

## 构建命令

### `--init`：构建代码图谱

```bash
# 增量构建（默认）
cg --init

# 强制全量重新解析
cg --init --force

# 指定工作区
cg --workspace /path/to/project --init
```

### `--refresh <PATH>`：刷新单个文件

```bash
cg --refresh src/payment/mod.rs
```

增量更新指定文件，重新解析符号和调用关系。

### `--watch`：文件监控

```bash
cg --watch
```

启动文件监控守护进程，文件变化时自动增量更新。按 `Ctrl+C` 停止。

### `--status`：查看状态

```bash
cg --status
```

显示工作区、文件分布、符号分布、调用关系统计、上次构建时间等。

### `--stats`：统计信息（JSON）

```bash
cg --stats
```

以 JSON 格式输出统计信息，便于脚本解析。

---

## 查询命令

### `--search <QUERY>`：符号搜索

```bash
cg --search "login"
cg --search "User" --search-kind class
cg --search "handle" --search-limit 20
```

| 参数 | 说明 |
|------|------|
| `--search <QUERY>` | 搜索关键词（模糊匹配） |
| `--search-kind <KIND>` | 类型过滤：fn/method/class/struct/enum/trait/interface |
| `--search-limit <N>` | 返回数量（默认 50） |

### `--symbol <QN>`：符号详情

```bash
cg --symbol "my_project::payment::process_payment"
```

显示符号的类型、深度、文件位置、签名、注释、调用关系（调用的函数 + 被谁调用）。

### `--file <PATH>`：文件内符号

```bash
cg --file src/payment/mod.rs
```

### `--query <NAME> <FILE>`：精确查询位置

```bash
cg --query process_payment src/payment/mod.rs
```

### `--callers <NAME>` / `--callees <NAME>`

```bash
# 谁调用了我
cg --callers process_payment

# 我调用了谁
cg --callees process_payment
```

### `--topo`：拓扑排序

```bash
cg --topo
cg --topo --topo-limit 100
```

按依赖深度排序，底层（被调用最多）在前。

---

## 调用链分析命令

### `--impact <QN>`：影响面分析（向上）

```bash
cg --impact "my_project::payment::process_payment"
cg --impact "my_project::payment::process_payment" --chain-depth 5
```

向上追踪所有调用该函数的上游函数，按层级显示。

### `--call-chain <QN>`：调用链向下

```bash
cg --call-chain "my_project::payment::process_payment"
```

### `--top-callers [N]`：被调用最多排行

```bash
cg --top-callers          # 默认 20
cg --top-callers 50
cg --top-callers --top-callers-module "src/api"
```

### `--deepest [N]`：调用深度最深

```bash
cg --deepest
cg --deepest 50
```

### `--detect-cycles`：循环调用检测

```bash
cg --detect-cycles
cg --detect-cycles --cycle-depth 15
```

### `--module-calls [N]`：模块间调用统计

```bash
cg --module-calls
```

### `--call-heatmap [GROUP]`：调用频率热力图

```bash
cg --call-heatmap              # 默认按 module
cg --call-heatmap file         # 按 file
cg --call-heatmap --heatmap-limit 30
```

### `--orphan-symbols [KIND]`：孤立符号

```bash
cg --orphan-symbols            # 默认 fn
cg --orphan-symbols struct
cg --orphan-symbols --orphan-module "src/legacy"
```

查找未被调用的孤立函数/结构体，适合清理死代码。

---

## 安全命令

### `vuln-blast`：漏洞爆炸半径（子命令）

```bash
cg vuln-blast
cg vuln-blast --finding-id 42
cg vuln-blast --severity ERROR --depth 5
```

| 参数 | 说明 |
|------|------|
| `--finding-id <N>` | 指定 Semgrep finding ID（默认扫描全部） |
| `--severity <SEV>` | 严重度过滤：ERROR/WARN/INFO |
| `--depth <N>` | 调用图反向遍历深度（默认 3） |

将 Semgrep findings 与调用图结合，分析漏洞能影响多少下游调用方。返回风险等级 + 每个漏洞的影响树。

### `guardrail`：安全护栏（子命令）

```bash
# 扫描违规
cg guardrail scan
cg guardrail scan --file src/db/
cg guardrail scan --category db_safety

# 列出规则
cg guardrail rules
cg guardrail rules --category api_compat
```

扫描 DB/API/Incident 三类可阻断规则。

### `--semgrep [PATH...]`：Semgrep 扫描

```bash
# 详细扫描
cg --semgrep
cg --semgrep src/payment/ src/api/

# 快速汇总
cg --semgrep --semgrep-quick

# 扫描并存入数据库
cg --semgrep --semgrep-save

# 自定义规则配置
cg --semgrep --semgrep-config p/security
cg --semgrep --semgrep-config p/security --semgrep-scan-lang rust typescript
```

| 参数 | 说明 |
|------|------|
| `--semgrep [PATH...]` | 扫描路径（为空则扫描整个工作区） |
| `--semgrep-config <CONFIG>` | 规则配置（默认 `p/default`） |
| `--semgrep-scan-lang <LANG...>` | 限制语言 |
| `--semgrep-timeout <N>` | 超时秒数（默认 180） |
| `--semgrep-quick` | 快速汇总模式 |
| `--semgrep-save` | 扫描结果存入数据库 |

### `--semgrep-stats` / `--semgrep-list`

```bash
# 统计
cg --semgrep-stats

# 列表
cg --semgrep-list
cg --semgrep-list --semgrep-severity ERROR
cg --semgrep-list --semgrep-list-lang rust
```

---

## 编辑与注释命令

### `--restore-comment <SPEC>`：恢复函数注释

```bash
# 预览
cg --restore-comment "src/payment/mod.rs:process_payment@3" --preview

# 实际写入
cg --restore-comment "src/payment/mod.rs:process_payment@3"
```

SPEC 格式：`文件路径:符号名@版本号` 或 `文件路径:行号`

### `--restore-all-comments`：批量恢复

```bash
# 预览全部
cg --restore-all-comments --preview

# 恢复指定文件
cg --restore-all-comments --restore-file src/payment/
```

### `--history <NAME>`：函数历史版本

```bash
cg --history process_payment
cg --history process_payment --show-content
```

### `--diff <H1> <H2>`：对比版本

```bash
cg --diff a1b2c3d4e5f6... d4e5f6a1b2c3...
```

---

## 任务管理命令

### `task create`：创建任务

```bash
cg task create \
  --title "为支付函数添加注释" \
  --desc "补全 process_payment 的文档注释" \
  --steps '[{"action":"annotate","target_file":"src/payment/mod.rs","target_symbol":"process_payment"}]'
```

### `task next`：领取下一步骤

```bash
cg task next <task_id>
```

返回下一步骤详情。若步骤为编辑类操作，系统自动调用护栏检查：
- `guardrail_alert`（block）：步骤被阻塞，需先处理告警
- `guardrail_warning`（warn）：可执行，但需关注告警

### `task report`：回报结果

```bash
# 成功
cg task report <task_id> <step_id> --result "已添加注释"

# 失败
cg task report <task_id> <step_id> --result "文件不存在" --fail
```

失败时系统自动插入"修复缺陷"步骤。

### `task rollback`：回滚变更

```bash
cg task rollback <task_id> <step_id>
```

### `check-gate`：检查门禁

```bash
# 检查
cg check-gate <task_id>

# 标记门禁发现已解决
cg check-gate <task_id> --resolve
```

对变更文件运行语法检查 + Semgrep 扫描。失败会自动插入 `fix_gate_failure` 步骤。

### `--task-list` / `--task-show`

```bash
cg --task-list
cg --task-show <task_id>
```

---

## 度量命令

### `--metrics`：度量汇总

```bash
cg --metrics
```

### `--complexity [N]`：圈复杂度热点

```bash
cg --complexity
cg --complexity 50
cg --complexity --complexity-module "src/payment"
```

复杂度 >10 的函数建议重构（标记 `!`）。

### `--coupling`：模块耦合度

```bash
cg --coupling
```

计算每个模块的传入/传出耦合度和不稳定性（instability）。

### `--largest-fns [N]` / `--coupled-fns [N]`

```bash
cg --largest-fns          # 代码行数最多
cg --coupled-fns          # 耦合度最高（扇入+扇出）
```

### `--fn-metrics <NAME>`：单函数度量

```bash
cg --fn-metrics "my_project::payment::process_payment"
```

### `--comment-coverage`：注释覆盖率

```bash
cg --comment-coverage
cg --comment-coverage --coverage-by module   # 按 module/file/kind
```

### `--test-coverage`：测试覆盖率

```bash
cg --test-coverage
```

---

## 演化智能命令

### `evolution <QN>`：函数变更频率

```bash
cg evolution "my_project::payment::process_payment"
cg evolution "my_project::payment::process_payment" --window 30d
```

显示变更次数、变更者、变更时间线、变更分布。

### `hotspot`：热点函数排名

```bash
cg hotspot
cg hotspot --module src/payment --limit 50
```

按热点分（变更次数 + 缺陷数 + 复杂度）排序。

### `churn`：代码流失分析

```bash
cg churn
cg churn --window 90d
cg churn --module src/api
```

### `symbol-history <HASH>`：符号 Git 历史

```bash
cg symbol-history a1b2c3d4e5f6...
cg symbol-history a1b2c3d4e5f6... --limit 50
```

### `test-impact <QN>`：测试影响选择

```bash
cg test-impact "my_project::payment::process_payment"
```

返回改了该函数后需要运行的测试列表（通过反向调用链 BFS）。

---

## 影响分析命令

### `impact <HASH>`：变更影响半径（子命令）

```bash
cg impact a1b2c3d4e5f6...
cg impact a1b2c3d4e5f6... --depth 5
```

以符号为起点，沿调用链向上游扩散，计算受影响调用者数量与跨层分布（代码/DB/API/配置）。

### `review <HASH>`：审查就绪报告

```bash
cg review a1b2c3d4e5f6...
```

生成审查就绪报告：风险等级、影响范围、必测项、人工审查点、覆盖率。

---

## 缺陷知识库命令

### `defect search`：搜索缺陷模式

```bash
cg defect search
cg defect search --category security
cg defect search --severity error --limit 30
```

### `defect suggest`：推荐修复方案

```bash
cg defect suggest a1b2c3d4e5f6...
cg defect suggest a1b2c3d4e5f6... --finding 42
```

### `defect learn`：从修复 commit 学习

```bash
cg defect learn abc123def456
```

### `defect stats` / `defect build`

```bash
cg defect stats     # 统计
cg defect build     # 构建知识库
```

### `--function-issues [FN]`：函数缺陷检测

```bash
# 单函数
cg --function-issues "my_project::payment::process_payment"

# 全部函数列表
cg --function-issues
cg --function-issues --issue-type missing_comment
cg --function-issues --issue-module src/api
```

### `--issue-summary`：缺陷汇总

```bash
cg --issue-summary
```

---

## Git 集成命令

### `--git-import [N]`：导入 Git 历史

```bash
cg --git-import          # 默认 100 个 commit
cg --git-import 500
```

### `--git-log [N]`：commit 历史

```bash
cg --git-log
cg --git-log 50
```

### `--git-show <COMMIT>`：commit 详情

```bash
cg --git-show abc123def456
```

### `--git-stats`：Git 统计

```bash
cg --git-stats
```

---

## 向量与语义搜索命令

### `--semantic-search <QUERY>`：语义搜索

```bash
cg --semantic-search "处理用户认证的函数"
```

> 首次使用前需运行 `--embed` 生成向量嵌入。嵌入模型不可用时自动回退到关键词匹配。

### `--embed` / `--embed-force`：生成向量嵌入

```bash
cg --embed           # 增量嵌入
cg --embed-force     # 强制重新嵌入所有函数
```

### `--similar <NAME>`：查找相似函数

```bash
cg --similar "my_project::payment::process_payment"
```

---

## 概览与导出命令

### `--brief`：项目简报

```bash
cg --brief
```

输出项目类型、文件数、函数数、健康评分、复杂度热点等。

### `--map`：仓库模块依赖图

```bash
cg --map                    # 默认 text
cg --map --map-format mermaid
```

### `--export-module-graph [FORMAT]`：导出模块依赖图

```bash
cg --export-module-graph mermaid
cg --export-module-graph mermaid --graph-output deps.mmd
cg --export-module-graph dot --graph-output deps.dot
```

---

## 覆盖率命令

### `--coverage-import <FILE>`：导入覆盖率报告

```bash
cg --coverage-import coverage.lcov --coverage-format lcov
cg --coverage-import coverage.xml --coverage-format cobertura
```

### `--coverage-fn <NAME>`：函数覆盖率

```bash
cg --coverage-fn "my_project::payment::process_payment"
```

### `--coverage-uncovered`：未覆盖函数

```bash
cg --coverage-uncovered
```

---

## 所有权命令

### `--who <FILE>`：文件负责人

```bash
cg --who src/payment/mod.rs
```

综合 CODEOWNERS 和 git blame 信息。

### `--ownership-map`：所有权映射

```bash
cg --ownership-map
```

---

## 工作区命令

### `--list-workspaces`

```bash
cg --list-workspaces
```

### `--register-workspace <NAME> <ROOT>`

```bash
cg --register-workspace my_project /path/to/project
```

### `--set-workspace <ID_OR_NAME>`

```bash
cg --set-workspace my_project
cg --set-workspace 1
```

### `--delete-workspace <ID_OR_NAME>`

```bash
cg --delete-workspace my_project
```

---

## 常用组合命令示例

### 示例 1：全量构建并查看状态

```bash
cg --init --force && cg --status
```

### 示例 2：查找函数 → 分析影响 → 查看度量

```bash
cg --search "process_payment"
cg --impact "my_project::payment::process_payment"
cg --fn-metrics "my_project::payment::process_payment"
```

### 示例 3：扫描缺陷 → 查看漏洞爆炸半径

```bash
cg --semgrep --semgrep-save
cg --semgrep-stats
cg vuln-blast --severity ERROR
```

### 示例 4：导入 Git 历史 → 分析热点

```bash
cg --git-import 200
cg hotspot --limit 30
cg churn --window 90d
```

### 示例 5：生成向量嵌入 → 语义搜索

```bash
cg --embed
cg --semantic-search "处理订单支付的函数"
cg --similar "my_project::payment::process_payment"
```

### 示例 6：导出模块依赖图用于文档

```bash
cg --export-module-graph mermaid --graph-output docs/architecture.mmd
cg --map --map-format mermaid > docs/repo_map.md
```

### 示例 7：完整任务流程

```bash
# 1. 创建任务
cg task create --title "重构支付模块" --steps '[{"action":"refactor","target_file":"src/payment/mod.rs"}]'

# 2. 领取步骤
cg task next <task_id>

# 3. Agent 执行编辑（通过 MCP propose_edit）

# 4. 回报成功
cg task report <task_id> <step_id> --result "已完成重构"

# 5. 检查门禁
cg check-gate <task_id>

# 6. 如需回滚
cg task rollback <task_id> <step_id>
```

---

## 多语言支持

CLI 命令默认使用中文输出，可通过 `--lang` 切换：

```bash
cg --lang en_US --status
cg --lang zh_CN --search "login"
```

## 下一步

- [MCP 工具参考](mcp_tools.md)：通过 MCP 协议调用 120 个工具
- [架构设计](architecture.md)：理解数据库 Schema 和 Mixin 架构
- [部署指南](deployment.md)：Docker 部署与多容器共享
