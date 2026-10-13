"""TP-11X 外部依赖（Git / Semgrep / LSP / 向量）可达性验收。

在完全隔离的 daemon 上验证「外部依赖面」的**真实可达性与真实响应**：
每个 method 断言真实结构或真实拒绝码，不写「调用成功即通过」的占位断言。

🔴 X3 的 semgrep 保存缺陷（xfail 固化，非假 PASS）：
`run_semgrep_and_save` 永久返回 saved_findings=0，根因链（实测+源码+手动 INSERT
三重确认）：
1. semgrep 返回 `path = "ext\\unsafe.py"`（相对 workspace 根、Windows 反斜杠）；
2. save_findings 用 `path.trim_start_matches('/')` 归一化 → 不处理反斜杠；
3. 查 file_instances.rel_path 0 命中 —— 因为 rel_path 实际存的是**全小写绝对
   路径**（`c:/users/.../ext/unsafe.py`，与 TP-11L 的 L5 缺陷同源）；
4. file_instance_id 兜底 0；
5. INSERT 时 file_instance_id=0 违反 `FOREIGN KEY ... REFERENCES file_instances(id)`
   （daemon 开 PRAGMA foreign_keys=ON），而 **INSERT OR IGNORE 把 FK 违约一并
   静默吞掉** → execute 返回 Ok(0) → inserted 不增；
6. 结果：semgrep_scans 有行、semgrep_findings 永久空 → 所有 semgrep 读面
   （query.semgrep_stats / query.semgrep_findings）永久空。
修复后本例 XPASS。

🔴 MCP 工具名 ≠ RPC method：run_semgrep_scan / embed_symbols /
import_git_history 是 MCP/CLI 名，RPC 通道是 task.job_submit（job_type=
semgrep_scan / embed / git_history）；裸名调用 → method_not_found。
本域 X1 即该类缺陷的回归护栏（项目已踩过：handler 写了、match 臂注册了、
但收敛名单未登记 → 恒 method_not_found）。
"""

from __future__ import annotations

import pytest

from external_harness import ExternalEnv


@pytest.fixture(scope="module")
def env():
    e = ExternalEnv()
    ExternalEnv.wipe_run_root(e.run_root)
    e = ExternalEnv()
    e.start_daemon()
    try:
        e.write_fixture_sources()
        e.build_graph()
        e.job_executor_start()
        e.snapshot_publish()
        e.setup_workspace()
        # seed 两次确定性提交（gamma / delta），让 git 面有多个可断言对象，
        # 且 head 是 tp11x seed 而非底座 provenance（tp11g）
        e.seed_git_commit("ext/gamma.py", "def gamma():\n    return 3\n",
                          "tp11x: add gamma")
        e.seed_git_commit("ext/delta.py", "def delta():\n    return 4\n",
                          "tp11x: add delta")
        # 预热 semgrep（首次进程启动慢，曾观测到首调超时）
        e.run_semgrep()
        yield e
    finally:
        e.stop_daemon()


# ---------------------------------------------------------------- X1 可达性

def test_x1_convergence_reachability_matrix(env):
    """X1：收敛 RPC 方法可达；MCP/CLI 裸名必须 method_not_found（回归护栏）。"""
    # 裸名（MCP 工具名）→ method_not_found
    for bare in ("run_semgrep_scan", "embed_symbols", "import_git_history"):
        r = env.rpc(bare, {"workspace_instance_id": env.setup_workspace()[1]})
        assert not r.ok and r.code == "method_not_found", \
            f"{bare} 裸名应 method_not_found：{r!r}"

    # 真正的 RPC 通道：task.job_submit + job_type
    j = env.job_submit("git_history", sync=True)
    assert j.ok, f"job_submit(git_history) 应可达：{j!r}"
    assert (j.result or {}).get("job_type") == "git_history"
    assert (j.result or {}).get("status") == "completed"


def test_x1b_missing_params_fail_closed(env):
    """X1b：收敛方法缺 workspace_instance_id → invalid_params（不可静默通过）。"""
    for method, extra in [
        ("semantic_search", {"query": "alpha"}),
        ("find_similar_functions", {"qualified_name": "ext/beta.alpha"}),
        ("lsp_hover", {"file_path": "ext/alpha.py", "line": 1, "character": 4}),
        ("run_semgrep", {}),
    ]:
        r = env.rpc(method, extra)
        assert not r.ok and r.code == "invalid_params", \
            f"{method} 缺 workspace_instance_id 应拒绝：{r!r}"


# ---------------------------------------------------------------- X2 Git 面

def test_x2_git_history_import_and_read_surface(env):
    """X2：git_history 作业导入真实提交 → query.git_* 读面返回具体哈希。

    断言 daemon 响应与本机 git 直查的真相源一致（harness 的 local_head）。
    """
    # fixture 提交链：README 初始 + gamma + delta（harness 种子，确定性）
    head = env.local_head()
    assert head, "fixture git 仓必须有提交"

    imp = env.job_submit("git_history", sync=True)
    assert imp.ok, f"git_history 导入失败：{imp!r}"
    res = imp.result or {}
    assert res.get("status") == "completed"
    # 🔴 实测：只导入 head 一个提交（本地 3 个提交，commits_imported=1）。
    # 与 file_changes_imported=0 一并按实测固化；是否为缺陷待立项。
    assert res.get("result", {}).get("commits_imported") >= 1, res

    commits = env.git_commits()
    assert commits.ok, f"query.git_commits 失败：{commits!r}"
    rows = commits.result or []
    assert isinstance(rows, list) and rows, f"应返回非空列表：{rows}"
    first = rows[0]
    assert {"id", "commit_hash", "message", "author", "timestamp"} <= set(first), \
        f"提交结构不全：{first}"
    assert first["commit_hash"] == head, \
        f"读面 head 应与本机 git 一致：{first['commit_hash']} vs {head}"
    assert first["author"] == "tp11x", first

    stats = env.git_stats()
    assert stats.ok, f"query.git_stats 失败：{stats!r}"
    sr = stats.result or {}
    assert {"commit_count", "file_change_count", "change_types"} <= set(sr), sr
    assert sr["commit_count"] >= 1, sr


def test_x2b_git_read_surface_details_and_negatives(env):
    """X2b：commit_changes 详情结构 + 负向参数门禁。"""
    head = env.local_head()
    det = env.git_commit_changes(head)
    assert det.ok, f"git_commit_changes 失败：{det!r}"
    dr = det.result or {}
    assert {"commit", "file_changes"} <= set(dr), dr

    # 不存在的 hash → ok 信封 + commit null（不是错误码）
    bogus = env.git_commit_changes("0" * 40)
    assert bogus.ok, f"幽灵 hash 应是 ok 信封：{bogus!r}"
    assert (bogus.result or {}).get("commit") is None, bogus.result

    # 负向参数
    for method, args, code in [
        ("query.git_commits", {"limit": -1}, "invalid_params"),
        ("query.git_commits", {"offset": -1}, "invalid_params"),
        ("query.git_commit_changes", {}, "invalid_params"),
        ("query.file_history", {}, "invalid_params"),
    ]:
        params = {"workspace_instance_id": env.setup_workspace()[1]}
        params.update(args)
        r = env.rpc(method, params)
        assert not r.ok and r.code == code, f"{method} 负向应拒绝：{r!r}"


# ---------------------------------------------------------------- X3 Semgrep

def test_x3_semgrep_scan_returns_real_findings(env):
    """X3：run_semgrep 真实扫描出 finding（断言具体规则/路径/行号，非空壳）。"""
    env.write_semgrep_rule()
    r = env.run_semgrep()
    assert r.ok, f"run_semgrep 失败：{r!r}"
    rr = r.result or {}
    assert rr.get("success") is True, rr
    assert {"results", "severity_counts", "paths_scanned", "errors",
            "total_findings"} <= set(rr), rr
    assert rr["total_findings"] == len(rr["results"]), \
        f"total_findings 与 results 数必须一致：{rr}"
    # 本机 semgrep 真实可用（若不可用，本例降级为结构断言）
    if rr["total_findings"]:
        f0 = rr["results"][0]
        assert {"rule_id", "rule_name", "message", "severity", "path",
                "start_line", "language"} <= set(f0), f"finding 结构不全：{f0}"
        assert f0["path"].endswith("unsafe.py"), f"应命中 unsafe.py：{f0}"
        assert f0["severity"] in ("ERROR", "WARNING"), f0


def test_x3b_semgrep_bogus_config_reports_error(env):
    """X3b：不存在的规则文件 → success=false 且 errors 非空（不可静默成功）。"""
    r = env.run_semgrep(config="no/such/rule.yml")
    assert r.ok, f"run_semgrep 本身应返回信封：{r!r}"
    rr = r.result or {}
    assert rr.get("total_findings") == 0, rr
    assert rr.get("errors"), f"坏规则必须报 errors：{rr}"


@pytest.mark.xfail(
    reason="已知缺陷：save_findings 的 path 归一化不处理 Windows 反斜杠 + "
           "file_instances.rel_path 存的是全小写绝对路径（L5 同源）→ 关联 0 命中 → "
           "file_instance_id=0 违反 FK（daemon 开 PRAGMA foreign_keys=ON），而 "
           "INSERT OR IGNORE 把 FK 违约静默吞掉 → saved_findings 恒 0、"
           "semgrep 读面永久空。修复后本例 XPASS。")
def test_x3c_semgrep_save_persists_findings(env):
    """X3c：run_semgrep_and_save 必须把 finding 落库，读面随后可见。"""
    env.write_semgrep_rule()
    scan = env.run_semgrep()
    assert (scan.result or {}).get("total_findings", 0) >= 1, \
        f"前置：必须有 finding 才能验证保存：{scan.result}"

    save = env.run_semgrep_and_save()
    assert save.ok, f"run_semgrep_and_save 失败：{save!r}"
    sr = save.result or {}
    assert sr.get("saved_findings") >= 1, \
        f"保存的 finding 数应与扫描一致：{sr}"

    stats = env.semgrep_stats()
    assert stats.ok, f"query.semgrep_stats 失败：{stats!r}"
    assert (stats.result or {}).get("total_findings", 0) >= 1, \
        f"读面必须反映已保存的 finding：{stats.result}"

    findings = env.semgrep_findings()
    assert findings.ok, f"query.semgrep_findings 失败：{findings!r}"
    assert (findings.result or []), "读面必须能列出已保存的 finding"


def test_x3d_semgrep_read_surface_structure(env):
    """X3d：semgrep 读面结构完整（即使为空也必须返回正确形状）。"""
    stats = env.semgrep_stats()
    assert stats.ok, f"query.semgrep_stats 失败：{stats!r}"
    sr = stats.result or {}
    assert {"by_language", "by_rule", "by_severity", "by_symbol",
            "total_findings"} <= set(sr), f"stats 结构不全：{sr}"

    findings = env.semgrep_findings(severity="ERROR")
    assert findings.ok, f"query.semgrep_findings 失败：{findings!r}"
    assert isinstance(findings.result, list), findings.result

    incr = env.scan_semgrep_incremental()
    assert incr.ok, f"scan_semgrep_incremental 失败：{incr!r}"
    ir = incr.result or {}
    assert {"changed_files", "scanned_files", "saved_findings",
            "stale_file_ids", "success", "total_findings"} <= set(ir), ir


# ---------------------------------------------------------------- X4 LSP 面

def test_x4_lsp_check_available_is_real_probe(env):
    """X4：lsp_check_available 是真实 where 探测（断言结构，不断言具体语言值）。"""
    r = env.lsp_check_available()
    assert r.ok, f"lsp_check_available 失败：{r!r}"
    rr = r.result or {}
    assert {"available_servers", "total_available"} <= set(rr), rr
    assert rr["total_available"] == sum(rr["available_servers"].values()), \
        f"total_available 必须与可用数一致：{rr}"

    # 单语言探测：结构收窄到该语言
    one = env.lsp_check_available(language="rust")
    assert one.ok, f"lsp_check_available(rust) 失败：{one!r}"
    assert sorted((one.result or {}).get("available_servers", {}).keys()) == ["rust"], \
        one.result
    # 未知语言：fail-soft 回 false，不报错
    none = env.lsp_check_available(language="nonesuch")
    assert none.ok, f"未知语言应 fail-soft：{none!r}"
    assert (none.result or {}).get("total_available") == 0, none.result


def test_x4b_lsp_operations_are_fail_soft_without_server(env):
    """X4b：无 LSP 服务端时 5 个操作 fail-soft 返回空结构（available=false）。"""
    for op, keys in [
        ("lsp_hover", {"available", "contents", "file_path", "line", "character"}),
        ("lsp_definition", {"available", "definitions"}),
        ("lsp_references", {"available", "references", "total"}),
        ("lsp_diagnostics", {"available", "diagnostics", "file_path", "total"}),
        ("lsp_completion", {"available", "completions", "total"}),
    ]:
        r = env.rpc(op, {
            "workspace_instance_id": env.setup_workspace()[1],
            "file_path": "ext/alpha.py", "line": 1, "character": 4})
        assert r.ok, f"{op} 应 fail-soft 不报错：{r!r}"
        rr = r.result or {}
        assert keys <= set(rr), f"{op} 结构不全：{rr}"
        assert rr.get("available") is False, \
            f"{op} 无服务端时 available 必须 false：{rr}"


# ---------------------------------------------------------------- X5 向量面

def test_x5_embed_job_is_idempotent_and_forceable(env):
    """X5：embed 作业确定性 hash-v1；重复跳过；force 重算。"""
    first = env.job_submit("embed", sync=True)
    assert first.ok, f"embed 作业失败：{first!r}"
    fr = (first.result or {}).get("result", {})
    assert fr.get("model") == "hash-v1", fr
    assert fr.get("total", 0) >= 1, f"fixture 符号应被嵌入：{fr}"
    ok1 = fr.get("ok", 0)
    assert ok1 >= 1, fr

    # 重复 → 全部跳过（幂等）
    again = env.job_submit("embed", sync=True)
    ar = (again.result or {}).get("result", {})
    assert ar.get("skipped", 0) >= 1, f"重复嵌入应跳过：{ar}"

    # force → 重新计算
    force = env.job_submit("embed", sync=True, force=True)
    vr = (force.result or {}).get("result", {})
    assert vr.get("ok", 0) >= 1, f"force 应重算：{vr}"


def test_x5b_semantic_search_returns_ranked_results(env):
    """X5b：semantic_search 返回排序结果；top_k 生效。"""
    full = env.semantic_search("alpha")
    assert full.ok, f"semantic_search 失败：{full!r}"
    rows = full.result or []
    assert isinstance(rows, list) and rows, f"应返回结果：{rows}"
    for row in rows:
        assert {"qualified_name", "file_path", "start_line",
                "similarity", "summary"} <= set(row), f"结果结构不全：{row}"
    # 相似度降序
    sims = [r["similarity"] for r in rows]
    assert sims == sorted(sims, reverse=True), f"应按相似度降序：{sims}"

    limited = env.semantic_search("alpha", top_k=1)
    assert len(limited.result or []) <= 1, \
        f"top_k 必须限制结果数：{limited.result}"
    # 空查询 → 空结果（不报错）
    empty = env.semantic_search("")
    assert empty.ok and (empty.result or []) == [], empty.result


def test_x5c_find_similar_functions_contract(env):
    """X5c：find_similar 按 qualified_name 检索；短名 0 命中、全名有结果。"""
    # 🔴 qualified_name 是全路径（含全小写绝对路径前缀，L5 同源），短名查不到
    short = env.find_similar_functions("beta")
    assert short.ok, f"find_similar 失败：{short!r}"
    assert (short.result or []) == [], \
        f"短名应 0 命中（qualified_name 是全路径）：{short.result}"

    names = env.semantic_names()
    assert names, "fixture 必须有嵌入符号"
    full_name = names[0]
    hit = env.find_similar_functions(full_name)
    assert hit.ok, f"find_similar(全名) 失败：{hit!r}"
    hits = hit.result or []
    assert isinstance(hits, list), hits
    # 排除自身后仍有近邻（fixture 里 alpha/alpha_wrapper/beta 互为近邻）
    others = [h for h in hits if h["qualified_name"] != full_name]
    assert others, f"全名查询应有近邻结果：{hits}"
    for h in others:
        assert {"qualified_name", "file_path", "similarity",
                "start_line", "summary"} <= set(h), f"结构不全：{h}"

    # 不存在的符号 → 空结果（ok 信封）
    miss = env.find_similar_functions("nope_zz_nope")
    assert miss.ok and (miss.result or []) == [], miss.result
