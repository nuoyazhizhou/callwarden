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
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _daemon_src():
    """聚合 rust_ext/src/daemon 下全部非测试 Rust 源码。

    stale 依据：生产已把单文件 task_collab.rs 拆分为多文件（现 task_collab.rs 仅剩薄壳）：
    - task_collab_planning.rs:108 handle_task_split / :206 let plan_text = if
      !plan_file.is_empty() / :227 params![sub_id, st_title, st_desc, owner, ts, ts,
      task_id] / :231 insert_task_steps(&tx, &sub_id, &steps, ts)?
    - parse_subtasks_from_plan_text 仍在 task_collab.rs:1465 content.find('@') /
      :1470 content.find(':') / :1478-1481 action/target_file/target_symbol/check_items
    只读 task_collab.rs 必然 assert in <薄壳> 失败。
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

def test_plan_file_branch_calls_insert_task_steps():
    """S1：plan_file 分支必须调用 insert_task_steps。"""
    src = _daemon_src()
    # plan_file 读取后的循环体内必须有 insert_task_steps 调用
    assert "let plan_text = if !plan_file.is_empty()" in src
    # stale 依据：task_collab_planning.rs:231 实际形参名为 steps（非旧 step_values），
    # 拆分时该变量被重命名。
    assert "insert_task_steps(&tx, &sub_id, &steps, ts)?" in src
    # stale 依据：task_collab_planning.rs:227 现在用本地 owner 变量
    # （:125 let owner = peer.owner_key()），不再内联 peer.owner_key()。
    assert "sub_id, st_title, st_desc, owner, ts, ts, task_id" in src


def test_parse_subtasks_produces_full_step_fields():
    """S2：解析出的步骤字段与 subtasks 参数路径一致（action/target_file/
    target_symbol/check_items 四字段齐全）。"""
    src = _daemon_src()
    assert '"action".to_string(), Value::String(action)' in src
    assert '"target_file".to_string(), Value::String(target_file)' in src
    assert '"target_symbol".to_string()' in src
    assert '"check_items".to_string()' in src


def test_parse_subtasks_handles_action_formats():
    """S2：三种步骤写法（@ / : / 纯 action）均被支持。"""
    src = _daemon_src()
    assert "content.find('@')" in src
    assert "content.find(':')" in src


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
    from _w3_harness import restore_w3_env, seed_cli_task_authority, setup_w3_client

    prev_pki = os.environ.pop("CW_PKI_DIR", None)
    tmp = tempfile.mkdtemp(prefix="task-split-e2e-")
    proc = None
    saved_env = {k: os.environ.get(k) for k in _CLI_ENV_KEYS}
    try:
        client, inst, endpoint, proc = setup_w3_client(tmp, _REPO_ROOT, ws_id=1)
        if client is None:
            pytest.skip("隔离 HTTPS daemon 前置不可用（二进制/PKI/端口）")
        task_db = os.path.join(tmp, "task.db")
        seed_cli_task_authority(
            task_db, ws_id=1, name="task-split-e2e",
            root_path=_REPO_ROOT, instance_id=inst,
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
            "子任务甲描述\n"
            "- implement @ rust_ext/src/daemon/task_collab.rs\n"
            "- test @ tests/test_task_split_steps.py\n"
            "\n"
            "## 子任务乙\n"
            "子任务乙描述\n"
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
