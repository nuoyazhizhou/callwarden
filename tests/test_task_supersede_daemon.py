r"""task.supersede P0-H daemon 层耐久性与往返测试（T-1787277487109-758e56d0）。

覆盖（prove-durable-governance-and-cli-http-roundtrip）：
1. durable governance：relation + append-only event + 权威 task_events 审计 +
   operation ledger 同一 SQLite transaction 落库（DB 直查核验）；
2. daemon 重启后同 request_id 重放：只读重放已保存结果，不追加任何行
   （ledger 持久化 = 重启即恢复）；
3. 历史任务字段不变：status/description/applied_at/closed_at 前后快照一致
   （append-only 语义）；
4. CLI→RPC→Rust round-trip：CLI 子进程 → Named Pipe → Rust handler → 只读投影回读；
   （HTTP 传输与 pipe 共用 dispatch.rs 同一 handler；/capabilities 的
   pending_promotion manifest 由 rust http_server 单元测试覆盖）

前置：Windows + 已构建 cw-daemon.exe（含 P0-H 新代码）。
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
    reason="task.supersede daemon 测试需要 Windows + Named Pipe",
)

requires_binaries = pytest.mark.skipif(
    not os.path.exists(_DAEMON_BIN),
    reason="cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）",
)

_AGENT = "adjudicator-workbuddy-v1"
_MODEL = "deepseek-v4"
_EVIDENCE = "evidence/verdict-pass.json"
_EVIDENCE_HASH = "sha256:ev-daemon-1"


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


def _spawn_daemon(tmp: str, pipe: str, log):
    config = _daemon_config(tmp)
    config_path = os.path.join(tmp, "daemon.json")
    if not os.path.exists(config_path):
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)
    return subprocess.Popen(
        [_DAEMON_BIN, "--config", config_path],
        stdout=log, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
    )


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

    tmp = tempfile.mkdtemp(prefix="cw_supersede_daemon_")
    log = open(os.path.join(tmp, "daemon.log"), "w", encoding="utf-8")
    proc = _spawn_daemon(tmp, pipe, log)

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
            "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (1, 'supersede-daemon', '.', ?1)",
            (time.time(),),
        )
        conn.commit()
    finally:
        conn.close()

    yield client, tmp, task_db, proc, pipe, log

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if not os.environ.get("CW_KEEP_SUPERSEDE_TMP"):
        shutil.rmtree(tmp, ignore_errors=True)


def _cli_env(pipe: str, session: str = "") -> dict:
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
    return env


def _run_cw_cli(args, env, cwd, timeout=90):
    return subprocess.run(
        [_PY_EXE, _CW_PY] + args,
        env=env, cwd=cwd, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _all_out(proc) -> str:
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _create_via_cli(tmp, pipe, title: str) -> str:
    proc = _run_cw_cli(
        ["task", "create", "--title", title,
         "--steps", '[{"action": "noop", "target_file": "f.py"}]'],
        _cli_env(pipe), tmp)
    assert proc.returncode == 0, f"task create 失败:\n{_all_out(proc)}"
    m = _TASK_ID_RE.search(_all_out(proc))
    assert m, f"task create 未解析 task_id:\n{_all_out(proc)}"
    return m.group(0)


def _setup_governance(client, source_task: str, session: str) -> tuple:
    """注册 adjudicator 身份 + 为 source task 获取 reviewer lease，返回 (token, counter)。

    注意：agent_instance_id 留空——CLI 的 supersede parser 无 --agent-instance-id flag；
    verify_registered_identity 对 instance 仅在"注册非空且与请求不一致"时拒绝，
    注册为空则跳过该检查（与 task.claim 的 CLI 路径一致）。
    """
    client.call("agent.register", {
        "agent_id": _AGENT,
        "identity": {
            "agent_id": _AGENT,
            "agent_instance_id": "",
            "session_id": session,
            "model_id": _MODEL,
            "role": "adjudicator",
        },
    })
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
    return lease["token"], lease["fencing_counter"]


def _supersede_via_cli(tmp, pipe, old, new, reason, session, token, counter,
                       request_id) -> subprocess.CompletedProcess:
    return _run_cw_cli(
        ["task", "supersede", old, new, "--reason", reason,
         "--agent-id", _AGENT, "--session-id", session,
         "--model-id", _MODEL, "--role", "adjudicator",
         "--lease-token", token, "--fencing-counter", str(counter),
         "--evidence-path", _EVIDENCE, "--evidence-hash", _EVIDENCE_HASH,
         "--request-id", request_id],
        _cli_env(pipe, session=session), tmp)


@requires_binaries
class TestSupersedeDaemonDurability:
    def test_durable_governance_all_rows_same_transaction(self, daemon_env):
        client, tmp, task_db, _proc, pipe, _log = daemon_env
        old = _create_via_cli(tmp, pipe, "DUR-OLD")
        new = _create_via_cli(tmp, pipe, "DUR-NEW")
        token, counter = _setup_governance(client, old, "session-dur")
        rid = f"req-dur-{old}"

        proc = _supersede_via_cli(tmp, pipe, old, new, "durable", "session-dur",
                                  token, counter, rid)
        assert proc.returncode == 0 and "已声明" in _all_out(proc), \
            f"supersede 未成功:\n{_all_out(proc)}"

        conn = sqlite3.connect(task_db)
        try:
            # relation + event + task_events 审计 + ledger 各一行（同一事务提交）
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
                (rid,)).fetchone()[0] == 1
            # relation 带 workspace/provenance
            rel = conn.execute(
                "SELECT workspace_id, supersedence_id, reason_code, request_id, lease_id, "
                "fencing_counter, evidence_path, evidence_hash, actor_role "
                "FROM task_supersede_relations WHERE superseded_task_id=?",
                (old,)).fetchone()
            assert rel is not None
            ws, sid, rcode, rid_db, lid, fence, epath, ehash, arole = rel
            assert ws is not None and ws > 0
            assert sid and rcode == "governance_supersede"
            assert rid_db == rid and lid and fence == counter
            assert epath == _EVIDENCE and ehash == _EVIDENCE_HASH
            assert arole == "adjudicator"
        finally:
            conn.close()

    def test_historical_task_fields_unchanged(self, daemon_env):
        client, tmp, task_db, _proc, pipe, _log = daemon_env
        old = _create_via_cli(tmp, pipe, "HIST-OLD")
        new = _create_via_cli(tmp, pipe, "HIST-NEW")
        conn = sqlite3.connect(task_db)
        try:
            before = conn.execute(
                "SELECT status, description, applied_at, closed_at FROM tasks WHERE id=?",
                (old,)).fetchone()
        finally:
            conn.close()

        token, counter = _setup_governance(client, old, "session-hist")
        proc = _supersede_via_cli(tmp, pipe, old, new, "hist", "session-hist",
                                  token, counter, f"req-hist-{old}")
        assert proc.returncode == 0, f"supersede 失败:\n{_all_out(proc)}"

        conn = sqlite3.connect(task_db)
        try:
            after = conn.execute(
                "SELECT status, description, applied_at, closed_at FROM tasks WHERE id=?",
                (old,)).fetchone()
        finally:
            conn.close()
        assert before == after, f"历史任务字段被修改: before={before} after={after}"

    def test_cli_rpc_rust_roundtrip_projection(self, daemon_env):
        client, tmp, task_db, _proc, pipe, _log = daemon_env
        old = _create_via_cli(tmp, pipe, "RT-OLD")
        new = _create_via_cli(tmp, pipe, "RT-NEW")
        token, counter = _setup_governance(client, old, "session-rt")
        rid = f"req-rt-{old}"

        proc = _supersede_via_cli(tmp, pipe, old, new, "rt", "session-rt",
                                  token, counter, rid)
        assert proc.returncode == 0 and "已声明" in _all_out(proc), \
            f"supersede 未成功:\n{_all_out(proc)}"

        # CLI 只读投影（workspace/provenance 字段）
        q = _run_cw_cli(["task", "superseded", old], _cli_env(pipe), tmp)
        assert q.returncode == 0
        qout = _all_out(q)
        assert new in qout
        assert "workspace_id" in qout and "supersedence_id" in qout and "request_id" in qout

        # RPC 直达 Rust handler：完整 provenance 回读
        rpc = client.call("task.superseded_by", {"task_id": old})
        assert rpc.get("found") is True
        assert rpc.get("superseding_task_id") == new
        assert rpc.get("request_id") == rid
        assert rpc.get("reason_code") == "governance_supersede"
        assert rpc.get("evidence_hash") == _EVIDENCE_HASH
        assert rpc.get("actor_role") == "adjudicator"

    def test_restart_replay_same_request_id(self, daemon_env):
        """daemon 重启后同 request_id 重放：ledger 持久化，不追加任何行。"""
        from callwarden.server.daemon_client import UnixDaemonRpcClient

        client, tmp, task_db, proc, pipe, log = daemon_env
        old = _create_via_cli(tmp, pipe, "RST-OLD")
        new = _create_via_cli(tmp, pipe, "RST-NEW")
        token, counter = _setup_governance(client, old, "session-rst")
        rid = f"req-rst-{old}"

        proc1 = _supersede_via_cli(tmp, pipe, old, new, "rst-1", "session-rst",
                                   token, counter, rid)
        assert proc1.returncode == 0 and "已声明" in _all_out(proc1), \
            f"首次 supersede 失败:\n{_all_out(proc1)}"

        # 重启 daemon（同一 task DB / config）；先等管道真正释放，避免 bind 冲突
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                UnixDaemonRpcClient(socket_path=pipe, timeout=1).call("ping")
            except Exception:
                break  # 管道已释放（连接失败）
            time.sleep(0.5)
        time.sleep(1)

        proc2 = None
        log2 = None
        try:
            log2 = open(os.path.join(tmp, "daemon2.log"), "w", encoding="utf-8")
            proc2 = _spawn_daemon(tmp, pipe, log2)
            client2 = UnixDaemonRpcClient(socket_path=pipe, timeout=10)
            deadline = time.time() + 40
            ready = False
            while time.time() < deadline:
                if proc2.poll() is not None:
                    break
                try:
                    if client2.call("ping").get("status") == "ok":
                        ready = True
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            if not ready:
                log2.flush()
                tail = ""
                if os.path.exists(os.path.join(tmp, "daemon2.log")):
                    with open(os.path.join(tmp, "daemon2.log"), encoding="utf-8",
                              errors="replace") as f:
                        tail = f.read()[-3000:]
                pytest.fail(f"重启后 daemon 未就绪 (exit={proc2.poll()})\n{tail}")

            # 同 request_id 同参数 → 只读重放（ledger 重启后仍在）
            proc3 = _supersede_via_cli(tmp, pipe, old, new, "rst-1", "session-rst",
                                       token, counter, rid)
            assert proc3.returncode == 0 and "已声明" in _all_out(proc3), \
                f"重启后重放失败:\n{_all_out(proc3)}"

            conn = sqlite3.connect(task_db)
            try:
                assert conn.execute(
                    "SELECT COUNT(*) FROM task_supersede_relations WHERE superseded_task_id=?",
                    (old,)).fetchone()[0] == 1, "重启重放不得追加关系行"
                assert conn.execute(
                    "SELECT COUNT(*) FROM task_operation_ledger WHERE request_id=?",
                    (rid,)).fetchone()[0] == 1, "重启重放不得追加 ledger 行"
            finally:
                conn.close()
        finally:
            if proc2 is not None and proc2.poll() is None:
                proc2.terminate()
                try:
                    proc2.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc2.kill()
            if log2 is not None:
                log2.close()
