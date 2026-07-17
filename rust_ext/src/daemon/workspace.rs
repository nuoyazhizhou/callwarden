//! Workspace registry —— workspace 注册、查询、状态管理 + UID ACL。
//!
//! 对应 Python：
//! - `db/db_daemon.py`（WORKSPACE_REGISTRY_DDL + register/list/get_status/update_status）
//! - `server/daemon_server.py:_owned_workspace` / `_validate_owned_path` / dispatch 的
//!   `workspace.register` / `workspace.list` / `workspace.status` 分支
//!
//! 跨平台：Windows 上 `_validate_owned_path` 跳过 owner_uid ACL 检查（开发测试用），
//! Unix 上做完整 ACL 校验（参考 daemon_server.py L227-242）。
//! `workspace.connect` / `workspace.file.refresh` / `workspace.recover` 依赖 CAS /
//! Replicator，留给 R5/R6 实现，本模块覆盖 `workspace.register` / `workspace.list`
//! / `workspace.status` / `workspace.recover`（标记 unimplemented）。

use std::path::Path;
use std::sync::Mutex;
use std::time::{SystemTime, UNIX_EPOCH};

use rusqlite::{params, Connection, Row};
use serde_json::{Map, Value};
use sha2::{Digest, Sha256};

use super::dispatch::{
    DaemonRpcError, DaemonState, DaemonStateExt, PeerCredential,
    get_str_param_or, require_str_param,
};

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
        })
    }

    /// 内存数据库（测试用）
    pub fn open_in_memory() -> Result<Self, rusqlite::Error> {
        let conn = Connection::open_in_memory()?;
        Self::init_conn(&conn)?;
        Ok(Self {
            conn: Mutex::new(conn),
        })
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

/// 组合 DaemonState + WorkspaceRegistry 的 daemon state 实现。
///
/// R4 阶段覆盖：
/// - `workspace.register`：注册 workspace（含路径校验）
/// - `workspace.list`：列出当前 UID 拥有的 workspace
/// - `workspace.status`：查询指定 workspace 状态（含 ACL）
/// - `workspace.recover`：R5/R6 才能实现，返回 method_not_found
///
/// 未覆盖（保留 trait 默认 method_not_found）：
/// - `workspace.connect`：依赖 R5 daemon_handle_connect
/// - `workspace.file.refresh`：依赖 R5 CAS 发布流程
/// - `snapshot.*` / `query.*` / `gc.*` / `backup` / `restore`：依赖 R6
pub struct WorkspaceDaemonState {
    pub base: DaemonState,
    pub registry: WorkspaceRegistry,
}

impl WorkspaceDaemonState {
    pub fn new(registry: WorkspaceRegistry) -> Self {
        Self {
            base: DaemonState::default(),
            registry,
        }
    }
}

impl DaemonStateExt for WorkspaceDaemonState {
    fn daemon_state(&self) -> &DaemonState {
        &self.base
    }

    fn handle_health(&mut self, _peer: PeerCredential) -> Result<Value, DaemonRpcError> {
        let state = self.daemon_state();
        let uptime = state.start_time.elapsed().as_secs();
        let workspace_count = self
            .registry
            .count_workspaces()
            .map_err(|e| DaemonRpcError::internal_error(format!("count_workspaces: {}", e)))?;
        let mut m = Map::new();
        m.insert("status".to_string(), Value::String("ok".to_string()));
        m.insert("pid".to_string(), Value::Number(state.pid.into()));
        m.insert(
            "uptime_seconds".to_string(),
            Value::Number(uptime.into()),
        );
        m.insert(
            "schema_version".to_string(),
            Value::Number(state.schema_version.into()),
        );
        m.insert(
            "workspace_count".to_string(),
            Value::Number(workspace_count.into()),
        );
        Ok(Value::Object(m))
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

    fn handle_workspace_recover(
        &mut self,
        _peer: PeerCredential,
        _params: &Value,
    ) -> Result<Value, DaemonRpcError> {
        // R5/R6 实现：依赖 StagingLog + Replicator
        Err(DaemonRpcError::method_not_found("workspace.recover"))
    }
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::dispatch::dispatch;
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
        let peer = make_peer(1000);
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
        assert_eq!(response["result"]["owner_uid"], 1000);
        assert_eq!(response["result"]["status"], "active");
        // host_real_root 应该被 canonicalize 处理
        assert!(response["result"]["host_real_root"].as_str().unwrap().len() > 0);
    }

    #[test]
    fn test_dispatch_workspace_status_returns_workspace_for_owner() {
        let mut state = make_state();
        let peer = make_peer(1000);

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
        let peer_owner = make_peer(1000);
        let peer_other = make_peer(2000);

        // owner=1000 注册
        let tmp = tempfile::tempdir().unwrap();
        let dir_path = tmp.path().to_str().unwrap();
        let reg_params = json!({"client_view_root": dir_path});
        let reg_response =
            dispatch(&mut state, peer_owner, "workspace.register", &reg_params, &[]);
        let instance_id = reg_response["result"]["workspace_instance_id"]
            .as_str()
            .unwrap()
            .to_string();

        // peer_uid=2000 查询 owner=1000 的 workspace
        let status_params = json!({"workspace_instance_id": instance_id});
        let response = dispatch(&mut state, peer_other, "workspace.status", &status_params, &[]);
        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "workspace_forbidden");
    }

    #[test]
    fn test_dispatch_health_returns_workspace_count() {
        let mut state = make_state();
        let peer = make_peer(1000);

        // 注册 2 个 workspace
        let tmp1 = tempfile::tempdir().unwrap();
        let tmp2 = tempfile::tempdir().unwrap();
        dispatch(
            &mut state,
            peer,
            "workspace.register",
            &json!({"client_view_root": tmp1.path().to_str().unwrap()}),
            &[],
        );
        dispatch(
            &mut state,
            peer,
            "workspace.register",
            &json!({"client_view_root": tmp2.path().to_str().unwrap()}),
            &[],
        );

        // health 应该返回 workspace_count=2
        let response = dispatch(&mut state, peer, "health", &json!({}), &[]);
        assert_eq!(response["ok"], true);
        assert_eq!(response["result"]["workspace_count"], 2);
    }

    #[test]
    fn test_dispatch_workspace_recover_returns_method_not_found() {
        let mut state = make_state();
        let peer = make_peer(1000);
        let params = json!({"workspace_instance_id": "xyz"});
        let response = dispatch(&mut state, peer, "workspace.recover", &params, &[]);

        assert_eq!(response["ok"], false);
        assert_eq!(response["error"]["code"], "method_not_found");
    }

    // ---- 跨用户隔离测试 ----

    #[test]
    fn test_workspace_list_isolates_by_uid() {
        let mut state = make_state();

        let tmp1 = tempfile::tempdir().unwrap();
        let tmp2 = tempfile::tempdir().unwrap();
        let tmp3 = tempfile::tempdir().unwrap();

        // user 1000 注册 2 个
        dispatch(
            &mut state,
            make_peer(1000),
            "workspace.register",
            &json!({"client_view_root": tmp1.path().to_str().unwrap()}),
            &[],
        );
        dispatch(
            &mut state,
            make_peer(1000),
            "workspace.register",
            &json!({"client_view_root": tmp2.path().to_str().unwrap()}),
            &[],
        );
        // user 2000 注册 1 个
        dispatch(
            &mut state,
            make_peer(2000),
            "workspace.register",
            &json!({"client_view_root": tmp3.path().to_str().unwrap()}),
            &[],
        );

        // user 1000 只看到自己的 2 个
        let r1 = dispatch(&mut state, make_peer(1000), "workspace.list", &json!({}), &[]);
        assert_eq!(r1["result"].as_array().unwrap().len(), 2);

        // user 2000 只看到自己的 1 个
        let r2 = dispatch(&mut state, make_peer(2000), "workspace.list", &json!({}), &[]);
        assert_eq!(r2["result"].as_array().unwrap().len(), 1);
    }
}
