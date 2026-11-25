"""MCP 响应预算 + 工具面 profile 单元测试（参考 GitNexus 响应预算设计）。

对应实现：server/response_budget.py（预算）与 server/tools/__init__.py
（CALLWARDEN_MCP_TOOL_PROFILE 裁剪注册面）。
"""

from __future__ import annotations

import asyncio

import pytest

from callwarden.server.response_budget import (
    EnvError,
    default_max_tokens,
    enforce_budget,
    estimate_tokens,
    trim_to_budget,
)
from callwarden.server.tools import (
    _MODULES,
    active_profile,
    modules_for_profile,
    register_all,
)


# ----------------------------------------------------------------------
# 预算估算
# ----------------------------------------------------------------------


def test_estimate_tokens_deterministic():
    payload = {"a": "x" * 400}
    assert estimate_tokens(payload) == estimate_tokens(payload)
    # 4 字节/token：405 字节紧凑 JSON → ~101 token
    assert 90 <= estimate_tokens(payload) <= 110
    assert estimate_tokens(None) == 0


def test_default_max_tokens_unset_means_unlimited(monkeypatch):
    monkeypatch.delenv("CALLWARDEN_MCP_DEFAULT_MAX_TOKENS", raising=False)
    assert default_max_tokens() == 0


def test_default_max_tokens_valid(monkeypatch):
    monkeypatch.setenv("CALLWARDEN_MCP_DEFAULT_MAX_TOKENS", "2048")
    assert default_max_tokens() == 2048


@pytest.mark.parametrize("bad", ["abc", "-1", "1.5"])
def test_default_max_tokens_invalid_fails_closed(monkeypatch, bad):
    monkeypatch.setenv("CALLWARDEN_MCP_DEFAULT_MAX_TOKENS", bad)
    with pytest.raises(EnvError):
        default_max_tokens()


# ----------------------------------------------------------------------
# deepest-first 裁剪
# ----------------------------------------------------------------------


def _big_result(items: int = 200) -> dict:
    return {
        "ok": True,
        "layers": [
            {"depth": d, "symbols": [{"name": f"s{i}", "qualified_name": f"m.f{i}"} for i in range(items)]}
            for d in range(3)
        ],
    }


def test_small_result_untouched():
    result = {"ok": True, "items": [1, 2, 3]}
    trimmed, changed = trim_to_budget(result, 100_000)
    assert changed is False
    assert trimmed == {"ok": True, "items": [1, 2, 3]}
    assert "_budget" not in trimmed


def test_trim_drops_deepest_list_tail_and_marks_budget():
    result = _big_result(200)
    trimmed, changed = trim_to_budget(result, 2000)
    assert changed is True
    assert "_budget" in trimmed
    meta = trimmed["_budget"]
    assert meta["trimmed"] is True
    assert meta["max_tokens"] == 2000
    assert meta["strategy"] == "deepest_first_drop_tail"
    # 每层至少保留 1 条
    for layer in trimmed["layers"]:
        assert len(layer["symbols"]) >= 1
    # 有实际丢弃记录
    assert any(entry["dropped"] > 0 for entry in meta["shown_by_depth"])
    # 裁剪后不再超预算（或已无可裁列表）
    assert estimate_tokens(trimmed) <= 2000 or all(
        len(layer["symbols"]) == 1 for layer in trimmed["layers"]
    )


def test_trim_deepest_first_prefers_deeper_lists():
    result = {
        "outer": [{"inner": [{"x": "y" * 50} for _ in range(50)]}],
        "flat": [{"x": "z" * 50} for _ in range(50)],
    }
    trimmed, changed = trim_to_budget(result, 700)
    assert changed is True
    # 更深的 outer[].inner[] 先被裁
    assert len(trimmed["outer"][0]["inner"]) < 50
    assert "_budget" in trimmed


def test_trim_root_list_no_meta():
    result = [{"name": f"n{i}", "pad": "p" * 100} for i in range(200)]
    trimmed, changed = trim_to_budget(result, 500)
    assert changed is True
    assert isinstance(trimmed, list)
    assert len(trimmed) >= 1


def test_enforce_budget_zero_returns_unchanged():
    result = _big_result(500)
    assert enforce_budget(result, max_tokens=0) is result


def test_enforce_budget_env_default(monkeypatch):
    monkeypatch.delenv("CALLWARDEN_MCP_DEFAULT_MAX_TOKENS", raising=False)
    result = _big_result(50)
    assert enforce_budget(result) is result


# ----------------------------------------------------------------------
# 工具面 profile
# ----------------------------------------------------------------------


def test_modules_for_all_is_full():
    assert modules_for_profile("all") == list(_MODULES)


@pytest.mark.parametrize(
    "profile,expected_subset",
    [
        ("core", ["tools_query", "tools_workspace", "tools_summary"]),
        ("graph", ["tools_query", "tools_semantic", "tools_summary", "tools_dependency_graph"]),
        ("task", ["tools_task", "tools_collab", "tools_identity", "tools_lease", "tools_task_prompt"]),
        ("security", ["tools_security", "tools_rules"]),
    ],
)
def test_modules_for_profile_subset(profile, expected_subset):
    selected = modules_for_profile(profile)
    assert expected_subset == selected
    assert len(selected) < len(_MODULES)


def test_active_profile_default_all(monkeypatch):
    monkeypatch.delenv("CALLWARDEN_MCP_TOOL_PROFILE", raising=False)
    assert active_profile() == "all"


def test_active_profile_invalid_fails_closed(monkeypatch):
    monkeypatch.setenv("CALLWARDEN_MCP_TOOL_PROFILE", "bogus")
    with pytest.raises(SystemExit):
        active_profile()


def test_register_all_profile_core_registers_subset():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("cw-profile-test")
    register_all(mcp, profile="core")
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert names, "core profile 应注册非空工具面"
    # core 面不含 security / task_prompt 工具
    assert not any("security" in n or "prompt" in n for n in names)


def test_register_all_default_all_keeps_full_surface(monkeypatch):
    from mcp.server.fastmcp import FastMCP

    monkeypatch.delenv("CALLWARDEN_MCP_TOOL_PROFILE", raising=False)
    mcp = FastMCP("cw-all-test")
    register_all(mcp)
    core = FastMCP("cw-core-test")
    register_all(core, profile="core")
    all_names = {t.name for t in asyncio.run(mcp.list_tools())}
    core_names = {t.name for t in asyncio.run(core.list_tools())}
    assert len(all_names) > len(core_names)
