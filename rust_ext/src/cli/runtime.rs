//! Rust `cw` 的统一执行上下文与数据源路由。

use std::path::{Path, PathBuf};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};
use serde_json::Value;

use super::router::{daemon_socket_path, get_daemon_mode, is_daemon_available, DaemonMode};

/// 命令实际使用的数据源。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RouteUsed {
    Local,
    Enterprise,
    None,
}

/// 所有 Rust CLI 命令的统一结果。
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct CommandResult {
    pub exit_code: i32,
    pub stdout: String,
    pub stderr: String,
    pub route: RouteUsed,
}

impl CommandResult {
    pub fn success_json(value: &Value, route: RouteUsed) -> Self {
        match serde_json::to_string_pretty(value) {
            Ok(stdout) => Self {
                exit_code: 0,
                stdout,
                stderr: String::new(),
                route,
            },
            Err(error) => Self::failure(
                1,
                format!("failed to serialize command result: {error}"),
                route,
            ),
        }
    }

    pub fn failure(exit_code: i32, stderr: String, route: RouteUsed) -> Self {
        Self {
            exit_code,
            stdout: String::new(),
            stderr,
            route,
        }
    }
}

/// CLI 全局执行选项。
#[derive(Debug, Clone)]
pub struct RuntimeOptions {
    pub mode: DaemonMode,
    pub socket_path: PathBuf,
    pub db_path: PathBuf,
    pub workspace_id: Option<String>,
    pub timeout: Duration,
}

impl RuntimeOptions {
    pub fn from_overrides(
        mode: Option<DaemonMode>,
        socket_path: Option<PathBuf>,
        db_path: Option<PathBuf>,
        workspace_id: Option<String>,
        timeout_secs: u64,
    ) -> Self {
        Self {
            mode: mode.unwrap_or_else(get_daemon_mode),
            socket_path: socket_path.unwrap_or_else(daemon_socket_path),
            db_path: db_path.unwrap_or_else(default_user_db_path),
            workspace_id,
            timeout: Duration::from_secs(timeout_secs),
        }
    }

    /// 打开本地只读数据库，写命令不得复用此入口。
    pub fn open_local_db(&self) -> Result<Connection, String> {
        Connection::open_with_flags(
            &self.db_path,
            OpenFlags::SQLITE_OPEN_READ_ONLY | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|error| {
            format!(
                "cannot open local Call Warden database {}: {error}",
                self.db_path.display()
            )
        })
    }

    /// 返回显式 workspace，或从本地库解析唯一 active workspace。
    pub fn resolve_local_workspace_id(&self, conn: &Connection) -> Result<i64, String> {
        if let Some(workspace_id) = self.workspace_id.as_deref() {
            let workspace_id = workspace_id.parse::<i64>().map_err(|_| {
                format!("local workspace_id must be a positive integer, got {workspace_id:?}")
            })?;
            if workspace_id <= 0 {
                return Err("workspace_id must be greater than zero".to_string());
            }
            return Ok(workspace_id);
        }

        let mut stmt = conn
            .prepare("SELECT id FROM workspaces WHERE is_active = 1 ORDER BY id LIMIT 2")
            .map_err(|error| format!("cannot query active workspace: {error}"))?;
        let ids = stmt
            .query_map([], |row| row.get::<_, i64>(0))
            .map_err(|error| format!("cannot query active workspace: {error}"))?
            .collect::<Result<Vec<_>, _>>()
            .map_err(|error| format!("cannot read active workspace: {error}"))?;

        match ids.as_slice() {
            [workspace_id] => Ok(*workspace_id),
            [] => Err(
                "no active workspace; pass --workspace-id or activate a workspace first"
                    .to_string(),
            ),
            _ => Err(
                "multiple active workspaces; pass --workspace-id to avoid ambiguous data"
                    .to_string(),
            ),
        }
    }

    pub fn daemon_available(&self) -> bool {
        is_daemon_available(&self.socket_path, std::env::consts::OS)
    }

    /// 执行只读命令。auto 模式下 daemon 调用失败会回退本地。
    pub fn execute_read_with<L, E>(&self, local: L, enterprise: E) -> CommandResult
    where
        L: FnOnce() -> Result<Value, String>,
        E: FnOnce() -> Result<Value, String>,
    {
        self.execute_read_with_availability(self.daemon_available(), local, enterprise)
    }

    fn execute_read_with_availability<L, E>(
        &self,
        daemon_available: bool,
        local: L,
        enterprise: E,
    ) -> CommandResult
    where
        L: FnOnce() -> Result<Value, String>,
        E: FnOnce() -> Result<Value, String>,
    {
        match self.mode {
            DaemonMode::Local => result_from_source(local(), RouteUsed::Local),
            DaemonMode::Enterprise => {
                if !daemon_available {
                    return CommandResult::failure(
                        2,
                        format!(
                            "enterprise daemon is unavailable at {}",
                            self.socket_path.display()
                        ),
                        RouteUsed::None,
                    );
                }
                result_from_source(enterprise(), RouteUsed::Enterprise)
            }
            DaemonMode::Auto => {
                if daemon_available {
                    match enterprise() {
                        Ok(value) => {
                            return CommandResult::success_json(&value, RouteUsed::Enterprise);
                        }
                        Err(enterprise_error) => {
                            let mut result = result_from_source(local(), RouteUsed::Local);
                            if result.exit_code == 0 {
                                result.stderr = format!(
                                    "warning: daemon query failed; used local database: {enterprise_error}"
                                );
                            }
                            return result;
                        }
                    }
                }
                result_from_source(local(), RouteUsed::Local)
            }
        }
    }

    /// 调用 daemon RPC。非 Unix 平台明确返回不支持。
    pub fn daemon_call(&self, method: &str, params: Value) -> Result<Value, String> {
        #[cfg(unix)]
        {
            let client = crate::daemon::client::unix::UnixDaemonRpcClient::new(
                &self.socket_path.to_string_lossy(),
            )
            .with_timeout(self.timeout);
            client
                .call(method, params)
                .map_err(|error| format!("daemon RPC {method} failed: {error}"))
        }
        #[cfg(not(unix))]
        {
            let _ = (method, params);
            Err("enterprise daemon transport is unavailable on this platform".to_string())
        }
    }
}

fn result_from_source(result: Result<Value, String>, route: RouteUsed) -> CommandResult {
    match result {
        Ok(value) => CommandResult::success_json(&value, route),
        Err(error) => CommandResult::failure(1, error, route),
    }
}

pub fn default_user_db_path() -> PathBuf {
    home_dir().join(".callwarden").join("callwarden.db")
}

fn home_dir() -> PathBuf {
    std::env::var_os("HOME")
        .or_else(|| std::env::var_os("USERPROFILE"))
        .map(PathBuf::from)
        .unwrap_or_else(|| Path::new(".").to_path_buf())
}

#[cfg(test)]
mod tests {
    use super::*;
    use rusqlite::params;

    fn options(mode: DaemonMode, db_path: PathBuf) -> RuntimeOptions {
        RuntimeOptions {
            mode,
            socket_path: db_path.with_extension("missing.sock"),
            db_path,
            workspace_id: None,
            timeout: Duration::from_secs(1),
        }
    }

    #[test]
    fn local_mode_never_calls_enterprise() {
        let runtime = options(DaemonMode::Local, PathBuf::from("unused.db"));
        let result = runtime.execute_read_with(
            || Ok(serde_json::json!({"source": "local"})),
            || panic!("enterprise closure must not run"),
        );
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.route, RouteUsed::Local);
        assert!(result.stdout.contains("\"local\""));
    }

    #[test]
    fn enterprise_mode_is_fail_closed_when_socket_is_missing() {
        let runtime = options(DaemonMode::Enterprise, PathBuf::from("unused.db"));
        let result = runtime.execute_read_with(
            || panic!("local closure must not run"),
            || panic!("enterprise closure must not run without a socket"),
        );
        assert_eq!(result.exit_code, 2);
        assert_eq!(result.route, RouteUsed::None);
        assert!(result.stderr.contains("unavailable"));
    }

    #[test]
    fn auto_mode_uses_local_when_socket_is_missing() {
        let runtime = options(DaemonMode::Auto, PathBuf::from("unused.db"));
        let result = runtime.execute_read_with(
            || Ok(serde_json::json!({"source": "local"})),
            || panic!("enterprise closure must not run"),
        );
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.route, RouteUsed::Local);
    }

    #[test]
    fn enterprise_mode_uses_enterprise_when_daemon_is_available() {
        let runtime = options(DaemonMode::Enterprise, PathBuf::from("unused.db"));
        let result = runtime.execute_read_with_availability(
            true,
            || panic!("local closure must not run"),
            || Ok(serde_json::json!({"source": "enterprise"})),
        );
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.route, RouteUsed::Enterprise);
        assert!(result.stdout.contains("\"enterprise\""));
    }

    #[test]
    fn auto_mode_falls_back_when_daemon_call_fails() {
        let runtime = options(DaemonMode::Auto, PathBuf::from("unused.db"));
        let result = runtime.execute_read_with_availability(
            true,
            || Ok(serde_json::json!({"source": "local"})),
            || Err("stale socket".to_string()),
        );
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.route, RouteUsed::Local);
        assert!(result.stderr.contains("stale socket"));
    }

    #[test]
    fn resolves_exactly_one_active_workspace() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("callwarden.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0
            );",
        )
        .unwrap();
        conn.execute(
            "INSERT INTO workspaces(id, name, is_active) VALUES (?1, ?2, 1)",
            params![17_i64, "active"],
        )
        .unwrap();

        let runtime = options(DaemonMode::Local, db_path);
        assert_eq!(runtime.resolve_local_workspace_id(&conn).unwrap(), 17);
    }

    #[test]
    fn rejects_ambiguous_active_workspaces() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("callwarden.db");
        let conn = Connection::open(&db_path).unwrap();
        conn.execute_batch(
            "CREATE TABLE workspaces (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0
            );
            INSERT INTO workspaces(id, name, is_active) VALUES
                (1, 'one', 1),
                (2, 'two', 1);",
        )
        .unwrap();

        let runtime = options(DaemonMode::Local, db_path);
        let error = runtime.resolve_local_workspace_id(&conn).unwrap_err();
        assert!(error.contains("multiple active workspaces"));
    }

    #[test]
    fn preserves_enterprise_workspace_instance_id() {
        let runtime = RuntimeOptions::from_overrides(
            Some(DaemonMode::Enterprise),
            None,
            None,
            Some("ws-user-project-main".to_string()),
            30,
        );
        assert_eq!(
            runtime.workspace_id.as_deref(),
            Some("ws-user-project-main")
        );
    }

    #[test]
    fn rejects_non_numeric_workspace_id_for_local_sqlite() {
        let temp = tempfile::tempdir().unwrap();
        let db_path = temp.path().join("callwarden.db");
        let conn = Connection::open(&db_path).unwrap();
        let mut runtime = options(DaemonMode::Local, db_path);
        runtime.workspace_id = Some("ws-enterprise-id".to_string());
        let error = runtime.resolve_local_workspace_id(&conn).unwrap_err();
        assert!(error.contains("positive integer"));
    }
}
