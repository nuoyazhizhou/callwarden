# T3 执行证据（T-1795832915536-9e838eec / REQ-RG-02）

- 卡片：T-1795832915536-9e838eec（REQ-RG-02：SnapshotCache 真实 LRU 空闲驱逐与字节预算，3 steps）
- executor：callwarden-alm-executor-zhengdx / sess-executor-zhengdx-t1-20261128
- lease：L-8ef87e45699e66ec（fencing_counter=1）

## Steps 完成度（3/3）

| step | step_id | 符号 | commit | 状态 |
|---|---|---|---|---|
| #0 | S-1795832915543-9eed8450 | GraphStore::approximate_bytes | d898bcc | done |
| #1 | S-1795832915543-9eee8bfc | SnapshotCache touch + 真 LRU | a0f1c4c | done |
| #2 | S-1795832915543-9eeee638 | evict_idle + approx_bytes + Arc 语义 | 032f131 | done |

## 实现要点

1. **C3 approximate_bytes**（graph.rs）：四项求和——by_id(+衍生排序/搜索索引数组) + CSR forward/backward 边(+偏移/位置/名字索引/roots) + 4 字符串池 + 文件路径表；Vec/String 取 capacity()（真实堆分配）；空 store=0。O3 口径假设登记于 `.design/daemon-resource-governance/evidence/o3-approximate-bytes-calibration.md`（capacity vs len、不含 struct 自身/Arc 头/padding、衍生索引计入 by_id 项、上界推导 <10×）。
2. **C2 touch + 真 LRU**（snapshot.rs）：cache 侧 `last_access: RwLock<HashMap<ws, Instant>>`（与 managers 同步增删）；`touch(ws)` 更新 last_access=now + 条目移到 order 尾部，不存在无操作；`get_or_create` 命中路径调用 touch（修正「注释称 LRU 实为 FIFO」）；创建路径插入即访问；evict 同步清理 last_access；锁序 `managers → order → last_access` 全路径统一（防交叉死锁，注释留痕）。
3. **C2 evict_idle + approx_bytes**（snapshot.rs）：`evict_idle(idle: Duration) -> Vec<String>` 驱逐 now-last_access>idle 并返回列表（eprintln 日志；ZERO=全量清冷，C6 压力路径语义）；`approx_bytes() -> u64` 各 ws 已发布 snapshot 的 GraphStore::approximate_bytes() 求和（未发布跳过=0，历史 generation 不计）；Arc 语义由 AC-RG-02-5 单测锁定。

## 测试实证

命令：`CARGO_BUILD_JOBS=2 cargo test --manifest-path rust_ext/Cargo.toml --lib snapshot::tests` → **22 passed; 0 failed**
命令：`CARGO_BUILD_JOBS=2 cargo test --manifest-path rust_ext/Cargo.toml --lib graph::tests` → **3 passed; 0 failed**

- AC-RG-02-1（空闲驱逐）：`ac_rg_02_1_evict_idle_short_timeout`——30ms 空闲 vs 10ms 超时，驱逐后 len 下降 + 返回列表含该 ws。
- AC-RG-02-3（访问序驱逐）：`ac_rg_02_3_eviction_by_access_order_not_insertion`——A 持续 touch、B/C 不访问，插入 D → B 被驱逐（FIFO 会错杀 A）。
- AC-RG-02-5（in-flight 不破坏）：`ac_rg_02_5_guard_survives_evict`——load() Guard 存活时 evict，Guard 仍可读 generation，查询结束 drop 后才释放。
- AC-RG-02-4 前置（估值聚合）：`approx_bytes_sums_published_stores`——2 ws 同规模 store，total == single×2，驱逐后回落。
- 其余：touch 顺序/无操作语义、命中路径访问序、evict 清理、保留新近条目、ZERO 全清、未发布=0、空 store=0、上下界、单调性。

## 与 contracts.md / 卡面的偏差（如实登记）

1. **graph.rs +9（#[cfg(test)] pub(crate) with_tables）**：step#2 target_file 白名单仅 snapshot.rs；approx_bytes 聚合单测需要带已知数据的 GraphStore，而 GraphStore 无轻量装配路径（load_from_sqlite 需真实 DB）。新增测试专用装配函数（不进生产二进制），随 032f131 入库，此处登记。
2. **O3 口径假设**（step#0，已单独登记 o3-approximate-bytes-calibration.md）：capacity 口径、衍生索引计入、不含 struct/Arc 头。
3. **evict_idle 候选判定取 last_access 与 managers 交集**：防 evict 残留 stale 条目被误驱逐（防御性，契约未要求但与既有 stale 兜底风格一致）。
4. **approx_bytes 只算 current 不算 history generation**：C2 字面「当前 snapshot」，历史归 gc_generations 管。
5. **evict_idle 驱逐日志用 eprintln**：与卡面「驱逐含日志」及 daemon 现有 eprintln 风格一致；结构化指标留 T5（evictions_total 计数在 C6）。

## 边界与未覆盖

- evict_idle 的并发正确性由锁序约定保证（单锁粒度测试），未做多线程压力测试（T4 集成测试 AC-RG-02-6 单飞覆盖并发面）。
- approx_bytes 的 ±20% 精度依赖 O3 假设，真实大库校准留 T5 字节预算驱逐联调。
- get_store_or_load 接线（C4）属 T4，本单未实现。
