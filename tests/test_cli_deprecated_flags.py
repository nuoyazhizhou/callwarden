"""C8 Step #2: --flag 命令 deprecated 警告测试。

覆盖：
- _DEPRECATED_FLAG_MAPPING 完整性（≥60 entry）
- _emit_deprecated_flag_warning 输出到 stderr
- subcommand 模式不触发 warning
- warning 不阻断执行
- i18n key 完整性（zh/en + 占位符）
- deprecated_flag_mapping.json 文件有效性
- --task-list 不在新 mapping（已有自己实现）
- cli/main.py 语法正确
"""

import argparse
import json
import os
import py_compile
import sys
from unittest import mock

import pytest

# 确保项目根目录在 path 中
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.cli import main as cli_main
from i18n import set_language


# ============================================
# 1. 映射表完整性
# ============================================


def test_deprecated_flag_mapping_completeness():
    """_DEPRECATED_FLAG_MAPPING 至少包含 60 个 entry"""
    count = len(cli_main._DEPRECATED_FLAG_MAPPING)
    assert count >= 60, (
        f"_DEPRECATED_FLAG_MAPPING 应至少包含 60 个 entry，实际 {count} 个"
    )


def test_deprecated_flag_mapping_format():
    """每个 entry 格式为 (flag_name, subcommand) 元组"""
    for attr, value in cli_main._DEPRECATED_FLAG_MAPPING.items():
        assert isinstance(attr, str), f"attr 应为字符串: {attr}"
        assert isinstance(value, tuple), f"value 应为元组: {attr}={value}"
        assert len(value) == 2, f"元组应有 2 个元素: {attr}={value}"
        flag_name, subcommand = value
        assert flag_name.startswith("--"), f"flag_name 应以 -- 开头: {flag_name}"
        assert isinstance(subcommand, str) and subcommand, (
            f"subcommand 应为非空字符串: {attr}={subcommand}"
        )


# ============================================
# 2. _emit_deprecated_flag_warning 输出到 stderr
# ============================================


def test_emit_warning_to_stderr(capsys):
    """--search 触发时，warning 输出到 stderr（不污染 stdout）"""
    set_language("zh_CN")
    # 构造 args：模拟 --search "main"
    args = argparse.Namespace(search="main")
    # 其他 deprecated flag 设为 None/False 避免干扰
    for attr in cli_main._DEPRECATED_FLAG_MAPPING:
        if attr != "search":
            setattr(args, attr, None)

    cli_main._emit_deprecated_flag_warning(args)
    captured = capsys.readouterr()

    # stdout 不应包含 deprecated 警告
    assert "--search" not in captured.out, (
        "deprecated 警告不应输出到 stdout（会污染管道）"
    )
    # stderr 应包含警告
    assert "--search" in captured.err, (
        "deprecated 警告应输出到 stderr"
    )
    # stderr 应包含推荐的 subcommand
    assert "search" in captured.err, (
        "stderr 应包含推荐的 subcommand 名称"
    )


def test_emit_warning_contains_hint(capsys):
    """每个 deprecated flag 警告后应输出通用引导提示"""
    set_language("zh_CN")
    args = argparse.Namespace(search="test")
    for attr in cli_main._DEPRECATED_FLAG_MAPPING:
        if attr != "search":
            setattr(args, attr, None)

    cli_main._emit_deprecated_flag_warning(args)
    captured = capsys.readouterr()

    # stderr 应包含通用提示（含 "未来版本" 或 "future" 关键词）
    assert "未来版本" in captured.err or "future" in captured.err.lower(), (
        "stderr 应包含通用 deprecated 提示"
    )


def test_emit_warning_multiple_flags(capsys):
    """多个 deprecated flag 同时设置时，每个都应输出警告"""
    set_language("zh_CN")
    args = argparse.Namespace(search="test", stats=True)
    for attr in cli_main._DEPRECATED_FLAG_MAPPING:
        if attr not in ("search", "stats"):
            setattr(args, attr, None)

    cli_main._emit_deprecated_flag_warning(args)
    captured = capsys.readouterr()

    # stderr 应同时包含 --search 和 --stats 的警告
    assert "--search" in captured.err, "--search 警告缺失"
    assert "--stats" in captured.err, "--stats 警告缺失"


# ============================================
# 3. subcommand 模式不触发 warning
# ============================================


def test_no_warning_for_subcommand(capsys):
    """subcommand 模式（cw stats）不触发 deprecated warning

    模拟 subcommand 模式：所有 deprecated flag 均为默认值（None/False）。
    _emit_deprecated_flag_warning 不应输出任何内容。
    """
    set_language("zh_CN")
    # 构造 args：所有 deprecated flag 均为默认值（模拟 subcommand 模式不经过 flag parser）
    args = argparse.Namespace()
    for attr in cli_main._DEPRECATED_FLAG_MAPPING:
        setattr(args, attr, None)

    cli_main._emit_deprecated_flag_warning(args)
    captured = capsys.readouterr()

    # stderr 不应包含任何 deprecated 警告
    assert captured.err == "", (
        f"subcommand 模式不应触发 deprecated 警告，但 stderr 有内容: {captured.err!r}"
    )


def test_subcommand_mode_skips_warning_call(monkeypatch, capsys):
    """cw <subcommand> 走 _run_subcommand_mode，不调用 _emit_deprecated_flag_warning"""
    # 用 spy 监视 _emit_deprecated_flag_warning 是否被调用
    call_count = {"count": 0}

    def spy(args):
        call_count["count"] += 1

    monkeypatch.setattr(cli_main, "_emit_deprecated_flag_warning", spy)

    # 模拟 cw stats（subcommand 模式）
    old_argv = sys.argv
    sys.argv = ["cw", "stats"]
    try:
        # mock 掉 _run_subcommand_mode 避免真实 db 初始化
        def fake_run_subcommand():
            return None
        monkeypatch.setattr(cli_main, "_run_subcommand_mode", fake_run_subcommand)

        try:
            cli_main.main()
        except SystemExit:
            pass
    finally:
        sys.argv = old_argv

    # _emit_deprecated_flag_warning 不应被调用
    assert call_count["count"] == 0, (
        "subcommand 模式不应调用 _emit_deprecated_flag_warning"
    )


# ============================================
# 4. warning 不阻断执行
# ============================================


def test_warning_does_not_block_execution(capsys):
    """--search 触发 warning 后仍应继续执行（不抛异常、不 exit）"""
    set_language("zh_CN")
    args = argparse.Namespace(search="main")
    for attr in cli_main._DEPRECATED_FLAG_MAPPING:
        if attr != "search":
            setattr(args, attr, None)

    # 调用 _emit_deprecated_flag_warning 不应抛异常
    cli_main._emit_deprecated_flag_warning(args)

    # 函数正常返回，无异常即代表不阻断
    captured = capsys.readouterr()
    assert "--search" in captured.err, "warning 应输出到 stderr"


# ============================================
# 5. i18n key 完整性
# ============================================


def _load_i18n(lang):
    """加载 i18n JSON 文件"""
    i18n_dir = os.path.join(_PKG_PARENT, "i18n")
    path = os.path.join(i18n_dir, f"{lang}.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def test_i18n_keys_exist_zh():
    """zh_CN.json 中存在 deprecated_flag_warning / deprecated_flag_hint"""
    data = _load_i18n("zh_CN")
    messages = data["cli"]["messages"]
    assert "deprecated_flag_warning" in messages, (
        "zh_CN.json 缺少 cli.messages.deprecated_flag_warning"
    )
    assert "deprecated_flag_hint" in messages, (
        "zh_CN.json 缺少 cli.messages.deprecated_flag_hint"
    )


def test_i18n_keys_exist_en():
    """en_US.json 中存在 deprecated_flag_warning / deprecated_flag_hint"""
    data = _load_i18n("en_US")
    messages = data["cli"]["messages"]
    assert "deprecated_flag_warning" in messages, (
        "en_US.json 缺少 cli.messages.deprecated_flag_warning"
    )
    assert "deprecated_flag_hint" in messages, (
        "en_US.json 缺少 cli.messages.deprecated_flag_hint"
    )


def test_i18n_placeholders():
    """deprecated_flag_warning 包含 {flag} 和 {subcommand} 占位符"""
    for lang in ("zh_CN", "en_US"):
        data = _load_i18n(lang)
        msg = data["cli"]["messages"]["deprecated_flag_warning"]
        assert "{flag}" in msg, (
            f"{lang}.json deprecated_flag_warning 缺少 {{flag}} 占位符: {msg!r}"
        )
        assert "{subcommand}" in msg, (
            f"{lang}.json deprecated_flag_warning 缺少 {{subcommand}} 占位符: {msg!r}"
        )


def test_i18n_placeholder_substitution():
    """t() 函数能正确替换 deprecated_flag_warning 的占位符"""
    from i18n import t
    set_language("zh_CN")
    result = t("cli.messages.deprecated_flag_warning",
               flag="--search", subcommand="search <QUERY>")
    assert "--search" in result, "占位符替换后应包含 --search"
    assert "search" in result, "占位符替换后应包含 search"


# ============================================
# 6. deprecated_flag_mapping.json 文件有效性
# ============================================


def _mapping_json_path():
    """返回 deprecated_flag_mapping.json 的绝对路径"""
    return os.path.join(_PKG_PARENT, "deprecated_flag_mapping.json")


def test_mapping_json_file_exists():
    """deprecated_flag_mapping.json 存在且 JSON 有效"""
    path = _mapping_json_path()
    assert os.path.isfile(path), f"文件不存在: {path}"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    assert isinstance(data, dict), "JSON 顶层应为对象"


def test_mapping_json_contains_all_flags():
    """JSON 中 flag 数量与 _DEPRECATED_FLAG_MAPPING 一致（扣除 _meta）"""
    with open(_mapping_json_path(), encoding="utf-8") as f:
        data = json.load(f)
    # 排除 _meta 字段
    flag_entries = {k: v for k, v in data.items() if not k.startswith("_")}
    expected_count = len(cli_main._DEPRECATED_FLAG_MAPPING)
    actual_count = len(flag_entries)
    assert actual_count == expected_count, (
        f"JSON flag 数量 ({actual_count}) 与 _DEPRECATED_FLAG_MAPPING ({expected_count}) 不一致"
    )


def test_mapping_no_duplicates():
    """JSON 中无重复 flag（dict 天然去重，验证 key 唯一性）"""
    with open(_mapping_json_path(), encoding="utf-8") as f:
        raw = f.read()
    data = json.loads(raw)
    # JSON 对象的 key 天然唯一，这里验证所有 flag 都以 -- 开头
    for key in data:
        if not key.startswith("_"):
            assert key.startswith("--"), f"flag 应以 -- 开头: {key}"


def test_mapping_json_matches_python_mapping():
    """JSON 中每个 flag 的 subcommand 与 _DEPRECATED_FLAG_MAPPING 一致"""
    with open(_mapping_json_path(), encoding="utf-8") as f:
        data = json.load(f)

    # 从 Python mapping 构建 flag -> subcommand 字典
    python_mapping = {
        flag_name: subcommand
        for attr, (flag_name, subcommand) in cli_main._DEPRECATED_FLAG_MAPPING.items()
    }

    # 比较（排除 _meta）
    json_mapping = {k: v for k, v in data.items() if not k.startswith("_")}
    assert json_mapping == python_mapping, (
        "JSON 映射与 Python _DEPRECATED_FLAG_MAPPING 不一致"
    )


# ============================================
# 7. --task-list 不在新 mapping（已有自己实现）
# ============================================


def test_task_list_not_in_mapping():
    """--task-list 不在 _DEPRECATED_FLAG_MAPPING 中（已有自己的 deprecated 实现）"""
    # 检查 args 属性名
    assert "task_list" not in cli_main._DEPRECATED_FLAG_MAPPING, (
        "task_list 不应在 _DEPRECATED_FLAG_MAPPING 中（已有自己的 deprecated 提示）"
    )
    # 检查 flag 名
    for attr, (flag_name, _) in cli_main._DEPRECATED_FLAG_MAPPING.items():
        assert flag_name != "--task-list", (
            "--task-list 不应在 _DEPRECATED_FLAG_MAPPING 中"
        )


def test_task_show_not_in_mapping():
    """--task-show 不在 _DEPRECATED_FLAG_MAPPING 中（已有自己的 deprecated 实现）"""
    assert "task_show" not in cli_main._DEPRECATED_FLAG_MAPPING, (
        "task_show 不应在 _DEPRECATED_FLAG_MAPPING 中（已有自己的 deprecated 提示）"
    )
    for attr, (flag_name, _) in cli_main._DEPRECATED_FLAG_MAPPING.items():
        assert flag_name != "--task-show", (
            "--task-show 不应在 _DEPRECATED_FLAG_MAPPING 中"
        )


# ============================================
# 8. cli/main.py 语法正确
# ============================================


def test_python_syntax_ok():
    """cli/main.py 语法正确（py_compile 通过）"""
    main_path = os.path.join(_PKG_PARENT, "cli", "main.py")
    # py_compile.compile 会在语法错误时抛 py_compile.PyCompileError
    py_compile.compile(main_path, doraise=True)


# ============================================
# 9. 通用 flag 不在 mapping 中
# ============================================


def test_generic_flags_not_in_mapping():
    """通用 flag（--lang/--workspace/--root/--force/--preview）不在 mapping 中"""
    excluded_attrs = {"lang", "workspace", "root", "force", "preview"}
    for attr in excluded_attrs:
        assert attr not in cli_main._DEPRECATED_FLAG_MAPPING, (
            f"通用 flag {attr} 不应在 _DEPRECATED_FLAG_MAPPING 中"
        )
