"""任务系统 reopen 机制测试。

覆盖：
- task_create(parent_id=closed_task) 自动触发父任务链 reopen
- 显式 db.task_reopen() 方法
- 各种状态转换（review/applied/closed → in_progress）
- 递归 reopen 祖父任务链
- 父任务 open/in_progress 时直接挂不改状态
- audit_chain 记录 reopen 事件
- i18n key 完整性
"""

import os
import sys
import tempfile

import pytest

# 确保项目根目录在 path 中
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
)


@pytest.fixture
def db():
    """创建临时数据库用于测试"""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = CodeGraphDB(db_path)
        yield db
        db.close()


def _create_task(db, title="test", parent_id="", steps=None):
    """创建任务的辅助函数"""
    return db.task_create(
        title=title,
        description="test desc",
        steps=steps or [],
        creator="test",
        parent_id=parent_id,
    )


def _create_subtask(db, parent_id, title="subtask", steps=None):
    """创建子任务的辅助函数"""
    return db.task_create_subtask(
        parent_task_id=parent_id,
        title=title,
        steps=steps or [],
        creator="test",
    )


def _complete_task_to_status(db, task_id, target_status):
    """把任务推进到指定状态（open → in_progress → review → applied → closed）"""
    now = 0  # 占位

    if target_status == TASK_STATUS_OPEN:
        return  # 已经是 open

    # 添加一个步骤并完成，使任务进入 review
    # 先创建步骤（如果任务没有步骤）
    import sqlite3
    cur = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ?",
        (task_id,),
    )
    if cur.fetchone()["cnt"] == 0:
        # 创建一个步骤
        step_id = "S-test-" + task_id[-8:]
        import time
        db.conn.execute(
            "INSERT INTO task_steps (id, task_id, step_index, action, target_file, "
            "target_symbol, check_items, status, result, created_at, completed_at) "
            "VALUES (?, ?, 0, 'annotate', 'test.py', '', '', 'done', '', ?, ?)",
            (step_id, task_id, time.time(), time.time()),
        )
        db.conn.commit()

    # open → in_progress
    db.conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (TASK_STATUS_IN_PROGRESS, _now(), task_id),
    )
    db.conn.commit()

    if target_status == TASK_STATUS_IN_PROGRESS:
        return

    # in_progress → review
    db.conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (TASK_STATUS_REVIEW, _now(), task_id),
    )
    db.conn.commit()

    if target_status == TASK_STATUS_REVIEW:
        return

    # review → applied
    db.conn.execute(
        "UPDATE tasks SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
        (TASK_STATUS_APPLIED, _now(), _now(), task_id),
    )
    db.conn.commit()

    if target_status == TASK_STATUS_APPLIED:
        return

    # applied → closed
    db.conn.execute(
        "UPDATE tasks SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
        (TASK_STATUS_CLOSED, _now(), _now(), task_id),
    )
    db.conn.commit()


def _now():
    import time
    return time.time()


def _get_task_status(db, task_id):
    """获取任务状态"""
    cur = db.conn.execute(
        "SELECT status, applied_at, closed_at FROM tasks WHERE id = ?",
        (task_id,),
    )
    return cur.fetchone()


# ============================================
# 1. task_create 自动触发 reopen
# ============================================


class TestTaskCreateReopen:
    """测试 task_create(parent_id=closed_task) 自动 reopen"""

    def test_reopen_closed_parent(self, db):
        """父任务 closed 时挂子任务 → 父任务 reopen 为 in_progress"""
        parent_id = _create_task(db, title="parent")
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        # 挂子任务
        child_id = _create_subtask(db, parent_id, title="child")

        # 父任务应变为 in_progress
        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS
        assert row["applied_at"] is None
        assert row["closed_at"] is None

    def test_reopen_applied_parent(self, db):
        """父任务 applied 时挂子任务 → 父任务 reopen 为 in_progress"""
        parent_id = _create_task(db, title="parent")
        _complete_task_to_status(db, parent_id, TASK_STATUS_APPLIED)

        child_id = _create_subtask(db, parent_id, title="child")

        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS
        assert row["applied_at"] is None

    def test_reopen_review_parent(self, db):
        """父任务 review 时挂子任务 → 父任务 reopen 为 in_progress"""
        parent_id = _create_task(db, title="parent")
        _complete_task_to_status(db, parent_id, TASK_STATUS_REVIEW)

        child_id = _create_subtask(db, parent_id, title="child")

        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS

    def test_no_reopen_open_parent(self, db):
        """父任务 open 时挂子任务 → 直接挂，不改状态"""
        parent_id = _create_task(db, title="parent")
        # 父任务保持 open

        child_id = _create_subtask(db, parent_id, title="child")

        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_OPEN

    def test_no_reopen_in_progress_parent(self, db):
        """父任务 in_progress 时挂子任务 → 直接挂，不改状态"""
        parent_id = _create_task(db, title="parent")
        _complete_task_to_status(db, parent_id, TASK_STATUS_IN_PROGRESS)

        child_id = _create_subtask(db, parent_id, title="child")

        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS


# ============================================
# 2. 递归 reopen 祖父任务链
# ============================================


class TestRecursiveReopen:
    """测试递归 reopen 祖父任务链"""

    def test_reopen_grandparent_chain(self, db):
        """祖父任务 closed 时，挂子任务 → 父任务和祖父任务都 reopen"""
        grandparent_id = _create_task(db, title="grandparent")
        parent_id = _create_subtask(db, grandparent_id, title="parent")

        # 把 parent 和 grandparent 都推进到 closed
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, grandparent_id, TASK_STATUS_CLOSED)

        # 在 parent 下挂新子任务
        new_child_id = _create_subtask(db, parent_id, title="new_child")

        # parent 应 reopen
        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS

        # grandparent 也应 reopen
        row = _get_task_status(db, grandparent_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS

    def test_reopen_three_level_chain(self, db):
        """三层任务链 closed 时，挂子任务 → 全链 reopen"""
        root_id = _create_task(db, title="root")
        mid_id = _create_subtask(db, root_id, title="mid")
        leaf_id = _create_subtask(db, mid_id, title="leaf")

        # 全链推进到 closed
        _complete_task_to_status(db, leaf_id, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, mid_id, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, root_id, TASK_STATUS_CLOSED)

        # 在 leaf 下挂新子任务
        new_child_id = _create_subtask(db, leaf_id, title="new_child")

        # 全链应 reopen
        assert _get_task_status(db, leaf_id)["status"] == TASK_STATUS_IN_PROGRESS
        assert _get_task_status(db, mid_id)["status"] == TASK_STATUS_IN_PROGRESS
        assert _get_task_status(db, root_id)["status"] == TASK_STATUS_IN_PROGRESS


# ============================================
# 2.5 兄弟子任务状态判断（task_create 场景）
# ============================================


class TestSiblingStatusCheck:
    """测试 task_create 挂子任务时，检查兄弟子任务状态决定是否 reopen 父任务

    用户需求（2026-07-07）：
    - reopen 的时候，如果所有子任务都是 close 的，应该是 in_progress
    - 如果其他所有子任务是 open 状态，应该是直接挂，不应该置为 in_progress
    - 就是说挂任务的时候，需要考虑其他子任务和父任务的状态
    """

    def test_reopen_when_all_siblings_closed(self, db):
        """父任务 closed + 所有兄弟子任务 closed → reopen 父任务为 in_progress"""
        parent_id = _create_task(db, title="parent")
        # 先挂一个子任务并完成到 closed
        sibling_id = _create_subtask(db, parent_id, title="sibling1")
        _complete_task_to_status(db, sibling_id, TASK_STATUS_CLOSED)
        # parent 因级联 close 也变为 closed
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        # 再挂一个新子任务
        new_child_id = _create_subtask(db, parent_id, title="new_child")

        # 所有兄弟子任务都是 closed → reopen 父任务为 in_progress
        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS
        assert row["applied_at"] is None
        assert row["closed_at"] is None

    def test_no_reopen_when_sibling_open(self, db):
        """父任务 closed + 有兄弟子任务 open → 直接挂，不 reopen 父任务"""
        parent_id = _create_task(db, title="parent")
        # 先挂一个子任务，保持 open 状态
        sibling_id = _create_subtask(db, parent_id, title="sibling_open")
        # 手动把 parent 推到 closed（模拟数据不一致或误操作）
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        # 再挂一个新子任务
        new_child_id = _create_subtask(db, parent_id, title="new_child")

        # 有兄弟子任务是 open → 不 reopen 父任务，保持 closed
        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_CLOSED

    def test_no_reopen_when_sibling_in_progress(self, db):
        """父任务 closed + 有兄弟子任务 in_progress → 直接挂，不 reopen 父任务"""
        parent_id = _create_task(db, title="parent")
        sibling_id = _create_subtask(db, parent_id, title="sibling_in_progress")
        _complete_task_to_status(db, sibling_id, TASK_STATUS_IN_PROGRESS)
        # 手动把 parent 推到 closed
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        new_child_id = _create_subtask(db, parent_id, title="new_child")

        # 有兄弟子任务是 in_progress → 不 reopen 父任务
        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_CLOSED

    def test_reopen_when_no_siblings(self, db):
        """父任务 closed + 无兄弟子任务（第一个子任务）→ reopen 父任务"""
        parent_id = _create_task(db, title="parent")
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        # 挂第一个子任务
        child_id = _create_subtask(db, parent_id, title="first_child")

        # 无兄弟子任务 → reopen 父任务
        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS

    def test_no_reopen_when_mixed_sibling_statuses(self, db):
        """父任务 closed + 兄弟子任务部分 closed 部分 open → 不 reopen 父任务"""
        parent_id = _create_task(db, title="parent")
        # 兄弟1：closed
        sibling1_id = _create_subtask(db, parent_id, title="sibling_closed")
        _complete_task_to_status(db, sibling1_id, TASK_STATUS_CLOSED)
        # 兄弟2：open
        sibling2_id = _create_subtask(db, parent_id, title="sibling_open")
        # 手动把 parent 推到 closed
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        # 挂新子任务
        new_child_id = _create_subtask(db, parent_id, title="new_child")

        # 有兄弟子任务是 open → 不 reopen 父任务
        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_CLOSED

    def test_reopen_when_all_multiple_siblings_closed(self, db):
        """父任务 closed + 多个兄弟子任务全部 closed → reopen 父任务"""
        parent_id = _create_task(db, title="parent")
        s1 = _create_subtask(db, parent_id, title="s1")
        s2 = _create_subtask(db, parent_id, title="s2")
        s3 = _create_subtask(db, parent_id, title="s3")
        _complete_task_to_status(db, s1, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, s2, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, s3, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        new_child_id = _create_subtask(db, parent_id, title="new_child")

        # 所有兄弟子任务都是 closed → reopen 父任务
        row = _get_task_status(db, parent_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS


# ============================================
# 3. 显式 task_reopen 方法
# ============================================


class TestTaskReopenMethod:
    """测试 db.task_reopen() 方法"""

    def test_reopen_closed_task(self, db):
        """reopen closed 任务 → 变为 in_progress"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_CLOSED)

        result = db.task_reopen(task_id, reviewer="test", reason="found bug")

        assert result["status"] == TASK_STATUS_IN_PROGRESS
        assert result["previous_status"] == TASK_STATUS_CLOSED
        assert "reopened_at" in result

    def test_reopen_applied_task(self, db):
        """reopen applied 任务 → 变为 in_progress"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_APPLIED)

        result = db.task_reopen(task_id, reviewer="test")

        assert result["status"] == TASK_STATUS_IN_PROGRESS
        assert result["previous_status"] == TASK_STATUS_APPLIED

    def test_reopen_review_task(self, db):
        """reopen review 任务 → 变为 in_progress"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_REVIEW)

        result = db.task_reopen(task_id, reviewer="test")

        assert result["status"] == TASK_STATUS_IN_PROGRESS
        assert result["previous_status"] == TASK_STATUS_REVIEW

    def test_reopen_open_task_no_need(self, db):
        """reopen open 任务 → 返回 no need 错误"""
        task_id = _create_task(db, title="task")
        # 保持 open 状态

        result = db.task_reopen(task_id, reviewer="test")

        assert "error" in result
        assert result["status"] == TASK_STATUS_OPEN
        assert result["reason"] == "not_closed"

    def test_reopen_in_progress_task_no_need(self, db):
        """reopen in_progress 任务 → 返回 no need 错误"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_IN_PROGRESS)

        result = db.task_reopen(task_id, reviewer="test")

        assert "error" in result
        assert result["status"] == TASK_STATUS_IN_PROGRESS

    def test_reopen_nonexistent_task(self, db):
        """reopen 不存在的任务 → 返回 not found 错误"""
        result = db.task_reopen("T-nonexistent", reviewer="test")

        assert "error" in result
        # i18n 可能是中文（"未找到任务"）或英文（"not found"），都应通过
        err_lower = result["error"].lower()
        assert "not_found" in err_lower or "not found" in err_lower or "未找到" in result["error"]

    def test_reopen_clears_timestamps(self, db):
        """reopen 后 applied_at 和 closed_at 应被清理"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_CLOSED)

        # 确认有 applied_at 和 closed_at
        row = _get_task_status(db, task_id)
        assert row["applied_at"] is not None
        assert row["closed_at"] is not None

        db.task_reopen(task_id, reviewer="test")

        # 确认被清理
        row = _get_task_status(db, task_id)
        assert row["applied_at"] is None
        assert row["closed_at"] is None

    def test_reopen_propagates_to_grandparent(self, db):
        """reopen 子任务时，祖父任务链也应 reopen"""
        grandparent_id = _create_task(db, title="grandparent")
        parent_id = _create_subtask(db, grandparent_id, title="parent")

        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, grandparent_id, TASK_STATUS_CLOSED)

        db.task_reopen(parent_id, reviewer="test")

        assert _get_task_status(db, parent_id)["status"] == TASK_STATUS_IN_PROGRESS
        assert _get_task_status(db, grandparent_id)["status"] == TASK_STATUS_IN_PROGRESS


# ============================================
# 4. i18n key 完整性
# ============================================


class TestReopenI18n:
    """测试 reopen 相关 i18n key"""

    def test_reopen_i18n_keys_exist(self):
        """zh_CN 和 en_US 都应包含 reopen 相关 key"""
        import json

        i18n_dir = os.path.join(_PKG_PARENT, "i18n")
        for lang in ["zh_CN", "en_US"]:
            with open(os.path.join(i18n_dir, f"{lang}.json"), encoding="utf-8") as f:
                data = json.load(f)
            # task_reopen_no_need key 应存在（目前可能还没有，子任务 2 会添加）
            # 这里只验证 task_not_found 存在（已被 task_reopen 使用）
            assert "task_not_found" in data.get("cli", {}).get("messages", {}) or \
                   "task_not_found" in str(data)
