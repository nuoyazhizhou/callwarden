# HTTP Daemon MVP 子任务拆分

## H0：HTTP MVP 契约与 capability registry
冻结 loopback-only HTTP/JSON-RPC、capability backend、错误 envelope、compatibility worker 和 M2.5 历史证据保留策略。必须新增版本化 MVP Transport Profile，显式限定 `dev_loopback_unauthenticated` 例外，并创建 H2I、H4A、H4B 的真实任务步骤。
- review @ docs/design/http-daemon-mvp-compatibility-contract.md
- sync @ docs/design/http-daemon-mvp-task-plan.md

真实任务：`T-1786590214634-9e740cdc-sub-1`。

## H1：Rust HTTP server transport
实现 Rust daemon 的动态 loopback HTTP listener、原子 manifest、health、capabilities、v1/rpc 和现有 dispatch adapter；不改变 Named Pipe/UDS 默认路径。
- 真实任务：`T-1786590214634-9e740cdc-sub-2`
- 权威合同：`RC-T-1786590214634-9e740cdc-sub-2-implementer-r1`、`RC-T-1786590214634-9e740cdc-sub-2-independent_reviewer-r1`
- 领取前置：H0 closed；只允许与 H2 并行
- implement @ rust_ext/src/daemon/http_server.rs
- wire @ rust_ext/src/daemon/server.rs
- wire @ rust_ext/src/bin/cw_daemon.rs
- test @ rust_ext/src/daemon/http_server.rs

## H2：Python HTTP client 与 endpoint discovery
实现 Python thin client、HTTP transport、manifest 发现、bounded timeout 和业务错误透传；客户端不得直连 SQLite。H1/H2 可并行，但 H2 仅能以 H0-conformant fake server 做 unit test，不得抢占真实集成验收。
- 真实任务：`T-1786590214634-9e740cdc-sub-3`
- 权威合同：`RC-T-1786590214634-9e740cdc-sub-3-implementer-r1`、`RC-T-1786590214634-9e740cdc-sub-3-independent_reviewer-r1`
- 领取前置：H0 closed；只允许与 H1 并行
- implement @ server/daemon_client.py
- implement @ config.py
- wire @ server/daemon_autostart.py
- test @ tests/test_http_daemon_client.py
- test @ tests/test_http_manifest_discovery.py

## H2I：H1/H2 真实集成门
H1、H2 关闭后，由独立 Tester 使用 fresh `cw-daemon` 和 production `DaemonClient` 做真实 HTTP round-trip；验证 manifest、health、RPC、错误、timeout、request-id dedup、重启和无 SQLite client fallback。H2I PASS 前不得启动 H3。
- test @ tests/test_http_daemon_integration.py
- evidence @ g0-reviewer-scratch/http-mvp/h2i/

真实任务：`T-1786590214634-9e740cdc-h2i`（3 steps）。

## H3：daemon-managed Python compatibility worker
实现由 Rust daemon 管理的 Python worker adapter，先恢复自举所需的代表性 Python 能力；worker 不作为外部 MCP endpoint。
- 真实任务：`T-1786590214634-9e740cdc-sub-4`
- 权威合同：`RC-T-1786590214634-9e740cdc-sub-4-implementer-r1`、`RC-T-1786590214634-9e740cdc-sub-4-independent_reviewer-r1`
- 领取前置：H2I Independent Reviewer PASS 且 Coordinator close
- implement @ server/compat_worker.py
- implement @ server/compat_registry.py
- wire @ rust_ext/src/daemon/compat_adapter.rs
- test @ tests/test_http_compat_worker.py

## H4A：MCP/CLI 核心 thin shell 与 self-bootstrap
让 MCP/CLI 的核心 self-bootstrap 工具统一走 HTTP client，保留工具名和参数契约，完成 health/workspace/task/query/代表性 compatibility 方法的真实自举验收。
- wire @ server/tools/tools_query.py
- wire @ server/tools/tools_workspace.py
- wire @ server/mcp_server.py
- wire @ cli/main.py
- test @ tests/test_http_daemon_self_bootstrap.py

真实任务：既有 `T-1786590214634-9e740cdc-sub-5`。该 ID 正式替代旧逻辑名称 H4，
从 H0 起按 H4A 执行；不得另建重复 H4A。

## H4B：237 工具 HTTP cutover
H4B 是父任务，不直接批量编码。先按 capability registry 拆分 native read、compatibility read、index-write/job、unsupported/error 和 registry/documentation 子任务；每个 child 有独立白名单、route evidence 与 Reviewer handoff。全部 237 工具都必须有 HTTP route 或明确结构化 unsupported 结果，禁止生产 MCP/CLI SQLite fallback。
- plan @ docs/design/http-daemon-mvp-role-prompts.md
- test @ tests/test_http_daemon_capability_matrix.py

真实父任务：`T-1786590214634-9e740cdc-h4b`（3 steps）。真实 children：

- `T-1786590214634-9e740cdc-h4b-native-read`（3 steps）
- `T-1786590214634-9e740cdc-h4b-compat-read`（3 steps）
- `T-1786590214634-9e740cdc-h4b-index-job`（3 steps）
- `T-1786590214634-9e740cdc-h4b-unsupported-error`（6 steps）
- `T-1786590214634-9e740cdc-h4b-registry-docs`（4 steps）
- `T-1786590214634-9e740cdc-h4b-full-matrix`（3 steps）

六个 child 的生产/测试/文档 ownership 互不重叠，并各自冻结 implementer 与
independent_reviewer Role Contract。H4A PASS/closed 前，H4B 父子任务均不得领取。

## H5：独立复审、fresh runtime 与统一部署
构建当前 Git HEAD 的 fresh daemon，归档 HTTP 自举证据，交 Independent Reviewer 复审；PASS 后再决定后续 Rust backend 迁移顺序。
- evidence @ docs/design/http-daemon-mvp-evidence.md
- test @ tests/test_http_daemon_release_acceptance.py
- handoff @ docs/design/daemon-rust-migration-ledger.md
