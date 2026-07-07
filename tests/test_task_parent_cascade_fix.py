"""父任务状态机边缘 bug 修复测试。

修复的 bug：父任务有自身步骤（如 verify），子任务全部 closed 后父任务
自身步骤才完成进入 review，但 task_apply/task_close 拒绝手动操作父任务，
导致父任务卡在 review 状态无法 close。

修复方案：_update_parent_status 把父任务推到 review 后，自动调用
_cascade_close_if_ready 检查是否所有子任务都已 closed，如果是则级联 close。
"""

import os
import sys
import tempfile
import time

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db import CodeGraphDB
from callwarden.db.schema import (
    TASK_STATUS_OPEN,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_REVIEW,
    TASK_STATUS_APPLIED,
    TASK_STATUS_CLOSED,
    STEP_STATUS_PENDING,
    STEP_STATUS_DONE,
)


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = CodeGraphDB(db_path)
        yield db
        db.close()


# ============================================
# 1. 核心 bug 复现：父任务有自身步骤+子任务先全部 closed
# ============================================


class TestParentWithOwnStepsSubtasksClosed:
    """父任务有自身步骤，子任务全部 closed 后父任务步骤才完成"""

    def test_parent_with_verify_step_auto_closes(self, db):
        """父任务有 verify 步骤，子任务全部 closed 后 verify 完成
        → 父任务应自动级联 close（不再卡在 review）"""
        # 创建父任务（有自身步骤）
        parent_id = db.task_create(
            title="parent",
            description="父任务带 verify 步骤",
            steps=[{"action": "verify", "target_file": ""}],
            creator="test",
        )
        # 创建子任务
        child_id = db.task_create(
            title="child",
            description="子任务",
            steps=[{"action": "edit", "target_file": "f.py"}],
            creator="test",
            parent_id=parent_id,
        )

        # 1. 完成子任务的所有步骤
        step = db.task_next_step(child_id)
        assert step is not None
        db.task_report_step(child_id, step["step_id"], result="done", success=True)
        # 子任务进入 review
        # 2. apply 子任务（父任务还在 in_progress，不会级联 close）
        db.task_apply(child_id)
        # 子任务应该是 applied（父任务未 review，不级联 close）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (child_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_APPLIED

        # 3. 此时父任务仍在 open/in_progress（自身 verify 步骤未完成）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        assert cur.fetchone()["status"] in (TASK_STATUS_OPEN, TASK_STATUS_IN_PROGRESS)

        # 4. 完成父任务的 verify 步骤
        parent_step = db.task_next_step(parent_id)
        assert parent_step is not None
        assert parent_step["action"] == "verify"
        db.task_report_step(parent_id, parent_step["step_id"], result="done", success=True)

        # 5. 关键断言：父任务应自动 closed（不再卡在 review）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        parent_status = cur.fetchone()["status"]
        assert parent_status == TASK_STATUS_CLOSED, \
            f"父任务应自动 closed，但状态为 {parent_status}"

    def test_parent_with_multiple_steps_and_multiple_subtasks(self, db):
        """父任务有多个步骤，多个子任务全部 closed 后父任务步骤完成
        → 父任务应自动级联 close"""
        parent_id = db.task_create(
            title="parent",
            steps=[
                {"action": "build", "target_file": ""},
                {"action": "verify", "target_file": ""},
            ],
            creator="test",
        )
        child1 = db.task_create(
            title="child1",
            steps=[{"action": "edit", "target_file": "f1.py"}],
            creator="test",
            parent_id=parent_id,
        )
        child2 = db.task_create(
            title="child2",
            steps=[{"action": "edit", "target_file": "f2.py"}],
            creator="test",
            parent_id=parent_id,
        )

        # 完成两个子任务
        for cid in [child1, child2]:
            step = db.task_next_step(cid)
            db.task_report_step(cid, step["step_id"], result="done")
            db.task_apply(cid)

        # 两个子任务都 applied（父任务有自身步骤未完成，不会级联 close 子任务）
        for cid in [child1, child2]:
            cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (cid,))
            assert cur.fetchone()["status"] == TASK_STATUS_APPLIED

        # 完成父任务的两个步骤
        step1 = db.task_next_step(parent_id)
        db.task_report_step(parent_id, step1["step_id"], result="done")
        # 第一个步骤完成后父任务不应进入 review（还有第二个步骤）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_IN_PROGRESS

        step2 = db.task_next_step(parent_id)
        db.task_report_step(parent_id, step2["step_id"], result="done")
        # 第二个步骤完成后父任务应自动 closed
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_CLOSED

    def test_parent_with_step_before_subtasks_close(self, db):
        """父任务步骤在子任务 close 之前完成
        → 父任务进入 review 但不自动 close（等子任务全 closed 后由 task_apply 触发）"""
        parent_id = db.task_create(
            title="parent",
            steps=[{"action": "verify", "target_file": ""}],
            creator="test",
        )
        child_id = db.task_create(
            title="child",
            steps=[{"action": "edit", "target_file": "f.py"}],
            creator="test",
            parent_id=parent_id,
        )

        # 1. 直接通过 SQL 完成父任务的 verify 步骤（绕过 task_next_step 的深度优先逻辑）
        cur = db.conn.execute(
            "SELECT id FROM task_steps WHERE task_id = ? AND action = ?",
            (parent_id, "verify"),
        )
        parent_step_id = cur.fetchone()["id"]
        db.task_report_step(parent_id, parent_step_id, result="done")
        # 父任务不应 closed（子任务未完成）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        parent_status = cur.fetchone()["status"]
        # 父任务可能还在 in_progress/review（子任务未完成，不自动 close）
        assert parent_status != TASK_STATUS_CLOSED

        # 2. 完成子任务
        child_step = db.task_next_step(child_id)
        db.task_report_step(child_id, child_step["step_id"], result="done")
        db.task_apply(child_id)

        # 3. 子任务 closed 后，父任务应自动 closed（通过 task_apply 级联）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_CLOSED


# ============================================
# 2. 回归测试：父任务无自身步骤
# ============================================


class TestParentWithoutOwnStepsRegression:
    """父任务无自身步骤的回归测试（确保不破坏现有行为）"""

    def test_parent_no_steps_subtasks_close(self, db):
        """无自身步骤的父任务，子任务全部 apply 后级联 close（原有行为）"""
        parent_id = db.task_create(
            title="parent",
            steps=[],  # 无自身步骤
            creator="test",
        )
        child_id = db.task_create(
            title="child",
            steps=[{"action": "edit", "target_file": "f.py"}],
            creator="test",
            parent_id=parent_id,
        )

        # 完成子任务
        step = db.task_next_step(child_id)
        db.task_report_step(child_id, step["step_id"], result="done")
        db.task_apply(child_id)

        # 父任务应自动 closed（级联）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_CLOSED

    def test_parent_no_steps_partial_subtasks(self, db):
        """无自身步骤的父任务，部分子任务未完成时不级联"""
        parent_id = db.task_create(
            title="parent",
            steps=[],
            creator="test",
        )
        child1 = db.task_create(
            title="child1",
            steps=[{"action": "edit", "target_file": "f1.py"}],
            creator="test",
            parent_id=parent_id,
        )
        child2 = db.task_create(
            title="child2",
            steps=[{"action": "edit", "target_file": "f2.py"}],
            creator="test",
            parent_id=parent_id,
        )

        # 只完成 child1
        step = db.task_next_step(child1)
        db.task_report_step(child1, step["step_id"], result="done")
        db.task_apply(child1)

        # 父任务不应 closed（child2 未完成）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        parent_status = cur.fetchone()["status"]
        assert parent_status != TASK_STATUS_CLOSED


# ============================================
# 3. 多层任务树递归级联
# ============================================


class TestMultiLevelCascade:
    """多层任务树（祖父-父-子）递归级联"""

    def test_grandparent_with_steps_cascades(self, db):
        """祖父任务有自身步骤，父任务和子任务都 closed 后祖父自动 close"""
        grandparent_id = db.task_create(
            title="grandparent",
            steps=[{"action": "verify", "target_file": ""}],
            creator="test",
        )
        parent_id = db.task_create(
            title="parent",
            steps=[],  # 父任务无自身步骤
            creator="test",
            parent_id=grandparent_id,
        )
        child_id = db.task_create(
            title="child",
            steps=[{"action": "edit", "target_file": "f.py"}],
            creator="test",
            parent_id=parent_id,
        )

        # 1. 完成子任务
        step = db.task_next_step(child_id)
        db.task_report_step(child_id, step["step_id"], result="done")
        db.task_apply(child_id)

        # 子任务 closed，父任务级联 closed
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_CLOSED

        # 2. 此时祖父任务仍在 open/in_progress（自身 verify 步骤未完成）
        # task_next_step 只更新被调用的任务，不会递归更新祖先链
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (grandparent_id,))
        assert cur.fetchone()["status"] in (TASK_STATUS_OPEN, TASK_STATUS_IN_PROGRESS)

        # 3. 完成祖父任务的 verify 步骤
        gp_step = db.task_next_step(grandparent_id)
        assert gp_step["action"] == "verify"
        db.task_report_step(grandparent_id, gp_step["step_id"], result="done")

        # 4. 祖父任务应自动 closed
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (grandparent_id,))
        gp_status = cur.fetchone()["status"]
        assert gp_status == TASK_STATUS_CLOSED, \
            f"祖父任务应自动 closed，但状态为 {gp_status}"

    def test_three_level_all_with_steps(self, db):
        """三层都有自身步骤的任务树，子任务先全部 closed"""
        grandparent_id = db.task_create(
            title="grandparent",
            steps=[{"action": "verify", "target_file": ""}],
            creator="test",
        )
        parent_id = db.task_create(
            title="parent",
            steps=[{"action": "test", "target_file": ""}],
            creator="test",
            parent_id=grandparent_id,
        )
        child_id = db.task_create(
            title="child",
            steps=[{"action": "edit", "target_file": "f.py"}],
            creator="test",
            parent_id=parent_id,
        )

        # 1. 完成子任务 → applied（父任务有自身步骤未完成，不级联 close）
        step = db.task_next_step(child_id)
        db.task_report_step(child_id, step["step_id"], result="done")
        db.task_apply(child_id)
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (child_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_APPLIED

        # 2. 完成父任务的 test 步骤 → 父任务自动 closed
        p_step = db.task_next_step(parent_id)
        db.task_report_step(parent_id, p_step["step_id"], result="done")
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_CLOSED

        # 3. 完成祖父任务的 verify 步骤 → 祖父自动 closed
        gp_step = db.task_next_step(grandparent_id)
        db.task_report_step(grandparent_id, gp_step["step_id"], result="done")
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (grandparent_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_CLOSED


# ============================================
# 4. 边界情况
# ============================================


class TestEdgeCases:
    """边界情况测试"""

    def test_parent_step_fails_not_closed(self, db):
        """父任务步骤失败时不应自动 close"""
        parent_id = db.task_create(
            title="parent",
            steps=[{"action": "verify", "target_file": ""}],
            creator="test",
        )
        child_id = db.task_create(
            title="child",
            steps=[{"action": "edit", "target_file": "f.py"}],
            creator="test",
            parent_id=parent_id,
        )

        # 完成子任务
        step = db.task_next_step(child_id)
        db.task_report_step(child_id, step["step_id"], result="done")
        db.task_apply(child_id)

        # 父任务步骤失败
        parent_step = db.task_next_step(parent_id)
        db.task_report_step(parent_id, parent_step["step_id"], result="fail", success=False)

        # 父任务不应 closed
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        assert cur.fetchone()["status"] != TASK_STATUS_CLOSED

    def test_leaf_task_not_affected(self, db):
        """叶子任务（无子任务）不受此修复影响"""
        task_id = db.task_create(
            title="leaf",
            steps=[{"action": "edit", "target_file": "f.py"}],
            creator="test",
        )
        step = db.task_next_step(task_id)
        db.task_report_step(task_id, step["step_id"], result="done")
        # 叶子任务进入 review（不自动 close）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_REVIEW

    def test_parent_with_some_subtasks_not_closed(self, db):
        """父任务有自身步骤，部分子任务未 closed 时父任务步骤完成
        → 父任务进入 review 但不自动 close"""
        parent_id = db.task_create(
            title="parent",
            steps=[{"action": "verify", "target_file": ""}],
            creator="test",
        )
        child1 = db.task_create(
            title="child1",
            steps=[{"action": "edit", "target_file": "f1.py"}],
            creator="test",
            parent_id=parent_id,
        )
        child2 = db.task_create(
            title="child2",
            steps=[{"action": "edit", "target_file": "f2.py"}],
            creator="test",
            parent_id=parent_id,
        )

        # 只完成 child1
        step = db.task_next_step(child1)
        db.task_report_step(child1, step["step_id"], result="done")
        db.task_apply(child1)

        # 完成父任务 verify 步骤
        parent_step = db.task_next_step(parent_id)
        db.task_report_step(parent_id, parent_step["step_id"], result="done")

        # 父任务不应 closed（child2 未完成）
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        parent_status = cur.fetchone()["status"]
        assert parent_status != TASK_STATUS_CLOSED, \
            f"child2 未 closed，父任务不应 closed，但状态为 {parent_status}"
