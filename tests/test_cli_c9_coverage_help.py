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
import re
import sys
import tempfile
from contextlib import redirect_stdout

import pytest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.cli import main as cli_main
from callwarden.cli.categories import COMMAND_CATEGORIES, all_command_names
from callwarden.i18n import set_language
from callwarden.db import CodeGraphDB

set_language("zh_CN")


def _main_help_text() -> str:
    """渲染主 --help 文本（T10 Phase 2 契约：COMMAND_CATEGORIES → _print_main_help）。

    stale 依据（T-1791511742091-064b1c40 族B）：_MAIN_HELP_GROUPS 静态块已删除，
    主 --help 由 cli/main.py:141 _print_main_help() 从 cli/categories.py 渲染，
    断言目标改为渲染后的 help 文本（用户实际所见）。
    """
    buf = io.StringIO()
    with redirect_stdout(buf):
        cli_main._print_main_help()
    return buf.getvalue()


# _handle_coverage 实际注册的子命令（cli/main.py:12139-12156，
# 含 T10 阶段2.5 重新并入的 test）
REGISTERED_COVERAGE_SUBCOMMANDS = {"import", "fn", "uncovered", "test"}


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
    """验证 coverage comment/uncommented 不再出现在主 --help 渲染输出中

    stale 修正（族B）：断言目标从 _MAIN_HELP_GROUPS 静态块改为渲染后的
    主 --help 文本（_print_main_help 输出）。
    """

    def test_coverage_comment_removed(self):
        """主 --help 不应列出 'coverage comment'"""
        text = _main_help_text()
        assert "coverage comment" not in text, \
            "主 --help 仍包含 'coverage comment'"

    def test_coverage_uncommented_removed(self):
        """主 --help 不应列出 'coverage uncommented'"""
        text = _main_help_text()
        assert "coverage uncommented" not in text, \
            "主 --help 仍包含 'coverage uncommented'"

    def test_coverage_test_removed(self):
        """主 --help 顶层命令面不应有 'coverage test' 行（test 是 coverage 的
        argparse 子命令，主 help 只列顶层命令）"""
        text = _main_help_text()
        assert not re.search(r"^  coverage test\b", text, re.M), \
            "主 --help 不应列出 'coverage test' 顶层命令行"

    def test_coverage_import_kept(self):
        """coverage 命令行应保留并列出 import 子命令"""
        text = _main_help_text()
        assert re.search(r"^  coverage\s{2,}.*\bimport\b", text, re.M), \
            "主 --help coverage 行应列出 import 子命令"

    def test_coverage_fn_kept(self):
        """coverage 命令行应保留并列出 fn 子命令"""
        text = _main_help_text()
        assert re.search(r"^  coverage\s{2,}.*\bfn\b", text, re.M), \
            "主 --help coverage 行应列出 fn 子命令"

    def test_coverage_uncovered_kept(self):
        """coverage 命令行应保留并列出 uncovered 子命令"""
        text = _main_help_text()
        assert re.search(r"^  coverage\s{2,}.*\buncovered\b", text, re.M), \
            "主 --help coverage 行应列出 uncovered 子命令"

    def test_coverage_group_has_5_items(self):
        """coverage 命令归 coverage_ownership 分类，desc 列出的子命令集与
        argparse 注册集一致（不超前声明）

        stale 修正：旧断言按 _MAIN_HELP_GROUPS「coverage 分组 5 项」计数；
        新结构下子命令级真相源是 argparse（见 TestRegisteredSubcommands），
        categories desc 只须与注册集一致。
        """
        cat = next(c for c in COMMAND_CATEGORIES if c.key == "coverage_ownership")
        cov = next(c for c in cat.commands if c.name == "coverage")
        inner = cov.default_desc.split("（", 1)[1].rstrip("）")
        listed = {tok.strip() for tok in inner.split("/")}
        assert listed == REGISTERED_COVERAGE_SUBCOMMANDS, (
            f"coverage desc 子命令集 {sorted(listed)} 应与注册集 "
            f"{sorted(REGISTERED_COVERAGE_SUBCOMMANDS)} 一致"
        )


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

    def test_coverage_test_registered(self, db, capsys):
        """coverage test 自 T10 阶段2.5 重新注册（原 --test-coverage flag 并入，
        cli/main.py:12154-12156），应正常执行而非报 invalid choice"""
        cli_main._handle_coverage(["test"], db)  # 不应抛 SystemExit
        assert "测试覆盖率统计" in capsys.readouterr().out


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
        from callwarden.i18n import _load_lang
        zh = _load_lang("zh_CN")
        cli_msgs = zh.get("cli", {}).get("messages", {})
        for key in self.RETAINED_KEYS:
            assert key in cli_msgs, f"zh_CN 不应删除 key: {key}"

    def test_keys_retained_en(self):
        """en_US 应保留 3 个 key"""
        from callwarden.i18n import _load_lang
        en = _load_lang("en_US")
        cli_msgs = en.get("cli", {}).get("messages", {})
        for key in self.RETAINED_KEYS:
            assert key in cli_msgs, f"en_US 不应删除 key: {key}"

    def test_keys_have_nonempty_text_zh(self):
        """zh_CN 保留的 key 应有非空文本"""
        from callwarden.i18n import _load_lang, t
        zh = _load_lang("zh_CN")
        cli_msgs = zh.get("cli", {}).get("messages", {})
        for key in self.RETAINED_KEYS:
            text = cli_msgs.get(key, "")
            assert text, f"zh_CN key '{key}' 文本为空"

    def test_keys_have_nonempty_text_en(self):
        """en_US 保留的 key 应有非空文本"""
        from callwarden.i18n import _load_lang
        en = _load_lang("en_US")
        cli_msgs = en.get("cli", {}).get("messages", {})
        for key in self.RETAINED_KEYS:
            text = cli_msgs.get(key, "")
            assert text, f"en_US key '{key}' 文本为空"


# ============================================
# 5. help 模板与实际注册的全量一致性
# ============================================


class TestHelpTemplateConsistency:
    """全量交叉验证：categories 真相源列出的 coverage 子命令都应已注册

    C9 核心理念（help/文档不超前声明未注册子命令）在新结构下的等价物：
    主 --help 由 COMMAND_CATEGORIES 渲染，coverage 命令 default_desc 列出
    子命令清单，校验该清单与 argparse 实际注册集一致。
    """

    def test_all_coverage_help_items_are_registered(self):
        """coverage desc 列出的子命令都必须已注册"""
        cat = next(c for c in COMMAND_CATEGORIES if c.key == "coverage_ownership")
        cov = next(c for c in cat.commands if c.name == "coverage")
        inner = cov.default_desc.split("（", 1)[1].rstrip("）")
        listed = {tok.strip() for tok in inner.split("/")}
        unregistered = listed - REGISTERED_COVERAGE_SUBCOMMANDS
        assert not unregistered, \
            f"coverage desc 列出未注册子命令: {unregistered}"

    def test_help_template_msg_keys_resolve(self):
        """coverage_ownership 分类中所有 desc_key 应可解析（en_US）"""
        from callwarden.i18n import set_language, t as _t
        set_language("en_US")
        try:
            cat = next(c for c in COMMAND_CATEGORIES
                       if c.key == "coverage_ownership")
            for cmd in cat.commands:
                if cmd.desc_key:
                    text = _t(cmd.desc_key, default="")
                    assert text, f"无法解析 msg_key: {cmd.desc_key}"
        finally:
            set_language("zh_CN")


# ============================================
# 6. 等价功能可通过其他命令访问
# ============================================


class TestEquivalentFunctionalityAvailable:
    """被移除子命令的等价功能仍可访问（T10 Phase 2 后契约更新）

    stale 修正：comment-coverage / uncommented / test-impact 已从 metrics
    flag / 附带子命令升格为 coverage_ownership 分类下的顶层命令
    （cli/categories.py coverage_ownership.commands），主 --help 直接列出。
    """

    EQUIV_COMMANDS = ["comment-coverage", "uncommented", "test-impact"]

    @pytest.mark.parametrize("cmd_name", EQUIV_COMMANDS)
    def test_equiv_command_is_top_level(self, cmd_name):
        """等价命令已在顶层命令真相源（categories）注册"""
        assert cmd_name in all_command_names()

    @pytest.mark.parametrize("cmd_name", EQUIV_COMMANDS)
    def test_equiv_command_in_main_help(self, cmd_name):
        """等价命令已在主 --help 列出"""
        text = _main_help_text()
        assert re.search(rf"^  {re.escape(cmd_name)}\s", text, re.M), \
            f"主 --help 应列出顶层命令 {cmd_name}"
