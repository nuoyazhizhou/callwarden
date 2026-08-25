# -*- coding: utf-8 -*-
"""MCP-067: list_clones → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~066）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 list_clones
3. 语义对齐 Python db_clone_detection.list_clones：
   - 查询 clone_pairs（按 active workspace 过滤），JOIN symbols/file_instances
   - 支持 clone_type/min_similarity/symbol_id/limit 过滤
   - 每项含 clone_type/similarity/token_hash/lines_a/lines_b/detected_at/
     symbol_a_name/symbol_a_qualified/symbol_a_line/
     symbol_b_name/symbol_b_qualified/symbol_b_line/file_a/file_b
   - 按相似度降序、detected_at 降序
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
    return rpc.call("list_clones", params or {})


def test_list_clones_default_shape(rpc):
    """默认调用：返回 list，每项含全部 14 个字段且类型正确。"""
    out = _call(rpc)
    assert isinstance(out, list), f"期望 list，实际 {type(out)}"
    for item in out:
        assert isinstance(item, dict), f"每项应为 dict，实际 {type(item)}"
        for key in (
            "clone_type",
            "similarity",
            "token_hash",
            "lines_a",
            "lines_b",
            "detected_at",
            "symbol_a_name",
            "symbol_a_qualified",
            "symbol_a_line",
            "symbol_b_name",
            "symbol_b_qualified",
            "symbol_b_line",
            "file_a",
            "file_b",
        ):
            assert key in item, f"缺字段 {key}"
        assert isinstance(item["clone_type"], int)
        assert isinstance(item["similarity"], (int, float))
        assert isinstance(item["token_hash"], str)
        assert isinstance(item["lines_a"], int)
        assert isinstance(item["lines_b"], int)
        assert isinstance(item["detected_at"], (int, float))
        assert isinstance(item["symbol_a_name"], str)
        assert isinstance(item["symbol_a_qualified"], str)
        assert isinstance(item["symbol_a_line"], int)
        assert isinstance(item["symbol_b_name"], str)
        assert isinstance(item["symbol_b_qualified"], str)
        assert isinstance(item["symbol_b_line"], int)
        assert isinstance(item["file_a"], str)
        assert isinstance(item["file_b"], str)


def test_list_clones_clone_type_filter(rpc):
    """clone_type=1（Type-1）：仅返回 clone_type==1 的项。"""
    out = _call(rpc, {"clone_type": 1})
    assert isinstance(out, list)
    for item in out:
        assert item["clone_type"] == 1, f"clone_type 应为 1，实际 {item['clone_type']}"


def test_list_clones_min_similarity_filter(rpc):
    """min_similarity=0.9：仅返回 similarity>=0.9 的项。"""
    out = _call(rpc, {"min_similarity": 0.9})
    assert isinstance(out, list)
    for item in out:
        assert item["similarity"] >= 0.9, f"similarity 应 >=0.9，实际 {item['similarity']}"


def test_list_clones_limit(rpc):
    """limit 限制返回条数（上限 100，传 2 应最多返回 2 条）。"""
    out = _call(rpc, {"limit": 2})
    assert isinstance(out, list)
    assert len(out) <= 2, f"limit=2 应最多返回 2 条，实际 {len(out)}"


def test_list_clones_unknown_symbol_id(rpc):
    """symbol_id 指向不存在的符号：返回空 list（不报错）。"""
    out = _call(rpc, {"symbol_id": 999999999})
    assert isinstance(out, list)
    assert len(out) == 0


def test_list_clones_repeatable(rpc):
    """重复调用结果稳定（无连接态副作用）。"""
    a = _call(rpc)
    b = _call(rpc)
    assert a == b


def test_list_clones_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("list_clones", {})
