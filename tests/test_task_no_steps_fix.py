"""无 steps 任务的状态机修复测试。

修复场景：task_split 创建的子任务如果 plan 中没有列表项，就不会创建 steps，
导致任务卡在 open 状态无法 apply/close。

修复方案：task_apply 和 task_close 检测到无 steps 的叶子任务时，
允许从 open/in_progress 状态自动推进到 review/applied，完成状态机流转。

测试内容：
- 无 steps 任务可以从 open 直接 apply
- 无 steps 任务可以从 open 直接 close
- 有 steps 任务仍受状态机约束（不能从 open 直接 apply）
- 无 steps 的父任务仍禁止手动 apply/close（必须级联触发）
- task_split 创建的无 steps 子任务可正常 apply + close
"""

import os
import tempfile

from callwarden.db.db import CodeGraphDB
from callwarden.db.schema import (
    TASK_STATUS_OPEN,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_REVIEW,
    TASK_STATUS_APPLIED,
    TASK_STATUS_CLOSED,
)


def _db_with_workspace():
    """构造临时工作区数据库。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def test_apply_no_steps_task_from_open():
    """无 steps 的任务可以从 open 直接 apply（自动推进 open → review → applied）。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(
            title="无 steps 任务",
            description="task_split 创建的子任务可能没有 steps",
            steps=[],
        )
        # 确认任务状态是 open 且无 steps
        assert _get_status(db, task_id) == TASK_STATUS_OPEN
        assert _step_count(db, task_id) == 0

        # apply 应该成功（自动推进 open → review → applied）
        result = db.task_apply(task_id, reviewer="test")
        assert "error" not in result, f"apply 失败: {result}"
        assert result["status"] == TASK_STATUS_APPLIED
        assert _get_status(db, task_id) == TASK_STATUS_APPLIED
    finally:
        db.close()


def test_close_no_steps_task_from_open():
    """无 steps 的任务可以从 open 直接 close（自动推进 open → applied → closed）。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(
            title="无 steps 任务",
            description="直接 close",
            steps=[],
        )
        assert _get_status(db, task_id) == TASK_STATUS_OPEN

        # close 应该成功（自动推进 open → applied → closed）
        result = db.task_close(task_id, reviewer="test")
        assert "error" not in result, f"close 失败: {result}"
        assert result["status"] == TASK_STATUS_CLOSED
        assert _get_status(db, task_id) == TASK_STATUS_CLOSED
    finally:
        db.close()


def test_apply_with_steps_task_from_open_still_rejected():
    """有 steps 的任务不能从 open 直接 apply（仍受状态机约束）。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(
            title="有 steps 任务",
            description="必须走完整状态机",
            steps=[{"action": "annotate", "target_file": "a.py"}],
        )
        assert _get_status(db, task_id) == TASK_STATUS_OPEN
        assert _step_count(db, task_id) == 1

        # apply 应该被拒绝（有 steps，状态不是 review）
        result = db.task_apply(task_id, reviewer="test")
        assert "error" in result, "应该拒绝有 steps 的 open 任务 apply"
        assert _get_status(db, task_id) == TASK_STATUS_OPEN
    finally:
        db.close()


def test_apply_parent_task_with_subtasks_still_rejected():
    """无 steps 的父任务（有子任务）仍禁止手动 apply。"""
    db, _root = _db_with_workspace()
    try:
        parent_id = db.task_create(title="父任务", steps=[])
        sub_id = db.task_create_subtask(parent_id, title="子任务", steps=[])
        assert _get_status(db, parent_id) == TASK_STATUS_OPEN

        # 父任务有子任务，即使无 steps 也禁止手动 apply
        result = db.task_apply(parent_id, reviewer="test")
        assert "error" in result
        assert "parent" in result.get("reason", "").lower() or "parent" in result.get("error", "").lower()
    finally:
        db.close()


def test_split_created_no_steps_subtask_can_apply_and_close():
    """task_split 创建的无 steps 子任务可以正常 apply + close。"""
    db, _root = _db_with_workspace()
    try:
        parent_id = db.task_create(title="父任务", steps=[])

        # 用 task_split 创建无 steps 的子任务
        subtasks_def = [
            {"title": "子任务A（无 steps）", "description": "补录任务", "steps": []},
            {"title": "子任务B（无 steps）", "description": "补录任务", "steps": []},
        ]
        sub_ids = db.task_split(parent_id, subtasks_def)
        assert len(sub_ids) == 2

        # 每个子任务 apply（close 由级联自动完成）
        for sid in sub_ids:
            assert _get_status(db, sid) == TASK_STATUS_OPEN
            assert _step_count(db, sid) == 0

            result = db.task_apply(sid, reviewer="test")
            assert "error" not in result, f"apply 失败: {result}"

        # 最后一个子任务 apply 后，级联 close 会自动 close 所有兄弟 + 父任务
        assert _get_status(db, parent_id) == TASK_STATUS_CLOSED
    finally:
        db.close()


def _get_status(db, task_id):
    """读取任务状态。"""
    cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
    return cur.fetchone()[0]


def _step_count(db, task_id):
    """统计任务的 steps 数量。"""
    cur = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ?", (task_id,)
    )
    return cur.fetchone()["cnt"]
