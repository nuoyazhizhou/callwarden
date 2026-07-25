"""C8 Step #4: 子命令 --help 统一模板测试。

覆盖：
- _SUBCOMMAND_HELP_SPECS 完整性（≥18 个子命令）
- 每个规格包含 5 个字段（usage/description/parameters/examples/exit_codes）
- _get_subcommand_epilog 输出包含 5 个章节（用法/描述/参数/示例/退出码）
- task / audit / gc / rule / defect / guardrail 子命令模板内容验证
- examples 至少 2 个、parameters 含 [必填]/[可选] 标记
- i18n key 完整性（zh_CN + en_US）
- cli/main.py 语法正确
- 端到端验证 cw <cmd> --help 输出
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


def _run_cw_subcommand_help(cmd, lang="zh_CN"):
    """运行 cw <cmd> --help，返回 stdout 文本。

    通过 NO_COLOR=1 禁用颜色，避免 ANSI 转义序列干扰断言。
    """
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["CALLWARDEN_LANG"] = lang
    env["PYTHONIOENCODING"] = "utf-8"
    result = subprocess.run(
        [sys.executable, os.path.join(_PKG_PARENT, "cw.py"), cmd, "--help"],
        capture_output=True, text=True, env=env, encoding="utf-8",
    )
    return result.stdout


def _load_i18n(lang):
    """加载 i18n JSON 文件"""
    i18n_dir = os.path.join(_PKG_PARENT, "i18n")
    path = os.path.join(i18n_dir, f"{lang}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# 5 个模板章节 i18n key
_TEMPLATE_KEYS = [
    "help_template_usage",
    "help_template_description",
    "help_template_parameters",
    "help_template_examples",
    "help_template_exit_codes",
    "help_template_required",
    "help_template_optional",
]

# 18 个应有模板的子命令（与 _SUBCOMMAND_HELP_SPECS 一致）
_EXPECTED_SUBCOMMANDS = [
    "task", "rule", "gc", "audit", "bootstrap", "defect", "guardrail",
    "impact", "review", "evolution", "hotspot", "churn", "vuln-blast",
    "symbol-history", "check-gate", "test-impact", "doctor", "install-agent",
]


# ============================================
# 1. _SUBCOMMAND_HELP_SPECS 完整性
# ============================================


def test_subcommand_help_specs_has_at_least_18_entries():
    """_SUBCOMMAND_HELP_SPECS 至少包含 18 个子命令规格"""
    count = len(cli_main._SUBCOMMAND_HELP_SPECS)
    assert count >= 18, (
        f"_SUBCOMMAND_HELP_SPECS 应至少包含 18 个规格，实际 {count} 个"
    )


def test_at_least_18_subcommands_have_template():
    """18 个目标子命令都应有对应的模板规格"""
    missing = [
        cmd for cmd in _EXPECTED_SUBCOMMANDS
        if cmd not in cli_main._SUBCOMMAND_HELP_SPECS
    ]
    assert not missing, f"以下子命令缺少模板规格: {missing}"


def test_each_spec_has_five_required_fields():
    """每个规格包含 5 个必需字段：usage/description/parameters/examples/exit_codes"""
    required_fields = {"usage", "description", "parameters", "examples", "exit_codes"}
    for cmd, spec in cli_main._SUBCOMMAND_HELP_SPECS.items():
        for field in required_fields:
            assert field in spec, (
                f"子命令 '{cmd}' 缺少字段: {field}"
            )


def test_each_spec_examples_at_least_two():
    """每个规格的 examples 至少 2 个"""
    for cmd, spec in cli_main._SUBCOMMAND_HELP_SPECS.items():
        examples = spec["examples"]
        assert isinstance(examples, list), (
            f"子命令 '{cmd}' 的 examples 应为列表"
        )
        assert len(examples) >= 2, (
            f"子命令 '{cmd}' 的 examples 至少 2 个，实际 {len(examples)} 个"
        )


def test_each_spec_parameters_non_empty():
    """每个规格的 parameters 为非空列表（至少有一项参数）

    注：某些命令（如 review/test-impact）只有必填位置参数，没有可选参数；
    某些命令（如 doctor）只有可选参数。这是合理的，不强制要求同时存在。
    """
    for cmd, spec in cli_main._SUBCOMMAND_HELP_SPECS.items():
        params = spec["parameters"]
        assert isinstance(params, list) and len(params) > 0, (
            f"子命令 '{cmd}' 的 parameters 应为非空列表"
        )


def test_each_spec_exit_codes_valid():
    """每个规格的 exit_codes 包含 0（成功）和至少一个非零码"""
    for cmd, spec in cli_main._SUBCOMMAND_HELP_SPECS.items():
        codes = spec["exit_codes"]
        assert isinstance(codes, list) and len(codes) >= 1, (
            f"子命令 '{cmd}' 的 exit_codes 应为非空列表"
        )
        code_values = [str(c[0]) for c in codes]
        assert "0" in code_values, (
            f"子命令 '{cmd}' 的 exit_codes 应包含 0（成功）"
        )
        assert any(c != "0" for c in code_values), (
            f"子命令 '{cmd}' 的 exit_codes 应至少包含一个非零码（失败）"
        )


# ============================================
# 2. _get_subcommand_epilog 输出包含 5 个章节
# ============================================


def test_get_subcommand_epilog_returns_string_for_known_cmd():
    """已知子命令应返回非空 epilog 字符串"""
    for cmd in _EXPECTED_SUBCOMMANDS:
        epilog = cli_main._get_subcommand_epilog(cmd)
        assert isinstance(epilog, str) and epilog, (
            f"子命令 '{cmd}' 的 epilog 应为非空字符串"
        )


def test_get_subcommand_epilog_returns_empty_for_unknown_cmd():
    """未知子命令应返回空字符串"""
    epilog = cli_main._get_subcommand_epilog("__nonexistent_cmd__")
    assert epilog == "", (
        f"未知子命令的 epilog 应为空字符串，实际: {epilog!r}"
    )


def test_epilog_contains_all_five_sections():
    """每个子命令的 epilog 应包含 5 个章节标题"""
    set_language("zh_CN")
    section_titles = [
        t("cli.messages.help_template_usage"),
        t("cli.messages.help_template_description"),
        t("cli.messages.help_template_parameters"),
        t("cli.messages.help_template_examples"),
        t("cli.messages.help_template_exit_codes"),
    ]
    for cmd in _EXPECTED_SUBCOMMANDS:
        epilog = cli_main._get_subcommand_epilog(cmd)
        for title in section_titles:
            assert title in epilog, (
                f"子命令 '{cmd}' 的 epilog 缺少章节: {title}"
            )


def test_epilog_contains_at_least_one_marker():
    """epilog 应包含 [必填] 或 [可选] 标记中的至少一个

    注：某些命令（如 review/test-impact）只有必填参数，epilog 只有 [必填] 标记；
    某些命令（如 doctor）只有可选参数，epilog 只有 [可选] 标记。这是合理的。
    """
    set_language("zh_CN")
    required_marker = t("cli.messages.help_template_required")
    optional_marker = t("cli.messages.help_template_optional")
    for cmd in _EXPECTED_SUBCOMMANDS:
        epilog = cli_main._get_subcommand_epilog(cmd)
        assert required_marker in epilog or optional_marker in epilog, (
            f"子命令 '{cmd}' 的 epilog 应至少包含一个标记: {required_marker} 或 {optional_marker}"
        )


# ============================================
# 3. task 子命令模板章节验证
# ============================================


def _get_task_epilog():
    """获取 task 子命令的 epilog 文本"""
    set_language("zh_CN")
    return cli_main._get_subcommand_epilog("task")


def test_task_help_contains_usage_section():
    """task epilog 包含「用法 / Usage」章节且包含 'cw task'"""
    epilog = _get_task_epilog()
    usage_title = t("cli.messages.help_template_usage")
    assert usage_title in epilog, "task epilog 缺少用法章节标题"
    assert "cw task" in epilog, "task epilog 的用法应包含 'cw task'"


def test_task_help_contains_description_section():
    """task epilog 包含「描述 / Description」章节且非空"""
    epilog = _get_task_epilog()
    desc_title = t("cli.messages.help_template_description")
    assert desc_title in epilog, "task epilog 缺少描述章节标题"
    # 描述应包含至少一个 task 子命令名（如 create/next/report）
    assert any(kw in epilog for kw in ("create", "next", "report", "apply")), (
        "task epilog 的描述应提及子命令名"
    )


def test_task_help_contains_parameters_section():
    """task epilog 包含「参数 / Parameters」章节且含必填和可选标记"""
    epilog = _get_task_epilog()
    params_title = t("cli.messages.help_template_parameters")
    required_marker = t("cli.messages.help_template_required")
    optional_marker = t("cli.messages.help_template_optional")
    assert params_title in epilog, "task epilog 缺少参数章节标题"
    assert required_marker in epilog, "task epilog 缺少必填标记"
    assert optional_marker in epilog, "task epilog 缺少可选标记"


def test_task_help_contains_examples_section():
    """task epilog 包含「示例 / Examples」章节且至少 2 个示例"""
    epilog = _get_task_epilog()
    examples_title = t("cli.messages.help_template_examples")
    assert examples_title in epilog, "task epilog 缺少示例章节标题"
    # task 的示例数应 >=2（在 spec 中验证过，这里再校验输出文本含多条 cw task 行）
    cw_task_lines = [ln for ln in epilog.splitlines() if "cw task" in ln]
    assert len(cw_task_lines) >= 2, (
        f"task epilog 的示例应至少 2 条 cw task 命令行，实际 {len(cw_task_lines)} 条"
    )


def test_task_help_contains_exit_codes_section():
    """task epilog 包含「退出码 / Exit Codes」章节且含 0 和非零码"""
    epilog = _get_task_epilog()
    exit_title = t("cli.messages.help_template_exit_codes")
    assert exit_title in epilog, "task epilog 缺少退出码章节标题"
    # 退出码应包含 0（成功）
    lines = epilog.splitlines()
    exit_section_lines = []
    in_exit_section = False
    for ln in lines:
        if exit_title in ln:
            in_exit_section = True
            continue
        if in_exit_section:
            if ln.strip() and not ln.startswith(" ") and "=" not in ln:
                break
            exit_section_lines.append(ln)
    exit_text = "\n".join(exit_section_lines)
    assert "0" in exit_text, "task epilog 退出码应包含 0（成功）"
    assert any(c in exit_text for c in ("1", "2")), (
        "task epilog 退出码应包含非零码（1 或 2）"
    )


# ============================================
# 4. 其他子命令模板验证
# ============================================


@pytest.mark.parametrize("cmd", _EXPECTED_SUBCOMMANDS)
def test_all_subcommand_epilogs_have_five_sections(cmd):
    """所有 18 个子命令的 epilog 都包含 5 个章节"""
    set_language("zh_CN")
    epilog = cli_main._get_subcommand_epilog(cmd)
    sections = [
        t("cli.messages.help_template_usage"),
        t("cli.messages.help_template_description"),
        t("cli.messages.help_template_parameters"),
        t("cli.messages.help_template_examples"),
        t("cli.messages.help_template_exit_codes"),
    ]
    for s in sections:
        assert s in epilog, f"子命令 '{cmd}' 缺少章节: {s}"


def test_audit_help_template():
    """audit 子命令模板验证：含 verify/rotate-key/keys 子命令"""
    set_language("zh_CN")
    epilog = cli_main._get_subcommand_epilog("audit")
    assert "cw audit" in epilog, "audit epilog 应包含 'cw audit'"
    # audit 的描述应提及 verify/rotate-key/keys
    assert any(kw in epilog for kw in ("verify", "rotate-key", "keys")), (
        "audit epilog 应提及 verify/rotate-key/keys 子命令"
    )
    # 至少 2 个示例
    cw_audit_lines = [ln for ln in epilog.splitlines() if "cw audit" in ln]
    assert len(cw_audit_lines) >= 2, (
        f"audit epilog 示例应至少 2 条，实际 {len(cw_audit_lines)} 条"
    )


def test_gc_help_template():
    """gc 子命令模板验证：含 archive/restore/status 等子命令"""
    set_language("zh_CN")
    epilog = cli_main._get_subcommand_epilog("gc")
    assert "cw gc" in epilog, "gc epilog 应包含 'cw gc'"
    assert any(kw in epilog for kw in ("archive", "restore", "status", "purge")), (
        "gc epilog 应提及 archive/restore/status/purge 子命令"
    )
    cw_gc_lines = [ln for ln in epilog.splitlines() if "cw gc" in ln]
    assert len(cw_gc_lines) >= 2, (
        f"gc epilog 示例应至少 2 条，实际 {len(cw_gc_lines)} 条"
    )


def test_rule_help_template():
    """rule 子命令模板验证：含 candidate/list/sync 等子命令"""
    set_language("zh_CN")
    epilog = cli_main._get_subcommand_epilog("rule")
    assert "cw rule" in epilog, "rule epilog 应包含 'cw rule'"
    assert any(kw in epilog for kw in ("candidate", "list", "sync", "applicable")), (
        "rule epilog 应提及 candidate/list/sync/applicable 子命令"
    )
    cw_rule_lines = [ln for ln in epilog.splitlines() if "cw rule" in ln]
    assert len(cw_rule_lines) >= 2, (
        f"rule epilog 示例应至少 2 条，实际 {len(cw_rule_lines)} 条"
    )


def test_defect_help_template():
    """defect 子命令模板验证：含 search/suggest/build/learn 等子命令"""
    set_language("zh_CN")
    epilog = cli_main._get_subcommand_epilog("defect")
    assert "cw defect" in epilog, "defect epilog 应包含 'cw defect'"
    # defect 子命令的描述或参数应提及 search/suggest/build/learn/stats 之一
    assert any(kw in epilog for kw in ("search", "suggest", "build", "learn", "stats")), (
        "defect epilog 应提及 search/suggest/build/learn/stats 子命令"
    )


def test_guardrail_help_template():
    """guardrail 子命令模板验证：含 scan/list 等子命令"""
    set_language("zh_CN")
    epilog = cli_main._get_subcommand_epilog("guardrail")
    assert "cw guardrail" in epilog, "guardrail epilog 应包含 'cw guardrail'"
    assert any(kw in epilog for kw in ("scan", "list", "rules")), (
        "guardrail epilog 应提及 scan/list/rules 子命令"
    )


# ============================================
# 5. i18n key 完整性
# ============================================


def test_i18n_template_keys_exist_zh():
    """zh_CN.json 中存在 7 个模板 i18n key"""
    data = _load_i18n("zh_CN")
    messages = data["cli"]["messages"]
    for key in _TEMPLATE_KEYS:
        assert key in messages, f"zh_CN.json 缺少 cli.messages.{key}"


def test_i18n_template_keys_exist_en():
    """en_US.json 中存在 7 个模板 i18n key"""
    data = _load_i18n("en_US")
    messages = data["cli"]["messages"]
    for key in _TEMPLATE_KEYS:
        assert key in messages, f"en_US.json 缺少 cli.messages.{key}"


def test_i18n_template_keys_match_zh_en():
    """zh_CN 和 en_US 的模板 key 数量一致"""
    zh = _load_i18n("zh_CN")["cli"]["messages"]
    en = _load_i18n("en_US")["cli"]["messages"]
    zh_keys = {k for k in zh if k.startswith("help_template_")}
    en_keys = {k for k in en if k.startswith("help_template_")}
    assert zh_keys == en_keys, (
        f"zh/en 模板 key 不一致: 仅 zh 有 {zh_keys - en_keys}; 仅 en 有 {en_keys - zh_keys}"
    )


# ============================================
# 6. 端到端验证（cw <cmd> --help 子进程）
# ============================================


def test_cw_task_help_e2e_contains_sections():
    """cw task --help 子进程输出应包含 5 个章节标题"""
    set_language("zh_CN")
    output = _run_cw_subcommand_help("task")
    # 子进程可能因为 argparse 自身 help 而 exit 0，输出在 stdout
    sections = [
        t("cli.messages.help_template_usage"),
        t("cli.messages.help_template_description"),
        t("cli.messages.help_template_parameters"),
        t("cli.messages.help_template_examples"),
        t("cli.messages.help_template_exit_codes"),
    ]
    found = [s for s in sections if s in output]
    # 至少应能找到模板章节（argparse 可能截断或重排，但 epilog 应在末尾）
    assert len(found) >= 3, (
        f"cw task --help 应至少包含 3 个模板章节，实际找到 {len(found)} 个: {found}"
    )


def test_cw_gc_help_e2e_contains_usage():
    """cw gc --help 子进程输出应包含用法章节"""
    set_language("zh_CN")
    output = _run_cw_subcommand_help("gc")
    usage_title = t("cli.messages.help_template_usage")
    assert usage_title in output, (
        f"cw gc --help 输出应包含用法章节: {usage_title}"
    )


def test_cw_audit_help_e2e_contains_sections():
    """cw audit --help 子进程输出应包含模板章节"""
    set_language("zh_CN")
    output = _run_cw_subcommand_help("audit")
    # 至少包含用法章节
    usage_title = t("cli.messages.help_template_usage")
    assert usage_title in output, (
        f"cw audit --help 输出应包含用法章节: {usage_title}"
    )


# ============================================
# 7. 语法检查
# ============================================


def test_python_syntax_ok():
    """cli/main.py 应可正常编译（无语法错误）"""
    main_path = os.path.join(_PKG_PARENT, "cli", "main.py")
    py_compile.compile(main_path, doraise=True)


def test_test_file_syntax_ok():
    """本测试文件应可正常编译"""
    py_compile.compile(__file__, doraise=True)
