"""CLI-089 (A′ cli_command_projection) `cw task split` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_089_http_rpc.py）：
  - success：plan 解析 + daemon RPC task.split 成功，Python 仅编排（route_task_write）
  - 结构不变量：split 走 route_task_write（HTTP/daemon 权威），不持有 Unix socket 业务路径。

Python 侧已通过 route_task_write/route_task_read 路由到 Rust daemon；Rust 侧
`task.split` handler（dispatch.rs / http_server.rs）的实现与编译由其它 agent 核验。
"""

import pytest

import callwarden.cli.main as main_mod


def _plan_text():
    return (
        "# 拆分计划\n"
        "## 子任务 A：重构解析器\n"
        "这一步重构 Markdown 解析。\n"
        "## 子任务 B：补齐测试\n"
        "这一步补齐测试。\n"
    )


def test_cli089_split_routes_to_daemon(monkeypatch, tmp_path):
    """success：split 通过 route_task_write 调用 task.split，Python 仅编排。"""
    plan = tmp_path / "plan.md"
    plan.write_text(_plan_text(), encoding="utf-8")

    captured = {}

    def _fake_write(method, params, fallback_fn):
        captured["method"] = method
        captured["params"] = params
        return {"subtasks": ["T-sub-1", "T-sub-2"], "subtask_count": 2}

    def _fake_read(method, params, fallback_fn):
        # 等价于 daemon 返回的 task.status（已存在）
        return {"task_id": "T-1", "status": "in_progress"}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_write)
    monkeypatch.setattr(main_mod, "route_task_read", _fake_read)

    rc = main_mod._handle_task(["split", "T-789", "--plan", str(plan)], None)
    assert rc is True
    assert captured.get("method") == "task.split"
    assert "subtasks" in captured.get("params", {})
    assert captured["params"].get("task_id") == "T-789"


def test_cli089_split_plan_missing(monkeypatch, tmp_path):
    """plan 文件不存在 -> 返回 True（业务错误，不崩溃，不触达 daemon）。"""
    captured = {}

    def _fake_write(method, params, fallback_fn):
        captured["hit"] = True
        return {"subtasks": [], "subtask_count": 0}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_write)
    monkeypatch.setattr(main_mod, "route_task_read", lambda m, p, f: {"task_id": "T-1"})

    rc = main_mod._handle_task(["split", "T-789", "--plan", str(tmp_path / "missing.md")], None)
    assert rc is True
    assert "hit" not in captured, "plan 缺失时应短路，不应触达 route_task_write"
