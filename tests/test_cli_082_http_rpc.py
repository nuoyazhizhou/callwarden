"""CLI-082（T-1787322799580-d7f9eeec）：cw create → task.create thin client 契约。

验证 _handle_task 的 create 分支（_local_create）：
1. daemon 路径：route_task_write 调 task.create，title/description/steps/creator 透传，
   {task_id, status, title, step_count} 格式化输出。
2. invalid input：daemon 返回 {} / 缺 task_id 时不崩溃（task_id 空串）。
3. wrong/unknown authority：DaemonRemoteError（method_not_found）原样格式化输出。
4. daemon unavailable：DaemonUnavailableError fail-closed 提示（禁止本地回退）。
5. restart 后行为一致：handler 无本地状态，重复调用输出一致。
6. local fallback 为 forbidden 回调：禁止 db.task_create 本地业务路径。
7. HTTP {result, degraded} 包装解包 + degraded fail-closed。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callwarden.cli import main as main_mod  # noqa: E402
from callwarden.i18n import set_language  # noqa: E402

set_language("zh_CN")


@pytest.fixture(autouse=True)
def _stub_route_rpc_client(monkeypatch):
    """A 桶：把路由 RPC client 替换为最小替身（daemon 权威 readback 短路）。

    stale 依据（A 桶：薄客户端 RPC seam）：生产已 daemon authority 化。
    `cli/main.py:3798-3811 _verify_create_readback` 在 create 回包为 dict 时经
    `server.daemon_client._get_rpc_client_for_route()` 发 `task.status` readback RPC；
    `cli/main.py:4589` 在缺显式 workspace pair 时经
    `resolve_workspace_pair_from_daemon`（server/daemon_client.py:3551-3573）发
    `workspace.list`。无 live daemon 时二者均 fail-closed
    `E_HTTP_MANIFEST_MISSING`。本文件只验证 create 分支的
    `route_task_write("task.create", ...)` 透传 + 渲染契约，故以替身满足 readback
    （回 {}；用例回包无 workspace_* 键 → 不参与 provenance 比对），并由
    `_handle_create` 显式传 workspace pair 跳过 pair 解析 RPC。
    """
    from callwarden.server import daemon_client as dc

    class _StubClient:
        def call(self, method, params=None, **kwargs):
            if method == "task.status":
                return {}
            raise AssertionError(f"未预期的 RPC: {method}")

    monkeypatch.setattr(dc, "_get_rpc_client_for_route", lambda: _StubClient())


def _handle_create(title="t", desc="d", steps="[]", **extra_argv):
    # 显式 workspace pair：跳过 cli/main.py:4589 的 resolve_workspace_pair_from_daemon
    # （见 _stub_route_rpc_client stale 依据）。
    argv = ["create", "--title", title, "--desc", desc, "--steps", steps,
            "--workspace-id=7", "--workspace-instance-id=ws-inst-cli082"]
    argv += [f"--{k}={v}" for k, v in extra_argv.items()]
    return main_mod._handle_task(argv, main_mod.RpcDBProxy(workspace_root="C:/git_work/x"))


def test_cli082_routes_to_daemon(monkeypatch, capsys):
    """daemon 路径：task.create，title/description/steps/creator 透传，输出格式化。"""
    captured = {}
    calls = []

    def _fake_route_write(method, params, fallback):
        captured["method"] = method
        captured["params"] = params
        calls.append(method)
        return {
            "task_id": "T-1",
            "status": "open",
            "title": "t",
            "step_count": 0,
        }

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    assert _handle_create() is True
    assert "task.create" in calls
    assert captured["params"].get("title") == "t"
    assert captured["params"].get("description") == "d"
    assert captured["params"].get("steps") == []
    assert captured["params"].get("creator") == "agent"
    assert captured["params"].get("identity_policy") == "legacy_identity_v1"
    out = capsys.readouterr().out
    assert "T-1" in out
    assert "t" in out


def test_cli082_steps_transmitted(monkeypatch, capsys):
    """steps JSON 数组透传给 daemon（不本地解析执行）。"""
    captured = {}
    steps_json = '[{"action": "annotate", "target_file": "a.py"}]'

    def _fake_route_write(method, params, fallback):
        captured["params"] = params
        return {"task_id": "T-2", "status": "open", "title": "t", "step_count": 1}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    _handle_create(steps=steps_json)
    assert captured["params"].get("steps") == [
        {"action": "annotate", "target_file": "a.py"}]
    out = capsys.readouterr().out
    assert "1" in out  # 步骤数 1


def test_cli082_invalid_input_graceful(monkeypatch, capsys):
    """invalid input：{} / 缺 task_id 时不崩溃（task_id 空串）。"""
    for bad in ({}, {"status": "open"}):
        def _fake_route_write(method, params, fallback):
            return bad

        monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
        assert _handle_create() is True
        capsys.readouterr().out  # 不抛异常即通过


def test_cli082_error_dict_printed(monkeypatch, capsys):
    """daemon 返回 {"error": ...} 结构化错误时格式化输出。"""
    def _fake_route_write(method, params, fallback):
        return {"error": "invalid params"}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    assert _handle_create() is True
    out = capsys.readouterr().out
    assert "invalid params" in out


def test_cli082_wrong_authority_remote_error(monkeypatch):
    """wrong/unknown authority：DaemonRemoteError（method_not_found）原样上抛。

    stale 依据（A 桶：fail-closed 传播）：`cli/main.py:4631-4632` 的 legacy
    （非 governed）create 分支直接 `route_task_write(...)`，daemon 业务拒绝
    **不捕获格式化**（GATE-1B 注释 :4620-4624 明确 legacy root create 保持
    pre-Gate 行为，本卡未重设计）；仅 governed create（:4625-4630）捕获并
    `SystemExit(2)`。故现代期望为 DaemonRemoteError 原样上抛（fail-closed），
    而非打印后返回 True。
    """
    def _fake_route_write(method, params, fallback):
        raise main_mod.DaemonRemoteError("method_not_found", "no such method")

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    with pytest.raises(main_mod.DaemonRemoteError) as ei:
        _handle_create()
    assert ei.value.code == "method_not_found"
    assert "no such method" in str(ei.value)


def test_cli082_daemon_unavailable_fails_closed(monkeypatch):
    """daemon unavailable：DaemonUnavailableError 原样上抛，无本地回退。

    stale 依据（A 桶：fail-closed 传播）：同
    test_cli082_wrong_authority_remote_error —— legacy create 分支不捕获
    （`cli/main.py:4631-4632`），异常直达调用方，证明绝不回退本地 db.task_create。
    """
    def _fake_route_write(method, params, fallback):
        raise main_mod.DaemonUnavailableError("daemon down")

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    with pytest.raises(main_mod.DaemonUnavailableError) as ei:
        _handle_create()
    assert "daemon down" in str(ei.value)


def test_cli082_http_wrapper_unwrapped(monkeypatch, capsys):
    """HTTP client call_with_autostart 的 {result, degraded:false} 包装被解包。"""
    def _fake_route_write(method, params, fallback):
        return {"result": {"task_id": "T-3", "status": "open", "title": "t", "step_count": 0},
                "degraded": False}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    _handle_create()
    out = capsys.readouterr().out
    assert "T-3" in out


def test_cli082_http_degraded_fails_closed(monkeypatch, capsys):
    """HTTP degraded 标记（daemon 不可达）fail-closed 提示，禁止本地回退。"""
    def _fake_route_write(method, params, fallback):
        return {"result": None, "degraded": True,
                "mode": "direct_read", "op_class": "read_only"}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    assert _handle_create() is True
    out = capsys.readouterr().out
    assert "degraded" in out


def test_cli082_restart_consistent(monkeypatch, capsys):
    """restart 后行为一致：handler 无本地状态，重复调用输出一致。"""
    write_count = []

    def _fake_route_write(method, params, fallback):
        write_count.append(method)
        return {"task_id": "T-4", "status": "open", "title": "t", "step_count": 0}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    _handle_create()
    out1 = capsys.readouterr().out
    _handle_create()
    out2 = capsys.readouterr().out
    assert out1 == out2
    # 每次调用都重新走 route_task_write（无结果缓存），等价 daemon restart 后行为一致
    assert write_count.count("task.create") == 2


def test_cli082_local_fallback_forbidden(monkeypatch):
    """local fallback 为 forbidden 回调：禁止 db.task_create 本地业务路径。"""
    captured = {}

    def _fake_route_write(method, params, fallback):
        captured["fb"] = fallback
        return {"task_id": "T-5", "status": "open", "title": "t", "step_count": 0}

    monkeypatch.setattr(main_mod, "route_task_write", _fake_route_write)
    _handle_create()
    with pytest.raises(main_mod.DaemonUnavailableError):
        captured["fb"]()
