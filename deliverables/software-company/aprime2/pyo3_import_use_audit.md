# pyo3_import_use_audit（A″-G0 step1 AST/import/use-site/ABI consumer audit）

**任务**：`T-1787800241077-e7fd7231` A″-G0；**step**：`S-1787800317700-b1df1790` `audit_imports_and_abi_consumers`；**snapshot**：`02cf30ebfce924b0`
**性质**：只读静态审计；不改动任何 production source / runtime / task state / matrix。输出仅为消费者证据与 gate 复核。
**输入**：`pyo3_surface_manifest_v1.json`（frozen disposition，162 项）；`pyo3_authority_surface_inventory_20260827.json`（category/export 名）；本步独立 AST/import/动态/外部 ABI 扫描（`_g0_use_scan_out.json`）。

## 1. 方法与范围

对 callwarden 自身可执行面（`server/ db/ cli/ analyzers/ parsers/ i18n/ bin/`）与支撑面（`tests/ scripts/ cicd/ release/ bootstrap_prep/ skills/ .github/`）做只读扫描，排除 `.git/.venv/node_modules/target/__pycache__/archive` 等第三方/构建目录；`rust_ext/src/lib.rs` 仅作只读锚点参考（不进入 consumer 计数）。四种证据：

1. **import graph（AST）**：`import callwarden_core` / `import callwarden_core as X` / `from callwarden_core import <export>`（含 `*` star-import，一旦出现即对该文件所有 export 记 fail-closed 引用）；
2. **attribute use-site（AST）**：别名后的 `<alias>.<export>`（`callwarden_core.<export>` 或 `import … as _callwarden_core` 后 `_callwarden_core.<export>`），记录 文件:行；
3. **dynamic import search**：`importlib` / `__import__` / `spec_from_file_location` / `find_spec` 对 `callwarden_core` 的调用，及 `PyInit_callwarden_core*` 显式符号引用；
4. **external ABI / 文本引用**：对 `.py/.js/.ts/.ps1/.sh/.rs` 单趟联合正则（全字匹配，非子串）捕获非 AST 面（字符串构造、脚本、动态名）的引用文件与首行。

判定：生产面（P）= `server/db/cli/analyzers/parsers/i18n/bin`；测试面（T）= `tests/`；工具/CI（X）= `.github/cicd/release/bootstrap_prep/scripts/skills`。对任何仍无法静态证明为零的外部 ABI consumer，按 **fail-closed** 记录（不授予删除/退役许可）。

## 2. 全局统计与对 inventory「0 命中」的纠偏

| 度量 | 值 |
|---|---|
| manifest rows（唯一 symbol） | 162 |
| 本审计至少 1 条 consumer 记录的 export | 159 / 162 |
| 全部 consumer 记录数（from/attr/raw 去重合并计数前） | 1034 |
| 真实 import callwarden_core 模块的 Python 文件（AST，非 raw 标记） | 103 |
| 仅文本含 callwarden_core、无 AST import 的文件 | 26 |
| dynamic import / `PyInit_callwarden_core*` 站点 | 4 |
| AST 面完全 0 consumer 的 export | 3（见 §6.3 fail-closed） |

**纠偏声明（重要）**：`pyo3_authority_surface_inventory_20260827.json` 对全部 162 项给出 `python_reference_candidates=[]` 且 `python_reference_count=0`，但其 `meta.source` 的扫描面仅为 `cli/server/cw.py/config.py` 中**显式 `callwarden_core.<export>` 点号引用**（按 inventory 自带 source 字段），并非全库。step0 manifest 的 `all_python_imports` 列复述该字段时使用的「静态命中 0（…全库扫描）」措辞随 inventory 范围而**不构成全库零引用证明**；对 inventory 零引用行，不能据此满足 `retire_after_zero_callers` 的「call site 归零」前提。本审计以独立 AST/import 扫描为准，真实结果如下。

生产面真实调用（inventory 记为 0，本审计捕获）节选：

| export | 生产 use-site |
|---|---|
| `daemon_query::validate_owned_path` | server/daemon_server.py:770(attr) |
| `daemon_query::current_daemon_uid_py` | server/daemon_server.py:235(attr) |
| `daemon_query::health_check_all` | server/daemon_server.py:930(attr) |
| `daemon_query::protocol_encode_payload` | server/daemon_protocol.py:96(attr) |
| `daemon_query::protocol_decode_payload` | server/daemon_protocol.py:111(attr) |
| `daemon_query::budget_create` | server/query_budget.py:140(attr) |
| `daemon_query::budget_preset` | server/query_budget.py:234(attr) · server/query_budget.py:251(attr) · server/query_budget.py:268(attr) · server/query_budget.py:285(attr) |
| `daemon_query::check_path_within_workspace` | server/daemon_server.py:1227(attr) |
| `daemon_query::check_workspace_owner` | server/daemon_server.py:711(attr) · server/daemon_server.py:752(attr) |
| `daemon_query::is_admin_uid` | server/daemon_server.py:816(attr) |

> 例证：`server/daemon_protocol.py` 顶部 `import callwarden_core as _callwarden_core` 并以 `hasattr(_callwarden_core, …)` + 属性调用启用 **Rust 协议短路**（缺 PyO3 才 fail-soft 到 Python path）；该文件是默认生产路径，证明 protocol/authority 类 export 并非「0 调用」。

## 3. category × consumer 汇总

| category | export 数 | 有 consumer | 生产 AST use | 测试 AST use |
|---|---|---|---|---|
| `daemon::client` | 9 | 8 | 0 | 67 |
| `protocol/peercred/dispatch` | 14 | 14 | 5 | 14 |
| `authority/health/budget` | 11 | 11 | 15 | 56 |
| `local core / nontransport` | 128 | 126 | 63 | 438 |

## 4. 34 个 transport/authority 候选的逐项 consumer 证据

以下 9(daemon::client)+14(protocol/peercred)+11(authority/health/budget)=34 个候选逐项列出 AST use-site（from-import / attr / raw-text 合并，去重到文件内首次命中行，raw 仅作文本存在性信号）。disposition 为 step0 冻结值；若某项为 `retire_after_zero_callers`，§6.2 复核其零调用前提。

### daemon::client（9）

> HTTP client successor 候选；当前 consumer 主要在 tests（diff/旧 UDS Python 对照）。

| symbol | disposition | consumer use-sites（生产/测试/工具） |
|---|---|---|
| `daemon::client::build_connect_params_py` | `replace_with_http_client` | **测试 5** tests/test_phase5_2_slice6_agent_diff.py:72(attr) · tests/test_phase5_2_slice6_agent_diff.py:83(attr) · tests/test_phase5_2_slice6_agent_diff.py:116(attr) · tests/test_phase5_2_slice6_agent_diff.py:168(attr) · tests/test_phase5_2_slice6_agent_diff.py:178(attr) |
| `daemon::client::build_publish_params_py` | `requires_artifact_contract` | **测试 6** tests/test_phase5_2_slice4_publish_diff.py:67(attr) · tests/test_phase5_2_slice4_publish_diff.py:79(attr) · tests/test_phase5_2_slice4_publish_diff.py:91(attr) · tests/test_phase5_2_slice4_publish_diff.py:103(attr) · tests/test_phase5_2_slice4_publish_diff.py:110(attr) · tests/test_phase5_2_slice4_publish_diff.py:120(attr) |
| `daemon::client::build_query_request_py` | `replace_with_http_client` | **测试 15** tests/test_phase5_2_slice2_query_diff.py:79(attr) · tests/test_phase5_2_slice2_query_diff.py:95(attr) · tests/test_phase5_2_slice2_query_diff.py:111(attr) · tests/test_phase5_2_slice2_query_diff.py:143(attr) · tests/test_phase5_2_slice2_query_diff.py:162(attr) · tests/test_phase5_2_slice2_query_diff.py:180(attr) · tests/test_phase5_2_slice2_query_diff.py:198(attr) · tests/test_phase5_2_slice2_query_diff.py:216(attr) · tests/test_phase5_2_slice2_query_diff.py:233(attr) · tests/test_phase5_2_slice2_query_diff.py:248(attr) · tests/test_phase5_2_slice2_query_diff.py:264(attr) · tests/test_phase5_2_slice2_query_diff.py:282(attr) · tests/test_phase5_2_slice2_query_diff.py:306(attr) · tests/test_phase5_2_slice2_query_diff.py:342(attr) · tests/test_phase5_2_slice2_query_diff.py:360(attr) |
| `daemon::client::build_refresh_params_py` | `replace_with_http_client` | **测试 10** tests/test_phase5_2_slice6_agent_diff.py:94(attr) · tests/test_phase5_2_slice6_agent_diff.py:107(attr) · tests/test_phase5_2_slice6_agent_diff.py:123(attr) · tests/test_phase5_2_slice6_agent_diff.py:131(attr) · tests/test_phase5_2_slice6_agent_diff.py:140(attr) · tests/test_phase5_2_slice6_agent_diff.py:149(attr) · tests/test_phase5_2_slice6_agent_diff.py:158(attr) · tests/test_phase5_2_slice6_agent_diff.py:169(attr) · tests/test_phase5_2_slice6_agent_diff.py:185(attr) · tests/test_phase5_2_slice6_agent_diff.py:192(attr) |
| `daemon::client::build_request_py` | `replace_with_http_client` | **测试 7** tests/test_phase5_2_slice1_client_diff.py:93(attr) · tests/test_phase5_2_slice1_client_diff.py:106(attr) · tests/test_phase5_2_slice1_client_diff.py:184(attr) · tests/test_phase5_2_slice1_client_diff.py:200(attr) · tests/test_phase5_2_slice1_client_diff.py:214(attr) · tests/test_phase5_2_slice1_client_diff.py:226(attr) · tests/test_phase5_2_slice1_client_diff.py:334(attr) |
| `daemon::client::build_rpc_request_py` | `replace_with_http_client` | **测试 5** tests/test_phase5_2_slice5_rpc_diff.py:86(attr) · tests/test_phase5_2_slice5_rpc_diff.py:97(attr) · tests/test_phase5_2_slice5_rpc_diff.py:104(attr) · tests/test_phase5_2_slice5_rpc_diff.py:112(attr) · tests/test_phase5_2_slice5_rpc_diff.py:140(attr) |
| `daemon::client::build_simple_request_py` | `replace_with_http_client` | **测试 10** tests/test_phase5_2_slice3_simple_diff.py:68(attr) · tests/test_phase5_2_slice3_simple_diff.py:82(attr) · tests/test_phase5_2_slice3_simple_diff.py:96(attr) · tests/test_phase5_2_slice3_simple_diff.py:110(attr) · tests/test_phase5_2_slice3_simple_diff.py:123(attr) · tests/test_phase5_2_slice3_simple_diff.py:135(attr) · tests/test_phase5_2_slice3_simple_diff.py:148(attr) · tests/test_phase5_2_slice3_simple_diff.py:163(attr) · tests/test_phase5_2_slice3_simple_diff.py:185(attr) · tests/test_phase5_2_slice3_simple_diff.py:218(attr) |
| `daemon::client::daemon_client_call_py` | `retire_after_zero_callers` | — |
| `daemon::client::parse_rpc_response_py` | `replace_with_http_client` | **测试 9** tests/test_phase5_2_slice1_client_diff.py:121(attr) · tests/test_phase5_2_slice1_client_diff.py:140(attr) · tests/test_phase5_2_slice1_client_diff.py:153(attr) · tests/test_phase5_2_slice1_client_diff.py:171(attr) · tests/test_phase5_2_slice1_client_diff.py:243(attr) · tests/test_phase5_2_slice1_client_diff.py:262(attr) · tests/test_phase5_2_slice1_client_diff.py:279(attr) · tests/test_phase5_2_slice1_client_diff.py:297(attr) · tests/test_phase5_2_slice1_client_diff.py:345(attr) |

### protocol/peercred/dispatch（14）

> Rust-shortcut 与 tests 双向消费；`_RUST_PROTOCOL_AVAILABLE` 见 server/daemon_protocol.py。

| symbol | disposition | consumer use-sites（生产/测试/工具） |
|---|---|---|
| `daemon_query::dispatch_is_admin_method` | `replace_with_http_client` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:56(raw-text) |
| `daemon_query::dispatch_list_error_codes` | `replace_with_http_client` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:55(raw-text) |
| `daemon_query::dispatch_list_methods` | `replace_with_http_client` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:54(raw-text) |
| `daemon_query::peercred_info` | `requires_separate_authority_contract` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:51(raw-text) |
| `daemon_query::peercred_is_available` | `requires_separate_authority_contract` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:50(raw-text) |
| `daemon_query::protocol_build_frame` | `retire_after_zero_callers` | **生产 1** server/daemon_protocol.py:101(attr)<br/>**测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:103(raw-text) |
| `daemon_query::protocol_constants` | `replace_with_http_client` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:104(raw-text) |
| `daemon_query::protocol_decode_payload` | `replace_with_http_client` | **生产 1** server/daemon_protocol.py:111(attr)<br/>**测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:105(raw-text) |
| `daemon_query::protocol_encode_payload` | `replace_with_http_client` | **生产 2** server/daemon_protocol.py:96(attr) · server/daemon_protocol.py:37(raw-text)<br/>**测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:106(raw-text) |
| `daemon_query::protocol_make_error_response` | `retire_after_zero_callers` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:107(raw-text) |
| `daemon_query::protocol_make_ok_response` | `retire_after_zero_callers` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:108(raw-text) |
| `daemon_query::protocol_parse_header` | `retire_after_zero_callers` | **生产 1** server/daemon_protocol.py:106(attr)<br/>**测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:109(raw-text) |
| `daemon_query::protocol_parse_response` | `replace_with_http_client` | **生产 2** server/daemon_protocol.py:116(attr) · server/daemon_protocol.py:38(raw-text)<br/>**测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:110(raw-text) |
| `daemon_query::protocol_validate_message_size` | `replace_with_http_client` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:111(raw-text) |

### authority/health/budget（11）

> authority 语义多数直接由 server 生产面调用（认证/路径/budget），见 server/daemon_server.py、server/query_budget.py。

| symbol | disposition | consumer use-sites（生产/测试/工具） |
|---|---|---|
| `daemon_query::budget_create` | `retain_local_core` | **生产 2** server/query_budget.py:140(attr) · server/query_budget.py:40(raw-text)<br/>**测试 12** tests/test_phase4_2_acl_path_budget_diff.py:238(attr) · tests/test_phase4_2_acl_path_budget_diff.py:244(attr) · tests/test_phase4_2_acl_path_budget_diff.py:280(attr) · tests/test_phase4_2_acl_path_budget_diff.py:300(attr) · tests/test_phase4_2_acl_path_budget_diff.py:308(attr) · tests/test_phase4_2_acl_path_budget_diff.py:322(attr) · tests/test_phase4_2_acl_path_budget_diff.py:333(attr) · tests/test_phase4_2_acl_path_budget_diff.py:342(attr) · tests/test_phase4_2_acl_path_budget_diff.py:351(attr) · tests/test_phase4_2_acl_path_budget_diff.py:361(attr) · tests/test_phase4_2_acl_path_budget_diff.py:373(attr) · tests/test_phase4_2_acl_path_budget_diff.py:11(raw-text) |
| `daemon_query::budget_preset` | `retain_local_core` | **生产 5** server/query_budget.py:234(attr) · server/query_budget.py:251(attr) · server/query_budget.py:268(attr) · server/query_budget.py:285(attr) · server/query_budget.py:21(raw-text)<br/>**测试 6** tests/test_phase4_2_acl_path_budget_diff.py:251(attr) · tests/test_phase4_2_acl_path_budget_diff.py:257(attr) · tests/test_phase4_2_acl_path_budget_diff.py:263(attr) · tests/test_phase4_2_acl_path_budget_diff.py:269(attr) · tests/test_phase4_2_acl_path_budget_diff.py:276(attr) · tests/test_phase4_2_acl_path_budget_diff.py:11(raw-text) |
| `daemon_query::budget_tracker_new` | `retain_local_core` | **生产 2** server/query_budget.py:147(attr) · server/query_budget.py:22(raw-text)<br/>**测试 8** tests/test_phase4_2_acl_path_budget_diff.py:301(attr) · tests/test_phase4_2_acl_path_budget_diff.py:309(attr) · tests/test_phase4_2_acl_path_budget_diff.py:323(attr) · tests/test_phase4_2_acl_path_budget_diff.py:334(attr) · tests/test_phase4_2_acl_path_budget_diff.py:343(attr) · tests/test_phase4_2_acl_path_budget_diff.py:352(attr) · tests/test_phase4_2_acl_path_budget_diff.py:363(attr) · tests/test_phase4_2_acl_path_budget_diff.py:374(attr) |
| `daemon_query::budget_tracker_truncate_results` | `retain_local_core` | **生产 2** server/query_budget.py:216(attr) · server/query_budget.py:22(raw-text)<br/>**测试 2** tests/test_phase4_2_acl_path_budget_diff.py:337(attr) · tests/test_phase4_2_acl_path_budget_diff.py:346(attr) |
| `daemon_query::budget_tracker_visit_node` | `retain_local_core` | **生产 2** server/query_budget.py:160(attr) · server/query_budget.py:22(raw-text)<br/>**测试 8** tests/test_phase4_2_acl_path_budget_diff.py:302(attr) · tests/test_phase4_2_acl_path_budget_diff.py:312(attr) · tests/test_phase4_2_acl_path_budget_diff.py:313(attr) · tests/test_phase4_2_acl_path_budget_diff.py:316(attr) · tests/test_phase4_2_acl_path_budget_diff.py:325(attr) · tests/test_phase4_2_acl_path_budget_diff.py:326(attr) · tests/test_phase4_2_acl_path_budget_diff.py:329(attr) · tests/test_phase4_2_acl_path_budget_diff.py:377(attr) |
| `daemon_query::check_path_within_workspace` | `requires_separate_authority_contract` | **生产 2** server/daemon_server.py:1227(attr) · server/daemon_server.py:66(raw-text)<br/>**测试 6** tests/test_phase4_2_acl_path_budget_diff.py:117(attr) · tests/test_phase4_2_acl_path_budget_diff.py:123(attr) · tests/test_phase4_2_acl_path_budget_diff.py:131(attr) · tests/test_phase4_2_acl_path_budget_diff.py:141(attr) · tests/test_phase4_2_acl_path_budget_diff.py:151(attr) · tests/test_phase4_2_acl_path_budget_diff.py:8(raw-text) |
| `daemon_query::check_workspace_owner` | `requires_separate_authority_contract` | **生产 3** server/daemon_server.py:711(attr) · server/daemon_server.py:752(attr) · server/daemon_server.py:67(raw-text)<br/>**测试 5** tests/test_phase4_2_acl_path_budget_diff.py:198(attr) · tests/test_phase4_2_acl_path_budget_diff.py:202(attr) · tests/test_phase4_2_acl_path_budget_diff.py:207(attr) · tests/test_phase4_2_acl_path_budget_diff.py:212(attr) · tests/test_phase4_2_acl_path_budget_diff.py:10(raw-text) |
| `daemon_query::current_daemon_uid_py` | `requires_separate_authority_contract` | **生产 2** server/daemon_server.py:235(attr) · server/daemon_server.py:66(raw-text)<br/>**测试 6** tests/test_phase8_admin_rpc_authz.py:91(from-import) · tests/test_phase4_2_acl_path_budget_diff.py:170(attr) · tests/test_phase4_2_acl_path_budget_diff.py:175(attr) · tests/test_phase4_2_acl_path_budget_diff.py:181(attr) · tests/test_phase4_2_acl_path_budget_diff.py:9(raw-text) · tests/test_phase8_admin_rpc_authz.py:89(raw-text) |
| `daemon_query::health_check_all` | `replace_with_http_client` | **生产 3** server/daemon_server.py:930(attr) · server/daemon_server.py:132(raw-text) · server/health_check.py:46(raw-text)<br/>**测试 2** tests/test_phase4_3_health_check_diff.py:1(raw-text) · tests/test_srv_010.py:8(raw-text) |
| `daemon_query::is_admin_uid` | `requires_separate_authority_contract` | **生产 2** server/daemon_server.py:816(attr) · server/daemon_server.py:66(raw-text)<br/>**测试 4** tests/test_phase4_2_acl_path_budget_diff.py:166(attr) · tests/test_phase4_2_acl_path_budget_diff.py:171(attr) · tests/test_phase4_2_acl_path_budget_diff.py:177(attr) · tests/test_phase4_2_acl_path_budget_diff.py:9(raw-text) |
| `daemon_query::validate_owned_path` | `requires_separate_authority_contract` | **生产 3** server/daemon_server.py:770(attr) · server/daemon_client.py:3261(raw-text) · server/daemon_server.py:65(raw-text)<br/>**测试 8** tests/test_phase4_2_acl_path_budget_diff.py:63(attr) · tests/test_phase4_2_acl_path_budget_diff.py:72(attr) · tests/test_phase4_2_acl_path_budget_diff.py:81(attr) · tests/test_phase4_2_acl_path_budget_diff.py:86(attr) · tests/test_phase4_2_acl_path_budget_diff.py:93(attr) · tests/test_phase4_2_acl_path_budget_diff.py:101(attr) · tests/test_k2_audit_fix.py:100(raw-text) · tests/test_phase4_2_acl_path_budget_diff.py:7(raw-text) |

## 5. local core / nontransport（128）：保留面 consumer 汇总

128 项在 step0 均冻结为 `retain_local_core`（非 transport；不建 HTTP successor、不产生实现卡）。本步对其做同一 consumer 证据收集以确认**无 transport/ABI 意外面**，并给出 top consumer 分布。

local core 生产 AST use-site=63；测试 AST use-site=438；consumer 覆盖 export=126/128。

有生产 AST use-site 的 local-core export（证明 `db/`、`server/` 等真实 import 批处理/解析/查询核心）：

| symbol | 生产 use-site（节选） |
|---|---|
| `backup_restore::backup_db_only` | server/backup_restore.py:296(attr) |
| `backup_restore::backup_full` | server/backup_restore.py:203(attr) |
| `backup_restore::cleanup_backups` | server/backup_restore.py:423(attr) |
| `backup_restore::delete_backup` | server/backup_restore.py:404(attr) |
| `backup_restore::list_backups` | server/backup_restore.py:345(attr) |
| `backup_restore::restore_backup` | server/backup_restore.py:569(attr) |
| `backup_restore::verify_backup` | server/backup_restore.py:701(attr) |
| `batch_build_query::batch_save_symbols` | db/db_build.py:3515(from-import) |
| `batch_calls_query::batch_resolve_and_save_calls` | db/db_build.py:2158(from-import) |
| `batch_cosine_similarity` | db/db_vector.py:82(attr) |
| `batch_file_versions_query::batch_save_file_versions` | db/db_build.py:3015(from-import) |
| `batch_parse_c_files` | db/db_build.py:1438(from-import) · db/rust_parser_facade.py:307(from-import) · db/rust_parser_facade.py:527(from-import) |
| `batch_parse_c_files_pool` | db/db_build.py:1414(from-import) · db/rust_parser_facade.py:302(from-import) · db/rust_parser_facade.py:518(from-import) |
| `batch_parse_c_files_stream` | db/db_build.py:1383(from-import) · db/rust_parser_facade.py:509(from-import) |
| `batch_register_query::batch_register_files` | db/db_build.py:2822(from-import) |
| `build_graph_from_c_files` | cli/main.py:10440(from-import) |
| `canonicalize_source_py` | db/rust_parser_facade.py:340(from-import) · server/agent_watcher.py:60(attr) · server/replicator.py:460(from-import) |
| `cas_write_query::cas_file_generation_reset` | server/replicator.py:229(attr) |
| `cli::stats::stats_command_run_py` | cli/main.py:9079(from-import) |
| `clone_detection::py_detect_clones_core` | db/db_clone_detection.py:339(attr) |
| `core_version` | db/rust_parser_facade.py:62(from-import) |
| `daemon_query::audit_canonical_json` | db/db_audit_chain.py:208(attr) |
| `daemon_query::audit_compute_signature` | db/db_audit_chain.py:156(attr) |
| `daemon_query::backup_compute_file_sha256` | server/backup_restore.py:494(attr) · server/backup_restore.py:813(attr) |
| `daemon_query::backup_compute_meta_checksum` | server/backup_restore.py:518(attr) · server/backup_restore.py:835(attr) |
| `daemon_query::metrics_format_labels` | server/metrics.py:815(attr) |
| `daemon_query::metrics_percentile` | server/metrics.py:422(attr) |
| `impact::py_cross_layer_impact` | db/db_impact.py:753(attr) |
| `impact::py_defect_correlation` | db/db_evolution.py:582(attr) |
| `incremental_build_query::compute_and_apply_symbol_diff` | db/db_build.py:3426(from-import) |
| `incremental_build_query::load_file_result_from_db` | db/db_build.py:1994(from-import) |
| `multi_lang::batch_parse_files_lang_pool` | db/db_build.py:529(from-import) · db/rust_parser_facade.py:476(from-import) |
| `multi_lang::parse_canonical_bytes_py` | db/rust_parser_facade.py:430(from-import) · server/replicator.py:543(from-import) |
| `multi_lang::parse_file_lang` | db/rust_parser_facade.py:390(from-import) · server/replicator.py:552(from-import) |
| `multi_lang::supported_languages` | db/db_build.py:1336(from-import) · db/rust_parser_facade.py:284(from-import) |
| `parse_c_file` | db/rust_parser_facade.py:370(from-import) |
| `replicator_query::replicator_get_pending_count` | server/replicator.py:803(attr) |
| `sqlite_query::sqlite_migrate_schema` | db/db_base.py:3728(from-import) |
| `sqlite_query::sqlite_query_schema_version` | db/db_base.py:3973(from-import) |
| `staging_log_query::staging_log_append` | server/staging_log.py:211(attr) |
| `staging_log_query::staging_log_compact_applied` | server/staging_log.py:425(attr) |
| `staging_log_query::staging_log_mark_applied_batch` | server/staging_log.py:296(attr) · server/staging_log.py:315(attr) |
| `staging_log_query::staging_log_mark_failed` | server/staging_log.py:346(attr) |
| `staging_log_query::staging_log_read` | server/staging_log.py:246(attr) |
| `staging_log_query::staging_log_read_pending` | server/staging_log.py:280(attr) |
| `staging_log_query::staging_log_stats` | server/staging_log.py:471(attr) |
| `staging_log_query::staging_log_truncate` | server/staging_log.py:388(attr) |
| `storage::storage_initialize_or_migrate` | db/db_base.py:3694(from-import) |
| `vector_topk::py_vector_topk` | db/db_vector.py:481(attr) |

### local core 逐项 consumer 覆盖（完整 128 项）

| # | symbol | P-ast | T-ast | raw | 主要 consumer 文件 |
|---|---|---|---|---|---|
| 1 | `backup_restore::backup_db_only` | 1 | 0 | 4 | server/backup_restore.py |
| 2 | `backup_restore::backup_full` | 1 | 0 | 6 | server/backup_restore.py |
| 3 | `backup_restore::cleanup_backups` | 1 | 0 | 1 | server/backup_restore.py |
| 4 | `backup_restore::delete_backup` | 1 | 0 | 4 | server/backup_restore.py |
| 5 | `backup_restore::list_backups` | 1 | 0 | 5 | server/backup_restore.py |
| 6 | `backup_restore::restore_backup` | 1 | 0 | 1 | server/backup_restore.py |
| 7 | `backup_restore::verify_backup` | 1 | 0 | 5 | server/backup_restore.py |
| 8 | `batch_build_query::batch_save_symbols` | 1 | 1 | 2 | db/db_build.py · tests/test_phase2_2_behavioral_diff.py |
| 9 | `batch_calls_query::batch_resolve_and_save_calls` | 1 | 1 | 2 | db/db_build.py · tests/test_phase2_3_behavioral_diff.py |
| 10 | `batch_cosine_similarity` | 1 | 0 | 5 | db/db_vector.py |
| 11 | `batch_file_versions_query::batch_save_file_versions` | 1 | 1 | 2 | db/db_build.py · tests/test_phase2_4_behavioral_diff.py |
| 12 | `batch_parse_c_files` | 3 | 6 | 8 | db/db_build.py · db/rust_parser_facade.py · tests/_perf_p29_rust_vs_python.py · tests/test_p29_rust_parse.py · tests/test_p30_streaming.py |
| 13 | `batch_parse_c_files_pool` | 3 | 6 | 5 | db/db_build.py · db/rust_parser_facade.py · tests/test_p30_streaming.py |
| 14 | `batch_parse_c_files_stream` | 2 | 0 | 3 | db/db_build.py · db/rust_parser_facade.py |
| 15 | `batch_register_query::batch_register_files` | 1 | 19 | 3 | db/db_build.py · tests/bench_phase2_6_3_register.py · tests/test_phase2_6_3_batch_register_diff.py |
| 16 | `build_graph_from_c_files` | 1 | 1 | 3 | cli/main.py · tests/test_f11_rust_build_graph.py |
| 17 | `canonical_schema_checksum` | 0 | 0 | 1 | — |
| 18 | `canonicalize_source_py` | 3 | 3 | 13 | db/rust_parser_facade.py · server/agent_watcher.py · server/replicator.py · tests/parser_contract/_probe_encoding.py · tests/parser_contract/test_encoding_error.py |
| 19 | `cas_merge_query::cas_merge_init_schema` | 0 | 2 | 2 | tests/test_phase2_behavioral_diff.py |
| 20 | `cas_merge_query::cas_merge_to_codegraph` | 0 | 4 | 3 | tests/test_p0_1_save_to_query_e2e.py · tests/test_phase2_behavioral_diff.py |
| 21 | `cas_query::cas_global_count_files` | 0 | 2 | 1 | tests/test_phase1_behavioral_diff.py |
| 22 | `cas_query::cas_global_get_state` | 0 | 3 | 1 | tests/test_phase1_behavioral_diff.py |
| 23 | `cas_query::cas_global_lookup` | 0 | 6 | 3 | tests/test_phase1_behavioral_diff.py · tests/test_refresh_stall_regression.py |
| 24 | `cas_query::cas_local_get_file_generation` | 0 | 2 | 1 | tests/test_phase1_behavioral_diff.py |
| 25 | `cas_query::compute_cas_key_v1` | 0 | 6 | 10 | tests/test_phase1_behavioral_diff.py |
| 26 | `cas_query::compute_symbol_content_hash` | 0 | 1 | 1 | tests/test_phase1_behavioral_diff.py |
| 27 | `cas_write_query::cas_file_generation_committed` | 0 | 0 | 2 | — |
| 28 | `cas_write_query::cas_file_generation_reset` | 1 | 0 | 1 | server/replicator.py |
| 29 | `cas_write_query::cas_file_generation_seen` | 0 | 0 | 2 | — |
| 30 | `cas_write_query::cas_file_generation_uncommit` | 0 | 0 | 1 | — |
| 31 | `cas_write_query::cas_gc` | 0 | 0 | 3 | — |
| 32 | `cas_write_query::cas_pin` | 0 | 0 | 7 | — |
| 33 | `cas_write_query::cas_publish_with_retry` | 0 | 0 | 6 | — |
| 34 | `cli::config::check_role_supported_py` | 0 | 1 | 1 | tests/test_phase5_1_cli_diff.py |
| 35 | `cli::config::config_explain_py` | 0 | 1 | 1 | tests/test_phase5_1_cli_diff.py |
| 36 | `cli::config::load_config_py` | 0 | 2 | 1 | tests/test_phase5_1_cli_diff.py |
| 37 | `cli::config::platform_paths_detect` | 0 | 1 | 1 | tests/test_phase5_1_cli_diff.py |
| 38 | `cli::output::bold_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 39 | `cli::output::colorize_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 40 | `cli::output::cprint_py` | 0 | 0 | 0 | — |
| 41 | `cli::output::dim_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 42 | `cli::output::error_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 43 | `cli::output::format_duration_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 44 | `cli::output::format_size_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 45 | `cli::output::info_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 46 | `cli::output::json_dumps_pretty_py` | 0 | 2 | 1 | tests/test_phase5_3_output_diff.py |
| 47 | `cli::output::should_use_color_auto_py` | 0 | 0 | 0 | — |
| 48 | `cli::output::should_use_color_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 49 | `cli::output::success_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 50 | `cli::output::warning_py` | 0 | 1 | 1 | tests/test_phase5_3_output_diff.py |
| 51 | `cli::readonly::is_readonly_args_py` | 0 | 1 | 1 | tests/test_phase5_1_cli_diff.py |
| 52 | `cli::readonly::is_readonly_command_py` | 0 | 1 | 1 | tests/test_phase5_1_cli_diff.py |
| 53 | `cli::router::daemon_socket_path_py` | 0 | 2 | 1 | tests/test_phase5_1b_router_diff.py |
| 54 | `cli::router::get_daemon_mode_py` | 0 | 3 | 1 | tests/test_phase5_1b_router_diff.py |
| 55 | `cli::router::is_daemon_available_py` | 0 | 1 | 1 | tests/test_phase5_1b_router_diff.py |
| 56 | `cli::router::is_daemon_required_py` | 0 | 1 | 1 | tests/test_phase5_1b_router_diff.py |
| 57 | `cli::router::route_command_py` | 0 | 1 | 1 | tests/test_phase5_1b_router_diff.py |
| 58 | `cli::stats::stats_command_run_py` | 1 | 5 | 3 | cli/main.py · tests/test_phase5_1c_stats_diff.py |
| 59 | `clone_detection::clone_detection_params` | 0 | 4 | 1 | tests/test_phase6_2_minhash_lsh_diff.py |
| 60 | `clone_detection::py_batch_minhash_signatures` | 0 | 3 | 1 | tests/test_phase6_2_minhash_lsh_diff.py |
| 61 | `clone_detection::py_detect_clones_core` | 1 | 13 | 2 | db/db_clone_detection.py · tests/test_phase6_2_detect_clones_core_diff.py |
| 62 | `clone_detection::py_lsh_buckets` | 0 | 9 | 1 | tests/test_phase6_2_minhash_lsh_diff.py |
| 63 | `clone_detection::py_lsh_candidate_pairs` | 0 | 3 | 1 | tests/test_phase6_2_minhash_lsh_diff.py |
| 64 | `clone_detection::py_minhash_signature` | 0 | 23 | 2 | tests/test_phase6_2_detect_clones_core_diff.py · tests/test_phase6_2_minhash_lsh_diff.py |
| 65 | `core_version` | 1 | 0 | 3 | db/rust_parser_facade.py |
| 66 | `daemon_query::audit_canonical_json` | 1 | 0 | 2 | db/db_audit_chain.py |
| 67 | `daemon_query::audit_compute_signature` | 1 | 0 | 2 | db/db_audit_chain.py |
| 68 | `daemon_query::backup_compute_file_sha256` | 2 | 0 | 3 | server/backup_restore.py |
| 69 | `daemon_query::backup_compute_meta_checksum` | 2 | 0 | 3 | server/backup_restore.py |
| 70 | `daemon_query::metrics_format_labels` | 1 | 0 | 2 | server/metrics.py |
| 71 | `daemon_query::metrics_percentile` | 1 | 0 | 2 | server/metrics.py |
| 72 | `frontier::compute_frontier` | 0 | 2 | 2 | tests/test_phase5_frontier.py · tests/test_phase5_metrics.py |
| 73 | `frontier::compute_frontier_with_budget` | 0 | 0 | 3 | — |
| 74 | `impact::py_cross_layer_impact` | 1 | 8 | 2 | db/db_impact.py · tests/test_phase6_1_cross_layer_impact_diff.py |
| 75 | `impact::py_defect_correlation` | 1 | 8 | 2 | db/db_evolution.py · tests/test_phase6_1_defect_correlation_diff.py |
| 76 | `incremental_build_query::compute_and_apply_symbol_diff` | 1 | 1 | 2 | db/db_build.py · tests/test_phase2_6_1_incremental_build_diff.py |
| 77 | `incremental_build_query::load_file_result_from_db` | 1 | 4 | 3 | db/db_build.py · tests/test_phase2_6_1_incremental_build_diff.py · tests/test_refresh_stall_regression.py |
| 78 | `manifest_query::manifest_count` | 0 | 4 | 3 | tests/test_phase1_behavioral_diff.py |
| 79 | `manifest_query::manifest_get` | 0 | 5 | 4 | tests/test_phase1_behavioral_diff.py |
| 80 | `manifest_query::manifest_init_schema` | 0 | 0 | 2 | — |
| 81 | `manifest_query::manifest_link_to_snapshot` | 0 | 0 | 1 | — |
| 82 | `manifest_query::manifest_list` | 0 | 5 | 3 | tests/test_phase1_behavioral_diff.py |
| 83 | `manifest_query::manifest_upsert` | 0 | 0 | 1 | — |
| 84 | `manifest_query::manifest_verify_raw_hash` | 0 | 4 | 3 | tests/test_phase1_behavioral_diff.py |
| 85 | `manifest_query::snapshot_get_files` | 0 | 3 | 3 | tests/test_phase1_behavioral_diff.py |
| 86 | `metrics::compute_local_update` | 0 | 1 | 2 | tests/test_phase5_metrics.py |
| 87 | `multi_lang::batch_parse_files_lang` | 0 | 4 | 5 | tests/_p31_validate_realworld.py · tests/test_p31_multi_lang.py |
| 88 | `multi_lang::batch_parse_files_lang_pool` | 2 | 3 | 6 | db/db_build.py · db/rust_parser_facade.py · tests/test_p31_multi_lang.py |
| 89 | `multi_lang::parse_canonical_bytes_py` | 2 | 4 | 7 | db/rust_parser_facade.py · server/replicator.py · tests/parser_contract/test_encoding_error.py · tests/test_p0_1_save_to_query_e2e.py · tests/test_p0_3_cross_platform_package.py |
| 90 | `multi_lang::parse_diagnostics_from_fields` | 0 | 0 | 1 | — |
| 91 | `multi_lang::parse_file_lang` | 2 | 50 | 22 | db/rust_parser_facade.py · server/replicator.py · tests/_discover_alignment_diffs.py · tests/_p31_validate_realworld.py · tests/parser_contract/_probe_encoding.py |
| 92 | `multi_lang::parse_status_from_fields` | 0 | 0 | 1 | — |
| 93 | `multi_lang::supported_languages` | 2 | 8 | 13 | db/db_build.py · db/rust_parser_facade.py · tests/_p31_validate_realworld.py · tests/parser_contract/generate_baseline.py · tests/test_l9_rust_multilang.py |
| 94 | `parse_c_file` | 1 | 24 | 12 | db/rust_parser_facade.py · tests/parser_contract/_probe_encoding.py · tests/parser_contract/generate_baseline.py · tests/parser_contract/test_encoding_error.py · tests/parser_contract/test_identity_range.py |
| 95 | `parse_retry_log_query::parse_retry_log_append` | 0 | 17 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 96 | `parse_retry_log_query::parse_retry_log_compact` | 0 | 1 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 97 | `parse_retry_log_query::parse_retry_log_increment_retry` | 0 | 5 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 98 | `parse_retry_log_query::parse_retry_log_mark_applied` | 0 | 2 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 99 | `parse_retry_log_query::parse_retry_log_mark_exhausted` | 0 | 1 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 100 | `parse_retry_log_query::parse_retry_log_next_lsn` | 0 | 1 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 101 | `parse_retry_log_query::parse_retry_log_read` | 0 | 6 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 102 | `parse_retry_log_query::parse_retry_log_read_pending` | 0 | 3 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 103 | `parse_retry_log_query::parse_retry_log_read_retryable` | 0 | 2 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 104 | `replicator_query::replicator_get_pending_count` | 1 | 15 | 3 | server/replicator.py · tests/test_phase1_behavioral_diff.py · tests/test_phase1_replicator_snapshot_verify.py |
| 105 | `sqlite_query::sqlite_migrate_schema` | 1 | 0 | 1 | db/db_base.py |
| 106 | `sqlite_query::sqlite_query_schema_version` | 1 | 18 | 3 | db/db_base.py · tests/test_phase1_behavioral_diff.py · tests/test_phase1_sqlite_verify.py |
| 107 | `staging_log_query::staging_log_append` | 1 | 19 | 3 | server/staging_log.py · tests/test_phase3_4_behavioral_diff.py · tests/test_process_level_e2e_recovery.py |
| 108 | `staging_log_query::staging_log_compact_applied` | 1 | 2 | 2 | server/staging_log.py · tests/test_phase3_4_behavioral_diff.py |
| 109 | `staging_log_query::staging_log_mark_applied_batch` | 2 | 5 | 3 | server/staging_log.py · tests/test_phase3_4_behavioral_diff.py · tests/test_process_level_e2e_recovery.py |
| 110 | `staging_log_query::staging_log_mark_failed` | 1 | 1 | 2 | server/staging_log.py · tests/test_phase3_4_behavioral_diff.py |
| 111 | `staging_log_query::staging_log_next_lsn` | 0 | 1 | 1 | tests/test_phase3_4_behavioral_diff.py |
| 112 | `staging_log_query::staging_log_read` | 1 | 7 | 2 | server/staging_log.py · tests/test_phase3_4_behavioral_diff.py |
| 113 | `staging_log_query::staging_log_read_pending` | 1 | 4 | 3 | server/staging_log.py · tests/test_phase3_4_behavioral_diff.py · tests/test_process_level_e2e_recovery.py |
| 114 | `staging_log_query::staging_log_stats` | 1 | 1 | 2 | server/staging_log.py · tests/test_phase3_4_behavioral_diff.py |
| 115 | `staging_log_query::staging_log_truncate` | 1 | 1 | 2 | server/staging_log.py · tests/test_phase3_4_behavioral_diff.py |
| 116 | `storage::storage_backup_before_migration` | 0 | 1 | 1 | tests/test_rust_storage_service.py |
| 117 | `storage::storage_begin` | 0 | 1 | 1 | tests/test_rust_storage_service.py |
| 118 | `storage::storage_checkpoint_py` | 0 | 1 | 1 | tests/test_rust_storage_service.py |
| 119 | `storage::storage_commit` | 0 | 1 | 1 | tests/test_rust_storage_service.py |
| 120 | `storage::storage_initialize_or_migrate` | 1 | 6 | 3 | db/db_base.py · tests/test_rust_storage_service.py · tests/test_schema_checksum_regression.py |
| 121 | `storage::storage_integrity_check` | 0 | 1 | 1 | tests/test_rust_storage_service.py |
| 122 | `storage::storage_open` | 0 | 1 | 1 | tests/test_rust_storage_service.py |
| 123 | `storage::storage_rollback` | 0 | 1 | 1 | tests/test_rust_storage_service.py |
| 124 | `storage::storage_schema_version` | 0 | 1 | 1 | tests/test_rust_storage_service.py |
| 125 | `toolchain::compute_toolchain_fingerprint_py` | 0 | 1 | 1 | tests/test_phase6_toolchain_rust.py |
| 126 | `toolchain::detect_compiler_type_py` | 0 | 1 | 1 | tests/test_phase6_toolchain_rust.py |
| 127 | `vector_topk::py_load_embeddings_from_blobs` | 0 | 4 | 1 | tests/test_phase6_3_vector_topk_diff.py |
| 128 | `vector_topk::py_vector_topk` | 1 | 17 | 2 | db/db_vector.py · tests/test_phase6_3_vector_topk_diff.py |

## 6. dynamic import / external ABI / fail-closed

### 6.1 dynamic loader 与 `PyInit_callwarden_core*`

| 文件 | 行 | 信号 |
|---|---|---|
| `release/inspect_pyinstaller_bundle.py` | 455 | `spec_from_file_location(             "callwarden_core", core_path         )         if spec is None or spec.lo` |
| `.github/workflows/e2e/run_platform_e2e.py` | 147 | `spec_from_file_location("callwarden_core", core_path)     if spec is None or spec.loader is None:         rais` |
| `.github/workflows/e2e/run_platform_e2e.py` | 145 | `PyInit_callwarden_core` |
| `.github/workflows/e2e/run_platform_e2e.py` | 146 | `PyInit_callwarden_core` |

解释：`release/inspect_pyinstaller_bundle.py` 与 `.github/workflows/e2e/run_platform_e2e.py` 通过 `spec_from_file_location` / `PyInit_callwarden_core` 探测 PyO3 扩展加载 —— 这是**打包/CI 侧对扩展入口的 ABI 级依赖**，不是 Python 属性调用。它们不改变现有 retain/replace disposition，但证明扩展 ABI 边界（`PyInit_callwarden_core`、模块对象符号）存在外部消费者，任何 export 的**移除**都不得破坏该加载面。

### 6.2 `retire_after_zero_callers`（5 项）零调用前提复核

| symbol | consumer 证据 | 零调用前提复核 |
|---|---|---|
| `daemon::client::daemon_client_call_py` | — | 仓库内部 AST 面为 0 → 仅内部前提可满足；外部 ABI 无法静态归零，退役仍须 §6.3 fail-closed + 兼容窗口 |
| `daemon_query::protocol_build_frame` | **生产 1** server/daemon_protocol.py:101(attr)<br/>**测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:103(raw-text) | **不满足**（仍有 AST/文本引用 → gate 保持 blocked） |
| `daemon_query::protocol_make_error_response` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:107(raw-text) | **不满足**（仍有 AST/文本引用 → gate 保持 blocked） |
| `daemon_query::protocol_make_ok_response` | **测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:108(raw-text) | **不满足**（仍有 AST/文本引用 → gate 保持 blocked） |
| `daemon_query::protocol_parse_header` | **生产 1** server/daemon_protocol.py:106(attr)<br/>**测试 2** tests/test_phase4_1_daemon_protocol_diff.py:97(from-import) · tests/test_phase4_1_daemon_protocol_diff.py:109(raw-text) | **不满足**（仍有 AST/文本引用 → gate 保持 blocked） |

### 6.3 AST 面完全 0 consumer 的 export（fail-closed）

下列 export 在本审计范围（callwarden 自身 + CI/scripts + tests）无任何 AST use-site；但不等于「无任何 consumer」——外部插件、动态字符串、非仓库调用方无法被静态证明为零。**fail-closed：不得删除/退役；如需处置须走 A″ 独立复核（reviewer/adjudicator）或外部 ABI 搜索扩展。**

- `cli::output::cprint_py`（disposition `retain_local_core`）
- `cli::output::should_use_color_auto_py`（disposition `retain_local_core`）
- `daemon::client::daemon_client_call_py`（disposition `retire_after_zero_callers`）

## 7. A″ release-gate 快照（供 §R3/reviewer 复核）

| gate | step0/step1 记录 | 本步证据状态 |
|---|---|---|
| A′ status / python_compat count / runtime convergence | manifest meta + role contract acceptance | 本审计不修改；保留给 step2 successor map 引用 |
| `retire_after_zero_callers` 门禁 | §6.2 逐项复核 | 5 项中仅 AST 面 0 consumer 项可满足前提；其余保持 blocked（tests/server 仍引用） |
| `requires_artifact_contract`（FD/memfd/large payload） | manifest 1 项（build_publish_params_py） | 本审计未见其生产 Python 调用（tests 有对照）；保持 artifact gate，不授权 JSON-RPC 替换 |
| unknown / 外部 ABI | §6.1 + §6.3 | 扩展加载面、外部插件调用方无法静态归零 → fail-closed |

## 8. 与 step0 manifest 的一致性

- step0 的 162 项、唯一 symbol、disposition 计数保持不变；本审计不重算 disposition。
- step0 manifest `all_python_imports` 列复述 inventory「显式静态命中 0」在 inventory 自身范围（`cli/server/cw.py/config.py` 点号引用）内为真，但**不能外推为全库零引用**；§2 已纠偏并给出真实 use-site。
- 若 step2/3 或 reviewer 需要把任何 `replace_with_http_client` / `retire_after_zero_callers` 转成实现卡，本审计表中该 export 的 consumer use-sites 即为迁移/清理清单起点。

## 9. 附录：import 模块的文件清单

真实 AST import `callwarden_core` 的 Python 文件（103 个）：

- `cli/main.py`
- `db/db_audit_chain.py`
- `db/db_base.py`
- `db/db_build.py`
- `db/db_clone_detection.py`
- `db/db_evolution.py`
- `db/db_impact.py`
- `db/db_vector.py`
- `db/db_workspace_manifest.py`
- `db/rust_parser_facade.py`
- `server/agent_watcher.py`
- `server/backup_restore.py`
- `server/daemon_protocol.py`
- `server/daemon_server.py`
- `server/metrics.py`
- `server/query_budget.py`
- `server/replicator.py`
- `server/snapshot_manager.py`
- `server/staging_log.py`
- `server/watcher.py`
- `tests/_bench_10m_progressive.py`
- `tests/_bench_graph_store_large.py`
- `tests/_bench_graphstore.py`
- `tests/_bench_linux_e2e.py`
- `tests/_bench_unified_v3.py`
- `tests/_discover_alignment_diffs.py`
- `tests/_p31_validate_realworld.py`
- `tests/_perf_p29_rust_vs_python.py`
- `tests/bench_phase2_6_3_register.py`
- `tests/parser_contract/_probe_encoding.py`
- `tests/parser_contract/_probe_null_abi.py`
- `tests/parser_contract/_probe_qname.py`
- `tests/parser_contract/_probe_sig.py`
- `tests/parser_contract/gate_report.py`
- `tests/parser_contract/generate_baseline.py`
- `tests/parser_contract/test_baseline.py`
- `tests/parser_contract/test_encoding_error.py`
- `tests/parser_contract/test_identity_range.py`
- `tests/test_b_graph_store.py`
- `tests/test_b_p7b_integration.py`
- `tests/test_b_p7b_mcp_concurrency.py`
- `tests/test_f11_rust_build_graph.py`
- `tests/test_graphstore_staged_loading.py`
- `tests/test_integration_phase3_8.py`
- `tests/test_l9_rust_multilang.py`
- `tests/test_migration_manifest.py`
- `tests/test_p0_1_save_to_query_e2e.py`
- `tests/test_p0_3_cross_platform_package.py`
- `tests/test_p28b_qualified_name.py`
- `tests/test_p29_rust_parse.py`
- `tests/test_p30_streaming.py`
- `tests/test_p31_multi_lang.py`
- `tests/test_p32_typedef.py`
- `tests/test_phase1_behavioral_diff.py`
- `tests/test_phase1_parse_benchmark.py`
- `tests/test_phase1_replicator_snapshot_verify.py`
- `tests/test_phase1_sqlite_verify.py`
- `tests/test_phase2_2_behavioral_diff.py`
- `tests/test_phase2_3_behavioral_diff.py`
- `tests/test_phase2_4_behavioral_diff.py`
- `tests/test_phase2_5_behavioral_diff.py`
- `tests/test_phase2_6_1_incremental_build_diff.py`
- `tests/test_phase2_6_3_batch_register_diff.py`
- `tests/test_phase2_behavioral_diff.py`
- `tests/test_phase3_4_behavioral_diff.py`
- `tests/test_phase4_1_daemon_protocol_diff.py`
- `tests/test_phase4_2_acl_path_budget_diff.py`
- `tests/test_phase4_compare_snapshots.py`
- `tests/test_phase4_daemon_client.py`
- `tests/test_phase4_diff.py`
- `tests/test_phase4_diff_callers_callees.py`
- `tests/test_phase4_snapshot.py`
- `tests/test_phase5_1_cli_diff.py`
- `tests/test_phase5_1b_router_diff.py`
- `tests/test_phase5_1c_stats_diff.py`
- `tests/test_phase5_2_slice1_client_diff.py`
- `tests/test_phase5_2_slice2_query_diff.py`
- `tests/test_phase5_2_slice3_simple_diff.py`
- `tests/test_phase5_2_slice4_publish_diff.py`
- `tests/test_phase5_2_slice5_rpc_diff.py`
- `tests/test_phase5_2_slice6_agent_diff.py`
- `tests/test_phase5_3_output_diff.py`
- `tests/test_phase5_canonicalize.py`
- `tests/test_phase5_debounce.py`
- `tests/test_phase5_delta.py`
- `tests/test_phase5_frontier.py`
- `tests/test_phase5_hash_diff.py`
- `tests/test_phase5_metrics.py`
- `tests/test_phase5_watcher.py`
- `tests/test_phase6_1_blast_radius_diff.py`
- `tests/test_phase6_1_cross_layer_impact_diff.py`
- `tests/test_phase6_1_defect_correlation_diff.py`
- `tests/test_phase6_2_detect_clones_core_diff.py`
- `tests/test_phase6_2_minhash_lsh_diff.py`
- `tests/test_phase6_3_vector_topk_diff.py`
- `tests/test_phase6_3_wire_production_e2e.py`
- `tests/test_phase6_toolchain_rust.py`
- `tests/test_phase8_admin_rpc_authz.py`
- `tests/test_process_level_e2e_recovery.py`
- `tests/test_refresh_stall_regression.py`
- `tests/test_rust_python_alignment.py`
- `tests/test_rust_storage_service.py`
- `tests/test_schema_checksum_regression.py`

仅文本出现、无 AST import 的文件（26 个）：

- `.github/workflows/e2e/run_platform_e2e.py`
- `db/db_query.py`
- `db/schema.py`
- `release/build.py`
- `release/inspect_pyinstaller_bundle.py`
- `release/verify_upgrade_rollback_supply_chain.py`
- `server/health_check.py`
- `tests/test_c3_cas_facade_diff.py`
- `tests/test_c4_manifest_refresh_diff.py`
- `tests/test_c5_s4_backup_restore_unify.py`
- `tests/test_cli_062_http_rpc.py`
- `tests/test_enterprise_daemon_uds.py`
- `tests/test_graphstore_compact_indexes.py`
- `tests/test_p0_3_release_packaging.py`
- `tests/test_p1_f_frozen_strict_mode.py`
- `tests/test_phase1_manifest_production.py`
- `tests/test_phase1_multilang_rust_parse.py`
- `tests/test_phase4_3_health_check_diff.py`
- `tests/test_phase4_3_metrics_audit_backup_diff.py`
- `tests/test_phase4_multi_workspace.py`
- `tests/test_phase4_query_api.py`
- `tests/test_phase4_query_budget.py`
- `tests/test_phase4_snapshot_service.py`
- `tests/test_release_bundle_inspector.py`
- `tests/test_rust_only_parser_boundary.py`
- `tests/test_srv_010.py`
