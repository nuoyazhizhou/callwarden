"""函数级静态扫描能力补全测试。

覆盖 4 个缺口的补全：
1. 单元测试 case 关联（test_case_relations 表 + TestRelationMixin）
2. 测试稳定性追踪（test_runs 表 + import_test_results + get_test_stability）
3. 代码重复检测（cw clone list --symbol）
4. 变更-缺陷关联（get_defect_correlation_by_qn）

测试内容：
- test_case_relations 表存在 + 索引齐全
- test_runs 表存在 + 索引齐全
- _normalize_test_name 命名约定推断（testFoo → foo / foo_test → foo）
- build_test_relations 三阶推断（direct_call > name_convention > indirect）
- get_test_cases / get_tested_functions 正反向查询
- import_test_results JUnit XML 导入（含 matched 统计）
- get_test_stability 测试稳定性查询（pass_rate / recent_failures / by_test）
- get_symbol_issues 符号静态检查（无 findings 场景）
- get_defect_correlation_by_qn 变更-缺陷关联
- list_clones(symbol_id=...) 按符号过滤
"""

import os
import tempfile
import time

import pytest

from callwarden.db.db import CodeGraphDB
from callwarden.db.schema import SCHEMA_VERSION


def _db_with_workspace():
    """构造临时工作区数据库。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _seed_symbol(
    db,
    rel_path,
    symbol_name,
    content="pass",
    start_line=1,
    end_line=10,
    symbol_hash=None,
    qualified_name=None,
):
    """辅助：创建一个文件实例 + 符号 + 符号内容。

    同一路径重复调用时复用已存在的 file_instance（file_instances
    对 (workspace_id, rel_path) 有 UNIQUE 约束）。

    Returns:
        symbol id（int）
    """
    ws_id = db._get_active_workspace_id()
    ch = symbol_hash or f"hash_{symbol_name}_{rel_path}"
    qn = qualified_name or symbol_name

    db.conn.execute(
        "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) "
        "VALUES (?, 'python', ?, 0)",
        (ch, end_line - start_line + 1),
    )
    # 同一路径只插入一次 file_instance（UNIQUE 约束）
    db.conn.execute(
        "INSERT OR IGNORE INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, status, module_path) VALUES (?, ?, ?, ?, 0, 'parsed', '')",
        (ws_id, rel_path, os.path.join(db.workspace_root, rel_path), ch),
    )
    fi_id = db.conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id=? AND rel_path=?",
        (ws_id, rel_path),
    ).fetchone()[0]

    db.conn.execute(
        "INSERT OR REPLACE INTO symbol_contents (content_hash, name, kind, content, signature, "
        "has_comment, comment_content, qualified_name) "
        "VALUES (?, ?, 'fn', ?, '', 0, '', ?)",
        (ch, symbol_name, content, qn),
    )
    db.conn.execute(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, "
        "qualified_name, comment_status) VALUES (?, ?, ?, 'fn', ?, ?, ?, 'pending') "
        "ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET symbol_hash = excluded.symbol_hash",
        (fi_id, ch, symbol_name, start_line, end_line, qn),
    )
    sym_id = db.conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id=? AND name=? AND start_line=?",
        (fi_id, symbol_name, start_line),
    ).fetchone()[0]

    db.conn.commit()
    return sym_id


def _seed_call(db, caller_id, callee_id, caller_name="caller", callee_name="callee"):
    """辅助：创建一条调用关系。

    calls 表真实字段：caller_id, caller_name, caller_module, callee_name,
    callee_module, callee_qualified, callee_file, callee_id, call_line, is_cross_file
    """
    db.conn.execute(
        "INSERT INTO calls (caller_id, caller_name, caller_module, "
        "callee_name, callee_module, callee_qualified, callee_file, callee_id, "
        "call_line, is_cross_file) "
        "VALUES (?, ?, '', ?, '', '', '', ?, 0, 0)",
        (caller_id, caller_name, callee_name, callee_id),
    )
    db.conn.commit()


# ============================================
# 1. 表结构测试
# ============================================

def test_schema_version_is_34():
    """SCHEMA_VERSION >= 34（test_case_relations + test_runs 引入版本）。"""
    assert SCHEMA_VERSION >= 34


def test_test_case_relations_table_exists():
    """全新数据库包含 test_case_relations 表。"""
    db, _ = _db_with_workspace()
    try:
        cur = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='test_case_relations'"
        )
        assert cur.fetchone() is not None, "test_case_relations 表不存在"
    finally:
        db.close()


def test_test_runs_table_exists():
    """全新数据库包含 test_runs 表。"""
    db, _ = _db_with_workspace()
    try:
        cur = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='test_runs'"
        )
        assert cur.fetchone() is not None, "test_runs 表不存在"
    finally:
        db.close()


def test_test_case_relations_indexes_exist():
    """test_case_relations 表有 4 个索引（含 UNIQUE）。

    索引在 _create_indexes_after_build() 中创建（P12 优化：建表与建索引分离），
    全新测试库需显式触发索引创建。
    """
    db, _ = _db_with_workspace()
    try:
        db._create_indexes_after_build()
        cur = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='test_case_relations'"
        )
        names = {r[0] for r in cur.fetchall()}
        assert "idx_test_case_relations_workspace" in names
        assert "idx_test_case_relations_test" in names
        assert "idx_test_case_relations_tested" in names
        assert "idx_test_case_relations_unique" in names
    finally:
        db.close()


def test_test_runs_indexes_exist():
    """test_runs 表有 4 个索引。

    索引在 _create_indexes_after_build() 中创建，全新测试库需显式触发。
    """
    db, _ = _db_with_workspace()
    try:
        db._create_indexes_after_build()
        cur = db.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='test_runs'"
        )
        names = {r[0] for r in cur.fetchall()}
        assert "idx_test_runs_workspace" in names
        assert "idx_test_runs_test" in names
        assert "idx_test_runs_status" in names
        assert "idx_test_runs_ci" in names
    finally:
        db.close()


# ============================================
# 2. _normalize_test_name 命名约定推断
# ============================================

def test_normalize_test_name_camel_case():
    """testFoo → ['foo']（pattern 1 匹配，首字母小写化）"""
    db, _ = _db_with_workspace()
    try:
        candidates = db._normalize_test_name("testFoo")
        assert "foo" in candidates
    finally:
        db.close()


def test_normalize_test_name_camel_case_multi_word():
    """testParseFile → ['parseFile']（pattern 1 匹配，首字母小写化，无下划线不切分）"""
    db, _ = _db_with_workspace()
    try:
        candidates = db._normalize_test_name("testParseFile")
        assert "parseFile" in candidates
    finally:
        db.close()


def test_normalize_test_name_suffix():
    """foo_test → ['foo']（pattern 2 匹配后缀 _test）"""
    db, _ = _db_with_workspace()
    try:
        candidates = db._normalize_test_name("foo_test")
        assert "foo" in candidates
    finally:
        db.close()


def test_normalize_test_name_snake_with_upper():
    """test_Foo → ['foo']（pattern 1 匹配 test[_]?([A-Z].*)，首字母小写化）"""
    db, _ = _db_with_workspace()
    try:
        candidates = db._normalize_test_name("test_Foo")
        assert "foo" in candidates
    finally:
        db.close()


def test_normalize_test_name_no_match():
    """非 test 前缀 → 空列表"""
    db, _ = _db_with_workspace()
    try:
        candidates = db._normalize_test_name("helper_function")
        assert candidates == []
    finally:
        db.close()


# ============================================
# 3. build_test_relations 三阶推断
# ============================================

def test_build_test_relations_direct_call():
    """direct_call 推断：test_fn 直接调用 fn → high confidence"""
    db, _ = _db_with_workspace()
    try:
        # 被测函数
        fn_id = _seed_symbol(db, "src/module.py", "calculate", qualified_name="module.calculate")
        # 测试函数（在 tests/ 目录下，函数名以 test_ 开头）
        test_id = _seed_symbol(db, "tests/test_module.py", "test_calculate",
                               qualified_name="tests.test_module.test_calculate")
        # 创建调用关系：test_calculate → calculate
        _seed_call(db, test_id, fn_id, caller_name="test_calculate", callee_name="calculate")

        stats = db.build_test_relations(force=True)
        assert stats["total_test_fns"] >= 1
        assert stats.get("direct_call", 0) >= 1

        # 验证关联
        cases = db.get_test_cases("module.calculate")
        assert len(cases) >= 1
        assert cases[0]["match_method"] == "direct_call"
        assert cases[0]["confidence"] == "high"
    finally:
        db.close()


def test_build_test_relations_name_convention():
    """name_convention 推断：testFoo 匹配 foo（无调用关系，靠命名约定）"""
    db, _ = _db_with_workspace()
    try:
        # 被测函数 foo（无调用关系）
        _seed_symbol(db, "src/utils.py", "foo",
                     qualified_name="utils.foo")
        # 测试函数 testFoo（pattern 1 匹配 → foo，name_convention mid）
        _seed_symbol(db, "tests/test_utils.py", "testFoo",
                     qualified_name="tests.test_utils.testFoo")

        stats = db.build_test_relations(force=True)
        assert stats.get("name_convention", 0) >= 1

        cases = db.get_test_cases("utils.foo")
        assert len(cases) >= 1
        assert cases[0]["match_method"] == "name_convention"
        assert cases[0]["confidence"] == "mid"
    finally:
        db.close()


def test_get_tested_functions_reverse():
    """反向查询：test_fn 测了哪些函数"""
    db, _ = _db_with_workspace()
    try:
        fn_id = _seed_symbol(db, "src/app.py", "process_data",
                             qualified_name="app.process_data")
        test_id = _seed_symbol(db, "tests/test_app.py", "test_process_data",
                               qualified_name="tests.test_app.test_process_data")
        _seed_call(db, test_id, fn_id, caller_name="test_process_data", callee_name="process_data")

        db.build_test_relations(force=True)

        tested = db.get_tested_functions("tests.test_app.test_process_data")
        assert len(tested) >= 1
        assert tested[0]["tested_qualified_name"] == "app.process_data"
    finally:
        db.close()


def test_get_test_coverage_summary():
    """测试覆盖摘要：has_tests + test_count + high_confidence_count"""
    db, _ = _db_with_workspace()
    try:
        fn_id = _seed_symbol(db, "src/svc.py", "render",
                             qualified_name="svc.render")
        test_id = _seed_symbol(db, "tests/test_svc.py", "test_render",
                               qualified_name="tests.test_svc.test_render")
        _seed_call(db, test_id, fn_id, caller_name="test_render", callee_name="render")

        db.build_test_relations(force=True)

        summary = db.get_test_coverage_summary("svc.render")
        assert summary["has_tests"] is True
        assert summary["test_count"] >= 1
        assert summary["high_confidence_count"] >= 1
    finally:
        db.close()


# ============================================
# 4. import_test_results JUnit XML 导入
# ============================================

JUNIT_XML_SIMPLE = """<?xml version="1.0" encoding="UTF-8"?>
<testsuites>
  <testsuite name="tests.test_demo" tests="3" failures="1" errors="0" skipped="1" time="1.5">
    <testcase classname="tests.test_demo" name="test_pass" time="0.1"/>
    <testcase classname="tests.test_demo" name="test_fail" time="0.2">
      <failure type="AssertionError">AssertionError: expected 1, got 0</failure>
    </testcase>
    <testcase classname="tests.test_demo" name="test_skip" time="0.0">
      <skipped/>
    </testcase>
  </testsuite>
</testsuites>"""


def test_import_test_results_basic():
    """JUnit XML 导入：3 个 test case，1 passed / 1 failed / 1 skipped"""
    db, _ = _db_with_workspace()
    try:
        # 先创建匹配的 test 符号（同一路径下多个符号，_seed_symbol 会复用 file_instance）
        _seed_symbol(db, "tests/test_demo.py", "test_pass",
                     qualified_name="tests.test_demo.test_pass")
        _seed_symbol(db, "tests/test_demo.py", "test_fail",
                     qualified_name="tests.test_demo.test_fail")
        _seed_symbol(db, "tests/test_demo.py", "test_skip",
                     qualified_name="tests.test_demo.test_skip")

        stats = db.import_test_results(JUNIT_XML_SIMPLE, ci_run_id="ci-test-001")
        assert stats["total"] == 3
        assert stats["passed"] == 1
        assert stats["failed"] == 1
        assert stats["skipped"] == 1
        assert stats["matched"] >= 2  # test_pass 和 test_fail 应匹配
    finally:
        db.close()


def test_import_test_results_invalid_xml():
    """无效 XML 返回 parse_error"""
    db, _ = _db_with_workspace()
    try:
        stats = db.import_test_results("<not-valid-xml")
        assert "parse_error" in stats
    finally:
        db.close()


def test_import_test_results_with_ci_info():
    """导入时关联 ci_run_id 和 ci_url"""
    db, _ = _db_with_workspace()
    try:
        _seed_symbol(db, "tests/test_ci.py", "test_ci_info",
                     qualified_name="tests.test_ci.test_ci_info")

        xml = """<?xml version="1.0"?>
<testsuites><testsuite name="t" tests="1" failures="0" errors="0" skipped="0" time="0.1">
<testcase classname="tests.test_ci" name="test_ci_info" time="0.1"/>
</testsuite></testsuites>"""

        db.import_test_results(xml, ci_run_id="ci-123", ci_url="https://ci.example.com/123")

        # 验证 ci_run_id 写入
        row = db.conn.execute(
            "SELECT ci_run_id, ci_url FROM test_runs WHERE test_name='test_ci_info'"
        ).fetchone()
        assert row[0] == "ci-123"
        assert row[1] == "https://ci.example.com/123"
    finally:
        db.close()


# ============================================
# 5. get_test_stability 测试稳定性
# ============================================

def test_get_test_stability_no_runs():
    """无运行记录：返回 total_runs=0"""
    db, _ = _db_with_workspace()
    try:
        _seed_symbol(db, "src/noop.py", "noop", qualified_name="noop")
        result = db.get_test_stability("noop")
        assert result["total_runs"] == 0
        assert result["pass_rate"] == 0.0
    finally:
        db.close()


def test_get_test_stability_with_runs():
    """有运行记录：计算 pass_rate / recent_failures / by_test"""
    db, _ = _db_with_workspace()
    try:
        # 创建被测函数 + 测试函数 + 调用关系
        fn_id = _seed_symbol(db, "src/calc.py", "compute",
                             qualified_name="calc.compute")
        test_id = _seed_symbol(db, "tests/test_calc.py", "test_compute",
                               qualified_name="tests.test_calc.test_compute")
        _seed_call(db, test_id, fn_id, caller_name="test_compute", callee_name="compute")
        db.build_test_relations(force=True)

        # 导入 2 次运行结果（1 passed / 1 failed）
        xml1 = """<?xml version="1.0"?>
<testsuites><testsuite name="t" tests="1" failures="0" errors="0" skipped="0" time="0.1">
<testcase classname="tests.test_calc" name="test_compute" time="0.1"/>
</testsuite></testsuites>"""
        xml2 = """<?xml version="1.0"?>
<testsuites><testsuite name="t" tests="1" failures="1" errors="0" skipped="0" time="0.2">
<testcase classname="tests.test_calc" name="test_compute" time="0.2">
<failure type="AssertionError">expected 42, got 0</failure>
</testcase>
</testsuite></testsuites>"""
        db.import_test_results(xml1, ci_run_id="run-1")
        db.import_test_results(xml2, ci_run_id="run-2")

        result = db.get_test_stability("calc.compute")
        assert result["total_runs"] == 2
        assert result["pass_rate"] == 0.5  # 1/2
        assert len(result["recent_failures"]) == 1
        assert result["recent_failures"][0]["test_name"] == "test_compute"
        assert "test_compute" in result["by_test"]
        assert result["by_test"]["test_compute"]["total"] == 2
        assert result["by_test"]["test_compute"]["failed"] == 1
    finally:
        db.close()


# ============================================
# 6. get_symbol_issues 符号静态检查
# ============================================

def test_get_symbol_issues_no_findings():
    """无 findings：返回空列表（符号存在但无 file_symbol_versions 也返回空）"""
    db, _ = _db_with_workspace()
    try:
        _seed_symbol(db, "src/clean.py", "clean_fn", qualified_name="clean_fn")
        issues = db.get_symbol_issues("clean_fn")
        assert issues == []
    finally:
        db.close()


def test_get_symbol_issues_symbol_not_found():
    """符号不存在：返回空列表"""
    db, _ = _db_with_workspace()
    try:
        issues = db.get_symbol_issues("nonexistent.symbol")
        assert issues == []
    finally:
        db.close()


# ============================================
# 7. get_defect_correlation_by_qn 变更-缺陷关联
# ============================================

def test_get_defect_correlation_no_history():
    """无变更历史：返回 change_count=0 / defect_count=0"""
    db, _ = _db_with_workspace()
    try:
        _seed_symbol(db, "src/stable.py", "stable_fn", qualified_name="stable_fn")
        result = db.get_defect_correlation_by_qn("stable_fn")
        assert result["change_count"] == 0
        assert result["defect_count"] == 0
        assert result["defect_rate"] == 0.0
    finally:
        db.close()


def test_get_defect_correlation_symbol_not_found():
    """符号不存在：返回零值"""
    db, _ = _db_with_workspace()
    try:
        result = db.get_defect_correlation_by_qn("nonexistent.symbol")
        assert result["change_count"] == 0
        assert result["defect_count"] == 0
    finally:
        db.close()


# ============================================
# 8. clone list --symbol 过滤
# ============================================

def test_list_clones_with_symbol_filter():
    """list_clones(symbol_id=...) 按符号过滤"""
    db, _ = _db_with_workspace()
    try:
        ws_id = db._get_active_workspace_id()
        # 创建 2 个相同内容的符号（Type-1 clone）
        content = "def foo():\n    return 42\n"
        sym_a = _seed_symbol(db, "src/a.py", "foo", content=content,
                             symbol_hash="clone_hash_1", qualified_name="a.foo")
        sym_b = _seed_symbol(db, "src/b.py", "foo", content=content,
                             symbol_hash="clone_hash_1", qualified_name="b.foo")

        # 插入一条 clone_pair（真实字段：token_hash 单数，无 file_a/file_b）
        db.conn.execute(
            "INSERT INTO clone_pairs "
            "(workspace_id, symbol_a_id, symbol_b_id, clone_type, similarity, "
            "token_hash, lines_a, lines_b, detected_at) "
            "VALUES (?, ?, ?, 1, 1.0, 'th1', 2, 2, ?)",
            (ws_id, sym_a, sym_b, time.time()),
        )
        db.conn.commit()

        # 按 sym_a 过滤
        clones = db.list_clones(symbol_id=sym_a)
        assert len(clones) >= 1

        # 用一个不相关的 symbol_id 过滤 → 应返回空
        sym_c = _seed_symbol(db, "src/c.py", "bar", content="def bar(): pass",
                             symbol_hash="unique_hash_3", qualified_name="c.bar")
        clones_empty = db.list_clones(symbol_id=sym_c)
        assert len(clones_empty) == 0
    finally:
        db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
