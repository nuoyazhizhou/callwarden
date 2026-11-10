r"""task.supersede P0-H daemon 层耐久性与往返测试（T-1787277487109-758e56d0）。

覆盖（prove-durable-governance-and-cli-http-roundtrip）：
1. durable governance：relation + append-only event + 权威 task_events 审计 +
   operation ledger 同一 SQLite transaction 落库（DB 直查核验）；
2. daemon 重启后同 request_id 重放：只读重放已保存结果，不追加任何行
   （ledger 持久化 = 重启即恢复）；
3. 历史任务字段不变：status/description/applied_at/closed_at 前后快照一致
   （append-only 语义）；
4. CLI→RPC→Rust round-trip：CLI 子进程 → HTTPS/mTLS → Rust handler → 只读投影回读；

前置：Windows + 已构建 cw-daemon.exe（含 P0-H 新代码）。
"""

import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PY_EXE = sys.executable
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")

_TASK_ID_RE = re.compile(r"T-[0-9A-Za-z-]+")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="task.supersede daemon 测试需要 Windows 隔离 daemon",
)

_AGENT = "adjudicator-workbuddy-v1"
_REVIEWER_AGENT = "reviewer-supersede-daemon-v1"
_MODEL = "deepseek-v4"
_EVIDENCE = "evidence/verdict-pass.json"
_EVIDENCE_HASH = "sha256:ev-daemon-1"

_REAL_PKI_DIR = None


@pytest.fixture(scope="module")
def daemon_env():
    """启动隔离 HTTPS daemon，返回 (client, tmp, task_db, proc, endpoint)。

    隔离 daemon 会生成自己的 CA；客户端使用真实 PKI，故 fixture 内临时设置
    `CW_PKI_DIR`（隔离 daemon 继承该值，双方证书/CA 一致）。
    """
    global _REAL_PKI_DIR
    from callwarden.config import get_pki_dir
    from _w3_harness import seed_cli_task_authority, setup_w3_client

    _REAL_PKI_DIR = get_pki_dir()
    os.environ["CW_PKI_DIR"] = _REAL_PKI_DIR
    tmp = tempfile.mkdtemp(prefix="cw_supersede_daemon_")
    proc = None
    try:
        client, inst, endpoint, proc = setup_w3_client(tmp, _REPO_ROOT, ws_id=1)
        if client is None:
            pytest.skip("隔离 HTTPS daemon 前置不可用（二进制/PKI/端口）")
        task_db = os.path.join(tmp, "task.db")
        seed_cli_task_authority(
            task_db, ws_id=1, name="supersede-daemon",
            root_path=_REPO_ROOT, instance_id=inst,
        )
        yield client, tmp, task_db, proc, endpoint
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
        raise
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if not os.environ.get("CW_KEEP_SUPERSEDE_TMP"):
            shutil.rmtree(tmp, ignore_errors=True)


def _cli_env(endpoint: str, data_root: str, session: str = "") -> dict:
    env = dict(os.environ)
    env.pop("CW_AGENT_SESSION_ID", None)
    env["CW_DAEMON_MODE"] = "enterprise"
    env["CW_DAEMON_HTTP_ENDPOINT"] = endpoint
    env["CW_DAEMON_TRANSPORT"] = "http"
    env["USERPROFILE"] = os.path.join(data_root, "userhome")
    env["HOME"] = env["USERPROFILE"]
    env["CW_PKI_DIR"] = _REAL_PKI_DIR or env.get("CW_PKI_DIR", "")
    env["CW_TASK_WRITE_POLICY"] = "shared"
    env["CW_DAEMON_AUTOSTART_WINDOW"] = "0"
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CALLWARDEN_LANG"] = "zh_CN"
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


def _create_via_cli(tmp, endpoint, title: str) -> str:
    proc = _run_cw_cli(
        ["task", "create", "--title", title,
         "--steps", '[{"action": "noop", "target_file": "f.py"}]'],
        _cli_env(endpoint, tmp), _REPO_ROOT)
    assert proc.returncode == 0, f"task create 失败:\n{_all_out(proc)}"
    m = _TASK_ID_RE.search(_all_out(proc))
    assert m, f"task create 未解析 task_id:\n{_all_out(proc)}"
    return m.group(0)


def _setup_governance(client, source_task: str, session: str) -> tuple:
    """注册 adjudicator/reviewer 身份 + 为 source task 获取 reviewer lease。

    daemon 治理校验要求 reviewer lease holder 是 active registered reviewer
    且注册 session 与 lease session 一致；adjudicator 与 reviewer holder 不得
    是同一 agent/session——因此注册两个不同 agent，reviewer 用独立派生 session。
    """
    reviewer_session = f"reviewer-{session}"
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
    client.call("agent.register", {
        "agent_id": _REVIEWER_AGENT,
        "identity": {
            "agent_id": _REVIEWER_AGENT,
            "agent_instance_id": "",
            "session_id": reviewer_session,
            "model_id": _MODEL,
            "role": "reviewer",
        },
    })
    lease = client.call("lease.acquire", {
        "task_id": source_task,
        "role": "reviewer",
        "identity": {
            "agent_id": _REVIEWER_AGENT,
            "agent_instance_id": "",
            "session_id": reviewer_session,
            "model_id": _MODEL,
            "role": "reviewer",
        },
        "ttl_seconds": 3600,
    })
    return lease["token"], lease["fencing_counter"]


def _supersede_via_cli(tmp, endpoint, old, new, reason, session, token, counter,
                       request_id) -> subprocess.CompletedProcess:
    return _run_cw_cli(
        ["task", "supersede", old, new, "--reason", reason,
         "--agent-id", _AGENT, "--session-id", session,
         "--model-id", _MODEL, "--role", "adjudicator",
         "--lease-token", token, "--fencing-counter", str(counter),
         "--evidence-path", _EVIDENCE, "--evidence-hash", _EVIDENCE_HASH,
         "--request-id", request_id],
        _cli_env(endpoint, tmp, session=session), _REPO_ROOT)


class TestSupersedeDaemonDurability:
    def test_durable_governance_all_rows_same_transaction(self, daemon_env):
        client, tmp, task_db, _proc, endpoint = daemon_env
        old = _create_via_cli(tmp, endpoint, "DUR-OLD")
        new = _create_via_cli(tmp, endpoint, "DUR-NEW")
        token, counter = _setup_governance(client, old, "session-dur")
        rid = f"req-dur-{old}"

        proc = _supersede_via_cli(tmp, endpoint, old, new, "durable", "session-dur",
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
        client, tmp, task_db, _proc, endpoint = daemon_env
        old = _create_via_cli(tmp, endpoint, "HIST-OLD")
        new = _create_via_cli(tmp, endpoint, "HIST-NEW")
        conn = sqlite3.connect(task_db)
        try:
            before = conn.execute(
                "SELECT status, description, applied_at, closed_at FROM tasks WHERE id=?",
                (old,)).fetchone()
        finally:
            conn.close()

        token, counter = _setup_governance(client, old, "session-hist")
        proc = _supersede_via_cli(tmp, endpoint, old, new, "hist", "session-hist",
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
        client, tmp, task_db, _proc, endpoint = daemon_env
        old = _create_via_cli(tmp, endpoint, "RT-OLD")
        new = _create_via_cli(tmp, endpoint, "RT-NEW")
        token, counter = _setup_governance(client, old, "session-rt")
        rid = f"req-rt-{old}"

        proc = _supersede_via_cli(tmp, endpoint, old, new, "rt", "session-rt",
                                  token, counter, rid)
        assert proc.returncode == 0 and "已声明" in _all_out(proc), \
            f"supersede 未成功:\n{_all_out(proc)}"

        # CLI 只读投影（workspace/provenance 字段）
        q = _run_cw_cli(["task", "superseded", old], _cli_env(endpoint, tmp), _REPO_ROOT)
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
        from callwarden.config import get_http_authority_id
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        from _w3_harness import find_daemon_binary, spawn_isolated_daemon, wait_manifest

        client, tmp, task_db, proc, endpoint = daemon_env
        old = _create_via_cli(tmp, endpoint, "RST-OLD")
        new = _create_via_cli(tmp, endpoint, "RST-NEW")
        token, counter = _setup_governance(client, old, "session-rst")
        rid = f"req-rst-{old}"

        proc1 = _supersede_via_cli(tmp, endpoint, old, new, "rst-1", "session-rst",
                                   token, counter, rid)
        assert proc1.returncode == 0 and "已声明" in _all_out(proc1), \
            f"首次 supersede 失败:\n{_all_out(proc1)}"

        # 重启隔离 daemon（同一 data_root/task DB；随机端口，无需等待旧端口释放）
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        bin_path = find_daemon_binary()
        if bin_path is None:
            pytest.skip("cw-daemon 二进制不可用（重启前置缺失）")
        proc2, _env = spawn_isolated_daemon(bin_path, tmp)
        try:
            m, man_file = wait_manifest(tmp, proc2)
            assert m is not None, \
                f"重启后 daemon 未在超时内落盘 manifest (exit={proc2.poll()})"
            endpoint2 = m["endpoint"]
            client2 = HttpDaemonRpcClient(
                endpoint=endpoint2, authority_id=get_http_authority_id(),
                manifest_path=man_file)

            # 同 request_id 同参数 → 只读重放（ledger 重启后仍在）
            proc3 = _supersede_via_cli(tmp, endpoint2, old, new, "rst-1", "session-rst",
                                       token, counter, rid)
            assert proc3.returncode == 0 and "已声明" in _all_out(proc3), \
                f"重启后重放失败:\n{_all_out(proc3)}"
            assert client2.call("task.superseded_by", {"task_id": old}).get("found") is True

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
            if proc2.poll() is None:
                proc2.terminate()
                try:
                    proc2.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc2.kill()
