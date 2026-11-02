"""i18n 占位符与格式化安全门禁（backlog §27 · T-1793540468063-993d3638）

§25/§26 已收口「引用即存在」与「default 兜底缺失」。本门禁覆盖第三类漂移：
**catalog 值含占位符，但调用未提供同名 kwargs**。

``i18n.t()`` 用 ``value.format(**kwargs)`` 替换占位符；缺参时抛 ``KeyError``，
被 ``except (KeyError, ValueError, IndexError)`` 吞掉后原样返回 catalog 值，
用户直接看到裸 ``{dir}`` / ``{error}`` / ``{id}`` 等。key 存在性门禁无法发现
这类缺口——key 存在，坏的是调用与 catalog 的形状契约。

判定口径：

- 只检查**静态字面量 key + 显式关键字实参**的调用；
- 带 ``**kwargs`` 展开的调用无法静态判定，豁免；
- 占位符只认 ``{ident}`` / ``{ident!r}`` / ``{ident:spec}``；
- 两个 catalog 各判一遍，任一缺参即失败。

§28（T-1793549305665-435643ec）追加**转义感知的格式化安全门禁**：catalog 中
字面花括号（JSON 示例 / 计划模板 / 脚本文本）必须写成 ``{{`` / ``}}``，其余
``{...}`` 必须是合法字段 token；任何裸花括号都会让 ``format()`` 抛
``ValueError`` 并被静默吞掉，属同一静默分支的不可检测缺口。6 个已转义 key
另断言 ``format()`` 输出与转义前基线逐字节一致。

§29（T-1793584498885-52bec080）把 §28.5 登记的 3 个表头 key 收口：只转义字面列宽
标签（``{{'行数':>5}}``），保留真实占位符 ``{hash:>3}``；残留白名单清零，全量扫描
不再允许任何豁免。

§30（T-1793598690524-924bef1c）收口**值类型契约**：6 个列表型叶子此前经 ``t()``
的 ``str(value)`` 分支变成 Python repr 字符串，污染 ``constraints`` / ``check_items``。
本门禁断言这 6 个 key 均为非空 ``list[str]``、``t_list()`` 返回与 catalog 相等、
str 值包成单元素列表，并用 AST 正反扫描保证调用点已全部迁移到 ``t_list``。
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

# §28 转义的 6 个「纯字面」key（两套 catalog 均已把 { } 写成 {{ }}）
ESCAPED_LITERAL_KEYS = {
    "cli.messages.install_agent_hook_script",
    "cli.messages.task_plan_template",
    "cli_rule_arg_scope",
    "cli_rule_arg_evidence",
    "cli_rule_arg_context",
    "cli_task_arg_steps",
}

# §29 转义的 3 个「表头」key：保留真实占位符 {hash:>3}，只把字面列宽标签
# （{'行数':>5} 等）写成 {{...}}。
ESCAPED_HEADER_KEYS = {
    "cli.messages.complexity_header",
    "cli.messages.largest_fns_header",
    "cli.messages.coupled_fns_header",
}

# 表头值的唯一真实占位符与断言样例
HEADER_HASH_FIELD = "{hash:>3}"
HEADER_SAMPLE = "A1B"

# §28.5 登记、§29 已收口的残留白名单。§29 后必须为空：任何裸花括号值都应
# 先转义再纳入本门禁，不允许再通过白名单豁免。
LITERAL_BRACE_RESIDUAL: set = set()

# 转义感知 token：{{ / }} 是字面量；其余 {..} 必须是合法字段
ESCAPE_TOKEN = re.compile(
    r"\{\{|\}\}|\{[A-Za-z_][A-Za-z0-9_]*(?:![rsa])?(?::[^{}]*)?\}"
)


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


def _iter_leaves(data, prefix: str = ""):
    """遍历 catalog 全部字符串叶子：(点分 key, 值)。"""
    if isinstance(data, dict):
        for name, value in data.items():
            path = f"{prefix}.{name}" if prefix else name
            yield from _iter_leaves(value, path)
    elif isinstance(data, str):
        yield prefix, data


def _bare_braces(value: str) -> list:
    """返回未被合法 token 覆盖的裸花括号片段；空列表表示格式化安全。"""
    bad, pos = [], 0
    for match in ESCAPE_TOKEN.finditer(value):
        gap = value[pos:match.start()]
        if "{" in gap or "}" in gap:
            bad.append(gap)
        pos = match.end()
    tail = value[pos:]
    if "{" in tail or "}" in tail:
        bad.append(tail)
    return bad


def _rendered_baseline(value: str) -> str:
    """转义前的可见文本：把 ``{{`` / ``}}`` 还原为 ``{`` / ``}``。"""
    return value.replace("{{", "{").replace("}}", "}")


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


# ---------------------------------------------------------------------------
# §28/§29 转义感知的格式化安全门禁（T-1793549305665-435643ec / T-1793584498885-52bec080）
# ---------------------------------------------------------------------------


@pytest.fixture()
def active_language():
    """临时切换 i18n 语言并在用例结束后还原，避免污染其他测试。"""
    import i18n

    original = i18n.get_language()

    def _use(lang):
        i18n.set_language(lang)

    yield _use
    i18n.set_language(original)


@pytest.mark.parametrize("key", sorted(ESCAPED_LITERAL_KEYS))
def test_escaped_literal_key_formats_to_baseline(key, zh_catalog, en_catalog):
    """6 个 key 已完成转义，且 format() 输出与转义前基线逐字节一致。"""
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        value = _reach(catalog, key)
        assert isinstance(value, str), f"{lang} 缺少 {key}"
        assert "{{" in value and "}}" in value, f"{lang} 的 {key} 未完成转义"
        assert not _bare_braces(value), f"{lang} 的 {key} 仍含裸花括号"
        assert value.format() == _rendered_baseline(value), (
            f"{lang} 的 {key} 格式化结果与转义前基线不一致"
        )


@pytest.mark.parametrize("key", sorted(ESCAPED_LITERAL_KEYS))
def test_escaped_literal_key_t_output_matches_baseline(
    key, active_language, zh_catalog, en_catalog
):
    """走真实 ``i18n.t()`` 路径：渲染文本必须等于转义前的基线。"""
    import i18n

    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        active_language(lang)
        value = _reach(catalog, key)
        assert i18n.t(key) == _rendered_baseline(value), (
            f"{lang} 的 {key} 经 t() 渲染后发生文本漂移"
        )


@pytest.mark.parametrize("key", sorted(ESCAPED_HEADER_KEYS))
def test_escaped_header_key_formats_with_hash(key, zh_catalog, en_catalog):
    """§29 表头值：字面标签已转义，仅保留 {hash} 真实占位符。"""
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        value = _reach(catalog, key)
        assert isinstance(value, str), f"{lang} 缺少 {key}"
        assert "{{" in value and "}}" in value, f"{lang} 的 {key} 未完成转义"
        assert not _bare_braces(value), f"{lang} 的 {key} 仍含裸花括号"
        assert _format_fields(value) == {"hash"}, (
            f"{lang} 的 {key} 占位符应恰为 {{hash}}，实际 {_format_fields(value)}"
        )
        pieces = _rendered_baseline(value).split(HEADER_HASH_FIELD)
        assert len(pieces) == 2, f"{lang} 的 {key} 应恰含一个 {HEADER_HASH_FIELD}"
        expected = f"{HEADER_SAMPLE:>3}".join(pieces)
        assert value.format(hash=HEADER_SAMPLE) == expected, (
            f"{lang} 的 {key} 格式化结果与转义前基线不一致"
        )


@pytest.mark.parametrize("key", sorted(ESCAPED_HEADER_KEYS))
def test_escaped_header_key_t_output_matches_baseline(
    key, active_language, zh_catalog, en_catalog
):
    """走真实 ``i18n.t(key, hash=..)`` 路径：渲染文本必须与基线一致。"""
    import i18n

    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        active_language(lang)
        value = _reach(catalog, key)
        pieces = _rendered_baseline(value).split(HEADER_HASH_FIELD)
        expected = f"{HEADER_SAMPLE:>3}".join(pieces)
        assert i18n.t(key, hash=HEADER_SAMPLE) == expected, (
            f"{lang} 的 {key} 经 t() 渲染后发生文本漂移"
        )


def test_catalogs_have_no_bare_braces(zh_catalog, en_catalog):
    """全量 catalog 扫描：任何叶子都不得含裸花括号（残留白名单已清零）。"""
    problems = []
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        for path, value in _iter_leaves(catalog):
            bad = _bare_braces(value)
            if bad:
                problems.append(f"{lang}:{path} -> {bad[:3]!r}")
    assert not problems, "catalog 存在未转义的裸花括号:\n" + "\n".join(problems)


def test_no_residual_literal_brace_keys(zh_catalog, en_catalog):
    """§29 收口后残留白名单必须为空，3 个表头 key 不得再含裸花括号。"""
    assert LITERAL_BRACE_RESIDUAL == set(), "残留白名单必须为空"
    assert LITERAL_BRACE_RESIDUAL.isdisjoint(
        ESCAPED_LITERAL_KEYS | ESCAPED_HEADER_KEYS
    )
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        for key in sorted(ESCAPED_HEADER_KEYS):
            value = _reach(catalog, key)
            assert isinstance(value, str), f"{lang} 缺少表头 key: {key}"
            assert not _bare_braces(value), f"{lang} 的 {key} 仍含裸花括号"


def test_escape_scan_self_check_hits_all_escaped_keys(zh_catalog, en_catalog):
    """扫描口径自检：6+3=9 个已转义 key 必须全部被叶子扫描命中且已转义。"""
    escaped = ESCAPED_LITERAL_KEYS | ESCAPED_HEADER_KEYS
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        leaves = dict(_iter_leaves(catalog))
        missing = sorted(escaped - set(leaves))
        assert not missing, f"{lang} 扫描面未命中: {missing}"
        for key in sorted(escaped):
            assert "{{" in leaves[key] and "}}" in leaves[key], (
                f"{lang} 的 {key} 尚未转义"
            )


@pytest.mark.parametrize(
    "value, unsafe",
    [
        ("Task not found: {id}", False),
        ("Invalid: {value:03d}", False),
        ("Repr: {value!r}", False),
        ("{{literal}} and {id}", False),
        ("Tail }} is literal", False),
        ('Scope JSON, e.g. {"languages":["python"]}', True),
        ("print({'decision': 'block'})", True),
        ("Unbalanced {", True),
        ("{{start}} but {tail", True),
    ],
)
def test_bare_brace_detection(value, unsafe):
    """转义感知判定的正/负向样例。"""
    assert bool(_bare_braces(value)) == unsafe


# ---------------------------------------------------------------------------
# §30 列表值契约 t_list 与 6 个 list 型 key（T-1793598690524-924bef1c）
# ---------------------------------------------------------------------------

LIST_VALUE_KEYS = {
    "cli.messages.task_constraints_annotate",
    "cli.messages.task_constraints_refactor",
    "cli.messages.task_constraints_fix",
    "cli.messages.task_constraints_edit",
    "cli.messages.task_constraints_default",
    "cli.messages.task_plan_root_check_items",
}

# 用于验证「str 值 → 单元素列表」形状的对照 key（含 ``{title}`` 占位符）
LIST_VALUE_STR_KEY = "cli.messages.task_plan_subtask_check_item"


def _iter_all_leaves(data, prefix: str = ""):
    """遍历 catalog 全部叶子（不论类型）：(点分 key, 值)。"""
    if isinstance(data, dict):
        for name, value in data.items():
            path = f"{prefix}.{name}" if prefix else name
            yield from _iter_all_leaves(value, path)
    else:
        yield prefix, data


def _collect_t_list_calls():
    """收集 ``t_list`` 静态字面量 key 调用：(path, line, key)。"""
    calls = []
    for rel_path, source in _iter_i18n_sources():
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                continue
            if node.func.id != "t_list":
                continue
            if not node.args or not isinstance(node.args[0], ast.Constant):
                continue
            key = node.args[0].value
            if isinstance(key, str):
                calls.append((rel_path, node.lineno, key))
    return calls


@pytest.fixture(scope="module")
def t_list_calls():
    return _collect_t_list_calls()


@pytest.mark.parametrize("key", sorted(LIST_VALUE_KEYS))
def test_list_value_key_is_nonempty_string_list(key, zh_catalog, en_catalog):
    """6 个 list 型 key 在两套 catalog 中均为非空 ``list[str]``。"""
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        value = _reach(catalog, key)
        assert isinstance(value, list), f"{lang} 的 {key} 不是 list: {type(value)}"
        assert value, f"{lang} 的 {key} 是空列表"
        assert all(isinstance(item, str) for item in value), (
            f"{lang} 的 {key} 含非字符串元素"
        )


def test_catalog_non_string_leaves_are_exactly_list_value_keys(zh_catalog, en_catalog):
    """两套 catalog 的非字符串叶子必须恰为这 6 个 list 型 key。

    新增其它类型的叶子会让 ``t()`` 的 ``str(value)`` 分支再次产出 repr，
    因此在此收紧口径：非 str 叶子只能来自已迁移的 list key。
    """
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        actual = {
            path for path, value in _iter_all_leaves(catalog)
            if not isinstance(value, str)
        }
        assert actual == LIST_VALUE_KEYS, (
            f"{lang} 非字符串叶子集合漂移: {sorted(actual ^ LIST_VALUE_KEYS)}"
        )


@pytest.mark.parametrize("key", sorted(LIST_VALUE_KEYS))
def test_t_list_matches_catalog_list(key, active_language, zh_catalog, en_catalog):
    """``t_list`` 双语返回与 catalog 列表逐项相等。"""
    import i18n

    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        active_language(lang)
        assert i18n.t_list(key) == _reach(catalog, key), (
            f"{lang} 的 {key} 经 t_list() 后与 catalog 不一致"
        )


def test_t_list_returns_copy_not_catalog_object(active_language, zh_catalog):
    """``t_list`` 返回元素副本，调用方修改不得污染语言缓存。"""
    import i18n

    key = "cli.messages.task_constraints_fix"
    active_language("zh_CN")
    first = i18n.t_list(key)
    first.append("__pollute__")
    assert i18n.t_list(key) == _reach(zh_catalog, key)


def test_t_list_str_value_wraps_single_item(active_language, zh_catalog):
    """catalog 值为 str 时返回单元素列表，并保留 ``format`` 容错语义。"""
    import i18n

    active_language("zh_CN")
    value = _reach(zh_catalog, LIST_VALUE_STR_KEY)
    assert isinstance(value, str)
    assert i18n.t_list(LIST_VALUE_STR_KEY, title="X") == [value.format(title="X")]
    # 缺参时 ``format`` 失败被吞掉，原样返回单元素（与 t() 同口径）
    assert i18n.t_list(LIST_VALUE_STR_KEY) == [value]


def test_t_list_missing_key_falls_back(active_language):
    """key 缺失：有 default 用 default，无 default 返回空列表。"""
    import i18n

    active_language("zh_CN")
    missing = "cli.messages.__missing_list_key__"
    assert i18n.t_list(missing, default=["a", "b"]) == ["a", "b"]
    assert i18n.t_list(missing) == []
    assert i18n.t_list(missing, default="single") == ["single"]


def test_list_value_keys_not_called_via_t(calls):
    """AST 反向扫描：6 个 list 型 key 不得再经 ``t()`` 调用。"""
    referenced = {key for _, _, key, _, _ in calls}
    leaked = sorted(LIST_VALUE_KEYS & referenced)
    assert not leaked, f"list 型 key 仍经 t() 调用: {leaked}"


def test_t_list_migration_covers_all_list_keys(t_list_calls):
    """正向扫描：6 个 list 型 key 必须全部已迁移到 ``t_list``。"""
    covered = {key for _, _, key in t_list_calls}
    missing = sorted(LIST_VALUE_KEYS - covered)
    assert not missing, f"list 型 key 未迁移到 t_list: {missing}"
    paths = {rel for rel, _, key in t_list_calls if key in LIST_VALUE_KEYS}
    assert paths == {"db/db_tasks.py"}, f"迁移落点异常: {sorted(paths)}"

