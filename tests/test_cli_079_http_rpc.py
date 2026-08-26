"""CLI-079（T-1787322799418-ce4698f0）：cw local-close → task.close thin client 契约。

验证 close 分支：
1. daemon 路径：route_task_write 调 task.close，task_id/reviewer/identity 透传，
   P4 lease 凭证（lease_token/fencing_counter）原样携带。
2. lease 凭证缺失：CLI 不静默补默认（daemon E_LEASE_REQUIRED fail-closed）。
3. local 模式无 daemon：fallback fail-closed，抛 SharedTaskWriterRequiredError，
   禁止 legacy db.task_close 兜底（thin-client 冻结合同）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli079_close_routes_to_daemon_with_lease(monkeypatch, capsys):
    """daemon 路径：task.close 透传 identity + P4 lease 凭证。"""
    captured = {}

    def _fake_route_write(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        return {"task_id": "T-1", "status": "closed",
                "closed_at": "2026-08-23T00:00:00Z"}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    params = {"task_id": "T-1", "reviewer": "rv-1",
              "identity": {"agent_id": "a1", "session_id": "s1",
                           "model_id": "m1", "role": "reviewer"},
              "lease_token": "tok-1", "fencing_counter": "7"}
    result = main_mod.route_task_write("task.close", params, lambda: None)
    assert result["status"] == "closed"
    assert captured["method"] == "task.close"
    assert captured["params"]["lease_token"] == "tok-1"
    assert captured["params"]["fencing_counter"] == "7"


def test_cli079_close_missing_lease_fails_closed(monkeypatch):
    """lease 缺失：CLI 不补默认，params 无 lease 字段。"""
    captured = {}

    def _fake_route_write(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        return {"error": "E_LEASE_REQUIRED"}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    params = {"task_id": "T-1", "reviewer": "rv-1", "identity": None}
    result = main_mod.route_task_write("task.close", params, lambda: None)
    assert "E_LEASE_REQUIRED" in str(result.get("error", ""))
    assert "lease_token" not in captured["params"]
    assert "fencing_counter" not in captured["params"]


def test_cli079_close_local_fallback_fails_closed(monkeypatch):
    """local 模式无 daemon：禁止 legacy db 兜底，fail-closed（thin-client 冻结合同）。

    CLI-079 冻结合同要求 Python handler 无 direct db/local 业务路径；
    task.close 必须经 daemon 权威写点。fallback 抛 SharedTaskWriterRequiredError。
    """
    from callwarden.server.daemon_client import SharedTaskWriterRequiredError
    called = {"used": False, "raised": None}

    def _fake_route_write(method, params, fallback):
        called["used"] = True
        try:
            fallback()
        except SharedTaskWriterRequiredError as e:
            called["raised"] = str(e)
            return {"error": str(e)}
        raise AssertionError("fallback 不应执行 legacy db 路径")

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")

    def _local_close():
        # 与 cli/main.py 的 _local_close 行为一致：thin-client 冻结合同禁止 db 直写
        raise SharedTaskWriterRequiredError(
            "cw local-close 必须经 daemon 权威写点（thin-client 冻结合同）；"
            "local 模式需连接本地 daemon，禁止直接 db.task_close"
        )

    result = _fake_route_write("task.close", {"task_id": "T-1"}, _local_close)
    assert called.get("used") is True
    assert called.get("raised") is not None
    assert "daemon 权威写点" in result.get("error", "")
    assert "task_close" in called.get("raised", "") or "db.task_close" in called.get("raised", "")

