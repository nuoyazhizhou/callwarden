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


def _collab_src():
    with open(
        os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "task_collab.rs"),
        encoding="utf-8",
    ) as f:
        return f.read()


# ------------------------------------------------------------
# 1. 源码静态断言（不依赖运行时，Windows/Linux 均可执行）
# ------------------------------------------------------------

def test_close_has_child_status_gate():
    """S1：handle_task_close 必须检查非 closed 子任务，存在时拒绝关闭。"""
    src = _collab_src()
    assert '"E_CHILD_TASKS_NOT_CLOSED"' in src
    assert "parent_id = ?1 AND status != 'closed'" in src
    # 拒绝错误必须发生在任务状态写入之前（fail-closed）
    close_idx = src.index("pub fn handle_task_close")
    child_gate_idx = src.index("E_CHILD_TASKS_NOT_CLOSED", close_idx)
    write_idx = src.index("status = 'closed'", close_idx)
    assert child_gate_idx < write_idx, "子任务门禁必须先于 closed 写入"


def test_close_has_zero_steps_and_not_done_gate():
    """S2：叶子任务必须至少一个步骤且全部 done/skipped 才能关闭。"""
    src = _collab_src()
    assert '"E_NO_STEPS"' in src
    assert '"E_STEPS_NOT_DONE"' in src
    assert "status IN ('pending', 'failed', 'blocked')" in src


def test_apply_close_lease_clock_fail_closed():
    """S3：lease clock 不可用时 apply/close 均返回 E_LEASE_CLOCK_UNAVAILABLE。"""
    src = _collab_src()
    assert '"E_LEASE_CLOCK_UNAVAILABLE"' in src
    assert "fn validate_lease_for_mutation" in src
    assert "pub fn with_clock" in src
    # 校验函数必须做 fail-closed 的时钟检测（store 未注入时钟即拒绝）
    assert "lease clock 不可用" in src


def test_completion_review_zero_steps_blocked():
    """S4：零步骤普通任务 completion-review 返回 blocked，不能 vacuous pass。"""
    src = _collab_src()
    review_idx = src.index("pub fn handle_task_completion_review")
    review_src = src[review_idx:]
    assert '"blocked"' in review_src
    assert '"E_NO_STEPS"' in review_src
    assert "无法进行完成性评审" in review_src


def test_close_writes_closed_at():
    """S5：close 的 UPDATE 必须写入真实非零 closed_at 时间戳。"""
    src = _collab_src()
    close_idx = src.index("pub fn handle_task_close")
    close_src = src[close_idx:]
    assert "closed_at = ?1" in close_src


# ------------------------------------------------------------
# 2. 真实 CLI E2E（Windows daemon Named Pipe）
# ------------------------------------------------------------

pytestmark_cli = pytest.mark.skipif(
    sys.platform != "win32",
    reason="真实 cw daemon/CLI E2E 需要 Windows + Named Pipe",
)

_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe")
_CLIENT_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-client.exe")
_CW_BIN = os.path.join(_REPO_ROOT, "cw.py")
_requires_bin = pytest.mark.skipif(
    not (os.path.exists(_DAEMON_BIN) and os.path.exists(_CLIENT_BIN)),
    reason="cw-daemon.exe / cw-client.exe 未构建（需先 cargo build --bin cw-daemon --bin cw-client）",
)


def _client(pipe: str, args: list, timeout: int = 15) -> dict:
    """调用真实 cw-client.exe（Named Pipe 客户端），返回结构化结果。"""
    try:
        result = subprocess.run(
            [_CLIENT_BIN, "--socket", pipe, "--timeout", str(timeout)] + args,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        return {"code": -1, "json": {}, "stdout": "", "stderr": "client timeout"}
    parsed = {}
    try:
        parsed = json.loads(result.stdout)
    except Exception:
        pass
    return {
        "code": result.returncode,
        "json": parsed,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _daemon_config(tmp: str) -> dict:
    data_root = os.path.join(tmp, "data")
    return {
        "socket_path": "",
        "registry_db_path": os.path.join(tmp, "registry.db"),
        "task_db_path": os.path.join(tmp, "callwarden.db"),
        "data_root": data_root,
        "max_workers": 4,
        "request_timeout_secs": 30,
        "snapshot_cache_capacity": 4,
        "codegraph_db_path_template": os.path.join(
            data_root, "workspaces", "{workspace_instance_id}", "codegraph.db"
        ),
        "socket_mode": 0o660,
        "socket_group": "",
        "stage_toggle_db_path": os.path.join(tmp, "stage_toggle.db"),
    }


@pytest.fixture(scope="module")
def isolated_daemon():
    """启动隔离临时目录的真实 cw-daemon（Named Pipe 由 transport 按 SID 派生）。"""
    from callwarden.config import _get_windows_user_sid

    sid = _get_windows_user_sid()
    pipe = rf"\\.\pipe\callwarden-{sid}"

    # 若默认管道已被其他 daemon 占用，跳过（避免干扰既有实例）
    occupied = _client(pipe, ["ping"], timeout=2)
    if occupied["code"] == 0:
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过进程级 E2E")

    tmp = tempfile.mkdtemp(prefix="task-close-e2e-")
    config_path = os.path.join(tmp, "config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(_daemon_config(tmp), f)

    log_path = os.path.join(tmp, "daemon.log")
    log = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [_DAEMON_BIN, "--config", config_path],
        stdout=log, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8",
    )
    try:
        deadline = time.time() + 40.0
        ok = False
        time.sleep(1.5)  # 给 daemon 初始化（加载配置/注册表/任务库）留出时间
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            r = _client(pipe, ["ping"], timeout=2)
            if r["code"] == 0 and r["json"].get("status") == "ok":
                ok = True
                break
            time.sleep(0.5)
        if not ok:
            log_content = ""
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    log_content = f.read()[-3000:]
            except Exception:
                pass
            pytest.fail(f"daemon 未在超时内响应，日志：\n{log_content}")
        yield {"pipe": pipe, "tmp": tmp, "task_db": os.path.join(tmp, "callwarden.db")}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log.close()
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
        f.write("# 计划\n\n## 子任务\n- implement @ a.py\n")
    r2 = _run_cw(["task", "split", parent_id, "--plan", plan_path])
    assert r2.returncode == 0, f"task split 失败: {r2.stdout} {r2.stderr}"

    # 3. 关闭父任务必须被拒绝，且子任务状态不被破坏
    #    注：CLI 对 daemon 业务错误打印到 stdout 并返回 0 退出码（既有契约，非本任务可改范围），
    #    因此断言错误码出现在输出中 + 任务状态未被写入 closed
    r3 = _run_cw(["task", "close", parent_id, "--reviewer", "reviewer"])
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

    r2 = _run_cw(["task", "close", task_id, "--reviewer", "reviewer"])
    # CLI 对 daemon 业务错误打印到 stdout 并返回 0 退出码（既有契约），断言错误码即可
    assert "E_STEPS_NOT_DONE" in r2.stdout + r2.stderr, f"pending 步骤任务关闭应被拒绝: {r2.stdout} {r2.stderr}"


@_requires_bin
def test_cli_close_zero_step_task_rejected(isolated_daemon):
    """S2：零步骤任务 close 被拒绝（E_NO_STEPS）。"""
    r1 = _run_cw(["task", "create", "--title", "零步骤任务", "--desc", "no steps"])
    assert r1.returncode == 0, f"task create 失败: {r1.stdout} {r1.stderr}"
    task_id = _parse_task_id(r1.stdout)

    r2 = _run_cw(["task", "close", task_id, "--reviewer", "reviewer"])
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

    # 领取并回报唯一步骤为 done
    r2 = _run_cw(["task", "next", task_id])
    assert r2.returncode == 0, f"task next 失败: {r2.stdout} {r2.stderr}"
    r3 = _run_cw(["task", "report", task_id, step_ids[0], "--result", "ok"])
    assert r3.returncode == 0, f"task report 失败: {r3.stdout} {r3.stderr}"

    # 步骤全部 done 后允许关闭
    r4 = _run_cw(["task", "close", task_id, "--reviewer", "reviewer"])
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
