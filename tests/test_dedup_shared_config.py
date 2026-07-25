"""验证 _deduplicate_by_shared_config 去重逻辑"""
from callwarden.install import DetectedAgent, CallWardenInstaller


def make_agent(key, display, family):
    return DetectedAgent(
        agent_key=key, display=display,
        detected_by="cli", detect_detail="/fake/path",
        family=family,
    )


def test_cline_dedup_prefers_cli():
    """cline 家族：cline + cline-cli → 只保留 cline-cli"""
    agents = [
        make_agent("cline", "Cline", "cline"),
        make_agent("cline-cli", "Cline CLI", "cline"),
    ]
    result = CallWardenInstaller._deduplicate_by_shared_config(agents)
    keys = [a.agent_key for a in result]
    assert keys == ["cline-cli"], f"期望 ['cline-cli']，实际 {keys}"
    print("✓ cline 家族去重：cline + cline-cli → 只保留 cline-cli")


def test_cline_dedup_order_independent():
    """cline 家族：cline-cli 在前或后都应该保留 cline-cli"""
    agents = [
        make_agent("cline-cli", "Cline CLI", "cline"),
        make_agent("cline", "Cline", "cline"),
    ]
    result = CallWardenInstaller._deduplicate_by_shared_config(agents)
    keys = [a.agent_key for a in result]
    assert keys == ["cline-cli"], f"期望 ['cline-cli']，实际 {keys}"
    print("✓ cline 家族去重（顺序无关）：cline-cli + cline → 只保留 cline-cli")


def test_anthropic_no_dedup():
    """anthropic 家族：claude-code + claude-desktop → 都保留（不同配置文件）"""
    agents = [
        make_agent("claude-code", "Claude Code", "anthropic"),
        make_agent("claude-desktop", "Claude Desktop", "anthropic"),
    ]
    result = CallWardenInstaller._deduplicate_by_shared_config(agents)
    keys = [a.agent_key for a in result]
    assert len(keys) == 2, f"期望 2 个，实际 {len(keys)}: {keys}"
    print("✓ anthropic 家族不去重：Claude Code + Claude Desktop 都保留")


def test_google_no_dedup():
    """google 家族：gemini-cli + antigravity → 都保留（不同配置文件）"""
    agents = [
        make_agent("gemini-cli", "Gemini CLI", "google"),
        make_agent("antigravity", "Antigravity IDE", "google"),
    ]
    result = CallWardenInstaller._deduplicate_by_shared_config(agents)
    keys = [a.agent_key for a in result]
    assert len(keys) == 2, f"期望 2 个，实际 {len(keys)}: {keys}"
    print("✓ google 家族不去重：Gemini CLI + Antigravity 都保留")


def test_mixed_families():
    """混合场景：多家族多形态"""
    agents = [
        make_agent("cline", "Cline", "cline"),
        make_agent("cline-cli", "Cline CLI", "cline"),
        make_agent("claude-code", "Claude Code", "anthropic"),
        make_agent("claude-desktop", "Claude Desktop", "anthropic"),
        make_agent("cursor", "Cursor", "cursor"),
        make_agent("windsurf", "Windsurf", "codeium"),
    ]
    result = CallWardenInstaller._deduplicate_by_shared_config(agents)
    keys = [a.agent_key for a in result]
    assert "cline-cli" in keys, f"cline-cli 应保留，实际 {keys}"
    assert "cline" not in keys, f"cline 应被去重，实际 {keys}"
    assert "claude-code" in keys and "claude-desktop" in keys
    assert "cursor" in keys and "windsurf" in keys
    assert len(keys) == 5, f"期望 5 个，实际 {len(keys)}: {keys}"
    print(f"✓ 混合场景去重正确：6 → 5（cline 去重，其余保留）: {keys}")


def test_single_agent_no_change():
    """单个 Agent 不受影响"""
    agents = [make_agent("cursor", "Cursor", "cursor")]
    result = CallWardenInstaller._deduplicate_by_shared_config(agents)
    assert len(result) == 1 and result[0].agent_key == "cursor"
    print("✓ 单 Agent 不受去重影响")


def test_empty_list():
    """空列表"""
    result = CallWardenInstaller._deduplicate_by_shared_config([])
    assert result == []
    print("✓ 空列表返回空")


def test_cline_only_one_present():
    """cline 家族只有一个形态时不去重"""
    agents = [make_agent("cline", "Cline", "cline")]
    result = CallWardenInstaller._deduplicate_by_shared_config(agents)
    assert len(result) == 1 and result[0].agent_key == "cline"
    print("✓ cline 家族单形态不去重")


if __name__ == "__main__":
    test_cline_dedup_prefers_cli()
    test_cline_dedup_order_independent()
    test_anthropic_no_dedup()
    test_google_no_dedup()
    test_mixed_families()
    test_single_agent_no_change()
    test_empty_list()
    test_cline_only_one_present()
    print("\n全部 8 个测试通过 ✓")
