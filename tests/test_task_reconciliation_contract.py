"""历史任务 reconciliation daemon 路由的回归约束（fix_defect 修复版）。

覆盖 T-1787823611412-2f503878 #5 fix_defect 的 reviewer findings：
1. apply 路径必须要求 adjudicator 身份 + 独立 reviewer lease（token/fencing/holder），
   缺失/错误/陈旧/身份不符一律 fail-closed，拒绝路径无状态变化；
2. handler 位于 task_collab_query.rs（原断言错误指向 task_collab.rs）；
3. 真实 daemon 负向 E2E（daemon 不可用时 runtime 段 skip，静态门禁仍执行）。
"""

import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from callwarden.config import get_pki_dir, resolve_client_cert_paths
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
    # tasks 主表删除同样需容忍缺表：CI fresh runner 上 runtime 段整体 skip
    # 时库可能从未建 schema（无 daemon 交互），teardown 直接 DELETE 会以
    # no such table 把已通过/已跳过的测试染红（同 cascade_close 族）。
    try:
        conn.execute("DELETE FROM tasks WHERE id LIKE 'T-RECON-NEG-%'")
    except Exception:
        pass
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


def _client_principal_id() -> str:
    """解析当前 mTLS 客户端证书对应的 principal_id（identities/<principal>/ 布局）。"""
    cert, _key, _ca = resolve_client_cert_paths()
    assert cert, "无法定位 mTLS 客户端证书（cw pki init/enroll 未执行？）"
    name = Path(cert).parent.name
    assert name.startswith("pr_"), f"证书路径不含 identities/<principal>/ 布局: {cert}"
    return name


@pytest.fixture(scope="module")
def task_ws(rpc):
    """§9.39 归一：自建隔离 workspace，替代对共享 workspace 1 的归属假设。

    §9.36 CA 轮换后 owner principal 变更，workspace 1-8 的 owner_key 归历史
    principal → workspace.status 授权拒（E_PRINCIPAL_WORKSPACE_FORBIDDEN）。
    改为测试自建：workspace.register 注册唯一临时 root → 权威
    (workspace_id, workspace_instance_id)；dispatch 层 principal 白名单补
    grant（磁盘 grants.json 精确追加，每次 authorize 实时读取热更新；不走
    DB workspace_principal_grants——INTEGER 亲和会静默丢行，§9.39 实测发现，
    另行立项）；task-DB 补种 workspaces 行（case 4 语义）；teardown 精确
    清理 grants.json 还原 + 两库行 + 临时目录（任务行由既有 T-RECON-NEG-%
    清理 fixture 处理）。
    """
    root = Path(tempfile.mkdtemp(prefix="cw-recon-ws-"))
    name = f"ws-recon-{uuid.uuid4().hex[:8]}"
    reg = rpc.call("workspace.register", {
        "name": name,
        "client_view_root": str(root),
        "description": "test_task_reconciliation_contract 隔离 workspace",
    })
    wid = reg.get("workspace_id") or (reg.get("result") or {}).get("workspace_id")
    inst = (
        reg.get("workspace_instance_id")
        or (reg.get("result") or {}).get("workspace_instance_id")
    )
    assert wid and inst, f"workspace.register 返回缺 id/instance: {reg}"
    principal = _client_principal_id()
    grants_path = Path(get_pki_dir()) / "identities" / principal / "grants.json"
    grants_before = (
        grants_path.read_text(encoding="utf-8") if grants_path.is_file() else None
    )
    doc = json.loads(grants_before) if grants_before is not None else {}
    ids = doc.setdefault("workspace_ids", [])
    if str(wid) not in ids:
        ids.append(str(wid))
    grants_path.parent.mkdir(parents=True, exist_ok=True)
    grants_path.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    conn = sqlite3.connect(str(DB))
    conn.execute(
        "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at) "
        "VALUES (?, ?, ?, strftime('%s','now'))",
        (wid, name, str(root).replace("\\", "/")),
    )
    conn.commit()
    conn.close()
    yield int(wid), str(inst)
    # teardown：grants.json 还原为测试前状态（不存在过则删除）
    if grants_before is None:
        grants_path.unlink(missing_ok=True)
    else:
        grants_path.write_text(grants_before, encoding="utf-8")
    conn = sqlite3.connect(str(DB))
    conn.execute("DELETE FROM workspaces WHERE id=?", (int(wid),))
    conn.commit()
    conn.close()
    reg_db = Path.home() / ".callwarden" / "registry.db"
    if reg_db.exists():
        conn = sqlite3.connect(str(reg_db))
        conn.execute(
            "DELETE FROM daemon_workspaces WHERE workspace_instance_id=?", (str(inst),)
        )
        conn.commit()
        conn.close()
    shutil.rmtree(root, ignore_errors=True)


def _make_neg_task(rpc, wid: int, inst: str) -> tuple[str, str]:
    # stale 修正（step6 复核，2026-09-20）：daemon 现强制 task.create 显式传非空
    # workspace_instance_id，禁止空实例合成 ws-{id}
    # （E_TASK_WORKSPACE_INSTANCE_REQUIRED；权威 rust_ext/src/daemon/task_loop/create.rs
    # 的 fail-closed 校验）。§9.39 归一后 (wid, inst) 由 task_ws fixture 自建
    # 隔离 workspace 提供，不再依赖 workspace.status(workspace_id=1) 的归属假设。
    tid = f"T-RECON-NEG-{uuid.uuid4().hex[:8]}"
    rpc.call("task.create", {
        "task_id": tid, "title": "reconcile negative", "description": "d",
        "steps": [{"action": "govern", "target_file": "neg.rs"}],
        "creator": "coordinator-workbuddy-v1",
        "role_contracts": [{"role": "executor"}, {"role": "reviewer"}, {"role": "adjudicator"}],
        "identity_policy": "legacy_identity_v1", "workspace_id": wid,
        "workspace_instance_id": inst,
        "request_id": f"mk-{uuid.uuid4().hex[:8]}",
    })
    return tid, inst


def _acquire_reviewer_lease(rpc, tid):
    lres = rpc.call("lease.acquire", {
        "task_id": tid, "role": "reviewer", "ttl_seconds": 3600,
        "identity": REVIEWER,
    })
    token = lres.get("token") or (lres.get("result", {}) or {}).get("token")
    fc = int(lres.get("fencing_counter") or (lres.get("result", {}) or {}).get("fencing_counter"))
    return token, fc


def _reconcile_apply(rpc, tid, instance, identity, token=None, fc=0):
    params = {
        "root_task_id": tid, "workspace_instance_id": instance,
        "apply": True, "request_id": f"rec-{uuid.uuid4().hex[:8]}",
        "identity": identity,
    }
    if token is not None:
        params["lease_token"] = token
        params["fencing_counter"] = fc
    return rpc.call("task.reconcile", params)


def test_reconcile_apply_requires_adjudicator_lease(rpc, task_ws):
    """无 lease → E_LEASE_REQUIRED（任何写入前 fail-closed）。"""
    wid, inst = task_ws
    tid, inst = _make_neg_task(rpc, wid, inst)
    try:
        _reconcile_apply(rpc, tid, inst, ADJUDICATOR)
    except Exception as e:
        assert "E_LEASE_REQUIRED" in str(e), str(e)
    else:
        raise AssertionError("缺 lease 的 apply 必须被拒")


def test_reconcile_apply_rejects_wrong_token(rpc, task_ws):
    """错误 token → E_LEASE_TOKEN_MISMATCH。"""
    wid, inst = task_ws
    tid, inst = _make_neg_task(rpc, wid, inst)
    token, fc = _acquire_reviewer_lease(rpc, tid)
    try:
        _reconcile_apply(rpc, tid, inst, ADJUDICATOR, token="wrong-token", fc=fc)
    except Exception as e:
        assert "E_LEASE_TOKEN_MISMATCH" in str(e), str(e)
    else:
        raise AssertionError("错误 token 必须被拒")


def test_reconcile_apply_rejects_stale_fencing(rpc, task_ws):
    """fencing 陈旧 → E_LEASE_FENCING_STALE。"""
    wid, inst = task_ws
    tid, inst = _make_neg_task(rpc, wid, inst)
    token, fc = _acquire_reviewer_lease(rpc, tid)
    try:
        _reconcile_apply(rpc, tid, inst, ADJUDICATOR, token=token, fc=fc + 1)
    except Exception as e:
        assert "E_LEASE_FENCING_STALE" in str(e), str(e)
    else:
        raise AssertionError("陈旧 fencing 必须被拒")


def test_reconcile_apply_rejects_non_adjudicator(rpc, task_ws):
    """非 adjudicator（implementer）→ E_RECONCILE_ADJUDICATOR_ROLE_REQUIRED。"""
    wid, inst = task_ws
    tid, inst = _make_neg_task(rpc, wid, inst)
    token, fc = _acquire_reviewer_lease(rpc, tid)
    try:
        _reconcile_apply(rpc, tid, inst, IMPLEMENTER, token=token, fc=fc)
    except Exception as e:
        assert "E_RECONCILE_ADJUDICATOR_ROLE_REQUIRED" in str(e), str(e)
    else:
        raise AssertionError("非 adjudicator 身份必须被拒")
