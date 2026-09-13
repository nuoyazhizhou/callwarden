"""H4B-I: index-write/job HTTP cutover 测试

验证 tools_security.py / tools_rules.py（45 个 python_compat 工具）在 daemon
authority 化后统一经 `_route` 薄壳路由到 daemon，不建立伪路由、不直连本地 SQLite
（无 SQLite fallback），无 daemon 时 fail-closed。

stale 修复（daemon authority 化）：旧的 `_http_unsupported()` 结构化 unsupported
范式已退役，工具体改为单行 `return _route('<rpc method>', {...}, '<OP_CLASS>')`
（权威来源：server/tools/tools_security.py:43
`from ..daemon_client import route_rpc as _route`；tools_rules.py:39 同款）。
且 `_get_daemon_client` 现仅存在于两模块的 import 行，无任何工具体调用
（tools_security.py:27、tools_rules.py:36）——旧断言「HTTP 模式返回
E_HTTP_COMPAT_UNSUPPORTED」「legacy 模式调 get_db / _get_daemon_client」均已陈旧。
本文件按 `_route` 薄壳范式重写（模板①：patch 模块内 `_route`，断言
(method, params, op_class) 三元组 + 回包透传 + fail-closed）。

真实进程门（TestRealDaemonIndexJobRpcAlignment）迁移至 conftest `w3_live` 隔离
daemon（模板④：tests/conftest.py:8 `w3_live`，内部
tests/_w3_harness.py::setup_w3_client 模式 A USERPROFILE 重定向）：
- 正向：已注册 Rust-native 方法 query.stats_top_files 在生产 HttpDaemonRpcClient
  调用下不返回 method_not_found；
- 负向：伪路由候选名（rules.list_toolchains / security.diff_callees）在真实 daemon
  上必返回 method_not_found —— 实证 fail-closed 契约。
"""

import inspect
from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import tools_rules, tools_security


# ============================================================
# 辅助夹具
# ============================================================

COMPAT_MODULES = [tools_security, tools_rules]

# 工具体 `_route` 的合法 op_class（权威来源：server/daemon_client.py route_rpc）。
VALID_OP_CLASSES = ("READ_ONLY", "PROTECTED_MUTATION", "GOVERNANCE_WRITE")


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
# 1. HTTP 模式经 `_route` 薄壳路由（45 个 python_compat 工具全量）
# ============================================================

class TestToolsRouteViaThinShell:
    """所有 python_compat 工具经 `_route` 薄壳路由到 daemon，不直连本地 SQLite。

    stale 修复：旧断言 `result["error"] == "E_HTTP_COMPAT_UNSUPPORTED"` +
    `backend == "python_compat"` + `tool == name` 属已退役的 `_http_unsupported()`
    范式；现工具体为单行 `return _route('<method>', {...}, '<OP_CLASS>')`
    （权威来源：server/tools/tools_security.py:43、tools_rules.py:39）。
    """

    @pytest.mark.parametrize("module", COMPAT_MODULES, ids=lambda m: m.__name__.split(".")[-1])
    def test_all_tools_route_to_daemon(self, module, monkeypatch):
        tools = _register_tools(module)
        assert len(tools) > 0
        calls = []

        def fake_route(method, params, op_class):
            calls.append((method, params, op_class))
            return {"routed": method}

        # patch 模块内 `_route`（工具体唯一调用点）
        monkeypatch.setattr(module, "_route", fake_route)
        with patch(f"{module.__name__}.get_db") as mock_get_db:
            for name, fn in tools.items():
                args, kwargs = _make_call_args(fn)
                # 回包透传：工具体原样返回 `_route` 结果
                assert fn(*args, **kwargs) == {"routed": calls[-1][0]}, name
            # 无 SQLite fallback 证明：路由范式下 get_db 从未被调用
            mock_get_db.assert_not_called()
        # 每个工具恰好一次 `_route`，三元组形状合法
        assert len(calls) == len(tools)
        for method, params, op_class in calls:
            assert isinstance(method, str) and method, "RPC 方法名必须非空"
            assert isinstance(params, dict), "params 必须是 dict"
            assert op_class in VALID_OP_CLASSES, f"非法 op_class: {op_class}"


# ============================================================
# 2. fail-closed 静态验证：无伪路由
# ============================================================

class TestNoPseudoRoutes:
    """fail-closed：两模块不得存在指向不存在 RPC 的伪路由。"""

    def test_no_daemon_rpc_pseudo_route(self):
        """任何工具源码不得含 _call_daemon_rpc / _get_daemon_client 伪路由。

        stale 修复：旧断言允许 `_get_daemon_client` 出现在 `_http_unsupported`
        短路之后（legacy 分支）；daemon authority 化后两模块工具体全部为
        `return _route(...)`，`_get_daemon_client` 仅存在于 import 行
        （权威来源：tools_security.py:27、tools_rules.py:36），工具体内不得出现。
        """
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

    def test_every_tool_uses_route_thin_shell(self):
        """每个工具体均经 `_route(...)` 薄壳路由（唯一调用点）。

        stale 修复：旧断言 `_http_unsupported("{name}")` 针对已退役范式；现权威
        范式为 `_route('<method>', {...}, '<OP_CLASS>')`（tools_security.py:62 起）。
        """
        for module in COMPAT_MODULES:
            tools = _register_tools(module)
            for name, fn in tools.items():
                source = inspect.getsource(fn)
                assert "_route(" in source, (
                    f"{name} 应经 _route 薄壳路由到 daemon"
                )
                assert "_http_unsupported(" not in source, (
                    f"{name} 不应残留旧 _http_unsupported 范式"
                )

    def test_no_sqlite_fallback_in_module(self):
        """模块级不得直接构造 CodeGraphDB（无 SQLite fallback）。"""
        for module in COMPAT_MODULES:
            source = inspect.getsource(module)
            # stale 修复：旧断言 `"_http_unsupported" in source` 属退役范式；
            # HTTP 路由权威入口现为 `from ..daemon_client import route_rpc as _route`
            # （权威来源：tools_security.py:43、tools_rules.py:39）。
            assert "route_rpc as _route" in source, (
                f"{module.__name__} 应经 route_rpc 薄壳入口路由"
            )
            assert "CodeGraphDB(" not in source, (
                f"{module.__name__} 不得直接构造 CodeGraphDB（无 SQLite fallback）"
            )


# ============================================================
# 3. HTTP 模式无 daemon：fail-closed（不回退本地 SQLite）
# ============================================================

class TestFailClosedWithoutDaemon:
    """HTTP 模式无 daemon（无 authority-scoped manifest）时工具体 fail-closed。

    stale 修复：旧断言期望结构化 `E_HTTP_COMPAT_UNSUPPORTED` 回包，并断言
    `_get_daemon_client` 在 legacy 模式被调用；现工具体经 `_route` → `route_rpc`
    → manifest 发现失败 → 抛 `E_HTTP_MANIFEST_MISSING`
    （权威来源：server/daemon_autostart.py:1018-1025），且 `_get_daemon_client`
    已无工具体调用点。保留「绝不调用本地 get_db」的 fail-closed 证据。
    """

    @pytest.mark.parametrize("module", COMPAT_MODULES, ids=lambda m: m.__name__.split(".")[-1])
    def test_tool_fails_closed_without_daemon(self, module, mock_http_mode):
        tools = _register_tools(module)
        name, fn = next(iter(tools.items()))
        args, kwargs = _make_call_args(fn)
        with patch(f"{module.__name__}.get_db") as mock_get_db:
            with pytest.raises((DaemonRemoteError, DaemonUnavailableError)) as ei:
                fn(*args, **kwargs)
            # 无 SQLite fallback 证明：fail-closed 时本地 DB 从未被触达
            mock_get_db.assert_not_called()
        assert "E_HTTP_" in str(ei.value), (
            f"{name} 应以 HTTP fail-closed 错误码终止，实际: {ei.value}"
        )


# ============================================================
# 4. 真实进程级 compat 路由对齐门（迁移至 conftest `w3_live` 隔离 daemon）
# ============================================================

class TestRealDaemonIndexJobRpcAlignment:
    """真实进程级 compat 路由对齐门（H4B-I 产物）。

    stale 修复：旧实现自带隔离 daemon 启动但未重定向 USERPROFILE，且
    `_wait_manifest` 轮询真实 `get_http_manifest_dir()`，manifest 落点错位 →
    4 例 ERROR「隔离 daemon 未发布 manifest」（E_HTTP_MANIFEST_*，W3/W4 已统一为
    模式 A）。现改用 conftest `w3_live`（tests/conftest.py:8，内部
    tests/_w3_harness.py::setup_w3_client 模式 A USERPROFILE 重定向 +
    workspace.register + snapshot.publish）。

    - 正向：已注册 Rust-native 方法 query.stats_top_files
      （rust_ext/src/daemon/dispatch.rs:2883）在真实 daemon 调用下**绝不**返回
      method_not_found；
    - 负向：伪路由候选名（rules.list_toolchains / security.diff_callees）在真实
      daemon 必返回 method_not_found —— 实证 fail-closed 契约。
    """

    def test_compat_registered_method_has_route(self, w3_live):
        """已注册的 query.stats_top_files：不返回 method_not_found。

        stale 修复：旧断言用裸名 `stats_top_files`（旧 compat worker 路由名）；
        P0-COMPAT-v3 / INT-001 迁移为 rust_native 后权威 RPC 名为
        `query.stats_top_files`（权威来源：rust_ext/src/daemon/dispatch.rs:2883）。
        """
        try:
            result = w3_live["client"].call(
                "query.stats_top_files",
                {"workspace_instance_id": w3_live["inst"], "limit": 10},
            )
        except DaemonRemoteError as exc:
            assert exc.code != "method_not_found", (
                f"compat 方法不应 method_not_found（应走 rust_native 路由）: {exc}"
            )
        else:
            assert result is not None

    def test_rules_pseudo_route_returns_method_not_found(self, w3_live):
        """伪路由候选名 rules.* 在真实 daemon 上必返回 method_not_found。"""
        with pytest.raises(DaemonRemoteError) as ei:
            w3_live["client"].call("rules.list_toolchains", {})
        assert ei.value.code == "method_not_found", (
            "tools_rules 工具若走伪路由 rules.* 在 HTTP 模式必失败（fail-closed）"
        )

    def test_security_pseudo_route_returns_method_not_found(self, w3_live):
        """伪路由候选名 security.* 在真实 daemon 上必返回 method_not_found。"""
        with pytest.raises(DaemonRemoteError) as ei:
            w3_live["client"].call("security.diff_callees", {})
        assert ei.value.code == "method_not_found", (
            "tools_security 工具若走伪路由 security.* 在 HTTP 模式必失败（fail-closed）"
        )
