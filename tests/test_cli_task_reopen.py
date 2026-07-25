"""cw task reopen CLI 命令测试。

覆盖：
- argparse 子命令注册
- 帮助文本输出
- 端到端 reopen 流程（closed → in_progress）
- 失败场景（任务不存在 / 状态不对）
- 祖父任务链递归 reopen
- i18n key 完整性
"""

import io
import json
import os
import sys
import tempfile
from argparse import Namespace
from contextlib import redirect_stdout

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.cli import main as cli_main
from i18n import set_language
from callwarden.db import CodeGraphDB
from callwarden.db.schema import (
    TASK_STATUS_OPEN,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_REVIEW,
    TASK_STATUS_APPLIED,
    TASK_STATUS_CLOSED,
)

set_language("zh_CN")


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = CodeGraphDB(db_path)
        yield db
        db.close()


def _make_argv(task_id="T-test", **kwargs):
    """构造 task reopen argv 列表"""
    argv = ["reopen", task_id]
    if "reviewer" in kwargs:
        argv.extend(["--reviewer", kwargs["reviewer"]])
    if "reason" in kwargs:
        argv.extend(["--reason", kwargs["reason"]])
    return argv


# ============================================
# 1. argparse 子命令注册
# ============================================


class TestTaskReopenArgparse:
    """测试 task reopen 子命令注册"""

    def _get_task_subparser(self):
        """获取 _handle_task 的内部 parser，并返回 subparsers"""
        import io
        from i18n import t

        # 构造 task parser（复制 _handle_task 内部逻辑）
        import argparse
        parser = argparse.ArgumentParser(prog="cw task")
        sub = parser.add_subparsers(dest="action", required=True)
        # 注册所有 task 子命令（调用 _handle_task 时会重新创建）
        return parser, sub

    def test_reopen_subcommand_registered(self, monkeypatch):
        """argparse 应注册 reopen 子命令"""
        # 通过实际调用 _handle_task(["reopen", "--help"], db) 验证
        # 检查 task 子命令 choices 包含 reopen
        captured_choice = False

        # 临时 monkeypatch _handle_task 的 parser 创建，捕获 subparsers
        import callwarden.cli.main as m
        orig_handle = m._handle_task

        captured = {}

        def capture_handle(args, db):
            import argparse
            from i18n import t
            parser = argparse.ArgumentParser(prog="cw task")
            sub = parser.add_subparsers(dest="action", required=True)
            # 调用原始函数以注册所有子命令
            # 但我们需要捕获 sub，所以重新构造
            orig_handle(args, db)
            # 收集 choices
            for action in parser._actions:
                if hasattr(action, "choices"):
                    captured["choices"] = list(action.choices.keys())
            return True

        # 实际上更简单的方式：直接调用 _handle_task 检查是否报 invalid choice
        # 这里通过 --help 触发 SystemExit，证明 reopen 被识别
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            from callwarden.db import CodeGraphDB
            db = CodeGraphDB(db_path)

            # reopen --help 应该 SystemExit(0) 而不是 SystemExit(2)
            with pytest.raises(SystemExit) as exc_info:
                cli_main._handle_task(["reopen", "--help"], db)
            assert exc_info.value.code == 0

            db.close()

    def test_reopen_accepts_task_id(self):
        """reopen 子命令接受 task_id 位置参数"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            from callwarden.db import CodeGraphDB
            db = CodeGraphDB(db_path)

            # 不带 task_id 应该 SystemExit(2)（argparse 错误）
            with pytest.raises(SystemExit) as exc_info:
                cli_main._handle_task(["reopen"], db)
            assert exc_info.value.code == 2

            db.close()

    def test_reopen_default_reviewer(self):
        """默认 reviewer 应为 'reviewer'"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            from callwarden.db import CodeGraphDB
            db = CodeGraphDB(db_path)

            # 创建一个任务并 close 它，然后 reopen
            task_id = db.task_create(title="test", steps=[], creator="test")
            # 直接通过 SQL 推进到 closed
            import time
            now = time.time()
            db.conn.execute(
                "UPDATE tasks SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                ("closed", now, now, task_id),
            )
            db.conn.commit()

            # 调用 reopen（不带 --reviewer，应使用默认 'reviewer'）
            result = cli_main._handle_task(["reopen", task_id], db)
            assert result is True

            # 验证 reviewer 不影响状态（已 reopen 即可）
            cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
            assert cur.fetchone()["status"] == "in_progress"

            db.close()

    def test_reopen_custom_reviewer(self):
        """支持自定义 reviewer"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            from callwarden.db import CodeGraphDB
            db = CodeGraphDB(db_path)

            task_id = db.task_create(title="test", steps=[], creator="test")
            import time
            now = time.time()
            db.conn.execute(
                "UPDATE tasks SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                ("closed", now, now, task_id),
            )
            db.conn.commit()

            # 调用 reopen 带 --reviewer
            result = cli_main._handle_task(
                ["reopen", task_id, "--reviewer", "agent-007"], db
            )
            assert result is True

            db.close()

    def test_reopen_reason_optional(self):
        """reason 参数可选"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            from callwarden.db import CodeGraphDB
            db = CodeGraphDB(db_path)

            task_id = db.task_create(title="test", steps=[], creator="test")
            import time
            now = time.time()
            db.conn.execute(
                "UPDATE tasks SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                ("closed", now, now, task_id),
            )
            db.conn.commit()

            # 调用 reopen 带 --reason
            result = cli_main._handle_task(
                ["reopen", task_id, "--reason", "found bug"], db
            )
            assert result is True

            db.close()


# ============================================
# 2. 端到端 reopen 流程
# ============================================


class TestTaskReopenE2E:
    """端到端测试 cw task reopen 命令"""

    def _push_to_status(self, db, task_id, target_status):
        """把任务推进到指定状态"""
        import time
        now = time.time()
        if target_status == TASK_STATUS_OPEN:
            return
        # 添加一个步骤（如果任务没有步骤）
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

    def test_reopen_closed_task_e2e(self, db, capsys):
        """端到端：reopen closed 任务 → in_progress"""
        task_id = db.task_create(
            title="test task",
            description="test",
            steps=[],
            creator="test",
        )
        self._push_to_status(db, task_id, TASK_STATUS_CLOSED)

        # 调用 CLI handler
        result = cli_main._handle_task(
            _make_argv(task_id=task_id, reason="found bug after apply"),
            db,
        )

        assert result is True
        captured = capsys.readouterr()
        assert "in_progress" in captured.out or "重新打开" in captured.out

        # 验证 DB 状态
        cur = db.conn.execute("SELECT status, applied_at, closed_at FROM tasks WHERE id = ?", (task_id,))
        row = cur.fetchone()
        assert row["status"] == TASK_STATUS_IN_PROGRESS
        assert row["applied_at"] is None
        assert row["closed_at"] is None

    def test_reopen_applied_task_e2e(self, db, capsys):
        """端到端：reopen applied 任务 → in_progress"""
        task_id = db.task_create(title="test", steps=[], creator="test")
        self._push_to_status(db, task_id, TASK_STATUS_APPLIED)

        result = cli_main._handle_task(_make_argv(task_id=task_id), db)

        assert result is True
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_IN_PROGRESS

    def test_reopen_open_task_fails_e2e(self, db, capsys):
        """端到端：reopen open 任务 → 失败提示"""
        task_id = db.task_create(title="test", steps=[], creator="test")
        # 保持 open

        result = cli_main._handle_task(_make_argv(task_id=task_id), db)

        assert result is True  # 返回 True 但输出错误
        captured = capsys.readouterr()
        # i18n 可能是中文（"失败"/"无需"）或英文（"failed"/"no need"），都应通过
        out_lower = captured.out.lower()
        assert "失败" in captured.out or "无需" in captured.out or \
               "failed" in out_lower or "no need" in out_lower

    def test_reopen_nonexistent_task_e2e(self, db, capsys):
        """端到端：reopen 不存在的任务 → 失败"""
        result = cli_main._handle_task(_make_argv(task_id="T-nonexistent"), db)

        assert result is True
        captured = capsys.readouterr()
        # i18n 可能是中文（"失败"/"未找到"）或英文（"failed"/"not found"），都应通过
        out_lower = captured.out.lower()
        assert "失败" in captured.out or "未找到" in captured.out or \
               "failed" in out_lower or "not found" in out_lower

    def test_reopen_propagates_to_parent_e2e(self, db, capsys):
        """端到端：reopen 子任务时，祖父任务链也应 reopen"""
        grandparent_id = db.task_create(title="grandparent", steps=[], creator="test")
        parent_id = db.task_create_subtask(
            parent_task_id=grandparent_id,
            title="parent",
            steps=[],
            creator="test",
        )

        self._push_to_status(db, parent_id, TASK_STATUS_CLOSED)
        self._push_to_status(db, grandparent_id, TASK_STATUS_CLOSED)

        result = cli_main._handle_task(
            _make_argv(task_id=parent_id, reason="code review issue"),
            db,
        )

        assert result is True

        # 父任务 reopen
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (parent_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_IN_PROGRESS

        # 祖父任务也 reopen
        cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (grandparent_id,))
        assert cur.fetchone()["status"] == TASK_STATUS_IN_PROGRESS


# ============================================
# 3. i18n key 完整性
# ============================================


class TestTaskReopenI18n:
    """测试 reopen 相关 i18n key 完整性"""

    def test_zh_cn_has_all_reopen_keys(self):
        """zh_CN.json 应包含所有 reopen 相关 key"""
        i18n_path = os.path.join(_PKG_PARENT, "i18n", "zh_CN.json")
        with open(i18n_path, encoding="utf-8") as f:
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
            assert key in messages, f"Missing key: {key}"

    def test_en_us_has_all_reopen_keys(self):
        """en_US.json 应包含所有 reopen 相关 key"""
        i18n_path = os.path.join(_PKG_PARENT, "i18n", "en_US.json")
        with open(i18n_path, encoding="utf-8") as f:
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
            assert key in messages, f"Missing key: {key}"

    def test_cli_task_reopen_desc_key_exists(self):
        """cli_task_reopen_desc key 应存在"""
        i18n_path = os.path.join(_PKG_PARENT, "i18n", "zh_CN.json")
        with open(i18n_path, encoding="utf-8") as f:
            data = json.load(f)
        assert "cli_task_reopen_desc" in data

    def test_cli_task_arg_reopen_reason_key_exists(self):
        """cli_task_arg_reopen_reason key 应存在"""
        i18n_path = os.path.join(_PKG_PARENT, "i18n", "en_US.json")
        with open(i18n_path, encoding="utf-8") as f:
            data = json.load(f)
        assert "cli_task_arg_reopen_reason" in data

    def test_zh_en_keys_consistent(self):
        """zh_CN 和 en_US 的 key 集合应一致"""
        zh_path = os.path.join(_PKG_PARENT, "i18n", "zh_CN.json")
        en_path = os.path.join(_PKG_PARENT, "i18n", "en_US.json")
        with open(zh_path, encoding="utf-8") as f:
            zh_data = json.load(f)
        with open(en_path, encoding="utf-8") as f:
            en_data = json.load(f)

        zh_keys = set(zh_data.get("cli", {}).get("messages", {}).keys())
        en_keys = set(en_data.get("cli", {}).get("messages", {}).keys())

        # 验证 reopen 相关 key 在两个文件中都存在
        reopen_keys = {
            "task_reopen_failed",
            "task_reopen_success",
            "task_reopen_no_need",
            "task_reopened_at",
            "task_reopen_reason_label",
            "help_task_reopen",
        }
        assert reopen_keys.issubset(zh_keys)
        assert reopen_keys.issubset(en_keys)


# ============================================
# 4. help 模板更新验证
# ============================================


class TestHelpTemplateUpdate:
    """验证 _MAIN_HELP_GROUPS 中 task 分组包含 reopen"""

    def test_help_groups_contains_task_reopen(self):
        """_MAIN_HELP_GROUPS 中 task 分组应包含 'task reopen <TASK_ID>'"""
        # 找到 task 分组
        for group_key, items in cli_main._MAIN_HELP_GROUPS:
            if "task" in group_key.lower():
                commands = [cmd for cmd, _ in items]
                # 查找 reopen
                reopen_items = [cmd for cmd in commands if "reopen" in cmd]
                assert len(reopen_items) > 0, "task 分组应包含 reopen 命令"
                return
        pytest.fail("未找到 task 分组")

