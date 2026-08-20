r"""M7 Lease Gate 实证（任务 T-1786499847862-77260874）。

验证 daemon 权威路径下 `task.apply` / `task.close` 必须持有完整 reviewer lease
凭证（lease_token + fencing_counter），缺凭证 / 只提供其一一律 fail-closed 返回
`E_LEASE_REQUIRED`，并保持既有结构化错误码（E_LEASE_CLOCK_UNAVAILABLE /
E_LEASE_NOT_FOUND / E_LEASE_TOKEN_MISMATCH / E_LEASE_EXPIRED /
E_LEASE_FENCING_STALE / E_LEASE_HOLDER_MISMATCH）。

全程启动真实 `cw-daemon.exe`（隔离临时数据目录 + task DB），通过 Named Pipe
进程级 RPC 往返验证，**禁 mock**；每个拒绝场景都证明 task status / task_events /
action_identities 未被错误修改。

覆盖矩阵：
- 成功路径：create → claim → report(review) → lease.acquire →
  task.apply(applied) → task.close(closed)，核验 task_events 与 action_identities
- apply 拒绝（7）：无 lease_token / 无 fencing_counter / 只提供 lease_token /
  错误 token / 旧 fencing_counter / 已过期 lease / 错误 holder identity
- close 拒绝（6）：无 lease_token / 无 fencing_counter / 错误 token /
  旧 fencing_counter / 已过期 lease / 错误 holder identity
- 额外门禁：request_id 重放不产生重复事件 / 双 reviewer 竞争单 holder /
  close 保留父任务子任务关闸 / 业务错误原样返回（不包装为连接失败）/
  enterprise/auto daemon 不可用 fail-closed（不静默回退本地 DB）

前置条件（与 test_lease_rpc.py 一致）：
1. Windows 平台（Named Pipe）
2. 已构建 `cw-daemon.exe`：`cargo build --release --no-default-features
   --manifest-path rust_ext/Cargo.toml --bin cw-daemon`
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
from callwarden.server.daemon_client import DaemonUnavailableError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="进程级 Lease 门禁 round-trip 需要 Windows + Named Pipe",
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


@pytest.fixture(scope="class")
def lease_env():
    """启动真实 cw-daemon（隔离 task DB），返回 (client, tmp, task_db, proc)。

    与 test_lease_rpc.py 一致：探针默认管道，被生产 daemon 占用则 skip；
    隔离库插入 workspace id=1，使 action_identity 可绑定真实 workspace_id。
    """
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    from callwarden.config import _get_windows_user_sid

    pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
    probe = UnixDaemonRpcClient(socket_path=pipe, timeout=3)
    try:
        probe.call("ping")
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过")
    except Exception:
        pass

    tmp = tempfile.mkdtemp(prefix="cw_lease_gate_")
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
            "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (1, 'lease-gate-test', '.', ?1)",
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


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _reviewer_identity(tag: str) -> dict:
    return {
        "agent_id": f"agent-gate-{tag}",
        "session_id": f"session-gate-{tag}",
        "model_id": f"model-gate-{tag}",
        "role": "reviewer",
    }


def _prepare_task_review(client, task_id: str, session: str = "session-impl",
                         steps: list = None) -> dict:
    """create(open) → claim(in_progress) → report(review)。返回 claim 响应。"""
    client.call("task.create", {
        "task_id": task_id,
        "title": f"lease-gate-{task_id}",
        "steps": steps if steps is not None else [{"action": "实现", "target_file": "f.py"}],
    })
    claim = client.call("task.claim", {"task_id": task_id, "agent_session_id": session})
    report_params = {"task_id": task_id, "summary": "done", "agent_session_id": session,
                     "success": True}
    if claim.get("step_id"):
        report_params["step_id"] = claim["step_id"]
    client.call("task.report", report_params)
    assert client.task_status(task_id)["status"] == "review", "任务未推进到 review"
    return claim


def _acquire_reviewer_lease(client, task_id: str, tag: str) -> dict:
    return client.lease_acquire(task_id, "reviewer", identity=_reviewer_identity(tag),
                                ttl_seconds=3600.0)


def _prepare_task_applied(client, task_id: str, tag: str) -> dict:
    """推进到 applied（review + acquire + apply），返回 acquire 响应。"""
    _prepare_task_review(client, task_id)
    acq = _acquire_reviewer_lease(client, task_id, tag)
    res = client.task_apply(task_id, reviewer="reviewer", identity=_reviewer_identity(tag),
                            lease_token=acq["token"], fencing_counter=acq["fencing_counter"])
    assert res["status"] == "applied", f"apply 失败: {res}"
    return acq


def _count_actions(task_db: str, task_id: str) -> int:
    conn = sqlite3.connect(task_db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM action_identities WHERE task_id = ?", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _assert_rejected(client, task_db: str, task_id: str, method: str, params: dict,
                     expect_code: str):
    """断言拒绝：返回 expect_code，且 task status / task_events / action_identities 均不变。"""
    before_status = client.task_status(task_id)["status"]
    before_events = len(client.task_events(task_id)["events"])
    before_actions = _count_actions(task_db, task_id)
    with pytest.raises(DaemonRemoteError) as exc:
        client.call(method, params)
    assert exc.value.code == expect_code, f"{method}: {exc.value}"
    after_status = client.task_status(task_id)["status"]
    assert after_status == before_status, f"状态被错误修改: {before_status} -> {after_status}"
    after_events = len(client.task_events(task_id)["events"])
    assert after_events == before_events, f"拒绝后新增了 task_event: {before_events} -> {after_events}"
    after_actions = _count_actions(task_db, task_id)
    assert after_actions == before_actions, f"拒绝后写入了 action_identity: {before_actions} -> {after_actions}"


# ----------------------------------------------------------------------
# 成功路径
# ----------------------------------------------------------------------

@requires_binaries
class TestLeaseGateSuccessPath:
    """合法 reviewer lease 下 apply/close 全链路成功，事件与身份落库。"""

    def test_apply_close_full_success_with_lease_identity_events(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-SUCCESS"
        tag = "ok"
        acq = _prepare_task_applied(client, task_id, tag)

        # close 使用同一有效 lease
        res = client.task_close(task_id, reviewer="reviewer", identity=_reviewer_identity(tag),
                                lease_token=acq["token"], fencing_counter=acq["fencing_counter"])
        assert res["status"] == "closed"
        assert res.get("closed_at", 0) > 0, "closed_at 必须为非零真实时间戳"
        assert client.task_status(task_id)["status"] == "closed"

        # task_events：存在 applied / closed 状态流转事件
        events = client.task_events(task_id)["events"]
        to_statuses = [e["to_status"] for e in events]
        assert "applied" in to_statuses, f"缺少 applied 事件: {to_statuses}"
        assert "closed" in to_statuses, f"缺少 closed 事件: {to_statuses}"

        # action_identities：apply/close 各写一条 state_transition，身份与 lease holder 一致
        conn = sqlite3.connect(task_db)
        try:
            rows = conn.execute(
                "SELECT action_type, agent_id, session_id, model_id, role FROM action_identities "
                "WHERE task_id = ? ORDER BY id",
                (task_id,),
            ).fetchall()
        finally:
            conn.close()
        transitions = [r for r in rows if r[0] == "state_transition"]
        assert len(transitions) >= 2, f"apply/close 应各写一条 action_identity: {rows}"
        expect = ("state_transition", f"agent-gate-{tag}", f"session-gate-{tag}",
                  f"model-gate-{tag}", "reviewer")
        assert transitions[-1] == expect, f"close 身份记录不匹配: {transitions[-1]}"

        # lease 审计：acquire 后 release（幂等收尾）
        rel = client.lease_release(task_id, "reviewer", acq["token"],
                                   identity=_reviewer_identity(tag))
        assert rel["status"] == "released"


# ----------------------------------------------------------------------
# apply 拒绝路径
# ----------------------------------------------------------------------

@requires_binaries
class TestLeaseGateApplyRejections:
    """apply 缺/错凭证全部 fail-closed，状态与事件零改动。"""

    def test_apply_rejects_missing_lease_token(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-AP-NO-TOKEN"
        # 不 acquire：无凭证场景
        _prepare_task_review(client, task_id)
        _assert_rejected(client, task_db, task_id, "task.apply", {
            "task_id": task_id, "reviewer": "reviewer", "fencing_counter": 1,
        }, "E_LEASE_REQUIRED")

    def test_apply_rejects_missing_fencing_counter(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-AP-NO-COUNTER"
        _prepare_task_review(client, task_id)
        _assert_rejected(client, task_db, task_id, "task.apply", {
            "task_id": task_id, "reviewer": "reviewer", "lease_token": "any-token",
        }, "E_LEASE_REQUIRED")

    def test_apply_rejects_partial_lease_token_only(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-AP-PARTIAL"
        _prepare_task_review(client, task_id)
        acq = _acquire_reviewer_lease(client, task_id, "partial")
        _assert_rejected(client, task_db, task_id, "task.apply", {
            "task_id": task_id, "reviewer": "reviewer",
            "identity": _reviewer_identity("partial"),
            "lease_token": acq["token"],  # 只提供 token，缺 fencing_counter
        }, "E_LEASE_REQUIRED")

    def test_apply_rejects_wrong_token(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-AP-WRONG-TOKEN"
        _prepare_task_review(client, task_id)
        acq = _acquire_reviewer_lease(client, task_id, "wt")
        _assert_rejected(client, task_db, task_id, "task.apply", {
            "task_id": task_id, "reviewer": "reviewer",
            "identity": _reviewer_identity("wt"),
            "lease_token": "wrong-token", "fencing_counter": acq["fencing_counter"],
        }, "E_LEASE_TOKEN_MISMATCH")

    def test_apply_rejects_stale_fencing_counter(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-AP-STALE"
        _prepare_task_review(client, task_id)
        acq = _acquire_reviewer_lease(client, task_id, "stale")
        _assert_rejected(client, task_db, task_id, "task.apply", {
            "task_id": task_id, "reviewer": "reviewer",
            "identity": _reviewer_identity("stale"),
            "lease_token": acq["token"], "fencing_counter": 0,  # 旧 counter（当前为 1）
        }, "E_LEASE_FENCING_STALE")

    def test_apply_rejects_expired_lease(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-AP-EXPIRED"
        _prepare_task_review(client, task_id)
        acq = _acquire_reviewer_lease(client, task_id, "exp")
        conn = sqlite3.connect(task_db)
        try:
            conn.execute("UPDATE task_leases SET expires_at = 1.0 WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()
        _assert_rejected(client, task_db, task_id, "task.apply", {
            "task_id": task_id, "reviewer": "reviewer",
            "identity": _reviewer_identity("exp"),
            "lease_token": acq["token"], "fencing_counter": acq["fencing_counter"],
        }, "E_LEASE_EXPIRED")

    def test_apply_rejects_wrong_holder_identity(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-AP-HOLDER"
        _prepare_task_review(client, task_id)
        acq = _acquire_reviewer_lease(client, task_id, "holder-a")
        # 持有合法 token/counter，但 identity 与 lease holder 不一致
        _assert_rejected(client, task_db, task_id, "task.apply", {
            "task_id": task_id, "reviewer": "reviewer",
            "identity": _reviewer_identity("holder-b"),
            "lease_token": acq["token"], "fencing_counter": acq["fencing_counter"],
        }, "E_LEASE_HOLDER_MISMATCH")


# ----------------------------------------------------------------------
# close 拒绝路径
# ----------------------------------------------------------------------

@requires_binaries
class TestLeaseGateCloseRejections:
    """close 缺/错凭证全部 fail-closed，状态与事件零改动。"""

    def test_close_rejects_missing_lease_token(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-CL-NO-TOKEN"
        _prepare_task_applied(client, task_id, "cl-no-token")
        _assert_rejected(client, task_db, task_id, "task.close", {
            "task_id": task_id, "reviewer": "reviewer", "fencing_counter": 1,
        }, "E_LEASE_REQUIRED")

    def test_close_rejects_missing_fencing_counter(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-CL-NO-COUNTER"
        _prepare_task_applied(client, task_id, "cl-no-counter")
        _assert_rejected(client, task_db, task_id, "task.close", {
            "task_id": task_id, "reviewer": "reviewer", "lease_token": "any-token",
        }, "E_LEASE_REQUIRED")

    def test_close_rejects_wrong_token(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-CL-WRONG-TOKEN"
        acq = _prepare_task_applied(client, task_id, "cl-wt")
        _assert_rejected(client, task_db, task_id, "task.close", {
            "task_id": task_id, "reviewer": "reviewer",
            "identity": _reviewer_identity("cl-wt"),
            "lease_token": "wrong-token", "fencing_counter": acq["fencing_counter"],
        }, "E_LEASE_TOKEN_MISMATCH")

    def test_close_rejects_stale_fencing_counter(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-CL-STALE"
        acq = _prepare_task_applied(client, task_id, "cl-stale")
        _assert_rejected(client, task_db, task_id, "task.close", {
            "task_id": task_id, "reviewer": "reviewer",
            "identity": _reviewer_identity("cl-stale"),
            "lease_token": acq["token"], "fencing_counter": 0,
        }, "E_LEASE_FENCING_STALE")

    def test_close_rejects_expired_lease(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-CL-EXPIRED"
        acq = _prepare_task_applied(client, task_id, "cl-exp")
        conn = sqlite3.connect(task_db)
        try:
            conn.execute("UPDATE task_leases SET expires_at = 1.0 WHERE task_id = ?", (task_id,))
            conn.commit()
        finally:
            conn.close()
        _assert_rejected(client, task_db, task_id, "task.close", {
            "task_id": task_id, "reviewer": "reviewer",
            "identity": _reviewer_identity("cl-exp"),
            "lease_token": acq["token"], "fencing_counter": acq["fencing_counter"],
        }, "E_LEASE_EXPIRED")

    def test_close_rejects_wrong_holder_identity(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-CL-HOLDER"
        acq = _prepare_task_applied(client, task_id, "cl-holder-a")
        _assert_rejected(client, task_db, task_id, "task.close", {
            "task_id": task_id, "reviewer": "reviewer",
            "identity": _reviewer_identity("cl-holder-b"),
            "lease_token": acq["token"], "fencing_counter": acq["fencing_counter"],
        }, "E_LEASE_HOLDER_MISMATCH")


# ----------------------------------------------------------------------
# 额外门禁
# ----------------------------------------------------------------------

@requires_binaries
class TestLeaseGateExtraGates:
    """request_id 重放 / 单 holder 竞争 / 子任务关闸 / 业务错误透传。"""

    def test_request_id_replay_no_duplicate_events(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        task_id = "T-GATE-REPLAY"
        tag = "replay"
        _prepare_task_review(client, task_id)
        acq = _acquire_reviewer_lease(client, task_id, tag)
        identity = _reviewer_identity(tag)
        params = {
            "task_id": task_id, "reviewer": "reviewer", "identity": identity,
            "lease_token": acq["token"], "fencing_counter": acq["fencing_counter"],
            "request_id": "req-gate-replay-1",
        }
        r1 = client.call("task.apply", params)
        assert r1["status"] == "applied"
        events_after_first = len(client.task_events(task_id)["events"])

        # 同 request_id 重放：返回缓存结果，不得产生重复状态事件
        r2 = client.call("task.apply", params)
        assert r2["status"] == "applied"
        events_after_replay = len(client.task_events(task_id)["events"])
        assert events_after_replay == events_after_first, \
            f"request_id 重放新增了事件: {events_after_first} -> {events_after_replay}"

        applied_events = [e for e in client.task_events(task_id)["events"]
                          if e["to_status"] == "applied"]
        assert len(applied_events) == 1, f"applied 事件应为 1 条: {len(applied_events)}"
        # 状态仍为 applied，可继续 close（不受重放影响）
        res = client.task_close(task_id, reviewer="reviewer", identity=identity,
                                lease_token=acq["token"], fencing_counter=acq["fencing_counter"])
        assert res["status"] == "closed"

    def test_single_active_reviewer_holder_competition(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-GATE-COMPETITION"
        _prepare_task_review(client, task_id)
        acq_a = _acquire_reviewer_lease(client, task_id, "comp-a")
        # 第二个 reviewer acquire 同 task+role → E_LEASE_ACTIVE_EXISTS
        with pytest.raises(DaemonRemoteError) as exc:
            client.lease_acquire(task_id, "reviewer", identity=_reviewer_identity("comp-b"),
                                 ttl_seconds=3600.0)
        assert exc.value.code == "E_LEASE_ACTIVE_EXISTS", exc.value
        # 竞争失败者用他人 token + 自己身份 apply → holder mismatch
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.apply", {
                "task_id": task_id, "reviewer": "reviewer",
                "identity": _reviewer_identity("comp-b"),
                "lease_token": acq_a["token"], "fencing_counter": acq_a["fencing_counter"],
            })
        assert exc.value.code == "E_LEASE_HOLDER_MISMATCH", exc.value
        # 唯一有效 holder 正常 apply
        res = client.task_apply(task_id, reviewer="reviewer",
                                identity=_reviewer_identity("comp-a"),
                                lease_token=acq_a["token"],
                                fencing_counter=acq_a["fencing_counter"])
        assert res["status"] == "applied"

    def test_close_keeps_child_gate(self, lease_env):
        client, _tmp, task_db, _proc = lease_env
        parent_id = "T-GATE-PARENT"
        child_id = "T-GATE-CHILD"
        # 父任务（含步骤，推进到 review） + 未关闭子任务
        _prepare_task_review(client, parent_id)
        client.call("task.create", {"task_id": child_id, "title": "child",
                                    "steps": [{"action": "y", "target_file": "g.py"}],
                                    "parent_id": parent_id})
        # 父任务 acquire reviewer lease 并 apply
        acq = _acquire_reviewer_lease(client, parent_id, "parent")
        res = client.task_apply(parent_id, reviewer="reviewer",
                                identity=_reviewer_identity("parent"),
                                lease_token=acq["token"], fencing_counter=acq["fencing_counter"])
        assert res["status"] == "applied"

        # 带合法 lease 关闭父任务 → 子任务门禁拒绝（业务错误，非连接故障）
        before_events = len(client.task_events(parent_id)["events"])
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.close", {
                "task_id": parent_id, "reviewer": "reviewer",
                "identity": _reviewer_identity("parent"),
                "lease_token": acq["token"], "fencing_counter": acq["fencing_counter"],
            })
        assert exc.value.code == "E_CHILD_TASKS_NOT_CLOSED", exc.value
        assert not isinstance(exc.value, DaemonUnavailableError), "业务错误不得包装为连接故障"
        assert client.task_status(parent_id)["status"] == "applied", "子任务未关闭时父任务状态被改动"
        assert len(client.task_events(parent_id)["events"]) == before_events

    def test_apply_close_business_error_not_wrapped_as_connection_failure(self, lease_env):
        client, _tmp, _task_db, _proc = lease_env
        task_id = "T-GATE-NOTWRAP"
        tag = "nw"
        _prepare_task_review(client, task_id)
        acq = _acquire_reviewer_lease(client, task_id, tag)
        # 错误 token 是业务结论（E_LEASE_TOKEN_MISMATCH），必须是 DaemonRemoteError，
        # 不得被包装成 DaemonUnavailableError（连接故障），客户端才能区分。
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.apply", {
                "task_id": task_id, "reviewer": "reviewer",
                "identity": _reviewer_identity(tag),
                "lease_token": "wrong-token", "fencing_counter": acq["fencing_counter"],
            })
        assert exc.value.code == "E_LEASE_TOKEN_MISMATCH"
        assert not isinstance(exc.value, DaemonUnavailableError)


@requires_binaries
class TestLeaseGateRoutePolicy:
    """enterprise/auto 模式下 daemon 不可用时 fail-closed，不静默回退本地 DB。

    纯 Python 层单测（monkeypatch route_task_write 的依赖），不依赖真实 daemon。
    """

    def _run_fail_closed_case(self, monkeypatch, mode: str):
        import callwarden.server.daemon_client as dc
        fallback_calls = []

        def _fallback():
            fallback_calls.append(1)
            return {"local": "ok"}

        class _FakeFailClient:
            """构造成功、call 阶段抛连接错误（贴近真实 UnixDaemonRpcClient）。"""

            def call(self, method, params):
                raise OSError("endpoint 不可连接")

        def _fail_client(*args, **kwargs):
            return _FakeFailClient()

        monkeypatch.setattr(dc, "get_daemon_mode", lambda: mode)
        monkeypatch.setattr(dc, "get_task_write_policy", lambda: "shared")
        monkeypatch.setattr(dc, "is_daemon_required", lambda: mode == "enterprise")
        monkeypatch.setattr(dc, "UnixDaemonRpcClient", _fail_client)

        with pytest.raises(DaemonUnavailableError) as exc:
            dc.route_task_write("task.apply", {"task_id": "T-UNAVAIL"}, _fallback)
        assert "daemon 连接失败" in str(exc.value), exc.value
        assert fallback_calls == [], f"{mode} 模式不得静默回退本地 SQLite"
        # 拒绝路径同样 fail-closed
        with pytest.raises(DaemonUnavailableError):
            dc.route_task_write("task.close", {"task_id": "T-UNAVAIL"}, _fallback)
        assert fallback_calls == [], f"{mode} 模式 close 不得回退本地"

    def test_enterprise_daemon_unavailable_fail_closed_no_local_fallback(self, monkeypatch):
        self._run_fail_closed_case(monkeypatch, "enterprise")

    def test_auto_daemon_unavailable_fail_closed_no_local_fallback(self, monkeypatch):
        self._run_fail_closed_case(monkeypatch, "auto")
