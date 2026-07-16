# Changelog

本文件记录 Call Warden 的版本演化。版本号对应数据库 Schema 版本。

## [v37] - 2026-07-16

### Added
- **L2：破坏性 git 操作拦截**
  - 新增 `destructive_operations` 表：记录 force push 等破坏性 git 操作
  - 新增 `cw git check-push <local_ref> <local_sha> <remote_ref> <remote_sha>` 子命令：pre-push hook 调用，检测 force push 并记录（软门禁，仅记录不阻断）
  - 新增 `cw git destructive-log [limit] [--type <TYPE>]` 子命令：查询破坏性 git 操作历史
- **L3：pre-commit hook task_id 验证**
  - 新增 `cw git check-task` 子命令：pre-commit hook 调用，检查 active task 是否存在（软门禁，仅警告不阻断）
- **Schema v37 升级**：新增 `destructive_operations` 表，Schema 从 v36 升级到 v37

## [v14] - 2026-07-03

### Added
- **Java GC 机制**：归档/复活/状态/清除四大功能
  - 新增 `analyzers/ignore_spec.py`：.gitignore 完整语法解析器（IgnoreMatcher）
    - 支持 `!` 取反 / `**` 递归 / `/` 锚定 / 目录后缀 / `\#` 转义
  - 新增 `db/db_gc.py`：GCMixin（gc_archive / gc_restore / gc_status / gc_purge）
  - 新增 `archived_files` 表 + 4 索引
  - 默认基线规则 30+ 条（autogen / prebuilt / build output / proto 生成等）
  - build 末尾自动触发 Young GC（force=False 增量扫描 pending）
  - CLI `gc archive/restore/status/purge` 子命令
  - repo manifest 自动检测（AOSP 多仓库注册为 workspace）
- 9 个 GC 专项测试用例

### Performance
- PRAGMA 优化：WAL / synchronous=NORMAL / cache_size=64MB / mmap_size=256MB
- `executemany` 批量写入替代循环 `execute`
- `INSERT OR IGNORE` 替代 SELECT-then-INSERT
- 10w 符号构建 2.36 秒，1w 符号批量插入 0.23 秒

## [v13] - 2026-07-03

### Added
- LSP 集成（LSPMixin）：hover / 定义 / 引用 / 诊断 / 补全
  - 支持 pyright / tsserver / gopls / rust-analyzer
- 跨仓库分析（CrossRepoMixin）：依赖检测 + 共享符号 + 影响传播
- 安全修复 SEC-001 ~ SEC-007：
  - 原子文件写入（临时文件 + os.replace）
  - LSP 子进程安全（命令白名单 + 超时 + 输出限制）
  - 错误日志路径消毒
- 项目级数据库隔离：`$HOME/.callwarden/<16位hash>/callwarden.db`
- CI/CD 集成：sarif_exporter / incremental / pr_check + GitHub Actions
- 压力测试（test_stress.py）+ Fuzz 安全测试（test_fuzz.py）
- 用户文档（docs/ 目录 7 份）

## [v12] - 2026-07-03

### Added
- 安全文件编辑（EditSafetyMixin / propose_edit）
  - SHA-256 校验 + 原子写入 + 审计日志 + 可回滚
  - `file_edit_audit` 表
- 检查门禁（CheckGateMixin）
- 结构化指令（task_steps.check_items JSON 增强）

## [v11] - 2026-07-03

### Added
- Token 节省账本（TokenSavingsMixin）
  - `token_savings_ledger` 表
  - @track_token_savings 装饰器自动统计
- AI 摘要管理（SummaryMixin）：generate_summary / project_brief / repo_map
- 所有权分析（OwnershipMixin）：CODEOWNERS + git blame + who_to_ask
- 覆盖率导入（CoverageMixin）：LCOV / Cobertura + test_impact_selection

## [v10] - 2026-07-02

### Added
- **Guardian 架构（四大支柱）**
  - 生产安全护栏（GuardrailMixin）：DB/API/Incident 三类可阻断规则
  - 变更影响智能（ImpactMixin）：blast_radius + cross_layer_impact
  - 代码演化智能（EvolutionMixin）：变更频率 + 缺陷关联 + 热点排名
  - 缺陷知识库（DefectKbMixin）：模式挖掘 + 修复建议
- 5 张 Guardian 表 + 16 个 MCP 工具
- 任务驱动编排增强：task_next_step 自动触发 guardrail_check_edit
- Before-Edit Contract：编辑前阻断式检查

## [v9] - 2026-07-02

### Added
- 任务管理系统（TasksMixin）：task_create / task_next_step / task_report_step / task_rollback
- 6 个任务管理 MCP 工具
- 分支感知（BranchMixin）：独立工作区方案 + diff_branches

## [v8] - 2026-07-01

### Added
- 向量搜索（VectorMixin）：sqlite-vec + sentence-transformers
- 语义搜索（semantic_search）：自然语言查找函数
- ask_codebase RAG 管道（关键词回退 + 上下文拼接）

## [v7] - 2026-07-01

### Added
- Git 集成（GitMixin）：import_git_history + 符号级变更追踪
- git_commits / git_file_changes / git_symbol_changes 表

## [v6] - 2026-06-30

### Added
- 代码度量（MetricsMixin）：圈复杂度 / 耦合度 / 扇入扇出 / 健康评分
- AI Agent 健康检查：check_file_health 警告 Token 溢出风险

## [v5] - 2026-06-30

### Added
- Semgrep 集成（IssueAnalyzerMixin）：多语言静态安全扫描
- semgrep_findings / semgrep_scans 表
- 按严重度/语言/规则过滤

## [v4] - 2026-06-29

### Added
- 注释恢复（CommentMixin）：从历史版本恢复丢失注释
- 单函数（fn@vN / fn@hash）+ 批量恢复 + 预览模式

## [v3] - 2026-06-28

### Changed
- 重构为 hash 为主、path 为副的双层存储
- workspaces / file_contents / file_instances / symbols / calls 表
- file_versions / file_symbol_versions / call_versions 版本表
- is_deleted 删除标记
- 多工作区隔离

## [v2] - 2026-06-27

### Added
- 多语言并行解析（ThreadPoolExecutor 8 线程）
- 9 种语言：Rust / TypeScript / JavaScript / Python / Kotlin / Go / Java / C / C++

## [v1] - 2026-06-26

### Added
- 项目初始化
- 基础符号提取 + 调用关系（Rust 单语言）
- SQLite 存储 + Schema 版本化迁移
