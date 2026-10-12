# TP-08F 清单/义务合并冻结合同（冻结版 r1）

阶段 A1 · owner: inventory-freeze · 日期 2026-10-12 · 前置：TP-08C、TP-08M、TP-01、TP-02（均已 closed）

本文冻结 TP-08F 的合并规则、selector 与正负向义务。结果：**12 passed**（Windows + Py3.14 profile）。

## 1. 输入（真实提取产物，均已冻结并 hash 绑定）

| 输入 | 内容 | sha256 绑定 |
|---|---|---|
| `docs/testing/manifests/cli/cli_tree.json` | TP-08C：84 顶层 + 144 嵌套，parser 级参数 | `version_binding.cli_tree_sha256` |
| `docs/testing/manifests/mcp/mcp_tools.json` | TP-08M：243 工具，wire 级 schema | `version_binding.mcp_tools_sha256` |
| `docs/testing/planning/inventory/candidate_entries.json` | TP-01 静态候选集（对账基线） | `version_binding.tp01_candidates_sha256` |

## 2. 合并规则（CLI 入口展开为调用叶）

| 规则 | 处理 |
|---|---|
| subparser 父 → 子命令 | 逐子展开为独立入口（`task create` 等 144 条） |
| 第 3 层（`daemon`） | cw.py 层入口不经 `_dispatch_subcommand`，经**真实子进程** `cw.py daemon <sub> --help` 提取 23 条 |
| 位置参数 choices | `install-agent <agent>`（24 个 agent）、`install-hook <hook>`（post-commit）逐 choice 展开为独立入口，并保留不带 choice 的父入口 |
| 旗标 choices | `map --format text\|mermaid` **不**展开为独立入口，落为 C2 参数等价类（`param_equivalence_classes`） |
| MCP 入口 | 243 wire 工具，逐工具带 inputSchema（type/required/properties） |

## 3. 冻结规模

| 项 | 值 |
|---|---|
| CLI 入口 | **265**（199 dispatch 叶 + 5 cw.py standalone + 23 daemon 第3层 + 38 位置参数 choice 展开/父） |
| MCP 入口 | **243** |
| **合计** | **508** |
| profile | `P-COMMITTED-windows-amd64-py314`（唯一冻结 profile） |
| 义务模型 | C1–C6 全入口绑定（`applicable_pending_case`，逐 case 合同留 TP-10C/M） |

## 4. 对账结果（差异消除，不允许静默差异）

| 维度 | 静态（TP-01） | 真实（冻结） | 对账 |
|---|---|---|---|
| MCP 集合 | 243 | 243 | **static_only=0、real_new=0，集合完全相等** → GAP-14 集合级闭合 |
| CLI 集合 | 234 | 265 | static_only=**0**（49 条静态多出全部由第3层提取 + 位置参数展开覆盖）；real_new=**33**；reclassified=**2**（`map text`/`map mermaid` 归为 `--format` 旗标等价类） |

real_new 33 条示例：`call-heatmap`、`changes`、`deepest`、`detect-cycles`、`diff`、`embed`、
`export-module-graph`、`module-calls`、`orphan-symbols`、`restore-comment`、
`restore-all-comments`、`semantic-search`、`similar`、`top-callers`、`coverage test`、
`daemon`、`install-agent`、`install-hook`、`map` 等——TP-01 静态 AST 快照漏登或其后新增，
现由真实树登记。

## 5. GAP-14 闭合证据

- wire 实测集合**含** `detect_call_cycles`、`detect_dependency_cycle`；
- **不含**陈旧名 `detect_cycle`、`detect_cycles`；
- wire 工具数 243 == 源码 12 个 `tools_*.py` 的 `@mcp.tool()` 装饰器总数 243；
- 结论：总数相等不再掩盖集合漂移，`MCP:detect_call_cycles` /
  `MCP:detect_dependency_cycle` 的 `blocked_schema_drift` 状态解除。

## 6. 冻结 selector 与命令

```
合并: C:/Python314/python.exe docs/testing/manifests/frozen/merge_inventory.py
      cwd=C:/git_work/callwarden；rc=0；输出 inventory_frozen.json
校验: C:/Python314/python.exe docs/testing/manifests/frozen/validate_frozen.py --self-check
      C:/Python314/python.exe docs/testing/manifests/frozen/validate_frozen.py --verify-freeze
      两者 rc=0
验收: C:/Python314/python.exe -m pytest docs/testing/manifests/frozen/test_frozen_inventory.py \
        -p no:cacheprovider --noconftest -v --junitxml=<run根>/reports/junit.xml
      cwd=C:/git_work/callwarden；expected rc=0，12 passed
```

## 7. 正向义务

| 义务 | 测试 |
|---|---|
| 验证器 self-check / verify-freeze 通过 | `test_validator_self_check_passes` / `test_validator_verify_freeze_passes` |
| 508 入口计数与唯一 profile | `test_counts_and_profiles` |
| GAP-14 证据闭合 | `test_gap14_closed_with_evidence` |
| 对账完备（静态多出=0、MCP 集合相等、重分类显式） | `test_reconciliation_complete` |
| 每入口 C1–C6 + profile + provenance + status 全绑定 | `test_every_entry_has_full_obligation_binding` |
| entry_id 无重复 | `test_no_duplicate_entry_ids` |
| 展开依据可查（choice 展开 / daemon 第3层 23 / MCP 243） | `test_entry_kinds_cover_expansion_basis` |

## 8. 负向义务（未审漂移阻断）

| 场景 | 测试 / 期望 |
|---|---|
| **MCP wire 未审漂移** | `test_unreviewed_wire_drift_blocks`：**真实重提取** wire 工具名集与冻结集逐一比对，任何增删即失败 |
| 源码装饰器数漂移 | `test_source_decorator_count_matches_frozen`：源码 `@mcp.tool()` 总数必须 == 冻结 MCP 数 == 243 |
| 篡改 manifest | `test_tampered_manifest_is_rejected`：删入口 / 破坏 C1–C6 覆盖 / 散列漂移三种篡改均须被验证器 fail-closed 拒绝（反证，防验证器空转） |
| 行尾/编码漂移 | `test_manifest_is_lf_and_utf8` |

## 9. 边界

- 本卡冻结「入口 + 参数 + 义务适用性 + profile」，**不**产生运行证据；逐 case 的 C1–C6 合同与运行结果由 TP-10C/M 建立。
- 跨端分类合并：wire annotations 未暴露工具分类（本 MCP 版本为 null），MCP 侧 `category`
  暂为 null；`server/tools/_categories.py` 的 17 类与 CLI 21 类的同构映射作为 TP-10C/M
  C6 跨端语义的输入，不在此卡伪造绑定。
- 未创建正式 daemon 任务（§7 入库门禁未过），不伪造 task_id。
- 与旧 `tests/convergence` 隔离：`--noconftest`；未改任何旧 fixture 与 `gen_test_cases`
  消费链（TASK_BREAKDOWN 要求必要适配另冻独立路径）。
