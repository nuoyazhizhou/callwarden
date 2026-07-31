//! Rust `cw task` 只读查询。
//!
//! 任务编排数据属于当前 UID 的本地数据库，不随代码查询路由切换到 enterprise
//! daemon。这样同一用户在 local/auto/enterprise 模式下看到的是同一份任务事实。

use std::collections::{HashMap, HashSet};

use rusqlite::{params, Connection, OptionalExtension};

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
            "CREATE TABLE tasks(
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
                 (1,1,'child','s2','scope','block','open','outside scope','','scope',1002,NULL,'');",
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
}
