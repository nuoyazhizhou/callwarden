# Windows、WSL 与 Linux Daemon 共存实现任务计划

本计划由 `T-1786330576149-d2cd128c` 拆分。每个子任务必须独立实现、测试并交给
Independent Reviewer；实现 Agent 只能推进到 `review`。

## Authority/transport handshake
实现 authority_id、transport、task_db_fingerprint 的 daemon hello/ping 返回与客户端校验；冲突或缺失时 fail-closed。 @rust_ext/src/daemon/protocol.rs
- 增加 handshake 契约测试 @tests/test_daemon_authority_handshake.py
- 增加 authority 不一致和 fingerprint 不一致的拒绝测试 @tests/test_daemon_authority_handshake.py
- 更新协议文档和稳定错误码 @docs/design/windows-wsl-daemon-coexistence-contract.md

## Windows bridge MVP
实现 Windows 侧受限 bridge，将 WSL 可达的本地端点转发到当前用户 Named Pipe；bridge 不得打开 SQLite 或实现任务写逻辑。 @rust_ext/src/bin/cw_bridge.rs
- 增加 token 文件 ACL、下游 Named Pipe 和结构化错误的单元测试 @rust_ext/src/bridge
- 增加 bridge health 与 downstream authority 检查 @rust_ext/src/bin/cw_bridge.rs
- 增加 Windows bridge 进程级测试 @tests/test_windows_bridge_e2e.py

## WSL client routing
让 WSL 客户端根据 workspace authority 选择 Windows bridge 或 WSL UDS；禁止回退到 `/mnt/c` SQLite。 @server/daemon_client.py
- 增加 authority manifest 配置和路径命名空间校验 @config.py
- 增加 bridge 不可用时的 E_AUTHORITY_UNAVAILABLE 测试 @tests/test_wsl_authority_routing.py
- 增加 WSL client profile 安装/配置文档 @docs/design/windows-wsl-daemon-coexistence-contract.md

## Dual-daemon storage guard
启动时校验 Windows daemon 与 WSL daemon 的 task、registry、CAS、codegraph 和 staging 路径没有交集；冲突返回 E_AUTHORITY_STORAGE_CONFLICT。 @rust_ext/src/daemon/config.rs
- 覆盖 realpath、Windows/WSL 路径映射和 WAL/SHM 同目录冲突 @tests/test_dual_daemon_storage_guard.py
- 验证两个 authority 的 authority_id 和 database fingerprint 不相同 @tests/test_dual_daemon_storage_guard.py

## Bridge restart and request dedup
覆盖 bridge/daemon 重启、请求超时和 mutation 重试；相同 request_id 不得重复写 task_event、task_step 或 change_audit。 @server/daemon_client.py
- 增加重连后的 authority pin 校验 @tests/test_bridge_restart_dedup.py
- 增加提交结果未知时的查询再重试流程 @tests/test_bridge_restart_dedup.py

## Windows authority cross-boundary E2E
在 Windows 主机验证 Windows CLI、Windows MCP、WSL client 并发 claim/report 全部写入 Windows authority，禁止本地 fallback。 @tests/test_windows_wsl_authority_e2e.py
- 8 个并发源中只允许 1 个 claim 胜者 @tests/test_windows_wsl_authority_e2e.py
- 断言所有 task_events 位于 Windows authority 且没有 WSL DB 写入 @tests/test_windows_wsl_authority_e2e.py
- bridge、daemon 重启后验证 request_id 幂等 @tests/test_windows_wsl_authority_e2e.py

## WSL local daemon E2E
在 WSL ext4 临时根启动 Linux daemon，验证其 workspace 与 Windows authority 完全隔离。 @tests/test_wsl_local_daemon_e2e.py
- 使用 `/tmp` 或 WSL ext4 home，禁止使用 `/mnt/c` 的 DB/WAL/SHM @tests/test_wsl_local_daemon_e2e.py
- 验证 Windows daemon 不可用时 WSL authority 仍只写自己的数据库 @tests/test_wsl_local_daemon_e2e.py
- 验证两个 daemon 停止/重启互不影响 @tests/test_wsl_local_daemon_e2e.py

## Installation and operational acceptance
为 Windows bridge、WSL client、WSL local-daemon、Linux system-daemon 提供安装配置、health 检查和故障恢复验收。 @docs/design/windows-wsl-daemon-coexistence-contract.md
- 明确 `CW_AUTHORITY`、`CW_DAEMON_TRANSPORT`、`CW_DAEMON_ENDPOINT` 和 token 配置 @docs/design/windows-wsl-daemon-coexistence-contract.md
- 增加 Windows/WSL/Linux 启停与权限检查清单 @docs/design/daemon-deploy-runbook.md
- 运行 py_compile、focused tests、fresh daemon smoke 和 git diff --check @tests/test_windows_wsl_authority_e2e.py
