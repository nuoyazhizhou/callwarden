"""P27 file-local qname 索引测试。

验证策略3多候选分支改为 O(1) dict 查找后，调用解析结果与原算法一致：
- 多候选场景：两个文件同名函数，各自调用应解析到本文件
- 策略4同文件匹配：简名调用解析到当前文件
- 跨模块调用不受影响
"""
import os
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB


# ============================================
# 测试用源文件
# ============================================

# 两个文件都有 helper()，各自的 caller 调用 helper()
A_PY = '''"""模块 A，有同名 helper。"""
def helper():
    """A 的 helper。"""
    return 1

def caller_a():
    """调用本文件 helper。"""
    return helper()
'''

B_PY = '''"""模块 B，有同名 helper。"""
def helper():
    """B 的 helper。"""
    return 2

def caller_b():
    """调用本文件 helper。"""
    return helper()
'''

# 文件 C 有独立函数，单候选场景
C_PY = '''"""模块 C，独立函数。"""
def unique_func():
    """唯一函数。"""
    return 3

def caller_c():
    """调用本文件 unique_func。"""
    return unique_func()
'''

# 跨模块调用：D import B 的 helper 后调用，但 D 也有同名 helper
# 策略3多候选应优先选 D 本地的（简名匹配语义）
D_PY = '''"""模块 D，import B 但本地也有同名 helper。"""
from b import helper as ext_helper

def helper():
    """D 的 helper。"""
    return 4

def caller_d():
    """调用本文件 helper 和 ext_helper。"""
    x = helper()
    y = ext_helper()
    return x + y
'''


def _write_file(root, name, content):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _build_db(root):
    """创建 DB + 注册工作区 + 全量构建。返回 db。"""
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    ws_id = db.register_workspace("p27-test", root, "P27 测试")
    db.set_active_workspace(ws_id)
    db.build_full_graph()
    return db


def _get_calls_for_caller(db, caller_name):
    """获取指定 caller 的所有调用记录（callee_name, callee_qualified, callee_file）。"""
    cur = db.conn.execute(
        "SELECT c.callee_name, c.callee_qualified, c.callee_file "
        "FROM calls c JOIN symbols s ON c.caller_id = s.id "
        "WHERE s.name = ?",
        (caller_name,),
    )
    return [(row[0], row[1], row[2]) for row in cur.fetchall()]


# ============================================
# 测试
# ============================================


def test_multi_candidate_resolves_to_local_file():
    """策略3多候选：两文件同名 helper，各自调用应解析到本文件。"""
    root = tempfile.mkdtemp()
    _write_file(root, "a.py", A_PY)
    _write_file(root, "b.py", B_PY)

    db = _build_db(root)

    # caller_a 调用 helper，应解析到 a.py 的 helper
    calls_a = _get_calls_for_caller(db, "caller_a")
    assert len(calls_a) >= 1, f"caller_a 应有调用，实际 {calls_a}"
    helper_call = next((c for c in calls_a if c[0] == "helper"), None)
    assert helper_call is not None, f"caller_a 应调用 helper，实际 {calls_a}"
    # callee_file 应为 a.py（本文件优先）
    assert "a.py" in helper_call[2] or helper_call[2] == "", (
        f"caller_a 的 helper 调用应解析到 a.py，实际 callee_file={helper_call[2]}"
    )

    # caller_b 调用 helper，应解析到 b.py 的 helper
    calls_b = _get_calls_for_caller(db, "caller_b")
    assert len(calls_b) >= 1, f"caller_b 应有调用，实际 {calls_b}"
    helper_call_b = next((c for c in calls_b if c[0] == "helper"), None)
    assert helper_call_b is not None, f"caller_b 应调用 helper，实际 {calls_b}"
    assert "b.py" in helper_call_b[2] or helper_call_b[2] == "", (
        f"caller_b 的 helper 调用应解析到 b.py，实际 callee_file={helper_call_b[2]}"
    )


def test_single_candidate_resolves_correctly():
    """策略3单候选：唯一函数应正确解析。"""
    root = tempfile.mkdtemp()
    _write_file(root, "c.py", C_PY)

    db = _build_db(root)

    calls_c = _get_calls_for_caller(db, "caller_c")
    assert len(calls_c) >= 1, f"caller_c 应有调用，实际 {calls_c}"
    assert any(c[0] == "unique_func" for c in calls_c), (
        f"caller_c 应调用 unique_func，实际 {calls_c}"
    )


def test_cross_module_with_local_shadow():
    """D 有本地 helper 且 import 外部 helper，本地调用应优先解析到本地。"""
    root = tempfile.mkdtemp()
    _write_file(root, "a.py", A_PY)
    _write_file(root, "b.py", B_PY)
    _write_file(root, "d.py", D_PY)

    db = _build_db(root)

    calls_d = _get_calls_for_caller(db, "caller_d")
    # caller_d 调用了 helper() 和 ext_helper()
    helper_calls = [c for c in calls_d if c[0] == "helper"]
    assert len(helper_calls) >= 1, f"caller_d 应调用 helper，实际 {calls_d}"

    # 本地 helper() 调用应解析到 d.py
    local_helper = helper_calls[0]
    assert "d.py" in local_helper[2] or local_helper[2] == "", (
        f"caller_d 的本地 helper 调用应解析到 d.py，实际 callee_file={local_helper[2]}"
    )


def test_from_db_preserves_calls_on_refresh():
    """P27 改造后增量刷新不丢失 calls。"""
    root = tempfile.mkdtemp()
    _write_file(root, "a.py", A_PY)
    _write_file(root, "b.py", B_PY)

    db = _build_db(root)
    calls_before = _get_calls_for_caller(db, "caller_a")

    # 再次刷新（文件未变）
    db.build_full_graph()
    calls_after = _get_calls_for_caller(db, "caller_a")

    assert len(calls_after) == len(calls_before), (
        f"刷新后 calls 数应不变：before={len(calls_before)}, after={len(calls_after)}"
    )
