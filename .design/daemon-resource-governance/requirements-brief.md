# daemon 资源治理面 — 需求简报（R0）

- **feature**: daemon-resource-governance
- **阶段**: R0（需求澄清与分叉判定）
- **生成时间**: 2026-11-27
- **设计基线**: `4eabf04`（master HEAD；`1104fcc` 为其祖先，中间 6 个提交均为 preq F2/F3/F6 整改，与本需求无关）
- **判定**: 走**完整链**（新需求，非存量仓小迭代）

## 1. 用户场景（主理人与用户已对齐）

cw 典型用户同时开多个 Node.js agent 会话（内存管理差），机器 16-32GB 或 2-3 年前旧机。daemon 是多 agent 共享的基础设施，必须像 SQLite 一样安静，不能像 Chrome 一样吃内存。

实测现状：daemon（PID 28728）私有内存 ~1.9GB（单 workspace build 后），多 workspace 索引驻留会更高；无进程级资源限制，资源占用由工作负载自然驱动。

## 2. 仓库现状盘点（代码证据，均已实读验证）

### 2.1 内存去向（谁在吃 1.9GB）

| 驻留点 | 位置 | 机制 | 现状 |
|---|---|---|---|
| GraphSnapshot（符号+调用+CSR 全内存） | `rust_ext/src/snapshot.rs` `GraphSnapshot` / `GraphStore` | `build_and_publish_blocking` → `load_from_sqlite_blocking` 全量加载 | 发布后**常驻**，无空闲驱逐 |
| SnapshotCache | `rust_ext/src/snapshot.rs:369` | 按 workspace 个数 LRU（默认 32 个），**非按字节**；实为**插入序 FIFO**（`get_or_create` 命中不更新顺序，注释写 LRU 但实现是 FIFO） | 32 workspace × 全量图 = 无上界 |
| SnapshotManager.history | `snapshot.rs:139` | 旧 generation VecDeque，**不自动截断**，仅 `gc.snapshots` RPC 手动清 | 无自动 GC |
| SQLite 连接 PRAGMA | `server/daemon_config.py`（Python daemon）+ Rust daemon 各连接 | cache_size 256MB + mmap 256MB × 多连接 | 配置驱动 |
| build 解析流水线 | `rust_ext/src/daemon/fs_handlers.rs` `run_build_graph` | 阶段一 jsonl 分片（channel_depth=256 背压），REQ-07 已闭环 | build 侧不堆内存（已解决，不重做） |

### 2.2 启动加载路径（想法 3 的答案）

`cw_daemon.rs` `recover_all_workspaces_with_snapshot`（L1170）遍历**所有**注册 workspace 逐个 dispatch `workspace.recover`。但 `handle_workspace_recover`（`workspace.rs:4060`）在 **pending staging entries == 0 时直接返回成功、不构建 snapshot**（L4110-4115）；只有 pending>0 且 publisher 可用才走 Replicator→snapshot publish。parse_retry 重放路径同理（有 entry 才 publish）。

**结论**：启动无「全量索引进内存」点；真正的内存驱动是**查询/发布后的常驻 + history 不自动 GC + 无空闲驱逐**。REQ-RG-03 转为「证据化 + 回归锁 + 启动逐 workspace 日志」。

### 2.3 可复用组件

- `windows-sys` 已启用 `Win32_System_ProcessStatus`（GetProcessMemoryInfo 可用）与 `Win32_System_Threading`（SetPriorityClass/SetThreadPriority 可用）——**无需新增依赖**。
- `health.rs` `read_rss_linux` 仅 Linux（/proc/self/status），Windows 不可用 → 需跨平台 RSS 读取。
- `daemon.toml`（`~/.callwarden/daemon.toml`）已有 `[build]` 表先例（`BuildPipelineConfig`，容错回退默认），`[resources]` 段照此模式。
- daemon 主循环 100ms tick（SIGTERM/SIGHUP/SIGUSR1/证书巡检）→ resource tick 的天然挂点。
- `BuildFlights`（`build_flight.rs`）单飞 + `CALLWARDEN_BUILD_MAX_FILES` 拒绝先例（`build_too_large`）→ 软限拒新 build 的挂点。
- 查询路径 cache miss → `snapshot_not_ready` 错误（`snapshot_state.rs:281` 等 20+ 处 `get_store` 调用点），**无按需重建** → 驱逐必须配套冷加载自愈。

## 3. 需求（REQ 编号）

### REQ-RG-01 环境侦测自适应配置（must）
启动时读总内存/CPU 核数/磁盘类型，映射默认配置（16GB → 缓存上限 512MB、worker 2；32GB → 翻倍），写 `daemon.toml` `[resources]` 段并允许显式覆盖；不做全自动魔法（行为可预测优先）。使用习惯自适应并入 REQ-RG-02（workspace 访问频率即使用习惯）。

### REQ-RG-02 workspace 级局部性缓存（must）
热点 workspace 符号/调用缓存驻留内存，冷 workspace 只留 SQLite 按需加载，空闲超时（默认 30 分钟）驱逐。第一版按 workspace 粒度 LRU（符号子集粒度失效复杂，**不做**）。配套：查询路径 cache miss 冷加载自愈（当前直接报 snapshot_not_ready）。

### REQ-RG-03 启动加载路径盘点（must）
确认 daemon 启动时加载已有索引无全量进内存点（有则改按需加载+分页）；build 侧 jsonl 已覆盖无需重做。产出：证据文档 + 0-pending 回归测试 + 启动逐 workspace 日志（built/reused/skipped）。

### REQ-RG-04 build 低优先级（must）
build 线程（cw-build-graph + 解析 worker 池）设 CPU 低优先级（Windows BELOW_NORMAL_PRIORITY_CLASS / Unix nice），不抢前台 agent；IO 低优先级 v1 以 CPU 优先级为主（Windows 无独立 IO 优先级 API 可用且不跨平台，见未决项）。

### REQ-RG-05 内存软限（must）
周期采样 RSS；超软限 → ①拒新 build（排队语义=返回 build_memory_pressure 错误，客户端稍后重试）②驱逐空闲 cache ③降 build worker（下一轮 build 生效）；迟滞防抖；不用 Job Object 硬 OOM。

## 4. 非目标（明确不做）

- 符号子集粒度缓存（REQ-RG-02 v1 明确排除）
- Job Object / cgroup 硬内存上限（会杀 in-flight，违背「安静基础设施」）
- 全自动魔法调参（无监督自动调优；映射表固定、显式覆盖优先）
- build 侧 jsonl 流水线重做（REQ-07 已闭环）
- Python daemon（server/daemon_config.py）侧的同类改造（生产 daemon 是 Rust cw_daemon；Python 版配置仅注释参考）
- SIGHUP 热重载 [resources]（与 snapshot_cache_capacity 同口径：变更需重启，日志 WARN）

## 5. 未决项（三态）

| # | 状态 | 内容 | 阻塞 |
|---|---|---|---|
| O1 | assumption | 映射表数值：16GB→cache 512MB/workers 2、32GB→翻倍、软限=max(2GB, RAM×20%)。依据主理人口头方向 + 实测 1.9GB 推算，**未经用户数值确认** | T1 映射表常量（可后续调，不阻塞结构） |
| O2 | assumption | 磁盘类型 Windows 侦测路径（IOCTL_STORAGE_QUERY_PROPERTY BusType）在 WSL/网络盘/权限受限时行为未知；fail-soft 回退 unknown→按 SSD 处理 | T1 detect（fail-soft 不阻塞） |
| O3 | open | GraphStore `approximate_bytes` 的估算口径（by_id+CSR+字符串池求和 vs 实际 RSS 归因），误差容忍 ±20%；需 executor 实现时用真实 workspace 校准 | T3 的 approximate_bytes 精度（不影响接口形状） |
| O4 | open | `[resources]` 写回的并发写保护（单用户假设；多 agent 同时启动 daemon 的竞态）——v1 用「写临时文件+rename 原子替换 + 存在即跳过」，多用户场景留待后续 | T2 写回实现细节 |
| O5 | open | IO 低优先级：Windows 无跨进程线程级 IO 优先级公开 API（IoPriority 需 undocumented API）；v1 仅 CPU 优先级，IO 侧靠 channel_depth 背压天然限流 | 无（v1 范围已收敛） |

## 5.1 与主理人对齐的既定方向（confirmed）

- 配置落点：`daemon.toml` `[resources]` 段（沿用 `[build]` 先例）
- 驱逐粒度：workspace 级 LRU，30 分钟空闲超时
- 软限动作：拒新 build + 驱逐 cache + 降 worker，不用 Job Object
- build 优先级：默认低优先级，fail-soft
