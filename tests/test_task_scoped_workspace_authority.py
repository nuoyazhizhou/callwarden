"""Regression coverage for task-scoped HTTP workspace authority.

The HTTP client may have an unrelated active workspace.  A request containing
``task_id`` must therefore let Rust resolve the immutable task binding instead
of injecting that active workspace's numeric ID.
"""

from __future__ import annotations

from callwarden.server import daemon_client


class _FakeHttpClient:
    def __init__(self) -> None:
        self._project_root = "C:/git_work/callwarden"
        self.calls: list[tuple[str, dict]] = []

    def _ensure_remote_snapshot(self, _db_path):
        return "callwarden-instance"

    def call(self, method: str, params: dict):
        self.calls.append((method, dict(params)))
        return {"method": method, "params": params}


def _configure_http_route(monkeypatch):
    client = _FakeHttpClient()
    monkeypatch.setattr(daemon_client, "is_http_transport_enabled", lambda: True)
    monkeypatch.setattr(
        daemon_client.HttpDaemonRpcClient,
        "get_instance",
        classmethod(lambda _cls: client),
    )
    return client


def test_task_scoped_route_never_injects_active_numeric_workspace(monkeypatch):
    """A task-bound lease keeps numeric workspace absent for Rust resolution."""
    client = _configure_http_route(monkeypatch)
    injected = []

    def _unexpected_inject(params):
        injected.append(dict(params))
        return {**params, "workspace_id": 10}

    monkeypatch.setattr(daemon_client, "_inject_workspace_id", _unexpected_inject)

    daemon_client.route_rpc("lease.status", {"task_id": "T-callwarden"})

    assert injected == []
    method, params = client.calls[-1]
    assert method == "lease.status"
    assert "workspace_id" not in params
    # stale 修正依据（server/daemon_client.py:3940-3957）：ADJ-RP10-01 修复后，凡
    # task-scoped 请求（params 含非空 task_id/superseded_id）整体跳过 workspace 注入块，
    # 包括 workspace_instance_id 与 workspace_root——若注入会向 daemon 引入未知字段，
    # parse_request 直接 fail-closed（step spec 4.1-3）。task-scoped 的 numeric workspace
    # 完全由 daemon 侧 task_workspace_bindings 解析。故此处断言 instance 亦未注入。
    assert "workspace_instance_id" not in params


def test_workspace_scoped_route_keeps_numeric_authority_injection(monkeypatch):
    """No task binding means legacy workspace-scoped injection still applies."""
    client = _configure_http_route(monkeypatch)
    monkeypatch.setattr(
        daemon_client,
        "_inject_workspace_id",
        lambda params: {**params, "workspace_id": 10},
    )

    daemon_client.route_rpc("lease.list_events", {})

    method, params = client.calls[-1]
    assert method == "lease.list_events"
    assert params["workspace_id"] == 10


def test_explicit_task_workspace_remains_an_assertion(monkeypatch):
    """The client preserves an explicit caller assertion for daemon validation."""
    client = _configure_http_route(monkeypatch)
    monkeypatch.setattr(
        daemon_client,
        "_inject_workspace_id",
        lambda _params: (_ for _ in ()).throw(AssertionError("must not inject")),
    )

    daemon_client.route_rpc(
        "task.apply",
        {"task_id": "T-callwarden", "workspace_id": 1},
        "PROTECTED_MUTATION",
    )

    _, params = client.calls[-1]
    assert params["workspace_id"] == 1
    assert params["task_id"] == "T-callwarden"
    assert params["request_id"]
