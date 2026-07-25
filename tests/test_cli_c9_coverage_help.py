"""C9-2: coverage help 模板与子命令注册一致性测试。

修复 C8 遗留问题：help 模板声明了 coverage comment/uncommented/test 三个子命令，
但 argparse 实际未注册（功能由 metrics 分组的 --comment-coverage/--uncommented flag
和 test-impact 子命令提供）。从 help 模板移除这三项。

覆盖：
1. help 模板不再列出 coverage comment/uncommented/test
2. 保留的 coverage 子命令（import/fn/uncovered）确实已注册为 argparse 子命令
3. 被移除项的 i18n key 仍保留（兼容性，不破坏旧调用方）
4. 端到端：被移除的子命令调用应报 invalid choice（argparse 标准行为）
5. 端到端：保留的子命令能正常执行 --help
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stdout

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.cli import main as cli_main
from i18n import set_language
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
# 1. help 模板移除验证
# ============================================


class TestHelpTemplateRemoved:
    """验证 coverage comment/uncommented/test 已从 help 模板移除"""

    def _collect_help_cmds(self):
        """收集 _MAIN_HELP_GROUPS 中所有命令文本"""
        cmds = []
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            for cmd, msg_key in items:
                cmds.append(cmd)
        return cmds

    def test_coverage_comment_removed(self):
        """help 模板不应再列出 'coverage comment'"""
        cmds = self._collect_help_cmds()
        assert not any("coverage comment" == c for c in cmds), \
            "help 模板仍包含 'coverage comment'"
        # 更严格：不应有任何以 "coverage comment" 开头的项
        assert not any(c.startswith("coverage comment") for c in cmds), \
            "help 模板仍包含以 'coverage comment' 开头的项"

    def test_coverage_uncommented_removed(self):
        """help 模板不应再列出 'coverage uncommented'"""
        cmds = self._collect_help_cmds()
        assert not any(c.startswith("coverage uncommented") for c in cmds), \
            "help 模板仍包含 'coverage uncommented'"

    def test_coverage_test_removed(self):
        """help 模板不应再列出 'coverage test'"""
        cmds = self._collect_help_cmds()
        assert not any(c.startswith("coverage test") for c in cmds), \
            "help 模板仍包含 'coverage test'"

    def test_coverage_import_kept(self):
        """help 模板应保留 'coverage import'（已注册）"""
        cmds = self._collect_help_cmds()
        assert any(c.startswith("coverage import") for c in cmds)

    def test_coverage_fn_kept(self):
        """help 模板应保留 'coverage fn'（已注册）"""
        cmds = self._collect_help_cmds()
        assert any(c.startswith("coverage fn") for c in cmds)

    def test_coverage_uncovered_kept(self):
        """help 模板应保留 'coverage uncovered'（已注册）"""
        cmds = self._collect_help_cmds()
        assert any(c.startswith("coverage uncovered") for c in cmds)

    def test_coverage_group_has_5_items(self):
        """coverage 分组应剩 5 项（原 8 项移除 3 项）"""
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            if group_title == "cli.messages.help_group_coverage":
                assert len(items) == 5, \
                    f"coverage 分组应有 5 项，实际 {len(items)} 项"
                return
        pytest.fail("未找到 coverage help 分组")


# ============================================
# 2. 保留的子命令确实已注册
# ============================================


class TestRegisteredSubcommands:
    """验证保留在 help 模板中的 coverage 子命令确实已注册为 argparse 子命令"""

    def test_coverage_import_registered(self, db):
        """coverage import 应可被 argparse 识别"""
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_coverage(["import", "--help"], db)
        assert exc_info.value.code == 0

    def test_coverage_fn_registered(self, db):
        """coverage fn 应可被 argparse 识别"""
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_coverage(["fn", "--help"], db)
        assert exc_info.value.code == 0

    def test_coverage_uncovered_registered(self, db):
        """coverage uncovered 应可被 argparse 识别"""
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_coverage(["uncovered", "--help"], db)
        assert exc_info.value.code == 0


# ============================================
# 3. 被移除的子命令调用应报 invalid choice
# ============================================


class TestRemovedSubcommandsReject:
    """验证移除的子命令调用时 argparse 报错（invalid choice）"""

    def test_coverage_comment_rejected(self, db):
        """coverage comment 应报 invalid choice（SystemExit 2）"""
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_coverage(["comment"], db)
        assert exc_info.value.code == 2

    def test_coverage_uncommented_rejected(self, db):
        """coverage uncommented 应报 invalid choice"""
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_coverage(["uncommented"], db)
        assert exc_info.value.code == 2

    def test_coverage_test_rejected(self, db):
        """coverage test 应报 invalid choice"""
        with pytest.raises(SystemExit) as exc_info:
            cli_main._handle_coverage(["test"], db)
        assert exc_info.value.code == 2


# ============================================
# 4. i18n key 兼容性保留
# ============================================


class TestI18nKeysRetained:
    """验证被移除子命令的 i18n key 仍保留（兼容性）"""

    RETAINED_KEYS = [
        "help_coverage_comment",
        "help_coverage_uncommented",
        "help_coverage_test",
    ]

    def test_keys_retained_zh(self):
        """zh_CN 应保留 3 个 key"""
        from i18n import _load_lang
        zh = _load_lang("zh_CN")
        cli_msgs = zh.get("cli", {}).get("messages", {})
        for key in self.RETAINED_KEYS:
            assert key in cli_msgs, f"zh_CN 不应删除 key: {key}"

    def test_keys_retained_en(self):
        """en_US 应保留 3 个 key"""
        from i18n import _load_lang
        en = _load_lang("en_US")
        cli_msgs = en.get("cli", {}).get("messages", {})
        for key in self.RETAINED_KEYS:
            assert key in cli_msgs, f"en_US 不应删除 key: {key}"

    def test_keys_have_nonempty_text_zh(self):
        """zh_CN 保留的 key 应有非空文本"""
        from i18n import _load_lang, t
        zh = _load_lang("zh_CN")
        cli_msgs = zh.get("cli", {}).get("messages", {})
        for key in self.RETAINED_KEYS:
            text = cli_msgs.get(key, "")
            assert text, f"zh_CN key '{key}' 文本为空"

    def test_keys_have_nonempty_text_en(self):
        """en_US 保留的 key 应有非空文本"""
        from i18n import _load_lang
        en = _load_lang("en_US")
        cli_msgs = en.get("cli", {}).get("messages", {})
        for key in self.RETAINED_KEYS:
            text = cli_msgs.get(key, "")
            assert text, f"en_US key '{key}' 文本为空"


# ============================================
# 5. help 模板与实际注册的全量一致性
# ============================================


class TestHelpTemplateConsistency:
    """全量交叉验证：所有 help 模板中列出的 coverage 子命令都应已注册

    这是 C9 任务的核心理念：help 模板不应超前声明未注册的子命令。
    遍历 _MAIN_HELP_GROUPS 中所有 "coverage xxx" 形式的项，
    验证 xxx 是 _handle_coverage 中实际注册的子命令。
    """

    # _handle_coverage 中实际注册的子命令（从 cli/main.py 源码提取）
    REGISTERED_COVERAGE_SUBCOMMANDS = {"import", "fn", "uncovered"}

    def test_all_coverage_help_items_are_registered(self):
        """所有 help 模板中的 coverage xxx 项都应已注册"""
        unregistered = []
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            for cmd, msg_key in items:
                cmd = cmd.strip()
                # 匹配 "coverage <subcommand>" 形式
                if cmd.startswith("coverage "):
                    # 提取子命令名（取第一个 token，忽略后续参数）
                    parts = cmd.split()
                    if len(parts) >= 2:
                        sub_name = parts[1]
                        # 跳过带 <...> 占位符的（如 "coverage import <FILE>"）
                        # sub_name 是 "import"，不是 "<FILE>"
                        if sub_name not in self.REGISTERED_COVERAGE_SUBCOMMANDS:
                            unregistered.append(cmd)
        assert not unregistered, \
            f"以下 coverage help 项未注册为子命令: {unregistered}"

    def test_help_template_msg_keys_resolve(self):
        """所有保留的 help 模板 msg_key 应可解析"""
        from i18n import set_language, t as _t
        set_language("en_US")
        try:
            for group_title, items in cli_main._MAIN_HELP_GROUPS:
                if group_title == "cli.messages.help_group_coverage":
                    for cmd, msg_key in items:
                        text = _t(msg_key, default="")
                        assert text, f"无法解析 msg_key: {msg_key}"
        finally:
            set_language("zh_CN")


# ============================================
# 6. 等价功能可通过其他命令访问
# ============================================


class TestEquivalentFunctionalityAvailable:
    """验证被移除子命令的等价功能仍可通过其他命令访问

    - coverage comment → metrics 分组的 --comment-coverage flag
    - coverage uncommented → metrics 分组的 --uncommented flag
    - coverage test → test-impact 子命令
    """

    def test_comment_coverage_flag_in_help(self):
        """comment-coverage 等价功能应在 help 模板中列出（metrics 分组）"""
        cmds = []
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            for cmd, msg_key in items:
                cmds.append(cmd)
        # metrics 分组应包含 comment-coverage（help 模板用无 -- 前缀的名称）
        assert any("comment-coverage" in c for c in cmds), \
            "metrics 分组应列出 comment-coverage 等价功能"

    def test_uncommented_flag_in_help(self):
        """uncommented 等价功能应在 help 模板中列出（metrics 分组）"""
        cmds = []
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            for cmd, msg_key in items:
                cmds.append(cmd)
        # metrics 分组应包含 uncommented
        assert any("uncommented" in c for c in cmds), \
            "help 模板应列出 uncommented 等价功能"

    def test_test_impact_in_help(self):
        """test-impact 子命令应在 help 模板中列出"""
        cmds = []
        for group_title, items in cli_main._MAIN_HELP_GROUPS:
            for cmd, msg_key in items:
                cmds.append(cmd)
        assert any("test-impact" in c for c in cmds), \
            "help 模板应列出 test-impact 子命令"
