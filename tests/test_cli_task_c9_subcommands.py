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
from callwarden.i18n import set_language, t
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

    def test_completion_review_accepts_step_id(self, db, route_stub):
        """completion-review 接受可选 --step-id 参数

        stale 依据（A 桶：薄客户端 RPC seam）：生产已 daemon authority 化。
        `cli/main.py:5975` 的 completion-review 分支经
        `route_task_write("task.completion_review", {"task_id", "step_id"},
        _local_completion_review)` 走 daemon RPC（与 MCP 同协议）。本用例验证
        argparse 解析出的 `--step-id` 被路由透传（回包由 route_stub 提供），
        不再依赖本地 DB 直连语义。
        """
        route_stub.reply("task.completion_review", {
            "decision": "pass", "findings": [], "counts": {}})
        # 创建一个任务用于触发 handler（不报 argparse 错误即可）
        task_id = db.task_create(title="test", steps=[], creator="test")
        # 带 --step-id 应该正常解析（不抛 SystemExit(2)）
        result = cli_main._handle_task(
            ["completion-review", task_id, "--step-id", "S-123"], db
        )
        assert result is True
        assert route_stub.last_params("task.completion_review")["step_id"] == "S-123"


# ============================================
# 2. handler 派发
# ============================================


class TestHandlerDispatch:
    """验证 3 个 handler 分支被正确派发"""

    def test_completion_review_routes_rpc(self, db, route_stub):
        """completion-review handler 经 RPC 路由到 task.completion_review

        stale 依据（A 桶：薄客户端 RPC seam）：生产 `cli/main.py:5975` 已改为
        `route_task_write("task.completion_review", {...}, _local_completion_review)`；
        enterprise/auto 走 daemon 权威，`_local_completion_review`
        （`cli/main.py:5969-5973`）仅 local 回落时调用。故断言 RPC 路由契约
        （method + params），而非本地 `db.run_task_completion_review` 调用。
        """
        route_stub.reply("task.completion_review", {
            "decision": "pass", "findings": [], "counts": {}})
        task_id = db.task_create(title="test", steps=[], creator="test")
        result = cli_main._handle_task(["completion-review", task_id], db)
        assert result is True
        assert route_stub.count("task.completion_review") == 1
        assert route_stub.last_params("task.completion_review")["task_id"] == task_id

    def test_completion_review_with_step_id(self, db, route_stub):
        """completion-review --step-id 经 RPC 透传到 daemon

        stale 依据（A 桶：薄客户端 RPC seam）：`cli/main.py:5975-5978` 把
        `opts.step_id` 放入 `route_task_write("task.completion_review", {...})`
        的 params。断言 RPC params 透传而非本地 db 方法调用。
        """
        route_stub.reply("task.completion_review", {
            "decision": "pass", "findings": [], "counts": {}})
        task_id = db.task_create(title="test", steps=[], creator="test")
        cli_main._handle_task(
            ["completion-review", task_id, "--step-id", "S-abc"], db
        )
        assert route_stub.last_params("task.completion_review")["step_id"] == "S-abc"

    def test_completion_review_handles_error(self, db, route_stub):
        """completion-review 对 daemon 回包 error 的处理：fail-closed RC=2

        stale 依据（A 桶：薄客户端 RPC seam）：`cli/main.py:5981-5986` 在
        `"error" in result` 时打印 task_completion_review_failed 并 `sys.exit(2)`
        （GOV-FIX-08：socket 传输失败禁止 RC=0 假成功）。旧期望「返回 True」已过期。
        """
        route_stub.reply("task.completion_review", {"error": "task not found"})
        task_id = db.task_create(title="test", steps=[], creator="test")
        # daemon 返回 error 属传输/权威失败，禁止 RC=0 假成功
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(["completion-review", task_id], db)
        assert exc_info.value.code == 2

    def test_completion_review_handles_missing_db_method(
            self, db, route_stub, monkeypatch):
        """completion-review 的 local 回落在 db 无方法时 fail-closed RC=2

        stale 依据（A 桶：薄客户端 RPC seam）：生产 local 回落（
        `route_task_write` → `_local_completion_review`，`cli/main.py:5969-5973`）
        中 `hasattr(db, "run_task_completion_review")` 为假则返回 `{"error": ...}`，
        handler 于 `cli/main.py:5981-5986` `sys.exit(2)`。旧期望「打印不可用并
        返回 True」已过期（GOV-FIX-08 禁止假成功）。
        """
        # 从 mixin 类上移除方法（影响所有实例的 hasattr 检查）
        from callwarden.db.db_task_quality import TaskQualityMixin
        monkeypatch.delattr(TaskQualityMixin, "run_task_completion_review")
        # 显式启用 local 回落，模拟 daemon 不可达时的既有本地路径
        route_stub.use_fallback = True

        task_id = db.task_create(title="test", steps=[], creator="test")
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_task(["completion-review", task_id], db)
        assert exc_info.value.code == 2

    def test_split_calls_db_task_split(self, db, route_stub, tmp_path):
        """split handler 经 RPC 路由到 task.split（子任务解析后透传）

        stale 依据（A 桶：薄客户端 RPC seam）：生产 `cli/main.py:6019-6075` 的
        split 分支先 `route_task_read("task.status", {...}, _local_task_exists)`
        （:6034）校验任务，再 `route_task_write("task.split", {task_id, subtasks,
        plan_file, identity_policy}, _local_split)`（:6052）走 daemon 权威；
        `db.task_split` 仅在 local 回落时调用（:6049-6050）。故断言 RPC 路由
        与本地计划解析结果（2 个子任务）。
        """
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
        route_stub.reply("task.status", {"task_id": parent_id, "status": "pending"})
        route_stub.reply("task.split", {
            "task_id": parent_id, "status": "pending",
            "subtask_count": 2, "subtasks": ["T-sub-0", "T-sub-1"]})

        result = cli_main._handle_task(
            ["split", parent_id, "--plan", str(plan_file)], db
        )
        assert result is True
        assert route_stub.count("task.split") == 1
        # 应该解析出 2 个子任务并经 params 透传给 daemon
        assert len(route_stub.last_params("task.split")["subtasks"]) == 2

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

    def test_split_task_not_found(self, db, route_stub, tmp_path):
        """split handler 处理 task_id 不存在

        stale 依据（A 桶：薄客户端 RPC seam）：`cli/main.py:6034` 先经
        `route_task_read("task.status", ...)` 校验任务；daemon 回包为空
        （任务不存在）时 :6037-6041 打印 task_not_found 并返回 True。
        旧实现直查本地 `db.task_status`，已过期。
        """
        plan_file = tmp_path / "plan.md"
        plan_file.write_text("## sub\n- edit @ x.py\n", encoding="utf-8")
        route_stub.reply("task.status", {})  # 任务不存在 → 空回包

        captured = io.StringIO()
        with redirect_stdout(captured):
            result = cli_main._handle_task(
                ["split", "T-nonexistent", "--plan", str(plan_file)], db
            )
        assert result is True
        assert route_stub.count("task.status") == 1

    def test_split_no_subtasks_in_plan(self, db, route_stub, tmp_path):
        """split handler 处理 plan 中无子任务的情况

        stale 依据（A 桶：薄客户端 RPC seam）：`cli/main.py:6034` 先经
        `route_task_read("task.status", ...)` 校验任务存在（须预设非空回包），
        随后 :6043-6048 解析计划为空则打印 task_split_no_subtasks。
        """
        parent_id = db.task_create(title="parent", steps=[], creator="test")
        # 空 plan 文件（只有 H1，无 H2 子任务）
        plan_file = tmp_path / "empty.md"
        plan_file.write_text("# 根任务\n只有描述没有子任务\n", encoding="utf-8")
        route_stub.reply("task.status", {"task_id": parent_id, "status": "pending"})

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
        from callwarden.i18n import _load_lang
        zh = _load_lang("zh_CN")
        for key in self.REQUIRED_KEYS:
            assert key in zh, f"zh_CN 缺失 key: {key}"

    def test_subcommand_desc_keys_exist_en(self):
        """en_US 应包含所有 subcommand desc key"""
        from callwarden.i18n import _load_lang
        en = _load_lang("en_US")
        for key in self.REQUIRED_KEYS:
            assert key in en, f"en_US 缺失 key: {key}"

    def test_message_keys_exist_zh(self):
        """zh_CN 应包含所有 messages key"""
        from callwarden.i18n import _load_lang
        zh = _load_lang("zh_CN")
        cli_msgs = zh.get("cli", {}).get("messages", {})
        for key in self.REQUIRED_MSG_KEYS:
            assert key in cli_msgs, f"zh_CN.cli.messages 缺失 key: {key}"

    def test_message_keys_exist_en(self):
        """en_US 应包含所有 messages key"""
        from callwarden.i18n import _load_lang
        en = _load_lang("en_US")
        cli_msgs = en.get("cli", {}).get("messages", {})
        for key in self.REQUIRED_MSG_KEYS:
            assert key in cli_msgs, f"en_US.cli.messages 缺失 key: {key}"

    def test_help_keys_exist_zh(self):
        """zh_CN 应包含所有 help 模板 key"""
        from callwarden.i18n import _load_lang
        zh = _load_lang("zh_CN")
        cli_msgs = zh.get("cli", {}).get("messages", {})
        for key in self.REQUIRED_HELP_KEYS:
            assert key in cli_msgs, f"zh_CN.cli.messages 缺失 help key: {key}"

    def test_help_keys_exist_en(self):
        """en_US 应包含所有 help 模板 key"""
        from callwarden.i18n import _load_lang
        en = _load_lang("en_US")
        cli_msgs = en.get("cli", {}).get("messages", {})
        for key in self.REQUIRED_HELP_KEYS:
            assert key in cli_msgs, f"en_US.cli.messages 缺失 help key: {key}"

    def test_zh_en_keys_aligned(self):
        """zh_CN 和 en_US 的 key 集合应一致"""
        from callwarden.i18n import _load_lang
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
        from callwarden.i18n import set_language, t as _t
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

    def test_split_then_status_tree(self, db, route_stub, tmp_path):
        """split 拆分后 status-tree 可正常渲染

        stale 依据（A 桶：薄客户端 RPC seam）：split 现经
        `route_task_write("task.split", ...)`（`cli/main.py:6052`）由 daemon
        权威写入子任务，本地 DB 不再承载拆分结果，故旧的
        `SELECT COUNT(*) ... WHERE parent_id` 断言已过期；status-tree 走本地
        `_print_task_show`（`cli/main.py:6079` / 定义于 :6657）。
        """
        parent_id = db.task_create(title="parent", steps=[], creator="test")
        plan_file = tmp_path / "plan.md"
        plan_file.write_text(
            "## 子任务1\n- edit @ f1.py\n\n## 子任务2\n- edit @ f2.py\n",
            encoding="utf-8",
        )
        route_stub.reply("task.status", {"task_id": parent_id, "status": "pending"})
        route_stub.reply("task.split", {
            "task_id": parent_id, "status": "pending",
            "subtask_count": 2, "subtasks": ["T-sub-0", "T-sub-1"]})

        # 执行 split：经 daemon RPC 拆分（本地无写入副作用）
        result = cli_main._handle_task(
            ["split", parent_id, "--plan", str(plan_file)], db
        )
        assert result is True
        assert route_stub.count("task.split") == 1
        assert len(route_stub.last_params("task.split")["subtasks"]) == 2

        # status-tree 应能正常显示（不抛异常）
        result = cli_main._handle_task(["status-tree", parent_id], db)
        assert result is True

    def test_completion_review_on_nonexistent_task(self, db, route_stub, capsys):
        """completion-review 对不存在任务的处理

        stale 依据（A 桶：薄客户端 RPC seam）：`cli/main.py:5975` 经
        `route_task_write("task.completion_review", ...)` 交由 daemon 权威处置
        （任务是否存在由 daemon 判定并回包）。以 route_stub 提供回包，断言
        handler 正常返回 True（无本地直连 DB 语义）。
        """
        route_stub.reply("task.completion_review", {
            "decision": "pass", "findings": [], "counts": {}})
        result = cli_main._handle_task(
            ["completion-review", "T-nonexistent"], db
        )
        assert result is True
        assert route_stub.count("task.completion_review") == 1
