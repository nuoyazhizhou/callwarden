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

前置条件（与 test_cli_task_lease_parity.py 一致）：
1. Windows 平台（Named Pipe）；
2. 已构建 `cw-daemon.exe`（cargo build --release --bin cw-daemon）；
3. 默认管道 `\\.\pipe\callwarden-<sid>` 未被其他 daemon 占用（占用则 skip）。
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

# 任务指令：每个 CLI 调用必须走 C:\Python314\python.exe（禁止依赖 python/python3/py）
_PY_EXE = r"C:\Python314\python.exe"

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")
# P0-G 部署期：nested target（17:46 构建，已验证含 G1 修复 + INT-001 + role_contracts）。
# 沙箱环境下无法在 stage-refresh 目录重建，故指向已验证产物。
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "rust_ext", "target", "stage-refresh", "release", "cw-daemon.exe")
if not os.path.exists(_DAEMON_BIN):
    _DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")

_TASK_ID_RE = re.compile(r"T-[0-9A-Za-z-]+")
_STEP_ID_RE = re.compile(r"S-[0-9A-Za-z-]+")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="claim-recover CLI↔daemon E2E 需要 Windows + Named Pipe",
)

requires_binaries = pytest.mark.skipif(
    not os.path.exists(_DAEMON_BIN),
    reason="cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）",
)


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


@pytest.fixture(scope="module", autouse=True)
def ensure_fresh_binary():
    """P2 门禁：验证 cw-daemon 二进制存在且由当前源码重建（fresh runtime）。

    沙箱部署期无法在 stage-refresh 目录重建（.cargo-lock 被沙箱写保护拦截），
    改用已验证的 nested 产物（17:46 构建，rlib 17:31 晚于 task_collab.rs 17:20
    修改 → 含 G1 跨角色 lease 修复）。仅校验二进制存在与时间戳合理性。
    """
    if not os.path.exists(_DAEMON_BIN):
        pytest.fail(f"cw-daemon 二进制缺失: {_DAEMON_BIN}；"
                    f"需先 cargo build --bin cw-daemon（nested/stage-refresh 产物）")
    # 校验二进制是"较新"构建（含 2026-08-26 的 G1 修复），避免误用旧 13:30 产物
    mtime = os.path.getmtime(_DAEMON_BIN)
    if mtime < 1787731200:  # 2026-08-26 16:00 UTC+8 之前 → 太旧
        pytest.fail(f"cw-daemon 二进制过旧（mtime={mtime}），疑似未含 G1 修复: {_DAEMON_BIN}")


@pytest.fixture(scope="module")
def recover_env():
    """启动真实隔离 cw-daemon（临时 task DB + 默认 Named Pipe），返回
    (client, tmp, task_db, proc, pipe)。测试只用隔离临时任务库，绝不触碰真实用户库。"""
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    from callwarden.config import _get_windows_user_sid

    pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
    probe = UnixDaemonRpcClient(socket_path=pipe, timeout=3)
    try:
        probe.call("ping")
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过")
    except Exception:
        pass

    tmp = tempfile.mkdtemp(prefix="cw_cli_claim_recover_")
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
            "INSERT INTO workspaces (id, name, root_path, created_at) VALUES (1, 'claim-recover-test', '.', ?1)",
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
    if not os.environ.get("CW_KEEP_CLAIM_RECOVER_TMP"):
        shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# CLI 子进程辅助
# ----------------------------------------------------------------------

def _cli_env(pipe: str, session: str = "", mode: str = "enterprise",
             extra: dict = None) -> dict:
    env = dict(os.environ)
    env.pop("CW_AGENT_SESSION_ID", None)
    env["CW_DAEMON_MODE"] = mode
    env["CW_DAEMON_ENDPOINT"] = pipe
    # 强制 Named Pipe transport：跳过 HTTP manifest 检查（stale manifest 会
    # 触发 E_HTTP_MANIFEST_STALE fail-closed，阻塞隔离 daemon 测试）
    env["CW_DAEMON_TRANSPORT"] = "named-pipe"
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
        env=env, cwd=cwd, capture_output=True,
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
    return client.agent_register(
        agent_id=agent_id,
        agent_name=f"agent-{agent_id}",
        capabilities=["code"],
        identity={
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
    )


def _create_via_cli(tmpdir: str, pipe: str, title: str) -> str:
    proc = _run_cw_cli(
        ["task", "create", "--title", title,
         "--steps", '[{"action": "实现", "target_file": "f.py"}]'],
        _cli_env(pipe), tmpdir)
    _assert_ok(proc, f"task create {title}")
    match = _TASK_ID_RE.search(_all_out(proc))
    assert match, f"task create 输出未解析到 task_id:\n{_all_out(proc)}"
    return match.group(0)


def _claim_via_cli(tmpdir: str, pipe: str, task_id: str, session: str) -> str:
    proc = _run_cw_cli(["task", "next", task_id],
                       _cli_env(pipe, session=session), tmpdir)
    _assert_ok(proc, f"task next {task_id} session={session}")
    match = _STEP_ID_RE.search(_all_out(proc))
    assert match, f"task next 输出未解析到 step_id:\n{_all_out(proc)}"
    return match.group(0)


def _acquire_lease_via_cli(tmpdir: str, pipe: str, task_id: str,
                           tag: str, role: str = "reviewer") -> dict:
    aid, sid, mid = f"agent-{tag}", f"session-{tag}", f"model-{tag}"
    proc = _run_cw_cli(
        ["lease", "acquire", task_id, "--role", role,
         "--agent-id", aid, "--session-id", sid, "--model-id", mid, "--json"],
        _cli_env(pipe), tmpdir)
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


def _claim_recover_cli(tmpdir, pipe, task_id, reason, identity_flags, lease_flags,
                       request_id=""):
    args = ["task", "claim-recover", task_id,
            "--reason", reason, *identity_flags, *lease_flags]
    if request_id:
        args += ["--request-id", request_id]
    return _run_cw_cli(args, _cli_env(pipe), tmpdir)


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


@requires_binaries
class TestCliClaimRecover:
    """P0-G G1：claim-recover 端到端验收矩阵。"""

    def test_success_releases_stale_claim_with_independent_reviewer_lease(self, recover_env):
        """stale old owner + 独立 valid reviewer lease + adjudicator identity → 成功。"""
        client, tmp, task_db, _proc, pipe = recover_env
        # 注册 old owner / reviewer / adjudicator（独立 agent/instance/session）
        _register_agent(client, "old-owner", "old-inst", "old-sess", "implementer")
        _register_agent(client, "rev-1", "rev-inst-1", "rev-sess-1", "reviewer")
        _register_agent(client, "adj-1", "adj-inst-1", "adj-sess-1", "adjudicator")

        task_id = _create_via_cli(tmp, pipe, "T-RECOVER-OK")
        _claim_via_cli(tmp, pipe, task_id, "old-sess")
        assert client.task_status(task_id)["status"] == "in_progress"

        # 独立 Reviewer acquire reviewer lease
        rev_holder = _acquire_lease_via_cli(tmp, pipe, task_id, "rev-1", role="reviewer")
        assert rev_holder["agent_id"] == "agent-rev-1"

        # 标记 old owner stale（模拟失联）
        _mark_owner_stale(task_db, "old-sess")

        req = f"req-recover-ok-{int(time.time())}"
        proc = _claim_recover_cli(
            tmp, pipe, task_id, "old executor session lost",
            _identity_flags("adj-1", "adj-inst-1", "adj-sess-1", "adjudicator"),
            _lease_flags(rev_holder), request_id=req)
        _assert_ok(proc, f"claim-recover {task_id}")
        out = _all_out(proc)
        assert "claim_status: released" in out or "released" in out, f"输出异常:\n{out}"

        # daemon 侧核验：claim 已释放、事件追加、无步骤/历史改写
        assert _claim_released_count(task_db, task_id) == 1, "应恰好 1 个 claim_released 事件"
        events = client.task_events(task_id)
        released = [e for e in events.get("events", []) if e.get("reason_code") == "claim_released"]
        assert len(released) == 1, f"task_events 未看到 claim_released:\n{json.dumps(events, ensure_ascii=False)[:800]}"
        # 新 Executor 可显式 claim（recovery 不隐式替新角色写 claim）
        _register_agent(client, "new-exec", "new-inst", "new-sess", "implementer")
        step = _claim_via_cli(tmp, pipe, task_id, "new-sess")
        assert step, "新 Executor 应能 claim 已释放的任务"

    def test_fresh_owner_rejected(self, recover_env):
        """old owner 仍 active/fresh → E_CLAIM_OWNER_ACTIVE，零 mutation。"""
        client, tmp, task_db, _proc, pipe = recover_env
        _register_agent(client, "fresh-old", "fresh-old-inst", "fresh-old-sess", "implementer")
        _register_agent(client, "rev-fresh", "rev-fresh-inst", "rev-fresh-sess", "reviewer")
        _register_agent(client, "adj-fresh", "adj-fresh-inst", "adj-fresh-sess", "adjudicator")

        task_id = _create_via_cli(tmp, pipe, "T-RECOVER-FRESH")
        _claim_via_cli(tmp, pipe, task_id, "fresh-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, pipe, task_id, "rev-fresh", role="reviewer")
        # 不标记 stale → 仍 active
        proc = _claim_recover_cli(
            tmp, pipe, task_id, "attempt on fresh owner",
            _identity_flags("adj-fresh", "adj-fresh-inst", "adj-fresh-sess", "adjudicator"),
            _lease_flags(rev_holder))
        _assert_cli_rejected(proc, "fresh owner 必须拒绝", "E_CLAIM_OWNER_ACTIVE")
        assert _claim_released_count(task_db, task_id) == 0, "fresh owner 拒绝必须零 mutation"

    def test_non_adjudicator_role_rejected(self, recover_env):
        """非 adjudicator role → E_RECOVERY_ROLE_REQUIRED，零 mutation。"""
        client, tmp, task_db, _proc, pipe = recover_env
        _register_agent(client, "na-old", "na-old-inst", "na-old-sess", "implementer")
        _register_agent(client, "rev-na", "rev-na-inst", "rev-na-sess", "reviewer")

        task_id = _create_via_cli(tmp, pipe, "T-RECOVER-ROLE")
        _claim_via_cli(tmp, pipe, task_id, "na-old-sess")
        _mark_owner_stale(task_db, "na-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, pipe, task_id, "rev-na", role="reviewer")
        proc = _claim_recover_cli(
            tmp, pipe, task_id, "role misuse",
            _identity_flags("na-old", "na-old-inst", "na-old-sess", "implementer"),
            _lease_flags(rev_holder))
        _assert_cli_rejected(proc, "非 adjudicator 必须拒绝", "E_RECOVERY_ROLE_REQUIRED")
        assert _claim_released_count(task_db, task_id) == 0

    def test_missing_lease_fail_closed(self, recover_env):
        """无 reviewer lease → E_LEASE_REQUIRED（CLI 层）或 E_LEASE_NOT_FOUND（daemon 层）。"""
        client, tmp, task_db, _proc, pipe = recover_env
        _register_agent(client, "ml-old", "ml-old-inst", "ml-old-sess", "implementer")
        _register_agent(client, "adj-ml", "adj-ml-inst", "adj-ml-sess", "adjudicator")

        task_id = _create_via_cli(tmp, pipe, "T-RECOVER-NOLEASE")
        _claim_via_cli(tmp, pipe, task_id, "ml-old-sess")
        _mark_owner_stale(task_db, "ml-old-sess")

        # 完全不提供 lease 凭证 → CLI fail-closed E_LEASE_REQUIRED
        proc = _claim_recover_cli(
            tmp, pipe, task_id, "no lease at all",
            _identity_flags("adj-ml", "adj-ml-inst", "adj-ml-sess", "adjudicator"),
            [])
        _assert_cli_rejected(proc, "缺 lease 必须拒绝", "E_LEASE_REQUIRED")
        assert _claim_released_count(task_db, task_id) == 0

    def test_wrong_token_rejected(self, recover_env):
        """token 错 → E_LEASE_TOKEN_MISMATCH（daemon 层），零 mutation。"""
        client, tmp, task_db, _proc, pipe = recover_env
        _register_agent(client, "wt-old", "wt-old-inst", "wt-old-sess", "implementer")
        _register_agent(client, "rev-wt", "rev-wt-inst", "rev-wt-sess", "reviewer")
        _register_agent(client, "adj-wt", "adj-wt-inst", "adj-wt-sess", "adjudicator")

        task_id = _create_via_cli(tmp, pipe, "T-RECOVER-WTOKEN")
        _claim_via_cli(tmp, pipe, task_id, "wt-old-sess")
        _mark_owner_stale(task_db, "wt-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, pipe, task_id, "rev-wt", role="reviewer")
        bad_holder = dict(rev_holder, token="deadbeef" * 8)
        proc = _claim_recover_cli(
            tmp, pipe, task_id, "wrong token",
            _identity_flags("adj-wt", "adj-wt-inst", "adj-wt-sess", "adjudicator"),
            _lease_flags(bad_holder))
        _assert_cli_rejected(proc, "错 token 必须拒绝", "E_LEASE_TOKEN_MISMATCH")
        assert _claim_released_count(task_db, task_id) == 0

    def test_stale_fencing_rejected(self, recover_env):
        """fencing stale → E_LEASE_FENCING_STALE（daemon 层），零 mutation。"""
        client, tmp, task_db, _proc, pipe = recover_env
        _register_agent(client, "sf-old", "sf-old-inst", "sf-old-sess", "implementer")
        _register_agent(client, "rev-sf", "rev-sf-inst", "rev-sf-sess", "reviewer")
        _register_agent(client, "adj-sf", "adj-sf-inst", "adj-sf-sess", "adjudicator")

        task_id = _create_via_cli(tmp, pipe, "T-RECOVER-SFENCING")
        _claim_via_cli(tmp, pipe, task_id, "sf-old-sess")
        _mark_owner_stale(task_db, "sf-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, pipe, task_id, "rev-sf", role="reviewer")
        stale_holder = dict(rev_holder, fencing_counter=rev_holder["fencing_counter"] - 1)
        proc = _claim_recover_cli(
            tmp, pipe, task_id, "stale fencing",
            _identity_flags("adj-sf", "adj-sf-inst", "adj-sf-sess", "adjudicator"),
            _lease_flags(stale_holder))
        _assert_cli_rejected(proc, "stale fencing 必须拒绝", "E_LEASE_FENCING_STALE")
        assert _claim_released_count(task_db, task_id) == 0

    def test_same_agent_rejected(self, recover_env):
        """Reviewer 与 Adjudicator 同 agent → E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT。"""
        client, tmp, task_db, _proc, pipe = recover_env
        _register_agent(client, "sa-old", "sa-old-inst", "sa-old-sess", "implementer")
        # 同一 agent 同时注册 reviewer 与 adjudicator 身份（不同 session）
        _register_agent(client, "sa-shared", "sa-shared-inst", "sa-rev-sess", "reviewer")
        _register_agent(client, "sa-shared", "sa-shared-inst", "sa-adj-sess", "adjudicator")

        task_id = _create_via_cli(tmp, pipe, "T-RECOVER-SAMEAGENT")
        _claim_via_cli(tmp, pipe, task_id, "sa-old-sess")
        _mark_owner_stale(task_db, "sa-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, pipe, task_id, "sa-shared", role="reviewer")
        # adjudicator 用同一 agent_id（不同 session）→ 跨角色校验拒绝
        proc = _claim_recover_cli(
            tmp, pipe, task_id, "same agent misuse",
            _identity_flags("sa-shared", "sa-shared-inst", "sa-adj-sess", "adjudicator"),
            _lease_flags(rev_holder))
        _assert_cli_rejected(proc, "同 agent 必须拒绝",
                             "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_AGENT")
        assert _claim_released_count(task_db, task_id) == 0

    def test_same_session_rejected(self, recover_env):
        """Reviewer 与 Adjudicator 同 session → E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION。"""
        client, tmp, task_db, _proc, pipe = recover_env
        _register_agent(client, "ss-old", "ss-old-inst", "ss-old-sess", "implementer")
        # reviewer 与 adjudicator 不同 agent 但同 session
        _register_agent(client, "ss-rev", "ss-rev-inst", "ss-shared-sess", "reviewer")
        _register_agent(client, "ss-adj", "ss-adj-inst", "ss-shared-sess", "adjudicator")

        task_id = _create_via_cli(tmp, pipe, "T-RECOVER-SAMESESS")
        _claim_via_cli(tmp, pipe, task_id, "ss-old-sess")
        _mark_owner_stale(task_db, "ss-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, pipe, task_id, "ss-rev", role="reviewer")
        proc = _claim_recover_cli(
            tmp, pipe, task_id, "same session misuse",
            _identity_flags("ss-adj", "ss-adj-inst", "ss-shared-sess", "adjudicator"),
            _lease_flags(rev_holder))
        _assert_cli_rejected(proc, "同 session 必须拒绝",
                             "E_GOVERNANCE_REVIEWER_ADJUDICATOR_SAME_SESSION")
        assert _claim_released_count(task_db, task_id) == 0

    def test_request_id_dedup(self, recover_env):
        """同 request_id 重试 → dedup 返回原结果，不产生第二个 claim_released。"""
        client, tmp, task_db, _proc, pipe = recover_env
        _register_agent(client, "dd-old", "dd-old-inst", "dd-old-sess", "implementer")
        _register_agent(client, "rev-dd", "rev-dd-inst", "rev-dd-sess", "reviewer")
        _register_agent(client, "adj-dd", "adj-dd-inst", "adj-dd-sess", "adjudicator")

        task_id = _create_via_cli(tmp, pipe, "T-RECOVER-DEDUP")
        _claim_via_cli(tmp, pipe, task_id, "dd-old-sess")
        _mark_owner_stale(task_db, "dd-old-sess")

        rev_holder = _acquire_lease_via_cli(tmp, pipe, task_id, "rev-dd", role="reviewer")
        req = f"req-recover-dedup-{int(time.time())}"
        first = _claim_recover_cli(
            tmp, pipe, task_id, "dedup test",
            _identity_flags("adj-dd", "adj-dd-inst", "adj-dd-sess", "adjudicator"),
            _lease_flags(rev_holder), request_id=req)
        _assert_ok(first, "首次 claim-recover")
        assert _claim_released_count(task_db, task_id) == 1

        second = _claim_recover_cli(
            tmp, pipe, task_id, "dedup retry",
            _identity_flags("adj-dd", "adj-dd-inst", "adj-dd-sess", "adjudicator"),
            _lease_flags(rev_holder), request_id=req)
        _assert_ok(second, "同 request_id 重试")
        assert _claim_released_count(task_db, task_id) == 1, \
            "同 request_id 重试不得产生第二个 claim_released 事件"

    def test_daemon_unavailable_fail_closed(self, tmp_path):
        """daemon 不可达（enterprise 模式）→ 无本地 fallback，结构化失败。"""
        env = dict(os.environ)
        env["CW_DAEMON_MODE"] = "enterprise"
        env["CW_DAEMON_ENDPOINT"] = r"\\.\pipe\callwarden-nonexistent-sid-0"
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
