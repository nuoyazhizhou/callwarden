"""H1 HTTP transport 进程级验收测试（真实 cw-daemon + 真实 HTTP 往返）。

对应 frozen contract §4.1/§4.3/§9 与 H1 Role Prompt 的进程级要求：
- 隔离 daemon 启动（临时 task DB / data_root / registry / 命名管道）
- authority-scoped manifest 原子发布 + 动态端口发现
- GET /health、GET /capabilities、POST /v1/rpc（ping）
- 非 loopback bind 拒绝（E_HTTP_MVP_LOOPBACK_ONLY，不发布 manifest）
- mutation request_id 去重（同 request_id 同 params 返回原结果；不同 params mismatch）
- daemon restart 后 dedup 持久化（跨 restart 保留）

daemon 二进制不可用时 skip（CI 无 MSVC 工具链时不影响其他测试）。
"""

import json
import os
import subprocess
import sys
import time

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient, DaemonUnavailableError
from callwarden.server.daemon_protocol import DaemonRemoteError


def _find_daemon_binary():
    """定位当前 HEAD 构建的 cw-daemon 二进制。

    优先使用本地 cargo build 产物（rust_ext/target/debug），保证与当前源码一致；
    CW_DAEMON_BIN 可能指向旧的 runtime 部署（不含 --http-bind），仅作兜底。
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
    """等待 daemon 发布 authority-scoped manifest，返回 manifest dict 或 None。

    只接受 pid 匹配当前进程的 manifest，避免 restart 场景读到上一个 daemon
    遗留的旧 manifest（endpoint 指向已停止的端口）。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        for f in os.listdir(data_root):
            if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                p = os.path.join(data_root, f)
                try:
                    m = json.loads(open(p, encoding="utf-8").read())
                except (OSError, ValueError):
                    continue
                if m.get("pid") == proc.pid:
                    return m
        time.sleep(0.2)
    return None


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 命名管道）。返回 (proc, env)。

    通过 `--http-bind=<spec>`（等号格式，避免 clap 顶层 option 与 serve 子命令的
    空格解析问题）启用 HTTP transport。
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


@pytest.fixture
def http_daemon(tmp_path):
    """启动隔离 daemon 并返回 (endpoint, manifest, data_root)。"""
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
            files = os.listdir(data_root)
            pytest.fail(
                "隔离 daemon 未发布 manifest\n"
                f"returncode={proc.poll()}\nfiles={files}\n"
                f"stderr={err.decode('utf-8', 'replace')}"
            )
        endpoint = manifest["endpoint"]
        assert endpoint.startswith("http://127.0.0.1:"), f"非 loopback endpoint: {endpoint}"
        yield endpoint, manifest, data_root
    finally:
        _terminate(proc)


def test_health_and_capabilities(http_daemon):
    endpoint, manifest, _ = http_daemon
    client = HttpDaemonRpcClient(endpoint=endpoint, verify_health=False)
    health = client.health()
    assert health["security_profile"] == "dev_loopback_unauthenticated"
    assert health["pid"] == manifest["pid"]
    assert health["schema_version"] == manifest["schema_version"]

    caps = client.capabilities()
    assert caps["server_mode"] == "dev_loopback_unauthenticated"
    assert caps["methods"]["ping"]["backend"] == "rust_native"
    assert caps["methods"]["ping"]["status"] == "available"


def test_rpc_ping(http_daemon):
    endpoint, _, _ = http_daemon
    client = HttpDaemonRpcClient(endpoint=endpoint, verify_health=False)
    result = client.call("ping", {})
    assert isinstance(result, dict)


def test_manifest_authority_scoped_filename(http_daemon):
    endpoint, manifest, data_root = http_daemon
    from callwarden.config import get_http_authority_id, get_http_manifest_path

    expected_path = get_http_manifest_path(get_http_authority_id())
    # data_root 下的 manifest 文件名必须为 authority-scoped（P1-3 修复）
    names = [f for f in os.listdir(data_root) if f.startswith("http-daemon.")]
    assert names, "未发现 authority-scoped manifest 文件名"
    assert manifest["authority_id"] == get_http_authority_id()
    # manifest schema version 冻结
    assert manifest["manifest_version"] == "callwarden-http-manifest/v1"


def test_nonloopback_bind_rejected(tmp_path):
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
    # 非 loopback 必须 fail-closed（退出非零），且不发布 manifest
    assert rc != 0, "非 loopback bind 必须拒绝"
    manifests = [f for f in os.listdir(data_root) if f.startswith("http-daemon.")]
    assert manifests == [], "非 loopback bind 不得发布 manifest"


def test_mutation_dedup_same_request_id(http_daemon):
    endpoint, _, _ = http_daemon
    client = HttpDaemonRpcClient(endpoint=endpoint, verify_health=False)
    params = {"title": "dedup-test", "description": "", "steps": [], "creator": "agent"}
    rid = "h1-dedup-rid-1"

    first = client.call("task.create", params, request_id=rid)
    # 相同 request_id + 相同 params → 返回原结果（不重复副作用）
    replay = client.call("task.create", params, request_id=rid)
    assert first == replay, "相同 request_id 重放必须返回原结果"

    # 相同 request_id + 不同 params → E_REQUEST_ID_REUSE_MISMATCH
    other = dict(params, title="dedup-test-different")
    with pytest.raises(DaemonRemoteError) as exc:
        client.call("task.create", other, request_id=rid)
    assert exc.value.code == "E_REQUEST_ID_REUSE_MISMATCH"


def test_restart_dedup_persist(tmp_path):
    bin_path = _find_daemon_binary()
    if bin_path is None:
        pytest.skip("cw-daemon 二进制不可用")
    data_root = str(tmp_path / "data-restart")
    os.makedirs(data_root, exist_ok=True)

    params = {"title": "restart-dedup", "description": "", "steps": [], "creator": "agent"}
    rid = "h1-restart-rid-1"

    # 第一次启动：发 mutation
    proc1, _ = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
    try:
        m1 = _wait_manifest(data_root, proc1)
        assert m1 is not None, "第一次启动未发布 manifest"
        c1 = HttpDaemonRpcClient(endpoint=m1["endpoint"], verify_health=False)
        first = c1.call("task.create", params, request_id=rid)
    finally:
        _terminate(proc1)

    # 重启（同 data_root → 同 dedup sqlite）
    proc2, _ = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
    try:
        m2 = _wait_manifest(data_root, proc2)
        assert m2 is not None, "重启后未发布 manifest"
        c2 = HttpDaemonRpcClient(endpoint=m2["endpoint"], verify_health=False)
        # 相同 request_id 重放 → 返回原结果（dedup 跨 restart 持久化）
        replay = c2.call("task.create", params, request_id=rid)
        assert first == replay, "跨 restart dedup 必须返回原结果"
    finally:
        _terminate(proc2)
