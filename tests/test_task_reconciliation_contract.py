"""历史任务 reconciliation daemon 路由的回归约束（fix_defect 修复版）。

覆盖 T-1787823611412-2f503878 #5 fix_defect 的 reviewer findings：
1. apply 路径必须要求 adjudicator 身份 + 独立 reviewer lease（token/fencing/holder），
   缺失/错误/陈旧/身份不符一律 fail-closed，拒绝路径无状态变化；
2. handler 位于 task_collab_query.rs（原断言错误指向 task_collab.rs）；
3. 真实 daemon 负向 E2E（daemon 不可用时 runtime 段 skip，静态门禁仍执行）。
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient

ROOT = Path(__file__).resolve().parents[1]
QUERY = ROOT / "rust_ext" / "src" / "daemon" / "task_collab_query.rs"
TASK_COLLAB = ROOT / "rust_ext" / "src" / "daemon" / "task_collab.rs"
DISPATCH = ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs"
OPERATION_STORE = ROOT / "rust_ext" / "src" / "daemon" / "task_loop" / "operation_store.rs"
DB = Path.home() / ".callwarden" / "callwarden.db"

REVIEWER = {
    "agent_id": "reviewer-wb-186loop",
    "agent_instance_id": "inst-reviewer-wb-186loop",
    "session_id": "sess-reviewer-wb-186loop",
    "model_id": "deepseek-v4-flash",
    "role": "reviewer",
}
ADJUDICATOR = {
    "agent_id": "adjudicator-workbuddy-v1",
    "session_id": "sess-adjudicator-wb-20260821-01",
    "model_id": "deepseek-v4-flash",
    "role": "adjudicator",
}
IMPLEMENTER = {
    "agent_id": "workbuddy-186loop",
    "agent_instance_id": "inst-workbuddy-186loop",
    "session_id": "sess-workbuddy-186loop",
    "model_id": "deepseek-v4-flash",
    "role": "implementer",
}


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def rpc():
    c = HttpDaemonRpcClient()
    try:
        c.call("ping", {})
    except Exception:
        pytest.skip("daemon 不可用：runtime 段跳过（静态门禁仍执行）")
    return c


@pytest.fixture(scope="module", autouse=True)
def cleanup_trees():
    yield
    conn = sqlite3.connect(str(DB))
    for t in ("action_identities", "task_events", "task_contract_revisions",
              "task_steps", "task_verdict_events", "task_leases", "task_assignments",
              "task_step_role_contract_bindings", "role_contracts", "task_workspace_bindings"):
        try:
            conn.execute(f"DELETE FROM {t} WHERE task_id LIKE 'T-RECON-NEG-%'")
        except Exception:
            pass
    conn.execute("DELETE FROM tasks WHERE id LIKE 'T-RECON-NEG-%'")
    conn.commit()
    conn.close()


# ============================================================
# 静态门禁（handler 位于 task_collab_query.rs）
# ============================================================


def test_reconciliation_is_a_protected_idempotent_daemon_mutation():
    dispatch = _source(DISPATCH)
    operation_store = _source(OPERATION_STORE)
    query = _source(QUERY)

    assert '"task.reconcile" => state.handle_task_reconcile(peer, params)' in dispatch
    assert '"task.reconcile",' in dispatch
    assert '"task.reconcile",' in operation_store
    assert 'let method = "task.reconcile";' in query
    assert "OperationStore.dedupe" in query
    assert "OperationStore.record_result" in query
    # fix_defect：apply 必须经治理写门禁（adjudicator + reviewer lease）
    assert "validate_reviewer_lease_for_adjudication" in query
    assert "require_lease_params" in query
    assert "E_RECONCILE_ADJUDICATOR_ROLE_REQUIRED" in query


def test_reconciliation_requires_authority_and_preserves_step_history():
    # handler 在 task_collab_query.rs；helper SQL（tree/binding/capture）在 task_collab.rs
    query = _source(QUERY)
    collab = _source(TASK_COLLAB)
    start = query.index("pub fn handle_task_reconcile")
    end = query.index("pub fn handle_task_events", start)
    handler = query[start:end]

    assert "WITH RECURSIVE task_tree" in collab
    assert "task_workspace_bindings" in collab
    assert "workspace_authority_captures" in collab
    assert "instance != requested_instance" in collab
    assert "status = 'review'" in handler
    assert '"preserves_steps": true' in handler
    assert "UPDATE tasks SET status = 'in_progress'" in handler
    assert "UPDATE task_steps" not in handler
    assert "DELETE FROM task_steps" not in handler


def test_reconciliation_rejects_unsafe_apply_inputs():
    source = _source(QUERY)
    handler = source[source.index("pub fn handle_task_reconcile"):]

    assert "缺少 workspace_instance_id" in handler
    assert "apply 必须携带完整 identity" in handler
    assert "apply 必须携带 request_id" in handler
    assert "unchecked_transaction" in handler


# ============================================================
# 真实 daemon 负向 E2E（apply 缺 lease / token 错 / fencing 旧 / 身份不符）
# ============================================================


def _make_neg_task(rpc) -> str:
    tid = f"T-RECON-NEG-{uuid.uuid4().hex[:8]}"
    rpc.call("task.create", {
        "task_id": tid, "title": "reconcile negative", "description": "d",
        "steps": [{"action": "govern", "target_file": "neg.rs"}],
        "creator": "coordinator-workbuddy-v1",
        "role_contracts": [{"role": "executor"}, {"role": "reviewer"}, {"role": "adjudicator"}],
        "identity_policy": "legacy_identity_v1", "workspace_id": 1,
        "request_id": f"mk-{uuid.uuid4().hex[:8]}",
    })
    return tid


def _acquire_reviewer_lease(rpc, tid):
    lres = rpc.call("lease.acquire", {
        "task_id": tid, "role": "reviewer", "ttl_seconds": 3600,
        "identity": REVIEWER,
    })
    token = lres.get("token") or (lres.get("result", {}) or {}).get("token")
    fc = int(lres.get("fencing_counter") or (lres.get("result", {}) or {}).get("fencing_counter"))
    return token, fc


def _reconcile_apply(rpc, tid, identity, token=None, fc=0):
    params = {
        "root_task_id": tid, "workspace_instance_id": "ws-1",
        "apply": True, "request_id": f"rec-{uuid.uuid4().hex[:8]}",
        "identity": identity,
    }
    if token is not None:
        params["lease_token"] = token
        params["fencing_counter"] = fc
    return rpc.call("task.reconcile", params)


def test_reconcile_apply_requires_adjudicator_lease(rpc):
    """无 lease → E_LEASE_REQUIRED（任何写入前 fail-closed）。"""
    tid = _make_neg_task(rpc)
    try:
        _reconcile_apply(rpc, tid, ADJUDICATOR)
    except Exception as e:
        assert "E_LEASE_REQUIRED" in str(e), str(e)
    else:
        raise AssertionError("缺 lease 的 apply 必须被拒")


def test_reconcile_apply_rejects_wrong_token(rpc):
    """错误 token → E_LEASE_TOKEN_MISMATCH。"""
    tid = _make_neg_task(rpc)
    token, fc = _acquire_reviewer_lease(rpc, tid)
    try:
        _reconcile_apply(rpc, tid, ADJUDICATOR, token="wrong-token", fc=fc)
    except Exception as e:
        assert "E_LEASE_TOKEN_MISMATCH" in str(e), str(e)
    else:
        raise AssertionError("错误 token 必须被拒")


def test_reconcile_apply_rejects_stale_fencing(rpc):
    """fencing 陈旧 → E_LEASE_FENCING_STALE。"""
    tid = _make_neg_task(rpc)
    token, fc = _acquire_reviewer_lease(rpc, tid)
    try:
        _reconcile_apply(rpc, tid, ADJUDICATOR, token=token, fc=fc + 1)
    except Exception as e:
        assert "E_LEASE_FENCING_STALE" in str(e), str(e)
    else:
        raise AssertionError("陈旧 fencing 必须被拒")


def test_reconcile_apply_rejects_non_adjudicator(rpc):
    """非 adjudicator（implementer）→ E_RECONCILE_ADJUDICATOR_ROLE_REQUIRED。"""
    tid = _make_neg_task(rpc)
    token, fc = _acquire_reviewer_lease(rpc, tid)
    try:
        _reconcile_apply(rpc, tid, IMPLEMENTER, token=token, fc=fc)
    except Exception as e:
        assert "E_RECONCILE_ADJUDICATOR_ROLE_REQUIRED" in str(e), str(e)
    else:
        raise AssertionError("非 adjudicator 身份必须被拒")
