"""C8 Step #3: 主 --help 输出（12 组分组结构）测试。

覆盖：
- cw --help 输出包含所有 12 个分组标题
- 包含 "Workspace & Database" / "工作区与数据库" 分组
- 包含 deprecated flag 章节
- 包含全局选项章节（--lang/--workspace/--help）
- 包含补全缺失功能（task capture-diff / audit verify / bootstrap status / rule seed-bootstrap）
- 不再包含旧的 4-pillar 字样（"Four Pillars" / "四大支柱"）
- i18n key 完整性（zh_CN + en_US）
- cli/main.py 语法正确
"""

import json
import os
import py_compile
import subprocess
import sys
from unittest import mock

import pytest

# 确保项目根目录在 path 中
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.cli import main as cli_main
from i18n import set_language, t


# ============================================
# 工具函数
# ============================================


def _run_cw_help(lang="zh_CN"):
    """运行 cw --help，返回 stdout 文本（不含颜色转义）。

    通过 NO_COLOR=1 禁用颜色，避免 ANSI 转义序列干扰断言。
    """
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["CALLWARDEN_LANG"] = lang
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, os.path.join(_PKG_PARENT, "cw.py"), "--help"],
        capture_output=True, text=True, env=env, encoding="utf-8",
    )
    return result.stdout


def _load_i18n(lang):
    """加载 i18n JSON 文件"""
    i18n_dir = os.path.join(_PKG_PARENT, "i18n")
    path = os.path.join(i18n_dir, f"{lang}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# 12 个分组标题 i18n key
_GROUP_KEYS = [
    "help_group_workspace",
    "help_group_query",
    "help_group_call_chain",
    "help_group_metrics",
    "help_group_task",
    "help_group_rule",
    "help_group_audit",
    "help_group_git",
    "help_group_semgrep",
    "help_group_coverage",
    "help_group_gc",
    "help_group_diagnostics",
]

# 补全缺失功能的命令字符串（应出现在主 --help 输出中）
_COMPLETION_COMMANDS = [
    "task capture-diff",
    "audit verify",
    "bootstrap status",
    "rule seed-bootstrap",
]


# ============================================
# 1. 12 组分组标题测试
# ============================================


def test_main_help_contains_all_12_groups():
    """cw --help 输出包含所有 12 个分组标题"""
    output = _run_cw_help("zh_CN")
    set_language("zh_CN")
    missing = []
    for key in _GROUP_KEYS:
        expected = t(f"cli.messages.{key}")
        if expected not in output:
            missing.append((key, expected))
    assert not missing, (
        f"主 --help 输出缺少以下分组标题: {missing}"
    )


def test_main_help_contains_workspace_group():
    """cw --help 包含工作区与数据库分组"""
    output_zh = _run_cw_help("zh_CN")
    assert "工作区与数据库" in output_zh, "zh_CN 输出缺少 '工作区与数据库'"

    output_en = _run_cw_help("en_US")
    assert "Workspace & Database" in output_en, "en_US 输出缺少 'Workspace & Database'"


def test_main_help_contains_call_chain_group():
    """cw --help 包含调用链分析分组"""
    output_zh = _run_cw_help("zh_CN")
    assert "调用链分析" in output_zh, "zh_CN 输出缺少 '调用链分析'"

    output_en = _run_cw_help("en_US")
    assert "Call Chain Analysis" in output_en, "en_US 输出缺少 'Call Chain Analysis'"


def test_main_help_contains_gc_group():
    """cw --help 包含 GC 分组"""
    output_zh = _run_cw_help("zh_CN")
    assert "GC" in output_zh, "zh_CN 输出应包含 'GC'"

    output_en = _run_cw_help("en_US")
    assert "Garbage Collection" in output_en, "en_US 输出应包含 'Garbage Collection'"


# ============================================
# 2. deprecated flag 章节测试
# ============================================


def test_main_help_contains_deprecated_section():
    """cw --help 包含 deprecated flag 章节"""
    output_zh = _run_cw_help("zh_CN")
    assert "已废弃" in output_zh or "Deprecated" in output_zh, (
        "zh_CN 输出缺少 deprecated 章节"
    )

    output_en = _run_cw_help("en_US")
    assert "Deprecated" in output_en, "en_US 输出缺少 'Deprecated' 章节"


def test_main_help_contains_deprecated_arrow():
    """deprecated 章节包含 -> 指向替代 subcommand 的箭头"""
    output = _run_cw_help("en_US")
    assert "-> cw " in output, (
        "deprecated 章节应包含 '-> cw <subcommand>' 指向替代命令"
    )


def test_main_help_contains_deprecated_more_count():
    """deprecated 章节包含剩余数量提示（'... and N more deprecated flags'）"""
    output = _run_cw_help("en_US")
    # _DEPRECATED_FLAG_MAPPING 有 60+ entry，剩余 50+ 个
    assert "more deprecated flags" in output, (
        "应包含剩余 deprecated flag 数量提示"
    )


# ============================================
# 3. 全局选项章节测试
# ============================================


def test_main_help_contains_global_options():
    """cw --help 包含全局选项章节（--lang LANG / --workspace ROOT）"""
    output = _run_cw_help("en_US")
    assert "--lang LANG" in output, "缺少 '--lang LANG' 全局选项"
    assert "--workspace ROOT" in output, "缺少 '--workspace ROOT' 全局选项"
    assert "-h, --help" in output, "缺少 '-h, --help' 全局选项"


def test_main_help_contains_global_options_zh():
    """cw --help (zh_CN) 包含全局选项章节"""
    output = _run_cw_help("zh_CN")
    assert "--lang LANG" in output
    assert "--workspace ROOT" in output
    assert "全局选项" in output


# ============================================
# 4. 补全缺失功能测试
# ============================================


@pytest.mark.parametrize("cmd_str", _COMPLETION_COMMANDS)
def test_main_help_contains_completion_commands(cmd_str):
    """cw --help 包含补全的缺失功能命令（task capture-diff 等）"""
    output = _run_cw_help("en_US")
    assert cmd_str in output, (
        f"主 --help 输出应包含补全的命令 '{cmd_str}'"
    )


def test_main_help_contains_task_capture_diff():
    """cw --help 包含 task capture-diff"""
    output = _run_cw_help("en_US")
    assert "task capture-diff" in output


def test_main_help_contains_audit_verify():
    """cw --help 包含 audit verify"""
    output = _run_cw_help("en_US")
    assert "audit verify" in output


def test_main_help_contains_bootstrap_status():
    """cw --help 包含 bootstrap status"""
    output = _run_cw_help("en_US")
    assert "bootstrap status" in output


def test_main_help_contains_rule_seed_bootstrap():
    """cw --help 包含 rule seed-bootstrap"""
    output = _run_cw_help("en_US")
    assert "rule seed-bootstrap" in output


# ============================================
# 5. 不再包含旧的 4-pillar 字样
# ============================================


def test_main_help_no_legacy_four_pillars():
    """cw --help 不再包含旧的 4-pillar 字样（'Four Pillars' / '四大支柱'）"""
    output_en = _run_cw_help("en_US")
    assert "Four Pillars" not in output_en, (
        "主 --help 不应再包含 'Four Pillars' 字样（已替换为 12 组结构）"
    )
    assert "Code Warden Architecture Subcommands" not in output_en, (
        "主 --help 不应再包含旧的 'Code Warden Architecture Subcommands' 标题"
    )

    output_zh = _run_cw_help("zh_CN")
    assert "四大支柱" not in output_zh, (
        "主 --help 不应再包含 '四大支柱' 字样"
    )
    assert "代码守护者架构子命令" not in output_zh, (
        "主 --help 不应再包含旧的 '代码守护者架构子命令' 标题"
    )


# ============================================
# 6. i18n key 完整性
# ============================================


# 主 --help 所需的所有 i18n key（messages 对象内）
_REQUIRED_I18N_KEYS = [
    "main_help_title",
    "main_help_intro",
    # 12 组分组标题
    "help_group_workspace",
    "help_group_query",
    "help_group_call_chain",
    "help_group_metrics",
    "help_group_task",
    "help_group_rule",
    "help_group_audit",
    "help_group_git",
    "help_group_semgrep",
    "help_group_coverage",
    "help_group_gc",
    "help_group_diagnostics",
    # deprecated 章节
    "help_deprecated_title",
    "help_deprecated_intro",
    "help_deprecated_more",
    # 全局选项
    "help_global_options_title",
    "help_lang",
    "help_workspace_root",
    "help_root",
    "help_help",
    "help_footer",
    # 补全缺失功能的 help key
    "help_task_capture_diff",
    "help_audit_verify",
    "help_bootstrap_status",
    "help_rule_seed_bootstrap",
]


def test_i18n_keys_exist_zh():
    """zh_CN.json 包含所有新 i18n key"""
    data = _load_i18n("zh_CN")
    messages = data["cli"]["messages"]
    missing = [k for k in _REQUIRED_I18N_KEYS if k not in messages]
    assert not missing, (
        f"zh_CN.json 缺少以下 i18n key: {missing}"
    )


def test_i18n_keys_exist_en():
    """en_US.json 包含所有新 i18n key"""
    data = _load_i18n("en_US")
    messages = data["cli"]["messages"]
    missing = [k for k in _REQUIRED_I18N_KEYS if k not in messages]
    assert not missing, (
        f"en_US.json 缺少以下 i18n key: {missing}"
    )


def test_i18n_deprecated_more_has_placeholder():
    """help_deprecated_more 包含 {count} 占位符"""
    for lang in ("zh_CN", "en_US"):
        data = _load_i18n(lang)
        msg = data["cli"]["messages"]["help_deprecated_more"]
        assert "{count}" in msg, (
            f"{lang}.json help_deprecated_more 缺少 {{count}} 占位符: {msg!r}"
        )


def test_i18n_keys_bilingual():
    """zh_CN 和 en_US 的 i18n key 集合一致（无遗漏）"""
    zh_data = _load_i18n("zh_CN")["cli"]["messages"]
    en_data = _load_i18n("en_US")["cli"]["messages"]
    zh_keys = {k for k in zh_data if k.startswith("help_") or k.startswith("main_help")}
    en_keys = {k for k in en_data if k.startswith("help_") or k.startswith("main_help")}
    assert zh_keys == en_keys, (
        f"zh_CN 和 en_US 的 help_/main_help i18n key 不一致: "
        f"仅 zh 有 {zh_keys - en_keys}, 仅 en 有 {en_keys - zh_keys}"
    )


# ============================================
# 7. _print_main_help 函数行为测试
# ============================================


def test_print_main_help_callable(capsys):
    """_print_main_help() 可被独立调用且输出非空"""
    set_language("en_US")
    cli_main._print_main_help()
    captured = capsys.readouterr()
    assert captured.out, "_print_main_help 输出不应为空"
    assert "Call Warden CLI" in captured.out


def test_print_main_help_returns_no_exception(capsys):
    """_print_main_help() 不抛异常"""
    set_language("zh_CN")
    try:
        cli_main._print_main_help()
    except Exception as e:
        pytest.fail(f"_print_main_help 抛出异常: {e}")


def test_main_help_groups_data_structure():
    """_MAIN_HELP_GROUPS 数据结构正确（12 组，每组非空）"""
    assert len(cli_main._MAIN_HELP_GROUPS) == 12, (
        f"应有 12 组，实际 {len(cli_main._MAIN_HELP_GROUPS)}"
    )
    for group_title_key, items in cli_main._MAIN_HELP_GROUPS:
        assert isinstance(group_title_key, str)
        assert group_title_key.startswith("cli.messages.help_group_")
        assert isinstance(items, list) and len(items) >= 2, (
            f"组 {group_title_key} 至少应有 2 个命令，实际 {len(items)}"
        )
        for cmd, desc_key in items:
            assert isinstance(cmd, str) and cmd
            assert desc_key.startswith("cli.messages.help_")


# ============================================
# 8. cli/main.py 语法正确
# ============================================


def test_python_syntax_ok():
    """cli/main.py 语法正确（py_compile 通过）"""
    main_path = os.path.join(_PKG_PARENT, "cli", "main.py")
    py_compile.compile(main_path, doraise=True)


# ============================================
# 9. 主入口 main() 调用 _print_main_help
# ============================================


def test_main_calls_print_main_help_on_help(monkeypatch, capsys):
    """cw --help 走 main() 时应调用 _print_main_help 并 return（不进入 argparse）"""
    call_count = {"count": 0}

    def fake_print_help():
        call_count["count"] += 1
        print("FAKE_MAIN_HELP_OUTPUT")

    monkeypatch.setattr(cli_main, "_print_main_help", fake_print_help)

    old_argv = sys.argv
    sys.argv = ["cw", "--help"]
    try:
        cli_main.main()
    finally:
        sys.argv = old_argv

    assert call_count["count"] == 1, (
        "main() 应在 --help 时调用一次 _print_main_help"
    )
    captured = capsys.readouterr()
    assert "FAKE_MAIN_HELP_OUTPUT" in captured.out
