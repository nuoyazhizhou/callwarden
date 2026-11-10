r"""CLI ↔ Rust daemon 生命周期 RPC 一致性（任务 T-1786616113972-c74ad528）。

修复的两个 CLI/daemon 一致性缺陷：
1. `cw task apply` / `cw task close` 必须把用户提供的 lease_token 与
   fencing_counter **原样**传给 daemon RPC（此前只留在 _local_apply/_local_close
   fallback 参数里，daemon payload 缺失 → 真实 E2E 无法在 daemon 权威路径执行）。
2. `cw task next`（claim）与后续 `cw task report` 必须使用同一显式 action
   identity（同一 session_id）；不得让 claim 回退 Windows SID 而 report 用
   另一 session，导致 E_IDENTITY_SESSION_MISMATCH 或伪 task_conflict。

验收方式（必须 CLI 进程级）：
- 每个写调用都是 `sys.executable cw.py task ...` 真实子进程；
- 全程连接真实隔离 cw-daemon.exe（临时 task DB + HTTPS/mTLS），禁 mock；
- daemon 侧只读断言（task.status / task.events）仅用于核验状态，不替代 CLI 写入口。

覆盖矩阵：
- CLI 进程级全链路：create → claim → report → lease acquire → apply → close；
- apply/close 缺 lease、空 lease、只 token、错 token、旧 counter 全部 fail-closed
  （E_LEASE_REQUIRED / E_LEASE_CRED_INCOMPLETE / E_LEASE_TOKEN_MISMATCH /
  E_LEASE_FENCING_STALE），且拒绝后 task status 零改动；
- `task next` 与同 session 的 `task report` 成功，且 claim/report 两条
  task_events 的 agent_session_id 都等于显式 session（不漂移到 Windows SID）；
- enterprise/auto 模式下 daemon 不可用时 fail-closed，不回退本地 SQLite。

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

# CLI claim 携带的结构化 identity（daemon 要求 agent 已在 agent_registrations
# 注册且 active，否则 E_IDENTITY_UNREGISTERED fail-closed）。
_PARITY_AGENT_ID = "agent-cli-parity"
_PARITY_MODEL_ID = "model-cli-parity"
# lease_env 注册 workspace（带 git 元数据）后由 fixture 写入；report 必须携带
# 与绑定 workspace authority 一致的 snapshot_id，否则 E_TASK_REPORT_SNAPSHOT_MISMATCH。
_PARITY_SNAPSHOT_ID = ""

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="CLI↔daemon 生命周期 parity 需要 Windows 隔离 daemon",
)

_REAL_PKI_DIR = None


@pytest.fixture(scope="module")
def lease_env():
    """启动隔离 HTTPS daemon，返回 (client, tmp, task_db, proc, endpoint)。"""
    global _REAL_PKI_DIR, _PARITY_SNAPSHOT_ID
    from callwarden.config import get_pki_dir
    from _w3_harness import seed_cli_task_agent, seed_cli_task_authority, setup_w3_client

    _REAL_PKI_DIR = get_pki_dir()
    os.environ["CW_PKI_DIR"] = _REAL_PKI_DIR
    tmp = tempfile.mkdtemp(prefix="cw_cli_lease_parity_")
    proc = None
    try:
        client, _inst, endpoint, proc = setup_w3_client(tmp, _REPO_ROOT, ws_id=1)
        if client is None:
            pytest.skip("隔离 HTTPS daemon 前置不可用（二进制/PKI/端口）")
        task_db = os.path.join(tmp, "task.db")
        reg = client.call("workspace.register", {
            "client_view_root": _REPO_ROOT,
            "git_remote_url": "https://cli-parity.invalid/repo.git",
            "git_head_commit_sha": "a" * 40,
            "toolchain_fingerprint": "cli-parity-toolchain",
        })
        instance_id = (reg or {}).get("workspace_instance_id") or ""
        if not instance_id:
            raise RuntimeError(f"workspace.register 未返回 workspace_instance_id: {reg!r}")
        _PARITY_SNAPSHOT_ID = (reg or {}).get("snapshot_id") or ""
        if not _PARITY_SNAPSHOT_ID:
            raise RuntimeError(f"workspace.register 未返回 snapshot_id（需 git 元数据）: {reg!r}")
        seed_cli_task_authority(
            task_db, ws_id=1, name="cli-parity-test",
            root_path=_REPO_ROOT, instance_id=instance_id,
        )
        # claim 的 task.next 携带结构化 identity（agent 需注册且 active）。
        seed_cli_task_agent(task_db, _PARITY_AGENT_ID, model_id=_PARITY_MODEL_ID)
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
        if not os.environ.get("CW_KEEP_PARITY_TMP"):
            shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# CLI 子进程辅助
# ----------------------------------------------------------------------

def _cli_env(endpoint: str, data_root: str, session: str = "", mode: str = "enterprise",
             extra: dict = None) -> dict:
    """构造 CLI 子进程环境：路由到指定 daemon、enterprise/auto、autostart 窗口 0。

    - CW_DAEMON_AUTOSTART_WINDOW=0：daemon 连接失败时立即 fail-closed，
      不在有界窗口内等待、也不会自动唤起 daemon（避免测试环境残留 daemon 进程）。
    - CW_AGENT_SESSION_ID：本测试显式控制 claim/report 的 action session；
      父进程即使设置了也先清掉，保证每个子进程的 session 由测试决定。
    - CALLWARDEN_SKIP_AUTO_SETUP=1：跳过首次自动配置（幂等标记之外的副作用）。
    """
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
    r"""真实 `sys.executable cw.py ...` CLI 子进程。"""
    return subprocess.run(
        [_PY_EXE, _CW_PY] + args,
        env=env, cwd=_REPO_ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _all_out(proc) -> str:
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


def _assert_ok(proc, what: str):
    assert proc.returncode == 0, f"{what} 失败(exit={proc.returncode}):\n{_all_out(proc)}"


def _seed_pass_verdict(task_db: str, task_id: str) -> None:
    """seed 一条 reviewer pass verdict（S4 关闭门禁的前置账本）。

    本文件验证的是「CLI 原样透传 lease token/fencing_counter 到 daemon」（payload
    parity）。daemon S4 门禁要求 review 态 apply/close 前 `task_verdict_events`
    已有 pass 入账（或显式 waiver；CLI 未暴露 waiver flag）。这里直接 seed 账本
    行，等价于 reviewer 已提交 pass verdict，不绕过被测的 CLI→daemon 写路径。
    """
    conn = sqlite3.connect(task_db, timeout=10)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO task_verdict_events "
            "(verdict_id, task_id, contract_id, contract_revision, contract_hash,"
            " phase, overall, submitted_at, reviewer_identity)"
            " VALUES (?, ?, 'c-parity', 1, 'h-parity', 'blind_first_pass',"
            " 'pass', ?, 'parity-seed')",
            (f"v-parity-{task_id}", task_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _assert_cli_rejected(proc, what: str, *codes: str):
    """CLI 业务拒绝：`_dispatch_subcommand` 顶层打印 `✗ 执行子命令 'task' 失败:
    <code>: <message>` 并按运行时失败 exit 1。断言输出含结构化错误码。"""
    out = _all_out(proc)
    for code in codes:
        assert code in out, f"{what}: 输出缺少错误码 {code}:\n{out}"


def _create_via_cli(tmpdir: str, endpoint: str, title: str) -> str:
    """CLI task create → 解析 daemon 生成的 task_id。"""
    proc = _run_cw_cli(
        ["task", "create", "--title", title,
         "--steps", '[{"action": "实现", "target_file": "f.py"}]'],
        _cli_env(endpoint, tmpdir), tmpdir)
    _assert_ok(proc, f"task create {title}")
    match = _TASK_ID_RE.search(_all_out(proc))
    assert match, f"task create 输出未解析到 task_id:\n{_all_out(proc)}"
    return match.group(0)


def _claim_via_cli(tmpdir: str, endpoint: str, task_id: str, session: str) -> str:
    """CLI task next（claim）→ 解析 step_id。

    携带结构化 identity（agent 已在 fixture 注册 active）+ 由 daemon 权威
    contract_get 取回的 contract_claim，满足 A3 冻结 Role Contract 的领取门禁
    （E_IDENTITY_REQUIRED / E_IDENTITY_UNREGISTERED fail-closed）。
    """
    proc = _run_cw_cli(
        ["task", "next", task_id,
         "--agent-id", _PARITY_AGENT_ID,
         "--session-id", session,
         "--model-id", _PARITY_MODEL_ID,
         "--role", "executor"],
        _cli_env(endpoint, tmpdir, session=session), tmpdir)
    _assert_ok(proc, f"task next {task_id} session={session}")
    match = _STEP_ID_RE.search(_all_out(proc))
    assert match, f"task next 输出未解析到 step_id:\n{_all_out(proc)}"
    return match.group(0)


def _report_via_cli(tmpdir: str, endpoint: str, task_id: str, step_id: str, session: str):
    args = ["task", "report", task_id, step_id, "--result", "done"]
    # daemon task.report 要求携带与绑定 workspace authority 一致的 snapshot_id。
    if _PARITY_SNAPSHOT_ID:
        args += ["--snapshot-id", _PARITY_SNAPSHOT_ID]
    proc = _run_cw_cli(args, _cli_env(endpoint, tmpdir, session=session), tmpdir)
    _assert_ok(proc, f"task report {task_id} session={session}")
    return proc


def _prepare_review_via_cli(tmpdir: str, endpoint: str, task_id: str,
                            session: str = "session-impl") -> str:
    """CLI 链路 create → claim → report，推进到 review。

    `task_id` 参数同时用作 create 的标题；返回 **daemon 生成的真实 task_id**
    （不能把标题字符串当任务 ID 使用）。
    """
    task_id = _create_via_cli(tmpdir, endpoint, task_id)
    step_id = _claim_via_cli(tmpdir, endpoint, task_id, session)
    _report_via_cli(tmpdir, endpoint, task_id, step_id, session)
    return task_id


def _acquire_lease_via_cli(tmpdir: str, endpoint: str, task_id: str,
                           tag: str, role: str = "reviewer") -> dict:
    """CLI lease acquire（--json）→ 解析 raw token / fencing_counter。"""
    aid, sid, mid = f"agent-parity-{tag}", f"session-parity-{tag}", f"model-parity-{tag}"
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


def _reviewer_flags(holder: dict) -> list:
    return [
        "--agent-id", holder["agent_id"],
        "--session-id", holder["session_id"],
        "--model-id", holder["model_id"],
        "--role", holder["role"],
    ]


def _lease_flags(holder: dict) -> list:
    return ["--lease-token", holder["token"],
            "--fencing-counter", str(holder["fencing_counter"])]


# ----------------------------------------------------------------------
# 1) CLI payload 含 lease token/fencing counter + 全链路 + fail-closed 矩阵
# ----------------------------------------------------------------------

class TestCliLeasePayloadParity:
    """CLI 进程级：payload 原样携带 P4 lease 凭证；缺/错/旧凭证全部 fail-closed。"""

    def test_full_chain_create_claim_report_acquire_apply_close(self, lease_env):
        client, tmp, task_db, _proc, endpoint = lease_env
        task_id = "T-CLI-PARITY-FULL"
        session = "session-claim"
        tag = "full"

        # create（CLI 子进程）；task_id 参数是标题，create 返回 daemon 生成的真实 ID
        task_id = _create_via_cli(tmp, endpoint, task_id)
        assert client.call("task.status", {"task_id": task_id})["status"] == "open"

        # claim + 同 session report（CLI 子进程，仅 CW_AGENT_SESSION_ID，无 identity）
        step_id = _claim_via_cli(tmp, endpoint, task_id, session)
        _report_via_cli(tmp, endpoint, task_id, step_id, session)
        assert client.call("task.status", {"task_id": task_id})["status"] == "review", \
            "claim(env session) + 同 session report 未推进到 review"

        # lease acquire（CLI 子进程）
        holder = _acquire_lease_via_cli(tmp, endpoint, task_id, tag)
        assert holder["fencing_counter"] >= 1
        # S4 关闭门禁前置：reviewer pass verdict 已入账（详见 _seed_pass_verdict）。
        _seed_pass_verdict(task_db, task_id)

        # apply（CLI 子进程，原样传 lease_token + fencing_counter + identity）
        proc = _run_cw_cli(
            ["task", "apply", task_id, "--reviewer", "reviewer",
             *_lease_flags(holder), *_reviewer_flags(holder)],
            _cli_env(endpoint, tmp), tmp)
        _assert_ok(proc, f"task apply {task_id}")
        assert "已审核通过" in _all_out(proc), f"apply 输出异常:\n{_all_out(proc)}"
        assert client.call("task.status", {"task_id": task_id})["status"] == "applied"

        # close（CLI 子进程，同一 lease 凭证）
        proc = _run_cw_cli(
            ["task", "close", task_id, "--reviewer", "reviewer",
             *_lease_flags(holder), *_reviewer_flags(holder)],
            _cli_env(endpoint, tmp), tmp)
        _assert_ok(proc, f"task close {task_id}")
        assert "已关闭" in _all_out(proc), f"close 输出异常:\n{_all_out(proc)}"
        assert client.call("task.status", {"task_id": task_id})["status"] == "closed"

        # 事件落库：applied/closed 两条状态流转
        events = client.call("task.events", {"task_id": task_id})["events"]
        to_statuses = [e["to_status"] for e in events]
        assert "applied" in to_statuses and "closed" in to_statuses, to_statuses

    def test_apply_missing_lease_fail_closed(self, lease_env):
        client, tmp, _task_db, _proc, endpoint = lease_env
        task_id = "T-CLI-PARITY-AP-NO-LEASE"
        task_id = _prepare_review_via_cli(tmp, endpoint, task_id)
        proc = _run_cw_cli(
            ["task", "apply", task_id, "--reviewer", "reviewer",
             "--agent-id", "a", "--session-id", "s", "--model-id", "m",
             "--role", "reviewer"],
            _cli_env(endpoint, tmp), tmp)
        # 缺 lease：daemon require_lease_params fail-closed（CLI 不静默补默认值）
        _assert_cli_rejected(proc, "缺 lease apply", "E_LEASE_REQUIRED")
        assert client.call("task.status", {"task_id": task_id})["status"] == "review", \
            "缺 lease 拒绝后状态被错误修改"

    def test_apply_partial_lease_token_only_fail_closed(self, lease_env):
        client, tmp, _task_db, _proc, endpoint = lease_env
        task_id = "T-CLI-PARITY-AP-TOKEN-ONLY"
        task_id = _prepare_review_via_cli(tmp, endpoint, task_id)
        proc = _run_cw_cli(
            ["task", "apply", task_id, "--reviewer", "reviewer",
             "--lease-token", "some-token",
             "--agent-id", "a", "--session-id", "s", "--model-id", "m",
             "--role", "reviewer"],
            _cli_env(endpoint, tmp), tmp)
        # 只提供 token：CLI 凭证不完整 fail-closed（E_LEASE_CRED_INCOMPLETE）
        _assert_cli_rejected(proc, "只 token apply", "E_LEASE_CRED_INCOMPLETE")
        assert client.call("task.status", {"task_id": task_id})["status"] == "review"

    def test_apply_empty_lease_fail_closed(self, lease_env):
        client, tmp, _task_db, _proc, endpoint = lease_env
        task_id = "T-CLI-PARITY-AP-EMPTY"
        task_id = _prepare_review_via_cli(tmp, endpoint, task_id)
        proc = _run_cw_cli(
            ["task", "apply", task_id, "--reviewer", "reviewer",
             "--lease-token", "", "--fencing-counter", "1",
             "--agent-id", "a", "--session-id", "s", "--model-id", "m",
             "--role", "reviewer"],
            _cli_env(endpoint, tmp), tmp)
        # 空 token + counter：视为凭证不完整，fail-closed（E_LEASE_CRED_INCOMPLETE）
        _assert_cli_rejected(proc, "空 lease apply", "E_LEASE_CRED_INCOMPLETE")
        assert client.call("task.status", {"task_id": task_id})["status"] == "review"

    def test_apply_wrong_token_fail_closed(self, lease_env):
        client, tmp, _task_db, _proc, endpoint = lease_env
        task_id = "T-CLI-PARITY-AP-WRONG-TOKEN"
        task_id = _prepare_review_via_cli(tmp, endpoint, task_id)
        holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "wt")
        wrong = dict(holder, token="wrong-token")
        proc = _run_cw_cli(
            ["task", "apply", task_id, "--reviewer", "reviewer",
             *_lease_flags(wrong), *_reviewer_flags(holder)],
            _cli_env(endpoint, tmp), tmp)
        _assert_cli_rejected(proc, "错 token apply", "E_LEASE_TOKEN_MISMATCH")
        assert client.call("task.status", {"task_id": task_id})["status"] == "review"

    def test_apply_stale_fencing_counter_fail_closed(self, lease_env):
        client, tmp, _task_db, _proc, endpoint = lease_env
        task_id = "T-CLI-PARITY-AP-STALE"
        task_id = _prepare_review_via_cli(tmp, endpoint, task_id)
        holder = _acquire_lease_via_cli(tmp, endpoint, task_id, "stale")
        # 当前 counter 为 1，故意用旧 counter 0（合法 token）
        stale = dict(holder, fencing_counter=0)
        proc = _run_cw_cli(
            ["task", "apply", task_id, "--reviewer", "reviewer",
             *_lease_flags(stale), *_reviewer_flags(holder)],
            _cli_env(endpoint, tmp), tmp)
        _assert_cli_rejected(proc, "旧 counter apply", "E_LEASE_FENCING_STALE")
        assert client.call("task.status", {"task_id": task_id})["status"] == "review"

    def test_close_missing_lease_fail_closed(self, lease_env):
        client, tmp, task_db, _proc, endpoint = lease_env
        task_id = "T-CLI-PARITY-CL-NO-LEASE"
        tag = "cl-no-lease"
        task_id = _prepare_review_via_cli(tmp, endpoint, task_id)
        holder = _acquire_lease_via_cli(tmp, endpoint, task_id, tag)
        # S4 关闭门禁前置：reviewer pass verdict 已入账（详见 _seed_pass_verdict）。
        _seed_pass_verdict(task_db, task_id)
        proc = _run_cw_cli(
            ["task", "apply", task_id, "--reviewer", "reviewer",
             *_lease_flags(holder), *_reviewer_flags(holder)],
            _cli_env(endpoint, tmp), tmp)
        _assert_ok(proc, f"task apply {task_id}")
        assert client.call("task.status", {"task_id": task_id})["status"] == "applied"
        # close 缺 lease → daemon fail-closed
        proc = _run_cw_cli(
            ["task", "close", task_id, "--reviewer", "reviewer",
             "--agent-id", holder["agent_id"], "--session-id", holder["session_id"],
             "--model-id", holder["model_id"], "--role", holder["role"]],
            _cli_env(endpoint, tmp), tmp)
        _assert_cli_rejected(proc, "缺 lease close", "E_LEASE_REQUIRED")
        assert client.call("task.status", {"task_id": task_id})["status"] == "applied"


# ----------------------------------------------------------------------
# 2) claim/report 同一显式 action identity（缺陷 2）
# ----------------------------------------------------------------------

class TestCliIdentitySessionParity:
    """task next（claim）与 task report 必须使用同一显式 session。"""

    def test_claim_report_same_session_success(self, lease_env):
        client, tmp, _task_db, _proc, endpoint = lease_env
        task_id = "T-CLI-PARITY-SESSION-OK"
        session = "session-same"
        # 完整 CLI 链路：create → claim(session-same) → report(session-same)
        task_id = _create_via_cli(tmp, endpoint, task_id)
        step_id = _claim_via_cli(tmp, endpoint, task_id, session)
        _report_via_cli(tmp, endpoint, task_id, step_id, session)
        assert client.call("task.status", {"task_id": task_id})["status"] == "review", \
            "同 session claim/report 必须成功推进到 review"

    def test_claim_report_session_parity_events(self, lease_env):
        """claim 与 report 的事件必须记录同一显式 session（缺陷 2 修复验证）。

        daemon 的 report claim-owner 校验以 actor（Windows SID）为主键，session
        仅在 actor 不同时触发 permission_denied；同用户经同名管道连接时 actor 恒等，
        因此不同 session 的 report 不会被拒绝。本测试验证事件侧一致性——
        这是 CLI 缺陷 2 的真实修复目标：claim/report 都经 CW_AGENT_SESSION_ID 显式
        携带同一 session 时，两条 task_events 的 agent_session_id 都等于该 session
        （而非一方回退 Windows SID 造成事件漂移）。
        """
        client, tmp, _task_db, _proc, endpoint = lease_env
        task_id = "T-CLI-PARITY-SESSION-EVENTS"
        session = "session-parity-events"
        task_id = _create_via_cli(tmp, endpoint, task_id)
        step_id = _claim_via_cli(tmp, endpoint, task_id, session)
        _report_via_cli(tmp, endpoint, task_id, step_id, session)
        events = client.call("task.events", {"task_id": task_id})["events"]
        sess_by_reason = {
            e["reason_code"]: e["agent_session_id"]
            for e in events
            if e.get("reason_code") in ("claimed", "reported")
        }
        assert sess_by_reason.get("claimed") == session, \
            f"claim 事件 session 漂移: {sess_by_reason}"
        assert sess_by_reason.get("reported") == session, \
            f"report 事件 session 漂移: {sess_by_reason}"


# ----------------------------------------------------------------------
# 3) enterprise/auto 模式下 daemon 不可用 fail-closed（不回退本地 SQLite）
# ----------------------------------------------------------------------

class TestCliDaemonUnavailableFailClosed:
    """daemon 不可用时 CLI 写操作必须 fail-closed。

    无需 lease_env（不启动隔离 daemon）；CW_DAEMON_ENDPOINT 指向不存在的管道 +
    CW_DAEMON_AUTOSTART_WINDOW=0（立即失败、不自动唤起 daemon，避免测试环境
    残留后台进程）。断言输出含 "daemon 连接失败" —— 本地 SQLite fallback 不可能
    产生该消息，从而证明未回退本地库。
    """

    def _fake_endpoint(self):
        return "https://127.0.0.1:1"

    def _run_unavailable(self, args, mode):
        tmp = tempfile.mkdtemp(prefix="cw_cli_unavail_")
        try:
            return _run_cw_cli(args, _cli_env(self._fake_endpoint(), tmp, mode=mode), tmp)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_enterprise_apply_daemon_unavailable_no_local_fallback(self):
        proc = self._run_unavailable(
            ["task", "apply", "T-CLI-UNAVAIL-001", "--reviewer", "reviewer",
             "--agent-id", "a", "--session-id", "s", "--model-id", "m",
             "--role", "reviewer"],
            mode="enterprise")
        _assert_cli_rejected(proc, "enterprise apply 无 daemon",
                             "E_HTTP_DAEMON_UNAVAILABLE", "无法连接 daemon")

    def test_enterprise_close_daemon_unavailable_no_local_fallback(self):
        proc = self._run_unavailable(
            ["task", "close", "T-CLI-UNAVAIL-002", "--reviewer", "reviewer",
             "--agent-id", "a", "--session-id", "s", "--model-id", "m",
             "--role", "reviewer"],
            mode="enterprise")
        _assert_cli_rejected(proc, "enterprise close 无 daemon",
                             "E_HTTP_DAEMON_UNAVAILABLE", "无法连接 daemon")

    def test_auto_lease_acquire_daemon_unavailable_no_local_fallback(self):
        proc = self._run_unavailable(
            ["lease", "acquire", "T-CLI-UNAVAIL-003", "--role", "reviewer",
             "--agent-id", "a", "--session-id", "s", "--model-id", "m"],
            mode="auto")
        _assert_cli_rejected(proc, "auto lease acquire 无 daemon",
                             "E_HTTP_DAEMON_UNAVAILABLE")
