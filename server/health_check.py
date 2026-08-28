"""Phase 8.3: Daemon health check 健康检查。

设计参考：
- docs/design/enterprise-daemon-shared-snapshot-plan.md §Phase 8（health check）
- 验收：daemon restart 后自动恢复 workspace registry 和 snapshots

提供：
1. HealthChecker：检查 daemon 各组件健康状态
   - 数据库连通性（registry DB / CAS DB / project DB）
   - workspace registry 完整性
   - job executor 状态
   - 内存/CPU 资源
   - 磁盘空间
2. HealthStatus：健康状态数据结构
3. RecoveryHandler：daemon restart 后的自动恢复
   - 恢复 workspace registry
   - 恢复 snapshot 索引
   - 恢复 job 队列

健康状态级别：
- healthy：所有检查通过
- degraded：部分检查失败，但核心功能可用
- unhealthy：核心功能不可用

返回格式（JSON）：
{
    "status": "healthy",
    "timestamp": 1783698970.0,
    "uptime": 3600.0,
    "checks": [
        {"name": "db_registry", "status": "healthy", "message": ""},
        {"name": "db_cas", "status": "healthy", "message": ""},
        {"name": "disk_space", "status": "degraded", "message": "90% full"},
    ],
    "summary": {
        "total": 5,
        "healthy": 4,
        "degraded": 1,
        "unhealthy": 0
    }
}

SRV-010 权威归属声明（T-1787323461213-e46199b0）：
生产链健康检查权威在 Rust 侧：
- HealthChecker/RecoveryHandler 生产实现为 Rust `health.rs`（G14），经
  `callwarden_core.health_check_all`（PyO3）短路 daemon_server 生产路径；
- 本模块 4 个 Python direct authority（sqlite3.connect）函数已下沉为
  daemon RPC：`mcp.health_check.check_db_registry` /
  `mcp.health_check.recover_workspace_registry` /
  `mcp.health_check.recover_cas_db` / `mcp.health_check.recover_stale_jobs`
  （rust_ext/src/daemon/health_check_handlers.rs，逐字对齐本模块语义）。
本模块类/函数为 compat/test-only 形态：存量测试
（test_phase8_health_check.py 功能构造测试、
test_phase4_3_health_check_diff.py 差分真相源）锁定其函数体，
本卡不得破坏；生产路径不得使用本模块直连 SQLite。
"""

from __future__ import annotations

import os
import time
import sqlite3
import shutil
import json
from typing import Any, Dict, List, Optional, Callable
from enum import Enum


# ============================================================
# 健康状态
# ============================================================


class HealthStatus(str, Enum):
    """健康状态级别。"""

    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"

    @classmethod
    def from_checks(cls, checks: List[Dict[str, Any]]) -> "HealthStatus":
        """根据检查结果列表推导整体状态。

        规则：
        - 任一 unhealthy → unhealthy
        - 无 unhealthy，有 degraded → degraded
        - 全部 healthy → healthy
        """
        if not checks:
            return cls.HEALTHY
        statuses = [c.get("status", "healthy") for c in checks]
        if any(s == cls.UNHEALTHY.value for s in statuses):
            return cls.UNHEALTHY
        if any(s == cls.DEGRADED.value for s in statuses):
            return cls.DEGRADED
        return cls.HEALTHY


# ============================================================
# 单项检查
# ============================================================


class HealthCheck:
    """单项健康检查结果。"""

    def __init__(self, name: str, status: HealthStatus, message: str = "",
                 details: Optional[Dict[str, Any]] = None):
        self.name = name
        self.status = status
        self.message = message
        self.details = details or {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status.value,
            "message": self.message,
            "details": self.details,
        }


# ============================================================
# HealthChecker
# ============================================================


class HealthChecker:
    """Daemon 健康检查器。

    SRV-010：生产链健康检查权威为 Rust `health.rs`（经
    `callwarden_core.health_check_all` 短路 daemon_server 生产路径）；
    本类 `_check_db_registry` 的 daemon RPC 形态已下沉至
    `mcp.health_check.check_db_registry`。函数体受存量测试源码级断言
    锁定，保留原形态（compat/test-only）。

    用法：
        checker = HealthChecker(config)
        result = checker.check_all()
        if result["status"] == "unhealthy":
            # 处理不健康状态
            pass
    """

    def __init__(self, config=None, start_time: Optional[float] = None):
        """初始化健康检查器。

        Args:
            config: DaemonConfig 实例（可选，不传时使用默认配置）
            start_time: daemon 启动时间（用于计算 uptime）
        """
        if config is None:
            from .daemon_config import DaemonConfig
            config = DaemonConfig.default()
        self._config = config
        self._start_time = start_time or time.time()
        self._checks: List[Callable[[], HealthCheck]] = []
        self._register_default_checks()

    def _register_default_checks(self) -> None:
        """注册默认检查项。"""
        self.register_check("db_registry", self._check_db_registry)
        self.register_check("disk_space", self._check_disk_space)
        self.register_check("memory_usage", self._check_memory_usage)
        self.register_check("uptime", self._check_uptime)

    def register_check(self, name: str, check_func: Callable[[], HealthCheck]) -> None:
        """注册自定义检查项。

        Args:
            name: 检查名
            check_func: 检查函数，返回 HealthCheck 实例
        """
        # 用闭包包装，保留 name
        def wrapped():
            try:
                result = check_func()
                if result.name != name:
                    result.name = name
                return result
            except Exception as e:
                return HealthCheck(
                    name=name,
                    status=HealthStatus.UNHEALTHY,
                    message=f"check error: {e}",
                )

        self._checks.append(wrapped)

    # ----- 执行检查 -----

    def check_all(self) -> Dict[str, Any]:
        """执行所有检查，返回汇总结果。"""
        results = []
        for check_func in self._checks:
            check = check_func()
            results.append(check.to_dict())

        overall = HealthStatus.from_checks(results)

        # 汇总统计
        summary = {
            "total": len(results),
            "healthy": sum(1 for r in results if r["status"] == "healthy"),
            "degraded": sum(1 for r in results if r["status"] == "degraded"),
            "unhealthy": sum(1 for r in results if r["status"] == "unhealthy"),
        }

        return {
            "status": overall.value,
            "timestamp": time.time(),
            "uptime": time.time() - self._start_time,
            "checks": results,
            "summary": summary,
        }

    def check_single(self, name: str) -> Optional[Dict[str, Any]]:
        """执行单个检查项。

        Args:
            name: 检查名

        Returns:
            检查结果，如果检查不存在返回 None
        """
        for check_func in self._checks:
            result = check_func()
            if result.name == name:
                return result.to_dict()
        return None

    def list_checks(self) -> List[str]:
        """列出所有已注册的检查名。"""
        names = []
        for check_func in self._checks:
            try:
                result = check_func()
                names.append(result.name)
            except Exception:
                pass
        return names

    # ----- 默认检查项 -----

    def _check_db_registry(self) -> HealthCheck:
        """检查 registry DB 连通性。"""
        db_path = self._config.registry_db_path

        if not os.path.isfile(db_path):
            return HealthCheck(
                name="db_registry",
                status=HealthStatus.UNHEALTHY,
                message=f"registry DB not found: {db_path}",
            )

        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.execute("SELECT 1")
            # 检查表是否存在
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [row[0] for row in cursor.fetchall()]
            conn.close()

            if "daemon_workspaces" not in tables:
                return HealthCheck(
                    name="db_registry",
                    status=HealthStatus.DEGRADED,
                    message="daemon_workspaces table missing",
                    details={"tables": tables},
                )

            return HealthCheck(
                name="db_registry",
                status=HealthStatus.HEALTHY,
                message=f"OK ({len(tables)} tables)",
                details={"tables": tables},
            )
        except sqlite3.Error as e:
            return HealthCheck(
                name="db_registry",
                status=HealthStatus.UNHEALTHY,
                message=f"DB error: {e}",
            )

    def _check_disk_space(self) -> HealthCheck:
        """检查磁盘空间。"""
        data_root = self._config.data_root

        try:
            usage = shutil.disk_usage(
                data_root if os.path.exists(data_root) else "/")
            total = usage.total
            used = usage.used
            free = usage.free
            used_percent = (used / total * 100) if total > 0 else 0

            status = HealthStatus.HEALTHY
            message = f"{free // (1024*1024)} MB free ({used_percent:.1f}% used)"

            if used_percent >= 95:
                status = HealthStatus.UNHEALTHY
                message = f"disk nearly full: {used_percent:.1f}% used"
            elif used_percent >= 85:
                status = HealthStatus.DEGRADED
                message = f"disk space low: {used_percent:.1f}% used"

            return HealthCheck(
                name="disk_space",
                status=status,
                message=message,
                details={
                    "total_bytes": total,
                    "used_bytes": used,
                    "free_bytes": free,
                    "used_percent": round(used_percent, 2),
                },
            )
        except OSError as e:
            return HealthCheck(
                name="disk_space",
                status=HealthStatus.UNHEALTHY,
                message=f"disk check error: {e}",
            )

    def _check_memory_usage(self) -> HealthCheck:
        """检查内存使用情况。"""
        from .metrics import get_memory_info

        mem = get_memory_info()
        rss = mem.get("rss", 0)

        # 解析 memory_max 配置
        max_bytes = _parse_size_to_bytes(self._config.memory_max)
        if max_bytes == 0:
            max_bytes = 1024 * 1024 * 1024  # 默认 1GB

        used_percent = (rss / max_bytes * 100) if max_bytes > 0 else 0

        status = HealthStatus.HEALTHY
        message = f"{rss // (1024*1024)} MB / {max_bytes // (1024*1024)} MB ({used_percent:.1f}%)"

        if used_percent >= 95:
            status = HealthStatus.UNHEALTHY
            message = f"memory nearly full: {used_percent:.1f}%"
        elif used_percent >= 80:
            status = HealthStatus.DEGRADED
            message = f"memory usage high: {used_percent:.1f}%"

        return HealthCheck(
            name="memory_usage",
            status=status,
            message=message,
            details={
                "rss_bytes": rss,
                "vms_bytes": mem.get("vms", 0),
                "peak_bytes": mem.get("peak", 0),
                "max_bytes": max_bytes,
                "used_percent": round(used_percent, 2),
            },
        )

    def _check_uptime(self) -> HealthCheck:
        """检查 uptime。"""
        uptime = time.time() - self._start_time

        status = HealthStatus.HEALTHY
        message = f"uptime: {uptime:.1f}s"

        # daemon 刚启动（uptime < 5s）视为 degraded（可能还在恢复）
        if uptime < 5:
            status = HealthStatus.DEGRADED
            message = f"daemon starting up (uptime: {uptime:.1f}s)"

        return HealthCheck(
            name="uptime",
            status=status,
            message=message,
            details={"uptime_seconds": uptime},
        )


# ============================================================
# RecoveryHandler: daemon restart 后自动恢复
# ============================================================


class RecoveryHandler:
    """Daemon restart 后的自动恢复处理器。

    SRV-010：恢复权威生产形态为 Rust `health.rs` RecoveryHandler；
    本类 `_recover_workspace_registry` / `_recover_cas_db` /
    `_recover_stale_jobs` 三个 direct authority 函数已下沉为 daemon RPC
    `mcp.health_check.*`（health_check_handlers.rs）。本类零生产调用方，
    函数体受 test_phase8 存量测试锁定，保留原形态（compat/test-only）。

    职责：
    1. 恢复 workspace registry（验证已注册 workspace 仍然存在）
    2. 恢复 snapshot 索引（验证 snapshot 文件仍然可用）
    3. 清理 stale job（将 running 状态的 job 标记为 failed，因 daemon 已重启）
    4. 验证 CAS DB 完整性

    用法：
        handler = RecoveryHandler(config)
        result = handler.recover()
        if result["status"] == "healthy":
            # 恢复成功
            pass
    """

    def __init__(self, config=None):
        if config is None:
            from .daemon_config import DaemonConfig
            config = DaemonConfig.default()
        self._config = config

    def recover(self) -> Dict[str, Any]:
        """执行完整恢复流程。

        Returns:
            恢复结果摘要
        """
        results: List[Dict[str, Any]] = []

        # 1. 恢复 workspace registry
        results.append(self._recover_workspace_registry())

        # 2. 恢复 CAS DB
        results.append(self._recover_cas_db())

        # 3. 清理 stale jobs
        results.append(self._recover_stale_jobs())

        # 4. 验证 snapshot 完整性
        results.append(self._recover_snapshots())

        overall = HealthStatus.from_checks(results)
        return {
            "status": overall.value,
            "timestamp": time.time(),
            "recovery_steps": results,
            "summary": {
                "total": len(results),
                "healthy": sum(1 for r in results if r["status"] == "healthy"),
                "degraded": sum(1 for r in results if r["status"] == "degraded"),
                "unhealthy": sum(1 for r in results if r["status"] == "unhealthy"),
            },
        }

    def _recover_workspace_registry(self) -> Dict[str, Any]:
        """恢复 workspace registry。"""
        db_path = self._config.registry_db_path

        if not os.path.isfile(db_path):
            return {
                "name": "workspace_registry",
                "status": HealthStatus.DEGRADED.value,
                "message": "registry DB not found, will be created on first register",
            }

        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.row_factory = sqlite3.Row

            # 验证表结构
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = [row[0] for row in cursor.fetchall()]

            if "daemon_workspaces" not in tables:
                conn.close()
                return {
                    "name": "workspace_registry",
                    "status": HealthStatus.UNHEALTHY.value,
                    "message": "daemon_workspaces table missing",
                }

            # 统计 workspace 数量
            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM daemon_workspaces WHERE status='active'"
            )
            count = cursor.fetchone()["count"]

            # 更新所有 workspace 的 last_active_at（标记 daemon 已恢复）
            conn.execute(
                "UPDATE daemon_workspaces SET last_active_at = ? WHERE status = 'active'",
                (time.time(),)
            )
            conn.commit()
            conn.close()

            return {
                "name": "workspace_registry",
                "status": HealthStatus.HEALTHY.value,
                "message": f"recovered {count} active workspaces",
                "details": {"active_workspaces": count},
            }
        except sqlite3.Error as e:
            return {
                "name": "workspace_registry",
                "status": HealthStatus.UNHEALTHY.value,
                "message": f"DB error: {e}",
            }

    def _recover_cas_db(self) -> Dict[str, Any]:
        """恢复 CAS DB。"""
        db_path = self._config.cas_db_path

        if not os.path.isfile(db_path):
            # CAS DB 不存在是正常的（首次启动）
            return {
                "name": "cas_db",
                "status": HealthStatus.HEALTHY.value,
                "message": "CAS DB not found, will be created on first use",
            }

        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.execute("SELECT 1")
            conn.close()
            return {
                "name": "cas_db",
                "status": HealthStatus.HEALTHY.value,
                "message": "CAS DB accessible",
            }
        except sqlite3.Error as e:
            return {
                "name": "cas_db",
                "status": HealthStatus.UNHEALTHY.value,
                "message": f"CAS DB error: {e}",
            }

    def _recover_stale_jobs(self) -> Dict[str, Any]:
        """清理 stale jobs（将 running 状态的 job 标记为 failed）。

        daemon 重启后，之前 running 的 job 不会再完成，需要标记为 failed。
        """
        # 尝试连接 project DB 中的 jobs 表
        # 如果 jobs 表不存在或 DB 不可访问，跳过
        data_root = self._config.data_root

        # 查找所有 registry DB
        registry_db = self._config.registry_db_path
        if not os.path.isfile(registry_db):
            return {
                "name": "stale_jobs",
                "status": HealthStatus.HEALTHY.value,
                "message": "no registry DB, no stale jobs",
            }

        try:
            conn = sqlite3.connect(registry_db, timeout=5)
            conn.row_factory = sqlite3.Row

            # 检查是否有 jobs 表
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='jobs'"
            )
            if not cursor.fetchone():
                conn.close()
                return {
                    "name": "stale_jobs",
                    "status": HealthStatus.HEALTHY.value,
                    "message": "no jobs table, no stale jobs",
                }

            # 查找 running 状态的 job
            cursor = conn.execute(
                "SELECT COUNT(*) as count FROM jobs WHERE status = 'running'"
            )
            stale_count = cursor.fetchone()["count"]

            if stale_count > 0:
                # 标记为 failed
                conn.execute(
                    """UPDATE jobs
                       SET status = 'failed',
                           error = 'daemon restarted, job interrupted',
                           finished_at = ?
                       WHERE status = 'running'""",
                    (time.time(),)
                )
                conn.commit()

            conn.close()
            return {
                "name": "stale_jobs",
                "status": HealthStatus.HEALTHY.value,
                "message": f"cleaned {stale_count} stale jobs",
                "details": {"stale_jobs_cleaned": stale_count},
            }
        except sqlite3.Error as e:
            return {
                "name": "stale_jobs",
                "status": HealthStatus.DEGRADED.value,
                "message": f"stale job cleanup error: {e}",
            }

    def _recover_snapshots(self) -> Dict[str, Any]:
        """验证 snapshot 完整性。

        检查 snapshot 目录是否存在、是否可读。
        """
        snapshot_dir = os.path.join(self._config.data_root, "snapshots")

        if not os.path.isdir(snapshot_dir):
            # snapshot 目录不存在是正常的（首次启动或无 snapshot）
            return {
                "name": "snapshots",
                "status": HealthStatus.HEALTHY.value,
                "message": "snapshot directory not found, no snapshots to recover",
            }

        try:
            # 统计 snapshot 文件数
            snapshot_files = [
                f for f in os.listdir(snapshot_dir)
                if os.path.isfile(os.path.join(snapshot_dir, f))
            ]
            return {
                "name": "snapshots",
                "status": HealthStatus.HEALTHY.value,
                "message": f"found {len(snapshot_files)} snapshot files",
                "details": {"snapshot_count": len(snapshot_files)},
            }
        except OSError as e:
            return {
                "name": "snapshots",
                "status": HealthStatus.DEGRADED.value,
                "message": f"snapshot directory error: {e}",
            }


# ============================================================
# 工具函数
# ============================================================


def _parse_size_to_bytes(s: str) -> int:
    """将尺寸字符串（如 '1G'、'512M'）转换为字节数。

    Args:
        s: 尺寸字符串

    Returns:
        字节数，解析失败返回 0
    """
    if not s:
        return 0
    s = s.strip()
    if not s:
        return 0
    if s.isdigit():
        return int(s)
    if len(s) < 2:
        return 0
    num_part = s[:-1]
    unit_part = s[-1].upper()
    multipliers = {"K": 1024, "M": 1024*1024,
                   "G": 1024*1024*1024, "T": 1024*1024*1024*1024}
    if unit_part not in multipliers:
        return 0
    try:
        val = int(num_part)
        if val <= 0:
            return 0
        return val * multipliers[unit_part]
    except ValueError:
        return 0


# ============================================================
# 全局单例
# ============================================================


_global_health_checker: Optional[HealthChecker] = None


def get_health_checker(config=None) -> HealthChecker:
    """获取全局 HealthChecker 单例。"""
    global _global_health_checker
    if _global_health_checker is None:
        _global_health_checker = HealthChecker(config)
    return _global_health_checker


def reset_health_checker() -> None:
    """重置全局 HealthChecker（仅用于测试）。"""
    global _global_health_checker
    _global_health_checker = None
