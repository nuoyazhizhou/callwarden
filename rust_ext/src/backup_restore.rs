//! Rust backup/restore core used by the daemon data backup managers.
//!
//! The public functions deliberately return JSON strings. The Python wrapper
//! keeps the historical dictionary API and owns only configuration adaptation
//! and rollback selection; all backup I/O and validation lives here.

use pyo3::exceptions::{PyRuntimeError, PyValueError};
use pyo3::prelude::*;
use rusqlite::Connection;
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const DAEMON_VERSION: &str = "1.0.0";

fn error(message: impl Into<String>) -> PyErr {
    PyRuntimeError::new_err(message.into())
}

fn now_secs() -> f64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs_f64())
        .unwrap_or(0.0)
}

fn validate_backup_id(backup_id: &str) -> Result<(), String> {
    if backup_id.is_empty() || backup_id == "." || backup_id == ".." {
        return Err("backup_id 不能为空".to_string());
    }
    let mut components = Path::new(backup_id).components();
    match (components.next(), components.next()) {
        (Some(Component::Normal(_)), None) => Ok(()),
        _ => Err(format!("非法 backup_id: {backup_id}")),
    }
}

fn safe_backup_dir(backup_root: &Path, backup_id: &str) -> Result<PathBuf, String> {
    validate_backup_id(backup_id)?;
    Ok(backup_root.join(backup_id))
}

fn file_sha256(path: &Path) -> Result<String, String> {
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

fn copy_tree(source: &Path, destination: &Path) -> Result<usize, String> {
    if !source.is_dir() {
        return Err(format!("源目录不存在: {}", source.display()));
    }
    fs::create_dir_all(destination)
        .map_err(|e| format!("创建目录失败 {}: {e}", destination.display()))?;
    let mut file_count = 0;
    let entries =
        fs::read_dir(source).map_err(|e| format!("读取目录失败 {}: {e}", source.display()))?;
    for entry in entries {
        let entry = entry.map_err(|e| format!("读取目录项失败: {e}"))?;
        let source_path = entry.path();
        let destination_path = destination.join(entry.file_name());
        let kind = entry
            .file_type()
            .map_err(|e| format!("读取文件类型失败 {}: {e}", source_path.display()))?;
        if kind.is_dir() {
            file_count += copy_tree(&source_path, &destination_path)?;
        } else if kind.is_file() {
            fs::copy(&source_path, &destination_path).map_err(|e| {
                format!(
                    "复制文件失败 {} -> {}: {e}",
                    source_path.display(),
                    destination_path.display()
                )
            })?;
            file_count += 1;
        }
    }
    Ok(file_count)
}

fn sqlite_snapshot(source: &Path, destination: &Path) -> Result<(), String> {
    if destination.exists() {
        fs::remove_file(destination)
            .map_err(|e| format!("删除旧备份文件失败 {}: {e}", destination.display()))?;
    }
    let connection = Connection::open(source)
        .map_err(|e| format!("打开 SQLite 文件失败 {}: {e}", source.display()))?;
    connection
        .busy_timeout(std::time::Duration::from_secs(5))
        .map_err(|e| format!("设置 SQLite busy_timeout 失败: {e}"))?;
    let _ = connection.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    let escaped = destination.to_string_lossy().replace('\'', "''");
    connection
        .execute_batch(&format!("VACUUM INTO '{escaped}'"))
        .map_err(|e| format!("SQLite VACUUM INTO 失败 {}: {e}", source.display()))?;
    Ok(())
}

fn backup_file(source: &Path, destination_dir: &Path, name: &str) -> Result<Option<Value>, String> {
    if !source.is_file() {
        return Ok(None);
    }
    let destination = destination_dir.join(name);
    if name.ends_with(".db") {
        sqlite_snapshot(source, &destination)?;
    } else {
        fs::copy(source, &destination).map_err(|e| {
            format!(
                "复制文件失败 {} -> {}: {e}",
                source.display(),
                destination.display()
            )
        })?;
    }
    let size = fs::metadata(&destination)
        .map_err(|e| format!("读取文件大小失败 {}: {e}", destination.display()))?
        .len();
    let sha256 = file_sha256(&destination)?;
    Ok(Some(json!({
        "name": name,
        "type": "file",
        "size": size,
        "sha256": sha256,
        "source_path": source.to_string_lossy(),
    })))
}

fn meta_checksum(py: Python<'_>, meta: &Value) -> PyResult<String> {
    // Reuse the verified Python-compatible serializer so Rust metadata remains
    // readable by old Python RestoreManager instances.
    let serialized =
        serde_json::to_string(meta).map_err(|e| error(format!("序列化备份元数据失败: {e}")))?;
    crate::daemon_query::backup_compute_meta_checksum(py, &serialized)
}

fn write_json(path: &Path, value: &Value) -> Result<(), String> {
    let content = serde_json::to_string_pretty(value)
        .map_err(|e| format!("序列化 JSON 失败 {}: {e}", path.display()))?;
    let mut file =
        fs::File::create(path).map_err(|e| format!("创建文件失败 {}: {e}", path.display()))?;
    file.write_all(content.as_bytes())
        .map_err(|e| format!("写入文件失败 {}: {e}", path.display()))?;
    file.sync_all()
        .map_err(|e| format!("同步文件失败 {}: {e}", path.display()))?;
    Ok(())
}

fn create_backup(
    py: Python<'_>,
    backup_root: &Path,
    backup_id: &str,
    registry_db: &Path,
    cas_db: &Path,
    audit_db: &Path,
    data_root: &Path,
    db_only: bool,
) -> PyResult<String> {
    let final_dir = safe_backup_dir(backup_root, backup_id).map_err(error)?;
    if final_dir.exists() {
        return Err(error(format!("备份已存在: {}", final_dir.display())));
    }
    fs::create_dir_all(backup_root).map_err(|e| error(format!("创建备份根目录失败: {e}")))?;
    let temp_dir = backup_root.join(format!(".{backup_id}.partial"));
    if temp_dir.exists() {
        return Err(error(format!("备份临时目录已存在: {}", temp_dir.display())));
    }
    fs::create_dir_all(&temp_dir).map_err(|e| error(format!("创建备份临时目录失败: {e}")))?;

    let result = (|| -> PyResult<Value> {
        let mut files = Vec::new();
        for (source, name) in [
            (registry_db, "registry.db"),
            (cas_db, "cas.db"),
            (audit_db, "audit.db"),
        ] {
            if let Some(info) = backup_file(source, &temp_dir, name).map_err(error)? {
                files.push(info);
            }
        }
        if !db_only {
            let daemon_config = data_root.join("daemon.json");
            if let Some(info) =
                backup_file(&daemon_config, &temp_dir, "daemon.json").map_err(error)?
            {
                files.push(info);
            }
            let snapshots = data_root.join("snapshots");
            if snapshots.is_dir() {
                let destination = temp_dir.join("snapshots");
                let file_count = copy_tree(&snapshots, &destination).map_err(error)?;
                files.push(json!({
                    "name": "snapshots/",
                    "type": "directory",
                    "file_count": file_count,
                }));
            }
        }
        let backup_type = if db_only { "db_only" } else { "full" };
        let total_size = files
            .iter()
            .filter_map(|file| file.get("size").and_then(Value::as_u64))
            .sum::<u64>();
        let mut meta = Map::new();
        meta.insert(
            "backup_id".to_string(),
            Value::String(backup_id.to_string()),
        );
        meta.insert("timestamp".to_string(), json!(now_secs()));
        meta.insert(
            "backup_type".to_string(),
            Value::String(backup_type.to_string()),
        );
        meta.insert(
            "daemon_version".to_string(),
            Value::String(DAEMON_VERSION.to_string()),
        );
        meta.insert("files".to_string(), Value::Array(files));
        meta.insert("total_size".to_string(), json!(total_size));
        let mut meta_value = Value::Object(meta);
        let checksum = meta_checksum(py, &meta_value)?;
        meta_value
            .as_object_mut()
            .expect("metadata must be object")
            .insert("checksum".to_string(), Value::String(checksum));
        write_json(&temp_dir.join("backup_meta.json"), &meta_value).map_err(error)?;
        fs::rename(&temp_dir, &final_dir)
            .map_err(|e| error(format!("发布备份目录失败 {}: {e}", final_dir.display())))?;
        Ok(meta_value)
    })();

    if result.is_err() && temp_dir.exists() {
        let _ = fs::remove_dir_all(&temp_dir);
    }
    let value = result?;
    serde_json::to_string(&value).map_err(|e| error(format!("序列化备份结果失败: {e}")))
}

fn read_meta(backup_dir: &Path) -> Result<Value, String> {
    let path = backup_dir.join("backup_meta.json");
    let content = fs::read_to_string(&path)
        .map_err(|e| format!("读取 backup_meta.json 失败 {}: {e}", path.display()))?;
    serde_json::from_str(&content).map_err(|e| format!("解析 backup_meta.json 失败: {e}"))
}

fn allowed_member(name: &str) -> bool {
    matches!(
        name,
        "registry.db" | "cas.db" | "audit.db" | "daemon.json" | "snapshots/"
    )
}

fn verify_impl(py: Python<'_>, backup_dir: &Path) -> PyResult<Value> {
    if !backup_dir.is_dir() {
        return Ok(json!({"status":"invalid", "error":"backup not found"}));
    }
    let meta_path = backup_dir.join("backup_meta.json");
    let meta_content = match fs::read_to_string(&meta_path) {
        Ok(content) => content,
        Err(error) => {
            return Ok(json!({
                "status":"invalid",
                "error":format!("读取 backup_meta.json 失败 {}: {error}", meta_path.display())
            }))
        }
    };
    let meta: Value = match serde_json::from_str(&meta_content) {
        Ok(value) => value,
        Err(error) => {
            return Ok(json!({
                "status":"invalid",
                "error":format!("解析 backup_meta.json 失败: {error}")
            }))
        }
    };
    let expected = meta
        .get("checksum")
        .and_then(Value::as_str)
        .unwrap_or_default();
    // 不把 timestamp 等浮点数重新解析再序列化；直接把磁盘上的 JSON
    // 交给 Python 兼容校验器，避免 serde_json 的数字格式化造成偶发假损坏。
    let actual = crate::daemon_query::backup_compute_meta_checksum(py, &meta_content)?;
    if expected != actual {
        return Ok(json!({"status":"corrupted", "error":"checksum mismatch"}));
    }
    let mut all_valid = true;
    let mut files = Vec::new();
    for info in meta
        .get("files")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let name = info.get("name").and_then(Value::as_str).unwrap_or_default();
        if !allowed_member(name) {
            all_valid = false;
            files.push(json!({"name":name,"valid":false,"reason":"unsupported backup member"}));
            continue;
        }
        if name == "snapshots/" {
            let valid = backup_dir.join("snapshots").is_dir();
            all_valid &= valid;
            files.push(json!({"name":name,"valid":valid}));
            continue;
        }
        let path = backup_dir.join(name);
        let expected_sha = info
            .get("sha256")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let valid = path.is_file()
            && !expected_sha.is_empty()
            && file_sha256(&path)
                .map(|hash| hash == expected_sha)
                .unwrap_or(false);
        all_valid &= valid;
        files.push(json!({
            "name":name,
            "valid":valid,
            "expected_sha256":expected_sha
        }));
    }
    Ok(json!({
        "status": if all_valid { "valid" } else { "corrupted" },
        "backup_id": meta.get("backup_id").cloned().unwrap_or(Value::Null),
        "files": files,
    }))
}

fn restore_impl(
    py: Python<'_>,
    backup_root: &Path,
    backup_id: &str,
    registry_db: &Path,
    cas_db: &Path,
    audit_db: &Path,
    data_root: &Path,
) -> PyResult<String> {
    let backup_dir = safe_backup_dir(backup_root, backup_id).map_err(error)?;
    let verification = verify_impl(py, &backup_dir)?;
    if verification.get("status").and_then(Value::as_str) != Some("valid") {
        return serde_json::to_string(&json!({
            "status":"failure",
            "backup_id":backup_id,
            "error":verification.get("error").cloned().unwrap_or_else(|| json!("backup verification failed")),
            "verification":verification,
        }))
        .map_err(|e| error(format!("序列化恢复失败结果失败: {e}")));
    }
    let meta = read_meta(&backup_dir).map_err(error)?;
    let mut restored = Vec::new();
    for info in meta
        .get("files")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let name = info.get("name").and_then(Value::as_str).unwrap_or_default();
        if name == "snapshots/" {
            let source = backup_dir.join("snapshots");
            let staging = data_root.join(format!(".snapshots-restore-{backup_id}"));
            if staging.exists() {
                fs::remove_dir_all(&staging)
                    .map_err(|e| error(format!("清理恢复临时目录失败: {e}")))?;
            }
            copy_tree(&source, &staging).map_err(error)?;
            let destination = data_root.join("snapshots");
            if destination.exists() {
                fs::remove_dir_all(&destination)
                    .map_err(|e| error(format!("删除旧 snapshots 失败: {e}")))?;
            }
            fs::rename(&staging, &destination)
                .map_err(|e| error(format!("发布 snapshots 失败: {e}")))?;
            restored.push(json!({"name":name,"status":"restored"}));
            continue;
        }
        let destination = match name {
            "registry.db" => registry_db.to_path_buf(),
            "cas.db" => cas_db.to_path_buf(),
            "audit.db" => audit_db.to_path_buf(),
            "daemon.json" => data_root.join("daemon.json"),
            _ => continue,
        };
        let source = backup_dir.join(name);
        if let Some(parent) = destination.parent() {
            fs::create_dir_all(parent).map_err(|e| error(format!("创建恢复目录失败: {e}")))?;
        }
        let staging = destination.with_extension("restore.tmp");
        if staging.exists() {
            let _ = fs::remove_file(&staging);
        }
        fs::copy(&source, &staging).map_err(|e| error(format!("恢复文件失败 {name}: {e}")))?;
        if destination.exists() {
            fs::remove_file(&destination)
                .map_err(|e| error(format!("删除旧文件失败 {name}: {e}")))?;
        }
        fs::rename(&staging, &destination)
            .map_err(|e| error(format!("发布恢复文件失败 {name}: {e}")))?;
        restored.push(json!({
            "name":name,
            "status":"restored",
            "dest_path":destination.to_string_lossy()
        }));
    }
    let result = json!({
        "status":"success",
        "backup_id":backup_id,
        "timestamp":now_secs(),
        "restored_files":restored,
        "backup_meta":meta,
    });
    serde_json::to_string(&result).map_err(|e| error(format!("序列化恢复结果失败: {e}")))
}

#[pyfunction]
pub fn backup_full(
    py: Python<'_>,
    backup_root: &str,
    backup_id: &str,
    registry_db: &str,
    cas_db: &str,
    audit_db: &str,
    data_root: &str,
) -> PyResult<String> {
    create_backup(
        py,
        Path::new(backup_root),
        backup_id,
        Path::new(registry_db),
        Path::new(cas_db),
        Path::new(audit_db),
        Path::new(data_root),
        false,
    )
}

#[pyfunction]
pub fn backup_db_only(
    py: Python<'_>,
    backup_root: &str,
    backup_id: &str,
    registry_db: &str,
    cas_db: &str,
    audit_db: &str,
    data_root: &str,
) -> PyResult<String> {
    create_backup(
        py,
        Path::new(backup_root),
        backup_id,
        Path::new(registry_db),
        Path::new(cas_db),
        Path::new(audit_db),
        Path::new(data_root),
        true,
    )
}

#[pyfunction]
pub fn restore_backup(
    py: Python<'_>,
    backup_root: &str,
    backup_id: &str,
    registry_db: &str,
    cas_db: &str,
    audit_db: &str,
    data_root: &str,
) -> PyResult<String> {
    restore_impl(
        py,
        Path::new(backup_root),
        backup_id,
        Path::new(registry_db),
        Path::new(cas_db),
        Path::new(audit_db),
        Path::new(data_root),
    )
}

#[pyfunction]
pub fn verify_backup(py: Python<'_>, backup_root: &str, backup_id: &str) -> PyResult<String> {
    let directory = safe_backup_dir(Path::new(backup_root), backup_id).map_err(error)?;
    serde_json::to_string(&verify_impl(py, &directory)?)
        .map_err(|e| error(format!("序列化验证结果失败: {e}")))
}

#[pyfunction]
pub fn list_backups(backup_root: &str) -> PyResult<String> {
    let root = Path::new(backup_root);
    let mut backups = Vec::new();
    if root.is_dir() {
        for entry in fs::read_dir(root).map_err(|e| error(format!("读取备份根目录失败: {e}")))?
        {
            let entry = entry.map_err(|e| error(format!("读取备份目录项失败: {e}")))?;
            if !entry.path().is_dir() {
                continue;
            }
            if let Ok(meta) = read_meta(&entry.path()) {
                backups.push(meta);
            }
        }
    }
    backups.sort_by(|left, right| {
        right
            .get("timestamp")
            .and_then(Value::as_f64)
            .unwrap_or_default()
            .partial_cmp(
                &left
                    .get("timestamp")
                    .and_then(Value::as_f64)
                    .unwrap_or_default(),
            )
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    serde_json::to_string(&backups).map_err(|e| error(format!("序列化备份列表失败: {e}")))
}

#[pyfunction]
pub fn delete_backup(backup_root: &str, backup_id: &str) -> PyResult<bool> {
    let directory =
        safe_backup_dir(Path::new(backup_root), backup_id).map_err(PyValueError::new_err)?;
    if !directory.is_dir() {
        return Ok(false);
    }
    fs::remove_dir_all(directory).map_err(|e| error(format!("删除备份失败: {e}")))?;
    Ok(true)
}

#[pyfunction]
pub fn cleanup_backups(backup_root: &str, keep_count: usize) -> PyResult<usize> {
    let values: Vec<Value> = serde_json::from_str(&list_backups(backup_root)?)
        .map_err(|e| error(format!("解析备份列表失败: {e}")))?;
    let mut deleted = 0;
    for meta in values.into_iter().skip(keep_count) {
        if let Some(backup_id) = meta.get("backup_id").and_then(Value::as_str) {
            if delete_backup(backup_root, backup_id)? {
                deleted += 1;
            }
        }
    }
    Ok(deleted)
}
