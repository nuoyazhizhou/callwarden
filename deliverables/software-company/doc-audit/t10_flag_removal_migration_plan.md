# T10 CLI `--flag` 移除迁移清单（选项 B 执行计划）

> 日期：2026-10-07
> 目标：彻底移除 cli/main.py 的 ~48 个 deprecated `--flag`（`_DEPRECATED_FLAG_MAPPING` L133-218
> + create_parser() argparse），统一为已存在的等价 subcommand。遵循"项目未上线、不留同义词、
> 零二义性"原则。基于 context-gatherer 穷尽式活使用点扫描。
> **只迁移活引用**，不碰历史归档（archive/ / cw_task_commit_ledger.json / docs/evidence/ /
> docs/reports/ / .temp/ / outputs/ / .qoder/）。

## 关键前提（降低风险）

- **所有 subcommand 已存在且功能等价**（`_SUBCOMMANDS` L51-106 齐全，每个 handler docstring
  都标了"等价 flag"）。移除 flag **不需要新建任何 subcommand**，是纯粹的"调用方迁移 + 删 flag 定义"。
- `--version` / `--help` / `--force` / `--all` / `--watch` / `--lang` / `--workspace` / `--root`
  **不是** deprecated flag（是标准 flag 或新 subcommand 的选项），**保留不动**。

## 执行阶段（每阶段独立 commit + 验证）

### 阶段 1：功能性使用点迁移（先做，零破坏——flag 仍在，只改调用方）

这些点若在 flag 移除后不改会破坏基础设施，**必须先迁移**。此阶段 flag 仍保留，改完所有调用方用
subcommand，验证通过后再进阶段 3 删 flag。

| # | 文件:行 | 现状（flag） | 改为（subcommand） | 类型 |
|---|---------|------------|-------------------|------|
| 1 | install.py:938 | `{cmd} --refresh-all &` | `{cmd} refresh --all &` | pre-commit hook（核心） |
| 2 | install.py:980 | 错误文案 `cw --refresh-all 重试...` | `cw refresh --all` | hook 文案 |
| 3 | .github/workflows/callwarden.yml:37 | `cw --refresh-all` | `cw refresh --all` | CI |
| 4 | .github/workflows/release.yml:257 | `cw --refresh-all` | `cw refresh --all` | CI 发布说明 |
| 5 | .github/workflows/release.yml:258 | `cw --search "login"` | `cw search "login"` | CI 发布说明 |
| 6-19 | enterprise-release.yml:480-634 | `--register-workspace`/`--set-workspace`/`--refresh`/`--query`（14 处） | `workspace register`/`workspace set`/`refresh`/`query` | CI smoke/upgrade |
| 20 | e2e/run_platform_e2e.py:421 | `["--refresh-all"]` | `["refresh","--all"]` | E2E |
| 21 | e2e/run_platform_e2e.py:439 | `["--refresh",path]` | `["refresh",path]` | E2E |
| 22 | e2e/run_platform_e2e.py:748 | `["--refresh-all"]` | `["refresh","--all"]` | E2E |
| 23 | e2e/run_platform_e2e.py:870 | `["--refresh",path]` | `["refresh",path]` | E2E |
| 24 | db/db_agent_rules.py:479 | 种子规则文本 `运行 cw --refresh-all` | `cw refresh --all` | 写入 DB 的规则文本 |
| 25 | db/db_bootstrap.py:1211 | `recommended = "cw --refresh-all"` | `cw refresh --all` | 运行时推荐命令 |
| 26 | cli/main.py:8398 | `"Run 'cw --refresh-all' to initialize."` | `cw refresh --all` | 运行时提示 |
| 27 | cli/main.py:9426 | `建议 cw --refresh-all` | `cw refresh --all` | 运行时提示 |
| 28 | cw.py:8-11 | usage `cw --refresh-all`/`--search`/`--call-chain` | subcommand 形式 | 顶层 usage |
| 29 | i18n status_rebuild_hint 默认串 | `建议运行 cw --refresh-all` | `cw refresh --all` | i18n 默认文案 |

> enterprise-release.yml 的 14 处：L480/503/539/598 `--register-workspace`→`workspace register`；
> L481/504/540/599/616/634 `--set-workspace`→`workspace set`；L482/505/541/600 `--refresh`→`refresh`；
> L543 `--query`→`query`。

### 阶段 2：确认"纯 flag 无 subcommand"缺口（补齐再删）

T9 曾列 11 个疑似"纯 flag 无 subcommand"。经 create_parser() 核对，这些 flag 实际都有对应
subcommand（见 `_DEPRECATED_FLAG_MAPPING` 第 3 列）或作为子命令选项存在：
- `--impact` → `impact <QN>`（subcommand 存在）
- `--changes` → `file changes [SINCE]`
- `--top-callers` → `callers --top N`
- `--orphan-symbols` → `callers --orphans`
- `--deepest` → `call-chain --deepest N`
- `--module-calls` → `call-chain --module-calls N`
- `--call-heatmap` → `call-chain --heatmap`
- `--export-module-graph` → `call-chain --export-module-graph`
- `--symbol-content-by-hash` → （需确认 subcommand）
- `--git-blame` → （需确认）
- `--issue-summary` → `function-issues --summary`

**阶段 2 动作**：逐个确认这些 subcommand/子选项真实存在且功能等价（跑 `cw <subcommand> --help`）。
缺口的先补 subcommand，不缺的直接进阶段 3。

### 阶段 3：移除 flag 定义（所有调用方迁移完 + subcommand 齐全后）

1. 删除 cli/main.py `create_parser()` 里 ~48 个 deprecated flag 的 `add_argument`。
2. 删除 `_DEPRECATED_FLAG_MAPPING` 字典 + `_emit_deprecated_flag_warning` 调用。
3. 删除主 help 底部的 deprecated flag 清单渲染。
4. 删除 flag 模式的 dispatch 分支（`elif args.xxx:` 处理）。
5. 清理 handler docstring 里的"等价 flag: --xxx"注记。
6. 保留 flag 相关 i18n 键或一并清理。

### 阶段 4：文案性文档统一（不破坏执行，一致性）

AGENTS.md 规则 23/32 正文 + 代码读取工具分工表（L256-257）、cicd/bootstrap_check.py:44 docstring、
install.py docstring 893/904、以及用户文档（TOOLS.md/README.md/quickstart.md/cli_reference.md/
deployment.md/architecture.md/agent-usage-guide.md/capability_showcase.md/mcp_tools.md）里的
`cw --flag` 示例统一改 subcommand。

> 注意：AGENTS.md 规则正文改动会影响 Agent 行为理解，需谨慎逐条改（保持规则语义不变，只改命令形式）。

## 风险与验证

| 阶段 | 风险 | 验证 |
|------|------|------|
| 1 | 低（flag 仍在，只改调用方） | 装 hook 试跑 commit；本地模拟 CI 命令；check-imports |
| 2 | 低（只读确认） | `cw <subcommand> --help` 逐个 |
| 3 | 中（删 flag，若有漏改调用方会报错） | 全量 `cw --help`；跑 tests/ 的 CLI 测试；grep 确认无残留 `--flag` 调用 |
| 4 | 低（纯文档） | 文档 lint |

## 不迁移（历史归档，保留快照）

archive/ · cw_task_commit_ledger.json · docs/evidence/ · docs/reports/ · .temp/ · outputs/ ·
.qoder/ · deliverables/ 下的历史任务记录。这些记录当时的 flag 用法，是审计快照。
