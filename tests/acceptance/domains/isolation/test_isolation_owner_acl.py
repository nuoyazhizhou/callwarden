"""TP-11I · 隔离域验收 —— workspace/用户/权限隔离（IDOR owner ACL）。

selector: tests/acceptance/domains/isolation/test_isolation_owner_acl.py

在完全隔离的 daemon 上验证 workspace 隔离面（2026-10-13 IDOR 修复 fc1130e
的验收对象）：

- I1 owner ACL：foreign-owner workspace 的 status（instance 与数字 id 两种
  键）/ activate / remove 一律 `workspace_forbidden`
  （owned_workspace，workspace.rs:1182）；
- I2 list 过滤：workspace.list 只回本 owner 的 registry 行 + 其对应 capture；
  foreign 行排除；**孤儿 capture**（registry 行已删）也排除
  （list_unified_workspaces(Some(uid))，task_collab.rs:1843-1876）；
- I3 存在性不泄露：不存在的 instance → `E_WORKSPACE_NOT_FOUND`，与
  `workspace_forbidden` 错误码必须可区分（否则可枚举他人 workspace）；
- I4 archived 语义：读面 owned_workspace 拒绝 archived（snapshot.publish →
  `workspace_archived`），而 workspace.status 返回 archived 状态不报错，
  activate/remove 走 owned_workspace_any_status 保持幂等；
- I5 task.create 的 owner ACL **实测不在 create 路径**：capture 已存在时
  foreign-owner workspace 仍可建任务（只校验 task-DB 行存在性 + capture 链
  一致）。本测试如实记录该观察并标 `待裁决`，不判通过也不判缺陷；
- I6 自有 workspace 全链路基线（status/list/activate/snapshot.publish）。

fixture 说明：HTTP 回路下所有 workspace 的 owner 都是同一 peer uid
（loopback 无 UDS peer creds，register 响应 owner_uid=4294967295），故
foreign-owner 行由 harness 在**隔离 registry** 内改写 owner_uid 构造
（仅 run 根内，零生产触碰；与 TP-10C 的 codegraph 读面补表同模式）。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from isolation_harness import (  # noqa: E402
    FOREIGN_OWNER_UID,
    HTTP_PEER_UID,
    IsolationEnv,
)


@pytest.fixture(scope="module")
def env():
    # 自清洁：先杀 run 根内 daemon 并清空 run 根，保证零残留
    # （删 run 根会 orphan daemon 且锁文件随之消失，按 exe 路径主动杀）
    IsolationEnv.wipe_run_root()
    e = IsolationEnv()
    e.start_daemon()
    try:
        e.build_graph()
        yield e
    finally:
        e.stop_daemon()


def test_i6_own_workspace_baseline(env):
    """自有 workspace 全链路基线：status/list/activate/snapshot.publish 均通。"""
    ws_id, inst = env.setup_workspace()
    assert ws_id and inst
    assert env.registry_owner_of(inst) == HTTP_PEER_UID

    st = env.workspace_status_by_instance(inst)
    assert st.ok, f"自有 workspace status 应成功：{st!r}"
    assert st.get("registry_owner_uid") == HTTP_PEER_UID
    assert st.get("registry_instance_id") == inst

    assert inst in env.instances_in_list(), "自有 workspace 必须出现在 list"

    act = env.workspace_activate(inst)
    assert act.ok, f"自有 workspace activate 应成功：{act!r}"

    snap = env.snapshot_publish(instance_id=inst)
    assert snap.ok, f"自有 workspace snapshot.publish 应成功：{snap!r}"
    assert snap.get("snapshot_id"), f"git provenance 下应有 snapshot_id：{snap.result}"

    # 数字 id 键同样可读（自己的）
    st_num = env.workspace_status_by_id(ws_id)
    assert st_num.ok, f"自有 workspace 数字 id status 应成功：{st_num!r}"


def test_i1_foreign_owner_denied_on_all_mutations(env):
    """foreign-owner workspace：status/activate/remove 一律 workspace_forbidden。"""
    foreign = env.register_extra_workspace("tp11i-foreign")
    assert env.registry_owner_of(foreign) == HTTP_PEER_UID  # 基线：先是自己的

    # fixture：翻成 foreign owner（模拟他人 workspace）
    env.set_registry_owner(foreign, FOREIGN_OWNER_UID)
    assert env.registry_owner_of(foreign) == FOREIGN_OWNER_UID

    # 读面（instance 键）
    st = env.workspace_status_by_instance(foreign)
    assert not st.ok and st.code == "workspace_forbidden", \
        f"foreign status(instance) 应 forbidden：{st!r}"

    # 读面（数字 id 键）
    row = env.registry_row(foreign)
    assert row and row.get("workspace_id")
    st_num = env.workspace_status_by_id(int(row["workspace_id"]))
    assert not st_num.ok and st_num.code == "workspace_forbidden", \
        f"foreign status(数字 id) 应 forbidden：{st_num!r}"

    # 写面
    act = env.workspace_activate(foreign)
    assert not act.ok and act.code == "workspace_forbidden", \
        f"foreign activate 应 forbidden：{act!r}"
    rm = env.workspace_remove(foreign)
    assert not rm.ok and rm.code == "workspace_forbidden", \
        f"foreign remove 应 forbidden：{rm!r}"

    # registry 行不得被这些拒绝操作改变（fail-closed 在任何写入前）
    assert env.registry_owner_of(foreign) == FOREIGN_OWNER_UID
    assert env.registry_row(foreign).get("status") == "active"

    # 恢复为自己的，避免状态泄漏到同模块后续测试
    env.set_registry_owner(foreign, HTTP_PEER_UID)
    assert env.workspace_status_by_instance(foreign).ok


def test_i2_list_filters_foreign_and_orphan_captures(env):
    """workspace.list 只回本 owner 的 registry 行 + 对应 capture。

    - foreign registry 行排除；
    - 孤儿 capture（registry 行已删、task-DB capture 仍在）也排除
      （list_unified_workspaces 的 allowed 集合双向匹配）。
    """
    _, own_inst = env.setup_workspace()
    foreign = env.register_extra_workspace("tp11i-list-foreign")
    orphan = env.register_extra_workspace("tp11i-list-orphan")
    # foreign 行必须真的翻成 foreign owner，否则 list 过滤断言无意义
    env.set_registry_owner(foreign, FOREIGN_OWNER_UID)

    # orphan：未建图 workspace 的 create 会被 authority 拒（capture 只能经
    # 自己 workspace 的 create 建立）——先固化这个前置事实
    orphan_row = env.registry_row(orphan)
    orphan_num = int(orphan_row["workspace_id"])
    cr = env.rpc("task.create", {
        "title": "tp11i orphan capture", "description": "orphan",
        "steps": [{"action": "annotate", "target_file": "docs/note.md"}],
        "creator": "agent", "role_contracts": [],
        "identity_policy": "legacy_identity_v1",
        "workspace_id": orphan_num,
        "workspace_instance_id": orphan,
    })
    assert not cr.ok, f"未建图 workspace 的 create 应被拒：{cr!r}"

    # 在自己 workspace 上建任务 → capture 落在 own 行
    cr2 = env.task_create("tp11i own capture", "own",
                          steps=[{"action": "annotate",
                                  "target_file": "docs/note.md"}])
    assert cr2.ok, f"自有 workspace create 应成功：{cr2!r}"

    # 构造孤儿 capture：删掉 own 的 registry 行（capture 仍在 task-DB）
    env.delete_registry_row(own_inst)

    listed = env.instances_in_list()
    assert foreign not in listed, f"foreign registry 行必须排除：{listed}"
    assert own_inst not in listed, \
        f"registry 行已删的孤儿 capture 必须排除：{listed}"

    # 恢复 own registry 行（清缓存后重新 register 自愈同 instance）
    # 注意 setup_workspace 返回 (task_db_workspace_id, instance_id)
    _, inst2 = env.force_reregister_own()
    assert inst2 == own_inst, f"重注册应自愈同一 instance：{own_inst} vs {inst2}"
    assert own_inst in env.instances_in_list(), \
        "恢复注册后 own 应重新出现在 list"


def test_i3_nonexistent_not_confused_with_forbidden(env):
    """存在性不泄露：不存在实例 → E_WORKSPACE_NOT_FOUND（≠ forbidden）。"""
    ghost = "f" * 64  # 确定性不存在、且不可能与真实 instance 碰撞
    st = env.workspace_status_by_instance(ghost)
    assert not st.ok, f"不存在的 workspace 应被拒：{st!r}"
    assert st.code == "E_WORKSPACE_NOT_FOUND", \
        f"不存在必须返回 NOT_FOUND 而非 FORBIDDEN（防枚举）：{st.code}"
    assert st.code != "workspace_forbidden"

    # 数字 id 同样
    st_num = env.workspace_status_by_id(999999999)
    assert not st_num.ok
    assert st_num.code != "workspace_forbidden", \
        f"不存在数字 id 不得返回 forbidden：{st_num.code}"


def test_i4_archived_semantics(env):
    """archived：snapshot.publish 拒（workspace_archived）；status 返回状态；
    activate 幂等可恢复。"""
    ws = env.register_extra_workspace("tp11i-archived")
    env.set_registry_status(ws, "archived")

    # 读面 owned_workspace 拒绝 archived
    snap = env.snapshot_publish(instance_id=ws)
    assert not snap.ok and snap.code == "workspace_archived", \
        f"archived workspace 的 publish 应被拒：{snap!r}"

    # workspace.status 不报错，直接回 archived 状态（unified_workspace_authority
    # 不做 archived 门禁，只做 owner ACL）
    st = env.workspace_status_by_instance(ws)
    assert st.ok, f"archived 的 status 应可读：{st!r}"
    assert st.get("status") == "archived"

    # activate 走 owned_workspace_any_status：幂等恢复
    act = env.workspace_activate(ws)
    assert act.ok, f"archived 的 activate 应幂等成功：{act!r}"
    assert act.get("status") == "active"
    st2 = env.workspace_status_by_instance(ws)
    assert st2.get("status") == "active"


def test_i5_task_create_owner_acl_observation(env):
    """I5 观察项（待裁决）：task.create 不校验 registry owner ACL。

    实测：capture 已存在时，把 registry 行 owner 翻成 foreign 后 task.create
    **仍然成功**——create 路径只校验 task-DB workspaces 行存在性与 capture 链
    一致（bind_task_to_workspace / resolve_create_authority），不比对
    registry owner_uid。
    本测试如实固化该行为并标 `待裁决`：既不判通过（隔离面在 create 路径有
    缺口可能），也不判缺陷（task-DB 为 host 级共享库、任务本身按
    workspace_instance_id 绑定，是否为缺口需 daemon owner 结合 UDS 多用户
    威胁模型裁决）。
    """
    _, own_inst = env.setup_workspace()
    # 先在自己 workspace 上建任务，确立 capture 链
    base = env.task_create("tp11g i5 base", "i5",
                           steps=[{"action": "annotate",
                                   "target_file": "docs/note.md"}])
    assert base.ok, f"基线 create 应成功：{base!r}"

    # 翻 owner 为 foreign 后再建任务
    env.set_registry_owner(own_inst, FOREIGN_OWNER_UID)
    try:
        again = env.task_create("tp11g i5 foreign owner", "i5",
                                steps=[{"action": "annotate",
                                        "target_file": "docs/note.md"}])
        # 固化观察：create 不因 foreign owner 被拒
        assert again.ok, (
            "观察项行为变化：task.create 现在会因 foreign owner 被拒——"
            "若这是有意加固，请把本测试从「待裁决观察」升级为正向断言")
        assert again.get("workspace_id") == env.setup_workspace()[0] or \
            again.get("workspace_instance_id") == own_inst
    finally:
        # 恢复 owner，避免影响同模块后续测试
        env.set_registry_owner(own_inst, HTTP_PEER_UID)
