"""H4B-C: Compatibility read HTTP cutover 测试

验证 tools_summary.py / tools_semantic.py（50 个 python_compat 工具）在 HTTP
daemon 模式下 fail-closed，不建立指向不存在 RPC 的伪路由，且不直连本地 SQLite
（无 SQLite fallback）。

归类依据：.trae-cn/evidence/http-daemon-capability-matrix.json（237 tools 矩阵）
- tools_summary.py 31 个工具：全部 backend=python_compat、daemon_rpc_method=none
- tools_semantic.py 19 个工具：全部 backend=python_compat、daemon_rpc_method=none
- dispatch.rs 无任何 summary.* / semantic.* / ownership.* / gc_* RPC 分支，
  DaemonStateExt 默认返回 method_not_found —— 伪路由在 HTTP 模式必失败。

H4B-C 实现策略（tools_*.py 内 fail-closed，不触碰 Rust/compat_registry）：
- HTTP 模式：_http_unsupported() 返回结构化 E_HTTP_COMPAT_UNSUPPORTED，
  不构造 CodeGraphDB（无 SQLite fallback）；
- legacy 模式：保持本地 get_db() 执行，公开方法语义不变。

真实进程门（TestRealDaemonCompatRpcAlignment，参照 H4B-N 的
TestRealDaemonRpcNameAlignment）：
- 正向：compat_route 已注册的 get_uncommented_symbols 在生产
  HttpDaemonRpcClient 调用下不返回 method_not_found（dispatch_arc 错误为
  E_UNAVAILABLE / E_COMPAT_*，与 RPC 名无关）；
- 负向：伪路由候选名（summary.repo_map / semantic.gc_audit_list）在真实
  daemon 上必返回 method_not_found —— 实证 fail-closed 契约（若本任务工具
  建立伪路由，HTTP 模式必抛 method_not_found）。
"""

import inspect
import json
import os
import subprocess
import sys
import time
from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_semantic, tools_summary
from callwarden.config import (  # noqa: E402
    get_http_manifest_dir,
    get_http_manifest_path,
)
from callwarden.server.daemon_autostart import _pid_alive  # noqa: E402


# ============================================================
# 辅助夹具
# ============================================================

COMPAT_MODULES = [tools_summary, tools_semantic]


@pytest.fixture
def mock_http_mode(monkeypatch):
    """启用 HTTP 模式（is_http_transport_enabled 返回 True）。"""
    monkeypatch.setattr(
        "callwarden.server.daemon_client.is_http_transport_enabled",
        lambda: True,
    )


def _register_tools(module, mcp=None):
    """注册工具模块到 mock MCP，返回 {name: fn} 字典。"""
    if mcp is None:
        mcp = MagicMock()
    registrations = {}

    def tool_capture(name=None):
        def decorator(fn):
            registrations[fn.__name__] = fn
            return fn

        return decorator

    mcp.tool = tool_capture
    module.register(mcp)
    return registrations


def _default_value(annotation, name):
    """按注解推断一个安全的必填参数默认值（仅用于构造调用参数）。"""
    ann = str(annotation).lower()
    if ann == "bool" or "bool" in ann:
        return False
    if ann == "int" or "int" in ann:
        return 1
    if ann == "float" or "float" in ann:
        return 1.0
    if "list" in ann:
        return []
    return "x"  # str / 无注解 → 字符串


def _make_call_args(fn):
    """根据函数签名构造最小调用参数（仅必填参数，缺省参数不传）。"""
    args, kwargs = [], {}
    for name, p in inspect.signature(fn).parameters.items():
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                      inspect.Parameter.POSITIONAL_OR_KEYWORD):
            if p.default is inspect.Parameter.empty:
                args.append(_default_value(p.annotation, name))
        elif p.kind == inspect.Parameter.KEYWORD_ONLY:
            if p.default is inspect.Parameter.empty:
                kwargs[name] = _default_value(p.annotation, name)
    return args, kwargs


# ============================================================
# 1. HTTP 模式结构化 unsupported（50 个 python_compat 工具全量）
# ============================================================

class TestHttpModeStructuredUnsupported:
    """所有 python_compat 工具在 HTTP 模式下 fail-closed 返回结构化 unsupported。"""

    @pytest.mark.parametrize("module", COMPAT_MODULES, ids=lambda m: m.__name__.split(".")[-1])
    def test_all_tools_fail_closed_in_http_mode(self, module, mock_http_mode):
        tools = _register_tools(module)
        assert len(tools) > 0
        with patch(f"{module.__name__}.get_db") as mock_get_db:
            for name, fn in tools.items():
                args, kwargs = _make_call_args(fn)
                result = fn(*args, **kwargs)
                assert isinstance(result, dict), f"{name} HTTP 模式应返回结构化 dict"
                assert result.get("error") == "E_HTTP_COMPAT_UNSUPPORTED", name
                assert result.get("backend") == "python_compat", name
                assert result.get("tool") == name, name
            # 无 SQLite fallback 证明：HTTP 模式下 get_db 从未被调用
            mock_get_db.assert_not_called()


# ============================================================
# 2. fail-closed 静态验证：无伪路由
# ============================================================

class TestNoPseudoRoutes:
    """fail-closed：两模块不得存在指向不存在 RPC 的伪路由。"""

    def test_no_daemon_rpc_pseudo_route(self):
        """任何工具源码不得含 _call_daemon_rpc / _get_daemon_client 伪路由。"""
        for module in COMPAT_MODULES:
            tools = _register_tools(module)
            for name, fn in tools.items():
                source = inspect.getsource(fn)
                assert "_call_daemon_rpc" not in source, (
                    f"{name} 不应有 daemon RPC 伪路由"
                )
                assert "_get_daemon_client" not in source, (
                    f"{name} 不应有 client 伪路由"
                )

    def test_every_tool_starts_with_http_unsupported(self):
        """每个工具均以 _http_unsupported("<工具名>") 开头 fail-closed。"""
        for module in COMPAT_MODULES:
            tools = _register_tools(module)
            for name, fn in tools.items():
                source = inspect.getsource(fn)
                assert f'_http_unsupported("{name}")' in source, (
                    f"{name} 应以 _http_unsupported(\"{name}\") 开头 fail-closed"
                )

    def test_no_sqlite_fallback_in_module(self):
        """模块级不得直接构造 CodeGraphDB（无 SQLite fallback）。"""
        for module in COMPAT_MODULES:
            source = inspect.getsource(module)
            assert "CodeGraphDB(" not in source, (
                f"{module.__name__} 不得直接构造 CodeGraphDB（无 SQLite fallback）"
            )
            assert "_http_unsupported" in source


# ============================================================
# 3. legacy 模式保持本地执行（公开方法语义不变）
# ============================================================

class TestLegacyModeKeepsLocalExec:
    """非 HTTP 模式保持本地 get_db() 执行（无公开方法语义漂移）。"""

    @pytest.mark.parametrize("module", COMPAT_MODULES, ids=lambda m: m.__name__.split(".")[-1])
    def test_legacy_mode_calls_get_db(self, module):
        tools = _register_tools(module)
        with patch(f"{module.__name__}.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            for name, fn in tools.items():
                args, kwargs = _make_call_args(fn)
                fn(*args, **kwargs)
            # 非 HTTP 模式下所有工具均触达本地 DB（get_db 至少被调用一次）
            assert mock_get_db.called, "legacy 模式应保持本地 get_db() 执行"


# ============================================================
# 4. 真实进程级 compat 路由对齐门（参照 H4B-N TestRealDaemonRpcNameAlignment）
# ============================================================

def _find_daemon_binary():
    """定位 current-HEAD 构建的 cw-daemon 二进制（与 H2I 集成门同源）。

    优先本地 cargo build 产物，保证与当前源码一致；CW_DAEMON_BIN / runtime
    部署仅作兜底。二进制不可用时跳过用例。
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


def _wait_manifest(proc, timeout=10.0):
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配当前进程）。

    H6 修复（9d6ca63，2026-08-15）后 manifest 固定写 `~/.callwarden/`
    （http_manifest_dir = USERPROFILE/.callwarden），不再写 daemon data_root；
    本文件隔离 daemon 不重定向 USERPROFILE，故轮询真实 get_http_manifest_dir()。
    """
    directory = get_http_manifest_dir()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isdir(directory):
            for f in os.listdir(directory):
                if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                    p = os.path.join(directory, f)
                    try:
                        m = json.loads(open(p, encoding="utf-8").read())
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


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


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 管道），启用 HTTP transport。"""
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    # compat worker 使用与 daemon 同版本的 Python 解释器
    env["CW_COMPAT_PYTHON"] = sys.executable
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


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


class TestRealDaemonCompatRpcAlignment:
    """真实进程级 compat 路由对齐门（H4B-C 产物）。

    - 正向：compat_route 已注册方法（stats_top_files，get_uncommented_symbols
      已 W2-1 迁移 rust_native）在生产 HttpDaemonRpcClient 调用下**绝不**返回
      method_not_found（compat adapter 错误为 E_UNAVAILABLE / E_COMPAT_*，与
      RPC 名无关）；
    - 负向：若本任务工具建立伪路由（如 summary.repo_map / semantic.gc_audit_list），
      真实 daemon 必返回 method_not_found —— 实证 fail-closed 契约。
    """

    @pytest.fixture
    def real_daemon(self, tmp_path):
        """启动隔离真实 daemon，yield 生产类 HttpDaemonRpcClient。"""
        bin_path = _find_daemon_binary()
        if bin_path is None:
            pytest.skip("cw-daemon 二进制不可用（需先 cargo build --bin cw-daemon）")
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        backup = _backup_http_manifest()
        proc = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
        try:
            manifest = _wait_manifest(proc)
            if manifest is None:
                pytest.fail("隔离 daemon 未发布 manifest")
            client = HttpDaemonRpcClient(
                endpoint=manifest["endpoint"],
                verify_health=False,
                timeout=5.0,
            )
            yield client
        finally:
            _terminate(proc)
            _restore_or_clean_http_manifest(proc.pid, backup)

    def test_compat_registered_method_has_route(self, real_daemon):
        """compat_route 已注册的 stats_top_files：不返回 method_not_found。"""
        try:
            result = real_daemon.call(
                "stats_top_files", {"deadline_ms": 10000}
            )
        except DaemonRemoteError as exc:
            assert exc.code != "method_not_found", (
                f"compat 方法不应 method_not_found（应走 compat_route）: {exc}"
            )
        else:
            assert result is not None

    def test_summary_pseudo_route_returns_method_not_found(self, real_daemon):
        """伪路由候选名 summary.* 在真实 daemon 上必返回 method_not_found。"""
        with pytest.raises(DaemonRemoteError) as ei:
            real_daemon.call("summary.repo_map", {})
        assert ei.value.code == "method_not_found", (
            "tools_summary 工具若走伪路由 summary.* 在 HTTP 模式必失败（fail-closed）"
        )

    def test_semantic_pseudo_route_returns_method_not_found(self, real_daemon):
        """伪路由候选名 semantic.* 在真实 daemon 上必返回 method_not_found。"""
        with pytest.raises(DaemonRemoteError) as ei:
            real_daemon.call("semantic.gc_audit_list", {})
        assert ei.value.code == "method_not_found", (
            "tools_semantic 工具若走伪路由 semantic.* 在 HTTP 模式必失败（fail-closed）"
        )
