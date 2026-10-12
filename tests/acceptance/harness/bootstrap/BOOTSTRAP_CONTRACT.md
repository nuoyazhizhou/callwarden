# TP-07 最小安全进程/传输底座合同（冻结版 r1）

阶段 B0 · owner: harness-bootstrap · 日期 2026-10-12 · 前置：TP-06（已 closed，候选散列见 `tests/acceptance/artifacts/build_results.json`）

本文冻结 TP-07 的 selector、隔离环境、验收命令与正负向义务。底座代码即本文同目录的 `daemon_bootstrap.py`，验收套件即 `test_bootstrap.py`。结果：**9 passed in 12.66s**（Windows + Py3.14 profile），junit 见 `deliverables/testing/TP-07/run-20261012-1/junit.xml`。

**隔离强度的实证**：验收期间本机有一个**真实生产 daemon 在线运行**（PID 26656，exe=`C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe`，sha256 `57d550ba…`，endpoint `127.0.0.1:14166`，生产 manifest 在真实 HOME）。7 个测试 daemon 全部在 run 根内启停、零孤儿，生产 manifest 文件集合与 mtime 未变——`test_no_production_manifest_written` 是在**生产 daemon 活跃**时通过的，即「禁止 production 发现」负向义务得到真实场景验证，而非空库自证。

## 1. 冻结 selector 与验收命令

```
selector: tests/acceptance/harness/bootstrap/test_bootstrap.py
解释器  : C:/Python314/python.exe（冻结 profile；--noconftest 隔离全局 tests/conftest.py）
cwd     : C:/git_work/callwarden
env     : CW_TP07_RUN_ROOT / CW_TP07_DAEMON_EXE 可覆盖默认 run 根与候选 exe
expected: rc=0，9 passed，0 failed，junitxml 落 run 根 reports/

C:/Python314/python.exe -m pytest tests/acceptance/harness/bootstrap/test_bootstrap.py \
    -p no:cacheprovider --noconftest -v \
    --junitxml=<run根>/reports/junit.xml
```

## 2. 候选来源（只读复制，TP-06 候选）

- `CW_TP07_DAEMON_EXE` 默认 `C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-07/src/cw-daemon.exe`
- 从 TP-06 run 根**只读复制**到本卡 run 根，sha256 复核 == `6324df120c850e45cee43349e3353e2fb88808e436127cbf9025c9e0a0e8859d`（TP-06 冻结值）
- 扩展候选本卡不消费（传输底座只需 daemon 二进制）

## 3. 隔离设计（源码实证，关键不变量）

daemon 状态目录的解析路径分散在三处，**必须同时覆盖**才能完全隔离：

| 源码位置 | 读取顺序 | 隔离手段 |
|---|---|---|
| `http_server.rs:1624 http_manifest_dir()` | **USERPROFILE → HOME**（不读 CALLWARDEN_HOME） | `USERPROFILE` / `HOME` → run 根 home |
| `daemon_server_handlers.rs:33 default_callwarden_dir()` | CALLWARDEN_HOME → USERPROFILE → HOME | `CALLWARDEN_HOME` → run 根 home |
| `daemon_server_handlers.rs:57 default_registry_db_path()` | `CALLWARDEN_DAEMON_REGISTRY_DB` → `CW_DAEMON_DATA_ROOT` → 默认 | 显式 `--registry` + `CALLWARDEN_DAEMON_REGISTRY_DB` |
| `daemon_server_handlers.rs:45 default_db_path()` | `CALLWARDEN_DB` → 默认 | `CALLWARDEN_DB` → run 根 |

**🔴 关键坑**：只设 `CALLWARDEN_HOME` **不够**——`http_manifest_dir()` 根本不读它。若不覆盖 `USERPROFILE`，manifest 会写入生产 `~/.callwarden/http-daemon.<authority>.manifest.json`；又因 Windows 上 `authority_id = current_daemon_owner_key()`（同 owner 同名），测试 daemon 会**覆盖生产 daemon 的 manifest**，对生产客户端制造 `E_HTTP_MANIFEST_STALE`。这正是本卡「禁止 production 发现」负向义务要堵的漏洞。

transport：迁移期默认 HTTP（`CW_DAEMON_TRANSPORT` 清空继承值，由本卡显式控制）；绑定 `127.0.0.1:0` 动态端口，`resolve_loopback` 在 bind 前对非 loopback fail-closed（`E_HTTP_MVP_LOOPBACK_ONLY`）。

## 4. 正向义务（逐项有独立 oracle）

| 义务 | 实现 |
|---|---|
| bind 0（隔离动态端口） | `--http-bind 127.0.0.1:0`；manifest `endpoint` 断言 `http://127.0.0.1:<非0端口>` |
| source/hash 一致 | manifest `daemon_binary_sha256` == TP-06 冻结候选散列 |
| PID 一致 | manifest `pid` == `Popen` 的 `proc.pid`（陈旧 manifest 不纳） |
| executable 一致 | manifest `daemon_executable` 规范化 == 实际启动 exe 路径 |
| manifest 一致 | `manifest_version == callwarden-http-manifest/v1`，原子发布在启动 ~1s 内 |
| 传输真正服务 | HTTP 探针 GET endpoint，任何状态码（含 4xx/5xx）都证明 server 在应答 |
| 日志排空 | 读线程持续排空 stdout/stderr（Windows select 不支持管道，必须线程），断言启动 banner |
| 状态只落 run 根 | manifest / registry / 任务库路径全部在 run 根内 |

## 5. 负向义务（违反即 fail）

| 场景 | 期望 |
|---|---|
| 非 loopback 绑定 | daemon 早退 rc≠0，**不留 manifest**（bind 前即拒） |
| 就绪超时 | `start(timeout=…)` fail-closed 抛 `manifest not ready within`，不假成功 |
| 陈旧 manifest | 已死 PID 的 manifest 不被新 daemon 误纳（`_wait_manifest` 只认 pid 自洽者） |
| 强杀 cleanup | `kill()` 后进程消失，`cleanup_run_root()` 只删 run 根内本 daemon 写过的子树，越界拒绝 |
| 禁止 production 发现 | 真实 HOME 的 `.callwarden` manifest 目录在 daemon 运行前后文件集合与 mtime 完全不变 |
| 禁止联网 startup | endpoint 必须 loopback；daemon 不发起外部连接 |

## 6. 套件清单（9 项）

正向：`test_daemon_starts_and_manifest_consistent`、`test_http_endpoint_serves`、`test_logs_drained`、`test_daemon_writes_state_only_under_run_root`
负向：`test_non_loopback_bind_rejected`、`test_startup_timeout_is_fail_closed`、`test_stale_manifest_not_adopted`、`test_force_kill_cleanup`、`test_no_production_manifest_written`

## 7. 已知边界

- 本卡只验「进程/传输底座」可用性与隔离安全，**不**跑任何业务 RPC（那是 TP-09+ 的 scope）。
- Windows named pipe 由 SID 派生（`--socket` 仅 CLI 兼容），本卡以 HTTP 为传输；named-pipe 适配合同由承诺 profile 拆分时另立（见 TASK_BREAKDOWN TP-07 行「HTTP/UDS/Pipe 按承诺 profile 拆适配合同」）。
- 未创建正式 daemon 任务（TASK_BREAKDOWN §7 入库门禁未过），不伪造 task_id。
- 与旧 `tests/convergence` 套件完全隔离：`--noconftest` + 独立 run 根，不修改任何旧 conftest。
