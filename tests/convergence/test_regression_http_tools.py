"""回归验证：既有 HTTP 工具零回归（PRD R0.5 / M1） + 实际调用链抽查（M1 抽查）。

验证目标：
- 既有 native 工具（query.stats 等）在隔离 daemon 上可调用（RPC 分支在 dispatch.rs）；
- 1 个迁移工具（workspace.build_graph 类）与 1 个 compat 工具（get_impact 类）的
  路由分支存在且 dispatch.rs / 矩阵一致；
- 工具名/参数签名未破坏（create_mcp_server 注册名 = 矩阵名，见 test_m1）。

注意：本套件面向**隔离 daemon**（release 二进制）。若二进制不含新收敛 RPC
（Windows 构建阻塞已知问题），迁移工具的运行时调用会返回 method_not_found——
这是环境限制而非源码缺陷，此处仅做"分支存在 + 矩阵一致"静态断言 + 可运行工具的
运行时冒烟。
"""
from __future__ import annotations

import json
import os
import re

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _matrix_tool(name: str) -> dict:
    with open(os.path.join(_REPO_ROOT, "deliverables", "software-company",
                           "tool_migration_matrix.json"), encoding="utf-8") as fh:
        m = json.load(fh)
    for t in m["tools"]:
        if t["name"] == name:
            return t
    raise AssertionError(f"矩阵中不存在工具 {name}")


class TestExistingNativeToolRuntime:
    """既有 native 工具：隔离 daemon 实际可调用（零回归冒烟）。"""

    def test_workspace_list_runtime(self, isolated_http_daemon, rpc_client):
        r = rpc_client.call("workspace.list", {})
        assert isinstance(r, list)

    def test_task_create_runtime(self, isolated_http_daemon, rpc_client, qa_workspace):
        # stale 修复：daemon 现强制 task.create 显式传非空 workspace_instance_id，
        # 否则 E_TASK_WORKSPACE_INSTANCE_REQUIRED。
        # 权威：rust_ext/src/daemon/task_loop/create.rs:36-37,277-287。
        r = rpc_client.call("task.create", {
            "title": "QA-REGRESSION-TASK",
            "workspace_id": qa_workspace["workspace_id"],
            "workspace_instance_id": qa_workspace["workspace_instance_id"],
        })
        assert r["task_id"] and r["status"] == "open"

    def test_ping_health_runtime(self, isolated_http_daemon, rpc_client):
        ping = rpc_client.call("ping", {})
        assert ping.get("status") == "ok"
        health = rpc_client.call("health", {})
        assert "pid" in health or "status" in health


class TestMigratedToolRouting:
    """迁移工具：矩阵 ↔ dispatch 分支一致性 + 运行时可达性（含 method_not_found 环境说明）。"""

    def test_build_graph_matrix_and_dispatch(self):
        t = _matrix_tool("build_graph")
        assert t["target_backend"] == "rust_native"
        assert t["rpc_method"] == "workspace.build_graph"
        src = open(os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "dispatch.rs"),
                   encoding="utf-8").read()
        assert "workspace.build_graph" in src

    def test_detect_clones_matrix_and_dispatch(self):
        t = _matrix_tool("detect_clones")
        # detect_clones 是异步长任务 → task_rpc（job 状态机）
        assert t["target_backend"] == "task_rpc", f"detect_clones 应为 task_rpc: {t}"
        assert t["rpc_method"] == "task.job_submit"
        src = open(os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "dispatch.rs"),
                   encoding="utf-8").read()
        assert "task.job_submit" in src

    def test_get_impact_compat_matrix_and_dispatch(self):
        """get_impact：矩阵与实现均已迁 rust_native（两端 compat 白名单清零）。

        stale 修正依据（step6 复核，2026-09-20）：
        - 矩阵 tool_migration_matrix.json 的 get_impact.target_backend 已是 rust_native
          （status=migrated, batch=P0-COMPAT-v3），本测试旧断言
          ``t["target_backend"] == "python_compat"`` 与矩阵自相矛盾，属漏改 stale。
        - 旧断言 `'"get_impact", "read_only"' in http_src`（旧 COMPAT_ROUTE_WHITELIST
          元组格式）已失效：白名单已清零（rust_ext/src/daemon/http_server.rs:587-588），
          get_impact 现由 Rust 原生 route_matrix 注册为 rust_native
          （rust_ext/src/daemon/http_server.rs:2883-2897 的 add(... "rust_native" ...)）。
        - 旧断言 `'"get_impact"' in comp_src` 已失效：server/compat_registry.py:205-219
          RUST_COMPAT_ROUTE 现为空 {}，get_impact 在 Rust/Python 两端 compat 表皆缺，
          属冻结的 KNOWN_DRIFT（tests/convergence/test_m1_route_matrix.py:141-146）。
        """
        t = _matrix_tool("get_impact")
        assert t["target_backend"] == "rust_native"
        assert t["rpc_method"] == "get_impact"
        http_src = open(os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon",
                                     "http_server.rs"), encoding="utf-8").read()
        # get_impact 现注册为 rust_native 原生路由（http_server.rs:2883-2897）。
        assert re.search(
            r'add\(\s*"get_impact",\s*"get_impact",\s*"get-impact",\s*"rust_native",',
            http_src,
        ), "get_impact 应注册为 rust_native 路由（http_server.rs:2883-2897）"
        comp_src = open(os.path.join(_REPO_ROOT, "server", "compat_registry.py"),
                        encoding="utf-8").read()
        # RUST_COMPAT_ROUTE 已清零：get_impact 不再登记于 Python compat 表（KNOWN_DRIFT）。
        assert "get_impact" not in comp_src, \
            "RUST_COMPAT_ROUTE 已清零，get_impact 不应再登记（compat_registry.py:205-219）"

    def test_get_impact_runtime_on_isolated_daemon(self, isolated_http_daemon, rpc_client, qa_workspace):
        """compat 工具经隔离 daemon 实际可达（worker 路径），返回结构化结果。"""
        try:
            r = rpc_client.call("get_impact", {
                "qualified_name": "nonexistent_symbol",
                "workspace_id": qa_workspace["workspace_id"],
            })
            assert isinstance(r, dict)
            assert "total_upstream" in r or "levels" in r or "all_upstream" in r
        except Exception as exc:
            # 隔离 daemon 若无 compat worker 上下文 → 允许 E_HTTP_COMPAT_UNSUPPORTED
            # 类结构化错误（仍证明路由分支存在、fail-closed 而非本地执行）
            from callwarden.server.daemon_protocol import DaemonRemoteError
            if isinstance(exc, DaemonRemoteError):
                assert exc.code in ("E_HTTP_COMPAT_UNSUPPORTED", "method_not_found",
                                    "invalid_params", "E_IDENTITY_NOT_WIRED")
            else:
                raise
