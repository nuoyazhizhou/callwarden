# -*- coding: utf-8 -*-
"""B2 Legacy 可用入口守护测试。

对应任务 T-1786590722456-db00d074-sub-2 步骤 #3（test）。
验证 workspace active、snapshot publish、stats、uncommented、bootstrap_status
和 build 长任务的 legacy 可用入口，防止后续重构破坏 B2 冻结的调用链：

1. workspace active：register_workspace / set_active_workspace /
   get_active_workspace / list_workspaces 返回正确结果；
2. stats：get_stats() 返回必填键（total_files / total_symbols / total_calls /
   commented 等），且不静默抛异常；
3. uncommented：get_uncommented_symbols() 返回 list（空库返回空 list，非 None）；
4. bootstrap_status workspace 隔离（B2 修复核心）：task_quality_findings 按
   active workspace 过滤（open_findings_count / blocking_findings_count），
   tasks 保持用户级全局统计（tasks 表无 workspace_id 字段）；
5. build 长任务 + snapshot publish + get_status：Python 侧入口存在性断言
   （build_graph / build_directory / get_status 以及 daemon_client 的
   get_stats / _ensure_snapshot 快照发布链路）。

本测试为 db 层单元测试，不启动 daemon、不连接外部服务。
"""

import pytest

from callwarden.db.db import CodeGraphDB


@pytest.fixture()
def db(tmp_path):
    """构造隔离的 CodeGraphDB 实例（db 文件位于 tmp_path，不污染用户库）"""
    instance = CodeGraphDB(db_path=str(tmp_path / "b2_test.db"), workspace_root=str(tmp_path))
    yield instance
    instance.close()


def test_workspace_active_flow(db, tmp_path):
    """workspace active 入口：注册、激活、查询返回正确结果"""
    ws_a = db.register_workspace("ws-a", str(tmp_path / "repo-a"))
    ws_b = db.register_workspace("ws-b", str(tmp_path / "repo-b"))
    assert ws_a != ws_b

    # 注册后默认未激活新工作区
    assert db.set_active_workspace(ws_a) is True
    active = db.get_active_workspace()
    assert active is not None
    assert active["id"] == ws_a
    assert active["name"] == "ws-a"

    # 切换到 B
    assert db.set_active_workspace(ws_b) is True
    active = db.get_active_workspace()
    assert active["id"] == ws_b

    # 按名称激活
    assert db.set_active_workspace("ws-a") is True
    active = db.get_active_workspace()
    assert active["name"] == "ws-a"

    # 不存在的工作区返回 False，不抛异常
    assert db.set_active_workspace(999999) is False

    # list_workspaces 至少包含默认 + 2 个新工作区
    ws_list = db.list_workspaces()
    names = {w["name"] for w in ws_list}
    assert "ws-a" in names and "ws-b" in names
    assert len(ws_list) >= 3


def test_get_stats_returns_required_keys(db):
    """stats 入口：get_stats() 返回必填键，空库不抛异常"""
    stats = db.get_stats()
    assert isinstance(stats, dict)
    for key in ("total_files", "total_symbols", "total_calls", "commented"):
        assert key in stats, f"get_stats 缺少必填键: {key}"
    # 空库统计值为 0 或空 dict，但不能是 None
    assert stats["total_files"] == 0
    assert stats["total_symbols"] == 0


def test_get_uncommented_symbols_returns_list(db):
    """uncommented 入口：get_uncommented_symbols() 返回 list（空库为 []，非 None）"""
    result = db.get_uncommented_symbols(kind="fn")
    assert isinstance(result, list)
    # 带 module_filter 同样返回 list
    result2 = db.get_uncommented_symbols(kind="fn", module_filter="nonexistent")
    assert isinstance(result2, list)


def _insert_task_and_finding(db, task_title, severity, message):
    """创建任务并写入一条 open 质量 finding（跟随当前 active workspace）"""
    task_id = db.task_create(title=task_title, description="", steps=[], creator="test")
    finding_id = db.record_task_quality_finding(
        task_id=task_id,
        finding_type="manual",
        severity=severity,
        message=message,
        source="test",
    )
    assert finding_id > 0, "record_task_quality_finding 应返回 finding_id > 0"
    return task_id


def test_bootstrap_status_workspace_isolated_findings(db, tmp_path):
    """bootstrap_status workspace 隔离（B2 修复核心）：
    两个 workspace 各一条 open finding，激活 A 时只统计 A 的 finding，
    激活 B 时只统计 B 的；tasks 保持全局统计。
    """
    ws_a = db.register_workspace("ws-a", str(tmp_path / "repo-a"))
    ws_b = db.register_workspace("ws-b", str(tmp_path / "repo-b"))

    # 在 A 下写入一条 block finding
    db.set_active_workspace(ws_a)
    _insert_task_and_finding(db, "task-in-a", severity="block", message="blocking finding in A")

    # 在 B 下写入一条 warn finding
    db.set_active_workspace(ws_b)
    _insert_task_and_finding(db, "task-in-b", severity="warn", message="warn finding in B")

    # 激活 A：只统计 A 的 findings（block=1）
    db.set_active_workspace(ws_a)
    status_a = db.bootstrap_status()
    assert status_a["open_findings_count"] == 1
    assert status_a["blocking_findings_count"] == 1

    # 激活 B：只统计 B 的 findings（warn，非 block）
    db.set_active_workspace(ws_b)
    status_b = db.bootstrap_status()
    assert status_b["open_findings_count"] == 1
    assert status_b["blocking_findings_count"] == 0

    # tasks 用户级全局统计：两个 workspace 的任务都计入
    assert status_b["tasks"]["open"] >= 2
    assert set(status_b["tasks"].keys()) == {"open", "in_progress", "review", "applied"}

    # 修复前（无 workspace_id 过滤）的行为：激活 A 时 open_findings_count 会是 2。
    # 修复后必须为 1，证明 B2 修复生效。


def test_build_and_status_entry_points_exist(db):
    """build 长任务 + snapshot publish + get_status 入口存在性"""
    # db 层入口（build_graph MCP 工具实际调用 db.build_full_graph）
    for method in ("build_full_graph", "build_directory", "get_status",
                   "bootstrap_status", "get_stats", "get_uncommented_symbols"):
        assert hasattr(db, method), f"CodeGraphDB 缺少入口方法: {method}"

    # snapshot publish 链路：daemon_client 提供 get_stats（RPC）+ _ensure_snapshot（自动发布快照）
    from callwarden.server import daemon_client as dc

    assert hasattr(dc.DaemonClient, "get_stats")
    assert hasattr(dc.DaemonClient, "_ensure_snapshot")
    assert hasattr(dc.DaemonClient, "_remote_query")
