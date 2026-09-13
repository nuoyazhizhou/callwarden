"""task capture-diff --auto 模式测试（C4）

验证：
1. db.task_capture_diff_auto 在没有 in_progress 任务时返回 fail-soft 结果
2. db.task_capture_diff_auto 检测到 in_progress 任务后自动调用 task_capture_diff
3. db.task_capture_diff_auto 取 HEAD~1 作为 base（有上一个 commit）
4. db.task_capture_diff_auto 在首次提交场景下 base 留空
5. db.task_capture_diff_auto 任何异常都不抛出（fail-soft，不影响 git commit）
6. CLI `cw task capture-diff --auto` 不需要 task_id 参数
7. CLI --auto 成功时输出 task_id / base / 变更摘要
8. CLI --auto 失败时返回 True（不阻断 git commit）
9. CLI 手动模式未传 task_id 时报错（带提示用 --auto）
10. i18n key 全部存在（zh_CN / en_US）
"""
import os
import subprocess
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------

def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _init_git_repo(root):
    """初始化 git 仓库并提交一个文件，返回 HEAD commit hash。"""
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@test.com"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@test.com"
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True, env=env)
    with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("callwarden.db*\n*.pyc\n__pycache__/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=root, capture_output=True, check=True, env=env)
    f = os.path.join(root, "committed.py")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("print('initial')\n")
    subprocess.run(["git", "add", "committed.py"], cwd=root, capture_output=True, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True, env=env)
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, env=env
    )
    return r.stdout.strip()


def _make_second_commit(root):
    """在已有 git 仓库中追加第二个 commit，返回新的 HEAD~1（即原 HEAD）。"""
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@test.com"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@test.com"
    with open(os.path.join(root, "second.py"), "w", encoding="utf-8") as fh:
        fh.write("print('second')\n")
    subprocess.run(["git", "add", "second.py"], cwd=root, capture_output=True, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "second"], cwd=root, capture_output=True, check=True, env=env)
    # HEAD~1 现在指向第一次提交
    r = subprocess.run(
        ["git", "rev-parse", "HEAD~1"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, env=env,
    )
    return r.stdout.strip()


# ----------------------------------------------------------------------
# db.task_capture_diff_auto
# ----------------------------------------------------------------------

def test_auto_no_in_progress_task_returns_fail_soft():
    """没有 in_progress 任务时返回 success=False, reason=no_in_progress_task。"""
    db, _root = _db_with_workspace()
    try:
        result = db.task_capture_diff_auto()
        assert result["auto"] is True
        assert result["success"] is False
        assert result["reason"] == "no_in_progress_task"
        assert result["task_id"] == ""
        assert result["changed_files"] == []
        assert result["next_action"] == "noop"
    finally:
        db.close()


def test_auto_detects_in_progress_task():
    """检测到 in_progress 任务时调用 task_capture_diff 并返回结果。

    模拟 post-commit 场景：先有 1 个 commit，然后修改文件并 commit，
    HEAD~1 即为原 commit。
    """
    db, root = _db_with_workspace()
    try:
        head = _init_git_repo(root)
        # 创建任务并领取一步，进入 in_progress
        task_id = db.task_create("auto-test", "desc", [
            {"action": "edit", "target_file": "demo.py"},
        ])
        db.task_next_step(task_id)

        # 制造一个 dirty 文件并提交，模拟 post-commit 场景
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# extra\n")
        env = os.environ.copy()
        env["GIT_AUTHOR_NAME"] = "test"
        env["GIT_AUTHOR_EMAIL"] = "test@test.com"
        env["GIT_COMMITTER_NAME"] = "test"
        env["GIT_COMMITTER_EMAIL"] = "test@test.com"
        subprocess.run(
            ["git", "add", "committed.py"], cwd=root, capture_output=True, check=True, env=env
        )
        subprocess.run(
            ["git", "commit", "-m", "second"], cwd=root, capture_output=True, check=True, env=env
        )

        # 修改文件再 dirty 一下，让 capture-diff 能检测到变更
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# extra2\n")

        result = db.task_capture_diff_auto()

        assert result["auto"] is True
        assert result["success"] is True
        assert result["task_id"] == task_id
        # HEAD~1 应为原首次提交
        assert result["base"] == head
        assert result["dry_run"] is False
        assert isinstance(result["changed_files"], list)
        assert len(result["changed_files"]) >= 1
    finally:
        db.close()


def test_auto_uses_head_tilde_1_as_base():
    """有多个 commit 时，base 取 HEAD~1（即上一次提交）。"""
    db, root = _db_with_workspace()
    try:
        head = _init_git_repo(root)
        prev_head = _make_second_commit(root)  # 返回 HEAD~1，应等于原 head
        assert prev_head == head

        task_id = db.task_create("auto-base-test", "desc", [
            {"action": "edit", "target_file": "demo.py"},
        ])
        db.task_next_step(task_id)

        # 修改文件使 dirty
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# extra2\n")

        result = db.task_capture_diff_auto()
        assert result["success"] is True
        # base 应为 HEAD~1（即原首次提交）
        assert result["base"] == head
    finally:
        db.close()


def test_auto_exception_is_fail_soft():
    """task_list 抛异常时，task_capture_diff_auto 不抛出，封装为 fail-soft。"""
    db, _root = _db_with_workspace()
    try:
        # monkey-patch task_list 抛异常
        def boom(*args, **kwargs):
            raise RuntimeError("simulated failure")

        db.task_list = boom
        result = db.task_capture_diff_auto()
        assert result["auto"] is True
        assert result["success"] is False
        assert result["reason"] == "exception"
        assert "simulated failure" in result["error"]
        assert result["next_action"] == "noop"
    finally:
        db.close()


def test_auto_task_list_returns_empty_dict_entries():
    """task_list 返回的条目 task_id 为空串时也走 fail-soft。"""
    db, root = _db_with_workspace()
    try:
        _init_git_repo(root)
        # monkey-patch task_list 返回 [{'task_id': ''}]（异常数据）
        db.task_list = lambda *a, **kw: [{"task_id": ""}]

        result = db.task_capture_diff_auto()
        assert result["auto"] is True
        assert result["success"] is False
        assert result["reason"] == "no_in_progress_task"
    finally:
        db.close()


def test_auto_first_commit_base_empty():
    """首次提交场景（git rev-parse HEAD~1 失败）base 留空，不阻断。"""
    db, root = _db_with_workspace()
    try:
        # 没有 git 仓库，git rev-parse 会失败
        task_id = db.task_create("no-git-test", "desc", [
            {"action": "edit", "target_file": "demo.py"},
        ])
        db.task_next_step(task_id)

        result = db.task_capture_diff_auto()
        # 在非 git 项目中，task_capture_diff 内部会回退到 mtime 检测
        # 但无论结果如何，都不应抛出
        assert result["auto"] is True
        # base 应为空串（git rev-parse 失败）
        assert result["base"] == ""
    finally:
        db.close()


def test_auto_returns_auto_field_in_result():
    """成功路径下 result dict 也包含 auto=True 标识。"""
    db, root = _db_with_workspace()
    try:
        _init_git_repo(root)
        task_id = db.task_create("auto-flag-test", "desc", [
            {"action": "edit", "target_file": "demo.py"},
        ])
        db.task_next_step(task_id)

        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# extra3\n")

        result = db.task_capture_diff_auto()
        assert result["auto"] is True
        assert result["success"] is True
    finally:
        db.close()


# ----------------------------------------------------------------------
# CLI cw task capture-diff --auto
# ----------------------------------------------------------------------

def test_cli_capture_diff_auto_no_task_id_required(route_stub):
    """--auto 模式下 task_id 可省略。

    stale 依据（A2 / daemon authority 化）：CLI capture-diff --auto 已改走
    ``route_task_write("task.capture_diff", {"auto": True}, ...)``（cli/main.py:5574-5579），
    不再直连本地 ``db.task_capture_diff_auto``；旧断言 spy 本地 db 方法，在无 daemon
    环境下因路由失败（E_HTTP_MANIFEST_MISSING）而误判未被调用。改用
    tests/conftest.py:100-108 的 route_stub 按 RPC 契约断言。
    """
    import tempfile
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            expected = {
                "auto": True,
                "success": True,
                "task_id": "T-mock",
                "step_id": "",
                "base": "abc123",
                "dry_run": False,
                "scan_id": 1,
                "changed_files": [{"path": "demo.py", "status": "M"}],
                "linked_symbols": [],
                "quality_findings": [],
                "quality_decision": "",
                "next_action": "review",
                "error": "",
                "reason": "",
            }
            route_stub.reply("task.capture_diff", expected)

            ret = None
            try:
                ret = cli_main._handle_task(["capture-diff", "--auto"], db)
            except SystemExit:
                pass

            assert route_stub.count("task.capture_diff") == 1, (
                "capture-diff --auto 必须走 task.capture_diff RPC 一次"
            )
            assert route_stub.last_params("task.capture_diff").get("auto") is True, (
                f"--auto 应以 auto=True 路由，实际: {route_stub.last_params('task.capture_diff')}"
            )
            assert ret is True
        finally:
            db.close()


def test_cli_capture_diff_auto_failure_returns_true():
    """--auto 失败时返回 True（不阻断 git commit）。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            expected = {
                "auto": True,
                "success": False,
                "reason": "no_in_progress_task",
                "error": "",
                "task_id": "",
                "base": "",
                "dry_run": False,
                "changed_files": [],
                "linked_symbols": [],
                "quality_findings": [],
                "quality_decision": "",
                "next_action": "noop",
            }

            with mock.patch.object(db, "task_capture_diff_auto", return_value=expected):
                old_argv = sys.argv
                sys.argv = ["cw", "task", "capture-diff", "--auto"]
                try:
                    ret = cli_main._handle_task(["capture-diff", "--auto"], db)
                except SystemExit:
                    pass
                finally:
                    sys.argv = old_argv

            # fail-soft：即使失败也返回 True，不阻断 git commit
            assert ret is True
        finally:
            db.close()


def test_cli_capture_diff_auto_exception_returns_true():
    """--auto 模式下 db 抛异常时 CLI 兜底捕获，返回 True（fail-soft）。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            def boom():
                raise RuntimeError("db crashed")

            with mock.patch.object(db, "task_capture_diff_auto", side_effect=boom):
                old_argv = sys.argv
                sys.argv = ["cw", "task", "capture-diff", "--auto"]
                ret = None
                try:
                    ret = cli_main._handle_task(["capture-diff", "--auto"], db)
                except SystemExit:
                    pass
                except Exception as e:
                    pytest.fail(f"CLI 应兜底捕获异常，但抛出: {e}")
                finally:
                    sys.argv = old_argv

            # CLI 兜底 fail-soft：返回 True，不抛异常
            assert ret is True
        finally:
            db.close()


def test_cli_capture_diff_manual_missing_task_id_errors():
    """手动模式（未传 --auto）且未指定 task_id 时报错。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            old_argv = sys.argv
            sys.argv = ["cw", "task", "capture-diff"]
            exited = {"code": None}
            try:
                cli_main._handle_task(["capture-diff"], db)
            except SystemExit as e:
                exited["code"] = e.code
            finally:
                sys.argv = old_argv

            # argparse parser.error 调用 sys.exit(2)
            assert exited["code"] == 2
        finally:
            db.close()


def test_cli_capture_diff_auto_with_task_id_uses_auto(route_stub):
    """--auto 和 task_id 同时给出时，走 auto 路径（task_id 被忽略）。

    stale 依据（A2 / daemon authority 化）：CLI 已改走
    ``route_task_write("task.capture_diff", ...)``（cli/main.py:5574-5579）；
    auto 路径的 RPC params 仅 {"auto": True}（不含 task_id），手动路径会携带
    task_id 等字段（cli/main.py:5687-5694）。故断言 params == {"auto": True}
    即证明走 auto 路径且未走手动路径。
    """
    import tempfile
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            route_stub.reply("task.capture_diff", {
                "auto": True, "success": True, "task_id": "T-auto",
                "step_id": "", "base": "abc", "dry_run": False,
                "scan_id": 1, "changed_files": [], "linked_symbols": [],
                "quality_findings": [], "quality_decision": "",
                "next_action": "noop", "error": "", "reason": "",
            })

            try:
                cli_main._handle_task(["capture-diff", "T-xxx", "--auto"], db)
            except SystemExit:
                pass

            assert route_stub.count("task.capture_diff") == 1, "--auto 应路由 task.capture_diff 一次"
            params = route_stub.last_params("task.capture_diff")
            assert params.get("auto") is True and "task_id" not in params, (
                f"--auto 路径不应携带 task_id，实际: {params}"
            )
        finally:
            db.close()


# ----------------------------------------------------------------------
# i18n key 存在性
# ----------------------------------------------------------------------

def test_i18n_keys_exist_zh():
    """zh_CN.json 包含 C4 新增的所有 i18n key。"""
    import json
    from callwarden.i18n import _get_i18n_dir

    with open(
        os.path.join(_get_i18n_dir(), "zh_CN.json"),
        encoding="utf-8",
    ) as f:
        data = json.load(f)

    messages = data.get("cli", {}).get("messages", {})

    required_messages = [
        "task_capture_diff_auto_mode",
        "task_capture_diff_auto_no_task",
        "task_capture_diff_auto_exception",
        "task_capture_diff_auto_failed",
        "task_capture_diff_missing_task_id",
    ]
    for key in required_messages:
        assert key in messages, f"缺少 i18n key: cli.messages.{key}"

    # cli_task_arg_auto_capture 直接挂在顶层（与 cli_task_arg_task_id 同级）
    assert "cli_task_arg_auto_capture" in data, "缺少 i18n key: cli_task_arg_auto_capture"


def test_i18n_keys_exist_en():
    """en_US.json 包含 C4 新增的所有 i18n key。"""
    import json
    from callwarden.i18n import _get_i18n_dir

    with open(
        os.path.join(_get_i18n_dir(), "en_US.json"),
        encoding="utf-8",
    ) as f:
        data = json.load(f)

    messages = data.get("cli", {}).get("messages", {})

    required_messages = [
        "task_capture_diff_auto_mode",
        "task_capture_diff_auto_no_task",
        "task_capture_diff_auto_exception",
        "task_capture_diff_auto_failed",
        "task_capture_diff_missing_task_id",
    ]
    for key in required_messages:
        assert key in messages, f"缺少 i18n key: cli.messages.{key}"

    assert "cli_task_arg_auto_capture" in data, "缺少 i18n key: cli_task_arg_auto_capture"


def test_i18n_keys_have_placeholders():
    """带占位符的 i18n key 文案中包含正确的占位符。"""
    import json
    from callwarden.i18n import _get_i18n_dir

    with open(
        os.path.join(_get_i18n_dir(), "zh_CN.json"),
        encoding="utf-8",
    ) as f:
        data = json.load(f)

    messages = data["cli"]["messages"]
    assert "{error}" in messages["task_capture_diff_auto_exception"]
    assert "{reason}" in messages["task_capture_diff_auto_failed"]
    assert "{error}" in messages["task_capture_diff_auto_failed"]
