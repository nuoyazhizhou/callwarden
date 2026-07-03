# Tasks

## Pillar 1: 生产安全护栏（蓝海超越）

- [x] Task 1: 安全护栏数据库设计
  - [x] SubTask 1.1: 在 schema.py 中设计 `guardrail_rules` 表（rule_id, category, severity, pattern, action, description, is_builtin, created_at）
  - [x] SubTask 1.2: 在 schema.py 中设计 `guardrail_findings` 表（finding_id, rule_id, file_path, symbol_hash, severity, status, message, detected_at, resolved_at）
  - [x] SubTask 1.3: 在 db_base.py 中注册 v9→v10 迁移函数 `_migrate_v9_to_v10`
  - [x] SubTask 1.4: 预置内置规则集（DB Safety / API Compatibility / Incident Readiness 各 3-5 条）

- [x] Task 2: 实现 GuardrailMixin 核心逻辑
  - [x] SubTask 2.1: 创建 `db_guardrail.py` GuardrailMixin 类
  - [x] SubTask 2.2: 实现 `scan_guardrails(file_filter)` — 对指定文件运行规则扫描，返回 findings
  - [x] SubTask 2.3: 实现 `check_before_edit(file_path, proposed_change)` — 编辑前阻断式检查，返回 block/warn/pass
  - [x] SubTask 2.4: 实现 `guardrail_add_rule(category, pattern, severity, action, description)` — 添加自定义规则
  - [x] SubTask 2.5: 实现 `guardrail_list_rules(category_filter)` — 列出规则
  - [x] SubTask 2.6: 实现 `resolve_finding(finding_id, resolution)` — 标记 finding 已处理
  - [x] SubTask 2.7: 在 db.py 中集成 GuardrailMixin

- [x] Task 3: 实现三类安全规则检测器
  - [x] SubTask 3.1: 实现 DB Safety 检测器（SQL 解析：ALTER TABLE / DROP TABLE / 字段缩减 / 迁移缺失）
  - [x] SubTask 3.2: 实现 API Compatibility 检测器（函数签名 diff：参数增删 / 类型改变 / 可见性变化）
  - [x] SubTask 3.3: 实现 Incident Readiness 检测器（错误处理检测 / 日志检测 / 可回滚性评估）

## Pillar 2: 变更影响智能（补短板）

- [x] Task 4: 变更影响数据库与映射
  - [x] SubTask 4.1: 在 schema.py 中设计 `change_impacts` 表（change_id, source_symbol, impact_type, target_symbol, target_layer, confidence, detected_at）
  - [x] SubTask 4.2: 在迁移函数中添加 `change_impacts` 表创建
  - [x] SubTask 4.3: 实现 `diff_to_symbol(diff_text)` — 解析 git diff，映射到受影响符号（added/modified/deleted）

- [x] Task 5: 实现 ImpactMixin 核心逻辑
  - [x] SubTask 5.1: 创建 `db_impact.py` ImpactMixin 类
  - [x] SubTask 5.2: 实现 `blast_radius(symbol_hash, depth=3)` — BFS 遍历调用图 + 跨层关联，返回影响树
  - [x] SubTask 5.3: 实现 `cross_layer_impact(symbol_hash)` — 代码层（调用方）+ DB 层（SQL 表）+ API 层（端点）+ 配置层（配置项）
  - [x] SubTask 5.4: 实现 `review_readiness_report(symbol_hash)` — 影响范围 + 风险等级 + 必测项 + 人工审查点
  - [x] SubTask 5.5: 在 db.py 中集成 ImpactMixin

## Pillar 3: 代码演化智能（深化独有优势）

- [x] Task 6: 演化指标计算
  - [x] SubTask 6.1: 在 schema.py 中设计 `evolution_metrics` 表（symbol_hash, change_count, defect_count, hotspot_score, first_seen, last_changed_at, updated_at）
  - [x] SubTask 6.2: 在迁移函数中添加 `evolution_metrics` 表创建

- [x] Task 7: 实现 EvolutionMixin 核心逻辑
  - [x] SubTask 7.1: 创建 `db_evolution.py` EvolutionMixin 类
  - [x] SubTask 7.2: 实现 `function_change_frequency(qualified_name, time_window)` — 查询 symbol_versions + file_versions，统计变更频率
  - [x] SubTask 7.3: 实现 `defect_correlation(symbol_hash, window_commits=5)` — 关联 Semgrep findings 与函数变更，计算缺陷引入率
  - [x] SubTask 7.4: 实现 `hotspot_evolution(module_filter)` — 综合变更频率 + 缺陷密度 + 圈复杂度，输出热点排名
  - [x] SubTask 7.5: 实现 `churn_analysis(module_filter, time_window)` — 代码流失分析（churn rate + 高频变更文件）
  - [x] SubTask 7.6: 实现 `refresh_evolution_metrics()` — 批量刷新演化指标缓存
  - [x] SubTask 7.7: 在 db.py 中集成 EvolutionMixin

## Pillar 4: 缺陷知识库（深化独有优势）

- [x] Task 8: 缺陷知识库数据库设计
  - [x] SubTask 8.1: 在 schema.py 中设计 `defect_patterns` 表（pattern_id, category, description, detection_rule, fix_template, severity, learned_from, case_count, created_at）
  - [x] SubTask 8.2: 在 schema.py 中设计 `defect_fixes` 表（fix_id, pattern_id, symbol_hash, before_hash, after_hash, fix_diff, effectiveness, created_at）
  - [x] SubTask 8.3: 在迁移函数中添加两张表创建

- [x] Task 9: 实现 DefectKbMixin 核心逻辑
  - [x] SubTask 9.1: 创建 `db_defect_kb.py` DefectKbMixin 类
  - [x] SubTask 9.2: 实现 `build_defect_knowledge()` — 从历史 semgrep_findings + git 修复提交中挖掘缺陷模式
  - [x] SubTask 9.3: 实现 `defect_pattern_search(category, severity_filter)` — 按类别/严重度搜索缺陷模式
  - [x] SubTask 9.4: 实现 `suggest_fix(symbol_hash, finding_id)` — 匹配缺陷模式，返回修复 diff 模板 + 有效性评分
  - [x] SubTask 9.5: 实现 `learn_defect_from_fix(fix_commit_hash)` — 从修复 commit 提取 before/after 模式，存入知识库
  - [x] SubTask 9.6: 实现 `defect_stats()` — 知识库统计（模式总数 / 类别分布 / 修复有效性 / Top 缺陷）
  - [x] SubTask 9.7: 在 db.py 中集成 DefectKbMixin

## Pillar 5: MCP 工具与 CLI 集成

- [x] Task 10: MCP 工具注册（73→89）
  - [x] SubTask 10.1: 在 mcp_server.py 中添加安全护栏类 4 个工具（guardrail_scan / guardrail_check_edit / guardrail_list_rules / guardrail_add_rule）
  - [x] SubTask 10.2: 在 mcp_server.py 中添加变更影响类 4 个工具（blast_radius / diff_to_symbol / review_readiness / cross_layer_impact）
  - [x] SubTask 10.3: 在 mcp_server.py 中添加演化智能类 4 个工具（evolution_frequency / defect_correlation / hotspot_evolution / churn_analysis）
  - [x] SubTask 10.4: 在 mcp_server.py 中添加缺陷知识库类 4 个工具（defect_search / defect_suggest_fix / defect_learn / defect_stats）

- [x] Task 11: CLI 命令注册
  - [x] SubTask 11.1: 添加 `cw guardrail scan [--file <path>] [--category <cat>]` 命令
  - [x] SubTask 11.2: 添加 `cw guardrail rules [--category <cat>]` 命令
  - [x] SubTask 11.3: 添加 `cw impact <symbol> [--depth N]` 命令
  - [x] SubTask 11.4: 添加 `cw evolution <symbol> [--window 30d]` 命令
  - [x] SubTask 11.5: 添加 `cw hotspot [--module <path>]` 命令
  - [x] SubTask 11.6: 添加 `cw defect search [--category <cat>]` 命令
  - [x] SubTask 11.7: 添加 `cw defect suggest <symbol> <finding_id>` 命令
  - [x] SubTask 11.8: 添加 `cw defect learn <commit_hash>` 命令
  - [x] SubTask 11.9: 添加 `cw defect stats` 命令

## Pillar 6: 集成验证

- [x] Task 12: MCP 工具验证（目标 89 个工具）
  - [x] SubTask 12.1: 验证所有新增 MCP 工具可正常加载（73+16=89）
  - [x] SubTask 12.2: 验证 guardrail_scan 对 TokenSlim 自身代码返回合理 findings
  - [x] SubTask 12.3: 验证 blast_radius 返回多层影响树
  - [x] SubTask 12.4: 验证 evolution_frequency 返回变更历史
  - [x] SubTask 12.5: 验证 defect_suggest_fix 返回修复建议

- [x] Task 13: CLI 命令验证
  - [x] SubTask 13.1: 验证 `cw guardrail scan` 命令
  - [x] SubTask 13.2: 验证 `cw impact` 命令
  - [x] SubTask 13.3: 验证 `cw evolution` 和 `cw hotspot` 命令
  - [x] SubTask 13.4: 验证 `cw defect` 系列命令

- [x] Task 14: Before-Edit Contract 集成验证
  - [x] SubTask 14.1: 验证 task_next_step 自动触发 guardrail_check_edit
  - [x] SubTask 14.2: 验证 block 级别告警时步骤状态变为 blocked
  - [x] SubTask 14.3: 验证 Agent 处理告警后可继续步骤

# Task Dependencies
- [Task 2] depends on [Task 1]（需要规则表才能实现扫描逻辑）
- [Task 3] depends on [Task 2]（检测器是 scan_guardrails 的具体规则实现）
- [Task 5] depends on [Task 4]（需要 change_impacts 表和 diff_to_symbol 映射）
- [Task 7] depends on [Task 6]（需要 evolution_metrics 缓存表）
- [Task 9] depends on [Task 8]（需要缺陷模式表和修复案例表）
- [Task 10] depends on [Task 2, 5, 7, 9]（MCP 工具依赖各 Mixin 实现）
- [Task 11] depends on [Task 2, 5, 7, 9]（CLI 命令依赖各 Mixin 实现）
- [Task 12] depends on [Task 10]
- [Task 13] depends on [Task 11]
- [Task 14] depends on [Task 2]（Before-Edit Contract 依赖 GuardrailMixin）
- 并行机会：[Task 1-3]、[Task 4-5]、[Task 6-7]、[Task 8-9] 四组可并行
