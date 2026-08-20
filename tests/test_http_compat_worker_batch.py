"""H4C-1: compat worker 批量 read-only 基建扩展测试。

覆盖（派发单 T-1786713075422-d9a98426 要点 1/3/4）：
- registry 批量 read_only 注册（register_read_only_batch + 模块级
  register_compat_routes 同步 RUST_COMPAT_ROUTE + 两端对齐门覆盖批量）；
- daemon_client.route_worker_call 四态路由（local 直行 / HTTP fail-closed /
  enterprise fail-closed / auto 降级），不泄漏 method_not_found；
- 真实进程门 TestRealDaemonCompatWorkerBatch：隔离 daemon + 生产
  HttpDaemonRpcClient，覆盖 worker 批量执行成功、超时、worker 缺失
  fail-closed（不得 skip）。

归类依据：http-daemon-mvp-compatibility-contract.md §3.3 worker 契约；
Rust 侧 http_server.rs COMPAT_ROUTE_WHITELIST 与 Python 侧
compat_registry.py RUST_COMPAT_ROUTE 由 validate_against_rust_route 对齐。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from server import compat_registry as reg  # noqa: E402
from callwarden.server.daemon_client import (  # noqa: E402
    DaemonUnavailableError,
    HttpDaemonRpcClient,
    route_worker_call,
)
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402

# 白名单内方法（默认 registry 已注册）与白名单外方法名。
# W2-1（T-1786840097330-dec66710）：get_uncommented_symbols 已迁移 rust_native，
# 默认 registry 现仅注册 stats_top_files；其 handler 只依赖 file_instances +
# symbols 表，与 MINIMAL_SCHEMA 种子库兼容，故两个白名单方法均以
# stats_top_files 的（workspace/limit）参数变体覆盖批量执行路径。
WHITELISTED_METHOD = "stats_top_files"
WHITELISTED_METHOD2 = "stats_top_files"
NOT_WHITELISTED = "batch.worker_symbol_search"


# ============================================================
# 1. registry 批量 read_only 注册（要点 1）
# ============================================================


def _handler(ctx):
    return {"ok": True}


class TestRegistryReadOnlyBatch:
    """register_read_only_batch 批量注册 read_only 方法（fail-closed 校验不变）。"""

    def test_batch_registers_multiple_read_only_methods(self):
        r = reg.CompatRegistry()
        r.register_read_only_batch(
            {"sym_a": _handler, "sym_b": _handler},
            reg.SCOPE_WORKSPACE,
            "符号 read_only 白名单",
        )
        for m in ("sym_a", "sym_b"):
            assert r.is_compat_method(m)
            assert r.operation_class(m) == reg.READ_ONLY
            assert r.workspace_scope(m) == reg.SCOPE_WORKSPACE
        assert len(r) == 2

    def test_batch_empty_raises(self):
        r = reg.CompatRegistry()
        with pytest.raises(ValueError):
            r.register_read_only_batch({}, reg.SCOPE_WORKSPACE, "d")

    def test_batch_invalid_scope_raises(self):
        r = reg.CompatRegistry()
        with pytest.raises(ValueError):
            r.register_read_only_batch(
                {"sym_a": _handler}, reg.SCOPE_NONE, "d"
            )
        with pytest.raises(ValueError):
            r.register_read_only_batch(
                {"sym_a": _handler}, "bogus_scope", "d"
            )

    def test_batch_conflicts_with_existing_method_raises(self):
        r = reg.CompatRegistry()
        r.register_read_only_batch(
            {"sym_a": _handler}, reg.SCOPE_WORKSPACE, "d"
        )
        with pytest.raises(ValueError):
            r.register_read_only_batch(
                {"sym_a": _handler, "sym_b": _handler},
                reg.SCOPE_SNAPSHOT,
                "d",
            )


class TestRegisterCompatRoutes:
    """模块级 register_compat_routes 同步 RUST_COMPAT_ROUTE（两端对齐门覆盖批量）。"""

    def test_batch_syncs_rust_route_and_aligned(self, monkeypatch):
        # 隔离全局状态：默认 registry 与 RUST_COMPAT_ROUTE 换新，避免污染
        # 既有测试（test_default_registry_has_two_methods 等）。
        monkeypatch.setattr(reg, "_DEFAULT_REGISTRY", reg.CompatRegistry())
        monkeypatch.setattr(reg, "RUST_COMPAT_ROUTE", {})
        reg.register_compat_routes(
            {"sym_a": _handler, "sym_b": _handler},
            reg.SCOPE_WORKSPACE,
            "符号 read_only 白名单",
        )
        # RUST_COMPAT_ROUTE 已同步为 read_only
        assert reg.RUST_COMPAT_ROUTE["sym_a"] == reg.READ_ONLY
        assert reg.RUST_COMPAT_ROUTE["sym_b"] == reg.READ_ONLY
        # compat_route 镜像语义：批量注册后返回 read_only
        assert reg.compat_route("sym_a") == reg.READ_ONLY
        # 两端对齐门：registry 与 RUST_COMPAT_ROUTE 完全一致
        result = reg.validate_against_rust_route()
        assert result["aligned"] is True
        assert result["missing"] == []
        assert result["extra"] == []
        assert result["mismatch"] == {}

    def test_batch_unknown_method_still_none(self, monkeypatch):
        # fail-closed 语义不变：未注册方法 compat_route 返回 None
        monkeypatch.setattr(reg, "_DEFAULT_REGISTRY", reg.CompatRegistry())
        monkeypatch.setattr(reg, "RUST_COMPAT_ROUTE", {})
        reg.register_compat_routes({"sym_a": _handler}, reg.SCOPE_WORKSPACE, "d")
        assert reg.compat_route("sym_a") == reg.READ_ONLY
        assert reg.compat_route("no_such_method") is None

    def test_batch_with_existing_whitelist_method_raises(self, monkeypatch):
        # 批量注册方法名与默认 registry 冲突 → 复用 register 的重复校验
        monkeypatch.setattr(reg, "RUST_COMPAT_ROUTE", {})
        with pytest.raises(ValueError):
            reg.register_compat_routes(
                {WHITELISTED_METHOD: _handler},
                reg.SCOPE_WORKSPACE,
                "d",
            )


# ============================================================
# 2. route_worker_call 四态路由（要点 3）
# ============================================================


@pytest.fixture
def mock_route_env(monkeypatch):
    """提供 route_worker_call 依赖的 env 开关 mock 基座。"""

    def _apply(*, http=False, mode="auto", rpc_client=None, daemon_required=False):
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: http,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode",
            lambda: mode,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_daemon_required",
            lambda: daemon_required,
        )
        client = rpc_client if rpc_client is not None else MagicMock()
        monkeypatch.setattr(
            "callwarden.server.daemon_client._get_rpc_client_for_route",
            lambda: client,
        )
        return client

    return _apply


class TestRouteWorkerCall:
    """route_worker_call 四态路由：HTTP/enterprise fail-closed、auto 降级、免泄漏。"""

    def test_local_mode_executes_fallback(self, mock_route_env):
        # local 模式（非 HTTP）→ 直接执行 fallback，不触 RPC
        mock_route_env(http=False, mode="local")
        sentinel = {"local": True}
        result = route_worker_call(WHITELISTED_METHOD, {}, lambda: sentinel)
        assert result == sentinel

    def test_http_not_whitelisted_fail_closed(self, mock_route_env):
        # HTTP + 白名单外 → 结构化 E_HTTP_COMPAT_UNSUPPORTED（不泄漏 method_not_found）
        mock_route_env(http=True, mode="auto")
        result = route_worker_call(NOT_WHITELISTED, {}, lambda: {"fallback": True})
        assert result.get("error") == "E_HTTP_COMPAT_UNSUPPORTED"
        assert result.get("tool") == NOT_WHITELISTED
        assert result.get("backend") == "python_compat"
        assert "method_not_found" not in str(result)

    def test_http_whitelisted_calls_worker(self, mock_route_env):
        # HTTP + 白名单内 → 经 rpc_client.call 执行 worker，透传 params
        client = MagicMock()
        client.call.return_value = {"ok": True, "result": {"count": 3}}
        mock_route_env(http=True, mode="auto", rpc_client=client)
        params = {"workspace_id": 1, "limit": 10}
        result = route_worker_call(WHITELISTED_METHOD, params, lambda: {})
        assert result == {"ok": True, "result": {"count": 3}}
        client.call.assert_called_once_with(WHITELISTED_METHOD, params)

    def test_auto_not_whitelisted_degrades_to_fallback(self, mock_route_env):
        # auto 模式（非 HTTP）+ 白名单外 → 降级 fallback
        mock_route_env(http=False, mode="auto")
        sentinel = {"fallback": True}
        result = route_worker_call(NOT_WHITELISTED, {}, lambda: sentinel)
        assert result == sentinel

    def test_enterprise_not_whitelisted_fail_closed(self, mock_route_env):
        # enterprise + 白名单外 → fail-closed unsupported（不回退本地）
        mock_route_env(http=False, mode="enterprise", daemon_required=True)
        result = route_worker_call(NOT_WHITELISTED, {}, lambda: {"fallback": True})
        assert result.get("error") == "E_HTTP_COMPAT_UNSUPPORTED"

    def test_http_worker_remote_error_propagates(self, mock_route_env):
        # HTTP + worker 结构化错误 → 原样透传（不降级本地读）
        client = MagicMock()
        client.call.side_effect = DaemonRemoteError("E_COMPAT_WORKER_TIMEOUT", "timeout")
        mock_route_env(http=True, mode="auto", rpc_client=client)
        with pytest.raises(DaemonRemoteError) as ei:
            route_worker_call(WHITELISTED_METHOD, {}, lambda: {"fallback": True})
        assert ei.value.code == "E_COMPAT_WORKER_TIMEOUT"

    def test_auto_worker_remote_error_degrades(self, mock_route_env):
        # auto 模式（非 HTTP）+ worker 结构化错误 → 降级 fallback
        client = MagicMock()
        client.call.side_effect = DaemonRemoteError("E_COMPAT_WORKER_UNAVAILABLE", "unavail")
        mock_route_env(http=False, mode="auto", rpc_client=client)
        sentinel = {"fallback": True}
        result = route_worker_call(WHITELISTED_METHOD, {}, lambda: sentinel)
        assert result == sentinel

    def test_enterprise_worker_remote_error_propagates(self, mock_route_env):
        # enterprise + worker 结构化错误 → 原样透传
        client = MagicMock()
        client.call.side_effect = DaemonRemoteError("E_COMPAT_EXECUTION_ERROR", "exec")
        mock_route_env(http=False, mode="enterprise", rpc_client=client, daemon_required=True)
        with pytest.raises(DaemonRemoteError) as ei:
            route_worker_call(WHITELISTED_METHOD, {}, lambda: {})
        assert ei.value.code == "E_COMPAT_EXECUTION_ERROR"

    def test_http_connection_failure_fail_closed(self, mock_route_env):
        # HTTP + 连接失败 → DaemonUnavailableError（fail-closed，不回退本地 SQLite）
        client = MagicMock()
        client.call.side_effect = OSError("conn refused")
        mock_route_env(http=True, mode="auto", rpc_client=client)
        with pytest.raises(DaemonUnavailableError):
            route_worker_call(WHITELISTED_METHOD, {}, lambda: {"fallback": True})

    def test_enterprise_connection_failure_fail_closed(self, mock_route_env):
        client = MagicMock()
        client.call.side_effect = OSError("conn refused")
        mock_route_env(http=False, mode="enterprise", rpc_client=client, daemon_required=True)
        with pytest.raises(DaemonUnavailableError):
            route_worker_call(WHITELISTED_METHOD, {}, lambda: {})

    def test_auto_connection_failure_degrades(self, mock_route_env):
        # auto 模式（非 HTTP）+ 连接失败 → 降级 fallback
        client = MagicMock()
        client.call.side_effect = OSError("conn refused")
        mock_route_env(http=False, mode="auto", rpc_client=client)
        sentinel = {"fallback": True}
        result = route_worker_call(WHITELISTED_METHOD, {}, lambda: sentinel)
        assert result == sentinel


# ============================================================
# 3. 真实进程门（要点 4）：隔离 daemon + 生产 HttpDaemonRpcClient
# ============================================================

# 与 test_http_compat_worker.py 一致的 worker 种子库 schema（仅覆盖 worker 查询列）
MINIMAL_SCHEMA = """
CREATE TABLE IF NOT EXISTS file_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    status TEXT DEFAULT 'pending'
);
CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    qualified_name TEXT DEFAULT '',
    start_line INTEGER NOT NULL,
    end_line INTEGER NOT NULL,
    has_comment INTEGER DEFAULT 0
);
"""

SAMPLE_ROWS = [
    # (workspace_id, rel_path, name, kind, qualified_name, start_line, end_line, has_comment)
    (1, "src/app.py", "run", "function", "app.run", 1, 5, 0),
    (1, "src/app.py", "helper", "function", "app.helper", 10, 15, 1),
    (1, "src/util.py", "parse", "function", "util.parse", 20, 30, 0),
    (1, "src/util.py", "Token", "class", "util.Token", 40, 60, 0),
    (2, "other/main.py", "main", "function", "main.main", 1, 9, 0),
]


def _seed_worker_db(home_dir: Path) -> str:
    """在隔离 USERPROFILE 下建 worker 可读的用户级种子库，返回 db 路径。"""
    db_file = home_dir / ".callwarden" / "callwarden.db"
    db_file.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_file))
    try:
        conn.executescript(MINIMAL_SCHEMA)
        for (ws, rel, name, kind, qn, sl, el, hc) in SAMPLE_ROWS:
            conn.execute(
                "INSERT INTO file_instances (workspace_id, rel_path, status) VALUES (?, ?, 'parsed')",
                (ws, rel),
            )
            fi_id = conn.execute(
                "SELECT id FROM file_instances WHERE workspace_id=? AND rel_path=?",
                (ws, rel),
            ).fetchone()[0]
            conn.execute(
                """INSERT INTO symbols
                   (file_instance_id, name, kind, qualified_name, start_line, end_line, has_comment)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (fi_id, name, kind, qn, sl, el, hc),
            )
        conn.commit()
    finally:
        conn.close()
    return str(db_file)


def _find_daemon_binary():
    """定位 current-HEAD 构建的 cw-daemon 二进制（与 H4B-C 真实进程门同源）。"""
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
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配当前进程）。

    H6 修复（9d6ca63，2026-08-15）后 manifest 固定写 `USERPROFILE/.callwarden/`
    （http_manifest_dir），隔离 daemon 的 USERPROFILE = data_root/userhome，
    故轮询 data_root/userhome/.callwarden；data_root 根目录不再有 manifest。
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


def _terminate(proc):
    """终止 daemon 进程（terminate 优先，兜底 kill）。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _spawn_isolated_daemon(bin_path, data_root, http_bind, worker_script=None):
    """启动隔离 daemon（临时 task DB / registry / 管道 / USERPROFILE）。

    与 H4B-C _spawn_isolated_daemon 的差异：
    - USERPROFILE 重定向到隔离目录（daemon 与 worker 都按 config 解析
      ~/.callwarden/callwarden.db → 隔离种子库，不触碰真实用户数据库）；
    - 可注入 CW_COMPAT_WORKER_SCRIPT 指向缺失脚本，模拟 worker 缺失。
    """
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    # compat worker 使用与 daemon 同版本的 Python 解释器
    env["CW_COMPAT_PYTHON"] = sys.executable
    if worker_script is not None:
        env["CW_COMPAT_WORKER_SCRIPT"] = worker_script
    home_dir = Path(data_root) / "userhome"
    home_dir.mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(home_dir)
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _expect_daemon_remote_error(client, method, params, expected_code):
    """断言调用抛出指定结构化 E_COMPAT_* 错误（worker 侧 fail-closed 证据）。"""
    with pytest.raises(DaemonRemoteError) as ei:
        client.call(method, params)
    assert ei.value.code == expected_code, (
        f"{method} 应返回 {expected_code}，实际 {ei.value.code}: {ei.value.message}"
    )


class TestRealDaemonCompatWorkerBatch:
    """真实进程门：隔离 daemon + 生产 HttpDaemonRpcClient 覆盖 worker 批量行为。

    覆盖（派发单要点 4）：
    - 批量执行成功：2 个白名单方法均经 worker 返回种子库真实数据（绝不
      method_not_found）；
    - 超时：deadline_ms=1 → E_COMPAT_WORKER_TIMEOUT（retryable，fail-closed）；
    - worker 缺失：CW_COMPAT_WORKER_SCRIPT 指向不存在脚本 →
      E_COMPAT_WORKER_UNAVAILABLE（fail-closed，不直连本地 SQLite）。
    """

    @pytest.fixture
    def daemon_bin(self):
        bin_path = _find_daemon_binary()
        if bin_path is None:
            pytest.fail("cw-daemon 二进制不可用（H4C-1 真实进程门不得 skip：需先 cargo build --bin cw-daemon）")
        return bin_path

    def _spawn_with_client(self, daemon_bin, tmp_path, worker_script=None):
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        _seed_worker_db(Path(data_root) / "userhome")
        proc = _spawn_isolated_daemon(
            daemon_bin, data_root, "127.0.0.1:0", worker_script=worker_script
        )
        manifest = _wait_manifest(data_root, proc)
        if manifest is None:
            _terminate(proc)
            pytest.fail("隔离 daemon 未发布 manifest")
        client = HttpDaemonRpcClient(
            endpoint=manifest["endpoint"],
            verify_health=False,
            timeout=5.0,
        )
        return proc, client

    def test_batch_worker_success(self, daemon_bin, tmp_path):
        """批量成功：白名单方法经 worker 返回种子库真实数据（无 method_not_found）。"""
        proc, client = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            # 方法 1：stats_top_files（workspace_id=1 → 2 个文件，含注释覆盖）
            result = client.call(
                WHITELISTED_METHOD, {"workspace_id": 1, "limit": 100, "deadline_ms": 10000}
            )
            assert result is not None
            assert result["count"] == 2, f"批量成功路径应返回种子库真实数据: {result}"
            by_path = {f["rel_path"]: f for f in result["files"]}
            assert by_path["src/app.py"]["comment_coverage"] == 0.5
            assert by_path["src/util.py"]["comment_coverage"] == 0.0
            # 方法 2：stats_top_files（workspace_id=2 → 仅 other/main.py，隔离生效）
            result2 = client.call(
                WHITELISTED_METHOD2, {"workspace_id": 2, "limit": 10, "deadline_ms": 10000}
            )
            assert result2 is not None
            assert result2["count"] == 1
            assert result2["files"][0]["rel_path"] == "other/main.py"
            assert result2["files"][0]["comment_coverage"] == 0.0
        finally:
            _terminate(proc)

    def test_batch_worker_timeout_fail_closed(self, daemon_bin, tmp_path):
        """超时：deadline_ms=1 → E_COMPAT_WORKER_TIMEOUT（worker 被终止，fail-closed）。"""
        proc, client = self._spawn_with_client(daemon_bin, tmp_path)
        try:
            _expect_daemon_remote_error(
                client,
                WHITELISTED_METHOD,
                {"workspace_id": 1, "limit": 10, "deadline_ms": 1},
                "E_COMPAT_WORKER_TIMEOUT",
            )
        finally:
            _terminate(proc)

    def test_batch_worker_missing_fail_closed(self, daemon_bin, tmp_path):
        """worker 缺失：脚本路径不存在 → E_COMPAT_WORKER_UNAVAILABLE（不直连 SQLite）。"""
        missing_script = str(tmp_path / "no_such_worker.py")
        proc, client = self._spawn_with_client(daemon_bin, tmp_path, worker_script=missing_script)
        try:
            _expect_daemon_remote_error(
                client,
                WHITELISTED_METHOD,
                {"workspace_id": 1, "limit": 10, "deadline_ms": 10000},
                "E_COMPAT_WORKER_UNAVAILABLE",
            )
        finally:
            _terminate(proc)
