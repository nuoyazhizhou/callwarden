"""C-16 回归：`assignment_show` 的 workspace 权威解析（真实隔离 daemon 功能矩阵）。

背景（本卡 `T-1789365537146-bef4c2e4` 承接卡 A 出界发现的 C-16 缺陷）：

  - `handle_assignment_show` 原以 `unwrap_or(0)` 取 `workspace_id`
    （`rust_ext/src/daemon/task_collab_lease.rs`，旧实现），与
    `task_assignments.workspace_id` 的实际取值域（task DB `workspaces.id`）不同源
    → SQL 恒不命中 → 恒返回 `{"status":"none"}`，positive 分支对生产调用方
    （CLI / MCP）整体不可达。
  - 调用侧 `server/daemon_client.py::route_rpc` 对 task-scoped 请求刻意不注入数值
    `workspace_id`（门禁 A：`:3953-3957` 按通用 `_is_task_scoped_authority_request()`
    跳过 instance/root；门禁 B：`:3973-3977` 只对 `task.`/`lease.` 前缀注入数值 id）。
    因此 **daemon 侧必须自己从不可变 `task_workspace_bindings` 解析**。

本测试与 `tests/test_c14_c15_assignment_contract.py`（源码契约/文本提取范式）**互补**：
本文件**不做源码字符串断言**，而是启动**真实 `cw-daemon` 进程**（隔离临时数据目录），
对 6 条负向矩阵逐条做真实 RPC 往返，断言响应体与隔离任务库落库结果。

二进制选择（避开「陈旧二进制得到失真失败」陷阱）：
  1. `CW_DAEMON_BIN` 环境变量（显式指定，最高优先级——CI/验收应指向**当次构建**的产物）；
  2. `rust_ext/target/debug/cw-daemon.exe`（本地 cargo build 产物，与当前源码一致）；
  3. `rust_ext/target/stage-refresh/release/cw-daemon.exe`（部署门禁产出）；
  4. `rust_ext/target/release/cw-daemon.exe`；
  5. `runtime/current/cw-daemon.exe`。
  全部缺失 → `pytest.skip`。夹具会把实际选中的二进制路径 + SHA256 打印到 stdout，
  供证据引用（**不得**用未含修复的旧 binary 的运行结果冒充本卡验收）。

前置条件：Windows（进程级 HTTP daemon harness）、已构建 `cw-daemon`。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from callwarden.db.schema import SCHEMA_INDEXES_SQL, SCHEMA_TABLES_SQL  # noqa: E402
from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="隔离 daemon harness 依赖 Windows 进程/HTTP 传输"
)

# ---- 隔离夹具常量 -------------------------------------------------------

WS_ID = 1                       # 隔离 task DB 里 seed 的 workspaces.id（= binding 值）
BOUND_TASK = "T-C16-BOUND"      # 有 binding 的 task
UNBOUND_TASK = "T-C16-UNBOUND"  # 无 binding 的 task（legacy）
ACTIVE_ASG = "ASG-c16active00000000000000"
REVOKED_ROLE = "c16revprobe"
REVOKED_ASG = "ASG-c16revoked0000000000000"


def _find_daemon_binary():
    """定位当次构建的 cw-daemon；`CW_DAEMON_BIN` 显式覆盖优先。"""
    candidates = [
        os.environ.get("CW_DAEMON_BIN", ""),
        os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe"),
        os.path.join(
            _REPO_ROOT, "rust_ext", "target", "stage-refresh", "release", "cw-daemon.exe"
        ),
        os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe"),
        os.path.join(_REPO_ROOT, "runtime", "current", "cw-daemon.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _spawn_isolated_daemon(bin_path: str, data_root: str) -> subprocess.Popen:
    """启动隔离 daemon（临时 task DB / registry / USERPROFILE），HTTP 绑定临时端口。"""
    home_dir = os.path.join(data_root, "userhome")
    os.makedirs(os.path.join(home_dir, ".callwarden"), exist_ok=True)
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["USERPROFILE"] = home_dir
    return subprocess.Popen(
        [bin_path, "--http-bind=127.0.0.1:0"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_manifest(data_root: str, proc: subprocess.Popen, timeout: float = 60.0):
    """等待并返回隔离 daemon 自己发布的 manifest（只接受 pid 匹配的那份）。"""
    manifest_dir = os.path.join(data_root, "userhome", ".callwarden")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isdir(manifest_dir):
            for name in os.listdir(manifest_dir):
                if name.startswith("http-daemon.") and name.endswith(".manifest.json"):
                    try:
                        m = json.loads(
                            open(os.path.join(manifest_dir, name), encoding="utf-8").read()
                        )
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


def _terminate(proc: subprocess.Popen) -> None:
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _seed(task_db: str) -> None:
    """在隔离 task DB 上建权威 schema 并种入夹具。

    建表用仓库权威 schema（`callwarden.db.schema`），**不手写 DDL** —— 避免夹具与
    生产 schema 漂移（`task_assignments.workspace_id` 的 FK 目标是 `workspaces(id)`，
    正是 C-16 缺陷的命名空间来源，必须与生产逐字一致）。

    FK 语义：`task_assignments.workspace_id` 与 `task_workspace_bindings.workspace_id`
    都 FK 到 `workspaces(id)`，故先种 `workspaces` 行。本连接为测试自有连接
    （daemon 之外），仅写**隔离临时目录**内的库，不触碰生产库。
    """
    conn = sqlite3.connect(task_db, timeout=15)
    try:
        conn.executescript(SCHEMA_TABLES_SQL)
        conn.executescript(SCHEMA_INDEXES_SQL)
        now = time.time()
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (?1, ?2, ?3, ?4, 1)",
            (WS_ID, "c16-isolated-ws", "/c16-isolated-repo", now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO task_workspace_bindings "
            "(task_id, workspace_id, workspace_binding_id, workspace_capture_id, "
            " created_by, authoritative_created_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
            (BOUND_TASK, WS_ID, f"tb-{BOUND_TASK}", "wc-c16-isolated", "test", "0"),
        )
        conn.execute(
            "INSERT OR REPLACE INTO task_assignments "
            "(workspace_id, assignment_id, task_id, role, agent_id, session_id, model_id, "
            " status, created_at, revoked_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'active', ?8, NULL)",
            (WS_ID, ACTIVE_ASG, BOUND_TASK, "implementer", "c16-agent", "c16-sess", "c16-mdl", now),
        )
        conn.execute(
            "INSERT OR REPLACE INTO task_assignments "
            "(workspace_id, assignment_id, task_id, role, agent_id, session_id, model_id, "
            " status, created_at, revoked_at) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'revoked', ?8, ?9)",
            (WS_ID, REVOKED_ASG, BOUND_TASK, REVOKED_ROLE, "c16-agent", "c16-sess", "c16-mdl",
             now, now),
        )
        conn.commit()
    finally:
        conn.close()


def _snapshot_assignments(task_db: str) -> list:
    """只读取 `task_assignments` 全量行（用于只读性断言）。"""
    conn = sqlite3.connect(f"file:{task_db}?mode=ro", uri=True, timeout=15)
    try:
        return [tuple(r) for r in conn.execute(
            "SELECT id, workspace_id, assignment_id, task_id, role, agent_id, session_id, "
            "model_id, status, created_at, revoked_at FROM task_assignments ORDER BY id"
        )]
    finally:
        conn.close()


@pytest.fixture(scope="module")
def c16_daemon(tmp_path_factory):
    """启动隔离 daemon + 种夹具，yield (client, task_db, bin_path, bin_sha256)。"""
    bin_path = _find_daemon_binary()
    if bin_path is None:
        pytest.skip("cw-daemon 二进制不可用（需先 cargo build --bin cw-daemon）")

    bin_sha = hashlib.sha256(open(bin_path, "rb").read()).hexdigest().upper()
    data_root = str(tmp_path_factory.mktemp("c16") / "data")
    os.makedirs(data_root, exist_ok=True)
    task_db = os.path.join(data_root, "task.db")

    # 夹具在 daemon 启动**之前**落库：daemon 侧对隔离 task DB 不做 schema 迁移，
    # 若先启动再建表会因空库无表而无法 seed；先建表也让 daemon 启动即看到完整库。
    _seed(task_db)

    proc = _spawn_isolated_daemon(bin_path, data_root)
    try:
        manifest = _wait_manifest(data_root, proc)
        if manifest is None:
            err = b""
            try:
                fd = proc.stderr.fileno()
                os.set_blocking(fd, False)
                err = os.read(fd, 65536)
            except (BlockingIOError, OSError, ValueError):
                pass
            pytest.fail(
                "隔离 daemon 未发布 manifest\n"
                f"returncode={proc.poll()}\nbinary={bin_path}\n"
                f"stderr={err.decode('utf-8', 'replace')}"
            )
        endpoint = manifest["endpoint"]
        assert endpoint.startswith("http://127.0.0.1:"), f"非 loopback endpoint: {endpoint}"

        # 夹具记录（写进 stdout 供证据引用，避免「用了哪个二进制」不可考）
        print(f"[C-16 harness] bin={bin_path}")
        print(f"[C-16 harness] bin_sha256={bin_sha}")
        print(f"[C-16 harness] endpoint={endpoint}")
        print(f"[C-16 harness] task_db={task_db}")
        print(f"[C-16 harness] seeded={_snapshot_assignments(task_db)}")

        client = HttpDaemonRpcClient(
            endpoint=endpoint, verify_health=False, validate_manifest=False
        )
        yield client, task_db, bin_path, bin_sha
    finally:
        _terminate(proc)


def _show(client, params: dict):
    """调用 `assignment_show`；返回 (result, error) 二者其一为 None。"""
    try:
        return client.call("assignment_show", params), None
    except DaemonRemoteError as exc:  # noqa: BLE001
        return None, exc


# ----------------------------------------------------------------------
# 负向矩阵（6 条，逐条真实 RPC 往返）
# ----------------------------------------------------------------------

def test_matrix_1_no_workspace_param_with_binding_hits_active_row(c16_daemon):
    """① 参数无 workspace_id、task 有 binding → 命中 active 行。"""
    client, _, _, _ = c16_daemon
    result, err = _show(client, {"task_id": BOUND_TASK, "role": "implementer"})
    assert err is None, f"应成功返回 active 行，实际报错: {err}"
    assert result.get("status") != "none", (
        f"positive 分支对生产调用方不可达（C-16 缺陷回归）: {result}"
    )
    assert result.get("assignment_id") == ACTIVE_ASG
    assert result.get("workspace_id") == WS_ID
    assert result.get("status") == "active"


def test_matrix_1b_no_role_filter_also_hits_active_row(c16_daemon):
    """① 变体：不带 role 过滤同样必须命中（覆盖 SQL 的非 role 分支）。"""
    client, _, _, _ = c16_daemon
    result, err = _show(client, {"task_id": BOUND_TASK})
    assert err is None, f"应成功，实际报错: {err}"
    assert result.get("assignment_id") == ACTIVE_ASG


def test_matrix_2_no_binding_fails_closed(c16_daemon):
    """② 参数无 workspace_id、task 无 binding → E_TASK_WORKSPACE_UNBOUND（不得静默 none）。"""
    client, _, _, _ = c16_daemon
    result, err = _show(client, {"task_id": UNBOUND_TASK, "role": "implementer"})
    assert err is not None, f"无 binding 必须 fail-closed，实际返回: {result}"
    assert err.code == "E_TASK_WORKSPACE_UNBOUND", f"错误码不符: {err.code} / {err}"
    assert result is None


def test_matrix_3_workspace_mismatch_rejected(c16_daemon):
    """③ 显式 workspace_id 与 binding 不一致 → E_WORKSPACE_AUTHORITY_MISMATCH。"""
    client, _, _, _ = c16_daemon
    result, err = _show(
        client, {"task_id": BOUND_TASK, "role": "implementer", "workspace_id": WS_ID + 998}
    )
    assert err is not None, f"不一致必须拒绝，实际返回: {result}"
    assert err.code == "E_WORKSPACE_AUTHORITY_MISMATCH", f"错误码不符: {err.code} / {err}"


def test_matrix_3b_workspace_match_accepted(c16_daemon):
    """③ 变体：显式传**一致**的 workspace_id 必须放行（resolver 双分支都覆盖）。"""
    client, _, _, _ = c16_daemon
    result, err = _show(
        client, {"task_id": BOUND_TASK, "role": "implementer", "workspace_id": WS_ID}
    )
    assert err is None, f"一致值应放行，实际报错: {err}"
    assert result.get("assignment_id") == ACTIVE_ASG


@pytest.mark.parametrize("params", [{}, {"task_id": ""}, {"task_id": "   "}])
def test_matrix_4_blank_task_id_is_invalid_params(c16_daemon, params):
    """④ task_id 缺失/空/纯空白 → invalid_params（不 panic、不查 binding）。"""
    client, _, _, _ = c16_daemon
    result, err = _show(client, params)
    assert err is not None, f"空 task_id 必须 fail-closed，实际返回: {result}"
    assert "invalid_params" in (err.code or "") or err.code == "invalid_params", (
        f"应为 invalid_params，实际: {err.code} / {err}"
    )


def test_matrix_5_revoked_assignment_returns_none(c16_daemon):
    """⑤ 已 revoke 的 assignment（append 语义下 active 过滤生效）→ {"status":"none"}。"""
    client, _, _, _ = c16_daemon
    result, err = _show(client, {"task_id": BOUND_TASK, "role": REVOKED_ROLE})
    assert err is None, f"应正常返回 none，实际报错: {err}"
    assert result.get("status") == "none", f"revoked 不应命中: {result}"
    assert result.get("task_id") == BOUND_TASK


def test_matrix_6_read_only(c16_daemon):
    """⑥ 只读性：全部矩阵调用前后 `task_assignments` 内容不变。"""
    client, task_db, _, _ = c16_daemon
    before = _snapshot_assignments(task_db)
    for params in (
        {"task_id": BOUND_TASK, "role": "implementer"},
        {"task_id": BOUND_TASK},
        {"task_id": UNBOUND_TASK},
        {"task_id": BOUND_TASK, "workspace_id": WS_ID + 998},
        {"task_id": BOUND_TASK, "workspace_id": WS_ID},
        {"task_id": BOUND_TASK, "role": REVOKED_ROLE},
        {},
    ):
        _show(client, params)
    after = _snapshot_assignments(task_db)
    assert before == after, "assignment_show 必须是只读的（行集合与内容不得变化）"
    assert len(before) == 2, f"夹具应有 2 行（1 active + 1 revoked），实际 {len(before)}"
