"""L1 软门禁 + 赋能激励测试。

验证 propose_edit 系列的 task_id 校验和 task_context 返回。
对应 QA1 最终结论："让 Agent 用比不用更好，而不是必须用"。
"""

import os
import tempfile

from callwarden.db.db import CodeGraphDB


def _db_with_workspace():
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _write_sample(db, root, path="sample.py", content="x = 1\n"):
    abs_path = os.path.join(root, path)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return abs_path


# ============================================
# is_task_active 基础设施测试
# ============================================


def test_is_task_active_returns_false_for_empty():
    db, _ = _db_with_workspace()
    try:
        assert db.is_task_active("") is False
    finally:
        db.close()


def test_is_task_active_returns_false_for_nonexistent():
    db, _ = _db_with_workspace()
    try:
        assert db.is_task_active("nonexistent-id") is False
    finally:
        db.close()


def test_is_task_active_true_for_open_task():
    db, _ = _db_with_workspace()
    try:
        tid = db.task_create("test task")
        assert db.is_task_active(tid) is True
    finally:
        db.close()


def test_is_task_active_false_after_close():
    db, _ = _db_with_workspace()
    try:
        tid = db.task_create("test task")
        assert db.is_task_active(tid) is True
        db.task_close(tid)
        assert db.is_task_active(tid) is False
    finally:
        db.close()


# ============================================
# get_task_context 赋能字段测试
# ============================================


def test_get_task_context_returns_none_for_nonexistent():
    db, _ = _db_with_workspace()
    try:
        assert db.get_task_context("nonexistent-id") is None
    finally:
        db.close()


def test_get_task_context_returns_basic_info():
    db, _ = _db_with_workspace()
    try:
        tid = db.task_create("context test", steps=[
            {"action": "edit", "target_file": "sample.py"},
        ])
        ctx = db.get_task_context(tid)
        assert ctx is not None
        assert ctx["task_id"] == tid
        assert ctx["title"] == "context test"
        assert ctx["status"] == "open"
        assert ctx["is_active_task"] is False  # 未 set_active_task
        assert ctx["steps"]["total"] == 1
        assert ctx["steps"]["completed"] == 0
        assert ctx["steps"]["in_progress"] == 0
    finally:
        db.close()


def test_get_task_context_reflects_active_task():
    db, _ = _db_with_workspace()
    try:
        tid = db.task_create("active test")
        db.set_active_task(tid)
        ctx = db.get_task_context(tid)
        assert ctx["is_active_task"] is True
    finally:
        db.close()


# ============================================
# propose_edit 软门禁 + 赋能测试
# ============================================


def test_propose_edit_no_task_id_skips_validation():
    """agent_task_id="" 时跳过校验，完全向后兼容"""
    db, root = _db_with_workspace()
    try:
        _write_sample(db, root)
        result = db.propose_edit("sample.py", "x = 2\n")
        assert result["success"] is True
        assert result["task_validation"] is None
        assert result["task_context"] is None
    finally:
        db.close()


def test_propose_edit_valid_task_id_returns_context():
    """有效 task_id 时返回 task_validation=valid 和 task_context"""
    db, root = _db_with_workspace()
    try:
        _write_sample(db, root)
        tid = db.task_create("edit task", steps=[
            {"action": "edit", "target_file": "sample.py"},
        ])
        result = db.propose_edit("sample.py", "x = 2\n", agent_task_id=tid)
        assert result["success"] is True
        assert result["task_validation"] == {"status": "valid"}
        ctx = result["task_context"]
        assert ctx is not None
        assert ctx["task_id"] == tid
        assert ctx["title"] == "edit task"
        assert ctx["steps"]["total"] == 1
    finally:
        db.close()


def test_propose_edit_invalid_task_id_marks_validation_failed():
    """无效 task_id 时标记 task_validation=invalid（软门禁：不拒绝写入）"""
    db, root = _db_with_workspace()
    try:
        _write_sample(db, root)
        result = db.propose_edit("sample.py", "x = 2\n", agent_task_id="fake-id")
        # 软门禁：仍然写入成功（不拒绝），但标记 task_validation=invalid
        assert result["success"] is True
        assert result["task_validation"]["status"] == "invalid"
        assert "not found or not active" in result["task_validation"]["reason"]
        assert result["task_context"] is None
    finally:
        db.close()


def test_propose_edit_closed_task_id_marks_invalid():
    """已 close 的 task_id 标记为 invalid（不是活跃状态）"""
    db, root = _db_with_workspace()
    try:
        _write_sample(db, root)
        tid = db.task_create("closed task")
        db.task_close(tid)
        result = db.propose_edit("sample.py", "x = 2\n", agent_task_id=tid)
        assert result["success"] is True
        assert result["task_validation"]["status"] == "invalid"
    finally:
        db.close()


# ============================================
# propose_range_patch / propose_symbol_patch 继承测试
# ============================================


def test_propose_range_patch_inherits_task_validation():
    """propose_range_patch 调用 propose_edit，应继承 task_validation 字段"""
    db, root = _db_with_workspace()
    try:
        _write_sample(db, root, content="line1\nline2\nline3\n")
        tid = db.task_create("range task")
        result = db.propose_range_patch(
            "sample.py", 2, 2, "replaced",
            agent_task_id=tid,
        )
        assert result["success"] is True
        assert result["task_validation"] == {"status": "valid"}
        assert result["task_context"]["task_id"] == tid
        assert result["patch_scope"]["type"] == "range"
    finally:
        db.close()


def test_propose_symbol_patch_inherits_task_validation():
    """propose_symbol_patch 通过 range_patch 继承 task_validation"""
    db, root = _db_with_workspace()
    try:
        _write_sample(db, root, content="def foo():\n    return 1\n")
        db.build_full_graph(force=True)
        tid = db.task_create("symbol task")
        result = db.propose_symbol_patch(
            "sample.py", "foo", "    return 2",
            mode="replace",
            agent_task_id=tid,
        )
        assert result["success"] is True
        assert result["task_validation"] == {"status": "valid"}
        assert result["task_context"]["task_id"] == tid
        assert result["patch_scope"]["type"] == "symbol"
    finally:
        db.close()


# ============================================
# dry_run 路径测试
# ============================================


def test_propose_edit_dry_run_skips_task_validation():
    """dry_run 模式不触发写入，也不返回 task_validation（提前返回）"""
    db, root = _db_with_workspace()
    try:
        _write_sample(db, root)
        tid = db.task_create("dry run task")
        result = db.propose_edit(
            "sample.py", "x = 2\n",
            agent_task_id=tid,
            dry_run=True,
        )
        assert result["success"] is True
        assert result["status"] == "preview"
        # dry_run 在 task_validation 逻辑之前 return，所以字段不存在
        assert "task_validation" not in result or result.get("task_validation") is None
    finally:
        db.close()
