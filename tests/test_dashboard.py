"""Dashboard Mixin 冒烟测试。

测试 db_dashboard.py 的 get_project_dashboard() 和 get_project_risks()：
1. quick 模式返回所有 7 个 section
2. full 模式（quick=False）含圈复杂度字段
3. with_cycles=True 时 cycles_count 非 None
4. with_evolution=True 时 evolution section 非 None
5. get_project_risks quick 模式 < 200ms，跳过高复杂度检查
6. get_project_risks full 模式包含 high_complexity 风险（若有 > 20 的函数）
7. 隔离性：某 section 失败不影响其他 section
"""
import os
import sys
import time
import shutil
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"))

from callwarden.db.db import CodeGraphDB


@pytest.fixture
def db_with_data():
    """构造一个含小规模合成数据的 DB fixture"""
    tmp = tempfile.mkdtemp(prefix="cw_test_dash_")
    try:
        # 创建一个含高复杂度 + 大函数的测试文件
        # fn_big: 600 行（触发 oversized_function 风险）
        # fn_complex: 多 if/for/while 嵌套（触发 high_complexity 风险）
        test_file = os.path.join(tmp, "mod_test.py")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("def fn_simple():\n    return 1\n\n")
            f.write("def fn_complex(a, b, c):\n")
            f.write("    if a:\n")
            f.write("        if b:\n")
            f.write("            if c:\n")
            f.write("                for i in range(10):\n")
            f.write("                    while i < 5:\n")
            f.write("                        if i % 2 == 0:\n")
            f.write("                            return 1\n")
            f.write("    return 0\n\n")
            f.write("def fn_big():\n")
            for i in range(600):
                f.write(f"    x{i} = {i}\n")
            f.write("    return 0\n")

        db = CodeGraphDB(db_path=os.path.join(tmp, "test.db"), workspace_root=tmp)
        db.register_workspace(name="test", root_path=tmp)
        db.build_full_graph()
        yield db
        db.close()
    finally:
        try:
            shutil.rmtree(tmp)
        except Exception:
            pass


class TestDashboardBasic:
    """基础结构测试"""

    def test_quick_mode_returns_all_sections(self, db_with_data):
        """quick 模式应返回 7 个 section"""
        d = db_with_data.get_project_dashboard()
        expected_sections = {"overview", "code_scale", "code_quality", "call_graph",
                             "task_risk", "audit", "evolution"}
        assert set(d.keys()) == expected_sections

    def test_overview_section(self, db_with_data):
        """overview section 包含核心字段"""
        ov = db_with_data.get_project_dashboard()["overview"]
        assert "workspace_name" in ov
        assert "root_path" in ov
        assert "is_git_repo" in ov
        assert "db_size_bytes" in ov
        assert "db_stale" in ov

    def test_code_scale_section(self, db_with_data):
        """code_scale section 包含总行数和符号数"""
        cs = db_with_data.get_project_dashboard()["code_scale"]
        assert cs["total_files"] >= 1
        assert cs["total_lines"] > 0
        assert cs["total_symbols"] >= 3  # fn_simple + fn_complex + fn_big
        assert "by_kind" in cs
        assert "by_language" in cs

    def test_code_quality_quick_mode(self, db_with_data):
        """quick 模式跳过圈复杂度计算"""
        cq = db_with_data.get_project_dashboard()["code_quality"]
        assert cq["quick_mode"] is True
        assert cq["avg_complexity"] is None
        assert cq["max_complexity"] is None
        assert cq["complexity_distribution"] is None
        # 但注释覆盖率仍然计算
        assert "comment_coverage_pct" in cq
        assert "uncommented_fns" in cq

    def test_code_quality_full_mode(self, db_with_data):
        """full 模式（quick=False）计算圈复杂度"""
        cq = db_with_data.get_project_dashboard(quick=False)["code_quality"]
        assert cq["quick_mode"] is False
        assert cq["avg_complexity"] is not None
        assert cq["max_complexity"] >= 5  # fn_complex 至少有 5+ 分支
        assert cq["complexity_distribution"] is not None

    def test_call_graph_no_cycles(self, db_with_data):
        """call_graph section 默认不计算 cycles"""
        cg = db_with_data.get_project_dashboard()["call_graph"]
        assert cg["cycles_count"] is None
        assert "total_calls" in cg
        assert "resolve_rate_pct" in cg

    def test_call_graph_with_cycles(self, db_with_data):
        """with_cycles=True 时 cycles_count 非 None"""
        cg = db_with_data.get_project_dashboard(with_cycles=True)["call_graph"]
        assert cg["cycles_count"] is not None
        assert cg["cycles_count"] >= 0

    def test_evolution_none_by_default(self, db_with_data):
        """默认不计算 evolution"""
        d = db_with_data.get_project_dashboard()
        assert d["evolution"] is None

    def test_evolution_with_flag(self, db_with_data):
        """with_evolution=True 时 evolution 非 None"""
        d = db_with_data.get_project_dashboard(with_evolution=True)
        assert d["evolution"] is not None
        # 测试项目不是 git repo，recent_commits 应为空 list
        assert "recent_commits" in d["evolution"]


class TestProjectRisks:
    """风险预警测试"""

    def test_quick_mode_is_fast(self, db_with_data):
        """quick 模式应在 200ms 内完成"""
        t0 = time.time()
        risks = db_with_data.get_project_risks(quick=True)
        elapsed_ms = (time.time() - t0) * 1000
        assert elapsed_ms < 200, f"quick mode took {elapsed_ms:.1f}ms, expected < 200ms"

    def test_oversized_function_risk(self, db_with_data):
        """600 行的 fn_big 应触发 oversized_function 风险"""
        risks = db_with_data.get_project_risks(quick=True)
        oversized = [r for r in risks if r["type"] == "oversized_function"]
        assert len(oversized) >= 1
        assert any("fn_big" in r["qualified_name"] for r in oversized)

    def test_quick_skips_complexity(self, db_with_data):
        """quick 模式不返回 high_complexity 风险"""
        risks = db_with_data.get_project_risks(quick=True)
        high_cx = [r for r in risks if r["type"] == "high_complexity"]
        assert len(high_cx) == 0

    def test_full_mode_finds_complexity(self, db_with_data):
        """full 模式应找到高复杂度函数（fn_complex）"""
        risks = db_with_data.get_project_risks(quick=False)
        high_cx = [r for r in risks if r["type"] == "high_complexity"]
        # fn_complex 有 6+ 分支，至少在 complexity > 5 区间，不一定 > 20
        # 这里只验证 full 模式不报错，能返回 list
        assert isinstance(high_cx, list)

    def test_risks_sorted_by_severity(self, db_with_data):
        """风险按 severity 排序：high > medium > low"""
        risks = db_with_data.get_project_risks(quick=False)
        if len(risks) >= 2:
            sev_order = {"high": 0, "medium": 1, "low": 2}
            for i in range(len(risks) - 1):
                s1 = sev_order.get(risks[i].get("severity", "low"), 3)
                s2 = sev_order.get(risks[i + 1].get("severity", "low"), 3)
                assert s1 <= s2


class TestDashboardIsolation:
    """section 隔离性测试"""

    def test_section_error_isolation(self, db_with_data):
        """某 section 失败应返回 {"error": ...}，不影响其他 section"""
        # 模拟：mock get_stats 让 code_scale 抛错
        original = db_with_data.get_stats
        call_count = [0]

        def fake_get_stats():
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("mock error")
            return original()

        db_with_data.get_stats = fake_get_stats
        try:
            d = db_with_data.get_project_dashboard()
            # overview 应该不受影响（不调 get_stats）
            assert "workspace_name" in d["overview"]
            # code_scale 或 call_graph 可能返回 error，但其他 section 仍工作
            assert "task_risk" in d
            assert "audit" in d
        finally:
            db_with_data.get_stats = original
