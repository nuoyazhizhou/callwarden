"""task_create 孤儿任务 soft warning 测试（C1）。

覆盖任务 T-1783349079761-a71a：验证 task_create 在创建大型根任务时
输出 soft warning 建议使用 task_split，且不阻断创建。

测试内容：
- 小任务（steps <= 5 且 files <= 3）不触发 warning
- 大任务（steps > 5）触发 warning
- 大任务（files > 3）触发 warning
- warning 不阻断任务创建（task_id 仍返回）
- parent_id 非空时不触发 warning（子任务不受限）
- _check_orphan_task_warning 方法独立测试
"""

import io
import os
import sys
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB


def _db_with_workspace():
    """构造临时工作区数据库。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _capture_stderr(func):
    """捕获 stderr 输出。"""
    old_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        result = func()
        output = sys.stderr.getvalue()
    finally:
        sys.stderr = old_stderr
    return result, output


# ----------------------------------------------------------------------
# _check_orphan_task_warning 方法独立测试
# ----------------------------------------------------------------------

def test_check_orphan_warning_small_task_no_warning():
    """小任务（steps <= 5 且 files <= 3）不触发 warning。"""
    db, _ = _db_with_workspace()
    steps = [
        {"action": "edit", "target_file": "f1.py"},
        {"action": "edit", "target_file": "f2.py"},
        {"action": "test", "target_file": "f3.py"},
    ]
    _, output = _capture_stderr(lambda: db._check_orphan_task_warning("Small", steps))
    assert output == "", f"小任务不应触发 warning，实际输出: {output}"


def test_check_orphan_warning_large_step_count():
    """大任务（steps > 5）触发 warning。"""
    db, _ = _db_with_workspace()
    steps = [{"action": "edit", "target_file": "f1.py"} for _ in range(6)]
    _, output = _capture_stderr(lambda: db._check_orphan_task_warning("LargeSteps", steps))
    assert "Soft Warning" in output or "软警告" in output, f"应触发 warning，实际: {output}"
    assert "LargeSteps" in output or "task_split" in output


def test_check_orphan_warning_large_file_count():
    """大任务（files > 3）触发 warning。"""
    db, _ = _db_with_workspace()
    steps = [
        {"action": "edit", "target_file": "f1.py"},
        {"action": "edit", "target_file": "f2.py"},
        {"action": "edit", "target_file": "f3.py"},
        {"action": "edit", "target_file": "f4.py"},
    ]
    _, output = _capture_stderr(lambda: db._check_orphan_task_warning("LargeFiles", steps))
    assert "Soft Warning" in output or "软警告" in output, f"应触发 warning，实际: {output}"


def test_check_orphan_warning_multi_file_target():
    """target_file 包含 '+' 分隔的多文件时正确统计。"""
    db, _ = _db_with_workspace()
    # 一个 step 涉及 4 个文件（通过 + 分隔）
    steps = [
        {"action": "edit", "target_file": "f1.py + f2.py + f3.py + f4.py"},
    ]
    _, output = _capture_stderr(lambda: db._check_orphan_task_warning("MultiFile", steps))
    assert "Soft Warning" in output or "软警告" in output, f"多文件应触发 warning，实际: {output}"


def test_check_orphan_warning_no_steps_no_warning():
    """steps 为空时不触发 warning。"""
    db, _ = _db_with_workspace()
    _, output = _capture_stderr(lambda: db._check_orphan_task_warning("NoSteps", []))
    assert output == ""


# ----------------------------------------------------------------------
# task_create 集成测试
# ----------------------------------------------------------------------

def test_task_create_small_task_no_warning():
    """小任务 task_create 不触发 warning。"""
    db, _ = _db_with_workspace()
    steps = [
        {"action": "edit", "target_file": "f1.py"},
        {"action": "test", "target_file": "f1.py"},
    ]
    task_id, output = _capture_stderr(lambda: db.task_create("Small", steps=steps))
    assert task_id, "task_id 应非空"
    assert output == "", f"小任务不应触发 warning，实际: {output}"


def test_task_create_large_task_warning_shown():
    """大任务 task_create 触发 warning。"""
    db, _ = _db_with_workspace()
    steps = [{"action": "edit", "target_file": f"f{i}.py"} for i in range(6)]
    task_id, output = _capture_stderr(lambda: db.task_create("LargeTask", steps=steps))
    assert task_id, "task_id 应非空（warning 不阻断创建）"
    assert "Soft Warning" in output or "软警告" in output, f"应触发 warning，实际: {output}"


def test_task_create_warning_does_not_block():
    """warning 不阻断任务创建（task_id 仍返回，步骤仍写入）。"""
    db, _ = _db_with_workspace()
    steps = [{"action": "edit", "target_file": f"f{i}.py"} for i in range(5)]
    task_id, output = _capture_stderr(lambda: db.task_create("Blocked?", steps=steps))
    # 任务应成功创建
    assert task_id, "task_id 应非空"
    # 验证步骤已写入数据库
    cur = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ?",
        (task_id,),
    )
    assert cur.fetchone()["cnt"] == 5, "5 个步骤应全部写入"


def test_task_create_with_parent_no_warning():
    """有 parent_id 的子任务不触发 warning（子任务不受孤儿限制）。"""
    db, _ = _db_with_workspace()
    # 先创建父任务
    parent_id = db.task_create("Parent", steps=[{"action": "edit", "target_file": "p1.py"}])
    # 创建大子任务（steps > 5）
    steps = [{"action": "edit", "target_file": f"f{i}.py"} for i in range(6)]
    task_id, output = _capture_stderr(
        lambda: db.task_create("ChildLarge", steps=steps, parent_id=parent_id)
    )
    assert task_id, "子任务 task_id 应非空"
    assert output == "", f"子任务不应触发 warning，实际: {output}"


def test_task_create_threshold_boundary():
    """边界测试：恰好 5 steps + 3 files 不触发，6 steps 或 4 files 触发。"""
    db, _ = _db_with_workspace()
    # 5 steps + 3 files：不触发
    steps_5_3 = [
        {"action": "edit", "target_file": "f1.py"},
        {"action": "edit", "target_file": "f2.py"},
        {"action": "edit", "target_file": "f3.py"},
        {"action": "test", "target_file": "f1.py"},
        {"action": "test", "target_file": "f2.py"},
    ]
    _, output = _capture_stderr(lambda: db.task_create("Boundary5_3", steps=steps_5_3))
    assert output == "", f"5 steps + 3 files 不应触发 warning，实际: {output}"

    # 6 steps + 1 file：触发（steps > 5）
    steps_6_1 = [{"action": "edit", "target_file": "f1.py"} for _ in range(6)]
    _, output = _capture_stderr(lambda: db.task_create("Boundary6_1", steps=steps_6_1))
    assert "Soft Warning" in output or "软警告" in output, f"6 steps 应触发 warning，实际: {output}"

    # 3 steps + 4 files：触发（files > 3）
    steps_3_4 = [
        {"action": "edit", "target_file": "f1.py"},
        {"action": "edit", "target_file": "f2.py"},
        {"action": "edit", "target_file": "f3.py + f4.py"},
    ]
    _, output = _capture_stderr(lambda: db.task_create("Boundary3_4", steps=steps_3_4))
    assert "Soft Warning" in output or "软警告" in output, f"4 files 应触发 warning，实际: {output}"
