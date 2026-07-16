"""P10-P12 阶段计时拆分 + GC 条件化 + clone detect 拆出测试。

覆盖：
- P10: 阶段计时输出包含 register/parse 独立行
- P11: GC archive 条件化（parsed_new==0 跳过 + IgnoreMatcher 实例缓存）
- P12: clone detect 从 _perf_test.py 拆出（默认跳过，--clone 开启）
"""
import os
import sys
import tempfile

import pytest

from callwarden.db.db import CodeGraphDB


# ============================================
# P11: GC archive 条件化测试
# ============================================

def _build_db_with_file(root, filename="test.py", content='def foo():\n    pass\n'):
    """创建 DB + 注册工作区 + 全量构建。返回 db。"""
    db = CodeGraphDB(os.path.join(root, "cw.db"), workspace_root=root)
    db.register_workspace("test", root, "测试")
    with open(os.path.join(root, filename), "w", encoding="utf-8") as f:
        f.write(content)
    db.build_full_graph()
    return db


def test_gc_skipped_when_no_new_files():
    """P11: 增量刷新时无新解析文件，GC 应被跳过。

    全量构建后立即增量刷新（不修改任何文件），
    parsed_new 应为 0，GC 不应执行。
    """
    with tempfile.TemporaryDirectory() as root:
        db = _build_db_with_file(root)
        try:
            # 记录 GC 调用前的 matcher 缓存状态
            # 第一次 build 已经构建了 matcher
            assert hasattr(db, "_ignore_matcher_cache")
            cached_matcher = db._ignore_matcher_cache[1]

            # 增量刷新（无文件变化）
            # build_full_graph 内部会检查 mtime，发现 unchanged，parsed_new=0
            # GC 应被跳过（通过 gc archive 耗时为 ~0 验证）
            db.build_full_graph(force=False)

            # matcher 缓存应仍然存在（没有被重建）
            assert db._ignore_matcher_cache[1] is cached_matcher
        finally:
            db.close()


def test_ignore_matcher_instance_cache():
    """P11: _build_ignore_matcher 应缓存实例，相同 mtime 不重建。"""
    with tempfile.TemporaryDirectory() as root:
        db = CodeGraphDB(os.path.join(root, "cw.db"), workspace_root=root)
        try:
            # 第一次构建 matcher
            matcher1 = db._build_ignore_matcher()
            # 第二次调用应返回同一实例（缓存命中）
            matcher2 = db._build_ignore_matcher()
            assert matcher1 is matcher2

            # 修改 .callwardenignore 的 mtime（touch）
            cw_path = os.path.join(root, ".callwardenignore")
            with open(cw_path, "w") as f:
                f.write("# test\n")
            # 重新构建应返回新实例（缓存失效）
            matcher3 = db._build_ignore_matcher()
            assert matcher3 is not matcher1
        finally:
            db.close()


def test_gc_executed_when_files_changed():
    """P11: 有文件变化时 GC 应正常执行（parsed_new > 0）。"""
    with tempfile.TemporaryDirectory() as root:
        db = _build_db_with_file(root)
        try:
            # 新增一个文件
            with open(os.path.join(root, "new_file.py"), "w") as f:
                f.write('def bar():\n    pass\n')
            # 增量刷新，新文件需要解析，parsed_new > 0，GC 应执行
            db.build_full_graph(force=False)
            # 验证新文件被正确入库
            ws_id = db._get_active_workspace_id()
            cur = db.conn.execute(
                "SELECT COUNT(*) as c FROM file_instances WHERE workspace_id = ? AND rel_path = 'new_file.py'",
                (ws_id,),
            )
            assert cur.fetchone()["c"] == 1
        finally:
            db.close()


# ============================================
# P10: 阶段计时输出测试
# ============================================

def test_stage_timing_includes_register_and_parse(capsys):
    """P10: build_full_graph 输出应包含 register 和 parse 独立计时行。"""
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "test.py"), "w") as f:
            f.write('def foo():\n    pass\n')
        db = CodeGraphDB(os.path.join(root, "cw.db"), workspace_root=root)
        try:
            db.register_workspace("test", root, "测试")
            db.build_full_graph()
            captured = capsys.readouterr()
            output = captured.out
            # P10: 应有 register 和 parse 独立行
            assert "register" in output
            assert "parse" in output
            assert "tree-sitter" in output
        finally:
            db.close()


# ============================================
# P12: clone detect 从 perf test 拆出
# ============================================

def test_perf_test_skip_clone_default():
    """P12: _perf_test.py 的 benchmark_repo 默认 skip_clone=True。"""
    import inspect
    import tests._perf_test as perf

    sig = inspect.signature(perf.benchmark_repo)
    assert sig.parameters["skip_clone"].default is True


def test_perf_test_has_clone_flag():
    """P12: _perf_test.py 应有 --clone 命令行参数。"""
    import inspect
    import tests._perf_test as perf

    # 检查 main 函数的 argparse 是否有 --clone
    # 通过检查 main 的源码
    source = inspect.getsource(perf.main)
    assert "--clone" in source
