"""i18n 占位符一致性门禁（backlog §27 · T-1793540468063-993d3638）

§25/§26 已收口「引用即存在」与「default 兜底缺失」。本门禁覆盖第三类漂移：
**catalog 值含占位符，但调用未提供同名 kwargs**。

``i18n.t()`` 用 ``value.format(**kwargs)`` 替换占位符；缺参时抛 ``KeyError``，
被 ``except (KeyError, ValueError, IndexError)`` 吞掉后原样返回 catalog 值，
用户直接看到裸 ``{dir}`` / ``{error}`` / ``{id}`` 等。key 存在性门禁无法发现
这类缺口——key 存在，坏的是调用与 catalog 的形状契约。

判定口径：

- 只检查**静态字面量 key + 显式关键字实参**的调用；
- 带 ``**kwargs`` 展开的调用无法静态判定，豁免；
- 占位符只认 ``{ident}`` / ``{ident!r}`` / ``{ident:spec}``；值内若含非标识符
  花括号（JSON 示例、整段脚本），``format()`` 本就抛 ``ValueError`` 并原样返回，
  不纳入占位符口径；
- 两个 catalog 各判一遍，任一缺参即失败。
"""
import ast
import json
import os
import re

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {
    ".git", ".workbuddy", ".trae-cn", ".pytest_cache", "testcode", "tests",
    "node_modules", "__pycache__", "build", "dist", "runtime", "target",
    "vendor", "third_party", "i18n",
}

# 完整占位符 token：{ident} / {ident!r} / {ident:spec}
FIELD_TOKEN = re.compile(
    r"\{[A-Za-z_][A-Za-z0-9_]*(?:![rsa])?(?::[^{}]*)?\}"
)
ANY_BRACE = re.compile(r"{[^{}]*}")

# 本卡拆分的 7 变体 × 2 族（backlog §27.3）
VARIANT_FIELDS = {
    "cli.messages.install_semgrep_prefetch_skip_dir": {"dir"},
    "cli.messages.install_semgrep_prefetch_skip_error": {"error"},
    "cli.messages.install_semgrep_prefetch_skip_binary": set(),
    "cli.messages.install_semgrep_prefetch_skip_permission": {"error"},
    "cli.messages.install_semgrep_prefetch_skip_oserror": {"error"},
    "cli.messages.install_semgrep_prefetch_skip_unexpected": {"error"},
    "cli.messages.install_semgrep_prefetch_skip_copy": {"error"},
    "cli.messages.semgrep_rules_prefetch_skip_dir": {"dir"},
    "cli.messages.semgrep_rules_prefetch_skip_error": {"error"},
    "cli.messages.semgrep_rules_prefetch_skip_binary": set(),
    "cli.messages.semgrep_rules_prefetch_skip_permission": {"error"},
    "cli.messages.semgrep_rules_prefetch_skip_oserror": {"error"},
    "cli.messages.semgrep_rules_prefetch_skip_unexpected": {"error"},
    "cli.messages.semgrep_rules_prefetch_skip_copy": {"error"},
}


def _load_catalog(lang: str) -> dict:
    with open(os.path.join(PROJECT_ROOT, "i18n", f"{lang}.json"), encoding="utf-8") as f:
        return json.load(f)


def _reach(data: dict, key: str):
    """复刻 ``t()`` 的 ``key.split(".")`` 逐层 ``dict.get`` 语义。"""
    value = data
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


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


def _format_fields(value: str):
    """返回值的占位符集合；值含非标识符花括号时返回 ``None``（不纳入口径）。"""
    probe = value.replace("{{", "").replace("}}", "")
    fields = set()
    for match in ANY_BRACE.finditer(probe):
        token = match.group(0)
        field = FIELD_TOKEN.fullmatch(token)
        if not field:
            return None
        fields.add(re.search(r"[A-Za-z_][A-Za-z0-9_]*", token).group(0))
    return fields


def _iter_i18n_sources():
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


def _collect_calls():
    """收集静态字面量 key 的调用：(path, line, key, kwargs, has_splat)。"""
    calls = []
    for rel_path, source in _iter_i18n_sources():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id not in ("t", "get_msg", "get_error", "get_arg_help"):
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            raw = node.args[0].value
            if not isinstance(raw, str):
                continue
            key = _resolve_key(node.func.id, raw)
            if key is None:
                continue
            kwargs = {kw.arg for kw in node.keywords if kw.arg}
            has_splat = any(kw.arg is None for kw in node.keywords)
            calls.append((rel_path, node.lineno, key, kwargs, has_splat))
    return calls


def _violations(catalog: dict, calls):
    out = []
    for rel_path, line, key, kwargs, has_splat in calls:
        if has_splat:
            continue
        value = _reach(catalog, key)
        if not isinstance(value, str):
            continue
        fields = _format_fields(value)
        if fields is None:
            continue
        missing = sorted(fields - kwargs)
        if missing:
            out.append(f"{rel_path}:{line} {key} missing {missing}")
    return out


@pytest.fixture(scope="module")
def calls():
    return _collect_calls()


@pytest.fixture(scope="module")
def zh_catalog():
    return _load_catalog("zh_CN")


@pytest.fixture(scope="module")
def en_catalog():
    return _load_catalog("en_US")


def test_scanner_self_check(calls):
    """扫描口径失效自检：调用面数量与 14 个变体 key 的引用都必须命中。"""
    assert len(calls) > 1500, f"静态调用数量异常偏少: {len(calls)}"
    referenced = {key for _, _, key, _, _ in calls}
    missing = sorted(set(VARIANT_FIELDS) - referenced)
    assert not missing, f"变体 key 未被任何调用引用: {missing}"


def test_zh_catalog_placeholders_satisfied(calls, zh_catalog):
    violations = _violations(zh_catalog, calls)
    assert not violations, "zh_CN 占位符缺参:\n" + "\n".join(violations)


def test_en_catalog_placeholders_satisfied(calls, en_catalog):
    violations = _violations(en_catalog, calls)
    assert not violations, "en_US 占位符缺参:\n" + "\n".join(violations)


@pytest.mark.parametrize("key", sorted(VARIANT_FIELDS))
def test_variant_key_shape(key, zh_catalog, en_catalog):
    """7 变体的双语值存在，且占位符集合与预期一致。"""
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        value = _reach(catalog, key)
        assert isinstance(value, str), f"{lang} 缺少变体 key: {key}"
        fields = _format_fields(value)
        assert fields == VARIANT_FIELDS[key], (
            f"{lang} 的 {key} 占位符 {fields} != 预期 {VARIANT_FIELDS[key]}"
        )


@pytest.mark.parametrize(
    "value, kwargs, missing",
    [
        ("Task not found: {id}", {"id": "T-1"}, set()),
        ("Task not found: {id}", set(), {"id"}),
        ("Range {start}-{end} ({total} lines)", {"start": 1}, {"end", "total"}),
        ("Invalid: {value:03d}", set(), {"value"}),
        ("Repr: {value!r}", {"value": 1}, set()),
    ],
)
def test_missing_kwargs_detection_negative(value, kwargs, missing):
    """门禁核心判定的负向/正向样例。"""
    assert _format_fields(value) - set(kwargs) == missing


def test_non_identifier_braces_are_excluded():
    """JSON 示例 / 整段脚本值含非标识符花括号，不纳入占位符口径。"""
    json_example = 'Scope JSON, e.g. {"languages":["python"],"actions":["edit"]}'
    assert _format_fields(json_example) is None
    assert _format_fields('print({"decision": "block"})') is None
    assert _format_fields("Plain message") == set()
