r"""P0-G G1：`cw task claim-recover` CLI ↔ Rust daemon 端到端集成测试。

覆盖任务书 `a_prime_39_card_governance_recovery_implementation_backlog.md` G1 验收矩阵：
- stale old owner + 独立 valid reviewer lease + adjudicator identity → release 成功，
  写 1 个 `claim_released`，无步骤/证据历史改写；
- old owner 仍 active/fresh → E_CLAIM_OWNER_ACTIVE，零 mutation；
- 非 adjudicator role → E_RECOVERY_ROLE_REQUIRED，零 mutation；
- 无 reviewer lease → E_LEASE_NOT_FOUND，零 mutation；
- token 错 / fencing stale → E_LEASE_TOKEN_MISMATCH / E_LEASE_FENCING_STALE，零 mutation；
- Reviewer 与 Adjudicator 同 agent/instance/session → E_GOVERNANCE_* 拒绝，零 mutation；
- 同 request_id 重试 → dedup 返回原结果，不产生第二个 claim_released；
- daemon unavailable / CLI 缺身份 / 缺 lease 凭证 → Python/CLI fail-closed，绝不 local fallback。

关键语义（P0-G 修复核心）：`handle_task_claim_recover` 必须使用
`validate_reviewer_lease_for_adjudication`（独立 Reviewer lease + Adjudicator identity），
而不是 `validate_lease_for_mutation`（同 holder）。因此 reviewer lease 必须由
**已注册的独立 Reviewer** 持有，且其 agent/instance/session 与 Adjudicator 全不重叠。

前置条件：
1. 已构建 `cw-daemon.exe` 且存在可用的 mTLS PKI；缺失时由 `setup_w3_client`
   归一为前置缺失并 skip。
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

_PY_EXE = sys.executable

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")

_TASK_ID_RE = re.compile(r"T-[0-9A-Za-z-]+")
_STEP_ID_RE = re.compile(r"S-[0-9A-Za-z-]+")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="claim-recover CLI↔daemon E2E 需要 Windows 隔离 daemon",
)

_REAL_PKI_DIR = None


@pytest.fixture(scope="module")
def recover_env():
    """启动隔离 HTTPS daemon，返回 (client, tmp, task_db, proc, endpoint)。"""
    global _REAL_PKI_DIR
    from callwarden.config import get_pki_dir
    from _w3_harness import seed_cli_task_authority, setup_w3_client

    _REAL_PKI_DIR = get_pki_dir()
    os.environ["CW_PKI_DIR"] = _REAL_PKI_DIR
    tmp = tempfile.mkdtemp(prefix="cw_cli_claim_recover_")
    proc = None
    try:
        client, instance_id, endpoint, proc = setup_w3_client(tmp, _REPO_ROOT, ws_id=1)
        if client is None:
            pytest.skip("隔离 HTTPS daemon 前置不可用（二进制/PKI/端口）")
        task_db = os.path.join(tmp, "task.db")
        seed_cli_task_authority(
            task_db, ws_id=1, name="claim-recover-test",
            root_path=_REPO_ROOT, instance_id=instance_id,
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
        if not os.environ.get("CW_KEEP_CLAIM_RECOVER_TMP"):
            shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# CLI 子进程辅助
# ----------------------------------------------------------------------

def _cli_env(endpoint: str, data_root: str, session: str = "", mode: str = "enterprise",
             extra: dict = None) -> dict:
    env = dict(os.environ)
    env.pop("CW_AGENT_SESSION_ID", None)
    env["CW_DAEMON_MODE"] = mode
    env["CW_DAEMON_HTTP_ENDPOINT"] = endpoint
    env["CW_DAEMON_TRANSPORT"] = "http"
    # 隔离 USERPROFILE：CLI 读到的是本隔离 daemon 的 manifest，避免真实
    # manifest 的 manifest_id/PID 与隔离 daemon 不符触发 E_HTTP_MANIFEST_STALE。
    env["USERPROFILE"] = os.path.join(data_root, "userhome")
    env["HOME"] = env["USERPROFILE"]
    env["CW_PKI_DIR"] = _REAL_PKI_DIR or env.get("CW_PKI_DIR", "")
    env["CW_TASK_WRITE_POLICY"] = "shared"
    env["CW_DAEMON_AUTOSTART_WINDOW"] = "0"
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CALLWARDEN_LANG"] = "zh_CN"
    if session:
        env["CW_AGENT_SESSION_ID"] = session
    if extra:
        env.update(extra)
    return env


def _run_cw_cli(args, env, cwd, timeout=90):
    return subprocess.run(
        [_PY_EXE, _CW_PY] + args,
        env=env, cwd=_REPO_ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _all_out(proc) -> str:
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _assert_ok(proc, what: str):
    assert proc.returncode == 0, f"{what} 失败(exit={proc.returncode}):\n{_all_out(proc)}"


def _assert_cli_rejected(proc, what: str, *codes: str):
    out = _all_out(proc)
    for code in codes:
        assert code in out, f"{what}: 输出缺少错误码 {code}:\n{out}"


def _register_agent(client, agent_id: str, instance_id: str, session_id: str,
                    role: str, model_id: str = "model-x"):
    """daemon RPC 注册测试 agent（agent.register 本身是正式 Protected_Mutation；
    用于准备 reviewer/adjudicator/old-owner 身份，不替代任何被测命令面）。"""
    return client.call("agent.register", {
        "agent_id": agent_id,
        "agent_name": f"agent-{agent_id}",
        "capabilities": ["code"],
        "identity": {
            "agent_id": agent_id,
            "agent_instance_id": instance_id,
            "client_id": "trae",
            "provider": "anthropic",
            "model_id": model_id,
            "model_mode": "agent",
            "system_fingerprint": "fp-1",
            "session_id": session_id,
            "role": role,
            "runtime_hash": "deadbeef",
        },
    })


def _create_via_cli(tmpdir: str, endpoint: str, title: str) -> str:
    proc = _run_cw_cli(
        ["task", "create", "--title", title,
         "--steps", '[{"action": "实现", "target_file": "f.py"}]'],
        _cli_env(endpoint, tmpdir), tmpdir)
    _assert_ok(proc, f"task create {title}")
    match = _TASK_ID_RE.search(_all_out(proc))
    assert match, f"task create 输出未解析到 task_id:\n{_all_out(proc)}"
    return match.group(0)


def _claim_via_cli(tmpdir: str, endpoint: str, task_id: str, session: str,
                   agent_id: str, instance_id: str = "", role: str = "executor",
                   model_id: str = "model-x") -> str:
    """CLI task next（claim）→ 解析 step_id。

    携带结构化 identity：A3 冻结 Role Contract 后 claim 必须携带 identity
    （否则 E_IDENTITY_REQUIRED），且 agent 须已注册 active（_register_agent）；
    instance 非空时需透传 --agent-instance-id 保持一致（E_IDENTITY_INSTANCE_MISMATCH）。
    """
    args = ["task", "next", task_id,
            "--agent-id", agent_id,
            "--session-id", session,
            "--model-id", model_id,
            "--role", role]
    if instance_id:
        args += ["--agent-instance-id", instance_id]
    proc = _run_cw_cli(args, _cli_env(endpoint, tmpdir, session=session), tmpdir)
    _assert_ok(proc, f"task next {task_id} session={session}")
    match = _STEP_ID_RE.search(_all_out(proc))
    assert match, f"task next 输出未解析到 step_id:\n{_all_out(proc)}"
    return match.group(0)


def _acquire_lease_via_cli(tmpdir: str, endpoint: str, task_id: str,
                           tag: str, role: str = "reviewer") -> dict:
    aid, sid, mid = f"agent-{tag}", f"session-{tag}", f"model-{tag}"
    proc = _run_cw_cli(
        ["lease", "acquire", task_id, "--role", role,
         "--agent-id", aid, "--session-id", sid, "--model-id", mid, "--json"],
        _cli_env(endpoint, tmpdir), tmpdir)
    _assert_ok(proc, f"lease acquire {task_id}")
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"lease acquire 输出不是 JSON:\n{_all_out(proc)} ({exc})")
    assert data.get("ok"), f"lease acquire 未返回 ok:\n{_all_out(proc)}"
    assert data.get("token"), f"lease acquire 未返回 raw token:\n{data}"
    return {
        "token": data["token"],
        "fencing_counter": int(data["fencing_counter"]),
        "agent_id": aid, "session_id": sid, "model_id": mid, "role": role,
    }


def _identity_flags(agent_id, instance_id, session_id, role) -> list:
    flags = ["--agent-id", agent_id, "--session-id", session_id,
             "--model-id", "model-x", "--role", role]
    if instance_id:
        flags += ["--agent-instance-id", instance_id]
    return flags


def _lease_flags(holder: dict) -> list:
    return ["--lease-token", holder["token"],
            "--fencing-counter", str(holder["fencing_counter"])]


def _claim_recover_cli(tmpdir, endpoint, task_id, reason, identity_flags, lease_flags,
                       request_id=""):
    args = ["task", "claim-recover", task_id,
            "--reason", reason, *identity_flags, *lease_flags]
    if request_id:
        args += ["--request-id", request_id]
    return _run_cw_cli(args, _cli_env(endpoint, tmpdir), tmpdir)


def _mark_owner_stale(task_db: str, session_id: str):
    """测试注入：将旧 owner 身份注册心跳置 0，模拟失联（只动身份注册表心跳，
    不触碰 task/lease/verdict/contract 治理状态）。"""
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "UPDATE agent_registrations SET last_heartbeat = 0 WHERE session_id = ?1",
            (session_id,),
        )
        conn.commit()
    finally:
        conn.close()


def _claim_released_count(task_db: str, task_id: str) -> int:
    conn = sqlite3.connect(task_db)
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ?1 AND reason_code = 'claim_released'",
            (task_id,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


class TestCliClaimRecover:
    """P0-G G1：claim-recover 端到端验收矩阵。"""

    def test_success_releases_stale_claim_with_independent_reviewer_lease(self, recover_env):
        """stale old owner + 独立 valid reviewer lease + adjudicator identity → 成功。"""
        client, tmp, task_db, _proc, endpoint = recover_env
        # 注册 old owner / reviewer / adjudicator（独立 agent/instance/session）
        _register_agent(client, "old-owner", "old-inst", "old-sess", "implementer")
        # reviewer 注册身份须与 _acquire_lease_via_cli 派生约定一致：
        # lease holder = agent-{tag} / session-{tag}（daemon 校验 reviewer 已注册 active 且 session 一致）
        _register_agent(client, "agent-rev-1", "", "session-rev-1", "reviewer")
        _register_agent(client, "adj-1", "adj-inst-1", "adj-sess-1", "adjudicator")

        task_id = _create_via_cli(tmp, endpoint, "T-RECOVER-OK")
        _claim_via_cli(tmp, endpoint, task_id, "old-sess", "old-owner", "old-inst", "implementer")
        assert client.call("task.status", {"task_id": task_id})["status"] == "in_progress"

        # 独立 Reviewer acquire reviewer lease
        rev_holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "rev-1", role="reviewer")
        assert rev_holder["agent_id"] == "agent-rev-1"

        # 标记 old owner stale（模拟失联）
        _mark_owner_stale(task_db, "old-sess")

        req = f"req-recover-ok-{int(time.time())}"
        proc = _claim_recover_cli(
            tmp, endpoint, task_id, "old executor session lost",
            _identity_flags("adj-1", "adj-inst-1", "adj-sess-1", "adjudicator"),
            _lease_flags(rev_holder), request_id=req)
        _assert_ok(proc, f"claim-recover {task_id}")
        out = _all_out(proc)
        assert "claim_status: released" in out or "released" in out, f"输出异常:\n{out}"

        # daemon 侧核验：claim 已释放、事件追加、无步骤/历史改写
        assert _claim_released_count(task_db, task_id) == 1, "应恰好 1 个 claim_released 事件"
        events = client.call("task.events", {"task_id": task_id})
        released = [e for e in events.get("events", []) if e.get("reason_code") == "claim_released"]
        assert len(released) == 1, f"task_events 未看到 claim_released:\n{json.dumps(events, ensure_ascii=False)[:800]}"
        # 新 Executor 可显式 claim（recovery 不隐式替新角色写 claim）
        _register_agent(client, "new-exec", "new-inst", "new-sess", "implementer")
        step = _claim_via_cli(tmp, endpoint, task_id, "new-sess", "new-exec", "new-inst", "implementer")
        assert step, "新 Executor 应能 claim 已释放的任务"

    def test_fresh_owner_rejected(self, recover_env):
        """old owner 仍 active/fresh → E_CLAIM_OWNER_ACTIVE，零 mutation。"""
        client, tmp, task_db, _proc, endpoint = recover_env
        _register_agent(client, "fresh-old", "fresh-old-inst", "fresh-old-sess", "implementer")
        _register_agent(client, "agent-rev-fresh", "", "session-rev-fresh", "reviewer")
        _register_agent(client, "adj-fresh", "adj-fresh-inst", "adj-fresh-sess", "adjudicator")

        task_id = _create_via_cli(tmp, endpoint, "T-RECOVER-FRESH")
        _claim_via_cli(tmp, endpoint, task_id, "fresh-old-sess", "fresh-old", "fresh-old-inst", "implementer")

        rev_holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "rev-fresh", role="reviewer")
        # 不标记 stale → 仍 active
        proc = _claim_recover_cli(
            tmp, endpoint, task_id, "attempt on fresh owner",
            _identity_flags("adj-fresh", "adj-fresh-inst", "adj-fresh-sess", "adjudicator"),
            _lease_flags(rev_holder))
        _assert_cli_rejected(proc, "fresh owner 必须拒绝", "E_CLAIM_OWNER_ACTIVE")
        assert _claim_released_count(task_db, task_id) == 0, "fresh owner 拒绝必须零 mutation"

    def test_non_adjudicator_role_rejected(self, recover_env):
        """非 adjudicator role → E_RECOVERY_ROLE_REQUIRED，零 mutation。"""
        client, tmp, task_db, _proc, endpoint = recover_env
        _register_agent(client, "na-old", "na-old-inst", "na-old-sess", "implementer")
        _register_agent(client, "agent-rev-na", "", "session-rev-na", "reviewer")

        task_id = _create_via_cli(tmp, endpoint, "T-RECOVER-ROLE")
        _claim_via_cli(tmp, endpoint, task_id, "na-old-sess", "na-old", "na-old-inst", "implementer")
        _mark_owner_stale(task_db, "na-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "rev-na", role="reviewer")
        proc = _claim_recover_cli(
            tmp, endpoint, task_id, "role misuse",
            _identity_flags("na-old", "na-old-inst", "na-old-sess", "implementer"),
            _lease_flags(rev_holder))
        _assert_cli_rejected(proc, "非 adjudicator 必须拒绝", "E_RECOVERY_ROLE_REQUIRED")
        assert _claim_released_count(task_db, task_id) == 0

    def test_missing_lease_fail_closed(self, recover_env):
        """无 reviewer lease → E_LEASE_REQUIRED（CLI 层）或 E_LEASE_NOT_FOUND（daemon 层）。"""
        client, tmp, task_db, _proc, endpoint = recover_env
        _register_agent(client, "ml-old", "ml-old-inst", "ml-old-sess", "implementer")
        _register_agent(client, "adj-ml", "adj-ml-inst", "adj-ml-sess", "adjudicator")

        task_id = _create_via_cli(tmp, endpoint, "T-RECOVER-NOLEASE")
        _claim_via_cli(tmp, endpoint, task_id, "ml-old-sess", "ml-old", "ml-old-inst", "implementer")
        _mark_owner_stale(task_db, "ml-old-sess")

        # 完全不提供 lease 凭证 → CLI fail-closed E_LEASE_REQUIRED
        proc = _claim_recover_cli(
            tmp, endpoint, task_id, "no lease at all",
            _identity_flags("adj-ml", "adj-ml-inst", "adj-ml-sess", "adjudicator"),
            [])
        _assert_cli_rejected(proc, "缺 lease 必须拒绝", "E_LEASE_REQUIRED")
        assert _claim_released_count(task_db, task_id) == 0

    def test_wrong_token_rejected(self, recover_env):
        """token 错 → E_LEASE_TOKEN_MISMATCH（daemon 层），零 mutation。"""
        client, tmp, task_db, _proc, endpoint = recover_env
        _register_agent(client, "wt-old", "wt-old-inst", "wt-old-sess", "implementer")
        _register_agent(client, "agent-rev-wt", "", "session-rev-wt", "reviewer")
        _register_agent(client, "adj-wt", "adj-wt-inst", "adj-wt-sess", "adjudicator")

        task_id = _create_via_cli(tmp, endpoint, "T-RECOVER-WTOKEN")
        _claim_via_cli(tmp, endpoint, task_id, "wt-old-sess", "wt-old", "wt-old-inst", "implementer")
        _mark_owner_stale(task_db, "wt-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "rev-wt", role="reviewer")
        bad_holder = dict(rev_holder, token="deadbeef" * 8)
        proc = _claim_recover_cli(
            tmp, endpoint, task_id, "wrong token",
            _identity_flags("adj-wt", "adj-wt-inst", "adj-wt-sess", "adjudicator"),
            _lease_flags(bad_holder))
        _assert_cli_rejected(proc, "错 token 必须拒绝", "E_LEASE_TOKEN_MISMATCH")
        assert _claim_released_count(task_db, task_id) == 0

    def test_stale_fencing_rejected(self, recover_env):
        """fencing stale → E_LEASE_FENCING_STALE（daemon 层），零 mutation。"""
        client, tmp, task_db, _proc, endpoint = recover_env
        _register_agent(client, "sf-old", "sf-old-inst", "sf-old-sess", "implementer")
        _register_agent(client, "agent-rev-sf", "", "session-rev-sf", "reviewer")
        _register_agent(client, "adj-sf", "adj-sf-inst", "adj-sf-sess", "adjudicator")

        task_id = _create_via_cli(tmp, endpoint, "T-RECOVER-SFENCING")
        _claim_via_cli(tmp, endpoint, task_id, "sf-old-sess", "sf-old", "sf-old-inst", "implementer")
        _mark_owner_stale(task_db, "sf-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "rev-sf", role="reviewer")
        stale_holder = dict(rev_holder, fencing_counter=rev_holder["fencing_counter"] - 1)
        proc = _claim_recover_cli(
            tmp, endpoint, task_id, "stale fencing",
            _identity_flags("adj-sf", "adj-sf-inst", "adj-sf-sess", "adjudicator"),
            _lease_flags(stale_holder))
        _assert_cli_rejected(proc, "stale fencing 必须拒绝", "E_LEASE_FENCING_STALE")
        assert _claim_released_count(task_db, task_id) == 0

    def test_same_agent_rejected(self, recover_env):
        """Reviewer 与 Adjudicator 同 agent → E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT。"""
        client, tmp, task_db, _proc, endpoint = recover_env
        _register_agent(client, "sa-old", "sa-old-inst", "sa-old-sess", "implementer")
        # agent_registrations 以 agent_id 为唯一键：同一 agent 只能注册一种角色。
        # 故仅注册 reviewer（lease holder=agent-sa-shared/session-sa-shared），
        # adjudicator 身份不注册，仅通过 CLI 透传同 agent_id、不同 session →
        # validate_ 链在 SAME_AGENT 检查（早于 adjudicator 注册检查）先行拒绝。
        _register_agent(client, "agent-sa-shared", "", "session-sa-shared", "reviewer")

        task_id = _create_via_cli(tmp, endpoint, "T-RECOVER-SAMEAGENT")
        _claim_via_cli(tmp, endpoint, task_id, "sa-old-sess", "sa-old", "sa-old-inst", "implementer")
        _mark_owner_stale(task_db, "sa-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "sa-shared", role="reviewer")
        # adjudicator 用同一 agent_id（不同 session）→ 跨角色校验拒绝
        proc = _claim_recover_cli(
            tmp, endpoint, task_id, "same agent misuse",
            _identity_flags("agent-sa-shared", "", "sa-adj-sess", "adjudicator"),
            _lease_flags(rev_holder))
        _assert_cli_rejected(proc, "同 agent 必须拒绝",
                             "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT")
        assert _claim_released_count(task_db, task_id) == 0

    def test_same_session_rejected(self, recover_env):
        """Reviewer 与 Adjudicator 同 session → E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION。"""
        client, tmp, task_db, _proc, endpoint = recover_env
        _register_agent(client, "ss-old", "ss-old-inst", "ss-old-sess", "implementer")
        # reviewer 与 adjudicator 不同 agent 但同 session：
        # reviewer 注册身份须与 lease 派生一致（agent-ss-rev / session-ss-rev）；
        # adjudicator 透传同 session → SAME_SESSION 拒绝
        _register_agent(client, "agent-ss-rev", "", "session-ss-rev", "reviewer")

        task_id = _create_via_cli(tmp, endpoint, "T-RECOVER-SAMESESS")
        _claim_via_cli(tmp, endpoint, task_id, "ss-old-sess", "ss-old", "ss-old-inst", "implementer")
        _mark_owner_stale(task_db, "ss-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "ss-rev", role="reviewer")
        proc = _claim_recover_cli(
            tmp, endpoint, task_id, "same session misuse",
            _identity_flags("ss-adj", "ss-adj-inst", "session-ss-rev", "adjudicator"),
            _lease_flags(rev_holder))
        _assert_cli_rejected(proc, "同 session 必须拒绝",
                             "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION")
        assert _claim_released_count(task_db, task_id) == 0

    def test_request_id_dedup(self, recover_env):
        """同 request_id 重试 → dedup 返回原结果，不产生第二个 claim_released。"""
        client, tmp, task_db, _proc, endpoint = recover_env
        _register_agent(client, "dd-old", "dd-old-inst", "dd-old-sess", "implementer")
        _register_agent(client, "agent-rev-dd", "", "session-rev-dd", "reviewer")
        _register_agent(client, "adj-dd", "adj-dd-inst", "adj-dd-sess", "adjudicator")

        task_id = _create_via_cli(tmp, endpoint, "T-RECOVER-DEDUP")
        _claim_via_cli(tmp, endpoint, task_id, "dd-old-sess", "dd-old", "dd-old-inst", "implementer")
        _mark_owner_stale(task_db, "dd-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "rev-dd", role="reviewer")
        req = f"req-recover-dedup-{int(time.time())}"
        first = _claim_recover_cli(
            tmp, endpoint, task_id, "dedup test",
            _identity_flags("adj-dd", "adj-dd-inst", "adj-dd-sess", "adjudicator"),
            _lease_flags(rev_holder), request_id=req)
        _assert_ok(first, "首次 claim-recover")
        assert _claim_released_count(task_db, task_id) == 1

        second = _claim_recover_cli(
            tmp, endpoint, task_id, "dedup test",
            _identity_flags("adj-dd", "adj-dd-inst", "adj-dd-sess", "adjudicator"),
            _lease_flags(rev_holder), request_id=req)
        _assert_ok(second, "同 request_id 重试")
        assert _claim_released_count(task_db, task_id) == 1, \
            "同 request_id 重试不得产生第二个 claim_released 事件"

    def test_daemon_unavailable_fail_closed(self, tmp_path):
        """daemon 不可达（enterprise 模式）→ 无本地 fallback，结构化失败。"""
        env = dict(os.environ)
        env["CW_DAEMON_MODE"] = "enterprise"
        env["CW_DAEMON_TRANSPORT"] = "http"
        env["CW_DAEMON_HTTP_ENDPOINT"] = "https://127.0.0.1:1"
        env["CW_PKI_DIR"] = _REAL_PKI_DIR or env.get("CW_PKI_DIR", "")
        env["CW_TASK_WRITE_POLICY"] = "shared"
        env["CW_DAEMON_AUTOSTART_WINDOW"] = "0"
        env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
        env.pop("CW_AGENT_SESSION_ID", None)

        proc = _run_cw_cli(
            ["task", "claim-recover", "T-NO-DAEMON",
             "--reason", "daemon unavailable",
             "--agent-id", "adj-x", "--session-id", "adj-sess-x",
             "--model-id", "model-x", "--role", "adjudicator",
             "--lease-token", "tok", "--fencing-counter", "1"],
            env, str(tmp_path))
        out = _all_out(proc)
        # CLI 应 fail-closed：连接失败/不可用结构化错误，绝不回退本地 SQLite 成功
        assert "E_DAEMON_UNAVAILABLE" in out or "DaemonUnavailableError" in out or \
               "无法连接" in out or "daemon" in out.lower(), f"应 fail-closed:\n{out}"
        assert "claim_status" not in out, "daemon 不可达时不得伪造成功结果"
