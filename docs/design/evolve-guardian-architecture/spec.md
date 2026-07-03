# 代码守护者架构 (Evolve Guardian Architecture) Spec

## Why

基于 450 个竞品的雷达分析与差距报告，`semantic-steward-architecture` 完成后我们将补齐 `coverage_intelligence`(1.97) 和 `ownership_context`(2.02) 两个最弱维度。但仍有四个未覆盖的战略机会：

1. **生产安全护栏（蓝海）**：`production_risk` 竞品均分仅 4.00（450 个竞品中第三弱维度），几乎无人做好 DB/API/Incident 安全。差距报告明确建议"把 database-safety、api-compatibility、incident-readiness 做成可阻断 coding_guardrails 规则扫描"。
2. **变更影响智能（补短板）**：`change_safety` 均分 5.18，我们缺少 `impact_analysis` 和 `blast_radius`（TokenSlim.json 中两者均为 false）。差距报告 P1 项"变更影响分析（跨层）"——只有函数级调用链影响，没有数据库/API/配置层的跨层影响。
3. **代码演化智能（深化独有优势）**：我们有完整的版本历史（content_hash 去重 + 删除标记 + 注释恢复），但未挖掘。差距报告蓝海建议："版本历史 → 代码演化分析（函数变更频率 + 缺陷关联）"。
4. **缺陷知识库（深化独有优势）**：我们有 Semgrep 集成（tokensave 没有），但只是扫描器。差距报告蓝海建议："Semgrep 集成 → 缺陷知识库（缺陷模式库 + 修复建议）"。

**核心策略（Blue Ocean + VRIO）**：
- **ELIMINATE**：不跟 tokensave 卷纯 RAG 问答（红海，已饱和）
- **REDUCE**：减少在通用代码搜索上的投入（竞品已饱和，vector_search 367 个竞品都有）
- **RAISE**：把 `change_safety` 从缺失提升到 7+（超过竞品均分 5.18）
- **CREATE**：创造生产安全护栏（production_risk 4.00 蓝海）+ 代码演化智能 + 缺陷知识库

**VRIO 分析我们的独有资源**：
| 资源 | 价值 | 稀缺 | 难模仿 | 组织化 | 结论 |
|------|------|------|--------|--------|------|
| 版本历史（hash 去重+删除标记） | ✅ | ✅ | ✅ | ✅ | 持续竞争优势 |
| Semgrep 深度集成 | ✅ | ✅ | ✅ | ✅ | 持续竞争优势 |
| 注释恢复 | ✅ | ✅ | ✅ | ✅ | 持续竞争优势 |

这三个独有资源是竞品难以复制的护城河，本 spec 将其从"功能"升级为"智能"。

## What Changes

### Pillar 1: 生产安全护栏（蓝海超越，P0）
- 新增 `guardrail_rules` 表：可阻断的规则定义（rule_id, category, severity, pattern, action）
- 新增 `guardrail_findings` 表：规则扫描结果（finding_id, rule_id, file_path, symbol_hash, severity, status）
- 实现三类安全规则：
  - **DB Safety**：检测 SQL schema 变更（ALTER TABLE/DROP TABLE）、迁移脚本缺失、字段长度缩减
  - **API Compatibility**：检测 public API 签名变更（参数增删/类型改变/返回值变化）、breaking change 标记
  - **Incident Readiness**：检测日志缺失、错误处理缺失、可回滚性评估
- 实现 `scan_guardrails(file_filter)` 方法：对指定文件/符号运行规则扫描
- 实现 `check_before_edit(file_path, proposed_change)` 方法：编辑前阻断式检查
- 新增 MCP 工具：`guardrail_scan`、`guardrail_check_edit`、`guardrail_list_rules`、`guardrail_add_rule`

### Pillar 2: 变更影响智能（补短板，P0）
- 新增 `change_impacts` 表：变更影响记录（change_id, source_symbol, impact_type, target_symbol, target_layer）
- 实现 `blast_radius(symbol_hash, depth)` 方法：计算变更影响半径（代码层 + DB 层 + API 层 + 配置层）
- 实现 `diff_to_symbol(diff)` 方法：将 git diff 映射到受影响的符号
- 实现 `review_readiness_report(symbol_hash)` 方法：输出影响范围、风险等级、必测项、人工审查点
- 实现 `cross_layer_impact(symbol_hash)` 方法：跨层影响分析（代码变更 → 影响 DB schema？影响 API 契约？影响配置？）
- 新增 MCP 工具：`blast_radius`、`diff_to_symbol`、`review_readiness`、`cross_layer_impact`

### Pillar 3: 代码演化智能（深化独有优势，P1）
- 利用现有 `file_versions` / `symbol_versions` / `call_versions` 表，无需新表
- 新增 `evolution_metrics` 表：演化指标缓存（symbol_hash, change_count, defect_correlation, hotspot_score, last_changed_at）
- 实现 `function_change_frequency(qualified_name)` 方法：函数变更频率分析（按时间窗口）
- 实现 `defect_correlation(symbol_hash)` 方法：函数变更与缺陷的关联分析（变更后 N 次提交内是否引入缺陷）
- 实现 `hotspot_evolution(module_filter)` 方法：热点函数演化（基于变更频率 + 缺陷密度 + 圈复杂度）
- 实现 `churn_analysis(module_filter)` 方法：代码流失分析（churn rate，识别高频变更区域）
- 新增 MCP 工具：`evolution_frequency`、`defect_correlation`、`hotspot_evolution`、`churn_analysis`

### Pillar 4: 缺陷知识库（深化独有优势，P1）
- 新增 `defect_patterns` 表：缺陷模式库（pattern_id, category, description, detection_rule, fix_template, severity）
- 新增 `defect_fixes` 表：缺陷修复案例（fix_id, pattern_id, symbol_hash, before_hash, after_hash, fix_diff, effectiveness）
- 利用现有 `semgrep_findings` / `semgrep_scans` 表关联缺陷数据
- 实现 `build_defect_knowledge()` 方法：从历史 Semgrep 扫描结果 + git 历史中挖掘缺陷模式
- 实现 `suggest_fix(symbol_hash, finding_id)` 方法：基于缺陷知识库推荐修复方案
- 实现 `defect_pattern_search(category)` 方法：按类别搜索缺陷模式
- 实现 `learn_defect_from_fix(fix_commit_hash)` 方法：从历史修复提交中学习缺陷模式
- 新增 MCP 工具：`defect_search`、`defect_suggest_fix`、`defect_learn`、`defect_stats`

## Impact

- **Affected code**: 
  - `db_base.py`（schema 迁移 v9→v10）
  - `server/mcp_server.py`（新增约 16 个 MCP 工具，73→89）
  - `cli/main.py`（新增约 10 个 CLI 命令）
- **新增模块**:
  - `db_guardrail.py`（GuardrailMixin：生产安全护栏）
  - `db_impact.py`（ImpactMixin：变更影响智能）
  - `db_evolution.py`（EvolutionMixin：代码演化智能）
  - `db_defect_kb.py`（DefectKbMixin：缺陷知识库）
- **数据库迁移**: schema v9→v10，新增 5 张表
- **依赖现有能力**: 复用 `db_metrics.py`（圈复杂度）、`db_git.py`（git 集成）、`analyzers/issues.py`（Semgrep）

## ADDED Requirements

### Requirement: 生产安全护栏
系统 SHALL 提供可阻断的生产安全规则扫描能力，覆盖 DB/API/Incident 三类风险。

#### Scenario: DB schema 变更检测
- **WHEN** Agent 修改了一个包含 `ALTER TABLE` 的迁移文件
- **AND** 调用 `guardrail_check_edit(file_path, proposed_change)`
- **THEN** 系统返回 `block` 级别告警，提示"DB schema 变更需确认：ALTER TABLE 可能导致锁表；建议加 CONCURRENTLY 或分步迁移"

#### Scenario: API breaking change 检测
- **WHEN** Agent 修改了一个 public 函数的签名（删除参数 / 改变类型）
- **AND** 该函数被标记为 public API
- **THEN** 系统返回 `block` 级别告警，提示"API breaking change：参数 X 被删除，调用方可能崩溃"

#### Scenario: Incident readiness 检查
- **WHEN** Agent 新增了一个函数但未添加任何错误处理
- **AND** 调用 `guardrail_scan(file_filter="src/api/")`
- **THEN** 系统返回 `warn` 级别告警，提示"函数 X 缺少错误处理，影响事故可恢复性"

#### Scenario: 自定义规则
- **WHEN** 用户调用 `guardrail_add_rule(category="db_safety", pattern="DROP TABLE", severity="block", action="require_review")`
- **THEN** 规则存入数据库，后续扫描生效

### Requirement: 变更影响半径
系统 SHALL 提供变更影响半径计算能力，覆盖代码层、DB 层、API 层、配置层。

#### Scenario: 单函数影响半径
- **WHEN** Agent 调用 `blast_radius(symbol_hash="abc123", depth=3)`
- **THEN** 返回影响树：受影响的调用方（代码层）、关联的 DB 表（DB 层）、暴露的 API 端点（API 层）、关联的配置项（配置层）

#### Scenario: diff 到符号映射
- **WHEN** Agent 调用 `diff_to_symbol(diff="git diff HEAD~1")`
- **THEN** 返回受影响的符号列表，每个符号标注变更类型（added/modified/deleted）

#### Scenario: 审查就绪报告
- **WHEN** Agent 调用 `review_readiness_report(symbol_hash="abc123")`
- **THEN** 返回：影响范围（高/中/低）、风险等级、必测项列表、人工审查点列表

#### Scenario: 跨层影响
- **WHEN** Agent 调用 `cross_layer_impact(symbol_hash="abc123")`
- **AND** 该函数操作了 `users` 表
- **THEN** 返回跨层影响：代码层（3 个调用方）、DB 层（users 表）、API 层（/api/users 端点）、配置层（DB_POOL_SIZE）

### Requirement: 代码演化智能
系统 SHALL 利用版本历史数据提供代码演化分析能力。

#### Scenario: 函数变更频率
- **WHEN** Agent 调用 `evolution_frequency(qualified_name="auth.login")`
- **THEN** 返回该函数的变更次数、变更时间分布、变更者列表、变更频率趋势

#### Scenario: 缺陷关联分析
- **WHEN** Agent 调用 `defect_correlation(symbol_hash="abc123")`
- **THEN** 返回该函数变更后 N 次提交内引入的缺陷数量、缺陷类型分布、关联的 Semgrep finding

#### Scenario: 热点演化
- **WHEN** Agent 调用 `hotspot_evolution(module_filter="src/api/")`
- **THEN** 返回模块内热点函数排名（基于变更频率 + 缺陷密度 + 圈复杂度），标注"持续热点"和"新兴热点"

#### Scenario: 代码流失分析
- **WHEN** Agent 调用 `churn_analysis(module_filter="src/api/")`
- **THEN** 返回模块的 churn rate、高频变更文件列表、流失趋势

### Requirement: 缺陷知识库
系统 SHALL 从历史 Semgrep 扫描和 git 修复中构建缺陷知识库，提供修复建议。

#### Scenario: 缺陷模式搜索
- **WHEN** Agent 调用 `defect_search(category="sql_injection")`
- **THEN** 返回该类别下所有缺陷模式，包含检测规则、修复模板、历史案例数

#### Scenario: 修复建议
- **WHEN** Agent 调用 `defect_suggest_fix(symbol_hash="abc123", finding_id="F-001")`
- **THEN** 返回推荐的修复方案，包含修复 diff 模板、历史类似修复的有效性评分

#### Scenario: 从修复中学习
- **WHEN** 用户调用 `defect_learn(fix_commit_hash="abc123")`
- **THEN** 系统分析该 commit 的 diff，提取缺陷模式（前）和修复模式（后），存入知识库

#### Scenario: 缺陷统计
- **WHEN** Agent 调用 `defect_stats()`
- **THEN** 返回缺陷知识库统计：模式总数、按类别分布、修复有效性分布、最常见缺陷 Top 10

## MODIFIED Requirements

### Requirement: MCP 工具体系
现有 73 个 MCP 工具基础上，新增约 16 个工具：
- 安全护栏类：guardrail_scan、guardrail_check_edit、guardrail_list_rules、guardrail_add_rule（4个）
- 变更影响类：blast_radius、diff_to_symbol、review_readiness、cross_layer_impact（4个）
- 演化智能类：evolution_frequency、defect_correlation、hotspot_evolution、churn_analysis（4个）
- 缺陷知识库类：defect_search、defect_suggest_fix、defect_learn、defect_stats（4个）

### Requirement: 数据库 Schema
从 v9 迁移到 v10，新增表：
- `guardrail_rules`（rule_id, category, severity, pattern, action, description, created_at）
- `guardrail_findings`（finding_id, rule_id, file_path, symbol_hash, severity, status, detected_at, resolved_at）
- `change_impacts`（change_id, source_symbol, impact_type, target_symbol, target_layer, detected_at）
- `evolution_metrics`（symbol_hash, change_count, defect_correlation, hotspot_score, last_changed_at, updated_at）
- `defect_patterns`（pattern_id, category, description, detection_rule, fix_template, severity, learned_from, created_at）
- `defect_fixes`（fix_id, pattern_id, symbol_hash, before_hash, after_hash, fix_diff, effectiveness, created_at）

### Requirement: Before-Edit Contract
系统 SHALL 在 Agent 编辑文件前提供阻断式检查能力（与任务驱动 MCP 协同）。

#### Scenario: 编辑前强制检查
- **WHEN** Agent 通过 `task_next_step` 领取了一个"修改 src/api/auth.py"的步骤
- **THEN** 系统自动运行 `guardrail_check_edit`，若返回 `block` 级别告警，步骤状态变为 `blocked`，Agent 必须先处理告警
