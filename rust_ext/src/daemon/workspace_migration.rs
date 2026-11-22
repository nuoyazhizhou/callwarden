//! workspace 存量一次性迁移（REQ-03 / 卡片 C-04）。
//!
//! # 设计定位
//!
//! 本模块只负责 **task-DB 侧的归属重写**：把同一仓库历史上分裂出的多条
//! `workspaces` 行（主仓 / worktree / `\\?\` verbatim 变体）在任务域收敛到
//! 一条存活行。
//!
//! registry 侧（`daemon_workspaces`）的合并**不在本模块、也不在
//! `workspace.migrate` handler 内**：handler 只调用本函数；registry 行会在
//! 任一 checkout 下一次自然 `workspace.register` 时，因新身份一致而被 C-02 的
//! 收编（`find_adoptable_instance`）与归档（`archive_alias_rows`）机制收敛。
//!
//! # append-only 纪律
//!
//! 迁移只改「归属指针」`workspace_id`，不改写事件 payload，并且**排除**
//! append-only 表：
//! - `audit_chain`：审计链，任何改写都会破坏链式校验；
//! - `workspace_reconciliation_aliases`：跨库对齐别名，历史必须可审计；
//! - `workspace_authority_captures`：capture 只追加（迁移通过**追加**一条
//!   指向存活行的新 capture 完成旧 instance → 存活行的重定向）。
//!
//! `task_workspace_bindings` 是 `task.list` 可见性与 claim 三方一致性的权威来源
//! （`task_collab_query.rs` JOIN 其 `workspace_id`；`claim.rs` 要求
//! `lineage.workspace_id == binding.workspace_id`）。若保持旧值，存量任务在合并后
//! 仍然不可见，且 lineage 被改写后反而与 binding 冲突。因此迁移**同步改写**
//! binding 的 `workspace_id` 与 `workspace_capture_id`（指向为旧 instance 追加的
//! 存活行 capture，保持 FK 主键对一致），并随之改写 `role_contract_lineages` /
//! `task_step_role_contract_bindings`。历史 capture 行本身不改写。
//!
//! # 幂等
//!
//! 重跑时任务行已指向存活 id（`UPDATE ... WHERE workspace_id=<old>` 命中 0 行），
//! 旧 instance 已有指向存活行的 capture（跳过追加），因此报告中的
//! `merged_rows` / `rewired` / `pl06_cleaned` 全部归零。`scanned` / `groups`
//! 属诊断信息，不作为变更量。

use std::collections::{BTreeMap, BTreeSet};
use std::fmt;
use std::path::PathBuf;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use serde_json::{json, Value};

use super::workspace::{compute_workspace_instance_id, normalize_identity_root, sha256_16};

/// 一条待迁移的 checkout 自述（来自 CLI 对 workspace.list 逐 root 的本地解析）。
#[derive(Debug, Clone, Default)]
pub struct MigrationEntry {
    /// checkout 工作树绝对路径（可含 `\\?\` / 反斜杠 / 尾斜杠等历史写法）。
    pub root_path: String,
    /// `git config --get remote.origin.url` 原值；空串表示无 remote。
    pub git_remote_url: String,
    /// 主仓绝对路径（worktree 场景）；空串表示未提供/解析失败。
    pub git_main_root: String,
}

/// 迁移对账报告。
#[derive(Debug, Clone, Default)]
pub struct MigrationReport {
    /// 输入 entry（checkout 自述）条数，不是 task-DB workspaces 行数。
    pub scanned: usize,
    /// 输入 entry 派生出的身份组总数（含未发生合并的组）。
    pub total_groups: usize,
    /// 实际发生合并的身份组数（至少一行被 rewire/重定向）。
    pub groups: usize,
    pub merged_rows: usize,
    /// 表名 -> 实际回写行数（仅记录非零项）。
    pub rewired: BTreeMap<String, usize>,
    /// `principal=X keep=owner drop=agent` 形式的冲突明细。
    pub grant_conflicts: Vec<String>,
    /// PL-06：同组内因路径写法差异（`\\?\` / 斜杠 / 大小写）被合并掉的行数。
    pub pl06_cleaned: usize,
    /// 迁移前备份文件路径；dry_run 不备份，为空串。
    pub backup_path: String,
    pub dry_run: bool,
}

impl MigrationReport {
    /// RPC 序列化口径（C-05 直接包进 `{"report": ...}`）。
    pub fn to_json(&self) -> Value {
        json!({
            "scanned": self.scanned,
            "total_groups": self.total_groups,
            "groups": self.groups,
            "merged_rows": self.merged_rows,
            "rewired": self.rewired,
            "grant_conflicts": self.grant_conflicts,
            "pl06_cleaned": self.pl06_cleaned,
            "backup_path": self.backup_path,
            "dry_run": self.dry_run,
        })
    }
}

/// 迁移失败面。变体与失败点一一对应（C-04 出参契约）。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MigrationError {
    /// SQL 失败：事务整体回滚。
    Db(String),
    /// 备份失败：中止，不进入迁移。
    BackupFailed(String),
    /// 身份派生失败（理论不可达，防御）。
    IdentityCalc(String),
}

impl fmt::Display for MigrationError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            MigrationError::Db(e) => write!(f, "migration db error: {e}"),
            MigrationError::BackupFailed(p) => write!(f, "migration backup failed: {p}"),
            MigrationError::IdentityCalc(r) => write!(f, "migration identity calc failed: {r}"),
        }
    }
}

impl std::error::Error for MigrationError {}

fn db_err(e: rusqlite::Error) -> MigrationError {
    MigrationError::Db(e.to_string())
}

/// 任务域中允许回写 `workspace_id` 的表（按 schema.py 任务域段 + Rust 扩展表）。
///
/// 只改归属列，不改 payload；缺表/缺列在运行时用 `table_has_workspace_id` 跳过，
/// 因此旧库（未升级到最新 schema）也能安全执行。
const TASK_DOMAIN_TABLES: &[&str] = &[
    "task_operation_ledger",
    "task_contract_revisions",
    "task_role_view_events",
    "task_verdict_events",
    "task_evidence_events",
    "task_gate_decisions",
    "task_dependencies",
    "artifact_identities",
    "interface_identities",
    "interface_provider_selections",
    "dependency_edges",
    "action_identities",
    "attestation_records",
    "attestation_revocation_records",
    "task_assignments",
    "task_leases",
    "task_lease_events",
    "task_loop_capability_promotion_events",
    "role_contract_lineages",
    "task_supersede_relations",
    "task_supersede_events",
    "role_runtime_provenance",
    // v56：step → Role Contract binding。必须与 task_workspace_bindings 同步迁移，
    // 否则其 FK 目标 (task_id, workspace_id) 与 lineage 三方一致性校验会分裂。
    "task_step_role_contract_bindings",
];

/// 表是否存在且含指定列。
fn table_has_column(conn: &Connection, table: &str, column: &str) -> Result<bool, MigrationError> {
    let sql = format!("PRAGMA table_info({})", table);
    let mut stmt = conn.prepare(&sql).map_err(db_err)?;
    let mut rows = stmt.query([]).map_err(db_err)?;
    while let Some(row) = rows.next().map_err(db_err)? {
        let name: String = row.get(1).map_err(db_err)?;
        if name == column {
            return Ok(true);
        }
    }
    Ok(false)
}

/// 表是否存在且含 `workspace_id` 列。
fn table_has_workspace_id(conn: &Connection, table: &str) -> Result<bool, MigrationError> {
    table_has_column(conn, table, "workspace_id")
}

fn now_nanos() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0)
}

/// 取 `PRAGMA database_list` 中 main 库的文件路径（内存库返回 None）。
fn main_db_path(conn: &Connection) -> Option<PathBuf> {
    let mut stmt = conn.prepare("PRAGMA database_list").ok()?;
    let mut rows = stmt.query([]).ok()?;
    while let Some(row) = rows.next().ok()? {
        let name: String = row.get(1).ok()?;
        let file: String = row.get(2).ok()?;
        if name == "main" && !file.is_empty() {
            return Some(PathBuf::from(file));
        }
    }
    None
}

/// 备份 task-DB（`VACUUM INTO`，备份先行）。
fn backup_task_db(conn: &Connection) -> Result<String, MigrationError> {
    let base_dir = main_db_path(conn)
        .and_then(|p| p.parent().map(|d| d.to_path_buf()))
        .unwrap_or_else(std::env::temp_dir);
    let target = base_dir.join(format!(
        "pre-ws-migrate-{}-{}.db",
        now_nanos(),
        std::process::id()
    ));
    let target_str = target.to_string_lossy().to_string();
    // VACUUM INTO 不支持绑定参数；路径中的单引号按 SQL 字面量转义。
    let escaped = target_str.replace('\'', "''");
    conn.execute_batch(&format!("VACUUM INTO '{}'", escaped))
        .map_err(|e| {
            // 失败时清掉半成品文件，避免下次 VACUUM INTO 因文件已存在而失败。
            let _ = std::fs::remove_file(&target);
            MigrationError::BackupFailed(format!("{target_str}: {e}"))
        })?;
    Ok(target_str)
}

/// 逗号分隔的占位符（`?1,?2,...`）。
fn placeholders(n: usize) -> String {
    (0..n)
        .map(|i| format!("?{}", i + 1))
        .collect::<Vec<_>>()
        .join(",")
}

fn role_rank(role: &str) -> u8 {
    match role {
        "owner" => 4,
        "governance" => 3,
        "agent" => 2,
        "readonly" => 1,
        _ => 0,
    }
}

/// 迁移存量 workspace 身份（task-DB 侧）。
///
/// 详见模块文档：分组 → 选存活行（最早 `created_at`，平局取最小 id）→
/// 任务域 `workspace_id` 回写 → grants 冲突保最高 → 追加 capture 重定向
/// 旧 instance。`dry_run=true` 在同一事务内执行后回滚，零持久化副作用。
pub fn migrate_repo_identity(
    conn: &mut Connection,
    owner_uid: &str,
    entries: &[MigrationEntry],
    dry_run: bool,
) -> Result<MigrationReport, MigrationError> {
    let mut report = MigrationReport {
        scanned: entries.len(),
        dry_run,
        ..Default::default()
    };
    if entries.is_empty() {
        return Ok(report);
    }

    let owner: u32 = owner_uid
        .trim()
        .parse()
        .map_err(|_| MigrationError::IdentityCalc(format!("owner_uid 非数字: {owner_uid}")))?;

    // 备份先行（dry_run 不落盘：AC-04 要求 dry-run 零副作用）。
    if !dry_run {
        report.backup_path = backup_task_db(conn)?;
    }

    // 1. 身份分组 + 归一化 root 集合。
    let mut groups: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for entry in entries {
        if entry.root_path.trim().is_empty() {
            return Err(MigrationError::IdentityCalc(
                "entry.root_path 为空".to_string(),
            ));
        }
        let identity = compute_workspace_instance_id(
            owner,
            &entry.root_path,
            &entry.git_remote_url,
            &entry.git_main_root,
        );
        groups
            .entry(identity)
            .or_default()
            .insert(normalize_identity_root(&entry.root_path));
    }
    report.total_groups = groups.len();

    let tx = conn
        .transaction_with_behavior(TransactionBehavior::Immediate)
        .map_err(db_err)?;

    // 2. 读取全部 task-DB workspaces 行（数量级小，Rust 侧统一归一比较）。
    let workspaces: Vec<(i64, String, f64)> = {
        let mut stmt = tx
            .prepare("SELECT id, root_path, created_at FROM workspaces")
            .map_err(db_err)?;
        let rows = stmt
            .query_map([], |r| {
                Ok((
                    r.get::<_, i64>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, f64>(2)?,
                ))
            })
            .map_err(db_err)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(db_err)?
    };

    // 3. 逐组迁移。
    for (_identity, roots) in &groups {
        let mut matched: Vec<(i64, String, f64)> = workspaces
            .iter()
            .filter(|(_, root, _)| roots.contains(&normalize_identity_root(root)))
            .cloned()
            .collect();
        if matched.len() < 2 {
            continue;
        }
        matched.sort_by(|a, b| {
            a.2.partial_cmp(&b.2)
                .unwrap_or(std::cmp::Ordering::Equal)
                .then(a.0.cmp(&b.0))
        });
        let survivor = matched[0].0;
        let old_rows: Vec<(i64, String)> = matched[1..]
            .iter()
            .map(|(id, root, _)| (*id, root.clone()))
            .collect();

        let mut group_changed = false;
        for (old_id, raw_root) in &old_rows {
            let is_verbatim = {
                let trimmed = raw_root.trim();
                trimmed.starts_with("\\\\?\\") || trimmed.starts_with("\\??\\")
            };
            let mut row_changed = false;

            // 3.0 旧 instance → 存活行 capture 重定向（先解析/追加目标 capture，
            // 再据此改写 task_workspace_bindings，保证 (capture_id, workspace_id)
            // 主键对一致；历史 capture 行本身不改写）。
            let (redirects, appended) = redirect_captures(&tx, *old_id, survivor)?;
            if appended > 0 {
                *report
                    .rewired
                    .entry("workspace_authority_captures(append)".to_string())
                    .or_insert(0) += appended;
                row_changed = true;
            }
            let rewired_bindings = rewire_task_bindings(&tx, *old_id, survivor, &redirects)?;
            if rewired_bindings > 0 {
                *report
                    .rewired
                    .entry("task_workspace_bindings".to_string())
                    .or_insert(0) += rewired_bindings;
                row_changed = true;
            }

            // 3.1 任务域表归属回写（排除 append-only 表）。
            for table in TASK_DOMAIN_TABLES {
                if !table_has_workspace_id(&tx, table)? {
                    continue;
                }
                let sql = format!("UPDATE {table} SET workspace_id = ?1 WHERE workspace_id = ?2");
                let affected = tx
                    .execute(&sql, params![survivor, old_id])
                    .map_err(db_err)?;
                if affected > 0 {
                    *report.rewired.entry((*table).to_string()).or_insert(0) += affected;
                    row_changed = true;
                }
            }

            // 3.2 grants：同一 principal 冲突按 owner > governance > agent > readonly。
            if table_has_workspace_id(&tx, "workspace_principal_grants")? {
                let conflicts = rewire_grants(&tx, *old_id, survivor, &mut report.grant_conflicts)?;
                if conflicts > 0 {
                    *report
                        .rewired
                        .entry("workspace_principal_grants".to_string())
                        .or_insert(0) += conflicts;
                    row_changed = true;
                }
            }

            if row_changed {
                // PL-06：`\\?\` verbatim 前缀的重复注册行（普通大小写/分隔符差异
                // 属于同一正常写法的归一，不计入「重复注册」）。重跑无变更时不计。
                if is_verbatim {
                    report.pl06_cleaned += 1;
                }
                report.merged_rows += 1;
                group_changed = true;
            }
        }
        if group_changed {
            report.groups += 1;
        }
    }

    if dry_run {
        tx.rollback().map_err(db_err)?;
    } else {
        tx.commit().map_err(db_err)?;
    }
    Ok(report)
}

/// 回写 `workspace_principal_grants`，返回实际变更行数。
///
/// PK 为 `(workspace_id, principal_id)`：同一 principal 在两行分别持不同角色时，
/// 保最高权限、删低权限，并记录 `principal=X keep=owner drop=agent` 供对账。
fn rewire_grants(
    tx: &rusqlite::Transaction<'_>,
    old_id: i64,
    survivor: i64,
    conflicts: &mut Vec<String>,
) -> Result<usize, MigrationError> {
    let old_grants: Vec<(String, String)> = {
        let mut stmt = tx
            .prepare(
                "SELECT principal_id, role FROM workspace_principal_grants \
                 WHERE CAST(workspace_id AS TEXT) = CAST(?1 AS TEXT)",
            )
            .map_err(db_err)?;
        let rows = stmt
            .query_map(params![old_id], |r| {
                Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?))
            })
            .map_err(db_err)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(db_err)?
    };
    let mut changed = 0usize;
    for (principal, role) in old_grants {
        let existing: Option<String> = tx
            .query_row(
                "SELECT role FROM workspace_principal_grants \
                 WHERE CAST(workspace_id AS TEXT) = CAST(?1 AS TEXT) AND principal_id = ?2",
                params![survivor, &principal],
                |r| r.get(0),
            )
            .optional()
            .map_err(db_err)?;
        match existing {
            None => {
                tx.execute(
                    "UPDATE workspace_principal_grants SET workspace_id = ?1 \
                     WHERE CAST(workspace_id AS TEXT) = CAST(?2 AS TEXT) AND principal_id = ?3",
                    params![survivor, old_id, &principal],
                )
                .map_err(db_err)?;
                changed += 1;
            }
            Some(existing_role) => {
                let old_rank = role_rank(&role);
                let keep_rank = role_rank(&existing_role);
                if old_rank > keep_rank {
                    tx.execute(
                        "UPDATE workspace_principal_grants SET role = ?1 \
                         WHERE CAST(workspace_id AS TEXT) = CAST(?2 AS TEXT) AND principal_id = ?3",
                        params![&role, survivor, &principal],
                    )
                    .map_err(db_err)?;
                    conflicts.push(format!(
                        "principal={principal} keep={role} drop={existing_role}"
                    ));
                } else {
                    conflicts.push(format!(
                        "principal={principal} keep={existing_role} drop={role}"
                    ));
                }
                tx.execute(
                    "DELETE FROM workspace_principal_grants \
                     WHERE CAST(workspace_id AS TEXT) = CAST(?1 AS TEXT) AND principal_id = ?2",
                    params![old_id, &principal],
                )
                .map_err(db_err)?;
                changed += 1;
            }
        }
    }
    Ok(changed)
}

/// 为旧 workspace 行上的每个 instance 追加一条指向存活行的 capture。
///
/// 已存在「instance + 存活行」capture 时跳过追加（幂等），但仍返回该已存在的
/// capture id，供 `task_workspace_bindings` 重指向使用。新 capture 的 revision
/// 取该 instance 当前链尾 +1，`supersedes_capture_id` 指向链尾（不改写旧行）。
///
/// 返回 `(每个 instance 在存活行下的目标 capture id, 实际追加条数)`。
fn redirect_captures(
    tx: &rusqlite::Transaction<'_>,
    old_id: i64,
    survivor: i64,
) -> Result<(Vec<(String, String)>, usize), MigrationError> {
    let instances: Vec<String> = {
        let mut stmt = tx
            .prepare(
                "SELECT DISTINCT workspace_instance_id FROM workspace_authority_captures \
                 WHERE CAST(workspace_id AS TEXT) = CAST(?1 AS TEXT)",
            )
            .map_err(db_err)?;
        let rows = stmt
            .query_map(params![old_id], |r| r.get::<_, String>(0))
            .map_err(db_err)?;
        rows.collect::<Result<Vec<_>, _>>().map_err(db_err)?
    };

    let mut appended = 0usize;
    let mut redirects: Vec<(String, String)> = Vec::new();
    for instance in instances {
        // 已存在「instance + 存活行」capture：幂等跳过追加，取其最新一条作为
        // binding 的目标 capture。
        let existing: Option<String> = tx
            .query_row(
                "SELECT workspace_capture_id FROM workspace_authority_captures \
                 WHERE CAST(workspace_id AS TEXT) = CAST(?1 AS TEXT) \
                   AND workspace_instance_id = ?2 \
                 ORDER BY capture_revision DESC LIMIT 1",
                params![survivor, &instance],
                |r| r.get(0),
            )
            .optional()
            .map_err(db_err)?;
        if let Some(capture_id) = existing {
            redirects.push((instance, capture_id));
            continue;
        }

        // 链尾：没有被任何 capture 通过 supersedes_capture_id 引用的最新一条。
        let tip: Option<(
            String,
            i64,
            i64,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
        )> = tx
            .query_row(
                "SELECT c.workspace_capture_id, c.capture_revision, c.daemon_workspace_id, \
                        c.capture_canonicalization_version, c.capture_canonicalization_rules_hash, \
                        c.registry_identity_payload_json, c.registry_identity_hash, \
                        c.workspace_manifest_payload_json, c.workspace_manifest_hash, \
                        c.client_view_root_hash, c.host_real_root_hash \
                 FROM workspace_authority_captures c \
                 WHERE c.workspace_instance_id = ?1 \
                   AND NOT EXISTS (SELECT 1 FROM workspace_authority_captures x \
                                   WHERE x.supersedes_capture_id = c.workspace_capture_id) \
                 ORDER BY c.capture_revision DESC LIMIT 1",
                params![&instance],
                |r| {
                    Ok((
                        r.get(0)?,
                        r.get(1)?,
                        r.get(2)?,
                        r.get(3)?,
                        r.get(4)?,
                        r.get(5)?,
                        r.get(6)?,
                        r.get(7)?,
                        r.get(8)?,
                        r.get(9)?,
                        r.get(10)?,
                    ))
                },
            )
            .optional()
            .map_err(db_err)?;
        let Some((
            tip_id,
            tip_rev,
            daemon_ws_id,
            c14n_version,
            c14n_rules_hash,
            identity_payload,
            identity_hash,
            manifest_payload,
            manifest_hash,
            client_root_hash,
            host_root_hash,
        )) = tip
        else {
            continue;
        };

        let new_rev = tip_rev + 1;
        let new_id = format!(
            "CAP-MIG-{}",
            sha256_16(&format!("{}|{}|{}", survivor, instance, new_rev))
        );
        tx.execute(
            "INSERT INTO workspace_authority_captures (\
                 workspace_capture_id, workspace_id, capture_revision, supersedes_capture_id, \
                 daemon_workspace_id, workspace_instance_id, capture_canonicalization_version, \
                 capture_canonicalization_rules_hash, registry_identity_payload_json, \
                 registry_identity_hash, workspace_manifest_payload_json, workspace_manifest_hash, \
                 client_view_root_hash, host_real_root_hash, created_by, authoritative_created_at) \
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12,?13,?14,'workspace.migrate', \
                     strftime('%Y-%m-%dT%H:%M:%fZ','now'))",
            params![
                new_id,
                survivor,
                new_rev,
                tip_id,
                daemon_ws_id,
                instance,
                c14n_version,
                c14n_rules_hash,
                identity_payload,
                identity_hash,
                manifest_payload,
                manifest_hash,
                client_root_hash,
                host_root_hash,
            ],
        )
        .map_err(db_err)?;
        appended += 1;
        redirects.push((instance, new_id));
    }
    Ok((redirects, appended))
}

/// 把旧行上的 `task_workspace_bindings` 重指向存活行。
///
/// 同时改写 `workspace_id` 与 `workspace_capture_id`，保证
/// `(workspace_capture_id, workspace_id)` 仍命中同一条 capture（迁移为旧 instance
/// 追加的存活行 capture）。旧库若尚未引入 `workspace_capture_id` 列，则只改
/// `workspace_id`。
fn rewire_task_bindings(
    tx: &rusqlite::Transaction<'_>,
    old_id: i64,
    survivor: i64,
    redirects: &[(String, String)],
) -> Result<usize, MigrationError> {
    if !table_has_workspace_id(tx, "task_workspace_bindings")? {
        return Ok(0);
    }
    let has_capture_col = table_has_column(tx, "task_workspace_bindings", "workspace_capture_id")?;
    let mut affected = 0usize;
    for (instance, target_capture_id) in redirects {
        let sql = if has_capture_col {
            "UPDATE task_workspace_bindings \
                SET workspace_id = ?1, workspace_capture_id = ?2 \
              WHERE workspace_id = ?3 \
                AND workspace_capture_id IN (\
                    SELECT workspace_capture_id FROM workspace_authority_captures \
                     WHERE workspace_instance_id = ?4)"
        } else {
            "UPDATE task_workspace_bindings \
                SET workspace_id = ?1 \
              WHERE workspace_id = ?3 \
                AND task_id IN (\
                    SELECT b.task_id FROM task_workspace_bindings b \
                     JOIN workspace_authority_captures c \
                       ON c.workspace_capture_id = b.workspace_capture_id \
                     WHERE b.workspace_id = ?3 AND c.workspace_instance_id = ?4)"
        };
        let params_vec: Vec<&dyn rusqlite::ToSql> = if has_capture_col {
            vec![&survivor, target_capture_id, &old_id, instance]
        } else {
            vec![&survivor, &survivor, &old_id, instance]
        };
        affected += tx.execute(sql, params_vec.as_slice()).map_err(db_err)?;
    }
    Ok(affected)
}

// ---------------------------------------------------------------------------
// 测试
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn ddl() -> &'static str {
        r#"
        CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT UNIQUE NOT NULL,
            root_path TEXT UNIQUE NOT NULL,
            created_at REAL NOT NULL
        );
        CREATE TABLE workspace_authority_captures (
            workspace_capture_id TEXT PRIMARY KEY,
            workspace_id INTEGER NOT NULL,
            capture_revision INTEGER NOT NULL,
            supersedes_capture_id TEXT NULL,
            daemon_workspace_id INTEGER NOT NULL,
            workspace_instance_id TEXT NOT NULL,
            capture_canonicalization_version TEXT NOT NULL,
            capture_canonicalization_rules_hash TEXT NOT NULL,
            registry_identity_payload_json TEXT NOT NULL,
            registry_identity_hash TEXT NOT NULL,
            workspace_manifest_payload_json TEXT NOT NULL,
            workspace_manifest_hash TEXT NOT NULL,
            client_view_root_hash TEXT NOT NULL,
            host_real_root_hash TEXT NOT NULL,
            created_by TEXT NOT NULL,
            authoritative_created_at TEXT NOT NULL,
            UNIQUE(workspace_id, workspace_instance_id, registry_identity_hash, capture_revision),
            UNIQUE(supersedes_capture_id),
            UNIQUE(workspace_capture_id, workspace_id)
        );
        CREATE TABLE workspace_principal_grants (
            workspace_id INTEGER NOT NULL,
            principal_id TEXT NOT NULL,
            role TEXT NOT NULL,
            granted_at REAL NOT NULL,
            granted_by TEXT NOT NULL DEFAULT '',
            revoked_at REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (workspace_id, principal_id)
        );
        CREATE TABLE workspace_reconciliation_aliases (
            alias_id TEXT PRIMARY KEY,
            workspace_id INTEGER NOT NULL
        );
        CREATE TABLE audit_chain (
            record_id TEXT PRIMARY KEY,
            workspace_id INTEGER
        );
        CREATE TABLE task_leases (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            lease_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE task_assignments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            assignment_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE task_evidence_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER,
            evidence_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE action_identities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workspace_id INTEGER NOT NULL,
            action_id TEXT NOT NULL UNIQUE
        );
        CREATE TABLE role_runtime_provenance (
            event_id TEXT PRIMARY KEY,
            workspace_id INTEGER NOT NULL
        );
        CREATE TABLE task_workspace_bindings (
            task_id TEXT PRIMARY KEY,
            workspace_id INTEGER NOT NULL,
            workspace_capture_id TEXT NOT NULL
        );
        CREATE TABLE task_step_role_contract_bindings (
            workspace_id INTEGER NOT NULL,
            task_id TEXT NOT NULL,
            step_id TEXT NOT NULL,
            binding_revision INTEGER NOT NULL,
            PRIMARY KEY (workspace_id, task_id, step_id, binding_revision)
        );
        CREATE TABLE role_contract_lineages (
            task_id TEXT NOT NULL,
            workspace_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            PRIMARY KEY (task_id, workspace_id, role)
        );
        "#
    }

    fn seed_capture(conn: &Connection, ws_id: i64, instance: &str) {
        conn.execute(
            "INSERT INTO workspace_authority_captures VALUES (\
             ?1,?2,1,NULL,?2,?3,'v1','rules','{}','hash',\
             '{}','mhash','croot','hroot','test','2026-11-22T00:00:00Z')",
            params![format!("CAP-{ws_id}"), ws_id, instance],
        )
        .unwrap();
    }

    fn seed() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(ddl()).unwrap();
        let rows = [
            (1i64, "main", "C:\\repo", 1.0f64),
            (2, "wt-a", "C:\\repo-wt-a", 2.0),
            (3, "wt-b", "C:\\repo-wt-b", 3.0),
            (4, "variant", "\\\\?\\C:\\repo", 4.0),
        ];
        for (id, name, root, ts) in rows {
            conn.execute(
                "INSERT INTO workspaces (id,name,root_path,created_at) VALUES (?1,?2,?3,?4)",
                params![id, name, root, ts],
            )
            .unwrap();
        }
        for (id, inst) in [(1, "inst-1"), (2, "inst-2"), (3, "inst-3"), (4, "inst-4")] {
            seed_capture(&conn, id, inst);
        }
        for ws in [1i64, 2, 3] {
            conn.execute(
                "INSERT INTO task_leases (workspace_id,lease_id) VALUES (?1,?2)",
                params![ws, format!("L-{ws}")],
            )
            .unwrap();
        }
        conn.execute(
            "INSERT INTO task_evidence_events (workspace_id,evidence_id) VALUES (2,'E-2')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO action_identities (workspace_id,action_id) VALUES (3,'A-3')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO role_runtime_provenance (event_id,workspace_id) VALUES ('P-4',4)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspace_principal_grants (workspace_id,principal_id,role,granted_at) \
             VALUES (1,'pr_x','owner',0),(2,'pr_x','agent',0),(3,'pr_y','agent',0)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspace_reconciliation_aliases VALUES ('alias-1',2)",
            [],
        )
        .unwrap();
        conn.execute("INSERT INTO audit_chain VALUES ('audit-1',2)", [])
            .unwrap();
        conn.execute(
            "INSERT INTO task_workspace_bindings VALUES ('T-1',2,'CAP-2')",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO task_step_role_contract_bindings VALUES (2,'T-1','S-1',1)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO role_contract_lineages VALUES ('T-1',2,'executor')",
            [],
        )
        .unwrap();
        conn
    }

    fn entries() -> Vec<MigrationEntry> {
        [
            "C:\\repo",
            "C:\\repo-wt-a",
            "C:\\repo-wt-b",
            "\\\\?\\C:\\repo",
        ]
        .iter()
        .map(|root| MigrationEntry {
            root_path: (*root).to_string(),
            git_remote_url: "https://github.com/u/r.git".to_string(),
            git_main_root: "C:\\repo".to_string(),
        })
        .collect()
    }

    fn count(conn: &Connection, sql: &str) -> i64 {
        conn.query_row(sql, [], |r| r.get(0)).unwrap()
    }

    #[test]
    fn test_migrate_merges_task_rows_and_redirects_captures() {
        let mut conn = seed();
        let report = migrate_repo_identity(&mut conn, "1000", &entries(), false).unwrap();

        assert_eq!(report.scanned, 4);
        assert_eq!(report.total_groups, 1);
        assert_eq!(report.groups, 1);
        assert_eq!(report.merged_rows, 3);
        assert_eq!(report.pl06_cleaned, 1);
        assert!(!report.backup_path.is_empty());
        assert!(std::path::Path::new(&report.backup_path).exists());
        assert_eq!(report.rewired.get("task_leases").copied(), Some(2));

        // 任务域全部收敛到最早注册行 id=1。
        assert_eq!(
            count(
                &conn,
                "SELECT COUNT(*) FROM task_leases WHERE workspace_id=1"
            ),
            3
        );
        assert_eq!(
            count(
                &conn,
                "SELECT COUNT(*) FROM task_evidence_events WHERE workspace_id=1"
            ),
            1
        );
        assert_eq!(
            count(
                &conn,
                "SELECT COUNT(*) FROM action_identities WHERE workspace_id=1"
            ),
            1
        );
        assert_eq!(
            count(
                &conn,
                "SELECT COUNT(*) FROM role_runtime_provenance WHERE workspace_id=1"
            ),
            1
        );

        // append-only / 不可变表保持原值。
        assert_eq!(
            count(
                &conn,
                "SELECT workspace_id FROM audit_chain WHERE record_id='audit-1'"
            ),
            2
        );
        assert_eq!(count(&conn, "SELECT workspace_id FROM workspace_reconciliation_aliases WHERE alias_id='alias-1'"), 2);

        // task_workspace_bindings 必须与存活行及重定向 capture 同步，否则存量
        // 任务在合并后不可见、且与 lineage 三方一致性校验冲突。
        assert_eq!(
            count(
                &conn,
                "SELECT workspace_id FROM task_workspace_bindings WHERE task_id='T-1'"
            ),
            1
        );
        let bound_capture: String = conn
            .query_row(
                "SELECT workspace_capture_id FROM task_workspace_bindings WHERE task_id='T-1'",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_ne!(bound_capture, "CAP-2");
        assert_eq!(
            count(
                &conn,
                &format!(
                    "SELECT workspace_id FROM workspace_authority_captures \
                     WHERE workspace_capture_id='{bound_capture}'"
                )
            ),
            1
        );
        assert_eq!(
            count(
                &conn,
                "SELECT workspace_id FROM task_step_role_contract_bindings WHERE task_id='T-1'"
            ),
            1
        );
        assert_eq!(
            count(
                &conn,
                "SELECT workspace_id FROM role_contract_lineages WHERE task_id='T-1'"
            ),
            1
        );
        assert_eq!(
            report.rewired.get("task_workspace_bindings").copied(),
            Some(1)
        );

        // 旧 instance 各追加一条指向存活行的 capture，历史 capture 不改写。
        assert_eq!(
            count(
                &conn,
                "SELECT COUNT(*) FROM workspace_authority_captures WHERE workspace_id=1"
            ),
            4
        );
        assert_eq!(count(&conn, "SELECT capture_revision FROM workspace_authority_captures WHERE workspace_instance_id='inst-2' AND workspace_id=1"), 2);
        assert_eq!(count(&conn, "SELECT capture_revision FROM workspace_authority_captures WHERE workspace_instance_id='inst-1'"), 1);

        // grants：pr_x 保 owner、pr_y 迁入；冲突明细可查。
        assert_eq!(
            count(
                &conn,
                "SELECT COUNT(*) FROM workspace_principal_grants WHERE workspace_id=1"
            ),
            2
        );
        assert_eq!(count(&conn, "SELECT COUNT(*) FROM workspace_principal_grants WHERE principal_id='pr_x' AND role='owner'"), 1);
        assert!(report
            .grant_conflicts
            .iter()
            .any(|c| c == "principal=pr_x keep=owner drop=agent"));

        let _ = std::fs::remove_file(&report.backup_path);
    }

    #[test]
    fn test_migrate_is_idempotent() {
        let mut conn = seed();
        migrate_repo_identity(&mut conn, "1000", &entries(), false).unwrap();
        let captures = count(&conn, "SELECT COUNT(*) FROM workspace_authority_captures");
        let grants = count(&conn, "SELECT COUNT(*) FROM workspace_principal_grants");

        let report = migrate_repo_identity(&mut conn, "1000", &entries(), false).unwrap();
        assert_eq!(report.merged_rows, 0);
        assert_eq!(report.pl06_cleaned, 0);
        assert!(report.rewired.is_empty());
        assert!(report.grant_conflicts.is_empty());
        assert_eq!(
            count(&conn, "SELECT COUNT(*) FROM workspace_authority_captures"),
            captures
        );
        assert_eq!(
            count(&conn, "SELECT COUNT(*) FROM workspace_principal_grants"),
            grants
        );

        let _ = std::fs::remove_file(&report.backup_path);
    }

    #[test]
    fn test_dry_run_has_no_persistent_side_effects() {
        let mut conn = seed();
        let before: Vec<i64> = [
            "workspaces",
            "workspace_authority_captures",
            "workspace_principal_grants",
            "task_leases",
        ]
        .iter()
        .map(|t| count(&conn, &format!("SELECT COUNT(*) FROM {t}")))
        .collect();
        let ws_before: String = conn
            .query_row(
                "SELECT GROUP_CONCAT(workspace_id) FROM task_leases",
                [],
                |r| r.get(0),
            )
            .unwrap();

        let report = migrate_repo_identity(&mut conn, "1000", &entries(), true).unwrap();
        assert!(report.dry_run);
        assert_eq!(report.scanned, 4);
        assert_eq!(report.total_groups, 1);
        assert_eq!(report.merged_rows, 3);
        assert_eq!(report.groups, 1);
        assert!(report.backup_path.is_empty());

        let after: Vec<i64> = [
            "workspaces",
            "workspace_authority_captures",
            "workspace_principal_grants",
            "task_leases",
        ]
        .iter()
        .map(|t| count(&conn, &format!("SELECT COUNT(*) FROM {t}")))
        .collect();
        assert_eq!(before, after);
        let ws_after: String = conn
            .query_row(
                "SELECT GROUP_CONCAT(workspace_id) FROM task_leases",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(ws_after, ws_before);
    }

    #[test]
    fn test_different_remotes_keep_distinct_roots_apart() {
        let mut conn = seed();
        let mut es = entries();
        for (i, e) in es.iter_mut().enumerate() {
            e.git_remote_url = format!("https://github.com/u/repo-{i}.git");
        }
        let report = migrate_repo_identity(&mut conn, "1000", &es, true).unwrap();
        // 只有归一化 root 相同的重复注册行（`C:\repo` + `\\?\C:\repo`）被合并；
        // 不同 remote 的 worktree（root 各不相同）保持独立。
        assert_eq!(report.merged_rows, 1);
        // 4 个不同 remote = 4 个身份组，只有 1 组发生合并。
        assert_eq!(report.total_groups, 4);
        assert_eq!(report.groups, 1);
        assert_eq!(
            count(
                &conn,
                "SELECT COUNT(*) FROM task_leases WHERE workspace_id=2"
            ),
            1
        );
        assert_eq!(
            count(
                &conn,
                "SELECT COUNT(*) FROM task_leases WHERE workspace_id=3"
            ),
            1
        );
    }
}
