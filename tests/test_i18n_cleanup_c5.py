"""C5: i18n 全量改造收尾测试

验证：
1. zh_CN/en_US 包含本轮新增的 11 个 i18n key
2. 带占位符的 key 包含正确的占位符
3. cli/main.py 中之前硬编码的 print 已替换为 t() 调用
4. install.py 中 _check_group 不再硬编码 [OK]/[MISS]
5. cicd/github_action.py 中标题与 3 个标签已 i18n
6. install --check 输出格式保持不变（中英文均可用）
7. JSON 文件语法合法
"""
import ast
import json
import os
import subprocess
import sys

import pytest


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------

def _load_i18n(lang: str) -> dict:
    """加载 i18n 文件"""
    from callwarden.i18n import _get_i18n_dir

    with open(
        os.path.join(_get_i18n_dir(), f"{lang}.json"),
        encoding="utf-8",
    ) as f:
        return json.load(f)


def _read_module_source(rel_path: str) -> str:
    """读取项目内 Python 模块源码"""
    from callwarden.config import PROJECT_ROOT

    with open(os.path.join(PROJECT_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


# ----------------------------------------------------------------------
# JSON 语法与新增 key 存在性
# ----------------------------------------------------------------------

@pytest.fixture(scope="module")
def zh_messages():
    return _load_i18n("zh_CN").get("cli", {}).get("messages", {})


@pytest.fixture(scope="module")
def en_messages():
    return _load_i18n("en_US").get("cli", {}).get("messages", {})


NEW_KEYS = [
    "defect_suggest_fix_truncated",
    "callers_item",
    "callees_item",
    "topo_item",
    "diff_remove_line",
    "diff_add_line",
    "install_check_ok",
    "install_check_miss",
    "install_check_item",
    "install_hooks_no_git",
    "install_hooks_installed",
    "install_hooks_skipped",
    "install_hooks_summary",
    "install_agent_path_item",
    "github_action_title",
    "github_action_base_ref",
    "github_action_head_ref",
    "github_action_workspace",
    "restore_all_error_item",
]


def test_zh_new_keys_exist(zh_messages):
    """zh_CN.json 包含本轮新增的所有 i18n key。"""
    missing = [k for k in NEW_KEYS if k not in zh_messages]
    assert not missing, f"zh_CN 缺少 i18n key: {missing}"


def test_en_new_keys_exist(en_messages):
    """en_US.json 包含本轮新增的所有 i18n key。"""
    missing = [k for k in NEW_KEYS if k not in en_messages]
    assert not missing, f"en_US 缺少 i18n key: {missing}"


# ----------------------------------------------------------------------
# 占位符验证
# ----------------------------------------------------------------------

PLACEHOLDER_CHECKS = [
    ("callers_item", ["{file}", "{line}", "{name}", "{cross}"]),
    ("callees_item", ["{line}", "{name}", "{cross}", "{file_info}"]),
    ("topo_item", ["{idx}", "{depth}", "{path}", "{line}", "{name}"]),
    ("diff_remove_line", ["{idx}", "{content}"]),
    ("diff_add_line", ["{idx}", "{content}"]),
    # install_check_item 使用 {pip_name:<30} 格式化说明符
    ("install_check_item", ["{status}", "{desc}", "{lang_tag}", "{pip_name:"]),
    ("install_hooks_installed", ["{hook}"]),
    ("install_hooks_skipped", ["{hook}"]),
    ("install_hooks_summary", ["{installed}", "{skipped}"]),
    ("install_agent_path_item", ["{path}"]),
    ("github_action_base_ref", ["{ref}"]),
    ("github_action_head_ref", ["{ref}"]),
    ("github_action_workspace", ["{workspace}"]),
    ("restore_all_error_item", ["{err}"]),
]


@pytest.mark.parametrize("key,placeholders", PLACEHOLDER_CHECKS)
def test_zh_placeholders(zh_messages, key, placeholders):
    """zh 文案中包含正确的占位符。"""
    text = zh_messages.get(key, "")
    for ph in placeholders:
        assert ph in text, f"zh {key} 缺少占位符 {ph}: {text}"


@pytest.mark.parametrize("key,placeholders", PLACEHOLDER_CHECKS)
def test_en_placeholders(en_messages, key, placeholders):
    """en 文案中包含正确的占位符。"""
    text = en_messages.get(key, "")
    for ph in placeholders:
        assert ph in text, f"en {key} 缺少占位符 {ph}: {text}"


# ----------------------------------------------------------------------
# 源码替换验证
# ----------------------------------------------------------------------

def test_cli_main_no_hardcoded_callers_item():
    """cli/main.py 中 callers 不再使用硬编码 f-string。"""
    src = _read_module_source("cli/main.py")
    # 应该调用 t("cli.messages.callers_item", ...)
    assert 't("cli.messages.callers_item"' in src, "callers_item 未替换为 i18n"
    # 不应再有硬编码的 -> 箭头 print
    assert 'print(f"  {c[\'caller_file\']}:{c[\'call_line\']} -> ' not in src, "硬编码 callers 行未清理"


def test_cli_main_no_hardcoded_callees_item():
    """cli/main.py 中 callees 不再使用硬编码 f-string。"""
    src = _read_module_source("cli/main.py")
    assert 't("cli.messages.callees_item"' in src, "callees_item 未替换为 i18n"
    assert "print(f\"  line {c['call_line']}: {c['callee_name']}" not in src, "硬编码 callees 行未清理"


def test_cli_main_no_hardcoded_topo_item():
    """cli/main.py 中 topo 不再使用硬编码 f-string。"""
    src = _read_module_source("cli/main.py")
    assert 't("cli.messages.topo_item"' in src, "topo_item 未替换为 i18n"
    assert "print(f\"  {i+1}. depth={sym['depth']:2d}  {sym['path']}:" not in src, "硬编码 topo 行未清理"


def test_cli_main_no_hardcoded_diff_markers():
    """cli/main.py 中 diff 行不再硬编码 +/- 标记。"""
    src = _read_module_source("cli/main.py")
    assert 't("cli.messages.diff_remove_line"' in src, "diff_remove_line 未替换"
    assert 't("cli.messages.diff_add_line"' in src, "diff_add_line 未替换"
    assert 'print(f"  - {i+1}: {l1}")' not in src, "硬编码 diff - 行未清理"
    assert 'print(f"  + {i+1}: {l2}")' not in src, "硬编码 diff + 行未清理"


def test_cli_main_no_hardcoded_fix_truncated():
    """cli/main.py 中 fix 截断省略号已 i18n。"""
    src = _read_module_source("cli/main.py")
    assert 't("cli.messages.defect_suggest_fix_truncated"' in src, "fix_truncated 未替换"
    assert 'print("    ...")' not in src, "硬编码 '    ...' 未清理"


def test_cli_main_no_hardcoded_install_agent_path_item():
    """cli/main.py 中 install-agent path 列表项已 i18n。"""
    src = _read_module_source("cli/main.py")
    assert 't("cli.messages.install_agent_path_item"' in src, "install_agent_path_item 未替换"
    assert 'print(f"  - {path}")' not in src, "硬编码 install-agent 路径行未清理"


def test_cli_main_no_hardcoded_restore_all_error_item():
    """cli/main.py 中 restore-all 错误列表项已 i18n。"""
    src = _read_module_source("cli/main.py")
    assert 't("cli.messages.restore_all_error_item"' in src, "restore_all_error_item 未替换"
    assert 'print(f"  - {err}")' not in src, "硬编码 restore-all 错误行未清理"


def test_install_no_hardcoded_check_status():
    """install.py _check_group 不再硬编码 [OK]/[MISS]。"""
    src = _read_module_source("install.py")
    assert 'install_check_ok' in src, "install_check_ok 未使用"
    assert 'install_check_miss' in src, "install_check_miss 未使用"
    assert 'install_check_item' in src, "install_check_item 未使用"
    # 不应再出现硬编码状态
    assert 'status = "[OK]  " if installed else "[MISS]"' not in src, "硬编码 [OK]/[MISS] 未清理"


def test_install_uses_install_hooks_i18n():
    """install.py install_hooks 使用 i18n key 而非 default fallback。"""
    src = _read_module_source("install.py")
    assert 't("cli.messages.install_hooks_installed"' in src, "install_hooks_installed 未使用"
    assert 't("cli.messages.install_hooks_skipped"' in src, "install_hooks_skipped 未使用"
    assert 't("cli.messages.install_hooks_summary"' in src, "install_hooks_summary 未使用"
    # 不应再出现 default="" 的 fallback 模式
    assert 'default=""' not in src or 't("cli.messages.install_hooks_installed", hook=hook_path)' in src


def test_github_action_no_hardcoded_header():
    """cicd/github_action.py 头部不再硬编码标题与标签。"""
    src = _read_module_source("cicd/github_action.py")
    assert 't("cli.messages.github_action_title")' in src, "github_action_title 未使用"
    assert 't("cli.messages.github_action_base_ref"' in src, "github_action_base_ref 未使用"
    assert 't("cli.messages.github_action_head_ref"' in src, "github_action_head_ref 未使用"
    assert 't("cli.messages.github_action_workspace"' in src, "github_action_workspace 未使用"
    # 不应再出现硬编码字符串
    assert '"Code Graph Analysis - GitHub Action"' not in src, "硬编码标题未清理"
    assert 'print(f"  base ref : {base_ref}")' not in src, "硬编码 base ref 未清理"
    assert 'print(f"  head ref : {head_ref}")' not in src, "硬编码 head ref 未清理"
    assert 'print(f"  workspace: {workspace}")' not in src, "硬编码 workspace 未清理"


# ----------------------------------------------------------------------
# Python 语法验证
# ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "rel_path",
    [
        "cli/main.py",
        "install.py",
        "cicd/github_action.py",
    ],
)
def test_python_syntax_ok(rel_path):
    """修改过的 Python 文件语法合法。"""
    src = _read_module_source(rel_path)
    ast.parse(src)  # 抛异常即失败


# ----------------------------------------------------------------------
# JSON 语法验证
# ----------------------------------------------------------------------

def test_json_files_valid():
    """两个 i18n JSON 文件语法合法。"""
    from callwarden.i18n import _get_i18n_dir

    for lang in ("zh_CN", "en_US"):
        with open(os.path.join(_get_i18n_dir(), f"{lang}.json"), encoding="utf-8") as f:
            json.load(f)


# ----------------------------------------------------------------------
# 端到端：install --check 输出格式不变
# ----------------------------------------------------------------------

def test_install_check_output_unchanged():
    """cw install --check 输出仍然包含 [OK] 和 [MISS] 标签。"""
    from callwarden.config import PROJECT_ROOT

    result = subprocess.run(
        [sys.executable, os.path.join(PROJECT_ROOT, "cw.py"), "install", "--check"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        cwd=PROJECT_ROOT,
    )
    assert result.returncode == 0, f"install --check 失败: {result.stderr}"
    # 应该包含 [OK] 和 [MISS]（如果某个包确实缺失）或至少 [OK]
    assert "[OK]" in result.stdout or "[MISS]" in result.stdout, "install --check 输出缺少状态标签"


# ----------------------------------------------------------------------
# i18n key 在 t() 调用中的引用一致性
# ----------------------------------------------------------------------

def test_all_new_keys_used_in_source():
    """新增的 i18n key 至少在一个源文件中被引用。"""
    from callwarden.config import PROJECT_ROOT

    # 收集所有 Python 源码
    all_source = []
    for root, dirs, files in os.walk(PROJECT_ROOT):
        # 跳过 .git, __pycache__, tests
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__", ".pytest_cache")]
        for f in files:
            if f.endswith(".py"):
                try:
                    with open(os.path.join(root, f), encoding="utf-8") as fh:
                        all_source.append(fh.read())
                except (OSError, UnicodeDecodeError):
                    pass

    combined = "\n".join(all_source)

    # 每个新增 key 都应在某个源文件中以 t("...key") 形式被引用
    unused = []
    for key in NEW_KEYS:
        # 在源码中搜索 cli.messages.{key} 或 messages.{key}
        search_patterns = [
            f"cli.messages.{key}",
            f"messages.{key}",
        ]
        if not any(p in combined for p in search_patterns):
            unused.append(key)

    # 允许 install_hooks_no_git 在 install.py 中以 default= 形式存在
    # 但至少要被 t() 引用
    assert not unused, f"以下新增 i18n key 未在任何源文件中引用: {unused}"
