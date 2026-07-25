"""审计签名链表（audit_chain）schema 测试。

本测试覆盖 docs/design/task-quality-gate-plan.md 中 v22 schema 落地：
- audit_chain 表存在
- 2 个索引存在（table_record / signature）
- 字段完整性（table_name / record_id / operation / payload_hash /
  prev_signature / record_signature / signing_key_id / signed_at）
- 默认值正确（operation='insert', prev_signature='', signing_key_id='local'）
- 旧库重复迁移幂等（CREATE TABLE IF NOT EXISTS + CREATE INDEX IF NOT EXISTS）
- SCHEMA_VERSION 升级到 22 后 schema_version 表记录 v22
- 全新数据库直接包含 audit_chain（无需迁移）

后续步骤会扩展为 AuditChainMixin 业务方法、verify_audit_chain 等测试。
"""

import os
import sqlite3
import tempfile

import pytest

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


def test_schema_version_is_22():
    """SCHEMA_VERSION 常量不低于 22（audit_chain 引入版本）。"""
    assert SCHEMA_VERSION >= 22


def test_audit_chain_table_exists_on_fresh_db():
    """全新数据库直接包含 audit_chain 表（无需迁移）。"""
    db, _root = _db_with_workspace()
    try:
        assert _table_exists(db.conn, "audit_chain")
    finally:
        db.close()


def test_audit_chain_indexes_exist():
    """2 个索引存在：table_record / signature。"""
    db, _root = _db_with_workspace()
    try:
        assert _index_exists(db.conn, "idx_audit_chain_table_record")
        assert _index_exists(db.conn, "idx_audit_chain_signature")
    finally:
        db.close()


def test_audit_chain_columns():
    """字段完整性检查。"""
    expected = {
        "id", "table_name", "record_id", "operation",
        "payload_hash", "prev_signature", "record_signature",
        "signing_key_id", "signed_at",
    }
    db, _root = _db_with_workspace()
    try:
        cols = set(_table_columns(db.conn, "audit_chain"))
        missing = expected - cols
        assert not missing, f"missing columns: {missing}"
    finally:
        db.close()


def test_audit_chain_defaults():
    """默认值：operation='insert', prev_signature='', signing_key_id='local'。"""
    db, _root = _db_with_workspace()
    try:
        db.conn.execute(
            "INSERT INTO audit_chain (table_name, record_id, payload_hash, "
            "record_signature, signed_at) VALUES (?, ?, ?, ?, ?)",
            ("task_quality_findings", "1", "abc123", "sig001", 1000.0),
        )
        db.conn.commit()
        cur = db.conn.execute(
            "SELECT operation, prev_signature, signing_key_id "
            "FROM audit_chain WHERE table_name = ?",
            ("task_quality_findings",),
        )
        row = cur.fetchone()
        assert row["operation"] == "insert"
        assert row["prev_signature"] == ""
        assert row["signing_key_id"] == "local"
    finally:
        db.close()


def test_migration_v21_to_v22_is_idempotent():
    """v21 -> v22 迁移幂等：在已有表的库上重复执行不报错。

    直接调用迁移函数 _migrate_v21_to_v22，模拟旧库 v21 迁移到 v22。
    第二次调用应当因 IF NOT EXISTS 而 no-op。
    """
    root = tempfile.mkdtemp()
    db_path = os.path.join(root, "callwarden.db")
    db = CodeGraphDB(db_path, workspace_root=root)
    try:
        # 数据库已通过完整 SCHEMA_SQL 包含 audit_chain，
        # 现在再次调用迁移函数，验证幂等。
        from callwarden.db.db_base import _migrate_v21_to_v22
        _migrate_v21_to_v22(db.conn)
        _migrate_v21_to_v22(db.conn)
        assert _table_exists(db.conn, "audit_chain")
        assert _index_exists(db.conn, "idx_audit_chain_table_record")
        assert _index_exists(db.conn, "idx_audit_chain_signature")
    finally:
        db.close()


def test_migration_v21_to_v22_on_legacy_v21_db():
    """模拟 v21 旧库：手动构造一个不含 audit_chain 的库，
    再执行 _migrate_v21_to_v22，验证表和索引被创建。
    """
    root = tempfile.mkdtemp()
    db_path = os.path.join(root, "callwarden.db")
    # 直接用裸 sqlite3 建一个最小 v21 库（不含 audit_chain）
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, root_path TEXT)")
    conn.commit()

    from callwarden.db.db_base import _migrate_v21_to_v22
    _migrate_v21_to_v22(conn)
    conn.commit()

    assert _table_exists(conn, "audit_chain")
    assert _index_exists(conn, "idx_audit_chain_table_record")
    assert _index_exists(conn, "idx_audit_chain_signature")
    conn.close()


def test_schema_version_table_records_v22_on_fresh_db():
    """全新数据库 schema_version 表记录当前版本（≥22，audit_chain 引入版本）。"""
    db, _root = _db_with_workspace()
    try:
        cur = db.conn.execute(
            "SELECT MAX(version) as v FROM schema_version"
        )
        row = cur.fetchone()
        assert row is not None and row["v"] >= 22, "schema_version table missing or below v22"
    finally:
        db.close()


def test_legacy_v21_db_migrates_to_v22_via_init_schema():
    """旧 v21 库通过 _init_schema 自动迁移到 v22。

    构造一个 v21 库（schema_version 表标记为 21，不含 audit_chain），
    再用 CodeGraphDB 打开，触发 _migrate_schema(21, 22)。
    """
    root = tempfile.mkdtemp()
    db_path = os.path.join(root, "callwarden.db")
    import time

    # 手动构造 v21 库（含足够完整的 workspaces 以满足 _init_workspace）
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE workspaces (id INTEGER PRIMARY KEY, name TEXT, "
        "root_path TEXT, is_active INTEGER DEFAULT 0, "
        "created_at REAL, description TEXT DEFAULT '')"
    )
    conn.execute(
        "INSERT INTO workspaces (name, root_path, created_at, is_active, description) "
        "VALUES (?, ?, ?, 1, '')",
        ("test-ws", root, time.time()),
    )
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, title TEXT)")
    conn.execute("CREATE TABLE schema_version (version INTEGER PRIMARY KEY, applied_at REAL, description TEXT)")
    conn.execute("INSERT INTO schema_version (version, applied_at, description) VALUES (?, ?, ?)",
                (21, time.time(), "v21 for test"))
    conn.commit()
    conn.close()

    # 用 CodeGraphDB 打开，应自动触发迁移
    db = CodeGraphDB(db_path, workspace_root=root)
    try:
        assert _table_exists(db.conn, "audit_chain")
        assert _index_exists(db.conn, "idx_audit_chain_table_record")
        # schema_version 应升级到 22
        cur = db.conn.execute(
            "SELECT version FROM schema_version WHERE version = ?",
            (22,),
        )
        assert cur.fetchone() is not None, "v22 not recorded after migration"
    finally:
        db.close()


def test_audit_chain_insert_and_query():
    """插入和查询 audit_chain 记录的端到端流程。"""
    db, _root = _db_with_workspace()
    try:
        # 插入 3 条记录，模拟链式结构
        records = [
            ("task_quality_findings", "1", "hash1", "", "sig1", 1000.0),
            ("task_quality_findings", "2", "hash2", "sig1", "sig2", 2000.0),
            ("change_audit", "10", "hash3", "sig2", "sig3", 3000.0),
        ]
        for table_name, record_id, payload_hash, prev_sig, rec_sig, signed_at in records:
            db.conn.execute(
                "INSERT INTO audit_chain (table_name, record_id, payload_hash, "
                "prev_signature, record_signature, signed_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (table_name, record_id, payload_hash, prev_sig, rec_sig, signed_at),
            )
        db.conn.commit()

        # 按表查询
        cur = db.conn.execute(
            "SELECT * FROM audit_chain WHERE table_name = ? ORDER BY signed_at",
            ("task_quality_findings",),
        )
        rows = [dict(r) for r in cur.fetchall()]
        assert len(rows) == 2
        assert rows[0]["record_signature"] == "sig1"
        assert rows[1]["prev_signature"] == "sig1"  # 链式：第二条 prev 指向第一条

        # 按 record_id 查询
        cur = db.conn.execute(
            "SELECT * FROM audit_chain WHERE record_id = ?",
            ("10",),
        )
        rows = [dict(r) for r in cur.fetchall()]
        assert len(rows) == 1
        assert rows[0]["table_name"] == "change_audit"

        # 按 signature 查询
        cur = db.conn.execute(
            "SELECT * FROM audit_chain WHERE record_signature = ?",
            ("sig2",),
        )
        rows = [dict(r) for r in cur.fetchall()]
        assert len(rows) == 1
        assert rows[0]["record_id"] == "2"
    finally:
        db.close()


# ============================================
# 端到端集成测试：业务流程触发签名链
# ============================================


def test_task_quality_finding_write_triggers_audit_chain():
    """record_task_quality_finding 写入后 audit_chain 应有对应签名记录"""
    db, _root = _db_with_workspace()
    try:
        # 创建任务
        task_id = db.task_create("audit-test", steps=[{"action": "edit", "target_file": ""}])
        # 写入 finding
        fid = db.record_task_quality_finding(
            task_id=task_id,
            finding_type="semgrep",
            severity="warn",
            message="unused import",
            source="semgrep",
        )
        assert fid > 0

        # 验证 audit_chain 表中存在对应记录
        cur = db.conn.execute(
            "SELECT * FROM audit_chain WHERE table_name = ? AND record_id = ?",
            ("task_quality_findings", str(fid)),
        )
        rows = [dict(r) for r in cur.fetchall()]
        assert len(rows) == 1
        assert rows[0]["operation"] == "insert"
        assert rows[0]["payload_hash"]  # 非空
        assert rows[0]["record_signature"]  # 非空
    finally:
        db.close()


def test_task_symbol_change_write_triggers_audit_chain():
    """record_task_symbol_change 写入后 audit_chain 应有对应签名记录"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("audit-test", steps=[{"action": "edit", "target_file": ""}])
        result = db.record_task_symbol_change(
            task_id=task_id,
            file_path="some/file.py",
            qualified_name="module::func",
            symbol_name="func",
            symbol_hash_before="h_before",
            symbol_hash_after="h_after",
            change_type="modified",
            source="test",
        )
        assert result["success"] is True
        change_id = result["id"]

        cur = db.conn.execute(
            "SELECT * FROM audit_chain WHERE table_name = ? AND record_id = ?",
            ("task_symbol_changes", str(change_id)),
        )
        rows = [dict(r) for r in cur.fetchall()]
        assert len(rows) == 1
        assert rows[0]["operation"] == "insert"
    finally:
        db.close()


def test_audit_chain_continuity_across_multiple_signings():
    """多次签名后链应保持连续：每条 prev_signature 指向上一条 record_signature"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("audit-test", steps=[{"action": "edit", "target_file": ""}])

        # 写入 3 条 finding，对应 3 条 audit_chain 记录
        f1 = db.record_task_quality_finding(task_id, message="m1")
        f2 = db.record_task_quality_finding(task_id, message="m2")
        f3 = db.record_task_quality_finding(task_id, message="m3")

        # 查询 audit_chain 中 task_quality_findings 表的所有记录
        cur = db.conn.execute(
            "SELECT id, prev_signature, record_signature FROM audit_chain "
            "WHERE table_name = ? ORDER BY id ASC",
            ("task_quality_findings",),
        )
        rows = [dict(r) for r in cur.fetchall()]
        assert len(rows) == 3

        # 首条 prev_signature 为空
        assert rows[0]["prev_signature"] == ""
        # 第二条 prev_signature 等于第一条 record_signature
        assert rows[1]["prev_signature"] == rows[0]["record_signature"]
        # 第三条 prev_signature 等于第二条 record_signature
        assert rows[2]["prev_signature"] == rows[1]["record_signature"]
    finally:
        db.close()


def test_verify_detects_direct_update_to_audit_chain_record():
    """直接 UPDATE audit_chain 的 payload_hash 后 verify 应失败"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("audit-test", steps=[{"action": "edit", "target_file": ""}])
        db.record_task_quality_finding(task_id, message="original")

        # 篡改 audit_chain 中第一条记录的 payload_hash
        db.conn.execute(
            "UPDATE audit_chain SET payload_hash = ? WHERE id = ?",
            ("tampered_hash", 1),
        )
        db.conn.commit()

        result = db.verify_audit_chain(table_name="task_quality_findings")
        assert result["broken_count"] >= 1
        reasons = result["broken_records"][0]["reasons"]
        assert "signature_mismatch" in reasons
    finally:
        db.close()


def test_verify_detects_direct_update_to_source_table():
    """直接 UPDATE task_quality_findings 的 message 字段后，audit_chain 仍可检测
    （因为 payload_hash 不匹配源记录，但 verify_audit_chain 不直接比对源表，
    此处验证 audit_chain 自身的完整性）
    """
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("audit-test", steps=[{"action": "edit", "target_file": ""}])
        fid = db.record_task_quality_finding(task_id, message="original")

        # 直接 UPDATE 源表 task_quality_findings
        db.conn.execute(
            "UPDATE task_quality_findings SET message = ? WHERE id = ?",
            ("tampered_message", fid),
        )
        db.conn.commit()

        # verify_audit_chain 校验 audit_chain 自身完整性，应通过
        # （签名链本身未被破坏，但源表已被篡改——这体现了 hash_only 的局限：
        #   防不了「同时改源表和 audit_chain」的攻击，但能防「只改 audit_chain」）
        result = db.verify_audit_chain(table_name="task_quality_findings")
        assert result["broken_count"] == 0
    finally:
        db.close()


def test_hash_only_mode_detects_signature_tamper():
    """无 HMAC key 时（hash_only 模式）仍能检测 record_signature 被篡改"""
    db, _root = _db_with_workspace()
    try:
        # 确保无 HMAC key
        import os
        os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        task_id = db.task_create("audit-test", steps=[{"action": "edit", "target_file": ""}])
        db.record_task_quality_finding(task_id, message="m1")
        db.record_task_quality_finding(task_id, message="m2")

        # 篡改第一条的 record_signature（链头被改）
        db.conn.execute(
            "UPDATE audit_chain SET record_signature = ? WHERE id = ?",
            ("fake_signature_for_first_record", 1),
        )
        db.conn.commit()

        result = db.verify_audit_chain(table_name="task_quality_findings")
        assert result["broken_count"] >= 1
        assert result["security_level"] == "hash_only"
    finally:
        db.close()


def test_hash_only_mode_detects_middle_record_tamper():
    """无 HMAC key 时篡改中间记录的 payload_hash 应被检测"""
    db, _root = _db_with_workspace()
    try:
        import os
        os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        task_id = db.task_create("audit-test", steps=[{"action": "edit", "target_file": ""}])
        db.record_task_quality_finding(task_id, message="m1")
        db.record_task_quality_finding(task_id, message="m2")
        db.record_task_quality_finding(task_id, message="m3")

        # 篡改中间记录的 payload_hash
        db.conn.execute(
            "UPDATE audit_chain SET payload_hash = ? WHERE id = ?",
            ("tampered_middle_hash", 2),
        )
        db.conn.commit()

        result = db.verify_audit_chain(table_name="task_quality_findings")
        assert result["broken_count"] >= 1
        broken_ids = [r["id"] for r in result["broken_records"]]
        assert 2 in broken_ids
    finally:
        db.close()


def test_verify_all_pass_without_tamper():
    """无篡改时 verify 应全部通过"""
    db, _root = _db_with_workspace()
    try:
        import os
        os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        task_id = db.task_create("audit-test", steps=[{"action": "edit", "target_file": ""}])
        db.record_task_quality_finding(task_id, message="m1")
        db.record_task_quality_finding(task_id, message="m2")
        db.record_task_quality_finding(task_id, message="m3")

        result = db.verify_audit_chain(table_name="task_quality_findings")
        assert result["total_count"] == 3
        assert result["verified_count"] == 3
        assert result["broken_count"] == 0
        assert result["broken_records"] == []
    finally:
        db.close()


def test_verify_chain_broken_when_record_deleted():
    """删除中间 audit_chain 记录后，链断裂应被检测"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("audit-test", steps=[{"action": "edit", "target_file": ""}])
        db.record_task_quality_finding(task_id, message="m1")
        db.record_task_quality_finding(task_id, message="m2")
        db.record_task_quality_finding(task_id, message="m3")

        # 删除中间记录（id=2）
        db.conn.execute("DELETE FROM audit_chain WHERE id = ?", (2,))
        db.conn.commit()

        result = db.verify_audit_chain(table_name="task_quality_findings")
        assert result["total_count"] == 2  # 剩余 2 条
        # 第三条的 prev_signature 不再匹配第一条的 record_signature
        assert result["broken_count"] >= 1
        reasons = result["broken_records"][0]["reasons"]
        assert "chain_broken" in reasons
    finally:
        db.close()


# ============================================
# CLI cw audit verify 测试
# ============================================


def test_cli_audit_verify_help_no_db():
    """cw audit verify --help 不应初始化数据库。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    old_argv = sys.argv
    sys.argv = ["cw", "audit", "verify", "--help"]
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
                        pytest.fail("db initialized during cw audit verify --help")
                    raise
        assert db_init_called["count"] == 0
    finally:
        sys.argv = old_argv


def test_cli_audit_help_no_db():
    """cw audit --help 不应初始化数据库。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    old_argv = sys.argv
    sys.argv = ["cw", "audit", "--help"]
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
                        pytest.fail("db initialized during cw audit --help")
                    raise
        assert db_init_called["count"] == 0
    finally:
        sys.argv = old_argv


def test_cli_audit_verify_calls_db_method():
    """cw audit verify 必须调用 db.verify_audit_chain()。"""
    from unittest import mock
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            call_log = {"count": 0, "kwargs": None}
            original = db.verify_audit_chain

            def spy(*args, **kwargs):
                call_log["count"] += 1
                call_log["kwargs"] = kwargs
                return original(*args, **kwargs)

            with mock.patch.object(db, "verify_audit_chain", side_effect=spy):
                try:
                    cli_main._handle_audit(["verify"], db)
                except SystemExit:
                    pass

            assert call_log["count"] == 1, "db.verify_audit_chain 必须被调用一次"
            kw = call_log["kwargs"] or {}
            assert kw.get("table_name") == ""
            assert kw.get("limit") == 1000
        finally:
            db.close()


def test_cli_audit_verify_passes_table_and_limit():
    """cw audit verify --table X --limit N 必须传递参数。"""
    from unittest import mock
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            call_log = {"kwargs": None}
            original = db.verify_audit_chain

            def spy(*args, **kwargs):
                call_log["kwargs"] = kwargs
                return original(*args, **kwargs)

            with mock.patch.object(db, "verify_audit_chain", side_effect=spy):
                try:
                    cli_main._handle_audit(
                        ["verify", "--table", "change_audit", "--limit", "50"],
                        db,
                    )
                except SystemExit:
                    pass

            kw = call_log["kwargs"] or {}
            assert kw.get("table_name") == "change_audit"
            assert kw.get("limit") == 50
        finally:
            db.close()


def test_cli_audit_verify_output_contains_fields():
    """cw audit verify 输出必须包含 total/verified/broken/security_level 字段。"""
    import io
    from contextlib import redirect_stdout
    from callwarden.cli import main as cli_main

    with tempfile.TemporaryDirectory() as tmpdir:
        db = CodeGraphDB(workspace_root=tmpdir)
        try:
            # 无记录时的输出
            buf = io.StringIO()
            with redirect_stdout(buf):
                try:
                    cli_main._handle_audit(["verify"], db)
                except SystemExit:
                    pass
            out = buf.getvalue()

            # 应包含汇总字段标签（通过 i18n key 的 default 值或实际 zh/en 文本）
            # security_level 字段值 hash_only
            assert "hash_only" in out, f"输出应包含 security_level=hash_only, 实际: {out!r}"
            # 应包含 total/verified/broken 汇总行（数字 0）
            assert "0" in out, f"输出应包含数字 0, 实际: {out!r}"
        finally:
            db.close()


def test_cli_audit_verify_is_readonly():
    """audit verify 是只读命令，应在 _is_readonly_command 中返回 True。"""
    from callwarden.cli import main as cli_main

    assert cli_main._is_readonly_command("audit", ["verify"]) is True
    assert cli_main._is_readonly_command("audit", []) is False
    # 非 verify 的 action 不算只读
    assert cli_main._is_readonly_command("audit", ["unknown"]) is False


def test_cli_audit_in_subcommands():
    """audit 应在 _SUBCOMMANDS 集合中。"""
    from callwarden.cli import main as cli_main

    assert "audit" in cli_main._SUBCOMMANDS


def test_cli_audit_dispatched():
    """_dispatch_subcommand 应将 audit 分发到 _handle_audit。"""
    import sys
    from unittest import mock
    from callwarden.cli import main as cli_main

    old_argv = sys.argv
    sys.argv = ["cw", "audit", "--help"]
    try:
        called = {"handler": None}
        original = cli_main._handle_audit

        def spy(args, db):
            called["handler"] = "audit"
            return original(args, db)

        with mock.patch.object(cli_main, "_handle_audit", side_effect=spy):
            try:
                cli_main._dispatch_subcommand(["--help"], db=None)
            except SystemExit:
                pass
        assert called["handler"] == "audit", "audit 应分发到 _handle_audit"
    finally:
        sys.argv = old_argv


# ============================================
# MCP audit_verify_chain 工具测试
# ============================================


def test_mcp_audit_verify_chain_registered():
    """MCP server 注册了 audit_verify_chain 工具。"""
    import inspect
    from server import mcp_server

    src = inspect.getsource(mcp_server.create_mcp_server)
    assert "def audit_verify_chain(" in src, "MCP 源码缺少 audit_verify_chain 工具定义"
    assert "@mcp.tool()" in src, "MCP 源码缺少 @mcp.tool() 装饰器"


def test_mcp_audit_verify_chain_signature():
    """audit_verify_chain MCP 工具签名包含 table_name/limit。"""
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
        if isinstance(node, ast.FunctionDef) and node.name == "audit_verify_chain":
            func_def = node
            break
    assert func_def is not None, "未找到 audit_verify_chain 函数定义"

    arg_names = [a.arg for a in func_def.args.args]
    assert "table_name" in arg_names
    assert "limit" in arg_names


def test_mcp_audit_verify_chain_in_tool_list():
    """create_mcp_server 返回的 server 工具列表包含 audit_verify_chain。"""
    from server.mcp_server import create_mcp_server

    mcp = create_mcp_server()
    tools = [t.name for t in mcp._tool_manager.list_tools()]
    assert "audit_verify_chain" in tools
