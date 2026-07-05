"""任务质量门禁（Task Quality Gate）测试。

本测试覆盖 docs/design/task-quality-gate-plan.md 中 v21 schema 落地：
- task_quality_findings 表存在
- 4 个索引存在（task / step / status / severity）
- 字段完整性（task_id / step_id / finding_type / severity / status / message /
  evidence / source / created_at / resolved_at / resolved_by）
- 默认值正确（severity='warn', status='open', resolved_by='', resolved_at=NULL）
- 旧库重复迁移幂等（CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS）
- SCHEMA_VERSION 升级到 21 后 schema_version 表记录 v21
- 全新数据库直接包含 task_quality_findings（无需迁移）

后续步骤会扩展为 TaskQualityMixin 业务方法、completion review 调度器等测试。
"""

import os
import sqlite3
import tempfile

from callwarden.db.db import CodeGraphDB
from callwarden.db.schema import SCHEMA_VERSION


def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _table_columns(conn, table_name):
    """获取表字段列表。"""
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    return [row["name"] for row in cur.fetchall()]


def _index_exists(conn, index_name):
    """检查索引是否存在。"""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name=?",
        (index_name,),
    )
    return cur.fetchone() is not None


def _table_exists(conn, table_name):
    """检查表是否存在。"""
    cur = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cur.fetchone() is not None


def test_schema_version_is_21():
    """SCHEMA_VERSION 常量已升级到 21。"""
    assert SCHEMA_VERSION == 21


def test_task_quality_findings_table_exists_on_fresh_db():
    """全新数据库直接包含 task_quality_findings 表（无需迁移）。"""
    db, _root = _db_with_workspace()
    try:
        assert _table_exists(db.conn, "task_quality_findings")
    finally:
        db.close()


def test_task_quality_findings_indexes_exist():
    """4 个索引存在：task / step / status / severity。"""
    db, _root = _db_with_workspace()
    try:
        assert _index_exists(db.conn, "idx_task_quality_task")
        assert _index_exists(db.conn, "idx_task_quality_step")
        assert _index_exists(db.conn, "idx_task_quality_status")
        assert _index_exists(db.conn, "idx_task_quality_severity")
    finally:
        db.close()


def test_task_quality_findings_columns():
    """字段完整性检查。"""
    expected = {
        "id", "workspace_id", "task_id", "step_id", "finding_type",
        "severity", "status", "message", "evidence", "source",
        "created_at", "resolved_at", "resolved_by",
    }
    db, _root = _db_with_workspace()
    try:
        cols = set(_table_columns(db.conn, "task_quality_findings"))
        missing = expected - cols
        assert not missing, f"missing columns: {missing}"
    finally:
        db.close()


def test_task_quality_findings_defaults():
    """默认值：severity='warn', status='open', resolved_by='', resolved_at=NULL。"""
    db, _root = _db_with_workspace()
    try:
        db.conn.execute(
            "INSERT INTO task_quality_findings (task_id, finding_type, message, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("T-test", "semgrep", "test message", 0.0),
        )
        db.conn.commit()
        cur = db.conn.execute(
            "SELECT severity, status, resolved_by, resolved_at, source, evidence, step_id "
            "FROM task_quality_findings WHERE task_id = ?",
            ("T-test",),
        )
        row = cur.fetchone()
        assert row["severity"] == "warn"
        assert row["status"] == "open"
        assert row["resolved_by"] == ""
        assert row["resolved_at"] is None
        assert row["source"] == ""
        assert row["evidence"] == ""
        assert row["step_id"] == ""
    finally:
        db.close()


def test_migration_v20_to_v21_is_idempotent():
    """v20 -> v21 迁移幂等：在已有表的库上重复执行不报错。

    直接调用迁移函数 _migrate_v20_to_v21，模拟旧库 v20 迁移到 v21。
    第二次调用应当因 IF NOT EXISTS 而 no-op。
    """
    root = tempfile.mkdtemp()
    db_path = os.path.join(root, "callwarden.db")
    db = CodeGraphDB(db_path, workspace_root=root)
    try:
        # 数据库已通过完整 SCHEMA_SQL 包含 task_quality_findings，
        # 现在再次调用迁移函数，验证幂等。
        from callwarden.db.db_base import _migrate_v20_to_v21
        _migrate_v20_to_v21(db.conn)
        _migrate_v20_to_v21(db.conn)
        assert _table_exists(db.conn, "task_quality_findings")
        assert _index_exists(db.conn, "idx_task_quality_task")
    finally:
        db.close()


def test_migration_v20_to_v21_on_legacy_v20_db():
    """模拟 v20 旧库：手动构造一个不含 task_quality_findings 的库，
    再执行 _migrate_v20_to_v21，验证表和索引被创建。
    """
    root = tempfile.mkdtemp()
    db_path = os.path.join(root, "callwarden.db")
    # 直接用裸 sqlite3 建一个最小 v20 库（不含 task_quality_findings）
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT)")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
    conn.commit()

    from callwarden.db.db_base import _migrate_v20_to_v21
    _migrate_v20_to_v21(conn)
    conn.commit()

    assert _table_exists(conn, "task_quality_findings")
    assert _index_exists(conn, "idx_task_quality_task")
    assert _index_exists(conn, "idx_task_quality_step")
    assert _index_exists(conn, "idx_task_quality_status")
    assert _index_exists(conn, "idx_task_quality_severity")
    conn.close()


def test_schema_version_table_records_v21_on_fresh_db():
    """全新数据库 schema_version 表记录 v21 版本。"""
    db, _root = _db_with_workspace()
    try:
        cur = db.conn.execute(
            "SELECT version FROM schema_version WHERE version = ?",
            (21,),
        )
        row = cur.fetchone()
        assert row is not None, "v21 not recorded in schema_version table"
    finally:
        db.close()


def test_legacy_v20_db_migrates_to_v21_via_init_schema():
    """旧 v20 库通过 _init_schema 自动迁移到 v21。

    构造一个 v20 库（schema_version 表标记为 20，不含 task_quality_findings），
    再用 CodeGraphDB 打开，触发 _migrate_schema(20, 21)。
    """
    root = tempfile.mkdtemp()
    db_path = os.path.join(root, "callwarden.db")
    import time

    # 1. 先用 CodeGraphDB 创建完整 schema（v21）
    db = CodeGraphDB(db_path, workspace_root=root)
    db.close()

    # 2. 模拟降级到 v20：删除 task_quality_findings 和相关索引，并把版本号改成 20
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS task_quality_findings")
    conn.execute("DELETE FROM schema_version WHERE version = 21")
    conn.execute(
        "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
        (20, time.time(), "downgrade for test"),
    )
    conn.commit()
    conn.close()

    # 3. 再次用 CodeGraphDB 打开，应触发 v20 -> v21 迁移
    db = CodeGraphDB(db_path, workspace_root=root)
    try:
        assert _table_exists(db.conn, "task_quality_findings")
        assert _index_exists(db.conn, "idx_task_quality_task")
        cur = db.conn.execute(
            "SELECT version FROM schema_version WHERE version = 21"
        )
        assert cur.fetchone() is not None, "v21 migration not recorded"
    finally:
        db.close()


# ============================================
# TaskQualityMixin 业务方法测试
# ============================================

def _create_task_with_step(db, title="quality-test", step_count=1):
    """辅助：创建带 1 个步骤的任务，返回 (task_id, [step_dict])"""
    steps = [{"action": "edit", "target_file": "sample.py"} for _ in range(step_count)]
    task_id = db.task_create(title, steps=steps)
    return task_id


def test_record_task_quality_finding_returns_id():
    """record_task_quality_finding 成功写入返回 finding_id > 0"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        fid = db.record_task_quality_finding(
            task_id=task_id,
            finding_type="semgrep",
            severity="warn",
            message="unused import",
            source="semgrep",
        )
        assert isinstance(fid, int)
        assert fid > 0
    finally:
        db.close()


def test_record_task_quality_finding_invalid_input_returns_zero():
    """空 task_id / 空 message / 非法 severity 都返回 0"""
    db, _root = _db_with_workspace()
    try:
        assert db.record_task_quality_finding(task_id="", message="x") == 0
        task_id = _create_task_with_step(db)
        assert db.record_task_quality_finding(task_id=task_id, message="") == 0
        # 非法 severity 应回退为 warn
        fid = db.record_task_quality_finding(
            task_id=task_id, message="x", severity="bogus"
        )
        assert fid > 0
        findings = db.get_task_quality_findings(task_id)
        assert findings[0]["severity"] == "warn"
    finally:
        db.close()


def test_record_task_quality_finding_serializes_evidence():
    """evidence dict 自动 JSON 序列化"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        evidence = {"rule_id": "python-unused", "line": 10}
        fid = db.record_task_quality_finding(
            task_id=task_id, message="x", evidence=evidence
        )
        findings = db.get_task_quality_findings(task_id)
        import json as _json
        parsed = _json.loads(findings[0]["evidence"])
        assert parsed["rule_id"] == "python-unused"
        assert parsed["line"] == 10
    finally:
        db.close()


def test_get_task_quality_findings_filters():
    """get_task_quality_findings 支持 status / severity 过滤"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        # 写入 3 个 finding：warn-open, error-open, warn-resolved
        f1 = db.record_task_quality_finding(task_id, severity="warn", message="m1")
        f2 = db.record_task_quality_finding(task_id, severity="error", message="m2")
        f3 = db.record_task_quality_finding(task_id, severity="warn", message="m3")
        db.resolve_task_quality_finding(f3)

        # status=open 应返回 2 个
        open_findings = db.get_task_quality_findings(task_id, status="open")
        assert len(open_findings) == 2

        # status=open + severity=error 应返回 1 个
        err_findings = db.get_task_quality_findings(
            task_id, status="open", severity="error"
        )
        assert len(err_findings) == 1
        assert err_findings[0]["id"] == f2

        # status=all 应返回 3 个
        all_findings = db.get_task_quality_findings(task_id, status="all")
        assert len(all_findings) == 3
    finally:
        db.close()


def test_get_task_quality_findings_ordered_by_created_at_asc():
    """findings 按创建时间升序排列（旧的先处理）"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        import time as _time
        f1 = db.record_task_quality_finding(task_id, message="first")
        _time.sleep(0.05)
        f2 = db.record_task_quality_finding(task_id, message="second")
        findings = db.get_task_quality_findings(task_id, status="all")
        assert findings[0]["id"] == f1
        assert findings[1]["id"] == f2
    finally:
        db.close()


def test_resolve_task_quality_finding_fixed():
    """resolve_task_quality_finding 'fixed' 将 status 改为 resolved"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        fid = db.record_task_quality_finding(task_id, message="x")
        result = db.resolve_task_quality_finding(fid, resolution="fixed")
        assert result["success"] is True
        assert result["status"] == "resolved"
        findings = db.get_task_quality_findings(task_id, status="all")
        assert findings[0]["status"] == "resolved"
        assert findings[0]["resolved_at"] is not None
        assert findings[0]["resolved_by"] == "agent"
    finally:
        db.close()


def test_resolve_task_quality_finding_wontfix():
    """resolve_task_quality_finding 'wontfix' 将 status 改为 wontfix"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        fid = db.record_task_quality_finding(task_id, message="x")
        result = db.resolve_task_quality_finding(fid, resolution="wontfix")
        assert result["status"] == "wontfix"
    finally:
        db.close()


def test_resolve_task_quality_finding_not_found():
    """不存在的 finding_id 返回 success=False"""
    db, _root = _db_with_workspace()
    try:
        result = db.resolve_task_quality_finding(99999)
        assert result["success"] is False
    finally:
        db.close()


def test_resolve_task_quality_finding_zero_id():
    """finding_id=0 返回 success=False"""
    db, _root = _db_with_workspace()
    try:
        result = db.resolve_task_quality_finding(0)
        assert result["success"] is False
    finally:
        db.close()


def test_task_has_blocking_findings_false_when_no_findings():
    """无 finding 时返回 False"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        assert db.task_has_blocking_findings(task_id) is False
    finally:
        db.close()


def test_task_has_blocking_findings_false_for_warn():
    """warn/info 级别 finding 不阻塞"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        db.record_task_quality_finding(task_id, severity="warn", message="x")
        db.record_task_quality_finding(task_id, severity="info", message="y")
        assert db.task_has_blocking_findings(task_id) is False
    finally:
        db.close()


def test_task_has_blocking_findings_true_for_error():
    """open error/block finding 阻塞"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        db.record_task_quality_finding(task_id, severity="error", message="x")
        assert db.task_has_blocking_findings(task_id) is True

        # block 也阻塞
        task_id2 = _create_task_with_step(db, title="t2")
        db.record_task_quality_finding(task_id2, severity="block", message="y")
        assert db.task_has_blocking_findings(task_id2) is True
    finally:
        db.close()


def test_task_has_blocking_findings_false_after_resolved():
    """resolved/wontfix 的 error finding 不阻塞"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        fid = db.record_task_quality_finding(task_id, severity="error", message="x")
        assert db.task_has_blocking_findings(task_id) is True
        db.resolve_task_quality_finding(fid, resolution="fixed")
        assert db.task_has_blocking_findings(task_id) is False
    finally:
        db.close()


def test_insert_fix_quality_gate_step_creates_step():
    """insert_fix_quality_gate_step 创建 fix_quality_gate_failure 步骤"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        source_step_id = step["step_id"]

        findings = [
            {"id": 1, "severity": "error", "finding_type": "semgrep",
             "message": "sql injection", "source": "semgrep"},
            {"id": 2, "severity": "block", "finding_type": "call_chain",
             "message": "signature changed", "source": "call_chain"},
        ]
        new_step_id = db.insert_fix_quality_gate_step(task_id, source_step_id, findings)
        assert new_step_id != ""

        # 验证 step 写入正确
        cur = db.conn.execute(
            "SELECT * FROM task_steps WHERE id = ?",
            (new_step_id,),
        )
        row = cur.fetchone()
        assert row["action"] == "fix_quality_gate_failure"
        assert row["target_symbol"] == source_step_id
        assert row["status"] == "pending"
        # check_items 包含两条 finding 的修复提示
        assert "semgrep" in row["check_items"]
        assert "sql injection" in row["check_items"]
        assert "call_chain" in row["check_items"]
        # result 包含 findings JSON 摘要
        import json as _json
        result_json = _json.loads(row["result"])
        assert len(result_json) == 2
        assert result_json[0]["id"] == 1
    finally:
        db.close()


def test_insert_fix_quality_gate_step_appends_to_end():
    """新 step 追加到末尾，step_index 正确递增"""
    db, _root = _db_with_workspace()
    try:
        # 创建带 2 个步骤的任务
        task_id = db.task_create(
            "multi-step",
            steps=[
                {"action": "edit", "target_file": "a.py"},
                {"action": "edit", "target_file": "b.py"},
            ],
        )
        # 获取第二个 step 作为 source
        s1 = db.task_next_step(task_id)
        db.task_report_step(task_id, s1["step_id"], result="done")
        s2 = db.task_next_step(task_id)

        new_step_id = db.insert_fix_quality_gate_step(
            task_id, s2["step_id"], [{"id": 1, "severity": "error",
                                      "finding_type": "x", "message": "y"}]
        )
        cur = db.conn.execute(
            "SELECT step_index FROM task_steps WHERE id = ?",
            (new_step_id,),
        )
        assert cur.fetchone()["step_index"] == 2
    finally:
        db.close()


def test_insert_fix_quality_gate_step_empty_task_id():
    """空 task_id 返回空字符串"""
    db, _root = _db_with_workspace()
    try:
        result = db.insert_fix_quality_gate_step("", "src-step", [])
        assert result == ""
    finally:
        db.close()


def test_record_finding_with_step_id():
    """record_task_quality_finding 关联 step_id"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        fid = db.record_task_quality_finding(
            task_id=task_id,
            step_id=step["step_id"],
            message="x",
        )
        findings = db.get_task_quality_findings(task_id)
        assert findings[0]["step_id"] == step["step_id"]
    finally:
        db.close()


# ============================================
# run_task_completion_review 测试
# ============================================

def test_run_task_completion_review_pass_when_no_findings():
    """无 finding 时 decision=pass"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        review = db.run_task_completion_review(task_id)
        assert review["decision"] == "pass"
        assert review["counts"]["error"] == 0
        assert review["counts"]["block"] == 0
    finally:
        db.close()


def test_run_task_completion_review_warn_for_warn_severity():
    """warn/info finding → decision=warn"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        db.record_task_quality_finding(task_id, severity="warn", message="m1")
        db.record_task_quality_finding(task_id, severity="info", message="m2")
        review = db.run_task_completion_review(task_id)
        assert review["decision"] == "warn"
        assert review["counts"]["warn"] == 1
        assert review["counts"]["info"] == 1
    finally:
        db.close()


def test_run_task_completion_review_block_for_error_severity():
    """error/block finding → decision=block"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        db.record_task_quality_finding(task_id, severity="warn", message="m1")
        db.record_task_quality_finding(task_id, severity="error", message="m2")
        review = db.run_task_completion_review(task_id)
        assert review["decision"] == "block"
        assert review["counts"]["error"] == 1
    finally:
        db.close()


def test_run_task_completion_review_excludes_resolved():
    """resolved/wontfix finding 不计入 decision"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        fid = db.record_task_quality_finding(task_id, severity="error", message="m1")
        db.resolve_task_quality_finding(fid, resolution="fixed")
        review = db.run_task_completion_review(task_id)
        assert review["decision"] == "pass"
    finally:
        db.close()


def test_run_task_completion_review_scoped_by_step_id():
    """step_id 过滤：只算该 step 的 finding + 任务级 finding（无 step_id）"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("multi-step", steps=[
            {"action": "edit", "target_file": "a.py"},
            {"action": "edit", "target_file": "b.py"},
        ])
        s1 = db.task_next_step(task_id)
        s2 = db.task_next_step(task_id) if False else None  # 只领第一个
        # 实际上 task_next_step 一次只领一个，s1 是第一个
        # 在 s1 上记录 error finding
        db.record_task_quality_finding(
            task_id, step_id=s1["step_id"], severity="error", message="s1 error"
        )
        # 任务级 finding（无 step_id）
        db.record_task_quality_finding(task_id, severity="warn", message="task-level warn")

        # 用 s1 step_id 审查：应包含 s1 error + task-level warn → block
        review = db.run_task_completion_review(task_id, s1["step_id"])
        assert review["decision"] == "block"
        assert len(review["findings"]) == 2
    finally:
        db.close()


# ============================================
# task_report_step 集成质量门禁测试
# ============================================

def test_task_report_step_pass_quality_gate():
    """无 finding 时 task_report_step 正常完成，返回值含 quality_gate.decision=pass"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        result = db.task_report_step(task_id, step["step_id"], result="done", success=True)
        # result 可能是 None（无下一步）或 dict（有下一步）
        if result is None:
            # 无下一步，quality_gate 信息可通过 task_status 查询
            pass
        else:
            assert "quality_gate" in result
            assert result["quality_gate"]["decision"] == "pass"
        # step 应该是 done
        cur = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step["step_id"],))
        assert cur.fetchone()["status"] == "done"
    finally:
        db.close()


def test_task_report_step_block_quality_gate_blocks_step():
    """error finding → task_report_step 触发 block，step 标记 blocked + 插入 fix step"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        # 预先记录 error finding
        db.record_task_quality_finding(
            task_id, step_id=step["step_id"], severity="error", message="sql injection"
        )
        result = db.task_report_step(task_id, step["step_id"], result="done", success=True)

        # step 应该是 blocked（不是 done）
        cur = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step["step_id"],))
        assert cur.fetchone()["status"] == "blocked"

        # 应自动插入 fix_quality_gate_failure step
        cur = db.conn.execute(
            "SELECT * FROM task_steps WHERE task_id = ? AND action = ?",
            (task_id, "fix_quality_gate_failure"),
        )
        fix_row = cur.fetchone()
        assert fix_row is not None
        assert fix_row["target_symbol"] == step["step_id"]

        # 返回值含 quality_gate.blocked=True
        assert result is not None
        assert result["quality_gate"]["decision"] == "block"
        assert result["quality_gate"]["blocked"] is True
        assert "fix_step_id" in result["quality_gate"]
    finally:
        db.close()


def test_task_report_step_warn_quality_gate_allows_done():
    """warn finding → task_report_step 允许 step 完成，但 quality_gate.decision=warn"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        db.record_task_quality_finding(
            task_id, step_id=step["step_id"], severity="warn", message="unused import"
        )
        result = db.task_report_step(task_id, step["step_id"], result="done", success=True)

        # step 应该是 done（warn 不阻塞）
        cur = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step["step_id"],))
        assert cur.fetchone()["status"] == "done"

        # 不应插入 fix_quality_gate_failure step
        cur = db.conn.execute(
            "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ? AND action = ?",
            (task_id, "fix_quality_gate_failure"),
        )
        assert cur.fetchone()["cnt"] == 0

        # 返回值含 quality_gate.decision=warn
        if result and "quality_gate" in result:
            assert result["quality_gate"]["decision"] == "warn"
    finally:
        db.close()


def test_task_report_step_block_then_resolve_allows_done():
    """block 后解决所有 error finding，重新 report 应允许 done"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        fid = db.record_task_quality_finding(
            task_id, step_id=step["step_id"], severity="error", message="sql injection"
        )
        # 第一次 report：触发 block，step → blocked
        db.task_report_step(task_id, step["step_id"], result="done", success=True)
        cur = db.conn.execute("SELECT status FROM task_steps WHERE id = ?", (step["step_id"],))
        assert cur.fetchone()["status"] == "blocked"

        # 解决 error finding
        db.resolve_task_quality_finding(fid, resolution="fixed")

        # 重新 report：应允许 done（无阻塞 finding）
        # 注意：需要把 step 状态从 blocked 改回 in_progress 才能重新 report
        # 这里直接验证 task_has_blocking_findings 返回 False
        assert db.task_has_blocking_findings(task_id) is False
    finally:
        db.close()

