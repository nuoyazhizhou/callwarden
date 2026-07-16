"""active_task 持久化机制测试。

测试 set_active_task / get_active_task / clear_active_task 三个方法，
以及 task_next_step / task_close / task_reopen 中的自动调用，
还有 task_capture_diff_auto 优先读 active_task 的行为。

背景：替代 CALLWARDEN_TASK_ID 环境变量，让 task_capture_diff_auto 不再
依赖用户手动 export，默认安装即开即用。
"""

import os
import sys
import tempfile
from unittest.mock import patch

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
        # 注册 workspace 并设为 active
        db.register_workspace("test-ws", tmpdir)
        db.set_active_workspace("test-ws")
        yield db
        db.close()


# ============================================
# 1. set/get/clear 基本读写
# ============================================


class TestSetGetClear:
    """set_active_task / get_active_task / clear_active_task 基本读写"""

    def test_get_active_task_initial_none(self, db):
        """初始状态无 active task"""
        assert db.get_active_task() is None

    def test_set_and_get(self, db):
        """设置后能读到"""
        db.set_active_task("T-1234-abcd")
        assert db.get_active_task() == "T-1234-abcd"

    def test_set_overwrites(self, db):
        """重复 set 覆盖旧值"""
        db.set_active_task("T-1111-aaaa")
        db.set_active_task("T-2222-bbbb")
        assert db.get_active_task() == "T-2222-bbbb"

    def test_clear_empty_string(self, db):
        """clear_active_task() 无条件清除"""
        db.set_active_task("T-1111-aaaa")
        db.clear_active_task()
        assert db.get_active_task() is None

    def test_clear_with_matching_task_id(self, db):
        """clear_active_task(task_id) 防御性清除：匹配时清除"""
        db.set_active_task("T-1111-aaaa")
        db.clear_active_task("T-1111-aaaa")
        assert db.get_active_task() is None

    def test_clear_with_non_matching_task_id(self, db):
        """clear_active_task(task_id) 防御性清除：不匹配时不清除"""
        db.set_active_task("T-1111-aaaa")
        db.clear_active_task("T-9999-zzzz")
        # 不匹配，不应清除
        assert db.get_active_task() == "T-1111-aaaa"


# ============================================
# 2. task_next_step 自动设置 active_task
# ============================================


class TestTaskNextStepSetsActiveTask:
    """task_next_step 进入 in_progress 后自动设置 active_task"""

    def test_task_next_sets_active(self, db):
        """task_next_step 后 get_active_task 返回 task_id"""
        task_id = db.task_create(
            title="test task",
            steps=[{"action": "annotate", "target_file": "a.py"}],
        )
        db.task_next_step(task_id)
        assert db.get_active_task() == task_id

    def test_task_next_overwrites_previous(self, db):
        """连续 claim 多个任务时，active_task 覆盖为最新的"""
        t1 = db.task_create(title="t1", steps=[{"action": "annotate"}])
        t2 = db.task_create(title="t2", steps=[{"action": "annotate"}])
        db.task_next_step(t1)
        assert db.get_active_task() == t1
        db.task_next_step(t2)
        assert db.get_active_task() == t2


# ============================================
# 3. task_close 自动清除 active_task
# ============================================


class TestTaskCloseClearsActiveTask:
    """task_close 后自动清除 active_task（防御性匹配）"""

    def test_task_close_clears_active(self, db):
        """close 后 active_task 被清除"""
        task_id = db.task_create(
            title="t",
            steps=[{"action": "annotate"}],
        )
        step = db.task_next_step(task_id)
        assert db.get_active_task() == task_id

        # 推进到 review → applied → closed
        db.task_report_step(task_id, step["step_id"], result="done")
        db.task_apply(task_id)
        db.task_close(task_id)
        assert db.get_active_task() is None

    def test_task_close_does_not_clear_other(self, db):
        """close 一个任务不影响另一个 active_task"""
        t1 = db.task_create(title="t1", steps=[{"action": "annotate"}])
        t2 = db.task_create(title="t2", steps=[{"action": "annotate"}])
        # claim t1 完成（active_task = t1）
        step1 = db.task_next_step(t1)
        db.task_report_step(t1, step1["step_id"], result="done")
        db.task_apply(t1)
        # claim t2（active_task 覆盖为 t2）
        db.task_next_step(t2)
        assert db.get_active_task() == t2
        # close t1（不匹配 active_task=t2，不应清除 t2）
        db.task_close(t1)
        assert db.get_active_task() == t2


# ============================================
# 4. task_reopen 自动设置 active_task
# ============================================


class TestTaskReopenSetsActiveTask:
    """task_reopen 后自动设置 active_task"""

    def test_task_reopen_sets_active(self, db):
        """reopen 后 active_task 被设置为 reopen 的 task_id"""
        task_id = db.task_create(
            title="t",
            steps=[{"action": "annotate"}],
        )
        # 完整流转到 closed
        step = db.task_next_step(task_id)
        db.task_report_step(task_id, step["step_id"], result="done")
        db.task_apply(task_id)
        db.task_close(task_id)
        assert db.get_active_task() is None

        # reopen
        db.task_reopen(task_id, reason="need fix")
        assert db.get_active_task() == task_id


# ============================================
# 5. task_create / task_apply 不影响 active_task
# ============================================


class TestNoSideEffects:
    """task_create 和 task_apply 不应设置 active_task"""

    def test_task_create_no_active(self, db):
        """task_create 不设置 active_task（用户尚未 claim）"""
        db.task_create(title="t", steps=[{"action": "annotate"}])
        assert db.get_active_task() is None

    def test_task_apply_no_change(self, db):
        """task_apply 不改变 active_task"""
        task_id = db.task_create(
            title="t",
            steps=[{"action": "annotate"}],
        )
        step = db.task_next_step(task_id)
        db.task_report_step(task_id, step["step_id"], result="done")
        # apply 前 active_task = task_id
        assert db.get_active_task() == task_id
        db.task_apply(task_id)
        # apply 后仍为 task_id
        assert db.get_active_task() == task_id


# ============================================
# 6. task_capture_diff_auto 优先读 active_task
# ============================================


class TestCaptureDiffAutoPrefersActiveTask:
    """task_capture_diff_auto 优先从 get_active_task 读取"""

    def test_capture_diff_auto_uses_active_task(self, db):
        """active_task 存在时，capture_diff_auto 使用该 task_id"""
        task_id = db.task_create(
            title="t",
            steps=[{"action": "annotate"}],
        )
        db.task_next_step(task_id)

        # Mock task_capture_diff 来捕获传入的 task_id
        captured_task_id = []

        def mock_capture(task_id, step_id="", base="", dry_run=False, **kwargs):
            # **kwargs 接收 source_commit_hash / skip_quality_review 等新参数
            captured_task_id.append(task_id)
            return {
                "task_id": task_id,
                "step_id": step_id,
                "base": base,
                "dry_run": dry_run,
                "changed_files": [],
                "linked_symbols": [],
                "quality_findings": [],
                "quality_decision": "",
            }

        with patch.object(db, "task_capture_diff", side_effect=mock_capture):
            db.task_capture_diff_auto()

        assert len(captured_task_id) == 1
        assert captured_task_id[0] == task_id

    def test_capture_diff_auto_fallback_to_task_list(self, db):
        """active_task 为空时，fallback 到 task_list 找 in_progress"""
        # 不 claim 任何任务，active_task 为 None
        assert db.get_active_task() is None

        # Mock task_list 返回一个 in_progress 任务
        fake_task = {"task_id": "T-fallback-xxxx", "status": "in_progress"}
        with patch.object(db, "task_list", return_value=[fake_task]):
            with patch.object(db, "task_capture_diff") as mock_capture:
                mock_capture.return_value = {"changed_files": []}
                result = db.task_capture_diff_auto()

        mock_capture.assert_called_once()
        assert mock_capture.call_args.kwargs["task_id"] == "T-fallback-xxxx"

    def test_capture_diff_auto_no_task(self, db):
        """无 active_task 且无 in_progress 任务时返回 no_in_progress_task"""
        assert db.get_active_task() is None
        # Mock task_list 返回空
        with patch.object(db, "task_list", return_value=[]):
            result = db.task_capture_diff_auto()
        assert result["success"] is False
        assert result["reason"] == "no_in_progress_task"

    def test_capture_diff_auto_skips_non_in_progress_task(self, db):
        """active_task 状态为 review/applied/closed 时跳过自动捕获（修复 post-commit hook 卡顿）"""
        task_id = db.task_create(
            title="t",
            steps=[{"action": "annotate"}],
        )
        db.task_next_step(task_id)
        # 模拟任务完成进入 review 状态（post-commit hook 在任务完成后仍会触发）
        db.conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (TASK_STATUS_REVIEW, 1234567890.0, task_id),
        )
        db.conn.commit()
        assert db.get_active_task() == task_id  # active_task 仍指向该任务

        # task_capture_diff 不应被调用
        with patch.object(db, "task_capture_diff") as mock_capture:
            result = db.task_capture_diff_auto()

        mock_capture.assert_not_called()
        assert result["success"] is False
        assert result["reason"] == "task_not_in_progress"
        assert result["task_id"] == task_id
        assert result["next_action"] == "noop"


# ============================================
# 7. Schema v30 迁移幂等性
# ============================================


class TestSchemaV30Migration:
    """Schema v30 迁移幂等性"""

    def test_migration_idempotent(self, db):
        """重复执行迁移不应报错"""
        from callwarden.db.db_base import _migrate_v29_to_v30

        # 再跑一次迁移（已迁移过的数据库）
        _migrate_v29_to_v30(db.conn)

        # 字段仍存在，方法仍可用
        db.set_active_task("T-test-idempotent")
        assert db.get_active_task() == "T-test-idempotent"

    def test_active_task_id_column_exists(self, db):
        """v30 迁移后 active_task_id 字段存在"""
        cur = db.conn.execute("PRAGMA table_info(workspaces)")
        cols = {row[1] for row in cur.fetchall()}
        assert "active_task_id" in cols

    def test_idx_workspaces_active_task_exists(self, db):
        """v30 迁移后 idx_workspaces_active_task 索引存在"""
        cur = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_workspaces_active_task",),
        )
        assert cur.fetchone() is not None
