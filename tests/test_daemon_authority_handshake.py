"""W2/W2.3/W2.4/D0 共存契约子任务1：Authority/transport handshake 测试。

对应 windows-wsl-daemon-coexistence-contract.md §3.1/§5.3。
覆盖：
- hello() 从 ping 响应提取 authority_id/transport/task_db_fingerprint
- verify_authority 在 authority_id 不一致时 fail-closed（E_AUTHORITY_MISMATCH）
- verify_authority 在 task_db_fingerprint 不一致时 fail-closed
- daemon 未返回 authority_id 时 hello 抛 DaemonUnavailableError
"""
import pytest

from callwarden.server.daemon_client import UnixDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError


class _FakeClient:
    """最小 fake：记录 hello/verify_authority 行为，不依赖真实 daemon。"""

    def __init__(self, hello_result=None, call_error=None):
        self._hello_result = hello_result
        self._call_error = call_error
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if self._call_error:
            raise self._call_error
        return self._hello_result


def test_hello_extracts_authority_fields():
    """hello() 应从 ping 响应提取 authority 字段。"""
    fake = _FakeClient(
        hello_result={
            "status": "ok",
            "peer_uid": 1000,
            "authority_id": "host/windows/user-1/db-abc",
            "transport": "named-pipe",
            "task_db_fingerprint": "fp-123",
            "protocol_version": 1,
        }
    )
    # 用 fake 替换 client 的 call，模拟真实 daemon 响应
    client = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
    client.socket_path = "/fake.sock"
    client.timeout = 5
    client.max_message_bytes = 1 << 20
    client._ids = __import__("itertools").count(1)
    client.call = fake.call

    info = client.hello()
    assert info["authority_id"] == "host/windows/user-1/db-abc"
    assert info["transport"] == "named-pipe"
    assert info["task_db_fingerprint"] == "fp-123"
    assert info["protocol_version"] == 1
    assert fake.calls[0][0] == "ping"


def test_hello_fails_closed_when_authority_missing():
    """daemon 未返回 authority_id 时，hello 必须 fail-closed。"""
    fake = _FakeClient(hello_result={"status": "ok", "pid": 1})
    client = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
    client.socket_path = "/fake.sock"
    client.timeout = 5
    client.max_message_bytes = 1 << 20
    client._ids = __import__("itertools").count(1)
    client.call = fake.call

    from callwarden.server.daemon_client import DaemonUnavailableError

    with pytest.raises(DaemonUnavailableError):
        client.hello()


def test_verify_authority_rejects_authority_mismatch():
    """authority_id 不一致时 fail-closed（E_AUTHORITY_MISMATCH）。"""
    fake = _FakeClient(
        hello_result={
            "authority_id": "daemon-a",
            "task_db_fingerprint": "fp-a",
            "transport": "uds",
            "protocol_version": 1,
            "platform": "linux",
        }
    )
    client = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
    client.socket_path = "/fake.sock"
    client.timeout = 5
    client.max_message_bytes = 1 << 20
    client._ids = __import__("itertools").count(1)
    client.call = fake.call

    with pytest.raises(DaemonRemoteError) as exc_info:
        client.verify_authority(expected_authority_id="daemon-b")
    assert exc_info.value.code == "E_AUTHORITY_MISMATCH"
    assert "daemon-a" in exc_info.value.message


def test_verify_authority_rejects_fingerprint_mismatch():
    """task_db_fingerprint 不一致时 fail-closed。"""
    fake = _FakeClient(
        hello_result={
            "authority_id": "daemon-a",
            "task_db_fingerprint": "fp-a",
            "transport": "uds",
            "protocol_version": 1,
            "platform": "linux",
        }
    )
    client = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
    client.socket_path = "/fake.sock"
    client.timeout = 5
    client.max_message_bytes = 1 << 20
    client._ids = __import__("itertools").count(1)
    client.call = fake.call

    with pytest.raises(DaemonRemoteError) as exc_info:
        client.verify_authority(expected_fingerprint="fp-b")
    assert exc_info.value.code == "E_AUTHORITY_MISMATCH"
    assert "fp-a" in exc_info.value.message


def test_verify_authority_passes_when_matching():
    """authority 和 fingerprint 均匹配时通过，返回 hello 信息。"""
    fake = _FakeClient(
        hello_result={
            "authority_id": "daemon-a",
            "task_db_fingerprint": "fp-a",
            "transport": "uds",
            "protocol_version": 1,
            "platform": "linux",
        }
    )
    client = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
    client.socket_path = "/fake.sock"
    client.timeout = 5
    client.max_message_bytes = 1 << 20
    client._ids = __import__("itertools").count(1)
    client.call = fake.call

    info = client.verify_authority(
        expected_authority_id="daemon-a", expected_fingerprint="fp-a"
    )
    assert info["authority_id"] == "daemon-a"
