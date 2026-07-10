"""task_apply / task_close 命令测试。

覆盖任务状态机后段（review → applied → closed）：
- v24 schema：tasks 表新增 applied_at 字段，SCHEMA_VERSION == 24
- v23 → v24 迁移幂等性
- task_apply：review → applied 正常流程
- task_apply：非法状态拒绝（open/in_progress/applied/closed）
- task_apply：任务不存在返回 error
- task_close：applied → closed 正常流程
- task_close：非法状态拒绝（open/in_progress/review/closed）
- task_close：任务不存在返回 error
- applied_at / closed_at 时间戳正确写入数据库
- reviewer 字段正确回填
- 完整状态机：open → in_progress → review → applied → closed
- i18n key 生效（错误消息不返回 default 文案占位符）

设计原则：写代码的 Agent 不能自己 applied/closed，必须由其他会话的 LLM 审核。
本测试模拟"另一个会话的 LLM"角色，对 review/applied 状态的任务执行审核关闭。
"""

import os
import sqlite3
import tempfile
import time

from callwarden.db.db import CodeGraphDB
from callwarden.db.schema import (
    SCHEMA_VERSION,
    TASK_STATUS_OPEN,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_REVIEW,
    TASK_STATUS_APPLIED,
    TASK_STATUS_CLOSED,
    TASK_STATUS_REVERTED,
)


def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _table_columns(conn, table_name):
    """获取表字段列表。"""
    cur = conn.execute(f"PRAGMA table_info({table_name})")
    return [row["name"] for row in cur.fetchall()]


def _set_task_status(conn, task_id, status):
    """直接用 SQL 设置任务状态（绕过状态机，用于测试前置条件构造）。"""
    conn.execute(
        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
        (status, time.time(), task_id),
    )
    conn.commit()


def _get_task_row(conn, task_id):
    """读取 tasks 表整行。"""
    cur = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
    return cur.fetchone()


# ----------------------------------------------------------------------
# Schema v24
# ----------------------------------------------------------------------

def test_schema_version_is_24():
    """SCHEMA_VERSION 常量不低于 25（applied_at 引入版本 v24+）。"""
    assert SCHEMA_VERSION >= 25


def test_tasks_table_has_applied_at_column():
    """全新数据库的 tasks 表包含 applied_at 字段。"""
    db, _root = _db_with_workspace()
    try:
        cols = _table_columns(db.conn, "tasks")
        assert "applied_at" in cols
        # closed_at 字段早已存在，这里一并确认
        assert "closed_at" in cols
    finally:
        db.close()


def test_schema_version_table_records_v24():
    """schema_version 表记录当前版本（≥25，applied_at 字段已就绪）。"""
    db, _root = _db_with_workspace()
    try:
        cur = db.conn.execute(
            "SELECT MAX(version) as v FROM schema_version"
        )
        row = cur.fetchone()
        assert row is not None and row["v"] >= 25
    finally:
        db.close()


def test_v23_to_v24_migration_idempotent():
    """v23 → v24 迁移幂等：重复执行不报错，字段不重复添加。"""
    db, _root = _db_with_workspace()
    try:
        # 全新数据库已包含 applied_at 字段
        cols_before = _table_columns(db.conn, "tasks")
        assert "applied_at" in cols_before

        # 手动调用迁移函数（幂等）
        from callwarden.db.db_base import _migrate_v23_to_v24
        _migrate_v23_to_v24(db.conn)

        # 字段仍存在且只有一个
        cols_after = _table_columns(db.conn, "tasks")
        assert cols_after.count("applied_at") == 1
    finally:
        db.close()


def test_v23_to_v24_migration_on_old_db_without_applied_at():
    """旧库（无 applied_at 字段）执行 v24 迁移后补齐字段。"""
    db, _root = _db_with_workspace()
    try:
        # 模拟旧库：重建 tasks 表但不含 applied_at 字段
        cols = _table_columns(db.conn, "tasks")
        assert "applied_at" in cols  # 全新库已有该字段

        cols_without_applied = [c for c in cols if c != "applied_at"]
        col_defs = ", ".join([f'"{c}"' for c in cols_without_applied])
        col_list = ", ".join([f'"{c}"' for c in cols_without_applied])

        # 备份并重建（SQLite 不支持 DROP COLUMN，用重建表方式）
        db.conn.execute("ALTER TABLE tasks RENAME TO tasks_old")
        # 重建表结构：只保留非 applied_at 字段，类型统一用原 schema 中的定义
        # 为简化测试，用 TEXT 类型（足以验证迁移逻辑）
        db.conn.execute(f"CREATE TABLE tasks ({col_defs})")
        db.conn.execute(f"INSERT INTO tasks ({col_list}) SELECT {col_list} FROM tasks_old")
        db.conn.execute("DROP TABLE tasks_old")
        db.conn.commit()

        # 确认此时无 applied_at 字段
        cols_before = _table_columns(db.conn, "tasks")
        assert "applied_at" not in cols_before

        # 执行迁移
        from callwarden.db.db_base import _migrate_v23_to_v24
        _migrate_v23_to_v24(db.conn)

        # 字段已补齐
        cols_after = _table_columns(db.conn, "tasks")
        assert "applied_at" in cols_after
    finally:
        db.close()


# ----------------------------------------------------------------------
# task_apply: review → applied
# ----------------------------------------------------------------------

def test_task_apply_review_to_applied_success():
    """task_apply 正常流程：review → applied。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(
            title="测试任务",
            description="测试 task_apply 流程",
            steps=[{"action": "annotate", "target_file": "a.py"}],
        )
        # 直接设置为 review 状态（模拟写代码 Agent 完成后进入审核）
        _set_task_status(db.conn, task_id, TASK_STATUS_REVIEW)

        result = db.task_apply(task_id, reviewer="reviewer-llm-session")

        assert result["task_id"] == task_id
        assert result["status"] == TASK_STATUS_APPLIED
        assert result["reviewer"] == "reviewer-llm-session"
        assert "applied_at" in result
        assert isinstance(result["applied_at"], float)
        assert result["applied_at"] > 0

        # 数据库中状态确实已更新
        row = _get_task_row(db.conn, task_id)
        assert row["status"] == TASK_STATUS_APPLIED
        assert row["applied_at"] is not None
        assert row["applied_at"] > 0
    finally:
        db.close()


def test_task_apply_invalid_status_open():
    """task_apply 拒绝 open 状态任务（必须先到 review）。

    注意：无 steps 的任务可以自动推进（见 test_task_no_steps_fix.py），
    这里用有 steps 的任务测试拒绝逻辑。
    """
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试任务", description="",
                                 steps=[{"action": "annotate", "target_file": "a.py"}])
        # 任务初始状态为 open，未进入 review
        result = db.task_apply(task_id, reviewer="reviewer")

        assert "error" in result
        assert result["task_id"] == task_id
        assert result["status"] == TASK_STATUS_OPEN
        # error 消息不为空（i18n key 已配置）
        assert result["error"]
        # 错误消息不应是 default 占位符
        assert "Cannot apply task in status" not in result["error"]
    finally:
        db.close()


def test_task_apply_invalid_status_in_progress():
    """task_apply 拒绝 in_progress 状态任务。

    注意：无 steps 的任务可以自动推进（见 test_task_no_steps_fix.py），
    这里用有 steps 的任务测试拒绝逻辑。
    """
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试任务", description="",
                                 steps=[{"action": "annotate", "target_file": "a.py"}])
        _set_task_status(db.conn, task_id, TASK_STATUS_IN_PROGRESS)

        result = db.task_apply(task_id, reviewer="reviewer")

        assert "error" in result
        assert result["status"] == TASK_STATUS_IN_PROGRESS
    finally:
        db.close()


def test_task_apply_invalid_status_already_applied():
    """task_apply 拒绝已 applied 状态任务（避免重复审核）。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试任务", description="")
        _set_task_status(db.conn, task_id, TASK_STATUS_REVIEW)
        db.task_apply(task_id, reviewer="first-reviewer")

        # 再次 apply 应该失败
        result = db.task_apply(task_id, reviewer="second-reviewer")

        assert "error" in result
        assert result["status"] == TASK_STATUS_APPLIED
    finally:
        db.close()


def test_task_apply_invalid_status_closed():
    """task_apply 拒绝 closed 状态任务。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试任务", description="")
        _set_task_status(db.conn, task_id, TASK_STATUS_CLOSED)

        result = db.task_apply(task_id, reviewer="reviewer")

        assert "error" in result
        assert result["status"] == TASK_STATUS_CLOSED
    finally:
        db.close()


def test_task_apply_task_not_found():
    """task_apply 任务不存在时返回 error。"""
    db, _root = _db_with_workspace()
    try:
        result = db.task_apply("T-nonexistent-task-id", reviewer="reviewer")

        assert "error" in result
        assert result["task_id"] == "T-nonexistent-task-id"
        # i18n key 已加载：error 不等于 default 值 "Task not found"
        # （i18n 值带 {id} 占位符，default 不带）
        assert result["error"] != "Task not found"
        assert result["error"]  # 非空
    finally:
        db.close()


# ----------------------------------------------------------------------
# task_close: applied → closed
# ----------------------------------------------------------------------

def test_task_close_applied_to_closed_success():
    """task_close 正常流程：applied → closed。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(
            title="测试任务",
            description="测试 task_close 流程",
            steps=[{"action": "annotate", "target_file": "a.py"}],
        )
        # 模拟完整流程：review → applied
        _set_task_status(db.conn, task_id, TASK_STATUS_REVIEW)
        db.task_apply(task_id, reviewer="reviewer-llm")

        # 现在 status=applied，执行 close
        result = db.task_close(task_id, reviewer="closer-llm-session")

        assert result["task_id"] == task_id
        assert result["status"] == TASK_STATUS_CLOSED
        assert result["reviewer"] == "closer-llm-session"
        assert "closed_at" in result
        assert isinstance(result["closed_at"], float)
        assert result["closed_at"] > 0

        # 数据库中状态确实已更新
        row = _get_task_row(db.conn, task_id)
        assert row["status"] == TASK_STATUS_CLOSED
        assert row["closed_at"] is not None
        assert row["closed_at"] > 0
    finally:
        db.close()


def test_task_close_invalid_status_open():
    """task_close 拒绝 open 状态任务（必须先 applied）。

    注意：无 steps 的任务可以自动推进（见 test_task_no_steps_fix.py），
    这里用有 steps 的任务测试拒绝逻辑。
    """
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试任务", description="",
                                 steps=[{"action": "annotate", "target_file": "a.py"}])

        result = db.task_close(task_id, reviewer="reviewer")

        assert "error" in result
        assert result["task_id"] == task_id
        assert result["status"] == TASK_STATUS_OPEN
        # 错误消息使用 i18n
        assert "Cannot close task in status" not in result["error"]
    finally:
        db.close()


def test_task_close_invalid_status_review():
    """task_close 拒绝 review 状态任务（必须先 applied）。

    注意：无 steps 的任务可以自动推进（见 test_task_no_steps_fix.py），
    这里用有 steps 的任务测试拒绝逻辑。
    """
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试任务", description="",
                                 steps=[{"action": "annotate", "target_file": "a.py"}])
        _set_task_status(db.conn, task_id, TASK_STATUS_REVIEW)

        result = db.task_close(task_id, reviewer="reviewer")

        assert "error" in result
        assert result["status"] == TASK_STATUS_REVIEW
    finally:
        db.close()


def test_task_close_invalid_status_in_progress():
    """task_close 拒绝 in_progress 状态任务。

    注意：无 steps 的任务可以自动推进（见 test_task_no_steps_fix.py），
    这里用有 steps 的任务测试拒绝逻辑。
    """
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试任务", description="",
                                 steps=[{"action": "annotate", "target_file": "a.py"}])
        _set_task_status(db.conn, task_id, TASK_STATUS_IN_PROGRESS)

        result = db.task_close(task_id, reviewer="reviewer")

        assert "error" in result
        assert result["status"] == TASK_STATUS_IN_PROGRESS
    finally:
        db.close()


def test_task_close_invalid_status_already_closed():
    """task_close 拒绝已 closed 状态任务（避免重复关闭）。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试任务", description="")
        _set_task_status(db.conn, task_id, TASK_STATUS_REVIEW)
        db.task_apply(task_id, reviewer="first-reviewer")
        db.task_close(task_id, reviewer="first-closer")

        # 再次 close 应该失败
        result = db.task_close(task_id, reviewer="second-closer")

        assert "error" in result
        assert result["status"] == TASK_STATUS_CLOSED
    finally:
        db.close()


def test_task_close_task_not_found():
    """task_close 任务不存在时返回 error。"""
    db, _root = _db_with_workspace()
    try:
        result = db.task_close("T-nonexistent-task-id", reviewer="reviewer")

        assert "error" in result
        assert result["task_id"] == "T-nonexistent-task-id"
        # i18n key 已加载：error 不等于 default 值 "Task not found"
        assert result["error"] != "Task not found"
        assert result["error"]  # 非空
    finally:
        db.close()


# ----------------------------------------------------------------------
# 完整状态机：open → in_progress → review → applied → closed
# ----------------------------------------------------------------------

def test_full_state_machine_open_to_closed():
    """完整状态机：open → in_progress → review → applied → closed。

    模拟跨会话协作场景：
    - Session A（写代码 Agent）：open → in_progress → review
    - Session B（审核 LLM）：review → applied
    - Session C（关闭 LLM）：applied → closed

    本测试用 SQL 直接构造中间状态，重点验证 B/C 段。
    """
    db, _root = _db_with_workspace()
    try:
        # Session A: 创建任务（open）
        task_id = db.task_create(
            title="跨会话协作任务",
            description="验证 open → in_progress → review → applied → closed",
            steps=[{"action": "annotate", "target_file": "a.py"}],
            creator="agent-session-a",
        )
        row = _get_task_row(db.conn, task_id)
        assert row["status"] == TASK_STATUS_OPEN

        # Session A: 领取步骤（open → in_progress）
        step = db.task_next_step(task_id)
        assert step is not None
        row = _get_task_row(db.conn, task_id)
        assert row["status"] == TASK_STATUS_IN_PROGRESS

        # Session A: 报告步骤完成（in_progress → review，因所有步骤完成）
        db.task_report_step(task_id, step["step_id"], result="done: 完成注释")
        row = _get_task_row(db.conn, task_id)
        assert row["status"] == TASK_STATUS_REVIEW

        # Session B: 审核通过（review → applied）
        apply_result = db.task_apply(task_id, reviewer="reviewer-session-b")
        assert apply_result["status"] == TASK_STATUS_APPLIED
        assert apply_result["reviewer"] == "reviewer-session-b"
        row = _get_task_row(db.conn, task_id)
        assert row["status"] == TASK_STATUS_APPLIED
        assert row["applied_at"] is not None
        assert row["applied_at"] > 0

        # Session C: 关闭任务（applied → closed）
        close_result = db.task_close(task_id, reviewer="closer-session-c")
        assert close_result["status"] == TASK_STATUS_CLOSED
        assert close_result["reviewer"] == "closer-session-c"
        row = _get_task_row(db.conn, task_id)
        assert row["status"] == TASK_STATUS_CLOSED
        assert row["closed_at"] is not None
        assert row["closed_at"] > 0

        # 时间戳顺序：created_at < applied_at < closed_at
        assert row["created_at"] <= row["applied_at"]
        assert row["applied_at"] <= row["closed_at"]
    finally:
        db.close()


def test_reverted_status_not_applyable():
    """reverted 状态的任务无法 apply（避免错误恢复）。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="已回滚任务", description="")
        _set_task_status(db.conn, task_id, TASK_STATUS_REVERTED)

        result = db.task_apply(task_id, reviewer="reviewer")

        assert "error" in result
        assert result["status"] == TASK_STATUS_REVERTED
    finally:
        db.close()


def test_reverted_status_not_closable():
    """reverted 状态的任务无法 close。"""
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="已回滚任务", description="")
        _set_task_status(db.conn, task_id, TASK_STATUS_REVERTED)

        result = db.task_close(task_id, reviewer="reviewer")

        assert "error" in result
        assert result["status"] == TASK_STATUS_REVERTED
    finally:
        db.close()


# ----------------------------------------------------------------------
# i18n 验证
# ----------------------------------------------------------------------

def test_task_apply_error_uses_i18n_key():
    """task_apply 错误消息使用 i18n key，而非硬编码 default 文案。

    注意：无 steps 的任务可以自动推进，这里用有 steps 的任务测试拒绝逻辑。
    """
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试", description="",
                                 steps=[{"action": "annotate", "target_file": "a.py"}])
        # open 状态 apply 应失败
        result = db.task_apply(task_id, reviewer="reviewer")

        # i18n 文案应包含中文（zh_CN 是默认语言）
        assert result["error"]
        # 不应包含 default 占位符的英文
        assert "Cannot apply task in status" not in result["error"]
    finally:
        db.close()


def test_task_close_error_uses_i18n_key():
    """task_close 错误消息使用 i18n key。

    注意：无 steps 的任务可以自动推进，这里用有 steps 的任务测试拒绝逻辑。
    """
    db, _root = _db_with_workspace()
    try:
        task_id = db.task_create(title="测试", description="",
                                 steps=[{"action": "annotate", "target_file": "a.py"}])
        # open 状态 close 应失败
        result = db.task_close(task_id, reviewer="reviewer")

        assert result["error"]
        assert "Cannot close task in status" not in result["error"]
    finally:
        db.close()


def test_task_not_found_uses_i18n_key():
    """任务不存在的错误消息使用 i18n key。"""
    db, _root = _db_with_workspace()
    try:
        apply_result = db.task_apply("T-nonexistent", reviewer="reviewer")
        close_result = db.task_close("T-nonexistent", reviewer="reviewer")

        # 两个错误消息都非空
        assert apply_result["error"]
        assert close_result["error"]
        # i18n key 已加载：不等于 default 值 "Task not found"
        # （i18n 值带 {id} 占位符，default 不带）
        assert apply_result["error"] != "Task not found"
        assert close_result["error"] != "Task not found"
    finally:
        db.close()
