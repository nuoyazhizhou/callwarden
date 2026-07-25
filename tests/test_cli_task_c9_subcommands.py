"""cw task 子命令一致性测试（C9-1）。

验证 C9 修复的 6 个 help 模板不一致问题中的前 3 个：
- task completion-review：补注册子命令 + handler 调用 run_task_completion_review
- task split：补注册子命令 + handler 调用 task_split + _parse_plan_to_subtasks 辅助函数
- task status-tree：补注册子命令 + handler 调用 _print_task_show（task show 的别名）

覆盖：
1. argparse 子命令注册（3 个新子命令 choices 包含）
2. handler 派发（action == "completion-review"/"split"/"status-tree"）
3. i18n key 完整性（zh_CN + en_US 对齐）
4. help 模板一致性（_HELP_GROUPS 中列出 3 个新命令）
5. _parse_plan_to_subtasks 辅助函数解析正确性
6. 子命令端到端行为（错误场景：任务不存在 / 计划文件不存在 / 无子任务）
"""

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.cli import main as cli_main
from i18n import set_language, t
from callwarden.db import CodeGraphDB

set_language("zh_CN")


@pytest.fixture
def db():
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db = CodeGraphDB(db_path)
        yield db
        db.close()


# ============================================
# 1. argparse 子命令注册
# ============================================


class TestArgparseRegistration:
    """验证 3 个新子命令在 argparse 中注册"""

    def test_completion_review_registered(self, db):
        """completion-review 子命令应被注册"""
        # 通过 --help 触发 SystemExit(0)，证明子命令被识别
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(["completion-review", "--help"], db)
        assert exc_info.value.code == 0

    def test_split_registered(self, db):
        """split 子命令应被注册"""
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(["split", "--help"], db)
        assert exc_info.value.code == 0

    def test_status_tree_registered(self, db):
        """status-tree 子命令应被注册"""
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(["status-tree", "--help"], db)
        assert exc_info.value.code == 0

    def test_completion_review_requires_task_id(self, db):
        """completion-review 必须传 task_id 位置参数"""
        # 不带 task_id 应该 SystemExit(2)（argparse 错误）
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(["completion-review"], db)
        assert exc_info.value.code == 2

    def test_split_requires_task_id_and_plan(self, db):
        """split 必须传 task_id 和 --plan"""
        # 缺 task_id
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(["split"], db)
        assert exc_info.value.code == 2

        # 有 task_id 但缺 --plan
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(["split", "T-test"], db)
        assert exc_info.value.code == 2

    def test_status_tree_requires_task_id(self, db):
        """status-tree 必须传 task_id"""
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(["status-tree"], db)
        assert exc_info.value.code == 2

    def test_completion_review_accepts_step_id(self, db):
        """completion-review 接受可选 --step-id 参数"""
        # 创建一个任务用于触发 handler（不报 argparse 错误即可）
        task_id = db.task_create(title="test", steps=[], creator="test")
        # 带 --step-id 应该正常解析（不抛 SystemExit(2)）
        result = cli_main._handle_task(
            ["completion-review", task_id, "--step-id", "S-123"], db
        )
        assert result is True


# ============================================
# 2. handler 派发
# ============================================


class TestHandlerDispatch:
    """验证 3 个 handler 分支被正确派发"""

    def test_completion_review_calls_db_method(self, db, monkeypatch):
        """completion-review handler 应调用 db.run_task_completion_review"""
        called = {"count": 0, "args": None}

        def fake_review(task_id, step_id=""):
            called["count"] += 1
            called["args"] = (task_id, step_id)
            return {"decision": "pass", "findings": [], "counts": {}}

        # 临时替换 db.run_task_completion_review
        monkeypatch.setattr(db, "run_task_completion_review", fake_review)

        task_id = db.task_create(title="test", steps=[], creator="test")
        result = cli_main._handle_task(["completion-review", task_id], db)
        assert result is True
        assert called["count"] == 1
        assert called["args"][0] == task_id

    def test_completion_review_with_step_id(self, db, monkeypatch):
        """completion-review --step-id 透传到 db 方法"""
        called = {"step_id": None}

        def fake_review(task_id, step_id=""):
            called["step_id"] = step_id
            return {"decision": "pass", "findings": [], "counts": {}}

        monkeypatch.setattr(db, "run_task_completion_review", fake_review)

        task_id = db.task_create(title="test", steps=[], creator="test")
        cli_main._handle_task(
            ["completion-review", task_id, "--step-id", "S-abc"], db
        )
        assert called["step_id"] == "S-abc"

    def test_completion_review_handles_error(self, db, monkeypatch):
        """completion-review handler 处理 db 返回的 error"""
        def fake_review(task_id, step_id=""):
            return {"error": "task not found"}

        monkeypatch.setattr(db, "run_task_completion_review", fake_review)

        task_id = db.task_create(title="test", steps=[], creator="test")
        # 不应抛异常
        captured = io.StringIO()
        with redirect_stdout(captured):
            result = cli_main._handle_task(["completion-review", task_id], db)
        assert result is True

    def test_completion_review_handles_missing_db_method(self, db, monkeypatch):
        """completion-review handler 处理 db 无 run_task_completion_review 方法的情况"""
        # 从 mixin 类上移除方法（影响所有实例的 hasattr 检查）
        from callwarden.db.db_task_quality import TaskQualityMixin
        monkeypatch.delattr(TaskQualityMixin, "run_task_completion_review")

        task_id = db.task_create(title="test", steps=[], creator="test")
        captured = io.StringIO()
        with redirect_stdout(captured):
            result = cli_main._handle_task(["completion-review", task_id], db)
        assert result is True
        # 输出应包含错误提示
        out = captured.getvalue()
        assert "不可用" in out or "not available" in out.lower()

    def test_split_calls_db_task_split(self, db, monkeypatch, tmp_path):
        """split handler 应调用 db.task_split"""
        called = {"count": 0, "args": None}

        def fake_split(task_id, subtasks):
            called["count"] += 1
            called["args"] = (task_id, subtasks)
            return [f"T-sub-{i}" for i in range(len(subtasks))]

        monkeypatch.setattr(db, "task_split", fake_split)

        # 创建父任务
        parent_id = db.task_create(title="parent", steps=[], creator="test")
        # 写一个最小的 plan 文件
        plan_file = tmp_path / "plan.md"
        plan_file.write_text(
            "# 根任务\n\n"
            "## 子任务1\n"
            "描述1\n"
            "- edit @ file1.py\n\n"
            "## 子任务2\n"
            "描述2\n"
            "- refactor @ file2.py\n",
            encoding="utf-8",
        )

        result = cli_main._handle_task(
            ["split", parent_id, "--plan", str(plan_file)], db
        )
        assert result is True
        assert called["count"] == 1
        # 应该解析出 2 个子任务
        assert len(called["args"][1]) == 2

    def test_split_plan_not_found(self, db):
        """split handler 处理 plan 文件不存在"""
        parent_id = db.task_create(title="parent", steps=[], creator="test")
        captured = io.StringIO()
        with redirect_stdout(captured):
            result = cli_main._handle_task(
                ["split", parent_id, "--plan", "/nonexistent/plan.md"], db
            )
        assert result is True
        out = captured.getvalue()
        # 应输出文件不存在的错误
        assert "/nonexistent/plan.md" in out or "not found" in out.lower() or "不存在" in out

    def test_split_task_not_found(self, db, tmp_path):
        """split handler 处理 task_id 不存在"""
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("## sub\n- edit @ x.py\n", encoding="utf-8")

        captured = io.StringIO()
        with redirect_stdout(captured):
            result = cli_main._handle_task(
                ["split", "T-nonexistent", "--plan", str(plan_file)], db
            )
        assert result is True

    def test_split_no_subtasks_in_plan(self, db, tmp_path):
        """split handler 处理 plan 中无子任务的情况"""
        parent_id = db.task_create(title="parent", steps=[], creator="test")
        # 空 plan 文件（只有 H1，无 H2 子任务）
        plan_file = tmp_path / "empty.md"
        plan_file.write_text("# 根任务\n只有描述没有子任务\n", encoding="utf-8")

        captured = io.StringIO()
        with redirect_stdout(captured):
            result = cli_main._handle_task(
                ["split", parent_id, "--plan", str(plan_file)], db
            )
        assert result is True
        out = captured.getvalue()
        # 应输出未找到子任务的提示（i18n 文本为"计划文件中未找到子任务定义"）
        assert "未找到" in out or "no" in out.lower() or "no subtasks" in out.lower()

    def test_status_tree_calls_print_task_show(self, db, monkeypatch):
        """status-tree handler 应调用 _print_task_show(flat=False)"""
        called = {"count": 0, "flat": None}

        def fake_print(db_obj, task_id, flat=False):
            called["count"] += 1
            called["flat"] = flat
            return True

        monkeypatch.setattr(cli_main, "_print_task_show", fake_print)

        task_id = db.task_create(title="test", steps=[], creator="test")
        result = cli_main._handle_task(["status-tree", task_id], db)
        assert result is True
        assert called["count"] == 1
        assert called["flat"] is False  # status-tree 默认树形

    def test_status_tree_equivalent_to_task_show_no_flat(self, db, monkeypatch):
        """status-tree 应与 task show（无 --flat）行为一致"""
        call_log = []

        def fake_print(db_obj, task_id, flat=False):
            call_log.append((task_id, flat))
            return True

        monkeypatch.setattr(cli_main, "_print_task_show", fake_print)

        task_id = db.task_create(title="test", steps=[], creator="test")
        cli_main._handle_task(["status-tree", task_id], db)
        cli_main._handle_task(["show", task_id], db)

        # 两次调用应参数相同（flat=False）
        assert call_log[0] == call_log[1]
        assert call_log[0][1] is False


# ============================================
# 3. i18n key 完整性
# ============================================


class TestI18nCompleteness:
    """验证 i18n key 在 zh_CN / en_US 都存在"""

    REQUIRED_KEYS = [
        "cli_task_completion_review_desc",
        "cli_task_split_desc",
        "cli_task_status_tree_desc",
        "cli_task_arg_step_id",
        "cli_task_arg_plan_file",
    ]

    REQUIRED_MSG_KEYS = [
        "task_completion_review_unavailable",
        "task_completion_review_failed",
        "task_completion_review_result",
        "task_completion_review_task",
        "task_completion_review_step",
        "task_completion_review_summary",
        "task_completion_review_counts",
        "task_completion_review_findings_title",
        "task_completion_review_finding_item",
        "task_split_plan_not_found",
        "task_split_no_subtasks",
        "task_split_success",
        "task_split_subtask_item",
    ]

    REQUIRED_HELP_KEYS = [
        "help_task_completion_review",
        "help_task_split",
        "help_task_status_tree",
    ]

    def test_subcommand_desc_keys_exist_zh(self):
        """zh_CN 应包含所有 subcommand desc key"""
        from i18n import _load_lang
        zh = _load_lang("zh_CN")
        for key in self.REQUIRED_KEYS:
            assert key in zh, f"zh_CN 缺失 key: {key}"

    def test_subcommand_desc_keys_exist_en(self):
        """en_US 应包含所有 subcommand desc key"""
        from i18n import _load_lang
        en = _load_lang("en_US")
        for key in self.REQUIRED_KEYS:
            assert key in en, f"en_US 缺失 key: {key}"

    def test_message_keys_exist_zh(self):
        """zh_CN 应包含所有 messages key"""
        from i18n import _load_lang
        zh = _load_lang("zh_CN")
        cli_msgs = zh.get("cli", {}).get("messages", {})
        for key in self.REQUIRED_MSG_KEYS:
            assert key in cli_msgs, f"zh_CN.cli.messages 缺失 key: {key}"

    def test_message_keys_exist_en(self):
        """en_US 应包含所有 messages key"""
        from i18n import _load_lang
        en = _load_lang("en_US")
        cli_msgs = en.get("cli", {}).get("messages", {})
        for key in self.REQUIRED_MSG_KEYS:
            assert key in cli_msgs, f"en_US.cli.messages 缺失 key: {key}"

    def test_help_keys_exist_zh(self):
        """zh_CN 应包含所有 help 模板 key"""
        from i18n import _load_lang
        zh = _load_lang("zh_CN")
        cli_msgs = zh.get("cli", {}).get("messages", {})
        for key in self.REQUIRED_HELP_KEYS:
            assert key in cli_msgs, f"zh_CN.cli.messages 缺失 help key: {key}"

    def test_help_keys_exist_en(self):
        """en_US 应包含所有 help 模板 key"""
        from i18n import _load_lang
        en = _load_lang("en_US")
        cli_msgs = en.get("cli", {}).get("messages", {})
        for key in self.REQUIRED_HELP_KEYS:
            assert key in cli_msgs, f"en_US.cli.messages 缺失 help key: {key}"

    def test_zh_en_keys_aligned(self):
        """zh_CN 和 en_US 的 key 集合应一致"""
        from i18n import _load_lang
        zh = _load_lang("zh_CN")
        en = _load_lang("en_US")

        all_keys = (
            self.REQUIRED_KEYS
            + self.REQUIRED_MSG_KEYS
            + self.REQUIRED_HELP_KEYS
        )
        for key in all_keys:
            # subcommand desc key 在顶层；message/help key 在 cli.messages 下
            if key in self.REQUIRED_KEYS:
                assert key in zh, f"zh_CN 缺失: {key}"
                assert key in en, f"en_US 缺失: {key}"
            else:
                zh_msgs = zh.get("cli", {}).get("messages", {})
                en_msgs = en.get("cli", {}).get("messages", {})
                assert key in zh_msgs, f"zh_CN.cli.messages 缺失: {key}"
                assert key in en_msgs, f"en_US.cli.messages 缺失: {key}"


# ============================================
# 4. help 模板一致性
# ============================================


class TestHelpTemplateConsistency:
    """验证 help 模板列出 3 个新子命令"""

    def test_help_template_contains_completion_review(self):
        """_MAIN_HELP_GROUPS 应包含 task completion-review 项"""
        help_text = ""
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            for cmd, msg_key in items:
                help_text += cmd + "\n"
        assert "task completion-review" in help_text

    def test_help_template_contains_split(self):
        """_MAIN_HELP_GROUPS 应包含 task split 项"""
        help_text = ""
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            for cmd, msg_key in items:
                help_text += cmd + "\n"
        assert "task split" in help_text

    def test_help_template_contains_status_tree(self):
        """_MAIN_HELP_GROUPS 应包含 task status-tree 项"""
        help_text = ""
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            for cmd, msg_key in items:
                help_text += cmd + "\n"
        assert "task status-tree" in help_text

    def test_task_group_contains_new_commands(self):
        """task 分组应包含 3 个新命令"""
        found_task_group = False
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            if any("completion-review" in cmd for cmd, _ in items):
                found_task_group = True
                cmds = [cmd for cmd, _ in items]
                assert any("completion-review" in c for c in cmds)
                assert any(c.startswith("task split") for c in cmds)
                assert any("status-tree" in c for c in cmds)
                break
        assert found_task_group, "未找到包含 completion-review 的 help 分组"

    def test_help_template_msg_keys_resolve(self):
        """所有 help 模板引用的 msg_key 应可解析"""
        from i18n import set_language, t as _t
        set_language("en_US")
        try:
            for group_title, items in cli_main._MAIN_HELP_GROUPS:
                for cmd, msg_key in items:
                    if "completion-review" in cmd or "split" in cmd or "status-tree" in cmd:
                        text = _t(msg_key, default="")
                        assert text, f"无法解析 msg_key: {msg_key}"
        finally:
            set_language("zh_CN")


# ============================================
# 5. _parse_plan_to_subtasks 辅助函数
# ============================================


class TestParsePlanToSubtasks:
    """验证 _parse_plan_to_subtasks 辅助函数解析 Markdown 计划"""

    def test_parse_simple_plan(self):
        """解析简单的二级标题 + 列表项"""
        plan = (
            "## 子任务1\n"
            "描述1\n"
            "- edit @ file1.py\n\n"
            "## 子任务2\n"
            "描述2\n"
            "- refactor @ file2.py\n"
        )
        subtasks = cli_main._parse_plan_to_subtasks(plan)
        assert len(subtasks) == 2
        assert subtasks[0]["title"] == "子任务1"
        assert subtasks[0]["description"] == "描述1"
        assert len(subtasks[0]["steps"]) == 1
        assert subtasks[0]["steps"][0]["action"] == "edit"
        assert subtasks[0]["steps"][0]["target_file"] == "file1.py"
        assert subtasks[1]["title"] == "子任务2"
        assert subtasks[1]["steps"][0]["action"] == "refactor"

    def test_parse_empty_plan(self):
        """空 plan 应返回空列表"""
        assert cli_main._parse_plan_to_subtasks("") == []
        assert cli_main._parse_plan_to_subtasks("   \n  \n") == []

    def test_parse_only_h1(self):
        """只有一级标题没有二级标题，应返回空列表"""
        plan = "# 根任务\n只有描述\n"
        assert cli_main._parse_plan_to_subtasks(plan) == []

    def test_parse_multiple_steps_per_subtask(self):
        """一个子任务包含多个步骤"""
        plan = (
            "## 子任务\n"
            "- edit @ file1.py\n"
            "- annotate @ file2.py\n"
            "- test @ file3.py\n"
        )
        subtasks = cli_main._parse_plan_to_subtasks(plan)
        assert len(subtasks) == 1
        assert len(subtasks[0]["steps"]) == 3
        assert subtasks[0]["steps"][0]["action"] == "edit"
        assert subtasks[0]["steps"][1]["action"] == "annotate"
        assert subtasks[0]["steps"][2]["action"] == "test"

    def test_parse_action_colon_format(self):
        """支持 action: target_file 格式"""
        plan = "## 子任务\n- edit: file.py\n"
        subtasks = cli_main._parse_plan_to_subtasks(plan)
        assert len(subtasks) == 1
        assert subtasks[0]["steps"][0]["action"] == "edit"
        assert subtasks[0]["steps"][0]["target_file"] == "file.py"

    def test_parse_action_only_no_target(self):
        """只有 action 没有 target_file"""
        plan = "## 子任务\n- build\n"
        subtasks = cli_main._parse_plan_to_subtasks(plan)
        assert len(subtasks) == 1
        assert subtasks[0]["steps"][0]["action"] == "build"
        assert subtasks[0]["steps"][0]["target_file"] == ""

    def test_parse_code_block_ignored(self):
        """代码块内容不应被解析为步骤"""
        plan = (
            "## 子任务\n"
            "描述\n"
            "```\n"
            "- fake @ not_a_step.py\n"
            "```\n"
            "- edit @ real.py\n"
        )
        subtasks = cli_main._parse_plan_to_subtasks(plan)
        assert len(subtasks) == 1
        # 代码块内的 - 不应被解析
        assert len(subtasks[0]["steps"]) == 1
        assert subtasks[0]["steps"][0]["target_file"] == "real.py"

    def test_parse_trailing_hashes_in_title(self):
        """标题末尾的 # 应被清理"""
        plan = "## 子任务 ##\n- edit @ x.py\n"
        subtasks = cli_main._parse_plan_to_subtasks(plan)
        assert subtasks[0]["title"] == "子任务"

    def test_parse_h3_ignored_in_description(self):
        """三级标题不应被当作描述行"""
        plan = "## 子任务\n### 步骤分组\n- edit @ x.py\n"
        subtasks = cli_main._parse_plan_to_subtasks(plan)
        # 三级标题应被跳过，不进入描述
        assert "### 步骤分组" not in subtasks[0]["description"]

    def test_parse_all_list_markers(self):
        """支持 - * + 三种无序列表标记"""
        plan = (
            "## 子任务\n"
            "- a @ f1.py\n"
            "* b @ f2.py\n"
            "+ c @ f3.py\n"
        )
        subtasks = cli_main._parse_plan_to_subtasks(plan)
        assert len(subtasks[0]["steps"]) == 3


# ============================================
# 6. 端到端行为
# ============================================


class TestEndToEnd:
    """端到端：split 后通过 status-tree 查看"""

    def test_split_then_status_tree(self, db, tmp_path):
        """split 拆分后 status-tree 能看到子任务"""
        parent_id = db.task_create(title="parent", steps=[], creator="test")
        plan_file = tmp_path / "plan.md"
        plan_file.write_text(
            "## 子任务1\n- edit @ f1.py\n\n## 子任务2\n- edit @ f2.py\n",
            encoding="utf-8",
        )

        # 执行 split
        result = cli_main._handle_task(
            ["split", parent_id, "--plan", str(plan_file)], db
        )
        assert result is True

        # 验证子任务已创建
        cur = db.conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE parent_id = ?",
            (parent_id,),
        )
        assert cur.fetchone()["cnt"] == 2

        # status-tree 应能正常显示（不抛异常）
        result = cli_main._handle_task(["status-tree", parent_id], db)
        assert result is True

    def test_completion_review_on_nonexistent_task(self, db, capsys):
        """completion-review 对不存在任务的处理"""
        # 任务不存在，run_task_completion_review 应返回 error 或 decision=pass
        # handler 不应抛异常
        result = cli_main._handle_task(
            ["completion-review", "T-nonexistent"], db
        )
        assert result is True
