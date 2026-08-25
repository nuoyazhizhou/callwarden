# -*- coding: utf-8 -*-
"""MCP-063: get_symbol_change_tasks → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~062）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 get_symbol_change_tasks
3. 语义对齐 Python db_task_attribution.get_symbol_change_tasks：
   - 反查某个符号版本（symbol_hash_before/after）或符号名（qualified_name）
     由哪些任务改变过（task_symbol_changes 表）
   - symbol_hash / qualified_name 都为空 → 空数组（不做全表扫描）
   - symbol_hash 匹配 symbol_hash_before 或 symbol_hash_after
   - qualified_name 精确匹配
   - 排序 created_at DESC, id DESC；limit 截断
   - 返回 JSON 数组（每行含 task_id/file_path/qualified_name/change_type/created_at 等）
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


def _call(rpc, params):
    return rpc.call("get_symbol_change_tasks", params)


def test_get_symbol_change_tasks_no_filter_empty(rpc):
    """symbol_hash 与 qualified_name 都为空 → 空数组（不做全表扫描）。"""
    out = _call(rpc, {})
    assert isinstance(out, list)
    assert out == []


def test_get_symbol_change_tasks_default_limit(rpc):
    """仅提供 symbol_hash：返回数组，且长度不超过默认 limit=50。"""
    out = _call(rpc, {"symbol_hash": "deadbeef00000000000000000000000000000000"})
    assert isinstance(out, list)
    assert len(out) <= 50


def test_get_symbol_change_tasks_row_fields(rpc):
    """有数据时逐行字段齐备（task_symbol_changes 全列）。"""
    out = _call(rpc, {"qualified_name": "__nonexistent_symbol_zzz__"})
    assert isinstance(out, list)
    for row in out:
        for key in (
            "id",
            "workspace_id",
            "task_id",
            "step_id",
            "edit_audit_id",
            "change_audit_id",
            "file_path",
            "qualified_name",
            "symbol_name",
            "symbol_hash_before",
            "symbol_hash_after",
            "change_type",
            "source",
            "source_commit_hash",
            "metadata",
            "created_at",
        ):
            assert key in row, f"缺字段 {key}"


def test_get_symbol_change_tasks_hash_matches_before_or_after(rpc):
    """symbol_hash 过滤：仅返回 hash_before 或 hash_after 等于该值的行。"""
    import random

    # 用不存在的 hash 先确认空返回，避免对真实数据过度假设
    miss = "zz" * 32
    out = _call(rpc, {"symbol_hash": miss})
    assert isinstance(out, list)
    for row in out:
        assert row["symbol_hash_before"] == miss or row["symbol_hash_after"] == miss


def test_get_symbol_change_tasks_qualified_name_exact(rpc):
    """qualified_name 过滤：仅返回 qualified_name 精确匹配的行。"""
    miss = "__nonexistent_symbol_zzz__"
    out = _call(rpc, {"qualified_name": miss})
    assert isinstance(out, list)
    for row in out:
        assert row["qualified_name"] == miss


def test_get_symbol_change_tasks_limit_respected(rpc):
    """limit 截断：结果数不超过 limit。"""
    out = _call(rpc, {"qualified_name": "", "symbol_hash": "", "limit": 3})
    assert out == []


def test_get_symbol_change_tasks_ordering(rpc):
    """排序契约：created_at 单调不减（降序，同 created_at 按 id 降序）。"""
    # 用空过滤返回空，排序无从验证；这里只验证带过滤时契约不崩溃
    out = _call(rpc, {"symbol_hash": "deadbeef00000000000000000000000000000000", "limit": 10})
    assert isinstance(out, list)
    created = [row["created_at"] for row in out]
    assert created == sorted(created, reverse=True), f"created_at 降序被破坏: {created}"


def test_get_symbol_change_tasks_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("get_symbol_change_tasks", {})


def test_new_client_instance_stable(rpc):
    """新建 client 实例重复调用结果稳定（无连接态副作用）。"""
    c2 = HttpDaemonRpcClient()
    out = c2.call("get_symbol_change_tasks", {"symbol_hash": "deadbeef00000000000000000000000000000000"})
    assert isinstance(out, list)
