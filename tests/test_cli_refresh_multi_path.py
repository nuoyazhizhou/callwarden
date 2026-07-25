"""C8 Step #5 测试：--refresh 多 path 支持

验证：
1. --refresh flag 支持 nargs='+' 接收多个 path
2. metavar 改为 'PATH [...]'
3. 多文件时输出汇总（成功数/失败数/总耗时）
4. cw refresh <paths> subcommand 也支持多路径
5. 失败文件被记录并显示
6. i18n key 完整
"""
import json
import os
import sys
import tempfile

import pytest

# 让测试能导入 cli/main.py
_PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from callwarden.cli import main as cli_main
from callwarden.i18n import set_language

set_language("zh_CN")


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------

def _parse(args_list):
    """构造 parser 并解析 args_list，返回 args 对象。"""
    parser = cli_main.create_parser()
    return parser.parse_args(args_list)


# ----------------------------------------------------------------------
# argparse 层：--refresh 接受多 path
# ----------------------------------------------------------------------

def test_refresh_flag_accepts_multiple_paths():
    """--refresh a.py b.py c.py 解析后 args.refresh 为 list[str]。"""
    args = _parse(["--refresh", "a.py", "b.py", "c.py"])
    assert args.refresh == ["a.py", "b.py", "c.py"]


def test_refresh_flag_accepts_single_path():
    """--refresh a.py 仍兼容单 path（list 长度为 1）。"""
    args = _parse(["--refresh", "a.py"])
    assert args.refresh == ["a.py"]


def test_refresh_metavar_is_path_ellipsis():
    """--refresh 的 metavar 改为 'PATH [...]'（提示多 path）。

    验证方式：直接检查 parser 的 action 定义。
    """
    parser = cli_main.create_parser()
    # 找到 --refresh action
    refresh_action = None
    for action in parser._actions:
        if "--refresh" in (action.option_strings or []):
            refresh_action = action
            break
    assert refresh_action is not None, "--refresh flag not found in parser"
    assert refresh_action.metavar == "PATH [...]"
    assert refresh_action.nargs == "+"


# ----------------------------------------------------------------------
# i18n 层
# ----------------------------------------------------------------------

def _load_i18n(lang):
    path = os.path.join(_PROJECT_ROOT, "i18n", f"{lang}.json")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def test_i18n_keys_exist_zh():
    """zh_CN.json 中存在 refresh 多 path 汇总相关 key。"""
    data = _load_i18n("zh_CN")
    messages = data["cli"]["messages"]
    for key in (
        "refresh_done",
        "refresh_failed",
        "refresh_multi_summary",
        "refresh_multi_failed_title",
        "refresh_multi_failed_item",
    ):
        assert key in messages, f"missing key: cli.messages.{key}"


def test_i18n_keys_exist_en():
    """en_US.json 中存在 refresh 多 path 汇总相关 key。"""
    data = _load_i18n("en_US")
    messages = data["cli"]["messages"]
    for key in (
        "refresh_done",
        "refresh_failed",
        "refresh_multi_summary",
        "refresh_multi_failed_title",
        "refresh_multi_failed_item",
    ):
        assert key in messages, f"missing key: cli.messages.{key}"


def test_i18n_placeholders():
    """refresh_multi_summary 占位符完整（success/failure/total/elapsed）。"""
    data = _load_i18n("zh_CN")
    msg = data["cli"]["messages"]["refresh_multi_summary"]
    for placeholder in ("{success}", "{failure}", "{total}", "{elapsed}"):
        assert placeholder in msg, f"missing placeholder: {placeholder}"

    # refresh_failed 含 path 和 error
    msg_failed = data["cli"]["messages"]["refresh_failed"]
    assert "{path}" in msg_failed
    assert "{error}" in msg_failed

    # refresh_multi_failed_item 含 path 和 error
    msg_item = data["cli"]["messages"]["refresh_multi_failed_item"]
    assert "{path}" in msg_item
    assert "{error}" in msg_item


# ----------------------------------------------------------------------
# _handle_refresh 子命令层
# ----------------------------------------------------------------------

class _FakeDb:
    """模拟 CodeGraphDB 的最小接口，用于测试 _handle_refresh。"""

    def __init__(self):
        self.refreshed = []
        self.fail_on = set()  # 这些 path 抛异常

    def refresh_file(self, path):
        if path in self.fail_on:
            raise RuntimeError(f"simulated error: {path}")
        self.refreshed.append(path)

    def build_full_graph(self, force=False):
        pass

    def rule_sync_agents_md(self, target_path="AGENTS.md", dry_run=False, actor=""):
        return {"success": False, "error": "marker not found"}


def test_handle_refresh_multiple_paths(capsys):
    """cw refresh a.py b.py c.py 调用 db.refresh_file 三次。"""
    db = _FakeDb()
    cli_main._handle_refresh(["a.py", "b.py", "c.py"], db)
    assert db.refreshed == ["a.py", "b.py", "c.py"]

    out = capsys.readouterr()
    # 输出每个文件的刷新结果
    assert "Refreshed: a.py" in out.out or "已刷新: a.py" in out.out
    # 多文件时输出汇总
    assert "Refresh summary" in out.out or "刷新汇总" in out.out


def test_handle_refresh_failure_recorded(capsys):
    """失败文件被记录在汇总中。"""
    db = _FakeDb()
    db.fail_on = {"b.py"}
    cli_main._handle_refresh(["a.py", "b.py", "c.py"], db)

    # a.py 和 c.py 成功，b.py 失败
    assert db.refreshed == ["a.py", "c.py"]

    out = capsys.readouterr()
    # 失败信息出现
    assert "Failed" in out.out or "失败" in out.out
    assert "b.py" in out.out


def test_handle_refresh_single_path_no_summary(capsys):
    """单文件不输出汇总（len(paths) <= 1）。"""
    db = _FakeDb()
    cli_main._handle_refresh(["a.py"], db)
    out = capsys.readouterr()
    # 单文件不输出汇总
    assert "Refresh summary" not in out.out and "刷新汇总" not in out.out


def test_handle_refresh_all_flag(capsys):
    """cw refresh --all 走全量刷新路径。"""
    db = _FakeDb()
    cli_main._handle_refresh(["--all"], db)
    # build_full_graph 被调用（refreshed 应为空，因为是全量而非单文件）
    assert db.refreshed == []


def test_handle_refresh_all_force_flag(capsys):
    """cw refresh --all --force 走强制全量刷新。"""
    db = _FakeDb()
    cli_main._handle_refresh(["--all", "--force"], db)
    assert db.refreshed == []


# ----------------------------------------------------------------------
# 兼容性
# ----------------------------------------------------------------------

def test_refresh_flag_still_in_deprecated_mapping():
    """--refresh 仍在 deprecated mapping 中（提示迁移到 cw refresh）。"""
    assert "refresh" in cli_main._DEPRECATED_FLAG_MAPPING


def test_python_syntax_ok():
    """cli/main.py 语法正确。"""
    import subprocess
    result = subprocess.run(
        [sys.executable, "-m", "py_compile", os.path.join(_PROJECT_ROOT, "cli", "main.py")],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
