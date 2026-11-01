"""i18n 引用一致性门禁（backlog §25 · T-1793521395642-f45bc274；§26 升级）

漂移形态一（§25）：代码引用 catalog 中不存在的 key 且未传 ``default=`` 时，
``i18n.t()`` 会原样返回 key 字符串（见 ``i18n/__init__.py`` 的缺 key 分支），
用户在 zh_CN / en_US 下都会直接看到裸 key。

漂移形态二（§26）：带 ``default=`` 的调用虽不露裸 key，但 key 缺条目时
zh_CN 环境直接输出代码里的英文兜底文案，属于「缺翻译而非缺兜底」。

本门禁口径（§26 起）：**所有静态常量引用必须双语存在**，不再按 ``default=``
豁免。key 前缀映射：

- ``t(k)``             -> ``k``（原样）
- ``get_msg(k)``       -> ``cli.messages.k``
- ``get_error(k)``     -> ``errors.k``
- ``get_arg_help(k)``  -> ``cli.args.k``（``-`` 归一为 ``_``）

存在性判定必须复刻 ``t()`` 的 ``key.split(".")`` 逐层 ``dict.get`` 语义
（``_reach``），不能用扁平化成员判断：顶层含点号的字符串键（如
``"cli.messages.auto_setup_done"``）按扁平化集合会被误判为存在，实际
``t()`` 永远不可达（§26.2 实测 16 个）。``test_no_top_level_dotted_keys``
把这条结构不变量固化，防止死条目回流。
"""
import ast
import json
import os

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 只扫描本项目包内模块；排除第三方代码、测试与运行产物
SKIP_DIRS = {
    ".git", ".workbuddy", ".trae-cn", ".pytest_cache", "testcode", "tests",
    "node_modules", "__pycache__", "build", "dist", "runtime", "target",
    "vendor", "third_party", "i18n",
}

# 本卡补齐的 8 个 key（漂移清单，backlog §25.2）
REQUIRED_KEYS = [
    "cli.args.category",
    "cli.messages.semgrep_incremental_mode",
    "cli.messages.semgrep_incremental_done",
    "cli.messages.task_supersede_failed",
    "cli.messages.task_superseded_failed",
    "cli_fts_rebuild_done",
    "cli_fts_rebuild_mismatch_warning",
    "cli_fts_rebuild_failed",
]


def _load_catalog(lang: str) -> dict:
    with open(os.path.join(PROJECT_ROOT, "i18n", f"{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


def _flatten(data: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in data.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flatten(v, f"{path}."))
        else:
            out[path] = v
    return out


def _reach(data: dict, key: str):
    """复刻 ``i18n.t()`` 的查找语义：``key.split(".")`` 逐层 ``dict.get``。

    非 dict 中断即视为不可达；返回 None 表示该 key 在 catalog 中不存在。
    值不限定为字符串（catalog 中少数条目为列表，``t()`` 会 ``str()`` 返回）。
    """
    value = data
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


@pytest.fixture(scope="module")
def zh_catalog():
    return _load_catalog("zh_CN")


@pytest.fixture(scope="module")
def en_catalog():
    return _load_catalog("en_US")


def _resolve_key(func_name: str, raw: str):
    if func_name == "t":
        return raw
    if func_name == "get_msg":
        return f"cli.messages.{raw}"
    if func_name == "get_error":
        return f"errors.{raw}"
    if func_name == "get_arg_help":
        return f"cli.args.{raw.replace('-', '_')}"
    return None


def _iter_i18n_sources():
    """遍历项目内引用 i18n 的 Python 模块"""
    for dirpath, dirnames, filenames in os.walk(PROJECT_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for filename in filenames:
            if not filename.endswith(".py"):
                continue
            path = os.path.join(dirpath, filename)
            try:
                with open(path, encoding="utf-8") as f:
                    source = f.read()
            except (OSError, UnicodeDecodeError):
                continue
            if "i18n" not in source:
                continue
            yield os.path.relpath(path, PROJECT_ROOT).replace("\\", "/"), source


def _collect_references():
    """收集所有静态常量 key 引用（含带 ``default=`` 的调用）。"""
    refs = []
    for rel_path, source in _iter_i18n_sources():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            func_name = node.func.id
            if func_name not in ("t", "get_msg", "get_error", "get_arg_help"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            raw = node.args[0].value
            if not isinstance(raw, str):
                continue
            key = _resolve_key(func_name, raw)
            if key is None:
                continue
            refs.append((rel_path, node.lineno, func_name, key))
    return refs


@pytest.fixture(scope="module")
def references():
    return _collect_references()


def test_scanner_finds_references(references):
    """扫描口径失效自检：必须能收集到大量引用。"""
    assert references, "未发现任何 i18n 静态引用，扫描口径可能失效"
    assert len(references) > 100, f"引用数量异常偏少: {len(references)}"


def test_referenced_keys_exist_in_both_catalogs(references, zh_catalog, en_catalog):
    """所有静态常量 key 引用必须在 zh_CN / en_US 中可达（不豁免 default）。"""
    missing_zh = [
        f"{p}:{n} {k}" for p, n, _, k in references if _reach(zh_catalog, k) is None
    ]
    missing_en = [
        f"{p}:{n} {k}" for p, n, _, k in references if _reach(en_catalog, k) is None
    ]
    assert not missing_zh, f"zh_CN 缺少被引用的 key: {missing_zh}"
    assert not missing_en, f"en_US 缺少被引用的 key: {missing_en}"


def test_catalog_parity(zh_catalog, en_catalog):
    """两个语言 catalog 的 key 集合必须完全一致。"""
    zh_flat = _flatten(zh_catalog)
    en_flat = _flatten(en_catalog)
    only_zh = sorted(set(zh_flat) - set(en_flat))
    only_en = sorted(set(en_flat) - set(zh_flat))
    assert not only_zh, f"仅 zh_CN 存在的 key: {only_zh}"
    assert not only_en, f"仅 en_US 存在的 key: {only_en}"


@pytest.mark.parametrize("lang", ["zh_CN", "en_US"])
def test_no_top_level_dotted_keys(lang):
    """catalog 顶层不得存在含点号的字符串键（``t()`` 永远不可达的死条目）。"""
    data = _load_catalog(lang)
    dotted = sorted(k for k in data if "." in k)
    assert not dotted, (
        f"{lang}.json 顶层存在点号键（t() 不可达，应改为嵌套结构）: {dotted}"
    )


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_required_keys_translated(key, zh_catalog, en_catalog):
    """§25 补齐的 8 个 key 双语齐备，且值不等于 key 本身（确实翻译过）。"""
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        value = _reach(catalog, key)
        assert value is not None, f"{lang} 缺少 key: {key}"
        assert value != key, f"{lang} 的 {key} 值等于 key 本身（未翻译）"
