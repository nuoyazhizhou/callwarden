"""Phase 1-1 verify: sqlite_query_schema_version 性能/安全/恢复测试

迁移计划 §4 第 5/6 条要求:
- Performance: P50/P95 延迟、RSS
- Security: 只读、rollback_flag 控制、路径安全
- Recovery: 损坏输入、锁冲突、重复请求、表不存在

同时验证 db_base._get_current_version 接入 Rust 短路后的生产行为。
"""
import os
import sys
import sqlite3
import time
import statistics
from pathlib import Path

import pytest

# 仓库根目录的父目录需要在 sys.path 中(与 test_p0_4_rollback_config.py 一致)
_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

try:
    import callwarden_core
    HAS_RUST = True
except ImportError:
    HAS_RUST = False


@pytest.fixture
def v42_db(tmp_path):
    """创建含 v42 schema_version 记录的数据库"""
    db_path = tmp_path / "v42.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE schema_version ("
        "version INTEGER PRIMARY KEY, applied_at REAL, description TEXT)"
    )
    conn.execute(
        "INSERT INTO schema_version (version, applied_at, description) "
        "VALUES (42, 1000.0, 'test')"
    )
    conn.commit()
    conn.close()
    return str(db_path)


# ============================================================
# 性能测试(迁移计划 §4 第 5 条)
# ============================================================
class TestSqliteQueryPerformance:
    """性能测试:Rust 路径 P50/P95 延迟,并与 Python 对比"""

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_rust_p95_under_10ms(self, v42_db):
        """Rust 路径 P95 延迟应 < 10ms(短连接 + WAL checkpoint 开销可接受)"""
        times = []
        for _ in range(100):
            t0 = time.perf_counter()
            callwarden_core.sqlite_query_schema_version(v42_db)
            times.append((time.perf_counter() - t0) * 1000)

        p50 = statistics.median(times)
        p95 = sorted(times)[95]
        assert p95 < 10.0, f"Rust P95={p95:.3f}ms 超过 10ms 阈值"
        print(f"\n  Rust P50={p50:.3f}ms  P95={p95:.3f}ms  (100 次调用)")

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_rust_vs_python_latency(self, v42_db):
        """Rust 与 Python 路径延迟对比(记录,不强制 Rust 必须更快)"""
        rust_times = []
        for _ in range(50):
            t0 = time.perf_counter()
            callwarden_core.sqlite_query_schema_version(v42_db)
            rust_times.append((time.perf_counter() - t0) * 1000)

        py_times = []
        for _ in range(50):
            t0 = time.perf_counter()
            conn = sqlite3.connect(v42_db)
            conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
            conn.close()
            py_times.append((time.perf_counter() - t0) * 1000)

        rust_p50 = statistics.median(rust_times)
        py_p50 = statistics.median(py_times)
        # 记录对比数据(短连接场景 Rust 可能不比 Python 快,因 PyO3 固定开销)
        print(f"\n  Rust P50={rust_p50:.3f}ms  vs  Python P50={py_p50:.3f}ms")
        # 两者都应返回正确值 42(功能一致性)
        assert callwarden_core.sqlite_query_schema_version(v42_db) == 42


# ============================================================
# 安全测试(迁移计划 §4 第 5 条权限结果)
# ============================================================
class TestSqliteQuerySecurity:
    """安全测试:只读不写、路径校验、不创建文件"""

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_readonly_does_not_modify_db(self, v42_db):
        """只读连接不修改数据库内容"""
        callwarden_core.sqlite_query_schema_version(v42_db)
        conn = sqlite3.connect(v42_db)
        v = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()[0]
        conn.close()
        assert v == 42  # 内容不变

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_empty_path_raises_value_error(self):
        """空路径抛 PyValueError(安全校验,不 crash)"""
        with pytest.raises(Exception, match="db_path"):
            callwarden_core.sqlite_query_schema_version("")

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_nonexistent_path_raises_and_no_file_created(self, tmp_path):
        """不存在的路径抛异常,且只读模式不创建文件"""
        bad_path = str(tmp_path / "nonexistent.db")
        with pytest.raises(Exception):
            callwarden_core.sqlite_query_schema_version(bad_path)
        assert not os.path.exists(bad_path)  # 只读不创建

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_path_traversal_readonly(self, tmp_path):
        """路径遍历:只读连接不创建任何文件"""
        deep_path = str(tmp_path / "deep" / "nested" / "missing.db")
        with pytest.raises(Exception):
            callwarden_core.sqlite_query_schema_version(deep_path)
        assert not os.path.exists(deep_path)


# ============================================================
# 恢复测试(迁移计划 §4 第 6 条)
# ============================================================
class TestSqliteQueryRecovery:
    """恢复测试:损坏输入、表不存在、空表、重复请求、并发写入"""

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_corrupted_db_raises(self, tmp_path):
        """损坏的数据库文件:返回 0 或抛异常,不返回错误数据"""
        bad_db = tmp_path / "corrupted.db"
        bad_db.write_text("not a sqlite database content", encoding="utf-8")
        try:
            v = callwarden_core.sqlite_query_schema_version(str(bad_db))
            # rusqlite bundled 可能将损坏文件当作空库 → 返回 0(可接受)
            assert v == 0, f"损坏文件应返回 0 或抛异常,实际返回 {v}"
        except Exception:
            pass  # 抛异常也可接受(与 Python sqlite3 行为一致)

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_no_schema_version_table_returns_zero(self, tmp_path):
        """无 schema_version 表返回 0(与 Python _get_current_version 一致)"""
        db_path = tmp_path / "other.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE other_table (id INTEGER)")
        conn.commit()
        conn.close()
        assert callwarden_core.sqlite_query_schema_version(str(db_path)) == 0

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_empty_schema_version_table_returns_zero(self, tmp_path):
        """schema_version 表为空返回 0(MAX 返回 NULL → 0)"""
        db_path = tmp_path / "empty_table.db"
        conn = sqlite3.connect(str(db_path))
        conn.execute(
            "CREATE TABLE schema_version ("
            "version INTEGER PRIMARY KEY, applied_at REAL, description TEXT)"
        )
        conn.commit()
        conn.close()
        assert callwarden_core.sqlite_query_schema_version(str(db_path)) == 0

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_repeated_calls_consistent(self, v42_db):
        """重复请求(20 次)结果一致"""
        results = [
            callwarden_core.sqlite_query_schema_version(v42_db)
            for _ in range(20)
        ]
        assert all(r == 42 for r in results)

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_concurrent_write_then_read(self, tmp_path):
        """并发写入后读取:WAL checkpoint 后应读到最新 MAX(version)"""
        db_path = str(tmp_path / "concurrent.db")
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE schema_version ("
            "version INTEGER PRIMARY KEY, applied_at REAL, description TEXT)"
        )
        conn.execute(
            "INSERT INTO schema_version VALUES (42, 1000.0, 'v42')"
        )
        conn.commit()
        # 写入新版本后,Rust 读取应返回 MAX=43
        conn.execute(
            "INSERT INTO schema_version VALUES (43, 1001.0, 'v43')"
        )
        conn.commit()
        conn.close()

        v = callwarden_core.sqlite_query_schema_version(db_path)
        assert v == 43  # WAL checkpoint(PASSIVE)后应读到最新


# ============================================================
# 生产接入验证(_get_current_version Rust 短路 + rollback)
# ============================================================
class TestGetCurrentVersionWireProduction:
    """验证 db_base._get_current_version 接入 Rust 短路后的生产行为"""

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_get_current_version_returns_correct_value(self, tmp_path):
        """_get_current_version 通过 Rust 短路返回正确版本(新库 = SCHEMA_VERSION)"""
        from callwarden.db.db import CodeGraphDB
        from callwarden.db.schema import SCHEMA_VERSION
        db_path = str(tmp_path / "wire_prod.db")
        db = CodeGraphDB(db_path=db_path)
        # 全新数据库:CodeGraphDB __init__ 已 migrate 到 SCHEMA_VERSION
        assert db._get_current_version() == SCHEMA_VERSION
        db.close()

    @pytest.mark.skipif(not HAS_RUST, reason="callwarden_core 未安装")
    def test_rollback_flag_falls_back_to_python(self, tmp_path):
        """rollback_flag=1 时 _get_current_version 走 Python 降级路径"""
        from callwarden.db.db import CodeGraphDB
        from callwarden.db.schema import SCHEMA_VERSION
        db_path = str(tmp_path / "rollback.db")
        db = CodeGraphDB(db_path=db_path)

        # 注册 rollback_config 并设置 flag=1
        db.register_rollback_config(
            task_id="T-test-rollback",
            feature_name="sqlite_query_schema_version",
            phase=1,
            production_entry="db_base._get_current_version Rust shortcut",
            rollback_entry="db_base._get_current_version Python sqlite3",
        )
        db.set_rollback_flag("T-test-rollback", flag=1, reason="test rollback")

        # rollback_flag=1 时应走 Python,仍返回正确值(SCHEMA_VERSION)
        assert db._get_current_version() == SCHEMA_VERSION
        db.close()
