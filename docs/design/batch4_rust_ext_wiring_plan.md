# 批次4：Rust 扩展接线计划（M4/M5/M6/M8/L7）

> 状态：**已完成**（2026-07-20 批次4 文档对齐；2026-08-03 补测试与本文档）
> 对应 _feature_matrix.md 批次4 条目：M4 / M5 / M6 / M8 / L7

## 背景

批次4 目标：将已实现的 Rust 扩展模块（delta / frontier / metrics / watcher）接入
daemon 生产路径（workspace.rs committed 路径），并落地 server 侧共享指标模块（L7）。

接线前这些模块仅在 lib.rs 注册 PyO3 导出，未在生产调用链中被调用（"已实现未接入"）。

## 接线方案

### M4：parse delta 接入 committed 路径

- 位置：`rust_ext/src/daemon/workspace.rs` `handle_workspace_file_refresh` committed 分支
- 调用：`DeltaComputer::compute_parse_delta(&abs_file_path, None)`（store=None，纯 tree-sitter 解析）
- 写入：`StagingEntry.parse_delta`（file_path / language / content_hash / total_lines /
  symbols_added / symbols_removed / symbols_changed / calls_added / calls_removed / summary）
- 失败处理：写入 `{"error": ...}` 摘要，不阻塞 `staging_log.append`
- 模块：`rust_ext/src/lib.rs` 已注册 `mod delta;`

### M5：frontier 接入 committed 路径

- 位置：同一 committed 分支，紧随 M4
- 调用：`FrontierComputer::compute_frontier_with_budget(&parse_delta, None, QueryBudget::default())`
  （store=None 退化 + 默认预算）
- 写入：`StagingEntry.frontier`（directly_affected_count / upstream_direct_count /
  downstream_direct_count / upstream_transitive_count / downstream_transitive_count /
  partial / directly_affected(前 50) / summary）
- 模块：`rust_ext/src/lib.rs` 已注册 `mod frontier;` 并导出
  `frontier::compute_frontier_with_budget`

### M6：metrics update 接入 committed 路径

- 位置：同一 committed 分支，紧随 M5
- 调用：`MetricsComputer::compute_local_update(&frontier, &parse_delta, None, 2)`
  （store=None 退化，impact_depth=2）
- 写入：`StagingEntry.metrics_update`（is_empty / depth_changes_count /
  cycle_changes_count / impact_changes_count）
- 模块：`rust_ext/src/lib.rs` 已注册 `mod metrics;` 并导出 `metrics::compute_local_update`

### M8：文件监听 watcher

- 模块：`rust_ext/src/watcher.rs`（notify crate 跨平台监听，16+ 语言扩展名过滤，
  crossbeam channel 事件传递，支持 stop 优雅停止）
- PyO3 导出：`lib.rs` 注册 `watcher::PyFileWatcher`（实时）+ `watcher::PyDebouncedFileWatcher`
  （debounce + batch coalescing，Phase 5.1）
- 事件流：`notify crate → raw event → extension filter → channel → Python 消费`

### L7：server 侧共享指标

- 文件：`server/shared_benefit_metrics.py`
- 内容：`CASMetrics` / `ParseMetrics` / `SnapshotMetrics` / `RefreshLatency` 等指标类，
  供 watcher / daemon 刷新路径共享采集

## 接线后行为

`handle_workspace_file_refresh` committed 分支执行顺序：

1. 计算 parse_delta（M4）
2. 计算 frontier（M5，默认预算）
3. 计算 metrics_update（M6，impact_depth=2）
4. `staging_log.append(&mut entry)` → 触发 replicate（G11），按配置发布 snapshot

## 测试

- `tests/test_b4_rust_ext_wiring.py`：源码字符串断言（不依赖 cargo build，Windows 可跑）
  - TestM4ParseDeltaWiring：`DeltaComputer::compute_parse_delta` 调用 + `entry.parse_delta` 写入
  - TestM5FrontierWiring：`compute_frontier_with_budget` 调用 + `entry.frontier` 写入 + lib.rs 导出
  - TestM6MetricsWiring：`compute_local_update` 调用 + `entry.metrics_update` 写入 + lib.rs 导出
  - TestM8WatcherWiring：watcher.rs 存在 + notify 依赖 + PyO3 双导出
  - TestL7SharedMetrics：shared_benefit_metrics.py 存在 + 三个指标类
- 运行：`python -m pytest tests/test_b4_rust_ext_wiring.py -q`（14 个测试全通过）

## 已知边界（如实标注）

- query 侧通用 budget 参数化未做（committed 路径使用 `QueryBudget::default()` 退化）；
  需要精细预算控制的场景留待后续
- watcher 的 debounce 聚合在 `PyDebouncedFileWatcher`（Phase 5.1），committed 路径未直接依赖
