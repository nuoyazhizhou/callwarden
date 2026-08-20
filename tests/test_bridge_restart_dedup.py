"""共存契约子任务5：Bridge restart 与 request dedup 测试。

对应 windows-wsl-daemon-coexistence-contract.md §6.3 与
windows-wsl-daemon-coexistence-task-plan.md 子任务5。

覆盖：
- mutation_call 自动注入 request_id，同一请求重试复用同一 request_id；
- 重连后 authority pin 校验（E_AUTHORITY_MISMATCH fail-closed）；
- 重连成功后复用 request_id 提交（daemon 侧 dedup 返回已提交结果）；
- 重连失败时 DaemonUnavailableError（fail-closed，不盲目重复）。
"""
import os
import socket
import time

import pytest

from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    UnixDaemonRpcClient,
)
from callwarden.server.daemon_protocol import DaemonRemoteError


class _FakeMutationClient(UnixDaemonRpcClient):
    """伪造 client：记录调用并模拟重连/authority 校验。"""

    def __init__(self):
        super().__init__(socket_path="/fake.sock")
        self.calls = []
        self.verify_calls = 0
        self.authority_result = {
            "authority_id": "daemon-a",
            "task_db_fingerprint": "fp-a",
            "transport": "uds",
            "protocol_version": 1,
            "platform": "linux",
        }
        self.fail_call_times = 0  # 前 N 次 call 抛连接错误
        self.fail_verify_times = 0  # 前 N 次 verify 抛错误
        self.verify_error = None
        self.change_authority_after_calls = None  # 在第 N 次 call 后切换 authority_id

    def hello(self):
        if self.verify_error:
            raise self.verify_error
        return self.authority_result

    def verify_authority(self, expected_authority_id=None, expected_fingerprint=None):
        self.verify_calls += 1
        if self.fail_verify_times > 0:
            self.fail_verify_times -= 1
            raise DaemonUnavailableError("verify 连接失败")
        # 复用真实验证逻辑：mismatch 抛 E_AUTHORITY_MISMATCH
        info = self.hello()
        if expected_authority_id and info["authority_id"] != expected_authority_id:
            raise DaemonRemoteError(
                "E_AUTHORITY_MISMATCH",
                f"authority 不一致: daemon={info['authority_id']}, expected={expected_authority_id}",
            )
        if expected_fingerprint and info["task_db_fingerprint"] != expected_fingerprint:
            raise DaemonRemoteError(
                "E_AUTHORITY_MISMATCH",
                f"task_db_fingerprint 不一致: daemon={info['task_db_fingerprint']}, expected={expected_fingerprint}",
            )
        return info

    def call(self, method, params=None):
        self.calls.append((method, params))
        # 在第 N 次 call 后切换 authority（模拟重连到错误 authority）
        if self.change_authority_after_calls is not None:
            self.change_authority_after_calls -= 1
            if self.change_authority_after_calls == 0:
                self.authority_result["authority_id"] = "daemon-b"
        if self.fail_call_times > 0:
            self.fail_call_times -= 1
            raise DaemonUnavailableError("call 连接失败")
        # 模拟真实 parse_response：返回 result 部分（不含 ok 信封）
        return {"committed": True, "request_id": params.get("request_id")}


def test_mutation_call_injects_request_id():
    client = _FakeMutationClient()
    client.mutation_call("task.claim", {"task_id": "T1"})
    method, params = client.calls[-1]
    assert method == "task.claim"
    assert params["request_id"].startswith("req-")


def test_mutation_call_reuses_existing_request_id():
    client = _FakeMutationClient()
    client.mutation_call("task.report", {"task_id": "T1", "request_id": "req-fixed"})
    _, params = client.calls[-1]
    assert params["request_id"] == "req-fixed"


def test_mutation_call_retries_with_same_request_id():
    """前一次 mutation 连接失败，重试复用同一 request_id（dedup 幂等）。"""
    client = _FakeMutationClient()
    # 前 2 次 call 失败：mutation attempt0 失败 + task.status read 失败 →
    # 进入重连，mutation attempt1 复用同一 request_id
    client.fail_call_times = 2
    client.mutation_call("task.claim", {"task_id": "T1"}, reconnect_attempts=2)
    mutations = [(m, p) for m, p in client.calls if m == "task.claim"]
    assert len(mutations) >= 2
    req_ids = {p.get("request_id") for _, p in mutations}
    assert len(req_ids) == 1, f"mutation 重试应复用同一 request_id: {req_ids}"


def test_mutation_call_queries_outcome_before_retry():
    """契约 §6.3：连接中断后先用独立 read RPC 查询结果，再决定是否重试。"""
    client = _FakeMutationClient()
    # 第一次 call（mutation）连接失败；随后 outcome 查询走 task.status read 成功
    client.fail_call_times = 1
    result = client.mutation_call("task.claim", {"task_id": "T1"})
    # 第二次调用应是 read RPC task.status（真实查询，非重放 mutation）
    assert result["committed"] is True
    assert client.calls[0][0] == "task.claim"  # 第一次是 mutation
    assert client.calls[1][0] == "task.status"  # 第二次是 read outcome 查询
    assert "request_id" not in client.calls[1][1]  # read 查询不携带 request_id


def test_mutation_call_outcome_query_failure_then_retry():
    """outcome 查询也失败（仍不可达）→ 重连后重试 mutation。"""
    client = _FakeMutationClient()
    # 前 2 次 mutation call 失败（含 outcome read 失败），第 3 次成功
    client.fail_call_times = 2
    client.mutation_call("task.claim", {"task_id": "T1"}, reconnect_attempts=3)
    assert len(client.calls) >= 3
    # 所有 mutation 尝试复用同一 request_id
    mutations = [(m, p) for m, p in client.calls if m == "task.claim"]
    req_ids = {p.get("request_id") for _, p in mutations}
    assert len(req_ids) == 1, f"mutation 重试应复用同一 request_id: {req_ids}"


def test_mutation_call_rejects_authority_mismatch():
    """authority pin 不一致 → E_AUTHORITY_MISMATCH fail-closed。"""
    client = _FakeMutationClient()
    client.authority_result["authority_id"] = "daemon-b"
    with pytest.raises(DaemonRemoteError) as exc_info:
        client.mutation_call("task.claim", {"task_id": "T1"}, expected_authority_id="daemon-a")
    assert exc_info.value.code == "E_AUTHORITY_MISMATCH"
    # 不应发起 call
    assert client.calls == []


def test_mutation_call_outcome_query_rejects_authority_mismatch():
    """评审二轮：outcome 查询阶段 authority 变化 → fail-closed，不向错误 authority 查询。"""
    client = _FakeMutationClient()
    # 第一次 mutation 连接失败，进入 outcome 查询
    client.fail_call_times = 1
    # 在第一次 mutation call 后切换 authority（模拟重连到错误 authority）
    client.change_authority_after_calls = 1
    with pytest.raises(DaemonRemoteError) as exc_info:
        client.mutation_call(
            "task.claim",
            {"task_id": "T1"},
            expected_authority_id="daemon-a",
            reconnect_attempts=2,
        )
    assert exc_info.value.code == "E_AUTHORITY_MISMATCH"
    # 只发生 1 次 mutation 尝试（outcome 查询被 authority 校验拒绝，未发起 read）
    mutations = [(m, p) for m, p in client.calls if m == "task.claim"]
    assert len(mutations) == 1
    # 未发起 task.status read（authority mismatch 阻止查询）
    reads = [m for m, _ in client.calls if m == "task.status"]
    assert reads == []


def test_mutation_call_raises_after_reconnect_exhausted():
    """重连耗尽后 DaemonUnavailableError（fail-closed，不盲目重复）。"""
    client = _FakeMutationClient()
    client.fail_call_times = 99
    with pytest.raises(DaemonUnavailableError):
        client.mutation_call("task.claim", {"task_id": "T1"}, reconnect_attempts=2)
    # 至少 2 次 mutation 尝试（外加 outcome read 尝试）
    assert len(client.calls) >= 2
    # mutation 尝试复用同一 request_id（dedup 幂等）
    mutations = [(m, p) for m, p in client.calls if m == "task.claim"]
    req_ids = {p.get("request_id") for _, p in mutations}
    assert len(req_ids) == 1, f"重试应复用同一 request_id: {req_ids}"


def test_mutation_call_verify_authority_called():
    """每次 mutation 调用前都执行 authority pin 校验。"""
    client = _FakeMutationClient()
    client.mutation_call("task.claim", {"task_id": "T1"})
    assert client.verify_calls >= 1
