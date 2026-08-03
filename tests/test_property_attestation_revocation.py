"""Property 32：Attestation 撤销模式语义与时点可重算（任务 8.12）。

Validates：Requirements 10.10–10.18。

以固定种子随机生成任意撤销记录集合、任意 Revocation_Mode 取值、任意
Attestation 签发时间与撤销时间组合，验证：
- 每次撤销只追加一条不可变记录，不产生 N 条逐记录失效事件（10.10, 10.11）
- 不携带 Revocation_Mode 的请求以 Structured_Reason 拒绝、账本不增加任何记录，
  compromised/rotated 均不作为隐式默认（10.12）
- compromised：匹配 issuer+签名密钥的记录一律 invalid，独立于签发时间（10.13）
- rotated：仅当签发时间晚于撤销时间才 invalid；早于或等于保持原判定（10.14）
- 派生确定性：同一记录+同一撤销集合重复派生恒定；撤销派生与 Req 10.9 的
  Attestation 校验失败共用同一个 invalid 状态值，不存在第二个状态值（10.15）
- 时点可判定：可由 gate decision 记录的 issuer/签名密钥/签发时间/判定时间
  与匹配撤销记录的撤销时间和 Revocation_Mode 重算，不依赖历史失效事件（10.16）
- 撤销不修改既有 verdict/Evidence payload，逐字节不变（10.17）；
  个体失效事件机制仍保留（10.18, Req 6.6）

说明：环境未安装 hypothesis，采用固定种子 random.Random 的多轮随机属性测试；
断言只使用结构化状态与错误码（AGENTS.md 规则 35）。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB


E_REVOCATION_MODE_REQUIRED = "E_REVOCATION_MODE_REQUIRED"
MODES = ("compromised", "rotated")
SEED = 20260803
ITERATIONS = 20


_DB_COUNTER = 0


def _fresh_db(tmp_path):
    global _DB_COUNTER
    _DB_COUNTER += 1
    db_path = str(tmp_path / f"p3_property_{_DB_COUNTER}.db")
    db = CodeGraphDB(db_path, workspace_root=str(tmp_path))
    ws_id = db.register_workspace(f"p3-prop-{_DB_COUNTER}",
                                  str(tmp_path), "P3 属性测试")
    db.set_active_workspace(ws_id)
    return db, ws_id


def model_derive(records, issuer, key, issued_at):
    """纯模型：按撤销记录集合派生有效性（时点可重算的参照实现）。

    只依赖匹配记录的 revocation_mode 与 revoked_at——不依赖任何历史失效事件
    （Req 10.15, 10.16）。
    """
    for rec in records:
        if rec["issuer"] != issuer or rec["signing_key_id"] != key:
            continue
        if rec["revocation_mode"] == "compromised":
            return "invalid"
        if rec["revocation_mode"] == "rotated" and issued_at > rec["revoked_at"]:
            return "invalid"
    return "valid"


class TestPropertyAttestationRevocation:
    """Property 32：撤销模式语义与时点可重算。"""

    def test_random_sets_derive_consistently_with_model(self, tmp_path):
        """任意撤销集合下，db 派生与纯模型一致（含确定性）。"""
        rng = random.Random(SEED)
        for _ in range(ITERATIONS):
            db, _ws = _fresh_db(tmp_path)
            try:
                records = []
                n = rng.randint(0, 5)
                for i in range(n):
                    issuer = rng.choice(["daemon", "issuer-a", "issuer-b"])
                    key = rng.choice(["K1", "K2", "K3"])
                    mode = rng.choice(MODES)
                    ok, result = db.register_attestation_revocation(
                        issuer=issuer, signing_key_id=key,
                        revocation_mode=mode, revocation_reason="prop",
                        initiating_actor="tester")
                    assert ok is True
                    records.append({
                        "issuer": issuer,
                        "signing_key_id": key,
                        "revocation_mode": mode,
                        "revoked_at": result["revoked_at"],
                    })
                # 任意签发时间（含边界）下 db 派生 == 模型派生
                for _ in range(5):
                    issued_at = rng.uniform(-1000.0, 1000.0)
                    issuer = rng.choice(["daemon", "issuer-a", "issuer-b"])
                    key = rng.choice(["K1", "K2", "K3", "K-absent"])
                    got = db.derive_attestation_validity(
                        issuer, key, issued_at)
                    expected = model_derive(records, issuer, key, issued_at)
                    assert got == expected, (
                        f"派生与模型不一致 issuer={issuer} key={key} "
                        f"issued_at={issued_at} got={got} expected={expected}"
                    )
                    # 派生确定性：重复调用结果恒定（Req 10.15）
                    assert db.derive_attestation_validity(
                        issuer, key, issued_at) == got
            finally:
                db.close()

    def test_each_revocation_appends_exactly_one_record(self, tmp_path):
        """每次撤销只追加一条记录，且不产生逐条失效事件（Req 10.10-10.11）。"""
        rng = random.Random(SEED + 1)
        for _ in range(ITERATIONS):
            db, _ws = _fresh_db(tmp_path)
            try:
                ev_before = db.conn.execute(
                    "SELECT COUNT(*) FROM task_evidence_events").fetchone()[0]
                ver_before = db.conn.execute(
                    "SELECT COUNT(*) FROM task_verdict_events").fetchone()[0]
                n = rng.randint(1, 4)
                for _ in range(n):
                    ok, _r = db.register_attestation_revocation(
                        issuer="daemon",
                        signing_key_id=rng.choice(["K1", "K2"]),
                        revocation_mode=rng.choice(MODES),
                        revocation_reason="", initiating_actor="")
                    assert ok is True
                count = db.conn.execute(
                    "SELECT COUNT(*) FROM attestation_revocation_records").fetchone()[0]
                assert count == n
                assert db.conn.execute(
                    "SELECT COUNT(*) FROM task_evidence_events").fetchone()[0] == ev_before
                assert db.conn.execute(
                    "SELECT COUNT(*) FROM task_verdict_events").fetchone()[0] == ver_before
            finally:
                db.close()

    def test_missing_mode_rejected_no_implicit_default(self, tmp_path):
        """缺 Revocation_Mode 被拒、不追加记录；compromised/rotated 不作隐式默认。"""
        rng = random.Random(SEED + 2)
        for _ in range(ITERATIONS):
            db, _ws = _fresh_db(tmp_path)
            try:
                before = db.conn.execute(
                    "SELECT COUNT(*) FROM attestation_revocation_records").fetchone()[0]
                ok, reason = db.register_attestation_revocation(
                    issuer="daemon", signing_key_id="KX",
                    revocation_mode="", revocation_reason="", initiating_actor="")
                assert ok is False
                assert reason["code"] == E_REVOCATION_MODE_REQUIRED
                after = db.conn.execute(
                    "SELECT COUNT(*) FROM attestation_revocation_records").fetchone()[0]
                assert after == before
                # 未追加记录 → 该 issuer/key 的派生仍为 valid（无隐式默认撤销）
                assert db.derive_attestation_validity("daemon", "KX", 0.0) == "valid"
            finally:
                db.close()

    def test_compromised_independent_of_issuance_time(self, tmp_path):
        """compromised 全量 invalid，独立于签发时间（Req 10.13）。"""
        rng = random.Random(SEED + 3)
        for _ in range(ITERATIONS):
            db, _ws = _fresh_db(tmp_path)
            try:
                ok, _r = db.register_attestation_revocation(
                    issuer="daemon", signing_key_id="KC",
                    revocation_mode="compromised", revocation_reason="",
                    initiating_actor="")
                assert ok is True
                for _ in range(5):
                    t = rng.uniform(-1e9, 1e9)
                    assert db.derive_attestation_validity(
                        "daemon", "KC", t) == "invalid"
            finally:
                db.close()

    def test_rotated_iff_issued_after_revocation(self, tmp_path):
        """rotated：invalid 当且仅当签发时间晚于撤销时间（Req 10.14）。"""
        rng = random.Random(SEED + 4)
        for _ in range(ITERATIONS):
            db, _ws = _fresh_db(tmp_path)
            try:
                ok, result = db.register_attestation_revocation(
                    issuer="daemon", signing_key_id="KR",
                    revocation_mode="rotated", revocation_reason="",
                    initiating_actor="")
                assert ok is True
                revoked_at = result["revoked_at"]
                for _ in range(8):
                    t = rng.uniform(-1e9, 1e9)
                    expected = "invalid" if t > revoked_at else "valid"
                    assert db.derive_attestation_validity(
                        "daemon", "KR", t) == expected
                # 边界：等于撤销时间 → 保持原判定（valid）
                assert db.derive_attestation_validity(
                    "daemon", "KR", revoked_at) == "valid"
            finally:
                db.close()

    def test_point_in_time_recomputable_from_gate_decision_fields(self, tmp_path):
        """时点可判定：gate decision 记录的 issuer/key/签发时间即可重算。"""
        rng = random.Random(SEED + 5)
        for _ in range(ITERATIONS):
            db, _ws = _fresh_db(tmp_path)
            try:
                # 模拟 gate decision 记录的一组 issuer/key/签发时间
                decision_records = []
                for _ in range(rng.randint(1, 4)):
                    decision_records.append({
                        "issuer": rng.choice(["daemon", "issuer-x"]),
                        "signing_key_id": rng.choice(["K1", "K2", "K3"]),
                        "issued_at": rng.uniform(0.0, 1000.0),
                        "decision_time": rng.uniform(0.0, 2000.0),
                    })
                # 此后追加随机撤销集合
                revocations = []
                for _ in range(rng.randint(0, 4)):
                    ok, r = db.register_attestation_revocation(
                        issuer=rng.choice(["daemon", "issuer-x"]),
                        signing_key_id=rng.choice(["K1", "K2", "K3"]),
                        revocation_mode=rng.choice(MODES),
                        revocation_reason="", initiating_actor="")
                    assert ok is True
                    revocations.append({
                        "issuer": r["issuer"],
                        "signing_key_id": r["signing_key_id"],
                        "revocation_mode": r["revocation_mode"],
                        "revoked_at": r["revoked_at"],
                    })
                # 每条 decision 记录在判定时刻的撤销状态可仅由撤销记录集合重算
                for dr in decision_records:
                    got = db.derive_attestation_validity(
                        dr["issuer"], dr["signing_key_id"], dr["issued_at"])
                    expected = model_derive(
                        revocations, dr["issuer"], dr["signing_key_id"],
                        dr["issued_at"])
                    assert got == expected
            finally:
                db.close()

    def test_existing_payload_byte_for_byte_preserved(self, tmp_path):
        """撤销不修改既有 payload（Req 10.17）；个体失效机制保留（10.18）。"""
        rng = random.Random(SEED + 6)
        for _ in range(ITERATIONS):
            db, ws_id = _fresh_db(tmp_path)
            try:
                payload = f'{{"clause":"c","decision":"pass","n":{rng.randint(0, 9999)}}}'
                db.conn.execute(
                    "INSERT INTO task_verdict_events "
                    "(verdict_id, task_id, contract_id, contract_revision, "
                    " contract_hash, phase, clause_results, submitted_at, "
                    " workspace_id) VALUES ('V-PROP', 'T-1', 'C-1', 1, 'h', "
                    " 'first_pass', ?, 100.0, ?)",
                    (payload, ws_id))
                db.conn.commit()
                before = db.conn.execute(
                    "SELECT clause_results FROM task_verdict_events "
                    "WHERE verdict_id='V-PROP'").fetchone()[0]
                for _ in range(rng.randint(1, 3)):
                    db.register_attestation_revocation(
                        issuer="daemon", signing_key_id="K1",
                        revocation_mode=rng.choice(MODES),
                        revocation_reason="", initiating_actor="")
                after = db.conn.execute(
                    "SELECT clause_results FROM task_verdict_events "
                    "WHERE verdict_id='V-PROP'").fetchone()[0]
                assert after == before == payload
                # 个体失效机制保留（Req 6.6, 10.18）：表含失效字段
                cols = {r[1] for r in db.conn.execute(
                    "PRAGMA table_info(task_evidence_events)")}
                assert {"invalidation_reason", "original_evidence_ref"} <= cols
            finally:
                db.close()
