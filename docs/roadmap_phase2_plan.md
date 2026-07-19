# 产品路线图第二阶段

## 集成测试全流程

- [x] 设计集成测试场景：init → register workspace → refresh-all → task create → task next → edit file → git commit → capture-diff --auto → check-gate → task apply → task close
- [x] 编写 tests/test_integration_full_flow.py 覆盖完整闭环
- [x] 覆盖多语言混合项目场景（Python + TS + Rust）
- [x] 验证 audit_chain 签名完整性贯穿全流程
- [x] 验证 task_symbol_changes 关联正确
- [x] 运行全量回归确保无回归（2026-07-19：6/6 通过 in 79.85s）

## 千万级符号性能验证

- [ ] 准备大规模测试数据（模拟 100K / 1M / 10M 符号级别，用脚本生成）
- [ ] 测量 refresh-all 耗时随符号量增长的趋势
- [ ] 测量核心查询性能（search / call-chain / impact / clone detect）在千万级符号下的表现
- [ ] 测量 SQLite 数据库文件大小与内存占用
- [ ] 识别性能瓶颈（索引缺失 / 查询全表扫描 / O(n²) 算法）
- [ ] 输出性能报告并记录优化方向

## B2 AST 缓存激活

- [ ] 在 db_build.py 增量判断中调用 _read_ast_cache 参与决策
- [ ] 实现 ast_cache 元数据驱动的跨进程增量复用
- [ ] 编写测试验证 ast_cache 实际被读取并影响增量决策
- [ ] 更新 architecture.md 文档说明 AST 缓存两层架构

## 统一项目健康报告

- [ ] 设计 cw health-report 子命令（输出 markdown 格式报告）
- [ ] 聚合 comment_coverage + uncommented + complexity + issues + coupling 为统一报告
- [ ] 按模块分组输出问题清单
- [ ] 支持 --output file 参数导出报告文件
- [ ] 编写 tests/test_health_report.py
- [ ] 更新 cli_reference.md 文档

## MCP Server 测试

- [x] 编写 MCP Server 启动与协议握手测试
- [x] 测试 195+ MCP 工具的输入输出契约
- [x] 测试 MCP 与 CLI 并发访问（WAL 模式下读写并发安全验证）
- [x] 测试 MCP 长连接稳定性（长时间空闲后恢复）
- [x] 编写 tests/test_mcp_server_full.py（2026-07-19：15/15 通过 in 6.72s）

## B1 Clone Detection LSH 增强

- [ ] 实现 MinHash + LSH 索引替代符号名前 3 字符分组剪枝
- [ ] 支持跨命名克隆检测（processOrder vs handleOrder）
- [ ] 优化 O(n²) 为 O(n) 近似最近邻
- [ ] 编写测试验证召回率与精确率
- [ ] 更新 architecture.md 与 cli_reference.md

## Clone Detection 影响分析联动

- [ ] 修改 blast_radius / get_impact，检测到受影响符号有克隆时自动提示同步修改
- [ ] 新增 get_clone_aware_impact MCP 工具
- [ ] 编写测试验证联动逻辑
- [ ] 更新 mcp_tools.md 文档

## 扩展 Git Hook 到 AI CLI IDE

- [ ] 调研知名 AI CLI（Claude Code / Cursor / Aider / Continue / GitHub Copilot CLI）的 hook 机制
- [ ] 设计 hook 适配层统一接口
- [ ] 实现 Claude Code 适配
- [ ] 实现 Cursor 适配
- [ ] 编写测试验证适配器
- [ ] 更新文档

## 15 种语言开源项目测试

- [ ] 为每种语言选择代表性开源项目作为测试 fixture
- [ ] 编写 tests/test_language_<lang>.py 验证符号提取与调用关系
- [ ] 验证注释覆盖率统计准确性
- [ ] 记录每种语言的已知限制与边界情况
- [ ] 更新 docs/language_support.md 文档

## Enterprise Daemon 架构演进（基线 v10.2）

> 基于 [enterprise-daemon-shared-snapshot-plan.md](design/enterprise-daemon-shared-snapshot-plan.md) 的 9 Phase 实施路线图。
> 4 份短规范已就位：[parse-input-abi](design/parse-input-abi.md) / [cas-gc-protocol](design/cas-gc-protocol.md) / [watcher-generation-state-machine](design/watcher-generation-state-machine.md) / [daemon-ipc-security](design/daemon-ipc-security.md)。

### Phase 0: 文档与边界收敛
- [x] 明确 Python/Rust 职责边界（§5.3 禁止交叉矩阵）
- [x] 将 enterprise-daemon-shared-snapshot-plan.md 设为主设计（Baseline）
- [x] 在 roadmap 中增加 enterprise daemon epic
- [x] 标记 rust_daemon_architecture.md 已过时的描述

### Phase 1: Rust 多语言 parse 接入主 refresh 路径
- [ ] 按 language 对 to_parse 分组
- [ ] Rust 支持语言调用 batch_parse_files_lang_pool
- [ ] 不支持语言回退 Python parser
- [ ] 保留 CW_DISABLE_RUST_PARSE
- [ ] 增加 parse alignment smoke tests
- [ ] benchmark 验证 Python ProcessPool 退出主路径

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
