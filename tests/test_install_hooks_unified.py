"""P3: 统一 install-hook 与 install --hooks 测试

验证：
1. install_hooks() 默认安装三种 hook（pre-commit + pre-push + post-commit）
2. install_hooks(with_post_commit=False) 仅安装两种（pre-commit + pre-push）
3. install_hooks() 安装的 post-commit 使用 --auto 模式（无 CALLWARDEN_TASK_ID 依赖）
4. install_hooks() 幂等：重复安装不报错
5. install_hooks() 保护用户自定义 hook（非 Call Warden marker）
6. install_hooks(force=True) 强制覆盖用户自定义 hook
7. CLI `cw install --hooks` 分发正确参数（with_post_commit=True）
8. CLI `cw install --hooks --no-post-commit` 分发 with_post_commit=False
9. i18n key 存在（install_hooks / install_force_hooks / install_no_post_commit / install_hook_task_id_auto）
10. 旧 i18n key install_hook_task_id_envvar 已删除
"""
import json
import os
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from unittest import mock

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
    """chdir 上下文管理器。"""
    old_cwd = os.getcwd()
    os.chdir(root)
    try:
        yield
    finally:
        os.chdir(old_cwd)


def _hook_path(root, name):
    return os.path.join(root, ".git", "hooks", name)


def _read_hook(root, name):
    p = _hook_path(root, name)
    if not os.path.exists(p):
        return None
    with open(p, "r", encoding="utf-8") as f:
        return f.read()


# ----------------------------------------------------------------------
# install_hooks() 默认安装三种 hook
# ----------------------------------------------------------------------

def test_install_hooks_default_installs_three_hooks():
    """install_hooks() 默认安装 pre-commit + pre-push + post-commit 三种 hook。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = CallWardenInstaller()
        with _chdir_ctx(root):
            installer.install_hooks()

        for name in ("pre-commit", "pre-push", "post-commit"):
            p = _hook_path(root, name)
            assert os.path.exists(p), f"{name} hook 应被安装"
            content = _read_hook(root, name)
            assert content is not None
            assert installer._hook_marker() in content, f"{name} hook 必须包含 marker"


def test_install_hooks_default_post_commit_uses_auto_mode():
    """install_hooks() 安装的 post-commit 必须使用 --auto 模式（无 CALLWARDEN_TASK_ID 依赖）。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = CallWardenInstaller()
        with _chdir_ctx(root):
            installer.install_hooks()

        content = _read_hook(root, "post-commit")
        assert content is not None
        assert "--auto" in content, "post-commit 必须使用 --auto 模式"
        assert "CALLWARDEN_TASK_ID" not in content, "post-commit 不应依赖 CALLWARDEN_TASK_ID 环境变量"
        assert "|| true" in content, "post-commit 必须用 || true 兜底 fail-soft"


def test_install_hooks_with_post_commit_false_skips_post_commit():
    """install_hooks(with_post_commit=False) 仅安装 pre-commit + pre-push。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = CallWardenInstaller()
        with _chdir_ctx(root):
            installer.install_hooks(with_post_commit=False)

        for name in ("pre-commit", "pre-push"):
            assert os.path.exists(_hook_path(root, name)), f"{name} 应被安装"
        assert not os.path.exists(_hook_path(root, "post-commit")), \
            "post-commit 不应被安装（with_post_commit=False）"


def test_install_hooks_idempotent():
    """install_hooks() 重复安装是幂等的（marker 保护）。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = CallWardenInstaller()

        with _chdir_ctx(root):
            installer.install_hooks()
        # 记录第一轮内容
        contents1 = {n: _read_hook(root, n) for n in ("pre-commit", "pre-push", "post-commit")}

        with _chdir_ctx(root):
            installer.install_hooks()
        contents2 = {n: _read_hook(root, n) for n in ("pre-commit", "pre-push", "post-commit")}

        for name in ("pre-commit", "pre-push", "post-commit"):
            assert contents1[name] == contents2[name], f"{name} 重复安装内容应一致"


def test_install_hooks_preserves_non_cw_hooks():
    """install_hooks() 默认不覆盖用户自定义的 hook（无 marker）。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        # 用户已有自定义 pre-commit（无 marker）
        os.makedirs(os.path.join(root, ".git", "hooks"), exist_ok=True)
        user_hook = "#!/bin/sh\necho user-defined\n"
        with open(_hook_path(root, "pre-commit"), "w", encoding="utf-8") as f:
            f.write(user_hook)

        installer = CallWardenInstaller()
        with _chdir_ctx(root):
            installer.install_hooks()

        # 用户 hook 应被保留
        content = _read_hook(root, "pre-commit")
        assert "user-defined" in content, "用户自定义 pre-commit 应被保留"
        # post-commit 应被安装（之前不存在）
        assert os.path.exists(_hook_path(root, "post-commit"))


def test_install_hooks_force_overwrites_non_cw_hooks():
    """install_hooks(force=True) 强制覆盖用户自定义的 hook。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        os.makedirs(os.path.join(root, ".git", "hooks"), exist_ok=True)
        user_hook = "#!/bin/sh\necho user-defined\n"
        with open(_hook_path(root, "pre-commit"), "w", encoding="utf-8") as f:
            f.write(user_hook)

        installer = CallWardenInstaller()
        with _chdir_ctx(root):
            installer.install_hooks(force=True)

        content = _read_hook(root, "pre-commit")
        assert "user-defined" not in content, "force=True 应覆盖用户自定义 hook"
        assert installer._hook_marker() in content, "覆盖后应写入 Call Warden marker"


def test_install_hooks_no_git_dir_prints_error():
    """不在 git 仓库内调用 install_hooks() 不报错，仅打印提示。"""
    with tempfile.TemporaryDirectory() as root:
        installer = CallWardenInstaller()
        with _chdir_ctx(root):
            # 不 init git，应直接返回
            installer.install_hooks()
        # 无 hook 被创建
        assert not os.path.exists(_hook_path(root, "pre-commit"))


def test_install_hooks_executable_bits():
    """install_hooks() 安装的 hook 必须可执行。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        installer = CallWardenInstaller()
        with _chdir_ctx(root):
            installer.install_hooks()

        if os.name != "nt":
            for name in ("pre-commit", "pre-push", "post-commit"):
                mode = os.stat(_hook_path(root, name)).st_mode
                assert mode & stat.S_IXUSR, f"{name} 必须有用户可执行权限"


# ----------------------------------------------------------------------
# CLI cw install --hooks 参数分发
# ----------------------------------------------------------------------

def test_cli_install_hooks_dispatches_with_post_commit_true():
    """`cw install --hooks` 调用 install_hooks(with_post_commit=True)。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        old_cwd = os.getcwd()
        os.chdir(root)
        try:
            called = {"with_post_commit": None, "force": None}

            def fake_install(self, force=False, with_post_commit=True):
                called["with_post_commit"] = with_post_commit
                called["force"] = force
                return None

            with mock.patch.object(CallWardenInstaller, "install_hooks", fake_install):
                old_argv = sys.argv
                # 注意：cw.py 会 strip "install"，直接调用 install.main() 时 argv 不含 "install"
                sys.argv = ["cw", "--hooks"]
                try:
                    from callwarden.install import main as install_main
                    install_main()
                except SystemExit:
                    pass
                finally:
                    sys.argv = old_argv

            assert called["with_post_commit"] is True, "默认应安装 post-commit"
            assert called["force"] is False
        finally:
            os.chdir(old_cwd)


def test_cli_install_hooks_no_post_commit_dispatches_false():
    """`cw install --hooks --no-post-commit` 调用 install_hooks(with_post_commit=False)。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        old_cwd = os.getcwd()
        os.chdir(root)
        try:
            called = {"with_post_commit": None}

            def fake_install(self, force=False, with_post_commit=True):
                called["with_post_commit"] = with_post_commit
                return None

            with mock.patch.object(CallWardenInstaller, "install_hooks", fake_install):
                old_argv = sys.argv
                sys.argv = ["cw", "--hooks", "--no-post-commit"]
                try:
                    from callwarden.install import main as install_main
                    install_main()
                except SystemExit:
                    pass
                finally:
                    sys.argv = old_argv

            assert called["with_post_commit"] is False, "--no-post-commit 应跳过 post-commit"
        finally:
            os.chdir(old_cwd)


def test_cli_install_hooks_force_hooks_dispatches():
    """`cw install --hooks --force-hooks` 调用 install_hooks(force=True)。"""
    with tempfile.TemporaryDirectory() as root:
        _init_git_repo(root)
        old_cwd = os.getcwd()
        os.chdir(root)
        try:
            called = {"force": None, "with_post_commit": None}

            def fake_install(self, force=False, with_post_commit=True):
                called["force"] = force
                called["with_post_commit"] = with_post_commit
                return None

            with mock.patch.object(CallWardenInstaller, "install_hooks", fake_install):
                old_argv = sys.argv
                sys.argv = ["cw", "--hooks", "--force-hooks"]
                try:
                    from callwarden.install import main as install_main
                    install_main()
                except SystemExit:
                    pass
                finally:
                    sys.argv = old_argv

            assert called["force"] is True
            assert called["with_post_commit"] is True
        finally:
            os.chdir(old_cwd)


# ----------------------------------------------------------------------
# i18n key 存在性
# ----------------------------------------------------------------------

def test_i18n_keys_for_install_hooks_unified_exist():
    """zh_CN.json / en_US.json 包含 install_hooks 统一入口的所有 i18n key。"""
    from callwarden.i18n import _get_i18n_dir

    for lang_file in ("zh_CN.json", "en_US.json"):
        with open(os.path.join(_get_i18n_dir(), lang_file), encoding="utf-8") as f:
            data = json.load(f)
        args = data["cli"]["args"]
        messages = data["cli"]["messages"]
        required_args = [
            "install_hooks",
            "install_force_hooks",
            "install_no_post_commit",
        ]
        required_messages = [
            "install_hook_task_id_auto",
            "install_hook_task_id_hardcoded",
            "install_hook_installed",
            "install_hooks_installed",
            "install_hooks_skipped",
            "install_hooks_summary",
        ]
        for key in required_args:
            assert key in args, f"{lang_file} 缺少 i18n key: cli.args.{key}"
        for key in required_messages:
            assert key in messages, f"{lang_file} 缺少 i18n key: cli.messages.{key}"


def test_old_i18n_key_install_hook_task_id_envvar_removed():
    """旧的 install_hook_task_id_envvar i18n key 应已删除（改为 install_hook_task_id_auto）。"""
    from callwarden.i18n import _get_i18n_dir

    for lang_file in ("zh_CN.json", "en_US.json"):
        with open(os.path.join(_get_i18n_dir(), lang_file), encoding="utf-8") as f:
            data = json.load(f)
        messages = data["cli"]["messages"]
        assert "install_hook_task_id_envvar" not in messages, \
            f"{lang_file} 仍包含过时 key install_hook_task_id_envvar（应改名为 install_hook_task_id_auto）"


def test_install_hook_arg_task_id_no_envvar_reference():
    """install_hook_arg_task_id i18n 描述不应再引用 CALLWARDEN_TASK_ID 环境变量。"""
    from callwarden.i18n import _get_i18n_dir

    for lang_file in ("zh_CN.json", "en_US.json"):
        with open(os.path.join(_get_i18n_dir(), lang_file), encoding="utf-8") as f:
            data = json.load(f)
        text = data["cli"]["messages"]["install_hook_arg_task_id"]
        # 不应再出现 "CALLWARDEN_TASK_ID" 字样（应改为 --auto 模式描述）
        assert "CALLWARDEN_TASK_ID" not in text, \
            f"{lang_file} 的 install_hook_arg_task_id 仍引用 CALLWARDEN_TASK_ID"


# ----------------------------------------------------------------------
# pre-commit hook 容错重试逻辑（T-1784403320003）
# ----------------------------------------------------------------------

def test_pre_commit_hook_has_retry_loop():
    """pre-commit hook 包含重试循环变量 _refresh_attempt / _refresh_max=3。"""
    installer = CallWardenInstaller()
    content = installer._pre_commit_hook()
    assert "_refresh_attempt" in content, "hook 必须包含重试计数变量 _refresh_attempt"
    assert "_refresh_max=3" in content, "hook 必须设置最大重试次数 _refresh_max=3"
    assert "while [ \"$_refresh_attempt\" -lt \"$_refresh_max\" ]" in content, \
        "hook 必须有 while 重试循环"


def test_pre_commit_hook_has_sleep_between_retries():
    """pre-commit hook 在重试之间有 sleep 2 间隔。"""
    installer = CallWardenInstaller()
    content = installer._pre_commit_hook()
    assert "sleep 2" in content, "hook 必须在重试之间 sleep 2 秒"


def test_pre_commit_hook_has_trae_sandbox_diagnostics():
    """pre-commit hook 失败时打印 TRAE 沙箱排查建议。"""
    installer = CallWardenInstaller()
    content = installer._pre_commit_hook()
    # 必须提及 TRAE 沙箱（根因之一）
    assert "TRAE" in content, "hook 失败提示必须提及 TRAE 沙箱"
    assert "沙箱" in content, "hook 失败提示必须提及沙箱拦截"
    # 必须给出绕过建议（PowerShell + --no-verify）
    assert "PowerShell" in content, "hook 必须给出 PowerShell 替代方案"
    assert "--no-verify" in content, "hook 必须提及 --no-verify 绕过方式"
    # 必须提示停 MCP Server
    assert "cw server --stop" in content, "hook 必须提示停止 MCP Server"


def test_pre_commit_hook_preserves_check_task_soft_gate():
    """pre-commit hook 中 check-task 保持软门禁（|| true）。"""
    installer = CallWardenInstaller()
    content = installer._pre_commit_hook()
    assert "git check-task || true" in content, \
        "check-task 必须保留 || true 软门禁（不阻止 commit）"


def test_pre_commit_hook_exits_nonzero_on_final_failure():
    """pre-commit hook 重试耗尽（且看门狗降级也失败）后必须 exit 1（保持 AGENTS.md 规则 1 硬门禁）。"""
    installer = CallWardenInstaller()
    content = installer._pre_commit_hook()
    # 最终失败分支必须 exit 1
    assert "exit 1" in content, "hook 重试耗尽后必须 exit 1 阻止 commit"
    # 必须有最终失败判断（refresh 成功标志 / 看门狗降级均失败时进入）
    assert "if [ \"$_refresh_ok\" -ne 1 ]" in content, \
        "hook 必须以 _refresh_ok 作为最终失败判断（含看门狗降级结果）"
    # 重试计数变量为旧契约，仍须保留
    assert "_refresh_attempt" in content and "_refresh_max=3" in content


def test_pre_commit_hook_has_watchdog_degradation():
    """pre-commit hook 内置卡死看门狗与降级刷新（T-1785824926483）。"""
    installer = CallWardenInstaller()
    content = installer._pre_commit_hook()
    # 看门狗：mtime 采样函数 + 停滞计数 + 强制终止
    assert "_stat_mtime" in content, "hook 必须提供 DB/WAL mtime 采样函数"
    assert "_stall" in content, "hook 必须有停滞计数变量"
    assert "kill -9" in content, "hook 看门狗必须能终止卡死的 refresh-all 进程"
    # 降级：显式刷新本次提交的变更文件（规则 32 自动化）
    assert "git diff --cached --name-only" in content, \
        "hook 降级必须基于本次提交的变更文件清单"
    assert "refresh $_changed_files" in content, \
        "hook 降级必须调用 cw refresh <staged files>"


def test_pre_commit_hook_refresh_all_present():
    """pre-commit hook 必须实际执行 cw --refresh-all。"""
    installer = CallWardenInstaller()
    content = installer._pre_commit_hook()
    assert "--refresh-all" in content, "hook 必须执行 cw --refresh-all"
