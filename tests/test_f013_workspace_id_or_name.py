# -*- coding: utf-8 -*-
"""F-013（2026-09-25）：workspace.activate / workspace.remove 的
id-or-name → workspace_instance_id 解析回归测试。

事故背景：MCP 工具 set_active_workspace / delete_workspace 与 CLI 只传
workspace_id_or_name，而 Rust handler 只 require_str_param
"workspace_instance_id"。route_rpc 未解析时把当前活动 workspace 的
instance 顶替上去，delete_workspace("1711") 静默归档了主仓 1193
（instance 4baea3ff12c2ea5c）而非隔离 ws 1711。

本测试锁定：
1. 数字字符串 → 按 daemon_workspaces.workspace_id 精确解析；
2. 名称 → 按 os.path.basename(client_view_root) 解析；
3. 解析不到 → fail-closed 抛 DaemonRemoteError(workspace_not_found)，
   绝不回退活动 workspace，且不向 daemon 发出任何 mutation 调用；
4. 解析后兼容键 workspace_id_or_name 从下发参数中移除；
5. CLI 现有 workspace_id_or_name 调用签名保持不变（route_rpc 内部解析）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from callwarden.server.daemon_client import (  # noqa: E402
    route_rpc,
    _resolve_ws_instance_by_id_or_name,
)
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402

MAIN_INSTANCE = "4baea3ff12c2ea5c"
ISO_INSTANCE = "ebf553b4e42d5a6c"
FAKE_REGISTRY = [
    {"workspace_id": 1193, "workspace_instance_id": MAIN_INSTANCE,
     "client_view_root": "C:/git_work/callwarden", "status": "active"},
    {"workspace_id": 1711, "workspace_instance_id": ISO_INSTANCE,
     "client_view_root": "C:/git_work/callwarden/.workbuddy/outputs/wt_iso_ws",
     "status": "active"},
]


class _FakeClient:
    """记录式 RPC client：workspace.list 返回固定注册表，其余调用记账。"""

    def __init__(self):
        self.calls = []

    def call(self, method, params):
        self.calls.append((method, params))
        if method == "workspace.list":
            return list(FAKE_REGISTRY)
        # mutation 类只回一个可透传的 dict，内容由断言侧检查 params
        return {"ok": True, "method": method}


@pytest.fixture
def fake_route(monkeypatch):
    """把 route_rpc 的 client 获取与传输探测换成桩，避免触碰真实 daemon。"""
    import callwarden.server.daemon_client as dc

    client = _FakeClient()
    monkeypatch.setattr(dc, "get_daemon_mode", lambda: "http")
    monkeypatch.setattr(dc, "is_http_transport_enabled", lambda: False)
    monkeypatch.setattr(dc, "_get_rpc_client_for_route", lambda: client)
    return client


def _forwarded_mutations(client):
    return [c for c in client.calls if c[0] != "workspace.list"]


def test_f013_numeric_id_resolves_to_target_instance(fake_route):
    """delete_workspace("1711") 必须解析到隔离 ws instance，而非主仓。"""
    route_rpc("workspace.remove", {"workspace_id_or_name": "1711"},
              "PROTECTED_MUTATION")
    muts = _forwarded_mutations(fake_route)
    assert len(muts) == 1, "应恰好下发一次 workspace.remove"
    method, params = muts[0]
    assert method == "workspace.remove"
    assert params["workspace_instance_id"] == ISO_INSTANCE
    assert "workspace_id_or_name" not in params


def test_f013_activate_numeric_id(fake_route):
    """set_active_workspace 数字 id 同样解析到目标 instance。"""
    route_rpc("workspace.activate", {"workspace_id_or_name": "1711"},
              "PROTECTED_MUTATION")
    muts = _forwarded_mutations(fake_route)
    assert len(muts) == 1
    method, params = muts[0]
    assert method == "workspace.activate"
    assert params["workspace_instance_id"] == ISO_INSTANCE


def test_f013_name_resolves_via_basename(fake_route):
    """名称（basename(client_view_root)）解析到隔离 ws。"""
    route_rpc("workspace.remove", {"workspace_id_or_name": "wt_iso_ws"},
              "PROTECTED_MUTATION")
    muts = _forwarded_mutations(fake_route)
    assert len(muts) == 1
    assert muts[0][1]["workspace_instance_id"] == ISO_INSTANCE


def test_f013_main_repo_id_still_resolves(fake_route):
    """主仓数字 id 正常解析（不误伤合法的主仓操作）。"""
    route_rpc("workspace.activate", {"workspace_id_or_name": "1193"},
              "PROTECTED_MUTATION")
    muts = _forwarded_mutations(fake_route)
    assert len(muts) == 1
    assert muts[0][1]["workspace_instance_id"] == MAIN_INSTANCE


def test_f013_unknown_id_fail_closed_no_mutation(fake_route):
    """解析失败必须 fail-closed：不下发 mutation，不回退活动 workspace。"""
    with pytest.raises(DaemonRemoteError) as ei:
        route_rpc("workspace.remove", {"workspace_id_or_name": "999999"},
                  "PROTECTED_MUTATION")
    assert ei.value.code == "workspace_not_found"
    # 关键：没有任何 mutation 被下发到 daemon（事故复发的充要条件）
    assert _forwarded_mutations(fake_route) == []
    # workspace.list 是允许的（解析查询），但不应有第二次
    list_calls = [c for c in fake_route.calls if c[0] == "workspace.list"]
    assert len(list_calls) == 1


def test_f013_unknown_name_fail_closed(fake_route):
    with pytest.raises(DaemonRemoteError) as ei:
        route_rpc("workspace.activate", {"workspace_id_or_name": "no-such-ws"},
                  "PROTECTED_MUTATION")
    assert ei.value.code == "workspace_not_found"
    assert _forwarded_mutations(fake_route) == []


def test_f013_explicit_instance_short_circuits(fake_route):
    """调用方已显式传 workspace_instance_id 时跳过解析（不覆盖权威值）。"""
    route_rpc("workspace.remove",
              {"workspace_id_or_name": "1711",
               "workspace_instance_id": "explicit-inst-0001"},
              "PROTECTED_MUTATION")
    muts = _forwarded_mutations(fake_route)
    assert len(muts) == 1
    assert muts[0][1]["workspace_instance_id"] == "explicit-inst-0001"


def test_f013_resolver_helper_direct():
    """helper 本身：数字 / 名称 / 未命中三种语义。"""
    client = _FakeClient()
    assert _resolve_ws_instance_by_id_or_name(client, "1711") == ISO_INSTANCE
    assert _resolve_ws_instance_by_id_or_name(client, "wt_iso_ws") == ISO_INSTANCE
    assert _resolve_ws_instance_by_id_or_name(client, "1193") == MAIN_INSTANCE
    with pytest.raises(DaemonRemoteError) as ei:
        _resolve_ws_instance_by_id_or_name(client, "ghost")
    assert ei.value.code == "workspace_not_found"


def test_f013_resolver_empty_registry_fail_closed():
    """空注册表（daemon 无 workspace）时 fail-closed，不回退。"""
    client = _FakeClient()
    client.calls.clear()

    def _empty(method, params):
        client.calls.append((method, params))
        return []

    client.call = _empty
    with pytest.raises(DaemonRemoteError) as ei:
        _resolve_ws_instance_by_id_or_name(client, "1711")
    assert ei.value.code == "workspace_not_found"
