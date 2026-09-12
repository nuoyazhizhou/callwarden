"""H2I 真实进程级 HTTP 集成门（H1/H2 closed 之后、H3 之前的唯一真实进程 gate）。

对应 docs/design/http-daemon-mvp-task-plan.md §H2I 与
docs/design/http-daemon-mvp-compatibility-contract.md（frozen §2.1/§4/§9）：

- current-HEAD 真实 cw-daemon（优先 rust_ext/target/debug 构建，兜底 runtime）
- production DaemonClient：CW_DAEMON_TRANSPORT=http 时 get_daemon_client() 返回
  HttpDaemonRpcClient（不含业务 SQL、不回退 SQLite/Named Pipe/UDS）
- authority-scoped manifest 完整发现（manifest_path）+ /health 交叉核对（PID/schema）
- health / capabilities / 真实 RPC（ping、task.create）
- 结构化业务错误透传（method_not_found，HTTP 200 + error.data.code）
- 超时 fail-closed（E_HTTP_REQUEST_TIMEOUT）
- mutation dedup（同 request_id 重放返回原结果；异 params E_REQUEST_ID_REUSE_MISMATCH）
- daemon restart 后 dedup 持久化；daemon 停止后 fail-closed（不回退 SQLite）
- loopback only + HTTP client 不含 SQLite fallback

daemon 二进制不可用时 skip（CI 无 MSVC 工具链时不影响其他测试）。
"""

import json
import os
import socket
import sqlite3
import subprocess
import time

import pytest

from callwarden.config import HTTP_MVP_TRANSPORT_PROFILE
from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    HttpDaemonRpcClient,
)
from callwarden.server.daemon_protocol import DaemonRemoteError


# ----------------------------------------------------------------------
# 辅助：定位 / 启动 / 等待 真实 cw-daemon（与 H1 transport 测试同源）
# ----------------------------------------------------------------------

def _find_daemon_binary():
    """定位 current-HEAD 构建的 cw-daemon 二进制。

    优先本地 cargo build 产物（rust_ext/target/debug），保证与当前源码一致；
    CW_DAEMON_BIN / runtime 部署仅作兜底。
    """
    candidates = [
        os.path.join("rust_ext", "target", "debug", "cw-daemon.exe"),
        os.path.join("rust_ext", "target", "debug", "cw-daemon"),
        os.environ.get("CW_DAEMON_BIN", ""),
        os.path.join("runtime", "current", "cw-daemon.exe"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return os.path.abspath(c)
    return None


def _wait_manifest(data_root, proc, timeout=10.0):
    """等待隔离 daemon 发布 authority-scoped manifest。

    只接受 pid 匹配当前进程的 manifest，避免 restart 场景读到上一个 daemon
    遗留的旧 manifest（endpoint 指向已停止的端口）。

    harness 依据（W3 / A 桶统一）：H6 修复（9d6ca63）后 daemon 把 manifest
    固定写到 `USERPROFILE/.callwarden`（http_manifest_dir，见 http_server.rs:1292）。
    本 harness 将 daemon 的 `USERPROFILE` 重定向到 `data_root/userhome`
    （见 `_spawn_isolated_daemon`），故 manifest 落在
    `data_root/userhome/.callwarden`。该位置与 conftest 隔离的
    `CALLWARDEN_DIR`（tmp 目录）及真实 USERPROFILE 都解耦，稳定可查。
    """
    manifest_dir = os.path.join(data_root, "userhome", ".callwarden")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isdir(manifest_dir):
            for f in os.listdir(manifest_dir):
                if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                    p = os.path.join(manifest_dir, f)
                    try:
                        m = json.loads(open(p, encoding="utf-8").read())
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 命名管道 / USERPROFILE）。返回 (proc, env)。

    通过 `--http-bind=<spec>`（等号格式）启用 HTTP transport。

    harness 依据（W3 / A 桶统一）：H6 后 manifest 落 `USERPROFILE/.callwarden`。
    这里把 daemon 的 `USERPROFILE` 重定向到 `data_root/userhome`（并预建
    `.callwarden`，否则 daemon 发布 manifest 落盘失败），使 manifest 稳定落在
    隔离 data_root 下，与 conftest 隔离的 CALLWARDEN_DIR 及真实 USERPROFILE
    完全解耦（对齐 test_http_combined_worker_cutover.py 模式 A）。
    """
    task_db = os.path.join(data_root, "task.db")
    registry_db = os.path.join(data_root, "registry.db")
    socket_path = os.path.join(data_root, "pipe")
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = task_db
    env["CW_DAEMON_REGISTRY_DB"] = registry_db
    env["CW_DAEMON_SOCKET"] = socket_path
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    home_dir = os.path.join(data_root, "userhome")
    os.makedirs(os.path.join(home_dir, ".callwarden"), exist_ok=True)
    env["USERPROFILE"] = home_dir
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc, env


def _terminate(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _manifest_path_in(data_root):
    """返回隔离 daemon 发布的 authority-scoped manifest 路径。

    harness 依据（W3 / A 桶统一）：H6 后 manifest 落
    `USERPROFILE/.callwarden`（本 harness 重定向到 `data_root/userhome`），
    与 `_wait_manifest` 保持一致，返回 `data_root/userhome/.callwarden` 下的
    manifest 路径。
    """
    manifest_dir = os.path.join(data_root, "userhome", ".callwarden")
    names = [
        f for f in os.listdir(manifest_dir)
        if f.startswith("http-daemon.") and f.endswith(".manifest.json")
    ]
    assert names, "未发现 authority-scoped manifest"
    return os.path.join(manifest_dir, names[0])


def _start_blackhole_socket():
    """黑洞 TCP listener：接受连接但不响应，用于确定性触发读超时。"""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _ensure_workspace(client, root_path):
    """经 workspace.register 建立真实 workspace 实例，返回 workspace_instance_id。

    RP-09 governance（fail-closed）后 `task.create` 必传 `workspace_id>0`
    + 非空 `workspace_instance_id`；workspace.register 校验 client_view_root
    真实存在。参考 test_http_combined_worker_cutover.py._spawn_with_client。
    """
    os.makedirs(root_path, exist_ok=True)
    ws = client.call("workspace.register", {"client_view_root": root_path})
    return ws["workspace_instance_id"]


def _seed_task_db_workspace(task_db_path, workspace_id=1):
    """把 workspace 种进隔离 task-DB 的 `workspaces` 表（方案A 双库对齐）。

    双库 authority 分裂根因：`workspace.register` 只写 registry 库
    （`daemon_workspaces`），从不写隔离 task-DB（`callwarden.db`）；而
    `task.create` 的 `resolve_create_authority`（workspace_reconciliation.rs）
    只查 task-DB。首次 create 时 captures 为空（第2/3步 miss），必须命中
    第4步「task-DB `workspaces` 表含请求 id → 允许建立首个 capture」才不 fall
    to 第5步 mismatch。

    生产里 workspace 接入 task-DB 由外部/migrate 流程完成；本 harness 在
    隔离 task-DB 种一行 id，使 `task.create` 经第4步**合法建立权威 capture**，
    而非绕过 fail-closed。这是设计允许的对齐通道，不是随意 inject。
    """
    conn = sqlite3.connect(task_db_path, timeout=10)
    try:
        conn.execute(
            "INSERT OR IGNORE INTO workspaces (id, name, root_path, created_at, is_active) \
             VALUES (?1, ?2, ?3, ?4, 1)",
            (workspace_id, f"ws-{workspace_id}", "/seed-repo", time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def _make_http_client(endpoint, manifest_path=None, verify_health=True, timeout=10.0):
    """构造 production 类 HTTP client（含 manifest 完整校验 + /health 交叉核对）。"""
    return HttpDaemonRpcClient(
        endpoint=endpoint,
        manifest_path=manifest_path,
        verify_health=verify_health,
        timeout=timeout,
    )


# ----------------------------------------------------------------------
# fixtures
# ----------------------------------------------------------------------

@pytest.fixture
def isolated_daemon(tmp_path):
    """启动隔离真实 daemon，yield (endpoint, manifest, data_root, bin_path, proc)。"""
    bin_path = _find_daemon_binary()
    if bin_path is None:
        pytest.skip("cw-daemon 二进制不可用（需先 cargo build --bin cw-daemon）")

    data_root = str(tmp_path / "data")
    os.makedirs(data_root, exist_ok=True)
    proc, _ = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
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
                f"returncode={proc.poll()}\nfiles={os.listdir(data_root)}\n"
                f"stderr={err.decode('utf-8', 'replace')}"
            )
        endpoint = manifest["endpoint"]
        assert endpoint.startswith("http://127.0.0.1:"), f"非 loopback endpoint: {endpoint}"
        yield endpoint, manifest, data_root, bin_path, proc
    finally:
        _terminate(proc)


@pytest.fixture
def http_client(isolated_daemon):
    """经隔离 manifest 完整发现的 production 类 HTTP client（verify_health=True）。

    yield (client, data_root)：data_root 供方案A seed 隔离 task-DB 的
    `workspaces` 表（`task.create` 双库 authority 对齐前置）。
    """
    endpoint, manifest, data_root, _, _ = isolated_daemon
    client = _make_http_client(endpoint, manifest_path=_manifest_path_in(data_root))
    yield client, data_root
    client._resolved_endpoint = None  # 释放解析缓存，避免跨测试污染


# ----------------------------------------------------------------------
# step #0 real_process_roundtrip：current-HEAD daemon + production client
# ----------------------------------------------------------------------

def test_production_factory_selects_http_and_real_ping(isolated_daemon, monkeypatch):
    """H2I P0：production DaemonClient 在 CW_DAEMON_TRANSPORT=http 时返回 HTTP client，
    并对真实 daemon 完成 /health + RPC 往返。"""
    from callwarden.server.daemon_client import get_daemon_client

    endpoint, manifest, data_root, _, _ = isolated_daemon
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "http")
    # 让 production 单例发现隔离 daemon 的 endpoint（manifest 可选路径）
    monkeypatch.setattr("callwarden.config.get_http_daemon_endpoint", lambda: endpoint)
    HttpDaemonRpcClient.reset_instance()
    try:
        client = get_daemon_client()
        assert client.is_http_client is True
        assert isinstance(client, HttpDaemonRpcClient)
        # 真实进程 RPC 往返
        result = client.call("ping", {})
        assert isinstance(result, dict)
        # 真实 /health 与 manifest PID/schema 一致
        health = client.health()
        assert health["pid"] == manifest["pid"]
        assert health["schema_version"] == manifest["schema_version"]
        assert health["security_profile"] == HTTP_MVP_TRANSPORT_PROFILE
    finally:
        HttpDaemonRpcClient.reset_instance()


def test_manifest_discovery_and_health_cross_check(http_client, isolated_daemon):
    """manifest_path 完整发现 + /health 交叉核对（真实 PID/schema 一致性）。"""
    client, _ = http_client
    endpoint, manifest, data_root, _, _ = isolated_daemon
    assert client.discover() == endpoint.rstrip("/")
    health = client.verify_health()
    # /health 与 manifest 交叉核对点（真实 daemon /health 无 manifest_id，仅 PID/schema）
    assert health["pid"] == manifest["pid"]
    assert health["schema_version"] == manifest["schema_version"]
    assert health["security_profile"] == HTTP_MVP_TRANSPORT_PROFILE

    caps = client.capabilities()
    assert caps["server_mode"] == HTTP_MVP_TRANSPORT_PROFILE
    assert caps["methods"]["ping"]["status"] == "available"
    assert caps["methods"]["ping"]["backend"] == "rust_native"


def test_real_rpc_task_create(http_client, tmp_path):
    """真实业务 RPC：task.create 返回结构化结果（隔离 task DB，不触碰权威库）。

    RP-09 governance + 方案A 双库对齐：先 workspace.register（registry 侧），
    再把 workspace 种进隔离 task-DB `workspaces` 表，然后携带 workspace_id +
    真实 instance 创建任务（使 `resolve_create_authority` 命中第4步合法建 capture）。
    """
    client, data_root = http_client
    inst = _ensure_workspace(client, str(tmp_path / "seed-repo"))
    _seed_task_db_workspace(os.path.join(data_root, "task.db"))
    result = client.call(
        "task.create",
        {
            "title": "h2i-roundtrip",
            "description": "",
            "steps": [],
            "creator": "h2i-tester",
            "workspace_id": 1,
            "workspace_instance_id": inst,
        },
    )
    assert isinstance(result, dict)


def test_structured_error_method_not_found(http_client):
    """HTTP 200 业务错误透传：未知 method → DaemonRemoteError(code=method_not_found)。"""
    client, _ = http_client
    with pytest.raises(DaemonRemoteError) as exc:
        client.call("this.method.does.not.exist", {})
    assert exc.value.code == "method_not_found"


def test_mutation_dedup_real_daemon(http_client, tmp_path):
    """mutation dedup：同 request_id 重放返回原结果；异 params → E_REQUEST_ID_REUSE_MISMATCH。"""
    client, data_root = http_client
    inst = _ensure_workspace(client, str(tmp_path / "seed-repo"))
    _seed_task_db_workspace(os.path.join(data_root, "task.db"))
    params = {
        "title": "h2i-dedup",
        "description": "",
        "steps": [],
        "creator": "h2i-tester",
        "workspace_id": 1,
        "workspace_instance_id": inst,
    }
    rid = "h2i-dedup-rid-1"

    first = client.call("task.create", params, request_id=rid)
    replay = client.call("task.create", params, request_id=rid)
    assert first == replay, "相同 request_id 重放必须返回原结果"

    other = dict(params, title="h2i-dedup-different")
    with pytest.raises(DaemonRemoteError) as exc:
        client.call("task.create", other, request_id=rid)
    assert exc.value.code == "E_REQUEST_ID_REUSE_MISMATCH"


def test_timeout_fails_closed():
    """真实传输超时 → E_HTTP_REQUEST_TIMEOUT（fail-closed，不降级）。"""
    srv, port = _start_blackhole_socket()
    try:
        client = _make_http_client(
            endpoint=f"http://127.0.0.1:{port}",
            verify_health=False,
            timeout=0.5,
        )
        with pytest.raises(DaemonUnavailableError) as exc:
            client.call("ping", {})
        assert "E_HTTP_REQUEST_TIMEOUT" in str(exc.value)
    finally:
        srv.close()


def test_loopback_only_and_no_sqlite_fallback(http_client, isolated_daemon):
    """loopback only + HTTP client 结构上不含 SQLite fallback + 连接失败 fail-closed。"""
    endpoint, manifest, data_root, _, _ = isolated_daemon
    assert endpoint.startswith("http://127.0.0.1:"), f"非 loopback endpoint: {endpoint}"
    # 结构上无 SQL/服务 fallback 入口（frozen contract §9 负向矩阵）
    assert not hasattr(http_client, "_get_db")
    assert not hasattr(http_client, "_svc")

    # 连接失败必须 fail-closed，而非回退 SQLite/Named Pipe/UDS
    dead = _make_http_client(
        endpoint="http://127.0.0.1:1",
        verify_health=False,
        timeout=2.0,
    )
    with pytest.raises(DaemonUnavailableError):
        dead.call("ping", {})


def test_nonloopback_bind_rejected(tmp_path):
    """非 loopback bind 必须 fail-closed：进程退出非零且不发布 manifest。"""
    bin_path = _find_daemon_binary()
    if bin_path is None:
        pytest.skip("cw-daemon 二进制不可用")
    data_root = str(tmp_path / "data-nonloopback")
    os.makedirs(data_root, exist_ok=True)
    proc, _ = _spawn_isolated_daemon(bin_path, data_root, "0.0.0.0:0")
    try:
        rc = proc.wait(timeout=15)
    except Exception:
        _terminate(proc)
        rc = proc.returncode
    assert rc != 0, "非 loopback bind 必须拒绝"
    manifest_dir = os.path.join(data_root, "userhome", ".callwarden")
    manifests = (
        [f for f in os.listdir(manifest_dir) if f.startswith("http-daemon.")]
        if os.path.isdir(manifest_dir)
        else []
    )
    assert manifests == [], "非 loopback bind 不得发布 manifest"


# ----------------------------------------------------------------------
# step #1 restart_and_no_fallback
# ----------------------------------------------------------------------

def test_restart_dedup_persist(tmp_path):
    """daemon restart 后 dedup 持久化：同 request_id 重放仍返回原结果。"""
    bin_path = _find_daemon_binary()
    if bin_path is None:
        pytest.skip("cw-daemon 二进制不可用")
    data_root = str(tmp_path / "data-restart")
    os.makedirs(data_root, exist_ok=True)
    root_path = str(tmp_path / "seed-repo")
    task_db_path = os.path.join(data_root, "task.db")

    params_template = {
        "title": "h2i-restart-dedup",
        "description": "",
        "steps": [],
        "creator": "h2i-tester",
        "workspace_id": 1,
    }
    rid = "h2i-restart-rid-1"

    # 第一次启动：register 拿真实 instance + 种 task-DB workspaces，再发 mutation
    proc1, _ = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
    try:
        m1 = _wait_manifest(data_root, proc1)
        assert m1 is not None, "第一次启动未发布 manifest"
        c1 = _make_http_client(m1["endpoint"], manifest_path=_manifest_path_in(data_root))
        inst = _ensure_workspace(c1, root_path)
        _seed_task_db_workspace(task_db_path)
        first = c1.call("task.create", dict(params_template, workspace_instance_id=inst), request_id=rid)
    finally:
        _terminate(proc1)

    # 重启（同 data_root → task DB + workspace binding + capture + dedup sqlite 持久）
    proc2, _ = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
    try:
        m2 = _wait_manifest(data_root, proc2)
        assert m2 is not None, "重启后未发布 manifest"
        c2 = _make_http_client(m2["endpoint"], manifest_path=_manifest_path_in(data_root))
        replay = c2.call("task.create", dict(params_template, workspace_instance_id=inst), request_id=rid)
        assert first == replay, "跨 restart dedup 必须返回原结果"
    finally:
        _terminate(proc2)


def test_no_fallback_after_daemon_stop(isolated_daemon):
    """daemon 停止后 HTTP client fail-closed（E_HTTP_DAEMON_UNAVAILABLE），
    不回退 SQLite/Named Pipe/UDS（frozen contract §2.1 no-fallback）。"""
    endpoint, manifest, data_root, _, proc = isolated_daemon
    client = _make_http_client(endpoint, verify_health=False, timeout=2.0)
    # 停止前：真实 RPC 成功
    assert isinstance(client.call("ping", {}), dict)
    # 主动停止 daemon 进程
    _terminate(proc)
    # 停止后：fail-closed，绝不降级。
    # Windows 上进程被强制终止后新连接可能被内核静默丢弃（超时）或立即拒绝，
    # 两种错误码都属于 DaemonUnavailableError 语义，代表"HTTP profile 不可达即失败"。
    with pytest.raises(DaemonUnavailableError) as exc:
        client.call("ping", {})
    err = str(exc.value)
    assert err.startswith("E_HTTP_DAEMON_UNAVAILABLE") or err.startswith(
        "E_HTTP_REQUEST_TIMEOUT"
    ), err
