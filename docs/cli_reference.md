# CLI 命令参考

Call Warden CLI 统一使用 subcommand 风格：`cw <subcommand> [options]`，对应 21 大功能分类（[1]-[17] 与 MCP 工具分类同构，[18]-[21] 为 CLI 独有运维面）。

> 下文用 `cw` 作为命令前缀。全局 flag（`--lang`/`--workspace`/`--root`/`--force`/`--no-auto-setup`）见本文档末尾「全局 flag」章节。旧的 `cw --flag` 风格已在未上线阶段整体移除，不再保留兼容入口（决策记录见 [architecture.md 命令风格统一规范](architecture.md#命令风格统一规范c8)）。

## 退出码契约（全局约定）

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时或能力未接线（含 `_validate_identity_routed` 的权威判定失败） |
| 2 | 输入/用法错误：argparse 用法错误，或**本地**输入校验失败（identity 四字段缺失、lease 凭证不完整、计划/信封文件不存在或 JSON 非法等） |

判定边界：**本地即可判定不合法** → 2；**需要 daemon 权威判定或运行时环境** → 1。
identity 四字段齐备但 daemon 判 `E_IDENTITY_UNREGISTERED` / `E_IDENTITY_SESSION_MISMATCH`
等，属 daemon 拒绝 → 1。

## 命令概览（按 21 大功能分类）

Call Warden 把 84 个顶层命令按功能聚合为 21 个主分类（[1]-[17] 与 MCP 工具 17 分类同构对齐，[18]-[21] 为 CLI 独有运维面），每个主分类下包含若干 subcommand。顶层命令数 = `cli.main._SUBCOMMANDS` 的 79 个 subcommand + `cw.py` standalone 入口 3 个（`install` / `server` / `test`）+ `setup`（cli/main.py 单独分发）+ `daemon`（cli.main 转发 daemon_commands）= 84 个；展开各命令的二级子动作后约 200+ 个可执行命令（`gc` / `task` / `rule` 等含多级子动作）。分类真相源为 `cli/categories.py`（`COMMAND_CATEGORIES`），下表由 `scripts/gen_category_overview.py --emit-cli` 生成，勿手工编辑标记块内内容。命令数以实际 argparse 树为准（2026-10-08 审计核对）。

<!-- BEGIN:generated-overview (scripts/gen_category_overview.py; DO NOT EDIT) -->

| # | 主分类 | 命令数 | 涵盖范围 | 主要 subcommand |
| --- | --- | --- | --- | --- |
| 1 | **Workspace & Database** | 7 | 工作区管理、数据库刷新、状态概览、分支感知、分层配置、C 图构建、S3 治理写 | `workspace`、`refresh`、`stats`、`status`、`graph`、`config`、`write` |
| 2 | **Query & Search** | 13 | 符号/文件/语义搜索、摘要、RAG、版本对比、最近变更、FTS 全文索引 | `search`、`grep`、`symbol`、`file`、`query`、`brief`、`map`、`fts`、`semantic-search`、`similar`、`embed`、`diff`、`changes` |
| 3 | **Call Chain Analysis** | 12 | 调用链、拓扑、循环、孤儿、模块图、热力图、变更影响半径 | `callers`、`callees`、`call-chain`、`topo`、`impact`、`deepest`、`module-calls`、`detect-cycles`、`export-module-graph`、`call-heatmap`、`top-callers`、`orphan-symbols` |
| 4 | **Code Health & Metrics** | 11 | 度量、复杂度、耦合、演化、热点、流失、健康报告、驾驶舱 | `metrics`、`complexity`、`coupling`、`largest-fns`、`coupled-fns`、`fn-metrics`、`evolution`、`hotspot`、`churn`、`health-report`、`dashboard` |
| 5 | **Task Orchestration** | 1 | 任务创建/认领/上报/回滚/审批/关闭、派工查询、角色提示、capture-diff | `task` |
| 6 | **Agent Rule Memory** | 1 | 规则候选/审核/生效/同步/提取/清理/种子化 | `rule` |
| 7 | **Audit & Bootstrap** | 4 | 审计链、密钥轮换、自举健康、检查门禁、安全护栏 | `audit`、`bootstrap`、`check-gate`、`guardrail` |
| 8 | **Git Integration** | 2 | git 历史、commit、变更、统计、符号历史 | `git`、`symbol-history` |
| 9 | **Semgrep & Defects** | 6 | Semgrep 扫描、缺陷知识库、漏洞爆炸半径、审查就绪、符号静态检查 | `semgrep`、`defect`、`vuln-blast`、`review`、`issues`、`function-issues` |
| 10 | **Coverage & Ownership** | 9 | 注释/测试覆盖率、测试 case 关联、所有权、注释恢复 | `coverage`、`tests`、`test-impact`、`comment-coverage`、`uncommented`、`restore-comment`、`restore-all-comments`、`who`、`ownership-map` |
| 11 | **GC** | 1 | 归档、恢复、清理、策略、备份、审计、外部符号 | `gc` |
| 12 | **Diagnostics** | 2 | doctor 环境诊断、clone 重复检测 | `doctor`、`clone` |
| 13 | **构建上下文感知** | 2 | 工具链注册、build context、resolved edges | `build-context`、`toolchain` |
| 14 | **只读协同查询** | 1 | 协同证据、门禁决策、角色视图、新鲜度 | `collab` |
| 15 | **依赖图与环检测** | 1 | 依赖边、接口提供者、环检测、版本校验、工件身份 | `dependency` |
| 16 | **Assignment 与 Lease** | 2 | lease 获取/续租/释放、assignment 创建/查询/撤销 | `lease`、`assignment` |
| 17 | **Identity 与 Attestation** | 2 | PKI 生命周期（委托 cw-daemon）与 Attestation 撤销（CLI 仅暴露 revoke，避免 T=M=D） | `pki`、`identity` |
| 18 | **Migration Rollback（CLI 独有）** | 1 | 全量迁移自举计划的回滚配置登记与紧急回滚开关 | `rollback` |
| 19 | **Daemon 运维（CLI 独有）** | 1 | daemon 启动/状态/查询/快照/备份等运维操作（UDS/HTTP 客户端） | `daemon` |
| 20 | **安装与初始化（CLI 独有）** | 6 | 安装部署、Agent 集成包、Git hook、MCP Server、测试运行器 | `install`、`install-agent`、`install-hook`、`setup`、`server`、`test` |
| 21 | **盲评实验（CLI 独有）** | 1 | P0 盲评对照实验的批次/纳样/指标记录/揭示全生命周期 | `experiment` |
| **合计** | | **86** | | |

<!-- END:generated-overview -->

> **注**：详细 subcommand 用法见下文章节。

> `install` 是独立子命令，调用方式为 `cw install [options]`。

---

## MCP Server 启动与发布自检

```bash
cw server
cw server --check-imports
cw server --help
```

`cw server` 启动 stdio MCP Server。`--check-imports` 仅创建服务器并注册全部 MCP 工具，
随后立即退出；它不会同步 AGENTS.md、预下载 Semgrep 规则或进入 stdio 循环。发布流水线
用该模式验证冻结包中的 FastMCP 动态依赖，成功时输出
`Call Warden MCP imports OK`。`cw server --help` 打印用法后立即退出（不会进入 stdio 循环挂起）。

---

## 测试命令

### `test <MODULE>`：运行测试模块

运行 `callwarden/tests/` 下的某个测试模块（pytest 风格），额外参数透传给 pytest。
注意区分：`cw test` 运行开发者 pytest 模块；`cw tests <符号>` 查询某符号的测试用例/覆盖。

```bash
cw test <MODULE> [pytest 参数...]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<MODULE>` | 是 | `callwarden/tests/` 下的测试模块名（不含 `.py`，如 `test_p0_bugfixes`） |
| 其余参数 | 否 | 原样透传给 `pytest`（如 `-k`、`-v`、`-x`） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> `test` 为 `cw.py` 内置直通命令（非 argparse 子命令，无 `--help`）：
> 不带 `<MODULE>` 时打印用法并退出（rc=1）；模块不存在时导入失败（rc=1）；
> 正常路径透传 pytest 退出码（0=全部通过，1=存在失败用例）。

**示例**

```bash
cw test test_p0_bugfixes                 # 运行 tests/test_p0_bugfixes.py
cw test test_p0_bugfixes -k some_case    # 透传 pytest 参数
```

---

## HTTP daemon MVP 路由状态（H4B-N/C/I/E 收口）

HTTP transport 由 Rust daemon（`cw-daemon.exe`）提供（`cw daemon serve` 影子 server 已下线）：
不带 `--http-bind` 时默认监听 `127.0.0.1:0` 动态端口（客户端经 authority-scoped manifest
发现端口），`--http-bind 127.0.0.1:<port>` 可固定端口；绑定地址必须是 loopback
（`dev_loopback_unauthenticated` 安全档），环境变量 `CW_DAEMON_TRANSPORT=http` 显式
锁定 HTTP transport。HTTP 端点：`GET /health`、`GET /capabilities`、`POST /v1/rpc`
（JSON-RPC 2.0）、`POST /v1/jobs`。

工具/方法在 HTTP 模式下的可达性由 H0 冻结的 capability registry 决定（backend 归类
**python_compat 193 / rust_native 44 / legacy_local 0**，见
`.trae-cn/evidence/http-daemon-capability-matrix.json`）：

- **`rust_native`（44）**：走 Rust daemon dispatch（真实 RPC，如 `query.issues`、
  `query.tests`、`lease.*` 等）；
- **python_compat 路由（107）**：107 个 read_only 方法（第一批 H4C-1 默认 2 +
  H4C-2 符号组 17 + H4C-3 任务组 16 + H4C 第二批 compat read/index/governance 72）
  经 Rust `compat_route` 路由到 H3 compat
  worker（与 `server/compat_registry.py` 的 `RUST_COMPAT_ROUTE` 两端对齐）；
- **其余 python_compat（86）**：HTTP 模式统一 fail-closed，返回结构化
  `E_HTTP_COMPAT_UNSUPPORTED`，不构造 CodeGraphDB，不回退 SQLite / Named Pipe / UDS；
- **`legacy_local`（0）**：HTTP 无 legacy_local 方法；该分类仅 Legacy Baseline
  （stdio MCP / CLI）可用。

CLI 本地路径（stdio / Named Pipe / UDS）不受影响，`cw` 子命令仍按既有行为工作。

---

## GC 命令

GC 分两类：

- 文件归档 GC：`archive/restore/status/purge`，只处理被 ignore 规则命中的文件。
- 冷数据 retention：`policy/retention`，按数据库策略清理旧历史版本和可选外部符号。

### `gc policy show`：查看 retention 策略

```bash
cw gc policy show
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

策略保存在当前 workspace 的数据库中，作为 `gc retention` 未传参数时的默认值。

**示例**

```bash
cw gc policy show
```

### `gc policy set`：修改 retention 策略

```bash
cw gc policy set --older-than 730 --keep-versions 200 --no-include-external --backup --no-vacuum
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--older-than <DAYS>` | 否 | 文件版本超过多少天才进入候选（不传则使用数据库策略） |
| `--keep-versions <N>` | 否 | 每个文件至少保留最近 N 个版本（不传则使用数据库策略） |
| `--include-external` / `--no-include-external` | 否 | 是否清理长期未 seen/used 的外部包符号 |
| `--external-stale-days <DAYS>` | 否 | 外部包冷数据阈值天数（不传则使用数据库策略） |
| `--backup` / `--no-backup` | 否 | 执行前是否压缩备份完整数据库 |
| `--vacuum` / `--no-vacuum` | 否 | 执行后是否运行 VACUUM 释放磁盘空间 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw gc policy set --older-than 730 --keep-versions 200 --no-include-external --backup --no-vacuum
cw gc policy set --vacuum
```

### `gc retention`：按策略预演或执行清理

```bash
cw gc retention --dry-run
cw gc retention --apply
cw gc retention --apply --older-than 730 --keep-versions 200
cw gc retention --apply --older-than 730 --keep-versions 200 --save-policy
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--older-than <DAYS>` | 否 | 清理早于多少天的文件版本（不传则使用数据库策略） |
| `--keep-versions <N>` | 否 | 每个文件至少保留最近多少个版本（不传则使用数据库策略） |
| `--include-external` / `--no-include-external` | 否 | 同时清理长期未 seen/used 的外部包符号 |
| `--external-stale-days <DAYS>` | 否 | 外部包冷数据阈值天数（不传则使用数据库策略） |
| `--backup` / `--no-backup` | 否 | 清理前创建压缩数据库备份 |
| `--vacuum` / `--no-vacuum` | 否 | 清理后运行 VACUUM 释放磁盘空间 |
| `--dry-run` / `--apply` | 否 | 预演（默认）或执行清理 |
| `--save-policy` | 否 | 将本次传入的策略参数保存到数据库 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约）；`--apply` 写面操作受 PROTECTED_MUTATION 保护 |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

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

**示例**

```bash
cw gc retention --dry-run
cw gc retention --apply --older-than 730 --keep-versions 200 --save-policy
```

### `gc archive-list`：列出 GC 备份文件

```bash
cw gc archive-list
cw gc archive-list --limit 50
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--limit <N>` | 否 | 最多显示多少条（默认 20） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

列出 `gc_archives/*.db.gz` 备份文件，按修改时间倒序，默认最多 20 条。每条输出含序号、文件名、大小、归档原因（从文件名 `{YYYYMMDD-HHMMSS}-{reason}.db.gz` 解析）。

**示例**

```bash
cw gc archive-list
cw gc archive-list --limit 50
```

### `gc archive-inspect`：检查备份内容

```bash
cw gc archive-inspect <path-or-name>
cw gc archive-inspect 20260705-1030-retention
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<path-or-name>` | 是 | 备份文件路径（`.db.gz`，支持相对 `gc_archives` 目录的简写） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

以只读模式打开 `.db.gz` 备份，输出 Schema 版本、各表行数、关键摘要（workspaces / file_instances / file_versions / symbols / archived_files / external_symbols / gc_runs）。`<path-or-name>` 既可以是完整路径，也可以是文件名简写（自动在 `gc_archives/` 下查找）。

**示例**

```bash
cw gc archive-inspect 20260705-1030-retention
```

### `gc archive-import`：从备份恢复数据

```bash
# 预演：仅打印将要恢复什么，不写入
cw gc archive-import <path> --file src/a.py
cw gc archive-import <path> --package ext-python-oldpkg

# 执行：实际写入当前数据库（INSERT OR IGNORE，幂等）
cw gc archive-import <path> --file src/a.py --apply
cw gc archive-import <path> --package ext-python-oldpkg --apply
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<path>` | 是 | 备份文件路径（`.db.gz`，支持相对 `gc_archives` 目录的简写） |
| `--file <REL_PATH>` | 否 | 要导入的文件相对路径（如 `src/a.py`）；与 `--package` 二选一 |
| `--package <NAME>` | 否 | 要导入的外部包名；与 `--file` 二选一 |
| `--dry-run` / `--apply` | 否 | 预演（默认）：只统计不修改数据库；`--apply` 实际写入 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约）；`--apply` 写面操作受 PROTECTED_MUTATION 保护 |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

恢复语义：

- file 模式：恢复指定文件的 `file_versions / file_symbol_versions / call_versions / symbol_contents`（仅该文件历史版本涉及的部分）。
- package 模式：恢复指定包的 `external_symbols / package_versions`。
- 每次执行都会写入 `gc_runs` 审计记录（operation=`archive_import`），便于追踪谁在何时恢复了什么。

**示例**

```bash
cw gc archive-import 20260705-1030-retention --file src/a.py --apply
cw gc archive-import 20260705-1030-retention --package ext-python-oldpkg
```

### `gc audit-list`：列出 GC 审计记录

```bash
cw gc audit-list
cw gc audit-list --limit 50
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--limit <N>` | 否 | 最多显示多少条（默认 20） |
| `--operation <TYPE>` | 否 | 按操作类型过滤（retention/archive/purge） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

列出 `gc_runs` 表中的审计记录，按时间倒序，默认最多 20 条。每条含 audit_id、operation、status、dry_run、started_at、completed_at。operation 取值：`archive / restore / purge / retention / archive_import`，status 取值：`running / completed / failed`。

**示例**

```bash
cw gc audit-list --limit 50 --operation retention
```

### `gc audit-show`：查看审计详情

```bash
cw gc audit-show <AUDIT_ID>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<AUDIT_ID>` | 是 | 审计记录 ID（整数） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

输出指定审计记录的完整详情：策略参数（policy_json）、候选数量（candidate_counts）、实删数量（deleted_counts）、备份路径、备份大小、operator、起止时间、错误信息（仅 failed 时）。

**示例**

```bash
cw gc audit-show 42
```

### `gc db-cleanup`：清理孤儿数据库

扫描 `~/.callwarden/` 下所有数据库，找出孤儿数据库（测试残留 / 已删除项目的旧 hash 库），默认 dry-run 仅预览，不会删除任何文件。

```bash
cw gc db-cleanup                      # 预览（默认，不删除）
cw gc db-cleanup --apply              # 实际删除孤儿数据库
cw gc db-cleanup --all-but-current    # 除当前 workspace 外全部标记为孤儿
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--dry-run` / `--apply` | 否 | 仅预览（默认）；`--apply` 实际删除孤儿数据库 |
| `--all-but-current` | 否 | 将除当前 workspace 外的所有数据库标记为孤儿 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约）；`--apply` 写面操作受 PROTECTED_MUTATION 保护 |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw gc db-cleanup
cw gc db-cleanup --apply --all-but-current
```

### 旧版多库迁移（`gc db-migrate-single` 已退役）

`cw gc db-migrate-single` 已下线（KR-2026-10-23-A：`server/` Python 存储 authority 清零，命令无 Rust 等价实现）。存量旧库（`~/.callwarden/<16位hash>/callwarden.db`）请手工用 SQLite `ATTACH` 合并：

```bash
# 1) 先备份用户级单库
cp ~/.callwarden/callwarden.db ~/.callwarden/callwarden.db.bak.$(date +%Y%m%d)

# 2) 逐个 ATTACH 旧 hash 库并合并（workspaces 按 root_path 去重）
sqlite3 ~/.callwarden/callwarden.db "
ATTACH DATABASE '/path/to/<16位hash>/callwarden.db' AS legacy;
INSERT OR IGNORE INTO main.workspaces
  SELECT * FROM legacy.workspaces
   WHERE root_path NOT IN (SELECT root_path FROM main.workspaces);
INSERT OR IGNORE INTO main.tasks SELECT * FROM legacy.tasks;
INSERT OR IGNORE INTO main.task_steps SELECT * FROM legacy.task_steps;
DETACH DATABASE legacy;
"

# 3) 符号图谱是可重建的派生数据，迁移后重建
cw refresh --all
```

旧 `<hash>/` 目录保留作备份，确认无误后可手动删除。

---

## 安装命令

### `install`：一键级联安装依赖与 Git Hooks

Call Warden 提供独立的安装器脚本，级联安装核心依赖 + 各语言 tree-sitter grammar + 可选依赖，
并可通过 `--hooks` 一条命令装齐三种 Git hook（推荐的开箱即用方式）。

```bash
cw install [options]
cw install --hooks [--force-hooks] [--no-post-commit]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--all` | 否 | 安装全部依赖（含可选依赖：semgrep / sentence-transformers / numpy；sqlite-vec 当前未接入生产代码） |
| `--lang <LANG...>` | 否 | 仅安装指定语言的 grammar（空格分隔多个语言名） |
| `--check` | 否 | 仅检查依赖状态，不安装 |
| `--hooks` | 否 | 安装 Git hooks 到 `.git/hooks`（pre-commit + pre-push + post-commit 三种） |
| `--force-hooks` | 否 | 强制覆盖已存在的非 Call Warden hooks |
| `--no-post-commit` | 否 | 跳过安装 post-commit hook（仅装 pre-commit + pre-push） |
| `--no-optional` | 否 | 显式跳过可选依赖（默认行为） |
| `--verbose` | 否 | 显示详细安装日志（pip 输出） |
| `--agent` | 否 | 自动检测已安装的 AI 工具并安装 Call Warden MCP 集成 |
| `--detect-agents` | 否 | 仅检测已安装的 AI 工具，不执行安装 |
| `--force-agent` | 否 | 强制覆盖已有的 AI 工具 MCP 配置 |
| `--agent-project` | 否 | 在项目级安装 AI 工具集成，而非全局安装 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> `install` 为本地安装器，不经 daemon：0=全部成功；1=部分失败
> （查看输出中的 `[失败]` 行）；2=pip 不可用或网络错误。

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

#### 设计原则

1. **级联安装**：核心 → 已支持语言 → 扩展语言 → 可选依赖
2. **失败不中断**：单个包安装失败只警告，继续安装其他包
3. **状态可见**：每个包安装前后打印状态（已安装 / 安装中 / 成功 / 失败）
4. **幂等**：重复运行不会出错，已安装的包会跳过

#### Git hooks 安装（--hooks）

一条命令装齐三种 Git hook，开箱即用：

```bash
cw install --hooks
# 已安装：pre-commit + pre-push + post-commit
```

三种 hook 的职责：

| Hook          | 触发时机        | 作用                                                            | 依赖                                                |
| ------------- | --------------- | --------------------------------------------------------------- | --------------------------------------------------- |
| `pre-commit`  | `git commit` 前 | 刷新代码图谱（`cw refresh --all`），确保数据库与代码同步        | 无                                                  |
| `pre-push`    | `git push` 前   | 运行 check-gate 门禁（需 `export CALLWARDEN_TASK_ID=<T-xxx>`）  | 可选（未设置则跳过）                                |
| `post-commit` | `git commit` 后 | 自动捕获变更到 task/audit 闭环（`cw task capture-diff --auto`） | active_task 持久化字段（Schema v30+，无需环境变量） |

`post-commit` hook 默认使用 `--auto` 模式：
- 通过 `workspaces.active_task_id` 字段自动检测当前 `in_progress` 状态的任务（P1 引入，替代 `CALLWARDEN_TASK_ID` 环境变量）
- `task_next_step` 进入 `in_progress` 时自动写入 `active_task_id`
- `task_close` 时自动清除
- fail-soft：没有 in_progress 任务 / 数据库锁 / 异常时静默跳过，不影响 commit

#### 跳过 post-commit

如已有自定义 post-commit 流程，可跳过：

```bash
cw install --hooks --no-post-commit
# 仅安装 pre-commit + pre-push
```

#### 强制覆盖

```bash
cw install --hooks --force-hooks
# 强制覆盖已存在的非 Call Warden hooks
```

#### 幂等性

- 重复运行 `cw install --hooks` 不报错，已安装的 hook 会覆盖更新
- 非 Call Warden 生成的 hook（无 marker）默认保留，需 `--force-hooks` 才覆盖

**示例**

```bash
cw install
cw install --all
cw install --lang csharp ruby
cw install --check
cw install --hooks
cw install --hooks --no-post-commit
cw install --hooks --force-hooks
```

---

### `install-hook`：单独管理单个 Git Hook

Call Warden 也提供独立的 Git Hook 安装命令，用于单独安装/卸载 `post-commit` hook。

> **注意**：`cw install --hooks` 已默认包含 post-commit（--auto 模式），
> 通常无需单独执行此命令。此接口保留用于单独卸载 post-commit 或
> 安装硬编码 task_id 的 post-commit（如 CI 流水线场景）。

```bash
cw install-hook post-commit [--task-id <TASK_ID>] [--uninstall]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `hook` | 是 | Hook 名称（目前仅支持 `post-commit`：commit 后自动捕获变更） |
| `--task-id <ID>` | 否 | 硬编码 task_id 到 hook（不指定时使用 `--auto` 模式自动检测 in_progress 任务） |
| `--uninstall` | 否 | 卸载 hook（仅删除 Call Warden 生成的 hook，保护用户自定义 hook） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 本地 git hook 操作，不经 daemon：1=安装/卸载失败（如不在 git 仓库内）。

#### 安装（--auto 模式，推荐）

```bash
cw install-hook post-commit
# 之后直接 commit，hook 自动检测 in_progress 状态的任务并捕获变更
git commit -m "feat: xxx"
```

默认使用 `--auto` 模式，无需手动 `export CALLWARDEN_TASK_ID`。hook 调用 `cw task capture-diff --auto`，自动查找当前 `in_progress` 状态的任务，取 `HEAD~1` 作为 base，fail-soft 捕获变更到 change_audit / task_symbol_changes 表。

#### 安装（硬编码 task_id）

```bash
cw install-hook post-commit --task-id T-1783349079762-8246
```

绑定到固定任务，适合长时间专注单一任务的场景。

#### 卸载

```bash
cw install-hook post-commit --uninstall
```

#### fail-soft 设计

- `--auto` 模式下没有 in_progress 任务时静默跳过，不影响 commit
- 数据库锁或异常时打印提示但用 `|| true` 兜底退出码
- 双层 fail-soft（DB 层 + CLI 层）确保不影响 git commit

**示例**

```bash
cw install-hook post-commit
git commit -m "feat: xxx"          # hook 自动检测 in_progress 任务并捕获变更
cw install-hook post-commit --task-id T-1783349079762-8246
cw install-hook post-commit --uninstall
```

---

### `install-agent`：生成 AI Agent 集成包

为 23 种主流 AI Agent 生成 Call Warden 集成文件（MCP 配置 + rules/skill + hooks）。

```bash
cw install-agent <AGENT|all> [--output-dir <DIR>] [--force] [--global] [--auto-detect] [--registry <JSON>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<AGENT\|all>` | 是 | 目标 Agent（23 个受支持 Agent 之一）或 `all`（全部 Agent） |
| `--output-dir <DIR>` | 否 | 输出目录，默认 `.callwarden/agent-integrations` |
| `--force` | 否 | 覆盖已存在的集成文件 |
| `--global` | 否 | 写入用户全局 MCP 配置而非项目级 bundle |
| `--auto-detect` | 否 | 自动检测已安装的 AI 工具并为所有检测到的工具安装 |
| `--registry <JSON>` | 否 | 自定义 AI 工具注册表 JSON 路径（扩展内置工具列表） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 本地文件生成操作，不经 daemon：1=失败（无效 agent 名 / 写入错误）。

**支持的 Agent（23 个）**：

| Tier | Agent           | 项目级 MCP 配置                | 全局 MCP 配置                                                | 格式                           | 额外文件                            |
| ---- | --------------- | ------------------------------ | ------------------------------------------------------------ | ------------------------------ | ----------------------------------- |
| T1   | claude-code     | `.mcp.json`                    | `~/.claude.json`                                             | JSON mcpServers                | settings.json hooks + CALLWARDEN.md |
| T1   | claude-desktop  | N/A                            | `claude_desktop_config.json`                                 | JSON mcpServers                | N/A                                 |
| T1   | cursor          | `.cursor/mcp.json`             | `~/.cursor/mcp.json`                                         | JSON mcpServers                | `.cursor/rules/callwarden.mdc`      |
| T1   | trae            | `.trae/mcp.json`               | `~/.trae/mcp.json`（Win: `TRAE SOLO CN\User\mcp.json`）      | JSON mcpServers                | CALLWARDEN.md                       |
| T1   | qoder           | `.qoder/mcp.json`              | `~/.mcp.json`                                                | JSON mcpServers                | N/A                                 |
| T1   | antigravity     | `.antigravity/mcp_config.json` | `~/.gemini/config/mcp_config.json`                           | JSON mcpServers                | `callwarden.md`                     |
| T1   | gemini-cli      | `.gemini/settings.json`        | `~/.gemini/settings.json`                                    | JSON mcpServers                | N/A                                 |
| T1   | codex           | `.codex/.mcp.json`             | `~/.codex/config.toml`                                       | **TOML** `[mcp_servers]`       | 完整插件包                          |
| T2   | windsurf        | `.windsurf/mcp_config.json`    | `~/.codeium/windsurf/mcp_config.json`                        | JSON mcpServers                | `callwarden.md`                     |
| T2   | opencode        | `.opencode/opencode.json`      | `~/.config/opencode/opencode.json`                           | JSON mcpServers                | N/A                                 |
| T2   | jetbrains-junie | `.junie/mcp/mcp.json`          | N/A（仅项目级）                                              | JSON mcpServers                | N/A                                 |
| T2   | cline           | `.cline/mcp.json`              | `~/.cline/mcp.json`                                          | JSON mcpServers                | N/A                                 |
| T2   | cline-cli       | `.cline/mcp.json`              | `~/.cline/mcp.json`                                          | JSON json_mcp_servers          | N/A                                 |
| T2   | devin-cli       | `.devin/config.json`           | `~/.config/devin/config.json`                                | JSON json_mcp_servers          | N/A                                 |
| T2   | kimi-code       | `.kimi-code/mcp.json`          | `~/.kimi-code/mcp.json`（启动加 `--mcp-config`）             | JSON mcpServers                | N/A                                 |
| T2   | codebuddy-cli   | `.codebuddy/mcp.json`          | `~/.codebuddy/mcp.json`                                      | JSON mcpServers                | N/A                                 |
| T2   | deep-code       | `.deepcode/mcp.json`           | `~/.deepcode/mcp.json`                                       | JSON mcpServers                | N/A                                 |
| T2   | kiro (AWS)      | `.kiro/mcp.json`               | `~/.kiro/mcp.json`                                           | JSON mcpServers                | `callwarden.md`                     |
| T2   | comate          | `.comate/mcp.json`             | `~/.comate/mcp.json`                                         | JSON mcpServers                | N/A                                 |
| T3   | zed             | `.zed/settings.json`           | `~/.config/zed/settings.json`（Win: `AppData/Roaming/Zed/`） | JSON **context_servers**       | N/A                                 |
| T3   | pearai          | `.pearai/mcp.json`             | `~/.pearai/mcp.json`                                         | JSON mcpServers（兼容 Cursor） | N/A                                 |
| T3   | grok-build      | `.grok/mcp.json`               | `~/.grok/mcp.json`                                           | JSON json_mcp_servers          | N/A                                 |
| T3   | zcode           | `.zcode/mcp.json`              | `~/.zcode/mcp.json`                                          | JSON json_mcp_servers          | N/A                                 |

> **市场发布类**（不可脚本写入）：Comate AI IDE（百度 MCP World）、CodeBuddy IDE（IDE 设置面板）、华为云码道（IDE 配置，公测中）— 需在对应平台手动发布。

**模式说明**：
- **项目级**（默认）：生成到 `.callwarden/agent-integrations/<agent>/`，可入库共享给团队
- **全局**（`--global`）：安全合并到用户全局配置，开箱即用

**安全合并**（三种格式）：
- **merge_mcpServers**（标准 JSON）：读取现有 JSON → 在 `mcpServers` 下添加 `callwarden` 条目 → 原子写入
- **merge_context_servers**（Zed JSON）：合并到 `context_servers` 字段，`command` 为嵌套对象
- **toml_mcp_servers**（Codex TOML）：合并到 `[mcp_servers.callwarden]` 节，保留其他 TOML 节

**自动检测**：`cw install --detect-agents` 通过三层策略（CLI 命令 → `~/` 配置目录 → Windows `%APPDATA%`/`%LOCALAPPDATA%` 路径）发现已安装 Agent，配合 `cw install --agent` 自动写入对应 MCP 配置。

**示例**

```bash
cw install-agent claude-code              # 单个 agent 的项目级集成模板
cw install-agent all                      # 所有 agent 的项目级集成模板
cw install-agent claude-code --global     # 写入用户全局 MCP 配置（安全合并，不覆盖已有配置）
cw install-agent cursor --output-dir /path/to/output
cw install-agent all --force              # 覆盖已有文件
```

---

### `setup`：自动配置已安装 AI 工具的 MCP 集成

探测本机已安装的 AI 编码工具（通过 CLI 命令、配置目录、Windows 注册表三层策略），自动将 Call Warden MCP Server 配置写入对应的 MCP 配置文件。

```bash
cw setup [--force] [--dry-run]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--force` | 否 | 强制重新配置（删除已完成标记文件，重新写入配置） |
| `--dry-run` | 否 | 仅探测不写入（显示检测结果但不修改任何配置文件） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 本地配置写入操作，不经 daemon。**输出示例**：

```
检测到 3 个 AI 工具：
  [CLI] Claude Code (claude-code)
       -> claude 命令可用
  [CFG] Cursor (cursor)
       -> ~/.cursor/mcp.json 已存在
  [WIN] Trae IDE (trae)
       -> ~/AppData/Roaming/TRAE SOLO CN/User/mcp.json 已存在

已为 3 个工具配置 CW MCP Server
```

**探测策略**（三层）：
1. **CLI 命令**：检查 `claude`、`cursor`、`trae` 等命令是否在 PATH 中
2. **配置目录**：检查 `~/.cursor`、`~/.trae` 等目录是否存在
3. **Windows 路径**：检查 `%APPDATA%`/`%LOCALAPPDATA%` 下的配置目录

**幂等性**：首次运行后创建标记文件 `~/.callwarden/.auto_setup_done`，后续运行自动跳过，除非使用 `--force`。

> **提示**：`cw setup` 在首次运行 `cw` 时也会自动触发（Lazy Auto-Setup），可通过 `--no-auto-setup` 全局参数禁用。

**示例**

```bash
cw setup              # 探测并配置（首次运行会写入配置，后续运行跳过）
cw setup --dry-run    # 仅探测不写入（查看已安装的 AI 工具）
cw setup --force      # 强制重新配置（忽略已完成标记）
```

---

## 构建命令

### `refresh [PATH...]`：构建/增量刷新代码图谱

刷新代码图谱：`--all` 全量/增量刷新工作区，或指定文件路径增量刷新（支持多路径，C8 Step #5）。
`refresh` 始终是写操作（`build_full_graph` / `refresh_file`），需要已激活 workspace；
文件监控（watch）模式由独立入口 `cw-agent` 提供，`refresh` 的 argparse 已无 `--watch` 参数。

```bash
cw refresh --all [--force]
cw refresh <PATH...>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[PATH...]` | 否 | 待刷新的文件路径（支持多路径；不传 PATH 时须给 `--all`） |
| `--all` | 否 | 刷新工作区内的所有文件 |
| `--force` | 否 | 强制全量重新解析（仅配合 `--all`） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写面命令：refresh 经 PROTECTED_MUTATION 保护。`--all` 增量刷新仅解析变更文件，
> 不会清空数据；`--force` 强制全量重建。刷新后自动同步 AGENTS.md（fail-soft，
> 不阻断 refresh）。指定文件增量更新时重新解析符号和调用关系，多文件输出汇总
> （成功数/失败数/总耗时），失败文件单独列出。

**示例**

```bash
# 增量刷新（默认，仅解析变更文件，不会清空数据）
cw refresh --all

# 强制全量重新解析
cw refresh --all --force

# 指定工作区
cw --workspace /path/to/project refresh --all

# 刷新单个文件
cw refresh src/payment/mod.rs

# 同时刷新多个文件（C8 Step #5 新增支持）
cw refresh src/payment/mod.rs src/auth/login.py src/db/query.rs
```

多文件刷新输出示例：

```
Refresh: src/payment/mod.rs (rust)
Refreshed: src/payment/mod.rs
Refresh: src/auth/login.py (python)
Refreshed: src/auth/login.py
Refresh summary: success 2 / failure 0 / total 2, elapsed 0.04s
```

### `status`：查看状态

```bash
cw status
```

显示工作区、文件分布、符号分布、调用关系统计、上次构建时间等。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw status
```


### `stats`：统计信息（JSON）

```bash
cw stats
```

以 JSON 格式输出统计信息，便于脚本解析。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw stats
```


### `graph build-from-c <DIRECTORY>`：Rust 并行构建 GraphStore（F11）

从 C 文件列表用 rayon 并行构建内存 GraphStore（CSR），供后续调用链查询。
只读实验命令（仅解析 + 内存构 CSR，不写数据库；可选 dump 到 `.cwsnap` 文件不算写 DB）。

```bash
cw graph build-from-c <DIRECTORY> [--threads <N>] [--dump <FILE>] [--max-files <N>] [--query <QN>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<DIRECTORY>` | 是 | 目标目录（递归扫描 .c 文件） |
| `--threads <N>` | 否 | rayon 线程数（默认 None=自动） |
| `--dump <FILE>` | 否 | 将 GraphStore dump 到 `.cwsnap` 文件（可选） |
| `--max-files <N>` | 否 | 最大文件数（避免误扫描超大目录，默认 10000） |
| `--query <QN>` | 否 | 构建后查询指定符号的 callers（可选，用于自检） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw graph build-from-c examples/c_project                   # 递归扫描 .c 文件构建 CSR
cw graph build-from-c examples/c_project --threads 8       # 指定 rayon 线程数（默认自动）
cw graph build-from-c examples/c_project --dump out.cwsnap # 构建后 dump 到 .cwsnap 快照
cw graph build-from-c examples/c_project --max-files 5000  # 限制最大文件数
cw graph build-from-c examples/c_project --query <QN>      # 构建后查询指定符号 callers（自检）
```

---

## 查询命令

### `search <QUERY>`：符号搜索

```bash
cw search "login"
cw search "User" --kind class
cw search "handle" --limit 20
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<QUERY>` | 是 | 搜索关键词（模糊匹配） |
| `--kind <KIND>` | 否 | 类型过滤：fn/method/class/struct/enum/trait/interface |
| `--limit <N>` | 否 | 返回数量（默认 50） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw search process_payment --kind function --limit 10
```


### `grep <PATTERN...>`：带符号上下文的文本搜索

```bash
cw grep "parse_file"
cw grep parse_file MP_THRESHOLD          # 多关键词 = AND（同一行都命中）
cw grep "import time" --fixed            # 固定字符串（rg -F），不作正则
cw grep "TODO" --include-all             # 含符号外的命中（imports/docs/comments）
cw grep "handle" --kind fn --limit 50    # 仅 fn 内命中，最多 50 条
cw grep "config" --path src/             # 限定搜索路径
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<PATTERN>...` | 是 | 一个或多个搜索模式；多个 = AND（须在同一行全部命中）；加引号视为单个模式 |
| `--fixed` | 否 | 按固定字符串处理（等价 `rg -F`），不解析为正则 |
| `--limit <N>` | 否 | 最大命中数（默认 200，在符号过滤后应用） |
| `--path <PATH>` | 否 | 搜索路径（默认工作区根） |
| `--include-all` | 否 | 包含符号外命中（imports/docs/comments）；默认只显示符号内命中 |
| `--kind <KIND>` | 否 | 仅显示该类型符号内的命中（fn/class/...） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

在工作区内做文本搜索，并为每个命中行附加符号上下文（`file:line [in fn xxx] content`）。
底层是 ripgrep + `find_symbols_at_lines` 组合，能回答"这行代码在哪个函数里"，是 `grep`/`rg`
做不到的。默认只显示落在符号内部的命中（过滤掉 import/注释等无符号行）。

**示例**

```bash
cw grep "parse_file" --kind fn --limit 50
cw grep "TODO" --include-all
```

### `symbol <QN>`：符号详情

```bash
cw symbol "my_project::payment::process_payment"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<QN>` | 是 | 限定符号名 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

显示符号的类型、深度、文件位置、签名、注释、调用关系（调用的函数 + 被谁调用）。

**示例**

```bash
cw symbol "my_project::payment::process_payment"
```

### `file <PATH>`：文件内符号

```bash
cw file src/payment/mod.rs
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<path>` | 是 | File path |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw file src/payment/mod.rs
```


### `query <NAME> <FILE>`：精确查询位置

```bash
cw query process_payment src/payment/mod.rs
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<name>` | 是 | Symbol name |
| `<file>` | 是 | File path |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw query process_payment src/payment/mod.rs
```


### `callers <NAME>`：查询谁调用这个函数

```bash
cw callers process_payment
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<NAME>` | 是 | 符号名 |
| `--qualified <QUALIFIED>` | 否 | 完整限定名（如 module::Class::method），精确匹配避免跨模块误匹配 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw callers process_payment
cw callers handle_click --qualified "ui::Button::handle_click"
```

### `callees <NAME>`：查询这个函数调用了谁

```bash
cw callees process_payment
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<NAME>` | 是 | 符号名 |
| `--qualified <QUALIFIED>` | 否 | 完整限定名（如 module::Class::method），精确匹配避免跨模块误匹配 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw callees process_payment
cw callees main --qualified "app::main"
```

### `topo`：拓扑排序

```bash
cw topo
cw topo --limit 100
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--limit <N>` | 否 | 最大结果数（默认 50） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

按依赖深度排序，底层（被调用最多）在前。

**示例**

```bash
cw topo --limit 100
```

### `fts rebuild`：重建 FTS5 全文索引

从 symbols 表重建 FTS5 索引，用于修复中断刷新后全文搜索为空的问题。

```bash
cw fts rebuild
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写面命令：`fts rebuild` 走 PROTECTED_MUTATION 保护（`rebuild_fts_index`），
> 需已激活 workspace。输出：`symbols_count` / `fts_rows` / `triggers_recreated` /
> `elapsed`；若 `fts_rows != symbols_count` 打印 mismatch 警告。

**示例**

```bash
cw fts rebuild
```

### `fts status`：查看 FTS5 索引状态

查看 FTS5 索引状态（行数、triggers、一致性）。只读命令（不写数据库，不触发 workspace 激活）。

```bash
cw fts status
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw fts status
```

---

## 调用链分析命令

### `impact <SYMBOL_HASH>`：变更影响半径分析（向上）

```bash
cw impact <symbol_hash>
cw impact <symbol_hash> --depth 5
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<symbol_hash>` | 是 | 源符号内容 hash |
| `--depth <N>` | 否 | BFS 遍历最大深度（默认 3） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

以符号为起点，沿反向调用图向上 BFS 扩散，按层级显示所有调用该符号的上游函数与受影响范围，并输出受影响调用者的跨层分布（代码/DB/API/配置）。入参为符号内容 hash（可先用 `cw symbol <QN>` 查得），不是限定名。

**示例**

```bash
cw impact abc123def456
cw impact abc123def456 --depth 5
```

### `call-chain <NAME>`：向下调用链

```bash
cw call-chain "my_project::payment::process_payment"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<NAME>` | 是 | 符号名 |
| `--depth <N>` | 否 | 最大深度（默认 10） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw call-chain "my_project::payment::process_payment" --depth 5
```

### `top-callers [N]`：被调用最多排行

```bash
cw top-callers          # 默认 20
cw top-callers 50
cw top-callers --module "src/api"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[limit]` | 否 | 最大结果数（默认 20） |
| `--module <MODULE>` | 否 | 模块过滤（前缀） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw top-callers 50 --module "src/api"
```

### `deepest [N]`：调用深度最深

```bash
cw deepest
cw deepest 50
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[limit]` | 否 | 最大结果数（默认 20） |
| `--module <MODULE>` | 否 | 模块过滤（前缀） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw deepest 50 --module "src/core"
```

### `detect-cycles`：循环调用检测

```bash
cw detect-cycles
cw detect-cycles --depth 15
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--depth <N>` | 否 | 循环检测最大深度（默认 10） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw detect-cycles --depth 15
```

### `module-calls [N]`：模块间调用统计

```bash
cw module-calls
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[limit]` | 否 | 最大结果数（默认 20） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw module-calls 50
```

### `call-heatmap`：调用频率热力图

```bash
cw call-heatmap              # 默认按 module
cw call-heatmap --by file    # 按 file
cw call-heatmap --limit 30
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--by {module,file}` | 否 | 按模块或文件分组（默认 module） |
| `--limit <N>` | 否 | 前 N 条结果（默认 20） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw call-heatmap --by file --limit 30
```

### `orphan-symbols [KIND]`：孤立符号

```bash
cw orphan-symbols            # 默认 fn
cw orphan-symbols struct
cw orphan-symbols --module "src/legacy"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[kind]` | 否 | 符号类型（默认 fn） |
| `--module <MODULE>` | 否 | 模块过滤（前缀） |
| `--limit <N>` | 否 | 最大结果数（默认 50） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

查找未被调用的孤立函数/结构体，适合清理死代码。

**示例**

```bash
cw orphan-symbols struct --module "src/legacy" --limit 20
```

---

## 安全命令

### `vuln-blast`：漏洞爆炸半径（子命令）

```bash
cw vuln-blast --finding-id 42
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--finding-id <N>` | 否 | 指定 Semgrep finding ID（默认扫描全部） |
| `--severity <SEV>` | 否 | 严重度过滤：ERROR/WARN/INFO |
| `--depth <N>` | 否 | 调用图反向遍历深度（默认 3） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

将 Semgrep findings 与调用图结合，分析漏洞能影响多少下游调用方。返回风险等级 + 每个漏洞的影响树。

> 注：命令自带 epilog 中的「2 = Database locked」为退役口径；锁定等运行时失败按全局退出码契约归 1。

**示例**

```bash
cw vuln-blast
cw vuln-blast --finding-id 42
cw vuln-blast --severity ERROR --depth 5
```

### `guardrail scan`：安全护栏扫描

```bash
cw guardrail scan
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--file <PREFIX>` | 否 | 文件路径前缀过滤 |
| `--category <CAT>` | 否 | 按类别过滤（db_safety/api_compat/incident） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

扫描 DB/API/Incident 三类可阻断规则。

**示例**

```bash
cw guardrail scan --file src/db/
cw guardrail scan --category db_safety
```

### `guardrail rules`：列出护栏规则

```bash
cw guardrail rules
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--category <CAT>` | 否 | 按类别过滤 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw guardrail rules --category api_compat
```

### `semgrep scan [PATH...]`：Semgrep 扫描

```bash
cw semgrep scan src/payment/ src/api/
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[PATH...]` | 否 | 扫描路径（为空则扫描整个工作区） |
| `--config <CONFIG>` | 否 | 规则配置（默认 `p/default`） |
| `--lang <LANG...>` | 否 | 限制语言 |
| `--timeout <N>` | 否 | 超时秒数（默认 180） |
| `--quick` | 否 | 快速汇总模式 |
| `--save` | 否 | 扫描结果存入数据库 |
| `--incremental` | 否 | 增量扫描模式：只扫 git diff 变更文件，scan_type='incremental'，清理 stale findings（A14） |
| `--base <BRANCH>` | 否 | 增量扫描基准分支（默认 `main`，A14） |
| `--head <REF>` | 否 | 增量扫描目标提交（默认 `HEAD`，A14） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw semgrep scan
cw semgrep scan --quick
cw semgrep scan --save
cw semgrep scan --config p/security --lang rust typescript
cw semgrep scan --incremental
cw semgrep scan --incremental --base develop --head HEAD
cw semgrep scan --incremental --config p/security
```

### `semgrep stats`：Semgrep 统计

```bash
cw semgrep stats
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw semgrep stats
```

### `semgrep list [FILTER]`：Semgrep findings 列表

```bash
cw semgrep list
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[FILTER]` | 否 | 规则 ID 过滤 |
| `--severity <SEV>` | 否 | 按严重程度过滤 |
| `--lang <LANG>` | 否 | 按语言过滤 |
| `--limit <N>` | 否 | 最大结果数（默认 50） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw semgrep list --severity ERROR
cw semgrep list --lang rust
```

---

## 编辑与注释命令

### `restore-comment <SPEC>`：恢复函数注释

```bash
cw restore-comment <SPEC> [--preview]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<SPEC>` | 是 | 恢复规格：`file:symbol@version` 或 `file:line` |
| `--preview` | 否 | 仅预览，不写入 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

SPEC 格式：`文件路径:符号名@版本号` 或 `文件路径:行号`。写入前先用 `--preview` 确认将要恢复的目标版本。

**示例**

```bash
# 预览
cw restore-comment "src/payment/mod.rs:process_payment@3" --preview

# 实际写入
cw restore-comment "src/payment/mod.rs:process_payment@3"
```

### `restore-all-comments`：批量恢复

```bash
cw restore-all-comments [--preview] [--file-filter <PREFIX>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--preview` | 否 | 仅预览，不写入 |
| `--file-filter <PREFIX>` | 否 | 仅恢复匹配此前缀的文件 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
# 预览全部
cw restore-all-comments --preview

# 恢复指定文件
cw restore-all-comments --file-filter src/payment/
```

### `symbol-history <SYMBOL_HASH>`：符号 Git 变更历史

```bash
cw symbol-history <symbol_hash>
cw symbol-history <symbol_hash> --limit 50
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<symbol_hash>` | 是 | 符号内容 hash（先用 `cw symbol <QN>` / `cw search` 查得；命令不解析符号名） |
| `--limit <N>` | 否 | 返回数量限制（默认 20） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

按内容 hash 精确匹配 `git_symbol_changes`，列出改动过该符号的 commit（变更类型/作者/时间/message）；输出末尾追加 symbol → task 三角关联段（schema v35+），列出该符号被哪些任务改变过、对应的 `source_commit_hash` 与 `change_type`（对应 MCP 工具 `get_symbol_change_tasks`）。

> 注：argparse 只接受单一位置参数 hash；早期文档示例 `cw symbol-history process_payment --show-content` 的「符号名 + `--show-content`」形态不存在（`--show-content` 不是合法 flag）。

**示例**

```bash
cw symbol-history abc123def456
cw symbol-history abc123def456 --limit 50
```

### `diff <H1> <H2>`：按 hash 对比两个符号内容版本

```bash
cw diff <hash1> <hash2>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<hash1>` | 是 | 第一个内容哈希 |
| `<hash2>` | 是 | 第二个内容哈希 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw diff a1b2c3d4e5f6... d4e5f6a1b2c3...
```

### `changes [SINCE]`：最近变更的文件与函数

```bash
cw changes               # 最近 1 小时（默认）
cw changes 1d            # 最近 1 天
cw changes 2h30m         # 支持 h/d/m/s 组合窗口
cw changes 6h --detail   # 附带变更函数的 prev/curr hash
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[SINCE]` | 否 | 时间窗口（`30m`/`1h`/`1d`/`2h30m` 等 h/d/m/s 组合，默认 `1h`） |
| `--detail` | 否 | 显示每个变更函数的 prev/curr 内容 hash |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

[2] Query & Search 分类下的顶层命令（只读查询）：按时间窗口列出近期变更的
多版本文件与函数，常用于在 `symbol-history` / `diff` 之前先定位"最近改了什么"。

输出：变更文件总数、多版本文件数、变更函数数，随后列出多版本文件
（`v<版本号> | 解析时间 | 路径`）与变更函数（`[变更类型] 限定名` +
`文件:行 | 时间`）。数据来自 `file_versions`，多版本即 `version_num > 1`。

**示例**

```bash
cw changes 1d --detail
```

---

## 任务管理命令

### `task create`：创建任务

父任务创建（GATE-1B）使用 `--parent-id`。显式传入 daemon 返回的
`--workspace-id` 和 `--workspace-instance-id` 作为父子 workspace 一致性断言，
并提供 `--role-contracts` 与 `--identity-policy`。需要自定义 Task Contract 时，
使用 `--task-contract-envelope '<JSON>'`；该字符串原样转发，由 daemon 校验。
如 envelope 指定 contract_id，可用 `--task-id` 提供与之对应的任务 ID。
父任务、合同、策略和 workspace 校验均在 daemon 中完成；客户端不补造治理合同，
不选择 active workspace，不回退本地数据库。未知参数在发送前拒绝，daemon
返回的父任务不存在、workspace 不匹配、合同或策略缺失错误不重新分类。
未使用这些新参数的根任务请求保持原有默认三角色合同和 legacy policy 行为。

```bash
cw task create \
  --title "为支付函数添加注释" \
  --desc "补全 process_payment 的文档注释" \
  --steps '[{"action":"annotate","target_file":"src/payment/mod.rs","target_symbol":"process_payment"}]'
```

**workspace 配对（BR-02，role-prompt-v1-bootstrap-br02-python-client）**：
`task create` 的 workspace 绑定必须来自 daemon 权威解析，客户端一律不猜数字、
不合成 `ws-{id}`、不回退 active workspace：

- 根任务缺省：经 daemon `workspace.list` 和 `workspace.status` 查询当前项目根目录
  唯一一致的 canonical workspace 配对，原样转发给 `task.create`；
- 显式覆盖：`--workspace-id <id>` / `--workspace-instance-id <inst>` 可显式传入，
  但 daemon 仍做 0c 配对校验（不一致 → `E_WORKSPACE_AUTHORITY_MISMATCH`）；
- **fail-closed**：workspace 未在 daemon 注册（如 legacy SQLite `is_active`
  id 不在注册表）、无 active workspace、或 daemon 不可达时，一律明确报错，
  **绝不创建本地任务**（`E_TASK_WORKSPACE_INSTANCE_REQUIRED` /
  `E_WORKSPACE_AUTHORITY_MISMATCH` / `DaemonUnavailableError`）。

创建成功后输出 workspace provenance（`workspace_id` / `workspace_instance_id` /
`workspace_binding_id` / `workspace_capture_id` / `assignment_id` / `step_count`），
并自动对 `task.status` readback 做逐一对比，任一标识不一致即 fail-closed 上抛。

```bash
# 显式指定 daemon 注册的 workspace 配对（须与 daemon 权威一致）
cw task create --title "t" --workspace-id 72 --workspace-instance-id 2bba6e894ee2546f
```

**Rust `cw` 的 workspace 配对（BR-03，role-prompt-v1-bootstrap-br03-rust-cli）**：
Rust 发行版 `cw`（`cw_cli.rs`）与 Python CLI 保持同一 daemon binding parity：

- 新增 flag：全局 `--workspace-id <numeric_id>`（daemon 权威 numeric id，整数校验，
  非整数显式值 fail-closed）与 `task create` 子命令级 `--workspace-instance-id <inst>`；
  缺省（或只给其一）时，缺失项经
  `mcp.daemon_client.inject_workspace_id` → `workspace.status` 从 daemon 权威解析，
  任一步失败 fail-closed；
- **local numeric active 语义绝不进入 enterprise 参数**：`task create` 的
  enterprise 路径从不使用 `resolve_local_workspace_id`（本地 SQLite `is_active`），
  pair 只来自 daemon 解析或显式 flag，原样转发 `task.create`（daemon 0c 配对校验
  兜底，不一致 → `E_WORKSPACE_AUTHORITY_MISMATCH`）；
- **local 模式 / daemon 不可用一律 fail-closed**：`task create` 不再走本地 SQLite
  回退，绝不创建无 workspace binding 的本地任务（对齐 Python `_local_create_forbidden`）；
- **identity_policy parity**：与 Python BR-02 一致，Rust `task create` 显式转发
  `identity_policy: "legacy_identity_v1"`（默认三角色 legacy 模板），由 daemon 生成
  generic envelope；缺失会报 `E_TASK_IDENTITY_POLICY_REQUIRED` 且新任务无法 claim；
- create 响应 JSON 含 workspace provenance 五键（`workspace_id` /
  `workspace_instance_id` / `workspace_binding_id` / `workspace_capture_id` /
  `assignment_id`）+ `step_count`，并自动对 `task.status` readback 逐一对比，
  任一标识不一致即 fail-closed 上抛（`verify_create_readback`）。

```bash
# Rust cw：缺省从 daemon 解析权威 pair
cw task create --title "t" --desc "d" --steps '[{"action":"inspect","target_file":"src/main.rs"}]'
# Rust cw：显式 pair（daemon 0c 校验）
cw task create --title "t" --workspace-id 72 --workspace-instance-id 2bba6e894ee2546f
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--title <TITLE>` | 是 | 任务标题 |
| `--desc <DESC>` | 否 | 任务描述 |
| `--steps <STEPS>` | 否 | 步骤 JSON 数组，例如 [{"action":"annotate","target_file":"a.py"}] |
| `--role-contracts <ROLE_CONTRACTS>` | 否 | JSON array of role contract objects (keys: role, skill_id, skill_version, prompt_template_id, prompt_hash, allowed_paths, forbidden_paths, commands, acceptance_checks, required_evidence, handoff_to, independence). If omitted, the A' standard three-role (executor/reviewer/adjudicator) legacy contract template is used. Tasks are never created without contracts. |
| `--workspace-id <WORKSPACE_ID>` | 否 | 显式 daemon workspace id（缺省从 daemon 解析，fail-closed） |
| `--workspace-instance-id <WORKSPACE_INSTANCE_ID>` | 否 | 显式 daemon workspace 权威 capture instance id（缺省经 workspace.status 从 daemon 解析） |
| `--parent-id <PARENT_ID>` | 否 | 父任务 ID；须显式提供 daemon workspace 配对与合同 |
| `--identity-policy <IDENTITY_POLICY>` | 否 | 显式 daemon 身份策略；治理创建不推断默认策略 |
| `--task-contract-envelope <TASK_CONTRACT_ENVELOPE>` | 否 | Task Contract JSON envelope，原样传给 daemon |
| `--task-id <TASK_ID>` | 否 | 与所提供 Task Contract envelope 一致的显式任务 ID |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task create --title "修复登录超时" --desc "登录 30s 超时" --steps '[{"action":"fix","target_file":"src/auth.rs"}]'
```


### `task next`：领取下一步骤

```bash
cw task next <task_id>
```

返回下一步骤详情。若步骤为编辑类操作，系统自动调用护栏检查：
- `guardrail_alert`（block）：步骤被阻塞，需先处理告警
- `guardrail_warning`（warn）：可执行，但需关注告警

**active_task 持久化**（v30 新增）：`task next` 成功后自动将 `task_id` 写入
当前 workspace 的 `active_task_id` 字段，替代 `CALLWARDEN_TASK_ID` 环境变量。
后续 `cw task capture-diff --auto` 会优先从该字段读取 task_id，无需手动 export。
连续 claim 多个任务时后者覆盖前者。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--remediation-step-id <REMEDIATION_STEP_ID>` | 否 | Explicit daemon-issued fix_defect step ID to claim |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task next T-123 --agent-id executor --session-id s-exec --model-id m1 --role executor --agent-instance-id inst-exec
```


### `task next-action`：查询系统派工（只读）

```bash
cw task next-action <task_id>
cw task next-action <task_id> --json
```

`task.next_action` 是任务下一合法动作的唯一权威来源：daemon 侧 evaluator
按任务状态/角色/角色合同推导 `decision`（`READY`/`WAITING`/`BLOCKED`/`COMPLETE`）、
`action`（`CLAIM`/`REVIEW`/`ADJUDICATE`/`REVISE`/`WAIT`/`NONE`）、`required_role`、
`step_id`、`routing`（下一棒角色/动作与理由）与 `blocking_conditions`。

同时返回统一治理进度投影：

- `lifecycle_status`：raw `tasks.status`，用于 apply/close 门禁；
- `workflow_status`：`queued`、`execution_in_progress`、`remediation_in_progress`、
  `review_pending`、`adjudication_pending`、`remediation_pending`、
  `applied_pending_close`、`completed` 或 `reverted`；
- `current_role` / `next_role` / `next_action`：当前责任角色与下一条合法动作；
- `review.state`：`not_in_review`、`pending`、`blocked` 或 `passed`，有效 verdict 存在
  时还包含 `verdict_id` 与 `findings_count`；
- `blocking_reasons`：面向用户的阻断原因（`blocking_conditions` 保留兼容）。

因此 Reviewer PASS 仍显示 `lifecycle_status=review`、`workflow_status=adjudication_pending`，
而 BLOCKED 显示 `remediation_pending`；这两个结论都不会伪造 `applied`。CLI 的
`task show` 和 `task governance-projection` 展示 daemon 返回的投影，客户端不自行推导。

- **只读**：不领取步骤、不写任务状态。
- **权威在 daemon**：evaluator 只在 Rust daemon 中实现；daemon 未启动时返回
  `E_DAEMON_UNAVAILABLE`，local 模式无 evaluator（fail-closed），不会本地推算。
- `--json` 输出原始决策供脚本/客户端解析；`cw-task-loop` Skill 依此逐字渲染角色卡。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--workspace-instance-id <WORKSPACE_INSTANCE_ID>` | 否 | Workspace authority capture instance id (daemon binding key) |
| `--json` | 否 | Output raw daemon JSON (machine-readable) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task next-action T-123 --json
```


### `task prompt`：编译 Role Prompt Bundle（只读薄客户端）

```bash
cw task prompt <task_id>
cw task prompt <task_id> --format llm
cw task prompt <task_id> --format card
cw task prompt <task_id> --format json
```

Role Prompt Compiler v1 的 CLI HTTP 薄客户端：唯一 authority 是 daemon RPC
`task.prompt.compile`，daemon 侧完成路由选择（§6 状态机）、role prompt 渲染、
canonical 化与 hash，返回可直接供 LLM 使用的 Role Prompt Bundle（§5.1 schema）。

- **三种本地展示格式**（`--format` 只改变本地展示，不发送给 daemon、不进入任何
  hash；默认 `llm`）：
  - `llm`：逐字输出 bundle 的 `prompt.text`（供 LLM/Agent 直接使用）；
  - `card`：结构化角色卡投影（routing / contract / authorization / authority /
    template / hash 字段），全部逐字复述 daemon 返回值，缺失字段标 `—`；
  - `json`：daemon 返回的完整 bundle JSON 原样输出（与 MCP 工具
    `task_get_role_prompt` 字段与 hash 完全一致）。
- **请求契约**（spec §4.1）：params 恰为 `task_id` + 可选
  `--expected-workspace-instance-id`（caller assertion，仅用于检测调用方缓存的
  authority 是否过期；mismatch 返回 `E_TASK_PROMPT_AUTHORITY_MISMATCH`）。
- **不做 workspace 推导**（spec §4.3）：本命令不复用/扩散 `task next-action` 的
  legacy `derive_workspace_instance_id` fallback；workspace authority 由 daemon
  侧 immutable binding 自解析。
- **fail-closed**：daemon 未启动/不可达时返回 `E_DAEMON_UNAVAILABLE` 结构化错误；
  daemon 业务错误（如 `E_TASK_PROMPT_AUTHORITY_MISMATCH`）原样透传；无本地模板、
  无 SQLite、无 fallback 渲染（enterprise/daemon 模式无本地 authority）。
- **只读**：`task.prompt.compile` 为 READ_ONLY（单 snapshot 8 表零写入），不产生
  lease、claim 或 lifecycle mutation。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | Task ID |
| `--format <{llm,card,json}>` | 否 | Local display format only (llm\|card\|json); never sent to daemon |
| `--expected-workspace-instance-id <EXPECTED_WORKSPACE_INSTANCE_ID>` | 否 | Optional caller authority assertion (spec 4.2); never derived client-side |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task prompt T-123 --format json
```


### `task report`：回报结果

```bash
# 成功
cw task report <task_id> <step_id> --result "已添加注释"

# 失败
cw task report <task_id> <step_id> --result "文件不存在" --fail
```

失败时系统自动插入"修复缺陷"步骤。

**P3 Identity（Req 10.1-10.7）**：可携带 `--agent-id/--session-id/--model-id/--role`
四参数（全部必填，缺失拒绝）记录执行者 Identity，仅作 actor attribution，
不等于 assignment/lease/ownership。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `<step_id>` | 是 | 步骤 ID（可选，留空则做任务级审查） |
| `--result <RESULT>` | 否 | 执行结果描述 |
| `--fail` | 否 | 标记为失败（默认成功） |
| `--evidence-path <EVIDENCE_PATH>` | 否 | Task-bound evidence manifest path (daemon authority only) |
| `--evidence-hash <EVIDENCE_HASH>` | 否 | SHA-256 of the task-bound evidence manifest |
| `--snapshot-id <SNAPSHOT_ID>` | 否 | Authoritative review snapshot reference (must come from snapshot.publish) |
| `--changes-json <CHANGES_JSON>` | 否 | JSON array of explicit step-whitelisted file changes |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |
| `--lease-token <TOKEN>` | 否 | Lease raw token (P4 protected mutation) |
| `--fencing-counter <N>` | 否 | Current fencing counter (P4 protected mutation) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task report T-123 S-1 --result "完成解析器修复" --snapshot-id <SNAPSHOT_ID> --lease-token <TOKEN> --fencing-counter 1 --agent-id executor --session-id s-exec --model-id m1 --role executor
```


### `task rollback`：回滚变更

```bash
cw task rollback <task_id> <step_id>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `<step_id>` | 是 | 步骤 ID（作为 change_id 定位回滚范围） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task rollback T-123 S-1
```


### `task apply`：审核通过任务

```bash
cw task apply <task_id> [--reviewer <identity>] [--agent-id <A> --session-id <S> --model-id <M> --role <R>]
```

将任务状态从 `review` 推进到 `applied`，记录审核通过时间戳 `applied_at`。

**设计原则**：写代码的 Agent 完成任务后状态为 `review`，不能自己 `apply`，
必须由其他会话的 LLM 审核调用，避免基于奖励函数的激励直接 close 任务。

- 仅 `review` 状态的任务可 `apply`，其他状态返回错误
- 成功后 `applied_at` 字段写入当前时间戳
- `--reviewer` 参数标识审核人（默认 `reviewer`）

**P3 Identity（Req 10.1-10.7）**：`--agent-id/--session-id/--model-id/--role` 四参数
共同构成结构化 Identity（全部必填，缺失拒绝）。`task apply` **强制 Reviewer Session
与 Implementer Session 不同**（同 session 返回 `ERR_IDENTITY_SESSION_NOT_SEPARATED`，
保持 `review` 状态）。Identity 仅作 actor attribution，不等于 assignment/lease/ownership。

**父任务禁止手动 apply**：若任务有子任务（即父任务），返回错误
`reason=parent_task_must_cascade`，提示由级联触发。父任务的状态推进由系统在
最后一个子任务 apply 时自动级联完成（`review → applied → closed` 一次性推进）。

**级联 close**：若 apply 后所有兄弟子任务都已 `applied`/`closed`，触发原子级联：
1. close 所有 `applied` 状态的兄弟任务
2. 父任务 `review → applied → closed` 一次性推进
3. 递归向上检查祖父层级联

返回值新增 `cascaded_close: List[str]` 字段（仅触发级联时存在），列出所有自动
close 的 task_id。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--reviewer <REVIEWER>` | 否 | 审核人标识 |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |
| `--lease-token <TOKEN>` | 否 | Lease raw token (P4 protected mutation) |
| `--fencing-counter <N>` | 否 | Current fencing counter (P4 protected mutation) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task apply T-123 --reviewer review-agent --agent-id reviewer --session-id s-rev --model-id m1 --role reviewer --lease-token <TOKEN> --fencing-counter 3
```


### `task close`：关闭任务

```bash
cw task close <task_id> [--reviewer <identity>] [--agent-id <A> --session-id <S> --model-id <M> --role <R>]
```

将任务状态从 `applied` 推进到 `closed`，记录关闭时间戳 `closed_at`。

**设计原则**：关闭操作也必须由其他会话的 LLM 调用，与 `apply` 配合完成
`review → applied → closed` 审核闭环。

- 仅 `applied` 状态的任务可 `close`，其他状态返回错误
- 成功后 `closed_at` 字段写入当前时间戳
- `--reviewer` 参数标识关闭人（默认 `reviewer`）

**P3 Identity（Req 10.1-10.7）**：接受 `--agent-id/--session-id/--model-id/--role`
四参数记录关闭者 Identity；close 仍只收尾，**不强制** session 分离。

**父任务禁止手动 close**：若任务有子任务（即父任务），返回错误
`reason=parent_task_must_cascade` 和 `subtask_count` 字段，提示由级联触发。
父任务的 close 由系统在最后一个子任务 apply 时自动级联完成。

**active_task 清除**（v30 新增）：`task close` 成功后自动清除当前 workspace
的 `active_task_id`（防御性：仅当 `active_task == task_id` 时才清除，避免误清除
后续已 claim 的新任务）。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--reviewer <REVIEWER>` | 否 | 审核人标识 |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |
| `--lease-token <TOKEN>` | 否 | Lease raw token (P4 protected mutation) |
| `--fencing-counter <N>` | 否 | Current fencing counter (P4 protected mutation) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task close T-123 --reviewer review-agent --agent-id adjudicator --session-id s-adj --model-id m1 --role adjudicator --lease-token <TOKEN> --fencing-counter 4
```


### `task reopen`：重新打开任务

```bash
cw task reopen <task_id> [--reviewer <identity>] [--reason "<原因>"] [--agent-id <A> --session-id <S> --model-id <M> --role <R>]
```

将任务状态从 `review`/`applied`/`closed` 回退到 `in_progress`，清理
`applied_at`/`closed_at` 时间戳。用于 code review 发现已 applied/closed 的任务
有问题需要修复，或向已 closed 的父任务挂入新子任务。

**P3 Identity（Req 10.1-10.7）**：接受 `--agent-id/--session-id/--model-id/--role`
四参数记录发起者 Identity；reopen **不强制** session 分离（reopen 不是 apply）。

**active_task 设置**（v30 新增）：`task reopen` 成功后自动将 `task_id` 写入
`active_task_id`（用户显式 reopen 表示要重新开始干这个任务）。

**状态判断逻辑**：
- `review`/`applied`/`closed` → `in_progress`（清理时间戳，记录 audit_chain）
- `open`/`in_progress` → 返回 `no need to reopen` 错误（任务仍在工作中）

**递归 reopen 祖父链**：reopen 当前任务后，自动检查祖父任务状态。若祖父也是
`applied`/`closed`，递归 reopen 为 `in_progress`，确保整条任务链回到工作状态。

**自动触发场景**（`task_create(parent_id=closed_task)`）：
- 向已 `closed`/`applied`/`review` 状态的父任务挂入新子任务时，**检查兄弟子任务状态**
  决定是否 reopen 父任务：
  - 所有兄弟子任务都是 `closed`（或无兄弟子任务）→ reopen 父任务为 `in_progress`
  - 有兄弟子任务非 `closed`（如 `open`/`in_progress`）→ 直接挂，**不 reopen** 父任务
  （因为父任务下还有工作在进行中，不需要重新激活）
- 父任务 `open`/`in_progress` 时直接挂，不改状态
- 父任务被 reopen 后，递归向上检查祖父任务时**不再检查兄弟**（祖先链已被触发，
  无条件 reopen），确保整条链回到工作状态

> **设计理由**：挂新子任务时需要同时考虑父任务状态和兄弟子任务状态。若父任务已
> `closed` 但还有 `open` 的兄弟子任务（数据不一致或误操作），直接挂新子任务即可，
> 不自动改父任务状态；若所有兄弟都已 `closed`，说明之前的工作完成，新子任务表示新
> 需求来了，应 reopen 父任务。

**手动触发场景**（`cw task reopen`）：
- code review agent 发现 `applied`/`closed` 任务有回归问题
- 任务被误 close，需要重新打开
- 已 `review` 的任务发现问题需要退回修复
- 手动 reopen 时**不检查兄弟子任务状态**（用户明确要 reopen，直接 reopen 整条链）

**i18n key**：`task_reopen_failed`/`task_reopen_success`/`task_reopen_no_need`/
`task_reopened_at`/`task_reopen_reason_label`。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--reviewer <REVIEWER>` | 否 | 审核人标识 |
| `--reason <REASON>` | 否 | 重新打开原因（可选） |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |
| `--lease-token <TOKEN>` | 否 | Lease raw token (P4 protected mutation) |
| `--fencing-counter <N>` | 否 | Current fencing counter (P4 protected mutation) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task reopen T-123 --reason "回归失败" --reviewer review-agent
```


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

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--status <STATUS>` | 否 | 状态过滤（open/resolved/wontfix/all） |
| `--severity <SEVERITY>` | 否 | 严重度过滤（info/warn/error/block） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task findings T-123 --status open --severity error
```


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

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<finding_id>` | 是 | 发现 ID |
| `--resolution <RESOLUTION>` | 否 | 解决方式（fixed/wontfix/false_positive） |
| `--by <BY>` | 否 | 解决者（agent/human/system） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task resolve-finding F-1 --resolution fixed --by agent
```


### `task completion-review`：任务完成质量审查

```bash
# 任务级审查（不含 step）
cw task completion-review <task_id>

# 步骤级审查（指定 step_id）
cw task completion-review <task_id> --step-id <step_id>
```

运行任务完成质量审查，聚合 `run_check_gate` + 5 个扩展检查器
（scope violation / symbol attribution / file health / i18n 硬编码 /
signature mismatch），根据所有 open finding 的严重度给出决策：

- `pass`：无 finding，允许 step 进入 done
- `warn`：仅有 info/warn 级别 finding，记录但允许完成
- `block`：存在 error/block 级别 finding，step 阻塞，自动插入
  `fix_quality_gate_failure` 修复步骤

**输出字段**：`decision` / `summary` / `counts`（info/warn/error/block 计数）/
`findings`（详细发现列表）/ `check_gate_result`（底层 check_gate 原始结果）。

**i18n key**：`task_completion_review_unavailable`/`task_completion_review_failed`/
`task_completion_review_result`/`task_completion_review_task`/
`task_completion_review_step`/`task_completion_review_summary`/
`task_completion_review_counts`/`task_completion_review_findings_title`/
`task_completion_review_finding_item`。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--step-id <STEP_ID>` | 否 | 步骤 ID（可选，留空则做任务级审查） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task completion-review T-123 --step-id S-2
```


### `task split`：从 Markdown 计划拆分父子任务树

```bash
cw task split <task_id> --plan <plan.md>
```

读取 Markdown 计划文件，解析出子任务定义，调用 `db.task_split` 创建
父子任务树。适用于任务过大需要拆分为可管理的子任务时使用。

**Markdown 计划格式**：
- `## 子任务标题` = 子任务
- 标题下的普通文本 = 子任务描述
- `- / * / +` 开头的列表项 = 步骤（格式：`action @ target_file` 或
  `action: target_file`）
- 代码块（``` 围栏）内的内容不解析
- 一级标题（`#`）和三级及以上标题（`###`+）被跳过

**示例计划**：
```markdown
# 根任务

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--plan <PLAN>` | 是 | Markdown 计划文件路径 |
| `--identity-policy <IDENTITY_POLICY>` | 否 | Subtask identity policy (default legacy_identity_v1) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task split T-123 --plan docs/plan.md
```


## 子任务1
实现登录功能
- edit @ src/auth.py
- test @ tests/test_auth.py

## 子任务2
实现注册功能
- edit @ src/register.py
```

**输出**：每个新建子任务的 ID 和标题。

**i18n key**：`task_split_plan_not_found`/`task_split_no_subtasks`/
`task_split_success`/`task_split_subtask_item`。

### `task status-tree`：以树形显示任务状态

```bash
cw task status-tree <task_id>
```

以树形模式显示任务详情，递归展示所有子任务。等价于 `cw task show <task_id>`
（不带 `--flat`）。是 `task show` 的树形别名，方便用户记忆。

**输出格式**：与 `task show` 一致，按 `depth` × 4 空格缩进展示子任务链。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task status-tree T-123
```


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

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--blocked` | 否 | 仅显示有阻塞发现的任务 |
| `--limit <LIMIT>` | 否 | 最多列出的任务数量（默认 200） |
| `--status <STATUS>` | 否 | 状态过滤（open/in_progress/review/applied/closed/reverted） |
| `--flat` | 否 | 扁平展示（不按树形缩进） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task list --status in_progress --limit 20
```


### `task show`：查看任务详情（含子任务树）

```bash
# 默认树形展示（递归显示所有子任务 + 进度）
cw task show <task_id>

# 切换到扁平模式（仅显示主任务，不递归子任务）
cw task show <task_id> --flat
```

默认调用 `db.task_status_tree()`，递归展示：
- 任务详情：ID / 标题 / 状态 / 描述 / 创建者 / 创建时间
- 进度：`done/total (pct%)`，`pct` 固定显示两位小数；接口中的 `progress` 是 0..1
  ratio，`percent` 才是 0..100 的百分比
- 状态：同时显示 raw `status`/`lifecycle_status` 与 daemon 派生的 `workflow_status`；
  缺少历史治理事实时显示 `governance_blocked` 和阻断原因
- 自身步骤列表（仅根任务显示步骤明细）
- 子任务树（按 depth 缩进，带 `↳` 前缀）

`--flat` 模式调用 daemon `task.status`，显示主任务详情、自身步骤和统一治理状态；
daemon 不可用时按兼容模式规则 fail-closed，不用本地 raw status 伪造治理结论。

`task create` 的默认三角色合同同时声明 `identity_policy=legacy_identity_v1`，
保证新建任务的 Contract revision 可被后续 Executor claim；历史缺少该字段的任务
仍需由受权治理流程追加新 revision，不能由客户端直接补写。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--flat` | 否 | 扁平模式（不递归展示子任务） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task show T-123 --flat
```


### `task capture-diff`：捕获外部 Agent 文件改动到审计闭环

```bash
# Dry-run：只返回计划，不写库
cw task capture-diff <task_id> --dry-run

# 实际写入（默认）：落 change_audit / task_symbol_changes / audit_chain，并触发质量检查
cw task capture-diff <task_id>

# 指定关联 step 与 base commit
cw task capture-diff <task_id> --step-id S-1783... --base HEAD~1

# --auto 模式：自动检测 in_progress 任务，HEAD~1 作为 base，自动 apply（fail-soft）
# 常用于 post-commit hook 自动调用，task_id 可省略
cw task capture-diff --auto
```

**用途**：当外部 Agent（如 Claude Code、Codex 等）在 Call Warden 之外
直接修改了文件后，调用此命令把这些"真实改动"捕获回 Call Warden 的
任务/变更/符号/审计闭环，使图谱与磁盘保持一致，并自动生成质量发现。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 否 | 关联的任务 ID（`--auto` 模式下可省略） |
| `--step-id <STEP_ID>` | 否 | 关联的 step ID（默认空） |
| `--base <BASE>` | 否 | 基准 commit（空串 = 最近一次 `workspace_scan_runs` 的 git_head） |
| `--dry-run` | 否 | 只返回计划，不写数据库（默认执行 apply） |
| `--auto` | 否 | 自动模式（fail-soft，不阻断 git commit）：检测 `in_progress` 任务、取 `HEAD~1` 为 base、自动 apply、自动填 `source_commit_hash`；双层 fail-soft（DB 层 + CLI 层） |
| `--skip-quality-review` | 否 | 跳过 `run_task_completion_review`（Semgrep + 5 extension checkers）；`--auto` 模式总是跳过，手动模式默认 False |
| `--source-commit-hash <SOURCE_COMMIT_HASH>` | 否 | 引入此次变更的 git commit hash（schema v35+），写入 `task_symbol_changes.source_commit_hash`，供 `get_task_commits` / `get_commit_tasks` 三角关联查询 |

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

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task capture-diff T-123 --step-id S-1 --base HEAD~1
```


### `task assignment-status`：读取 daemon 派工队列投影（只读）

```bash
cw task assignment-status <task_id>
cw task assignment-status <task_id> --step-id S-1783... --role executor
cw task assignment-status <task_id> --json
```

读取 daemon durable 工作队列投影（`task.assignment.status`）；CLI 不在本地
推导或回收队列状态，local 模式无此投影（fail-closed）。

- `--step-id` / `--role`：可选过滤（step ID / 治理角色）
- `--json`：输出 daemon 原始 JSON
- 默认展示 `current_assignment`（assignment_id / step_id / role / status /
  holder_session_id / last_heartbeat_at）与队列历史条数 `history_count`

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | Task ID |
| `--step-id <STEP_ID>` | 否 | Optional step ID |
| `--role <ROLE>` | 否 | Optional governance role |
| `--json` | 否 | Output raw daemon JSON |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task assignment-status T-123 --role executor --json
```


### `task assignment-heartbeat`：daemon 派工心跳

```bash
cw task assignment-heartbeat <task_id> <assignment_id> \
  --request-id req-abc123 --fencing-counter 3
```

向 daemon 上报 durable assignment 心跳（`task.assignment.heartbeat`），证明
executor 仍持有该派工；仅转发 daemon，客户端不能直接修改队列。

- `<task_id>` / `<assignment_id>`：任务 ID 与 durable assignment ID（必填）
- `--request-id`：**必填**，稳定幂等请求 ID（重放安全）
- `--fencing-counter`：当前 fencing 计数（int，默认 `-1`，`>= 0` 时才随请求发送）
- `--agent-session-id`：Agent 会话 ID（缺省从结构化身份 session 解析）
- 身份参数：`--agent-id` / `--session-id` / `--model-id` / `--role` /
  `--agent-instance-id`（提供则须完整，否则 fail-closed）；`--json` 输出原始 JSON

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | Task ID |
| `<assignment_id>` | 是 | Durable assignment ID |
| `--request-id <REQUEST_ID>` | 是 | Stable replay-safe request ID |
| `--fencing-counter <FENCING_COUNTER>` | 否 | Current fencing counter |
| `--agent-session-id <AGENT_SESSION_ID>` | 否 | Agent session ID |
| `--agent-id <ID>` | 否 | Agent ID |
| `--session-id <ID>` | 否 | Identity session ID |
| `--model-id <ID>` | 否 | Model ID |
| `--role <ROLE>` | 否 | Governance role |
| `--agent-instance-id <ID>` | 否 | Agent instance ID |
| `--json` | 否 | Output raw daemon JSON |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task assignment-heartbeat T-123 A-abc --request-id hb-1 --fencing-counter 1 --agent-id executor --session-id s-exec --model-id m1 --role executor --agent-instance-id inst-exec
```


### `task handoff`：结构化角色交接（task.handoff ledger 事件）

```bash
cw task handoff <task_id> --from-role executor --outcome blocked \
  --next-role reviewer --next-action review --reason "自查发现风险" \
  --independence-requirement "different-session" --request-id req-001 \
  --step-id S-1783... --report-request-id req-000 \
  --evidence-path evidence/manifest.json --evidence-hash <sha256>
```

向 append-only ledger 追加一条结构化 Executor/Reviewer/Adjudicator 交接事件
（`task.handoff`）。仅 daemon 权威写入（禁止 local SQLite fallback），必须携带
完整结构化身份（否则 `E_IDENTITY_REQUIRED`）。以下 11 个 flag **全部必填**：
`--from-role` / `--outcome` / `--next-role` / `--next-action` / `--reason` /
`--independence-requirement` / `--request-id` / `--step-id` /
`--report-request-id` / `--evidence-path` / `--evidence-hash`。

- `--step-id` 传字符串 `null` 时透传为 JSON null（task-level 语义）
- 身份参数与 lease 凭证（`--lease-token` / `--fencing-counter`）可选，
  提供则须完整配对（fail-closed）

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | Task ID |
| `--from-role <FROM_ROLE>` | 是 | Source governance role |
| `--outcome <OUTCOME>` | 是 | Structured handoff outcome |
| `--next-role <NEXT_ROLE>` | 是 | Next governance role |
| `--next-action <NEXT_ACTION>` | 是 | Next role action |
| `--reason <REASON>` | 是 | Handoff reason |
| `--independence-requirement <INDEPENDENCE_REQUIREMENT>` | 是 | Independence requirement |
| `--request-id <REQUEST_ID>` | 是 | Stable request id |
| `--step-id <STEP_ID>` | 是 | Completed step id |
| `--report-request-id <REPORT_REQUEST_ID>` | 是 | Task report request id |
| `--evidence-path <EVIDENCE_PATH>` | 是 | Evidence path |
| `--evidence-hash <EVIDENCE_HASH>` | 是 | Evidence hash |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |
| `--lease-token <TOKEN>` | 否 | Lease raw token (P4 protected mutation) |
| `--fencing-counter <N>` | 否 | Current fencing counter (P4 protected mutation) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task handoff T-123 --from-role reviewer --outcome reviewer_pass --next-role adjudicator --next-action apply_and_close --reason "复审通过" --independence-requirement required --request-id ho-1 --step-id S-3 --report-request-id req-1 --evidence-path .trae-cn/evidence/pass.json --evidence-hash <SHA256>
```


### `task step-resolve`：失败步骤 remediation 后回审

```bash
cw task step-resolve <task_id> <failed_step_id> <remediation_step_id> <request_id> \
  --evidence-path evidence/fix.json --evidence-hash <sha256>
```

daemon-only 命令（`task.step.resolve`）：失败步骤经 remediation 步骤修复后合法
回审。resolution 以 append-only 方式写入 `task_events` ledger（原始失败行不可变），
校验（remediation 来源 / lease / request_id 重放）全部在 daemon authority 内完成。

- 位置参数：`task_id` / `failed_step_id`（原失败步骤）/ `remediation_step_id`
  （已完成的 fix_defect 步骤）/ `request_id`（幂等请求 ID）
- `--evidence-path` / `--evidence-hash`：**必填**，resolution 证据 manifest
  路径与 SHA-256
- 必须携带完整结构化身份；支持 `--lease-token` / `--fencing-counter` 与 `--json`

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | Task ID |
| `<failed_step_id>` | 是 | Original failed step id |
| `<remediation_step_id>` | 是 | done fix_defect step id |
| `<request_id>` | 是 | Stable request id (replay-safe) |
| `--evidence-path <EVIDENCE_PATH>` | 是 | Resolution evidence path (mandatory) |
| `--evidence-hash <EVIDENCE_HASH>` | 是 | SHA-256 of resolution evidence (mandatory) |
| `--json` | 否 | Output raw daemon JSON (machine-readable) |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |
| `--lease-token <TOKEN>` | 否 | Lease raw token (P4 protected mutation) |
| `--fencing-counter <N>` | 否 | Current fencing counter (P4 protected mutation) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task step-resolve T-123 S-failed S-fix req-1 --evidence-path .trae-cn/evidence/fix.json --evidence-hash <SHA256>
```


### `task cascade-close`：聚合节点级联收尾

```bash
cw task cascade-close <task_id>
```

聚合节点级联收尾：子树全部 closed 后树干自动 close（递归向上）。系统级操作，
经 daemon 权威 `task.cascade_close`，无 local fallback（daemon 不可用 fail-closed）。

- 目标卡已 closed 时幂等跳过（无新增收尾）
- 子树存在未 closed 子卡或叶子步骤未完成时拒绝收尾，退出码 1（GOV-FIX-06）
- 支持结构化身份参数（`--agent-id` / `--session-id` / `--model-id` / `--role` /
  `--agent-instance-id`）

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task cascade-close T-parent
```


### `task claim-recover`：释放失联 executor claim

```bash
cw task claim-recover <task_id> --reason "executor 会话已失联" \
  --agent-id A-1 --session-id S-2 --model-id M-3 --role adjudicator \
  --lease-token <token> --fencing-counter 5
```

释放已确认失联的旧 Executor claim（daemon-only，`task.claim.recover`）：只追加
`claim_released` 审计事件，不改变任务状态/步骤/历史。释放后新 Executor 需显式
调用 `cw task next` 重新领取。

- `--reason`：**必填**，判定 claim 失联的原因
- 仅限 **adjudicator** 身份（`--role adjudicator`），且必须持有目标任务**独立
  Reviewer** 的 `--lease-token` / `--fencing-counter`（缺失即 fail-closed）
- `--request-id`：可选，幂等请求 ID（缺省自动生成 `claim-recover-<hex>`）

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | 任务 ID |
| `--reason <REASON>` | 是 | Why the old claim is considered orphaned/stale (mandatory) |
| `--request-id <REQUEST_ID>` | 否 | Stable request id for replay-safe dedup (auto- generated if empty) |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |
| `--lease-token <TOKEN>` | 否 | Lease raw token (P4 protected mutation) |
| `--fencing-counter <N>` | 否 | Current fencing counter (P4 protected mutation) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task claim-recover T-123 --reason "executor 失联超过 30 分钟" --agent-id admin --session-id s-admin --model-id m1 --role planner
```


### `task supersede`：声明任务替代关系

```bash
cw task supersede <old_task_id> <new_task_id> --reason "范围扩大，改由新任务承接" \
  --evidence-path evidence/supersede.json --evidence-hash <sha256> \
  --lease-token <token> --fencing-counter 7 \
  --agent-id A-1 --session-id S-2 --model-id M-3 --role adjudicator
```

声明旧任务被新任务替代（`task.supersede`，GOVERNANCE_WRITE 治理 mutation，
append-only 事件 + 独立关系表）。仅 daemon 权威写点，local 模式 fail-closed。

- 位置参数：`<old>`（被替代任务）/ `<new>`（替代任务）
- `--evidence-path` / `--evidence-hash`：**必填**，治理依据 evidence manifest
  （CLI 前置 fail-fast，daemon 侧仍 fail-closed）
- `--request-id`：幂等 key 成员（缺省自动生成 `req-<uuid>`）
- `--lease-token` / `--fencing-counter`：source（被替代）任务的 reviewer lease
- `--role` 严格限定 `adjudicator`；身份参数与其他治理命令一致

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<old>` | 是 | Task ID being superseded (old) |
| `<new>` | 是 | Task ID superseding (new) |
| `--reason <REASON>` | 否 | Reason for superseding (recorded append-only) |
| `--request-id <ID>` | 否 | Stable request id for idempotent replay (auto- generated if empty) |
| `--evidence-path <PATH>` | 否 | Evidence manifest path (required for governance supersede) |
| `--evidence-hash <HASH>` | 否 | Evidence manifest sha256 (required for governance supersede) |
| `--lease-token <TOKEN>` | 否 | Reviewer lease token for the source (superseded) task |
| `--fencing-counter <N>` | 否 | Reviewer lease fencing counter (must equal current value) |
| `--agent-id <ID>` | 否 | Agent ID（P3 结构化身份，Req 10.1） |
| `--session-id <ID>` | 否 | Session ID（P3 结构化身份，Req 10.1） |
| `--model-id <ID>` | 否 | Model ID（P3 结构化身份，Req 10.1） |
| `--role <ROLE>` | 否 | 角色（planner/implementer/reviewer/tester） |
| `--agent-instance-id <ID>` | 否 | Agent instance ID (P3 Identity, optional; must match registration if non-empty) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task supersede T-old T-new --reason "拆分到新卡" --request-id sup-1
```


### `task governance-projection`：查看任务治理投影（只读）

```bash
cw task governance-projection <task_id>
cw task governance-projection <task_id> --json
```

只读查询任务治理投影（`task.governance_projection.get`，daemon 权威，无 local
fallback）。展示 lifecycle/workflow 状态、当前与下一角色、review 状态与 verdict、
Task Contract、当前步骤、Reviewer Role Contract、normalization 规则与阻断原因；
投影绝不返回 lease token（`lease_raw_token_omitted`）。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<task_id>` | 是 | Task ID |
| `--json` | 否 | Output raw daemon JSON (machine-readable) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task governance-projection T-123 --json
```


### `task superseded`：查询任务被替代状态（只读）

```bash
cw task superseded <task_id>
```

只读查询某任务是否已被其他任务替代（`task.superseded_by`，daemon 权威，无 local
fallback）。已替代时显示替代者 task_id、reason、actor、supersedence_id、
reason_code 与 evidence_hash；未被替代时明确提示。

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<id>` | 是 | 任务 ID |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task superseded T-old
```


### `audit verify`：验证审计链完整性

```bash
cw audit verify [--table <TABLE>] [--limit <N>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--table <TABLE>` | 否 | 按表名过滤（空串 = 全部表） |
| `--limit <N>` | 否 | 最大验证记录数（默认 1000） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

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

**示例**

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

### `audit rotate-key`：轮换审计签名密钥（C7）

```bash
cw audit rotate-key --key-id <KEY_ID> [--secret <SECRET>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--key-id <KEY_ID>` | 是 | 新密钥标识（唯一，如 `key-2026-07`） |
| `--secret <SECRET>` | 否 | 新密钥内容；省略时自动生成 32 字节随机密钥（hex 编码，64 字符） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> **PROTECTED_MUTATION 写操作**：此命令路由为 `admin.audit_rotate_key`（PROTECTED_MUTATION 保护，无本地 SQLite fallback），会 INSERT/UPDATE `audit_key_rotations` 表，需激活 workspace。

轮换审计签名密钥。轮换后：
- **新记录**用新密钥签名（`signing_key_id = <new_key_id>`）
- **旧记录保持原签名不变**（`signing_key_id` 不变）
- `verify_audit_chain` 按 `signing_key_id` 从 `audit_key_rotations` 表查找对应密钥验证

**输出**：
- 新密钥 ID
- 轮换时间戳
- 前一个密钥 ID（首次轮换为空）
- 提示：旧记录保持原签名，验证时按 `signing_key_id` 查找对应密钥

**幂等性**：相同 `key_id` 再次轮换会更新 `key_secret` 并保持 `is_active=1`。

**密钥轮换存储机制（Schema v29）**：`audit_key_rotations` 表记录每次密钥轮换：

| 字段         | 类型        | 说明                         |
| ------------ | ----------- | ---------------------------- |
| `id`         | INTEGER PK  | 自增主键                     |
| `key_id`     | TEXT UNIQUE | 密钥标识（如 `key-2026-07`） |
| `key_secret` | TEXT        | 密钥内容（用于 HMAC 计算）   |
| `rotated_at` | REAL        | 轮换时间戳                   |
| `is_active`  | INTEGER     | 1=当前活跃，0=已停用         |

**密钥查找优先级**（签名时 `_get_active_signing_key`）：
1. `audit_key_rotations` 表中 `is_active=1` 的记录
2. 环境变量 `CALLWARDEN_AUDIT_HMAC_KEY` / 文件 `~/.callwarden/audit.key`
3. 回落到 SHA-256 链（`signing_key_id='local'`）

**验证时密钥查找**（`_lookup_signing_key`）：
1. `audit_key_rotations` 表中 `key_id` 对应的 `key_secret`
2. `key_id == "hmac"`：回落到当前环境变量/文件密钥（向后兼容）
3. `key_id == "local"`：返回 `None`（SHA-256 链）
4. 未知 `key_id`：返回 `None`（无法验证，标记为 `signature_mismatch`）

**向后兼容**：
- legacy `signing_key_id="hmac"` 记录（无轮换表时签发）仍能用当前环境变量/文件密钥验证
- legacy `signing_key_id="local"` 记录（SHA-256 链）无需密钥即可验证

**示例**

```bash
# 轮换到新密钥（自动生成 32 字节随机密钥）
cw audit rotate-key --key-id key-2026-07

# 指定密钥内容
cw audit rotate-key --key-id key-2026-07 --secret "my-secret-string"
```

### `audit keys`：列出签名密钥轮换记录

```bash
cw audit keys
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> **只读**：此命令仅查询 `audit_key_rotations` 表，不修改数据库。

列出所有签名密钥轮换记录，按 `rotated_at` 倒序。每项含 `key_id` / `rotated_at` / `is_active`，
**不返回 `key_secret`** 以避免泄露密钥内容。

**示例**

```bash
cw audit keys
```

### `check-gate`：检查门禁

```bash
cw check-gate <TASK_ID> [--resolve] [--step-id <STEP_ID>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<TASK_ID>` | 是 | 任务 ID |
| `--resolve` | 否 | 标记该任务的门禁发现为已解决（Agent 修复后调用） |
| `--step-id <STEP_ID>` | 否 | 关联步骤 ID |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

对变更文件运行语法检查 + Semgrep 扫描。失败会自动插入 `fix_gate_failure` 步骤。

> **与 `task findings` 的关系**：`check-gate` 是手动触发一次检查并写入
> `task_quality_findings`；`task findings` 是查询已写入的发现。
> `task_completion_review`（MCP 工具）会自动调用 `check-gate` 并叠加
> 5 个扩展检查器（scope / symbol_attribution / file_health / i18n / signature）。

**示例**

```bash
# 检查
cw check-gate T-1783350489327

# 标记门禁发现已解决
cw check-gate T-1783350489327 --resolve
```

### `task list` / `task show`：任务列表与详情

```bash
cw task list                  # 任务列表
cw task show <task_id>        # 任务详情（树形模式）
```

---

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--blocked` | 否 | 仅显示有阻塞发现的任务 |
| `--limit <LIMIT>` | 否 | 最多列出的任务数量（默认 200） |
| `--status <STATUS>` | 否 | 状态过滤（open/in_progress/review/applied/closed/reverted） |
| `--flat` | 否 | 扁平展示（不按树形缩进） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw task list --status in_progress --limit 20
```


## 度量命令

### `metrics`：度量汇总

```bash
cw metrics
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw metrics
```

### `complexity [N]`：圈复杂度热点

```bash
cw complexity
cw complexity 50
cw complexity --module "src/payment"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[limit]` | 否 | 最大结果数（默认 20） |
| `--module <MODULE>` | 否 | 按模块过滤 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

复杂度 >10 的函数建议重构（标记 `!`）。

**示例**

```bash
cw complexity 50 --module "src/payment"
```

### `coupling`：模块耦合度

```bash
cw coupling
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

计算每个模块的传入/传出耦合度和不稳定性（instability）。

**示例**

```bash
cw coupling
```

### `largest-fns [N]`：最大函数排行

```bash
cw largest-fns
cw largest-fns 50
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[limit]` | 否 | 最大结果数（默认 20） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

按代码行数排序，列出最大的函数。

**示例**

```bash
cw largest-fns 50
```

### `coupled-fns [N]`：耦合度最高函数排行

```bash
cw coupled-fns
cw coupled-fns 50
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[limit]` | 否 | 最大结果数（默认 20） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

按耦合度（扇入+扇出）排序。

**示例**

```bash
cw coupled-fns 50
```

### `fn-metrics <NAME>`：单函数度量

```bash
cw fn-metrics "my_project::payment::process_payment"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<NAME>` | 是 | 函数名 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw fn-metrics "my_project::payment::process_payment"
```

### `comment-coverage`：注释覆盖率

```bash
cw comment-coverage
cw comment-coverage --by module   # 按 module/file/kind
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--by <GROUP_BY>` | 否 | 分组方式（默认 module） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw comment-coverage --by file
```

### `health-report`：项目整体健康报告

```bash
cw health-report          # 文本报告
cw health-report --json   # JSON 输出
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--json` | 否 | 以 JSON 输出（默认文本报告） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

聚合基础统计 + 演化热点（Top 5）+ 问题统计（Semgrep findings 按 severity）+ Token 节省，一眼看清项目健康状态。只读。

**示例**

```bash
cw health-report --json
```

### `dashboard`：项目综合状态驾驶舱

```bash
cw dashboard                               # quick 模式（默认）
cw dashboard --full                        # 完整模式（含圈复杂度计算，大仓库较慢）
cw dashboard --with-cycles                 # 启用调用循环检测
cw dashboard --with-evolution              # 演化趋势（需先导入 git history）
cw dashboard --risks --top 10              # 风险预警 Top 10（默认 5）
cw dashboard --json                        # JSON 输出
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--full` | 否 | 完整模式（计算圈复杂度，大仓库较慢） |
| `--with-cycles` | 否 | 启用调用循环检测（无 Rust GraphStore 时可能较慢） |
| `--with-evolution` | 否 | 演化趋势（需先导入 git history） |
| `--risks` | 否 | 显示项目风险预警（高复杂度 / 超大文件 / 阻塞 findings） |
| `--top <N>` | 否 | 风险列表 Top N（默认 5） |
| `--json` | 否 | JSON 输出 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

聚合 7 个 section（overview / code_scale / code_quality / call_graph / task_risk / audit / evolution）+ 风险预警列表。默认 quick 模式（100K 符号约 280ms）。只读。

**示例**

```bash
cw dashboard --full --with-cycles --json
```

---

## 演化智能命令

### `evolution <QN>`：函数变更频率

```bash
cw evolution "my_project::payment::process_payment"
cw evolution "my_project::payment::process_payment" --window 30d
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<QN>` | 是 | 函数限定名 |
| `--window <WINDOW>` | 否 | 时间窗口（如 30d/90d/1y；不传 = 全部历史） |
| `--defects` | 否 | 启用变更-缺陷关联模式（不加则只返回变更频率） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

显示变更次数、变更者、变更时间线、变更分布。

`--defects` 启用变更-缺陷关联模式，分析符号的变更频率与缺陷（Semgrep findings）的时间关联性，回答"这个函数改得多不多？改完之后容易引入缺陷吗？"：返回 `change_count`（变更次数）/ `defect_count`（关联缺陷数）/ `defect_rate`（defect_count / change_count）/ `defect_types`（缺陷类型）/ `recent_defects`（最近的关联缺陷列表）。变更-缺陷关联窗口固定为默认 5 次提交内（CLI 无窗口覆盖入口）；`--window` 仅作用于非 `--defects` 的变更频率模式（如 `30d/90d/1y`）。对应 MCP 工具：`get_defect_correlation`。

**示例**

```bash
cw evolution "my_project::payment::process_payment" --window 90d
cw evolution "module::fn" --defects
```

### `hotspot`：热点函数排名

```bash
cw hotspot
cw hotspot --module src/payment --limit 50
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--module <MODULE>` | 否 | 模块路径前缀过滤 |
| `--limit <N>` | 否 | 显示数量（默认 20） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

按热点分（变更次数 + 缺陷数 + 复杂度）排序。

**示例**

```bash
cw hotspot --module src/payment --limit 50
```

### `churn`：代码流失分析

```bash
cw churn
cw churn --window 90d
cw churn --module src/api
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--module <MODULE>` | 否 | 模块路径前缀过滤 |
| `--window <WINDOW>` | 否 | 时间窗口（默认 90d） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw churn --window 90d --module src/api
```

### `test-impact <QN>`：测试影响选择

```bash
cw test-impact "my_project::payment::process_payment"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<QN>` | 是 | 被修改函数的限定名 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

返回改了该函数后需要运行的测试列表（通过反向调用链 BFS）。

**示例**

```bash
cw test-impact "my_project::payment::process_payment"
```

---

## 静态检查命令

Call Warden 静态检查整合了 4 类能力：符号静态检查（issues）/ 测试 case 关联（tests）/ 代码重复检测（clone）/ 变更-缺陷关联（evolution --defects）。

> **背景**：这 4 类能力是 cw 独有的静态分析能力，Grep 做不到或做不好。详见 [TOOLS.md 场景映射 §6 静态检查](../TOOLS.md)。

### `issues <QN>`：符号静态检查

```bash
cw issues <QN> [--include-info]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<QN>` | 是 | 符号限定名 |
| `--include-info` | 否 | 包含 INFO 级别（默认只 WARNING+，避免噪音） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

整合 Semgrep + Guardrail findings，按符号聚合。返回符号相关的所有静态检查问题。

查询路径：
1. **Semgrep findings**：按 `symbol_qualified` 精确匹配（首选）；无精确匹配时按 `file_instance_id + line 范围交集` 兜底
2. **Guardrail findings**：按 `file_path + symbol_hash` 匹配

返回：issues 列表，按 severity 降序（ERROR > WARNING > INFO），每条含 source/rule_id/severity/message/start_line/end_line/snippet/fix。

对应 MCP 工具：`get_symbol_issues`

**示例**

```bash
cw issues "module::fn"
cw issues "module::fn" --include-info
```

### `tests <QN>`：符号测试 case 查询

```bash
cw tests <QN> [--reverse] [--history] [--limit <N>]
cw tests --build [--force]
cw tests --import <FILE> [--ci-run-id <ID>] [--ci-url <URL>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<QN>` | 否 | 被测函数限定名（`--reverse` 时为 test_fn 限定名）；`--build` / `--import` 模式下可省略 |
| `--reverse` | 否 | 反向查询：test_fn 测了哪些函数 |
| `--history` | 否 | 基于 `test_runs` 表的运行历史，返回 `pass_rate` / `recent_failures` / `by_test` |
| `--limit <N>` | 否 | `--history` 模式最多显示的运行记录数（默认 50） |
| `--build` | 否 | 重建/重算 test_case_relations（写操作，refresh 测试文件后调用） |
| `--force` | 否 | 与 `--build` 配合，强制清空已有关联后重建 |
| `--import <FILE>` | 否 | 导入 JUnit XML 测试运行结果（写操作；文件路径或 XML 内容） |
| `--ci-run-id <ID>` | 否 | 与 `--import` 配合，关联 CI 运行 ID |
| `--ci-url <URL>` | 否 | 与 `--import` 配合，关联 CI 运行 URL |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

回答 agent 高频问题："foo() 有哪些 test 在测它？"

#### 三阶推断算法

test_fn ↔ tested_fn 的关联分 3 个置信度等级：

1. **direct_call（high）**：test_fn 直接调用了 tested_fn（基于调用图）
2. **name_convention（mid）**：test_fn 名字能推断出 tested_fn（`testFoo` → `foo`，`foo_test` → `foo`）
3. **indirect（low）**：test_fn 调用了 tested_fn 的调用方（间接测试）

> 测试覆盖摘要（`has_tests` / `test_count` / `high_confidence_count` / `tests`）为 MCP 专属 `get_test_coverage_summary`，CLI 无对应 flag。

对应 MCP 工具（只读）：`get_test_cases` / `get_tested_functions` / `get_test_coverage_summary` / `get_test_stability`

> **注**：写操作（`--build` / `--import`）不暴露 MCP，遵循 AGENTS.md 规则 2（写操作走 CLI）。

**示例**

```bash
cw tests "module::fn"                      # 查测试 case 列表（按 confidence 降序）
cw tests "module::fn" --reverse            # 反向：test_fn 测了哪些函数
cw tests "module::fn" --history            # 测试稳定性（pass_rate / failures / by_test）
cw tests --build                           # 全量重建 test_case_relations（refresh 后调用）
cw tests --build --force                   # 强制全量重建（清空已有关联）
cw tests --import <junit.xml>              # 导入 JUnit XML 测试运行结果
cw tests --import <file> --ci-run-id ID --ci-url URL  # 关联 CI 运行信息
```

### `clone`：代码重复检测

```bash
cw clone detect [--file-filter <PREFIX>] [--min-lines <N>] [--similarity <F>]
cw clone list [--type <N>] [--min-similarity <F>] [--limit <N>] [--symbol <QN>]
cw clone stats
cw clone clear
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `detect --file-filter <PREFIX>` | 否 | 文件路径前缀过滤（如 `src/core/`），留空扫描全部 |
| `detect --min-lines <N>` | 否 | 最小符号行数（默认 5，跳过更短的符号） |
| `detect --similarity <F>` | 否 | Type-3 相似度阈值 [0,1]（默认 0.8） |
| `list --type <N>` | 否 | 克隆类型过滤（N=0 全部，1/2/3=Type-N） |
| `list --min-similarity <F>` | 否 | 最低相似度过滤（默认 0.0） |
| `list --limit <N>` | 否 | 返回上限（默认 100） |
| `list --symbol <QN>` | 否 | 只返回涉及此符号的克隆 |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

子命令组，检测和查询 Type-1/2/3 克隆。`detect` 将结果持久化到 `clone_pairs` 表，`clear` 清空当前工作区的所有克隆检测结果。

#### 克隆类型

| 类型   | 说明                           | 检测方法              |
| ------ | ------------------------------ | --------------------- |
| Type-1 | 完全相同（除空白/注释）        | content_hash 完全相同 |
| Type-2 | 重命名克隆（token 序列相同）   | token 归一化后相同    |
| Type-3 | 微调克隆（添加/删除/修改语句） | Jaccard 相似度 ≥ 阈值 |

返回字段：clone_type / similarity / token_hash / lines_a / lines_b / symbol_a_name / symbol_b_name / file_a / file_b / detected_at。

对应 MCP 工具：`detect_clones` / `list_clones`（含 `symbol_id` 参数）/ `get_clone_stats` / `clear_clones`

**示例**

```bash
cw clone detect                              # 检测克隆（默认 min_lines=5, similarity=0.8）
cw clone detect --min-lines 10 --similarity 0.8  # 自定义阈值
cw clone detect --file-filter src/core/      # 只扫描 src/core/ 前缀
cw clone list                               # 列出所有克隆对
cw clone list --type 1                      # 只列 Type-1（完全相同）
cw clone list --type 2 --limit 20           # Type-2 + 限制数量
cw clone list --symbol "module::fn"          # 按符号查重复代码
cw clone stats                              # 克隆统计
cw clone clear                              # 清空检测结果
```

---

## 影响分析命令

### `review <HASH>`：审查就绪报告

```bash
cw review <HASH>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<HASH>` | 是 | 源符号内容 hash |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

生成审查就绪报告：风险等级、影响范围、必测项、人工审查点、覆盖率。

**示例**

```bash
cw review abc123def456
```

---

## 缺陷知识库命令

### `defect search`：搜索缺陷模式

```bash
cw defect search
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--category <CAT>` | 否 | 按类别过滤 |
| `--severity <SEV>` | 否 | 按严重度过滤 |
| `--limit <N>` | 否 | 最大结果数（默认 20） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw defect search --category security
cw defect search --severity error --limit 30
```

### `defect suggest`：推荐修复方案

```bash
cw defect suggest <SYMBOL_HASH>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<SYMBOL_HASH>` | 是 | 缺陷符号哈希 |
| `--finding <N>` | 否 | 指定 Semgrep finding ID |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw defect suggest a1b2c3d4e5f6...
cw defect suggest a1b2c3d4e5f6... --finding 42
```

### `defect learn`：从修复 commit 学习

```bash
cw defect learn <COMMIT_HASH>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<COMMIT_HASH>` | 是 | 修复 commit 哈希 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw defect learn abc123def456
```

### `defect stats`：缺陷知识库统计

```bash
cw defect stats
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw defect stats
```

### `defect build`：构建缺陷知识库

```bash
cw defect build
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw defect build
```

### `function-issues [FN]`：函数缺陷检测

```bash
cw function-issues [FN] [--type <TYPE>] [--module <MODULE>] [--limit <N>] [--summary]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[FN]` | 否 | 函数限定名；省略为列表模式，传入为单函数详情模式 |
| `--type <TYPE>` | 否 | 按问题类型过滤 |
| `--module <MODULE>` | 否 | 按模块过滤（`--summary` 模式下同样生效） |
| `--limit <N>` | 否 | 最大结果数（默认 30） |
| `--summary` | 否 | 按严重程度汇总全项目问题 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
# 单函数
cw function-issues "my_project::payment::process_payment"

# 全部函数列表
cw function-issues
cw function-issues --type missing_comment
cw function-issues --module src/api

# 全项目按严重程度汇总
cw function-issues --summary
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

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--title <TITLE>` | 是 | 规则标题（简短描述） |
| `--text <TEXT>` | 是 | 规则正文（Agent 注入时会原文返回） |
| `--scope <JSON>` | 否 | 作用域 JSON，例如 `{"languages":["python"],"actions":["edit"]}` |
| `--severity <LEVEL>` | 否 | 严重级别：critical/error/warning/info（默认 `info`） |
| `--source <SOURCE>` | 否 | 来源：manual/auto_quality_findings/auto_semgrep/task_review/other（默认 `manual`） |
| `--evidence <JSON>` | 否 | 证据 JSON，例如 `{"task_id":"T-xxx","occurrences":3}` |
| `--confidence <0.0-1.0>` | 否 | 置信度 0.0-1.0 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：走 PROTECTED_MUTATION 保护；创建的候选默认 `pending`，accept 后才写入 `agent_rules` 生效。

必填：`--title` / `--text`。其余可选，`--severity` 默认 `info`，`--source` 默认 `manual`。

**示例**

```bash
cw rule candidate create --title "use i18n" --text "禁止硬编码字符串" --severity warning
cw rule candidate create --title "lock deps" --text "新增依赖必须锁定版本" \
    --source manual --confidence 0.8
```

### `rule candidate list`：列出候选规则

```bash
cw rule candidate list                       # 默认 pending
cw rule candidate list --status accepted    # 已接受
cw rule candidate list --status ""           # 所有状态
cw rule candidate list --limit 20
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--status <STATUS>` | 否 | 状态过滤：pending/accepted/rejected，空串=所有（默认 `pending`） |
| `--limit <N>` | 否 | 返回数量上限（默认 50） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw rule candidate list
cw rule candidate list --status accepted --limit 20
cw rule candidate list --status ""
```

### `rule candidate accept`：接受候选 -> active 规则

```bash
cw rule candidate accept ARC-1783253838000-a1b2
cw rule candidate accept ARC-1783253838000-a1b2 --reviewer human
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<CANDIDATE_ID>` | 是 | 候选规则 ID（ARC-xxx） |
| `--reviewer <REVIEWER>` | 否 | 审核人标识 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：走 PROTECTED_MUTATION 保护；accept 后规则写入 `agent_rules` 并参与上下文注入。

幂等：重复 accept 已 accepted 的 candidate 会返回原 `linked_rule_id`。

**示例**

```bash
cw rule candidate accept ARC-1783253838000-a1b2 --reviewer human
```

### `rule candidate reject`：拒绝候选规则

```bash
cw rule candidate reject ARC-1783253838000-a1b2
cw rule candidate reject ARC-1783253838000-a1b2 --reason "duplicate"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<CANDIDATE_ID>` | 是 | 候选规则 ID（ARC-xxx） |
| `--reviewer <REVIEWER>` | 否 | 审核人标识 |
| `--reason <REASON>` | 否 | 拒绝原因（可选） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：走 PROTECTED_MUTATION 保护；候选标记为 `rejected`，不会写入 `agent_rules`。

**示例**

```bash
cw rule candidate reject ARC-1783253838000-a1b2 --reviewer human --reason "duplicate"
```

### `rule list`：列出已生效规则

```bash
cw rule list                       # 默认 active
cw rule list --status deprecated   # 已弃用
cw rule list --status ""           # 所有状态
cw rule list --limit 50
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--status <STATUS>` | 否 | 状态：active/deprecated/removed，空串=所有（默认 `active`） |
| `--limit <N>` | 否 | 返回数量上限（默认 100） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw rule list
cw rule list --status deprecated --limit 50
```

输出含每条规则的 `id` / `title` / `severity` / `synced` 标记 / `scope`。

### `rule applicable`：按上下文查询匹配规则

```bash
cw rule applicable
cw rule applicable --context '{"languages":["python"],"actions":["edit"]}'
cw rule applicable --context '{"file_patterns":["src/api/**/*.py"]}' --limit 5
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--context <JSON>` | 否 | 上下文 JSON，例如 `{"languages":["python"],"actions":["edit"]}` |
| `--limit <N>` | 否 | 返回数量上限（默认 10） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw rule applicable --context '{"languages":["python"],"actions":["edit"]}' --limit 5
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

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--target <TARGET>` | 否 | 目标 AGENTS.md 路径（相对 workspace 或绝对） |
| `--apply` | 否 | 实际写入文件（默认 dry-run） |
| `--actor <ACTOR>` | 否 | 操作者标识 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：`--apply` 写文件操作走 PROTECTED_MUTATION 保护；默认 dry-run 只读预览，不写文件。

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

**示例**

```bash
cw rule sync                                          # dry-run 预览同步内容
cw rule sync --target path/to/AGENTS.md --apply --actor human
```

### `rule insert-block`：插入标记块

```bash
cw rule insert-block
cw rule insert-block --target path/to/AGENTS.md
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--target <TARGET>` | 否 | 目标 AGENTS.md 路径 |
| `--actor <ACTOR>` | 否 | 操作者标识 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：走 PROTECTED_MUTATION 保护。

当 AGENTS.md 还没有 Call Warden 标记区时调用此命令插入空标记块，
之后 `rule sync` 才能正常工作。重复插入会返回失败。

**示例**

```bash
cw rule insert-block --target path/to/AGENTS.md
```

### `rule extract`：从质量发现聚合候选规则

```bash
cw rule extract                                # 扫描全库
cw rule extract --task-id T-1783253838000-xxx  # 指定任务
cw rule extract --min-occurrences 3            # 提高阈值
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--task-id <TASK_ID>` | 否 | 任务 ID（空串=扫描所有任务） |
| `--min-occurrences <N>` | 否 | 最小出现次数阈值（默认 2） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：走 PROTECTED_MUTATION 保护；生成的候选默认 `pending`。

聚合维度：`(finding_type, severity, source)`。同一聚合键出现次数 ≥
`min_occurrences`（默认 2）时生成 1 个 pending 候选规则，自动去重。

**示例**

```bash
cw rule extract --min-occurrences 3
cw rule extract --task-id T-1783253838000-xxx
```

### `rule seed-bootstrap`：种子化内置自举 active rules

```bash
# Dry-run（默认）：只返回计划，不写库
cw rule seed-bootstrap

# 实际写入 agent_rules 表
cw rule seed-bootstrap --apply
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--apply` | 否 | 实际写入 agent_rules 表（默认 dry-run） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：`--apply` 写库操作走 PROTECTED_MUTATION 保护；默认 dry-run 只返回计划，不写库。

**用途**：把 Call Warden 自身的 5 条核心规约（i18n 强制、提交前刷新、
任务拆分、completion review、capture-diff）以**固定 ID** `AR-bootstrap-*`
写入 `agent_rules` 表，让 `task_next_step` / `work_next_job` /
`file_symbol_content` / `get_symbol` 等注入点能向 Agent 提供稳定的行为约束。

**幂等性**：通过固定 ID 实现：
- 不存在 → `create`
- 存在但 `rule_text` 变化 → `update`
- 存在且无变化 → `skip`

**内置规则清单**：

| ID                                   | severity | scope                     | 说明                                          |
| ------------------------------------ | -------- | ------------------------- | --------------------------------------------- |
| `AR-bootstrap-i18n`                  | warning  | `{}` (global)             | 用户可见输出必须通过 i18n.t()                 |
| `AR-bootstrap-refresh-before-commit` | warning  | `{actions:[commit]}`      | git commit 前必须 `cw refresh --all`          |
| `AR-bootstrap-task-split`            | info     | `{actions:[task_create]}` | 3+ 文件或 5+ 步骤必须 task_split              |
| `AR-bootstrap-completion-review`     | warning  | `{actions:[task_report]}` | task_report 前必须 run_task_completion_review |
| `AR-bootstrap-capture-diff`          | info     | `{actions:[task_report]}` | task_report 前建议 task_capture_diff 验证磁盘 |

**输出**：
- `total` / `created` / `updated` / `skipped` 计数
- 每条规则的 `action`（create / update / skip）+ title
- dry-run 模式末尾提示 `Use --apply to write rules to agent_rules table.`

**示例**

```bash
cw rule seed-bootstrap           # dry-run 预览计划
cw rule seed-bootstrap --apply   # 实际写入 agent_rules 表
```

---

### `rule cleanup-sync-log`：清理 agent_rule_sync_log 旧记录（GC）

```bash
# Dry-run（默认）：只预估删除数量，不执行 DELETE
cw rule cleanup-sync-log

# 自定义参数 + 实际执行删除
cw rule cleanup-sync-log --older-than 30 --keep-latest 50 --apply

# 仅清理 90 天前的记录，保留最近 100 条
cw rule cleanup-sync-log --apply
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--older-than <DAYS>` | 否 | 超过多少天的记录进入候选（默认 90） |
| `--keep-latest <N>` | 否 | 保留最近 N 条记录不删除（默认 100） |
| `--apply` | 否 | 实际执行删除（默认 dry-run，只预估） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：`--apply` 删除操作走 PROTECTED_MUTATION 保护；默认 dry-run 只预估，不执行 DELETE。

**用途**：`agent_rule_sync_log` 表记录每次 `cw rule sync` 的同步日志，
长期累积会无限增长。本命令按**双重过滤策略**清理旧记录，防止表膨胀。

**清理策略**（同时满足才删除）：
1. `created_at` 早于 `--older-than` 天前（默认 90 天）
2. 不在最近 `--keep-latest` 条记录内（按 `created_at` 倒序，默认 100 条）

**输出**：
- dry-run / apply 标题（含 `older_than` / `keep_latest` 参数回显）
- `total_before` / `deleted` / `remaining` 三项计数
- dry-run 模式末尾提示 `Use --apply to actually delete records.`

**fail-soft**：任何异常都封装为 `success=False + error`，不阻断流程。

**对应 MCP 工具**：`cleanup_agent_rule_sync_log(older_than_days, keep_latest, dry_run)`

**示例**

```bash
cw rule cleanup-sync-log                            # dry-run 预估删除数量
cw rule cleanup-sync-log --older-than 30 --keep-latest 50 --apply
```

---

## 自举闭环命令

### `bootstrap status`：自举健康摘要

```bash
cw bootstrap status
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**用途**：一行命令汇总自举闭环（bootstrap closure）整体健康度，便于
人工巡检或 CI 门禁脚本快速判断"系统是否处于一致性状态"。

**输出分组**（按区块）：

1. **DB stale 状态** — 当前 git_head 与最近一次 `workspace_scan_runs.git_head`
   是否一致；不一致会红色提示运行 `cw refresh --all`
2. **规则与候选** — `agent_rules` 已生效规则数 + `agent_rule_candidates`
   pending 候选数
3. **质量发现** — `task_quality_findings` open 数 + blocking（error/block）数
4. **审计链验证** — `audit_chain` total / broken / security_level
   （`hash_only` 或 `hmac`）
5. **最近扫描基线** — 最近一次 `workspace_scan_runs` 的 id / git_head / status
6. **任务状态分组** — tasks 表按 open / in_progress / review / applied 分组计数
7. **推荐下一条命令** — 根据当前状态推荐 `cw refresh --all` /
   `cw rule seed-bootstrap --apply` / `cw audit verify` /
   `cw task next <id>` 等

> **只读命令**：`bootstrap status` 不会写数据库，不会触发 workspace 激活，
> 不会与 MCP Server 长连接撞锁。

**示例**

```bash
cw bootstrap status
```

---

## 迁移回滚命令

全量 Rust 迁移自举计划专用：每个功能子任务在 wire-production step 完成后必须登记一条 `rollback_config` 记录，声明生产入口、回滚入口和回滚窗口。`rollback_flag=1` 时生产入口走 `rollback_entry`（切回 Python），用于线上紧急回滚。

### `rollback register`：登记回滚配置

```bash
cw rollback register --task-id <TASK_ID> --feature <FEATURE_NAME> \
    --phase <N> --production-entry <PATH> --rollback-entry <PATH> \
    [--window <ISO8601>] [--config-json <JSON>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--task-id <TASK_ID>` | 是 | Migration task ID |
| `--feature <FEATURE_NAME>` | 是 | Feature name（功能名） |
| `--phase <N>` | 是 | Phase number（0-7） |
| `--production-entry <PATH>` | 是 | Rust 生产入口路径，格式 `文件:类.方法`（如 `db/db_base.py:CodeGraphDB._connect`） |
| `--rollback-entry <PATH>` | 是 | Python 回滚入口路径（fallback 位置） |
| `--window <ISO8601>` | 否 | 回滚窗口截止时间（ISO8601，如 `2026-12-31T00:00:00`） |
| `--config-json <JSON>` | 否 | 附加配置 JSON（如 `{"flag":"CW_USE_RUST"}`） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：走 PROTECTED_MUTATION 保护；同一 `task_id` 已有记录时更新之。

**用途**：在功能子任务的 wire-production step 完成后调用，登记该功能的 Rust 生产入口路径与 Python 回滚入口路径。若同一 `task_id` 已有记录，更新之。

- `--production-entry`：Rust 生产入口，格式 `文件:类.方法`（如 `db/db_build.py:CodeGraphDB._save_file_version`）
- `--rollback-entry`：Python 回滚入口路径
- `--window`：回滚窗口截止时间（ISO8601，如 `2026-12-31T00:00:00`），过期后 Phase 7 删除 `rollback_entry`
- `--config-json`：附加配置 JSON（可选）

> **写命令**：走 CLI，避免与 MCP 长连接撞锁。

**示例**

```bash
cw rollback register --task-id T-1783253838000-xxx --feature db_build \
    --phase 3 --production-entry db/db_build.py:CodeGraphDB.build \
    --rollback-entry db/db_build_py.py:CodeGraphDBPy.build \
    --window 2026-12-31T00:00:00
```

### `rollback show <TASK_ID>`：查看回滚配置

```bash
cw rollback show <TASK_ID>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<TASK_ID>` | 是 | Task ID |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**用途**：查看指定任务的回滚配置详情（生产入口、回滚入口、rollback_flag、回滚窗口等）。

> **只读命令**：可走 MCP，WAL 安全。

**示例**

```bash
cw rollback show T-1783253838000-xxx
```

### `rollback config`：列出回滚配置

```bash
cw rollback config [--phase <N>] [--flag <-1|0|1>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--phase <N>` | 否 | 按迁移阶段过滤（0=全部） |
| `--flag <-1\|0\|1>` | 否 | 按回滚标志过滤（-1=全部、0=正常 Rust、1=已回退到 Python） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**用途**：列出所有回滚配置，可按 phase 或 rollback_flag 过滤。

> **只读命令**：可走 MCP，WAL 安全。

**示例**

```bash
cw rollback config
cw rollback config --phase 3 --flag 0
```

### `rollback set <TASK_ID> <FLAG>`：设置回滚标志

```bash
cw rollback set <TASK_ID> <0|1> [--reason "<原因>"]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<TASK_ID>` | 是 | Task ID |
| `<0\|1>` | 是 | Rollback flag（0=正常 Rust，1=回退到 Python） |
| `--reason <REASON>` | 否 | 回滚原因（可选，建议填写以便事后追溯） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写命令：走 PROTECTED_MUTATION 保护；flag 取值仅限 0/1，其余取值 argparse 直接拒绝（退出码 2）。

**用途**：紧急回滚开关。`flag=1` 时该功能切回 Python 回滚入口；`flag=0` 时恢复正常 Rust 路径。

- `flag=0`：正常 Rust 路径
- `flag=1`：回退到 Python 路径
- `--reason`：回滚原因（可选，建议填写以便事后追溯）

> **写命令**：走 CLI。这是线上紧急回滚的主入口。

**示例**

```bash
cw rollback set T-1783253838000-xxx 1 --reason "Rust 路径数据不一致，紧急切回 Python"
cw rollback set T-1783253838000-xxx 0
```

### `rollback is-rolled-back <FEATURE_NAME>`：检查功能是否回滚

```bash
cw rollback is-rolled-back <FEATURE_NAME>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<FEATURE_NAME>` | 是 | Feature name（功能名） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**用途**：检查指定功能是否已回滚到 Python。生产代码通过 `is_feature_rolled_back()` 方法（内部调用此命令的等价查询）决定走 Rust 还是 Python 路径。

**返回**：`true`（已回滚）/ `false`（正常 Rust 路径）。

> **只读命令**：可走 MCP，WAL 安全。生产代码内联调用时走 `CodeGraphDB.is_feature_rolled_back()` 方法。

**示例**

```bash
cw rollback is-rolled-back db_build
```

---

## Git 集成命令

### `git import [N]`：导入 Git 历史

```bash
cw git import          # 默认 100 个 commit
cw git import 500
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[N]` | 否 | 最大提交数（默认 100） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写面命令：导入经 daemon `import_git_history` 落库，受 PROTECTED_MUTATION 保护。

**示例**

```bash
cw git import
cw git import 500
```

### `git log [N]`：commit 历史

```bash
cw git log
cw git log 50
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[N]` | 否 | 最大提交数（默认 20） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw git log
cw git log 50
```

### `git show <COMMIT>`：commit 详情

```bash
cw git show abc123def456
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<COMMIT>` | 是 | 提交哈希 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw git show abc123def456
```

### `git stats`：Git 统计

```bash
cw git stats
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw git stats
```

### `git check-task`：检查 active task（L3 pre-commit hook）

```bash
cw git check-task
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

检查当前是否存在 active task（`in_progress` 状态的任务），供 L3 pre-commit hook 调用，作为软门禁。未设置 active task 时打印警告但不阻断 commit。

> **软门禁**：仅警告不阻断，确保不影响 git commit 正常流程。

**示例**

```bash
cw git check-task
```

### `git check-push <LOCAL_REF> <LOCAL_SHA> <REMOTE_REF> <REMOTE_SHA>`：检测 force push（L2 pre-push hook）

```bash
cw git check-push <LOCAL_REF> <LOCAL_SHA> <REMOTE_REF> <REMOTE_SHA>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<LOCAL_REF>` | 是 | 本地引用名 |
| `<LOCAL_SHA>` | 是 | 本地 commit SHA |
| `<REMOTE_REF>` | 是 | 远程引用名 |
| `<REMOTE_SHA>` | 是 | 远程 commit SHA |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

检测 pre-push 场景下的破坏性 git 操作（如 force push），供 L2 pre-push hook 调用。检测到破坏性操作时记录到 `destructive_operations` 表，作为软门禁（仅记录不阻断）。

> **软门禁**：仅记录到 `destructive_operations` 表不阻断 push，确保不影响 git push 正常流程。

**示例**

```bash
cw git check-push <LOCAL_REF> <LOCAL_SHA> <REMOTE_REF> <REMOTE_SHA>
```

### `git destructive-log [N]`：查询破坏性 git 操作历史

```bash
cw git destructive-log              # 最近 20 条
cw git destructive-log 50           # 最近 50 条
cw git destructive-log --type force_push  # 仅 force push 记录
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `[N]` | 否 | 返回条目上限（默认 20） |
| `--type <TYPE>` | 否 | 按操作类型过滤：`force_push` / `reset_hard` / `checkout_clean` |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

查询 `destructive_operations` 表中记录的破坏性 git 操作历史（force push 等）。

**示例**

```bash
cw git destructive-log
cw git destructive-log 50
cw git destructive-log --type force_push
```

### `git check-ref-transaction <OLD_VALUE> <NEW_VALUE> <REF_NAME> [FLAGS]`：审计 ref 更新

```bash
cw git check-ref-transaction <OLD_VALUE> <NEW_VALUE> <REF_NAME> [FLAGS]
cw git check-ref-transaction 0000... abcdef1234 refs/heads/main forced
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<OLD_VALUE>` | 是 | ref 旧值（全 0 SHA 表示分支新建） |
| `<NEW_VALUE>` | 是 | ref 新值（全 0 SHA 表示分支删除） |
| `<REF_NAME>` | 是 | 引用名（如 `refs/heads/main`） |
| `[FLAGS]` | 否 | ref 事务标志（如 `forced`），可选 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

Git reference-transaction hook 调用，审计破坏性 ref 更新（reset_hard / branch -f / force push），软护栏仅记录到 `destructive_operations` 表，不拦截 working tree 破坏。

**示例**

```bash
cw git check-ref-transaction 0000... abcdef1234 refs/heads/main forced
```

---

## 向量与语义搜索命令

### `semantic-search <QUERY>`：语义搜索

```bash
cw semantic-search "处理用户认证的函数"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<QUERY>` | 是 | 搜索查询文本 |
| `--top-k <TOP_K>` | 否 | 最大结果数（默认 10） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 首次使用前需运行 `cw embed` 生成向量嵌入。嵌入模型不可用时自动回退到关键词匹配。

**示例**

```bash
cw semantic-search "处理用户认证的函数"
cw semantic-search "处理用户认证的函数" --top-k 5
```

### `embed`：生成向量嵌入

```bash
cw embed           # 增量嵌入
cw embed --force     # 强制重新嵌入所有函数
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--force` | 否 | 强制重新嵌入全部（默认增量） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 写面命令：嵌入经 daemon `embed_all_symbols` 落库，受 PROTECTED_MUTATION 保护。

**示例**

```bash
cw embed
cw embed --force
```

### `similar <NAME>`：查找相似函数

```bash
cw similar "my_project::payment::process_payment"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<NAME>` | 是 | 符号全限定名 |
| `--threshold <THRESHOLD>` | 否 | 相似度阈值（默认 0.7） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw similar "my_project::payment::process_payment"
cw similar "my_project::payment::process_payment" --threshold 0.5
```

---

## 概览与导出命令

### `brief`：项目简报

```bash
cw brief
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

输出项目类型、文件数、函数数、健康评分、复杂度热点等。

**示例**

```bash
cw brief
```

### `map`：仓库模块依赖图

```bash
cw map                    # 默认 text
cw map --format mermaid
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--format <FORMAT>` | 否 | 输出格式：`text`（默认）/ `mermaid` |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw map
cw map --format mermaid
```

### `export-module-graph`：导出模块依赖图

```bash
cw export-module-graph --format mermaid
cw export-module-graph --format mermaid --output deps.mmd
cw export-module-graph --format dot --output deps.dot
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--format <FORMAT>` | 否 | 输出格式：`mermaid`（默认）/ `dot` |
| `--output <OUTPUT>` | 否 | 输出文件路径（默认打印到标准输出） |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw export-module-graph
cw export-module-graph --format mermaid --output deps.mmd
cw export-module-graph --format dot --output deps.dot
```

---

## 覆盖率命令

### `coverage import <FILE>`：导入覆盖率报告

```bash
cw coverage import coverage.lcov --format lcov
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<FILE>` | 是 | 覆盖率报告文件路径 |
| `--format <FORMAT>` | 否 | 报告格式：`lcov`（默认）/ `cobertura` |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约）；报告文件缺失或解析失败由 daemon 侧判定，属此列 |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

导入结果输出文件总数、匹配文件数、导入行数、匹配符号数。

**示例**

```bash
cw coverage import coverage.lcov
cw coverage import coverage.xml --format cobertura
```

### `coverage fn <NAME>`：函数覆盖率

```bash
cw coverage fn "my_project::payment::process_payment"
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<NAME>` | 是 | 函数全限定名 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功（含「无覆盖数据」查询结果，输出提示与搜索建议） |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw coverage fn "my_project::payment::process_payment"
```

### `coverage uncovered`：未覆盖函数

```bash
cw coverage uncovered
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

**示例**

```bash
cw coverage uncovered
```

### `coverage test`：测试覆盖率统计

```bash
cw coverage test
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

统计测试函数与模块分布（原 `--test-coverage` flag 并入本子命令，T10 阶段 2.5）。

**示例**

```bash
cw coverage test
```

---

## 所有权命令

### `who <FILE>`：文件负责人

```bash
cw who src/payment/mod.rs
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<FILE>` | 是 | 文件路径 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

综合 CODEOWNERS 和 git blame 信息。

**示例**

```bash
cw who src/payment/mod.rs
```

### `ownership-map`：所有权映射

```bash
cw ownership-map
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

按模块列出主要负责人与 owner 分布（Top 5）。

**示例**

```bash
cw ownership-map
```

---

## 工作区命令

### `workspace list`

```bash
cw workspace list
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| — | — | 无参数 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw workspace list
```


### `workspace register <NAME> <ROOT>`

```bash
cw workspace register my_project /path/to/project
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<name>` | 是 | Workspace name |
| `<root>` | 是 | Workspace root path |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw workspace register demo-root /path/to/project
```


### `workspace set <ID_OR_NAME>`

```bash
cw workspace set my_project
cw workspace set 1
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<id_or_name>` | 是 | Workspace ID or name |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw workspace set demo-root
```


### `workspace delete <ID_OR_NAME>`

```bash
cw workspace delete my_project
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<id_or_name>` | 是 | Workspace ID or name |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw workspace delete old-name
```


### `workspace generate-ignore`：自动生成 .callwardenignore

基于项目特征自动生成 `.callwardenignore`，默认 dry-run 仅预览不写入。

```bash
cw workspace generate-ignore                 # 预览（默认，当前目录）
cw workspace generate-ignore <dir>           # 指定扫描目录
cw workspace generate-ignore --apply         # 实际写入 .callwardenignore
```

---

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<dir>` | 否 | Directory to scan (default: current workspace root) |
| `--apply` | 否 | Actually write .callwardenignore (default: dry-run preview) |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw workspace generate-ignore --apply
```


## 构建上下文命令（构建上下文感知）

`cw build-context` 子命令组管理构建上下文（build context），用于区分同一份代码在不同编译配置（debug/release、不同 board、不同 include path）下的调用图。resolved_edges 按 `(workspace_id, build_context_hash)` 隔离，支持多构建配置并存。

### `build-context register <WORKSPACE_ID> <NAME>`

注册一个构建上下文。可选参数：`--flags`（编译选项）、`--defines`（宏定义）、`--includes`（头文件路径）、`--activate`（设为当前活跃上下文）。

```bash
cw build-context register 1 debug --flags -O0 -g --defines DEBUG=1 --activate
cw build-context register 1 release --flags -O2 --defines NDEBUG=1
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<workspace_id>` | 是 | Workspace ID |
| `<name>` | 是 | Context name (e.g. debug, release) |
| `--flags <[FLAGS ...]>` | 否 | Compile flags (e.g. -O2 -g) |
| `--defines <[DEFINES ...]>` | 否 | Defines (e.g. DEBUG=1 BOARD=A98) |
| `--includes <[INCLUDES ...]>` | 否 | Include paths |
| `--activate` | 否 | Set as active context |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw build-context register 1 debug --flags -O2 -g --defines DEBUG=1 --activate
```


### `build-context list <WORKSPACE_ID>`

列出某工作区下所有构建上下文。

```bash
cw build-context list 1
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<workspace_id>` | 是 | Workspace ID |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw build-context list 1
```


### `build-context show <WORKSPACE_ID> <HASH>`

查看指定构建上下文详情（含 flags/defines/includes）。

```bash
cw build-context show 1 3a2f1b8c
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<workspace_id>` | 是 | Workspace ID |
| `<hash>` | 是 | Build context hash |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw build-context show 1 3f2a9c
```


### `build-context activate <WORKSPACE_ID> <HASH>`

将指定构建上下文设为活跃。

```bash
cw build-context activate 1 3a2f1b8c
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<workspace_id>` | 是 | Workspace ID |
| `<hash>` | 是 | Build context hash |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw build-context activate 1 3f2a9c
```


### `build-context delete <WORKSPACE_ID> <HASH>`

删除构建上下文（含其 resolved_edges，不会删除 raw_calls/symbols）。

```bash
cw build-context delete 1 3a2f1b8c
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<workspace_id>` | 是 | Workspace ID |
| `<hash>` | 是 | Build context hash |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw build-context delete 1 3f2a9c
```


### `build-context import-compile-commands <FILE> <WORKSPACE_ID>`

从 `compile_commands.json` 导入构建上下文（自动提取 flags/defines/includes，按 directory 聚合）。可选 `--name`、`--activate`、`--workspace-root`（路径归一化基准）。

```bash
cw build-context import-compile-commands build/compile_commands.json 1 --name debug --activate
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<file>` | 是 | Path to compile_commands.json |
| `<workspace_id>` | 是 | Workspace ID |
| `--name <NAME>` | 否 | Context name |
| `--activate` | 否 | Set as active context |
| `--workspace-root <WORKSPACE_ROOT>` | 否 | Workspace root for path normalization |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw build-context import-compile-commands build/compile_commands.json 1 --name linux-release --activate
```


### `build-context resolve <WORKSPACE_ID> <HASH>`：计算 resolved_edges

计算指定构建上下文的 resolved_edges（**先清旧再写入，可重复执行**）。计算引擎优先走 CAS 模式（从 `cas_raw_calls` 解析，4 级 resolution：`exact_match` → `simple_name_unique` → `same_file` → `unresolved`），降级时从 `calls` 表复制（`resolution_method="from_calls"`）。

```bash
cw build-context resolve 1 3a2f1b8c
```

输出示例：
```
Resolved edges computed for: debug
  source: cas
  computed: 5428 edges
  skipped (caller unmapped): 12
  deleted old: 5416
  stored: 5428
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<workspace_id>` | 是 | Workspace ID |
| `<hash>` | 是 | Build context hash |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw build-context resolve 1 3f2a9c
```


### `build-context edges <WORKSPACE_ID> <HASH>`

查询已计算的 resolved_edges。可选 `--caller SYM_ID`（按 caller 过滤）、`--limit N`（限制返回条数）。

```bash
cw build-context edges 1 3a2f1b8c --limit 20
cw build-context edges 1 3a2f1b8c --caller 42
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<workspace_id>` | 是 | Workspace ID |
| `<hash>` | 是 | Build context hash |
| `--caller <CALLER>` | 否 | Filter by caller symbol ID |
| `--limit <LIMIT>` | 否 | Max results |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0      | 成功 |
| 1      | 运行时失败（daemon 不可达 / 校验失败 / 资源不存在等，具体错误码见输出） |
| 2      | 输入/用法错误（argparse 用法错误；或本地输入校验失败：identity 缺失、lease 凭证不完整、文件/JSON 非法等） |

**示例**

```bash
cw build-context edges 1 3f2a9c --limit 20
```


### `toolchain list-bound <WORKSPACE_ID>`：列出绑定到 workspace 的工具链

列出绑定到指定 workspace 的工具链（READ_ONLY，经 daemon RPC 查询）。

```bash
cw toolchain list-bound <WORKSPACE_ID> [--build-context-hash <HASH>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<WORKSPACE_ID>` | 是 | Workspace ID |
| `--build-context-hash <HASH>` | 否 | 按 build context hash 过滤 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> workspace 无绑定工具链时打印 `No toolchains bound to workspace <id>`，仍返回 0。

**示例**

```bash
cw toolchain list-bound 1
cw toolchain list-bound 1 --build-context-hash 3a2f1b8c   # 按 build context 过滤
```

---

## 常用组合命令示例

### 示例 1：全量构建并查看状态

```bash
cw refresh --all --force && cw status
```

### 示例 2：查找函数 → 分析影响 → 查看度量

```bash
cw search "process_payment"
cw impact "my_project::payment::process_payment"
cw fn-metrics "my_project::payment::process_payment"
```

### 示例 3：扫描缺陷 → 查看漏洞爆炸半径

```bash
cw semgrep scan --save
cw semgrep stats
cw vuln-blast --severity ERROR
```

### 示例 4：导入 Git 历史 → 分析热点

```bash
cw git import 200
cw hotspot --limit 30
cw churn --window 90d
```

### 示例 5：生成向量嵌入 → 语义搜索

```bash
cw embed
cw semantic-search "处理订单支付的函数"
cw similar "my_project::payment::process_payment"
```

### 示例 6：导出模块依赖图用于文档

```bash
cw export-module-graph --format mermaid --output docs/architecture.mmd
cw map --format mermaid > docs/repo_map.md
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

### agent_rule_sync_log 清理策略（C6）

`agent_rule_sync_log` 表记录每次 `cw rule sync` 的同步日志，长期累积会无限增长。
C6 引入 GC 清理机制，按**双重过滤策略**删除旧记录，防止表膨胀。

- **CLI 命令**：`cw rule cleanup-sync-log [--older-than 90] [--keep-latest 100] [--apply]`
- **MCP 工具**：`cleanup_agent_rule_sync_log(older_than_days, keep_latest, dry_run)`

**双重过滤**（同时满足才删除）：
1. `created_at` 早于 `--older-than` 天前（默认 90 天）
2. 不在最近 `--keep-latest` 条记录内（按 `created_at` 倒序，默认 100 条）

**默认 dry-run**：不传 `--apply` 时只预估删除数量（`SELECT COUNT`），不执行 `DELETE`；
传 `--apply` 才真正删除并 `commit`。

**fail-soft**：任何异常都封装为 `{"success": False, "error": ...}`，不抛出，不阻断流程。

## 全局 flag（非子命令参数）

以下 flag 是全局选项或首次自动配置专用，可与任意命令组合：

| flag               | 用途                                                  |
| ------------------ | ----------------------------------------------------- |
| `--lang <LANG>`    | 全局语言切换（zh_CN / en_US）                         |
| `--workspace ROOT` | 指定工作区根目录（命令前缀形式）                      |
| `--root ROOT`      | `--workspace` 的别名                                  |
| `--force`          | 强制重新配置（配合首次自动配置 / `cw refresh --all`） |
| `--no-auto-setup`  | 禁用首次运行时的自动 AI 工具配置（Lazy Auto-Setup）   |

> 各 subcommand 自身的选项（如 `search` 的 `--kind`/`--limit`、`call-chain` 的 `--depth`、
> `semgrep scan` 的 `--save`/`--config` 等）见对应 subcommand 章节。

---

## 角色化入口（cw-client / cw-agent / cw-daemon）

Call Warden 提供三个角色化入口，分离 daemon 管理、client 调用和 agent 监控职责：

| 入口        | 角色              | 平台                          | 说明                                                                  |
| ----------- | ----------------- | ----------------------------- | --------------------------------------------------------------------- |
| `cw-daemon` | Enterprise Daemon | Linux / Windows               | 启动 daemon server，管理端点（UDS / 命名管道）、registry DB、snapshot |
| `cw-client` | RPC Proxy         | Linux / Windows               | 纯 client 视角，调用 daemon RPC（不含 `serve` 启动 daemon）           |
| `cw-agent`  | Watcher Agent     | Linux / Windows               | per-UID 文件监控 agent，启动/停止/状态查询                            |

> **macOS 已收口（2026-10-23）**：macOS 不再是受支持平台（bundled SQLite 双
> 实例 malformed 撕裂面，无修法）。源码中的 macOS 能力代码保留但不作为受
> 支持路径。裁决与证据：docs/testing/ci_malformed_bisect_evidence.md
> 十三/十四节。

### cw-client 子命令

`cw-client` 是 `cw daemon` 的角色化简化版（纯 client 视角，不启动 daemon 本身）。
默认（Python）路径与 `cw daemon` 共用同一套 23 个子命令；设置 `CW_USE_RUST_CLIENT=1`
时优先 exec Rust 加速版 `cw-client`（18 个子命令，额外提供通用 `rpc`）：

```bash
# 检查 daemon 健康
cw-client ping
cw-client health

# Workspace 管理
cw-client register /path/to/project --git-remote <url> --git-head <sha>
cw-client list
cw-client status <workspace_id|workspace_instance_id>
cw-client publish <workspace_id> <db_path> --build-context <hash>

# 查询共享 snapshot
cw-client query <workspace_id> stats
cw-client query <workspace_id> symbol <qualified_name>
cw-client query <workspace_id> search <query> --kind <kind> --limit 20
cw-client query <workspace_id> callers <callee_name> --qualified-name <qn>
cw-client query <workspace_id> callees <caller_name> --qualified-name <qn>

# 运维
cw-client schema-version
cw-client backup --output <path>
cw-client restore --from <path>
cw-client gc-cas <workspace_id> --grace-days 7
cw-client gc-snapshots --keep-last 3

# 容器挂载映射
cw-client mount register <container_id> <container_path> <host_path> --type bind
cw-client mount list --container-id <id>
cw-client mount delete <container_id> <container_path>

# 通用 RPC 调用（仅 Rust 加速版 cw-client 提供；method + JSON params，可调用 task.* 等任意 daemon 方法）
cw-client rpc task.create '{"title":"示例任务"}'
cw-client rpc task.status '{"task_id":"T-xxx"}'

# 工具链 / build context / resolved edges（与 cw daemon toolchain 子命令一致）
cw-client toolchain register <name> <compiler_path> --version <v>
cw-client toolchain list
cw-client toolchain bind <workspace_id> <toolchain_id>
cw-client toolchain resolve <workspace_id> --build-context-hash <hash>
cw-client toolchain build-context register <workspace_id> <name>
cw-client toolchain resolved-edges store <workspace_id> <hash> --edges-json <json>

# Daemon 模式查询（不能修改，只查询）
cw-client mode                # 查看当前模式
cw-client mode --set auto     # 提示如何修改（不会真正设置）
```

`status` 同时接受数字主键 `workspace_id` 和 daemon 注册返回的
`workspace_instance_id`。例如：`cw-client status 730` 或
`cw-client status 4baea3ff12c2ea5c`。

### 与 `cw daemon` 的差异

| 子命令                 | `cw daemon`                     | `cw-client`                                        |
| ---------------------- | ------------------------------- | -------------------------------------------------- |
| `serve`                | ✗ 已下线（G1 影子 daemon 退休）  | ✗ 禁止（不启动 daemon 本身）                        |
| 其余子命令（Python）   | ✓ 全部可用（23 个）             | ✓ 全部可用（与 `cw daemon` 同一套 23 个）           |
| `rpc <method> <json>`  | ✗ 无此子命令                    | ✓ 仅 Rust 加速版提供（`CW_USE_RUST_CLIENT=1`）      |

### `daemon metrics`：查询 daemon 运行时指标（G13 二轮评审补全）

G13（2026-07-20）：默认通过 daemon RPC 拉取 daemon 进程的运行时指标（Rust daemon
为唯一 authority）。CLI-004 整改后已移除 `--local` 本地降级与 RPC 失败时的隐式
snapshot fallback——metrics 仅经 daemon 获取，失败即 fail-closed；`--from-file`
仅作**显式离线快照检视**（读取 daemon 周期性 dump 的 JSON 快照，不涉及本地 DB，
不作自动降级）。

```bash
cw daemon metrics [--format {prometheus,json}] [--name <NAME>] [--from-file <FILE>]
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `--format {prometheus,json}` | 否 | 输出格式（默认 json；prometheus 输出 Prometheus 文本格式） |
| `--name <NAME>` | 否 | 仅显示指定指标名（缺省显示全部；在 counters/gauges/histograms 三类中查找） |
| `--from-file <FILE>` | 否 | 显式离线读取 daemon 周期性 dump 的快照文件（JSON，不含本地 DB）；默认走 daemon RPC（Rust daemon 为唯一 authority），RPC 失败即 fail-closed |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> RPC 失败时 fail-closed 返回 1（无本地 SQLite 降级）；`--from-file` 指向的
> 快照文件不存在/损坏，或与 `--format prometheus` 组合使用时返回 2。

返回的指标包含：`memory_rss_bytes` / `cpu_total_seconds` / `uptime_seconds` /
`requests_total{method, status}` / `request_duration_seconds{method}` /
`errors_total{type}` / `jobs_submitted_total` / `job_duration_seconds{handler}` 等
内置指标。

**示例**

```bash
# 默认走 RPC 拉 daemon 进程指标（JSON 格式）
cw daemon metrics

# Prometheus 文本格式（适合 Prometheus scrape 通过 sidecar 暴露）
cw daemon metrics --format prometheus

# 按指标名过滤（在 counters/gauges/histograms 三类中查找）
cw daemon metrics --name requests_total

# 显式离线检视 daemon 周期性 dump 的 JSON 快照（daemon 不可达时人工使用；仅支持 json）
cw daemon metrics --from-file ~/.callwarden/metrics_snapshot.json
```

### daemon 运维命令（bridge / snapshot）

以下命令为 daemon runbook 运维命令，不需要 workspace_id（`build-context set-active` 除外）。

#### `daemon bridge`：检查 Windows bridge 配置与连通性

Windows bridge 健康检查（bridge transport + 端口/token 配置）。

```bash
cw daemon bridge                          # 检查默认配置
cw daemon bridge --endpoint tcp://127.0.0.1:8888   # 覆盖 bridge 端点
cw daemon bridge --token-file <path>      # 覆盖 bridge token 文件路径
```

#### `daemon snapshot-stats`：查询 SnapshotCache 统计

查询 daemon 内 SnapshotCache 统计（hit / miss / evictions）。

```bash
cw daemon snapshot-stats
```

#### `daemon snapshot-list`：列出 daemon 已知 snapshot

```bash
cw daemon snapshot-list
```

#### `daemon snapshot-evict`：驱逐 snapshot 缓存

驱逐指定 workspace 的 snapshot 缓存。

```bash
cw daemon snapshot-evict <workspace_id>
```

#### `daemon toolchain build-context set-active`：设置 active build context

设置指定 workspace 的 active build context（daemon 侧）。

```bash
cw daemon toolchain build-context set-active <workspace_id> <build_context_hash>
```

> 注意与本地 CLI 的 `cw build-context activate <WORKSPACE_ID> <HASH>` 区分：前者设置 daemon 内存 registry 中的 active context，后者写本地库 active 标志。

### daemon 核心运维子命令

`cw daemon <action>` 的全部子命令由 `cli.main` 转发到 `cli/daemon_commands.py`
（Rust 原生 daemon 的客户端；`metrics` / `bridge` / `snapshot-*` 见上文对应章节）。
通用参数 `--socket`：UDS 路径（默认 `CW_DAEMON_SOCKET`）。

#### `daemon serve`：启动影子 server（已下线）

G1（2026-09-09）整改：`cw daemon serve` 的 Python 影子 server 已下线，
argparse 不再注册该子命令（调用直接报 invalid choice）。Rust 原生 daemon 为
唯一权威，启动方式为 `cw-daemon.exe`（启动模板见 `docs/role-loop-templates/`）。

#### `daemon start`：启动本机 HTTP daemon

显式启动本机 HTTP daemon：先探活（已在运行则复用），不可达再 spawn；
detached 进程，client 退出后仍存活。

```bash
cw daemon start              # 已运行则复用（status=already_running）
cw daemon start --wait 30    # 等待就绪最长 30 秒（默认 15）
cw daemon start --force      # 探测到已有 daemon 也强制启动新实例（慎用）
```

输出 JSON：`ok` / `action` / `status`（`started` / `already_running`）/
`endpoint`；启动失败退出码 1（`E_DAEMON_START_FAILED`，检查
`~/.callwarden/runtime/current/cw-daemon.exe` 是否存在、端口是否被占用）。

#### `daemon ping`：检查 daemon 连通性

```bash
cw daemon ping
```

检查 daemon 与 peer credential（经 UDS / 命名管道发送 `ping` RPC）。

#### `daemon register <root>`：注册 workspace

```bash
cw daemon register /path/to/project \
  --git-remote <url> --git-head <sha> --toolchain <fingerprint>
```

注册当前 UID 的 workspace（`workspace.register`）；`--git-remote` /
`--git-head` / `--toolchain` 均可选（默认空）。

#### `daemon list`：列出 workspace

```bash
cw daemon list
```

列出当前 UID 已注册的全部 workspace（`workspace.list`）。

#### `daemon status <workspace_id>`：查询 workspace 与 snapshot 状态

```bash
cw daemon status 730                   # 数字 workspace 主键
cw daemon status 4baea3ff12c2ea5c      # workspace_instance_id
```

查询指定 workspace 和 snapshot 状态（`workspace.status`）；`workspace_id`
接受数字主键或 daemon 注册返回的 `workspace_instance_id`。

#### `daemon publish <workspace_id> <db_path>`：发布共享 snapshot

```bash
cw daemon publish <workspace_id> /path/to/callwarden.db --build-context <hash>
```

把已刷新的本地 DB 发布为 daemon 共享 snapshot；`--build-context` 可选（默认空）。

#### `daemon query <workspace_id> <query_type> [value]`：查询共享 snapshot

```bash
cw daemon query <workspace_id> stats
cw daemon query <workspace_id> symbol my_mod::process_payment
cw daemon query <workspace_id> search "login" --kind fn --limit 20
```

对共享 snapshot 执行查询，`query_type` 取值：`stats` / `symbol` / `search` /
`callers` / `callees` / `file` / `symbol_location` / `grep` / `issues` / `tests` /
`call_chain_down` / `topological_order` / `detect_cycles`。关键参数：
`--qualified-name`（callers/callees 精确限定名）、`--kind`、`--limit`（默认 20）、
`--max-depth`（默认 10，call_chain_down / detect_cycles 的最大深度）、
`--file-path`（file / symbol_location 使用的文件路径）、`--fixed` / `--path` /
`--include-all`（grep：字面量匹配 / 限定路径 / 含无符号命中行）、`--include-info`
（issues 包含 INFO finding）、`--reverse` / `--history`（tests 反向查询 /
运行稳定性历史）。

#### `daemon mode`：查看 daemon 模式

```bash
cw daemon mode                 # 查看当前模式
cw daemon mode --set auto      # 只提示设置方式，不真正修改
```

输出 `mode` / `available` / `required` / `socket`。`--set {auto,enterprise,local}`
仅打印"请设置环境变量 `CW_DAEMON_MODE=<值>`"，修改需自行设置环境变量。

#### `daemon health`：检查 daemon 健康状态

```bash
cw daemon health
```

经 HTTP thin client 调用 Rust daemon 的 `/health`（Rust 权威）；Python 仅做
client + 输出格式化，失败 fail-closed 返回结构化错误（如
`E_DAEMON_UNAVAILABLE`），不降级本地 SQLite。

#### `daemon manifest`：显示 daemon HTTP manifest

```bash
cw daemon manifest
```

输出 daemon HTTP manifest（authority / endpoint / registry revision）。
manifest 发现与校验（missing / stale / wrong-authority / daemon-unavailable）
由 Rust daemon + authority-scoped manifest 完成，失败 fail-closed，不静默
降级到本地 SQLite 或 `get_db()`。与 `health`/`capability` 同属 CLI-01（A′ 恢复）
诊断链路。

#### `daemon capability`：查询 capability registry

```bash
cw daemon capability
```

查询 daemon capability registry（MCP/RPC 工具矩阵）；同样经 HTTP thin client
查询 Rust daemon，fail-closed。

#### `daemon schema-version`：查询 registry DB schema 版本

```bash
cw daemon schema-version
```

查询 daemon registry DB 的 schema 版本（`schema.version` RPC）。

#### `daemon backup --output <path>`：备份 registry DB

```bash
cw daemon backup --output /backup/registry.db.bak
```

`--output` **必填**：备份输出路径（绝对化后传给 daemon）。

#### `daemon restore --from <path>`：从备份恢复 registry DB

```bash
cw daemon restore --from /backup/registry.db.bak
```

`--from` **必填**：备份文件路径。

#### `daemon gc-cas <workspace_id>`：GC CAS 存储

```bash
cw daemon gc-cas <workspace_instance_id> --grace-days 7
```

清理 CAS 存储中的未引用 content；`--grace-days`（int，默认 7）——只清理指定
天数之前的未引用内容。

#### `daemon gc-snapshots`：GC 快照

```bash
cw daemon gc-snapshots --keep-last 3
```

按保留策略 GC snapshot：每个 workspace 保留最近 N 个（`--keep-last`，int，
默认 3）。

### `cw-agent <COMMAND>`：per-UID 文件监控 agent（start / stop / status）

`cw-agent` 是独立角色入口（PyInstaller bundle / 角色化入口，非 `cw` 的 argparse
子命令），per-UID 文件监控 agent，通过 IPC 将文件变更转发给 Enterprise Daemon：

```bash
cw-agent <COMMAND> [options]    # COMMAND: start / stop / status
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<COMMAND>` | 是 | `start`（启动 watcher，前台运行）/ `stop`（读取 PID 文件，发送 SIGTERM）/ `status`（查询 agent 运行状态） |
| `--watch-dir <DIR>` | 否 | （仅 start）监控目录，默认当前工作目录 |
| `--workspace-id <ID>` | 否 | （仅 start）workspace_instance_id，默认从 watch-dir 推导 |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 平台门禁：非 Linux/Windows/macOS 平台 rc=2；未知子命令 rc=2；
> `start` 的 `--watch-dir` 目录不存在 rc=2；无参数运行或 `--help` 打印用法（rc=0）。
> PID 文件路径：`~/.callwarden/agent.pid`（per-UID），日志：`~/.callwarden/agent.log`。

**示例**

```bash
cw-agent start --watch-dir /path/to/project --workspace-id ws-001
cw-agent status
cw-agent stop
```

### `config check-role <ROLE>`：检查平台角色支持

检查当前平台是否支持指定角色。

```bash
cw config check-role <ROLE>
```

**参数**

| 参数 | 必填 | 说明 |
| ---- | ---- | ---- |
| `<ROLE>` | 是 | 要检查的角色：`local` / `client` / `agent` / `daemon` / `all` |

**退出码**

| 退出码 | 含义 |
| ------ | ---- |
| 0 | 成功 |
| 1 | daemon 业务拒绝 / 传输失败 / 运行时失败（见全局退出码契约） |
| 2 | 输入/用法错误：argparse 用法错误，或本地输入校验失败 |

> 平台差异：Windows/macOS 仅支持 `local` / `client` 角色；`agent` / `daemon` /
> `all` 需 Linux（SO_PEERCRED + SCM_RIGHTS + UDS）。角色不支持时打印 ❌ 与提示。

**示例**

```bash
cw config check-role local
cw config check-role agent
```

### 平台门禁

`cw-client`、`cw-daemon`、`cw-agent` 三个入口在 D0 后支持两平台（Linux / Windows；macOS 已于 2026-10-23 收口）：

- **Linux**：UDS (SO_PEERCRED) + SCM_RIGHTS FD 传递
- **Windows**：命名管道 (`\\.\pipe\callwarden-<user-sid>`) + `canonical_bytes_b64` 载荷路径

> macOS 的 UDS (LOCAL_PEERCRED) 等源码能力保留，但平台已收口（bundled
> SQLite 双实例 malformed 撕裂面，无修法）。证据：
> docs/testing/ci_malformed_bisect_evidence.md 十三/十四节。

```
$ cw-client ping
{"status": "ok", "platform": "windows", "endpoint": "\\\\.\pipe\\callwarden-S-1-5-21-..."}
```

端点排除（Req 14.20–14.21）：不使用监听 TCP 端口、不使用本机 HTTPS 端点、Windows 不使用 AF_UNIX——因为它们都不提供 Peer_Credential，无法满足 Req 14.5。

---

## cw experiment（P0 盲评对照实验）

P0 阶段盲评对照实验全生命周期管理命令。所有输出标记 `non_product_evidence=True`，
不构成产品 Evidence，不开放 P1 hard gate。

```bash
# 创建批次（自动生成默认协议并锁定）
cw experiment batch-create --seed 42 [--min-valid 30] [--min-nontrivial 20] [--json]

# v2 分层配对批次：同一 strata 使用 pair-slot 0/1，协议保证一控一治
cw experiment batch-create --seed 42 --assignment-mode paired_v2 [--json]

# 手动冻结协议
cw experiment batch-lock <batch_id> [--json]

# 列出所有批次
cw experiment batch-list [--json]

# 设置 P0 Stage_Toggle（Req 13.12/13.21）
cw experiment toggle-set --scope {global,workspace,task} --value {on,off} [--scope-key K] [--json]

# 显示解析后的 P0 开关（task > workspace > global 继承）
cw experiment toggle-show [--task-id T] [--workspace-id W] [--json]

# 纳样（资格检查→确定性分组→构建 blind view→写 JSONL）
cw experiment admit <task_id> <batch_id> [--strata K] \
    [--pair-id PAIR_ID] [--pair-slot {0,1}] \
    [--notes-file IMPLEMENTER_NOTES_UTF8_FILE] [--json]

# Control 必须提供 notes；Treatment 首轮不得提供 notes。缺失或泄露均 fail-closed。

# 记录 review 原始指标（Req 12.6）
cw experiment record-metrics <task_id> <batch_id> --tp N --fp N --misses N --duration SEC \
    --tokens-source {real,unavailable} [--tokens N] [--tokens-unavailable-reason TEXT] \
    [--reopen N] [--defects N] [--rollbacks N] [--obs-window W] [--json]

# v2：tokens-source=real 必须提供真实 provider token 计数；无法采集时使用
# tokens-source=unavailable + 非空 reason，禁止用字符数估算。

# 记录 reveal 前后 verdict 变更（Req 12.7）
cw experiment record-verdict <task_id> <batch_id> --changed {yes,no} \
    [--reason-code {no_change,new_fact,corrected_misunderstanding}] [--json]

# 在 JSONL 同一父目录生成 report_<batch_id>.json 和 evidence_manifest_<batch_id>.json
cw experiment report <batch_id> --artifacts-dir <JSONL_PARENT_DIR> [--json]

# 记录 Implementer_Notes 揭示事件（Req 12.7）
cw experiment record-reveal <task_id> <batch_id> [--sealed] [--json]

# 记录无效样本（Req 12.8）
cw experiment record-invalid <task_id> <batch_id> --reason-code CODE [--detail TEXT] [--json]

# 记录披露/完整性事件（Req 12.18/12.20）
cw experiment record-incident <task_id> <batch_id> --type {disclosure,integrity} \
    --reason-code CODE [--detail TEXT] [--json]

# 手动暂停批次（Req 12.15-12.21）
cw experiment pause <batch_id> --trigger TRIGGER [--reason TEXT] [--json]

# 汇总评估 + 机器可读 G0 决策
cw experiment report <batch_id> [--json]
```

`report --json` 输出包含 `g0_decision` 字段：

```json
{
  "eligible_for_p1": false,
  "gray_zones": [],
  "pause": null,
  "insufficient_sample": true,
  "non_product_evidence": true
}
```

### 实验记录位置

所有实验记录以 JSONL 追加写入 `~/.callwarden/experiments/<batch_id>/` 目录：

| 文件                     | 内容                                                |
| ------------------------ | --------------------------------------------------- |
| `blind_views.jsonl`      | 纳样时的 Minimal_Blind_View 快照（Req 12.24/12.25） |
| `metrics.jsonl`          | review 原始指标 + verdict 变更 + reveal 事件        |
| `invalid_samples.jsonl`  | 无效样本及原因码                                    |
| `incidents.jsonl`        | 披露/完整性事件                                     |
| `evaluation_report.json` | `report` 命令输出的完整评估（含 g0_decision）       |

每条记录均携带 `non_product_evidence: true` 标记。

### G0 判定语义（Req 12.14）

`report` 输出的 `g0_decision.eligible_for_p1` 为 `true` 当且仅当：

1. 有效样本数 ≥ `min_valid_tasks`（默认 30）
2. 非平凡代码变更样本 ≥ `min_nontrivial_code_changes`（默认 10）
3. 两组 Recall 分母均大于 0；`TP=0 且 misses=0` 是不可估计，不按 Recall=0 计算
4. 缺陷检出率 Treatment ≥ Control（方向性）
5. 误报率 Treatment 不超过 Control + 10pp
6. 中位审查延迟 Treatment 不超过 Control × 1.25
7. Control/Treatment 首轮视图披露差异通过完整性校验，且无未解决的安全/盲法事件

满足以上全部条件时 `eligible_for_p1=true`，表示"可以进入 P1 决策讨论"，
**不代表 P1 已实现或已启用**（Req 13.1）。

### 灰区语义（Req 12.27–12.29）

| 指标         | 灰区范围                                    | 效果                              |
| ------------ | ------------------------------------------- | --------------------------------- |
| 误报率差值   | Control + 10pp < Treatment ≤ Control + 20pp | 不授权 P1，继续纳样，记录灰区观察 |
| 中位延迟增幅 | 25% < 增幅 ≤ 50%                            | 同上                              |

灰区观察期间：批次**不暂停**（暂停条件仍为 Req 12.15–12.20 的硬阈值），
但 `eligible_for_p1` 保持 `false`，直到灰区观察解除。

### 暂停与恢复（Req 12.21–12.24）

触发暂停的 6 种条件（`PauseTrigger`）：

| 触发器                 | 条件                                                                    |
| ---------------------- | ----------------------------------------------------------------------- |
| `critical_miss`        | Treatment 出现 Control 没有的关键遗漏，且原因是 blind view 缺少必要事实 |
| `fp_rate_divergence`   | Treatment 误报率超 Control > 20pp，连续 10 个 Treatment 样本            |
| `latency_divergence`   | 中位延迟增幅 > 50% 持续 2 周，或无效率 > 30%                            |
| `disclosure_violation` | Treatment blind view 泄露 Implementer_Notes/先前 verdict/敏感推理       |
| `snapshot_drift`       | Workspace_Snapshot 漂移导致 > 20% 样本不可归因                          |
| `integrity_violation`  | 实验诱导伪造独立性或伪造 Evidence                                       |

暂停后：

- 批次记录、锁定的指标定义、分母、观察窗口和阈值**全部保留**
- 停止新样本纳样
- 若投影/抽样/指标/窗口规则需变更，**必须创建新批次**（`predecessor_batch_id` 指向旧批次）
- 若暂停机制本身无法记录/执行，fail-safe 停止纳样直到恢复（Req 12.24）

## cw collab（多 LLM 契约协同治理写命令）

经统一权威路由 `route_rpc(..., 'GOVERNANCE_WRITE')` 的治理写操作入口（Req 14），落到 daemon
authority（HTTP authority face，与 `cw lease` / `cw task` 写命令面同一真相源）。所有操作不可绕过 daemon；
连接失败时 auto-start → Degraded_Mode → Governance_Write fail closed + 平台恢复指引。

```bash
# 发布 Envelope（snapshot.publish）
cw collab publish --workspace PATH [--envelope FILE] [--json]

# 提交 Verdict 并封存（verdict.submit，完整 task-bound provenance）
cw collab verdict \
  --task-id ID --step-id ID \
  --contract-id ID --contract-hash HASH --contract-revision N \
  --role-contract-id ID --role-contract-hash HASH --role-contract-revision N \
  --snapshot-id ID --view-manifest-hash HASH --request-id ID \
  --phase {blind_first_pass,post_reveal_amendment} --overall {pass,block} \
  --attestation TEXT --agent-instance-id ID --role {reviewer,independent_reviewer} \
  [--agent-id ID] [--session-id ID] [--model-id ID] \
  [--amendment-ref ID] [--clause-results JSON] [--findings JSON] [--verdict-id ID] \
  [--lease-token TOKEN] [--fencing-counter N] [--json]

# 提交 Reveal_Event（reveal.submit）
cw collab reveal --event-id ID --task-id ID [--notes FILE] [--json]

# 触发 Gate 判定（gate.decide）
cw collab gate-trigger --gate-id ID --clause NAME --value {true,false} [--json]
```

`verdict` 的 `--view-manifest-hash` 由 daemon 强制非空（缺失/空白在写入前拒绝），`--request-id`
为同任务内唯一幂等键；`--findings` / `--clause-results` 为 JSON 数组字符串
（reviewer lease 的 `--lease-token` / `--fencing-counter` 亦由 daemon 校验）。

> daemon 实现状态：`verdict.submit` 与 `evidence.append/query` 已在 `handle_collab_rpc` 实现；
> `reveal.submit` / `gate.decide` 当前仍返回 `method_not_found`（待 daemon 侧实现），
> CLI 已按统一权威路由提交，daemon 补齐后无需再改 CLI。

所有子命令支持 `--json` 输出机器可读 JSON。失败路径输出 Structured_Reason（稳定错误码
`E_GOVERNANCE_WRITE_DEGRADED` + i18n key `error.governance_write_degraded` + 平台恢复指引），
exit code 1。

### 降级行为（Req 14.27–14.30）

| 操作类别                                                | daemon 不可用时行为                           |
| ------------------------------------------------------- | --------------------------------------------- |
| Governance_Write（publish/verdict/reveal/gate-trigger） | fail closed，输出 Structured_Reason，状态不变 |
| Index_Write                                             | 直连写入（collab 命令不涉及）                 |
| read_only                                               | 直连只读（collab 命令不涉及）                 |

### 自动唤起与有界等待窗口（Req 14.22–14.26）

客户端无法连接 daemon 端点时，按以下流程处理：

1. **尝试自动唤起**：获取跨进程互斥（Windows named mutex / Linux file lock），启动 daemon 进程
2. **有界等待窗口**：默认 10 秒（客户端时钟），窗口内以指数退避重试连接
3. **窗口内成功**：在已建立的连接上继续原请求
4. **窗口耗尽**：进入 Degraded_Mode，按操作类型分级处理（见上表）

**两平台唤起方式**（macOS 已于 2026-10-23 收口，其 launchd 源码能力保留但不作为受支持路径）：
- Windows：detached process（存活到客户端进程退出之后）
- Linux：systemd user service 激活

**单实例互斥**（Req 14.23）：多个会话并发触发唤起时，互斥原语确保每用户端点至多一个运行中的 daemon 进程，不创建第二个 Protected_Mutation 串行化点。

**降级产物审计**（Req 14.31, 14.33）：Degraded_Mode 产出的记录标记降级原因；降级路径直连 SQLite 写入的记录因缺有效 daemon 签发 Attestation，被 Evidence_Gate 判为 invalid，不满足任何 Blocking_Clause——因此系统**不设物理写屏障**。

### "非产品 Evidence" 限制（Req 12.23）

P0 实验记录**标记为 non-product Evidence**，具体限制：

- 不得用于 P1 hard-gate 声明
- 不得作为产品能力已实现的证据
- 不得替代确定性验证（测试通过、CI 绿灯等）
- 仅用于评估"盲评是否值得产品化"的决策输入

### 阶段可用性（Req 13.1）

| 阶段                   | 当前状态                | 说明                                                                                                                                                                                               |
| ---------------------- | ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| P0（盲评对照实验）     | ✅ 已实现                | 本章节所述命令                                                                                                                                                                                     |
| D0（跨平台 daemon 化） | ✅ 已实现                | 三平台端点、Peer_Credential、串行化点、Authoritative_Clock、Attestation、Stage_Toggle、稳定错误码、自动唤起与互斥、Degraded_Mode 分流                                                              |
| P1（契约驱动协作）     | ✅ 已实现                | Canonical Envelope + Role View allowlist + Blind Verdict/Reveal/Amendment + Evidence Ledger + Evidence Gate + CLI/MCP 工具                                                                         |
| P2（DAG 依赖调度）     | ✅ 已实现                | 四类依赖 + artifact freshness + interface identity + 硬依赖图 + 环检测 + provider 选择（`cw dependency` 命令）                                                                                     |
| P3（Agent 身份审计）   | ✅ 已实现                | `cw task report/apply/close/reopen --agent-id/--session-id/--model-id/--role` + `cw identity revoke`（强制 `--revocation-mode`）+ Attestation 校验与撤销 + Identity fail-closed 接入 Evidence Gate |
| P4（安全租约与分派）   | 🔲 planned / unavailable | 需 P1 + P3 启用                                                                                                                                                                                    |

在对应阶段启用前，其所有能力均表示为 planned 且 unavailable，
不得在文档、输出或判定中暗示已实现。

失败路径输出 Structured_Reason（稳定错误码 + i18n key），exit code 1。

---

## cw dependency（P2 依赖图与环检测诊断）

> **边界声明**：P2 只做无环校验和诊断，**不提供**自动排程、资源优化、自动 assignment 或复杂 DAG scheduler（Req 9.10）。

### cw dependency inspect

查看任务或契约的依赖声明与 artifact/interface freshness。

```bash
# 按任务查看
cw dependency inspect --task-id T-1785574343893-60e782d3

# 按契约查看
cw dependency inspect --contract-id C-test --revision 1

# JSON 输出
cw dependency inspect --task-id T-xxx --json
```

### cw dependency list

列出硬依赖图边。

```bash
cw dependency list                     # 全部
cw dependency list --contract-id C-test  # 按契约过滤
```

### cw dependency cycle

检测硬依赖图中的环。

```bash
cw dependency cycle           # 文本输出
cw dependency cycle --json    # JSON 输出
```

### cw dependency explain

解释指定 revision 的依赖验证结果。

```bash
cw dependency explain --contract-id C-test --revision 1
```

输出包含：验证状态（通过/拒绝）、错误列表、cycle path（如有）、无自动排程声明（Req 9.10）。

### cw dependency provider-select

记录显式 interface provider 选择（Req 9.9，写操作）。

```bash
cw dependency provider-select \
  --consumer-task-id T-consumer \
  --contract-id C-test \
  --revision 1 \
  --interface-name auth.verify \
  --provider-task-id T-provider
```

---

## cw identity（P3 Agent 身份审计）

> **边界声明**：Identity（agent_id/session_id/model_id/role）仅作 actor attribution，
> **不等于** assignment/lease/ownership/SQLite lock（Req 10.5, 10.7）。
> Attestation 只能由 daemon 签发（Req 14.13）；`cw identity revoke` 只追加撤销账本，
> 不修改任何既有 verdict/Evidence payload。

### cw identity revoke

追加一条 Attestation 撤销记录（Req 10.10-10.12，写操作）。

```bash
cw identity revoke \
  --issuer daemon \
  --signing-key-id <key-id> \
  --revocation-mode compromised \
  --reason "密钥泄露，紧急撤销" \
  --agent-id admin-agent --session-id sess-admin --model-id glm-5.2 --role reviewer
```

- **`--revocation-mode` 必填且无默认值**（`compromised` / `rotated`）：
  缺失或取值非法时以 `ERR_REVOCATION_MODE_REQUIRED` 拒绝，且**不追加任何记录**
- `compromised`：匹配 issuer+签名密钥的全部记录判 invalid（与签发时间无关）
- `rotated`：仅签发时间晚于撤销时间的记录判 invalid（例行轮换不判死历史账本）
- 每次撤销只追加**一条** `Attestation_Revocation_Record`，不产生 N 条逐条失效事件；
  `invalid` 由查询时派生（MCP `get_attestation_validity` / `list_attestation_revocations`）
- `--agent-id/--session-id/--model-id/--role` 记录发起者 Identity（P3，Req 10.1）

---

## cw lease / cw assignment（P4 Assignment 与安全 Lease）

> **边界声明（正面陈述，Req 11.13, 14.32）**：Lease 保证的是 **daemon 在线期间**的并发
> 正确性——同一 task/role 任一时刻只有一个有效持有者，旧持有者在新 lease 生效后无法再
> 写入（fencing，Property 11）。**不**提供自动 dispatch、抢占或中央调度；防篡改保证归属
> Attestation 校验与追加式 Evidence_Ledger，**不**防止离线直接改库。SQLite 写锁只负责
> 短事务互斥，不提供业务 ownership（Req 11.10）。Degraded_Mode 下 Lease
> 获取/续租/释放属 Governance_Write，一律 fail closed（Req 14.31）。

### cw lease acquire（写，Req 11.2-11.3）

原子比较当前 Lease 状态后获取；fencing counter 单调递增（Req 11.3）。

```bash
cw lease acquire <task-id> \
  --role implementer \
  --agent-id <agent-id> --session-id <session-id> --model-id <model-id> \
  --ttl 3600
```

- holder Identity（`--agent-id/--session-id/--model-id`）**必填**；缺失以 `E_ASSIGNMENT_INCOMPLETE` 拒绝
- 已有未过期 active lease → `E_LEASE_ACTIVE_EXISTS` 拒绝；已过期 → 覆盖
- **raw token 仅在成功响应输出一次**（Req 11.2）：数据库只存 sha256 hash，日志/审计事件不存储

### cw lease renew（写，Req 11.4-11.5）

```bash
cw lease renew <task-id> --role implementer --token <raw-token> \
  --agent-id <agent-id> --session-id <session-id> --model-id <model-id> --ttl 7200
```

- 要求当前 token hash / holder Identity / 未过期；错误 token → `E_LEASE_TOKEN_MISMATCH`，
  过期 → `E_LEASE_EXPIRED`，holder 不符 → `E_LEASE_HOLDER_MISMATCH`
- **幂等**（Req 11.5）：重复有效 renew 返回同一 lease 状态，不递增 counter，不创建新 lease

### cw lease release（写，Req 11.6-11.7）

```bash
cw lease release <task-id> --role implementer --token <raw-token> \
  --agent-id <agent-id> --session-id <session-id> --model-id <model-id>
```

- 当前 token 匹配时原子追加 release 审计事件并置 released
- **幂等**（Req 11.7）：重复 release 返回同一 released 状态，不改变 counter，不创建第二个 active lease

### cw lease status / list（只读）

```bash
cw lease status <task-id> [--role <role>] [--json]
cw lease list [--task-id <id>] [--role <role>] [--json]
```

- `status`：当前 active lease（含 token_hash 供校验，不含 raw token）；无 active 时返回最近历史
- `list`：append-only 审计事件账本（acquire/renew/release），不含 raw token

### cw assignment create / show / revoke（Req 11.1）

```bash
cw assignment create <task-id> --role implementer \
  --agent-id <agent-id> --session-id <session-id> --model-id <model-id>
cw assignment show <task-id> [--role <role>]
cw assignment revoke <assignment-id>
```

- assignment 绑定 task+role+holder Identity，**不把** workspace `active_task_id` 当作
  assignment authority（Req 13.4）；assignment 可以没有 lease（Req 11.12）
- `show` 只读；`create`/`revoke` 写操作（读/写分类见 [TOOLS.md](../TOOLS.md)）

### task report/apply/close/reopen 的 Lease 凭证（P4，Req 11.8-11.9）

```bash
cw task report <task-id> <step-id> --result "实现完成" \
  --agent-id <id> --session-id <id> --model-id <id> --role implementer \
  --lease-token <raw-token> --fencing-counter <n>
```

- 提供 `--lease-token` + `--fencing-counter` 时启用受保护写路径：
  过期（`E_LEASE_EXPIRED`）、token 不匹配（`E_LEASE_TOKEN_MISMATCH`）、旧 counter
  （`E_LEASE_FENCING_STALE`）、无 active lease（`E_LEASE_NOT_FOUND`）均在写入前拒绝，
  **不改变 task data**；只提供其一 → 凭证不完整拒绝（fail closed）
- 不提供 Lease 凭证时保持向后兼容（不启用受保护写）
- **Lease 校验通过不代表 mutation 被授权**：角色权限、Independent Review 与 Evidence
  Gate 仍然适用（Req 11.11）；task_close 仍只在 applied 后收尾

---

## 下一步

- [MCP 工具参考](mcp_tools.md)：通过 MCP 协议调用 244 个工具
- [架构设计](architecture.md)：理解数据库 Schema 和 Mixin 架构
- [部署指南](deployment.md)：Docker 部署与多容器共享
