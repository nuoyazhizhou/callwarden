//! 文件/构建面 handler（T02-fs 批次，9 个工具）。
//!
//! 对应 `deliverables/software-company/tool_migration_matrix.json` 中
//! target_backend=rust_native、batch=T02-fs 的 9 个纯本地 SQL 工具：
//! build_graph / build_directory / file_read / file_grep / file_list /
//! file_symbol_content / file_remove / file_health / refresh_file。
//!
//! 安全边界（设计 Q4，daemon handler 强制，Python 无权限逻辑）：
//! 1. `workspace_instance_id` 显式注入且归属当前 peer（owned_workspace ACL）；
//! 2. 路径参数经 `validate_owned_path`（canonicalize + owner_uid），禁止 `..` 穿越；
//! 3. 文件读取仅限 workspace 根内（host_real_root 前缀校验）。
//!
//! 工程决策（记录于交付摘要 §设计歧义处理）：build_graph / build_directory 的
//! 符号级解析仍由既有 `workspace.file.refresh` 深管线承担；本 handler 提供
//! 文件级全量索引重建（扫描 + file_instances upsert + 内容 hash），保证
//! daemon 权威写路径可独立重建文件清单，且不引入 Python 双实现。

use serde_json::{json, Map, Value};
use std::path::{Path, PathBuf};

use super::dispatch::{
    get_int_param_or, get_str_param, require_str_param, DaemonRpcError, PeerCredential,
};
use super::workspace::{owned_workspace, validate_owned_path, WorkspaceRegistry};

/// 可索引的源文件扩展名（文件面扫描白名单，与 db 层解析面一致）。
const INDEXABLE_EXTS: &[&str] = &[
    "py", "rs", "c", "h", "cpp", "hpp", "cc", "go", "java", "js", "jsx", "ts", "tsx",
    "rb", "php", "scala", "cs", "kt", "swift", "ex", "exs", "hcl", "vue", "svelte",
];

/// 跳过目录（与 db/db_build.py 的 skip_dirs 保持一致的常见噪声目录）。
const SKIP_DIRS: &[&str] = &[
    ".git", "node_modules", "target", "dist", "build", ".next", "__pycache__",
];

/// 判断 rel_path 是否属于可索引源文件。
pub fn is_indexable_path(rel_path: &str) -> bool {
    let norm = rel_path.replace('\\', "/");
    if norm.starts_with('.') || norm.contains("/.") {
        return false;
    }
    // 任一路径段命中跳过目录 → 不索引（如 node_modules/x.js、dist/app.js）。
    for seg in norm.split('/') {
        if SKIP_DIRS.contains(&seg) {
            return false;
        }
    }
    let lower = norm.to_lowercase();
    let ext = lower.rsplit('.').next().unwrap_or("");
    INDEXABLE_EXTS.contains(&ext)
}

/// 秒级 Unix 时间戳（ISO 8601 UTC 秒级契约的数值表示）。
pub fn now_ts_secs() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

/// 计算文件 sha256 内容哈希（十六进制小写）。
pub fn sha256_hex(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    format!("{:x}", hasher.finalize())
}

/// 解析 workspace 行并返回 (workspace_id, host_real_root)。
fn resolve_workspace(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    workspace_instance_id: &str,
) -> Result<(i64, PathBuf), DaemonRpcError> {
    let workspace = owned_workspace(registry, peer.uid, workspace_instance_id)?;
    let workspace_id = workspace
        .get("workspace_id")
        .and_then(Value::as_i64)
        .ok_or_else(|| DaemonRpcError::internal_error("workspace_id 字段缺失或非数值".to_string()))?;
    let root = workspace
        .get("host_real_root")
        .and_then(Value::as_str)
        .or_else(|| workspace.get("client_view_root").and_then(Value::as_str))
        .map(PathBuf::from)
        .ok_or_else(|| DaemonRpcError::internal_error("workspace 缺少 host_real_root".to_string()))?;
    Ok((workspace_id, root))
}

/// 校验路径必须在 workspace 根内，返回规范化绝对路径（String，validate_owned_path 契约）。
fn resolve_owned_path(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    workspace_instance_id: &str,
    file_path: &str,
) -> Result<String, DaemonRpcError> {
    let (_, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let real = validate_owned_path(file_path, peer.uid, true)?;
    let real_root = std::fs::canonicalize(&root).unwrap_or(root);
    let real_root_str = real_root.to_string_lossy().to_string();
    let sep = std::path::MAIN_SEPARATOR.to_string();
    let ok = real == real_root_str || real.starts_with(&format!("{real_root_str}{sep}"));
    if !ok {
        return Err(DaemonRpcError::new(
            "path_escape",
            format!("路径不在 workspace 根内：{real}"),
        ));
    }
    Ok(real)
}

/// 打开 workspace codegraph DB（写路径，daemon 权威库）。
/// codegraph_db 为 None 时 fail-closed（daemon 未配置 codegraph 模板）。
fn open_codegraph_write(
    codegraph_db: Option<&Path>,
) -> Result<rusqlite::Connection, DaemonRpcError> {
    let path = codegraph_db.ok_or_else(|| {
        DaemonRpcError::new(
            "codegraph_db_unconfigured",
            "daemon 未配置 codegraph_db_path_template，无法执行写操作（fail-closed）",
        )
    })?;
    rusqlite::Connection::open(path).map_err(|e| {
        DaemonRpcError::internal_error(format!("打开 codegraph DB 失败: {e}"))
    })
}

/// 扫描目录（递归），返回可索引文件列表。
fn scan_files(root: &Path, recursive: bool) -> Vec<PathBuf> {
    let mut out = Vec::new();
    if let Ok(entries) = std::fs::read_dir(root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path.is_dir() {
                if recursive {
                    out.extend(scan_files(&path, true));
                }
            } else if let Some(rel) = path.strip_prefix(root).ok() {
                if is_indexable_path(&rel.to_string_lossy()) {
                    out.push(path);
                }
            }
        }
    }
    out
}

/// `workspace.build_graph` —— 全量重建文件索引（file_instances upsert）。
pub fn handle_build_graph(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let (workspace_id, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let conn = open_codegraph_write(codegraph_db)?;
    let scan_root: PathBuf = match get_str_param(params, "scan_root") {
        Some(s) if !s.is_empty() => PathBuf::from(validate_owned_path(s, peer.uid, true)?),
        _ => root.clone(),
    };
    let files = scan_files(&scan_root, true);
    let mut scanned = 0usize;
    let mut inserted = 0usize;
    let mut unchanged = 0usize;
    let now = now_ts_secs();
    for path in files {
        let rel = path
            .strip_prefix(&root)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"));
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let hash = sha256_hex(&bytes);
        let total_lines = bytes.iter().filter(|&&b| b == b'\n').count() as i64;
        let mtime = std::fs::metadata(&path)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64())
            .unwrap_or(now);
        scanned += 1;
        // file_contents upsert
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![hash, infer_language(&rel), total_lines, now],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("file_contents upsert: {e}")))?;
        // file_instances upsert（UNIQUE(workspace_id, rel_path)）
        let changed = conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, 0, 'parsed', ?7)
             ON CONFLICT(workspace_id, rel_path) DO UPDATE SET
               abs_path = excluded.abs_path,
               current_content_hash = excluded.current_content_hash,
               mtime = excluded.mtime,
               total_lines = excluded.total_lines,
               status = excluded.status,
               module_path = excluded.module_path",
            rusqlite::params![
                workspace_id,
                rel,
                path.to_string_lossy(),
                hash,
                mtime,
                total_lines,
                module_path_of(&rel)
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("file_instances upsert: {e}")))?;
        if changed == 0 {
            unchanged += 1;
        } else {
            inserted += 1;
        }
    }
    let mut m = Map::new();
    m.insert("ok".into(), Value::Bool(true));
    m.insert("scanned".into(), Value::Number(scanned.into()));
    m.insert("inserted".into(), Value::Number(inserted.into()));
    m.insert("unchanged".into(), Value::Number(unchanged.into()));
    Ok(Value::Object(m))
}

/// `workspace.build_directory` —— 重建指定目录文件索引。
pub fn handle_build_directory(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let dir_path = require_str_param(params, "dir_path")?;
    let (workspace_id, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let conn = open_codegraph_write(codegraph_db)?;
    let real_dir = PathBuf::from(validate_owned_path(dir_path, peer.uid, true)?);
    let recursive = params.get("recursive").and_then(Value::as_bool).unwrap_or(false);
    let files = scan_files(&real_dir, recursive);
    let scanned_total = files.len();
    let mut refreshed = 0usize;
    let now = now_ts_secs();
    for path in files {
        let rel = path
            .strip_prefix(&root)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"));
        let bytes = match std::fs::read(&path) {
            Ok(b) => b,
            Err(_) => continue,
        };
        let hash = sha256_hex(&bytes);
        let total_lines = bytes.iter().filter(|&&b| b == b'\n').count() as i64;
        let mtime = std::fs::metadata(&path)
            .and_then(|m| m.modified())
            .ok()
            .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
            .map(|d| d.as_secs_f64())
            .unwrap_or(now);
        conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
             VALUES (?1, ?2, ?3, ?4)",
            rusqlite::params![hash, infer_language(&rel), total_lines, now],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("file_contents upsert: {e}")))?;
        conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, 0, 'parsed', ?7)
             ON CONFLICT(workspace_id, rel_path) DO UPDATE SET
               abs_path = excluded.abs_path,
               current_content_hash = excluded.current_content_hash,
               mtime = excluded.mtime,
               total_lines = excluded.total_lines,
               status = excluded.status,
               module_path = excluded.module_path",
            rusqlite::params![
                workspace_id,
                rel,
                path.to_string_lossy(),
                hash,
                mtime,
                total_lines,
                module_path_of(&rel)
            ],
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("file_instances upsert: {e}")))?;
        refreshed += 1;
    }
    let mut m = Map::new();
    m.insert("ok".into(), Value::Bool(true));
    m.insert("scanned".into(), Value::Number(scanned_total.into()));
    m.insert("refreshed".into(), Value::Number(refreshed.into()));
    Ok(Value::Object(m))
}

/// `workspace.file.read` —— 读取文件内容（offset/limit 分页）。
pub fn handle_file_read(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let real = resolve_owned_path(registry, peer, workspace_instance_id, file_path)?;
    let content = std::fs::read_to_string(Path::new(&real)).map_err(|e| {
        DaemonRpcError::new("file_read_failed", format!("读取 {real} 失败: {e}"))
    })?;
    let lines: Vec<&str> = content.lines().collect();
    let offset = get_int_param_or(params, "offset", 0).max(0) as usize;
    let limit = get_int_param_or(params, "limit", 200).max(1) as usize;
    let start = offset.min(lines.len());
    let end = (start + limit).min(lines.len());
    let selected = lines[start..end].join("\n");
    let mut m = Map::new();
    m.insert("file_path".into(), Value::String(file_path.to_string()));
    m.insert("offset".into(), Value::Number(offset.into()));
    m.insert("limit".into(), Value::Number(limit.into()));
    m.insert("total_lines".into(), Value::Number(lines.len().into()));
    m.insert("content".into(), Value::String(selected));
    Ok(Value::Object(m))
}

/// `workspace.file.grep` —— 递归 grep（大小写不敏感子串/简单正则）。
pub fn handle_file_grep(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let pattern = require_str_param(params, "pattern")?;
    if pattern.is_empty() {
        return Err(DaemonRpcError::invalid_params("pattern 不能为空"));
    }
    let (_, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let base: PathBuf = match get_str_param(params, "path") {
        Some(p) if !p.is_empty() => {
            let real = PathBuf::from(validate_owned_path(p, peer.uid, true)?);
            let root_str = root.to_string_lossy().to_string();
            if !(real == root || real.starts_with(format!("{root_str}/"))) {
                return Err(DaemonRpcError::new(
                    "path_escape",
                    "path 不在 workspace 根内".to_string(),
                ));
            }
            real
        }
        _ => root.clone(),
    };
    let glob = get_str_param(params, "glob").unwrap_or("").to_string();
    let head_limit = get_int_param_or(params, "head_limit", 50).max(1) as usize;
    let files = scan_files(&base, true);
    let needle = pattern.to_lowercase();
    let mut matches: Vec<Value> = Vec::new();
    for path in files {
        if matches.len() >= head_limit {
            break;
        }
        let rel = path
            .strip_prefix(&root)
            .map(|p| p.to_string_lossy().replace('\\', "/"))
            .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"));
        if !glob.is_empty() && !glob_match(&rel, &glob) {
            continue;
        }
        let Ok(content) = std::fs::read_to_string(&path) else { continue };
        for (idx, line) in content.lines().enumerate() {
            if matches.len() >= head_limit {
                break;
            }
            if line.to_lowercase().contains(&needle) {
                matches.push(json!({
                    "file_path": rel,
                    "line": idx + 1,
                    "text": line,
                }));
            }
        }
    }
    Ok(json!({ "matches": matches, "count": matches.len() }))
}

/// `workspace.file.list` —— 列出目录内容。
pub fn handle_file_list(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let (_, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let base: PathBuf = match get_str_param(params, "path") {
        Some(p) if !p.is_empty() => {
            let real = PathBuf::from(validate_owned_path(p, peer.uid, true)?);
            let root_str = root.to_string_lossy().to_string();
            if !(real == root || real.starts_with(format!("{root_str}/"))) {
                return Err(DaemonRpcError::new("path_escape", "path 不在 workspace 根内".to_string()));
            }
            real
        }
        _ => root.clone(),
    };
    let glob = get_str_param(params, "glob").unwrap_or("").to_string();
    let mut rows: Vec<Value> = Vec::new();
    if let Ok(entries) = std::fs::read_dir(&base) {
        for entry in entries.flatten() {
            let path = entry.path();
            let rel = path
                .strip_prefix(&root)
                .map(|p| p.to_string_lossy().replace('\\', "/"))
                .unwrap_or_else(|_| path.to_string_lossy().replace('\\', "/"));
            if !glob.is_empty() && !glob_match(&rel, &glob) {
                continue;
            }
            let is_dir = path.is_dir();
            let size = std::fs::metadata(&path).map(|m| m.len()).unwrap_or(0);
            rows.push(json!({ "rel_path": rel, "size": size, "is_dir": is_dir }));
        }
    }
    rows.sort_by_key(|r| r["rel_path"].as_str().unwrap_or("").to_string());
    Ok(Value::Array(rows))
}

/// `workspace.file.symbol_content` —— 查询符号内容（symbol_contents 表）。
pub fn handle_file_symbol_content(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let symbol_name = require_str_param(params, "symbol_name")?;
    let conn = open_codegraph_write(codegraph_db)?;
    let mut stmt = conn
        .prepare(
            "SELECT s.qualified_name, sc.content, s.start_line, s.end_line
             FROM symbols s
             JOIN symbol_contents sc ON sc.content_hash = s.symbol_hash
             JOIN file_instances fi ON fi.id = s.file_instance_id
             WHERE fi.rel_path = ?1 AND (s.name = ?2 OR s.qualified_name = ?2)
             ORDER BY s.start_line ASC LIMIT 1",
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_content prepare: {e}")))?;
    let mut rows = stmt
        .query_map(
            rusqlite::params![file_path, symbol_name],
            |row| {
                Ok((
                    row.get::<_, String>(0)?,
                    row.get::<_, String>(1)?,
                    row.get::<_, i64>(2)?,
                    row.get::<_, i64>(3)?,
                ))
            },
        )
        .map_err(|e| DaemonRpcError::internal_error(format!("symbol_content query: {e}")))?;
    if let Some(row) = rows.next() {
        let (qualified_name, content, start_line, end_line) =
            row.map_err(|e| DaemonRpcError::internal_error(format!("symbol_content row: {e}")))?;
        return Ok(json!({
            "qualified_name": qualified_name,
            "content": content,
            "start_line": start_line,
            "end_line": end_line,
        }));
    }
    Ok(Value::Null)
}

/// `workspace.file.remove` —— 删除文件（写操作，记录 destructive_operations）。
pub fn handle_file_remove(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let (workspace_id, _root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let real = validate_owned_path(file_path, peer.uid, true)?;
    std::fs::remove_file(Path::new(&real)).map_err(|e| {
        DaemonRpcError::new("file_remove_failed", format!("删除 {real} 失败: {e}"))
    })?;
    if let Some(db) = codegraph_db {
        if let Ok(conn) = rusqlite::Connection::open(db) {
            let rel = real.replace('\\', "/");
            let _ = conn.execute(
                "DELETE FROM file_instances WHERE workspace_id = ?1 AND (rel_path = ?2 OR abs_path = ?3)",
                rusqlite::params![workspace_id, rel, real],
            );
            let _ = conn.execute(
                "INSERT INTO destructive_operations (workspace_id, operation_type, target_path, created_at)
                 VALUES (?1, 'file_remove', ?2, ?3)",
                rusqlite::params![workspace_id, rel, now_ts_secs()],
            );
        }
    }
    Ok(json!({ "ok": true, "removed": true }))
}

/// `workspace.file.refresh_file` —— 刷新单个文件（文件索引增量更新，MCP
/// `refresh_file(file_path)` 契约：单文件增量更新）。
pub fn handle_refresh_file(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
    codegraph_db: Option<&Path>,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let (workspace_id, root) = resolve_workspace(registry, peer, workspace_instance_id)?;
    let conn = open_codegraph_write(codegraph_db)?;
    let real = PathBuf::from(validate_owned_path(file_path, peer.uid, true)?);
    let rel = real
        .strip_prefix(&root)
        .map(|p| p.to_string_lossy().replace('\\', "/"))
        .unwrap_or_else(|_| file_path.replace('\\', "/"));
    let bytes = std::fs::read(&real).map_err(|e| {
        DaemonRpcError::new(
            "file_read_failed",
            format!("读取 {} 失败: {e}", real.to_string_lossy()),
        )
    })?;
    let hash = sha256_hex(&bytes);
    let total_lines = bytes.iter().filter(|&&b| b == b'\n').count() as i64;
    let mtime = std::fs::metadata(&real)
        .and_then(|m| m.modified())
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or_else(now_ts_secs);
    let now = now_ts_secs();
    conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at)
         VALUES (?1, ?2, ?3, ?4)",
        rusqlite::params![hash, infer_language(&rel), total_lines, now],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("file_contents upsert: {e}")))?;
    conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
         VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'parsed', ?8)
         ON CONFLICT(workspace_id, rel_path) DO UPDATE SET
           abs_path = excluded.abs_path,
           current_content_hash = excluded.current_content_hash,
           mtime = excluded.mtime,
           total_lines = excluded.total_lines,
           last_parsed = excluded.last_parsed,
           status = excluded.status,
           module_path = excluded.module_path",
        rusqlite::params![
            workspace_id,
            rel,
            real.to_string_lossy(),
            hash,
            mtime,
            total_lines,
            now,
            module_path_of(&rel)
        ],
    )
    .map_err(|e| DaemonRpcError::internal_error(format!("file_instances upsert: {e}")))?;
    Ok(json!({ "ok": true, "file_path": rel, "content_hash": hash, "total_lines": total_lines }))
}

/// `workspace.file.health` —— 文件健康检查（存在性/大小/mtime/可读）。
pub fn handle_file_health(
    registry: &WorkspaceRegistry,
    peer: &PeerCredential,
    params: &Value,
) -> Result<Value, DaemonRpcError> {
    let workspace_instance_id = require_str_param(params, "workspace_instance_id")?;
    let file_path = require_str_param(params, "file_path")?;
    let real = validate_owned_path(file_path, peer.uid, false)?;
    let real_path = Path::new(&real);
    let mut m = Map::new();
    m.insert("file_path".into(), Value::String(file_path.to_string()));
    if !real_path.exists() {
        m.insert("exists".into(), Value::Bool(false));
        m.insert("size".into(), Value::Number(0.into()));
        m.insert("mtime".into(), Value::Null);
        m.insert("readable".into(), Value::Bool(false));
        return Ok(Value::Object(m));
    }
    let meta = std::fs::metadata(real_path).map_err(|e| {
        DaemonRpcError::new("file_health_failed", format!("stat {real} 失败: {e}"))
    })?;
    let mtime = meta
        .modified()
        .ok()
        .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0);
    let readable = std::fs::File::open(real_path).is_ok();
    m.insert("exists".into(), Value::Bool(true));
    m.insert("size".into(), Value::Number(meta.len().into()));
    m.insert("mtime".into(), serde_json::Number::from_f64(mtime).map(Value::Number).unwrap_or(Value::Null));
    m.insert("readable".into(), Value::Bool(readable));
    Ok(Value::Object(m))
}

/// 根据扩展名推断语言（与 db 层 language 字段语义一致）。
fn infer_language(rel_path: &str) -> String {
    let lower = rel_path.to_lowercase();
    let ext = lower.rsplit('.').next().unwrap_or("");
    match ext {
        "py" => "python",
        "rs" => "rust",
        "c" | "h" => "c",
        "cpp" | "hpp" | "cc" | "cxx" => "cpp",
        "go" => "go",
        "java" => "java",
        "js" | "jsx" | "ts" | "tsx" => "typescript",
        "rb" => "ruby",
        "php" => "php",
        "scala" => "scala",
        "cs" => "csharp",
        "kt" => "kotlin",
        "swift" => "swift",
        "ex" | "exs" => "elixir",
        "hcl" => "hcl",
        _ => "unknown",
    }
    .to_string()
}

/// 从 rel_path 派生模块路径（目录路径，去掉扩展名）。
fn module_path_of(rel_path: &str) -> String {
    let norm = rel_path.replace('\\', "/");
    match norm.rfind('.') {
        Some(idx) => norm[..idx].to_string(),
        None => norm,
    }
}

/// 极简 glob 匹配（支持 `*` 与 `**`），用于 file_list/file_grep 的 glob 过滤。
fn glob_match(path: &str, glob: &str) -> bool {
    if glob.is_empty() {
        return true;
    }
    let pattern = glob.replace("**", "\u{0}");
    let pattern = pattern.replace('*', "[^/]*");
    let pattern = pattern.replace('\u{0}', ".*");
    let re = match regex::Regex::new(&format!("^{}$", pattern)) {
        Ok(re) => re,
        Err(_) => return path.contains(&glob),
    };
    re.is_match(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_is_indexable_path() {
        assert!(is_indexable_path("src/main.rs"));
        assert!(is_indexable_path("app.py"));
        assert!(!is_indexable_path(".git/config"));
        assert!(!is_indexable_path("node_modules/x.js"));
        assert!(!is_indexable_path("README.md"));
    }

    #[test]
    fn test_sha256_hex() {
        let h = sha256_hex(b"hello");
        assert_eq!(h.len(), 64);
        assert!(h.chars().all(|c| c.is_ascii_hexdigit()));
    }

    #[test]
    fn test_glob_match() {
        assert!(glob_match("src/a.rs", "src/*.rs"));
        assert!(glob_match("a/b/c.rs", "**/*.rs"));
        assert!(!glob_match("a/b/c.rs", "src/*.rs"));
        assert!(glob_match("", ""));
    }

    #[test]
    fn test_module_path_of() {
        assert_eq!(module_path_of("src/main.rs"), "src/main");
        assert_eq!(module_path_of("a/b.py"), "a/b");
    }
}
