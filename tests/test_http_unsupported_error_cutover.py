"""H4B-E: Governance/unsupported/error HTTP cutover 测试

验证 tools_p2_graph.py、tools_p3_identity.py、tools_p4_lease.py 中工具的 HTTP 路由。

三类路由语义（H4C-2 第三批 T-1786747295227-b876fddf 适配版）：
1. 只读接入组（本批 11 个 + collab 4 个共 15 个）：HTTP/enterprise 模式经
   route_worker_call → compat worker 执行（fail-closed，不构造 CodeGraphDB、
   无 SQLite fallback）；local/auto 模式走 _local（保留原 get_db() legacy 语义）。
2. 写语义组（governance_write，10 个）：HTTP 模式短路 _http_unsupported 返回
   E_HTTP_COMPAT_UNSUPPORTED，绝不触碰 get_db()/本地 SQLite。
3. rust_native 组（p4 lease_* 5 个）：HTTP 模式经 _call_daemon_rpc 真名透传
   （daemon 权威路径，dispatch.rs 有真实 lease.* RPC 分支，不经 worker）。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 装配导入：import compat_worker 即触发其内部对工具模块的装配 import，
# 模块级 register_compat_routes 随之注册到 registry 单例（与
# test_http_combined_worker_cutover 同款）。只读接入组测试依赖
# route_worker_call 的白名单检查通过（未注册方法会 fail-closed 返回
# E_HTTP_COMPAT_UNSUPPORTED 而非调用 worker）。
import server.compat_worker as _compat_worker_asm  # noqa: E402,F401


# ============================================================
# 辅助：Mock 工具函数的通用夹具
# ============================================================


@pytest.fixture
def mock_http_worker_route(monkeypatch):
    """HTTP 模式 + mock rpc client 的 route_worker_call 基座。

    route_worker_call 内部动态读取 callwarden.server.daemon_client 模块属性
    （is_http_transport_enabled / get_daemon_mode / _get_rpc_client_for_route），
    monkeypatch daemon_client 上的绑定即可生效（与
    test_http_combined_worker_cutover.mock_http_worker_route 同款）。
    """

    def _apply(mode="auto"):
        client = MagicMock()
        monkeypatch.setattr(
            "callwarden.server.daemon_client.is_http_transport_enabled",
            lambda: True,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client.get_daemon_mode",
            lambda: mode,
        )
        monkeypatch.setattr(
            "callwarden.server.daemon_client._get_rpc_client_for_route",
            lambda: client,
        )
        return client

    return _apply


@pytest.fixture
def mock_http_fail_closed(monkeypatch):
    """HTTP 模式（fail-closed 短路）：写语义工具 _http_unsupported 通过
    `import callwarden.server.daemon_client as _dc; _dc.is_http_transport_enabled()`
    动态读取，monkeypatch daemon_client 模块属性后立即短路返回
    E_HTTP_COMPAT_UNSUPPORTED。"""
    monkeypatch.setattr(
        "callwarden.server.daemon_client.is_http_transport_enabled",
        lambda: True,
    )


@pytest.fixture
def mock_http_lease_native(monkeypatch):
    """HTTP 模式（rust_native lease_*）：工具函数顶层绑定
    `from ...daemon_client import is_http_transport_enabled`，必须 monkeypatch
    tools_p4_lease 模块上的绑定（daemon_client 上的改动对顶层绑定不生效）。"""
    import callwarden.server.tools.tools_p4_lease as _lease_mod

    monkeypatch.setattr(_lease_mod, "is_http_transport_enabled", lambda: True)


@pytest.fixture
def mock_legacy_mode(monkeypatch):
    """legacy 模式（非 HTTP）：
    - route_worker_call：mode=local 且非 HTTP → 直接执行 _local fallback（get_db()）
    - _http_unsupported：is_http_transport_enabled=False → 返回 None 走 get_db()
    - p4 lease_*：顶层绑定 is_http_transport_enabled / get_daemon_mode 同步覆盖
    """
    import callwarden.server.tools.tools_p4_lease as _lease_mod

    monkeypatch.setattr(
        "callwarden.server.daemon_client.is_http_transport_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "callwarden.server.daemon_client.get_daemon_mode",
        lambda: "local",
    )
    monkeypatch.setattr(_lease_mod, "is_http_transport_enabled", lambda: False)
    monkeypatch.setattr(_lease_mod, "get_daemon_mode", lambda: "local")


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
# 参数化清单（H4C-2 第三批，T-1786747295227-b876fddf）
# ============================================================

# 写语义工具（governance_write，HTTP 模式必须短路 _http_unsupported 返回
# E_HTTP_COMPAT_UNSUPPORTED，不触碰 get_db()/本地 SQLite）。
# 参数按各工具真实签名提供（仅需满足 Python 调用不抛 TypeError，
# _http_unsupported 在进入 db 写路径前拦截）。
FAIL_CLOSED_TOOLS = [
    ("tools_p2_graph", "import_envelope_dependencies",
     {"workspace_id": 1, "task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "dependencies": []}),
    ("tools_p2_graph", "record_artifact_identity",
     {"workspace_id": 1, "task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "artifact_type": "file", "artifact_ref": "src/main.py"}),
    ("tools_p2_graph", "publish_interface",
     {"workspace_id": 1, "task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "interface_name": "IFace", "version": "1.0"}),
    ("tools_p2_graph", "select_interface_provider",
     {"workspace_id": 1, "consumer_task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "interface_name": "IFace",
      "selected_provider_task_id": "T-002"}),
    ("tools_p2_graph", "build_hard_dependency_edges",
     {"workspace_id": 1, "contract_id": "C-001", "contract_revision": 1}),
    ("tools_p3_identity", "record_action_identity",
     {"action_id": "ACT-001", "action_type": "contract", "task_id": "T-001",
      "identity": '{"agent_id": "a1", "session_id": "s1", "model_id": "m1", "role": "implementer"}'}),
    ("tools_p3_identity", "register_attestation_revocation",
     {"issuer": "issuer-1", "signing_key_id": "key-1", "revocation_mode": "compromised"}),
    ("tools_p4_lease", "assignment_create",
     {"task_id": "T-001", "role": "implementer"}),
    ("tools_p4_lease", "assignment_revoke", {"assignment_id": "ASG-001"}),
]

# 只读接入组（route_worker_call，HTTP/enterprise 经 worker 执行）：
# (module_name, tool_name, 调用 kwargs, rpc_method, 期望 worker params)
WORKER_ROUTED_TOOLS = [
    ("tools_p2_graph", "get_artifact_freshness",
     {"workspace_id": 1, "task_id": "T-001", "artifact_ref": "src/main.py"},
     "get_artifact_freshness",
     {"workspace_id": 1, "task_id": "T-001", "artifact_ref": "src/main.py"}),
    ("tools_p2_graph", "get_interface_providers",
     {"workspace_id": 1, "interface_name": "IFace", "version": "1.0"},
     "get_interface_providers",
     {"workspace_id": 1, "interface_name": "IFace", "version": "1.0"}),
    ("tools_p2_graph", "detect_cycle",
     {"workspace_id": 1},
     "detect_cycle",
     {"workspace_id": 1}),
    ("tools_p2_graph", "validate_revision_dependencies",
     {"workspace_id": 1, "contract_id": "C-001", "contract_revision": 1},
     "validate_revision_dependencies",
     {"workspace_id": 1, "contract_id": "C-001", "contract_revision": 1}),
    ("tools_p2_graph", "get_dependency_edges",
     {"workspace_id": 1, "task_id": "T-001"},
     "get_dependency_edges",
     {"workspace_id": 1, "task_id": "T-001"}),
    ("tools_p3_identity", "get_action_identity",
     {"action_id": "ACT-001"},
     "get_action_identity",
     {"action_id": "ACT-001", "workspace_id": None}),
    ("tools_p3_identity", "check_action_identity",
     {"identity": '{"agent_id": "a1", "session_id": "s1", "model_id": "m1", "role": "implementer"}',
      "require_role": "implementer"},
     "check_action_identity",
     {"identity": '{"agent_id": "a1", "session_id": "s1", "model_id": "m1", "role": "implementer"}',
      "require_role": "implementer"}),
    ("tools_p3_identity", "check_session_separation",
     {"reviewer_identity": '{"agent_id": "r1", "session_id": "s1", "model_id": "m1", "role": "reviewer"}',
      "implementer_identity": '{"agent_id": "a1", "session_id": "s2", "model_id": "m1", "role": "implementer"}'},
     "check_session_separation",
     {"reviewer_identity": '{"agent_id": "r1", "session_id": "s1", "model_id": "m1", "role": "reviewer"}',
      "implementer_identity": '{"agent_id": "a1", "session_id": "s2", "model_id": "m1", "role": "implementer"}'}),
    ("tools_p3_identity", "get_attestation_validity",
     {"issuer": "issuer-1", "signing_key_id": "key-1", "issuance_time": 1234567890.0},
     "get_attestation_validity",
     {"issuer": "issuer-1", "signing_key_id": "key-1",
      "issuance_time": 1234567890.0, "workspace_id": None}),
    ("tools_p3_identity", "list_attestation_revocations",
     {"issuer": "issuer-1", "signing_key_id": "key-1"},
     "list_attestation_revocations",
     {"issuer": "issuer-1", "signing_key_id": "key-1", "workspace_id": None}),
    ("tools_p4_lease", "assignment_show",
     {"task_id": "T-001", "role": "implementer"},
     "assignment_show",
     {"task_id": "T-001", "role": "implementer"}),
]


def _import_tool_module(module_name):
    import importlib
    return importlib.import_module("callwarden.server.tools." + module_name)


# ============================================================
# 1. 只读接入组：HTTP 模式经 route_worker_call → compat worker
# ============================================================


class TestReadonlyToolsWorkerRouted:
    """本批只读接入组（p2 5 + p3 5 + p4 1）HTTP 模式经 worker 执行。

    mock client 返回 worker 数据，工具函数原样透传；断言 route_worker_call
    以 (rpc_method, params) 精确调用（HTTP fail-closed 不回退本地 SQLite）。
    """

    @pytest.mark.parametrize(
        "module_name, tool_name, kwargs, rpc_method, expected_params",
        WORKER_ROUTED_TOOLS,
        ids=[t[1] for t in WORKER_ROUTED_TOOLS],
    )
    def test_readonly_tool_worker_routed(
        self, mock_http_worker_route,
        module_name, tool_name, kwargs, rpc_method, expected_params,
    ):
        module = _import_tool_module(module_name)
        client = mock_http_worker_route()
        expected = {"ok": True, "worker": "compat"}
        client.call.return_value = expected

        tools = _register_tools(module)
        result = tools[tool_name](**kwargs)

        client.call.assert_called_once_with(rpc_method, expected_params)
        assert result == expected


# ============================================================
# 2. 写语义组：HTTP 模式 fail-closed（_http_unsupported 短路）
# ============================================================


class TestWriteToolsFailClosed:
    """写语义/治理工具（governance_write）HTTP 模式必须短路 _http_unsupported。

    返回结构化 E_HTTP_COMPAT_UNSUPPORTED；get_db 以抛错桩替换，若工具绕过
    fail-closed 触碰本地 SQLite 立即失败。
    """

    @pytest.mark.parametrize(
        "module_name, tool_name, kwargs",
        FAIL_CLOSED_TOOLS,
        ids=[t[1] for t in FAIL_CLOSED_TOOLS],
    )
    def test_write_tool_fail_closed(
        self, mock_http_fail_closed, monkeypatch,
        module_name, tool_name, kwargs,
    ):
        module = _import_tool_module(module_name)

        def _boom(*args, **kw):
            raise AssertionError(
                f"{tool_name} HTTP 模式不应触碰 get_db()/本地 SQLite")

        monkeypatch.setattr(module, "get_db", _boom)

        tools = _register_tools(module)
        result = tools[tool_name](**kwargs)

        assert result["error"] == "E_HTTP_COMPAT_UNSUPPORTED", (
            f"{tool_name} HTTP 模式应 fail-closed 返回 E_HTTP_COMPAT_UNSUPPORTED"
        )
        assert result["tool"] == tool_name
        assert result["backend"] == "python_compat"


# ============================================================
# 3. rust_native 组（p4 lease_*）：HTTP 模式经 _call_daemon_rpc 真名透传
# ============================================================


class TestToolsP4LeaseHttpRouting:
    """tools_p4_lease.py 中 lease_*（rust_native）HTTP 路由。

    dispatch.rs 有真实 lease.* RPC 分支（daemon 权威路径），HTTP 模式经
    _call_daemon_rpc 真名透传，不经 worker、不走本地 SQLite。
    """

    def test_lease_acquire_http_native(self, mock_http_lease_native):
        """lease_acquire HTTP 模式通过 _call_daemon_rpc 真名透传。"""
        from callwarden.server.tools import tools_p4_lease

        expected = {"ok": True, "lease_id": "L-001", "token": "secret"}
        with patch("callwarden.server.tools.tools_p4_lease._call_daemon_rpc") as mock_rpc:
            mock_rpc.return_value = expected
            tools = _register_tools(tools_p4_lease)
            result = tools["lease_acquire"]("T-001", "implementer", "agent-1", "sess-1", "model-1")
            mock_rpc.assert_called_once()
            args = mock_rpc.call_args[0]
            assert args[0] == "lease.acquire"
            assert args[1]["task_id"] == "T-001"
            assert result == expected

    def test_lease_renew_http_native(self, mock_http_lease_native):
        """lease_renew HTTP 模式通过 _call_daemon_rpc 真名透传。"""
        from callwarden.server.tools import tools_p4_lease

        expected = {"ok": True, "lease_id": "L-001", "renewed_at": 1234567890.0}
        with patch("callwarden.server.tools.tools_p4_lease._call_daemon_rpc") as mock_rpc:
            mock_rpc.return_value = expected
            tools = _register_tools(tools_p4_lease)
            result = tools["lease_renew"]("T-001", "implementer", "token-abc")
            mock_rpc.assert_called_once()
            assert mock_rpc.call_args[0][0] == "lease.renew"
            assert result == expected

    def test_lease_release_http_native(self, mock_http_lease_native):
        """lease_release HTTP 模式通过 _call_daemon_rpc 真名透传。"""
        from callwarden.server.tools import tools_p4_lease

        expected = {"ok": True, "lease_id": "L-001", "released_at": 1234567890.0}
        with patch("callwarden.server.tools.tools_p4_lease._call_daemon_rpc") as mock_rpc:
            mock_rpc.return_value = expected
            tools = _register_tools(tools_p4_lease)
            result = tools["lease_release"]("T-001", "implementer", "token-abc")
            mock_rpc.assert_called_once()
            assert mock_rpc.call_args[0][0] == "lease.release"
            assert result == expected

    def test_lease_status_http_native(self, mock_http_lease_native):
        """lease_status HTTP 模式通过 _call_daemon_rpc 真名透传。"""
        from callwarden.server.tools import tools_p4_lease

        expected = {"status": "active", "lease_id": "L-001"}
        with patch("callwarden.server.tools.tools_p4_lease._call_daemon_rpc") as mock_rpc:
            mock_rpc.return_value = expected
            tools = _register_tools(tools_p4_lease)
            result = tools["lease_status"]("T-001", "implementer")
            mock_rpc.assert_called_once_with(
                "lease.status", {"task_id": "T-001", "role": "implementer"},
            )
            assert result == expected

    def test_lease_list_events_http_native(self, mock_http_lease_native):
        """lease_list_events HTTP 模式通过 _call_daemon_rpc 真名透传。"""
        from callwarden.server.tools import tools_p4_lease

        expected = [{"event_id": "EVT-001", "event_type": "acquire"}]
        with patch("callwarden.server.tools.tools_p4_lease._call_daemon_rpc") as mock_rpc:
            mock_rpc.return_value = expected
            tools = _register_tools(tools_p4_lease)
            result = tools["lease_list_events"]("T-001", "implementer")
            mock_rpc.assert_called_once_with(
                "lease.list_events", {"task_id": "T-001", "role": "implementer"},
            )
            assert result == expected


# ============================================================
# 4. legacy 模式：保留本地 get_db() 执行路径
# ============================================================


class TestLegacyFallback:
    """legacy（非 HTTP）模式：只读接入组走 _local（get_db()）、写语义组与
    rust_native 组走原 get_db() 本地路径，公开方法语义不变。"""

    def test_legacy_fallback_import_envelope_dependencies(self, mock_legacy_mode):
        """legacy 模式下 import_envelope_dependencies（写语义）走 get_db() 路径。"""
        from callwarden.server.tools import tools_p2_graph

        with patch("callwarden.server.tools.tools_p2_graph.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.import_envelope_dependencies.return_value = {"imported": 3, "skipped": 0, "errors": []}
            mock_get_db.return_value = mock_db
            tools = _register_tools(tools_p2_graph)
            result = tools["import_envelope_dependencies"](
                1, "T-001", "C-001", 1,
                [{"dependency_type": "requires_existing", "target_ref": "fn_a"}],
            )
            assert result == {"imported": 3, "skipped": 0, "errors": []}
            mock_get_db.assert_called_once()

    def test_legacy_fallback_detect_cycle(self, mock_legacy_mode):
        """legacy 模式下 detect_cycle（只读接入组）走 _local → get_db() 路径。"""
        from callwarden.server.tools import tools_p2_graph

        with patch("callwarden.server.tools.tools_p2_graph.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.detect_cycle.return_value = {"has_cycle": False, "cycle_path": []}
            mock_get_db.return_value = mock_db
            tools = _register_tools(tools_p2_graph)
            result = tools["detect_cycle"](1)
            assert result == {"has_cycle": False, "cycle_path": []}
            mock_get_db.assert_called_once()

    def test_legacy_fallback_get_action_identity(self, mock_legacy_mode):
        """legacy 模式下 get_action_identity（只读接入组）走 _local → get_db() 路径。"""
        from callwarden.server.tools import tools_p3_identity

        with patch("callwarden.server.tools.tools_p3_identity.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.get_action_identity.return_value = {"action_id": "ACT-001", "agent_id": "a1"}
            mock_get_db.return_value = mock_db
            tools = _register_tools(tools_p3_identity)
            result = tools["get_action_identity"]("ACT-001")
            assert result == {"action_id": "ACT-001", "agent_id": "a1"}
            mock_get_db.assert_called_once()

    def test_legacy_fallback_record_action_identity(self, mock_legacy_mode):
        """legacy 模式下 record_action_identity（写语义）走 get_db() 路径。"""
        from callwarden.server.tools import tools_p3_identity

        with patch("callwarden.server.tools.tools_p3_identity.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.validate_action_identity.return_value = (True, {})
            mock_db.record_action_identity.return_value = (True, {"code": "OK"})
            mock_get_db.return_value = mock_db
            tools = _register_tools(tools_p3_identity)
            result = tools["record_action_identity"](
                "ACT-001", "contract", "T-001",
                '{"agent_id": "a1", "session_id": "s1", "model_id": "m1", "role": "implementer"}',
            )
            assert result == {"code": "OK"}

    def test_legacy_fallback_lease_status(self, mock_legacy_mode):
        """legacy 模式下 lease_status（rust_native）走 get_db() 本地路径。"""
        from callwarden.server.tools import tools_p4_lease

        with patch("callwarden.server.tools.tools_p4_lease.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.get_lease_status.return_value = {"status": "active", "lease_id": "L-001"}
            mock_get_db.return_value = mock_db
            tools = _register_tools(tools_p4_lease)
            result = tools["lease_status"]("T-001", "implementer")
            assert result == {"status": "active", "lease_id": "L-001"}


# ============================================================
# 5. 路由覆盖完整性验证（三类语义全覆盖）
# ============================================================


class TestRouteCoverage:
    """验证三个模块所有工具已接入三类 HTTP 路由语义：
    只读接入组 → route_worker_call；写语义组 → _http_unsupported；
    rust_native 组（lease_*）→ _call_daemon_rpc 真名透传。"""

    P2_READ_ONLY = [
        "get_artifact_freshness", "get_interface_providers", "detect_cycle",
        "validate_revision_dependencies", "get_dependency_edges",
    ]
    P2_WRITE = [
        "import_envelope_dependencies", "record_artifact_identity",
        "publish_interface", "select_interface_provider",
        "build_hard_dependency_edges",
    ]
    P3_READ_ONLY = [
        "get_action_identity", "check_action_identity",
        "check_session_separation", "get_attestation_validity",
        "list_attestation_revocations",
    ]
    P3_WRITE = ["record_action_identity", "register_attestation_revocation"]
    P4_LEASE_NATIVE = [
        "lease_acquire", "lease_renew", "lease_release",
        "lease_status", "lease_list_events",
    ]
    P4_WRITE = ["assignment_create", "assignment_revoke"]
    P4_READ_ONLY = ["assignment_show"]

    def _assert_markers(self, module, mapping, expected_total):
        import inspect

        tools = _register_tools(module)
        assert len(tools) == expected_total, (
            f"模块工具数应为 {expected_total}，实际 {len(tools)}"
        )
        for name, fn in tools.items():
            source = inspect.getsource(fn)
            marker, desc = mapping[name]
            assert marker in source, (
                f"{name} 缺少 {desc}（期望含 '{marker}'）"
            )

    def test_tools_p2_graph_all_routed(self):
        """tools_p2_graph.py：只读 5 个 route_worker_call，写 5 个 _http_unsupported。"""
        from callwarden.server.tools import tools_p2_graph

        mapping = {m: ("route_worker_call", "route_worker_call") for m in self.P2_READ_ONLY}
        mapping.update({m: ("_http_unsupported", "_http_unsupported") for m in self.P2_WRITE})
        self._assert_markers(tools_p2_graph, mapping, expected_total=10)

    def test_tools_p3_identity_all_routed(self):
        """tools_p3_identity.py：只读 5 个 route_worker_call，写 2 个 _http_unsupported。"""
        from callwarden.server.tools import tools_p3_identity

        mapping = {m: ("route_worker_call", "route_worker_call") for m in self.P3_READ_ONLY}
        mapping.update({m: ("_http_unsupported", "_http_unsupported") for m in self.P3_WRITE})
        self._assert_markers(tools_p3_identity, mapping, expected_total=7)

    def test_tools_p4_lease_all_routed(self):
        """tools_p4_lease.py：lease_* 5 个 _call_daemon_rpc 真名透传，
        assignment_create/revoke 写语义 _http_unsupported，
        assignment_show 只读接入 route_worker_call。"""
        from callwarden.server.tools import tools_p4_lease

        mapping = {m: ("_call_daemon_rpc", "_call_daemon_rpc") for m in self.P4_LEASE_NATIVE}
        mapping.update({m: ("_http_unsupported", "_http_unsupported") for m in self.P4_WRITE})
        mapping.update({m: ("route_worker_call", "route_worker_call") for m in self.P4_READ_ONLY})
        self._assert_markers(tools_p4_lease, mapping, expected_total=8)
