"""MCP-018（A′ task_evidence_read）get_impact → Rust daemon native。

覆盖 task 要求：
  success / 缺省参数 / 空调用链、daemon unavailable（fail-closed）、restart。

设计要点（与 task 不变量一致，W3 隔离 harness 适配 current-HEAD）：
- Python MCP wrapper（tools_query.get_impact）已从 _SYMBOL_READ_ONLY_METHODS 移除
  compat 注册，改由 Rust daemon（query_compat_handlers.rs::handle_get_impact）为权威：
  BFS 向上追踪调用链（call_versions is_current=1），返回 {start, max_depth_reached,
  total_upstream, levels, all_upstream}（与 Python get_call_chain_up 一致）。
- current-HEAD 路由层（snapshot_state.rs S2）要求 RPC 参数携带 `workspace_instance_id`
  （字符串），而非旧整数 `workspace_id`；本测试经 conftest 共享 `w3_live` 隔离 daemon
  权威栈（模式A USERPROFILE 重定向 + workspace.register + task-DB seed + 空 codegraph
  snapshot.publish）自建后直连，验证返回结构。
- 空库对未知 symbol 返回 total_upstream=0 / all_upstream=[]（合法空结构，非绕过
  fail-closed）。

daemon 二进制缺失/不可起时 skip（无人值守环境不依赖后台常驻 daemon）。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.config import get_http_authority_id


# ---------------------------------------------------------------------------
# success：HTTP round-trip，Rust daemon 为权威（空 codegraph → 合法空结构）
# ---------------------------------------------------------------------------
def test_get_impact_structure(w3_live):
    """返回结构完整：start/max_depth_reached/total_upstream/levels/all_upstream。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_impact",
               {"workspace_instance_id": inst, "qualified_name": "NO_SUCH_SYM_XYZ"})
    assert isinstance(r, dict)
    assert r.get("start") == "NO_SUCH_SYM_XYZ"
    assert isinstance(r.get("max_depth_reached"), int)
    assert isinstance(r.get("total_upstream"), int)
    assert isinstance(r.get("levels"), list)
    assert isinstance(r.get("all_upstream"), list)
    assert r.get("total_upstream") == 0


def test_get_impact_default_depth(w3_live):
    """缺 max_depth → 默认 10，结构完整。"""
    c, inst = w3_live["client"], w3_live["inst"]
    r = c.call("get_impact", {"workspace_instance_id": inst, "qualified_name": "X"})
    assert isinstance(r, dict)
    assert "levels" in r and "all_upstream" in r


def test_get_impact_unknown_workspace(w3_live):
    """未知 workspace_instance_id → 权威拒绝（fail-closed，不回退 SQLite）。"""
    c = w3_live["client"]
    with pytest.raises(DaemonRemoteError):
        c.call("get_impact",
               {"workspace_instance_id": "NO_SUCH_INSTANCE", "qualified_name": "X"})


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_impact_daemon_unavailable_fail_closed():
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_impact", {"workspace_instance_id": "x", "qualified_name": "X"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_impact_new_client_instance_stable(w3_live):
    """重启新 client 仍直连同一隔离 daemon，结构稳定。"""
    inst, endpoint = w3_live["inst"], w3_live["endpoint"]
    c2 = HttpDaemonRpcClient(endpoint=endpoint, authority_id=get_http_authority_id())
    r = c2.call("get_impact",
                {"workspace_instance_id": inst, "qualified_name": "X"})
    assert isinstance(r, dict)
    assert "start" in r and "all_upstream" in r