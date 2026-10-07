# T10 阶段2 核对结论：subcommand 等价缺口

> 日期：2026-10-07
> 方法：静态核对 _DEPRECATED_FLAG_MAPPING 第 3 列顶层词 ∈ _SUBCOMMANDS（60/60 通过），
> 再**实测每个 subcommand 的 `--help`** 核验声称的子选项是否真实存在。
> **重大发现**：映射表第 3 列有一部分 subcommand 子选项**从未实现**——这些 flag 删掉会丢失功能。

## 1. 核对方法与为何静态核对不够

静态核对显示 60/60 flag 的 subcommand 顶层词都在 `_SUBCOMMANDS`，但这只证明"顶层命令存在"。
实测 `--help` 后发现：映射表写的"组合子选项"形式（如 `call-chain --deepest`、`search --embed`、
`file changes`）里，**相当一部分子选项/子命令实际没实现**。即映射表第 3 列是"设计意图"，
部分未兑现。

## 2. 可安全移除（subcommand 等价真实存在，~38 个）

这些 flag 映射到独立顶层 subcommand，实测功能对应，阶段 3 可直接删：

`--list-workspaces`/`--register-workspace`/`--set-workspace`/`--delete-workspace`（→workspace *）、
`--refresh-all`(→refresh --all)/`--refresh`(→refresh <path>)/`--watch`(→refresh --watch)、
`--stats`/`--status`、`--query`/`--search`/`--symbol`、`--callers`/`--callees`、
`--call-chain`(→call-chain，不含声称的子选项)、`--impact`/`--topo`、
`--metrics`/`--complexity`/`--coupling`/`--largest-fns`/`--coupled-fns`/`--fn-metrics`、
`--comment-coverage`/`--uncommented`、`--history`(→symbol-history)、
`--git-import`/`--git-log`/`--git-show`/`--git-stats`（→git *）、
`--semgrep`/`--semgrep-list`/`--semgrep-stats`（→semgrep *）、
`--function-issues`(→function-issues，不含 --summary)、
`--coverage-import`/`--coverage-fn`/`--coverage-uncovered`（→coverage *）、
`--who`/`--ownership-map`/`--brief`/`--map`。

## 3. 不可直接移除（subcommand 等价缺失，~18 个）

实测这些 flag 声称的 subcommand 子选项/子命令**不存在**，删除会丢失功能。
阶段 3 **跳过**这些，归入阶段 2.5「补 subcommand」。

| flag | 映射表声称 | 实测 subcommand 真实情况 | 处置 |
|------|-----------|------------------------|------|
| `--deepest` | `call-chain --deepest N` | call-chain 只有 `--depth` | 补 call-chain 子选项或建 `deepest` 命令 |
| `--module-calls` | `call-chain --module-calls N` | 同上，不存在 | 同上 |
| `--detect-cycles` | `call-chain --detect-cycles` | 同上，不存在 | 同上（注意 MCP 侧已是 detect_call_cycles） |
| `--export-module-graph` | `call-chain --export-module-graph` | 同上，不存在 | 同上 |
| `--call-heatmap` | `call-chain --heatmap` | 同上，不存在 | 同上 |
| `--top-callers` | `callers --top N` | callers 只有 `--qualified` | 补 callers 子选项 |
| `--orphan-symbols` | `callers --orphans` | 同上，不存在 | 同上 |
| `--embed` | `search --embed` | search 只有 `--kind/--limit` | 补 search 子选项或建 embed 命令 |
| `--embed-force` | `search --embed --force` | 同上，不存在 | 同上 |
| `--semantic-search` | `search --semantic` | 同上，不存在 | 同上 |
| `--similar` | `search --similar` | 同上，不存在 | 同上 |
| `--changes` | `file changes [SINCE]` | file 只接受 path（列符号） | 补 file 子命令或建 changes 命令 |
| `--diff` | `file diff <H1> <H2>` | 同上，不存在 | 同上 |
| `--restore-comment` | `file restore-comment` | 同上，不存在 | 同上 |
| `--restore-all-comments` | `file restore-all-comments` | 同上，不存在 | 同上 |
| `--restore-file` | `file restore-file` | 同上，不存在 | 同上 |
| `--test-coverage` | `coverage --test` | coverage 有 import/fn/uncovered，无 --test | 评估 `cw tests` 是否覆盖，或补 |
| `--issue-summary` | `function-issues --summary` | function-issues 无 --summary | 补子选项 |

## 4. T10 清单修正

- `--symbol-content-by-hash`、`--git-blame` **不在** `_DEPRECATED_FLAG_MAPPING`（T10 清单误列）——
  它们是 MCP 工具名，CLI 侧 symbol_content_by_hash 只在 `--show-content`/`--diff` 内部调用。不属本次移除范围。
- 实际 deprecated flag 共 60 个（不是 48）。

## 5. 对执行计划的影响

- **阶段 3 范围缩小**：只移除 §2 的 ~38 个"等价真实存在"的 flag。
- **新增阶段 2.5**：§3 的 ~18 个 flag 的功能需要先在 subcommand 侧补齐子选项/子命令，才能移除对应 flag。
  这是一批**真实的功能补全工作**（不是纯删除），工作量和风险更高，需要你单独决策是否纳入本轮。
- 在 §3 的 subcommand 补齐前，这 18 个 flag **必须保留**（否则功能丢失）。

## 6. 建议

1. **阶段 3 先删 §2 的 ~38 个安全 flag**（功能有等价 subcommand，零丢失）。
2. **§3 的 18 个 flag 暂留**，单独立「补 subcommand 子选项」任务，补齐后再删对应 flag。
   这避免"为了删 flag 而丢功能"。
3. 若你希望一次清理到底，§3 的补全可以做，但那是新增功能开发（给 call-chain/callers/search/file/
   coverage/function-issues 补子选项），需明确授权。
