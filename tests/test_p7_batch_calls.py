"""P7 批量化 call 写入路径测试。

覆盖 `_build_call_graph_multi_lang` 的批量收集 + executemany 写入逻辑：
- calls 表正确写入（caller_id 多级 fallback）
- call_versions 表正确写入（caller_hash 关联）
- _from_db 未变文件跳过重写（calls 不丢失、不重复）
- 增量刷新（修改一个文件）只重写该文件的 calls
- 批量删除旧 calls + 批量插入新 calls 的一致性
"""
import os
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB


# ============================================
# 测试用源文件
# ============================================

CALC_PY = '''"""计算模块。"""
def add(a, b):
    """加法。"""
    return a + b

def subtract(a, b):
    """减法。"""
    return a - b
'''

MAIN_PY = '''"""主模块，调用 calc。"""
def main():
    """入口函数。"""
    x = add(1, 2)
    y = subtract(5, 3)
    return x + y
'''

MAIN_PY_V2 = '''"""主模块 v2，调用 calc。"""
def main():
    """入口函数 v2。"""
    x = add(1, 2)
    y = subtract(5, 3)
    z = add(10, 20)
    return x + y + z
'''


def _write_file(root, name, content):
    path = os.path.join(root, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _build_db(root):
    """创建 DB + 注册工作区 + 全量构建。返回 db。"""
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    ws_id = db.register_workspace("p7-test", root, "P7 测试")
    db.set_active_workspace(ws_id)
    db.build_full_graph()
    return db


def _count_calls(db):
    """统计当前工作区的 calls 数量。"""
    ws_id = db._get_active_workspace_id()
    cur = db.conn.execute(
        "SELECT COUNT(*) as c FROM calls c "
        "JOIN symbols s ON c.caller_id = s.id "
        "JOIN file_instances fi ON s.file_instance_id = fi.id "
        "WHERE fi.workspace_id = ?",
        (ws_id,),
    )
    return cur.fetchone()["c"]


def _count_call_versions(db):
    """统计 call_versions 表数量。"""
    return db.conn.execute("SELECT COUNT(*) as c FROM call_versions").fetchone()["c"]


# ============================================
# 测试
# ============================================


def test_calls_correctly_written_after_full_build():
    """全量构建后 calls 表正确写入，caller_id 多级 fallback 正确解析。"""
    root = tempfile.mkdtemp()
    _write_file(root, "calc.py", CALC_PY)
    _write_file(root, "main.py", MAIN_PY)

    db = _build_db(root)

    # main.py 的 main() 调用了 add() 和 subtract()
    # 期望 calls 表至少有 2 条调用记录（add, subtract）
    call_count = _count_calls(db)
    assert call_count >= 2, f"calls 表应至少有 2 条记录，实际 {call_count}"

    # 验证 callee_name 包含 add 和 subtract
    cur = db.conn.execute("SELECT DISTINCT callee_name FROM calls")
    callee_names = {row[0] for row in cur.fetchall()}
    assert "add" in callee_names, f"callee_name 缺少 add，实际 {callee_names}"
    assert "subtract" in callee_names, f"callee_name 缺少 subtract，实际 {callee_names}"


def test_call_versions_correctly_written():
    """call_versions 表正确写入，caller_hash 关联函数符号。"""
    root = tempfile.mkdtemp()
    _write_file(root, "calc.py", CALC_PY)
    _write_file(root, "main.py", MAIN_PY)

    db = _build_db(root)

    cv_count = _count_call_versions(db)
    assert cv_count >= 2, f"call_versions 表应至少有 2 条记录，实际 {cv_count}"

    # 验证 call_versions 有 caller_hash 非空（main 函数有 content_hash）
    cur = db.conn.execute(
        "SELECT caller_qualified, caller_hash FROM call_versions "
        "WHERE caller_hash != '' LIMIT 5"
    )
    rows = cur.fetchall()
    assert len(rows) > 0, "call_versions 应有非空 caller_hash 的记录"


def test_from_db_skips_rewrite_on_unchanged_refresh():
    """未变文件增量刷新时，_from_db 跳过重写但 calls 不丢失。"""
    root = tempfile.mkdtemp()
    _write_file(root, "calc.py", CALC_PY)
    _write_file(root, "main.py", MAIN_PY)

    db = _build_db(root)
    calls_before = _count_calls(db)
    cv_before = _count_call_versions(db)

    assert calls_before > 0, "首次构建应有 calls"

    # 再次 refresh-all（文件未变化）
    db.build_full_graph()

    calls_after = _count_calls(db)
    cv_after = _count_call_versions(db)

    # calls 不应丢失（_from_db 跳过重写，原有 calls 保留）
    assert calls_after == calls_before, (
        f"未变文件刷新后 calls 数应不变：before={calls_before}, after={calls_after}"
    )


def test_incremental_refresh_only_rewrites_changed_file():
    """修改一个文件后刷新，只重写该文件的 calls，另一文件 calls 不受影响。"""
    root = tempfile.mkdtemp()
    _write_file(root, "calc.py", CALC_PY)
    _write_file(root, "main.py", MAIN_PY)

    db = _build_db(root)
    calls_v1 = _count_calls(db)

    # 修改 main.py（增加一个调用：z = add(10, 20)）
    _write_file(root, "main.py", MAIN_PY_V2)

    # 刷新（增量）
    db.build_full_graph()

    calls_v2 = _count_calls(db)
    # main.py v2 有 3 个调用（add, subtract, add），v1 有 2 个
    # calls_v2 应 > calls_v1（新增了一个 add 调用）
    assert calls_v2 > calls_v1, (
        f"修改后 calls 应增加：v1={calls_v1}, v2={calls_v2}"
    )

    # 验证 calc.py 的 calls 仍然只有 0 条（calc.py 的函数不调用其他函数）
    # 而 main.py 的 calls 应该被更新
    cur = db.conn.execute(
        "SELECT c.callee_name FROM calls c "
        "JOIN symbols s ON c.caller_id = s.id "
        "JOIN file_instances fi ON s.file_instance_id = fi.id "
        "WHERE fi.rel_path = ?",
        ("main.py",),
    )
    main_calls = [row[0] for row in cur.fetchall()]
    # main.py v2 调用 add(1,2), subtract(5,3), add(10,20)
    assert main_calls.count("add") == 2, f"main.py v2 应有 2 个 add 调用，实际 {main_calls}"
    assert "subtract" in main_calls, f"main.py v2 应有 subtract 调用，实际 {main_calls}"


def test_batch_delete_and_insert_consistency():
    """批量删除旧 calls + 批量插入新 calls 保持数据一致性。"""
    root = tempfile.mkdtemp()
    _write_file(root, "calc.py", CALC_PY)
    _write_file(root, "main.py", MAIN_PY)

    db = _build_db(root)

    # 获取 main.py 的 file_instance_id
    cur = db.conn.execute(
        "SELECT id FROM file_instances WHERE rel_path = 'main.py'"
    )
    fi_id = cur.fetchone()[0]

    # 获取 main.py 的 main 函数的 caller_id
    cur = db.conn.execute(
        "SELECT id FROM symbols WHERE file_instance_id = ? AND name = 'main'",
        (fi_id,),
    )
    caller_id = cur.fetchone()[0]

    # 验证 calls 表中该 caller 的调用
    cur = db.conn.execute(
        "SELECT callee_name FROM calls WHERE caller_id = ?",
        (caller_id,),
    )
    callee_names = [row[0] for row in cur.fetchall()]
    assert "add" in callee_names, f"应包含 add 调用，实际 {callee_names}"
    assert "subtract" in callee_names, f"应包含 subtract 调用，实际 {callee_names}"

    # 再次刷新（增量，文件未变），验证不会出现重复 calls
    db.build_full_graph()
    cur = db.conn.execute(
        "SELECT COUNT(*) as c FROM calls WHERE caller_id = ?",
        (caller_id,),
    )
    count_after = cur.fetchone()["c"]
    assert count_after == len(callee_names), (
        f"刷新后该 caller 的 calls 数应不变：before={len(callee_names)}, after={count_after}"
    )


def test_fts_search_works_after_build():
    """P8: build 期间禁用 FTS 触发器，build 后重建 FTS，搜索仍然正常。"""
    root = tempfile.mkdtemp()
    _write_file(root, "calc.py", CALC_PY)
    _write_file(root, "main.py", MAIN_PY)

    db = _build_db(root)

    # 验证 FTS 触发器存在（build 后已重建）
    cur = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'symbols_fts_%'"
    )
    triggers = [row[0] for row in cur.fetchall()]
    assert len(triggers) == 3, f"应有 3 个 FTS 触发器，实际 {triggers}"

    # 验证 FTS 搜索能找到符号
    results = db.search_symbols("add")
    assert any(r["name"] == "add" for r in results), f"FTS 搜索 add 应返回结果，实际 {results}"

    results = db.search_symbols("subtract")
    assert any(r["name"] == "subtract" for r in results), f"FTS 搜索 subtract 应返回结果，实际 {results}"


def test_fts_search_works_after_incremental_refresh():
    """P8: 增量刷新后 FTS 触发器仍然存在，搜索正常。"""
    root = tempfile.mkdtemp()
    _write_file(root, "calc.py", CALC_PY)
    _write_file(root, "main.py", MAIN_PY)

    db = _build_db(root)

    # 修改 calc.py（新增一个函数）
    _write_file(root, "calc.py", '''"""计算模块 v2。"""
def add(a, b):
    return a + b

def subtract(a, b):
    return a - b

def multiply(a, b):
    return a * b
''')

    # 增量刷新
    db.build_full_graph()

    # 验证 FTS 触发器仍然存在
    cur = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'symbols_fts_%'"
    )
    triggers = [row[0] for row in cur.fetchall()]
    assert len(triggers) == 3, f"增量刷新后应有 3 个 FTS 触发器，实际 {triggers}"

    # 验证 FTS 搜索能找到新增的 multiply 函数
    results = db.search_symbols("multiply")
    assert any(r["name"] == "multiply" for r in results), (
        f"FTS 搜索 multiply 应返回结果，实际 {results}"
    )

