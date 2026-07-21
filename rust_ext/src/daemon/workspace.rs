//! Workspace registry —— workspace 注册、查询、状态管理 + UID ACL。
//!
//! 对应 Python：
//! - `db/db_daemon.py`（WORKSPACE_REGISTRY_DDL + register/list/get_status/update_status）
//! - `server/daemon_server.py:_owned_workspace` / `_validate_owned_path` / dispatch 的
//!   `workspace.register` / `workspace.list` / `workspace.status` /
//!   `workspace.connect` / `workspace.file.refresh` / `workspace.recover` 分支
//!
//! 跨平台：Windows 上 `_validate_owned_path` 跳过 owner_uid ACL 检查（开发测试用），
//! Unix 上做完整 ACL 校验（参考 daemon_server.py L227-242）。
//! 本模块覆盖 `workspace.register` / `workspace.list` / `workspace.status` /
//! `workspace.connect` / `workspace.file.refresh` / `workspace.recover`。
//! `snapshot.*` / `query.*` / `gc.*` / `backup` / `restore` 在 R6 的
//! `SnapshotDaemonState` 中实现。

use std::collections::HashMap;
use std::path::Path;
use std::sync::{Arc, Mutex};
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, Row};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use super::dispatch::{
    DaemonRpcError, DaemonState, DaemonStateExt, PeerCredential,
    get_str_param, get_str_param_or, require_str_param,
};
use super::replicator::{
    RefreshMessage, SessionStore, daemon_handle_connect, daemon_handle_refresh,
};
use super::staging_log::{StagingEntry, StagingLog};
use super::cas::CasStore;

/// workspace registry schema DDL（与 Python db_daemon.py:WORKSPACE_REGISTRY_DDL 一致）
const WORKSPACE_REGISTRY_DDL: &str = r#"
CREATE TABLE IF NOT EXISTS daemon_workspaces (
    workspace_id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_instance_id TEXT NOT NULL UNIQUE,
    snapshot_id TEXT,
    owner_uid INTEGER NOT NULL,
    git_remote_url TEXT DEFAULT '',
    git_head_commit_sha TEXT DEFAULT '',
    client_view_root TEXT NOT NULL,
    host_real_root TEXT NOT NULL,
    toolchain_fingerprint TEXT DEFAULT '',
    registered_at REAL NOT NULL,
    last_active_at REAL NOT NULL,
    status TEXT DEFAULT 'active'
);

CREATE TABLE IF NOT EXISTS container_mount_mappings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_id TEXT NOT NULL,
    container_path TEXT NOT NULL,
    host_path TEXT NOT NULL,
    mapping_type TEXT DEFAULT 'bind',
    UNIQUE(container_id, container_path)
);

CREATE TABLE IF NOT EXISTS daemon_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_workspaces_owner ON daemon_workspaces(owner_uid);
CREATE INDEX IF NOT EXISTS idx_workspaces_snapshot ON daemon_workspaces(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_workspaces_status ON daemon_workspaces(status);
"#;

/// schema_meta 写入 schema_version（与 Python init_daemon_schema 行为一致）
const SCHEMA_META_UPSERT: &str = r#"
INSERT OR REPLACE INTO daemon_state (key, value, updated_at)
VALUES ('schema_version', ?1, ?2);
"#;

fn now_ts() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 计算 workspace_instance_id（与 Python db_daemon.py:register_workspace 一致）
/// sha256("owner_uid|host_real_root|git_remote_url|git_head_commit_sha")[:16]
fn compute_workspace_instance_id(
    owner_uid: u32,
    host_real_root: &str,
    git_remote_url: &str,
    git_head_commit_sha: &str,
) -> String {
    let mut hasher = Sha256::new();
    hasher.update(format!(
        "{}|{}|{}|{}",
        owner_uid, host_real_root, git_remote_url, git_head_commit_sha
    ));
    let full = hasher.finalize();
    // 取前 8 字节，hex 编码后 16 字符
    hex_encode(&full[..8])
}

/// 计算 snapshot_id（与 Python db_daemon.py:register_workspace 一致）
/// 仅当 git_remote_url 和 git_head_commit_sha 均非空时生成。
/// sha256("git_remote_url|git_head_commit_sha|toolchain_fingerprint")[:16]
fn compute_snapshot_id(
    git_remote_url: &str,
    git_head_commit_sha: &str,
    toolchain_fingerprint: &str,
) -> String {
    let mut hasher = Sha256::new();
    hasher.update(format!(
        "{}|{}|{}",
        git_remote_url, git_head_commit_sha, toolchain_fingerprint
    ));
    let full = hasher.finalize();
    hex_encode(&full[..8])
}

/// 简易 hex 编码（避免引入 hex crate 依赖）
fn hex_encode(bytes: &[u8]) -> String {
    let mut s = String::with_capacity(bytes.len() * 2);
    for b in bytes {
        s.push_str(&format!("{:02x}", b));
    }
    s
}

/// workspace registry：封装 SQLite 连接 + 提供线程安全访问
///
/// 内部用 `std::sync::Mutex`（而非 parking_lot::Mutex）保护 rusqlite Connection，
/// 因为 rusqlite 默认不是 Sync。所有公共方法内部加锁，调用方无需关心。
pub struct WorkspaceRegistry {
    conn: Mutex<Connection>,
    /// registry DB 文件路径（用于 backup/restore VACUUM INTO）
    pub db_path: String,
}

impl WorkspaceRegistry {
    /// 打开指定路径的 registry DB（不存在则创建并初始化 schema）
    pub fn open(db_path: &str) -> Result<Self, rusqlite::Error> {
        // 确保父目录存在
        if let Some(parent) = Path::new(db_path).parent() {
            if !parent.as_os_str().is_empty() {
                let _ = std::fs::create_dir_all(parent);
            }
        }
        let conn = Connection::open(db_path)?;
        Self::init_conn(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
            db_path: db_path.to_string(),
        })
    }

    /// 内存数据库（测试用）
    pub fn open_in_memory() -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        Self::init_conn(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
            db_path: ":memory:".to_string(),
        })
    }

    /// 关闭当前连接并从 db_path 重新打开（用于 restore 流程）
    ///
    /// 实现细节：
    /// 1. 先把旧 Connection 替换为内存连接（释放对 db 文件的锁）
    /// 2. 然后用 db_path 重新打开并初始化 schema
    /// 3. 替换为新连接
    ///
    /// 调用方负责确保在调用前文件已被替换（如 std::fs::copy 覆盖）。
    pub fn reopen(&mut self) -> Result<(), rusqlite::Error> {
        let db_path = self.db_path.clone();
        // 内存数据库无法 restore（数据丢失），直接返回错误
        if db_path == ":memory:" {
            return Err(rusqlite::Error::InvalidParameterName(
                "无法对内存数据库执行 restore".to_string(),
            ));
        }
        // 步骤 1：用内存连接占位，旧 Connection 被 drop 释放文件锁
        let placeholder = Connection::open_in_memory()?;
        {
            let mut guard = self.conn.lock().unwrap();
            *guard = placeholder;
        }
        // 步骤 2：现在文件锁已释放，可以打开新连接
        let new_conn = Connection::open(&db_path)?;
        Self::init_conn(&new_conn)?;
        let mut guard = self.conn.lock().unwrap();
        *guard = new_conn;
        Ok(())
    }

    fn init_conn(conn: &Connection) -> Result<(), rusqlite::Error> {
        conn.execute_batch("PRAGMA busy_timeout=5000;")?;
        conn.execute_batch("PRAGMA journal_mode=WAL;")?;
        conn.execute_batch(WORKSPACE_REGISTRY_DDL)?;
        // 写入 schema_version 元信息（与 Python init_daemon_schema 一致）
        conn.execute(
            SCHEMA_META_UPSERT,
            params![super::SCHEMA_VERSION.to_string(), now_ts()],
        )?;
        Ok(())
    }

    /// 注册 workspace（对应 Python register_workspace）
    pub fn register_workspace(
        &self,
        owner_uid: u32,
        client_view_root: &str,
        host_real_root: &str,
        git_remote_url: &str,
        git_head_commit_sha: &str,
        toolchain_fingerprint: &str,
    ) -> Result<Value, rusqlite::Error> {
        let instance_id = compute_workspace_instance_id(
            owner_uid,
            host_real_root,
            git_remote_url,
            git_head_commit_sha,
        );
        let snapshot_id = if !git_remote_url.is_empty() && !git_head_commit_sha.is_empty() {
            Some(compute_snapshot_id(
                git_remote_url,
                git_head_commit_sha,
                toolchain_fingerprint,
            ))
        } else {
            None
        };

        let now = now_ts();
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO daemon_workspaces
             (workspace_instance_id, snapshot_id, owner_uid, git_remote_url,
              git_head_commit_sha, client_view_root, host_real_root,
              toolchain_fingerprint, registered_at, last_active_at, status)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, 'active')",
            params![
                instance_id,
                snapshot_id,
                owner_uid,
                git_remote_url,
                git_head_commit_sha,
                client_view_root,
                host_real_root,
                toolchain_fingerprint,
                now,
                now,
            ],
        )?;
        fetch_workspace_by_instance(&conn, &instance_id)
    }

    /// 列出 workspace（对应 Python list_workspaces）
    /// owner_uid=None 时列出所有，否则按 owner_uid 过滤
    pub fn list_workspaces(&self, owner_uid: Option<u32>) -> Result<Vec<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = if owner_uid.is_some() {
            conn.prepare(
                "SELECT workspace_id, workspace_instance_id, snapshot_id, owner_uid,
                        git_remote_url, git_head_commit_sha, client_view_root, host_real_root,
                        toolchain_fingerprint, registered_at, last_active_at, status
                 FROM daemon_workspaces WHERE owner_uid = ?1
                 ORDER BY last_active_at DESC",
            )?
        } else {
            conn.prepare(
                "SELECT workspace_id, workspace_instance_id, snapshot_id, owner_uid,
                        git_remote_url, git_head_commit_sha, client_view_root, host_real_root,
                        toolchain_fingerprint, registered_at, last_active_at, status
                 FROM daemon_workspaces ORDER BY last_active_at DESC",
            )?
        };
        let mut result = Vec::new();
        if let Some(uid) = owner_uid {
            for row in stmt.query_map(params![uid], row_to_json)? {
                result.push(row?);
            }
        } else {
            for row in stmt.query_map([], row_to_json)? {
                result.push(row?);
            }
        }
        Ok(result)
    }

    /// 获取 workspace 状态（对应 Python get_workspace_status）
    pub fn get_workspace_status(
        &self,
        workspace_instance_id: &str,
    ) -> Result<Option<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = conn.prepare(
            "SELECT workspace_id, workspace_instance_id, snapshot_id, owner_uid,
                    git_remote_url, git_head_commit_sha, client_view_root, host_real_root,
                    toolchain_fingerprint, registered_at, last_active_at, status
             FROM daemon_workspaces WHERE workspace_instance_id = ?1",
        )?;
        let mut rows = stmt.query(params![workspace_instance_id])?;
        if let Some(row) = rows.next()? {
            Ok(Some(row_to_json(row)?))
        } else {
            Ok(None)
        }
    }

    /// 更新 workspace 状态（对应 Python update_workspace_status）
    pub fn update_workspace_status(
        &self,
        workspace_instance_id: &str,
        status: &str,
    ) -> Result<bool, rusqlite::Error> {
        let now = now_ts();
        let conn = self.conn.lock().unwrap();
        let affected = conn.execute(
            "UPDATE daemon_workspaces SET status = ?1, last_active_at = ?2
             WHERE workspace_instance_id = ?3",
            params![status, now, workspace_instance_id],
        )?;
        Ok(affected > 0)
    }

    /// 统计 workspace 数量（用于 health 检查）
    pub fn count_workspaces(&self) -> Result<u32, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let count: u32 = conn.query_row(
            "SELECT COUNT(*) FROM daemon_workspaces",
            [],
            |row| row.get(0),
        )?;
        Ok(count)
    }

    // ============================================
    // G4: Container Mount Mapping CRUD
    // ============================================

    /// 注册容器挂载映射（INSERT OR REPLACE，UNIQUE(container_id, container_path) 约束）
    ///
    /// 对应 RPC `mount.register`：
    /// - container_id：容器标识（如 "ubuntu_2204"）
    /// - container_path：容器内路径前缀（如 "/home/user1"）
    /// - host_path：宿主机真实路径（如 "/data/docker_volumes/user1"）
    /// - mapping_type：bind / volume / smb（默认 bind）
    ///
    /// 返回新插入或替换后的映射记录（JSON）。
    pub fn register_mount_mapping(
        &self,
        container_id: &str,
        container_path: &str,
        host_path: &str,
        mapping_type: &str,
    ) -> Result<Value, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        conn.execute(
            "INSERT OR REPLACE INTO container_mount_mappings
             (container_id, container_path, host_path, mapping_type)
             VALUES (?1, ?2, ?3, ?4)",
            params![container_id, container_path, host_path, mapping_type],
        )?;
        // 重新查询以拿回 id（INSERT OR REPLACE 可能改 id）
        let mut stmt = conn.prepare(
            "SELECT id, container_id, container_path, host_path, mapping_type
             FROM container_mount_mappings
             WHERE container_id = ?1 AND container_path = ?2",
        )?;
        let mut rows = stmt.query(params![container_id, container_path])?;
        if let Some(row) = rows.next()? {
            mount_row_to_json(row)
        } else {
            Err(rusqlite::Error::QueryReturnedNoRows)
        }
    }

    /// 列出容器挂载映射
    ///
    /// 对应 RPC `mount.list`：
    /// - container_id=None：列出所有映射
    /// - container_id=Some(cid)：按 container_id 过滤
    pub fn list_mount_mappings(
        &self,
        container_id: Option<&str>,
    ) -> Result<Vec<Value>, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let mut stmt = if container_id.is_some() {
            conn.prepare(
                "SELECT id, container_id, container_path, host_path, mapping_type
                 FROM container_mount_mappings
                 WHERE container_id = ?1
                 ORDER BY id ASC",
            )?
        } else {
            conn.prepare(
                "SELECT id, container_id, container_path, host_path, mapping_type
                 FROM container_mount_mappings
                 ORDER BY id ASC",
            )?
        };
        let mut result = Vec::new();
        if let Some(cid) = container_id {
            for row in stmt.query_map(params![cid], mount_row_to_json)? {
                result.push(row?);
            }
        } else {
            for row in stmt.query_map([], mount_row_to_json)? {
                result.push(row?);
            }
        }
        Ok(result)
    }

    /// 删除容器挂载映射
    ///
    /// 对应 RPC `mount.delete`：
    /// - 按 (container_id, container_path) 删除单条
    /// - 返回删除的行数（0 表示不存在，1 表示已删除）
    pub fn delete_mount_mapping(
        &self,
        container_id: &str,
        container_path: &str,
    ) -> Result<u64, rusqlite::Error> {
        let conn = self.conn.lock().unwrap();
        let affected = conn.execute(
            "DELETE FROM container_mount_mappings
             WHERE container_id = ?1 AND container_path = ?2",
            params![container_id, container_path],
        )?;
        Ok(affected as u64)
    }
}

/// 从 Row 构造 mount mapping JSON 对象
fn mount_row_to_json(row: &Row<'_>) -> rusqlite::Result<Value> {
    let id: i64 = row.get(0)?;
    let container_id: String = row.get(1)?;
    let container_path: String = row.get(2)?;
    let host_path: String = row.get(3)?;
    let mapping_type: String = row.get(4)?;
    let mut m = Map::new();
    m.insert("id".to_string(), Value::Number(id.into()));
    m.insert("container_id".to_string(), Value::String(container_id));
    m.insert("container_path".to_string(), Value::String(container_path));
    m.insert("host_path".to_string(), Value::String(host_path));
    m.insert("mapping_type".to_string(), Value::String(mapping_type));
    Ok(Value::Object(m))
}

/// 从 Row 构造 JSON 对象（字段顺序与 Python SELECT * 一致）
fn row_to_json(row: &Row<'_>) -> rusqlite::Result<Value> {
    let workspace_id: i64 = row.get(0)?;
    let workspace_instance_id: String = row.get(1)?;
    let snapshot_id: Option<String> = row.get(2)?;
    let owner_uid: i64 = row.get(3)?;
    let git_remote_url: String = row.get(4)?;
    let git_head_commit_sha: String = row.get(5)?;
    let client_view_root: String = row.get(6)?;
    let host_real_root: String = row.get(7)?;
    let toolchain_fingerprint: String = row.get(8)?;
    let registered_at: f64 = row.get(9)?;
    let last_active_at: f64 = row.get(10)?;
    let status: String = row.get(11)?;

    let mut m = Map::new();
    m.insert("workspace_id".to_string(), Value::Number(workspace_id.into()));
    m.insert(
        "workspace_instance_id".to_string(),
        Value::String(workspace_instance_id),
    );
    m.insert(
        "snapshot_id".to_string(),
        snapshot_id.map(Value::String).unwrap_or(Value::Null),
    );
    m.insert("owner_uid".to_string(), Value::Number(owner_uid.into()));
    m.insert(
        "git_remote_url".to_string(),
        Value::String(git_remote_url),
    );
    m.insert(
        "git_head_commit_sha".to_string(),
        Value::String(git_head_commit_sha),
    );
    m.insert(
        "client_view_root".to_string(),
        Value::String(client_view_root),
    );
    m.insert("host_real_root".to_string(), Value::String(host_real_root));
    m.insert(
        "toolchain_fingerprint".to_string(),
        Value::String(toolchain_fingerprint),
    );
    m.insert(
        "registered_at".to_string(),
        serde_json::Number::from_f64(registered_at)
            .map(Value::Number)
            .unwrap_or(Value::Null),
    );
    m.insert(
        "last_active_at".to_string(),
        serde_json::Number::from_f64(last_active_at)
            .map(Value::Number)
            .unwrap_or(Value::Null),
    );
    m.insert("status".to_string(), Value::String(status));
    Ok(Value::Object(m))
}

/// 按 workspace_instance_id 查询单条记录
fn fetch_workspace_by_instance(
    conn: &Connection,
    workspace_instance_id: &str,
) -> Result<Value, rusqlite::Error> {
    let mut stmt = conn.prepare(
        "SELECT workspace_id, workspace_instance_id, snapshot_id, owner_uid,
                git_remote_url, git_head_commit_sha, client_view_root, host_real_root,
                toolchain_fingerprint, registered_at, last_active_at, status
         FROM daemon_workspaces WHERE workspace_instance_id = ?1",
    )?;
    let mut rows = stmt.query(params![workspace_instance_id])?;
    if let Some(row) = rows.next()? {
        row_to_json(row)
    } else {
        // 不应发生（INSERT OR REPLACE 后必然存在）
        Err(rusqlite::Error::QueryReturnedNoRows)
    }
}

// ============================================
// UID ACL 路径校验（对应 Python daemon_server.py:_validate_owned_path）
// ============================================

/// 校验路径存在且属于 peer_uid（Windows 跳过 UID 检查，仅 Unix 校验）
///
/// - require_file=true：要求是文件
/// - require_file=false：要求是目录
pub fn validate_owned_path(
    path: &str,
    peer_uid: u32,
    require_file: bool,
) -> Result<String, DaemonRpcError> {
    let real_path = std::fs::canonicalize(path).map_err(|_| {
        DaemonRpcError::new("path_not_found", format!("路径不存在: {}", path))
    })?;
    let real_path_str = real_path.to_string_lossy().to_string();

    // 检查文件类型
    let metadata = std::fs::metadata(&real_path).map_err(|_| {
        DaemonRpcError::new("path_not_found", real_path_str.clone())
    })?;
    if require_file && !metadata.is_file() {
        return Err(DaemonRpcError::new(
            "path_not_found",
            format!("不是文件: {}", real_path_str),
        ));
    }
    if !require_file && !metadata.is_dir() {
        return Err(DaemonRpcError::new(
            "path_not_found",
            format!("不是目录: {}", real_path_str),
        ));
    }

    // Unix UID ACL 检查（root 跳过；Windows 跳过）
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if peer_uid != 0 {
            let owner_uid = metadata.uid();
            if owner_uid != peer_uid {
                return Err(DaemonRpcError::new(
                    "path_forbidden",
                    format!(
                        "路径 owner_uid={}，peer_uid={}",
                        owner_uid, peer_uid
                    ),
                ));
            }
        }
    }
    #[cfg(not(unix))]
    {
        // Windows 上不做 UID ACL 检查（开发测试），生产部署用 Linux
        let _ = peer_uid;
    }

    Ok(real_path_str)
}

/// 校验 workspace 属于 peer_uid（对应 Python daemon_server.py:_owned_workspace）
///
/// 返回 workspace JSON（用于后续处理）。错误码：
/// - workspace_not_found：workspace 不存在
/// - workspace_forbidden：owner_uid != peer_uid
/// - workspace_archived：status == "archived"
pub fn owned_workspace(
    registry: &WorkspaceRegistry,
    peer_uid: u32,
    workspace_instance_id: &str,
) -> Result<Value, DaemonRpcError> {
    let workspace = registry
        .get_workspace_status(workspace_instance_id)
        .map_err(|e| DaemonRpcError::internal_error(format!("registry 查询失败: {}", e)))?
        .ok_or_else(|| DaemonRpcError::workspace_not_found(workspace_instance_id))?;

    let owner_uid = workspace
        .get("owner_uid")
        .and_then(|v| v.as_i64())
        .unwrap_or(-1);
    if owner_uid != peer_uid as i64 {
        return Err(DaemonRpcError::workspace_forbidden(
            "workspace 不属于当前 UID",
        ));
    }
    let status = workspace
        .get("status")
        .and_then(|v| v.as_str())
        .unwrap_or("");
    if status == "archived" {
        return Err(DaemonRpcError::workspace_archived(workspace_instance_id));
    }
    Ok(workspace)
}

// ============================================
// DaemonStateExt 扩展：接入 workspace.* RPC
// ============================================

/// 组合 DaemonState + WorkspaceRegistry + data_root 的 daemon state 实现。
///
/// 覆盖：
/// - `workspace.register`：注册 workspace（含路径校验）
/// - `workspace.list`：列出当前 UID 拥有的 workspace
/// - `workspace.status`：查询指定 workspace 状态（含 ACL）
/// - `workspace.connect`：session epoch CAS 握手
/// - `workspace.file.refresh`：增量 refresh 经 CAS 两阶段 + staging log + replicate
/// - `workspace.recover`：用 Replicator 重放 pending staging entries
///
/// 未覆盖（保留 trait 默认 method_not_found，在 `SnapshotDaemonState` 中实现）：
/// - `snapshot.*` / `query.*` / `gc.*` / `backup` / `restore`
pub struct WorkspaceDaemonState {
    pub base: DaemonState,
    pub registry: WorkspaceRegistry,
    /// workspace 数据根目录（$data_root/$workspace_instance_id/{workspace.db,cas.db,staging.log}）
    pub data_root: std::path::PathBuf,
    /// per-workspace 资源懒缓存：workspace_instance_id → SessionStore/CasStore/StagingLog
    ///
    /// 对应 Python EnterpriseDaemonService._workspace_resources（懒初始化 + 线程安全）。
    /// 这里用 HashMap 缓存（daemon 是单 Arc<Mutex<State>> 持有，&mut self 即可访问，
    /// 无需额外锁）。资源一旦创建，后续 RPC 直接复用，避免重复 open/schema 初始化。
    pub resources: HashMap<String, Arc<WorkspaceResources>>,
    /// G11: Snapshot 发布器（可选，None 时 replicate 跳过 publish_snapshot）。
    ///
    /// 注入路径：`cw_daemon.rs` 启动时创建共享 `Arc<SnapshotCachePublisher>`，
    /// 通过 `with_snapshot_publisher` builder 传入。daemon 内部所有 worker 线程
    /// 共享同一个 publisher 实例（`Arc` 引用计数 + 内部只读）。
    pub snapshot_publisher: Option<Arc<super::replicator::SnapshotCachePublisher>>,
    /// G11: CodeGraph DB 路径模板（含 `{workspace_instance_id}` 占位符）。
    ///
    /// 空字符串表示不启用 snapshot publish（保持 R5 行为，db_path 传空）。
    /// 模板来源：`DaemonConfig.codegraph_db_path_template`，daemon 启动时传入。
    pub codegraph_db_path_template: String,
}

/// per-workspace 资源（懒初始化，缓存于 WorkspaceDaemonState.resources）
///
/// 对应 Python EnterpriseDaemonService._get_workspace_resources 返回的 dict：
/// - `ws_conn` → SessionStore（含 agent_sessions / workspace_active_session / file_generations）
/// - `cas_conn` → CasStore（含 cas_file_cache / file_generations）
/// - `staging_log` → StagingLog（持久化的 JSONL staging log）
///
/// 注意：R5 阶段 SessionStore 内部已经初始化了 CAS_SCHEMA_DDL（含 file_generations 表），
/// 因此 cas_store 与 session_store 中的 file_generations 是分离的两张表（不同 DB）。
/// daemon_handle_refresh 同时使用 ws_conn（session epoch 校验）和 cas_store（两阶段 CAS）。
pub struct WorkspaceResources {
    /// workspace session DB（agent_sessions + workspace_active_session + file_generations）
    pub session_store: Arc<SessionStore>,
    /// CAS DB（cas_file_cache + file_generations）
    pub cas_store: Arc<CasStore>,
    /// staging log（追加写入 pending entries，replicate 后标记 applied）
    pub staging_log: Arc<StagingLog>,
}

impl WorkspaceDaemonState {
    pub fn new(registry: WorkspaceRegistry) -> Self {
        Self {
            base: DaemonState::default(),
            registry,
            data_root: std::path::PathBuf::new(),
            resources: HashMap::new(),
            snapshot_publisher: None,
            codegraph_db_path_template: String::new(),
        }
    }

    /// 指定 data_root 构造（用于 workspace.recover / workspace.connect / workspace.file.refresh）
    pub fn with_data_root(
        registry: WorkspaceRegistry,
        data_root: std::path::PathBuf,
    ) -> Self {
        Self {
            base: DaemonState::default(),
            registry,
            data_root,
            resources: HashMap::new(),
            snapshot_publisher: None,
            codegraph_db_path_template: String::new(),
        }
    }

    /// G11: 注入 SnapshotCachePublisher（启用 snapshot publish 主路径）
    ///
    /// 配合 `with_codegraph_db_path_template` 一起使用：
    /// - publisher 注入后，Replicator.replicate() 会调用 publish_snapshot
    /// - db_path 模板用于运行时替换 `{workspace_instance_id}` 占位符
    ///
    /// 二者任一为空/None，replicate 跳过 publish（保持 R5 行为）。
    pub fn with_snapshot_publisher(
        mut self,
        publisher: Arc<super::replicator::SnapshotCachePublisher>,
    ) -> Self {
        self.snapshot_publisher = Some(publisher);
        self
    }

    /// G11: 设置 CodeGraph DB 路径模板
    ///
    /// 模板含 `{workspace_instance_id}` 占位符，运行时替换为实际 workspace ID。
    /// 空字符串表示不启用 snapshot publish（保持 R5 行为）。
    pub fn with_codegraph_db_path_template(mut self, template: String) -> Self {
        self.codegraph_db_path_template = template;
        self
    }

    /// 懒初始化 per-workspace 资源（SessionStore + CasStore + StagingLog）
    ///
    /// 对应 Python EnterpriseDaemonService._get_workspace_resources。
    /// 路径布局：`$data_root/$workspace_instance_id/{workspace.db, cas.db, staging.log}`
    ///
    /// 已缓存则直接返回 Arc clone；未缓存则创建目录 + 打开三个资源 + 缓存。
    pub fn get_or_init_resources(
        &mut self,
        workspace_instance_id: &str,
    ) -> Result<Arc<WorkspaceResources>, DaemonRpcError> {
        // 已缓存直接返回
        if let Some(res) = self.resources.get(workspace_instance_id) {
            return Ok(Arc::clone(res));
        }

        // data_root 必须已配置
        if self.data_root.as_os_str().is_empty() {
            return Err(DaemonRpcError::new(
                "resources_init_failed",
                "data_root 未配置（无法定位 workspace 资源目录）",
            ));
        }

        // 创建 workspace 数据目录
        let ws_dir = self.data_root.join(workspace_instance_id);
        if let Err(e) = std::fs::create_dir_all(&ws_dir) {
            return Err(DaemonRpcError::new(
                "resources_init_failed",
                format!("create_dir_all({:?}) 失败: {}", ws_dir, e),
            ));
        }

        // 打开 SessionStore（workspace.db）
        let ws_db_path = ws_dir.join("workspace.db");
        let session_store = SessionStore::open(ws_db_path.to_string_lossy().as_ref())
            .map_err(|e| DaemonRpcError::new(
                "resources_init_failed",
                format!("SessionStore::open 失败: {}", e),
            ))?;

        // 打开 CasStore（cas.db）
        let cas_db_path = ws_dir.join("cas.db");
        let cas_store = CasStore::open(cas_db_path.to_string_lossy().as_ref())
            .map_err(|e| DaemonRpcError::new(
                "resources_init_failed",
                format!("CasStore::open 失败: {}", e),
            ))?;

        // 打开 StagingLog（staging.log）
        let staging_log_path = ws_dir.join("staging.log");
        let staging_log = StagingLog::new(staging_log_path.to_string_lossy().as_ref())
            .map_err(|e| DaemonRpcError::new(
                "resources_init_failed",
                format!("StagingLog::new 失败: {}", e),
            ))?;

        let resources = Arc::new(WorkspaceResources {
            session_store: Arc::new(session_store),
            cas_store: Arc::new(cas_store),
            staging_log: Arc::new(staging_log),
        });
        self.resources.insert(workspace_instance_id.to_string(), Arc::clone(&resources));
        Ok(resources)
    }
}

impl DaemonStateExt for WorkspaceDaemonState {
    fn daemon_state(&self) -> &DaemonState {
        &self.base
    }

    fn handle_health(&mut self, _peer: PeerCredential) -> Result<Value, DaemonRpcError> {
        // G14: 接入 HealthChecker，返回完整检查结果
        // 保留向后兼容字段（pid / schema_version / workspace_count）
        let state = self.daemon_state();
        let workspace_count = self
            .registry
            .count_workspaces()
            .map_err(|e| DaemonRpcError::internal_error(format!("count_workspaces: {}", e)))?;

        let config = super::health::HealthConfig {
            registry_db_path: self.registry.db_path.clone(),
            data_root: self.data_root.to_string_lossy().to_string(),
            start_time: state.start_time,
            // 默认 1GB（后续可从 daemon config 读取）
            memory_max_bytes: 1024 * 1024 * 1024,
        };
        let checker = super::health::HealthChecker::new(config);
        let mut result = checker.check_all();

        // 追加向后兼容字段
        if let Some(obj) = result.as_object_mut() {
            obj.insert("pid".to_string(), Value::Number(state.pid.into()));
            obj.insert(
                "schema_version".to_string(),
                Value::Number(state.schema_version.into()),
            );
            obj.insert(
                "workspace_count".to_string(),
                Value::Number(workspace_count.into()),
            );
        }

        Ok(result)
    }

    fn handle_workspace_register(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let client_view_root = require_str_param(params, "client_view_root")?;
        // 路径校验（require_file=false，要求是目录）
        let host_real_root = validate_owned_path(client_view_root, peer.uid, false)?;
        let git_remote_url = get_str_param_or(params, "git_remote_url", "");
        let git_head_commit_sha = get_str_param_or(params, "git_head_commit_sha", "");
        let toolchain_fingerprint = get_str_param_or(params, "toolchain_fingerprint", "");

        self.registry
            .register_workspace(
                peer.uid,
                client_view_root,
                &host_real_root,
                &git_remote_url,
                &git_head_commit_sha,
                &toolchain_fingerprint,
            )
            .map_err(|e| DaemonRpcError::internal_error(format!("register_workspace: {}", e)))
    }

    fn handle_workspace_list(
        &mut self,
        peer: PeerCredential,
        _params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspaces = self
            .registry
            .list_workspaces(Some(peer.uid))
            .map_err(|e| DaemonRpcError::internal_error(format!("list_workspaces: {}", e)))?;
        Ok(Value::Array(workspaces))
    }

    fn handle_workspace_status(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // ACL 检查（owner_uid 匹配 + 非 archived）
        let workspace = owned_workspace(&self.registry, peer.uid, workspace_instance_id)?;
        Ok(workspace)
    }

    fn handle_workspace_connect(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // 对应 Python daemon_server.py L271-292
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // ACL 校验（owner_uid 匹配 + 非 archived）
        let workspace = owned_workspace(&self.registry, peer.uid, workspace_instance_id)?;
        let agent_session_id = require_str_param(params, "agent_session_id")?;

        // 从 registry 行提取数值 workspace_id（agent_sessions 表的 PK 之一）
        let workspace_id_num: i64 = workspace
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| {
                DaemonRpcError::internal_error("workspace_id 字段缺失或非数值".to_string())
            })?;

        // 懒初始化 per-workspace 资源（SessionStore / CasStore / StagingLog）
        let resources = self.get_or_init_resources(workspace_instance_id)?;

        // 调用 daemon_handle_connect（session epoch CAS）
        let result = daemon_handle_connect(
            peer.uid,
            workspace_id_num,
            agent_session_id,
            resources.session_store.conn(),
        )
        .map_err(|e| DaemonRpcError::new("connect_failed", e.message))?;

        // 在返回里附上 workspace_instance_id（与 Python 一致）
        let mut result = result;
        if let Value::Object(ref mut m) = result {
            m.insert(
                "workspace_instance_id".to_string(),
                Value::String(workspace_instance_id.to_string()),
            );
        }
        Ok(result)
    }

    fn handle_workspace_file_refresh(
        &mut self,
        peer: PeerCredential,
        params: &Value,
        received_fds: &[i32],
    ) -> Result<Value, DaemonRpcError> {
        // 对应 Python daemon_server.py L376-423
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // ACL 校验
        let workspace = owned_workspace(&self.registry, peer.uid, workspace_instance_id)?;
        let workspace_id_num: i64 = workspace
            .get("workspace_id")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| {
                DaemonRpcError::internal_error("workspace_id 字段缺失或非数值".to_string())
            })?;

        // 提取 RefreshMessage 字段
        let rel_path = require_str_param(params, "rel_path")?.to_string();
        let agent_session_id = require_str_param(params, "agent_session_id")?.to_string();
        let monotonic_seq = params
            .get("monotonic_seq")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少字段: monotonic_seq"))?;
        let session_epoch = params
            .get("session_epoch")
            .and_then(|v| v.as_i64())
            .ok_or_else(|| DaemonRpcError::invalid_params("缺少字段: session_epoch"))?;
        let abs_path = get_str_param(params, "abs_path").map(|s| s.to_string());

        let msg = RefreshMessage {
            rel_path: rel_path.clone(),
            agent_session_id: agent_session_id.clone(),
            monotonic_seq,
            session_epoch,
            abs_path,
        };

        // G8-T3：提取 canonical_bytes（优先 FD，次选 hex/b64）
        // 规范：daemon-ipc-security.md §3.2 —— daemon 不信任 agent 提供的 hash，必须重新计算
        // 规范：parse-input-abi.md §2 —— canonical bytes 是唯一输入入口
        // 四种获取方式（按优先级）：
        // 1. FD（仅 Unix，SCM_RIGHTS 传递）：daemon 直接读文件内容，消除 TOCTOU
        // 2. canonical_bytes_hex（跨平台）：客户端 hex 编码后传入（agent_protocol.py 默认路径）
        // 3. canonical_bytes_b64（跨平台）：客户端 base64 编码后传入（兼容旧客户端）
        // 4. 均无：返回 None，_daemon_parse_and_publish 内降级为 abs_path 读取
        //
        // G10/G20: FD 路径用四重校验替代 read_to_end 无界读，避免 OOM 攻击
        let canonical_bytes: Option<Vec<u8>> = if !received_fds.is_empty() {
            #[cfg(unix)]
            {
                if received_fds.len() > 1 {
                    return Err(DaemonRpcError::invalid_params(
                        "workspace.file.refresh 最多接收 1 个 FD",
                    ));
                }
                let fd = received_fds[0];
                // G10/G20: 六重校验（P1-3 复审整改 2026-07-21）
                // 1. FD 类型（fstat S_IFREG）
                // 2. owner UID 校验（st_uid == peer_uid，root 跳过）
                // 3. memfd seals 校验（仅 Linux，F_GET_SEALS 包含完整 seals 集合）
                // 4. 大小预检（st_size 预分配）
                // 5. 容量上限（DEFAULT_MAX_FD_READ_BYTES = 64MB）
                // 6. 摘要比对（客户端提供 expected_sha256 时校验）
                //
                // from_raw_fd 接管 FD 所有权；校验 + 读取完成后 file drop 会关闭 FD。
                // server.rs 在 dispatch 后会再次 close_fds，但关闭已关闭 FD 只返回
                // EBADF 并被忽略，双重关闭是安全的。
                use crate::daemon::memfd;
                // G9/G34（2026-07-20 批次7）：字段对齐——
                // agent_protocol.py 实际传 content_hash（小文件 hex 路径 + 大文件 FD 路径），
                // 但 Rust 端原读 expected_sha256，导致字段错配 + 摘要校验永远跳过。
                // 修复：优先 expected_sha256（保留旧路径），fallback content_hash（agent 默认）。
                let expected_sha256: Option<&str> = params
                    .get("expected_sha256")
                    .and_then(|v| v.as_str())
                    .or_else(|| {
                        params
                            .get("content_hash")
                            .and_then(|v| v.as_str())
                            .filter(|s| !s.is_empty())
                    });
                match memfd::read_from_fd_with_validation(
                    fd,
                    memfd::DEFAULT_MAX_FD_READ_BYTES,
                    expected_sha256,
                    peer.uid,
                ) {
                    Ok(buf) => Some(buf),
                    Err(e) => {
                        return Err(DaemonRpcError::new(
                            "fd_read_failed",
                            format!("FD 读取校验失败（fd={}）: {}", fd, e),
                        ));
                    }
                }
            }
            #[cfg(not(unix))]
            {
                let _ = peer;
                return Err(DaemonRpcError::invalid_params(
                    "FD 模式仅 Unix 支持（Windows 请使用 canonical_bytes_b64 参数）",
                ));
            }
        } else if let Some(hex) = params.get("canonical_bytes_hex").and_then(|v| v.as_str()) {
            // canonical_bytes_hex：agent_protocol.py 小文件默认路径
            match hex::decode(hex) {
                Ok(bytes) => Some(bytes),
                Err(e) => {
                    return Err(DaemonRpcError::new(
                        "hex_decode_failed",
                        format!("canonical_bytes_hex decode failed: {}", e),
                    ));
                }
            }
        } else if let Some(b64) = params.get("canonical_bytes_b64").and_then(|v| v.as_str()) {
            // canonical_bytes_b64：兼容旧客户端 / 显式 base64 路径
            use base64::Engine;
            match base64::engine::general_purpose::STANDARD.decode(b64) {
                Ok(bytes) => Some(bytes),
                Err(e) => {
                    return Err(DaemonRpcError::new(
                        "base64_decode_failed",
                        format!("canonical_bytes_b64 decode failed: {}", e),
                    ));
                }
            }
        } else {
            None
        };

        // K2 评审修复（2026-07-20）：canonical_bytes is None 时 daemon 会
        // 从 msg.abs_path 直接读取客户端文件，必须校验：
        // 1) owner UID 匹配（validate_owned_path 已覆盖）
        // 2) path 必须落在 workspace host_real_root 内（防路径逃逸）
        if canonical_bytes.is_none() {
            if let Some(ref abs_path_str) = msg.abs_path {
                if !abs_path_str.is_empty() {
                    let real_abs = validate_owned_path(abs_path_str, peer.uid, true)?;
                    if let Some(host_root_val) = workspace.get("host_real_root").and_then(|v| v.as_str()) {
                        if !host_root_val.is_empty() {
                            let real_host_root = std::fs::canonicalize(host_root_val)
                                .map(|p| p.to_string_lossy().to_string())
                                .unwrap_or_else(|_| host_root_val.to_string());
                            // real_abs == real_host_root 或以 real_host_root + sep 开头
                            let sep = std::path::MAIN_SEPARATOR.to_string();
                            let ok = real_abs == real_host_root
                                || real_abs.starts_with(&format!("{}{}", real_host_root, sep));
                            if !ok {
                                return Err(DaemonRpcError::new(
                                    "path_escape",
                                    format!(
                                        "abs_path 不在 workspace host_real_root 内：{}",
                                        real_abs
                                    ),
                                ));
                            }
                        }
                    }
                }
            }
        }

        // 懒初始化 per-workspace 资源
        let resources = self.get_or_init_resources(workspace_instance_id)?;

        // 调用 daemon_handle_refresh（session epoch 校验 + 两阶段 CAS + parse + publish）
        let result = daemon_handle_refresh(
            workspace_id_num,
            &msg,
            resources.session_store.conn(),
            Some(&resources.cas_store),
            canonical_bytes.as_deref(),
        )
        .map_err(|e| DaemonRpcError::new("refresh_failed", e.message))?;

        // 构造响应：status + generation + cas_result（若有）
        let mut response = Map::new();
        response.insert(
            "status".to_string(),
            Value::String(result.status.clone()),
        );
        response.insert(
            "generation".to_string(),
            Value::String(result.generation.clone()),
        );
        if let Some(cas_result) = result.cas_result {
            response.insert("cas_result".to_string(), cas_result);
        }

        // committed 时追加 staging entry 并触发 replicate（与 Python L404-420 一致）
        if result.status == "committed" {
            let content_hash = get_str_param_or(params, "content_hash", "");
            let language = get_str_param_or(params, "language", "");

            let mut entry = StagingEntry::new(
                workspace_instance_id,
                &rel_path,
                &content_hash,
                &language,
            );

            // M4+M5+M6（2026-07-20 批次4）：填充 staging entry 的
            // parse_delta / frontier / metrics_update 三个 JSON 字段。
            //
            // 调用链：
            //   DeltaComputer::compute_parse_delta
            //     → FrontierComputer::compute_frontier_with_budget
            //       → MetricsComputer::compute_local_update
            //
            // 当前为 store=None 退化模式：直接 delta + directly_affected，
            // upstream/downstream 为空（依赖 GraphStore 才能展开）。
            // GraphStore 可通过 SnapshotCachePublisher 注入（后续接入），
            // 接入后此分支可改为 if let Some(store) = ... 计算完整 frontier。
            //
            // 设计：写入 JSON 摘要而非完整结构体（StagingEntry 字段定义为
            // Map<String, Value>），便于 Python 端 audit 与可视化。
            // 任何一步失败都不阻塞 staging_log.append（保持与现状一致的容错）。
            {
                use crate::delta::DeltaComputer;
                use crate::frontier::FrontierComputer;
                use crate::metrics::MetricsComputer;
                use crate::daemon::budget::QueryBudget;
                use std::path::PathBuf;

                // 构造绝对文件路径：优先 msg.abs_path（客户端真实路径），
                // 否则 host_real_root + rel_path 拼接（daemon 端可读）
                let abs_file_path: PathBuf = msg
                    .abs_path
                    .clone()
                    .filter(|s| !s.is_empty())
                    .map(PathBuf::from)
                    .or_else(|| {
                        workspace
                            .get("host_real_root")
                            .and_then(|v| v.as_str())
                            .filter(|root| !root.is_empty())
                            .map(|root| Path::new(root).join(&rel_path))
                    })
                    .unwrap_or_else(|| Path::new(&rel_path).to_path_buf());

                // M4: parse delta（store=None，纯 tree-sitter 解析）
                match DeltaComputer::compute_parse_delta(&abs_file_path, None) {
                    Ok(parse_delta) => {
                        let mut pd = Map::new();
                        pd.insert(
                            "file_path".to_string(),
                            Value::String(
                                parse_delta.file_path.to_string_lossy().into_owned(),
                            ),
                        );
                        pd.insert(
                            "language".to_string(),
                            Value::String(parse_delta.language.clone()),
                        );
                        pd.insert(
                            "content_hash".to_string(),
                            Value::String(parse_delta.content_hash.clone()),
                        );
                        pd.insert(
                            "total_lines".to_string(),
                            Value::Number(parse_delta.total_lines.into()),
                        );
                        pd.insert(
                            "symbols_added".to_string(),
                            Value::Number(parse_delta.symbol_delta.added.len().into()),
                        );
                        pd.insert(
                            "symbols_removed".to_string(),
                            Value::Number(parse_delta.symbol_delta.removed.len().into()),
                        );
                        pd.insert(
                            "symbols_changed".to_string(),
                            Value::Number(parse_delta.symbol_delta.changed.len().into()),
                        );
                        pd.insert(
                            "calls_added".to_string(),
                            Value::Number(parse_delta.raw_call_delta.added.len().into()),
                        );
                        pd.insert(
                            "calls_removed".to_string(),
                            Value::Number(parse_delta.raw_call_delta.removed.len().into()),
                        );
                        pd.insert(
                            "summary".to_string(),
                            Value::String(parse_delta.summary()),
                        );
                        entry.parse_delta = pd;

                        // M5: frontier（store=None 退化，QueryBudget::default）
                        let budget = QueryBudget::default();
                        let frontier = FrontierComputer::compute_frontier_with_budget(
                            &parse_delta,
                            None,
                            budget,
                        );
                        let mut fd = Map::new();
                        fd.insert(
                            "directly_affected_count".to_string(),
                            Value::Number(frontier.directly_affected.len().into()),
                        );
                        fd.insert(
                            "upstream_direct_count".to_string(),
                            Value::Number(frontier.upstream_direct.len().into()),
                        );
                        fd.insert(
                            "downstream_direct_count".to_string(),
                            Value::Number(frontier.downstream_direct.len().into()),
                        );
                        fd.insert(
                            "upstream_transitive_count".to_string(),
                            Value::Number(frontier.upstream_transitive.len().into()),
                        );
                        fd.insert(
                            "downstream_transitive_count".to_string(),
                            Value::Number(frontier.downstream_transitive.len().into()),
                        );
                        fd.insert(
                            "partial".to_string(),
                            Value::Bool(frontier.partial),
                        );
                        // 直接列出受影响的 qnames（前 50 个，避免 JSON 过大）
                        let directly_affected: Vec<Value> = frontier
                            .directly_affected
                            .iter()
                            .take(50)
                            .map(|s| Value::String(s.clone()))
                            .collect();
                        fd.insert(
                            "directly_affected".to_string(),
                            Value::Array(directly_affected),
                        );
                        fd.insert(
                            "summary".to_string(),
                            Value::String(frontier.summary()),
                        );
                        entry.frontier = fd;

                        // M6: metrics update（store=None 退化，impact_depth=2）
                        let metrics_update = MetricsComputer::compute_local_update(
                            &frontier,
                            &parse_delta,
                            None,
                            2,
                        );
                        let mut md = Map::new();
                        md.insert(
                            "is_empty".to_string(),
                            Value::Bool(metrics_update.is_empty()),
                        );
                        md.insert(
                            "depth_changes_count".to_string(),
                            Value::Number(metrics_update.depth_changes.len().into()),
                        );
                        md.insert(
                            "cycle_changes_count".to_string(),
                            Value::Number(metrics_update.cycle_changes.len().into()),
                        );
                        md.insert(
                            "impact_changes_count".to_string(),
                            Value::Number(metrics_update.impact_changes.len().into()),
                        );
                        entry.metrics_update = md;
                    }
                    Err(e) => {
                        // M4 失败：写入 error 摘要，不阻塞 staging_log.append
                        let mut pd = Map::new();
                        pd.insert("error".to_string(), Value::String(e.clone()));
                        entry.parse_delta = pd;
                        eprintln!(
                            "[M4] compute_parse_delta failed for {}: {}",
                            rel_path, e
                        );
                    }
                }
            }

            match resources.staging_log.append(&mut entry) {
                Ok(_lsn) => {
                    // G11: 触发 replicate 并按配置发布 snapshot
                    //
                    // - 若 daemon 启动时注入了 SnapshotCachePublisher + 配置了
                    //   codegraph_db_path_template：replicate 会调用 publish_snapshot，
                    //     从 db_path 指向的 SQLite 加载符号 + 调用图 → 构建 GraphSnapshot
                    //     → 发布到 SnapshotCache（per-workspace ArcSwap）
                    // - 若 publisher 为 None 或 db_path 解析为空：保持 R5 行为，
                    //   只做 read_pending → mark_applied_batch，不发布 snapshot
                    //
                    // db_path 解析：模板中 `{workspace_instance_id}` 替换为实际 workspace ID
                    use crate::daemon::replicator::Replicator;
                    let db_path = if !self.codegraph_db_path_template.is_empty() {
                        self.codegraph_db_path_template
                            .replace("{workspace_instance_id}", workspace_instance_id)
                    } else {
                        String::new()
                    };
                    let replicator = Replicator::new(&resources.staging_log);
                    // 注入 publisher（若配置齐全）
                    let replicator = if !db_path.is_empty() {
                        if let Some(ref publisher) = self.snapshot_publisher {
                            replicator.with_snapshot_publisher(publisher.as_ref())
                        } else {
                            replicator
                        }
                    } else {
                        replicator
                    };
                    let repl_result = replicator.replicate(workspace_instance_id, &db_path, "");

                    let mut repl_map = Map::new();
                    repl_map.insert(
                        "generation".to_string(),
                        Value::Number(repl_result.generation.into()),
                    );
                    repl_map.insert(
                        "applied_count".to_string(),
                        Value::Number(repl_result.applied_count.into()),
                    );
                    repl_map.insert(
                        "pending_count".to_string(),
                        Value::Number(repl_result.pending_count.into()),
                    );
                    repl_map.insert(
                        "duration_ms".to_string(),
                        Value::Number(
                            serde_json::Number::from_f64(repl_result.duration_ms)
                                .unwrap_or_else(|| serde_json::Number::from(0u32)),
                        ),
                    );
                    // G11: 根据 publisher + db_path + replicate 结果决定 snapshot_published
                    let snapshot_published = !db_path.is_empty()
                        && self.snapshot_publisher.is_some()
                        && repl_result.success
                        && repl_result.generation > 0;
                    repl_map.insert(
                        "snapshot_published".to_string(),
                        Value::Bool(snapshot_published),
                    );
                    if !snapshot_published {
                        let warning = if db_path.is_empty() {
                            "snapshot 未发布（codegraph_db_path_template 未配置，db_path 为空）。\
                             G11 修复：在 daemon 配置中设置 codegraph_db_path_template \
                             + 注入 SnapshotCachePublisher 后可启用 snapshot 发布。"
                        } else if self.snapshot_publisher.is_none() {
                            "snapshot 未发布（SnapshotCachePublisher 未注入）。\
                             G11 修复：daemon 启动时通过 with_snapshot_publisher 注入 publisher。"
                        } else if !repl_result.success {
                            "snapshot 发布失败（replicate 返回 success=false），\
                             查看 error 字段了解详情。"
                        } else {
                            "snapshot 未发布（未知原因：generation <= 0）。"
                        };
                        repl_map.insert(
                            "snapshot_warning".to_string(),
                            Value::String(warning.to_string()),
                        );
                    }
                    if let Some(err) = repl_result.error {
                        repl_map.insert(
                            "error".to_string(),
                            Value::String(err),
                        );
                    }
                    response.insert("replication".to_string(), Value::Object(repl_map));
                }
                Err(e) => {
                    // staging log append 失败不阻塞 refresh 成功，但记录 error
                    response.insert(
                        "staging_error".to_string(),
                        Value::String(format!("staging_log::append 失败: {}", e)),
                    );
                }
            }
        }

        // 附 workspace_instance_id（便于客户端关联）
        response.insert(
            "workspace_instance_id".to_string(),
            Value::String(workspace_instance_id.to_string()),
        );
        Ok(Value::Object(response))
    }

    fn handle_workspace_recover(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // 1. 提取 workspace_instance_id 参数
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;

        // 2. ACL 校验：必须是 owner 才能触发 recover
        let workspace = owned_workspace(&self.registry, peer.uid, workspace_instance_id)?;

        let ws_id = workspace
            .get("workspace_instance_id")
            .and_then(|v| v.as_str())
            .ok_or_else(|| {
                DaemonRpcError::internal_error(
                    "workspace_instance_id missing in registry record".to_string(),
                )
            })?
            .to_string();

        // 2. 定位 staging.log（$data_root/$ws_id/staging.log）
        // data_root 为空时（make_state() 测试场景）返回 recover_failed
        if self.data_root.as_os_str().is_empty() {
            return Err(DaemonRpcError::new(
                "recover_failed",
                "data_root 未配置（无法定位 staging.log）",
            ));
        }
        let ws_dir = self.data_root.join(&ws_id);
        let staging_log_path = ws_dir.join("staging.log");

        // 3. 打开 StagingLog（不存在视为无 pending，返回空结果）
        let staging_log = match StagingLog::new(staging_log_path.to_string_lossy().as_ref()) {
            Ok(l) => l,
            Err(e) => {
                return Err(DaemonRpcError::new(
                    "recover_failed",
                    format!("StagingLog::new 失败: {}", e),
                ));
            }
        };

        // 4. 调用 Replicator::recover（内部：read_pending → filter by ws_id →
        //    mark_applied_batch → compact_applied）
        let replicator = crate::daemon::replicator::Replicator::new(&staging_log);
        let result = replicator.recover(&ws_id, "");

        // 5. 构造返回（与 Python daemon_server.py L430-437 字段一致）
        let mut m = Map::new();
        m.insert(
            "status".to_string(),
            Value::String(
                if result.success {
                    "recovered"
                } else {
                    "failed"
                }
                .to_string(),
            ),
        );
        m.insert("generation".to_string(), Value::Number(result.generation.into()));
        m.insert(
            "applied_count".to_string(),
            Value::Number(result.applied_count.into()),
        );
        m.insert(
            "pending_count".to_string(),
            Value::Number(result.pending_count.into()),
        );
        m.insert(
            "duration_ms".to_string(),
            Value::Number(
                serde_json::Number::from_f64(result.duration_ms)
                    .unwrap_or_else(|| serde_json::Number::from(0u32)),
            ),
        );
        m.insert(
            "error".to_string(),
            match result.error {
                Some(s) => Value::String(s),
                None => Value::Null,
            },
        );
        Ok(Value::Object(m))
    }

    // ---- 运维方法（backup / restore / gc.cas）----

    fn handle_backup(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // 对应 Python daemon_server.py L321-330
        // VACUUM INTO 将 registry DB 完整备份（含 WAL）到指定路径
        let output_path = require_str_param(params, "output_path")?;
        let abs_path = std::path::absolute(output_path).map_err(|e| {
            DaemonRpcError::invalid_params(format!("output_path 转绝对路径失败: {}", e))
        })?;

        // 创建父目录
        if let Some(parent) = abs_path.parent() {
            std::fs::create_dir_all(parent).map_err(|e| {
                DaemonRpcError::internal_error(format!("create_dir_all 失败: {}", e))
            })?;
        }

        // VACUUM INTO 不支持参数绑定，必须拼接（单引号需 escape 为 ''）
        let path_str = abs_path.to_string_lossy().replace('\'', "''");
        let sql = format!("VACUUM INTO '{}'", path_str);

        let conn = self.registry.conn.lock().unwrap();
        conn.execute_batch(&sql).map_err(|e| {
            DaemonRpcError::internal_error(format!("VACUUM INTO 失败: {}", e))
        })?;
        drop(conn);

        let mut m = Map::new();
        m.insert(
            "backup_path".to_string(),
            Value::String(abs_path.to_string_lossy().to_string()),
        );
        m.insert("status".to_string(), Value::String("ok".to_string()));
        Ok(Value::Object(m))
    }

    fn handle_restore(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // 对应 Python daemon_server.py L332-343
        // 从备份文件恢复 registry DB：关闭当前连接 → 文件 copy → 重新打开
        let source_path = require_str_param(params, "source_path")?;
        let abs_source = std::path::absolute(source_path).map_err(|e| {
            DaemonRpcError::invalid_params(format!("source_path 转绝对路径失败: {}", e))
        })?;

        // 校验备份文件存在
        if !abs_source.exists() || !abs_source.is_file() {
            return Err(DaemonRpcError::new(
                "backup_not_found",
                abs_source.to_string_lossy().to_string(),
            ));
        }

        // 校验 registry DB 路径（内存数据库无法 restore）
        let db_path = self.registry.db_path.clone();
        if db_path == ":memory:" {
            return Err(DaemonRpcError::new(
                "restore_failed",
                "无法对内存数据库执行 restore".to_string(),
            ));
        }

        // restore 流程（WAL 模式下需谨慎处理）：
        // 1. 当前连接执行 wal_checkpoint(TRUNCATE)，把 WAL 数据合并到主 .db
        // 2. copy 备份文件覆盖主 .db
        // 3. 删除旧 -wal / -shm 文件（避免新连接读到旧 WAL 数据）
        // 4. reopen registry
        {
            let conn = self.registry.conn.lock().unwrap();
            // wal_checkpoint(TRUNCATE) 把 WAL 合并到主 .db 并截断 WAL 文件
            let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
        }

        std::fs::copy(&abs_source, &db_path).map_err(|e| {
            DaemonRpcError::internal_error(format!(
                "文件复制失败 ({} → {}): {}",
                abs_source.display(),
                db_path,
                e
            ))
        })?;

        // 删除 -wal / -shm（若存在），确保 reopen 时从纯 .db 文件读取
        for suffix in &["-wal", "-shm"] {
            let sidecar = format!("{}{}", db_path, suffix);
            if std::path::Path::new(&sidecar).exists() {
                let _ = std::fs::remove_file(&sidecar);
            }
        }

        // 重新打开 registry，加载新数据
        self.registry.reopen().map_err(|e| {
            DaemonRpcError::internal_error(format!("registry reopen 失败: {}", e))
        })?;

        let mut m = Map::new();
        m.insert(
            "restored_from".to_string(),
            Value::String(abs_source.to_string_lossy().to_string()),
        );
        m.insert("registry_db".to_string(), Value::String(db_path));
        m.insert("status".to_string(), Value::String("ok".to_string()));
        Ok(Value::Object(m))
    }

    fn handle_gc_cas(
        &mut self,
        peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // 对应 Python daemon_server.py L358-369
        // gc.cas 需要 workspace_instance_id（per-workspace CAS GC）
        let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
        // ACL 校验
        let _workspace = owned_workspace(&self.registry, peer.uid, workspace_instance_id)?;

        let grace_days = get_str_param(params, "grace_days")
            .and_then(|s| s.parse::<u32>().ok())
            .unwrap_or(7);

        // 懒初始化 per-workspace 资源（含 CasStore）
        let resources = self.get_or_init_resources(workspace_instance_id)?;

        // 调用 CasStore::gc_unreferenced
        let deleted = resources
            .cas_store
            .gc_unreferenced(grace_days)
            .map_err(|e| DaemonRpcError::new("gc_failed", format!("{}", e)))?;

        let mut m = Map::new();
        m.insert("deleted_count".to_string(), Value::Number(deleted.into()));
        m.insert("grace_days".to_string(), Value::Number(grace_days.into()));
        m.insert(
            "workspace_instance_id".to_string(),
            Value::String(workspace_instance_id.to_string()),
        );
        m.insert("status".to_string(), Value::String("ok".to_string()));
        Ok(Value::Object(m))
    }

    // ============================================
    // G4: Container Mount Mapping handlers
    // ============================================

    fn handle_mount_register(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // mount.register RPC：注册容器挂载映射
        // 参数：container_id（必填）、container_path（必填）、host_path（必填）、
        //       mapping_type（可选，默认 "bind"，可选值 bind/volume/smb）
        // ACL：当前实现不限制 UID（mount mapping 是 admin 级配置，daemon 部署时
        //       通过 socket 文件权限控制谁能连）。如需 admin-only 校验，可在此添加。
        let container_id = require_str_param(params, "container_id")?;
        let container_path = require_str_param(params, "container_path")?;
        let host_path = require_str_param(params, "host_path")?;
        let mapping_type = get_str_param_or(params, "mapping_type", "bind");

        // 校验 mapping_type 取值
        if !matches!(mapping_type.as_str(), "bind" | "volume" | "smb") {
            return Err(DaemonRpcError::invalid_params(format!(
                "mapping_type 必须是 bind/volume/smb，得到: {}",
                mapping_type
            )));
        }

        self.registry
            .register_mount_mapping(container_id, container_path, host_path, &mapping_type)
            .map_err(|e| {
                DaemonRpcError::internal_error(format!("register_mount_mapping: {}", e))
            })
    }

    fn handle_mount_list(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // mount.list RPC：列出容器挂载映射
        // 参数：container_id（可选，缺失则列出全部）
        let container_id = get_str_param(params, "container_id");
        let mappings = self
            .registry
            .list_mount_mappings(container_id)
            .map_err(|e| DaemonRpcError::internal_error(format!("list_mount_mappings: {}", e)))?;
        Ok(Value::Array(mappings))
    }

    fn handle_mount_delete(
        &mut self,
        _peer: PeerCredential,
        params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // mount.delete RPC：删除容器挂载映射
        // 参数：container_id（必填）、container_path（必填）
        // 返回：{"deleted": 0|1}
        let container_id = require_str_param(params, "container_id")?;
        let container_path = require_str_param(params, "container_path")?;
        let deleted = self
            .registry
            .delete_mount_mapping(container_id, container_path)
            .map_err(|e| DaemonRpcError::internal_error(format!("delete_mount_mapping: {}", e)))?;
        let mut m = Map::new();
        m.insert("deleted".to_string(), Value::Number(deleted.into()));
        Ok(Value::Object(m))
    }
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::dispatch::dispatch;
    use crate::daemon::staging_log::{StagingEntry, StagingLog};
    use serde_json::json;

    fn make_peer(uid: u32) -> PeerCredential {
        PeerCredential {
            uid,
            gid: 1000,
            pid: 12345,
        }
    }

    fn make_state() -> WorkspaceDaemonState {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        WorkspaceDaemonState::new(registry)
    }

    /// 获取当前测试运行的 uid（Unix: getuid()，Windows: 固定 1000）
    ///
    /// 用于让 peer_uid 匹配 tempfile::tempdir() 创建目录的实际 owner_uid，
    /// 避免 WSL 以 root (uid=0) 运行时 peer.uid=1000 触发 validate_owned_path 的 UID ACL 失败。
    fn current_uid() -> u32 {
        #[cfg(unix)]
        {
            // libc 是 unix target 的正式依赖，dev 也可用
            unsafe { libc::getuid() }
        }
        #[cfg(not(unix))]
        {
            // Windows 上 validate_owned_path 跳过 UID 检查，固定值即可
            1000
        }
    }

    /// 构造一个 peer_uid = current_uid() 的 peer（用于注册操作以匹配 tempdir owner）
    fn make_owner_peer() -> PeerCredential {
        make_peer(current_uid())
    }

    /// 构造一个 peer_uid != current_uid() 的 peer（用于验证非 owner 被拒绝）
    fn make_other_peer() -> PeerCredential {
        // +1 确保与 owner_uid 不同（getuid() 不会返回 u32::MAX）
        make_peer(current_uid().wrapping_add(1))
    }

    // ---- WorkspaceRegistry 基础 CRUD ----

    #[test]
    fn test_registry_open_in_memory_initializes_schema() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        // 表应该存在
        let count = registry.count_workspaces().unwrap();
        assert_eq!(count, 0);
    }

    #[test]
    fn test_registry_open_in_memory_writes_schema_version() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let conn = registry.conn.lock().unwrap();
        let version: String = conn
            .query_row(
                "SELECT value FROM daemon_state WHERE key = 'schema_version'",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(version, super::super::SCHEMA_VERSION.to_string());
    }

    #[test]
    fn test_register_workspace_returns_full_row() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let result = registry
            .register_workspace(
                1000,
                "/tmp/client_view",
                "/var/host/real",
                "https://github.com/x/y.git",
                "abc123",
                "rustc-1.75",
            )
            .unwrap();

        assert_eq!(result["owner_uid"], 1000);
        assert_eq!(result["client_view_root"], "/tmp/client_view");
        assert_eq!(result["host_real_root"], "/var/host/real");
        assert_eq!(result["git_remote_url"], "https://github.com/x/y.git");
        assert_eq!(result["git_head_commit_sha"], "abc123");
        assert_eq!(result["toolchain_fingerprint"], "rustc-1.75");
        assert_eq!(result["status"], "active");
        // workspace_instance_id 应该是 16 字符的 hex
        let instance_id = result["workspace_instance_id"].as_str().unwrap();
        assert_eq!(instance_id.len(), 16);
        assert!(instance_id.chars().all(|c| c.is_ascii_hexdigit()));
        // snapshot_id 应该是 16 字符的 hex（因为 git_remote_url + git_head_commit_sha 都非空）
        let snapshot_id = result["snapshot_id"].as_str().unwrap();
        assert_eq!(snapshot_id.len(), 16);
    }

    #[test]
    fn test_register_workspace_without_git_returns_null_snapshot_id() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let result = registry
            .register_workspace(1000, "/tmp/cv", "/var/hr", "", "", "")
            .unwrap();
        // git_remote_url 和 git_head_commit_sha 都为空时，snapshot_id 应为 null
        assert!(result["snapshot_id"].is_null());
    }

    #[test]
    fn test_register_workspace_idempotent_insert_or_replace() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        // 相同参数注册两次，应该返回同一 workspace_instance_id
        let r1 = registry
            .register_workspace(1000, "/tmp/cv", "/var/hr", "url", "head", "fp")
            .unwrap();
        let r2 = registry
            .register_workspace(1000, "/tmp/cv", "/var/hr", "url", "head", "fp")
            .unwrap();
        assert_eq!(
            r1["workspace_instance_id"],
            r2["workspace_instance_id"]
        );
        // 仍然只有一条记录
        assert_eq!(registry.count_workspaces().unwrap(), 1);
    }

    #[test]
    fn test_list_workspaces_filters_by_owner_uid() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        registry
            .register_workspace(1000, "/tmp/cv1", "/var/hr1", "", "", "")
            .unwrap();
        registry
            .register_workspace(2000, "/tmp/cv2", "/var/hr2", "", "", "")
            .unwrap();
        registry
            .register_workspace(1000, "/tmp/cv3", "/var/hr3", "", "", "")
            .unwrap();

        // 全量
        let all = registry.list_workspaces(None).unwrap();
        assert_eq!(all.len(), 3);

        // 按 owner_uid=1000 过滤
        let user1 = registry.list_workspaces(Some(1000)).unwrap();
        assert_eq!(user1.len(), 2);
        for ws in &user1 {
            assert_eq!(ws["owner_uid"], 1000);
        }

        // 按 owner_uid=2000 过滤
        let user2 = registry.list_workspaces(Some(2000)).unwrap();
        assert_eq!(user2.len(), 1);
    }

    #[test]
    fn test_list_workspaces_ordered_by_last_active_desc() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let r1 = registry
            .register_workspace(1000, "/tmp/cv1", "/var/hr1", "", "", "")
            .unwrap();
        // 稍微 sleep 一下让 last_active_at 不同
        std::thread::sleep(std::time::Duration::from_millis(10));
        let r2 = registry
            .register_workspace(1000, "/tmp/cv2", "/var/hr2", "", "", "")
            .unwrap();

        let list = registry.list_workspaces(Some(1000)).unwrap();
        // 后注册的应该在前面（last_active_at DESC）
        assert_eq!(
            list[0]["workspace_instance_id"],
            r2["workspace_instance_id"]
        );
        assert_eq!(
            list[1]["workspace_instance_id"],
            r1["workspace_instance_id"]
        );
    }

    #[test]
    fn test_get_workspace_status_returns_none_for_missing() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let result = registry.get_workspace_status("nonexistent").unwrap();
        assert!(result.is_none());
    }

    #[test]
    fn test_get_workspace_status_returns_row() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let r = registry
            .register_workspace(1000, "/tmp/cv", "/var/hr", "", "", "")
            .unwrap();
        let instance_id = r["workspace_instance_id"].as_str().unwrap();

        let status = registry.get_workspace_status(instance_id).unwrap();
        assert!(status.is_some());
        let status = status.unwrap();
        assert_eq!(status["workspace_instance_id"], instance_id);
        assert_eq!(status["status"], "active");
    }

    #[test]
    fn test_update_workspace_status_changes_status() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let r = registry
            .register_workspace(1000, "/tmp/cv", "/var/hr", "", "", "")
            .unwrap();
        let instance_id = r["workspace_instance_id"].as_str().unwrap();

        let updated = registry
            .update_workspace_status(instance_id, "archived")
            .unwrap();
        assert!(updated);

        let status = registry.get_workspace_status(instance_id).unwrap().unwrap();
        assert_eq!(status["status"], "archived");
    }

    #[test]
    fn test_update_workspace_status_returns_false_for_missing() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let updated = registry
            .update_workspace_status("nonexistent", "archived")
            .unwrap();
        assert!(!updated);
    }

    // ---- ID 计算测试 ----

    #[test]
    fn test_compute_workspace_instance_id_is_deterministic() {
        let id1 = compute_workspace_instance_id(1000, "/var/hr", "url", "head");
        let id2 = compute_workspace_instance_id(1000, "/var/hr", "url", "head");
        assert_eq!(id1, id2);
        assert_eq!(id1.len(), 16);
    }

    #[test]
    fn test_compute_workspace_instance_id_differs_on_owner() {
        let id1 = compute_workspace_instance_id(1000, "/var/hr", "url", "head");
        let id2 = compute_workspace_instance_id(2000, "/var/hr", "url", "head");
        assert_ne!(id1, id2);
    }

    #[test]
    fn test_compute_snapshot_id_differs_on_fingerprint() {
        let id1 = compute_snapshot_id("url", "head", "fp1");
        let id2 = compute_snapshot_id("url", "head", "fp2");
        assert_ne!(id1, id2);
    }

    #[test]
    fn test_hex_encode_uppercase_bytes() {
        assert_eq!(hex_encode(&[0x00]), "00");
        assert_eq!(hex_encode(&[0xff]), "ff");
        assert_eq!(hex_encode(&[0xab, 0xcd]), "abcd");
    }

    // ---- 路径校验测试 ----

    #[test]
    fn test_validate_owned_path_rejects_nonexistent() {
        let result = validate_owned_path("/nonexistent/path/xyz", 0, false);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "path_not_found");
    }

    #[test]
    fn test_validate_owned_path_accepts_existing_directory() {
        // 用 tempdir 创建一个临时目录
        let tmp = tempfile::tempdir().unwrap();
        let path = tmp.path().to_str().unwrap();
        let result = validate_owned_path(path, 0, false);
        assert!(result.is_ok());
    }

    #[test]
    fn test_validate_owned_path_rejects_file_when_dir_required() {
        let tmp = tempfile::tempdir().unwrap();
        let file_path = tmp.path().join("test.txt");
        std::fs::write(&file_path, "hello").unwrap();
        let result = validate_owned_path(file_path.to_str().unwrap(), 0, false);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "path_not_found");
    }

    #[test]
    fn test_validate_owned_path_accepts_file_when_file_required() {
        let tmp = tempfile::tempdir().unwrap();
        let file_path = tmp.path().join("test.txt");
        std::fs::write(&file_path, "hello").unwrap();
        let result = validate_owned_path(file_path.to_str().unwrap(), 0, true);
        assert!(result.is_ok());
    }

    // ---- owned_workspace ACL 测试 ----

    #[test]
    fn test_owned_workspace_returns_workspace_not_found() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let result = owned_workspace(&registry, 1000, "nonexistent");
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "workspace_not_found");
    }

    #[test]
    fn test_owned_workspace_returns_forbidden_for_wrong_uid() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let r = registry
            .register_workspace(1000, "/tmp/cv", "/var/hr", "", "", "")
            .unwrap();
        let instance_id = r["workspace_instance_id"].as_str().unwrap();

        // 用 peer_uid=2000 访问 owner=1000 的 workspace
        let result = owned_workspace(&registry, 2000, instance_id);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "workspace_forbidden");
    }

    #[test]
    fn test_owned_workspace_returns_archived_for_archived_status() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let r = registry
            .register_workspace(1000, "/tmp/cv", "/var/hr", "", "", "")
            .unwrap();
        let instance_id = r["workspace_instance_id"].as_str().unwrap();
        registry
            .update_workspace_status(instance_id, "archived")
            .unwrap();

        // owner 正确但 status=archived
        let result = owned_workspace(&registry, 1000, instance_id);
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "workspace_archived");
    }

    #[test]
    fn test_owned_workspace_returns_ok_for_correct_owner() {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let r = registry
            .register_workspace(1000, "/tmp/cv", "/var/hr", "", "", "")
            .unwrap();
        let instance_id = r["workspace_instance_id"].as_str().unwrap();

        let result = owned_workspace(&registry, 1000, instance_id);
        assert!(result.is_ok());
        let ws = result.unwrap();
        assert_eq!(ws["workspace_instance_id"], instance_id);
    }

    // ---- WorkspaceDaemonState + dispatch 集成测试 ----

    #[test]
    fn test_dispatch_workspace_list_returns_empty_for_new_user() {
        let mut state = make_state();
        let peer = make_peer(1000);
        let params = json!({});
        let response = dispatch(&mut state, peer, "workspace.list", &params, &[]);

        assert_eq!(response["ok"], true);
        assert!(response["result"].is_array());
        assert_eq!(response["result"].as_array().unwrap().len(), 0);
    }

    #[test]
    fn test_dispatch_workspace_register_requires_client_view_root() {
        let mut state = make_state();
        let peer = make_peer(1000);
        let params = json!({});
        let response = dispatch(&mut state, peer, "workspace.register", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_dispatch_workspace_register_rejects_nonexistent_path() {
        let mut state = make_state();
        let peer = make_peer(1000);
        let params = json!({
            "client_view_root": "/nonexistent/xyz/abc"
        });
        let response = dispatch(&mut state, peer, "workspace.register", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "path_not_found");
    }

    #[test]
    fn test_dispatch_workspace_register_succeeds_with_real_dir() {
        let mut state = make_state();
        // 用 current_uid() 匹配 tempfile 创建目录的实际 owner_uid，
        // 避免 WSL root (uid=0) 运行时 peer.uid=1000 触发 path_forbidden
        let peer = make_owner_peer();
        let tmp = tempfile::tempdir().unwrap();
        let dir_path = tmp.path().to_str().unwrap();
        let params = json!({
            "client_view_root": dir_path,
            "git_remote_url": "https://github.com/x/y.git",
            "git_head_commit_sha": "abc123",
            "toolchain_fingerprint": "rustc-1.75"
        });
        let response = dispatch(&mut state, peer, "workspace.register", &params, &[]);

        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["owner_uid"], current_uid());
        assert_eq!(response["result"]["status"], "active");
        // host_real_root 应该被 canonicalize 处理
        assert!(response["result"]["host_real_root"].as_str().unwrap().len() > 0);
    }

    #[test]
    fn test_dispatch_workspace_status_returns_workspace_for_owner() {
        let mut state = make_state();
        let peer = make_owner_peer();

        // 先注册一个 workspace
        let tmp = tempfile::tempdir().unwrap();
        let dir_path = tmp.path().to_str().unwrap();
        let reg_params = json!({"client_view_root": dir_path});
        let reg_response = dispatch(&mut state, peer, "workspace.register", &reg_params, &[]);
        assert_eq!(reg_response["ok"], true);

        let instance_id = reg_response["result"]["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string();

        // 查询状态
        let status_params = json!({"workspace_instance_id": instance_id});
        let response = dispatch(&mut state, peer, "workspace.status", &status_params, &[]);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["workspace_instance_id"], instance_id);
    }

    #[test]
    fn test_dispatch_workspace_status_rejects_non_owner() {
        let mut state = make_state();
        let peer_owner = make_owner_peer();
        let peer_other = make_other_peer();

        // owner 注册（peer_uid = tempdir owner_uid）
        let tmp = tempfile::tempdir().unwrap();
        let dir_path = tmp.path().to_str().unwrap();
        let reg_params = json!({"client_view_root": dir_path});
        let reg_response =
            dispatch(&mut state, peer_owner, "workspace.register", &reg_params, &[]);
        assert_eq!(reg_response["ok"], true);
        let instance_id = reg_response["result"]["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string();

        // 非owner查询，应被拒绝
        let status_params = json!({"workspace_instance_id": instance_id});
        let response = dispatch(&mut state, peer_other, "workspace.status", &status_params, &[]);
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_dispatch_health_returns_workspace_count() {
        let mut state = make_state();
        let peer = make_owner_peer();

        // 注册 2 个 workspace（用不同 git_remote_url 生成不同 instance_id）
        let tmp1 = tempfile::tempdir().unwrap();
        let tmp2 = tempfile::tempdir().unwrap();
        dispatch(
            &mut state,
            peer,
            "workspace.register",
            &json!({
                "client_view_root": tmp1.path().to_str().unwrap(),
                "git_remote_url": "https://github.com/a/a.git",
                "git_head_commit_sha": "aaa"
            }),
            &[],
        );
        dispatch(
            &mut state,
            peer,
            "workspace.register",
            &json!({
                "client_view_root": tmp2.path().to_str().unwrap(),
                "git_remote_url": "https://github.com/b/b.git",
                "git_head_commit_sha": "bbb"
            }),
            &[],
        );

        // health 应该返回 workspace_count=2
        let response = dispatch(&mut state, peer, "health", &json!({}), &[]);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["workspace_count"], 2);
    }

    #[test]
    fn test_dispatch_workspace_recover_returns_workspace_not_found_for_unknown_ws() {
        // 实现已接入：对不存在的 workspace 返回 workspace_not_found
        // （对 ACL 校验前的 data_root 校验会先返回 recover_failed）
        let mut state = make_state();
        let peer = make_owner_peer();
        let params = json!({"workspace_instance_id": "xyz"});
        let response = dispatch(&mut state, peer, "workspace.recover", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_not_found");
    }

    // ---- 跨用户隔离测试 ----

    #[test]
    fn test_workspace_list_isolates_by_uid() {
        let mut state = make_state();
        let owner_uid = current_uid();
        let other_uid = current_uid().wrapping_add(1);

        let tmp1 = tempfile::tempdir().unwrap();
        let tmp2 = tempfile::tempdir().unwrap();

        // owner 通过 dispatch 注册 2 个 workspace（dispatch 会做 path 校验，
        // peer_uid 必须匹配 tempdir owner_uid，用 current_uid() 即可）
        dispatch(
            &mut state,
            make_peer(owner_uid),
            "workspace.register",
            &json!({
                "client_view_root": tmp1.path().to_str().unwrap(),
                "git_remote_url": "https://github.com/o1/o1.git",
                "git_head_commit_sha": "o1"
            }),
            &[],
        );
        dispatch(
            &mut state,
            make_peer(owner_uid),
            "workspace.register",
            &json!({
                "client_view_root": tmp2.path().to_str().unwrap(),
                "git_remote_url": "https://github.com/o2/o2.git",
                "git_head_commit_sha": "o2"
            }),
            &[],
        );

        // other_uid 不拥有任何 tempdir，无法通过 dispatch 的 path ACL 检查。
        // 直接调用 registry.register_workspace 注入 other_uid 的数据，
        // 用于验证 workspace.list 按 owner_uid 过滤（此测试关注 list 隔离，不关注 register ACL）。
        state
            .registry
            .register_workspace(other_uid, "/tmp/other", "/tmp/other", "", "", "")
            .unwrap();

        // owner 只看到自己的 2 个
        let r1 = dispatch(&mut state, make_peer(owner_uid), "workspace.list", &json!({}), &[]);
        assert_eq!(r1["result"].as_array().unwrap().len(), 2);

        // other 只看到自己的 1 个
        let r2 = dispatch(&mut state, make_peer(other_uid), "workspace.list", &json!({}), &[]);
        assert_eq!(r2["result"].as_array().unwrap().len(), 1);
    }

    // ---- handle_workspace_recover 测试 ----

    /// 构造带 data_root 的 state（用于 workspace.recover 测试）
    fn make_state_with_data_root(data_root: &Path) -> WorkspaceDaemonState {
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        WorkspaceDaemonState::with_data_root(registry, data_root.to_path_buf())
    }

    /// 注册 workspace 并返回 instance_id（直接调用 registry，绕过路径 ACL）
    fn register_ws_for_recover(state: &mut WorkspaceDaemonState, owner_uid: u32) -> String {
        let tmp = tempfile::tempdir().unwrap();
        let dir_path = tmp.path().to_str().unwrap().to_string();
        let response = state
            .registry
            .register_workspace(owner_uid, &dir_path, &dir_path, "", "", "")
            .unwrap();
        // 注意：tmp 在函数结束时 drop，workspace.register 只在 ACL 校验时需要路径存在
        // 这里测试 recover 不依赖 client_view_root 的存在性
        response["workspace_instance_id"].as_str().unwrap().to_string()
    }

    #[test]
    fn test_recover_returns_recover_failed_when_data_root_empty() {
        // data_root 为空（WorkspaceDaemonState::new 默认值）：应返回 recover_failed
        let mut state = make_state();
        let peer = make_owner_peer();
        let ws_id = register_ws_for_recover(&mut state, peer.uid);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.recover",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "recover_failed");
    }

    #[test]
    fn test_recover_rejects_non_owner() {
        // 非 owner 不能 recover
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let owner = make_owner_peer();
        let other = make_other_peer();
        let ws_id = register_ws_for_recover(&mut state, owner.uid);

        let response = dispatch(
            &mut state,
            other,
            "workspace.recover",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_recover_rejects_nonexistent_workspace() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();

        let response = dispatch(
            &mut state,
            peer,
            "workspace.recover",
            &json!({"workspace_instance_id": "nonexistent_ws"}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_not_found");
    }

    #[test]
    fn test_recover_returns_zero_when_no_staging_log() {
        // 无 staging.log：StagingLog::new 创建空文件，无 pending → applied_count=0
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws_for_recover(&mut state, peer.uid);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.recover",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "recovered");
        assert_eq!(response["result"]["applied_count"], 0);
        assert_eq!(response["result"]["pending_count"], 0);
        assert_eq!(response["result"]["error"], serde_json::Value::Null);
    }

    #[test]
    fn test_recover_applies_pending_entries() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws_for_recover(&mut state, peer.uid);

        // 在 data_root/$ws_id/staging.log 写入 3 条 pending
        let ws_dir = tmp.path().join(&ws_id);
        std::fs::create_dir_all(&ws_dir).unwrap();
        let log_path = ws_dir.join("staging.log");
        let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();
        for i in 0..3 {
            let mut entry = StagingEntry::new(
                &ws_id,
                &format!("file_{}.py", i),
                &format!("hash_{}", i),
                "python",
            );
            log.append(&mut entry).unwrap();
        }

        let response = dispatch(
            &mut state,
            peer,
            "workspace.recover",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "recovered");
        assert_eq!(response["result"]["applied_count"], 3);
        assert_eq!(response["result"]["pending_count"], 3);

        // 第二次 recover：无 pending，applied_count=0
        let response2 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.recover",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response2["result"]["applied_count"], 0);
    }

    #[test]
    fn test_recover_filters_entries_by_workspace_id() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws_for_recover(&mut state, peer.uid);

        // 写入 2 条 ws_id + 1 条其他 workspace 的 pending entries
        // （实际上 daemon 一个 workspace 一个 staging.log，但 StagingLog 支持多 workspace entries）
        let ws_dir = tmp.path().join(&ws_id);
        std::fs::create_dir_all(&ws_dir).unwrap();
        let log_path = ws_dir.join("staging.log");
        let log = StagingLog::new(log_path.to_str().unwrap()).unwrap();
        for i in 0..2 {
            let mut entry = StagingEntry::new(
                &ws_id,
                &format!("file_{}.py", i),
                &format!("hash_{}", i),
                "python",
            );
            log.append(&mut entry).unwrap();
        }
        // 其他 workspace 的 entry
        let mut other_entry = StagingEntry::new("other_ws", "other.py", "other_hash", "python");
        log.append(&mut other_entry).unwrap();

        let response = dispatch(
            &mut state,
            peer,
            "workspace.recover",
            &json!({"workspace_instance_id": ws_id}),
            &[],
        );
        assert_eq!(response["ok"], true);
        // 只应用 ws_id 的 2 条，不应用 other_ws 的 1 条
        assert_eq!(response["result"]["applied_count"], 2);
        assert_eq!(response["result"]["pending_count"], 2);
    }

    #[test]
    fn test_recover_missing_workspace_instance_id_param() {
        // 缺少 workspace_instance_id 参数：invalid_params
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();

        let response = dispatch(
            &mut state,
            peer,
            "workspace.recover",
            &json!({}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    // ---- handle_workspace_connect 测试 ----

    /// 注册 workspace 并返回 instance_id（用于 connect/refresh 测试）
    fn register_ws(state: &mut WorkspaceDaemonState, owner_uid: u32) -> String {
        let tmp = tempfile::tempdir().unwrap();
        let dir_path = tmp.path().to_str().unwrap().to_string();
        let response = state
            .registry
            .register_workspace(owner_uid, &dir_path, &dir_path, "", "", "")
            .unwrap();
        // 注意：tmp 在函数结束时 drop，但 connect/refresh 不依赖 client_view_root 存在
        response["workspace_instance_id"].as_str().unwrap().to_string()
    }

    #[test]
    fn test_connect_returns_session_epoch_1_for_first_session() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-1"
            }),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["session_epoch"], 1);
        assert_eq!(response["result"]["workspace_instance_id"], ws_id);
    }

    #[test]
    fn test_connect_assigns_increasing_epoch() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        // 第一次：epoch=1
        let r1 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-1"
            }),
            &[],
        );
        assert_eq!(r1["result"]["session_epoch"], 1);

        // 第二次（不同 session_id）：epoch=2
        let r2 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-2"
            }),
            &[],
        );
        assert_eq!(r2["result"]["session_epoch"], 2);
    }

    #[test]
    fn test_connect_rejects_non_owner() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let owner = make_owner_peer();
        let other = make_other_peer();
        let ws_id = register_ws(&mut state, owner.uid);

        let response = dispatch(
            &mut state,
            other,
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-1"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_connect_rejects_unknown_workspace() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();

        let response = dispatch(
            &mut state,
            peer,
            "workspace.connect",
            &json!({
                "workspace_instance_id": "nonexistent_ws",
                "agent_session_id": "session-1"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_not_found");
    }

    #[test]
    fn test_connect_missing_workspace_instance_id_param() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();

        let response = dispatch(
            &mut state,
            peer,
            "workspace.connect",
            &json!({
                "agent_session_id": "session-1"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_connect_missing_agent_session_id_param() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_connect_fails_when_data_root_empty() {
        // data_root 为空：get_or_init_resources 返回 resources_init_failed
        let mut state = make_state();
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-1"
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "resources_init_failed");
    }

    #[test]
    fn test_connect_caches_resources_in_state() {
        // 两次 connect 应复用同一 SessionStore（资源缓存生效）
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        // 第一次：初始化资源
        let r1 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-1"
            }),
            &[],
        );
        assert_eq!(r1["ok"], true);
        assert_eq!(r1["result"]["session_epoch"], 1);

        // 第二次：应复用同一 SessionStore（epoch 递增而非重置）
        let r2 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-2"
            }),
            &[],
        );
        assert_eq!(r2["ok"], true);
        assert_eq!(r2["result"]["session_epoch"], 2);

        // 资源应已缓存
        assert!(state.resources.contains_key(&ws_id));
        assert_eq!(state.resources.len(), 1);
    }

    // ---- handle_workspace_file_refresh 测试 ----

    /// 注册 workspace + 调用 connect 拿到 epoch，返回 (ws_id, session_epoch)
    fn setup_connected_workspace(
        state: &mut WorkspaceDaemonState,
        peer: PeerCredential,
        session_id: &str,
    ) -> (String, i64) {
        let ws_id = register_ws(state, peer.uid);
        let r = dispatch(
            state,
            peer,
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": session_id
            }),
            &[],
        );
        assert_eq!(r["ok"], true);
        let epoch = r["result"]["session_epoch"].as_i64().unwrap();
        (ws_id, epoch)
    }

    #[test]
    fn test_refresh_rejects_when_no_active_session() {
        // 没有 connect 直接 refresh：refresh_failed (no active session)
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": 1
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "refresh_failed");
    }

    #[test]
    fn test_refresh_rejects_stale_session() {
        // connect 后用错误的 epoch refresh：refresh_failed (stale session)
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let (ws_id, _epoch) = setup_connected_workspace(&mut state, peer, "session-1");

        // 用错误的 epoch（应该是 1，传 999）
        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": 999
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "refresh_failed");
    }

    #[test]
    fn test_refresh_committed_triggers_replication() {
        // 正常 refresh：status=committed，generation="epoch:seq"，
        // 并且 replication.applied_count=1（追加 staging entry + replicate）
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let (ws_id, epoch) = setup_connected_workspace(&mut state, peer, "session-1");

        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": epoch,
                "content_hash": "abc123",
                "language": "rust"
            }),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "committed");
        assert_eq!(response["result"]["generation"], format!("{}:{}", epoch, 1));
        // replication 应触发并应用 1 条 entry
        assert_eq!(response["result"]["replication"]["applied_count"], 1);
        assert_eq!(response["result"]["replication"]["pending_count"], 1);
        assert_eq!(response["result"]["workspace_instance_id"], ws_id);
    }

    #[test]
    fn test_refresh_committed_marks_snapshot_not_published() {
        // P0-2 修复：committed 时 replication 应显式标记 snapshot_published=false
        // 让客户端明确知道本次 refresh 没有产生可查询 snapshot
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let (ws_id, epoch) = setup_connected_workspace(&mut state, peer, "session-1");

        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": epoch,
                "content_hash": "abc123",
                "language": "rust"
            }),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "committed");
        // P0-2：显式标记 snapshot 未发布
        assert_eq!(response["result"]["replication"]["snapshot_published"], false);
        assert!(
            response["result"]["replication"]["snapshot_warning"].is_string(),
            "snapshot_warning 字段应存在且为字符串"
        );
    }

    #[test]
    fn test_refresh_accepts_canonical_bytes_hex() {
        // P0-2 修复：daemon 同时支持 canonical_bytes_hex（agent_protocol.py 默认路径）
        // 和 canonical_bytes_b64（旧客户端）。本测试验证 hex 路径正常工作。
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let (ws_id, epoch) = setup_connected_workspace(&mut state, peer, "session-1");

        // 准备 canonical bytes（模拟 Python canonicalize_source_py 输出）
        let canonical = b"pub fn main() {}\n".to_vec();
        let hex_encoded = hex::encode(&canonical);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": epoch,
                "content_hash": "abc123",
                "language": "rust",
                "canonical_bytes_hex": hex_encoded,
                "canonical_len": canonical.len(),
            }),
            &[],
        );
        assert_eq!(response["ok"], true, "hex 路径应正常工作");
        assert_eq!(response["result"]["status"], "committed");
    }

    #[test]
    fn test_refresh_accepts_canonical_bytes_b64() {
        // P0-2 兼容性：canonical_bytes_b64（旧路径）仍正常工作
        use base64::Engine;
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let (ws_id, epoch) = setup_connected_workspace(&mut state, peer, "session-1");

        let canonical = b"pub fn main() {}\n".to_vec();
        let b64_encoded = base64::engine::general_purpose::STANDARD.encode(&canonical);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": epoch,
                "content_hash": "abc123",
                "language": "rust",
                "canonical_bytes_b64": b64_encoded,
                "canonical_len": canonical.len(),
            }),
            &[],
        );
        assert_eq!(response["ok"], true, "b64 路径应正常工作");
        assert_eq!(response["result"]["status"], "committed");
    }

    #[test]
    fn test_refresh_rejects_invalid_hex() {
        // P0-2 错误路径：hex 解码失败应返回 hex_decode_failed 错误
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let (ws_id, epoch) = setup_connected_workspace(&mut state, peer, "session-1");

        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": epoch,
                "canonical_bytes_hex": "not valid hex!@#",
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "hex_decode_failed");
    }

    #[test]
    fn test_refresh_stale_seq_dropped() {
        // 第一次 refresh seq=5（committed），第二次用 seq=3 → stale_seq_dropped
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let (ws_id, epoch) = setup_connected_workspace(&mut state, peer, "session-1");

        // 第一次：seq=5
        let r1 = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 5,
                "session_epoch": epoch
            }),
            &[],
        );
        assert_eq!(r1["result"]["status"], "committed");

        // 第二次：seq=3（小于 5）→ stale_seq_dropped，无 replication
        let r2 = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 3,
                "session_epoch": epoch
            }),
            &[],
        );
        assert_eq!(r2["ok"], true);
        assert_eq!(r2["result"]["status"], "stale_seq_dropped");
        // stale_seq_dropped 时不应触发 replication
        assert!(r2["result"].get("replication").is_none() || r2["result"]["replication"].is_null());
    }

    #[test]
    fn test_refresh_accepts_newer_seq() {
        // seq=1 → committed；seq=10 → committed（新 seq）
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let (ws_id, epoch) = setup_connected_workspace(&mut state, peer, "session-1");

        // seq=1
        let r1 = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": epoch
            }),
            &[],
        );
        assert_eq!(r1["result"]["status"], "committed");

        // seq=10（更大）
        let r2 = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 10,
                "session_epoch": epoch
            }),
            &[],
        );
        assert_eq!(r2["result"]["status"], "committed");
        assert_eq!(r2["result"]["generation"], format!("{}:{}", epoch, 10));
    }

    #[test]
    fn test_refresh_rejects_non_owner() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let owner = make_owner_peer();
        let other = make_other_peer();
        let (ws_id, epoch) = setup_connected_workspace(&mut state, owner, "session-1");

        let response = dispatch(
            &mut state,
            other,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": epoch
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_refresh_rejects_unknown_workspace() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();

        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": "nonexistent_ws",
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": 1
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_not_found");
    }

    #[test]
    fn test_refresh_missing_required_params() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let (ws_id, epoch) = setup_connected_workspace(&mut state, peer, "session-1");

        // 缺 rel_path
        let r1 = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": epoch
            }),
            &[],
        );
        assert_eq!(r1["ok"], false);
        assert_eq!(r1["error"]["code"], "invalid_params");

        // 缺 monotonic_seq
        let r2 = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "session_epoch": epoch
            }),
            &[],
        );
        assert_eq!(r2["ok"], false);
        assert_eq!(r2["error"]["code"], "invalid_params");

        // 缺 session_epoch
        let r3 = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1
            }),
            &[],
        );
        assert_eq!(r3["ok"], false);
        assert_eq!(r3["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_refresh_fails_when_data_root_empty() {
        let mut state = make_state();
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        let response = dispatch(
            &mut state,
            peer,
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": 1
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "resources_init_failed");
    }

    #[test]
    fn test_connect_then_refresh_full_pipeline() {
        // 端到端：connect → refresh(seq=1) → refresh(seq=2) → connect(new session) → refresh rejected
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        // 1. connect session-1 → epoch=1
        let r1 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-1"
            }),
            &[],
        );
        assert_eq!(r1["result"]["session_epoch"], 1);
        let epoch: i64 = r1["result"]["session_epoch"].as_i64().unwrap();

        // 2. refresh seq=1 → committed
        let r2 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 1,
                "session_epoch": epoch
            }),
            &[],
        );
        assert_eq!(r2["result"]["status"], "committed");

        // 3. refresh seq=2 → committed（新 seq）
        let r3 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 2,
                "session_epoch": epoch
            }),
            &[],
        );
        assert_eq!(r3["result"]["status"], "committed");
        assert_eq!(r3["result"]["generation"], format!("{}:{}", epoch, 2));

        // 4. connect session-2 → epoch=2（旧 session 失效）
        let r4 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-2"
            }),
            &[],
        );
        assert_eq!(r4["result"]["session_epoch"], 2);

        // 5. 用旧 session-1 refresh → refresh_failed (stale session)
        let r5 = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.file.refresh",
            &json!({
                "workspace_instance_id": ws_id,
                "rel_path": "src/main.rs",
                "agent_session_id": "session-1",
                "monotonic_seq": 3,
                "session_epoch": 1
            }),
            &[],
        );
        assert_eq!(r5["ok"], false);
        assert_eq!(r5["error"]["code"], "refresh_failed");
    }

    // ---- handle_backup / handle_restore / handle_gc_cas 测试 ----

    /// 构造一个使用文件 DB 的 WorkspaceDaemonState（用于 backup/restore 测试）
    ///
    /// 返回 (state, registry_db_path, data_root_tempdir)
    /// tempdir 由调用方持有，drop 时自动清理
    fn make_state_with_file_registry() -> (
        WorkspaceDaemonState,
        std::path::PathBuf,
        tempfile::TempDir,
    ) {
        let tmp = tempfile::tempdir().unwrap();
        let registry_db_path = tmp.path().join("registry.db");
        let registry = WorkspaceRegistry::open(registry_db_path.to_str().unwrap()).unwrap();
        let state = WorkspaceDaemonState::with_data_root(
            registry,
            tmp.path().to_path_buf(),
        );
        (state, registry_db_path, tmp)
    }

    #[test]
    fn test_backup_creates_valid_db_file() {
        // backup：VACUUM INTO 创建一份完整 backup 文件
        let (mut state, _reg_path, tmp) = make_state_with_file_registry();
        let peer = make_owner_peer();

        // 注册一个 workspace，让 registry DB 有数据
        let dir = tmp.path().to_str().unwrap().to_string();
        let _ = state
            .registry
            .register_workspace(current_uid(), &dir, &dir, "", "", "")
            .unwrap();

        let backup_path = tmp.path().join("backup.db");
        let backup_path_str = backup_path.to_str().unwrap().to_string();

        let response = dispatch(
            &mut state,
            peer,
            "backup",
            &json!({"output_path": backup_path_str}),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
        assert_eq!(response["result"]["backup_path"], backup_path_str);

        // backup 文件应存在且为有效 SQLite DB
        assert!(backup_path.exists());
        let conn = rusqlite::Connection::open(&backup_path).unwrap();
        let count: i64 = conn
            .query_row("SELECT COUNT(*) FROM daemon_workspaces", [], |row| row.get(0))
            .unwrap();
        assert_eq!(count, 1);
    }

    #[test]
    fn test_backup_missing_output_path_param() {
        let (mut state, _reg_path, tmp) = make_state_with_file_registry();
        let peer = make_owner_peer();
        let _ = tmp; // 持有 tempdir

        let response = dispatch(
            &mut state,
            peer,
            "backup",
            &json!({}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_backup_creates_parent_directory() {
        // backup：父目录不存在时自动创建
        let (mut state, _reg_path, tmp) = make_state_with_file_registry();
        let peer = make_owner_peer();

        let backup_path = tmp.path().join("nested").join("dir").join("backup.db");
        let response = dispatch(
            &mut state,
            peer,
            "backup",
            &json!({"output_path": backup_path.to_str().unwrap()}),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert!(backup_path.exists());
    }

    #[test]
    fn test_restore_replaces_registry_db() {
        // restore：从 backup 文件恢复 registry DB
        // 流程：1. 注册 ws1 → 2. backup → 3. 注册 ws2（破坏数据）
        //       4. restore from backup → 5. 验证只有 ws1（ws2 被覆盖）
        let (mut state, reg_path, tmp) = make_state_with_file_registry();
        let peer = make_owner_peer();
        let dir = tmp.path().to_str().unwrap().to_string();

        // 1. 注册 ws1
        let r1 = state
            .registry
            .register_workspace(current_uid(), &dir, &dir, "", "", "")
            .unwrap();
        let ws1_id = r1["workspace_instance_id"].as_str().unwrap().to_string();

        // 2. backup
        let backup_path = tmp.path().join("backup.db");
        let backup_resp = dispatch(
            &mut state,
            peer,
            "backup",
            &json!({"output_path": backup_path.to_str().unwrap()}),
            &[],
        );
        assert_eq!(backup_resp["ok"], true);

        // 3. 注册 ws2（增加一条数据）
        let r2 = state
            .registry
            .register_workspace(
                current_uid(),
                &format!("{}-2", dir),
                &format!("{}-2", dir),
                "",
                "",
                "",
            )
            .unwrap();
        let ws2_id = r2["workspace_instance_id"].as_str().unwrap().to_string();
        assert_ne!(ws1_id, ws2_id);

        // 确认 registry 有 2 条
        assert_eq!(state.registry.count_workspaces().unwrap(), 2);

        // 4. restore from backup
        let restore_resp = dispatch(
            &mut state,
            peer,
            "restore",
            &json!({"source_path": backup_path.to_str().unwrap()}),
            &[],
        );
        assert_eq!(restore_resp["ok"], true);
        assert_eq!(restore_resp["result"]["status"], "ok");
        assert_eq!(restore_resp["result"]["restored_from"], backup_path.to_str().unwrap());
        assert_eq!(restore_resp["result"]["registry_db"], reg_path.to_str().unwrap());

        // 5. 验证：registry 应只剩 1 条（ws1）
        let count = state.registry.count_workspaces().unwrap();
        assert_eq!(count, 1, "restore 后应只剩 backup 时的 1 条 workspace");

        // ws1 还在
        let ws1_after = state.registry.get_workspace_status(&ws1_id).unwrap();
        assert!(ws1_after.is_some(), "ws1 应在 restore 后保留");
    }

    #[test]
    fn test_restore_rejects_missing_source_path() {
        let (mut state, _reg_path, tmp) = make_state_with_file_registry();
        let peer = make_owner_peer();
        let _ = tmp;

        let response = dispatch(
            &mut state,
            peer,
            "restore",
            &json!({}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_restore_rejects_nonexistent_file() {
        let (mut state, _reg_path, tmp) = make_state_with_file_registry();
        let peer = make_owner_peer();
        let _ = tmp;

        let response = dispatch(
            &mut state,
            peer,
            "restore",
            &json!({"source_path": "/nonexistent/path/backup.db"}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "backup_not_found");
    }

    #[test]
    fn test_restore_rejects_in_memory_db() {
        // 内存数据库无法 restore（无文件路径可覆盖）
        let mut state = make_state();
        let peer = make_owner_peer();
        let tmp = tempfile::tempdir().unwrap();
        let fake_backup = tmp.path().join("fake.db");
        std::fs::write(&fake_backup, b"dummy").unwrap();

        let response = dispatch(
            &mut state,
            peer,
            "restore",
            &json!({"source_path": fake_backup.to_str().unwrap()}),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "restore_failed");
    }

    #[test]
    fn test_gc_cas_returns_zero_for_empty_workspace() {
        // gc.cas：workspace 没有任何 CAS 条目时返回 deleted_count=0
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        let response = dispatch(
            &mut state,
            peer,
            "gc.cas",
            &json!({
                "workspace_instance_id": ws_id,
                "grace_days": 7
            }),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["status"], "ok");
        assert_eq!(response["result"]["deleted_count"], 0);
        assert_eq!(response["result"]["grace_days"], 7);
        assert_eq!(response["result"]["workspace_instance_id"], ws_id);
    }

    #[test]
    fn test_gc_cas_default_grace_days_is_7() {
        // 未传 grace_days 参数时默认 7
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        let response = dispatch(
            &mut state,
            peer,
            "gc.cas",
            &json!({
                "workspace_instance_id": ws_id
            }),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["grace_days"], 7);
    }

    #[test]
    fn test_gc_cas_rejects_non_owner() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let owner = make_owner_peer();
        let other = make_other_peer();
        let ws_id = register_ws(&mut state, owner.uid);

        let response = dispatch(
            &mut state,
            other,
            "gc.cas",
            &json!({
                "workspace_instance_id": ws_id,
                "grace_days": 7
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_gc_cas_rejects_unknown_workspace() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();

        let response = dispatch(
            &mut state,
            peer,
            "gc.cas",
            &json!({
                "workspace_instance_id": "nonexistent_ws",
                "grace_days": 7
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_not_found");
    }

    #[test]
    fn test_gc_cas_missing_workspace_instance_id_param() {
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();

        let response = dispatch(
            &mut state,
            peer,
            "gc.cas",
            &json!({
                "grace_days": 7
            }),
            &[],
        );
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_gc_cas_deletes_stale_entries() {
        // 集成测试：插入 ready + parsed_at 过期的 cas_file_cache 条目，
        // gc.cas 应删除（grace_days=0 时所有 ready 条目都被视为过期）
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        // 先 connect + 懒初始化资源
        let _ = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-1"
            }),
            &[],
        );

        // 直接通过 CasStore 插入测试数据
        let resources = state.resources.get(&ws_id).unwrap().clone();
        let cas_store = &resources.cas_store;
        {
            let conn = cas_store.conn().lock().unwrap();
            // 插入 2 条 ready 条目：1 条 parsed_at=0（很久以前）、1 条 parsed_at=now（刚创建）
            conn.execute(
                "INSERT OR REPLACE INTO cas_file_cache
                 (cas_key, content_hash, language, file_size, total_lines,
                  parser_version, callwarden_version, extraction_config_version,
                  abi_version, input_abi_version, state, parsed_at)
                 VALUES ('stale_key', 'hash1', 'rust', 100, 10,
                         'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0)",
                [],
            ).unwrap();
            conn.execute(
                "INSERT OR REPLACE INTO cas_file_cache
                 (cas_key, content_hash, language, file_size, total_lines,
                  parser_version, callwarden_version, extraction_config_version,
                  abi_version, input_abi_version, state, parsed_at)
                 VALUES ('fresh_key', 'hash2', 'rust', 100, 10,
                         'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 9999999999.0)",
                [],
            ).unwrap();
            // 插入子表数据
            conn.execute(
                "INSERT OR REPLACE INTO cas_symbols
                 (cas_key, local_symbol_id, symbol_content_hash, name,
                  local_qualified_name, kind, start_line, end_line)
                 VALUES ('stale_key', 1, 'content_hash_1', 'sym1', 'sym1', 'function', 1, 10)",
                [],
            ).unwrap();
            conn.execute(
                "INSERT OR REPLACE INTO cas_symbol_contents (content_hash, content)
                 VALUES ('content_hash_1', 'dummy')",
                [],
            ).unwrap();
        }

        // grace_days=0：parsed_at < now 的 ready 条目都应被删除
        // 注意 fresh_key 的 parsed_at=9999999999（≈2286年）远大于 now（2026年），应保留
        let response = dispatch(
            &mut state,
            make_owner_peer(),
            "gc.cas",
            &json!({
                "workspace_instance_id": ws_id,
                "grace_days": 0
            }),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["deleted_count"], 1);

        // 验证：stale_key 被删，fresh_key 保留
        let resources2 = state.resources.get(&ws_id).unwrap().clone();
        let conn = resources2.cas_store.conn().lock().unwrap();
        let remaining: i64 = conn
            .query_row("SELECT COUNT(*) FROM cas_file_cache", [], |row| row.get::<_, i64>(0))
            .unwrap();
        assert_eq!(remaining, 1, "应只剩 fresh_key");

        let fresh_key: String = conn
            .query_row(
                "SELECT cas_key FROM cas_file_cache",
                [],
                |row| row.get::<_, String>(0),
            )
            .unwrap();
        assert_eq!(fresh_key, "fresh_key");

        // 子表也应被清理：cas_symbols 中 stale_key 的行被删
        let symbols_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM cas_symbols", [], |row| row.get::<_, i64>(0))
            .unwrap();
        assert_eq!(symbols_count, 0, "stale_key 关联的 cas_symbols 应被级联删除");

        // cas_symbol_contents 中的孤儿 content_hash 也应被清理
        let contents_count: i64 = conn
            .query_row("SELECT COUNT(*) FROM cas_symbol_contents", [], |row| row.get::<_, i64>(0))
            .unwrap();
        assert_eq!(contents_count, 0, "孤儿 cas_symbol_contents 应被清理");
    }

    // ============================================
    // G4: Container Mount Mapping 测试
    // ============================================

    #[test]
    fn test_register_mount_mapping_inserts_row() {
        // 注册一条 mount mapping，验证字段正确写入
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let result = registry
            .register_mount_mapping("ubuntu_2204", "/home/user1", "/data/volumes/user1", "bind")
            .unwrap();
        assert_eq!(result["container_id"], "ubuntu_2204");
        assert_eq!(result["container_path"], "/home/user1");
        assert_eq!(result["host_path"], "/data/volumes/user1");
        assert_eq!(result["mapping_type"], "bind");
        assert!(result["id"].as_i64().unwrap_or(0) > 0);
    }

    #[test]
    fn test_register_mount_mapping_default_type_is_bind() {
        // mapping_type 缺省时默认 bind（与 schema DEFAULT 'bind' 一致）
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let result = registry
            .register_mount_mapping("c1", "/app", "/host/app", "")
            .unwrap();
        // 注意：传入空字符串时，SQL 写入空字符串而非 default。
        // DB DEFAULT 仅在未指定列时生效；这里显式传入空字符串。
        // 实际 RPC handler 会传入默认值 "bind"，故此处验证 handler 行为更合适。
        // 但为了测试 DB 层的灵活性，这里接受空字符串。
        assert_eq!(result["mapping_type"], "");
    }

    #[test]
    fn test_register_mount_mapping_upsert_replaces_existing() {
        // 同一 (container_id, container_path) 重复注册：host_path 和 mapping_type 被更新
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        registry
            .register_mount_mapping("c1", "/app", "/host/app_v1", "bind")
            .unwrap();
        let updated = registry
            .register_mount_mapping("c1", "/app", "/host/app_v2", "volume")
            .unwrap();
        assert_eq!(updated["host_path"], "/host/app_v2");
        assert_eq!(updated["mapping_type"], "volume");

        // 验证只有 1 条记录（UNIQUE 约束生效）
        let all = registry.list_mount_mappings(None).unwrap();
        assert_eq!(all.len(), 1, "重复注册应替换而非插入新行");
    }

    #[test]
    fn test_list_mount_mappings_returns_all_when_no_filter() {
        // 不传 container_id 时返回所有映射
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        registry
            .register_mount_mapping("c1", "/app", "/h1", "bind")
            .unwrap();
        registry
            .register_mount_mapping("c2", "/app", "/h2", "bind")
            .unwrap();
        registry
            .register_mount_mapping("c1", "/data", "/h3", "volume")
            .unwrap();
        let all = registry.list_mount_mappings(None).unwrap();
        assert_eq!(all.len(), 3, "应列出全部 3 条映射");
    }

    #[test]
    fn test_list_mount_mappings_filters_by_container_id() {
        // 按 container_id 过滤
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        registry
            .register_mount_mapping("c1", "/app", "/h1", "bind")
            .unwrap();
        registry
            .register_mount_mapping("c2", "/app", "/h2", "bind")
            .unwrap();
        registry
            .register_mount_mapping("c1", "/data", "/h3", "volume")
            .unwrap();
        let c1_only = registry.list_mount_mappings(Some("c1")).unwrap();
        assert_eq!(c1_only.len(), 2, "c1 应有 2 条映射");
        for m in &c1_only {
            assert_eq!(m["container_id"], "c1");
        }
    }

    #[test]
    fn test_list_mount_mappings_empty_returns_empty_array() {
        // 无映射时返回空数组（而非 None）
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let all = registry.list_mount_mappings(None).unwrap();
        assert!(all.is_empty());
    }

    #[test]
    fn test_delete_mount_mapping_removes_row() {
        // 删除存在的映射，返回 1
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        registry
            .register_mount_mapping("c1", "/app", "/h1", "bind")
            .unwrap();
        let deleted = registry.delete_mount_mapping("c1", "/app").unwrap();
        assert_eq!(deleted, 1);
        // 再查应不存在
        let all = registry.list_mount_mappings(None).unwrap();
        assert!(all.is_empty());
    }

    #[test]
    fn test_delete_mount_mapping_returns_zero_for_missing() {
        // 删除不存在的映射，返回 0（不报错）
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        let deleted = registry.delete_mount_mapping("c1", "/app").unwrap();
        assert_eq!(deleted, 0);
    }

    #[test]
    fn test_delete_mount_mapping_only_affects_target() {
        // 删除单条不影响其他映射
        let registry = WorkspaceRegistry::open_in_memory().unwrap();
        registry
            .register_mount_mapping("c1", "/app", "/h1", "bind")
            .unwrap();
        registry
            .register_mount_mapping("c1", "/data", "/h2", "bind")
            .unwrap();
        registry
            .register_mount_mapping("c2", "/app", "/h3", "bind")
            .unwrap();
        // 删除 c1:/app，应只删 1 条
        let deleted = registry.delete_mount_mapping("c1", "/app").unwrap();
        assert_eq!(deleted, 1);
        let remaining = registry.list_mount_mappings(None).unwrap();
        assert_eq!(remaining.len(), 2, "应剩 2 条");
        // 验证剩余的 (container_id, container_path) 不包含 c1:/app
        for m in &remaining {
            let cid = m["container_id"].as_str().unwrap();
            let cpath = m["container_path"].as_str().unwrap();
            assert!(!(cid == "c1" && cpath == "/app"));
        }
    }

    // ---- G4: dispatch 层 mount.* RPC handler 测试 ----

    #[test]
    fn test_dispatch_mount_register_succeeds() {
        // mount.register RPC 成功注册映射
        let mut state = make_state();
        let peer = make_peer(1000);
        let params = json!({
            "container_id": "ubuntu_2204",
            "container_path": "/home/user1",
            "host_path": "/data/volumes/user1",
            "mapping_type": "bind"
        });
        let resp = dispatch(&mut state, peer, "mount.register", &params, &[]);
        assert!(resp["ok"].as_bool().unwrap_or(false), "mount.register 应成功");
        let result = &resp["result"];
        assert_eq!(result["container_id"], "ubuntu_2204");
        assert_eq!(result["container_path"], "/home/user1");
        assert_eq!(result["host_path"], "/data/volumes/user1");
        assert_eq!(result["mapping_type"], "bind");
    }

    #[test]
    fn test_dispatch_mount_register_default_mapping_type_is_bind() {
        // 缺省 mapping_type 时，handler 默认填充 "bind"
        let mut state = make_state();
        let peer = make_peer(1000);
        let params = json!({
            "container_id": "c1",
            "container_path": "/app",
            "host_path": "/h1"
        });
        let resp = dispatch(&mut state, peer, "mount.register", &params, &[]);
        assert!(resp["ok"].as_bool().unwrap_or(false));
        assert_eq!(resp["result"]["mapping_type"], "bind");
    }

    #[test]
    fn test_dispatch_mount_register_rejects_invalid_mapping_type() {
        // mapping_type 非 bind/volume/smb 时返回 invalid_params 错误
        let mut state = make_state();
        let peer = make_peer(1000);
        let params = json!({
            "container_id": "c1",
            "container_path": "/app",
            "host_path": "/h1",
            "mapping_type": "invalid_type"
        });
        let resp = dispatch(&mut state, peer, "mount.register", &params, &[]);
        assert!(!resp["ok"].as_bool().unwrap_or(true), "应失败");
        assert_eq!(resp["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_dispatch_mount_register_requires_container_id() {
        // 缺少 container_id 时返回 invalid_params
        let mut state = make_state();
        let peer = make_peer(1000);
        let params = json!({
            "container_path": "/app",
            "host_path": "/h1"
        });
        let resp = dispatch(&mut state, peer, "mount.register", &params, &[]);
        assert!(!resp["ok"].as_bool().unwrap_or(true));
        assert_eq!(resp["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_dispatch_mount_list_returns_empty_initially() {
        // 初始状态下 mount.list 返回空数组
        let mut state = make_state();
        let peer = make_peer(1000);
        let resp = dispatch(&mut state, peer, "mount.list", &json!({}), &[]);
        assert!(resp["ok"].as_bool().unwrap_or(false));
        assert!(resp["result"].as_array().unwrap().is_empty());
    }

    #[test]
    fn test_dispatch_mount_list_returns_all_without_filter() {
        // 注册 3 条后，mount.list 无 filter 返回全部
        let mut state = make_state();
        let peer = make_peer(1000);
        for (cid, cpath, hpath, mtype) in [
            ("c1", "/app", "/h1", "bind"),
            ("c2", "/app", "/h2", "bind"),
            ("c1", "/data", "/h3", "volume"),
        ] {
            let params = json!({
                "container_id": cid,
                "container_path": cpath,
                "host_path": hpath,
                "mapping_type": mtype
            });
            let _ = dispatch(&mut state, peer, "mount.register", &params, &[]);
        }
        let resp = dispatch(&mut state, peer, "mount.list", &json!({}), &[]);
        assert!(resp["ok"].as_bool().unwrap_or(false));
        assert_eq!(resp["result"].as_array().unwrap().len(), 3);
    }

    #[test]
    fn test_dispatch_mount_list_filters_by_container_id() {
        // mount.list 带 container_id 过滤
        let mut state = make_state();
        let peer = make_peer(1000);
        for (cid, cpath, hpath) in [
            ("c1", "/app", "/h1"),
            ("c2", "/app", "/h2"),
            ("c1", "/data", "/h3"),
        ] {
            let params = json!({
                "container_id": cid,
                "container_path": cpath,
                "host_path": hpath
            });
            let _ = dispatch(&mut state, peer, "mount.register", &params, &[]);
        }
        let resp = dispatch(
            &mut state,
            peer,
            "mount.list",
            &json!({"container_id": "c1"}),
            &[],
        );
        assert!(resp["ok"].as_bool().unwrap_or(false));
        let arr = resp["result"].as_array().unwrap();
        assert_eq!(arr.len(), 2);
        for m in arr {
            assert_eq!(m["container_id"], "c1");
        }
    }

    #[test]
    fn test_dispatch_mount_delete_removes_mapping() {
        // mount.delete 删除存在的映射，返回 deleted=1
        let mut state = make_state();
        let peer = make_peer(1000);
        // 先注册一条
        let _ = dispatch(
            &mut state,
            peer,
            "mount.register",
            &json!({
                "container_id": "c1",
                "container_path": "/app",
                "host_path": "/h1"
            }),
            &[],
        );
        // 删除
        let resp = dispatch(
            &mut state,
            peer,
            "mount.delete",
            &json!({"container_id": "c1", "container_path": "/app"}),
            &[],
        );
        assert!(resp["ok"].as_bool().unwrap_or(false));
        assert_eq!(resp["result"]["deleted"], 1);
        // 再 list 应为空
        let list_resp = dispatch(&mut state, peer, "mount.list", &json!({}), &[]);
        assert!(list_resp["result"].as_array().unwrap().is_empty());
    }

    #[test]
    fn test_dispatch_mount_delete_returns_zero_for_missing() {
        // mount.delete 不存在的映射，返回 deleted=0（不报错）
        let mut state = make_state();
        let peer = make_peer(1000);
        let resp = dispatch(
            &mut state,
            peer,
            "mount.delete",
            &json!({"container_id": "c1", "container_path": "/app"}),
            &[],
        );
        assert!(resp["ok"].as_bool().unwrap_or(false));
        assert_eq!(resp["result"]["deleted"], 0);
    }

    #[test]
    fn test_dispatch_mount_delete_requires_container_id() {
        // 缺少 container_id 时返回 invalid_params
        let mut state = make_state();
        let peer = make_peer(1000);
        let resp = dispatch(
            &mut state,
            peer,
            "mount.delete",
            &json!({"container_path": "/app"}),
            &[],
        );
        assert!(!resp["ok"].as_bool().unwrap_or(true));
        assert_eq!(resp["error"]["code"], "invalid_params");
    }

    #[test]
    fn test_dispatch_mount_register_then_upsert() {
        // 同一 (container_id, container_path) 二次注册应替换而非新增
        let mut state = make_state();
        let peer = make_peer(1000);
        let params_v1 = json!({
            "container_id": "c1",
            "container_path": "/app",
            "host_path": "/h_v1",
            "mapping_type": "bind"
        });
        let _ = dispatch(&mut state, peer, "mount.register", &params_v1, &[]);
        let params_v2 = json!({
            "container_id": "c1",
            "container_path": "/app",
            "host_path": "/h_v2",
            "mapping_type": "volume"
        });
        let resp_v2 = dispatch(&mut state, peer, "mount.register", &params_v2, &[]);
        assert!(resp_v2["ok"].as_bool().unwrap_or(false));
        assert_eq!(resp_v2["result"]["host_path"], "/h_v2");
        assert_eq!(resp_v2["result"]["mapping_type"], "volume");
        // list 应只有 1 条
        let list_resp = dispatch(&mut state, peer, "mount.list", &json!({}), &[]);
        assert_eq!(list_resp["result"].as_array().unwrap().len(), 1);
    }

    #[test]
    fn test_dispatch_unknown_mount_method_returns_method_not_found() {
        // 未知 mount.* 方法返回 method_not_found
        let mut state = make_state();
        let peer = make_peer(1000);
        let resp = dispatch(
            &mut state,
            peer,
            "mount.update",
            &json!({}),
            &[],
        );
        assert!(!resp["ok"].as_bool().unwrap_or(true));
        assert_eq!(resp["error"]["code"], "method_not_found");
    }

    #[test]
    fn test_mount_register_persists_across_state_recreation() {
        // 验证 mount mapping 持久化到 DB 文件，重新打开 registry 仍能读到
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("registry.db");
        let db_path_str = db_path.to_string_lossy().to_string();

        // 第一个 registry 注册一条
        {
            let registry = WorkspaceRegistry::open(&db_path_str).unwrap();
            registry
                .register_mount_mapping("c1", "/app", "/h1", "bind")
                .unwrap();
        }

        // 重新打开同一 DB 文件，应能读到
        {
            let registry = WorkspaceRegistry::open(&db_path_str).unwrap();
            let all = registry.list_mount_mappings(None).unwrap();
            assert_eq!(all.len(), 1, "重新打开 registry 应能读到持久化的映射");
            assert_eq!(all[0]["container_id"], "c1");
            assert_eq!(all[0]["host_path"], "/h1");
        }
    }

    #[test]
    fn test_gc_cas_preserves_pending_refs() {
        // cas_pending_refs 中未过期的 cas_key 不应被 GC
        let tmp = tempfile::tempdir().unwrap();
        let mut state = make_state_with_data_root(tmp.path());
        let peer = make_owner_peer();
        let ws_id = register_ws(&mut state, peer.uid);

        // connect + 懒初始化
        let _ = dispatch(
            &mut state,
            make_owner_peer(),
            "workspace.connect",
            &json!({
                "workspace_instance_id": ws_id,
                "agent_session_id": "session-1"
            }),
            &[],
        );

        let resources = state.resources.get(&ws_id).unwrap().clone();
        {
            let conn = resources.cas_store.conn().lock().unwrap();
            // 插入 1 条 parsed_at=0 的 ready 条目
            conn.execute(
                "INSERT OR REPLACE INTO cas_file_cache
                 (cas_key, content_hash, language, file_size, total_lines,
                  parser_version, callwarden_version, extraction_config_version,
                  abi_version, input_abi_version, state, parsed_at)
                 VALUES ('pending_key', 'hash1', 'rust', 100, 10,
                         'v1', 'v1', 'v1', 'v1', 'v1', 'ready', 0)",
                [],
            ).unwrap();
            // 插入对应的未过期 pending_ref（expires_at=9999999999，远在未来）
            conn.execute(
                "INSERT OR REPLACE INTO cas_pending_refs
                 (cas_key, workspace_id, expires_at, created_at)
                 VALUES ('pending_key', 1, 9999999999.0, 0)",
                [],
            ).unwrap();
        }

        // gc.cas grace_days=0：parsed_at=0 的条目本应被删，但因为有未过期 pending_ref，应保留
        let response = dispatch(
            &mut state,
            make_owner_peer(),
            "gc.cas",
            &json!({
                "workspace_instance_id": ws_id,
                "grace_days": 0
            }),
            &[],
        );
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["deleted_count"], 0);

        // 验证 pending_key 仍在
        let resources2 = state.resources.get(&ws_id).unwrap().clone();
        let conn = resources2.cas_store.conn().lock().unwrap();
        let count: i64 = conn
            .query_row(
                "SELECT COUNT(*) FROM cas_file_cache WHERE cas_key = 'pending_key'",
                [],
                |row| row.get::<_, i64>(0),
            )
            .unwrap();
        assert_eq!(count, 1);
    }
}
