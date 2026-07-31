//! Rust `cw task` 只读查询。
//!
//! 任务编排数据属于当前 UID 的本地数据库，不随代码查询路由切换到 enterprise
//! daemon。这样同一用户在 local/auto/enterprise 模式下看到的是同一份任务事实。

use std::collections::{HashMap, HashSet};
use std::process;
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension, Transaction, TransactionBehavior};
use serde::Deserialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

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
}
