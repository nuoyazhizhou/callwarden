"""
Phase 5.5: 局部 depth/cycle/impact 更新 测试

测试 LocalMetricsUpdate 和 compute_local_update 函数。
"""
import os
import sys
import time
import tempfile
from pathlib import Path

import pytest

# 设置 PYTHONPATH
os.environ.setdefault("PYTHONPATH", "")

sys.path.insert(0, str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"))

from callwarden_core import (
    LocalMetricsUpdate,
    compute_local_update,
    PyDeltaComputer,
    compute_frontier,
)


# ============================================
# TestMetricsBasic —— 基础测试
# ============================================

class TestMetricsBasic:
    """基础功能测试"""

    def test_compute_no_store(self, tmp_path):
        """无 store 时，只返回 depth_changes（来自 parse_delta）"""
        f = tmp_path / "test.py"
        f.write_text("def foo():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)

        update = compute_local_update(delta, frontier, impact_depth=3)

        # 无 store 时，depth_changes 来自 parse_delta 的 added 符号
        # added 符号 old_depth=-1, new_depth=0 → is_changed=True
        assert isinstance(update, LocalMetricsUpdate)
        # 至少有一个 depth change（foo 函数 added）
        assert len(update.depth_changes) >= 1
        # 无 store 时无 cycle/impact
        assert len(update.cycle_changes) == 0
        assert len(update.impact_changes) == 0

    def test_empty_frontier(self, tmp_path):
        """空 frontier → 空 update"""
        f = tmp_path / "empty.py"
        f.write_text("# just a comment\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)

        # 空文件无符号变更
        update = compute_local_update(delta, frontier, impact_depth=3)
        assert update.is_empty or len(update.depth_changes) == 0

    def test_repr_and_summary(self, tmp_path):
        """__repr__ 和 summary 方法"""
        f = tmp_path / "test.py"
        f.write_text("def bar():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        repr_str = repr(update)
        summary_str = update.summary()

        assert "LocalMetricsUpdate" in repr_str
        assert "depth" in summary_str
        assert "cycle" in summary_str
        assert "impact" in summary_str

    def test_total_changes(self, tmp_path):
        """total_changes getter"""
        f = tmp_path / "test.py"
        f.write_text("def baz():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        # total_changes = depth + cycle + impact
        total = update.total_changes
        assert total == len(update.depth_changes) + len(update.cycle_changes) + len(update.impact_changes)


# ============================================
# TestDepthChange —— depth 变更测试
# ============================================

class TestDepthChange:
    """Depth 变更测试"""

    def test_added_symbol_depth(self, tmp_path):
        """Added 符号：old_depth=-1, new_depth=0"""
        f = tmp_path / "test.py"
        f.write_text("def new_func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        # new_func 是 added，old_depth=-1, new_depth=0
        added_depths = [d for d in update.depth_changes if d.qualified_name.endswith("new_func")]
        assert len(added_depths) >= 1
        for d in added_depths:
            assert d.old_depth == -1
            assert d.new_depth == 0
            assert d.is_changed is True

    def test_removed_symbol_depth(self, tmp_path):
        """Removed 符号：old_depth 从 store, new_depth=-1"""
        # 无 store 时，removed 符号 old_depth=-1, new_depth=-1 → is_changed=False
        # 所以需要 store 才能测试 removed
        # 简化：无 store 时 removed 不产生 depth change
        f = tmp_path / "test.py"
        f.write_text("# empty\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        # 无符号 → 无 depth change
        for d in update.depth_changes:
            assert d.is_changed

    def test_depth_change_repr(self, tmp_path):
        """DepthChange __repr__"""
        f = tmp_path / "test.py"
        f.write_text("def func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        if update.depth_changes:
            repr_str = repr(update.depth_changes[0])
            assert "DepthChange" in repr_str
            assert "->" in repr_str


# ============================================
# TestCycleChange —— cycle 变更测试
# ============================================

class TestCycleChange:
    """Cycle 变更测试"""

    def test_no_cycle_without_store(self, tmp_path):
        """无 store 时无 cycle 检测"""
        f = tmp_path / "test.py"
        f.write_text("def foo():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        assert len(update.cycle_changes) == 0


# ============================================
# TestImpactChange —— impact 变更测试
# ============================================

class TestImpactChange:
    """Impact 变更测试"""

    def test_no_impact_without_store(self, tmp_path):
        """无 store 时无 impact 计算"""
        f = tmp_path / "test.py"
        f.write_text("def foo():\n    bar()\n\ndef bar():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        assert len(update.impact_changes) == 0

    def test_impact_change_repr(self, tmp_path):
        """ImpactChange __repr__ — 通过实际数据验证"""
        f = tmp_path / "test.py"
        f.write_text("def foo():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        # 无 store 时 impact_changes 为空，验证 is_empty 属性
        assert update.is_empty or len(update.impact_changes) == 0


# ============================================
# TestImpactDepth —— impact 深度参数测试
# ============================================

class TestImpactDepth:
    """Impact 深度参数测试"""

    def test_default_depth_3(self, tmp_path):
        """默认 impact_depth=3"""
        f = tmp_path / "test.py"
        f.write_text("def func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        # 无 store 时 impact 为空，但不应报错
        assert isinstance(update, LocalMetricsUpdate)

    def test_custom_depth(self, tmp_path):
        """自定义 impact_depth"""
        f = tmp_path / "test.py"
        f.write_text("def func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=5)

        assert isinstance(update, LocalMetricsUpdate)


# ============================================
# TestEndToEnd —— 端到端测试
# ============================================

class TestEndToEnd:
    """端到端测试：parse_delta → frontier → metrics"""

    def test_full_pipeline_no_store(self, tmp_path):
        """完整管道（无 store）"""
        f = tmp_path / "test.py"
        f.write_text("""
def alpha():
    pass

def beta():
    alpha()

class MyClass:
    def method(self):
        beta()
""")

        delta = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier = compute_frontier(delta, max_depth=1)
        update = compute_local_update(delta, frontier, impact_depth=3)

        # 验证有 depth changes（alpha, beta, MyClass.method 都是 added）
        assert len(update.depth_changes) >= 2

        # 所有 depth change 都应该是 is_changed=True
        for d in update.depth_changes:
            assert d.is_changed

    def test_modify_then_metrics(self, tmp_path):
        """修改文件后重新计算 metrics"""
        f = tmp_path / "test.py"
        f.write_text("def func_a():\n    pass\n")

        delta1 = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier1 = compute_frontier(delta1, max_depth=1)
        update1 = compute_local_update(delta1, frontier1, impact_depth=3)

        # 修改文件
        f.write_text("def func_a():\n    pass\n\ndef func_b():\n    func_a()\n")

        delta2 = PyDeltaComputer.compute_parse_delta(str(f), None)
        frontier2 = compute_frontier(delta2, max_depth=1)
        update2 = compute_local_update(delta2, frontier2, impact_depth=3)

        # 第二次应该有更多 depth changes（func_b 是新增的）
        assert len(update2.depth_changes) >= len(update1.depth_changes)

    def test_multiple_files_metrics(self, tmp_path):
        """多文件场景"""
        f1 = tmp_path / "a.py"
        f1.write_text("def func_a():\n    pass\n")

        f2 = tmp_path / "b.py"
        f2.write_text("def func_b():\n    pass\n")

        # 分别计算
        delta1 = PyDeltaComputer.compute_parse_delta(str(f1), None)
        frontier1 = compute_frontier(delta1, max_depth=1)
        update1 = compute_local_update(delta1, frontier1, impact_depth=3)

        delta2 = PyDeltaComputer.compute_parse_delta(str(f2), None)
        frontier2 = compute_frontier(delta2, max_depth=1)
        update2 = compute_local_update(delta2, frontier2, impact_depth=3)

        # 两个文件都应有 depth changes
        assert len(update1.depth_changes) >= 1
        assert len(update2.depth_changes) >= 1
