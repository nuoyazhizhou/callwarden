"""CLI-092 (A′ task_projection) `cw task list` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_092_http_rpc.py）：
  - success：daemon RPC task.list 返回 {tasks: [...]}，Python 仅编排展示（route_task_read）
  - 结构不变量：list 走 route_task_read（HTTP/daemon 权威），daemon 不可用时 fail-closed 上抛，
    绝不静默回退为空列表（M4：CW_DAEMON_HTTP_ENDPOINT 指向死端点时曾静默返回 Total tasks: 0）。

Python 侧已通过 route_task_read(task.list) 路由到 Rust daemon；Rust 侧
`task.list` handler（dispatch.rs / http_server.rs）的实现与编译由其它 agent 核验。
"""

import pytest

import callwarden.cli.main as main_mod
from callwarden.server.daemon_client import DaemonUnavailableError


def test_cli092_task_list_routes_to_daemon(monkeypatch, capsys):
    """success：task list 通过 route_task_read 调用 task.list，Python 仅编排。"""
    calls = []

    def _fake_read(method, params, fallback_fn):
        calls.append(method)
        if method == "task.list":
            return {
                "tasks": [
                    {"task_id": "T-1", "title": "甲任务", "status": "open",
                     "parent_id": ""},
                    {"task_id": "T-2", "title": "乙任务", "status": "closed",
                     "parent_id": ""},
                ]
            }
        # task.has_blocking_findings 等投影调用
        return {"has_blocking": False}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_read)
    monkeypatch.setattr(main_mod, "route_task_write",
                        lambda m, p, f: None)

    rc = main_mod._handle_task(["list", "--flat"], None)
    assert rc is True
    assert calls[0] == "task.list", "首个读取调用必须是 task.list"
    assert calls.count("task.list") == 1
    out = capsys.readouterr().out
    assert "Total tasks: 2" in out


def test_cli092_task_list_daemon_unavailable_fail_closed(monkeypatch):
    """daemon 不可用 -> DaemonUnavailableError 上抛，不静默输出空列表。"""
    def _boom(method, params, fallback_fn):
        raise DaemonUnavailableError("daemon 连接失败")

    monkeypatch.setattr(main_mod, "route_task_read", _boom)

    with pytest.raises(DaemonUnavailableError):
        main_mod._handle_task(["list", "--flat"], None)
