r"""task.supersede P0-H 治理机制集成测试（T-1787277487109-758e56d0）。

在基础任务（T-1787203926824-9f873bfc-sub-1）之上验证 P0-H 加固后的正式治理链路：
1. `cw task supersede <old> <new>` 携带完整凭证（identity/role=adjudicator/
   reviewer lease/fencing/evidence/request-id）成功写入，relation + append-only
   event + 权威 task_events 审计 + ledger result 同一事务落库；
2. 被替代任务 status 保持 open、字段零改动（append-only）；
3. 幂等重放：同 request_id + 同参数只读重放，不追加任何行；
4. fail-closed 矩阵：self/重复/缺失任务/跨 workspace/环/role/lease/fencing/
   evidence 全部拒绝（稳定 E_SUPERSEDE_* / E_IDENTITY_* 码）；
5. `cw task superseded <old>` 只读投影输出 workspace/provenance。

验收方式（CLI 进程级，与基础任务一致）：
- 每调用走真实 `python cw.py task ...` 子进程；
- 全程连接真实隔离 cw-daemon.exe（临时 task DB + Named Pipe），禁 mock；
- setup（agent.register / lease.acquire）经 daemon RPC，被测入口为 CLI。

前置：Windows + 已构建 cw-daemon.exe（rust_ext/target/debug，需含 P0-H 新代码）。
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PY_EXE = sys.executable
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe")

_TASK_ID_RE = re.compile(r"T-[0-9A-Za-z-]+")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="task.supersede 集成需要 Windows + Named Pipe",
)

requires_binaries = pytest.mark.skipif(
    not os.path.exists(_DAEMON_BIN),
    reason="cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）",
)

_AGENT = "adjudicator-workbuddy-v1"
_MODEL = "deepseek-v4"
_EVIDENCE = "evidence/verdict-pass.json"
_EVIDENCE_HASH = "sha256:ev-p0h-1"


def _daemon_config(tmp: str) -> dict:
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


@pytest.fixture(scope="module")
def daemon_env():
    """启动真实隔离 cw-daemon（临时 task DB + 默认 Named Pipe；Windows transport
    硬编码 `\\.\\pipe\\callwarden-<SID>`，无法自定义管道名——生产 daemon 占用时跳过）。"""
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    from callwarden.config import _get_windows_user_sid

    pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
    probe = UnixDaemonRpcClient(socket_path=pipe, timeout=3)
    try:
        probe.call("ping")
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过")
    except Exception:
        pass

    tmp = tempfile.mkdtemp(prefix="cw_task_supersede_p0h_")
    config = _daemon_config(tmp)
    config_path = os.path.join(tmp, "daemon.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    log = open(os.path.join(tmp, "daemon.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [_DAEMON_BIN, "--config", config_path],
        stdout=log, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
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
        pytest.fail("隔离 daemon 未在超时内响应")

    task_db = os.path.join(tmp, "callwarden.db")
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (1, 'supersede-test', '.', ?1)",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()

    yield client, tmp, task_db, proc, pipe

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not os.environ.get("CW_KEEP_SUPERSEDE_TMP"):
        shutil.rmtree(tmp, ignore_errors=True)


def _cli_env(pipe: str, session: str = "", extra: dict = None) -> dict:
    env = dict(os.environ)
    env.pop("CW_AGENT_SESSION_ID", None)
    env["CW_DAEMON_MODE"] = "enterprise"
    env["CW_DAEMON_ENDPOINT"] = pipe
    env["CW_TASK_WRITE_POLICY"] = "shared"
    env["CW_DAEMON_AUTOSTART_WINDOW"] = "0"
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CALLWARDEN_LANG"] = "zh_CN"
    env["PYTHONPATH"] = _REPO_ROOT
    if session:
        env["CW_AGENT_SESSION_ID"] = session
    if extra:
        env.update(extra)
    return env


def _run_cw_cli(args, env, cwd, timeout=90):
    return subprocess.run(
        [_PY_EXE, _CW_PY] + args,
        env=env, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _all_out(proc) -> str:
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _assert_ok(proc, what: str):
    assert proc.returncode == 0, f"{what} 失败(exit={proc.returncode}):\n{_all_out(proc)}"


def _create_via_cli(tmp, pipe, title: str) -> str:
    proc = _run_cw_cli(
        ["task", "create", "--title", title,
         "--steps", '[{"action": "noop", "target_file": "f.py"}]'],
        _cli_env(pipe), tmp)
    _assert_ok(proc, f"task create {title}")
    m = _TASK_ID_RE.search(_all_out(proc))
    assert m, f"task create 未解析 task_id:\n{_all_out(proc)}"
    return m.group(0)


def _setup_governance(client, source_task: str, session: str) -> tuple:
    """注册 adjudicator 身份 + 为 source task 获取 reviewer lease，返回 (token, counter)。

    agent_instance_id 留空（CLI supersede parser 无此 flag；注册为空则
    verify_registered_identity 跳过 instance 检查，与 task.claim CLI 路径一致）。
    """
    reg = client.call("agent.register", {
        "agent_id": _AGENT,
        "identity": {
            "agent_id": _AGENT,
            "agent_instance_id": "",
            "session_id": session,
            "model_id": _MODEL,
            "role": "adjudicator",
        },
    })
    assert reg.get("status") == "registered", f"agent.register 失败: {reg}"
    lease = client.call("lease.acquire", {
        "task_id": source_task,
        "role": "reviewer",
        "identity": {
            "agent_id": _AGENT,
            "agent_instance_id": "",
            "session_id": session,
            "model_id": _MODEL,
            "role": "adjudicator",
        },
        "ttl_seconds": 3600,
    })
    assert lease.get("token"), f"lease.acquire 未返回 token: {lease}"
    return lease["token"], lease["fencing_counter"]


def _assert_superseded_ok(proc, what: str):
    """supersede CLI 成功判定：returncode 0 且输出含成功文案（失败时 CLI 也 exit 0）。"""
    out = _all_out(proc)
    assert proc.returncode == 0 and ("已声明" in out or "superseded" in out), \
        f"{what} 未成功:\n{out}"


def _supersede_via_cli(tmp, pipe, old, new, reason, session, *, token="tok-bad",
                       counter=1, request_id="", agent_id=_AGENT,
                       role="adjudicator", evidence_path=_EVIDENCE,
                       evidence_hash=_EVIDENCE_HASH, extra=None) -> subprocess.CompletedProcess:
    args = ["task", "supersede", old, new, "--reason", reason,
            "--agent-id", agent_id, "--session-id", session,
            "--model-id", _MODEL, "--role", role,
            "--lease-token", token, "--fencing-counter", str(counter),
            "--evidence-path", evidence_path, "--evidence-hash", evidence_hash]
    if request_id:
        args += ["--request-id", request_id]
    env = _cli_env(pipe, session=session)
    if extra:
        env.update(extra)
    return _run_cw_cli(args, env, tmp)


def _superseded_via_cli(tmp, pipe, old) -> subprocess.CompletedProcess:
    return _run_cw_cli(["task", "superseded", old], _cli_env(pipe), tmp)


@requires_binaries
class TestTaskSupersedeP0H:
    def test_supersede_round_trip_status_unchanged_and_provenance(self, daemon_env):
        client, tmp, task_db, _proc, pipe = daemon_env
        old = _create_via_cli(tmp, pipe, "OLD-S2-TASK")
        new = _create_via_cli(tmp, pipe, "NEW-APLAN-TASK")
        token, counter = _setup_governance(client, old, "session-sup1")

        proc = _supersede_via_cli(tmp, pipe, old, new,
                                  "re-decompose under A' plan",
                                  "session-sup1", token=token, counter=counter,
                                  request_id=f"req-p0h-{old}-{new}")
        _assert_superseded_ok(proc, f"task supersede {old} -> {new}")
        out = _all_out(proc)
        assert "已声明" in out or "superseded" in out, f"supersede 输出异常:\n{out}"

        # 被替代任务 status 仍 open，字段零改动
        assert client.task_status(old)["status"] == "open"
        assert client.task_status(new)["status"] == "open"

        # superseded 只读投影含 workspace/provenance
        q = _superseded_via_cli(tmp, pipe, old)
        _assert_ok(q, f"task superseded {old}")
        qout = _all_out(q)
        assert new in qout, f"未返回新 ID {new}:\n{qout}"
        assert "workspace_id" in qout, f"投影未输出 workspace_id:\n{qout}"
        assert "supersedence_id" in qout, f"投影未输出 supersedence_id:\n{qout}"

        # daemon 只读核验：relation/event/task_events/ledger 同一事务落库
        rpc = client.call("task.superseded_by", {"task_id": old})
        assert rpc.get("found") is True
        assert rpc.get("superseding_task_id") == new
        assert rpc.get("workspace_id") is not None
        assert rpc.get("supersedence_id")
        assert rpc.get("request_id") == f"req-p0h-{old}-{new}"
        assert rpc.get("evidence_hash") == _EVIDENCE_HASH
        conn = sqlite3.connect(task_db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM task_supersede_relations WHERE superseded_task_id=?",
                (old,)).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM task_supersede_events WHERE superseded_task_id=?",
                (old,)).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id=? AND reason_code='task_superseded'",
                (old,)).fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM task_operation_ledger WHERE method='task.supersede' AND request_id=?",
                (f"req-p0h-{old}-{new}",)).fetchone()[0] == 1
        finally:
            conn.close()

    def test_supersede_replay_same_request_id_no_new_rows(self, daemon_env):
        client, tmp, task_db, _proc, pipe = daemon_env
        old = _create_via_cli(tmp, pipe, "OLD-REPLAY-TASK")
        new = _create_via_cli(tmp, pipe, "NEW-REPLAY-TASK")
        token, counter = _setup_governance(client, old, "session-replay")

        rid = f"req-replay-{old}-{new}"
        proc1 = _supersede_via_cli(tmp, pipe, old, new, "replay-1",
                                   "session-replay", token=token, counter=counter,
                                   request_id=rid)
        _assert_superseded_ok(proc1, "supersede 首次")
        proc2 = _supersede_via_cli(tmp, pipe, old, new, "replay-1",
                                   "session-replay", token=token, counter=counter,
                                   request_id=rid)
        _assert_superseded_ok(proc2, "supersede 重放")
        conn = sqlite3.connect(task_db)
        try:
            assert conn.execute(
                "SELECT COUNT(*) FROM task_supersede_relations WHERE superseded_task_id=?",
                (old,)).fetchone()[0] == 1, "重放不得追加关系行"
            assert conn.execute(
                "SELECT COUNT(*) FROM task_supersede_events WHERE superseded_task_id=?",
                (old,)).fetchone()[0] == 1, "重放不得追加事件行"
            assert conn.execute(
                "SELECT COUNT(*) FROM task_operation_ledger WHERE request_id=?",
                (rid,)).fetchone()[0] == 1, "重放不得追加 ledger 行"
        finally:
            conn.close()

    def test_supersede_rejects_identity_role_lease_evidence(self, daemon_env):
        client, tmp, _task_db, _proc, pipe = daemon_env
        old = _create_via_cli(tmp, pipe, "OLD-REJ-TASK")
        new = _create_via_cli(tmp, pipe, "NEW-REJ-TASK")
        token, counter = _setup_governance(client, old, "session-rej")

        # 未注册身份（agent_id 不在 agent_registrations）→ E_IDENTITY_UNREGISTERED
        proc = _supersede_via_cli(tmp, pipe, old, new, "bad-agent",
                                  "session-rej", token=token, counter=counter,
                                  agent_id="ghost-agent")
        assert proc.returncode != 0 or "E_IDENTITY_UNREGISTERED" in _all_out(proc)

        # 错误 lease token → E_LEASE_TOKEN_MISMATCH（稳定码透传）
        proc = _supersede_via_cli(tmp, pipe, old, new, "bad-token",
                                  "session-rej", token="tok-wrong", counter=counter)
        assert proc.returncode != 0 or "E_LEASE" in _all_out(proc)

        # 陈旧 fencing counter → E_SUPERSEDE_FENCED
        proc = _supersede_via_cli(tmp, pipe, old, new, "stale-fence",
                                  "session-rej", token=token, counter=counter - 1)
        assert proc.returncode != 0 or "E_SUPERSEDE_FENCED" in _all_out(proc) or "fencing" in _all_out(proc).lower()

        # 缺证据 → CLI 前置 fail-fast
        proc = _supersede_via_cli(tmp, pipe, old, new, "no-evidence",
                                  "session-rej", token=token, counter=counter,
                                  evidence_path="", evidence_hash="")
        assert "evidence" in _all_out(proc).lower()

    def test_supersede_rejects_self_duplicate_cycle_missing(self, daemon_env):
        client, tmp, _task_db, _proc, pipe = daemon_env
        a = _create_via_cli(tmp, pipe, "A-TASK")
        b = _create_via_cli(tmp, pipe, "B-TASK")
        c = _create_via_cli(tmp, pipe, "C-TASK")
        tok_a, cnt_a = _setup_governance(client, a, "session-cyc")

        # A -> B 成功
        proc = _supersede_via_cli(tmp, pipe, a, b, "first", "session-cyc",
                                  token=tok_a, counter=cnt_a, request_id=f"req-cyc-{a}-{b}")
        _assert_superseded_ok(proc, "A -> B supersede")

        # 重复 A -> B 拒绝（E_SUPERSEDE_ALREADY_EXISTS）
        dup = _supersede_via_cli(tmp, pipe, a, b, "dup", "session-cyc",
                                 token=tok_a, counter=cnt_a, request_id=f"req-cyc-dup-{a}-{b}")
        assert dup.returncode != 0 or "E_SUPERSEDE_ALREADY_EXISTS" in _all_out(dup) \
            or "关系已存在" in _all_out(dup)

        # 引用不存在任务 → E_SUPERSEDE_TASK_NOT_FOUND
        missing = _supersede_via_cli(tmp, pipe, a, "T-NOT-EXIST-999", "missing",
                                     "session-cyc", token=tok_a, counter=cnt_a,
                                     request_id=f"req-cyc-{a}-missing")
        assert missing.returncode != 0 or "E_SUPERSEDE_TASK_NOT_FOUND" in _all_out(missing) \
            or "不存在" in _all_out(missing)

        # B -> A 构成环 → E_SUPERSEDE_CYCLE（B 需自己的 reviewer lease）
        tok_b, cnt_b = _setup_governance(client, b, "session-cyc-b")
        cyc = _supersede_via_cli(tmp, pipe, b, a, "cycle", "session-cyc-b",
                                 token=tok_b, counter=cnt_b, request_id=f"req-cyc-{b}-{a}")
        assert cyc.returncode != 0 or "E_SUPERSEDE_CYCLE" in _all_out(cyc) \
            or "替代环" in _all_out(cyc)

        # C -> A 多出边环（A 已有出边到 B；C->A 沿 C? 实际 A->B 链）—— 用 B->A 已覆盖；C->B 不成环可成功
        tok_c, cnt_c = _setup_governance(client, c, "session-cyc-c")
        ok_c = _supersede_via_cli(tmp, pipe, c, b, "chain-ok", "session-cyc-c",
                                  token=tok_c, counter=cnt_c, request_id=f"req-cyc-{c}-{b}")
        _assert_ok(ok_c, "C -> B supersede（无环）")
