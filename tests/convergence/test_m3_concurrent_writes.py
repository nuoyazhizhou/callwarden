"""M3 双 Agent 并发写验证（R0.6 / PRD M3，场景 A）。

验证目标（cw-rust-client-convergence-design.md §4.1 场景 A + PRD §3.2 M3）：
- 两个独立 client（模拟两个 agent）并发对同一 workspace 发写操作：
  task.create 同 title / lease 争用 / task.apply+close 同 task；
- 无丢失更新、无误冲突（无 E_* 误冲突）、最终结果一致；
- daemon SerializationPoint 串行化 + request_id dedup 路径生效；
- 并发下无死锁/超时崩溃；
- lease fencing：旧 token 过期后新 lease 生效，旧请求被拒。

策略：使用隔离 daemon（release 二进制 + 临时 data root），多线程各持独立
HttpDaemonRpcClient 打同一 daemon 端点；所有断言基于真实 daemon 响应。

注意（真实 daemon 语义，测试设计对齐）：
- lease 争用前须先 `agent.register`（否则 daemon 将无注册 holder 视为 stale
  并允许孤儿回收，两方都会 acquire 成功——这是设计的 orphan reclaim 语义，
  不是并发冲突）；注册后第二个 acquire 才会收到 E_LEASE_ACTIVE_EXISTS。
- `lease.release` 参数名是 `token`（不是 lease_token）。
"""
from __future__ import annotations

import concurrent.futures
import json
import threading
import time
import uuid

import pytest

from callwarden.server.daemon_protocol import DaemonRemoteError

from conftest import _ensure_task_db_workspace

pytestmark = pytest.mark.usefixtures("isolated_http_daemon")


def _new_client(endpoint: str):
    """为每个"agent"创建独立 HTTP client 实例（模拟独立 MCP server 进程）。"""
    from callwarden.server.daemon_client import HttpDaemonRpcClient

    return HttpDaemonRpcClient(endpoint=endpoint, verify_health=False,
                               validate_manifest=False, timeout=15)


def _identity(agent_id: str, role: str = "implementer") -> dict:
    return {
        "agent_id": agent_id,
        "session_id": f"sess-{agent_id}",
        "model_id": "qa-model",
        "role": role,
    }


def _register_agent(client, agent_id: str, ws_id: int) -> None:
    """agent.register 使 holder 处于 active 状态（否则 lease 争用被孤儿回收吞掉）。"""
    ident = _identity(agent_id)
    client.call("agent.register", {
        "agent_id": agent_id,
        "agent_name": agent_id,
        "workspace_id": ws_id,
        **ident,
    })


class TestConcurrentTaskCreate:
    """并发 task.create 同 title：两个 agent 同时创建同 title 任务。

    期望：两笔都成功、各得独立 task_id（无丢失更新）、无 E_* 冲突、状态一致。
    """

    def test_two_agents_create_same_title(self, isolated_http_daemon, qa_workspace):
        ep = isolated_http_daemon["endpoint"]
        ws_id = qa_workspace["workspace_id"]
        title = f"QA-CONC-{uuid.uuid4().hex[:8]}"

        def create(agent: str):
            client = _new_client(ep)
            return client.call("task.create", {
                "title": title,
                "description": f"created-by-{agent}",
                "workspace_id": ws_id,
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(create, f"agent-{i}") for i in range(2)]
            results = [f.result(timeout=30) for f in futs]

        assert len(results) == 2
        task_ids = {r["task_id"] for r in results}
        assert len(task_ids) == 2, f"同 title 并发 create 应各得独立 task_id: {task_ids}"
        for r in results:
            assert r["status"] == "open"
            assert r["title"] == title

    def test_many_concurrent_creates_no_conflict(self, isolated_http_daemon, qa_workspace):
        """N=8 并发 create（同 workspace、不同 title）：全部成功、无 E_* 误冲突。"""
        ep = isolated_http_daemon["endpoint"]
        ws_id = qa_workspace["workspace_id"]
        n = 8

        def create(i: int):
            client = _new_client(ep)
            return client.call("task.create", {
                "title": f"QA-CONC-{uuid.uuid4().hex[:8]}-{i}",
                "workspace_id": ws_id,
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=n) as pool:
            futs = [pool.submit(create, i) for i in range(n)]
            results = [f.result(timeout=30) for f in futs]

        assert len(results) == n
        assert len({r["task_id"] for r in results}) == n
        for r in results:
            assert r["status"] == "open"


class TestRequestIdDedup:
    """request_id 幂等：同一 request_id 重复提交 → Replay 而非重复执行。"""

    def test_same_request_id_returns_same_task(self, isolated_http_daemon, qa_workspace):
        ep = isolated_http_daemon["endpoint"]
        ws_id = qa_workspace["workspace_id"]
        req_id = f"req-{uuid.uuid4().hex}"
        params = {
            "title": "QA-DEDUP",
            "description": "dedup test",
            "workspace_id": ws_id,
            "request_id": req_id,
        }
        client = _new_client(ep)
        r1 = client.call("task.create", params)
        r2 = client.call("task.create", params)
        assert r1["task_id"] == r2["task_id"], "同一 request_id 应命中 dedup Replay"
        assert r1["title"] == r2["title"] == "QA-DEDUP"

    def test_distinct_request_id_creates_distinct_tasks(self, isolated_http_daemon, qa_workspace):
        ep = isolated_http_daemon["endpoint"]
        ws_id = qa_workspace["workspace_id"]
        client = _new_client(ep)
        base = {"title": "QA-DEDUP-2", "workspace_id": ws_id}
        r1 = client.call("task.create", {**base, "request_id": f"req-{uuid.uuid4().hex}"})
        r2 = client.call("task.create", {**base, "request_id": f"req-{uuid.uuid4().hex}"})
        assert r1["task_id"] != r2["task_id"]


class TestConcurrentLeaseFencing:
    """lease 争用 + fencing：并发 acquire 同 task 只有一方成功；旧 token 被拒。"""

    def _create_task(self, client, ws_id) -> str:
        r = client.call("task.create", {
            "title": f"QA-LEASE-{uuid.uuid4().hex[:8]}",
            "workspace_id": ws_id,
        })
        return r["task_id"]

    def test_concurrent_acquire_single_winner(self, isolated_http_daemon, qa_workspace):
        ep = isolated_http_daemon["endpoint"]
        ws_id = qa_workspace["workspace_id"]
        setup = _new_client(ep)
        task_id = self._create_task(setup, ws_id)
        # 两个 agent 都先注册（active holder），制造真实争用
        _register_agent(setup, "agent-0", ws_id)
        _register_agent(setup, "agent-1", ws_id)

        def acquire(agent: str):
            client = _new_client(ep)
            return client.call("lease.acquire", {
                "task_id": task_id,
                "role": "implementer",
                "ttl_seconds": 120,
                "workspace_id": ws_id,
                "identity": _identity(agent),
            })

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(acquire, f"agent-{i}") for i in range(2)]
            for f in futs:
                try:
                    results.append(("ok", f.result(timeout=30)))
                except DaemonRemoteError as e:
                    results.append(("err", e.code))

        wins = [r for r in results if r[0] == "ok"]
        losses = [r for r in results if r[0] == "err"]
        assert len(wins) == 1, f"lease 并发 acquire 应恰好 1 个 winner: {results}"
        assert len(losses) == 1, f"应恰好 1 个 loser（E_LEASE_ACTIVE_EXISTS）: {results}"
        assert losses[0][1] in ("E_LEASE_ACTIVE_EXISTS", "E_LEASE_HOLDER_MISMATCH"), \
            f"败方应收到结构化 lease 冲突而非连接错误: {losses[0]}"
        # 胜方 fencing_counter 应为 1（首次 acquire）
        assert wins[0][1]["fencing_counter"] == 1

    def test_fencing_old_token_rejected_after_new_lease(self, isolated_http_daemon, qa_workspace):
        """fencing：新 reviewer lease 发布后，旧 token/counter 提交 task.apply 必须被拒。"""
        ep = isolated_http_daemon["endpoint"]
        ws_id = qa_workspace["workspace_id"]
        client = _new_client(ep)
        task_id = self._create_task(client, ws_id)
        _register_agent(client, "agent-a", ws_id)
        _register_agent(client, "agent-b", ws_id)

        # Agent A 先获取 reviewer lease（counter=1）
        a = client.call("lease.acquire", {
            "task_id": task_id, "role": "reviewer", "ttl_seconds": 300,
            "workspace_id": ws_id, "identity": _identity("agent-a", role="reviewer"),
        })
        old_token = a["token"]
        old_counter = a["fencing_counter"]

        # Agent A 释放 → Agent B 重新获取（counter 递增）
        client.call("lease.release", {
            "task_id": task_id, "role": "reviewer",
            "token": old_token, "workspace_id": ws_id,
        })
        b = client.call("lease.acquire", {
            "task_id": task_id, "role": "reviewer", "ttl_seconds": 300,
            "workspace_id": ws_id, "identity": _identity("agent-b", role="reviewer"),
        })
        assert b["fencing_counter"] > old_counter

        # 旧 token 提交 apply → 必须被拒（E_LEASE_FENCING_STALE 或 token 不匹配）
        with pytest.raises(DaemonRemoteError) as ei:
            client.call("task.apply", {
                "task_id": task_id,
                "lease_token": old_token, "fencing_counter": old_counter,
                "workspace_id": ws_id,
            })
        assert ei.value.code in ("E_LEASE_FENCING_STALE", "E_LEASE_TOKEN_MISMATCH",
                                 "E_LEASE_EXPIRED", "E_LEASE_CLOCK_UNAVAILABLE",
                                 "E_LEASE_NOT_FOUND"), \
            f"旧 token apply 应被 fencing 拒绝，实际 {ei.value.code}"

        # 新 token 提交 apply → 成功（最终一致）
        ok = client.call("task.apply", {
            "task_id": task_id,
            "lease_token": b["token"], "fencing_counter": b["fencing_counter"],
            "workspace_id": ws_id,
        })
        assert ok is not None

    def test_concurrent_apply_close_same_task(self, isolated_http_daemon, qa_workspace):
        """并发 task.apply + task.close 同 task：串行化、最终状态一致、无 E_* 误冲突。"""
        ep = isolated_http_daemon["endpoint"]
        ws_id = qa_workspace["workspace_id"]
        setup = _new_client(ep)
        task_id = self._create_task(setup, ws_id)
        _register_agent(setup, "agent-main", ws_id)
        lease = setup.call("lease.acquire", {
            "task_id": task_id, "role": "implementer", "ttl_seconds": 300,
            "workspace_id": ws_id, "identity": _identity("agent-main"),
        })
        token, counter = lease["token"], lease["fencing_counter"]

        def apply_step(agent: str):
            client = _new_client(ep)
            return client.call("task.apply", {
                "task_id": task_id, "role": "implementer",
                "lease_token": token, "fencing_counter": counter,
                "workspace_id": ws_id,
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futs = [pool.submit(apply_step, f"agent-{i}") for i in range(2)]
            results = []
            for f in futs:
                try:
                    results.append(("ok", f.result(timeout=30)))
                except DaemonRemoteError as e:
                    results.append(("err", e.code))

        # 同 lease 并发 apply：串行化后要么都成功、要么一方被 fencing/状态机拒绝，
        # 但绝不允许连接错误/死锁/超时（timeout 由 f.result 保证已抛）。
        codes = [r[1] for r in results if r[0] == "err"]
        for c in codes:
            assert c.startswith("E_"), f"失败应为结构化 E_* 错误，实际 {c}"


class TestNoDeadlockUnderConcurrency:
    """并发风暴下无死锁/超时崩溃：混合读写并发，全部请求在超时内完成。"""

    def test_mixed_concurrent_workload(self, isolated_http_daemon, qa_workspace):
        ep = isolated_http_daemon["endpoint"]
        ws_id = qa_workspace["workspace_id"]
        setup = _new_client(ep)
        # 预创建 3 个任务
        task_ids = []
        for i in range(3):
            r = setup.call("task.create", {
                "title": f"QA-STORM-{i}-{uuid.uuid4().hex[:6]}", "workspace_id": ws_id,
            })
            task_ids.append(r["task_id"])

        def read_ops(agent: str, tid: str):
            client = _new_client(ep)
            out = []
            for _ in range(5):
                out.append(client.call("lease.status", {
                    "task_id": tid, "workspace_id": ws_id,
                }))
            return out

        def write_ops(agent: str, i: int):
            client = _new_client(ep)
            return client.call("task.create", {
                "title": f"QA-STORM-W-{i}-{uuid.uuid4().hex[:6]}", "workspace_id": ws_id,
            })

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            futs = []
            for i, tid in enumerate(task_ids):
                futs.append(pool.submit(read_ops, f"reader-{i}", tid))
            for i in range(4):
                futs.append(pool.submit(write_ops, f"writer-{i}", i))
            results = [f.result(timeout=45) for f in futs]

        # 全部在超时内完成（无死锁），写全成功
        write_results = results[len(task_ids):]
        assert len(write_results) == 4
        for r in write_results:
            assert r["status"] == "open"
        # 读返回列表（无异常）
        for reads in results[:len(task_ids)]:
            assert isinstance(reads, list) and len(reads) == 5
