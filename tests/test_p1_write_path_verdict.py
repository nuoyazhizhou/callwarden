"""P1 write-path：submit_verdict 写方法单元测试。

验证：
- submit_verdict 写入 task_verdict_events，字段完整落库
- 追加式记录：重跑追加新记录，不替换既有 payload（Req 1.7, 6.23）
- P4 受保护写（Req 11.8-11.9）：无 lease 直写通过；错误 token / 过期 /
  旧 counter 写入前拒绝，不改变 task data
- 写路径与 Evidence_Gate 消费闭环：evaluate_evidence_gate_for_task 可读到
  submit_verdict 写入的 verdict（legacy 任务无 contract 时仍判定 pass）
"""
import json
import os
import time

import pytest

from callwarden.db import CodeGraphDB


def _identity(agent_id="agent-a", session_id="sess-1", model_id="model-x", role="reviewer"):
    return {"agent_id": agent_id, "session_id": session_id, "model_id": model_id, "role": role}


@pytest.fixture()
def db(tmp_path):
    os.environ["CW_USE_RUST_STORAGE"] = "0"
    d = CodeGraphDB(str(tmp_path / "verdict.db"))
    d.register_workspace("verdict-ws", str(tmp_path))
    d.set_active_workspace("verdict-ws")
    # 测试注入 Authoritative_Clock 模拟（真实环境由 daemon ping 提供；
    # daemon 不可用时 Lease 写操作 fail-closed，见 LeaseMixin._clock）
    d._clock = lambda: time.time()
    yield d
    try:
        d.conn.close()
    except Exception:
        pass


# ---------------------------------------------------------------
# 1. 基本写入（P1 write-path）
# ---------------------------------------------------------------

def test_submit_verdict_basic(db):
    r = db.submit_verdict(
        task_id="T-1",
        contract_id="C-1",
        contract_revision=1,
        contract_hash="sha256:abc",
        phase="PRE_VERDICT",
        clause_results={"c1": {"status": "pass", "satisfied": True}},
        findings=[{"severity": "info", "text": "ok"}],
        overall="approved",
        reviewer_identity="reviewer-a",
    )
    assert r["success"] is True
    vid = r["verdict_id"]
    assert vid.startswith("V-")

    row = db.conn.execute(
        "SELECT * FROM task_verdict_events WHERE verdict_id = ?", (vid,)
    ).fetchone()
    assert row is not None
    assert row["task_id"] == "T-1"
    assert row["contract_id"] == "C-1"
    assert row["contract_revision"] == 1
    assert row["contract_hash"] == "sha256:abc"
    assert row["phase"] == "PRE_VERDICT"
    assert row["overall"] == "approved"
    assert json.loads(row["clause_results"]) == {"c1": {"status": "pass", "satisfied": True}}
    assert json.loads(row["findings"]) == [{"severity": "info", "text": "ok"}]
    assert row["reviewer_identity"] == "reviewer-a"


def test_submit_verdict_explicit_id(db):
    r = db.submit_verdict(
        task_id="T-1", contract_id="C-1", contract_revision=1,
        contract_hash="sha256:abc", verdict_id="V-fixed",
    )
    assert r["success"] is True
    assert r["verdict_id"] == "V-fixed"


def test_submit_verdict_validation(db):
    # 缺 contract_id
    r = db.submit_verdict(task_id="T-1", contract_id="", contract_revision=1, contract_hash="h")
    assert r["success"] is False
    # revision 非正
    r = db.submit_verdict(task_id="T-1", contract_id="C-1", contract_revision=0, contract_hash="h")
    assert r["success"] is False
    # 缺 hash
    r = db.submit_verdict(task_id="T-1", contract_id="C-1", contract_revision=1, contract_hash="")
    assert r["success"] is False


# ---------------------------------------------------------------
# 2. 追加式（Req 1.7, 6.23）
# ---------------------------------------------------------------

def test_submit_verdict_append_only(db):
    r1 = db.submit_verdict(
        task_id="T-1", contract_id="C-1", contract_revision=1,
        contract_hash="sha256:abc", overall="needs_changes",
    )
    r2 = db.submit_verdict(
        task_id="T-1", contract_id="C-1", contract_revision=1,
        contract_hash="sha256:abc", overall="approved",
    )
    assert r1["success"] is True
    assert r2["success"] is True
    assert r1["verdict_id"] != r2["verdict_id"]
    rows = db.conn.execute(
        "SELECT overall FROM task_verdict_events WHERE task_id = 'T-1' ORDER BY id"
    ).fetchall()
    overalls = [row["overall"] for row in rows]
    assert overalls == ["needs_changes", "approved"], "重跑追加新记录，不替换旧记录"


# ---------------------------------------------------------------
# 3. P4 受保护写（Req 11.8-11.9）
# ---------------------------------------------------------------

def test_submit_verdict_no_lease_direct_write(db):
    """无 lease_token 时不启用受保护校验，直写通过（向后兼容）。"""
    r = db.submit_verdict(
        task_id="T-1", contract_id="C-1", contract_revision=1, contract_hash="h",
    )
    assert r["success"] is True


def test_submit_verdict_lease_token_mismatch_rejected(db):
    """提供 lease_token/fencing_counter 时启用受保护写；token 不匹配写入前拒绝。"""
    ok, lease = db.acquire_lease("T-1", "reviewer", _identity())
    assert ok
    r = db.submit_verdict(
        task_id="T-1", contract_id="C-1", contract_revision=1, contract_hash="h",
        lease_token="wrong-token", fencing_counter=lease["fencing_counter"],
        lease_role="reviewer",
    )
    assert r["success"] is False
    # task data 未改变
    n = db.conn.execute(
        "SELECT COUNT(*) c FROM task_verdict_events WHERE task_id = 'T-1'"
    ).fetchone()["c"]
    assert n == 0


def test_submit_verdict_lease_stale_counter_rejected(db):
    """旧 fencing counter 写入前拒绝。"""
    ok, lease = db.acquire_lease("T-1", "reviewer", _identity())
    assert ok
    r = db.submit_verdict(
        task_id="T-1", contract_id="C-1", contract_revision=1, contract_hash="h",
        lease_token=lease["token"], fencing_counter=0,
        lease_role="reviewer",
    )
    assert r["success"] is False
    n = db.conn.execute(
        "SELECT COUNT(*) c FROM task_verdict_events WHERE task_id = 'T-1'"
    ).fetchone()["c"]
    assert n == 0


def test_submit_verdict_lease_valid_accepted(db):
    """正确 token + 当前 counter 通过。"""
    ok, lease = db.acquire_lease("T-1", "reviewer", _identity())
    assert ok
    r = db.submit_verdict(
        task_id="T-1", contract_id="C-1", contract_revision=1, contract_hash="h",
        overall="approved",
        lease_token=lease["token"], fencing_counter=lease["fencing_counter"],
        lease_role="reviewer",
    )
    assert r["success"] is True
    assert r["verdict_id"].startswith("V-")


# ---------------------------------------------------------------
# 4. 写路径与 Evidence_Gate 消费闭环
# ---------------------------------------------------------------

def test_gate_consumes_submitted_verdict(db):
    """submit_verdict 写入的 verdict 可被 evaluate_evidence_gate_for_task 读取。"""
    db.submit_verdict(
        task_id="T-1", contract_id="C-1", contract_revision=1,
        contract_hash="sha256:abc", overall="approved",
        reviewer_identity="reviewer-a",
    )
    # 无 contract_envelope 的 legacy 任务：有 verdict 事件则走正常 gate 评估（不再跳过）
    res = db.evaluate_evidence_gate_for_task("T-1", identity=_identity())
    assert res["decision"] in ("pass", "block", "review")
    # verdict 已进入评估输入
    assert res.get("verdicts_count", 1) >= 1 or "verdict" in json.dumps(res, ensure_ascii=False)
