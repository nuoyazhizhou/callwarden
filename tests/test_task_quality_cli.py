"""任务质量门禁 CLI 命令测试。

覆盖 Step S-1783247858393-9d50 新增的 3 个 CLI 子命令：
- cw task findings <task_id> [--status] [--severity]
- cw task resolve-finding <finding_id> [--resolution] [--by]
- cw task list [--blocked]

测试策略：
- 通过 _handle_task 直接调用 CLI 处理函数（不启动子进程）
- 验证返回值为 True（命令成功执行）
- 验证副作用：findings 写入 / resolve 改变 status / list 输出正确
- 验证 i18n key 走 t() 而非硬编码
"""

import os
import tempfile

from callwarden.db.db import CodeGraphDB
from callwarden.cli.main import _handle_task


def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _create_task_with_step(db, title="cli-test"):
    """辅助：创建带 1 个步骤的任务，返回 task_id"""
    task_id = db.task_create(title, steps=[{"action": "edit"}])
    return task_id


def _capture_output(func, *args, **kwargs):
    """辅助：捕获 print/cprint 输出"""
    import io
    import contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        result = func(*args, **kwargs)
    return result, buf.getvalue()


# ---------- cw task findings ----------

def test_cli_task_findings_returns_true():
    """cw task findings 命令返回 True（成功执行）"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        result, output = _capture_output(_handle_task, ["findings", task_id], db)
        assert result is True
        # 输出包含标题
        assert "任务质量发现" in output or "Task Quality Findings" in output
    finally:
        db.close()


def test_cli_task_findings_shows_findings():
    """有 finding 时显示 finding 详情"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        # 写入一条 finding
        db.record_task_quality_finding(
            task_id, severity="warn", message="test warning",
            finding_type="semgrep", source="semgrep",
        )
        result, output = _capture_output(_handle_task, ["findings", task_id], db)
        assert result is True
        # 输出应包含 finding 的 message
        assert "test warning" in output
        assert "warn" in output
    finally:
        db.close()


def test_cli_task_findings_status_filter():
    """--status 过滤：只显示 resolved"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        # 第一条将被标记为 resolved（消息内容表明它原本是 open 状态）
        fid = db.record_task_quality_finding(
            task_id, severity="warn", message="will-be-resolved",
        )
        db.record_task_quality_finding(
            task_id, severity="warn", message="still-open",
        )
        db.resolve_task_quality_finding(fid, resolution="fixed")
        # --status resolved 应只显示已被解决的那一条
        result, output = _capture_output(
            _handle_task, ["findings", task_id, "--status", "resolved"], db
        )
        assert result is True
        assert "will-be-resolved" in output
        assert "still-open" not in output
    finally:
        db.close()


def test_cli_task_findings_no_findings():
    """无 finding 时显示 no findings 提示"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        result, output = _capture_output(_handle_task, ["findings", task_id], db)
        assert result is True
        # 输出包含「无质量发现」或「no findings」
        assert "无质量发现" in output or "no findings" in output
    finally:
        db.close()


# ---------- cw task resolve-finding ----------

def test_cli_task_resolve_finding_success():
    """cw task resolve-finding 成功解决 finding"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        fid = db.record_task_quality_finding(
            task_id, severity="error", message="error finding",
        )
        result, output = _capture_output(
            _handle_task, ["resolve-finding", str(fid)], db
        )
        assert result is True
        # 验证 finding 状态已变更为 resolved
        findings = db.get_task_quality_findings(task_id, status="all")
        assert findings[0]["status"] == "resolved"
        # 输出应包含成功标记
        assert "已解决" in output or "resolved" in output
    finally:
        db.close()


def test_cli_task_resolve_finding_wontfix():
    """--resolution wontfix 标记为 wontfix"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db)
        fid = db.record_task_quality_finding(task_id, message="x")
        result, _output = _capture_output(
            _handle_task,
            ["resolve-finding", str(fid), "--resolution", "wontfix"],
            db,
        )
        assert result is True
        findings = db.get_task_quality_findings(task_id, status="all")
        assert findings[0]["status"] == "wontfix"
    finally:
        db.close()


def test_cli_task_resolve_finding_not_found():
    """不存在的 finding_id → 失败但不抛异常"""
    db, _root = _db_with_workspace()
    try:
        result, output = _capture_output(
            _handle_task, ["resolve-finding", "99999"], db
        )
        assert result is True  # 命令成功执行（即使业务失败也返回 True）
        # 输出应包含失败提示
        assert "失败" in output or "Failed" in output
    finally:
        db.close()


# ---------- cw task list ----------

def test_cli_task_list_returns_true():
    """cw task list 命令返回 True"""
    db, _root = _db_with_workspace()
    try:
        _create_task_with_step(db, title="task-a")
        _create_task_with_step(db, title="task-b")
        result, output = _capture_output(_handle_task, ["list"], db)
        assert result is True
        # 输出包含标题
        assert "任务列表" in output or "Task List" in output
    finally:
        db.close()


def test_cli_task_list_shows_tasks():
    """list 显示所有任务"""
    db, _root = _db_with_workspace()
    try:
        _create_task_with_step(db, title="visible-task")
        result, output = _capture_output(_handle_task, ["list"], db)
        assert result is True
        assert "visible-task" in output
    finally:
        db.close()


def test_cli_task_list_blocked_filter():
    """--blocked 只显示有阻塞发现的任务"""
    db, _root = _db_with_workspace()
    try:
        # task-a：有 error finding（阻塞）
        task_a = _create_task_with_step(db, title="blocked-task")
        db.record_task_quality_finding(task_a, severity="error", message="blocking")
        # task-b：无 finding（不阻塞）
        _create_task_with_step(db, title="clean-task")

        result, output = _capture_output(
            _handle_task, ["list", "--blocked"], db
        )
        assert result is True
        # 输出应包含 blocked-task，不包含 clean-task
        assert "blocked-task" in output
        assert "clean-task" not in output
    finally:
        db.close()


def test_cli_task_list_blocked_marker():
    """有阻塞发现的任务显示 [!] 标记"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db, title="marked-task")
        db.record_task_quality_finding(task_id, severity="error", message="x")
        result, output = _capture_output(_handle_task, ["list"], db)
        assert result is True
        # [!] 表示阻塞
        assert "[!]" in output
    finally:
        db.close()


# ---------- i18n 验证 ----------

def test_cli_task_findings_uses_i18n_keys():
    """CLI 输出使用 i18n key（验证关键 key 存在）"""
    from callwarden.i18n import t
    # 验证关键 key 不返回 default 值（说明 key 存在）
    assert t("cli_task_findings_desc", default="__MISSING__") != "__MISSING__"
    assert t("cli_task_resolve_finding_desc", default="__MISSING__") != "__MISSING__"
    assert t("cli_task_list_desc", default="__MISSING__") != "__MISSING__"
    # 验证 messages key 存在
    assert t("cli.messages.task_findings_title", default="__MISSING__") != "__MISSING__"
    assert t("cli.messages.task_findings_count", default="__MISSING__") != "__MISSING__"
    assert t("cli.messages.task_resolve_finding_ok", default="__MISSING__") != "__MISSING__"
    assert t("cli.messages.task_panel_title", default="__MISSING__") != "__MISSING__"
    assert t("cli.messages.task_panel_item", default="__MISSING__") != "__MISSING__"
