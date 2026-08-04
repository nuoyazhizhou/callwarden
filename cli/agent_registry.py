"""AI Agent 统一注册表模块

将 cli/main.py 中的 AGENT_SPECS（配置能力）和 install.py 中的
AGENT_DETECT_SPECS（探测规则）合并为单一数据源。

提供：
- AgentSpec dataclass（合并配置能力 + 探测规则）
- AGENT_REGISTRY：内置 23 个 Agent 的完整注册表
- AGENT_SPECS：向后兼容的 dict 接口
- load_registry_overlay / get_merged_specs：外部 JSON 扩展机制
"""

from dataclasses import dataclass, field, asdict
from typing import Optional, Dict, Tuple, List
import json
import os
import sys


# ---------------------------------------------------------------------------
# AgentSpec dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AgentSpec:
    """AI Agent 产品规格（合并配置能力 + 探测规则）"""
    key: str                     # Agent 唯一标识，如 "claude-code"
    display: str                 # 显示名称
    family: str                  # 产品家族：anthropic, cursor, trae, qoder, cognition, openai, google, aws, jetbrains, cline, baidu, tencent, moonshot, deepseek, opencode, pearai
    # --- 配置能力字段（原 AGENT_SPECS）---
    supports_mcp: bool = True
    supports_hooks: bool = False
    supports_rules: bool = False
    reads_agents_md: bool = False
    project_mcp_relpath: Optional[str] = None
    project_mcp_format: Optional[str] = None
    global_mcp_relpath: Optional[str] = None
    global_mcp_relpath_win: Optional[str] = None
    global_mcp_format: str = "merge_mcpServers"
    rules_relpath: Optional[str] = None
    rules_type: Optional[str] = None
    hooks_type: str = "none"
    # --- 探测字段（原 AGENT_DETECT_SPECS）---
    cli_commands: Tuple[str, ...] = ()
    config_dirs: Tuple[str, ...] = ()
    win_config_dirs: Tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# AGENT_REGISTRY — 内置 23 个 Agent（按产品家族分组）
# ---------------------------------------------------------------------------

AGENT_REGISTRY: Dict[str, AgentSpec] = {

    # ── Anthropic ──────────────────────────────────────────────────────────
    "claude-code": AgentSpec(
        key="claude-code",
        display="Claude Code",
        family="anthropic",
        supports_mcp=True,
        supports_hooks=True,
        supports_rules=True,
        reads_agents_md=True,
        project_mcp_relpath=".mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.claude.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=".callwarden/agent-integrations/claude-code/CALLWARDEN.md",
        rules_type="skill_md",
        hooks_type="claude_settings",
        cli_commands=("claude",),
        config_dirs=(".claude",),
    ),
    "claude-desktop": AgentSpec(
        key="claude-desktop",
        display="Claude Desktop",
        family="anthropic",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        project_mcp_relpath=None,
        project_mcp_format=None,
        global_mcp_relpath="~/Library/Application Support/Claude/claude_desktop_config.json",
        global_mcp_relpath_win="~/AppData/Roaming/Claude/claude_desktop_config.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=(),
        config_dirs=(),
        win_config_dirs=("Claude",),
    ),

    # ── Cursor ─────────────────────────────────────────────────────────────
    "cursor": AgentSpec(
        key="cursor",
        display="Cursor",
        family="cursor",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=True,
        reads_agents_md=False,
        project_mcp_relpath=".cursor/mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.cursor/mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=".cursor/rules/callwarden.mdc",
        rules_type="cursor_mdc",
        hooks_type="none",
        cli_commands=("cursor",),
        config_dirs=(".cursor",),
    ),

    # ── Cline ──────────────────────────────────────────────────────────────
    "cline": AgentSpec(
        key="cline",
        display="Cline",
        family="cline",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        # 项目级支持两种路径：Cline CLI 用 .cline/mcp.json，VSCode 扩展用 .cline/mcp_settings.json
        project_mcp_relpath=".cline/mcp.json",
        project_mcp_format="mcpServers",
        # 全局配置：Cline CLI 用 ~/.cline/mcp.json（用户清单指定）
        global_mcp_relpath="~/.cline/mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("cline",),
        config_dirs=(".cline",),
        win_config_dirs=("Code/User/globalStorage/saoudrizwan.claude-dev",),
    ),
    # Cline CLI —— Cline 的独立 CLI 形态，配置路径与 Cline 扩展相同
    "cline-cli": AgentSpec(
        key="cline-cli",
        display="Cline CLI",
        family="cline",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        project_mcp_relpath=".cline/mcp.json",
        project_mcp_format="json_mcp_servers",
        global_mcp_relpath="~/.cline/mcp.json",
        global_mcp_format="json_mcp_servers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("cline",),
        config_dirs=(".cline",),
    ),

    # ── Cognition（Devin）──────────────────────────────────────────────────
    # Devin CLI —— Cognition 产品，Windsurf（codeium 家族）是 Codeium 的产品，两者分属不同家族
    "devin-cli": AgentSpec(
        key="devin-cli",
        display="Devin CLI",
        family="cognition",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        project_mcp_relpath=".devin/config.json",
        project_mcp_format="json_mcp_servers",
        global_mcp_relpath="~/.config/devin/config.json",
        global_mcp_format="json_mcp_servers",
        rules_relpath=None,
        rules_type="generic_md",
        hooks_type="none",
        cli_commands=("devin",),
        config_dirs=("~/.config/devin",),
    ),

    # ── Codeium（Windsurf）───────────────────────────────────────────────
    # 注：Windsurf 已被 OpenAI 收购，但产品源自 Codeium。当前保留 codeium 家族名，
    # 待后续版本统一命名。Cognition（Devin）是完全独立的公司，见上方 cognition 家族。
    "windsurf": AgentSpec(
        key="windsurf",
        display="Windsurf",
        family="codeium",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=True,
        reads_agents_md=False,
        project_mcp_relpath=".windsurf/mcp_config.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.codeium/windsurf/mcp_config.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=".windsurf/rules/callwarden.md",
        rules_type="generic_md",
        hooks_type="none",
        cli_commands=("windsurf",),
        config_dirs=(".windsurf", ".codeium/windsurf"),
    ),

    # ── Trae ───────────────────────────────────────────────────────────────
    "trae": AgentSpec(
        key="trae",
        display="Trae IDE",
        family="trae",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=True,
        reads_agents_md=True,
        project_mcp_relpath=".trae/mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.trae/mcp.json",
        # Trae CN Windows 版使用独立路径
        global_mcp_relpath_win="~/AppData/Roaming/TRAE SOLO CN/User/mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=".callwarden/agent-integrations/trae/CALLWARDEN.md",
        rules_type="skill_md",
        hooks_type="none",
        cli_commands=("trae",),
        config_dirs=(".trae", ".trae-cn"),
        win_config_dirs=("TRAE SOLO CN",),
    ),

    # ── Google ─────────────────────────────────────────────────────────────
    # 注意：Gemini CLI 已并入 Antigravity IDE，两者同属 google 家族但保留独立条目
    "gemini-cli": AgentSpec(
        key="gemini-cli",
        display="Gemini CLI",
        family="google",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=True,
        project_mcp_relpath=".gemini/settings.json",
        project_mcp_format="merge_mcpServers",
        global_mcp_relpath="~/.gemini/settings.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("gemini",),
        config_dirs=(".gemini",),
    ),
    "antigravity": AgentSpec(
        key="antigravity",
        display="Antigravity IDE (Google)",
        family="google",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=True,
        reads_agents_md=False,
        project_mcp_relpath=".antigravity/mcp_config.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.gemini/antigravity-ide/mcp_config.json",
        global_mcp_relpath_win="~/.gemini/antigravity-ide/mcp_config.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=".antigravity/rules/callwarden.md",
        rules_type="generic_md",
        hooks_type="none",
        cli_commands=("antigravity",),
        config_dirs=(".antigravity", ".gemini/antigravity-ide"),
        win_config_dirs=("Antigravity", ".gemini/antigravity-ide"),
    ),

    # ── OpenAI ─────────────────────────────────────────────────────────────
    "codex": AgentSpec(
        key="codex",
        display="Codex CLI",
        family="openai",
        supports_mcp=True,
        supports_hooks=True,
        supports_rules=True,
        reads_agents_md=False,
        project_mcp_relpath=".codex/.mcp.json",
        project_mcp_format="mcpServers",
        # Codex CLI 使用 TOML 格式配置（~/.codex/config.toml）
        global_mcp_relpath="~/.codex/config.toml",
        global_mcp_format="toml_mcp_servers",
        rules_relpath=".codex-plugin/",
        rules_type="codex_skill",
        hooks_type="codex_hooks",
        cli_commands=("codex",),
        config_dirs=(".codex",),
    ),

    # ── OpenCode ───────────────────────────────────────────────────────────
    "opencode": AgentSpec(
        key="opencode",
        display="OpenCode",
        family="opencode",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=True,
        project_mcp_relpath=".opencode/opencode.json",
        project_mcp_format="merge_mcpServers",
        global_mcp_relpath="~/.config/opencode/opencode.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("opencode",),
        config_dirs=(".config/opencode", ".opencode"),
    ),

    # ── AWS ────────────────────────────────────────────────────────────────
    "kiro": AgentSpec(
        key="kiro",
        display="Kiro (AWS)",
        family="aws",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=True,
        reads_agents_md=False,
        project_mcp_relpath=".kiro/mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.kiro/mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=".kiro/rules/callwarden.md",
        rules_type="generic_md",
        hooks_type="none",
        cli_commands=("kiro",),
        config_dirs=(".kiro",),
    ),

    # ── Qoder（Alibaba）──────────────────────────────────────────────────
    "qoder": AgentSpec(
        key="qoder",
        display="Qoder (Alibaba)",
        family="qoder",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        project_mcp_relpath=".qoder/mcp.json",
        project_mcp_format="mcpServers",
        # 用户清单指定：~/.mcp.json（共享路径，多 Agent 共用）
        global_mcp_relpath="~/.mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("qoder",),
        config_dirs=(".qoder",),
    ),

    # ── JetBrains ──────────────────────────────────────────────────────────
    "jetbrains-junie": AgentSpec(
        key="jetbrains-junie",
        display="JetBrains Junie",
        family="jetbrains",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        # Junie 项目级配置（.junie/mcp/mcp.json），不支持全局配置
        project_mcp_relpath=".junie/mcp/mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath=None,
        global_mcp_format=None,
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=(),
        config_dirs=(".junie",),
    ),

    # ── Zed ────────────────────────────────────────────────────────────────
    "zed": AgentSpec(
        key="zed",
        display="Zed Editor",
        family="zed",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        # Zed 使用 context_servers 字段（非标准 mcpServers），command 为嵌套对象
        project_mcp_relpath=".zed/settings.json",
        project_mcp_format="context_servers",
        global_mcp_relpath="~/.config/zed/settings.json",
        global_mcp_relpath_win="~/AppData/Roaming/Zed/settings.json",
        global_mcp_format="merge_context_servers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("zed", "zeditor"),
        config_dirs=(".config/zed", ".zed"),
    ),

    # ── PearAI ─────────────────────────────────────────────────────────────
    "pearai": AgentSpec(
        key="pearai",
        display="PearAI",
        family="pearai",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        # PearAI 兼容 Cursor 格式（mcpServers）
        project_mcp_relpath=".pearai/mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.pearai/mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("pearai",),
        config_dirs=(".pearai",),
    ),

    # ── Moonshot（Kimi）──────────────────────────────────────────────────
    "kimi-code": AgentSpec(
        key="kimi-code",
        display="Kimi Code CLI",
        family="moonshot",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        # Kimi Code CLI 通过 --mcp-config <file> 启动时加载 MCP 配置
        # 写入 ~/.kimi-code/mcp.json，启动时用 kimi-code --mcp-config ~/.kimi-code/mcp.json
        project_mcp_relpath=".kimi-code/mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.kimi-code/mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("kimi-code", "kimi"),
        config_dirs=(".kimi-code", ".kimi"),
    ),

    # ── Tencent ────────────────────────────────────────────────────────────
    "codebuddy-cli": AgentSpec(
        key="codebuddy-cli",
        display="CodeBuddy Code CLI",
        family="tencent",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        # 腾讯云 CodeBuddy Code CLI，配置路径无公开文档，使用合理默认 ~/.codebuddy/mcp.json
        project_mcp_relpath=".codebuddy/mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.codebuddy/mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("codebuddy",),
        config_dirs=(".codebuddy",),
    ),

    # ── DeepSeek ───────────────────────────────────────────────────────────
    "deep-code": AgentSpec(
        key="deep-code",
        display="Deep Code CLI",
        family="deepseek",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        # Deep Code CLI（DeepSeek），配置路径无公开文档，使用合理默认 ~/.deepcode/mcp.json
        project_mcp_relpath=".deepcode/mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.deepcode/mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("deep-code", "deepcode"),
        config_dirs=(".deepcode", ".deep-code"),
    ),

    # ── Baidu ──────────────────────────────────────────────────────────────
    "comate": AgentSpec(
        key="comate",
        display="Comate AI IDE (Baidu)",
        family="baidu",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        project_mcp_relpath=".comate/mcp.json",
        project_mcp_format="mcpServers",
        global_mcp_relpath="~/.comate/mcp.json",
        global_mcp_format="merge_mcpServers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("comate",),
        config_dirs=(".comate",),
    ),
    # ── xAI（Grok）────────────────────────────────────────────────────────
    "grok-build": AgentSpec(
        key="grok-build",
        display="Grok Build",
        family="grok",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        project_mcp_relpath=".grok/mcp.json",
        project_mcp_format="json_mcp_servers",
        global_mcp_relpath="~/.grok/mcp.json",
        global_mcp_format="json_mcp_servers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=("grok",),
        config_dirs=("~/.grok",),
    ),

    # ── ZCode（智谱 AI）────────────────────────────────────────────────────
    # 桌面应用，无 CLI 命令，仅通过配置目录检测
    "zcode": AgentSpec(
        key="zcode",
        display="ZCode",
        family="zcode",
        supports_mcp=True,
        supports_hooks=False,
        supports_rules=False,
        reads_agents_md=False,
        project_mcp_relpath=".zcode/mcp.json",
        project_mcp_format="json_mcp_servers",
        global_mcp_relpath="~/.zcode/mcp.json",
        global_mcp_format="json_mcp_servers",
        rules_relpath=None,
        rules_type=None,
        hooks_type="none",
        cli_commands=(),
        config_dirs=("~/.zcode",),
    ),
}


# ---------------------------------------------------------------------------
# 兼容层
# ---------------------------------------------------------------------------

def as_dict(spec: AgentSpec) -> dict:
    """将 AgentSpec 转为兼容旧 AGENT_SPECS 格式的 dict

    保持与 cli/main.py 和 install.py 中 spec.get("xxx") 调用完全兼容。
    注意：探测字段（cli_commands, config_dirs, win_config_dirs）不包含在输出中，
    因为这些字段在旧 AGENT_SPECS 中不存在。
    """
    return {
        "display": spec.display,
        "supports_mcp": spec.supports_mcp,
        "supports_hooks": spec.supports_hooks,
        "supports_rules": spec.supports_rules,
        "reads_agents_md": spec.reads_agents_md,
        "project_mcp_relpath": spec.project_mcp_relpath,
        "project_mcp_format": spec.project_mcp_format,
        "global_mcp_relpath": spec.global_mcp_relpath,
        "global_mcp_relpath_win": spec.global_mcp_relpath_win,
        "global_mcp_format": spec.global_mcp_format,
        "rules_relpath": spec.rules_relpath,
        "rules_type": spec.rules_type,
        "hooks_type": spec.hooks_type,
    }


# 向后兼容的 dict 接口（旧代码可直接 from .agent_registry import AGENT_SPECS）
AGENT_SPECS: Dict[str, dict] = {
    k: as_dict(v) for k, v in AGENT_REGISTRY.items()}


# ---------------------------------------------------------------------------
# 外部 JSON 扩展机制
# ---------------------------------------------------------------------------

def load_registry_overlay(overlay_path: str = "") -> Dict[str, dict]:
    """加载外部 JSON 注册表叠加层

    叠加规则：
    - 新 key：扩展注册表
    - 已存在 key：覆盖内置条目（打印 warning）
    - 文件不存在或解析失败：静默返回空 dict
    """
    if not overlay_path:
        # 默认路径：.callwarden/agent_registry.json（相对于用户数据目录）
        home = os.path.expanduser("~")
        overlay_path = os.path.join(home, ".callwarden", "agent_registry.json")

    if not os.path.exists(overlay_path):
        return {}

    try:
        with open(overlay_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (ValueError, OSError):
        return {}

    if not isinstance(data, list):
        return {}

    result = {}
    for item in data:
        if not isinstance(item, dict) or "key" not in item:
            continue
        result[item["key"]] = item
    return result


def get_merged_specs(overlay_path: str = "") -> Dict[str, dict]:
    """返回合并后的完整 specs（内置 + 外部叠加）

    叠加规则：
    - 新 key：直接扩展注册表
    - 已存在 key：以内置 spec 为底，叠加 overlay 字段（保留默认值如 hooks_type）
    """
    merged = dict(AGENT_SPECS)
    overlay = load_registry_overlay(overlay_path)
    for key, spec in overlay.items():
        if key in merged:
            print(f"  [overlay] overriding built-in spec: {key}")
            # 以内置 spec 为底，叠加 overlay 字段，避免丢失默认字段
            base = dict(merged[key])
            base.update(spec)
            merged[key] = base
        else:
            merged[key] = spec
    return merged
