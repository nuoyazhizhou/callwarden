"""端到端三角关联测试：task ↔ commit ↔ symbol

验证完整的 post-commit hook 闭环：
1. 创建临时 git 仓库 + Python 文件
2. build_full_graph 构建符号图谱
3. task_create 创建任务（steps 指向目标符号）
4. task_next_step 认领任务（进入 in_progress，设置 active_task_id）
5. 修改文件 + git commit（模拟开发者编辑）
6. refresh_file 刷新数据库（保持符号同步）
7. task_capture_diff_auto 模拟 post-commit hook 自动捕获
8. 验证三角关联：
   - task_symbol_changes 有记录
   - get_task_commits 正向查询（task → commit）
   - get_commit_tasks 反向查询（commit → task）

本测试不依赖外部 cw_demo 项目，完全自包含在 pytest tmp_path 中。
"""
import os
import subprocess
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB


# ----------------------------------------------------------------------
# 辅助函数
# ----------------------------------------------------------------------

def _git_env():
    """构造隔离的 git 环境变量（避免污染全局 git config）。"""
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@test.com"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@test.com"
    # 禁用 GPG 签名（避免无 gpg 时 commit 失败）
    env["GIT_CONFIG_GLOBAL"] = "/dev/null"
    return env


def _init_git_repo(root):
    """初始化 git 仓库。"""
    subprocess.run(["git", "init", "-q"], cwd=root, capture_output=True, check=True, env=_git_env())


def _git_commit(root, message, add_files=None):
    """在 root 目录执行 git add + commit，返回 commit hash。"""
    env = _git_env()
    if add_files is None:
        subprocess.run(["git", "add", "-A"], cwd=root, capture_output=True, check=True, env=env)
    else:
        for f in add_files:
            subprocess.run(["git", "add", f], cwd=root, capture_output=True, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-m", message, "--no-gpg-sign"],
        cwd=root, capture_output=True, check=True, env=env,
    )
    r = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root, capture_output=True, text=True, encoding="utf-8", errors="replace", check=True, env=env,
    )
    return r.stdout.strip()


def _write_file(root, rel_path, content):
    """写入文件（自动创建父目录）。"""
    abs_path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)


# ----------------------------------------------------------------------
# 端到端测试
# ----------------------------------------------------------------------

class TestE2ETriangleLinkage:
    """端到端三角关联测试套件。"""

    def test_full_triangle_linkage_e2e(self):
        """完整端到端：创建任务 → 编辑文件 → 刷新 → commit → 捕获 → 验证三角关联。"""
        with tempfile.TemporaryDirectory() as root:
            # 1. 初始化 git 仓库
            _init_git_repo(root)

            # 2. 创建初始 Python 文件（含 hub 函数 process_request）
            _write_file(root, "app/core.py", '''"""核心模块。"""


def process_request(request):
    """处理请求的 hub 函数。"""
    validated = validate_request(request)
    result = handle_request(validated)
    log_request(result)
    return result


def validate_request(request):
    """验证请求。"""
    if not request:
        raise ValueError("empty")
    return request


def handle_request(validated):
    """处理请求。"""
    return {"status": "ok", "data": validated}


def log_request(result):
    """记录日志。"""
    print(f"Request: {result}")
''')
            # 写 .gitignore 排除 db 文件
            _write_file(root, ".gitignore", "callwarden.db*\n*.pyc\n__pycache__/\n")

            initial_commit = _git_commit(root, "Initial commit: app/core.py")

            # 3. 构建符号图谱
            db = CodeGraphDB(workspace_root=root)
            try:
                db.build_full_graph()

                # 验证符号已入库（QN 格式为 core.process_request，模块名=文件名 stem）
                cur = db.conn.execute(
                    "SELECT qualified_name FROM symbols WHERE qualified_name LIKE '%.process_request' LIMIT 1"
                )
                row = cur.fetchone()
                assert row is not None, "process_request 符号应已入库"
                process_request_qn = row["qualified_name"]
                # retry_request 的 QN 同理（同模块）
                retry_request_qn = process_request_qn.replace(
                    "process_request", "retry_request"
                )

                # 4. 导入 git 历史
                db.import_git_history(max_commits=10)

                # 5. 创建任务（steps 指向 process_request）
                task_id = db.task_create(
                    title="test: add retry to process_request",
                    description="Add retry logic, test triangle linkage",
                    steps=[
                        {
                            "action": "annotate",
                            "target_file": "app/core.py",
                            "target_symbol": retry_request_qn,
                        },
                        {
                            "action": "refactor",
                            "target_file": "app/core.py",
                            "target_symbol": process_request_qn,
                        },
                    ],
                )
                assert task_id.startswith("T-"), f"task_id 应以 T- 开头，实际: {task_id}"

                # 6. 认领任务（进入 in_progress）
                step = db.task_next_step(task_id)
                assert step is not None, "应返回第一个 step"
                assert step["status"] == "in_progress"

                # 验证 active_task_id 已设置
                active = db.get_active_task()
                assert active == task_id, f"active_task_id 应为 {task_id}，实际: {active}"

                # 7. 修改文件：新增 retry_request 函数
                _write_file(root, "app/core.py", '''"""核心模块。"""


def process_request(request):
    """处理请求的 hub 函数。"""
    validated = validate_request(request)
    result = handle_request(validated)
    log_request(result)
    return result


def retry_request(request, max_retries=3):
    """重试请求 - 新增函数用于测试三角关联。"""
    for attempt in range(max_retries):
        try:
            return process_request(request)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            print(f"Retry {attempt + 1}/{max_retries}: {e}")
    return None


def validate_request(request):
    """验证请求。"""
    if not request:
        raise ValueError("empty")
    return request


def handle_request(validated):
    """处理请求。"""
    return {"status": "ok", "data": validated}


def log_request(result):
    """记录日志。"""
    print(f"Request: {result}")
''')

                # 8. 刷新数据库（模拟 cw --refresh）
                db.refresh_file("app/core.py")

                # 9. git commit（模拟开发者提交）
                second_commit = _git_commit(root, "Add retry_request function")

                # 10. 重新导入 git 历史（让 git_commits 表有新 commit）
                db.import_git_history(max_commits=10)

                # 11. 模拟 post-commit hook：调用 task_capture_diff_auto
                #     （需要文件有 dirty 变化，再修改一次让 capture 检测到）
                _write_file(root, "app/core.py", '''"""核心模块。"""


def process_request(request):
    """处理请求的 hub 函数。"""
    validated = validate_request(request)
    result = handle_request(validated)
    log_request(result)
    return result


def retry_request(request, max_retries=3):
    """重试请求 - 新增函数用于测试三角关联。"""
    for attempt in range(max_retries):
        try:
            return process_request(request)
        except Exception as e:
            if attempt == max_retries - 1:
                raise
            print(f"Retry {attempt + 1}/{max_retries}: {e}")
    return None


def validate_request(request):
    """验证请求。"""
    if not request:
        raise ValueError("empty")
    return request


def handle_request(validated):
    """处理请求。"""
    return {"status": "ok", "data": validated}


def log_request(result):
    """记录日志。"""
    print(f"Request: {result}")
# extra line for dirty detection
''')
                result = db.task_capture_diff_auto()

                # 12. 验证捕获结果
                assert result["auto"] is True, "应为 auto 模式"
                assert result["success"] is True, (
                    f"捕获应成功，实际: reason={result.get('reason')}, error={result.get('error')}"
                )
                assert result["task_id"] == task_id, (
                    f"task_id 应匹配，实际: {result['task_id']}"
                )

                # 13. 验证三角关联：task_symbol_changes
                symbol_changes = db.get_task_symbol_changes(task_id)
                assert len(symbol_changes) >= 1, (
                    f"task_symbol_changes 应有记录，实际: {len(symbol_changes)}"
                )

                # 14. 验证正向查询：task → commit
                task_commits = db.get_task_commits(task_id)
                assert len(task_commits) >= 1, (
                    f"get_task_commits 应返回至少 1 条，实际: {len(task_commits)}"
                )
                # 应包含 second_commit
                commit_hashes = [tc["source_commit_hash"] for tc in task_commits]
                assert any(
                    h.startswith(second_commit[:8]) for h in commit_hashes
                ), f"task_commits 应包含 second_commit {second_commit[:8]}，实际: {commit_hashes}"
                # author 应非空（git-import 后）
                tc_with_author = [tc for tc in task_commits if tc.get("commit_author")]
                assert len(tc_with_author) >= 1, (
                    f"至少 1 条 task_commit 应有 author（git-import 后），实际: {task_commits}"
                )

                # 15. 验证反向查询：commit → task
                commit_tasks = db.get_commit_tasks(second_commit)
                assert len(commit_tasks) >= 1, (
                    f"get_commit_tasks 应返回至少 1 条，实际: {len(commit_tasks)}"
                )
                task_ids = [ct["task_id"] for ct in commit_tasks]
                assert task_id in task_ids, (
                    f"commit_tasks 应包含 {task_id}，实际: {task_ids}"
                )
                # task_title 应非空（JOIN tasks 表后）
                ct_with_title = [ct for ct in commit_tasks if ct.get("task_title")]
                assert len(ct_with_title) >= 1, (
                    f"至少 1 条 commit_task 应有 task_title，实际: {commit_tasks}"
                )

                # 16. 验证任务状态仍为 in_progress（未被自动关闭）
                cur = db.conn.execute(
                    "SELECT status FROM tasks WHERE id = ?", (task_id,)
                )
                assert cur.fetchone()["status"] == "in_progress"
            finally:
                db.close()


# ----------------------------------------------------------------------
# Fail-soft 测试：hook 异常不阻断 commit
# ----------------------------------------------------------------------

class TestPostCommitHookFailSoft:
    """验证 post-commit hook 的 fail-soft 语义。"""

    def test_capture_diff_auto_no_in_progress_task(self):
        """没有 in_progress 任务时返回 success=False，不抛异常。"""
        with tempfile.TemporaryDirectory() as root:
            _init_git_repo(root)
            _write_file(root, "dummy.py", "x = 1\n")
            _git_commit(root, "init")

            db = CodeGraphDB(workspace_root=root)
            try:
                result = db.task_capture_diff_auto()
                assert result["auto"] is True
                assert result["success"] is False
                assert result["reason"] == "no_in_progress_task"
            finally:
                db.close()

    def test_capture_diff_auto_exception_does_not_raise(self):
        """task_list 抛异常时，capture_diff_auto 封装为 fail-soft，不抛出。"""
        with tempfile.TemporaryDirectory() as root:
            db = CodeGraphDB(workspace_root=root)
            try:
                def boom(*args, **kwargs):
                    raise RuntimeError("simulated db failure")

                db.task_list = boom
                result = db.task_capture_diff_auto()
                assert result["auto"] is True
                assert result["success"] is False
                assert result["reason"] == "exception"
                assert "simulated db failure" in result["error"]
            finally:
                db.close()


# ----------------------------------------------------------------------
# 三角关联查询边界测试
# ----------------------------------------------------------------------

class TestTriangleQueryEdgeCases:
    """三角关联查询的边界情况。"""

    def test_get_task_commits_empty_task_id(self):
        """空 task_id 返回空列表。"""
        with tempfile.TemporaryDirectory() as root:
            db = CodeGraphDB(workspace_root=root)
            try:
                result = db.get_task_commits("")
                assert result == []
            finally:
                db.close()

    def test_get_commit_tasks_empty_commit_hash(self):
        """空 commit_hash 返回空列表。"""
        with tempfile.TemporaryDirectory() as root:
            db = CodeGraphDB(workspace_root=root)
            try:
                result = db.get_commit_tasks("")
                assert result == []
            finally:
                db.close()

    def test_get_task_commits_nonexistent_task(self):
        """不存在的 task_id 返回空列表（不报错）。"""
        with tempfile.TemporaryDirectory() as root:
            db = CodeGraphDB(workspace_root=root)
            try:
                result = db.get_task_commits("T-nonexistent-1234")
                assert result == []
            finally:
                db.close()

    def test_get_commit_tasks_nonexistent_commit(self):
        """不存在的 commit_hash 返回空列表（不报错）。"""
        with tempfile.TemporaryDirectory() as root:
            db = CodeGraphDB(workspace_root=root)
            try:
                result = db.get_commit_tasks("0000000000000000000000000000000000000000")
                assert result == []
            finally:
                db.close()
