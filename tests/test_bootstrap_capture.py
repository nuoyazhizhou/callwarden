"""bootstrap capture 闭环测试（v25 schema）

验证 workspace_scan_runs 表的 schema 与迁移：
1. SCHEMA_VERSION == 25
2. 新库直接包含 workspace_scan_runs 表（无需迁移）
3. 三个索引存在：workspace / task / git_head
4. v24 -> v25 迁移幂等（CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS）
5. 旧 v24 库通过 _init_schema 自动迁移到 v25
6. 字段完整性（17 个字段）
7. 默认值正确（purpose='bootstrap', baseline_type='git', status='running', changed_files_json='[]' 等）
"""
import os
import sqlite3
import tempfile

from callwarden.db.schema import SCHEMA_VERSION
from callwarden.db.db import CodeGraphDB


# ----------------------------------------------------------------------
# 基础断言
# ----------------------------------------------------------------------

def test_schema_version_is_25():
    """SCHEMA_VERSION 常量已升级到 25。"""
    assert SCHEMA_VERSION == 25


def test_new_db_has_workspace_scan_runs_table():
    """新库直接包含 workspace_scan_runs 表（无需迁移）。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            cur = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_scan_runs'"
            )
            assert cur.fetchone() is not None, "workspace_scan_runs 表不存在"
        finally:
            db.close()


def test_workspace_scan_runs_indexes_exist():
    """workspace_scan_runs 表有三个索引：workspace、task、git_head。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            cur = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_workspace_scan_runs_%'"
            )
            names = {row[0] for row in cur.fetchall()}
            assert "idx_workspace_scan_runs_workspace" in names
            assert "idx_workspace_scan_runs_task" in names
            assert "idx_workspace_scan_runs_git_head" in names
        finally:
            db.close()


# ----------------------------------------------------------------------
# 字段与默认值
# ----------------------------------------------------------------------

def test_workspace_scan_runs_columns():
    """workspace_scan_runs 表字段完整（17 个字段）。"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            cur = db.conn.execute("PRAGMA table_info(workspace_scan_runs)")
            cols = {row[1] for row in cur.fetchall()}
            expected = {
                "id", "workspace_id", "purpose", "task_id", "step_id",
                "baseline_type", "git_head", "git_merge_base", "git_status_hash",
                "root_mtime", "file_count", "manifest_hash", "changed_files_json",
                "metadata_json", "started_at", "completed_at", "status",
            }
            missing = expected - cols
            assert not missing, f"缺少字段: {missing}"
        finally:
            db.close()


def test_workspace_scan_runs_defaults():
    """workspace_scan_runs 表默认值正确。

    验证：purpose='bootstrap', baseline_type='git', git_head='', git_merge_base='',
    git_status_hash='', root_mtime=0, file_count=0, manifest_hash='',
    changed_files_json='[]', metadata_json='{}', completed_at=NULL, status='running'
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            # 取 workspace_id
            cur = db.conn.execute(
                "SELECT id FROM workspaces WHERE is_active = 1 LIMIT 1"
            )
            row = cur.fetchone()
            workspace_id = row[0] if row else 1

            # 仅插入必填字段，其余走默认值
            db.conn.execute(
                "INSERT INTO workspace_scan_runs (workspace_id, started_at) VALUES (?, ?)",
                (workspace_id, 1.0),
            )
            db.conn.commit()

            cur = db.conn.execute(
                "SELECT purpose, task_id, step_id, baseline_type, git_head, "
                "       git_merge_base, git_status_hash, root_mtime, file_count, "
                "       manifest_hash, changed_files_json, metadata_json, "
                "       completed_at, status "
                "FROM workspace_scan_runs WHERE workspace_id = ?",
                (workspace_id,),
            )
            r = cur.fetchone()
            assert r[0] == "bootstrap", f"purpose 默认值应为 'bootstrap', 实际: {r[0]}"
            assert r[1] == "", f"task_id 默认值应为 '', 实际: {r[1]}"
            assert r[2] == "", f"step_id 默认值应为 '', 实际: {r[2]}"
            assert r[3] == "git", f"baseline_type 默认值应为 'git', 实际: {r[3]}"
            assert r[4] == "", f"git_head 默认值应为 '', 实际: {r[4]}"
            assert r[5] == "", f"git_merge_base 默认值应为 '', 实际: {r[5]}"
            assert r[6] == "", f"git_status_hash 默认值应为 '', 实际: {r[6]}"
            assert r[7] == 0, f"root_mtime 默认值应为 0, 实际: {r[7]}"
            assert r[8] == 0, f"file_count 默认值应为 0, 实际: {r[8]}"
            assert r[9] == "", f"manifest_hash 默认值应为 '', 实际: {r[9]}"
            assert r[10] == "[]", f"changed_files_json 默认值应为 '[]', 实际: {r[10]}"
            assert r[11] == "{}", f"metadata_json 默认值应为 '{{}}', 实际: {r[11]}"
            assert r[12] is None, f"completed_at 默认值应为 NULL, 实际: {r[12]}"
            assert r[13] == "running", f"status 默认值应为 'running', 实际: {r[13]}"
        finally:
            db.close()


# ----------------------------------------------------------------------
# 迁移幂等性
# ----------------------------------------------------------------------

def test_v25_migration_idempotent():
    """v24 -> v25 迁移幂等：重复执行不报错，表不重复创建。"""
    from callwarden.db.db_base import _migrate_v24_to_v25

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        conn = sqlite3.connect(db_path)
        # 构造一个最小 v24 库（含 workspaces 和 schema_version 表）
        conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY)")
        conn.execute(
            "CREATE TABLE schema_version (version INTEGER, applied_at REAL, description TEXT)"
        )
        conn.execute("INSERT INTO schema_version VALUES (24, 0, 'v24')")
        conn.commit()

        # 第一次迁移
        _migrate_v24_to_v25(conn)
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='workspace_scan_runs'"
        )
        assert cur.fetchone()[0] == 1

        # 第二次迁移（幂等，IF NOT EXISTS 保证不报错）
        _migrate_v24_to_v25(conn)
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name='workspace_scan_runs'"
        )
        assert cur.fetchone()[0] == 1

        # 索引也不重复
        cur = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND name LIKE 'idx_workspace_scan_runs_%'"
        )
        assert cur.fetchone()[0] == 3

        conn.close()


def test_legacy_v24_db_migrates_to_v25():
    """旧 v24 库通过 _init_schema 自动迁移到 v25。

    先建一个完整 v25 库（CodeGraphDB 打开即 v25），降级版本号到 24 并删除
    workspace_scan_runs 表模拟旧库，再重新打开触发 v24→v25 迁移。
    """
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        # 先建一个完整 v25 库
        db1 = CodeGraphDB(db_path=db_path, workspace_root=tmp)
        db1.close()

        # 降级到 v24：删除 workspace_scan_runs 表 + 索引，回退版本号
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM schema_version WHERE version = 25")
        conn.execute("DROP TABLE IF EXISTS workspace_scan_runs")
        conn.execute("DROP INDEX IF EXISTS idx_workspace_scan_runs_workspace")
        conn.execute("DROP INDEX IF EXISTS idx_workspace_scan_runs_task")
        conn.execute("DROP INDEX IF EXISTS idx_workspace_scan_runs_git_head")
        conn.commit()
        conn.close()

        # 重新打开触发 v24 -> v25 迁移
        db = CodeGraphDB(db_path=db_path, workspace_root=tmp)
        try:
            v = db.conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            assert v["version"] == 25

            cur = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='workspace_scan_runs'"
            )
            assert cur.fetchone() is not None

            # 索引也被重建
            cur = db.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_workspace_scan_runs_%'"
            )
            names = {row[0] for row in cur.fetchall()}
            assert "idx_workspace_scan_runs_workspace" in names
            assert "idx_workspace_scan_runs_task" in names
            assert "idx_workspace_scan_runs_git_head" in names
        finally:
            db.close()


# ----------------------------------------------------------------------
# BootstrapMixin 业务方法测试
# ----------------------------------------------------------------------

def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def test_record_workspace_scan_run_returns_id():
    """record_workspace_scan_run 返回有效的 scan_id（>0）。"""
    db, _root = _db_with_workspace()
    try:
        scan_id = db.record_workspace_scan_run(purpose="bootstrap")
        assert isinstance(scan_id, int)
        assert scan_id > 0
    finally:
        db.close()


def test_record_workspace_scan_run_stores_baseline():
    """record_workspace_scan_run 写入的基线字段可被读回。"""
    db, _root = _db_with_workspace()
    try:
        scan_id = db.record_workspace_scan_run(
            purpose="capture",
            task_id="T-test-1",
            step_id="S-test-1",
            status="completed",
            metadata={"agent": "codex"},
        )
        cur = db.conn.execute(
            "SELECT purpose, task_id, step_id, status, metadata_json, "
            "baseline_type, started_at, completed_at "
            "FROM workspace_scan_runs WHERE id = ?",
            (scan_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["purpose"] == "capture"
        assert row["task_id"] == "T-test-1"
        assert row["step_id"] == "S-test-1"
        assert row["status"] == "completed"
        assert row["metadata_json"] == '{"agent": "codex"}'
        assert row["baseline_type"] in ("git", "mtime")
        assert row["started_at"] > 0
        assert row["completed_at"] is None  # completed_at 由 update_scan_run_status 设置
    finally:
        db.close()


def test_get_latest_scan_run_returns_most_recent():
    """get_latest_scan_run 返回最近一次扫描基线。"""
    db, _root = _db_with_workspace()
    try:
        import time as _time
        sid1 = db.record_workspace_scan_run(purpose="bootstrap")
        _time.sleep(0.05)
        sid2 = db.record_workspace_scan_run(purpose="bootstrap")

        latest = db.get_latest_scan_run()
        assert latest is not None
        assert latest["id"] == sid2

        # purpose 过滤
        latest_capture = db.get_latest_scan_run(purpose="capture")
        assert latest_capture is None

        # task_id 过滤
        db.record_workspace_scan_run(purpose="capture", task_id="T-xyz")
        latest_task = db.get_latest_scan_run(task_id="T-xyz")
        assert latest_task is not None
        assert latest_task["task_id"] == "T-xyz"
    finally:
        db.close()


def test_update_scan_run_status_sets_completed():
    """update_scan_run_status 将 running 更新为 completed 并写入 changed_files。"""
    db, _root = _db_with_workspace()
    try:
        scan_id = db.record_workspace_scan_run(purpose="bootstrap", status="running")
        changed = [{"path": "a.py", "status": "M"}, {"path": "b.py", "status": "A"}]
        ok = db.update_scan_run_status(scan_id, "completed", changed_files=changed)
        assert ok is True

        cur = db.conn.execute(
            "SELECT status, completed_at, changed_files_json "
            "FROM workspace_scan_runs WHERE id = ?",
            (scan_id,),
        )
        row = cur.fetchone()
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        import json as _json
        parsed = _json.loads(row["changed_files_json"])
        assert len(parsed) == 2
        assert parsed[0]["path"] == "a.py"
    finally:
        db.close()


def test_update_scan_run_status_invalid_id():
    """update_scan_run_status 无效 ID 返回 False。"""
    db, _root = _db_with_workspace()
    try:
        ok = db.update_scan_run_status(99999, "completed")
        assert ok is False
        ok = db.update_scan_run_status(0, "completed")
        assert ok is False
    finally:
        db.close()


def test_get_workspace_changes_since_non_git_fallback():
    """非 Git 项目回退到 file_instances.mtime 对比。

    构造一个临时目录（无 .git），写入 file_instances 记录，
    修改磁盘文件 mtime，验证 get_workspace_changes_since 能检测到。
    """
    db, root = _db_with_workspace()
    try:
        # 非目录 .git，_is_git_repo 返回 False
        assert db._is_git_repo() is False

        # 构造一个文件实例记录
        test_file = os.path.join(root, "demo.py")
        with open(test_file, "w", encoding="utf-8") as f:
            f.write("print('hello')\n")

        # 写入 file_instances（mtime 较早）
        old_mtime = 1.0
        ws_id = db._get_active_workspace_id()
        content_hash = "fakehash0001"
        db.conn.execute(
            "INSERT INTO file_instances "
            "(workspace_id, rel_path, abs_path, current_content_hash, mtime, "
            " total_lines, last_parsed, status, module_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ws_id, "demo.py", test_file, content_hash, old_mtime, 1, 0, "parsed", ""),
        )
        db.conn.commit()

        # 磁盘文件 mtime 比 db 记录的旧 mtime 新
        # （刚写入的文件 mtime 自然远大于 1.0）
        result = db.get_workspace_changes_since(scan_id=0)
        assert result["baseline_type"] == "mtime"
        # 至少检测到 demo.py 被修改
        paths = [c["path"] for c in result["changed_files"]]
        assert "demo.py" in paths
    finally:
        db.close()


def test_get_workspace_changes_since_non_git_detects_deleted():
    """非 Git 项目检测到已删除文件（D 状态）。"""
    db, root = _db_with_workspace()
    try:
        ws_id = db._get_active_workspace_id()
        deleted_file = os.path.join(root, "deleted.py")
        # 写入 file_instances 但磁盘文件不存在
        db.conn.execute(
            "INSERT INTO file_instances "
            "(workspace_id, rel_path, abs_path, current_content_hash, mtime, "
            " total_lines, last_parsed, status, module_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ws_id, "deleted.py", deleted_file, "hash1", 1.0, 1, 0, "parsed", ""),
        )
        db.conn.commit()

        result = db.get_workspace_changes_since(scan_id=0)
        paths = [c for c in result["changed_files"] if c["path"] == "deleted.py"]
        assert len(paths) == 1
        assert paths[0]["status"] == "D"
    finally:
        db.close()


def test_parse_git_porcelain_basic():
    """_parse_git_porcelain 正确解析各种状态码。"""
    db, _root = _db_with_workspace()
    try:
        status = (
            " M modified.py\n"
            "A  staged.py\n"
            "?? untracked.py\n"
            "D  deleted.py\n"
            "R  renamed_old.py -> renamed_new.py\n"
        )
        result = db._parse_git_porcelain(status)
        assert len(result) == 5

        # modified
        assert result[0]["path"] == "modified.py"
        assert result[0]["worktree"] == "M"

        # staged
        assert result[1]["path"] == "staged.py"
        assert result[1]["staged"] == "A"

        # untracked
        assert result[2]["path"] == "untracked.py"
        assert result[2]["status"] == "untracked"

        # deleted
        assert result[3]["path"] == "deleted.py"
        assert result[3]["staged"] == "D"

        # renamed: path 取新名
        assert result[4]["path"] == "renamed_new.py"
    finally:
        db.close()


def test_parse_git_porcelain_empty():
    """_parse_git_porcelain 空输入或纯空格行返回空列表。"""
    db, _root = _db_with_workspace()
    try:
        assert db._parse_git_porcelain("") == []
        # 纯空格行（无路径）应被跳过
        assert db._parse_git_porcelain("   \n  \n") == []
    finally:
        db.close()


def _init_git_repo(root):
    """在临时目录初始化一个 git 仓库并提交一个文件。

    同时添加 .gitignore 排除 callwarden.db 数据库文件，避免测试数据库被
    git status 当作 untracked 文件干扰变化检测测试。
    """
    import subprocess
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "test"
    env["GIT_AUTHOR_EMAIL"] = "test@test.com"
    env["GIT_COMMITTER_NAME"] = "test"
    env["GIT_COMMITTER_EMAIL"] = "test@test.com"
    subprocess.run(["git", "init"], cwd=root, capture_output=True, check=True, env=env)
    # 添加 .gitignore 排除数据库文件
    with open(os.path.join(root, ".gitignore"), "w", encoding="utf-8") as fh:
        fh.write("callwarden.db*\n*.pyc\n__pycache__/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=root, capture_output=True, check=True, env=env)
    # 写入并提交一个文件
    f = os.path.join(root, "committed.py")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("print('initial')\n")
    subprocess.run(["git", "add", "committed.py"], cwd=root, capture_output=True, check=True, env=env)
    subprocess.run(["git", "commit", "-m", "init"], cwd=root, capture_output=True, check=True, env=env)
    # 获取 HEAD
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, check=True, env=env)
    return r.stdout.strip()


def test_get_workspace_changes_since_git_dirty():
    """Git 项目：检测 dirty（unstaged 修改）文件。"""
    db, root = _db_with_workspace()
    try:
        head = _init_git_repo(root)
        assert db._is_git_repo() is True

        # 修改已提交文件（unstaged）
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("print('modified')\n")

        result = db.get_workspace_changes_since(base_commit=head)
        assert result["baseline_type"] == "git"
        assert result["base_commit"] == head
        assert result["current_head"] == head  # 未提交，HEAD 不变
        assert result["is_dirty"] is True
        paths = [c["path"] for c in result["changed_files"]]
        assert "committed.py" in paths
    finally:
        db.close()


def test_get_workspace_changes_since_git_untracked():
    """Git 项目：检测 untracked 文件，include_untracked 控制是否包含。"""
    db, root = _db_with_workspace()
    try:
        head = _init_git_repo(root)
        # 新增 untracked 文件
        with open(os.path.join(root, "new_file.py"), "w", encoding="utf-8") as f:
            f.write("print('new')\n")

        # 默认包含 untracked
        result = db.get_workspace_changes_since(base_commit=head)
        paths = [c["path"] for c in result["changed_files"]]
        assert "new_file.py" in paths

        # exclude untracked
        result2 = db.get_workspace_changes_since(
            base_commit=head, include_untracked=False
        )
        paths2 = [c["path"] for c in result2["changed_files"]]
        assert "new_file.py" not in paths2
    finally:
        db.close()


def test_get_workspace_changes_since_git_clean():
    """Git 项目：无变更时 changed_files 为空，is_dirty 为 False。"""
    db, root = _db_with_workspace()
    try:
        head = _init_git_repo(root)
        result = db.get_workspace_changes_since(base_commit=head)
        assert result["is_dirty"] is False
        assert result["changed_files"] == []
        assert result["current_head"] == head
    finally:
        db.close()
