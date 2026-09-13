"""CLI-091 (A′ task_projection) `cw task split` fail-closed 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_091_http_rpc.py）：
  - task_not_found：daemon 经 route_task_read(task.status) 返回 task_not_found 时，
    DaemonRemoteError 原样上抛（fail-closed，不触达 route_task_write，不执行
    task.split 拆分）。

stale 依据（A 类=测试侧陈旧期望）：
  旧断言期待 `_handle_task(["split", ...])` 返回 True 并打印「Task not found」。
  但 GOV-FIX-07（cli/main.py:1774-1783）起，daemon 业务拒绝（DaemonRemoteError）
  由 `_run_subcommand_mode` 统一转 RC=2；`_handle_task` 的 split 分支
  （cli/main.py:6034-6041）直接调用 `route_task_read("task.status", ...)`，不再
  吞掉 DaemonRemoteError，故本层既不返回 True 也不打印 not found。
  与已对齐的 tests/test_cli_093_http_rpc.py 同范式。

Python 侧已通过 route_task_read(task.status) 路由到 Rust daemon；Rust 侧 task.status
handler（dispatch.rs / http_server.rs）的实现与编译由其它 agent 核验。
"""

import pytest

import callwarden.cli.main as main_mod
from callwarden.server.daemon_protocol import DaemonRemoteError


def test_cli091_task_not_found_fail_closed(monkeypatch, tmp_path):
    """task.status 返回 task_not_found -> DaemonRemoteError 上抛，不触达 route_task_write。"""
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

    with pytest.raises(DaemonRemoteError) as ei:
        main_mod._handle_task(["split", "T-1", "--plan", str(plan)], None)
    assert ei.value.code == "task_not_found"
    assert not touched_write["hit"], (
        "task 不存在时不应触达 route_task_write（task.split）"
    )
