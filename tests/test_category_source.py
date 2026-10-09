"""分类真相源校验测试（cli-mcp-surface-audit Phase 1d）。

覆盖两组不变量：
1. CLI 侧：cli/categories.py 的 COMMAND_CATEGORIES 恰好覆盖全部顶层命令
   （cli.main._SUBCOMMANDS 79 个 + cw.py standalone 3 个 + setup + daemon
   = 84 个），无重复、无遗漏——新增命令漏归类在此拦截；
2. MCP 侧：server/tools/_categories.py 的 17 个 TOOL_CATEGORIES 恰好划分
   server/tools/*.py 注册的全部 @mcp.tool() 工具（243 个），无重复、
   无遗漏——新增工具漏归类在此拦截；
3. 同构校验：MCP 分类的 cli_category 引用必须存在于 CLI 分类 key 中。
"""

import importlib.util
import os
import re
import sys

# 确保项目根目录在 path 中
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.cli import main as cli_main
from callwarden.cli.categories import COMMAND_CATEGORIES, all_command_names
from callwarden.server.tools import _categories as tool_categories_mod
from callwarden.server.tools._categories import TOOL_CATEGORIES, all_tool_names

# cw.py standalone 入口 + cli/main.py 单独分发的入口（不在 _SUBCOMMANDS 中）
_EXTRA_TOP_LEVEL = {"install", "server", "test", "setup", "daemon"}


# ============================================
# 1. CLI 分类完整性
# ============================================


def test_cli_categories_cover_all_top_level_commands():
    """COMMAND_CATEGORIES 恰好覆盖全部顶层命令（84 = 79 + 3 + setup + daemon）"""
    categorized = set(all_command_names())
    expected = set(cli_main._SUBCOMMANDS) | _EXTRA_TOP_LEVEL
    missing = expected - categorized
    extra = categorized - expected
    assert not missing, f"以下顶层命令未归类: {sorted(missing)}"
    assert not extra, f"以下命令不在 dispatch 入口集合中（幻影条目）: {sorted(extra)}"


def test_cli_categories_no_duplicate():
    """每个顶层命令恰好出现一次（无跨类重复）"""
    names = all_command_names()
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f"以下命令被重复归类: {sorted(dupes)}"


def test_cli_categories_command_count():
    """顶层命令总数为 84（数量变化时须同步更新本断言与文档）"""
    assert len(all_command_names()) == 84


def test_cli_categories_desc_key_no_placeholder_risk():
    """desc_key 引用的 i18n key 在 zh_CN.json 中存在（防 key 拼写漂移）"""
    import json
    i18n_path = os.path.join(_PKG_PARENT, "i18n", "zh_CN.json")
    with open(i18n_path, encoding="utf-8") as f:
        data = json.load(f)

    def _has_key(key: str) -> bool:
        node = data
        for part in key.split("."):
            if not isinstance(node, dict):
                return False
            node = node.get(part)
        return node is not None

    missing = []
    for cat in COMMAND_CATEGORIES:
        if cat.title_key and not _has_key(cat.title_key):
            missing.append(cat.title_key)
        for cmd in cat.commands:
            if cmd.desc_key and not _has_key(cmd.desc_key):
                missing.append(cmd.desc_key)
    assert not missing, f"以下 i18n key 不存在（拼写漂移）: {sorted(set(missing))}"


def test_cli_categories_metadata_complete():
    """每个分类/命令的元数据字段非空（key/title/scope/desc）"""
    for cat in COMMAND_CATEGORIES:
        assert cat.key and cat.title and cat.scope, f"分类元数据缺失: {cat.key}"
        for cmd in cat.commands:
            assert cmd.name, "命令 name 为空"
            # desc_key 与 default_desc 至少一个非空（desc_key 的存在性
            # 由 test_cli_categories_desc_key_no_placeholder_risk 保证）
            assert cmd.desc_key or cmd.default_desc, (
                f"命令 {cmd.name} 既无 desc_key 也无 default_desc，help 必裸 key"
            )


# ============================================
# 2. MCP 分类完整性（对照 server/tools 注册面）
# ============================================


def _load_gen_route_matrix():
    """按路径加载 scripts/gen_route_matrix.py（scripts 非包，用 importlib）"""
    path = os.path.join(_PKG_PARENT, "scripts", "gen_route_matrix.py")
    spec = importlib.util.spec_from_file_location("_gen_route_matrix", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _registered_tool_names():
    """从 server/tools/*.py 源码提取 @mcp.tool() 注册的工具名集合"""
    names = set()
    for mod_name in _load_gen_route_matrix().TOOL_MODULES:
        path = os.path.join(
            _PKG_PARENT, "server", "tools", f"{mod_name}.py")
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        names.update(re.findall(r"@mcp\.tool\([^)]*\)\s*\n\s*def (\w+)\(", src))
    return names


def test_mcp_categories_exactly_partition_registered_tools():
    """17 分类恰好划分 server/tools 注册的全部工具（无重复、无遗漏）"""
    categorized = all_tool_names()
    dupes = {n for n in categorized if categorized.count(n) > 1}
    assert not dupes, f"以下工具被重复归类: {sorted(dupes)}"

    registered = _registered_tool_names()
    missing = registered - set(categorized)
    extra = set(categorized) - registered
    assert not missing, f"以下已注册工具未归类: {sorted(missing)}"
    assert not extra, f"以下归类工具未在 server/tools 注册（幻影条目）: {sorted(extra)}"


def test_mcp_tool_count_243():
    """MCP 工具总数为 243（数量变化时须同步更新文档与本断言）"""
    assert len(all_tool_names()) == 243, (
        f"工具总数应为 243，实际 {len(all_tool_names())}"
    )


def test_mcp_category_count_17():
    """MCP 分类数为 17"""
    assert len(TOOL_CATEGORIES) == 17


# ============================================
# 3. CLI ↔ MCP 分类同构校验
# ============================================


def test_mcp_cli_category_refs_exist():
    """MCP 分类的 cli_category 引用必须存在于 CLI 分类 key 中"""
    cli_keys = {cat.key for cat in COMMAND_CATEGORIES}
    dangling = [cat.cli_category for cat in TOOL_CATEGORIES
                if cat.cli_category not in cli_keys]
    assert not dangling, f"MCP 分类引用了不存在的 CLI 分类 key: {sorted(dangling)}"


def test_mcp_category_keys_subset_of_cli():
    """MCP 17 个分类 key 与 CLI 分类 key 同构（一一对应）"""
    cli_keys = {cat.key for cat in COMMAND_CATEGORIES}
    mcp_keys = [cat.key for cat in TOOL_CATEGORIES]
    assert len(mcp_keys) == len(set(mcp_keys)), "MCP 分类 key 有重复"
    not_in_cli = [k for k in mcp_keys if k not in cli_keys]
    assert not not_in_cli, f"以下 MCP 分类 key 在 CLI 侧无同名分类: {not_in_cli}"


def test_cli_only_categories_flagged():
    """cli_only 标记的分类必须真的无 MCP 同构分类"""
    mcp_keys = {cat.key for cat in TOOL_CATEGORIES}
    for cat in COMMAND_CATEGORIES:
        if cat.cli_only:
            assert cat.key not in mcp_keys, (
                f"分类 {cat.key} 标记 cli_only 但 MCP 侧存在同构分类"
            )


# ============================================
# 4. MCP 工具 → CLI 入口映射校验（cli-mcp-surface-audit Phase 2）
# ============================================


def _extract_cli_subcommands():
    """从 cli/main.py 与 cli/daemon_commands.py 静态提取 add_parser 路径。

    Returns:
        {顶层命令: {"子命令路径", ...}}，路径不含顶层命令名，
        如 {"rule": {"candidate", "candidate create", "list", ...},
            "daemon": {"ping", "mount register", ...}}。

    提取方式（覆盖两个文件的全部 argparse 域）：
    - 域声明：``prog="cw xxx"`` 切换当前域；
    - 子命令：``X = P.add_parser("name")`` / ``P.add_parser("name")``
      （含跨行写法 ``add_parser(\\n "xxx"``）；
    - 嵌套：``S = X.add_subparsers()`` 后 ``S.add_parser("y")`` 得
      ``"x y"`` 三级路径。task/git/gc/semgrep/coverage 等映射涉及的
      域均在提取覆盖范围内（见 test_mapping_subcommands_exist_static）。
    """
    token_re = re.compile(
        r'prog=["\']cw ([\w-]+)["\']'                      # 域声明
        r"|(\w+)\s*=\s*(\w+)\.add_subparsers\("            # subparsers 容器赋值
        r"|(?:(\w+)\s*=\s*)?(\w+)\.add_parser\(\s*[\"']([\w-]+)[\"']"
    )
    result = {}
    for rel in (os.path.join("cli", "main.py"),
                os.path.join("cli", "daemon_commands.py")):
        with open(os.path.join(_PKG_PARENT, rel), encoding="utf-8") as fh:
            src = "".join(
                line for line in fh if not line.lstrip().startswith("#"))
        current_cmd = None
        var_path = {}   # add_parser 赋值变量 -> 相对路径 tuple
        sub_owner = {}  # subparsers 变量 -> 父 add_parser 变量
        for m in token_re.finditer(src):
            if m.group(1):
                current_cmd = m.group(1)
                result.setdefault(current_cmd, set())
            elif m.group(2):
                sub_owner[m.group(2)] = m.group(3)
            elif m.group(5):
                parent, name = m.group(5), m.group(6)
                if parent in sub_owner and sub_owner[parent] in var_path:
                    path = var_path[sub_owner[parent]] + (name,)
                else:
                    path = (name,)
                if m.group(4):
                    var_path[m.group(4)] = path
                if current_cmd:
                    result.setdefault(current_cmd, set()).add(" ".join(path))
    return result


class TestToolCliMapping:
    """TOOL_CLI_MAPPING（243 工具 → CLI 入口全量映射）不变量校验"""

    def test_mapping_keys_exactly_cover_all_tools(self):
        """映射键集 == all_tool_names()（243 恰好覆盖，无多无漏）"""
        keys = set(tool_categories_mod.TOOL_CLI_MAPPING)
        expected = set(all_tool_names())
        missing = expected - keys
        extra = keys - expected
        assert not missing, f"以下工具缺映射条目: {sorted(missing)}"
        assert not extra, f"以下映射键不是已归类工具: {sorted(extra)}"

    def test_mapping_top_level_commands_exist(self):
        """每条非 None 值的顶层命令 ∈ all_command_names()（84 个）"""
        all_cmds = set(all_command_names())
        bad = [
            (tool, entry[0])
            for tool, entry in tool_categories_mod.TOOL_CLI_MAPPING.items()
            if entry is not None and entry[0].split()[0] not in all_cmds
        ]
        assert not bad, f"以下映射引用了不存在的顶层命令: {bad}"

    def test_mapping_value_shape(self):
        """每条非 None 值是 (path, note) 元组且 path 非空 str、note 是 str"""
        for tool, entry in tool_categories_mod.TOOL_CLI_MAPPING.items():
            assert entry is None or (
                isinstance(entry, tuple) and len(entry) == 2
                and isinstance(entry[0], str) and entry[0].strip()
                and isinstance(entry[1], str)
            ), f"{tool} 的映射值形状非法: {entry!r}"
            if entry is not None:
                assert not entry[0].startswith("cw "), (
                    f"{tool}: 路径不应含 cw 前缀: {entry[0]!r}")

    def test_cli_only_in_shared_categories_valid(self):
        """cli_only_in_shared_categories() 的分类 key 均为 cli_only=False，
        且命令都属于该分类"""
        cmd_owner = {c.name: cat.key
                     for cat in COMMAND_CATEGORIES for c in cat.commands}
        cli_only_keys = {cat.key for cat in COMMAND_CATEGORIES if cat.cli_only}
        for cat_key, cmds in tool_categories_mod.cli_only_in_shared_categories(
                COMMAND_CATEGORIES):
            assert cat_key not in cli_only_keys, (
                f"CLI 独有结果混入 cli_only 分类: {cat_key}")
            for name in cmds:
                assert cmd_owner.get(name) == cat_key, (
                    f"命令 {name} 不属于分类 {cat_key}")

    def test_mapping_subcommands_exist_static(self):
        """子命令级静态校验：映射中每个多段路径在 main.py/daemon_commands.py
        的 add_parser 注册集合中存在（防幻影 CLI 子命令，如曾经的
        `guardrail check-edit` / `git symbol-history`）"""
        subs = _extract_cli_subcommands()
        bad = []
        for tool, entry in tool_categories_mod.TOOL_CLI_MAPPING.items():
            if entry is None:
                continue
            parts = entry[0].split()
            top, sub = parts[0], " ".join(parts[1:])
            if len(parts) > 1:
                if top not in subs:
                    bad.append((tool, f"{top} 域未提取到子命令"))
                elif sub not in subs[top]:
                    bad.append((tool, f"cw {entry[0]}"))
        assert not bad, f"以下映射的子命令在 CLI 侧不存在: {bad}"

    def test_extraction_covers_core_domains(self):
        """提取器覆盖映射涉及的全部子命令级域（提取器失效时兜底报警）"""
        subs = _extract_cli_subcommands()
        # [1]-[12] 涉及子命令的核心域（含三级路径的 rule/gc）
        for cmd, expect in {
            "workspace": {"list", "register", "set", "delete"},
            "rule": {"candidate", "candidate create", "list", "sync"},
            "guardrail": {"scan", "rules"},
            "task": {"create", "next", "show", "governance-projection"},
            "audit": {"verify", "rotate-key", "keys"},
            "bootstrap": {"status"},
            "git": {"import", "log", "show", "stats"},
            "semgrep": {"scan", "list", "stats"},
            "defect": {"search", "suggest", "learn", "stats"},
            "coverage": {"import", "fn", "uncovered", "test"},
            "gc": {"policy", "policy show", "policy set", "retention",
                   "archive-inspect", "audit-show"},
            "clone": {"detect", "list", "stats", "clear"},
            "toolchain": {"show", "list", "list-bound"},
            "build-context": {"show", "list", "edges"},
            "collab": {"publish", "verdict", "reveal", "gate-trigger"},
            "dependency": {"inspect", "list", "cycle", "explain",
                           "provider-select"},
            "lease": {"acquire", "renew", "release", "status", "list"},
            "assignment": {"create", "show", "revoke"},
            "identity": {"revoke"},
            "daemon": {"ping", "backup", "mount register"},
        }.items():
            got = subs.get(cmd, set())
            missing = expect - got
            assert not missing, (
                f"add_parser 提取器漏掉 {cmd} 域子命令: {sorted(missing)}"
                f"（提取到: {sorted(got)}）")
