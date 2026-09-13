"""i18n 闭环：清理残留 print 语句测试（C5 遗留）

验证：
1. 新增的 i18n key 在 zh_CN.json / en_US.json 中存在
2. 修复的预存在 key（stdlib_import_done / external_import_done）现在有翻译
3. 代码中不再有未走 i18n 的 f-string print（db_stdlib / db_external / bootstrap_check）
"""
import ast
import json
import os
import re

import pytest

from callwarden.i18n import _get_i18n_dir


# ----------------------------------------------------------------------
# i18n key 存在性
# ----------------------------------------------------------------------

@pytest.mark.parametrize("lang_file", ["zh_CN.json", "en_US.json"])
def test_new_i18n_keys_exist(lang_file):
    """新增的 i18n key 必须在两种语言文件中存在。"""
    with open(os.path.join(_get_i18n_dir(), lang_file), encoding="utf-8") as f:
        data = json.load(f)
    messages = data["cli"]["messages"]
    required_keys = [
        "bootstrap_gate_reason_detail",
        "stdlib_import_lang_summary",
        "external_import_lang_failed",
        "external_import_pkg_failed",
    ]
    for key in required_keys:
        assert key in messages, f"{lang_file} 缺少 i18n key: cli.messages.{key}"


@pytest.mark.parametrize("lang_file", ["zh_CN.json", "en_US.json"])
def test_fixed_preexisting_keys_have_translation(lang_file):
    """修复的预存在 key（stdlib_import_done / external_import_done）现在必须有翻译。

    之前这两个 key 在代码中调用但未在 i18n 文件中定义，t() 会返回 key 本身。
    """
    with open(os.path.join(_get_i18n_dir(), lang_file), encoding="utf-8") as f:
        data = json.load(f)
    messages = data["cli"]["messages"]
    for key in ["stdlib_import_done", "external_import_done"]:
        assert key in messages, f"{lang_file} 仍缺少预存在 key: cli.messages.{key}"
        # 翻译值不应等于 key 本身（说明未翻译）
        assert messages[key] != key, f"{lang_file} 的 {key} 值等于 key 本身（未翻译）"


# ----------------------------------------------------------------------
# 代码中不再有未走 i18n 的 f-string print
# ----------------------------------------------------------------------

def _extract_fstring_prints(filepath: str):
    """提取文件中所有 print(f"...") 调用的 f-string 内容。

    返回 list of (line_no, f_string_content)。
    """
    with open(filepath, "r", encoding="utf-8") as f:
        source = f.read()
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    results = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name):
            continue
        if node.func.id != "print":
            continue
        for arg in node.args:
            if isinstance(arg, ast.JoinedStr):
                # f-string，检查是否包含 t() 调用
                has_t_call = any(
                    isinstance(v, ast.FormattedValue) and
                    isinstance(v.value, ast.Call) and
                    isinstance(v.value.func, ast.Name) and
                    v.value.func.id == "t"
                    for v in arg.values
                )
                if not has_t_call:
                    results.append((node.lineno, ast.unparse(arg)))
    return results


def test_db_stdlib_no_raw_fstring_print():
    """db_stdlib.py 中不应有未走 t() 的 f-string print。"""
    filepath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "db", "db_stdlib.py"
    )
    raw_prints = _extract_fstring_prints(filepath)
    assert not raw_prints, f"db_stdlib.py 仍有未走 t() 的 f-string print: {raw_prints}"


def test_db_external_no_raw_fstring_print():
    """db_external.py 中不应有未走 t() 的 f-string print。"""
    filepath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "db", "db_external.py"
    )
    raw_prints = _extract_fstring_prints(filepath)
    assert not raw_prints, f"db_external.py 仍有未走 t() 的 f-string print: {raw_prints}"


def test_bootstrap_check_no_raw_fstring_print():
    """cicd/bootstrap_check.py 中不应有未走 t() 的 f-string print。"""
    filepath = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "cicd", "bootstrap_check.py"
    )
    raw_prints = _extract_fstring_prints(filepath)
    assert not raw_prints, f"bootstrap_check.py 仍有未走 t() 的 f-string print: {raw_prints}"


# ----------------------------------------------------------------------
# i18n key 翻译值正确性
# ----------------------------------------------------------------------

def test_i18n_key_translations_contain_placeholders():
    """i18n key 的翻译值必须包含正确的占位符。"""
    for lang_file in ("zh_CN.json", "en_US.json"):
        with open(os.path.join(_get_i18n_dir(), lang_file), encoding="utf-8") as f:
            data = json.load(f)
        messages = data["cli"]["messages"]
        # 检查占位符
        checks = [
            ("stdlib_import_done", ["{created}", "{skipped}"]),
            ("stdlib_import_lang_summary", ["{lang}", "{count}"]),
            ("external_import_done", ["{created}", "{skipped}"]),
            ("external_import_lang_failed", ["{lang}", "{error}"]),
            ("external_import_pkg_failed", ["{lang}", "{package}", "{error}"]),
            ("bootstrap_gate_reason_detail", ["{reason}"]),
            ("entry_point_sqlite_error", ["{cw_py}", "{args}"]),
        ]
        for key, placeholders in checks:
            value = messages.get(key, "")
            for ph in placeholders:
                assert ph in value, f"{lang_file} 的 {key} 缺少占位符 {ph}: {value}"


# ----------------------------------------------------------------------
# cw.py 入口错误提示已走 i18n
# ----------------------------------------------------------------------

def test_cw_entry_point_uses_t():
    """cw.py 的入口提示应通过 t() 获取翻译文本。

    stale 修正：
    - 依据 cw.py:138，现为相对导入 `from .i18n import t`（不再是绝对导入）。
    - 依据 cw.py:18-21 T04 收敛，`_check_entry_point_sqlite` 与
      `entry_point_sqlite_error` 已随「移除 entry_point sqlite 检测」删除；
      现存的 t() 调用为 `t("cli_test_usage")`（cw.py:139）。
    """
    cw_py = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "cw.py",
    )
    with open(cw_py, "r", encoding="utf-8") as f:
        source = f.read()
    # 必须包含 t() 调用（相对导入）
    assert "from .i18n import t" in source, \
        "cw.py 应通过 from .i18n import t 获取翻译"
    assert 't("cli_test_usage")' in source, \
        "cw.py 应通过 t() 获取入口提示译文"
    # 不应再硬编码中文错误文案
    assert '"错误：通过 cw.exe 启动时' not in source, \
        "cw.py 不应再硬编码中文错误文案"


def test_cw_entry_point_handles_encoding():
    """cw.py 入口应保证 stdout/stderr UTF-8（Windows GBK 终端兼容）。

    stale 修正：编码处理实现已收敛（cw.py:47-53 委托
    cli/console.py:63-81 的 ensure_utf8_output()）；cw.py 自身不再出现
    `sys.stderr.encoding` 字面量，也无 `buffer.write` 兜底分支（改用
    `getattr(stream, "reconfigure", None)` 探测，见 console.py:76-81）。
    """
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    cw_py = os.path.join(root, "cw.py")
    console_py = os.path.join(root, "cli", "console.py")
    with open(cw_py, "r", encoding="utf-8") as f:
        source = f.read()
    with open(console_py, "r", encoding="utf-8") as f:
        console_source = f.read()
    # cw.py 入口必须委托统一实现
    assert "ensure_utf8_output" in source, \
        "cw.py 应调用 cli.console.ensure_utf8_output() 统一处理编码"
    # 统一实现必须 reconfigure(encoding='utf-8', errors='replace')
    assert "reconfigure" in console_source, \
        "cli/console.py 应使用 reconfigure(encoding='utf-8') 处理非 utf-8 终端"
    assert "errors=\"replace\"" in console_source or "errors='replace'" in console_source, \
        "cli/console.py reconfigure 应使用 errors='replace' 避免抛异常"
    # frozen 分支仍直接 reconfigure stderr（cw.py:85/:94）
    assert "sys.stderr.reconfigure" in source, \
        "cw.py frozen 分支应 reconfigure stderr"
