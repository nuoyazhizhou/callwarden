# 产品路线图第二阶段

## 集成测试全流程

- [x] 设计集成测试场景：init → register workspace → refresh-all → task create → task next → edit file → git commit → capture-diff --auto → check-gate → task apply → task close
- [x] 编写 tests/test_integration_full_flow.py 覆盖完整闭环
- [x] 覆盖多语言混合项目场景（Python + TS + Rust）
- [x] 验证 audit_chain 签名完整性贯穿全流程
- [x] 验证 task_symbol_changes 关联正确
- [x] 运行全量回归确保无回归（2026-07-19：6/6 通过 in 79.85s）

## 千万级符号性能验证

- [x] 准备大规模测试数据（模拟 100K / 1M / 10M 符号级别，用脚本生成）— `generate_synthetic_repo` 默认 5000×2000=10M；test_multi_scale_perf 覆盖 1K/10K/100K 三级
- [x] 测量 refresh-all 耗时随符号量增长的趋势 — 多规模阶梯测试通过；规模增长 100x 时 build 耗时增长 31x（线性偏下，无 O(n²) 退化）
- [x] 测量核心查询性能（search / call-chain / impact / clone detect）在千万级符号下的表现 — 100K 符号下：search 0.135s、call_chain_up 0.054s、call_chain_down 0.000s、blast_radius 0.011s、detect_clones 0.156s
- [x] 测量 SQLite 数据库文件大小与内存占用 — 100K 符号：db 130 MB（+WAL 2.5 MB）、RSS 92→530 MB（Rust GraphStore CSR 加载占大头）
- [x] 识别性能瓶颈（索引缺失 / 查询全表扫描 / O(n²) 算法）— 100K 规模下无明显瓶颈；detect_clones 限定 file_filter 后 O(N²) 仅在单文件 200 符号内展开（19900 对）；瓶颈识别阈值：build > 60s / stats > 1s / search > 1s / chain > 2s / clone > 5s
- [x] 输出性能报告并记录优化方向 — `_perf_10m_report.json`（单规模全量报告）+ `_perf_multi_scale_trend.json`（多规模趋势报告）；优化方向：build 阶段 parse 2.66s 占 30%，可考虑并行度提升或 AST 缓存复用；call resolve 1.74s 占 20%，可下推到 Rust CSR 短路

## B2 AST 缓存激活

- [x] 在 db_build.py 增量判断中调用 _read_ast_cache 参与决策
- [x] 实现 ast_cache 元数据驱动的跨进程增量复用
- [x] 编写测试验证 ast_cache 实际被读取并影响增量决策
- [x] 更新 architecture.md 文档说明 AST 缓存两层架构

## 统一项目健康报告

- [x] 设计 cw health-report 子命令（输出 markdown 格式报告）
- [x] 聚合 comment_coverage + uncommented + complexity + issues + coupling 为统一报告
- [x] 按模块分组输出问题清单
- [x] 支持 --output file 参数导出报告文件
- [x] 编写 tests/test_health_report.py
- [ ] 更新 cli_reference.md 文档

## MCP Server 测试

- [x] 编写 MCP Server 启动与协议握手测试
- [x] 测试 206+ MCP 工具的输入输出契约
- [x] 测试 MCP 与 CLI 并发访问（WAL 模式下读写并发安全验证）
- [x] 测试 MCP 长连接稳定性（长时间空闲后恢复）
- [x] 编写 tests/test_mcp_server_full.py（2026-07-19：15/15 通过 in 6.72s）

## B1 Clone Detection LSH 增强

- [x] 实现 MinHash + LSH 索引替代符号名前 3 字符分组剪枝
- [x] 支持跨命名克隆检测（processOrder vs handleOrder）
- [x] 优化 O(n²) 为 O(n) 近似最近邻
- [ ] 编写测试验证召回率与精确率（_feature_matrix 注：缺召回率/精确率基准测试）
- [x] 更新 architecture.md 与 cli_reference.md

## Clone Detection 影响分析联动

- [x] 修改 blast_radius / get_impact，检测到受影响符号有克隆时自动提示同步修改
- [x] 新增 get_clone_aware_impact MCP 工具
- [x] 编写测试验证联动逻辑
- [x] 更新 mcp_tools.md 文档

## 扩展 Git Hook 到 AI CLI IDE

- [x] 调研知名 AI CLI（Claude Code / Cursor / Aider / Continue / GitHub Copilot CLI）的 hook 机制
- [x] 设计 hook 适配层统一接口
- [x] 实现 Claude Code 适配
- [x] 实现 Cursor 适配
- [x] 编写测试验证适配器
- [x] 更新文档

## 15 种语言开源项目测试

- [x] 为每种语言选择代表性开源项目作为测试 fixture
- [x] 编写 tests/test_language_<lang>.py 验证符号提取与调用关系
- [x] 验证注释覆盖率统计准确性
- [x] 记录每种语言的已知限制与边界情况
- [x] 更新 docs/language_support.md 文档

## Enterprise Daemon 架构演进（基线 v10.2）

> 基于 [enterprise-daemon-shared-snapshot-plan.md](design/enterprise-daemon-shared-snapshot-plan.md) 的 9 Phase 实施路线图。
> 4 份短规范已就位：[parse-input-abi](design/parse-input-abi.md) / [cas-gc-protocol](design/cas-gc-protocol.md) / [watcher-generation-state-machine](design/watcher-generation-state-machine.md) / [daemon-ipc-security](design/daemon-ipc-security.md)。

### Phase 0: 文档与边界收敛
- [x] 明确 Python/Rust 职责边界（§5.3 禁止交叉矩阵）
- [x] 将 enterprise-daemon-shared-snapshot-plan.md 设为主设计（Baseline）
- [x] 在 roadmap 中增加 enterprise daemon epic
- [x] 标记 rust_daemon_architecture.md 已过时的描述

### Phase 1: Rust 多语言 parse 接入主 refresh 路径
- [x] 按 language 对 to_parse 分组 — db_build.py:1329 `rust_multilang_files: Dict[str, list] = defaultdict(list)` 按 lang 分组 + `non_rust_files` 收集非 Rust 支持语言
- [x] Rust 支持语言调用 batch_parse_files_lang_pool — `_rust_multilang_parse` (db_build.py:519/1469) + L6 stream mode 优先 + P30 pool fallback + P29 batch fallback
- [x] 不支持语言回退 Python parser — `non_rust_files` → `_python_multiprocess_parse` (db_build.py:1481)；小批量走 `parse_file_lang` Rust 单文件，失败 fallback Python parser (db_build.py:1513-1531)
- [x] 保留 CW_DISABLE_RUST_PARSE — db_build.py:1325 `rust_disabled = bool(os.environ.get("CW_DISABLE_RUST_PARSE"))` + db_build.py:1514 小批量路径同样检查
- [x] 增加 parse alignment smoke tests — tests/test_phase1_multilang_rust_parse.py 28 测试覆盖分组/fallback/六元组解包/normalize/CW_DISABLE_RUST_PARSE 环境变量
- [x] benchmark 验证 Python ProcessPool 退出主路径 — tests/test_phase1_parse_benchmark.py 6 测试：路径选择验证 + Rust/Python 耗时对比 smoke benchmark

### Phase 2: Daemon Skeleton + UDS + Workspace Registry
- [x] Rust daemon crate / binary — `rust_ext/src/bin/cw_daemon.rs`（clap CLI + DaemonConfig + schema 初始化 + UDS server + 4 信号 + sd_notify + 3 子命令 serve/schema-check/health-check）
- [x] UDS server + SO_PEERCRED — G3 `rust_ext/src/daemon/peercred.rs`（libc getsockopt 内核认证 UID/GID/PID）+ `server.rs` UDS server；14 用例 WSL2 全通过
- [x] workspace registry schema — G4 `rust_ext/src/daemon/workspace.rs` WorkspaceRegistry 3 个 CRUD 方法 + `db/db_daemon.py` Python 端 schema
- [x] container mount mapping — G4 dispatch.rs mount.register/list/delete RPC + Python db_daemon.py CRUD + daemon_server.py mount.* handler + CLI mount register/list/delete 子命令；35 测试通过
- [x] register/list/status API — G4 workspace.register/list/status RPC + `cli/daemon_commands.py` 对应子命令
- [x] Python CLI daemon client + enterprise/auto/local 模式 — `config.py` `get_daemon_mode`/`is_daemon_available`/`is_daemon_required`（auto/enterprise/local 三模式）+ `server/daemon_client.py` UnixDaemonRpcClient + `cli/daemon_commands.py` 完整 CLI

### Phase 3: Global CAS + Workspace Manifest
- [x] CAS schema + key 设计 — G5 `rust_ext/src/daemon/cas.rs` CasStore + `db/db_cas.py` + `docs/design/cas-gc-protocol.md` 7 参数 hash
- [x] daemon refresh CAS lookup — G34 `_daemon_parse_and_publish`（lang detect → canonicalize+hash → CAS lookup → parse → atomic publish）
- [x] clean snapshot manifest — G7 `SnapshotManager` + ArcSwap 发布 + 多 generation history + gc_generations
- [x] dirty overlay manifest — G11 Replicator CAS → Manifest → Snapshot（SnapshotCachePublisher 桥接 SnapshotCache → build_and_publish_blocking；ReplicationResult.merged_summary 填充）
- [x] CAS GC — G6 `CasStore` fs2 flock + BEGIN IMMEDIATE 双保险 + GcLockGuard RAII；5 个测试（内存模式跳过/文件模式锁创建/并发互斥/gc/gc_unreferenced）

### Phase 4-8: Snapshot Query / 秒级 Watcher / Toolchain CAS / Heavy Jobs / 生产化
> 详见 [enterprise-daemon-shared-snapshot-plan.md §14](design/enterprise-daemon-shared-snapshot-plan.md#14-实施路线图)

#### Phase 4: Snapshot Query Service
- [x] 实现 GraphSnapshot generation — G7 SnapshotManager + ArcSwap 发布 + 多 generation history
- [x] 实现 ArcSwap 原子发布 — G7 ArcSwap 原子发布（无锁读路径）
- [x] 将当前 GraphStore 演进为 snapshot manager — G7 + G28 SnapshotManagerService 完整查询（8 方法 + QueryBudget）
- [x] 支持多个 workspace 的 snapshot cache — G7 多 workspace snapshot cache + G37 跨 UID query isolation 测试
- [x] query API 全部带 workspace_instance_id — G23 EnterpriseDaemonService 11 RPC dispatch（ping/workspace.register/list/status/snapshot.publish/query.*）
- [x] 加入 query budget — G29 QueryBudget 限制（max_results + max_depth + timeout + truncate）+ default_budget() + truncate_results 所有查询统一接入
- [x] 实现函数级 diff_symbol / diff_signature — G27 DaemonClient diff_symbol/signature（5 种 diff + ScopeFilter + _ensure_remote_snapshot）
- [x] 实现 diff_callers / diff_callees — G27 + H17 MCP 已暴露 diff_callers (L3546) + diff_callees (L3574)；DaemonClient 完整实现（daemon_client.py L460/L474）
- [x] 实现小 scope compare_snapshots 同步查询 — H18 MCP 已暴露 compare_snapshots (L3602)；同步查询 + 后台 job（job_handlers.py L237/L282）+ _should_run_async 大小判断
- [x] Python MCP 查询工具改为 daemon client — G26 DaemonClient 三级路由（Rust GraphStore → Python SQL fallback）+ 8 查询方法 + routing stats（daemon_hits/sql_fallbacks）

#### Phase 5: 秒级 Watcher + Delta Replicator
- [x] 使用 Rust notify crate — `rust_ext/Cargo.toml` notify = "7.0" + `rust_ext/src/watcher.rs` notify::Watcher + Event handler
- [x] 实现 debounce 和 batch event coalescing — G9 `server/agent_watcher.py` _AgentChangeHandler watchdog 防抖 + G8 daemon_handle_refresh 两阶段 CAS
- [x] 实现 changed file hash diff — G34 canonicalize+hash（CanonicalizeResult.content_hash = sha256(canonical_bytes)）
- [x] 实现 parse delta、resolve delta — L8 增量调用图更新（`_build_call_graph_multi_lang` only_files 参数，calls 只 resolve 变化文件）
- [x] 实现 affected frontier 计算 — `rust_ext/src/frontier.rs`（CSR 图遍历计算受影响符号 frontier）
- [x] 实现局部 depth/cycle/impact 更新 — L8 增量调用图更新 + Rust CSR 短路（get_callers/get_callees 走内存索引）
- [x] 实现 Staging durable log — G12 `server/durable_staging.py`（JSONL + fsync + G30 mark_applied_batch 单次文件重写 + G31 compact_applied 按 status 过滤）
- [x] Replicator 合并 delta 并发布新 generation — G11 `rust_ext/src/daemon/replicator.rs` + `server/replicator.py`（CAS → Manifest → Snapshot）；5 个 E2E 测试

#### Phase 6: Toolchain CAS 和 Build Context
- [x] 实现 register_toolchain — G1 `cli/main.py:7706` register_toolchain + daemon_server.py toolchain.register RPC + db_toolchain.py
- [x] 实现 compiler version、target triple、sysroot、include_dirs、predefined_macros 探测 — G1 Rust ToolchainStore 1000+ 行 4 表 + Python db_toolchain.py open_toolchain_db/attach_toolchain_db/detach_toolchain_db/is_toolchain_attached
- [x] 实现 toolchain_fingerprint — `db/db_daemon.py:80` `f"{git_remote_url}|{git_head_commit_sha}|{toolchain_fingerprint}".encode()` + G1 fingerprint 去重
- [x] workspace 绑定 build context — G1 + L5 `cli/daemon_commands.py` toolchain bind 子命令 + `db/build_context.py`
- [x] resolved edges 按 build_context_hash 隔离 — G1 + L5 resolved_edges 5 级解析（exact_match/simple_name_unique/same_file/include_path/sysroot/unresolved）+ ATTACH DATABASE workspace 隔离
- [x] compile_commands.json / Makefile / Kconfig 的接入策略 — L5 compile_commands.json 解析器（`db/build_context.py` parse_compile_commands）+ build-context CLI 8 子命令 + 8 MCP 工具

#### Phase 7: Heavy Jobs 后台化
- [x] Clone detect 改为 job — G18 JobExecutor + JobContext + 3 handler（clone_detect handler 在 `server/job_handlers.py`）
- [x] MinHash/LSH 使用稳定 hash 和 shingle — H10 3-gram shingle + _MAX_BUCKET_SIZE=200 + LSH(8 bands, 16 rows) + 降级策略 + 稳定 hash
- [x] Vector indexing 改为 changed symbol 增量 job — G18 vector_embed handler（`server/job_handlers.py`）
- [x] Semgrep scan 改为 bounded external process job — G18 semgrep_scan handler（`server/job_handlers.py`）
- [x] MCP 工具返回 job_id/status/result summary — G18 JobExecutor + JobContext（job_id/status/result 字段）

#### Phase 8: 生产化
- [x] systemd unit — G9 `release/linux/deb/systemd/callwarden-agent.service`（Type=simple, MemoryMax=512M, ProtectHome=read-only, ReadWritePaths=%h/.callwarden）
- [x] config 文件和权限模板 — `server/daemon_config.py` + G25 `_validate_owned_path`（realpath + owner UID 校验 + 防路径穿越 + archived workspace 拒绝）
- [x] metrics endpoint — ✅ 已实现（CLI + MCP，非 HTTP endpoint）：`cw daemon metrics` 子命令（--format prometheus/json + --name 过滤 + --reset）+ `get_metrics` MCP 工具（206 个）；直接复用 `server/metrics.py` 的 `MetricsCollector` 单例（691 行 Counter/Gauge/Histogram + `to_prometheus()` 文本生成），不依赖 daemon RPC；13 测试通过（test_phase8_metrics_endpoint.py）
- [x] health check — G14 Rust HealthChecker（4 项检查：db_registry/disk_space/memory_usage/uptime）+ RecoveryHandler（4 步恢复：workspace_registry/cas_db/stale_jobs/snapshots）；workspace.handle_health 接入完整检查
- [x] audit log — H2 audit_chain 表 + db_audit_chain.py(491 行) + audit_verify_chain MCP + 密钥轮换
- [x] backup/restore — G16 `server/backup_restore.py` + CLI `cw daemon backup/restore` 子命令
- [x] schema migration — G15 `server/schema_migrator.py` + G8 file_generations schema + `cli/daemon_commands.py` schema-version 子命令
- [x] snapshot GC — G17 `server/snapshot_gc.py` + G32 两阶段 mark→sweep + GCPolicy（retention=3/max_age=7d/batch=1000；5 类扫描范围）
- [x] chaos tests — G37 跨 UID query isolation 测试（30 passed, 6 skipped；双 UID 隔离验证）+ `scripts/test_enterprise_daemon_dual_uid.sh`
