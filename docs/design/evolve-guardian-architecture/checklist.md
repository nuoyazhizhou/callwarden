# Checklist

## Pillar 1: 生产安全护栏

- [x] `guardrail_rules` 表已在 schema.py 中定义，包含 rule_id/category/severity/pattern/action/description/is_builtin/created_at 字段
- [x] `guardrail_findings` 表已在 schema.py 中定义，包含 finding_id/rule_id/file_path/symbol_hash/severity/status/message/detected_at/resolved_at 字段
- [x] v9→v10 迁移函数 `_migrate_v9_to_v10` 已在 db_base.py 中注册
- [x] 内置规则集已预置（DB Safety / API Compatibility / Incident Readiness 各 3-5 条）
- [x] `db_guardrail.py` GuardrailMixin 类已创建
- [x] `scan_guardrails(file_filter)` 方法可对指定文件运行规则扫描并返回 findings
- [x] `check_before_edit(file_path, proposed_change)` 方法可执行编辑前阻断式检查，返回 block/warn/pass
- [x] `guardrail_add_rule()` 方法可添加自定义规则
- [x] `guardrail_list_rules()` 方法可列出规则
- [x] DB Safety 检测器可识别 ALTER TABLE / DROP TABLE / 字段缩减 / 迁移缺失
- [x] API Compatibility 检测器可识别函数签名变更（参数增删 / 类型改变 / 可见性变化）
- [x] Incident Readiness 检测器可识别错误处理缺失 / 日志缺失 / 可回滚性
- [x] GuardrailMixin 已在 db.py 中集成

## Pillar 2: 变更影响智能

- [x] `change_impacts` 表已在 schema.py 中定义
- [x] `diff_to_symbol(diff_text)` 方法可将 git diff 映射到受影响符号列表
- [x] `db_impact.py` ImpactMixin 类已创建
- [x] `blast_radius(symbol_hash, depth)` 方法可返回多层影响树（代码层调用方）
- [x] `cross_layer_impact(symbol_hash)` 方法可返回跨层影响（代码 + DB + API + 配置）
- [x] `review_readiness_report(symbol_hash)` 方法可输出影响范围 / 风险等级 / 必测项 / 人工审查点
- [x] ImpactMixin 已在 db.py 中集成

## Pillar 3: 代码演化智能

- [x] `evolution_metrics` 表已在 schema.py 中定义
- [x] `db_evolution.py` EvolutionMixin 类已创建
- [x] `function_change_frequency(qualified_name)` 方法可返回变更次数 / 时间分布 / 变更者 / 趋势
- [x] `defect_correlation(symbol_hash)` 方法可返回变更后引入的缺陷数量 / 类型 / 关联 finding
- [x] `hotspot_evolution(module_filter)` 方法可返回热点排名（变更频率 + 缺陷密度 + 圈复杂度）
- [x] `churn_analysis(module_filter)` 方法可返回 churn rate / 高频变更文件 / 流失趋势
- [x] `refresh_evolution_metrics()` 方法可批量刷新指标缓存
- [x] EvolutionMixin 已在 db.py 中集成

## Pillar 4: 缺陷知识库

- [x] `defect_patterns` 表已在 schema.py 中定义
- [x] `defect_fixes` 表已在 schema.py 中定义
- [x] `db_defect_kb.py` DefectKbMixin 类已创建
- [x] `build_defect_knowledge()` 方法可从历史 Semgrep findings + git 修复中挖掘缺陷模式
- [x] `defect_pattern_search(category)` 方法可按类别搜索缺陷模式
- [x] `suggest_fix(symbol_hash, finding_id)` 方法可返回修复 diff 模板 + 有效性评分
- [x] `learn_defect_from_fix(fix_commit_hash)` 方法可从修复 commit 提取 before/after 模式
- [x] `defect_stats()` 方法可返回知识库统计
- [x] DefectKbMixin 已在 db.py 中集成

## Pillar 5: MCP 工具与 CLI

- [x] MCP 工具总数达到 89 个（73 + 16 新增）
- [x] 安全护栏类 4 个 MCP 工具可正常加载
- [x] 变更影响类 4 个 MCP 工具可正常加载
- [x] 演化智能类 4 个 MCP 工具可正常加载
- [x] 缺陷知识库类 4 个 MCP 工具可正常加载
- [x] `cg guardrail scan` CLI 命令可执行
- [x] `cg guardrail rules` CLI 命令可执行
- [x] `cg impact` CLI 命令可执行
- [x] `cg evolution` CLI 命令可执行
- [x] `cg hotspot` CLI 命令可执行
- [x] `cg defect search` CLI 命令可执行
- [x] `cg defect suggest` CLI 命令可执行
- [x] `cg defect learn` CLI 命令可执行
- [x] `cg defect stats` CLI 命令可执行

## Pillar 6: 集成验证

- [x] guardrail_scan 对 TokenSlim 自身代码返回合理 findings
- [x] blast_radius 返回多层影响树
- [x] evolution_frequency 返回变更历史
- [x] defect_suggest_fix 返回修复建议
- [x] task_next_step 自动触发 guardrail_check_edit
- [x] block 级别告警时步骤状态变为 blocked
- [x] Agent 处理告警后可继续步骤
