"""Route-level fail-closed semantics for the single-track HTTPS+mTLS daemon client.

ADR-002 单轨传输：唯一 daemon RPC = 隔离 HTTPS + mTLS（`HttpDaemonRpcClient`）。

历史说明（已退役用例）：本文件原包含一个
`test_windows_daemon_multi_process_high_concurrency_writer_e2e`，其核心断言建立在
「多进程并发写同一 Windows Named Pipe」、默认管道占用探测与管道释放等待之上
（P1-2 明确禁止杀他人进程、管道被占用则 skip）。ADR-002 删除
`UnixDaemonRpcClient` 与 Named Pipe 传输后，该场景（并发 pipe 写 / pipe 占用）
已不存在，故整体退役。daemon 单写者串行化、task_conflict 冲突错误码与
task_events 无间隙序列语义已由 `test_lease_rpc.py`、`test_lease_gate_empirical.py`
与 `test_cli_task_lease_parity.py` 在 HTTPS 隔离 daemon 上覆盖。

保留的两个用例只验证 `route_task_write/read` 的传输故障与业务错误语义，
与具体传输无关：注入 fake `HttpDaemonRpcClient`，不启动真实 daemon。
"""

import pytest

import callwarden.server.daemon_client as _dc_mod
from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    route_task_read,
    route_task_write,
)
from callwarden.server.daemon_protocol import DaemonRemoteError


class _FakeRemoteErrorClient:
    """模拟 daemon 已连接但返回业务错误（parse_response 抛 DaemonRemoteError）。"""

    def __init__(self, code, message):
        self._code, self._message = code, message

    def call(self, rpc_method, params):
        raise DaemonRemoteError(self._code, self._message)


class _FakeConnErrorClient:
    """模拟连接层故障（daemon 不可达 / 传输异常）。"""

    def call(self, rpc_method, params):
        raise OSError(2, "No such file or directory")


def _patch_client(monkeypatch, fake):
    """把 `HttpDaemonRpcClient.get_instance()` 指向 fake 客户端。"""
    monkeypatch.setattr(
        _dc_mod.HttpDaemonRpcClient, "get_instance",
        lambda *args, **kwargs: fake,
    )


def test_enterprise_auto_fail_closed_without_daemon(monkeypatch):
    """enterprise/auto 模式下 daemon 不可用时写操作严格 Fail-Closed。"""
    called_local = False

    def mock_local():
        nonlocal called_local
        called_local = True
        return "local_written"

    _patch_client(monkeypatch, _FakeConnErrorClient())

    monkeypatch.setenv("CW_DAEMON_MODE", "enterprise")
    monkeypatch.setenv("CW_TASK_WRITE_POLICY", "shared")
    # 显式 workspace_id 短路 `_inject_workspace_id`，精确覆盖传输故障接缝。
    with pytest.raises(DaemonUnavailableError):
        route_task_write("task.create",
                         {"title": "fail_closed_test", "workspace_id": 1},
                         mock_local)
    assert not called_local, "enterprise 模式绝对不得调用 local 写入闭包"

    monkeypatch.setenv("CW_DAEMON_MODE", "auto")
    with pytest.raises(DaemonUnavailableError):
        route_task_write("task.create",
                         {"title": "fail_closed_test", "workspace_id": 1},
                         mock_local)
    assert not called_local, "auto 模式在指定写操作时底层异常绝不得 fallback 本地"

    monkeypatch.setenv("CW_DAEMON_MODE", "local")
    monkeypatch.setenv("CW_TASK_WRITE_POLICY", "isolated")
    res = route_task_write("task.create", {"title": "local_test"}, mock_local)
    assert res == "local_written"
    assert called_local, "local 模式必须使用 local 闭包"


def test_route_task_preserves_daemon_remote_error_code(monkeypatch):
    """route_task_write/read 必须保留 DaemonRemoteError 的结构化错误语义。

    - 远端业务错误（task_conflict / task_not_found）：异常类型 + code 原样透传，
      不得包装成 DaemonUnavailableError；
    - 连接层异常（OSError）在 enterprise/auto 下仍应包装为 DaemonUnavailableError。
    """
    monkeypatch.setenv("CW_DAEMON_MODE", "enterprise")

    # ---- 写路径：业务错误原样透传，绝不调用 local 闭包，绝不伪装成连接失败 ----
    _patch_client(monkeypatch, _FakeRemoteErrorClient("task_conflict",
                                                      "任务已被其他 agent 抢占"))
    with pytest.raises(DaemonRemoteError) as ei:
        route_task_write("task.claim",
                         {"task_id": "T-1", "workspace_id": 1},
                         lambda: None)
    assert ei.value.code == "task_conflict", f"写路径 code 丢失: {ei.value.code}"

    # ---- 读路径（auto）：业务错误必须透传，不得降级为本地读（数据可能不一致）----
    monkeypatch.setenv("CW_DAEMON_MODE", "auto")
    _patch_client(monkeypatch, _FakeRemoteErrorClient("task_not_found",
                                                      "任务不存在"))
    with pytest.raises(DaemonRemoteError) as ei:
        route_task_read("task.status",
                        {"task_id": "T-999", "workspace_id": 1},
                        lambda: "LOCAL_READ")
    assert ei.value.code == "task_not_found", f"读路径 code 丢失: {ei.value.code}"

    # ---- 连接层异常仍必须包装为 DaemonUnavailableError（Fail-Closed 回归）----
    _patch_client(monkeypatch, _FakeConnErrorClient())
    with pytest.raises(DaemonUnavailableError):
        route_task_write("task.create",
                         {"title": "x", "workspace_id": 1},
                         lambda: "LOCAL")
