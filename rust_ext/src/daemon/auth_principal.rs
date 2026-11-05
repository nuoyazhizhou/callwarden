//! principal 身份层与授权（ADR-002 §5 / §15.3）。
//!
//! 单轨 mTLS 下**唯一授权主语**是客户端证书 SAN URI 中的 `principal_id`；
//! `peer_uid` / SID / CN 一律不参与授权判定（仅诊断/审计）。
//!
//! 两个台账来源（并集，任一命中即生效）：
//!
//! 1. **签发台账（磁盘）**：`<pki>/identities/<principal_id>/meta.json`（daemon 签发时
//!    写入，`kind` 区分 `service`/`agent`）、`grants.json`（workspace 白名单）、
//!    `revoked`（吊销标记）。
//! 2. **权威 DB**（`CW_DAEMON_TASK_DB` 或 `~/.callwarden/callwarden.db`）：
//!    `pki_principals` / `pki_issued_certs` / `pki_revocations` /
//!    `workspace_principal_grants`（`db/db_base.py:ensure_pki_schema`）。
//!
//! 吊销热更新：DB 侧按 `PRAGMA data_version` 变化重载（≤1s 一次），磁盘侧每次新连接
//! 直接读取 `identities/<id>/` 最新内容。旧连接由 `MAX_CONNECTION_AGE`（300s）兜底
//! 断开——即“吊销后 ≤max_conn_age 内旧连接失效”。
//!
//! 全部失败路径 fail-closed：未知 principal → 拒绝；无 grant 的 workspace → 拒绝；
//! 白名单溢出 → 拒绝（绝不静默放宽）。

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, RwLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use rusqlite::Connection;
use serde_json::Value;

use crate::daemon::peercred::{FixedStr, PrincipalIdentity};

/// 连接最大存活时间（ADR-002 `max_conn_age` 默认值）。
///
/// 旧连接在吊销后最迟在本时限内被强制断开；新连接立即按最新吊销集判定。
pub const MAX_CONNECTION_AGE: Duration = Duration::from_secs(300);

/// DB 吊销集/授权的重载粒度（避免每连接都查一次 `data_version`）。
const DB_RECHECK_INTERVAL: Duration = Duration::from_secs(1);

/// `allowed_workspace_ids` 的固定容量（与 `PrincipalIdentity` 字段一致）。
const MAX_WORKSPACE_SCOPE_BYTES: usize = 512;

/// 授权失败错误码（ADR-002 §15.3）。
pub const E_PRINCIPAL_UNKNOWN: &str = "E_PRINCIPAL_UNKNOWN";
pub const E_PRINCIPAL_REVOKED: &str = "E_PRINCIPAL_REVOKED";
pub const E_PRINCIPAL_WORKSPACE_FORBIDDEN: &str = "E_PRINCIPAL_WORKSPACE_FORBIDDEN";
pub const E_MTLS_CLIENT_CERT_REJECTED: &str = "E_MTLS_CLIENT_CERT_REJECTED";

/// principal 授权失败原因。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum PrincipalError {
    /// 证书无 SAN URI（或 URI 前缀非法）：连接不得建立身份。
    SanMissing,
    /// 证书由本 CA 签发，但 principal 不在任何台账中。
    UnknownPrincipal,
    /// principal 或证书 serial 已吊销。
    Revoked,
    /// principal 存在，但未获该 workspace 授权。
    WorkspaceForbidden,
}

impl PrincipalError {
    pub fn code(self) -> &'static str {
        match self {
            PrincipalError::SanMissing => E_MTLS_CLIENT_CERT_REJECTED,
            PrincipalError::UnknownPrincipal => E_PRINCIPAL_UNKNOWN,
            PrincipalError::Revoked => E_PRINCIPAL_REVOKED,
            PrincipalError::WorkspaceForbidden => E_PRINCIPAL_WORKSPACE_FORBIDDEN,
        }
    }

    pub fn message(self) -> &'static str {
        match self {
            PrincipalError::SanMissing => "client certificate has no principal SAN URI",
            PrincipalError::UnknownPrincipal => "principal is not registered with this daemon",
            PrincipalError::Revoked => "principal or certificate serial is revoked",
            PrincipalError::WorkspaceForbidden => "principal is not granted access to this workspace",
        }
    }
}

/// DB 侧的只读投影（一次 `data_version` 变更对应一次重载）。
#[derive(Debug, Clone, Default)]
struct DbSnapshot {
    /// principal_id → kind（仅 `status='active'`）。
    principals: BTreeMap<String, String>,
    /// 已吊销的 principal_id。
    revoked_principals: BTreeSet<String>,
    /// 已吊销的证书 serial（大写 hex，与 `pki_api::cert_serial_hex` 一致）。
    revoked_serials: BTreeSet<String>,
    /// principal_id → 已授权的 workspace_id 集合。
    grants: BTreeMap<String, BTreeSet<String>>,
}

/// 磁盘签发台账（每次连接实时读取，天然热更新）。
#[derive(Debug, Clone, Default)]
struct FileRecord {
    kind: Option<String>,
    revoked: bool,
    workspaces: BTreeSet<String>,
}

struct RefreshState {
    conn: Option<Connection>,
    data_version: Option<i64>,
    last_check: Option<Instant>,
    last_error: Option<String>,
}

/// principal 授权注册表（daemon 进程内单例）。
pub struct PrincipalRegistry {
    pki_root: PathBuf,
    db_path: Option<PathBuf>,
    snapshot: RwLock<Arc<DbSnapshot>>,
    refresh: Mutex<RefreshState>,
}

impl std::fmt::Debug for PrincipalRegistry {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("PrincipalRegistry")
            .field("pki_root", &self.pki_root)
            .field("db_path", &self.db_path)
            .finish()
    }
}

impl PrincipalRegistry {
    pub fn new(pki_root: PathBuf, db_path: Option<PathBuf>) -> Self {
        Self {
            pki_root,
            db_path,
            snapshot: RwLock::new(Arc::new(DbSnapshot::default())),
            refresh: Mutex::new(RefreshState {
                conn: None,
                data_version: None,
                last_check: None,
                last_error: None,
            }),
        }
    }

    /// 从环境解析权威 DB 路径（`CW_DAEMON_TASK_DB` 优先）。
    pub fn from_env(pki_root: PathBuf) -> Self {
        let db_path = std::env::var_os("CW_DAEMON_TASK_DB")
            .map(PathBuf::from)
            .filter(|p| !p.as_os_str().is_empty())
            .unwrap_or_else(crate::daemon::config::default_authority_task_db_path);
        let db_path = if db_path.as_os_str().is_empty() {
            None
        } else {
            Some(db_path)
        };
        Self::new(pki_root, db_path)
    }

    pub fn pki_root(&self) -> &Path {
        &self.pki_root
    }

    pub fn db_path(&self) -> Option<&Path> {
        self.db_path.as_deref()
    }

    /// 若 DB 的 `data_version` 变化（≤`DB_RECHECK_INTERVAL` 粒度的检查）则重载吊销集/授权。
    ///
    /// 重载失败保留上一次成功快照（daemon 不因 DB 短暂忙而拒绝全部流量），
    /// 错误写入 `last_error` 供诊断；磁盘台账不受影响。
    pub fn refresh_if_stale(&self) {
        let mut state = self
            .refresh
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if state
            .last_check
            .map(|t| t.elapsed() < DB_RECHECK_INTERVAL)
            .unwrap_or(false)
        {
            return;
        }
        state.last_check = Some(Instant::now());

        let Some(path) = self.db_path.as_ref() else {
            return;
        };

        if state.conn.is_none() {
            match Connection::open(path) {
                Ok(conn) => {
                    let _ = conn.busy_timeout(Duration::from_secs(2));
                    state.conn = Some(conn);
                }
                Err(e) => {
                    state.last_error = Some(format!("open {}: {}", path.display(), e));
                    return;
                }
            }
        }

        let Some(conn) = state.conn.as_ref() else {
            return;
        };
        let version: i64 = match conn.pragma_query_value(None, "data_version", |row| row.get(0)) {
            Ok(v) => v,
            Err(e) => {
                state.last_error = Some(format!("data_version: {}", e));
                return;
            }
        };
        if state.data_version == Some(version) {
            return;
        }
        let loaded = load_snapshot(conn);
        match loaded {
            Ok(snapshot) => {
                *self.snapshot.write().unwrap_or_else(|e| e.into_inner()) = Arc::new(snapshot);
                state.data_version = Some(version);
                state.last_error = None;
            }
            Err(e) => {
                state.last_error = Some(e);
            }
        }
    }

    /// 强制重载（测试/显式刷新用）。
    pub fn refresh_now(&self) {
        self.refresh
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .last_check = None;
        self.refresh_if_stale();
    }

    fn snapshot(&self) -> Arc<DbSnapshot> {
        Arc::clone(&self.snapshot.read().unwrap_or_else(|e| e.into_inner()))
    }

    /// SAN URI → principal 授权（ADR-002 §5.1/§5.4）。
    ///
    /// 入参都是证书里已经验证过的字段（rustls 已验签），这里只做台账判定。
    pub fn authorize(
        &self,
        principal_id: &str,
        cert_serial: &str,
        cert_sha256: &str,
    ) -> Result<PrincipalIdentity, PrincipalError> {
        if principal_id.is_empty() {
            return Err(PrincipalError::SanMissing);
        }
        self.refresh_if_stale();
        let db = self.snapshot();
        let file = self.read_file_record(principal_id);

        let kind = file
            .kind
            .clone()
            .or_else(|| db.principals.get(principal_id).cloned());
        let Some(kind) = kind else {
            return Err(PrincipalError::UnknownPrincipal);
        };

        // 吊销集（磁盘标记 ∪ DB principal 状态 ∪ DB 证书 serial）
        let serial_revoked = !cert_serial.is_empty() && db.revoked_serials.contains(cert_serial);
        if file.revoked || serial_revoked || db.revoked_principals.contains(principal_id) {
            return Err(PrincipalError::Revoked);
        }

        // workspace 白名单 = 磁盘 grants ∪ DB grants（service 不看白名单）
        let mut workspaces: BTreeSet<String> = file.workspaces.clone();
        if let Some(g) = db.grants.get(principal_id) {
            workspaces.extend(g.iter().cloned());
        }
        let joined = workspaces.iter().cloned().collect::<Vec<_>>().join(",");
        let scope = FixedStr::<MAX_WORKSPACE_SCOPE_BYTES>::from_str(&joined);
        let scopes_overflow = scope.is_none();

        Ok(PrincipalIdentity {
            principal_id: FixedStr::<48>::from_str(principal_id).unwrap_or_default(),
            cert_serial: FixedStr::<64>::from_str(cert_serial).unwrap_or_default(),
            cert_sha256: FixedStr::<64>::from_str(cert_sha256).unwrap_or_default(),
            kind: FixedStr::<16>::from_str(&kind).unwrap_or_default(),
            allowed_workspace_ids: scope.unwrap_or_default(),
            scopes_overflow,
        })
    }

    /// 读取 `<pki>/identities/<id>/`：`meta.json`（kind/revoked）+ `grants.json` + `revoked` 标记。
    fn read_file_record(&self, principal_id: &str) -> FileRecord {
        // principal_id 必须是安全路径片段（不含分隔符/上跳），否则按“未知”处理。
        if principal_id.contains('/') || principal_id.contains('\\') || principal_id.contains("..") {
            return FileRecord::default();
        }
        let dir = self.pki_root.join("identities").join(principal_id);
        let mut record = FileRecord::default();
        if !dir.is_dir() {
            return record;
        }
        if dir.join("revoked").is_file() || dir.join("revoked.json").is_file() {
            record.revoked = true;
        }
        if let Ok(raw) = std::fs::read_to_string(dir.join("meta.json")) {
            if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw) {
                if let Some(Value::Bool(true)) = map.get("revoked") {
                    record.revoked = true;
                }
                if let Some(Value::String(kind)) = map.get("kind") {
                    record.kind = Some(kind.clone());
                }
                if let Some(Value::Array(ids)) = map.get("workspace_ids") {
                    record.workspaces.extend(
                        ids.iter()
                            .filter_map(|v| v.as_str())
                            .map(|s| s.to_string()),
                    );
                }
            }
        }
        if let Ok(raw) = std::fs::read_to_string(dir.join("grants.json")) {
            if let Ok(Value::Object(map)) = serde_json::from_str::<Value>(&raw) {
                if let Some(Value::Array(ids)) = map.get("workspace_ids") {
                    record.workspaces.extend(
                        ids.iter()
                            .filter_map(|v| v.as_str())
                            .map(|s| s.to_string()),
                    );
                }
            }
        }
        record
    }

    /// 登记本次签发的叶证书（enroll 成功后调用）：磁盘 grants + DB 台账（best-effort）。
    ///
    /// 磁盘 `grants.json` 是本机权威（不依赖 DB 迁移）；DB 台账供审计与跨组件查询。
    pub fn record_issued_certificate(
        &self,
        principal_id: &str,
        cert_serial: &str,
        cert_sha256: &str,
        workspace_id: &str,
    ) -> Result<(), String> {
        // 1) 磁盘授权（必须成功：否则新 principal 无法通过授权）。
        if principal_id.contains('/') || principal_id.contains('\\') || principal_id.contains("..") {
            return Err(format!("非法 principal_id: {}", principal_id));
        }
        let dir = self.pki_root.join("identities").join(principal_id);
        std::fs::create_dir_all(&dir).map_err(|e| format!("mkdir {}: {}", dir.display(), e))?;
        let grants_path = dir.join("grants.json");
        let mut workspaces: BTreeSet<String> = std::fs::read_to_string(&grants_path)
            .ok()
            .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
            .and_then(|v| match v.get("workspace_ids") {
                Some(Value::Array(ids)) => Some(
                    ids.iter()
                        .filter_map(|x| x.as_str())
                        .map(|s| s.to_string())
                        .collect::<BTreeSet<_>>(),
                ),
                _ => None,
            })
            .unwrap_or_default();
        if !workspace_id.is_empty() {
            workspaces.insert(workspace_id.to_string());
        }
        let payload = serde_json::json!({
            "principal_id": principal_id,
            "cert_serial": cert_serial,
            "cert_sha256": cert_sha256,
            "workspace_ids": workspaces.iter().cloned().collect::<Vec<_>>(),
        });
        let body = serde_json::to_string_pretty(&payload).map_err(|e| e.to_string())?;
        let tmp = dir.join("grants.json.tmp");
        std::fs::write(&tmp, body).map_err(|e| format!("write {}: {}", tmp.display(), e))?;
        std::fs::rename(&tmp, &grants_path)
            .map_err(|e| format!("rename {}: {}", grants_path.display(), e))?;

        // 2) DB 台账（best-effort：DB 缺失/未迁移不阻断签发）。
        if let Some(db_path) = self.db_path.as_ref() {
            let _ = record_db_ledger(db_path, principal_id, cert_serial, cert_sha256, workspace_id);
            // 让下一个连接立刻看到新 principal（data_version 已变化）。
            self.refresh
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .last_check = None;
        }
        Ok(())
    }
}

/// `pki_revocations` / `pki_principals` / `pki_issued_certs` / `workspace_principal_grants` 读投影。
fn load_snapshot(conn: &Connection) -> Result<DbSnapshot, String> {
    let mut snap = DbSnapshot::default();

    if let Ok(mut stmt) = conn.prepare("SELECT principal_id, status, kind FROM pki_principals") {
        if let Ok(rows) = stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, Option<String>>(2)?,
            ))
        }) {
            for (id, status, kind) in rows.flatten() {
                if status.eq_ignore_ascii_case("active") {
                    snap.principals
                        .insert(id, kind.unwrap_or_else(|| "agent".to_string()));
                } else {
                    snap.revoked_principals.insert(id);
                }
            }
        }
    }

    if let Ok(mut stmt) = conn.prepare("SELECT cert_serial FROM pki_revocations") {
        if let Ok(rows) = stmt.query_map([], |row| row.get::<_, String>(0)) {
            snap.revoked_serials.extend(rows.flatten());
        }
    }

    if let Ok(mut stmt) =
        conn.prepare("SELECT cert_serial, status FROM pki_issued_certs WHERE status <> 'active'")
    {
        if let Ok(rows) = stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        }) {
            for (serial, status) in rows.flatten() {
                if status.eq_ignore_ascii_case("revoked") {
                    snap.revoked_serials.insert(serial);
                }
            }
        }
    }

    // `revoked_at` 是 `REAL NOT NULL DEFAULT 0`（0 = 未吊销）。
    if let Ok(mut stmt) = conn.prepare(
        "SELECT principal_id, workspace_id FROM workspace_principal_grants \
         WHERE COALESCE(revoked_at, 0) = 0",
    ) {
        if let Ok(rows) = stmt.query_map([], |row| {
            Ok((row.get::<_, String>(0)?, row.get::<_, String>(1)?))
        }) {
            for (principal_id, workspace_id) in rows.flatten() {
                snap.grants
                    .entry(principal_id)
                    .or_default()
                    .insert(workspace_id);
            }
        }
    }

    Ok(snap)
}

/// 写入 DB 台账（签发登记）。失败只返回错误，不改变磁盘权威状态。
fn record_db_ledger(
    db_path: &Path,
    principal_id: &str,
    cert_serial: &str,
    cert_sha256: &str,
    workspace_id: &str,
) -> Result<(), String> {
    let conn = Connection::open(db_path).map_err(|e| format!("open: {}", e))?;
    let _ = conn.busy_timeout(Duration::from_secs(2));
    // 隔离 daemon / 新机器上 Python DB 层可能从未开过库：`cw pki` 只补了 3 张表，
    // 这里幂等补齐签发登记所需表（列定义与 `db/db_base.py::PKI_SCHEMA_STATEMENTS`
    // 保持一致），否则下文 INSERT 会因缺表失败并静默丢掉台账。
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS pki_principals (\
             principal_id TEXT PRIMARY KEY, kind TEXT NOT NULL, \
             display_name TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'active', \
             created_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL DEFAULT 0, \
             note TEXT NOT NULL DEFAULT '');\
         CREATE TABLE IF NOT EXISTS pki_issued_certs (\
             cert_serial TEXT PRIMARY KEY, principal_id TEXT NOT NULL, \
             fingerprint_sha256 TEXT NOT NULL, issued_at REAL NOT NULL, \
             not_before REAL NOT NULL, not_after REAL NOT NULL, \
             status TEXT NOT NULL DEFAULT 'active', revoked_at REAL NOT NULL DEFAULT 0, \
             issued_by TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'enroll', \
             subject_san TEXT NOT NULL DEFAULT '');\
         CREATE TABLE IF NOT EXISTS workspace_principal_grants (\
             workspace_id INTEGER NOT NULL, principal_id TEXT NOT NULL, \
             role TEXT NOT NULL, granted_at REAL NOT NULL, \
             granted_by TEXT NOT NULL DEFAULT '', revoked_at REAL NOT NULL DEFAULT 0, \
             PRIMARY KEY (workspace_id, principal_id));\
         CREATE TABLE IF NOT EXISTS audit_chain (\
             id INTEGER PRIMARY KEY AUTOINCREMENT, table_name TEXT NOT NULL, \
             record_id TEXT NOT NULL, operation TEXT NOT NULL DEFAULT 'insert', \
             payload_hash TEXT NOT NULL, prev_signature TEXT DEFAULT '', \
             record_signature TEXT NOT NULL, signing_key_id TEXT DEFAULT 'local', \
             signed_at REAL NOT NULL);\
         CREATE INDEX IF NOT EXISTS idx_audit_chain_table_record \
             ON audit_chain(table_name, record_id);",
    )
    .map_err(|e| format!("ensure pki tables: {}", e))?;
    let now = unix_now_f64();
    conn.execute(
        "INSERT OR IGNORE INTO pki_principals \
         (principal_id, kind, display_name, status, created_at, updated_at) \
         VALUES (?1, 'agent', ?1, 'active', ?2, ?2)",
        rusqlite::params![principal_id, now],
    )
    .map_err(|e| format!("pki_principals: {}", e))?;
    conn.execute(
        "INSERT OR REPLACE INTO pki_issued_certs \
         (cert_serial, principal_id, fingerprint_sha256, issued_at, not_before, not_after, status, source) \
         VALUES (?1, ?2, ?3, ?4, ?4, 0, 'active', 'enroll')",
        rusqlite::params![cert_serial, principal_id, cert_sha256, now],
    )
    .map_err(|e| format!("pki_issued_certs: {}", e))?;
    if !workspace_id.is_empty() {
        conn.execute(
            "INSERT OR IGNORE INTO workspace_principal_grants \
             (principal_id, workspace_id, role, granted_at, granted_by) \
             VALUES (?1, ?2, 'agent', ?3, 'daemon')",
            rusqlite::params![principal_id, workspace_id, now],
        )
        .map_err(|e| format!("workspace_principal_grants: {}", e))?;
    }
    // S1-19：签发证书写入 audit_chain（与 Python db_audit_chain 同一哈希链格式）。
    crate::cli::task::sign_audit_chain(
        &conn,
        "pki_issued_certs",
        cert_serial,
        &serde_json::json!({
            "event": "pki_cert_issued",
            "principal_id": principal_id,
            "cert_serial": cert_serial,
            "fingerprint_sha256": cert_sha256,
            "workspace_id": workspace_id,
            "issued_at": now,
            "source": "enroll",
        }),
    )
    .map_err(|e| format!("pki audit_chain: {}", e))?;
    Ok(())
}

fn unix_now_f64() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write_identity(root: &Path, principal_id: &str, meta: &Value) {
        let dir = root.join("identities").join(principal_id);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(
            dir.join("meta.json"),
            serde_json::to_string(meta).unwrap(),
        )
        .unwrap();
    }

    fn pki_root() -> tempfile::TempDir {
        let dir = tempfile::tempdir().unwrap();
        std::fs::create_dir_all(dir.path().join("identities")).unwrap();
        dir
    }

    #[test]
    fn test_unknown_principal_is_rejected() {
        let root = pki_root();
        let registry = PrincipalRegistry::new(root.path().to_path_buf(), None);
        let err = registry
            .authorize("pr_missing", "AA", "deadbeef")
            .unwrap_err();
        assert_eq!(err, PrincipalError::UnknownPrincipal);
        assert_eq!(err.code(), E_PRINCIPAL_UNKNOWN);
    }

    #[test]
    fn test_missing_san_is_rejected() {
        let root = pki_root();
        let registry = PrincipalRegistry::new(root.path().to_path_buf(), None);
        assert_eq!(registry.authorize("", "", "").unwrap_err(), PrincipalError::SanMissing);
    }

    #[test]
    fn test_service_identity_is_known_and_unrestricted() {
        let root = pki_root();
        write_identity(
            root.path(),
            "pr_svc",
            &serde_json::json!({"kind": "service", "serial": "AA"}),
        );
        let registry = PrincipalRegistry::new(root.path().to_path_buf(), None);
        let ident = registry.authorize("pr_svc", "AA", "fp").unwrap();
        assert!(ident.is_authenticated());
        assert!(ident.workspace_allowed("1"));
    }

    #[test]
    fn test_agent_identity_requires_grant() {
        let root = pki_root();
        write_identity(
            root.path(),
            "pr_agent",
            &serde_json::json!({"kind": "agent", "workspace_ids": ["1"]}),
        );
        let registry = PrincipalRegistry::new(root.path().to_path_buf(), None);
        let ident = registry.authorize("pr_agent", "AA", "fp").unwrap();
        assert!(ident.workspace_allowed("1"));
        assert!(!ident.workspace_allowed("2"));
    }

    #[test]
    fn test_revoked_marker_is_hot_reloaded_per_connection() {
        let root = pki_root();
        write_identity(root.path(), "pr_agent", &serde_json::json!({"kind": "agent"}));
        let registry = PrincipalRegistry::new(root.path().to_path_buf(), None);
        assert!(registry.authorize("pr_agent", "AA", "fp").is_ok());

        std::fs::write(
            root.path().join("identities").join("pr_agent").join("revoked"),
            b"",
        )
        .unwrap();
        assert_eq!(
            registry.authorize("pr_agent", "AA", "fp").unwrap_err(),
            PrincipalError::Revoked
        );
    }

    #[test]
    fn test_db_revocation_serial_hot_reload() {
        let root = pki_root();
        let db_dir = tempfile::tempdir().unwrap();
        let db_path = db_dir.path().join("callwarden.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE pki_principals (principal_id TEXT PRIMARY KEY, status TEXT NOT NULL, kind TEXT);
             CREATE TABLE pki_revocations (cert_serial TEXT PRIMARY KEY);
             CREATE TABLE pki_issued_certs (cert_serial TEXT PRIMARY KEY, status TEXT NOT NULL);
             CREATE TABLE workspace_principal_grants (principal_id TEXT NOT NULL, workspace_id TEXT NOT NULL, revoked_at REAL);
             INSERT INTO pki_principals (principal_id, status, kind) VALUES ('pr_agent', 'active', 'agent');",
        )
        .unwrap();
        drop(conn);

        let registry = PrincipalRegistry::new(root.path().to_path_buf(), Some(db_path.clone()));
        let ident = registry.authorize("pr_agent", "AA", "fp").unwrap();
        assert!(ident.is_authenticated());

        let conn = Connection::open(&db_path).unwrap();
        conn.execute(
            "INSERT INTO pki_revocations (cert_serial) VALUES ('AA')",
            [],
        )
        .unwrap();
        drop(conn);

        registry.refresh_now();
        assert_eq!(
            registry.authorize("pr_agent", "AA", "fp").unwrap_err(),
            PrincipalError::Revoked
        );

        // 空 serial 的连接仍可用（只吊销该证书，不吊销 principal）
        assert!(registry.authorize("pr_agent", "", "fp").is_ok());
    }

    #[test]
    fn test_db_grant_union_with_file_grants() {
        let root = pki_root();
        write_identity(
            root.path(),
            "pr_agent",
            &serde_json::json!({"kind": "agent", "workspace_ids": ["1"]}),
        );
        let db_dir = tempfile::tempdir().unwrap();
        let db_path = db_dir.path().join("callwarden.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE pki_principals (principal_id TEXT PRIMARY KEY, status TEXT NOT NULL, kind TEXT);
             CREATE TABLE pki_revocations (cert_serial TEXT PRIMARY KEY);
             CREATE TABLE pki_issued_certs (cert_serial TEXT PRIMARY KEY, status TEXT NOT NULL);
             CREATE TABLE workspace_principal_grants (principal_id TEXT NOT NULL, workspace_id TEXT NOT NULL, revoked_at REAL);
             INSERT INTO pki_principals (principal_id, status, kind) VALUES ('pr_agent', 'active', 'agent');
             INSERT INTO workspace_principal_grants (principal_id, workspace_id) VALUES ('pr_agent', '9');",
        )
        .unwrap();
        drop(conn);

        let registry = PrincipalRegistry::new(root.path().to_path_buf(), Some(db_path));
        registry.refresh_now();
        let ident = registry.authorize("pr_agent", "AA", "fp").unwrap();
        assert!(ident.workspace_allowed("1"));
        assert!(ident.workspace_allowed("9"));
        assert!(!ident.workspace_allowed("2"));
    }

    #[test]
    fn test_record_issued_certificate_writes_disk_grant() {
        let root = pki_root();
        let registry = PrincipalRegistry::new(root.path().to_path_buf(), None);
        registry
            .record_issued_certificate("pr_new", "BB", "fp", "7")
            .unwrap();
        // 只有 grants.json（无 meta.json）时 kind 未知 → 需要 meta；先补 meta
        write_identity(root.path(), "pr_new", &serde_json::json!({"kind": "agent"}));
        let ident = registry.authorize("pr_new", "BB", "fp").unwrap();
        assert!(ident.workspace_allowed("7"));
        assert!(!ident.workspace_allowed("8"));
    }

    #[test]
    fn test_scope_overflow_fails_closed() {
        let root = pki_root();
        let ids: Vec<String> = (0..100).map(|i| format!("ws-{:030}", i)).collect();
        write_identity(
            root.path(),
            "pr_agent",
            &serde_json::json!({"kind": "agent", "workspace_ids": ids}),
        );
        let registry = PrincipalRegistry::new(root.path().to_path_buf(), None);
        let ident = registry.authorize("pr_agent", "AA", "fp").unwrap();
        assert!(ident.scopes_overflow);
        assert!(!ident.workspace_allowed("ws-000000000000000000000000000000"));
    }
}
