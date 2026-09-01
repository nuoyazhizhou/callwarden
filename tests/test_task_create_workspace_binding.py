"""BR-02: Python cw task.create daemon binding parity 测试。

Work order: role-prompt-v1-task-create-binding-bootstrap-recovery（卡 BR-02）
idempotency_key: role-prompt-v1-bootstrap-br02-python-client

覆盖 4 条 acceptance（1-4）：
A1  Python CLI forwards the exact daemon-returned workspace pair
    （resolve_workspace_pair_from_daemon 从 daemon 解析；create 请求原样转发
    workspace_id + workspace_instance_id，不猜数字 / 不合成 ws-{id}）
A2  create output includes task/binding/capture/assignment/step identifiers
    （_render_create_provenance 渲染 5 键 readback）
A3  readback mismatch fails closed
    （_verify_create_readback：task.status 与 create 响应逐一对比，不一致 fail-closed）
A4  daemon unavailable never creates a local task
    （create 分支 resolve 失败 / local fallback 一律 fail-closed，绝不本地建任务）

辅助实现：
- server/daemon_client.resolve_workspace_pair_from_daemon（daemon 权威配对解析）
- cli/main._handle_task create 分支（转发 + provenance 渲染 + readback 对比）
"""

import pytest

from callwarden.cli.main import (
    _handle_task,
    _render_create_provenance,
    _verify_create_readback,
)
from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    resolve_workspace_pair_from_daemon,
)


# ----------------------------------------------------------------------
# A1：resolve_workspace_pair_from_daemon 从 daemon 解析权威配对
# ----------------------------------------------------------------------

class _FakeClient:
    """可控的假 daemon RPC client（记录调用）。"""

    def __init__(self, inject_result, status_result=None, status_error=None):
        self.inject_result = inject_result
        self.status_result = status_result
        self.status_error = status_error
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "mcp.daemon_client.inject_workspace_id":
            return self.inject_result
        if method == "workspace.status":
            if self.status_error is not None:
                raise self.status_error
            return self.status_result
        raise AssertionError(f"unexpected method: {method}")


def test_resolve_pair_returns_daemon_authoritative_pair(monkeypatch):
    """A1：daemon 返回 workspace_id + workspace.status 返回 instance → 配对原样返回。"""
    fake = _FakeClient(
        inject_result={"params": {"workspace_id": 10}, "injected": True},
        status_result={"workspace_id": 10, "workspace_instance_id": "2bba6e894ee2546f",
                       "status": "active"},
    )
    monkeypatch.setattr("callwarden.server.daemon_client._get_rpc_client_for_route",
                        lambda: fake)
    pair = resolve_workspace_pair_from_daemon()
    assert pair == {"workspace_id": 10, "workspace_instance_id": "2bba6e894ee2546f"}
    # 只调用了 inject + workspace.status，无任何本地推导
    assert [m for m, _ in fake.calls] == [
        "mcp.daemon_client.inject_workspace_id", "workspace.status"]


def test_resolve_pair_missing_workspace_id_fails_closed(monkeypatch):
    """A1：daemon 未解析出 workspace_id（无 active workspace）→ fail-closed。"""
    fake = _FakeClient(inject_result={"params": {}, "injected": True})
    monkeypatch.setattr("callwarden.server.daemon_client._get_rpc_client_for_route",
                        lambda: fake)
    with pytest.raises(DaemonUnavailableError):
        resolve_workspace_pair_from_daemon()


def test_resolve_pair_workspace_not_registered_fails_closed(monkeypatch):
    """A1：workspace 未在 daemon 注册表（legacy is_active id 不在注册表）→ fail-closed。

    绝不猜测 instance / 绝不回退 active workspace（forbid_numeric_guess /
    forbid_active_workspace_fallback）。
    """
    from callwarden.server.daemon_client import DaemonRemoteError
    err = DaemonRemoteError("workspace_not_found", "10")
    fake = _FakeClient(
        inject_result={"params": {"workspace_id": 10}, "injected": True},
        status_error=err,
    )
    monkeypatch.setattr("callwarden.server.daemon_client._get_rpc_client_for_route",
                        lambda: fake)
    with pytest.raises(DaemonUnavailableError):
        resolve_workspace_pair_from_daemon()


def test_resolve_pair_missing_instance_fails_closed(monkeypatch):
    """A1：workspace.status 未返回 workspace_instance_id → fail-closed。"""
    fake = _FakeClient(
        inject_result={"params": {"workspace_id": 72}, "injected": True},
        status_result={"workspace_id": 72},  # 缺 instance
    )
    monkeypatch.setattr("callwarden.server.daemon_client._get_rpc_client_for_route",
                        lambda: fake)
    with pytest.raises(DaemonUnavailableError):
        resolve_workspace_pair_from_daemon()


# ----------------------------------------------------------------------
# A1：CLI create 转发 exact daemon-returned pair
# ----------------------------------------------------------------------

def _fake_create_flow(monkeypatch, resolve_pair, create_response, readback_response,
                      fallback_calls=None):
    """装配 create 分支的假 daemon 依赖，返回捕获的转发参数。"""
    captured = {}

    def fake_resolve():
        return dict(resolve_pair)

    def fake_route(method, params, fallback):
        captured["method"] = method
        captured["params"] = dict(params)
        captured["fallback"] = fallback
        return create_response

    class _ReadbackClient:
        def call(self, method, params):
            assert method == "task.status"
            return readback_response

    monkeypatch.setattr("callwarden.cli.main.resolve_workspace_pair_from_daemon",
                        fake_resolve)
    monkeypatch.setattr("callwarden.cli.main.route_task_write", fake_route)
    monkeypatch.setattr("callwarden.server.daemon_client._get_rpc_client_for_route",
                        lambda: _ReadbackClient())
    return captured


def test_create_forwards_exact_daemon_pair(monkeypatch, capsys):
    """A1：CLI task create 把 daemon 解析的 pair 原样转发给 task.create。"""
    create_resp = {
        "task_id": "T-br02-test-1", "status": "open", "title": "BR02 t",
        "step_count": 0, "contract_count": 3,
        "workspace_id": 10, "workspace_instance_id": "2bba6e894ee2546f",
        "workspace_binding_id": "tb-br02-1", "workspace_capture_id": "wc-br02-1",
        "assignment_id": None,
    }
    captured = _fake_create_flow(
        monkeypatch,
        resolve_pair={"workspace_id": 10, "workspace_instance_id": "2bba6e894ee2546f"},
        create_response=create_resp,
        readback_response=create_resp,  # 一致
    )
    _handle_task(["create", "--title", "BR02 t"], None)
    assert captured["method"] == "task.create"
    assert captured["params"]["workspace_id"] == 10
    assert captured["params"]["workspace_instance_id"] == "2bba6e894ee2546f"
    assert "identity_policy" in captured["params"]


def test_create_explicit_instance_still_forwarded(monkeypatch, capsys):
    """A1：显式 --workspace-instance-id 时与 daemon 解析的 workspace_id 合并转发。"""
    create_resp = {
        "task_id": "T-br02-test-2", "status": "open", "title": "BR02 t2",
        "step_count": 0, "contract_count": 3,
        "workspace_id": 10, "workspace_instance_id": "my-explicit-inst",
        "workspace_binding_id": "tb-2", "workspace_capture_id": "wc-2",
        "assignment_id": None,
    }
    captured = _fake_create_flow(
        monkeypatch,
        resolve_pair={"workspace_id": 10, "workspace_instance_id": "ignored"},
        create_response=create_resp,
        readback_response=create_resp,
    )
    _handle_task(["create", "--title", "t2",
                  "--workspace-instance-id", "my-explicit-inst"], None)
    assert captured["params"]["workspace_id"] == 10      # 缺省 → daemon 解析
    assert captured["params"]["workspace_instance_id"] == "my-explicit-inst"  # 显式保留


# ----------------------------------------------------------------------
# A2：create output includes task/binding/capture/assignment/step identifiers
# ----------------------------------------------------------------------

def test_render_create_provenance_includes_identifiers(capsys):
    """A2：渲染输出含 workspace/binding/capture/assignment/step 标识。"""
    create_resp = {
        "task_id": "T-x", "workspace_id": 10,
        "workspace_instance_id": "inst-1",
        "workspace_binding_id": "tb-x", "workspace_capture_id": "wc-x",
        "assignment_id": "asg-x", "step_count": 3,
    }
    _render_create_provenance(create_resp)
    out = capsys.readouterr().out
    for token in ("workspace_id", "workspace_instance_id", "workspace_binding_id",
                  "workspace_capture_id", "assignment_id", "step_count"):
        assert token in out
    for value in ("10", "inst-1", "tb-x", "wc-x", "asg-x", "3"):
        assert value in out


# ----------------------------------------------------------------------
# A3：readback mismatch fails closed
# ----------------------------------------------------------------------

def test_readback_consistent_passes(monkeypatch):
    """A3：create 与 task.status readback 完全一致 → 无异常。"""
    create_resp = {
        "task_id": "T-ok", "workspace_id": 10, "workspace_instance_id": "inst",
        "workspace_binding_id": "tb-ok", "workspace_capture_id": "wc-ok",
        "assignment_id": None,
    }

    class _Client:
        def call(self, method, params):
            return create_resp

    monkeypatch.setattr("callwarden.server.daemon_client._get_rpc_client_for_route",
                        lambda: _Client())
    _verify_create_readback(create_resp, workspace_id=10, workspace_instance_id="inst")


def test_readback_binding_mismatch_fails_closed(monkeypatch):
    """A3：readback workspace_binding_id 不一致 → fail-closed 上抛。"""
    create_resp = {
        "task_id": "T-bad", "workspace_id": 10, "workspace_instance_id": "inst",
        "workspace_binding_id": "tb-create", "workspace_capture_id": "wc-x",
        "assignment_id": None,
    }
    readback = dict(create_resp, workspace_binding_id="tb-OTHER")

    class _Client:
        def call(self, method, params):
            return readback

    monkeypatch.setattr("callwarden.server.daemon_client._get_rpc_client_for_route",
                        lambda: _Client())
    with pytest.raises(RuntimeError, match="workspace_binding_id"):
        _verify_create_readback(create_resp, workspace_id=10, workspace_instance_id="inst")


def test_readback_missing_key_fails_closed(monkeypatch):
    """A3：create 返回了某键但 readback 缺省（非 assignment_id）→ fail-closed。"""
    create_resp = {
        "task_id": "T-bad2", "workspace_id": 10, "workspace_instance_id": "inst",
        "workspace_binding_id": "tb-x", "workspace_capture_id": "wc-x",
        "assignment_id": None,
    }
    readback = {k: v for k, v in create_resp.items() if k != "workspace_capture_id"}

    class _Client:
        def call(self, method, params):
            return readback

    monkeypatch.setattr("callwarden.server.daemon_client._get_rpc_client_for_route",
                        lambda: _Client())
    with pytest.raises(RuntimeError, match="workspace_capture_id"):
        _verify_create_readback(create_resp, workspace_id=10, workspace_instance_id="inst")


def test_readback_assignment_null_tolerated(monkeypatch):
    """A3：assignment_id 两处均为 null/缺省 → 容忍（无 assignment_queued 事件）。"""
    create_resp = {
        "task_id": "T-null", "workspace_id": 10, "workspace_instance_id": "inst",
        "workspace_binding_id": "tb-n", "workspace_capture_id": "wc-n",
        "assignment_id": None,
    }
    readback = dict(create_resp)
    readback.pop("assignment_id", None)

    class _Client:
        def call(self, method, params):
            return readback

    monkeypatch.setattr("callwarden.server.daemon_client._get_rpc_client_for_route",
                        lambda: _Client())
    _verify_create_readback(create_resp, workspace_id=10, workspace_instance_id="inst")


# ----------------------------------------------------------------------
# A4：daemon unavailable never creates a local task
# ----------------------------------------------------------------------

def test_daemon_unavailable_no_local_task(monkeypatch):
    """A4：resolve 失败（daemon 不可达）→ DaemonUnavailableError，绝不落本地。"""
    def boom():
        raise DaemonUnavailableError("daemon down")

    route_called = []

    def fake_route(method, params, fallback):
        route_called.append(method)  # 不应被调用
        return fallback()

    monkeypatch.setattr("callwarden.cli.main.resolve_workspace_pair_from_daemon", boom)
    monkeypatch.setattr("callwarden.cli.main.route_task_write", fake_route)
    with pytest.raises(DaemonUnavailableError):
        _handle_task(["create", "--title", "t"], None)
    assert route_called == []  # 未进入任何创建路径（含本地）


def test_local_fallback_forbidden(monkeypatch):
    """A4：local 模式走到 fallback 分支时抛错，而非调用 db.task_create 本地建任务。"""
    captured = {"fallback_called": False}

    def fake_resolve():
        return {"workspace_id": 10, "workspace_instance_id": "ws-10"}

    def fake_route(method, params, fallback):
        captured["fallback_called"] = True
        return fallback()  # 模拟 local 模式直接执行 fallback

    monkeypatch.setattr("callwarden.cli.main.resolve_workspace_pair_from_daemon",
                        fake_resolve)
    monkeypatch.setattr("callwarden.cli.main.route_task_write", fake_route)
    with pytest.raises(DaemonUnavailableError, match="绝不本地建任务"):
        _handle_task(["create", "--title", "t"], None)
    assert captured["fallback_called"] is True


def test_create_task_with_steps_output_step_count(monkeypatch, capsys):
    """A2/A4 组合：带 steps 的 create 输出含 step_count 标识且不触碰本地 db。"""
    create_resp = {
        "task_id": "T-steps", "status": "open", "title": "t",
        "step_count": 2, "contract_count": 3,
        "workspace_id": 10, "workspace_instance_id": "inst",
        "workspace_binding_id": "tb-s", "workspace_capture_id": "wc-s",
        "assignment_id": None,
    }
    captured = _fake_create_flow(
        monkeypatch,
        resolve_pair={"workspace_id": 10, "workspace_instance_id": "inst"},
        create_response=create_resp,
        readback_response=create_resp,
    )
    steps = '[{"action":"edit","target_file":"a.py"},{"action":"test","target_file":"a.py"}]'
    _handle_task(["create", "--title", "t", "--steps", steps], None)
    out = capsys.readouterr().out
    assert "step_count" in out and "2" in out
    assert captured["params"]["steps"] == [
        {"action": "edit", "target_file": "a.py"},
        {"action": "test", "target_file": "a.py"},
    ]
