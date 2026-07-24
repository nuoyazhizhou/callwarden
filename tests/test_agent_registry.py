"""AI Agent 统一注册表测试

覆盖注册表加载、叠加合并、字段完整性和向后兼容性。
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_registry_overlay_loading(tmp_path):
    """外部 JSON 叠加层应正确扩展和覆盖内置 Agent"""
    from cli.agent_registry import load_registry_overlay, get_merged_specs, AGENT_SPECS

    # 创建临时 overlay 文件
    overlay = [
        {"key": "new-test-agent", "display": "New Test Agent", "family": "test"},
        {"key": "cursor", "display": "Cursor Override", "family": "cursor"},
    ]
    overlay_file = tmp_path / "test_registry.json"
    overlay_file.write_text(json.dumps(overlay), encoding="utf-8")

    # 测试 load_registry_overlay
    result = load_registry_overlay(str(overlay_file))
    assert "new-test-agent" in result
    assert "cursor" in result
    assert result["cursor"]["display"] == "Cursor Override"

    # 测试 get_merged_specs 合并
    merged = get_merged_specs(str(overlay_file))
    assert "new-test-agent" in merged  # 新增
    assert merged["cursor"]["display"] == "Cursor Override"  # 覆盖
    assert "claude-code" in merged  # 内置保留
    # 覆盖已有 key 时，必须以内置 spec 为底，保留未覆盖的默认字段（如 hooks_type）
    assert merged["cursor"]["hooks_type"] == "none", "overlay 覆盖不应丢失内置默认字段"
    assert merged["cursor"]["supports_mcp"] is True


def test_windsurf_is_codeium_product():
    """Windsurf 是 Codeium 的产品，不应与 Cognition Devin 混淆"""
    from cli.agent_registry import AGENT_REGISTRY

    spec = AGENT_REGISTRY["windsurf"]
    assert spec.display == "Windsurf", "Windsurf display 应为 'Windsurf'"
    assert spec.family == "codeium", "Windsurf 属于 Codeium 家族"


def test_jetbrains_junie_project_only():
    """jetbrains-junie 仅支持项目级配置，无全局路径"""
    from cli.agent_registry import AGENT_REGISTRY

    spec = AGENT_REGISTRY["jetbrains-junie"]
    assert spec.global_mcp_relpath is None, "Junie 不支持全局 MCP 配置"
    assert spec.global_mcp_format is None, "Junie 全局格式应为 None"
    assert spec.project_mcp_relpath == ".junie/mcp/mcp.json"


@pytest.mark.xdist_group("no_tmpfile")
def test_registry_missing_file_graceful():
    """缺失的 overlay 文件应静默返回空 dict"""
    from cli.agent_registry import load_registry_overlay

    result = load_registry_overlay("/nonexistent/path/registry.json")
    assert result == {}


def test_agent_spec_has_family():
    """AGENT_REGISTRY 中所有 Agent 必须有非空的 family 字段"""
    from cli.agent_registry import AGENT_REGISTRY

    for key, spec in AGENT_REGISTRY.items():
        assert spec.family, f"Agent {key} has empty family"
        assert isinstance(spec.family, str)


def test_as_dict_compatibility():
    """as_dict() 输出的 dict 必须包含所有 13 个旧字段"""
    from cli.agent_registry import AGENT_REGISTRY, as_dict

    expected_keys = {
        "display", "supports_mcp", "supports_hooks", "supports_rules",
        "reads_agents_md", "project_mcp_relpath", "project_mcp_format",
        "global_mcp_relpath", "global_mcp_relpath_win", "global_mcp_format",
        "rules_relpath", "rules_type", "hooks_type",
    }

    for key, spec in AGENT_REGISTRY.items():
        d = as_dict(spec)
        assert set(d.keys()) == expected_keys, f"Agent {key}: keys mismatch"
        # 验证 .get() 兼容
        assert d.get("display") == spec.display


def test_agent_registry_count():
    """注册表应包含至少 23 个 Agent（19 原始 + cline-cli/devin-cli/grok-build/zcode）"""
    from cli.agent_registry import AGENT_REGISTRY
    assert len(AGENT_REGISTRY) >= 23


def test_new_agents_exist():
    """验证 4 个新增 Agent 存在且 family 正确"""
    from cli.agent_registry import AGENT_REGISTRY

    expected = {
        "grok-build": ("Grok Build", "grok"),
        "zcode": ("ZCode", "zcode"),
        "devin-cli": ("Devin CLI", "cognition"),
        "cline-cli": ("Cline CLI", "cline"),
    }
    for key, (display, family) in expected.items():
        assert key in AGENT_REGISTRY, f"新增 Agent {key} 未找到"
        spec = AGENT_REGISTRY[key]
        assert spec.display == display, f"{key} display 应为 {display}"
        assert spec.family == family, f"{key} family 应为 {family}"


def test_family_uniqueness():
    """验证每个 Agent 的 family 非空且为字符串"""
    from cli.agent_registry import AGENT_REGISTRY

    families = set()
    for key, spec in AGENT_REGISTRY.items():
        assert spec.family, f"Agent {key} 的 family 不能为空"
        assert isinstance(spec.family, str), f"Agent {key} 的 family 必须是字符串"
        families.add(spec.family)

    # 确保至少有一定数量的不同家族
    assert len(families) >= 15, f"家族种类数不足，当前: {len(families)}"
