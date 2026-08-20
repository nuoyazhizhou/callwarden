"""H4B-E / H4C-3: governance/unsupported/error HTTP cutover 测试

验证 tools_p2_graph.py / tools_p3_identity.py / tools_p4_lease.py /
tools_collab.py / tools_task.py 在 HTTP daemon 模式下的路由契约。

H4C-2/3 整改（T-1786747295227-b876fddf 接入 H3 compat worker）后三类语义：
- **只读 read_only 工具（15 个，本批接入）→ route_worker_call 经 compat
  worker 执行**：HTTP/enterprise 模式 fail-closed 走 worker（白名单外方法
  返回结构化 E_HTTP_COMPAT_UNSUPPORTED，不回退本地 SQLite）；local/auto
  模式走 _local（保留 get_db() legacy 语义）。
  接入清单：collab 4（get_role_view/find_evidence/get_freshness_status/
  get_gate_decision）+ p2 5（get_artifact_freshness/get_interface_providers/
  detect_cycle/validate_revision_dependencies/get_dependency_edges）+
  p3 5（get_action_identity/check_action_identity/check_session_separation/
  get_attestation_validity/list_attestation_revocations）+ p4 1
  （assignment_show）；另 tools_task 3 个（get_symbol_change_tasks/
  get_commit_tasks/task_plan_template）在 H4C-3 已接入。
- **写语义工具（11 个）→ _http_unsupported() 结构化 E_HTTP_COMPAT_UNSUPPORTED
  fail-closed**：p2 写 5（import_envelope_dependencies/record_artifact_identity/
  publish_interface/select_interface_provider/build_hard_dependency_edges）+
  p3 写 2（record_action_identity/register_attestation_revocation）+
  p4 assignment 写 2（assignment_create/assignment_revoke）+
  collab 写 2（submit_verdict/append_evidence）。worker 只读连接无法承载写
  操作，写语义工具不接入 worker（governance_write 维度由 MVP 禁止）。
  任务 4 起 collab 写 2 改经 daemon RPC 薄壳转发（_collab_write_rpc →
  verdict.submit / evidence.append，业务逻辑全在 Rust daemon），不再
  _http_unsupported，也不回退本地 SQLite。
- **rust_native lease.* 5 个 → _call_daemon_rpc 真名透传**（dispatch.rs 有
  真实 lease.* RPC 分支，禁止改动）；task.* / query.* 便捷方法保留原有透传。

归类依据：.trae-cn/evidence/http-daemon-capability-matrix.json（237 tools 矩阵）
- 本批 15 个接入工具 current_status=entry_verified → runtime_verified；
  其余 python_compat 工具 backend=python_compat、daemon_rpc_method=none。
- dispatch.rs 无 p2.* / p3.* / p4.* / role_view.* / evidence.* / freshness.* /
  gate.* 伪路由 RPC 分支（grep 实证），HTTP 模式不得直传这些假名。

真实进程门（TestRealDaemonGovernanceErrorRpcAlignment，参照 H4B-C/I 模板）：
- 正向：lease.acquire / lease.status / lease.renew（→lease.extend 兼容别名）/
  task.status / query.issues 在生产 HttpDaemonRpcClient 调用下**绝不**返回
  method_not_found；
- 负向：伪路由候选名（p2.detect_cycle / p3.check_action_identity /
  p4.assignment_show / task.get_change_tasks / task.get_commit_tasks /
  task.plan_template）在真实 daemon 上必返回 method_not_found —— 实证
  fail-closed 契约（HTTP 模式只允许经 compat registry 注册名路由）。
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
# 辅助夹具
# ============================================================

# 只读接入 worker 的工具模块（route_worker_call，HTTP/enterprise 经 compat
# worker）：collab 4 + p2 5 + p3 5 + p4 1 = 15 个只读工具对应模块完整清单
READONLY_WORKER_MODULES = [
    tools_collab,
    tools_p2_graph,
    tools_p3_identity,
    tools_p4_lease,
]

# 15 个只读工具名 → 所属模块（HTTP/enterprise 模式经 route_worker_call 调
# compat worker，返回值原样透传；local/auto 走 _local）
READONLY_WORKER_TOOLS = {
    # collab 4（_local 内保留 _collab_rpc_call legacy 语义）
    "get_role_view": tools_collab,
    "find_evidence": tools_collab,
    "get_freshness_status": tools_collab,
    "get_gate_decision": tools_collab,
    # p2 5（_local 内 get_db()）
    "get_artifact_freshness": tools_p2_graph,
    "get_interface_providers": tools_p2_graph,
    "detect_cycle": tools_p2_graph,
    "validate_revision_dependencies": tools_p2_graph,
    "get_dependency_edges": tools_p2_graph,
    # p3 5（_local 内 get_db()）
    "get_action_identity": tools_p3_identity,
    "check_action_identity": tools_p3_identity,
    "check_session_separation": tools_p3_identity,
    "get_attestation_validity": tools_p3_identity,
    "list_attestation_revocations": tools_p3_identity,
    # p4 1（_local 内 get_db()）
    "assignment_show": tools_p4_lease,
}

# 本任务相关的伪路由 RPC 前缀（dispatch.rs 均无对应分支）
PSEUDO_ROUTE_PREFIXES = ('"p2.', '"p3.', '"p4.')

# 写语义 fail-closed 工具（_http_unsupported("<name>")，不接入 worker）：
# p2 写 5 + p3 写 2 + p4 assignment 写 2 + collab 写 2 = 11
WRITE_FAIL_CLOSED_TOOLS = {
    "import_envelope_dependencies": tools_p2_graph,
    "record_artifact_identity": tools_p2_graph,
    "publish_interface": tools_p2_graph,
    "select_interface_provider": tools_p2_graph,
    "build_hard_dependency_edges": tools_p2_graph,
    "record_action_identity": tools_p3_identity,
    "register_attestation_revocation": tools_p3_identity,
    "assignment_create": tools_p4_lease,
    "assignment_revoke": tools_p4_lease,
    "submit_verdict": tools_collab,
    "append_evidence": tools_collab,
}


@pytest.fixture
def mock_http_mode(monkeypatch):
    """启用 HTTP 模式（daemon_client.is_http_transport_enabled 返回 True）。

    写语义工具的 _http_unsupported 通过模块属性动态读取 daemon_client，
    monkeypatch 模块级即可生效；p4 顶层绑定 import 的
    is_http_transport_enabled 需要单独 monkeypatch（见 mock_p4_http_mode）。
    """
    monkeypatch.setattr(
        "callwarden.server.daemon_client.is_http_transport_enabled",
        lambda: True,
    )


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
def mock_legacy_mode(monkeypatch):
    """legacy 模式（非 HTTP）：
    - route_worker_call：mode=local 且非 HTTP → 直接执行 _local fallback（get_db()）
    - _http_unsupported：is_http_transport_enabled=False → 返回 None 走 get_db()
    - p4 顶层绑定 is_http_transport_enabled / get_daemon_mode 同步覆盖
      （顶层绑定 import 不受 daemon_client 模块级改动影响）
    """
    monkeypatch.setattr(
        "callwarden.server.daemon_client.is_http_transport_enabled",
        lambda: False,
    )
    monkeypatch.setattr(
        "callwarden.server.daemon_client.get_daemon_mode",
        lambda: "local",
    )
    monkeypatch.setattr(tools_p4_lease, "is_http_transport_enabled", lambda: False)
    monkeypatch.setattr(tools_p4_lease, "get_daemon_mode", lambda: "local")


@pytest.fixture
def mock_p4_http_mode(monkeypatch):
    """启用 p4 模块顶层的 is_http_transport_enabled（顶层绑定 import）。"""
    monkeypatch.setattr(
        tools_p4_lease,
        "is_http_transport_enabled",
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


def _assert_http_unsupported(result, name):
    """断言 HTTP 模式结构化 unsupported 返回（fail-closed 契约）。"""
    assert isinstance(result, dict), f"{name} HTTP 模式应返回结构化 dict"
    assert result.get("error") == "E_HTTP_COMPAT_UNSUPPORTED", name
    assert result.get("backend") == "python_compat", name
    assert result.get("tool") == name, name


# ============================================================
# 1. HTTP 模式结构化 unsupported（写语义 11 个 fail-closed）
# ============================================================

class TestHttpGovernanceUnsupported:
    """写语义工具在 HTTP 模式下 fail-closed 返回结构化 unsupported。

    H4C-2/3 整改（T-1786747295227-b876fddf）后新契约：15 个只读工具
    （collab 4 + p2 5 + p3 5 + p4 1）已接入 compat worker，HTTP/enterprise
    模式经 route_worker_call 执行（正向见 TestHttpWorkerRouteReadonlyTools，
    不再 fail-closed）；仅写语义 11 个工具（governance_write）保持
    _http_unsupported() 结构化 E_HTTP_COMPAT_UNSUPPORTED fail-closed，不构造
    CodeGraphDB、无 SQLite fallback。
    """

    @pytest.mark.parametrize(
        "tool_name",
        [
            n for n, m in WRITE_FAIL_CLOSED_TOOLS.items()
            if m in (tools_p2_graph, tools_p3_identity)
        ],
    )
    def test_p2_p3_write_tools_fail_closed_in_http_mode(
        self, tool_name, mock_http_mode
    ):
        """p2/p3 写语义 7 个工具（import_envelope_dependencies 等）HTTP 模式
        fail-closed；p2/p3 只读工具已接入 worker（见 TestHttpWorkerRouteReadonlyTools）。"""
        module = WRITE_FAIL_CLOSED_TOOLS[tool_name]
        tools = _register_tools(module)
        with patch(f"{module.__name__}.get_db") as mock_get_db:
            fn = tools[tool_name]
            args, kwargs = _make_call_args(fn)
            result = fn(*args, **kwargs)
            _assert_http_unsupported(result, tool_name)
            # 无 SQLite fallback 证明：HTTP 模式下 get_db 从未被调用
            mock_get_db.assert_not_called()

    def test_p4_assignment_write_tools_fail_closed_in_http_mode(
        self, mock_http_mode, mock_p4_http_mode
    ):
        """p4 仅 assignment_create / assignment_revoke 写语义 fail-closed；
        assignment_show 已接入 worker 路由（见 TestHttpWorkerRouteReadonlyTools）。"""
        tools = _register_tools(tools_p4_lease)
        write_tools = {
            n: fn for n, fn in tools.items()
            if n in ("assignment_create", "assignment_revoke")
        }
        assert set(write_tools) == {"assignment_create", "assignment_revoke"}
        with patch(f"{tools_p4_lease.__name__}.get_db") as mock_get_db:
            for name, fn in write_tools.items():
                args, kwargs = _make_call_args(fn)
                result = fn(*args, **kwargs)
                _assert_http_unsupported(result, name)
            mock_get_db.assert_not_called()

    def test_collab_write_tools_route_to_daemon_in_http_mode(self, mock_http_mode):
        """collab 写 2（submit_verdict / append_evidence）HTTP 模式经 daemon
        权威 RPC 薄壳转发（任务 4）。

        任务 4 契约（Python 只做 cw 客户端薄壳）：legacy 枚举适配为 native v1
        wire 后经 `verdict.submit` / `evidence.append` RPC 透传，业务校验/落库
        全在 Rust daemon。HTTP 模式走 HttpDaemonRpcClient.call（无
        call_with_autostart）；绝不触碰 get_db() / _collab_direct_read。
        """
        tools = _register_tools(tools_collab)
        write_tools = {
            n: fn for n, fn in tools.items()
            if n in ("submit_verdict", "append_evidence")
        }
        assert set(write_tools) == {"submit_verdict", "append_evidence"}
        expected_method = {
            "submit_verdict": "verdict.submit",
            "append_evidence": "evidence.append",
        }
        call_kwargs = {
            "submit_verdict": {
                "task_id": "T-1", "step_id": "S-1",
                "contract_id": "C-1", "contract_revision": 1,
                "contract_hash": "h",
                "role_contract_id": "RC-1", "role_contract_revision": 1,
                "role_contract_hash": "rh",
                "phase": "PRE_VERDICT", "overall": "approved",
                "lease_token": "tok", "fencing_counter": 1,
            },
            "append_evidence": {
                "task_id": "T-1", "step_id": "S-1", "evidence_id": "E-1",
                "evidence_type": "test_run",
                "manifest_path": "docs/evidence/e1.json",
                "lease_token": "tok", "fencing_counter": 1,
            },
        }
        with (
            patch(f"{tools_collab.__name__}.get_db") as mock_get_db,
            patch(f"{tools_collab.__name__}._collab_direct_read") as mock_direct,
        ):
            for name, fn in write_tools.items():
                client = object.__new__(HttpDaemonRpcClient)
                client.call = MagicMock(return_value={"ok": True, "sentinel": name})
                with patch(f"{tools_collab.__name__}._get_daemon_client",
                           return_value=client):
                    result = fn(**call_kwargs[name])
                # _collab_write_rpc 契约：daemon dict 结果补 success=True 原样透传
                assert result == {
                    "ok": True, "sentinel": name, "success": True,
                }, name
                call_method, call_params = client.call.call_args[0]
                assert call_method == expected_method[name], (
                    f"{name} 应经 {expected_method[name]} RPC 透传"
                )
                if name == "submit_verdict":
                    # legacy→v1 冻结枚举迁移（§4.2 表）
                    assert call_params["phase"] == "blind_first_pass", name
                    assert call_params["overall"] == "pass", name
            mock_get_db.assert_not_called()
            mock_direct.assert_not_called()

    def test_collab_write_tools_daemon_unavailable_fail_closed(self, mock_http_mode):
        """collab 写 2 daemon 不可达时 fail-closed 返回 E_*_DAEMON_UNAVAILABLE，
        不回退本地 SQLite（governance_write 语义，degraded_mode）。"""
        tools = _register_tools(tools_collab)
        call_kwargs = {
            "submit_verdict": {
                "task_id": "T-1", "step_id": "S-1",
                "contract_id": "C-1", "contract_revision": 1,
                "contract_hash": "h",
                "role_contract_id": "RC-1", "role_contract_revision": 1,
                "role_contract_hash": "rh",
                "phase": "PRE_VERDICT", "overall": "approved",
                "lease_token": "tok", "fencing_counter": 1,
            },
            "append_evidence": {
                "task_id": "T-1", "step_id": "S-1", "evidence_id": "E-1",
                "evidence_type": "test_run",
                "manifest_path": "docs/evidence/e1.json",
                "lease_token": "tok", "fencing_counter": 1,
            },
        }
        from callwarden.server.daemon_client import DaemonUnavailableError
        with (
            patch(f"{tools_collab.__name__}.get_db") as mock_get_db,
            patch(f"{tools_collab.__name__}._collab_direct_read") as mock_direct,
        ):
            for name, kwargs in call_kwargs.items():
                client = object.__new__(HttpDaemonRpcClient)
                client.call = MagicMock(
                    side_effect=DaemonUnavailableError("daemon down")
                )
                with patch(f"{tools_collab.__name__}._get_daemon_client",
                           return_value=client):
                    result = tools[name](**kwargs)
                assert result["success"] is False, name
                assert result["error"]["code"].endswith("_DAEMON_UNAVAILABLE"), name
            mock_get_db.assert_not_called()
            mock_direct.assert_not_called()


# ============================================================
# 1b. HTTP/enterprise 模式 worker 路由正向（15 个只读工具）
# ============================================================

class TestHttpWorkerRouteReadonlyTools:
    """HTTP/enterprise 模式：15 个只读工具经 route_worker_call 调 compat worker。

    H4C-2/3 整改核心正向契约：15 个 python_compat 只读工具接入 worker 后，
    HTTP/enterprise 模式不再 fail-closed，而是经 route_worker_call 路由到
    compat worker（mock rpc client 替代——route_worker_call 内部
    _get_rpc_client_for_route 已被 mock_http_worker_route fixture patch 为
    client），函数返回值 = client.call 结果原样透传，get_db 从未被调用
    （无本地 SQLite fallback）。

    route_worker_call 语义（daemon_client.py 可读参考）：HTTP/enterprise 模式
    经 worker 执行（白名单前置检查命中 compat_registry read_only 注册——本
    文件顶部 import 工具模块即触发模块级 register_compat_routes）；白名单外
    fail-closed E_HTTP_COMPAT_UNSUPPORTED；local/auto 走 _local。mock 时
    client.call 的调用参数第一个即方法名（如 "get_role_view"）。
    """

    def test_http_mode_worker_route_passthrough(
        self, mock_http_worker_route, monkeypatch
    ):
        """HTTP 模式（auto + enterprise）：15 个只读工具经 route_worker_call 调
        worker，函数返回值 = client.call 结果原样透传，client.call 首参为对应
        工具名，get_db 从未被调用。"""
        registered = {}
        for mode in ("auto", "enterprise"):
            client = mock_http_worker_route(mode)
            for name, module in READONLY_WORKER_TOOLS.items():
                if module not in registered:
                    registered[module] = _register_tools(module)
                expected = {"ok": True, "sentinel": name}
                client.call.return_value = expected
                client.call.reset_mock()
                fn = registered[module][name]
                args, kwargs = _make_call_args(fn)
                with patch(f"{module.__name__}.get_db") as mock_get_db:
                    result = fn(*args, **kwargs)
                    # 返回值 = worker 结果原样透传（未做 unsupported 包装）
                    assert result == expected, (
                        f"{name} ({mode}) 应原样透传 worker 结果，实际 {result}"
                    )
                    # client.call 首参为 compat worker 路由方法名（= 工具名）
                    assert client.call.call_args[0][0] == name, (
                        f"{name} ({mode}) 应路由到 compat worker 方法名 {name}"
                    )
                    # HTTP 模式绝不触碰本地 SQLite（_local 不执行）
                    mock_get_db.assert_not_called()


# ============================================================
# 2. lease.* 真实 RPC 保留（HTTP 模式真名透传，不做 unsupported 包装）
# ============================================================

class TestLeaseKeepsRealRpc:
    """5 个 lease.* 工具在 HTTP 模式保留 _call_daemon_rpc 真名透传。"""

    LEASE_TOOLS = ["lease_acquire", "lease_renew", "lease_release",
                   "lease_status", "lease_list_events"]
    LEASE_RPCS = {
        "lease_acquire": "lease.acquire",
        "lease_renew": "lease.renew",        # dispatch.rs 兼容别名 → lease.extend
        "lease_release": "lease.release",
        "lease_status": "lease.status",
        "lease_list_events": "lease.list_events",
    }

    def test_lease_tools_passthrough_real_rpc(self, mock_http_mode, mock_p4_http_mode):
        tools = _register_tools(tools_p4_lease)
        for name in self.LEASE_TOOLS:
            assert name in tools, f"{name} 应保留真实 RPC 分支"
        with patch(f"{tools_p4_lease.__name__}._call_daemon_rpc") as mock_rpc:
            mock_rpc.return_value = {"ok": True, "rpc": "sentinel"}
            for name in self.LEASE_TOOLS:
                args, kwargs = _make_call_args(tools[name])
                result = tools[name](*args, **kwargs)
                # 返回 _call_daemon_rpc 原样结果（未做 unsupported 包装）
                assert result == {"ok": True, "rpc": "sentinel"}, name
            # 每个工具都透传了真实 RPC 名（与 dispatch.rs / daemon_client 对齐）
            called_methods = [c.args[0] for c in mock_rpc.call_args_list]
            for name, rpc in self.LEASE_RPCS.items():
                assert rpc in called_methods, f"{name} 应透传 {rpc}，实际 {called_methods}"

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
# 3. task 5 个便捷方法工具：HTTP 模式经通用 call 透传真实 RPC
# ============================================================

class TestTaskConvenienceMethodsRpcPassthrough:
    """tools_task 5 个便捷方法工具 HTTP 模式调用便捷方法（M2.4/M2.5）。

    背景：HttpDaemonRpcClient 无 get_symbol_issues / get_test_cases /
    get_tested_functions / get_test_coverage_summary / get_test_stability，
    直接调用会 AttributeError 后 except 返回错误。M2.4（query.issues）+
    M2.5（query.tests）整改：is_http_client 分支改为调用 HttpDaemonRpcClient
    便捷方法 client.query_issues(...) / client.query_tests(...)（内部注入
    workspace_instance_id 并经 daemon RPC query.issues / query.tests 执行），
    不再经通用 client.call 透传（H4B-E 旧断言已随 M2.5 更新）。
    """

    EXPECTED_CALLS = {
        "get_symbol_issues": ("query_issues", {
            "qualified_name": "x", "include_info": False,
        }),
        "get_test_cases": ("query_tests", {
            "qualified_name": "x", "reverse": False, "history": False, "limit": 50,
        }),
        "get_tested_functions": ("query_tests", {
            "qualified_name": "x", "reverse": True, "history": False, "limit": 50,
        }),
        "get_test_stability": ("query_tests", {
            "qualified_name": "x", "reverse": False, "history": True, "limit": 50,
        }),
    }

    def _http_client(self):
        client = MagicMock()
        client.is_http_client = True
        client.query_issues.return_value = []  # query.issues 便捷方法返回
        client.query_tests.return_value = []  # query.tests 便捷方法返回
        return client

    def test_http_mode_passthrough_query_rpc(self):
        tools = _register_tools(tools_task)
        client = self._http_client()
        with patch(f"{tools_task.__name__}._get_daemon_client", return_value=client):
            for name, (method, params) in self.EXPECTED_CALLS.items():
                args, kwargs = _make_call_args(tools[name])
                tools[name](*args, **kwargs)
                # M2.5：HTTP 分支经便捷方法调用（不再走通用 client.call），
                # 便捷方法内部注入 workspace_instance_id 并转发 query.* RPC。
                assert getattr(client, method).called, \
                    f"{name} 应调用便捷方法 {method}()"
                call_args, call_kwargs = getattr(client, method).call_args
                sent = dict(call_kwargs)
                if call_args:
                    sent["qualified_name"] = call_args[0]
                sent.pop("db_path", None)  # mock 下可能为 None，不纳入断言
                assert sent == params, f"{name} 关键参数 {sent} != {params}"

    def test_http_mode_coverage_summary_aggregation(self):
        """get_test_coverage_summary：query_tests 便捷方法返回后本地聚合（与
        DaemonClient.get_test_coverage_summary 语义一致，M2.5 fail-closed）。"""
        tools = _register_tools(tools_task)
        client = MagicMock()
        client.is_http_client = True
        client.query_tests.return_value = [
            {"confidence": "high"}, {"confidence": "high"}, {"confidence": "mid"},
        ]
        with patch(f"{tools_task.__name__}._get_daemon_client", return_value=client):
            result = tools["get_test_coverage_summary"]("x")
        # HTTP 分支经便捷方法（非通用 call）拉取 query.tests 数据
        assert client.query_tests.called, "HTTP 模式应调用 query_tests 便捷方法"
        call_args, call_kwargs = client.query_tests.call_args
        assert call_args[0] == "x"
        assert call_kwargs.get("reverse") is False
        assert call_kwargs.get("history") is False
        assert call_kwargs.get("limit") == 50
        assert result == {
            "has_tests": True,
            "test_count": 3,
            "high_confidence_count": 2,
            "tests": [{"confidence": "high"}, {"confidence": "high"},
                      {"confidence": "mid"}],
        }

    def test_legacy_mode_keeps_convenience_methods(self):
        """legacy 模式（client 无 is_http_client）保持便捷方法调用，语义不变。

        注意：MagicMock 的 __getattr__ 对任意属性返回 truthy child mock，
        必须显式设置 is_http_client = False 才能模拟真实 DaemonClient
        （getattr(client, "is_http_client", False) 返回 False）。
        """
        tools = _register_tools(tools_task)
        client = MagicMock()
        client.is_http_client = False  # 显式模拟 legacy DaemonClient
        client.get_symbol_issues.return_value = []
        client.get_test_cases.return_value = []
        client.get_tested_functions.return_value = []
        client.get_test_coverage_summary.return_value = {"has_tests": False}
        client.get_test_stability.return_value = {"total_runs": 0}
        with patch(f"{tools_task.__name__}._get_daemon_client", return_value=client):
            for name in list(self.EXPECTED_CALLS) + ["get_test_coverage_summary"]:
                args, kwargs = _make_call_args(tools[name])
                tools[name](*args, **kwargs)
            assert not client.call.called, "legacy 模式不应走通用 call"
            for m in ("get_symbol_issues", "get_test_cases",
                      "get_tested_functions", "get_test_coverage_summary",
                      "get_test_stability"):
                assert getattr(client, m).called, f"legacy 模式应调用 {m}()"


# ============================================================
# 3b. task 3 个工具：H4C-3 接入 worker 路由（不泄漏 method_not_found）
# ============================================================

class TestTaskPseudoRouteFailClosed:
    """tools_task 2 个工具（get_symbol_change_tasks / task_plan_template）已按
    H4C-3（T-1786716190783-ba187c88）接入 worker；get_commit_tasks 在 W4-1
    （T-1786886251769-22b94ee8-sub-1）迁移 rust_native（HTTP 直连
    _get_daemon_client().get_commit_tasks → query.commit_tasks 真实 RPC），
    不再属于本类伪路由 worker 工具（见 test_get_commit_tasks_http_native_rpc /
    test_get_commit_tasks_legacy_local_exec）。

    复判背景：H4B-M 曾发现这 3 个工具 HTTP 分支经 route_task_read 直传不存在的
    RPC 名（task.get_change_tasks / task.get_commit_tasks / task.plan_template
    在 dispatch.rs 无分支），而 route_task_read 对 DaemonRemoteError 原样 raise
    （不降级）→ HTTP/enterprise 模式必抛 method_not_found，非结构化错误，违反
    fail-closed 契约。H4C-3 整改：改走 route_worker_call（compat registry
    read_only 注册，见 tools_task.py 模块级 register_compat_routes），
    HTTP/enterprise 模式经 worker 执行（返回 worker 结果原样透传，不触碰
    get_db）；local 模式执行 _local（get_db() 本地 SQL），公开语义不变。
    本类核验：HTTP 模式不再泄漏 method_not_found（不调用 route_task_read）、
    legacy 模式保持本地执行、矩阵 daemon_rpc_method=none 归类一致。
    """

    PSEUDO_RPC_TOOLS = {
        "get_symbol_change_tasks": "task.get_change_tasks",
        "task_plan_template": "task.plan_template",
    }

    def test_http_mode_worker_route_passthrough(self, mock_http_worker_route):
        """HTTP 模式：2 工具（get_symbol_change_tasks / task_plan_template）经
        route_worker_call 调 worker（mock rpc client），返回值 = client.call 结果
        原样透传，client.call 首参为工具名，get_db 从未被调用（无本地 SQLite
        fallback）。W4-1 后 get_commit_tasks 已迁移 rust_native（不再经
        route_worker_call），其 HTTP 行为见 test_get_commit_tasks_http_native_rpc。
        """
        tools = _register_tools(tools_task)
        for mode in ("auto", "enterprise"):
            client = mock_http_worker_route(mode)
            for name, fn in tools.items():
                if name not in self.PSEUDO_RPC_TOOLS:
                    continue
                expected = {"ok": True, "sentinel": name}
                client.call.return_value = expected
                client.call.reset_mock()
                args, kwargs = _make_call_args(fn)
                with patch(f"{tools_task.__name__}.get_db") as mock_get_db:
                    result = fn(*args, **kwargs)
                    # 返回值 = worker 结果原样透传（未做 unsupported 包装）
                    assert result == expected, (
                        f"{name} ({mode}) 应原样透传 worker 结果，实际 {result}"
                    )
                    # client.call 首参为 compat worker 路由方法名（= 工具名）
                    assert client.call.call_args[0][0] == name, (
                        f"{name} ({mode}) 应路由到 compat worker 方法名 {name}"
                    )
                    # HTTP 模式绝不触碰本地 SQLite（_local 不执行）
                    mock_get_db.assert_not_called()

    def test_http_mode_no_method_not_found_leak(self, mock_http_worker_route):
        """HTTP 模式：2 工具不再经 route_task_read 直传伪路由 RPC 名（route_task_read
        不被调用），经 worker 路由执行 → 不会泄漏 method_not_found。
        W4-1 后 get_commit_tasks 走 query.commit_tasks 真实 RPC（见
        test_get_commit_tasks_http_native_rpc），亦不泄漏 method_not_found。"""
        tools = _register_tools(tools_task)
        client = mock_http_worker_route("auto")
        with (
            patch(f"{tools_task.__name__}.route_task_read") as mock_route,
            patch(f"{tools_task.__name__}.get_db") as mock_get_db,
        ):
            for name, fn in tools.items():
                if name not in self.PSEUDO_RPC_TOOLS:
                    continue
                expected = {"ok": True, "no": "leak"}
                client.call.return_value = expected
                client.call.reset_mock()
                args, kwargs = _make_call_args(fn)
                result = fn(*args, **kwargs)
                # 结构化透传 worker 结果，而非 DaemonRemoteError(method_not_found)
                assert result == expected, name
            mock_route.assert_not_called()
            mock_get_db.assert_not_called()

    def test_legacy_mode_keeps_local_exec(self, mock_legacy_mode):
        """legacy 模式（HTTP transport 关闭 + local）：2 工具经 route_worker_call
        local 分支执行 _local（get_db() 本地 SQL），公开语义不变。
        W4-1 后 get_commit_tasks 的 legacy 路径同样保持本地执行（见
        test_get_commit_tasks_legacy_local_exec）。"""
        tools = _register_tools(tools_task)
        with patch(f"{tools_task.__name__}.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.get_symbol_change_tasks.return_value = ["sym-tasks"]
            mock_db.task_plan_template.return_value = "TEMPLATE"
            mock_get_db.return_value = mock_db
            for name, fn in tools.items():
                if name not in self.PSEUDO_RPC_TOOLS:
                    continue
                args, kwargs = _make_call_args(fn)
                result = fn(*args, **kwargs)
                assert result is not None, f"{name} legacy 模式应返回本地结果"
            assert mock_get_db.called

    def test_matrix_daemon_rpc_method_none(self):
        """capability-matrix.json：2 工具 daemon_rpc_method=none、backend=python_compat
        （与 fail-closed 归类一致，不再是虚构 RPC 名）。"""
        matrix = json.load(open(
            os.path.join(".trae-cn", "evidence", "http-daemon-capability-matrix.json"),
            encoding="utf-8",
        ))
        rows = matrix.get("tools", matrix)
        by_name = {r.get("tool_name"): r for r in rows}
        for name in self.PSEUDO_RPC_TOOLS:
            row = by_name.get(name)
            assert row is not None, f"{name} 应在矩阵中存在"
            assert row.get("daemon_rpc_method") == "none", name
            assert row.get("backend") == "python_compat", name

    def test_get_commit_tasks_http_native_rpc(self, monkeypatch):
        """W4-1（T-1786886251769-22b94ee8-sub-1）：get_commit_tasks 已迁移
        rust_native，HTTP 模式直连 _get_daemon_client().get_commit_tasks(...)
        （Rust native query.commit_tasks，注入权威 workspace_instance_id），
        返回值原样透传；get_db 未被调用（无本地 SQLite fallback），不再经
        route_worker_call / route_task_read（不泄漏 method_not_found）。"""
        tools = _register_tools(tools_task)
        client = MagicMock()
        client.get_commit_tasks.return_value = [{"task_id": "T-1"}]
        monkeypatch.setattr(tools_task, "is_http_transport_enabled", lambda: True)
        monkeypatch.setattr(tools_task, "_get_daemon_client", lambda: client)
        monkeypatch.setattr(tools_task, "_get_db_path_for_daemon", lambda: "/tmp/w4_1.db")
        fn = tools["get_commit_tasks"]
        with patch(f"{tools_task.__name__}.get_db") as mock_get_db:
            result = fn(commit_hash="abc123")
        client.get_commit_tasks.assert_called_once_with(
            commit_hash="abc123",
            include_task_details=True,
            db_path="/tmp/w4_1.db",
        )
        assert result == [{"task_id": "T-1"}]
        mock_get_db.assert_not_called()

    def test_get_commit_tasks_legacy_local_exec(self, mock_legacy_mode, monkeypatch):
        """W4-1（T-1786886251769-22b94ee8-sub-1）：get_commit_tasks legacy 模式
        （HTTP transport 关闭 + local）仍经 route_worker_call local 分支执行
        _local（get_db().get_commit_tasks()），公开语义不变。"""
        monkeypatch.setattr(tools_task, "is_http_transport_enabled", lambda: False)
        tools = _register_tools(tools_task)
        with patch(f"{tools_task.__name__}.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.get_commit_tasks.return_value = ["commit-tasks"]
            mock_get_db.return_value = mock_db
            result = tools["get_commit_tasks"](commit_hash="abc123")
            assert result == ["commit-tasks"]
            mock_get_db.assert_called()


# ============================================================
# 4. fail-closed 静态验证：无伪路由
# ============================================================

class TestNoPseudoRoutes:
    """fail-closed：相关模块不得存在指向不存在 RPC 的伪路由。"""

    def test_p2_p3_no_daemon_rpc_pseudo_route(self):
        """p2/p3 工具源码不得含 _call_daemon_rpc / _get_daemon_client 伪路由。

        p2/p3 全部工具（只读经 route_worker_call + _local，写语义经
        _http_unsupported）均无 daemon RPC 伪路由；p4 lease.* 是真实 RPC
        保留分支（_call_daemon_rpc 真名透传），不在此断言范围内。
        """
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

    def test_every_fail_closed_tool_starts_with_http_unsupported(self):
        """写语义工具以 _http_unsupported("<name>") 开头 fail-closed。

        任务 4 起 collab 写 2（submit_verdict/append_evidence）改经 daemon RPC
        薄壳转发（_collab_write_rpc），不再 _http_unsupported，故排除在断言
        之外；只读工具已接入 route_worker_call（函数体为 route_worker_call
        路由），不再断言。
        """
        registered = {}
        for name, module in WRITE_FAIL_CLOSED_TOOLS.items():
            if name in ("submit_verdict", "append_evidence"):
                continue  # 任务 4：daemon RPC 薄壳，见 TestHttpGovernanceUnsupported
            if module not in registered:
                registered[module] = _register_tools(module)
            fn = registered[module][name]
            source = inspect.getsource(fn)
            assert f'_http_unsupported("{name}")' in source, (
                f"{name} 应以 _http_unsupported(\"{name}\") 开头 fail-closed"
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

    def test_no_sqlite_fallback_in_modules(self):
        """模块级不得直接构造 CodeGraphDB（无 SQLite fallback）。

        15 个只读工具模块（collab/p2/p3/p4）均含模块级 _http_unsupported helper
        定义（写语义 fail-closed 入口），且不直接构造 CodeGraphDB。
        """
        for module in READONLY_WORKER_MODULES:
            source = inspect.getsource(module)
            assert "CodeGraphDB(" not in source, (
                f"{module.__name__} 不得直接构造 CodeGraphDB（无 SQLite fallback）"
            )
            assert "def _http_unsupported" in source

    def test_collab_read_tools_go_through_helper(self):
        """collab 4 只读工具经 _collab_rpc_call helper，helper 内前置 fail-closed。

        _collab_rpc_call 是 register() 内部闭包，无法直接模块级引用；此处
        从模块源码断言 helper 内的 HTTP 前置拦截与只读工具对 helper 的调用。
        """
        module_source = inspect.getsource(tools_collab)
        # helper 必须先拦截 HTTP 模式（防 AttributeError→direct_read 违规回退）
        assert "_http_unsupported(tool_name)" in module_source, (
            "_collab_rpc_call 必须先拦截 HTTP 模式（防 AttributeError→direct_read）"
        )
        tools = _register_tools(tools_collab)
        for name in ("get_role_view", "find_evidence",
                     "get_freshness_status", "get_gate_decision"):
            fn_source = inspect.getsource(tools[name])
            assert "_collab_rpc_call" in fn_source, f"{name} 应经 _collab_rpc_call"
            # 工具自身不直接构造 CodeGraphDB（helper 内 fail-closed 已拦截）
            assert "get_db" not in fn_source or "_collab_rpc_call" in fn_source, (
                f"{name} 不应绕过 helper 直连 SQLite"
            )


# ============================================================
# 5. legacy 模式保持本地执行（公开方法语义不变）
# ============================================================

class TestLegacyModeKeepsLocalExec:
    """非 HTTP 模式保持本地执行（公开方法语义不变）。

    H4C-2/3 整改后：只读工具 legacy 模式经 route_worker_call local 分支执行
    _local（_local 内调用 get_db() / _collab_rpc_call）；p2/p3/p4 写工具
    _http_unsupported 非 HTTP 返回 None 后本地 get_db() 执行。两种路径 legacy
    下都保持本地 SQLite 语义，无公开方法语义漂移。

    任务 4：collab 写 2（submit_verdict/append_evidence）legacy 模式同样经
    _collab_write_rpc → DaemonClient.call_with_autostart 走 daemon 权威（不
    再本地 get_db()，业务逻辑全在 Rust daemon）。
    """

    @pytest.mark.parametrize(
        "module",
        [tools_p2_graph, tools_p3_identity],
        ids=lambda m: m.__name__.split(".")[-1],
    )
    def test_p2_p3_legacy_calls_get_db(self, module, mock_legacy_mode):
        """只读工具 legacy 经 route_worker_call local 模式执行 _local
        （_local 内调用 get_db()）；写工具 _http_unsupported 返回 None 后本地
        get_db()。均保持本地 SQLite 执行。"""
        tools = _register_tools(module)
        with patch(f"{module.__name__}.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_get_db.return_value = mock_db
            for name, fn in tools.items():
                args, kwargs = _make_call_args(fn)
                fn(*args, **kwargs)
            assert mock_get_db.called, "legacy 模式应保持本地 get_db() 执行"

    def test_p4_assignment_legacy_calls_get_db(self, mock_legacy_mode):
        """legacy 模式 assignment_show 经 route_worker_call local 分支执行
        _local（get_db().get_assignment）；assignment_create/revoke 经
        _http_unsupported（非 HTTP 返回 None）后本地 get_db() 执行。"""
        tools = _register_tools(tools_p4_lease)
        assignment_tools = {
            n: fn for n, fn in tools.items() if n.startswith("assignment_")
        }
        assert set(assignment_tools) == {
            "assignment_create", "assignment_show", "assignment_revoke",
        }
        with patch(f"{tools_p4_lease.__name__}.get_db") as mock_get_db:
            mock_db = MagicMock()
            mock_db.create_assignment.return_value = (True, {"assignment_id": "ASG-x"})
            mock_db.get_assignment.return_value = None
            mock_db.revoke_assignment.return_value = (True, {"assignment_id": "ASG-x"})
            mock_get_db.return_value = mock_db
            for name, fn in assignment_tools.items():
                args, kwargs = _make_call_args(fn)
                result = fn(*args, **kwargs)
                assert isinstance(result, dict), f"{name} legacy 应返回 dict"
            assert mock_get_db.called

    def test_collab_legacy_keeps_s5_direct_read(self, mock_legacy_mode):
        """legacy 模式 collab 只读经 route_worker_call local 分支执行 _local
        （_collab_rpc_call：daemon 尝试失败 → _collab_direct_read S5 显式降级，
        直查 SQLite 真实表，属 P1 计划内显式降级）。"""
        tools = _register_tools(tools_collab)
        client = MagicMock()
        client.call_with_autostart.side_effect = AttributeError("no autostart")
        with (
            patch(f"{tools_collab.__name__}._get_daemon_client", return_value=client),
            patch(f"{tools_collab.__name__}.get_db") as mock_get_db,
            patch(f"{tools_collab.__name__}._collab_direct_read") as mock_direct,
        ):
            mock_direct.return_value = {"status": "planned", "stage": "P1"}
            for name in ("get_role_view", "find_evidence",
                         "get_freshness_status", "get_gate_decision"):
                args, kwargs = _make_call_args(tools[name])
                result = tools[name](*args, **kwargs)
                assert result == {"status": "planned", "stage": "P1"}, name
            assert mock_direct.called, "legacy 模式应保持 S5 direct_read 显式降级"

    def test_collab_write_tools_route_to_daemon_in_legacy_mode(self, mock_legacy_mode):
        """任务 4：collab 写 2 legacy 模式经 DaemonClient.call_with_autostart 走
        daemon 权威（薄壳），不再本地 get_db() 直写（旧路径拒绝）。"""
        tools = _register_tools(tools_collab)
        client = MagicMock()
        client.call_with_autostart.return_value = {
            "result": {"success": True, "verdict_id": "V-1"}, "degraded": False,
        }
        call_kwargs = {
            "submit_verdict": {
                "task_id": "T-1", "step_id": "S-1",
                "contract_id": "C-1", "contract_revision": 1,
                "contract_hash": "h",
                "role_contract_id": "RC-1", "role_contract_revision": 1,
                "role_contract_hash": "rh",
                "phase": "PRE_VERDICT", "overall": "approved",
                "lease_token": "tok", "fencing_counter": 1,
            },
            "append_evidence": {
                "task_id": "T-1", "step_id": "S-1", "evidence_id": "E-1",
                "evidence_type": "test_run",
                "manifest_path": "docs/evidence/e1.json",
                "lease_token": "tok", "fencing_counter": 1,
            },
        }
        with (
            patch(f"{tools_collab.__name__}._get_daemon_client", return_value=client),
            patch(f"{tools_collab.__name__}.get_db") as mock_get_db,
            patch(f"{tools_collab.__name__}._collab_direct_read") as mock_direct,
        ):
            for name, kwargs in call_kwargs.items():
                result = tools[name](**kwargs)
                assert result["success"] is True, name
            assert client.call_with_autostart.call_count == 2, (
                "collab 写工具 legacy 模式应经 daemon RPC 薄壳转发"
            )
            mock_get_db.assert_not_called()
            mock_direct.assert_not_called()


# ============================================================
# 6. 真实进程级 RPC 对齐门（参照 H4B-C/I TestRealDaemon*RpcAlignment）
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


def test_http_mode_route_rpc_injects_workspace_id_for_task_methods(monkeypatch):
    """HTTP 模式 route_rpc 对 task.*/lease.* 同时注入数值 workspace_id。

    回归（QA Round 2）：route_rpc 的 HTTP 分支此前只注入 workspace_instance_id
    （字符串），而 Rust handle_task_create/handle_task_list 的
    required_workspace_id_param 只认数值 workspace_id（>0），导致 HTTP 模式下
    task.create / task.list / lease.* 恒返回 E_TASK_WORKSPACE_UNBOUND。
    修复：HTTP 分支对 task.*/lease.* 复用 _inject_workspace_id 注入数值 id。
    """
    import callwarden.server.daemon_client as dc

    fake_db = MagicMock()
    fake_db._get_active_workspace_id.return_value = 7
    captured = {}

    class _FakeHttpClient:
        @staticmethod
        def get_instance():
            return _singleton

        def _ensure_remote_snapshot(self, db_path):
            return "ws-instance-abc"

        def call(self, method, params=None, request_id=None):
            captured["method"] = method
            captured["params"] = params
            return {"ok": True}

    _singleton = _FakeHttpClient()

    monkeypatch.setattr(dc, "is_http_transport_enabled", lambda: True)
    monkeypatch.setattr(dc, "get_daemon_mode", lambda: "auto")
    monkeypatch.setattr(dc, "HttpDaemonRpcClient", _FakeHttpClient)
    monkeypatch.setattr("callwarden.server._mcp_common.get_db", lambda: fake_db)

    # task.create：HTTP 模式必须同时注入 workspace_instance_id 与数值 workspace_id
    dc.route_rpc("task.create", {"title": "t"}, op_class="GOVERNANCE_WRITE")
    assert captured["method"] == "task.create"
    p = captured["params"]
    assert p["workspace_instance_id"] == "ws-instance-abc"
    assert p["workspace_id"] == 7

    # lease.acquire：同样注入数值 workspace_id
    dc.route_rpc("lease.acquire", {"task_id": "T-1"}, op_class="PROTECTED_MUTATION")
    assert captured["params"]["workspace_instance_id"] == "ws-instance-abc"
    assert captured["params"]["workspace_id"] == 7

    # 非任务读方法（query.issues）：只注入 workspace_instance_id，不注入数值 id
    dc.route_rpc("query.issues", {"issue_id": "i1"}, op_class="READ_ONLY")
    p = captured["params"]
    assert p["workspace_instance_id"] == "ws-instance-abc"
    assert "workspace_id" not in p

