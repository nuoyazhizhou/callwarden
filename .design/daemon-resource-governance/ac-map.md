# daemon 资源治理面 — 验收地图（ac-map）

- **feature**: daemon-resource-governance
- **阶段**: R0（与 requirements-brief.md 同批产出）
- **设计基线**: `4eabf04`

每条 AC 标注：关联 REQ、证据采集方式（测试类型/断言点）、承载任务。

## REQ-RG-01 环境侦测自适应配置

| AC | 断言 | 证据采集 | 任务 |
|---|---|---|---|
| AC-RG-01-1 | 16GB 档机器首次启动后 `~/.callwarden/daemon.toml` 出现 `[resources]`，`cache_max_bytes=536870912`、`build_parse_threads=2` | 集成测试：临时 HOME + 注入 HostProfile（mock 侦测值）→ 启动 → 读文件断言 | T2 |
| AC-RG-01-2 | 显式覆盖优先：文件已有 `cache_max_bytes="1G"` 时侦测映射**不**改写该键，生效值=1G | 单测：load_resource_config 显式值覆盖 apply_profile 映射值 | T1 |
| AC-RG-01-3 | daemon.toml 损坏/单键非法 → 该键回退默认、启动不失败、日志 WARN | 单测：from_toml_str 容错分支（照 BuildPipelineConfig 同款） | T1 |
| AC-RG-01-4 | 侦测 API 失败 → fail-soft 回退 16GB 档 + WARN，进程不 panic | 单测：detect_host_profile 容错分支（feature 无法 mock 时以返回值兜底分支覆盖） | T1 |
| AC-RG-01-5 | write-once：`[resources]` 已存在时二次启动不重写、不改用户值 | 集成测试：连续启动两次，断言文件 mtime/内容不变 | T2 |

## REQ-RG-02 workspace 局部性缓存

| AC | 断言 | 证据采集 | 任务 |
|---|---|---|---|
| AC-RG-02-1 | workspace 空闲超过 `idle_evict_secs` 后被驱逐，`cache.len()` 下降，驱逐含日志 | 单测：SnapshotCache::evict_idle 注入短超时 | T3/T5 |
| AC-RG-02-2 | 驱逐后同 workspace 查询自愈：冷加载重建 snapshot 并返回正确结果（不再报 snapshot_not_ready） | 集成测试：发布→驱逐→query.symbol 断言结果正确 | T4 |
| AC-RG-02-3 | 驱逐按**访问序**（修正 FIFO）：持续访问的旧条目不被优先驱逐 | 单测：A 插入后持续 touch，B/C 插入后不访问 → B 先被驱逐 | T3 |
| AC-RG-02-4 | cache 驻留字节受 `cache_max_bytes` 约束：超预算时 tick 驱逐最旧直到低于预算 | 单测：注入 approx_bytes 超预算场景 | T5 |
| AC-RG-02-5 | 驱逐不破坏 in-flight 查询：查询持有 Guard 期间驱逐，查询正常返回 | 单测：load Guard 存活时 evict，查询完成才释放 | T3 |
| AC-RG-02-6 | 并发冷加载同一 workspace 不重复 build（单飞） | 集成测试：两线程同时 miss，断言 build 计数=1 | T4 |

## REQ-RG-03 启动加载路径盘点

| AC | 断言 | 证据采集 | 任务 |
|---|---|---|---|
| AC-RG-03-1 | 0 pending staging entries 的 workspace 启动恢复**不**构建 snapshot（回归锁，锁住现状懒加载语义） | 集成测试：注册 ws（无 pending）→ 启动 recover → 断言 cache 无该 ws snapshot | T6 |
| AC-RG-03-2 | 启动日志逐 workspace 报告 built/reused/skipped 计数 | 集成测试：捕获 eprintln 断言含三类计数 | T6 |
| AC-RG-03-3 | 盘点证据文档存在且覆盖「启动路径无全量加载点」结论与代码位置 | 文档审阅（audit）：.design 本 feature 下 evidence 文件 | T6 |

## REQ-RG-04 build 低优先级

| AC | 断言 | 证据采集 | 任务 |
|---|---|---|---|
| AC-RG-04-1 | cw-build-graph 线程与解析 worker 线程优先级低于 normal（Windows BELOW_NORMAL_PRIORITY_CLASS / Unix nice>0） | 平台测试：线程内读回优先级断言 | T7 |
| AC-RG-04-2 | 优先级设置失败不阻断 build（fail-soft，返回 false 继续） | 单测：直接调用返回 bool，build 路径不受影响 | T7 |

## REQ-RG-05 内存软限

| AC | 断言 | 证据采集 | 任务 |
|---|---|---|---|
| AC-RG-05-1 | RSS 连续超软限 → 新 build 被拒（错误码 `build_memory_pressure`），进行中 build 不受影响 | 集成测试：注入 rss provider 超限 → build 返回错误码 | T8 |
| AC-RG-05-2 | 软限触发时：空闲 cache 被驱逐 + 下一轮 build worker 数下降（parse_threads/2，下限 1） | 单测：Governor tick 返回 Pressure 动作 + override 生效断言 | T8 |
| AC-RG-05-3 | 迟滞：单次超限不触发（需连续 2 次采样）；退出 pressure 需低于阈值-10% | 单测：状态机序列断言 | T8 |
| AC-RG-05-4 | health/status RPC 暴露 rss_bytes、pressure 状态、驱逐计数、生效 worker 数 | RPC 测试：断言返回字段 | T8 |
| AC-RG-05-5 | RSS 读取跨平台可用（Windows GetProcessMemoryInfo / Linux /proc），失败返回 None 不 panic | 单测：current_rss_bytes() 当前平台返回 Some 或 None（不 panic） | T8 |

## 覆盖矩阵（REQ × AC）

| REQ | AC 数 | 承载任务 |
|---|---|---|
| REQ-RG-01 | 5 | T1, T2 |
| REQ-RG-02 | 6 | T3, T4, T5 |
| REQ-RG-03 | 3 | T6 |
| REQ-RG-04 | 2 | T7 |
| REQ-RG-05 | 5 | T8 |

总计 21 条 AC。每条 AC 均有自动化证据（单测/集成测试/RPC 测试/文档审阅），无「人工确认」型 AC。
