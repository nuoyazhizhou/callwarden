# 审计任务提示词：Agent Rule Memory 任务关闭审核

> **审计人**：其他会话的 LLM Agent
> **审计任务**：T-1783253838055-f033「实现 Agent Rule Memory 与规则上下文注入」
> **审计范围**：10 个子任务（全部 review 状态）+ 父任务级联 close
> **工具约束**：MCP 未激活，只能用 `cw` CLI 命令审计和 close

## 一、任务背景

任务 T-1783253838055-f033 是 Agent Rule Memory 功能的实现，包含 10 个子任务：

| # | 子任务 ID | 标题 | 步骤数 |
|---|----------|------|--------|
| 1 | T-1783253838062-494d | Phase 1: 新增 Agent Rule schema 与迁移 | 2 |
| 2 | T-1783253838062-1917 | Phase 1: 实现 AgentRulesMixin 候选与生效规则 CRUD | 3 |
| 3 | T-1783253838062-6687 | Phase 2: 实现 get_applicable_rules 作用域匹配 | 2 |
| 4 | T-1783253838063-f35d | Phase 3: 将规则注入 task_next_step 与 work_next_job | 3 |
| 5 | T-1783253838063-afd7 | Phase 4: 将规则注入 get_symbol 与 file_symbol_content | 3 |
| 6 | T-1783253838063-6770 | Phase 5: 从 task_quality_findings 自动提取规则候选 | 2 |
| 7 | T-1783253838063-807e | Phase 6: 实现 AGENTS.md 安全同步 | 2 |
| 8 | T-1783253838063-b70c | Phase 7: 暴露 CLI/MCP 并补齐 i18n | 5 |
| 9 | T-1783253838063-59b0 | Phase 8: 更新文档并运行回归验证 | 4 |
| 10 | T-1783309017863-a1b6 | 实现任务级联 close 与父任务 close 校验 | 10 |

**所有子任务都是 `review` 状态**，所有步骤都是 `done` 状态。

## 二、审计目标

验证每个子任务的：
1. **代码实现真实存在** — 不是只标记 done 而没写代码
2. **commit 历史完整** — 每个 step 都有独立 commit
3. **测试覆盖** — 新增功能有对应测试
4. **文档同步** — CLI / MCP / 架构文档已更新
5. **i18n 规范** — 所有用户可见字符串走 i18n key

## 三、审计命令（无 MCP，只能用 cw CLI）

### 3.1 查看任务详情

```bash
# 查看父任务
cw task show T-1783253838055-f033

# 查看子任务（逐个查看）
cw task show T-1783253838062-494d
cw task show T-1783253838062-1917
cw task show T-1783253838062-6687
cw task show T-1783253838063-f35d
cw task show T-1783253838063-afd7
cw task show T-1783253838063-6770
cw task show T-1783253838063-807e
cw task show T-1783253838063-59b0
cw task show T-1783309017863-a1b6
```

### 3.2 验证代码实现

```bash
# 查看 Agent Rule Memory 相关文件
cw --search "AgentRulesMixin"
cw --search "agent_rule"
cw --search "get_applicable_rules"

# 查看级联 close 实现
cw --search "task_apply"
cw --search "task_close"
cw --search "_cascade_close_if_ready"

# 查看文件符号
cw --file db/db_agent_rules.py
cw --file db/db_tasks.py
cw --file cli/main.py
cw --file server/mcp_server.py

# 查看特定符号内容
cw --symbol "AgentRulesMixin"
cw --symbol "TaskMixin.task_apply"
cw --symbol "TaskMixin.task_close"
cw --symbol "TaskMixin._cascade_close_if_ready"
```

### 3.3 验证 commit 历史

```bash
# 查看所有 Agent Rule Memory 相关 commit
git log --oneline --since="2026-07-05" -- db/db_agent_rules.py
git log --oneline --since="2026-07-05" -- db/schema.py
git log --oneline --since="2026-07-05" -- server/mcp_server.py
git log --oneline --since="2026-07-05" -- cli/main.py
git log --oneline --since="2026-07-06" -- db/db_tasks.py  # 级联 close 部分
git log --oneline --since="2026-07-06" -- tests/test_task_cascade_close.py
git log --oneline --since="2026-07-06" -- docs/design/task-state-machine.md
```

### 3.4 运行回归测试

```bash
# 全量测试
python -m pytest tests/ --tb=short

# 重点测试
python -m pytest tests/test_agent_rules.py -v
python -m pytest tests/test_task_close.py -v
python -m pytest tests/test_task_cascade_close.py -v
python -m pytest tests/test_task_quality_gate.py -v

# 确认测试数量
python -m pytest tests/ --collect-only -q | Select-String "test"
```

### 3.5 验证数据库 schema

```bash
# 查看 schema 版本
cw --status

# 确认 v24 迁移
python -c "from callwarden.db.db import CodeGraphDB; db = CodeGraphDB(); print('Schema version:', db.get_schema_version()); db.close()"
```

### 3.6 验证 i18n key

```bash
# 检查 i18n key 是否齐全
python -c "
import json
with open('i18n/zh_CN.json', encoding='utf-8') as f:
    zh = json.load(f)
with open('i18n/en_US.json', encoding='utf-8') as f:
    en = json.load(f)

# Agent Rule Memory 相关 key
agent_keys = [k for k in zh.keys() if 'agent_rule' in k or 'rule_' in k.lower()]
print(f'Agent Rule keys in zh_CN: {len(agent_keys)}')

# 级联 close 相关 key
cascade_keys = [k for k in zh.get('cli', {}).get('messages', {}).keys()
                if 'task_apply' in k or 'task_close' in k or 'cascade' in k]
print(f'Cascade close keys in zh_CN: {len(cascade_keys)}')
for k in cascade_keys:
    print(f'  - {k}: zh={zh[\"cli\"][\"messages\"][k][:30]}... en={en[\"cli\"][\"messages\"][k][:30]}...')
"
```

## 四、审计检查清单

对每个子任务逐项检查：

### 子任务 1: T-1783253838062-494d (Schema 与迁移)
- [ ] `db/schema.py` 中存在 `agent_rule_candidates` / `agent_rules` / `agent_rule_sync_log` 三张表
- [ ] `SCHEMA_VERSION = 24`（含 v24 迁移）
- [ ] `db/db_base.py` 中有 `_migrate_v22_to_v23` 和 `_migrate_v23_to_v24` 方法
- [ ] 迁移是幂等的（PRAGMA table_info 检查字段存在性）

### 子任务 2: T-1783253838062-1917 (AgentRulesMixin CRUD)
- [ ] `db/db_agent_rules.py` 中存在 `AgentRulesMixin` 类
- [ ] 实现了候选规则 CRUD：`add_candidate` / `list_candidates` / `accept_candidate` / `reject_candidate`
- [ ] 实现了生效规则 CRUD：`list_rules` / `delete_rule`
- [ ] 实现了同步日志：`log_sync` / `list_sync_log`

### 子任务 3: T-1783253838062-6687 (作用域匹配)
- [ ] 实现了 `get_applicable_rules` 方法
- [ ] 支持 `scope_json` 匹配：空 scope = 全局；同字段 OR；不同字段 AND
- [ ] 支持 `file_patterns` glob 匹配
- [ ] 支持 `module_prefixes` 前缀匹配
- [ ] 排序规则：severity 优先 → 匹配字段数 → updated_at desc

### 子任务 4: T-1783253838063-f35d (注入 task_next_step / work_next_job)
- [ ] `task_next_step` 返回值包含 `applicable_rules` 字段
- [ ] `work_next_job` 返回值包含 `project_rules` + `context.applicable_rules`
- [ ] 注入失败时 fail-soft 降级为空列表（不抛异常）

### 子任务 5: T-1783253838063-afd7 (注入 get_symbol / file_symbol_content)
- [ ] `get_symbol` 返回值包含 `applicable_rules` 字段
- [ ] `file_symbol_content` 返回值包含 `applicable_rules` 字段
- [ ] 注入失败时 fail-soft 降级为空列表

### 子任务 6: T-1783253838063-6770 (自动提取规则候选)
- [ ] 实现了从 `task_quality_findings` 提取候选规则的方法
- [ ] 提取的候选规则 status=`pending`（需人审后才生效）
- [ ] 重复提取不会产生重复记录（幂等）

### 子任务 7: T-1783253838063-807e (AGENTS.md 安全同步)
- [ ] 实现了 `sync_to_agents_md` 方法
- [ ] 使用 marker block 格式：`<!-- CALLWARDEN_RULES_START -->` ... `<!-- CALLWARDEN_RULES_END -->`
- [ ] 包含提示注释"自动同步区域，请通过 cw rule sync 更新，不要手改"
- [ ] 默认 dry-run 模式（不直接写文件，需 `--apply` 参数）

### 子任务 8: T-1783253838063-b70c (CLI/MCP 暴露 + i18n)
- [ ] CLI 新增 `cw rule` 命令组：candidate / list / applicable / sync / insert-block / extract
- [ ] MCP 工具注册：`agent_rule_*` 系列 9 个工具
- [ ] `i18n/zh_CN.json` 和 `i18n/en_US.json` 含 53+ 个 Agent Rule 相关 key
- [ ] CLI 输出全部走 i18n（无硬编码字符串）

### 子任务 9: T-1783253838063-59b0 (文档 + 回归验证)
- [ ] `docs/cli_reference.md` 含 Rule Memory 命令组章节
- [ ] `docs/mcp_tools.md` 含 Agent Rule Memory 工具章节（9 个工具）
- [ ] `docs/architecture.md` 含 Agent Rule Memory 表（v23）和架构章节
- [ ] 203 测试全部通过

### 子任务 10: T-1783309017863-a1b6 (级联 close 实现)
- [ ] `db/db_tasks.py` 中 `task_apply` 支持级联 close
- [ ] 新增 `_cascade_close_if_ready` 辅助方法
- [ ] `task_close` 拒绝父任务手动 close（返回 `reason=parent_task_must_cascade`）
- [ ] `task_next_step` 领取子任务时推进父任务状态 open → in_progress
- [ ] `_update_parent_status` 实现父任务 in_progress → review 自动推进
- [ ] `i18n/zh_CN.json` 和 `i18n/en_US.json` 新增 6 个级联 close 相关 key
- [ ] `tests/test_task_cascade_close.py` 包含 10 个测试
- [ ] `docs/design/task-state-machine.md` 设计文档存在
- [ ] `docs/architecture.md` 含任务级联 close 章节
- [ ] `README.md` 更新任务驱动编排特性说明
- [ ] `docs/cli_reference.md` 含 task apply/close 级联说明 + 示例 8
- [ ] `docs/mcp_tools.md` 含 task_apply/task_close 工具文档
- [ ] 223 测试全部通过（10 新 + 213 旧）

## 五、审计通过后的 close 流程

### 5.1 关键约束

**重要**：当前已实现级联 close 机制，规则如下：
- **叶子任务**（无子任务）：可手动 `cw task apply` + `cw task close`
- **父任务**（有子任务）：**禁止手动 apply/close**，由最后一个子任务 apply 时自动级联触发

父任务 T-1783253838055-f033 有 10 个子任务，属于父任务，**不能手动 apply/close**。

### 5.2 close 子任务的正确顺序

**关键**：必须**按顺序逐个 apply + close**，最后一个子任务 apply 时会触发级联 close 自动关闭父任务。

推荐顺序（按 Phase 顺序）：

```bash
# Phase 1: Schema 与 CRUD
cw task apply T-1783253838062-494d --reviewer "你的审核人标识"
cw task close T-1783253838062-494d --reviewer "你的审核人标识"

cw task apply T-1783253838062-1917 --reviewer "你的审核人标识"
cw task close T-1783253838062-1917 --reviewer "你的审核人标识"

# Phase 2: 作用域匹配
cw task apply T-1783253838062-6687 --reviewer "你的审核人标识"
cw task close T-1783253838062-6687 --reviewer "你的审核人标识"

# Phase 3-4: 注入点
cw task apply T-1783253838063-f35d --reviewer "你的审核人标识"
cw task close T-1783253838063-f35d --reviewer "你的审核人标识"

cw task apply T-1783253838063-afd7 --reviewer "你的审核人标识"
cw task close T-1783253838063-afd7 --reviewer "你的审核人标识"

# Phase 5: 自动提取
cw task apply T-1783253838063-6770 --reviewer "你的审核人标识"
cw task close T-1783253838063-6770 --reviewer "你的审核人标识"

# Phase 6: AGENTS.md 同步
cw task apply T-1783253838063-807e --reviewer "你的审核人标识"
cw task close T-1783253838063-807e --reviewer "你的审核人标识"

# Phase 7: CLI/MCP + i18n
cw task apply T-1783253838063-b70c --reviewer "你的审核人标识"
cw task close T-1783253838063-b70c --reviewer "你的审核人标识"

# Phase 8: 文档 + 回归
cw task apply T-1783253838063-59b0 --reviewer "你的审核人标识"
cw task close T-1783253838063-59b0 --reviewer "你的审核人标识"

# 级联 close 实现（最后一个 apply 会触发级联关闭父任务）
cw task apply T-1783309017863-a1b6 --reviewer "你的审核人标识"
# ↑ 这一步 apply 后会触发级联 close：
#   - 自动 close T-1783309017863-a1b6 自己
#   - 自动 close 父任务 T-1783253838055-f033
# 返回值会包含 cascaded_close: [T-1783309017863-a1b6, T-1783253838055-f033]
# 不需要再手动 close！
```

### 5.3 验证级联 close 是否成功

```bash
# 最后一个子任务 apply 后，检查任务树状态
cw task show T-1783253838055-f033

# 预期结果：
# - 父任务 T-1783253838055-f033 状态为 closed
# - 所有 10 个子任务状态都是 closed
# - 不应该有任何任务停留在 applied 或 review 状态
```

### 5.4 如果级联未触发

若最后一个子任务 apply 后父任务仍为 review，说明级联 close 逻辑有问题，排查步骤：

```bash
# 1. 检查所有子任务状态（确认都是 applied 或 closed）
cw task show T-1783253838055-f033

# 2. 若有子任务还是 review，说明该子任务还没被 apply
# → 先 apply + close 该子任务，再 apply 最后一个

# 3. 若所有子任务都是 applied/closed 但父任务还是 review
# → 检查 _cascade_close_if_ready 方法是否有 bug
cw --symbol "TaskMixin._cascade_close_if_ready"
```

### 5.5 审计失败的处理

若审计发现某个子任务不符合要求（如代码缺失、测试失败、文档未更新）：

```bash
# 1. 不要 apply 该子任务
# 2. 创建修复任务挂在父任务下
cw task create --title "修复 <子任务标题> 的 <具体问题>" --parent-id T-1783253838055-f033

# 3. 完成修复后再 apply + close
# 4. 其他正常的子任务可以先 apply + close，不受影响
```

## 六、审计报告模板

审计完成后，输出以下报告：

```markdown
# Agent Rule Memory 任务审计报告

## 审计概览
- 审计人: <你的标识>
- 审计时间: <YYYY-MM-DD HH:MM>
- 父任务: T-1783253838055-f033
- 子任务数: 10
- 测试通过: <数量> / <总数>

## 子任务审计结果

| # | 子任务 ID | 标题 | 代码 | Commit | 测试 | 文档 | i18n | 结果 |
|---|----------|------|------|--------|------|------|------|------|
| 1 | T-1783253838062-494d | Schema 与迁移 | ✓ | ✓ | ✓ | N/A | N/A | PASS |
| 2 | ... | ... | ... | ... | ... | ... | ... | ... |

## 关键发现
- <发现的优点>
- <发现的问题>

## Close 执行结果
- 已 close 子任务: <数量> / 10
- 父任务状态: <closed / review>
- 级联触发: <是 / 否>

## 结论
<审计通过 / 需修复 / 审计失败>
```

## 七、重要提醒

1. **不要跳过审计直接 close** — 必须逐个验证代码、commit、测试、文档
2. **不要批量 apply** — 必须逐个 apply + close，便于追踪问题
3. **父任务不能手动 close** — 必须由最后一个子任务 apply 时级联触发
4. **审核人标识要明确** — `--reviewer` 参数填入你的会话标识，便于追溯
5. **测试必须全部通过** — 若有失败，先修复再 apply
6. **i18n key 必须对齐** — zh_CN 和 en_US 必须都有对应 key

## 八、快速验证脚本

可一键运行以下命令快速验证整体状态：

```bash
# 一键查看所有子任务状态
python -c "
from callwarden.db.db import CodeGraphDB
db = CodeGraphDB()
import time
cur = db.conn.execute('SELECT id, title, status FROM tasks WHERE parent_id = ? ORDER BY sort_order', ('T-1783253838055-f033',))
for row in cur:
    print(f'{row[\"status\"]:12s} {row[\"id\"]} {row[\"title\"]}')
db.close()
"

# 一键运行所有测试
python -m pytest tests/ --tb=short -q

# 一键查看 commit 数量
git log --oneline --since="2026-07-05" | Measure-Object -Line
```

---

**审计完成后，将本报告提交给原任务创建者，确认任务关闭。**
