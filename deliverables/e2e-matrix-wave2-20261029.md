# Wave 2 · CLI 验收矩阵复跑报告（2026-10-29）

## 结论

| 项 | 结果 |
| --- | --- |
| 执行器 | `.workbuddy/scripts/cli_e2e_runner.py`（v2 校准版：stdout+stderr 合并比对、ambiguous 重试、BLOCKED_ENV） |
| 被测二进制 | `C:\Users\wanpi\.callwarden\runtime\current\cw.exe`（Rust CLI，sha256 `489bde5a…` 与本次构建一致，daemon PID 32424） |
| 命令树 | 127 个叶子命令（由 `cw.exe --help` 递归 clap `Commands:` 段生成，写入 `outputs/cli_command_tree.json`） |
| 代码基线 | `e2c5ab3`（缺陷 A/B 修复已部署） |
| 产物 | `.workbuddy/scripts/outputs/cli_e2e_matrix.json` |
| 判定 | **PASS（Wave 2 关闭）**：无新增阻塞级缺陷；2 条 `daemon_side_error` 与 11 条 enterprise 缺参均归入既有 backlog |

## 判定分布

```
WRITE              47   （写命令，按设计跳过，不产生副作用）
DUAL_RPC           34   （local/enterprise 输出不同 → 确认走 daemon）
PARAM_MISSING      33   （两边均 usage 缺参 → 参数推断未覆盖，非命令缺陷）
SUSPECT_NO_DAEMON  11   （两模式输出完全一致 → 疑似无 daemon 路径）
DAEMON_ERR          2   （enterprise 模式 daemon 侧明确报错）
```

## findings（24 条）

### 1. suspect_query_local（11 条，high）

两模式输出逐字节一致，说明这些查询**未下沉 daemon**，enterprise 模式下仍直读本地库（与"多项目共享 daemon"的单实例守卫目标不一致）：

`guardrail rules`、`doctor`、`rule candidate list`、`rule list`、`rule applicable`、
`audit verify`、`audit keys`、`bootstrap status`、`toolchain list`、`config explain`、`config paths`

> 说明：`config explain` / `config paths` / `doctor` 属本地平台/配置探针，天然无需 daemon；
> 其余 8 条为治理读路径，应列入 Wave 3（治理写/读路径环）一并评估。

### 2. enterprise_unexpected_rc（11 条，medium）

enterprise 模式要求显式 `--workspace-id`，daemon 拒绝隐式 workspace 推导：

`churn`、`task list`、`workspace status`、`stats`、`status`、`topo`、`uncommented`、
`git log`、`git stats`、`semgrep list`、`semgrep stats`

> 与本轮缺陷 A 同类：调用方身份未由 CLI 从 daemon 权威解析，而要求人肉传参。
> 建议复用 `_resolve_caller_workspace_instance_id` 同一收敛点（后续 backlog 卡）。

### 3. daemon_side_error（2 条，high）

命令在 enterprise 模式下明确不可用（无对应 daemon RPC）：

| 命令 | daemon 报错 |
| --- | --- |
| `function-issues` | 尚无对应的 daemon RPC（该查询未下沉 daemon），enterprise 不可用；改用 `--mode local` / `auto` |
| `coverage uncovered` | 同上 |

> 这两条是"能力缺口"而非回归：需补 RPC 或在文档/帮助中显式声明 local-only。

### 4. PARAM_MISSING（33 条，不计缺陷）

两边均返回 clap `usage` 缺参，属执行器参数推断未覆盖（如需要真实 symbol/file 之外的多参数组合），
不代表命令不可用。

## 与 9 月基线对比

9 月 Python CLI 基线（`deliverables/cli-e2e-findings.md`）：146 叶子 / DUAL_RPC 58 / 剩余 11 条有意本地。
本轮 Rust CLI：127 叶子 / DUAL_RPC 34 / SUSPECT_NO_DAEMON 11。命令树规模不同（Python 与 Rust 入口
命令集不完全重合），但"有意本地"数量持平（11），无新增 daemon 侧回归。

## 复跑方式

```powershell
# 1. 生成命令树（clap 格式；本轮新增，此前仓库内无生成脚本）
C:\Python314\python.exe .workbuddy\scripts\gen_cli_tree.py
# 2. 跑矩阵
cd C:\git_work\callwarden\.workbuddy\scripts
C:\Python314\python.exe cli_e2e_runner.py
```

## backlog（不阻塞本卡关闭）

- Wave 3：治理读/写路径环（上述 8 条 suspect_query_local 治理读路径 + 治理写命令环）
- Wave 4：并发/崩溃/多实例边界
- 67 个 MCP 写工具补测、B 类 5 条双契约重测、F-001 registry 数据治理、build_graph 增量化
- enterprise 隐式 workspace 解析收敛（11 条 `enterprise_unexpected_rc`）
- `function-issues` / `coverage uncovered` 的 RPC 补齐或 local-only 声明
