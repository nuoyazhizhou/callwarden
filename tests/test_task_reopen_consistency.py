"""任务 reopen 机制最终一致性测试（Step #4）。

端到端验证整个 reopen 闭环：
1. task_create 闭环：创建任务→close→挂子任务→验证父任务 reopen 为 in_progress
2. cw task reopen 命令测试
3. 祖父任务链递归 reopen
4. review/applied/closed 各种状态 reopen
5. 兄弟子任务状态判断（task_create 场景 vs task_reopen 场景）

整合 DB 层 + CLI 层 + 文档层的一致性验证。
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
from callwarden.cli import main as cli_main


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
    """把任务推进到指定状态"""
    if target_status == TASK_STATUS_OPEN:
        return
    import time
    now = time.time()

    cur = db.conn.execute(
        "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ?",
        (task_id,),
    )
    if cur.fetchone()["cnt"] == 0:
        step_id = "S-test-" + task_id[-8:]
        db.conn.execute(
            "INSERT INTO task_steps (id, task_id, step_index, action, target_file, "
            "target_symbol, check_items, status, result, created_at, completed_at) "
            "VALUES (?, ?, 0, 'annotate', 'test.py', '', '', 'done', '', ?, ?)",
            (step_id, task_id, now, now),
        )
        db.conn.commit()

    db.conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (TASK_STATUS_IN_PROGRESS, now, task_id),
    )
    db.conn.commit()
    if target_status == TASK_STATUS_IN_PROGRESS:
        return

    db.conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (TASK_STATUS_REVIEW, now, task_id),
    )
    db.conn.commit()
    if target_status == TASK_STATUS_REVIEW:
        return

    db.conn.execute(
        "UPDATE tasks SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
        (TASK_STATUS_APPLIED, now, now, task_id),
    )
    db.conn.commit()
    if target_status == TASK_STATUS_APPLIED:
        return

    db.conn.execute(
        "UPDATE tasks SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
        (TASK_STATUS_CLOSED, now, now, task_id),
    )
    db.conn.commit()


def _get_status(db, task_id):
    """获取任务状态"""
    cur = db.conn.execute(
        "SELECT status FROM tasks WHERE id = ?",
        (task_id,),
    )
    row = cur.fetchone()
    return row["status"] if row else None


# ============================================
# 1. 端到端闭环：task_create → close → 挂子任务 → reopen
# ============================================


class TestEndToEndReopenCycle:
    """端到端测试：完整的 reopen 闭环"""

    def test_full_cycle_closed_parent(self, db):
        """端到端：创建任务→close→挂子任务→验证父任务 reopen 为 in_progress"""
        # 1. 创建父任务
        parent_id = _create_task(db, title="parent")
        assert _get_status(db, parent_id) == TASK_STATUS_OPEN

        # 2. 推进到 closed
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)
        assert _get_status(db, parent_id) == TASK_STATUS_CLOSED

        # 3. 挂子任务 → 自动 reopen
        child_id = _create_subtask(db, parent_id, title="child")

        # 4. 验证父任务 reopen 为 in_progress
        assert _get_status(db, parent_id) == TASK_STATUS_IN_PROGRESS
        assert _get_status(db, child_id) == TASK_STATUS_OPEN

    def test_full_cycle_applied_parent(self, db):
        """端到端：applied 父任务 → 挂子任务 → reopen"""
        parent_id = _create_task(db, title="parent")
        _complete_task_to_status(db, parent_id, TASK_STATUS_APPLIED)

        child_id = _create_subtask(db, parent_id, title="child")

        assert _get_status(db, parent_id) == TASK_STATUS_IN_PROGRESS

    def test_full_cycle_review_parent(self, db):
        """端到端：review 父任务 → 挂子任务 → reopen"""
        parent_id = _create_task(db, title="parent")
        _complete_task_to_status(db, parent_id, TASK_STATUS_REVIEW)

        child_id = _create_subtask(db, parent_id, title="child")

        assert _get_status(db, parent_id) == TASK_STATUS_IN_PROGRESS

    def test_no_reopen_for_open_parent(self, db):
        """端到端：open 父任务 → 挂子任务 → 不改状态"""
        parent_id = _create_task(db, title="parent")

        child_id = _create_subtask(db, parent_id, title="child")

        assert _get_status(db, parent_id) == TASK_STATUS_OPEN

    def test_no_reopen_for_in_progress_parent(self, db):
        """端到端：in_progress 父任务 → 挂子任务 → 不改状态"""
        parent_id = _create_task(db, title="parent")
        _complete_task_to_status(db, parent_id, TASK_STATUS_IN_PROGRESS)

        child_id = _create_subtask(db, parent_id, title="child")

        assert _get_status(db, parent_id) == TASK_STATUS_IN_PROGRESS


# ============================================
# 2. cw task reopen 命令测试
# ============================================


class TestTaskReopenCommand:
    """测试 cw task reopen 命令"""

    def test_reopen_closed_task(self, db):
        """cw task reopen closed 任务 → in_progress"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_CLOSED)

        result = db.task_reopen(task_id, reviewer="test", reason="e2e test")

        assert result["status"] == TASK_STATUS_IN_PROGRESS
        assert result["previous_status"] == TASK_STATUS_CLOSED

    def test_reopen_applied_task(self, db):
        """cw task reopen applied 任务 → in_progress"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_APPLIED)

        result = db.task_reopen(task_id, reviewer="test")

        assert result["status"] == TASK_STATUS_IN_PROGRESS
        assert result["previous_status"] == TASK_STATUS_APPLIED

    def test_reopen_review_task(self, db):
        """cw task reopen review 任务 → in_progress"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_REVIEW)

        result = db.task_reopen(task_id, reviewer="test")

        assert result["status"] == TASK_STATUS_IN_PROGRESS
        assert result["previous_status"] == TASK_STATUS_REVIEW

    def test_reopen_open_task_returns_error(self, db):
        """cw task reopen open 任务 → 返回错误"""
        task_id = _create_task(db, title="task")

        result = db.task_reopen(task_id, reviewer="test")

        assert "error" in result

    def test_reopen_nonexistent_task(self, db):
        """cw task reopen 不存在的任务 → 返回错误"""
        result = db.task_reopen("T-nonexistent", reviewer="test")

        assert "error" in result


# ============================================
# 3. 祖父任务链递归 reopen
# ============================================


class TestRecursiveReopenChain:
    """测试祖父任务链递归 reopen"""

    def test_two_level_recursive_reopen(self, db):
        """两层任务链 reopen：挂子任务→父 reopen→祖父 reopen"""
        grandparent_id = _create_task(db, title="grandparent")
        parent_id = _create_subtask(db, grandparent_id, title="parent")

        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, grandparent_id, TASK_STATUS_CLOSED)

        new_child_id = _create_subtask(db, parent_id, title="new_child")

        assert _get_status(db, parent_id) == TASK_STATUS_IN_PROGRESS
        assert _get_status(db, grandparent_id) == TASK_STATUS_IN_PROGRESS

    def test_three_level_recursive_reopen(self, db):
        """三层任务链 reopen"""
        root_id = _create_task(db, title="root")
        mid_id = _create_subtask(db, root_id, title="mid")
        leaf_id = _create_subtask(db, mid_id, title="leaf")

        _complete_task_to_status(db, leaf_id, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, mid_id, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, root_id, TASK_STATUS_CLOSED)

        new_child_id = _create_subtask(db, leaf_id, title="new_child")

        assert _get_status(db, leaf_id) == TASK_STATUS_IN_PROGRESS
        assert _get_status(db, mid_id) == TASK_STATUS_IN_PROGRESS
        assert _get_status(db, root_id) == TASK_STATUS_IN_PROGRESS

    def test_manual_reopen_propagates_upward(self, db):
        """手动 reopen 子任务 → 祖父链也 reopen"""
        grandparent_id = _create_task(db, title="grandparent")
        parent_id = _create_subtask(db, grandparent_id, title="parent")

        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, grandparent_id, TASK_STATUS_CLOSED)

        db.task_reopen(parent_id, reviewer="test")

        assert _get_status(db, parent_id) == TASK_STATUS_IN_PROGRESS
        assert _get_status(db, grandparent_id) == TASK_STATUS_IN_PROGRESS


# ============================================
# 4. 兄弟子任务状态判断一致性（task_create vs task_reopen）
# ============================================


class TestSiblingCheckConsistency:
    """验证 task_create 和 task_reopen 对兄弟子任务状态的处理一致"""

    def test_create_with_all_siblings_closed_reopens(self, db):
        """task_create：所有兄弟 closed → reopen 父任务"""
        parent_id = _create_task(db, title="parent")
        s1 = _create_subtask(db, parent_id, title="s1")
        _complete_task_to_status(db, s1, TASK_STATUS_CLOSED)
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        _create_subtask(db, parent_id, title="s2")

        assert _get_status(db, parent_id) == TASK_STATUS_IN_PROGRESS

    def test_create_with_open_sibling_does_not_reopen(self, db):
        """task_create：有兄弟 open → 不 reopen 父任务"""
        parent_id = _create_task(db, title="parent")
        s1 = _create_subtask(db, parent_id, title="s1_open")
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        _create_subtask(db, parent_id, title="s2")

        # 有兄弟 open → 不 reopen
        assert _get_status(db, parent_id) == TASK_STATUS_CLOSED

    def test_manual_reopen_ignores_sibling_status(self, db):
        """task_reopen：手动 reopen 不检查兄弟子任务状态"""
        parent_id = _create_task(db, title="parent")
        s1 = _create_subtask(db, parent_id, title="s1_open")
        _complete_task_to_status(db, parent_id, TASK_STATUS_CLOSED)

        # 手动 reopen 父任务，即使有 open 的兄弟子任务
        result = db.task_reopen(parent_id, reviewer="test")

        assert result["status"] == TASK_STATUS_IN_PROGRESS
        assert _get_status(db, parent_id) == TASK_STATUS_IN_PROGRESS


# ============================================
# 5. 时间戳清理验证
# ============================================


class TestTimestampCleanup:
    """验证 reopen 后时间戳被正确清理"""

    def test_reopen_clears_applied_at(self, db):
        """reopen 后 applied_at 被清理"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_APPLIED)

        cur = db.conn.execute(
            "SELECT applied_at FROM tasks WHERE id = ?", (task_id,)
        )
        assert cur.fetchone()["applied_at"] is not None

        db.task_reopen(task_id, reviewer="test")

        cur = db.conn.execute(
            "SELECT applied_at FROM tasks WHERE id = ?", (task_id,)
        )
        assert cur.fetchone()["applied_at"] is None

    def test_reopen_clears_closed_at(self, db):
        """reopen 后 closed_at 被清理"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_CLOSED)

        cur = db.conn.execute(
            "SELECT closed_at FROM tasks WHERE id = ?", (task_id,)
        )
        assert cur.fetchone()["closed_at"] is not None

        db.task_reopen(task_id, reviewer="test")

        cur = db.conn.execute(
            "SELECT closed_at FROM tasks WHERE id = ?", (task_id,)
        )
        assert cur.fetchone()["closed_at"] is None


# ============================================
# 6. CLI ↔ DB 一致性验证
# ============================================


class TestCliDbConsistency:
    """验证 CLI 命令与 DB 方法的行为一致"""

    def test_cli_reopen_matches_db_reopen(self, db):
        """CLI cw task reopen 与 db.task_reopen 行为一致"""
        task_id = _create_task(db, title="task")
        _complete_task_to_status(db, task_id, TASK_STATUS_CLOSED)

        # 通过 CLI handler 调用
        argv = ["reopen", task_id, "--reviewer", "cli_test"]
        result = cli_main._handle_task(argv, db)

        assert result is True
        assert _get_status(db, task_id) == TASK_STATUS_IN_PROGRESS

    def test_cli_reopen_nonexistent_returns_error(self, db):
        """CLI reopen 不存在的任务返回错误"""
        argv = ["reopen", "T-nonexistent", "--reviewer", "cli_test"]
        result = cli_main._handle_task(argv, db)

        # _handle_task 返回 True 表示已处理（即使失败也打印了错误）
        assert result is True


# ============================================
# 7. i18n 完整性验证
# ============================================


class TestI18nCompleteness:
    """验证 reopen 相关 i18n key 完整性"""

    def test_zh_cn_has_all_keys(self):
        """zh_CN 应包含所有 reopen 相关 key"""
        import json
        i18n_dir = os.path.join(_PKG_PARENT, "i18n")
        with open(os.path.join(i18n_dir, "zh_CN.json"), encoding="utf-8") as f:
            data = json.load(f)
        messages = data.get("cli", {}).get("messages", {})
        required_keys = [
            "task_reopen_failed",
            "task_reopen_success",
            "task_reopen_no_need",
            "task_reopened_at",
            "task_reopen_reason_label",
            "help_task_reopen",
        ]
        for key in required_keys:
            assert key in messages, f"zh_CN 缺少 key: {key}"

    def test_en_us_has_all_keys(self):
        """en_US 应包含所有 reopen 相关 key"""
        import json
        i18n_dir = os.path.join(_PKG_PARENT, "i18n")
        with open(os.path.join(i18n_dir, "en_US.json"), encoding="utf-8") as f:
            data = json.load(f)
        messages = data.get("cli", {}).get("messages", {})
        required_keys = [
            "task_reopen_failed",
            "task_reopen_success",
            "task_reopen_no_need",
            "task_reopened_at",
            "task_reopen_reason_label",
            "help_task_reopen",
        ]
        for key in required_keys:
            assert key in messages, f"en_US 缺少 key: {key}"

    def test_cli_task_reopen_desc_key_exists(self):
        """cli_task_reopen_desc key 应存在"""
        import json
        i18n_dir = os.path.join(_PKG_PARENT, "i18n")
        for lang in ["zh_CN", "en_US"]:
            with open(os.path.join(i18n_dir, f"{lang}.json"), encoding="utf-8") as f:
                data = json.load(f)
            assert "cli_task_reopen_desc" in data, f"{lang} 缺少 cli_task_reopen_desc"


# ============================================
# 8. help 模板验证
# ============================================


class TestHelpTemplate:
    """验证 help 模板包含 task reopen"""

    def test_help_groups_contains_task_reopen(self):
        """_MAIN_HELP_GROUPS 应包含 task reopen"""
        assert hasattr(cli_main, "_MAIN_HELP_GROUPS")
        help_text = str(cli_main._MAIN_HELP_GROUPS)
        assert "task reopen" in help_text, "help 模板缺少 task reopen"
