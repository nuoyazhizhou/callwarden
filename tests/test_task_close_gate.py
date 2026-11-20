"""task close 父子状态门禁回归测试（任务 B：T-1786412969125-6edaa100）。

缺陷现场：rust daemon `handle_task_close`/`handle_task_apply` 直接 UPDATE 任务
状态，不检查子任务状态、不检查步骤完成度、不校验 lease 凭证，导致：
1. 父任务在子任务未关闭时被误关闭（父子状态门禁缺失）
2. 零步骤任务被误关闭（无验收证据）
3. lease 受保护写完全跳过校验（fail-open）

覆盖（对应实施计划 S1-S6）：
1. 源码断言：handle_task_close 含子任务状态门禁（E_CHILD_TASKS_NOT_CLOSED）
2. 源码断言：零步骤 / 未完成步骤门禁（E_NO_STEPS / E_STEPS_NOT_DONE）
3. 源码断言：lease clock fail-closed（E_LEASE_CLOCK_UNAVAILABLE，apply/close 均拒绝）
4. 源码断言：completion-review 零步骤任务 blocked（不得 vacuous pass）
5. 源码断言：closed_at 写入真实非零时间戳
6. 真实 CLI E2E：父任务含 open 子任务 close 被拒；叶子任务 pending 步骤 close 被拒；
   零步骤任务 close 被拒；全部步骤 done 后 close 成功且 closed_at 非零
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

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _daemon_src():
    """聚合 rust_ext/src/daemon 下全部非测试 Rust 源码。

    stale 依据：生产已把单文件 task_collab.rs 拆分为多文件（现 task_collab.rs 仅剩薄壳）：
    - task_collab_lifecycle_apply.rs:96 handle_task_close / :156 parent_id = ?1 AND
      status != 'closed' / :163 E_CHILD_TASKS_NOT_CLOSED / :184 E_NO_STEPS /
      :201 E_STEPS_NOT_DONE / :212 closed_at = ?1
    - task_collab_planning.rs:385 handle_task_completion_review / :415 "blocked" /
      :418 "E_NO_STEPS" / :411 无法进行完成性评审
    - task_collab_lifecycle_ops.rs:174 validate_lease_for_mutation / :186
      E_LEASE_CLOCK_UNAVAILABLE / :188 lease clock 不可用
    - task_collab.rs:1895 pub fn with_clock
    - task_loop/lifecycle_lease.rs:859 status IN ('pending', 'failed', 'blocked')
    只读 task_collab.rs 必然 substring not found / assert in <薄壳>。
    这里递归聚合所有文件名不含 "test" 的 .rs，排除测试源码以免断言被测试代码自身满足。
    """
    root = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon")
    parts = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in sorted(filenames):
            if not name.endswith(".rs") or "test" in name:
                continue
            with open(os.path.join(dirpath, name), encoding="utf-8") as f:
                parts.append(f.read())
    return "\n".join(parts)


# ------------------------------------------------------------
# 1. 源码静态断言（不依赖运行时，Windows/Linux 均可执行）
# ------------------------------------------------------------

def test_close_has_child_status_gate():
    """S1：handle_task_close 必须检查非 closed 子任务，存在时拒绝关闭。"""
    src = _daemon_src()
    # 关键校验点：close_idx / child_gate_idx / write_idx 均落在同一文件
    # task_collab_lifecycle_apply.rs（96 / 163 / 212），相对顺序不因目录聚合改变。
    assert '"E_CHILD_TASKS_NOT_CLOSED"' in src
    assert "parent_id = ?1 AND status != 'closed'" in src
    # 拒绝错误必须发生在任务状态写入之前（fail-closed）
    close_idx = src.index("pub fn handle_task_close")
    child_gate_idx = src.index("E_CHILD_TASKS_NOT_CLOSED", close_idx)
    write_idx = src.index("status = 'closed'", close_idx)
    assert child_gate_idx < write_idx, "子任务门禁必须先于 closed 写入"


def test_close_has_zero_steps_and_not_done_gate():
    """S2：叶子任务必须至少一个步骤且全部 done/skipped 才能关闭。"""
    src = _daemon_src()
    assert '"E_NO_STEPS"' in src
    assert '"E_STEPS_NOT_DONE"' in src
    assert "status IN ('pending', 'failed', 'blocked')" in src


def test_apply_close_lease_clock_fail_closed():
    """S3：lease clock 不可用时 apply/close 均返回 E_LEASE_CLOCK_UNAVAILABLE。"""
    src = _daemon_src()
    assert '"E_LEASE_CLOCK_UNAVAILABLE"' in src
    assert "fn validate_lease_for_mutation" in src
    assert "pub fn with_clock" in src
    # 校验函数必须做 fail-closed 的时钟检测（store 未注入时钟即拒绝）
    assert "lease clock 不可用" in src


def test_completion_review_zero_steps_blocked():
    """S4：零步骤普通任务 completion-review 返回 blocked，不能 vacuous pass。"""
    src = _daemon_src()
    # review_idx（task_collab_planning.rs:385）及其后的 blocked/E_NO_STEPS/无法进行
    # 完成性评审（同文件 415/418/411）相对顺序不变，切片语义仍成立。
    review_idx = src.index("pub fn handle_task_completion_review")
    review_src = src[review_idx:]
    assert '"blocked"' in review_src
    assert '"E_NO_STEPS"' in review_src
    assert "无法进行完成性评审" in review_src


def test_close_writes_closed_at():
    """S5：close 的 UPDATE 必须写入真实非零 closed_at 时间戳。"""
    src = _daemon_src()
    close_idx = src.index("pub fn handle_task_close")
    close_src = src[close_idx:]
    assert "closed_at = ?1" in close_src


# ------------------------------------------------------------
# 2. 真实 CLI E2E（Windows daemon，ADR-002 单轨 HTTPS + manifest）
# ------------------------------------------------------------

pytestmark_cli = pytest.mark.skipif(
    sys.platform != "win32",
    reason="真实 cw daemon/CLI E2E 需要 Windows + 本机 mTLS PKI",
)

_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe")
_CW_BIN = os.path.join(_REPO_ROOT, "cw.py")
_requires_bin = pytest.mark.skipif(
    not os.path.exists(_DAEMON_BIN),
    reason="cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）",
)

# fixture 会临时改写这些 env，teardown 逐项还原（避免污染后续测试）
_CLI_ENV_KEYS = (
    "USERPROFILE", "HOME", "CW_DAEMON_HTTP_ENDPOINT", "CW_DAEMON_TRANSPORT",
    "CW_DAEMON_MODE", "CW_TASK_WRITE_POLICY", "CW_DAEMON_AUTOSTART_WINDOW",
    "CALLWARDEN_SKIP_AUTO_SETUP",
)


@pytest.fixture(scope="module")
def isolated_daemon():
    """隔离 HTTPS daemon + workspace 权威栈（USERPROFILE 重定向，不碰真实 HOME）。

    沿用 `_w3_harness.setup_w3_client`：隔离 PKI / manifest / registry / task DB，
    并补齐 CLI `resolve_workspace_pair` 所需的 registry 注册与 task-DB canonical
    workspace 权威（workspaces + workspace_authority_captures）。
    """
    from _w3_harness import (
        restore_w3_env, seed_cli_task_agent, seed_cli_task_authority, setup_w3_client,
    )

    prev_pki = os.environ.pop("CW_PKI_DIR", None)
    tmp = tempfile.mkdtemp(prefix="task-close-e2e-")
    proc = None
    saved_env = {k: os.environ.get(k) for k in _CLI_ENV_KEYS}
    try:
        client, inst, endpoint, proc = setup_w3_client(tmp, _REPO_ROOT, ws_id=1)
        if client is None:
            pytest.skip("隔离 HTTPS daemon 前置不可用（二进制/PKI/端口）")
        task_db = os.path.join(tmp, "task.db")
        seed_cli_task_authority(
            task_db, ws_id=1, name="task-close-e2e",
            root_path=_REPO_ROOT, instance_id=inst,
        )
        # task.claim 要求 identity 已注册且 active（role 必须与 claim 一致）
        seed_cli_task_agent(
            task_db, "agent-close-e2e", agent_name="close-e2e",
            session_id="session-close-e2e", model_id="model-close-e2e",
            role="implementer",
        )
        # CLI 子进程经这些 env 命中隔离 daemon（身份证书由 setup_w3_client 注入）
        os.environ.update({
            "USERPROFILE": os.path.join(tmp, "userhome"),
            "HOME": os.path.join(tmp, "userhome"),
            "CW_DAEMON_HTTP_ENDPOINT": endpoint,
            "CW_DAEMON_TRANSPORT": "http",
            "CW_DAEMON_MODE": "enterprise",
            "CW_TASK_WRITE_POLICY": "shared",
            "CW_DAEMON_AUTOSTART_WINDOW": "0",
            "CALLWARDEN_SKIP_AUTO_SETUP": "1",
        })
        yield {"tmp": tmp, "task_db": task_db}
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        restore_w3_env()
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        if prev_pki is not None:
            os.environ["CW_PKI_DIR"] = prev_pki
        shutil.rmtree(tmp, ignore_errors=True)


def _run_cw(args: list, timeout: int = 120):
    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    return subprocess.run(
        [sys.executable, _CW_BIN] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=timeout,
    )


def _parse_task_id(stdout: str) -> str:
    for line in stdout.splitlines():
        for tok in line.split():
            if tok.startswith("T-"):
                return tok.strip()
    raise AssertionError(f"无法从输出解析任务 ID: {stdout}")


def _task_db_step_ids(task_db: str, task_id: str) -> list:
    """只读查询 task_steps id（隔离临时测试库，非权威库，仅测试用途）。"""
    conn = sqlite3.connect(f"file:{task_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, status FROM task_steps WHERE task_id = ? ORDER BY step_index",
            (task_id,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        conn.close()


def _task_db_status(task_db: str, task_id: str) -> str:
    conn = sqlite3.connect(f"file:{task_db}?mode=ro", uri=True)
    try:
        return conn.execute(
            "SELECT status FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def _seed_pass_verdict(task_db: str, task_id: str) -> None:
    """测试注入：预置 reviewer pass verdict（关闭门禁前置条件，非本次断言对象）。

    `verdict.submit` 的完整 provenance 契约由 test_task_verdict_cli 覆盖；这里
    只验证 close 在「已有 pass verdict」时放行并写入 closed_at。
    """
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT INTO task_verdict_events "
            "(verdict_id, task_id, contract_id, contract_revision, contract_hash, "
            "phase, reviewer_identity, findings, overall, attestation, submitted_at) "
            "VALUES (?, ?, ?, 1, ?, 'blind_first_pass', '{}', '[]', 'pass', "
            "'e2e pass verdict', ?)",
            (f"V-{task_id}", task_id, f"TC-{task_id}", f"sha256:{task_id}",
             time.time()),
        )
        conn.commit()
    finally:
        conn.close()


_IDENTITY = [
    "--agent-id", "agent-close-e2e",
    "--session-id", "session-close-e2e",
    "--model-id", "model-close-e2e",
]


def _acquire_lease(task_id: str, role: str) -> list:
    """按 P4 受保护写契约领取 lease，返回 --lease-token/--fencing-counter 参数。"""
    r = _run_cw(["lease", "acquire", task_id, "--role", role, *_IDENTITY, "--json"])
    assert r.returncode == 0, f"lease acquire 失败: {r.stdout} {r.stderr}"
    data = json.loads(r.stdout)
    assert data.get("ok") and data.get("token"), f"lease acquire 未返回 token: {data}"
    return [
        "--lease-token", data["token"],
        "--fencing-counter", str(data["fencing_counter"]),
    ]


def _close_with_lease(task_id: str, role: str = "reviewer"):
    """携带结构化身份 + 有效 lease 执行 close（P3/P4 治理契约）。"""
    return _run_cw([
        "task", "close", task_id, "--role", role, *_IDENTITY,
        *_acquire_lease(task_id, role),
    ])


@_requires_bin
def test_cli_close_parent_with_open_child_rejected(isolated_daemon):
    """S1：父任务含 open 子任务时 `cw task close` 被拒绝（E_CHILD_TASKS_NOT_CLOSED）。"""
    # 1. 创建父任务
    r1 = _run_cw(["task", "create", "--title", "close 门禁父任务", "--desc", "parent"])
    assert r1.returncode == 0, f"task create 失败: {r1.stdout} {r1.stderr}"
    parent_id = _parse_task_id(r1.stdout)

    # 2. 用 split 创建 1 个 open 子任务
    plan_path = os.path.join(isolated_daemon["tmp"], "plan-close.md")
    with open(plan_path, "w", encoding="utf-8") as f:
        f.write("# 计划\n\n## 子任务\n子任务描述\n- implement @ a.py\n")
    r2 = _run_cw(["task", "split", parent_id, "--plan", plan_path])
    assert r2.returncode == 0, f"task split 失败: {r2.stdout} {r2.stderr}"

    # 3. 关闭父任务必须被拒绝，且子任务状态不被破坏
    #    注：CLI 对 daemon 业务错误打印到 stdout 并返回 0 退出码（既有契约，非本任务可改范围），
    #    因此断言错误码出现在输出中 + 任务状态未被写入 closed
    r3 = _close_with_lease(parent_id)
    assert "E_CHILD_TASKS_NOT_CLOSED" in r3.stdout + r3.stderr, f"父任务关闭应被拒绝: {r3.stdout} {r3.stderr}"

    # 4. 父任务状态保持原样（未写入 closed）
    assert _task_db_status(isolated_daemon["task_db"], parent_id) != "closed"


@_requires_bin
def test_cli_close_leaf_with_pending_steps_rejected(isolated_daemon):
    """S2：叶子任务含 pending 步骤时 close 被拒绝（E_STEPS_NOT_DONE）。"""
    steps = json.dumps([
        {"action": "implement", "target_file": "a.py"},
        {"action": "verify", "target_file": "a.py"},
    ])
    r1 = _run_cw(["task", "create", "--title", "pending 步骤任务", "--steps", steps])
    assert r1.returncode == 0, f"task create 失败: {r1.stdout} {r1.stderr}"
    task_id = _parse_task_id(r1.stdout)

    assert len(_task_db_step_ids(isolated_daemon["task_db"], task_id)) == 2

    r2 = _close_with_lease(task_id)
    # CLI 对 daemon 业务错误打印到 stdout 并返回 0 退出码（既有契约），断言错误码即可
    assert "E_STEPS_NOT_DONE" in r2.stdout + r2.stderr, f"pending 步骤任务关闭应被拒绝: {r2.stdout} {r2.stderr}"


@_requires_bin
def test_cli_close_zero_step_task_rejected(isolated_daemon):
    """S2：零步骤任务 close 被拒绝（E_NO_STEPS）。"""
    r1 = _run_cw(["task", "create", "--title", "零步骤任务", "--desc", "no steps"])
    assert r1.returncode == 0, f"task create 失败: {r1.stdout} {r1.stderr}"
    task_id = _parse_task_id(r1.stdout)

    r2 = _close_with_lease(task_id)
    # CLI 对 daemon 业务错误打印到 stdout 并返回 0 退出码（既有契约），断言错误码即可
    assert "E_NO_STEPS" in r2.stdout + r2.stderr, f"零步骤任务关闭应被拒绝: {r2.stdout} {r2.stderr}"


@_requires_bin
def test_cli_close_succeeds_after_steps_done(isolated_daemon):
    """S5：全部步骤 done 后 close 成功，closed_at 写入真实非零时间戳。"""
    steps = json.dumps([{"action": "implement", "target_file": "a.py"}])
    r1 = _run_cw(["task", "create", "--title", "可关闭任务", "--steps", steps])
    assert r1.returncode == 0, f"task create 失败: {r1.stdout} {r1.stderr}"
    task_id = _parse_task_id(r1.stdout)

    step_ids = _task_db_step_ids(isolated_daemon["task_db"], task_id)
    assert len(step_ids) == 1

    # 领取并回报唯一步骤为 done（P3 结构化身份 + P4 受保护写 lease）
    impl_lease = _acquire_lease(task_id, "implementer")
    r2 = _run_cw(["task", "next", task_id, "--role", "implementer", *_IDENTITY])
    assert r2.returncode == 0, f"task next 失败: {r2.stdout} {r2.stderr}"
    r3 = _run_cw([
        "task", "report", task_id, step_ids[0], "--result", "ok",
        "--role", "implementer", *_IDENTITY, *impl_lease,
    ])
    assert r3.returncode == 0, f"task report 失败: {r3.stdout} {r3.stderr}"

    # report 后任务进入 review 态：预置 pass verdict 满足 verdict 关闭门禁
    _seed_pass_verdict(isolated_daemon["task_db"], task_id)

    # 步骤全部 done 后允许关闭
    r4 = _close_with_lease(task_id)
    assert r4.returncode == 0, f"task close 失败: {r4.stdout} {r4.stderr}"

    # closed_at 非零
    conn = sqlite3.connect(f"file:{isolated_daemon['task_db']}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT status, closed_at FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert row[0] == "closed"
    assert row[1] is not None and row[1] > 0, f"closed_at 应为真实非零时间戳，实际 {row[1]}"
