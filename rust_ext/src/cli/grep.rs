//! `cw grep` 的本地文本搜索、符号归属和兼容输出。

use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use regex::Regex;
use rusqlite::{params, Connection};
use serde_json::Value;

const RG_TIMEOUT: Duration = Duration::from_secs(30);
const SOURCE_EXTENSIONS: &[&str] = &[
    "py", "rs", "ts", "js", "go", "java", "c", "h", "cpp", "hpp", "cs", "rb", "php", "kt", "swift",
    "scala",
];
const IGNORED_DIRECTORIES: &[&str] = &[
    ".git",
    "__pycache__",
    "node_modules",
    "target",
    ".venv",
    "venv",
    "dist",
    "build",
];

/// `cw grep` 的本地查询参数。
#[derive(Debug, Clone)]
pub struct GrepOptions {
    pub patterns: Vec<String>,
    pub fixed: bool,
    pub limit: usize,
    pub path: Option<PathBuf>,
    pub include_all: bool,
    pub kind: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct TextMatch {
    file_path: String,
    line: i64,
    content: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct SymbolContext {
    qualified_name: Option<String>,
    name: String,
    kind: String,
    start_line: i64,
    end_line: i64,
}

enum RgOutcome {
    Lines(Vec<String>),
    Unavailable,
    UserMessage(String),
}

/// 执行本地 grep，并返回与 Python CLI 一致的最终文本。
pub fn query_local_grep(
    conn: &Connection,
    workspace_id: i64,
    options: &GrepOptions,
) -> Result<Value, String> {
    if options.patterns.is_empty() {
        return Err("grep requires at least one pattern".to_string());
    }

    let workspace_root = query_workspace_root(conn, workspace_id)?;
    let search_root = resolve_search_root(&workspace_root, options.path.as_deref())?;
    let primary_pattern = &options.patterns[0];

    let raw_lines = match run_rg(primary_pattern, options.fixed, &search_root)? {
        RgOutcome::Lines(lines) => lines,
        RgOutcome::Unavailable => {
            match fallback_search(primary_pattern, options.fixed, &search_root) {
                Ok(lines) => lines,
                Err(error) if error.starts_with("regex error:") => {
                    return Ok(Value::String(format!("Error: {error}")));
                }
                Err(error) => return Err(error),
            }
        }
        RgOutcome::UserMessage(message) => return Ok(Value::String(message)),
    };

    if raw_lines.is_empty() {
        return Ok(Value::String(format!("No matches for: {primary_pattern}")));
    }

    let mut matches = parse_raw_lines(&raw_lines)?;
    if options.patterns.len() > 1 {
        matches = match filter_and_patterns(matches, &options.patterns[1..], options.fixed) {
            Ok(matches) => matches,
            Err(error) if error.starts_with("regex error in pattern:") => {
                return Ok(Value::String(format!("Error: {error}")));
            }
            Err(error) => return Err(error),
        };
        if matches.is_empty() {
            return Ok(Value::String(format!(
                "No matches for AND: {}",
                options.patterns.join(" ")
            )));
        }
    }

    let contexts = query_symbol_contexts(
        conn,
        workspace_id,
        &workspace_root,
        matches.iter().map(|item| item.file_path.as_str()),
    )?;
    Ok(Value::String(format_matches(matches, contexts, options)))
}

fn query_workspace_root(conn: &Connection, workspace_id: i64) -> Result<PathBuf, String> {
    let root: String = conn
        .query_row(
            "SELECT root_path FROM workspaces WHERE id = ?1",
            params![workspace_id],
            |row| row.get(0),
        )
        .map_err(|error| format!("cannot query workspace root: {error}"))?;
    let path = PathBuf::from(root);
    if !path.is_dir() {
        return Err(format!(
            "workspace root is not a directory: {}",
            path.display()
        ));
    }
    Ok(path)
}

fn resolve_search_root(
    workspace_root: &Path,
    requested_path: Option<&Path>,
) -> Result<PathBuf, String> {
    let candidate = match requested_path {
        Some(path) if path.is_dir() => path.to_path_buf(),
        Some(path) if path.is_file() => path
            .parent()
            .map(Path::to_path_buf)
            .unwrap_or_else(|| workspace_root.to_path_buf()),
        _ => workspace_root.to_path_buf(),
    };
    let resolved_workspace = fs::canonicalize(workspace_root).map_err(|error| {
        format!(
            "cannot resolve workspace root {}: {error}",
            workspace_root.display()
        )
    })?;
    let resolved_candidate = fs::canonicalize(&candidate).map_err(|error| {
        format!(
            "cannot resolve grep search path {}: {error}",
            candidate.display()
        )
    })?;
    if !resolved_candidate.starts_with(&resolved_workspace) {
        return Err(format!(
            "grep search path escapes workspace root: {}",
            resolved_candidate.display()
        ));
    }
    Ok(candidate)
}

fn run_rg(pattern: &str, fixed: bool, search_root: &Path) -> Result<RgOutcome, String> {
    let mut command = Command::new("rg");
    command
        .arg("-n")
        .arg("--no-heading")
        .arg("--color")
        .arg("never");
    if fixed {
        command.arg("-F");
    }
    command
        .arg(pattern)
        .arg(search_root)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error)
            if matches!(
                error.kind(),
                io::ErrorKind::NotFound | io::ErrorKind::PermissionDenied
            ) =>
        {
            // rg 是可选加速器；在 WSL 挂载 Windows 工作区等场景中，文件可能存在但不可执行。
            // 这类外部工具不可用必须走受限源码回退，不能把正常查询变成 internal_error。
            return Ok(RgOutcome::Unavailable);
        }
        Err(error) => return Err(format!("cannot start rg: {error}")),
    };
    let stdout = child
        .stdout
        .take()
        .ok_or_else(|| "cannot capture rg stdout".to_string())?;
    let stderr = child
        .stderr
        .take()
        .ok_or_else(|| "cannot capture rg stderr".to_string())?;
    let stdout_reader = thread::spawn(move || read_stream(stdout));
    let stderr_reader = thread::spawn(move || read_stream(stderr));

    let started = Instant::now();
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if started.elapsed() < RG_TIMEOUT => {
                thread::sleep(Duration::from_millis(10));
            }
            Ok(None) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = join_reader(stdout_reader, "stdout");
                let _ = join_reader(stderr_reader, "stderr");
                return Ok(RgOutcome::UserMessage(
                    "Error: grep timeout after 30s".to_string(),
                ));
            }
            Err(error) => {
                let _ = child.kill();
                let _ = child.wait();
                let _ = join_reader(stdout_reader, "stdout");
                let _ = join_reader(stderr_reader, "stderr");
                return Err(format!("cannot wait for rg: {error}"));
            }
        }
    };

    let stdout = String::from_utf8_lossy(&join_reader(stdout_reader, "stdout")?).into_owned();
    let stderr = String::from_utf8_lossy(&join_reader(stderr_reader, "stderr")?).into_owned();
    match status.code() {
        Some(0) => Ok(RgOutcome::Lines(
            stdout.lines().map(ToOwned::to_owned).collect(),
        )),
        Some(1) => Ok(RgOutcome::Lines(Vec::new())),
        Some(2) => Ok(RgOutcome::UserMessage(format!("Error: {}", stderr.trim()))),
        Some(code) => Err(format!("rg exited with status {code}: {}", stderr.trim())),
        None => Err("rg terminated without an exit code".to_string()),
    }
}

fn read_stream(mut stream: impl Read) -> io::Result<Vec<u8>> {
    let mut output = Vec::new();
    stream.read_to_end(&mut output)?;
    Ok(output)
}

fn join_reader(
    reader: thread::JoinHandle<io::Result<Vec<u8>>>,
    stream_name: &str,
) -> Result<Vec<u8>, String> {
    reader
        .join()
        .map_err(|_| format!("rg {stream_name} reader panicked"))?
        .map_err(|error| format!("cannot read rg {stream_name}: {error}"))
}

fn fallback_search(pattern: &str, fixed: bool, search_root: &Path) -> Result<Vec<String>, String> {
    let regex = if fixed {
        None
    } else {
        Some(
            Regex::new(pattern)
                .map_err(|error| format!("regex error: {}", python_regex_error(&error)))?,
        )
    };
    let mut output = Vec::new();
    visit_source_files(search_root, &mut |path| {
        let bytes = match fs::read(path) {
            Ok(bytes) => bytes,
            Err(_) => return,
        };
        let text = String::from_utf8_lossy(&bytes);
        for (index, line) in text.lines().enumerate() {
            let matched = if fixed {
                line.contains(pattern)
            } else {
                regex.as_ref().is_some_and(|regex| regex.is_match(line))
            };
            if matched {
                output.push(format!(
                    "{}:{}:{}",
                    path.display(),
                    index + 1,
                    line.trim_end()
                ));
            }
        }
    })?;
    Ok(output)
}

fn visit_source_files(directory: &Path, visitor: &mut impl FnMut(&Path)) -> Result<(), String> {
    let entries = fs::read_dir(directory).map_err(|error| {
        format!(
            "cannot read grep directory {}: {error}",
            directory.display()
        )
    })?;
    for entry in entries {
        let entry = match entry {
            Ok(entry) => entry,
            Err(_) => continue,
        };
        let path = entry.path();
        let metadata = match fs::symlink_metadata(&path) {
            Ok(metadata) => metadata,
            Err(_) => continue,
        };
        if metadata.file_type().is_symlink() {
            continue;
        }
        if metadata.is_dir() {
            let ignored = path
                .file_name()
                .and_then(|name| name.to_str())
                .is_some_and(|name| IGNORED_DIRECTORIES.contains(&name));
            if !ignored {
                visit_source_files(&path, visitor)?;
            }
            continue;
        }
        if metadata.is_file() && is_source_file(&path) {
            visitor(&path);
        }
    }
    Ok(())
}

fn is_source_file(path: &Path) -> bool {
    path.extension()
        .and_then(|extension| extension.to_str())
        .is_some_and(|extension| SOURCE_EXTENSIONS.contains(&extension))
}

fn parse_raw_lines(raw_lines: &[String]) -> Result<Vec<TextMatch>, String> {
    let line_pattern =
        Regex::new(r"^(.+?):(\d+):(.*)$").map_err(|error| format!("regex error: {error}"))?;
    let mut matches = Vec::new();
    for raw in raw_lines {
        let Some(captures) = line_pattern.captures(raw) else {
            continue;
        };
        let line = captures[2]
            .parse::<i64>()
            .map_err(|error| format!("invalid grep line number {:?}: {error}", &captures[2]))?;
        matches.push(TextMatch {
            file_path: captures[1].to_string(),
            line,
            content: captures[3].to_string(),
        });
    }
    Ok(matches)
}

fn filter_and_patterns(
    matches: Vec<TextMatch>,
    remaining_patterns: &[String],
    fixed: bool,
) -> Result<Vec<TextMatch>, String> {
    if fixed {
        return Ok(matches
            .into_iter()
            .filter(|item| {
                remaining_patterns
                    .iter()
                    .all(|pattern| item.content.contains(pattern))
            })
            .collect());
    }
    let regexes = remaining_patterns
        .iter()
        .map(|pattern| {
            Regex::new(pattern)
                .map_err(|error| format!("regex error in pattern: {}", python_regex_error(&error)))
        })
        .collect::<Result<Vec<_>, _>>()?;
    Ok(matches
        .into_iter()
        .filter(|item| regexes.iter().all(|regex| regex.is_match(&item.content)))
        .collect())
}

fn query_symbol_contexts<'a>(
    conn: &Connection,
    workspace_id: i64,
    workspace_root: &Path,
    file_paths: impl Iterator<Item = &'a str>,
) -> Result<HashMap<String, Vec<SymbolContext>>, String> {
    let mut contexts = HashMap::new();
    let unique_paths = file_paths
        .map(ToOwned::to_owned)
        .collect::<HashSet<String>>();
    for file_path in unique_paths {
        let rel_path = normalize_match_path(workspace_root, &file_path);
        let mut stmt = conn
            .prepare(
                "
                SELECT s.qualified_name, s.name, s.kind, s.start_line, s.end_line
                FROM symbols s
                JOIN file_instances fi ON s.file_instance_id = fi.id
                WHERE fi.workspace_id = ?1
                  AND fi.rel_path = ?2
                  AND fi.status != 'archived'
                  AND s.start_line > 0
                  AND s.end_line > 0
                ORDER BY (s.end_line - s.start_line) ASC
                ",
            )
            .map_err(|error| format!("cannot prepare grep symbol query: {error}"))?;
        let rows = stmt
            .query_map(params![workspace_id, rel_path], |row| {
                Ok(SymbolContext {
                    qualified_name: row.get(0)?,
                    name: row.get(1)?,
                    kind: row.get(2)?,
                    start_line: row.get(3)?,
                    end_line: row.get(4)?,
                })
            })
            .map_err(|error| format!("cannot query grep symbol contexts: {error}"))?;
        let mut symbols = Vec::new();
        for row in rows {
            symbols.push(row.map_err(|error| format!("cannot read grep symbol context: {error}"))?);
        }
        contexts.insert(file_path, symbols);
    }
    Ok(contexts)
}

fn normalize_match_path(workspace_root: &Path, file_path: &str) -> String {
    let path = Path::new(file_path);
    let relative = if path.is_absolute() {
        path.strip_prefix(workspace_root).unwrap_or(path)
    } else {
        path
    };
    relative.to_string_lossy().replace('\\', "/")
}

fn format_matches(
    matches: Vec<TextMatch>,
    contexts: HashMap<String, Vec<SymbolContext>>,
    options: &GrepOptions,
) -> String {
    let mut enriched = matches
        .into_iter()
        .map(|item| {
            let context = contexts
                .get(&item.file_path)
                .and_then(|symbols| find_innermost_symbol(symbols, item.line))
                .cloned();
            (item, context)
        })
        .collect::<Vec<_>>();

    let no_symbol_filtered = if options.include_all {
        0
    } else {
        let before = enriched.len();
        enriched.retain(|(_, context)| context.is_some());
        before - enriched.len()
    };
    if let Some(kind) = options.kind.as_deref() {
        enriched
            .retain(|(_, context)| context.as_ref().is_some_and(|context| context.kind == kind));
    }
    let total_before_limit = enriched.len();
    enriched.truncate(options.limit);

    let pattern_display = options.patterns.join(" ");
    let filter_note = if !options.include_all && no_symbol_filtered > 0 {
        format!(", filtered {no_symbol_filtered} no-symbol")
    } else {
        String::new()
    };
    let kind_note = options
        .kind
        .as_deref()
        .map(|kind| format!(" [kind={kind}]"))
        .unwrap_or_default();
    let mut lines = vec![format!(
        "Grep with symbol context: pattern={}, {} matches (of {} after filter{}){}",
        python_string_repr(&pattern_display),
        enriched.len(),
        total_before_limit,
        filter_note,
        kind_note
    )];
    let displayed_count = enriched.len();
    lines.push(String::new());
    for (item, context) in enriched {
        match context {
            Some(context) => {
                let qualified_name = context
                    .qualified_name
                    .as_deref()
                    .filter(|name| !name.is_empty())
                    .unwrap_or(&context.name);
                lines.push(format!(
                    "{}:{} [in {} {}] {}",
                    item.file_path, item.line, context.kind, qualified_name, item.content
                ));
            }
            None => lines.push(format!(
                "{}:{} [no symbol] {}",
                item.file_path, item.line, item.content
            )),
        }
    }
    lines.push(String::new());
    lines.push(format!(
        "Total: {} matches (of {} after filter)",
        displayed_count, total_before_limit
    ));
    lines.join("\n")
}

fn find_innermost_symbol(symbols: &[SymbolContext], line: i64) -> Option<&SymbolContext> {
    symbols
        .iter()
        .find(|symbol| symbol.start_line <= line && line <= symbol.end_line)
}

fn python_regex_error(error: &regex::Error) -> String {
    error.to_string()
}

fn python_string_repr(value: &str) -> String {
    let quote = if value.contains('\'') && !value.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut output = String::with_capacity(value.len() + 2);
    output.push(quote);
    for character in value.chars() {
        match character {
            '\\' => output.push_str(r"\\"),
            '\n' => output.push_str(r"\n"),
            '\r' => output.push_str(r"\r"),
            '\t' => output.push_str(r"\t"),
            character if character == quote => {
                output.push('\\');
                output.push(character);
            }
            character => output.push(character),
        }
    }
    output.push(quote);
    output
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fixed_and_regex_filters_require_every_pattern() {
        let matches = vec![
            TextMatch {
                file_path: "a.py".to_string(),
                line: 1,
                content: "import time".to_string(),
            },
            TextMatch {
                file_path: "a.py".to_string(),
                line: 2,
                content: "import json".to_string(),
            },
        ];
        assert_eq!(
            filter_and_patterns(matches.clone(), &["time".to_string()], true)
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            filter_and_patterns(matches, &[r"t.me".to_string()], false)
                .unwrap()
                .len(),
            1
        );
    }

    #[test]
    fn pattern_header_uses_python_repr_quotes() {
        assert_eq!(python_string_repr("needle"), "'needle'");
        assert_eq!(python_string_repr("can't"), "\"can't\"");
        assert_eq!(python_string_repr("a\\b"), "'a\\\\b'");
    }

    #[test]
    fn fallback_scans_source_files_and_skips_ignored_directories() {
        let temp = tempfile::tempdir().unwrap();
        fs::write(temp.path().join("a.py"), "needle\n").unwrap();
        fs::write(temp.path().join("notes.txt"), "needle\n").unwrap();
        fs::create_dir(temp.path().join("target")).unwrap();
        fs::write(temp.path().join("target").join("ignored.py"), "needle\n").unwrap();

        let lines = fallback_search("needle", true, temp.path()).unwrap();
        assert_eq!(lines.len(), 1);
        assert!(lines[0].ends_with("a.py:1:needle"));
    }

    #[test]
    fn path_scope_rejects_workspace_escape() {
        let workspace = tempfile::tempdir().unwrap();
        let outside = tempfile::tempdir().unwrap();
        let error = resolve_search_root(workspace.path(), Some(outside.path())).unwrap_err();
        assert!(error.contains("escapes workspace root"));
    }

    #[test]
    fn limit_is_applied_after_symbol_and_kind_filters() {
        let matches = vec![
            TextMatch {
                file_path: "a.py".to_string(),
                line: 1,
                content: "needle one".to_string(),
            },
            TextMatch {
                file_path: "a.py".to_string(),
                line: 5,
                content: "needle two".to_string(),
            },
            TextMatch {
                file_path: "a.py".to_string(),
                line: 9,
                content: "needle top".to_string(),
            },
        ];
        let contexts = HashMap::from([(
            "a.py".to_string(),
            vec![
                SymbolContext {
                    qualified_name: Some("a.alpha".to_string()),
                    name: "alpha".to_string(),
                    kind: "fn".to_string(),
                    start_line: 1,
                    end_line: 2,
                },
                SymbolContext {
                    qualified_name: Some("a.Thing".to_string()),
                    name: "Thing".to_string(),
                    kind: "class".to_string(),
                    start_line: 4,
                    end_line: 6,
                },
            ],
        )]);
        let output = format_matches(
            matches,
            contexts,
            &GrepOptions {
                patterns: vec!["needle".to_string()],
                fixed: true,
                limit: 1,
                path: None,
                include_all: false,
                kind: Some("fn".to_string()),
            },
        );
        assert!(output.contains("1 matches (of 1 after filter, filtered 1 no-symbol) [kind=fn]"));
        assert!(output.contains("[in fn a.alpha] needle one"));
        assert!(!output.contains("needle two"));
    }
}
