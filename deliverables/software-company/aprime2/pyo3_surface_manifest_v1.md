# pyo3_surface_manifest_v1（A″-G0 step0 executor 静态清单）

**任务**：`T-1787800241077-e7fd7231` A″-G0；**step**：`S-1787800317700-b1de35c8` `inventory_all_pyo3_exports`；**snapshot**：`02cf30ebfce924b0`
**性质**：一项一行、唯一 disposition 的静态 inventory；不修改任何 production source / runtime / task state / matrix。
**输入**：`rust_ext/src/lib.rs`（只读 wrap_pyfunction! registry）、`pyo3_authority_surface_inventory_20260827.json`（162 项分类）、lib.rs 注册 cross-check（163 次 occurrence / 162 个唯一 export）。

## 1. 行结构（14 列，缺列 fail-closed）

| # | 列 | 说明 |
|---|---|---|
| 1 | `symbol` | 见 JSON rows |
| 2 | `category` | 见 JSON rows |
| 3 | `source_definition` | 见 JSON rows |
| 4 | `wrap_registration` | 见 JSON rows |
| 5 | `all_python_imports` | 见 JSON rows |
| 6 | `all_external_consumers` | 见 JSON rows |
| 7 | `current_semantics` | 见 JSON rows |
| 8 | `db_or_authority_effect` | 见 JSON rows |
| 9 | `HTTP_successor` | 见 JSON rows |
| 10 | `disposition` | 见 JSON rows |
| 11 | `required_gate` | 见 JSON rows |
| 12 | `removal_condition` | 见 JSON rows |
| 13 | `ABI_version_decision` | 见 JSON rows |
| 14 | `owner` | 见 JSON rows |

## 2. Disposition 定义

- `retain_local_core`：保留本地 core（不 HTTP 化；A″-30…34 类语义不建立实现卡）
- `replace_with_http_client`：收敛到已存在/应新增的 HTTP client successor
- `retire_after_zero_callers`：已有 successor；待 call site 归零 + 兼容窗口完成
- `requires_artifact_contract`：FD/memfd/large-payload 语义约束，不能直接替换
- `requires_separate_authority_contract`：peer credential/ACL/policy，须另建 auth contract
- `unknown_blocked`：任何 import/call-site/ABI/side-effect 不明 → 不删除

## 3. 统计

| 度量 | 值 |
|---|---|
| total rows | 162 |
| disposition `retain_local_core` | 133 |
| disposition `replace_with_http_client` | 16 |
| disposition `retire_after_zero_callers` | 5 |
| disposition `requires_artifact_contract` | 1 |
| disposition `requires_separate_authority_contract` | 7 |
| category `authority_helper_candidate` | 11 |
| category `daemon_client_candidate` | 9 |
| category `local_core_or_nontransport` | 128 |
| category `protocol_or_peercred_candidate` | 14 |

lib.rs 注册 cross-check：occurrences=163，唯一 export=162；重复仅 `cas_merge_query::cas_merge_to_codegraph`（同 fn 两次 add_function）；unmatched=[]、inventory-not-found=[]。

## 4. 全量符号清单（按 inventory category 分组）

### daemon::client（9）

| symbol | lib.rs | disposition | HTTP_successor / scope |
|---|---|---|---|
| `daemon::client::build_connect_params_py` | lib.rs L2118 | `replace_with_http_client` | HTTP workspace.connect / agent-connect 参数适配器（P0-K role/provenance contract 稳定后） |
| `daemon::client::build_publish_params_py` | lib.rs L2111 | `requires_artifact_contract` | 无 HTTP successor 在 A″-G1 冻结前（G1 后为 artifact-id-only HTTP snapshot.publish 参数，实现卡 A″-35） |
| `daemon::client::build_query_request_py` | lib.rs L2095 | `replace_with_http_client` | query helper 内 HttpDaemonRpcClient.call 参数归一化（query params / workspace_instance_id） |
| `daemon::client::build_refresh_params_py` | lib.rs L2122 | `replace_with_http_client` | HTTP workspace.refresh 参数适配器（target/path/budget 校验留在 daemon 侧） |
| `daemon::client::build_request_py` | lib.rs L2086 | `replace_with_http_client` | HttpDaemonRpcClient.call 内 canonical HTTP envelope builder（或 generated SDK）：jsonrpc/protocol_version/id/method/params |
| `daemon::client::build_rpc_request_py` | lib.rs L2106 | `replace_with_http_client` | generic client RPC serializer（保留 request_id dedup/retry 同 id 语义） |
| `daemon::client::build_simple_request_py` | lib.rs L2099 | `replace_with_http_client` | health/status/list 等 thin wrapper |
| `daemon::client::daemon_client_call_py` | lib.rs L2090 | `retire_after_zero_callers` | server/daemon_client.py::HttpDaemonRpcClient.call（HTTP JSON-RPC）；本 export 为 Unix-only UDS legacy client entry |
| `daemon::client::parse_rpc_response_py` | lib.rs L2087 | `replace_with_http_client` | HttpDaemonRpcClient._handle_response 结构化 error/result 映射 |

### protocol/peercred/dispatch（14）

| symbol | lib.rs | disposition | HTTP_successor / scope |
|---|---|---|---|
| `daemon_query::dispatch_is_admin_method` | lib.rs L1988 | `replace_with_http_client` | server-authoritative capability/property view；client display only |
| `daemon_query::dispatch_list_error_codes` | lib.rs L1984 | `replace_with_http_client` | daemon-owned error catalog / capability metadata |
| `daemon_query::dispatch_list_methods` | lib.rs L1983 | `replace_with_http_client` | daemon /capabilities 或 /v1/meta/tools canonical registry |
| `daemon_query::peercred_info` | lib.rs L1982 | `requires_separate_authority_contract` | read-only /capabilities transport-profile 字段；绝不作授权输入 |
| `daemon_query::peercred_is_available` | lib.rs L1981 | `requires_separate_authority_contract` | read-only /capabilities transport-profile 字段（或仅 obsolete diagnostics 时 retire）；绝不作授权输入 |
| `daemon_query::protocol_build_frame` | lib.rs L1966 | `retire_after_zero_callers` | 无 HTTP successor；retire 外部 export（Rust internal framing helper 若仍被 server 使用则保留） |
| `daemon_query::protocol_constants` | lib.rs L1963 | `replace_with_http_client` | HTTP /capabilities / versioned constants payload；不得把 UDS frame constants 复制到 client |
| `daemon_query::protocol_decode_payload` | lib.rs L1965 | `replace_with_http_client` | HttpDaemonRpcClient._handle_response JSON decode only |
| `daemon_query::protocol_encode_payload` | lib.rs L1964 | `replace_with_http_client` | HTTP client canonical serializer（Python json.dumps / generated SDK）；不得复制 UDS frame encoder |
| `daemon_query::protocol_make_error_response` | lib.rs L1977 | `retire_after_zero_callers` | daemon http_server.rs error emitter（非 Python client）；外部 PyO3 export retire/deprecate only |
| `daemon_query::protocol_make_ok_response` | lib.rs L1973 | `retire_after_zero_callers` | daemon http_server.rs response emitter（非 Python client）；外部 PyO3 export retire/deprecate only |
| `daemon_query::protocol_parse_header` | lib.rs L1967 | `retire_after_zero_callers` | 无 HTTP successor；retire 外部 export only（header 校验留在 Rust internal 协议测试层） |
| `daemon_query::protocol_parse_response` | lib.rs L1972 | `replace_with_http_client` | HTTP JSON-RPC result/error mapping only |
| `daemon_query::protocol_validate_message_size` | lib.rs L1968 | `replace_with_http_client` | HTTP 8MiB limit + server 413 行为；不得保留 UDS constant fallback |

### authority/health/budget（11）

| symbol | lib.rs | disposition | HTTP_successor / scope |
|---|---|---|---|
| `daemon_query::budget_create` | lib.rs L1998 | `retain_local_core` | 无 HTTP successor；保留 daemon-internal query budget 状态机（server 保留最终资源 enforcement） |
| `daemon_query::budget_preset` | lib.rs L1999 | `retain_local_core` | 无 HTTP successor；preset 值 versioned 且仅 daemon 侧生效 |
| `daemon_query::budget_tracker_new` | lib.rs L2000 | `retain_local_core` | 无 HTTP successor；不建 Python 对象代理，生命周期留在 Rust 内 |
| `daemon_query::budget_tracker_truncate_results` | lib.rs L2005 | `retain_local_core` | 无 HTTP successor；truncation/budget limit server-side |
| `daemon_query::budget_tracker_visit_node` | lib.rs L2001 | `retain_local_core` | 无 HTTP successor；visit/budget 超限行为保留 Rust internal |
| `daemon_query::check_path_within_workspace` | lib.rs L1991 | `requires_separate_authority_contract` | daemon-internal workspace-root confinement |
| `daemon_query::check_workspace_owner` | lib.rs L1997 | `requires_separate_authority_contract` | workspace authorization 在 Rust handler 内完成 |
| `daemon_query::current_daemon_uid_py` | lib.rs L1996 | `requires_separate_authority_contract` | non-authoritative daemon diagnostic 字段（独立 auth 契约下决定保留/退役） |
| `daemon_query::health_check_all` | lib.rs L2012 | `replace_with_http_client` | daemon /health canonical response；client 只 display/parse |
| `daemon_query::is_admin_uid` | lib.rs L1995 | `requires_separate_authority_contract` | dispatch/auth internal decision；client 只看到 allowed/denied |
| `daemon_query::validate_owned_path` | lib.rs L1990 | `requires_separate_authority_contract` | daemon-internal path validation；client 只得到结构化 error |

### local core / nontransport（128）

| symbol | lib.rs | disposition | HTTP_successor / scope |
|---|---|---|---|
| `backup_restore::backup_db_only` | lib.rs L2038 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `backup_restore::backup_full` | lib.rs L2037 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `backup_restore::cleanup_backups` | lib.rs L2043 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `backup_restore::delete_backup` | lib.rs L2042 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `backup_restore::list_backups` | lib.rs L2041 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `backup_restore::restore_backup` | lib.rs L2039 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `backup_restore::verify_backup` | lib.rs L2040 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `batch_build_query::batch_save_symbols` | lib.rs L1872 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `batch_calls_query::batch_resolve_and_save_calls` | lib.rs L1874 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `batch_cosine_similarity` | lib.rs L1741 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `batch_file_versions_query::batch_save_file_versions` | lib.rs L1879 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `batch_parse_c_files` | lib.rs L1714 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `batch_parse_c_files_pool` | lib.rs L1719 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `batch_parse_c_files_stream` | lib.rs L1722 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `batch_register_query::batch_register_files` | lib.rs L1884 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `build_graph_from_c_files` | lib.rs L1724 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `canonical_schema_checksum` | lib.rs L1712 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `canonicalize_source_py` | lib.rs L1802 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_merge_query::cas_merge_init_schema` | lib.rs L1870 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_merge_query::cas_merge_to_codegraph` | lib.rs L1812 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_query::cas_global_count_files` | lib.rs L1830 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_query::cas_global_get_state` | lib.rs L1829 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_query::cas_global_lookup` | lib.rs L1828 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_query::cas_local_get_file_generation` | lib.rs L1831 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_query::compute_cas_key_v1` | lib.rs L1826 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_query::compute_symbol_content_hash` | lib.rs L1827 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_write_query::cas_file_generation_committed` | lib.rs L1841 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_write_query::cas_file_generation_reset` | lib.rs L1843 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_write_query::cas_file_generation_seen` | lib.rs L1840 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_write_query::cas_file_generation_uncommit` | lib.rs L1842 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_write_query::cas_gc` | lib.rs L1844 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_write_query::cas_pin` | lib.rs L1839 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cas_write_query::cas_publish_with_retry` | lib.rs L1835 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::config::check_role_supported_py` | lib.rs L2050 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::config::config_explain_py` | lib.rs L2049 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::config::load_config_py` | lib.rs L2048 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::config::platform_paths_detect` | lib.rs L2047 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::bold_py` | lib.rs L2073 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::colorize_py` | lib.rs L2066 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::cprint_py` | lib.rs L2067 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::dim_py` | lib.rs L2072 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::error_py` | lib.rs L2069 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::format_duration_py` | lib.rs L2074 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::format_size_py` | lib.rs L2075 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::info_py` | lib.rs L2071 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::json_dumps_pretty_py` | lib.rs L2076 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::should_use_color_auto_py` | lib.rs L2065 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::should_use_color_py` | lib.rs L2064 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::success_py` | lib.rs L2068 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::output::warning_py` | lib.rs L2070 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::readonly::is_readonly_args_py` | lib.rs L2052 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::readonly::is_readonly_command_py` | lib.rs L2051 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::router::daemon_socket_path_py` | lib.rs L2059 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::router::get_daemon_mode_py` | lib.rs L2056 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::router::is_daemon_available_py` | lib.rs L2058 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::router::is_daemon_required_py` | lib.rs L2057 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::router::route_command_py` | lib.rs L2060 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `cli::stats::stats_command_run_py` | lib.rs L2080 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `clone_detection::clone_detection_params` | lib.rs L1758 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `clone_detection::py_batch_minhash_signatures` | lib.rs L1750 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `clone_detection::py_detect_clones_core` | lib.rs L1762 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `clone_detection::py_lsh_buckets` | lib.rs L1749 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `clone_detection::py_lsh_candidate_pairs` | lib.rs L1754 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `clone_detection::py_minhash_signature` | lib.rs L1748 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `core_version` | lib.rs L1716 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `daemon_query::audit_canonical_json` | lib.rs L2021 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `daemon_query::audit_compute_signature` | lib.rs L2022 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `daemon_query::backup_compute_file_sha256` | lib.rs L2026 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `daemon_query::backup_compute_meta_checksum` | lib.rs L2030 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `daemon_query::metrics_format_labels` | lib.rs L2017 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `daemon_query::metrics_percentile` | lib.rs L2016 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `frontier::compute_frontier` | lib.rs L1787 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `frontier::compute_frontier_with_budget` | lib.rs L1788 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `impact::py_cross_layer_impact` | lib.rs L1764 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `impact::py_defect_correlation` | lib.rs L1765 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `incremental_build_query::compute_and_apply_symbol_diff` | lib.rs L1954 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `incremental_build_query::load_file_result_from_db` | lib.rs L1958 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `manifest_query::manifest_count` | lib.rs L1854 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `manifest_query::manifest_get` | lib.rs L1852 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `manifest_query::manifest_init_schema` | lib.rs L1846 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `manifest_query::manifest_link_to_snapshot` | lib.rs L1848 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `manifest_query::manifest_list` | lib.rs L1853 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `manifest_query::manifest_upsert` | lib.rs L1847 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `manifest_query::manifest_verify_raw_hash` | lib.rs L1856 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `manifest_query::snapshot_get_files` | lib.rs L1855 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `metrics::compute_local_update` | lib.rs L1794 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `multi_lang::batch_parse_files_lang` | lib.rs L1728 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `multi_lang::batch_parse_files_lang_pool` | lib.rs L1729 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `multi_lang::parse_canonical_bytes_py` | lib.rs L1727 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `multi_lang::parse_diagnostics_from_fields` | lib.rs L1736 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `multi_lang::parse_file_lang` | lib.rs L1726 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `multi_lang::parse_status_from_fields` | lib.rs L1735 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `multi_lang::supported_languages` | lib.rs L1733 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_c_file` | lib.rs L1715 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_retry_log_query::parse_retry_log_append` | lib.rs L1917 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_retry_log_query::parse_retry_log_compact` | lib.rs L1945 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_retry_log_query::parse_retry_log_increment_retry` | lib.rs L1941 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_retry_log_query::parse_retry_log_mark_applied` | lib.rs L1933 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_retry_log_query::parse_retry_log_mark_exhausted` | lib.rs L1937 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_retry_log_query::parse_retry_log_next_lsn` | lib.rs L1949 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_retry_log_query::parse_retry_log_read` | lib.rs L1921 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_retry_log_query::parse_retry_log_read_pending` | lib.rs L1925 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `parse_retry_log_query::parse_retry_log_read_retryable` | lib.rs L1929 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `replicator_query::replicator_get_pending_count` | lib.rs L1861 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `sqlite_query::sqlite_migrate_schema` | lib.rs L1808 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `sqlite_query::sqlite_query_schema_version` | lib.rs L1804 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `staging_log_query::staging_log_append` | lib.rs L1889 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `staging_log_query::staging_log_compact_applied` | lib.rs L1907 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `staging_log_query::staging_log_mark_applied_batch` | lib.rs L1895 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `staging_log_query::staging_log_mark_failed` | lib.rs L1899 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `staging_log_query::staging_log_next_lsn` | lib.rs L1912 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `staging_log_query::staging_log_read` | lib.rs L1890 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `staging_log_query::staging_log_read_pending` | lib.rs L1891 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `staging_log_query::staging_log_stats` | lib.rs L1911 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `staging_log_query::staging_log_truncate` | lib.rs L1903 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `storage::storage_backup_before_migration` | lib.rs L1820 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `storage::storage_begin` | lib.rs L1816 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `storage::storage_checkpoint_py` | lib.rs L1824 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `storage::storage_commit` | lib.rs L1817 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `storage::storage_initialize_or_migrate` | lib.rs L1811 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `storage::storage_integrity_check` | lib.rs L1819 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `storage::storage_open` | lib.rs L1815 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `storage::storage_rollback` | lib.rs L1818 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `storage::storage_schema_version` | lib.rs L1810 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `toolchain::compute_toolchain_fingerprint_py` | lib.rs L1797 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `toolchain::detect_compiler_type_py` | lib.rs L1796 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `vector_topk::py_load_embeddings_from_blobs` | lib.rs L1767 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |
| `vector_topk::py_vector_topk` | lib.rs L1771 | `retain_local_core` | 无 HTTP successor（非 transport；保留本地实现） |

## 5. local-core 模块族语义（retain_local_core 依据）

- `lib`：lib.rs 顶层 #[pyfunction]（parse 热路径 / CSR graph 构建 / schema checksum / 路径规范化等本地 compute）
- `backup_restore`：本地备份/恢复生命周期（backup_full/backup_db_only/list/verify/cleanup/delete/restore）
- `batch_build_query`：批量构建写入（symbols 批量落库）
- `batch_calls_query`：批量 calls 解析与落库
- `batch_file_versions_query`：file_versions 批量落库
- `batch_register_query`：文件批量注册入库
- `cas_merge_query`：CAS 与 codegraph merge（schema init / merge-to-codegraph）
- `cas_query`：CAS 内容寻址查询（count/state/lookup/file_generation/cas_key/content_hash）
- `cas_write_query`：CAS 写路径（file_generation 状态机 / gc / pin / publish_with_retry）
- `cli::config`：CLI 配置加载/解释/role 支持/platform 路径探测
- `cli::output`：CLI 终端输出/颜色/时长/大小/JSON pretty（纯 UI utility）
- `cli::readonly`：CLI 只读命令判定 helper
- `cli::router`：CLI 路由/daemon 模式与 socket 路径探测（本地 CLI 入口决策，非 daemon transport）
- `cli::stats`：CLI stats 命令运行入口
- `clone_detection`：clone 检测纯计算（minhash/LSH/buckets/candidate pairs/core）
- `daemon_query_local`：daemon_query 内 metrics/audit/backup 纯计算 helper（Phase4-3 契约 §3.2/§3.3，非 transport/authority 面）
- `frontier`：graph frontier 纯计算（budget-aware）
- `impact`：cross-layer impact / defect correlation 纯计算
- `incremental_build_query`：增量构建 diff 计算与从 db 加载结果
- `manifest_query`：snapshot/manifest 生命周期（count/get/list/upsert/init_schema/link/verify_raw_hash/snapshot_get_files）
- `metrics`：本地 metrics 更新纯计算
- `multi_lang`：多语言 parse/status/diagnostics/supported_languages 纯计算
- `parse_retry_log_query`：parse-retry 日志表操作（append/compact/increment_retry/mark/read/next_lsn）
- `replicator_query`：replicator pending count 查询
- `sqlite_query`：SQLite schema 版本/migrate 入口（本地 storage 层）
- `staging_log_query`：staging 日志表操作（append/compact/mark/read/stats/truncate/next_lsn）
- `storage`：SQLite storage 生命周期（open/begin/commit/rollback/checkpoint/integrity_check/migrate/schema_version/backup-before-migration）
- `toolchain`：toolchain 指纹/编译器类型探测纯计算
- `vector_topk`：embedding blob 加载与 top-k 向量查询纯计算

## 6. Implementation release gates（R3 实测快照，2026-09-08，只读）

| Gate | 实测 | 满足 |
|---|---|---|
| P0-K closed | T-1787407700109-f5562c60 status=closed | 是 |
| A′ closed | T-1787293451688-c14b1e44 status=closed | 是 |
| root/route parent | T-1787203926824-9f873bfc status=in_progress | 是 |
| old S3 independent disposition | T-1787203937208-0a795c68 status=open | 否（保持 blocked） |
| matrix python_compat=0 | not rerun in step0 (G0/matrix independent verification scope) | 否（保持 blocked） |
| live/runtime convergence | not captured in step0 (runtime preflight scope) | 否（保持 blocked） |
| G0 applied | pending (this step report + review) | 否（保持 blocked） |

## 7. 未知项 / BLOCKED / fail-closed 声明

- 本清单无 `unknown_blocked` 行；所有 162 项均有唯一 disposition。
- 显式 `callwarden_core.<export>` Python 命中为 0 **不构成删除许可**：alias import、动态加载与外部 ABI consumer 需 step1 AST/import 审计 + A″-36 复核。
- FD/memfd/SCM_RIGHTS/大载荷 export（`build_publish_params_py`）标记 `requires_artifact_contract`，G1 前不授权普通 JSON-RPC 替代。
- A″-01…37 实现卡在本 G0 applied 前不得创建/领取；retain/separate-authority 项永不建立实现卡。
- 证据文件仅存在于 executor allowed_edit_scope（`deliverables/software-company/aprime2/`）；production source 未改动。
