# Spec · backlog §46 报告型命令 rc 新语义（opt-in --fail-on-findings + 退出码 3）

> 章节：§46（承接 §45.4 挂账「报告型命令 rc 新语义，涉及 CI 消费方契约」）
> 用户裁决：方案 = opt-in flag + rc=3；范围 = 仅门禁型命令 6 个
> 日期：2026-11-21

## 1. 背景与裁决

§45.4 登记：报告型命令（issues/tests 等）即使报告 findings 也 `return True` → rc=0，
CI 消费方无法据退出码建门禁。经裁决采用：

- **方案**：opt-in flag `--fail-on-findings`，默认 rc=0 不变（零 CI 破坏）；
  flag 开启且报告了 findings → `raise SystemExit(3)`。
- **新退出码**：`3 = 报告发现失败`（区别于 1=daemon/运行时失败、2=本地输入校验失败）。
- **范围（仅 6 个门禁型命令）**：`issues`、`function-issues`、`detect-cycles`、
  `orphan-symbols`、`semgrep scan`、`clone detect`。咨询型（tests/coverage/uncommented/
  doctor/graph 等）不加 flag。

## 2. 变更清单

### 2.1 cli/main.py

新增 helper（置于 `_handle_issues` 前附近，模块级）：

```python
def _exit_on_findings(opts, count: int) -> None:
    """§46：--fail-on-findings 门禁语义——报告了 findings 时以退出码 3 终止。"""
    if getattr(opts, "fail_on_findings", False) and count > 0:
        print(t("cli.messages.fail_on_findings_triggered", count=count))
        raise SystemExit(3)
```

6 个 parser 各加 `parser.add_argument("--fail-on-findings", action="store_true",
help=t("cli.args.fail_on_findings", default="Exit with code 3 when findings are reported (CI gate)"))`，
并在对应 return True 前调用 `_exit_on_findings(opts, <findings_count>)`：

| Handler | 位置 | findings 计数口径 |
| --- | --- | --- |
| `_handle_issues`（L9907） | 末尾 `return True`（L9988）前 | `len(issues)` |
| `_handle_detect_cycles`（L10862） | 末尾 `return True`（L10897）前 | `len(cycles)` |
| `_handle_orphan_symbols`（L10770） | 末尾 `return True`（L10804）前 | `len(results)` |
| `_handle_function_issues`（L11524） | fn 模式与 list 模式 `return True`（L11604）前 | fn 模式 = `results[0]["issue_count"]`（results 非空）；list 模式 = `len(results)`；`--summary` 模式不加（见 §4 非目标） |
| `_handle_semgrep` scan 动作（L12060） | scan 分支末尾（L12245）`return True` 前 | 4 个成功分支的 `result['total_findings']`（incremental/save/quick/default）；失败分支见 2.2 |
| `_handle_clone` detect 动作（L7281） | detect 分支 `return True`（L7387）前 | `result.get("total_pairs", 0)` |

注：`issues` 空态分支（L9929-9932）与 `function-issues` 无结果分支 findings=0，
`_exit_on_findings` 自然不触发，无需改空态分支。

### 2.2 连带收口：semgrep scan 真失败静默 rc=0

`_handle_semgrep` scan 动作 4 个分支（incremental L12133-12135 / save L12150-12152 /
quick L12162-12164 / default L12204-12206）在 `result.get("success")` 为 False 时
打印 `semgrep_error` 后落到 L12243 `return True`（rc=0）。按 §43-45 口径
「真失败 → rc=1」：每个失败分支打印 error 后改为 `raise SystemExit(1)`。

### 2.3 i18n catalog（i18n/zh_CN.json + i18n/en_US.json，双语同步）

- `cli.args.fail_on_findings`：
  - zh：`有 findings 时以退出码 3 终止（CI 门禁）`
  - en：`Exit with code 3 when findings are reported (CI gate)`
- `cli.messages.fail_on_findings_triggered`（含 `{count}` 占位符，两侧一致）：
  - zh：`--fail-on-findings：报告了 {count} 条 findings，以退出码 3 终止`
  - en：`--fail-on-findings: {count} finding(s) reported, exiting with code 3`

受门禁约束：`tests/test_i18n_key_existence.py`（静态引用必须双语存在）、
`tests/test_i18n_placeholder_consistency.py`（占位符两侧一致）。

### 2.4 docs/cli_reference.md

1. **全局退出码契约表**（L9-13）加一行：
   `| 3 | 报告发现失败：门禁型命令（issues / function-issues / detect-cycles / orphan-symbols / semgrep scan / clone detect）开启 --fail-on-findings 且报告了 findings |`
   「判定边界」段补一句：退出码 3 仅由 --fail-on-findings opt-in 触发，默认行为仍为 rc=0。
2. **6 个命令章节**（四段式）：「参数」表加 `--fail-on-findings` 行；
   「退出码」表加 `| 3 | ... |` 行（semgrep/clone 章节注意退出码表须仍覆盖 0/1/2，
   semgrep scan 失败收 rc=1 需在退出码表 1 行含义中体现）。

### 2.5 测试

新增 `tests/test_report_rc_semantics.py`（沿用现有 exit-code 套件的 db fixture 模式，
执行时先读 `tests/test_cli_exit_code_local_input.py` 等现有写法再定 harness）：

- `issues` 有 findings + flag → SystemExit 3；无 flag → rc=0；无 findings + flag → rc=0。
- `detect-cycles` / `orphan-symbols` / `function-issues`（fn 模式）同口径各 1 例。
- `semgrep scan` 失败分支 → SystemExit 1（至少 incremental 或 save 一个分支的负例）。
- `clone detect` 用 mock/最小 db 覆盖 1 例（如成本高可仅做 parser 层 flag 存在性断言 +
  helper `_exit_on_findings` 单测）。

## 3. 验证

| 项 | 命令 | 期望 |
| --- | --- | --- |
| 活体 1 | `cw detect-cycles`（当前库若有环）不带 flag | rc=0 |
| 活体 2 | `cw issues <符号>` 有/无 findings 不带 flag | rc=0 |
| 回归 | `pytest tests/test_report_rc_semantics.py tests/test_cli_reference_sections.py tests/test_i18n_key_existence.py tests/test_i18n_placeholder_consistency.py` + §45 用过的 CLI 回归套件 | 全 passed |
| 门禁 | `python scripts/check_e2e_matrix_closure.py` | exit 0（若涉及矩阵行） |

## 4. 非目标

- 不改任何命令默认退出行为（无 flag 时 rc 与现状完全一致）。
- 咨询型命令（tests/coverage fn/uncovered/uncommented/doctor/graph/gc audit-list 等）不加 flag。
- `function-issues --summary` 模式本轮不加 flag 语义（汇总视图门禁口径另立批次）。
- `semgrep quick` 分支的 `result["errors"]` 告警计数保持 rc=0。
- MCP schema / daemon 不动：flag 为 CLI-only；`issues`/`detect-cycles` 等对应 MCP 工具契约不变。
- 文档 en 表述打磨、退出码语义之外的文案改动。

## 5. 回滚

revert 本次提交即可；无 schema/数据迁移。
