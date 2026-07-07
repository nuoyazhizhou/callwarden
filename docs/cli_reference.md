# CLI 命令参考

Call Warden CLI 提供两种命令风格：

1. **子命令风格**：`cw <subcommand> [options]`（12 个子命令，对应"代码守护者架构"四大支柱）
2. **Flag 风格**：`cw --flag [options]`（传统命令，覆盖构建/查询/编辑/度量等）

> 下文用 `cw` 作为命令前缀。

## 命令概览（按功能分组）

| 分组 | 命令 | 风格 | 说明 |
|------|------|------|------|
| **构建** | `--refresh-all` | flag | 增量刷新代码图谱（仅解析变更文件，不会清空数据） |
| | `--refresh-all --force` | flag | 强制全量重新解析 |
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
| | `task list/show/findings/resolve-finding` | sub | 任务列表/详情/质量发现/解决 |
| | `task apply/close` | sub | 任务审核通过/关闭（带级联关闭子任务） |
| | `task capture-diff <TASK_ID>` | sub | 捕获外部 Agent 真实文件改动到 task/change/audit 闭环 |
| | `--task-list` | flag (兼容) | 列出任务（等价 `cw task list`） |
| | `--task-show <ID>` | flag (兼容) | 查看任务详情（等价 `cw task show <ID>`） |
| | `check-gate <TASK_ID>` | sub | 检查门禁 |
| **审计** | `audit verify` | sub | 验证 `audit_chain` 签名链完整性 |
| **GC** | `gc archive/restore/status/purge` | sub | ignore 文件归档、复活、状态、清除 |
| | `gc policy show/set` | sub | 查看或修改 retention 策略 |
| | `gc retention` | sub | 按冷热策略清理旧版本/外部符号（含 Top N 收益预估） |
| | `gc archive list/inspect` | sub | 列出/检查 `gc_archives/*.db.gz` 备份 |
| | `gc archive import` | sub | 从备份恢复指定文件或外部包（INSERT OR IGNORE 幂等） |
| | `gc audit list/show` | sub | 查看 GC 审计记录（策略/候选/实删/备份） |
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
| **Agent 集成** | `install-agent <codex|claude|cursor|all>` | sub | 生成 MCP/Skill/Rules/Hooks 集成模板 |
| | `install --all` | sub | 安装全部依赖（含可选） |
| | `install --lang <LANG...>` | sub | 仅安装指定语言 grammar |
| | `install --check` | sub | 检查依赖状态 |
| **Rule Memory** | `rule candidate create/list/accept/reject` | sub | 候选规则 CRUD 与审核 |
| | `rule list` | sub | 列出已生效规则 |
| | `rule applicable` | sub | 按上下文查询匹配规则 |
| | `rule sync [--apply]` | sub | 同步 active 规则到 AGENTS.md 标记区 |
| | `rule insert-block` | sub | 在 AGENTS.md 末尾插入规则标记块 |
| | `rule extract` | sub | 从 task_quality_findings 聚合候选 |
| | `rule seed-bootstrap [--apply]` | sub | 种子化内置自举 active rules（幂等，固定 ID `AR-bootstrap-*`） |
| **自举** | `bootstrap status` | sub | 自举闭环健康摘要（DB 同步/规则/质量发现/审计链/扫描基线/任务/推荐动作） |

> `install` 是独立子命令，调用方式为 `cw install [options]`。

---

## GC 命令

GC 分两类：

- 文件归档 GC：`archive/restore/status/purge`，只处理被 ignore 规则命中的文件。
- 冷数据 retention：`policy/retention`，按数据库策略清理旧历史版本和可选外部符号。

### `gc policy show`：查看 retention 策略

```bash
cw gc policy show
```

策略保存在当前 workspace 的数据库中，作为 `gc retention` 未传参数时的默认值。

### `gc policy set`：修改 retention 策略

```bash
cw gc policy set --older-than 730 --keep-versions 200 --no-include-external --backup --no-vacuum
```

常用参数：

- `--older-than <DAYS>`：文件版本超过多少天才进入候选。
- `--keep-versions <N>`：每个文件至少保留最近 N 个版本。
- `--include-external / --no-include-external`：是否清理长期未 seen/used 的外部包符号。
- `--external-stale-days <DAYS>`：外部包冷数据阈值。
- `--backup / --no-backup`：执行前是否压缩备份完整数据库。
- `--vacuum / --no-vacuum`：执行后是否运行 VACUUM 释放磁盘空间。

### `gc retention`：按策略预演或执行清理

```bash
cw gc retention --dry-run
cw gc retention --apply
cw gc retention --apply --older-than 730 --keep-versions 200
cw gc retention --apply --older-than 730 --keep-versions 200 --save-policy
```

语义规则：

- `--dry-run`：只预演，不删除、不备份、不保存策略。
- `--apply`：执行清理；默认仍不保存临时参数。
- `--save-policy`：把本次传入的策略参数保存到数据库。
- 未传策略参数时，使用 `gc_policies` 中保存的策略。
- 传入策略参数但不带 `--save-policy` 时，只覆盖本次运行。

执行删除前默认会在数据库目录下创建 `gc_archives/*.db.gz` 压缩备份，便于后续离线导回。

dry-run 与 apply 输出末尾会附 **Top N 收益预估**（v20 新增）：

- `预计删除行数`：file_versions / file_symbol_versions / call_versions / symbol_contents / external_symbols / external_packages 各表估算行数。
- `受影响文件 Top N`：按候选版本数倒序，最多 10 条，含 `rel_path / candidate_versions / newest_parsed`。
- `受影响外部包 Top N`：按符号数倒序，最多 10 条，含 `package_name / package_version / symbol_count / last_touch`。
- `VACUUM 提示`：仅当 `--vacuum` 真正执行后 SQLite 文件空间才会释放到磁盘，单纯 DELETE 只会把空闲页留给后续写入。

> 所有数量均为估算（基于候选 ID 集合预统计），不承诺精确磁盘节省。

### `gc archive list`：列出 GC 备份文件

```bash
cw gc archive list
cw gc archive list --limit 50
```

列出 `gc_archives/*.db.gz` 备份文件，按修改时间倒序，默认最多 20 条。每条输出含序号、文件名、大小、归档原因（从文件名 `{YYYYMMDD-HHMMSS}-{reason}.db.gz` 解析）。

### `gc archive inspect`：检查备份内容

```bash
cw gc archive inspect <path-or-name>
cw gc archive inspect 20260705-1030-retention
```

以只读模式打开 `.db.gz` 备份，输出 Schema 版本、各表行数、关键摘要（workspaces / file_instances / file_versions / symbols / archived_files / external_symbols / gc_runs）。`<path-or-name>` 既可以是完整路径，也可以是文件名简写（自动在 `gc_archives/` 下查找）。

### `gc archive import`：从备份恢复数据

```bash
# 预演：仅打印将要恢复什么，不写入
cw gc archive import <path> --file src/a.py
cw gc archive import <path> --package ext-python-oldpkg

# 执行：实际写入当前数据库（INSERT OR IGNORE，幂等）
cw gc archive import <path> --file src/a.py --apply
cw gc archive import <path> --package ext-python-oldpkg --apply
```

参数：

- `<path>`：备份文件路径或简写文件名。
- `--file <REL_PATH>`：要恢复的文件相对路径；与 `--package` 二选一。
- `--package <NAME>`：要恢复的外部包名；与 `--file` 二选一。
- `--dry-run`（默认）：只预演，不写入。
- `--apply`：实际写入；以 INSERT OR IGNORE 方式恢复，已存在的行不会被覆盖（当前库优先）。

恢复语义：

- file 模式：恢复指定文件的 `file_versions / file_symbol_versions / call_versions / symbol_contents`（仅该文件历史版本涉及的部分）。
- package 模式：恢复指定包的 `external_symbols / package_versions`。
- 每次执行都会写入 `gc_runs` 审计记录（operation=`archive_import`），便于追踪谁在何时恢复了什么。

### `gc audit list`：列出 GC 审计记录

```bash
cw gc audit list
cw gc audit list --limit 50
```

列出 `gc_runs` 表中的审计记录，按时间倒序，默认最多 20 条。每条含 audit_id、operation、status、dry_run、started_at、completed_at。operation 取值：`archive / restore / purge / retention / archive_import`，status 取值：`running / completed / failed`。

### `gc audit show`：查看审计详情

```bash
cw gc audit show <AUDIT_ID>
```

输出指定审计记录的完整详情：策略参数（policy_json）、候选数量（candidate_counts）、实删数量（deleted_counts）、备份路径、备份大小、operator、起止时间、错误信息（仅 failed 时）。

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

### `install-hook`：安装 Git Hook（task_capture_diff 闭环）

Call Warden 提供独立的 Git Hook 安装命令，安装 post-commit hook 让 Agent 在 commit 后自动捕获文件变更到 task/audit 闭环。

**调用方式**：`cw install-hook post-commit [options]`

#### 安装（从环境变量读取 task_id，推荐）

```bash
cw install-hook post-commit
# 之后通过 CALLWARDEN_TASK_ID 环境变量传递当前任务 ID
export CALLWARDEN_TASK_ID=T-xxx
git commit -m "feat: xxx"
```

#### 安装（硬编码 task_id）

```bash
cw install-hook post-commit --task-id T-1783349079762-8246
```

#### 卸载

```bash
cw install-hook post-commit --uninstall
```

#### 命令行参数

| 参数 | 说明 |
|------|------|
| `hook` | Hook 类型（目前仅支持 `post-commit`） |
| `--task-id <ID>` | 硬编码 task_id 到 hook（不指定时从 `CALLWARDEN_TASK_ID` 环境变量读取） |
| `--uninstall` | 卸载 hook（仅删除 Call Warden 生成的 hook，保护用户自定义 hook） |

#### 退出码

| 退出码 | 含义 |
|--------|------|
| 0 | 安装/卸载成功 |
| 1 | 安装/卸载失败（如不在 git 仓库内） |

#### fail-soft 设计

- 没有进行中的任务时静默跳过，不影响 commit
- 数据库锁或异常时打印提示但用 `|| true` 兜底退出码
- 双层 fail-soft（DB 层 + CLI 层）确保不影响 git commit

---

## 构建命令

### `--refresh-all`：构建/增量刷新代码图谱

```bash
# 增量刷新（默认，仅解析变更文件，不会清空数据）
cw --refresh-all

# 强制全量重新解析
cw --refresh-all --force

# 指定工作区
cw --workspace /path/to/project --refresh-all
```

### `--refresh <PATH>`：刷新单个文件

```bash
cw --refresh src/payment/mod.rs
```

增量更新指定文件，重新解析符号和调用关系。

### `--watch`：文件监控

```bash
cw --watch
```

启动文件监控守护进程，文件变化时自动增量更新。按 `Ctrl+C` 停止。

### `--status`：查看状态

```bash
cw --status
```

显示工作区、文件分布、符号分布、调用关系统计、上次构建时间等。

### `--stats`：统计信息（JSON）

```bash
cw --stats
```

以 JSON 格式输出统计信息，便于脚本解析。

---

## 查询命令

### `--search <QUERY>`：符号搜索

```bash
cw --search "login"
cw --search "User" --search-kind class
cw --search "handle" --search-limit 20
```

| 参数 | 说明 |
|------|------|
| `--search <QUERY>` | 搜索关键词（模糊匹配） |
| `--search-kind <KIND>` | 类型过滤：fn/method/class/struct/enum/trait/interface |
| `--search-limit <N>` | 返回数量（默认 50） |

### `--symbol <QN>`：符号详情

```bash
cw --symbol "my_project::payment::process_payment"
```

显示符号的类型、深度、文件位置、签名、注释、调用关系（调用的函数 + 被谁调用）。

### `--file <PATH>`：文件内符号

```bash
cw --file src/payment/mod.rs
```

### `--query <NAME> <FILE>`：精确查询位置

```bash
cw --query process_payment src/payment/mod.rs
```

### `--callers <NAME>` / `--callees <NAME>`

```bash
# 谁调用了我
cw --callers process_payment

# 我调用了谁
cw --callees process_payment
```

### `--topo`：拓扑排序

```bash
cw --topo
cw --topo --topo-limit 100
```

按依赖深度排序，底层（被调用最多）在前。

---

## 调用链分析命令

### `--impact <QN>`：影响面分析（向上）

```bash
cw --impact "my_project::payment::process_payment"
cw --impact "my_project::payment::process_payment" --chain-depth 5
```

向上追踪所有调用该函数的上游函数，按层级显示。

### `--call-chain <QN>`：调用链向下

```bash
cw --call-chain "my_project::payment::process_payment"
```

### `--top-callers [N]`：被调用最多排行

```bash
cw --top-callers          # 默认 20
cw --top-callers 50
cw --top-callers --top-callers-module "src/api"
```

### `--deepest [N]`：调用深度最深

```bash
cw --deepest
cw --deepest 50
```

### `--detect-cycles`：循环调用检测

```bash
cw --detect-cycles
cw --detect-cycles --cycle-depth 15
```

### `--module-calls [N]`：模块间调用统计

```bash
cw --module-calls
```

### `--call-heatmap [GROUP]`：调用频率热力图

```bash
cw --call-heatmap              # 默认按 module
cw --call-heatmap file         # 按 file
cw --call-heatmap --heatmap-limit 30
```

### `--orphan-symbols [KIND]`：孤立符号

```bash
cw --orphan-symbols            # 默认 fn
cw --orphan-symbols struct
cw --orphan-symbols --orphan-module "src/legacy"
```

查找未被调用的孤立函数/结构体，适合清理死代码。

---

## 安全命令

### `vuln-blast`：漏洞爆炸半径（子命令）

```bash
cw vuln-blast
cw vuln-blast --finding-id 42
cw vuln-blast --severity ERROR --depth 5
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
cw guardrail scan
cw guardrail scan --file src/db/
cw guardrail scan --category db_safety

# 列出规则
cw guardrail rules
cw guardrail rules --category api_compat
```

扫描 DB/API/Incident 三类可阻断规则。

### `--semgrep [PATH...]`：Semgrep 扫描

```bash
# 详细扫描
cw --semgrep
cw --semgrep src/payment/ src/api/

# 快速汇总
cw --semgrep --semgrep-quick

# 扫描并存入数据库
cw --semgrep --semgrep-save

# 自定义规则配置
cw --semgrep --semgrep-config p/security
cw --semgrep --semgrep-config p/security --semgrep-scan-lang rust typescript
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
cw --semgrep-stats

# 列表
cw --semgrep-list
cw --semgrep-list --semgrep-severity ERROR
cw --semgrep-list --semgrep-list-lang rust
```

---

## 编辑与注释命令

### `--restore-comment <SPEC>`：恢复函数注释

```bash
# 预览
cw --restore-comment "src/payment/mod.rs:process_payment@3" --preview

# 实际写入
cw --restore-comment "src/payment/mod.rs:process_payment@3"
```

SPEC 格式：`文件路径:符号名@版本号` 或 `文件路径:行号`

### `--restore-all-comments`：批量恢复

```bash
# 预览全部
cw --restore-all-comments --preview

# 恢复指定文件
cw --restore-all-comments --restore-file src/payment/
```

### `--history <NAME>`：函数历史版本

```bash
cw --history process_payment
cw --history process_payment --show-content
```

### `--diff <H1> <H2>`：对比版本

```bash
cw --diff a1b2c3d4e5f6... d4e5f6a1b2c3...
```

---

## 任务管理命令

### `task create`：创建任务

```bash
cw task create \
  --title "为支付函数添加注释" \
  --desc "补全 process_payment 的文档注释" \
  --steps '[{"action":"annotate","target_file":"src/payment/mod.rs","target_symbol":"process_payment"}]'
```

### `task next`：领取下一步骤

```bash
cw task next <task_id>
```

返回下一步骤详情。若步骤为编辑类操作，系统自动调用护栏检查：
- `guardrail_alert`（block）：步骤被阻塞，需先处理告警
- `guardrail_warning`（warn）：可执行，但需关注告警

### `task report`：回报结果

```bash
# 成功
cw task report <task_id> <step_id> --result "已添加注释"

# 失败
cw task report <task_id> <step_id> --result "文件不存在" --fail
```

失败时系统自动插入"修复缺陷"步骤。

### `task rollback`：回滚变更

```bash
cw task rollback <task_id> <step_id>
```

### `task apply`：审核通过任务

```bash
cw task apply <task_id> [--reviewer <identity>]
```

将任务状态从 `review` 推进到 `applied`，记录审核通过时间戳 `applied_at`。

**设计原则**：写代码的 Agent 完成任务后状态为 `review`，不能自己 `apply`，
必须由其他会话的 LLM 审核调用，避免基于奖励函数的激励直接 close 任务。

- 仅 `review` 状态的任务可 `apply`，其他状态返回错误
- 成功后 `applied_at` 字段写入当前时间戳
- `--reviewer` 参数标识审核人（默认 `reviewer`）

**父任务禁止手动 apply**：若任务有子任务（即父任务），返回错误
`reason=parent_task_must_cascade`，提示由级联触发。父任务的状态推进由系统在
最后一个子任务 apply 时自动级联完成（`review → applied → closed` 一次性推进）。

**级联 close**：若 apply 后所有兄弟子任务都已 `applied`/`closed`，触发原子级联：
1. close 所有 `applied` 状态的兄弟任务
2. 父任务 `review → applied → closed` 一次性推进
3. 递归向上检查祖父层级联

返回值新增 `cascaded_close: List[str]` 字段（仅触发级联时存在），列出所有自动
close 的 task_id。

### `task close`：关闭任务

```bash
cw task close <task_id> [--reviewer <identity>]
```

将任务状态从 `applied` 推进到 `closed`，记录关闭时间戳 `closed_at`。

**设计原则**：关闭操作也必须由其他会话的 LLM 调用，与 `apply` 配合完成
`review → applied → closed` 审核闭环。

- 仅 `applied` 状态的任务可 `close`，其他状态返回错误
- 成功后 `closed_at` 字段写入当前时间戳
- `--reviewer` 参数标识关闭人（默认 `reviewer`）

**父任务禁止手动 close**：若任务有子任务（即父任务），返回错误
`reason=parent_task_must_cascade` 和 `subtask_count` 字段，提示由级联触发。
父任务的 close 由系统在最后一个子任务 apply 时自动级联完成。

### `task findings`：查看任务质量门禁发现

```bash
# 查看任务下所有 open 状态的发现
cw task findings <task_id>

# 按状态过滤（open/resolved/wontfix/all）
cw task findings <task_id> --status resolved

# 按严重度过滤（info/warn/error/block）
cw task findings <task_id> --severity error
```

返回 `task_quality_findings` 表中匹配过滤条件的记录，按 `created_at` 升序排列。
每条发现包含：`id` / `severity` / `status` / `finding_type` / `message` /
`source` / `step_id` 等字段。

**严重度说明**：
- `info`：提示性发现，不阻塞任务完成
- `warn`：警告，不阻塞但建议处理
- `error`：错误，阻塞 step 进入 done
- `block`：阻塞，必须修复后才能继续

### `task resolve-finding`：解决质量门禁发现

```bash
# 标记为已修复
cw task resolve-finding <finding_id>

# 标记为暂不修复（接受风险）
cw task resolve-finding <finding_id> --resolution wontfix

# 标记为误报
cw task resolve-finding <finding_id> --resolution false_positive

# 指定解决者
cw task resolve-finding <finding_id> --by human
```

将 finding 状态从 `open` 推进到 `resolved`（fixed）或 `wontfix`
（wontfix / false_positive）。`error` / `block` 级别的发现被解决后，
该 step 的阻塞状态才会解除，再次 `task_completion_review` 会重新评估。

### `task list`：列出任务

```bash
# 列出任务（默认按树形展示父子任务，最多 200 个）
cw task list

# 切换到扁平展示（不缩进）
cw task list --flat

# 仅显示有阻塞发现的任务
cw task list --blocked

# 按状态过滤
cw task list --status in_progress
cw task list --status review

# 限制返回数量
cw task list --limit 50
```

默认按 **树形模式** 展示：根任务在前，子任务按 `sort_order` 递归缩进。
每行格式：`[indent]  [icon] <task_id> [status] <title>`，其中：
- `indent`：4 空格 × depth（`--flat` 模式下无缩进）
- `icon`：`[!]` 表示有 `error`/`block` 级别的 open 发现，`[ ]` 表示无阻塞

**返回字段**：`task_id / title / status / created_at / parent_id / depth / sort_order / step_count`

**排序规则**：根任务优先 → `parent_id` 升序 → `sort_order` 升序 → `created_at` 倒序

### `task show`：查看任务详情（含子任务树）

```bash
# 默认树形展示（递归显示所有子任务 + 进度）
cw task show <task_id>

# 切换到扁平模式（仅显示主任务，不递归子任务）
cw task show <task_id> --flat
```

默认调用 `db.task_status_tree()`，递归展示：
- 任务详情：ID / 标题 / 状态 / 描述 / 创建者 / 创建时间
- 进度：`done/total (pct%)`
- 自身步骤列表（仅根任务显示步骤明细）
- 子任务树（按 depth 缩进，带 `↳` 前缀）

`--flat` 模式调用 `db.task_status()`，仅显示主任务详情和自身步骤。

### `task capture-diff`：捕获外部 Agent 文件改动到审计闭环

```bash
# Dry-run（默认）：只返回计划，不写库
cw task capture-diff <task_id>

# 实际写入：落 change_audit / task_symbol_changes / audit_chain，并触发质量检查
cw task capture-diff <task_id> --apply

# 指定关联 step 与 base commit
cw task capture-diff <task_id> --step-id S-1783... --base HEAD~1 --apply

# --auto 模式：自动检测 in_progress 任务，HEAD~1 作为 base，自动 apply（fail-soft）
# 常用于 post-commit hook 自动调用，task_id 可省略
cw task capture-diff --auto
```

**用途**：当外部 Agent（如 Claude Code、Codex 等）在 Call Warden 之外
直接修改了文件后，调用此命令把这些"真实改动"捕获回 Call Warden 的
任务/变更/符号/审计闭环，使图谱与磁盘保持一致，并自动生成质量发现。

**参数**：
- `<task_id>`（可选，`--auto` 模式下可省略）：关联的任务 ID
- `--step-id <ID>`：关联的 step ID（可选，默认空）
- `--base <COMMIT>`：基准 commit（可选，默认使用最近一次 `workspace_scan_runs` 的 git_head）
- `--dry-run`（默认）：只返回计划，不写数据库
- `--auto`：自动模式（fail-soft，不阻断 git commit）：
  - 自动检测当前 `in_progress` 状态的任务
  - 取 `HEAD~1` 作为 base（commit 后 hook 触发，HEAD 已是新提交）
  - 自动 apply（`dry_run=False`）
  - 双层 fail-soft（DB 层 + CLI 层）确保不影响 git commit

**输出**：
- 变更文件清单（含新增 / 修改 / 删除统计）
- `scan_id`：对应的 `workspace_scan_runs` 记录 ID（apply 模式）
- `linked_findings`：本次变更触发的质量发现（按 finding_type 分组）
- `quality_decision`：`pass` / `warn` / `block`
- `next_action`：建议的下一步（`review` / `fix_findings` / `noop`）

**写入的事实表**：
- `workspace_scan_runs`：扫描基线记录（purpose=`task_capture`，author=`capture-diff`）
- `change_audit`：每文件一条变更记录（hash_before / hash_after）
- `task_symbol_changes`：受影响的符号变更记录
- `audit_chain`：每条 change_audit 的签名链记录
- `task_quality_findings`：scope/call_chain/file_health 等检查器发现的违规

> **与 `task report` 的关系**：`task report` 是 Agent 声明完成 step；
> `task capture-diff` 是从磁盘真实变更反向同步到任务上下文，二者配合
> 构成完整的"声明 + 验证"闭环。

### `audit verify`：验证审计链完整性

```bash
# 验证全部审计链
cw audit verify

# 仅验证指定表的审计链
cw audit verify --table change_audit
cw audit verify --table file_edit_audit
cw audit verify --table task_symbol_changes
cw audit verify --table task_quality_findings

# 限制验证记录数
cw audit verify --limit 500
```

验证 `audit_chain` 表中签名链的连续性与签名匹配。每条 `audit_chain` 记录包含
`payload_hash` + `prev_signature` + `record_signature`，形成链式结构。

**签名算法**：
- 无 HMAC key 时：`SHA-256(prev_signature + "|" + payload_hash)`，
  `signing_key_id='local'`，`security_level='hash_only'`
- 有 HMAC key 时：`HMAC-SHA256(key, prev_signature + "|" + payload_hash)`，
  `signing_key_id='hmac'`，`security_level='hmac'`

**HMAC key 来源**（优先级从高到低）：
1. 环境变量 `CALLWARDEN_AUDIT_HMAC_KEY`
2. 文件 `~/.callwarden/audit.key`
3. 回落到 SHA-256 链

**输出**：
- 总记录数 / 通过数 / 不通过数
- 当前安全级别（`hash_only` 或 `hmac`）
- 不通过记录明细（id / table / record_id / reasons）

**reasons 含义**：
- `signature_mismatch`：`record_signature` 与重新计算的签名不匹配（记录被篡改）
- `chain_broken`：`prev_signature` 与上一条的 `record_signature` 不匹配（链断裂）
- `first_prev_not_empty`：首条记录 `prev_signature` 应为空串但非空

### `check-gate`：检查门禁

```bash
# 检查
cw check-gate <task_id>

# 标记门禁发现已解决
cw check-gate <task_id> --resolve
```

对变更文件运行语法检查 + Semgrep 扫描。失败会自动插入 `fix_gate_failure` 步骤。

> **与 `task findings` 的关系**：`check-gate` 是手动触发一次检查并写入
> `task_quality_findings`；`task findings` 是查询已写入的发现。
> `task_completion_review`（MCP 工具）会自动调用 `check-gate` 并叠加
> 5 个扩展检查器（scope / symbol_attribution / file_health / i18n / signature）。

### `--task-list` / `--task-show`：兼容入口（已废弃）

```bash
cw --task-list                # 等价于 cw task list（显示兼容提示后转调）
cw --task-show <task_id>      # 等价于 cw task show <task_id>（树形模式）
```

> **注意**：这两个 flag 作为兼容入口保留，会先打印一行提示再转调对应子命令。
> 推荐直接使用 `cw task list` / `cw task show` 子命令。

---

## 度量命令

### `--metrics`：度量汇总

```bash
cw --metrics
```

### `--complexity [N]`：圈复杂度热点

```bash
cw --complexity
cw --complexity 50
cw --complexity --complexity-module "src/payment"
```

复杂度 >10 的函数建议重构（标记 `!`）。

### `--coupling`：模块耦合度

```bash
cw --coupling
```

计算每个模块的传入/传出耦合度和不稳定性（instability）。

### `--largest-fns [N]` / `--coupled-fns [N]`

```bash
cw --largest-fns          # 代码行数最多
cw --coupled-fns          # 耦合度最高（扇入+扇出）
```

### `--fn-metrics <NAME>`：单函数度量

```bash
cw --fn-metrics "my_project::payment::process_payment"
```

### `--comment-coverage`：注释覆盖率

```bash
cw --comment-coverage
cw --comment-coverage --coverage-by module   # 按 module/file/kind
```

### `--test-coverage`：测试覆盖率

```bash
cw --test-coverage
```

---

## 演化智能命令

### `evolution <QN>`：函数变更频率

```bash
cw evolution "my_project::payment::process_payment"
cw evolution "my_project::payment::process_payment" --window 30d
```

显示变更次数、变更者、变更时间线、变更分布。

### `hotspot`：热点函数排名

```bash
cw hotspot
cw hotspot --module src/payment --limit 50
```

按热点分（变更次数 + 缺陷数 + 复杂度）排序。

### `churn`：代码流失分析

```bash
cw churn
cw churn --window 90d
cw churn --module src/api
```

### `symbol-history <HASH>`：符号 Git 历史

```bash
cw symbol-history a1b2c3d4e5f6...
cw symbol-history a1b2c3d4e5f6... --limit 50
```

### `test-impact <QN>`：测试影响选择

```bash
cw test-impact "my_project::payment::process_payment"
```

返回改了该函数后需要运行的测试列表（通过反向调用链 BFS）。

---

## 影响分析命令

### `impact <HASH>`：变更影响半径（子命令）

```bash
cw impact a1b2c3d4e5f6...
cw impact a1b2c3d4e5f6... --depth 5
```

以符号为起点，沿调用链向上游扩散，计算受影响调用者数量与跨层分布（代码/DB/API/配置）。

### `review <HASH>`：审查就绪报告

```bash
cw review a1b2c3d4e5f6...
```

生成审查就绪报告：风险等级、影响范围、必测项、人工审查点、覆盖率。

---

## 缺陷知识库命令

### `defect search`：搜索缺陷模式

```bash
cw defect search
cw defect search --category security
cw defect search --severity error --limit 30
```

### `defect suggest`：推荐修复方案

```bash
cw defect suggest a1b2c3d4e5f6...
cw defect suggest a1b2c3d4e5f6... --finding 42
```

### `defect learn`：从修复 commit 学习

```bash
cw defect learn abc123def456
```

### `defect stats` / `defect build`

```bash
cw defect stats     # 统计
cw defect build     # 构建知识库
```

### `--function-issues [FN]`：函数缺陷检测

```bash
# 单函数
cw --function-issues "my_project::payment::process_payment"

# 全部函数列表
cw --function-issues
cw --function-issues --issue-type missing_comment
cw --function-issues --issue-module src/api
```

### `--issue-summary`：缺陷汇总

```bash
cw --issue-summary
```

---

## Agent Rule Memory 命令

Agent Rule Memory 提供项目规则的「候选 → 审核 → 生效 → 注入 → 同步」全生命周期管理。
规则候选默认 pending，必须 accept 后才会写入 `agent_rules` 并参与上下文注入；
active 规则可同步到 AGENTS.md 标记区，让无 MCP 的 Agent 也能读到。

### `rule candidate create`：创建候选规则

```bash
cw rule candidate create --title "use i18n" --text "禁止硬编码字符串" \
    --severity warning \
    --scope '{"languages":["python"],"actions":["edit"]}' \
    --source manual \
    --evidence '{"task_id":"T-xxx","occurrences":3}' \
    --confidence 0.8
```

必填：`--title` / `--text`。其余可选，`--severity` 默认 `info`，`--source` 默认 `manual`。

### `rule candidate list`：列出候选规则

```bash
cw rule candidate list                       # 默认 pending
cw rule candidate list --status accepted    # 已接受
cw rule candidate list --status ""           # 所有状态
cw rule candidate list --limit 20
```

### `rule candidate accept`：接受候选 -> active 规则

```bash
cw rule candidate accept ARC-1783253838000-a1b2
cw rule candidate accept ARC-1783253838000-a1b2 --reviewer human
```

幂等：重复 accept 已 accepted 的 candidate 会返回原 `linked_rule_id`。

### `rule candidate reject`：拒绝候选规则

```bash
cw rule candidate reject ARC-1783253838000-a1b2
cw rule candidate reject ARC-1783253838000-a1b2 --reason "duplicate"
```

### `rule list`：列出已生效规则

```bash
cw rule list                       # 默认 active
cw rule list --status deprecated   # 已弃用
cw rule list --status ""           # 所有状态
cw rule list --limit 50
```

输出含每条规则的 `id` / `title` / `severity` / `synced` 标记 / `scope`。

### `rule applicable`：按上下文查询匹配规则

```bash
cw rule applicable
cw rule applicable --context '{"languages":["python"],"actions":["edit"]}'
cw rule applicable --context '{"file_patterns":["src/api/**/*.py"]}' --limit 5
```

返回按 `severity → 命中字段数 → updated_at` 排序的匹配规则。

### `rule sync`：同步 active 规则到 AGENTS.md

```bash
# 默认 dry-run，只返回 preview，不写文件
cw rule sync
cw rule sync --target AGENTS.md

# 实际写入文件（只改 marker block，不触碰人工内容）
cw rule sync --apply
cw rule sync --target path/to/AGENTS.md --apply --actor human
```

标记区格式：

```markdown
## Call Warden 自动沉淀规则

<!-- CALLWARDEN_RULES_START -->
<!-- 自动同步区域，请通过 cw rule sync 更新，不要手改 -->
- [AR-xxx] **rule-title** (severity: warning): rule text
<!-- CALLWARDEN_RULES_END -->
```

apply 模式会：
1. 只替换 `CALLWARDEN_RULES_START/END` 之间的内容
2. 写入 `agent_rule_sync_log`（before_hash / after_hash / rule_ids）
3. 标记规则的 `synced_to_agents_md=1` 与 `sync_hash`

### `rule insert-block`：插入标记块

```bash
cw rule insert-block
cw rule insert-block --target path/to/AGENTS.md
```

当 AGENTS.md 还没有 Call Warden 标记区时调用此命令插入空标记块，
之后 `rule sync` 才能正常工作。重复插入会返回失败。

### `rule extract`：从质量发现聚合候选规则

```bash
cw rule extract                                # 扫描全库
cw rule extract --task-id T-1783253838000-xxx  # 指定任务
cw rule extract --min-occurrences 3            # 提高阈值
```

聚合维度：`(finding_type, severity, source)`。同一聚合键出现次数 ≥
`min_occurrences`（默认 2）时生成 1 个 pending 候选规则，自动去重。

### `rule seed-bootstrap`：种子化内置自举 active rules

```bash
# Dry-run（默认）：只返回计划，不写库
cw rule seed-bootstrap

# 实际写入 agent_rules 表
cw rule seed-bootstrap --apply
```

**用途**：把 Call Warden 自身的 5 条核心规约（i18n 强制、提交前刷新、
任务拆分、completion review、capture-diff）以**固定 ID** `AR-bootstrap-*`
写入 `agent_rules` 表，让 `task_next_step` / `work_next_job` /
`file_symbol_content` / `get_symbol` 等注入点能向 Agent 提供稳定的行为约束。

**幂等性**：通过固定 ID 实现：
- 不存在 → `create`
- 存在但 `rule_text` 变化 → `update`
- 存在且无变化 → `skip`

**内置规则清单**：

| ID | severity | scope | 说明 |
|----|----------|-------|------|
| `AR-bootstrap-i18n` | warning | `{}` (global) | 用户可见输出必须通过 i18n.t() |
| `AR-bootstrap-refresh-before-commit` | warning | `{actions:[commit]}` | git commit 前必须 `cw --refresh-all` |
| `AR-bootstrap-task-split` | info | `{actions:[task_create]}` | 3+ 文件或 5+ 步骤必须 task_split |
| `AR-bootstrap-completion-review` | warning | `{actions:[task_report]}` | task_report 前必须 run_task_completion_review |
| `AR-bootstrap-capture-diff` | info | `{actions:[task_report]}` | task_report 前建议 task_capture_diff 验证磁盘 |

**输出**：
- `total` / `created` / `updated` / `skipped` 计数
- 每条规则的 `action`（create / update / skip）+ title
- dry-run 模式末尾提示 `Use --apply to write rules to agent_rules table.`

---

## 自举闭环命令

### `bootstrap status`：自举健康摘要

```bash
cw bootstrap status
```

**用途**：一行命令汇总自举闭环（bootstrap closure）整体健康度，便于
人工巡检或 CI 门禁脚本快速判断"系统是否处于一致性状态"。

**输出分组**（按区块）：

1. **DB stale 状态** — 当前 git_head 与最近一次 `workspace_scan_runs.git_head`
   是否一致；不一致会红色提示运行 `cw --refresh-all`
2. **规则与候选** — `agent_rules` 已生效规则数 + `agent_rule_candidates`
   pending 候选数
3. **质量发现** — `task_quality_findings` open 数 + blocking（error/block）数
4. **审计链验证** — `audit_chain` total / broken / security_level
   （`hash_only` 或 `hmac`）
5. **最近扫描基线** — 最近一次 `workspace_scan_runs` 的 id / git_head / status
6. **任务状态分组** — tasks 表按 open / in_progress / review / applied 分组计数
7. **推荐下一条命令** — 根据当前状态推荐 `cw --refresh-all` /
   `cw rule seed-bootstrap --apply` / `cw audit verify` /
   `cw task next <id>` 等

> **只读命令**：`bootstrap status` 不会写数据库，不会触发 workspace 激活，
> 不会与 MCP Server 长连接撞锁。

---

## Git 集成命令

### `--git-import [N]`：导入 Git 历史

```bash
cw --git-import          # 默认 100 个 commit
cw --git-import 500
```

### `--git-log [N]`：commit 历史

```bash
cw --git-log
cw --git-log 50
```

### `--git-show <COMMIT>`：commit 详情

```bash
cw --git-show abc123def456
```

### `--git-stats`：Git 统计

```bash
cw --git-stats
```

---

## 向量与语义搜索命令

### `--semantic-search <QUERY>`：语义搜索

```bash
cw --semantic-search "处理用户认证的函数"
```

> 首次使用前需运行 `--embed` 生成向量嵌入。嵌入模型不可用时自动回退到关键词匹配。

### `--embed` / `--embed-force`：生成向量嵌入

```bash
cw --embed           # 增量嵌入
cw --embed-force     # 强制重新嵌入所有函数
```

### `--similar <NAME>`：查找相似函数

```bash
cw --similar "my_project::payment::process_payment"
```

---

## 概览与导出命令

### `--brief`：项目简报

```bash
cw --brief
```

输出项目类型、文件数、函数数、健康评分、复杂度热点等。

### `--map`：仓库模块依赖图

```bash
cw --map                    # 默认 text
cw --map --map-format mermaid
```

### `--export-module-graph [FORMAT]`：导出模块依赖图

```bash
cw --export-module-graph mermaid
cw --export-module-graph mermaid --graph-output deps.mmd
cw --export-module-graph dot --graph-output deps.dot
```

---

## 覆盖率命令

### `--coverage-import <FILE>`：导入覆盖率报告

```bash
cw --coverage-import coverage.lcov --coverage-format lcov
cw --coverage-import coverage.xml --coverage-format cobertura
```

### `--coverage-fn <NAME>`：函数覆盖率

```bash
cw --coverage-fn "my_project::payment::process_payment"
```

### `--coverage-uncovered`：未覆盖函数

```bash
cw --coverage-uncovered
```

---

## 所有权命令

### `--who <FILE>`：文件负责人

```bash
cw --who src/payment/mod.rs
```

综合 CODEOWNERS 和 git blame 信息。

### `--ownership-map`：所有权映射

```bash
cw --ownership-map
```

---

## 工作区命令

### `--list-workspaces`

```bash
cw --list-workspaces
```

### `--register-workspace <NAME> <ROOT>`

```bash
cw --register-workspace my_project /path/to/project
```

### `--set-workspace <ID_OR_NAME>`

```bash
cw --set-workspace my_project
cw --set-workspace 1
```

### `--delete-workspace <ID_OR_NAME>`

```bash
cw --delete-workspace my_project
```

---

## 常用组合命令示例

### 示例 1：全量构建并查看状态

```bash
cw --refresh-all --force && cw --status
```

### 示例 2：查找函数 → 分析影响 → 查看度量

```bash
cw --search "process_payment"
cw --impact "my_project::payment::process_payment"
cw --fn-metrics "my_project::payment::process_payment"
```

### 示例 3：扫描缺陷 → 查看漏洞爆炸半径

```bash
cw --semgrep --semgrep-save
cw --semgrep-stats
cw vuln-blast --severity ERROR
```

### 示例 4：导入 Git 历史 → 分析热点

```bash
cw --git-import 200
cw hotspot --limit 30
cw churn --window 90d
```

### 示例 5：生成向量嵌入 → 语义搜索

```bash
cw --embed
cw --semantic-search "处理订单支付的函数"
cw --similar "my_project::payment::process_payment"
```

### 示例 6：导出模块依赖图用于文档

```bash
cw --export-module-graph mermaid --graph-output docs/architecture.mmd
cw --map --map-format mermaid > docs/repo_map.md
```

### 示例 7：完整任务流程

```bash
# 1. 创建任务
cw task create --title "重构支付模块" --steps '[{"action":"refactor","target_file":"src/payment/mod.rs"}]'

# 2. 领取步骤
cw task next <task_id>

# 3. Agent 执行编辑（通过 MCP propose_edit）

# 4. 回报成功
cw task report <task_id> <step_id> --result "已完成重构"

# 5. 检查门禁
cw check-gate <task_id>

# 6. 审核通过（由其他会话的 LLM 调用，review -> applied）
cw task apply <task_id> --reviewer reviewer-session

# 7. 关闭任务（由其他会话的 LLM 调用，applied -> closed）
cw task close <task_id> --reviewer closer-session

# 8. 如需回滚
cw task rollback <task_id> <step_id>
```

### 示例 8：父子任务级联 close

```bash
# 1. 创建父任务（含 3 个子任务）
cw task create --title "重构支付模块" --parent-id ""  # 父任务
# → 返回 parent_id

# 2. 为父任务创建 3 个子任务
cw task create --title "重构支付 API" --parent-id <parent_id>
cw task create --title "重构支付 DB 层" --parent-id <parent_id>
cw task create --title "重构支付测试" --parent-id <parent_id>

# 3. 领取并完成所有子任务（深度优先）
cw task next <parent_id>  # 自动下钻到第一个子任务
# ... 每个子任务完成后 task_next_step 自动下钻到下一个

# 4. 所有子任务进入 review 后，父任务自动推进到 review
# （由 _update_parent_status 自动触发）

# 5. 逐个 apply 子任务（由其他会话 LLM 调用）
cw task apply <sub1_id> --reviewer reviewer-A  # 不级联（还有兄弟未 apply）
cw task apply <sub2_id> --reviewer reviewer-B  # 不级联（还有兄弟未 apply）
cw task apply <sub3_id> --reviewer reviewer-C  # 触发级联 close！
# → 返回值包含 cascaded_close: [sub1_id, sub2_id, sub3_id, parent_id]
# → 所有子任务和父任务一次性变为 closed 状态

# 6. 查看任务树状态确认
cw task show <parent_id>
# → 所有子任务和父任务都是 closed
```

---

## 多语言支持

CLI 命令默认使用中文输出，可通过 `--lang` 切换：

```bash
cw --lang en_US --status
cw --lang zh_CN --search "login"
```

### i18n 全量改造收尾（C5）

Call Warden 自身的所有用户可见输出（标题、标签、列表项、分隔说明等）已通过
`i18n.t()` 走国际化文案，新增的 i18n key 覆盖以下场景：

- **查询类**：`callers_item` / `callees_item` / `topo_item` — 调用者、被调用者、拓扑排序的列表项
- **diff 显示**：`diff_remove_line` / `diff_add_line` — 版本对比的 +/- 行
- **install --check**：`install_check_ok` / `install_check_miss` / `install_check_item` — 依赖状态行
- **install hooks**：`install_hooks_installed` / `install_hooks_skipped` / `install_hooks_summary` — Git hook 安装结果
- **install-agent**：`install_agent_path_item` — Agent 集成文件路径列表项
- **GitHub Action**：`github_action_title` / `github_action_base_ref` / `github_action_head_ref` / `github_action_workspace` — CI 标题与分支信息
- **缺陷建议**：`defect_suggest_fix_truncated` — 修复方案截断省略号
- **restore-all**：`restore_all_error_item` — 批量恢复错误列表项

> 程序化输出（如 `print(json.dumps(...))`）、纯分隔符（`"=" * N`）、CLI 命令示例
> （如 `cw doctor --add-defender-exclusion`）以及 `cw.py` 启动前 i18n 模块未加载时的
> 引导信息保留硬编码，不在 i18n 范围内。

## 下一步

- [MCP 工具参考](mcp_tools.md)：通过 MCP 协议调用 120 个工具
- [架构设计](architecture.md)：理解数据库 Schema 和 Mixin 架构
- [部署指南](deployment.md)：Docker 部署与多容器共享
