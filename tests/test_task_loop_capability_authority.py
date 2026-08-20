"""0C：authority 写路径（db / API / CLI）的 gate-first fail-closed 回归测试。

计划 §3.3 锁序 `CapabilityMutationGate → authority store → task DB`：
0B 阶段 daemon control-plane route 未接入，enterprise/auto 模式下 authority
写入口必须稳定 fail-closed 为 `E_TASK_LOOP_CAPABILITY_DISABLED`，禁止绕过
gate 直写本地 SQLite；local 模式保留 legacy 直写语义（迁移契约）。

覆盖：
1. db 层：register_attestation_revocation / invalidate_evidence / revoke_verifier
2. MCP API 层：register_attestation_revocation 工具（get_db 不被触碰）
3. CLI 层：identity revoke（db 不被触碰）
4. writer-inventory：全部 authority 写路径在 enterprise 模式统一 fail-closed
5. migration：local 模式 legacy 校验语义不变（gate 未阻断直写）
"""

import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from callwarden.db.db import CodeGraphDB

GATE_DISABLED = "E_TASK_LOOP_CAPABILITY_DISABLED"


def _db(tmp_path):
    return CodeGraphDB(
        db_path=str(tmp_path / "capability-authority.db"),
        workspace_root=str(tmp_path),
    )


@pytest.fixture
def db(tmp_path):
    d = _db(tmp_path)
    yield d
    d.close()


@pytest.fixture
def daemon_mode(monkeypatch):
    """控制 `callwarden.config.get_daemon_mode` 返回值。

    db 层 `_require_authority_gate` 与 MCP 工具都在函数体内
    `from callwarden.config import get_daemon_mode`，patch 模块级属性即生效。
    """
    import callwarden.config as config

    def _apply(mode: str) -> str:
        monkeypatch.setattr(config, "get_daemon_mode", lambda: mode)
        return mode

    return _apply


# ============================================================
# 1. db 层：enterprise/auto 模式 fail-closed（no bypass）
# ============================================================

@pytest.mark.parametrize("mode", ["enterprise", "auto"])
def test_db_register_attestation_revocation_fail_closed(db, daemon_mode, mode):
    daemon_mode(mode)
    ok, result = db.register_attestation_revocation(
        issuer="iss",
        signing_key_id="k1",
        revocation_mode="compromised",
        revocation_reason="",
        initiating_actor="",
    )
    assert ok is False
    assert result["code"] == GATE_DISABLED
    # 无 bypass：未追加任何撤销记录
    n = db.conn.execute(
        "SELECT COUNT(*) AS n FROM attestation_revocation_records"
    ).fetchone()["n"]
    assert n == 0


@pytest.mark.parametrize("mode", ["enterprise", "auto"])
def test_db_invalidate_evidence_fail_closed(db, daemon_mode, mode):
    daemon_mode(mode)
    result = db.invalidate_evidence("E-x", "EVIDENCE_PAYLOAD_HASH_INVALID", "d")
    assert result.get("success") is False
    assert result.get("code") == GATE_DISABLED


@pytest.mark.parametrize("mode", ["enterprise", "auto"])
def test_db_revoke_verifier_fail_closed(db, daemon_mode, mode):
    daemon_mode(mode)
    result = db.revoke_verifier("v", "1", "cfg", "reason")
    assert result.get("success") is False
    assert result.get("code") == GATE_DISABLED


# ============================================================
# 2. migration：local 模式保留 legacy 直写语义
# ============================================================

def test_db_local_mode_keeps_legacy_validation_semantics(db, daemon_mode):
    daemon_mode("local")
    # 未携带 revocation_mode → 由 legacy 校验拒绝（gate 未阻断 local 直写）
    ok, result = db.register_attestation_revocation(
        issuer="iss",
        signing_key_id="k1",
        revocation_mode="",
        revocation_reason="",
        initiating_actor="",
    )
    assert ok is False
    assert result["code"] == "E_REVOCATION_MODE_REQUIRED"

    # 空 evidence_id → legacy 参数校验
    r2 = db.invalidate_evidence("", "EVIDENCE_PAYLOAD_HASH_INVALID")
    assert r2.get("success") is False
    assert r2.get("error") == "evidence_id is required"


# ============================================================
# 3. MCP API 层：register_attestation_revocation 工具 fail-closed
# ============================================================

def _register_tools(module, mcp=None):
    """注册工具模块到 mock MCP，返回 {name: fn} 字典（与既有 cutover 测试同款）。"""
    if mcp is None:
        mcp = MagicMock()

    registrations = {}

    def tool_capture(name=None):
        def decorator(fn):
            registrations[fn.__name__] = fn
            return fn

        return decorator

    mcp.tool = tool_capture
    module.register(mcp)
    return registrations


@pytest.mark.parametrize("mode", ["enterprise", "auto"])
def test_api_register_attestation_revocation_fail_closed(daemon_mode, mode, monkeypatch):
    from callwarden.server.tools import tools_p3_identity

    daemon_mode(mode)
    # HTTP transport 默认启用时会先命中 _http_unsupported（E_HTTP_COMPAT_UNSUPPORTED）；
    # 强制非 HTTP，使工具走到 0B gate-first 检查（E_TASK_LOOP_CAPABILITY_DISABLED）。
    monkeypatch.setattr(
        "callwarden.server.daemon_client.is_http_transport_enabled",
        lambda: False,
    )
    tools = _register_tools(tools_p3_identity)
    fn = tools["register_attestation_revocation"]
    with patch.object(tools_p3_identity, "get_db") as mock_get_db:
        result = fn(
            issuer="iss",
            signing_key_id="k1",
            revocation_mode="compromised",
        )
    assert isinstance(result, dict)
    assert result["status"] == "error"
    assert result["reason"]["code"] == GATE_DISABLED
    # 无 bypass：fail-closed 在触碰 get_db 之前返回
    mock_get_db.assert_not_called()


# ============================================================
# 4. CLI 层：identity revoke fail-closed（db 不被触碰）
# ============================================================

def test_cli_identity_revoke_fail_closed(monkeypatch):
    from callwarden.cli import main as cli_main

    # cli/main.py 顶层 import get_daemon_mode，patch 模块级属性
    monkeypatch.setattr(cli_main, "get_daemon_mode", lambda: "enterprise")

    captured = {}
    monkeypatch.setattr(
        cli_main,
        "_identity_reason_output",
        lambda reason, use_json: captured.update(reason=reason, use_json=use_json),
    )
    opts = SimpleNamespace(
        revocation_mode="compromised",
        agent_id="",
        session_id="",
        model_id="",
        role="",
    )

    class _NoDb:
        def register_attestation_revocation(self, *a, **k):
            raise AssertionError("enterprise 模式下 CLI 不得直写 db")

    ret = cli_main._identity_revoke(_NoDb(), opts, use_json=False)
    assert ret is True
    assert captured["reason"]["code"] == GATE_DISABLED
    assert captured["use_json"] is False


# ============================================================
# 5. writer-inventory：全部 authority 写路径统一 fail-closed
# ============================================================

# 已知 authority 写路径清单（含 db / API / CLI 三层）。
# 新增 authority 写入口必须同步登记并保持 gate-first，否则本清单回归失败。
WRITER_INVENTORY = [
    ("db.register_attestation_revocation", "db"),
    ("db.invalidate_evidence", "db"),
    ("db.revoke_verifier", "db"),
    ("api.register_attestation_revocation", "api"),
    ("cli.identity_revoke", "cli"),
]


class _BoomConn:
    """任何 DB 访问都抛错；commit/close 为 no-op 便于 teardown 兼容。"""

    def execute(self, *a, **k):
        raise AssertionError("gate 未生效：authority 写触碰了 DB")

    def commit(self, *a, **k):
        pass

    def close(self, *a, **k):
        pass


def _write_via(path, db, monkeypatch=None):
    """按清单路径执行一次 authority 写调用，返回结果 dict。"""
    if path == "db.register_attestation_revocation":
        ok, result = db.register_attestation_revocation(
            issuer="iss", signing_key_id="k1", revocation_mode="compromised",
            revocation_reason="", initiating_actor="",
        )
        assert ok is False
        return result
    if path == "db.invalidate_evidence":
        return db.invalidate_evidence("E-x", "EVIDENCE_PAYLOAD_HASH_INVALID", "d")
    if path == "db.revoke_verifier":
        return db.revoke_verifier("v", "1", "cfg", "reason")
    if path == "api.register_attestation_revocation":
        from callwarden.server.tools import tools_p3_identity

        # 强制非 HTTP，使工具走到 0B gate-first 检查（见 API 层专项测试）
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: False,
        )
        fn = _register_tools(tools_p3_identity)["register_attestation_revocation"]
        with patch.object(tools_p3_identity, "get_db"):
            return fn(issuer="iss", signing_key_id="k1",
                      revocation_mode="compromised")
    if path == "cli.identity_revoke":
        raise NotImplementedError("CLI 路径在 _write_via 内单独验证")
    raise KeyError(path)


def _result_code(result):
    """归一化提取各写路径返回中的稳定错误码。

    - db evidence 层：{success, code, ...}（code 顶层）
    - db.register_attestation_revocation：{code, detail, message_key}（code 顶层）
    - api 工具：{status: "error", reason: {code, ...}}（code 嵌套在 reason）
    """
    if not isinstance(result, dict):
        return None
    if result.get("code"):
        return result["code"]
    reason = result.get("reason")
    if isinstance(reason, dict) and reason.get("code"):
        return reason["code"]
    return None


@pytest.mark.parametrize("writer", WRITER_INVENTORY, ids=lambda w: w[0])
def test_writer_inventory_fail_closed(db, daemon_mode, monkeypatch, writer):
    """enterprise 模式下每个 authority 写路径都必须返回 GATE_DISABLED，
    且任何写路径都不得触碰 DB 连接（conn 被替换为抛错即证明无 bypass）。"""
    daemon_mode("enterprise")
    if writer[0].startswith("cli."):
        return  # CLI 路径由 test_cli_identity_revoke_fail_closed 覆盖
    # no-bypass 实证：任何 DB 访问都抛错；fail-closed 必须在触碰 DB 之前返回。
    # sqlite3.Connection 属性只读，不能 setattr，故整体替换 db.conn。
    monkeypatch.setattr(db, "conn", _BoomConn())
    result = _write_via(writer[0], db, monkeypatch)
    assert _result_code(result) == GATE_DISABLED, (
        f"{writer[0]} 未 fail-closed 为 {GATE_DISABLED}，返回 {result!r}"
    )
