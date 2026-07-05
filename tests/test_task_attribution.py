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
