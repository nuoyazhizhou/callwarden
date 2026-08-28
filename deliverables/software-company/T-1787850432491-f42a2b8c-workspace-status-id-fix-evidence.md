# T-1787850432491-f42a2b8c workspace status 参数修复证据

## 变更

- `cli/daemon_commands.py`：`cw daemon status` 的数字参数按 `workspace_id` 发送；非数字参数按 `workspace_instance_id` 发送。
- `rust_ext/src/daemon/workspace.rs`：`workspace.status` 同时支持且严格区分两种标识，并对数字主键执行同样的 owner/archived ACL 校验。
- `tests/test_cw_client_rpc_proxy.py`：覆盖数字主键和 instance id 的客户端请求映射。
- `docs/cli_reference.md`、`TOOLS.md`：补充参数契约和示例。

## 验证

执行命令：

```text
tokenslim run python -m pytest tests/test_cw_client_rpc_proxy.py -q
tokenslim run python -m py_compile cli/daemon_commands.py
tokenslim run cargo test --manifest-path rust_ext/Cargo.toml workspace::tests::test_dispatch_workspace_status --quiet
tokenslim run git diff --check
```

结果：Python 代理测试 14 passed；Rust workspace status 测试 2 passed；语法检查和 diff 检查通过。

## 部署与真实 daemon round-trip

部署命令：

```text
.\scripts\refresh_shared_runtime.ps1 -TaskId T-1787850432491-f42a2b8c -Configuration release
```

部署证据：`C:\Users\wanpi\.callwarden\runtime\evidence\20260828-080006-fcf4652c2e38-442872cc.json`

部署后 daemon ping 成功，PID 为 `52456`，task DB fingerprint 为
`475f79ccbb41bebd39875ec4183df7244adb23747dff20f41219548665b2c9e2`。

真实查询结果：

- `cw daemon status 731` 成功，返回 `workspace_instance_id=4baea3ff12c2ea5c`。
- `cw daemon status 4baea3ff12c2ea5c` 成功，返回同一 workspace。
- `cw daemon status 1` 按数字 `workspace_id=1` 查询并返回 `workspace_not_found: 1`；这是该主键不存在的业务结果，已不再错误地按 instance id 查询。

## 已知环境限制

提交前按项目要求执行刷新时，当前 daemon 未提供 `build_full_graph` RPC，`cw --refresh-all` 返回
`method_not_found`。未使用 SQL 或其他旁路；本次 release runtime refresh 已成功完成。
