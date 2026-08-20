"""已知源码缺陷回归门（QA Round 1 发现，路由给 Engineer 修复后本文件应全绿）。

缺陷描述（CW-1）：`server/tools/*.py` 薄壳化过程中，`scripts/thinify_tools.py`
把 JSON 风格字面量 `"sync": true/false` 直接写入 Python dict 字面量，
而 Python 语言关键字是 `True/False`（大写）。运行时执行到该 dict 构造即抛
`NameError: name 'true' is not defined`，导致 18 个 MCP 工具在调用时崩溃，
既破坏 M1（239/239 可路由——这 18 个工具路由前就崩），也破坏 R0.5 零回归。

修复建议（Engineer）：将 `server/tools/{tools_task,tools_query,tools_semantic,
tools_security,tools_summary,tools_workspace,tools_p2_graph}.py` 中
`"sync": true` → `"sync": True`、`"sync": false` → `"sync": False`，
并修正 `thinify_tools.py` 生成逻辑（防复发）。
"""
from __future__ import annotations

import ast
import os
import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_TOOLS_DIR = os.path.join(_REPO_ROOT, "server", "tools")

# 受影响函数清单（Round 1 AST 枚举）：模块 → [函数名]
AFFECTED: dict[str, list[str]] = {
    "tools_p2_graph.py": ["import_envelope_dependencies", "build_hard_dependency_edges"],
    "tools_query.py": ["run_semgrep_scan", "scan_semgrep_incremental"],
    "tools_security.py": ["detect_cross_repo_deps"],
    "tools_semantic.py": [
        "embed_symbols", "embed_single_symbol", "import_codeowners",
        "import_git_blame", "import_project_dependencies", "prune_external_symbols",
    ],
    "tools_summary.py": ["import_coverage"],
    "tools_task.py": ["detect_clones", "detect_clones_async",
                      "embed_symbols_async", "semgrep_scan_async"],
    "tools_workspace.py": ["import_git_history"],
}


def _find_json_literal_functions(path: str) -> set[str]:
    """返回函数体内使用 `true`/`false`（非关键字）作为表达式的函数名集合。"""
    src = open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    hits = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for sub in ast.walk(node):
                if (isinstance(sub, ast.Name) and sub.id in ("true", "false")
                        and isinstance(sub.ctx, ast.Load)):
                    hits.add(node.name)
                    break
    return hits


class TestJsonLiteralBooleanBug:
    """CW-1：薄壳不得使用 JSON 风格 true/false（必须 Python True/False）。"""

    @pytest.mark.parametrize("module", sorted(AFFECTED))
    def test_no_json_literal_booleans(self, module):
        path = os.path.join(_TOOLS_DIR, module)
        assert os.path.isfile(path), f"{module} 不存在"
        hits = _find_json_literal_functions(path)
        expected = set(AFFECTED[module])
        # 断言：期望无受影响函数；若有（bug 未修复）则明确列出
        assert not (hits & expected), (
            f"{module} 仍存在 JSON 字面量布尔缺陷（NameError: true/false）: "
            f"{sorted(hits & expected)}"
        )

    def test_affected_function_count(self):
        """汇总：17 个受影响函数（防新增遗漏）。"""
        total = sum(len(v) for v in AFFECTED.values())
        assert total == 17
