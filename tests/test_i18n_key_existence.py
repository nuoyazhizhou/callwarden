"""i18n 引用一致性门禁（backlog §25 · T-1793521395642-f45bc274）

漂移形态：代码引用 catalog 中不存在的 key 且未传 ``default=`` 时，
``i18n.t()`` 会原样返回 key 字符串（见 ``i18n/__init__.py`` 的缺 key 分支），
用户在 zh_CN / en_US 下都会直接看到裸 key。

本门禁把「引用即存在」固化为 AST 静态扫描口径：

- ``t(k)``             -> ``k``（原样）
- ``get_msg(k)``       -> ``cli.messages.k``
- ``get_error(k)``     -> ``errors.k``
- ``get_arg_help(k)``  -> ``cli.args.k``（``-`` 归一为 ``_``）

带 ``default=`` 的宽容调用（含 ``t(k, default)`` 第 2 位置参数）不参与断言，
因为它们缺 key 时会回退到兜底文案而非裸 key。
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


@pytest.fixture(scope="module")
def zh_catalog():
    return _flatten(_load_catalog("zh_CN"))


@pytest.fixture(scope="module")
def en_catalog():
    return _flatten(_load_catalog("en_US"))


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
    """收集所有无 default 兜底的静态 key 引用"""
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
            has_default = any(kw.arg == "default" for kw in node.keywords)
            if func_name == "t" and len(node.args) >= 2:
                has_default = True
            if has_default:
                continue
            refs.append((rel_path, node.lineno, func_name, key))
    return refs


@pytest.fixture(scope="module")
def references():
    return _collect_references()


def test_scanner_finds_references(references):
    """扫描口径失效自检：必须能收集到大量引用。"""
    assert references, "未发现任何无 default 的 i18n 引用，扫描口径可能失效"
    assert len(references) > 100, f"引用数量异常偏少: {len(references)}"


def test_referenced_keys_exist_in_both_catalogs(references, zh_catalog, en_catalog):
    """所有无 default 兜底的 key 引用必须在 zh_CN / en_US 中存在。"""
    missing_zh = [f"{p}:{n} {k}" for p, n, _, k in references if k not in zh_catalog]
    missing_en = [f"{p}:{n} {k}" for p, n, _, k in references if k not in en_catalog]
    assert not missing_zh, f"zh_CN 缺少被引用的 key: {missing_zh}"
    assert not missing_en, f"en_US 缺少被引用的 key: {missing_en}"


def test_catalog_parity(zh_catalog, en_catalog):
    """两个语言 catalog 的 key 集合必须完全一致。"""
    only_zh = sorted(set(zh_catalog) - set(en_catalog))
    only_en = sorted(set(en_catalog) - set(zh_catalog))
    assert not only_zh, f"仅 zh_CN 存在的 key: {only_zh}"
    assert not only_en, f"仅 en_US 存在的 key: {only_en}"


@pytest.mark.parametrize("key", REQUIRED_KEYS)
def test_required_keys_translated(key, zh_catalog, en_catalog):
    """本卡补齐的 8 个 key 双语齐备，且值不等于 key 本身（确实翻译过）。"""
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        assert key in catalog, f"{lang} 缺少 key: {key}"
        assert catalog[key] != key, f"{lang} 的 {key} 值等于 key 本身（未翻译）"
