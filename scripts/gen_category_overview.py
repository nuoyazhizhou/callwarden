#!/usr/bin/env python3
"""gen_category_overview.py —— 从分类真相源生成文档概览表与映射表。

设计契约（cli-mcp-surface-audit Phase 3）：
- `cli/categories.py` 的 COMMAND_CATEGORIES（84 顶层命令 / 21 分类）与
  `server/tools/_categories.py` 的 TOOL_CATEGORIES（243 工具 / 17 分类）
  是**单一真相源**；本脚本只做读取与派生，不修改分类内容。
- 文档中 `<!-- BEGIN:generated-overview ... -->` 等 marker 标记块内的
  内容由本脚本生成，标记块外的文字手工维护。

输出：
- `--emit-cli`    把 CLI 概览表写回 docs/cli_reference.md 生成区间
                  （BEGIN/END marker 之间，header/footer 不动）。
- `--emit-mcp`    把 MCP 概览表写回 docs/mcp_tools.md 生成区间。
- `--emit-mapping` 把「CLI↔MCP 命名映射对照表」（243 工具全量，17 小节）
                  与「[1]-[17] 域内 CLI 独有命令」两块写回 docs/mcp_tools.md。
- `--print-cli` / `--print-mcp` / `--print-mapping`
                  打印生成内容（人工核对，不写文件）。
- `--check`       只读自检：内存生成结果 vs 文档各 marker 区间
                  byte-drift 比较，不写任何文件；退出码 0/1（可进 CI）。

用法：
    python scripts/gen_category_overview.py --emit-cli
    python scripts/gen_category_overview.py --emit-mcp
    python scripts/gen_category_overview.py --emit-mapping
    python scripts/gen_category_overview.py --check
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any, List, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CLI_DOC = os.path.join(_REPO_ROOT, "docs", "cli_reference.md")
_MCP_DOC = os.path.join(_REPO_ROOT, "docs", "mcp_tools.md")

# 生成区间 marker（marker 之间为脚本权威区，DO NOT EDIT）
_OVERVIEW_BEGIN = ("<!-- BEGIN:generated-overview "
                   "(scripts/gen_category_overview.py; DO NOT EDIT) -->")
_OVERVIEW_END = "<!-- END:generated-overview -->"
_MAPPING_BEGIN = ("<!-- BEGIN:generated-mapping "
                  "(scripts/gen_category_overview.py; DO NOT EDIT) -->")
_MAPPING_END = "<!-- END:generated-mapping -->"
_CLI_ONLY_BEGIN = ("<!-- BEGIN:generated-cli-only "
                   "(scripts/gen_category_overview.py; DO NOT EDIT) -->")
_CLI_ONLY_END = "<!-- END:generated-cli-only -->"


# ---------------------------------------------------------------------------
# 1. 真相源加载（importlib 按路径加载，不依赖 callwarden 安装，
#    与 gen_route_matrix.py 的自包含风格一致）
# ---------------------------------------------------------------------------

def _load_module(rel_path: str, alias: str) -> Any:
    """按文件路径加载 Python 模块（cli/categories.py 等无内部依赖的数据模块）。

    注意：加载含 dataclass 的模块必须先把模块注册进 sys.modules，
    否则 dataclass 处理器查 cls.__module__ 命名空间会得到 None。
    """
    path = os.path.join(_REPO_ROOT, rel_path)
    spec = importlib.util.spec_from_file_location(alias, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[alias] = mod
    spec.loader.exec_module(mod)
    return mod


_cli_categories = _load_module(os.path.join("cli", "categories.py"),
                               "_cli_categories")
_mcp_categories = _load_module(
    os.path.join("server", "tools", "_categories.py"), "_mcp_categories")

COMMAND_CATEGORIES = _cli_categories.COMMAND_CATEGORIES
TOOL_CATEGORIES = _mcp_categories.TOOL_CATEGORIES
TOOL_CLI_MAPPING = _mcp_categories.TOOL_CLI_MAPPING


# ---------------------------------------------------------------------------
# 2. 概览表 / 映射表生成
# ---------------------------------------------------------------------------

def build_cli_overview() -> str:
    """生成 CLI 概览表 markdown（docs/cli_reference.md 生成区间内容）。

    列结构与既有文档一致（# / 主分类 / 命令数 / 涵盖范围 / 主要 subcommand），
    增加命令数列；CLI-only 分类在主分类名后标注「（CLI 独有）」。
    """
    lines = [
        "| # | 主分类 | 命令数 | 涵盖范围 | 主要 subcommand |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, cat in enumerate(COMMAND_CATEGORIES, 1):
        title = cat.title + ("（CLI 独有）" if cat.cli_only else "")
        cmds = "、".join(f"`{c.name}`" for c in cat.commands)
        lines.append(
            f"| {idx} | **{title}** | {len(cat.commands)} | {cat.scope} | {cmds} |")
    total = sum(len(c.commands) for c in COMMAND_CATEGORIES)
    lines.append(f"| **合计** | | **{total}** | | |")
    return "\n".join(lines)


def build_mcp_overview() -> str:
    """生成 MCP 概览表 markdown（docs/mcp_tools.md 生成区间内容）。

    「对应 CLI 主分类」列由 TOOL_CATEGORIES.cli_category 引用
    COMMAND_CATEGORIES 的同构 key 派生——[13]-[17] 不再是
    「纯 MCP 无对应 CLI」（修复概览表与文末增量映射的自相矛盾，AC-4）。
    """
    cli_title_by_key = {c.key: c.title for c in COMMAND_CATEGORIES}
    lines = [
        "| # | 主分类 | 工具数 | 涵盖范围 | 对应 CLI 主分类 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for idx, cat in enumerate(TOOL_CATEGORIES, 1):
        cli_title = cli_title_by_key[cat.cli_category]
        lines.append(
            f"| {idx} | **{cat.title}** | {len(cat.tools)} | {cat.scope} "
            f"| {cli_title} |")
    total = sum(len(c.tools) for c in TOOL_CATEGORIES)
    lines.append(f"| **合计** | | **{total}** | | |")
    return "\n".join(lines)


def build_mapping_doc() -> str:
    """生成 243 工具全量 CLI 映射表（docs/mcp_tools.md mapping 生成区间）。

    17 个 `### [N]` 小节与概览表同构；每工具一行（含 MCP 专属 `—` 行），
    小节末尾附分类统计注释。数据源 TOOL_CLI_MAPPING（单一真相源）。
    """
    lines: List[str] = []
    for idx, cat in enumerate(TOOL_CATEGORIES, 1):
        with_cli = sum(1 for t in cat.tools if TOOL_CLI_MAPPING.get(t))
        mcp_only = len(cat.tools) - with_cli
        lines.append(f"### [{idx}] {cat.title}")
        lines.append("")
        lines.append("| CLI 入口 | MCP 工具 | 说明 |")
        lines.append("| --- | --- | --- |")
        for tool in cat.tools:
            entry = TOOL_CLI_MAPPING.get(tool)
            if entry is None:
                lines.append(f"| — | `{tool}` | MCP 专属，无 CLI 入口 |")
            else:
                path, note = entry
                lines.append(f"| `cw {path}` | `{tool}` | {note} |")
        lines.append("")
        lines.append(f"<!-- [{idx}]: {len(cat.tools)} 工具"
                     f"（{with_cli} 有 CLI 对应 / {mcp_only} MCP 专属） -->")
        lines.append("")
    return "\n".join(lines).rstrip("\n")


def _load_i18n_descs() -> dict:
    """加载 i18n/zh_CN.json（CLI 独有命令表的 desc_key 解析用）。"""
    path = os.path.join(_REPO_ROOT, "i18n", "zh_CN.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _i18n_lookup(data: dict, key: str) -> str:
    """按点分 key 查 i18n 描述；缺失返回空串。"""
    node = data
    for part in key.split("."):
        if not isinstance(node, dict):
            return ""
        node = node.get(part)
    return node if isinstance(node, str) else ""


def build_cli_only_doc() -> str:
    """生成「[1]-[17] 域内 CLI 独有命令」小节（cli-only 生成区间）。

    数据源 cli_only_in_shared_categories()：[1]-[17] 共享分类域内，
    无任何 MCP 工具映射的 CLI 顶层命令（说明列优先 default_desc，
    缺省回退解析 desc_key 的 i18n 文案）。
    """
    groups = _mcp_categories.cli_only_in_shared_categories(COMMAND_CATEGORIES)
    cat_by_key = {c.key: c for c in COMMAND_CATEGORIES}
    i18n = _load_i18n_descs()
    lines = [
        "### [1]-[17] 域内 CLI 独有命令（无任何 MCP 工具映射）",
        "",
        "| 分类 | 命令 | 说明 |",
        "| --- | --- | --- |",
    ]
    for cat_key, cmd_names in groups:
        cat = cat_by_key[cat_key]
        for name in cmd_names:
            info = next(c for c in cat.commands if c.name == name)
            desc = info.default_desc or _i18n_lookup(i18n, info.desc_key)
            lines.append(f"| {cat.title} | `cw {name}` | {desc} |")
    lines.append("")
    lines.append("> [18]-[21] CLI-only 分类（rollback / daemon / setup_install /"
                 " experiment）为有意的 CLI 独有能力域，不在此表统计。")
    return "\n".join(lines).rstrip("\n")


# ---------------------------------------------------------------------------
# 3. 文档 marker 区间读写
# ---------------------------------------------------------------------------

def _read_doc_sections(doc_path: str, begin_marker: str,
                       end_marker: str) -> Tuple[str, str, str]:
    """读取文档并按 marker 切成三段。

    Returns:
        (header, generated, footer)：
        header = 文件头到 BEGIN marker（含）；generated = marker 之间
        （不含 marker 行）；footer = END marker 之后（含）。
        marker 缺失时报错退出（需先手工插入 marker 再 --emit）。
    """
    with open(doc_path, "r", encoding="utf-8") as fh:
        text = fh.read()
    begin_idx = text.find(begin_marker)
    end_idx = text.find(end_marker)
    if begin_idx < 0 or end_idx < 0 or end_idx < begin_idx:
        sys.exit(
            f"{doc_path} 缺少生成区间 marker（{begin_marker} / "
            f"{end_marker}）；请先在目标区域两侧手工插入 marker 后重试")
    header = text[:begin_idx + len(begin_marker)]
    generated = text[begin_idx + len(begin_marker):end_idx].strip("\n")
    footer = text[end_idx:]
    return header, generated, footer


def _emit_doc(doc_path: str, body: str, label: str,
              begin_marker: str, end_marker: str) -> str:
    """把生成内容写入文档 marker 区间（header/footer 不动）。"""
    header, _, footer = _read_doc_sections(doc_path, begin_marker, end_marker)
    with open(doc_path, "w", encoding="utf-8", newline="") as fh:
        fh.write(header + "\n\n" + body + "\n\n" + footer)
    return f"已写回 {label}: {doc_path}"


def _check_doc(doc_path: str, body: str, label: str,
               begin_marker: str, end_marker: str,
               errors: List[str]) -> None:
    """--check 单文档比对：marker 区间 vs 内存生成（字节级）。"""
    _, generated, _ = _read_doc_sections(doc_path, begin_marker, end_marker)
    if generated.strip("\n") != body.strip("\n"):
        errors.append(f"{label}与真相源不一致（字节级）："
                      f"请运行 gen_category_overview.py --emit-{label} 刷新")


# ---------------------------------------------------------------------------
# 4. main
# ---------------------------------------------------------------------------

def main() -> int:
    args = sys.argv[1:]
    if "--emit-cli" in args:
        print(_emit_doc(_CLI_DOC, build_cli_overview(), "cli",
                        _OVERVIEW_BEGIN, _OVERVIEW_END))
        return 0
    if "--emit-mcp" in args:
        print(_emit_doc(_MCP_DOC, build_mcp_overview(), "mcp",
                        _OVERVIEW_BEGIN, _OVERVIEW_END))
        return 0
    if "--emit-mapping" in args:
        print(_emit_doc(_MCP_DOC, build_mapping_doc(), "mapping",
                        _MAPPING_BEGIN, _MAPPING_END))
        print(_emit_doc(_MCP_DOC, build_cli_only_doc(), "cli-only",
                        _CLI_ONLY_BEGIN, _CLI_ONLY_END))
        return 0
    if "--print-cli" in args:
        print(build_cli_overview())
        return 0
    if "--print-mcp" in args:
        print(build_mcp_overview())
        return 0
    if "--print-mapping" in args:
        print(build_mapping_doc())
        print()
        print(build_cli_only_doc())
        return 0
    if "--check" in args:
        errors: List[str] = []
        _check_doc(_CLI_DOC, build_cli_overview(), "cli 概览表",
                   _OVERVIEW_BEGIN, _OVERVIEW_END, errors)
        _check_doc(_MCP_DOC, build_mcp_overview(), "mcp 概览表",
                   _OVERVIEW_BEGIN, _OVERVIEW_END, errors)
        _check_doc(_MCP_DOC, build_mapping_doc(), "映射表",
                   _MAPPING_BEGIN, _MAPPING_END, errors)
        _check_doc(_MCP_DOC, build_cli_only_doc(), "CLI 独有命令表",
                   _CLI_ONLY_BEGIN, _CLI_ONLY_END, errors)
        if errors:
            print("CHECK FAILED:")
            for e in errors:
                print(f"  - {e}")
            return 1
        cli_total = sum(len(c.commands) for c in COMMAND_CATEGORIES)
        mcp_total = sum(len(c.tools) for c in TOOL_CATEGORIES)
        with_cli = sum(1 for v in TOOL_CLI_MAPPING.values() if v is not None)
        print(f"CHECK OK: cli_reference.md（{cli_total} 命令 / "
              f"{len(COMMAND_CATEGORIES)} 分类）与 mcp_tools.md（{mcp_total} 工具 / "
              f"{len(TOOL_CATEGORIES)} 分类）概览表、映射表（{with_cli} 有 CLI 对应 / "
              f"{mcp_total - with_cli} MCP 专属）、CLI 独有命令表均与真相源一致")
        return 0
    print(__doc__)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
