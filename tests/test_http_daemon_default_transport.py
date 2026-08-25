"""H6 默认态 HTTP transport 测试：默认 HTTP / manifest / 探针去重 / named-pipe 回落 / RPC round-trip。

对应 docs/design/http-daemon-mvp-task-plan.md §H6 与
docs/design/http-daemon-mvp-compatibility-contract.md §2.2（迁移期临时例外，
2026-08-15）：未显式指定 transport 时默认启用 HTTP（loopback 动态端口），
显式指定 named-pipe/uds/windows-bridge/cli-bridge 时回落旧通道。

覆盖 4 项检查项：

1. **Python 侧默认 transport 配置**（纯单元测试，不依赖 daemon）：
   `is_http_transport_enabled()` 未显式设置 / 为 http / auto → True；
   显式 named-pipe / uds / windows-bridge / cli-bridge → False；
   `get_daemon_transport()` 白名单含 http（bogus → auto）；
   `get_daemon_client()` 默认返回 `HttpDaemonRpcClient`（is_http_client=True）。
2. **Rust daemon 默认态 HTTP**（隔离 daemon，不传 --http-bind、不设 transport env）：
   默认启用 HTTP server（loopback 动态端口）+ authority-scoped manifest 写入
   `~/.callwarden/`；/health 交叉核对；`ping` RPC 报告 transport=http；
   HTTP RPC 只读 round-trip 成功。
   —— 依赖含 H6 改动的 cw-daemon 二进制；旧二进制（无 H6）默认态不发布
   HTTP manifest，测试 skip 附诊断（步骤 #6 fresh runtime 部署后重跑即全绿）。
3. **探针去重原语**：`DaemonMutex` 同 endpoint 跨线程互斥（并发启动不产生
   双实例的核心原语，AGENTS.md 规则 34 单写点）。
4. **显式 named-pipe 回落**：`CW_DAEMON_TRANSPORT=named-pipe` 时 Rust daemon
   HTTP 不启用（无 manifest 发布），Python 侧回落 legacy DaemonClient。

注意（H6 修复后 manifest 固定写入 `~/.callwarden/`，与 data_root 无关）：
- 隔离 daemon 的 manifest 会落在真实 `~/.callwarden/`，teardown 终止隔离
  daemon 并清理 pid 匹配的 manifest（有生产备份时恢复），避免污染生产 HTTP 发现。
- Windows 下隔离 daemon 的 named-pipe 名恒为 pipe 前缀 + callwarden-<SID>
  （transport_windows 按 owner SID 派生，--socket 参数在 Windows 被忽略），
  与生产 daemon 共存秒级（CreateNamedPipe 多实例兼容）；测试不执行业务 RPC。

测试自包含、不依赖临时手工状态；失败给出可诊断信息；不 mock 真实断言。
"""

import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time
import uuid

import pytest

# 仓库根加入 sys.path（支持 `server.*` 与 `callwarden.server.*` 两种 import）
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from callwarden.config import (  # noqa: E402
    HTTP_MANIFEST_SCHEMA_VERSION,
    HTTP_MVP_TRANSPORT_PROFILE,
    get_daemon_transport,
    get_http_authority_id,
    get_http_manifest_dir,
    get_http_manifest_path,
    is_http_transport_enabled,
)
from callwarden.server.daemon_autostart import _pid_alive  # noqa: E402
from callwarden.server.daemon_client import (  # noqa: E402
    DaemonClient,
    DaemonUnavailableError,
    E_HTTP_REQUEST_TIMEOUT,
    HttpDaemonRpcClient,
    get_daemon_client,
)
from callwarden.server.daemon_mutex import DaemonMutex  # noqa: E402
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402

_RUNTIME_ROOT = os.path.join(os.path.expanduser("~"), ".callwarden", "runtime")
_CURRENT_DAEMON = os.path.join(_RUNTIME_ROOT, "current", "cw-daemon.exe")

# 预期 schema_version（与 release acceptance 一致）
_SCHEMA_VERSION = 60


def _sha256(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _spawn_isolated_daemon(bin_path, data_root, extra_args=None, extra_env=None):
    """启动隔离 daemon（临时 task DB / registry / 管道）。

    - 默认态（不传 extra_env）：删除继承的 CW_DAEMON_TRANSPORT /
      CW_DAEMON_HTTP_BIND，使 daemon 处于 H6 默认态（HTTP 默认启用）。
    - 显式回落（extra_env={"CW_DAEMON_TRANSPORT": "named-pipe"}）覆盖之。
    - 始终传 `--socket <tmp>/pipe` 隔离 UDS 路径（Windows 下 pipe 名仍按
      SID 派生，此处仅为 Unix 分支与启动参数一致性）。
    """
    env = os.environ.copy()
    env.pop("CW_DAEMON_TRANSPORT", None)   # H6 默认态：不允许外部 env 污染
    env.pop("CW_DAEMON_HTTP_BIND", None)
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CW_COMPAT_PYTHON"] = sys.executable
    if extra_env:
        env.update(extra_env)
    args = [bin_path] + (extra_args or [])
    return subprocess.Popen(
        args,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _scan_manifest_for_pid(pid, directory=None):
    """在 manifest 目录扫描 pid 匹配的 HTTP manifest；无则返回 None。"""
    directory = directory or get_http_manifest_dir()
    if not os.path.isdir(directory):
        return None
    for name in os.listdir(directory):
        if name.startswith("http-daemon.") and name.endswith(".manifest.json"):
            p = os.path.join(directory, name)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                continue
            if int(m.get("pid", -1)) == pid:
                return m
    return None


def _wait_manifest_for_pid(pid, timeout=8.0):
    """等待 pid 匹配的 HTTP manifest 出现（H6 后固定写 `~/.callwarden/`）。"""
    directory = get_http_manifest_dir()
    deadline = time.time() + timeout
    while time.time() < deadline:
        m = _scan_manifest_for_pid(pid, directory)
        if m is not None:
            return m
        time.sleep(0.2)
    return None


def _proc_diag(proc, nbytes=4000) -> str:
    """读取隔离 daemon 的 stdout/stderr 尾部（诊断用）。"""
    parts = []
    for stream in (proc.stdout, proc.stderr):
        if stream is None:
            continue
        try:
            data = stream.read(nbytes)
            parts.append(data.decode("utf-8", "replace"))
        except Exception as exc:  # noqa: BLE001
            parts.append(f"<读取失败: {exc}>")
    return "\n".join(parts)


def _terminate(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _backup_http_manifest():
    """备份当前 authority 的 HTTP manifest（若存在），teardown 时恢复。"""
    path = get_http_manifest_path()
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data


def _restore_or_clean_http_manifest(pid, backup):
    """teardown 清理：删除 pid 匹配的隔离 manifest；备份 pid 存活则恢复。"""
    path = get_http_manifest_path()
    try:
        if os.path.isfile(path):
            with open(path, "r", encoding="utf-8") as f:
                current = json.load(f)
            if int(current.get("pid", -1)) == pid:
                os.remove(path)
    except (OSError, ValueError):
        pass
    if backup is not None and _pid_alive(int(backup.get("pid", -1))):
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(backup, f, ensure_ascii=False)
        except OSError:
            pass


# ============================================================
# 1. Python 侧默认 transport 配置（纯单元测试，不依赖 daemon）
# ============================================================


class TestPythonDefaultTransportConfig:
    def test_default_enabled_without_env(self, monkeypatch):
        monkeypatch.delenv("CW_DAEMON_TRANSPORT", raising=False)
        assert is_http_transport_enabled() is True

    def test_http_auto_explicit_enabled(self, monkeypatch):
        for v in ("http", "auto", "HTTP", " http "):
            monkeypatch.setenv("CW_DAEMON_TRANSPORT", v)
            assert is_http_transport_enabled() is True

    def test_explicit_other_transport_disabled(self, monkeypatch):
        for v in ("named-pipe", "uds", "windows-bridge", "cli-bridge"):
            monkeypatch.setenv("CW_DAEMON_TRANSPORT", v)
            assert is_http_transport_enabled() is False

    def test_get_daemon_transport_whitelist_contains_http(self, monkeypatch):
        monkeypatch.setenv("CW_DAEMON_TRANSPORT", "http")
        assert get_daemon_transport() == "http"

    def test_get_daemon_transport_bogus_falls_back_auto(self, monkeypatch):
        monkeypatch.setenv("CW_DAEMON_TRANSPORT", "bogus")
        assert get_daemon_transport() == "auto"

    def test_get_daemon_client_default_returns_http_client(self, monkeypatch):
        monkeypatch.delenv("CW_DAEMON_TRANSPORT", raising=False)
        try:
            client = get_daemon_client()
            assert client.is_http_client is True
            assert isinstance(client, HttpDaemonRpcClient)
        finally:
            HttpDaemonRpcClient.reset_instance()

    def test_get_daemon_client_named_pipe_returns_legacy(self, monkeypatch):
        monkeypatch.setenv("CW_DAEMON_TRANSPORT", "named-pipe")
        try:
            client = get_daemon_client()
            assert client.is_http_client is False
            assert isinstance(client, DaemonClient)
        finally:
            DaemonClient.reset_instance()


# ============================================================
# 2. Rust daemon 默认态 HTTP（隔离 daemon，不传 --http-bind 不设 transport env）
# ============================================================


def _warm_worker(proc, client, retries=2):
    """compat worker 冷启动就绪探测（首个 python_compat 调用可能慢）。

    W2-1（T-1786840097330-dec66710）：get_uncommented_symbols 已迁移
    rust_native，预热改用仍走 compat worker 的 stats_top_files。
    """
    last_err = None
    for _ in range(retries + 1):
        try:
            client.call("stats_top_files", {"workspace_id": 1, "limit": 1})
            return
        except DaemonUnavailableError as e:
            if E_HTTP_REQUEST_TIMEOUT not in str(e):
                raise
            last_err = e
        except DaemonRemoteError as e:
            if e.code == "method_not_found":
                raise
            return
    _terminate(proc)
    pytest.fail(f"compat worker 冷启动就绪超时: {last_err}")


class TestDaemonDefaultHttpTransport:
    """默认态（无 --http-bind、无 transport env）隔离 daemon 的 HTTP 行为。

    依赖 runtime/current 二进制含 H6 改动；旧二进制默认态不发布 HTTP manifest，
    此时 skip 附诊断（步骤 #6 fresh runtime 部署后重跑即全绿）。
    """

    @pytest.fixture
    def isolated_default_http_daemon(self, tmp_path):
        if not os.path.isfile(_CURRENT_DAEMON):
            pytest.skip(f"runtime/current/cw-daemon.exe 不存在: {_CURRENT_DAEMON}")
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        backup = _backup_http_manifest()
        proc = _spawn_isolated_daemon(
            _CURRENT_DAEMON, data_root,
            extra_args=["--socket", os.path.join(data_root, "pipe")],
        )
        try:
            manifest = _wait_manifest_for_pid(proc.pid)
            if manifest is None:
                if proc.poll() is not None:
                    pytest.fail(
                        "隔离 daemon 启动失败（默认态 HTTP 未发布 manifest）\n"
                        f"{_proc_diag(proc)}"
                    )
                pytest.skip(
                    "runtime/current cw-daemon.exe 不含 H6 默认态 HTTP 改动"
                    "（默认态未发布 HTTP manifest）；"
                    "需先执行步骤 #6 refresh_shared_runtime.ps1 部署 fresh runtime"
                    "后重跑本测试验证默认态 HTTP"
                )
            client = HttpDaemonRpcClient(
                endpoint=manifest["endpoint"],
                verify_health=False,
                timeout=5.0,
            )
            _warm_worker(proc, client)
            yield client, manifest
        finally:
            _terminate(proc)
            _restore_or_clean_http_manifest(proc.pid, backup)

    def test_default_transport_publishes_http_manifest(self, isolated_default_http_daemon):
        client, manifest = isolated_default_http_daemon
        assert manifest["manifest_version"] == HTTP_MANIFEST_SCHEMA_VERSION
        assert manifest["security_profile"] == HTTP_MVP_TRANSPORT_PROFILE
        assert manifest["authority_id"] == get_http_authority_id()
        assert int(manifest["schema_version"]) == _SCHEMA_VERSION
        assert manifest["endpoint"].startswith("http://127.0.0.1:"), (
            f"默认态应监听 loopback 动态端口，endpoint={manifest['endpoint']!r}"
        )

    def test_health_cross_check(self, isolated_default_http_daemon):
        client, manifest = isolated_default_http_daemon
        health = client.verify_health()
        assert int(health["pid"]) == int(manifest["pid"]), (
            f"/health pid {health['pid']} != manifest pid {manifest['pid']}"
        )
        assert int(health["schema_version"]) == _SCHEMA_VERSION
        assert health["security_profile"] == HTTP_MVP_TRANSPORT_PROFILE

    def test_ping_reports_transport_http(self, isolated_default_http_daemon):
        client, manifest = isolated_default_http_daemon
        result = client.call("ping", {})
        assert result["transport"] == "http", (
            f"默认态 daemon ping 应报告 transport=http，实际 {result.get('transport')!r}"
        )
        assert int(result["pid"]) == int(manifest["pid"])
        assert result["status"] == "ok"

    def test_http_rpc_read_only_round_trip(self, isolated_default_http_daemon):
        client, _ = isolated_default_http_daemon
        try:
            # stats_top_files 为 authority 范围方法，需注入 workspace_id
            result = client.call("stats_top_files", {"workspace_id": 1, "limit": 1})
        except DaemonRemoteError as e:
            assert e.code != "method_not_found", (
                f"stats_top_files 是 Rust COMPAT_ROUTE_WHITELIST 声明的 compat 方法，"
                f"不应 method_not_found: {e}"
            )
            raise
        # 只读方法返回结构不深断言：route 受理并成功执行（字段级断言交给
        # capabilities 测试；release acceptance 同源契约容忍 E_COMPAT 执行错误，
        # 此处要求真正 round-trip 成功）
        assert result is not None
        assert result.get("files") is not None or result.get("count") is not None


# ============================================================
# 2b. H6-FIX：RPC request_id 生成与 dedup 幂等语义
# ============================================================


class TestHttpRequestIdDedupSemantics:
    """H6-FIX（T-1786787764852-4c330571）：request_id 唯一性与幂等重试。

    修复前 HttpDaemonRpcClient.call() 默认 id = str(next(self._ids))，CLI
    短生命周期进程每次从 "1" 开始，与 daemon 持久化 dedup 表 http_dedup
    （保留 24h）中同名 method 的旧记录冲突 → E_REQUEST_ID_REUSE_MISMATCH。
    修复后：默认 uuid4 全局唯一；params.request_id（CLI 路由注入的 uuid）
    优先用作 envelope id；显式 request_id 参数语义不变（同 id + 同 params
    重试命中 Replay 缓存，不重复执行）。

    依赖 runtime/current 二进制含 H6 改动（与 TestDaemonDefaultHttpTransport
    相同的隔离 daemon 启动方式）。
    """

    @pytest.fixture
    def isolated_default_http_daemon(self, tmp_path):
        if not os.path.isfile(_CURRENT_DAEMON):
            pytest.skip(f"runtime/current/cw-daemon.exe 不存在: {_CURRENT_DAEMON}")
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        backup = _backup_http_manifest()
        proc = _spawn_isolated_daemon(
            _CURRENT_DAEMON, data_root,
            extra_args=["--socket", os.path.join(data_root, "pipe")],
        )
        try:
            manifest = _wait_manifest_for_pid(proc.pid)
            if manifest is None:
                if proc.poll() is not None:
                    pytest.fail(
                        "隔离 daemon 启动失败（默认态 HTTP 未发布 manifest）\n"
                        f"{_proc_diag(proc)}"
                    )
                pytest.skip(
                    "runtime/current cw-daemon.exe 不含 H6 默认态 HTTP 改动"
                    "（默认态未发布 HTTP manifest）"
                )
            client = HttpDaemonRpcClient(
                endpoint=manifest["endpoint"],
                verify_health=False,
                timeout=5.0,
            )
            _warm_worker(proc, client)
            yield client, manifest
        finally:
            _terminate(proc)
            _restore_or_clean_http_manifest(proc.pid, backup)

    def test_default_request_id_is_globally_unique_uuid(self, isolated_default_http_daemon):
        """默认 envelope id 为 uuid4 字符串（非计数器 "1"），同 client 连续调用唯一。"""
        client, _ = isolated_default_http_daemon
        seen = set()
        for _ in range(3):
            client.call("ping", {})
            rid = client.last_request_id
            assert isinstance(rid, str) and rid, "request id 必须是非空字符串"
            parts = rid.split("-")
            assert len(parts) == 5 and parts[2].startswith("4"), (
                f"默认 request id 应为 uuid4，实际 {rid!r}"
            )
            seen.add(rid)
        assert len(seen) == 3, "同 client 连续调用的 request id 必须唯一"

    def test_params_request_id_adopted_as_envelope_id(self, isolated_default_http_daemon):
        """params.request_id（CLI 路由注入的 uuid）应被采用为 envelope id。"""
        client, _ = isolated_default_http_daemon
        custom = f"req-{uuid.uuid4().hex[:12]}"
        result = client.call("ping", {"request_id": custom})
        assert result["transport"] == "http"
        assert client.last_request_id == custom, (
            f"params.request_id 应被采用为 envelope id，实际 {client.last_request_id!r}"
        )
        assert client.last_request_body["id"] == custom

    def test_explicit_request_id_same_params_replays(self, isolated_default_http_daemon):
        """显式同 request_id + 同 params 重试命中 Replay（不重复执行）。"""
        client, _ = isolated_default_http_daemon
        params = {
            "title": f"h6fix-replay-{uuid.uuid4().hex[:8]}",
            "description": "H6-FIX Replay 验证",
            "steps": [],
            "creator": "agent",
        }
        rid = f"req-{uuid.uuid4().hex[:12]}"
        r1 = client.call("task.create", params, request_id=rid)
        r2 = client.call("task.create", params, request_id=rid)
        assert isinstance(r1, dict) and "task_id" in r1
        assert r2 == r1, (
            "同 request_id + 同 params 重试应返回缓存结果（Replay），"
            f"r1={r1} r2={r2}"
        )
        # 不重复执行：任务只创建了一个（status 查询返回同一 task_id）
        status = client.call("task.status", {"task_id": r1["task_id"]})
        assert status.get("status") == "open"

    def test_cross_instance_same_method_diff_params_no_mismatch(
            self, isolated_default_http_daemon):
        """跨 client 实例（模拟短生命周期新进程）同 method 不同 params 不冲突。

        修复前每个新实例默认 id 从 "1" 开始，第二个实例同 method 不同 params
        会触发 daemon E_REQUEST_ID_REUSE_MISMATCH；修复后 uuid 全局唯一，均成功。
        """
        _, manifest = isolated_default_http_daemon

        def _new_client():
            return HttpDaemonRpcClient(
                endpoint=manifest["endpoint"], verify_health=False, timeout=5.0,
            )

        r1 = _new_client().call("task.create", {
            "title": f"h6fix-cross-a-{uuid.uuid4().hex[:8]}",
            "description": "H6-FIX 跨实例不冲突 A",
            "steps": [],
            "creator": "agent",
        })
        r2 = _new_client().call("task.create", {
            "title": f"h6fix-cross-b-{uuid.uuid4().hex[:8]}",
            "description": "H6-FIX 跨实例不冲突 B",
            "steps": [],
            "creator": "agent",
        })
        assert isinstance(r1, dict) and "task_id" in r1
        assert isinstance(r2, dict) and "task_id" in r2
        assert r1["task_id"] != r2["task_id"], "两个不同任务应各自创建成功"


# ============================================================
# 3. 探针去重原语（DaemonMutex：并发启动不产生双实例）
# ============================================================


class TestDedupProbe:
    def test_daemon_mutex_same_endpoint_cross_thread_exclusive(self):
        """DaemonMutex 同 endpoint 跨线程互斥（探针去重核心原语）。

        Windows 命名互斥体/Unix flock 均为进程级锁：holder 线程持有期间，
        其他会话（线程）try_acquire 必须失败——这是「连续启动不产生双实例」
        的第一道门禁（ensure_daemon_for_startup → DaemonMutex）。
        """
        ep = r"\\.\pipe\callwarden-test-mutex-" + uuid.uuid4().hex
        holder_acquired = threading.Event()
        holder_done = threading.Event()

        def holder():
            m = DaemonMutex(ep)
            if m.try_acquire():
                holder_acquired.set()
                holder_done.wait(10)  # 持有互斥直到主线程验证完毕
                m.release()

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            assert holder_acquired.wait(5), "holder 线程未能获取互斥"
            contender = DaemonMutex(ep)
            assert contender.try_acquire() is False, (
                "同 endpoint 互斥被破坏：第二个会话不应获取同一互斥"
            )
            # 不同 endpoint 不互斥（探针去重不影响独立端点）
            other = DaemonMutex(r"\\.\pipe\callwarden-test-mutex-" + uuid.uuid4().hex)
            assert other.try_acquire() is True
            other.release()
        finally:
            holder_done.set()
            t.join(5)
        # release 后同 endpoint 可重新获取（释放路径正确）
        again = DaemonMutex(ep)
        assert again.try_acquire() is True
        again.release()

    def test_daemon_mutex_lock_id_deterministic(self):
        ep = r"\\.\pipe\callwarden-test-lockid"
        assert DaemonMutex(ep).lock_id == DaemonMutex(ep).lock_id
        assert DaemonMutex(ep).lock_id != DaemonMutex(ep + "-other").lock_id


# ============================================================
# 4. 显式 named-pipe 回落（HTTP 不启用，无 manifest 发布）
# ============================================================


class TestNamedPipeFallback:
    def test_rust_daemon_named_pipe_no_http_manifest(self, tmp_path):
        """显式 CW_DAEMON_TRANSPORT=named-pipe：Rust daemon HTTP 不启用。

        等待启动窗口（3s）后断言：隔离 daemon 存活、`~/.callwarden/` 下无
        pid 匹配的 HTTP manifest（回落旧通道）。旧/新二进制行为一致。
        """
        if not os.path.isfile(_CURRENT_DAEMON):
            pytest.skip(f"runtime/current/cw-daemon.exe 不存在: {_CURRENT_DAEMON}")
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        proc = _spawn_isolated_daemon(
            _CURRENT_DAEMON, data_root,
            extra_args=["--socket", os.path.join(data_root, "pipe")],
            extra_env={"CW_DAEMON_TRANSPORT": "named-pipe"},
        )
        try:
            # manifest 在 daemon 启动早期原子发布；3s 窗口足够判定「未发布」
            time.sleep(3.0)
            assert proc.poll() is None, (
                f"隔离 daemon（named-pipe 回落）启动失败\n{_proc_diag(proc)}"
            )
            manifest = _scan_manifest_for_pid(proc.pid)
            assert manifest is None, (
                f"显式 CW_DAEMON_TRANSPORT=named-pipe 回落时不应发布 HTTP manifest: "
                f"{manifest}"
            )
        finally:
            _terminate(proc)
            _restore_or_clean_http_manifest(proc.pid, None)
