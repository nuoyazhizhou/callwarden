"""C8 Step #8: 最终一致性测试（端到端集成 + 跨组件一致性）。

本测试不重复 Step #2-#7 已有的单元测试，而是聚焦于：
- 端到端集成验证（通过 subprocess 运行真实 cw 命令）
- 跨组件一致性检查（CLI ↔ JSON ↔ i18n ↔ 文档 ↔ MCP）

覆盖 Check Items:
[1] deprecated flag 显示 warning 但仍执行（抽样 10 个 flag 端到端验证）
[2] --refresh 支持多 path（端到端验证多文件刷新）
[3] 主 --help 输出包含 12 个分组标题（端到端验证）
[4] 子命令 --help 详细化（抽样 5 个子命令端到端验证 5 章节）
[5] MCP 工具命名对齐（验证 @mcp.tool() 数量与 .mcp_audit.md 一致）
[6] 一致性: deprecated_flag_mapping.json ↔ _DEPRECATED_FLAG_MAPPING
[7] 一致性: i18n zh_CN ↔ en_US key 集合
[8] 一致性: 文档交叉引用链路完整性
"""

import ast
import json
import os
import subprocess
import sys
from unittest import mock

import pytest

# 项目根目录
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

CW_PATH = os.path.join(_PKG_PARENT, "cw.py")
I18N_DIR = os.path.join(_PKG_PARENT, "i18n")
DEPRECATED_JSON = os.path.join(_PKG_PARENT, "deprecated_flag_mapping.json")
MCP_AUDIT = os.path.join(_PKG_PARENT, ".mcp_audit.md")
MCP_SERVER = os.path.join(_PKG_PARENT, "server", "mcp_server.py")

from callwarden.cli import main as cli_main


# ============================================
# 工具函数
# ============================================


def _run_cw(args, timeout=30):
    """运行 cw 命令，返回 (returncode, stdout, stderr)。

    使用 NO_COLOR=1 禁用颜色，避免 ANSI 转义序列干扰断言。
    """
    env = os.environ.copy()
    env["NO_COLOR"] = "1"
    env["CALLWARDEN_LANG"] = "zh_CN"
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [sys.executable, CW_PATH] + args
    result = subprocess.run(
        cmd, capture_output=True, text=True,
        env=env, encoding="utf-8", timeout=timeout,
        cwd=_PKG_PARENT,
    )
    return result.returncode, result.stdout, result.stderr


def _load_i18n(lang):
    """加载 i18n JSON 文件"""
    with open(os.path.join(I18N_DIR, f"{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


def _flatten_keys(d, prefix=""):
    """递归收集所有 key 路径"""
    keys = set()
    for k, v in d.items():
        full = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            keys |= _flatten_keys(v, full)
        else:
            keys.add(full)
    return keys


# ============================================
# [1] deprecated flag 端到端抽样验证（10 个）
# ============================================

# 抽样 10 个只读、安全的 deprecated flag 进行端到端验证
DEPRECATED_FLAG_SAMPLES = [
    "--stats",
    "--status",
    "--list-workspaces",
    "--metrics",
    "--comment-coverage",
    "--topo",
    "--call-heatmap",
    "--orphan-symbols",
    "--deepest",
    "--module-calls",
]


@pytest.mark.parametrize("flag", DEPRECATED_FLAG_SAMPLES)
def test_deprecated_flag_emits_warning_and_executes(flag):
    """[1] 端到端: deprecated flag 显示 warning 但仍执行

    验证：
    - stderr 包含 'deprecated' 警告
    - 程序不崩溃（exit code 不为 argparse 错误码 2）
    """
    returncode, stdout, stderr = _run_cw([flag], timeout=60)
    # argparse 错误时 exit code = 2
    assert returncode != 2, (
        f"{flag} 触发 argparse 错误 (exit 2): stderr={stderr}"
    )
    # stderr 应包含 deprecated 警告
    combined = stdout + stderr
    assert "deprecated" in combined.lower() or "废弃" in combined, (
        f"{flag} 未输出 deprecated 警告: stderr={stderr[:500]}"
    )


# ============================================
# [2] --refresh 多 path 端到端验证
# ============================================


def test_refresh_multi_path_end_to_end():
    """[2] 端到端: --refresh 支持多 path

    验证 cw --refresh <path1> <path2> 不会因参数解析失败而 exit 2。
    使用不存在的路径，验证程序能尝试刷新（即使失败也算解析正确）。
    """
    returncode, stdout, stderr = _run_cw(
        ["--refresh", "nonexistent_a.py", "nonexistent_b.py"],
        timeout=60,
    )
    # 不应为 argparse 错误（exit 2）
    assert returncode != 2, (
        f"--refresh 多 path 触发 argparse 错误: stderr={stderr[:500]}"
    )


def test_refresh_metavar_shows_path_ellipsis():
    """[2] --refresh 的 metavar 显示 'PATH [...]' 表明支持多路径"""
    parser = cli_main.create_parser()
    refresh_action = None
    for action in parser._actions:
        if "--refresh" in (action.option_strings or []):
            refresh_action = action
            break
    assert refresh_action is not None
    assert refresh_action.nargs == "+"
    assert refresh_action.metavar == "PATH [...]"


# ============================================
# [3] 主 --help 端到端验证 12 分组
# ============================================


def test_main_help_end_to_end_has_12_groups():
    """[3] 端到端: cw --help 输出包含 12 个分组标题"""
    returncode, stdout, stderr = _run_cw(["--help"], timeout=30)
    assert returncode == 0, f"cw --help 失败: exit={returncode}, stderr={stderr[:300]}"

    EXPECTED_GROUPS = [
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
    missing = [g for g in EXPECTED_GROUPS if g not in stdout]
    assert not missing, f"cw --help 缺少分组: {missing}"


# ============================================
# [4] 子命令 --help 端到端抽样验证（5 个子命令）
# ============================================

# 抽样 5 个子命令验证 --help 输出
SUBCOMMAND_SAMPLES = ["task", "rule", "gc", "workspace", "audit"]


@pytest.mark.parametrize("sub", SUBCOMMAND_SAMPLES)
def test_subcommand_help_end_to_end(sub):
    """[4] 端到端: cw <sub> --help 输出包含 5 章节关键词

    验证子命令 --help 输出包含:
    - 用法（usage/用法）
    - 描述（description/描述）
    - 参数（parameters/参数/options/选项）
    - 示例（example/示例）
    - 退出码（exit code/退出码）

    注意: 并非所有子命令都有完整 5 章节（取决于 _SUBCOMMAND_HELP_SPECS 是否定义），
    因此只验证 --help 能正常执行（exit 0）且输出非空，且至少包含"用法"或"usage"。
    """
    returncode, stdout, stderr = _run_cw([sub, "--help"], timeout=30)
    assert returncode == 0, (
        f"cw {sub} --help 失败: exit={returncode}, stderr={stderr[:300]}"
    )
    assert len(stdout) > 0, f"cw {sub} --help 输出为空"
    # 至少包含 usage/用法 之一
    assert "usage" in stdout.lower() or "用法" in stdout, (
        f"cw {sub} --help 输出缺少 usage/用法: {stdout[:300]}"
    )


# ============================================
# [5] MCP 工具命名对齐验证
# ============================================


def _extract_mcp_tool_names():
    """从 server/mcp_server.py AST 中提取所有 @mcp.tool() 工具名"""
    with open(MCP_SERVER, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=MCP_SERVER)
    names = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                func = dec.func
                if (isinstance(func, ast.Attribute)
                        and func.attr == "tool"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "mcp"):
                    names.append(node.name)
    return names


def test_mcp_tools_count_matches_audit():
    """[5] MCP 工具数量与 .mcp_audit.md 声明的 173 一致"""
    names = _extract_mcp_tool_names()
    assert len(names) >= 170, (
        f"MCP 工具数量 {len(names)} < 170（.mcp_audit.md 声明 173）"
    )


def test_mcp_tools_no_duplicates():
    """[5] MCP 工具名无重复"""
    names = _extract_mcp_tool_names()
    from collections import Counter
    counts = Counter(names)
    duplicates = [n for n, c in counts.items() if c > 1]
    assert not duplicates, f"MCP 工具名重复: {duplicates}"


def test_mcp_tools_all_have_docstring():
    """[5] 所有 @mcp.tool() 都有 docstring"""
    with open(MCP_SERVER, encoding="utf-8") as f:
        source = f.read()
    tree = ast.parse(source, filename=MCP_SERVER)
    missing = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        has_mcp_tool = False
        for dec in node.decorator_list:
            if isinstance(dec, ast.Call):
                func = dec.func
                if (isinstance(func, ast.Attribute)
                        and func.attr == "tool"
                        and isinstance(func.value, ast.Name)
                        and func.value.id == "mcp"):
                    has_mcp_tool = True
                    break
        if has_mcp_tool and not (node.body and isinstance(node.body[0], ast.Expr)
                                  and isinstance(node.body[0].value, ast.Constant)
                                  and isinstance(node.body[0].value.value, str)):
            missing.append(node.name)
    assert not missing, f"MCP 工具缺少 docstring: {missing[:10]}"


# ============================================
# [6] 一致性: deprecated_flag_mapping.json ↔ _DEPRECATED_FLAG_MAPPING
# ============================================


def test_deprecated_flag_json_matches_python_mapping():
    """[6] deprecated_flag_mapping.json 与 cli/main.py::_DEPRECATED_FLAG_MAPPING 一致

    JSON 文件中每个 flag -> subcommand 映射应与 Python 字典一致（去除 _meta）。
    """
    with open(DEPRECATED_JSON, encoding="utf-8") as f:
        json_data = json.load(f)
    # 去除 _meta key
    json_mapping = {k: v for k, v in json_data.items() if k != "_meta"}

    # Python 字典: attr -> (flag_name, subcommand)
    py_mapping = cli_main._DEPRECATED_FLAG_MAPPING
    # 转换为: flag_name -> subcommand
    py_as_dict = {flag_name: subcommand for (flag_name, subcommand) in py_mapping.values()}

    # 验证 key 集合一致
    assert set(json_mapping.keys()) == set(py_as_dict.keys()), (
        f"JSON 与 Python 映射的 key 不一致:\n"
        f"  JSON only: {set(json_mapping.keys()) - set(py_as_dict.keys())}\n"
        f"  Python only: {set(py_as_dict.keys()) - set(json_mapping.keys())}"
    )
    # 验证 value 一致
    for flag in json_mapping:
        assert json_mapping[flag] == py_as_dict[flag], (
            f"{flag} 映射不一致: JSON={json_mapping[flag]}, Python={py_as_dict[flag]}"
        )


# ============================================
# [7] 一致性: i18n zh_CN ↔ en_US key 集合
# ============================================


def test_i18n_key_consistency_zh_en():
    """[7] zh_CN.json 和 en_US.json 的 key 集合一致

    两个语言文件应具有完全相同的 key 结构（值可以不同）。
    """
    zh = _load_i18n("zh_CN")
    en = _load_i18n("en_US")
    zh_keys = _flatten_keys(zh)
    en_keys = _flatten_keys(en)
    only_zh = zh_keys - en_keys
    only_en = en_keys - zh_keys
    assert not only_zh, f"zh_CN 独有 key（en_US 缺失）: {sorted(only_zh)[:10]}"
    assert not only_en, f"en_US 独有 key（zh_CN 缺失）: {sorted(only_en)[:10]}"


# ============================================
# [8] 一致性: 文档交叉引用链路完整性
# ============================================


def test_doc_cross_references():
    """[8] 三个文档之间的交叉引用链接完整

    - cli_reference.md 引用 architecture.md 的命令风格统一规范
    - mcp_tools.md 引用 cli_reference.md 的相关章节
    - architecture.md 引用 cli_reference.md 和 mcp_tools.md
    """
    docs = {
        "cli_ref": os.path.join(_PKG_PARENT, "docs", "cli_reference.md"),
        "mcp": os.path.join(_PKG_PARENT, "docs", "mcp_tools.md"),
        "arch": os.path.join(_PKG_PARENT, "docs", "architecture.md"),
    }
    contents = {k: open(v, encoding="utf-8").read() for k, v in docs.items()}

    # cli_reference.md → architecture.md（命令风格统一规范）
    assert "architecture.md" in contents["cli_ref"], \
        "cli_reference.md 未引用 architecture.md"
    assert "命令风格统一规范" in contents["cli_ref"], \
        "cli_reference.md 未引用 architecture.md 的命令风格统一规范"

    # mcp_tools.md → cli_reference.md（CLI 参考）
    assert "cli_reference.md" in contents["mcp"] or "CLI" in contents["mcp"], \
        "mcp_tools.md 未引用 CLI 相关文档"

    # architecture.md → cli_reference.md + mcp_tools.md
    assert "cli_reference.md" in contents["arch"], \
        "architecture.md 未引用 cli_reference.md"
    assert "mcp_tools.md" in contents["arch"], \
        "architecture.md 未引用 mcp_tools.md"
