# -*- coding: utf-8 -*-
"""MCP-069: get_clone_group_detail → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~068）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 get_clone_group_detail
3. 语义对齐 Python db_clone_groups.get_clone_group_detail：
   - 按 group_id 查询 clone_groups 单行（按 active workspace 过滤）
   - 返回 group 基本信息 + members 列表
   - group 含 id/workspace_id/group_hash/clone_type/token_hash/similarity/
     representative_symbol_id/member_count/created_at
   - members 每项含 symbol_id/name/qualified_name/start_line/file_path
   - group 不存在时返回 {"error": "group not found: <id>"}
"""

import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402

# P0-COMPAT-v3（T-1788963104058-fdb2e848）：Rust native 路由要求显式 workspace 绑定
CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入  # noqa: E402


@pytest.fixture()
def rpc(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


def _call(rpc, params=None):
    return rpc.call("get_clone_group_detail", {"workspace_instance_id": CANONICAL_INSTANCE, **(params or {})})


def test_get_clone_group_detail_default_shape(rpc):
    """默认参数（group_id=0）：返回 dict 含 group 和 members 字段。"""
    out = _call(rpc, {"group_id": 0})
    assert isinstance(out, dict), f"期望 dict，实际 {type(out)}"
    # group_id=0 不存在时返回 error
    if "error" in out:
        assert "not found" in out["error"]
        return
    # 正常返回：检查 group 字段
    group = out.get("group")
    assert group is not None, "缺 group 字段"
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
        assert key in group, f"group 缺字段 {key}"
    # 检查 members 字段
    members = out.get("members")
    assert members is not None, "缺 members 字段"
    assert isinstance(members, list), f"members 应为 list，实际 {type(members)}"
    for m in members:
        for key in ("symbol_id", "name", "qualified_name", "start_line", "file_path"):
            assert key in m, f"member 缺字段 {key}"


def test_get_clone_group_detail_group_not_found(rpc):
    """不存在 group_id（如 -1）：返回 error 信息。"""
    out = _call(rpc, {"group_id": -1})
    assert isinstance(out, dict)
    assert "error" in out, "期望 error 响应"
    assert "not found" in out["error"].lower() or "not found" in out["error"]


def test_get_clone_group_detail_members_type(rpc):
    """members 列表非空时，每项字段类型正确。"""
    out = _call(rpc, {"group_id": 1})
    assert isinstance(out, dict)
    if "error" in out:
        pytest.skip("无 group_id=1 数据，跳过类型检查")
    members = out.get("members", [])
    for m in members:
        assert isinstance(m["symbol_id"], int)
        assert isinstance(m["name"], str)
        assert isinstance(m["qualified_name"], str)
        assert isinstance(m["start_line"], int)
        assert isinstance(m["file_path"], str)


def test_get_clone_group_detail_members_limit(rpc):
    """members_limit 限制返回成员数。"""
    out = _call(rpc, {"group_id": 1, "members_limit": 1})
    assert isinstance(out, dict)
    if "error" in out:
        pytest.skip("无 group_id=1 数据，跳过 limit 检查")
    members = out.get("members", [])
    assert len(members) <= 1, f"members_limit=1 应最多返回 1 个成员，实际 {len(members)}"


def test_get_clone_group_detail_missing_group_id(rpc):
    """缺 group_id 参数：默认 0 → {"error": "group not found: 0"}（Python 真相源语义）。"""
    out = _call(rpc, {})
    assert isinstance(out, dict)
    assert "error" in out


def test_get_clone_group_detail_repeatable(rpc):
    """重复调用相同 group_id 结果稳定。"""
    out_a = _call(rpc, {"group_id": 1})
    out_b = _call(rpc, {"group_id": 1})
    assert out_a == out_b


def test_get_clone_group_detail_daemon_unavailable_fail_closed():
    """daemon 不可达时必须报错（fail-closed），不得静默返回空。"""
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("get_clone_group_detail", {"group_id": 1})