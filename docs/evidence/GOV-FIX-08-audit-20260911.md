# GOV-FIX-08 巡检核对表：cli/main.py 全部 `"error" in result` 分支

- 任务卡：T-1789113280915-c3e851b0
- 巡检时间：2026-09-11（GMT+8）
- 背景：GOV-FIX-07（T-1789110285614-5e09ba98）reviewer ADV-1 finding。HTTP 传输下 daemon error 信封由 `_dispatch_subcommand` 的 `except DaemonRemoteError → RC=2` 统一兜住；socket/named-pipe 传输下各分支拿到 `{"error": ...}` dict 或 `None`（no response）时走分支内路径，此前 `return True` → RC=0 假成功。

## 逐项核对表

| # | 命令分支 | 位置（修后行号） | 失败路径 | 修复 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | apply | L5283 | error dict | sys.exit(2) | 本卡修复 |
| 2 | close | L5346 | error dict | sys.exit(2) | 本卡修复 |
| 3 | cascade-close | L5382 | error dict | sys.exit(2) | GOV-FIX-06 已修（核验通过） |
| 4 | reopen | L5465 | error dict | sys.exit(2) | 本卡修复 |
| 5 | claim-recover | L5542 | error dict | sys.exit(2) | 本卡修复 |
| 6 | completion-review | L5981 | error dict | sys.exit(2) | 本卡修复 |
| 7 | supersede | L6138/L6142 | no response + error dict | sys.exit(2) ×2 | 本卡修复 |
| 8 | attest-legacy-workspace-binding | L6205/L6211 | no response + error dict | sys.exit(2) ×2 | GOV-FIX-07 已修（核验通过） |
| 9 | contract-bootstrap | L6269/L6276 | no response + error dict | sys.exit(2) ×2 | 本卡修复 |
| 10 | contract-revise | L6332/L6339 | no response + error dict | sys.exit(2) ×2 | 本卡修复 |
| 11 | bootstrap-executor-evidence | L6382/L6389 | no response + error dict | sys.exit(2) ×2 | 本卡修复 |
| 12 | bootstrap-reviewer-pass | L6425/L6432 | no response + error dict | sys.exit(2) ×2 | 本卡修复 |
| 13 | governance-projection | L6460 | error dict（读路径） | sys.exit(2) | 本卡修复 |
| 14 | superseded | L6498/L6515 | no response + error dict | sys.exit(2) ×2 | 本卡修复 |

脚本化终检：14/14 分支均含 `sys.exit(2)`，0 遗漏；`ast.parse` 语法通过。

## 成功路径语义保护

所有修改仅在失败分支内追加 `sys.exit(2)`，成功分支输出/返回值不变。成功路径以单测锁定：

- `test_supersede_success_path_rc0`：mock 成功响应 → `return True`（RC=0），输出含新旧 task_id
- GOV-FIX-07 的 `test_local_exception_keeps_legacy_semantics`：本地异常维持原语义

## 回归测试

新增 `tests/test_cli_govfix08_error_dict_rc.py`（10/10 通过）：

| 用例 | 覆盖 |
| --- | --- |
| apply / close / claim-recover / completion-review error dict → RC=2 | 写分支 error dict |
| supersede error dict / no response → RC=2 | 写分支双失败路径 |
| governance-projection / superseded / contract-bootstrap error dict → RC=2 | 读路径 + governance mutation |
| supersede 成功路径 RC=0 | 成功语义不破坏 |

既有测试：test_cli_govfix07_daemon_error_rc.py（4 绿）+ test_cli_main_help.py（27 绿）+ test_phase5_2_slice7_routing.py（1 绿）= 32 passed。

## live 验收

| # | 场景 | 结果 |
| --- | --- | --- |
| 1 | HTTP 路径 attest 拒绝（req-govfix08-live-2，无管道直取退出码） | `E_LEGACY_BIND_TASK_NOT_FOUND` + **REAL_RC=2** ✓（注：首测经管道取 `$?` 得 tail 的 RC=0 为测量假象，已排除） |
| 2 | socket 传输 error dict 路径 | 由单测 mock 覆盖（10/10） |

## 结论

cli/main.py 全部 14 处 `"error" in result` 分支在 socket 传输下的失败路径均以 RC=2 fail-closed，与 dispatch 层 HTTP 路径语义对齐；成功路径与本地异常语义不变。CLI 层 RC=0 假成功缺陷族（GOV-FIX-06/07/08）至此全谱系闭合。
