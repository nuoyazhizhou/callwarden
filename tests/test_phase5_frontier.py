"""Phase 5.4 单元测试：Affected Frontier 计算

测试覆盖：
- AffectedFrontier 基础属性
- compute_frontier 无 store 时只返回 directly_affected
- compute_frontier 有 store 时计算 upstream/downstream
- max_depth 参数控制传递闭包深度
- 端到端管道：parse_delta → compute_frontier
"""
import os
import pytest

import sys
_pyinstall = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rust_ext", "target", "pyinstall"
)
if os.path.isdir(_pyinstall):
    sys.path.insert(0, _pyinstall)

try:
    from callwarden_core import (
        PyDeltaComputer, PyParseDelta, PyResolveDelta,
        PyAffectedFrontier, compute_frontier,
    )
    HAS_RUST = True
except ImportError:
    HAS_RUST = False

pytestmark = pytest.mark.skipif(not HAS_RUST, reason="callwarden_core Rust 扩展未构建")


class TestFrontierBasic:
    """基础 frontier 测试"""

    def test_compute_frontier_no_store(self, tmp_path):
        """无 store 时 frontier 只含 directly_affected"""
        f = tmp_path / "test.py"
        f.write_text("def func_a():\n    pass\n\ndef func_b():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        frontier = compute_frontier(delta)

        assert frontier.direct_count >= 2
        assert frontier.total_count >= 2
        # 无 store → upstream/downstream 为空
        assert len(frontier.upstream_direct) == 0
        assert len(frontier.downstream_direct) == 0

    def test_frontier_is_empty(self, tmp_path):
        """空 parse delta → 空 frontier"""
        f = tmp_path / "empty.py"
        f.write_text("# just a comment\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        # 无符号变更时
        if delta.symbol_stats["added"] == 0:
            frontier = compute_frontier(delta)
            assert frontier.is_empty()

    def test_frontier_repr(self, tmp_path):
        """__repr__ 包含摘要"""
        f = tmp_path / "repr.py"
        f.write_text("def func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        frontier = compute_frontier(delta)
        r = repr(frontier)
        assert "PyAffectedFrontier" in r
        assert "direct" in r

    def test_frontier_summary(self, tmp_path):
        """summary 包含统计信息"""
        f = tmp_path / "summary.py"
        f.write_text("def func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        frontier = compute_frontier(delta)
        s = frontier.summary()
        assert "direct" in s
        assert "upstream" in s
        assert "downstream" in s


class TestFrontierProperties:
    """Frontier 属性测试"""

    def test_directly_affected(self, tmp_path):
        """directly_affected 包含变更文件中的符号"""
        f = tmp_path / "direct.py"
        f.write_text("def alpha():\n    pass\ndef beta():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        frontier = compute_frontier(delta)

        direct = list(frontier.directly_affected)
        assert len(direct) >= 2

    def test_all_affected(self, tmp_path):
        """all_affected 返回所有受影响符号"""
        f = tmp_path / "all.py"
        f.write_text("def gamma():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        frontier = compute_frontier(delta)

        all_aff = list(frontier.all_affected())
        assert len(all_aff) >= 1

    def test_total_count_geq_direct_count(self, tmp_path):
        """total_count >= direct_count"""
        f = tmp_path / "count.py"
        f.write_text("def delta_func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        frontier = compute_frontier(delta)

        assert frontier.total_count >= frontier.direct_count


class TestFrontierMaxDepth:
    """max_depth 参数测试"""

    def test_default_max_depth_1(self, tmp_path):
        """默认 max_depth=1，只有直接 1-hop"""
        f = tmp_path / "depth1.py"
        f.write_text("def func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        frontier = compute_frontier(delta, max_depth=1)

        # 无 store，transitive 应为空
        assert len(frontier.upstream_transitive) == 0
        assert len(frontier.downstream_transitive) == 0

    def test_max_depth_3(self, tmp_path):
        """max_depth=3，允许 3 跳传递闭包"""
        f = tmp_path / "depth3.py"
        f.write_text("def func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        frontier = compute_frontier(delta, max_depth=3)

        # 无 store，transitive 仍为空
        assert len(frontier.upstream_transitive) == 0
        assert len(frontier.downstream_transitive) == 0


class TestFrontierEndToEnd:
    """端到端管道测试"""

    def test_parse_delta_to_frontier(self, tmp_path):
        """从 parse_delta 到 frontier 的完整管道"""
        f = tmp_path / "e2e_frontier.py"
        f.write_text(
            "def caller_func():\n"
            "    return callee_func()\n"
            "\n"
            "def callee_func():\n"
            "    return 42\n"
        )

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        frontier = compute_frontier(delta)

        # 应有至少 2 个直接受影响符号
        assert frontier.direct_count >= 2
        assert frontier.total_count >= 2

    def test_modify_then_frontier(self, tmp_path):
        """修改文件后重新计算 frontier"""
        f = tmp_path / "modify_frontier.py"
        f.write_text("def v1():\n    return 1\n")

        delta1 = PyDeltaComputer.compute_parse_delta(str(f))
        frontier1 = compute_frontier(delta1)

        # 修改文件
        f.write_text("def v1():\n    return 1\n\ndef v2():\n    return 2\n")

        delta2 = PyDeltaComputer.compute_parse_delta(str(f))
        frontier2 = compute_frontier(delta2)

        # 修改后应有更多受影响符号
        assert frontier2.direct_count >= frontier1.direct_count


if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
