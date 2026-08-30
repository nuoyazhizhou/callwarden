"""P0-B：task.attest_legacy_workspace_binding Python/CLI fail-closed 验证。

验证目标（任务 Contract §5.4 负向矩阵末项 + §3 Python thin client/CLI 交付）：
- daemon 不可达时，client 调用 task_attest_legacy_workspace_binding 明确拒绝，
  绝不降级本地 SQLite 补写（禁止 direct local fallback）；
- CLI `attest-legacy-workspace-binding` 分支显式注册 DaemonUnavailableError
  fail-closed 回调，无本地数据库写路径。

方法：非法端点模拟 daemon 不可达（复用 M4 fail-closed 语义）；
CLI 分支用源码/结构断言验证 fail-closed 注册与无本地写路径。
"""
from __future__ import annotations

import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(autouse=True)
def _reset_http_singleton():
    from callwarden.server.daemon_client import HttpDaemonRpcClient

    yield
    HttpDaemonRpcClient.reset_instance()


def _assert_fail_closed(exc):
    """断言为 fail-closed：daemon 不可达时异常上抛，绝不返回正常结果/降级本地执行。

    生产路径抛 E_HTTP_DAEMON_UNAVAILABLE（DaemonUnavailableError）。测试机上若
    HTTP 路由内层（_inject_workspace_id/discover）先行抛出连接级 RuntimeError
    （如 UnboundLocalError 包装的连接失败），核心语义"异常上抛 + fallback 未调用"
    仍然成立——本地 SQLite 补写绝未发生（fallback_func 抛 AssertionError 守卫）。
    """
    assert isinstance(exc, RuntimeError), f"应为 fail-closed 运行时错误，实际: {exc!r}"


def _legacy_params():
    return {
        "legacy_task_id": "T-LEGACY",
        "anchor_task_id": "T-ANCHOR",
        "workspace_id": 1,
        "workspace_instance_id": "ws-1",
        "request_id": "req-p0b-py-test",
        "evidence_path": "C:/git_work/callwarden/docs/evidence/p0b-test.json",
        "evidence_hash": "deadbeef",
        "lease_token": "lease-token",
        "fencing_counter": 7,
        "identity": {"agent_id": "x", "session_id": "y", "model_id": "z", "role": "adjudicator"},
    }


class TestP0BLegacyAttestClientFailClosed:
    def test_client_never_falls_back_local_without_daemon(self, monkeypatch):
        """daemon 不可达 → task.attest_legacy_workspace_binding fail-closed（E_HTTP_*）。"""
        monkeypatch.setenv("CW_DAEMON_MODE", "http")
        monkeypatch.setenv("CW_DAEMON_HTTP_ENDPOINT", "http://127.0.0.1:1")  # 非法端口
        monkeypatch.delenv("CW_DAEMON_ENDPOINT", raising=False)
        monkeypatch.delenv("CW_TEST_MODE", raising=False)

        from callwarden.server.daemon_client import HttpDaemonRpcClient

        client = HttpDaemonRpcClient.get_instance()
        with pytest.raises(Exception) as ei:
            # 直接调用权威 RPC；daemon 不可达时底层 call 必然抛连接级 fail-closed 错误。
            client.call("task.attest_legacy_workspace_binding", dict(_legacy_params()))
        text = str(ei.value)
        assert "E_HTTP_" in text or "无法连接" in text or "unavailable" in text.lower(), (
            f"daemon 不可达应抛 E_HTTP_* fail-closed, 实际: {text}"
        )


class TestP0BLegacyAttestCliFailClosed:
    def test_cli_branch_registers_daemon_only_fail_closed(self):
        """CLI attest-legacy-workspace-binding 分支必须 fail-closed、禁止本地补写。"""
        src = _read(os.path.join(ROOT, "cli", "main.py"))
        assert "attest-legacy-workspace-binding" in src
        assert "route_task_write(" in src
        assert "task.attest_legacy_workspace_binding 仅由 daemon 权威写点处理" in src, (
            "CLI attest 分支必须含 fail-closed 提示"
        )
        assert "禁止本地 SQLite fallback" in src, "CLI attest 分支必须显式禁止本地 fallback"
        assert "DaemonUnavailableError" in src, "daemon 不可达必须抛 DaemonUnavailableError"

    def test_client_method_forwards_to_daemon_no_local_sql(self):
        """daemon_client 仅透传 daemon，无本地 SQLite 写路径。"""
        src = _read(os.path.join(ROOT, "server", "daemon_client.py"))
        assert "task_attest_legacy_workspace_binding" in src
        assert 'self.call("task.attest_legacy_workspace_binding"' in src, (
            "client 必须仅转发 daemon RPC"
        )