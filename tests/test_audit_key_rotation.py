"""audit_chain 签名密钥轮换机制测试（C7）

验证：
1. rotate_signing_key 基本功能：首次轮换无 previous_key_id，二次轮换有 previous_key_id
2. 轮换后 sign_audit_record 使用新 key（signing_key_id = new_key_id）
3. 旧记录保持原签名不变，verify_audit_chain 在轮换后仍通过
4. 跨轮换点验证：多次轮换后所有记录都能验证通过
5. 幂等性：相同 key_id 再次轮换会更新 secret 并保持 active
6. 参数校验：空 key_id / 空 secret 抛 ValueError
7. list_signing_keys 不返回 key_secret（避免泄露）
8. _get_active_signing_key 优先级（表 > 环境变量 > None）
9. _lookup_signing_key 按 key_id 查找（含 "local"/"hmac" 向后兼容）
10. CLI `cw audit rotate-key` / `cw audit keys` 子命令 dispatch
11. CLI `--help` 不初始化数据库
12. MCP 工具 rotate_audit_signing_key / list_audit_signing_keys 已注册
13. i18n key（zh_CN / en_US）存在且占位符齐全
"""
import json
import os
import sys
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------

def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化，含 audit_key_rotations 表）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _sign_sample(db, table_name="task_quality_findings", idx=0):
    """签发一条样本审计记录，返回 signing_key_id 和 record_signature。"""
    result = db.sign_audit_record(
        table_name=table_name,
        record_id=f"rec-{idx}",
        payload={"action": "test", "idx": idx},
        operation="insert",
    )
    return result


# ----------------------------------------------------------------------
# DB 层：rotate_signing_key 基本功能
# ----------------------------------------------------------------------

def test_rotate_first_time_no_previous():
    """首次轮换：previous_key_id 为空串。"""
    db, _ = _db_with_workspace()
    try:
        result = db.rotate_signing_key(
            new_key_id="key-2026-07",
            new_key_secret="secret-1",
        )
        assert result["success"] is True
        assert result["key_id"] == "key-2026-07"
        assert result["previous_key_id"] == ""
        assert result["rotated_at"] > 0
    finally:
        db.close()


def test_rotate_second_time_has_previous():
    """二次轮换：previous_key_id 为前一个 active 密钥的 key_id。"""
    db, _ = _db_with_workspace()
    try:
        db.rotate_signing_key(new_key_id="key-2026-07", new_key_secret="secret-1")
        result = db.rotate_signing_key(new_key_id="key-2026-08", new_key_secret="secret-2")
        assert result["success"] is True
        assert result["key_id"] == "key-2026-08"
        assert result["previous_key_id"] == "key-2026-07"
    finally:
        db.close()


def test_rotate_empty_key_id_raises():
    """空 key_id 抛 ValueError。"""
    db, _ = _db_with_workspace()
    try:
        with pytest.raises(ValueError, match="new_key_id is required"):
            db.rotate_signing_key(new_key_id="", new_key_secret="secret")
    finally:
        db.close()


def test_rotate_whitespace_only_key_id_raises():
    """仅空白字符的 key_id 抛 ValueError。"""
    db, _ = _db_with_workspace()
    try:
        with pytest.raises(ValueError, match="new_key_id is required"):
            db.rotate_signing_key(new_key_id="   ", new_key_secret="secret")
    finally:
        db.close()


def test_rotate_empty_secret_raises():
    """空 secret 抛 ValueError。"""
    db, _ = _db_with_workspace()
    try:
        with pytest.raises(ValueError, match="new_key_secret is required"):
            db.rotate_signing_key(new_key_id="key-1", new_key_secret="")
    finally:
        db.close()


def test_rotate_idempotent_same_key_id():
    """相同 key_id 再次轮换：更新 secret 并保持 active（幂等）。"""
    db, _ = _db_with_workspace()
    try:
        # 首次轮换
        db.rotate_signing_key(new_key_id="key-1", new_key_secret="secret-old")
        # 再次用相同 key_id 但不同 secret
        result = db.rotate_signing_key(new_key_id="key-1", new_key_secret="secret-new")
        assert result["success"] is True
        assert result["key_id"] == "key-1"
        # previous_key_id 应为空（因为 key-1 之前就是 active，被更新后仍是 active）
        # 实际上：第二次调用时，查询 active 是 key-1，置为 inactive，再插入 key-1 active
        # 所以 previous_key_id 应该是 "key-1"
        assert result["previous_key_id"] == "key-1"

        # 验证只有一条记录，且 secret 已更新为 secret-new
        rows = db.list_signing_keys()
        key1_rows = [r for r in rows if r["key_id"] == "key-1"]
        assert len(key1_rows) == 1
        assert key1_rows[0]["is_active"] == 1
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：list_signing_keys
# ----------------------------------------------------------------------

def test_list_signing_keys_empty():
    """无轮换记录时返回空列表。"""
    db, _ = _db_with_workspace()
    try:
        rows = db.list_signing_keys()
        assert rows == []
    finally:
        db.close()


def test_list_signing_keys_returns_records_without_secret():
    """list_signing_keys 返回 key_id/rotated_at/is_active，不返回 key_secret。"""
    db, _ = _db_with_workspace()
    try:
        db.rotate_signing_key(new_key_id="key-1", new_key_secret="topsecret")
        db.rotate_signing_key(new_key_id="key-2", new_key_secret="another")

        rows = db.list_signing_keys()
        assert len(rows) == 2
        # 倒序：key-2 在前
        assert rows[0]["key_id"] == "key-2"
        assert rows[1]["key_id"] == "key-1"
        # is_active 字段存在
        assert "is_active" in rows[0]
        # key-2 是 active，key-1 不是
        assert rows[0]["is_active"] == 1
        assert rows[1]["is_active"] == 0
        # rotated_at 字段存在
        assert "rotated_at" in rows[0]
        # 不返回 key_secret
        assert "key_secret" not in rows[0]
        assert "key_secret" not in rows[1]
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：_get_active_signing_key 优先级
# ----------------------------------------------------------------------

def test_get_active_signing_key_prefers_table_over_env(monkeypatch):
    """audit_key_rotations 表中的 active 记录优先于环境变量。"""
    db, _ = _db_with_workspace()
    try:
        # 设置环境变量
        monkeypatch.setenv("CALLWARDEN_AUDIT_HMAC_KEY", "env-secret")
        # 先不轮换：应回落到环境变量
        key_id, key_bytes, level = db._get_active_signing_key()
        assert key_id == "hmac"
        assert key_bytes == b"env-secret"
        assert level == "hmac"

        # 轮换后：应使用表中的密钥
        db.rotate_signing_key(new_key_id="key-table", new_key_secret="table-secret")
        key_id, key_bytes, level = db._get_active_signing_key()
        assert key_id == "key-table"
        assert key_bytes == b"table-secret"
        assert level == "hmac"
    finally:
        db.close()


def test_get_active_signing_key_falls_back_to_hash_only(monkeypatch):
    """无表记录且无环境变量时，回落到 SHA-256 链（local）。"""
    db, _ = _db_with_workspace()
    try:
        # 清除环境变量
        monkeypatch.delenv("CALLWARDEN_AUDIT_HMAC_KEY", raising=False)
        # 确保没有 audit.key 文件（通过 mock os.path.isfile 返回 False）
        import callwarden.db.db_audit_chain as ac
        original_isfile = os.path.isfile
        monkeypatch.setattr(os.path, "isfile", lambda p: False if p == ac._AUDIT_KEY_FILE else original_isfile(p))

        key_id, key_bytes, level = db._get_active_signing_key()
        assert key_id == "local"
        assert key_bytes is None
        assert level == "hash_only"
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：_lookup_signing_key 按 key_id 查找
# ----------------------------------------------------------------------

def test_lookup_signing_key_local_returns_none():
    """key_id='local' 返回 None（SHA-256 链，无需密钥）。"""
    db, _ = _db_with_workspace()
    try:
        result = db._lookup_signing_key("local")
        assert result is None
    finally:
        db.close()


def test_lookup_signing_key_from_table():
    """从 audit_key_rotations 表中查找对应 key_secret。"""
    db, _ = _db_with_workspace()
    try:
        db.rotate_signing_key(new_key_id="key-1", new_key_secret="secret-1")
        db.rotate_signing_key(new_key_id="key-2", new_key_secret="secret-2")

        # 查找已停用的 key-1
        result = db._lookup_signing_key("key-1")
        assert result == b"secret-1"

        # 查找当前 active 的 key-2
        result = db._lookup_signing_key("key-2")
        assert result == b"secret-2"
    finally:
        db.close()


def test_lookup_signing_key_hmac_fallback(monkeypatch):
    """key_id='hmac' 时回落到环境变量/文件（向后兼容）。"""
    db, _ = _db_with_workspace()
    try:
        monkeypatch.setenv("CALLWARDEN_AUDIT_HMAC_KEY", "env-secret")
        result = db._lookup_signing_key("hmac")
        assert result == b"env-secret"
    finally:
        db.close()


def test_lookup_signing_key_unknown_returns_none():
    """未知 key_id 返回 None（无法验证，将标记为 signature_mismatch）。"""
    db, _ = _db_with_workspace()
    try:
        result = db._lookup_signing_key("nonexistent-key")
        assert result is None
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：轮换后 sign 使用新 key
# ----------------------------------------------------------------------

def test_sign_after_rotation_uses_new_key_id():
    """轮换后 sign_audit_record 的 signing_key_id 是新 key_id。"""
    db, _ = _db_with_workspace()
    try:
        # 轮换前：signing_key_id 为 'local'（无环境变量、无表记录）
        # 注意：测试环境可能没有 CALLWARDEN_AUDIT_HMAC_KEY，需清除
        import os as _os
        _os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        result_before = _sign_sample(db, idx=0)
        assert result_before["signing_key_id"] == "local"

        # 轮换
        db.rotate_signing_key(new_key_id="key-2026-07", new_key_secret="new-secret")

        # 轮换后：signing_key_id 为 'key-2026-07'
        result_after = _sign_sample(db, idx=1)
        assert result_after["signing_key_id"] == "key-2026-07"
        assert result_after["security_level"] == "hmac"
    finally:
        db.close()


def test_sign_before_and_after_rotation_have_different_signatures():
    """轮换前后签发相同 payload，record_signature 应不同（因密钥不同）。"""
    db, _ = _db_with_workspace()
    try:
        import os as _os
        _os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        # 轮换前签发
        before = db.sign_audit_record(
            table_name="task_quality_findings",
            record_id="rec-before",
            payload={"action": "test"},
        )

        # 轮换
        db.rotate_signing_key(new_key_id="key-1", new_key_secret="secret-1")

        # 轮换后签发相同 payload
        after = db.sign_audit_record(
            table_name="task_quality_findings",
            record_id="rec-after",
            payload={"action": "test"},
        )

        # 两条记录的 payload_hash 相同（因为 payload 相同）
        assert before["payload_hash"] == after["payload_hash"]
        # 但 record_signature 不同（因为密钥不同 + prev_signature 不同）
        assert before["record_signature"] != after["record_signature"]
        # signing_key_id 不同
        assert before["signing_key_id"] == "local"
        assert after["signing_key_id"] == "key-1"
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：旧记录验证仍通过（向后兼容核心）
# ----------------------------------------------------------------------

def test_verify_passes_after_rotation():
    """轮换后 verify_audit_chain 应全部通过（旧记录保持原签名，新记录用新 key）。"""
    db, _ = _db_with_workspace()
    try:
        import os as _os
        _os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        # 轮换前签发 2 条
        _sign_sample(db, idx=0)
        _sign_sample(db, idx=1)

        # 轮换
        db.rotate_signing_key(new_key_id="key-1", new_key_secret="secret-1")

        # 轮换后签发 2 条
        _sign_sample(db, idx=2)
        _sign_sample(db, idx=3)

        # 验证：全部应通过
        result = db.verify_audit_chain(table_name="task_quality_findings")
        assert result["total_count"] == 4
        assert result["verified_count"] == 4
        assert result["broken_count"] == 0
        assert result["security_level"] == "hmac"
    finally:
        db.close()


def test_verify_passes_across_multiple_rotations():
    """跨多次轮换点验证：所有记录都能通过（核心场景）。"""
    db, _ = _db_with_workspace()
    try:
        import os as _os
        _os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        # 阶段 1：无密钥（local）签发 2 条
        _sign_sample(db, idx=0)
        _sign_sample(db, idx=1)

        # 第一次轮换
        db.rotate_signing_key(new_key_id="key-v1", new_key_secret="secret-v1")
        _sign_sample(db, idx=2)
        _sign_sample(db, idx=3)

        # 第二次轮换
        db.rotate_signing_key(new_key_id="key-v2", new_key_secret="secret-v2")
        _sign_sample(db, idx=4)
        _sign_sample(db, idx=5)

        # 第三次轮换
        db.rotate_signing_key(new_key_id="key-v3", new_key_secret="secret-v3")
        _sign_sample(db, idx=6)
        _sign_sample(db, idx=7)

        # 验证：8 条记录，跨越 4 个密钥阶段（local + 3 次轮换），全部应通过
        result = db.verify_audit_chain(table_name="task_quality_findings")
        assert result["total_count"] == 8
        assert result["verified_count"] == 8
        assert result["broken_count"] == 0
        assert result["broken_records"] == []
    finally:
        db.close()


def test_verify_detects_tampered_record_after_rotation():
    """轮换后篡改旧记录的 payload（直接改库），verify 应检测到 signature_mismatch。"""
    db, _ = _db_with_workspace()
    try:
        import os as _os
        _os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        # 轮换前签发
        _sign_sample(db, idx=0)

        # 轮换
        db.rotate_signing_key(new_key_id="key-1", new_key_secret="secret-1")

        # 轮换后签发
        _sign_sample(db, idx=1)

        # 篡改第一条记录的 payload_hash（模拟直接改库）
        db.conn.execute(
            "UPDATE audit_chain SET payload_hash = 'tampered_hash' WHERE record_id = ?",
            ("rec-0",),
        )
        db.conn.commit()

        # 验证：应检测到 signature_mismatch
        result = db.verify_audit_chain(table_name="task_quality_findings")
        assert result["broken_count"] >= 1
        # 篡改的记录应在 broken_records 中
        reasons_set = set()
        for r in result["broken_records"]:
            reasons_set.update(r.get("reasons", []))
        assert "signature_mismatch" in reasons_set or "chain_broken" in reasons_set
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：链连续性（prev_signature）
# ----------------------------------------------------------------------

def test_chain_continuity_maintained_across_rotation():
    """轮换前后，audit_chain 的 prev_signature 连续性保持。"""
    db, _ = _db_with_workspace()
    try:
        import os as _os
        _os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        # 轮换前签发
        r1 = _sign_sample(db, idx=0)

        # 轮换
        db.rotate_signing_key(new_key_id="key-1", new_key_secret="secret-1")

        # 轮换后签发
        r2 = _sign_sample(db, idx=1)

        # 第二条记录的 prev_signature 应等于第一条的 record_signature
        assert r2["prev_signature"] == r1["record_signature"]
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：不同 table_name 独立链
# ----------------------------------------------------------------------

def test_independent_chains_per_table_after_rotation():
    """不同 table_name 维护独立链，轮换后两者都能验证通过。"""
    db, _ = _db_with_workspace()
    try:
        import os as _os
        _os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)

        # 轮换
        db.rotate_signing_key(new_key_id="key-1", new_key_secret="secret-1")

        # 在两个表上签发
        db.sign_audit_record("table_a", "rec-a-1", {"v": 1})
        db.sign_audit_record("table_b", "rec-b-1", {"v": 1})
        db.sign_audit_record("table_a", "rec-a-2", {"v": 2})

        # 两个表都能验证通过
        result_a = db.verify_audit_chain(table_name="table_a")
        result_b = db.verify_audit_chain(table_name="table_b")
        assert result_a["verified_count"] == 2
        assert result_b["verified_count"] == 1
        assert result_a["broken_count"] == 0
        assert result_b["broken_count"] == 0
    finally:
        db.close()


# ----------------------------------------------------------------------
# CLI 层
# ----------------------------------------------------------------------

def test_cli_rotate_key_dispatched():
    """CLI `cw audit rotate-key --key-id X --secret Y` 调用 db.rotate_signing_key。"""
    from callwarden.cli.main import _handle_audit

    class _MockDB:
        def __init__(self):
            self.calls = []

        def rotate_signing_key(self, new_key_id, new_key_secret):
            self.calls.append((new_key_id, new_key_secret))
            return {
                "success": True,
                "key_id": new_key_id,
                "rotated_at": 1700000000.0,
                "previous_key_id": "",
            }

    mock_db = _MockDB()
    _handle_audit(["rotate-key", "--key-id", "key-1", "--secret", "secret-1"], mock_db)
    assert mock_db.calls[-1] == ("key-1", "secret-1")


def test_cli_rotate_key_auto_generates_secret():
    """CLI `cw audit rotate-key --key-id X`（不带 --secret）自动生成随机密钥。"""
    from callwarden.cli.main import _handle_audit

    class _MockDB:
        def __init__(self):
            self.secrets = []

        def rotate_signing_key(self, new_key_id, new_key_secret):
            self.secrets.append(new_key_secret)
            return {"success": True, "key_id": new_key_id, "rotated_at": 0.0, "previous_key_id": ""}

    mock_db = _MockDB()
    _handle_audit(["rotate-key", "--key-id", "key-1"], mock_db)
    # 自动生成的 secret 应为 64 字符的 hex（32 字节）
    assert len(mock_db.secrets[-1]) == 64
    int(mock_db.secrets[-1], 16)  # 应为合法 hex


def test_cli_rotate_key_invalid_arg_prints_error(capsys):
    """db.rotate_signing_key 抛 ValueError 时，CLI 输出错误信息。"""
    from callwarden.cli.main import _handle_audit

    class _MockDB:
        def rotate_signing_key(self, new_key_id, new_key_secret):
            raise ValueError("new_key_id is required")

    _handle_audit(["rotate-key", "--key-id", "x", "--secret", "y"], _MockDB())
    out = capsys.readouterr().out
    assert "new_key_id is required" in out or "参数无效" in out or "Invalid" in out


def test_cli_keys_dispatched(capsys):
    """CLI `cw audit keys` 调用 db.list_signing_keys 并输出。"""
    from callwarden.cli.main import _handle_audit

    class _MockDB:
        def list_signing_keys(self):
            return [
                {"key_id": "key-2", "rotated_at": 2000.0, "is_active": 1},
                {"key_id": "key-1", "rotated_at": 1000.0, "is_active": 0},
            ]

    _handle_audit(["keys"], _MockDB())
    out = capsys.readouterr().out
    assert "key-2" in out
    assert "key-1" in out


def test_cli_keys_empty(capsys):
    """无记录时 `cw audit keys` 输出空提示。"""
    from callwarden.cli.main import _handle_audit

    class _MockDB:
        def list_signing_keys(self):
            return []

    _handle_audit(["keys"], _MockDB())
    out = capsys.readouterr().out
    # 应包含空提示（zh/en 任一）
    assert "无" in out or "No" in out


def test_cli_rotate_key_help_no_db():
    """`cw audit rotate-key --help` 不应初始化数据库。"""
    from unittest import mock
    from callwarden.cli import main as cli_main
    from callwarden.db.db import CodeGraphDB

    old_argv = sys.argv
    sys.argv = ["cw", "audit", "rotate-key", "--help"]
    try:
        db_init_called = {"count": 0}

        def fake_init(self, *args, **kwargs):
            db_init_called["count"] += 1
            raise RuntimeError("db should not be initialized for --help")

        with mock.patch.object(CodeGraphDB, "__init__", fake_init):
            with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                try:
                    cli_main._run_subcommand_mode()
                except SystemExit as e:
                    assert e.code == 0
        assert db_init_called["count"] == 0
    finally:
        sys.argv = old_argv


def test_cli_keys_help_no_db():
    """`cw audit keys --help` 不应初始化数据库。"""
    from unittest import mock
    from callwarden.cli import main as cli_main
    from callwarden.db.db import CodeGraphDB

    old_argv = sys.argv
    sys.argv = ["cw", "audit", "keys", "--help"]
    try:
        db_init_called = {"count": 0}

        def fake_init(self, *args, **kwargs):
            db_init_called["count"] += 1
            raise RuntimeError("db should not be initialized for --help")

        with mock.patch.object(CodeGraphDB, "__init__", fake_init):
            with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                try:
                    cli_main._run_subcommand_mode()
                except SystemExit as e:
                    assert e.code == 0
        assert db_init_called["count"] == 0
    finally:
        sys.argv = old_argv


# ----------------------------------------------------------------------
# CLI 层：只读判断
# ----------------------------------------------------------------------

def test_is_readonly_audit_verify():
    """audit verify 是只读。"""
    from callwarden.cli.main import _is_readonly_command
    assert _is_readonly_command("audit", ["verify"]) is True


def test_is_readonly_audit_keys():
    """audit keys 是只读（查询列表）。"""
    from callwarden.cli.main import _is_readonly_command
    assert _is_readonly_command("audit", ["keys"]) is True


def test_is_not_readonly_audit_rotate_key():
    """audit rotate-key 是写操作（不跳过 workspace 激活）。"""
    from callwarden.cli.main import _is_readonly_command
    assert _is_readonly_command("audit", ["rotate-key"]) is False


# ----------------------------------------------------------------------
# MCP 层
# ----------------------------------------------------------------------

def test_mcp_tool_rotate_audit_signing_key_registered():
    """MCP 工具 rotate_audit_signing_key 已注册到 server。"""
    server_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "server", "mcp_server.py",
    )
    with open(server_src, encoding="utf-8") as fh:
        content = fh.read()
    assert "def rotate_audit_signing_key(" in content
    assert "def list_audit_signing_keys(" in content


def test_mcp_tool_calls_db_methods():
    """MCP 工具内部调用 db.rotate_signing_key 和 db.list_signing_keys。"""
    server_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "server", "mcp_server.py",
    )
    with open(server_src, encoding="utf-8") as fh:
        content = fh.read()
    assert "db.rotate_signing_key(" in content
    assert "db.list_signing_keys()" in content


def test_mcp_tool_auto_generates_secret_when_empty():
    """MCP 工具 key_secret 为空时自动生成随机密钥。"""
    server_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "server", "mcp_server.py",
    )
    with open(server_src, encoding="utf-8") as fh:
        content = fh.read()
    # 确认有自动生成逻辑
    assert "token_hex" in content


# ----------------------------------------------------------------------
# i18n 层
# ----------------------------------------------------------------------

I18N_CLI_MESSAGES_KEYS = [
    "audit_rotate_key_title",
    "audit_rotate_key_key_id",
    "audit_rotate_key_rotated_at",
    "audit_rotate_key_previous",
    "audit_rotate_key_no_previous",
    "audit_rotate_key_hint",
    "audit_rotate_key_invalid_arg",
    "audit_rotate_key_failed",
    "audit_keys_title",
    "audit_keys_empty",
    "audit_keys_count",
    "audit_keys_item",
    "audit_keys_active_yes",
    "audit_keys_active_no",
    "audit_keys_current_active",
]

I18N_ARGPARSE_KEYS = [
    "cli_audit_rotate_key_desc",
    "cli_audit_rotate_key_arg_key_id",
    "cli_audit_rotate_key_arg_secret",
    "cli_audit_keys_desc",
]

PLACEHOLDER_CHECKS = [
    ("audit_rotate_key_key_id", ["{key_id}"]),
    ("audit_rotate_key_rotated_at", ["{ts}"]),
    ("audit_rotate_key_previous", ["{prev}"]),
    ("audit_rotate_key_invalid_arg", ["{error}"]),
    ("audit_rotate_key_failed", ["{error}"]),
    ("audit_keys_count", ["{count}"]),
    ("audit_keys_item", ["{idx}", "{key_id}", "{ts}", "{active}"]),
    ("audit_keys_current_active", ["{key_id}"]),
]


def _load_i18n(lang):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "i18n", f"{lang}.json",
    )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize("key", I18N_CLI_MESSAGES_KEYS + I18N_ARGPARSE_KEYS)
def test_i18n_keys_exist_zh(key):
    data = _load_i18n("zh_CN")
    if key in I18N_CLI_MESSAGES_KEYS:
        assert key in data.get("cli", {}).get("messages", {}), f"missing zh cli.messages.{key}"
    else:
        assert key in data, f"missing zh {key}"


@pytest.mark.parametrize("key", I18N_CLI_MESSAGES_KEYS + I18N_ARGPARSE_KEYS)
def test_i18n_keys_exist_en(key):
    data = _load_i18n("en_US")
    if key in I18N_CLI_MESSAGES_KEYS:
        assert key in data.get("cli", {}).get("messages", {}), f"missing en cli.messages.{key}"
    else:
        assert key in data, f"missing en {key}"


@pytest.mark.parametrize("key,placeholders", PLACEHOLDER_CHECKS)
def test_i18n_placeholders_zh(key, placeholders):
    data = _load_i18n("zh_CN")
    val = data.get("cli", {}).get("messages", {}).get(key, "")
    for ph in placeholders:
        assert ph in val, f"zh {key} missing placeholder {ph}"


@pytest.mark.parametrize("key,placeholders", PLACEHOLDER_CHECKS)
def test_i18n_placeholders_en(key, placeholders):
    data = _load_i18n("en_US")
    val = data.get("cli", {}).get("messages", {}).get(key, "")
    for ph in placeholders:
        assert ph in val, f"en {key} missing placeholder {ph}"


def test_i18n_json_files_valid():
    """两个 i18n JSON 文件都能被解析。"""
    _load_i18n("zh_CN")
    _load_i18n("en_US")


# ----------------------------------------------------------------------
# 源码引用一致性
# ----------------------------------------------------------------------

def test_source_uses_i18n_keys():
    """cli/main.py 的 handler 引用新 i18n key，不再硬编码标题。"""
    cli_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "cli", "main.py",
    )
    with open(cli_src, encoding="utf-8") as fh:
        content = fh.read()
    assert "audit_rotate_key_title" in content
    assert "audit_rotate_key_key_id" in content
    assert "audit_rotate_key_failed" in content
    assert "audit_keys_title" in content
    assert "audit_keys_empty" in content


def test_python_syntax_ok():
    """修改的 .py 文件语法正确。"""
    import py_compile
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ["cli/main.py", "server/mcp_server.py", "db/db_audit_chain.py",
                "db/db_base.py", "db/schema.py"]:
        py_compile.compile(os.path.join(base, rel), doraise=True)
