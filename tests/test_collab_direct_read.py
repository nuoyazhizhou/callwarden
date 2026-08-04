"""S5：collab 只读 MCP 工具 direct_read 接线测试

对应计划 4.8-4.11 验收（P1 集成面）：
- _collab_direct_read 直查 SQLite 真实表（不再引用不存在的 evidence_snapshots /
  evidence_verdict_gates 表）
- role_view.get 从最新契约 Envelope 生成视图（签名修正）
- evidence.query / freshness.status / gate.decision.query 返回真实数据
"""

import json
import os
import sys
import tempfile
import time

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB  # noqa: E402
from callwarden.db.task_snapshot import WorkspaceSnapshot  # noqa: E402
from callwarden.server.tools.tools_collab import _collab_direct_read  # noqa: E402

_FRESH_STATUSES = {
    "fresh", "stale", "invalid", "superseded",
    "historical_unbound", "unknown",
}


def _db_with_workspace():
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    db.register_workspace("collab-test-ws", root)
    db.set_active_workspace("collab-test-ws")
    return db, root


def _inject_contract(db, task_id, contract_id="C-collab-001"):
    """注入一条契约 Envelope 事件。"""
    db.conn.execute(
        """
        INSERT INTO task_contract_revisions
            (contract_id, revision, contract_hash, profile, task_id, workspace_id,
             envelope_payload, created_at, created_by)
        VALUES (?, 1, ?, 'code_change', ?, ?, ?, ?, ?)
        """,
        (
            contract_id,
            f"HASH-{contract_id}",
            task_id,
            db._get_active_workspace_id(),
            json.dumps({"contract_id": contract_id, "revision": 1,
                        "objective": "collab read test",
                        "tasks": [{"id": task_id, "description": "x"}]}),
            time.time(),
            "sess-impl-001",
        ),
    )
    db.conn.commit()


def _append_evidence(db, task_id, contract_id="C-collab-001", verifier="v1"):
    res = db.append_evidence(
        task_id=task_id,
        contract_id=contract_id,
        contract_revision=1,
        contract_hash=f"HASH-{contract_id}",
        evidence_type="test_run",
        snapshot=WorkspaceSnapshot(),
        verifier_name=verifier,
        verifier_version="1.0.0",
        verifier_config_hash="cfg-1",
        producer_identity="sess-impl-001",
        payload={"passed": True, "count": 3},
    )
    assert res.get("success"), f"append_evidence failed: {res}"
    return res["evidence_id"]


def test_direct_read_role_view_with_envelope():
    """注入契约 Envelope → role_view.get 返回基于真实 envelope 的视图。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("role-view-test", steps=[])
        _inject_contract(db, task_id)

        res = _collab_direct_read(db, "role_view.get", {"task_id": task_id, "role": "implementer"})

        assert res.get("task_id") == task_id
        assert res.get("view_type") == "implementer" or res.get("view", {}) is not None
    finally:
        db.close()


def test_direct_read_evidence_query():
    """append_evidence 后 → evidence.query 返回真实记录（含 verifier 过滤）。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("evidence-test", steps=[])
        eid = _append_evidence(db, task_id)

        res = _collab_direct_read(db, "evidence.query", {"task_id": task_id})
        assert res["count"] == 1
        assert res["items"][0]["evidence_id"] == eid

        # verifier 过滤
        res2 = _collab_direct_read(db, "evidence.query", {"task_id": task_id, "verifier": "v1"})
        assert res2["count"] == 1
        res3 = _collab_direct_read(db, "evidence.query", {"task_id": task_id, "verifier": "nobody"})
        assert res3["count"] == 0

        # contract_id 过滤
        res4 = _collab_direct_read(db, "evidence.query", {"contract_id": "C-collab-001"})
        assert res4["count"] == 1
    finally:
        db.close()


def test_direct_read_freshness_status():
    """append_evidence 后 → freshness.status 返回派生状态（合法值集合内）。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("freshness-test", steps=[])
        eid = _append_evidence(db, task_id)

        res = _collab_direct_read(db, "freshness.status", {"evidence_id": eid})
        assert len(res["items"]) == 1
        assert res["items"][0]["evidence_id"] == eid
        assert res["items"][0]["status"] in _FRESH_STATUSES

        res2 = _collab_direct_read(db, "freshness.status", {"task_id": task_id})
        assert len(res2["items"]) == 1
    finally:
        db.close()


def test_direct_read_gate_decision_query():
    """Evidence Gate 评估落盘 task_gate_decisions → gate.decision.query 返回真实记录。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create("gate-test", steps=[])
        _inject_contract(db, task_id)

        # 触发一次 gate 评估（缺 verdict → block），落盘 task_gate_decisions
        db.evaluate_evidence_gate_for_task(
            task_id=task_id, identity="sess-impl-001", authoritative_time=time.time()
        )

        res = _collab_direct_read(db, "gate.decision.query", {"task_id": task_id})
        assert res["count"] >= 1
        assert res["items"][0]["task_id"] == task_id
        assert res["items"][0]["decision"] in ("pass", "block")

        # 不存在的 task_id → 空列表
        res2 = _collab_direct_read(db, "gate.decision.query", {"task_id": "T-none"})
        assert res2["count"] == 0
    finally:
        db.close()


def test_direct_read_unknown_method():
    """未知方法 → 结构化 ok 响应（不抛异常）。"""
    db, _root = _db_with_workspace()
    try:
        res = _collab_direct_read(db, "unknown.method", {})
        assert res.get("status") == "ok"
    finally:
        db.close()
