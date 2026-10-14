"""TP-11L 16 语言解析 / 引用独立性 golden 验收。

在完全隔离的 daemon（独立 run 根 + git provenance workspace + RPC 直调）上，
对 16 种语言的最小 golden 源文件做**具名符号**级断言，并验证同名字号在
不同语言/不同文件下互不串扰。

套件结构：
- L1 逐语种 golden：15 个有解析配置的语言各断言其具名符号（名 + kind +
  完整 qualified_name）；C 断言「只索引不出符号」；
- L2 引用独立性：`alpha_probe` 这一同名符号在 13 个语言文件中各自独立，
  qualified_name 两两不同、file_path 各自回指本语言文件；
- L3 边界：不支持扩展名 / 空文件 / BOM / CRLF / 非 UTF-8 / 非 ASCII 标识符
  均不得炸掉建图，且能出符号的照常出符号；
- L4 读面 fail-closed：query.file / query.symbol / query.search 的参数校验
  （具体错误码 out_of_bounds / invalid_params）；
- L5 已知产品缺陷（xfail 固化，非假 PASS）：query.file 恒 0 命中、
  query.symbol 恒 null。

🔴 未实测/跳过的项：无。16 个语种全部实测；`tf`（hcl 的另一扩展名）与
`mjs/cjs`（javascript 的另一扩展名）不在 INDEXABLE_EXTS 白名单内，故未用
它们做 fixture（用 `.hcl` / `.js` 实测），此项已在 harness docstring 说明。
"""

from __future__ import annotations

import pytest

from languages_harness import (
    EDGE_FIXTURES,
    GOLDEN_EXPECTATIONS,
    GOLDEN_SYMBOL_TOTAL,
    LANG_FIXTURES,
    SYMBOL_LANGS,
    LanguagesEnv,
)


@pytest.fixture(scope="module")
def env():
    e = LanguagesEnv()
    # run 根自清洁（防上一轮残留 daemon/registry/snapshot 污染本轮断言；
    # 多代 snapshot 会让 query.search 读到上一代符号，golden 计数失真）
    LanguagesEnv.wipe_run_root(e.run_root)
    e = LanguagesEnv()
    e.start_daemon()
    try:
        e.write_golden_sources()
        e.write_edge_fixtures()
        e.build_graph()
        e.snapshot_publish()
        yield e
    finally:
        e.stop_daemon()


@pytest.fixture(scope="module")
def symbols(env):
    """一次性捞出 fixture 全部符号：{qualified_name: 符号行}。"""
    return env.collect_symbols()


def _by_file(symbols):
    """{文件绝对路径: [(name, kind, qualified_name), ...]}"""
    out = {}
    for qn, row in symbols.items():
        fp = str(row.get("file_path") or "")
        out.setdefault(fp, []).append(
            (str(row.get("name")), str(row.get("kind")), qn))
    return out


# ===================================================================
# L1 逐语种 golden：具名符号必须被提取出来
# ===================================================================

@pytest.mark.parametrize("lang", SYMBOL_LANGS)
def test_l1_language_golden_symbols(env, symbols, lang):
    """L1：每个语言的 golden 文件必须产出**具名**符号（名 + kind + qname）。

    断言的是具体符号三元组，不是「非空即可」：缺任一条目或 kind/qname 变了
    都会失败。
    """
    fname = LANG_FIXTURES[lang]
    per_file = _by_file(symbols)
    matches = [rows for fp, rows in per_file.items()
               if fp.rsplit("/", 1)[-1] == fname]
    assert matches, (
        f"{lang}（{fname}）在检索结果里没有任何符号行；"
        f"实际按文件归集到的文件：{sorted(per_file)}")
    got = {(n, k, q) for rows in matches for (n, k, q) in rows}

    prefix = env.qname_prefix()
    for name, kind, suffix in GOLDEN_EXPECTATIONS[lang]:
        expected = (name, kind, prefix + suffix)
        assert expected in got, (
            f"{lang} 应产出符号 {expected}；该文件实际产出：{sorted(got)}")


@pytest.mark.parametrize("lang", SYMBOL_LANGS)
def test_l1b_no_unexpected_extra_symbols(env, symbols, lang):
    """L1b：golden 文件不得产出 golden 表之外的符号（防解析规则静默漂移）。"""
    fname = LANG_FIXTURES[lang]
    per_file = _by_file(symbols)
    rows = [r for fp, rs in per_file.items()
            if fp.rsplit("/", 1)[-1] == fname for r in rs]
    got = {(n, k, q) for (n, k, q) in rows}
    prefix = env.qname_prefix()
    expected = {(n, k, prefix + s) for (n, k, s) in GOLDEN_EXPECTATIONS[lang]}
    assert got == expected, (
        f"{lang}（{fname}）符号集与 golden 表不一致：\n"
        f"  多出：{sorted(got - expected)}\n"
        f"  缺失：{sorted(expected - got)}")


def test_l1c_c_file_is_indexed_without_symbols(env, symbols):
    """L1c（边界语种）：`.c` 映射到 lang_id "c" 但无解析配置 → 0 符号。

    这不是「跳过」，是实测断言：`get_config("c")` 返回 None，
    `parse_and_store_symbols` 直接 (0, 0) 返回。若将来给 C 补了配置，
    本断言会失败并显式暴露（而不是静默放行）。
    """
    fname = LANG_FIXTURES["c"]
    leaked = [(n, k, q) for (n, k, q) in
              [r for fp, rs in _by_file(symbols).items()
               if fp.rsplit("/", 1)[-1] == fname for r in rs]]
    assert leaked == [], (
        f"C 文件（{fname}）当前无解析配置，不应出符号；实际：{leaked}")


def test_l1d_golden_total_and_stats(env, symbols):
    """L1d：统计面与 golden 表自洽（symbol_count 覆盖全部 golden + 边界符号）。"""
    # golden 表 15 语种合计 38 个符号 + 4 个边界符号（unicode/bom/crlf/latin1）
    edge_total = sum(len(exp) for _rel, (_c, exp) in EDGE_FIXTURES.items())
    floor = GOLDEN_SYMBOL_TOTAL + edge_total
    assert GOLDEN_SYMBOL_TOTAL == 38, "golden 表总量变化须同步本断言"
    assert edge_total == 4, "边界 fixture 期望符号总量变化须同步本断言"
    assert len(symbols) >= floor, (
        f"检索捞到的符号数 {len(symbols)} 应 >= golden+边界合计 {floor}")

    st = env.query_stats()
    assert st.ok, f"query.stats 失败：{st!r}"
    count = (st.result or {}).get("symbol_count", 0)
    assert count >= floor, (
        f"query.stats.symbol_count={count} 应 >= {floor}（golden+边界符号总数）")
    # 文件索引覆盖 16 个语种 fixture + 6 个边界 fixture
    files = (st.result or {}).get("file_index_size", 0)
    assert files >= len(LANG_FIXTURES) + len(EDGE_FIXTURES), (
        f"file_index_size={files} 应覆盖 "
        f"{len(LANG_FIXTURES) + len(EDGE_FIXTURES)} 个 fixture 文件")


# ===================================================================
# L2 引用独立性：同名字号跨语言/跨文件互不串扰
# ===================================================================

def test_l2_same_name_is_not_merged_across_languages(env, symbols):
    """L2：同名的 `alpha_probe` 在每个语言文件里是**不同**符号。

    golden 断言：13 个语言各自产出一条 alpha_probe，qualified_name 两两不同
    且 file_path 精确回指本语言文件（不是别的语言的文件）。
    """
    hits = [row for row in symbols.values()
            if str(row.get("name")) == "alpha_probe"]
    got_files = sorted(str(r.get("file_path")).rsplit("/", 1)[-1] for r in hits)

    # 14 个语言定义了短名为 alpha_probe 的符号（hcl 的短名是
    # "aws_instance.alpha_probe"，C 无解析配置，均不计入）。
    expected_files = sorted(
        LANG_FIXTURES[lang] for lang in (
            "python", "rust", "go", "java", "csharp", "scala", "typescript",
            "javascript", "ruby", "php", "cpp", "kotlin", "swift", "elixir"))
    assert got_files == expected_files, (
        f"alpha_probe 应在这些文件里各自独立存在：{expected_files}；"
        f"实际：{got_files}")

    qns = [str(r.get("qualified_name")) for r in hits]
    assert len(qns) == len(set(qns)), \
        f"alpha_probe 的 qualified_name 必须两两不同：{qns}"


def test_l2b_qualified_names_are_globally_unique(symbols):
    """L2b：所有 golden 符号的 qualified_name 全局唯一（无跨文件合并）。"""
    qns = list(symbols.keys())
    assert len(qns) == len(set(qns)), (
        f"qualified_name 出现重复："
        f"{[q for q in qns if qns.count(q) > 1]}")


def test_l2c_file_path_matches_qualified_name_prefix(symbols):
    """L2c：符号的 file_path 与其 qualified_name 前缀一致（无跨文件挂错）。

    qualified_name 形态 `<文件绝对路径小写去扩展名>.<符号路径>`，
    故 file_path 去扩展名后必须等于 qualified_name 的目录+文件名部分。
    """
    for qn, row in symbols.items():
        fp = str(row.get("file_path") or "")
        assert fp, f"符号 {qn} 缺 file_path：{row}"
        assert fp.lower() == fp, f"file_path 应为规范化小写路径：{fp}"
        stem = fp.rsplit(".", 1)[0]
        # 顶层符号：qname 恰为 stem + "." + name；嵌套符号则更长
        assert qn.startswith(stem + "."), (
            f"符号 {qn} 的 qualified_name 前缀与 file_path {fp} 不匹配")


def test_l2d_cross_language_kind_isolation(symbols):
    """L2d：kind 按语言语义区分，不被统一成同一个值（struct/class/module）。

    具体断言：rust 与 go 的 BetaProbe 是 struct，python/cs/java/scala 的
    BetaProbe 是 class，elixir 的 GoldenEx 是 module——三者互不混淆。
    """
    kind_of = {}
    for row in symbols.values():
        if str(row.get("name")) == "BetaProbe":
            fp = str(row.get("file_path")).rsplit("/", 1)[-1]
            kind_of[fp] = str(row.get("kind"))
    assert kind_of.get("golden_rust.rs") == "struct", \
        f"rust 的 BetaProbe 应为 struct：{kind_of}"
    assert kind_of.get("golden_go.go") == "struct", \
        f"go 的 BetaProbe 应为 struct：{kind_of}"
    assert kind_of.get("golden_python.py") == "class", \
        f"python 的 BetaProbe 应为 class：{kind_of}"
    assert kind_of.get("golden_cs.cs") == "class", \
        f"csharp 的 BetaProbe 应为 class：{kind_of}"
    assert kind_of.get("golden_java.java") == "class", \
        f"java 的 BetaProbe 应为 class：{kind_of}"
    assert kind_of.get("golden_scala.scala") == "class", \
        f"scala 的 BetaProbe 应为 class：{kind_of}"

    ex = [r for r in symbols.values()
          if str(r.get("file_path")).rsplit("/", 1)[-1] == "golden_ex.ex"]
    kinds = {str(r.get("name")): str(r.get("kind")) for r in ex}
    assert kinds.get("GoldenEx") == "module", \
        f"elixir 的模块符号应为 module：{kinds}"


# ===================================================================
# L3 边界：坏输入不得炸掉建图
# ===================================================================

@pytest.mark.parametrize("rel", sorted(EDGE_FIXTURES))
def test_l3_edge_fixtures_do_not_break_parse(env, symbols, rel):
    """L3：边界 fixture 的符号产出与 golden 表一致（含 0 符号的两例）。

    - `edge_unsupported.txt`：扩展名不在 INDEXABLE_EXTS → 不入索引、0 符号；
    - `edge_empty.py`：可索引但 0 符号；
    - unicode / BOM / CRLF / 非 UTF-8：照常出符号（具名断言）。
    建图本身在本 fixture 阶段已完成且未抛错——若边界文件炸掉建图，
    module fixture 就会在 build_graph() 处失败，不会走到这里。
    """
    fname = rel.rsplit("/", 1)[-1]
    _content, expected_names = EDGE_FIXTURES[rel]
    per_file = _by_file(symbols)
    rows = [r for fp, rs in per_file.items()
            if fp.rsplit("/", 1)[-1] == fname for r in rs]
    got = sorted(n for (n, _k, _q) in rows)
    assert got == sorted(expected_names), (
        f"边界文件 {fname} 期望符号 {sorted(expected_names)}，实际 {got}")


def test_l3b_unsupported_extension_does_not_pollute_other_languages(
        env, symbols):
    """L3b：`.txt` 文件既不出符号，也不挤掉其它语言的符号。"""
    txt = [r for r in symbols.values()
           if str(r.get("file_path")).endswith(".txt")]
    assert txt == [], f"不支持的扩展名不应出符号：{txt}"
    # 15 个语种的 golden 仍然完整
    for lang in SYMBOL_LANGS:
        fname = LANG_FIXTURES[lang]
        assert any(str(r.get("file_path")).rsplit("/", 1)[-1] == fname
                   for r in symbols.values()), (
            f"边界文件污染后 {lang}（{fname}）的符号消失了")


# ===================================================================
# L4 读面 fail-closed：参数校验的具体错误码
# ===================================================================

def test_l4_query_file_param_validation(env):
    """L4：query.file 参数校验（dispatch 层前置校验，不进 handler）。"""
    inst = env.setup_workspace()[1]
    empty = env.rpc("query.file", {"workspace_instance_id": inst,
                                   "file_path": ""})
    assert not empty.ok and empty.code == "invalid_params", \
        f"空 file_path 应 invalid_params：{empty!r}"

    traversal = env.rpc("query.file", {"workspace_instance_id": inst,
                                       "file_path": "../escape.py"})
    assert not traversal.ok and traversal.code == "out_of_bounds", \
        f"`..` 穿越应 out_of_bounds：{traversal!r}"

    missing = env.rpc("query.file", {"file_path": "golden/golden_python.py"})
    assert not missing.ok and missing.code == "invalid_params", \
        f"缺 workspace_instance_id 应 invalid_params：{missing!r}"


def test_l4b_query_symbol_param_validation(env):
    """L4b：query.symbol 参数校验（空/纯空白/缺失 → invalid_params）。"""
    inst = env.setup_workspace()[1]
    for tag, params in (
        ("空", {"qualified_name": ""}),
        ("纯空白", {"qualified_name": "   "}),
        ("缺失", {}),
    ):
        r = env.rpc("query.symbol",
                    {"workspace_instance_id": inst, **params})
        assert not r.ok and r.code == "invalid_params", \
            f"qualified_name {tag} 应 invalid_params：{r!r}"


def test_l4c_query_search_param_validation(env):
    """L4c：query.search 参数校验 + limit / kind 语义。"""
    inst = env.setup_workspace()[1]
    missing = env.rpc("query.search", {"workspace_instance_id": inst})
    assert not missing.ok and missing.code == "invalid_params", \
        f"缺 query 应 invalid_params：{missing!r}"

    # limit 生效（golden 里 alpha_probe 有 14 处，limit=3 只回 3 条）
    r = env.query_search("alpha_probe", limit=3)
    assert r.ok, f"query.search 失败：{r!r}"
    assert len(LanguagesEnv.symbol_rows(r.result)) == 3, \
        f"limit=3 应只回 3 条：{r.result}"

    # kind 过滤生效：class 过滤后全部是 class，且不再含 fn 的 alpha_probe
    r = env.query_search("alpha_probe", kind="class", limit=50)
    rows = LanguagesEnv.symbol_rows(r.result)
    assert all(str(x.get("kind")) == "class" for x in rows), \
        f"kind=class 过滤后仍含非 class：{rows}"


# ===================================================================
# L5 查询面（此前为已知产品缺陷，2026-10-14 修复后转正）
# 根因：resolve_workspace 返回的是 registry 的身份归一化串（全小写绝对
# 路径）而非文件系统根 → build_graph 的 strip_prefix 全部失败 →
# file_instances.rel_path 退化为全小写绝对路径 → query.file 的
# normalize_workspace_path 归一出的相对路径永不相等。修复点见
# fs_handlers.rs:resolve_workspace 的 canonicalize 还原。
# ===================================================================

def test_l5_query_file_should_return_file_symbols(env, symbols):
    """L5：query.file 按文件取回该文件的符号（相对路径入参）。"""
    rel = "golden/golden_python.py"
    r = env.query_file(rel)
    assert r.ok, f"query.file 失败：{r!r}"
    names = LanguagesEnv.symbol_names(r.result)
    assert "alpha_probe" in names, \
        f"query.file({rel}) 应回 alpha_probe；实际 {names}"


def test_l5b_query_symbol_should_return_symbol_detail(env, symbols):
    """L5b：query.symbol 返回符号详情（此前为已知缺陷，2026-10-14 修复）。

    根因：P0-CR1 随行落库只写 symbols/calls/symbol_contents，而
    query.symbol 走 query_symbol_detail（JOIN file_symbol_versions ↔
    file_versions）；版本化 schema 从未被写入 → 恒 null。修复：
    parse_and_store_symbols 现同步镜像到 file_versions +
    file_symbol_versions（fs_handlers.rs）。
    """
    qn = env.qname_prefix() + "golden_python.alpha_probe"
    assert qn in symbols, f"golden 符号 {qn} 未检出：{sorted(symbols)[:5]}"
    r = env.query_symbol(qn)
    assert r.ok, f"query.symbol 失败：{r!r}"
    assert isinstance(r.result, dict) and r.result.get("name") == "alpha_probe", \
        f"query.symbol({qn}) 应回符号详情；实际 {r.result}"
