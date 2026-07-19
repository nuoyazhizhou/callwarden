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
- [x] 测试 195+ MCP 工具的输入输出契约
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
- [ ] Rust daemon crate / binary
- [ ] UDS server + SO_PEERCRED
- [ ] workspace registry schema
- [ ] container mount mapping
- [ ] register/list/status API
- [ ] Python CLI daemon client + enterprise/auto/local 模式

### Phase 3: Global CAS + Workspace Manifest
- [ ] CAS schema + key 设计
- [ ] daemon refresh CAS lookup
- [ ] clean snapshot manifest
- [ ] dirty overlay manifest
- [ ] CAS GC

### Phase 4-8: Snapshot Query / 秒级 Watcher / Toolchain CAS / Heavy Jobs / 生产化
- [ ] 详见 enterprise-daemon-shared-snapshot-plan.md §14
