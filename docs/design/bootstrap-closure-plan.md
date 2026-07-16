# 自举闭环需求与实施计划

## 背景

Call Warden 已经具备任务状态机、Agent Rule Memory、Task Quality Gate、Audit Chain、Semgrep、变更归因等模块，但真实自举链路还没有完全打通：

- 真实开发改动可能由 Codex/Claude/Cursor 自带编辑器完成，绕过 `propose_symbol_patch` / `propose_range_patch`。
- `change_audit`、`file_edit_audit`、`task_symbol_changes`、`audit_chain` 在真实项目库中可能为空，导致任务质量门禁缺少事实依据。
- `agent_rules` 为空时，规则注入机制空转。
- 文档已有 `cw audit verify` / MCP `audit_chain_verify` 描述，但 CLI/MCP 接线需要校验并补齐。
- 数据库能知道文件 `mtime` 和版本 hash，但缺少“上一次自举扫描基线”，无法快速判断两次任务之间真实变更了哪些文件。

目标：让 Call Warden 从“代码知识图谱工具”推进到“能监督自己开发过程的自举执行管道”。

## 总目标

建立三段闭环：

1. **capture-diff**：把外部 Agent 的真实文件改动捕获到 task/change/symbol/audit。
2. **audit verify + bootstrap status**：一条命令判断当前自举健康度。
3. **seed active rules + review pipeline**：让规则注入、质量门禁、任务 apply/close 真正产生约束。

## Scan Baseline 设计

### 为什么需要新表

`file_instances.mtime` 和 `file_versions.content_hash` 适合表达“当前代码图谱看到的文件状态”，但不适合表达“某次任务/某次扫描从哪个基线开始”。自举执行需要回答：

- 上一次 capture/review 是哪个 commit？
- 如果工作区未提交，上次扫描时 dirty 文件有哪些？
- 非 git 项目如何判断两次扫描之间变了什么？
- 这次 capture-diff 关联的是哪个 task/step？

因此建议新增扫描运行表，而不是只在 `workspaces` 上加字段。

### 新表：`workspace_scan_runs`

记录每次自举扫描或捕获的基线。

```sql
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
```

索引：

```sql
CREATE INDEX IF NOT EXISTS idx_workspace_scan_runs_workspace
ON workspace_scan_runs(workspace_id, purpose, started_at);

CREATE INDEX IF NOT EXISTS idx_workspace_scan_runs_task
ON workspace_scan_runs(task_id, step_id);

CREATE INDEX IF NOT EXISTS idx_workspace_scan_runs_git_head
ON workspace_scan_runs(git_head);
```

### 可选表：`workspace_scan_files`

非 git 项目或需要极精确 dirty 检测时使用。第一阶段可以先不做，优先复用 `file_instances`。

```sql
CREATE TABLE IF NOT EXISTS workspace_scan_files (
    scan_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    mtime REAL DEFAULT 0,
    size INTEGER DEFAULT 0,
    content_hash TEXT DEFAULT '',
    status TEXT DEFAULT 'seen',
    PRIMARY KEY (scan_id, rel_path),
    FOREIGN KEY (scan_id) REFERENCES workspace_scan_runs(id)
);
```

### 变化检测策略

优先级：

1. **Git 项目**
   - 基线有 `git_head`：使用 `git diff --name-status <git_head>...HEAD`。
   - 同时读取 `git status --porcelain=v1` 捕获 staged/unstaged/untracked。
   - `git_status_hash` 用于快速判断 dirty 状态是否变化。
2. **非 Git 项目**
   - 使用 `file_instances.mtime/current_content_hash` 对比当前扫描结果。
   - 如果启用 `workspace_scan_files`，用 manifest 逐文件比较。
3. **目录 mtime**
   - 只作为快速提示，不作为唯一真相源。目录 mtime 在深层文件变更、Windows、工具批量写入场景下不可靠。

## Phase 1：capture-diff

### 用户故事

当 Agent 用外部编辑器改了文件后，调用：

```bash
cw task capture-diff <task_id> --step-id <step_id>
```

Call Warden 应：

1. 读取上次 scan baseline 或指定 `--base`。
2. 找到变更文件。
3. 对变更文件执行刷新。
4. 写入 `change_audit`。
5. 尽可能关联 `task_symbol_changes`。
6. 调用 `run_task_completion_review`。
7. 写入 `audit_chain`。
8. 返回 changed files、linked symbols、quality findings、下一步建议。

### CLI

```bash
cw task capture-diff <task_id>
cw task capture-diff <task_id> --step-id <step_id>
cw task capture-diff <task_id> --base HEAD~1
cw task capture-diff <task_id> --dry-run
```

### MCP

```text
task_capture_diff(task_id, step_id="", base="", dry_run=True)
```

### DB 方法

新增 `db/db_bootstrap.py` 或放入独立 mixin：

```python
def record_workspace_scan_run(
    self,
    purpose: str = "bootstrap",
    task_id: str = "",
    step_id: str = "",
    status: str = "running",
    metadata: dict | None = None,
) -> int:
    """记录扫描基线。"""

def get_workspace_changes_since(
    self,
    scan_id: int = 0,
    base_commit: str = "",
    include_untracked: bool = True,
) -> dict:
    """返回两次扫描/commit 之间的变更文件。"""

def task_capture_diff(
    self,
    task_id: str,
    step_id: str = "",
    base: str = "",
    dry_run: bool = True,
) -> dict:
    """把外部 diff 纳入 task/change/symbol/audit/review。"""
```

### 验收标准

- dry-run 不写审计表，只返回将要捕获的文件。
- 非 dry-run 写入 `workspace_scan_runs` 和 `change_audit`。
- 刷新变更文件后，能生成 `task_symbol_changes`。
- 成功后自动调用 `run_task_completion_review`。
- 有 blocking finding 时，任务保持 review/blocking，不允许误 close。

## Phase 2：audit verify + bootstrap status

### CLI

```bash
cw audit verify
cw audit verify --table change_audit
cw bootstrap status
```

### MCP

```text
audit_chain_verify(table_name="", limit=1000)
bootstrap_status()
```

### `bootstrap status` 输出

至少包含：

- DB 是否 stale。
- active rules 数量。
- pending rule candidates 数量。
- open/blocking quality findings 数量。
- audit_chain verify 结果。
- 最近一次 `workspace_scan_runs`。
- 最近 review/open/applied 任务。
- 推荐下一条命令。

### 验收标准

- CLI/MCP 都能调用 `verify_audit_chain`。
- `bootstrap_status` 能在 3 秒内返回摘要。
- 输出全部走 i18n。
- 文档与实际命令一致。

## Phase 3：seed active rules + review pipeline

### 种子规则

新增命令：

```bash
cw rule seed-bootstrap --dry-run
cw rule seed-bootstrap --apply
```

建议内置规则：

1. 用户可见输出必须使用 i18n key。
2. 提交前必须刷新代码图谱。
3. 大任务必须通过 Call Warden task 拆分并推进。
4. 任务完成后必须运行 completion review，blocking finding 未解决不得 apply/close。
5. 外部编辑完成后必须运行 `task capture-diff`。

### Review pipeline

建议把任务完成流程标准化为：

```text
work_next_job
→ 外部编辑或 propose_symbol_patch
→ cw task capture-diff
→ cw task findings
→ cw rule extract
→ 人审 accept/reject rule candidates
→ cw task apply/close
```

### 验收标准

- seed 后 `agent_rules` 至少有 5 条 active 规则。
- `task_next_step` / `work_next_job` / `get_symbol` / `file_symbol_content` 能返回适用规则。
- `cw rule sync --dry-run` 能预览 AGENTS.md 标记区变更。
- 完整跑一条自举样例任务，产生 scan_run、change_audit、audit_chain、quality review 结果。

## 推荐任务拆分

1. Schema：新增 workspace_scan_runs，可选 workspace_scan_files。
2. Baseline：实现 git/non-git 变化检测。
3. Capture：实现 `task_capture_diff` DB 方法。
4. CLI/MCP：暴露 `cw task capture-diff` 与 MCP。
5. Audit：补齐 `cw audit verify` 与 MCP `audit_chain_verify`。
6. Bootstrap status：实现 `cw bootstrap status` / MCP。
7. Rules：实现 `cw rule seed-bootstrap`。
8. 文档/i18n/测试：更新文档并覆盖核心路径。

## 推荐测试

```bash
python -m pytest tests/test_bootstrap_capture.py tests/test_audit_chain_mixin.py tests/test_task_quality_gate.py -q
python -m pytest tests/test_agent_rules.py tests/test_agent_integration.py -q
```

