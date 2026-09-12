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
from callwarden.i18n import set_language
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
        from callwarden.i18n import t

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
            from callwarden.i18n import t
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

    def test_reopen_default_reviewer(self, db, route_stub):
        """默认 reviewer 应为 'reviewer'，并经 task.reopen RPC 下发。

        stale 依据（A 桶 / daemon authority 化）：CLI 已改走
        `route_task_write("task.reopen", {...}, _local_reopen)`，本地
        `db.task_reopen` 不再是 authority；旧断言（直连本地 CodeGraphDB 并检查
        本地 status）在无 daemon 环境下只会撞 `E_HTTP_MANIFEST_STALE`。
        改为 RPC 契约断言。
        """
        route_stub.reply("task.reopen", {
            "task_id": "T-1", "previous_status": "closed", "status": "in_progress",
        })
        result = cli_main._handle_task(["reopen", "T-1"], db)
        assert result is True
        params = route_stub.last_params("task.reopen")
        assert params["task_id"] == "T-1"
        assert params["reviewer"] == "reviewer", (
            f"--reviewer 缺省应为 'reviewer'，实际: {params.get('reviewer')!r}"
        )

    def test_reopen_custom_reviewer(self, db, route_stub):
        """--reviewer 自定义值应透传到 task.reopen RPC（stale 依据同上一用例）。"""
        route_stub.reply("task.reopen", {
            "task_id": "T-2", "previous_status": "closed", "status": "in_progress",
        })
        result = cli_main._handle_task(
            ["reopen", "T-2", "--reviewer", "agent-007"], db
        )
        assert result is True
        assert route_stub.last_params("task.reopen")["reviewer"] == "agent-007"

    def test_reopen_reason_optional(self, db, route_stub):
        """reason 参数可选：给出时透传，缺省为空串（stale 依据同上）。"""
        route_stub.reply("task.reopen", {
            "task_id": "T-3", "previous_status": "applied", "status": "in_progress",
        })
        cli_main._handle_task(
            ["reopen", "T-3", "--reason", "found bug"], db
        )
        assert route_stub.last_params("task.reopen")["reason"] == "found bug"

        route_stub.reply("task.reopen", {
            "task_id": "T-4", "previous_status": "closed", "status": "in_progress",
        })
        cli_main._handle_task(["reopen", "T-4"], db)
        assert route_stub.last_params("task.reopen")["reason"] == ""


# ============================================
# 2. 端到端 reopen 流程
# ============================================


class TestTaskReopenE2E:
    """cw task reopen 的 CLI 侧 RPC 契约与输出渲染。

    stale 依据（A 桶 / daemon authority 化）：任务状态迁移（review/applied/
    closed → in_progress）与祖父链递归 reopen 已由 daemon 承接（db 级语义见
    tests/test_task_reopen.py、tests/test_task_reopen_consistency.py）。
    CLI 现在只负责 ``route_task_write("task.reopen", …)`` 下发 + 渲染 daemon
    回包/错误信封。旧断言直连本地 CodeGraphDB 并检查本地行，在 daemon
    authority 下既不再成立，也会在无 daemon 时撞 ``E_HTTP_MANIFEST_STALE``。
    """

    def test_reopen_closed_task_e2e(self, db, route_stub, capsys):
        """closed 任务：下发 task.reopen 并渲染成功回包。"""
        route_stub.reply("task.reopen", {
            "task_id": "T-1", "previous_status": "closed", "status": "in_progress",
        })
        result = cli_main._handle_task(
            _make_argv(task_id="T-1", reason="found bug after apply"), db
        )
        assert result is True
        out = capsys.readouterr().out
        assert "重新打开" in out or "in_progress" in out
        params = route_stub.last_params("task.reopen")
        assert params["task_id"] == "T-1"
        assert params["reason"] == "found bug after apply"

    def test_reopen_applied_task_e2e(self, db, route_stub):
        """applied 任务：previous_status 取 daemon 回包并渲染。"""
        route_stub.reply("task.reopen", {
            "task_id": "T-2", "previous_status": "applied", "status": "in_progress",
        })
        result = cli_main._handle_task(_make_argv(task_id="T-2"), db)
        assert result is True
        assert route_stub.last_params("task.reopen")["task_id"] == "T-2"

    def test_reopen_open_task_fails_e2e(self, db, route_stub, capsys):
        """open 任务无需 reopen：daemon 错误信封 → CLI 渲染并 RC=2。"""
        route_stub.reply("task.reopen", {
            "error": "任务当前状态为 'open'，无需重新打开",
            "task_id": "T-3",
            "status": "open",
        })
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(_make_argv(task_id="T-3"), db)
        # GOV-FIX-08：daemon/传输错误路径禁止 RC=0 假成功
        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        assert "无需重新打开" in out or "失败" in out

    def test_reopen_nonexistent_task_e2e(self, db, route_stub, capsys):
        """任务不存在：daemon 错误信封 → CLI 渲染并 RC=2。"""
        route_stub.reply("task.reopen", {
            "error": "未找到任务: T-nonexistent",
            "task_id": "T-nonexistent",
        })
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(_make_argv(task_id="T-nonexistent"), db)
        assert exc_info.value.code == 2
        out = capsys.readouterr().out
        assert "未找到任务" in out or "失败" in out

    def test_reopen_propagates_to_parent_e2e(self, db, route_stub):
        """子任务 reopen：CLI 只如实下发子任务 id，祖父链递归由 daemon 承接。

        db 级递归语义由 tests/test_task_reopen.py 与
        tests/test_task_reopen_consistency.py 覆盖；本用例锁 CLI→RPC 契约
        （子任务 id 不在 CLI 侧被截断/改写）。
        """
        route_stub.reply("task.reopen", {
            "task_id": "T-child", "previous_status": "closed", "status": "in_progress",
        })
        result = cli_main._handle_task(
            _make_argv(task_id="T-child", reason="code review issue"), db
        )
        assert result is True
        params = route_stub.last_params("task.reopen")
        assert params["task_id"] == "T-child"
        assert route_stub.count("task.reopen") == 1


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

