# daemon 资源治理面 — 契约与设计门禁（R3）

- **feature**: daemon-resource-governance
- **阶段**: R3（契约基线，过设计门禁后打基线提交）
- **设计基线**: `4eabf04`
- **追溯**：REQ ↔ AC 见 `ac-map.md`；符号锚定见 `symbol-anchor.md`；任务映射见 `task-map.md`（唯一映射表，本文不重复）

## 一、函数级契约

### C1 `rust_ext/src/daemon/resources.rs`（新建模块）

#### C1.1 `pub struct ResourceConfig`

| 项 | 内容 |
|---|---|
| 入参（字段） | `cache_max_bytes: u64`、`cache_max_workspaces: usize`、`idle_evict_secs: u64`、`memory_soft_limit_bytes: u64`、`build_parse_threads: usize`、`build_priority_low: bool`、`disk_type: DiskType{Auto,Ssd,Hdd,Unknown}`、`explicit_keys: HashSet<String>`（daemon.toml 中显式出现的键） |
| 出参 | 构造 + `Default`（内置默认：cache_max_bytes=2GB、cache_max_workspaces=32（对齐现状 `DEFAULT_SNAPSHOT_CACHE_CAPACITY`）、idle_evict_secs=1800、memory_soft_limit_bytes=0（0=禁用软限，仅侦测档映射后非 0）、build_parse_threads=4（对齐 `[build]` 默认）、build_priority_low=true、disk_type=Auto） |
| 异常路径 | 无（纯数据） |
| 校验归属 | `load_resource_config` 负责非法值回退 |
| 测试要点 | Default 与现状值对齐（32/4/1800） |

#### C1.2 `pub fn load_resource_config() -> ResourceConfig`

| 项 | 内容 |
|---|---|
| 入参 | 无（内部读 `~/.callwarden/daemon.toml`，路径逻辑复用 `BuildPipelineConfig::config_path` 同款：USERPROFILE/HOME） |
| 出参 | ResourceConfig |
| 异常路径 | 文件缺失/损坏/单键非法/类型错 → **该键**回退默认 + WARN（照 `BuildPipelineConfig::from_toml_str` 容错语义，不 panic 不阻断）；`cache_max_bytes` 接受 `"512M"`/`"1G"`/字节数（复用 `_is_valid_size_string` 同款解析，Rust 侧自实现） |
| 校验归属 | 本函数；`explicit_keys` 在解析时记录（供 apply_profile 跳过） |
| 测试要点 | AC-RG-01-2（显式覆盖）、AC-RG-01-3（损坏容错）；顶层键与 `[resources]` 表两种写法 |

#### C1.3 `pub struct HostProfile` + `pub fn detect_host_profile() -> HostProfile`

| 项 | 内容 |
|---|---|
| 入参 | 无 |
| 出参 | `total_ram_bytes: u64`、`cpu_cores: usize`、`disk_type: DiskType` |
| 异常路径 | RAM：Windows `GlobalMemoryStatusEx` / Linux `/proc/meminfo` 失败 → total_ram_bytes=0；核数：`available_parallelism` 已有兜底；磁盘：IOCTL 失败/网络盘/WSL → Unknown。任何失败 **fail-soft**（WARN，不 panic），由 apply_profile 对 0/Unknown 走 16GB 保守档 |
| 校验归属 | apply_profile |
| 测试要点 | AC-RG-01-4（容错分支）；当前平台 smoke（返回值不 panic） |

#### C1.4 `pub fn apply_profile(cfg: &mut ResourceConfig, profile: &HostProfile)`

| 项 | 内容 |
|---|---|
| 入参 | 可变 ResourceConfig + HostProfile |
| 出参 | 原地修改；返回 `Vec<String>`（本次被映射覆盖的键，供日志） |
| 映射规则 | RAM<24GB → 档1：cache_max_bytes=512MB、cache_max_workspaces=8、build_parse_threads=2、memory_soft_limit_bytes=max(2GB, RAM×20%)；RAM≥24GB → 档2：翻倍（cache 1GB、workspaces 16、workers 4、soft=max(2GB, RAM×20%)）；disk_type=Hdd → cache_max_bytes 封顶 512MB；RAM=0（侦测失败）→ 档1 |
| 异常路径 | 无（纯函数） |
| 校验归属 | **只覆盖 `explicit_keys` 之外的键**（显式覆盖神圣，AC-RG-01-2） |
| 测试要点 | 档1/档2/HDD 封顶/显式键不被覆盖/侦测失败走档1 |

#### C1.5 `pub fn write_resources_section_once(cfg: &ResourceConfig) -> std::io::Result<WriteOutcome>`

| 项 | 内容 |
|---|---|
| 入参 | 生效后的 ResourceConfig |
| 出参 | `WriteOutcome{Written, SkippedExisting, Failed}` |
| 行为 | daemon.toml 无 `[resources]` 段 → 追加写入（临时文件+rename 原子替换，O4）；已有段 → SkippedExisting（write-once）；写失败 → Failed + WARN，**不阻断启动** |
| 异常路径 | 磁盘满/权限/并发写 → Failed 分支 |
| 校验归属 | 写前重读文件判定（缩小竞态窗，O4 不声称完全防并发） |
| 测试要点 | AC-RG-01-1、AC-RG-01-5（连续两次启动内容不变） |

#### C1.6 `pub fn current_rss_bytes() -> Option<u64>`

| 项 | 内容 |
|---|---|
| 出参 | 当前进程 RSS 字节；Windows `GetProcessMemoryInfo`（WorkingSetSize）/ Linux `/proc/self/status` VmRSS（对齐 `health.read_rss_linux` 口径）；失败 None |
| 异常路径 | API 失败/字段缺失 → None（调用方跳过本轮） |
| 测试要点 | AC-RG-05-5（当前平台不 panic） |

#### C1.7 `pub fn set_build_thread_priority_low() -> bool`

| 项 | 内容 |
|---|---|
| 出参 | true=设置成功；Windows：`SetPriorityClass(GetCurrentProcess(), BELOW_NORMAL_PRIORITY_CLASS)`？——**否**：进程级会殃及查询线程。用 `SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL)`（线程级）；Unix：`libc::setpriority(Interval::Current(0), 10)` nice（线程级经 `setpriority` 对 tid？——Unix 线程级需 `libc::syscall` settid，v1 降级为进程 nice 仅在 build 线程内调用会殃及全进程 → **Unix 侧 v1 用 `thread` 作用域不可行时记 false 并 WARN，Windows 为一线平台必须生效**） |
| 异常路径 | 失败返回 false，调用方 WARN 继续（fail-soft，AC-RG-04-2） |
| 测试要点 | AC-RG-04-1（Windows 读回优先级断言；Unix 平台允许 false+WARN） |

> 说明：上表 Unix 分支的取舍是 R3 新识别的约束——`setpriority(PRIO_PROCESS, 0, nice)` 是进程级，在 build 线程调用会降低整个 daemon（含查询）优先级，违背「不抢前台 agent」但也不伤查询（只是整体变慢）——**不可接受**。Unix 正确做法是对线程调 `settid`（`syscall(SYS_setpriority, PRIO_PROCESS, tid, nice)`，`libc::pthread_getthreadid_np`/gettid）。契约要求 executor 按 gettid+setpriority(tid) 实现，失败才 false。

#### C1.8 `pub struct MemoryGovernor` + `pub fn tick(&mut self, rss: Option<u64>) -> GovernorAction`

| 项 | 内容 |
|---|---|
| 入参 | rss 字节（None=采样失败） |
| 出参 | `GovernorAction{ Normal, EnterPressure, StayInPressure, ExitPressure }`（动作含：reject_new_builds、evict_cold、reduce_workers 三个 bool 由状态推导） |
| 状态机 | Normal + rss>limit 连续 ≥2 次 → EnterPressure；Pressure + rss < limit×0.9 → ExitPressure；Pressure + rss>limit → StayInPressure；采样失败 → 保持现状（不波动） |
| 异常路径 | None 输入不改变状态 |
| 测试要点 | AC-RG-05-3（迟滞序列）、AC-RG-05-2（动作推导） |

### C2 `rust_ext/src/snapshot.rs` SnapshotCache 扩展

| 符号 | 契约 |
|---|---|
| `pub fn touch(&self, workspace_id: &str)` | 更新该 ws 的 last_access=now；不存在则无操作（不创建）。**同时修 `get_or_create` 命中路径调用 touch（FIFO→真 LRU）** |
| `pub fn evict_idle(&self, idle: Duration) -> Vec<String>` | 驱逐 now-last_access > idle 的 ws，返回被驱逐列表（供日志/指标）；Arc 语义：cache 移除后 in-flight Guard 持有到查询结束才释放（AC-RG-02-5） |
| `pub fn approx_bytes(&self) -> u64` | 各 ws 当前 snapshot 的 `GraphStore::approximate_bytes()` 求和（未发布=0） |
| 内部新增 | `last_access: HashMap<WorkspaceInstanceId, Instant>`（与 managers 同锁域）；`SnapshotManager` 增 `last_access: AtomicU64`（或统一放 cache 侧，executor 择一，契约只要求外部行为） |

测试要点：AC-RG-02-1/2-3/2-5。

### C3 `rust_ext/src/graph.rs` `pub fn approximate_bytes(&self) -> u64`

- 入参无；出参 = by_id 字节 + CSR forward/backward 边字节 + 字符串池字节 + 文件路径表字节（求和口径，O3 待校准 ±20%）。
- 测试：构造已知规模 store，断言估值在合理区间（下界 >0、上界 < 10× 元素原始字节和）。

### C4 `rust_ext/src/daemon/snapshot_state.rs` `fn get_store_or_load(&mut self, peer, workspace_instance_id) -> Result<Arc<GraphStore>, DaemonRpcError>`

| 项 | 内容 |
|---|---|
| 行为 | ①cache hit → touch → 返回；②miss → 单飞（同 ws 并发重建合并，复用 `SnapshotCache.get_or_create` 的 manager 单例 + per-ws 重建互斥，实现方式 executor 定，契约要求 AC-RG-02-6 通过）→ registry 查 ws（owner ACL 复用 `owned_workspace`）→ `resolve_codegraph_db_path` → `build_and_publish_blocking`（生产路径，复用 A2）→ touch → 返回；③任一步失败 → `snapshot_not_ready`（现状错误码，不恶化）+ WARN |
| 校验归属 | ACL 归 `owned_workspace`（现状）；db path 归 `resolve_codegraph_db_path`（现状） |
| 调用点替换 | `snapshot_state.rs` 内所有 `get_store(ws).ok_or(snapshot_not_ready)` 查询 handler 逐个切换（20+ 处，grep 清单见 symbol-anchor A4）；只改取 store 方式，不改业务逻辑 |
| 测试要点 | AC-RG-02-2（驱逐后自愈）、AC-RG-02-6（并发单飞） |

### C5 `rust_ext/src/daemon/fs_handlers.rs` + `build_flight.rs` build gate + 低优先级

| 符号 | 契约 |
|---|---|
| `BuildFlights` 或模块级 `pub fn memory_gate_open() -> bool` | 读全局 AtomicBool（cw_daemon 持有）；`handle_build_graph` 在 `flights.begin` **前**检查，关闭时 `Err(DaemonRpcError::new("build_memory_pressure", ...))`（错误码风格照 `build_too_large`） |
| cw-build-graph 线程入口 | 首行调 `set_build_thread_priority_low()`，忽略返回值外再加 WARN |
| run_build_graph worker 入口 | 同上（每 worker 一次） |
| worker 降档 | MemoryGovernor Pressure 时写 `AtomicUsize` override；`run_build_graph` 的 `n_workers` 计算（fs_handlers L2385-2388）改为 `override.unwrap_or(cfg.parse_threads).min(available_parallelism).max(1)` |

测试要点：AC-RG-05-1、AC-RG-04-1/2。

### C6 `rust_ext/src/bin/cw_daemon.rs` resource tick 接线

- 主循环（L600-639 现状 tick 块）新增 `resource_tick` 计数分支：每 300 tick（30s）执行：
  1. `current_rss_bytes()` → `governor.tick()`（governor 的 limit 来自 ResourceConfig）
  2. EnterPressure/StayInPressure → 置 build gate + `snapshot_cache.evict_idle(Duration::ZERO)`（全量清冷）+ worker override 置位
  3. Normal → `evict_idle(idle_evict_secs)` + 字节预算驱逐（`approx_bytes > cache_max_bytes` 时按访问序驱逐）
  4. ExitPressure → 清 gate/override
- 启动日志：逐 ws built/reused/skipped（C12，T6）。
- 测试：AC-RG-05-4（health/status 暴露 `resources` 段：rss_bytes、pressure、evictions_total、effective_build_workers）。

## 二、设计门禁四件套

### 2.1 方案对比（每项 ≥2，含优缺点与失效模式）

**D1 配置落点**
- 方案 A（选）：`daemon.toml [resources]`，照 `BuildPipelineConfig` 先例。优点：用户可见可编辑、与 [build] 同文件、容错先例成熟。缺点：与 Rust DaemonConfig（JSON）双配置体系并存。失效模式：用户改了 JSON DaemonConfig 以为能覆盖 [resources] → 文档明确两者职责（启动拓扑=JSON，资源治理=daemon.toml）。
- 方案 B（否决）：扩展 Rust DaemonConfig JSON。优点：单一启动配置。缺点：`DaemonConfig` 注释自称「最小集」，扩 7 个字段要动 serde 结构 + 测试矩阵 + 兼容旧 JSON；且用户编辑频率高的恰是 daemon.toml。**否决理由**：改造成本高且偏离主理人指定方向。

**D2 驱逐粒度**
- 方案 A（选）：workspace 粒度 LRU + idle。优点：失效边界清晰（整 ws 重建）、Arc 语义天然安全。缺点：大 ws 驱逐后冷查询有一次 build 延迟。失效模式：热点大 ws 被误驱逐 → 冷加载自愈兜底（F2）。
- 方案 B（否决）：符号子集粒度。优点：更细内存控制。缺点：失效复杂（主理人明确 v1 不做）、CSR 结构不支持部分驻留。**否决理由**：复杂度/收益比失衡。

**D3 冷加载方式**
- 方案 A（选）：cache miss → `build_and_publish_blocking` 重建。优点：复用生产发布管线、查询面零改动（只换取 store 入口）。缺点：冷查询延迟 = build 延迟（与 daemon 冷启动首次查询同代价）。失效模式：重建失败 → snapshot_not_ready（不恶化现状）。
- 方案 B（否决）：查询旁路直接走 SQLite。优点：无常驻内存。缺点：查询面 20+ handler 全部依赖 GraphStore 内存结构（CSR），重写等于第二套查询引擎；双路径一致性风险。**否决理由**：工程量爆炸且引入一致性风险。

**D4 内存软限**
- 方案 A（选）：RSS 采样 + 软动作（拒新 build/驱逐/降 worker）。优点：行为可预测、不杀 in-flight。缺点：非硬保证（极端情况仍可能超限）。失效模式：RSS 采样窗口漏掉尖峰 → 靠迟滞+周期（30s）收敛。
- 方案 B（否决）：Job Object 硬 OOM。优点：硬上限。缺点：杀 in-flight 查询与 build，违背「像 SQLite 一样安静」。**否决理由**：主理人明确排除 + 行为不可预测。

**D5 build 优先级作用域**
- 方案 A（选）：线程级（SetThreadPriority / settid nice）。优点：只降 build，查询不受影响。失效模式：Unix settid 失败 → false+WARN（fail-soft）。
- 方案 B（否决）：进程级 SetPriorityClass/nice。优点：实现简单。缺点：整 daemon（含查询）降速。**否决理由**：伤及查询面。

**D6 侦测写回**
- 方案 A（选）：write-once 写 `[resources]`。优点：用户能看到默认值从哪来、可直接改。缺点：引入文件写入路径（O4 竞态）。失效模式：写失败 → WARN 不阻断。
- 方案 B（否决）：只写日志。优点：零风险。缺点：用户不知道当前生效值/无法就地覆盖。**否决理由**：违背「行为可预测优先」的可观测要求。

### 2.2 被否决方案理由汇总

见 2.1 各项「否决理由」。共性原则：**可预测 > 激进；复用 > 重写；fail-soft > fail-hard**。

### 2.3 异常场景矩阵（8 类）

| # | 场景 | 处置 | 验证 |
|---|---|---|---|
| E1 | daemon.toml 损坏/非法值 | 该键回退默认 + WARN，不阻断 | AC-RG-01-3 |
| E2 | RAM/磁盘侦测 API 失败（WSL/网络盘/权限） | fail-soft 保守档（16GB 档）+ WARN | AC-RG-01-4 |
| E3 | 驱逐时 in-flight 查询持有 snapshot | Arc 计数：查询完成后才释放；不阻塞不崩溃 | AC-RG-02-5 |
| E4 | 驱逐后查询（cache miss） | F2 冷加载自愈；失败保留 snapshot_not_ready | AC-RG-02-2 |
| E5 | 软限触发时正在 build | 不杀进行中；只拒新 build；结束后自然恢复 | AC-RG-05-1（gate 只拦 begin 前） |
| E6 | RSS 阈值附近抖动 | 迟滞：连续 2 次进、-10% 出；采样失败保持现状 | AC-RG-05-3 |
| E7 | 并发冷加载同一 workspace | 单飞合并（AC-RG-02-6），不重复 build | AC-RG-02-6 |
| E8 | SIGHUP 热重载 [resources] | v1 不支持：WARN「需重启生效」（与 snapshot_cache_capacity 同口径） | 日志断言（T2） |

### 2.4 未决项三态

| # | 状态 | 内容 | 阻塞 |
|---|---|---|---|
| O1 | assumption | 映射表数值（16GB→512MB/2、32GB→翻倍、软限=max(2GB,20%RAM)）——待用户数值确认或运行期调优 | T1 常量（结构不阻塞） |
| O2 | assumption | 磁盘类型 IOCTL 在 WSL/网络盘行为未知 → fail-soft Unknown→按 SSD | T1 detect |
| O3 | open | approximate_bytes 口径 ±20% 校准 | T3 精度（不影响接口） |
| O4 | open | [resources] 写回并发竞态（单用户假设；临时文件+rename+存在即跳过） | T2 实现细节 |
| O5 | open | IO 低优先级 Windows 无公开线程级 API → v1 仅 CPU 优先级 + channel_depth 背压 | 无（范围已收敛） |
| O6 | open | SnapshotManager.history 不自动 GC 是否纳入本期（现 `gc.snapshots` 手动可清；若软限触发时 history 占比高，v1 只驱逐 current，history 留待 Phase 2） | T5 范围（v1 明确不动 history） |
| O7 | confirmed | 配置落点 daemon.toml [resources]、workspace 粒度、软限动作集合、build 默认低优先级（主理人已对齐） | — |
