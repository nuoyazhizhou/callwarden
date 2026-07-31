//! `cw status` 的本地状态查询与 enterprise 结果组合。
//!
//! local 模式对齐 Python `CodeGraphDB.get_status()`，包括文件系统扫描、
//! `.callwardenignore` 和 P21 第三方目录识别。enterprise 模式不能直接读取
//! 用户工作区，因此显式组合 registry 与 snapshot 图统计，不伪造磁盘新旧状态。

use std::collections::{BTreeMap, BTreeSet, HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::UNIX_EPOCH;

use rusqlite::{params, Connection};
use serde_json::{json, Map, Value};

use super::stats::query_local_stats;
use crate::daemon::replicator::detect_language_from_path;

const DEFAULT_IGNORE_PATTERNS: &[&str] = &[
    ".git/",
    "node_modules/",
    ".next/",
    "__pycache__/",
    ".venv/",
    "venv/",
    "env/",
    ".tox/",
    "*.egg-info/",
    "target/",
    "dist/",
    "build/",
    "out/",
    "output/",
    "outputs/",
    "obj/",
    "bin/",
    "rootfs/",
    "staging/",
    "sysroot/",
    "ccache/",
    "prebuilt/",
    "prebuilts/",
    "blob/",
    "toolchain/",
    "toolchains/",
    "ndk/",
    "jdk/",
    "thirdParty/",
    "third_party/",
    "vendor/",
    "autogen/",
    "auto_gen/",
    "generated/",
    "gen/",
    "generated_src/",
    "proto_gen/",
    "protobuf_gen/",
    "grpc_gen/",
    "moc/",
    "*.pb.cc",
    "*.pb.h",
    "*.pb.go",
    "*_pb2.py",
    "*_pb2.pyi",
    "*_pb2_grpc.py",
    "*.grpc.cc",
    "*.grpc.h",
    "moc_*.cpp",
    "ui_*.h",
    "qrc_*.cpp",
    "*.pyc",
    "*.pyo",
    ".repo/",
];

const KNOWN_THIRD_PARTY_DIRS: &[&str] = &[
    "node_modules",
    "vendor",
    "third_party",
    "thirdparty",
    "3rdparty",
    "bower_components",
    "jspm_packages",
    "web_modules",
    ".m2",
    ".gradle",
    "ivy",
    "deps",
    "deps_packages",
];

const SUSPICIOUS_THIRD_PARTY_DIRS: &[&str] = &[
    "static",
    "libs",
    "lib",
    "external",
    "externals",
    "assets",
    "resources",
    "vendor_src",
];

const THIRD_PARTY_SOURCE_EXTENSIONS: &[&str] = &[
    "c", "cpp", "cc", "cxx", "h", "hpp", "hxx", "java", "kt", "scala", "py", "rb", "php", "js",
    "jsx", "ts", "tsx", "go", "rs", "swift", "cs", "m", "mm", "ex", "exs", "hcl", "tf",
];

const LARGE_SOURCE_FILE_BYTES: u64 = 500 * 1024;
const LARGE_SOURCE_FILE_COUNT: usize = 5;

/// 查询 local 模式下与 Python `CodeGraphDB.get_status()` 等价的 JSON。
pub fn query_local_status(
    conn: &Connection,
    workspace_id: i64,
    db_path: &Path,
) -> Result<Value, String> {
    let (workspace_name, workspace_root): (String, String) = conn
        .query_row(
            "SELECT name, root_path FROM workspaces WHERE id = ?1",
            params![workspace_id],
            |row| Ok((row.get(0)?, row.get(1)?)),
        )
        .map_err(status_sql_error)?;
    let stats = query_local_stats(conn, workspace_id)?;
    let scanned_files = scan_supported_files(Path::new(&workspace_root));
    let current_set: BTreeSet<String> = scanned_files.into_iter().collect();

    let mut tracked = HashSet::new();
    let mut stale_files = Vec::new();
    let mut deleted_files = Vec::new();
    let mut last_build = 0.0_f64;
    let mut stmt = conn
        .prepare(
            "SELECT rel_path, mtime, last_parsed
             FROM file_instances
             WHERE workspace_id = ?1 AND status != 'archived'",
        )
        .map_err(status_sql_error)?;
    let rows = stmt
        .query_map(params![workspace_id], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, f64>(1)?,
                row.get::<_, f64>(2)?,
            ))
        })
        .map_err(status_sql_error)?;
    for row in rows {
        let (rel_path, mtime, last_parsed) = row.map_err(status_sql_error)?;
        if last_parsed > last_build {
            last_build = last_parsed;
        }
        tracked.insert(rel_path.clone());
        if !current_set.contains(&rel_path) {
            deleted_files.push(rel_path);
            continue;
        }
        let abs_path = Path::new(&workspace_root).join(path_from_rel(&rel_path));
        match file_mtime(&abs_path) {
            Some(disk_mtime) if (disk_mtime - mtime).abs() <= 0.001 => {}
            _ => stale_files.push(rel_path),
        }
    }

    let new_files: Vec<String> = current_set
        .iter()
        .filter(|rel_path| !tracked.contains(*rel_path))
        .cloned()
        .collect();
    let uncommented_fns: i64 = conn
        .query_row(
            "SELECT COUNT(*) FROM symbols s
             WHERE s.has_comment = 0 AND s.kind IN ('fn','test_fn','method')
               AND s.file_instance_id IN (
                   SELECT id FROM file_instances
                   WHERE workspace_id = ?1 AND status != 'archived'
               )",
            params![workspace_id],
            |row| row.get(0),
        )
        .map_err(status_sql_error)?;
    let by_language = query_language_distribution(conn, workspace_id)?;

    let total_calls = stat_i64(&stats, "total_calls");
    let resolved_calls = stat_i64(&stats, "resolved_calls");
    let resolve_rate = if total_calls > 0 {
        ((resolved_calls as f64 / total_calls as f64 * 1000.0).round_ties_even()) / 10.0
    } else {
        0.0
    };
    let db_size = fs::metadata(db_path).map(|meta| meta.len()).unwrap_or(0);

    Ok(json!({
        "workspace": {
            "name": workspace_name,
            "root": workspace_root,
            "db_size": db_size,
        },
        "files": {
            "tracked": stat_i64(&stats, "current_files"),
            "on_disk": current_set.len(),
            "new": new_files.len(),
            "stale": stale_files.len(),
            "deleted": deleted_files.len(),
            "new_files": first_ten(&new_files),
            "stale_files": first_ten(&stale_files),
            "deleted_files": first_ten(&deleted_files),
            "by_language": by_language,
        },
        "symbols": {
            "total": stat_i64(&stats, "total_symbols"),
            "by_kind": stats.get("by_kind").cloned().unwrap_or_else(|| json!({})),
            "uncommented_fns": uncommented_fns,
        },
        "calls": {
            "total": total_calls,
            "resolved": resolved_calls,
            "cross_file": stat_i64(&stats, "cross_file_calls"),
            "resolve_rate": resolve_rate,
        },
        "depth": stats
            .get("depth_distribution")
            .cloned()
            .unwrap_or_else(|| json!({})),
        "last_build": last_build,
        "needs_rebuild": !new_files.is_empty() || !stale_files.is_empty(),
    }))
}

/// 组合 enterprise status。daemon 不可见用户文件系统，因此显式声明缺失能力。
pub fn combine_enterprise_status(workspace_registry: Value, graph_stats: Value) -> Value {
    json!({
        "source": "enterprise",
        "workspace_registry": workspace_registry,
        "graph_stats": graph_stats,
        "filesystem_freshness": {
            "available": false,
            "reason": "enterprise daemon does not scan the user workspace; use watcher generation and health metrics",
        },
    })
}

/// 扫描 workspace 中受支持且未被 ignore/P21 排除的源文件。
///
/// status 与 refresh --all 共用这一实现，避免状态与构建使用不同文件集合。
pub fn scan_supported_files(workspace_root: &Path) -> Vec<String> {
    let patterns = load_ignore_patterns(workspace_root);
    let mut files = Vec::new();
    scan_directory(workspace_root, workspace_root, &patterns, &mut files);
    files.sort();
    files
}

fn scan_directory(root: &Path, current: &Path, patterns: &[String], files: &mut Vec<String>) {
    let Ok(entries) = fs::read_dir(current) else {
        return;
    };
    let mut entries: Vec<_> = entries.filter_map(Result::ok).collect();
    entries.sort_by_key(|entry| entry.file_name());

    for entry in entries {
        let path = entry.path();
        let Ok(file_type) = entry.file_type() else {
            continue;
        };
        let Some(rel_path) = relative_path(root, &path) else {
            continue;
        };
        if file_type.is_dir() {
            let name = entry.file_name();
            let name = name.to_string_lossy();
            if name.starts_with('.') && name != ".codegraph" {
                continue;
            }
            if should_ignore(&rel_path, true, patterns) || detect_third_party_dir(&path, &rel_path)
            {
                continue;
            }
            scan_directory(root, &path, patterns, files);
            continue;
        }

        // os.walk 会把指向文件的符号链接放入 filenames；保持相同行为。
        if (file_type.is_file() || path.is_file())
            && !detect_language_from_path(&rel_path).is_empty()
            && !should_ignore(&rel_path, false, patterns)
        {
            files.push(rel_path);
        }
    }
}

fn load_ignore_patterns(workspace_root: &Path) -> Vec<String> {
    let mut patterns: Vec<String> = DEFAULT_IGNORE_PATTERNS
        .iter()
        .map(|pattern| (*pattern).to_string())
        .collect();
    let ignore_path = workspace_root.join(".callwardenignore");
    if let Ok(text) = fs::read_to_string(ignore_path) {
        patterns.extend(text.lines().filter_map(|line| {
            let line = line.trim();
            (!line.is_empty() && !line.starts_with('#')).then(|| line.to_string())
        }));
    }
    patterns
}

fn should_ignore(rel_path: &str, is_dir: bool, patterns: &[String]) -> bool {
    let path_parts: Vec<&str> = rel_path.split('/').collect();
    for pattern in patterns {
        let match_dir_only = pattern.ends_with('/');
        let candidate_pattern = pattern.trim_end_matches('/');
        if let Some(root_pattern) = candidate_pattern.strip_prefix('/') {
            if fnmatch(root_pattern, rel_path)
                || (is_dir && fnmatch(&format!("{root_pattern}/"), &format!("{rel_path}/")))
            {
                return true;
            }
            continue;
        }
        if candidate_pattern.contains('/') {
            if fnmatch(candidate_pattern, rel_path) {
                return true;
            }
            continue;
        }
        if match_dir_only {
            if is_dir && path_parts.last().copied() == Some(candidate_pattern) {
                return true;
            }
            continue;
        }
        if path_parts
            .last()
            .is_some_and(|name| fnmatch(candidate_pattern, name))
        {
            return true;
        }
        for index in 0..path_parts.len() {
            if fnmatch(candidate_pattern, &path_parts[index..].join("/")) {
                return true;
            }
        }
    }
    false
}

fn fnmatch(pattern: &str, value: &str) -> bool {
    #[cfg(windows)]
    let (pattern, value) = (pattern.to_lowercase(), value.to_lowercase());
    #[cfg(not(windows))]
    let (pattern, value) = (pattern.to_string(), value.to_string());
    let pattern: Vec<char> = pattern.chars().collect();
    let value: Vec<char> = value.chars().collect();
    let mut memo = HashMap::new();
    wildcard_match(&pattern, &value, 0, 0, &mut memo)
}

fn wildcard_match(
    pattern: &[char],
    value: &[char],
    pattern_index: usize,
    value_index: usize,
    memo: &mut HashMap<(usize, usize), bool>,
) -> bool {
    if let Some(result) = memo.get(&(pattern_index, value_index)) {
        return *result;
    }
    let result = if pattern_index == pattern.len() {
        value_index == value.len()
    } else {
        match pattern[pattern_index] {
            '*' => {
                let mut next = pattern_index + 1;
                while next < pattern.len() && pattern[next] == '*' {
                    next += 1;
                }
                wildcard_match(pattern, value, next, value_index, memo)
                    || (value_index < value.len()
                        && wildcard_match(pattern, value, pattern_index, value_index + 1, memo))
            }
            '?' => {
                value_index < value.len()
                    && wildcard_match(pattern, value, pattern_index + 1, value_index + 1, memo)
            }
            '[' => match match_character_class(pattern, pattern_index, value.get(value_index)) {
                Some((matches, next_pattern)) => {
                    matches && wildcard_match(pattern, value, next_pattern, value_index + 1, memo)
                }
                None => {
                    value.get(value_index) == Some(&'[')
                        && wildcard_match(pattern, value, pattern_index + 1, value_index + 1, memo)
                }
            },
            literal => {
                value.get(value_index) == Some(&literal)
                    && wildcard_match(pattern, value, pattern_index + 1, value_index + 1, memo)
            }
        }
    };
    memo.insert((pattern_index, value_index), result);
    result
}

fn match_character_class(
    pattern: &[char],
    open_index: usize,
    value: Option<&char>,
) -> Option<(bool, usize)> {
    let value = *value?;
    let mut close_index = open_index + 1;
    if pattern.get(close_index) == Some(&']') {
        close_index += 1;
    }
    while close_index < pattern.len() && pattern[close_index] != ']' {
        close_index += 1;
    }
    if close_index >= pattern.len() {
        return None;
    }
    let mut index = open_index + 1;
    let negated = matches!(pattern.get(index), Some('!') | Some('^'));
    if negated {
        index += 1;
    }
    let mut matched = false;
    while index < close_index {
        if index + 2 < close_index && pattern[index + 1] == '-' {
            matched |= pattern[index] <= value && value <= pattern[index + 2];
            index += 3;
        } else {
            matched |= pattern[index] == value;
            index += 1;
        }
    }
    Some((if negated { !matched } else { matched }, close_index + 1))
}

fn detect_third_party_dir(abs_dir: &Path, rel_dir: &str) -> bool {
    let dir_name = abs_dir
        .file_name()
        .map(|name| name.to_string_lossy().to_lowercase())
        .unwrap_or_default();
    if KNOWN_THIRD_PARTY_DIRS.contains(&dir_name.as_str()) {
        return true;
    }
    if rel_dir.matches('/').count() > 2 {
        return false;
    }

    let Ok(entries) = fs::read_dir(abs_dir) else {
        return false;
    };
    let mut large_files = 0_usize;
    let mut has_minified = false;
    for entry in entries.filter_map(Result::ok) {
        let file_name = entry.file_name();
        let file_name = file_name.to_string_lossy();
        if file_name.starts_with('.') {
            continue;
        }
        if file_name.to_lowercase().contains(".min.") {
            has_minified = true;
            continue;
        }
        let extension = Path::new(file_name.as_ref())
            .extension()
            .map(|ext| ext.to_string_lossy().to_lowercase())
            .unwrap_or_default();
        if !THIRD_PARTY_SOURCE_EXTENSIONS.contains(&extension.as_str()) {
            continue;
        }
        if entry
            .metadata()
            .map(|meta| meta.is_file() && meta.len() > LARGE_SOURCE_FILE_BYTES)
            .unwrap_or(false)
        {
            large_files += 1;
        }
    }
    large_files >= LARGE_SOURCE_FILE_COUNT
        || has_minified
        || (SUSPICIOUS_THIRD_PARTY_DIRS.contains(&dir_name.as_str()) && large_files >= 1)
}

fn query_language_distribution(
    conn: &Connection,
    workspace_id: i64,
) -> Result<Map<String, Value>, String> {
    let mut stmt = conn
        .prepare(
            "SELECT rel_path
             FROM file_instances
             WHERE workspace_id = ?1 AND rel_path LIKE '%.%'",
        )
        .map_err(status_sql_error)?;
    let rows = stmt
        .query_map(params![workspace_id], |row| row.get::<_, String>(0))
        .map_err(status_sql_error)?;
    let mut counts = BTreeMap::new();
    for row in rows {
        let rel_path = row.map_err(status_sql_error)?;
        let extension = rel_path.rsplit('.').next().unwrap_or_default().to_string();
        *counts.entry(extension).or_insert(0_i64) += 1;
    }
    let mut result = Map::new();
    for (extension, count) in counts {
        result.insert(extension, Value::from(count));
    }
    Ok(result)
}

fn relative_path(root: &Path, path: &Path) -> Option<String> {
    path.strip_prefix(root)
        .ok()
        .map(|path| path.to_string_lossy().replace('\\', "/"))
}

fn path_from_rel(rel_path: &str) -> PathBuf {
    rel_path.split('/').collect()
}

fn file_mtime(path: &Path) -> Option<f64> {
    let modified = fs::metadata(path).ok()?.modified().ok()?;
    modified
        .duration_since(UNIX_EPOCH)
        .ok()
        .map(|duration| duration.as_secs_f64())
}

fn stat_i64(stats: &Value, key: &str) -> i64 {
    stats.get(key).and_then(Value::as_i64).unwrap_or(0)
}

fn first_ten(values: &[String]) -> Vec<&str> {
    values.iter().take(10).map(String::as_str).collect()
}

fn status_sql_error(error: rusqlite::Error) -> String {
    format!("status query failed: {error}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fnmatch_supports_python_style_wildcards() {
        assert!(fnmatch("*.pb.cc", "sample.pb.cc"));
        assert!(fnmatch("src/**/generated?.py", "src/a/b/generated1.py"));
        assert!(fnmatch("file[0-9].rs", "file7.rs"));
        assert!(!fnmatch("file[!0-9].rs", "file7.rs"));
        assert!(fnmatch("file[!0-9].rs", "filex.rs"));
    }

    #[test]
    fn scanner_applies_default_user_and_p21_ignores() {
        let temp = tempfile::tempdir().unwrap();
        fs::create_dir_all(temp.path().join("src")).unwrap();
        fs::create_dir_all(temp.path().join("target")).unwrap();
        fs::create_dir_all(temp.path().join("custom")).unwrap();
        fs::create_dir_all(temp.path().join("assets")).unwrap();
        fs::write(temp.path().join("src").join("main.rs"), "fn main() {}").unwrap();
        fs::write(temp.path().join("target").join("generated.rs"), "").unwrap();
        fs::write(temp.path().join("custom").join("skip.py"), "").unwrap();
        fs::write(temp.path().join("assets").join("bundle.min.js"), "").unwrap();
        fs::write(temp.path().join(".callwardenignore"), "custom/\n").unwrap();

        assert_eq!(scan_supported_files(temp.path()), vec!["src/main.rs"]);
    }

    #[test]
    fn enterprise_payload_does_not_claim_filesystem_freshness() {
        let value = combine_enterprise_status(
            json!({"workspace_instance_id": "ws-1"}),
            json!({"symbols": 3}),
        );
        assert_eq!(value["source"], "enterprise");
        assert_eq!(value["filesystem_freshness"]["available"], false);
        assert_eq!(value["workspace_registry"]["workspace_instance_id"], "ws-1");
    }
}
