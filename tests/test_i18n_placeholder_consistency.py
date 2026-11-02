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

§31（T-1793601570549-21299a68）收口**分支语义与文案错位**：两处
``os.access(cache_parent, os.W_OK)`` 分支此前复用 ``*_skip_dir``（文案「缓存父目录
不存在」），zh 环境把「无写权限」显示成「目录不存在」。本门禁断言两个
``*_skip_no_write`` key 双语存在且占位符恰为 ``{dir}``、文案与同族 ``skip_dir``
不同，并用 AST 扫描确认写权限分支不再引用 ``*_skip_dir``。

§32（T-1793608696517-45fb6034）收口**动态拼接 key 盲区**：静态门禁只覆盖首参为
字符串字面量的调用，``t(variable)`` 无法在 AST 阶段判定。本门禁把现存动态调用点
（文件 + 函数限定名 + 函数名 + 实参签名）冻结成显式 allowlist，新增或消失都失败；
另把两处可枚举的 ``mode_key`` 迁移为字面量 key，并为 ``cli/categories.py`` 注册表的
``title_key`` / ``desc_key`` 补双语存在性断言。
"""
import ast
import json
import os
import re
from collections import Counter

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
    # §31：os.access(..., os.W_OK) 分支的专属变体（原先复用 *_skip_dir）
    "cli.messages.install_semgrep_prefetch_skip_no_write": {"dir"},
    "cli.messages.semgrep_rules_prefetch_skip_no_write": {"dir"},
}

# §31：写权限变体 → 其同族「目录不存在」key 的映射
NO_WRITE_KEYS = {
    "cli.messages.install_semgrep_prefetch_skip_no_write":
        "cli.messages.install_semgrep_prefetch_skip_dir",
    "cli.messages.semgrep_rules_prefetch_skip_no_write":
        "cli.messages.semgrep_rules_prefetch_skip_dir",
}

# §31：写权限分支的调用点（文件 → 应引用的 no_write key）
NO_WRITE_CALL_SITES = {
    "install.py": "cli.messages.install_semgrep_prefetch_skip_no_write",
    "server/mcp_server.py": "cli.messages.semgrep_rules_prefetch_skip_no_write",
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


# ---------------------------------------------------------------------------
# §31 _dir 写权限语义拆分与 2 处调用点迁移（T-1793601570549-21299a68）
# ---------------------------------------------------------------------------


def _load_source(rel_path: str) -> str:
    with open(os.path.join(PROJECT_ROOT, rel_path), encoding="utf-8") as f:
        return f.read()


def _os_attr_name(func):
    """返回 ``os.xxx`` / ``os.path.xxx`` 的 xxx；其它形态返回 None。"""
    if not isinstance(func, ast.Attribute):
        return None
    owner = func.value
    if isinstance(owner, ast.Name) and owner.id == "os":
        return func.attr
    if (
        isinstance(owner, ast.Attribute)
        and isinstance(owner.value, ast.Name)
        and owner.value.id == "os"
        and owner.attr == "path"
    ):
        return func.attr
    return None


def _if_branch_kind(test):
    """识别 ``if not os.path.isdir(...)`` / ``if not os.access(..., os.W_OK)``。"""
    if not isinstance(test, ast.UnaryOp) or not isinstance(test.op, ast.Not):
        return None
    call = test.operand
    if not isinstance(call, ast.Call):
        return None
    attr = _os_attr_name(call.func)
    if attr == "isdir":
        return "isdir"
    if (
        attr == "access"
        and call.args
        and isinstance(call.args[-1], ast.Attribute)
        and call.args[-1].attr == "W_OK"
    ):
        return "write"
    return None


def _literal_t_keys(node) -> list:
    """收集子树内 ``t("...")`` 静态字面量 key。"""
    keys = []
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call) or not isinstance(sub.func, ast.Name):
            continue
        if sub.func.id != "t" or not sub.args:
            continue
        raw = sub.args[0]
        if isinstance(raw, ast.Constant) and isinstance(raw.value, str):
            keys.append(raw.value)
    return keys


def _collect_branch_keys(rel_path: str) -> dict:
    """按分支语义收集文件内 ``t()`` 字面量 key：{'isdir': {...}, 'write': {...}}。"""
    tree = ast.parse(_load_source(rel_path))
    branches = {"isdir": set(), "write": set()}
    for node in ast.walk(tree):
        if isinstance(node, ast.If):
            kind = _if_branch_kind(node.test)
            if kind:
                branches[kind].update(_literal_t_keys(node))
    return branches


@pytest.mark.parametrize("key", sorted(NO_WRITE_KEYS))
def test_no_write_text_differs_from_skip_dir(key, zh_catalog, en_catalog):
    """写权限变体的双语文案与同族「目录不存在」文案必须不同。"""
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        value = _reach(catalog, key)
        dir_value = _reach(catalog, NO_WRITE_KEYS[key])
        assert isinstance(value, str) and value.strip(), f"{lang} 的 {key} 为空"
        assert isinstance(dir_value, str), f"{lang} 缺少 {NO_WRITE_KEYS[key]}"
        assert value != dir_value, f"{lang} 的 {key} 仍与 skip_dir 文案相同"


@pytest.mark.parametrize("rel_path, write_key", sorted(NO_WRITE_CALL_SITES.items()))
def test_write_permission_branch_uses_no_write_key(rel_path, write_key):
    """AST 扫描：写权限分支只引用 no_write 变体，isdir 分支只引用 skip_dir。"""
    dir_key = NO_WRITE_KEYS[write_key]
    branches = _collect_branch_keys(rel_path)
    assert branches["write"] == {write_key}, (
        f"{rel_path} 写权限分支 key 漂移: {sorted(branches['write'])}"
    )
    assert branches["isdir"] == {dir_key}, (
        f"{rel_path} isdir 分支 key 漂移: {sorted(branches['isdir'])}"
    )
    all_keys = set(_literal_t_keys(ast.parse(_load_source(rel_path))))
    assert {k for k in all_keys if k.endswith("_skip_dir")} == {dir_key}
    assert {k for k in all_keys if k.endswith("_skip_no_write")} == {write_key}


# ---------------------------------------------------------------------------
# §32 动态拼接 key 调用点清单化与门禁冻结（T-1793608696517-45fb6034）
# ---------------------------------------------------------------------------

# 扫描面：tests 之外的全部源码目录 + 两个单文件入口；与 §32.2 基线口径一致。
DYNAMIC_KEY_TARGETS = (
    "cli", "db", "server", "i18n", "scripts", "experiments", "tools",
    "install.py", "cw.py",
)
DYNAMIC_KEY_FUNCS = {"t", "t_list", "get_msg", "get_error", "get_arg_help"}
DYNAMIC_KEY_SKIP_DIRS = {"__pycache__", "tests"}

# 迁移为字面量 key 的两处 mode_key（dry_run / apply × 2 组）。
MIGRATED_MODE_KEYS = {
    "cli.messages.gc_retention_mode_dry_run",
    "cli.messages.gc_retention_mode_apply",
    "cli.messages.gc_archive_import_mode_dry_run",
    "cli.messages.gc_archive_import_mode_apply",
}

# 冻结清单：(文件, 函数限定名, 函数名, 实参签名) -> 保留理由（分类见 backlog §32.1）。
# 实参签名只取 Name.id / Attribute.attr / <fstring> 等稳定形状，避免引号与换行改写
# 触发假失败；新增或消失的调用点都必须同步修改本表，否则门禁失败。
DYNAMIC_KEY_CALL_SITES = {
    ("cli/main.py", "_collab_output_structured_reason", "t", "msg_key"):
        "D 结构化 reason：message_key 由 daemon 返回",
    ("cli/main.py", "_handle_experiment._msg", "t", "key"):
        "F 实验 CLI 薄包装：29 个调用方均传字面量 key",
    ("cli/main.py", "_print_main_help", "t", "title_key"):
        "B 注册表驱动：cli/categories.py title_key（存在性由本段用例兜底）",
    ("cli/main.py", "_print_main_help", "t", "desc_key"):
        "B 注册表驱动：cli/categories.py desc_key（存在性由本段用例兜底）",
    ("cli/main.py", "_t_structured", "t", "message_key"):
        "D 结构化 reason：daemon Structured_Reason message_key",
    ("db/db_task_contracts.py", "_resolve_contract_message", "t", "message_key"):
        "D 常量表：契约错误码 → i18n key",
    ("db/db_task_evidence.py", "_resolve_evidence_message", "t", "message_key"):
        "D 常量表：证据错误码 → i18n key",
    ("db/db_task_gate.py", "_resolve_gate_message", "t", "message_key"):
        "D GATE_I18N_KEYS 常量表",
    ("experiments/blind_review_evaluator.py", "_resolve_evaluator_message", "t", "message_key"):
        "E 盲审协议常量",
    ("experiments/blind_review_protocol.py", "_resolve_message", "t", "message_key"):
        "E 盲审协议常量",
    ("experiments/blind_review_views.py", "_resolve_view_message", "t", "message_key"):
        "E 盲审协议常量",
    ("i18n/__init__.py", "get_arg_help", "t", "<fstring>"):
        "A t() 公开入口包装",
    ("i18n/__init__.py", "get_error", "t", "<fstring>"):
        "A t() 公开入口包装",
    ("i18n/__init__.py", "get_msg", "t", "<fstring>"):
        "A t() 公开入口包装",
    ("server/tools/tools_collab.py", "register._collab_error_response", "t", "message_key"):
        "D MCP 协作工具错误 key",
}

EXPECTED_DYNAMIC_SITES = 15
REGISTRY_KEY_MIN = 56  # 12 title_key + 44 desc_key（backlog §32.2）


def _iter_dynamic_key_files(root: str):
    """遍历动态 key 扫描面：目录递归 + 单文件入口。"""
    for target in DYNAMIC_KEY_TARGETS:
        path = os.path.join(root, target)
        if os.path.isfile(path):
            yield path
            continue
        for dirpath, dirnames, filenames in os.walk(path):
            dirnames[:] = [d for d in dirnames if d not in DYNAMIC_KEY_SKIP_DIRS]
            for filename in sorted(filenames):
                if filename.endswith(".py"):
                    yield os.path.join(dirpath, filename)


def _dynamic_arg_signature(node) -> str:
    """把动态实参归一为稳定签名：Name.id / Attribute.attr / <fstring> 等。"""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.JoinedStr):
        return "<fstring>"
    if isinstance(node, ast.Call):
        return "<call>"
    if isinstance(node, ast.Subscript):
        return "<subscript>"
    return f"<{type(node).__name__}>"


class _DynamicKeyVisitor(ast.NodeVisitor):
    """收集非字面量首参调用：(相对路径, 函数限定名, 函数名, 实参签名)。"""

    def __init__(self, rel_path: str):
        self.rel_path = rel_path
        self.stack: list = []
        self.sites: list = []

    def _visit_scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope

    def visit_Call(self, node):
        func_name = None
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr
        if func_name in DYNAMIC_KEY_FUNCS and node.args:
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                ctx = ".".join(self.stack) or "<module>"
                self.sites.append(
                    (self.rel_path, ctx, func_name, _dynamic_arg_signature(first))
                )
        self.generic_visit(node)


def _scan_dynamic_key_files(files, root: str) -> Counter:
    found = Counter()
    for path in files:
        try:
            with open(path, encoding="utf-8") as f:
                source = f.read()
        except (OSError, UnicodeDecodeError):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        rel_path = os.path.relpath(path, root).replace("\\", "/")
        visitor = _DynamicKeyVisitor(rel_path)
        visitor.visit(tree)
        found.update(visitor.sites)
    return found


def _collect_dynamic_key_sites(root: str = PROJECT_ROOT) -> Counter:
    return _scan_dynamic_key_files(_iter_dynamic_key_files(root), root)


def test_dynamic_key_sites_match_frozen_allowlist():
    """动态 key 调用点必须与冻结 allowlist 完全一致（新增/消失都失败）。"""
    found = _collect_dynamic_key_sites()
    allowed = Counter({site: 1 for site in DYNAMIC_KEY_CALL_SITES})
    added = sorted((found - allowed).elements())
    removed = sorted((allowed - found).elements())
    assert not added, f"新增未登记的动态 key 调用点（请改为字面量 key 或更新 §32 清单）: {added}"
    assert not removed, f"§32 清单中的调用点已消失（请同步更新 allowlist）: {removed}"


def test_dynamic_key_scanner_self_check():
    """扫描口径自检：数量与已知哨兵都必须命中，防止门禁退化成空转。"""
    found = _collect_dynamic_key_sites()
    assert sum(found.values()) == EXPECTED_DYNAMIC_SITES, (
        f"动态 key 调用点数量漂移: {sum(found.values())}"
    )
    assert ("i18n/__init__.py", "get_msg", "t", "<fstring>") in found
    assert ("cli/main.py", "_print_main_help", "t", "title_key") in found


def test_dynamic_key_scanner_detects_new_sites(tmp_path):
    """负向自检：扫描器必须识别新增动态调用，并忽略同文件内的字面量调用。"""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "def outer(k):\n"
        "    def inner(v):\n"
        "        return t(v, default='x')\n"
        "    return t('cli.messages.exact_key'), t(k)\n",
        encoding="utf-8",
    )
    found = _scan_dynamic_key_files([str(sample)], str(tmp_path))
    assert found == Counter(
        {
            ("sample.py", "outer.inner", "t", "v"): 1,
            ("sample.py", "outer", "t", "k"): 1,
        }
    )


def test_migrated_mode_keys_are_literal_and_exist(calls, zh_catalog, en_catalog):
    """迁移后的 4 个 mode key 必须以字面量调用出现，且双语可达。"""
    literal_keys = {key for _, _, key, _, _ in calls}
    for key in sorted(MIGRATED_MODE_KEYS):
        assert key in literal_keys, f"{key} 未以字面量形式出现在静态调用中"
        for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
            assert isinstance(_reach(catalog, key), str), f"{lang} 缺少 {key}"


def test_categories_registry_keys_exist_in_both_catalogs(zh_catalog, en_catalog):
    """cli/categories.py 注册表的 title_key / desc_key 必须双语可达。"""
    from cli.categories import COMMAND_CATEGORIES

    keys = []
    for category in COMMAND_CATEGORIES:
        if category.title_key:
            keys.append(category.title_key)
        for command in category.commands:
            if command.desc_key:
                keys.append(command.desc_key)
    assert len(keys) >= REGISTRY_KEY_MIN, f"注册表 key 数量异常偏少: {len(keys)}"
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        missing = sorted(k for k in keys if not isinstance(_reach(catalog, k), str))
        assert not missing, f"{lang} 缺少注册表 key: {missing}"


# ---------------------------------------------------------------------------
# §33 message_key 兜底审计与 bundled 双语覆盖冻结（T-1793614942434-83a49fb4）
# ---------------------------------------------------------------------------

# (标签, 模块路径, 错误码映射属性, 警告码映射属性, bundled 属性, resolver 属性,
#  emit key 基线数, bundled key 基线数)
BUNDLED_RESOLVER_SPECS = (
    ("contracts", "callwarden.db.db_task_contracts", "CONTRACT_I18N_KEYS",
     "CONTRACT_WARNING_I18N_KEYS", "_CONTRACT_BUNDLED_DEFAULTS",
     "_resolve_contract_message", 12, 12),
    ("evidence", "callwarden.db.db_task_evidence", "EVIDENCE_I18N_KEYS",
     "EVIDENCE_WARNING_I18N_KEYS", "_EVIDENCE_BUNDLED_DEFAULTS",
     "_resolve_evidence_message", 10, 10),
    ("gate", "callwarden.db.db_task_gate", "GATE_I18N_KEYS",
     None, "_GATE_BUNDLED_DEFAULTS", "_resolve_gate_message", 6, 6),
    ("protocol", "callwarden.experiments.blind_review_protocol",
     "EXPERIMENT_I18N_KEYS", None, "_BUNDLED_DEFAULTS", "_resolve_message", 10, 10),
    ("evaluator", "callwarden.experiments.blind_review_evaluator",
     "EVALUATOR_I18N_KEYS", None, "_EVALUATOR_BUNDLED_DEFAULTS",
     "_resolve_evaluator_message", 7, 7),
    ("views", "callwarden.experiments.blind_review_views", "VIEW_I18N_KEYS",
     None, "_VIEW_BUNDLED_DEFAULTS", "_resolve_view_message", 6, 6),
)

UNKNOWN_KEY_SENTINEL = "__cw_audit_unknown_message_key__"

# db_task_contracts.py:911 的受保护写路径真实上下文（lease_denied 调用点）。
LEASE_DENIED_CONTEXT = {
    "task_id": "T-1",
    "lease_code": "LEASE_EXPIRED",
    "lease_detail": "lease expired",
}


def _import_dotted(dotted: str):
    import importlib

    return importlib.import_module(dotted)


def _emitted_keys(module, error_attr: str, warning_attr) -> set:
    """映射表 values 去重：错误码 + 警告码的全部可 emit message_key。"""
    keys = set(getattr(module, error_attr).values())
    if warning_attr:
        keys |= set(getattr(module, warning_attr).values())
    return keys


def _catalog_string(catalog: dict, key: str):
    """三路双语可达判定：catalog 嵌套 → daemon_errors 扁平 → 原样扁平。"""
    value = _reach(catalog, key)
    if isinstance(value, str) and value:
        return value
    flat_section = catalog.get("daemon_errors")
    if isinstance(flat_section, dict):
        candidates = [key]
        if key.startswith("errors."):
            candidates.insert(0, key[len("errors."):])
        for candidate in candidates:
            value = flat_section.get(candidate)
            if isinstance(value, str) and value:
                return value
    return None


def _dict_literal_keys(tree, var_name: str, function=None) -> set:
    """抽取字典字面量赋值的字符串 key 集合（可按函数限定）。"""
    scopes = [tree]
    if function is not None:
        scopes = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function
        ]
    found = set()
    for scope in scopes:
        for node in ast.walk(scope):
            if not isinstance(node, ast.Assign):
                continue
            if not isinstance(node.value, ast.Dict):
                continue
            if any(isinstance(t, ast.Name) and t.id == var_name
                   for t in node.targets):
                found |= {
                    k.value for k in node.value.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
    return found


def test_bundled_resolvers_cover_all_emitted_keys():
    """6 个常量映射模块必须满足 emit ⊆ bundled，且集合规模与基线一致。"""
    for label, dotted, err_attr, warn_attr, bundled_attr, _resolver, emit_n, bundled_n in BUNDLED_RESOLVER_SPECS:
        module = _import_dotted(dotted)
        emit = _emitted_keys(module, err_attr, warn_attr)
        bundled = getattr(module, bundled_attr)
        assert len(emit) == emit_n, f"{label} emit key 数量漂移: {len(emit)} != {emit_n}"
        assert len(bundled) == bundled_n, (
            f"{label} bundled key 数量漂移: {len(bundled)} != {bundled_n}"
        )
        missing = sorted(emit - set(bundled))
        assert not missing, f"{label} emit key 缺少 bundled 兜底: {missing}"


def test_emitted_keys_resolve_bilingually(zh_catalog, en_catalog):
    """每个 emit key 在 zh/en 下至少命中 catalog 或 bundled，不得退化为裸 key。"""
    for label, dotted, err_attr, warn_attr, bundled_attr, _resolver, _emit_n, _bundled_n in BUNDLED_RESOLVER_SPECS:
        module = _import_dotted(dotted)
        emit = _emitted_keys(module, err_attr, warn_attr)
        bundled = getattr(module, bundled_attr)
        for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
            unresolved = sorted(
                key for key in emit
                if _catalog_string(catalog, key) is None and key not in bundled
            )
            assert not unresolved, f"{label} {lang} 存在裸 key 泄漏: {unresolved}"


def test_bundled_defaults_are_bilingual_nonempty():
    """bundled 的每个条目必须同时具备非空 zh_CN 与 en_US。"""
    for label, dotted, _e, _w, bundled_attr, _r, _en, _bn in BUNDLED_RESOLVER_SPECS:
        module = _import_dotted(dotted)
        for key, bundle in getattr(module, bundled_attr).items():
            for lang in ("zh_CN", "en_US"):
                value = bundle.get(lang)
                assert isinstance(value, str) and value.strip(), (
                    f"{label} bundled {key} 缺少 {lang} 文案"
                )


def test_unknown_message_key_returns_key_without_raising():
    """未知 key 的可观察兜底终点：原样返回 key，不抛异常、不返回空串。"""
    for label, dotted, _e, _w, _bundled, resolver_attr, _en, _bn in BUNDLED_RESOLVER_SPECS:
        module = _import_dotted(dotted)
        resolver = getattr(module, resolver_attr)
        assert resolver(UNKNOWN_KEY_SENTINEL, {}) == UNKNOWN_KEY_SENTINEL, (
            f"{label} 未知 key 兜底行为漂移"
        )


def test_resolver_prefers_catalog_over_bundled(monkeypatch):
    """catalog 命中时必须优先使用 catalog 文案，bundled 只是缺失时的兜底。"""
    from callwarden import i18n as i18n_mod

    sentinel = "CATALOG-SENTINEL-DO-NOT-USE-BUNDLED"
    fake_catalog = {"errors": {"contract_envelope_grammar": sentinel}}
    monkeypatch.setattr(i18n_mod, "_load_lang", lambda lang: fake_catalog)
    module = _import_dotted("callwarden.db.db_task_contracts")
    assert module._resolve_contract_message(
        "errors.contract_envelope_grammar", {}
    ) == sentinel


def test_lease_denied_placeholders_match_call_site(zh_catalog, en_catalog, monkeypatch):
    """lease_denied 的 catalog 与 bundled 占位符必须匹配真实调用上下文。"""
    key = "errors.contract_lease_denied"
    module = _import_dotted("callwarden.db.db_task_contracts")
    bundle = module._CONTRACT_BUNDLED_DEFAULTS[key]
    texts = [
        _reach(zh_catalog, key),
        _reach(en_catalog, key),
        bundle["zh_CN"],
        bundle["en_US"],
    ]
    for text in texts:
        assert isinstance(text, str) and text, f"{key} 文案缺失"
        assert _format_fields(text) == {"task_id", "lease_code", "lease_detail"}, (
            f"{key} 占位符与调用点不一致: {text}"
        )
    i18n_mod = _import_dotted("callwarden.i18n")
    for lang in ("zh_CN", "en_US"):
        monkeypatch.setattr(i18n_mod, "_current_lang", lang)
        rendered = module._resolve_contract_message(key, LEASE_DENIED_CONTEXT)
        assert "{" not in rendered, f"{lang} 渲染残留花括号: {rendered}"


def test_cli_structured_default_keys_resolve_in_daemon_errors(zh_catalog, en_catalog):
    """cli/main.py 的 Structured_Reason 默认 key 必须在 daemon_errors 扁平段双语可达。"""
    tree = ast.parse(_load_source("cli/main.py"))
    keys = _dict_literal_keys(tree, "_IDENTITY_REASON_DEFAULTS")
    keys |= _dict_literal_keys(tree, "_DEFAULTS", function="_collab_output_structured_reason")
    assert len(keys) == 5, f"cli 结构化默认 key 数量漂移: {sorted(keys)}"
    for lang, catalog in (("zh_CN", zh_catalog), ("en_US", en_catalog)):
        flat = catalog.get("daemon_errors", {})
        missing = sorted(
            k for k in keys
            if not isinstance(flat.get(k[len("daemon_errors."):] if k.startswith("daemon_errors.") else k), str)
        )
        assert not missing, f"{lang} daemon_errors 缺少: {missing}"


def test_mcp_collab_error_response_has_no_call_sites():
    """MCP 协作错误构造器当前无调用点；新增 emit 点必须同步进入 §33 审计。"""
    tree = ast.parse(_load_source("server/tools/tools_collab.py"))
    calls = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id == "_collab_error_response")
            or (isinstance(node.func, ast.Attribute) and node.func.attr == "_collab_error_response")
        )
    ]
    assert len(calls) == 0, (
        f"_collab_error_response 出现新调用点，必须纳入 §33 兜底审计: {len(calls)}"
    )

