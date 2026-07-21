"""批次3 daemon 接线缺口修复验证测试（G14/G15/G17/G19/G32）。

验证 _feature_matrix.md 中 5 个 🟡 部分完成条目的修复：

- G15 SchemaMigrator daemon 启动调用：EnterpriseDaemonService.__init__
  调用 migrate_daemon_dbs（registry.db / audit.db schema 迁移）
- G19 RefreshScheduler daemon 启动实例化：EnterpriseDaemonService.__init__
  实例化 RefreshScheduler，并启动后台定期 flush 线程
- G14 HealthChecker RPC 执行四项检查：health RPC 替代固定 status=ok，
  调用 HealthChecker.check_all() 返回真实四项检查结果
- G17 SnapshotGC Python disk 接入 daemon：daemon 实例化 SnapshotGC，
  提供 evict_snapshot_cache 回调
- G32 Snapshot GC mark→sweep daemon 定期触发：daemon 启动后台线程，
  每 6 小时调用 SnapshotGC.run_gc()
"""

import os
import re
import sys
import time

import pytest

# Windows 不支持 SO_PEERCRED，部分测试需要 import-only 验证
IS_WINDOWS = not hasattr(__import__("socket"), "AF_UNIX")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DAEMON_SERVER_PATH = os.path.join(ROOT, "server", "daemon_server.py")


def _read_daemon_server_source():
    with open(DAEMON_SERVER_PATH, encoding="utf-8") as f:
        return f.read()


# ============================================================
# 1. G15 SchemaMigrator daemon 启动调用
# ============================================================


class TestG15SchemaMigratorWiring:
    """G15：daemon 启动时调用 SchemaMigrator.migrate_daemon_dbs。"""

    def test_g15_daemon_service_has_run_startup_migrations(self):
        """EnterpriseDaemonService 应有 _run_startup_migrations 方法。"""
        source = _read_daemon_server_source()
        assert "_run_startup_migrations" in source, (
            "G15: daemon_server.py 缺少 _run_startup_migrations 方法"
        )

    def test_g15_calls_migrate_daemon_dbs(self):
        """_run_startup_migrations 应调用 migrate_daemon_dbs。"""
        source = _read_daemon_server_source()
        assert "migrate_daemon_dbs" in source, (
            "G15: _run_startup_migrations 未调用 migrate_daemon_dbs"
        )

    def test_g15_init_invokes_startup_migrations(self):
        """__init__ 应通过 run_startup_migrations 参数触发迁移。"""
        source = _read_daemon_server_source()
        # __init__ 中应有 if run_startup_migrations: self._run_startup_migrations()
        assert "run_startup_migrations" in source, (
            "G15: __init__ 缺少 run_startup_migrations 参数"
        )
        assert "self._run_startup_migrations()" in source, (
            "G15: __init__ 未调用 _run_startup_migrations"
        )

    def test_g15_init_loads_daemon_config(self):
        """__init__ 应加载 DaemonConfig（用于 migrate_daemon_dbs 路径）。"""
        source = _read_daemon_server_source()
        assert "DaemonConfig" in source, (
            "G15: __init__ 未加载 DaemonConfig"
        )
        assert "self._config" in source, (
            "G15: __init__ 未保存 self._config"
        )

    def test_g15_run_startup_migrations_handles_failure_gracefully(self):
        """迁移失败时应记录日志但不抛出（保持向后兼容）。"""
        source = _read_daemon_server_source()
        # _run_startup_migrations 应有 try-except 包裹
        idx = source.find("def _run_startup_migrations")
        assert idx >= 0, "G15: _run_startup_migrations 方法不存在"
        # 截取方法体（到下一个 def 之前）
        method_body = source[idx:source.find("def ", idx + 30)]
        assert "try:" in method_body, "G15: _run_startup_migrations 缺少 try 块"
        assert "except Exception" in method_body, (
            "G15: _run_startup_migrations 缺少 except Exception"
        )

    def test_g15_run_startup_migrations_param_default_true(self):
        """run_startup_migrations 默认应为 True（启用迁移）。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        import inspect
        sig = inspect.signature(EnterpriseDaemonService.__init__)
        param = sig.parameters.get("run_startup_migrations")
        assert param is not None, (
            "G15: __init__ 缺少 run_startup_migrations 参数"
        )
        assert param.default is True, (
            f"G15: run_startup_migrations 默认值应为 True，实际 {param.default}"
        )


# ============================================================
# 2. G19 RefreshScheduler daemon 启动实例化
# ============================================================


class TestG19RefreshSchedulerWiring:
    """G19：daemon 启动时实例化 RefreshScheduler 并启动后台 flush 线程。"""

    def test_g19_init_instantiates_refresh_scheduler(self):
        """__init__ 应实例化 RefreshScheduler。"""
        source = _read_daemon_server_source()
        assert "RefreshScheduler" in source, (
            "G19: __init__ 未实例化 RefreshScheduler"
        )
        assert "self._refresh_scheduler" in source, (
            "G19: __init__ 未保存 self._refresh_scheduler"
        )

    def test_g19_starts_background_thread(self):
        """daemon 启动应启动后台 flush 线程。"""
        source = _read_daemon_server_source()
        assert "_refresh_flush_loop" in source, (
            "G19: 缺少 _refresh_flush_loop 方法"
        )
        assert "cw-refresh-flush" in source, (
            "G19: 后台线程命名缺少 cw-refresh-flush"
        )

    def test_g19_start_background_tasks_default_true(self):
        """start_background_tasks 默认应为 True。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        import inspect
        sig = inspect.signature(EnterpriseDaemonService.__init__)
        param = sig.parameters.get("start_background_tasks")
        assert param is not None, (
            "G19: __init__ 缺少 start_background_tasks 参数"
        )
        assert param.default is True, (
            f"G19: start_background_tasks 默认值应为 True，实际 {param.default}"
        )

    def test_g19_default_flush_interval_constant(self):
        """应有默认 flush 间隔常量（60 秒）。"""
        source = _read_daemon_server_source()
        assert "DEFAULT_REFRESH_FLUSH_INTERVAL_SEC" in source, (
            "G19: 缺少 DEFAULT_REFRESH_FLUSH_INTERVAL_SEC 常量"
        )

    def test_g19_refresh_batch_callback_registered(self):
        """RefreshScheduler 应注册 batch 就绪回调。"""
        source = _read_daemon_server_source()
        assert "_on_refresh_batch_ready" in source, (
            "G19: 缺少 _on_refresh_batch_ready 回调方法"
        )
        assert "on_batch_ready=self._on_refresh_batch_ready" in source, (
            "G19: __init__ 未注册 on_batch_ready 回调"
        )

    def test_g19_shutdown_background_tasks(self):
        """daemon 应提供 shutdown_background_tasks 方法停止后台线程。"""
        source = _read_daemon_server_source()
        assert "def shutdown_background_tasks" in source, (
            "G19: 缺少 shutdown_background_tasks 方法"
        )


# ============================================================
# 3. G14 HealthChecker RPC 执行四项检查
# ============================================================


class TestG14HealthCheckerWiring:
    """G14：health RPC 替代固定 status=ok，调用 HealthChecker.check_all()。"""

    def test_g14_init_instantiates_health_checker(self):
        """__init__ 应实例化 HealthChecker。"""
        source = _read_daemon_server_source()
        assert "HealthChecker" in source, (
            "G14: __init__ 未实例化 HealthChecker"
        )
        assert "self._health_checker" in source, (
            "G14: __init__ 未保存 self._health_checker"
        )

    def test_g14_health_rpc_calls_check_all(self):
        """health RPC 应调用 HealthChecker.check_all()。"""
        source = _read_daemon_server_source()
        # 定位 if method == "health":
        idx = source.find('if method == "health"')
        assert idx >= 0, "G14: 缺少 if method == \"health\" 分支"
        # 截取 health 方法体
        method_body = source[idx:source.find("if method == ", idx + 30)]
        assert "check_all" in method_body, (
            "G14: health RPC 未调用 HealthChecker.check_all()"
        )

    def test_g14_health_rpc_no_hardcoded_ok(self):
        """health RPC 不应硬编码 status=ok（应由 check_all 决定）。"""
        source = _read_daemon_server_source()
        idx = source.find('if method == "health"')
        assert idx >= 0, "G14: 缺少 if method == \"health\" 分支"
        # 找到 health 方法体的 return 语句
        method_end = source.find("if method == ", idx + 30)
        method_body = source[idx:method_end]
        # 找 "status": "ok" 硬编码（应已移除）
        hardcoded_ok_pattern = r'"status"\s*:\s*"ok"'
        matches = re.findall(hardcoded_ok_pattern, method_body)
        # 允许 0 个匹配（已完全移除硬编码）或 1 个且在 health_result.update 中（兼容字段）
        # 实际应 0 个，因为 health_result 来自 check_all()
        assert len(matches) == 0, (
            f"G14: health RPC 仍硬编码 status=ok ({len(matches)} 处)，"
            "应由 HealthChecker.check_all() 决定"
        )

    def test_g14_health_rpc_returns_checks_field(self):
        """health RPC 应返回 checks 字段（四项检查详细结果）。"""
        source = _read_daemon_server_source()
        idx = source.find('if method == "health"')
        method_body = source[idx:source.find("if method == ", idx + 30)]
        # check_all() 返回的 dict 已包含 checks 字段，不需要硬编码
        # 只验证调用了 check_all
        assert "health_result" in method_body, (
            "G14: health RPC 未保存 check_all() 结果到 health_result"
        )

    def test_g14_health_checker_config_passed(self):
        """HealthChecker 应接收 daemon 的 DaemonConfig 实例。"""
        source = _read_daemon_server_source()
        # 找 HealthChecker 实例化代码
        idx = source.find("HealthChecker(")
        assert idx >= 0, "G14: __init__ 中未实例化 HealthChecker"
        snippet = source[idx:idx + 300]
        assert "config=self._config" in snippet, (
            "G14: HealthChecker 未接收 self._config"
        )
        assert "start_time=self._start_time" in snippet, (
            "G14: HealthChecker 未接收 self._start_time"
        )


# ============================================================
# 4. G17 SnapshotGC Python disk 接入 daemon scheduler
# ============================================================


class TestG17SnapshotGCWiring:
    """G17：daemon 实例化 SnapshotGC 并提供 evict 回调。"""

    def test_g17_init_instantiates_snapshot_gc(self):
        """__init__ 应实例化 SnapshotGC。"""
        source = _read_daemon_server_source()
        assert "SnapshotGC" in source, (
            "G17: __init__ 未实例化 SnapshotGC"
        )
        assert "self._snapshot_gc" in source, (
            "G17: __init__ 未保存 self._snapshot_gc"
        )

    def test_g17_evict_callback_registered(self):
        """SnapshotGC 应注册 snapshot_cache_evictor 回调。"""
        source = _read_daemon_server_source()
        assert "snapshot_cache_evictor" in source, (
            "G17: 未注册 snapshot_cache_evictor 回调"
        )
        assert "_evict_snapshot_cache" in source, (
            "G17: 缺少 _evict_snapshot_cache 方法"
        )

    def test_g17_snapshot_gc_uses_daemon_config(self):
        """SnapshotGC 应接收 daemon 的 DaemonConfig。"""
        source = _read_daemon_server_source()
        idx = source.find("SnapshotGC(")
        assert idx >= 0, "G17: __init__ 中未实例化 SnapshotGC"
        snippet = source[idx:idx + 200]
        assert "cfg=self._config" in snippet, (
            "G17: SnapshotGC 未接收 self._config"
        )


# ============================================================
# 5. G32 Snapshot GC mark→sweep daemon 定期触发
# ============================================================


class TestG32SnapshotGCPeriodicRun:
    """G32：daemon 启动后台线程定期调用 SnapshotGC.run_gc()。"""

    def test_g32_starts_gc_thread(self):
        """daemon 启动应启动 SnapshotGC 后台线程。"""
        source = _read_daemon_server_source()
        assert "_snapshot_gc_loop" in source, (
            "G32: 缺少 _snapshot_gc_loop 方法"
        )
        assert "cw-snapshot-gc" in source, (
            "G32: 后台线程命名缺少 cw-snapshot-gc"
        )

    def test_g32_default_gc_interval_constant(self):
        """应有默认 GC 间隔常量（6 小时 = 21600 秒）。"""
        source = _read_daemon_server_source()
        assert "DEFAULT_SNAPSHOT_GC_INTERVAL_SEC" in source, (
            "G32: 缺少 DEFAULT_SNAPSHOT_GC_INTERVAL_SEC 常量"
        )

    def test_g32_loop_calls_run_gc(self):
        """_snapshot_gc_loop 应调用 SnapshotGC.run_gc()。"""
        source = _read_daemon_server_source()
        idx = source.find("def _snapshot_gc_loop")
        assert idx >= 0, "G32: _snapshot_gc_loop 方法不存在"
        method_body = source[idx:source.find("def ", idx + 30)]
        assert "run_gc" in method_body, (
            "G32: _snapshot_gc_loop 未调用 run_gc()"
        )

    def test_g32_loop_uses_stop_event(self):
        """_snapshot_gc_loop 应通过 stop event 优雅停止。"""
        source = _read_daemon_server_source()
        idx = source.find("def _snapshot_gc_loop")
        method_body = source[idx:source.find("def ", idx + 30)]
        assert "_gc_stop" in method_body, (
            "G32: _snapshot_gc_loop 未检查 _gc_stop 事件"
        )
        assert ".wait(" in method_body, (
            "G32: _snapshot_gc_loop 未使用 Event.wait() 阻塞"
        )

    def test_g32_shutdown_background_tasks_stops_gc(self):
        """shutdown_background_tasks 应设置 _gc_stop 事件。"""
        source = _read_daemon_server_source()
        idx = source.find("def shutdown_background_tasks")
        assert idx >= 0, "G32: 缺少 shutdown_background_tasks 方法"
        method_body = source[idx:source.find("def ", idx + 30)]
        assert "_gc_stop.set()" in method_body, (
            "G32: shutdown_background_tasks 未设置 _gc_stop.set()"
        )


# ============================================================
# 6. 端到端验证（非 Windows 平台）
# ============================================================


@pytest.mark.skipif(IS_WINDOWS, reason="UDS E2E 测试需要 Unix domain socket")
class TestBatch3DaemonE2E:
    """端到端验证：构造 EnterpriseDaemonService 实例并验证接线。"""

    def test_service_init_with_background_tasks(self, tmp_path):
        """构造 service 时应启动后台任务并执行 schema 迁移。"""
        registry_db = str(tmp_path / "registry.db")
        # 构造 service，启用所有接线
        from callwarden.server.daemon_server import EnterpriseDaemonService
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            data_root=str(tmp_path / "enterprise"),
        )
        try:
            # G15: 启动迁移应已执行（registry.db schema_version 表存在）
            assert hasattr(service, "_config"), "G15: _config 未设置"
            assert hasattr(service, "_health_checker"), "G14: _health_checker 未设置"
            assert hasattr(service, "_refresh_scheduler"), "G19: _refresh_scheduler 未设置"
            assert hasattr(service, "_snapshot_gc"), "G17: _snapshot_gc 未设置"
            assert service._gc_thread is not None, "G32: _gc_thread 未启动"
            assert service._gc_thread.is_alive(), "G32: _gc_thread 未运行"
        finally:
            service.shutdown_background_tasks()

    def test_health_rpc_returns_real_checks(self, tmp_path):
        """health RPC 应返回四项实际检查结果（而非固定 status=ok）。"""
        registry_db = str(tmp_path / "registry.db")
        from callwarden.server.daemon_server import EnterpriseDaemonService
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            data_root=str(tmp_path / "enterprise"),
        )
        try:
            peer = {"uid": os.getuid(), "gid": os.getgid(), "pid": os.getpid()}
            result = service.dispatch(peer, "health", {})
            # G14: 应包含 checks 字段（四项检查结果）
            assert "checks" in result, "G14: health RPC 缺少 checks 字段"
            assert "summary" in result, "G14: health RPC 缺少 summary 字段"
            assert "status" in result, "G14: health RPC 缺少 status 字段"
            # 应有四项检查
            checks = result["checks"]
            check_names = [c.get("name", "") for c in checks]
            assert "db_registry" in check_names, "G14: 缺少 db_registry 检查"
            assert "disk_space" in check_names, "G14: 缺少 disk_space 检查"
            assert "memory_usage" in check_names, "G14: 缺少 memory_usage 检查"
            assert "uptime" in check_names, "G14: 缺少 uptime 检查"
            # 兼容字段
            assert "pid" in result, "G14: health RPC 缺少 pid 兼容字段"
            assert "uptime_seconds" in result, "G14: health RPC 缺少 uptime_seconds 兼容字段"
            assert "workspace_count" in result, "G14: health RPC 缺少 workspace_count 兼容字段"
        finally:
            service.shutdown_background_tasks()

    def test_service_init_without_background_tasks(self, tmp_path):
        """构造 service 时可禁用后台任务（用于测试）。"""
        registry_db = str(tmp_path / "registry.db")
        from callwarden.server.daemon_server import EnterpriseDaemonService
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            data_root=str(tmp_path / "enterprise"),
            start_background_tasks=False,
        )
        try:
            assert service._gc_thread is None, (
                "禁用后台任务时不应启动 _gc_thread"
            )
        finally:
            service.shutdown_background_tasks()

    def test_service_init_without_migrations(self, tmp_path):
        """构造 service 时可禁用 schema 迁移（用于测试）。"""
        registry_db = str(tmp_path / "registry.db")
        from callwarden.server.daemon_server import EnterpriseDaemonService
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            data_root=str(tmp_path / "enterprise"),
            run_startup_migrations=False,
            start_background_tasks=False,
        )
        try:
            # 即使禁用迁移，DaemonConfig 仍应加载
            assert hasattr(service, "_config"), "禁用迁移时 _config 仍应加载"
        finally:
            service.shutdown_background_tasks()


# ============================================================
# 7. _feature_matrix.md 状态更新
# ============================================================


class TestFeatureMatrixStatusUpdate:
    """_feature_matrix.md 中 G14/G15/G17/G19/G32 状态应与复审报告一致。

    复审报告 §6（feature-matrix-code-reaudit-2026-07-21.md）：G14/G15 因
    Python daemon 有实现但 Linux systemd 启动 Rust cw_daemon 未对齐，回退为 🟡。
    G17/G19/G32 已通过复审保持 ✅。
    """

    @pytest.fixture
    def matrix_content(self):
        with open(os.path.join(ROOT, "_feature_matrix.md"), encoding="utf-8") as f:
            return f.read()

    @pytest.mark.parametrize("gid,expected_keyword", [
        ("G14", "🟡"),
        ("G15", "🟡"),
        ("G17", "✅"),
        ("G19", "✅"),
        ("G32", "✅"),
    ])
    def test_g_entry_status_updated(self, matrix_content, gid, expected_keyword):
        """G14/G15 应为 🟡 复审回退；G17/G19/G32 保持 ✅ 已修复。"""
        line_match = re.search(rf"^\| {gid} \|.*$", matrix_content, re.MULTILINE)
        assert line_match, f"_feature_matrix.md 必须有 {gid} 行"
        line = line_match.group(0)
        assert expected_keyword in line, (
            f"{gid} 状态应为 {expected_keyword}，实际：{line}"
        )
        assert "批次3" in line or "2026-07-20" in line or "2026-07-21" in line, (
            f"{gid} 应标注批次3 / 2026-07-20 / 2026-07-21 日期，实际：{line}"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
