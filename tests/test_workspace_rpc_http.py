r"""W1-1（T-1786808777378-bbcbf059）workspace 读面 HTTP native 修复单测。

背景：项目已默认 HTTP transport（CW_DAEMON_TRANSPORT=http，H6）。MCP 工具
HTTP 分支应经 `HttpDaemonRpcClient` 便捷方法调用，由便捷方法经
`_ensure_remote_snapshot(db_path)` 注入权威 `workspace_instance_id` 后
`self.call("workspace.*", params)`。参考 M2 系列模式：
`query_issues`/`query_tests`（先 _ensure_remote_snapshot，再注入
workspace_instance_id，最后 call）。

修复前缺陷（tools_workspace.py get_active_workspace HTTP 分支）：
调 `_call_daemon_rpc("workspace.activate", {})` 缺 workspace_instance_id
→ Rust `handle_workspace_activate`（workspace.rs）`require_str_param(params,
"workspace_instance_id")` 强制 → 返回 invalid_params。

Rust 侧路由（禁止改动，零 Rust 改动）：
- workspace.list：无参，按 peer uid 返回 daemon_workspaces 行数组
  （workspace_id/workspace_instance_id/snapshot_id/owner_uid/
  git_remote_url/git_head_commit_sha/client_view_root/host_real_root/
  toolchain_fingerprint/registered_at/last_active_at/status）
- workspace.status：强制 workspace_instance_id（owned ACL：
  owner_uid 匹配 + 非 archived，越权/不存在 → workspace_not_found）
- workspace.register：强制 client_view_root

覆盖矩阵（对齐统一验收标准 6 问）：
- HTTP 注入：workspace_status 便捷方法自动 register（+ 按需 publish）后
  注入权威 instance_id 调 workspace.status（无 db_path 跳过 publish）
- 复用：重复调用不重复 register
- 越界参数：register 响应缺 workspace_instance_id → DaemonUnavailableError
  （fail-closed，不静默成功）
- 工具层 HTTP 分支：get_active_workspace 调用便捷方法并传
  db_path=_get_db_path_for_daemon()；兼容映射（client_view_root→root_path、
  name=basename 兜底、host_real_root 保留）；list_workspaces 走 workspace.list
  并做逐行兼容映射
- 进程级 round-trip：生产 daemon 占用默认 HTTP 端口/权威 manifest 时
  设计性 skip（避免污染生产 registry 与覆盖 ~/.callwarden 权威 manifest，
  对齐 test_query_issues_rpc.py 管道占用 skip 模式）；否则启动隔离 daemon
  验证 register→status 命中 / status 缺参 invalid_params / 不存在
  instance_id → workspace_not_found。

前置条件（进程级部分，与 test_query_issues_rpc.py 一致）：
1. Windows 平台
2. 已构建 `cw-daemon.exe`：`cargo build --release --no-default-features
   --manifest-path rust_ext/Cargo.toml --bin cw-daemon`
3. 默认 HTTP endpoint（authority-scoped manifest）未被生产 daemon 占用
   （占用则 skip）
"""

import json
import os
import subprocess
import sys
import time

import pytest

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

class _WorkspaceStatusHarness:
    """workspace_status 便捷方法注入 harness（对齐 test_query_issues_rpc.py
    _HttpInjectionHarness 模式）。"""

    @staticmethod
    def _make_client(monkeypatch, register_ok=True):
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
        client._remote_workspace_id = None
        client._remote_snapshot_ready = False
        client._project_root = None
        calls = []

        def fake_call(method, params, request_id=None):
            calls.append((method, params))
            if method == "workspace.register":
                if not register_ok:
                    return {"workspace_id": 1}  # 缺少 workspace_instance_id
                return {"workspace_id": 1, "workspace_instance_id": "inst-w1"}
            if method == "snapshot.publish":
                return {"ok": True, "snapshot_id": "snap-w1"}
            if method == "workspace.status":
                return {
                    "workspace_id": 1,
                    "workspace_instance_id": "inst-w1",
                    "client_view_root": os.getcwd(),
                    "host_real_root": os.getcwd(),
                    "status": "active",
                }
            raise AssertionError(f"意外 method: {method}")

        monkeypatch.setattr(client, "call", fake_call)
        return client, calls


class TestHttpWorkspaceStatusInjection:
    """W1-1：HttpDaemonRpcClient.workspace_status 便捷方法自动注入权威
    workspace_instance_id（Rust handle_workspace_status 强制 require，
    缺注入返回 invalid_params——即 MCP get_active_workspace HTTP 分支
    修复前调 workspace.activate {} 的缺陷）。"""

    def test_http_workspace_status_registers_and_injects_instance_id(self, monkeypatch):
        client, calls = _WorkspaceStatusHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        result = client.workspace_status(db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "snapshot.publish", "workspace.status"], \
            f"调用序应为 register→publish→workspace.status，实际 {methods}"
        # register：client_view_root 默认取进程 cwd（与 legacy 对齐）
        assert calls[0][1] == {"client_view_root": os.getcwd()}
        # publish：注入权威 instance_id + 透传 db_path（abspath 规范化）
        assert calls[1][1]["workspace_instance_id"] == "inst-w1"
        assert calls[1][1]["db_path"] == os.path.abspath(db_path)
        # workspace.status：注入权威 instance_id —— 本修复的核心断言
        assert calls[2][1] == {"workspace_instance_id": "inst-w1"}
        assert result["workspace_instance_id"] == "inst-w1"
        assert result["status"] == "active"

    def test_http_workspace_status_without_db_path_skips_publish(self, monkeypatch):
        client, calls = _WorkspaceStatusHarness._make_client(monkeypatch)

        result = client.workspace_status()

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "workspace.status"], \
            f"无 db_path 时不应 publish，实际 {methods}"
        assert calls[-1][1] == {"workspace_instance_id": "inst-w1"}
        assert result["status"] == "active"

    def test_http_workspace_status_reuses_registered_workspace(self, monkeypatch):
        client, calls = _WorkspaceStatusHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        client.workspace_status(db_path=db_path)
        client.workspace_status(db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods.count("workspace.register") == 1, "重复调用不得重复 register"
        assert methods.count("snapshot.publish") == 1, "重复调用不得重复 publish"
        assert methods.count("workspace.status") == 2
        assert calls[-1][1]["workspace_instance_id"] == "inst-w1"

    def test_http_workspace_status_register_missing_instance_id_raises(self, monkeypatch):
        from callwarden.server.daemon_client import DaemonUnavailableError
        client, _calls = _WorkspaceStatusHarness._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.workspace_status(db_path=os.path.join(os.getcwd(), "snap.db"))


# ----------------------------------------------------------------------
# 工具层 HTTP 分支单测（mock HttpDaemonRpcClient + _get_db_path_for_daemon）
# ----------------------------------------------------------------------

class TestToolsWorkspaceHttpBranches:
    """tools_workspace.py 读面工具 HTTP 分支单测。

    - get_active_workspace：HTTP 模式必须调用 client.workspace_status 便捷
      方法并传 db_path=_get_db_path_for_daemon()（修复前调
      workspace.activate {} 缺注入）；返回结构做 legacy 兼容映射。
    - list_workspaces：HTTP 模式走 workspace.list 并做逐行兼容映射。
    """

    @pytest.fixture
    def mock_http_mode(self, monkeypatch):
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: True,
        )

    @pytest.fixture
    def mock_db_path(self, monkeypatch):
        fake = r"C:\fake\ws\db.sqlite"
        monkeypatch.setattr(
            "callwarden.server._mcp_common._get_db_path_for_daemon",
            lambda: fake,
        )
        return fake

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

    # -- get_active_workspace -------------------------------------------------

    def test_get_active_workspace_http_calls_convenience_with_db_path(
        self, monkeypatch, mock_http_mode, mock_db_path
    ):
        from callwarden.server.daemon_client import HttpDaemonRpcClient

        client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
        calls = []
        daemon_row = {
            "workspace_id": 7,
            "workspace_instance_id": "inst-w1-tool",
            "snapshot_id": "snap-1",
            "owner_uid": 1001,
            "git_remote_url": "",
            "git_head_commit_sha": "",
            "client_view_root": r"C:\fake\ws",
            "host_real_root": r"C:\fake\ws",
            "toolchain_fingerprint": "fp",
            "registered_at": 1700000000.0,
            "last_active_at": 1700000001.0,
            "status": "active",
        }

        def fake_workspace_status(db_path=None):
            calls.append(db_path)
            return dict(daemon_row)

        monkeypatch.setattr(client, "workspace_status", fake_workspace_status)
        monkeypatch.setattr(HttpDaemonRpcClient, "get_instance", classmethod(lambda cls: client))

        tools = self._register_tools()
        result = tools["get_active_workspace"]()

        assert calls == [mock_db_path], \
            f"便捷方法必须收到 db_path=_get_db_path_for_daemon()，实际 {calls}"
        # 兼容映射：client_view_root→root_path、name 用 basename 兜底、
        # host_real_root 保留、daemon 行其余字段透传
        assert result["root_path"] == r"C:\fake\ws"
        assert result["name"] == "ws"
        assert result["host_real_root"] == r"C:\fake\ws"
        assert result["workspace_instance_id"] == "inst-w1-tool"
        assert result["status"] == "active"
        assert result["workspace_id"] == 7

    def test_get_active_workspace_http_returns_none(self, monkeypatch, mock_http_mode, mock_db_path):
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
        monkeypatch.setattr(client, "workspace_status", lambda db_path=None: None)
        monkeypatch.setattr(HttpDaemonRpcClient, "get_instance", classmethod(lambda cls: client))

        tools = self._register_tools()
        assert tools["get_active_workspace"]() is None

    def test_get_active_workspace_http_passthrough_without_client_view_root(
        self, monkeypatch, mock_http_mode, mock_db_path
    ):
        """daemon 行无 client_view_root（异常形态）时直接透传，不做映射。"""
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
        odd_row = {"workspace_instance_id": "inst-odd", "status": "active"}
        monkeypatch.setattr(client, "workspace_status", lambda db_path=None: dict(odd_row))
        monkeypatch.setattr(HttpDaemonRpcClient, "get_instance", classmethod(lambda cls: client))

        tools = self._register_tools()
        result = tools["get_active_workspace"]()
        assert result == odd_row

    def test_get_active_workspace_legacy_when_not_http(self, monkeypatch):
        """非 HTTP 模式保持 legacy db.get_active_workspace()（workspaces 表行）。"""
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: False,
        )
        with patch.object(tools_workspace, "get_db") as mock_get_db:
            mock_db = MagicMock()
            legacy_row = {"id": 1, "name": "ws1", "root_path": r"C:\ws1", "is_active": 1}
            mock_db.get_active_workspace.return_value = legacy_row
            mock_get_db.return_value = mock_db
            tools = self._register_tools()
            result = tools["get_active_workspace"]()
            assert result == legacy_row
            mock_db.get_active_workspace.assert_called_once_with()

    # -- list_workspaces ------------------------------------------------------

    def test_list_workspaces_http_maps_daemon_rows(self, mock_http_mode):
        from unittest.mock import patch
        from callwarden.server.tools import tools_workspace
        daemon_rows = [
            {
                "workspace_id": 1,
                "workspace_instance_id": "inst-a",
                "client_view_root": r"C:\proj\alpha",
                "host_real_root": r"C:\proj\alpha",
                "status": "active",
            },
            {
                "workspace_id": 2,
                "workspace_instance_id": "inst-b",
                "client_view_root": r"C:\proj\beta",
                "host_real_root": r"C:\proj\beta",
                "status": "active",
            },
        ]
        with patch.object(tools_workspace, "_call_daemon_rpc") as mock_rpc:
            mock_rpc.return_value = daemon_rows
            tools = self._register_tools()
            result = tools["list_workspaces"]()

        mock_rpc.assert_called_once_with("workspace.list", {})
        assert len(result) == 2
        assert result[0]["root_path"] == r"C:\proj\alpha"
        assert result[0]["name"] == "alpha"
        assert result[0]["host_real_root"] == r"C:\proj\alpha"
        assert result[0]["workspace_instance_id"] == "inst-a"
        assert result[1]["root_path"] == r"C:\proj\beta"
        assert result[1]["name"] == "beta"

    def test_list_workspaces_http_handles_empty(self, mock_http_mode):
        from unittest.mock import patch
        from callwarden.server.tools import tools_workspace
        with patch.object(tools_workspace, "_call_daemon_rpc") as mock_rpc:
            mock_rpc.return_value = []
            tools = self._register_tools()
            assert tools["list_workspaces"]() == []

    def test_list_workspaces_legacy_when_not_http(self, monkeypatch):
        from unittest.mock import MagicMock, patch
        from callwarden.server.tools import tools_workspace
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: False,
        )
        with patch.object(tools_workspace, "get_db") as mock_get_db:
            mock_db = MagicMock()
            legacy_rows = [{"id": 1, "name": "ws1", "root_path": r"C:\ws1", "is_active": 1}]
            mock_db.list_workspaces.return_value = legacy_rows
            mock_get_db.return_value = mock_db
            tools = self._register_tools()
            result = tools["list_workspaces"]()
            assert result == legacy_rows
            mock_db.list_workspaces.assert_called_once_with()


# ----------------------------------------------------------------------
# 进程级 round-trip（设计性 skip：生产 daemon 占用默认 HTTP 端口时跳过）
# ----------------------------------------------------------------------

def _http_manifest_occupied() -> bool:
    """判断权威 HTTP manifest 是否"占用"（有效或 stale 均视为占用）。

    生产 daemon（transport=http）运行中时 manifest 有效 → True（skip）。
    manifest 存在但 stale（PID 已死，例如隔离测试 daemon 残留）→ 也返回
    True：此时无法可靠区分"生产 daemon 仍在跑但 manifest 被覆盖"与"确无
    daemon"，而启动隔离 daemon 必然覆盖 ~/.callwarden 权威 manifest（H6
    固定目录），属于禁止路径；保守 skip 对齐 test_query_issues_rpc.py 的
    设计性 skip 模式。仅当 E_HTTP_MANIFEST_MISSING（manifest 完全不存在，
    即从未有 daemon 发布过）才返回 False 允许启动隔离 daemon。
    """
    from callwarden.config import get_http_authority_id
    from callwarden.server.daemon_autostart import resolve_http_endpoint_and_manifest
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    try:
        endpoint, _manifest = resolve_http_endpoint_and_manifest(
            authority_id=get_http_authority_id()
        )
    except DaemonRemoteError as exc:
        # E_HTTP_MANIFEST_MISSING：确实无 manifest → 允许隔离 daemon
        if getattr(exc, "code", "") == "E_HTTP_MANIFEST_MISSING":
            return False
        # E_HTTP_MANIFEST_STALE 等：manifest 存在但不可用 → 保守视为占用
        return True
    client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
    client._resolved_endpoint = endpoint
    try:
        resp = client.call("ping")
        return not (isinstance(resp, dict) and resp.get("status") == "ok")
    except Exception:
        # manifest 有效但 ping 失败：同样保守视为占用（可能是竞态）
        return True


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 管道），启用 HTTP transport。

    对齐 test_http_native_read_cutover.py _spawn_isolated_daemon：经环境变量
    配置隔离数据目录，`--http-bind 127.0.0.1:0` 随机端口避免与生产冲突。
    注意：HTTP manifest 仍写入 ~/.callwarden（http_manifest_dir 固定），仅
    在确认生产 daemon 未运行时才允许本路径（测试结束 terminate 后，生产
    daemon 下次启动会重新发布权威 manifest）。
    """
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
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配）。"""
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
    """终止 daemon 进程（terminate 优先，兜底 kill）。"""
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


class TestRealDaemonWorkspaceRoundTrip:
    """真实 daemon 进程级 workspace.status/list round-trip（设计性 skip）。

    生产 daemon（transport=http）占用默认 HTTP 端口与 ~/.callwarden 权威
    manifest 时跳过：直接连生产 daemon 会污染生产 registry（daemon_workspaces
    表），启动隔离 daemon 会覆盖权威 manifest，均不可接受 —— 对齐
    test_query_issues_rpc.py 的"默认管道被占用则 skip"设计性 skip 模式。
    无 daemon 响应时才启动隔离 daemon（随机端口）验证拒绝矩阵。
    """

    @pytest.fixture
    def real_daemon_client(self, tmp_path):
        if _http_manifest_occupied():
            pytest.skip(
                "权威 HTTP manifest 被占用（生产 daemon 运行中或残留 stale "
                "manifest）；为避免污染生产 registry（daemon_workspaces 表）与"
                "覆盖 ~/.callwarden 权威 manifest，进程级 round-trip 设计性 "
                "skip（对齐 test_query_issues_rpc.py 管道占用 skip 模式）"
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
    def test_register_then_status_hit(self, real_daemon_client):
        """a) workspace.register 成功拿 instance_id；b) workspace.status 命中。"""
        import tempfile
        root = tempfile.mkdtemp(prefix="cw_w1_ws_")
        try:
            reg = real_daemon_client.call("workspace.register", {"client_view_root": root})
            instance_id = reg["workspace_instance_id"]
            assert isinstance(instance_id, str) and instance_id
            status = real_daemon_client.call(
                "workspace.status", {"workspace_instance_id": instance_id}
            )
            assert status["workspace_instance_id"] == instance_id
            assert status["client_view_root"] == root
            assert status["status"] == "active"
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    @requires_binaries
    def test_list_hits_current_workspace(self, real_daemon_client):
        """c) workspace.list 命中当前注册 workspace（peer uid 限定）。"""
        import tempfile
        root = tempfile.mkdtemp(prefix="cw_w1_ls_")
        try:
            reg = real_daemon_client.call("workspace.register", {"client_view_root": root})
            instance_id = reg["workspace_instance_id"]
            rows = real_daemon_client.call("workspace.list", {})
            assert isinstance(rows, list)
            ids = [r["workspace_instance_id"] for r in rows]
            assert instance_id in ids, f"workspace.list 应命中当前 workspace，实际 {ids}"
        finally:
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    @requires_binaries
    def test_status_unknown_instance_rejected(self, real_daemon_client):
        """d) 不存在 instance_id 的 workspace.status → workspace_not_found。"""
        with pytest.raises(DaemonRemoteError) as exc:
            real_daemon_client.call(
                "workspace.status", {"workspace_instance_id": "deadbeefdeadbeef01"}
            )
        assert exc.value.code == "workspace_not_found"

    @requires_binaries
    def test_status_missing_instance_id_invalid_params(self, real_daemon_client):
        """e) 缺 workspace_instance_id 的 workspace.status → invalid_params。"""
        with pytest.raises(DaemonRemoteError) as exc:
            real_daemon_client.call("workspace.status", {})
        assert exc.value.code == "invalid_params"
