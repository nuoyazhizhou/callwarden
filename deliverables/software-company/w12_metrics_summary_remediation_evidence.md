# W12 证据清单：`query.metrics_summary` 越 scope 契约缺陷 remediation

- **承接卡**：`T-1789274621921-e5464ad8`（W12 承接：query.metrics_summary 越 scope 契约缺陷 remediation（方案 A 剥离））
- **step**：`S-1789274621923-e5584788`（`action=implement`，`target_file=rust_ext/src/daemon/metrics_handlers.rs`，`target_symbol=handle_metrics_summary`）
- **assignment**：`A-403210640b1eece589965435`（`status=claimed`）
- **执行身份**：`implementer-workbuddy-v1` / `cw-exec-w12-20260913` / `claude-sonnet-4.5` / `executor`（`legacy_identity_v1`）
- **时间**：2026-09-13
- **合同**：[w12_metrics_summary_remediation_contract.md](file:///c:/git_work/callwarden/deliverables/software-company/w12_metrics_summary_remediation_contract.md)

---

## 1. 变更清单（工作树 → 本卡提交）

| 文件 | +/-（numstat） | SHA-256（工作树） |
|---|---|---|
| `rust_ext/src/daemon/metrics_handlers.rs` | +44 / -46 | `28609b38ad5d564d41b04a2b3d6b105266dc31c98017b923d91a200114a30b31` |
| `rust_ext/src/daemon/query_compat_handlers.rs` | +9 / -1 | `f0ea57f2ce2e823b2a7f5562b39214cc622154478de8f7ed5ad902c516a85d36` |
| `server/tools/tools_workspace.py` | +23 / -3 | `bb1ce1289f754933bd99e1da7f40a390f1b47814765b2af2951ef86e567a64d6` |

`git diff --stat` 汇总：`3 files changed, 76 insertions(+), 50 deletions(-)`。

关键改动（与合同 §3.1 逐字一致）：

1. `metrics_handlers.rs::handle_metrics_summary` 全文替换为委托
   `super::query_compat_handlers::summary_metrics_summary(conn, workspace_id)`；
   删除在无匹配行时 `AVG(s.depth)` 返回 NULL 导致 `internal_error` 的旧实现与 `fn scalar_f64(...)`（缺陷 A）。
2. `query_compat_handlers.rs`：`summary_metrics_summary` 由 `fn` 提升为 `pub(crate) fn`
   并补 doc，明确其为 8 字段 legacy 契约的**单一真相源**（`query.metrics_summary` 与 `project_brief` 共用，缺陷 B）。
3. `tools_workspace.py`：新增 `CodeMetricsSummary(TypedDict)`（8 字段）作为
   `get_code_metrics_summary` 的显式返回注解，使 FastMCP 生成的 `outputSchema` 与 Rust 侧契约对齐并 fail-closed。

## 2. 验收命令与结果

### 2.1 `cargo test`（合同验收 ①）

```
$env:CARGO_TARGET_DIR="rust_ext/target-check-tis"
cargo test --manifest-path rust_ext/Cargo.toml --no-default-features --lib metrics_handlers
```

结果：

```
running 7 tests
test daemon::metrics_handlers::tests::test_max_result_rows_sane ... ok
test daemon::metrics_handlers::tests::test_metrics_directory_path_fail_soft ... ok
test daemon::metrics_handlers::tests::test_metrics_missing_row_and_table_fail_soft ... ok
test daemon::metrics_handlers::tests::test_metrics_rollback_flag_unset_and_other_feature_ignored ... ok
test daemon::metrics_handlers::tests::test_metrics_rollback_flag_set ... ok
test daemon::metrics_handlers::tests::test_metrics_latest_row_wins ... ok
test daemon::metrics_handlers::tests::test_metrics_summary_empty_workspace_returns_zeroed_contract ... ok

test result: ok. 7 passed; 0 failed; 0 ignored; 0 measured; 1772 filtered out; finished in 0.09s
```

修复前为 6 passed；新增 `test_metrics_summary_empty_workspace_returns_zeroed_contract`
对**空 workspace（表存在但零行）**负例断言：不得返回 `internal_error`，且须返回
8 字段全零契约（`file_count`/`function_count`/`total_lines`/`total_calls`/`avg_complexity`/
`max_complexity`/`complexity_distribution`(4 桶)/`comment_coverage`）。

### 2.2 `pytest`（合同验收 ②）

```
python -m pytest tests/test_cli_046_http_rpc.py \
  tests/test_cli_084_http_rpc.py tests/test_cli_085_http_rpc.py \
  tests/test_cli_086_http_rpc.py tests/test_cli_087_http_rpc.py \
  tests/test_cli_088_http_rpc.py -v
```

结果（隔离 daemon harness，`CW_DAEMON_BIN` 指向携带本修复的
`rust_ext/target-check-tis/debug/cw-daemon.exe`）：

```
tests\test_cli_046_http_rpc.py .                                         [  3%]
tests\test_cli_084_http_rpc.py .....                                     [ 23%]
tests\test_cli_085_http_rpc.py .....                                     [ 42%]
tests\test_cli_086_http_rpc.py .....                                     [ 61%]
tests\test_cli_087_http_rpc.py .....                                     [ 80%]
tests\test_cli_088_http_rpc.py .....                                     [100%]

============================= 26 passed in 38.17s =============================
```

**出向信封断言**（合同验收 ② 后半，证明真实 HTTP transport）：
`tests/test_cli_046_http_rpc.py::test_cli046_metrics_routes_to_daemon` 通过。该用例把
`w3_live` 隔离 daemon 的 `HttpDaemonRpcClient` 装载为 `_instance`、执行 `cw metrics`
（`main_mod._handle_metrics([], RpcDBProxy(...))`），随后断言出向请求信封：

```python
assert body.get("method") == "query.metrics_summary", (
    f"应经 HTTP 打到 query.metrics_summary：{body!r}")
```

即 `cw metrics` 的渲染链路确实经 `RpcDBProxy → HTTP POST /v1/rpc → query.metrics_summary`
命中 Rust daemon，且 8 字段契约渲染无 `KeyError`。

### 2.3 `git diff --check`（合同验收 ③）

```
$ git diff --check
（无输出）  exit 0
```

## 3. 证据产物落点

本卡 `allowed_paths` 不含 `rust_ext/target-check-tis/**`，故**原始日志仅作本地核验**，
不进入提交；下表为其内容指针，本文档即提交版证据载体。

| 原始日志（本地，未提交） | 关键结论 |
|---|---|
| `rust_ext/target-check-tis/w12_cargo_test_final.log` | `7 passed; 0 failed` |
| `rust_ext/target-check-tis/w12_pytest_final.log` | `26 passed in 38.17s` |
| `rust_ext/target-check-tis/w12_cargo_build_daemon.log` | 修复版 `cw-daemon` 构建成功 |

## 4. 权威绑定事实（供 report / handoff 复核）

- 任务 workspace binding → `workspace_capture_id=wc-4baea3ff12c2ea5c-3847560944`，
  `workspace_instance_id=4baea3ff12c2ea5c`。
- `task.report` 的 review snapshot 由 store 实际读取的 registry 校验（`TaskCollabStore.registry_db_path`
  = 启动配置注入值）。实测权威 registry = `C:\Users\wanpi\.callwarden\registry.db`
  （`daemon_workspaces` 该实例行 `snapshot_id=30fa422d8b75b549`）。
- report 使用 `snapshot_id=30fa422d8b75b549`，已被 daemon 接受（`report_request_id=req-6de772e2021f`），
  证明其与 store registry 注册值一致。
- 说明：`workspace.status` 走 `unified_workspace_authority` 的**独立**默认路径
  （`/var/lib/callwarden/registry.db`，回包 `registry_workspace_id=1193` → 该文件的注册值
  `52a28d6c4f56d16a`），与 store 的 authority 源不是同一个文件；report 门禁以 **store 实际读到的
  注册值**为准，故取 `30fa422d8b75b549`。

## 5. 纪律声明（合同 §3.2 / 验收 ⑤）

- 未触发 shared runtime 发布（未执行 `scripts/refresh_shared_runtime.ps1`）。
- 未修改 `cli/**`、`db/**`。
- 未做 direct SQLite writes；未执行 `task.apply` / `task.close` / `task.supersede`。
- 提交前缀使用本卡 `task_id`（`[T-1789274621921-e5464ad8]`），未复用
  `T-1788871227327-45c94bd8`（PYT 卡）。
