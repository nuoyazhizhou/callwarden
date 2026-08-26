"""SRV-005 迁移验收：server daemon autostart Python authority → Rust daemon。

覆盖 task `T-1787323460652-c2eaada8` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-005 = `control_plane` 端口，route B thin-client）：
- Python `server/daemon_autostart.py` 原有三个直接执行本地 socket connect 的
  探测权威函数：_try_connect_tcp / _try_connect_unix / try_http_connect。
- SRV-005 后全部退化为 daemon RPC 薄客户端（`mcp.daemon_autostart.*`，
  Rust handler `rust_ext/src/daemon/daemon_autostart_handlers.rs`）。
- RPC 无法传递 socket 对象：下沉为「connect + 立即关闭」的探测语义，
  返回 `{"connectable": bool, ...}`（try_http_connect 保持 bool 签名）。
- 错误语义与下沉前对齐：endpoint 缺失 → `invalid_params`（stable errors），
  格式非法/不可达 → fail-soft `connectable=false`（对齐 Python 返回 None/False）。
- daemon 不可用时 fail-closed 上抛 DaemonUnavailableError，
  绝不回退本地 socket 探测（no business fallback）。
- `try_connect` 的 transport bootstrap（活 socket）为客户端连接自身 daemon 的
  物理必需（RPC 依赖该连接），保留最小本地 transport 连接——transport 适配
  职责，非 DB/业务 authority，不在本矩阵的退役扫描目标内。
- 本测试用内存态 `FakeAutostartDaemon` 模拟 daemon handler 行为，
  不依赖真实 daemon 进程，也不发起真实网络连接。
"""

import ast

import pytest

from callwarden.server.daemon_autostart import (
    _try_connect_tcp,
    _try_connect_unix,
    try_http_connect,
)
from callwarden.server.daemon_client import DaemonUnavailableError


class FakeDaemonRpcError(Exception):
    """模拟 daemon 端 DaemonRpcError（带稳定 error code）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FakeAutostartDaemon:
    """内存态 daemon autostart 探测权威（对齐 Rust `daemon_autostart_handlers` 语义）。"""

    def __init__(self):
        self.available: bool = True
        self.calls: list = []

    def __call__(self, method: str, params: dict):
        if not self.available:
            raise DaemonUnavailableError("daemon 不可用（测试模拟）", code="daemon_unavailable")
        self.calls.append((method, dict(params)))

        if method == "mcp.daemon_autostart.try_connect_tcp":
            endpoint = params.get("endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise FakeDaemonRpcError("invalid_params", "缺少字段: endpoint")
            host_port = endpoint.removeprefix("tcp://")
            host, _, port = host_port.rpartition(":")
            if not host or not port.isdigit():
                # fail-soft：格式非法 connectable=false（对齐 Python 返回 None）
                return {
                    "connectable": False,
                    "endpoint": endpoint,
                    "error": "invalid tcp endpoint: expect tcp://host:port or host:port",
                }
            if "unreachable" in endpoint:
                return {
                    "connectable": False,
                    "host": host,
                    "port": int(port),
                    "error": "connect failed or unreachable",
                }
            return {"connectable": True, "host": host, "port": int(port), "error": None}

        if method == "mcp.daemon_autostart.try_connect_unix":
            endpoint = params.get("endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise FakeDaemonRpcError("invalid_params", "缺少字段: endpoint")
            if "nonexistent" in endpoint:
                return {
                    "connectable": False,
                    "endpoint": endpoint,
                    "error": "connect failed or unreachable",
                }
            return {"connectable": True, "endpoint": endpoint, "error": None}

        if method == "mcp.daemon_autostart.try_http_connect":
            endpoint = params.get("endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise FakeDaemonRpcError("invalid_params", "缺少字段: endpoint")
            return {"connectable": "unreachable" not in endpoint}

        raise FakeDaemonRpcError("method_not_found", f"未知方法 {method}")


@pytest.fixture
def fake_daemon(monkeypatch):
    """每个测试安装一个干净的内存态 daemon 薄客户端。"""
    daemon = FakeAutostartDaemon()
    monkeypatch.setattr("callwarden.server.daemon_autostart._call_daemon_rpc", daemon)
    return daemon


# ============================================================
# 1) success
# ============================================================


def test_success_try_connect_tcp_reachable(fake_daemon):
    result = _try_connect_tcp("tcp://127.0.0.1:8456")
    assert result["connectable"] is True
    assert result["host"] == "127.0.0.1"
    assert result["port"] == 8456
    assert result["error"] is None


def test_success_try_connect_tcp_invalid_format_fail_soft(fake_daemon):
    # 格式非法不抛异常（对齐 Python 原返回 None 的 fail-soft 语义）
    result = _try_connect_tcp("tcp://host:abc")
    assert result["connectable"] is False
    assert isinstance(result["error"], str)


def test_success_try_connect_unix(fake_daemon):
    result = _try_connect_unix("/run/callwarden/callwarden.sock")
    assert result["connectable"] is True
    result_miss = _try_connect_unix("/nonexistent/callwarden.sock")
    assert result_miss["connectable"] is False
    assert isinstance(result_miss["error"], str)


def test_success_try_http_connect_returns_bool(fake_daemon):
    assert try_http_connect("http://127.0.0.1:8456") is True
    assert try_http_connect("http://127.0.0.1:8456", timeout=0.5) is True
    assert try_http_connect("http://unreachable.local:9") is False


# ============================================================
# 2) invalid（stable errors：endpoint 缺失 → invalid_params，薄客户端透传）
# ============================================================


def test_invalid_try_connect_tcp_missing_endpoint(fake_daemon):
    with pytest.raises(FakeDaemonRpcError) as exc:
        _try_connect_tcp("")
    assert exc.value.code == "invalid_params"


def test_invalid_try_connect_unix_missing_endpoint(fake_daemon):
    with pytest.raises(FakeDaemonRpcError) as exc:
        _try_connect_unix("")
    assert exc.value.code == "invalid_params"


def test_invalid_try_http_connect_missing_endpoint(fake_daemon):
    with pytest.raises(FakeDaemonRpcError) as exc:
        try_http_connect("")
    assert exc.value.code == "invalid_params"


# ============================================================
# 3) authority（探测权威在 daemon；Python 不再本地 socket connect）
# ============================================================


def test_authority_all_calls_route_through_daemon(fake_daemon):
    _try_connect_tcp("tcp://127.0.0.1:8456")
    _try_connect_unix("/run/callwarden/callwarden.sock")
    try_http_connect("http://127.0.0.1:8456")
    methods = [m for m, _ in fake_daemon.calls]
    assert methods == [
        "mcp.daemon_autostart.try_connect_tcp",
        "mcp.daemon_autostart.try_connect_unix",
        "mcp.daemon_autostart.try_http_connect",
    ]


def test_authority_params_forwarded_intact(fake_daemon):
    _try_connect_tcp("tcp://127.0.0.1:8456")
    _, params = fake_daemon.calls[-1]
    assert params["endpoint"] == "tcp://127.0.0.1:8456"
    assert params["timeout"] == 1.0  # CONNECT_TIMEOUT 默认转发

    try_http_connect("http://127.0.0.1:9999/health", timeout=3.5)
    _, params = fake_daemon.calls[-1]
    assert params == {"endpoint": "http://127.0.0.1:9999/health", "timeout": 3.5}


# ============================================================
# 4) unavailable（fail-closed 上抛，绝不回退本地 socket 探测）
# ============================================================


def test_unavailable_all_functions_raise(fake_daemon):
    fake_daemon.available = False
    with pytest.raises(DaemonUnavailableError):
        _try_connect_tcp("tcp://127.0.0.1:8456")
    with pytest.raises(DaemonUnavailableError):
        _try_connect_unix("/run/callwarden/callwarden.sock")
    with pytest.raises(DaemonUnavailableError):
        try_http_connect("http://127.0.0.1:8456")


# ============================================================
# 5) restart（不可用 → 恢复后立即成功，无缓存/状态污染）
# ============================================================


def test_restart_recovers_after_unavailable(fake_daemon):
    fake_daemon.available = False
    with pytest.raises(DaemonUnavailableError):
        try_http_connect("http://127.0.0.1:8456")
    fake_daemon.available = True
    assert try_http_connect("http://127.0.0.1:8456") is True
    result = _try_connect_tcp("tcp://127.0.0.1:8456")
    assert result["connectable"] is True


# ============================================================
# 零权威证据：AST 扫描（三个退役函数不再含本地 socket/URL 探测残留；
# 模块无 SQLite 权威残留。_transport_* 为 transport bootstrap，
# 物理必需且非 DB authority，不在扫描目标内。）
# ============================================================


def test_no_probe_authority_in_source():
    # 直接取已加载模块的 __file__，确保扫描的是当前生效（worktree）的迁移后源码
    import callwarden.server.daemon_autostart as da

    src_path = da.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        full_src = f.read()
    tree = ast.parse(full_src)

    banned_imports = {"sqlite3"}
    target_funcs = {"_try_connect_tcp", "_try_connect_unix", "try_http_connect"}
    banned_tokens = {
        "socket.socket",
        ".connect(",
        "urllib",
        "urlparse",
        "sqlite3",
        "get_db(",
        "PRAGMA ",
        "SELECT ",
    }

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name in banned_imports:
                    violations.append("import sqlite3")
        elif isinstance(node, ast.ImportFrom):
            if node.module in banned_imports:
                violations.append("from sqlite3 import")

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in target_funcs:
            # 仅扫描函数体实际代码（排除 docstring，docstring 可合法描述 daemon 行为）
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0].value, "value", None), str)
            ):
                body = body[1:]
            code_seg = "\n".join(ast.get_source_segment(full_src, s) or "" for s in body)
            for tok in banned_tokens:
                if tok in code_seg:
                    violations.append(f"{node.name}: contains {tok}")

    assert not violations, f"server/daemon_autostart.py 仍含探测权威残留: {violations}"
