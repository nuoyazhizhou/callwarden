"""H4B-N: Native read/query HTTP cutover 测试

验证 tools_query.py / tools_workspace.py 在 `_route` 化后只下发真实存在的
rust_native RPC，不建立指向不存在 RPC 的伪路由（fail-closed 契约：伪路由会在
HTTP 模式抛 method_not_found）。

MCP 工具层 cutover（A 桶）：`server/tools/tools_query.py:56` /
`server/tools/tools_workspace.py:29` 均为
`from ..daemon_client import route_rpc as _route`，所有工具体退化为一行式
`return _route('<rpc method>', {...}, '<OP_CLASS>')`；HTTP/local/compat 分流
整体下沉 `server/daemon_client.py::route_rpc`。因此本文件 1–4 节锁定
「`_route` 三元组（method/params/op_class）+ 回包透传 + 失败 fail-closed 不回落
本地 get_db」，旧 seam（`_get_daemon_client()`、`_call_daemon_rpc`、
`is_http_transport_enabled()`）已无调用点。

归类依据：.trae-cn/evidence/http-daemon-capability-matrix.json（237 tools 矩阵）

- tools_query.py rust_native 14 个：get_stats / search_symbols / get_symbol /
  get_symbol_location / get_file_symbols / get_callers / get_callees /
  get_topological_order / get_call_chain_down / detect_cycles /
  get_uncommented_symbols / get_module_call_stats / get_semgrep_stats /
  get_semgrep_findings
- tools_query.py 其余工具（get_issue_summary / find_issues / get_test_coverage /
  get_symbol_history / get_impact / get_top_callers 等）同样 `_route` 化，
  本地/compat 回退由 `route_rpc` 内部承担
- tools_workspace.py 全部工具 `_route` 化（list_workspaces→workspace.list、
  get_active_workspace→workspace.status、build_graph→workspace.build_graph 等）

真实进程级 RPC 名对齐门（第 5 节 TestRealDaemonRpcNameAlignment）：dispatch.rs
仅存在 query.stats / query.search，隔离 daemon 未注册 workspace、未发布
snapshot 时调用会返回业务错误（invalid_params / snapshot_not_ready），但绝不
返回 method_not_found。该节需要隔离 daemon harness（cw-daemon 二进制 + 发布
manifest），属环境/harness 依赖，本文件保持原样不动。

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


def _route_recorder(monkeypatch, module, result=None):
    """patch 模块级 `_route`，记录 (method, params, op_class) 并返回固定回包。

    stale 依据（A 桶 / MCP 工具 `_route` 化）：生产工具体已从
    `_get_daemon_client()` / `_call_daemon_rpc()` / `is_http_transport_enabled()`
    分支退化为一行式 `return _route('<rpc method>', {...}, '<OP_CLASS>')`
    （`server/tools/tools_query.py:56`、`server/tools/tools_workspace.py:29`），
    HTTP/local/compat 分流整体下沉 `server/daemon_client.py::route_rpc`。
    旧 seam（client 便捷方法计数、`_call_daemon_rpc` 名断言）已无调用点，
    故改为锁定 `_route` 三元组。
    """
    seen = {}

    def fake_route(method, params, op_class):
        seen["method"] = method
        seen["params"] = dict(params)
        seen["op"] = op_class
        return result if result is not None else {"ok": True}

    monkeypatch.setattr(module, "_route", fake_route)
    return seen


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
# 1. tools_query.py rust_native 路由（`_route` 化，走真实 RPC 名）
# ============================================================

# (工具名, 调用 args, 调用 kwargs, RPC method, 期望 params)
NATIVE_READ_CASES = [
    pytest.param(
        "get_stats", (), {}, "query.stats", {},
        id="get_stats",
    ),
    pytest.param(
        "search_symbols", ("fn_a",), {}, "query.search",
        {"query": "fn_a", "kind": "", "limit": 20},
        id="search_symbols",
    ),
    pytest.param(
        "get_symbol", ("test::fn_a",), {}, "query.symbol",
        {"qualified_name": "test::fn_a"},
        id="get_symbol",
    ),
    pytest.param(
        "get_symbol_location", ("fn_a",), {}, "query.symbol_location",
        {"name": "fn_a", "file_path": ""},
        id="get_symbol_location",
    ),
    pytest.param(
        "get_file_symbols", ("src/main.rs",), {}, "query.file",
        {"file_path": "src/main.rs"},
        id="get_file_symbols",
    ),
    pytest.param(
        "get_callers", ("fn_a",), {}, "query.callers",
        {"callee_name": "fn_a", "qualified_name": None},
        id="get_callers",
    ),
    pytest.param(
        "get_callees", ("fn_a",), {}, "query.callees",
        {"caller_name": "fn_a", "qualified_name": None},
        id="get_callees",
    ),
    pytest.param(
        "get_topological_order", (), {}, "query.topological_order", {"limit": 50},
        id="get_topological_order",
    ),
    pytest.param(
        "detect_cycles", (), {}, "query.detect_cycles", {"max_depth": 10},
        id="detect_cycles",
    ),
]


class TestToolsQueryNativeRead:
    """tools_query.py 中 rust_native 工具经模块级 `_route` 下发真实 RPC（READ_ONLY）。"""

    @pytest.mark.parametrize(
        "tool_name,args,kwargs,rpc_method,expect_params",
        NATIVE_READ_CASES,
    )
    def test_native_read_routes_to_route(self, monkeypatch, tool_name, args,
                                         kwargs, rpc_method, expect_params):
        """工具经 `_route` 下发 READ_ONLY RPC，params 逐字透传，回包透传，不碰本地 db。

        stale 依据：旧用例 patch `tools_query._get_daemon_client` 并断言 client
        便捷方法被调用；`server/tools/tools_query.py:56` 已改为
        `from ..daemon_client import route_rpc as _route`，各工具体（tools_query.py
        :63/:74/:87/:97/:110/:123/:136/:180/:260）均 `return _route(..., 'READ_ONLY')`，
        client 便捷方法已无调用点（故旧断言恒为 `Called 0 times`）。
        """
        seen = _route_recorder(monkeypatch, tools_query, result={"ok": True})
        tools = _register_tools(tools_query)
        with patch("callwarden.server.tools.tools_query.get_db") as mock_db:
            result = tools[tool_name](*args, **kwargs)
            mock_db.assert_not_called()
        assert result == {"ok": True}
        assert seen["method"] == rpc_method
        assert seen["op"] == "READ_ONLY"
        assert seen["params"] == expect_params

    def test_get_call_chain_down_passthrough(self, monkeypatch):
        """get_call_chain_down：`_route` 回包逐字透传（旧 list→dict 兼容转换已移除）。

        stale 依据：旧用例断言 `client.get_call_chain_down(...)` 并把 daemon 返回
        的 list 转成 `{"chain": [...], "edges": [...]}`；`tools_query.py:200`
        已退化为 `return _route('query.call_chain_down', {...}, 'READ_ONLY')`，
        不再做任何形状转换。
        """
        seen = _route_recorder(monkeypatch, tools_query, result=["a", "b"])
        tools = _register_tools(tools_query)

        result = tools["get_call_chain_down"]("test::fn_a", max_depth=3)

        assert result == ["a", "b"]
        assert seen["method"] == "query.call_chain_down"
        assert seen["op"] == "READ_ONLY"
        assert seen["params"] == {"qualified_name": "test::fn_a", "max_depth": 3}


# ============================================================
# 2. tools_query.py 其余工具：同样 `_route` 化（分流下沉 route_rpc）
# ============================================================

# (工具名, 调用 args, 调用 kwargs, RPC method, 期望 params)
COMPAT_ROUTE_CASES = [
    pytest.param(
        "get_issue_summary", (), {}, "get_issue_summary", {},
        id="get_issue_summary",
    ),
    pytest.param(
        "find_issues", ("unwrap_call",), {}, "find_issues",
        {"issue_type": "unwrap_call", "limit": 30}, id="find_issues",
    ),
    pytest.param(
        "get_test_coverage", (), {}, "get_test_coverage", {},
        id="get_test_coverage",
    ),
    pytest.param(
        "get_symbol_history", ("test::fn_a",), {}, "get_symbol_history",
        {"qualified_name": "test::fn_a"}, id="get_symbol_history",
    ),
    pytest.param(
        "get_impact", ("test::fn_a",), {"max_depth": 5}, "get_impact",
        {"qualified_name": "test::fn_a", "max_depth": 5}, id="get_impact",
    ),
    pytest.param(
        "get_top_callers", (), {}, "get_top_callers",
        {"limit": 20, "kind": "fn", "module_filter": ""}, id="get_top_callers",
    ),
]


class TestToolsQueryCompatRouted:
    """非 rust_native 工具同样退化为 `_route`（分流下沉 route_rpc）。

    stale 依据（A 桶 / MCP 工具 `_route` 化）：旧用例把这里当作「HTTP 模式经
    compat worker / legacy 回退 get_db」的边界，patch
    `daemon_client._get_rpc_client_for_route` / `get_daemon_mode` 并断言本地 db
    方法调用。`server/tools/tools_query.py:56` 全面 `_route` 化后，这些工具体
    （tools_query.py:289/:303/:434/:145/:190/:211）均 `return _route(..., 'READ_ONLY')`，
    HTTP/local/compat 分流已整体下沉 `route_rpc`，工具层不再判断模式。

    本类只锁定「下发哪个 RPC、params 逐字透传、READ_ONLY、失败 fail-closed
    且不回落本地 get_db」；真实 local 回退行为由 `route_rpc` 单测覆盖。
    """

    @pytest.mark.parametrize(
        "tool_name,args,kwargs,rpc_method,expect_params",
        COMPAT_ROUTE_CASES,
    )
    def test_compat_tools_route_read_only_rpc(self, monkeypatch, tool_name, args,
                                              kwargs, rpc_method, expect_params):
        seen = _route_recorder(monkeypatch, tools_query, result={"ok": True})
        tools = _register_tools(tools_query)
        with patch("callwarden.server.tools.tools_query.get_db") as mock_db:
            result = tools[tool_name](*args, **kwargs)
            mock_db.assert_not_called()
        assert result == {"ok": True}
        assert seen["method"] == rpc_method
        assert seen["op"] == "READ_ONLY"
        assert seen["params"] == expect_params

    @pytest.mark.parametrize(
        "tool_name,args,kwargs,rpc_method,expect_params",
        COMPAT_ROUTE_CASES,
    )
    def test_compat_tools_fail_closed_no_local_fallback(
        self, monkeypatch, tool_name, args, kwargs, rpc_method, expect_params,
    ):
        """`_route` 抛 DaemonRemoteError → 原样传播，绝不回落本地 get_db。"""
        def fake_route(method, params, op_class):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(tools_query, "_route", fake_route)
        tools = _register_tools(tools_query)
        with patch("callwarden.server.tools.tools_query.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                tools[tool_name](*args, **kwargs)
            mock_db.assert_not_called()


# ============================================================
# 3. tools_workspace.py：全部 `_route` 化
# ============================================================

# (工具名, 调用 args, 调用 kwargs, RPC method, 期望 params, op_class)
WORKSPACE_ROUTE_CASES = [
    pytest.param("list_workspaces", (), {}, "workspace.list", {}, "READ_ONLY",
                 id="list_workspaces"),
    pytest.param("get_active_workspace", (), {}, "workspace.status", {},
                 "READ_ONLY", id="get_active_workspace"),
    pytest.param("build_graph", (), {}, "workspace.build_graph", {},
                 "PROTECTED_MUTATION", id="build_graph"),
]


class TestToolsWorkspaceRouting:
    """tools_workspace.py 工具经模块级 `_route` 下发（含写面 op_class）。

    stale 依据（A 桶 / MCP 工具 `_route` 化）：旧用例 `test_h4a_routes_via_rpc`
    patch `tools_workspace._call_daemon_rpc`、`test_get_active_workspace_...`
    patch `HttpDaemonRpcClient.get_instance` 并断言 `client.workspace_status(...)`、
    `test_build_graph_stays_local_in_http_mode` 断言本地 `db.build_full_graph()`。
    `server/tools/tools_workspace.py:29` 改为
    `from ..daemon_client import route_rpc as _route`；对应工具体
    （tools_workspace.py:89 list_workspaces /:161 get_active_workspace /
    :59 build_graph）分别下发 workspace.list（READ_ONLY）/ workspace.status
    （READ_ONLY）/ workspace.build_graph（PROTECTED_MUTATION）。旧 seam 已无调用点。
    """

    @pytest.mark.parametrize(
        "tool_name,args,kwargs,rpc_method,expect_params,op_class",
        WORKSPACE_ROUTE_CASES,
    )
    def test_workspace_tools_route_via_route(self, monkeypatch, tool_name, args,
                                             kwargs, rpc_method, expect_params,
                                             op_class):
        """工具经 `_route` 下发对应 RPC 与 op_class，params 逐字透传，不碰本地 db。"""
        seen = _route_recorder(monkeypatch, tools_workspace, result=[{"name": "ws1"}])
        tools = _register_tools(tools_workspace)
        with patch("callwarden.server.tools.tools_workspace.get_db") as mock_db:
            result = tools[tool_name](*args, **kwargs)
            mock_db.assert_not_called()
        assert result == [{"name": "ws1"}]
        assert seen["method"] == rpc_method
        assert seen["op"] == op_class
        assert seen["params"] == expect_params


# ============================================================
# 4. 静态验证：全部 `_route` 化、无旧 seam、native 名对齐
# ============================================================

class TestNoPseudoRoutes:
    """fail-closed：工具体统一 `_route`，无旧 client/RPC seam，native 名对齐。"""

    QUERY_NATIVE_RPC = {
        "get_stats": "query.stats",
        "search_symbols": "query.search",
        "get_symbol": "query.symbol",
        "get_symbol_location": "query.symbol_location",
        "get_file_symbols": "query.file",
        "get_callers": "query.callers",
        "get_callees": "query.callees",
        "get_topological_order": "query.topological_order",
        "get_call_chain_down": "query.call_chain_down",
        "detect_cycles": "query.detect_cycles",
        "get_uncommented_symbols": "query.uncommented_symbols",
        "get_module_call_stats": "query.module_call_stats",
        "get_semgrep_stats": "query.semgrep_stats",
        "get_semgrep_findings": "query.semgrep_findings",
    }

    def test_query_native_tools_route_to_real_rpc(self):
        """tools_query.py 的 rust_native 工具均 `_route('<真实 RPC 名>', ...)`。

        stale 依据：旧断言 `_get_daemon_client()` in source 已过期——
        `server/tools/tools_query.py:56` 全面 `_route` 化后，工具体不再出现该 seam。
        """
        tools = _register_tools(tools_query)
        for name, rpc in self.QUERY_NATIVE_RPC.items():
            assert f"_route('{rpc}'" in inspect.getsource(tools[name]), (
                f"{name} 应经 _route 下发 {rpc}"
            )

    def test_no_legacy_daemon_seam_in_route_modules(self):
        """两模块工具体不再出现 `_get_daemon_client(` / `_call_daemon_rpc(` 旧 seam。

        stale 依据：旧用例按「native 走 _get_daemon_client()、其余走本地、
        workspace 走 _call_daemon_rpc」三分法断言；cutover 后所有工具体统一
        `return _route(...)`（tools_query.py:56 / tools_workspace.py:29），
        故改为反向断言：旧 seam 调用点必须为 0。
        """
        for module in (tools_query, tools_workspace):
            for name, fn in _register_tools(module).items():
                source = inspect.getsource(fn)
                assert "_get_daemon_client(" not in source, (
                    f"{module.__name__}.{name} 不应再有 _get_daemon_client 调用"
                )
                assert "_call_daemon_rpc(" not in source, (
                    f"{module.__name__}.{name} 不应再有 _call_daemon_rpc 调用"
                )
                assert "_route(" in source, (
                    f"{module.__name__}.{name} 应经 _route 路由"
                )

    def test_workspace_tools_route_to_real_rpc(self):
        """tools_workspace.py 关键工具 `_route` 名对齐（含写面 PROTECTED_MUTATION）。"""
        tools = _register_tools(tools_workspace)
        expects = {
            "list_workspaces": "workspace.list",
            "get_active_workspace": "workspace.status",
            "build_graph": "workspace.build_graph",
        }
        for name, rpc in expects.items():
            assert f"_route('{rpc}'" in inspect.getsource(tools[name]), (
                f"{name} 应经 _route 下发 {rpc}"
            )


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
    def real_daemon(self, w3_live):
        """迁移到 conftest 模块级 `w3_live`（内部 tests/_w3_harness.setup_w3_client：
        USERPROFILE 重定向 + workspace.register + task-DB seed + snapshot.publish），
        取代本文件内联复制的 daemon spawn/manifest 逻辑——其 `_wait_manifest` 读父进程
        `get_http_manifest_dir()`（共享 authority manifest 目录），与并行/共享 daemon
        争用 → 假『隔离 daemon 未发布 manifest』。yield 生产类 HttpDaemonRpcClient，
        测试语义零改动（get_stats/search_symbols 只校验非 method_not_found 或返回结构）。
        """
        yield w3_live["client"]

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