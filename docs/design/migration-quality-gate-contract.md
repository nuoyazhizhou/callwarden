# 迁移质量门禁、回滚和任务自举契约（Phase 0 子任务 4 Contract）

> 本文件是 [rust-full-migration-self-bootstrap-plan.md](rust-full-migration-self-bootstrap-plan.md) Phase 0 第四个子任务的契约交付物。
> 它定义迁移过程中**质量门禁规则、回滚机制和任务自举运行方式**，使迁移本身由 Call Warden 持久化、可查询、可审计、可回滚。
>
> 真相源：
> - [migration-manifest.md](migration-manifest.md)（Phase 0-1 产出，生产入口和迁移状态跟踪表）
> - [abi-error-code-contract.md](abi-error-code-contract.md)（Phase 0-2 产出，ABI 和错误码契约）
> - [differential-harness-contract.md](differential-harness-contract.md)（Phase 0-3 产出，差分对照和性能基线）
> - [db/db_task_quality.py](../../db/db_task_quality.py)（质量门禁 Mixin 实现）
> - [db/db_tasks.py](../../db/db_tasks.py)（任务状态机、rollback、reopen 实现）
> - [rust_ext/src/daemon/cas.rs](../../rust_ext/src/daemon/cas.rs)（file_generation_uncommit 实现）
> - [rust_ext/src/bin/cw_daemon.rs](../../rust_ext/src/bin/cw_daemon.rs)（recover_all_workspaces_with_snapshot 实现）
>
> 维护规则：每次新增或修改质量门禁规则、回滚路径或任务自举流程时同步本文件。

## 1. 设计目标

### 1.1 核心目标

- **门禁可执行**：迁移质量门禁不是文档约定，而是由 `task_quality_findings` 表和 `run_task_completion_review` 调度器强制执行的硬门禁
- **回滚可追溯**：每个功能子任务的回滚路径、回滚窗口和回滚条件显式记录在 `rollback_config` 表中，不依赖记忆
- **自举可审计**：迁移任务树本身由 Call Warden 持久化，`cw task status-tree` + `cw task completion-review` 即可审计整个迁移进度
- **门禁与状态机协作**：阻塞 finding 阻止 step 进入 done，自动插入 `fix_quality_gate_failure` 步骤，禁止"门禁失败但任务完成"

### 1.2 设计原则

- **不复制已有能力**：Call Warden 已有任务状态机、质量门禁 Mixin、CAS generation 回滚、daemon 恢复链，本契约只补充迁移专属的 `rollback_config` 和门禁规则
- **门禁 fail-closed**：质量审查失败时 step 阻塞，不允许跳过；只有显式 `skip_quality_review=True`（仅用于紧急回滚）才绕过
- **回滚分层**：任务级回滚（`task_rollback`）+ 状态级回滚（`task_reopen`）+ generation 级回滚（`file_generation_uncommit`）+ 启动级恢复（`recover_all_workspaces_with_snapshot`）各司其职
- **自举最小化**：迁移任务树复用现有 `tasks`/`task_steps`/`task_quality_findings`/`change_audit` 表，不引入迁移专属表

## 2. 质量门禁规则

### 2.1 七步完成协议门禁

每个功能子任务必须按以下 7 步顺序推进，前一步未通过不得进入后一步：

| Step | action | 门禁条件 | 失败处理 |
|---|---|---|---|
| 0 | `contract` | 契约文档存在且引用 manifest 行 | 阻塞，重写契约 |
| 1 | `implement` | Rust 模块编译通过 + 单元测试通过 | 阻塞，修复编译/测试 |
| 2 | `differential-test` | Python/Rust 差分对照通过率 100%（已知缺口除外） | 阻塞，修复 Rust 实现 |
| 3 | `wire-production` | 至少一个真实生产入口接入且不破坏既有测试 | 阻塞，回退接入 |
| 4 | `verify` | 性能无回归（< 1.5x 基线）+ 恢复测试通过 + 安全测试通过 | 阻塞，修复后重测 |
| 5 | `refresh` | `cw --refresh-all` 成功且数据库无 `database is locked` | 阻塞，排查锁冲突 |
| 6 | `review` | `run_task_completion_review` decision ∈ {pass, warn} | 阻塞，插入 `fix_quality_gate_failure` |

### 2.2 门禁执行机制

门禁由 `run_task_completion_review`（[db/db_task_quality.py](../../db/db_task_quality.py)）调度，流程：

1. **清理旧 finding**：删除该 step 的旧 `check_gate` finding，避免重复累积
2. **运行 `run_check_gate`**：对变更文件做语法/Semgrep 检查，结果写入 `task_quality_findings`（source='check_gate'）
3. **运行 5 个扩展检查器**（均使用 source='check_gate'）：
   - `_check_scope_violations`：变更文件超出 `target_file` 范围 → error
   - `_check_symbol_attribution`：`target_symbol` 无 `task_symbol_changes` → warn
   - `_check_file_health_findings`：文件过大/复杂度热点 → warn/error
   - `_check_i18n_hardcoded`：硬编码 print/cprint/logger 输出 → warn
   - `_check_signature_mismatch`：签名变更后调用方未解析 → block/info
4. **决策**：
   - 无 finding → `pass`
   - 仅有 info/warn → `warn`（记录但允许完成）
   - 存在 error/block → `block`（step 阻塞，自动插入 `fix_quality_gate_failure`）

### 2.3 迁移专属门禁规则（本契约新增）

在已有 5 个检查器基础上，迁移子任务额外强制以下规则（通过 `target_file` 和 `target_symbol` 约束 + change_audit 审计实现）：

| 规则 | 检查方式 | 严重度 | 说明 |
|---|---|---|---|
| **G1: 双写禁止** | 变更文件不得同时修改 Python 生产路径和 Rust 对应模块 | block | AGENTS.md 规则：不把 Python 与 Rust 同时写入同一业务表 |
| **G2: Phase 依赖** | 不得在 Phase N 未完成时修改 Phase N+1 的生产入口 | block | manifest §7 跟踪表状态守卫 |
| **G3: 契约对齐** | differential-test step 必须引用 baseline.json 或 golden fixture | warn | 防止差分测试空跑 |
| **G4: 文档同步** | 涉及关键指标变更时必须同步更新文档（AGENTS.md 规则 22） | warn | 由 `_check_scope_violations` 扩展检查 docs/ 目录 |
| **G5: 回滚配置** | wire-production step 必须在 `rollback_config` 表登记回滚路径 | block | 见 §3.4 |

> **实现说明**：G1/G2/G5 通过 `run_task_completion_review` 的扩展检查器实现（本契约 step #1 implement 阶段补充）；G3/G4 通过 `target_file` 约束和 change_audit 审计实现，不新增检查器。

### 2.4 门禁与任务状态机协作

```
task_report_step(step_id, result)
  ├─ run_task_completion_review(task_id, step_id)
  │    ├─ decision = pass  → step → done
  │    ├─ decision = warn  → step → done（记录 finding）
  │    └─ decision = block → step 阻塞
  │                          ├─ insert_fix_quality_gate_step()
  │                          └─ step_status = blocked
  └─ task_has_blocking_findings(task_id) == True → 拒绝进入 done
```

关键不变量：
- `task_has_blocking_findings` 返回 True 时，`task_report_step` 必须拒绝将 step 置为 done
- `fix_quality_gate_failure` step 的 `target_symbol` 字段记录源 step_id，便于追溯
- `skip_quality_review=True` 仅允许用于紧急回滚场景（如生产故障回滚），且必须在 `change_audit.diff` 中记录原因

## 3. 回滚机制

### 3.1 回滚分层

| 层级 | 触发场景 | 实现入口 | 影响范围 |
|---|---|---|---|
| **L0: 任务级回滚** | 整个功能子任务失败，需回退所有变更 | `task_rollback(task_id)` | change_audit 记录回滚意图，task_status = reverted |
| **L1: 状态级回滚** | code review 发现已 applied/closed 的任务有问题 | `task_reopen(task_id)` | task_status 回到 in_progress，递归向上 reopen 祖父链 |
| **L2: generation 级回滚** | 单文件 refresh 失败，需回退 CAS generation | `file_generation_uncommit(workspace_id, rel_path)` | 清除 committed 状态，允许同 seq 重试 |
| **L3: 启动级恢复** | daemon 崩溃后重启，需重放 staging/retry log | `recover_all_workspaces_with_snapshot()` | 重放 staging log + parse_retry_log + snapshot publish |
| **L4: 紧急回滚开关** | 生产故障，需立即切回 Python 路径 | `rollback_config` 表的 `rollback_flag` | 见 §3.4 |

### 3.2 任务级回滚（L0）

**入口**：[db/db_tasks.py](../../db/db_tasks.py) `task_rollback(task_id, change_id=None, reason="")`

**行为**：
- 查询 `change_audit` 表该任务的所有变更记录
- 为每条原始变更记录一条回滚操作（hash 前后对调）
- 将 task_status 置为 `reverted`
- **不操作文件系统**：Call Warden 是图谱系统而非文件系统管理器，只记录回滚意图和元数据
- 调用方根据返回的 `rolled_back_changes` 自行恢复文件内容

**返回结构**：
```python
{
    "rolled_back_changes": [
        {
            "original_change_id": "C-xxx",
            "file_path": "src/main.rs",
            "hash_before": "<原变更前哈希>",
            "hash_after": "<原变更后哈希>",
            "restorable": True,  # bool(change.get("hash_before"))
        }
    ],
    "task_status": "reverted",
    "note": "图谱系统不操作文件系统，调用方自行恢复文件内容",
}
```

### 3.3 状态级回滚（L1）

**入口**：[db/db_tasks.py](../../db/db_tasks.py) `task_reopen(task_id, reviewer, reason)`

**行为**：
- 将 task_status 从 `review`/`applied`/`closed` 回退到 `in_progress`
- 清理 `applied_at`/`closed_at` 时间戳
- 递归向上 reopen 祖父任务链（无条件，不检查兄弟子任务）
- 在 `audit_chain` 中记录 reopen 事件

**两种触发方式**（详见 AGENTS.md §任务 reopen 机制）：
1. **自动触发**：`task_create(parent_id=closed_task)` 时检查兄弟子任务状态决定是否 reopen 父任务
2. **手动触发**：`cw task reopen <task_id> --reason "..."` 直接 reopen 整条祖先链

### 3.4 generation 级回滚（L2）和紧急回滚开关（L4）

**L2 入口**：[rust_ext/src/daemon/cas.rs](../../rust_ext/src/daemon/cas.rs) `CasStore::file_generation_uncommit(workspace_id, rel_path)`

**行为**：
- 清除 `file_generation_committed` 表中该 (workspace_id, rel_path) 的 committed 状态
- 允许同一 `session_epoch` + `monotonic_seq` 重新尝试 publish
- 不删除 CAS 事实文件（事实不可变，只回滚 generation 状态）

**L4 紧急回滚开关**：本契约新增 `rollback_config` 表（见 §5.1），每个功能子任务在 wire-production step 必须登记：

| 字段 | 说明 | 示例 |
|---|---|---|
| `task_id` | 关联的迁移子任务 ID | `T-1785148066852-10a8673b` |
| `feature_name` | 功能名称 | `rust_sqlite_connection` |
| `phase` | 所属阶段 | `1` |
| `production_entry` | 生产入口路径 | `db/db_base.py:CodeGraphDB._connect` |
| `rollback_entry` | 回滚入口路径（切回 Python 的代码位置） | `db/db_base.py:CodeGraphDB._connect_python_fallback` |
| `rollback_flag` | 当前是否回滚到 Python（0=正常 Rust，1=已回滚） | `0` |
| `rollback_window_until` | 回滚窗口有效期（ISO8601），过期后回滚入口将被删除 | `2026-10-27T00:00:00` |
| `config_blob` | 额外配置 JSON（如 feature flag 名、环境变量名） | `{"flag": "CW_USE_RUST_SQLITE"}` |
| `created_at` | 创建时间 | epoch |
| `updated_at` | 最后更新时间 | epoch |

**回滚流程**：
1. 设置 `rollback_flag = 1`（通过 CLI 或 Python API）
2. 生产入口检查 `rollback_flag`，为 1 时走 `rollback_entry`
3. `rollback_window_until` 过期后，Phase 7 的"删除 Python 生产 fallback"子任务会删除 `rollback_entry`，此时 `rollback_flag` 必须为 0

### 3.5 启动级恢复（L3）

**入口**：[rust_ext/src/bin/cw_daemon.rs](../../rust_ext/src/bin/cw_daemon.rs) `recover_all_workspaces_with_snapshot()`

**完整恢复链**（已在 R16 修复中实现）：
```
daemon 启动
  └─ recover_all_workspaces_with_snapshot()
       └─ for each workspace:
            └─ dispatch("workspace.recover")
                 ├─ 1. staging log append 重放
                 ├─ 2. merge CAS 到 CodeGraph DB
                 ├─ 3. file_generation_committed（条件 UPDATE）
                 ├─ 4. Replicator::replicate（发布 snapshot）
                 ├─ 5. parse_retry_log 重放（best-effort）
                 └─ 失败时: file_generation_uncommit 回滚
```

**关键设计**：
- 启动恢复必须复用 RPC 的生产路径（`dispatch("workspace.recover")`），经过与真实请求相同的 ACL 检查
- 使用 workspace owner 的 UID 作为内部 peer
- 恢复失败时打印 `[cw_daemon] [WARN] durable recovery deferred for ws=...`，不阻塞 daemon 启动
- `recover_all_workspaces`（不带 `_with_snapshot`）是兼容旧测试的只读扫描，**生产路径不得调用**

## 4. 任务自举运行方式

### 4.1 迁移任务树结构

迁移任务树已在 Phase 0 启动时创建（父任务 `T-1785147987425-07c09f75`）：

```
T-1785147987425-07c09f75 (Call Warden 全量 Rust 迁移自举计划)
├─ Phase 0: T-1785148066851-c984a0dc
│   ├─ 迁移 manifest 与生产调用链盘点 [review]
│   ├─ Parse/Query/Storage ABI 与错误码契约 [review]
│   ├─ Python/Rust differential harness 与基线 [review]
│   └─ 迁移质量门禁、回滚和任务自举 [in_progress] ← 本子任务
├─ Phase 1: T-1785148066852-d8ad3f5a
│   ├─ Rust SQLite 连接、schema migration 与事务边界
│   ├─ Global CAS、Local CAS 与 pending refs
│   ├─ workspace manifest、projection 与 refresh commit
│   └─ Replicator、SnapshotManager 与 backup/restore
└─ ... (Phase 2-7)
```

每个功能子任务包含 7 个 step（contract/implement/differential-test/wire-production/verify/refresh/review），共 32 个功能子任务 × 7 step = 224 个可追踪步骤。

### 4.2 自举运行流程

任务开始前和每个阶段结束时执行（[rust-full-migration-self-bootstrap-plan.md](rust-full-migration-self-bootstrap-plan.md) §9）：

```powershell
$env:PYTHONUTF8='1'
cw task status-tree <父任务 ID>
cw --refresh-all
cw task completion-review <子任务 ID>
```

**自举意义**：
- 迁移状态本身由 Call Warden 持久化（`tasks`/`task_steps`/`task_quality_findings`/`change_audit` 表）
- `cw task status-tree` 输出整个迁移进度（如当前 21/224 = 9.375%）
- `cw --refresh-all` 确保数据库与代码同步（AGENTS.md 规则 1）
- `cw task completion-review` 运行质量门禁，输出 decision ∈ {pass, warn, block}

### 4.3 任务状态推进流程

```
open
  └─ task_next_step() → in_progress
       └─ step #0 contract: in_progress → done
       └─ step #1 implement: in_progress → done
       └─ ... (step #2-#5)
       └─ step #6 review: in_progress → done
            └─ task_report_step(所有 step done) → task_status = review
                 └─ task_apply() → applied
                      └─ task_close() → closed
```

**父任务自动完成规则**：
- 所有子任务都 `closed` 时，父任务自动 `applied` → `closed`
- 子任务挂入已 `closed` 的父任务时，触发 reopen 机制（见 §3.3）

### 4.4 质量门禁与任务状态机的协作

**关键不变量**（由 [db/db_task_quality.py](../../db/db_task_quality.py) 强制）：
1. `task_report_step` 在决定 step 是否 done 前，必须调用 `task_has_blocking_findings`
2. 存在 open 的 error/block finding 时，step 阻塞，自动插入 `fix_quality_gate_failure` 步骤
3. `fix_quality_gate_failure` step 的 `target_symbol` 字段记录源 step_id
4. `skip_quality_review=True` 仅用于紧急回滚，且必须在 `change_audit.diff` 中记录原因

### 4.5 迁移状态跟踪表更新规则

[migration-manifest.md](migration-manifest.md) §7 的跟踪表由本契约强制维护：

- **contract step done** → manifest §7 对应行的 `contract` 列改为 ✅
- **implement step done** → `implement` 列改为 ✅
- **review step done（task_status = review）** → `review` 列改为 ⏸️
- **task_close** → 所有列改为 ✅

跟踪表更新由 Agent 在每个 step 完成后手动执行（不自动同步，避免循环依赖）。

## 5. 数据结构

### 5.1 rollback_config 表（本契约新增，schema v23）

```sql
CREATE TABLE IF NOT EXISTS rollback_config (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER,
    task_id TEXT NOT NULL,
    feature_name TEXT NOT NULL,
    phase INTEGER NOT NULL,
    production_entry TEXT NOT NULL,
    rollback_entry TEXT NOT NULL,
    rollback_flag INTEGER NOT NULL DEFAULT 0,
    rollback_window_until TEXT DEFAULT '',
    config_blob TEXT DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX IF NOT EXISTS idx_rollback_config_task ON rollback_config(task_id);
CREATE INDEX IF NOT EXISTS idx_rollback_config_feature ON rollback_config(feature_name);
CREATE INDEX IF NOT EXISTS idx_rollback_config_flag ON rollback_config(rollback_flag);
```

**查询接口**（Python Mixin + Rust 扩展）：
- `get_rollback_config(task_id: str) -> Optional[Dict]`：查询单个任务的回滚配置
- `list_rollback_configs(phase: int = 0, rollback_flag: int = -1) -> List[Dict]`：批量查询
- `set_rollback_flag(task_id: str, flag: int, reason: str = "") -> Dict`：设置回滚标志（写操作，走 CLI）
- `is_feature_rolled_back(feature_name: str) -> bool`：生产入口快速查询（只读，可走 MCP）

### 5.2 已有表复用

| 表 | 用途 | 关键字段 |
|---|---|---|
| `tasks` | 迁移任务树 | id, title, status, parent_task_id |
| `task_steps` | 7 步完成协议 | action ∈ {contract, implement, differential-test, wire-production, verify, refresh, review, fix_quality_gate_failure} |
| `task_quality_findings` | 门禁 finding 记录 | severity ∈ {info, warn, error, block}, source='check_gate' |
| `change_audit` | 变更审计 + 回滚记录 | hash_before/after, diff=[ROLLBACK] reason=... |
| `task_symbol_changes` | 符号级变更归属 | symbol_hash_before/after, change_type |
| `audit_chain` | 审计链签名 | operation=reopen/rollback |

## 6. 实现范围（本子任务 step #1-#6）

### 6.1 implement 范围

**新增**：
- `db/schema.py`：新增 `rollback_config` 表定义（schema v42）
- `db/db_base.py`：新增 `_migrate_v41_to_v42` migration 函数
- `db/db_rollback_config.py`：新增 `RollbackConfigMixin`（5 个方法：register/get/list/set_flag/is_feature_rolled_back）
- `db/db.py`：组合 `RollbackConfigMixin`
- `cli/main.py`：新增 `cw rollback register/show/config/set/is-rolled-back` 子命令
- `i18n/zh_CN.json` + `en_US.json`：添加 rollback 相关 i18n key

**Rust 扩展调整决策**：
- 契约原计划实现 `rust_ext/src/rollback_config.rs`，但根据 AGENTS.md 规则 8"单值查询保持 Python SQL"，
  `is_feature_rolled_back` 是单行 SELECT，Python sqlite3 比 PyO3 跨语言调用更快（固定开销占比大）
- `rollback_config` 是低频查询（生产入口检查 + CLI 操作），不需要 Rust 内存索引加速
- 差分测试改为验证 Python 端的方法行为一致性（register/get/set/list 的幂等性和状态保持）
- 若 Phase 1+ 的 daemon 需要高频查询 rollback_flag，届时再实现 Rust 扩展

**不修改**：
- 已有的 `task_rollback`/`task_reopen`/`file_generation_uncommit`/`recover_all_workspaces_with_snapshot` 实现
- 已有的 `run_task_completion_review` 5 个检查器（G1/G2/G5 规则通过 `target_file` 约束实现，不新增检查器）

### 6.2 differential-test 范围

- Python/Rust 差分对照 `rollback_config` 表的读写
- 验证 `is_feature_rolled_back` 在 Python 和 Rust 路径返回一致

### 6.3 wire-production 范围

- 接入 `cw rollback config` 作为生产入口
- 在迁移任务树本身使用 `rollback_config`（dogfooding）：为本子任务登记一条 rollback_config 记录

### 6.4 verify 范围

- 性能：`is_feature_rolled_back` 查询 P95 < 1ms（SQLite 索引查询）
- 安全：`set_rollback_flag` 必须经过写锁，不允许 MCP 直写
- 恢复：daemon 崩溃后 `rollback_flag` 状态持久化（不丢失）

## 7. 与已有基础设施的关系

| 已有基础设施 | 本契约的关系 |
|---|---|
| `run_task_completion_review` | 复用，不修改；迁移专属门禁规则通过 `target_file` 约束实现 |
| `task_rollback` | 作为 L0 回滚入口，本契约只定义调用时机 |
| `task_reopen` | 作为 L1 回滚入口，本契约只定义触发条件 |
| `file_generation_uncommit` | 作为 L2 回滚入口，已在 R16 修复中实现 |
| `recover_all_workspaces_with_snapshot` | 作为 L3 恢复入口，已在 R16 修复中实现 |
| `migration-manifest.md` §7 跟踪表 | 本契约定义跟踪表更新规则 |
| `differential-harness-contract.md` | 差分测试通过率门禁引用 harness 的 `verify_baseline` |
| `abi-error-code-contract.md` | 错误码 `RECOVERY_FAILED` 关联 L2/L3 回滚 |

## 8. 验收标准

本子任务完成 review step 的条件：

- [ ] `rollback_config` 表 schema 已添加到 `db/schema.py`（SCHEMA_VERSION bump 到 23）
- [ ] `RollbackConfigMixin` 实现并组合到 `CodeGraphDB`
- [ ] Rust 扩展 `is_feature_rolled_back` 实现并通过差分测试
- [ ] `cw rollback config/show/set` CLI 命令可用
- [ ] 本子任务自身登记了一条 `rollback_config` 记录（dogfooding）
- [ ] 性能测试：`is_feature_rolled_back` P95 < 1ms
- [ ] 恢复测试：daemon 崩溃后 `rollback_flag` 状态持久化
- [ ] `cw --refresh-all` 成功
- [ ] `cw task completion-review T-1785148066852-10a8673b` decision ∈ {pass, warn}
- [ ] [migration-manifest.md](migration-manifest.md) §7 跟踪表本行更新为 ✅

## 9. 不在本子任务范围

- 不实现 G1/G2/G4/G5 迁移专属检查器（通过 `target_file` 约束实现，不新增检查器代码）
- 不修改已有的 `task_rollback`/`task_reopen`/`file_generation_uncommit`/`recover_all_workspaces_with_snapshot` 实现
- 不删除任何 Python 生产入口（Phase 0 禁止）
- 不实现 Phase 7 的"删除 Python 生产 fallback"（本子任务只登记 `rollback_config`，删除由 Phase 7 执行）
- 不实现 rollback_flag 的生产入口检查逻辑（由各功能子任务在 wire-production step 自行实现）
