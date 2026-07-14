"""任务-符号变更归因层测试。"""

import os
import tempfile

from callwarden.db.db import CodeGraphDB


def _db_with_workspace():
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def test_propose_edit_records_file_attribution():
    db, root = _db_with_workspace()
    try:
        path = os.path.join(root, "sample.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("x = 1\n")

        task_id = db.task_create("attr", steps=[{"action": "edit", "target_file": "sample.py"}])
        step = db.task_next_step(task_id)
        result = db.propose_range_patch("sample.py", 1, 1, "x = 2", agent_task_id=task_id)

        assert result["success"] is True
        assert result["attribution"]["success"] is True
        changes = db.get_task_symbol_changes(task_id)
        assert len(changes) == 1
        assert changes[0]["task_id"] == task_id
        assert changes[0]["step_id"] == step["step_id"]
        assert changes[0]["file_path"] == "sample.py"
        assert changes[0]["edit_audit_id"] == result["audit_id"]
        assert changes[0]["source"] == "file_edit_audit"
    finally:
        db.close()


def test_task_report_step_records_symbol_attribution():
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("attr", steps=[{"action": "edit", "target_file": "sample.py"}])
        step = db.task_next_step(task_id)
        db.task_report_step(
            task_id,
            step["step_id"],
            result="done",
            changes=[
                {
                    "file_path": "sample.py",
                    "hash_before": "file-before",
                    "hash_after": "file-after",
                    "qualified_name": "sample.foo",
                    "symbol_name": "foo",
                    "symbol_hash_before": "sym-before",
                    "symbol_hash_after": "sym-after",
                    "change_type": "modified",
                    "diff": "changed foo",
                }
            ],
        )

        changes = db.get_task_symbol_changes(task_id)
        assert len(changes) == 1
        assert changes[0]["qualified_name"] == "sample.foo"
        assert changes[0]["symbol_hash_before"] == "sym-before"
        assert changes[0]["symbol_hash_after"] == "sym-after"
        assert changes[0]["change_audit_id"]
        assert changes[0]["metadata"]["file_hash_before"] == "file-before"
    finally:
        db.close()


def test_link_edit_audit_symbols_after_refresh():
    db, root = _db_with_workspace()
    try:
        path = os.path.join(root, "sample.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("def foo():\n    return 1\n")
        db.build_full_graph(force=True)

        task_id = db.task_create("attr", steps=[{"action": "edit", "target_file": "sample.py"}])
        step = db.task_next_step(task_id)
        result = db.propose_range_patch(
            "sample.py",
            2,
            2,
            "    return 2",
            agent_task_id=task_id,
            expected_hash=result_hash(db, "sample.py"),
        )
        assert result["success"] is True
        db.build_full_graph(force=True)

        linked = db.link_edit_audit_symbols(result["audit_id"], step_id=step["step_id"])
        assert linked["success"] is True
        assert linked["linked"] >= 1

        changes = [
            c for c in db.get_task_symbol_changes(task_id)
            if c["source"] == "edit_audit_symbol_diff"
        ]
        assert len(changes) == 1
        assert changes[0]["qualified_name"].endswith("foo")
        assert changes[0]["symbol_hash_before"]
        assert changes[0]["symbol_hash_after"]
        assert changes[0]["symbol_hash_before"] != changes[0]["symbol_hash_after"]
    finally:
        db.close()


def result_hash(db, file_path):
    abs_path = os.path.join(db.workspace_root, file_path)
    with open(abs_path, "r", encoding="utf-8") as f:
        return db._compute_sha256(f.read())


# ============================================
# 三角关联测试（v35：task ↔ commit ↔ symbol）
# ============================================


def _insert_git_commit(db, commit_hash: str, author: str, message: str, ts: float):
    """向 git_commits 表插入一条测试用 commit 记录"""
    ws_id = db._get_active_workspace_id()
    db.conn.execute(
        "INSERT INTO git_commits (commit_hash, author, email, message, timestamp, workspace_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (commit_hash, author, "tester@example.com", message, ts, ws_id),
    )
    db.conn.commit()


def test_record_task_symbol_change_with_source_commit_hash():
    """record_task_symbol_change 传 source_commit_hash 应写入字段"""
    db, _ = _db_with_workspace()
    try:
        task_id = db.task_create("triangle")
        result = db.record_task_symbol_change(
            task_id=task_id,
            file_path="sample.py",
            qualified_name="sample.foo",
            symbol_hash_before="hash-a",
            symbol_hash_after="hash-b",
            change_type="modified",
            source_commit_hash="abc123",
        )
        assert result["success"] is True
        changes = db.get_task_symbol_changes(task_id)
        assert len(changes) == 1
        assert changes[0]["source_commit_hash"] == "abc123"
    finally:
        db.close()


def test_get_task_commits_join_git_commits():
    """get_task_commits 应通过 JOIN git_commits 返回聚合 commit 信息"""
    db, _ = _db_with_workspace()
    try:
        _insert_git_commit(db, "abc123", "tester", "fix: bug A\n\nBody", 1700000000.0)
        _insert_git_commit(db, "def456", "tester2", "feat: add B", 1700000100.0)

        task_id = db.task_create("triangle")
        # 2 条变更关联 abc123，1 条关联 def456
        for i in range(2):
            db.record_task_symbol_change(
                task_id=task_id,
                file_path="sample.py",
                qualified_name=f"sample.foo{i}",
                symbol_hash_before=f"hb-{i}",
                symbol_hash_after=f"ha-{i}",
                source_commit_hash="abc123",
            )
        db.record_task_symbol_change(
            task_id=task_id,
            file_path="sample.py",
            qualified_name="sample.bar",
            symbol_hash_before="hb-2",
            symbol_hash_after="ha-2",
            source_commit_hash="def456",
        )

        # 查询 task → commit（含详情）
        commits = db.get_task_commits(task_id)
        assert len(commits) == 2
        # 按 last_change_at DESC 排序，def456 应在前（最后写入）
        assert commits[0]["source_commit_hash"] == "def456"
        assert commits[0]["change_count"] == 1
        assert commits[0]["commit_author"] == "tester2"
        assert commits[0]["commit_subject"] == "feat: add B"
        assert commits[1]["source_commit_hash"] == "abc123"
        assert commits[1]["change_count"] == 2
        assert commits[1]["commit_author"] == "tester"
        assert commits[1]["commit_subject"] == "fix: bug A"

        # include_commit_details=False：不应包含 commit 详情字段
        commits_simple = db.get_task_commits(task_id, include_commit_details=False)
        assert len(commits_simple) == 2
        assert "commit_author" not in commits_simple[0]
        assert "commit_subject" not in commits_simple[0]
    finally:
        db.close()


def test_get_commit_tasks_join_tasks():
    """get_commit_tasks 应通过 JOIN tasks 表返回聚合 task 信息"""
    db, _ = _db_with_workspace()
    try:
        task1 = db.task_create("triangle1")
        task2 = db.task_create("triangle2")

        # task1 + commit abc123 写入 2 条
        for i in range(2):
            db.record_task_symbol_change(
                task_id=task1,
                file_path="sample.py",
                qualified_name=f"sample.foo{i}",
                symbol_hash_before=f"hb-{i}",
                symbol_hash_after=f"ha-{i}",
                source_commit_hash="abc123",
            )
        # task2 + commit abc123 写入 1 条
        db.record_task_symbol_change(
            task_id=task2,
            file_path="sample.py",
            qualified_name="sample.bar",
            symbol_hash_before="hb-2",
            symbol_hash_after="ha-2",
            source_commit_hash="abc123",
        )

        # 查询 commit → task（含详情）
        tasks = db.get_commit_tasks("abc123")
        assert len(tasks) == 2
        # 验证聚合 count
        all_counts = {t["task_id"]: t["change_count"] for t in tasks}
        assert all_counts[task1] == 2
        assert all_counts[task2] == 1
        # 验证 task 详情
        for t in tasks:
            if t["task_id"] == task1:
                assert t["task_title"] == "triangle1"
            elif t["task_id"] == task2:
                assert t["task_title"] == "triangle2"
            assert "task_status" in t
            assert "task_parent_id" in t

        # include_task_details=False：不应包含 task 详情字段
        tasks_simple = db.get_commit_tasks("abc123", include_task_details=False)
        assert len(tasks_simple) == 2
        assert "task_title" not in tasks_simple[0]
        assert "task_status" not in tasks_simple[0]
    finally:
        db.close()


def test_get_task_commits_empty_when_no_commit_hash():
    """没有 source_commit_hash 的记录应被 get_task_commits 过滤掉"""
    db, _ = _db_with_workspace()
    try:
        task_id = db.task_create("triangle")
        # 不传 source_commit_hash
        db.record_task_symbol_change(
            task_id=task_id,
            file_path="sample.py",
            qualified_name="sample.foo",
            symbol_hash_before="hb",
            symbol_hash_after="ha",
        )
        commits = db.get_task_commits(task_id)
        assert commits == []
    finally:
        db.close()


def test_get_commit_tasks_unknown_commit():
    """查询不存在关联的 commit 应返回空列表"""
    db, _ = _db_with_workspace()
    try:
        tasks = db.get_commit_tasks("nonexistent_hash_999")
        assert tasks == []
    finally:
        db.close()
