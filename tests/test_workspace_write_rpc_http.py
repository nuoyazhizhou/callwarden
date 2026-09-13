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
- workspace.remove：owned ACL → status=archived（软删语义）。

覆盖矩阵（对齐统一验收标准 6 问）：
- HTTP 注入：写面便捷方法自动 register（幂等）后注入权威 instance_id；
  缓存复用不重复 register；register 响应缺 instance_id → DaemonUnavailableError
- 工具层薄壳路由：三写工具经 `_route(<rpc method>, {...}, 'PROTECTED_MUTATION')`
  下发，参数逐字透传、原样回包、不触碰本地 db
- 跨 workspace 隔离：root_path 为 join key，不同 root 映射不同 instance_id
- 进程级 round-trip：真实 daemon register→activate→remove 三态验证
  （remove 后 workspace.status 经统一权威返回 status=archived）

陈旧期望修正（PYT 回归 step#4，2026-09）——对齐生产演进后的现状：
1. 三写工具已随 T03 收敛为一行式
   `_route(<rpc method>, {...}, 'PROTECTED_MUTATION')`
   （server/tools/tools_workspace.py:107 register_workspace、:121
   set_active_workspace、:138 delete_workspace）。HTTP/local 分流、SQLite 真相源
   先行写入、daemon 便捷方法同步、fail-closed 传播全部下沉到
   server/daemon_client.py::route_rpc（L3880-3994）。故工具层测试改为断言薄壳
   路由契约，不再 mock tools_workspace.get_db / HttpDaemonRpcClient.get_instance /
   is_http_transport_enabled（这些 seam 已无调用点）。
2. `workspace_register` 便捷方法现经 `_workspace_snapshot_metadata` 附带 git
   provenance（server/daemon_client.py:3290-3291、1091-1113），register params
   含 git_remote_url / git_head_commit_sha，不再等于仅 client_view_root 的单键 dict。
3. `workspace.status` 现经统一权威解析（rust_ext/src/daemon/snapshot_state.rs:478-527
   → task_collab.rs:1756-1772 → workspace_reconciliation.rs:211+），
   registry `get_workspace_status`（workspace.rs:578-595）不过滤 archived，故
   workspace.remove 归档后 workspace.status 返回 status="archived" 而非抛错
   （旧断言"读面不可见 → workspace_not_found"陈旧）。

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

        # stale 依据：workspace_register 现经 _workspace_snapshot_metadata 附加
        # git provenance（server/daemon_client.py:3290-3291、1091-1113）——
        # register params 不再等于仅 client_view_root 的单键 dict（旧断言陈旧）。
        methods = [m for m, _ in calls]
        assert methods == ["workspace.register"]
        assert calls[0][1]["client_view_root"] == root
        assert set(calls[0][1]) <= {
            "client_view_root", "git_remote_url", "git_head_commit_sha",
        }
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


def _route_recorder(monkeypatch, module, result):
    """把模块级 `_route` 替换为记录器，返回记录列表 [(method, params, op_class)]。"""
    calls = []

    def _fake(method, params, op_class):
        calls.append((method, dict(params), op_class))
        return result

    monkeypatch.setattr(module, "_route", _fake)
    return calls


# ----------------------------------------------------------------------
# 工具层薄壳路由单测（对齐 T03 收敛后的 `_route` 契约）
# ----------------------------------------------------------------------

class TestWriteToolsHttpBranches:
    """tools_workspace.py 三写工具薄壳路由单测（陈旧期望已重写）。

    stale 依据：T03 收敛后三写工具已改为一行式
    `_route(<rpc method>, {...}, 'PROTECTED_MUTATION')`
    （server/tools/tools_workspace.py:107 register_workspace、:121
    set_active_workspace、:138 delete_workspace）。HTTP/local 分流、SQLite 真相源
    先行写入、daemon 便捷方法同步（client.workspace_register / workspace_activate
    / workspace_remove）、fail-closed 传播全部下沉到
    server/daemon_client.py::route_rpc（L3880-3994）。因此旧断言 mock 的
    tools_workspace.get_db / HttpDaemonRpcClient.get_instance /
    is_http_transport_enabled 已无调用点（恒为 Called 0 times，或未 patch 时直连
    真实 daemon 抛 path_not_found / invalid_params）；现改为断言薄壳透传的路由契约。
    """

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

    # (tool_name, 调用 kwargs, rpc_method, 期望 params)
    WRITE_ROUTE_CASES = [
        ("register_workspace",
         {"name": "ws1", "root_path": r"C:\ws1", "description": "desc"},
         "workspace.register",
         {"name": "ws1", "client_view_root": r"C:\ws1", "description": "desc"}),
        ("register_workspace",
         {"name": "ws1", "root_path": r"C:\ws1"},
         "workspace.register",
         {"name": "ws1", "client_view_root": r"C:\ws1", "description": ""}),
        ("set_active_workspace",
         {"workspace_id_or_name": "3"},
         "workspace.activate",
         {"workspace_id_or_name": "3"}),
        ("set_active_workspace",
         {"workspace_id_or_name": "ws4"},
         "workspace.activate",
         {"workspace_id_or_name": "ws4"}),
        ("delete_workspace",
         {"workspace_id_or_name": "9"},
         "workspace.remove",
         {"workspace_id_or_name": "9"}),
        ("delete_workspace",
         {"workspace_id_or_name": "ws10"},
         "workspace.remove",
         {"workspace_id_or_name": "ws10"}),
    ]

    @pytest.mark.parametrize(
        "tool_name, kwargs, rpc_method, expect_params",
        WRITE_ROUTE_CASES,
        ids=[f"{c[0]}:{c[2]}" for c in WRITE_ROUTE_CASES],
    )
    def test_write_tool_routes_protected_mutation_rpc(
        self, monkeypatch, tool_name, kwargs, rpc_method, expect_params,
    ):
        """三写工具经 `_route(..., 'PROTECTED_MUTATION')` 下发，参数逐字透传、
        原样回包，且不触碰本地 db（get_db 已非工具层调用点）。"""
        from unittest.mock import patch
        from callwarden.server.tools import tools_workspace

        expected = {"ok": True, "value": 1}
        calls = _route_recorder(monkeypatch, tools_workspace, expected)
        tools = self._register_tools()

        with patch("callwarden.server.tools.tools_workspace.get_db") as mock_db:
            out = tools[tool_name](**kwargs)
            mock_db.assert_not_called()

        assert out == expected
        assert calls == [(rpc_method, expect_params, "PROTECTED_MUTATION")]

    def test_write_tool_route_error_propagates(self, monkeypatch):
        """fail-closed：route_rpc 抛 DaemonUnavailableError → 薄壳原样传播
        （不静默回退纯 SQL）。"""
        from callwarden.server.tools import tools_workspace

        def _boom(method, params, op_class):
            raise DaemonUnavailableError("daemon down")

        monkeypatch.setattr(tools_workspace, "_route", _boom)
        tools = self._register_tools()
        with pytest.raises(DaemonUnavailableError):
            tools["register_workspace"]("ws1", r"C:\ws1")


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
    经统一权威返回 status="archived"（读面权威仍在，归档态可解析）；越权参数
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
        c) remove 后 registry 权威 status=archived（归档，读面权威仍可解析）。"""
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

            # 归档态权威：workspace.status 经统一权威解析
            # （rust_ext/src/daemon/snapshot_state.rs:478-527 →
            # task_collab.rs:1756-1772 → workspace_reconciliation.rs:211+），
            # registry get_workspace_status（workspace.rs:578-595）不过滤
            # archived，故返回 status="archived" 而非抛错（旧断言
            # "读面不可见 → workspace_not_found" 陈旧）。
            status = real_daemon_client.call(
                "workspace.status", {"workspace_instance_id": instance_id}
            )
            assert status["registry_instance_id"] == instance_id
            assert status["status"] == "archived"
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
