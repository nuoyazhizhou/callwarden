# GOV-FIX-07 live 验收：daemon 业务拒绝 CLI 层 RC 修复

- 任务卡：T-1789110285614-5e09ba98
- 验收时间：2026-09-11（GMT+8）
- 运行环境：daemon 127.0.0.1:1615（pid 29028，HTTP dev_loopback）、CLI = 主仓源码 `cw.py`（Python 3.13.12 managed）

## 1. 缺陷复现（修复前）

命令：

```
cw task attest-legacy-workspace-binding T-NONEXIST-PROBE-20260911 T-NONEXIST-ANCHOR \
  --workspace-id 1 --workspace-instance-id 1 --request-id req-repro-govfix07-2 ...
```

结果（修复前）：

```
✗ Subcommand 'task' failed: E_LEGACY_BIND_TASK_NOT_FOUND: ... 引用的任务不存在: T-NONEXIST-PROBE-20260911
RC=0   ← 假成功
```

根因链：
1. HTTP 客户端把 daemon error 信封统一 `raise DaemonRemoteError(code, message)`（server/daemon_client.py:2374-2380）；
2. `route_task_write` 对非连接级业务错误原样上抛（server/daemon_client.py:3728-3731）；
3. `cli/main.py::_dispatch_subcommand` 通用 `except Exception` 打 ✗ 后 `return True` → 进程 RC=0（cli/main.py:1774-1782，修复前行号）。

波及面：全部子命令家族的 daemon 权威拒绝（attest / supersede / cascade_close error 路径等）。
GOV-FIX-06 的 cascade_close `if "error" in result → sys.exit(2)` 分支在 HTTP 传输下为死代码（error 信封走异常路径）。

## 2. 修复内容

| 文件 | 修改 |
| --- | --- |
| cli/main.py `_dispatch_subcommand` | 在 `SharedTaskWriterRequiredError` 分支后、通用 `Exception` 前新增 `except DaemonRemoteError`：打结构化 `code: message` 红字并 `SystemExit(2)`。仅 daemon 权威拒绝 fail-closed；普通本地异常维持原语义（不扩大化） |
| cli/main.py attest 分支 | `result is None`（无响应）与 `error` dict 两失败路径补 `sys.exit(2)`（socket 传输对齐 GOV-FIX-06） |

## 3. live 验收（修复后）

| # | 场景 | 命令要点 | 结果 |
| --- | --- | --- | --- |
| 1 | daemon 拒绝（不存在任务） | attest + `request_id=req-repro-govfix07-3` | `✗ ... E_LEGACY_BIND_TASK_NOT_FOUND ...`，**RC=2** ✓ |
| 2 | 成功读路径（不扩大化） | `cw task show T-1789110285614-5e09ba98` | 正常输出，**RC=0** ✓ |
| 3 | 成功读路径 | `cw task next-action T-1789110285614-5e09ba98 --workspace-instance-id 4baea3ff12c2ea5c` | 正常投影，**RC=0** ✓ |
| 4 | 成功读路径 | `cw task list --workspace-id 1` | 正常列表，**RC=0** ✓ |

## 4. 回归测试

新增 `tests/test_cli_govfix07_daemon_error_rc.py`（4/4 通过）：

1. `test_daemon_remote_error_exits_2` — dispatch 层 DaemonRemoteError → SystemExit(2)
2. `test_local_exception_keeps_legacy_semantics` — 普通本地异常维持 return True（RC=0，不扩大化）
3. `test_shared_writer_required_still_exits_2` — 既有 RC=2 特例不回归
4. `test_attest_error_dict_path_exits_2` — attest socket 传输 error dict 路径 → SystemExit(2)

既有测试子集：

- tests/test_cli_main_help.py：27 passed
- tests/test_phase5_2_slice7_routing.py：1 passed
- tests/test_audit_chain.py：30 errors —— **基线即坏**（已在 HEAD e936787 临时 worktree 复现同数错误；PYTHONPATH/mock 环境差异，与本次改动无关）
- tests/test_cli_task_lease_parity.py、tests/test_rust_cli_diff.py：fixture `ensure_fresh_binary` 触发 cargo build，受已知 MSVC/Git Bash link.exe 环境问题阻塞；本卡零 Rust 改动，不构成阻断（advisory）

## 5. 结论

daemon 权威拒绝在 CLI 层从 RC=0 假成功修复为 RC=2 fail-closed，成功路径语义未破坏。批量驱动脚本的双重判定（rc+✗）中的 ✗ 项自此可与 RC 判定合并。
