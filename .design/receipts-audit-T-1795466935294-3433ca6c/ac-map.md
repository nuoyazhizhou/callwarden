# ac-map — receipts 审计验收映射

- **feature**：receipts 审计：build_graph 成功/失败/转后台均落可验证审计回执
- **R 阶段**：R0 验收设计（R2 锚定后保持；若 U-02 改判须同步修订本文）
- **生成时间**：2026-11-24 15:32:19
- **依据 HEAD**：`1fbcd9bad21d58b7ed76e7e1710883213d5495cc`
- **任务**：`T-1795466935294-3433ca6c`
- **追溯**：REQ ↔ 卡片 ↔ AC ↔ task_id ↔ 证据 的唯一映射在 `task-map.md`；本文只列 AC 定义与证据采集方式

## AC 列表（6 条）

| AC | 验收陈述（可判定） | 来源 | 关联 step | 状态 |
|---|---|---|---|---|
| AC-01 | build_graph **成功**完成时，写且仅写 1 条审计回执，`ok=true`，含完整字段集（build_seq/root/scanned/inserted/unchanged/deleted/耗时/ok/error） | 目标1 + 验收"成功一条" | step 0 / step 1 | 可判定 |
| AC-02 | build_graph **失败**完成时，凡**持有可用审计库连接**的失败出口（`run_build_graph` Err / spawn 失败），写 1 条回执，`ok=false` 且带 error 字段，不得静默丢失。**偏差口径（🟡2，2026-11-24 planner 修订、reviewer 复评确认）**：审计库物理不可用（codegraph db 未配置 / 打开失败）的出口降级为 0 回执 + 源码注释说明，不伪造审计成功——`write_audit_events` 与 codegraph 同库，无库即无处落。该口径同样适用于 🔴1 修复若保留 open-失败出口（方案①）；方案②（open 提前到 begin 之前 fail-fast）下该类失败发生在 begin 前、无可审计构建，天然适用 | 目标1 + 验收"失败一条" + 🟡2 偏差裁决 | step 0 / step 1 + 整改单 T-1795513182788-09a23f38 | 可判定（含偏差口径） |
| AC-03 | build_graph **超时转后台**时，调用方立即得到 `accepted=true`；回执在后台真正完成时落 1 条（落点见 U-02） | 目标1"超时转后台" | step 0 / step 1 | 依赖 U-02 定落点 |
| AC-04 | 每条回执同步进 `audit_chain`，`audit_verify_chain(table_name="remote_write_events")` 回放后 `broken_count=0`、回执 event 与链记录一一对应 | 目标2 + C-3 | step 1 | 可判定（方案 A） |
| AC-05 | 存在按 workspace + 时间范围查回执的**读面**，复用既有审计查询、不新增 MCP 工具；对同一查询重复读结果幂等 | 目标3 + 验收"重复读幂等" | step 1 | 依赖 U-03 确认读面 |
| AC-06 | 既有审计测试（audit_chain / audit_verify_chain / write_audit_events 相关）全部不回归，枚举与签名 hash 契约不变 | 验收"既有审计测试不回归" | step 2 | 可判定 |

## 每条 AC 的证据采集方式

### AC-01（成功路径回执）
- **采集**：Rust 单测（`fs_handlers.rs` 或 `build_flight.rs` 的 `#[cfg(test)]`），构造 `run_build_graph` Ok 路径 → 断言 `write_audit_events` 新增 1 行、`detail_json` 含全部回执字段、`ok=true`，且 `remote_write_events` 链 +1。
- **命令**：`cargo test --manifest-path rust_ext/Cargo.toml <模块过滤> --lib`（过滤器一次只传一个，见规则 37）。
- **证据落盘**：`evidence/AC-01.md`（测试输出 + 回执行 JSON 片段 + commit hash）。

### AC-02（失败路径回执）
- **采集**：负向单测，注入 `run_build_graph` Err → 断言回执落 1 行、`ok=false`、error 字段非空、链 +1；另断言审计写入失败时（E7）无半成品行。**E4 类出口（审计库物理不可用）按 🟡2 偏差口径采证**：断言 0 回执 + 源码注释存在（不伪造审计成功），而非断言回执落库。
- **命令**：同 AC-01 的 cargo test，过滤器指向失败用例。
- **证据落盘**：`evidence/AC-02.md`。

### AC-03（超时转后台回执）
- **采集**：集成/单测设 `CALLWARDEN_BUILD_WAIT_MS=0` 触发 `RecvTimeoutError::Timeout` → 断言立即返回 `accepted=true`；后台完成后经 `dry_run` 轮询收敛，再查回执落 1 条（落点按 U-02）。
- **fail-closed 边界留痕（🟡3a，2026-11-24 planner 登记、reviewer 复评确认）**：超时转后台路径的 fail-closed **不可达**——客户端已收到 accepted、tx 接收端已 drop，后台审计失败退化为"回执缺失"，lastBuild 照常发布；唯一发现机制是 `build_seq` 对账。此边界必须写入 AC-03 证据与工具文档说明，不得声称该路径审计失败会阻断构建。
- **命令**：cargo test + 必要时 Python `test_build_graph_async_client.py` 的轮询契约测试（`C:\Python314\python.exe -m pytest tests/test_build_graph_async_client.py`）。
- **证据落盘**：`evidence/AC-03.md`。

### AC-04（链完整性）
- **采集**：对 AC-01/02/03 产生的 workspace，在既有测试夹具上调用 `audit_verify_chain(table_name="remote_write_events", limit=N)` → 断言 `broken_count=0`，并核对回执 event_id 与 `audit_chain.record_id` 一一对应（参照 `fs_handlers.rs:4920-4928` 的既有"event 与链一一对应"断言写法）。
- **命令**：cargo test（daemon 侧）+ MCP/HTTP RPC `test_mcp_audit_verify_chain_http_rpc.py`（回归兼验证）。
- **证据落盘**：`evidence/AC-04.md`（verify 结果 JSON + 一一对应断言输出）。

### AC-05（读面 + 幂等）
- **采集**：在既有读面 RPC 上，以 workspace_id + start_time/end_time 查询回执 → 断言返回目标回执集合；连续两次相同查询断言结果完全一致（幂等）。**须先完成 U-03**：确认既有查询是否原生支持时间范围；若不支持且不能新增工具，登记 capability gap 并上报 Planner，不得伪造 AC 通过。
- **命令**：既有审计查询的 HTTP RPC 测试路径（复用，不新增工具）。
- **证据落盘**：`evidence/AC-05.md`（两次查询结果 diff + 读面来源说明）。

### AC-06（既有测试不回归）
- **采集**：运行既有审计相关测试集合，全部通过：
  - Rust：`cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib`（规则 24：ACL/审计变更须跑完整 daemon 测试集）
  - Python：`C:\Python314\python.exe -m pytest tests/test_audit_chain.py tests/test_audit_chain_mixin.py tests/test_audit_chain_full_flow.py tests/test_audit_key_rotation.py tests/test_mcp_audit_verify_chain_http_rpc.py tests/test_phase8_audit_log.py tests/test_remote_write_governance.py`
- **注意**：cargo test 一次只接受一个位置过滤器（规则 37），上面的 `daemon::` 是上层过滤器；Python 侧按文件列举。
- **证据落盘**：`evidence/AC-06.md`（测试汇总输出 + 退出码）。

## 全局证据门禁（work_order.acceptance_checks）

- one tool/CLI link：证明回执写面经**单一** `sign_audit_chain` 入口挂链，不新增 MCP 工具。
- tests：AC-01~06 全部有对应测试输出。
- evidence manifest/hash：`evidence/manifest.json`（列每个 AC 的证据文件与 SHA-256）。
- executor_ready_for_review：全部 AC 有证据且无 open 阻塞后，executor 以该 outcome 交 reviewer。
