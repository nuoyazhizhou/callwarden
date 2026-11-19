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

import json
import shutil
import sqlite3
import tempfile
import uuid
from pathlib import Path

import pytest

from callwarden.config import get_pki_dir, resolve_client_cert_paths
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


@pytest.fixture(scope="module", autouse=True)
def cleanup_cascade_trees():
    """模块级清理：测试树（T-CSC-% 合成数据）跑完即删，避免污染权威库。

    此前调试轮次残留的测试树曾使"级联缺口"复现（root 卡 review），
    故测试必须自清理；清理顺序先子表后主表。
    """
    yield
    conn = sqlite3.connect(str(DB))
    # 存在性守卫：CI fresh runner 的库可能尚未建全部表（如 action_identities
    # 属较新 schema 版本），本地全迁移库则有——清理须容忍缺表，否则 teardown
    # 报 no such table 把已通过的测试染红。
    for t in ("action_identities", "task_events", "task_contract_revisions",
              "task_steps", "tasks"):
        col = "id" if t == "tasks" else "task_id"
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (t,)
        ).fetchone()
        if exists:
            conn.execute(f"DELETE FROM {t} WHERE {col} LIKE 'T-CSC-%'")
    conn.commit()
    conn.close()


def db_conn():
    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    return conn


def _client_principal_id() -> str:
    """解析当前 mTLS 客户端证书对应的 principal_id。

    resolve_client_cert_paths 按 ADR-002 §7 定位证书；本机 enroll 铸证的目录
    布局是 `identities/<principal_id>/client.crt`（目录名与证书 SAN 同名），
    故取证书路径父目录名即 principal_id。CW_CLIENT_CERT 直指定路径时该推导
    不成立，直接断言失败提示（本套件不覆盖该部署形态）。
    """
    cert, _key, _ca = resolve_client_cert_paths()
    assert cert, "无法定位 mTLS 客户端证书（cw pki init/enroll 未执行？）"
    name = Path(cert).parent.name
    assert name.startswith("pr_"), f"证书路径不含 identities/<principal>/ 布局: {cert}"
    return name


@pytest.fixture(scope="module")
def task_ws(rpc):
    """§9.39 归一：自建隔离 workspace，替代对共享 workspace 1 的归属假设。

    历史：本文件原先硬编码 workspace 1（root 恰为 pytest 临时目录）。
    §9.36 CA 轮换后 owner principal 变更，workspace 1-8 的 owner_key 归
    历史 principal → workspace.status 授权拒（E_PRINCIPAL_WORKSPACE_FORBIDDEN），
    runtime 段全红。「共享全局状态可用」不是环境前置，改为测试自建：

    1. `workspace.register` 注册唯一临时 root（当前 principal 名下）→
       权威 (workspace_id, workspace_instance_id)；
    2. dispatch 层 principal 白名单补 grant：向当前 principal 的磁盘
       `grants.json` 精确追加新 workspace（磁盘台账每次 authorize 实时读取，
       天然热更新）。不走 DB `workspace_principal_grants`：该列 INTEGER 亲和
       会把数字型 workspace_id 强转整数存储，Rust load_snapshot 以 String
       读取时 InvalidColumnType 静默丢行（§9.39 实测发现，fail-closed 方向
       的产品缺陷，另行立项）；
    3. task-DB 补种 workspaces 行（resolve_create_authority case 4 语义，
       同 §9.38 沙箱实测）；
    4. teardown 精确清理：grants.json 还原为测试前字节 + task-DB
       workspaces 行 + registry.db daemon_workspaces 行（任务行由既有
       T-CSC-% 清理 fixture 处理）。
    """
    root = Path(tempfile.mkdtemp(prefix="cw-task-ws-"))
    name = f"ws-csc-{uuid.uuid4().hex[:8]}"
    reg = rpc.call("workspace.register", {
        "name": name,
        "client_view_root": str(root),
        "description": "test_task_cascade_close 隔离 workspace",
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


def make_tree(rpc, depth: int, instance: str, workspace_id: int,
              with_policy: bool = True) -> list[str]:
    """构造 depth+1 层的任务树，返回 [leaf, mid, root]。

    创建顺序 = 先根后叶（i=0 为根，无 parent；i=depth 为叶子，parent 指向
    上一层）。直写状态（测试树为合成数据，直写模拟"已闭环/树干卡住"的
    存量事实，避开 reviewer lease 流程）：
    - 叶子（i == depth，最后创建）→ steps done + task closed（"已闭环"）
    - 聚合节点（i < depth）→ task review（"树干卡住"）
    返回序与创建序相反：list(reversed(ids)) = [leaf, ..., mid, root]。

    fixture 适配说明（2026-08-29 rev 修复后实跑修正）：
    - v59 治理硬化后 task.create 强制要求 identity_policy（缺则
      E_TASK_IDENTITY_POLICY_REQUIRED），故全部带 policy 创建；
      autofill 场景由测试对存量 revision 删字段模拟，不走 with_policy=False。
    - task.create 默认状态为 open、步骤为 pending；cascade-close 对叶子
      要求步骤全 done，故叶子闭环必须显式直写，不能靠创建即 closed。
    - cascade-close 语义 = 从指定节点**向上**聚合收尾（chain=[task,parent,root]），
      不自上而下处理后代；故测试从 mid（中层）触发，由实现向上关闭 root。
    """
    ids = []
    for i in range(depth + 1):
        tid = f"T-CSC-{uuid.uuid4().hex[:10]}-{i}"
        ids.append(tid)
        steps = [{"action": "govern", "target_file": f"file_{i}.rs"}]
        parent = ids[-2] if len(ids) >= 2 else None
        params = {
            "task_id": tid, "title": f"cascade test {i}", "description": "d",
            "steps": steps,
            "role_contracts": [{"role": "executor"}, {"role": "reviewer"}, {"role": "adjudicator"}],
            "identity_policy": "legacy_identity_v1",
            "workspace_id": workspace_id,
            "workspace_instance_id": instance,
            "request_id": f"mk-{uuid.uuid4().hex[:8]}",
        }
        if parent:
            params["parent_id"] = parent
        rpc.call("task.create", params)
        conn = db_conn()
        if i == depth:
            # 叶子（最后创建）：真实闭环需 reviewer lease，DB 直写模拟"已闭环"事实
            conn.execute(
                "UPDATE task_steps SET status='done', completed_at=strftime('%s','now') "
                "WHERE task_id=?", (tid,))
            conn.execute(
                "UPDATE tasks SET status='closed', closed_at=strftime('%s','now'), "
                "updated_at=strftime('%s','now') WHERE id=?", (tid,))
        else:
            # 聚合节点（根/中间层）：模拟"树干卡住"
            conn.execute(
                "UPDATE tasks SET status='review', updated_at=strftime('%s','now') "
                "WHERE id=?", (tid,))
        conn.commit()
        conn.close()
    return list(reversed(ids))  # [leaf, mid, root]（创建序先根后叶，返回序反转为叶在前）


# ============================================================
# 1) success：子树全 closed → 父/根级联 close
# ============================================================


def test_success_cascade_closes_ancestors(rpc, task_ws):
    """叶子 closed 后 cascade-close(mid) → mid + root 自动 closed（自底向上）。"""
    wid, inst = task_ws
    # 构造 3 层：leaf(done, closed) -> mid(review) -> root(review)
    ids = make_tree(rpc, depth=2, instance=inst, workspace_id=wid)
    leaf, mid, root = ids[0], ids[1], ids[2]

    # 前置：leaf 已闭环（直写 closed），mid/root 卡在 review
    for tid, expect in ((leaf, "closed"), (mid, "review"), (root, "review")):
        conn = db_conn()
        st = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
        conn.close()
        assert st["status"] == expect, f"{tid} 应为 {expect}，实际 {st['status']}"

    # cascade-close(mid)：mid 子 leaf 已 closed → mid 聚合 close；
    # 向上 root 子 mid 已 closed → root 聚合 close（chain=[mid,root]）
    res = rpc.call(CASCADE, {
        "task_id": mid,
        "identity": {"agent_id": "coordinator-workbuddy-v1",
                     "session_id": "sess-coord-wb-20260820",
                     "model_id": "workbuddy", "role": "coordinator"},
        "request_id": f"cs-{uuid.uuid4().hex[:8]}",
        "workspace_id": wid,
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


def test_identity_policy_autofill(rpc, task_ws):
    """聚合节点缺 identity_policy → cascade 自动追加 revision（legacy 默认）。"""
    wid, inst = task_ws
    # v59 治理硬化后 task.create 强制要求 identity_policy（创建即拒绝缺 policy），
    # autofill 只对存量历史任务有意义：先带 policy 建树，再从最新 revision
    # payload 删除 identity_policy 字段模拟存量缺 policy。
    ids = make_tree(rpc, depth=1, instance=inst, workspace_id=wid)
    leaf, root = ids[0], ids[1]

    # 构造前置：删除 root 最新 revision 中的 identity_policy
    conn = db_conn()
    row = conn.execute(
        "SELECT revision, envelope_payload FROM task_contract_revisions WHERE task_id=? "
        "ORDER BY revision DESC LIMIT 1", (root,)).fetchone()
    payload = json.loads(row["envelope_payload"])
    assert "identity_policy" in payload, "前置应有 policy"
    del payload["identity_policy"]
    conn.execute(
        "UPDATE task_contract_revisions SET envelope_payload=? WHERE task_id=? AND revision=?",
        (json.dumps(payload, ensure_ascii=False), root, row["revision"]))
    conn.commit()
    conn.close()

    # 前置校验：当前 revision 已无 policy
    conn = db_conn()
    row = conn.execute(
        "SELECT envelope_payload FROM task_contract_revisions WHERE task_id=? "
        "ORDER BY revision DESC LIMIT 1", (root,)).fetchone()
    conn.close()
    assert "identity_policy" not in (row["envelope_payload"] or ""), "前置应缺 policy"

    res = rpc.call(CASCADE, {
        "task_id": leaf,
        "identity": {"agent_id": "coordinator-workbuddy-v1",
                     "session_id": "sess-coord-wb-20260820",
                     "model_id": "workbuddy", "role": "coordinator"},
        "request_id": f"cs-{uuid.uuid4().hex[:8]}",
        "workspace_id": wid,
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


def test_not_all_closed_reject(rpc, task_ws):
    """子树仍有 open 节点 → cascade 不关闭任何祖先（stop）。"""
    wid, inst = task_ws
    ids = make_tree(rpc, depth=1, instance=inst, workspace_id=wid)
    _leaf, root = ids[0], ids[1]

    # 打开一个子任务（模拟未完成）：给 leaf 挂一个 open 兄弟
    open_tid = f"T-CSC-{uuid.uuid4().hex[:10]}-open"
    rpc.call("task.create", {
        "task_id": open_tid, "title": "open sibling", "description": "d",
        "steps": [{"action": "govern", "target_file": "open.rs"}],
        "role_contracts": [{"role": "executor"}, {"role": "reviewer"}, {"role": "adjudicator"}],
        "identity_policy": "legacy_identity_v1",
        "parent_id": root, "workspace_id": wid,
        "workspace_instance_id": inst,
        "request_id": f"mk-{uuid.uuid4().hex[:8]}",
    })

    res = rpc.call(CASCADE, {
        "task_id": root,
        "identity": {"agent_id": "coordinator-workbuddy-v1",
                     "session_id": "sess-coord-wb-20260820",
                     "model_id": "workbuddy", "role": "coordinator"},
        "request_id": f"cs-{uuid.uuid4().hex[:8]}",
        "workspace_id": wid,
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


def test_idempotent(rpc, task_ws):
    """重复 cascade-close 幂等：已 closed 节点跳过，不重复写。"""
    wid, inst = task_ws
    ids = make_tree(rpc, depth=1, instance=inst, workspace_id=wid)
    _leaf, root = ids[0], ids[1]

    params = {
        "task_id": root,
        "identity": {"agent_id": "coordinator-workbuddy-v1",
                     "session_id": "sess-coord-wb-20260820",
                     "model_id": "workbuddy", "role": "coordinator"},
        "workspace_id": wid,
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
