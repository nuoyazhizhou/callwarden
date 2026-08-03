"""P3 task mutation Identity 接入 smoke test（临时验证脚本）

验证 task_report_step / task_apply / task_close / task_reopen 接受 identity：
- task_report_step 记录 implementer Identity
- task_apply 强制 session 分离（同 session 被拒，不同 session 通过）
- task_close 仅记录身份，不强制 session 分离
- task_reopen 仅记录身份，不强制 session 分离
- 缺失 identity 时向后兼容

验证后可删除。
"""
import os
import sys
import tempfile
import time

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)


def _make_identity(agent_id, session_id, model_id, role):
    return {
        "agent_id": agent_id,
        "session_id": session_id,
        "model_id": model_id,
        "role": role,
    }


def main():
    from callwarden.db import CodeGraphDB
    print("[setup] 导入检查通过")

    os.environ["CW_USE_RUST_STORAGE"] = "0"
    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        os.environ["CALLWARDEN_DB_PATH"] = db_path
        db = CodeGraphDB(db_path)
        try:
            db.register_workspace("test-ws", tmp)
            db.set_active_workspace("test-ws")

            # 创建任务带步骤
            task_id = db.task_create(
                title="P3 mutation test",
                description="测试 task mutation Identity 接入",
                steps=[{"action": "implement", "target_file": "test.py", "target_symbol": "main"}],
            )
            print(f"[setup] 任务已创建: {task_id}")

            # 获取步骤 ID
            cur = db.conn.execute(
                "SELECT id FROM task_steps WHERE task_id = ? ORDER BY step_index LIMIT 1",
                (task_id,),
            )
            step_id = cur.fetchone()[0]

            # ---------- 1. task_report_step 带 implementer Identity ----------
            impl_identity = _make_identity(
                "agent-impl-001", "sess-impl-001", "glm-5.2", "implementer"
            )
            result = db.task_report_step(
                task_id=task_id,
                step_id=step_id,
                result="实现完成",
                success=True,
                identity=impl_identity,
            )
            # 任务应进入 review 状态
            cur = db.conn.execute("SELECT status FROM tasks WHERE id = ?", (task_id,))
            task_status = cur.fetchone()[0]
            assert task_status == "review", f"report 后应为 review, 实际 {task_status}"

            # 验证 Identity 已记录
            recorded = db.get_task_identity_by_role(task_id, "implementer")
            assert recorded is not None, "implementer Identity 未记录"
            assert recorded["session_id"] == "sess-impl-001"
            print("[1/6] task_report_step 带 implementer Identity 通过")

            # ---------- 2. task_apply 同 session → 拒绝 ----------
            reviewer_same_session = _make_identity(
                "agent-rev-001", "sess-impl-001", "glm-5.2", "reviewer"  # 同 session
            )
            result = db.task_apply(
                task_id=task_id,
                identity=reviewer_same_session,
            )
            assert "error" in result, f"同 session apply 应拒绝, 实际 {result}"
            assert result["error"] == "ERR_IDENTITY_SESSION_NOT_SEPARATED", \
                f"错误码不匹配, 实际 {result.get('error')}"
            print("[2/6] task_apply 同 session → 拒绝通过")

            # ---------- 3. task_apply 不同 session → 通过 ----------
            reviewer_diff_session = _make_identity(
                "agent-rev-002", "sess-rev-002", "glm-5.2", "reviewer"  # 不同 session
            )
            result = db.task_apply(
                task_id=task_id,
                identity=reviewer_diff_session,
            )
            assert "error" not in result, f"不同 session apply 应通过, 实际 {result}"
            assert result["status"] == "applied", f"状态应为 applied, 实际 {result.get('status')}"
            print("[3/6] task_apply 不同 session → 通过")

            # ---------- 4. task_close 带 Identity ----------
            closer_identity = _make_identity(
                "agent-closer-001", "sess-close-001", "glm-5.2", "reviewer"
            )
            result = db.task_close(
                task_id=task_id,
                identity=closer_identity,
            )
            assert "error" not in result, f"close 应通过, 实际 {result}"
            assert result["status"] == "closed", f"状态应为 closed, 实际 {result.get('status')}"

            # 验证 closer Identity 已记录（close 不强制 session 分离）
            recorded = db.get_task_identity_by_role(task_id, "reviewer")
            assert recorded is not None, "reviewer Identity 未记录"
            print("[4/6] task_close 带 Identity 通过")

            # ---------- 5. task_reopen 带 Identity ----------
            reopen_identity = _make_identity(
                "agent-reopener-001", "sess-reopen-001", "glm-5.2", "reviewer"
            )
            result = db.task_reopen(
                task_id=task_id,
                reason="需要修复",
                identity=reopen_identity,
            )
            assert "error" not in result, f"reopen 应通过, 实际 {result}"
            assert result["status"] == "in_progress", f"状态应为 in_progress, 实际 {result.get('status')}"
            print("[5/6] task_reopen 带 Identity 通过")

            # ---------- 6. 缺失 identity 向后兼容 ----------
            # 不传 identity 时不应产生 Identity 相关错误（状态错误是预期的）
            result = db.task_apply(task_id=task_id)
            if "error" in result:
                assert "IDENTITY" not in result.get("error", ""), \
                    f"无 identity 不应产生 Identity 错误, 实际 {result.get('error')}"
            else:
                assert result["status"] == "applied"
            result = db.task_close(task_id=task_id)
            if "error" in result:
                assert "IDENTITY" not in result.get("error", ""), \
                    f"无 identity 不应产生 Identity 错误, 实际 {result.get('error')}"
            else:
                assert result["status"] == "closed"
            print("[6/6] 缺失 identity 向后兼容通过")

            # ---------- bonus: 不完整 Identity 被拒 ----------
            result = db.task_reopen(
                task_id=task_id,
                identity=_make_identity("agent-x", "", "glm-5.2", "reviewer"),  # 缺 session_id
            )
            assert "error" in result, f"不完整 Identity 应拒绝, 实际 {result}"
            assert result["error"] == "ERR_IDENTITY_INCOMPLETE"
            print("[bonus] 不完整 Identity 被拒通过")

        finally:
            db.close()

    print("\n=== ALL P3 TASK MUTATION SMOKE TESTS PASSED ===")


if __name__ == "__main__":
    main()
