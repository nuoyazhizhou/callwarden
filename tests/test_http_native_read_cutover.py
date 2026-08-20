"""H4B-N: Native read/query HTTP cutover 测试

验证 tools_query.py / tools_workspace.py 在 HTTP daemon 模式下只路由真实存在的
rust_native RPC，不建立指向不存在 RPC 的伪路由（fail-closed 契约：伪路由会在
HTTP 模式抛 method_not_found）。

归类依据：.trae-cn/evidence/http-daemon-capability-matrix.json（237 tools 矩阵）

- tools_query.py rust_native（H4A 已建路由，走 _get_daemon_client()）10 个：
  get_stats / search_symbols / get_symbol / get_symbol_location / get_file_symbols /
  get_callers / get_callees / get_topological_order / get_call_chain_down / detect_cycles
  W2-1（T-1786840097330-dec66710）：+ get_uncommented_symbols / get_module_call_stats /
  get_semgrep_stats（HTTP 分支直连 HttpDaemonRpcClient 便捷方法），共 13 个
  W3-3（T-1786861820151-deb64c48）：+ get_semgrep_findings，共 14 个
- tools_query.py 本地 SQL（矩阵 daemon_rpc_method=none / 语义不映射）3 个：
  get_issue_summary / find_issues / get_test_coverage（M2.4/M2.5 说明）
- tools_query.py python_compat（归 H4B-compat-read，待 compat_route 扩展）15 个：
  其余工具保持本地 get_db() 执行
- tools_workspace.py rust_native（H4A 已建路由）2 个：
  list_workspaces（workspace.list）/ get_active_workspace
  （W1-1，T-1786808777378-bbcbf059：经 HttpDaemonRpcClient.workspace_status
  便捷方法 → workspace.status，替代修复前缺 workspace_instance_id 注入的
  workspace.activate {} 直接调用——Rust handle_workspace_activate/status
  强制 require workspace_instance_id，旧调用返回 invalid_params）
- tools_workspace.py python_compat / legacy_local 25 个：保持本地执行

H4B-N 复审整改（BLOCKED → 已修复）：
- HttpDaemonRpcClient.get_stats / search_symbols / get_active_workspace /
  set_active_workspace 的 RPC 名已对齐 dispatch.rs（query.stats / query.search /
  workspace.activate），见本文件「真实进程级 RPC 名对齐门」。
- tools_workspace.py get_active_workspace 工具的 _call_daemon_rpc 名已同步对齐。

超范围遗留（需后续任务处理，本任务不触碰）：
- http_server.rs::build_capability_registry 宣告的 "workspace.active" / "stats"
  与 dispatch.rs 真名（workspace.activate / query.stats）不一致，属 H1 产物，
  不在 H4B-N 白名单；已记录待后续维护任务处理。
- HttpDaemonRpcClient 便捷方法不注入 workspace_instance_id（legacy
  _remote_query 会注入）。隔离/无 snapshot 环境调用 query.stats / query.search
  返回 invalid_params / snapshot_not_ready 属业务错误，与 RPC 名无关。
"""

import inspect
import json
import os
import subprocess
import time
from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_query, tools_workspace
from callwarden.config import (  # noqa: E402
    get_http_manifest_dir,
    get_http_manifest_path,
)
from callwarden.server.daemon_autostart import _pid_alive  # noqa: E402


# ============================================================
# 辅助夹具
# ============================================================

DB_PATH = "/tmp/h4b_test.db"


@pytest.fixture
def mock_http_mode(monkeypatch):
    """启用 HTTP 模式（is_http_transport_enabled 返回 True）。"""
    monkeypatch.setattr(
        "callwarden.server.daemon_client.is_http_transport_enabled",
        lambda: True,
    )


@pytest.fixture
def mock_daemon_client(monkeypatch):
    """patch tools_query._get_daemon_client / _get_db_path_for_daemon，返回 mock client。

    W2-1（T-1786840097330-dec66710）：同时固定 tools_query.is_http_transport_enabled
    =True——三工具 HTTP 分支依赖该判断，避免 CI 环境设置 CW_DAEMON_TRANSPORT
    导致分支偏移（tools_query 经 `from callwarden.config import is_http_transport_enabled`
    绑定引用，patch daemon_client 模块不影响 tools_query）。
    """
    client = MagicMock()
    monkeypatch.setattr(
        "callwarden.server.tools.tools_query._get_daemon_client",
        lambda: client,
    )
    monkeypatch.setattr(
        "callwarden.server.tools.tools_query._get_db_path_for_daemon",
        lambda: DB_PATH,
    )
    monkeypatch.setattr(
        "callwarden.server.tools.tools_query.is_http_transport_enabled",
        lambda: True,
    )
    return client


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


# ============================================================
# 1. tools_query.py rust_native 路由（H4A 已建，走 _get_daemon_client）
# ============================================================

# (工具名, 调用 args, 调用 kwargs, client 方法, client args, client kwargs, 返回值)
NATIVE_READ_CASES = [
    pytest.param(
        "get_stats", (), {},
        "get_stats", (), {"db_path": DB_PATH},
        {"files": 10, "functions": 20},
        id="get_stats",
    ),
    pytest.param(
        "search_symbols", ("fn_a",), {},
        "search_symbols", ("fn_a",), {"kind": None, "limit": 20, "db_path": DB_PATH},
        [{"qualified_name": "test::fn_a"}],
        id="search_symbols",
    ),
    pytest.param(
        "get_symbol", ("test::fn_a",), {},
        "get_symbol", ("test::fn_a",), {"db_path": DB_PATH},
        {"qualified_name": "test::fn_a"},
        id="get_symbol",
    ),
    pytest.param(
        "get_symbol_location", ("fn_a",), {},
        "get_symbol_location", ("fn_a",), {"file_path": "", "db_path": DB_PATH},
        {"file": "a.rs", "line": 1},
        id="get_symbol_location",
    ),
    pytest.param(
        "get_file_symbols", ("src/main.rs",), {},
        "get_file_symbols", ("src/main.rs",), {"db_path": DB_PATH},
        [{"name": "main"}],
        id="get_file_symbols",
    ),
    pytest.param(
        "get_callers", ("fn_a",), {},
        "get_callers", ("fn_a", None), {"db_path": DB_PATH},
        [{"caller": "fn_b"}],
        id="get_callers",
    ),
    pytest.param(
        "get_callees", ("fn_a",), {},
        "get_callees", ("fn_a", None), {"db_path": DB_PATH},
        [{"callee": "fn_c"}],
        id="get_callees",
    ),
    pytest.param(
        "get_topological_order", (), {},
        "get_topological_order", (), {"limit": 50, "db_path": DB_PATH},
        ["test::fn_a"],
        id="get_topological_order",
    ),
    pytest.param(
        "detect_cycles", (), {},
        "detect_cycles", (), {"max_depth": 10, "db_path": DB_PATH},
        [["a", "b"]],
        id="detect_cycles",
    ),
]


class TestToolsQueryNativeRead:
    """tools_query.py 中 10 个 rust_native 工具在 daemon 模式下走 client 路由。"""

    @pytest.mark.parametrize(
        "tool_name,args,kwargs,client_method,expect_args,expect_kwargs,expected",
        NATIVE_READ_CASES,
    )
    def test_native_read_routes_to_client(self, mock_daemon_client, tool_name, args,
                                          kwargs, client_method, expect_args,
                                          expect_kwargs, expected):
        tools = _register_tools(tools_query)
        mock_method = getattr(mock_daemon_client, client_method)
        mock_method.return_value = expected

        result = tools[tool_name](*args, **kwargs)

        mock_method.assert_called_once_with(*expect_args, **expect_kwargs)
        assert result == expected

    def test_get_call_chain_down_converts_list_to_dict(self, mock_daemon_client):
        """get_call_chain_down：daemon 返回 list，MCP 接口期望 dict（兼容旧格式）。"""
        tools = _register_tools(tools_query)
        mock_daemon_client.get_call_chain_down.return_value = ["a", "b"]

        result = tools["get_call_chain_down"]("test::fn_a", max_depth=3)

        mock_daemon_client.get_call_chain_down.assert_called_once_with(
            "test::fn_a", max_depth=3, db_path=DB_PATH
        )
        assert result == {"chain": ["a", "b"], "edges": ["a", "b"]}


# ============================================================
# 2. tools_query.py 非 rust_native 工具：HTTP 模式下保持本地执行
# ============================================================

LOCAL_KEEP_CASES = [
    # (工具名, 调用 args, 调用 kwargs, db 方法, db args, db kwargs, 返回值)
    pytest.param(
        "get_issue_summary", (), {}, "get_issue_summary", (), {},
        {"total_issues": 10}, id="local-sql-get_issue_summary",
    ),
    pytest.param(
        "find_issues", ("unwrap_call",), {}, "get_function_issues", (),
        {"issue_filter": "unwrap_call", "limit": 30},
        [{"issue_type": "unwrap_call"}], id="local-sql-find_issues",
    ),
    pytest.param(
        "get_test_coverage", (), {}, "get_test_coverage", (), {},
        {"total_tests": 5}, id="local-sql-get_test_coverage",
    ),
    pytest.param(
        "get_symbol_history", ("test::fn_a",), {}, "get_symbol_history",
        ("test::fn_a",), {}, [{"version": 1}], id="python_compat-get_symbol_history",
    ),
    pytest.param(
        "get_impact", ("test::fn_a",), {"max_depth": 5}, "get_call_chain_up",
        ("test::fn_a",), {"max_depth": 5}, {"chain": []}, id="python_compat-get_impact",
    ),
    pytest.param(
        "get_top_callers", (), {}, "get_top_callers", (),
        {"limit": 20, "kind": "fn", "module_filter": ""},
        [{"name": "fn_a"}], id="python_compat-get_top_callers",
    ),
]


class TestToolsQueryStaysLocal:
    """非 rust_native 工具的 HTTP/legacy 路由边界（W2-1 同步修正断言）。

    H4C-2 之后这些工具经 route_worker_call：HTTP 模式 fail-closed（经 compat
    worker RPC 执行，client 失败即上抛，不回落本地 SQLite）；仅 legacy local
    模式（is_http_transport_enabled()=False + mode=local）才回退 get_db()
    本地执行。原断言「HTTP 模式下调用 get_db」与 fail-closed 契约相悖，且
    依赖本机是否运行 daemon（非确定性），W2-1（T-1786840097330-dec66710）
    一并修正。
    """

    @pytest.mark.parametrize(
        "tool_name,args,kwargs,db_method,db_args,db_kwargs,expected",
        LOCAL_KEEP_CASES,
    )
    def test_http_mode_routes_worker_no_db(self, mock_http_mode, tool_name, args,
                                           kwargs, db_method, db_args, db_kwargs,
                                           expected):
        """HTTP 模式：经 compat worker RPC 执行（result 透传），不调用 get_db。"""
        client = MagicMock()
        client.call.return_value = expected
        with patch(
            "callwarden.server.daemon_client._get_rpc_client_for_route",
            return_value=client,
        ), patch("callwarden.server.tools.tools_query.get_db") as mock_get_db:
            tools = _register_tools(tools_query)

            result = tools[tool_name](*args, **kwargs)

            mock_get_db.assert_not_called()
            assert result == expected

    @pytest.mark.parametrize(
        "tool_name,args,kwargs,db_method,db_args,db_kwargs,expected",
        LOCAL_KEEP_CASES,
    )
    def test_legacy_local_mode_falls_back_to_db(self, monkeypatch, tool_name, args,
                                                kwargs, db_method, db_args, db_kwargs,
                                                expected):
        """legacy local 模式（HTTP 关闭 + mode=local）→ 回退 get_db() 本地执行。"""
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode",
            lambda: "local",
        )
        with patch("callwarden.server.tools.tools_query.get_db") as mock_get_db:
            mock_db = MagicMock()
            getattr(mock_db, db_method).return_value = expected
            mock_get_db.return_value = mock_db
            tools = _register_tools(tools_query)

            result = tools[tool_name](*args, **kwargs)

            getattr(mock_db, db_method).assert_called_once_with(*db_args, **db_kwargs)
            assert result == expected


# ============================================================
# 3. tools_workspace.py：2 个 H4A 路由 + 其余本地保持
# ============================================================

class TestToolsWorkspaceRouting:
    """tools_workspace.py 的 HTTP 路由（仅 H4A 的 2 个）与本地保持。"""

    @pytest.mark.parametrize(
        "tool_name,rpc_method,expected",
        [
            pytest.param("list_workspaces", "workspace.list", [{"name": "ws1"}],
                         id="list_workspaces-workspace.list"),
        ],
    )
    def test_h4a_routes_via_rpc(self, mock_http_mode, tool_name, rpc_method, expected):
        with patch("callwarden.server.tools.tools_workspace._call_daemon_rpc") as mock_rpc:
            mock_rpc.return_value = expected
            tools = _register_tools(tools_workspace)

            result = tools[tool_name]()

            mock_rpc.assert_called_once_with(rpc_method, {})
            assert result == expected

    def test_get_active_workspace_via_workspace_status_convenience(self, mock_http_mode):
        """W1-1：get_active_workspace HTTP 分支经 workspace_status 便捷方法注入
        workspace_instance_id（修复前 workspace.activate {} 缺注入 → invalid_params）。"""
        client = MagicMock()
        client.workspace_status.return_value = {
            "workspace_id": 1,
            "workspace_instance_id": "inst-1",
            "client_view_root": r"C:\ws1",
            "host_real_root": r"C:\ws1",
            "status": "active",
        }
        with patch(
            "callwarden.server.daemon_client.HttpDaemonRpcClient.get_instance",
            return_value=client,
        ), patch(
            "callwarden.server._mcp_common._get_db_path_for_daemon",
            return_value=DB_PATH,
        ):
            tools = _register_tools(tools_workspace)
            result = tools["get_active_workspace"]()

        client.workspace_status.assert_called_once_with(db_path=DB_PATH)
        # 兼容映射：client_view_root→root_path、name=basename 兜底
        assert result["root_path"] == r"C:\ws1"
        assert result["name"] == "ws1"
        assert result["workspace_instance_id"] == "inst-1"

    def test_build_graph_stays_local_in_http_mode(self, mock_http_mode):
        """build_graph（python_compat）在 HTTP 模式仍走本地 db，不调用 RPC。"""
        with patch("callwarden.server.tools.tools_workspace.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            tools = _register_tools(tools_workspace)

            result = tools["build_graph"]()

            assert result is True
            mock_db.build_full_graph.assert_called_once_with()


# ============================================================
# 4. fail-closed 静态验证：无伪路由
# ============================================================

class TestNoPseudoRoutes:
    """fail-closed：不得存在指向不存在 RPC 的伪路由。"""

    QUERY_NATIVE = {
        "get_stats", "search_symbols", "get_symbol", "get_symbol_location",
        "get_file_symbols", "get_callers", "get_callees", "get_topological_order",
        "get_call_chain_down", "detect_cycles",
        # W2-1（T-1786840097330-dec66710）：三工具迁移 rust_native
        "get_uncommented_symbols", "get_module_call_stats", "get_semgrep_stats",
        # W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 迁移 rust_native
        "get_semgrep_findings",
    }

    def test_query_native_tools_use_daemon_client(self):
        """tools_query.py 的 14 个 rust_native 工具均走 _get_daemon_client()。"""
        tools = _register_tools(tools_query)
        for name, fn in tools.items():
            if name in self.QUERY_NATIVE:
                assert "_get_daemon_client()" in inspect.getsource(fn), (
                    f"{name} 应走 _get_daemon_client()"
                )

    def test_query_local_tools_have_no_daemon_route(self):
        """tools_query.py 其余 19 个工具不得含 client 或 RPC 伪路由。"""
        tools = _register_tools(tools_query)
        for name, fn in tools.items():
            if name in self.QUERY_NATIVE:
                continue
            source = inspect.getsource(fn)
            assert "_get_daemon_client" not in source, f"{name} 不应有 client 路由"
            assert "_call_daemon_rpc" not in source, f"{name} 不应有 daemon RPC 伪路由"

    def test_workspace_only_h4a_tools_have_rpc(self):
        """tools_workspace.py 仅 2 个 H4A 工具含 daemon 路由：
        list_workspaces 走 _call_daemon_rpc；get_active_workspace 经
        workspace_status 便捷方法（W1-1）。其余 25 个保持本地。"""
        tools = _register_tools(tools_workspace)
        for name, fn in tools.items():
            source = inspect.getsource(fn)
            if name == "list_workspaces":
                assert "_call_daemon_rpc" in source, "list_workspaces 应走 workspace.list"
            elif name == "get_active_workspace":
                assert "workspace_status" in source and "HttpDaemonRpcClient" in source, (
                    "get_active_workspace 应经 workspace_status 便捷方法"
                )
            else:
                assert "_call_daemon_rpc" not in source, f"{name} 不应有 daemon RPC 伪路由"
                assert "workspace_status" not in source, f"{name} 不应有便捷方法路由"


# ============================================================
# 5. 真实进程级 RPC 名对齐门（H4B-N 复审 BLOCKED 整改）
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


class TestRealDaemonRpcNameAlignment:
    """真实进程级 RPC 名对齐门（H4B-N 复审 BLOCKED 整改产物）。

    dispatch.rs 仅存在 query.stats / query.search / workspace.activate。
    隔离 daemon 未注册 workspace、未发布 snapshot，调用 query.stats /
    query.search 会返回业务错误（invalid_params 缺 workspace_instance_id /
    snapshot_not_ready），但**绝不返回 method_not_found** —— method_not_found
    只在 RPC 名不存在时出现。本类补上 H2I 真实进程 gate 未覆盖的
    get_stats / search_symbols 两个 cutover 方法，验证 RPC 名已对齐 dispatch.rs。
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

    def test_get_stats_rpc_name_aligned(self, real_daemon):
        """get_stats 走 query.stats：不得 method_not_found。"""
        try:
            result = real_daemon.get_stats()
        except DaemonRemoteError as exc:
            assert exc.code != "method_not_found", (
                f"get_stats 使用坏 RPC 名（应为 query.stats）: {exc}"
            )
        else:
            assert isinstance(result, (dict, list)) or result is None

    def test_search_symbols_rpc_name_aligned(self, real_daemon):
        """search_symbols 走 query.search：不得 method_not_found。"""
        try:
            result = real_daemon.search_symbols("fn_a", limit=5)
        except DaemonRemoteError as exc:
            assert exc.code != "method_not_found", (
                f"search_symbols 使用坏 RPC 名（应为 query.search）: {exc}"
            )
        else:
            assert isinstance(result, list) or result is None