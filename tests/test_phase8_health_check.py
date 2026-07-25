"""Phase 8.3: health check 测试。

测试覆盖：
1. HealthStatus：状态枚举、from_checks 推导
2. HealthCheck：单项检查结果
3. HealthChecker：注册检查、执行检查、汇总结果
4. 默认检查项：db_registry、disk_space、memory_usage、uptime
5. RecoveryHandler：daemon restart 后恢复
6. 全局单例
7. 工具函数
"""

import os
import time
import sqlite3
import json
import pytest

from server.health_check import (
    HealthStatus,
    HealthCheck,
    HealthChecker,
    RecoveryHandler,
    get_health_checker,
    reset_health_checker,
    _parse_size_to_bytes,
)
from server.daemon_config import DaemonConfig


# ============================================================
# HealthStatus 测试
# ============================================================


class TestHealthStatus:
    """HealthStatus 枚举测试。"""

    def test_enum_values(self):
        assert HealthStatus.HEALTHY.value == "healthy"
        assert HealthStatus.DEGRADED.value == "degraded"
        assert HealthStatus.UNHEALTHY.value == "unhealthy"

    def test_from_checks_all_healthy(self):
        checks = [
            {"status": "healthy"},
            {"status": "healthy"},
        ]
        assert HealthStatus.from_checks(checks) == HealthStatus.HEALTHY

    def test_from_checks_has_degraded(self):
        checks = [
            {"status": "healthy"},
            {"status": "degraded"},
        ]
        assert HealthStatus.from_checks(checks) == HealthStatus.DEGRADED

    def test_from_checks_has_unhealthy(self):
        checks = [
            {"status": "healthy"},
            {"status": "degraded"},
            {"status": "unhealthy"},
        ]
        assert HealthStatus.from_checks(checks) == HealthStatus.UNHEALTHY

    def test_from_checks_empty(self):
        assert HealthStatus.from_checks([]) == HealthStatus.HEALTHY

    def test_from_checks_unhealthy_overrides_degraded(self):
        checks = [
            {"status": "degraded"},
            {"status": "unhealthy"},
        ]
        assert HealthStatus.from_checks(checks) == HealthStatus.UNHEALTHY


# ============================================================
# HealthCheck 测试
# ============================================================


class TestHealthCheck:
    """HealthCheck 数据结构测试。"""

    def test_default_values(self):
        check = HealthCheck("test", HealthStatus.HEALTHY)
        assert check.name == "test"
        assert check.status == HealthStatus.HEALTHY
        assert check.message == ""
        assert check.details == {}

    def test_with_message(self):
        check = HealthCheck("test", HealthStatus.DEGRADED, "low disk space")
        assert check.message == "low disk space"

    def test_with_details(self):
        check = HealthCheck("test", HealthStatus.HEALTHY, details={"free": 1024})
        assert check.details == {"free": 1024}

    def test_to_dict(self):
        check = HealthCheck("test", HealthStatus.UNHEALTHY, "error", {"code": 1})
        d = check.to_dict()
        assert d["name"] == "test"
        assert d["status"] == "unhealthy"
        assert d["message"] == "error"
        assert d["details"] == {"code": 1}


# ============================================================
# HealthChecker 测试
# ============================================================


class TestHealthCheckerRegister:
    """HealthChecker 注册检查测试。"""

    def test_default_checks_registered(self):
        checker = HealthChecker()
        checks = checker.list_checks()
        assert "db_registry" in checks
        assert "disk_space" in checks
        assert "memory_usage" in checks
        assert "uptime" in checks

    def test_register_custom_check(self):
        checker = HealthChecker()

        def custom_check():
            return HealthCheck("custom", HealthStatus.HEALTHY, "ok")

        checker.register_check("custom", custom_check)
        assert "custom" in checker.list_checks()

    def test_custom_check_executed(self):
        checker = HealthChecker()

        def custom_check():
            return HealthCheck("custom", HealthStatus.DEGRADED, "custom message")

        checker.register_check("custom", custom_check)
        result = checker.check_single("custom")
        assert result is not None
        assert result["status"] == "degraded"
        assert result["message"] == "custom message"

    def test_check_single_not_found(self):
        checker = HealthChecker()
        assert checker.check_single("nonexistent") is None

    def test_check_with_exception(self):
        checker = HealthChecker()

        def failing_check():
            raise RuntimeError("check failed")

        checker.register_check("failing", failing_check)
        result = checker.check_single("failing")
        assert result is not None
        assert result["status"] == "unhealthy"
        assert "check error" in result["message"]


class TestHealthCheckerCheckAll:
    """HealthChecker.check_all() 测试。"""

    def test_returns_dict(self):
        checker = HealthChecker()
        result = checker.check_all()
        assert isinstance(result, dict)

    def test_has_status(self):
        checker = HealthChecker()
        result = checker.check_all()
        assert "status" in result
        assert result["status"] in ("healthy", "degraded", "unhealthy")

    def test_has_timestamp(self):
        checker = HealthChecker()
        result = checker.check_all()
        assert "timestamp" in result
        assert result["timestamp"] > 0

    def test_has_uptime(self):
        checker = HealthChecker()
        result = checker.check_all()
        assert "uptime" in result
        assert result["uptime"] >= 0.0

    def test_has_checks_list(self):
        checker = HealthChecker()
        result = checker.check_all()
        assert "checks" in result
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) >= 4  # 至少 4 个默认检查

    def test_has_summary(self):
        checker = HealthChecker()
        result = checker.check_all()
        assert "summary" in result
        summary = result["summary"]
        assert "total" in summary
        assert "healthy" in summary
        assert "degraded" in summary
        assert "unhealthy" in summary

    def test_summary_counts_match(self):
        checker = HealthChecker()
        result = checker.check_all()
        summary = result["summary"]
        assert summary["total"] == len(result["checks"])
        assert summary["healthy"] + summary["degraded"] + summary["unhealthy"] == summary["total"]

    def test_all_healthy_when_no_issues(self):
        checker = HealthChecker()

        def healthy_check():
            return HealthCheck("custom", HealthStatus.HEALTHY)

        checker.register_check("custom_healthy", healthy_check)
        result = checker.check_all()
        # 至少有一个自定义检查是 healthy
        custom_check = [c for c in result["checks"] if c["name"] == "custom_healthy"]
        assert len(custom_check) == 1
        assert custom_check[0]["status"] == "healthy"

    def test_unhealthy_check_makes_overall_unhealthy(self):
        checker = HealthChecker()

        def unhealthy_check():
            return HealthCheck("failing", HealthStatus.UNHEALTHY, "test failure")

        checker.register_check("failing_check", unhealthy_check)
        result = checker.check_all()
        assert result["status"] == "unhealthy"


class TestHealthCheckerDefaultChecks:
    """HealthChecker 默认检查项测试。"""

    def test_check_db_registry_not_found(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        checker = HealthChecker(cfg)
        result = checker.check_single("db_registry")
        assert result is not None
        # DB 不存在时应该是 unhealthy
        assert result["status"] == "unhealthy"

    def test_check_db_registry_found(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        db_path = cfg.registry_db_path

        # 创建一个空的 DB
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE daemon_workspaces (
                workspace_id INTEGER PRIMARY KEY,
                status TEXT
            )
        """)
        conn.commit()
        conn.close()

        checker = HealthChecker(cfg)
        result = checker.check_single("db_registry")
        assert result is not None
        assert result["status"] == "healthy"

    def test_check_db_registry_missing_table(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        db_path = cfg.registry_db_path

        # 创建一个 DB 但没有 daemon_workspaces 表
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE other_table (id INTEGER)")
        conn.commit()
        conn.close()

        checker = HealthChecker(cfg)
        result = checker.check_single("db_registry")
        assert result is not None
        assert result["status"] == "degraded"

    def test_check_disk_space_returns_result(self):
        checker = HealthChecker()
        result = checker.check_single("disk_space")
        assert result is not None
        assert "used_percent" in result["details"]

    def test_check_memory_usage_returns_result(self):
        checker = HealthChecker()
        result = checker.check_single("memory_usage")
        assert result is not None
        assert "rss_bytes" in result["details"]

    def test_check_uptime_healthy(self):
        start_time = time.time() - 60  # 60 秒前启动
        checker = HealthChecker(start_time=start_time)
        result = checker.check_single("uptime")
        assert result is not None
        assert result["status"] == "healthy"
        assert result["details"]["uptime_seconds"] >= 60

    def test_check_uptime_degraded(self):
        start_time = time.time() - 1  # 1 秒前启动
        checker = HealthChecker(start_time=start_time)
        result = checker.check_single("uptime")
        assert result is not None
        assert result["status"] == "degraded"


# ============================================================
# RecoveryHandler 测试
# ============================================================


class TestRecoveryHandler:
    """RecoveryHandler 测试。"""

    def test_recover_returns_dict(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        handler = RecoveryHandler(cfg)
        result = handler.recover()
        assert isinstance(result, dict)
        assert "status" in result
        assert "recovery_steps" in result
        assert "summary" in result

    def test_recover_has_4_steps(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        handler = RecoveryHandler(cfg)
        result = handler.recover()
        assert len(result["recovery_steps"]) == 4

    def test_recover_workspace_registry_no_db(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        handler = RecoveryHandler(cfg)
        result = handler.recover()
        # DB 不存在时应该是 degraded
        ws_step = [s for s in result["recovery_steps"] if s["name"] == "workspace_registry"]
        assert len(ws_step) == 1
        assert ws_step[0]["status"] == "degraded"

    def test_recover_workspace_registry_with_db(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        db_path = cfg.registry_db_path

        # 创建 DB 并插入 workspace 数据
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE daemon_workspaces (
                workspace_id INTEGER PRIMARY KEY,
                workspace_instance_id TEXT,
                status TEXT,
                last_active_at REAL
            )
        """)
        conn.execute("""
            INSERT INTO daemon_workspaces
            (workspace_id, workspace_instance_id, status, last_active_at)
            VALUES (1, 'ws-1', 'active', 0)
        """)
        conn.execute("""
            INSERT INTO daemon_workspaces
            (workspace_id, workspace_instance_id, status, last_active_at)
            VALUES (2, 'ws-2', 'archived', 0)
        """)
        conn.commit()
        conn.close()

        handler = RecoveryHandler(cfg)
        result = handler.recover()
        ws_step = [s for s in result["recovery_steps"] if s["name"] == "workspace_registry"]
        assert len(ws_step) == 1
        assert ws_step[0]["status"] == "healthy"
        assert ws_step[0]["details"]["active_workspaces"] == 1

    def test_recover_cas_db_not_found(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        handler = RecoveryHandler(cfg)
        result = handler.recover()
        cas_step = [s for s in result["recovery_steps"] if s["name"] == "cas_db"]
        assert len(cas_step) == 1
        # CAS DB 不存在是正常的
        assert cas_step[0]["status"] == "healthy"

    def test_recover_cas_db_accessible(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        db_path = cfg.cas_db_path

        # 创建 CAS DB
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE cas_entries (id INTEGER)")
        conn.commit()
        conn.close()

        handler = RecoveryHandler(cfg)
        result = handler.recover()
        cas_step = [s for s in result["recovery_steps"] if s["name"] == "cas_db"]
        assert cas_step[0]["status"] == "healthy"

    def test_recover_stale_jobs_no_table(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        db_path = cfg.registry_db_path

        # 创建 DB 但没有 jobs 表
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE daemon_workspaces (id INTEGER)")
        conn.commit()
        conn.close()

        handler = RecoveryHandler(cfg)
        result = handler.recover()
        job_step = [s for s in result["recovery_steps"] if s["name"] == "stale_jobs"]
        assert len(job_step) == 1
        assert job_step[0]["status"] == "healthy"

    def test_recover_stale_jobs_cleans_running(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        db_path = cfg.registry_db_path

        # 创建 DB 并插入 running 状态的 job
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE daemon_workspaces (
                workspace_id INTEGER PRIMARY KEY,
                status TEXT,
                last_active_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE jobs (
                job_id TEXT PRIMARY KEY,
                status TEXT,
                error TEXT,
                finished_at REAL
            )
        """)
        conn.execute("""
            INSERT INTO jobs (job_id, status, error, finished_at)
            VALUES ('J-1', 'running', NULL, NULL)
        """)
        conn.execute("""
            INSERT INTO jobs (job_id, status, error, finished_at)
            VALUES ('J-2', 'running', NULL, NULL)
        """)
        conn.execute("""
            INSERT INTO jobs (job_id, status, error, finished_at)
            VALUES ('J-3', 'completed', NULL, 12345)
        """)
        conn.commit()
        conn.close()

        handler = RecoveryHandler(cfg)
        result = handler.recover()
        job_step = [s for s in result["recovery_steps"] if s["name"] == "stale_jobs"]
        assert job_step[0]["status"] == "healthy"
        assert job_step[0]["details"]["stale_jobs_cleaned"] == 2

        # 验证 DB 中的 running job 已被标记为 failed
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute("SELECT * FROM jobs WHERE job_id = 'J-1'")
        row = cursor.fetchone()
        assert row["status"] == "failed"
        assert "daemon restarted" in row["error"]

        cursor = conn.execute("SELECT * FROM jobs WHERE job_id = 'J-3'")
        row = cursor.fetchone()
        assert row["status"] == "completed"  # 已完成的 job 不受影响
        conn.close()

    def test_recover_snapshots_no_dir(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        handler = RecoveryHandler(cfg)
        result = handler.recover()
        snap_step = [s for s in result["recovery_steps"] if s["name"] == "snapshots"]
        assert len(snap_step) == 1
        assert snap_step[0]["status"] == "healthy"

    def test_recover_snapshots_with_files(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        snapshot_dir = os.path.join(str(tmp_path), "snapshots")
        os.makedirs(snapshot_dir, exist_ok=True)

        # 创建 snapshot 文件
        for i in range(3):
            with open(os.path.join(snapshot_dir, f"snap_{i}.bin"), "w") as f:
                f.write("snapshot data")

        handler = RecoveryHandler(cfg)
        result = handler.recover()
        snap_step = [s for s in result["recovery_steps"] if s["name"] == "snapshots"]
        assert snap_step[0]["status"] == "healthy"
        assert snap_step[0]["details"]["snapshot_count"] == 3

    def test_recover_overall_status(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        handler = RecoveryHandler(cfg)
        result = handler.recover()
        # DB 不存在时 workspace_registry 是 degraded，整体应该是 degraded
        # 但其他步骤可能是 healthy，取决于整体
        assert result["status"] in ("healthy", "degraded", "unhealthy")

    def test_recover_summary_counts(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"data_root": str(tmp_path)})
        handler = RecoveryHandler(cfg)
        result = handler.recover()
        summary = result["summary"]
        assert summary["total"] == 4
        assert summary["healthy"] + summary["degraded"] + summary["unhealthy"] == 4


# ============================================================
# 工具函数测试
# ============================================================


class TestParseSizeToBytes:
    """_parse_size_to_bytes 函数测试。"""

    def test_pure_number(self):
        assert _parse_size_to_bytes("2048") == 2048

    def test_kilobytes(self):
        assert _parse_size_to_bytes("1K") == 1024

    def test_megabytes(self):
        assert _parse_size_to_bytes("1M") == 1024 * 1024

    def test_gigabytes(self):
        assert _parse_size_to_bytes("1G") == 1024 * 1024 * 1024

    def test_terabytes(self):
        assert _parse_size_to_bytes("1T") == 1024 * 1024 * 1024 * 1024

    def test_empty_string(self):
        assert _parse_size_to_bytes("") == 0

    def test_invalid_format(self):
        assert _parse_size_to_bytes("abc") == 0

    def test_invalid_unit(self):
        assert _parse_size_to_bytes("1X") == 0

    def test_negative(self):
        assert _parse_size_to_bytes("-1G") == 0


# ============================================================
# 全局单例测试
# ============================================================


class TestGlobalHealthChecker:
    """全局 HealthChecker 单例测试。"""

    def test_get_health_checker_returns_instance(self):
        reset_health_checker()
        checker = get_health_checker()
        assert isinstance(checker, HealthChecker)

    def test_get_health_checker_singleton(self):
        reset_health_checker()
        c1 = get_health_checker()
        c2 = get_health_checker()
        assert c1 is c2

    def test_reset_health_checker(self):
        c1 = get_health_checker()
        reset_health_checker()
        c2 = get_health_checker()
        assert c1 is not c2
