# Q10 raw-gz-body multipart + 异步受理：执行证据

**日期**：2026-09-17
**线索**：client_convergence P0 CR（Q9 大文件传输续作）
**提交**：
- `a9f8bde` feat(q10): raw-gz-body multipart 传输——消除 base64 税
- `efee889` feat(q10): 异步受理——202 accepted + 后台解析 + request_id 轮询

## 1. 交付内容

| 文件 | 改动 | 要点 |
|---|---|---|
| `rust_ext/src/daemon/http_server.rs` | +280 | 新路由 `POST /v1/rpc/multipart`（axum `Multipart` 提取器）；`validate_rpc_envelope` 与 `/v1/rpc` 共用；`rpc_multipart_handler`：part 解析（`params` 必需、`payload`/`payload_gz` 互斥、尺寸 413 门禁）→ dedup reserve（全部门禁同步生效）→ 异步分支（`workspace.file.refresh` 且无 `X-CW-Wait` → `tokio::spawn` 整个 dispatch + 结果落 dedup + 202 accepted）→ 否则同步分发 |
| `rust_ext/src/daemon/dispatch.rs` / `workspace.rs` / `snapshot_state.rs` / `replicator_handlers.rs` | （`a9f8bde`） | `InlinePayload<'a>`（Raw/Gz）贯穿 dispatch 链；tier-0 消费裸字节 / gz（`decompress_gz_bounded` 复用 Q9 bomb 防护，`canonical_len` 上限） |
| `server/daemon_client.py` | `call_multipart()` + `_build_multipart_body()`（RFC 7578 手写编码器）+ `MultipartUnsupportedError`（404 降级）+ `supports_multipart` 显式能力位 |
| `server/agent_protocol.py` | `_await_async_refresh()`（202 → 轮询同 request_id → Replay 最终结果；`E_REQUEST_IN_FLIGHT` 继续轮询，600s 超时上抛）+ `send_refresh_to_daemon` 分档（≤1MB hex / ≤32MB raw / >32MB gz / FD 优先 / 404 降级 gz_b64） |
| `tests/test_cw_agent_daemon_integration.py` | `TestSendRefreshMultipartTiers`（4）+ `TestSendRefreshAsyncAccepted`（3）+ 小/大文件回归 |

## 2. 验证证据

| 验证 | 结果 | 产物 |
|---|---|---|
| Rust 单测 `http_server::tests::test_rpc_multipart*` | **8/8 ok**（202 异步 / X-CW-Wait 同步 / 轮询回放 / 缺 params / payload 冲突 / 畸形 JSON / duplicate key / ping） | wrapper 日志 `C:\Users\wanpi\AppData\Local\Temp\cw_cargo_check.log` |
| Python pytest（Multipart/Async/SmallFile/LargeFile） | **14 passed + 1 skipped** | `Temp/q10_pytest_final.log` |
| live probe phase-1（隔离 release daemon） | **5/5**：raw multipart committed / gz committed / gz bomb 被拒 / 旧 `/v1/rpc` ping 200 / 小文件 hex committed | `Temp/live_probe_q10_result.txt` |
| live probe phase-2 异步 | **4/4**：F1 accepted 0.69s → F2 轮询 committed 5.01s（generation 1:1）；G `_await_async_refresh` 透明收口 2.28s committed（1:2）；H `X-CW-Wait: 1` 强制同步 HTTP **200**（非 202）committed 2.21s（1:3）；D 旧路由 ping 回归 | `Temp/q10_async_probe_run3.log` |

### 异步语义要点（已 live 实证）

- dedup `check_and_reserve(First)` 在 202 之前**同步**完成：ACL / stale_session / envelope 门禁一个不漏
- 后台 task 完成后结果落 dedup；client 按同 request_id 重发 → `InFlight` 自旋（50ms tick，`deadline_ms` 默认 30s、上限 120s）→ `Replay` 返回**最终业务结果**（`stale_seq_dropped` 也会被如实 Replay——probe run-2 意外佐证了这一点）
- `X-CW-Wait: 1` → 同步分支，单次往返拿结果（HTTP 200）
- 语义与同步完全一致：调用方（`send_refresh_to_daemon`）对 202 无感知

### 隔离验证二进制（未触碰共享 runtime）

| 项 | 值 |
|---|---|
| 路径 | `C:\git_work\callwarden\.workbuddy\p0j_isolated_target\release\cw-daemon.exe` |
| SHA256 | `7aa0ffce2a6a9bfc1aea66d45a531f8e1fd3b18efd364557cc67fb424d5347c6` |
| size | 45964288 |
| manifest 自报 git_commit | `a9f8bded…`（异步代码当时未提交；`efee889` 后的重建会更新该字段——异步分支的存在已由 202 响应本身证明） |

## 3. 部署状态：已执行并通过（共享 runtime）

部署治理 TaskId：**T-1787293451688-c14b1e44**（Q10 所属迁移任务线）。因 PowerShell
tool 后台 runspace 在 tool-call 返回后被宿主终止（`refresh_shared_runtime.ps1` 的
cargo 阶段被杀，catch/finally 未执行、无 evidence，与 09-09 §5.9 同因），改为
「Bash 后台全量构建 + Python 手动复刻部署闭环」两段式：

1. **构建**（Bash 后台，独立 `CARGO_TARGET_DIR=stage-refresh`）：rc=0，4 bin +
   PyO3 extension 齐备
2. **换装**（`Temp/deploy_q10_manual.py`，复刻 ps1 核心语义）：Python3.14 权威
   校验 → core 双目标部署（repo pyd + user site-packages，原子替换 + hash 校验 +
   dumpbin python314 依赖检查）→ `cw.exe lease status` migration authority →
   staging 拷贝 + `runtime/current` 原子换装（旧版备份 `previous-*`）→ hash 三方
   校验（构建 = 安装 = 运行）→ `setx CW_DAEMON_BIN`（User 级）→ 停旧 daemon →
   启动新 daemon → ping 轮询 → 运行态核验 → smoke → evidence JSON

### 3.1 部署 evidence

| 项 | 值 |
|---|---|
| evidence JSON | `~/.callwarden/runtime/evidence/20260918-005353-a1440c9062dc-5f6d039a.json` |
| status | **passed** |
| git_head | `a1440c9062dc…` |
| runtime/current cw-daemon.exe SHA256 | `32aa085589ec684c9bf5cb5879fd0337ed5cb0c330581a37ec770edf75d6bc13` |
| core.dll / repo pyd / site-pkg pyd SHA256 | `79067a0c6936ecff…`（三方一致） |
| py 依赖 | core = `python314_direct`；cw-daemon.exe = `python_free` |
| authority_cli | exit=2（业务错误不阻断，migration authority 通过） |
| daemon 启动 | ping OK ~2.0s；smoke `cw.py --version` + `daemon ping` 均 exit=0 |

### 3.2 部署后共享 runtime 回归（`Temp/q10_shared_regress.py`）

| 项 | 结果 |
|---|---|
| manifest `daemon_binary_sha256` = `32aa085589ec684c…` | PASS |
| manifest `daemon_executable` = `runtime\current\cw-daemon.exe` | PASS |
| `/health` git_commit = `a1440c9062dc…` / schema 60 / worker healthy | PASS |
| `cw.py daemon health` / `daemon ping` | exit=0 / exit=0 |
| Q10 multipart async 1.20 MiB（F：accepted → `_await_async_refresh` 收口 → committed generation 1:1，2.48s） | PASS |
| 再来一次（G：committed generation 1:2，1.86s，轮询路径稳定） | PASS |

### 3.3 部署过程的两个坑（已修，写入记忆）

1. `resolve_core_targets` 探测子进程必须从**临时目录**运行（原 ps1 的
   `Push-Location $tempDir`）：否则仓库根顶层 `callwarden_core.pyd`（单文件模块）
   遮蔽 user site-packages 的 package 形态，`callwarden_core.callwarden_core`
   报 `not a package`
2. 运行态 daemon 路径核验须用 `QueryFullProcessImageNameW` +
   `PROCESS_QUERY_LIMITED_INFORMATION`：`GetModuleFileNameExW` 需 VM_READ，
   非管理员沙箱返回 None（误判为"路径不等于 current"触发整部署回滚）

## 4. 下一棒

- Q10 全链路已闭环（三道门 + 隔离 live + 共享 runtime 部署 + 部署后回归全绿）
- spec 验收清单部署项已回填：`Temp/q10_raw_gz_body_spec.md`
