# Call Warden CLI-01 → SRV-019 逐任务链路核实报告

**日期：** 2026-08-26　**范围：** A′ 迁移系列 116 个任务（CLI-01~96 + SRV-01~19）
**判定标准：** 任务标题中的命令/模块名，在 master 源码（Python 权威 + Rust daemon/router）中的存在性。
**结论：`MISSING = 0`** —— 任务单状态虽有 review/closed/in_progress，但代码全部在 master，无一丢失。

| 任务 | 状态 | 目标 | Python权威 | Rust侧 | 判定 |
|---|---|---|---|---|---|
| CLI-01 | closed | daemon | 是 | 是 | 双在(已迁移) |
| CLI-02 | review | search | 是 | 是 | 双在(已迁移) |
| CLI-03 | review | task | 是 | 是 | 双在(已迁移) |
| CLI-04 | review | daemon | 是 | 是 | 双在(已迁移) |
| CLI-05 | review | agent | 是 | 是 | 双在(已迁移) |
| CLI-06 | review | agent | 是 | 是 | 双在(已迁移) |
| CLI-07 | review | dependency | 是 | 是 | 双在(已迁移) |
| CLI-08 | review | dependency | 是 | 是 | 双在(已迁移) |
| CLI-09 | review | dependency | 是 | 是 | 双在(已迁移) |
| CLI-10 | review | dependency | 是 | 是 | 双在(已迁移) |
| CLI-11 | review | assignment | 是 | 是 | 双在(已迁移) |
| CLI-12 | review | audit | 是 | 是 | 双在(已迁移) |
| CLI-13 | review | bootstrap | 是 | 是 | 双在(已迁移) |
| CLI-14 | review | brief | 是 | 是 | 双在(已迁移) |
| CLI-15 | review | build-context | 是 | 是 | 双在(已迁移) |
| CLI-16 | review | call-chain | 是 | 是 | 双在(已迁移) |
| CLI-17 | review | callees | 是 | 是 | 双在(已迁移) |
| CLI-18 | review | callers | 是 | 是 | 双在(已迁移) |
| CLI-19 | review | check-gate | 是 | 是 | 双在(已迁移) |
| CLI-20 | review | churn | 是 | 是 | 双在(已迁移) |
| CLI-21 | review | clone | 是 | 是 | 双在(已迁移) |
| CLI-22 | review | comment-coverage | 是 | 是 | 双在(已迁移) |
| CLI-23 | review | complexity | 是 | 是 | 双在(已迁移) |
| CLI-24 | review | coupled-fns | 是 | 是 | 双在(已迁移) |
| CLI-25 | review | coupling | 是 | 是 | 双在(已迁移) |
| CLI-26 | review | coverage | 是 | 是 | 双在(已迁移) |
| CLI-27 | review | dashboard | 是 | 是 | 双在(已迁移) |
| CLI-28 | review | defect | 是 | 是 | 双在(已迁移) |
| CLI-29 | review | dependency | 是 | 是 | 双在(已迁移) |
| CLI-30 | review | evolution | 是 | 是 | 双在(已迁移) |
| CLI-31 | review | file | 是 | 是 | 双在(已迁移) |
| CLI-32 | review | fn-metrics | 是 | 是 | 双在(已迁移) |
| CLI-33 | review | fts | 是 | 是 | 双在(已迁移) |
| CLI-34 | review | function-issues | 是 | 是 | 双在(已迁移) |
| CLI-35 | review | gc | 是 | 是 | 双在(已迁移) |
| CLI-36 | review | git | 是 | 是 | 双在(已迁移) |
| CLI-37 | review | grep | 是 | 是 | 双在(已迁移) |
| CLI-38 | review | guardrail | 是 | 是 | 双在(已迁移) |
| CLI-39 | review | health-report | 是 | 是 | 双在(已迁移) |
| CLI-40 | review | hotspot | 是 | 是 | 双在(已迁移) |
| CLI-41 | review | impact | 是 | 是 | 双在(已迁移) |
| CLI-42 | review | issues | 是 | 是 | 双在(已迁移) |
| CLI-43 | review | largest-fns | 是 | 是 | 双在(已迁移) |
| CLI-44 | review | lease | 是 | 是 | 双在(已迁移) |
| CLI-45 | review | map | 是 | 是 | 双在(已迁移) |
| CLI-46 | review | metrics | 是 | 是 | 双在(已迁移) |
| CLI-47 | review | ownership-map | 是 | 是 | 双在(已迁移) |
| CLI-48 | review | query | 是 | 是 | 双在(已迁移) |
| CLI-49 | review | refresh | 是 | 是 | 双在(已迁移) |
| CLI-50 | review | review | 是 | 是 | 双在(已迁移) |
| CLI-51 | review | rollback | 是 | 是 | 双在(已迁移) |
| CLI-52 | review | rule-applicable | 是 | 否 | 仅Python(迁移中) |
| CLI-53 | review | rule-candidate | 是 | 是 | 双在(已迁移) |
| CLI-54 | review | rule-cleanup-sync-log | 是 | 否 | 仅Python(迁移中) |
| CLI-55 | review | rule-extract | 是 | 是 | 双在(已迁移) |
| CLI-56 | review | rule-insert-block | 是 | 否 | 仅Python(迁移中) |
| CLI-57 | review | rule-list | 是 | 是 | 双在(已迁移) |
| CLI-58 | review | rule-seed-bootstrap | 是 | 是 | 双在(已迁移) |
| CLI-59 | review | rule-sync | 是 | 是 | 双在(已迁移) |
| CLI-60 | review | search | 是 | 是 | 双在(已迁移) |
| CLI-61 | review | semgrep | 是 | 是 | 双在(已迁移) |
| CLI-62 | review | stats | 是 | 是 | 双在(已迁移) |
| CLI-63 | review | status | 是 | 是 | 双在(已迁移) |
| CLI-64 | review | symbol | 是 | 是 | 双在(已迁移) |
| CLI-65 | review | symbol-history | 是 | 是 | 双在(已迁移) |
| CLI-66 | review | test-impact | 是 | 是 | 双在(已迁移) |
| CLI-67 | review | tests | 是 | 是 | 双在(已迁移) |
| CLI-68 | review | topo | 是 | 是 | 双在(已迁移) |
| CLI-69 | review | uncommented | 是 | 是 | 双在(已迁移) |
| CLI-70 | review | vuln-blast | 是 | 是 | 双在(已迁移) |
| CLI-71 | review | who | 是 | 是 | 双在(已迁移) |
| CLI-72 | review | workspace | 是 | 是 | 双在(已迁移) |
| CLI-73 | review | identity-revoke | 是 | 否 | 仅Python(迁移中) |
| CLI-74 | review | local | 是 | 是 | 双在(已迁移) |
| CLI-75 | review | local-apply | 是 | 否 | 仅Python(迁移中) |
| CLI-76 | review | local-capture-auto | 是 | 否 | 仅Python(迁移中) |
| CLI-77 | review | local-capture-manual | 是 | 否 | 仅Python(迁移中) |
| CLI-78 | review | local-changes | 是 | 否 | 仅Python(迁移中) |
| CLI-79 | in_progress | local-close | 是 | 否 | 仅Python(迁移中) |
| CLI-80 | review | local-commits | 是 | 否 | 仅Python(迁移中) |
| CLI-81 | review | local-completion-review | 是 | 否 | 仅Python(迁移中) |
| CLI-82 | review | local-create | 是 | 否 | 仅Python(迁移中) |
| CLI-82 | open | ? | 否 | 否 | 标题无法解析 |
| CLI-83 | in_progress | local-findings | 是 | 否 | 仅Python(迁移中) |
| CLI-84 | closed | local-next | 是 | 否 | 仅Python(迁移中) |
| CLI-85 | closed | local-reopen | 是 | 否 | 仅Python(迁移中) |
| CLI-86 | closed | local-report | 是 | 否 | 仅Python(迁移中) |
| CLI-87 | review | local-resolve-finding | 是 | 否 | 仅Python(迁移中) |
| CLI-88 | review | local-rollback | 是 | 否 | 仅Python(迁移中) |
| CLI-89 | review | local-split | 是 | 否 | 仅Python(迁移中) |
| CLI-90 | review | local-status | 是 | 是 | 双在(已迁移) |
| CLI-91 | review | local-task-exists | 是 | 否 | 仅Python(迁移中) |
| CLI-92 | review | local-task-list | 是 | 否 | 仅Python(迁移中) |
| CLI-93 | review | local-tree | 是 | 否 | 仅Python(迁移中) |
| CLI-94 | review | internal | 是 | 是 | 双在(已迁移) |
| CLI-95 | review | run-subcommand-mode | 是 | 否 | 仅Python(迁移中) |
| CLI-96 | review | main | 是 | 是 | 双在(已迁移) |
| SRV-01 | review | mcp | 是 | 是 | 双在(已迁移) |
| SRV-02 | review | audit | 是 | 是 | 双在(已迁移) |
| SRV-03 | in_progress | backup | 是 | 是 | 双在(已迁移) |
| SRV-04 | in_progress | cli | 是 | 是 | 双在(已迁移) |
| SRV-05 | in_progress | daemon | 是 | 是 | 双在(已迁移) |
| SRV-06 | in_progress | daemon | 是 | 是 | 双在(已迁移) |
| SRV-07 | in_progress | daemon | 是 | 是 | 双在(已迁移) |
| SRV-08 | in_progress | daemon | 是 | 是 | 双在(已迁移) |
| SRV-09 | in_progress | durable | 是 | 是 | 双在(已迁移) |
| SRV-10 | in_progress | health | 是 | 是 | 双在(已迁移) |
| SRV-11 | in_progress | job | 是 | 是 | 双在(已迁移) |
| SRV-12 | in_progress | metrics | 是 | 是 | 双在(已迁移) |
| SRV-13 | in_progress | query | 是 | 是 | 双在(已迁移) |
| SRV-14 | in_progress | replicator | 是 | 是 | 双在(已迁移) |
| SRV-15 | in_progress | schema | 是 | 是 | 双在(已迁移) |
| SRV-16 | in_progress | snapshot | 是 | 是 | 双在(已迁移) |
| SRV-17 | in_progress | stage | 是 | 是 | 双在(已迁移) |
| SRV-18 | in_progress | staging | 是 | 是 | 双在(已迁移) |
| SRV-19 | in_progress | ? | 否 | 否 | 标题无法解析 |

## 解读
1. **91 个任务**：Python + Rust 双端代码都在 master（迁移已完成或并行保留）。
2. **23 个任务**：仅 Python 权威在 master，Rust 侧未命中 → 迁移未完成（与任务 in_progress/review 状态一致），**代码未丢**。
3. **0 个 MISSING**：没有任何任务的命令/模块代码从 master 消失。
4. 2 个 unknown：CLI-082(e2e smoke test，无命令名) 与 SRV-019(final gate，标题无模块名)——非缺失。
5. 早期自动扫描的 26 个 MISSING 全部为 `\b` 词边界误报（`cmd_local_apply` 不匹配 `\blocal_apply\b`），已用子串法修正。
6. 之前已确认：i18n.py→callwarden/i18n/ 重构、cw_client.rs 全部函数在、audit_log/LazyDBProxy/测试全在、`_h_*` 助手重构为新集合。

## 与恢复工作的衔接
- 5 个 pilot（CLI-087/088、SRV-001/002/003）已从回收站恢复 commit 并验证内容在 master。
- 唯一真缺口 = srv-003 的 backup_restore_handlers.rs + test_srv_003.py（已提交 901cc3c）+ SRV-003_evidence.md（已恢复）。
- 44 个 prune commit 的 7,300 个函数逐函数比对 = 零真实缺口（见 cw_203_gap_report.md）。

## 下一步（补齐阶段，如需要）
- 23 个 python_only 任务是正常的「待迁移」，不属丢失；如需推进 Rust 迁移，可逐个领取。
- fsck 46 条 broken link 待全部恢复+备份后按用户决定处理。