# Agent Rule Memory 设计计划

## 背景

Call Warden 已经有任务系统、结构化工作入口、质量发现表和护栏规则，但还缺少一层“项目规则记忆”：

- 任务完成后发现的问题，无法自动沉淀为下一次任务的上下文。
- `AGENTS.md` 是 Agent 入口，但直接让模型改它风险太高。
- `task_next_step`、`work_next_job`、`get_symbol`、`file_symbol_content` 目前可以返回代码上下文，但不会返回与当前任务、文件、语言、符号相关的项目规则。

目标不是让 Call Warden 对 Agent 说“我是最好的”，而是让 Agent 在领取任务或读取函数时自然拿到最短、最相关、最可执行的规则。这样不用 Call Warden 反而更累、更容易漏。

## 设计原则

1. **规则先入库，再同步文档**：自动提取只生成候选规则，默认不直接改 `AGENTS.md`。
2. **人审后生效**：候选规则必须经过 accept/reject，只有 accepted 规则会被注入任务和函数上下文。
3. **注入要短**：每次只返回与上下文匹配的 Top N 规则，避免污染 token。
4. **结构化优先**：MCP 返回结构化字段，AGENTS.md 只是兼容没有 MCP 的 Agent。
5. **可审计**：规则来源、证据、接受人、同步 hash 都要可追踪。
6. **不复用 guardrail_rules**：`guardrail_rules` 用于阻断安全问题，`agent_rules` 用于 Agent 工作约束。两者可以互相引用，但生命周期不同。

## 现有基础

- `db/db_tasks.py`
  - `task_next_step()` 已返回 `structured_instruction`。
  - `work_next_job()` 已返回目标源码、调用方/被调用方、允许编辑范围和推荐工具。
- `server/mcp_server.py`
  - `task_next_step`、`work_next_job` 是薄包装，适合直接透传 DB 新字段。
  - `get_symbol`、`file_symbol_content` 是函数级规则注入点。
- `db/schema.py`
  - 已有 `task_quality_findings`，后续可作为规则候选来源。
- `db/db_guardrail.py`
  - 已有 `guardrail_rules`，可作为机器阻断规则来源，但不适合直接承载 Agent 经验规则。

## 数据模型

### `agent_rule_candidates`

候选规则表。自动提取、人工创建、任务复盘都写入这里。

```sql
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
```

### `agent_rules`

已接受规则表。只有这里的 active 规则会参与上下文注入和 AGENTS.md 同步。

```sql
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
```

### `agent_rule_sync_log`

记录每次同步 `AGENTS.md` 的摘要，后续接入审计签名链。

```sql
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
```

## 规则作用域

`scope_json` 使用 JSON 存储，先支持这些字段：

```json
{
  "languages": ["python"],
  "file_patterns": ["cli/*.py", "server/*.py"],
  "symbol_kinds": ["function", "method"],
  "actions": ["edit", "fix", "review"],
  "finding_types": ["i18n", "semgrep", "signature"],
  "module_prefixes": ["cli.", "server."]
}
```

匹配策略：

- 空 scope 表示全局规则。
- 同一字段内是 OR，不同字段之间是 AND。
- 文件路径支持 glob。
- `limit` 默认 5，按 severity、scope 命中精度、更新时间排序。

## 新增 DB Mixin

新增 `db/db_agent_rules.py`，类名 `AgentRulesMixin`。

核心方法：

```python
def rule_candidate_create(
    self,
    title: str,
    rule_text: str,
    scope: dict | None = None,
    severity: str = "info",
    source: str = "manual",
    evidence: dict | None = None,
    confidence: float = 0.0,
) -> str:
    """创建候选规则。"""

def rule_candidate_list(
    self,
    status: str = "pending",
    limit: int = 50,
) -> list[dict]:
    """列出候选规则。"""

def rule_candidate_accept(
    self,
    candidate_id: str,
    reviewer: str = "agent",
) -> str:
    """接受候选规则，写入 agent_rules。"""

def rule_candidate_reject(
    self,
    candidate_id: str,
    reviewer: str = "agent",
    reason: str = "",
) -> bool:
    """拒绝候选规则。"""

def rule_list(
    self,
    status: str = "active",
    limit: int = 100,
) -> list[dict]:
    """列出已生效规则。"""

def get_applicable_rules(
    self,
    context: dict,
    limit: int = 5,
) -> list[dict]:
    """根据任务、文件、符号、动作等上下文返回适用规则。"""

def extract_rule_candidates_from_quality_findings(
    self,
    task_id: str = "",
    min_occurrences: int = 2,
) -> list[str]:
    """从 task_quality_findings 中聚合重复问题并生成候选规则。"""

def rule_sync_agents_md(
    self,
    target_path: str = "AGENTS.md",
    dry_run: bool = True,
    actor: str = "agent",
) -> dict:
    """把 active 规则同步到 AGENTS.md 标记区。"""
```

## 注入点设计

### `task_next_step`

在返回值中增加：

```json
{
  "applicable_rules": [
    {
      "id": "AR-...",
      "title": "用户可见输出必须走 i18n",
      "rule_text": "修改 return/print/log 输出时，不要硬编码用户可见文本，应新增 i18n key。",
      "severity": "warning",
      "matched_scope": ["language:python", "action:edit"]
    }
  ]
}
```

同时把规则摘要放入 `structured_instruction.project_rules`，让只读 `structured_instruction` 的 Agent 也能看到。

### `work_next_job`

在 job 顶层增加：

```json
{
  "project_rules": [...],
  "context": {
    "target_symbol": {...},
    "applicable_rules": [...]
  }
}
```

`work_next_job` 是推荐主入口，因此这里应返回更完整但仍受 limit 控制的规则。

### `get_symbol`

不破坏原字段，在返回 dict 中追加：

```json
{
  "applicable_rules": [...]
}
```

上下文由 `qualified_name`、`file_path`、`kind`、语言推断。

### `file_symbol_content`

返回源码片段时追加：

```json
{
  "applicable_rules": [...]
}
```

这是精确函数级读取入口，适合返回最贴近当前函数的规则。

## AGENTS.md 同步策略

只修改标记区，不触碰人工维护内容。

推荐区块：

```markdown
## Call Warden 自动沉淀规则

<!-- CALLWARDEN_RULES_START -->
<!-- 自动同步区域，请通过 cw rule sync 更新，不要手改 -->
<!-- CALLWARDEN_RULES_END -->
```

行为：

- `dry_run=True` 默认只返回 diff/preview。
- `dry_run=False` 才写文件并记录 `agent_rule_sync_log`。
- 如果标记区不存在，默认返回错误和建议插入块；CLI 可提供 `--insert-block` 显式插入。
- 同步内容包含规则 id，便于从文档追溯到数据库。

## 自动提取来源

第一阶段只做保守提取：

1. `task_quality_findings`
   - 同类问题重复出现达到阈值时生成候选规则。
   - 例如 `finding_type=i18n` 在 Python CLI 文件重复出现，生成“用户可见输出必须使用 i18n key”。
2. `guardrail_findings`
   - 可生成候选经验规则，但不自动降级或升级 guardrail。
3. `task_report_step` 结果
   - 如果步骤失败原因重复，可以生成候选规则。
4. 人工创建
   - 用户或 Agent 可以直接创建候选规则。

第二阶段再接入 Semgrep 规则聚合、代码 review 发现和变更归因发现。

## CLI 与 MCP

新增 CLI：

```bash
cw rule candidate create
cw rule candidate list
cw rule candidate accept <candidate_id>
cw rule candidate reject <candidate_id>
cw rule list
cw rule applicable --file cli/main.py --action edit
cw rule sync --dry-run
cw rule sync --apply
```

新增 MCP：

- `rule_candidate_create`
- `rule_candidate_list`
- `rule_candidate_accept`
- `rule_candidate_reject`
- `rule_list`
- `get_applicable_rules`
- `rule_sync_agents_md`
- `extract_rule_candidates_from_quality_findings`

所有 CLI 用户可见输出必须走 `i18n/zh_CN.json` 和 `i18n/en_US.json`。

## 与审计签名的关系

规则系统要预留审计签名字段，但第一阶段可以先不强制阻断。

后续接入：

- accept/reject 候选规则时写审计链。
- 同步 AGENTS.md 时写审计链。
- `verify_audit_chain` 能检查规则状态和同步记录是否被篡改。

## 实施阶段

### Phase 1：最小规则库

- 增加 schema 与迁移。
- 新增 `AgentRulesMixin`。
- 支持候选规则 create/list/accept/reject。
- 支持 active rule list。

### Phase 2：适用规则匹配

- 实现 `get_applicable_rules(context, limit)`。
- 支持语言、文件 glob、符号类型、动作、finding_type、模块前缀。
- 增加排序和 limit。

### Phase 3：任务入口注入

- 修改 `task_next_step` 返回 `applicable_rules`。
- 修改 `build_structured_instruction` 返回 `project_rules`。
- 修改 `work_next_job` 返回 `project_rules`。

### Phase 4：函数入口注入

- 修改 `get_symbol` 返回 `applicable_rules`。
- 修改 `file_symbol_content` 返回 `applicable_rules`。
- 如担心兼容性，可新增 `get_symbol_context`，但优先直接追加字段。

### Phase 5：候选规则自动提取

- 从 `task_quality_findings` 聚合重复问题。
- 生成 pending candidate，不自动接受。
- 支持 task_id 过滤和 min_occurrences。

### Phase 6：AGENTS.md 同步

- 实现 marker block 检测、dry-run preview、apply 写入。
- 记录 `agent_rule_sync_log`。
- 文档说明 AGENTS.md 是只读入口，规则源头在 DB。

### Phase 7：CLI/MCP/i18n/测试

- 新增 CLI/MCP 工具。
- 更新 `docs/cli_reference.md`、`docs/mcp_tools.md`、`docs/architecture.md`。
- 新增中英文 i18n key。
- 单元测试覆盖 schema、匹配、注入、同步、提取。

## 验收标准

1. 候选规则默认 pending，不会自动影响 Agent 行为。
2. accepted 规则会出现在 `task_next_step`、`work_next_job`、`get_symbol`、`file_symbol_content` 返回值中。
3. `get_applicable_rules` 能根据文件、语言、动作、符号类型过滤规则。
4. `AGENTS.md` 同步默认 dry-run，apply 只修改 marker block。
5. 没有 marker block 时不会静默改全文。
6. CLI 输出全部走 i18n。
7. 新增功能有单元测试，且不破坏现有任务系统测试。

## 推荐测试

```bash
python -m py_compile db/db_agent_rules.py db/db_tasks.py db/db_query.py server/mcp_server.py cli/main.py
python -m pytest tests/test_agent_rules.py tests/test_agent_integration.py tests/test_task_quality_gate.py -q
```

