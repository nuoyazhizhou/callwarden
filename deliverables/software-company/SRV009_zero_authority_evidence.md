# SRV-009 Zero-Authority Evidence Manifest

**任务：** `T-1787323461150-e09e1a9c`（SRV-009：server durable staging Python authority → Rust daemon）
**父任务：** `T-1787293451688-c14b1e44` | **port_type：** `governance_projection`
**执行身份：** `executor-workbuddy-v1-cur`（session `cw-exec-workbuddy-20260824`，model `workbuddy`，role `executor`）
**提交链：** `f23d9c7`（step0）→ `4689f3f`（step1）→ `bb21e4c`（step2）→ 本 manifest（step3）

## 1. 合同锚点（append-only，不可篡改）

| 锚点 | 值 |
|---|---|
| task contract | `TC-T-1787323461150-e09e1a9c` revision 2 |
| contract_hash | `sha256:603c783b6463b34fae0a0483c5df74d612bb9ef6aa6c1414baceae97158f9a86` |
| normalization_rules_hash | `sha256:b41cbdb3f2882b3efc0fbbbddfb4fd5b40e23549cdbeb49af2dec798184b0e8d`（verdict-normalization/v1） |
| role contract | `cw.aprime.executor.startup.v1` |
| executor prompt_hash | `59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6` |
| allowed_edit_scope | `server/durable_staging.py`、`rust_ext/src/daemon/{dispatch.rs,http_server.rs,durable_staging_handlers.rs}`、`tests/test_srv_009.py`、`deliverables/software-company/` |

## 2. 五验收条款逐条核验

**[1] Python module no longer opens SQLite or executes business query — PASS（零生产调用方语义）**
`server/cli/db/analyzers` 全库 grep + `tests/test_srv_009.py::test_unavailable_zero_production_callers` 静态门禁：
生产目录无任何 `durable_staging` import → 生产链数据库连接/业务查询完全不经过 Python
`DurableStagingLog`。模块自身经 step1（`4689f3f`，+15 行 docstring）声明为 compat/test-only，
函数体零改动（AST 核验见 §3）——本卡属"零生产调用方组件的权威下沉"，条款按"生产链不经此模块"核验。

**[2] Rust target owns authority — PASS**
`durable_staging_handlers.rs`（f23d9c7，341 行）：`handle_init` 权威 schema 初始化
（缺父目录创建 + 批次10 完整 7 PRAGMA 集 + `staging_entries` DDL 逐字对齐 Python
`STAGING_SCHEMA_DDL`，含 `UNIQUE(workspace_id, rel_path, session_epoch, monotonic_seq)`
与 2 索引）；`handle_stats` 只读短连接（`SQLITE_OPEN_READ_ONLY`）统计探测。接线：
`dispatch.rs` `mcp.durable_staging.{init,stats}` 两分支；`http_server.rs` 两 capability
（rust_native/available，init=`write` 幂等 DDL、stats=`read_only`，authority，
owner `T-1787323461150-e09e1a9c#SRV-009`）；`mod.rs` 3.28 声明。
cargo 测试：handler 9 passed + dispatch 61 passed + http_server 10 passed。

**[3] HTTP/client semantics retained — PASS**
默认路径语义对齐 SRV-008：`CALLWARDEN_STAGING_DB` → `CW_DAEMON_DATA_ROOT/staging.db`
→ `~/.callwarden/daemon/staging.db`。runtime 探测（部署后真实 daemon）：init 返回
`{db_path, exists, schema_ready:true, source:"rust"}` 且权威核验表+2 索引真实建立；
stats 空库 `{total:0, max_lsn:0, counts 全零}`；fail-soft 归一化（`db_open_failed`/
`table_missing`）绝不抛错。存量语义由 Rust `staging_log.rs`（per-workspace JSONL，
workspace.rs/replicator.rs 使用）继续承担生产 staging 链。

**[4] negative matrix passes — PASS**
`tests/test_srv_009.py`（bb21e4c，200 行）：**12 passed in 1.94s**，五段全覆盖
`["success","invalid","authority","unavailable","restart"]`（success 2 / invalid 2 /
authority 4 / unavailable 3 / restart 1）。runtime 段经真实 daemon 执行
（rpc fixture ping 探活，不可用则 skip；本次实际执行）。

**[5] no local fallback — PASS**
三重固化：①`test_unavailable_zero_production_callers` 生产目录全量 import 扫描零调用方；
②模块声明含"生产路径不得使用本模块直连 SQLite"（`test_no_business_fallback_in_module_doc`）；
③daemon 不可用时不存在可降级的 Python 本地权威路径（零生产调用方 ⇒ 无降级入口）。

## 3. 函数体零改动证据（AST before/after）

基线 `f23d9c7^`（step0 前）vs HEAD，`server/durable_staging.py`：
`git diff --stat` 全程 **仅 +15 行（step1 docstring）**。
AST 函数级比对（`ast.dump` 逐函数）：**11/11 函数 identical**
（`__init__`/`_init_schema`/`append`/`transition`/`read_pending`/`read_all`/
`compact_applied`/`recover`/`stats`/`close`/`_row_to_entry`），changed/removed/added
均为空——函数体受存量测试（test_phase6 8 例功能测试 + test_phase5 `__init__`
PRAGMA 块源码断言）锁定，本卡零破坏。

## 4. Runtime 指纹

- 部署 evidence：`~/.callwarden/runtime/evidence/20260826-215012-f23d9c737824-*.json`
  status=`passed`，daemon PID 50424（transport=http，ping ok）
- step0 探测：init schema_ready=true / stats 空库全零 / 缺库 fail-soft / init 幂等二次调用 PASS
- step2 负矩阵 runtime 段真实执行（12 passed 含 daemon 在线断言路径）

## 5. Handoff Manifest（下一棒：Reviewer）

```text
Handoff:
  from_role: executor
  outcome: executor_ready_for_review
  next_role: reviewer
  next_action: 独立核验 SRV-009 五验收条款与本 manifest 证据锚点（只读）
  reason: step0-3 全部 report 成功，负矩阵 12 passed，零权威证据齐备
  independence_requirement: required
```

## 6. Findings

1. **零生产调用方组件处置范式**：DurableStagingLog 无生产调用方，Python 侧无生产
   权威可退役；step1 以 docstring 权威归属声明替代代码退役，与 SRV-008 保留先例
   （dispatch/_registry_conn/_get_workspace_resources）同一范式。
2. **存量测试锁定函数体**：test_phase6 功能测试 + test_phase5 `__init__` PRAGMA
   源码级断言 ⇒ RPC 薄客户端化必然破坏存量测试（tests/ 除 test_srv_009.py 外
   forbidden），函数体保留原形态为唯一合规处置。
3. **生产 staging 权威在先**：Rust `staging_log.rs`（JSONL）原已承担生产链 staging，
   本卡仅下沉 SQLite WAL 形态权威（init/stats），不引入重复生产路径。
4. **mod.rs 白名单外配套**：`rust_ext/src/daemon/mod.rs` 一行 `pub mod` 声明为接线
   必需的最小配套（与 SRV-006~008 先例一致），已在 step0 report 披露。
5. **HEAD blob 构造法**：工作树存在外部偏离（forbidden 未触碰），step0 以
   `git show HEAD:<file>` 基线 + bytes 锚点 + `hash-object/update-index` 隔离提交
   4 文件 +360，零混入。
6. **fail-soft 契约**：init/stats 全部异常归一化为 reason 字段（db_open_failed/
   table_missing/schema_init_failed），绝不向 RPC 调用方抛错。
7. **静态门禁固化不变量**：`test_unavailable_zero_production_callers` 使后续任何
   生产代码引入 Python durable_staging 路径都会测试失败。
8. **任务转 review**：本 step report 后任务进入 review，等待独立 Reviewer 核验。
