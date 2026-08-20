# HTTP MVP H5 独立复审与统一部署 Evidence Bundle

> 任务：`T-1786590214634-9e740cdc-sub-6`（H5，HTTP MVP 父任务最后一个子任务）
> 角色：tester/evidence
> 日期：2026-08-15
> 门禁：Independent Reviewer PASS 后 Coordinator 才 apply/close

本文件是 H5「独立复审、fresh runtime 与统一部署」的客观证据记录：Git HEAD、
toolchain provenance、fresh daemon 构建与部署（含 hash 对比）、daemon health、
capability registry 快照、HTTP 自举验证、以及与旧 evidence 的 diff 说明。
仅客观记录事实，不写结论性 PASS。

## 1. Git HEAD 与工作区状态

- `git log --oneline -1` → `5db4d84 docs: H4C 第二批文档同步收口（compat 路由 107/86 + MCP/CLI 补录 + task-plan §7.4-7.6）`
- `git rev-parse HEAD` → `5db4d843cdbaa20acf48aaea765b53f6f0c94613`
- `git status --short`（2026-08-15 构建前）仅有 untracked 无关产物：
  `.workbuddy/`、`_h4bc_matrix.py`、`docs/role-execution-report-2026-08-13.md`、`g0-reviewer-scratch/http-mvp/`
  —— 无已跟踪文件被修改，构建基线干净。

## 2. Toolchain Provenance

| 项 | 值 |
|---|---|
| Python 解释器 | `C:\Python314\python.exe` |
| Python 版本 | 3.14.3 (tags/v3.14.3:323c59a, Feb 3 2026, 16:04:56) [MSC v.1944 64 bit (AMD64)] |
| cargo | 1.93.1 (083ac5135 2025-12-15) |
| 构建配置 | release（`-RestartMcp -RunSmokeTests`） |
| 构建 target dir | `rust_ext\target\stage-refresh`（脚本隔离 stage 目录，不复用 Windows/WSL 共享 target） |

## 3. Fresh Daemon 构建与部署记录（规则 43 强制流程）

执行命令：

```powershell
pwsh -File .\scripts\refresh_shared_runtime.ps1 -TaskId T-1786590214634-9e740cdc-sub-6 -RestartMcp -RunSmokeTests
```

结果：`status=passed`，`rollback=false`，`error=null`。脚本完整输出见
`C:\Users\wanpi\.callwarden\runtime\evidence\20260815-151521-5db4d843cdba-0ae6f6b0.json`
（以下数据均取自该 evidence 文件，2026-08-15 07:15:21Z 开始，07:25:29Z 结束，约 10 分钟）。

### 3.1 部署时序

1. 先 cargo build（release，`cw-daemon/cw/cw-client/cw-agent/cw-bridge` 5 个二进制），不停止现有 MCP/daemon；
2. 停止旧 MCP（PID 42676、39336）与旧 daemon（PID 46828）；
3. 安装到 `%USERPROFILE%\.callwarden\runtime\current`（旧 runtime 备份为 `previous-<时间戳>`）；
4. 校验 `runtime/current/cw-daemon.exe` hash 与构建产物一致后启动新 daemon；
5. `dumpbin /dependents` 核验 Python DLL 依赖；
6. ping 通过 + 运行 daemon 路径/hash 复核；
7. smoke tests（`cw --version`、`cw daemon ping`）exit 0。

### 3.2 三端 hash 对比表（cw-daemon.exe）

| 端点 | SHA-256 |
|---|---|
| 构建产物（stage-refresh/release/cw-daemon.exe） | `98629c5f23b38fcf6588eb61e717bb297f0ef1fcd493839fc6fa9110271ad2fb` |
| runtime/current/cw-daemon.exe（evidence `binaries[0]`） | `98629c5f23b38fcf6588eb61e717bb297f0ef1fcd493839fc6fa9110271ad2fb` |
| 运行 daemon executable（evidence `daemon_runtime.sha256`） | `98629c5f23b38fcf6588eb61e717bb297f0ef1fcd493839fc6fa9110271ad2fb` |

三端一致 ✓（构建 = 安装 = 运行）。独立复核（2026-08-15，Get-FileHash）：
`runtime/current/cw-daemon.exe` = `98629C5F23B38FCF6588EB61E717BB297F0EF1FCD493839FC6FA9110271AD2FB`。

> 注：release binary hash 是构建快照值（PE 时间戳等非确定性，见
> `daemon-rust-migration-ledger.md` §9.1），发布流程以 Git commit + dumpbin 依赖清单 + 测试为准，
> hash 一致性作为部署正确性的辅助证据（本批次三端一致）。

### 3.3 Python DLL 依赖核验（dumpbin）

- 模式：`python_free`（cw-daemon.exe 为纯 Rust 二进制，未直接导入 python*.dll，属规则 43 允许情形）；
- 依赖列表：`[]`（无 python310/311/312/313.dll 等旧依赖）。

### 3.4 其余二进制（runtime/current）

| 二进制 | SHA-256 |
|---|---|
| cw.exe | `6714ba9e697db76ebb58db9c90608569fbcb58dab30b5ec25c33dfbe8f7184ea` |
| cw-client.exe | `441124af7eed928fd6bd19344af8bcb1ebad4882084980066779268818b46ee0` |
| cw-agent.exe | `8bac3291c30e2a73ef967ff93577810b4c14d1ba7e2e13bc2869632f905b7c31` |
| cw-bridge.exe | `e97607cd6594ce9bf282ee736e934159b41c927a779e9321ca1257360804052d` |

### 3.5 Smoke Tests（脚本内）

| 命令 | exit | 输出摘要 |
|---|---|---|
| `C:\Python314\python.exe cw.py --version` | 0 | `callwarden 0.3.23` |
| `C:\Python314\python.exe cw.py daemon ping` | 0 | `status=ok, pid 44044, transport=named-pipe, protocol_version=1` |

## 4. Daemon Health（部署后独立核验）

```text
$ python cw.py daemon health
{
  "status": "ok",
  "pid": 44044,
  "uptime_seconds": 36,
  "schema_version": 50,
  "workspace_count": 4,
  "snapshot_workspace_count": 0
}
$ python cw.py daemon ping
{ "status": "ok", "peer_uid": 4294967295, "pid": 43528,
  "authority_id": "LINKPLAY-SCM/windows/S-1-5-21-1583625257-826939952-3615027596-1001/67a410a0640fa885098ce577ab1c0113e6e80322c6fba8ce4253830c915f9899",
  "transport": "named-pipe", "task_db_fingerprint": "67a410a0640fa885098ce577ab1c0113e6e80322c6fba8ce4253830c915f9899", "protocol_version": 1 }
```

- schema_version=50（与运行时一致，未变）；
- health 与 ping 分别报告 PID 44044 / 43528 —— 部署后存在**两个 daemon 实例**
  （43528 带 `--socket` 由 refresh 脚本启动；44044 无参数由 MCP supervisor/autostart 拉起），
  两者 executable 均为 `runtime/current/cw-daemon.exe`（fresh binary，hash 一致）。
  双实例现象与 M2.3 时期 Reviewer 观察同类（AGENTS.md 规则 34 诊断边界：多个 MCP 同时启动时
  探针只允许一个 daemon，其他进程应重连同一 Named Pipe），本次未清理（tester 只读权限），
  如实记录供 Reviewer 判断。

## 5. Capability Registry 快照（以当前代码为准重新生成）

> 背景：`.trae-cn/evidence/http-daemon-capability-matrix.json` 是 H4B-M 阶段快照
> （2026-08-14，python_compat=193 / rust_native=44 / legacy_local=0，
> compat registry registered_methods=67）。H4C 第二批把 compat 路由推到 107。
> H5 快照脚本 `.trae-cn/evidence/h5_capability_snapshot.py`（只读，不写 DB、不改生产代码）
> 以「当前代码」为准重新核验，结果如下。

运行：`& 'C:\Python314\python.exe' .trae-cn\evidence\h5_capability_snapshot.py`

| 端 | 计数 |
|---|---|
| Rust `COMPAT_ROUTE_WHITELIST`（http_server.rs，源码提取） | **107** |
| Python registry（import server.compat_worker 触发全量装配后） | **107** |
| Python `RUST_COMPAT_ROUTE` 常量 | **107** |
| `validate_against_rust_route()` | **aligned=True**（missing=[] / extra=[] / mismatch={}） |
| Python 有而 Rust 无 | `[]` |
| Rust 有而 Python 无 | `[]` |
| operation_class 不一致 | `[]` |

三端对齐数字：**Rust 白名单 107 = Python registry 107 = RUST_COMPAT_ROUTE 107**。

与旧 H4B-M 矩阵的 diff 说明（matrix 是历史快照，非当前 registry 真相）：
- 旧矩阵 `metadata.compat_registry.registered_methods = 67`（H4B-M 时代）；
- 旧矩阵 `python_compat` 工具标注 193 个（含 registry 外直调 Python Mixin 的工具，按
  B1/B6 基线定义 python_compat=190 + rust_native=28 + legacy_local=19 的 237 全量分布，
  H4B-M 与 B6 的 backend 计数口径不同）；
- H5 快照关注的是 **compat registry 路由数**（HTTP compat worker 可达方法数）= 107，
  已随 H4C 全量装配同步；`h5-capability-registry-snapshot.json` 存有完整结构化快照。

快照文件：`.trae-cn/evidence/h5-capability-registry-snapshot.json`（gitignore 不提交）。

## 6. HTTP 自举验证

- 脚本内 smoke：`cw --version` / `cw daemon ping` 均为 named-pipe 传输（HTTP 自举冒烟
  由 release acceptance 测试覆盖，见 §7）。

## 7. Release Acceptance 测试结果（step #1 产物回填）

测试文件：`tests/test_http_daemon_release_acceptance.py`（H5 新增，自包含、真实断言、
不 mock）。运行命令：

```powershell
& 'C:\Python314\python.exe' -m pytest tests\test_http_daemon_release_acceptance.py -q
```

结果：**13 passed**（单独运行与并行回归同时运行两个场景均通过，exit 0）。

覆盖内容（逐项）：

1. `TestFreshDaemonArtifacts`（4 用例）：
   - `runtime/current/cw-daemon.exe` 存在；
   - 最新 refresh evidence `status=passed` 且 `git_head` = 当前 HEAD `5db4d84…`；
   - 安装 hash == evidence 记录 hash（`98629c5f…`）；
   - 运行中来自 runtime/current 的 daemon hash == evidence（并行测试 spawn 的
     隔离 daemon 不计入，避免跨测试干扰——修复记录见测试文件 docstring 下注释）。
2. `TestCapabilityRegistryThreeWayAlignment`（4 用例）：import server.compat_worker
   全量装配后 registry=107 = RUST_COMPAT_ROUTE = Rust COMPAT_ROUTE_WHITELIST 提取值，
   `validate_against_rust_route()` aligned=True，worker 侧 registry 一致。
3. `TestDaemonHealthReachable`（1 用例）：`cw daemon health` 真实入口
   status=ok、schema_version=50、pid 非空（生产 daemon 为 named-pipe 传输，
   HttpDaemonRpcClient 面向 HTTP 会 E_HTTP_MANIFEST_MISSING fail-closed，属设计行为，
   故走 CLI 真实入口；daemon 未运行时 skip 附诊断）。
4. `TestHttpBootstrapSmoke`（4 用例，隔离 daemon + runtime/current fresh binary +
   `--http-bind=127.0.0.1:0`）：
   - manifest discovery + `/health` 交叉核对（pid/schema/security_profile=HTTP MVP profile）；
   - `/capabilities`：server_mode=HTTP MVP、python_compat available 集合 ==
     Rust 白名单 107 项（三端对齐最强实证）；
   - 真实 HTTP RPC round-trip：`stats_top_files` / `get_uncommented_symbols`
     经 compat worker 受理，绝不 method_not_found；
   - 负向：registry 未注册的 `get_code_metrics_summary` 必 method_not_found
     （HTTP 模式 fail-closed 实证）。

关键回归（H4C 装配门，运行同一次会话）：

```powershell
& 'C:\Python314\python.exe' -m pytest tests\test_http_capability_registry.py tests\test_http_combined_worker_cutover.py -q
```

结果：**120 passed**（capability_registry 72 + combined_worker_cutover 48，单独与并行场景均 exit 0）。

## 8. 环境限制与观察（客观记录）

1. 双 daemon 实例（43528 / 44044，均 fresh binary）：见 §4，未清理，供 Reviewer 判断。
2. 旧 evidence（H4B-M matrix json）与当前 registry 的计数口径不同，H5 快照以
   registry 路由数（107）为 compat worker 可达方法数真相，matrix 仅作历史 diff 参考。
3. release binary hash 为构建快照值（§9.1 约定），三端一致为本批次部署正确性证据。
