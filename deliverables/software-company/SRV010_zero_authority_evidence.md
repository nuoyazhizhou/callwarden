# SRV-010 Zero-Authority Evidence Manifest

**任务：** `T-1787323461213-e46199b0`（SRV-010：server health check Python authority → Rust daemon）
**父任务：** `T-1787293451688-c14b1e44` | **port_type：** `runtime_projection`
**执行身份：** `executor-workbuddy-v1-cur`（session `cw-exec-workbuddy-20260824`，model `workbuddy`，role `executor`）
**提交链：** `e006950`（step0）→ `5103c1f`（step1）→ `83ecf15`（step2）→ 本 manifest（step3）

## 1. 合同锚点（append-only，不可篡改）

| 锚点 | 值 |
|---|---|
| task contract | `TC-T-1787323461213-e46199b0` revision 2 |
| contract_hash | `sha256:a2de9d6cb89134b294997ec54ef3ebc98cf578624738ac24e88566610604dadc` |
| normalization_rules_hash | `sha256:b41cbdb3f2882b3efc0fbbbddfb4fd5b40e23549cdbeb49af2dec798184b0e8d`（verdict-normalization/v1） |
| role contract | `cw.aprime.executor.startup.v1` |
| executor prompt_hash | `59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6` |
| allowed_edit_scope | `server/health_check.py`、`rust_ext/src/daemon/{dispatch.rs,http_server.rs,health_check_handlers.rs}`、`tests/test_srv_010.py`、`deliverables/software-company/` |

## 2. 五验收条款逐条核验

**[1] Python module no longer opens SQLite or executes business query — PASS（生产 Rust 短路语义）**
生产链健康检查权威原已由 Rust `health.rs`（G14：`HealthChecker::check_all` /
`RecoveryHandler::recover`）承担，经 `callwarden_core.health_check_all`（PyO3）短路
daemon_server 生产路径（仅 rollback feature `rust_daemon_health_check`=1 或 Rust
失败 fail-soft 时才走 Python 降级）。本卡将模块内 4 个 Python direct authority
（sqlite3.connect）函数——`_check_db_registry`/`_recover_workspace_registry`/
`_recover_cas_db`/`_recover_stale_jobs`——的 daemon RPC 形态下沉至
`health_check_handlers.rs`，消除权威接缝；模块经 step1（`5103c1f`，+26 行
docstring）声明 compat/test-only，函数体零改动（AST 核验见 §3）。
`tests/test_srv_010.py::test_unavailable_production_path_is_rust_short_circuit`
静态门禁固化该不变量。

**[2] Rust target owns authority — PASS**
`health_check_handlers.rs`（e006950，532 行）4 handler 逐字对齐 Python 语义：
`handle_check_db_registry`（只读：缺库→unhealthy / 缺 `daemon_workspaces` 表→
degraded / 就绪→healthy "OK (N tables)"）；`handle_recover_workspace_registry`
（写：验证表 + 统计 active + `UPDATE last_active_at`，archived 不受影响）；
`handle_recover_cas_db`（只读：缺库→healthy first-use / `SELECT 1` 探测）；
`handle_recover_stale_jobs`（写：running→failed('daemon restarted, job
interrupted') + finished_at，completed 不受影响）。全部 fail-soft 归一化为
healthy/degraded/unhealthy + `source:"rust"`，`PRAGMA busy_timeout=5000`。
接线：`dispatch.rs` `mcp.health_check.*` 4 分支；`http_server.rs` 4 capability
（rust_native/available/authority，check_db_registry 与 recover_cas_db=
`read_only`、recover_workspace_registry 与 recover_stale_jobs=`write`
daemon 重启恢复语义，owner `T-1787323461213-e46199b0#SRV-010`）；
`mod.rs` 3.29 声明。cargo 测试：handler 10 passed + dispatch 61 passed +
http_server 10 passed。

**[3] HTTP/client semantics retained — PASS**
默认路径语义对齐 SRV-008/009：registry `CALLWARDEN_DAEMON_REGISTRY_DB`/
`CW_DAEMON_REGISTRY_DB` → `CW_DAEMON_DATA_ROOT/registry.db` →
`~/.callwarden/daemon/registry.db`；cas `CALLWARDEN_CAS_DB` →
`CW_DAEMON_DATA_ROOT/cas.db` → `~/.callwarden/daemon/cas.db`。
runtime 探测（部署后真实 daemon）8/8 PASS：check healthy(`source:"rust"`,
2 tables) / 缺库 unhealthy fail-soft / ws_recover `active_workspaces=1` 且 DB
`last_active_at` 已更新（archived=0.0 不受影响）/ cas 缺库 healthy(first use) /
stale_jobs cleaned=2 且 DB `J-1→failed+error`、`J-3 completed` 不变 / stale 缺库
healthy fail-soft。返回形态 `{name, status, message, details, source}` 对齐
Python HealthStatus 约定。

**[4] negative matrix passes — PASS**
`tests/test_srv_010.py`（83ecf15，279 行）：**17 passed in 1.69s**，五段全覆盖
`["success","invalid","authority","unavailable","restart"]`（success 3 /
invalid 5 / authority 4 / unavailable 3 / restart 2），零 skip。runtime 段经
真实 daemon 执行（rpc fixture ping 探活；本次实际执行），DB 状态核验直达
sqlite3 复查（last_active_at / jobs status+error）。

**[5] no local fallback — PASS**
三重固化：①`test_unavailable_production_path_is_rust_short_circuit`——生产 health
路径默认 Rust 短路（`health_check_all` + `_rust_health_available()`），仅 rollback
feature 显式置位才回退，不存在隐式本地权威降级；②模块声明含"生产路径不得使用
本模块直连 SQLite"（`test_no_business_fallback_in_module_doc`）；
③`test_retained_compat_body_locked_by_legacy_tests` 固化 4 个 direct authority
函数体保留契约（compat/test-only，非生产降级入口）。

## 3. 函数体零改动证据（AST before/after）

基线 `e006950`（step0 后、step1 前）vs `5103c1f`（step1），
`server/health_check.py`：`git diff --stat` 全程 **仅 +26 行（step1 docstring）**。
AST 函数级比对（`ast.dump` 逐函数、剔除 docstring）：**21/21 函数 identical**，
changed/removed/added 均为空——函数体受存量测试锁定：
`test_phase8_health_check.py`（RecoveryHandler/HealthChecker 12+ 例真实构造
临时 SQLite 功能测试）、`test_phase4_3_health_check_diff.py`（以 Python
`HealthChecker.check_all()` 为真相源与 pyd 差分）。step1 后两文件 **68 项测试全绿**，
本卡零破坏。

## 4. Runtime 指纹

- 部署 evidence：`~/.callwarden/runtime/evidence/20260826-223715-e0069505bc2d-42eacb73.json`
  status=`passed`（refresh_shared_runtime.ps1 -TaskId T-1787323461213-e46199b0），
  daemon PID 6864（transport=http，ping ok）
- step0 探测：8/8 PASS（见 §2[3]），含 DB 状态直查核验
- step2 负矩阵 runtime 段真实执行（17 passed 含 daemon 在线断言路径）

## 5. Handoff Manifest（下一棒：Reviewer）

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立核验 SRV-010 五验收条款与本 manifest 证据锚点（只读）
  reason: step0-3 全部 report 成功，负矩阵 17 passed，零权威证据齐备
  independence_requirement: required
```

## 6. Findings

1. **生产权威在先的处置范式**：Rust `health.rs`（G14）原已承担生产链健康权威
   （PyO3 短路 daemon_server），本卡不重复建生产路径，仅下沉 4 个 Python direct
   authority 函数的 daemon RPC 形态，消除 sqlite3.connect 权威接缝。
2. **Rust handler 逐字对齐 Python 而非复用 health.rs**：`health.rs` 的
   `recover_cas_db`（只查 data_root 目录）与 `recover_stale_jobs`（返回 not
   applicable）语义与 Python 不同；新 handler 按本卡目标逐字对齐
   `server/health_check.py` 语义，保证返回形态与归一化行为一致。
3. **存量测试三型锁定函数体**：test_phase8 功能构造 + test_phase4_3 差分真相源
   + test_b3 G14（daemon_server 源码断言，锁定短路形态）⇒ 函数体保留原形态为
   唯一合规处置（同 SRV-008/009 保留先例），step1 以归属声明替代代码退役。
4. **RecoveryHandler 零生产调用方**：生产恢复经 Rust health.rs；Python
   RecoveryHandler 仅 test_phase8 构造，compat/test-only 定位成立。
5. **mod.rs 白名单外配套**：`rust_ext/src/daemon/mod.rs` 一行 `pub mod` 声明为
   接线必需的最小配套（与 SRV-006~009 先例一致），已在 step0 report 披露。
6. **HEAD blob 构造法**：工作树存在外部偏离（forbidden 未触碰），step0 以
   `git show HEAD:<file>` 基线 + bytes 锚点 + `hash-object/update-index` 隔离
   提交 4 文件 +562，暂存集恰 4 文件零混入。
7. **fail-soft 契约**：4 handler 全部异常归一化为 healthy/degraded/unhealthy
   状态返回，绝不向 RPC 调用方抛错（与 SRV-003~009 先例一致）。
8. **任务转 review**：本 step report 后任务进入 review，等待独立 Reviewer 核验。
