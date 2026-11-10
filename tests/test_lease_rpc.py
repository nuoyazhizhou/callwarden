r"""Lease Control Plane 真实 daemon RPC round-trip（M7 lease slice）。

本套件启动真实 `cw-daemon.exe`（隔离临时数据目录 + task DB），
通过 `HttpDaemonRpcClient`（HTTPS + mTLS，ADR-002 单轨）对 5 个 lease RPC 做真实往返验证，
**全程禁 mock**：所有断言都基于真实 daemon 进程的响应与隔离任务库的落库结果。

覆盖（用户 M7 验收 10 项）：
1. lease.acquire 成功返回 raw token + fencing_counter=1，DB 只存 sha256（不含 raw token）
2. lease.acquire 双活拒绝（E_LEASE_ACTIVE_EXISTS）
3. lease.acquire 过期后重新获取 fencing counter 单调递增
4. lease.extend / lease.renew（兼容别名）续期且 counter 不变（幂等）
5. lease.extend 错误 token → E_LEASE_TOKEN_MISMATCH
6. lease.extend 过期 → E_LEASE_EXPIRED
7. lease.extend 旧 fencing counter → E_LEASE_FENCING_STALE
8. lease.release 幂等（重复 release 返回 idempotent，不创建新 lease）
9. lease.status 只读且不暴露 raw token（保留 token_hash）
10. lease.list_events 返回 acquire/renew/release append-only 审计事件

前置条件：
1. 已构建 `cw-daemon.exe`（`cargo build --release --bin cw-daemon`）且存在可用的 mTLS PKI；
   缺失时由 `setup_w3_client` 归一为前置缺失并 skip。
"""

import os
import shutil
import sqlite3
import sys
import tempfile
import time

import pytest

from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.config import get_pki_dir
from _w3_harness import seed_cli_task_agent, seed_cli_task_authority, setup_w3_client

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="进程级 Lease RPC round-trip 需要 Windows 隔离 daemon",
)

_REAL_PKI_DIR = get_pki_dir()


def _seed_lease_tasks(task_db: str, inst: str, root_path: str, ws_id: int = 1) -> None:
    """为 lease 用例 seed canonical task→workspace binding（daemon fail-closed 要求）。"""
    now = time.time()
    seed_cli_task_authority(task_db, ws_id, f"ws-{ws_id}", root_path, inst, now)
    capture_id = f"wc-cli-{inst}"
    conn = sqlite3.connect(task_db, timeout=10)
    try:
        for i in range(1, 11):
            task_id = f"T-LEASE-RPC-{i}"
            conn.execute(
                "INSERT OR REPLACE INTO tasks "
                "(id, title, description, creator, status, created_at, updated_at, parent_id)"
                " VALUES (?, ?, '', 'lease-rpc', 'open', ?, ?, '')",
                (task_id, task_id, now, now),
            )
            conn.execute(
                "INSERT OR IGNORE INTO task_workspace_bindings "
                "(task_id, workspace_id, workspace_binding_id, workspace_capture_id,"
                " created_by, authoritative_created_at)"
                " VALUES (?, ?, ?, ?, 'lease-rpc', ?)",
                (task_id, ws_id, f"tb-{task_id}", capture_id, now),
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def lease_env():
    """启动隔离 HTTPS daemon，返回 (client, tmp, task_db, proc)。"""
    os.environ["CW_PKI_DIR"] = _REAL_PKI_DIR
    tmp = tempfile.mkdtemp(prefix="cw_lease_")
    root_path = os.path.join(tmp, "proj")
    try:
        client, inst, _endpoint, proc = setup_w3_client(tmp, root_path, ws_id=1)
        if client is None:
            pytest.skip("隔离 HTTPS daemon 前置不可用（二进制/PKI/端口）")
        task_db = os.path.join(tmp, "task.db")
        _seed_lease_tasks(task_db, inst, root_path, ws_id=1)
        # holder 已注册且 active：未过期 lease 才按 E_LEASE_ACTIVE_EXISTS 拒绝，
        # 而不是被 daemon 当作 orphan holder 回收。
        seed_cli_task_agent(task_db, "agent-lease-rpc",
                            session_id="session-lease-rpc", role="implementer")
        yield client, tmp, task_db, proc
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
        shutil.rmtree(tmp, ignore_errors=True)


def _lease_acquire(client, task_id, role, identity, ttl_seconds):
    return client.call("lease.acquire", {
        "task_id": task_id,
        "role": role,
        "identity": identity,
        "ttl_seconds": ttl_seconds,
    })


def _lease_extend(client, task_id, role, token, identity, ttl_seconds=None,
                  fencing_counter=None):
    params = {"task_id": task_id, "role": role, "token": token, "identity": identity}
    if ttl_seconds is not None:
        params["ttl_seconds"] = ttl_seconds
    if fencing_counter is not None:
        params["fencing_counter"] = fencing_counter
    return client.call("lease.extend", params)


def _lease_release(client, task_id, role, token, identity):
    return client.call("lease.release", {
        "task_id": task_id,
        "role": role,
        "token": token,
        "identity": identity,
    })


def _identity(role: str = "implementer") -> dict:
    return {
        "agent_id": "agent-lease-rpc",
        "session_id": "session-lease-rpc",
        "model_id": "model-lease-rpc",
        "role": role,
    }


class TestLeaseRpcRoundTrip:
    """真实 daemon 进程级 Lease 5 RPC 全链路（禁 mock）。"""

    def test_acquire_returns_raw_token_and_stores_hash(self, lease_env):
        client, tmp, task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-1"

        res = _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)
        raw_token = res["token"]
        assert isinstance(raw_token, str) and len(raw_token) > 0
        assert res["fencing_counter"] == 1
        assert res["lease_id"].startswith("L-")

        # 落库：DB 只存 sha256，不含 raw token
        conn = sqlite3.connect(task_db)
        try:
            row = conn.execute(
                "SELECT token_hash, fencing_counter, status FROM task_leases "
                "WHERE task_id = ? AND status = 'active'",
                (task_id,),
            ).fetchone()
            assert row is not None, "acquire 后应有 active lease"
            token_hash, counter, status = row
            import hashlib
            assert token_hash != raw_token, "DB 不得存 raw token"
            assert token_hash == hashlib.sha256(raw_token.encode("utf-8")).hexdigest()
            assert counter == 1
            assert status == "active"
        finally:
            conn.close()

    def test_acquire_blocks_double_active(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-2"
        _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)

        with pytest.raises(DaemonRemoteError) as exc:
            _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)
        assert exc.value.code == "E_LEASE_ACTIVE_EXISTS", exc.value

    def test_acquire_after_expiry_increments_counter(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-3"
        _lease_acquire(client, task_id, "reviewer", _identity("reviewer"), 3600.0)

        # 人为置过期（模拟权威时钟流逝）
        conn = sqlite3.connect(task_db)
        try:
            conn.execute("UPDATE task_leases SET expires_at = 1.0 WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()

        res = _lease_acquire(client, task_id, "reviewer", _identity("reviewer"), 3600.0)
        assert res["fencing_counter"] == 2, "过期后重新获取 counter 必须递增"

    def test_extend_renews_and_keeps_counter_via_alias(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-4"
        acq = _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)
        expires_before = acq["expires_at"]

        # lease.renew 兼容别名（并入 lease.extend）
        ext = client.call("lease.renew", {
            "task_id": task_id, "role": "implementer", "token": acq["token"],
            "identity": _identity(), "ttl_seconds": 7200.0,
        })
        assert ext["fencing_counter"] == 1, "extend/renew 不得递增 counter"
        assert ext["expires_at"] > expires_before, "续期后 expires_at 前进"

    def test_extend_rejects_bad_token(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-5"
        _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)

        with pytest.raises(DaemonRemoteError) as exc:
            _lease_extend(client, task_id, "implementer", "wrong-token", _identity())
        assert exc.value.code == "E_LEASE_TOKEN_MISMATCH", exc.value

    def test_extend_rejects_expired(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-6"
        acq = _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)

        conn = sqlite3.connect(task_db)
        try:
            conn.execute("UPDATE task_leases SET expires_at = 1.0 WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(DaemonRemoteError) as exc:
            _lease_extend(client, task_id, "implementer", acq["token"], _identity())
        assert exc.value.code == "E_LEASE_EXPIRED", exc.value

    def test_extend_rejects_stale_fencing(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-7"
        acq = _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)

        with pytest.raises(DaemonRemoteError) as exc:
            _lease_extend(client, task_id, "implementer", acq["token"], _identity(),
                          fencing_counter=99)
        assert exc.value.code == "E_LEASE_FENCING_STALE", exc.value

    def test_release_and_idempotent(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-8"
        acq = _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)

        rel = _lease_release(client, task_id, "implementer", acq["token"], _identity())
        assert rel["status"] == "released"

        # 幂等：重复 release（同 token）不创建新 lease、不报错
        rel2 = _lease_release(client, task_id, "implementer", acq["token"], _identity())
        assert rel2.get("idempotent") is True

        conn = sqlite3.connect(task_db)
        try:
            n = conn.execute(
                "SELECT COUNT(*) FROM task_leases WHERE task_id = ?", (task_id,)
            ).fetchone()[0]
            assert n == 1, "幂等 release 不得创建新 lease"
        finally:
            conn.close()

    def test_status_hides_raw_token(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-9"
        _acq = _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)

        status = client.call("lease.status", {"task_id": task_id, "role": "implementer"})
        assert status["status"] == "active"
        assert "token" not in status, "status 不得暴露 raw token"
        assert "token_hash" in status, "status 保留 token_hash 供受保护校验"
        assert status["lease_id"].startswith("L-")

    def test_list_events_returns_audit_trail(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-10"
        acq = _lease_acquire(client, task_id, "implementer", _identity(), 3600.0)
        client.call("lease.renew", {
            "task_id": task_id, "role": "implementer", "token": acq["token"],
            "identity": _identity(), "ttl_seconds": 7200.0,
        })
        _lease_release(client, task_id, "implementer", acq["token"], _identity())

        events = client.call("lease.list_events", {"task_id": task_id, "role": "implementer"})
        types = [e["event_type"] for e in events]
        assert types == ["acquire", "renew", "release"], f"append-only 顺序错误: {types}"
        for e in events:
            assert "token" not in e, "事件不得含 raw token"
            assert e.get("actor_agent_id") == "agent-lease-rpc"
            assert e.get("fencing_counter") == 1
