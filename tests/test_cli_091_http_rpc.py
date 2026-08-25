"""CLI-091 (A′ task_projection) `cw task split` fail-closed 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_091_http_rpc.py）：
  - task_not_found：daemon 经 route_task_read(task.status) 返回 task_not_found 时，
    Python 捕获结构化业务错误、输出「Task not found」并 fail-closed 返回 True，
    不触达 route_task_write（不执行 task.split 拆分）。

Python 侧已通过 route_task_read(task.status) 路由到 Rust daemon；Rust 侧 task.status
handler（dispatch.rs / http_server.rs）的实现与编译由其它 agent 核验。
"""

import pytest

import callwarden.cli.main as main_mod
from callwarden.server.daemon_protocol import DaemonRemoteError


def test_cli091_task_not_found_fail_closed(monkeypatch, tmp_path, capsys):
    """task.status 返回 task_not_found -> 输出 not found，不触达 route_task_write。"""
    plan = tmp_path / "plan.md"
    plan.write_text("# P\n## 子任务 A\n", encoding="utf-8")

    touched_write = {"hit": False}

    def _fake_write(method, params, fallback):
        touched_write["hit"] = True
        return {"subtasks": [], "subtask_count": 0}

    def _boom(method, params, fallback):
        raise DaemonRemoteError("task_not_found", "no such task")

    monkeypatch.setattr(main_mod, "route_task_read", _boom)
    monkeypatch.setattr(main_mod, "route_task_write", _fake_write)

    rc = main_mod._handle_task(["split", "T-1", "--plan", str(plan)], None)
    assert rc is True
    assert not touched_write["hit"], (
        "task 不存在时不应触达 route_task_write（task.split）"
    )
    out = capsys.readouterr().out
    assert "Task not found" in out, "task_not_found 必须输出可见的 not found 提示"
