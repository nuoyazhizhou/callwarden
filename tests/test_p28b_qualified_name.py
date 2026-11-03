#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P28 测试：get_callers/get_callees 增加 qualified_name 参数

测试目标：
1. 默认行为（qualified_name=None）：对齐原接口，按短名匹配（向后兼容）
2. 传入 qualified_name：精确匹配唯一符号，避免跨模块误匹配
3. Rust 短路路径与 SQL 降级路径结果一致
4. 多模块同名函数场景验证（核心痛点）

场景设计：
- 两个模块都有 init() 函数时，短名查询返回所有模块的调用者
- 传入 qualified_name 后只返回指定模块的调用者
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

# 添加 rust_ext/target/pyinstall 到 PYTHONPATH（maturin build 产物）
_rust_pyinstall = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rust_ext", "target", "pyinstall"
)
if _rust_pyinstall not in sys.path:
    sys.path.insert(0, _rust_pyinstall)

# 添加项目根目录到 path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def _has_rust_ext() -> bool:
    try:
        import callwarden_core  # noqa: F401
        return True
    except ImportError:
        return False


# 两个模块都有 init() 函数，模拟跨模块同名场景
MOD_A_PY = '''"""模块 A，有 init 函数。"""
def init():
    """A 的 init。"""
    return 1

def foo():
    """调用本模块 init。"""
    return init()
'''

MOD_B_PY = '''"""模块 B，也有 init 函数。"""
def init():
    """B 的 init。"""
    return 2

def bar():
    """调用本模块 init。"""
    return init()
'''


def _write_file(root, name, content):
    path = os.path.join(root, name)
    os.makedirs(os.path.dirname(path), exist_ok=True) if os.path.dirname(name) else None
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _build_db(root):
    """创建 DB + 注册工作区 + 全量构建。返回 db。"""
    from callwarden.db.db import CodeGraphDB
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    ws_id = db.register_workspace("p28-test", root, "P28 测试")
    db.set_active_workspace(ws_id)
    db.build_full_graph()
    return db


class TestP28QualifiedName(unittest.TestCase):
    """P28：qualified_name 参数避免短名跨模块误匹配"""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix="cw_p28_test_")
        _write_file(cls.tmpdir, "mod_a.py", MOD_A_PY)
        _write_file(cls.tmpdir, "mod_b.py", MOD_B_PY)
        cls.db = _build_db(cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        # 每个测试前失效缓存，确保从干净状态开始
        self.db._invalidate_graph_store()

    # ----------------------------------------------------------------
    # 默认行为（向后兼容）
    # ----------------------------------------------------------------

    def test_get_callers_default_short_name(self):
        """默认（qualified_name=None）按短名匹配，返回所有模块的调用者"""
        callers = self.db.get_callers("init")
        # 两个模块都有 init 被 foo/bar 调用，短名应返回 2 条
        self.assertEqual(len(callers), 2, "短名查询应返回两个模块的调用者")

    def test_get_callees_default_short_name(self):
        """默认（qualified_name=None）按短名匹配 caller"""
        # foo 调 init
        callees_foo = self.db.get_callees("foo")
        self.assertEqual(len(callees_foo), 1, "foo 应有 1 个 callee")
        # 短名 init 可能匹配 mod_a.init 或 mod_b.init
        self.assertEqual(callees_foo[0]["callee_name"], "init")

    # ----------------------------------------------------------------
    # qualified_name 精确匹配（核心场景）
    # ----------------------------------------------------------------

    def test_get_callers_with_qualified_name_filters(self):
        """传入 qualified_name 后只返回指定模块的调用者"""
        # 短名返回 2 个（跨模块），qname 过滤后只返回 1 个
        all_callers = self.db.get_callers("init")
        self.assertEqual(len(all_callers), 2)

        mod_a_callers = self.db.get_callers("init", qualified_name="mod_a.init")
        self.assertEqual(len(mod_a_callers), 1, "qname=mod_a.init 应只返回 1 个调用者")
        self.assertEqual(mod_a_callers[0]["caller_name"], "foo")

        mod_b_callers = self.db.get_callers("init", qualified_name="mod_b.init")
        self.assertEqual(len(mod_b_callers), 1, "qname=mod_b.init 应只返回 1 个调用者")
        self.assertEqual(mod_b_callers[0]["caller_name"], "bar")

    def test_get_callees_with_qualified_name(self):
        """传入 qualified_name 后精确到唯一 caller"""
        # foo 的 qname 是 mod_a.foo
        callees = self.db.get_callees("foo", qualified_name="mod_a.foo")
        self.assertEqual(len(callees), 1)
        self.assertEqual(callees[0]["callee_name"], "init")

    def test_get_callers_qualified_name_not_found(self):
        """传入不存在的 qualified_name 应返回空结果（避免短名误匹配）"""
        callers = self.db.get_callers("init", qualified_name="nonexistent.module.init")
        self.assertEqual(len(callers), 0, "qname 不存在时应返回空，不回退到短名")

    def test_get_callees_qualified_name_not_found(self):
        """传入不存在的 qualified_name 应返回空结果"""
        callees = self.db.get_callees("foo", qualified_name="nonexistent.foo")
        self.assertEqual(len(callees), 0, "qname 不存在时应返回空")

    # ----------------------------------------------------------------
    # Rust 短路与 SQL 降级路径一致性
    # ----------------------------------------------------------------

    @unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
    def test_rust_and_sql_paths_agree_with_qualified_name(self):
        """Rust 短路路径与 SQL 降级路径在 qualified_name 下结果一致"""
        # Rust 路径
        self.db._invalidate_graph_store()
        rust_callers = self.db.get_callers("init", qualified_name="mod_a.init")
        rust_count = len(rust_callers)

        # SQL 降级路径（临时让 _get_graph_store 返回 None）
        self.db._graph_store = None
        self.db._graph_store_dirty = False
        original_get = self.db._get_graph_store
        self.db._get_graph_store = lambda: None
        try:
            sql_callers = self.db.get_callers("init", qualified_name="mod_a.init")
            sql_count = len(sql_callers)
        finally:
            self.db._get_graph_store = original_get

        self.assertEqual(rust_count, sql_count, "Rust 与 SQL 路径结果数应一致")
        self.assertEqual(rust_count, 1, "mod_a.init 应只有 1 个调用者")


SHARED_FN_PY = '''"""共享夹具：同名 helper/main 用于跨 workspace 隔离验证。"""
def helper():
    """被调用函数。"""
    return 1

def main():
    """调用 helper。"""
    return helper()
'''


class TestWorkspaceScopedSqlFallback(unittest.TestCase):
    """C2（backlog §6.5）：本地 SQL 降级路径按 workspace 归属过滤

    两个 workspace 各自有同名 `helper`/`main`，强制走 SQL 降级路径后，
    查询当前 workspace 不得返回另一 workspace 的同名调用边。
    """

    @classmethod
    def setUpClass(cls):
        from callwarden.db.db import CodeGraphDB

        cls.tmpdir = tempfile.mkdtemp(prefix="cw_c2_ws_test_")
        ws1_root = os.path.join(cls.tmpdir, "ws1")
        ws2_root = os.path.join(cls.tmpdir, "ws2")
        os.makedirs(ws1_root, exist_ok=True)
        os.makedirs(ws2_root, exist_ok=True)
        _write_file(ws1_root, "alpha.py", SHARED_FN_PY)
        _write_file(ws2_root, "beta.py", SHARED_FN_PY)

        cls.db = CodeGraphDB(os.path.join(cls.tmpdir, "callwarden.db"),
                             workspace_root=ws1_root)
        cls.ws1 = cls.db.register_workspace("c2-ws1", ws1_root, "C2 ws1")
        cls.db.set_active_workspace(cls.ws1)
        cls.db.build_full_graph()

        cls.ws2 = cls.db.register_workspace("c2-ws2", ws2_root, "C2 ws2")
        cls.db.set_active_workspace(cls.ws2)
        cls.db.build_full_graph()

    @classmethod
    def tearDownClass(cls):
        try:
            cls.db.close()
        except Exception:
            pass
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def _sql_only(self, workspace_id):
        """切到目标 workspace 并强制 SQL 降级路径（禁用 Rust GraphStore 短路）"""
        self.db.set_active_workspace(workspace_id)
        original_get = self.db._get_graph_store
        self.db._get_graph_store = lambda: None
        self.addCleanup(setattr, self.db, "_get_graph_store", original_get)

    def test_get_callers_short_name_is_workspace_scoped(self):
        """短名 callers 只返回当前 workspace 的调用边"""
        self._sql_only(self.ws1)
        callers = self.db.get_callers("helper")
        self.assertEqual(len(callers), 1, "ws1 短名 callers 不应包含 ws2 边")
        self.assertEqual(callers[0]["caller_file"], "alpha.py")

        self.db.set_active_workspace(self.ws2)
        callers_ws2 = self.db.get_callers("helper")
        self.assertEqual(len(callers_ws2), 1, "ws2 短名 callers 不应包含 ws1 边")
        self.assertEqual(callers_ws2[0]["caller_file"], "beta.py")

    def test_get_callees_short_name_is_workspace_scoped(self):
        """短名 callees 只返回当前 workspace 的调用边"""
        self._sql_only(self.ws1)
        callees = self.db.get_callees("main")
        self.assertEqual(len(callees), 1, "ws1 短名 callees 不应包含 ws2 边")
        self.assertEqual(callees[0]["callee_name"], "helper")

        self.db.set_active_workspace(self.ws2)
        callees_ws2 = self.db.get_callees("main")
        self.assertEqual(len(callees_ws2), 1, "ws2 短名 callees 不应包含 ws1 边")

    def test_get_callees_qn_is_workspace_scoped(self):
        """QN 分支在非归属 workspace 下返回空（不回退到别 ws 的同名符号）"""
        self._sql_only(self.ws1)
        self.assertEqual(
            self.db.get_callees("main", qualified_name="beta.main"), [],
            "ws1 查询 ws2 的 QN caller 应返回空")

        self.db.set_active_workspace(self.ws2)
        self.assertEqual(
            self.db.get_callees("main", qualified_name="alpha.main"), [],
            "ws2 查询 ws1 的 QN caller 应返回空")

    def test_get_callers_auto_qn_fallback_stays_in_workspace(self):
        """自动识别 QN 的短名回退仍受 workspace 约束"""
        self._sql_only(self.ws1)
        callers = self.db.get_callers("beta.helper")
        # 自动识别 QN 允许降级短名（QN 未入库），但降级结果仍只能来自当前 workspace
        self.assertEqual(
            {c["caller_file"] for c in callers}, {"alpha.py"},
            "自动降级短名的结果不得引用其它 workspace 的文件")
        self.assertEqual(callers, self.db.get_callers("helper"))


if __name__ == "__main__":
    unittest.main()
