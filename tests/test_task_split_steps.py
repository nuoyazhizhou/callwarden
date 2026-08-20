"""task.split plan_file 路径回归测试（任务 A：T-1786332208647-eb4a39c0）。

缺陷：rust_ext/src/daemon/task_collab.rs handle_task_split 的 plan_file 分支
只 INSERT tasks 创建子任务，不调用 insert_task_steps，导致 `task.split --plan`
创建的子任务 Steps(0)。修复后 plan_file 路径必须为每个子任务写入完整步骤。

覆盖：
1. 源码断言：plan_file 分支存在 insert_task_steps 调用（防回归）
2. 源码断言：parse_subtasks_from_plan_text 产出 action/target_file/target_symbol/
   check_items 字段（与 subtasks 参数路径一致）
3. 真实 CLI：cw task split --plan 后子任务 steps 完整（daemon/CLI E2E）
4. 真实 CLI：多子任务不互相串步骤
"""
import json
import os
import shutil
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

def test_plan_file_branch_calls_insert_task_steps():
    """S1：plan_file 分支必须调用 insert_task_steps。"""
    src = _collab_src()
    # plan_file 读取后的循环体内必须有 insert_task_steps 调用
    assert "let plan_text = if !plan_file.is_empty()" in src
    assert "insert_task_steps(&tx, &sub_id, &step_values, ts)?" in src
    # 确保调用发生在同一事务（tx 被复用）
    assert "sub_id, st_title, st_desc, peer.owner_key()" in src


def test_parse_subtasks_produces_full_step_fields():
    """S2：解析出的步骤字段与 subtasks 参数路径一致（action/target_file/
    target_symbol/check_items 四字段齐全）。"""
    src = _collab_src()
    assert '"action".to_string(), Value::String(action)' in src
    assert '"target_file".to_string(), Value::String(target_file)' in src
    assert '"target_symbol".to_string()' in src
    assert '"check_items".to_string()' in src


def test_parse_subtasks_handles_action_formats():
    """S2：三种步骤写法（@ / : / 纯 action）均被支持。"""
    src = _collab_src()
    assert "content.find('@')" in src
    assert "content.find(':')" in src


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
        # 管道尚未就绪时 cw-client 可能阻塞等待，按未连接处理
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

    tmp = tempfile.mkdtemp(prefix="task-split-e2e-")
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


def _task_db_steps(task_db: str, task_id: str):
    """只读查询 task_steps（隔离临时测试库，非权威库，仅测试用途）。"""
    import sqlite3

    conn = sqlite3.connect(f"file:{task_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT step_index, action, target_file, target_symbol, check_items, status "
            "FROM task_steps WHERE task_id = ? ORDER BY step_index",
            (task_id,),
        ).fetchall()
        return rows
    finally:
        conn.close()


@_requires_bin
def test_cli_split_plan_persists_steps(isolated_daemon):
    """S4：cw task split --plan 后子任务必须有完整 steps（非 0）。"""
    pipe = isolated_daemon["pipe"]
    task_db = isolated_daemon["task_db"]

    env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    # 1. 通过 daemon RPC 创建父任务（走 route_task_write 的 daemon 路径）
    r1 = subprocess.run(
        [sys.executable, _CW_BIN, "task", "create",
         "--title", "E2E 父任务 split 测试", "--desc", "e2e parent"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    assert r1.returncode == 0, f"task create 失败: {r1.stdout} {r1.stderr}"
    parent_id = None
    for line in r1.stdout.splitlines():
        for tok in line.split():
            if tok.startswith("T-"):
                parent_id = tok.strip()
    assert parent_id, f"无法从输出解析父任务 ID: {r1.stdout}"

    # 2. 写 plan 文件（与 docs/plan-task-split-fix.md 同构：## 标题 + - 步骤）
    plan_path = os.path.join(isolated_daemon["tmp"], "plan.md")
    with open(plan_path, "w", encoding="utf-8") as f:
        f.write(
            "# 计划\n"
            "\n"
            "## 子任务甲\n"
            "- implement @ rust_ext/src/daemon/task_collab.rs\n"
            "- test @ tests/test_task_split_steps.py\n"
            "\n"
            "## 子任务乙\n"
            "路由逻辑\n"
            "- implement @ server/daemon_client.py\n"
        )

    # 3. cw task split --plan（走 daemon RPC 路径）
    r2 = subprocess.run(
        [sys.executable, _CW_BIN, "task", "split", parent_id,
         "--plan", plan_path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, timeout=120,
    )
    assert r2.returncode == 0, f"task split 失败: {r2.stdout} {r2.stderr}"

    # 4. 子任务 steps 必须完整（修复前为 0）
    sub1 = f"{parent_id}-sub-1"
    sub2 = f"{parent_id}-sub-2"
    steps1 = _task_db_steps(task_db, sub1)
    steps2 = _task_db_steps(task_db, sub2)

    assert len(steps1) == 2, f"sub-1 应有 2 步，实际 {len(steps1)}: {steps1}"
    assert steps1[0][1] == "implement"
    assert steps1[0][2] == "rust_ext/src/daemon/task_collab.rs"
    assert steps1[1][1] == "test"
    assert steps1[1][2] == "tests/test_task_split_steps.py"

    assert len(steps2) == 1, f"sub-2 应有 1 步，实际 {len(steps2)}: {steps2}"
    assert steps2[0][1] == "implement"
    assert steps2[0][2] == "server/daemon_client.py"
