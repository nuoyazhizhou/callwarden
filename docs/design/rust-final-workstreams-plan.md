# Rust 最终迁移六个工作包

## W1 CLI 完整语义迁移

完成剩余 Rust `cw` 命令的真实语义、参数、i18n、退出码、错误码和 Python/Rust 进程差分。

## W2 Client Agent 与 daemon 闭环

完成 client/agent Slice 6/7、跨平台 transport、watcher、重连、恢复和真实多用户 E2E。

### W2.3 查询 RPC 真实接线（已完成，待独立复审）

- **路由**：`rust_ext/src/daemon/dispatch.rs` 统一 dispatch 将 `query.file` / `query.symbol` / `query.symbol_location` / `query.grep` / `query.issues` / `query.tests` 路由到 `DaemonStateExt`。
- **handler**：`rust_ext/src/daemon/snapshot_state.rs` 的 `SnapshotDaemonState` 在发布 snapshot 后真实执行五类查询（`handle_query_symbol/file/symbol_location/grep/issues/tests`），并做 workspace owner ACL 过滤。
- **Python 侧**：`server/daemon_client.py` 的 `get_symbol` / `get_symbol_location` / `get_file_symbols` / `get_file_symbol_issues` / `get_symbol_issues` / `get_symbol_tests` 等统一经 `_remote_query` 走 daemon RPC，生产路径禁止旁路 SQLite。
- **验证**：除进程内测试外，`tests/test_w2_3_query_uds_e2e.py` 在 Linux/WSL 以 fresh `cw-daemon` + Unix domain socket 实测 `workspace.register`、`publish_snapshot` 以及 W2.3 五类 `query.file` / `query.symbol` / `query.grep` / `query.issues` / `query.tests` + 扩展 `query.symbol_location` 的 round-trip 成功路径；同时验证拒绝路径：未知 workspace 拒绝（`workspace_forbidden`/`workspace_not_found`）、空 patterns `invalid_params`、未知 symbol 返回 null、越界绝对路径返回空数组（fail-closed 不泄露）、未发布 snapshot 的 workspace 返回 `snapshot_not_ready`。Linux workflow 对该测试启用 `CW_DAEMON_BIN` 与 no-skip 门禁（skipped/failed/errors 任一非零即判失败）。
- **兼容性**：`query.grep` 使用 `rg` 作为加速器；若外部程序不存在或不可执行（例如 WSL 挂载 Windows 工作区的权限场景），Rust 进入受限源码回退，不把可处理的查询失败包装成 `internal_error`。

### W2.4 Linux 多用户与恢复 E2E（已完成，待独立复审）

- **fixture**：`tests/test_process_level_e2e_recovery.py::test_two_uid_isolation` 使用两个真实 UID（19001/19002）、同 origin 的 stable/product-a 两分支、B 追加 dirty overlay、UDS/SO_PEERCRED、跨 UID 查询拒绝（workspace_forbidden）。
- **CI 入口**：`.github/workflows/e2e/run_platform_e2e.py` 新增 `run_linux_multi_user` 与 `--linux-multi-user/--daemon-bin`，在 Linux runner 以真实 UID 执行，非 Linux/非 root/缺二进制时报告 skip。
- **恢复**：`rust_ext/src/bin/cw_daemon.rs` 注册 SIGTERM/SIGINT/SIGHUP/SIGUSR1 信号；`rust_ext/src/daemon/health.rs` 提供 `RecoveryHandler`（daemon restart 后自动恢复 workspace_registry / cas_db / snapshot）；`recover_all_workspaces_with_snapshot` 统一走 `workspace.recover` 管道，避免“CAS 已恢复但 snapshot 未发布”的假成功。
- **性能**：`test_save_to_query_latency_performance_metric` 验证保存→查询 P95 时延 < 3000ms；`test_durable_log_crash_recovery_flow` 验证 crash 后 staging log pending 条目恢复。

## W3 默认切换与 rollback 窗口

为所有 service 建立版本化 rollback feature，默认走 Rust，异常 fail closed，显式 rollback 才允许兼容路径。

## W4 删除 Python fallback 与死代码

删除 parser/storage/build/query/CAS/watcher/daemon 生产 fallback，加入 import 和冻结包门禁。

## W5 发布与企业证据

完成六平台包体、SHA256、SBOM、签名、升级/回滚、schema/backup/restore 和 CI smoke 证据。

## W6 最终 parity、灾备与独立复审

完成全量差分、性能、多用户、灾备、审计、文档同步，并交由独立 Reviewer 关闭任务。
