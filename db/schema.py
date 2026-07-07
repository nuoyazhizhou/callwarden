"""
schema.py
=========

数据库 Schema 定义。
"""

SCHEMA_SQL = """
-- 工作区表：管理多个工作区（不同目录/不同分支）
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    root_path TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    is_active INTEGER DEFAULT 0,
    description TEXT DEFAULT ''
);

-- 文件内容表：按 content_hash 唯一存储，相同内容只存一次（hash 为主）
CREATE TABLE IF NOT EXISTS file_contents (
    content_hash TEXT PRIMARY KEY,
    language TEXT DEFAULT '',
    total_lines INTEGER DEFAULT 0,
    first_seen_at REAL NOT NULL
);

-- 文件实例表：一个内容可以出现在多个工作区的多个路径（path 为副）
CREATE TABLE IF NOT EXISTS file_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    abs_path TEXT NOT NULL,
    current_content_hash TEXT DEFAULT '',
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    last_parsed REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    module_path TEXT DEFAULT '',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (current_content_hash) REFERENCES file_contents(content_hash),
    UNIQUE(workspace_id, rel_path)
);

-- 符号表（当前快照，查询优化用）
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    visibility TEXT DEFAULT 'private',
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    start_col INTEGER DEFAULT 0,
    end_col INTEGER DEFAULT 0,
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    comment_status TEXT DEFAULT 'pending',
    module_path TEXT DEFAULT '',
    qualified_name TEXT DEFAULT '',
    depth INTEGER DEFAULT -1,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);

-- 调用关系表（当前快照，查询优化用）
CREATE TABLE IF NOT EXISTS calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    caller_id INTEGER NOT NULL,
    caller_name TEXT NOT NULL,
    caller_module TEXT NOT NULL,
    callee_name TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT '',
    callee_file TEXT DEFAULT '',
    callee_id INTEGER DEFAULT 0,
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0,
    FOREIGN KEY (caller_id) REFERENCES symbols(id)
);

-- 注释记录表（按 symbol_hash 关联，同一函数注释全局共享）
CREATE TABLE IF NOT EXISTS comments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_hash TEXT NOT NULL,
    comment_type TEXT DEFAULT 'doc',
    content TEXT DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);

-- 文件版本表：记录每个文件实例的所有历史版本
CREATE TABLE IF NOT EXISTS file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    version_num INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    parsed_at REAL NOT NULL,
    is_current INTEGER DEFAULT 1,
    is_deleted INTEGER DEFAULT 0,
    commit_hash TEXT DEFAULT '',
    ast_cache BLOB DEFAULT NULL,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    FOREIGN KEY (content_hash) REFERENCES file_contents(content_hash)
);

-- 符号内容表：按 content_hash 唯一，相同内容只存一次（去重）
CREATE TABLE IF NOT EXISTS symbol_contents (
    content_hash TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    content TEXT NOT NULL,
    signature TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    comment_content TEXT DEFAULT '',
    qualified_name TEXT DEFAULT ''
);

-- 文件-符号关联表：记录每个文件版本包含哪些符号及其位置
CREATE TABLE IF NOT EXISTS file_symbol_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    module_path TEXT DEFAULT '',
    depth INTEGER DEFAULT -1,
    is_deleted INTEGER DEFAULT 0,
    FOREIGN KEY (file_version_id) REFERENCES file_versions(id),
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);

-- 调用关系版本表：记录每个文件版本对应的调用关系
CREATE TABLE IF NOT EXISTS call_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_version_id INTEGER NOT NULL,
    caller_qualified TEXT NOT NULL,
    caller_hash TEXT DEFAULT '',
    callee_name TEXT NOT NULL,
    callee_module TEXT DEFAULT '',
    callee_qualified TEXT DEFAULT '',
    callee_file TEXT DEFAULT '',
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0,
    FOREIGN KEY (file_version_id) REFERENCES file_versions(id)
);

-- Semgrep 缺陷表：存储 Semgrep 扫描发现的问题（按内容去重）
CREATE TABLE IF NOT EXISTS semgrep_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    content_hash TEXT DEFAULT '',
    rule_id TEXT NOT NULL,
    rule_name TEXT DEFAULT '',
    message TEXT DEFAULT '',
    severity TEXT DEFAULT 'INFO',
    confidence TEXT DEFAULT 'UNKNOWN',
    language TEXT DEFAULT '',
    start_line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    snippet TEXT DEFAULT '',
    fix TEXT DEFAULT '',
    symbol_id INTEGER DEFAULT 0,
    symbol_qualified TEXT DEFAULT '',
    scanned_at REAL DEFAULT 0,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    UNIQUE(content_hash, rule_id, start_line)
);

-- 缺陷扫描记录表：记录每次 Semgrep 扫描的元信息
CREATE TABLE IF NOT EXISTS semgrep_scans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scan_type TEXT DEFAULT 'full',
    config TEXT DEFAULT '',
    workspace_id INTEGER DEFAULT 0,
    started_at REAL NOT NULL,
    completed_at REAL DEFAULT 0,
    total_findings INTEGER DEFAULT 0,
    files_scanned INTEGER DEFAULT 0,
    status TEXT DEFAULT 'running'
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_workspaces_active ON workspaces(is_active);
CREATE INDEX IF NOT EXISTS idx_file_contents_lang ON file_contents(language);
CREATE INDEX IF NOT EXISTS idx_file_instances_workspace ON file_instances(workspace_id);
CREATE INDEX IF NOT EXISTS idx_file_instances_hash ON file_instances(current_content_hash);
CREATE INDEX IF NOT EXISTS idx_file_instances_relpath ON file_instances(rel_path);
CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_symbols_hash ON symbols(symbol_hash);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
CREATE INDEX IF NOT EXISTS idx_symbols_kind ON symbols(kind);
CREATE INDEX IF NOT EXISTS idx_symbols_qualified ON symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_symbols_module ON symbols(module_path);
CREATE UNIQUE INDEX IF NOT EXISTS idx_symbols_unique ON symbols(file_instance_id, name, start_line);
CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id);
CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name);
CREATE INDEX IF NOT EXISTS idx_calls_callee_qualified ON calls(callee_qualified);
CREATE INDEX IF NOT EXISTS idx_comments_hash ON comments(symbol_hash);
CREATE INDEX IF NOT EXISTS idx_file_versions_instance ON file_versions(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_file_versions_hash ON file_versions(content_hash);
CREATE INDEX IF NOT EXISTS idx_file_versions_current ON file_versions(is_current);
CREATE INDEX IF NOT EXISTS idx_file_symbol_versions_file ON file_symbol_versions(file_version_id);
CREATE INDEX IF NOT EXISTS idx_file_symbol_versions_hash ON file_symbol_versions(symbol_hash);
CREATE INDEX IF NOT EXISTS idx_file_symbol_versions_qualified ON file_symbol_versions(qualified_name);
CREATE INDEX IF NOT EXISTS idx_call_versions_file ON call_versions(file_version_id);
CREATE INDEX IF NOT EXISTS idx_call_versions_caller ON call_versions(caller_qualified);
CREATE INDEX IF NOT EXISTS idx_semgrep_instance ON semgrep_findings(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_semgrep_hash ON semgrep_findings(content_hash);
CREATE INDEX IF NOT EXISTS idx_semgrep_rule ON semgrep_findings(rule_id);
CREATE INDEX IF NOT EXISTS idx_semgrep_severity ON semgrep_findings(severity);
CREATE INDEX IF NOT EXISTS idx_semgrep_symbol ON semgrep_findings(symbol_qualified);
CREATE INDEX IF NOT EXISTS idx_semgrep_language ON semgrep_findings(language);

-- ============================================
-- v4: Git 集成表
-- ============================================

CREATE TABLE IF NOT EXISTS git_commits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_hash TEXT UNIQUE NOT NULL,
    message TEXT DEFAULT '',
    author TEXT DEFAULT '',
    email TEXT DEFAULT '',
    timestamp REAL NOT NULL,
    workspace_id INTEGER NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE TABLE IF NOT EXISTS git_file_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_hash TEXT NOT NULL,
    file_instance_id INTEGER NOT NULL,
    change_type TEXT NOT NULL,
    old_content_hash TEXT DEFAULT '',
    new_content_hash TEXT DEFAULT '',
    FOREIGN KEY (commit_hash) REFERENCES git_commits(commit_hash),
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
);

CREATE TABLE IF NOT EXISTS git_symbol_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commit_hash TEXT NOT NULL,
    symbol_hash TEXT NOT NULL,
    change_type TEXT NOT NULL,
    old_content TEXT DEFAULT '',
    new_content TEXT DEFAULT '',
    FOREIGN KEY (commit_hash) REFERENCES git_commits(commit_hash),
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);

-- file_versions 表的 commit_hash 字段（v4 新增）
-- 注意：新建数据库时直接包含该字段，迁移时用 ALTER TABLE 添加

-- Git 相关索引
CREATE INDEX IF NOT EXISTS idx_git_commits_hash ON git_commits(commit_hash);
CREATE INDEX IF NOT EXISTS idx_git_commits_workspace ON git_commits(workspace_id);
CREATE INDEX IF NOT EXISTS idx_git_commits_timestamp ON git_commits(timestamp);
CREATE INDEX IF NOT EXISTS idx_git_file_changes_commit ON git_file_changes(commit_hash);
CREATE INDEX IF NOT EXISTS idx_git_file_changes_file ON git_file_changes(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_git_symbol_changes_commit ON git_symbol_changes(commit_hash);
CREATE INDEX IF NOT EXISTS idx_git_symbol_changes_symbol ON git_symbol_changes(symbol_hash);
CREATE INDEX IF NOT EXISTS idx_file_versions_commit ON file_versions(commit_hash);

-- ============================================
-- v5: 向量嵌入表（sqlite-vec）
-- ============================================

-- 向量嵌入表（函数代码的语义向量）
CREATE TABLE IF NOT EXISTS symbol_embeddings (
    symbol_hash TEXT PRIMARY KEY,
    embedding BLOB NOT NULL,
    model_version TEXT NOT NULL DEFAULT 'jina-v2-base-code',
    dim INTEGER NOT NULL DEFAULT 768,
    embedded_at REAL NOT NULL,
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);
CREATE INDEX IF NOT EXISTS idx_embeddings_model ON symbol_embeddings(model_version);

-- ============================================
-- v6: 符号摘要表（AI 生成的函数/模块摘要，版本化）
-- ============================================

-- 符号摘要表（AI 生成的函数/模块摘要，版本化）
CREATE TABLE IF NOT EXISTS symbol_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_hash TEXT NOT NULL,
    summary TEXT NOT NULL,
    model TEXT DEFAULT 'manual',
    version INTEGER NOT NULL DEFAULT 1,
    is_current INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);
CREATE INDEX IF NOT EXISTS idx_summaries_hash ON symbol_summaries(symbol_hash);
CREATE INDEX IF NOT EXISTS idx_summaries_current ON symbol_summaries(is_current);

-- ============================================
-- v7: 任务管理表（任务驱动 MCP 的核心）
-- ============================================

-- 任务表：管理 agent 创建的结构化任务（支持父子任务树）
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT DEFAULT '',
    creator TEXT NOT NULL DEFAULT 'agent',
    status TEXT NOT NULL DEFAULT 'open',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    applied_at REAL,
    closed_at REAL,
    parent_id TEXT DEFAULT '',
    depth INTEGER NOT NULL DEFAULT 0,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- 任务步骤表：每个任务包含多个有序步骤
CREATE TABLE IF NOT EXISTS task_steps (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_index INTEGER NOT NULL,
    action TEXT NOT NULL,
    target_file TEXT DEFAULT '',
    target_symbol TEXT DEFAULT '',
    check_items TEXT DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    result TEXT DEFAULT '',
    created_at REAL NOT NULL,
    completed_at REAL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_steps_task ON task_steps(task_id);
CREATE INDEX IF NOT EXISTS idx_task_steps_status ON task_steps(status);

-- 变更审计日志表：记录每个步骤对文件的修改（hash + diff）
CREATE TABLE IF NOT EXISTS change_audit (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    step_id TEXT,
    file_path TEXT NOT NULL,
    hash_before TEXT DEFAULT '',
    hash_after TEXT DEFAULT '',
    diff TEXT DEFAULT '',
    author TEXT NOT NULL DEFAULT 'agent',
    timestamp REAL NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_audit_task ON change_audit(task_id);

-- ============================================
-- v8: 文件所有权表
-- ============================================

-- 文件所有权表：记录每个文件实例的负责人（来源 CODEOWNERS / git blame）
CREATE TABLE IF NOT EXISTS file_ownership (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    owner TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'codeowners',
    confidence REAL DEFAULT 1.0,
    last_commit_hash TEXT DEFAULT '',
    last_commit_author TEXT DEFAULT '',
    last_commit_time REAL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
);
CREATE INDEX IF NOT EXISTS idx_ownership_file ON file_ownership(file_instance_id);

-- ============================================
-- v9: 覆盖率数据表（LCOV / Cobertura 导入的行级覆盖率）
-- ============================================

-- 覆盖率数据表：存储从 LCOV/Cobertura 报告导入的行级覆盖率数据
CREATE TABLE IF NOT EXISTS coverage_data (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    symbol_id INTEGER,
    line_start INTEGER NOT NULL,
    line_end INTEGER NOT NULL,
    hit_count INTEGER DEFAULT 0,
    report_source TEXT DEFAULT 'lcov',
    imported_at REAL NOT NULL,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    FOREIGN KEY (symbol_id) REFERENCES symbols(id)
);
CREATE INDEX IF NOT EXISTS idx_coverage_file ON coverage_data(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_coverage_symbol ON coverage_data(symbol_id);
CREATE INDEX IF NOT EXISTS idx_coverage_source ON coverage_data(report_source);

-- ============================================
-- v10: 守护者架构表（生产安全护栏 + 变更影响 + 演化智能 + 缺陷知识库）
-- ============================================

-- 安全护栏规则表：可阻断的规则定义
CREATE TABLE IF NOT EXISTS guardrail_rules (
    rule_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warn',
    pattern TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT 'warn',
    description TEXT DEFAULT '',
    is_builtin INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);

-- 安全护栏发现表：规则扫描结果
CREATE TABLE IF NOT EXISTS guardrail_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    symbol_hash TEXT DEFAULT '',
    severity TEXT NOT NULL DEFAULT 'warn',
    status TEXT NOT NULL DEFAULT 'open',
    message TEXT DEFAULT '',
    detected_at REAL NOT NULL,
    resolved_at REAL,
    FOREIGN KEY (rule_id) REFERENCES guardrail_rules(rule_id)
);
CREATE INDEX IF NOT EXISTS idx_guardrail_findings_rule ON guardrail_findings(rule_id);
CREATE INDEX IF NOT EXISTS idx_guardrail_findings_file ON guardrail_findings(file_path);
CREATE INDEX IF NOT EXISTS idx_guardrail_findings_severity ON guardrail_findings(severity);
CREATE INDEX IF NOT EXISTS idx_guardrail_findings_status ON guardrail_findings(status);

-- 变更影响记录表：跨层影响分析结果
CREATE TABLE IF NOT EXISTS change_impacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_symbol TEXT NOT NULL,
    impact_type TEXT NOT NULL,
    target_symbol TEXT NOT NULL,
    target_layer TEXT NOT NULL DEFAULT 'code',
    confidence REAL DEFAULT 1.0,
    detected_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_impacts_source ON change_impacts(source_symbol);
CREATE INDEX IF NOT EXISTS idx_change_impacts_layer ON change_impacts(target_layer);

-- 演化指标缓存表：函数变更频率、缺陷关联、热点评分
CREATE TABLE IF NOT EXISTS evolution_metrics (
    symbol_hash TEXT PRIMARY KEY,
    change_count INTEGER DEFAULT 0,
    defect_count INTEGER DEFAULT 0,
    hotspot_score REAL DEFAULT 0.0,
    first_seen REAL,
    last_changed_at REAL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (symbol_hash) REFERENCES symbol_contents(content_hash)
);
CREATE INDEX IF NOT EXISTS idx_evolution_hotspot ON evolution_metrics(hotspot_score);

-- 缺陷模式表：从历史缺陷中挖掘的模式库
CREATE TABLE IF NOT EXISTS defect_patterns (
    pattern_id TEXT PRIMARY KEY,
    category TEXT NOT NULL,
    description TEXT NOT NULL,
    detection_rule TEXT DEFAULT '',
    fix_template TEXT DEFAULT '',
    severity TEXT DEFAULT 'warn',
    learned_from TEXT DEFAULT '',
    case_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_defect_patterns_category ON defect_patterns(category);
CREATE INDEX IF NOT EXISTS idx_defect_patterns_severity ON defect_patterns(severity);

-- 缺陷修复案例表：历史修复记录
CREATE TABLE IF NOT EXISTS defect_fixes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pattern_id TEXT,
    symbol_hash TEXT DEFAULT '',
    before_hash TEXT DEFAULT '',
    after_hash TEXT DEFAULT '',
    fix_diff TEXT DEFAULT '',
    effectiveness REAL DEFAULT 0.0,
    created_at REAL NOT NULL,
    FOREIGN KEY (pattern_id) REFERENCES defect_patterns(pattern_id)
);
CREATE INDEX IF NOT EXISTS idx_defect_fixes_pattern ON defect_fixes(pattern_id);
CREATE INDEX IF NOT EXISTS idx_defect_fixes_symbol ON defect_fixes(symbol_hash);

-- v11: Token 节省账本表（记录每次 Agent 操作节省的 token 数）
CREATE TABLE IF NOT EXISTS token_savings_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,           -- 操作类型：rag_context / call_chain_summary / semantic_search / comment_restore / blast_radius
    workspace_id INTEGER,
    agent_task_id TEXT DEFAULT '',     -- 关联的任务 ID（可选）
    original_tokens INTEGER DEFAULT 0, -- 原始 token 数（无压缩时的估算值）
    actual_tokens INTEGER DEFAULT 0,   -- 实际使用的 token 数
    tokens_saved INTEGER DEFAULT 0,    -- 节省的 token 数 = original - actual
    savings_pct REAL DEFAULT 0.0,      -- 节省百分比
    detail TEXT DEFAULT '',            -- 详情 JSON（如涉及的符号数、文件数等）
    created_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE INDEX IF NOT EXISTS idx_token_savings_op ON token_savings_ledger(operation);
CREATE INDEX IF NOT EXISTS idx_token_savings_workspace ON token_savings_ledger(workspace_id);
CREATE INDEX IF NOT EXISTS idx_token_savings_created ON token_savings_ledger(created_at);

-- ============================================
-- v12: 安全文件编辑审计表（Agent OS 核心：propose_edit 安全编辑流水线）
-- ============================================

-- 文件编辑审计表：记录 Agent 通过 propose_edit 提交的每一次编辑
CREATE TABLE IF NOT EXISTS file_edit_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,
    file_path TEXT NOT NULL,           -- 文件相对路径
    operation TEXT NOT NULL,           -- edit / create / delete
    file_hash_before TEXT DEFAULT '',  -- 编辑前的文件 hash（SHA-256）
    file_hash_after TEXT DEFAULT '',   -- 编辑后的文件 hash
    symbol_hash TEXT DEFAULT '',       -- 关联的符号 hash（可选）
    agent_task_id TEXT DEFAULT '',     -- 关联的任务 ID
    diff_summary TEXT DEFAULT '',      -- 变更摘要（新增 N 行 / 删除 M 行）
    status TEXT DEFAULT 'pending',     -- pending / applied / reverted / failed
    created_at REAL NOT NULL,
    applied_at REAL DEFAULT 0,
    reverted_at REAL DEFAULT 0,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_edit_audit_file ON file_edit_audit(file_path);
CREATE INDEX IF NOT EXISTS idx_edit_audit_status ON file_edit_audit(status);
CREATE INDEX IF NOT EXISTS idx_edit_audit_task ON file_edit_audit(agent_task_id);

-- ============================================
-- v13: 跨仓库分析表（cross_repo_analysis）
-- ============================================

-- 跨仓库依赖表：记录仓库间的依赖关系（通过 import 语句匹配）
CREATE TABLE IF NOT EXISTS cross_repo_deps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_workspace_id INTEGER NOT NULL,   -- 源仓库 workspace_id
    target_workspace_id INTEGER NOT NULL,   -- 目标仓库 workspace_id
    dependency_type TEXT NOT NULL,          -- import / call / shared_symbol
    source_symbol_hash TEXT DEFAULT '',     -- 源符号 hash（调用方）
    target_symbol_hash TEXT DEFAULT '',     -- 目标符号 hash（被调用方）
    evidence TEXT DEFAULT '',               -- 证据（如 import 语句、调用位置）
    confidence REAL DEFAULT 0.0,            -- 置信度（0-1）
    detected_at REAL NOT NULL,
    FOREIGN KEY (source_workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (target_workspace_id) REFERENCES workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_cross_repo_source ON cross_repo_deps(source_workspace_id);
CREATE INDEX IF NOT EXISTS idx_cross_repo_target ON cross_repo_deps(target_workspace_id);
CREATE INDEX IF NOT EXISTS idx_cross_repo_type ON cross_repo_deps(dependency_type);

-- ============================================
-- v14: 归档表（类 Java GC 老年代）
-- ============================================
-- 当文件被 .gitignore / .callwardenignore 命中时，从主表迁出到此表。
-- 保留原 file_instance_id 和符号快照，便于取消 ignore 后复活（类 GC promotion/demotion）。
-- archived_files 与 file_instances 一对一：归档时 file_instances.status='archived'，
-- 同时把当时的 symbols/calls 快照迁到 archived_symbols/archived_calls（按需，当前仅记元数据）。
CREATE TABLE IF NOT EXISTS archived_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,             -- 原 file_instances.id（保留关联，不删除原行）
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,                         -- 归档时的相对路径
    abs_path TEXT NOT NULL,                         -- 归档时的绝对路径
    content_hash TEXT DEFAULT '',                   -- 归档时的内容 hash
    symbol_count INTEGER DEFAULT 0,                 -- 归档时的符号数（统计用）
    call_count INTEGER DEFAULT 0,                   -- 归档时的调用关系数
    archive_reason TEXT DEFAULT '',                 -- 归档原因（如 "matched: .gitignore:build/"）
    archived_at REAL NOT NULL,                      -- 归档时间戳
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

CREATE INDEX IF NOT EXISTS idx_archived_files_instance ON archived_files(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_archived_files_workspace ON archived_files(workspace_id);
CREATE INDEX IF NOT EXISTS idx_archived_files_path ON archived_files(rel_path);
CREATE INDEX IF NOT EXISTS idx_archived_files_hash ON archived_files(content_hash);

-- ============================================
-- v16: 外部符号表（标准库 + 第三方包）
-- ============================================
-- 存储项目外部的符号信息，包括 Python 标准库和第三方包的函数、类、常量等。
-- 用于跨文件调用解析时，当项目内部找不到被调符号时，查找外部符号表。
-- 支持版本化：同一符号在不同版本的包中可能有不同的签名和文档。
CREATE TABLE IF NOT EXISTS external_symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package_name TEXT NOT NULL,                      -- 包名（如 'stdlib', 'requests', 'fastapi'）
    package_version TEXT DEFAULT '',                  -- 版本号（如 '3.11.0', '2.31.0'）
    module_path TEXT NOT NULL,                       -- 模块路径（如 'os.path', 'requests.get'）
    qualified_name TEXT NOT NULL UNIQUE,             -- 限定名（如 'os.path.join'）
    symbol_name TEXT NOT NULL,                       -- 符号名（如 'join'）
    symbol_kind TEXT DEFAULT 'fn',                   -- 符号类型（fn/class/property/constant/module）
    signature TEXT DEFAULT '',                       -- 签名（如 'def join(path1, path2)'）
    docstring TEXT DEFAULT '',                       -- 文档字符串
    source_file TEXT DEFAULT '',                     -- 源文件路径（标准库模块的 __file__）
    imported_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),  -- 导入时间
    FOREIGN KEY (package_name, package_version) REFERENCES package_versions(package_name, package_version)
);

CREATE INDEX IF NOT EXISTS idx_external_symbols_name ON external_symbols(symbol_name);
CREATE INDEX IF NOT EXISTS idx_external_symbols_qualified ON external_symbols(qualified_name);
CREATE INDEX IF NOT EXISTS idx_external_symbols_package ON external_symbols(package_name);
CREATE INDEX IF NOT EXISTS idx_external_symbols_module ON external_symbols(module_path);

-- 包版本表：记录已导入的包及其版本
CREATE TABLE IF NOT EXISTS package_versions (
    package_name TEXT NOT NULL,                      -- 包名
    package_version TEXT NOT NULL,                    -- 版本号
    installed_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),  -- 安装时间
    last_seen_at REAL NOT NULL DEFAULT (strftime('%s', 'now')),  -- 最近一次被项目依赖清单看到
    last_used_at REAL DEFAULT 0,                       -- 最近一次被查询/调用解析命中
    import_source TEXT DEFAULT 'external',             -- 导入来源：manifest/manual/stdlib
    PRIMARY KEY (package_name, package_version)
);

-- GC 策略表：每个 workspace 独立保存 retention 默认策略。
CREATE TABLE IF NOT EXISTS gc_policies (
    workspace_id INTEGER PRIMARY KEY,
    older_than_days INTEGER NOT NULL DEFAULT 365,
    keep_versions INTEGER NOT NULL DEFAULT 100,
    include_external INTEGER NOT NULL DEFAULT 0,
    external_stale_days INTEGER NOT NULL DEFAULT 365,
    backup_enabled INTEGER NOT NULL DEFAULT 1,
    vacuum_enabled INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);

-- ============================================
-- v20: GC 运行审计表（记录每次 GC 操作的策略/候选/删除/备份/状态）
-- ============================================
-- 类 JVM GC 统计：每次 retention/archive/purge 都记一行，便于事后追溯"为什么少了数据"。
-- policy_json 存当时的策略参数（older_than_days/keep_versions/include_external 等）。
-- candidate_counts/deleted_counts 存 JSON 明细（按 file_versions/external_symbols 等分类）。
-- status: running / completed / failed；失败时 error 记异常信息。
CREATE TABLE IF NOT EXISTS gc_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,                          -- 工作区 ID（NULL 表示全局）
    operation TEXT NOT NULL,                      -- 操作类型：retention / archive / purge
    dry_run INTEGER NOT NULL DEFAULT 0,            -- 1=预演（未实际删除）；0=apply
    policy_json TEXT DEFAULT '',                  -- 策略参数 JSON（older_than_days/keep_versions 等）
    candidate_counts TEXT DEFAULT '{}',           -- 候选数量明细 JSON（{file_versions: N, external_packages: M}）
    deleted_counts TEXT DEFAULT '{}',             -- 实删数量明细 JSON（{file_versions: N, external_symbols: M}）
    backup_path TEXT DEFAULT '',                   -- 备份文件路径（gzip 压缩的 .db.gz）
    backup_size INTEGER DEFAULT 0,                -- 备份文件字节数
    started_at REAL NOT NULL,                     -- 开始时间戳
    completed_at REAL,                            -- 完成时间戳（失败时也填）
    status TEXT NOT NULL DEFAULT 'running',       -- running / completed / failed
    error TEXT DEFAULT '',                        -- 失败时的异常信息
    operator TEXT DEFAULT 'cli',                  -- 触发者：cli / mcp / agent
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_gc_runs_workspace ON gc_runs(workspace_id);
CREATE INDEX IF NOT EXISTS idx_gc_runs_operation ON gc_runs(operation);
CREATE INDEX IF NOT EXISTS idx_gc_runs_status ON gc_runs(status);
CREATE INDEX IF NOT EXISTS idx_gc_runs_started ON gc_runs(started_at);

-- ============================================
-- v17: 任务-符号变更归因表
-- ============================================
-- 保持 file_symbol_versions / symbol_contents 作为事实层；
-- 本表只记录一次任务/步骤/编辑行为为什么导致某个符号版本变化。
CREATE TABLE IF NOT EXISTS task_symbol_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,
    task_id TEXT NOT NULL,
    step_id TEXT DEFAULT '',
    edit_audit_id INTEGER DEFAULT 0,
    change_audit_id TEXT DEFAULT '',
    file_path TEXT NOT NULL,
    qualified_name TEXT DEFAULT '',
    symbol_name TEXT DEFAULT '',
    symbol_hash_before TEXT DEFAULT '',
    symbol_hash_after TEXT DEFAULT '',
    change_type TEXT NOT NULL DEFAULT 'modified',
    source TEXT NOT NULL DEFAULT 'manual',
    metadata TEXT DEFAULT '',
    created_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_task ON task_symbol_changes(task_id);
CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_step ON task_symbol_changes(step_id);
CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_edit ON task_symbol_changes(edit_audit_id);
CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_file ON task_symbol_changes(file_path);
CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_before ON task_symbol_changes(symbol_hash_before);
CREATE INDEX IF NOT EXISTS idx_task_symbol_changes_after ON task_symbol_changes(symbol_hash_after);

-- ============================================
-- v21: 任务质量门禁发现表（Task Quality Gate Findings）
-- ============================================
-- 区别于通用 guardrail_findings：本表专门承载任务完成门禁发现，
-- 把 Semgrep、复杂度、调用链一致性、scope violation、i18n 硬编码等
-- 质量问题挂到 task/step 上，使 open error/block finding 阻止任务进入 done。
-- severity: info / warn / error / block
-- status:   open / resolved / wontfix
-- source:   semgrep / file_health / call_chain / scope / i18n / manual
CREATE TABLE IF NOT EXISTS task_quality_findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,
    task_id TEXT NOT NULL,
    step_id TEXT DEFAULT '',
    finding_type TEXT NOT NULL,
    severity TEXT NOT NULL DEFAULT 'warn',
    status TEXT NOT NULL DEFAULT 'open',
    message TEXT NOT NULL,
    evidence TEXT DEFAULT '',
    source TEXT DEFAULT '',
    created_at REAL NOT NULL,
    resolved_at REAL,
    resolved_by TEXT DEFAULT '',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_task_quality_task ON task_quality_findings(task_id);
CREATE INDEX IF NOT EXISTS idx_task_quality_step ON task_quality_findings(step_id);
CREATE INDEX IF NOT EXISTS idx_task_quality_status ON task_quality_findings(status);
CREATE INDEX IF NOT EXISTS idx_task_quality_severity ON task_quality_findings(severity);

-- v22: 审计签名链表（audit_chain）
-- 为关键审计表（task_quality_findings / change_audit / file_edit_audit 等）
-- 生成可验证的 hash/HMAC 链，防止误改或有意篡改。
-- 每条记录包含 payload_hash + prev_signature + record_signature，形成链式结构。
-- signing_key_id：标识使用的密钥（'local' 表示本地 SHA-256 链，无 HMAC）。
-- 第一阶段使用 SHA-256 链；第二阶段可通过环境变量
-- CALLWARDEN_AUDIT_HMAC_KEY 或 ~/.callwarden/audit.key 切换到 HMAC。
CREATE TABLE IF NOT EXISTS audit_chain (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    table_name TEXT NOT NULL,
    record_id TEXT NOT NULL,
    operation TEXT NOT NULL DEFAULT 'insert',
    payload_hash TEXT NOT NULL,
    prev_signature TEXT DEFAULT '',
    record_signature TEXT NOT NULL,
    signing_key_id TEXT DEFAULT 'local',
    signed_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_chain_table_record ON audit_chain(table_name, record_id);
CREATE INDEX IF NOT EXISTS idx_audit_chain_signature ON audit_chain(record_signature);

-- ============================================
-- 审计签名密钥轮换（v29）
-- ============================================
-- 记录每次签名密钥轮换的时间点与密钥内容，支持按时间点选择对应 key 验证。
-- 轮换后新记录用新 key 签名，旧记录保持原签名；验证时按 signing_key_id 查找对应密钥。
CREATE TABLE IF NOT EXISTS audit_key_rotations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id TEXT NOT NULL UNIQUE,
    key_secret TEXT NOT NULL,
    rotated_at REAL NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audit_key_rotations_active ON audit_key_rotations(is_active);

-- ============================================
-- Agent Rule Memory（v23）
-- ============================================
-- 候选规则表：自动提取、人工创建、任务复盘都写入这里。
-- 默认 status=pending，必须 accept 后才会注入到 Agent 上下文。
CREATE TABLE IF NOT EXISTS agent_rule_candidates (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    scope_json TEXT DEFAULT '{}',
    severity TEXT DEFAULT 'info',
    source TEXT DEFAULT 'manual',
    evidence_json TEXT DEFAULT '{}',
    confidence REAL DEFAULT 0.0,
    status TEXT DEFAULT 'pending',
    created_at REAL NOT NULL,
    reviewed_at REAL,
    reviewer TEXT DEFAULT '',
    linked_rule_id TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_rule_candidates_status ON agent_rule_candidates(status);
CREATE INDEX IF NOT EXISTS idx_agent_rule_candidates_source ON agent_rule_candidates(source);
CREATE INDEX IF NOT EXISTS idx_agent_rule_candidates_severity ON agent_rule_candidates(severity);

-- 已接受规则表：只有 active 规则参与上下文注入和 AGENTS.md 同步。
CREATE TABLE IF NOT EXISTS agent_rules (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    rule_text TEXT NOT NULL,
    scope_json TEXT DEFAULT '{}',
    severity TEXT DEFAULT 'info',
    status TEXT DEFAULT 'active',
    source_candidate_id TEXT DEFAULT '',
    evidence_json TEXT DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    synced_to_agents_md INTEGER DEFAULT 0,
    sync_hash TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_agent_rules_status ON agent_rules(status);
CREATE INDEX IF NOT EXISTS idx_agent_rules_severity ON agent_rules(severity);
CREATE INDEX IF NOT EXISTS idx_agent_rules_synced ON agent_rules(synced_to_agents_md);

-- 同步日志表：记录每次同步 AGENTS.md 的摘要，便于审计追溯。
CREATE TABLE IF NOT EXISTS agent_rule_sync_log (
    id TEXT PRIMARY KEY,
    target_path TEXT NOT NULL,
    rule_ids_json TEXT DEFAULT '[]',
    before_hash TEXT DEFAULT '',
    after_hash TEXT DEFAULT '',
    dry_run INTEGER DEFAULT 1,
    created_at REAL NOT NULL,
    actor TEXT DEFAULT 'agent'
);
CREATE INDEX IF NOT EXISTS idx_agent_rule_sync_log_target ON agent_rule_sync_log(target_path);
CREATE INDEX IF NOT EXISTS idx_agent_rule_sync_log_created ON agent_rule_sync_log(created_at);

-- 自举扫描运行表：记录每次 capture/review 的基线（commit/status_hash/mtime/manifest）
-- 用途：判断两次扫描之间真实变更了哪些文件，关联 task/step
-- 设计：Git 项目优先用 git_head + git_status_hash；非 Git 项目回退到 root_mtime + manifest_hash
CREATE TABLE IF NOT EXISTS workspace_scan_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    purpose TEXT NOT NULL DEFAULT 'bootstrap',
    task_id TEXT DEFAULT '',
    step_id TEXT DEFAULT '',
    baseline_type TEXT NOT NULL DEFAULT 'git',
    git_head TEXT DEFAULT '',
    git_merge_base TEXT DEFAULT '',
    git_status_hash TEXT DEFAULT '',
    root_mtime REAL DEFAULT 0,
    file_count INTEGER DEFAULT 0,
    manifest_hash TEXT DEFAULT '',
    changed_files_json TEXT DEFAULT '[]',
    metadata_json TEXT DEFAULT '{}',
    started_at REAL NOT NULL,
    completed_at REAL,
    status TEXT DEFAULT 'running',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id)
);
CREATE INDEX IF NOT EXISTS idx_workspace_scan_runs_workspace
ON workspace_scan_runs(workspace_id, purpose, started_at);
CREATE INDEX IF NOT EXISTS idx_workspace_scan_runs_task
ON workspace_scan_runs(task_id, step_id);
CREATE INDEX IF NOT EXISTS idx_workspace_scan_runs_git_head
ON workspace_scan_runs(git_head);

-- 重复代码对表：记录 Type-1/2/3 重复代码检测结果
-- 用途：识别代码克隆，辅助重构决策
-- 设计：存储配对符号 ID + 克隆类型 + 相似度 + token 序列哈希
CREATE TABLE IF NOT EXISTS clone_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    symbol_a_id INTEGER NOT NULL,
    symbol_b_id INTEGER NOT NULL,
    clone_type INTEGER NOT NULL,
    similarity REAL NOT NULL,
    token_hash TEXT NOT NULL DEFAULT '',
    lines_a INTEGER DEFAULT 0,
    lines_b INTEGER DEFAULT 0,
    detected_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (symbol_a_id) REFERENCES symbols(id),
    FOREIGN KEY (symbol_b_id) REFERENCES symbols(id)
);
CREATE INDEX IF NOT EXISTS idx_clone_pairs_workspace
ON clone_pairs(workspace_id, detected_at);
CREATE INDEX IF NOT EXISTS idx_clone_pairs_symbol_a
ON clone_pairs(symbol_a_id);
CREATE INDEX IF NOT EXISTS idx_clone_pairs_symbol_b
ON clone_pairs(symbol_b_id);
CREATE INDEX IF NOT EXISTS idx_clone_pairs_type
ON clone_pairs(clone_type, similarity);
CREATE UNIQUE INDEX IF NOT EXISTS idx_clone_pairs_unique
ON clone_pairs(workspace_id, symbol_a_id, symbol_b_id, clone_type);
"""

# Schema 版本号（用于迁移判断）
# v4: Git 集成表
# v5: 向量嵌入表（sqlite-vec）
# v6: 符号摘要表（AI 生成的函数/模块摘要，版本化）
# v7: 任务管理表（tasks / task_steps / change_audit）
# v8: 文件所有权表（file_ownership：CODEOWNERS + git blame）
# v9: 覆盖率数据表（coverage_data，LCOV/Cobertura 行级覆盖率）
# v10: 守护者架构表（guardrail_rules/findings + change_impacts + evolution_metrics + defect_patterns/fixes）
# v11: Token 节省账本表（token_savings_ledger，记录每次 Agent 操作的 token 节省）
# v12: 安全文件编辑审计表（file_edit_audit，propose_edit 安全编辑流水线）
# v13: 跨仓库分析表（cross_repo_deps，跨仓库依赖关系）
# v14: 归档表（archived_files，被 .gitignore/.callwardenignore 命中的文件迁出主表，类 Java GC 老年代）
# v15: 父子任务支持（tasks 表增加 parent_id/depth/sort_order，支持任务树嵌套）
# v16: 外部符号表（external_symbols + package_versions，标准库 + 第三方包符号）
# v17: 任务-符号变更归因表（task_symbol_changes）
# v18: 外部包冷数据追踪（package_versions.last_seen_at / last_used_at / import_source）
# v19: GC retention 策略表（gc_policies）
# v20: GC 运行审计表（gc_runs，记录每次 retention/archive/purge 的策略/候选/删除/备份/状态）
# v21: 任务质量门禁发现表（task_quality_findings，承载任务完成门禁发现，区分于通用 guardrail_findings）
# v22: 审计签名链表（audit_chain，为关键审计表生成可验证的 hash/HMAC 链）
# v23: Agent Rule Memory 表（agent_rule_candidates / agent_rules / agent_rule_sync_log，沉淀项目规则并注入到任务和函数上下文）
# v24: tasks 表新增 applied_at 字段（记录 review → applied 审核通过时间，与 closed_at 配合完成任务状态机）
# v25: 自举扫描运行表（workspace_scan_runs，记录 capture/review 基线，支持 task capture-diff 闭环）
# v26: symbols 表 UNIQUE 索引（file_instance_id, name, start_line）+ UPSERT，防止重复符号、支持并发安全写入
# v27: 重复代码对表（clone_pairs，记录 Type-1/2/3 克隆检测结果，支持重构决策）
# v28: file_versions 表新增 ast_cache 字段（BLOB，存储 tree-sitter AST 序列化结果，支持增量解析）
# v29: 审计签名密钥轮换表（audit_key_rotations，记录密钥轮换时间点，支持按 key_id 验证旧记录）
SCHEMA_VERSION = 29


# ============================================
# 任务状态机常量
# ============================================

# 任务状态
TASK_STATUS_OPEN = "open"
TASK_STATUS_IN_PROGRESS = "in_progress"
TASK_STATUS_REVIEW = "review"
TASK_STATUS_APPLIED = "applied"
TASK_STATUS_CLOSED = "closed"
TASK_STATUS_REVERTED = "reverted"

# 任务步骤状态
STEP_STATUS_PENDING = "pending"
STEP_STATUS_IN_PROGRESS = "in_progress"
STEP_STATUS_DONE = "done"
STEP_STATUS_FAILED = "failed"
STEP_STATUS_SKIPPED = "skipped"
STEP_STATUS_BLOCKED = "blocked"  # Before-Edit Contract 触发：guardrail 阻断时步骤被阻塞

# 触发 Before-Edit Contract 的编辑类动作（小写匹配）
EDIT_ACTIONS = frozenset({
    "edit", "modify", "write", "update", "delete", "remove",
    "refactor", "fix", "annotate", "rename", "move", "replace",
})


# ============================================
# 护栏规则常量
# ============================================

# 护栏规则类别
GUARDRAIL_CATEGORY_DB_SAFETY = "db_safety"
GUARDRAIL_CATEGORY_API_COMPAT = "api_compat"
GUARDRAIL_CATEGORY_INCIDENT = "incident"

# 护栏严重级别
GUARDRAIL_SEVERITY_BLOCK = "block"
GUARDRAIL_SEVERITY_WARN = "warn"
GUARDRAIL_SEVERITY_INFO = "info"

# 护栏动作
GUARDRAIL_ACTION_BLOCK = "block"
GUARDRAIL_ACTION_REQUIRE_REVIEW = "require_review"
GUARDRAIL_ACTION_WARN = "warn"

# 护栏 finding 状态
GUARDRAIL_STATUS_OPEN = "open"
GUARDRAIL_STATUS_RESOLVED = "resolved"
GUARDRAIL_STATUS_WONTFIX = "wontfix"
