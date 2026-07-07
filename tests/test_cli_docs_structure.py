"""C8 Step #7: 文档结构验证测试。

覆盖三个文档的关键章节存在性：
- docs/cli_reference.md: 12 大类概览表 + Deprecated --flag 清单 + 链接到 architecture.md
- docs/mcp_tools.md: 12 大类分组 + CLI↔MCP 命名映射对照表
- docs/architecture.md: 命令风格统一规范章节 + 三阶段迁移时间线

不验证具体内容细节（已在 Step #2-#6 的功能测试中覆盖），
仅验证文档骨架结构符合 Step #7 的 Check Items 要求。
"""

import os
import re
import sys

import pytest

# 项目根目录
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

DOCS_DIR = os.path.join(_PKG_PARENT, "docs")
CLI_REF = os.path.join(DOCS_DIR, "cli_reference.md")
MCP_TOOLS = os.path.join(DOCS_DIR, "mcp_tools.md")
ARCHITECTURE = os.path.join(DOCS_DIR, "architecture.md")

# 12 主分类名称（与 cli/main.py 的 _MAIN_HELP_GROUPS 对齐）
EXPECTED_MAIN_CATEGORIES = [
    "Workspace & Database",
    "Query & Search",
    "Call Chain Analysis",
    "Code Health & Metrics",
    "Task Orchestration",
    "Agent Rule Memory",
    "Audit & Bootstrap",
    "Git Integration",
    "Semgrep & Defects",
    "Coverage & Ownership",
    "GC",
    "Diagnostics",
]


def _read_file(path):
    """读取文档内容"""
    with open(path, encoding="utf-8") as f:
        return f.read()


# ============================================
# docs/cli_reference.md 结构验证
# ============================================


class TestCliReferenceDoc:
    """验证 cli_reference.md 文档结构"""

    def test_file_exists(self):
        assert os.path.isfile(CLI_REF), f"缺少文件: {CLI_REF}"

    def test_has_12_categories_overview_section(self):
        """[1] 文档开头包含 '命令概览（按 12 大功能分类）' 章节"""
        content = _read_file(CLI_REF)
        assert "命令概览（按 12 大功能分类）" in content, \
            "cli_reference.md 缺少 12 大类概览章节"

    def test_overview_table_contains_all_12_categories(self):
        """[1] 概览表包含全部 12 个主分类名称"""
        content = _read_file(CLI_REF)
        missing = [c for c in EXPECTED_MAIN_CATEGORIES if c not in content]
        assert not missing, f"cli_reference.md 概览缺少分类: {missing}"

    def test_has_deprecated_flag_section(self):
        """[1] 文档末尾包含 'Deprecated --flag 清单' 章节"""
        content = _read_file(CLI_REF)
        assert "Deprecated --flag 清单" in content, \
            "cli_reference.md 缺少 Deprecated --flag 清单章节"

    def test_deprecated_flag_table_has_60_entries(self):
        """[1] Deprecated --flag 清单包含 60 个 flag 条目

        通过统计表格行（| N | `--xxx` | ...）数量验证。
        """
        content = _read_file(CLI_REF)
        # 匹配表格行: | <序号> | `--xxx` | ... |
        pattern = re.compile(r"^\|\s*\d+\s*\|\s*`--[a-z-]+`\s*\|", re.MULTILINE)
        matches = pattern.findall(content)
        # 至少 60 个（允许少量通用 flag 也在表中，不强制恰好 60）
        assert len(matches) >= 60, \
            f"Deprecated --flag 清单条目不足: {len(matches)} < 60"

    def test_links_to_architecture_command_style(self):
        """[1] 文档开头链接到 architecture.md 的命令风格统一规范章节"""
        content = _read_file(CLI_REF)
        # 必须存在对 architecture.md 命令风格统一规范的交叉引用
        assert "命令风格统一规范" in content, \
            "cli_reference.md 缺少对 architecture.md 命令风格统一规范的引用"
        assert "architecture.md" in content, \
            "cli_reference.md 缺少 architecture.md 链接"

    def test_subcommand_style_recommendation(self):
        """[1] 文档说明 subcommand 为主、--flag deprecated 为辅"""
        content = _read_file(CLI_REF)
        assert "子命令风格" in content or "subcommand" in content.lower(), \
            "缺少 subcommand 风格说明"
        assert "deprecated" in content.lower() or "已废弃" in content, \
            "缺少 deprecated 说明"


# ============================================
# docs/mcp_tools.md 结构验证
# ============================================


class TestMcpToolsDoc:
    """验证 mcp_tools.md 文档结构"""

    def test_file_exists(self):
        assert os.path.isfile(MCP_TOOLS), f"缺少文件: {MCP_TOOLS}"

    def test_has_12_categories_section(self):
        """[2] 文档包含 '按 12 大功能分类' 章节"""
        content = _read_file(MCP_TOOLS)
        assert "按 12 大功能分类" in content, \
            "mcp_tools.md 缺少 '按 12 大功能分类' 章节"

    def test_overview_table_contains_all_12_categories(self):
        """[2] 概览表包含全部 12 个主分类名称"""
        content = _read_file(MCP_TOOLS)
        missing = [c for c in EXPECTED_MAIN_CATEGORIES if c not in content]
        assert not missing, f"mcp_tools.md 概览缺少分类: {missing}"

    def test_has_cli_mcp_mapping_section(self):
        """[2] 文档包含 'CLI↔MCP 命名映射对照表' 章节"""
        content = _read_file(MCP_TOOLS)
        assert "CLI↔MCP 命名映射对照表" in content, \
            "mcp_tools.md 缺少 CLI↔MCP 命名映射对照表章节"

    def test_mapping_table_has_100_plus_entries(self):
        """[2] CLI↔MCP 映射对照表包含 100+ 条映射

        通过统计表格行（| `cw xxx` | `yyy` | ...）数量验证。
        """
        content = _read_file(MCP_TOOLS)
        # 匹配映射表行: | `cw xxx` | `yyy` | ... |
        pattern = re.compile(r"^\|\s*`cw [^|]+`\s*\|\s*`[a-z_]+`\s*\|", re.MULTILINE)
        matches = pattern.findall(content)
        assert len(matches) >= 100, \
            f"CLI↔MCP 映射条目不足: {len(matches)} < 100"

    def test_mapping_grouped_by_12_categories(self):
        """[2] 映射表按 12 大类分组（每个分类有独立的 ### 子标题）"""
        content = _read_file(MCP_TOOLS)
        # 找到 CLI↔MCP 命名映射对照表 起始位置
        idx = content.find("CLI↔MCP 命名映射对照表")
        assert idx >= 0, "找不到 CLI↔MCP 命名映射对照表章节"
        # 截取映射表章节内容
        mapping_section = content[idx:]
        # 统计 ### [N] XXX 形式的子标题
        sub_headers = re.findall(r"^###\s+\[\d+\]\s+", mapping_section, re.MULTILINE)
        assert len(sub_headers) >= 12, \
            f"映射表分类子标题不足: {len(sub_headers)} < 12"


# ============================================
# docs/architecture.md 结构验证
# ============================================


class TestArchitectureDoc:
    """验证 architecture.md 的命令风格统一规范章节"""

    def test_file_exists(self):
        assert os.path.isfile(ARCHITECTURE), f"缺少文件: {ARCHITECTURE}"

    def test_has_command_style_section(self):
        """[3] 文档包含 '命令风格统一规范' 章节"""
        content = _read_file(ARCHITECTURE)
        assert "命令风格统一规范" in content, \
            "architecture.md 缺少 '命令风格统一规范' 章节"

    def test_section_has_subcommand_first_direction(self):
        """[3] 章节说明 subcommand 为主、--flag deprecated 为辅"""
        content = _read_file(ARCHITECTURE)
        idx = content.find("命令风格统一规范")
        assert idx >= 0
        section = content[idx:idx + 5000]  # 取章节后续 5000 字符
        assert "subcommand" in section.lower(), \
            "缺少 subcommand 关键词"
        assert "deprecated" in section.lower(), \
            "缺少 deprecated 关键词"
        # 验证"为主"/"为辅"或等价表述
        assert ("为主" in section) or ("primary" in section.lower()), \
            "缺少 'subcommand 为主' 的方向说明"

    def test_section_has_12_categories_table(self):
        """[3] 章节包含 12 主分类设计表"""
        content = _read_file(ARCHITECTURE)
        idx = content.find("命令风格统一规范")
        section = content[idx:idx + 10000]
        assert "12 主分类设计" in section or "12 主分类" in section, \
            "缺少 12 主分类设计表"

    def test_section_has_three_phase_migration(self):
        """[3] 章节包含三阶段迁移时间线"""
        content = _read_file(ARCHITECTURE)
        idx = content.find("命令风格统一规范")
        section = content[idx:idx + 15000]
        assert "迁移时间线" in section or "三阶段" in section, \
            "缺少迁移时间线说明"
        # 验证三个阶段都存在
        assert "阶段 1" in section, "缺少阶段 1"
        assert "阶段 2" in section, "缺少阶段 2"
        assert "阶段 3" in section, "缺少阶段 3"

    def test_section_has_design_decisions(self):
        """[3] 章节包含设计决策子章节"""
        content = _read_file(ARCHITECTURE)
        idx = content.find("命令风格统一规范")
        section = content[idx:idx + 20000]
        assert "设计决策" in section, \
            "缺少设计决策子章节"

    def test_section_cross_references_docs(self):
        """[3] 章节交叉引用 cli_reference.md 和 mcp_tools.md"""
        content = _read_file(ARCHITECTURE)
        idx = content.find("命令风格统一规范")
        section = content[idx:idx + 20000]
        assert "cli_reference.md" in section, \
            "缺少 cli_reference.md 交叉引用"
        assert "mcp_tools.md" in section, \
            "缺少 mcp_tools.md 交叉引用"
