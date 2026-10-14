"""TP-11W watcher/dirty overlay/增量验收：文件面增量刷新 + 构建面 + 读面结构。

在完全隔离的 daemon 上验证增量（dirty overlay）语义，每步断言 daemon 权威响应。

🔴 W5/W6 是本套件发现的**真实缺陷回归**：`workspace.file.list` 与
`workspace.file.grep` 的 `path`（子目录作用域）参数走
`validate_owned_path(p, uid, require_file=true)`，传目录一律报
`path_not_found: 不是文件` —— 而该参数的缺省值本身就是 workspace 根（目录）。
结果：这两个方法的子目录剪枝能力**从未真正可用**（与 workspace.rs:970 注释记载的
P0-CR2 同源缺陷，那次只修了 build_directory / build_graph，漏了这两个入口）。

🔴 路径契约（实测，禁再猜）：
- file.read / refresh_file / file.remove / file.health / build_directory 一律按
  **绝对路径**解析（走 canonicalize），传相对路径会 path_not_found；
- refresh_file 成功后回显的 file_path 反而是**相对路径**（入/出参不对称）；
- file.symbol_content 是唯一吃**相对路径**的（SQL 按 fi.rel_path 精确匹配，
  fs_handlers.rs:930）；
- file.list / file.grep 的作用域参数叫 `path`（不是 directory），另有 `glob`；
- file.health 按文档合同工作（文件：exists/size/mtime/readable）；目录亦被
  忍；越根一律 path_escape（TP-11A D4 修复；此前"只接受目录"是
  require_file 误用，现存 quirk 已被 TP-11A 定性为缺陷并修复）。
"""

from __future__ import annotations

import pytest

from incremental_harness import IncrementalEnv

SAMPLE_V1 = "def alpha():\n    return 1\n"
SAMPLE_V2 = "def alpha():\n    return 1\n\n\ndef beta():\n    return 2\n"


@pytest.fixture(scope="module")
def env():
    e = IncrementalEnv()
    # run 根自清洁（防上一轮残留 daemon/registry 污染本轮断言）
    IncrementalEnv.wipe_run_root(e.run_root)
    e = IncrementalEnv()
    e.start_daemon()
    try:
        e.build_graph()
        yield e
    finally:
        e.stop_daemon()


@pytest.fixture
def ws(env):
    """每个用例一份干净的工作区文件集（用例间互不污染）。"""
    env.delete_ws_file("src/sample.py")
    env.delete_ws_file("src/other.py")
    env.delete_ws_file("README.md")
    env.write_ws_file("src/sample.py", SAMPLE_V1)
    env.write_ws_file("src/other.py", "def gamma():\n    return alpha()\n")
    env.write_ws_file("README.md", "# tp11w\n")
    return env


# ---------------------------------------------------------------- W1 增量刷新

def test_w1_refresh_file_is_incremental_and_observable(env, ws):
    """W1（核心）：改文件 → 单文件 refresh → 读面立即反映新内容。

    断言 dirty overlay 的「单文件增量」语义：不需要全量重建，refresh 一个文件
    就能让读面看到新行；且 refresh 回显的 symbols 数随新符号增长。
    """
    abs_p = str(ws.workspace_dir / "src" / "sample.py")

    # 基线：建索引
    built = ws.build_directory("src")
    assert built.ok, f"build_directory 失败：{built!r}"
    assert (built.result or {}).get("scanned") == 2, \
        f"src 下 2 个 .py 文件：{built.result}"
    assert ((built.result or {}).get("symbols") or 0) >= 2, \
        f"基线应索引到 alpha/gamma：{built.result}"

    before = ws.file_read(abs_p)
    assert before.ok, f"file.read 失败：{before!r}"
    assert (before.result or {}).get("total_lines") == 2, before.result
    assert "beta" not in (before.result or {}).get("content", "")

    # 增量：改内容（不重建）
    ws.write_ws_file("src/sample.py", SAMPLE_V2)
    # 未 refresh 前读面走磁盘，已能看到新内容（file.read 是直读）
    mid = ws.file_read(abs_p)
    assert (mid.result or {}).get("total_lines") == 6, mid.result

    # refresh 落索引（符号数从 1 涨到 2）
    ref = ws.refresh_file(abs_p)
    assert ref.ok, f"refresh_file 失败：{ref!r}"
    r = ref.result or {}
    assert r.get("ok") is True, r
    # 回显是相对路径（入/出参不对称，固化该契约）
    assert r.get("file_path") == "src/sample.py", r
    assert r.get("total_lines") == 6, r
    assert r.get("symbols") == 2, f"refresh 后应有 alpha+beta 两个符号：{r}"

    after = ws.file_read(abs_p)
    assert "def beta():" in (after.result or {}).get("content", ""), after.result


# ---------------------------------------------------------------- W2 文件移除

def test_w2_remove_file_detaches_from_index_and_disk(env, ws):
    """W2：file.remove 摘除文件 —— 磁盘删除 + 读面转 path_not_found。

    remove 同时写 destructive_operations 审计行并清 file_instances（fail-soft）。
    """
    abs_p = str(ws.workspace_dir / "src" / "sample.py")
    ws.build_directory("src")

    rm = ws.remove_file(abs_p)
    assert rm.ok, f"remove_file 失败：{rm!r}"
    assert (rm.result or {}).get("removed") is True, rm.result

    # 磁盘上真的没了
    assert not (ws.workspace_dir / "src" / "sample.py").is_file(), \
        "remove_file 必须真的删除磁盘文件"

    # 读面转 path_not_found
    gone = ws.file_read(abs_p)
    assert not gone.ok and gone.code == "path_not_found", f"移除后应读不到：{gone!r}"


# ---------------------------------------------------------------- W3 目录构建

def test_w3_build_directory_contract(env, ws):
    """W3：build_directory 参数名/路径语义/失败面。

    - 参数名是 dir_path（harness 已归一）；
    - 绝对路径目录 → 正常扫描；
    - 不存在的目录 → path_not_found（fail-closed）；
    - 相对路径 → fail-closed 拒绝（path_escape/path_not_found），绝不静默
      成功（TP-11A 修复前是「静默 scanned=0」的易误判陷阱）。
    """
    ok = ws.build_directory("src")
    assert ok.ok, f"build_directory(abs src) 失败：{ok!r}"
    r = ok.result or {}
    assert r.get("scanned") == 2, f"src 下 2 个 .py：{r}"
    assert r.get("refreshed") == 2, r
    # 符号数 ≥2（alpha + gamma）；解析器对每文件还会记模块级条目，故不锁死等号
    assert (r.get("symbols") or 0) >= 2, f"src 下应索引到 alpha/gamma：{r}"
    assert r.get("calls") == 1, f"gamma→alpha 一条调用边：{r}"

    missing = ws.build_directory("nope_dir")
    assert not missing.ok and missing.code == "path_not_found", \
        f"不存在的目录应 path_not_found：{missing!r}"

    # 相对路径按 daemon CWD 解析（不相对 workspace 根拼接），解析结果落在
    # workspace 外或不存在 → fail-closed 拒绝。TP-11A 修复前是静默 scanned=0
    # （易误判为「构建成功但没文件」），已由根目录限制收敛为显式拒绝。
    rel = ws.fs_call("workspace.build_directory", dir_path="src", recursive=True)
    assert not rel.ok and rel.code in ("path_escape", "path_not_found"), \
        f"相对路径不得静默成功：{rel!r}"


# ---------------------------------------------------------------- W4 读面结构

def test_w4_read_surface_structure(env, ws):
    """W4：file.list / file.grep 的返回结构完整（缺字段会让上层静默取空）。"""
    lst = ws.file_list()
    assert lst.ok, f"file.list 失败：{lst!r}"
    rows = lst.result or []
    assert isinstance(rows, list) and rows, f"应返回非空列表：{rows}"
    for row in rows:
        assert {"rel_path", "size", "is_dir"} <= set(row), f"条目结构不全：{row}"
    rels = {r["rel_path"] for r in rows}
    assert {"src", "README.md"} <= rels, f"应含工作区条目：{rels}"

    g = ws.file_grep("def ")
    assert g.ok, f"file.grep 失败：{g!r}"
    gr = g.result or {}
    assert isinstance(gr.get("matches"), list), gr
    assert gr.get("count") == len(gr["matches"]), f"count 与 matches 必须一致：{gr}"
    for m in gr["matches"]:
        assert {"file_path", "line", "text"} <= set(m), f"匹配结构不全：{m}"

    empty = ws.file_grep("nomatch_zzz")
    assert empty.ok and (empty.result or {}).get("count") == 0, empty.result


# ------------------------------------------------- W5/W6 缺陷回归（path 剪枝）

def test_w5_file_list_path_subdir_must_work(env, ws):
    """W5（缺陷回归）：file.list 的 path 子目录剪枝必须可用。

    修复前：path 走 require_file=true → 传目录报 `path_not_found: 不是文件`，
    而该参数缺省值就是 workspace 根（目录），即子目录剪枝从未可用。
    """
    scoped = ws.file_list("src")
    assert scoped.ok, f"file.list(path=子目录) 应可用（修复前恒报不是文件）：{scoped!r}"
    rows = scoped.result or []
    rels = {r["rel_path"] for r in rows}
    assert "src/sample.py" in rels and "src/other.py" in rels, \
        f"应列出 src 下条目（相对根的路径）：{rels}"
    # 作用域确实收窄了（不再回根目录全量）
    assert "README.md" not in rels, f"不应越出 src：{rels}"


def test_w6_file_grep_path_subdir_must_work(env, ws):
    """W6（缺陷回归）：file.grep 的 path 子目录剪枝必须可用（同源 W5）。"""
    all_g = ws.file_grep("def ")
    scoped = ws.file_grep("def ", path=str(ws.workspace_dir / "src"))
    assert scoped.ok, f"file.grep(path=子目录) 应可用（修复前恒报不是文件）：{scoped!r}"
    sr = scoped.result or {}
    assert sr.get("count") >= 1, f"src 下应有 def 匹配：{sr}"
    for m in sr.get("matches", []):
        assert m["file_path"].startswith("src/"), f"不应越出 src：{m}"
    # 剪枝后不应多于全量
    assert sr["count"] <= (all_g.result or {}).get("count", 0), \
        f"剪枝结果不应超过全量：{sr} vs {all_g.result}"


# ---------------------------------------------------------------- W7 越界门禁

def test_w7_path_outside_workspace_is_rejected(env, ws):
    """W7：file.list 传工作区外路径必须 fail-closed（owner ACL 先于根内校验）。"""
    r = ws.fs_call("workspace.file.list", path="C:/Windows")
    assert not r.ok, f"工作区外路径必须拒绝：{r!r}"
    assert r.code in ("path_forbidden", "path_escape"), f"应为 ACL/越界拒绝：{r!r}"


# ---------------------------------------------------------------- W8 参数门禁

def test_w8_fail_closed_on_bad_params(env, ws):
    """W8：写面/读面的参数与存在性门禁（安全面，不可静默通过）。"""
    # 缺 file_path → invalid_params
    for m in ("workspace.file.refresh_file", "workspace.file.remove",
              "workspace.file.read"):
        r = ws.fs_call(m)
        assert not r.ok and r.code == "invalid_params", f"{m} 缺参应拒绝：{r!r}"

    # 不存在路径 → path_not_found
    ghost = str(ws.workspace_dir / "src" / "ghost.py")
    for m in ("workspace.file.refresh_file", "workspace.file.remove",
              "workspace.file.read"):
        r = ws.fs_call(m, file_path=ghost)
        assert not r.ok and r.code == "path_not_found", f"{m} 幽灵路径应拒绝：{r!r}"

    # file.health：文档合同是文件健康检查（TP-11A D4 修复后文件可用，
    # 此前 require_file 误用导致传文件恒报"不是目录"）
    h_file = ws.file_health("src/sample.py")
    assert h_file.ok, f"file.health(文件) 应可用：{h_file!r}"
    assert (h_file.result or {}).get("exists") is True and \
        (h_file.result or {}).get("readable") is True, h_file.result
    h_dir = ws.file_health("src")
    assert h_dir.ok, f"file.health(目录) 应可用：{h_dir!r}"
    assert (h_dir.result or {}).get("exists") is True, h_dir.result


# ---------------------------------------------------------------- W9 符号内容

def test_w9_symbol_content_by_rel_path(env, ws):
    """W9：symbol_content 按 DB 相对路径 + symbol_name 取回源码（唯一吃相对路径）。"""
    ws.build_directory("src")

    hit = ws.file_symbol_content("src/sample.py", "alpha")
    assert hit.ok, f"symbol_content 失败：{hit!r}"
    hr = hit.result or {}
    assert hr.get("content") == "def alpha():\n    return 1", hr
    assert hr.get("start_line") == 1 and hr.get("end_line") == 2, hr

    # 查不到 → ok 信封 + 空 result（不是错误码，固化该契约）
    miss = ws.file_symbol_content("src/sample.py", "nope_zz")
    assert miss.ok, f"未命中仍应是 ok 信封：{miss!r}"
    assert not miss.result, f"未命中应返回空：{miss.result}"
