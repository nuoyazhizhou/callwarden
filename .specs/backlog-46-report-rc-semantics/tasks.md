# Tasks · backlog §46 报告型命令 rc 新语义

> Spec：`.specs/backlog-46-report-rc-semantics/spec.md`
> 按序执行；每步完成后勾选。

## T1 cli/main.py 实施

- [x] 1.1 新增模块级 helper `_exit_on_findings(opts, count)`（spec §2.1 原文）。
- [x] 1.2 六个 parser 加 `--fail-on-findings`（issues / function-issues /
      detect-cycles / orphan-symbols / semgrep scan_p / clone detect_p）。
- [x] 1.3 六处 return True 前接入 `_exit_on_findings`（口径见 spec §2.1 表）。
- [x] 1.4 semgrep scan 四个 `success=False` 分支改为打印 error 后
      `raise SystemExit(1)`（spec §2.2）。

## T2 i18n catalog

- [x] 2.1 `i18n/zh_CN.json` + `i18n/en_US.json` 双语加
      `cli.args.fail_on_findings` 与 `cli.messages.fail_on_findings_triggered`
      （文案见 spec §2.3；占位符 `{count}` 两侧一致）。

## T3 文档

- [x] 3.1 `docs/cli_reference.md` 全局退出码契约表加 3 行 + 判定边界补 opt-in 说明。
- [x] 3.2 六个命令章节四段式：参数表加 flag 行、退出码表加 3 行；
      semgrep 章节退出码表 1 行含义补「scan 失败」。

## T4 测试

- [x] 4.1 先读现有 exit-code 套件 harness（tests/test_cli_exit_code_local_input.py 等）。
- [x] 4.2 新增 `tests/test_report_rc_semantics.py`（spec §2.5 用例清单）。

## T5 验证与收尾

- [x] 5.1 回归：spec §3 全表（含 i18n 两个门禁 + test_cli_reference_sections + CLI 回归套件）。
- [x] 5.2 活体验证：带/不带 flag 各一例（rc=3 / rc=0）。
- [ ] 5.3 ledger 回填 §9.57、backlog 回填 §46（含 §45.4 挂账清零标注）。
- [ ] 5.4 账本重生：`python docs/testing/gen_test_cases.py --revision <40位HEAD>`。
- [ ] 5.5 commit + push origin master。
