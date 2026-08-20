# Rust Client Convergence —— daemon RPC 协议（T01 冻结契约）

> 项目：`cw-rust-client-convergence`
> 版本：v0.1（冻结协议，任何改动须先更新本文档）
> 关联：`deliverables/software-company/tool_migration_matrix.json`（239 工具单一真相源）

本文档收录 daemon RPC 的冻结信封协议、新增 method schema、错误码表与
`workspace_instance_id` 注入约定。Python 薄壳层与 Rust daemon 都必须遵守。

---

## 1. RPC 信封协议（冻结，不改变）

`POST /v1/rpc`（loopback-only）：

```json
{
  "jsonrpc": "2.0",
  "id": "<1..128 byte non-empty string>",
  "protocol_version": "1",
  "method": "<rpc_method>",
  "params": { "...": "..." }
}
```

响应（成功）：

```json
{ "ok": true, "result": { ... } }
```

响应（失败）：

```json
{ "ok": false, "error": { "code": "E_*", "message": "...", "retryable": false } }
```

约束：
- `protocol_version` 必须为字符串 `"1"`，否则 426。
- `params` 必须是 JSON object（缺省空 object）。
- mutation 请求必须携带 `request_id`（重试幂等，daemon 侧 dedup ≥24h）。
- 所有时间戳统一 ISO 8601 UTC 秒级；daemon 内 `now_ts()` 为准（§8.8）。

## 2. workspace_instance_id 注入约定

- 除 `workspace.list` / `ping` / `health` / `schema.version` 外，**所有 RPC 必须
  显式携带 `workspace_instance_id`**。
- 权威值由 `workspace.register` 幂等返回（同 root 确定性派生）；Python 只透传，
  禁止用 cwd/本地派生兜底。
- daemon handler 侧通过 `owned_workspace(registry, peer.uid, workspace_instance_id)`
  做 owner ACL；不匹配返回 `workspace_forbidden`。
- 路径类参数一律经 `validate_owned_path`（canonicalize + owner_uid），禁止 `..` 穿越。

## 3. 新增 method schema（T02）

### 3.1 文件/构建面（fs_handlers.rs，9 个）

| method | 参数（必填 *） | 返回 | op_class |
|---|---|---|---|
| `workspace.build_graph` | workspace_instance_id*, scan_root? | `{ok: true}` | PROTECTED_MUTATION |
| `workspace.build_directory` | workspace_instance_id*, dir_path*, recursive? | `{ok: true, scanned, refreshed}` | PROTECTED_MUTATION |
| `workspace.file.read` | workspace_instance_id*, file_path*, offset?, limit? | `{file_path, content, offset, limit, total_lines}` | READ_ONLY |
| `workspace.file.grep` | workspace_instance_id*, pattern*, path?, glob?, output_mode?, head_limit? | `{matches: [...]}` | READ_ONLY |
| `workspace.file.list` | workspace_instance_id*, path?, glob? | `[ {rel_path, size, is_dir} ]` | READ_ONLY |
| `workspace.file.symbol_content` | workspace_instance_id*, file_path*, symbol_name* | `{qualified_name, content, start_line, end_line}` | READ_ONLY |
| `workspace.file.remove` | workspace_instance_id*, file_path* | `{ok: true, removed: true}` | PROTECTED_MUTATION |
| `workspace.file.health` | workspace_instance_id*, file_path* | `{file_path, exists, size, mtime, readable}` | READ_ONLY |
| `workspace.refresh_file` | （沿用既有 `workspace.file.refresh` RefreshMessage 契约） | 既有 | PROTECTED_MUTATION |

### 3.2 度量/状态面（metrics_handlers.rs，9 个）

| method | 参数 | 返回 | op_class |
|---|---|---|---|
| `query.code_health` | workspace_instance_id*, severity? | `{severity, issues: [...]}` | READ_ONLY |
| `query.metrics_summary` | workspace_instance_id* | `{symbols, calls, files, ...}` | READ_ONLY |
| `query.complexity_hotspots` | workspace_instance_id*, limit?, module_filter? | `[...]` | READ_ONLY |
| `query.coupling_analysis` | workspace_instance_id*, limit? | `[...]` | READ_ONLY |
| `query.function_metrics` | workspace_instance_id*, qualified_name* | `{...}` | READ_ONLY |
| `query.largest_functions` | workspace_instance_id*, limit?, module_filter? | `[...]` | READ_ONLY |
| `query.most_coupled_functions` | workspace_instance_id*, limit? | `[...]` | READ_ONLY |
| `query.status` | workspace_instance_id* | `{workspace_instance_id, status, schema_version, ...}` | READ_ONLY |
| `query.symbol_content_by_hash` | workspace_instance_id*, content_hash* | `{symbol_hash, content, ...}` | READ_ONLY |

### 3.3 异步长任务（job_runner.rs，18 个 → task_rpc）

| method | 参数 | 返回 | 说明 |
|---|---|---|---|
| `task.job_submit` | workspace_instance_id*, job_type*, params?, sync? | `{job_id, status}` | 提交 job（job_type ∈ semgrep_scan/semgrep_incremental/clone_detect/embed/embed_single/git_history/git_blame/codeowners/project_deps/envelope_deps/coverage/hard_dep_edges/cross_repo_deps/prune_external） |
| `task.job_cancel` | workspace_instance_id*, job_id* | `{job_id, status}` | 取消排队/运行中 job |
| `task.job_status` | workspace_instance_id*, job_id* | `{job_id, status, progress, result?}` | 既有，复用 |
| `task.wait_for_job` | workspace_instance_id*, job_id*, timeout_ms? | `{job_id, status, result}` | 既有，复用 |
| `task.list_jobs` | workspace_instance_id*, status? | `[...]` | 既有，复用 |

同步语义：`sync=true` 的 `task.job_submit` 由 daemon 内部执行完再返回（等价
同步工具）；`sync=false` 立即返回 `job_id`，客户端用 `task.wait_for_job` 轮询。

### 3.4 GC/审计/运维（admin_handlers.rs，22 个）

| method | 参数 | op_class |
|---|---|---|
| `admin.gc_archive_import` | workspace_instance_id*, archive_path* | PROTECTED_MUTATION |
| `admin.gc_archive_inspect` | workspace_instance_id*, archive_path* | READ_ONLY |
| `admin.gc_archive_list` | workspace_instance_id*, limit? | READ_ONLY |
| `admin.gc_audit_get` | workspace_instance_id*, audit_id* | READ_ONLY |
| `admin.gc_audit_list` | workspace_instance_id*, limit? | READ_ONLY |
| `admin.gc_policy_get` | workspace_instance_id* | READ_ONLY |
| `admin.gc_policy_set` | workspace_instance_id*, policy* | PROTECTED_MUTATION |
| `admin.gc_retention` | workspace_instance_id*, retention_days* | READ_ONLY |
| `admin.audit_rotate_key` | workspace_instance_id*, reason? | PROTECTED_MUTATION |
| `admin.cleanup_rule_sync_log` | workspace_instance_id*, before_ts? | PROTECTED_MUTATION |
| `admin.clear_clones` | workspace_instance_id* | PROTECTED_MUTATION |
| `admin.snapshot_compare` | workspace_instance_id*, snapshot_a*, snapshot_b* | READ_ONLY |
| `admin.metrics_get` | workspace_instance_id* | READ_ONLY |
| `admin.branch_register` | workspace_instance_id*, name*, ref_sha? | PROTECTED_MUTATION |
| `admin.branch_switch` | workspace_instance_id*, name* | PROTECTED_MUTATION |
| `admin.assignment_create` | workspace_instance_id*, task_id*, role*, agent_id*, session_id*, model_id* | PROTECTED_MUTATION |
| `admin.assignment_revoke` | workspace_instance_id*, task_id*, role*, reason? | PROTECTED_MUTATION |
| `admin.record_action_identity` | workspace_instance_id*, action_id*, identity* | GOVERNANCE_WRITE |
| `admin.register_attestation_revocation` | workspace_instance_id*, attestation_id*, mode*, reason? | GOVERNANCE_WRITE |
| `admin.record_artifact_identity` | workspace_instance_id*, artifact_id*, identity* | GOVERNANCE_WRITE |
| `admin.publish_interface` | workspace_instance_id*, interface_name*, provider* | PROTECTED_MUTATION |
| `admin.select_interface_provider` | workspace_instance_id*, interface_name*, provider* | PROTECTED_MUTATION |

### 3.5 编辑/提案/规则写面（edit_handlers.rs，21 个）

| method | 参数 | op_class |
|---|---|---|
| `edit.propose` | workspace_instance_id*, file_path*, new_content*, operation? | PROTECTED_MUTATION |
| `edit.propose_range_patch` | workspace_instance_id*, file_path*, start_line*, end_line*, new_content* | PROTECTED_MUTATION |
| `edit.propose_symbol_id_patch` | workspace_instance_id*, symbol_id*, new_content* | PROTECTED_MUTATION |
| `edit.propose_symbol_patch` | workspace_instance_id*, qualified_name*, new_content* | PROTECTED_MUTATION |
| `edit.revert` | workspace_instance_id*, edit_id* | PROTECTED_MUTATION |
| `edit.restore_all_comments` | workspace_instance_id*, file_path? | PROTECTED_MUTATION |
| `edit.restore_comment` | workspace_instance_id*, symbol_hash* | PROTECTED_MUTATION |
| `edit.record_token_savings` | workspace_instance_id*, tokens*, detail? | PROTECTED_MUTATION |
| `gate.resolve_findings` | workspace_instance_id*, gate_id*, resolution* | PROTECTED_MUTATION |
| `gate.run_check` | workspace_instance_id*, config* | PROTECTED_MUTATION |
| `rule.seed_bootstrap` | workspace_instance_id*, source* | PROTECTED_MUTATION |
| `rule.extract_candidates` | workspace_instance_id*, source? | PROTECTED_MUTATION |
| `rule.candidate_accept` | workspace_instance_id*, candidate_id* | PROTECTED_MUTATION |
| `rule.candidate_create` | workspace_instance_id*, rule* | PROTECTED_MUTATION |
| `rule.candidate_reject` | workspace_instance_id*, candidate_id*, reason? | PROTECTED_MUTATION |
| `rule.insert_agents_md_block` | workspace_instance_id*, content* | PROTECTED_MUTATION |
| `rule.sync_agents_md` | workspace_instance_id*, target_path* | PROTECTED_MUTATION |
| `guardrail.add_rule` | workspace_instance_id*, rule* | PROTECTED_MUTATION |
| `summary.generate` | workspace_instance_id*, scope?, target? | PROTECTED_MUTATION |
| `query.diff_callees` | workspace_instance_id*, symbol_a*, symbol_b* | READ_ONLY |
| `query.diff_callers` | workspace_instance_id*, symbol_a*, symbol_b* | READ_ONLY |

## 4. 错误码表（结构化 E_*，新增部分）

| code | message_key | 语义 |
|---|---|---|
| `E_TOOL_DEPRECATED` | error.tool_deprecated | 工具显式废弃，无本地回退 |
| `E_MODE_DEPRECATED` | error.mode_deprecated | local/legacy 模式仅测试可用 |
| `E_TOOL_MIGRATION_PENDING` | error.tool_migration_pending | 路由已登记，handler 未落地 |
| `E_HTTP_DAEMON_UNAVAILABLE` | error.http_daemon_unavailable | daemon 不可用，fail-closed |

全量错误码见 `rust_ext/src/daemon/error_codes.rs`（ERROR_CODE_DIRECTORY）。

## 5. 自描述接口

`GET /v1/meta/tools`（HttpServer.meta_tools）返回 239 工具路由矩阵数组，每项：
`{name, module, target_backend, rpc_method, op_class, batch, status}`。
用于监控 `backend=python_compat` 的工具清单（Q5 compat 窗口截止跟踪）。
