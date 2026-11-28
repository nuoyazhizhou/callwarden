# daemon 资源治理面 — 符号锚定（R2）

- **feature**: daemon-resource-governance
- **阶段**: R2（逐元素 reuse/extend/new 判定）
- **设计基线**: `4eabf04`
- **证据分级**：
  - `[cw]` = callwarden MCP 符号索引命中（search_symbols / get_callers）
  - `[read]` = 直接实读源码验证（cw 索引对 `bin/cw_daemon.rs` 等 binary target 的跨文件 Rust 调用边覆盖不全，`get_symbol` 对这些符号返回空；调用点以 grep 全量核对为准）

## 锚定表

| # | 设计元素 | 判定 | cw 锚点 | 证据与说明 |
|---|---|---|---|---|
| A1 | SnapshotCache（多 workspace 缓存容器） | **extend** | `[cw]` `rust_ext/src/snapshot.SnapshotCache`（struct, L369-375；impl L377-444） | 已有按个数 LRU（`get_or_create` L387、`evict` L431、`len` L441）。`[read]` 确认：命中不更新顺序 → 实为 FIFO，注释称 LRU 与实现不符（T3 修正点）。新增 last_access/touch/evict_idle/approx_bytes |
| A2 | SnapshotManager（单 workspace ArcSwap 发布） | **reuse**（不改动核心） | `[cw]` `rust_ext/src/snapshot.SnapshotManager.build_and_publish_blocking`（L267-307）、`gc_generations`（L320-329） | 冷加载复用 `build_and_publish_blocking` 生产路径；history 自动 GC 不在本期（`gc.snapshots` RPC 已可手动清，见未决项 O6 备注） |
| A3 | GraphStore（全内存符号+CSR） | **extend**（新增近似字节） | `[cw]` 搜索命中 `graph.rs` 结构（`by_id: Vec<GraphSymbol>` L126、CSR L278-292，`[read]` 实读） | 新增 `approximate_bytes()`（O3：口径待 executor 校准，±20% 容忍） |
| A4 | 查询路径冷加载 `get_store_or_load` | **new**（方法）+ **extend**（调用点替换） | `[read]` `snapshot_state.rs:248-260` `get_snapshot_manager`/`get_store`；`snapshot_not_ready` 错误点 L281、L1547、L2222 等 20+ 处（grep 全量核对） | 现状 miss 即报错；新方法 miss → registry 查 ws → resolve db path → build_and_publish_blocking（复用 A2）→ touch |
| A5 | cw_daemon main 启动接线（侦测→映射→写回） | **extend** | `[read]` `cw_daemon.rs` main 流程 L380-560（registry 初始化、SnapshotCache 构造 L433、recover L442） | 在 config 加载后、SnapshotCache 构造前插入 resources 装配；`snapshot_cache_capacity` 取值改为来自 ResourceConfig |
| A6 | daemon.toml [resources] 解析 | **new**（ResourceConfig） | `[read]` `build_journal.rs:297-378` `BuildPipelineConfig`（`[build]` 表先例：`from_toml_str` 容错、`config_path()` = `~/.callwarden/daemon.toml`） | 照同款模式新建 `rust_ext/src/daemon/resources.rs`；容错语义与 `[build]` 一致 |
| A7 | 主机侦测（RAM/核数/磁盘） | **new** | `[read]` `Cargo.toml` L180-192：windows-sys 已启用 `Win32_System_ProcessStatus`（GlobalMemoryStatusEx/GetProcessMemoryInfo）、`Win32_System_Threading`（SetPriorityClass）；`available_parallelism` 已在 `fs_handlers.rs:2387` 使用 | 零新依赖；磁盘类型 IOCTL fail-soft（O2） |
| A8 | 跨平台 RSS 读取 | **new**（`current_rss_bytes`） | `[cw]` `rust_ext/src/daemon/health.read_rss_linux`（L394-412，仅 Linux /proc） | Windows 走 GetProcessMemoryInfo（A7 依赖已具备）；Linux 复用/对齐 read_rss_linux 口径；失败返回 None |
| A9 | resource tick（主循环周期治理） | **extend** | `[read]` `cw_daemon.rs` 主循环 L600-639（100ms tick：SIGTERM/SIGHUP/SIGUSR1/证书巡检） | 新增 30s 周期分支：RSS 采样 + MemoryGovernor.tick + evict_idle；照证书巡检的 tick 计数模式 |
| A10 | build 低优先级注入 | **extend**（两处线程入口） | `[read]` `fs_handlers.rs` `handle_build_graph` spawn `"cw-build-graph"` 线程（L2740-2742）；`run_build_graph` worker 池 spawn（L2385-2389 起） | 线程入口调用 `set_build_thread_priority_low()`（fail-soft）；spawn 语句不改结构 |
| A11 | build 内存软限 gate | **extend**（BuildFlights.begin 前置检查） | `[cw]` `rust_ext/src/daemon/build_flight.BuildFlights`（struct L67-71；`begin` L80-101） | `[read]` `handle_build_graph` 中 `flights.begin` 调用（L2720）前插 gate 检查；拒绝先例照 `build_too_large`（`build_max_files()` L171-172）错误码风格 → `build_memory_pressure` |
| A12 | 启动 recover 懒加载语义 | **reuse**（锁现状）+ **extend**（日志） | `[cw]` `handle_workspace_recover`（workspace.rs impl L4060-4455） | `[read]` L4110-4115：pending==0 → 不发布 snapshot（启动无全量加载点的关键证据）；T6 加回归测试 + 逐 ws 启动日志 |
| A13 | worker 数映射（max_workers / parse_threads） | **extend** | `[read]` `config.rs` `DaemonConfig`（L37-100，`max_workers: 16` 默认 L163）；`BuildPipelineConfig.parse_threads: 4` 默认（build_journal.rs L311） | 16GB 档 → build parse_threads=2（经 [resources] 覆盖 [build] 同名键）；daemon HTTP worker 数 v1 不动（HTTP 池与 build 池解耦，避免过度治理） |
| A14 | MemoryGovernor 状态机 | **new** | 无既有符号（grep `memory_limit|soft_limit` 无命中，仅 health.rs `memory_max_bytes` 只算健康比例不执法 `[read]` L129-138、L308-345） | 迟滞状态机 + 动作输出；接 A9 tick 与 A11 gate |

## 判定汇总

- **reuse（不改动）**：A2 核心发布路径、A12 懒加载语义、BuildFlights 单飞机制、[build] 流水线（REQ-07 闭环，不重做）
- **extend（在既有符号上改）**：A1 SnapshotCache、A3 GraphStore、A4 查询调用点、A5/A9 cw_daemon main、A10 两处线程入口、A11 flights.begin 前置、A13 配置取值来源
- **new（新符号）**：A6 resources.rs（ResourceConfig/HostProfile/detect/apply/write_once）、A7 detect_host_profile、A8 current_rss_bytes、A14 MemoryGovernor

## 锚定对 R0 设计的反馈（无推翻）

1. R0 假设「查询记录可用于统计访问频率」——实查无 workspace 级访问日志表（grep `query_log|recent_access|last_access` 无命中）；REQ-RG-02 改为**在 SnapshotCache 内新增 last_access 跟踪**，不依赖外部查询记录。ac-map 不受影响（AC-RG-02-* 均按 cache 内跟踪断言）。
2. R0 假设「启动可能全量加载」——实查 recover 对 0-pending ws 不发布 snapshot（A12），启动路径本身无全量加载点；REQ-RG-03 从「改造」收敛为「证据化 + 回归锁 + 日志」，已同步 requirements-brief §2.2 与 ac-map AC-RG-03-*。
3. 驱逐必须配套冷加载（A4）：现状 miss 即 `snapshot_not_ready`，若只做驱逐不做自愈会**打破现有可查询性**——这是锚定阶段发现的设计依赖，已提升为 AC-RG-02-2（自愈）与 T4 必做单。
