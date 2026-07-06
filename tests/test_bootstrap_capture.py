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

import pytest

from callwarden.db.schema import SCHEMA_VERSION
from callwarden.db.db import CodeGraphDB


# ----------------------------------------------------------------------
# 基础断言
# ----------------------------------------------------------------------

def test_schema_version_is_25():
    """SCHEMA_VERSION 常量不低于 25（workspace_scan_runs 引入版本）。"""
    assert SCHEMA_VERSION >= 25


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
        # 当前 SCHEMA_VERSION 可能高于 25，需删除所有 >= 25 的版本记录以模拟 v24
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM schema_version WHERE version >= 25")
        conn.execute("DROP TABLE IF EXISTS workspace_scan_runs")
        conn.execute("DROP INDEX IF EXISTS idx_workspace_scan_runs_workspace")
        conn.execute("DROP INDEX IF EXISTS idx_workspace_scan_runs_task")
        conn.execute("DROP INDEX IF EXISTS idx_workspace_scan_runs_git_head")
        conn.commit()
        conn.close()

        # 重新打开触发 v24 -> v25 迁移（后续可能继续到 v26+）
        db = CodeGraphDB(db_path=db_path, workspace_root=tmp)
        try:
            v = db.conn.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
            assert v["version"] >= 25

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


# ----------------------------------------------------------------------
# task_capture_diff 闭环入口测试
# ----------------------------------------------------------------------

def _db_with_git_repo_and_task():
    """构造临时工作区 + git 仓库 + 一个关联任务。

    返回 (db, root, head, task_id)，调用方负责 db.close()。
    """
    db, root = _db_with_workspace()
    head = _init_git_repo(root)
    task_id = db.task_create(
        title="capture-diff 测试任务",
        description="为 task_capture_diff 测试构造的真实任务",
    )
    return db, root, head, task_id


def test_task_capture_diff_dry_run_no_writes():
    """dry_run=True 不写 change_audit / audit_chain / 已完成 scan_runs。"""
    db, root, head, task_id = _db_with_git_repo_and_task()
    try:
        # 制造一个 dirty 文件
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# extra\n")

        before_audit = db.conn.execute(
            "SELECT COUNT(*) FROM change_audit"
        ).fetchone()[0]
        before_chain = db.conn.execute(
            "SELECT COUNT(*) FROM audit_chain"
        ).fetchone()[0]

        result = db.task_capture_diff(
            task_id=task_id, step_id="S-test", base=head, dry_run=True
        )

        # dry_run 应只返回计划，不落库
        after_audit = db.conn.execute(
            "SELECT COUNT(*) FROM change_audit"
        ).fetchone()[0]
        after_chain = db.conn.execute(
            "SELECT COUNT(*) FROM audit_chain"
        ).fetchone()[0]
        assert after_audit == before_audit, "dry-run 不应写 change_audit"
        assert after_chain == before_chain, "dry-run 不应写 audit_chain"

        # dry_run 不应创建已完成的 scan_run
        assert result["dry_run"] is True
        assert result["scan_id"] == 0
        assert result["next_action"] == "apply"
        assert len(result["changed_files"]) >= 1
        assert result["quality_findings"] == []
        assert result["quality_decision"] == ""
    finally:
        db.close()


def test_task_capture_diff_dry_run_empty_changes_next_action_noop():
    """clean repo + dry_run=True → changed_files 为空，next_action='noop'。"""
    db, _root, head, task_id = _db_with_git_repo_and_task()
    try:
        result = db.task_capture_diff(
            task_id=task_id, base=head, dry_run=True
        )
        assert result["dry_run"] is True
        assert result["changed_files"] == []
        assert result["next_action"] == "noop"
    finally:
        db.close()


def test_task_capture_diff_apply_writes_change_audit():
    """apply 模式：变更文件写入 change_audit，含 hash_before/hash_after。"""
    db, root, head, task_id = _db_with_git_repo_and_task()
    try:
        # dirty：修改已提交文件
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("print('extra')\n")

        result = db.task_capture_diff(
            task_id=task_id, step_id="S-cap1", base=head, dry_run=False
        )

        assert result["dry_run"] is False
        assert result["scan_id"] > 0
        assert result["next_action"] in ("review", "fix")
        assert len(result["changed_files"]) >= 1
        assert len(result["linked_symbols"]) >= 1

        # 验证 change_audit 落库
        cur = db.conn.execute(
            "SELECT id, file_path, hash_before, hash_after, author, task_id, step_id "
            "FROM change_audit WHERE task_id = ?",
            (task_id,),
        )
        rows = cur.fetchall()
        assert len(rows) >= 1, "change_audit 应至少有一条记录"
        r = rows[0]
        assert r["file_path"] == "committed.py"
        assert r["author"] == "capture-diff"
        assert r["task_id"] == task_id
        assert r["step_id"] == "S-cap1"
        # hash_before 在 file_instances 无记录时为空串，hash_after 来自磁盘
        # 这里不强制 hash_before 非空（取决于 file_instances 是否有记录）
        assert r["hash_after"] != ""  # 磁盘文件存在，hash_after 应非空
    finally:
        db.close()


def test_task_capture_diff_apply_writes_audit_chain_signatures():
    """apply 模式：为每个 change_audit 写入 audit_chain 签名。"""
    db, root, head, task_id = _db_with_git_repo_and_task()
    try:
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# dirty\n")
        # 新增 untracked 文件，触发两条变更
        with open(os.path.join(root, "new_file.py"), "w", encoding="utf-8") as f:
            f.write("print('new')\n")

        db.task_capture_diff(
            task_id=task_id, step_id="S-cap2", base=head, dry_run=False
        )

        # 每个 change_audit.id 都应在 audit_chain 有对应签名记录
        cur = db.conn.execute(
            "SELECT id FROM change_audit WHERE task_id = ?",
            (task_id,),
        )
        change_ids = [row["id"] for row in cur.fetchall()]
        assert len(change_ids) >= 1

        for cid in change_ids:
            cur2 = db.conn.execute(
                "SELECT COUNT(*) FROM audit_chain "
                "WHERE table_name = 'change_audit' AND record_id = ?",
                (cid,),
            )
            n = cur2.fetchone()[0]
            assert n >= 1, f"change_audit {cid} 缺少 audit_chain 签名记录"
    finally:
        db.close()


def test_task_capture_diff_apply_links_task_symbol_changes():
    """apply 模式：每个变更文件尽量关联 task_symbol_changes。"""
    db, root, head, task_id = _db_with_git_repo_and_task()
    try:
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# dirty\n")

        result = db.task_capture_diff(
            task_id=task_id, step_id="S-cap3", base=head, dry_run=False
        )

        # linked_symbols 列表应与 changed_files 数量一致（best-effort，可能为 0）
        assert isinstance(result["linked_symbols"], list)

        # 验证 task_symbol_changes 表有记录（capture-diff 应触发）
        cur = db.conn.execute(
            "SELECT COUNT(*) FROM task_symbol_changes "
            "WHERE task_id = ? AND source = 'task_capture_diff'",
            (task_id,),
        )
        n = cur.fetchone()[0]
        assert n >= 1, "task_symbol_changes 应至少有一条 task_capture_diff 来源的记录"
    finally:
        db.close()


def test_task_capture_diff_apply_completes_scan_run():
    """apply 模式：scan_run 状态由 running 更新为 completed。"""
    db, root, head, task_id = _db_with_git_repo_and_task()
    try:
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# dirty\n")

        result = db.task_capture_diff(
            task_id=task_id, step_id="S-cap4", base=head, dry_run=False
        )
        scan_id = result["scan_id"]
        assert scan_id > 0

        cur = db.conn.execute(
            "SELECT status, completed_at, changed_files_json, purpose, task_id "
            "FROM workspace_scan_runs WHERE id = ?",
            (scan_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["status"] == "completed"
        assert row["completed_at"] is not None
        assert row["purpose"] == "capture"
        assert row["task_id"] == task_id
        # changed_files_json 应为非空数组
        import json as _json
        changed = _json.loads(row["changed_files_json"])
        assert len(changed) >= 1
    finally:
        db.close()


def test_task_capture_diff_apply_empty_changes_next_action_noop():
    """apply 模式 + 无变更 → next_action='noop'，不写 change_audit。"""
    db, _root, head, task_id = _db_with_git_repo_and_task()
    try:
        before = db.conn.execute(
            "SELECT COUNT(*) FROM change_audit WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]

        result = db.task_capture_diff(
            task_id=task_id, base=head, dry_run=False
        )

        assert result["dry_run"] is False
        assert result["changed_files"] == []
        assert result["next_action"] == "noop"

        after = db.conn.execute(
            "SELECT COUNT(*) FROM change_audit WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
        assert after == before, "无变更时不应写 change_audit"
    finally:
        db.close()


def test_task_capture_diff_apply_returns_quality_fields():
    """apply 模式：返回结果包含 quality_findings 和 quality_decision 字段。"""
    db, root, head, task_id = _db_with_git_repo_and_task()
    try:
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# dirty\n")

        result = db.task_capture_diff(
            task_id=task_id, step_id="S-cap5", base=head, dry_run=False
        )

        # quality_decision 为字符串（pass/warn/block 之一或空串）
        assert isinstance(result["quality_decision"], str)
        assert result["quality_decision"] in ("", "pass", "warn", "block")
        # quality_findings 为列表
        assert isinstance(result["quality_findings"], list)
    finally:
        db.close()


def test_task_capture_diff_apply_block_decision_next_action_fix():
    """quality_decision='block' 时 next_action='fix'。

    通过 monkey-patch run_task_completion_review 强制返回 block 决策。
    """
    db, root, head, task_id = _db_with_git_repo_and_task()
    try:
        with open(os.path.join(root, "committed.py"), "a", encoding="utf-8") as f:
            f.write("# dirty\n")

        # monkey-patch 让 run_task_completion_review 返回 block 决策
        original = getattr(db, "run_task_completion_review", None)
        db.run_task_completion_review = lambda tid, sid="": {
            "decision": "block",
            "findings": [{"severity": "block", "message": "mocked"}],
            "summary": "mocked block",
            "counts": {"info": 0, "warn": 0, "error": 0, "block": 1},
            "check_gate_result": None,
        }
        try:
            result = db.task_capture_diff(
                task_id=task_id, step_id="S-cap6", base=head, dry_run=False
            )
            assert result["quality_decision"] == "block"
            assert result["next_action"] == "fix"
            assert len(result["quality_findings"]) >= 1
        finally:
            if original is not None:
                db.run_task_completion_review = original
    finally:
        db.close()


# ----------------------------------------------------------------------
# CLI cw task capture-diff 与 MCP task_capture_diff 测试
# ----------------------------------------------------------------------

def test_cli_task_capture_diff_help_no_db():
    """cw task capture-diff --help 不应初始化数据库。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    old_argv = sys.argv
    sys.argv = ["cw", "task", "capture-diff", "--help"]
    try:
        db_init_called = {"count": 0}

        def fake_init(self, *args, **kwargs):
            db_init_called["count"] += 1
            raise RuntimeError("db should not be initialized for --help")

        with mock.patch.object(CodeGraphDB, "__init__", fake_init):
            with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                try:
                    cli_main._run_subcommand_mode()
                except RuntimeError as e:
                    if "should not" in str(e):
                        pytest.fail("db initialized during cw task capture-diff --help")
                    raise
        assert db_init_called["count"] == 0
    finally:
        sys.argv = old_argv


def test_cli_task_capture_diff_dry_run_calls_db_method():
    """cw task capture-diff --dry-run 必须调用 db.task_capture_diff(dry_run=True)。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            tid = db.task_create("cli-dry-run-test", "desc", [])
            call_log = {"count": 0, "kwargs": None}
            original = db.task_capture_diff

            def spy(*args, **kwargs):
                call_log["count"] += 1
                call_log["kwargs"] = kwargs
                return original(*args, **kwargs)

            with mock.patch.object(db, "task_capture_diff", side_effect=spy):
                old_argv = sys.argv
                sys.argv = ["cw", "task", "capture-diff", tid, "--dry-run"]
                try:
                    cli_main._handle_task(["capture-diff", tid, "--dry-run"], db)
                except SystemExit:
                    pass
                finally:
                    sys.argv = old_argv

            assert call_log["count"] == 1, "db.task_capture_diff 必须被调用一次"
            kw = call_log["kwargs"] or {}
            assert kw.get("dry_run") is True
            assert kw.get("task_id") == tid
        finally:
            db.close()


def test_cli_task_capture_diff_apply_passes_dry_run_false():
    """cw task capture-diff（不带 --dry-run）必须以 dry_run=False 调用 db 方法。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            tid = db.task_create("cli-apply-test", "desc", [])
            call_log = {"kwargs": None}
            original = db.task_capture_diff

            def spy(*args, **kwargs):
                call_log["kwargs"] = kwargs
                # 不真正执行 apply，避免触发质量审查的副作用
                return {
                    "task_id": kwargs.get("task_id", ""),
                    "step_id": kwargs.get("step_id", ""),
                    "dry_run": False,
                    "scan_id": 1,
                    "changed_files": [{"path": "demo.py", "status": "M"}],
                    "linked_symbols": [{"file_path": "demo.py", "change_id": "C-1", "linked": True}],
                    "quality_findings": [],
                    "quality_decision": "pass",
                    "next_action": "review",
                }

            with mock.patch.object(db, "task_capture_diff", side_effect=spy):
                old_argv = sys.argv
                sys.argv = ["cw", "task", "capture-diff", tid, "--step-id", "S-1"]
                try:
                    cli_main._handle_task(["capture-diff", tid, "--step-id", "S-1"], db)
                except SystemExit:
                    pass
                finally:
                    sys.argv = old_argv

            kw = call_log["kwargs"] or {}
            assert kw.get("dry_run") is False, "未传 --dry-run 时 dry_run 必须为 False"
            assert kw.get("step_id") == "S-1"
        finally:
            db.close()


def test_mcp_task_capture_diff_registered():
    """MCP server 注册了 task_capture_diff 工具。"""
    import inspect
    from callwarden.server import mcp_server

    # create_mcp_server 内部定义 task_capture_diff，无法直接拿到引用，
    # 但可以通过源代码字符串验证工具已注册。
    src = inspect.getsource(mcp_server.create_mcp_server)
    assert "def task_capture_diff(" in src, "MCP 源码缺少 task_capture_diff 工具定义"
    assert "@mcp.tool()" in src, "MCP 源码缺少 @mcp.tool() 装饰器"


def test_mcp_task_capture_diff_signature():
    """task_capture_diff MCP 工具签名包含 task_id/step_id/base/dry_run。"""
    import ast
    import os as _os

    src_path = _os.path.join(
        _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
        "server", "mcp_server.py",
    )
    with open(src_path, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    func_def = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "task_capture_diff":
            func_def = node
            break
    assert func_def is not None, "未找到 task_capture_diff 函数定义"

    arg_names = [a.arg for a in func_def.args.args]
    assert "task_id" in arg_names
    assert "step_id" in arg_names
    assert "base" in arg_names
    assert "dry_run" in arg_names
