"""MCP 工具运行时消息 i18n 防回潮门禁（architecture.md 阶段 3 首项）。

两条硬约束：
1. 已 i18n 化的 6 个模块内不得再出现「非 docstring 的中文字符串字面量」
   （运行时消息必须走 ``i18n.t()``）；仅豁免 2 处裸 section-marker（工具分组
   注释，不参与运行时输出）。
2. ``mcp.messages`` 必须同时存在于 zh_CN / en_US 两套 catalog，key 集合一致、
   值非空，且 ``t()`` 在两种语言下都能解析出非 key 文本。
"""

import ast
import json
import re
from pathlib import Path

import pytest

from callwarden.i18n import get_language, set_language, t

REPO_ROOT = Path(__file__).resolve().parents[1]

# 本任务（T-1793458835218-f8812188）转换的运行时消息模块
CONVERTED_MODULES = [
    "server/mcp_server.py",
    "server/_mcp_common.py",
    "server/tools/tools_collab.py",
    "server/tools/tools_dependency_graph.py",
    "server/tools/tools_rules.py",
    "server/tools/tools_task.py",
]

# 既定排除项：裸 section-marker（不是运行时消息，转换范围外）
ALLOWED_SECTION_MARKERS = {
    "H4C-2 第三批 p2 依赖图/环检测组只读工具（5 个，"
    "T-1786747295227-b876fddf 步骤#1）",
    "H4C-3 任务组只读工具（P0-COMPAT-v3 后全部 rust_native）",
}

_CJK = re.compile(r"[\u4e00-\u9fff]")


def _runtime_chinese_literals(path: Path):
    """返回非 docstring 的中文字面量 ``[(lineno, value)]``。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstring_nodes = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                docstring_nodes.add(id(body[0].value))
    hits = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstring_nodes
            and _CJK.search(node.value)
        ):
            hits.append((node.lineno, node.value))
    return hits


@pytest.mark.parametrize("rel_path", CONVERTED_MODULES)
def test_converted_module_has_no_runtime_chinese_literals(rel_path):
    hits = _runtime_chinese_literals(REPO_ROOT / rel_path)
    unexpected = [(ln, val) for ln, val in hits if val not in ALLOWED_SECTION_MARKERS]
    assert not unexpected, (
        f"{rel_path} 仍有未 i18n 化的运行时中文字面量（应改走 i18n.t 的 "
        f"mcp.messages.* key）: {unexpected}"
    )


def _catalog(lang: str) -> dict:
    return json.loads(
        (REPO_ROOT / "i18n" / f"{lang}.json").read_text(encoding="utf-8")
    )


def _mcp_messages(lang: str) -> dict:
    return _catalog(lang).get("mcp", {}).get("messages", {})


@pytest.fixture
def restore_language():
    """恢复进入测试前的当前语言，避免污染同进程其他用例。"""
    previous = get_language()
    try:
        yield
    finally:
        set_language(previous)


def test_mcp_messages_present_in_both_catalogs():
    zh = _mcp_messages("zh_CN")
    en = _mcp_messages("en_US")
    assert isinstance(zh, dict) and isinstance(en, dict)
    assert zh, "zh_CN catalog 缺少 mcp.messages 分支"
    assert set(zh) == set(en), "zh_CN / en_US 的 mcp.messages key 集合不一致"
    for key in zh:
        assert isinstance(zh[key], str) and zh[key].strip(), f"zh_CN 值缺失: {key}"
        assert isinstance(en[key], str) and en[key].strip(), f"en_US 值缺失: {key}"
        assert not _CJK.search(en[key]), f"en_US 值不应含中文: {key}"


@pytest.mark.parametrize("lang", ["zh_CN", "en_US"])
def test_mcp_messages_resolve_in_both_languages(lang, restore_language):
    set_language(lang)
    keys = _mcp_messages(lang)
    assert keys, f"{lang} catalog 缺少 mcp.messages"
    for key in keys:
        resolved = t(f"mcp.messages.{key}")
        assert resolved != f"mcp.messages.{key}", f"{lang} 未解析到 key: {key}"
        assert resolved.strip()
