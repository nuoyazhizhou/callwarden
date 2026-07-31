//! Rust `cw task` 本地编排与审计状态机。
//!
//! 任务编排数据属于当前 UID 的本地数据库，不随代码查询路由切换到 enterprise
//! daemon。这样同一用户在 local/auto/enterprise 模式下看到的是同一份任务事实。

use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};
use std::process;
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::canonicalize::canonicalize_source;

static TASK_ID_COUNTER: AtomicU64 = AtomicU64::new(0);

#[derive(Debug, Clone)]
pub struct TaskListOptions {
    pub blocked: bool,
    pub limit: usize,
    pub status: Option<String>,
    pub flat: bool,
}

#[derive(Debug, Clone)]
pub struct TaskSummary {
    pub task_id: String,
    pub title: String,
    pub status: String,
    pub parent_id: String,
    pub blocking: bool,
}

#[derive(Debug, Clone)]
pub struct TaskStep {
    pub step_index: i64,
    pub action: String,
    pub target_file: String,
    pub target_symbol: String,
    pub status: String,
}

#[derive(Debug, Clone, Deserialize)]
pub struct TaskStepInput {
    #[serde(default)]
    pub action: String,
    #[serde(default)]
    pub target_file: String,
    #[serde(default)]
    pub target_symbol: String,
    #[serde(default)]
    pub check_items: Value,
}

#[derive(Debug, Clone)]
pub struct TaskCreateResult {
    pub task_id: String,
    pub title: String,
    pub description: String,
    pub steps: Vec<TaskStepInput>,
}

#[derive(Debug, Clone)]
pub struct ClaimedTaskStep {
    pub step_id: String,
    pub task_id: String,
    pub step_index: i64,
    pub action: String,
    pub target_file: String,
    pub target_symbol: String,
    pub check_items: Value,
    pub task_title: String,
    pub status: String,
    pub parent_task_chain: Vec<TaskChainItem>,
    pub open_quality_findings: Vec<TaskFinding>,
}

#[derive(Debug, Clone)]
pub struct TaskChainItem {
    pub task_id: String,
    pub title: String,
    pub status: String,
}

#[derive(Debug, Clone)]
pub struct TaskReportResult {
    pub task_id: String,
    pub step_id: String,
    pub success: bool,
    pub next_step: Option<PendingTaskStep>,
}

#[derive(Debug, Clone)]
pub struct PendingTaskStep {
    pub step_id: String,
    pub task_id: String,
    pub step_index: i64,
    pub action: String,
    pub target_file: String,
    pub target_symbol: String,
    pub check_items: Value,
    pub task_title: String,
}

#[derive(Debug, Clone)]
pub struct TaskReopenResult {
    pub task_id: String,
    pub status: String,
    pub previous_status: String,
    pub reopened_at: f64,
    pub reviewer: String,
    pub reason: String,
}

#[derive(Debug, Clone)]
pub struct TaskRollbackChange {
    pub original_change_id: String,
    pub file_path: String,
    pub hash_before: String,
    pub hash_after: String,
    pub restorable: bool,
}

#[derive(Debug, Clone)]
pub struct TaskRollbackResult {
    pub task_id: String,
    pub task_status: String,
    pub rolled_back_changes: Vec<TaskRollbackChange>,
}

#[derive(Debug, Clone, Serialize)]
pub struct ChangedFile {
    pub path: String,
    pub status: String,
}

#[derive(Debug, Clone)]
pub struct TaskCaptureResult {
    pub task_id: String,
    pub step_id: String,
    pub base: String,
    pub dry_run: bool,
    pub scan_id: i64,
    pub changed_files: Vec<ChangedFile>,
    pub linked_symbols: usize,
    pub quality_findings: Vec<TaskFinding>,
    pub quality_decision: String,
    pub next_action: String,
    pub auto: bool,
    pub success: bool,
    pub reason: String,
    pub error: String,
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct TaskFindingCounts {
    pub info: usize,
    pub warn: usize,
    pub error: usize,
    pub block: usize,
}

impl TaskFindingCounts {
    pub fn total(&self) -> usize {
        self.info + self.warn + self.error + self.block
    }
}

#[derive(Debug, Clone)]
pub struct TaskCompletionReviewResult {
    pub task_id: String,
    pub step_id: String,
    pub decision: String,
    pub findings: Vec<TaskFinding>,
    pub counts: TaskFindingCounts,
    pub summary: String,
}

#[derive(Debug, Clone)]
pub struct TaskFindingResolutionResult {
    pub finding_id: i64,
    pub status: String,
    pub resolution: String,
    pub resolved_at: f64,
    pub resolved_by: String,
}

#[derive(Debug, Clone)]
pub struct TaskApplyResult {
    pub task_id: String,
    pub status: String,
    pub applied_at: f64,
    pub reviewer: String,
    pub cascaded_close: Vec<String>,
}

#[derive(Debug, Clone)]
pub struct TaskCloseResult {
    pub task_id: String,
    pub status: String,
    pub closed_at: f64,
    pub reviewer: String,
}

#[derive(Debug, Clone)]
pub struct TaskSplitItem {
    pub task_id: String,
    pub title: String,
}

#[derive(Debug, Clone)]
pub struct TaskSplitResult {
    pub parent_task_id: String,
    pub subtasks: Vec<TaskSplitItem>,
}

#[derive(Debug, Clone)]
struct TaskSplitDefinition {
    title: String,
    description: String,
    steps: Vec<TaskStepInput>,
}

#[derive(Debug, Clone)]
pub struct TaskNode {
    pub task_id: String,
    pub title: String,
    pub description: String,
    pub status: String,
    pub creator: String,
    pub created_display: String,
    pub steps: Vec<TaskStep>,
    pub subtasks: Vec<TaskNode>,
}

#[derive(Debug, Clone)]
pub struct TaskFinding {
    pub id: i64,
    pub step_id: String,
    pub finding_type: String,
    pub severity: String,
    pub status: String,
    pub message: String,
    pub source: String,
}

#[derive(Debug, Clone, Default)]
pub struct TaskLinks {
    pub commits: Vec<TaskCommit>,
    pub symbol_changes: Vec<TaskSymbolChange>,
}

#[derive(Debug, Clone)]
pub struct TaskCommit {
    pub commit_hash: String,
    pub change_count: i64,
    pub subject: String,
    pub author: String,
}

#[derive(Debug, Clone)]
pub struct TaskSymbolChange {
    pub qualified_name: String,
    pub symbol_name: String,
    pub change_type: String,
    pub source_commit_hash: String,
}

#[derive(Debug, Clone)]
struct TaskRow {
    task_id: String,
    title: String,
    description: String,
    status: String,
    creator: String,
    parent_id: String,
    created_display: String,
}

pub fn create_task(
    conn: &mut Connection,
    title: &str,
    description: &str,
    steps: Vec<TaskStepInput>,
    creator: &str,
    parent_id: Option<&str>,
) -> Result<TaskCreateResult, String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start task create transaction: {error}"))?;
    let now = unix_timestamp()?;
    let task_id = generate_id("T")?;
    let parent_id = parent_id.unwrap_or("").trim();

    let (depth, sort_order) = if parent_id.is_empty() {
        (0_i64, 0_i64)
    } else {
        let parent = tx
            .query_row(
                "SELECT depth, status FROM tasks WHERE id = ?1",
                params![parent_id],
                |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
            )
            .optional()
            .map_err(|error| format!("cannot inspect parent task {parent_id}: {error}"))?
            .ok_or_else(|| format!("parent task not found: {parent_id}"))?;
        reopen_parent_chain_if_needed(&tx, parent_id, &parent.1, true, now)?;
        let sort_order = tx
            .query_row(
                "SELECT COALESCE(MAX(sort_order), -1) + 1
                 FROM tasks WHERE parent_id = ?1",
                params![parent_id],
                |row| row.get(0),
            )
            .map_err(|error| format!("cannot allocate child task order: {error}"))?;
        (parent.0 + 1, sort_order)
    };

    tx.execute(
        "INSERT INTO tasks(
             id, title, description, creator, status, created_at, updated_at,
             parent_id, depth, sort_order
         ) VALUES (?1, ?2, ?3, ?4, 'open', ?5, ?5, ?6, ?7, ?8)",
        params![
            task_id,
            title,
            description,
            creator,
            now,
            parent_id,
            depth,
            sort_order
        ],
    )
    .map_err(|error| format!("cannot insert task: {error}"))?;

    for (index, step) in steps.iter().enumerate() {
        let step_id = generate_id("S")?;
        let step_index = i64::try_from(index).map_err(|_| "task has too many steps".to_string())?;
        tx.execute(
            "INSERT INTO task_steps(
                 id, task_id, step_index, action, target_file, target_symbol,
                 check_items, status, result, created_at, completed_at
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'pending', '', ?8, NULL)",
            params![
                step_id,
                task_id,
                step_index,
                step.action,
                step.target_file,
                step.target_symbol,
                serialize_check_items(&step.check_items)?,
                now
            ],
        )
        .map_err(|error| format!("cannot insert task step {index}: {error}"))?;
    }
    tx.commit()
        .map_err(|error| format!("cannot commit task create: {error}"))?;
    Ok(TaskCreateResult {
        task_id,
        title: title.to_string(),
        description: description.to_string(),
        steps,
    })
}

pub fn claim_next_task_step(
    conn: &mut Connection,
    task_id: &str,
) -> Result<Option<ClaimedTaskStep>, String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start task claim transaction: {error}"))?;
    let root_status = tx
        .query_row(
            "SELECT status FROM tasks WHERE id = ?1",
            params![task_id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| format!("cannot inspect task {task_id}: {error}"))?
        .ok_or_else(|| format!("task not found: {task_id}"))?;
    if matches!(root_status.as_str(), "applied" | "closed" | "reverted") {
        return Err(format!(
            "task {task_id} is {root_status} and cannot claim new work"
        ));
    }

    let mut visited = HashSet::new();
    let pending = find_priority_fix_step(&tx, task_id)?.or(find_next_pending_step_tree(
        &tx,
        task_id,
        &mut visited,
    )?);
    let Some(pending) = pending else {
        tx.commit()
            .map_err(|error| format!("cannot finish empty task claim: {error}"))?;
        return Ok(None);
    };
    let open_quality_findings = query_open_quality_findings(&tx, &pending.task_id)?;
    let has_blocking_finding = open_quality_findings
        .iter()
        .any(|finding| matches!(finding.severity.as_str(), "error" | "block"));
    let is_repair_step = matches!(
        pending.action.as_str(),
        "fix_quality_gate_failure" | "fix_gate_failure" | "fix_defect"
    );
    let claimed_status = if has_blocking_finding && !is_repair_step {
        "blocked"
    } else {
        "in_progress"
    };
    let updated = tx
        .execute(
            "UPDATE task_steps SET status = ?1
             WHERE id = ?2 AND task_id = ?3 AND status = 'pending'",
            params![claimed_status, pending.step_id, pending.task_id],
        )
        .map_err(|error| format!("cannot claim task step {}: {error}", pending.step_id))?;
    if updated != 1 {
        return Err(format!(
            "task step {} was concurrently claimed",
            pending.step_id
        ));
    }

    let now = unix_timestamp()?;
    let chain_to_advance = build_parent_chain(&tx, &pending.task_id)?;
    for item in &chain_to_advance {
        tx.execute(
            "UPDATE tasks SET status = 'in_progress', updated_at = ?1
             WHERE id = ?2 AND status = 'open'",
            params![now, item.task_id],
        )
        .map_err(|error| format!("cannot advance task {}: {error}", item.task_id))?;
    }
    tx.execute(
        "UPDATE workspaces SET active_task_id = ?1 WHERE is_active = 1",
        params![task_id],
    )
    .map_err(|error| format!("cannot persist active task: {error}"))?;
    let parent_task_chain = build_parent_chain(&tx, &pending.task_id)?;

    tx.commit()
        .map_err(|error| format!("cannot commit task claim: {error}"))?;
    Ok(Some(ClaimedTaskStep {
        step_id: pending.step_id,
        task_id: pending.task_id,
        step_index: pending.step_index,
        action: pending.action,
        target_file: pending.target_file,
        target_symbol: pending.target_symbol,
        check_items: pending.check_items,
        task_title: pending.task_title,
        status: claimed_status.to_string(),
        parent_task_chain,
        open_quality_findings,
    }))
}

pub fn report_task_step(
    conn: &mut Connection,
    task_id: &str,
    step_id: &str,
    result: &str,
    success: bool,
) -> Result<TaskReportResult, String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start task report transaction: {error}"))?;
    let (actual_task_id, current_status) = tx
        .query_row(
            "SELECT task_id, status FROM task_steps WHERE id = ?1",
            params![step_id],
            |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|error| format!("cannot inspect task step {step_id}: {error}"))?
        .ok_or_else(|| format!("task step not found: {step_id}"))?;
    if !is_ancestor_or_same(&tx, task_id, &actual_task_id)? {
        return Err(format!(
            "task step {step_id} does not belong to task tree {task_id}"
        ));
    }
    if !matches!(current_status.as_str(), "in_progress" | "blocked") {
        return Err(format!(
            "task step {step_id} is {current_status}; only in_progress/blocked steps can be reported"
        ));
    }

    let now = unix_timestamp()?;
    let new_status = if success { "done" } else { "failed" };
    let updated = tx
        .execute(
            "UPDATE task_steps
             SET status = ?1, result = ?2, completed_at = ?3
             WHERE id = ?4 AND task_id = ?5 AND status = ?6",
            params![
                new_status,
                result,
                now,
                step_id,
                actual_task_id,
                current_status
            ],
        )
        .map_err(|error| format!("cannot report task step {step_id}: {error}"))?;
    if updated != 1 {
        return Err(format!("task step {step_id} changed concurrently"));
    }

    if !success {
        let next_index = tx
            .query_row(
                "SELECT COALESCE(MAX(step_index), -1) + 1
                 FROM task_steps WHERE task_id = ?1",
                params![actual_task_id],
                |row| row.get::<_, i64>(0),
            )
            .map_err(|error| format!("cannot allocate fix step index: {error}"))?;
        tx.execute(
            "INSERT INTO task_steps(
                 id, task_id, step_index, action, target_file, target_symbol,
                 check_items, status, result, created_at, completed_at
             ) VALUES (?1, ?2, ?3, 'fix_defect', '', '', '', 'pending', '', ?4, NULL)",
            params![generate_id("S")?, actual_task_id, next_index, now],
        )
        .map_err(|error| format!("cannot create fix_defect step: {error}"))?;
    }

    tx.execute(
        "UPDATE tasks SET updated_at = ?1 WHERE id IN (?2, ?3)",
        params![now, actual_task_id, task_id],
    )
    .map_err(|error| format!("cannot update task timestamps: {error}"))?;

    if success && task_can_enter_review(&tx, &actual_task_id)? {
        tx.execute(
            "UPDATE tasks SET status = 'review', updated_at = ?1
             WHERE id = ?2 AND status IN ('open', 'in_progress')",
            params![now, actual_task_id],
        )
        .map_err(|error| format!("cannot advance task to review: {error}"))?;
        update_parent_statuses(&tx, &actual_task_id, now)?;
    }

    let mut visited = HashSet::new();
    let next_step = find_next_pending_step_tree(&tx, task_id, &mut visited)?;
    tx.commit()
        .map_err(|error| format!("cannot commit task report: {error}"))?;
    Ok(TaskReportResult {
        task_id: task_id.to_string(),
        step_id: step_id.to_string(),
        success,
        next_step,
    })
}

pub fn reopen_task(
    conn: &mut Connection,
    task_id: &str,
    reviewer: &str,
    reason: &str,
) -> Result<TaskReopenResult, String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start task reopen transaction: {error}"))?;
    let previous_status = tx
        .query_row(
            "SELECT status FROM tasks WHERE id = ?1",
            params![task_id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| format!("cannot inspect task {task_id}: {error}"))?
        .ok_or_else(|| format!("task not found: {task_id}"))?;
    if !matches!(previous_status.as_str(), "review" | "applied" | "closed") {
        return Err(format!(
            "task is in status '{previous_status}', no need to reopen \
             (only review/applied/closed can be reopened)"
        ));
    }
    let now = unix_timestamp()?;
    tx.execute(
        "UPDATE tasks
         SET status = 'in_progress', applied_at = NULL, closed_at = NULL, updated_at = ?1
         WHERE id = ?2 AND status = ?3",
        params![now, task_id, previous_status],
    )
    .map_err(|error| format!("cannot reopen task {task_id}: {error}"))?;

    let parent_id = tx
        .query_row(
            "SELECT COALESCE(parent_id, '') FROM tasks WHERE id = ?1",
            params![task_id],
            |row| row.get::<_, String>(0),
        )
        .map_err(|error| format!("cannot read task parent: {error}"))?;
    if !parent_id.is_empty() {
        let parent_status = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![parent_id],
                |row| row.get::<_, String>(0),
            )
            .map_err(|error| format!("cannot inspect parent task {parent_id}: {error}"))?;
        reopen_parent_chain_if_needed(&tx, &parent_id, &parent_status, false, now)?;
    }
    tx.execute(
        "UPDATE workspaces SET active_task_id = ?1 WHERE is_active = 1",
        params![task_id],
    )
    .map_err(|error| format!("cannot persist reopened active task: {error}"))?;
    tx.commit()
        .map_err(|error| format!("cannot commit task reopen: {error}"))?;
    Ok(TaskReopenResult {
        task_id: task_id.to_string(),
        status: "in_progress".to_string(),
        previous_status,
        reopened_at: now,
        reviewer: reviewer.to_string(),
        reason: reason.to_string(),
    })
}

pub fn rollback_task(
    conn: &mut Connection,
    task_id: &str,
    change_or_step_id: &str,
    reason: &str,
) -> Result<TaskRollbackResult, String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start task rollback transaction: {error}"))?;
    let task_exists = tx
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE id = ?1)",
            params![task_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|error| format!("cannot inspect rollback task: {error}"))?;
    if task_exists == 0 {
        return Err(format!("task not found: {task_id}"));
    }

    let mut sql = String::from(
        "SELECT id, COALESCE(step_id, ''), file_path,
                COALESCE(hash_before, ''), COALESCE(hash_after, '')
         FROM change_audit WHERE task_id = ?1",
    );
    let mut values = vec![task_id.to_string()];
    if !change_or_step_id.trim().is_empty() {
        values.push(change_or_step_id.trim().to_string());
        sql.push_str(" AND (id = ?2 OR step_id = ?2)");
    }
    sql.push_str(" ORDER BY timestamp ASC, id ASC");
    let refs = values
        .iter()
        .map(|value| value as &dyn rusqlite::ToSql)
        .collect::<Vec<_>>();
    let originals = {
        let mut stmt = tx
            .prepare(&sql)
            .map_err(|error| format!("cannot prepare rollback changes: {error}"))?;
        let rows = stmt
            .query_map(refs.as_slice(), |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                ))
            })
            .map_err(|error| format!("cannot query rollback changes: {error}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read rollback changes: {error}"))?
    };
    if !change_or_step_id.trim().is_empty() && originals.is_empty() {
        return Err(format!(
            "no change_audit row for task {task_id} and scope {change_or_step_id}"
        ));
    }

    let now = unix_timestamp()?;
    let mut rolled_back_changes = Vec::with_capacity(originals.len());
    for (original_id, step_id, file_path, hash_before, hash_after) in originals {
        let rollback_id = generate_id("C")?;
        tx.execute(
            "INSERT INTO change_audit(
                 id, task_id, step_id, file_path, hash_before, hash_after,
                 diff, author, timestamp
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'agent', ?8)",
            params![
                rollback_id,
                task_id,
                step_id,
                file_path,
                hash_after,
                hash_before,
                format!(
                    "[ROLLBACK] reason={}",
                    if reason.trim().is_empty() {
                        "unspecified"
                    } else {
                        reason.trim()
                    }
                ),
                now
            ],
        )
        .map_err(|error| format!("cannot insert rollback audit row: {error}"))?;
        rolled_back_changes.push(TaskRollbackChange {
            original_change_id: original_id,
            file_path,
            restorable: !hash_before.is_empty(),
            hash_before,
            hash_after,
        });
    }

    let updated = tx
        .execute(
            "UPDATE tasks SET status = 'reverted', updated_at = ?1, closed_at = ?1
             WHERE id = ?2",
            params![now, task_id],
        )
        .map_err(|error| format!("cannot mark task reverted: {error}"))?;
    if updated != 1 {
        return Err(format!("task {task_id} changed concurrently"));
    }
    tx.execute(
        "UPDATE workspaces SET active_task_id = ''
         WHERE is_active = 1 AND active_task_id = ?1",
        params![task_id],
    )
    .map_err(|error| format!("cannot clear rolled back active task: {error}"))?;
    tx.commit()
        .map_err(|error| format!("cannot commit task rollback: {error}"))?;
    Ok(TaskRollbackResult {
        task_id: task_id.to_string(),
        task_status: "reverted".to_string(),
        rolled_back_changes,
    })
}

#[allow(clippy::too_many_arguments)]
pub fn capture_task_diff(
    conn: &mut Connection,
    task_id: &str,
    step_id: &str,
    workspace_root: &Path,
    base: &str,
    dry_run: bool,
    source_commit_hash: &str,
    skip_quality_review: bool,
) -> Result<TaskCaptureResult, String> {
    let task_id = task_id.trim();
    if task_id.is_empty() {
        return Err("task_id is required".to_string());
    }
    let (workspace_id, db_root) = active_workspace(conn)?;
    let root = if workspace_root.as_os_str().is_empty() {
        PathBuf::from(db_root)
    } else {
        workspace_root.to_path_buf()
    };
    validate_task_scope(conn, task_id, step_id)?;
    let (changed_files, status_text) = collect_workspace_changes(conn, workspace_id, &root, base)?;
    if dry_run {
        return Ok(TaskCaptureResult {
            task_id: task_id.to_string(),
            step_id: step_id.to_string(),
            base: base.to_string(),
            dry_run: true,
            scan_id: 0,
            next_action: if changed_files.is_empty() {
                "noop".to_string()
            } else {
                "apply".to_string()
            },
            changed_files,
            linked_symbols: 0,
            quality_findings: Vec::new(),
            quality_decision: String::new(),
            auto: false,
            success: true,
            reason: String::new(),
            error: String::new(),
        });
    }

    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start capture-diff transaction: {error}"))?;
    validate_task_scope_tx(&tx, task_id, step_id)?;
    let now = unix_timestamp()?;
    let git_head = git_stdout(&root, &["rev-parse", "HEAD"]).unwrap_or_default();
    let status_hash = sha256_hex(status_text.as_bytes());
    let changed_json = serde_json::to_string(&changed_files)
        .map_err(|error| format!("cannot serialize changed files: {error}"))?;
    tx.execute(
        "INSERT INTO workspace_scan_runs(
             workspace_id, purpose, task_id, step_id, baseline_type, git_head,
             git_merge_base, git_status_hash, file_count, changed_files_json,
             metadata_json, started_at, completed_at, status
         ) VALUES (?1, 'capture', ?2, ?3, 'git', ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?10, 'completed')",
        params![
            workspace_id,
            task_id,
            step_id,
            git_head,
            base,
            status_hash,
            i64::try_from(changed_files.len()).map_err(|_| "too many changed files".to_string())?,
            changed_json,
            serde_json::json!({"source_commit_hash": source_commit_hash}).to_string(),
            now
        ],
    )
    .map_err(|error| format!("cannot record capture scan: {error}"))?;
    let scan_id = tx.last_insert_rowid();

    let mut linked_symbols = 0_usize;
    for changed in &changed_files {
        let hash_before = tx
            .query_row(
                "SELECT COALESCE(current_content_hash, '') FROM file_instances
                 WHERE workspace_id = ?1 AND rel_path = ?2
                 ORDER BY id DESC LIMIT 1",
                params![workspace_id, changed.path],
                |row| row.get::<_, String>(0),
            )
            .optional()
            .map_err(|error| format!("cannot query prior file hash: {error}"))?
            .unwrap_or_default();
        let hash_after = file_content_hash(&root.join(&changed.path)).unwrap_or_default();
        let change_id = generate_id("C")?;
        tx.execute(
            "INSERT INTO change_audit(
                 id, task_id, step_id, file_path, hash_before, hash_after,
                 diff, author, timestamp
             ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, '', 'capture-diff', ?7)",
            params![
                change_id,
                task_id,
                step_id,
                changed.path,
                hash_before,
                hash_after,
                now
            ],
        )
        .map_err(|error| format!("cannot insert capture change audit: {error}"))?;

        if table_exists(&tx, "task_symbol_changes")? {
            let change_type = match changed.status.as_str() {
                "A" | "untracked" => "added",
                "D" => "deleted",
                _ => "modified",
            };
            tx.execute(
                "INSERT INTO task_symbol_changes(
                     workspace_id, task_id, step_id, change_audit_id, file_path,
                     symbol_hash_before, symbol_hash_after, change_type, source,
                     source_commit_hash, metadata, created_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8,
                           'task_capture_diff', ?9, ?10, ?11)",
                params![
                    workspace_id,
                    task_id,
                    step_id,
                    change_id,
                    changed.path,
                    hash_before,
                    hash_after,
                    change_type,
                    source_commit_hash,
                    serde_json::json!({"status": changed.status}).to_string(),
                    now
                ],
            )
            .map_err(|error| format!("cannot link capture symbol change: {error}"))?;
            linked_symbols += 1;
        }
    }

    let quality_findings = if skip_quality_review {
        Vec::new()
    } else {
        query_open_quality_findings_scoped(&tx, task_id, step_id)?
    };
    let quality_decision = if skip_quality_review {
        String::new()
    } else {
        finding_decision(&quality_findings).to_string()
    };
    let next_action = if quality_decision == "block" {
        "fix"
    } else if changed_files.is_empty() {
        "noop"
    } else {
        "review"
    };
    tx.commit()
        .map_err(|error| format!("cannot commit capture-diff: {error}"))?;
    Ok(TaskCaptureResult {
        task_id: task_id.to_string(),
        step_id: step_id.to_string(),
        base: base.to_string(),
        dry_run: false,
        scan_id,
        changed_files,
        linked_symbols,
        quality_findings,
        quality_decision,
        next_action: next_action.to_string(),
        auto: false,
        success: true,
        reason: String::new(),
        error: String::new(),
    })
}

pub fn capture_task_diff_auto(conn: &mut Connection, workspace_root: &Path) -> TaskCaptureResult {
    let empty = |reason: &str, error: String, task_id: String| TaskCaptureResult {
        task_id,
        step_id: String::new(),
        base: String::new(),
        dry_run: false,
        scan_id: 0,
        changed_files: Vec::new(),
        linked_symbols: 0,
        quality_findings: Vec::new(),
        quality_decision: String::new(),
        next_action: "noop".to_string(),
        auto: true,
        success: false,
        reason: reason.to_string(),
        error,
    };
    let task_id = match active_task_id(conn) {
        Ok(Some(task_id)) => task_id,
        Ok(None) => return empty("no_in_progress_task", String::new(), String::new()),
        Err(error) => return empty("exception", error, String::new()),
    };
    let status = match conn.query_row(
        "SELECT status FROM tasks WHERE id = ?1",
        params![task_id],
        |row| row.get::<_, String>(0),
    ) {
        Ok(status) => status,
        Err(error) => return empty("exception", error.to_string(), task_id),
    };
    if status != "in_progress" {
        return empty("task_not_in_progress", String::new(), task_id);
    }
    let root = active_workspace(conn)
        .map(|(_, root)| {
            if workspace_root.as_os_str().is_empty() {
                PathBuf::from(root)
            } else {
                workspace_root.to_path_buf()
            }
        })
        .unwrap_or_else(|_| workspace_root.to_path_buf());
    let base = git_stdout(&root, &["rev-parse", "HEAD~1"]).unwrap_or_default();
    let head = git_stdout(&root, &["rev-parse", "HEAD"]).unwrap_or_default();
    match capture_task_diff(conn, &task_id, "", &root, &base, false, &head, true) {
        Ok(mut result) => {
            result.auto = true;
            result.base = base;
            result
        }
        Err(error) => empty("exception", error, task_id),
    }
}

pub fn review_task_completion(
    conn: &Connection,
    task_id: &str,
    step_id: &str,
) -> Result<TaskCompletionReviewResult, String> {
    validate_task_scope(conn, task_id, step_id)?;
    let findings = query_open_quality_findings_scoped(conn, task_id, step_id)?;
    let mut counts = TaskFindingCounts::default();
    for finding in &findings {
        match finding.severity.as_str() {
            "info" => counts.info += 1,
            "warn" => counts.warn += 1,
            "error" => counts.error += 1,
            "block" => counts.block += 1,
            severity => {
                return Err(format!(
                    "finding {} has unsupported severity '{severity}'",
                    finding.id
                ));
            }
        }
    }
    let decision = finding_decision(&findings).to_string();
    let summary = format!(
        "review: {decision}, {} findings (info={}, warn={}, error={}, block={})",
        counts.total(),
        counts.info,
        counts.warn,
        counts.error,
        counts.block
    );
    Ok(TaskCompletionReviewResult {
        task_id: task_id.to_string(),
        step_id: step_id.to_string(),
        decision,
        findings,
        counts,
        summary,
    })
}

pub fn resolve_task_finding(
    conn: &mut Connection,
    finding_id: i64,
    resolution: &str,
    resolved_by: &str,
) -> Result<TaskFindingResolutionResult, String> {
    let resolution = resolution.trim();
    let new_status = match resolution {
        "fixed" => "resolved",
        "wontfix" | "false_positive" => "wontfix",
        _ => {
            return Err(format!(
                "unsupported finding resolution '{resolution}'; expected fixed/wontfix/false_positive"
            ));
        }
    };
    let resolved_by = resolved_by.trim();
    if resolved_by.is_empty() {
        return Err("resolved_by identity is required".to_string());
    }
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start finding resolve transaction: {error}"))?;
    let current = tx
        .query_row(
            "SELECT status FROM task_quality_findings WHERE id = ?1",
            params![finding_id],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| format!("cannot inspect task finding {finding_id}: {error}"))?
        .ok_or_else(|| format!("finding not found: {finding_id}"))?;
    if current != "open" {
        return Err(format!(
            "finding {finding_id} is already {current}; only open findings can be resolved"
        ));
    }
    let now = unix_timestamp()?;
    let updated = tx
        .execute(
            "UPDATE task_quality_findings
             SET status = ?1, resolved_at = ?2, resolved_by = ?3
             WHERE id = ?4 AND status = 'open'",
            params![new_status, now, resolved_by, finding_id],
        )
        .map_err(|error| format!("cannot resolve task finding {finding_id}: {error}"))?;
    if updated != 1 {
        return Err(format!("finding {finding_id} changed concurrently"));
    }
    tx.commit()
        .map_err(|error| format!("cannot commit finding resolution: {error}"))?;
    Ok(TaskFindingResolutionResult {
        finding_id,
        status: new_status.to_string(),
        resolution: resolution.to_string(),
        resolved_at: now,
        resolved_by: resolved_by.to_string(),
    })
}

pub fn apply_task(
    conn: &mut Connection,
    task_id: &str,
    reviewer: &str,
) -> Result<TaskApplyResult, String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start task apply transaction: {error}"))?;
    let (status, parent_id, creator) = task_identity(&tx, task_id)?;
    validate_independent_reviewer(task_id, &creator, reviewer)?;
    reject_blocking_findings(&tx, task_id)?;
    let subtask_count = tx
        .query_row(
            "SELECT COUNT(*) FROM tasks WHERE parent_id = ?1",
            params![task_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|error| format!("cannot count task children: {error}"))?;
    if subtask_count > 0 {
        return Err(format!(
            "parent task cannot be applied manually; {subtask_count} subtasks must cascade it"
        ));
    }
    let now = unix_timestamp()?;
    if status != "review" {
        let step_count = direct_step_count(&tx, task_id)?;
        if matches!(status.as_str(), "open" | "in_progress") && step_count == 0 {
            let updated = tx
                .execute(
                    "UPDATE tasks SET status = 'review', updated_at = ?1
                     WHERE id = ?2 AND status = ?3",
                    params![now, task_id, status],
                )
                .map_err(|error| format!("cannot advance empty task to review: {error}"))?;
            if updated != 1 {
                return Err(format!("task {task_id} changed concurrently"));
            }
        } else {
            return Err(format!(
                "cannot apply task in status '{status}', only 'review' can be applied"
            ));
        }
    }
    let updated = tx
        .execute(
            "UPDATE tasks SET status = 'applied', applied_at = ?1, updated_at = ?1
             WHERE id = ?2 AND status = 'review'",
            params![now, task_id],
        )
        .map_err(|error| format!("cannot apply task {task_id}: {error}"))?;
    if updated != 1 {
        return Err(format!("task {task_id} changed concurrently"));
    }

    let mut cascaded_close = Vec::new();
    if !parent_id.is_empty() {
        update_parent_statuses(&tx, task_id, now)?;
        cascade_close_if_ready(&tx, &parent_id, reviewer, now, &mut cascaded_close)?;
    }
    tx.commit()
        .map_err(|error| format!("cannot commit task apply: {error}"))?;
    Ok(TaskApplyResult {
        task_id: task_id.to_string(),
        status: "applied".to_string(),
        applied_at: now,
        reviewer: reviewer.trim().to_string(),
        cascaded_close,
    })
}

pub fn close_task(
    conn: &mut Connection,
    task_id: &str,
    reviewer: &str,
) -> Result<TaskCloseResult, String> {
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start task close transaction: {error}"))?;
    let (status, _, creator) = task_identity(&tx, task_id)?;
    validate_independent_reviewer(task_id, &creator, reviewer)?;
    reject_blocking_findings(&tx, task_id)?;
    let subtask_count = tx
        .query_row(
            "SELECT COUNT(*) FROM tasks WHERE parent_id = ?1",
            params![task_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|error| format!("cannot count task children: {error}"))?;
    if subtask_count > 0 {
        return Err(format!(
            "parent task cannot be closed manually; {subtask_count} subtasks must cascade it"
        ));
    }
    let now = unix_timestamp()?;
    if status != "applied" {
        let step_count = direct_step_count(&tx, task_id)?;
        if matches!(status.as_str(), "open" | "in_progress" | "review") && step_count == 0 {
            let updated = tx
                .execute(
                    "UPDATE tasks SET status = 'applied', applied_at = ?1, updated_at = ?1
                     WHERE id = ?2 AND status = ?3",
                    params![now, task_id, status],
                )
                .map_err(|error| format!("cannot advance empty task to applied: {error}"))?;
            if updated != 1 {
                return Err(format!("task {task_id} changed concurrently"));
            }
        } else {
            return Err(format!(
                "cannot close task in status '{status}', only 'applied' can be closed"
            ));
        }
    }
    let updated = tx
        .execute(
            "UPDATE tasks SET status = 'closed', closed_at = ?1, updated_at = ?1
             WHERE id = ?2 AND status = 'applied'",
            params![now, task_id],
        )
        .map_err(|error| format!("cannot close task {task_id}: {error}"))?;
    if updated != 1 {
        return Err(format!("task {task_id} changed concurrently"));
    }
    tx.execute(
        "UPDATE workspaces SET active_task_id = ''
         WHERE is_active = 1 AND active_task_id = ?1",
        params![task_id],
    )
    .map_err(|error| format!("cannot clear closed active task: {error}"))?;
    tx.commit()
        .map_err(|error| format!("cannot commit task close: {error}"))?;
    Ok(TaskCloseResult {
        task_id: task_id.to_string(),
        status: "closed".to_string(),
        closed_at: now,
        reviewer: reviewer.trim().to_string(),
    })
}

pub fn split_task_from_plan(
    conn: &mut Connection,
    task_id: &str,
    plan_markdown: &str,
) -> Result<TaskSplitResult, String> {
    let definitions = parse_split_plan(plan_markdown)?;
    if definitions.is_empty() {
        return Err("no subtasks found in plan".to_string());
    }
    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(|error| format!("cannot start task split transaction: {error}"))?;
    let (parent_depth, parent_status) = tx
        .query_row(
            "SELECT depth, status FROM tasks WHERE id = ?1",
            params![task_id],
            |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
        )
        .optional()
        .map_err(|error| format!("cannot inspect split parent {task_id}: {error}"))?
        .ok_or_else(|| format!("task not found: {task_id}"))?;
    let now = unix_timestamp()?;
    reopen_parent_chain_if_needed(&tx, task_id, &parent_status, true, now)?;
    let first_sort_order = tx
        .query_row(
            "SELECT COALESCE(MAX(sort_order), -1) + 1 FROM tasks WHERE parent_id = ?1",
            params![task_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|error| format!("cannot allocate split child order: {error}"))?;

    let mut subtasks = Vec::with_capacity(definitions.len());
    for (definition_index, definition) in definitions.into_iter().enumerate() {
        let child_id = generate_id("T")?;
        let sort_order = first_sort_order
            + i64::try_from(definition_index).map_err(|_| "too many split subtasks".to_string())?;
        tx.execute(
            "INSERT INTO tasks(
                 id, title, description, creator, status, created_at, updated_at,
                 parent_id, depth, sort_order
             ) VALUES (?1, ?2, ?3, 'agent', 'open', ?4, ?4, ?5, ?6, ?7)",
            params![
                child_id,
                definition.title,
                definition.description,
                now,
                task_id,
                parent_depth + 1,
                sort_order
            ],
        )
        .map_err(|error| format!("cannot insert split subtask: {error}"))?;
        for (step_index, step) in definition.steps.iter().enumerate() {
            tx.execute(
                "INSERT INTO task_steps(
                     id, task_id, step_index, action, target_file, target_symbol,
                     check_items, status, result, created_at, completed_at
                 ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'pending', '', ?8, NULL)",
                params![
                    generate_id("S")?,
                    child_id,
                    i64::try_from(step_index)
                        .map_err(|_| "split subtask has too many steps".to_string())?,
                    step.action,
                    step.target_file,
                    step.target_symbol,
                    serialize_check_items(&step.check_items)?,
                    now
                ],
            )
            .map_err(|error| format!("cannot insert split subtask step: {error}"))?;
        }
        subtasks.push(TaskSplitItem {
            task_id: child_id,
            title: definition.title,
        });
    }
    tx.commit()
        .map_err(|error| format!("cannot commit task split: {error}"))?;
    Ok(TaskSplitResult {
        parent_task_id: task_id.to_string(),
        subtasks,
    })
}

pub fn query_task_list(
    conn: &Connection,
    status: Option<&str>,
    limit: usize,
) -> Result<Vec<TaskSummary>, String> {
    let limit = i64::try_from(limit).map_err(|_| "task limit is too large".to_string())?;
    let order = "ORDER BY CASE WHEN t.parent_id IS NULL OR t.parent_id = '' THEN 0 ELSE 1 END,
                 t.parent_id ASC, t.sort_order ASC, t.created_at DESC";
    let base = "SELECT t.id, t.title, t.status, COALESCE(t.parent_id, ''),
                       EXISTS(
                           SELECT 1 FROM task_quality_findings q
                           WHERE q.task_id = t.id
                             AND q.status = 'open'
                             AND q.severity IN ('error', 'block')
                       )
                FROM tasks t";
    let sql = if status.is_some() {
        format!("{base} WHERE t.status = ?1 {order} LIMIT ?2")
    } else {
        format!("{base} {order} LIMIT ?1")
    };
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare task list query: {error}"))?;
    let map_row = |row: &rusqlite::Row<'_>| {
        Ok(TaskSummary {
            task_id: row.get(0)?,
            title: row.get(1)?,
            status: row.get(2)?,
            parent_id: row.get(3)?,
            blocking: row.get::<_, i64>(4)? != 0,
        })
    };
    let rows = match status {
        Some(status) => stmt.query_map(params![status, limit], map_row),
        None => stmt.query_map(params![limit], map_row),
    }
    .map_err(|error| format!("cannot query task list: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read task list: {error}"))
}

pub fn query_task_findings(
    conn: &Connection,
    task_id: &str,
    status: &str,
    severity: &str,
) -> Result<Vec<TaskFinding>, String> {
    let mut sql = String::from(
        "SELECT id, COALESCE(step_id, ''), finding_type, severity, status,
                message, COALESCE(source, '')
         FROM task_quality_findings WHERE task_id = ?1",
    );
    let mut values = vec![task_id.to_string()];
    if !status.is_empty() && status != "all" {
        values.push(status.to_string());
        sql.push_str(&format!(" AND status = ?{}", values.len()));
    }
    if !severity.is_empty() {
        values.push(severity.to_string());
        sql.push_str(&format!(" AND severity = ?{}", values.len()));
    }
    sql.push_str(" ORDER BY created_at ASC");

    let refs = values
        .iter()
        .map(|value| value as &dyn rusqlite::ToSql)
        .collect::<Vec<_>>();
    let mut stmt = conn
        .prepare(&sql)
        .map_err(|error| format!("cannot prepare task findings query: {error}"))?;
    let rows = stmt
        .query_map(refs.as_slice(), |row| {
            Ok(TaskFinding {
                id: row.get(0)?,
                step_id: row.get(1)?,
                finding_type: row.get(2)?,
                severity: row.get(3)?,
                status: row.get(4)?,
                message: row.get(5)?,
                source: row.get(6)?,
            })
        })
        .map_err(|error| format!("cannot query task findings: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read task findings: {error}"))
}

pub fn query_task_detail(
    conn: &Connection,
    task_id: &str,
    include_tree: bool,
) -> Result<Option<TaskNode>, String> {
    if !include_tree {
        let Some(row) = query_task_row(conn, task_id)? else {
            return Ok(None);
        };
        return Ok(Some(TaskNode {
            steps: query_task_steps(conn, task_id)?,
            task_id: row.task_id,
            title: row.title,
            description: row.description,
            status: row.status,
            creator: row.creator,
            created_display: row.created_display,
            subtasks: Vec::new(),
        }));
    }

    let mut stmt = conn
        .prepare(
            "WITH RECURSIVE subtree(id) AS (
                 SELECT id FROM tasks WHERE id = ?1
                 UNION
                 SELECT t.id FROM tasks t JOIN subtree s ON t.parent_id = s.id
             )
             SELECT t.id, t.title, COALESCE(t.description, ''), t.status,
                    COALESCE(t.creator, ''), COALESCE(t.parent_id, ''),
                    COALESCE(strftime('%Y-%m-%d %H:%M:%S', t.created_at,
                                      'unixepoch', 'localtime'), '?')
             FROM tasks t JOIN subtree s ON s.id = t.id
             ORDER BY t.depth ASC, t.parent_id ASC, t.sort_order ASC",
        )
        .map_err(|error| format!("cannot prepare task tree query: {error}"))?;
    let rows = stmt
        .query_map(params![task_id], row_to_task)
        .map_err(|error| format!("cannot query task tree: {error}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read task tree: {error}"))?;
    if rows.is_empty() {
        return Ok(None);
    }

    let steps = query_steps_for_tree(conn, task_id)?;
    let mut children = HashMap::<String, Vec<String>>::new();
    let mut by_id = HashMap::new();
    for row in rows {
        if row.task_id != task_id {
            children
                .entry(row.parent_id.clone())
                .or_default()
                .push(row.task_id.clone());
        }
        by_id.insert(row.task_id.clone(), row);
    }

    build_task_node(task_id, &by_id, &children, &steps, &mut HashSet::new()).map(Some)
}

pub fn query_task_links(conn: &Connection, task_id: &str) -> Result<TaskLinks, String> {
    if !table_exists(conn, "task_symbol_changes")? {
        return Ok(TaskLinks::default());
    }

    let symbol_changes = {
        let mut stmt = conn
            .prepare(
                "SELECT COALESCE(qualified_name, ''), COALESCE(symbol_name, ''),
                        COALESCE(change_type, ''), COALESCE(source_commit_hash, '')
                 FROM task_symbol_changes
                 WHERE task_id = ?1
                 ORDER BY created_at DESC, id DESC
                 LIMIT 20",
            )
            .map_err(|error| format!("cannot prepare task symbol links query: {error}"))?;
        let rows = stmt
            .query_map(params![task_id], |row| {
                Ok(TaskSymbolChange {
                    qualified_name: row.get(0)?,
                    symbol_name: row.get(1)?,
                    change_type: row.get(2)?,
                    source_commit_hash: row.get(3)?,
                })
            })
            .map_err(|error| format!("cannot query task symbol links: {error}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read task symbol links: {error}"))?;
        rows
    };

    let commits = if table_exists(conn, "git_commits")? {
        let mut stmt = conn
            .prepare(
                "SELECT tsc.source_commit_hash, COUNT(*),
                        COALESCE(gc.message, ''), COALESCE(gc.author, '')
                 FROM task_symbol_changes tsc
                 LEFT JOIN git_commits gc
                   ON tsc.source_commit_hash = gc.commit_hash
                 WHERE tsc.task_id = ?1 AND tsc.source_commit_hash != ''
                 GROUP BY tsc.source_commit_hash
                 ORDER BY MAX(tsc.created_at) DESC",
            )
            .map_err(|error| format!("cannot prepare task commit links query: {error}"))?;
        let rows = stmt
            .query_map(params![task_id], |row| {
                let message: String = row.get(2)?;
                Ok(TaskCommit {
                    commit_hash: row.get(0)?,
                    change_count: row.get(1)?,
                    subject: message.lines().next().unwrap_or("").to_string(),
                    author: row.get(3)?,
                })
            })
            .map_err(|error| format!("cannot query task commit links: {error}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read task commit links: {error}"))?;
        rows
    } else {
        Vec::new()
    };

    Ok(TaskLinks {
        commits,
        symbol_changes,
    })
}

pub fn format_task_create(result: &TaskCreateResult, zh_cn: bool) -> String {
    let mut lines = vec![
        if zh_cn {
            "=== 任务创建成功 ==="
        } else {
            "=== Task Created ==="
        }
        .to_string(),
        format!(
            "  {}: {}",
            if zh_cn { "任务 ID" } else { "Task ID" },
            result.task_id
        ),
        format!(
            "  {}: {}",
            if zh_cn { "标题" } else { "Title" },
            result.title
        ),
    ];
    if !result.description.is_empty() {
        lines.push(format!(
            "  {}: {}",
            if zh_cn { "描述" } else { "Description" },
            result.description
        ));
    }
    lines.push(format!(
        "  {}: {}",
        if zh_cn { "步骤数" } else { "Steps" },
        result.steps.len()
    ));
    if !result.steps.is_empty() {
        lines.push(String::new());
        lines.push(if zh_cn {
            "  [步骤列表]:".to_string()
        } else {
            "  [Step List]:".to_string()
        });
        for (index, step) in result.steps.iter().enumerate() {
            let target = if step.target_file.is_empty() {
                &step.target_symbol
            } else {
                &step.target_file
            };
            lines.push(format!("    {}. [{}] {}", index + 1, step.action, target));
        }
    }
    lines.push(String::new());
    lines.join("\n")
}

pub fn format_task_claim(
    requested_task_id: &str,
    step: Option<&ClaimedTaskStep>,
    zh_cn: bool,
) -> String {
    let mut lines = vec![
        if zh_cn {
            "=== 领取下一步骤 ==="
        } else {
            "=== Claim Next Step ==="
        }
        .to_string(),
        format!(
            "  {}: {}",
            if zh_cn { "任务 ID" } else { "Task ID" },
            requested_task_id
        ),
    ];
    let Some(step) = step else {
        lines.push(
            if zh_cn {
                "  (没有待执行的步骤，任务可能已完成)"
            } else {
                "  (no pending steps, task may be completed)"
            }
            .to_string(),
        );
        return lines.join("\n");
    };
    lines.push(format!(
        "  {}: {}",
        if zh_cn { "步骤 ID" } else { "Step ID" },
        step.step_id
    ));
    lines.push(format!(
        "  {}: {}",
        if zh_cn { "步骤序号" } else { "Step index" },
        step.step_index
    ));
    lines.push(format!(
        "  {}: {}",
        if zh_cn { "操作" } else { "Action" },
        step.action
    ));
    if !step.target_file.is_empty() {
        lines.push(format!(
            "  {}: {}",
            if zh_cn { "目标文件" } else { "Target file" },
            step.target_file
        ));
    }
    if !step.target_symbol.is_empty() {
        lines.push(format!(
            "  {}: {}",
            if zh_cn {
                "目标符号"
            } else {
                "Target symbol"
            },
            step.target_symbol
        ));
    }
    lines.push(format!(
        "  {}: {}",
        if zh_cn { "状态" } else { "Status" },
        step.status
    ));
    lines.push(String::new());
    format_check_items(&step.check_items, zh_cn, &mut lines);
    format_structured_instruction(&step.action, zh_cn, &mut lines);
    lines.join("\n")
}

pub fn format_task_report(result: &TaskReportResult, description: &str, zh_cn: bool) -> String {
    let mut lines = vec![
        if zh_cn {
            "=== 步骤回报完成 ==="
        } else {
            "=== Step Report Done ==="
        }
        .to_string(),
        format!(
            "  {}: {}",
            if zh_cn { "任务 ID" } else { "Task ID" },
            result.task_id
        ),
        format!(
            "  {}: {}",
            if zh_cn { "步骤 ID" } else { "Step ID" },
            result.step_id
        ),
        format!(
            "  {}: {}",
            if zh_cn { "结果" } else { "Result" },
            if result.success {
                if zh_cn {
                    "成功"
                } else {
                    "success"
                }
            } else if zh_cn {
                "失败"
            } else {
                "failure"
            }
        ),
    ];
    if !description.is_empty() {
        lines.push(format!(
            "  {}: {}",
            if zh_cn { "结果描述" } else { "Result desc" },
            description
        ));
    }
    lines.push(String::new());
    if let Some(next) = &result.next_step {
        lines.push(if zh_cn {
            "  [下一步骤已就绪]".to_string()
        } else {
            "  [Next Step Ready]".to_string()
        });
        lines.push(format!(
            "    {}: {}",
            if zh_cn { "步骤 ID" } else { "Step ID" },
            next.step_id
        ));
        lines.push(format!(
            "    {}: {}",
            if zh_cn { "操作" } else { "Action" },
            next.action
        ));
        if !next.target_file.is_empty() {
            lines.push(format!(
                "    {}: {}",
                if zh_cn { "目标文件" } else { "Target file" },
                next.target_file
            ));
        }
    } else {
        lines.push(
            if zh_cn {
                "  (没有更多待执行步骤，任务进入 review 状态)"
            } else {
                "  (no more pending steps, task moved to review)"
            }
            .to_string(),
        );
    }
    lines.push(String::new());
    lines.join("\n")
}

pub fn format_task_reopen(result: &TaskReopenResult, zh_cn: bool) -> String {
    let mut lines = vec![
        if zh_cn {
            format!(
                "✓ 任务已重新打开: {}（原状态: {}）",
                result.task_id, result.previous_status
            )
        } else {
            format!(
                "✓ Task reopened: {} (previous: {})",
                result.task_id, result.previous_status
            )
        },
        format!(
            "  {}: {}",
            if zh_cn { "状态" } else { "Status" },
            result.status
        ),
        format!(
            "  {}: {}",
            if zh_cn {
                "重新打开时间"
            } else {
                "Reopened at"
            },
            result.reopened_at
        ),
    ];
    if !result.reason.is_empty() {
        lines.push(format!(
            "  {}: {}",
            if zh_cn { "原因" } else { "Reason" },
            result.reason
        ));
    }
    lines.push(String::new());
    lines.join("\n")
}

pub fn format_task_rollback(result: &TaskRollbackResult, zh_cn: bool) -> String {
    let mut lines = vec![
        if zh_cn {
            "=== 任务回滚 ===".to_string()
        } else {
            "=== Task Rollback ===".to_string()
        },
        format!(
            "  {}: {}",
            if zh_cn { "任务 ID" } else { "Task ID" },
            result.task_id
        ),
        format!(
            "  {}: {}",
            if zh_cn { "任务状态" } else { "Task status" },
            result.task_status
        ),
        format!(
            "  {}: {}",
            if zh_cn {
                "回滚变更数"
            } else {
                "Rolled back changes"
            },
            result.rolled_back_changes.len()
        ),
        String::new(),
    ];
    if !result.rolled_back_changes.is_empty() {
        lines.push(if zh_cn {
            "  【回滚变更详情】:".to_string()
        } else {
            "  [Rollback Details]:".to_string()
        });
        for (index, change) in result.rolled_back_changes.iter().enumerate() {
            lines.push(format!(
                "    {}. {} {}",
                index + 1,
                if change.restorable { "[✓]" } else { "[✗]" },
                change.file_path
            ));
            if !change.hash_before.is_empty() {
                let prefix = change.hash_before.chars().take(12).collect::<String>();
                lines.push(format!(
                    "        {}: {}...",
                    if zh_cn {
                        "原始 hash"
                    } else {
                        "Original hash"
                    },
                    prefix
                ));
            }
        }
        lines.push(String::new());
    }
    lines.push(if zh_cn {
        "  注意: 仅记录回滚意图，未操作文件系统。调用方应根据 rolled_back_changes 中的 hash_before 自行恢复文件内容。".to_string()
    } else {
        "  Note: Only rollback intent was recorded; no filesystem changes were made. Callers should restore file content using hash_before from rolled_back_changes.".to_string()
    });
    lines.push(String::new());
    lines.join("\n")
}

pub fn format_task_capture(result: &TaskCaptureResult, zh_cn: bool) -> String {
    let mut lines = vec!["=== Capture Diff ===".to_string()];
    if result.auto {
        lines.push(if zh_cn {
            "[--auto 模式] 自动检测 in_progress 任务，HEAD~1 作为 base，自动 apply（fail-soft）"
                .to_string()
        } else {
            "[--auto mode] Auto-detect in_progress task, use HEAD~1 as base, auto apply (fail-soft)"
                .to_string()
        });
        lines.push(String::new());
    }
    if !result.success {
        let message = match result.reason.as_str() {
            "no_in_progress_task" => {
                if zh_cn {
                    "  ⚠ 没有 in_progress 状态的任务，capture-diff 闭环未运行。".to_string()
                } else {
                    "  ⚠ No in_progress task found; capture-diff loop did not run.".to_string()
                }
            }
            "task_not_in_progress" => format!(
                "  ⚠ Active task {} is not in_progress; auto-capture skipped.",
                result.task_id
            ),
            _ => format!(
                "  ✗ {} (reason={}): {}",
                if zh_cn {
                    "自动捕获失败"
                } else {
                    "Auto-capture failed"
                },
                result.reason,
                result.error
            ),
        };
        lines.push(message);
        lines.push(String::new());
        return lines.join("\n");
    }
    lines.push(format!(
        "  {}: {}",
        if zh_cn { "任务 ID" } else { "Task ID" },
        result.task_id
    ));
    if !result.step_id.is_empty() {
        lines.push(format!(
            "  {}: {}",
            if zh_cn { "关联步骤" } else { "Step" },
            result.step_id
        ));
    }
    if !result.base.is_empty() {
        lines.push(format!(
            "  {}: {}",
            if zh_cn {
                "基线 commit"
            } else {
                "Base commit"
            },
            result.base
        ));
    }
    lines.push(format!(
        "  {}: {}",
        if zh_cn { "模式" } else { "Mode" },
        if result.dry_run {
            if zh_cn {
                "dry-run（只返回计划，不写库）"
            } else {
                "dry-run (return plan only, no DB writes)"
            }
        } else if zh_cn {
            "apply（写入审计并触发质量审查）"
        } else {
            "apply (write audit records and trigger quality review)"
        }
    ));
    lines.push(String::new());
    lines.push(format!(
        "  {}: {}",
        if zh_cn {
            "变更文件数"
        } else {
            "Changed files"
        },
        result.changed_files.len()
    ));
    if !result.changed_files.is_empty() {
        lines.push(String::new());
        for changed in &result.changed_files {
            lines.push(format!("    [{}] {}", changed.status, changed.path));
        }
        lines.push(String::new());
    }
    if result.dry_run {
        lines.push(format!(
            "[dry-run] next_action = {}{}",
            result.next_action,
            if zh_cn {
                "（apply 模式才会真正写库）"
            } else {
                " (apply mode writes to DB)"
            }
        ));
        lines.push(String::new());
        return lines.join("\n");
    }
    lines.push(format!(
        "  {}: {}",
        if zh_cn {
            "扫描基线 ID"
        } else {
            "Scan run ID"
        },
        result.scan_id
    ));
    lines.push(format!(
        "  {}: {}",
        if zh_cn {
            "关联符号变更记录数"
        } else {
            "Linked symbol change records"
        },
        result.linked_symbols
    ));
    if !result.quality_decision.is_empty() {
        lines.push(format!(
            "  {}: {} (findings: {})",
            if zh_cn {
                "质量审查决策"
            } else {
                "Quality decision"
            },
            result.quality_decision,
            result.quality_findings.len()
        ));
        for finding in &result.quality_findings {
            lines.push(format!(
                "    [{}] {}: {}",
                finding.severity, finding.finding_type, finding.message
            ));
        }
    }
    lines.push(String::new());
    lines.push(format!(
        "▶ {}: {}",
        if zh_cn { "下一步" } else { "Next" },
        result.next_action
    ));
    lines.push(String::new());
    lines.join("\n")
}

pub fn format_task_completion_review(result: &TaskCompletionReviewResult, zh_cn: bool) -> String {
    let mut lines = vec![format!(
        "{}: {}",
        if zh_cn {
            "审查结论"
        } else {
            "Review decision"
        },
        result.decision
    )];
    lines.push(format!(
        "  {}: {}",
        if zh_cn { "任务 ID" } else { "Task ID" },
        result.task_id
    ));
    if !result.step_id.is_empty() {
        lines.push(format!(
            "  {}: {}",
            if zh_cn { "步骤 ID" } else { "Step ID" },
            result.step_id
        ));
    }
    lines.push(format!(
        "  {}: {}",
        if zh_cn { "摘要" } else { "Summary" },
        result.summary
    ));
    lines.push(format!(
        "  Findings: total {} | info {} | warn {} | error {} | block {}",
        0, result.counts.info, result.counts.warn, result.counts.error, result.counts.block
    ));
    if !result.findings.is_empty() {
        lines.push(String::new());
        lines.push(if zh_cn {
            format!("  【发现列表】（{} 条）:", result.findings.len())
        } else {
            format!("  [Findings] ({} items):", result.findings.len())
        });
        for (index, finding) in result.findings.iter().enumerate() {
            lines.push(format!(
                "    {}. [{}] {}",
                index + 1,
                finding.severity,
                finding.message
            ));
        }
    }
    lines.push(String::new());
    lines.join("\n")
}

pub fn format_task_finding_resolution(result: &TaskFindingResolutionResult, zh_cn: bool) -> String {
    vec![
        if zh_cn {
            "=== 解决质量发现 ===".to_string()
        } else {
            "=== Resolve Finding ===".to_string()
        },
        if zh_cn {
            format!(
                "  ✓ 发现 #{} 已解决 (status={})",
                result.finding_id, result.status
            )
        } else {
            format!(
                "  ✓ Finding #{} resolved (status={})",
                result.finding_id, result.status
            )
        },
        format!(
            "  {}: {}",
            if zh_cn { "解决方式" } else { "Resolution" },
            result.resolution
        ),
        String::new(),
    ]
    .join("\n")
}

pub fn format_task_apply(result: &TaskApplyResult, zh_cn: bool) -> String {
    vec![
        if zh_cn {
            format!("✓ 任务已审核通过: {}", result.task_id)
        } else {
            format!("✓ Task applied: {}", result.task_id)
        },
        format!(
            "  {}: {}",
            if zh_cn { "状态" } else { "Status" },
            result.status
        ),
        format!(
            "  {}: {}",
            if zh_cn { "审核时间" } else { "Applied at" },
            result.applied_at
        ),
        String::new(),
    ]
    .join("\n")
}

pub fn format_task_close(result: &TaskCloseResult, zh_cn: bool) -> String {
    vec![
        if zh_cn {
            format!("✓ 任务已关闭: {}", result.task_id)
        } else {
            format!("✓ Task closed: {}", result.task_id)
        },
        format!(
            "  {}: {}",
            if zh_cn { "状态" } else { "Status" },
            result.status
        ),
        format!(
            "  {}: {}",
            if zh_cn { "关闭时间" } else { "Closed at" },
            result.closed_at
        ),
        String::new(),
    ]
    .join("\n")
}

pub fn format_task_split(result: &TaskSplitResult, zh_cn: bool) -> String {
    let mut lines = vec![if zh_cn {
        format!(
            "✓ 任务 {} 已拆分为 {} 个子任务",
            result.parent_task_id,
            result.subtasks.len()
        )
    } else {
        format!(
            "✓ Task {} split into {} subtasks",
            result.parent_task_id,
            result.subtasks.len()
        )
    }];
    for (index, subtask) in result.subtasks.iter().enumerate() {
        lines.push(format!(
            "  {}. {} - {}",
            index + 1,
            subtask.task_id,
            subtask.title
        ));
    }
    lines.push(String::new());
    lines.join("\n")
}

fn format_check_items(value: &Value, zh_cn: bool, lines: &mut Vec<String>) {
    let is_empty = matches!(value, Value::Null)
        || matches!(value, Value::String(text) if text.is_empty())
        || matches!(value, Value::Array(items) if items.is_empty());
    if is_empty {
        return;
    }
    lines.push(if zh_cn {
        "  [检查项]:".to_string()
    } else {
        "  [Check Items]:".to_string()
    });
    match value {
        Value::Array(items) => {
            for item in items {
                let text = item
                    .as_str()
                    .map(str::to_string)
                    .unwrap_or_else(|| item.to_string());
                lines.push(format!("    - {text}"));
            }
        }
        Value::String(text) => lines.push(format!("    {text}")),
        other => lines.push(format!("    {other}")),
    }
    lines.push(String::new());
}

fn format_structured_instruction(action: &str, zh_cn: bool, lines: &mut Vec<String>) {
    let action = action.to_ascii_lowercase();
    // Python i18n 当前把 JSON 数组降级成其 repr 字符串，CLI 随后逐字符输出。
    // E2 保留这一既有终端契约，待独立 i18n 迁移切片统一修复两端。
    let (constraints_repr, checks): (&str, &[&str]) = match action.as_str() {
        "annotate_function" | "annotate" | "add_comment" | "comment" => (
            if zh_cn {
                "['只添加注释，不修改函数逻辑', '注释语言: 中文', '注释格式遵循目标语言规范（Rust: /// 或 /** */，Python: # 或 docstring）']"
            } else {
                "['Only add comments; do not change function logic', 'Comment language: Chinese', \"Use the target language's comment style (Rust: /// or /** */, Python: # or docstring)\"]"
            },
            &["syntax", "semgrep_quick"],
        ),
        "refactor" | "refactor_function" => (
            if zh_cn {
                "['保持函数的外部行为不变（签名、返回值、副作用）', '修改后必须通过语法检查', '如涉及公共 API 变更须同步更新调用方']"
            } else {
                "['Keep external behavior unchanged (signature, return value, side effects)', 'Run syntax checks after changes', 'Update callers when public API changes are involved']"
            },
            &["syntax", "semgrep"],
        ),
        "fix" | "fix_defect" | "fix_gate_failure" => (
            if zh_cn {
                "['只修复报告的问题，不做额外修改', '修复后必须通过之前的检查门禁']"
            } else {
                "['Fix only the reported issue; avoid unrelated changes', 'The previous check gate must pass after the fix']"
            },
            &["syntax", "semgrep"],
        ),
        "edit" | "propose_edit" | "write" => (
            if zh_cn {
                "['使用 propose_edit 工具执行写入，禁止直接操作文件系统', '写入前先 dry_run 确认 diff', '提供 expected_hash 防止并发冲突']"
            } else {
                "['Use propose_edit for writes; do not write directly to the filesystem', 'Run dry_run first to inspect the diff', 'Provide expected_hash to prevent concurrent overwrite']"
            },
            &["syntax"],
        ),
        _ => (
            if zh_cn {
                "['按步骤 check_items 描述执行']"
            } else {
                "['Follow the step check_items']"
            },
            &["syntax"],
        ),
    };
    lines.push(if zh_cn {
        "  📐 结构化指令:".to_string()
    } else {
        "  📐 Structured Instruction:".to_string()
    });
    lines.push(if zh_cn {
        "    约束:".to_string()
    } else {
        "    Constraints:".to_string()
    });
    for constraint in constraints_repr.chars() {
        lines.push(format!("      • {constraint}"));
    }
    lines.push(format!(
        "    {}: {}",
        if zh_cn {
            "完成后检查"
        } else {
            "Post-checks"
        },
        checks.join(", ")
    ));
    lines.push(String::new());
}

pub fn format_task_list(
    tasks: &[TaskSummary],
    options: &TaskListOptions,
    zh_cn: bool,
) -> Result<String, String> {
    let mut lines = vec![if zh_cn {
        "=== 任务列表 ===".to_string()
    } else {
        "=== Task List ===".to_string()
    }];
    if options.blocked {
        lines.push(if zh_cn {
            "  (仅显示有阻塞发现的任务)".to_string()
        } else {
            "  (only showing tasks with blocking findings)".to_string()
        });
    }
    if let Some(status) = options.status.as_deref() {
        lines.push(if zh_cn {
            format!("  (状态过滤: {status})")
        } else {
            format!("  (status filter: {status})")
        });
    }
    if !options.flat {
        lines.push(if zh_cn {
            "  (树形模式：使用 --flat 切换扁平展示)".to_string()
        } else {
            "  (tree mode: use --flat for flat list)".to_string()
        });
    }
    lines.push(if zh_cn {
        format!("  任务总数: {}", tasks.len())
    } else {
        format!("  Total tasks: {}", tasks.len())
    });
    lines.push(String::new());

    if options.flat {
        for task in tasks {
            if !options.blocked || task.blocking {
                lines.push(format_task_summary(task, 0, true));
            }
        }
        return Ok(lines.join("\n") + "\n");
    }

    let ids = tasks
        .iter()
        .map(|task| task.task_id.as_str())
        .collect::<HashSet<_>>();
    let mut children = HashMap::<&str, Vec<&TaskSummary>>::new();
    let mut roots = Vec::new();
    for task in tasks {
        if task.parent_id.is_empty() {
            roots.push(task);
        } else {
            children
                .entry(task.parent_id.as_str())
                .or_default()
                .push(task);
        }
    }
    let mut printed = HashSet::new();
    for task in &roots {
        format_task_branch(
            task,
            0,
            options.blocked,
            &children,
            &mut HashSet::new(),
            &mut printed,
            &mut lines,
        )?;
    }
    for task in tasks {
        if !task.parent_id.is_empty()
            && !ids.contains(task.parent_id.as_str())
            && !printed.contains(task.task_id.as_str())
        {
            format_task_branch(
                task,
                0,
                options.blocked,
                &children,
                &mut HashSet::new(),
                &mut printed,
                &mut lines,
            )?;
        }
    }
    for task in tasks {
        if !printed.contains(task.task_id.as_str()) {
            format_task_branch(
                task,
                0,
                options.blocked,
                &children,
                &mut HashSet::new(),
                &mut printed,
                &mut lines,
            )?;
        }
    }
    Ok(lines.join("\n") + "\n")
}

pub fn format_task_findings(task_id: &str, findings: &[TaskFinding], zh_cn: bool) -> String {
    let mut lines = vec![
        if zh_cn {
            "=== 任务质量发现 ===".to_string()
        } else {
            "=== Task Quality Findings ===".to_string()
        },
        if zh_cn {
            format!("  任务 ID: {task_id}")
        } else {
            format!("  Task ID: {task_id}")
        },
        if zh_cn {
            format!("  发现数量: {}", findings.len())
        } else {
            format!("  Findings: {}", findings.len())
        },
        String::new(),
    ];
    if findings.is_empty() {
        lines.push(if zh_cn {
            "  (无质量发现)".to_string()
        } else {
            "  (no findings)".to_string()
        });
        return lines.join("\n");
    }
    for finding in findings {
        let icon = match finding.severity.as_str() {
            "error" | "block" => "[!]",
            "warn" => "[~]",
            _ => "[i]",
        };
        lines.push(format!(
            "  {icon} #{} [{}] {} ({})",
            finding.id, finding.severity, finding.finding_type, finding.status
        ));
        lines.push(if zh_cn {
            format!("    消息: {}", finding.message)
        } else {
            format!("    Message: {}", finding.message)
        });
        if !finding.step_id.is_empty() {
            lines.push(if zh_cn {
                format!("    步骤: {}", finding.step_id)
            } else {
                format!("    Step: {}", finding.step_id)
            });
        }
        lines.push(if zh_cn {
            format!("    来源: {}", finding.source)
        } else {
            format!("    Source: {}", finding.source)
        });
        lines.push(String::new());
    }
    lines.join("\n")
}

pub fn format_task_show(
    task_id: &str,
    detail: Option<&TaskNode>,
    links: &TaskLinks,
    flat: bool,
    zh_cn: bool,
) -> String {
    let Some(detail) = detail else {
        return if zh_cn {
            format!("未找到任务: {task_id}")
        } else {
            format!("Task not found: {task_id}")
        };
    };
    let mut lines = Vec::new();
    if !flat {
        lines.push(if zh_cn {
            "任务详情".to_string()
        } else {
            "Task Detail".to_string()
        });
        lines.push("-".repeat(50));
    }
    format_task_node(detail, 0, flat, zh_cn, &mut lines);
    if !flat {
        lines.push(String::new());
    }
    format_task_links(links, &mut lines);
    lines.join("\n")
}

fn query_task_row(conn: &Connection, task_id: &str) -> Result<Option<TaskRow>, String> {
    conn.query_row(
        "SELECT id, title, COALESCE(description, ''), status,
                COALESCE(creator, ''), COALESCE(parent_id, ''),
                COALESCE(strftime('%Y-%m-%d %H:%M:%S', created_at,
                                  'unixepoch', 'localtime'), '?')
         FROM tasks WHERE id = ?1",
        params![task_id],
        row_to_task,
    )
    .optional()
    .map_err(|error| format!("cannot query task {task_id}: {error}"))
}

fn row_to_task(row: &rusqlite::Row<'_>) -> rusqlite::Result<TaskRow> {
    Ok(TaskRow {
        task_id: row.get(0)?,
        title: row.get(1)?,
        description: row.get(2)?,
        status: row.get(3)?,
        creator: row.get(4)?,
        parent_id: row.get(5)?,
        created_display: row.get(6)?,
    })
}

fn query_task_steps(conn: &Connection, task_id: &str) -> Result<Vec<TaskStep>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT step_index, action, COALESCE(target_file, ''),
                    COALESCE(target_symbol, ''), status
             FROM task_steps WHERE task_id = ?1 ORDER BY step_index ASC",
        )
        .map_err(|error| format!("cannot prepare task steps query: {error}"))?;
    let rows = stmt
        .query_map(params![task_id], row_to_step)
        .map_err(|error| format!("cannot query task steps: {error}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read task steps: {error}"))?;
    Ok(rows)
}

fn query_steps_for_tree(
    conn: &Connection,
    root_task_id: &str,
) -> Result<HashMap<String, Vec<TaskStep>>, String> {
    let mut stmt = conn
        .prepare(
            "WITH RECURSIVE subtree(id) AS (
                 SELECT id FROM tasks WHERE id = ?1
                 UNION
                 SELECT t.id FROM tasks t JOIN subtree s ON t.parent_id = s.id
             )
             SELECT ts.task_id, ts.step_index, ts.action, COALESCE(ts.target_file, ''),
                    COALESCE(target_symbol, ''), status
             FROM task_steps ts JOIN subtree s ON s.id = ts.task_id
             ORDER BY ts.task_id ASC, ts.step_index ASC",
        )
        .map_err(|error| format!("cannot prepare task tree steps query: {error}"))?;
    let rows = stmt
        .query_map(params![root_task_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                TaskStep {
                    step_index: row.get(1)?,
                    action: row.get(2)?,
                    target_file: row.get(3)?,
                    target_symbol: row.get(4)?,
                    status: row.get(5)?,
                },
            ))
        })
        .map_err(|error| format!("cannot query task tree steps: {error}"))?;
    let mut result = HashMap::<String, Vec<TaskStep>>::new();
    for row in rows {
        let (task_id, step) =
            row.map_err(|error| format!("cannot read task tree steps: {error}"))?;
        result.entry(task_id).or_default().push(step);
    }
    Ok(result)
}

fn row_to_step(row: &rusqlite::Row<'_>) -> rusqlite::Result<TaskStep> {
    Ok(TaskStep {
        step_index: row.get(0)?,
        action: row.get(1)?,
        target_file: row.get(2)?,
        target_symbol: row.get(3)?,
        status: row.get(4)?,
    })
}

fn build_task_node(
    task_id: &str,
    rows: &HashMap<String, TaskRow>,
    children: &HashMap<String, Vec<String>>,
    steps: &HashMap<String, Vec<TaskStep>>,
    visiting: &mut HashSet<String>,
) -> Result<TaskNode, String> {
    if !visiting.insert(task_id.to_string()) {
        return Err(format!("task hierarchy contains a cycle at {task_id}"));
    }
    let row = rows
        .get(task_id)
        .ok_or_else(|| format!("task hierarchy references missing task {task_id}"))?;
    let mut subtasks = Vec::new();
    for child_id in children.get(task_id).into_iter().flatten() {
        subtasks.push(build_task_node(child_id, rows, children, steps, visiting)?);
    }
    visiting.remove(task_id);
    Ok(TaskNode {
        task_id: row.task_id.clone(),
        title: row.title.clone(),
        description: row.description.clone(),
        status: row.status.clone(),
        creator: row.creator.clone(),
        created_display: row.created_display.clone(),
        steps: steps.get(task_id).cloned().unwrap_or_default(),
        subtasks,
    })
}

fn format_task_branch<'a>(
    task: &'a TaskSummary,
    depth: usize,
    blocked_only: bool,
    children: &HashMap<&'a str, Vec<&'a TaskSummary>>,
    visiting: &mut HashSet<&'a str>,
    printed: &mut HashSet<&'a str>,
    lines: &mut Vec<String>,
) -> Result<bool, String> {
    if !visiting.insert(task.task_id.as_str()) {
        return Err(format!(
            "task hierarchy contains a cycle at {}",
            task.task_id
        ));
    }
    let mut child_lines = Vec::new();
    let mut include_child = false;
    for child in children.get(task.task_id.as_str()).into_iter().flatten() {
        include_child |= format_task_branch(
            child,
            depth + 1,
            blocked_only,
            children,
            visiting,
            printed,
            &mut child_lines,
        )?;
    }
    visiting.remove(task.task_id.as_str());
    let include = !blocked_only || task.blocking || include_child;
    if include {
        lines.push(format_task_summary(task, depth, false));
        lines.extend(child_lines);
        printed.insert(task.task_id.as_str());
    }
    Ok(include)
}

fn format_task_summary(task: &TaskSummary, depth: usize, flat: bool) -> String {
    let icon = if task.blocking { "[!]" } else { "[ ]" };
    let indent = if flat {
        String::new()
    } else {
        "    ".repeat(depth)
    };
    format!(
        "{indent}  {icon} {} [{}] {}",
        task.task_id, task.status, task.title
    )
}

fn format_task_node(
    node: &TaskNode,
    depth: usize,
    flat: bool,
    zh_cn: bool,
    lines: &mut Vec<String>,
) {
    let indent = "    ".repeat(depth);
    if depth == 0 {
        lines.push(format!("  ID: {}", node.task_id));
        lines.push(if zh_cn {
            format!("  标题: {}", node.title)
        } else {
            format!("  Title: {}", node.title)
        });
        lines.push(if zh_cn {
            format!("  状态: {}", node.status)
        } else {
            format!("  Status: {}", node.status)
        });
        if !node.description.is_empty() {
            lines.push(if zh_cn {
                format!("  描述: {}", node.description)
            } else {
                format!("  Description: {}", node.description)
            });
        }
        if !node.creator.is_empty() {
            lines.push(if zh_cn {
                format!("  创建者: {}", node.creator)
            } else {
                format!("  Creator: {}", node.creator)
            });
        }
        lines.push(if zh_cn {
            format!("  创建时间: {}", node.created_display)
        } else {
            format!("  Created: {}", node.created_display)
        });
        if flat {
            lines.push(String::new());
        }
    } else {
        lines.push(format!(
            "{indent}  ↳ {} [{}] {}",
            node.task_id, node.status, node.title
        ));
    }

    if !flat {
        let (done, total) = task_progress(node);
        if total > 0 {
            let pct = done as f64 / total as f64;
            let pct = format!("{pct:?}");
            lines.push(if zh_cn {
                format!("{indent}  进度: {done}/{total} ({pct}%)")
            } else {
                format!("{indent}  Progress: {done}/{total} ({pct}%)")
            });
        }
    }

    if depth == 0 {
        lines.push(if zh_cn {
            format!("  步骤（{} 个）:", node.steps.len())
        } else {
            format!("  Steps ({}):", node.steps.len())
        });
        for step in &node.steps {
            lines.push(format!(
                "    #{} [{}] {}",
                step.step_index, step.status, step.action
            ));
            if !step.target_file.is_empty() {
                lines.push(if zh_cn {
                    format!("        文件: {}", step.target_file)
                } else {
                    format!("        File: {}", step.target_file)
                });
            }
            if !step.target_symbol.is_empty() {
                lines.push(if zh_cn {
                    format!("        符号: {}", step.target_symbol)
                } else {
                    format!("        Symbol: {}", step.target_symbol)
                });
            }
        }
    }

    if !flat && !node.subtasks.is_empty() {
        if depth == 0 {
            lines.push(String::new());
            lines.push(if zh_cn {
                format!("  子任务（{} 个）:", node.subtasks.len())
            } else {
                format!("  Subtasks ({}):", node.subtasks.len())
            });
        }
        for child in &node.subtasks {
            format_task_node(child, depth + 1, false, zh_cn, lines);
        }
    }
}

fn task_progress(node: &TaskNode) -> (usize, usize) {
    let mut total = node.steps.len();
    let mut done = node
        .steps
        .iter()
        .filter(|step| matches!(step.status.as_str(), "done" | "skipped"))
        .count();
    for child in &node.subtasks {
        let (child_done, child_total) = task_progress(child);
        done += child_done;
        total += child_total;
    }
    (done, total)
}

fn format_task_links(links: &TaskLinks, lines: &mut Vec<String>) {
    if links.commits.is_empty() && links.symbol_changes.is_empty() {
        return;
    }
    lines.push("── Related ──".to_string());
    if !links.commits.is_empty() {
        lines.push(format!("Commits ({}):", links.commits.len()));
        for commit in &links.commits {
            let short = commit.commit_hash.chars().take(8).collect::<String>();
            let suffix = if commit.change_count == 1 { "" } else { "s" };
            lines.push(format!(
                "  {short} {} [{} change{suffix}]",
                commit.subject, commit.change_count
            ));
            if !commit.author.is_empty() {
                lines.push(format!("       by {}", commit.author));
            }
        }
    }
    if !links.symbol_changes.is_empty() {
        lines.push(format!("Symbol changes ({}):", links.symbol_changes.len()));
        for change in links.symbol_changes.iter().take(10) {
            let name = if change.qualified_name.is_empty() {
                &change.symbol_name
            } else {
                &change.qualified_name
            };
            let tag = if change.source_commit_hash.is_empty() {
                String::new()
            } else {
                format!(
                    " [commit:{}]",
                    change
                        .source_commit_hash
                        .chars()
                        .take(8)
                        .collect::<String>()
                )
            };
            lines.push(format!("  {name} {}{tag}", change.change_type));
        }
        if links.symbol_changes.len() > 10 {
            lines.push(format!(
                "  ... and {} more",
                links.symbol_changes.len() - 10
            ));
        }
    }
}

fn unix_timestamp() -> Result<f64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .map_err(|error| format!("system clock is before Unix epoch: {error}"))
}

fn generate_id(prefix: &str) -> Result<String, String> {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("system clock is before Unix epoch: {error}"))?;
    let millis = duration.as_millis();
    let counter = TASK_ID_COUNTER.fetch_add(1, Ordering::Relaxed);
    let mut hasher = Sha256::new();
    hasher.update(prefix.as_bytes());
    hasher.update(duration.as_nanos().to_le_bytes());
    hasher.update(process::id().to_le_bytes());
    hasher.update(counter.to_le_bytes());
    let digest = hasher.finalize();
    Ok(format!("{prefix}-{millis}-{}", hex::encode(&digest[..4])))
}

fn serialize_check_items(value: &Value) -> Result<String, String> {
    match value {
        Value::Null => Ok(String::new()),
        Value::String(text) => Ok(text.clone()),
        _ => python_style_json(value),
    }
}

fn python_style_json(value: &Value) -> Result<String, String> {
    match value {
        Value::Null => Ok("null".to_string()),
        Value::Bool(flag) => Ok(if *flag { "true" } else { "false" }.to_string()),
        Value::Number(number) => Ok(number.to_string()),
        Value::String(text) => serde_json::to_string(text)
            .map_err(|error| format!("cannot serialize task check_items string: {error}")),
        Value::Array(items) => {
            let items = items
                .iter()
                .map(python_style_json)
                .collect::<Result<Vec<_>, _>>()?;
            Ok(format!("[{}]", items.join(", ")))
        }
        Value::Object(items) => {
            let fields = items
                .iter()
                .map(|(key, value)| {
                    let key = serde_json::to_string(key).map_err(|error| {
                        format!("cannot serialize task check_items key: {error}")
                    })?;
                    Ok(format!("{key}: {}", python_style_json(value)?))
                })
                .collect::<Result<Vec<_>, String>>()?;
            Ok(format!("{{{}}}", fields.join(", ")))
        }
    }
}

fn deserialize_check_items(raw: &str) -> Value {
    if raw.is_empty() {
        return Value::String(String::new());
    }
    serde_json::from_str(raw).unwrap_or_else(|_| Value::String(raw.to_string()))
}

fn reopen_parent_chain_if_needed(
    tx: &Transaction<'_>,
    parent_id: &str,
    parent_status: &str,
    check_siblings: bool,
    now: f64,
) -> Result<(), String> {
    let mut current_id = parent_id.to_string();
    let mut current_status = parent_status.to_string();
    let mut first = true;
    let mut visited = HashSet::new();
    loop {
        if !visited.insert(current_id.clone()) {
            return Err(format!("task parent cycle detected at {current_id}"));
        }
        if !matches!(current_status.as_str(), "review" | "applied" | "closed") {
            return Ok(());
        }
        if first && check_siblings {
            let non_closed = tx
                .query_row(
                    "SELECT EXISTS(
                         SELECT 1 FROM tasks
                         WHERE parent_id = ?1 AND status != 'closed'
                     )",
                    params![current_id],
                    |row| row.get::<_, i64>(0),
                )
                .map_err(|error| format!("cannot inspect sibling task states: {error}"))?;
            if non_closed != 0 {
                return Ok(());
            }
        }
        let updated = tx
            .execute(
                "UPDATE tasks
                 SET status = 'in_progress', applied_at = NULL, closed_at = NULL,
                     updated_at = ?1
                 WHERE id = ?2 AND status = ?3",
                params![now, current_id, current_status],
            )
            .map_err(|error| format!("cannot reopen parent task {current_id}: {error}"))?;
        if updated != 1 {
            return Err(format!("parent task {current_id} changed concurrently"));
        }
        let parent = tx
            .query_row(
                "SELECT COALESCE(parent_id, '') FROM tasks WHERE id = ?1",
                params![current_id],
                |row| row.get::<_, String>(0),
            )
            .map_err(|error| format!("cannot read parent chain for {current_id}: {error}"))?;
        if parent.is_empty() {
            return Ok(());
        }
        current_id = parent;
        current_status = tx
            .query_row(
                "SELECT status FROM tasks WHERE id = ?1",
                params![current_id],
                |row| row.get::<_, String>(0),
            )
            .map_err(|error| format!("cannot inspect ancestor task {current_id}: {error}"))?;
        first = false;
    }
}

fn find_priority_fix_step(
    tx: &Transaction<'_>,
    task_id: &str,
) -> Result<Option<PendingTaskStep>, String> {
    tx.query_row(
        "WITH RECURSIVE subtree(id) AS (
             SELECT id FROM tasks WHERE id = ?1
             UNION
             SELECT t.id FROM tasks t JOIN subtree s ON t.parent_id = s.id
         )
         SELECT ts.id, ts.task_id, ts.step_index, ts.action,
                COALESCE(ts.target_file, ''), COALESCE(ts.target_symbol, ''),
                COALESCE(ts.check_items, ''), t.title
         FROM task_steps ts
         JOIN tasks t ON t.id = ts.task_id
         JOIN subtree s ON s.id = ts.task_id
         WHERE ts.status = 'pending'
           AND ts.action = 'fix_quality_gate_failure'
         ORDER BY ts.created_at ASC, ts.step_index ASC, ts.id ASC
         LIMIT 1",
        params![task_id],
        |row| {
            let raw: String = row.get(6)?;
            Ok(PendingTaskStep {
                step_id: row.get(0)?,
                task_id: row.get(1)?,
                step_index: row.get(2)?,
                action: row.get(3)?,
                target_file: row.get(4)?,
                target_symbol: row.get(5)?,
                check_items: deserialize_check_items(&raw),
                task_title: row.get(7)?,
            })
        },
    )
    .optional()
    .map_err(|error| format!("cannot find priority quality-fix step: {error}"))
}

fn find_next_pending_step_tree(
    tx: &Transaction<'_>,
    task_id: &str,
    visited: &mut HashSet<String>,
) -> Result<Option<PendingTaskStep>, String> {
    if !visited.insert(task_id.to_string()) {
        return Err(format!("task child cycle detected at {task_id}"));
    }
    let mut stmt = tx
        .prepare(
            "SELECT id, status FROM tasks
             WHERE parent_id = ?1 ORDER BY sort_order ASC, created_at ASC, id ASC",
        )
        .map_err(|error| format!("cannot prepare child task query: {error}"))?;
    let children = stmt
        .query_map(params![task_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|error| format!("cannot query child tasks: {error}"))?
        .collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read child tasks: {error}"))?;
    drop(stmt);
    for (child_id, status) in children {
        if matches!(status.as_str(), "closed" | "applied" | "reverted") {
            continue;
        }
        if let Some(step) = find_next_pending_step_tree(tx, &child_id, visited)? {
            return Ok(Some(step));
        }
    }

    tx.query_row(
        "SELECT ts.id, ts.task_id, ts.step_index, ts.action,
                COALESCE(ts.target_file, ''), COALESCE(ts.target_symbol, ''),
                COALESCE(ts.check_items, ''), t.title
         FROM task_steps ts
         JOIN tasks t ON t.id = ts.task_id
         WHERE ts.task_id = ?1 AND ts.status = 'pending'
         ORDER BY CASE WHEN ts.action = 'fix_quality_gate_failure' THEN 0 ELSE 1 END,
                  ts.step_index ASC, ts.created_at ASC
         LIMIT 1",
        params![task_id],
        |row| {
            let raw: String = row.get(6)?;
            Ok(PendingTaskStep {
                step_id: row.get(0)?,
                task_id: row.get(1)?,
                step_index: row.get(2)?,
                action: row.get(3)?,
                target_file: row.get(4)?,
                target_symbol: row.get(5)?,
                check_items: deserialize_check_items(&raw),
                task_title: row.get(7)?,
            })
        },
    )
    .optional()
    .map_err(|error| format!("cannot find pending step for task {task_id}: {error}"))
}

fn build_parent_chain(tx: &Transaction<'_>, task_id: &str) -> Result<Vec<TaskChainItem>, String> {
    let mut chain = Vec::new();
    let mut current_id = task_id.to_string();
    let mut visited = HashSet::new();
    loop {
        if !visited.insert(current_id.clone()) {
            return Err(format!("task parent cycle detected at {current_id}"));
        }
        let row = tx
            .query_row(
                "SELECT title, status, COALESCE(parent_id, '')
                 FROM tasks WHERE id = ?1",
                params![current_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                },
            )
            .optional()
            .map_err(|error| format!("cannot read task parent chain: {error}"))?
            .ok_or_else(|| format!("task not found in parent chain: {current_id}"))?;
        chain.push(TaskChainItem {
            task_id: current_id,
            title: row.0,
            status: row.1,
        });
        if row.2.is_empty() {
            break;
        }
        current_id = row.2;
    }
    chain.reverse();
    Ok(chain)
}

fn query_open_quality_findings(
    tx: &Transaction<'_>,
    task_id: &str,
) -> Result<Vec<TaskFinding>, String> {
    let mut stmt = tx
        .prepare(
            "SELECT id, COALESCE(step_id, ''), finding_type, severity, status,
                    message, COALESCE(source, '')
             FROM task_quality_findings
             WHERE task_id = ?1 AND status = 'open'
             ORDER BY created_at ASC LIMIT 10",
        )
        .map_err(|error| format!("cannot prepare open finding query: {error}"))?;
    let rows = stmt
        .query_map(params![task_id], |row| {
            Ok(TaskFinding {
                id: row.get(0)?,
                step_id: row.get(1)?,
                finding_type: row.get(2)?,
                severity: row.get(3)?,
                status: row.get(4)?,
                message: row.get(5)?,
                source: row.get(6)?,
            })
        })
        .map_err(|error| format!("cannot query open task findings: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read open task findings: {error}"))
}

fn is_ancestor_or_same(
    tx: &Transaction<'_>,
    ancestor_id: &str,
    task_id: &str,
) -> Result<bool, String> {
    tx.query_row(
        "WITH RECURSIVE ancestors(id, parent_id) AS (
             SELECT id, COALESCE(parent_id, '') FROM tasks WHERE id = ?1
             UNION
             SELECT t.id, COALESCE(t.parent_id, '')
             FROM tasks t JOIN ancestors a ON t.id = a.parent_id
         )
         SELECT EXISTS(SELECT 1 FROM ancestors WHERE id = ?2)",
        params![task_id, ancestor_id],
        |row| row.get::<_, i64>(0),
    )
    .map(|value| value != 0)
    .map_err(|error| format!("cannot validate task ancestry: {error}"))
}

fn task_can_enter_review(tx: &Transaction<'_>, task_id: &str) -> Result<bool, String> {
    let incomplete = tx
        .query_row(
            "SELECT EXISTS(
                 SELECT 1 FROM task_steps
                 WHERE task_id = ?1 AND status NOT IN ('done', 'skipped')
             )",
            params![task_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|error| format!("cannot inspect unfinished task steps: {error}"))?;
    if incomplete != 0 {
        return Ok(false);
    }
    let blocking = tx
        .query_row(
            "SELECT EXISTS(
                 SELECT 1 FROM task_quality_findings
                 WHERE task_id = ?1 AND status = 'open'
                   AND severity IN ('error', 'block')
             )",
            params![task_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|error| format!("cannot inspect blocking task findings: {error}"))?;
    Ok(blocking == 0)
}

fn update_parent_statuses(tx: &Transaction<'_>, task_id: &str, now: f64) -> Result<(), String> {
    let mut child_id = task_id.to_string();
    let mut visited = HashSet::new();
    loop {
        if !visited.insert(child_id.clone()) {
            return Err(format!("task parent cycle detected at {child_id}"));
        }
        let parent_id = tx
            .query_row(
                "SELECT COALESCE(parent_id, '') FROM tasks WHERE id = ?1",
                params![child_id],
                |row| row.get::<_, String>(0),
            )
            .map_err(|error| format!("cannot read task parent: {error}"))?;
        if parent_id.is_empty() {
            return Ok(());
        }
        let child_incomplete = tx
            .query_row(
                "SELECT EXISTS(
                     SELECT 1 FROM tasks
                     WHERE parent_id = ?1
                       AND status NOT IN ('review', 'applied', 'closed', 'reverted')
                 )",
                params![parent_id],
                |row| row.get::<_, i64>(0),
            )
            .map_err(|error| format!("cannot inspect child task states: {error}"))?;
        if child_incomplete != 0 || !task_can_enter_review(tx, &parent_id)? {
            return Ok(());
        }
        tx.execute(
            "UPDATE tasks SET status = 'review', updated_at = ?1
             WHERE id = ?2 AND status IN ('open', 'in_progress')",
            params![now, parent_id],
        )
        .map_err(|error| format!("cannot advance parent task {parent_id}: {error}"))?;
        child_id = parent_id;
    }
}

fn task_identity(conn: &Connection, task_id: &str) -> Result<(String, String, String), String> {
    conn.query_row(
        "SELECT status, COALESCE(parent_id, ''), COALESCE(creator, '')
         FROM tasks WHERE id = ?1",
        params![task_id],
        |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        },
    )
    .optional()
    .map_err(|error| format!("cannot inspect task {task_id}: {error}"))?
    .ok_or_else(|| format!("task not found: {task_id}"))
}

fn validate_independent_reviewer(
    task_id: &str,
    creator: &str,
    reviewer: &str,
) -> Result<(), String> {
    let reviewer = reviewer.trim();
    if reviewer.is_empty() {
        return Err("reviewer identity is required".to_string());
    }
    if !creator.trim().is_empty() && reviewer.eq_ignore_ascii_case(creator.trim()) {
        return Err(format!(
            "reviewer '{reviewer}' is the creator of task {task_id}; self-approval is forbidden"
        ));
    }
    Ok(())
}

fn reject_blocking_findings(conn: &Connection, task_id: &str) -> Result<(), String> {
    let blocking = conn
        .query_row(
            "SELECT COUNT(*) FROM task_quality_findings
             WHERE task_id = ?1 AND status = 'open'
               AND severity IN ('error', 'block')",
            params![task_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|error| format!("cannot inspect blocking findings: {error}"))?;
    if blocking > 0 {
        return Err(format!(
            "task {task_id} has {blocking} open error/block findings"
        ));
    }
    Ok(())
}

fn direct_step_count(conn: &Connection, task_id: &str) -> Result<i64, String> {
    conn.query_row(
        "SELECT COUNT(*) FROM task_steps WHERE task_id = ?1",
        params![task_id],
        |row| row.get::<_, i64>(0),
    )
    .map_err(|error| format!("cannot count task steps: {error}"))
}

fn cascade_close_if_ready(
    tx: &Transaction<'_>,
    parent_id: &str,
    reviewer: &str,
    now: f64,
    cascaded: &mut Vec<String>,
) -> Result<(), String> {
    let siblings = {
        let mut stmt = tx
            .prepare(
                "SELECT id, status, COALESCE(creator, '')
                 FROM tasks WHERE parent_id = ?1 ORDER BY sort_order, created_at",
            )
            .map_err(|error| format!("cannot prepare cascade siblings: {error}"))?;
        let rows = stmt
            .query_map(params![parent_id], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, String>(2)?,
                ))
            })
            .map_err(|error| format!("cannot query cascade siblings: {error}"))?;
        rows.collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read cascade siblings: {error}"))?
    };
    if siblings.is_empty()
        || siblings
            .iter()
            .any(|(_, status, _)| !matches!(status.as_str(), "applied" | "closed"))
    {
        return Ok(());
    }
    let (parent_status, grandparent_id, parent_creator) = task_identity(tx, parent_id)?;
    if parent_status != "review" {
        return Ok(());
    }
    validate_independent_reviewer(parent_id, &parent_creator, reviewer)?;
    reject_blocking_findings(tx, parent_id)?;
    for (sibling_id, status, creator) in &siblings {
        if status == "applied" {
            validate_independent_reviewer(sibling_id, creator, reviewer)?;
        }
    }
    for (sibling_id, status, _) in siblings {
        if status == "applied" {
            let updated = tx
                .execute(
                    "UPDATE tasks SET status = 'closed', closed_at = ?1, updated_at = ?1
                     WHERE id = ?2 AND status = 'applied'",
                    params![now, sibling_id],
                )
                .map_err(|error| format!("cannot cascade close task {sibling_id}: {error}"))?;
            if updated != 1 {
                return Err(format!("cascade task {sibling_id} changed concurrently"));
            }
            cascaded.push(sibling_id);
        }
    }
    let updated = tx
        .execute(
            "UPDATE tasks
             SET status = 'closed', applied_at = COALESCE(applied_at, ?1),
                 closed_at = ?1, updated_at = ?1
             WHERE id = ?2 AND status = 'review'",
            params![now, parent_id],
        )
        .map_err(|error| format!("cannot cascade close parent {parent_id}: {error}"))?;
    if updated != 1 {
        return Err(format!("cascade parent {parent_id} changed concurrently"));
    }
    cascaded.push(parent_id.to_string());
    if !grandparent_id.is_empty() {
        cascade_close_if_ready(tx, &grandparent_id, reviewer, now, cascaded)?;
    }
    Ok(())
}

fn parse_split_plan(plan_markdown: &str) -> Result<Vec<TaskSplitDefinition>, String> {
    let mut definitions = Vec::new();
    let mut title: Option<String> = None;
    let mut description = Vec::new();
    let mut steps = Vec::new();
    let mut in_code_block = false;

    let finish = |definitions: &mut Vec<TaskSplitDefinition>,
                  title: &mut Option<String>,
                  description: &mut Vec<String>,
                  steps: &mut Vec<TaskStepInput>| {
        if let Some(title_value) = title.take() {
            definitions.push(TaskSplitDefinition {
                title: title_value,
                description: description.join("\n").trim().to_string(),
                steps: std::mem::take(steps),
            });
            description.clear();
        }
    };

    for raw_line in plan_markdown.lines() {
        let line = raw_line.trim();
        if line.starts_with("```") || line.starts_with("~~~") {
            in_code_block = !in_code_block;
            continue;
        }
        if in_code_block || line.is_empty() {
            continue;
        }
        if line.starts_with("## ") && !line.starts_with("### ") {
            finish(&mut definitions, &mut title, &mut description, &mut steps);
            let parsed = line[3..].trim().trim_end_matches('#').trim();
            if parsed.is_empty() {
                return Err("split plan contains an empty subtask title".to_string());
            }
            title = Some(parsed.to_string());
            continue;
        }
        let list_content = ["- ", "* ", "+ "]
            .iter()
            .find_map(|prefix| line.strip_prefix(prefix));
        if let Some(content) = list_content {
            if title.is_none() {
                continue;
            }
            let content = content.trim();
            let (action, target_file) = if let Some((action, target)) = content.split_once('@') {
                (action.trim(), target.trim())
            } else if let Some((action, target)) = content.split_once(':') {
                (action.trim(), target.trim())
            } else {
                (content, "")
            };
            if action.is_empty() {
                return Err("split plan contains an empty step action".to_string());
            }
            steps.push(TaskStepInput {
                action: action.to_string(),
                target_file: target_file.to_string(),
                target_symbol: String::new(),
                check_items: Value::String(String::new()),
            });
            continue;
        }
        if title.is_some() && !line.starts_with('#') {
            description.push(line.to_string());
        }
    }
    if in_code_block {
        return Err("split plan has an unclosed code fence".to_string());
    }
    finish(&mut definitions, &mut title, &mut description, &mut steps);
    Ok(definitions)
}

fn active_workspace(conn: &Connection) -> Result<(i64, String), String> {
    conn.query_row(
        "SELECT id, root_path FROM workspaces WHERE is_active = 1 ORDER BY id LIMIT 1",
        [],
        |row| Ok((row.get::<_, i64>(0)?, row.get::<_, String>(1)?)),
    )
    .map_err(|error| format!("cannot resolve active workspace: {error}"))
}

fn active_task_id(conn: &Connection) -> Result<Option<String>, String> {
    let active = conn
        .query_row(
            "SELECT COALESCE(active_task_id, '') FROM workspaces
             WHERE is_active = 1 ORDER BY id LIMIT 1",
            [],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| format!("cannot query active task: {error}"))?
        .unwrap_or_default();
    if !active.is_empty() {
        return Ok(Some(active));
    }
    conn.query_row(
        "SELECT id FROM tasks WHERE status = 'in_progress'
         ORDER BY sort_order ASC, created_at DESC LIMIT 1",
        [],
        |row| row.get::<_, String>(0),
    )
    .optional()
    .map_err(|error| format!("cannot find in-progress task: {error}"))
}

fn validate_task_scope(conn: &Connection, task_id: &str, step_id: &str) -> Result<(), String> {
    let exists = conn
        .query_row(
            "SELECT EXISTS(SELECT 1 FROM tasks WHERE id = ?1)",
            params![task_id],
            |row| row.get::<_, i64>(0),
        )
        .map_err(|error| format!("cannot validate capture task: {error}"))?;
    if exists == 0 {
        return Err(format!("task not found: {task_id}"));
    }
    if !step_id.trim().is_empty() {
        let valid = conn
            .query_row(
                "SELECT EXISTS(
                     SELECT 1 FROM task_steps WHERE id = ?1 AND task_id = ?2
                 )",
                params![step_id, task_id],
                |row| row.get::<_, i64>(0),
            )
            .map_err(|error| format!("cannot validate capture step: {error}"))?;
        if valid == 0 {
            return Err(format!(
                "task step {step_id} does not belong to task {task_id}"
            ));
        }
    }
    Ok(())
}

fn validate_task_scope_tx(
    tx: &Transaction<'_>,
    task_id: &str,
    step_id: &str,
) -> Result<(), String> {
    validate_task_scope(tx, task_id, step_id)
}

fn collect_workspace_changes(
    conn: &Connection,
    workspace_id: i64,
    root: &Path,
    base: &str,
) -> Result<(Vec<ChangedFile>, String), String> {
    if git_stdout(root, &["rev-parse", "--is-inside-work-tree"])
        .map(|value| value == "true")
        .unwrap_or(false)
    {
        let mut changes = Vec::<ChangedFile>::new();
        let mut seen_paths = HashSet::<String>::new();
        let mut raw = String::new();
        if !base.trim().is_empty() {
            let diff = git_stdout(root, &["diff", "--name-status", base, "--"])?;
            raw.push_str(&diff);
            for line in diff.lines() {
                let parts = line.split('\t').collect::<Vec<_>>();
                if parts.len() < 2 {
                    continue;
                }
                let code = parts[0];
                let path = parts.last().copied().unwrap_or_default();
                if !path.is_empty() {
                    let path = normalize_rel_path(path);
                    if seen_paths.insert(path.clone()) {
                        changes.push(ChangedFile {
                            path,
                            status: normalize_git_status(code),
                        });
                    }
                }
            }
        }
        let status = git_stdout(root, &["status", "--porcelain=v1", "--untracked-files=all"])?;
        raw.push_str(&status);
        for line in status.lines() {
            if let Some((path, worktree_status)) = parse_git_porcelain_line(line) {
                if seen_paths.insert(path.clone()) {
                    changes.push(ChangedFile {
                        path,
                        status: worktree_status,
                    });
                }
            }
        }
        return Ok((changes, raw));
    }

    let mut stmt = conn
        .prepare(
            "SELECT rel_path, COALESCE(current_content_hash, '')
             FROM file_instances WHERE workspace_id = ?1 AND status != 'archived'
             ORDER BY rel_path",
        )
        .map_err(|error| format!("cannot prepare non-git change scan: {error}"))?;
    let rows = stmt
        .query_map(params![workspace_id], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        })
        .map_err(|error| format!("cannot scan indexed files: {error}"))?;
    let mut changed = Vec::new();
    for row in rows {
        let (rel_path, before) =
            row.map_err(|error| format!("cannot read indexed file: {error}"))?;
        let path = root.join(&rel_path);
        let status = if !path.exists() {
            Some("D".to_string())
        } else {
            let after = file_content_hash(&path).unwrap_or_default();
            (after != before).then(|| "M".to_string())
        };
        if let Some(status) = status {
            changed.push(ChangedFile {
                path: normalize_rel_path(&rel_path),
                status,
            });
        }
    }
    Ok((changed, String::new()))
}

fn git_stdout(root: &Path, args: &[&str]) -> Result<String, String> {
    let output = Command::new("git")
        .args(args)
        .current_dir(root)
        .output()
        .map_err(|error| format!("cannot run git {}: {error}", args.join(" ")))?;
    if !output.status.success() {
        return Err(format!(
            "git {} failed: {}",
            args.join(" "),
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(String::from_utf8_lossy(&output.stdout)
        .trim_end_matches(['\r', '\n'])
        .to_string())
}

fn parse_git_porcelain_line(line: &str) -> Option<(String, String)> {
    if line.len() < 4 || !line.is_char_boundary(2) || !line.is_char_boundary(3) {
        return None;
    }
    let code = &line[..2];
    let mut path = line[3..].trim();
    if let Some((_, renamed)) = path.rsplit_once(" -> ") {
        path = renamed;
    }
    let path = path.trim_matches('"');
    if path.is_empty() {
        return None;
    }
    let bytes = code.as_bytes();
    let status = if code == "??" {
        "untracked".to_string()
    } else if bytes[0] != b' ' && bytes[1] != b' ' {
        "staged+worktree".to_string()
    } else if bytes[0] != b' ' {
        format!("staged-{}", bytes[0] as char)
    } else if bytes[1] != b' ' {
        format!("worktree-{}", bytes[1] as char)
    } else {
        "modified".to_string()
    };
    Some((normalize_rel_path(path), status))
}

fn normalize_git_status(code: &str) -> String {
    let code = code.trim();
    if code == "??" {
        return "untracked".to_string();
    }
    match code.chars().next().unwrap_or('M') {
        'A' => "A",
        'D' => "D",
        'R' => "R",
        _ => "M",
    }
    .to_string()
}

fn normalize_rel_path(path: &str) -> String {
    path.replace('\\', "/")
}

fn file_content_hash(path: &Path) -> Result<String, String> {
    if !path.exists() {
        return Ok(String::new());
    }
    let path = path
        .to_str()
        .ok_or_else(|| format!("file path is not UTF-8: {}", path.display()))?;
    canonicalize_source(path)
        .map(|result| result.content_hash)
        .map_err(|error| format!("cannot canonicalize {path}: {error}"))
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

fn query_open_quality_findings_scoped(
    conn: &Connection,
    task_id: &str,
    step_id: &str,
) -> Result<Vec<TaskFinding>, String> {
    let all = query_open_quality_findings_conn(conn, task_id)?;
    if step_id.is_empty() {
        return Ok(all);
    }
    Ok(all
        .into_iter()
        .filter(|finding| finding.step_id.is_empty() || finding.step_id == step_id)
        .collect())
}

fn query_open_quality_findings_conn(
    conn: &Connection,
    task_id: &str,
) -> Result<Vec<TaskFinding>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT id, COALESCE(step_id, ''), finding_type, severity, status,
                    message, COALESCE(source, '')
             FROM task_quality_findings
             WHERE task_id = ?1 AND status = 'open'
             ORDER BY created_at ASC",
        )
        .map_err(|error| format!("cannot prepare task finding decision: {error}"))?;
    let rows = stmt
        .query_map(params![task_id], |row| {
            Ok(TaskFinding {
                id: row.get(0)?,
                step_id: row.get(1)?,
                finding_type: row.get(2)?,
                severity: row.get(3)?,
                status: row.get(4)?,
                message: row.get(5)?,
                source: row.get(6)?,
            })
        })
        .map_err(|error| format!("cannot query task finding decision: {error}"))?;
    rows.collect::<Result<Vec<_>, _>>()
        .map_err(|error| format!("cannot read task finding decision: {error}"))
}

fn finding_decision(findings: &[TaskFinding]) -> &'static str {
    if findings
        .iter()
        .any(|finding| matches!(finding.severity.as_str(), "error" | "block"))
    {
        "block"
    } else if findings.is_empty() {
        "pass"
    } else {
        "warn"
    }
}

fn table_exists(conn: &Connection, table: &str) -> Result<bool, String> {
    conn.query_row(
        "SELECT EXISTS(SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?1)",
        params![table],
        |row| row.get::<_, i64>(0),
    )
    .map(|value| value != 0)
    .map_err(|error| format!("cannot inspect SQLite schema: {error}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fixture() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces(
                 id INTEGER PRIMARY KEY, name TEXT, root_path TEXT,
                 is_active INTEGER, active_task_id TEXT
             );
             CREATE TABLE tasks(
                 id TEXT PRIMARY KEY, title TEXT, description TEXT, creator TEXT,
                 status TEXT, created_at REAL, updated_at REAL, applied_at REAL,
                 closed_at REAL, parent_id TEXT, depth INTEGER, sort_order INTEGER
             );
             CREATE TABLE task_steps(
                 id TEXT PRIMARY KEY, task_id TEXT, step_index INTEGER, action TEXT,
                 target_file TEXT, target_symbol TEXT, check_items TEXT, status TEXT,
                 result TEXT, created_at REAL, completed_at REAL
             );
             CREATE TABLE task_quality_findings(
                 id INTEGER PRIMARY KEY, workspace_id INTEGER, task_id TEXT, step_id TEXT,
                 finding_type TEXT, severity TEXT, status TEXT, message TEXT,
                 evidence TEXT, source TEXT, created_at REAL, resolved_at REAL,
                 resolved_by TEXT
             );
             CREATE TABLE change_audit(
                 id TEXT PRIMARY KEY, task_id TEXT, step_id TEXT, file_path TEXT,
                 hash_before TEXT, hash_after TEXT, diff TEXT, author TEXT,
                 timestamp REAL
             );
             CREATE TABLE file_instances(
                 id INTEGER PRIMARY KEY, workspace_id INTEGER, rel_path TEXT,
                 current_content_hash TEXT, status TEXT
             );
             CREATE TABLE workspace_scan_runs(
                 id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER,
                 purpose TEXT, task_id TEXT, step_id TEXT, baseline_type TEXT,
                 git_head TEXT, git_merge_base TEXT, git_status_hash TEXT,
                 root_mtime REAL DEFAULT 0, file_count INTEGER,
                 manifest_hash TEXT DEFAULT '', changed_files_json TEXT,
                 metadata_json TEXT, started_at REAL, completed_at REAL, status TEXT
             );
             CREATE TABLE task_symbol_changes(
                 id INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id INTEGER,
                 task_id TEXT, step_id TEXT, edit_audit_id INTEGER DEFAULT 0,
                 change_audit_id TEXT, file_path TEXT, qualified_name TEXT DEFAULT '',
                 symbol_name TEXT DEFAULT '', symbol_hash_before TEXT,
                 symbol_hash_after TEXT, change_type TEXT, source TEXT,
                 source_commit_hash TEXT, metadata TEXT, created_at REAL
             );
             INSERT INTO tasks VALUES
                 ('root','Root','','agent','in_progress',1000,1000,NULL,NULL,'',0,0),
                 ('child','Child','','agent','review',1001,1001,NULL,NULL,'root',1,0);
             INSERT INTO task_steps VALUES
                 ('s1','root',0,'inspect','','','[]','done','',1000,1001),
                 ('s2','child',0,'fix','a.py','','[]','pending','',1001,NULL);
             INSERT INTO task_quality_findings VALUES
                 (1,1,'child','s2','scope','block','open','outside scope','','scope',1002,NULL,'');
             INSERT INTO workspaces VALUES (1, 'fixture', '/fixture', 1, '');",
        )
        .unwrap();
        conn
    }

    #[test]
    fn blocked_tree_keeps_ancestor_path() {
        let conn = fixture();
        let tasks = query_task_list(&conn, None, 20).unwrap();
        let output = format_task_list(
            &tasks,
            &TaskListOptions {
                blocked: true,
                limit: 20,
                status: None,
                flat: false,
            },
            false,
        )
        .unwrap();
        assert!(output.contains("[ ] root"));
        assert!(output.contains("    [!] child"));
    }

    #[test]
    fn task_tree_progress_and_findings_match_contract() {
        let conn = fixture();
        let tree = query_task_detail(&conn, "root", true).unwrap().unwrap();
        let output = format_task_show("root", Some(&tree), &TaskLinks::default(), false, false);
        assert!(output.contains("Progress: 1/2 (0.5%)"));
        assert!(output.contains("Subtasks (1):"));

        let findings = query_task_findings(&conn, "child", "open", "block").unwrap();
        let output = format_task_findings("child", &findings, false);
        assert!(output.contains("#1 [block] scope (open)"));
        assert!(output.contains("Message: outside scope"));
    }

    #[test]
    fn missing_or_corrupt_schema_fails_closed() {
        let conn = Connection::open_in_memory().unwrap();
        assert!(query_task_list(&conn, None, 20).is_err());
        assert!(query_task_findings(&conn, "missing", "open", "").is_err());
    }

    #[test]
    fn create_nested_task_reopens_completed_parent_chain() {
        let mut conn = fixture();
        conn.execute(
            "UPDATE tasks SET status = 'closed', applied_at = 10, closed_at = 11",
            [],
        )
        .unwrap();
        let result = create_task(
            &mut conn,
            "New child",
            "",
            vec![TaskStepInput {
                action: "inspect".to_string(),
                target_file: "src/lib.rs".to_string(),
                target_symbol: String::new(),
                check_items: Value::Array(vec![Value::String("syntax".to_string())]),
            }],
            "agent",
            Some("root"),
        )
        .unwrap();
        let row = conn
            .query_row(
                "SELECT parent_id, depth, sort_order FROM tasks WHERE id = ?1",
                params![result.task_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, i64>(1)?,
                        row.get::<_, i64>(2)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(row, ("root".to_string(), 1, 1));
        let root = conn
            .query_row(
                "SELECT status, applied_at, closed_at FROM tasks WHERE id = 'root'",
                [],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, Option<f64>>(1)?,
                        row.get::<_, Option<f64>>(2)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(root, ("in_progress".to_string(), None, None));
    }

    #[test]
    fn blocking_finding_blocks_normal_step_claim() {
        let mut conn = fixture();
        let claimed = claim_next_task_step(&mut conn, "child").unwrap().unwrap();
        assert_eq!(claimed.step_id, "s2");
        assert_eq!(claimed.status, "blocked");
        assert_eq!(claimed.open_quality_findings.len(), 1);
        assert_eq!(
            conn.query_row("SELECT status FROM task_steps WHERE id = 's2'", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
            "blocked"
        );
    }

    #[test]
    fn quality_fix_step_preempts_deeper_normal_work() {
        let mut conn = fixture();
        conn.execute(
            "INSERT INTO task_steps VALUES(
                 'quality-fix', 'root', 1, 'fix_quality_gate_failure',
                 '', '', '', 'pending', '', 2000, NULL
             )",
            [],
        )
        .unwrap();
        let claimed = claim_next_task_step(&mut conn, "root").unwrap().unwrap();
        assert_eq!(claimed.step_id, "quality-fix");
        assert_eq!(claimed.status, "in_progress");
    }

    #[test]
    fn claim_report_and_reopen_preserve_parent_state_machine() {
        let mut conn = fixture();
        conn.execute(
            "UPDATE task_quality_findings SET status = 'resolved' WHERE task_id = 'child'",
            [],
        )
        .unwrap();
        let claimed = claim_next_task_step(&mut conn, "root").unwrap().unwrap();
        assert_eq!(claimed.step_id, "s2");
        assert_eq!(claimed.task_id, "child");
        assert_eq!(
            claimed
                .parent_task_chain
                .iter()
                .map(|item| (item.task_id.as_str(), item.status.as_str()))
                .collect::<Vec<_>>(),
            vec![("root", "in_progress"), ("child", "review")]
        );
        assert_eq!(
            conn.query_row(
                "SELECT active_task_id FROM workspaces WHERE is_active = 1",
                [],
                |row| row.get::<_, String>(0)
            )
            .unwrap(),
            "root"
        );

        let report = report_task_step(&mut conn, "root", "s2", "done", true).unwrap();
        assert!(report.next_step.is_none());
        assert_eq!(
            conn.query_row("SELECT status FROM tasks WHERE id = 'child'", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
            "review"
        );
        assert_eq!(
            conn.query_row("SELECT status FROM tasks WHERE id = 'root'", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
            "review"
        );

        conn.execute(
            "UPDATE tasks SET status = 'closed', applied_at = 12, closed_at = 13
             WHERE id IN ('root', 'child')",
            [],
        )
        .unwrap();
        let reopened = reopen_task(&mut conn, "child", "reviewer", "regression").unwrap();
        assert_eq!(reopened.previous_status, "closed");
        assert_eq!(
            conn.query_row("SELECT status FROM tasks WHERE id = 'root'", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
            "in_progress"
        );
    }

    #[test]
    fn failed_report_inserts_fix_step_and_rejects_replay() {
        let mut conn = fixture();
        let claimed = claim_next_task_step(&mut conn, "child").unwrap().unwrap();
        report_task_step(&mut conn, "child", &claimed.step_id, "failed", false).unwrap();
        let fix = conn
            .query_row(
                "SELECT action, status FROM task_steps
                 WHERE task_id = 'child' ORDER BY step_index DESC LIMIT 1",
                [],
                |row| Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?)),
            )
            .unwrap();
        assert_eq!(fix, ("fix_defect".to_string(), "pending".to_string()));
        assert!(report_task_step(&mut conn, "child", &claimed.step_id, "again", true).is_err());
    }

    #[test]
    fn report_rolls_back_when_a_late_gate_query_fails() {
        let mut conn = fixture();
        conn.execute(
            "UPDATE task_quality_findings SET status = 'resolved' WHERE task_id = 'child'",
            [],
        )
        .unwrap();
        let claimed = claim_next_task_step(&mut conn, "child").unwrap().unwrap();
        conn.execute("DROP TABLE task_quality_findings", [])
            .unwrap();
        assert!(report_task_step(&mut conn, "child", &claimed.step_id, "done", true).is_err());
        let state = conn
            .query_row(
                "SELECT status, result, completed_at FROM task_steps WHERE id = ?1",
                params![claimed.step_id],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, Option<f64>>(2)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(state, ("in_progress".to_string(), String::new(), None));
    }

    #[test]
    fn immediate_transaction_allows_only_one_claim() {
        use std::sync::{Arc, Barrier};
        use std::thread;

        let path =
            std::env::temp_dir().join(format!("cw-task-{}.db", generate_id("test").unwrap()));
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE workspaces(
                     id INTEGER PRIMARY KEY, name TEXT, root_path TEXT,
                     is_active INTEGER, active_task_id TEXT
                 );
                 CREATE TABLE tasks(
                     id TEXT PRIMARY KEY, title TEXT, description TEXT, creator TEXT,
                     status TEXT, created_at REAL, updated_at REAL, applied_at REAL,
                     closed_at REAL, parent_id TEXT, depth INTEGER, sort_order INTEGER
                 );
                 CREATE TABLE task_steps(
                     id TEXT PRIMARY KEY, task_id TEXT, step_index INTEGER, action TEXT,
                     target_file TEXT, target_symbol TEXT, check_items TEXT, status TEXT,
                     result TEXT, created_at REAL, completed_at REAL
                 );
                 CREATE TABLE task_quality_findings(
                     id INTEGER PRIMARY KEY, workspace_id INTEGER, task_id TEXT, step_id TEXT,
                     finding_type TEXT, severity TEXT, status TEXT, message TEXT,
                     evidence TEXT, source TEXT, created_at REAL, resolved_at REAL,
                     resolved_by TEXT
                 );
                 INSERT INTO workspaces VALUES (1, 'fixture', '/fixture', 1, '');
                 INSERT INTO tasks VALUES
                     ('root','Root','','agent','open',1,1,NULL,NULL,'',0,0);
                 INSERT INTO task_steps VALUES
                     ('step','root',0,'inspect','','','','pending','',1,NULL);",
            )
            .unwrap();
        }
        let barrier = Arc::new(Barrier::new(3));
        let handles = (0..2)
            .map(|_| {
                let path = path.clone();
                let barrier = Arc::clone(&barrier);
                thread::spawn(move || {
                    let mut conn = Connection::open(path).unwrap();
                    conn.busy_timeout(std::time::Duration::from_secs(5))
                        .unwrap();
                    barrier.wait();
                    claim_next_task_step(&mut conn, "root").unwrap().is_some()
                })
            })
            .collect::<Vec<_>>();
        barrier.wait();
        let claimed = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .filter(|claimed| *claimed)
            .count();
        assert_eq!(claimed, 1);
        std::fs::remove_file(path).unwrap();
    }

    #[test]
    fn rollback_scopes_changes_and_clears_matching_active_task() {
        let mut conn = fixture();
        conn.execute(
            "UPDATE workspaces SET active_task_id = 'child' WHERE id = 1",
            [],
        )
        .unwrap();
        conn.execute_batch(
            "INSERT INTO change_audit VALUES
                 ('c1','child','s2','a.py','before-a','after-a','','agent',1),
                 ('c2','child','other','b.py','before-b','after-b','','agent',2);",
        )
        .unwrap();
        let result = rollback_task(&mut conn, "child", "s2", "bad change").unwrap();
        assert_eq!(result.task_status, "reverted");
        assert_eq!(result.rolled_back_changes.len(), 1);
        assert_eq!(result.rolled_back_changes[0].original_change_id, "c1");
        assert_eq!(
            conn.query_row(
                "SELECT hash_before, hash_after, diff FROM change_audit
                 WHERE task_id = 'child' AND id NOT IN ('c1','c2')",
                [],
                |row| {
                    Ok((
                        row.get::<_, String>(0)?,
                        row.get::<_, String>(1)?,
                        row.get::<_, String>(2)?,
                    ))
                },
            )
            .unwrap(),
            (
                "after-a".to_string(),
                "before-a".to_string(),
                "[ROLLBACK] reason=bad change".to_string()
            )
        );
        assert_eq!(
            conn.query_row(
                "SELECT active_task_id FROM workspaces WHERE id = 1",
                [],
                |row| { row.get::<_, String>(0) }
            )
            .unwrap(),
            ""
        );
    }

    #[test]
    fn capture_diff_dry_run_is_read_only_and_apply_is_atomic() {
        let dir = tempfile::tempdir().unwrap();
        assert!(Command::new("git")
            .args(["init", "-q"])
            .current_dir(dir.path())
            .status()
            .unwrap()
            .success());
        std::fs::write(dir.path().join("new.py"), "print('new')\n").unwrap();
        let mut conn = fixture();
        conn.execute(
            "UPDATE workspaces SET root_path = ?1 WHERE id = 1",
            params![dir.path().to_string_lossy().to_string()],
        )
        .unwrap();

        let dry = capture_task_diff(&mut conn, "child", "s2", Path::new(""), "", true, "", false)
            .unwrap();
        assert_eq!(dry.changed_files.len(), 1);
        assert_eq!(dry.next_action, "apply");
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM workspace_scan_runs", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap(),
            0
        );

        let applied = capture_task_diff(
            &mut conn,
            "child",
            "s2",
            Path::new(""),
            "",
            false,
            "deadbeef",
            true,
        )
        .unwrap();
        assert_eq!(applied.changed_files.len(), 1);
        assert_eq!(applied.linked_symbols, 1);
        assert_eq!(applied.next_action, "review");
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM change_audit", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap(),
            1
        );
        assert_eq!(
            conn.query_row("SELECT COUNT(*) FROM task_symbol_changes", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap(),
            1
        );

        let before = conn
            .query_row("SELECT COUNT(*) FROM change_audit", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap();
        assert!(capture_task_diff(
            &mut conn,
            "child",
            "wrong-step",
            Path::new(""),
            "",
            false,
            "",
            true,
        )
        .is_err());
        let after = conn
            .query_row("SELECT COUNT(*) FROM change_audit", [], |row| {
                row.get::<_, i64>(0)
            })
            .unwrap();
        assert_eq!(before, after);
    }

    #[test]
    fn porcelain_parser_preserves_leading_status_space_and_path() {
        assert_eq!(
            parse_git_porcelain_line(" M tracked.py"),
            Some(("tracked.py".to_string(), "worktree-M".to_string()))
        );
        assert_eq!(
            parse_git_porcelain_line("?? new.py"),
            Some(("new.py".to_string(), "untracked".to_string()))
        );
        assert_eq!(
            parse_git_porcelain_line("RM old.py -> renamed.py"),
            Some(("renamed.py".to_string(), "staged+worktree".to_string()))
        );
    }

    #[test]
    fn completion_review_is_scoped_and_unknown_severity_fails_closed() {
        let conn = fixture();
        let review = review_task_completion(&conn, "child", "s2").unwrap();
        assert_eq!(review.decision, "block");
        assert_eq!(review.counts.block, 1);
        assert_eq!(review.counts.total(), 1);
        assert!(review_task_completion(&conn, "child", "s1").is_err());

        conn.execute(
            "UPDATE task_quality_findings SET severity = 'critical' WHERE id = 1",
            [],
        )
        .unwrap();
        assert!(review_task_completion(&conn, "child", "s2").is_err());
    }

    #[test]
    fn finding_resolution_is_atomic_and_replay_fails_closed() {
        let mut conn = fixture();
        let result = resolve_task_finding(&mut conn, 1, "false_positive", "reviewer").unwrap();
        assert_eq!(result.status, "wontfix");
        assert_eq!(result.resolved_by, "reviewer");
        assert!(resolve_task_finding(&mut conn, 1, "fixed", "reviewer").is_err());

        conn.execute(
            "INSERT INTO task_quality_findings VALUES
             (2,1,'child','s2','scope','warn','open','warn','','scope',1003,NULL,'')",
            [],
        )
        .unwrap();
        assert!(resolve_task_finding(&mut conn, 2, "invented", "reviewer").is_err());
        assert_eq!(
            conn.query_row(
                "SELECT status FROM task_quality_findings WHERE id = 2",
                [],
                |row| row.get::<_, String>(0)
            )
            .unwrap(),
            "open"
        );
    }

    #[test]
    fn apply_rejects_self_review_and_blocking_findings() {
        let mut conn = fixture();
        assert!(apply_task(&mut conn, "child", "agent").is_err());
        assert!(apply_task(&mut conn, "child", "external-reviewer").is_err());
        assert_eq!(
            conn.query_row("SELECT status FROM tasks WHERE id = 'child'", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
            "review"
        );
    }

    #[test]
    fn last_child_apply_cascades_atomically_to_parent() {
        let mut conn = fixture();
        resolve_task_finding(&mut conn, 1, "fixed", "fixer").unwrap();
        let result = apply_task(&mut conn, "child", "external-reviewer").unwrap();
        assert_eq!(
            result.cascaded_close,
            vec!["child".to_string(), "root".to_string()]
        );
        let statuses = conn
            .prepare("SELECT id, status FROM tasks ORDER BY id")
            .unwrap()
            .query_map([], |row| {
                Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
            })
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(
            statuses,
            vec![
                ("child".to_string(), "closed".to_string()),
                ("root".to_string(), "closed".to_string())
            ]
        );
    }

    #[test]
    fn close_requires_independent_reviewer_and_clears_active_task() {
        let mut conn = fixture();
        conn.execute(
            "INSERT INTO tasks VALUES
             ('leaf','Leaf','','builder','applied',2,2,2,NULL,'',0,1)",
            [],
        )
        .unwrap();
        conn.execute("UPDATE workspaces SET active_task_id = 'leaf'", [])
            .unwrap();
        assert!(close_task(&mut conn, "leaf", "builder").is_err());
        let result = close_task(&mut conn, "leaf", "reviewer").unwrap();
        assert_eq!(result.status, "closed");
        assert_eq!(
            conn.query_row("SELECT active_task_id FROM workspaces", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
            ""
        );
    }

    #[test]
    fn split_plan_is_atomic_and_preserves_order() {
        let mut conn = fixture();
        let plan = r#"
## Parser
Move parser logic.
- edit @ src/parser.rs

```text
## ignored
- ignored
```

## Tests ##
- test: tests/test_parser.py
"#;
        let result = split_task_from_plan(&mut conn, "root", plan).unwrap();
        assert_eq!(result.subtasks.len(), 2);
        assert_eq!(result.subtasks[0].title, "Parser");
        assert_eq!(result.subtasks[1].title, "Tests");
        let rows = conn
            .prepare(
                "SELECT t.title, t.depth, t.sort_order, s.action, s.target_file
                 FROM tasks t JOIN task_steps s ON s.task_id = t.id
                 WHERE t.parent_id = 'root' AND t.id != 'child'
                 ORDER BY t.sort_order",
            )
            .unwrap()
            .query_map([], |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, i64>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, String>(3)?,
                    row.get::<_, String>(4)?,
                ))
            })
            .unwrap()
            .collect::<Result<Vec<_>, _>>()
            .unwrap();
        assert_eq!(
            rows,
            vec![
                (
                    "Parser".to_string(),
                    1,
                    1,
                    "edit".to_string(),
                    "src/parser.rs".to_string()
                ),
                (
                    "Tests".to_string(),
                    1,
                    2,
                    "test".to_string(),
                    "tests/test_parser.py".to_string()
                )
            ]
        );
    }

    #[test]
    fn split_rolls_back_all_children_when_step_insert_fails() {
        let mut conn = fixture();
        let before = conn
            .query_row("SELECT COUNT(*) FROM tasks", [], |row| row.get::<_, i64>(0))
            .unwrap();
        conn.execute("DROP TABLE task_steps", []).unwrap();
        assert!(split_task_from_plan(&mut conn, "root", "## Child\n- edit @ a.py").is_err());
        let after = conn
            .query_row("SELECT COUNT(*) FROM tasks", [], |row| row.get::<_, i64>(0))
            .unwrap();
        assert_eq!(before, after);
    }

    #[test]
    fn concurrent_apply_allows_exactly_one_reviewer_commit() {
        use std::sync::{Arc, Barrier};
        use std::thread;

        let path =
            std::env::temp_dir().join(format!("cw-task-apply-{}.db", generate_id("test").unwrap()));
        {
            let conn = Connection::open(&path).unwrap();
            conn.execute_batch(
                "CREATE TABLE workspaces(
                     id INTEGER PRIMARY KEY, is_active INTEGER, active_task_id TEXT
                 );
                 CREATE TABLE tasks(
                     id TEXT PRIMARY KEY, title TEXT, description TEXT, creator TEXT,
                     status TEXT, created_at REAL, updated_at REAL, applied_at REAL,
                     closed_at REAL, parent_id TEXT, depth INTEGER, sort_order INTEGER
                 );
                 CREATE TABLE task_steps(
                     id TEXT PRIMARY KEY, task_id TEXT, step_index INTEGER, action TEXT,
                     target_file TEXT, target_symbol TEXT, check_items TEXT, status TEXT,
                     result TEXT, created_at REAL, completed_at REAL
                 );
                 CREATE TABLE task_quality_findings(
                     id INTEGER PRIMARY KEY, task_id TEXT, status TEXT, severity TEXT
                 );
                 INSERT INTO workspaces VALUES (1,1,'');
                 INSERT INTO tasks VALUES
                     ('leaf','Leaf','','builder','review',1,1,NULL,NULL,'',0,0);",
            )
            .unwrap();
        }
        let barrier = Arc::new(Barrier::new(3));
        let handles = (0..2)
            .map(|index| {
                let path = path.clone();
                let barrier = Arc::clone(&barrier);
                thread::spawn(move || {
                    let mut conn = Connection::open(path).unwrap();
                    conn.busy_timeout(std::time::Duration::from_secs(5))
                        .unwrap();
                    barrier.wait();
                    apply_task(&mut conn, "leaf", &format!("reviewer-{index}")).is_ok()
                })
            })
            .collect::<Vec<_>>();
        barrier.wait();
        let applied = handles
            .into_iter()
            .map(|handle| handle.join().unwrap())
            .filter(|success| *success)
            .count();
        assert_eq!(applied, 1);
        let conn = Connection::open(&path).unwrap();
        assert_eq!(
            conn.query_row("SELECT status FROM tasks WHERE id = 'leaf'", [], |row| {
                row.get::<_, String>(0)
            })
            .unwrap(),
            "applied"
        );
        drop(conn);
        std::fs::remove_file(path).unwrap();
    }
}
