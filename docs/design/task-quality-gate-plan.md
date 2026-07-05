# Task Quality Gate 与审计签名设计计划

## 背景

Call Warden 已经具备任务状态机、编辑审计、Semgrep 增量扫描、护栏规则和任务-符号变更归因。但当前这些能力还没有完全收束成一个“任务完成质量门禁”：

- Agent 可以在 `task_report_step` 中声明步骤完成。
- `run_check_gate` 已能执行语法检查和 Semgrep，但发现主要写入 `guardrail_findings`。
- 质量问题尚未作为 task/step 的一等状态存在。
- `cw task status` 还不能直接回答“这个任务为什么不能 done”。
- 审计记录有 hash，但缺少防直接改库的签名链。

目标是把 Call Warden 从“提醒 Agent 关注质量”升级成“Agent 不修复质量问题就无法让任务变绿”的执行状态机。

## 当前距离目标有多远

已有能力约覆盖 50%-60%：

- `db/db_tasks.py`
  - `task_next_step` 已有编辑前 guardrail 检查。
  - `task_report_step` 已记录 `change_audit`，并尝试调用 `run_check_gate`。
  - `work_next_job` 已能返回结构化指令和推荐工具。
- `db/db_check_gate.py`
  - `run_check_gate` 已支持 changed files 语法检查和 Semgrep 增量扫描。
  - `resolve_gate_findings` 已能按任务关联文件将 `guardrail_findings` 标记为 resolved。
- `db/db_task_attribution.py`
  - 已能把编辑/变更关联到任务和符号。
- `db/schema.py`
  - 已有 `tasks`、`task_steps`、`change_audit`、`guardrail_findings`、`file_edit_audit`、`task_symbol_changes`。

缺口集中在三处：

1. 缺少 task 级质量发现表。
2. 缺少 completion review 统一调度器，把 Semgrep、复杂度、调用链一致性、scope 检查统一转为 task findings。
3. 缺少审计签名链，无法检测直接改 SQLite 数据库导致的审计记录篡改。

## 设计目标

1. 质量问题必须挂到 task/step 上。
2. 有 open blocking finding 时，相关 step/task 不得进入 done/closed。
3. `task_next_step` 应优先返回修复质量门禁失败的步骤。
4. `cw task status` / MCP `task_status` 能显示 open/blocking findings。
5. Semgrep 不只是扫描命令，而是任务完成门禁的一部分。
6. 审计记录支持签名链校验，能发现直接改库导致的记录不一致。

## 新增数据库表

### `task_quality_findings`

用于承载任务完成门禁发现，区别于通用 `guardrail_findings`。

```sql
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
```

### `audit_chain`

用于给关键审计表生成可验证的 hash/HMAC 链。

```sql
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
```

第一阶段签名可使用 SHA-256 链；第二阶段再支持 HMAC。若 HMAC key 存在同一个 SQLite 中，只能防误改，不能防有意篡改。更好的做法是从环境变量或用户配置读取：

- `CALLWARDEN_AUDIT_HMAC_KEY`
- 或 `$HOME/.callwarden/audit.key`

## 新增文件

### `db/db_task_quality.py`

新增 `TaskQualityMixin`，承载任务质量门禁和 finding 管理。

建议函数：

```python
def run_task_completion_review(
    self,
    task_id: str,
    step_id: str,
    changed_files: list[str],
    result: str = "",
) -> dict:
    """统一执行任务完成门禁，返回 pass/warn/block。"""
```

```python
def record_task_quality_finding(
    self,
    task_id: str,
    step_id: str = "",
    finding_type: str = "",
    severity: str = "warn",
    message: str = "",
    evidence: dict | str | None = None,
    source: str = "",
) -> int:
    """写入 task_quality_findings。"""
```

```python
def get_task_quality_findings(
    self,
    task_id: str,
    status: str = "open",
    severity: str = "",
) -> list[dict]:
    """查询任务质量发现。"""
```

```python
def resolve_task_quality_finding(
    self,
    finding_id: int,
    resolution: str = "fixed",
    resolved_by: str = "agent",
) -> dict:
    """解决或豁免单条 finding。"""
```

```python
def task_has_blocking_findings(self, task_id: str) -> bool:
    """是否存在 open error/block finding。"""
```

```python
def insert_fix_quality_gate_step(
    self,
    task_id: str,
    source_step_id: str,
    findings: list[dict],
) -> str:
    """为质量门禁失败自动插入修复步骤。"""
```

### `db/db_audit_chain.py`

新增 `AuditChainMixin`，承载审计签名链。

建议函数：

```python
def sign_audit_record(
    self,
    table_name: str,
    record_id: str,
    payload: dict,
    operation: str = "insert",
) -> dict:
    """为关键审计记录写入 audit_chain。"""
```

```python
def verify_audit_chain(
    self,
    table_name: str = "",
    limit: int = 1000,
) -> dict:
    """验证审计链是否连续、签名是否匹配。"""
```

```python
def canonical_json(self, payload: dict) -> str:
    """稳定序列化签名 payload。"""
```

## 需要修改的文件和函数

### `db/schema.py`

- 新增 `task_quality_findings`。
- 新增 `audit_chain`。
- 提升 `SCHEMA_VERSION`。
- 增加版本注释。

### `db/db_base.py`

- 新增 schema migration。
- 迁移函数必须幂等。
- 增加 i18n migration 文案。

### `db/db.py`

- 引入并混入 `TaskQualityMixin`。
- 引入并混入 `AuditChainMixin`。

### `db/db_tasks.py`

#### 修改 `task_report_step`

当前流程应改为：

```text
记录 change_audit
记录 task_symbol_changes
运行 run_task_completion_review
如果 decision=pass:
  step -> done
如果 decision=warn:
  step -> done 或 review，finding 挂 task
如果 decision=block:
  step -> blocked
  task -> in_progress
  插入 fix_quality_gate_failure 步骤
  返回 quality_gate block 结果
```

注意：不要让 Agent 传 `success=True` 就直接完成步骤。`success` 只是 Agent 声明，最终状态由 completion review 决定。

#### 修改 `task_next_step`

- 若任务存在 open blocking findings，优先返回 `fix_quality_gate_failure` 步骤。
- 返回普通步骤时附带 `open_quality_findings` 摘要。

#### 修改 `task_status`

返回字段增加：

```json
{
  "quality_status": "pass|warn|block",
  "open_quality_findings_count": 0,
  "blocking_findings_count": 0,
  "quality_findings": []
}
```

#### 修改 `task_list`

- 支持 `status_filter="blocked"` 或新增 `blocked_only` 参数。
- 返回每个任务的 blocking finding 数量。

### `db/db_check_gate.py`

- 保留 `run_check_gate`，但把它降级为 completion review 的子检查。
- 不再由它直接决定 task 状态。
- 返回结构应足够标准化，方便 `run_task_completion_review` 转为 `task_quality_findings`。

### `server/mcp_server.py`

新增工具：

```python
task_completion_review(task_id, step_id, changed_files)
task_quality_findings(task_id, status="open", severity="")
task_resolve_quality_finding(finding_id, resolution="fixed")
audit_chain_verify(table_name="", limit=1000)
```

### `cli/main.py`

新增或扩展：

```bash
cw task findings <task_id>
cw task resolve-finding <finding_id> [--resolution fixed|waived]
cw task list --blocked
cw audit verify [--table change_audit]
```

### `i18n/zh_CN.json` 与 `i18n/en_US.json`

- 所有新增 CLI 输出必须走 i18n key。
- 包括 migration 文案、finding 展示、audit verify 结果。

### `docs/cli_reference.md`

- 增加 task findings / resolve-finding / list --blocked。
- 增加 audit verify。

### `docs/mcp_tools.md`

- 增加 task quality 和 audit chain 工具说明。

## Completion Review 检查项

第一阶段只做 changed files，避免全库扫描成本过高。

### Semgrep

来源：`run_check_gate` / `run_semgrep`

生成 finding：

- `finding_type="semgrep"`
- `severity` 根据 Semgrep severity 映射。
- `source="semgrep"`
- `evidence` 包含 rule_id、path、line、message。

### 复杂度与坏味道

来源：`check_file_health` / `get_function_metrics`

第一阶段建议只做文件级健康检查：

- 大文件新增 warning。
- 复杂函数新增 warning。
- 后续再做“复杂度增量”。

### 调用链一致性

来源：`task_symbol_changes` + `get_callers` + 当前解析结果。

第一阶段聚焦函数签名：

- 如果 `symbol_hash_before` 与 `symbol_hash_after` 对应 signature 变化，查询旧调用者。
- 如果刷新后存在 unresolved call 或调用者仍指向旧签名，生成 block finding。
- message 示例：`函数 parse_policy 签名已变更，但 3/23 个调用方未更新。`

### Scope violation

来源：`task_steps.target_file/target_symbol` + `change_audit.file_path`

- 当前步骤声明只改一个文件，却改了其他文件，生成 warning/block。
- 当前步骤 target_symbol 非空，但没有 `task_symbol_changes` 关联该符号，生成 warning。

### i18n 硬编码输出

来源：changed files 内容扫描。

第一阶段可用简单规则：

- Python：`print("...")`、`cprint("...")`、`logger.info("...")`
- 只对新增 diff 或 changed files 检查。
- 生成 `finding_type="i18n"`。

## 审计签名范围

第一阶段覆盖：

- `change_audit`
- `file_edit_audit`
- `task_symbol_changes`
- `task_quality_findings`

后续扩展：

- `gc_runs`
- `guardrail_rules`
- `guardrail_findings`

## 状态规则

| finding severity | task/step 行为 |
| --- | --- |
| info | 记录，不阻塞 |
| warn | 记录，可完成，但 task_status 显示 warn |
| error/block | step blocked，自动插入 fix_quality_gate_failure |

当存在 open error/block finding 时：

- `task_report_step` 不得将相关 step 标记为 done。
- `task_status` 必须显示 block。
- `task_next_step` 必须优先返回修复步骤。

## 测试计划

新增 `tests/test_task_quality_gate.py`：

1. `task_report_step` 触发 Semgrep finding 后，step 不变 done。
2. Blocking finding 自动插入 `fix_quality_gate_failure` 步骤。
3. `task_status` 返回 blocking finding 计数。
4. `task_next_step` 优先返回修复步骤。
5. `resolve_task_quality_finding` 后任务可继续。
6. scope violation 能生成 finding。

新增 `tests/test_audit_chain.py`：

1. 插入 `change_audit` 后生成签名链。
2. 直接篡改审计表后 `verify_audit_chain` 返回 failed。
3. 多条记录 prev_signature 连续。

回归测试：

```powershell
python -m py_compile db/db_task_quality.py db/db_audit_chain.py db/db_tasks.py server/mcp_server.py cli/main.py
python -m pytest tests/test_task_quality_gate.py tests/test_audit_chain.py tests/test_task_attribution.py tests/test_agent_integration.py -q
python -m pytest -q --ignore=tests/test_stress.py
git diff --check
$env:PYTHONIOENCODING='utf-8'; python cw.py --refresh-all
```

## 分阶段实施建议

### Phase 1：Task Quality Findings 最小闭环

- 新增 `task_quality_findings`。
- 新增 `TaskQualityMixin`。
- `task_report_step` block 时不放行。
- CLI/MCP 查询 findings。
- 不做签名链。

### Phase 2：Completion Review 扩展

- Semgrep 标准化。
- scope violation。
- changed files file health。
- 初步 i18n 硬编码检查。

### Phase 3：调用链一致性

- 签名变化检测。
- caller 更新遗漏检测。
- unresolved call 检测。

### Phase 4：审计签名链

- `audit_chain` 表。
- 签名关键审计表。
- `cw audit verify`。

## 验收标准

1. Agent 即使忽略返回文本，也无法把带 open blocking finding 的步骤置为 done。
2. `cw task status <task_id>` 能看到质量问题和阻塞原因。
3. `cw task next <task_id>` 会返回修复质量门禁失败的步骤。
4. Semgrep finding 能挂到 task，而不是只停留在扫描报告。
5. 审计链能检测至少一种直接改库篡改。
6. 所有新增用户可见输出支持中英文 i18n。

## 实现状态

截至 2026-07-05，本计划已全部落地：

### Schema（v21 + v22）

- `task_quality_findings` 表（v21）：13 字段 + 4 索引
- `audit_chain` 表（v22）：9 字段 + 2 索引
- SCHEMA_VERSION 22，含 `_migrate_v20_to_v21` / `_migrate_v21_to_v22` 迁移函数

### Mixin 实现

- `db/db_task_quality.py`：`TaskQualityMixin`
  - `record_task_quality_finding` / `get_task_quality_findings` / `resolve_task_quality_finding`
  - `task_has_blocking_findings` / `insert_fix_quality_gate_step`
  - `run_task_completion_review`（聚合 5 个扩展检查器）
  - 5 个检查器：`_check_scope_violations` / `_check_symbol_attribution` /
    `_check_file_health` / `_check_i18n_hardcoded` / `_check_signature_mismatch`
- `db/db_audit_chain.py`：`AuditChainMixin`
  - `canonical_json`：稳定序列化（sort_keys + ensure_ascii=False + 紧凑分隔符）
  - `sign_audit_record`：写入 audit_chain 表，payload_hash + 链式 record_signature
  - `verify_audit_chain`：校验链连续性与签名匹配，返回 broken_records 明细
  - HMAC key 优先级：`CALLWARDEN_AUDIT_HMAC_KEY` > `~/.callwarden/audit.key` > SHA-256 链
  - 无 key 时 `security_level=hash_only`，有 key 时 `security_level=hmac`

### 接线（Wire）

- `db/db.py`：`AuditChainMixin` 混入 `CodeGraphDB` 继承链
- `db/db_tasks.py`：`task_report_step` / `task_rollback` 的 `change_audit` 写入后签名
- `db/db_edit.py`：`file_edit_audit` 状态更新为 applied 后签名（dry_run 跳过）
- `db/db_task_attribution.py`：`task_symbol_changes` 写入后签名
- `db/db_task_quality.py`：`task_quality_findings` 写入后签名
- 所有签名调用使用 `hasattr` 防御性检查 + `try-except` 静默失败

### CLI 命令

- `cw task findings <task_id>`：查看任务质量门禁发现
- `cw task resolve-finding <finding_id>`：解决质量门禁发现
- `cw task list --blocked`：仅显示有阻塞发现的任务
- `cw audit verify [--table <name>]`：验证审计链完整性（待实现 CLI 子命令）

### MCP 工具

- `task_completion_review`：调度器，聚合 run_check_gate + 5 个扩展检查器
- `task_quality_findings`：查询任务质量发现
- `task_resolve_quality_finding`：解决单条 finding
- `audit_chain_verify`：验证审计链（待实现 MCP 工具）

### 测试

- `tests/test_task_quality_gate.py`：schema + TaskQualityMixin 业务方法
- `tests/test_task_quality_cli.py`：CLI 子命令测试
- `tests/test_agent_integration.py`：MCP 工具集成测试
- `tests/test_audit_chain.py`：audit_chain schema + 端到端集成测试（19 个）
- `tests/test_audit_chain_mixin.py`：AuditChainMixin 单元测试（26 个）

### i18n

- 25+ 个 task_quality_* i18n key
- 9 个 audit_verify_* i18n key
- migration_v21 / migration_v22 文案

