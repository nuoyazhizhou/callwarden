r"""B3（T-1786590722456-db00d074-sub-3）五类查询 legacy 基线核对。

核对 file / symbol / grep / issues / tests 五类查询的三条基线：

1. **统一入口**：MCP 工具（tools_query.py / tools_task.py）→ DaemonClient 方法
   → RPC 方法名 → dispatch.rs 路由臂 → snapshot_state.rs handler，五层链完整。
   任何一层缺失即视为该查询"没有统一入口"，测试失败。

2. **fresh runtime**：客户端方法通过 `_remote_query("query.<x>", ...)` 走 RPC
   （非旧 UDS 直连 SQLite 或静默空结果）；矩阵 `daemon_rpc_method=rpc_none`
   的 legacy_local 工具（file_read/file_grep/file_list/get_issue_summary 等）
   注册入口存在，声明其走 Python 直调。

3. **结构化拒绝路径**：Rust 侧错误码构造（invalid_params / workspace_not_found
   / snapshot_not_ready / out_of_bounds）+ Python client fail-closed 语义单测
   （auto/enterprise 模式 daemon 不可用抛 DaemonUnavailableError，不静默回退
   本地 SQLite；仅 local 模式允许回退/返回 None）。

本测试为静态一致性基线 + client fail-closed 单测，**不启动真实 daemon**
（进程级 round-trip 由 M2.1-M2.5 的 5 个 `test_query_*_rpc.py` 覆盖，避免
生产 daemon 占用默认 Named Pipe 导致整体 skip）。若路由链源码变更，本测试
会先于进程级测试失败，作为回归哨兵。

参考：
- B1 冻结矩阵：.trae-cn/evidence/mcp-tool-matrix-baseline.json
- M2 迁移记录：docs/design/daemon-rust-migration-ledger.md §9.3
- M2.5：T-1786584287058-7f712ff4（query.tests）
"""

import ast
import os
import re
import sys

import pytest

from callwarden.server.daemon_client import DaemonClient, DaemonUnavailableError, _NO_REMOTE

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS_DIR = os.path.join(_REPO_ROOT, "server", "tools")
_RUST_DAEMON_DIR = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon")


def _read_rel(rel_path: str) -> str:
    """读取仓库内源码文件，缺失即失败（结构破坏不应静默）。"""
    full = os.path.join(_REPO_ROOT, rel_path)
    assert os.path.exists(full), f"源码文件缺失：{rel_path}"
    with open(full, encoding="utf-8") as f:
        return f.read()


def _read_tools(mod_name: str) -> str:
    full = os.path.join(_TOOLS_DIR, mod_name)
    assert os.path.exists(full), f"工具模块缺失：{mod_name}"
    with open(full, encoding="utf-8") as f:
        return f.read()


def _class_methods(src: str, class_name: str) -> dict:
    """AST 提取指定类的 {方法名: FunctionDef 节点}。"""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {n.name: n for n in node.body if isinstance(n, ast.FunctionDef)}
    return {}


def _rpc_calls_in(method_node: ast.FunctionDef) -> set:
    """提取方法体内 `_remote_query("query.<x>", ...)` 的 RPC 方法名集合。"""
    names = set()
    for node in ast.walk(method_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "_remote_query" and node.args:
                arg0 = node.args[0]
                if isinstance(arg0, ast.Constant) and isinstance(arg0.value, str):
                    names.add(arg0.value)
    return names


def _self_calls_in(method_node: ast.FunctionDef, attr: str) -> bool:
    """方法体内是否存在 `self.<attr>(` 调用（用于委托方法核对）。"""
    for node in ast.walk(method_node):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == attr and isinstance(node.func.value, ast.Name):
                if node.func.value.id == "self":
                    return True
    return False


# ----------------------------------------------------------------------
# 五层链矩阵：MCP 工具 → DaemonClient 方法 → RPC 方法 → dispatch 臂 → handler
# ----------------------------------------------------------------------

# client 方法 → RPC 方法名（方法体内 _remote_query 第一参数应等于此值）
CLIENT_RPC = {
    "get_file_symbols": "query.file",
    "search_symbols": "query.search",
    "get_symbol": "query.symbol",
    "get_symbol_location": "query.symbol_location",
    "get_callers": "query.callers",
    "get_callees": "query.callees",
    "get_topological_order": "query.topological_order",
    "get_call_chain_down": "query.call_chain_down",
    "query_grep": "query.grep",
    "query_issues": "query.issues",
    "get_symbol_issues": "query.issues",
    "query_tests": "query.tests",
    "get_stats": "query.stats",
}

# 委托 client 方法 → 目标 self 方法（query.tests 子模式入口）
CLIENT_DELEGATE = {
    "get_test_cases": "query_tests",
    "get_tested_functions": "query_tests",
    "get_test_stability": "query_tests",
    "get_test_coverage_summary": "query_tests",
}

# MCP 工具（tools_query.py / tools_task.py）→ 其调用的 client 方法名
TOOL_CLIENT = {
    "tools_query.py": {
        "get_file_symbols": "get_file_symbols",
        "search_symbols": "search_symbols",
        "get_symbol": "get_symbol",
        "get_symbol_location": "get_symbol_location",
        "get_callers": "get_callers",
        "get_callees": "get_callees",
        "get_topological_order": "get_topological_order",
        "get_call_chain_down": "get_call_chain_down",
        "get_stats": "get_stats",
    },
    "tools_task.py": {
        "get_symbol_issues": "get_symbol_issues",
        "get_test_cases": "get_test_cases",
        "get_tested_functions": "get_tested_functions",
        "get_test_stability": "get_test_stability",
        "get_test_coverage_summary": "get_test_coverage_summary",
    },
}

# 矩阵 daemon_rpc_method=rpc_none 的 legacy_local 工具 → 注册文件（Python 直调入口）
LEGACY_LOCAL_TOOLS = {
    "tools_workspace.py": ["file_read", "file_grep", "file_list", "file_symbol_content"],
    "tools_query.py": [
        "get_symbol_history", "get_file_history", "get_issue_summary", "find_issues",
        "get_semgrep_stats", "get_semgrep_findings", "run_semgrep_scan",
        "scan_semgrep_incremental", "get_test_coverage",
    ],
    "tools_summary.py": ["test_impact_selection"],
    "tools_task.py": ["get_defect_correlation"],
}

# Rust 侧结构化错误码构造位置（文件, 特征字符串）
ERROR_CODE_SITES = {
    "invalid_params": ("rust_ext/src/daemon/dispatch.rs", "fn invalid_params"),
    "workspace_not_found": ("rust_ext/src/daemon/dispatch.rs", "fn workspace_not_found"),
    "out_of_bounds": ("rust_ext/src/daemon/query_handlers.rs", "out_of_bounds"),
    "snapshot_not_ready": ("rust_ext/src/daemon/snapshot_state.rs", "snapshot_not_ready"),
}

# fail-closed client 方法（auto/enterprise 模式 daemon 不可用必须抛错，不得静默回退）
FAIL_CLOSED_METHODS = ["get_symbol", "get_file_symbols",
                       "query_grep", "query_issues", "query_tests"]
# local 模式语义（SRV-006 后）：get_symbol/get_file_symbols 本地 SQL 已退役，
# daemon 不可用时 fail-closed；grep/issues/tests 仍返回 None
LOCAL_MODE_SQL_FALLBACK = {"get_symbol", "get_file_symbols"}
LOCAL_MODE_NONE = {"query_grep", "query_issues", "query_tests"}


class TestQueryDispatchRoutes:
    """dispatch.rs 必须存在 `"query.<x>" => ...` 路由臂。"""

    @pytest.fixture(scope="class")
    def dispatch_src(self):
        return _read_rel("rust_ext/src/daemon/dispatch.rs")

    @pytest.mark.parametrize("rpc_method", sorted(set(CLIENT_RPC.values())))
    def test_dispatch_has_route(self, dispatch_src, rpc_method):
        pattern = r'"%s"\s*=>' % re.escape(rpc_method)
        assert re.search(pattern, dispatch_src), (
            f"dispatch.rs 缺少 {rpc_method} 路由臂"
        )

    def test_dispatch_routes_to_handler(self, dispatch_src):
        """每个 query.* 路由臂必须引用对应的 handle_query_<x>，防止路由悬空。"""
        for rpc_method in set(CLIENT_RPC.values()):
            handler = "handle_query_" + rpc_method.split(".")[1]
            assert re.search(r"\b%s\s*\(" % re.escape(handler), dispatch_src), (
                f"dispatch.rs {rpc_method} 路由未引用 {handler}"
            )


class TestQueryHandlersImplemented:
    """snapshot_state.rs 必须实现 dispatch 引用的全部 handle_query_* handler。"""

    @pytest.fixture(scope="class")
    def handler_names(self):
        src = _read_rel("rust_ext/src/daemon/snapshot_state.rs")
        raw = re.findall(r"fn handle_query_([a-z_0-9]+)\s*\(", src)
        names = {"handle_query_" + n for n in raw}
        assert names, "snapshot_state.rs 未找到任何 handle_query_* 方法"
        return names

    @pytest.mark.parametrize("rpc_method", sorted(set(CLIENT_RPC.values())))
    def test_handler_exists(self, handler_names, rpc_method):
        handler = "handle_query_" + rpc_method.split(".")[1]
        assert handler in handler_names, (
            f"snapshot_state.rs 缺少 {handler}（{rpc_method} 路由悬空）"
        )


class TestClientRpcRouting:
    """daemon_client.py 的客户端方法必须路由到 RPC 方法名（fresh runtime，非直连 SQL）。"""

    @pytest.fixture(scope="class")
    def client_methods(self):
        src = _read_rel("server/daemon_client.py")
        methods = _class_methods(src, "DaemonClient")
        assert methods, "daemon_client.py 未解析到 DaemonClient 类"
        return methods

    @pytest.mark.parametrize("client_method", sorted(CLIENT_RPC))
    def test_method_exists_and_routes_to_rpc(self, client_methods, client_method):
        expected_rpc = CLIENT_RPC[client_method]
        node = client_methods.get(client_method)
        assert node is not None, f"DaemonClient 缺少方法 {client_method}"
        rpc_calls = _rpc_calls_in(node)
        assert expected_rpc in rpc_calls, (
            f"{client_method} 未调用 _remote_query({expected_rpc!r})，实际调用 {sorted(rpc_calls)}"
        )

    @pytest.mark.parametrize("client_method", sorted(CLIENT_DELEGATE))
    def test_delegate_methods_route_to_query_tests(self, client_methods, client_method):
        node = client_methods.get(client_method)
        assert node is not None, f"DaemonClient 缺少方法 {client_method}"
        assert _self_calls_in(node, CLIENT_DELEGATE[client_method]), (
            f"{client_method} 未委托 self.{CLIENT_DELEGATE[client_method]}()"
        )

    def test_legacy_direct_db_methods_remain_python(self):
        """legacy_local 工具的 db 直调方法（get_symbol_history）保持 Python 入口，
        不得混入 daemon RPC（矩阵 rpc_none 声明）。get_file_history 已 W4-1
        （T-1786886251769-22b94ee8-sub-1）迁移 rust_native（HTTP 分支新增
        _get_daemon_client），不再属于本清单。"""
        src = _read_rel("server/tools/tools_query.py")
        for tool in ("get_symbol_history",):
            m = re.search(r"\n\s+def\s+%s\b" % re.escape(tool), src)
            assert m, f"tools_query.py 缺少 {tool}"
            # 工具实现应通过 get_db()（Python 直调），不引用 _get_daemon_client
            block = src[m.end():]
            next_def = re.search(r"\n    def ", block)
            body = block[:next_def.start()] if next_def else block
            assert "get_db()" in body, f"{tool} 未使用 Python get_db() 入口"
            assert "_get_daemon_client" not in body, f"{tool} 不应路由 daemon RPC"


class TestMcpToolsEntry:
    """MCP 工具注册存在且调用对应 client 方法（统一入口第一层）。"""

    @pytest.mark.parametrize("mod_name", sorted(TOOL_CLIENT))
    def test_tools_call_client(self, mod_name):
        src = _read_tools(mod_name)
        for tool, client_method in TOOL_CLIENT[mod_name].items():
            m = re.search(r"\n\s+def\s+%s\b" % re.escape(tool), src)
            assert m, f"{mod_name} 缺少 MCP 工具 {tool}"
            block = src[m.end():]
            next_def = re.search(r"\n    def ", block)
            body = block[:next_def.start()] if next_def else block
            assert re.search(r"client\.%s\s*\(" % re.escape(client_method), body), (
                f"{mod_name}.{tool} 未调用 client.{client_method}()"
            )


class TestLegacyLocalToolsEntry:
    """矩阵 rpc=none（legacy_local）工具必须保留 Python 直调注册入口。"""

    @pytest.mark.parametrize("mod_name", sorted(LEGACY_LOCAL_TOOLS))
    def test_legacy_local_tools_registered(self, mod_name):
        src = _read_tools(mod_name)
        for tool in LEGACY_LOCAL_TOOLS[mod_name]:
            assert re.search(r"\n\s+def\s+%s\b" % re.escape(tool), src), (
                f"{mod_name} 缺少 legacy_local 工具 {tool}"
            )


class TestStructuredRejectionCodes:
    """Rust 侧结构化错误码构造存在，拒绝路径不依赖自然语言文本。"""

    @pytest.mark.parametrize("code", sorted(ERROR_CODE_SITES))
    def test_error_code_site_present(self, code):
        rel_path, marker = ERROR_CODE_SITES[code]
        src = _read_rel(rel_path)
        assert marker in src, f"{rel_path} 缺少 {code} 错误码特征（{marker}）"


class TestClientFailClosed:
    """五类查询 client 方法 fail-closed 语义（auto/enterprise 不可回退，local 才允许）。"""

    def _make_client(self, mode, remote_result=_NO_REMOTE):
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        client._remote_query = lambda method, params, db_path=None: remote_result
        client._get_db = lambda: None  # 阻止误入真实 DB 路径
        from callwarden.server import daemon_client as dc_module
        dc_module.get_daemon_mode = lambda: mode
        return client

    @pytest.mark.parametrize("method", FAIL_CLOSED_METHODS)
    @pytest.mark.parametrize("mode", ["auto", "enterprise"])
    def test_fail_closed_when_daemon_down(self, method, mode):
        """daemon 不可用时必须抛 DaemonUnavailableError，且不得计入 SQL fallback。"""
        client = self._make_client(mode)
        with pytest.raises(DaemonUnavailableError):
            getattr(client, method)("crate::foo")
        assert client._sql_fallbacks == 0, f"{method} fail-closed 不得计数 SQL fallback"

    @pytest.mark.parametrize("method", sorted(LOCAL_MODE_SQL_FALLBACK))
    def test_local_mode_sql_fallback(self, method):
        """SRV-006：local 模式本地 SQL 回退已退役——get_symbol 的回退入口
        `_sql_fallback_get_symbol` 已薄客户端化为 daemon RPC
        （mcp.daemon_client.sql_fallback_get_symbol），daemon 不可用时抛
        DaemonUnavailableError；get_file_symbols 的 `_get_db()` 直连路径已移除，
        直接 fail-closed。local 模式不再允许本地业务 SQL（check_items：
        no SQLite / no get_db / no business fallback）。
        """
        client = self._make_client("local")

        def _unavailable(qn):
            raise DaemonUnavailableError("daemon 不可用（SRV-006：local SQL 已退役）")

        client._sql_fallback_get_symbol = _unavailable
        with pytest.raises(DaemonUnavailableError):
            if method == "get_symbol":
                client.get_symbol("crate::foo")
            else:  # get_file_symbols：本地 SQL 路径已退役，直接 fail-closed
                client.get_file_symbols("a.py")

    @pytest.mark.parametrize("method", sorted(LOCAL_MODE_NONE))
    def test_local_mode_returns_none(self, method):
        """local 模式：grep/issues/tests 无本地 SQL 回退，返回 None 由本地组件处理。"""
        client = self._make_client("local")
        result = getattr(client, method)("crate::foo")
        assert result is None, f"{method} local 模式应返回 None"
        assert client._sql_fallbacks == 0

    @pytest.mark.parametrize("method", ["get_callers", "get_callees", "search_symbols"])
    def test_sql_fallback_methods_still_route_rpc_first(self, method):
        """非 fail-closed 查询：remote 命中时直接返回 daemon 结果（不走 SQL）。"""
        client = self._make_client("auto", remote_result={"__mode__": "rpc"})
        result = getattr(client, method)("crate::foo")
        assert result == {"__mode__": "rpc"}
        assert client._sql_fallbacks == 0
