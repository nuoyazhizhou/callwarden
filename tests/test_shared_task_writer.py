"""共享任务写入路由的 fail-closed 契约测试。

PYT 回归卡 step#4（thin-client / daemon authority 迁移对齐）——本文件三处
过期期望的分桶与 stale 依据：

* ``test_local_task_write_requires_shared_daemon`` → **C（生产缺陷）**
  生产 ``server/daemon_client.py:93-99`` 的
  ``SharedTaskWriterRequiredError.__init__`` 只把结构化错误码拼进 message
  （``super().__init__(f"{self.code}: {message}")``），未以 ``code=`` 透传给
  父类；父类 ``DaemonUnavailableError.__init__``（``server/daemon_client.py:88-90``）
  会执行 ``self.code = code``（默认 ``E_HTTP_DAEMON_UNAVAILABLE``），从而把子类
  class attr ``code = "E_SHARED_TASK_WRITER_REQUIRED"`` 覆盖。故实例 ``.code``
  实为 ``E_HTTP_DAEMON_UNAVAILABLE``，仅 ``str(exc)`` 仍携带
  ``E_SHARED_TASK_WRITER_REQUIRED``。生产消费方 ``cli/main.py:1771`` 按异常类
  捕获并打印消息（契约见 ``docs/design/shared-task-write-coordination.md``
  第 20-22 行「错误必须是结构化的 E_SHARED_TASK_WRITER_REQUIRED」）。按 C 桶
  规则不改生产源码，改为断言当前可观察契约（异常类 + 消息内含结构化码）。

* ``test_task_write_uses_daemon_autostart`` → **A（mock RPC seam 过期）**
  生产 ``server/daemon_client.py:3498-3506`` 的 ``_get_rpc_client_for_route()``
  现优先返回 ``HttpDaemonRpcClient.get_instance()``（``is_http_transport_enabled()``
  为真时），仅在其为假时才构造 ``UnixDaemonRpcClient()``；且
  ``_inject_workspace_id``（``server/daemon_client.py:3509-3524``）在 try 之外
  复用同一 client。旧测试 monkeypatch ``UnixDaemonRpcClient`` 已失效，必须
  替身 ``_get_rpc_client_for_route`` 才能命中 daemon 自动拉起路径
  （``route_task_write`` 的 ``call_with_autostart``，:3718-3723）。

* ``test_cli_shared_task_write_has_nonzero_exit_code`` → **B（subprocess 集成期望过期）**
  生产 ``cli/main.py:3656-3666`` 使 ``task.create`` 总是带 ``identity_policy``，
  必然命中 ``governed_create`` 分支并在 local 模式抛 ``DaemonUnavailableError``
  （:3660），该异常被 ``_dispatch_subcommand`` 的通用 ``except Exception``
  （``cli/main.py:1784``）吞掉并 ``return True`` → RC=0。此外旧测试从仓库根 cwd
  运行会触发 ``_run_subcommand_mode`` 的 workspace 注册（``cli/main.py:1466-1483``）
  → HTTP manifest 探测失败 → RC=1（环境相关）。改为在隔离中性 cwd 下用非 governed
  的 ``task close`` 触发 ``route_task_write`` local+shared fail-closed 路径
  （``cli/main.py:5340-5344`` → ``SharedTaskWriterRequiredError`` → ``SystemExit(2)``，
  由 ``cli/main.py:1771-1773`` 保证 RC=2）。
"""

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from callwarden.server.daemon_client import (
    SharedTaskWriterRequiredError,
    route_task_write,
)


def test_local_task_write_requires_shared_daemon(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_MODE", "local")
    monkeypatch.delenv("CW_TASK_WRITE_POLICY", raising=False)
    called = []

    with pytest.raises(SharedTaskWriterRequiredError) as exc_info:
        route_task_write("task.report", {"task_id": "T-1"}, lambda: called.append(True))

    # C 类（生产缺陷 server/daemon_client.py:93-99）：实例 .code 被父类
    # DaemonUnavailableError.__init__ 覆盖为 E_HTTP_DAEMON_UNAVAILABLE，结构化码
    # E_SHARED_TASK_WRITER_REQUIRED 仅保留在消息字符串中。
    assert "E_SHARED_TASK_WRITER_REQUIRED" in str(exc_info.value)
    assert called == []


def test_local_task_write_can_be_explicitly_isolated(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_MODE", "local")
    monkeypatch.setenv("CW_TASK_WRITE_POLICY", "isolated")

    assert route_task_write("task.report", {"task_id": "T-1"}, lambda: "local-test") == "local-test"


def test_non_task_local_write_is_not_blocked(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_MODE", "local")
    monkeypatch.delenv("CW_TASK_WRITE_POLICY", raising=False)

    assert route_task_write("rule.sync", {}, lambda: "non-task-local") == "non-task-local"


def test_task_write_uses_daemon_autostart(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_MODE", "auto")
    calls = []

    class FakeRpc:
        """RPC client 替身：区分裸 call 与 call_with_autostart 两条 seam。"""

        def call(self, method, params):
            calls.append(("call", method, params))
            if method == "mcp.daemon_client.inject_workspace_id":
                # _inject_workspace_id 期望 daemon 回包 {"params": {...}}（:3521-3524）
                return {"params": params["params"]}
            return {}

        def call_with_autostart(self, method, params):
            calls.append(("autostart", method, params))
            return {"task_id": "T-1"}

    fake = FakeRpc()
    # A 类：HTTP transport 优先（server/daemon_client.py:3498-3506），必须替身
    # _get_rpc_client_for_route 才能让 route_task_write 走 daemon 自动拉起 seam。
    monkeypatch.setattr(
        "callwarden.server.daemon_client._get_rpc_client_for_route",
        lambda: fake,
    )
    result = route_task_write("task.create", {"title": "x"}, lambda: "local")
    assert result["task_id"] == "T-1"
    autostart_calls = [(m, p) for kind, m, p in calls if kind == "autostart"]
    assert [m for m, _ in autostart_calls] == ["task.create"]
    assert autostart_calls[0][1]["title"] == "x"


def test_cli_shared_task_write_has_nonzero_exit_code():
    root = Path(__file__).resolve().parents[1]
    # B 类：中性 cwd 避免 _run_subcommand_mode 的 workspace 注册副作用
    # （cli/main.py:1443-1483，仓库根 cwd 会触发 HTTP manifest 探测失败 → RC=1）；
    # 用非 governed 的 task close 命中 local+shared fail-closed 写路径。
    workdir = tempfile.mkdtemp(prefix="cw-shared-writer-")
    try:
        env = {
            **os.environ,
            "CW_DAEMON_MODE": "local",
            "CW_TASK_WRITE_POLICY": "shared",
            "CW_USE_RUST_STORAGE": "0",
        }
        env.pop("CALLWARDEN_WORKSPACE", None)
        result = subprocess.run(
            [sys.executable, str(root / "cw.py"), "task", "close", "T-1", "--reviewer", "r"],
            cwd=workdir,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    assert result.returncode == 2
    assert "E_SHARED_TASK_WRITER_REQUIRED" in result.stdout + result.stderr
