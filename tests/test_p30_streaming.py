"""P30: 流式回传（ParseResultPool）单元测试。

验证 Rust 侧 ParseResultPool 的 API：
- len() / get_at() / get_by_path() / stats()
- 迭代器协议 __iter__ / __next__（支持 for r in pool）
- 与 batch_parse_c_files（非 pool 版）结果一致
- 错误处理（越界、路径不存在）
- 可重复迭代
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)


def _has_rust_ext() -> bool:
    try:
        import callwarden_core  # noqa: F401
        return True
    except ImportError:
        return False


# C 源码样例：含函数、结构体、调用关系
_SAMPLE_C = """\
#include <stdio.h>
#include <stdlib.h>

struct Point {
    int x;
    int y;
};

int add(int a, int b) {
    return a + b;
}

int main(int argc, char** argv) {
    struct Point p;
    p.x = 1;
    p.y = 2;
    int sum = add(p.x, p.y);
    printf("sum = %d\\n", sum);
    return 0;
}

void helper(void) {
    printf("helper called\\n");
    main(0, NULL);
}
"""


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestParseResultPool(unittest.TestCase):
    """P30: ParseResultPool 基础 API 测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p30_test_")
        self.sample_path = os.path.join(self.tmpdir, "sample.c")
        with open(self.sample_path, "w", encoding="utf-8") as f:
            f.write(_SAMPLE_C)
        # 第二个文件
        self.sample2_path = os.path.join(self.tmpdir, "sample2.c")
        with open(self.sample2_path, "w", encoding="utf-8") as f:
            f.write("int foo(void) { return 42; }\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_pool(self):
        """创建包含 2 个文件的 pool"""
        from callwarden_core import batch_parse_c_files_pool
        return batch_parse_c_files_pool(
            [(self.sample_path, "test.sample"),
             (self.sample2_path, "test.sample2")],
            num_threads=2,
        )

    # ------------------------------------------------------------------
    # len() / get_at()
    # ------------------------------------------------------------------

    def test_pool_len(self):
        """pool.len() 返回文件数"""
        pool = self._make_pool()
        self.assertEqual(pool.len(), 2)

    def test_pool_get_at_returns_dict(self):
        """pool.get_at(0) 返回有效 dict，包含必要字段"""
        pool = self._make_pool()
        result = pool.get_at(0)
        self.assertEqual(result["language"], "c")
        self.assertGreater(result["total_lines"], 10)
        # 符号列表
        func_names = [s["name"] for s in result["symbols"] if s["kind"] == "fn"]
        self.assertIn("add", func_names)
        self.assertIn("main", func_names)

    def test_pool_get_at_out_of_range(self):
        """pool.get_at(越界) 抛 IndexError"""
        pool = self._make_pool()
        with self.assertRaises(IndexError):
            pool.get_at(100)

    def test_pool_get_at_each_index(self):
        """get_at(0) 和 get_at(1) 返回不同文件的结果"""
        pool = self._make_pool()
        r0 = pool.get_at(0)
        r1 = pool.get_at(1)
        # 第一个文件有多个符号，第二个文件只有 1 个函数
        funcs0 = [s["name"] for s in r0["symbols"] if s["kind"] == "fn"]
        funcs1 = [s["name"] for s in r1["symbols"] if s["kind"] == "fn"]
        self.assertIn("add", funcs0)
        self.assertEqual(funcs1, ["foo"])

    # ------------------------------------------------------------------
    # get_by_path()
    # ------------------------------------------------------------------

    def test_pool_get_by_path(self):
        """pool.get_by_path() 按 abs_path 查找"""
        pool = self._make_pool()
        result = pool.get_by_path(self.sample_path)
        self.assertEqual(result["language"], "c")
        funcs = [s["name"] for s in result["symbols"] if s["kind"] == "fn"]
        self.assertIn("add", funcs)

    def test_pool_get_by_path_not_found(self):
        """pool.get_by_path(不存在) 抛 KeyError"""
        pool = self._make_pool()
        with self.assertRaises(KeyError):
            pool.get_by_path("/nonexistent/path.c")

    # ------------------------------------------------------------------
    # stats()
    # ------------------------------------------------------------------

    def test_pool_stats(self):
        """pool.stats() 返回 (files, symbols, calls, errors) 四元组"""
        pool = self._make_pool()
        stats = pool.stats()
        self.assertEqual(len(stats), 4)
        files, symbols, calls, errors = stats
        self.assertEqual(files, 2)
        self.assertGreater(symbols, 0)
        self.assertGreaterEqual(calls, 0)
        self.assertEqual(errors, 0)

    def test_pool_stats_includes_errors(self):
        """pool 包含不存在的文件时，stats() 的 errors > 0"""
        from callwarden_core import batch_parse_c_files_pool
        pool = batch_parse_c_files_pool(
            [(self.sample_path, "test.sample"),
             ("/nonexistent/path.c", "test.bad")],
            num_threads=2,
        )
        files, symbols, calls, errors = pool.stats()
        self.assertEqual(files, 2)
        self.assertEqual(errors, 1)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestParseResultPoolIteration(unittest.TestCase):
    """P30: ParseResultPool 迭代器协议测试"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p30_iter_")
        self.files = []
        for i in range(5):
            path = os.path.join(self.tmpdir, f"f{i}.c")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"int func_{i}(void) {{ return {i}; }}\n")
            self.files.append((path, f"mod.{i}"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _make_pool(self):
        from callwarden_core import batch_parse_c_files_pool
        return batch_parse_c_files_pool(self.files, num_threads=4)

    def test_iter_yields_all_files(self):
        """for r in pool 遍历所有文件，顺序与输入一致"""
        pool = self._make_pool()
        results = list(pool)
        self.assertEqual(len(results), 5)
        # 验证每个文件的函数名
        for i, r in enumerate(results):
            funcs = [s["name"] for s in r["symbols"] if s["kind"] == "fn"]
            self.assertEqual(funcs, [f"func_{i}"])

    def test_iter_preserves_order(self):
        """迭代顺序与输入文件顺序一致"""
        pool = self._make_pool()
        names = []
        for r in pool:
            funcs = [s["name"] for s in r["symbols"] if s["kind"] == "fn"]
            names.append(funcs[0])
        self.assertEqual(names, [f"func_{i}" for i in range(5)])

    def test_re_iterate_after_exhaustion(self):
        """迭代结束后可再次迭代（游标重置）"""
        pool = self._make_pool()
        first_pass = list(pool)
        second_pass = list(pool)
        self.assertEqual(len(first_pass), len(second_pass))
        # 两次遍历结果一致
        for a, b in zip(first_pass, second_pass):
            fa = [s["name"] for s in a["symbols"] if s["kind"] == "fn"]
            fb = [s["name"] for s in b["symbols"] if s["kind"] == "fn"]
            self.assertEqual(fa, fb)

    def test_iter_stats_consistent(self):
        """迭代次数与 stats() 的 files 数一致"""
        pool = self._make_pool()
        files, symbols, calls, errors = pool.stats()
        iter_count = sum(1 for _ in pool)
        self.assertEqual(iter_count, files)

    def test_get_at_and_iter_consistent(self):
        """get_at(i) 与迭代结果一致"""
        pool = self._make_pool()
        iter_results = list(pool)
        for i, iter_r in enumerate(iter_results):
            at_r = pool.get_at(i)
            ia = [s["name"] for s in iter_r["symbols"] if s["kind"] == "fn"]
            aa = [s["name"] for s in at_r["symbols"] if s["kind"] == "fn"]
            self.assertEqual(ia, aa)


@unittest.skipUnless(_has_rust_ext(), "callwarden_core 未安装")
class TestPoolVsBatchConsistency(unittest.TestCase):
    """P30: 验证 pool 版与 batch 版结果一致（流式不丢数据）"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cw_p30_consistency_")
        self.files = []
        for i in range(8):
            path = os.path.join(self.tmpdir, f"f{i}.c")
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"int f{i}(void) {{ return f{i}(); }}\n")
            self.files.append((path, f"mod.{i}"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_pool_and_batch_same_symbols(self):
        """pool 和 batch 提取的符号数一致"""
        from callwarden_core import batch_parse_c_files, batch_parse_c_files_pool

        batch_results = batch_parse_c_files(self.files, num_threads=4)
        pool = batch_parse_c_files_pool(self.files, num_threads=4)

        # 符号总数一致
        batch_symbols = sum(len(r["symbols"]) for r in batch_results)
        _, pool_symbols, _, _ = pool.stats()
        self.assertEqual(batch_symbols, pool_symbols)

    def test_pool_and_batch_same_calls(self):
        """pool 和 batch 提取的调用数一致"""
        from callwarden_core import batch_parse_c_files, batch_parse_c_files_pool

        batch_results = batch_parse_c_files(self.files, num_threads=4)
        pool = batch_parse_c_files_pool(self.files, num_threads=4)

        batch_calls = sum(len(r["raw_calls"]) for r in batch_results)
        _, _, pool_calls, _ = pool.stats()
        self.assertEqual(batch_calls, pool_calls)

    def test_pool_get_at_matches_batch_index(self):
        """pool.get_at(i) 与 batch_results[i] 内容一致"""
        from callwarden_core import batch_parse_c_files, batch_parse_c_files_pool

        batch_results = batch_parse_c_files(self.files, num_threads=4)
        pool = batch_parse_c_files_pool(self.files, num_threads=4)

        for i in range(len(self.files)):
            b = batch_results[i]
            p = pool.get_at(i)
            # 符号名一致
            bn = sorted(s["name"] for s in b["symbols"])
            pn = sorted(s["name"] for s in p["symbols"])
            self.assertEqual(bn, pn)
            # content_hash 一致
            self.assertEqual(b["content_hash"], p["content_hash"])


if __name__ == "__main__":
    unittest.main()
