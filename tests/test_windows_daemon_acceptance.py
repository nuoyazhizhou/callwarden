"""Windows 命名管道 daemon 端到端自动化验收测试。

固化 T-1785841342363-296bd0d7 的手工实测（GD/D0 Windows 端点验收）：
1. cw-daemon.exe 启动后绑定 ``\\\\.\\pipe\\callwarden-<user-sid>``，不再报 UDS 错误
2. daemon_autostart / daemon_client 连接并完成 RPC（ping / health / schema.version）
3. ensure_daemon 自动拉起 daemon 并完成 RPC
4. 多进程并发读写无 ``database is locked``

仅在 Windows 且存在构建产物 cw-daemon.exe 时运行，否则 skip。
"""

import os
import subprocess
import sys
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from server.daemon_autostart import (  # noqa: E402
    ensure_daemon,
    get_default_endpoint,
    try_connect,
)
from server.daemon_client import UnixDaemonRpcClient  # noqa: E402
from callwarden.db.schema import SCHEMA_VERSION  # noqa: E402


def _daemon_binary() -> str:
    """定位 cw-daemon.exe（与 daemon_autostart._find_daemon_binary 同路径约定）。"""
    candidates = [
        os.path.join(ROOT, "rust_ext", "target", "release", "cw-daemon.exe"),
        os.path.join(ROOT, "rust_ext", "target", "debug", "cw-daemon.exe"),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return ""


DAEMON_BIN = _daemon_binary()

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or not DAEMON_BIN,
    reason="Windows 命名管道 daemon 验收仅限 Windows 且需构建 cw-daemon.exe",
)


def _wait_pipe_ready(endpoint: str, timeout: float = 10.0) -> bool:
    """有界等待窗口内轮询命名管道可连接（与 ensure_daemon 语义一致）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        conn = try_connect(endpoint)
        if conn is not None:
            conn.close()
            return True
        time.sleep(0.2)
    return False


@pytest.fixture()
def daemon(tmp_path):
    """启动独立 cw-daemon 实例（临时 registry/data，避免触碰用户级 DB）。"""
    env = dict(os.environ)
    env["CW_DAEMON_REGISTRY_DB"] = str(tmp_path / "registry.db")
    env["CW_DAEMON_DATA_ROOT"] = str(tmp_path / "data")
    err_path = tmp_path / "daemon.err.log"
    out_path = tmp_path / "daemon.out.log"
    err_file = open(err_path, "w", encoding="utf-8")
    out_file = open(out_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [DAEMON_BIN, "serve"],
        env=env,
        stdout=out_file,
        stderr=err_file,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    endpoint = get_default_endpoint()
    try:
        assert _wait_pipe_ready(endpoint), "daemon 未在有界等待窗口内就绪"
        yield {"proc": proc, "endpoint": endpoint, "err_path": err_path}
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        out_file.close()
        err_file.close()


def _read_daemon_log(err_path) -> str:
    with open(err_path, encoding="utf-8") as f:
        return f.read()


def _cw_daemon_pids() -> set:
    """返回当前 cw-daemon.exe 进程 PID 集合（用于自动拉起后的清理）。"""
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                "Get-Process cw-daemon -ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty Id",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    pids = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.add(int(line))
    return pids


def test_daemon_binds_named_pipe_and_no_uds_error(daemon):
    """验收 1：绑定 \\\\.\\pipe\\callwarden-<sid>，无 'UDS server is only available'。"""
    endpoint = daemon["endpoint"]
    assert endpoint.startswith(r"\\.\pipe\callwarden-"), f"端点非法: {endpoint}"
    assert daemon["proc"].poll() is None, "daemon 进程提前退出"
    log = _read_daemon_log(daemon["err_path"])
    assert "named pipe endpoint: named-pipe:" in log, f"未绑定命名管道: {log}"
    assert "UDS server is only available" not in log, "仍输出 UDS stub 错误"


def test_rpc_ping_health_schema_version(daemon):
    """验收 2：daemon_autostart / daemon_client 连接并完成 RPC。"""
    endpoint = daemon["endpoint"]
    client = UnixDaemonRpcClient(endpoint, timeout=10)
    for _ in range(3):
        res = client.call("ping", {})
        assert res["status"] == "ok"
        assert res["pid"] == daemon["proc"].pid, "响应 PID 不是本测试启动的 daemon"
    health = client.call("health", {})
    assert health["status"] == "ok"
    schema = client.call("schema.version", {})
    # 与 db/schema.py 动态对齐：若 Rust daemon SCHEMA_VERSION 与 Python 漂移，
    # 本断言自动失败（T-1785919930949-f5feb98c 回归守卫）
    assert schema["version"] == SCHEMA_VERSION


def test_ensure_daemon_autostart(tmp_path):
    """验收 3：daemon 未运行，ensure_daemon 自动拉起并完成 RPC。"""
    endpoint = get_default_endpoint()
    before = _cw_daemon_pids()
    env_keys = ("CW_DAEMON_REGISTRY_DB", "CW_DAEMON_DATA_ROOT")
    old = {k: os.environ.get(k) for k in env_keys}
    os.environ["CW_DAEMON_REGISTRY_DB"] = str(tmp_path / "autostart_registry.db")
    os.environ["CW_DAEMON_DATA_ROOT"] = str(tmp_path / "autostart_data")
    try:
        conn = ensure_daemon(endpoint, window=10)
        assert conn is not None, "ensure_daemon 未能连接自动拉起的 daemon"
        conn.close()
        client = UnixDaemonRpcClient(endpoint, timeout=10)
        res = client.call("ping", {})
        assert res["status"] == "ok"
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        # 仅终止本测试自动拉起的 daemon（before 快照之外的 PID）
        for pid in _cw_daemon_pids() - before:
            try:
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command", f"Stop-Process -Id {pid} -Force"],
                    capture_output=True,
                    timeout=10,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass


def test_concurrent_rpc_no_database_locked(daemon):
    """验收 4：多进程并发读写无 database is locked（写路径经 daemon 串行化点）。"""
    endpoint = daemon["endpoint"]
    worker_code = r"""
import sys
sys.path.insert(0, {root!r})
from server.daemon_autostart import ensure_daemon, get_default_endpoint
from server.daemon_client import UnixDaemonRpcClient

endpoint = {endpoint!r}
errs = []
client = UnixDaemonRpcClient(endpoint, timeout=20)
for i in range(15):
    conn = ensure_daemon(endpoint, window=5)
    if conn:
        conn.close()
    r = client.call("ping", {{}})
    if not isinstance(r, dict) or r.get("status") != "ok":
        errs.append("ping bad: %r" % (r,))
for i in range(10):
    try:
        client.call("workspace.recover", {{"workspace_instance_id": "ws-acc-%d" % i}})
    except Exception as e:
        low = str(e).lower()
        if "locked" in low or "database" in low:
            errs.append("LOCKED: %r" % (e,))
if errs:
    sys.stderr.write("\\n".join(errs))
    sys.exit(1)
sys.exit(0)
"""
    procs = []
    for _ in range(4):
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", worker_code.format(root=ROOT, endpoint=endpoint)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        )
    for proc in procs:
        out, err = proc.communicate(timeout=90)
        assert proc.returncode == 0, (
            f"并发 worker 失败:\nstdout={out.decode(errors='replace')}\n"
            f"stderr={err.decode(errors='replace')}"
        )
