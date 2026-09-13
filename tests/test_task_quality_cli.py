"""任务质量门禁 CLI 命令测试。

覆盖 Step S-1783247858393-9d50 新增的 3 个 CLI 子命令：
- cw task findings <task_id> [--status] [--severity]
- cw task resolve-finding <finding_id> [--resolution] [--by]
- cw task list [--blocked]

stale 依据（A 桶：薄客户端 RPC seam）：生产已 daemon authority 化，本文件覆盖的
3 个命令现均为 daemon 薄客户端（Rust daemon 为唯一 authority，禁止回退本地 SQLite）：
- `cli/main.py:5755-5768` findings → `route_task_read("task.quality_findings",
  {task_id, status, severity}, _local_findings)`；`_local_findings`
  （`cli/main.py:5758-5762`）为 forbidden，直接 `DaemonUnavailableError`。
- `cli/main.py:5805-5820` resolve-finding → `route_task_write(
  "task.resolve_quality_finding", {finding_id, resolution, resolved_by},
  _local_resolve_finding)`；`_local_resolve_finding`（`cli/main.py:5810-5814`）
  同样 forbidden。
- `cli/main.py:5843-5857` list → `route_task_read("task.list", {status, limit},
  _local_task_list)`；`--blocked` 过滤经 `_route_has_blocking_findings`
  （`cli/main.py:3755-3769` → `route_task_read("task.has_blocking_findings", ...)`）。

故测试改用 conftest 的 `route_stub`（mock `cli_main.route_task_read/
route_task_write`），断言「CLI 走对 RPC + 渲染 daemon 回包」，不再断言本地 DB
直连副作用（本地写库/查库语义已随 authority 迁移失效）。
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

def test_cli_task_findings_returns_true(route_stub):
    """cw task findings 命令返回 True（成功执行）"""
    route_stub.reply("task.quality_findings", {"task_id": "T-1", "findings": []})
    result, output = _capture_output(_handle_task, ["findings", "T-1"], None)
    assert result is True
    assert route_stub.count("task.quality_findings") == 1
    # 输出包含标题
    assert "任务质量发现" in output or "Task Quality Findings" in output


def test_cli_task_findings_shows_findings(route_stub):
    """有 finding 时显示 finding 详情"""
    route_stub.reply("task.quality_findings", {
        "task_id": "T-1",
        "findings": [{
            "id": 1, "severity": "warn", "message": "test warning",
            "status": "open", "finding_type": "semgrep", "source": "semgrep",
        }],
    })
    result, output = _capture_output(_handle_task, ["findings", "T-1"], None)
    assert result is True
    # 输出应包含 finding 的 message
    assert "test warning" in output
    assert "warn" in output


def test_cli_task_findings_status_filter(route_stub):
    """--status 过滤：经 RPC params 透传给 daemon，仅渲染回包内容"""
    route_stub.reply("task.quality_findings", {
        "task_id": "T-1",
        "findings": [{
            "id": 1, "severity": "warn", "message": "will-be-resolved",
            "status": "resolved",
        }],
    })
    # --status resolved 应经 RPC 透传给 daemon（过滤发生在 daemon 侧）
    result, output = _capture_output(
        _handle_task, ["findings", "T-1", "--status", "resolved"], None
    )
    assert result is True
    assert route_stub.last_params("task.quality_findings")["status"] == "resolved"
    assert "will-be-resolved" in output
    assert "still-open" not in output


def test_cli_task_findings_no_findings(route_stub):
    """无 finding 时显示 no findings 提示"""
    route_stub.reply("task.quality_findings", {"task_id": "T-1", "findings": []})
    result, output = _capture_output(_handle_task, ["findings", "T-1"], None)
    assert result is True
    # 输出包含「无质量发现」或「no findings」
    assert "无质量发现" in output or "no findings" in output


# ---------- cw task resolve-finding ----------

def test_cli_task_resolve_finding_success(route_stub):
    """cw task resolve-finding 成功解决 finding"""
    route_stub.reply("task.resolve_quality_finding", {
        "finding_id": 1, "status": "resolved", "updated": True})
    result, output = _capture_output(
        _handle_task, ["resolve-finding", "1"], None
    )
    assert result is True
    # daemon 权威写入：断言 RPC 契约（method + params）
    assert route_stub.count("task.resolve_quality_finding") == 1
    params = route_stub.last_params("task.resolve_quality_finding")
    assert params["finding_id"] == 1
    assert params["resolution"] == "fixed"
    # 输出应包含成功标记
    assert "已解决" in output or "resolved" in output


def test_cli_task_resolve_finding_wontfix(route_stub):
    """--resolution wontfix 经 RPC 透传"""
    route_stub.reply("task.resolve_quality_finding", {
        "finding_id": 1, "status": "wontfix", "updated": True})
    result, _output = _capture_output(
        _handle_task,
        ["resolve-finding", "1", "--resolution", "wontfix"],
        None,
    )
    assert result is True
    assert route_stub.last_params(
        "task.resolve_quality_finding")["resolution"] == "wontfix"


def test_cli_task_resolve_finding_not_found(route_stub):
    """不存在的 finding_id → daemon 回包失败但不抛异常"""
    route_stub.reply("task.resolve_quality_finding", {"error": "not found"})
    result, output = _capture_output(
        _handle_task, ["resolve-finding", "99999"], None
    )
    assert result is True  # 命令成功执行（即使业务失败也返回 True）
    # 输出应包含失败提示
    assert "失败" in output or "Failed" in output


# ---------- cw task list ----------

def test_cli_task_list_returns_true(route_stub):
    """cw task list 命令返回 True"""
    route_stub.reply("task.list", {"tasks": [
        {"task_id": "T-a", "title": "task-a", "status": "open"},
        {"task_id": "T-b", "title": "task-b", "status": "open"},
    ]})
    result, output = _capture_output(_handle_task, ["list"], None)
    assert result is True
    assert route_stub.count("task.list") == 1
    # 输出包含标题
    assert "任务列表" in output or "Task List" in output


def test_cli_task_list_shows_tasks(route_stub):
    """list 显示 daemon 返回的任务"""
    route_stub.reply("task.list", {"tasks": [
        {"task_id": "T-v", "title": "visible-task", "status": "open"},
    ]})
    result, output = _capture_output(_handle_task, ["list"], None)
    assert result is True
    assert "visible-task" in output


def test_cli_task_list_blocked_filter(route_stub):
    """--blocked 只显示有阻塞发现的任务

    任务列表经 `task.list` RPC 提供；阻塞状态经 `_route_has_blocking_findings`
    （`cli/main.py:3755-3769`）判定。此处 `task.has_blocking_findings` 未预设回包
    → 走 local 回落读取本地记录的 finding，验证 --blocked 过滤链路。
    """
    db, _root = _db_with_workspace()
    try:
        # task-a：有 error finding（阻塞）
        task_a = _create_task_with_step(db, title="blocked-task")
        db.record_task_quality_finding(task_a, severity="error", message="blocking")
        # task-b：无 finding（不阻塞）
        task_b = _create_task_with_step(db, title="clean-task")

        route_stub.reply("task.list", {"tasks": [
            {"task_id": task_a, "title": "blocked-task", "status": "open"},
            {"task_id": task_b, "title": "clean-task", "status": "open"},
        ]})
        # has_blocking_findings 未预设 → local 回落（本地已记录 error finding）
        route_stub.use_fallback = True

        result, output = _capture_output(
            _handle_task, ["list", "--blocked"], db
        )
        assert result is True
        # 输出应包含 blocked-task，不包含 clean-task
        assert "blocked-task" in output
        assert "clean-task" not in output
    finally:
        db.close()


def test_cli_task_list_blocked_marker(route_stub):
    """有阻塞发现的任务显示 [!] 标记"""
    db, _root = _db_with_workspace()
    try:
        task_id = _create_task_with_step(db, title="marked-task")
        db.record_task_quality_finding(task_id, severity="error", message="x")
        route_stub.reply("task.list", {"tasks": [
            {"task_id": task_id, "title": "marked-task", "status": "open"},
        ]})
        route_stub.use_fallback = True  # has_blocking_findings → local 回落
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
