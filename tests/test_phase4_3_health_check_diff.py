"""Phase 4-3 差分测试：health_check_all Rust↔Python 行为一致性。

验证 Rust `callwarden_core.health_check_all` 与 Python
`server/health_check.py:HealthChecker.check_all()` 在相同输入下的行为一致性。

契约：docs/design/phase4-3-metrics-health-audit-contract.md §4 D1 测试矩阵
"""

import json
import os
import sqlite3
import tempfile
from pathlib import Path

import pytest


callwarden_core = pytest.importorskip("callwarden_core")


def _py_check_all(registry_db_path: str, data_root: str,
                  uptime_secs: float, memory_max_bytes: int) -> dict:
    """Python 真相源：调用 HealthChecker.check_all()。

    Python 端的 config.registry_db_path 从 data_root 派生（data_root/registry.db），
    而 Rust 端直接接收 registry_db_path。为对齐两者，Python 端构造 DaemonConfig
    时设置 data_root，然后 HealthChecker 内部自动派生 registry_db_path。

    但差分测试需要精确控制 registry_db_path（测试不存在/存在的 DB），
    因此用 mock config 直接覆盖 registry_db_path 属性。
    """
    from callwarden.server.health_check import HealthChecker
    import time
    from unittest.mock import MagicMock

    # 构造 mock config，覆盖 HealthChecker 4 项检查用到的属性
    # memory_max 是字符串格式（如 "1G"），需转换为 Rust 端的 memory_max_bytes
    if memory_max_bytes >= 1024 * 1024 * 1024:
        memory_max_str = f"{memory_max_bytes // (1024 * 1024 * 1024)}G"
    elif memory_max_bytes >= 1024 * 1024:
        memory_max_str = f"{memory_max_bytes // (1024 * 1024)}M"
    else:
        memory_max_str = str(memory_max_bytes)

    mock_config = MagicMock()
    mock_config.registry_db_path = registry_db_path
    mock_config.data_root = data_root
    mock_config.memory_max = memory_max_str

    checker = HealthChecker(
        config=mock_config,
        start_time=time.time() - uptime_secs,
    )
    return checker.check_all()


def _rust_check_all(registry_db_path: str, data_root: str,
                    uptime_secs: float, memory_max_bytes: int) -> dict:
    """Rust 路径：调用 callwarden_core.health_check_all。"""
    result_json = callwarden_core.health_check_all(
        registry_db_path, data_root, uptime_secs, memory_max_bytes,
    )
    return json.loads(result_json)


class TestD1HealthCheckAll:
    """D1: health_check_all 行为一致性测试。"""

    def test_d1_1_uptime_healthy(self, tmp_path):
        """D1.1: uptime >= 5s，其他检查通过 → uptime check=healthy"""
        rust = _rust_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)
        py = _py_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)

        # uptime 检查应一致
        rust_uptime = next(c for c in rust["checks"] if c["name"] == "uptime")
        py_uptime = next(c for c in py["checks"] if c["name"] == "uptime")
        assert rust_uptime["status"] == py_uptime["status"] == "healthy"

    def test_d1_6_uptime_degraded(self, tmp_path):
        """D1.6: uptime < 5s → status=degraded"""
        rust = _rust_check_all("", str(tmp_path), 2.0, 1024 * 1024 * 1024)
        py = _py_check_all("", str(tmp_path), 2.0, 1024 * 1024 * 1024)

        rust_uptime = next(c for c in rust["checks"] if c["name"] == "uptime")
        py_uptime = next(c for c in py["checks"] if c["name"] == "uptime")
        assert rust_uptime["status"] == py_uptime["status"] == "degraded"

    def test_d1_7_registry_db_not_found(self, tmp_path):
        """D1.7: registry DB 不存在 → db_registry check=unhealthy"""
        missing_db = str(tmp_path / "missing.db")
        rust = _rust_check_all(missing_db, str(tmp_path), 100.0, 1024 * 1024 * 1024)
        py = _py_check_all(missing_db, str(tmp_path), 100.0, 1024 * 1024 * 1024)

        rust_db = next(c for c in rust["checks"] if c["name"] == "db_registry")
        py_db = next(c for c in py["checks"] if c["name"] == "db_registry")
        assert rust_db["status"] == py_db["status"] == "unhealthy"

    def test_d1_registry_db_exists(self, tmp_path):
        """D1: registry DB 存在 + daemon_workspaces 表 → db_registry check=healthy"""
        db_path = tmp_path / "registry.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE daemon_workspaces (
                workspace_instance_id TEXT PRIMARY KEY,
                owner_uid INTEGER,
                client_view_root TEXT,
                host_real_root TEXT,
                status TEXT DEFAULT 'active',
                git_remote_url TEXT DEFAULT '',
                git_head_commit_sha TEXT DEFAULT '',
                toolchain_fingerprint TEXT DEFAULT '',
                created_at TEXT DEFAULT '',
                updated_at TEXT DEFAULT ''
            );
        """)
        conn.commit()
        conn.close()

        rust = _rust_check_all(str(db_path), str(tmp_path), 100.0, 1024 * 1024 * 1024)
        py = _py_check_all(str(db_path), str(tmp_path), 100.0, 1024 * 1024 * 1024)

        rust_db = next(c for c in rust["checks"] if c["name"] == "db_registry")
        py_db = next(c for c in py["checks"] if c["name"] == "db_registry")
        assert rust_db["status"] == py_db["status"] == "healthy"

    def test_d1_disk_space_check_present(self, tmp_path):
        """D1: disk_space 检查存在于 checks 列表中"""
        rust = _rust_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)
        py = _py_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)

        rust_disk = next(c for c in rust["checks"] if c["name"] == "disk_space")
        py_disk = next(c for c in py["checks"] if c["name"] == "disk_space")
        # 两者都应有 status 字段
        assert "status" in rust_disk
        assert "status" in py_disk
        # 磁盘空间应一致（同一目录）
        assert rust_disk["status"] == py_disk["status"]

    def test_d1_memory_check_present(self, tmp_path):
        """D1: memory_usage 检查存在于 checks 列表中"""
        rust = _rust_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)
        py = _py_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)

        rust_mem = next(c for c in rust["checks"] if c["name"] == "memory_usage")
        py_mem = next(c for c in py["checks"] if c["name"] == "memory_usage")
        # 两者都应有 status 字段
        assert "status" in rust_mem
        assert "status" in py_mem
        # 内存检查在 Windows 上可能不同（Rust 返回 unsupported，Python 有 psutil fallback）
        # 但 status 应该都是 healthy（Rust unsupported→healthy，Python 正常检查）

    def test_d1_json_format_consistency(self, tmp_path):
        """D1: JSON 格式一致性（status / timestamp / uptime / checks / summary）"""
        rust = _rust_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)
        py = _py_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)

        # 顶层字段一致
        assert "status" in rust and "status" in py
        assert "checks" in rust and "checks" in py
        assert "summary" in rust and "summary" in py
        # summary 字段一致
        assert rust["summary"]["total"] == py["summary"]["total"] == 4
        assert "healthy" in rust["summary"]
        assert "degraded" in rust["summary"]
        assert "unhealthy" in rust["summary"]

    def test_d1_overall_status_consistency(self, tmp_path):
        """D1: overall status 在相同输入下一致"""
        db_path = tmp_path / "registry.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE daemon_workspaces (
                workspace_instance_id TEXT PRIMARY KEY,
                owner_uid INTEGER,
                client_view_root TEXT,
                host_real_root TEXT,
                status TEXT DEFAULT 'active'
            );
        """)
        conn.commit()
        conn.close()

        rust = _rust_check_all(str(db_path), str(tmp_path), 100.0, 1024 * 1024 * 1024)
        py = _py_check_all(str(db_path), str(tmp_path), 100.0, 1024 * 1024 * 1024)

        # 在正常环境下，两者 overall status 应一致
        # 注意：内存检查在 Windows 上可能不同，但都是 healthy
        # 所以 overall status 应该一致
        assert rust["status"] == py["status"]

    def test_d1_check_names_consistency(self, tmp_path):
        """D1: 4 项检查名称一致（db_registry / disk_space / memory_usage / uptime）"""
        rust = _rust_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)
        py = _py_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)

        rust_names = {c["name"] for c in rust["checks"]}
        py_names = {c["name"] for c in py["checks"]}
        assert rust_names == py_names == {"db_registry", "disk_space", "memory_usage", "uptime"}


class TestD1EdgeCases:
    """D1 边界情况测试。"""

    def test_d1_empty_registry_path(self, tmp_path):
        """D1: 空 registry DB 路径 → db_registry check=unhealthy"""
        rust = _rust_check_all("", str(tmp_path), 100.0, 1024 * 1024 * 1024)
        rust_db = next(c for c in rust["checks"] if c["name"] == "db_registry")
        assert rust_db["status"] == "unhealthy"

    def test_d1_zero_uptime(self, tmp_path):
        """D1: uptime=0 → status=degraded（<5s）"""
        rust = _rust_check_all("", str(tmp_path), 0.0, 1024 * 1024 * 1024)
        rust_uptime = next(c for c in rust["checks"] if c["name"] == "uptime")
        assert rust_uptime["status"] == "degraded"

    def test_d1_large_uptime(self, tmp_path):
        """D1: uptime=86400（1天）→ status=healthy"""
        rust = _rust_check_all("", str(tmp_path), 86400.0, 1024 * 1024 * 1024)
        rust_uptime = next(c for c in rust["checks"] if c["name"] == "uptime")
        assert rust_uptime["status"] == "healthy"

    def test_d1_returns_valid_json(self, tmp_path):
        """D1: 返回有效的 JSON 字符串"""
        result_json = callwarden_core.health_check_all(
            "", str(tmp_path), 100.0, 1024 * 1024 * 1024,
        )
        assert isinstance(result_json, str)
        # 能被 json.loads 解析
        result = json.loads(result_json)
        assert isinstance(result, dict)
        assert "checks" in result
