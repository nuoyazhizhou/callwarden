//! Health Check + Recovery Handler（G14 落地）
//!
//! 对应 Python `server/health_check.py`：
//! - `HealthChecker`：4 个默认检查项（db_registry / disk_space / memory_usage / uptime）
//! - `RecoveryHandler`：daemon restart 后的自动恢复（workspace_registry / cas_db /
//!   stale_jobs / snapshots）
//! - `HealthStatus`：healthy / degraded / unhealthy
//!
//! 跨平台：rusqlite + fs2 + std，无新依赖。
//! 磁盘空间用 `fs2::FileExt::total_space` / `free_space`（fs2 已是依赖）。
//! 内存使用：Linux 读 `/proc/self/status`，其他平台返回 "unsupported"。
//!
//! 返回格式与 Python `check_all()` 一致，客户端无需区分 daemon 实现。

use std::path::Path;
use std::time::Instant;

use rusqlite::Connection;
use serde_json::{json, Value};

// ============================================
// 健康状态
// ============================================

/// 健康状态级别（对应 Python HealthStatus enum）
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum HealthStatus {
    Healthy,
    Degraded,
    Unhealthy,
}

impl HealthStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            HealthStatus::Healthy => "healthy",
            HealthStatus::Degraded => "degraded",
            HealthStatus::Unhealthy => "unhealthy",
        }
    }

    /// 根据检查结果列表推导整体状态
    /// - 任一 unhealthy → unhealthy
    /// - 无 unhealthy，有 degraded → degraded
    /// - 全部 healthy → healthy
    pub fn from_checks(checks: &[Value]) -> Self {
        if checks.is_empty() {
            return HealthStatus::Healthy;
        }
        let has_unhealthy = checks
            .iter()
            .any(|c| c.get("status").and_then(|v| v.as_str()) == Some("unhealthy"));
        if has_unhealthy {
            return HealthStatus::Unhealthy;
        }
        let has_degraded = checks
            .iter()
            .any(|c| c.get("status").and_then(|v| v.as_str()) == Some("degraded"));
        if has_degraded {
            return HealthStatus::Degraded;
        }
        HealthStatus::Healthy
    }
}

// ============================================
// HealthChecker
// ============================================

/// 健康检查配置（从 daemon config 提取）
#[derive(Debug, Clone)]
pub struct HealthConfig {
    /// registry DB 文件路径
    pub registry_db_path: String,
    /// workspace 数据根目录（用于磁盘空间检查）
    pub data_root: String,
    /// daemon 启动时间（用于 uptime 计算）
    pub start_time: Instant,
    /// 内存上限（字节，用于内存使用百分比计算）
    pub memory_max_bytes: u64,
}

impl Default for HealthConfig {
    fn default() -> Self {
        Self {
            registry_db_path: String::new(),
            data_root: String::from("."),
            start_time: Instant::now(),
            memory_max_bytes: 1024 * 1024 * 1024, // 默认 1GB
        }
    }
}

/// 健康检查器（对应 Python HealthChecker）
pub struct HealthChecker {
    config: HealthConfig,
}

impl HealthChecker {
    pub fn new(config: HealthConfig) -> Self {
        Self { config }
    }

    /// 执行所有检查，返回汇总结果（格式与 Python check_all() 一致）
    pub fn check_all(&self) -> Value {
        let checks = vec![
            self.check_db_registry(),
            self.check_disk_space(),
            self.check_memory_usage(),
            self.check_uptime(),
        ];

        let overall = HealthStatus::from_checks(&checks);
        let healthy_count = checks
            .iter()
            .filter(|c| c.get("status").and_then(|v| v.as_str()) == Some("healthy"))
            .count();
        let degraded_count = checks
            .iter()
            .filter(|c| c.get("status").and_then(|v| v.as_str()) == Some("degraded"))
            .count();
        let unhealthy_count = checks
            .iter()
            .filter(|c| c.get("status").and_then(|v| v.as_str()) == Some("unhealthy"))
            .count();

        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let uptime = self.config.start_time.elapsed().as_secs_f64();

        json!({
            "status": overall.as_str(),
            "timestamp": now,
            "uptime": uptime,
            "checks": checks,
            "summary": {
                "total": checks.len(),
                "healthy": healthy_count,
                "degraded": degraded_count,
                "unhealthy": unhealthy_count,
            },
        })
    }

    /// 检查 registry DB 连通性 + daemon_workspaces 表存在
    fn check_db_registry(&self) -> Value {
        let db_path = &self.config.registry_db_path;

        if db_path.is_empty() || !Path::new(db_path).exists() {
            return json!({
                "name": "db_registry",
                "status": "unhealthy",
                "message": format!("registry DB not found: {}", db_path),
                "details": {},
            });
        }

        match Connection::open(db_path) {
            Ok(conn) => {
                match conn.prepare("SELECT name FROM sqlite_master WHERE type='table'") {
                    Ok(mut stmt) => {
                        let tables: Vec<String> = stmt
                            .query_map([], |row| row.get::<_, String>(0))
                            .ok()
                            .map(|rows| rows.filter_map(|r| r.ok()).collect())
                            .unwrap_or_default();

                        if !tables.iter().any(|t| t == "daemon_workspaces") {
                            return json!({
                                "name": "db_registry",
                                "status": "degraded",
                                "message": "daemon_workspaces table missing",
                                "details": {"tables": tables},
                            });
                        }

                        json!({
                            "name": "db_registry",
                            "status": "healthy",
                            "message": format!("OK ({} tables)", tables.len()),
                            "details": {"tables": tables},
                        })
                    }
                    Err(e) => json!({
                        "name": "db_registry",
                        "status": "unhealthy",
                        "message": format!("DB query error: {}", e),
                        "details": {},
                    }),
                }
            }
            Err(e) => json!({
                "name": "db_registry",
                "status": "unhealthy",
                "message": format!("DB connect error: {}", e),
                "details": {},
            }),
        }
    }

    /// 检查磁盘空间（用 fs2::total_space / fs2::free_space 自由函数）
    fn check_disk_space(&self) -> Value {
        let data_root = &self.config.data_root;
        let check_path = if Path::new(data_root).exists() {
            data_root.as_str()
        } else {
            "."
        };

        // fs2 提供 fs2::total_space(path) 和 fs2::free_space(path) 自由函数
        // 跨平台：Windows 用 GetDiskFreeSpaceEx，Unix 用 statvfs
        let total = fs2::total_space(check_path).unwrap_or(0);
        let free = fs2::free_space(check_path).unwrap_or(0);
        let used = total.saturating_sub(free);
        let used_percent = if total > 0 {
            (used as f64 / total as f64) * 100.0
        } else {
            0.0
        };

        let (status, message) = if used_percent >= 95.0 {
            (
                "unhealthy",
                format!("disk nearly full: {:.1}% used", used_percent),
            )
        } else if used_percent >= 85.0 {
            (
                "degraded",
                format!("disk space low: {:.1}% used", used_percent),
            )
        } else {
            (
                "healthy",
                format!("{} MB free ({:.1}% used)", free / (1024 * 1024), used_percent),
            )
        };

        json!({
            "name": "disk_space",
            "status": status,
            "message": message,
            "details": {
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": free,
                "used_percent": (used_percent * 100.0).round() / 100.0,
            },
        })
    }

    /// 检查内存使用
    /// Linux：读 /proc/self/status 获取 VmRSS
    /// 其他平台：返回 "unsupported"（status=healthy，避免误报）
    fn check_memory_usage(&self) -> Value {
        let max_bytes = self.config.memory_max_bytes;

        #[cfg(target_os = "linux")]
        {
            match read_rss_linux() {
                Ok(rss) => {
                    let used_percent = if max_bytes > 0 {
                        (rss as f64 / max_bytes as f64) * 100.0
                    } else {
                        0.0
                    };
                    let (status, message) = if used_percent >= 95.0 {
                        (
                            "unhealthy",
                            format!("memory nearly full: {:.1}%", used_percent),
                        )
                    } else if used_percent >= 80.0 {
                        (
                            "degraded",
                            format!("memory usage high: {:.1}%", used_percent),
                        )
                    } else {
                        (
                            "healthy",
                            format!(
                                "{} MB / {} MB ({:.1}%)",
                                rss / (1024 * 1024),
                                max_bytes / (1024 * 1024),
                                used_percent
                            ),
                        )
                    };
                    json!({
                        "name": "memory_usage",
                        "status": status,
                        "message": message,
                        "details": {
                            "rss_bytes": rss,
                            "max_bytes": max_bytes,
                            "used_percent": (used_percent * 100.0).round() / 100.0,
                        },
                    })
                }
                Err(e) => json!({
                    "name": "memory_usage",
                    "status": "degraded",
                    "message": format!("memory check error: {}", e),
                    "details": {},
                }),
            }
        }

        #[cfg(not(target_os = "linux"))]
        {
            json!({
                "name": "memory_usage",
                "status": "healthy",
                "message": "memory check not supported on this platform",
                "details": {"max_bytes": max_bytes},
            })
        }
    }

    /// 检查 uptime
    fn check_uptime(&self) -> Value {
        let uptime = self.config.start_time.elapsed().as_secs_f64();
        let (status, message) = if uptime < 5.0 {
            (
                "degraded",
                format!("daemon starting up (uptime: {:.1}s)", uptime),
            )
        } else {
            ("healthy", format!("uptime: {:.1}s", uptime))
        };

        json!({
            "name": "uptime",
            "status": status,
            "message": message,
            "details": {"uptime_seconds": uptime},
        })
    }
}

/// Linux：读取 /proc/self/status 获取 VmRSS（字节）
#[cfg(target_os = "linux")]
fn read_rss_linux() -> std::io::Result<u64> {
    let content = std::fs::read_to_string("/proc/self/status")?;
    for line in content.lines() {
        if line.starts_with("VmRSS:") {
            // 格式：VmRSS:    12345 kB
            let parts: Vec<&str> = line.split_whitespace().collect();
            if parts.len() >= 2 {
                let kb: u64 = parts[1].parse().map_err(|_| {
                    std::io::Error::new(std::io::ErrorKind::InvalidData, "VmRSS parse error")
                })?;
                return Ok(kb * 1024);
            }
        }
    }
    Err(std::io::Error::new(
        std::io::ErrorKind::NotFound,
        "VmRSS not found in /proc/self/status",
    ))
}

// ============================================
// RecoveryHandler
// ============================================

/// 恢复处理器（对应 Python RecoveryHandler）
pub struct RecoveryHandler {
    config: HealthConfig,
}

impl RecoveryHandler {
    pub fn new(config: HealthConfig) -> Self {
        Self { config }
    }

    /// 执行完整恢复流程
    pub fn recover(&self) -> Value {
        let steps = vec![
            self.recover_workspace_registry(),
            self.recover_cas_db(),
            self.recover_stale_jobs(),
            self.recover_snapshots(),
        ];

        let overall = HealthStatus::from_checks(&steps);
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);

        let healthy_count = steps
            .iter()
            .filter(|s| s.get("status").and_then(|v| v.as_str()) == Some("healthy"))
            .count();
        let degraded_count = steps
            .iter()
            .filter(|s| s.get("status").and_then(|v| v.as_str()) == Some("degraded"))
            .count();
        let unhealthy_count = steps
            .iter()
            .filter(|s| s.get("status").and_then(|v| v.as_str()) == Some("unhealthy"))
            .count();

        json!({
            "status": overall.as_str(),
            "timestamp": now,
            "recovery_steps": steps,
            "summary": {
                "total": steps.len(),
                "healthy": healthy_count,
                "degraded": degraded_count,
                "unhealthy": unhealthy_count,
            },
        })
    }

    /// 恢复 workspace registry：验证 daemon_workspaces 表 + 更新 last_active_at
    fn recover_workspace_registry(&self) -> Value {
        let db_path = &self.config.registry_db_path;

        if db_path.is_empty() || !Path::new(db_path).exists() {
            return json!({
                "name": "workspace_registry",
                "status": "degraded",
                "message": "registry DB not found, will be created on first register",
            });
        }

        match Connection::open(db_path) {
            Ok(conn) => {
                // 验证 daemon_workspaces 表存在
                let tables: Vec<String> = conn
                    .prepare("SELECT name FROM sqlite_master WHERE type='table'")
                    .ok()
                    .and_then(|mut stmt| {
                        stmt.query_map([], |row| row.get::<_, String>(0))
                            .ok()
                            .map(|rows| rows.filter_map(|r| r.ok()).collect())
                    })
                    .unwrap_or_default();

                if !tables.iter().any(|t| t == "daemon_workspaces") {
                    return json!({
                        "name": "workspace_registry",
                        "status": "unhealthy",
                        "message": "daemon_workspaces table missing",
                    });
                }

                // 统计 active workspace 数量
                let count: i64 = conn
                    .query_row(
                        "SELECT COUNT(*) FROM daemon_workspaces WHERE status='active'",
                        [],
                        |row| row.get(0),
                    )
                    .unwrap_or(0);

                // 更新 last_active_at（标记 daemon 已恢复）
                let now = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .map(|d| d.as_secs_f64())
                    .unwrap_or(0.0);
                let _ = conn.execute(
                    "UPDATE daemon_workspaces SET last_active_at = ? WHERE status = 'active'",
                    rusqlite::params![now],
                );

                json!({
                    "name": "workspace_registry",
                    "status": "healthy",
                    "message": format!("recovered {} active workspaces", count),
                    "details": {"active_workspaces": count},
                })
            }
            Err(e) => json!({
                "name": "workspace_registry",
                "status": "unhealthy",
                "message": format!("DB error: {}", e),
            }),
        }
    }

    /// 恢复 CAS DB：检查可访问性
    /// 注意：CAS DB 是 per-workspace 的（$data_root/$workspace_instance_id/cas.db），
    /// 这里只检查 data_root 目录是否存在，具体 CAS DB 检查在 recover_all_workspaces 中进行。
    fn recover_cas_db(&self) -> Value {
        let data_root = &self.config.data_root;
        if Path::new(data_root).is_dir() {
            json!({
                "name": "cas_db",
                "status": "healthy",
                "message": "data_root accessible",
                "details": {"data_root": data_root},
            })
        } else {
            json!({
                "name": "cas_db",
                "status": "degraded",
                "message": "data_root not found, will be created on first use",
                "details": {"data_root": data_root},
            })
        }
    }

    /// 清理 stale jobs
    /// 当前 daemon 没有 jobs 表（jobs 在 Python 侧的 job_executor 中），
    /// Rust daemon 的 job 队列通过 staging.log + Replicator::recover 处理。
    /// 这里返回 "not applicable"。
    fn recover_stale_jobs(&self) -> Value {
        json!({
            "name": "stale_jobs",
            "status": "healthy",
            "message": "job queue recovery handled by staging.log + Replicator::recover",
            "details": {},
        })
    }

    /// 验证 snapshot 完整性：统计 snapshot 目录文件数
    fn recover_snapshots(&self) -> Value {
        let snapshot_dir = format!("{}/snapshots", self.config.data_root);
        let snapshot_count = match std::fs::read_dir(&snapshot_dir) {
            Ok(entries) => entries.filter_map(|e| e.ok()).count(),
            Err(_) => 0,
        };

        json!({
            "name": "snapshots",
            "status": "healthy",
            "message": format!("{} snapshot files", snapshot_count),
            "details": {"snapshot_count": snapshot_count},
        })
    }
}

// ============================================
// 辅助：从 DaemonState 构建 HealthConfig
// ============================================

/// 从 daemon 运行时参数构建 HealthConfig
pub fn build_health_config(
    registry_db_path: &str,
    data_root: &str,
    start_time: Instant,
    memory_max_bytes: u64,
) -> HealthConfig {
    HealthConfig {
        registry_db_path: registry_db_path.to_string(),
        data_root: data_root.to_string(),
        start_time,
        memory_max_bytes,
    }
}

// ============================================
// 测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Duration;

    fn make_test_db() -> (tempfile::TempDir, String) {
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("test_registry.db");
        let db_path_str = db_path.to_str().unwrap().to_string();
        {
            let conn = Connection::open(&db_path_str).unwrap();
            conn.execute_batch(
                r#"
                CREATE TABLE daemon_workspaces (
                    workspace_id INTEGER PRIMARY KEY,
                    workspace_instance_id TEXT NOT NULL,
                    owner_uid INTEGER NOT NULL,
                    client_view_root TEXT NOT NULL,
                    host_real_root TEXT NOT NULL,
                    git_remote_url TEXT DEFAULT '',
                    git_head_commit_sha TEXT DEFAULT '',
                    toolchain_fingerprint TEXT DEFAULT '',
                    snapshot_id TEXT,
                    registered_at REAL NOT NULL,
                    last_active_at REAL NOT NULL,
                    status TEXT DEFAULT 'active'
                );
                INSERT INTO daemon_workspaces
                    (workspace_id, workspace_instance_id, owner_uid,
                     client_view_root, host_real_root, registered_at, last_active_at)
                VALUES
                    (1, 'ws-1', 1000, '/tmp/a', '/tmp/a', 0.0, 0.0),
                    (2, 'ws-2', 1000, '/tmp/b', '/tmp/b', 0.0, 0.0);
            "#,
            )
            .unwrap();
        }
        (tmp, db_path_str)
    }

    #[test]
    fn test_health_status_from_checks() {
        let checks = vec![json!({"status": "healthy"})];
        assert_eq!(HealthStatus::from_checks(&checks), HealthStatus::Healthy);

        let checks = vec![
            json!({"status": "healthy"}),
            json!({"status": "degraded"}),
        ];
        assert_eq!(HealthStatus::from_checks(&checks), HealthStatus::Degraded);

        let checks = vec![
            json!({"status": "healthy"}),
            json!({"status": "unhealthy"}),
        ];
        assert_eq!(HealthStatus::from_checks(&checks), HealthStatus::Unhealthy);

        let checks: Vec<Value> = vec![];
        assert_eq!(HealthStatus::from_checks(&checks), HealthStatus::Healthy);
    }

    #[test]
    fn test_health_checker_check_all() {
        let (tmp, db_path) = make_test_db();
        let data_root = tmp.path().to_str().unwrap().to_string();
        let config = HealthConfig {
            registry_db_path: db_path,
            data_root,
            start_time: Instant::now() - Duration::from_secs(10),
            memory_max_bytes: 1024 * 1024 * 1024,
        };
        let checker = HealthChecker::new(config);
        let result = checker.check_all();

        assert!(result["status"].is_string());
        assert!(result["timestamp"].is_number());
        assert!(result["uptime"].is_number());
        assert!(result["checks"].is_array());
        assert_eq!(result["checks"].as_array().unwrap().len(), 4);
        assert!(result["summary"].is_object());
        assert_eq!(result["summary"]["total"], 4);

        let check_names: Vec<&str> = result["checks"]
            .as_array()
            .unwrap()
            .iter()
            .map(|c| c["name"].as_str().unwrap())
            .collect();
        assert_eq!(check_names, vec!["db_registry", "disk_space", "memory_usage", "uptime"]);
    }

    #[test]
    fn test_check_db_registry_healthy() {
        let (tmp, db_path) = make_test_db();
        let config = HealthConfig {
            registry_db_path: db_path,
            data_root: tmp.path().to_str().unwrap().to_string(),
            start_time: Instant::now(),
            memory_max_bytes: 0,
        };
        let checker = HealthChecker::new(config);
        let result = checker.check_db_registry();
        assert_eq!(result["status"], "healthy");
        assert!(result["message"].as_str().unwrap().contains("tables"));
    }

    #[test]
    fn test_check_db_registry_missing() {
        let tmp = tempfile::tempdir().unwrap();
        let config = HealthConfig {
            registry_db_path: tmp
                .path()
                .join("nonexistent.db")
                .to_str()
                .unwrap()
                .to_string(),
            data_root: tmp.path().to_str().unwrap().to_string(),
            start_time: Instant::now(),
            memory_max_bytes: 0,
        };
        let checker = HealthChecker::new(config);
        let result = checker.check_db_registry();
        assert_eq!(result["status"], "unhealthy");
    }

    #[test]
    fn test_check_db_registry_no_daemon_workspaces_table() {
        let tmp = tempfile::tempdir().unwrap();
        let db_path = tmp.path().join("test.db");
        let db_path_str = db_path.to_str().unwrap().to_string();
        {
            let conn = Connection::open(&db_path_str).unwrap();
            conn.execute_batch("CREATE TABLE other_table (id INTEGER);")
                .unwrap();
        }
        let config = HealthConfig {
            registry_db_path: db_path_str,
            data_root: tmp.path().to_str().unwrap().to_string(),
            start_time: Instant::now(),
            memory_max_bytes: 0,
        };
        let checker = HealthChecker::new(config);
        let result = checker.check_db_registry();
        assert_eq!(result["status"], "degraded");
        assert!(result["message"].as_str().unwrap().contains("daemon_workspaces"));
    }

    #[test]
    fn test_check_disk_space_returns_valid() {
        let tmp = tempfile::tempdir().unwrap();
        let config = HealthConfig {
            registry_db_path: String::new(),
            data_root: tmp.path().to_str().unwrap().to_string(),
            start_time: Instant::now(),
            memory_max_bytes: 0,
        };
        let checker = HealthChecker::new(config);
        let result = checker.check_disk_space();
        assert!(result["status"].is_string());
        assert!(result["details"]["total_bytes"].is_number());
        assert!(result["details"]["free_bytes"].is_number());
    }

    #[test]
    fn test_check_uptime_healthy() {
        let config = HealthConfig {
            registry_db_path: String::new(),
            data_root: String::from("."),
            start_time: Instant::now() - Duration::from_secs(60),
            memory_max_bytes: 0,
        };
        let checker = HealthChecker::new(config);
        let result = checker.check_uptime();
        assert_eq!(result["status"], "healthy");
    }

    #[test]
    fn test_check_uptime_degraded() {
        let config = HealthConfig {
            registry_db_path: String::new(),
            data_root: String::from("."),
            start_time: Instant::now(),
            memory_max_bytes: 0,
        };
        let checker = HealthChecker::new(config);
        let result = checker.check_uptime();
        assert_eq!(result["status"], "degraded");
    }

    #[test]
    fn test_recovery_handler_recover() {
        let (tmp, db_path) = make_test_db();
        let data_root = tmp.path().to_str().unwrap().to_string();
        let config = HealthConfig {
            registry_db_path: db_path,
            data_root: data_root.clone(),
            start_time: Instant::now(),
            memory_max_bytes: 0,
        };
        let handler = RecoveryHandler::new(config);
        let result = handler.recover();

        assert!(result["status"].is_string());
        assert!(result["recovery_steps"].is_array());
        assert_eq!(result["recovery_steps"].as_array().unwrap().len(), 4);
        assert_eq!(result["summary"]["total"], 4);

        // workspace_registry 应该 healthy
        let ws_step = &result["recovery_steps"][0];
        assert_eq!(ws_step["name"], "workspace_registry");
        assert_eq!(ws_step["status"], "healthy");
        assert_eq!(ws_step["details"]["active_workspaces"], 2);
    }

    #[test]
    fn test_recovery_handler_missing_db() {
        let tmp = tempfile::tempdir().unwrap();
        let config = HealthConfig {
            registry_db_path: tmp
                .path()
                .join("nonexistent.db")
                .to_str()
                .unwrap()
                .to_string(),
            data_root: tmp.path().to_str().unwrap().to_string(),
            start_time: Instant::now(),
            memory_max_bytes: 0,
        };
        let handler = RecoveryHandler::new(config);
        let result = handler.recover();

        let ws_step = &result["recovery_steps"][0];
        assert_eq!(ws_step["status"], "degraded");
    }

    #[test]
    fn test_recovery_handler_updates_last_active_at() {
        let (tmp, db_path) = make_test_db();
        let config = HealthConfig {
            registry_db_path: db_path.clone(),
            data_root: tmp.path().to_str().unwrap().to_string(),
            start_time: Instant::now(),
            memory_max_bytes: 0,
        };

        // 恢复前的 last_active_at 应为 0.0
        {
            let conn = Connection::open(&db_path).unwrap();
            let lat: f64 = conn
                .query_row(
                    "SELECT last_active_at FROM daemon_workspaces WHERE workspace_id=1",
                    [],
                    |row| row.get(0),
                )
                .unwrap();
            assert_eq!(lat, 0.0);
        }

        let handler = RecoveryHandler::new(config);
        let _ = handler.recover();

        // 恢复后 last_active_at 应 > 0
        let conn = Connection::open(&db_path).unwrap();
        let lat: f64 = conn
            .query_row(
                "SELECT last_active_at FROM daemon_workspaces WHERE workspace_id=1",
                [],
                |row| row.get(0),
            )
            .unwrap();
        assert!(lat > 0.0, "last_active_at should be updated");
    }

    #[test]
    fn test_build_health_config() {
        let cfg = build_health_config("/path/to/registry.db", "/data/root", Instant::now(), 1024);
        assert_eq!(cfg.registry_db_path, "/path/to/registry.db");
        assert_eq!(cfg.data_root, "/data/root");
        assert_eq!(cfg.memory_max_bytes, 1024);
    }
}
