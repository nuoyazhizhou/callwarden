"""Phase 4-2 差分测试：UID/workspace ACL、路径安全与资源预算。

验证 Rust PyO3 API（callwarden_core.*）与 Python 真相源（server/daemon_server.py、
server/query_budget.py）的行为一致性。

测试矩阵（D1-D6）：
- D1: validate_owned_path 基本行为
- D2: check_path_within_workspace 路径逃逸检查
- D3: is_admin_uid / current_daemon_uid_py
- D4: check_workspace_owner
- D5: budget_create / budget_preset
- D6: budget_tracker 行为
"""
import os
import sys
import time
import pytest
from pathlib import Path

# 加载 callwarden_core
import callwarden_core as rust

# Python 真相源
sys.path.insert(0, str(Path(__file__).parent.parent))
from server.query_budget import (
    QueryBudget,
    default_budget,
    deep_budget,
    shallow_budget,
    unlimited_budget,
)


def _normalize_path_for_compare(p: str) -> str:
    """归一化路径用于跨平台比较。

    Rust std::fs::canonicalize 在 Windows 上会加 `\\\\?\\` UNC 前缀，
    Python os.path.realpath 不会。此函数剥离 UNC 前缀让两者可比对。
    契约 §5.1 预期差异：路径规范化在无 symlink 时行为一致。
    """
    import os
    if os.name == "nt" and p.startswith("\\\\?\\"):
        return p[4:]
    return p


# ============================================
# D1: validate_owned_path 基本行为
# ============================================

class TestValidateOwnedPathDiff:
    """验证 Rust validate_owned_path 与 Python _validate_owned_path 行为一致。

    Python 真相源：server/daemon_server.py:_validate_owned_path
    """

    def test_d1_1_existing_file_require_file_true(self, tmp_path):
        """D1.1: 存在的文件路径，require_file=True，owner 匹配"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")

        # Rust 路径
        rust_result = rust.validate_owned_path(str(test_file), 0, True)
        # Python 真相源（简化版，不调 daemon_server，直接 os.path.realpath）
        import os
        py_result = os.path.realpath(str(test_file))

        assert _normalize_path_for_compare(rust_result) == _normalize_path_for_compare(py_result)

    def test_d1_2_existing_dir_require_file_false(self, tmp_path):
        """D1.2: 存在的目录路径，require_file=False，owner 匹配"""
        rust_result = rust.validate_owned_path(str(tmp_path), 0, False)
        import os
        py_result = os.path.realpath(str(tmp_path))
        assert _normalize_path_for_compare(rust_result) == _normalize_path_for_compare(py_result)

    def test_d1_3_nonexistent_path(self, tmp_path):
        """D1.3: 不存在的路径 → path_not_found"""
        fake_path = str(tmp_path / "nonexistent.txt")
        with pytest.raises(RuntimeError, match="path_not_found"):
            rust.validate_owned_path(fake_path, 0, True)

    def test_d1_4_require_file_true_but_is_dir(self, tmp_path):
        """D1.4: require_file=True 但路径是目录 → path_not_found"""
        with pytest.raises(RuntimeError, match="path_not_found"):
            rust.validate_owned_path(str(tmp_path), 0, True)

    def test_d1_5_require_file_false_but_is_file(self, tmp_path):
        """D1.5: require_file=False 但路径是文件 → path_not_found"""
        test_file = tmp_path / "test.txt"
        test_file.write_text("hello")
        with pytest.raises(RuntimeError, match="path_not_found"):
            rust.validate_owned_path(str(test_file), 0, False)

    def test_d1_6_root_uid_skips_owner_check(self, tmp_path):
        """D1.6: peer_uid=0（root）跳过 owner 检查"""
        test_file = tmp_path / "root_test.txt"
        test_file.write_text("root")

        # peer_uid=0 应该通过（即使文件 owner 不一定是 0）
        result = rust.validate_owned_path(str(test_file), 0, True)
        assert result  # 返回路径字符串


# ============================================
# D2: check_path_within_workspace 路径逃逸检查
# ============================================

class TestCheckPathWithinWorkspaceDiff:
    """验证 Rust check_path_within_workspace 路径逃逸检查。

    Python 真相源：server/daemon_server.py workspace.file.refresh (L944-956)
    """

    def test_d2_1_abs_path_equals_host_root(self, tmp_path):
        """D2.1: abs_path == host_real_root → 通过"""
        rust.check_path_within_workspace(str(tmp_path), str(tmp_path))

    def test_d2_2_abs_path_is_child_of_host_root(self, tmp_path):
        """D2.2: abs_path 是 host_real_root 的子路径 → 通过"""
        child = tmp_path / "child_dir"
        child.mkdir()
        rust.check_path_within_workspace(str(child), str(tmp_path))

    def test_d2_3_abs_path_is_sibling_of_host_root(self, tmp_path):
        """D2.3: abs_path 是 host_real_root 的兄弟路径 → path_escape"""
        sibling = tmp_path.parent / "sibling_dir"
        sibling.mkdir(exist_ok=True)
        try:
            with pytest.raises(RuntimeError, match="path_escape"):
                rust.check_path_within_workspace(str(sibling), str(tmp_path))
        finally:
            sibling.rmdir()

    def test_d2_4_abs_path_outside_host_root(self, tmp_path):
        """D2.4: abs_path 完全在 host_real_root 之外 → path_escape"""
        outside = tmp_path.parent / "completely_outside"
        outside.mkdir(exist_ok=True)
        try:
            with pytest.raises(RuntimeError, match="path_escape"):
                rust.check_path_within_workspace(str(outside), str(tmp_path))
        finally:
            outside.rmdir()

    def test_d2_5_nested_file_within_workspace(self, tmp_path):
        """D2.5: workspace 内的嵌套文件 → 通过"""
        nested_dir = tmp_path / "a" / "b" / "c"
        nested_dir.mkdir(parents=True)
        nested_file = nested_dir / "file.txt"
        nested_file.write_text("nested")
        rust.check_path_within_workspace(str(nested_file), str(tmp_path))


# ============================================
# D3: is_admin_uid / current_daemon_uid_py
# ============================================

class TestIsAdminUidDiff:
    """验证 Rust is_admin_uid / current_daemon_uid_py 行为。

    Python 真相源：server/daemon_server.py:_is_admin_peer / _current_uid
    """

    def test_d3_1_root_uid_is_admin(self):
        """D3.1: uid=0（root）→ true"""
        assert rust.is_admin_uid(0) is True

    def test_d3_2_daemon_self_is_admin(self):
        """D3.2: uid=current_daemon_uid() → true"""
        daemon_uid = rust.current_daemon_uid_py()
        assert rust.is_admin_uid(daemon_uid) is True

    def test_d3_3_random_uid_not_admin(self):
        """D3.3: uid=99999 → false"""
        daemon_uid = rust.current_daemon_uid_py()
        if daemon_uid != 99999:  # 确保不是 daemon 自己
            assert rust.is_admin_uid(99999) is False

    def test_d3_4_current_daemon_uid_returns_positive(self):
        """D3.4: current_daemon_uid_py() 返回正整数"""
        uid = rust.current_daemon_uid_py()
        assert isinstance(uid, int)
        assert uid >= 0


# ============================================
# D4: check_workspace_owner
# ============================================

class TestCheckWorkspaceOwnerDiff:
    """验证 Rust check_workspace_owner 行为。

    Python 真相源：server/daemon_server.py:_owned_workspace 内部比较逻辑
    """

    def test_d4_1_owner_matches_peer(self):
        """D4.1: owner_uid == peer_uid → 通过"""
        rust.check_workspace_owner(1000, 1000)

    def test_d4_2_root_peer_bypasses_check(self):
        """D4.2: peer_uid == 0（root）→ 通过"""
        rust.check_workspace_owner(1000, 0)

    def test_d4_3_owner_mismatch_forbidden(self):
        """D4.3: owner_uid != peer_uid → workspace_forbidden"""
        with pytest.raises(RuntimeError, match="workspace_forbidden"):
            rust.check_workspace_owner(1000, 2000)

    def test_d4_4_root_owner_with_non_root_peer(self):
        """D4.4: owner_uid=0, peer_uid=1000 → workspace_forbidden"""
        with pytest.raises(RuntimeError, match="workspace_forbidden"):
            rust.check_workspace_owner(0, 1000)


# ============================================
# D5: budget_create / budget_preset
# ============================================

class TestBudgetCreateDiff:
    """验证 Rust budget_create / budget_preset 与 Python QueryBudget 一致。

    Python 真相源：server/query_budget.py:QueryBudget / default_budget / deep_budget /
    shallow_budget / unlimited_budget
    """

    def _budget_to_dict(self, budget: QueryBudget) -> dict:
        """将 Python QueryBudget 转为 dict 用于比较"""
        return {
            "max_depth": budget.max_depth,
            "max_nodes": budget.max_nodes,
            "timeout_ms": budget.timeout_ms,
            "max_results": budget.max_results,
            "frontier_limit": budget.frontier_limit,
        }

    def test_d5_1_default_budget_matches(self):
        """D5.1: budget_create() 默认参数 == Python default_budget()"""
        rust_budget = dict(rust.budget_create())
        py_budget = self._budget_to_dict(default_budget())
        assert rust_budget == py_budget

    def test_d5_2_custom_budget(self):
        """D5.2: budget_create(max_depth=10) 自定义参数"""
        rust_budget = dict(rust.budget_create(max_depth=10))
        assert rust_budget["max_depth"] == 10
        assert rust_budget["max_nodes"] == 1000  # 默认值
        assert rust_budget["timeout_ms"] == 5000  # 默认值

    def test_d5_3_preset_default(self):
        """D5.3: budget_preset("default") == Python default_budget()"""
        rust_budget = dict(rust.budget_preset("default"))
        py_budget = self._budget_to_dict(default_budget())
        assert rust_budget == py_budget

    def test_d5_4_preset_deep(self):
        """D5.4: budget_preset("deep") == Python deep_budget()"""
        rust_budget = dict(rust.budget_preset("deep"))
        py_budget = self._budget_to_dict(deep_budget())
        assert rust_budget == py_budget

    def test_d5_5_preset_shallow(self):
        """D5.5: budget_preset("shallow") == Python shallow_budget()"""
        rust_budget = dict(rust.budget_preset("shallow"))
        py_budget = self._budget_to_dict(shallow_budget())
        assert rust_budget == py_budget

    def test_d5_6_preset_unlimited(self):
        """D5.6: budget_preset("unlimited") == Python unlimited_budget()"""
        rust_budget = dict(rust.budget_preset("unlimited"))
        py_budget = self._budget_to_dict(unlimited_budget())
        assert rust_budget == py_budget

    def test_d5_7_preset_invalid(self):
        """D5.7: budget_preset("invalid") → unknown_preset"""
        with pytest.raises(RuntimeError, match="unknown_preset"):
            rust.budget_preset("invalid")

    def test_d5_8_all_fields_present(self):
        """D5.8: budget_create 返回 dict 包含所有 5 个字段"""
        budget = dict(rust.budget_create())
        assert "max_depth" in budget
        assert "max_nodes" in budget
        assert "timeout_ms" in budget
        assert "max_results" in budget
        assert "frontier_limit" in budget


# ============================================
# D6: budget_tracker 行为
# ============================================

class TestBudgetTrackerDiff:
    """验证 Rust budget_tracker 行为与 Python QueryBudget 运行时一致。

    Python 真相源：server/query_budget.py:QueryBudget.visit_node / truncate_results
    """

    def test_d6_1_visit_node_under_limit(self):
        """D6.1: visit_node 未超限 → true，visited_count=1"""
        budget = rust.budget_create(max_nodes=10, timeout_ms=5000)
        tracker = rust.budget_tracker_new(budget)
        result = rust.budget_tracker_visit_node(tracker)
        assert result is True
        assert tracker["visited_count"] == 1

    def test_d6_2_visit_node_exceeds_max_nodes(self):
        """D6.2: visit_node 超过 max_nodes → false，exhausted_reason=max_nodes"""
        budget = rust.budget_create(max_nodes=2, timeout_ms=5000)
        tracker = rust.budget_tracker_new(budget)

        # 访问 2 个节点（未超限）
        assert rust.budget_tracker_visit_node(tracker) is True
        assert rust.budget_tracker_visit_node(tracker) is True

        # 第 3 个节点超限
        assert rust.budget_tracker_visit_node(tracker) is False
        assert tracker["exceeded"] is True
        assert tracker["exhausted_reason"] == "max_nodes"

    def test_d6_3_visit_node_after_exceeded(self):
        """D6.3: 超限后继续 visit_node → false"""
        budget = rust.budget_create(max_nodes=1, timeout_ms=5000)
        tracker = rust.budget_tracker_new(budget)

        rust.budget_tracker_visit_node(tracker)  # 1 个节点
        rust.budget_tracker_visit_node(tracker)  # 超限

        # 继续访问应该直接返回 false
        assert rust.budget_tracker_visit_node(tracker) is False

    def test_d6_4_truncate_results_over_limit(self):
        """D6.4: truncate_results 超过 max_results → 截断"""
        budget = rust.budget_create(max_results=3)
        tracker = rust.budget_tracker_new(budget)
        results = list(range(10))

        truncated = rust.budget_tracker_truncate_results(tracker, results)
        assert list(truncated) == [0, 1, 2]

    def test_d6_5_truncate_results_under_limit(self):
        """D6.5: truncate_results 未超 max_results → 原样返回"""
        budget = rust.budget_create(max_results=100)
        tracker = rust.budget_tracker_new(budget)
        results = list(range(5))

        truncated = rust.budget_tracker_truncate_results(tracker, results)
        assert list(truncated) == [0, 1, 2, 3, 4]

    def test_d6_6_tracker_initial_state(self):
        """D6.6: tracker 初始状态验证"""
        budget = rust.budget_create()
        tracker = rust.budget_tracker_new(budget)

        assert tracker["visited_count"] == 0
        assert tracker["exceeded"] is False
        assert tracker["exhausted_reason"] is None
        assert "start_time" in tracker

    def test_d6_7_tracker_copies_budget_fields(self):
        """D6.7: tracker 复制了 budget 的所有字段"""
        budget = rust.budget_create(max_depth=7, max_nodes=70, timeout_ms=7000,
                                    max_results=70, frontier_limit=700)
        tracker = rust.budget_tracker_new(budget)

        assert tracker["max_depth"] == 7
        assert tracker["max_nodes"] == 70
        assert tracker["timeout_ms"] == 7000
        assert tracker["max_results"] == 70
        assert tracker["frontier_limit"] == 700

    def test_d6_8_visit_node_count_increments(self):
        """D6.8: 多次 visit_node，visited_count 递增"""
        budget = rust.budget_create(max_nodes=100, timeout_ms=5000)
        tracker = rust.budget_tracker_new(budget)

        for i in range(5):
            rust.budget_tracker_visit_node(tracker)

        assert tracker["visited_count"] == 5
        assert tracker["exceeded"] is False
