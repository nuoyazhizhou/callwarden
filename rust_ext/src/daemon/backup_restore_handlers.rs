//! Backup/restore 面 handler（SRV-003：server backup restore Python authority → Rust daemon）。
//!
//! 对应 `server/backup_restore.py` 中仍直接 open SQLite 的两个 Python authority 助手：
//! - `_backup_file`：单文件备份（`.db` 走 `VACUUM INTO` 一致性快照，其它走文件复制），并
//!   计算 sha256；
//! - `_is_rust_backup_rolled_back`：读 daemon 权威 `rollback_config`，判断
//!   `rust_daemon_backup_compute` feature 是否已回滚。
//!
//! 下沉后 Python `server/backup_restore.py` 仅作 daemon RPC 薄客户端，不再 `import sqlite3`、
//! 不再 open 本地 DB、不再执行业务 SQL。fail-closed：daemon 不可用时薄客户端抛错，
//! 绝不回退 Python SQLite 充当业务存储。
//!
//! 不变量：
//! - 数据源：daemon 进程内（文件系统 + `self.base.registry` 权威 DB）；薄客户端无本地权威。
//! - `backup_file` 是 admin-only Protected_Mutation（写盘），非 admin 直接拒绝，不降级；
//! - `is_rust_backup_rolled_back` 是只读 authority 读，返回稳定 `{"rolled_back": bool}`；
//!   registry 路径为空 / 表缺失 / 查询失败均 fail-closed 视为未回滚（`false`）。

use std::fs;
use std::io::Read;
use std::path::Path;

use rusqlite::Connection;
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use super::dispatch::{get_str_param_or, DaemonRpcError};

/// SRV-003 治理的 rollback feature 名（与 Python `_is_rust_backup_rolled_back` 一致）。
const RUST_BACKUP_COMPUTE_FEATURE: &str = "rust_daemon_backup_compute";

/// 计算文件 SHA-256（流式，避免大文件 OOM；对齐 `backup_restore.rs::file_sha256`）。
fn sha256_of_file(path: &Path) -> Result<String, String> {
    let mut file =
        fs::File::open(path).map_err(|e| format!("打开文件失败 {}: {e}", path.display()))?;
    let mut hash = Sha256::new();
    let mut buffer = [0_u8; 64 * 1024];
    loop {
        let read = file
            .read(&mut buffer)
            .map_err(|e| format!("读取文件失败 {}: {e}", path.display()))?;
        if read == 0 {
            break;
        }
        hash.update(&buffer[..read]);
    }
    Ok(hex::encode(hash.finalize()))
}

/// `mcp.backup_restore.backup_file` —— 在 daemon 内备份单个文件（对齐 Python
/// `BackupManager._backup_file`）。
///
/// - 参数缺失 → `invalid_params`；
/// - 源文件不存在 → 返回 `null`（薄客户端据此跳过，对齐 Python 返回 `None`）；
/// - `.db` 文件 → 先 `wal_checkpoint(PASSIVE)` 再 `VACUUM INTO` 一致性快照，失败降级为文件复制；
/// - 其它文件 → `fs::copy`；
/// - 返回 `{"name","type":"file","size","sha256","source_path"}`。
pub fn handle_backup_file(params: &Value) -> Result<Value, DaemonRpcError> {
    let src_path = get_str_param_or(params, "src_path", "");
    let dest_dir = get_str_param_or(params, "dest_dir", "");
    let dest_name = get_str_param_or(params, "dest_name", "");
    if src_path.is_empty() || dest_dir.is_empty() || dest_name.is_empty() {
        return Err(DaemonRpcError::invalid_params(
            "backup_file 需要 src_path/dest_dir/dest_name",
        ));
    }
    let src = Path::new(src_path.as_str());
    if !src.is_file() {
        // 对齐 Python `_backup_file` 源不存在返回 None（薄客户端据此跳过）
        return Ok(Value::Null);
    }
    let dest_dir_path = Path::new(dest_dir.as_str());
    fs::create_dir_all(dest_dir_path).map_err(|e| {
        DaemonRpcError::internal_error(format!("创建备份目录失败 {dest_dir}: {e}"))
    })?;
    let dest_path = dest_dir_path.join(&dest_name);

    if dest_name.ends_with(".db") {
        // VACUUM INTO 要求目标不存在；先清场再快照
        if dest_path.exists() {
            let _ = fs::remove_file(&dest_path);
        }
        if let Ok(src_conn) = Connection::open(src) {
            let _ = src_conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
            let escaped = dest_path.to_string_lossy().replace('\'', "''");
            let sql = format!("VACUUM INTO '{escaped}'");
            if src_conn.execute_batch(&sql).is_err() {
                // 降级为文件复制
                let _ = fs::copy(src, &dest_path);
            }
        } else {
            let _ = fs::copy(src, &dest_path);
        }
    } else {
        fs::copy(src, &dest_path).map_err(|e| {
            DaemonRpcError::internal_error(format!(
                "复制文件失败 {src_path} -> {dest_path:?}: {e}"
            ))
        })?;
    }

    let size = fs::metadata(&dest_path)
        .map_err(|e| {
            DaemonRpcError::internal_error(format!("读取文件大小失败 {dest_path:?}: {e}"))
        })?
        .len();
    let sha256 = sha256_of_file(&dest_path).map_err(DaemonRpcError::internal_error)?;
    Ok(json!({
        "name": dest_name,
        "type": "file",
        "size": size,
        "sha256": sha256,
        "source_path": src_path,
    }))
}

/// `mcp.backup_restore.is_rust_backup_rolled_back` —— 读 daemon 权威 `rollback_config`，
/// 判断 `rust_daemon_backup_compute` feature 是否已回滚（对齐 Python `_is_rust_backup_rolled_back`）。
///
/// fail-closed：registry 路径为空 / 表缺失 / 查询失败 → 视为未回滚（`{"rolled_back": false}`）。
pub fn handle_is_rust_backup_rolled_back(
    registry_db_path: &Path,
) -> Result<Value, DaemonRpcError> {
    if registry_db_path.as_os_str().is_empty() {
        return Ok(json!({"rolled_back": false, "reason": "registry_db_unconfigured"}));
    }
    let conn = match Connection::open(registry_db_path) {
        Ok(c) => c,
        Err(_) => return Ok(json!({"rolled_back": false, "reason": "registry_db_open_failed"})),
    };
    let value: i64 = conn
        .query_row(
            "SELECT COALESCE((SELECT rollback_flag FROM rollback_config \
             WHERE feature_name = ?1 ORDER BY updated_at DESC LIMIT 1), 0)",
            rusqlite::params![RUST_BACKUP_COMPUTE_FEATURE],
            |row| row.get(0),
        )
        .unwrap_or(0);
    Ok(json!({"rolled_back": value == 1}))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn test_backup_file_plain_copy_and_sha256() {
        let dir = std::env::temp_dir().join(format!("srv003_bf_{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let src = dir.join("src.txt");
        fs::write(&src, b"hello callwarden").unwrap();
        let dest_dir = dir.join("out");
        fs::create_dir_all(&dest_dir).unwrap();

        let r = handle_backup_file(&json!({
            "src_path": src.to_string_lossy(),
            "dest_dir": dest_dir.to_string_lossy(),
            "dest_name": "src.txt",
        }))
        .unwrap();
        assert_eq!(r["name"], json!("src.txt"));
        assert_eq!(r["type"], json!("file"));
        // 已知内容 sha256
        let mut h = Sha256::new();
        h.update(b"hello callwarden");
        assert_eq!(r["sha256"], json!(hex::encode(h.finalize())));
        assert_eq!(r["size"], json!(16));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_backup_file_missing_source_returns_null() {
        let r = handle_backup_file(&json!({
            "src_path": "/nonexistent/path/to/file.txt",
            "dest_dir": std::env::temp_dir().to_string_lossy(),
            "dest_name": "x.txt",
        }))
        .unwrap();
        assert!(r.is_null(), "源缺失应返回 null");
    }

    #[test]
    fn test_backup_file_invalid_params() {
        let e = handle_backup_file(&json!({"src_path": "a"})).unwrap_err();
        assert_eq!(e.code, "invalid_params");
    }

    #[test]
    fn test_backup_file_db_vacuum_into() {
        let dir = std::env::temp_dir().join(format!("srv003_bfdb_{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let src = dir.join("src.db");
        {
            let c = Connection::open(&src).unwrap();
            c.execute_batch("CREATE TABLE t(x INTEGER); INSERT INTO t VALUES (1),(2),(3);")
                .unwrap();
        }
        let dest_dir = dir.join("out");
        fs::create_dir_all(&dest_dir).unwrap();
        let r = handle_backup_file(&json!({
            "src_path": src.to_string_lossy(),
            "dest_dir": dest_dir.to_string_lossy(),
            "dest_name": "src.db",
        }))
        .unwrap();
        assert_eq!(r["name"], json!("src.db"));
        // VACUUM INTO 产物应是有效 SQLite
        let out = dest_dir.join("src.db");
        assert!(out.exists());
        let verify = Connection::open(&out).unwrap();
        let n: i64 = verify
            .query_row("SELECT COUNT(*) FROM t", [], |row| row.get(0))
            .unwrap();
        assert_eq!(n, 3);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_is_rust_backup_rolled_back_reads_flag() {
        let dir = std::env::temp_dir().join(format!("srv003_rb_{}", std::process::id()));
        let _ = fs::create_dir_all(&dir);
        let db = dir.join("registry.db");
        {
            let c = Connection::open(&db).unwrap();
            c.execute_batch(
                "CREATE TABLE rollback_config (\
                   id INTEGER PRIMARY KEY, feature_name TEXT, rollback_flag INTEGER, updated_at REAL);",
            )
            .unwrap();
            c.execute(
                "INSERT INTO rollback_config (feature_name, rollback_flag, updated_at) \
                 VALUES (?1, 1, 1.0)",
                rusqlite::params![RUST_BACKUP_COMPUTE_FEATURE],
            )
            .unwrap();
        }
        let r = handle_is_rust_backup_rolled_back(&db).unwrap();
        assert_eq!(r["rolled_back"], json!(true));

        // 无表时 fail-closed 视为未回滚
        let empty = dir.join("empty.db");
        {
            let c = Connection::open(&empty).unwrap();
            let _ = c.execute_batch("CREATE TABLE other(x);");
        }
        let r2 = handle_is_rust_backup_rolled_back(&empty).unwrap();
        assert_eq!(r2["rolled_back"], json!(false));
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn test_is_rust_backup_rolled_back_unconfigured() {
        let r = handle_is_rust_backup_rolled_back(Path::new("")).unwrap();
        assert_eq!(r["rolled_back"], json!(false));
    }
}
