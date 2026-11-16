"""task.split 原子治理初始化 fixture 矩阵测试（T-1787963386217-0ae6d628）。

覆盖 task step `fixture_matrix`（target_file: tests/test_task_split_governance.py）
要求的矩阵：
  success  —— 有绑定父任务 split → 子任务原子拥有 workspace binding + 3×Role
              Contract + Task Contract（identity_policy）+ step binding，可 claim；
  rollback —— 父任务无 workspace binding（不存在）→
              E_TASK_BINDING_ATTESTATION_REQUIRED（dispatch.rs legacy binding 门），
              且不留下任何半成品子任务；
  no-bypass —— 子任务治理完整性逐项断言（不允许"有任务有步骤但无合同"残缺态），
              split event 记录真实 workspace_id（非空串）。

依赖：模块级隔离 daemon（`_w3_harness.setup_w3_client`，USERPROFILE / registry /
task-DB 全部重定向到 tmp 目录）经 HttpDaemonRpcClient；断言只读该隔离 task-DB。

隔离而非复用常驻 daemon 的原因：本文件用例会在权威库真实建卡（父任务 + split
子任务），跑在常驻 daemon 上会永久残留 open 卡（历史实测遗留 71 张）。隔离栈在
fixture teardown kill 进程并随 tmp 目录销毁，真实 `~/.callwarden/callwarden.db`
零写入。
"""

import os
import sqlite3
import uuid

import pytest

from callwarden.server.daemon_protocol import DaemonRemoteError
from _w3_harness import restore_w3_env, setup_w3_client

_REPO_ROOT = r"C:\git_work\callwarden"
# 由 `client` fixture 在模块级填充：隔离 task-DB 路径与权威 workspace 配对。
_TASK_DB = ""
_WS_PAIR = (0, "")
IDENTITY = {
    "agent_id": "executor-workbuddy-v1-cur",
    "session_id": "sess-workbuddy-cw-20260822-0320",
    "model_id": "deepseek-v4-flash",
    "role": "executor",
}


def _db():
    conn = sqlite3.connect(_TASK_DB)
    conn.row_factory = sqlite3.Row
    return conn


def _seed_capture_rule(task_db: str) -> None:
    """seed `workspace_capture` c14n rule row（task.create 的 capability 前置）。

    隔离 task-DB 由 `setup_w3_client` 建库但不含该 rule row，缺失时 `task.create`
    以 E_WORKSPACE_AUTHORITY_MISMATCH（capability 未就绪）fail-closed。这里只补
    rule row、不预置 capture：首条 create 由 daemon 自行追加 rev1 capture，后续
    create 复用同一 registry identity，避免手工 hash 与 daemon 重算漂移。
    """
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO canonicalization_rule_sets "
            "(domain, canonicalization_version, rules_hash) "
            "VALUES ('workspace_capture', 'workspace-capture-c14n/v1', 'cli-test-rules-hash')"
        )
        conn.commit()
    finally:
        conn.close()


def _create_parent(client, steps=None):
    """经 daemon 创建带 workspace binding 的父任务。"""
    tid = "T-SPLIT-" + uuid.uuid4().hex[:10]
    ws_id, ws_instance_id = _WS_PAIR
    steps = steps or [
        {"action": "port_rust_authority", "target_file": "rust_ext/src/daemon/task_collab_planning.rs"},
    ]
    _r = client.call("task.create", {
        "task_id": tid, "title": "split 父任务", "description": "test",
        "steps": steps, "creator": "test",
        "role_contracts": [{"role": "executor"}, {"role": "reviewer"}, {"role": "adjudicator"}],
        "identity_policy": "legacy_identity_v1",
        "workspace_id": ws_id, "workspace_instance_id": ws_instance_id,
        "request_id": f"mk-{tid}-{uuid.uuid4().hex[:8]}",
    })
    return tid


def _subtask_defs():
    return [
        {"title": "子任务 A", "description": "a", "steps": [
            {"action": "port_rust_authority", "target_file": "a.rs"},
            {"action": "thin_cli_client", "target_file": "b.py"},
        ]},
        {"title": "子任务 B", "description": "b", "steps": [
            {"action": "fixture_matrix", "target_file": "tests/test_x.py"},
        ]},
    ]


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    # 隔离栈不可用（无 cw-daemon 二进制 / 启动失败）时 runtime 段整体 skip：
    # CI fresh runner 无常驻 daemon，直接连 endpoint 会以不可连接染红。
    data_root = str(tmp_path_factory.mktemp("task_split_"))
    c, inst, _endpoint, proc = setup_w3_client(data_root, _REPO_ROOT, ws_id=1)
    if proc is None:
        pytest.skip("隔离 cw-daemon 不可用：runtime 段跳过（静态门禁仍执行）")
    global _TASK_DB, _WS_PAIR
    _TASK_DB = os.path.join(data_root, "task.db")
    _WS_PAIR = (1, inst)
    _seed_capture_rule(_TASK_DB)
    try:
        yield c
    finally:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        restore_w3_env()


def test_split_success_full_governance(client):
    """success：子任务原子拥有 binding + 3×Role Contract + Task Contract(identity_policy) + step binding。"""
    parent = _create_parent(client)
    r = client.call("task.split", {
        "task_id": parent, "subtasks": _subtask_defs(),
        "identity_policy": "legacy_identity_v1",
        "request_id": f"sp-{parent}-{uuid.uuid4().hex[:8]}",
    })
    assert r.get("subtask_count") == 2
    subs = r["subtasks"]
    assert len(subs) == 2

    conn = _db()
    for sid in subs:
        # 1) workspace binding 继承
        n = conn.execute(
            "SELECT COUNT(*) FROM task_workspace_bindings WHERE task_id=?", (sid,)
        ).fetchone()[0]
        assert n == 1, f"{sid} 缺 workspace binding"
        # 2) 三角色 Role Contract
        n = conn.execute(
            "SELECT COUNT(*) FROM role_contracts WHERE task_id=?", (sid,)
        ).fetchone()[0]
        assert n == 3, f"{sid} 缺三角色 Role Contract (got {n})"
        # 3) Task Contract + identity_policy
        row = conn.execute(
            "SELECT envelope_payload FROM task_contract_revisions WHERE task_id=? ORDER BY revision DESC LIMIT 1",
            (sid,),
        ).fetchone()
        assert row is not None, f"{sid} 缺 Task Contract"
        import json as _json
        env = _json.loads(row[0])
        assert env.get("identity_policy") == "legacy_identity_v1", f"{sid} identity_policy 缺失"
        # 4) step binding == 步骤数
        n_steps = conn.execute(
            "SELECT COUNT(*) FROM task_steps WHERE task_id=?", (sid,)
        ).fetchone()[0]
        n_bind = conn.execute(
            "SELECT COUNT(*) FROM task_step_role_contract_bindings WHERE task_id=?", (sid,)
        ).fetchone()[0]
        assert n_bind == n_steps, f"{sid} step binding {n_bind} != steps {n_steps}"
    conn.close()


def test_split_rollback_on_unbound_parent(client):
    """rollback：父任务无 workspace binding → E_TASK_BINDING_ATTESTATION_REQUIRED，且无半成品子任务。

    错误码由更早的 E_TASK_BINDING_REQUIRED 演进为更明确的
    E_TASK_BINDING_ATTESTATION_REQUIRED（dispatch.rs 的 legacy workspace binding
    门，提示先执行 ``cw task attest-legacy-workspace-binding``）；拒绝语义不变。
    """
    ghost = "T-NOEXIST-" + uuid.uuid4().hex[:10]
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("task.split", {
            "task_id": ghost, "subtasks": _subtask_defs(),
            "request_id": f"sp-{ghost}-{uuid.uuid4().hex[:8]}",
        })
    assert "E_TASK_BINDING_ATTESTATION_REQUIRED" in str(ei.value), str(ei.value)
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_id=?", (ghost,)
    ).fetchone()[0]
    assert n == 0, f"幽灵父任务不应产生子任务 (got {n})"
    conn.close()


def test_split_no_bypass_no_partial_governance(client):
    """no-bypass：子任务不允许残缺治理；split event 记录真实 workspace_id（非空串）。"""
    parent = _create_parent(client)
    r = client.call("task.split", {
        "task_id": parent, "subtasks": _subtask_defs(),
        "identity_policy": "legacy_identity_v1",
        "request_id": f"sp-{parent}-{uuid.uuid4().hex[:8]}",
    })
    subs = r["subtasks"]
    conn = _db()
    for sid in subs:
        # 治理三要素缺一即失败：binding / role_contracts / task_contract 必须同时存在
        missing = 0
        for tbl in ("task_workspace_bindings", "role_contracts", "task_contract_revisions"):
            n = conn.execute(
                f"SELECT COUNT(*) FROM {tbl} WHERE task_id=?", (sid,)
            ).fetchone()[0]
            if n == 0:
                missing += 1
        assert missing == 0, f"{sid} 存在治理残缺（缺 {missing} 类治理事实）"
    # split event 的 workspace_id 必须是真实值（非空串，修复前为 ''）
    ev = conn.execute(
        "SELECT workspace_id FROM task_events WHERE task_id=? AND reason_code='split' ORDER BY monotonic_seq DESC LIMIT 1",
        (parent,),
    ).fetchone()
    assert ev is not None and ev[0] not in ("", None), "split event workspace_id 为空串（修复前缺陷）"
    conn.close()


def test_split_rejects_empty_title(client):
    """fail-closed：空 title 的子任务被拒绝，且不留任何半成品子任务。"""
    parent = _create_parent(client)
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("task.split", {
            "task_id": parent,
            "subtasks": [
                {"title": "", "description": "d", "steps": [
                    {"action": "port_rust_authority", "target_file": "a.rs"}]},
            ],
            "identity_policy": "legacy_identity_v1",
            "request_id": f"sp-{parent}-{uuid.uuid4().hex[:8]}",
        })
    assert "E_TASK_TITLE_REQUIRED" in str(ei.value), str(ei.value)
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_id=?", (parent,)
    ).fetchone()[0]
    assert n == 0, f"空标题 split 不应创建子任务 (got {n})"
    conn.close()


def test_split_rejects_blank_title(client):
    """fail-closed：纯空白 title（trim 后为空）同样被拒绝。"""
    parent = _create_parent(client)
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("task.split", {
            "task_id": parent,
            "subtasks": [
                {"title": "   ", "description": "d", "steps": [
                    {"action": "port_rust_authority", "target_file": "a.rs"}]},
            ],
            "identity_policy": "legacy_identity_v1",
            "request_id": f"sp-{parent}-{uuid.uuid4().hex[:8]}",
        })
    assert "E_TASK_TITLE_REQUIRED" in str(ei.value), str(ei.value)


def test_split_rejects_empty_description(client):
    """fail-closed：空 description 的子任务被拒绝（无验收上下文）。"""
    parent = _create_parent(client)
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("task.split", {
            "task_id": parent,
            "subtasks": [
                {"title": "有标题无描述", "description": "", "steps": [
                    {"action": "port_rust_authority", "target_file": "a.rs"}]},
            ],
            "identity_policy": "legacy_identity_v1",
            "request_id": f"sp-{parent}-{uuid.uuid4().hex[:8]}",
        })
    assert "E_TASK_DESCRIPTION_REQUIRED" in str(ei.value), str(ei.value)
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_id=?", (parent,)
    ).fetchone()[0]
    assert n == 0, f"空描述 split 不应创建子任务 (got {n})"
    conn.close()


def test_split_rejects_whole_batch_on_any_invalid(client):
    """整批 fail-closed：混合批次中任一子任务违规 → 全部不创建（含合法的那个）。"""
    parent = _create_parent(client)
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("task.split", {
            "task_id": parent,
            "subtasks": [
                {"title": "合法子任务", "description": "合法", "steps": [
                    {"action": "port_rust_authority", "target_file": "a.rs"}]},
                {"title": "", "description": "空标题", "steps": [
                    {"action": "thin_cli_client", "target_file": "b.py"}]},
            ],
            "identity_policy": "legacy_identity_v1",
            "request_id": f"sp-{parent}-{uuid.uuid4().hex[:8]}",
        })
    assert "E_TASK_TITLE_REQUIRED" in str(ei.value), str(ei.value)
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_id=?", (parent,)
    ).fetchone()[0]
    assert n == 0, f"整批拒绝语义：合法子任务也不应被创建 (got {n})"
    conn.close()


# ---- create_subtask title fail-closed（T-P2-789860416867）----
# 与 split 同源的空壳卡根因；description 保持可选（MCP 工具默认空串 +
# 既有调用方不传 description），仅 title 强制非空。


def test_create_subtask_rejects_empty_title(client):
    """空 title 不再静默兜底为 "subtask"，直接 fail-closed 拒绝。"""
    parent = _create_parent(client)
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("task.create_subtask", {
            "parent_task_id": parent,
            "title": "",
            "description": "有描述但标题空",
        })
    assert "E_TASK_TITLE_REQUIRED" in str(ei.value), str(ei.value)
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_id=?", (parent,)
    ).fetchone()[0]
    assert n == 0, f"空 title 不应创建子任务 (got {n})"
    conn.close()


def test_create_subtask_rejects_blank_title(client):
    """blank title（trim 后为空）同样拒绝，与 split 校验器 trim 语义一致。"""
    parent = _create_parent(client)
    with pytest.raises(DaemonRemoteError) as ei:
        client.call("task.create_subtask", {
            "parent_task_id": parent,
            "title": "   \t ",
            "description": "有描述",
        })
    assert "E_TASK_TITLE_REQUIRED" in str(ei.value), str(ei.value)
    conn = _db()
    n = conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE parent_id=?", (parent,)
    ).fetchone()[0]
    assert n == 0, f"blank title 不应创建子任务 (got {n})"
    conn.close()


def test_create_subtask_accepts_no_description(client):
    """回归：description 仍可选（MCP 工具默认空串 + 既有调用方依赖），
    有 title 无 description 必须正常创建。"""
    parent = _create_parent(client)
    r = client.call("task.create_subtask", {
        "parent_task_id": parent,
        "title": "无描述子任务",
    })
    assert r.get("task_id"), f"合法创建应返回 task_id: {r}"
    conn = _db()
    row = conn.execute(
        "SELECT title, description FROM tasks WHERE id=?", (r["task_id"],)
    ).fetchone()
    assert row is not None, "子任务未落库"
    assert row["title"] == "无描述子任务", row["title"]
    conn.close()
