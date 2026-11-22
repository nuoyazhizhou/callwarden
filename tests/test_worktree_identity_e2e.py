"""三 worktree 仓库级身份端到端验收（REQ-05 / C-09）。

方案 A（仓库级 workspace 身份）的进程级验证：主仓 + 2 worktree 各自以真实
git 元数据（remote + 主仓 root）向隔离 daemon 注册，验证：

1. AC-01：三客户端注册收敛到同一 workspace_instance_id；同 remote 不同路径
   clone → 同 instance_id；不同 remote → 不同 instance_id。
2. AC-02：主仓建任务 → 任一 worktree task.list 可见；worktree A 建任务 →
   worktree B 可见；两 worktree 同角色争用同 task lease → 单 winner；
   lease.status 跨 worktree 一致。
3. AC-04（registry 侧）：同 repo 多次注册后 registry 仅一条 active 行。
4. AC-05：非 git 目录客户端注册成功（fail-open 身份，与 git 仓库身份不同）。

与 tests/convergence 共用隔离 daemon 启动/等待/清理助手（同源，避免复制漂移）。
"""
from __future__ import annotations

import concurrent.futures
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TESTS_DIR = os.path.join(_REPO_ROOT, "tests")
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_http_daemon_release_acceptance import (  # noqa: E402
    _backup_http_manifest,
    _isolated_manifest_path,
    _restore_or_clean_http_manifest,
    _spawn_isolated_daemon,
    _terminate,
    _wait_manifest,
)
from _prereq_guard import (  # noqa: E402
    skip_if_daemon_exited_before_manifest,
    skip_if_isolated_daemon_pipe_conflict,
)
from _w3_harness import inject_isolated_mtls_env, restore_w3_env  # noqa: E402

from callwarden.server.daemon_client import (  # noqa: E402
    HttpDaemonRpcClient,
    _workspace_snapshot_metadata,
)
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402

_EXE = "cw-daemon.exe" if os.name == "nt" else "cw-daemon"
_RELEASE_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", _EXE)
_DEBUG_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", _EXE)
# 显式候选入口：设置后以其为准（缺失/无效仍 RuntimeError 硬错，fail-loud）
_EXPLICIT_BIN = os.environ.get("CW_E2E_DAEMON_BIN", "")

# 假 remote（仅入库，从不网络访问）：同一 remote 是三级身份 tier-1 的收敛键
_FAKE_REMOTE = "https://git.example.com/u/ws-e2e.git"


def _pick_bin() -> str:
    if _EXPLICIT_BIN:
        if os.path.isfile(_EXPLICIT_BIN):
            return _EXPLICIT_BIN
        raise RuntimeError(f"显式指定的 CW_E2E_DAEMON_BIN 不存在: {_EXPLICIT_BIN}")
    if os.path.isfile(_RELEASE_BIN):
        return _RELEASE_BIN
    if os.path.isfile(_DEBUG_BIN):
        return _DEBUG_BIN
    pytest.skip(
        f"cw-daemon 二进制候选缺失且未显式指定 CW_E2E_DAEMON_BIN"
        f"（release={_RELEASE_BIN}, debug={_DEBUG_BIN}）")


# ----------------------------------------------------------------------
# git 仓库 / worktree / clone 构造
# ----------------------------------------------------------------------

def _git(cwd: str, *args: str) -> None:
    subprocess.run(
        ["git", "-C", cwd, *args],
        check=True, capture_output=True, text=True,
    )


def _make_repo(base: str, name: str, remote: str = _FAKE_REMOTE) -> str:
    """建一个带 origin remote 的最小 git 仓库（单文件单提交）。"""
    root = os.path.join(base, name)
    os.makedirs(root)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "e2e@example.com")
    _git(root, "config", "user.name", "e2e")
    if remote:
        _git(root, "remote", "add", "origin", remote)
    with open(os.path.join(root, "f.txt"), "w", encoding="utf-8") as f:
        f.write("v1\n")
    _git(root, "add", ".")
    _git(root, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "init")
    return root


def _add_worktree(main_root: str, path: str, branch: str) -> str:
    _git(main_root, "worktree", "add", "-b", branch, path)
    return path


# ----------------------------------------------------------------------
# 隔离 daemon + task-DB seed（与 convergence 夹具同源）
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def ws_e2e():
    """模块级隔离 daemon + 主仓/2 worktree/独立仓/clone/非 git 目录的公共底座。"""
    if shutil.which("git") is None:
        pytest.skip("git 不可用：三 worktree e2e 需要真实 git")
    bin_path = _pick_bin()
    base = tempfile.mkdtemp(prefix="cw_ws_e2e_repo_")
    data_root = tempfile.mkdtemp(prefix="cw_ws_e2e_data_")
    try:
        main_root = _make_repo(base, "main")
        wt_a = _add_worktree(main_root, os.path.join(base, "wt-a"), "feature/a")
        wt_b = _add_worktree(main_root, os.path.join(base, "wt-b"), "feature/b")

        manifest_path = _isolated_manifest_path(data_root)
        backup = _backup_http_manifest(manifest_path)
        proc = _spawn_isolated_daemon(bin_path, data_root)
        try:
            manifest = _wait_manifest(proc, data_root, timeout=20)
            if manifest is None:
                stderr = (proc.stderr.read(4000).decode("utf-8", "replace")
                          if proc.stderr else "")
                if "E_DAEMON_ALREADY_RUNNING" in stderr:
                    _terminate(proc)
                    pytest.skip(
                        "隔离 daemon 无法启动:生产 daemon 正持有 SID 级 instance 锁。"
                        "隔离 e2e 需在生产 daemon 停止时运行(cw daemon stop)。")
                skip_if_daemon_exited_before_manifest(
                    proc, stderr, context="隔离 daemon 未发布 manifest")
                pytest.fail(f"隔离 daemon 未发布 manifest\nstderr={stderr}")
            skip_if_isolated_daemon_pipe_conflict(proc)
            # 与 daemon 共用隔离 PKI（data_root/userhome/.callwarden/pki），
            # 客户端证书 env 指向同源 CA（w3_live 同款）；teardown 还原。
            inject_isolated_mtls_env(data_root)
            yield {
                "endpoint": manifest["endpoint"],
                "data_root": data_root,
                "base": base,
                "main": main_root,
                "wt_a": wt_a,
                "wt_b": wt_b,
            }
        finally:
            _terminate(proc)
            _restore_or_clean_http_manifest(manifest_path, proc.pid, backup)
            restore_w3_env()
    finally:
        shutil.rmtree(base, ignore_errors=True)
        shutil.rmtree(data_root, ignore_errors=True)


def _client(endpoint: str) -> HttpDaemonRpcClient:
    return HttpDaemonRpcClient(endpoint=endpoint, verify_health=False,
                               validate_manifest=False, timeout=30)


def _ensure_task_db_workspace(data_root: str, ws_id: int, name: str, root: str) -> None:
    """在隔离 daemon 的 task-DB `workspaces` 表插入/更新权威 workspace 行。

    与 tests/convergence/conftest.py 同源：Rust task handler 的 lease 依赖
    task-DB `workspaces` 表绑定，HTTP `workspace.register` 只写 daemon
    注册表（daemon_workspaces），两者必须一致否则 E_IDENTITY_NOT_WIRED。
    """
    import sqlite3

    task_db = os.path.join(data_root, "task.db")
    if not os.path.exists(task_db):
        raise RuntimeError(f"task-DB 不存在: {task_db}")
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (ws_id, name, root, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _register_checkout(endpoint: str, data_root: str, root: str, *,
                       seed_task_db: bool = True, name: str | None = None) -> dict:
    """以 checkout 自述元数据（remote + 主仓 root）注册 workspace。

    返回 register 回包（workspace_id / workspace_instance_id）。
    """
    meta = _workspace_snapshot_metadata(root)
    reg = _client(endpoint).call("workspace.register", {
        "name": name or f"ws-e2e-{uuid.uuid4().hex[:8]}",
        "client_view_root": root,
        "git_remote_url": meta.get("git_remote_url", ""),
        "git_main_root": meta.get("git_main_root", ""),
    })
    if seed_task_db:
        _ensure_task_db_workspace(
            data_root, reg["workspace_id"], reg.get("name", "ws-e2e"), root)
    return reg


def _identity(agent_id: str, role: str = "implementer") -> dict:
    return {
        "agent_id": agent_id,
        "session_id": f"sess-{agent_id}",
        "model_id": "qa-model",
        "role": role,
    }


def _register_agent(client: HttpDaemonRpcClient, agent_id: str, ws_id: int) -> None:
    client.call("agent.register", {
        "agent_id": agent_id,
        "agent_name": agent_id,
        "workspace_id": ws_id,
        **_identity(agent_id),
    })


def _create_task(client: HttpDaemonRpcClient, ws: dict, title: str) -> str:
    r = client.call("task.create", {
        "title": title,
        "workspace_id": ws["workspace_id"],
        "workspace_instance_id": ws["workspace_instance_id"],
    })
    return r["task_id"]


def _list_task_ids(client: HttpDaemonRpcClient, ws: dict) -> set:
    res = client.call("task.list", {
        "workspace_id": ws["workspace_id"],
        "workspace_instance_id": ws["workspace_instance_id"],
    })
    rows = res if isinstance(res, list) else (
        res.get("tasks", []) if isinstance(res, dict) else [])
    return {r["task_id"] for r in rows if isinstance(r, dict) and r.get("task_id")}


# ----------------------------------------------------------------------
# AC-01：三 worktree 同 instance_id
# ----------------------------------------------------------------------

class TestThreeCheckoutsConverge:
    def test_same_instance_and_workspace_id(self, ws_e2e):
        """AC-01-1：主仓+2 worktree 三客户端注册 → 同 instance_id、同 workspace 行。"""
        ep, data_root = ws_e2e["endpoint"], ws_e2e["data_root"]
        reg_main = _register_checkout(ep, data_root, ws_e2e["main"])
        reg_a = _register_checkout(ep, data_root, ws_e2e["wt_a"], seed_task_db=False)
        reg_b = _register_checkout(ep, data_root, ws_e2e["wt_b"], seed_task_db=False)

        insts = {
            reg_main["workspace_instance_id"],
            reg_a["workspace_instance_id"],
            reg_b["workspace_instance_id"],
        }
        assert reg_main["workspace_instance_id"], "register 应返回非空 instance_id"
        assert len(insts) == 1, f"三 checkout 应收敛到同一 instance_id: {insts}"

        ws_ids = {reg_main["workspace_id"], reg_a["workspace_id"], reg_b["workspace_id"]}
        assert len(ws_ids) == 1, f"三 checkout 应命中同一 registry 行: {ws_ids}"

    def test_registry_single_active_row(self, ws_e2e):
        """AC-04（registry 侧）：同 repo 三次注册后 registry 仅一条 active 行。"""
        import sqlite3

        ep, data_root = ws_e2e["endpoint"], ws_e2e["data_root"]
        reg_main = _register_checkout(ep, data_root, ws_e2e["main"])
        _register_checkout(ep, data_root, ws_e2e["wt_a"], seed_task_db=False)
        _register_checkout(ep, data_root, ws_e2e["wt_b"], seed_task_db=False)

        registry_db = os.path.join(data_root, "registry.db")
        conn = sqlite3.connect(registry_db)
        try:
            total, active = conn.execute(
                "SELECT COUNT(*), SUM(CASE WHEN status = 'active' THEN 1 ELSE 0 END) "
                "FROM daemon_workspaces WHERE workspace_instance_id = ?",
                (reg_main["workspace_instance_id"],),
            ).fetchone()
        finally:
            conn.close()
        assert total == 1, f"registry 该 instance 应只有 1 行，实际 {total}"
        assert active == 1, f"registry 该 instance 应只有 1 条 active 行，实际 {active}"

    def test_clone_same_remote_converges(self, ws_e2e):
        """AC-01-3：同 remote 不同路径 clone → 同 instance_id。"""
        ep, data_root = ws_e2e["endpoint"], ws_e2e["data_root"]
        clone_root = os.path.join(ws_e2e["base"], "clone")
        _git(ws_e2e["base"], "clone", "-q", ws_e2e["main"], clone_root)
        # 本地 clone 的 origin 是源仓路径；改为与主仓同 remote 模拟同一远端仓库
        _git(clone_root, "remote", "set-url", "origin", _FAKE_REMOTE)
        try:
            reg_clone = _register_checkout(ep, data_root, clone_root, seed_task_db=False)
            reg_main = _register_checkout(ep, data_root, ws_e2e["main"],
                                          seed_task_db=False)
            assert reg_clone["workspace_instance_id"] == reg_main["workspace_instance_id"], \
                "同 remote 的 clone 应与主仓收敛到同一 instance_id"
        finally:
            shutil.rmtree(clone_root, ignore_errors=True)

    def test_distinct_remote_distinct_instance(self, ws_e2e):
        """AC-01-2：不同 remote → 不同 instance_id。"""
        ep, data_root = ws_e2e["endpoint"], ws_e2e["data_root"]
        other = _make_repo(ws_e2e["base"], "other-remote",
                           remote="https://git.example.com/u/other.git")
        try:
            reg_other = _register_checkout(ep, data_root, other, seed_task_db=False)
            reg_main = _register_checkout(ep, data_root, ws_e2e["main"],
                                          seed_task_db=False)
            assert reg_other["workspace_instance_id"] != reg_main["workspace_instance_id"], \
                "不同 remote 应得到不同 instance_id"
        finally:
            shutil.rmtree(other, ignore_errors=True)

    def test_non_git_dir_failopen_register(self, ws_e2e):
        """AC-05：非 git 目录客户端注册成功（fail-open 身份，与 git 仓库身份不同）。"""
        ep, data_root = ws_e2e["endpoint"], ws_e2e["data_root"]
        plain = tempfile.mkdtemp(prefix="cw_ws_e2e_plain_", dir=ws_e2e["base"])
        try:
            reg_plain = _register_checkout(ep, data_root, plain, seed_task_db=False)
            assert reg_plain.get("workspace_instance_id"), \
                "非 git 目录注册应成功（fail-open）"
            reg_main = _register_checkout(ep, data_root, ws_e2e["main"],
                                          seed_task_db=False)
            assert reg_plain["workspace_instance_id"] != reg_main["workspace_instance_id"], \
                "fail-open 身份应与 git 仓库身份不同"
        finally:
            shutil.rmtree(plain, ignore_errors=True)


# ----------------------------------------------------------------------
# AC-02：跨 worktree 任务可见与 lease 互斥
# ----------------------------------------------------------------------

class TestCrossWorktreeCollaboration:
    def test_cross_worktree_task_visibility(self, ws_e2e):
        """AC-02-1/2：主仓建任务 → 任一 worktree 可见；worktree A 建任务 → 主仓/B 可见。"""
        ep, data_root = ws_e2e["endpoint"], ws_e2e["data_root"]
        reg_main = _register_checkout(ep, data_root, ws_e2e["main"])
        reg_a = _register_checkout(ep, data_root, ws_e2e["wt_a"], seed_task_db=False)
        reg_b = _register_checkout(ep, data_root, ws_e2e["wt_b"], seed_task_db=False)
        # 三 checkout 同一 workspace（前置：收敛语义）
        assert reg_a["workspace_id"] == reg_main["workspace_id"]
        assert reg_b["workspace_id"] == reg_main["workspace_id"]

        client_main, client_a, client_b = _client(ep), _client(ep), _client(ep)
        t_main = _create_task(client_main, reg_main,
                              f"E2E-MAIN-{uuid.uuid4().hex[:8]}")
        t_wta = _create_task(client_a, reg_a, f"E2E-WTA-{uuid.uuid4().hex[:8]}")

        assert t_main in _list_task_ids(client_a, reg_a), \
            "主仓建任务应对 worktree A 可见"
        assert t_main in _list_task_ids(client_b, reg_b), \
            "主仓建任务应对 worktree B 可见"
        assert t_wta in _list_task_ids(client_main, reg_main), \
            "worktree A 建任务应对主仓可见"
        assert t_wta in _list_task_ids(client_b, reg_b), \
            "worktree A 建任务应对 worktree B 可见"

    def test_cross_worktree_lease_mutex(self, ws_e2e):
        """AC-02-3/4：两 worktree 同角色争用同 task lease → 单 winner；lease.status 跨 worktree 一致。"""
        ep, data_root = ws_e2e["endpoint"], ws_e2e["data_root"]
        reg_main = _register_checkout(ep, data_root, ws_e2e["main"])
        _register_checkout(ep, data_root, ws_e2e["wt_a"], seed_task_db=False)
        _register_checkout(ep, data_root, ws_e2e["wt_b"], seed_task_db=False)

        client_a, client_b = _client(ep), _client(ep)
        _register_agent(client_a, "agent-wta", reg_main["workspace_id"])
        _register_agent(client_b, "agent-wtb", reg_main["workspace_id"])
        task_id = _create_task(_client(ep), reg_main,
                               f"E2E-LEASE-{uuid.uuid4().hex[:8]}")

        def acquire(client, agent_id):
            try:
                return ("ok", client.call("lease.acquire", {
                    "task_id": task_id,
                    "role": "implementer",
                    "ttl_seconds": 120,
                    "workspace_id": reg_main["workspace_id"],
                    "identity": _identity(agent_id),
                }))
            except DaemonRemoteError as e:
                return ("err", e.code)

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(acquire, client_a, "agent-wta")
            fb = pool.submit(acquire, client_b, "agent-wtb")
            results = [fa.result(timeout=30), fb.result(timeout=30)]

        wins = [r for r in results if r[0] == "ok"]
        losses = [r for r in results if r[0] == "err"]
        assert len(wins) == 1, f"两 worktree 争用应恰好 1 winner: {results}"
        assert len(losses) == 1, f"两 worktree 争用应恰好 1 loser: {results}"
        assert losses[0][1] in ("E_LEASE_ACTIVE_EXISTS", "E_LEASE_HOLDER_MISMATCH"), \
            f"loser 错误码应符合 lease 互斥语义: {losses[0][1]}"

        # AC-02-4：从主仓视角 lease.status 查到同一活跃 lease（跨 worktree 一致，
        # handler 返回单对象：命中 → status=active；未命中 → status=none）
        status = _client(ep).call("lease.status", {
            "task_id": task_id,
            "workspace_id": reg_main["workspace_id"],
        })
        assert isinstance(status, dict), f"lease.status 应返回单对象: {status!r}"
        assert status.get("status") == "active", \
            f"跨 worktree 查询应看到活跃 lease: {status!r}"
        winner_id = wins[0][1].get("agent_id", "") if isinstance(wins[0][1], dict) else ""
        if winner_id:
            assert status.get("agent_id") == winner_id, \
                f"lease 持有者应为争用 winner: {status.get('agent_id')} != {winner_id}"
