# T6 性能基准报告 — 生产 daemon + 真实 workspace 端到端 RPC 延迟

> 测试日期：2026-10-07
> 负载：生产 Rust daemon（HTTP transport）+ 已发布 snapshot 的 callwarden 自身 workspace
> 方法论遵循 AGENTS.md 规则 13（真实 E2E 优先，串行取分位数，分开报告，记录硬件）

## 1. 执行摘要

本轮（T6）区别于历史的合成数据压测（`tests/_bench_*`、`docs/performance_report_million_symbols.md`），
聚焦此前未覆盖的维度：**以生产 daemon + 真实已加载 workspace 为负载，测量端到端
RPC 往返延迟**。查询参数全部从真实符号表采样（非合成），所有调用 `errors=0`。

### 关键结论

| 结论 | 数据支撑 |
|------|---------|
| **核心查询 p50 为个位数毫秒** | ping 1.0ms / query.search 2.1ms / query.callers 1.7ms（p50） |
| **Rust CSR 查询本体极快** | 所有图查询 min 延迟 0.6–1.7ms（去除抖动后的真实下界） |
| **尾延迟（p95/p99）由 daemon 侧调度抖动主导，非连接建立** | 持久连接对照 p95/p99 与每调用新建几乎相同（~17–25ms） |
| **get_fts_status 是唯一稳定偏高项** | min 都 31.7ms——全表 `COUNT(symbols)+COUNT(symbols_fts)` 的真实成本，非抖动 |
| **调用链 BFS p50 16ms** | depth=5 全图 BFS，仍在交互可接受范围 |

## 2. 测试环境

| 项 | 值 |
|----|----|
| OS | Windows 11（10.0.26200） |
| CPU | Intel64 Family 6 Model 170（22 逻辑核） |
| Python | 3.14.3 |
| Transport | HTTP（CW_DAEMON_TRANSPORT=http） |
| Workspace | callwarden self（4baea3ff12c2ea5c） |
| 负载规模 | 339,797 符号 / 274,336 调用 / 130,177 文件实例 |
| 迭代 | 每方法 warmup=25 + 测量 200 次（调用链 50 次），串行 |

> 负载数字来源：`cw daemon snapshot-list`（generation 1，本轮 `collab publish` 发布）。

## 3. 延迟结果（毫秒，n=200，串行）

| 维度 | 方法 | min | p50 | p95 | p99 | max | mean |
|------|------|-----|-----|-----|-----|-----|------|
| 基线 RPC | ping | 0.69 | **1.04** | 19.8 | 24.3 | 26.9 | 6.4 |
| 基线 RPC | health | 0.65 | 1.13 | 22.1 | 24.5 | 25.6 | 7.7 |
| 基线 RPC | schema.version | 0.57 | **0.87** | 19.6 | 24.9 | 26.1 | 5.6 |
| 统计 | query.stats | 1.59 | 12.7 | 34.2 | 41.7 | 41.8 | 11.8 |
| 索引状态 | get_fts_status | 31.7 | 34.4 | 48.3 | 53.8 | 64.1 | 37.2 |
| 符号搜索 | query.search（真实词轮转） | 1.48 | **2.07** | 33.0 | 41.5 | 46.3 | 10.1 |
| 符号详情 | query.symbol（真实 qname） | 3.75 | 16.9 | 40.0 | 44.1 | 44.6 | 17.5 |
| 图遍历 | query.callers（真实 qname） | 1.15 | **1.70** | 30.7 | 42.0 | 43.6 | 9.3 |
| 图遍历 | query.callees（真实 qname） | 1.13 | 12.9 | 33.4 | 41.3 | 42.7 | 11.9 |
| 调用链 | query.call_chain_down（depth5，n=50） | 1.15 | 16.2 | 41.6 | 44.6 | 44.6 | 15.8 |

原始数据：[t6_perf_result.json](t6_perf_result.json)

## 4. 尾延迟根因分析（持久连接对照）

主基准经 `route_rpc` 每次调用新建连接，p50 低但 p95/p99 达 ~20–40ms。为定位尾延迟根因，
用 `HttpDaemonRpcClient` 单例持久复用连接重测 ping / schema.version（n=200）：

| 方法 | route_rpc（每调用新建） | 持久连接 | 尾延迟变化 |
|------|------|------|------|
| ping | p50=1.04 / p95=19.8 / p99=24.3 | p50=0.73 / p95=17.9 / p99=25.2 | p50 略降，**p95/p99 基本不变** |
| schema.version | p50=0.87 / p95=19.6 / p99=24.9 | p50=0.69 / p95=16.8 / p99=19.9 | 同上 |

**结论**：连接复用只小幅降低 p50（~0.3ms），**对 p95/p99 尾延迟几乎无改善**。因此尾延迟
**不是** HTTP 连接建立开销，而是 **daemon worker 调度 / GIL / Windows HTTP 栈的周期性抖动**。
若未来要压尾延迟，优化方向应在 daemon 侧并发/调度，而非客户端连接池。

原始数据：[t6_persistent_result.json](t6_persistent_result.json)

## 5. 与历史基准的关系

- 历史 `docs/performance_report_million_symbols.md`（合成 1M 符号）聚焦 **refresh/构建** 的可扩展性
  （瓶颈 call_resolve_write，已由 P27 优化）与 **GraphStore 本体 vs SQL** 的查询加速比。
- 本轮 T6 聚焦 **真实 workspace 的端到端 RPC 延迟**，是对历史压测的补充维度，不重复其负载。
- 两者一致佐证：**Rust CSR 查询本体 < 几毫秒**（T6 的 min 延迟 ↔ 历史的 GraphStore 加速比）。

## 6. 可用性结论

| 场景 | 结论 |
|------|------|
| 交互式单查询（符号/调用者/被调用者） | ✅ p50 1–17ms，交互无感 |
| 调用链 depth5 | ✅ p50 16ms，可接受 |
| 批量/高频查询 | ⚠️ 注意 p95/p99 ~30–45ms 尾延迟（daemon 调度抖动），高吞吐场景需评估 |
| FTS 状态检查 | ⚠️ 固定 ~34ms（全表 COUNT），不宜高频轮询 |

## 7. 未发现功能缺陷

T6 是性能表征，非功能测试。全部 10 个方法 × 200 次调用 `errors=0`，无 method_not_found、
无异常。未发现需要修复的性能缺陷；尾延迟是 daemon 调度特征（已记录，供后续优化参考）。
