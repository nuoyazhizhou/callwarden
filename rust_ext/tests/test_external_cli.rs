use std::fs;
use std::time::Duration;
use tempfile::tempdir;

use callwarden_core::cli::external::{
    run_coverage_fn, run_coverage_import, run_doctor, run_gc_archive, run_gc_db_cleanup,
    run_gc_purge, run_gc_status, run_install_agent, run_install_hook, run_semgrep_list,
    run_semgrep_stats,
};
use callwarden_core::cli::router::DaemonMode;
use callwarden_core::cli::runtime::RuntimeOptions;

fn make_test_runtime(dir: &std::path::Path) -> RuntimeOptions {
    let db_path = dir.join("callwarden.db");
    let conn = rusqlite::Connection::open(&db_path).unwrap();
    conn.execute_batch(
        "CREATE TABLE workspaces (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            root_path TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1
        );
        INSERT INTO workspaces (id, name, root_path, is_active) VALUES (1, 'default', '', 1);
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY,
            workspace_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            abs_path TEXT NOT NULL DEFAULT '',
            current_content_hash TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'active'
        );
        INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, current_content_hash, status)
            VALUES (1, 1, 'src/main.py', '/workspace/src/main.py', 'hash-main', 'active');
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY,
            file_instance_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            qualified_name TEXT NOT NULL,
            kind TEXT NOT NULL,
            start_line INTEGER NOT NULL,
            end_line INTEGER NOT NULL
        );
        INSERT INTO symbols (id, file_instance_id, name, qualified_name, kind, start_line, end_line)
            VALUES (10, 1, 'main', 'src.main.main', 'fn', 1, 10);
        CREATE TABLE archived_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            workspace_id INTEGER NOT NULL,
            rel_path TEXT NOT NULL,
            abs_path TEXT NOT NULL,
            content_hash TEXT DEFAULT '',
            symbol_count INTEGER DEFAULT 0,
            call_count INTEGER DEFAULT 0,
            archive_reason TEXT DEFAULT '',
            archived_at REAL NOT NULL
        );
        CREATE TABLE coverage_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER NOT NULL,
            symbol_id INTEGER,
            line_start INTEGER NOT NULL,
            line_end INTEGER NOT NULL,
            hit_count INTEGER DEFAULT 0,
            report_source TEXT DEFAULT 'lcov',
            imported_at REAL NOT NULL
        );
        CREATE TABLE semgrep_findings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_instance_id INTEGER,
            content_hash TEXT,
            rule_id TEXT,
            rule_name TEXT,
            message TEXT,
            severity TEXT,
            confidence TEXT,
            language TEXT,
            start_line INTEGER,
            end_line INTEGER,
            scanned_at REAL
        );
        INSERT INTO semgrep_findings (file_instance_id, rule_id, severity, message) VALUES (1, 'rule.test', 'WARNING', 'found issue');
        "
    ).unwrap();
    conn.execute(
        "UPDATE workspaces SET root_path = ?1",
        rusqlite::params![dir.to_string_lossy().to_string()],
    )
    .unwrap();

    RuntimeOptions {
        mode: DaemonMode::Local,
        socket_path: dir.join("missing.sock"),
        db_path,
        workspace_id: Some("1".to_string()),
        timeout: Duration::from_secs(5),
    }
}

#[test]
fn test_doctor_command() {
    let dir = tempdir().unwrap();
    let runtime = make_test_runtime(dir.path());
    let res = run_doctor(&runtime, false);
    assert_eq!(res.exit_code, 0);
    assert!(res.stdout.contains("\"doctor\": \"passed\""));
}

#[test]
fn test_semgrep_list_and_stats() {
    let dir = tempdir().unwrap();
    let runtime = make_test_runtime(dir.path());

    let list_res = run_semgrep_list(&runtime, 10);
    assert_eq!(list_res.exit_code, 0);
    assert!(list_res.stdout.contains("rule.test"));

    let stats_res = run_semgrep_stats(&runtime);
    assert_eq!(stats_res.exit_code, 0);
    assert!(stats_res.stdout.contains("WARNING"));
}

#[test]
fn test_coverage_import_and_query() {
    let dir = tempdir().unwrap();
    let runtime = make_test_runtime(dir.path());

    let cov_file = dir.path().join("coverage.info");
    fs::write(&cov_file, "SF:src/main.py\nDA:3,1\nend_of_record\n").unwrap();

    let imp_res = run_coverage_import(&runtime, &cov_file, "lcov");
    assert_eq!(imp_res.exit_code, 0);
    assert!(imp_res.stdout.contains("\"imported\": true"));

    let fn_res = run_coverage_fn(&runtime, "main");
    assert_eq!(fn_res.exit_code, 0);
    assert!(fn_res.stdout.contains("\"found\": true"));
}

#[test]
fn test_install_agent_and_hook() {
    let dir = tempdir().unwrap();
    let runtime = make_test_runtime(dir.path());

    let out_dir = dir.path().join("agent_out");
    let agent_res = run_install_agent(
        &runtime,
        Some("claude".to_string()),
        Some(out_dir.clone()),
        true,
        false,
    );
    assert_eq!(agent_res.exit_code, 0);
    assert!(out_dir.join("claude_callwarden.json").exists());

    let hook_res = run_install_hook(&runtime, "post-commit", "T-1234", false);
    assert_eq!(hook_res.exit_code, 0);
    assert!(hook_res.stdout.contains("\"installed\": true"));
}

#[test]
fn test_gc_commands() {
    let dir = tempdir().unwrap();
    let runtime = make_test_runtime(dir.path());

    let arch_res = run_gc_archive(&runtime, false, false);
    assert_eq!(arch_res.exit_code, 0);

    let status_res = run_gc_status(&runtime);
    assert_eq!(status_res.exit_code, 0);

    let purge_res = run_gc_purge(&runtime, false);
    assert_eq!(purge_res.exit_code, 0);

    let cleanup_res = run_gc_db_cleanup(&runtime, false, false);
    assert_eq!(cleanup_res.exit_code, 0);
}
