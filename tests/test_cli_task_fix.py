"""CLI 任务命令修复测试。

覆盖 cli-task-fix-plan.md 中 5 个 bug 修复：
1. cw task --help 不卡 db 初始化
2. --task-list 与 task list 行为一致
3. task list 显示父子树形结构
4. --task-show 显示子任务
5. 全量回归通过
"""

import os
import sys
import tempfile
from unittest import mock

import pytest

# 确保项目根目录在 path 中
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB
from callwarden.cli import main as cli_main


# ============================================
# Step 1: --help 不卡 db
# ============================================


def test_help_no_db_init():
    """cw task --help 不应该初始化 CodeGraphDB

    通过 mock CodeGraphDB.__init__ 让它抛异常，验证 --help 路径不会触达。
    """
    # 模拟 cw task --help
    old_argv = sys.argv
    sys.argv = ["cw", "task", "--help"]
    try:
        db_init_called = {"count": 0}

        # 替换 CodeGraphDB.__init__ 让它抛异常，验证 --help 不会触达
        original_init = CodeGraphDB.__init__

        def fake_init(self, *args, **kwargs):
            db_init_called["count"] += 1
            raise RuntimeError("CodeGraphDB.__init__ should not be called for --help")

        with mock.patch.object(CodeGraphDB, "__init__", fake_init):
            with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                # 应该返回 0 而不抛 RuntimeError
                try:
                    cli_main._run_subcommand_mode()
                except RuntimeError as e:
                    if "should not be called" in str(e):
                        pytest.fail(
                            "CodeGraphDB.__init__ was called during cw task --help, "
                            "indicating --help path still touches db"
                        )
                    raise
        assert db_init_called["count"] == 0, "CodeGraphDB.__init__ was called"
    finally:
        sys.argv = old_argv


def test_help_subcommand_with_h():
    """cw task list -h 也不应初始化 db"""
    old_argv = sys.argv
    sys.argv = ["cw", "task", "list", "-h"]
    try:
        db_init_called = {"count": 0}

        def fake_init(self, *args, **kwargs):
            db_init_called["count"] += 1
            raise RuntimeError("db should not be initialized for -h")

        with mock.patch.object(CodeGraphDB, "__init__", fake_init):
            with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                try:
                    cli_main._run_subcommand_mode()
                except RuntimeError as e:
                    if "db should not" in str(e):
                        pytest.fail("db initialized during cw task list -h")
                    raise
        assert db_init_called["count"] == 0
    finally:
        sys.argv = old_argv


def test_help_other_subcommands_no_db():
    """cw gc --help / cw guardrail --help 也不应初始化 db"""
    for sub_args in (["cw", "gc", "--help"], ["cw", "guardrail", "--help"]):
        old_argv = sys.argv
        sys.argv = sub_args
        try:
            db_init_called = {"count": 0}

            def fake_init(self, *args, **kwargs):
                db_init_called["count"] += 1
                raise RuntimeError("no db for help")

            with mock.patch.object(CodeGraphDB, "__init__", fake_init):
                with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                    try:
                        cli_main._run_subcommand_mode()
                    except RuntimeError as e:
                        if "no db for help" in str(e):
                            pytest.fail(f"db initialized during {sub_args}")
                        raise
            assert db_init_called["count"] == 0
        finally:
            sys.argv = old_argv
