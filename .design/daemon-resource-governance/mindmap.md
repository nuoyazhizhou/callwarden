# daemon 资源治理面 — 结构脑图（R1）

- **feature**: daemon-resource-governance
- **阶段**: R1（可视化，与 R0 理解一致）
- **设计基线**: `4eabf04`

```mindmap
daemon 资源治理面
├── REQ-RG-01 环境侦测自适应配置
│   ├── 侦测（resources.rs: detect_host_profile）
│   │   ├── 总内存（Windows GlobalMemoryStatusEx / Linux /proc/meminfo）
│   │   ├── CPU 核数（available_parallelism，已有）
│   │   └── 磁盘类型（IOCTL BusType，fail-soft → unknown 按 SSD）
│   ├── 映射表（apply_profile）
│   │   ├── 16GB 档：cache 512MB / workers 2
│   │   ├── 32GB 档：翻倍
│   │   └── HDD：cache 封顶 512MB（防换页）
│   ├── 配置落点（daemon.toml [resources]）
│   │   ├── load_resource_config（容错回退，照 [build] 先例）
│   │   ├── 显式覆盖 > 侦测映射 > 内置默认
│   │   └── write-once 写回（临时文件+rename 原子）
│   └── 启动接线（cw_daemon.rs main）
├── REQ-RG-02 workspace 局部性缓存
│   ├── 驻留面（现状盘点）
│   │   ├── GraphSnapshot = GraphStore 全内存（by_id + CSR + 字符串池）
│   ├── SnapshotCache 个数 LRU（默认 32，实为 FIFO → 修正）
│   ├── history 旧 generation 不自动 GC（gc.snapshots 手动）
│   ├── 治理动作
│   │   ├── last_access 跟踪（touch on get）
│   │   ├── 空闲驱逐（idle_evict_secs=1800s，resource tick 驱逐）
│   │   ├── 字节预算（cache_max_bytes，approx_bytes 估算）
│   │   └── 真实 LRU（访问序驱逐，修 FIFO）
│   └── 冷加载自愈（snapshot_state.rs: get_store_or_load）
│       ├── cache miss → registry 查 ws → codegraph db → build_and_publish_blocking
│       └── 失败保留 snapshot_not_ready 语义（不恶化）
├── REQ-RG-03 启动加载路径盘点
│   ├── 结论：启动无全量加载点
│   │   └── recover：pending==0 → 不发布 snapshot（workspace.rs:4110）
│   ├── 产出
│   │   ├── 证据文档（.design evidence）
│   │   ├── 0-pending 回归锁测试
│   │   └── 启动逐 workspace 日志（built/reused/skipped）
├── REQ-RG-04 build 低优先级
│   ├── 挂点 1：cw-build-graph 线程入口（fs_handlers）
│   ├── 挂点 2：解析 worker 池线程入口（run_build_graph）
│   └── 手段：set_build_thread_priority_low（Win BELOW_NORMAL / Unix nice，fail-soft）
├── REQ-RG-05 内存软限
│   ├── 采样：current_rss_bytes（Win GetProcessMemoryInfo / Linux /proc）
│   ├── 状态机：MemoryGovernor（迟滞：连续 2 次进，-10% 出）
│   ├── 动作
│   │   ├── 拒新 build（build_memory_pressure，BuildFlights.begin 前检查）
│   │   ├── 驱逐空闲 cache（联动 evict_idle）
│   │   └── 降 build worker（parse_threads/2，下一轮生效）
│   └── 暴露：health/status RPC（rss/pressure/驱逐计数/生效 worker）
└── 横切
    ├── daemon 主循环 100ms tick → resource tick（30s 周期）
    ├── daemon.toml [resources] 与 [build] 并列
    └── 不做：符号子集缓存 / Job Object 硬 OOM / 全自动魔法 / jsonl 重做
```
