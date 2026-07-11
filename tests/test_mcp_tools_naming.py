"""C8 Step #6: MCP 工具命名对齐 + 分组 + 文档测试。

覆盖：
- 所有 @mcp.tool() 都有 docstring
- 没有重名的工具
- 至少 100 个 @mcp.tool()
- 所有工具名符合前缀约定（get_/search_/list_/find_/detect_/import_/create_/
  delete_/update_/task_/rule_/audit_/gc_/lsp_/embed_/semantic_/project_/repo_/
  who_/cross_repo_/branch_/edit_/work_ 等已知前缀）
- server/mcp_server.py 中存在 12 个分类注释（[L1]-[L12]）
- docs/mcp_tools.md 包含 CLI↔MCP 映射对照表
- server/mcp_server.py 语法正确

审计依据：.mcp_audit.md（173 个 @mcp.tool()，30+ 前缀种类，0 严重不一致）。
"""

import ast
import os
import py_compile
import re
from collections import Counter

import pytest

# 项目根目录
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MCP_SERVER_PATH = os.path.join(_PKG_PARENT, "server", "mcp_server.py")
_MCP_TOOLS_DOC_PATH = os.path.join(_PKG_PARENT, "docs", "mcp_tools.md")
_MCP_AUDIT_PATH = os.path.join(_PKG_PARENT, ".mcp_audit.md")


# ============================================
# 工具函数
# ============================================


def _parse_mcp_server():
    """解析 server/mcp_server.py 源码，返回 AST Module 节点。"""
    with open(_MCP_SERVER_PATH, encoding="utf-8") as f:
        source = f.read()
    return ast.parse(source, filename=_MCP_SERVER_PATH), source


def _extract_mcp_tools(tree):
    """从 AST 中提取所有 @mcp.tool() 装饰的工具。

    遍历整棵 AST，找出带 `@mcp.tool()` 或 `@mcp.tool` 装饰器的 FunctionDef，
    返回 `list[(name, docstring, lineno)]`。
    """
    tools = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        has_mcp_tool = False
        for dec in node.decorator_list:
            # @mcp.tool() — Call(func=Attribute(attr='tool', value=Name(id='mcp')))
            if isinstance(dec, ast.Call):
                func = dec.func
                if (isinstance(func, ast.Attribute)
                        and func.attr == "tool"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "mcp"):
                    has_mcp_tool = True
                    break
            # @mcp.tool — Attribute(attr='tool', value=Name(id='mcp'))
            elif (isinstance(dec, ast.Attribute)
                    and dec.attr == "tool"
                    and isinstance(dec.value, ast.Name)
                    and dec.value.id == "mcp"):
                has_mcp_tool = True
                break
        if has_mcp_tool:
            doc = ast.get_docstring(node)
            tools.append((node.name, doc, node.lineno))
    return tools


# 允许的命名前缀（参考 .mcp_audit.md §2 + .cli_audit.md §4.3）。
# 包含动词前缀与模块前缀两类，凡以其中任一开头的工具名均视为合规。
_ALLOWED_PREFIXES = (
    # —— 动词前缀 ——
    "get_", "search_", "list_", "find_", "detect_", "import_", "create_",
    "delete_", "update_", "export_", "restore_", "run_", "record_",
    "register_", "build_", "refresh_", "propose_", "revert_", "generate_",
    "parse_", "rotate_", "link_", "prune_", "clear_", "ask_", "remove_",
    "set_", "review_", "test_", "merge_", "switch_", "diff_", "check_",
    "extract_", "resolve_", "cancel_", "submit_",
    # —— 模块前缀 ——
    "task_", "gc_", "rule_", "audit_", "lsp_", "guardrail_", "file_",
    "work_", "defect_", "embed_", "semantic_", "project_", "repo_",
    "cross_repo_", "cross_", "blast_", "bootstrap_", "cleanup_",
    "evolution_", "churn_", "hotspot_", "who_", "branch_",
)


def _prefix_of(name):
    """返回工具名的前缀（首个下划线之前含下划线），如 'get_stats' -> 'get_'。"""
    idx = name.find("_")
    if idx < 0:
        return ""
    return name[: idx + 1]


# ============================================
# 1. 所有 @mcp.tool() 都有 docstring
# ============================================


def test_all_mcp_tools_have_docstring():
    """所有 @mcp.tool() 装饰的函数都应有 docstring（供 MCP client 展示）。"""
    tree, _ = _parse_mcp_server()
    tools = _extract_mcp_tools(tree)
    assert tools, "未提取到任何 @mcp.tool() 工具"
    missing = [(name, line) for name, doc, line in tools if not doc]
    assert not missing, (
        f"以下 @mcp.tool() 工具缺少 docstring（共 {len(missing)} 个）: {missing[:10]}"
    )


# ============================================
# 2. 没有重名的工具
# ============================================


def test_no_duplicate_tool_names():
    """所有 @mcp.tool() 工具名应唯一，不允许重名（重名会导致后注册覆盖前者）。"""
    tree, _ = _parse_mcp_server()
    tools = _extract_mcp_tools(tree)
    names = [name for name, _, _ in tools]
    counts = Counter(names)
    duplicates = {name: cnt for name, cnt in counts.items() if cnt > 1}
    assert not duplicates, (
        f"发现重复的 MCP 工具名: {duplicates}"
    )


# ============================================
# 3. 至少 100 个 @mcp.tool()
# ============================================


def test_tool_count_at_least_100():
    """@mcp.tool() 工具总数应 ≥ 100（当前审计数 173，留有冗余）。"""
    tree, _ = _parse_mcp_server()
    tools = _extract_mcp_tools(tree)
    count = len(tools)
    assert count >= 100, (
        f"@mcp.tool() 工具数应 ≥ 100，实际 {count}"
    )


# ============================================
# 4. 所有工具名符合前缀约定
# ============================================


def test_naming_prefix_conventions():
    """所有工具名应以已知前缀（动词或模块前缀）开头。

    约定来源：.cli_audit.md §4.3 与 .mcp_audit.md §2。
    若出现未知前缀，需在此列表中补充，或修正工具命名。
    """
    tree, _ = _parse_mcp_server()
    tools = _extract_mcp_tools(tree)
    violations = []
    for name, _, line in tools:
        if not any(name.startswith(p) for p in _ALLOWED_PREFIXES):
            violations.append((name, line, _prefix_of(name)))
    assert not violations, (
        f"以下工具名不符合已知前缀约定（共 {len(violations)} 个），"
        f"需补充前缀或修正命名: {violations[:10]}"
    )


# ============================================
# 5. server/mcp_server.py 中存在 12 个分类注释
# ============================================


def test_category_comments_exist():
    """server/mcp_server.py 中存在 12 大类分组注释（[L1]-[L12] 标记）。

    分组注释格式形如 `# [L1] ...` / `# [L9+L2] ...`，
    覆盖 .cli_audit.md §2 定义的 12 个主分类。
    """
    _, source = _parse_mcp_server()
    # 匹配 [L1] / [L1+L2] / [L9+L2] 等标记，提取其中所有 L<数字>
    pattern = re.compile(r"\[L((?:\d+\+)*\d+)\]")
    found_categories = set()
    for match in pattern.finditer(source):
        for num_str in match.group(1).split("+"):
            found_categories.add(int(num_str))
    expected = set(range(1, 13))  # {1, 2, ..., 12}
    missing = expected - found_categories
    assert not missing, (
        f"server/mcp_server.py 中缺少以下分类的分组注释: {sorted(missing)}"
    )


# ============================================
# 6. docs/mcp_tools.md 包含 CLI↔MCP 映射对照表
# ============================================


def test_cli_to_mcp_mapping_table_in_docs():
    """docs/mcp_tools.md 应包含 CLI↔MCP 命名映射对照表章节（C8 Step #6 交付物）。"""
    with open(_MCP_TOOLS_DOC_PATH, encoding="utf-8") as f:
        content = f.read()
    # 章节标题
    assert "CLI↔MCP 命名映射对照表" in content, (
        "docs/mcp_tools.md 缺少 'CLI↔MCP 命名映射对照表' 章节"
    )
    # 至少包含若干 12 大类的子表标题（按主分类编号或名称）
    category_markers = [
        "Workspace & Database",
        "Query & Search",
        "Call Chain Analysis",
        "Task Orchestration",
    ]
    missing = [m for m in category_markers if m not in content]
    assert not missing, (
        f"docs/mcp_tools.md 映射表缺少以下分类章节: {missing}"
    )
    # 至少 30 条映射条目（`| cw` 开头的表格行）
    mapping_rows = re.findall(r"^\|\s*`cw ", content, flags=re.MULTILINE)
    assert len(mapping_rows) >= 30, (
        f"映射表条目应 ≥ 30 条，实际 {len(mapping_rows)} 条"
    )


# ============================================
# 7. server/mcp_server.py 语法正确
# ============================================


def test_python_syntax_ok():
    """server/mcp_server.py 语法正确（py_compile 通过）。"""
    py_compile.compile(_MCP_SERVER_PATH, doraise=True)


# ============================================
# 附：审计报告存在性（可选，确保 .mcp_audit.md 已交付）
# ============================================


def test_mcp_audit_report_exists():
    """C8 Step #6 审计报告 .mcp_audit.md 已交付。"""
    assert os.path.isfile(_MCP_AUDIT_PATH), (
        f"未找到 MCP 审计报告: {_MCP_AUDIT_PATH}"
    )
    with open(_MCP_AUDIT_PATH, encoding="utf-8") as f:
        content = f.read()
    assert "173" in content, "审计报告应记录 173 个 @mcp.tool() 工具"
    assert "12 大类" in content, "审计报告应包含 12 大类分组结果"
