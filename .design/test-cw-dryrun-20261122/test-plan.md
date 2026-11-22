# test-plan · backlog-46（test-cw 试跑重建）

> G-Test 工件形态：金字塔分层 + AC 映射 + 缺陷分级。本文件同时记录**三源反查的实测量级**，
> 因为量级直接决定批次能否成立。

## 1. 分层

| 层 | 内容 | 本轮是否覆盖 |
|---|---|---|
| 单元 | `cli/main.py` 各 `_handle_*` 直接调用（fake DB，不触 daemon） | ✅ `test_report_rc_semantics.py`（19 例） |
| 契约/门禁 | i18n 双语键 + 占位符、cli_reference 文档漂移 | ✅ 122 + 8 例（强制并入） |
| 集成/E2E | 真实 daemon + workspace 的活体命令（spec §3 活体 1/2） | ❌ manual（`AC-07`） |

## 2. AC → nodeid

| AC | nodeid（`tests/test_report_rc_semantics.py::…`） |
|---|---|
| AC-01 | `test_issues_findings_with_flag_exit3` |
| AC-02 | `test_issues_findings_without_flag_rc0`、`test_issues_empty_with_flag_rc0` |
| AC-03 | `test_semgrep_scan_incremental_failure_exit1`、`test_semgrep_scan_save_failure_exit1`、`test_semgrep_scan_default_failure_exit1` |
| AC-04 | `test_detect_cycles_findings_with_flag_exit3`、`test_orphan_symbols_findings_with_flag_exit3`、`test_function_issues_fn_mode_with_flag_exit3`、`test_function_issues_list_mode_with_flag_exit3`、`test_semgrep_scan_default_findings_with_flag_exit3`、`test_clone_detect_findings_with_flag_exit3` |
| AC-05 | `tests/test_i18n_key_existence.py`（全量）+ `tests/test_i18n_placeholder_consistency.py`（全量） |
| AC-06 | `tests/test_cli_reference_sections.py`（全量） |
| AC-07 | —（manual） |

## 3. 三源反查实测（关键：量级决定批次）

| 源 | 检索式 | 结果 | 结论 |
|---|---|---|---|
| A（AC→用例） | spec §2.5 登记文件 | `test_report_rc_semantics.py` 19 例 | 有效，但**要求 spec 阶段就登记文件名** |
| B（符号→用例）· 符号名 | `_exit_on_findings` | 1 文件 | 命中，但仅覆盖直接单测该 helper 的场景 |
| B · 符号名（snake） | `fail_on_findings` | **0 命中** | ❌ 只按符号名反查会空手而归 |
| B · CLI 字面量（kebab） | `--fail-on-findings` | 1 文件 | ✅ 真实入口在 argv 字符串里 |
| C（改动文件→闭包） | `callwarden.cli`（注意包名） | **140 文件 / 1307 nodeid** | 远超 800/批上限 → 只能作候选池，必须用 A/B 裁剪 |
| 强制并入 | 改动含 `i18n/*.json`、`docs/cli_reference.md` | 122 + 8 例 | 必须并入，与 AC 映射无关 |

## 4. 批次

| 批 | 内容 | 用例数 | 上限校验 |
|---|---|---|---|
| 1 | 上述全部（149 例） | 149 | < 800 ✅，实测耗时 < 2 min ✅ |

## 5. 缺陷分级（若红）

| 级别 | 判据 | 例子 |
|---|---|---|
| P1 | 安全 / 数据 / 门禁契约破裂；AC 直接 FAIL | `--fail-on-findings` 未退出 3；semgrep 失败仍 rc=0 |
| P2 | 行为可用但口径不一致 | 提示文案缺 count；退出码正确但无 message |
| P3 | 文档 / 命名 / 可读性 | cli_reference 章节缺退出码行 |
