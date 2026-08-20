r"""W1-2（T-1786808777379-15702f0c）workspace 写面 HTTP daemon 通道单测。

背景：项目已默认 HTTP transport（CW_DAEMON_TRANSPORT=http，H6）。MCP 写面
工具 register_workspace / set_active_workspace / delete_workspace 在 HTTP 模式
必须经 daemon RPC 同步注册表（daemon_workspaces 是读面 workspace.list/status
的数据源），同时保持 SQLite workspaces 表为真相源（避免双表分裂）。

桥接设计（零 Rust 改动，Rust handler 契约见 workspace.rs L1332-1417）：
- `HttpDaemonRpcClient.workspace_register(root_path)`：RPC workspace.register
  （强制 client_view_root），响应以 workspace_instance_id 为权威；缺字段抛
  DaemonUnavailableError（fail-closed）。
- `HttpDaemonRpcClient.workspace_activate(root_path)` /
  `workspace_remove(root_path)`：先按 root_path 解析权威 workspace_instance_id
  （内存缓存 `_workspace_instance_by_root` 优先，缺省幂等 register 确定性
  重算——workspaces 表无 instance_id 列且禁改 schema 的最小侵入映射），再调
  workspace.activate / workspace.remove。
- 工具层 HTTP 分支（is_http_transport_enabled() 门控）：SQLite 真相源先行，
  daemon 同步随后；daemon 不可用 → DaemonUnavailableError（fail-closed，
  禁止静默回退纯 SQL）。local 模式行为保持纯 SQL 不变。

Rust 侧语义：
- workspace.register：INSERT OR REPLACE（幂等），instance_id =
  sha256(owner_uid|host_real_root|git_remote_url|git_head_commit_sha)[:16]。
- workspace.activate：owned ACL（owner_uid 匹配，任意状态可激活）→ status=active。
- workspace.remove：owned ACL → status=archived（软删语义，读面 owned ACL
  已排除 archived 行）。

覆盖矩阵（对齐统一验收标准 6 问）：
- HTTP 注入：写面便捷方法自动 register（幂等）后注入权威 instance_id；
  缓存复用不重复 register；register 响应缺 instance_id → DaemonUnavailableError
- 工具层 HTTP 分支：三写工具 SQLite 真相源先行 + daemon 同步；返回语义不变；
  daemon 不可用 → DaemonUnavailableError 传播（fail-closed，无静默 SQL 回退）
- 越界参数：workspace 不存在 → False（不调 daemon）；register 缺 instance_id
  → DaemonUnavailableError
- 跨 workspace 隔离：root_path 为 join key，不同 root 映射不同 instance_id
- Python fallback 边界：非 HTTP（legacy）模式三工具走纯 SQL，不触碰 daemon
- 进程级 round-trip：真实 daemon register→activate→remove 三态验证
  （remove 后 workspace.status → workspace_not_found，即读面不可见）

前置条件（进程级部分，与 test_workspace_rpc_http.py 一致）：
1. Windows 平台
2. 已构建 `cw-daemon.exe`（cargo build --release --no-default-features
   --manifest-path rust_ext/Cargo.toml --bin cw-daemon）
3. 默认 HTTP endpoint（authority-scoped manifest）未被生产 daemon 占用
   （占用则设计性 skip，避免污染生产 registry 与覆盖 ~/.callwarden 权威
   manifest）
"""

import json
import os
import subprocess
import sys
import tempfile
import time

import pytest

from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.daemon_protocol import DaemonRemoteError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="进程级 workspace round-trip 需要 Windows + loopback HTTP daemon",
)

requires_binaries = pytest.mark.skipif(
    not os.path.exists(_DAEMON_BIN),
    reason="cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）",
)


# ----------------------------------------------------------------------
# HTTP 便捷方法注入 harness（禁真实 daemon，仅 mock call）
# ----------------------------------------------------------------------

class _WriteClientHarness:
    """写面便捷方法注入 harness（对齐 test_workspace_rpc_http.py 模式）。"""

    @staticmethod
    def _make_client(monkeypatch, register_ok=True, root=None):
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
        client._remote_workspace_id = None
        client._remote_snapshot_ready = False
        client._project_root = None
        client._workspace_instance_by_root = {}
        root = root or os.getcwd()
        calls = []

        def fake_call(method, params, request_id=None):
            calls.append((method, params))
            if method == "workspace.register":
                if not register_ok:
                    return {"workspace_id": 1}  # 缺少 workspace_instance_id
                return {"workspace_id": 1, "workspace_instance_id": "inst-w2"}
            if method == "workspace.activate":
                return {
                    "workspace_id": 1,
                    "workspace_instance_id": "inst-w2",
                    "client_view_root": root,
                    "host_real_root": root,
                    "status": "active",
                }
            if method == "workspace.remove":
                return {
                    "workspace_id": 1,
                    "workspace_instance_id": "inst-w2",
                    "client_view_root": root,
                    "host_real_root": root,
                    "status": "archived",
                }
            raise AssertionError(f"意外 method: {method}")

        monkeypatch.setattr(client, "call", fake_call)
        return client, calls


class TestHttpWriteConvenienceMethods:
    """W1-2：HttpDaemonRpcClient 写面便捷方法自动 register 注入权威
    workspace_instance_id（Rust handler 强制 require，缺注入返回
    invalid_params）。"""

    def test_workspace_register_registers_and_caches(self, monkeypatch):
        client, calls = _WriteClientHarness._make_client(monkeypatch)
        root = os.getcwd()

        row = client.workspace_register(root)

        assert calls == [("workspace.register", {"client_view_root": root})]
        assert row["workspace_instance_id"] == "inst-w2"
        assert client._workspace_instance_by_root[
            _norm_key(root)
        ] == "inst-w2"

    def test_workspace_activate_registers_then_activates(self, monkeypatch):
        client, calls = _WriteClientHarness._make_client(monkeypatch)
        root = os.getcwd()

        row = client.workspace_activate(root)

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "workspace.activate"], \
            f"调用序应为 register→activate，实际 {methods}"
        # 核心断言：activate 注入权威 instance_id（Rust 强制 require）
        assert calls[1][1] == {"workspace_instance_id": "inst-w2"}
        assert row["status"] == "active"

    def test_workspace_remove_registers_then_archives(self, monkeypatch):
        client, calls = _WriteClientHarness._make_client(monkeypatch)
        root = os.getcwd()

        row = client.workspace_remove(root)

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "workspace.remove"]
        assert calls[1][1] == {"workspace_instance_id": "inst-w2"}
        assert row["status"] == "archived"

    def test_resolve_reuses_cache_without_duplicate_register(self, monkeypatch):
        client, calls = _WriteClientHarness._make_client(monkeypatch)
        root = os.getcwd()

        client.workspace_activate(root)
        client.workspace_activate(root)

        methods = [m for m, _ in calls]
        assert methods.count("workspace.register") == 1, "缓存命中后不得重复 register"
        assert methods.count("workspace.activate") == 2
        assert calls[-1][1]["workspace_instance_id"] == "inst-w2"

    def test_norm_root_cache_key_merges_case_and_separators(self, monkeypatch):
        """_norm_root 规范化：`C:\foo` 与 `c:/foo` 命中同一缓存 key。"""
        client, calls = _WriteClientHarness._make_client(monkeypatch, root=r"C:\foo")
        client.workspace_activate(r"C:\foo")
        client.workspace_activate(r"c:/foo")

        methods = [m for m, _ in calls]
        assert methods.count("workspace.register") == 1, \
            f"两种写法应命中同一缓存 key，实际 {methods}"

    def test_register_missing_instance_id_raises(self, monkeypatch):
        client, _calls = _WriteClientHarness._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.workspace_register(os.getcwd())

    def test_activate_register_missing_instance_id_raises(self, monkeypatch):
        client, _calls = _WriteClientHarness._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.workspace_activate(os.getcwd())

    def test_remove_register_missing_instance_id_raises(self, monkeypatch):
        client, _calls = _WriteClientHarness._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.workspace_remove(os.getcwd())


def _norm_key(p: str) -> str:
    """测试侧复刻 _norm_root（不 import 私有函数，避免过度耦合）。"""
    normalized = p.replace("\\", "/")
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        normalized = normalized[0].lower() + normalized[1:]
    return normalized


# ----------------------------------------------------------------------
# 工具层 HTTP 分支单测（mock HttpDaemonRpcClient + get_db）
# ----------------------------------------------------------------------

class TestWriteToolsHttpBranches:
    """tools_workspace.py 三写工具 HTTP 分支单测。

    - register_workspace：SQLite 真相源先行（db.register_workspace 幂等返回
      ws_id），再 client.workspace_register(root_path)；返回 ws_id 语义不变
    - set_active_workspace：db.set_active_workspace 成功后再
      client.workspace_activate(row.root_path)；workspace 不存在 → False
    - delete_workspace：先 _find_workspace_root 解析 root_path，再 SQLite 硬删，
      再 client.workspace_remove(root_path)；不存在 → False
    - fail-closed：daemon 不可用（DaemonUnavailableError）→ 传播，不静默
      回退纯 SQL；legacy（非 HTTP）模式纯 SQL 不触碰 daemon
    """

    @pytest.fixture
    def mock_http_mode(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: True,
        )

    @pytest.fixture
    def mock_legacy_mode(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: False,
        )

    def _register_tools(self):
        from unittest.mock import MagicMock
        from callwarden.server.tools import tools_workspace
        mcp = MagicMock()
        registrations = {}

        def tool_capture(name=None):
            def decorator(fn):
                registrations[fn.__name__] = fn
                return fn
            return decorator

        mcp.tool = tool_capture
        tools_workspace.register(mcp)
        return registrations

    # -- register_workspace ------------------------------------------------

    def test_register_workspace_http_sqlite_then_daemon_sync(
        self, mock_http_mode, monkeypatch
    ):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.register_workspace.return_value = 42
        client = MagicMock()
        client.workspace_register.return_value = {
            "workspace_id": 7,
            "workspace_instance_id": "inst-r",
            "client_view_root": r"C:\ws1",
            "status": "active",
        }
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["register_workspace"]("ws1", r"C:\ws1", "desc")

        # SQLite 真相源先行
        mock_db.register_workspace.assert_called_once_with("ws1", r"C:\ws1", "desc")
        # daemon 同步随后
        client.workspace_register.assert_called_once_with(r"C:\ws1")
        # 返回语义不变（int ws_id）
        assert result == 42

    def test_register_workspace_http_daemon_unavailable_raises(
        self, mock_http_mode, monkeypatch
    ):
        """fail-closed：daemon 不可用 → DaemonUnavailableError 传播，禁止
        静默回退纯 SQL（否则读面 workspace.list 看不到新 workspace）。"""
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.register_workspace.return_value = 42
        client = MagicMock()
        client.workspace_register.side_effect = DaemonUnavailableError("daemon down")
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            with pytest.raises(DaemonUnavailableError):
                tools["register_workspace"]("ws1", r"C:\ws1")

    def test_register_workspace_legacy_pure_sql(self, mock_legacy_mode, monkeypatch):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.register_workspace.return_value = 42
        client = MagicMock()
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["register_workspace"]("ws1", r"C:\ws1")

        mock_db.register_workspace.assert_called_once_with("ws1", r"C:\ws1", "")
        client.workspace_register.assert_not_called()
        assert result == 42

    # -- set_active_workspace ----------------------------------------------

    def test_set_active_workspace_http_sqlite_then_daemon_activate(
        self, mock_http_mode, monkeypatch
    ):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.set_active_workspace.return_value = True
        mock_db.get_active_workspace.return_value = {
            "id": 3,
            "name": "ws3",
            "root_path": r"C:\ws3",
            "is_active": 1,
        }
        client = MagicMock()
        client.workspace_activate.return_value = {
            "workspace_id": 3,
            "workspace_instance_id": "inst-a",
            "status": "active",
        }
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["set_active_workspace"]("3")

        assert result is True
        mock_db.set_active_workspace.assert_called_once_with(3)
        # daemon 同步注入 SQLite 真相源行的 root_path
        client.workspace_activate.assert_called_once_with(r"C:\ws3")

    def test_set_active_workspace_http_by_name(
        self, mock_http_mode, monkeypatch
    ):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.set_active_workspace.return_value = True
        mock_db.get_active_workspace.return_value = {
            "id": 4,
            "name": "ws4",
            "root_path": r"C:\ws4",
        }
        client = MagicMock()
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["set_active_workspace"]("ws4")

        assert result is True
        mock_db.set_active_workspace.assert_called_once_with("ws4")
        client.workspace_activate.assert_called_once_with(r"C:\ws4")

    def test_set_active_workspace_http_not_found_no_daemon(
        self, mock_http_mode, monkeypatch
    ):
        """越界参数：workspace 不存在 → False，不调 daemon。"""
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.set_active_workspace.return_value = False
        client = MagicMock()
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["set_active_workspace"]("nope")

        assert result is False
        client.workspace_activate.assert_not_called()

    def test_set_active_workspace_http_daemon_unavailable_raises(
        self, mock_http_mode, monkeypatch
    ):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.set_active_workspace.return_value = True
        mock_db.get_active_workspace.return_value = {
            "id": 5,
            "name": "ws5",
            "root_path": r"C:\ws5",
        }
        client = MagicMock()
        client.workspace_activate.side_effect = DaemonUnavailableError("daemon down")
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            with pytest.raises(DaemonUnavailableError):
                tools["set_active_workspace"]("5")

    def test_set_active_workspace_legacy_pure_sql(self, mock_legacy_mode, monkeypatch):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.set_active_workspace.return_value = True
        client = MagicMock()
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["set_active_workspace"]("7")

        assert result is True
        mock_db.set_active_workspace.assert_called_once_with(7)
        client.workspace_activate.assert_not_called()

    # -- delete_workspace ----------------------------------------------------

    def test_delete_workspace_http_resolves_root_then_sql_then_daemon(
        self, mock_http_mode, monkeypatch
    ):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.list_workspaces.return_value = [
            {"id": 9, "name": "ws9", "root_path": r"C:\ws9"},
        ]
        mock_db.delete_workspace.return_value = True
        client = MagicMock()
        client.workspace_remove.return_value = {
            "workspace_id": 9,
            "workspace_instance_id": "inst-d",
            "status": "archived",
        }
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["delete_workspace"]("9")

        assert result is True
        # 先解析 root_path（SQLite 删除前），再 SQLite 硬删（真相源），
        # 再 daemon workspace.remove 归档同步
        mock_db.delete_workspace.assert_called_once_with(9)
        client.workspace_remove.assert_called_once_with(r"C:\ws9")

    def test_delete_workspace_http_by_name(
        self, mock_http_mode, monkeypatch
    ):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.list_workspaces.return_value = [
            {"id": 10, "name": "ws10", "root_path": r"C:\ws10"},
        ]
        mock_db.delete_workspace.return_value = True
        client = MagicMock()
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["delete_workspace"]("ws10")

        assert result is True
        mock_db.delete_workspace.assert_called_once_with("ws10")
        client.workspace_remove.assert_called_once_with(r"C:\ws10")

    def test_delete_workspace_http_not_found_no_daemon(
        self, mock_http_mode, monkeypatch
    ):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.list_workspaces.return_value = []
        client = MagicMock()
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["delete_workspace"]("missing")

        assert result is False
        mock_db.delete_workspace.assert_not_called()
        client.workspace_remove.assert_not_called()

    def test_delete_workspace_http_daemon_unavailable_raises(
        self, mock_http_mode, monkeypatch
    ):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.list_workspaces.return_value = [
            {"id": 11, "name": "ws11", "root_path": r"C:\ws11"},
        ]
        mock_db.delete_workspace.return_value = True
        client = MagicMock()
        client.workspace_remove.side_effect = DaemonUnavailableError("daemon down")
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            with pytest.raises(DaemonUnavailableError):
                tools["delete_workspace"]("11")

    def test_delete_workspace_legacy_pure_sql(self, mock_legacy_mode, monkeypatch):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace

        mock_db = MagicMock()
        mock_db.delete_workspace.return_value = True
        client = MagicMock()
        with patch.object(tools_workspace, "get_db", return_value=mock_db), patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ):
            tools = self._register_tools()
            result = tools["delete_workspace"]("12")

        assert result is True
        mock_db.delete_workspace.assert_called_once_with(12)
        client.workspace_remove.assert_not_called()


# ----------------------------------------------------------------------
# 进程级 round-trip（设计性 skip：生产 daemon 占用默认 HTTP 端口时跳过）
# ----------------------------------------------------------------------

def _http_manifest_occupied() -> bool:
    """判断权威 HTTP manifest 是否"占用"（有效或 stale 均视为占用）。

    对齐 test_workspace_rpc_http.py：生产 daemon（transport=http）运行中时
    manifest 有效 → True（skip）；manifest 存在但 stale → 保守视为占用。
    仅当 E_HTTP_MANIFEST_MISSING（manifest 完全不存在）才返回 False。
    """
    from callwarden.config import get_http_authority_id
    from callwarden.server.daemon_autostart import resolve_http_endpoint_and_manifest
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    try:
        endpoint, _manifest = resolve_http_endpoint_and_manifest(
            authority_id=get_http_authority_id()
        )
    except DaemonRemoteError as exc:
        if getattr(exc, "code", "") == "E_HTTP_MANIFEST_MISSING":
            return False
        return True
    client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
    client._resolved_endpoint = endpoint
    try:
        resp = client.call("ping")
        return not (isinstance(resp, dict) and resp.get("status") == "ok")
    except Exception:
        return True


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 管道），启用 HTTP transport。"""
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _wait_isolated_manifest(proc, timeout=15.0):
    from callwarden.config import get_http_authority_id
    authority = get_http_authority_id()
    safe = authority.replace("/", "_").replace("\\", "_").replace(":", "_")
    manifest_path = os.path.join(
        os.path.expanduser("~"), ".callwarden",
        f"http-daemon.{safe}.manifest.json",
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                time.sleep(0.2)
                continue
            if m.get("pid") == proc.pid:
                return m
        time.sleep(0.2)
    return None


def _terminate(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class TestRealDaemonWriteRoundTrip:
    """真实 daemon 进程级写面 round-trip（设计性 skip，对齐 W1-1）。

    register→activate→remove 三态：register 幂等拿 instance_id；
    workspace.status 命中 active；workspace.remove 归档后 workspace.status
    → workspace_not_found（读面不可见，即"删除"对调用方生效）；越权参数
    拒绝（不存在 instance_id → workspace_not_found）。
    """

    @pytest.fixture
    def real_daemon_client(self, tmp_path):
        if _http_manifest_occupied():
            pytest.skip(
                "权威 HTTP manifest 被占用（生产 daemon 运行中或残留 stale "
                "manifest）；为避免污染生产 registry（daemon_workspaces 表）"
                "与覆盖 ~/.callwarden 权威 manifest，进程级 round-trip 设计性 "
                "skip（对齐 test_workspace_rpc_http.py skip 模式）"
            )
        bin_path = _DAEMON_BIN
        if not os.path.exists(bin_path):
            pytest.skip("cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）")
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        proc = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
        try:
            manifest = _wait_isolated_manifest(proc)
            if manifest is None:
                pytest.fail("隔离 daemon 未在超时内发布 manifest")
            from callwarden.server.daemon_client import HttpDaemonRpcClient
            client = HttpDaemonRpcClient(
                endpoint=manifest["endpoint"],
                verify_health=False,
                timeout=5.0,
            )
            yield client
        finally:
            _terminate(proc)

    @requires_binaries
    def test_register_activate_remove_roundtrip(self, real_daemon_client):
        """a) register 幂等拿 instance_id；b) activate 后 status=active；
        c) remove 后 status → workspace_not_found（读面不可见）。"""
        root = tempfile.mkdtemp(prefix="cw_w12_ws_")
        try:
            # 注册（幂等）→ 权威 instance_id
            reg1 = real_daemon_client.workspace_register(root)
            instance_id = reg1["workspace_instance_id"]
            assert isinstance(instance_id, str) and instance_id
            reg2 = real_daemon_client.workspace_register(root)
            assert reg2["workspace_instance_id"] == instance_id, "register 应幂等"

            # activate → status=active
            act = real_daemon_client.workspace_activate(root)
            assert act["workspace_instance_id"] == instance_id
            assert act["status"] == "active"

            # remove（archive 软删）→ status=archived
            rem = real_daemon_client.workspace_remove(root)
            assert rem["workspace_instance_id"] == instance_id
            assert rem["status"] == "archived"

            # 读面不可见：workspace.status → workspace_not_found（owned ACL
            # 排除 archived 行，对调用方呈现"已删除"）
            with pytest.raises(DaemonRemoteError) as exc:
                real_daemon_client.call(
                    "workspace.status", {"workspace_instance_id": instance_id}
                )
            assert exc.value.code == "workspace_not_found"
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    @requires_binaries
    def test_write_unknown_instance_rejected(self, real_daemon_client):
        """d) 不存在 instance_id 的 workspace.activate → workspace_not_found
        （fail-closed，不静默成功）。"""
        with pytest.raises(DaemonRemoteError) as exc:
            real_daemon_client.call(
                "workspace.activate", {"workspace_instance_id": "deadbeefdeadbeef01"}
            )
        assert exc.value.code == "workspace_not_found"

    @requires_binaries
    def test_write_missing_instance_id_invalid_params(self, real_daemon_client):
        """e) 缺 workspace_instance_id 的 workspace.remove → invalid_params
        （Rust handler 强制 require_str_param，fail-closed）。"""
        with pytest.raises(DaemonRemoteError) as exc:
            real_daemon_client.call("workspace.remove", {})
        assert exc.value.code == "invalid_params"
