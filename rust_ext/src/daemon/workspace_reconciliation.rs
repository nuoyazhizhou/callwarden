//! Workspace Authority Reconciliation（T-1788346430756-8c900ec0）。
//!
//! 修复迁移期分裂：registry.db `daemon_workspaces` 用自增 `workspace_id`（如 144），
//! 而 task-DB `callwarden.db` 的 `workspaces`/`workspace_authority_captures` 用另一套数字
//! 主键（如 1/10）。两者通过稳定身份键 `workspace_instance_id` 关联。
//!
//! 设计纪律（合约 §3/§8）：
//! - instance id 是稳定身份；numeric id 在两个 DB 间允许不同，必须经由 **append-only
//!   别名** 显式关联。
//! - 绝不改写历史 binding / capture / task event；只 append-only 写 `workspace_reconciliation_aliases`。
//! - 禁止合成 `ws-{id}`；空 instance → `E_TASK_WORKSPACE_INSTANCE_REQUIRED`。

use rusqlite::{params, Connection, OptionalExtension, Transaction};

use crate::canonicalize::sha256_hex;
use crate::daemon::dispatch::DaemonRpcError;
use crate::daemon::workspace::WorkspaceRegistry;

/// 任一 registry/capture/local workspace 不一致（§8.1.1 fail-closed）。
pub const ERR_WORKSPACE_AUTHORITY_MISMATCH: &str = "E_WORKSPACE_AUTHORITY_MISMATCH";
/// BR-01：workspace_instance_id 缺失/空白在领域写之前拒绝（禁止合成 ws-{id}）。
pub const ERR_TASK_WORKSPACE_INSTANCE_REQUIRED: &str = "E_TASK_WORKSPACE_INSTANCE_REQUIRED";

/// `workspace_reconciliation_aliases` 建表 DDL（task-DB，append-only）。
pub const RECONCILIATION_TABLE_DDL: &str = "\
CREATE TABLE IF NOT EXISTS workspace_reconciliation_aliases (
    alias_id                 TEXT PRIMARY KEY,
    registry_workspace_id    INTEGER NOT NULL,
    registry_instance_id     TEXT NOT NULL,
    task_db_workspace_id     INTEGER NOT NULL,
    task_db_instance_id      TEXT NOT NULL,
    root_path_hash           TEXT NOT NULL,
    reconciliation_status    TEXT NOT NULL DEFAULT 'active',
    created_by               TEXT NOT NULL,
    authoritative_created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_recon_alias_instance
    ON workspace_reconciliation_aliases(registry_instance_id, task_db_instance_id);";

/// 在 task-DB 上确保 reconciliation 表存在（幂等）。
pub fn ensure_reconciliation_table(conn: &Connection) -> Result<(), DaemonRpcError> {
    conn.execute_batch(RECONCILIATION_TABLE_DDL)
        .map_err(|e| DaemonRpcError::internal_error(format!("建 reconciliation 表失败: {e}")))
}

/// `task.create` 解析后的权威结果。
#[derive(Debug, Clone)]
pub struct ReconciledAuthority {
    /// 用于写入 task-DB binding 的规范（task-DB）数字 id。
    pub task_db_workspace_id: i64,
    /// 稳定的 instance id（透传请求值）。
    pub canonical_instance_id: String,
    /// 请求数字 id 与规范 task-DB id 不同（发生了 registry↔task-DB 对齐）。
    pub reconciled: bool,
    /// 本次是否新增了 append-only 别名记录。
    pub alias_recorded: bool,
}

/// `task.create` 权威解析：把请求 `(requested_workspace_id, requested_instance_id)`
/// 解析为规范的 task-DB 绑定权威。
///
/// 1. 空 instance → `E_TASK_WORKSPACE_INSTANCE_REQUIRED`。
/// 2. 精确 capture `(id, instance)` 命中 → 原样返回（无 reconciliation）。
/// 3. 仅 instance 在 captures 命中（跨任意 task-DB id）→ 规范为该 task-DB id；
///    若请求数字 id 不同 → 记录 append-only 别名（幂等），`reconciled=true`。
/// 4. instance 不在 captures 但 task-DB `workspaces` 含请求 id → 允许（将建首个 capture）。
/// 5. 其它 → `E_WORKSPACE_AUTHORITY_MISMATCH`（不得 invent）。
pub fn resolve_create_authority(
    conn: &Connection,
    requested_workspace_id: i64,
    requested_instance_id: &str,
    created_by: &str,
) -> Result<ReconciledAuthority, DaemonRpcError> {
    let instance = requested_instance_id.trim();
    if instance.is_empty() {
        return Err(DaemonRpcError::new(
            ERR_TASK_WORKSPACE_INSTANCE_REQUIRED,
            "task.create 必须显式传入非空 workspace_instance_id；禁止空实例合成 ws-{id}",
        ));
    }

    // 2. 精确 capture (id, instance)
    let exact: Option<i64> = conn
        .query_row(
            "SELECT workspace_id FROM workspace_authority_captures \
             WHERE workspace_id = ?1 AND workspace_instance_id = ?2 \
             ORDER BY capture_revision DESC LIMIT 1",
            params![requested_workspace_id, instance],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("精确 capture 读取失败: {e}")))?;
    if exact.is_some() {
        return Ok(ReconciledAuthority {
            task_db_workspace_id: requested_workspace_id,
            canonical_instance_id: instance.to_string(),
            reconciled: false,
            alias_recorded: false,
        });
    }

    // 3. instance 在 captures 命中（跨任意 task-DB id）→ 规范为该 task-DB id
    let by_instance: Option<i64> = conn
        .query_row(
            "SELECT workspace_id FROM workspace_authority_captures \
             WHERE workspace_instance_id = ?1 \
             ORDER BY capture_revision DESC LIMIT 1",
            params![instance],
            |r| r.get(0),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("instance capture 读取失败: {e}")))?;
    if let Some(canonical_id) = by_instance {
        let reconciled = canonical_id != requested_workspace_id;
        let mut alias_recorded = false;
        if reconciled {
            alias_recorded =
                record_reconciliation_alias(conn, requested_workspace_id, instance, canonical_id, instance, created_by)?;
        }
        return Ok(ReconciledAuthority {
            task_db_workspace_id: canonical_id,
            canonical_instance_id: instance.to_string(),
            reconciled,
            alias_recorded,
        });
    }

    // 4. instance 未知但 task-DB workspaces 含请求 id → 允许（将建首个 capture）
    let ws_exists: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM workspaces WHERE id = ?1",
            params![requested_workspace_id],
            |r| r.get(0),
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("workspace 存在性校验失败: {e}")))?;
    if ws_exists > 0 {
        return Ok(ReconciledAuthority {
            task_db_workspace_id: requested_workspace_id,
            canonical_instance_id: instance.to_string(),
            reconciled: false,
            alias_recorded: false,
        });
    }

    // 5. 其它 → mismatch
    Err(DaemonRpcError::new(
        ERR_WORKSPACE_AUTHORITY_MISMATCH,
        format!(
            "task-DB 中不存在 workspace_id={} 且其 instance={} 无 capture；\
             不得用客户端 numeric id 补齐（registry/task-DB 分裂需经 alias 对齐）",
            requested_workspace_id, instance
        ),
    ))
}

/// 记录 append-only 别名（registry 数字 id ↔ task-DB 数字 id，按 instance 关联）。
/// 幂等：已存在相同 (registry_instance_id, task_db_instance_id) 则跳过，返回 false。
pub fn record_reconciliation_alias(
    conn: &Connection,
    registry_workspace_id: i64,
    registry_instance_id: &str,
    task_db_workspace_id: i64,
    task_db_instance_id: &str,
    created_by: &str,
) -> Result<bool, DaemonRpcError> {
    let root_path_hash = sha256_hex(registry_instance_id.as_bytes());
    let alias_id = format!(
        "wa-{}-{}",
        registry_instance_id, task_db_instance_id
    );
    let now = now_unix();
    let affected = conn
        .execute(
            "INSERT OR IGNORE INTO workspace_reconciliation_aliases \
             (alias_id, registry_workspace_id, registry_instance_id, task_db_workspace_id, \
              task_db_instance_id, root_path_hash, reconciliation_status, created_by, \
              authoritative_created_at) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active', ?7, ?8)",
            params![
                alias_id,
                registry_workspace_id,
                registry_instance_id,
                task_db_workspace_id,
                task_db_instance_id,
                root_path_hash,
                created_by,
                now,
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("写入 reconciliation alias 失败: {e}")))?;
    Ok(affected > 0)
}

/// 统一权威元组（用于 `workspace.status` / `workspace.list`）。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct UnifiedAuthority {
    pub registry_workspace_id: Option<i64>,
    pub registry_instance_id: Option<String>,
    pub task_db_workspace_id: Option<i64>,
    pub task_db_instance_id: Option<String>,
    pub client_view_root: Option<String>,
    pub status: Option<String>,
}

/// 解析 `workspace.status` 的查询键（instance id 或数字 id，任一 DB 视角），
/// 交叉引用 registry.db 与 task-DB captures/alias，返回统一权威元组。
///
/// - 数字键：先查 registry `get_workspace_by_numeric_id`，再查 task-DB captures 按
///   `workspace_id`，再查 alias 表（registry_id 或 task_db_id 命中）。
/// - instance 键：先查 registry `get_workspace_status`，再查 task-DB captures 按 instance。
pub fn resolve_status_authority(
    conn: &Connection,
    registry: &WorkspaceRegistry,
    key: &str,
) -> Result<Option<UnifiedAuthority>, DaemonRpcError> {
    let is_numeric = key.parse::<i64>().is_ok();
    let mut out = UnifiedAuthority {
        registry_workspace_id: None,
        registry_instance_id: None,
        task_db_workspace_id: None,
        task_db_instance_id: None,
        client_view_root: None,
        status: None,
    };
    let mut found = false;
    // 稳定身份键：解析到的 registry instance（数字键漂移后仍稳定），供下方持久跨引用使用。
    let mut resolved_instance: Option<String> = None;

    if is_numeric {
        let id: i64 = key.parse().unwrap();
        if let Ok(Some(reg)) = registry.get_workspace_by_numeric_id(id) {
            out.registry_workspace_id = reg.get("workspace_id").and_then(|v| v.as_i64());
            let rinst = reg.get("workspace_instance_id").and_then(|v| v.as_str()).map(|s| s.to_string());
            out.registry_instance_id = rinst.clone();
            out.client_view_root = reg.get("client_view_root").and_then(|v| v.as_str()).map(|s| s.to_string());
            out.status = reg.get("status").and_then(|v| v.as_str()).map(|s| s.to_string());
            // 稳定身份键：即便 registry 数字 id 漂移（如 144→156），instance 不变，
            // 后续持久跨引用仍可用它解析 task-DB 侧。
            resolved_instance = rinst;
            found = true;
        }
        // task-DB captures by numeric id（直接）
        if let Some((tid, tinst)) = task_db_capture_by_id(conn, id)? {
            out.task_db_workspace_id = Some(tid);
            out.task_db_instance_id = Some(tinst.clone());
            found = true;
            if resolved_instance.is_none() {
                resolved_instance = Some(tinst);
            }
        }
        // alias：任一数字方向命中（兼容历史 alias 行，其 registry 数字列可能已过期）
        if let Some(a) = alias_by_any_id(conn, id)? {
            if out.registry_workspace_id.is_none() {
                out.registry_workspace_id = Some(a.0);
            }
            if out.task_db_workspace_id.is_none() {
                out.task_db_workspace_id = Some(a.1);
            }
            if out.registry_instance_id.is_none() {
                out.registry_instance_id = Some(a.2.clone());
            }
            if out.task_db_instance_id.is_none() {
                out.task_db_instance_id = Some(a.2);
            }
            found = true;
        }
    } else {
        // instance 键
        if let Ok(Some(reg)) = registry.get_workspace_status(key) {
            out.registry_workspace_id = reg.get("workspace_id").and_then(|v| v.as_i64());
            out.registry_instance_id = Some(key.to_string());
            out.client_view_root = reg.get("client_view_root").and_then(|v| v.as_str()).map(|s| s.to_string());
            out.status = reg.get("status").and_then(|v| v.as_str()).map(|s| s.to_string());
            resolved_instance = Some(key.to_string());
            found = true;
        }
        if let Some((tid, tinst)) = task_db_capture_by_instance(conn, key)? {
            out.task_db_workspace_id = Some(tid);
            out.task_db_instance_id = Some(tinst);
            found = true;
        }
    }

    // === 持久跨引用（本卡核心修复：T-1788382908707-bbdd0cfc）===
    // 只要解析到了 registry 的 instance（数字键或 instance 键），就经稳定 instance 重新解析
    // task-DB 侧：先读持久 `workspace_authority_captures`（重启后仍在），再用按 instance 的 alias
    // 审计行补全。这样重启后 registry 数字 id 漂移也不会让统一视图的 task-DB 侧变 null，且无需
    // 依赖可能过期的 alias 数字列。
    if let Some(inst) = resolved_instance {
        if out.task_db_workspace_id.is_none() {
            if let Some((tid, tinst)) = task_db_capture_by_instance(conn, &inst)? {
                out.task_db_workspace_id = Some(tid);
                out.task_db_instance_id = Some(tinst);
                found = true;
            }
        }
        if out.task_db_workspace_id.is_none() {
            if let Some(a) = alias_by_instance(conn, &inst)? {
                if out.registry_workspace_id.is_none() {
                    out.registry_workspace_id = Some(a.0);
                }
                out.task_db_workspace_id = Some(a.1);
                if out.registry_instance_id.is_none() {
                    out.registry_instance_id = Some(a.2.clone());
                }
                if out.task_db_instance_id.is_none() {
                    out.task_db_instance_id = Some(a.2);
                }
                found = true;
            }
        }
    }

    if found {
        Ok(Some(out))
    } else {
        Ok(None)
    }
}

/// task-DB：按数字 id 取最近 capture 的 (workspace_id, instance)。
fn task_db_capture_by_id(
    conn: &Connection,
    workspace_id: i64,
) -> Result<Option<(i64, String)>, DaemonRpcError> {
    conn.query_row(
        "SELECT workspace_id, workspace_instance_id FROM workspace_authority_captures \
         WHERE workspace_id = ?1 ORDER BY capture_revision DESC LIMIT 1",
        params![workspace_id],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )
    .optional()
    .map_err(|e| DaemonRpcError::internal_error(format!("task-DB capture(id) 读取失败: {e}")))
}

/// task-DB：按 instance 取最近 capture 的 (workspace_id, instance)。
fn task_db_capture_by_instance(
    conn: &Connection,
    instance: &str,
) -> Result<Option<(i64, String)>, DaemonRpcError> {
    conn.query_row(
        "SELECT workspace_id, workspace_instance_id FROM workspace_authority_captures \
         WHERE workspace_instance_id = ?1 ORDER BY capture_revision DESC LIMIT 1",
        params![instance],
        |r| Ok((r.get(0)?, r.get(1)?)),
    )
    .optional()
    .map_err(|e| DaemonRpcError::internal_error(format!("task-DB capture(instance) 读取失败: {e}")))
}

/// task-DB：alias 表按 registry_id 或 task_db_id 命中 → (registry_id, task_db_id, instance)。
fn alias_by_any_id(
    conn: &Connection,
    id: i64,
) -> Result<Option<(i64, i64, String)>, DaemonRpcError> {
    conn.query_row(
        "SELECT registry_workspace_id, task_db_workspace_id, registry_instance_id \
         FROM workspace_reconciliation_aliases \
         WHERE registry_workspace_id = ?1 OR task_db_workspace_id = ?1 \
         ORDER BY rowid DESC LIMIT 1",
        params![id],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )
    .optional()
    .map_err(|e| DaemonRpcError::internal_error(format!("alias 读取失败: {e}")))
}

/// task-DB：alias 表按 instance（registry 或 task-DB 任一侧）命中 →
/// (registry_id, task_db_id, instance)。用于持久跨引用，不受 registry 数字 id 漂移影响。
fn alias_by_instance(
    conn: &Connection,
    instance: &str,
) -> Result<Option<(i64, i64, String)>, DaemonRpcError> {
    conn.query_row(
        "SELECT registry_workspace_id, task_db_workspace_id, registry_instance_id \
         FROM workspace_reconciliation_aliases \
         WHERE registry_instance_id = ?1 OR task_db_instance_id = ?1 \
         ORDER BY rowid DESC LIMIT 1",
        params![instance],
        |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
    )
    .optional()
    .map_err(|e| DaemonRpcError::internal_error(format!("alias(instance) 读取失败: {e}")))
}

/// Unix 时间戳秒（float，兼容 SQLite REAL created_at）。
fn now_unix() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// T-1788595874892-c9b82244：legacy parent capture → canonical child 的**有界**权威
/// 等价证明（parent-aware task.create 事务内调用）。
///
/// 仅当以下全部成立才允许（任一不成立 → `E_WORKSPACE_AUTHORITY_MISMATCH` fail-closed）：
/// 1. parent capture instance 是 legacy 形态 `ws-<digits>`（非 legacy 形态不适用本桥）；
/// 2. parent capture 不可变自洽：`client_view_root_hash == host_real_root_hash` 且非空；
/// 3. requested instance 在 daemon 持久 capture 域存在（registry-owned 权威）；
/// 4. 其数字 workspace_id **唯一**（无跨 workspace 歧义）且与 parent 数字 workspace
///    精确一致（exact numeric workspace rejection 不放松）；
/// 5. canonical 最新 capture 同样不可变自洽；
/// 6. root 溯源一致：两侧 `client_view_root_hash` 逐字节相等；
/// 7. 两侧 daemon registry identity payload 的 `client_view_root_hash` 均等于共享 root
///    （daemon-owned 持久 registry 身份交叉证明同一 project root/owner authority）。
///
/// 成立后在**同一事务** append-only 写 `workspace_reconciliation_aliases` 等价
/// provenance（root_path_hash 记录真实共享 root hash，非合成值）：
/// - 绝不改写历史 binding/capture；
/// - 不把 legacy instance 传播给新任务（child binding 仍写请求的 canonical instance）；
/// - 不放松跨 workspace 检查。
pub(crate) fn prove_parent_canonical_equivalence(
    tx: &Transaction<'_>,
    parent_workspace_id: i64,
    parent_capture_id: &str,
    parent_instance: &str,
    requested_workspace_id: i64,
    requested_instance: &str,
    created_by: &str,
) -> Result<(), DaemonRpcError> {
    let mismatch = |msg: String| {
        DaemonRpcError::new(ERR_WORKSPACE_AUTHORITY_MISMATCH, msg)
    };

    // 1. 仅 legacy 形态 `ws-<digits>` 适用（有界：非 legacy 形态的 instance 差异维持拒绝）。
    let legacy_shape_ok = {
        let body = parent_instance.strip_prefix("ws-").unwrap_or("");
        !body.is_empty() && body.bytes().all(|b| b.is_ascii_digit())
    };
    if !legacy_shape_ok {
        return Err(mismatch(format!(
            "task.create parent-aware：parent task capture instance={parent_instance:?} \
             与 child 请求 instance={requested_instance:?} 不一致，且 parent instance 非 \
             legacy ws-<digits> 形态，legacy→canonical 桥不适用（fail-closed）"
        )));
    }

    // 2. parent capture 不可变自洽。
    let parent_row: Option<(String, String, String)> = tx
        .query_row(
            "SELECT client_view_root_hash, host_real_root_hash, registry_identity_payload_json \
             FROM workspace_authority_captures WHERE workspace_capture_id = ?1",
            params![parent_capture_id],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?)),
        )
        .optional()
        .map_err(|e| DaemonRpcError::internal_error(format!("bridge parent capture 读取失败: {e}")))?;
    let (p_client, p_host, p_identity) = parent_row.ok_or_else(|| {
        mismatch(format!(
            "task.create parent-aware：parent capture {parent_capture_id} 不存在（fail-closed）"
        ))
    })?;
    if p_client.is_empty() || p_client != p_host {
        return Err(mismatch(format!(
            "task.create parent-aware：parent capture {parent_capture_id} 非不可变自洽 \
             (client_root={p_client:?}, host_root={p_host:?})，legacy→canonical 桥拒绝"
        )));
    }

    // 3+4. requested instance 必须在 daemon 持久 capture 域存在，数字 workspace 唯一
    //      且与 parent 精确一致（exact numeric rejection 不放松）。
    let distinct_ws: i64 = tx
        .query_row(
            "SELECT COUNT(DISTINCT workspace_id) FROM workspace_authority_captures \
             WHERE workspace_instance_id = ?1",
            params![requested_instance],
            |r| r.get(0),
        )
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("bridge canonical 跨域统计失败: {e}"))
        })?;
    if distinct_ws != 1 {
        return Err(mismatch(format!(
            "task.create parent-aware：child 请求 instance={requested_instance:?} 在持久 \
             capture 域跨 {distinct_ws} 个数字 workspace（歧义），legacy→canonical 桥拒绝"
        )));
    }
    let canonical_row: Option<(i64, String, String, String)> = tx
        .query_row(
            "SELECT workspace_id, client_view_root_hash, host_real_root_hash, \
                    registry_identity_payload_json \
             FROM workspace_authority_captures WHERE workspace_instance_id = ?1 \
             ORDER BY capture_revision DESC LIMIT 1",
            params![requested_instance],
            |r| Ok((r.get(0)?, r.get(1)?, r.get(2)?, r.get(3)?)),
        )
        .optional()
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("bridge canonical capture 读取失败: {e}"))
        })?;
    let (c_ws, c_client, c_host, c_identity) = canonical_row.ok_or_else(|| {
        mismatch(format!(
            "task.create parent-aware：child 请求 instance={requested_instance:?} 在 daemon \
             持久 capture 域无权威记录，legacy→canonical 桥拒绝（不得 invent）"
        ))
    })?;
    if c_ws != parent_workspace_id || c_ws != requested_workspace_id {
        return Err(mismatch(format!(
            "task.create parent-aware：canonical capture workspace={c_ws} 与 \
             parent={parent_workspace_id}/请求={requested_workspace_id} 数字不一致，\
             exact numeric workspace rejection 不放松"
        )));
    }

    // 5. canonical 最新 capture 不可变自洽。
    if c_client.is_empty() || c_client != c_host {
        return Err(mismatch(format!(
            "task.create parent-aware：canonical capture（instance={requested_instance:?}）\
             非不可变自洽 (client_root={c_client:?}, host_root={c_host:?})，桥拒绝"
        )));
    }

    // 6. root 溯源一致：两侧 client root 逐字节相等。
    if c_client != p_client {
        return Err(mismatch(format!(
            "task.create parent-aware：parent capture root={p_client:?} 与 canonical \
             capture root={c_client:?} 不一致，非同一 project authority，桥拒绝"
        )));
    }

    // 7. daemon registry identity payload 交叉证明（不可信 payload → fail-closed）。
    for (side, identity_json, expect_instance) in [
        ("parent", &p_identity, parent_instance),
        ("canonical", &c_identity, requested_instance),
    ] {
        let payload: serde_json::Value = serde_json::from_str(identity_json).map_err(|e| {
            mismatch(format!(
                "task.create parent-aware：{side} capture registry_identity_payload_json \
                 解析失败（{e}），身份不可证，桥拒绝"
            ))
        })?;
        let id_client = payload.get("client_view_root_hash").and_then(|v| v.as_str());
        let id_instance = payload
            .get("workspace_instance_id")
            .and_then(|v| v.as_str());
        let ok = id_client == Some(p_client.as_str())
            && id_client == Some(c_client.as_str())
            && id_instance == Some(expect_instance);
        if !ok {
            return Err(mismatch(format!(
                "task.create parent-aware：{side} capture registry identity 与共享 root/instance \
                 不符 (client_root={id_client:?}, instance={id_instance:?})，桥拒绝"
            )));
        }
    }

    // 证明成立：同一事务 append-only 写等价 provenance（幂等；不改历史行）。
    let alias_id = format!("wa-{requested_instance}-{parent_instance}");
    tx.execute(
        "INSERT OR IGNORE INTO workspace_reconciliation_aliases \
         (alias_id, registry_workspace_id, registry_instance_id, task_db_workspace_id, \
          task_db_instance_id, root_path_hash, reconciliation_status, created_by, \
          authoritative_created_at) \
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'active', ?7, ?8)",
        params![
            alias_id,
            parent_workspace_id,
            requested_instance,
            parent_workspace_id,
            parent_instance,
            p_client,
            created_by,
            now_unix(),
        ],
    )
    .map_err(|e| {
        DaemonRpcError::internal_error(format!("bridge 等价 provenance 写入失败: {e}"))
    })?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::Connection;

    fn minimal_db() -> Connection {
        let conn = Connection::open_in_memory().unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (id INTEGER PRIMARY KEY);
             CREATE TABLE workspace_authority_captures (
                 workspace_capture_id TEXT, workspace_id INTEGER,
                 workspace_instance_id TEXT, capture_revision INTEGER);
             CREATE TABLE canonicalization_rule_sets (
                 domain TEXT, canonicalization_version TEXT, rules_hash TEXT);",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO canonicalization_rule_sets VALUES ('workspace_capture','workspace-capture-c14n/v1','test-rules')",
            [],
        )
        .unwrap();
        // task-DB 侧 workspace_id=1 绑定 instance 4baea3ff12c2ea5c（历史权威）
        conn.execute(
            "INSERT INTO workspaces (id) VALUES (1)",
            [],
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspace_authority_captures VALUES ('wc-1-a',1,'4baea3ff12c2ea5c',1)",
            [],
        )
        .unwrap();
        // 历史遗留 ws-1 也挂在 workspace_id=1
        conn.execute(
            "INSERT INTO workspace_authority_captures VALUES ('wc-1-b',1,'ws-1',2)",
            [],
        )
        .unwrap();
        ensure_reconciliation_table(&conn).unwrap();
        conn
    }

    #[test]
    fn exact_capture_no_reconciliation() {
        let conn = minimal_db();
        let a = resolve_create_authority(&conn, 1, "4baea3ff12c2ea5c", "test").unwrap();
        assert!(!a.reconciled);
        assert_eq!(a.task_db_workspace_id, 1);
    }

    #[test]
    fn registry_id_divergence_records_alias() {
        // 请求携带 registry 数字 id=144 + 真实 instance；task-DB 侧该 instance 属 workspace_id=1
        let conn = minimal_db();
        let a = resolve_create_authority(&conn, 144, "4baea3ff12c2ea5c", "test").unwrap();
        assert!(a.reconciled);
        assert!(a.alias_recorded);
        assert_eq!(a.task_db_workspace_id, 1);

        // alias 表应记录 144↔1
        let rows: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM workspace_reconciliation_aliases \
                 WHERE registry_workspace_id=144 AND task_db_workspace_id=1",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(rows, 1);

        // 幂等：再次调用不新增 alias
        let a2 = resolve_create_authority(&conn, 144, "4baea3ff12c2ea5c", "test").unwrap();
        assert!(!a2.alias_recorded);
        let rows2: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM workspace_reconciliation_aliases",
                [],
                |r| r.get(0),
            )
            .unwrap();
        assert_eq!(rows2, 1);
    }

    #[test]
    fn empty_instance_rejected() {
        let conn = minimal_db();
        let err = resolve_create_authority(&conn, 1, "   ", "test").unwrap_err();
        assert_eq!(err.code, ERR_TASK_WORKSPACE_INSTANCE_REQUIRED);
    }

    #[test]
    fn unknown_workspace_mismatch() {
        let conn = minimal_db();
        let err = resolve_create_authority(&conn, 999, "no-such-instance", "test").unwrap_err();
        assert_eq!(err.code, ERR_WORKSPACE_AUTHORITY_MISMATCH);
    }

    #[test]
    fn historical_ws1_resolves_to_task_db_1() {
        let conn = minimal_db();
        let a = resolve_create_authority(&conn, 1, "ws-1", "test").unwrap();
        assert!(!a.reconciled);
        assert_eq!(a.task_db_workspace_id, 1);
    }

    /// 核心回归：registry 数字 id 漂移（144→156）后，数字键 `workspace.status` 仍经稳定
    /// instance id 解析出非空且一致的 task-DB 权威（重启持久性）。
    #[test]
    fn status_numeric_key_resolves_task_db_via_instance_after_id_drift() {
        let conn = minimal_db();
        // 历史 alias：漂移前的 registry id=144 ↔ task-db id=1（instance 4baea3ff12c2ea5c）
        record_reconciliation_alias(&conn, 144, "4baea3ff12c2ea5c", 1, "4baea3ff12c2ea5c", "test")
            .unwrap();

        // 模拟 fresh-restart 后 registry 数字 id 漂移：当前 registry id=156 → instance 4baea3ff12c2ea5c
        let dir = std::env::temp_dir().join(format!("cw_recon_status_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let reg_path = dir.join("registry.db");
        {
            let rc = rusqlite::Connection::open(&reg_path).unwrap();
            rc.execute_batch(
                "CREATE TABLE IF NOT EXISTS daemon_workspaces (\
                    workspace_id INTEGER PRIMARY KEY AUTOINCREMENT, \
                    workspace_instance_id TEXT NOT NULL UNIQUE, \
                    snapshot_id TEXT, owner_uid INTEGER NOT NULL, \
                    git_remote_url TEXT DEFAULT '', git_head_commit_sha TEXT DEFAULT '', \
                    client_view_root TEXT NOT NULL, host_real_root TEXT NOT NULL, \
                    toolchain_fingerprint TEXT DEFAULT '', \
                    registered_at REAL NOT NULL, last_active_at REAL NOT NULL, \
                    status TEXT DEFAULT 'active');",
            )
            .unwrap();
            rc.execute(
                "INSERT INTO daemon_workspaces \
                 (workspace_id, workspace_instance_id, client_view_root, host_real_root, \
                  owner_uid, registered_at, last_active_at, status) \
                 VALUES (156, '4baea3ff12c2ea5c', '/v', '/r', 0, 0.0, 0.0, 'active')",
                [],
            )
            .unwrap();
        }
        let registry = WorkspaceRegistry::open_readonly(reg_path.to_str().unwrap()).unwrap();

        // 数字键 156：历史 alias 仅含 registry=144（过期），但经 instance 跨引用仍应得到 task-db=1
        let auth = resolve_status_authority(&conn, &registry, "156").unwrap().unwrap();
        assert_eq!(auth.registry_workspace_id, Some(156));
        assert_eq!(auth.registry_instance_id.as_deref(), Some("4baea3ff12c2ea5c"));
        assert_eq!(auth.task_db_workspace_id, Some(1));
        assert_eq!(auth.task_db_instance_id.as_deref(), Some("4baea3ff12c2ea5c"));

        // 重启后 instance 键同样一致（稳定性对照）
        let auth2 = resolve_status_authority(&conn, &registry, "4baea3ff12c2ea5c").unwrap().unwrap();
        assert_eq!(auth2.task_db_workspace_id, Some(1));
        assert_eq!(auth2.registry_workspace_id, Some(156));

        let _ = std::fs::remove_dir_all(&dir);
    }

    /// 负向：registry-only 孤儿（instance 无 task-DB capture）数字键状态仍返回 registry 侧，
    /// 但 task-DB 侧为 null（不合成、不崩）；确定性 repair-required 由 create 路径保证。
    #[test]
    fn status_registry_only_orphan_returns_partial_no_synthetic() {
        let conn = minimal_db();
        let dir = std::env::temp_dir().join(format!("cw_recon_orphan_{}", std::process::id()));
        let _ = std::fs::create_dir_all(&dir);
        let reg_path = dir.join("registry.db");
        {
            let rc = rusqlite::Connection::open(&reg_path).unwrap();
            rc.execute_batch(
                "CREATE TABLE IF NOT EXISTS daemon_workspaces (\
                    workspace_id INTEGER PRIMARY KEY AUTOINCREMENT, \
                    workspace_instance_id TEXT NOT NULL UNIQUE, \
                    snapshot_id TEXT, owner_uid INTEGER NOT NULL, \
                    git_remote_url TEXT DEFAULT '', git_head_commit_sha TEXT DEFAULT '', \
                    client_view_root TEXT NOT NULL, host_real_root TEXT NOT NULL, \
                    toolchain_fingerprint TEXT DEFAULT '', \
                    registered_at REAL NOT NULL, last_active_at REAL NOT NULL, \
                    status TEXT DEFAULT 'active');",
            )
            .unwrap();
            rc.execute(
                "INSERT INTO daemon_workspaces \
                 (workspace_id, workspace_instance_id, client_view_root, host_real_root, \
                  owner_uid, registered_at, last_active_at, status) \
                 VALUES (777, 'orphan-inst-zz', '/v', '/r', 0, 0.0, 0.0, 'active')",
                [],
            )
            .unwrap();
        }
        let registry = WorkspaceRegistry::open_readonly(reg_path.to_str().unwrap()).unwrap();
        let auth = resolve_status_authority(&conn, &registry, "777").unwrap().unwrap();
        assert_eq!(auth.registry_workspace_id, Some(777));
        assert_eq!(auth.registry_instance_id.as_deref(), Some("orphan-inst-zz"));
        assert!(auth.task_db_workspace_id.is_none(), "registry-only 不得合成 task-DB 侧");
        assert!(auth.task_db_instance_id.is_none());

        let _ = std::fs::remove_dir_all(&dir);
    }
}
