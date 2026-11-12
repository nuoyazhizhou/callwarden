# HTTPS+mTLS 远端控制面治理链路闭环总结报告

> 生成日期：2026-11-12
> 范围：ADR-002（`docs/design/mtls-remote-plane.md`）定义的远端控制面 S1–S4 全部治理卡
> 结论：**全部 17 张 REMOTE 卡 closed，S1–S4 收口门禁 PASS，治理链路完整闭环**

---

## 1. 总览

| 维度 | 状态 |
| --- | --- |
| 架构决策 | HTTPS + mTLS 单轨传输（ADR-002），pipe/UDS/bridge 全量删除 |
| 任务卡 | 17 张 REMOTE 卡全部 `closed`（含 1 张总卡、4 张分期子卡、1 张收尾卡、8 张清理/取证卡、2 张 D4 缺陷卡、1 张部署口径卡） |
| Reviewer 盲审 | 17 份 verdict，最终全部 `pass`（S3 卡曾有 1 份 `block` verdict，即 D4 缺陷，已修复重审转 pass） |
| Adjudicator | 全部卡经 `adjudicator_accepted` → `apply`（applied）→ `close`（closed）三步裁决闭合 |
| 矩阵门禁 | `python scripts/check_e2e_matrix_closure.py` → **PASS（收口条件全部满足）**，S1-01…S1-20 / S2-01…S2-09 / S3-01…S3-10 / S4-01 / S4-03 逐行 [OK] |
| 代码提交 | `179be21`（S4 单轨清理）、`39cd150`（D4 修复，4 files, +768/−23） |

## 2. 治理链路与角色分离

全程禁止自审，三类角色会话/实例完全隔离：

| 角色 | 身份 | 职责 |
| --- | --- | --- |
| Executor | trae-executor | lease acquire → 实现/取证 → report（step_id/evidence_path/evidence_hash 绑定）→ handoff `executor_ready_for_review` |
| Reviewer | trae-reviewer（inst/sess `1793414579172-rev`） | 独立会话真实 lease → Role_View 盲审投影 → verdict（view_manifest_hash/contract_hash 固化）→ `reviewer_pass` |
| Adjudicator | trae-adjudicator（inst/sess `1793414579172-adj`，模型 DeepSeek-V4.1-Flash） | `--role adjudicator` lease → handoff `adjudicator_accepted`；apply/close 需重新 `--role reviewer` lease（fencing counter 防重放） |

链路闭环要求：report provenance 三元组（`report_request_id` 与 DB `task_events` reason_code='reported' 的 step_id/evidence_path/evidence_hash）必须逐字一致，否则 `E_HANDOFF_REPORT_PROVENANCE_MISMATCH` 拒绝——本轮多次被该门禁拦截后修正，证明 fail-closed 生效。

## 3. 卡片清单（全部 closed）

### 3.1 分期实施（S1–S4）

| 卡 | 标题 | Verdict |
| --- | --- | --- |
| T-1793774104011-34e361b4 | [REMOTE] 总卡：删除 pipe/UDS/bridge（S1–S4） | V-5f6cc39e06c831aa367b1f5e |
| T-1793774130685-6acab9d4 | [REMOTE-S1] mTLS 控制面（TLS + 内置 CA + principal + Python 证书） | V-d73e6509eff540ddf96c8e1e |
| T-1793774130739-6e096a14 | [REMOTE-S2] 远端只读面（客户端本机执行 + snapshot.upload + db_path 拒绝） | V-44a5fc4d31f1dc9676bb83bc |
| T-1793774130800-71aa5b74 | [REMOTE-S3] 远端写治理（write.grant/base_hash/审计索引） | 先 block：V-c0f858cb214d97f94d443418（D4）→ 修复后 pass：V-88e7fa2a2fd3e26c4a195e6a |
| T-1793774130880-766de4a0 | [REMOTE-S4] 单轨清理与跨 OS 矩阵 | （子卡闭环后随总卡闭合） |
| T-1793807024448-17d4cd60 | [REMOTE-S4-FU] daemon RPC 内核去 pipe/UDS（单轨收尾，承接 deferred） | V-dfa3ac0b7e64b682e5451cd8 |

### 3.2 缺陷取证与修复（D4 专项）

| 卡 | 标题 | Verdict |
| --- | --- | --- |
| T-1794452006394-a7a161a0 | [REMOTE-S4-06-D4] 写治理链路跨 OS 实机取证 | V-ea6565b9993e621f6d54ebcc |
| T-1794454711064-62856418 | [REMOTE-S4-06-D4-FIX] 修复 remote 写基线与 snapshot.upload 不同源 | V-8cf6a3c1c5fe475e6f2bddfe |

### 3.3 收口、部署口径与遗留清理

| 卡 | 标题 | Verdict |
| --- | --- | --- |
| T-1794410782335-6e36db64 | [REMOTE-S4-06] 跨 OS 实机回归取证与收口 | V-aace1fd8034a11bf8307a1bb |
| T-1794413849157-7b176de0 | [REMOTE-S4-07] cw-agent 部署口径去 pipe/UDS 残留并复核文档一致 | V-33f15ea9ae7f7b5f47cb707a |
| T-1794414587897-7b694118 | [REMOTE-LEGACY] config.py 去 v1 socket/pipe 默认与 bridge 解析残留 | V-1f0f030b0aa9e073bf4eacd2 |
| T-1794429656781-fadf491c | [REMOTE-LEGACY] 打包 systemd unit 去 v1 socket 残留 | V-5a4869b7941db973a0fbc12c |
| T-1794440035255-681da7fc | [REMOTE-LEGACY] 清理打包 SOCKET 变量并标注 daemon_socket parity 键 | V-5309a6a8032546bb3f520bcc |
| T-1794440363268-c739d728 | [REMOTE-LEGACY] 收口 systemd 打包资产 UDS 残留注释 | V-f1a0994337be651cdb889e88 |
| T-1794440752338-5d97f8a8 | [REMOTE-LEGACY] 收口 Debian 包描述与 enterprise 提示的 UDS/socket 文案 | V-3c6348e3cf8f3d2fdfea3590 |
| T-1794441441949-eda545e0 | [REMOTE-LEGACY] 重写文档容器 client 示例为 HTTPS+mTLS | V-0bcaf934a23cc442747ba156 |
| T-1794297791083-9ac6e5dc | [FIX-C] server.crt SAN 缺 IP:0.0.0.0 致 E_TLS_SAN_MISMATCH | V-10878fb9789e85666a21608e |

## 4. D4 缺陷闭环（本轮核心工作）

### 4.1 缺陷

remote `write.grant` 对既有文件提交 `expected=""` 时被 `E_WRITE_STALE_BASE` 拒绝——
跨 OS 实机回归（Windows ↔ WSL 双向）中写面全 FAIL。

### 4.2 根因

`snapshot.upload` 的 ingest 基线只写入快照库，未同步主 codegraph DB 的写基线表；
写治理读基线时拿到空基线，与客户端 `expected` 不一致。

### 4.3 修复（提交 39cd150）

- [snapshot_state.rs](file:///c:/git_work/callwarden/rust_ext/src/daemon/snapshot_state.rs)：抽取 `resolve_codegraph_db_path`；新增 `sync_uploaded_baseline` —— 用 `ensure_workspace_row_by_root(host_real_root)` 与写治理读面**同源**解析目标 workspace id，事务内补 `workspaces` 外键行，POST publish 后同步基线；首建库 canonical DDL 引导；全程 fail-closed。附 4 项单测。
- [test_remote_write_governance.py](file:///c:/git_work/callwarden/tests/test_remote_write_governance.py)：新增 `TestS3RemoteCrossOsWriteGovernance` env-gated 跨 OS 实机用例。

### 4.4 双端复验证据（gitignored，不入库）

| 证据 | sha256 |
| --- | --- |
| `.trae-cn/evidence/s4-06/d4-write-governance-cross-os-fix.json`（6149B） | `fedeb12f683234338f7d8ba691c4d2410026f5ea01976681800d74ee7a1a50dc` |
| `.trae-cn/evidence/s4-06/d4fix-raw/A-win2wsl-after-fix.log` | `5772bf97…` |
| `.trae-cn/evidence/s4-06/d4fix-raw/B-wsl2win-after-fix.log` | `b2672d9f…` |
| `.trae-cn/evidence/s4-06/d4-write-governance-cross-os.json`（缺陷取证） | — |

双端结果：A（win→wsl）与 B（wsl→win）写面全部 PASS，`E_WRITE_STALE_BASE` 不再复现。

### 4.5 文档一致性收口

- [mtls-remote-plane.md](file:///c:/git_work/callwarden/docs/design/mtls-remote-plane.md)：§17.6 缺陷标签改为「已于同日修复并闭合（见 §17.7）」；§17.7 记录修复与双端复验。
- [cli-mcp-matrix-backlog.md](file:///c:/git_work/callwarden/docs/design/cli-mcp-matrix-backlog.md)：§38.4 S4-06 行由「判 BLOCKED，不得计 PASS」改为「D4 闭合，写面可计 PASS」。
- 全 `docs/design` 扫描「不得计 PASS / 未修复 / D4 未闭合」0 残留命中。

## 5. 门禁与最终验证（2026-11-12 实测）

- `cw task list`：17 张 REMOTE 卡全部 `[closed]`，无 `review`/`applied` 残留。
- `python scripts/check_e2e_matrix_closure.py` → `结论: PASS（收口条件全部满足）`：
  S1-01…S1-20（测试证据：`test_mtls_control_plane.py` / `test_mtls_enrollment.py` / `test_principal_authorization.py`）、
  S2-01…S2-09、S3-01…S3-10（owner_task_closed）、S4-01/S4-03（`test_single_track_kernel.py` 用例标记）全部 [OK]。
- 工作区干净：最新提交 `39cd150 fix(s4-06): snapshot.upload 基线与写治理同源，D4 双端实机闭合`。

## 6. 遗留与说明

1. 证据文件（`.trae-cn/evidence/` 下 `T-*.json`、`d4fix-raw/`、`d4-write-governance-cross-os*.json`）按约定 gitignored，不入库；哈希已固化在 verdict 与本报告中。
2. `T-1790136923625-2803e218` 的历史遗留豁免（`T-1789563951668-ba0c338c`，supersede 死锁）由 `LEGACY_WAIVERS` 证据文件 `docs/evidence/supersede_9-16_live_e2e_20261029.json` 承接，与 REMOTE 面无关。
3. 整个 HTTPS+mTLS 远端控制面（pipe/UDS/bridge 删除、S1–S4、D4 缺陷取证与修复）的治理链路至此**完整闭环**。
