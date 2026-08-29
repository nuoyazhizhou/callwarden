"""task.cascade_close 迁移验收：聚合节点级联收尾。

覆盖 task `T-1787973726845-a85ebacc` step[2] fixture_matrix：
["success", "identity_policy_autofill", "not_all_closed_reject", "idempotent"]。

设计要点（树干=纯聚合投影，审计点只在叶子）：
- 架构定调（2026-08-29）：功能都在叶子/枝条上，树干不应有独立审计点；
  子树全 closed 即树干 closed。`task.cascade_close` 由 coordinator 调用，
  自底向上递归：子全 closed + 叶子步骤 done → 节点聚合收尾；缺
  identity_policy 自动追加 revision；系统权威 close（cascade_closed）。
- 真实 daemon 集成（HttpDaemonRpcClient），daemon 不可用时 runtime 段 skip，
  静态门禁（dispatch 路由 / methods 表 / CLI 命令）仍执行。
"""

import sqlite3
import uuid
from pathlib import Path

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient

ROOT = Path(__file__).resolve().parent.parent
DB = Path.home() / ".callwarden" / "callwarden.db"

CASCADE = "task.cascade_close"


@pytest.fixture(scope="module")
def rpc():
    """真实 daemon RPC 接缝；daemon 不可用时 runtime 段整体 skip。"""
    c = HttpDaemonRpcClient()
    try:
        c.call("ping", {})
    except Exception:
        pytest.skip("daemon 不可用：runtime 段跳过（静态门禁仍执行）")
    return c


def db_conn():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    return conn


def make_tree(rpc, depth: int, with_policy: bool = True) -> list[str]:
    """构造 depth+1 层的任务树（root + 子 + 孙...），返回 [leaf, mid, root]。

    每层一个任务：叶子带 1 个 done 步骤；聚合节点带 1 个 done 步骤。
    全部任务先创建为 closed（模拟"叶子已闭环"），聚合节点创建为 review
    态（模拟"树干卡住"）。返回 [leaf_id, mid_id, root_id]。
    """
    ids = []
    cur = None
    for i in range(depth + 1):
        tid = f"T-CSC-{uuid.uuid4().hex[:10]}-{i}"
        ids.append(tid)
        steps = [{"action": "govern", "target_file": f"file_{i}.rs"}]
        parent = ids[-2] if len(ids) >= 2 else None
        params = {
            "task_id": tid, "title": f"cascade test {i}", "description": "d",
            "steps": steps, "creator": "cascade-test",
            "role_contracts": [{"role": "executor"}, {"role": "reviewer"}, {"role": "adjudicator"}],
            "identity_policy": "legacy_identity_v1" if with_policy else None,
            "workspace_id": 1,
            "request_id": f"mk-{uuid.uuid4().hex[:8]}",
        }
        if parent:
            params["parent_id"] = parent
        rpc.call("task.create", params)
        # 叶子直接 closed（模拟已闭环）；聚合节点保持 review（卡住）
        if i == 0:
            rpc.call("task.close", {
                "task_id": tid,
                "identity": {"agent_id": "adjudicator-wb-186loop",
                             "session_id": "sess-adjudicator-wb-186loop",
                             "model_id": "deepseek-v4-flash", "role": "adjudicator",
                             "agent_instance_id": "inst-adjudicator-wb-186loop"},
                "lease_token": "x", "fencing_counter": 0,
                "request_id": f"cl-{uuid.uuid4().hex[:8]}", "workspace_id": 1,
            })
    return list(reversed(ids))  # [root, mid, leaf] 顺序返回时调用方自行处理


# ============================================================
# 1) success：子树全 closed → 父/根级联 close
# ============================================================


def test_success_cascade_closes_ancestors(rpc):
    """叶子 closed 后 cascade-close(root) → mid + root 自动 closed。"""
    # 构造 3 层：leaf(done, closed) -> mid(review) -> root(review)
    ids = make_tree(rpc, depth=2)
    leaf, mid, root = ids[0], ids[1], ids[2]

    # 先确保 leaf 已 closed（make_tree 里做了），mid/root 处于 review
    conn = db_conn()
    for tid, expect in ((leaf, "closed"), (mid, "review"), (root, "review")):
        st = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert st["status"] == expect, f"{tid} 应为 {expect}，实际 {st['status']}"

    # cascade-close(root)：leaf 已 closed → mid 应聚合 close → root 应聚合 close
    res = rpc.call(CASCADE, {
        "task_id": root,
        "identity": {"agent_id": "coordinator-workbuddy-v1",
                     "session_id": "sess-coord-wb-20260820",
                     "model_id": "workbuddy", "role": "coordinator"},
        "request_id": f"cs-{uuid.uuid4().hex[:8]}",
        "workspace_id": 1,
    })
    closed = res.get("closed", [])
    assert root in closed, f"root 应被级联关闭: {closed}"
    assert mid in closed, f"mid 应被级联关闭: {closed}"

    conn = db_conn()
    for tid in (mid, root):
        st = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        assert st["status"] == "closed", f"{tid} 未 closed"
    conn.close()


# ============================================================
# 2) identity_policy autofill：缺 policy 的聚合节点自动补
# ============================================================


def test_identity_policy_autofill(rpc):
    """聚合节点缺 identity_policy → cascade 自动追加 revision（legacy 默认）。"""
    ids = make_tree(rpc, depth=1, with_policy=False)
    leaf, root = ids[0], ids[1]

    # 构造后 root 缺 policy（make_tree 传 None）
    conn = db_conn()
    row = conn.execute(
        "SELECT envelope_payload FROM task_contract_revisions WHERE task_id=? "
        "ORDER BY revision DESC LIMIT 1", (root,)).fetchone()
    conn.close()
    assert "identity_policy" not in (row["envelope_payload"] or ""), "前置应缺 policy"

    res = rpc.call(CASCADE, {
        "task_id": root,
        "identity": {"agent_id": "coordinator-workbuddy-v1",
                     "session_id": "sess-coord-wb-20260820",
                     "model_id": "workbuddy", "role": "coordinator"},
        "request_id": f"cs-{uuid.uuid4().hex[:8]}",
        "workspace_id": 1,
    })
    assert root in res.get("closed", []), f"root 应关闭: {res}"

    # 自动补 policy：最新 revision 应含 identity_policy
    conn = db_conn()
    row = conn.execute(
        "SELECT envelope_payload FROM task_contract_revisions WHERE task_id=? "
        "ORDER BY revision DESC LIMIT 1", (root,)).fetchone()
    conn.close()
    assert "identity_policy" in (row["envelope_payload"] or ""), "应自动补 policy"
    assert "legacy_identity_v1" in (row["envelope_payload"] or "")


# ============================================================
# 3) not all closed：子树未全 closed → 拒绝级联（不关闭任何祖先）
# ============================================================


def test_not_all_closed_reject(rpc):
    """子树仍有 open 节点 → cascade 不关闭任何祖先（stop）。"""
    ids = make_tree(rpc, depth=1, with_policy=True)
    leaf, root = ids[0], ids[1]

    # 打开一个子任务（模拟未完成）：给 leaf 挂一个 open 兄弟
    open_tid = f"T-CSC-{uuid.uuid4().hex[:10]}-open"
    rpc.call("task.create", {
        "task_id": open_tid, "title": "open sibling", "description": "d",
        "steps": [{"action": "govern", "target_file": "open.rs"}],
        "creator": "cascade-test",
        "role_contracts": [{"role": "executor"}, {"role": "reviewer"}, {"role": "adjudicator"}],
        "identity_policy": "legacy_identity_v1",
        "parent_id": root, "workspace_id": 1,
        "request_id": f"mk-{uuid.uuid4().hex[:8]}",
    })

    res = rpc.call(CASCADE, {
        "task_id": root,
        "identity": {"agent_id": "coordinator-workbuddy-v1",
                     "session_id": "sess-coord-wb-20260820",
                     "model_id": "workbuddy", "role": "coordinator"},
        "request_id": f"cs-{uuid.uuid4().hex[:8]}",
        "workspace_id": 1,
    })
    # root 有 open 子任务 → 不关闭 root
    assert root not in res.get("closed", []), f"root 不应关闭（有 open 子任务）: {res}"
    conn = db_conn()
    st = conn.execute("SELECT status FROM tasks WHERE id=?", (root,)).fetchone()
    conn.close()
    assert st["status"] != "closed", "root 不应被关闭"


# ============================================================
# 4) idempotent：已 closed 节点跳过，重复调用稳定
# ============================================================


def test_idempotent(rpc):
    """重复 cascade-close 幂等：已 closed 节点跳过，不重复写。"""
    ids = make_tree(rpc, depth=1, with_policy=True)
    leaf, root = ids[0], ids[1]

    params = {
        "task_id": root,
        "identity": {"agent_id": "coordinator-workbuddy-v1",
                     "session_id": "sess-coord-wb-20260820",
                     "model_id": "workbuddy", "role": "coordinator"},
        "workspace_id": 1,
    }
    params["request_id"] = f"cs-{uuid.uuid4().hex[:8]}"
    r1 = rpc.call(CASCADE, params)
    params["request_id"] = f"cs-{uuid.uuid4().hex[:8]}"
    r2 = rpc.call(CASCADE, params)

    # 第一次关闭 root；第二次 root 已 closed → skipped
    assert root in r1.get("closed", [])
    assert root in r2.get("skipped", []) or root not in r2.get("closed", [])
    conn = db_conn()
    st = conn.execute("SELECT status FROM tasks WHERE id=?", (root,)).fetchone()
    conn.close()
    assert st["status"] == "closed"


# ============================================================
# 静态门禁：dispatch 路由 / methods 表 / CLI 命令
# ============================================================


def test_authority_dispatch_wired():
    src = (ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8")
    assert '"task.cascade_close" => state.handle_task_cascade_close(peer, params)' in src
    assert '"task.cascade_close",' in src  # CONVERGENCE_RPC_METHODS


def test_authority_handler_exists():
    src = (ROOT / "rust_ext" / "src" / "daemon"
           / "task_collab_lifecycle_apply.rs").read_text(encoding="utf-8")
    assert "pub fn handle_task_cascade_close" in src
    assert "cascade_closed" in src  # reason_code


def test_authority_cli_wired():
    src = (ROOT / "cli" / "main.py").read_text(encoding="utf-8")
    assert '"cascade-close",' in src
    assert "cascade_p = sub.add_parser" in src
    assert '"task.cascade_close"' in src
