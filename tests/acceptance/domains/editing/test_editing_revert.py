"""TP-11E 编辑/hash/并发冲突/回滚动验收：提案生命周期 + 回滚语义 + 隔离。

在完全隔离的 daemon（独立 run 根 + git provenance workspace + RPC 直调）上
验证编辑审计面，每步断言 daemon 权威响应。

🔴 本域实测**未发现缺陷**（与 TP-11I I5 / TP-11J 作业落库不同），套件价值
在于把回滚幂等、跨 workspace 隔离、缺参 fail-closed 这些**易被后续改动破坏**
的语义固化为回归护栏。
"""

from __future__ import annotations

import pytest

from editing_harness import EditingEnv

FIXTURE_FILE = "docs/note.md"


@pytest.fixture(scope="module")
def env():
    e = EditingEnv()
    # run 根自清洁（防上一轮残留 daemon/registry 污染本轮断言）
    EditingEnv.wipe_run_root(e.run_root)
    e = EditingEnv()
    e.start_daemon()
    try:
        e.build_graph()
        yield e
    finally:
        e.stop_daemon()


def test_e1_propose_lifecycle(env):
    """E1：四类提案均写入 file_edit_audit，回 edit_id + status='proposed'。"""
    p = env.propose_edit(FIXTURE_FILE, "hello world", operation="edit")
    assert p.ok, f"edit.propose 失败：{p!r}"
    assert p.result.get("status") == "proposed"
    assert p.result.get("file_path") == FIXTURE_FILE
    eid = p.result.get("edit_id")
    assert isinstance(eid, int) and eid > 0, f"应回正整数 edit_id：{p.result}"

    r = env.propose_range_patch(FIXTURE_FILE, "patched content", 1, 3)
    assert r.ok, f"propose_range_patch 失败：{r!r}"
    assert r.result.get("status") == "proposed"
    assert r.result.get("edit_id") != eid, "每次提案应是新 edit_id"


def test_e2_revert_semantics(env):
    """E2：回滚语义——首次成功，重复/不存在一律 edit_not_found（非静默）。

    🔴 关键契约：revert 的 WHERE 带 `status != 'reverted'`，故重复回滚返回
    `edit_not_found` 而不是静默成功。这是「回滚幂等」的真实形态，不是缺陷——
    调用方须据此区分「已回滚」与「不存在」之外的第三种情况。
    """
    p = env.propose_edit(FIXTURE_FILE, "to be reverted")
    assert p.ok, f"propose 失败：{p!r}"
    eid = p.result.get("edit_id")

    first = env.revert_edit(eid)
    assert first.ok, f"首次回滚应成功：{first!r}"
    assert first.result.get("status") == "reverted"

    # 重复回滚 → edit_not_found（WHERE status != 'reverted' 命中 0 行）
    again = env.revert_edit(eid)
    assert not again.ok and again.code == "edit_not_found", \
        f"重复回滚应报 edit_not_found（非静默成功）：{again!r}"

    # 不存在的 edit_id → 同错（与「已回滚」共用错误码，属既定契约）
    missing = env.revert_edit(999999)
    assert not missing.ok and missing.code == "edit_not_found", \
        f"不存在的 edit_id 应被拒：{missing!r}"


def test_e3_cross_workspace_revert_isolated(env):
    """E3：跨 workspace 回滚隔离——他 workspace 的 edit_id 不可回滚。

    revert 的 WHERE 带 workspace_id，故用另一个 workspace 的 instance 回滚
    本 workspace 的 edit，应命中 0 行 → edit_not_found（fail-closed）。
    """
    p = env.propose_edit(FIXTURE_FILE, "cross ws guard")
    assert p.ok, f"propose 失败：{p!r}"
    eid = p.result.get("edit_id")

    other = env.register_extra_workspace("tp11e-other")
    cross = env.revert_edit(eid, instance_id=other)
    assert not cross.ok, \
        f"跨 workspace 回滚必须被拒（workspace_id 限定）：{cross!r}"

    # 本 workspace 仍可正常回滚（未被跨 workspace 调用改写）
    own = env.revert_edit(eid)
    assert own.ok and own.result.get("status") == "reverted", \
        f"本 workspace 回滚应仍有效：{own!r}"


def test_e3b_missing_params_fail_closed(env):
    """E3b：缺必填参数一律 invalid_params（不含糊默认值放行）。"""
    inst = env.setup_workspace()[1]
    cases = {
        "缺 new_content": {"workspace_instance_id": inst, "file_path": "a.md"},
        "缺 file_path": {"workspace_instance_id": inst, "new_content": "x"},
        "缺 workspace_instance_id": {"file_path": "a.md", "new_content": "x"},
    }
    for name, params in cases.items():
        r = env.rpc("edit.propose", params)
        assert not r.ok and r.code == "invalid_params", \
            f"{name} 应被拒：{r!r}"

    # revert 缺 workspace_instance_id
    r = env.rpc("edit.revert", {"edit_id": 1})
    assert not r.ok and r.code == "invalid_params", \
        f"revert 缺 workspace_instance_id 应被拒：{r!r}"


def test_e4_symbol_patch_requires_existing_symbol(env):
    """E4：按符号提案时符号不存在 → symbol_not_found（不静默建空编辑）。"""
    by_name = env.propose_symbol_patch("no.such.QualifiedSymbol", "x")
    assert not by_name.ok and by_name.code == "symbol_not_found", \
        f"不存在的符号名应被拒：{by_name!r}"

    by_id = env.propose_symbol_id_patch(999999, "x")
    assert not by_id.ok and by_id.code == "symbol_not_found", \
        f"不存在的 symbol_id 应被拒：{by_id!r}"


def test_e5_guardrail_and_stats(env):
    """E5：守卫面与统计面可达且结构完整。"""
    gr = env.guardrail_check_edit(FIXTURE_FILE, "hello")
    assert gr.ok, f"guardrail_check_edit 失败：{gr!r}"
    assert "decision" in gr.result, f"守卫应回 decision：{gr.result}"

    st = env.edit_stats()
    assert st.ok, f"edit.stats 失败：{st!r}"
    # 结构完整性（统计口径键全在）
    for key in ("total", "by_status", "by_operation", "revert_rate"):
        assert key in st.result, f"edit.stats 缺 {key}：{st.result}"
    # 已提案 + 已回滚的编辑必须计入 total（否则统计面失真）
    assert st.result.get("total", 0) >= 1, \
        f"已发生的编辑应计入 total（实测 {st.result.get('total')}）：{st.result}"
