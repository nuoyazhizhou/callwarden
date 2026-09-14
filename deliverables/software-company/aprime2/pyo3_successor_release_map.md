# pyo3_successor_release_map（A″-G0 step2 HTTP successor / gate / microtask slot 冻结）

**任务**：`T-1787800241077-e7fd7231` A″-G0；**step**：`S-1787800317700-b1df7bf4` `freeze_http_successor_and_release_map`；**snapshot**：`02cf30ebfce924b0`
**性质**：只读静态映射；不改动任何 production source / runtime / task state / matrix。为每个 transport/authority candidate 冻结 successor、retain/blocked reason、required gate 与单-export microtask slot。
**输入**：`pyo3_surface_manifest_v1.json`（disposition/required_gate/HTTP_successor 冻结值）、step1 `pyo3_import_use_audit.md`/consumer 扫描（caller 证据）、`aprime2_pyo3_daemon_transport_microtask_breakdown_draft_20260827.md` §5-9（A″-NN 槽位表）。

## 1. 总则（与 G0 manifest / microtask breakdown 一致）

- 只有 disposition ∈ {`replace_with_http_client`, `retire_after_zero_callers`} 的 slot 在 G0 `applied` 后才具备**创建实施卡**资格；`retain_local_core` / `requires_artifact_contract` / `requires_separate_authority_contract` 一律**不创建 implementation card**（保留或另有独立契约）。
- FD/memfd / large-payload 项只能标 `requires_artifact_contract`（本表唯一成员 `build_publish_params_py`，slot A″-09 → G1 applied 后 A″-35）；禁止以普通 JSON-RPC / 增大 body 上限替代。
- retire 槽位（A″-01/13/14/17/18）的释放先决条件是**零生产 Python caller 且无外部 ABI consumer**；step1 审计已记录 caller 现状，凡仍被引用者 gate 保持 blocked。

## 2. 34 个 transport/authority candidate 的 successor / gate / slot 冻结表

| slot | symbol | disposition | HTTP successor / retain·blocked reason | required gate（G0 applied 之外） | step1 caller 证据 |
|---|---|---|---|---|---|
| A″-07 | `daemon::client::build_connect_params_py` | `replace_with_http_client` | HTTP workspace.connect / agent-connect 参数适配器（P0-K role/provenance contract 稳定后） | G0 applied 后卡槽 A″-07 才具备创建资格；A″-07 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 5）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-09 (→A″-35 after G1) | `daemon::client::build_publish_params_py` | `requires_artifact_contract` | 无 HTTP successor 在 A″-G1 冻结前（G1 后为 artifact-id-only HTTP snapshot.publish 参数，实现卡 A″-35） | A″-G1 applied（artifact/snapshot HTTP successor contract）→ 才释放卡槽 A″-09 / 后续实现卡 A″-35 | 有 consumer（生产 0 / 测试 6）（⛔ 不创建实施卡） |
| A″-04 | `daemon::client::build_query_request_py` | `replace_with_http_client` | query helper 内 HttpDaemonRpcClient.call 参数归一化（query params / workspace_instance_id） | G0 applied 后卡槽 A″-04 才具备创建资格；A″-04 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 15）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-08 | `daemon::client::build_refresh_params_py` | `replace_with_http_client` | HTTP workspace.refresh 参数适配器（target/path/budget 校验留在 daemon 侧） | G0 applied 后卡槽 A″-08 才具备创建资格；A″-08 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 10）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-02 | `daemon::client::build_request_py` | `replace_with_http_client` | HttpDaemonRpcClient.call 内 canonical HTTP envelope builder（或 generated SDK）：jsonrpc/protocol_version/id/method/params | G0 applied 后卡槽 A″-02 才具备创建资格；A″-02 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 7）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-06 | `daemon::client::build_rpc_request_py` | `replace_with_http_client` | generic client RPC serializer（保留 request_id dedup/retry 同 id 语义） | G0 applied 后卡槽 A″-06 才具备创建资格；A″-06 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 5）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-05 | `daemon::client::build_simple_request_py` | `replace_with_http_client` | health/status/list 等 thin wrapper | G0 applied 后卡槽 A″-05 才具备创建资格；A″-05 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 10）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-01 | `daemon::client::daemon_client_call_py` | `retire_after_zero_callers` | server/daemon_client.py::HttpDaemonRpcClient.call（HTTP JSON-RPC）；本 export 为 Unix-only UDS legacy client entry | G0 applied + 卡槽 A″-01 closed（证明零生产 Python caller 且无外部 ABI consumer） | **0 caller**（本审计范围）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-03 | `daemon::client::parse_rpc_response_py` | `replace_with_http_client` | HttpDaemonRpcClient._handle_response 结构化 error/result 映射 | G0 applied 后卡槽 A″-03 才具备创建资格；A″-03 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 9）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-23 | `daemon_query::dispatch_is_admin_method` | `replace_with_http_client` | server-authoritative capability/property view；client display only | G0 applied 后卡槽 A″-23 才具备创建资格；A″-23 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-22 | `daemon_query::dispatch_list_error_codes` | `replace_with_http_client` | daemon-owned error catalog / capability metadata | G0 applied 后卡槽 A″-22 才具备创建资格；A″-22 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-21 | `daemon_query::dispatch_list_methods` | `replace_with_http_client` | daemon /capabilities 或 /v1/meta/tools canonical registry | G0 applied 后卡槽 A″-21 才具备创建资格；A″-21 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-20 | `daemon_query::peercred_info` | `requires_separate_authority_contract` | read-only /capabilities transport-profile 字段；绝不作授权输入 | G0 applied + 独立 authority/transport-auth contract 评审（卡槽 A″-20 不因本 disposition 创建实现卡） | 有 consumer（生产 0 / 测试 1）（⛔ 不创建实施卡） |
| A″-19 | `daemon_query::peercred_is_available` | `requires_separate_authority_contract` | read-only /capabilities transport-profile 字段（或仅 obsolete diagnostics 时 retire）；绝不作授权输入 | G0 applied + 独立 authority/transport-auth contract 评审（卡槽 A″-19 不因本 disposition 创建实现卡） | 有 consumer（生产 0 / 测试 1）（⛔ 不创建实施卡） |
| A″-13 | `daemon_query::protocol_build_frame` | `retire_after_zero_callers` | 无 HTTP successor；retire 外部 export（Rust internal framing helper 若仍被 server 使用则保留） | G0 applied + 卡槽 A″-13 closed（证明零生产 Python caller 且无外部 ABI consumer） | 有 consumer（生产 1 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-10 | `daemon_query::protocol_constants` | `replace_with_http_client` | HTTP /capabilities / versioned constants payload；不得把 UDS frame constants 复制到 client | G0 applied 后卡槽 A″-10 才具备创建资格；A″-10 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-12 | `daemon_query::protocol_decode_payload` | `replace_with_http_client` | HttpDaemonRpcClient._handle_response JSON decode only | G0 applied 后卡槽 A″-12 才具备创建资格；A″-12 closed 后按 successor 收敛 | 有 consumer（生产 1 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-11 | `daemon_query::protocol_encode_payload` | `replace_with_http_client` | HTTP client canonical serializer（Python json.dumps / generated SDK）；不得复制 UDS frame encoder | G0 applied 后卡槽 A″-11 才具备创建资格；A″-11 closed 后按 successor 收敛 | 有 consumer（生产 1 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-18 | `daemon_query::protocol_make_error_response` | `retire_after_zero_callers` | daemon http_server.rs error emitter（非 Python client）；外部 PyO3 export retire/deprecate only | G0 applied + 卡槽 A″-18 closed（证明零生产 Python caller 且无外部 ABI consumer） | 有 consumer（生产 0 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-17 | `daemon_query::protocol_make_ok_response` | `retire_after_zero_callers` | daemon http_server.rs response emitter（非 Python client）；外部 PyO3 export retire/deprecate only | G0 applied + 卡槽 A″-17 closed（证明零生产 Python caller 且无外部 ABI consumer） | 有 consumer（生产 0 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-14 | `daemon_query::protocol_parse_header` | `retire_after_zero_callers` | 无 HTTP successor；retire 外部 export only（header 校验留在 Rust internal 协议测试层） | G0 applied + 卡槽 A″-14 closed（证明零生产 Python caller 且无外部 ABI consumer） | 有 consumer（生产 1 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-16 | `daemon_query::protocol_parse_response` | `replace_with_http_client` | HTTP JSON-RPC result/error mapping only | G0 applied 后卡槽 A″-16 才具备创建资格；A″-16 closed 后按 successor 收敛 | 有 consumer（生产 1 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-15 | `daemon_query::protocol_validate_message_size` | `replace_with_http_client` | HTTP 8MiB limit + server 413 行为；不得保留 UDS constant fallback | G0 applied 后卡槽 A″-15 才具备创建资格；A″-15 closed 后按 successor 收敛 | 有 consumer（生产 0 / 测试 1）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-30 | `daemon_query::budget_create` | `retain_local_core` | 无 HTTP successor；保留 daemon-internal query budget 状态机（server 保留最终资源 enforcement） | G0 applied（retain 声明；按 microtask A″-30…34 语义不建立实现卡） | 有 consumer（生产 1 / 测试 11）（⛔ 不创建实施卡） |
| A″-31 | `daemon_query::budget_preset` | `retain_local_core` | 无 HTTP successor；preset 值 versioned 且仅 daemon 侧生效 | G0 applied（retain 声明；按 microtask A″-30…34 语义不建立实现卡） | 有 consumer（生产 4 / 测试 5）（⛔ 不创建实施卡） |
| A″-32 | `daemon_query::budget_tracker_new` | `retain_local_core` | 无 HTTP successor；不建 Python 对象代理，生命周期留在 Rust 内 | G0 applied（retain 声明；按 microtask A″-30…34 语义不建立实现卡） | 有 consumer（生产 1 / 测试 8）（⛔ 不创建实施卡） |
| A″-34 | `daemon_query::budget_tracker_truncate_results` | `retain_local_core` | 无 HTTP successor；truncation/budget limit server-side | G0 applied（retain 声明；按 microtask A″-30…34 语义不建立实现卡） | 有 consumer（生产 1 / 测试 2）（⛔ 不创建实施卡） |
| A″-33 | `daemon_query::budget_tracker_visit_node` | `retain_local_core` | 无 HTTP successor；visit/budget 超限行为保留 Rust internal | G0 applied（retain 声明；按 microtask A″-30…34 语义不建立实现卡） | 有 consumer（生产 1 / 测试 8）（⛔ 不创建实施卡） |
| A″-25 | `daemon_query::check_path_within_workspace` | `requires_separate_authority_contract` | daemon-internal workspace-root confinement | G0 applied + 独立 authority/transport-auth contract 评审（卡槽 A″-25 不因本 disposition 创建实现卡） | 有 consumer（生产 1 / 测试 5）（⛔ 不创建实施卡） |
| A″-28 | `daemon_query::check_workspace_owner` | `requires_separate_authority_contract` | workspace authorization 在 Rust handler 内完成 | G0 applied + 独立 authority/transport-auth contract 评审（卡槽 A″-28 不因本 disposition 创建实现卡） | 有 consumer（生产 2 / 测试 4）（⛔ 不创建实施卡） |
| A″-27 | `daemon_query::current_daemon_uid_py` | `requires_separate_authority_contract` | non-authoritative daemon diagnostic 字段（独立 auth 契约下决定保留/退役） | G0 applied + 独立 authority/transport-auth contract 评审（卡槽 A″-27 不因本 disposition 创建实现卡） | 有 consumer（生产 1 / 测试 4）（⛔ 不创建实施卡） |
| A″-29 | `daemon_query::health_check_all` | `replace_with_http_client` | daemon /health canonical response；client 只 display/parse | G0 applied 后卡槽 A″-29 才具备创建资格；A″-29 closed 后按 successor 收敛 | 有 consumer（生产 1 / 测试 0）（✅ 可创建（G0 applied 后逐张释放）） |
| A″-26 | `daemon_query::is_admin_uid` | `requires_separate_authority_contract` | dispatch/auth internal decision；client 只看到 allowed/denied | G0 applied + 独立 authority/transport-auth contract 评审（卡槽 A″-26 不因本 disposition 创建实现卡） | 有 consumer（生产 1 / 测试 3）（⛔ 不创建实施卡） |
| A″-24 | `daemon_query::validate_owned_path` | `requires_separate_authority_contract` | daemon-internal path validation；client 只得到结构化 error | G0 applied + 独立 authority/transport-auth contract 评审（卡槽 A″-24 不因本 disposition 创建实现卡） | 有 consumer（生产 1 / 测试 6）（⛔ 不创建实施卡） |

## 3. 逐组备注

### daemon::client（A″-01…09）

- A″-01 `daemon_client_call_py`：唯一 Unix-only legacy UDS client entry。step1 显示其本仓库 AST caller = 0；但仍须 A″-01 closed 证明零生产 Python caller + 无外部 ABI consumer 才允许 retire（§1 fail-closed）。
- A″-02…08（build_request/parse_rpc_response/build_query/build_simple/build_rpc/build_connect/build_refresh）：HTTP successor 集中于 `server/daemon_client.py::HttpDaemonRpcClient`；step1 caller 主要在 `tests/test_phase5_2_slice*.py`（diff 对照）+ 少量生产调用。逐张依赖链见 breakdown §5（A″-02/03 → 04/05/06 → 07/08）。
- A″-09 `build_publish_params_py`：**requires_artifact_contract**；G1 前无 HTTP successor，不得提前以 JSON-RPC/FD 混合实现。

### protocol/peercred/dispatch（A″-10…23）

- A″-10/11/12/15/16（constants/encode/decode/validate_size/parse_response）：`replace_with_http_client`，successor 为 HTTP client 内 canonical JSON 序列化/解析或 `/capabilities` 常量面；**不得复制 UDS frame constants/encoder 到 client**。
- A″-13/14/17/18（build_frame/parse_header/make_ok/make_error）：`retire_after_zero_callers`。step1：`protocol_build_frame`、`protocol_parse_header` 仍有 `server/daemon_protocol.py` 生产 attr（Rust 短路默认路径）→ **gate 保持 blocked**；make_ok/make_error 仍被 `tests/test_phase4_1_daemon_protocol_diff.py` from-import → blocked。
- A″-19/20（peercred_is_available/info）：`requires_separate_authority_contract`，不创建实施卡；diagnostics 绝不作授权输入。A″-21/22/23（dispatch_*）：`replace_with_http_client`，收敛到 daemon `/capabilities` canonical registry；step1 caller 主要 tests（`test_phase4_1_daemon_protocol_diff.py`）。

### authority/health/budget（A″-24…34）

- A″-24/25/26/27/28（validate_owned_path/check_path_within_workspace/is_admin_uid/current_daemon_uid/check_workspace_owner）：`requires_separate_authority_contract`，不创建实施卡；**client 不保留最终 policy/authorization 决定**。step1 显示 server/daemon_server.py 生产调用真实存在（非 0 hit）。
- A″-29 `health_check_all`：`replace_with_http_client` → daemon `/health` canonical response。step1：server/daemon_server.py:930 生产 attr + raw 引用。
- A″-30…34（budget_*）：全部 `retain_local_core`，**不建立实施卡**（microtask §9 高概率不创建）；step1 显示 server/query_budget.py 生产调用 + tests diff。

## 4. 128 个 local-core / nontransport：全部 retain，无 slot、无实施卡

step0 冻结 128 项全部 `retain_local_core`（非 transport；不 HTTP 化）。step1 审计确认 126/128 有 consumer（生产 63 / 测试 438 AST use-site），典型生产调用见 `db/`（rust_parser_facade.py、db_build.py、db_base.py）、`server/`（backup_restore.py、staging_log.py、replicator.py、metrics.py）、`cli/`。它们**不映射到 A″-NN**；本步不改变其 disposition。

## 5. A″ release-gate 快照（与 step0 manifest meta 对齐，供 step3/reviewer）

| gate | required | step0 实测（manifest meta） | 状态 |
|---|---|---|---|
| P0-K closed | closed | T-1787407700109-f5562c60 status=closed | **met** |
| A′ closed | A′ and required descendants closed | T-1787293451688-c14b1e44 status=closed | **met** |
| root/route parent | active | T-1787203926824-9f873bfc status=in_progress | **met** |
| old S3 independent disposition | append-only disposition done | T-1787203937208-0a795c68 status=open | **blocked/未证** |
| matrix python_compat=0 | 0 | not rerun in step0 (matrix independent verification scope) | **blocked/未证** |
| live/runtime convergence | independently verified | not captured in step0 (runtime preflight scope) | **blocked/未证** |
| G0 applied | reviewer pass + adjudicator apply | pending (steps + review) | **blocked/未证** |

**结论**：G0 applied 前任何 A″-NN 实施卡都不可创建；A″-01/13/14/17/18（retire 槽）另需各自零-caller 证明，当前 step1 显示多数仍被生产/tests 引用。

## 6. 与 step0/step1 的一致性

- disposition 计数与 step0 一致：retain 133 / replace 16 / retire 5 / artifact 1 / separate-authority 7；本步仅映射 successor+gate+slot，不重算。
- caller 证据沿用 step1 审计；inventory 0-hit 不构成 retire 许可（见 step1 §2）。
- FD/memfd 语义唯一落在 A″-09（build_publish_params_py）= artifact-gated；无普通 JSON-RPC 替代授权。

## 7. 附录：slot 覆盖核对

- candidates=34；已映射 slot=34（slot A″-09 标注 →A″-35 after G1，仍计为已用）；未映射 symbol=[]；已定义 A″-01…34 未使用=[]；重复分配=[]。
