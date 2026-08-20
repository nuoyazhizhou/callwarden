r"""Lease Control Plane 真实 daemon RPC round-trip（M7 lease slice）。

本套件启动真实 `cw-daemon.exe`（隔离临时数据目录 + task DB），
通过 `UnixDaemonRpcClient`（Windows Named Pipe）对 5 个 lease RPC 做真实往返验证，
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

前置条件（与 test_windows_daemon_e2e.py 一致）：
1. Windows 平台（Named Pipe）
2. 已构建 `cw-daemon.exe`：`cargo build --release --no-default-features --manifest-path rust_ext/Cargo.toml --bin cw-daemon`
3. 默认管道 `\\.\pipe\callwarden-<sid>` 未被其他 daemon 占用（占用则 skip）
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

from callwarden.server.daemon_protocol import DaemonRemoteError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 使用 release 二进制：debug 构建的 daemon 冷启动存在 ~50s 的既有延迟
# （与 M7 改动无关，release 构建 2s 内就绪），release 亦与生产 runtime 一致。
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="进程级 Lease RPC round-trip 需要 Windows + Named Pipe",
)

requires_binaries = pytest.mark.skipif(
    not os.path.exists(_DAEMON_BIN),
    reason="cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）",
)


def _daemon_config(tmp: str) -> dict:
    """生成隔离的 daemon JSON 配置（Windows 管道名由 transport 按 SID 派生）。"""
    data_root = os.path.join(tmp, "data")
    return {
        "socket_path": "",
        "registry_db_path": os.path.join(tmp, "registry.db"),
        # 权威任务库：daemon 与 Python `cw task` 共享同一 DB（此处隔离临时目录）
        "task_db_path": os.path.join(tmp, "callwarden.db"),
        "data_root": data_root,
        "max_workers": 2,
        "request_timeout_secs": 30,
        "snapshot_cache_capacity": 2,
        "codegraph_db_path_template": os.path.join(
            data_root, "workspaces", "{workspace_instance_id}", "codegraph.db"
        ),
        "socket_mode": 0o660,
        "socket_group": "",
        "stage_toggle_db_path": os.path.join(tmp, "stage_toggle.db"),
    }


@pytest.fixture(scope="module", autouse=True)
def ensure_fresh_binary():
    """P2 门禁：显式构建 cw-daemon，确保二进制由当前源码重建。"""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("未找到 cargo，无法构建新鲜二进制")
    build = subprocess.run(
        [cargo, "build", "--release", "--no-default-features",
         "--manifest-path", os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml"),
         "--bin", "cw-daemon"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if build.returncode != 0:
        pytest.fail("cargo build 失败，二进制无法由当前源码重建：\n" + (build.stdout + build.stderr)[-3000:])
    if not os.path.exists(_DAEMON_BIN):
        pytest.fail(f"cargo build 成功但未产出 {_DAEMON_BIN}")


@pytest.fixture()
def lease_env():
    """启动真实 cw-daemon（隔离 task DB），返回 (client, tmp, task_db, proc)。

    daemon migrate schema 后插入一行测试 workspace，使 Lease 能绑定到真实
    workspace_id（active_workspace_id 回退取唯一一行）。
    """
    from server.daemon_client import UnixDaemonRpcClient
    from callwarden.config import _get_windows_user_sid

    pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
    probe = UnixDaemonRpcClient(socket_path=pipe, timeout=3)
    try:
        probe.call("ping")
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过")
    except Exception:
        pass

    tmp = tempfile.mkdtemp(prefix="cw_lease_")
    config = _daemon_config(tmp)
    config_path = os.path.join(tmp, "daemon.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    log = open(os.path.join(tmp, "daemon.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [_DAEMON_BIN, "--config", config_path],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )

    client = UnixDaemonRpcClient(socket_path=pipe, timeout=10)
    deadline = time.time() + 40
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            if client.call("ping").get("status") == "ok":
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ready:
        log.flush()
        pytest.fail("daemon 未在超时内响应")

    task_db = os.path.join(tmp, "callwarden.db")
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (1, 'lease-test', '.', ?1)",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()

    yield client, tmp, task_db, proc

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    shutil.rmtree(tmp, ignore_errors=True)


def _identity(role: str = "implementer") -> dict:
    return {
        "agent_id": "agent-lease-rpc",
        "session_id": "session-lease-rpc",
        "model_id": "model-lease-rpc",
        "role": role,
    }


@requires_binaries
class TestLeaseRpcRoundTrip:
    """真实 daemon 进程级 Lease 5 RPC 全链路（禁 mock）。"""

    def test_acquire_returns_raw_token_and_stores_hash(self, lease_env):
        client, tmp, task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-1"

        res = client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)
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
        client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)

        with pytest.raises(DaemonRemoteError) as exc:
            client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)
        assert exc.value.code == "E_LEASE_ACTIVE_EXISTS", exc.value

    def test_acquire_after_expiry_increments_counter(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-3"
        client.lease_acquire(task_id, "reviewer", identity=_identity("reviewer"), ttl_seconds=3600.0)

        # 人为置过期（模拟权威时钟流逝）
        conn = sqlite3.connect(task_db)
        try:
            conn.execute("UPDATE task_leases SET expires_at = 1.0 WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()

        res = client.lease_acquire(task_id, "reviewer", identity=_identity("reviewer"), ttl_seconds=3600.0)
        assert res["fencing_counter"] == 2, "过期后重新获取 counter 必须递增"

    def test_extend_renews_and_keeps_counter_via_alias(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-4"
        acq = client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)
        expires_before = acq["expires_at"]

        # lease.renew 兼容别名（并入 lease.extend）
        ext = client.lease_renew(task_id, "implementer", acq["token"], identity=_identity(), ttl_seconds=7200.0)
        assert ext["fencing_counter"] == 1, "extend/renew 不得递增 counter"
        assert ext["expires_at"] > expires_before, "续期后 expires_at 前进"

    def test_extend_rejects_bad_token(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-5"
        client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)

        with pytest.raises(DaemonRemoteError) as exc:
            client.lease_extend(task_id, "implementer", "wrong-token", identity=_identity())
        assert exc.value.code == "E_LEASE_TOKEN_MISMATCH", exc.value

    def test_extend_rejects_expired(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-6"
        acq = client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)

        conn = sqlite3.connect(task_db)
        try:
            conn.execute("UPDATE task_leases SET expires_at = 1.0 WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()

        with pytest.raises(DaemonRemoteError) as exc:
            client.lease_extend(task_id, "implementer", acq["token"], identity=_identity())
        assert exc.value.code == "E_LEASE_EXPIRED", exc.value

    def test_extend_rejects_stale_fencing(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-7"
        acq = client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)

        with pytest.raises(DaemonRemoteError) as exc:
            client.lease_extend(task_id, "implementer", acq["token"], identity=_identity(),
                                fencing_counter=99)
        assert exc.value.code == "E_LEASE_FENCING_STALE", exc.value

    def test_release_and_idempotent(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-8"
        acq = client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)

        rel = client.lease_release(task_id, "implementer", acq["token"], identity=_identity())
        assert rel["status"] == "released"

        # 幂等：重复 release（同 token）不创建新 lease、不报错
        rel2 = client.lease_release(task_id, "implementer", acq["token"], identity=_identity())
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
        acq = client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)

        status = client.lease_status(task_id, "implementer")
        assert status["status"] == "active"
        assert "token" not in status, "status 不得暴露 raw token"
        assert "token_hash" in status, "status 保留 token_hash 供受保护校验"
        assert status["lease_id"].startswith("L-")

    def test_list_events_returns_audit_trail(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-LEASE-RPC-10"
        acq = client.lease_acquire(task_id, "implementer", identity=_identity(), ttl_seconds=3600.0)
        client.lease_renew(task_id, "implementer", acq["token"], identity=_identity(), ttl_seconds=7200.0)
        client.lease_release(task_id, "implementer", acq["token"], identity=_identity())

        events = client.lease_list_events(task_id, "implementer")
        types = [e["event_type"] for e in events]
        assert types == ["acquire", "renew", "release"], f"append-only 顺序错误: {types}"
        for e in events:
            assert "token" not in e, "事件不得含 raw token"
            assert e.get("actor_agent_id") == "agent-lease-rpc"
            assert e.get("fencing_counter") == 1
