# ac-map · backlog-46 报告型命令 rc 新语义（test-cw 试跑重建）

> 本文件是 **test-cw 试跑时从 `.specs/backlog-46-report-rc-semantics/spec.md` 反推重建**的 ac-map。
> 原 feature 交付于 `f8b8588`，当时没有 G-Test 工件；本文件用于验证「test-cw 的输入契约是否够用」，
> 不代表该 feature 当时走过 requirements-cw 流程。
>
> 追溯主线：REQ → AC → 验收方式 → 证据位置。

| REQ | 需求（源自 spec 章节） | MoSCoW | AC | 验收方式 | 证据位置 |
|---|---|---|---|---|---|
| REQ-46.1 | 6 个门禁型命令新增 opt-in `--fail-on-findings`，开启且报告 findings → rc=3；默认行为不变（§1、§2.1） | must | AC-01 · `issues` flag→3 | 自动化：`test_report_rc_semantics.py::test_issues_findings_with_flag_exit3` | `evidence/AC-01.md` |
| REQ-46.1 | 同上（默认零破坏） | must | AC-02 · 无 flag / 无 findings → 0 | 自动化：同文件 2 例 | `evidence/AC-02.md` |
| REQ-46.1 | 同上（其余 5 命令接线） | must | AC-04 · 6 条接线路径 | 自动化：同文件 6 例 | `evidence/AC-04.md` |
| REQ-46.1 | 同上（活体验证） | must | AC-07 · 活体 rc=0 | **manual**（spec §3 执行前预标） | `evidence/AC-07.md`（N/A，待人工补） |
| REQ-46.2 | semgrep scan 各分支 `success=false` → rc=1，不再静默 rc=0（§2.2） | must | AC-03 · 三失败分支 rc=1 | 自动化：同文件 3 例（负向） | `evidence/AC-03.md` |
| REQ-46.3 | i18n 双语键 `cli.args.fail_on_findings` / `cli.messages.fail_on_findings_triggered`，占位符两侧一致（§2.3） | must | AC-05 · i18n 门禁 | 强制并入：`test_i18n_key_existence.py` + `test_i18n_placeholder_consistency.py` | `evidence/AC-05.md` |
| REQ-46.4 | `cli_reference.md` 退出码契约加 3，6 命令章节四段式（§2.4） | must | AC-06 · 文档漂移门禁 | 强制并入：`test_cli_reference_sections.py` | `evidence/AC-06.md` |

## 非目标（不建 AC）

spec §4 明确列出：不改默认退出行为（已由 AC-02 覆盖其为「必须保持」）、咨询型命令不加 flag、`function-issues --summary` 不加、semgrep quick 的 `errors` 计数仍 rc=0、MCP schema 不动。**这些不验收、不建单**——避免把"明确不做"折算成待办。

## 本轮结论

7 条 AC：6 条自动化 PASS（`evidence/AC-01…06.md`），1 条 `N/A(manual)`（`evidence/AC-07.md`）→ **需求未关闭**（AC-07 未绿）。
这与 design §1 的口径一致：N/A 不等于通过。
