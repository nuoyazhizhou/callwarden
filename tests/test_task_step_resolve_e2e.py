r"""baf7e552 S6：`task.step.resolve` 失败步骤 remediation 回审的进程级验收测试。

覆盖 §4 冻结合同的 resolution 场景（cw-role-handoff-task-loop.md §4 L509-559、
§6 验收矩阵）：
1. 成功路径：failed step + done fix_defect（provenance 链接一致）+ implementer
   lease + evidence → 追加 `step_resolved` 事件，任务推进 `review`，
   original failed 行**不可变**（仍为 failed）。
2. 重放：同 request_id + 同参数 → 由 HTTP 控制面 dedup 层回放首次 resolution
   结果（响应逐字节一致、同 resolution_event_id、不新增事件；权威层见
   只读评审.md L91，handler 级 `replayed=true` 分支仅在 dedup 行失效后可达）；
   同 request_id + 不同参数 → `E_REQUEST_ID_REUSE_MISMATCH`。
3. 负向（7）：`E_FAILED_STEP_NOT_FOUND` / `E_FAILED_STEP_NOT_UNRESOLVED` /
   `E_REMEDIATION_NOT_DONE` / `E_REMEDIATION_STEP_MISMATCH` /
   `E_RESOLUTION_EVIDENCE_REQUIRED` / `E_LEASE_REQUIRED` /
   `E_LEASE_TOKEN_MISMATCH`，并证明拒绝零写入。
4. CLI 进程级：`cw.py task step-resolve`（enterprise + daemon）成功 JSON /
   人类可读双渲染；local 模式 fail-closed（E_DAEMON_UNAVAILABLE，无 evaluator）。

前置条件（与 test_lease_gate_empirical.py 一致）：
1. Windows 平台；
2. 已构建 `cw-daemon.exe`（含 HTTP/mTLS 支持）；
3. 可用 mTLS PKI；隔离 daemon 与客户端共用真实 PKI（fixture 内设置
   `CW_PKI_DIR`）。

测试只用隔离临时任务库，绝不触碰真实用户任务库。
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
_PY_EXE = sys.executable
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="task.step.resolve 进程级 E2E 需要 Windows 隔离 daemon",
)

_REAL_PKI_DIR = None
_WS_INSTANCE_ID = ""


@pytest.fixture(scope="module")
def resolve_env():
    """启动隔离 HTTPS daemon，返回 (client, tmp, task_db, proc, endpoint)。"""
    global _REAL_PKI_DIR, _WS_INSTANCE_ID
    from callwarden.config import get_pki_dir
    from _w3_harness import seed_cli_task_authority, setup_w3_client

    _REAL_PKI_DIR = get_pki_dir()
    os.environ["CW_PKI_DIR"] = _REAL_PKI_DIR
    tmp = tempfile.mkdtemp(prefix="cw_step_resolve_")
    proc = None
    try:
        client, inst, endpoint, proc = setup_w3_client(tmp, _REPO_ROOT, ws_id=1)
        if client is None:
            pytest.skip("隔离 HTTPS daemon 前置不可用（二进制/PKI/端口）")
        _WS_INSTANCE_ID = inst
        task_db = os.path.join(tmp, "task.db")
        seed_cli_task_authority(
            task_db, ws_id=1, name="resolve-test",
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
        if not os.environ.get("CW_KEEP_6A_TMP"):
            shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# 播种 helpers
# ----------------------------------------------------------------------

def _seed_failed_remediation(task_db: str, task_id: str, failed_step_id: str,
                             remediation_step_id: str,
                             failed_status: str = "failed",
                             remediation_status: str = "done",
                             remediation_linked_to: str = None,
                             task_status: str = "in_progress") -> None:
    """直接播种：task + failed step + done fix_defect（provenance 可配置）。

    与 daemon `handle_task_step_resolve` 的校验契约对齐：
    - failed 行 status='failed'（可覆写以触发 E_FAILED_STEP_NOT_UNRESOLVED）；
    - remediation 行 action='fix_defect'、status='done'、result JSON 含
      `remediation_of_step_id`（可覆写以触发 E_REMEDIATION_STEP_MISMATCH）。
    """
    now = time.time()
    linked = remediation_linked_to if remediation_linked_to is not None else failed_step_id
    result = json.dumps({"remediation_of_step_id": linked}, ensure_ascii=False)
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at) "
            "VALUES (?1, 'resolve-seed', '', 'resolve-test', ?2, ?3, ?3)",
            (task_id, task_status, now),
        )
        # daemon lease.acquire 要求 canonical task→workspace binding（fail-closed）。
        conn.execute(
            "INSERT OR IGNORE INTO task_workspace_bindings "
            "(task_id, workspace_id, workspace_binding_id, workspace_capture_id,"
            " created_by, authoritative_created_at)"
            " VALUES (?, 1, ?, ?, 'resolve-test', ?)",
            (task_id, f"tb-{task_id}", f"wc-cli-{_WS_INSTANCE_ID}", now),
        )
        conn.execute(
            "INSERT INTO task_steps (id, task_id, step_index, action, target_file, status, result, created_at) "
            "VALUES (?1, ?2, 1, 'implement', 'f.py', ?3, '', ?4)",
            (failed_step_id, task_id, failed_status, now),
        )
        conn.execute(
            "INSERT INTO task_steps (id, task_id, step_index, action, target_file, status, result, created_at) "
            "VALUES (?1, ?2, 2, 'fix_defect', 'f.py', ?3, ?4, ?5)",
            (remediation_step_id, task_id, remediation_status, result, now),
        )
        conn.commit()
    finally:
        conn.close()


def _implementer_identity(tag: str) -> dict:
    return {
        "agent_id": f"agent-resolve-{tag}",
        "session_id": f"session-resolve-{tag}",
        "model_id": f"model-resolve-{tag}",
        "role": "implementer",
    }


def _acquire_implementer_lease(client, task_id: str, tag: str) -> dict:
    return client.call("lease.acquire", {
        "task_id": task_id,
        "role": "implementer",
        "identity": _implementer_identity(tag),
        "ttl_seconds": 3600.0,
    })


def _resolve_params(client, task_id: str, failed_step_id: str, remediation_step_id: str,
                    request_id: str, tag: str, acq: dict, evidence_path: str = "docs/evidence/resolve.md",
                    evidence_hash: str = "sha256:deadbeef") -> dict:
    return {
        "task_id": task_id,
        "failed_step_id": failed_step_id,
        "remediation_step_id": remediation_step_id,
        "request_id": request_id,
        "evidence_path": evidence_path,
        "evidence_hash": evidence_hash,
        "identity": _implementer_identity(tag),
        # .get()：test_lease_required 传 acq={} 且随后 pop 掉这两个键，
        # 这里不能先于 pop 抛 KeyError。
        "lease_token": acq.get("token"),
        "fencing_counter": acq.get("fencing_counter"),
    }


def _count_step_resolved_events(task_db: str, task_id: str) -> int:
    conn = sqlite3.connect(task_db)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?1 AND reason_code = 'step_resolved'",
            (task_id,),
        ).fetchone()[0]
    finally:
        conn.close()


def _failed_row_status(task_db: str, failed_step_id: str) -> str:
    conn = sqlite3.connect(task_db)
    try:
        return conn.execute(
            "SELECT status FROM task_steps WHERE id = ?", (failed_step_id,)
        ).fetchone()[0]
    finally:
        conn.close()


# ----------------------------------------------------------------------
# 1) wire 级：成功 + 重放 + 负向（禁 mock）
# ----------------------------------------------------------------------

class TestStepResolveWireLevel:
    """真实 cw-daemon.exe 上 task.step.resolve 的验收。"""

    def test_success_advances_review_keeps_failed_immutable(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-OK"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)
        acq = _acquire_implementer_lease(client, task_id, "ok")

        res = client.call("task.step.resolve", _resolve_params(
            client, task_id, failed_step_id, remediation_step_id, "req-ok", "ok", acq))

        assert res["task_id"] == task_id
        assert res["replayed"] is False
        assert res["resolution_event_id"] > 0
        assert res["status"] == "review", res
        assert client.call("task.status", {"task_id": task_id})["status"] == "review"
        # original failed 行不可变：仍为 failed（append-only，不覆盖）
        assert _failed_row_status(task_db, failed_step_id) == "failed"
        assert _count_step_resolved_events(task_db, task_id) == 1

    def test_replay_same_request_id_no_duplicate_event(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-REPLAY"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)
        acq = _acquire_implementer_lease(client, task_id, "replay")

        params = _resolve_params(client, task_id, failed_step_id, remediation_step_id,
                                 "req-replay", "replay", acq)
        first = client.call("task.step.resolve", params)
        second = client.call("task.step.resolve", params)

        # 契约 §4.5：「重复 request-id 重放同一 resolution 结果」。
        # 权威重放由 HTTP 控制面 dedup 层承担（http_server.rs DedupStore；
        # 只读评审.md L91 明确要求停用 handler 级 dedup、统一到持久 dedup 层）：
        # 同 request_id + 同 params_hash 直接回放首次响应，handler 不再执行，
        # 故响应与首次逐字节一致（replayed 沿用首次的 False）。handler 级
        # ledger replay 分支（replayed=True）仅在 dedup 行失效后可达。
        # 可观测不变量：不新增事件、同一 resolution_event_id、幂等。
        assert second == first, second
        assert first["replayed"] is False and first["resolution_event_id"] > 0
        assert _count_step_resolved_events(task_db, task_id) == 1

    def test_request_id_reuse_mismatch(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-MISMATCH"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)
        acq = _acquire_implementer_lease(client, task_id, "mismatch")

        params = _resolve_params(client, task_id, failed_step_id, remediation_step_id,
                                 "req-mismatch", "mismatch", acq,
                                 evidence_path="docs/evidence/a.md")
        client.call("task.step.resolve", params)
        # 同 request_id + 不同 evidence → 冲突
        params2 = _resolve_params(client, task_id, failed_step_id, remediation_step_id,
                                  "req-mismatch", "mismatch", acq,
                                  evidence_path="docs/evidence/b.md")
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.step.resolve", params2)
        assert exc.value.code == "E_REQUEST_ID_REUSE_MISMATCH", exc.value
        assert _count_step_resolved_events(task_db, task_id) == 1

    def test_failed_step_not_found(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-NOTFOUND"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)
        acq = _acquire_implementer_lease(client, task_id, "notfound")

        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.step.resolve", _resolve_params(
                client, task_id, "T-RES-NOTFOUND-no-such", remediation_step_id,
                "req-notfound", "notfound", acq))
        assert exc.value.code == "E_FAILED_STEP_NOT_FOUND", exc.value

    def test_failed_step_not_unresolved(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-NOTUNRES"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        # failed 行已不是 failed（如已 done）→ E_FAILED_STEP_NOT_UNRESOLVED
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id,
                                 failed_status="done")
        acq = _acquire_implementer_lease(client, task_id, "notunres")

        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.step.resolve", _resolve_params(
                client, task_id, failed_step_id, remediation_step_id,
                "req-notunres", "notunres", acq))
        assert exc.value.code == "E_FAILED_STEP_NOT_UNRESOLVED", exc.value

    def test_remediation_not_done(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-REMD"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id,
                                 remediation_status="pending")
        acq = _acquire_implementer_lease(client, task_id, "remd")

        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.step.resolve", _resolve_params(
                client, task_id, failed_step_id, remediation_step_id,
                "req-remd", "remd", acq))
        assert exc.value.code == "E_REMEDIATION_NOT_DONE", exc.value

    def test_remediation_step_mismatch(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-MISLINK"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        # remediation provenance 链接到别的 failed 步骤 → MISMATCH
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id,
                                 remediation_linked_to="other-failed")
        acq = _acquire_implementer_lease(client, task_id, "misl")

        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.step.resolve", _resolve_params(
                client, task_id, failed_step_id, remediation_step_id,
                "req-misl", "misl", acq))
        assert exc.value.code == "E_REMEDIATION_STEP_MISMATCH", exc.value

    def test_evidence_required(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-EVID"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)
        acq = _acquire_implementer_lease(client, task_id, "evid")

        params = _resolve_params(client, task_id, failed_step_id, remediation_step_id,
                                 "req-evid", "evid", acq)
        params["evidence_path"] = ""
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.step.resolve", params)
        assert exc.value.code == "E_RESOLUTION_EVIDENCE_REQUIRED", exc.value

    def test_lease_required(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-NOLEASE"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)

        params = _resolve_params(client, task_id, failed_step_id, remediation_step_id,
                                 "req-nolease", "nolease", {})
        params.pop("lease_token")
        params.pop("fencing_counter")
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.step.resolve", params)
        assert exc.value.code == "E_LEASE_REQUIRED", exc.value

    def test_lease_token_mismatch(self, resolve_env):
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-TOKEN"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)
        _acquire_implementer_lease(client, task_id, "token")

        params = _resolve_params(client, task_id, failed_step_id, remediation_step_id,
                                 "req-token", "token",
                                 {"token": "bogus-token", "fencing_counter": 1})
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("task.step.resolve", params)
        assert exc.value.code == "E_LEASE_TOKEN_MISMATCH", exc.value

    def test_negative_never_mutates(self, resolve_env):
        """全部负向拒绝零写入：无 step_resolved 事件、failed 行不可变、无新 action identity。"""
        client, _tmp, task_db, _proc, _endpoint = resolve_env
        task_id = "T-RES-ZERO"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)
        acq = _acquire_implementer_lease(client, task_id, "zero")

        before_events = _count_step_resolved_events(task_db, task_id)
        before_status = client.call("task.status", {"task_id": task_id})["status"]

        # E_FAILED_STEP_NOT_UNRESOLVED（done 行）+ E_REQUEST_ID_REUSE_MISMATCH
        for bad in [
            {"task_id": task_id, "failed_step_id": "no-such",
             "remediation_step_id": remediation_step_id, "request_id": "req-z1",
             "evidence_path": "docs/evidence/x.md", "evidence_hash": "sha256:x",
             "identity": _implementer_identity("zero"),
             "lease_token": acq["token"], "fencing_counter": acq["fencing_counter"]},
        ]:
            with pytest.raises(DaemonRemoteError):
                client.call("task.step.resolve", bad)

        after_events = _count_step_resolved_events(task_db, task_id)
        assert after_events == before_events, "拒绝后新增了 step_resolved 事件"
        assert client.call("task.status", {"task_id": task_id})["status"] == before_status, "拒绝后任务状态被改动"
        assert _failed_row_status(task_db, failed_step_id) == "failed"


# ----------------------------------------------------------------------
# 2) CLI 进程级：真实 cw.py 子进程
# ----------------------------------------------------------------------

def _cli_env(endpoint: str, data_root: str, mode: str = "enterprise",
             extra: dict = None) -> dict:
    env = dict(os.environ)
    env.pop("CW_AGENT_SESSION_ID", None)
    env["CW_DAEMON_MODE"] = mode
    if endpoint:
        env["CW_DAEMON_HTTP_ENDPOINT"] = endpoint
        env["CW_DAEMON_TRANSPORT"] = "http"
    env["USERPROFILE"] = os.path.join(data_root, "userhome")
    env["HOME"] = env["USERPROFILE"]
    env["CW_PKI_DIR"] = _REAL_PKI_DIR or env.get("CW_PKI_DIR", "")
    env["CW_TASK_WRITE_POLICY"] = "shared"
    env["CW_DAEMON_AUTOSTART_WINDOW"] = "0"
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CALLWARDEN_LANG"] = "zh_CN"
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


class TestStepResolveCliProcess:
    r"""真实 `C:\Python314\python.exe cw.py task step-resolve` 子进程验收。"""

    def test_cli_json_success(self, resolve_env):
        client, tmp, task_db, _proc, endpoint = resolve_env
        task_id = "T-RES-CLI-OK"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)
        acq = _acquire_implementer_lease(client, task_id, "cli-ok")

        proc = _run_cw_cli(
            ["task", "step-resolve", task_id, failed_step_id, remediation_step_id,
             "req-cli-ok",
             "--evidence-path", "docs/evidence/cli.md",
             "--evidence-hash", "sha256:cli",
             "--agent-id", "agent-resolve-cli-ok",
             "--session-id", "session-resolve-cli-ok",
             "--model-id", "model-resolve-cli-ok",
             "--role", "implementer",
             "--lease-token", acq["token"],
             "--fencing-counter", str(acq["fencing_counter"]),
             "--json"],
            _cli_env(endpoint, tmp), _REPO_ROOT)
        assert proc.returncode == 0, f"step-resolve --json 失败:\n{_all_out(proc)}"
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            pytest.fail(f"--json 输出不是合法 JSON:\n{_all_out(proc)} ({exc})")
        assert data["task_id"] == task_id
        assert data["replayed"] is False
        assert data["status"] == "review", data

    def test_cli_human_readable_success(self, resolve_env):
        client, tmp, task_db, _proc, endpoint = resolve_env
        task_id = "T-RES-CLI-HUMAN"
        failed_step_id = f"{task_id}-failed"
        remediation_step_id = f"{task_id}-fix"
        _seed_failed_remediation(task_db, task_id, failed_step_id, remediation_step_id)
        acq = _acquire_implementer_lease(client, task_id, "cli-human")

        proc = _run_cw_cli(
            ["task", "step-resolve", task_id, failed_step_id, remediation_step_id,
             "req-cli-human",
             "--evidence-path", "docs/evidence/cli-human.md",
             "--evidence-hash", "sha256:ch",
             "--agent-id", "agent-resolve-cli-human",
             "--session-id", "session-resolve-cli-human",
             "--model-id", "model-resolve-cli-human",
             "--role", "implementer",
             "--lease-token", acq["token"],
             "--fencing-counter", str(acq["fencing_counter"])],
            _cli_env(endpoint, tmp), _REPO_ROOT)
        assert proc.returncode == 0, f"step-resolve 人类可读失败:\n{_all_out(proc)}"
        out = _all_out(proc)
        assert "步骤回审" in out, out
        assert "review" in out, out

    def test_cli_local_mode_fail_closed(self):
        """local 模式无 resolution authority：fail-closed（E_DAEMON_UNAVAILABLE）。"""
        tmp = tempfile.mkdtemp(prefix="cw_6a_local_")
        try:
            proc = _run_cw_cli(
                ["task", "step-resolve", "T-RES-LOCAL", "s-failed", "s-fix", "req-local",
                 "--evidence-path", "docs/evidence/l.md", "--evidence-hash", "sha256:l",
                 "--agent-id", "a", "--session-id", "s", "--model-id", "m",
                 "--role", "implementer",
                 "--lease-token", "t", "--fencing-counter", "1",
                 "--json"],
                _cli_env("", tmp, mode="local",
                         extra={"CW_TASK_WRITE_POLICY": "isolated"}), tmp)
            assert proc.returncode == 0, f"local 模式 step-resolve 失败:\n{_all_out(proc)}"
            data = json.loads(proc.stdout)
            assert data["ok"] is False
            assert data["code"] == "E_DAEMON_UNAVAILABLE", data
            assert "local 模式" in data["message"], data
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
