# -*- coding: utf-8 -*-
"""MCP-068: list_clone_groups → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~067）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 list_clone_groups
3. 语义对齐 Python db_clone_groups.list_clone_groups：
   - 查询 clone_groups（按 active workspace 过滤）
   - 支持 clone_type/min_similarity/limit 过滤
   - 每项含 id/workspace_id/group_hash/clone_type/token_hash/similarity/
     representative_symbol_id/member_count/created_at
   - 按相似度降序、member_count 降序
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
    return rpc.call("list_clone_groups", params or {})


def test_list_clone_groups_default_shape(rpc):
    """默认调用：返回 list，每项含全部 9 个字段且类型正确。"""
    out = _call(rpc)
    assert isinstance(out, list), f"期望 list，实际 {type(out)}"
    for item in out:
        assert isinstance(item, dict), f"每项应为 dict，实际 {type(item)}"
        for key in (
            "id",
            "workspace_id",
            "group_hash",
            "clone_type",
            "token_hash",
            "similarity",
            "representative_symbol_id",
            "member_count",
            "created_at",
        ):
            assert key in item, f"缺字段 {key}"
        assert isinstance(item["id"], int)
        assert isinstance(item["workspace_id"], int)
        assert isinstance(item["group_hash"], str)
        assert isinstance(item["clone_type"], int)
        assert isinstance(item["token_hash"], str)
        assert isinstance(item["similarity"], (int, float))
        assert isinstance(item["representative_symbol_id"], int)
        assert isinstance(item["member_count"], int)
        assert isinstance(item["created_at"], (int, float))


def test_list_clone_groups_clone_type_filter(rpc):
    """clone_type=2（Type-2）：仅返回 clone_type==2 的项。"""
    out = _call(rpc, {"clone_type": 2})
    assert isinstance(out, list)
    for item in out:
        assert item["clone_type"] == 2, f"clone_type 应为 2，实际 {item['clone_type']}"


def test_list_clone_groups_min_similarity_filter(rpc):
    """min_similarity=0.8：仅返回 similarity>=0.8 的项。"""
    out = _call(rpc, {"min_similarity": 0.8})
    assert isinstance(out, list)
    for item in out:
        assert item["similarity"] >= 0.8, f"similarity 应 >=0.8，实际 {item['similarity']}"


def test_list_clone_groups_limit(rpc):
    """limit 限制返回条数（上限 100，传 2 应最多返回 2 条）。"""
    out = _call(rpc, {"limit": 2})
    assert isinstance(out, list)
    assert len(out) <= 2, f"limit=2 应最多返回 2 条，实际 {len(out)}"


def test_list_clone_groups_invalid_clone_type(rpc):
    """非法 clone_type（如 99）：不过滤（与 Python 一致，视为 0=全部）。"""
    out = _call(rpc, {"clone_type": 99})
    assert isinstance(out, list)


def test_list_clone_groups_repeatable(rpc):
    """重复调用结果稳定（无连接态副作用）。"""
    a = _call(rpc)
    b = _call(rpc)
    assert a == b


def test_list_clone_groups_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("list_clone_groups", {})
