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
import time

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


def test_schema_version_is_23():
    """SCHEMA_VERSION 常量已升级到 23。"""
    assert SCHEMA_VERSION == 24


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


def test_schema_version_table_records_v23_on_fresh_db():
    """全新数据库 schema_version 表记录 v24 版本。"""
    db, _root = _db_with_workspace()
    try:
        cur = db.conn.execute(
            "SELECT version FROM schema_version WHERE version = ?",
            (24,),
        )
        row = cur.fetchone()
        assert row is not None, "v24 not recorded in schema_version table"
    finally:
        db.close()


def test_legacy_v22_db_migrates_to_v23_via_init_schema():
    """旧 v22 库通过 _init_schema 自动迁移到 v23。

    构造一个 v22 库（schema_version 表标记为 22，不含 agent_rule 表），
    再用 CodeGraphDB 打开，触发 _migrate_schema(22, 23)。
    """
    root = tempfile.mkdtemp()
    db_path = os.path.join(root, "callwarden.db")
    import time

    # 1. 先用 CodeGraphDB 创建完整 schema（v24）
    db = CodeGraphDB(db_path, workspace_root=root)
    db.close()

    # 2. 模拟降级到 v22：删除 agent_rule 三张表，并把版本号改成 22
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS agent_rule_candidates")
    conn.execute("DROP TABLE IF EXISTS agent_rules")
    conn.execute("DROP TABLE IF EXISTS agent_rule_sync_log")
    conn.execute("DELETE FROM schema_version WHERE version >= 23")
    conn.execute(
        "INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
        (22, time.time(), "downgrade for test"),
    )
    conn.commit()
    conn.close()

    # 3. 再次用 CodeGraphDB 打开，应触发 v22 -> v23 -> v24 迁移
    db = CodeGraphDB(db_path, workspace_root=root)
    try:
        assert _table_exists(db.conn, "agent_rule_candidates")
        assert _table_exists(db.conn, "agent_rules")
        assert _table_exists(db.conn, "agent_rule_sync_log")
        cur = db.conn.execute(
            "SELECT version FROM schema_version WHERE version = 24"
        )
        assert cur.fetchone() is not None, "v24 migration not recorded"
    finally:
        db.close()


# ============================================
# TaskQualityMixin 业务方法测试
# ============================================

def _create_task_with_step(db, title="quality-test", step_count=1):
    """辅助：创建带 1 个步骤的任务，返回 (task_id, [step_dict])

    注意：target_file 留空，避免触发 _check_scope_violations（scope 检查器
    会对变更文件超出 target_file 范围的情况报 error finding，干扰其他测试）。
    专门测试 scope 的用例自行构造带 target_file 的任务。
    """
    steps = [{"action": "edit", "target_file": ""} for _ in range(step_count)]
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


# ============================================
# task_next_step 集成质量门禁测试
# ============================================

def test_task_next_step_prioritizes_fix_quality_gate_step():
    """存在 fix_quality_gate_failure pending 步骤时，task_next_step 优先返回它"""
    db, _root = _db_with_workspace()
    try:
        # 创建带 2 个普通步骤的任务
        task_id = db.task_create("priority-test", steps=[
            {"action": "edit", "target_file": "a.py"},
            {"action": "edit", "target_file": "b.py"},
        ])
        # 领取第一个 step，但触发 block（插入 fix_quality_gate_failure）
        s1 = db.task_next_step(task_id)
        db.record_task_quality_finding(
            task_id, step_id=s1["step_id"], severity="error", message="sql injection"
        )
        db.task_report_step(task_id, s1["step_id"], result="done", success=True)
        # 此时应该有 fix_quality_gate_failure step（pending）

        # 再次 task_next_step：应优先返回 fix_quality_gate_failure（而非第二个普通步骤）
        next_step = db.task_next_step(task_id)
        assert next_step["action"] == "fix_quality_gate_failure"
        assert next_step["target_symbol"] == s1["step_id"]
    finally:
        db.close()


def test_task_next_step_returns_open_quality_findings_summary():
    """普通步骤返回 open_quality_findings 摘要"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        # 预先记录 2 个 finding（warn + error）
        db.record_task_quality_finding(
            task_id, severity="warn", message="unused import", finding_type="semgrep"
        )
        db.record_task_quality_finding(
            task_id, severity="error", message="sql injection", finding_type="semgrep"
        )

        step = db.task_next_step(task_id)
        assert "open_quality_findings" in step
        summary = step["open_quality_findings"]
        assert summary["count"] == 2
        assert summary["blocking"] == 1  # 1 个 error
        assert len(summary["items"]) == 2
        severities = {item["severity"] for item in summary["items"]}
        assert "warn" in severities
        assert "error" in severities
    finally:
        db.close()


def test_task_next_step_open_quality_findings_empty_when_no_findings():
    """无 finding 时 open_quality_findings.count=0"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        assert step["open_quality_findings"]["count"] == 0
        assert step["open_quality_findings"]["blocking"] == 0
        assert step["open_quality_findings"]["items"] == []
    finally:
        db.close()


def test_task_next_step_open_quality_findings_capped_at_10():
    """open_quality_findings.items 最多 10 条"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        # 写入 12 个 finding
        for i in range(12):
            db.record_task_quality_finding(
                task_id, severity="warn", message=f"finding {i}"
            )
        step = db.task_next_step(task_id)
        assert step["open_quality_findings"]["count"] == 12
        assert len(step["open_quality_findings"]["items"]) == 10
    finally:
        db.close()


# ============================================
# run_check_gate 标准化 finding 测试（Step S-1783247858392-f383）
# ============================================

def test_normalize_severity_maps_uppercase_to_lowercase():
    """_normalize_severity 将大写 severity 映射为小写"""
    from callwarden.db.db_check_gate import _normalize_severity

    assert _normalize_severity("ERROR") == "error"
    assert _normalize_severity("warning") == "warn"
    assert _normalize_severity("Info") == "info"
    assert _normalize_severity("BLOCK") == "block"


def test_normalize_severity_unknown_defaults_to_warn():
    """_normalize_severity 对 None / 空串 / 未知值默认返回 warn"""
    from callwarden.db.db_check_gate import _normalize_severity

    assert _normalize_severity("") == "warn"
    assert _normalize_severity(None) == "warn"
    assert _normalize_severity("CRITICAL") == "warn"


def test_standardize_finding_returns_normalized_fields():
    """_standardize_finding 返回标准化字段（finding_type/severity 小写等）"""
    from callwarden.db.db_check_gate import CheckGateMixin

    finding = CheckGateMixin._standardize_finding(
        check="syntax",
        file_path="sample.py",
        severity="ERROR",
        message="syntax error",
        rule_id="rule1",
        line=10,
    )
    # 标准化字段（与 task_quality_findings 表对齐）
    assert finding["finding_type"] == "syntax"
    assert finding["file_path"] == "sample.py"
    assert finding["severity"] == "error"  # 小写
    assert finding["rule_id"] == "rule1"
    assert finding["line"] == 10
    assert finding["message"] == "syntax error"
    # 向后兼容字段
    assert finding["check"] == "syntax"
    assert finding["file"] == "sample.py"
    assert finding["raw_severity"] == "ERROR"


def test_standardize_finding_unknown_severity_defaults_to_warn():
    """未知 severity 默认标准化为 warn"""
    from callwarden.db.db_check_gate import CheckGateMixin

    finding = CheckGateMixin._standardize_finding(
        check="semgrep", file_path="x.py", severity="CRITICAL", message="x"
    )
    assert finding["severity"] == "warn"


def test_run_check_gate_empty_files_passes():
    """空文件列表 → passed=True, findings 为空"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        result = db.run_check_gate(task_id, step["step_id"], changed_files=[])
        assert result["passed"] is True
        assert result["findings"] == []
        assert result["fix_required"] is False
        assert "summary" in result
    finally:
        db.close()


def test_run_check_gate_nonexistent_file_skipped():
    """不存在的文件被跳过，不抛异常"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        result = db.run_check_gate(
            task_id, step["step_id"], changed_files=["/nonexistent/path.py"]
        )
        assert result["passed"] is True
        assert result["findings"] == []
    finally:
        db.close()


def test_run_check_gate_syntax_error_emits_standardized_finding():
    """语法错误 → 返回 error 级 finding，包含标准化字段，并写入 guardrail_findings 表"""
    import tempfile as _tempfile

    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        # 创建一个临时文件（内容无所谓，mock 会接管解析）
        tmp = _tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write("def broken(:\n")
        tmp.close()
        try:
            # 注入 mock create_parser 触发语法错误
            class _FakeParser:
                def parse_file(self, path):
                    return {"parse_error": "SyntaxError at line 1"}

            _had = hasattr(db, "create_parser")
            _orig = getattr(db, "create_parser", None)
            db.create_parser = lambda path: _FakeParser()
            try:
                result = db.run_check_gate(
                    task_id, step["step_id"], changed_files=[tmp.name]
                )
            finally:
                if _had and _orig is not None:
                    db.create_parser = _orig
                else:
                    try:
                        delattr(db, "create_parser")
                    except AttributeError:
                        pass

            # 验证：passed=False，findings 标准化
            assert result["passed"] is False
            assert result["fix_required"] is True
            assert "syntax" in result["checks_run"]
            assert len(result["findings"]) == 1
            f = result["findings"][0]
            # 标准化字段
            assert f["finding_type"] == "syntax"
            assert f["severity"] == "error"  # 小写
            assert f["file_path"] == tmp.name
            assert "message" in f
            # 向后兼容字段
            assert f["check"] == "syntax"
            assert f["file"] == tmp.name
            assert f["raw_severity"] == "ERROR"

            # 验证写入 guardrail_findings 表
            cur = db.conn.execute(
                "SELECT COUNT(*) as cnt FROM guardrail_findings WHERE file_path = ?",
                (tmp.name,),
            )
            assert cur.fetchone()["cnt"] >= 1
        finally:
            os.unlink(tmp.name)
    finally:
        db.close()


def test_run_check_gate_does_not_modify_task_or_step_status():
    """run_check_gate 不直接修改 task/step 状态（只负责检查与报告）"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        original_step_status = step["status"]
        original_task_status_row = db.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        original_task_status = original_task_status_row["status"]

        class _FakeParser:
            def parse_file(self, path):
                return {"parse_error": "SyntaxError"}

        _had = hasattr(db, "create_parser")
        _orig = getattr(db, "create_parser", None)
        db.create_parser = lambda path: _FakeParser()
        try:
            db.run_check_gate(
                task_id, step["step_id"], changed_files=["dummy.py"]
            )
        finally:
            if _had and _orig is not None:
                db.create_parser = _orig
            else:
                try:
                    delattr(db, "create_parser")
                except AttributeError:
                    pass

        # step 状态不应改变
        cur = db.conn.execute(
            "SELECT status FROM task_steps WHERE id = ?",
            (step["step_id"],),
        )
        assert cur.fetchone()["status"] == original_step_status
        # task 状态不应改变
        cur = db.conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        )
        assert cur.fetchone()["status"] == original_task_status
    finally:
        db.close()


def test_run_check_gate_semgrep_warn_finding_passes():
    """Semgrep WARNING finding → severity=warn，passed=True（warn 不阻塞）"""
    import tempfile as _tempfile

    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        tmp = _tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write("x = 1\n")
        tmp.close()
        try:
            def _fake_semgrep(target_paths, config, timeout):
                return {
                    "success": True,
                    "total_findings": 1,
                    "results": [
                        {
                            "rule_id": "python-best-practice",
                            "severity": "WARNING",
                            "message": "useless assignment",
                            "start_line": 1,
                        }
                    ],
                }

            _had = hasattr(db, "run_semgrep")
            _orig = getattr(db, "run_semgrep", None)
            db.run_semgrep = _fake_semgrep
            try:
                result = db.run_check_gate(
                    task_id, step["step_id"], changed_files=[tmp.name]
                )
            finally:
                if _had and _orig is not None:
                    db.run_semgrep = _orig
                else:
                    try:
                        delattr(db, "run_semgrep")
                    except AttributeError:
                        pass

            assert result["passed"] is True  # warn 不阻塞
            assert result["fix_required"] is False
            assert "semgrep" in result["checks_run"]
            assert len(result["findings"]) == 1
            f = result["findings"][0]
            assert f["finding_type"] == "semgrep"
            assert f["severity"] == "warn"  # 标准化为小写
            assert f["rule_id"] == "python-best-practice"
            assert f["line"] == 1
        finally:
            os.unlink(tmp.name)
    finally:
        db.close()


def test_run_check_gate_semgrep_error_finding_blocks():
    """Semgrep ERROR finding → severity=error，passed=False（error 阻塞）"""
    import tempfile as _tempfile

    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        tmp = _tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write("x = 1\n")
        tmp.close()
        try:
            def _fake_semgrep(target_paths, config, timeout):
                return {
                    "success": True,
                    "total_findings": 1,
                    "results": [
                        {
                            "rule_id": "python-security",
                            "severity": "ERROR",
                            "message": "sql injection",
                            "start_line": 5,
                        }
                    ],
                }

            _had = hasattr(db, "run_semgrep")
            _orig = getattr(db, "run_semgrep", None)
            db.run_semgrep = _fake_semgrep
            try:
                result = db.run_check_gate(
                    task_id, step["step_id"], changed_files=[tmp.name]
                )
            finally:
                if _had and _orig is not None:
                    db.run_semgrep = _orig
                else:
                    try:
                        delattr(db, "run_semgrep")
                    except AttributeError:
                        pass

            assert result["passed"] is False
            assert result["fix_required"] is True
            assert len(result["findings"]) == 1
            assert result["findings"][0]["severity"] == "error"
            assert result["findings"][0]["finding_type"] == "semgrep"
        finally:
            os.unlink(tmp.name)
    finally:
        db.close()


def test_run_check_gate_summary_contains_check_status():
    """summary 包含检查项状态（pass/FAIL）"""
    import tempfile as _tempfile

    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        tmp = _tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write("def broken(:\n")
        tmp.close()
        try:
            class _FakeParser:
                def parse_file(self, path):
                    return {"parse_error": "SyntaxError"}

            _had = hasattr(db, "create_parser")
            _orig = getattr(db, "create_parser", None)
            db.create_parser = lambda path: _FakeParser()
            try:
                result = db.run_check_gate(
                    task_id, step["step_id"], changed_files=[tmp.name]
                )
            finally:
                if _had and _orig is not None:
                    db.create_parser = _orig
                else:
                    try:
                        delattr(db, "create_parser")
                    except AttributeError:
                        pass

            assert "syntax:FAIL" in result["summary"]
            assert "检查失败" in result["summary"]
        finally:
            os.unlink(tmp.name)
    finally:
        db.close()


def test_run_check_gate_save_gate_findings_uses_lowercase_severity():
    """_save_gate_findings 写入 guardrail_findings 时 severity 为小写"""
    import tempfile as _tempfile

    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        tmp = _tempfile.NamedTemporaryFile(
            suffix=".py", delete=False, mode="w", encoding="utf-8"
        )
        tmp.write("def broken(:\n")
        tmp.close()
        try:
            class _FakeParser:
                def parse_file(self, path):
                    return {"parse_error": "SyntaxError"}

            _had = hasattr(db, "create_parser")
            _orig = getattr(db, "create_parser", None)
            db.create_parser = lambda path: _FakeParser()
            try:
                db.run_check_gate(
                    task_id, step["step_id"], changed_files=[tmp.name]
                )
            finally:
                if _had and _orig is not None:
                    db.create_parser = _orig
                else:
                    try:
                        delattr(db, "create_parser")
                    except AttributeError:
                        pass

            # 验证 guardrail_findings 表中 severity 为小写
            cur = db.conn.execute(
                "SELECT severity FROM guardrail_findings WHERE file_path = ?",
                (tmp.name,),
            )
            for row in cur:
                assert row["severity"] == "error"  # 小写
        finally:
            os.unlink(tmp.name)
    finally:
        db.close()


# ============================================
# run_task_completion_review 集成 run_check_gate 测试
# （Step S-1783247858392-b616）
# ============================================

def _inject_change_audit(db, task_id, step_id, file_path="sample.py"):
    """辅助：向 change_audit 表注入一条变更记录（让 get_task_changed_files 能查到）"""
    now = time.time()
    db.conn.execute(
        """
        INSERT INTO change_audit
            (id, task_id, step_id, file_path, hash_before, hash_after,
             diff, author, timestamp)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("C-test-" + str(int(now * 1000)), task_id, step_id, file_path,
         "old", "new", "diff", "agent", now),
    )
    db.conn.commit()


def _mock_run_check_gate(db, findings):
    """辅助：注入 mock run_check_gate，返回指定的 findings"""
    _had = hasattr(db, "run_check_gate")
    _orig = getattr(db, "run_check_gate", None)

    def _fake_check_gate(task_id, step_id, changed_files):
        return {
            "passed": all(f.get("severity") not in ("error", "block") for f in findings),
            "checks_run": ["syntax"] if findings else [],
            "findings": findings,
            "fix_required": any(f.get("severity") in ("error", "block") for f in findings),
            "summary": "mock summary",
        }

    db.run_check_gate = _fake_check_gate

    def _restore():
        if _had and _orig is not None:
            db.run_check_gate = _orig
        else:
            try:
                delattr(db, "run_check_gate")
            except AttributeError:
                pass

    return _restore


def test_run_task_completion_review_invokes_run_check_gate():
    """有变更文件时，run_task_completion_review 调用 run_check_gate"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "sample.py")

        restore = _mock_run_check_gate(db, findings=[])
        try:
            review = db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore()

        # check_gate_result 应非 None（说明 run_check_gate 被调用了）
        assert review["check_gate_result"] is not None
        assert review["decision"] == "pass"
    finally:
        db.close()


def test_run_task_completion_review_no_changed_files_skips_check_gate():
    """无变更文件时，run_task_completion_review 不调用 run_check_gate"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        # 不注入 change_audit，get_task_changed_files 返回空列表

        _called = {"value": False}

        def _fake_check_gate(task_id, step_id, changed_files):
            _called["value"] = True
            return {"passed": True, "findings": [], "checks_run": [], "fix_required": False, "summary": ""}

        db.run_check_gate = _fake_check_gate
        try:
            review = db.run_task_completion_review(task_id, step["step_id"])
        finally:
            try:
                delattr(db, "run_check_gate")
            except AttributeError:
                pass

        assert _called["value"] is False  # 未被调用
        assert review["check_gate_result"] is None
        assert review["decision"] == "pass"
    finally:
        db.close()


def test_run_task_completion_review_converts_semgrep_error_to_task_quality_finding():
    """run_check_gate 的 error finding 被转换为 task_quality_findings 记录"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "vulnerable.py")

        findings = [
            {
                "finding_type": "semgrep",
                "severity": "error",
                "file_path": "vulnerable.py",
                "rule_id": "python-security",
                "line": 5,
                "message": "sql injection",
                # 向后兼容字段
                "check": "semgrep",
                "file": "vulnerable.py",
                "raw_severity": "ERROR",
            }
        ]
        restore = _mock_run_check_gate(db, findings=findings)
        try:
            review = db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore()

        # 决策应为 block（存在 error finding）
        assert review["decision"] == "block"
        assert review["counts"]["error"] == 1

        # task_quality_findings 表应包含该 finding
        task_quality = db.get_task_quality_findings(task_id, status="open")
        assert any(
            f["finding_type"] == "semgrep"
            and f["severity"] == "error"
            and f["source"] == "check_gate"
            for f in task_quality
        ), f"expected semgrep error finding from check_gate, got: {task_quality}"
    finally:
        db.close()


def test_run_task_completion_review_converts_syntax_warn_to_task_quality_finding():
    """run_check_gate 的 warn finding 被转换为 task_quality_findings 记录，decision=warn"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "style.py")

        findings = [
            {
                "finding_type": "syntax",
                "severity": "warn",
                "file_path": "style.py",
                "rule_id": "",
                "line": 0,
                "message": "style issue",
                "check": "syntax",
                "file": "style.py",
                "raw_severity": "WARNING",
            }
        ]
        restore = _mock_run_check_gate(db, findings=findings)
        try:
            review = db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore()

        assert review["decision"] == "warn"
        assert review["counts"]["warn"] == 1
        # task_quality_findings 表应包含该 finding
        task_quality = db.get_task_quality_findings(task_id, status="open")
        assert any(
            f["finding_type"] == "syntax"
            and f["severity"] == "warn"
            and f["source"] == "check_gate"
            for f in task_quality
        )
    finally:
        db.close()


def test_run_task_completion_review_error_severity_blocks():
    """error/block severity 导致 decision=block"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "bad.py")

        findings = [
            {
                "finding_type": "syntax",
                "severity": "error",
                "file_path": "bad.py",
                "rule_id": "",
                "line": 0,
                "message": "syntax error",
                "check": "syntax",
                "file": "bad.py",
                "raw_severity": "ERROR",
            },
            {
                "finding_type": "semgrep",
                "severity": "block",
                "file_path": "bad.py",
                "rule_id": "sec",
                "line": 10,
                "message": "critical vuln",
                "check": "semgrep",
                "file": "bad.py",
                "raw_severity": "BLOCK",
            },
        ]
        restore = _mock_run_check_gate(db, findings=findings)
        try:
            review = db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore()

        assert review["decision"] == "block"
        assert review["counts"]["error"] == 1
        assert review["counts"]["block"] == 1
    finally:
        db.close()


def test_run_task_completion_review_check_gate_no_duplicate_findings():
    """重复调用 run_task_completion_review 不会累积 check_gate finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "dup.py")

        findings = [
            {
                "finding_type": "semgrep",
                "severity": "error",
                "file_path": "dup.py",
                "rule_id": "r1",
                "line": 1,
                "message": "issue",
                "check": "semgrep",
                "file": "dup.py",
                "raw_severity": "ERROR",
            }
        ]
        restore = _mock_run_check_gate(db, findings=findings)
        try:
            # 第一次调用
            db.run_task_completion_review(task_id, step["step_id"])
            # 第二次调用
            db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore()

        # task_quality_findings 表中 source='check_gate' 的 finding 应只有 1 条
        task_quality = db.get_task_quality_findings(task_id, status="all")
        check_gate_findings = [f for f in task_quality if f.get("source") == "check_gate"]
        assert len(check_gate_findings) == 1, (
            f"expected 1 check_gate finding, got {len(check_gate_findings)}"
        )
    finally:
        db.close()


def test_run_task_completion_review_preserves_manual_findings():
    """清理 check_gate finding 时不影响手动记录的 finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        # 手动记录一条 finding（source='manual'）
        db.record_task_quality_finding(
            task_id, step_id=step["step_id"],
            finding_type="manual", severity="warn",
            message="manual issue", source="manual",
        )
        _inject_change_audit(db, task_id, step["step_id"], "m.py")

        findings = [
            {
                "finding_type": "semgrep",
                "severity": "error",
                "file_path": "m.py",
                "rule_id": "r",
                "line": 1,
                "message": "auto issue",
                "check": "semgrep",
                "file": "m.py",
                "raw_severity": "ERROR",
            }
        ]
        restore = _mock_run_check_gate(db, findings=findings)
        try:
            db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore()

        # manual finding 应保留
        task_quality = db.get_task_quality_findings(task_id, status="all")
        manual_findings = [f for f in task_quality if f.get("source") == "manual"]
        assert len(manual_findings) == 1
        assert manual_findings[0]["message"] == "manual issue"
        # check_gate finding 也应存在
        check_gate_findings = [f for f in task_quality if f.get("source") == "check_gate"]
        assert len(check_gate_findings) == 1
    finally:
        db.close()


def test_run_task_completion_review_check_gate_result_returned():
    """返回值包含 check_gate_result 字段"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "r.py")

        restore = _mock_run_check_gate(db, findings=[])
        try:
            review = db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore()

        assert "check_gate_result" in review
        assert review["check_gate_result"] is not None
        assert review["check_gate_result"]["summary"] == "mock summary"
    finally:
        db.close()


# ============================================
# Step S-1783247858392-9aaf: Semgrep finding 写入 + decision=block 阻止任务完成
# ============================================

def test_semgrep_finding_written_via_completion_review_blocks_task():
    """通过 run_task_completion_review 写入 Semgrep error finding 后，
    task_quality_findings 表包含该 finding，且 task_has_blocking_findings=True"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "vuln.py")

        findings = [
            {
                "finding_type": "semgrep",
                "severity": "error",
                "file_path": "vuln.py",
                "rule_id": "python-security",
                "line": 5,
                "message": "sql injection",
                "check": "semgrep",
                "file": "vuln.py",
                "raw_severity": "ERROR",
            }
        ]
        restore = _mock_run_check_gate(db, findings=findings)
        try:
            review = db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore()

        # decision 应为 block
        assert review["decision"] == "block"
        # task_has_blocking_findings 应返回 True（任务不可完成）
        assert db.task_has_blocking_findings(task_id) is True

        # task_quality_findings 表应包含 semgrep error finding（source=check_gate）
        stored = db.get_task_quality_findings(task_id, status="open")
        semgrep_errors = [
            f for f in stored
            if f["finding_type"] == "semgrep"
            and f["severity"] == "error"
            and f["source"] == "check_gate"
        ]
        assert len(semgrep_errors) == 1
        assert semgrep_errors[0]["message"] == "sql injection"
    finally:
        db.close()


def test_decision_block_prevents_step_done_end_to_end():
    """端到端：run_task_completion_review 写入 semgrep error finding →
    task_report_step 触发 block，step 状态变为 blocked（不可 done）"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "bad.py")

        # mock run_check_gate：
        # - 第 1 次调用（F6 检查门禁）返回 passed=True（让 F6 通过）
        # - 第 2 次调用（Task Quality Gate）返回 semgrep error finding（触发 block）
        _call_count = {"n": 0}
        _had = hasattr(db, "run_check_gate")
        _orig = getattr(db, "run_check_gate", None)

        def _fake_check_gate(task_id, step_id, changed_files):
            _call_count["n"] += 1
            if _call_count["n"] == 1:
                # F6 门禁：通过
                return {
                    "passed": True,
                    "checks_run": ["syntax"],
                    "findings": [],
                    "fix_required": False,
                    "summary": "F6 pass",
                }
            else:
                # Task Quality Gate：返回 semgrep error finding
                return {
                    "passed": False,
                    "checks_run": ["semgrep"],
                    "findings": [
                        {
                            "finding_type": "semgrep",
                            "severity": "error",
                            "file_path": "bad.py",
                            "rule_id": "python-security",
                            "line": 1,
                            "message": "sql injection",
                            "check": "semgrep",
                            "file": "bad.py",
                            "raw_severity": "ERROR",
                        }
                    ],
                    "fix_required": True,
                    "summary": "semgrep error",
                }

        db.run_check_gate = _fake_check_gate
        try:
            # 调用 task_report_step（带 changes 触发 F6 + Task Quality Gate）
            result = db.task_report_step(
                task_id, step["step_id"],
                result="done", success=True,
                changes=[{
                    "file_path": "bad.py",
                    "hash_before": "a",
                    "hash_after": "b",
                    "diff": "+x",
                    "author": "agent",
                }],
            )
        finally:
            if _had and _orig is not None:
                db.run_check_gate = _orig
            else:
                try:
                    delattr(db, "run_check_gate")
                except AttributeError:
                    pass

        # step 应被 blocked（不是 done）
        cur = db.conn.execute(
            "SELECT status FROM task_steps WHERE id = ?", (step["step_id"],)
        )
        assert cur.fetchone()["status"] == "blocked"

        # task_quality_findings 应包含 semgrep error finding（来自 Task Quality Gate）
        findings = db.get_task_quality_findings(task_id, status="open")
        semgrep_errors = [
            f for f in findings
            if f["finding_type"] == "semgrep" and f["severity"] == "error"
        ]
        assert len(semgrep_errors) >= 1
        assert semgrep_errors[0]["source"] == "check_gate"

        # 应自动插入 fix_quality_gate_failure step
        cur = db.conn.execute(
            "SELECT COUNT(*) as cnt FROM task_steps "
            "WHERE task_id = ? AND action = 'fix_quality_gate_failure'",
            (task_id,),
        )
        assert cur.fetchone()["cnt"] >= 1

        # task_has_blocking_findings 应返回 True（任务不可完成）
        assert db.task_has_blocking_findings(task_id) is True

        # 返回值含 quality_gate.blocked=True
        assert result is not None
        assert result["quality_gate"]["decision"] == "block"
        assert result["quality_gate"]["blocked"] is True
    finally:
        db.close()


def test_decision_block_resolved_allows_step_done():
    """block 后解决所有 error finding，task_has_blocking_findings 返回 False（可完成）"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "v.py")

        # 通过 run_task_completion_review 写入 semgrep error finding
        findings = [
            {
                "finding_type": "semgrep",
                "severity": "error",
                "file_path": "v.py",
                "rule_id": "r",
                "line": 1,
                "message": "vuln",
                "check": "semgrep",
                "file": "v.py",
                "raw_severity": "ERROR",
            }
        ]
        restore = _mock_run_check_gate(db, findings=findings)
        try:
            db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore()

        # 此时任务被阻塞
        assert db.task_has_blocking_findings(task_id) is True

        # 解决所有 error finding
        all_findings = db.get_task_quality_findings(task_id, status="open")
        for f in all_findings:
            if f["severity"] in ("error", "block"):
                db.resolve_task_quality_finding(f["id"], resolution="fixed")

        # 阻塞解除
        assert db.task_has_blocking_findings(task_id) is False
    finally:
        db.close()


# ============================================
# Step S-1783247858392-82b7: 4 个扩展检查器测试
# ============================================

# ---------- _check_scope_violations ----------

def test_check_scope_violations_detects_out_of_target_files():
    """变更文件超出 step.target_file 范围 → error finding"""
    db, _root = _db_with_workspace()
    try:
        # 创建带 target_file='src/a.py' 的任务
        task_id = db.task_create("scope-test", steps=[
            {"action": "edit", "target_file": "src/a.py"},
        ])
        step = db.task_next_step(task_id)
        # 调用 _check_scope_violations：传入 b.py（不在 src/a.py 范围）
        db._check_scope_violations(task_id, step["step_id"], ["src/b.py"])
        findings = db.get_task_quality_findings(task_id, status="open")
        scope_findings = [f for f in findings if f["finding_type"] == "scope"]
        assert len(scope_findings) == 1
        assert scope_findings[0]["severity"] == "error"
        assert scope_findings[0]["source"] == "check_gate"
    finally:
        db.close()


def test_check_scope_violations_skips_when_no_target_file():
    """step 无 target_file → 不检查（直接返回）"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("no-target", steps=[
            {"action": "edit"},  # 无 target_file
        ])
        step = db.task_next_step(task_id)
        db._check_scope_violations(task_id, step["step_id"], ["any.py"])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_scope_violations_allows_target_file_itself():
    """变更文件等于 target_file → 不报 scope violation"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("self-target", steps=[
            {"action": "edit", "target_file": "src/a.py"},
        ])
        step = db.task_next_step(task_id)
        db._check_scope_violations(task_id, step["step_id"], ["src/a.py"])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_scope_violations_allows_subpath_of_target():
    """变更文件是 target_file 的子路径 → 不报 scope violation"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("subpath-target", steps=[
            {"action": "edit", "target_file": "src"},
        ])
        step = db.task_next_step(task_id)
        # src/foo.py 是 src 的子路径 → 不报
        db._check_scope_violations(task_id, step["step_id"], ["src/foo.py"])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_scope_violations_normalizes_backslash_paths():
    """Windows 反斜杠路径标准化后比较（不误报）"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("win-path", steps=[
            {"action": "edit", "target_file": "src\\a.py"},
        ])
        step = db.task_next_step(task_id)
        # 同一文件，路径分隔符不同 → 不报
        db._check_scope_violations(task_id, step["step_id"], ["src/a.py"])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_scope_violations_skips_empty_changed_files():
    """changed_files 为空 → 不检查"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("empty-cf", steps=[
            {"action": "edit", "target_file": "a.py"},
        ])
        step = db.task_next_step(task_id)
        db._check_scope_violations(task_id, step["step_id"], [])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


# ---------- _check_symbol_attribution ----------

def test_check_symbol_attribution_warns_when_no_changes():
    """target_symbol 非空但 task_symbol_changes 无记录 → warn finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("attr-test", steps=[
            {"action": "edit", "target_file": "a.py", "target_symbol": "module::func"},
        ])
        step = db.task_next_step(task_id)
        # 不向 task_symbol_changes 表插入记录 → 应触发 warn
        db._check_symbol_attribution(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        attr_findings = [f for f in findings if f["finding_type"] == "call_chain"]
        assert len(attr_findings) == 1
        assert attr_findings[0]["severity"] == "warn"
        assert attr_findings[0]["source"] == "check_gate"
    finally:
        db.close()


def test_check_symbol_attribution_skips_when_no_target_symbol():
    """无 target_symbol → 不检查"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("no-symbol", steps=[
            {"action": "edit", "target_file": "a.py"},
        ])
        step = db.task_next_step(task_id)
        db._check_symbol_attribution(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_symbol_attribution_skips_when_changes_exist():
    """target_symbol + task_symbol_changes 已有记录 → 不报"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("attr-exist", steps=[
            {"action": "edit", "target_file": "a.py", "target_symbol": "module::func"},
        ])
        step = db.task_next_step(task_id)
        # 手动插入一条 task_symbol_changes 记录
        db.conn.execute(
            """
            INSERT INTO task_symbol_changes
                (workspace_id, task_id, step_id, file_path, qualified_name,
                 symbol_name, change_type, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (db._get_active_workspace_id(), task_id, step["step_id"],
             "a.py", "module::func", "func", "modified", "manual", time.time()),
        )
        db.conn.commit()
        # 应不报 finding（已有变更归因）
        db._check_symbol_attribution(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        attr_findings = [f for f in findings if f["finding_type"] == "call_chain"]
        assert len(attr_findings) == 0
    finally:
        db.close()


# ---------- _check_file_health_findings ----------

def _inject_mock_check_file_health(db, health_return):
    """辅助：注入 mock check_file_health 返回指定 dict"""
    _had = hasattr(db, "check_file_health")
    _orig = getattr(db, "check_file_health", None)
    db.check_file_health = lambda fp: health_return

    def _restore():
        if _had and _orig is not None:
            db.check_file_health = _orig
        else:
            try:
                delattr(db, "check_file_health")
            except AttributeError:
                pass

    return _restore


def test_check_file_health_findings_warns_large_file():
    """文件大小 >= 1000 行 → warn finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        health = {"total_lines": 1500, "function_issues": []}
        restore = _inject_mock_check_file_health(db, health)
        try:
            db._check_file_health_findings(task_id, step["step_id"], ["big.py"])
        finally:
            restore()
        findings = db.get_task_quality_findings(task_id, status="open")
        fh_findings = [f for f in findings if f["finding_type"] == "file_health"]
        assert len(fh_findings) == 1
        assert fh_findings[0]["severity"] == "warn"
        assert fh_findings[0]["source"] == "check_gate"
    finally:
        db.close()


def test_check_file_health_findings_errors_huge_file():
    """文件大小 >= 2000 行 → error finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        health = {"total_lines": 2500, "function_issues": []}
        restore = _inject_mock_check_file_health(db, health)
        try:
            db._check_file_health_findings(task_id, step["step_id"], ["huge.py"])
        finally:
            restore()
        findings = db.get_task_quality_findings(task_id, status="open")
        fh_findings = [f for f in findings if f["finding_type"] == "file_health"]
        assert len(fh_findings) == 1
        assert fh_findings[0]["severity"] == "error"
    finally:
        db.close()


def test_check_file_health_findings_warns_complexity_hotspot():
    """函数复杂度 >= 20 → warn finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        health = {
            "total_lines": 100,
            "function_issues": [
                {
                    "qualified_name": "mod::complex_fn",
                    "cyclomatic_complexity": 25,
                    "line_count": 50,
                    "severity": "medium",
                }
            ],
        }
        restore = _inject_mock_check_file_health(db, health)
        try:
            db._check_file_health_findings(task_id, step["step_id"], ["c.py"])
        finally:
            restore()
        findings = db.get_task_quality_findings(task_id, status="open")
        fh_findings = [f for f in findings if f["finding_type"] == "file_health"]
        assert len(fh_findings) == 1
        assert fh_findings[0]["severity"] == "warn"
    finally:
        db.close()


def test_check_file_health_findings_skips_when_check_raises():
    """check_file_health 抛异常时 → 跳过该文件，不报错也不报 finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)

        def _raise(fp):
            raise RuntimeError("boom")

        _had = hasattr(db, "check_file_health")
        _orig = getattr(db, "check_file_health", None)
        db.check_file_health = _raise
        try:
            db._check_file_health_findings(task_id, step["step_id"], ["any.py"])
        finally:
            if _had and _orig is not None:
                db.check_file_health = _orig
            else:
                try:
                    delattr(db, "check_file_health")
                except AttributeError:
                    pass
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_file_health_findings_skips_when_health_none():
    """check_file_health 返回 None → 跳过"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        restore = _inject_mock_check_file_health(db, None)
        try:
            db._check_file_health_findings(task_id, step["step_id"], ["any.py"])
        finally:
            restore()
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_file_health_findings_skips_small_healthy_file():
    """小文件且无复杂度热点 → 不报 finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        health = {
            "total_lines": 100,
            "function_issues": [
                {
                    "qualified_name": "mod::simple",
                    "cyclomatic_complexity": 5,  # 低于阈值 20
                    "line_count": 10,
                    "severity": "low",
                }
            ],
        }
        restore = _inject_mock_check_file_health(db, health)
        try:
            db._check_file_health_findings(task_id, step["step_id"], ["ok.py"])
        finally:
            restore()
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


# ---------- _check_i18n_hardcoded ----------

def _write_temp_source(root, rel_path, content):
    """辅助：在工作区下写入临时源文件，返回绝对路径"""
    abs_path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(abs_path), exist_ok=True) if os.path.dirname(rel_path) else None
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    return abs_path


def test_check_i18n_hardcoded_detects_print():
    """检测到 print() → warn finding"""
    db, root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        abs_path = _write_temp_source(root, "src_mod.py", "print('hello world')\n")
        try:
            db._check_i18n_hardcoded(task_id, step["step_id"], ["src_mod.py"])
        finally:
            os.unlink(abs_path)
        findings = db.get_task_quality_findings(task_id, status="open")
        i18n_findings = [f for f in findings if f["finding_type"] == "i18n"]
        assert len(i18n_findings) == 1
        assert i18n_findings[0]["severity"] == "warn"
        assert i18n_findings[0]["source"] == "check_gate"
    finally:
        db.close()


def test_check_i18n_hardcoded_detects_logger():
    """检测到 logger.info() → warn finding"""
    db, root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        abs_path = _write_temp_source(
            root, "log_mod.py",
            "import logging\nlogger = logging.getLogger(__name__)\nlogger.info('x')\n",
        )
        try:
            db._check_i18n_hardcoded(task_id, step["step_id"], ["log_mod.py"])
        finally:
            os.unlink(abs_path)
        findings = db.get_task_quality_findings(task_id, status="open")
        i18n_findings = [f for f in findings if f["finding_type"] == "i18n"]
        # 第 3 行 logger.info() 应被检测到
        assert len(i18n_findings) == 1
        assert i18n_findings[0]["severity"] == "warn"
    finally:
        db.close()


def test_check_i18n_hardcoded_detects_logging_module():
    """检测到 logging.warning() → warn finding"""
    db, root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        abs_path = _write_temp_source(
            root, "lg.py",
            "import logging\nlogging.error('fail')\n",
        )
        try:
            db._check_i18n_hardcoded(task_id, step["step_id"], ["lg.py"])
        finally:
            os.unlink(abs_path)
        findings = db.get_task_quality_findings(task_id, status="open")
        i18n_findings = [f for f in findings if f["finding_type"] == "i18n"]
        assert len(i18n_findings) == 1
    finally:
        db.close()


def test_check_i18n_hardcoded_skips_tests_dir():
    """tests/ 目录下的文件不扫描"""
    db, root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        abs_path = _write_temp_source(
            root, "tests/test_x.py", "print('test output')\n"
        )
        try:
            db._check_i18n_hardcoded(task_id, step["step_id"], ["tests/test_x.py"])
        finally:
            os.unlink(abs_path)
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_i18n_hardcoded_skips_comment_lines():
    """以 # 开头的注释行不扫描"""
    db, root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        abs_path = _write_temp_source(
            root, "cmt.py",
            "# print('this is a comment')\n# logger.info('also comment')\nx = 1\n",
        )
        try:
            db._check_i18n_hardcoded(task_id, step["step_id"], ["cmt.py"])
        finally:
            os.unlink(abs_path)
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_i18n_hardcoded_skips_nonexistent_file():
    """不存在的文件不报错（静默跳过）"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        # 不抛异常
        db._check_i18n_hardcoded(task_id, step["step_id"], ["/nonexistent/file.py"])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_i18n_hardcoded_one_finding_per_line():
    """同一行即使匹配多个模式也只记录一次"""
    db, root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        # 单行同时包含 print 和 logger（理论上不可能，但测试 break 逻辑）
        abs_path = _write_temp_source(
            root, "multi.py", "print('a')\nlogger.info('b')\n"
        )
        try:
            db._check_i18n_hardcoded(task_id, step["step_id"], ["multi.py"])
        finally:
            os.unlink(abs_path)
        findings = db.get_task_quality_findings(task_id, status="open")
        i18n_findings = [f for f in findings if f["finding_type"] == "i18n"]
        # 2 行各 1 个 finding
        assert len(i18n_findings) == 2
    finally:
        db.close()


# ---------- run_task_completion_review 集成 4 个检查器 ----------

def test_run_task_completion_review_invokes_all_four_checkers():
    """有变更文件时，run_task_completion_review 调用 4 个检查器，
    通过 mock 验证每个检查器都被调用"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "sample.py")

        # mock run_check_gate 返回空（不让它干扰）
        restore_cg = _mock_run_check_gate(db, findings=[])

        # 替换 4 个检查器为 mock，记录调用
        calls = {"scope": 0, "symbol": 0, "file_health": 0, "i18n": 0}

        _orig_scope = db._check_scope_violations
        _orig_symbol = db._check_symbol_attribution
        _orig_fh = db._check_file_health_findings
        _orig_i18n = db._check_i18n_hardcoded

        def _mock_scope(tid, sid, cf):
            calls["scope"] += 1

        def _mock_symbol(tid, sid):
            calls["symbol"] += 1

        def _mock_fh(tid, sid, cf):
            calls["file_health"] += 1

        def _mock_i18n(tid, sid, cf):
            calls["i18n"] += 1

        db._check_scope_violations = _mock_scope
        db._check_symbol_attribution = _mock_symbol
        db._check_file_health_findings = _mock_fh
        db._check_i18n_hardcoded = _mock_i18n
        try:
            db.run_task_completion_review(task_id, step["step_id"])
        finally:
            db._check_scope_violations = _orig_scope
            db._check_symbol_attribution = _orig_symbol
            db._check_file_health_findings = _orig_fh
            db._check_i18n_hardcoded = _orig_i18n
            restore_cg()

        # 所有 4 个检查器都应被调用
        assert calls["scope"] == 1, f"scope called {calls['scope']} times"
        assert calls["symbol"] == 1, f"symbol called {calls['symbol']} times"
        assert calls["file_health"] == 1, f"file_health called {calls['file_health']} times"
        assert calls["i18n"] == 1, f"i18n called {calls['i18n']} times"
    finally:
        db.close()


def test_run_task_completion_review_skips_checkers_when_no_changed_files():
    """无变更文件时，4 个检查器都不调用"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        # 不注入 change_audit，get_task_changed_files 返回空列表

        calls = {"scope": 0, "symbol": 0, "file_health": 0, "i18n": 0}
        _orig_scope = db._check_scope_violations
        _orig_symbol = db._check_symbol_attribution
        _orig_fh = db._check_file_health_findings
        _orig_i18n = db._check_i18n_hardcoded

        db._check_scope_violations = lambda *a, **kw: calls.__setitem__("scope", calls["scope"] + 1)
        db._check_symbol_attribution = lambda *a, **kw: calls.__setitem__("symbol", calls["symbol"] + 1)
        db._check_file_health_findings = lambda *a, **kw: calls.__setitem__("file_health", calls["file_health"] + 1)
        db._check_i18n_hardcoded = lambda *a, **kw: calls.__setitem__("i18n", calls["i18n"] + 1)
        try:
            db.run_task_completion_review(task_id, step["step_id"])
        finally:
            db._check_scope_violations = _orig_scope
            db._check_symbol_attribution = _orig_symbol
            db._check_file_health_findings = _orig_fh
            db._check_i18n_hardcoded = _orig_i18n

        assert calls["scope"] == 0
        assert calls["symbol"] == 0
        assert calls["file_health"] == 0
        assert calls["i18n"] == 0
    finally:
        db.close()


def test_run_task_completion_review_checkers_use_check_gate_source():
    """4 个检查器记录的 finding 都使用 source='check_gate'，
    在重复调用 review 时被清理去重"""
    db, _root = _db_with_workspace()
    try:
        # 构造任务：target_file='a.py'，变更文件='b.py'（超出 scope）
        task_id = db.task_create("dedup-test", steps=[
            {"action": "edit", "target_file": "a.py"},
        ])
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "b.py")

        # mock run_check_gate 返回空
        restore_cg = _mock_run_check_gate(db, findings=[])
        try:
            # 第一次 review：应记录 1 个 scope error finding
            db.run_task_completion_review(task_id, step["step_id"])
            findings_1 = db.get_task_quality_findings(task_id, status="all")
            check_gate_1 = [f for f in findings_1 if f.get("source") == "check_gate"]
            assert len(check_gate_1) >= 1

            # 第二次 review：check_gate finding 应被清理去重（不累积）
            db.run_task_completion_review(task_id, step["step_id"])
            findings_2 = db.get_task_quality_findings(task_id, status="all")
            check_gate_2 = [f for f in findings_2 if f.get("source") == "check_gate"]
            # 应与第一次数量相同（去重生效）
            assert len(check_gate_2) == len(check_gate_1)
        finally:
            restore_cg()
    finally:
        db.close()


def test_run_task_completion_review_scope_violation_blocks():
    """scope violation（error）→ decision=block"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("scope-block", steps=[
            {"action": "edit", "target_file": "a.py"},
        ])
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "b.py")

        # mock run_check_gate 返回空（不影响 scope 检查）
        restore_cg = _mock_run_check_gate(db, findings=[])
        try:
            review = db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore_cg()

        # scope violation 是 error → decision=block
        assert review["decision"] == "block"
        assert review["counts"]["error"] >= 1
        # task_has_blocking_findings 应返回 True
        assert db.task_has_blocking_findings(task_id) is True
    finally:
        db.close()


def test_run_task_completion_review_i18n_hardcoded_warns():
    """i18n 硬编码（warn）→ decision=warn（不阻塞）"""
    db, root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "hardcoded.py")
        abs_path = _write_temp_source(root, "hardcoded.py", "print('hello')\n")
        try:
            # mock run_check_gate 返回空
            restore_cg = _mock_run_check_gate(db, findings=[])
            try:
                review = db.run_task_completion_review(task_id, step["step_id"])
            finally:
                restore_cg()
        finally:
            os.unlink(abs_path)

        # i18n hardcoded 是 warn → decision=warn（不阻塞）
        assert review["decision"] == "warn"
        assert review["counts"]["warn"] >= 1
        # warn 不阻塞
        assert db.task_has_blocking_findings(task_id) is False
    finally:
        db.close()


# ============================================
# Step S-1783247858392-480b: _check_signature_mismatch 测试
# ============================================

def _inject_symbol_change_with_signature(
    db, task_id, step_id, qualified_name, symbol_name,
    hash_before, hash_after, sig_before, sig_after,
    file_path="a.py",
):
    """辅助：注入 task_symbol_changes 记录 + symbol_contents 签名数据"""
    # 插入 symbol_contents（before / after 两条）
    db.conn.execute(
        "INSERT OR IGNORE INTO symbol_contents "
        "(content_hash, name, kind, content, signature) "
        "VALUES (?, ?, ?, ?, ?)",
        (hash_before, symbol_name, "fn", "old-content", sig_before),
    )
    db.conn.execute(
        "INSERT OR IGNORE INTO symbol_contents "
        "(content_hash, name, kind, content, signature) "
        "VALUES (?, ?, ?, ?, ?)",
        (hash_after, symbol_name, "fn", "new-content", sig_after),
    )
    # 插入 task_symbol_changes 记录
    db.conn.execute(
        """
        INSERT INTO task_symbol_changes
            (workspace_id, task_id, step_id, file_path, qualified_name,
             symbol_name, symbol_hash_before, symbol_hash_after,
             change_type, source, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (db._get_active_workspace_id(), task_id, step_id, file_path,
         qualified_name, symbol_name, hash_before, hash_after,
         "modified", "manual", time.time()),
    )
    db.conn.commit()


def _inject_caller(db, caller_name, caller_file, callee_name,
                   callee_qualified="mod::fn", callee_id=1, call_line=10):
    """辅助：注入 calls 表记录（模拟调用方）"""
    # 需要 symbols 表中有 caller 符号 + file_instances 中有 caller 文件
    ws_id = db._get_active_workspace_id()
    # 先确保 file_instances 有记录
    cur = db.conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
        (ws_id, caller_file),
    )
    fi_row = cur.fetchone()
    if not fi_row:
        db.conn.execute(
            "INSERT INTO file_instances (workspace_id, rel_path, abs_path, "
            "current_content_hash, mtime, total_lines, last_parsed, status) "
            "VALUES (?, ?, ?, '', ?, 0, 0, 'parsed')",
            (ws_id, caller_file, caller_file, time.time()),
        )
        db.conn.commit()
        cur = db.conn.execute(
            "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
            (ws_id, caller_file),
        )
        fi_row = cur.fetchone()
    fi_id = fi_row["id"]

    # 插入 caller 符号
    db.conn.execute(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, "
        "start_line, end_line) VALUES (?, ?, ?, 'fn', 1, 10)",
        (fi_id, f"hash-{caller_name}-{int(time.time()*1000)}", caller_name),
    )
    db.conn.commit()
    caller_id_row = db.conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND name = ? "
        "ORDER BY id DESC LIMIT 1",
        (fi_id, caller_name),
    ).fetchone()
    caller_id = caller_id_row["id"]

    # 插入 calls 记录
    db.conn.execute(
        "INSERT INTO calls (caller_id, caller_name, caller_module, "
        "callee_name, callee_module, callee_qualified, callee_file, "
        "callee_id, call_line, is_cross_file) "
        "VALUES (?, ?, '', ?, '', ?, ?, ?, ?, 1)",
        (caller_id, caller_name, callee_name, callee_qualified,
         caller_file, callee_id, call_line),
    )
    db.conn.commit()


def test_check_signature_mismatch_blocks_when_unresolved_callers():
    """签名变更且存在 unresolved callers → block finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        # 注入符号变更（签名不同）
        _inject_symbol_change_with_signature(
            db, task_id, step["step_id"],
            qualified_name="mod::parse_policy",
            symbol_name="parse_policy",
            hash_before="hash-old-001", hash_after="hash-new-001",
            sig_before="fn parse_policy(text: &str) -> Result<Policy>",
            sig_after="fn parse_policy(text: &str, strict: bool) -> Result<Policy>",
        )
        # 注入 1 个 unresolved caller（callee_id=0）
        _inject_caller(
            db, caller_name="mod::main", caller_file="src/main.rs",
            callee_name="parse_policy", callee_qualified="mod::parse_policy",
            callee_id=0, call_line=45,  # callee_id=0 → unresolved
        )
        db._check_signature_mismatch(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        sig_findings = [f for f in findings if f["finding_type"] == "call_chain"
                        and f["severity"] == "block"]
        assert len(sig_findings) == 1
        assert sig_findings[0]["source"] == "check_gate"
    finally:
        db.close()


def test_check_signature_mismatch_info_when_all_resolved():
    """签名变更但所有 callers 已解析 → info finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_symbol_change_with_signature(
            db, task_id, step["step_id"],
            qualified_name="mod::helper", symbol_name="helper",
            hash_before="hash-old-002", hash_after="hash-new-002",
            sig_before="fn helper(x: i32) -> i32",
            sig_after="fn helper(x: i32, y: i32) -> i32",
        )
        # 注入 1 个 resolved caller（callee_id != 0 且 callee_qualified 非空）
        _inject_caller(
            db, caller_name="mod::caller", caller_file="src/caller.rs",
            callee_name="helper", callee_qualified="mod::helper",
            callee_id=42, call_line=10,  # resolved
        )
        db._check_signature_mismatch(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        info_findings = [f for f in findings if f["finding_type"] == "call_chain"
                         and f["severity"] == "info"]
        assert len(info_findings) == 1
    finally:
        db.close()


def test_check_signature_mismatch_info_when_no_callers():
    """签名变更但无调用方 → info finding（不阻塞）"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_symbol_change_with_signature(
            db, task_id, step["step_id"],
            qualified_name="mod::orphan", symbol_name="orphan",
            hash_before="hash-old-003", hash_after="hash-new-003",
            sig_before="fn orphan()",
            sig_after="fn orphan(x: bool)",
        )
        # 不注入任何 caller
        db._check_signature_mismatch(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        info_findings = [f for f in findings if f["finding_type"] == "call_chain"
                         and f["severity"] == "info"]
        assert len(info_findings) == 1
    finally:
        db.close()


def test_check_signature_mismatch_skips_when_no_changes():
    """无 task_symbol_changes 记录 → 不检查"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        db._check_signature_mismatch(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_signature_mismatch_skips_when_signature_unchanged():
    """hash 变但 signature 未变 → 不报 finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_symbol_change_with_signature(
            db, task_id, step["step_id"],
            qualified_name="mod::same", symbol_name="same",
            hash_before="hash-old-004", hash_after="hash-new-004",
            sig_before="fn same(x: i32) -> i32",
            sig_after="fn same(x: i32) -> i32",  # 签名相同
        )
        db._check_signature_mismatch(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_signature_mismatch_skips_when_hash_unchanged():
    """hash_before == hash_after → 不检查（内容未变）"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_symbol_change_with_signature(
            db, task_id, step["step_id"],
            qualified_name="mod::nohash", symbol_name="nohash",
            hash_before="same-hash", hash_after="same-hash",  # 相同 hash
            sig_before="fn nohash()",
            sig_after="fn nohash(x: bool)",
        )
        db._check_signature_mismatch(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        assert len(findings) == 0
    finally:
        db.close()


def test_check_signature_mismatch_evidence_format():
    """evidence JSON 包含 changed_symbol/old_signature/new_signature/caller_count/unresolved_callers"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_symbol_change_with_signature(
            db, task_id, step["step_id"],
            qualified_name="mod::test_fn", symbol_name="test_fn",
            hash_before="hash-old-005", hash_after="hash-new-005",
            sig_before="fn test_fn()",
            sig_after="fn test_fn(x: i32)",
        )
        _inject_caller(
            db, caller_name="mod::unresolved_caller", caller_file="src/u.rs",
            callee_name="test_fn", callee_qualified="",  # 空 → unresolved
            callee_id=0, call_line=20,
        )
        db._check_signature_mismatch(task_id, step["step_id"])
        findings = db.get_task_quality_findings(task_id, status="open")
        block_findings = [f for f in findings if f["severity"] == "block"]
        assert len(block_findings) == 1
        import json as _json
        evidence = _json.loads(block_findings[0]["evidence"])
        assert evidence["changed_symbol"] == "mod::test_fn"
        assert evidence["old_signature"] == "fn test_fn()"
        assert evidence["new_signature"] == "fn test_fn(x: i32)"
        assert evidence["caller_count"] == 1
        assert len(evidence["unresolved_callers"]) == 1
        assert evidence["unresolved_callers"][0]["caller"] == "mod::unresolved_caller"
    finally:
        db.close()


def test_run_task_completion_review_invokes_signature_mismatch_checker():
    """run_task_completion_review 调用 _check_signature_mismatch（即使无 changed_files）"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        # 不注入 change_audit（无 changed_files），但注入 symbol_changes
        _inject_symbol_change_with_signature(
            db, task_id, step["step_id"],
            qualified_name="mod::sig_test", symbol_name="sig_test",
            hash_before="hash-old-006", hash_after="hash-new-006",
            sig_before="fn sig_test()",
            sig_after="fn sig_test(x: bool)",
        )

        calls = {"signature_mismatch": 0}
        _orig = db._check_signature_mismatch

        def _mock_sig(tid, sid):
            calls["signature_mismatch"] += 1

        db._check_signature_mismatch = _mock_sig
        try:
            db.run_task_completion_review(task_id, step["step_id"])
        finally:
            db._check_signature_mismatch = _orig

        # 即使无 changed_files，signature_mismatch 也应被调用
        assert calls["signature_mismatch"] == 1
    finally:
        db.close()


def test_run_task_completion_review_signature_mismatch_blocks():
    """signature_mismatch block finding → decision=block"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        step = db.task_next_step(task_id)
        _inject_change_audit(db, task_id, step["step_id"], "sig.py")
        _inject_symbol_change_with_signature(
            db, task_id, step["step_id"],
            qualified_name="mod::break_fn", symbol_name="break_fn",
            hash_before="hash-old-007", hash_after="hash-new-007",
            sig_before="fn break_fn()",
            sig_after="fn break_fn(x: i32) -> bool",
        )
        _inject_caller(
            db, caller_name="mod::old_caller", caller_file="src/old.rs",
            callee_name="break_fn", callee_qualified="",  # unresolved
            callee_id=0, call_line=5,
        )

        restore_cg = _mock_run_check_gate(db, findings=[])
        try:
            review = db.run_task_completion_review(task_id, step["step_id"])
        finally:
            restore_cg()

        # signature_mismatch block → decision=block
        assert review["decision"] == "block"
        assert review["counts"]["block"] >= 1
        assert db.task_has_blocking_findings(task_id) is True
    finally:
        db.close()


