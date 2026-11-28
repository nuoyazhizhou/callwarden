# daemon 资源治理面 — 流程图（R1，含异常分支）

- **feature**: daemon-resource-governance
- **阶段**: R1
- **设计基线**: `4eabf04`

## F1 启动流程（REQ-RG-01 + REQ-RG-03）

```
cw_daemon main 启动
  │
  ├─► 加载 DaemonConfig（JSON，现状不动）
  │     └─[文件损坏]─► 默认配置（现状，不改）
  │
  ├─► [新] load_resource_config() 读 daemon.toml [resources]
  │     ├─[文件缺失/损坏/单键非法]─► 该键回退内置默认 + WARN 日志（不阻断）
  │     └─[键显式存在]─► 记录 explicit=true（优先于侦测）
  │
  ├─► [新] detect_host_profile()
  │     ├─[RAM/核数/磁盘 API 失败]─► 回退 16GB 保守档 + WARN（不 panic）
  │     └─成功─► HostProfile{ram, cores, disk}
  │
  ├─► [新] apply_profile(explicit 未覆盖的键 ← 映射表)
  │     ├─16GB 档─► cache_max_bytes=512MB, workers=2
  ├────32GB 档─► 翻倍
  │     └─HDD─► cache 封顶 512MB
  │
  ├─► [新] write_resources_section_once()
  │     ├─[已有 [resources]]─► 跳过写（write-once，用户值神圣）
  │     └─[无]─► 临时文件 + rename 原子写入
  │           └─[写失败]─► WARN，继续启动（配置可观测性失败不阻断服务）
  │
  ├─► 构造 SnapshotCache(cache_max_workspaces ← 配置)
  │
  ├─► recover_all_workspaces_with_snapshot（现状）
  │     ├─[ws 有 pending staging]─► Replicator.publish → snapshot 进 cache（-built-）
  │     ├─[ws 无 pending]─► 跳过发布（-skipped-，不构建，回归锁见 T6）
  │     ├─[ws 有 parse_retry entry]─► 重放 → publish（-built-）
  │     └─[单 ws 失败]─► WARN 继续（现状：错误容忍）
  │     └─► [新] 启动日志逐 ws 报告 built/reused/skipped
  │
  └─► HTTPS accept loop + 100ms 主循环
        └─► [新] resource tick（每 30s，见 F4）
```

## F2 查询冷加载自愈（REQ-RG-02，新增分支）

```
query.* RPC（query.symbol / query.callers / ...）
  │
  ├─► get_store_or_load(ws)  [新，替换原 get_store 直查]
  │     ├─cache hit─► touch(ws) 更新 last_access ─► 返回 store ─► 正常查询
  │     │
  │     └─cache miss（被驱逐或从未加载）
  │         ├─► 单飞检查（同 ws 并发重建只跑一次）
  │         │     └─[并发已重建中]─► 等待/返回同一 manager
  │         ├─► registry 查 ws 记录 → resolve codegraph db path
  │         │     └─[ws 不存在/无 db]─► snapshot_not_ready（现状语义，不恶化）
  │         ├─► build_and_publish_blocking（生产路径重建）
  │         │     └─[重建失败]─► snapshot_not_ready + WARN（可重试）
  │         └─► touch + 返回 store ─► 正常查询（自愈完成）
  │
  └─► in-flight 查询持有 Guard 期间发生驱逐
        └─► Arc 引用计数保证查询完成后才释放（不阻塞、不崩溃）
```

## F3 空闲驱逐（REQ-RG-02）

```
resource tick（30s 周期）
  │
  ├─► evict_idle(idle_evict_secs=1800)
  │     ├─遍历 cache：now - last_access > 超时？
  │     │   ├─是─► evict(ws)（cache 移除 Arc；有读者则延迟到查询结束释放）
  │     │   │     └─► 驱逐日志（ws、空闲时长、估算字节）
  │     │   └─否─► 保留 + touch 语义不变
  │     └─► 字节预算检查：approx_bytes > cache_max_bytes？
  │           ├─是─► 按访问序从最旧驱逐直到 ≤ 预算
  │           └─否─► 结束
  │
  └─[驱逐后查询该 ws]─► 走 F2 冷加载自愈
```

## F4 内存软限状态机（REQ-RG-05）

```
resource tick
  │
  ├─► rss = current_rss_bytes()
  │     └─[读取失败 None]─► 跳过本轮（保持现状，WARN 节流）
  │
  ├─► MemoryGovernor.tick(rss)
  │     ├─state=Normal：
  │     │   ├─rss ≤ soft_limit─► 保持 Normal
  │     │   └─rss > soft_limit─► strike+=1
  │     │         └─strike ≥ 2（连续两次）─► 进入 Pressure
  │     │               ├─► 置 build gate（AtomicBool）
  │     │               ├─► 触发 evict_idle(0)（全量清冷）
  │     │               └─► worker 降档（parse_threads/2，下限 1，下一轮 build 生效）
  │     └─state=Pressure：
  │         ├─rss > soft_limit─► 保持（strike 重置）
  │         └─rss < soft_limit × 0.9（-10% 迟滞）─► 退出 Pressure
  │               ├─► 清 build gate
  │               └─► 清 worker 降档
  │     └─[单次超限]─► 不动作（迟滞防抖）
  │
  ├─► [Pressure] 新 build 请求
  │     └─handle_build_graph → flights.begin 前检查 gate
  │           ├─gate 置位─► Err build_memory_pressure（客户端稍后重试=排队语义）
  │           └─gate 未置位─► 正常单飞 + spawn（低优先级，见 F5）
  │           └─注：进行中 build 不杀，自然结束后不再新增
  │
  └─► health/status RPC 暴露：rss_bytes / pressure / evictions / effective workers
```

## F5 build 低优先级（REQ-RG-04）

```
handle_build_graph（fs_handlers）
  │
  ├─► [现状] max_files 检查 → open_codegraph_write → flights.begin（单飞）
  │     └─[已有 build 进行中]─► build_in_progress（现状）
  │     └─[新] gate 检查（F4）
  │
  ├─► spawn "cw-build-graph" 线程
  │     └─► [新] 线程入口 set_build_thread_priority_low()
  │           ├─成功─► 低于 normal 运行
  │           └─失败─► WARN，继续（fail-soft，不阻断 build）
  │
  └─► run_build_graph 阶段一解析 worker 池
        └─► [新] 每个 worker 线程入口同上
              └─（jsonl 分片/背压逻辑 REQ-07 已闭环，不重做）
```
