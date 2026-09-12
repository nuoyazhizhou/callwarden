"""CLI-090 (A′ cli_command_projection) `cw local-status` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_090_http_rpc.py）：
  - success：task.status 经 route_task_read 返回，Python 仅格式化输出；
  - daemon 不可达：fail-closed，捕获并提示 'daemon 不可用'，不崩溃。

Python 侧已通过 route_task_read 路由到 Rust daemon；Rust 侧 task.status handler
（dispatch.rs / http_server.rs）的实现与编译由其它 agent 核验。
"""

import pytest

import callwarden.cli.main as main_mod
from callwarden.server.daemon_client import DaemonUnavailableError


def _patch_sections(monkeypatch):
    # 隔离下游展示函数，聚焦 route_task_read 路由断言
    monkeypatch.setattr(main_mod, "_print_task_link_section", lambda *a, **k: None)
    monkeypatch.setattr(main_mod, "_print_task_superseded_section", lambda *a, **k: None)


def test_cli090_local_status_routes_to_daemon(monkeypatch, capsys):
    """success：local-status 通过 route_task_read 调用 task.status。"""
    calls = []

    def _fake_read(method, params, fallback):
        calls.append(method)
        if method == "task.status":
            return {"task_id": "T-1", "status": "in_progress", "title": "demo"}
        # task.governance_projection.get 等投影调用（_print_task_governance_summary）
        return {"lifecycle_status": "in_progress", "workflow_status": "-",
                "current_role": "-", "next_role": "-", "next_action": "-",
                "review": {}}

    monkeypatch.setattr(main_mod, "route_task_read", _fake_read)
    _patch_sections(monkeypatch)

    rc = main_mod._print_task_show(None, "T-1", flat=True)
    assert rc is True
    assert "task.status" in calls, (
        f"扁平主任务状态数据源必须是 task.status，实际调用: {calls}"
    )
    # task.status 与 task.governance_projection.get 各恰 1 次（主任务 + 治理投影）
    assert calls.count("task.status") == 1
    assert calls.count("task.governance_projection.get") == 1


def test_cli090_local_status_daemon_unavailable(monkeypatch):
    """daemon 不可达 -> DaemonUnavailableError 原样上抛（fail-closed，无本地回退）。

    stale 依据：GOV-FIX-08 起 `_print_task_show` 不吞 DaemonUnavailableError，
    RC=2 兜底发生在 `_run_subcommand_mode`（cli/main.py:1774-1783）；旧断言期待
    打印 'daemon 不可用' 并返回 True，属已失效的行为。
    """

    def _boom(method, params, fallback):
        raise DaemonUnavailableError("E_HTTP_DAEMON_UNAVAILABLE: connection refused")

    monkeypatch.setattr(main_mod, "route_task_read", _boom)
    _patch_sections(monkeypatch)

    with pytest.raises(DaemonUnavailableError):
        main_mod._print_task_show(None, "T-1", flat=True)
