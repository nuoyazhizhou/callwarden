"""CLI-093 (A′ cli_command_projection) `cw task show --tree` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_093_http_rpc.py）：
  - success：daemon RPC task.status_tree 返回树，Python 仅编排展示（route_task_read）
  - task_not_found：daemon 返回 task_not_found 时输出「not found」，不崩溃
  - daemon 不可用：DaemonUnavailableError 输出「daemon 不可用」，fail-closed

Python 侧已通过 route_task_read(task.status_tree) 路由到 Rust daemon；Rust 侧
`task.status_tree` handler（dispatch.rs / http_server.rs）的实现与编译由其它 agent 核验。
"""

import pytest

import callwarden.cli.main as main_mod
from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.daemon_protocol import DaemonRemoteError


def test_cli093_tree_routes_to_daemon(monkeypatch, capsys):
    """success：task show --tree 通过 route_task_read 调用 task.status_tree。"""
    calls = []

    def _fake_read(method, params, fallback_fn):
        calls.append(method)
        if method == "task.status_tree":
            return {
                "task_id": "T-1",
                "title": "根任务",
                "status": "in_progress",
                "subtasks": [
                    {"task_id": "T-1-sub-1", "title": "子任务", "status": "open",
                     "subtasks": []},
                ],
            }
        # task.superseded_by / task.has_blocking_findings 等投影调用
        return {"superseded_by": None}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_read)

    rc = main_mod._print_task_show(None, "T-1", flat=False)
    assert rc is True
    assert "task.status_tree" in calls, "树形数据源必须是 task.status_tree"
    assert calls.count("task.status_tree") == 1
    out = capsys.readouterr().out
    assert "根任务" in out
    assert "子任务" in out


def test_cli093_tree_task_not_found(monkeypatch, capsys):
    """task.status_tree 返回 task_not_found -> 输出 not found，不崩溃。"""
    def _boom(method, params, fallback_fn):
        raise DaemonRemoteError("task_not_found", "no such task")

    monkeypatch.setattr(main_mod, "route_task_read", _boom)

    rc = main_mod._print_task_show(None, "T-nope", flat=False)
    assert rc is True
    out = capsys.readouterr().out
    assert "not found" in out.lower()


def test_cli093_tree_daemon_unavailable(monkeypatch, capsys):
    """daemon 不可用 -> 输出「daemon 不可用」，fail-closed。"""
    def _boom(method, params, fallback_fn):
        raise DaemonUnavailableError("daemon 连接失败")

    monkeypatch.setattr(main_mod, "route_task_read", _boom)

    rc = main_mod._print_task_show(None, "T-1", flat=False)
    assert rc is True
    out = capsys.readouterr().out
    assert "daemon 不可用" in out


def test_cli093_tree_formats_ratio_as_percent(monkeypatch, capsys):
    """daemon 返回 ratio/percent 后，CLI 不得把 0..1 直接当百分比打印。"""
    def _fake_read(method, params, fallback_fn):
        assert method == "task.status_tree"
        return {
            "task_id": "T-1",
            "title": "根任务",
            "status": "review",
            "workflow_status": "review_pending",
            "progress": {"total": 764, "done": 758, "progress": 758 / 764,
                          "ratio": 758 / 764, "percent": 99.21},
            "subtasks": [],
        }

    monkeypatch.setattr(main_mod, "route_task_read", _fake_read)
    assert main_mod._print_task_show(None, "T-1", flat=False) is True
    out = capsys.readouterr().out
    assert "758/764 (99.21%)" in out
    assert "0.9921465968586387%" not in out
