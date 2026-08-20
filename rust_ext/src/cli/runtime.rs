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

    pub fn success_text(stdout: String, route: RouteUsed) -> Self {
        Self {
            exit_code: 0,
            stdout,
            stderr: String::new(),
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

    /// 打开本地读写数据库，仅供已定义事务边界的写命令使用。
    pub fn open_local_write_db(&self) -> Result<Connection, String> {
        let conn = Connection::open_with_flags(
            &self.db_path,
            OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_NO_MUTEX,
        )
        .map_err(|error| {
            format!(
                "cannot open local Call Warden database {} for writing: {error}",
                self.db_path.display()
            )
        })?;
        conn.busy_timeout(Duration::from_secs(5))
            .map_err(|error| format!("cannot configure SQLite busy_timeout: {error}"))?;
        conn.execute_batch("PRAGMA foreign_keys=ON; PRAGMA journal_mode=WAL;")
            .map_err(|error| format!("cannot configure writable SQLite connection: {error}"))?;
        Ok(conn)
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

    /// 执行写命令。auto 只在写入前选择一次路由，写入失败后禁止跨源回退。
    pub fn execute_write_with<L, E>(&self, local: L, enterprise: E) -> CommandResult
    where
        L: FnOnce() -> Result<String, String>,
        E: FnOnce() -> Result<String, String>,
    {
        self.execute_write_with_availability(self.daemon_available(), local, enterprise)
    }

    fn execute_write_with_availability<L, E>(
        &self,
        daemon_available: bool,
        local: L,
        enterprise: E,
    ) -> CommandResult
    where
        L: FnOnce() -> Result<String, String>,
        E: FnOnce() -> Result<String, String>,
    {
        match self.mode {
            DaemonMode::Local => result_text_from_source(local(), RouteUsed::Local),
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
                result_text_from_source(enterprise(), RouteUsed::Enterprise)
            }
            DaemonMode::Auto if daemon_available => {
                result_text_from_source(enterprise(), RouteUsed::Enterprise)
            }
            // auto 模式在路由选择时才须感知 daemon 可用性：daemon 不可用时与读路径一致
            // 回落本地（仅限"写入前"，见 execute_write_with 的"禁止跨源回退"约束）；
            // daemon 可用但写入失败时则由上面的分支返回失败，不回退本地。
            DaemonMode::Auto => result_text_from_source(local(), RouteUsed::Local),
        }
    }

    /// 调用 daemon RPC。支持 Unix UDS 和 Windows Named Pipe。
    pub fn daemon_call(&self, method: &str, params: Value) -> Result<Value, String> {
        let mut params = params;
        if let Value::Object(ref mut map) = params {
            if !map.contains_key("request_id") {
                let pid = std::process::id();
                let now = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
                map.insert("request_id".to_string(), Value::String(format!("req-{pid}-{now}")));
            }
        }
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
            use std::io::{Read, Write};
            use std::os::windows::io::FromRawHandle;
            use windows_sys::Win32::Foundation::INVALID_HANDLE_VALUE;
            use windows_sys::Win32::Storage::FileSystem::{
                CreateFileW, FILE_SHARE_READ, FILE_SHARE_WRITE, OPEN_EXISTING,
            };
            use windows_sys::Win32::System::Pipes::WaitNamedPipeW;

            let generic_read = 0x80000000u32;
            let generic_write = 0x40000000u32;

            let pipe_name = if self.socket_path.to_string_lossy().starts_with(r"\\.\pipe\") {
                self.socket_path.to_string_lossy().to_string()
            } else {
                let current_sid = crate::daemon::transport_windows::get_current_user_sid()
                    .unwrap_or_else(|_| "unknown".to_string());
                format!(r"\\.\pipe\callwarden-{current_sid}")
            };

            let wide: Vec<u16> = pipe_name.encode_utf16().chain(std::iter::once(0)).collect();
            unsafe {
                WaitNamedPipeW(wide.as_ptr(), (self.timeout.as_secs() * 1000) as u32);
            }

            let handle = unsafe {
                CreateFileW(
                    wide.as_ptr(),
                    generic_read | generic_write,
                    FILE_SHARE_READ | FILE_SHARE_WRITE,
                    std::ptr::null(),
                    OPEN_EXISTING,
                    0,
                    std::ptr::null_mut(),
                )
            };

            if handle == INVALID_HANDLE_VALUE {
                return Err(format!("failed to connect to Windows Named Pipe at {}", pipe_name));
            }

            let mut file = unsafe { std::fs::File::from_raw_handle(handle as _) };

            let req_body = serde_json::json!({
                "id": 1,
                "method": method,
                "params": params,
            });
            let payload = serde_json::to_vec(&req_body)
                .map_err(|e| format!("json encode error: {e}"))?;
            let len_bytes = (payload.len() as u32).to_be_bytes();

            file.write_all(&len_bytes).map_err(|e| format!("write len error: {e}"))?;
            file.write_all(&payload).map_err(|e| format!("write payload error: {e}"))?;
            file.flush().map_err(|e| format!("flush error: {e}"))?;

            let mut len_buf = [0u8; 4];
            file.read_exact(&mut len_buf).map_err(|e| format!("read len error: {e}"))?;
            let resp_len = u32::from_be_bytes(len_buf) as usize;

            let mut resp_buf = vec![0u8; resp_len];
            file.read_exact(&mut resp_buf).map_err(|e| format!("read resp error: {e}"))?;

            let resp_json: Value = serde_json::from_slice(&resp_buf)
                .map_err(|e| format!("parse resp json error: {e}"))?;

            if resp_json.get("ok").and_then(|v| v.as_bool()) == Some(true) {
                Ok(resp_json.get("result").cloned().unwrap_or(Value::Null))
            } else {
                let err_msg = resp_json.get("error").map(|v| v.to_string()).unwrap_or_else(|| "unknown RPC error".to_string());
                Err(format!("daemon RPC {method} returned error: {err_msg}"))
            }
        }
    }
}

fn result_from_source(result: Result<Value, String>, route: RouteUsed) -> CommandResult {
    match result {
        Ok(value) => CommandResult::success_json(&value, route),
        Err(error) => CommandResult::failure(1, error, route),
    }
}

fn result_text_from_source(result: Result<String, String>, route: RouteUsed) -> CommandResult {
    match result {
        Ok(value) => CommandResult::success_text(value, route),
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
    fn auto_write_does_not_fallback_after_enterprise_failure() {
        let runtime = options(DaemonMode::Auto, PathBuf::from("unused.db"));
        let result = runtime.execute_write_with_availability(
            true,
            || panic!("write route must not cross-fallback after daemon selection"),
            || Err("generation rejected".to_string()),
        );
        assert_eq!(result.exit_code, 1);
        assert_eq!(result.route, RouteUsed::Enterprise);
        assert!(result.stderr.contains("generation rejected"));
    }

    #[test]
    fn auto_write_uses_local_only_when_daemon_is_absent() {
        let runtime = options(DaemonMode::Auto, PathBuf::from("unused.db"));
        let result = runtime.execute_write_with_availability(
            false,
            || Ok("local write".to_string()),
            || panic!("enterprise closure must not run"),
        );
        assert_eq!(result.exit_code, 0);
        assert_eq!(result.route, RouteUsed::Local);
        assert_eq!(result.stdout, "local write");
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
