"""H4B-E: Governance/unsupported/error HTTP cutover 测试

验证 tools_p2_graph.py、tools_p3_identity.py、tools_p4_lease.py 中所有工具的
HTTP 路由契约。

stale 依据（A 桶 / MCP 工具 `_route` 化）：
旧版本断言三类旧路由机制——
1. 只读接入组经 `route_worker_call()` → compat worker（tools_p2_graph.py:22 /
   tools_p3_identity.py:22 / tools_p4_lease.py:26 的顶层 import 仍在，但工具体
   内已无调用点，仅剩注释）；
2. 写语义组 HTTP 模式短路 `_http_unsupported()` 返回 E_HTTP_COMPAT_UNSUPPORTED
   （源码已无 `def _http_unsupported`，各模块仅剩注释引用）；
3. rust_native lease_* HTTP 模式经 `_call_daemon_rpc()` 真名透传
   （`from .._mcp_common import _call_daemon_rpc` 仍在 tools_p4_lease.py:23，
   但工具体内已无调用点）。

现行生产实现已全面 `_route` 化：
- `server/tools/tools_p2_graph.py:33`、`tools_p3_identity.py:33`、
  `tools_p4_lease.py:43` 均为
  `from ..daemon_client import route_rpc as _route`；
- 每个工具体退化为一行式 `return _route('<rpc method>', {...}, '<OP_CLASS>')`
  （写语义 job_submit 类工具额外 unwrap `result`）；
- 工具层不再有 HTTP/local 分支，也不再有 compat worker / `_http_unsupported` /
  `_call_daemon_rpc` 分支——HTTP/local/compat 分流与 fail-closed（异常包装为
  `DaemonUnavailableError`）整体下沉到 `route_rpc`（server/daemon_client.py）。

因此本文件按「工具层 `_route` 契约」重写：
- 只读组断言 `_route('<rpc>', {...}, 'READ_ONLY')`，参数逐字透传；
- 写语义组断言 `_route('<rpc>', {...}, 'GOVERNANCE_WRITE'|'PROTECTED_MUTATION')`；
- p4 lease_* 组断言 `_route('lease.*', {...}, ...)`；
- 失败路径断言 `DaemonRemoteError` 原样传播且不回落本地 get_db()
  （fail-closed 语义由 route_rpc 保证，工具层不做本地兜底）。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402


# ============================================================
# 辅助
# ============================================================


def _import_tool_module(module_name):
    import importlib
    return importlib.import_module("callwarden.server.tools." + module_name)


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


def _route_recorder(monkeypatch, module, result):
    """把模块级 `_route` 替换为记录器，返回记录列表 [(method, params, op_class)]。

    现行工具体一律 `return _route(method, params, op_class)`（tools_p2_graph.py:60
    等），故此处直接替换模块属性即可捕获下发路由。
    """
    calls = []

    def _fake(method, params, op_class):
        calls.append((method, dict(params), op_class))
        return result

    monkeypatch.setattr(module, "_route", _fake)
    return calls


def _module_name(module):
    return module.__name__


# ============================================================
# 1. 只读组：`_route(..., 'READ_ONLY')`
# ============================================================

# (module_name, tool_name, 调用 kwargs, rpc_method, 期望 params)
READ_ROUTE_CASES = [
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


class TestReadToolsRouteReadOnly:
    """只读组工具经 `_route(..., 'READ_ONLY')` 下发，参数逐字透传、不碰本地 db。

    stale 依据：旧用例 patch `route_worker_call` 后断言 worker 被调用 / patch
    `_get_daemon_client` 断言客户端便捷方法被调用；工具 `_route` 化后这些 seam
    已无调用点（断言恒为 `Called 0 times`）。
    """

    @pytest.mark.parametrize(
        "module_name, tool_name, kwargs, rpc_method, expect_params",
        READ_ROUTE_CASES,
        ids=[f"{c[0]}:{c[1]}" for c in READ_ROUTE_CASES],
    )
    def test_read_tool_routes_read_only_rpc(
        self, monkeypatch, module_name, tool_name, kwargs, rpc_method, expect_params,
    ):
        module = _import_tool_module(module_name)
        expected = {"ok": True, "value": 1}
        calls = _route_recorder(monkeypatch, module, expected)

        tools = _register_tools(module)
        with patch(f"{_module_name(module)}.get_db") as mock_db:
            out = tools[tool_name](**kwargs)
            mock_db.assert_not_called()

        assert out == expected
        assert calls == [(rpc_method, expect_params, "READ_ONLY")]

    @pytest.mark.parametrize(
        "module_name, tool_name, kwargs, rpc_method, expect_params",
        READ_ROUTE_CASES,
        ids=[f"{c[0]}:{c[1]}" for c in READ_ROUTE_CASES],
    )
    def test_read_tool_fail_closed_no_local_fallback(
        self, monkeypatch, module_name, tool_name, kwargs, rpc_method, expect_params,
    ):
        """`_route` 抛 DaemonRemoteError → 原样传播，绝不回落本地 get_db()。"""
        module = _import_tool_module(module_name)

        def _boom(*a, **kw):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(module, "_route", _boom)
        tools = _register_tools(module)
        with patch(f"{_module_name(module)}.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                tools[tool_name](**kwargs)
            mock_db.assert_not_called()


# ============================================================
# 2. 写语义组：`_route(..., 'GOVERNANCE_WRITE'|'PROTECTED_MUTATION')`
# ============================================================

# (module_name, tool_name, 调用 kwargs, rpc_method, 期望 params, op_class, unwrap_result)
WRITE_ROUTE_CASES = [
    ("tools_p2_graph", "import_envelope_dependencies",
     {"workspace_id": 1, "task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "dependencies": []},
     "task.job_submit",
     {"workspace_id": 1, "task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "dependencies": [],
      "job_type": "envelope_deps", "sync": True},
     "PROTECTED_MUTATION", True),
    ("tools_p2_graph", "record_artifact_identity",
     {"workspace_id": 1, "task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "artifact_type": "file", "artifact_ref": "src/main.py"},
     "admin.record_artifact_identity",
     {"workspace_id": 1, "task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "artifact_type": "file", "artifact_ref": "src/main.py",
      "artifact_hash": "", "workspace_snapshot_id": ""},
     "GOVERNANCE_WRITE", False),
    ("tools_p2_graph", "publish_interface",
     {"workspace_id": 1, "task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "interface_name": "IFace", "version": "1.0"},
     "admin.publish_interface",
     {"workspace_id": 1, "task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "interface_name": "IFace", "version": "1.0",
      "interface_hash": ""},
     "PROTECTED_MUTATION", False),
    ("tools_p2_graph", "select_interface_provider",
     {"workspace_id": 1, "consumer_task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "interface_name": "IFace",
      "selected_provider_task_id": "T-002"},
     "admin.select_interface_provider",
     {"workspace_id": 1, "consumer_task_id": "T-001", "contract_id": "C-001",
      "contract_revision": 1, "interface_name": "IFace",
      "selected_provider_task_id": "T-002"},
     "PROTECTED_MUTATION", False),
    ("tools_p2_graph", "build_hard_dependency_edges",
     {"workspace_id": 1, "contract_id": "C-001", "contract_revision": 1},
     "task.job_submit",
     {"workspace_id": 1, "contract_id": "C-001", "contract_revision": 1,
      "job_type": "hard_dep_edges", "sync": True},
     "PROTECTED_MUTATION", True),
    ("tools_p3_identity", "record_action_identity",
     {"action_id": "ACT-001", "action_type": "contract", "task_id": "T-001",
      "identity": '{"agent_id": "a1", "session_id": "s1", "model_id": "m1", "role": "implementer"}'},
     "admin.record_action_identity",
     {"action_id": "ACT-001", "action_type": "contract", "task_id": "T-001",
      "identity": '{"agent_id": "a1", "session_id": "s1", "model_id": "m1", "role": "implementer"}',
      "contract_id": "", "contract_revision": 0, "workspace_id": None},
     "GOVERNANCE_WRITE", False),
    ("tools_p3_identity", "register_attestation_revocation",
     {"issuer": "issuer-1", "signing_key_id": "key-1", "revocation_mode": "compromised"},
     "admin.register_attestation_revocation",
     {"issuer": "issuer-1", "signing_key_id": "key-1", "revocation_mode": "compromised",
      "revocation_reason": "", "initiating_actor": "", "workspace_id": None},
     "GOVERNANCE_WRITE", False),
    ("tools_p4_lease", "assignment_create",
     {"task_id": "T-001"},
     "admin.assignment_create",
     {"task_id": "T-001", "role": "implementer",
      "agent_id": "", "session_id": "", "model_id": ""},
     "PROTECTED_MUTATION", False),
    ("tools_p4_lease", "assignment_revoke",
     {"assignment_id": "ASG-001"},
     "admin.assignment_revoke",
     {"assignment_id": "ASG-001"},
     "PROTECTED_MUTATION", False),
]


class TestWriteToolsRouteMutation:
    """写语义/治理工具经 `_route(..., 'GOVERNANCE_WRITE'|'PROTECTED_MUTATION')` 下发。

    stale 依据：旧用例 patch `_http_unsupported`（源码已无此函数，仅剩注释）并断言
    返回 E_HTTP_COMPAT_UNSUPPORTED；现行工具体直接 `_route` 到真实 RPC
    （如 tools_p2_graph.py:85 `admin.record_artifact_identity`），无客户端 fail-closed
    短路。HTTP 模式不可用等 fail-closed 语义整体下沉到 route_rpc。
    """

    @pytest.mark.parametrize(
        "module_name, tool_name, kwargs, rpc_method, expect_params, op_class, unwrap",
        WRITE_ROUTE_CASES,
        ids=[f"{c[0]}:{c[1]}" for c in WRITE_ROUTE_CASES],
    )
    def test_write_tool_routes_mutation_rpc(
        self, monkeypatch, module_name, tool_name, kwargs,
        rpc_method, expect_params, op_class, unwrap,
    ):
        module = _import_tool_module(module_name)
        payload = {"result": {"value": 1}} if unwrap else {"value": 1}
        calls = _route_recorder(monkeypatch, module, payload)

        tools = _register_tools(module)
        with patch(f"{_module_name(module)}.get_db") as mock_db:
            out = tools[tool_name](**kwargs)
            mock_db.assert_not_called()

        # job_submit 类工具额外 unwrap `result`；其余原样返回
        assert out == {"value": 1}
        assert calls == [(rpc_method, expect_params, op_class)]

    @pytest.mark.parametrize(
        "module_name, tool_name, kwargs, rpc_method, expect_params, op_class, unwrap",
        WRITE_ROUTE_CASES,
        ids=[f"{c[0]}:{c[1]}" for c in WRITE_ROUTE_CASES],
    )
    def test_write_tool_fail_closed_no_local_fallback(
        self, monkeypatch, module_name, tool_name, kwargs,
        rpc_method, expect_params, op_class, unwrap,
    ):
        """`_route` 抛 DaemonRemoteError → 原样传播，绝不回落本地 get_db()。"""
        module = _import_tool_module(module_name)

        def _boom(*a, **kw):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(module, "_route", _boom)
        tools = _register_tools(module)
        with patch(f"{_module_name(module)}.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                tools[tool_name](**kwargs)
            mock_db.assert_not_called()


# ============================================================
# 3. p4 lease_* 组：`_route('lease.*', ...)`
# ============================================================

# (tool_name, 位置参数 args, rpc_method, 期望 params, op_class)
LEASE_ROUTE_CASES = [
    ("lease_acquire", ("T-001", "implementer", "agent-1", "sess-1", "model-1"),
     "lease.acquire",
     {"task_id": "T-001", "role": "implementer", "agent_id": "agent-1",
      "session_id": "sess-1", "model_id": "model-1", "ttl_seconds": 3600.0},
     "PROTECTED_MUTATION"),
    ("lease_renew", ("T-001", "implementer", "token-abc"),
     "lease.renew",
     {"task_id": "T-001", "role": "implementer", "token": "token-abc",
      "agent_id": "", "session_id": "", "model_id": "", "ttl_seconds": 3600.0},
     "PROTECTED_MUTATION"),
    ("lease_release", ("T-001", "implementer", "token-abc"),
     "lease.release",
     {"task_id": "T-001", "role": "implementer", "token": "token-abc",
      "agent_id": "", "session_id": "", "model_id": ""},
     "PROTECTED_MUTATION"),
    ("lease_status", ("T-001", "implementer"),
     "lease.status",
     {"task_id": "T-001", "role": "implementer"},
     "READ_ONLY"),
    ("lease_list_events", ("T-001", "implementer"),
     "lease.list_events",
     {"task_id": "T-001", "role": "implementer"},
     "READ_ONLY"),
]


class TestToolsP4LeaseRoute:
    """tools_p4_lease.py 中 lease_* 工具现行经 `_route('lease.*', ...)` 下发。

    stale 依据：旧用例 patch `tools_p4_lease._call_daemon_rpc` 并断言真名透传；
    现行工具体为一行式 `_route('lease.acquire', {...}, 'PROTECTED_MUTATION')`
    （tools_p4_lease.py:78/:109/:138/:156/:172），`_call_daemon_rpc` 已无调用点。
    """

    @pytest.mark.parametrize(
        "tool_name, args, rpc_method, expect_params, op_class",
        LEASE_ROUTE_CASES,
        ids=[c[0] for c in LEASE_ROUTE_CASES],
    )
    def test_lease_tool_routes_native_rpc(
        self, monkeypatch, tool_name, args, rpc_method, expect_params, op_class,
    ):
        module = _import_tool_module("tools_p4_lease")
        expected = {"ok": True}
        calls = _route_recorder(monkeypatch, module, expected)

        tools = _register_tools(module)
        with patch(f"{_module_name(module)}.get_db") as mock_db:
            out = tools[tool_name](*args)
            mock_db.assert_not_called()

        assert out == expected
        assert calls == [(rpc_method, expect_params, op_class)]

    @pytest.mark.parametrize(
        "tool_name, args, rpc_method, expect_params, op_class",
        LEASE_ROUTE_CASES,
        ids=[c[0] for c in LEASE_ROUTE_CASES],
    )
    def test_lease_tool_fail_closed_no_local_fallback(
        self, monkeypatch, tool_name, args, rpc_method, expect_params, op_class,
    ):
        module = _import_tool_module("tools_p4_lease")

        def _boom(*a, **kw):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(module, "_route", _boom)
        tools = _register_tools(module)
        with patch(f"{_module_name(module)}.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                tools[tool_name](*args)
            mock_db.assert_not_called()


# ============================================================
# 4. 路由覆盖完整性（cutover 已完成，无旧 seam 残留）
# ============================================================


class TestRouteCoverage:
    """三个模块所有工具已完整 `_route` 化，工具体内无旧 seam 残留。

    stale 依据：旧用例断言 `route_worker_call` / `_http_unsupported` /
    `_call_daemon_rpc` 出现在工具源码中；现行工具一律 `return _route(...)`
    （tools_p2_graph.py:60/85/98/115/128/144/159/171/187/202，
    tools_p3_identity.py:66/82/98/114/139/161/191，
    tools_p4_lease.py:78/109/138/156/172/199/213/228），旧 marker 已消失。
    """

    MODULE_TOOLS = {
        "tools_p2_graph": [
            "import_envelope_dependencies", "record_artifact_identity",
            "get_artifact_freshness", "publish_interface",
            "get_interface_providers", "select_interface_provider",
            "build_hard_dependency_edges", "detect_cycle",
            "validate_revision_dependencies", "get_dependency_edges",
        ],
        "tools_p3_identity": [
            "record_action_identity", "get_action_identity",
            "check_action_identity", "check_session_separation",
            "get_attestation_validity", "list_attestation_revocations",
            "register_attestation_revocation",
        ],
        "tools_p4_lease": [
            "lease_acquire", "lease_renew", "lease_release",
            "lease_status", "lease_list_events",
            "assignment_create", "assignment_show", "assignment_revoke",
        ],
    }

    STALE_MARKERS = ("_http_unsupported", "_call_daemon_rpc", "route_worker_call")

    @pytest.mark.parametrize(
        "module_name, expected_tools",
        list(MODULE_TOOLS.items()),
        ids=list(MODULE_TOOLS),
    )
    def test_all_tools_route_via_route_rpc(self, module_name, expected_tools):
        import inspect

        module = _import_tool_module(module_name)
        tools = _register_tools(module)
        assert sorted(tools) == sorted(expected_tools), (
            f"{module_name} 工具集与预期不一致：{sorted(tools)}"
        )
        for name, fn in tools.items():
            assert "_route(" in inspect.getsource(fn), (
                f"{name} 未 `_route` 化（工具体内缺少 '_route('）"
            )
            for marker in self.STALE_MARKERS:
                assert marker not in inspect.getsource(fn), (
                    f"{name} 工具体内仍含过期 seam '{marker}'"
                )
