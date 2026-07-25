"""post-commit hook 与 task_capture_diff 集成测试（C4 Step #2）

验证：
1. _post_commit_hook 生成正确内容（含 marker、cw 命令、exit 0 守护）
2. task_id 硬编码模式 vs 环境变量模式
3. install_post_commit_hook 写入文件且可执行
4. install_post_commit_hook 幂等：重复安装不报错
5. install_post_commit_hook 卸载只删除 Call Warden 标记的 hook
6. install_post_commit_hook 卸载时不破坏非 Call Warden 的 hook
7. CLI cw install-hook post-commit 命令分发正确
8. CLI --uninstall 卸载路径
9. i18n key 存在
"""
import os
import stat
import subprocess
import tempfile
from contextlib import contextmanager

import pytest

from callwarden.install import CallWardenInstaller


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------

def _init_git_repo(root):
    """初始化 git 仓库（无 commit，仅 .git 目录）。"""
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@test.com"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@test.com"
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True, env=env)
    return os.path.join(root, ".git")


@contextmanager
def _chdir_ctx(root):
    """chdir 上下文管理器，确保在 root 目录下执行代码块。"""
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(old_cwd)


def _installer_in(root):
    """在 root 目录下工作的 installer 实例（chdir 仅在构造期间生效）。

    注意：后续调用 install_post_commit_hook 时仍需在 root 目录下执行，
    请使用 `with _chdir_ctx(root):` 包裹调用代码。
    """
    with _chdir_ctx(root):
        return CallWardenInstaller()


# ----------------------------------------------------------------------
# _post_commit_hook 内容生成
# ----------------------------------------------------------------------

def test_post_commit_hook_envvar_mode_contains_marker():
    """生成的 post-commit hook 必须包含 Call Warden marker（用于幂等更新）。"""
    installer = CallWardenInstaller()
    content = installer._post_commit_hook(task_id="")
    assert installer._hook_marker() in content, "hook 必须包含 marker 标记"


def test_post_commit_hook_auto_mode_contains_cw_command():
    """hook 脚本必须包含调用 cw task capture-diff --auto 的命令。"""
    installer = CallWardenInstaller()
    content = installer._post_commit_hook(task_id="")
    assert "task capture-diff" in content
    assert "--auto" in content


def test_post_commit_hook_auto_mode_no_envvar_dependency():
    """--auto 模式不依赖 CALLWARDEN_TASK_ID 环境变量。"""
    installer = CallWardenInstaller()
    content = installer._post_commit_hook(task_id="")
    # 不应有 CALLWARDEN_TASK_ID 检查（--auto 自动检测 in_progress 任务）
    assert "CALLWARDEN_TASK_ID" not in content
    # 不应有静默 exit 0（--auto 模式总是尝试执行）
    assert "exit 0" not in content


def test_post_commit_hook_auto_mode_fail_soft_with_or_true():
    """hook 末尾必须用 || true 兜底退出码，确保不影响 git commit。"""
    installer = CallWardenInstaller()
    content = installer._post_commit_hook(task_id="")
    assert "|| true" in content, "hook 必须用 || true 兜底 fail-soft"


def test_post_commit_hook_hardcoded_mode_includes_task_id():
    """硬编码 task_id 模式：脚本内容包含指定的 task_id。"""
    installer = CallWardenInstaller()
    content = installer._post_commit_hook(task_id="T-test-12345")
    assert "T-test-12345" in content
    # 不应该有 CALLWARDEN_TASK_ID 检查（因为 task_id 已硬编码）
    assert "exit 0" not in content or "|| true" in content


def test_post_commit_hook_hardcoded_mode_no_envvar_check():
    """硬编码模式下不检查环境变量。"""
    installer = CallWardenInstaller()
    content = installer._post_commit_hook(task_id="T-xxx")
    # 不应有 if [ -z "${CALLWARDEN_TASK_ID:-}" ]; then exit 0; fi 守护
    assert content.count("CALLWARDEN_TASK_ID") == 0


def test_post_commit_hook_sets_pythonioencoding():
    """hook 设置 PYTHONIOENCODING=utf-8 避免 Windows 中文编码问题。"""
    installer = CallWardenInstaller()
    content = installer._post_commit_hook(task_id="")
    assert "PYTHONIOENCODING" in content
    assert "utf-8" in content


# ----------------------------------------------------------------------
# install_post_commit_hook
# ----------------------------------------------------------------------

def test_install_post_commit_hook_creates_executable_file():
    """install_post_commit_hook 在 .git/hooks/post-commit 创建可执行文件。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = _installer_in(root)
        with _chdir_ctx(root):
            ok = installer.install_post_commit_hook()
        assert ok is True

        hook_path = os.path.join(root, ".git", "hooks", "post-commit")
        assert os.path.exists(hook_path), "post-commit hook 文件应被创建"
        # 可执行权限检查（仅 Linux/macOS；Windows NTFS 不保留 exec bit，
        # git for windows 通过 sh 调用 hook 不依赖此位）
        if os.name != "nt":
            mode = os.stat(hook_path).st_mode
            assert mode & stat.S_IXUSR, "post-commit hook 必须有用户可执行权限"


def test_install_post_commit_hook_with_hardcoded_task_id():
    """install_post_commit_hook(--task-id X) 写入硬编码 task_id 的脚本。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = _installer_in(root)
        with _chdir_ctx(root):
            ok = installer.install_post_commit_hook(task_id="T-hardcode-1")
        assert ok is True

        hook_path = os.path.join(root, ".git", "hooks", "post-commit")
        with open(hook_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "T-hardcode-1" in content


def test_install_post_commit_hook_idempotent():
    """重复安装 post-commit hook 是幂等的（marker 保护）。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = _installer_in(root)

        with _chdir_ctx(root):
            ok1 = installer.install_post_commit_hook()
        assert ok1 is True
        hook_path = os.path.join(root, ".git", "hooks", "post-commit")
        with open(hook_path, "r", encoding="utf-8") as f:
            content1 = f.read()

        # 第二次安装（应覆盖，因为 marker 已存在）
        with _chdir_ctx(root):
            ok2 = installer.install_post_commit_hook()
        assert ok2 is True
        with open(hook_path, "r", encoding="utf-8") as f:
            content2 = f.read()

        # 内容应一致
        assert content1 == content2


def test_install_post_commit_hook_no_git_dir_returns_false():
    """不在 git 仓库内调用 install_post_commit_hook 返回 False。"""
    with tempfile.TemporaryDirectory() as root:
        # 没有 git init
        installer = _installer_in(root)
        with _chdir_ctx(root):
            ok = installer.install_post_commit_hook()
        assert ok is False


def test_uninstall_post_commit_hook_removes_cw_hook():
    """卸载时只删除 Call Warden 生成的 hook。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = _installer_in(root)

        with _chdir_ctx(root):
            installer.install_post_commit_hook()
        hook_path = os.path.join(root, ".git", "hooks", "post-commit")
        assert os.path.exists(hook_path)

        with _chdir_ctx(root):
            ok = installer.install_post_commit_hook(uninstall=True)
        assert ok is True
        assert not os.path.exists(hook_path), "卸载后文件应被删除"


def test_uninstall_post_commit_hook_preserves_non_cw_hook():
    """卸载时不破坏用户自定义的 hook（无 marker 时跳过）。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        hook_path = os.path.join(root, ".git", "hooks", "post-commit")
        os.makedirs(os.path.dirname(hook_path), exist_ok=True)
        # 写入用户自定义 hook（无 marker）
        with open(hook_path, "w", encoding="utf-8") as f:
            f.write("#!/bin/sh\necho user-defined\n")

        installer = _installer_in(root)
        with _chdir_ctx(root):
            ok = installer.install_post_commit_hook(uninstall=True)
        assert ok is True

        # 文件应仍存在（不被删除）
        assert os.path.exists(hook_path), "用户自定义 hook 应被保留"


def test_uninstall_post_commit_hook_no_file():
    """卸载时 hook 文件不存在也算成功（幂等）。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = _installer_in(root)
        with _chdir_ctx(root):
            ok = installer.install_post_commit_hook(uninstall=True)
        assert ok is True


# ----------------------------------------------------------------------
# CLI cw install-hook post-commit
# ----------------------------------------------------------------------

def test_cli_install_hook_post_commit_dispatches():
    """cw install-hook post-commit 正确调用 installer。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        old_cwd = os.getcwd()
        os.chdir(root)
        try:
            called = {"count": 0, "task_id": None, "uninstall": None}

            def fake_install(self, task_id="", uninstall=False):
                called["count"] += 1
                called["task_id"] = task_id
                called["uninstall"] = uninstall
                return True

            with mock.patch.object(CallWardenInstaller, "install_post_commit_hook", fake_install):
                old_argv = sys.argv
                sys.argv = ["cw", "install-hook", "post-commit", "--task-id", "T-cli-1"]
                try:
                    ret = cli_main._handle_install_hook(
                        ["post-commit", "--task-id", "T-cli-1"], None
                    )
                except SystemExit:
                    pass
                finally:
                    sys.argv = old_argv

            assert called["count"] == 1
            assert called["task_id"] == "T-cli-1"
            assert called["uninstall"] is False
            assert ret is True
        finally:
            os.chdir(old_cwd)


def test_cli_install_hook_uninstall_dispatches():
    """cw install-hook post-commit --uninstall 正确调用 installer。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        old_cwd = os.getcwd()
        os.chdir(root)
        try:
            called = {"uninstall": None}

            def fake_install(self, task_id="", uninstall=False):
                called["uninstall"] = uninstall
                return True

            with mock.patch.object(CallWardenInstaller, "install_post_commit_hook", fake_install):
                old_argv = sys.argv
                sys.argv = ["cw", "install-hook", "post-commit", "--uninstall"]
                try:
                    cli_main._handle_install_hook(
                        ["post-commit", "--uninstall"], None
                    )
                except SystemExit:
                    pass
                finally:
                    sys.argv = old_argv

            assert called["uninstall"] is True
        finally:
            os.chdir(old_cwd)


def test_cli_install_hook_no_git_returns_false():
    """不在 git 仓库内调用 cw install-hook 返回 False 但不报错。"""
    import sys
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as root:
        old_cwd = os.getcwd()
        os.chdir(root)
        try:
            old_argv = sys.argv
            sys.argv = ["cw", "install-hook", "post-commit"]
            try:
                ret = cli_main._handle_install_hook(["post-commit"], None)
            except SystemExit:
                pass
            finally:
                sys.argv = old_argv

            # 没有 .git 目录时应返回 False
            assert ret is False
        finally:
            os.chdir(old_cwd)


# ----------------------------------------------------------------------
# i18n key 存在性
# ----------------------------------------------------------------------

def test_i18n_keys_for_install_hook_exist():
    """zh_CN.json / en_US.json 包含 install-hook 相关的所有 i18n key。"""
    import json
    from i18n import _get_i18n_dir

    for lang_file in ("zh_CN.json", "en_US.json"):
        with open(os.path.join(_get_i18n_dir(), lang_file), encoding="utf-8") as f:
            data = json.load(f)
        messages = data["cli"]["messages"]
        required = [
            "install_hook_desc",
            "install_hook_arg_hook",
            "install_hook_arg_task_id",
            "install_hook_arg_uninstall",
            "install_hook_uninstalled",
            "install_hook_skip_non_cw",
            "install_hook_not_found",
            "install_hook_installed",
        ]
        for key in required:
            assert key in messages, f"{lang_file} 缺少 i18n key: cli.messages.{key}"
