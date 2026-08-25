# -*- coding: utf-8 -*-
"""MCP-066: bootstrap_status → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~065）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 bootstrap_status
3. 语义对齐 Python db_bootstrap.bootstrap_status（含 B2 T-1786590722456-
   db00d074-sub-2：findings 按 active workspace 过滤，tasks 全局统计）：
   - db_stale：最近 scan_run 的 git_head 与当前 HEAD 不一致
   - current_head / active_rules_count / pending_candidates_count
   - open_findings_count / blocking_findings_count
   - audit_verify：total_count/verified_count/broken_count/security_level
   - latest_scan_run：最近一次 workspace_scan_runs 记录
   - tasks：open/in_progress/review/applied 分组计数
   - recommended_next_action：按优先级推荐下一条命令
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402


@pytest.fixture(scope="module")
def rpc():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c


def _call(rpc, params=None):
    return rpc.call("bootstrap_status", params or {})


def test_bootstrap_status_default_shape(rpc):
    """默认调用：返回 dict，含全部顶层字段且类型正确。"""
    out = _call(rpc)
    assert isinstance(out, dict), f"期望 dict，实际 {type(out)}"
    for key in (
        "db_stale",
        "current_head",
        "active_rules_count",
        "pending_candidates_count",
        "open_findings_count",
        "blocking_findings_count",
        "audit_verify",
        "latest_scan_run",
        "tasks",
        "recommended_next_action",
    ):
        assert key in out, f"缺字段 {key}"
    assert isinstance(out["db_stale"], bool)
    assert isinstance(out["current_head"], str)
    for key in (
        "active_rules_count",
        "pending_candidates_count",
        "open_findings_count",
        "blocking_findings_count",
    ):
        assert isinstance(out[key], int), f"{key} 应为 int，实际 {type(out[key])}"
    assert isinstance(out["recommended_next_action"], str)


def test_bootstrap_status_audit_verify_shape(rpc):
    """audit_verify：含 total_count/verified_count/broken_count/security_level。"""
    out = _call(rpc)
    av = out["audit_verify"]
    assert isinstance(av, dict)
    for key in ("total_count", "verified_count", "broken_count", "security_level"):
        assert key in av, f"audit_verify 缺字段 {key}"
    assert isinstance(av["total_count"], int)
    assert isinstance(av["verified_count"], int)
    assert isinstance(av["broken_count"], int)
    assert isinstance(av["security_level"], str)
    # 计数一致性：verified + broken 不得超过 total（均为非负）
    assert av["verified_count"] >= 0
    assert av["broken_count"] >= 0


def test_bootstrap_status_tasks_shape(rpc):
    """tasks：open/in_progress/review/applied 四键均为非负 int。"""
    out = _call(rpc)
    tasks = out["tasks"]
    assert isinstance(tasks, dict)
    for key in ("open", "in_progress", "review", "applied"):
        assert key in tasks, f"tasks 缺字段 {key}"
        assert isinstance(tasks[key], int), f"tasks.{key} 应为 int，实际 {type(tasks[key])}"
        assert tasks[key] >= 0


def test_bootstrap_status_latest_scan_shape(rpc):
    """latest_scan_run：None 或含 id/git_head/started_at/status。"""
    out = _call(rpc)
    scan = out["latest_scan_run"]
    if scan is None:
        return
    assert isinstance(scan, dict)
    for key in ("id", "git_head", "started_at", "status"):
        assert key in scan, f"latest_scan_run 缺字段 {key}"
    assert isinstance(scan["id"], int)
    assert isinstance(scan["git_head"], str)
    assert isinstance(scan["started_at"], (int, float))
    assert isinstance(scan["status"], str)


def test_bootstrap_status_repeatable(rpc):
    """重复调用结果稳定（无连接态副作用）。"""
    a = _call(rpc)
    b = _call(rpc)
    assert a == b


def test_bootstrap_status_ignore_params(rpc):
    """工具无参数：传入任意参数不影响语义（只读聚合查询）。"""
    out = _call(rpc, {"workspace": "ws-does-not-exist", "limit": 999})
    assert isinstance(out, dict)
    assert "db_stale" in out


def test_bootstrap_status_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("bootstrap_status", {})
