"""H4B-E / H4C-3: governance/unsupported/error HTTP cutover 测试

验证 tools_collab.py / tools_p2_graph.py / tools_p3_identity.py /
tools_p4_lease.py / tools_task.py 中工具的 HTTP 路由契约与 fail-closed 语义。

stale 依据（A 桶 / MCP 工具 `_route` 化）：
旧版本断言三类已失效的旧路由机制——
1. 只读工具经 `route_worker_call()` → compat worker（tools_collab.py:38 /
   tools_p2_graph.py:22 / tools_p3_identity.py:22 / tools_p4_lease.py:26 /
   tools_task.py:33 的顶层 import 仍在，但工具体内已无调用点）；
2. 写语义工具 HTTP 模式短路 `_http_unsupported()` 返回
   E_HTTP_COMPAT_UNSUPPORTED（源码已无 `def _http_unsupported`，仅剩注释）；
3. rust_native lease.* HTTP 模式经 `_call_daemon_rpc()` 真名透传，task 便捷
   方法经 `_get_daemon_client()` 便捷方法（源码工具体内已无调用点）。

现行生产实现已全面 `_route` 化：
- `server/tools/tools_collab.py:50`、`tools_p2_graph.py:33`、
  `tools_p3_identity.py:33`、`tools_p4_lease.py:43`、`tools_task.py:46` 均为
  `from ..daemon_client import route_rpc as _route`；
- 每个工具体退化为一行式 `return _route('<rpc method>', {...}, '<OP_CLASS>')`
  （job_submit 类工具额外 unwrap `result`）；
- 工具层不再有 HTTP/local 分支，也不再有 compat worker / `_http_unsupported` /
  `_call_daemon_rpc` / `_get_daemon_client` 分支——HTTP/local/compat 分流与
  fail-closed（异常包装为 `DaemonUnavailableError`）整体下沉到 `route_rpc`
  （server/daemon_client.py:3880-3994）。

因此本文件按「工具层 `_route` 契约」重写：
- 只读组断言 `_route('<rpc>', {...}, 'READ_ONLY')`，参数逐字透传；
- 写语义组断言 `_route('<rpc>', {...}, 'GOVERNANCE_WRITE'|'PROTECTED_MUTATION')`；
- 失败路径断言 `DaemonRemoteError` 原样传播且不回落本地 get_db()。

真实进程门（TestRealDaemonGovernanceErrorRpcAlignment）保持原样不动：其失败属
环境/harness 类（无隔离 daemon manifest），本任务不负责迁移。
"""

import inspect
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.tools import (
    tools_collab,
    tools_p2_graph,
    tools_p3_identity,
    tools_p4_lease,
    tools_task,
)


# ============================================================
# 辅助
# ============================================================


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
    """把模块级 `_route` 替换为记录器，返回记录列表 [(method, params, op_class)]。"""
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
    # collab 4
    ("tools_collab", "get_role_view",
     {"task_id": "T-1"}, "get_role_view",
     {"task_id": "T-1", "role": ""}),
    ("tools_collab", "find_evidence",
     {}, "find_evidence",
     {"task_id": "", "contract_id": "", "verifier": "", "limit": 50}),
    ("tools_collab", "get_freshness_status",
     {}, "get_freshness_status",
     {"evidence_id": "", "task_id": ""}),
    ("tools_collab", "get_gate_decision",
     {}, "get_gate_decision",
     {"task_id": "", "gate_id": "", "limit": 20}),
    # p2 5
    ("tools_p2_graph", "get_artifact_freshness",
     {"workspace_id": 1, "task_id": "T-001", "artifact_ref": "src/main.py"},
     "get_artifact_freshness",
     {"workspace_id": 1, "task_id": "T-001", "artifact_ref": "src/main.py"}),
    ("tools_p2_graph", "get_interface_providers",
     {"workspace_id": 1, "interface_name": "IFace", "version": "1.0"},
     "get_interface_providers",
     {"workspace_id": 1, "interface_name": "IFace", "version": "1.0"}),
    ("tools_p2_graph", "detect_cycle",
     {"workspace_id": 1}, "detect_cycle", {"workspace_id": 1}),
    ("tools_p2_graph", "validate_revision_dependencies",
     {"workspace_id": 1, "contract_id": "C-001", "contract_revision": 1},
     "validate_revision_dependencies",
     {"workspace_id": 1, "contract_id": "C-001", "contract_revision": 1}),
    ("tools_p2_graph", "get_dependency_edges",
     {"workspace_id": 1, "task_id": "T-001"},
     "get_dependency_edges",
     {"workspace_id": 1, "task_id": "T-001"}),
    # p3 5
    ("tools_p3_identity", "get_action_identity",
     {"action_id": "ACT-001"}, "get_action_identity",
     {"action_id": "ACT-001", "workspace_id": None}),
    ("tools_p3_identity", "check_action_identity",
     {"identity": '{"agent_id": "a1", "session_id": "s1", "model_id": "m1", "role": "implementer"}'},
     "check_action_identity",
     {"identity": '{"agent_id": "a1", "session_id": "s1", "model_id": "m1", "role": "implementer"}',
      "require_role": ""}),
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
    # p4 1
    ("tools_p4_lease", "assignment_show",
     {"task_id": "T-001", "role": "implementer"},
     "assignment_show",
     {"task_id": "T-001", "role": "implementer"}),
]


class TestReadToolsRouteReadOnly:
    """只读组工具经 `_route(..., 'READ_ONLY')` 下发，参数逐字透传、不碰本地 db。

    stale 依据：旧用例 patch `route_worker_call` / `_get_daemon_client` 后断言
    worker / 客户端便捷方法被调用；工具 `_route` 化后这些 seam 已无调用点
    （断言恒为 `Called 0 times`），且 HTTP 模式不再走 compat worker。
    """

    @pytest.mark.parametrize(
        "module_name, tool_name, kwargs, rpc_method, expect_params",
        READ_ROUTE_CASES,
        ids=[f"{c[0]}:{c[1]}" for c in READ_ROUTE_CASES],
    )
    def test_read_tool_routes_read_only_rpc(
        self, monkeypatch, module_name, tool_name, kwargs, rpc_method, expect_params,
    ):
        module = __import__(
            "callwarden.server.tools." + module_name, fromlist=["register"])
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
        module = __import__(
            "callwarden.server.tools." + module_name, fromlist=["register"])

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
    # p2 写 5
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
    # p3 写 2
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
    # p4 写 2
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
    # collab 写 2（任务 4：daemon 权威薄壳 forwarding）
    ("tools_collab", "submit_verdict",
     {"task_id": "T-1", "step_id": "S-1", "contract_id": "C-1",
      "contract_revision": 1, "contract_hash": "h",
      "role_contract_id": "RC-1", "role_contract_revision": 1,
      "role_contract_hash": "rh"},
     "verdict.submit",
     {"task_id": "T-1", "step_id": "S-1", "contract_id": "C-1",
      "contract_revision": 1, "contract_hash": "h",
      "role_contract_id": "RC-1", "role_contract_revision": 1,
      "role_contract_hash": "rh",
      "phase": "PRE_VERDICT", "overall": "",
      "clause_results": [], "findings": [],
      "reviewer_identity": "", "view_manifest_hash": "", "snapshot_id": "",
      "attestation": "", "amendment_ref": "", "verdict_id": "",
      "lease_token": "", "fencing_counter": 0,
      "identity": {"agent_id": "", "agent_instance_id": "", "session_id": "",
                   "model_id": "", "role": "reviewer"},
      "request_id": ""},
     "GOVERNANCE_WRITE", False),
    ("tools_collab", "append_evidence",
     {"task_id": "T-1", "step_id": "S-1", "evidence_id": "E-1",
      "evidence_type": "test_run", "manifest_path": "docs/evidence/e1.json"},
     "evidence.append",
     {"task_id": "T-1", "step_id": "S-1", "evidence_id": "E-1",
      "evidence_type": "test_run", "manifest_path": "docs/evidence/e1.json",
      "contract_id": "", "contract_revision": 0, "contract_hash": "",
      "snapshot_id": "", "verifier_name": "", "verifier_version": "",
      "verifier_config_hash": "", "producer_identity": "",
      "payload": "", "payload_hash": "", "test_run_id": "",
      "lease_token": "", "fencing_counter": 0,
      "identity_role": "implementer", "identity_agent_id": "",
      "identity_session_id": "", "identity_model_id": "",
      "request_id": ""},
     "GOVERNANCE_WRITE", False),
]


class TestWriteToolsRouteMutation:
    """写语义/治理工具经 `_route(..., 'GOVERNANCE_WRITE'|'PROTECTED_MUTATION')` 下发。

    stale 依据：旧用例 patch `_http_unsupported`（源码已无此函数，仅剩注释）并断言
    返回 E_HTTP_COMPAT_UNSUPPORTED；现行工具体直接 `_route` 到真实 RPC
    （如 tools_collab.py:277 `verdict.submit`、tools_p2_graph.py:85
    `admin.record_artifact_identity`），无客户端 fail-closed 短路。HTTP 模式不可用
    等 fail-closed 语义整体下沉到 route_rpc。
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
        module = __import__(
            "callwarden.server.tools." + module_name, fromlist=["register"])
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
        module = __import__(
            "callwarden.server.tools." + module_name, fromlist=["register"])

        def _boom(*a, **kw):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(module, "_route", _boom)
        tools = _register_tools(module)
        with patch(f"{_module_name(module)}.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                tools[tool_name](**kwargs)
            mock_db.assert_not_called()


# ============================================================
# 3. tools_task 只读便捷方法：`_route(..., 'READ_ONLY')`
# ============================================================

# (tool_name, 调用 kwargs, rpc_method, 期望 params)
TASK_READ_ROUTE_CASES = [
    ("get_symbol_issues", {"qualified_name": "x"},
     "query.issues", {"qualified_name": "x", "include_info": False}),
    ("get_test_cases", {"qualified_name": "x"},
     "query.tests", {"qualified_name": "x"}),
    ("get_tested_functions", {"test_qualified_name": "x"},
     "query.tests", {"test_qualified_name": "x"}),
    ("get_test_coverage_summary", {"qualified_name": "x"},
     "query.tests", {"qualified_name": "x"}),
    ("get_test_stability", {"qualified_name": "x"},
     "query.tests", {"qualified_name": "x", "limit": 50}),
    ("get_commit_tasks", {"commit_hash": "abc123"},
     "query.commit_tasks", {"commit_hash": "abc123", "include_task_details": True}),
    ("get_symbol_change_tasks", {},
     "get_symbol_change_tasks",
     {"symbol_hash": "", "qualified_name": "", "limit": 50}),
    ("task_plan_template", {}, "task_plan_template", {}),
]


class TestTaskReadToolsRouteReadOnly:
    """tools_task 只读便捷方法经 `_route(..., 'READ_ONLY')` 下发。

    stale 依据：旧用例 patch `tools_task._get_daemon_client` 后断言
    client.query_issues()/query_tests() 便捷方法被调用（M2.4/M2.5），或断言
    `route_task_read` / `route_worker_call` 直传；现行工具体为一行式
    `_route('query.issues'|'query.tests'|'query.commit_tasks'|...)`
    （tools_task.py:532/554/574/596/621/237/219/1005），旧 seam 已无调用点。
    """

    @pytest.mark.parametrize(
        "tool_name, kwargs, rpc_method, expect_params",
        TASK_READ_ROUTE_CASES,
        ids=[c[0] for c in TASK_READ_ROUTE_CASES],
    )
    def test_task_read_tool_routes_read_only_rpc(
        self, monkeypatch, tool_name, kwargs, rpc_method, expect_params,
    ):
        expected = {"ok": True, "value": 1}
        calls = _route_recorder(monkeypatch, tools_task, expected)

        tools = _register_tools(tools_task)
        with patch(f"{tools_task.__name__}.get_db") as mock_db:
            out = tools[tool_name](**kwargs)
            mock_db.assert_not_called()

        assert out == expected
        assert calls == [(rpc_method, expect_params, "READ_ONLY")]

    @pytest.mark.parametrize(
        "tool_name, kwargs, rpc_method, expect_params",
        TASK_READ_ROUTE_CASES,
        ids=[c[0] for c in TASK_READ_ROUTE_CASES],
    )
    def test_task_read_tool_fail_closed_no_local_fallback(
        self, monkeypatch, tool_name, kwargs, rpc_method, expect_params,
    ):
        def _boom(*a, **kw):
            raise DaemonRemoteError("E_HTTP_DAEMON_UNAVAILABLE", "daemon 不可达")

        monkeypatch.setattr(tools_task, "_route", _boom)
        tools = _register_tools(tools_task)
        with patch(f"{tools_task.__name__}.get_db") as mock_db:
            with pytest.raises(DaemonRemoteError):
                tools[tool_name](**kwargs)
            mock_db.assert_not_called()

    def test_no_method_not_found_leak_via_route(self, monkeypatch):
        """HTTP 模式：get_symbol_change_tasks / task_plan_template 经 `_route`
        下发（不再 route_task_read 直传伪路由 RPC 名），不泄漏 method_not_found。"""
        monkeypatch.setattr(tools_task, "route_task_read", MagicMock(
            side_effect=AssertionError("不应经 route_task_read 直传伪路由")))
        expected = {"ok": True, "no": "leak"}
        calls = _route_recorder(monkeypatch, tools_task, expected)
        tools = _register_tools(tools_task)
        with patch(f"{tools_task.__name__}.get_db") as mock_db:
            assert tools["get_symbol_change_tasks"]() == expected
            assert tools["task_plan_template"]() == expected
            mock_db.assert_not_called()
        assert calls == [
            ("get_symbol_change_tasks",
             {"symbol_hash": "", "qualified_name": "", "limit": 50}, "READ_ONLY"),
            ("task_plan_template", {}, "READ_ONLY"),
        ]


# ============================================================
# 4. p4 lease_* 组：`_route('lease.*', ...)`
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


class TestLeaseRoute:
    """lease.* 工具现行经 `_route('lease.*', ...)` 下发（真实 RPC 保留）。

    stale 依据：旧用例 patch `tools_p4_lease._call_daemon_rpc` 并断言真名透传；
    现行工具体为一行式 `_route('lease.acquire', {...}, 'PROTECTED_MUTATION')`
    （tools_p4_lease.py:78/:109/:138/:156/:172），`_call_daemon_rpc` 已无调用点。
    """

    LEASE_RPCS = {
        "lease_acquire": "lease.acquire",
        "lease_renew": "lease.renew",        # dispatch.rs 兼容别名 → lease.extend
        "lease_release": "lease.release",
        "lease_status": "lease.status",
        "lease_list_events": "lease.list_events",
    }

    @pytest.mark.parametrize(
        "tool_name, args, rpc_method, expect_params, op_class",
        LEASE_ROUTE_CASES,
        ids=[c[0] for c in LEASE_ROUTE_CASES],
    )
    def test_lease_tool_routes_native_rpc(
        self, monkeypatch, tool_name, args, rpc_method, expect_params, op_class,
    ):
        expected = {"ok": True}
        calls = _route_recorder(monkeypatch, tools_p4_lease, expected)
        tools = _register_tools(tools_p4_lease)
        with patch(f"{tools_p4_lease.__name__}.get_db") as mock_db:
            out = tools[tool_name](*args)
            mock_db.assert_not_called()
        assert out == expected
        assert calls == [(rpc_method, expect_params, op_class)]

    def test_lease_tools_passthrough_all_real_rpcs(self, monkeypatch):
        calls = _route_recorder(monkeypatch, tools_p4_lease, {"ok": True})
        tools = _register_tools(tools_p4_lease)
        # 各 lease 工具所需位置参数（部分工具体（如 lease_acquire）task_id 为必填），
        # 复用 LEASE_ROUTE_CASES 中已校验的调用签名。
        args_by_tool = {case[0]: case[1] for case in LEASE_ROUTE_CASES}
        for name, rpc in self.LEASE_RPCS.items():
            tools[name](*args_by_tool[name])
        called = [c[0] for c in calls]
        for rpc in self.LEASE_RPCS.values():
            assert rpc in called, f"lease 工具应透传 {rpc}，实际 {called}"

    def test_lease_rpc_names_match_dispatch_rs(self):
        """静态验证：lease.* 真名存在于 dispatch.rs（与 daemon 端对齐）。"""
        dispatch = open(
            os.path.join("rust_ext", "src", "daemon", "dispatch.rs"),
            encoding="utf-8",
        ).read()
        for rpc in self.LEASE_RPCS.values():
            # lease.renew 作为 lease.extend 的兼容别名存在
            assert rpc in dispatch, f"{rpc} 必须在 dispatch.rs 有分支"


# ============================================================
# 5. fail-closed 静态验证：无伪路由 / 无本地 SQLite 直构造
# ============================================================

# 本任务相关的伪路由 RPC 前缀（dispatch.rs 均无对应分支）
PSEUDO_ROUTE_PREFIXES = ('"p2.', '"p3.', '"p4.')


class TestNoPseudoRoutes:
    """fail-closed：相关模块不得存在指向不存在 RPC 的伪路由。"""

    def test_p2_p3_no_daemon_rpc_pseudo_route(self):
        """p2/p3 工具源码不得含旧 seam（_call_daemon_rpc / _get_daemon_client /
        _http_unsupported）；现行工具一律 `_route` 化。"""
        for module in (tools_p2_graph, tools_p3_identity):
            tools = _register_tools(module)
            for name, fn in tools.items():
                source = inspect.getsource(fn)
                assert "_call_daemon_rpc" not in source, (
                    f"{name} 不应有 daemon RPC 伪路由"
                )
                assert "_get_daemon_client" not in source, (
                    f"{name} 不应有 client 伪路由"
                )
                assert "_http_unsupported" not in source, (
                    f"{name} 不应有 _http_unsupported 旧 seam"
                )

    def test_no_pseudo_route_rpc_strings_in_tool_source(self):
        """工具函数源码不得含 "p2./"p3./"p4. 伪路由 RPC 字符串。"""
        modules = [tools_p2_graph, tools_p3_identity, tools_p4_lease,
                   tools_collab]
        for module in modules:
            tools = _register_tools(module)
            for name, fn in tools.items():
                source = inspect.getsource(fn)
                for prefix in PSEUDO_ROUTE_PREFIXES:
                    assert prefix not in source, (
                        f"{name} 不应含伪路由 RPC 字符串 {prefix}"
                    )

    def test_no_direct_codegraphdb_construction(self):
        """模块级不得直接构造 CodeGraphDB（无 SQLite fallback）。

        stale 依据：旧用例额外断言 `def _http_unsupported in source`；现行模块已无
        该函数（仅剩注释引用），故移除该断言，仅保留「不直接构造 CodeGraphDB」。
        """
        for module in (tools_collab, tools_p2_graph, tools_p3_identity,
                       tools_p4_lease):
            source = inspect.getsource(module)
            assert "CodeGraphDB(" not in source, (
                f"{module.__name__} 不得直接构造 CodeGraphDB（无 SQLite fallback）"
            )


# ============================================================
# 6. route_rpc 层：HTTP 模式 workspace 权威注入
# ============================================================


def test_http_mode_route_rpc_injects_workspace_id_for_workspace_scoped_methods(
    monkeypatch,
):
    """HTTP 模式 route_rpc 对 workspace-scoped 方法注入权威 workspace。

    stale 依据：旧断言假设 route_rpc 从本地 get_db() 读取 active workspace
    （=7）；现行 route_rpc 不再读本地 SQLite（daemon_client.py:3915-3974）：
    - workspace-scoped（无非空 task_id）方法注入 workspace_instance_id（经
      `_ensure_remote_snapshot`）+ 数值 workspace_id（task./lease. 经 daemon RPC
      `mcp.daemon_client.inject_workspace_id` 权威注入，daemon_client.py:3509）；
    - task-scoped（携带非空 task_id/superseded_id）请求整个 workspace 注入块被
      跳过（binding 权威优先，daemon_client.py:3942-3974）。
    """
    import callwarden.server.daemon_client as dc

    captured = {}

    class _FakeHttpClient:
        # route_rpc 访问 client._project_root / configure_workspace（daemon_client.py:3924）
        _project_root = None

        @staticmethod
        def get_instance():
            return _singleton

        def configure_workspace(self, root):
            self._project_root = root

        def _ensure_remote_snapshot(self, db_path):
            return "ws-instance-abc"

        def call(self, method, params=None, request_id=None):
            if method == "mcp.daemon_client.inject_workspace_id":
                # 模拟 daemon 权威解析 active workspace → 注入数值 workspace_id
                injected = dict((params or {}).get("params", {}))
                injected["workspace_id"] = 7
                return {"params": injected}
            captured["method"] = method
            captured["params"] = params
            return {"ok": True}

    _singleton = _FakeHttpClient()

    monkeypatch.setattr(dc, "is_http_transport_enabled", lambda: True)
    monkeypatch.setattr(dc, "get_daemon_mode", lambda: "auto")
    monkeypatch.setattr(dc, "HttpDaemonRpcClient", _FakeHttpClient)
    monkeypatch.setattr(dc, "_get_rpc_client_for_route", lambda: _singleton)

    # workspace-scoped task.create：注入 workspace_instance_id + 数值 workspace_id
    dc.route_rpc("task.create", {"title": "t"}, op_class="GOVERNANCE_WRITE")
    assert captured["method"] == "task.create"
    p = captured["params"]
    assert p["workspace_instance_id"] == "ws-instance-abc"
    assert p["workspace_id"] == 7

    # workspace-scoped lease.list_events（无 task_id）：同样注入两者
    dc.route_rpc("lease.list_events", {}, op_class="READ_ONLY")
    assert captured["params"]["workspace_instance_id"] == "ws-instance-abc"
    assert captured["params"]["workspace_id"] == 7

    # task-scoped（携带 task_id）：binding 权威优先，不注入 workspace
    dc.route_rpc("task.status", {"task_id": "T-1"}, op_class="READ_ONLY")
    p = captured["params"]
    assert "workspace_instance_id" not in p
    assert "workspace_id" not in p

    # 非任务读方法（query.issues）：只注入 workspace_instance_id，不注入数值 id
    dc.route_rpc("query.issues", {"issue_id": "i1"}, op_class="READ_ONLY")
    p = captured["params"]
    assert p["workspace_instance_id"] == "ws-instance-abc"
    assert "workspace_id" not in p


# ============================================================
# 7. 真实进程级 RPC 对齐门（参照 H4B-C/I TestRealDaemon*RpcAlignment）
# ============================================================


def _find_daemon_binary():
    """定位 current-HEAD 构建的 cw-daemon 二进制（与 H4B-C/I 集成门同源）。

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


def _wait_manifest(data_root, proc, timeout=10.0):
    """等待隔离 daemon 发布 authority-scoped manifest（仅接受 pid 匹配当前进程）。

    H6 修复（9d6ca63，2026-08-15）后 manifest 固定写 `USERPROFILE/.callwarden/`
    （http_manifest_dir），隔离 daemon 的 USERPROFILE = data_root/userhome，
    故轮询 data_root/userhome/.callwarden；data_root 根目录不再有 manifest。
    """
    manifest_dir = os.path.join(data_root, "userhome", ".callwarden")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isdir(manifest_dir):
            for f in os.listdir(manifest_dir):
                if f.startswith("http-daemon.") and f.endswith(".manifest.json"):
                    p = os.path.join(manifest_dir, f)
                    try:
                        m = json.loads(open(p, encoding="utf-8").read())
                    except (OSError, ValueError):
                        continue
                    if m.get("pid") == proc.pid:
                        return m
        time.sleep(0.2)
    return None


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 管道 / USERPROFILE）。"""
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    # compat worker 使用与 daemon 同版本的 Python 解释器
    env["CW_COMPAT_PYTHON"] = sys.executable
    home_dir = Path(data_root) / "userhome"
    home_dir.mkdir(parents=True, exist_ok=True)
    # H6：manifest 固定写 USERPROFILE/.callwarden，须先建目录否则 daemon 发布失败
    (home_dir / ".callwarden").mkdir(parents=True, exist_ok=True)
    env["USERPROFILE"] = str(home_dir)
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


class TestRealDaemonGovernanceErrorRpcAlignment:
    """真实进程级 RPC 对齐门（H4B-E 产物）。

    - 正向：dispatch.rs 真实 RPC（lease.acquire / lease.status / lease.renew
      [→lease.extend 兼容别名] / task.status / query.issues）在生产
      HttpDaemonRpcClient 调用下**绝不**返回 method_not_found；
    - 负向：若本任务工具建立伪路由（p2.detect_cycle / p3.check_action_identity
      / p4.assignment_show），真实 daemon 必返回 method_not_found —— 实证
      fail-closed 契约（伪路由在 HTTP 模式必失败）。
    """

    POSITIVE_RPCS = [
        # (rpc, params) —— 只要求不返回 method_not_found（业务校验失败可接受）
        ("lease.acquire", {
            "task_id": "T-real-daemon-gate",
            "role": "implementer",
            "ttl_seconds": 60.0,
            "identity": {"agent_id": "gate", "session_id": "gate",
                         "model_id": "gate", "role": "implementer"},
        }),
        ("lease.status", {"task_id": "T-real-daemon-gate", "role": ""}),
        # lease.renew 是 lease.extend 的兼容别名（dispatch.rs 合并分支）
        ("lease.renew", {
            "task_id": "T-real-daemon-gate", "role": "implementer",
            "token": "bad-token", "ttl_seconds": 60.0,
        }),
        ("task.status", {"task_id": "T-real-daemon-gate"}),
        ("query.issues", {"qualified_name": "x", "include_info": False}),
    ]

    NEGATIVE_RPCS = [
        "p2.detect_cycle",
        "p3.check_action_identity",
        "p4.assignment_show",
        # H4B-E 整改（H4B-M 复判）：tools_task 3 个曾经 route_task_read 直传的
        # 伪路由 RPC 名（dispatch.rs 无分支）——真实 daemon 必 method_not_found，
        # 实证 fail-closed 契约（HTTP 模式不得泄漏 method_not_found）。
        "task.get_change_tasks",
        "task.get_commit_tasks",
        "task.plan_template",
    ]

    @pytest.fixture
    def real_daemon(self, tmp_path):
        """启动隔离真实 daemon，yield 生产类 HttpDaemonRpcClient。"""
        bin_path = _find_daemon_binary()
        if bin_path is None:
            pytest.skip("cw-daemon 二进制不可用（需先 cargo build --bin cw-daemon）")
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        proc = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
        try:
            manifest = _wait_manifest(data_root, proc)
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

    def test_positive_rpcs_never_method_not_found(self, real_daemon):
        """正向：真实 RPC 在生产 client 下绝不返回 method_not_found。"""
        for rpc, params in self.POSITIVE_RPCS:
            try:
                result = real_daemon.call(rpc, params)
            except DaemonRemoteError as exc:
                assert exc.code != "method_not_found", (
                    f"{rpc} 是 dispatch.rs 真实 RPC，不应 method_not_found: {exc}"
                )
            else:
                assert result is not None

    def test_pseudo_routes_return_method_not_found(self, real_daemon):
        """负向：伪路由候选名在真实 daemon 上必返回 method_not_found。"""
        for rpc in self.NEGATIVE_RPCS:
            with pytest.raises(DaemonRemoteError) as ei:
                real_daemon.call(rpc, {})
            assert ei.value.code == "method_not_found", (
                f"{rpc} 伪路由在 HTTP 模式必 method_not_found（fail-closed）"
            )
